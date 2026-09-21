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

# Leading characters that make a spreadsheet engine treat a *text* cell as a
# formula (openpyxl stores a string starting with "=" as a live formula; Excel
# also evaluates cells starting with +, -, @, or a leading tab/CR/LF on open).
# Broker statements — and the free-text exclusion note — are untrusted input, so
# a crafted trade ref like `=WEBSERVICE(...)`/`=cmd|'/c ...'!A1` would otherwise
# execute when an analyst opens the generated workbook (CSV/formula injection).
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")


def _safe_cell(value):
    """Neutralize spreadsheet-formula injection in text cells written to output.

    Numbers, dates, None, and enums pass through untouched (a negative MTM is a
    float, not a string, so it is never affected). Only a *string* beginning with
    a formula trigger is defused, by prefixing a single quote so the engine keeps
    it as literal text. Legitimate values in these columns (trade refs, index
    names, currencies) never start with those characters, so display is unchanged
    in practice — the guard bites only on hostile input."""
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGERS):
        return "'" + value
    return value


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
    # Set when the statement's quantity is NOT a share count (e.g. GS Asian
    # options quote a notional): shares is left None so subtotals stay honest,
    # and this note says why the row needs manual/traders-DB conversion.
    shares_review: str | None = None

    def as_cells(self) -> list:
        return [
            self.cusip, self.index, self.trade_reference, self.strike_rate,
            self.maturity_date, self.currency, self.notional_local,
            self.notional_usd, self.mtm_local, self.mtm_usd, self.shares,
            self.price_local, self.broker,
        ]


def _signed_shares(quantity: float | None, direction: str | None) -> float | None:
    """Signed position size from the statement's own perspective: negative for
    short, positive for long.

    Statements report quantity (N) either as a positive magnitude plus a
    direction (BNP/GS/HSBC/MS) or as an already-signed number (BAML). When an
    explicit direction is known it is authoritative — magnitude from quantity,
    sign from direction — so double-signed inputs can't flip twice. Without a
    direction the quantity passes through as-is. The dealer→client flip for
    Nomura/Barclays is applied separately (apply_sign_convention), same as MTM.
    """
    if quantity is None:
        return None
    if direction:
        d = direction.strip().lower()
        if d == "short":
            return -abs(quantity)
        if d == "long":
            return abs(quantity)
    return quantity


# An equity-index option struck far below the index level (< 50, e.g. GS's
# real $1.0 strikes) is a normalized/notional contract: its quantity column
# holds a dollar notional, not a share count. Real index strikes are >> this.
_RATIO_STRIKE_MAX = 50.0
# A position more than this multiple of its broker's median |quantity| is
# almost certainly a notional mis-booked as a quantity, not a real share count.
# Calibrated to separate the two observed regimes cleanly: Nomura's mis-booked
# notionals run ~20,000x the broker median, while legitimate large positions
# top out far lower (Barclays' biggest real .BXIIGTBB position is ~190x). 1000x
# sits well above every genuine position and well below the notional mis-books.
_QTY_OUTLIER_MULTIPLE = 1000.0


def _broker_qty_medians(marks: list[BrokerMark]) -> dict:
    """Median |quantity| per broker, for the outlier guard below."""
    import statistics

    by_broker: dict = {}
    for m in marks:
        q = m.extra.get("quantity")
        if q:
            by_broker.setdefault(m.broker, []).append(abs(q))
    return {b: statistics.median(v) for b, v in by_broker.items() if v}


def _low_index_strike(m: BrokerMark) -> bool:
    """True when an equity-index option carries a strike far below any real
    index level (< 50). The strike itself is genuine (GS really does book these
    at e.g. $1.0 / $1.0609) — it marks a NORMALIZED / notional-based contract,
    where the statement's quantity is a dollar notional, not a share count.
    Real index strikes are hundreds-to-thousands, so this cleanly isolates the
    normalized structures."""
    strike = m.extra.get("strike")
    return isinstance(strike, (int, float)) and 0 < strike < _RATIO_STRIKE_MAX


