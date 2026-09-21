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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from derivative_pricing.adaptive.drift import check_spec_drift
from derivative_pricing.adaptive.router import route_folder
from derivative_pricing.adaptive.spec import ParserSpec
from derivative_pricing.adaptive.statement_date import statement_date
from derivative_pricing.ingest import screen_marks
from derivative_pricing.output import (
    build_rows_from_marks,
    split_marks_by_option_type,
    write_call_options_sectioned_xlsx,
)
from derivative_pricing.output.call_options_sheet import COLUMNS


@dataclass
class MonthRunSummary:
    as_of: str
    known: list[tuple[str, str, int]] = field(default_factory=list)   # (file, broker, n_marks)
    unknown: list[str] = field(default_factory=list)
    # Files that could not be READ (encrypted, corrupt). Kept apart from
    # ``unknown`` because the remedies are opposite: an unknown layout needs a
    # spec authored, an unreadable file needs a password or a re-send. Reporting
    # an encrypted file as "needs onboarding" sends a human down a dead end.
    unreadable: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)
    # A second file for a broker already loaded this run. Excluded from totals
    # (double-counting a broker would silently inflate every control total);
    # a human decides which file is authoritative.
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (file, broker)
    # Known broker whose file parsed but whose LAYOUT changed vs the saved spec
    # (a column renamed/dropped since the spec was authored). Non-fatal: the run
    # still produces output, but a human is told the format drifted so a silent
    # column loss can't slip through month to month.
    format_warnings: list[tuple[str, str]] = field(default_factory=list)  # (broker, detail)
    # Per-entry screening (repeats removed from the priced set; matured flagged).
    repeat_entries: list[tuple[str, str]] = field(default_factory=list)   # (broker, trade_ref)
    matured_entries: list[tuple[str, str, str]] = field(default_factory=list)  # (broker, ref, detail)
    unknown_maturity: list[tuple[str, str]] = field(default_factory=list)
    total_marks: int = 0
    # Calls and puts are written to separate ALIC-shaped sheets. The MVP prices
    # calls; puts are captured and split out so they are never mixed into the
    # calls sheet (which would double the totals) nor silently dropped.
    call_marks: int = 0
    put_marks: int = 0
    output_xlsx: str | None = None
    output_csv: str | None = None
    put_output_xlsx: str | None = None
    put_output_csv: str | None = None
    # Each statement's own stated valuation date, and whether they agree with
    # the as-of this run was given. A month-end run whose files say a different
    # date is almost always the wrong folder or a stale default, and the damage
    # is silent: the sheet is mis-stamped and the maturity screen compares every
    # expiry against the wrong day.
    statement_dates: list[tuple[str, str]] = field(default_factory=list)  # (broker, iso date)
    date_mismatches: list[tuple[str, str]] = field(default_factory=list)  # (broker, iso date)


