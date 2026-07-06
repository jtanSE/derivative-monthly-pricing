import datetime as dt

from derivative_pricing.ingest import content_key, screen_marks
from derivative_pricing.ingest.screening import parse_date
from derivative_pricing.schema import Broker, BrokerMark, Lineage


def _mark(ref, mtm=100.0, strike=6500.0, qty=10.0, expiry="2026-12-31", broker=Broker.HSBC):
    return BrokerMark(
        broker=broker, trade_ref=ref, mtm_local=mtm, currency="USD",
        extra={"strike": strike, "quantity": qty, "expiry": expiry},
        lineage=Lineage(source_file="x.xls", sheet="s", row=1),
    )


def test_parse_date_handles_broker_formats():
    assert parse_date("2026-04-02") == dt.date(2026, 4, 2)      # ISO (BNP/BAML)
    assert parse_date("31-Dec-2026") == dt.date(2026, 12, 31)   # GS/HSBC
    assert parse_date("23-Mar-2027") == dt.date(2027, 3, 23)
    assert parse_date("") is None
    assert parse_date("not a date") is None


def test_exact_repeat_is_dropped_and_flagged():
    m = _mark("T1")
    res = screen_marks([m, m.model_copy()], "2026-03-31")
    assert len(res.kept) == 1
    assert len(res.repeats) == 1
    assert res.repeats[0].trade_ref == "T1"


def test_double_legged_same_ref_not_a_repeat():
    # two legs share a trade ref but differ in strike/qty/mtm -> both kept
    leg1 = _mark("D1", mtm=1000.0, strike=5000.0, qty=100.0, broker=Broker.BAML)
    leg2 = _mark("D1", mtm=-800.0, strike=6000.0, qty=-100.0, broker=Broker.BAML)
    res = screen_marks([leg1, leg2], "2026-03-31")
    assert len(res.kept) == 2
    assert not res.repeats


def test_matured_is_flagged_but_kept():
    live = _mark("L1", expiry="2026-12-31")
    matured = _mark("M1", expiry="2026-01-15")   # before as-of
    on_asof = _mark("E1", expiry="2026-03-31")   # exactly as-of counts as matured
    res = screen_marks([live, matured, on_asof], "2026-03-31")
    assert len(res.kept) == 3  # matured entries are kept, not dropped
    matured_refs = {f.trade_ref for f in res.matured}
    assert matured_refs == {"M1", "E1"}


def test_unknown_maturity_flagged():
    res = screen_marks([_mark("U1", expiry="garbage")], "2026-03-31")
    assert len(res.unknown_maturity) == 1
    assert res.kept  # still kept


def test_existing_keys_catches_cross_run_repeat():
    m = _mark("X1")
    res = screen_marks([m], "2026-03-31", existing_keys={content_key(m)})
    assert not res.kept
    assert len(res.repeats) == 1
