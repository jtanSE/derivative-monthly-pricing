"""Cross-time regression: every backtestable past month must still reconcile.

A spec that reproduced the analyst's confirmed file in March can silently break
in April when a broker tweaks its statement layout. This gate re-runs the
current specs against every confirmed month we have and asserts each broker's
calls still reconcile to the confirmed Call Options sheet (per-reference MTM, to
the cent, allowing the known handful of manual overrides).

Months come from backtest.discover_backtest_months(): the built-in March 2026
sample (repo root) plus any data/backtests/<as-of>/ folder. All files are
git-ignored, so this skips cleanly in CI and guards them locally. To add a new
month, drop that month's confirmed ALIC file + its broker statements into
data/backtests/<as-of>/ and this test picks it up automatically.
"""

import pytest

from derivative_pricing.adaptive.router import route
from derivative_pricing.backtest import (
    discover_backtest_months,
    reconcile_calls_to_confirmed,
)

MIN_PER_REF_PASS = 0.95  # allows the few genuine analyst overrides (BNP: 269/272)

_MONTHS = discover_backtest_months()


@pytest.mark.parametrize("month", _MONTHS or [None],
                         ids=[m.as_of for m in _MONTHS] or ["no-months"])
def test_month_reconciles(month):
    if month is None:
        pytest.skip("no backtest months present (confirmed file + statements)")

    reconciled_brokers = []
    for stmt in month.statements:
        r = route(stmt)
        if r.status != "known":
            continue
        report = reconcile_calls_to_confirmed(month.confirmed, stmt, r.spec.broker)
        if report.common == 0:
            continue  # broker not in this confirmed file (e.g. puts-only)
        reconciled_brokers.append(report.broker)
        assert report.rate >= MIN_PER_REF_PASS, (
            f"{month.as_of} {report.broker}: only {report.to_cent}/{report.common} "
            f"refs to the cent ({report.rate:.1%}); overrides={report.overrides[:8]} "
            f"missing={report.missing[:8]}"
        )

    assert reconciled_brokers, f"{month.as_of}: no broker reconciled against the confirmed file"
