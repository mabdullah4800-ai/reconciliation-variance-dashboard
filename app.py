"""
app.py
------
The Streamlit dashboard. Run it with:

    streamlit run app.py

It ties the whole pipeline together -- load -> clean -> match ->
summarise -> visualise -- and presents the result the way an operations
team would want it: headline KPIs, where the money is breaking, when it
broke, and a filterable exception table to actually work from.

The dashboard makes no reconciliation decisions of its own. Every figure
comes from matcher.py and summary.py, so what you see on screen is
exactly what the engine concluded.
"""

import plotly.express as px
import streamlit as st

from loader import load_data
from matcher import reconcile
from summary import breaks_by_category, compute_kpis, variance_timeline

st.set_page_config(page_title="Reconciliation & Variance Dashboard",
                   page_icon="\U0001F4B6", layout="wide")

# Friendly labels for the raw status codes used inside the engine.
STATUS_LABELS = {
    "matched": "Matched",
    "missing_payout": "Missing payout",
    "orphan_payout": "Orphan payout",
    "amount_mismatch": "Amount mismatch",
    "fee_variance": "Fee variance",
    "duplicate_payout": "Duplicate payout",
    "data_issue": "Data issue",
}


@st.cache_data
def run_pipeline():
    """Load, clean, match and summarise. Cached so the page is snappy."""
    sales, payouts = load_data()
    results = reconcile(sales, payouts)
    return results


def main() -> None:
    st.title("Reconciliation & Variance Dashboard")
    st.caption(
        "Matches payment-processor payouts against recorded sales, "
        "flags the breaks, and quantifies the variance."
    )

    # A fresh clone has no CSVs until generate_data.py has been run.
    # Fail with a clear instruction rather than a raw stack trace.
    try:
        results = run_pipeline()
    except FileNotFoundError as exc:
        st.error(f"{exc}")
        st.stop()

    kpis = compute_kpis(results)

    # --- KPI cards ---------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Match rate", f"{kpis['match_rate']:.1%}",
              help="Share of orders that reconciled cleanly.")
    c2.metric("Exceptions", f"{kpis['exception_orders']:,}",
              help="Orders that did not reconcile and may need a human.")
    c3.metric("Value under exception", f"€{kpis['break_value_eur']:,.0f}",
              help="Absolute euro value of every break -- money in question.")
    c4.metric("Net settlement gap", f"€{kpis['net_settlement_gap_eur']:,.0f}",
              help="Total payouts minus total recorded sales.")

    st.divider()

    # --- Charts: where and when the money is breaking ----------------
    left, right = st.columns(2)

    by_cat = breaks_by_category(results)
    by_cat["Category"] = by_cat["status"].map(STATUS_LABELS)
    with left:
        st.subheader("Variance by category")
        fig = px.bar(by_cat, x="Category", y="break_value_eur",
                     text_auto=".2s", labels={"break_value_eur": "Value (€)"})
        fig.update_layout(showlegend=False, xaxis_title=None)
        st.plotly_chart(fig, width="stretch")

    timeline = variance_timeline(results)
    with right:
        st.subheader("Break value over time (weekly)")
        fig = px.bar(timeline, x="report_date", y="break_value_eur",
                     labels={"break_value_eur": "Value (€)", "report_date": "Week"})
        fig.update_layout(xaxis_title=None)
        st.plotly_chart(fig, width="stretch")

    st.divider()

    # --- Exception table an ops person can actually work from --------
    st.subheader("Exceptions")

    exceptions = results[results["status"] != "matched"].copy()
    exceptions["Status"] = exceptions["status"].map(STATUS_LABELS)

    # Filter controls. Default selection is every exception type.
    options = sorted(exceptions["Status"].unique())
    chosen = st.multiselect("Filter by status", options, default=options)
    search = st.text_input("Search order ID", placeholder="e.g. ORD-10105")

    view = exceptions[exceptions["Status"].isin(chosen)]
    if search:
        view = view[view["order_id"].str.contains(search, case=False, na=False)]

    st.write(f"Showing **{len(view)}** of {len(exceptions)} exceptions.")
    st.dataframe(
        view[["order_id", "Status", "sale_date", "payout_date",
              "sale_amount", "payout_gross", "variance", "detail"]],
        width="stretch", hide_index=True,
        column_config={
            "sale_amount": st.column_config.NumberColumn("Sale (€)", format="%.2f"),
            "payout_gross": st.column_config.NumberColumn("Payout (€)", format="%.2f"),
            "variance": st.column_config.NumberColumn("Variance (€)", format="%.2f"),
        },
    )

    # Let an ops user export the filtered view to work it offline.
    st.download_button(
        "Download these exceptions (CSV)",
        view.to_csv(index=False).encode("utf-8"),
        file_name="exceptions.csv", mime="text/csv",
    )


if __name__ == "__main__":
    main()
