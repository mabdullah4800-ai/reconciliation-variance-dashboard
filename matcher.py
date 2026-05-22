"""
matcher.py
----------
The matching engine -- the core of the reconciliation.

It takes the cleaned sales and payouts tables and produces ONE result
row per order, each labelled with exactly one status:

    matched          sale and payout agree (within tolerance)
    missing_payout   sale recorded, but the processor never paid out
    orphan_payout    processor paid out, but no sale was recorded
    amount_mismatch  payout gross differs from the sale amount
    fee_variance     gross is right, but the fee is off the contracted rate
    duplicate_payout the same order was paid out two or more times
    data_issue       a malformed cell makes the row impossible to compare

WHY ONE ROW PER ORDER: `order_id` is the key both systems share, so it
is the natural unit of reconciliation. An ops person works the output
order by order, so the output is shaped that way.

WHY A TOLERANCE: floating-point money rounds at different points in
different systems, so a payout can land a cent off a sale without
anything being wrong. Comparing for *exact* equality would flag those
harmless rounding differences as breaks and bury the real ones. Every
amount comparison therefore uses TOLERANCE_EUR; anything inside it is
treated as agreement.

NOTHING IS DROPPED: every sale and every payout ends up in the output
under some status. An unexplained payout becomes `orphan_payout`, not a
silently discarded row -- because in finance the row you dropped is the
one that mattered.
"""

import pandas as pd

# Amount agreement tolerance. 5 cents comfortably covers rounding noise
# while staying far below any real break (the smallest seeded amount
# mismatch is tens of euros). Tighten it if your processor is exact.
TOLERANCE_EUR = 0.05

# The processor's contracted fee schedule. The matcher knows this
# independently of the data -- it is what the *contract* says the fee
# should be, so a fee that drifts from it is a genuine variance.
FEE_RATE = 0.029      # 2.9% of gross
FEE_FIXED = 0.30      # + EUR 0.30 per transaction


def expected_fee(gross: float) -> float:
    """The fee the processor is contracted to charge on a given gross."""
    return round(gross * FEE_RATE + FEE_FIXED, 2)


def reconcile(sales: pd.DataFrame, payouts: pd.DataFrame,
              tolerance: float = TOLERANCE_EUR) -> pd.DataFrame:
    """Match payouts to sales and classify every order.

    Returns a DataFrame with one row per order_id and these columns:
        order_id, status, sale_amount, payout_gross, payout_fee,
        expected_fee, variance, n_payouts, settlement_lag_days, detail
    """
    # Group payouts by the shared key. A list per order_id lets us see
    # at a glance whether an order was paid zero, one, or many times.
    payouts_by_order: dict[str, pd.DataFrame] = {
        order_id: group for order_id, group in payouts.groupby("order_id")
    }

    results = []
    matched_order_ids = set()

    # --- Pass 1: walk every recorded sale ----------------------------
    for sale in sales.itertuples(index=False):
        order_id = sale.order_id
        group = payouts_by_order.get(order_id)
        n_payouts = 0 if group is None else len(group)
        matched_order_ids.add(order_id)

        row = {
            "order_id": order_id,
            "sale_date": sale.date,
            "payout_date": pd.NaT,
            "sale_amount": sale.amount,
            "payout_gross": pd.NA,
            "payout_fee": pd.NA,
            "expected_fee": pd.NA,
            "variance": pd.NA,
            "n_payouts": n_payouts,
            "settlement_lag_days": pd.NA,
            "status": "",
            "detail": "",
        }

        # A malformed sale cell -- we cannot trust a comparison on it.
        if pd.isna(sale.amount):
            row["status"] = "data_issue"
            row["detail"] = "sale amount is missing/invalid -- cannot compare"
            results.append(row)
            continue

        # The sale exists but the processor never paid it out.
        if n_payouts == 0:
            row["status"] = "missing_payout"
            row["variance"] = -sale.amount      # we are owed this money
            row["detail"] = "sale recorded, no payout found"
            results.append(row)
            continue

        # Paid out more than once -- a double payment to claw back.
        if n_payouts >= 2:
            total_gross = group["gross_amount"].sum()
            row["payout_gross"] = total_gross
            row["payout_fee"] = group["fee"].sum()
            row["payout_date"] = group["date"].min()
            row["variance"] = total_gross - sale.amount
            row["status"] = "duplicate_payout"
            row["detail"] = (
                f"{n_payouts} payouts for one order "
                f"({', '.join(group['transaction_id'])})"
            )
            results.append(row)
            continue

        # Exactly one payout -- the normal case. Compare the figures.
        payout = group.iloc[0]
        row["payout_gross"] = payout["gross_amount"]
        row["payout_fee"] = payout["fee"]
        row["payout_date"] = payout["date"]
        row["expected_fee"] = expected_fee(payout["gross_amount"])
        if pd.notna(payout["date"]) and pd.notna(sale.date):
            row["settlement_lag_days"] = (payout["date"] - sale.date).days

        # A malformed payout cell -- same logic as a malformed sale.
        if pd.isna(payout["gross_amount"]):
            row["status"] = "data_issue"
            row["detail"] = "payout gross is missing/invalid -- cannot compare"
            results.append(row)
            continue

        gross_diff = payout["gross_amount"] - sale.amount
        fee_diff = payout["fee"] - row["expected_fee"]
        row["variance"] = gross_diff

        # Order of checks: a wrong gross is the bigger problem, so test
        # it first. Only if the gross is right do we scrutinise the fee.
        if abs(gross_diff) > tolerance:
            row["status"] = "amount_mismatch"
            row["detail"] = f"payout gross is {gross_diff:+.2f} vs the sale"
        elif abs(fee_diff) > tolerance:
            row["status"] = "fee_variance"
            row["variance"] = fee_diff       # the break here is the fee
            row["detail"] = (
                f"fee {payout['fee']:.2f} vs contracted {row['expected_fee']:.2f} "
                f"({fee_diff:+.2f})"
            )
        else:
            row["status"] = "matched"
            row["detail"] = "within tolerance"

        results.append(row)

    # --- Pass 2: payouts whose order_id never appeared in sales ------
    orphan_ids = set(payouts_by_order) - matched_order_ids
    for order_id in orphan_ids:
        group = payouts_by_order[order_id]
        total_gross = group["gross_amount"].sum()
        results.append({
            "order_id": order_id,
            "sale_date": pd.NaT,
            "payout_date": group["date"].min(),
            "sale_amount": pd.NA,
            "payout_gross": total_gross,
            "payout_fee": group["fee"].sum(),
            "expected_fee": pd.NA,
            "variance": total_gross,         # money out with nothing behind it
            "n_payouts": len(group),
            "settlement_lag_days": pd.NA,
            "status": "orphan_payout",
            "detail": "payout with no matching sale",
        })

    column_order = [
        "order_id", "status", "sale_date", "payout_date",
        "sale_amount", "payout_gross", "payout_fee", "expected_fee",
        "variance", "n_payouts", "settlement_lag_days", "detail",
    ]
    return pd.DataFrame(results, columns=column_order)


if __name__ == "__main__":
    # Smoke test: load, reconcile, and show the status breakdown.
    from loader import load_data

    sales, payouts = load_data()
    results = reconcile(sales, payouts)
    print(f"Reconciled {len(results)} orders.\n")
    print(results["status"].value_counts().to_string())
    print("\nSample breaks:")
    breaks = results[~results["status"].isin(["matched"])]
    print(breaks[["order_id", "status", "variance", "detail"]].head(12).to_string(index=False))
