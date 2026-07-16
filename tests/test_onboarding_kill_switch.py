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
    load_spec,
    onboard_file,
    propose_spec,
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
