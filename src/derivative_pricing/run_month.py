"""Month-end batch entry point (key-free MVP slice).

Point it at a folder of broker statements; it routes each file, parses every
known broker deterministically, and writes one combined ALIC-style Call Options
sheet (.xlsx + .csv). Unknown/ambiguous files are reported, never silently
dropped.

CLI:
    uv run python -m derivative_pricing.run_month <statements_dir> \
        --as-of 2026-03-31 --out data/2026-03/output [--fx fx.csv --index idx.csv]

Market data is optional: when an index-prices CSV is not supplied, the builder
uses each statement's own month-end underlying price (and USD FX = 1), which is
enough to reproduce the analyst's notional/price columns for USD books.
"""

from __future__ import annotations

import argparse
import csv as _csv
from dataclasses import dataclass, field
from pathlib import Path

from derivative_pricing.adaptive.router import route_folder
from derivative_pricing.output import build_rows_from_marks, write_call_options_xlsx
from derivative_pricing.output.call_options_sheet import COLUMNS


@dataclass
class MonthRunSummary:
    as_of: str
    known: list[tuple[str, str, int]] = field(default_factory=list)   # (file, broker, n_marks)
    unknown: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)
    total_marks: int = 0
    output_xlsx: str | None = None
    output_csv: str | None = None


def run_month(
    statements_dir: Path | str,
    output_dir: Path | str,
    as_of: str,
    fx_rates: dict[str, float] | None = None,
    index_prices: dict[str, float] | None = None,
) -> MonthRunSummary:
    summary = MonthRunSummary(as_of=as_of)
    all_marks = []
    for r in route_folder(statements_dir):
        name = Path(r.path).name
        if r.status == "known":
            summary.known.append((name, r.broker, len(r.marks)))
            all_marks.extend(r.marks)
        elif r.status == "ambiguous":
            summary.ambiguous.append((name, r.candidates))
        else:
            summary.unknown.append(name)
    summary.total_marks = len(all_marks)

    rows = build_rows_from_marks(all_marks, fx_rates or {"USD": 1.0}, index_prices)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    xlsx = write_call_options_xlsx(
        rows, out / f"call_options_{as_of}.xlsx", as_of=as_of, broker_label="All brokers"
    )
    csv_path = out / f"call_options_{as_of}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(COLUMNS)
        for row in rows:
            w.writerow(row.as_cells())

    summary.output_xlsx = str(xlsx)
    summary.output_csv = str(csv_path)
    return summary


def _print_summary(s: MonthRunSummary) -> None:
    # ASCII-only: this prints to the Windows console (cp1252), which cannot
    # encode glyphs like checkmarks or em-dashes.
    print(f"\nMonth-end run - as of {s.as_of}")
    print("=" * 52)
    print(f"Known brokers parsed ({len(s.known)} files):")
    for name, broker, n in s.known:
        print(f"  [OK]   {broker:<16} {n:>5} trades   ({name})")
    if s.unknown:
        print(f"\nUnknown formats ({len(s.unknown)}) - need onboarding:")
        for name in s.unknown:
            print(f"  [????] {name}")
    if s.ambiguous:
        print(f"\nAmbiguous ({len(s.ambiguous)}) - need a human:")
        for name, cands in s.ambiguous:
            print(f"  [DUP]  {name}: matched {', '.join(cands)}")
    print(f"\nTotal trades: {s.total_marks}")
    print(f"Output: {s.output_xlsx}")
    print(f"        {s.output_csv}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Month-end broker-statement batch run")
    ap.add_argument("statements_dir", help="folder of broker statements (.xls/.xlsx)")
    ap.add_argument("--as-of", required=True, help="month-end date, e.g. 2026-03-31")
    ap.add_argument("--out", default="data/output", help="output directory")
    ap.add_argument("--fx", help="optional FX rates CSV (currency,rate_to_usd)")
    ap.add_argument("--index", help="optional index prices CSV (index_name,ticker,price)")
    args = ap.parse_args(argv)

    fx_rates = index_prices = None
    if args.fx or args.index:
        from derivative_pricing.reference.market_data import load_market_data

        md = load_market_data(args.as_of, args.fx, args.index)
        fx_rates, index_prices = md.fx_rates, md.index_prices

    summary = run_month(args.statements_dir, args.out, args.as_of, fx_rates, index_prices)
    _print_summary(summary)


if __name__ == "__main__":
    main()
