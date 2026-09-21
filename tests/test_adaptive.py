"""Adaptive parsing: generic engine + AI-proposed spec.

Proves the declarative spec + engine reproduces the analyst's marks for every
reference broker (BNP/BAML), and that a spec drafted for held-out brokers
(Goldman Sachs, HSBC) parses correctly and matches the analyst's Marks sheet.
Correctness is judged against the analyst's own Marks sheet -- no hand-written
parser is needed as an intermediary.
"""

import os

import pytest

from derivative_pricing.adaptive import agent as agent_module
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
    # Compare NET mtm per ref: split trades parse as two legs whose sum equals
    # the statement's (and Marks sheet's) net value. ~92% agree to the cent;
    # the rest are known manual analyst overrides.
    parsed: dict[str, float] = {}
    for m in _spec_marks("bnp.json", bnp_statement_path):
        parsed[m.trade_ref] = parsed.get(m.trade_ref, 0.0) + m.mtm_local
    sheet: dict[str, float] = {}
    for m in read_marks_sheet(workbook_path):
        if m.broker == Broker.BNP_PARIBAS:
            sheet[m.trade_ref] = sheet.get(m.trade_ref, 0.0) + m.mtm_local
    common = set(parsed) & set(sheet)
    assert len(common) > 200
    agree = sum(1 for r in common if abs(parsed[r] - sheet[r]) <= 0.01)
    assert agree / len(common) >= 0.90


def test_bnp_split_trades_parse_as_two_legs(bnp_statement_path):
    """BNP 'Split' trades: parent row carries ref + NET mtm; ref-less leg rows
    below carry per-leg direction/qty/strike/mtm. The engine must emit the two
    legs (inheriting the parent's ref/expiry/underlying) and drop the net row
    -- matching the ALIC sheet, which records these as two rows per ref."""
    marks = [m for m in _spec_marks("bnp.json", bnp_statement_path)
             if m.trade_ref == "OP64573MCB"]
    assert len(marks) == 2
    legs = {m.extra["direction"]: m for m in marks}
    assert set(legs) == {"Long", "Short"}
    assert legs["Long"].mtm_local == pytest.approx(37150.95)
    assert legs["Short"].mtm_local == pytest.approx(-18221.76)
    # legs inherit parent context and carry their own qty/strike
    for m in marks:
        assert m.extra["quantity"] == pytest.approx(148.70)
        assert m.extra["underlying"] == "Russell 2000 Index"
        assert m.extra["expiry"] == "2026-08-13"
    assert legs["Long"].extra["strike"] == pytest.approx(2258.04)
    assert legs["Short"].extra["strike"] == pytest.approx(2396.23)
    # every BNP row now carries a quantity (no more net-only parents)
    all_marks = _spec_marks("bnp.json", bnp_statement_path)
    assert all(m.extra.get("quantity") is not None for m in all_marks)


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


def test_quantity_notional_fallback_signed():
    """Barclays books .BXIIGTBB structured trades with an empty (or absent)
    Quantity column and the share count under a SIGNED Notional column, while
    vanilla trades have Quantity populated and Notional = dollar notional. The
    quantity rule's fallback must fire only on the empty-Quantity row, and take
    that row's sign from the (signed) Notional value."""
    from derivative_pricing.adaptive.engine import (
        _map_columns, _map_fallback_columns, _parse_table)
    from derivative_pricing.adaptive.reader import Grid
    from derivative_pricing.adaptive.spec import ColumnRule, DirectionRule, ParserSpec
    from derivative_pricing.output import build_rows_from_marks
    from derivative_pricing.parsers.base import apply_sign_convention

    grid = Grid("s", [
        ["Trade Ref.", "Put / Call", "Strike Rate", "Quantity", "Notional",
         "MTM(Ccy)", "Ccy", "Short / Long"],
        ["N1", "C", 6500.0, 220.0, 473275.0, 1000.0, "USD", "Long"],   # vanilla
        ["B1", "C", None, None, -2935.37, 5000.0, "USD", ""],           # .BXIIGTBB
    ])
    spec = ParserSpec(
        broker=Broker.BARCLAYS, header_anchor="Trade Ref",
        columns=[
            ColumnRule(field="trade_ref", header_contains="Trade Ref"),
            ColumnRule(field="mtm_local", header_contains="MTM(Ccy)", kind="number"),
            ColumnRule(field="quantity", header_contains="Quantity", kind="number",
                       fallback_header_contains="Notional", fallback_is_signed=True),
            ColumnRule(field="strike", header_contains="Strike Rate", kind="number"),
            ColumnRule(field="put_call", header_contains="Put / Call"),
        ],
        direction=DirectionRule(mode="column", header_contains="Short / Long"),
    )
    cols = _map_columns(grid, 0, spec.columns)
    fbs = _map_fallback_columns(grid, 0, spec.columns)
    marks = {m.trade_ref: m for m in _parse_table(spec, grid, __import__("pathlib").Path("x"), 0, grid.nrows, cols, fbs)}

    # vanilla: quantity from Quantity, direction from column
    assert marks["N1"].extra["quantity"] == 220.0
    assert marks["N1"].extra["direction"] == "Long"
    # structured: quantity from the signed Notional fallback, direction from its sign
    assert marks["B1"].extra["quantity"] == pytest.approx(-2935.37)
    assert marks["B1"].extra["direction"] == "Short"

    # end-to-end sign: Barclays dealer flip yields ALIC's client view
    rows = {r.trade_reference: r for r in build_rows_from_marks(list(marks.values()), {"USD": 1.0})}
    assert rows["N1"].shares == apply_sign_convention(Broker.BARCLAYS, 220.0)   # -220
    assert rows["B1"].shares == pytest.approx(2935.37)                          # +2935.37 (ALIC)


