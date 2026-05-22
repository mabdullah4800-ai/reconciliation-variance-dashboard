"""
loader.py
---------
Reads the raw sales.csv / payouts.csv files and returns clean, correctly
typed pandas DataFrames ready for the matching engine.

A CSV is just text -- every cell arrives as a string, and some cells are
blank or malformed. This module does the unglamorous but essential work:

    * parse date strings into real datetimes (bad dates -> NaT)
    * coerce money columns into numbers (blank/garbage -> NaN)
    * trim stray whitespace
    * record a `data_issue` note on any row that needed fixing, so
      problems are *flagged for a human*, never silently dropped

Reconciliation breaks (a missing payout, a wrong amount) are NOT handled
here -- that is the matching engine's job. This module only deals with
data *quality*: is the row even readable? Keeping the two concerns
separate means a malformed cell and a genuine break never get confused.
"""

from pathlib import Path

import pandas as pd

# This tool reconciles a single settlement currency (see generate_data.py).
# Any other currency is flagged for a manual FX check, not converted here.
EXPECTED_CURRENCY = "EUR"

DATA_DIR = Path(__file__).parent / "data"


def _note_issue(issues: pd.Series, mask: pd.Series, text: str) -> pd.Series:
    """Append `text` to the data_issue note for every row where mask is True.

    Multiple issues on one row are joined with '; ' so nothing is lost.
    """
    addition = mask.map({True: text, False: ""})
    joined = issues.str.cat(addition, sep="; ").str.strip("; ")
    return joined


def clean_sales(df: pd.DataFrame) -> pd.DataFrame:
    """Type-cast and quality-check the sales table."""
    df = df.copy()

    # Strings: strip whitespace so " EUR" and "EUR" compare equal.
    for col in ["order_id", "currency", "customer_ref"]:
        df[col] = df[col].astype("string").str.strip()

    # Dates: explicit format; anything that doesn't fit becomes NaT.
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")

    # Money: blank or non-numeric cells become NaN rather than crashing.
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    # Build a per-row data-quality note. Empty string means "clean".
    issues = pd.Series("", index=df.index, dtype="string")
    issues = _note_issue(issues, df["amount"].isna(), "missing/invalid amount")
    issues = _note_issue(issues, df["date"].isna(), "unparseable date")
    issues = _note_issue(issues, df["customer_ref"].isna() | (df["customer_ref"] == ""),
                         "missing customer_ref")
    issues = _note_issue(issues, df["currency"] != EXPECTED_CURRENCY,
                         "non-EUR currency (manual FX check)")
    df["data_issue"] = issues

    return df


def clean_payouts(df: pd.DataFrame) -> pd.DataFrame:
    """Type-cast and quality-check the payouts table."""
    df = df.copy()

    for col in ["transaction_id", "order_id", "status"]:
        df[col] = df[col].astype("string").str.strip()

    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")

    for col in ["gross_amount", "fee", "net_amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    issues = pd.Series("", index=df.index, dtype="string")
    issues = _note_issue(issues, df["gross_amount"].isna(), "missing/invalid gross_amount")
    issues = _note_issue(issues, df["net_amount"].isna(), "missing/invalid net_amount")
    issues = _note_issue(issues, df["date"].isna(), "unparseable date")
    df["data_issue"] = issues

    return df


def load_data(data_dir: Path = DATA_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load and clean both files. Returns (sales, payouts).

    Raises FileNotFoundError with a helpful hint if the CSVs are missing,
    so a fresh clone fails loudly with an instruction instead of a
    cryptic pandas error.
    """
    sales_path = data_dir / "sales.csv"
    payouts_path = data_dir / "payouts.csv"
    for path in (sales_path, payouts_path):
        if not path.exists():
            raise FileNotFoundError(
                f"{path.name} not found in {data_dir}. "
                f"Run `python generate_data.py` first."
            )

    sales = clean_sales(pd.read_csv(sales_path))
    payouts = clean_payouts(pd.read_csv(payouts_path))
    return sales, payouts


if __name__ == "__main__":
    # Quick smoke test: load the data and report what cleaning found.
    sales, payouts = load_data()
    print(f"sales:   {len(sales):>4} rows, {(sales['data_issue'] != '').sum()} with data issues")
    print(f"payouts: {len(payouts):>4} rows, {(payouts['data_issue'] != '').sum()} with data issues")
    print("\nSample sales data-quality issues:")
    print(sales.loc[sales["data_issue"] != "", ["order_id", "data_issue"]].head(10).to_string(index=False))