def run_month(
    statements_dir: Path | str,
    output_dir: Path | str,
    as_of: str,
    fx_rates: dict[str, float] | None = None,
    index_prices: dict[str, float] | None = None,
    specs: list[ParserSpec] | None = None,
) -> MonthRunSummary:
    summary = MonthRunSummary(as_of=as_of)
    all_marks = []
    seen_brokers: set[str] = set()
    for r in route_folder(statements_dir, specs):
        name = Path(r.path).name
        if r.status == "known":
            if r.broker in seen_brokers:
                summary.duplicates.append((name, r.broker))
                continue
            seen_brokers.add(r.broker)
            summary.known.append((name, r.broker, len(r.marks)))
            all_marks.extend(r.marks)
            # Date check: what does this statement itself say it values as of?
            stated = statement_date(r.spec, r.path)
            if stated:
                summary.statement_dates.append((r.broker, stated))
                if stated != as_of:
                    summary.date_mismatches.append((r.broker, stated))
            # Layout check: parsing succeeded, but did the format drift vs the
            # saved spec (a column renamed/dropped since it was authored)?
            drift = check_spec_drift(r.spec, r.path)
            if drift.missing_required or drift.missing_optional:
                summary.format_warnings.append((r.broker, drift.summary()))
        elif r.status == "ambiguous":
            summary.ambiguous.append((name, r.candidates))
        elif r.error:
            summary.unreadable.append((name, r.error))
        else:
            summary.unknown.append(name)

    # Screen every entry for repeats (dropped) and maturity (flagged, kept).
    screen = screen_marks(all_marks, as_of)
    summary.repeat_entries = [(f.broker, f.trade_ref) for f in screen.repeats]
    summary.matured_entries = [(f.broker, f.trade_ref, f.detail) for f in screen.matured]
    summary.unknown_maturity = [(f.broker, f.trade_ref) for f in screen.unknown_maturity]
    priced_marks = screen.kept
    summary.total_marks = len(priced_marks)

    # Calls and puts go to separate ALIC sheets (see MonthRunSummary note).
    call_marks, put_marks = split_marks_by_option_type(priced_marks)
    summary.call_marks = len(call_marks)
    summary.put_marks = len(put_marks)

    fx = fx_rates or {"USD": 1.0}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ALIC-shaped calls sheet: per-broker sections + subtotals + grand total.
    call_rows = build_rows_from_marks(call_marks, fx, index_prices)
    summary.output_xlsx = str(write_call_options_sectioned_xlsx(
        call_rows, out / f"call_options_{as_of}.xlsx", as_of=as_of,
        sheet_label="Call Options"))
    summary.output_csv = str(_write_rows_csv(call_rows, out / f"call_options_{as_of}.csv"))

    # Matching puts sheet -- only when there are puts (no empty artifacts).
    if put_marks:
        put_rows = build_rows_from_marks(put_marks, fx, index_prices)
        summary.put_output_xlsx = str(write_call_options_sectioned_xlsx(
            put_rows, out / f"put_options_{as_of}.xlsx", as_of=as_of,
            sheet_label="Put Options"))
        summary.put_output_csv = str(_write_rows_csv(put_rows, out / f"put_options_{as_of}.csv"))

    return summary


def detect_as_of(statements_dir: Path | str,
                 specs: list[ParserSpec] | None = None) -> tuple[str | None, dict[str, str]]:
    """(consensus as-of, {broker: stated date}) for a folder of statements.

    The consensus is the most commonly stated date, which tolerates one broker
    valuing a day early (HSBC has been seen closing a Friday when the rest close
    the following Monday) without that one file dragging the whole run onto the
    wrong date. Returns (None, {...}) when no statement states a date, so the
    caller can insist on an explicit --as-of rather than inventing one.
    """
    from derivative_pricing.adaptive.router import identify

    stated: dict[str, str] = {}
    for path in sorted(Path(statements_dir).iterdir()):
        if path.suffix.lower() not in (".xls", ".xlsx") or path.name.startswith("~$"):
            continue
        try:
            matches = identify(path, specs)
        except Exception:  # noqa: BLE001 -- unreadable files simply have no date
            continue
        if len(matches) != 1:
            continue
        got = statement_date(matches[0], path)
        if got:
            stated[matches[0].broker.value] = got
    if not stated:
        return None, {}
    counts = Counter(stated.values())
    # Tie-break on the later date: a stale file left in the folder is a more
    # common mistake than the whole book being valued early.
    top = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return top[0], stated


