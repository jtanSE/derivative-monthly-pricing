"""Adaptive parsing: generic engine + AI-proposed spec.

Proves the declarative spec + engine reproduces the analyst's marks for every
reference broker (BNP/BAML), and that a spec drafted for held-out brokers
(Goldman Sachs, HSBC) parses correctly and matches the analyst's Marks sheet.
Correctness is judged against the analyst's own Marks sheet -- no hand-written
parser is needed as an intermediary.
"""

import os

import pytest

from derivative_pricing.adaptive.agent import (
    SPECS_DIR,
    fingerprint,
    load_spec,
    onboard_file,
    propose_spec,
    save_spec,
    validate_spec,
)
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.parsers.marks_sheet import read_marks_sheet
from derivative_pricing.schema import Broker


def _spec_marks(name, path):
    return run_spec(load_spec(SPECS_DIR / name), path)


def test_bnp_spec_matches_marks_sheet(bnp_statement_path, workbook_path):
    # 251/272 agree to the cent; the rest are known manual analyst overrides.
    parsed = {m.trade_ref: m.mtm_local for m in _spec_marks("bnp.json", bnp_statement_path)}
    sheet = {m.trade_ref: m.mtm_local
             for m in read_marks_sheet(workbook_path) if m.broker == Broker.BNP_PARIBAS}
    common = set(parsed) & set(sheet)
    assert len(common) > 200
    agree = sum(1 for r in common if abs(parsed[r] - sheet[r]) <= 0.01)
    assert agree / len(common) >= 0.90


def test_baml_spec_matches_marks_sheet(baml_statement_path, workbook_path):
    # BAML is double-legged: each marks-sheet value must appear as some leg.
    parsed: dict[str, list[float]] = {}
    for m in _spec_marks("baml.json", baml_statement_path):
        parsed.setdefault(m.trade_ref, []).append(m.mtm_local)
    sheet = [(m.trade_ref, m.mtm_local)
             for m in read_marks_sheet(workbook_path) if m.broker == Broker.BAML]
    common = [(ref, v) for ref, v in sheet if ref in parsed]
    assert len(common) > 30
    hits = sum(1 for ref, v in common if any(abs(v - leg) <= 0.5 for leg in parsed[ref]))
    assert hits / len(common) >= 0.90


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


def test_engine_reads_xlsx(hsbc_statement_path):
    """HSBC is .xlsx; the format-agnostic reader must handle it."""
    spec = load_spec(SPECS_DIR / "hsbc.json")
    marks = run_spec(spec, hsbc_statement_path)
    assert len(marks) > 10
    assert all(m.broker == Broker.HSBC for m in marks)


def test_hsbc_spec_matches_marks_sheet(hsbc_statement_path, workbook_path):
    """Held-out HSBC (.xlsx, 4th distinct format) must reproduce the analyst's
    HSBC marks exactly."""
    spec = load_spec(SPECS_DIR / "hsbc.json")
    parsed = {m.trade_ref: m.mtm_local for m in run_spec(spec, hsbc_statement_path)}
    sheet = {m.trade_ref: m.mtm_local
             for m in read_marks_sheet(workbook_path) if m.broker == Broker.HSBC}
    common = set(parsed) & set(sheet)
    assert len(common) >= 15
    agree = sum(1 for r in common if abs(parsed[r] - sheet[r]) <= 0.01)
    assert agree == len(common), f"{agree}/{len(common)} agree"


def test_propose_spec_without_key_is_explicit():
    if os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("API key present; skip the no-key guard test")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        propose_spec("anything.xls")


def test_save_spec_roundtrip_and_no_overwrite(tmp_path):
    spec = load_spec(SPECS_DIR / "gs.json")
    saved = save_spec(spec, specs_dir=tmp_path)
    assert saved.exists()
    assert load_spec(saved).broker == spec.broker
    # refuses to silently overwrite
    with pytest.raises(FileExistsError):
        save_spec(spec, specs_dir=tmp_path)


def test_onboard_known_file_is_noop(gs_statement_path):
    res = onboard_file(gs_statement_path)
    assert res.status == "already_known"
    assert res.broker == "Goldman Sachs"


def test_onboard_unknown_without_key_asks_for_key(workbook_path):
    if os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("API key present")
    res = onboard_file(workbook_path)
    assert res.status == "needs_key"
