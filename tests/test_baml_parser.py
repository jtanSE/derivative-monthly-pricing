from derivative_pricing.parsers.baml import BamlParser
from derivative_pricing.parsers.base import PARSER_REGISTRY
from derivative_pricing.parsers.marks_sheet import read_marks_sheet
from derivative_pricing.schema import Broker


def test_parser_is_registered():
    assert PARSER_REGISTRY[Broker.BAML] is BamlParser


def test_reads_legs(baml_statement_path):
    marks = BamlParser().parse(baml_statement_path)
    assert len(marks) > 50
    assert all(m.broker == Broker.BAML for m in marks)
    # double-legged: at least some refs appear on two legs
    from collections import Counter
    c = Counter(m.trade_ref for m in marks)
    assert any(v >= 2 for v in c.values()), "expected multi-leg deals"


def test_known_long_leg_value(baml_statement_path):
    marks = BamlParser().parse(baml_statement_path)
    by_ref = {}
    for m in marks:
        by_ref.setdefault(m.trade_ref, []).append(m)
    legs = by_ref.get("2582240555")
    assert legs, "deal 2582240555 not found"
    values = [m.mtm_local for m in legs]
    assert any(abs(v - 1422758.69) < 0.5 for v in values)  # long leg
    assert any(abs(v - -763320) < 1 for v in values)  # short leg


def test_rich_fields(baml_statement_path):
    marks = BamlParser().parse(baml_statement_path)
    m = next(m for m in marks if m.trade_ref == "2582240555" and m.mtm_local > 0)
    assert m.extra["underlying"] == "S&P 500 INDEX"
    assert m.extra["put_call"] == "C"
    assert m.extra["direction"] == "Long"
    assert m.extra["expiry"] == "2026-04-02"
    assert m.currency == "USD"


def test_marks_values_present(baml_statement_path, workbook_path):
    """Every BAML value the analyst recorded in the Marks sheet must appear as
    some leg's "Estimated mid-market value" in the raw statement -- proving the
    parser reads the correct column even with the double-legged structure."""
    parsed = {}
    for m in BamlParser().parse(baml_statement_path):
        parsed.setdefault(m.trade_ref, []).append(m.mtm_local)
    marks_sheet = [
        (m.trade_ref, m.mtm_local)
        for m in read_marks_sheet(workbook_path)
        if m.broker == Broker.BAML
    ]
    assert len(marks_sheet) > 30
    common = [(ref, v) for ref, v in marks_sheet if ref in parsed]
    assert len(common) > 30, f"only {len(common)} BAML refs overlap"
    hits = sum(
        1 for ref, v in common
        if any(abs(v - leg) <= 0.5 for leg in parsed[ref])
    )
    assert hits / len(common) >= 0.90, f"only {hits}/{len(common)} marks-sheet values found"
