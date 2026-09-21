"""Kill switch for LLM-assisted onboarding (AI_ONBOARDING_ENABLED).

The deterministic monthly pipeline never calls the model, so this flag only
gates propose_spec / onboard_file. These tests make no network calls: the
kill-switch check runs before any API key or model use, and the router is
stubbed so the unknown-format path is exercised offline.
"""

from pathlib import Path

import pytest

from derivative_pricing.adaptive import agent
from derivative_pricing.adaptive.agent import (
    SPECS_DIR,
    ai_onboarding_enabled,
    budget_status,
    load_spec,
    load_usage_totals,
    onboard_file,
    onboarding_budget_usd,
    propose_spec,
    record_usage,
)


def test_flag_parsing():
    # unset or empty -> enabled (container default keeps onboarding available)
    assert ai_onboarding_enabled({}) is True
    assert ai_onboarding_enabled({"AI_ONBOARDING_ENABLED": ""}) is True
    # explicit truthy values -> enabled
    for v in ("true", "1", "yes", "on", "TRUE", "  On  "):
        assert ai_onboarding_enabled({"AI_ONBOARDING_ENABLED": v}) is True
    # explicit falsey values -> disabled
    for v in ("false", "0", "no", "off", "FALSE", " Off "):
        assert ai_onboarding_enabled({"AI_ONBOARDING_ENABLED": v}) is False


def test_propose_spec_refuses_when_disabled_even_with_key(monkeypatch):
    # Kill switch must win over a present API key, before any model call.
    monkeypatch.setenv("AI_ONBOARDING_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    with pytest.raises(RuntimeError, match="disabled"):
        propose_spec(Path("does-not-matter.xls"))


def test_onboard_file_disabled_on_unknown_format(monkeypatch):
    monkeypatch.setenv("AI_ONBOARDING_ENABLED", "off")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    # Stub the router so the file is treated as an unknown format, offline.
    monkeypatch.setattr("derivative_pricing.adaptive.router.identify", lambda p: [])

    res = onboard_file(Path("unknown.xls"))
    assert res.status == "disabled"
    assert "AI_ONBOARDING_ENABLED" in res.message


def test_known_format_still_works_when_disabled(monkeypatch):
    # Deterministic parsing of an already-known broker must be unaffected by the
    # kill switch -- onboard_file returns "already_known" without any model path.
    monkeypatch.setenv("AI_ONBOARDING_ENABLED", "false")
    known_spec = load_spec(SPECS_DIR / "bnp.json")
    monkeypatch.setattr("derivative_pricing.adaptive.router.identify",
                        lambda p: [known_spec])

    res = onboard_file(Path("bnp-like.xls"))
    assert res.status == "already_known"
    assert res.broker == known_spec.broker.value


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("AI_ONBOARDING_ENABLED", raising=False)
    assert ai_onboarding_enabled() is True


# --- Spend cap (AI_ONBOARDING_BUDGET_USD) ---------------------------------

def test_budget_parsing():
    # unset / blank / non-numeric / <= 0 -> no cap
    assert onboarding_budget_usd({}) is None
    assert onboarding_budget_usd({"AI_ONBOARDING_BUDGET_USD": ""}) is None
    assert onboarding_budget_usd({"AI_ONBOARDING_BUDGET_USD": "abc"}) is None
    assert onboarding_budget_usd({"AI_ONBOARDING_BUDGET_USD": "0"}) is None
    assert onboarding_budget_usd({"AI_ONBOARDING_BUDGET_USD": "-5"}) is None
    # positive figure -> that cap
    assert onboarding_budget_usd({"AI_ONBOARDING_BUDGET_USD": "12.50"}) == 12.50


def test_ledger_roundtrip_and_atomic_write(tmp_path):
    # record_usage folds spend into the ledger; a zero-token usage is a no-op.
    p = tmp_path / "usage.json"
    assert load_usage_totals(p) == {"total_tokens": 0, "total_cost_usd": 0.0}
    record_usage({"total_tokens": 100, "cost_usd": 0.25}, path=p)
    record_usage({"total_tokens": 50, "cost_usd": 0.10}, path=p)
    record_usage(None, path=p)  # no-op
    record_usage({"total_tokens": 0, "cost_usd": 0.0}, path=p)  # no-op
    totals = load_usage_totals(p)
    assert totals["total_tokens"] == 150
    assert round(totals["total_cost_usd"], 2) == 0.35
    # no stray temp file left behind by the atomic write
    assert not (tmp_path / "usage.json.tmp").exists()


def test_budget_status_uncapped_and_exhausted(tmp_path):
    p = tmp_path / "usage.json"
    record_usage({"total_tokens": 10, "cost_usd": 9.99}, path=p)
    # no cap configured -> never exhausted
    assert budget_status({}, p) == (False, None, 9.99)
    # under cap -> not exhausted
    assert budget_status({"AI_ONBOARDING_BUDGET_USD": "10"}, p)[0] is False
    # at/over cap -> exhausted
    exhausted, cap, spent = budget_status({"AI_ONBOARDING_BUDGET_USD": "9.99"}, p)
    assert exhausted is True and cap == 9.99 and spent == 9.99


def test_propose_spec_refuses_when_budget_exhausted(monkeypatch, tmp_path):
    # Budget must block the model call before any drafting, even with a key.
    p = tmp_path / "usage.json"
    record_usage({"total_tokens": 10, "cost_usd": 5.00}, path=p)
    monkeypatch.setattr(agent, "USAGE_TOTALS_PATH", p)
    monkeypatch.delenv("AI_ONBOARDING_ENABLED", raising=False)
    monkeypatch.setenv("AI_ONBOARDING_BUDGET_USD", "5.00")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    with pytest.raises(RuntimeError, match="budget"):
        propose_spec(Path("does-not-matter.xls"))


def test_onboard_file_budget_exhausted_on_unknown_format(monkeypatch, tmp_path):
    p = tmp_path / "usage.json"
    record_usage({"total_tokens": 10, "cost_usd": 5.00}, path=p)
    monkeypatch.setattr(agent, "USAGE_TOTALS_PATH", p)
    monkeypatch.delenv("AI_ONBOARDING_ENABLED", raising=False)
    monkeypatch.setenv("AI_ONBOARDING_BUDGET_USD", "5.00")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    monkeypatch.setattr("derivative_pricing.adaptive.router.identify", lambda p: [])

    res = onboard_file(Path("unknown.xls"))
    assert res.status == "budget_exhausted"
    assert "AI_ONBOARDING_BUDGET_USD" in res.message
