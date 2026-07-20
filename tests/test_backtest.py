"""The column-discovery / backtest harness.

Self-validating: point it at the March ALIC (confirmed) + the BNP statement and
it must reverse-engineer BNP's *known* column mappings with no prior knowledge.
If it can rediscover columns we already know, we trust it to discover unknown
ones (e.g. Nomura shares -> exchangedNotional1Amount).

Skips cleanly when the git-ignored files are absent (CI); guards them locally.
"""

import pytest

from derivative_pricing.backtest import (
    confirmed_values,
    discover_field_source,
    spec_for_broker,
)
from derivative_pricing.schema import Broker


def _top(workbook_path, statement_path, field):
    spec = spec_for_broker(Broker.BNP_PARIBAS)
    conf = confirmed_values(workbook_path, Broker.BNP_PARIBAS, field)
    matches = discover_field_source(statement_path, spec, conf)
    assert matches, f"no candidate columns for {field}"
    return matches[0]


def test_spec_for_broker():
    assert spec_for_broker(Broker.BNP_PARIBAS).broker == Broker.BNP_PARIBAS
    with pytest.raises(KeyError):
        spec_for_broker(Broker.WELLS_FARGO)  # no spec onboarded


def test_discovers_bnp_shares_column(workbook_path, bnp_statement_path):
    top = _top(workbook_path, bnp_statement_path, "shares")
    assert "number of options" in top.header.casefold()
    assert top.match_rate >= 0.90
    assert top.mode == "abs"  # ALIC shares are signed; the column is a magnitude


def test_discovers_bnp_strike_column(workbook_path, bnp_statement_path):
    top = _top(workbook_path, bnp_statement_path, "strike")
    assert "strike" in top.header.casefold()
    assert top.match_rate >= 0.95


def test_discovers_bnp_mtm_column(workbook_path, bnp_statement_path):
    top = _top(workbook_path, bnp_statement_path, "mtm")
    assert "mark to market" in top.header.casefold()
    assert top.match_rate >= 0.90


def test_derived_notional_matches_no_single_column(workbook_path, bnp_statement_path):
    # ALIC computes notional = shares x index price, so NO raw statement column
    # should reproduce it -- the discovery must not fabricate a false source.
    top = _top(workbook_path, bnp_statement_path, "notional")
    assert top.match_rate < 0.5


def test_confirmed_values_keys_by_ref(workbook_path):
    conf = confirmed_values(workbook_path, Broker.BNP_PARIBAS, "mtm")
    assert conf, "no confirmed BNP mtm values read"
    # every entry is a list (double-listed refs keep all their values)
    assert all(isinstance(v, list) and v for v in conf.values())
