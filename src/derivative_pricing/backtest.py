"""Backtest / column-discovery harness.

Systematizes the analyst's manual reverse-engineering. Given a PAST month's
analyst-confirmed ALIC file (the ground truth) and the raw broker statement for
that same month, it finds which brokerage column produced each confirmed output
field -- by joining on trade ref and scoring every column against the confirmed
values. This is how you'd discover, automatically, that Nomura's "shares" comes
from the ``exchangedNotional1Amount`` column instead of a share-count column.

Two ways to use it:
  discover_field_source(...)  -> rank a statement's columns by how well they
      reproduce ONE confirmed field (the "training" step).
  discover_all(...)           -> do that for every numeric field at once
      (a full mapping report for a broker).

Magnitude fields (shares, notional) are scored under BOTH exact and
absolute-value matching, because ALIC carries the long/short sign on shares
while the broker column is usually an unsigned magnitude plus a direction
column. The harness reports whichever mode matched better, so a sign convention
never hides the right column.

CLI:
    uv run python -m derivative_pricing.backtest \
        --confirmed "<old ALIC>.xls" --statement "<old broker file>.xls" \
        --broker Nomura --field shares
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from derivative_pricing.adaptive.agent import load_reference_specs
from derivative_pricing.adaptive.engine import _find_header_row, _map_columns
from derivative_pricing.adaptive.reader import load_grid
from derivative_pricing.adaptive.router import route
from derivative_pricing.adaptive.spec import ParserSpec
from derivative_pricing.output import build_rows_from_marks, split_marks_by_option_type
from derivative_pricing.schema import Broker
from derivative_pricing.workbook.call_options import read_call_options

REPO_ROOT = Path(__file__).resolve().parents[2]

# Confirmed field -> how to read it off an analyst CallOptionRow. Numeric only:
# these are the values a brokerage column could reproduce.
_CONFIRMED_GETTERS = {
    "shares": lambda r: r.shares,
    "strike": lambda r: r.strike,
    "mtm": lambda r: r.mtm_local,
    "notional": lambda r: r.notional_local,
    "price": lambda r: r.price_local,
}
FIELDS = tuple(_CONFIRMED_GETTERS)


def _close(a: float, b: float, tol_rel: float) -> bool:
    return abs(a - b) <= tol_rel * max(abs(a), abs(b), 1.0)


def spec_for_broker(broker: Broker) -> ParserSpec:
    """The known spec for a broker (needed for the header anchor + ref column)."""
    for s in load_reference_specs():
        if s.broker == broker:
            return s
    raise KeyError(f"no spec for broker {broker.value!r} (onboard it first)")


def confirmed_values(confirmed_path: Path | str, broker: Broker,
                     field: str) -> dict[str, list[float]]:
    """{trade_ref: [confirmed values]} for one field/broker from the analyst
    file. A ref maps to a LIST so double-listed refs (e.g. BNP's MCB legs) all
    count as valid targets rather than clobbering each other."""
    get = _CONFIRMED_GETTERS[field]
    out: dict[str, list[float]] = defaultdict(list)
    for row in read_call_options(confirmed_path).rows:
        if row.broker == broker:
            v = get(row)
            if v is not None:
                out[row.trade_ref].append(float(v))
    return dict(out)


@dataclass
class ColumnMatch:
    col_index: int          # 0-based column in the statement
    header: str             # that column's header text
    match_rate: float       # best of exact / abs
    mode: str               # "exact" | "abs"
    matched: int
    compared: int           # refs present in both confirmed set and statement


def discover_field_source(
    statement_path: Path | str,
    spec: ParserSpec,
    confirmed_by_ref: dict[str, list[float]],
    *,
    tol_rel: float = 1e-4,
    max_cols: int = 80,
) -> list[ColumnMatch]:
    """Rank the statement's columns by how well they reproduce the confirmed
    field values, joined on trade ref. Best match first."""
    grid = load_grid(statement_path)
    header_row = _find_header_row(grid, spec.header_anchor)
    ref_col = _map_columns(grid, header_row, spec.columns).get("trade_ref")
    if ref_col is None:
        raise ValueError(f"spec for {spec.broker.value} has no locatable trade_ref column")

    # data rows whose ref is in the confirmed set
    rows = [(r, grid.text(r, ref_col)) for r in range(header_row + 1, grid.nrows)]
    rows = [(r, ref) for r, ref in rows if ref and ref in confirmed_by_ref]

    results: list[ColumnMatch] = []
    for c in range(min(grid.ncols, max_cols)):
        if c == ref_col:
            continue
        exact = abso = compared = 0
        for r, ref in rows:
            v = grid.num(r, c)
            if v is None:
                continue
            compared += 1
            targets = confirmed_by_ref[ref]
            if any(_close(v, t, tol_rel) for t in targets):
                exact += 1
            if any(_close(abs(v), abs(t), tol_rel) for t in targets):
                abso += 1
        if compared == 0:
            continue
        if abso >= exact:
            results.append(ColumnMatch(c, grid.text(header_row, c), abso / compared,
                                       "abs", abso, compared))
        else:
            results.append(ColumnMatch(c, grid.text(header_row, c), exact / compared,
                                       "exact", exact, compared))
    results.sort(key=lambda m: (m.match_rate, m.compared), reverse=True)
    return results


def discover_all(
    confirmed_path: Path | str,
    statement_path: Path | str,
    broker: Broker,
    *,
    tol_rel: float = 1e-4,
) -> dict[str, list[ColumnMatch]]:
    """Best-matching statement column(s) for every numeric field of a broker."""
    spec = spec_for_broker(broker)
    report: dict[str, list[ColumnMatch]] = {}
    for field in FIELDS:
        conf = confirmed_values(confirmed_path, broker, field)
        report[field] = discover_field_source(statement_path, spec, conf, tol_rel=tol_rel) if conf else []
    return report


# ---------------------------------------------------------------------------
# Cross-time regression: reconcile a spec's output against a PAST month's
# confirmed file, so a spec that worked in March can't silently drift in April.
# ---------------------------------------------------------------------------
@dataclass
class ReconReport:
    broker: str
    confirmed_refs: int
    parsed_refs: int
    common: int
    to_cent: int
    missing: list[str]     # confirmed refs we failed to parse
    overrides: list[str]   # common refs whose MTM differs (analyst overrides)

    @property
    def rate(self) -> float:
        return self.to_cent / self.common if self.common else 0.0


def reconcile_calls_to_confirmed(
    confirmed_path: Path | str,
    statement_path: Path | str,
    broker: Broker,
    *,
    tol: float = 0.01,
) -> ReconReport:
    """Reconcile a broker statement's CALLS against the confirmed Call Options
    sheet, per-reference (sums, so BNP's MCB net line and BAML's two legs
    reconcile uniformly). MTM carries the sign convention via build_rows."""
    calls, _puts = split_marks_by_option_type(route(statement_path).marks)
    rows = build_rows_from_marks(calls, {"USD": 1.0})
    ours: dict[str, float] = defaultdict(float)
    for r in rows:
        ours[r.trade_reference] += (r.mtm_local or 0)

    ref: dict[str, float] = defaultdict(float)
    for row in read_call_options(confirmed_path).rows:
        if row.broker == broker:
            ref[row.trade_ref] += (row.mtm_local or 0)

    common = set(ours) & set(ref)
    to_cent = sum(1 for k in common if abs(ours[k] - ref[k]) <= tol)
    overrides = sorted(k for k in common if abs(ours[k] - ref[k]) > tol)
    missing = sorted(set(ref) - set(ours))
    return ReconReport(broker.value, len(ref), len(ours), len(common),
                       to_cent, missing, overrides)


@dataclass
class BacktestMonth:
    as_of: str
    confirmed: Path
    statements: list[Path]


# March 2026 is the sample that ships in the repo root; future months are dropped
# into data/backtests/<as-of>/ (git-ignored) as they are confirmed.
_MARCH_2026 = (
    "ALIC MTM Prices for Call Options Test - 31 March 2026.xls",
    [
        "BNP AUGUSTAR LIFE INSURANCE COMPANY_01 Apr 2026 (2).xls",
        "BAML 03-31-2026_Results_82654.xls",
        "GS_Ohio National Life Ins Co (EQ)_20260331.xls",
        "HSBC GV_AUGUSTAR LIFE INSURANCE COMPANY REG15_31Mar2026_070407.xlsx",
    ],
)


def _find_confirmed(folder: Path) -> Path | None:
    """A month folder's confirmed file: the one whose name flags it as the
    analyst reference ('ALIC' or 'confirmed')."""
    for p in sorted(folder.glob("*.xls*")):
        low = p.name.lower()
        if "alic" in low or "confirmed" in low:
            return p
    return None


def discover_backtest_months(repo: Path | str = REPO_ROOT) -> list[BacktestMonth]:
    """Every past month we can backtest: the built-in March 2026 sample (repo
    root) plus any data/backtests/<as-of>/ folder holding a confirmed file and
    its broker statements. All git-ignored; absent months are simply skipped."""
    repo = Path(repo)
    months: list[BacktestMonth] = []

    confirmed_name, statement_names = _MARCH_2026
    root_confirmed = repo / confirmed_name
    if root_confirmed.exists():
        stmts = [repo / n for n in statement_names if (repo / n).exists()]
        if stmts:
            months.append(BacktestMonth("2026-03", root_confirmed, stmts))

    bt_dir = repo / "data" / "backtests"
    if bt_dir.is_dir():
        for d in sorted(bt_dir.iterdir()):
            if not d.is_dir():
                continue
            confirmed = _find_confirmed(d)
            if confirmed is None:
                continue
            stmts = [p for p in sorted(d.glob("*.xls*")) if p != confirmed
                     and not p.name.startswith("~$")]
            if stmts:
                months.append(BacktestMonth(d.name, confirmed, stmts))
    return months


def _broker_from_name(name: str) -> Broker:
    key = name.strip().casefold()
    for b in Broker:
        if b.value.casefold() == key or b.name.casefold() == key:
            return b
    raise SystemExit(f"unknown broker {name!r}; known: {', '.join(b.value for b in Broker)}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Discover which brokerage column produced a confirmed ALIC field")
    ap.add_argument("--confirmed", required=True, help="analyst-confirmed ALIC .xls for a PAST month")
    ap.add_argument("--statement", required=True, help="raw broker statement for the SAME month")
    ap.add_argument("--broker", required=True, help="broker name, e.g. Nomura")
    ap.add_argument("--field", choices=FIELDS, help="which field to trace (default: all)")
    ap.add_argument("--top", type=int, default=5, help="how many candidate columns to show")
    args = ap.parse_args(argv)

    broker = _broker_from_name(args.broker)
    fields = [args.field] if args.field else list(FIELDS)

    print(f"Backtest: which {args.statement} columns reproduce the confirmed "
          f"{broker.value} fields in {args.confirmed}?\n")
    spec = spec_for_broker(broker)
    for field in fields:
        conf = confirmed_values(args.confirmed, broker, field)
        print(f"== {field} ==")
        if not conf:
            print(f"  (no confirmed {field} values for {broker.value})\n")
            continue
        matches = discover_field_source(args.statement, spec, conf)
        if not matches:
            print("  (no comparable columns found)\n")
            continue
        for m in matches[: args.top]:
            star = " <-- likely source" if m.match_rate >= 0.98 else ""
            print(f"  col {m.col_index:>2}  {m.match_rate:6.1%} [{m.mode}]  "
                  f"({m.matched}/{m.compared})  '{m.header}'{star}")
        print()


if __name__ == "__main__":
    main()
