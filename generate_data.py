"""
generate_data.py
----------------
Creates two synthetic CSV files that *should* reconcile against each other:

    data/sales.csv    -- what the business recorded as sales
    data/payouts.csv  -- what the payment processor says it paid out

In a perfect world every sale has exactly one matching payout for the
same amount. The real world is messier, so this script deliberately
seeds ~5-10% of "breaks" -- the exact problems a reconciliation process
exists to catch:

    * missing payouts      -- a sale with no corresponding payout
    * amount mismatches    -- payout gross != sale amount
    * duplicate payouts    -- the same order paid out twice
    * fee variances        -- processor fee far off the expected rate
    * orphan payouts       -- a payout for an order that was never recorded

Everything is generated from a fixed random seed so the output is
reproducible: re-running the script gives the identical files.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

# A fixed seed makes the data reproducible. Anyone who clones the repo
# and runs this gets byte-identical CSVs, so the dashboard numbers match.
SEED = 42
random.seed(SEED)

# How many "clean" sales to generate before we inject breaks.
N_SALES = 500

# Expila processor fee model: most payment processors charge a
# percentage of the transaction plus a small fixed fee.
FEE_RATE = 0.029          # 2.9%
FEE_FIXED = 0.30          # + EUR 0.30 per transaction

# Where the CSVs go. data/ is git-ignored so we never commit generated files.
DATA_DIR = Path(__file__).parent / "data"

# Currency mix -- mostly EUR (Dublin/IFSC context), a few GBP/USD.
CURRENCIES = ["EUR", "EUR", "EUR", "EUR", "GBP", "USD"]

START_DATE = date(2024, 1, 1)
DATE_RANGE_DAYS = 120


def expected_fee(amount: float) -> float:
    """The fee the processor *should* charge for a given gross amount."""
    return round(amount * FEE_RATE + FEE_FIXED, 2)


def random_date(start: date, span_days: int) -> date:
    """A random date in [start, start + span_days]."""
    return start + timedelta(days=random.randint(0, span_days))


def make_sales(n: int) -> list[dict]:
    """Generate `n` clean sales rows."""
    sales = []
    for i in range(n):
        order_id = f"ORD-{10000 + i}"
        sales.append(
            {
                "order_id": order_id,
                "date": random_date(START_DATE, DATE_RANGE_DAYS).isoformat(),
                # Amounts skew low with an occasional big order -- a
                # log-ish distribution looks more realistic than uniform.
                "amount": round(random.uniform(5, 500) * random.choice([1, 1, 1, 3]), 2),
                "currency": random.choice(CURRENCIES),
                "customer_ref": f"CUST-{random.randint(1000, 1300)}",
            }
        )
    return sales


def make_payout(sale: dict, transaction_id: str) -> dict:
    """Build the 'correct' payout that matches a given sale."""
    gross = sale["amount"]
    fee = expected_fee(gross)
    return {
        "transaction_id": transaction_id,
        "order_id": sale["order_id"],
        # Payouts settle a few days after the sale -- a normal lag,
        # not a break. The matching engine must tolerate this.
        "date": (date.fromisoformat(sale["date"]) + timedelta(days=random.randint(1, 4))).isoformat(),
        "gross_amount": gross,
        "fee": fee,
        "net_amount": round(gross - fee, 2),
        "status": "completed",
    }


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    sales = make_sales(N_SALES)

    payouts = []
    txn_counter = 0

    def next_txn_id() -> str:
        nonlocal txn_counter
        txn_counter += 1
        return f"TXN-{500000 + txn_counter}"

    # Decide upfront which sales become which kind of break. Sampling
    # distinct sales keeps the break categories from overlapping, so
    # every flagged row has exactly one explanation.
    pool = list(range(N_SALES))
    random.shuffle(pool)

    missing_idx = set(pool[0:18])        # ~3.6%  -- sale exists, no payout
    amount_idx = set(pool[18:33])        # ~3.0%  -- payout gross != sale amount
    duplicate_idx = set(pool[33:45])     # ~2.4%  -- order paid out twice
    fee_idx = set(pool[45:60])           # ~3.0%  -- fee far off expected rate
    # The remaining ~85% are clean matches.

    for i, sale in enumerate(sales):
        if i in missing_idx:
            # BREAK: no payout row at all. The sale will be unmatched.
            continue

        payout = make_payout(sale, next_txn_id())

        if i in amount_idx:
            # BREAK: processor reports a different gross than we recorded.
            # Could be a partial refund, a capture error, or fraud.
            payout["gross_amount"] = round(sale["amount"] * random.uniform(0.5, 1.4), 2)
            payout["fee"] = expected_fee(payout["gross_amount"])
            payout["net_amount"] = round(payout["gross_amount"] - payout["fee"], 2)

        if i in fee_idx:
            # BREAK: fee is wrong while gross is right. Net stops tying out.
            payout["fee"] = round(payout["fee"] * random.uniform(1.8, 3.5), 2)
            payout["net_amount"] = round(payout["gross_amount"] - payout["fee"], 2)

        payouts.append(payout)

        if i in duplicate_idx:
            # BREAK: the same order is paid out a second time under a
            # new transaction_id -- a double-payment we'd want clawed back.
            dup = make_payout(sale, next_txn_id())
            payouts.append(dup)

    # A handful of orphan payouts: money moved for an order that never
    # appears in sales.csv. These must surface as exceptions, not vanish.
    for _ in range(6):
        ghost_order = f"ORD-{random.randint(90000, 99999)}"
        amount = round(random.uniform(20, 300), 2)
        fee = expected_fee(amount)
        payouts.append(
            {
                "transaction_id": next_txn_id(),
                "order_id": ghost_order,
                "date": random_date(START_DATE, DATE_RANGE_DAYS).isoformat(),
                "gross_amount": amount,
                "fee": fee,
                "net_amount": round(amount - fee, 2),
                "status": "completed",
            }
        )

    # Sprinkle a few non-completed statuses across real payouts so the
    # dashboard has something to say about pending/failed money too.
    for payout in random.sample(payouts, 12):
        payout["status"] = random.choice(["pending", "failed"])

    # Shuffle so the files don't arrive in a tidy, unrealistic order.
    random.shuffle(payouts)

    sales_path = DATA_DIR / "sales.csv"
    with sales_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["order_id", "date", "amount", "currency", "customer_ref"])
        writer.writeheader()
        writer.writerows(sales)

    payouts_path = DATA_DIR / "payouts.csv"
    with payouts_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["transaction_id", "order_id", "date", "gross_amount", "fee", "net_amount", "status"],
        )
        writer.writeheader()
        writer.writerows(payouts)

    print(f"Wrote {len(sales)} rows  -> {sales_path}")
    print(f"Wrote {len(payouts)} rows -> {payouts_path}")
    print(
        f"Seeded breaks: {len(missing_idx)} missing, {len(amount_idx)} amount, "
        f"{len(duplicate_idx)} duplicate, {len(fee_idx)} fee, 6 orphan payouts."
    )


if __name__ == "__main__":
    main()
