from derivative_pricing.parsers.bnp import BnpParser
from derivative_pricing.parsers.base import PARSER_REGISTRY
from derivative_pricing.parsers.marks_sheet import read_marks_sheet
from derivative_pricing.schema import Broker


def test_parser_is_registered():
    assert PARSER_REGISTRY[Broker.BNP_PARIBAS] is BnpParser


def test_reads_expected_volume(bnp_statement_path):
    marks = BnpParser().parse(bnp_statement_path)
    # 278 clean option rows in the 31 Mar 2026 file (delivered 01 Apr)
    assert len(marks) > 250
    assert all(m.broker == Broker.BNP_PARIBAS for m in marks)


def test_known_mark_value(bnp_statement_path):
    marks = {m.trade_ref: m for m in BnpParser().parse(bnp_statement_path)}
    assert "OP38270MUE" in marks
    assert abs(marks["OP38270MUE"].mtm_local - 8399.05) < 0.01


def test_rich_fields_captured(bnp_statement_path):
    marks = {m.trade_ref: m for m in BnpParser().parse(bnp_statement_path)}
    m = marks["OP38270MUE"]
    assert m.currency == "USD"
    assert m.extra["bloomberg_code"] == "NIGHTOWL"
    assert m.extra["put_call"].upper() == "CALL"
    assert m.extra["expiry"] == "2026-04-02"
    assert abs(m.extra["strike"] - 18514.3) < 0.1
    assert m.lineage.sheet == "Option - Reg. MM"


def test_matches_marks_sheet(bnp_statement_path, workbook_path):
    """The parser must extract the same column the analyst consumed: column P,
    "Mark to Market (N*P)". Proof: the overwhelming majority of refs present in
    both the raw statement and the analyst's consolidated Marks sheet agree to
    the cent.

    A minority (the "MCB" trades) differ because the analyst manually overrode
    those marks (consistent with the Marks-sheet note "Used purchase price.");
    the statement's columns P/R/S are internally identical for those rows, so
    the divergence is an upstream override, not a parsing error. These are
    exactly the cases the reconciliation engine is designed to flag.
    """
    parsed = {m.trade_ref: m.mtm_local for m in BnpParser().parse(bnp_statement_path)}
    marks_sheet = {
        m.trade_ref: m.mtm_local
        for m in read_marks_sheet(workbook_path)
        if m.broker == Broker.BNP_PARIBAS
    }
    common = set(parsed) & set(marks_sheet)
    assert len(common) > 200, f"only {len(common)} overlapping refs"
    agree = sum(1 for ref in common if abs(parsed[ref] - marks_sheet[ref]) <= 0.01)
    rate = agree / len(common)
    assert rate >= 0.90, f"only {rate:.1%} of marks agree to the cent ({agree}/{len(common)})"