def _shares_review_reason(m: BrokerMark, qty_median: float | None) -> str | None:
    """Why this row's quantity should NOT be summed as shares (else None).

    Both cases are trades whose statement quantity is a notional, needing the
    trade's fixing level (traders DB) to convert to a share count. The strike
    is reported as-is either way — it is not the thing being flagged."""
    if _low_index_strike(m):
        strike = m.extra.get("strike")
        return (f"normalized contract (strike {strike:g} << index level): the "
                "statement quantity is a notional, converted to shares via the "
                "trade's fixing level (traders DB)")
    q = m.extra.get("quantity")
    if q and qty_median and abs(q) > _QTY_OUTLIER_MULTIPLE * qty_median:
        return (f"quantity {abs(q):,.0f} is >{_QTY_OUTLIER_MULTIPLE:g}x the broker "
                f"median ({qty_median:,.0f}) — likely a notional, not shares")
    return None


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
    qty_medians = _broker_qty_medians(marks)
    rows: list[CallOptionsOutputRow] = []
    for m in marks:
        currency = (m.currency or "USD").upper()
        fx = fx_rates.get(currency, 1.0 if currency == "USD" else None)

        # Signed shares in the client view: magnitude+sign from the statement's
        # own direction, then the F3 dealer flip so Nomura/Barclays (which
        # report position size negated, exactly like their MTM) land on the
        # client's side. Verified against ALIC: Nomura shares == -exchNotl1.
        # A row whose quantity is really a NOTIONAL (ratio-strike trades; wild
        # magnitude outliers) is flagged instead of summed, since converting it
        # needs per-trade terms not on the statement.
        shares_review = _shares_review_reason(m, qty_medians.get(m.broker))
        if shares_review is not None:
            shares = None
        else:
            signed = _signed_shares(m.extra.get("quantity"), m.extra.get("direction"))
            shares = apply_sign_convention(m.broker, signed) if signed is not None else None
        raw_index = m.extra.get("underlying")
        # canonicalize_index returns "" for product-type values ("Compound"),
        # which are not index names; normalize that to None so the column reads
        # as genuinely absent rather than empty-but-present.
        index_name = (canonicalize_index(raw_index) or None) if raw_index else None
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
                strike_rate=m.extra.get("strike"),  # shown as reported (a $1.0 strike is real)
                maturity_date=m.extra.get("expiry"),
                currency=currency,
                notional_local=notl,
                notional_usd=notl_usd,
                mtm_local=mtm,
                mtm_usd=mtm_usd,
                shares=shares,
                price_local=price,
                broker=m.broker.value,
                shares_review=shares_review,
            )
        )
    return rows


def valuation_review(row: "CallOptionsOutputRow") -> str | None:
    """Narrow flag: a call we can neither value nor identify.

    A row is flagged only when it has NO strike, NO computable per-unit price,
    AND no recognized underlying/index — i.e. nothing to price it by and nothing
    to identify it as. This deliberately does NOT flag structured-index trades
    (e.g. Barclays `.BXIIGTBB`), which legitimately carry no strike/shares but
    ARE identified by their index and valued straight off MTM — those are a
    normal product class, not an anomaly. The MTM is kept regardless; this is a
    review hint, not an exclusion.
    """
    has_index = bool((row.index or "").strip())
    if row.strike_rate is None and row.price_local is None and not has_index:
        return "no strike, price, or underlying — cannot value or identify this row"
    return None


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
            ws.cell(row=i, column=c, value=_safe_cell(val))

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
        present = [v for v in vals if v is not None]
        # When NO row in the section carried this value, report it absent rather
        # than as 0. Goldman and Nomura can produce a whole section with no
        # notional (no index price to multiply by), and a "0.00" subtotal reads
        # as "these positions are worth nothing" -- a different and wrong claim
        # from "this could not be computed". A blank cannot be mistaken for a
        # control total; a zero silently can, and rolls up into the grand total
        # looking complete.
        if not present:
            return None
        return sum(v for v in present if v > 0)

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
                ws.cell(row=rr, column=c, value=_safe_cell(val))
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

    # Say so when the notional total is incomplete. Without this the grand
    # total looks like a control total while excluding whole brokers, and the
    # only clue is a blank subtotal several hundred rows up.
    no_notional = [b for b, rs in groups.items()
                   if all(r.notional_usd is None for r in rs)]
    if no_notional and len(no_notional) < len(groups):
        ws.cell(row=r_idx + 1, column=1,
                value=(f"NOTE: Notional excludes {', '.join(sorted(no_notional))} "
                       f"({len(no_notional)} of {len(groups)} sections) -- no index price "
                       f"available, so no notional could be computed. MTM is complete.")
                ).font = bold

    for c in range(1, len(COLUMNS) + 1):
        ws.column_dimensions[chr(64 + c)].width = 16
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="left")

    wb.save(path)
    return path