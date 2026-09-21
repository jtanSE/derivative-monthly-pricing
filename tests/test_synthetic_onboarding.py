"""Onboarding NEW broker formats (creative, randomized synthetic statements).

These brokers are NOT the currently-spec'd ones (BNP/BAML/GS/HSBC/Nomura/MS/
Barclays) -- reusing those names would make the system read the file as a
*change* to a known broker (drift), which is tested separately. Here we ask a
different question: given an UNFAMILIAR statement, can it be onboarded?

The brokers used (Credit Suisse, JP Morgan, Wells Fargo) are valid but spec-less
in the closed Broker enum, so the router treats them as unknown and hands them
to the onboarding agent -- exactly the new-format path.

The LLM drafting step is non-deterministic and costs tokens, so it is not run in
normal test runs. Instead we test everything the AI onboarding relies on and
orchestrates, deterministically:

  * the fingerprint surfaces the (oddly-named) headers the model must map;
  * the engine locates creatively-named columns ('Share Number' -> quantity,
    'Valuation (USD)' -> mtm) once a spec points at them;
  * the validation gate accepts a good drafted spec and rejects broken ones
    (a required column absent, quantity barely present);
  * the onboard_file plumbing returns 'proposed' for a good draft and 'rejected'
    for a bad one / an unknown counterparty -- driven with a mocked model.

A single opt-in test (RUN_LLM_ONBOARDING=1) exercises the real model end to end.
"""

import os

import pytest

from derivative_pricing.adaptive import agent as agent_module
from derivative_pricing.adaptive.agent import (
    fingerprint,
    onboard_file,
    validate_spec,
)
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.adaptive.spec import ColumnRule, ParserSpec
from synth_statements import (
    credit_suisse_profile,
    default_profile,
    generate_statement,
    jp_morgan_profile,
    make_spec,
)


def _mock_llm(monkeypatch, spec_or_raiser):
    """Make onboard_file's drafting step deterministic: a backend is 'present'
    and propose_spec returns a fixed spec (or raises)."""
    monkeypatch.setattr(agent_module, "drafting_backend", lambda: "api")
    if isinstance(spec_or_raiser, ParserSpec):
        monkeypatch.setattr(agent_module, "propose_spec",
                            lambda path, model="claude-opus-4-8": spec_or_raiser)
    else:
        monkeypatch.setattr(agent_module, "propose_spec", spec_or_raiser)


# --- what the model sees + what the engine can map -------------------------


def test_fingerprint_surfaces_unfamiliar_headers(tmp_path):
    """The onboarding agent can only map columns it is shown. The fingerprint
    must expose the odd header wording verbatim."""
    p, _ = generate_statement(credit_suisse_profile(), tmp_path / "cs.xlsx",
                              seed=1, n_rows=20)
    fp = fingerprint(p)
    headers = {c for cand in fp.header_candidates for c in cand["cells"]}
    for h in ("Deal Reference", "Share Number", "Valuation (USD)", "Right (P/C)"):
        assert h in headers, f"fingerprint did not surface {h!r}"


def test_engine_maps_creatively_named_columns(tmp_path):
    """Once a spec points at them, the engine locates unfamiliar headers -- even
    with extra noise columns and a shuffled order."""
    profile = credit_suisse_profile()
    spec = make_spec(profile)
    p, truth = generate_statement(profile, tmp_path / "cs.xlsx", seed=2,
                                  n_rows=25, extra_cols=6, shuffle=True)
    by = {m.trade_ref: m for m in run_spec(spec, p)}
    assert len(by) == 25
    for ref, t in truth.items():
        m = by[ref]
        assert m.extra.get("quantity") == pytest.approx(t["quantity"])   # "Share Number"
        assert m.mtm_local == pytest.approx(t["mtm_local"])              # "Valuation (USD)"
        assert m.extra.get("strike") == pytest.approx(t["strike"])       # "Strike Level"
        assert m.extra.get("put_call") == t["put_call"]                  # "Right (P/C)"
        assert m.currency == "USD"                                       # "Settlement Ccy"


def test_leaner_format_onboards(tmp_path):
    """JP Morgan's layout omits whole columns (no underlying-price, no trade
    date). It should still validate and parse; the absent fields are just None."""
    profile = jp_morgan_profile()
    spec = make_spec(profile)
    p, _ = generate_statement(profile, tmp_path / "jpm.xlsx", seed=8,
                              n_rows=30, extra_cols=5, shuffle=True)
    rep = validate_spec(spec, p)
    assert rep.ok, rep.issues
    marks = run_spec(spec, p)
    assert len(marks) == 30
    assert all(m.extra.get("underlying_price") is None for m in marks)
    assert all(m.extra.get("quantity") is not None for m in marks)


# --- the validation gate ----------------------------------------------------


