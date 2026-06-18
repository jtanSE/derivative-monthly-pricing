"""Build an ALIC-style "Call Options" sheet from normalized broker marks.

This is the system's analogue of the analyst's `Call Options` sheet: the same
trade-level columns, with the derived columns (Notional, USD conversions,
per-unit price) computed deterministically by the pricing engine (rules
F1, F2/F4, F5) instead of by spreadsheet formulas.

Market data (month-end index price + FX) normally comes from the analyst's
Bloomberg entry. When a broker statement already carries the month-end
underlying price (BNP column H "Underlying price"), it can be used directly,
which is what the demo runner does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from derivative_pricing.parsers.base import apply_sign_convention
from derivative_pricing.pricing import notional_local, price_per_unit, to_usd
from derivative_pricing.reference.indices import canonicalize_index
from derivative_pricing.schema import BrokerMark

# Mirrors the analyst's Call Options header row (cols A-L).
COLUMNS = [
    "Cusip", "Index", "Trade Reference", "Strike Rate", "Maturity Date",
    "Currency", "Notional (Ccy)", "Notional (USD)", "MTM (Ccy)", "MTM (USD)",
    "Shares", "Price (Ccy)",
]


@dataclass
class CallOptionsOutputRow:
    cusip: str | None
    index: str | None
    trade_reference: str
    strike_rate: float | None
    maturity_date: str | None
    currency: str | None
    notional_local: float | None
    notional_usd: float | None
    mtm_local: float | None
    mtm_usd: float | None
    shares: float | None
    price_local: float | None

    def as_cells(self) -> list:
        return [
            self.cusip, self.index, self.trade_reference, self.strike_rate,
            self.maturity_date, self.currency, self.notional_local,
            self.notional_usd, self.mtm_local, self.mtm_usd, self.shares,
            self.price_local,
        ]


def _signed_shares(quantity: float | None, direction: str | None) -> float | None:
    """Signed position size: negative for short, positive otherwise.

    Statements report quantity (N) as a positive magnitude plus a direction;
    ALIC carries the sign on the shares themselves.
    """
    if quantity is None:
        return None
    if direction and direction.strip().lower() == "short":
        return -abs(quantity)
    return quantity


def build_rows_from_marks(
    marks: list[BrokerMark],
    fx_rates: dict[str, float],
    index_prices: dict[str, float] | None = None,
) -> list[CallOptionsOutputRow]:
    """Map broker marks -> ALIC-style rows, computing derived columns.

    ``index_prices`` (canonical index name -> month-end price) is optional: if
    a mark carries its own ``extra['underlying_price']`` it is used as the
    month-end index price, mirroring how the analyst keys the same number in.
    FX defaults to 1.0 for any currency not supplied (all USD in the BNP file).
    """
    index_prices = index_prices or {}
    rows: list[CallOptionsOutputRow] = []
    for m in marks:
        currency = (m.currency or "USD").upper()
        fx = fx_rates.get(currency, 1.0 if currency == "USD" else None)

        # Apply the Long/Short sign to shares: the analyst's sheet stores short
        # positions as negative shares (verified 251/253 vs ALIC). This flows
        # through to notional and per-unit price, which both go negative for
        # shorts. MTM keeps the sign the statement reports. The traders DB
        # LongShort field is the authoritative source once matching is wired;
        # here we use the statement's own Client Direction.
        shares = _signed_shares(m.extra.get("quantity"), m.extra.get("direction"))
        raw_index = m.extra.get("underlying")
        index_name = canonicalize_index(raw_index) if raw_index else None
        index_price = index_prices.get(index_name) if index_name else None
        if index_price is None:
            index_price = m.extra.get("underlying_price")

        notl = notional_local(shares, index_price) if (shares is not None and index_price is not None) else None
        notl_usd = to_usd(notl, fx) if (notl is not None and fx is not None) else None

        # MTM from the statement, converted to internal sign convention (F3).
        mtm = apply_sign_convention(m.broker, m.mtm_local)
        mtm_usd = to_usd(mtm, fx) if fx is not None else None
        price = price_per_unit(mtm, shares) if shares else None

        rows.append(
            CallOptionsOutputRow(
                cusip=None,  # CUSIP comes from the traders DB join (not yet wired)
                index=index_name,
                trade_reference=m.trade_ref,
                strike_rate=m.extra.get("strike"),
                maturity_date=m.extra.get("expiry"),
                currency=currency,
                notional_local=notl,
                notional_usd=notl_usd,
                mtm_local=mtm,
                mtm_usd=mtm_usd,
                shares=shares,
                price_local=price,
            )
        )
    return rows


def write_call_options_xlsx(
    rows: list[CallOptionsOutputRow],
    path: Path | str,
    *,
    as_of: str,
    broker_label: str,
) -> Path:
    """Write rows to an .xlsx mirroring the analyst's Call Options layout."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Call Options"

    ws["A1"] = f"{broker_label} (system-generated)"
    ws["A1"].font = Font(bold=True)
    ws["A2"] = "Equity Derivatives - ALIC Call Options"
    ws["A3"] = as_of
    ws["J1"] = "Prepared by:"
    ws["K1"] = "derivative-monthly-pricing (automated)"

    header_row = 5
    for c, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=c, value=name)
        cell.font = Font(bold=True)

    for i, row in enumerate(rows, start=header_row + 1):
        for c, val in enumerate(row.as_cells(), start=1):
            ws.cell(row=i, column=c, value=val)

    # widen columns a little
    for c in range(1, len(COLUMNS) + 1):
        ws.column_dimensions[chr(64 + c)].width = 16

    wb.save(path)
    return path