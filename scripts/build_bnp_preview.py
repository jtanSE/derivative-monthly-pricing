"""Demo runner: parse the BNP statement and emit an ALIC-style Call Options
sheet, then print a summary.

Usage:
    python scripts/build_bnp_preview.py <bnp_statement.xls>

Output is written under data/2026-03/output/ (git-ignored).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from derivative_pricing.output import build_rows_from_marks, write_call_options_xlsx
from derivative_pricing.parsers.bnp import BnpParser


def main(bnp_path: str) -> None:
    marks = BnpParser().parse(bnp_path)
    rows = build_rows_from_marks(marks, fx_rates={"USD": 1.0})

    out_dir = REPO / "data" / "2026-03" / "output"
    xlsx = write_call_options_xlsx(
        rows,
        out_dir / "BNP_call_options_preview.xlsx",
        as_of="2026-03-31",
        broker_label="BNP Paribas",
    )

    # also a CSV for quick inspection
    csv_path = out_dir / "BNP_call_options_preview.csv"
    import csv as _csv

    from derivative_pricing.output.call_options_sheet import COLUMNS

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow(r.as_cells())

    print(f"Parsed {len(marks)} BNP marks -> {len(rows)} Call Options rows")
    print(f"Wrote: {xlsx}")
    print(f"Wrote: {csv_path}")

    print("\n=== First 12 rows (Trade Ref | Index | Strike | Maturity | Shares | "
          "Notional(Ccy) | MTM(Ccy) | Price(Ccy)) ===")
    for r in rows[:12]:
        print(
            f"  {r.trade_reference:<14} {str(r.index)[:22]:<22} "
            f"{r.strike_rate!s:>10} {str(r.maturity_date):<11} "
            f"{r.shares!s:>9} {r.notional_local!s:>14} "
            f"{r.mtm_local!s:>12} {r.price_local!s:>10}"
        )

    # quick sanity totals
    n_priced = sum(1 for r in rows if r.notional_local is not None)
    tot_mtm = sum(r.mtm_local for r in rows if r.mtm_local is not None)
    tot_notl = sum(r.notional_local for r in rows if r.notional_local is not None)
    print(f"\nRows with computed notional: {n_priced}/{len(rows)}")
    print(f"Total MTM (Ccy): {tot_mtm:,.2f}")
    print(f"Total Notional (Ccy): {tot_notl:,.2f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])