def test_validate_accepts_good_new_broker_spec(tmp_path):
    profile = credit_suisse_profile()
    p, _ = generate_statement(profile, tmp_path / "cs.xlsx", seed=3, n_rows=30)
    rep = validate_spec(make_spec(profile), p)
    assert rep.ok, rep.issues
    assert rep.rows_parsed == 30
    assert rep.pct_with_quantity == 1.0


def test_missing_cell_values_still_parse(tmp_path):
    """A present-but-sparse column (many blank cells) parses fine: rows keep
    their ref + mtm, and the blanked field reads back as None."""
    profile = credit_suisse_profile()
    spec = make_spec(profile)
    p, truth = generate_statement(profile, tmp_path / "cs.xlsx", seed=6,
                                  n_rows=40, missing={"strike": 0.5, "quantity": 0.25})
    by = {m.trade_ref: m for m in run_spec(spec, p)}
    assert len(by) == 40  # rows still parse (ref + mtm intact)
    assert any(by[ref].extra.get("strike") is None for ref in truth)  # some blank
    for ref, t in truth.items():
        got = by[ref].extra.get("strike")
        assert got is None if t["strike"] is None else got == pytest.approx(t["strike"])


def test_validate_flags_barely_present_quantity(tmp_path):
    """A column that is mostly blank trips the low-coverage gate for review."""
    profile = credit_suisse_profile()
    p, _ = generate_statement(profile, tmp_path / "cs.xlsx", seed=7,
                              n_rows=40, missing={"quantity": 0.9})
    rep = validate_spec(make_spec(profile), p)
    assert not rep.ok
    assert any("quantity coverage" in i for i in rep.issues)


def test_validate_rejects_when_required_column_absent(tmp_path):
    """If the statement has no MTM column at all, the drafted spec can't locate
    it -- validation must fail (a required field can never be optional)."""
    profile = credit_suisse_profile()
    spec = make_spec(profile)  # still expects "Valuation (USD)"
    p, _ = generate_statement(profile, tmp_path / "cs.xlsx", seed=5,
                              n_rows=20, drop={"mtm_local"})
    rep = validate_spec(spec, p)
    assert not rep.ok
    assert any("not located" in i or "engine error" in i for i in rep.issues)


# --- the onboard_file plumbing (model mocked) ------------------------------


def test_onboard_new_broker_returns_proposed(tmp_path, monkeypatch):
    profile = credit_suisse_profile()
    _mock_llm(monkeypatch, make_spec(profile))
    p, _ = generate_statement(profile, tmp_path / "cs.xlsx", seed=4, n_rows=25)
    res = onboard_file(p)
    assert res.status == "proposed"
    assert res.broker == "Credit Suisse"
    assert res.validation.ok


def test_onboard_rejects_broken_draft(tmp_path, monkeypatch):
    profile = credit_suisse_profile()
    _mock_llm(monkeypatch, make_spec(profile))  # expects an MTM column...
    p, _ = generate_statement(profile, tmp_path / "cs.xlsx", seed=5,
                              n_rows=20, drop={"mtm_local"})  # ...that isn't there
    res = onboard_file(p)
    assert res.status == "rejected"
    assert res.validation is not None and res.validation.issues


def test_onboard_rejects_unknown_counterparty(tmp_path, monkeypatch):
    """A model that drafts a counterparty outside the closed Broker enum is
    rejected with actionable guidance (add it to schema.Broker)."""
    def _draft_unknown(path, model="claude-opus-4-8"):
        raise ValueError("1 validation error for ParserSpec: broker 'Zephyr Securities'")

    _mock_llm(monkeypatch, _draft_unknown)
    p, _ = generate_statement(default_profile(), tmp_path / "x.xlsx", seed=9, n_rows=10)
    res = onboard_file(p)
    assert res.status == "rejected"
    assert "schema.Broker" in res.message


def test_random_name_is_not_a_valid_broker():
    """Guards the closed-enum control directly: a spec for a random counterparty
    cannot even be constructed."""
    with pytest.raises(Exception):
        ParserSpec(
            broker="Zephyr Securities",
            header_anchor="Deal Ref",
            columns=[
                ColumnRule(field="trade_ref", header_contains="Deal Ref"),
                ColumnRule(field="mtm_local", header_contains="MTM", kind="number"),
            ],
        )


# --- opt-in: exercise the real onboarding model ----------------------------


@pytest.mark.skipif(not os.environ.get("RUN_LLM_ONBOARDING"),
                    reason="set RUN_LLM_ONBOARDING=1 to exercise the real onboarding LLM")
def test_live_onboarding_drafts_a_spec(tmp_path):
    if agent_module.drafting_backend() is None:
        pytest.skip("no drafting backend (ANTHROPIC_API_KEY or claude CLI)")
    profile = credit_suisse_profile()
    p, _ = generate_statement(profile, tmp_path / "cs.xlsx", seed=1, n_rows=25)
    res = onboard_file(p)
    assert res.status in ("proposed", "rejected")
    if res.status == "proposed":
        assert res.broker == "Credit Suisse"
        assert res.validation.rows_parsed > 0