def test_canonical_broker_normalizes_letterhead_spellings():
    # observed live: the model copied "J.P. Morgan" off a letterhead
    assert agent_module._canonical_broker("J.P. Morgan") == "JP Morgan"
    assert agent_module._canonical_broker("credit  suisse") == "Credit Suisse"
    assert agent_module._canonical_broker("BNP PARIBAS") == "BNP Paribas"
    # unknown counterparties pass through unchanged (must still fail validation)
    assert agent_module._canonical_broker("Acme Bank") == "Acme Bank"


def test_propose_spec_without_backend_is_explicit(monkeypatch):
    # simulate: no API key AND no claude CLI on this machine
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agent_module, "_find_claude_cli", lambda: None)
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


def test_onboard_unknown_without_backend_asks_for_key(workbook_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agent_module, "_find_claude_cli", lambda: None)
    res = onboard_file(workbook_path)
    assert res.status == "needs_key"


# --- ops-audit regression tests ---------------------------------------------


def test_spec_rejects_unknown_fields():
    """Typo'd directives in a spec JSON must fail loudly, not be dropped."""
    import json

    from derivative_pricing.adaptive.spec import ParserSpec

    good = json.loads((SPECS_DIR / "gs.json").read_text(encoding="utf-8"))
    bogus = dict(good, bogus_directive=True)
    with pytest.raises(Exception):
        ParserSpec.model_validate(bogus)
    # mtm_sign was removed in the sign-consolidation; it must stay gone
    with pytest.raises(Exception):
        ParserSpec.model_validate(dict(good, mtm_sign=-1))


def test_single_source_of_sign_truth(tmp_path):
    """Specs emit AS-REPORTED values; the output layer applies the broker sign
    map exactly once. A dealer-perspective Nomura mark of -5000 must surface
    as +5000 (internal view) in the output row — no double flip possible."""
    import openpyxl

    from derivative_pricing.adaptive.spec import ColumnRule, ParserSpec
    from derivative_pricing.output import build_rows_from_marks

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Nomura Trade ID", "valuationNativeCurrencyAmount", "Qty"])
    ws.append(["EUN1", -5000.0, 10.0])
    path = tmp_path / "nomura_like.xlsx"
    wb.save(path)

    spec = ParserSpec(broker="Nomura", header_anchor="Nomura Trade ID", columns=[
        ColumnRule(field="trade_ref", header_contains="Nomura Trade ID"),
        ColumnRule(field="mtm_local", header_contains="valuationNativeCurrencyAmount", kind="number"),
        ColumnRule(field="quantity", header_contains="Qty", kind="number"),
    ])
    marks = run_spec(spec, path)
    assert marks[0].mtm_local == -5000.0  # as-reported, per the BrokerMark contract
    row = build_rows_from_marks(marks, {"USD": 1.0})[0]
    assert row.mtm_local == 5000.0  # internal view after the single flip


def test_validate_spec_catches_mismapped_mtm_column(gs_statement_path):
    """A spec whose mtm_local resolves to the Strike column parses fine but is
    a mis-mapping; the gate must flag the column collision."""
    import json

    from derivative_pricing.adaptive.spec import ParserSpec

    raw = json.loads((SPECS_DIR / "gs.json").read_text(encoding="utf-8"))
    for c in raw["columns"]:
        if c["field"] == "mtm_local":
            c["header_contains"] = "Strike"
    rep = validate_spec(ParserSpec.model_validate(raw), gs_statement_path)
    assert not rep.ok
    assert any("same column" in i for i in rep.issues)
