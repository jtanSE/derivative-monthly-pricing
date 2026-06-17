import datetime as dt

from derivative_pricing.matching import match_marks_to_trades
from derivative_pricing.schema import Broker, BrokerMark, Lineage, PutCall, Trade, TradeStatus


def _trade(ref: str, expiry="2026-12-14") -> Trade:
    return Trade(
        TradeNum="T1",
        PAM_CUSIP="BXIIG$125",
        CounterpartyTradeID=ref,
        EquityIndex="S&P 500",
        PutCall=PutCall.CALL,
        Date_Expiry=dt.date.fromisoformat(expiry),
        Strike=180.0,
        Notional=1_000_000.0,
        NumContract=5286.35,
        LongShort="L",
        Counterparty="Barclays",
        Date_Traded=dt.date(2024, 4, 14),
    )


def _mark(ref: str) -> BrokerMark:
    return BrokerMark(
        broker=Broker.BARCLAYS,
        trade_ref=ref,
        mtm_local=-6737.23,
        lineage=Lineage(source_file="x.xls", sheet="Marks", row=3),
    )


def test_exact_match():
    out = match_marks_to_trades([_trade("SWD1")], [_mark("SWD1")])
    assert len(out.matched) == 1
    assert not out.unmatched_trades and not out.unmatched_marks


def test_unmatched_both_sides_are_flagged_not_dropped():
    out = match_marks_to_trades([_trade("SWD1")], [_mark("SWD2")])
    assert [t.counterparty_trade_id for t in out.unmatched_trades] == ["SWD1"]
    assert [m.trade_ref for m in out.unmatched_marks] == ["SWD2"]


def test_duplicate_marks_flagged_ambiguous():
    # double-legged trades can report two marks on one ref (plan Q5)
    out = match_marks_to_trades([_trade("SWD1")], [_mark("SWD1"), _mark("SWD1")])
    assert not out.matched
    assert "SWD1" in out.ambiguous_marks
    assert len(out.ambiguous_marks["SWD1"]) == 2


def test_trade_status_classification():
    as_of = dt.date(2026, 3, 31)
    assert _trade("a", "2026-04-14").status(as_of) == TradeStatus.LIVE
    assert _trade("b", "2026-03-31").status(as_of) == TradeStatus.LIVE
    expired_unsettled = _trade("c", "2026-03-13")
    assert expired_unsettled.status(as_of) == TradeStatus.EXPIRED_UNSETTLED
    settled = _trade("d", "2026-03-13").model_copy(
        update={"date_settlement": dt.date(2026, 3, 17)}
    )
    assert settled.status(as_of) == TradeStatus.EXPIRED_SETTLED