from derivative_pricing.parsers.marks_sheet import read_marks_sheet
from derivative_pricing.schema import Broker


# In the March 2026 sanitized file these three small-book brokers have empty
# mark columns (only analyst notes like "Convert Mid-Price to # after
# pasting"); their layout mapping is verified correct, they just carry no data
# this month. Every other broker must read cleanly.
EMPTY_IN_TEST_FILE = {Broker.MORGAN_STANLEY, Broker.WELLS_FARGO, Broker.CREDIT_SUISSE}


def test_reads_every_populated_broker(workbook_path):
    marks = read_marks_sheet(workbook_path)
    brokers_seen = {m.broker for m in marks}
    expected = set(Broker) - EMPTY_IN_TEST_FILE
    assert brokers_seen == expected, f"missing: {expected - brokers_seen}"


def test_known_barclays_mark(workbook_path):
    # Verified directly from the Marks sheet: SWD273918119 -> 2,049,038.xx
    marks = read_marks_sheet(workbook_path)
    barclays = {m.trade_ref: m for m in marks if m.broker == Broker.BARCLAYS}
    assert "SWD273918119" in barclays
    assert abs(barclays["SWD273918119"].mtm_local - 2_049_038) < 1.0


def test_numeric_trade_refs_normalized(workbook_path):
    # BAML refs are numeric cells in Excel; they must come out as clean strings
    marks = read_marks_sheet(workbook_path)
    baml_refs = [m.trade_ref for m in marks if m.broker == Broker.BAML]
    assert baml_refs, "no BAML marks read"
    assert all("." not in ref for ref in baml_refs)


def test_lineage_present_on_every_mark(workbook_path):
    marks = read_marks_sheet(workbook_path)
    assert all(m.lineage.sheet == "Marks" and m.lineage.row >= 3 for m in marks)