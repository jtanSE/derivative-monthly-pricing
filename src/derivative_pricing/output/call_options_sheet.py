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

# Mirrors the analyst's Call Options header row (cols A-L), plus Broker for CSV output.
COLUMNS = [
    "Cusip", "Index", "Trade Reference", "Strike Rate", "Maturity Date",
    "Currency", "Notional (Ccy)", "Notional (USD)", "MTM (Ccy)", "MTM (USD)",
    "Shares", "Price (Ccy)", "Broker",
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
    broker: str | None = None  # for ALIC-style per-broker sectioning

    def as_cells(self) -> list:
        return [
            self.cusip, self.index, self.trade_reference, self.strike_rate,
            self.maturity_date, self.currency, self.notional_local,
            self.notional_usd, self.mtm_local, self.mtm_usd, self.shares,
            self.price_local, self.broker,
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
                broker=m.broker.value,
            )
        )
    return rows


def option_type(mark: BrokerMark) -> str:
    """'put' or 'call' for a mark, from its Call/Put field.

    Only an explicit put marker ('P'/'PUT') routes a trade to the puts sheet.
    Everything else -- plain calls AND the MCB net-MTM structures that carry no
    Call/Put marker -- is treated as a call, matching the analyst's ALIC, which
    books the MCB call-spreads in the Call Options sheet. A broker whose spec
    does not capture Call/Put therefore defaults to calls; onboarding a broker
    that trades puts requires mapping its Call/Put column.
    """
    raw = mark.extra.get("put_call")
    return "put" if raw is not None and str(raw).strip().upper().startswith("P") else "call"


def split_marks_by_option_type(
    marks: list[BrokerMark],
) -> tuple[list[BrokerMark], list[BrokerMark]]:
    """Partition marks into (calls, puts). Total is preserved -- every mark
    lands in exactly one sheet, so nothing is silently dropped."""
    calls, puts = [], []
    for m in marks:
        (puts if option_type(m) == "put" else calls).append(m)
    return calls, puts


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


# Banner text per broker, matching the analyst's section headers.
_BANNER = {"Barclays": "Barclays Custom"}


def _subtotal_cells(rows: list[CallOptionsOutputRow], label: str) -> list:
    """A subtotal/total row replicating the workbook's F7 semantics: notional
    and shares are POSITIVE-ONLY sums (short legs excluded); MTM is a plain sum.
    Per-unit price is left blank (not meaningful at the aggregate)."""
    def pos(vals):
        return sum(v for v in vals if v is not None and v > 0)

    def tot(vals):
        return sum(v for v in vals if v is not None)

    return [
        label, None, None, None, None, None,
        pos(r.notional_local for r in rows),
        pos(r.notional_usd for r in rows),
        tot(r.mtm_local for r in rows),
        tot(r.mtm_usd for r in rows),
        pos(r.shares for r in rows),
        None,
    ]


def write_call_options_sectioned_xlsx(
    rows: list[CallOptionsOutputRow], path: Path | str, *, as_of: str,
    sheet_label: str = "Call Options",
) -> Path:
    """Write an ALIC-shaped option sheet: one section per broker (banner ->
    header -> trades -> positive-only subtotal), then a grand total.

    Mirrors the analyst's actual layout (section banners + F7 subtotals + F8
    grand total) rather than a flat table. ``sheet_label`` names the sheet and
    its section title, so the same writer produces both the Call Options and
    Put Options ALIC sheets.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_label
    bold = Font(bold=True)

    # group rows by broker, preserving first-seen order
    groups: dict[str, list[CallOptionsOutputRow]] = {}
    for r in rows:
        groups.setdefault(r.broker or "Unknown", []).append(r)

    r_idx = 1
    section_subtotals: list[list] = []
    for broker, broker_rows in groups.items():
        banner = _BANNER.get(broker, broker)
        ws.cell(row=r_idx, column=1, value=banner).font = bold
        ws.cell(row=r_idx + 1, column=1, value=f"Equity Derivatives - ALIC {sheet_label}")
        ws.cell(row=r_idx + 2, column=1, value=as_of)
        header_r = r_idx + 3
        for c, name in enumerate(COLUMNS, start=1):
            ws.cell(row=header_r, column=c, value=name).font = bold
        rr = header_r + 1
        for row in broker_rows:
            for c, val in enumerate(row.as_cells(), start=1):
                ws.cell(row=rr, column=c, value=val)
            rr += 1
        sub = _subtotal_cells(broker_rows, f"{broker} subtotal")
        for c, val in enumerate(sub, start=1):
            cell = ws.cell(row=rr, column=c, value=val)
            cell.font = bold
        section_subtotals.append(sub)
        r_idx = rr + 2  # blank row between sections

    # grand total: sum the section subtotals (positive-only fields stay
    # positive-only because the subtotals already are; MTM sums the sums)
    def col_sum(i):
        return sum(s[i] for s in section_subtotals if isinstance(s[i], (int, float)))

    grand = ["GRAND TOTAL", None, None, None, None, None,
             col_sum(6), col_sum(7), col_sum(8), col_sum(9), col_sum(10), None]
    for c, val in enumerate(grand, start=1):
        ws.cell(row=r_idx, column=c, value=val).font = bold

    for c in range(1, len(COLUMNS) + 1):
        ws.column_dimensions[chr(64 + c)].width = 16
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="left")

    wb.save(path)
    return path