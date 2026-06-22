"""Generic spec-driven statement parser.

Runs a ParserSpec against a broker .xls and returns BrokerMark records --
the same output contract as a hand-written parser. This single, well-tested
engine replaces per-broker parsing logic for any format expressible as a spec.

The data-row heuristic is what makes one engine cover flat tables (BNP/GS) and
deal-block layouts (BAML) alike: after the header, a row is a trade leg iff it
has a non-empty trade_ref AND a numeric mtm. Banner rows (account headers,
"Deal ..." lines) and repeated headers naturally fail that test.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import xlrd

from derivative_pricing.adaptive.spec import ColumnRule, ParserSpec
from derivative_pricing.parsers.xls_compat import open_workbook
from derivative_pricing.schema import BrokerMark, Lineage


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _cell_text(sheet, r, c) -> str:
    if c is None or c >= sheet.ncols:
        return ""
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return cell.value.strip()
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        v = cell.value
        return str(int(v)) if v == int(v) else str(v)
    return ""


def _cell_num(sheet, r, c):
    if c is None or c >= sheet.ncols:
        return None
    cell = sheet.cell(r, c)
    return float(cell.value) if cell.ctype == xlrd.XL_CELL_NUMBER else None


def _cell_date(sheet, r, c, datemode):
    if c is None or c >= sheet.ncols:
        return None
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return dt.datetime(*xlrd.xldate_as_tuple(cell.value, datemode)).date().isoformat()
    return _cell_text(sheet, r, c) or None


def _find_header_row(sheet, anchor: str) -> int:
    a = _norm(anchor)
    for r in range(min(sheet.nrows, 80)):
        for c in range(min(sheet.ncols, 6)):
            if _norm(_cell_text(sheet, r, c)) == a or a in _norm(_cell_text(sheet, r, c)):
                return r
    raise ValueError(f"header anchor {anchor!r} not found")


def _is_header_row(sheet, r, anchor: str) -> bool:
    a = _norm(anchor)
    for c in range(min(sheet.ncols, 6)):
        if a in _norm(_cell_text(sheet, r, c)):
            return True
    return False


def _map_columns(sheet, header_row: int, rules: list[ColumnRule]) -> dict[str, int]:
    """field -> column index, matching each rule's header_contains substring.
    Most-specific (longest matching substring) wins per cell."""
    headers = {c: _norm(_cell_text(sheet, header_row, c)) for c in range(sheet.ncols)}
    col_of: dict[str, int] = {}
    for rule in rules:
        key = _norm(rule.header_contains)
        best_c, best_len = None, -1
        for c, h in headers.items():
            if h and key in h and len(key) > best_len:
                best_c, best_len = c, len(key)
        if best_c is not None:
            col_of[rule.field] = best_c
    return col_of


def _read_value(sheet, r, c, kind, datemode):
    if kind == "number":
        return _cell_num(sheet, r, c)
    if kind == "date":
        return _cell_date(sheet, r, c, datemode)
    return _cell_text(sheet, r, c) or None


def _resolve_direction_col(sheet, header_row, direction) -> int | None:
    if direction.mode != "column" or not direction.header_contains:
        return None
    key = _norm(direction.header_contains)
    for c in range(sheet.ncols):
        if key in _norm(_cell_text(sheet, header_row, c)):
            return c
    return None


def _direction_for(direction, sheet, r, dir_col, extra) -> str:
    if direction.mode == "none":
        return "Long"
    if direction.mode == "sign_of" and direction.sign_of_field:
        v = extra.get(direction.sign_of_field)
        return "Short" if (v is not None and v < 0) else "Long"
    if direction.mode == "column" and dir_col is not None:
        val = _cell_text(sheet, r, dir_col).casefold().strip()
        return "Short" if val in {v.casefold() for v in direction.short_values} else "Long"
    return "Long"


def run_spec(spec: ParserSpec, path: Path | str) -> list[BrokerMark]:
    spec.require()
    path = Path(path)
    book = open_workbook(path, formatting_info=False)
    if spec.sheet_name and spec.sheet_name in book.sheet_names():
        sheet = book.sheet_by_name(spec.sheet_name)
    else:
        sheet = book.sheet_by_index(0)

    header_row = _find_header_row(sheet, spec.header_anchor)
    cols = _map_columns(sheet, header_row, spec.columns)
    if "trade_ref" not in cols or "mtm_local" not in cols:
        raise ValueError(f"spec engine: trade_ref/mtm_local columns not located in {path.name}")

    kinds = {c.field: c.kind for c in spec.columns}
    ref_c, mtm_c = cols["trade_ref"], cols["mtm_local"]
    banners = [b.casefold() for b in spec.banner_prefixes]
    dir_col = _resolve_direction_col(sheet, header_row, spec.direction)

    marks: list[BrokerMark] = []
    for r in range(header_row + 1, sheet.nrows):
        if spec.repeated_headers and _is_header_row(sheet, r, spec.header_anchor):
            continue
        ref = _cell_text(sheet, r, ref_c)
        mtm = _cell_num(sheet, r, mtm_c)
        if not ref or mtm is None:
            continue
        if any(ref.casefold().startswith(b) for b in banners):
            continue

        extra: dict = {}
        for field, c in cols.items():
            if field in ("trade_ref", "mtm_local"):
                continue
            extra[field] = _read_value(sheet, r, c, kinds.get(field, "text"), book.datemode)

        extra["direction"] = _direction_for(spec.direction, sheet, r, dir_col, extra)
        currency = extra.pop("currency", None)

        marks.append(
            BrokerMark(
                broker=spec.broker,
                trade_ref=ref,
                mtm_local=spec.mtm_sign * mtm,
                currency=currency,
                extra=extra,
                lineage=Lineage(source_file=path.name, sheet=sheet.name, row=r + 1),
            )
        )
    return marks
