"""Adaptive parsing: generic engine + AI-proposed spec.

Proves (a) the declarative engine reproduces the hand-written BNP/BAML parsers
exactly, and (b) a spec drafted for a held-out broker (Goldman Sachs) parses
correctly and matches the analyst's Marks sheet.
"""

import os

import pytest

from derivative_pricing.adaptive.agent import (
    SPECS_DIR,
    fingerprint,
    load_spec,
    propose_spec,
    validate_spec,
)
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.parsers.baml import BamlParser
from derivative_pricing.parsers.bnp import BnpParser
from derivative_pricing.parsers.marks_sheet import read_marks_sheet
from derivative_pricing.schema import Broker


def _pairs(marks):
    return {(m.trade_ref, round(m.mtm_local, 2)) for m in marks}


def test_engine_reproduces_bnp(bnp_statement_path):
    spec = load_spec(SPECS_DIR / "bnp.json")
    assert _pairs(run_spec(spec, bnp_statement_path)) == _pairs(BnpParser().parse(bnp_statement_path))


def test_engine_reproduces_baml(baml_statement_path):
    spec = load_spec(SPECS_DIR / "baml.json")
    assert _pairs(run_spec(spec, baml_statement_path)) == _pairs(BamlParser().parse(baml_statement_path))


def test_fingerprint_finds_header(gs_statement_path):
    fp = fingerprint(gs_statement_path)
    header_rows = [h["row"] for h in fp.header_candidates]
    assert 7 in header_rows  # GS header row
    assert any("GS Ref" in c for h in fp.header_candidates for c in h["cells"])


def test_gs_spec_validates(gs_statement_path):
    spec = load_spec(SPECS_DIR / "gs.json")
    rep = validate_spec(spec, gs_statement_path)
    assert rep.ok, rep.issues
    assert rep.rows_parsed > 250
    assert rep.pct_with_quantity == 1.0


def test_gs_spec_matches_marks_sheet(gs_statement_path, workbook_path):
    """The held-out GS spec must reproduce the analyst's GS marks exactly."""
    spec = load_spec(SPECS_DIR / "gs.json")
    parsed = {m.trade_ref: m.mtm_local for m in run_spec(spec, gs_statement_path)}
    sheet = {m.trade_ref: m.mtm_local
             for m in read_marks_sheet(workbook_path) if m.broker == Broker.GOLDMAN_SACHS}
    common = set(parsed) & set(sheet)
    assert len(common) > 250
    agree = sum(1 for r in common if abs(parsed[r] - sheet[r]) <= 0.01)
    assert agree == len(common), f"{agree}/{len(common)} agree"


def test_propose_spec_without_key_is_explicit():
    if os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("API key present; skip the no-key guard test")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        propose_spec("anything.xls")
