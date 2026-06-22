"""Generic spec-driven statement parser.

Runs a ParserSpec against a broker statement (.xls or .xlsx) and returns
BrokerMark records -- the same output contract as a hand-written parser. This
single, well-tested engine replaces per-broker parsing logic for any format
expressible as a spec.

The data-row heuristic is what makes one engine cover flat tables (BNP/GS/HSBC)
and deal-block layouts (BAML) alike: after the header, a row is a trade leg iff
it has a non-empty trade_ref AND a numeric mtm. Banner rows (account headers,
"Deal ..." lines) and repeated headers naturally fail that test.
"""

from __future__ import annotations

from pathlib import Path

from derivative_pricing.adaptive.reader import Grid, load_grid
from derivative_pricing.adaptive.spec import ColumnRule, ParserSpec
from derivative_pricing.schema import BrokerMark, Lineage


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _find_header_row(grid: Grid, anchor: str) -> int:
    a = _norm(anchor)
    for r in range(min(grid.nrows, 80)):
        for c in range(min(grid.ncols, 6)):
            if a in _norm(grid.text(r, c)):
                return r
    raise ValueError(f"header anchor {anchor!r} not found")


def _is_header_row(grid: Grid, r: int, anchor: str) -> bool:
    a = _norm(anchor)
    return any(a in _norm(grid.text(r, c)) for c in range(min(grid.ncols, 6)))


def _map_columns(grid: Grid, header_row: int, rules: list[ColumnRule]) -> dict[str, int]:
    """field -> column index for each rule's header_contains.

    Disambiguation when several headers contain the key (e.g. "MtM" also
    appears in "MtM Ccy", "MtM Less Accrued Interest"): prefer an EXACT header
    match, otherwise the SHORTEST containing header (closest match), leftmost
    on ties.
    """
    headers = {c: _norm(grid.text(header_row, c)) for c in range(grid.ncols)}
    col_of: dict[str, int] = {}
    for rule in rules:
        key = _norm(rule.header_contains)
        exact = [c for c, h in headers.items() if h == key]
        if exact:
            col_of[rule.field] = exact[0]
            continue
        candidates = sorted((len(h), c) for c, h in headers.items() if h and key in h)
        if candidates:
            col_of[rule.field] = candidates[0][1]
    return col_of


def _read_value(grid: Grid, r, c, kind):
    if kind == "number":
        return grid.num(r, c)
    if kind == "date":
        return grid.date_iso(r, c)
    return grid.text(r, c) or None


def _resolve_direction_col(grid: Grid, header_row, direction) -> int | None:
    if direction.mode != "column" or not direction.header_contains:
        return None
    key = _norm(direction.header_contains)
    for c in range(grid.ncols):
        if key in _norm(grid.text(header_row, c)):
            return c
    return None


def _direction_for(direction, grid: Grid, r, dir_col, extra) -> str:
    if direction.mode == "none":
        return "Long"
    if direction.mode == "sign_of" and direction.sign_of_field:
        v = extra.get(direction.sign_of_field)
        return "Short" if (v is not None and v < 0) else "Long"
    if direction.mode == "column" and dir_col is not None:
        val = grid.text(r, dir_col).casefold().strip()
        return "Short" if val in {v.casefold() for v in direction.short_values} else "Long"
    return "Long"


def run_spec(spec: ParserSpec, path: Path | str) -> list[BrokerMark]:
    spec.require()
    path = Path(path)
    grid = load_grid(path)

    header_row = _find_header_row(grid, spec.header_anchor)
    cols = _map_columns(grid, header_row, spec.columns)
    if "trade_ref" not in cols or "mtm_local" not in cols:
        raise ValueError(f"spec engine: trade_ref/mtm_local columns not located in {path.name}")

    kinds = {c.field: c.kind for c in spec.columns}
    ref_c, mtm_c = cols["trade_ref"], cols["mtm_local"]
    banners = [b.casefold() for b in spec.banner_prefixes]
    dir_col = _resolve_direction_col(grid, header_row, spec.direction)

    marks: list[BrokerMark] = []
    for r in range(header_row + 1, grid.nrows):
        if spec.repeated_headers and _is_header_row(grid, r, spec.header_anchor):
            continue
        ref = grid.text(r, ref_c)
        mtm = grid.num(r, mtm_c)
        if not ref or mtm is None:
            continue
        if any(ref.casefold().startswith(b) for b in banners):
            continue

        extra: dict = {}
        for field, c in cols.items():
            if field in ("trade_ref", "mtm_local"):
                continue
            extra[field] = _read_value(grid, r, c, kinds.get(field, "text"))

        extra["direction"] = _direction_for(spec.direction, grid, r, dir_col, extra)
        currency = extra.pop("currency", None)

        marks.append(
            BrokerMark(
                broker=spec.broker,
                trade_ref=ref,
                mtm_local=spec.mtm_sign * mtm,
                currency=currency,
                extra=extra,
                lineage=Lineage(source_file=path.name, sheet=grid.name, row=r + 1),
            )
        )
    return marks
