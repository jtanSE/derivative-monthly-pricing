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


def _find_header_row(grid: Grid, anchor: str, requires: list[str] | None = None) -> int:
    """Row index of the header.

    The anchor is matched in the first few columns (headers live at the left).
    When ``requires`` is given, anchor-matching rows are skipped until one is
    found whose cells -- scanned across ALL columns -- contain every required
    substring; this disambiguates statements that stack several tables under the
    same anchor (see ParserSpec.header_requires)."""
    reqs = [_norm(x) for x in (requires or [])]
    for r in range(min(grid.nrows, 80)):
        if not any(_norm(anchor) in _norm(grid.text(r, c)) for c in range(min(grid.ncols, 6))):
            continue
        if reqs:
            row_text = [_norm(grid.text(r, c)) for c in range(grid.ncols)]
            if not all(any(req in cell for cell in row_text) for req in reqs):
                continue
        return r
    if reqs:
        raise ValueError(f"header anchor {anchor!r} with requires {requires!r} not found")
    raise ValueError(f"header anchor {anchor!r} not found")


def _is_header_row(grid: Grid, r: int, anchor: str) -> bool:
    a = _norm(anchor)
    return any(a in _norm(grid.text(r, c)) for c in range(min(grid.ncols, 6)))


def _resolve_col(headers: dict[int, str], header_contains: str) -> int | None:
    """Column whose header matches header_contains: prefer an EXACT match, else
    the SHORTEST containing header (closest match), leftmost on ties."""
    key = _norm(header_contains)
    exact = [c for c, h in headers.items() if h == key]
    if exact:
        return exact[0]
    candidates = sorted((len(h), c) for c, h in headers.items() if h and key in h)
    return candidates[0][1] if candidates else None


def _map_columns(grid: Grid, header_row: int, rules: list[ColumnRule]) -> dict[str, int]:
    """field -> column index for each rule's header_contains (see _resolve_col)."""
    headers = {c: _norm(grid.text(header_row, c)) for c in range(grid.ncols)}
    col_of: dict[str, int] = {}
    for rule in rules:
        c = _resolve_col(headers, rule.header_contains)
        if c is not None:
            col_of[rule.field] = c
    return col_of


def _map_fallback_columns(grid: Grid, header_row: int,
                          rules: list[ColumnRule]) -> dict[str, int]:
    """field -> fallback column index, for rules that declare one."""
    headers = {c: _norm(grid.text(header_row, c)) for c in range(grid.ncols)}
    out: dict[str, int] = {}
    for rule in rules:
        if rule.fallback_header_contains:
            c = _resolve_col(headers, rule.fallback_header_contains)
            if c is not None:
                out[rule.field] = c
    return out


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


def _find_all_header_rows(grid: Grid, anchor: str, requires: list[str] | None = None) -> list[int]:
    """Every row matching the anchor (and, if given, containing all required
    substrings) -- one per stacked table sharing the anchor. Scans the whole
    sheet (unlike the single-table finder), since later tables can sit deep."""
    reqs = [_norm(x) for x in (requires or [])]
    out: list[int] = []
    for r in range(grid.nrows):
        if not any(_norm(anchor) in _norm(grid.text(r, c)) for c in range(min(grid.ncols, 6))):
            continue
        if reqs:
            row_text = [_norm(grid.text(r, c)) for c in range(grid.ncols)]
            if not all(any(req in cell for cell in row_text) for req in reqs):
                continue
        out.append(r)
    return out


def run_spec(spec: ParserSpec, path: Path | str) -> list[BrokerMark]:
    spec.require()
    path = Path(path)
    grid = load_grid(path)

    # A statement may stack several tables under one anchor (Barclays: Equity
    # Options + Other Equity Derivatives + a bond-forward table). With
    # multi_table, parse every table whose columns include trade_ref + mtm_local
    # (which drops the bond-forward table -- it has no MTM(Ccy) column); columns
    # are mapped PER TABLE, so a table lacking Quantity simply omits it.
    segments: list[tuple[int, int]] = []  # (header_row, end_row)
    seg_cols: list[dict[str, int]] = []
    seg_fbs: list[dict[str, int]] = []
    if spec.multi_table:
        headers = _find_all_header_rows(grid, spec.header_anchor, spec.header_requires)
        if not headers:
            raise ValueError(f"spec engine: no header row for anchor "
                             f"{spec.header_anchor!r} in {path.name}")
        for i, hr in enumerate(headers):
            end = headers[i + 1] if i + 1 < len(headers) else grid.nrows
            cols = _map_columns(grid, hr, spec.columns)
            if "trade_ref" in cols and "mtm_local" in cols:
                segments.append((hr, end))
                seg_cols.append(cols)
                seg_fbs.append(_map_fallback_columns(grid, hr, spec.columns))
        if not segments:
            raise ValueError(f"spec engine: no table with trade_ref+mtm_local in {path.name}")
    else:
        header_row = _find_header_row(grid, spec.header_anchor, spec.header_requires)
        cols = _map_columns(grid, header_row, spec.columns)
        if "trade_ref" not in cols or "mtm_local" not in cols:
            raise ValueError(f"spec engine: trade_ref/mtm_local columns not located in {path.name}")
        segments.append((header_row, grid.nrows))
        seg_cols.append(cols)
        seg_fbs.append(_map_fallback_columns(grid, header_row, spec.columns))

    marks: list[BrokerMark] = []
    for (header_row, end_row), cols, fbs in zip(segments, seg_cols, seg_fbs):
        marks.extend(_parse_table(spec, grid, path, header_row, end_row, cols, fbs))
    return marks


