"""BNP Paribas month-end statement parser.

Statement layout (verified, April 2026 file, sheet "Option - Reg. MM"):
- A short preamble (client name, blurb), then a header row whose first cell
  is "BNPP's ref", then one row per option.
- The MTM the analyst consumes is column P, "Mark to Market (N*P)" -- this is
  the value that lands in the Marks sheet's BNP column (verified: OP38270MUE
  -> 8399.05 in both the statement and Marks).

Columns (by header text, matched case-insensitively so minor relabeling
between months still resolves):
    BNPP's ref            -> trade_ref
    Mark to Market (N*P)  -> mtm_local      (primary MTM)
    Currency              -> currency
    Trade Date            -> extra.trade_date
    Expiration date       -> extra.expiry
    Underlying            -> extra.underlying
    Bloom Code            -> extra.bloomberg_code
    Underlying price      -> extra.underlying_price
    Client Direction      -> extra.direction
    Call/ Put             -> extra.put_call
    Number of options (N) -> extra.quantity
    Strike                -> extra.strike
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import xlrd

from derivative_pricing.parsers.base import register_parser
from derivative_pricing.parsers.xls_compat import open_workbook
from derivative_pricing.schema import Broker, BrokerMark, Lineage

_SHEET = "Option - Reg. MM"
_HEADER_KEY = "bnpp's ref"

# header substring (lower) -> (canonical field, is_core)
_FIELD_MAP = {
    "bnpp's ref": ("trade_ref", True),
    "mark to market (n*p)": ("mtm_local", True),
    "currency": ("currency", True),
    "trade date": ("trade_date", False),
    "expiration date": ("expiry", False),
    "underlying": ("underlying", False),
    "bloom code": ("bloomberg_code", False),
    "underlying price": ("underlying_price", False),
    "client direction": ("direction", False),
    "call/ put": ("put_call", False),
    "number of options (n)": ("quantity", False),
    "strike": ("strike", False),
}


def _cell_text(sheet, r, c) -> str:
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return cell.value.strip()
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        v = cell.value
        return str(int(v)) if v == int(v) else str(v)
    return ""


def _find_header_row(sheet) -> int:
    for r in range(min(sheet.nrows, 60)):
        for c in range(min(sheet.ncols, 4)):
            if _cell_text(sheet, r, c).casefold() == _HEADER_KEY:
                return r
    raise ValueError(f"BNP header row ('{_HEADER_KEY}') not found in sheet {sheet.name!r}")


def _map_columns(sheet, header_row: int) -> dict[str, int]:
    """field name -> column index, by matching header text substrings.

    'underlying' is matched last/most-specific so it doesn't shadow
    'underlying price'.
    """
    col_of: dict[str, int] = {}
    for c in range(sheet.ncols):
        # Headers contain embedded newlines (e.g. "Mark to Market\n(N*P) *");
        # collapse all whitespace so substring matching is reliable.
        h = " ".join(_cell_text(sheet, header_row, c).split()).casefold()
        if not h:
            continue
        # most specific (longest header key) wins for this cell
        best = None
        for key, (field, _core) in _FIELD_MAP.items():
            if key in h and (best is None or len(key) > len(best[0])):
                best = (key, field)
        if best:
            col_of.setdefault(best[1], c)
    return col_of


def _to_date(sheet, r, c, datemode):
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return dt.datetime(*xlrd.xldate_as_tuple(cell.value, datemode)).date().isoformat()
    return _cell_text(sheet, r, c) or None


def _to_num(sheet, r, c):
    cell = sheet.cell(r, c)
    return float(cell.value) if cell.ctype == xlrd.XL_CELL_NUMBER else None


@register_parser(Broker.BNP_PARIBAS)
class BnpParser:
    broker = Broker.BNP_PARIBAS

    def parse(self, path: Path | str) -> list[BrokerMark]:
        path = Path(path)
        book = open_workbook(path, formatting_info=False)
        sheet = book.sheet_by_name(_SHEET) if _SHEET in book.sheet_names() else book.sheet_by_index(0)
        header_row = _find_header_row(sheet)
        cols = _map_columns(sheet, header_row)

        if "trade_ref" not in cols or "mtm_local" not in cols:
            missing = {"trade_ref", "mtm_local"} - set(cols)
            raise ValueError(f"BNP parser: missing required columns {missing} in {path.name}")

        marks: list[BrokerMark] = []
        for r in range(header_row + 1, sheet.nrows):
            ref = _cell_text(sheet, r, cols["trade_ref"])
            mtm = _to_num(sheet, r, cols["mtm_local"])
            if not ref or mtm is None:
                continue  # skip blank/subtotal/footer rows

            extra: dict = {}
            if "trade_date" in cols:
                extra["trade_date"] = _to_date(sheet, r, cols["trade_date"], book.datemode)
            if "expiry" in cols:
                extra["expiry"] = _to_date(sheet, r, cols["expiry"], book.datemode)
            for f in ("underlying", "bloomberg_code", "direction", "put_call"):
                if f in cols:
                    extra[f] = _cell_text(sheet, r, cols[f]) or None
            for f in ("underlying_price", "quantity", "strike"):
                if f in cols:
                    extra[f] = _to_num(sheet, r, cols[f])

            marks.append(
                BrokerMark(
                    broker=Broker.BNP_PARIBAS,
                    trade_ref=ref,
                    mtm_local=mtm,
                    currency=_cell_text(sheet, r, cols["currency"]) or None if "currency" in cols else None,
                    extra=extra,
                    lineage=Lineage(source_file=path.name, sheet=sheet.name, row=r + 1),
                )
            )
        return marks