def _write_rows_csv(rows, path: Path) -> Path:
    """Flat CSV: one row per trade, for downstream/machine use."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(COLUMNS)
        for row in rows:
            w.writerow(row.as_cells())
    return path


def _print_summary(s: MonthRunSummary) -> None:
    # ASCII-only: this prints to the Windows console (cp1252), which cannot
    # encode glyphs like checkmarks or em-dashes.
    print(f"\nMonth-end run - as of {s.as_of}")
    print("=" * 52)
    print(f"Known brokers parsed ({len(s.known)} files):")
    for name, broker, n in s.known:
        print(f"  [OK]   {broker:<16} {n:>5} trades   ({name})")
    if s.date_mismatches:
        # Severity depends on whether the as-of is outvoted. A single broker
        # closing a day early is normal (HSBC does it); the as-of agreeing with
        # NO statement means the wrong folder or a stale default, which silently
        # mis-stamps the sheet and points the maturity screen at the wrong day.
        agreeing = sum(1 for _, d in s.statement_dates if d == s.as_of)
        if agreeing == 0:
            print(f"\n*** AS-OF MISMATCH - no statement values as of {s.as_of}:")
            for broker, stated in s.date_mismatches:
                print(f"  [DATE] {broker:<16} statement says {stated}")
            print("         Wrong folder or a stale --as-of. The sheet is mis-stamped AND")
            print("         the maturity screen is comparing expiries to the wrong day.")
        else:
            print(f"\nStatement dates ({agreeing} of {len(s.statement_dates)} agree with "
                  f"{s.as_of}) - these value earlier/later, which can be legitimate:")
            for broker, stated in s.date_mismatches:
                print(f"  [date] {broker:<16} values as of {stated}")
    if s.unknown:
        print(f"\nUnknown formats ({len(s.unknown)}) - need onboarding:")
        for name in s.unknown:
            print(f"  [????] {name}")
    if s.unreadable:
        print(f"\nUnreadable files ({len(s.unreadable)}) - CANNOT be onboarded, fix the file:")
        for name, reason in s.unreadable:
            print(f"  [LOCK] {name}: {reason}")
    if s.duplicates:
        print(f"\nDuplicate broker files ({len(s.duplicates)}) - EXCLUDED from totals, pick one per broker:")
        for name, broker in s.duplicates:
            print(f"  [SKIP] {name} ({broker} already loaded)")
    if s.ambiguous:
        print(f"\nAmbiguous ({len(s.ambiguous)}) - need a human:")
        for name, cands in s.ambiguous:
            print(f"  [AMBG] {name}: matched {', '.join(cands)}")
    if s.format_warnings:
        print(f"\nFormat drift ({len(s.format_warnings)}) - parsed OK but layout changed, review:")
        for broker, detail in s.format_warnings:
            print(f"  [DRIFT] {detail}")
    if s.repeat_entries:
        print(f"\nRepeat entries dropped ({len(s.repeat_entries)}):")
        for broker, ref in s.repeat_entries[:20]:
            print(f"  [DUP]  {broker} {ref}")
    if s.matured_entries:
        print(f"\nMatured entries flagged ({len(s.matured_entries)}) - kept, review settlement:")
        for broker, ref, detail in s.matured_entries[:20]:
            print(f"  [MATD] {broker} {ref} ({detail})")
    if s.unknown_maturity:
        print(f"\nUnparseable maturity ({len(s.unknown_maturity)}):")
        for broker, ref in s.unknown_maturity[:20]:
            print(f"  [?MAT] {broker} {ref}")
    print(f"\nTotal trades (priced): {s.total_marks}  (calls: {s.call_marks}, puts: {s.put_marks})")
    print(f"Calls output: {s.output_xlsx}")
    print(f"              {s.output_csv}")
    if s.put_output_xlsx:
        print(f"Puts output:  {s.put_output_xlsx}")
        print(f"              {s.put_output_csv}")
    else:
        print("Puts output:  none (no put options in this run)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Month-end broker-statement batch run")
    ap.add_argument("statements_dir", help="folder of broker statements (.xls/.xlsx)")
    ap.add_argument("--as-of", help="month-end date, e.g. 2026-03-31. Omit to take it "
                                    "from the statements' own valuation dates.")
    ap.add_argument("--out", default="data/output", help="output directory")
    ap.add_argument("--fx", help="optional FX rates CSV (currency,rate_to_usd)")
    ap.add_argument("--index", help="optional index prices CSV (index_name,ticker,price)")
    args = ap.parse_args(argv)

    as_of = args.as_of
    if not as_of:
        as_of, stated = detect_as_of(args.statements_dir)
        if not as_of:
            ap.error("no statement in that folder states a valuation date; pass --as-of")
        print(f"as-of taken from the statements: {as_of}")
        for broker, date in sorted(stated.items()):
            if date != as_of:
                print(f"  note: {broker} states {date}, not {as_of}")

    fx_rates = index_prices = None
    if args.fx or args.index:
        from derivative_pricing.reference.market_data import load_market_data

        md = load_market_data(as_of, args.fx, args.index)
        fx_rates, index_prices = md.fx_rates, md.index_prices

    summary = run_month(args.statements_dir, args.out, as_of, fx_rates, index_prices)
    _print_summary(summary)


if __name__ == "__main__":
    main()
