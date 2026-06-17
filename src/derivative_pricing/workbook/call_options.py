"""Reader for the analyst's ``Call Options`` sheet.

Purpose: the parallel-run acceptance gate (plan section 7). We read what the
analyst's workbook produced -- trade rows plus its own FX and index-price
lookup tables -- so tests can recompute every derived column with the pricing
engine and prove the implementations agree.

Layout (verified, plan section 1.2): ten broker sections stacked vertically,
each: banner row -> title row -> header row ("Cusip" in col A) -> trade rows
-> subtotal row. Reference tables on the right: T/U = currency -> FX rate,
V/W/X = ticker / index name / month-end price (VLOOKUP key is the NAME, col W).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import xlrd

from derivative_pricing.schema import Broker

BANNER_TO_BROKER: dict[str, Broker] = {
    "Barclays Custom": Broker.BARCLAYS,
    "Morgan Stanley": Broker.MORGAN_STANLEY,
    "BNP Paribas": Broker.BNP_PARIBAS,
    "BAML": Broker.BAML,
    "Goldman Sachs": Broker.GOLDMAN_SACHS,
    "Wells Fargo": Broker.WELLS_FARGO,
    "Nomura": Broker.NOMURA,
    "JP Morgan": Broker.JP_MORGAN,
    "Credit Suisse": Broker.CREDIT_SUISSE,
    "HSBC": Broker.HSBC,
}

# 0-based column positions, verified from the header row.
_COL = {
    "cusip": 0, "index": 1, "trade_ref": 2, "strike": 3, "maturity": 4,
    "currency": 5, "notional_local": 6, "notional_usd": 7, "mtm_local": 8,
    "mtm_usd": 9, "shares": 10, "price_local": 11, "raw_broker_mtm": 12,
    "fx_ccy": 19, "fx_rate": 20, "idx_ticker": 21, "idx_name": 22, "idx_price": 23,
}


@dataclass
class CallOptionRow:
    broker: Broker
    excel_row: int  # 1-based
    cusip: str
    index_name: str
    trade_ref: str
    strike: float | None
    maturity: dt.date | None
    currency: str
    notional_local: float | None
    notional_usd: float | None
    mtm_local: float | None
    mtm_usd: float | None
    shares: float | None
    price_local: float | None
    raw_broker_mtm: float | None


@dataclass
class CallOptionsWorkbook:
    as_of: dt.date | None
    rows: list[CallOptionRow] = field(default_factory=list)
    fx_rates: dict[str, float] = field(default_factory=dict)
    # VLOOKUP semantics: key = index NAME as written, first match wins
    index_prices: dict[str, float] = field(default_factory=dict)
    index_tickers: dict[str, str] = field(default_factory=dict)  # name -> ticker


def _num(sheet, row, col) -> float | None:
    cell = sheet.cell(row, col)
    return float(cell.value) if cell.ctype == xlrd.XL_CELL_NUMBER else None


def _text(sheet, row, col) -> str:
    cell = sheet.cell(row, col)
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return cell.value.strip()
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        v = cell.value
        return str(int(v)) if v == int(v) else str(v)
    return ""


def _date(sheet, row, col, datemode) -> dt.date | None:
    cell = sheet.cell(row, col)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return dt.datetime(*xlrd.xldate_as_tuple(cell.value, datemode)).date()
    return None


def read_call_options(path: Path | str, sheet_name: str = "Call Options") -> CallOptionsWorkbook:
    path = Path(path)
    book = xlrd.open_workbook(str(path), on_demand=True)
    sheet = book.sheet_by_name(sheet_name)
    wb = CallOptionsWorkbook(as_of=_date(sheet, 4, 0, book.datemode))  # A5

    # Reference tables (right side, from row 8 / 0-based 7 downward).
    for r in range(7, sheet.nrows):
        ccy = _text(sheet, r, _COL["fx_ccy"])
        rate = _num(sheet, r, _COL["fx_rate"])
        if ccy and rate is not None and ccy not in wb.fx_rates:
            wb.fx_rates[ccy] = rate
        name = _text(sheet, r, _COL["idx_name"])
        price = _num(sheet, r, _COL["idx_price"])
        if name and price is not None and name not in wb.index_prices:
            wb.index_prices[name] = price
            ticker = _text(sheet, r, _COL["idx_ticker"])
            if ticker:
                wb.index_tickers[name] = ticker

    # Trade rows, walked section by section.
    current_broker: Broker | None = None
    for r in range(sheet.nrows):
        a = _text(sheet, r, 0)
        if a in BANNER_TO_BROKER:
            current_broker = BANNER_TO_BROKER[a]
            continue
        if current_broker is None or a in ("", "Cusip"):
            continue
        trade_ref = _text(sheet, r, _COL["trade_ref"])
        if not trade_ref:
            continue  # subtotal/blank rows have no trade reference
        wb.rows.append(
            CallOptionRow(
                broker=current_broker,
                excel_row=r + 1,
                cusip=a,
                index_name=_text(sheet, r, _COL["index"]),
                trade_ref=trade_ref,
                strike=_num(sheet, r, _COL["strike"]),
                maturity=_date(sheet, r, _COL["maturity"], book.datemode),
                currency=_text(sheet, r, _COL["currency"]),
                notional_local=_num(sheet, r, _COL["notional_local"]),
                notional_usd=_num(sheet, r, _COL["notional_usd"]),
                mtm_local=_num(sheet, r, _COL["mtm_local"]),
                mtm_usd=_num(sheet, r, _COL["mtm_usd"]),
                shares=_num(sheet, r, _COL["shares"]),
                price_local=_num(sheet, r, _COL["price_local"]),
                raw_broker_mtm=_num(sheet, r, _COL["raw_broker_mtm"]),
            )
        )
    return wb