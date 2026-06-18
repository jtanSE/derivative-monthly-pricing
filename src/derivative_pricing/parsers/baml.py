"""BAML (Bank of America / Merrill Lynch) month-end statement parser.

Statement layout (verified, March 2026 file, sheet "ValAppReport_Default"):
- A preamble (TO / COMPANY / RE / DATE), then a "Value Date" row.
- The body is a sequence of DEAL BLOCKS. Each block is:
      "Deal <ref>  Net Value: <net>"          (block banner)
      a header row whose first data cell is "ML Ref."
      one or more LEG rows (typically two: a long N>0 and a short N<0)
- The MTM the analyst consumes is column Q, "Estimated mid-market value"
  (matches the Marks sheet's BAML column; e.g. ML Ref 2582240555 long leg
  -> 1,422,758).

Because a double-legged deal shares one ML Ref across two legs, this parser
emits one BrokerMark per LEG (same trade_ref, different position/MTM). The
matching engine treats repeated refs as ambiguous and routes them to the
double-legged pairing logic (plan Q5) rather than silently picking one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import xlrd

from derivative_pricing.parsers.base import register_parser
from derivative_pricing.parsers.xls_compat import open_workbook
from derivative_pricing.schema import Broker, BrokerMark, Lineage

_SHEET = "ValAppReport_Default"
_HEADER_KEY = "ml ref."

# header substring (lower) -> canonical field
_FIELD_MAP = {
    "ml ref.": "trade_ref",
    "estimated mid-market value": "mtm_local",
    "estimated mid-market price": "mid_price",
    "sett. cur.": "currency",
    "instrument type": "instrument_type",
    "trade date": "trade_date",
    "maturity date": "expiry",
    "instrument name": "instrument_name",
    "underlying name": "underlying",
    "underlying reference price": "underlying_price",
    "call/put": "put_call",
    "strike": "strike",
    "position": "quantity",
    "multiplier": "multiplier",
}


def _cell_text(sheet, r, c) -> str:
    if c >= sheet.ncols:
        return ""
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return cell.value.strip()
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        v = cell.value
        return str(int(v)) if v == int(v) else str(v)
    return ""


def _to_num(sheet, r, c):
    if c >= sheet.ncols:
        return None
    cell = sheet.cell(r, c)
    return float(cell.value) if cell.ctype == xlrd.XL_CELL_NUMBER else None


def _to_date(sheet, r, c, datemode):
    if c >= sheet.ncols:
        return None
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return dt.datetime(*xlrd.xldate_as_tuple(cell.value, datemode)).date().isoformat()
    return _cell_text(sheet, r, c) or None


def _is_header_row(sheet, r) -> bool:
    for c in range(min(sheet.ncols, 4)):
        if _cell_text(sheet, r, c).casefold() == _HEADER_KEY:
            return True
    return False


def _map_columns(sheet, header_row: int) -> dict[str, int]:
    col_of: dict[str, int] = {}
    for c in range(sheet.ncols):
        h = " ".join(_cell_text(sheet, header_row, c).split()).casefold()
        if not h:
            continue
        best = None
        for key, field in _FIELD_MAP.items():
            if key in h and (best is None or len(key) > len(best[0])):
                best = (key, field)
        if best:
            col_of.setdefault(best[1], c)
    return col_of


@register_parser(Broker.BAML)
class BamlParser:
    broker = Broker.BAML

    def parse(self, path: Path | str) -> list[BrokerMark]:
        path = Path(path)
        book = open_workbook(path, formatting_info=False)
        sheet = book.sheet_by_name(_SHEET) if _SHEET in book.sheet_names() else book.sheet_by_index(0)

        # Column positions are stable across the repeated headers; map once.
        first_header = next((r for r in range(sheet.nrows) if _is_header_row(sheet, r)), None)
        if first_header is None:
            raise ValueError(f"BAML parser: no '{_HEADER_KEY}' header row in {path.name}")
        cols = _map_columns(sheet, first_header)
        if "trade_ref" not in cols or "mtm_local" not in cols:
            missing = {"trade_ref", "mtm_local"} - set(cols)
            raise ValueError(f"BAML parser: missing required columns {missing} in {path.name}")

        ref_col, mtm_col = cols["trade_ref"], cols["mtm_local"]
        marks: list[BrokerMark] = []
        for r in range(first_header + 1, sheet.nrows):
            if _is_header_row(sheet, r):
                continue  # repeated block header
            ref = _cell_text(sheet, r, ref_col)
            mtm = _to_num(sheet, r, mtm_col)
            # leg rows have a numeric ML Ref and a numeric MTM; "Deal ..."
            # banners and blanks do not.
            if not ref or mtm is None or ref.lower().startswith("deal"):
                continue

            extra: dict = {}
            for f in ("trade_date", "expiry"):
                if f in cols:
                    extra[f] = _to_date(sheet, r, cols[f], book.datemode)
            for f in ("instrument_type", "instrument_name", "underlying", "put_call"):
                if f in cols:
                    extra[f] = _cell_text(sheet, r, cols[f]) or None
            for f in ("underlying_price", "quantity", "strike", "multiplier", "mid_price"):
                if f in cols:
                    extra[f] = _to_num(sheet, r, cols[f])
            # BAML uses signed Position directly (long N>0, short N<0); expose
            # a normalized direction so the output layer's sign logic still works.
            qty = extra.get("quantity")
            extra["direction"] = "Short" if (qty is not None and qty < 0) else "Long"

            marks.append(
                BrokerMark(
                    broker=Broker.BAML,
                    trade_ref=ref,
                    mtm_local=mtm,
                    currency=_cell_text(sheet, r, cols["currency"]) or None if "currency" in cols else None,
                    extra=extra,
                    lineage=Lineage(source_file=path.name, sheet=sheet.name, row=r + 1),
                )
            )
        return marks
