"""Puts acceptance gate.

The calls gate (test_broker_reconciliation) reconciles against the ALIC Call
Options sheet, which is calls-only. Puts are cross-checked against the
workbook's consolidated **Marks** tab instead -- an independent per-broker
(trade-ref, MTM) consolidation the analyst keyed from the raw statements, which
(unlike the Call Options sheet) can include puts.

Current sample reality: of 7 puts, only the 1 Goldman put was carried into the
Marks tab -- and it reconciles to the cent. The 6 BNP MCB puts were excluded by
the analyst, so they have no independent reference yet. The gate therefore:
  (a) requires every put the analyst DID record in Marks to match our parse to
      the cent (sign convention applied), and
  (b) reports puts with no Marks reference as an explicit, documented gap
      rather than silently passing.

Both the ALIC workbook and the broker files are git-ignored; the tests skip
cleanly when they are absent (CI) and guard them locally.
"""

from collections import defaultdict
from pathlib import Path

import pytest

from derivative_pricing.adaptive.router import route
from derivative_pricing.output import split_marks_by_option_type
from derivative_pricing.parsers.base import apply_sign_convention
from derivative_pricing.parsers.marks_sheet import read_marks_sheet
from derivative_pricing.schema import Broker

REPO = Path(__file__).resolve().parents[1]

# (ALIC broker section, statement filename)
BROKER_FILES = [
    (Broker.BNP_PARIBAS, "data/2026-03/BNP AUGUSTAR LIFE INSURANCE COMPANY_01 Apr 2026 (2).xls"),
    (Broker.BAML, "data/2026-03/BAML 03-31-2026_Results_82654.xls"),
    (Broker.GOLDMAN_SACHS, "data/2026-03/GS_Ohio National Life Ins Co (EQ)_20260331.xls"),
    (Broker.HSBC, "data/2026-03/HSBC GV_AUGUSTAR LIFE INSURANCE COMPANY REG15_31Mar2026_070407.xlsx"),
]


@pytest.fixture(scope="module")
def marks_by_broker(workbook_path):
    """broker -> {trade_ref: MTM} from the analyst's consolidated Marks tab."""
    out: dict[Broker, dict[str, float]] = defaultdict(dict)
    for m in read_marks_sheet(workbook_path):
        out[m.broker][m.trade_ref] = m.mtm_local
    return out


def _puts(path: Path):
    _calls, puts = split_marks_by_option_type(route(path).marks)
    return puts


@pytest.mark.parametrize(
    "broker,filename", BROKER_FILES, ids=[b.value for b, _ in BROKER_FILES]
)
def test_puts_present_in_marks_reconcile_to_the_cent(broker, filename, marks_by_broker):
    path = REPO / filename
    if not path.exists():
        pytest.skip(f"statement not present: {filename}")
    puts = _puts(path)
    if not puts:
        pytest.skip(f"{broker.value}: no puts in this statement")

    ref = marks_by_broker.get(broker, {})
    present = {p.trade_ref: p for p in puts if p.trade_ref in ref}
    # Every put the analyst recorded in Marks must match our parse to the cent.
    for r, p in present.items():
        ours = apply_sign_convention(p.broker, p.mtm_local)
        assert abs(ours - ref[r]) <= 0.01, (
            f"{broker.value} put {r}: ours {ours:,.2f} vs Marks {ref[r]:,.2f}"
        )

    # Puts with no Marks reference are a documented gap, not a silent pass.
    unverified = [p.trade_ref for p in puts if p.trade_ref not in ref]
    if unverified:
        print(f"\n{broker.value}: {len(unverified)} put(s) have no Marks reference "
              f"(analyst excluded them): {unverified}")


def test_framework_reconciles_at_least_one_put(marks_by_broker):
    """Guard the cross-check itself: across all brokers, at least one put must be
    present in the Marks tab AND reconcile to the cent -- otherwise the puts gate
    has silently stopped matching anything and only its skips would remain."""
    if not any((REPO / f).exists() for _, f in BROKER_FILES):
        pytest.skip("no broker statements present")

    matched = 0
    for broker, filename in BROKER_FILES:
        path = REPO / filename
        if not path.exists():
            continue
        ref = marks_by_broker.get(broker, {})
        for p in _puts(path):
            if p.trade_ref in ref:
                ours = apply_sign_convention(p.broker, p.mtm_local)
                if abs(ours - ref[p.trade_ref]) <= 0.01:
                    matched += 1
    assert matched >= 1, "no put reconciled against the Marks tab"