def _parse_table(spec: ParserSpec, grid: Grid, path: Path, header_row: int,
                 end_row: int, cols: dict[str, int],
                 fallback_cols: dict[str, int] | None = None) -> list[BrokerMark]:
    """Parse one table's data rows [header_row+1, end_row)."""
    kinds = {c.field: c.kind for c in spec.columns}
    fallback_cols = fallback_cols or {}
    # fields whose fallback value is already sign-carrying (Notional): the sign
    # of the fallback value IS the position sign, so the direction column is
    # bypassed for that row (see below).
    signed_fb = {c.field for c in spec.columns if c.fallback_is_signed}
    ref_c, mtm_c = cols["trade_ref"], cols["mtm_local"]
    banners = [b.casefold() for b in spec.banner_prefixes]
    dir_col = _resolve_direction_col(grid, header_row, spec.direction)

    qty_c = cols.get("quantity")

    def read_field(r: int, field: str, c: int | None) -> tuple[object, bool]:
        """Value for one field at row r, falling back to a secondary column when
        the primary is empty OR the primary column is absent from this table
        (Barclays' .BXIIGTBB table has a Notional column but no Quantity column
        at all). Returns (value, used_fallback)."""
        v = _read_value(grid, r, c, kinds.get(field, "text")) if c is not None else None
        if v is None and field in fallback_cols:
            fv = _read_value(grid, r, fallback_cols[field], kinds.get(field, "text"))
            if fv is not None:
                return fv, True
        return v, False

    # Optional row-exclusion filter (drop non-option products, e.g. Nomura's
    # ForwardBondTrade rows mixed into the same sheet as options).
    filt_col: int | None = None
    filt_excl: set[str] = set()
    if spec.row_filter is not None:
        key = _norm(spec.row_filter.header_contains)
        filt_col = next((c for c in range(grid.ncols)
                         if key in _norm(grid.text(header_row, c))), None)
        filt_excl = {v.strip().casefold() for v in spec.row_filter.exclude_values}

    marks: list[BrokerMark] = []
    # Split-leg bookkeeping: the most recent net-style parent (has ref+mtm but
    # no quantity) that ref-less leg rows may attach to, and whether it is
    # still sitting in `marks` awaiting replacement by its first leg.
    parent: BrokerMark | None = None
    parent_in_marks = False

    for r in range(header_row + 1, end_row):
        if spec.repeated_headers and _is_header_row(grid, r, spec.header_anchor):
            continue
        ref = grid.text(r, ref_c)
        mtm = grid.num(r, mtm_c)

        # Excluded product (e.g. a bond forward in an options sheet): drop it
        # before it can be summed as either a leg or a standalone trade.
        if filt_col is not None and grid.text(r, filt_col).strip().casefold() in filt_excl:
            continue

        if not ref:
            # Possibly a split-leg continuation row: no ref, but a real
            # quantity + mtm right below a net-style parent.
            if (
                spec.split_legs
                and parent is not None
                and mtm is not None
                and qty_c is not None
                and grid.num(r, qty_c) is not None
            ):
                if parent_in_marks:
                    marks.remove(parent)  # the net row is replaced by its legs
                    parent_in_marks = False
                leg_extra = {k: v for k, v in parent.extra.items() if k != "direction"}
                for field, c in cols.items():
                    if field in ("trade_ref", "mtm_local"):
                        continue
                    v = _read_value(grid, r, c, kinds.get(field, "text"))
                    if v is not None:
                        leg_extra[field] = v
                leg_extra["direction"] = _direction_for(spec.direction, grid, r, dir_col, leg_extra)
                currency = leg_extra.pop("currency", None) or parent.currency
                marks.append(
                    BrokerMark(
                        broker=spec.broker,
                        trade_ref=parent.trade_ref,
                        mtm_local=mtm,
                        currency=currency,
                        extra=leg_extra,
                        lineage=Lineage(source_file=path.name, sheet=grid.name, row=r + 1),
                    )
                )
            continue

        if mtm is None:
            continue
        if any(ref.casefold().startswith(b) for b in banners):
            continue

        extra: dict = {}
        signed_from_fallback: dict[str, float] = {}
        # union of mapped fields and fallback-only fields, so a field whose
        # PRIMARY column is missing from this table still resolves via fallback
        for field in dict.fromkeys([*cols, *fallback_cols]):
            if field in ("trade_ref", "mtm_local"):
                continue
            val, used_fb = read_field(r, field, cols.get(field))
            extra[field] = val
            if used_fb and field in signed_fb and isinstance(val, (int, float)):
                signed_from_fallback[field] = val

        extra["direction"] = _direction_for(spec.direction, grid, r, dir_col, extra)
        # A field taken from a signed fallback column (Barclays .BXIIGTBB share
        # count under Notional) carries its own sign; reconstruct direction from
        # that sign so downstream signing reproduces it, ignoring the (blank)
        # direction column for these rows.
        if "quantity" in signed_from_fallback:
            extra["direction"] = "Short" if signed_from_fallback["quantity"] < 0 else "Long"
        currency = extra.pop("currency", None)

        mark = BrokerMark(
            broker=spec.broker,
            trade_ref=ref,
            # AS-REPORTED per the BrokerMark contract; sign conventions are
            # applied downstream via parsers.base.BROKER_MTM_SIGN.
            mtm_local=mtm,
            currency=currency,
            extra=extra,
            lineage=Lineage(source_file=path.name, sheet=grid.name, row=r + 1),
        )
        marks.append(mark)
        # only a quantity-less row can be a split parent awaiting legs
        parent = mark if (spec.split_legs and extra.get("quantity") is None) else None
        parent_in_marks = parent is not None
    return marks
