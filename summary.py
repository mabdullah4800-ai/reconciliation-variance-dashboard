"""
summary.py
----------
Turns the row-level reconciliation results into the headline numbers an
operations team actually reports on:

    * match rate -- what share of orders reconciled cleanly
    * total value under exception -- how much money is in question
    * a breakdown of breaks by category (count and euro value)
    * a weekly trend of break value, to spot a bad week

This module only aggregates. It makes no matching decisions -- those
all happened in matcher.py -- so the dashboard and any report draw
from one consistent set of figures.
"""

import pandas as pd

# Statuses that represent a clean reconciliation. Everything else is an
# exception a human may need to work.
CLEAN_STATUSES = {"matched"}


def compute_kpis(results: pd.DataFrame) -> dict:
    """Headline KPIs for the dashboard's summary cards."""
    total_orders = len(results)
    matched = int(results["status"].isin(CLEAN_STATUSES).sum())
    exceptions = total_orders - matched

    # Money the business recorded as sales vs money the processor moved.
    total_sales = float(results["sale_amount"].sum())       # NaNs skipped
    total_payouts = float(results["payout_gross"].sum())

    # Value under exception: the absolute euro size of every break.
    # Absolute, because a missing payout (we are owed money) and a
    # duplicate (we were overpaid) are both money in question -- they
    # must not net each other off to a misleadingly small number.
    breaks = results[~results["status"].isin(CLEAN_STATUSES)]
    break_value = float(breaks["variance"].abs().sum())

    return {
        "total_orders": total_orders,
        "matched_orders": matched,
        "exception_orders": exceptions,
        # Guard against an empty input so the dashboard never divides by 0.
        "match_rate": matched / total_orders if total_orders else 0.0,
        "total_sales_eur": total_sales,
        "total_payouts_eur": total_payouts,
        # Signed: positive means the processor paid out more than we sold.
        "net_settlement_gap_eur": total_payouts - total_sales,
        "break_value_eur": break_value,
    }


def breaks_by_category(results: pd.DataFrame) -> pd.DataFrame:
    """Count and euro value of breaks, one row per status.

    Sorted by euro value so the most material problem is on top.
    """
    breaks = results[~results["status"].isin(CLEAN_STATUSES)].copy()
    breaks["abs_variance"] = breaks["variance"].abs()

    summary = (
        breaks.groupby("status")
        .agg(break_count=("order_id", "size"),
             break_value_eur=("abs_variance", "sum"))
        .reset_index()
        .sort_values("break_value_eur", ascending=False, ignore_index=True)
    )
    return summary


def variance_timeline(results: pd.DataFrame, freq: str = "W") -> pd.DataFrame:
    """Break value over time, bucketed by `freq` (default weekly).

    Each break is dated by its sale date, falling back to the payout
    date for orphan payouts (which have no sale). Lets the dashboard
    show whether breaks cluster in a particular week.
    """
    breaks = results[~results["status"].isin(CLEAN_STATUSES)].copy()

    # One date per break: sale_date normally, payout_date for orphans.
    breaks["report_date"] = breaks["sale_date"].fillna(breaks["payout_date"])
    breaks = breaks.dropna(subset=["report_date"])
    breaks["abs_variance"] = breaks["variance"].abs()

    timeline = (
        breaks.groupby(pd.Grouper(key="report_date", freq=freq))
        .agg(break_count=("order_id", "size"),
             break_value_eur=("abs_variance", "sum"))
        .reset_index()
    )
    return timeline


if __name__ == "__main__":
    # Smoke test: load, reconcile, summarise, print.
    from loader import load_data
    from matcher import reconcile

    sales, payouts = load_data()
    results = reconcile(sales, payouts)
    kpis = compute_kpis(results)

    print("KPIs")
    print(f"  match rate           {kpis['match_rate']:.1%}")
    print(f"  orders reconciled    {kpis['matched_orders']} / {kpis['total_orders']}")
    print(f"  exceptions           {kpis['exception_orders']}")
    print(f"  total sales          EUR {kpis['total_sales_eur']:,.2f}")
    print(f"  total payouts        EUR {kpis['total_payouts_eur']:,.2f}")
    print(f"  net settlement gap   EUR {kpis['net_settlement_gap_eur']:,.2f}")
    print(f"  value under exception EUR {kpis['break_value_eur']:,.2f}")
    print("\nBreaks by category")
    print(breaks_by_category(results).to_string(index=False))
