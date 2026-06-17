"""Canonical data models. See SYSTEMIMPLEMENTATION.md section 4.

Confidence tags from the plan apply here: fields marked TBD in the plan
(currency provenance, shares provenance, portfolio allocations) are present
but optional until the open questions are resolved.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# Canonical broker names. Workbook banner "Barclays Custom" maps to BARCLAYS.
class Broker(str, Enum):
    BARCLAYS = "Barclays"
    BNP_PARIBAS = "BNP Paribas"
    MORGAN_STANLEY = "Morgan Stanley"
    GOLDMAN_SACHS = "Goldman Sachs"
    WELLS_FARGO = "Wells Fargo"
    BAML = "BAML"
    CREDIT_SUISSE = "Credit Suisse"
    NOMURA = "Nomura"
    JP_MORGAN = "JP Morgan"
    HSBC = "HSBC"


class PutCall(str, Enum):
    CALL = "C"
    PUT = "P"


class TradeStatus(str, Enum):
    LIVE = "live"
    EXPIRED_UNSETTLED = "expired_unsettled"
    EXPIRED_SETTLED = "expired_settled"


class MatchStatus(str, Enum):
    EXACT = "exact"
    CANDIDATE = "candidate"  # fuzzy / composite-key match, needs review
    UNMATCHED = "unmatched"


class ReconStatus(str, Enum):
    OK = "ok"
    OUT_OF_TOLERANCE = "out_of_tolerance"
    NOT_COMPARABLE = "not_comparable"  # missing one side
    OUT_OF_SCOPE_MVP = "out_of_scope_mvp"  # e.g. puts in the calls-only MVP


class Lineage(BaseModel):
    """Where a sourced value came from. Every ingested record carries one."""

    source_file: str
    sheet: str | None = None
    row: int | None = None  # 1-based Excel row


class BrokerMark(BaseModel):
    """Normalized output contract of every broker parser (plan section 1.4).

    ``mtm_local`` is the value as reported by the broker, BEFORE any sign
    convention is applied. Use parsers.base.apply_sign_convention to get the
    internal-view MTM (rule F3: Barclays reports with flipped sign).
    """

    broker: Broker
    trade_ref: str
    mtm_local: float
    currency: str | None = None
    # Richer fields a statement provides beyond the minimal contract
    # (trade/expiry dates, underlying, strike, quantity, put/call, ...).
    # Used to enrich/validate the canonical record during matching.
    extra: dict[str, Any] = Field(default_factory=dict)
    lineage: Lineage


class Trade(BaseModel):
    """One row of the traders database export (15-field schema, plan section 2).

    Field types are provisional until a real export file is available (Q1).
    """

    trade_num: str = Field(alias="TradeNum")
    pam_cusip: str = Field(alias="PAM_CUSIP")
    counterparty_trade_id: str = Field(alias="CounterpartyTradeID")
    equity_index: str = Field(alias="EquityIndex")
    put_call: PutCall = Field(alias="PutCall")
    date_expiry: dt.date = Field(alias="Date_Expiry")
    strike: float = Field(alias="Strike")
    notional: float = Field(alias="Notional")
    num_contract: float = Field(alias="NumContract")
    long_short: str = Field(alias="LongShort")
    total_premium_local: float | None = Field(None, alias="TotalPremium_LOCAL")
    total_premium_usd: float | None = Field(None, alias="TotalPremium_USD")
    counterparty: str = Field(alias="Counterparty")
    date_traded: dt.date = Field(alias="Date_Traded")
    date_settlement: dt.date | None = Field(None, alias="Date_Settlement")

    model_config = {"populate_by_name": True}

    def status(self, as_of: dt.date) -> TradeStatus:
        """Live = expiry not passed. Expired-but-unsettled trades may still
        appear in broker/trader records and cause MTM differences."""
        if self.date_expiry >= as_of:
            return TradeStatus.LIVE
        if self.date_settlement is None or self.date_settlement > as_of:
            return TradeStatus.EXPIRED_UNSETTLED
        return TradeStatus.EXPIRED_SETTLED


class CanonicalRecord(BaseModel):
    """One row of the canonical live-trades CSV (plan section 4, draft v1)."""

    as_of_date: dt.date
    trade_num: str | None = None
    pam_cusip: str | None = None
    counterparty_trade_id: str
    broker: Broker | None = None
    equity_index_raw: str | None = None
    equity_index_canonical: str | None = None
    put_call: PutCall | None = None
    long_short: str | None = None
    strike: float | None = None
    date_traded: dt.date | None = None
    date_expiry: dt.date | None = None
    date_settlement: dt.date | None = None
    currency: str | None = None
    shares: float | None = None
    index_price_monthend: float | None = None
    fx_rate_to_usd: float | None = None
    notional_local: float | None = None
    notional_usd: float | None = None
    mtm_broker_local: float | None = None
    mtm_broker_usd: float | None = None
    mtm_derived_local: float | None = None
    mtm_derived_usd: float | None = None
    mtm_source: str = "broker"  # owner decision: broker MTM is primary
    price_per_unit_local: float | None = None
    portfolio_118_alloc: float | None = None  # TBD: feed unresolved (Q6)
    portfolio_119_alloc: float | None = None
    trade_status: TradeStatus | None = None
    match_status: MatchStatus | None = None
    recon_status: ReconStatus | None = None
    recon_diff_local: float | None = None
    lineage_mtm: Lineage | None = None
    lineage_trade: Lineage | None = None