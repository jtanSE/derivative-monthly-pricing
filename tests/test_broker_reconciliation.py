"""Per-broker acceptance gate (plan section 7): the system's CALLS output must
reconcile to the analyst's ALIC section for every broker we parse.

MTM is the reliable acceptance figure (it comes straight from the broker, with
only a sign convention applied), so the gate is built on it:

  1. COVERAGE  -- every ALIC reference for the broker is parsed (nothing missing).
  2. PER-REF   -- per-reference MTM sums match ALIC to the cent for >= 98% of
                  references (a few genuine manual analyst overrides may differ).
  3. TOTAL     -- the section MTM total reconciles to ALIC within 0.1%.

References are compared as per-reference SUMS so BNP's MCB net-MTM lines (one
line vs ALIC's two decomposed legs) and BAML's double-legs reconcile uniformly.
ALIC's own section totals are read at test time -- nothing is hard-coded -- so
this stays a true reconciliation if the sanitized workbook is refreshed.

Both the ALIC workbook and the broker files are git-ignored; this test skips
cleanly when they are absent (e.g. in CI), and guards them locally.
"""

from collections import defaultdict
from pathlib import Path

import pytest

from derivative_pricing.adaptive.router import route
from derivative_pricing.output import build_rows_from_marks, split_marks_by_option_type
from derivative_pricing.schema import Broker
from derivative_pricing.workbook import read_call_options

REPO = Path(__file__).resolve().parents[1]

# (ALIC broker section, statement filename)
BROKER_FILES = [
    (Broker.BNP_PARIBAS, "BNP AUGUSTAR LIFE INSURANCE COMPANY_01 Apr 2026 (2).xls"),
    (Broker.BAML, "BAML 03-31-2026_Results_82654.xls"),
    (Broker.GOLDMAN_SACHS, "GS_Ohio National Life Ins Co (EQ)_20260331.xls"),
    (Broker.HSBC, "HSBC GV_AUGUSTAR LIFE INSURANCE COMPANY REG15_31Mar2026_070407.xlsx"),
]

MTM_REL_TOL = 0.001        # 0.1% on the section MTM total
MIN_PER_REF_PASS = 0.98    # allow a few genuine manual overrides (BNP: 269/272)


@pytest.fixture(scope="module")
def alic(workbook_path):
    return read_call_options(workbook_path)


def _mtm_by_ref(rows_or_marks, mtm_attr, ref_attr):
    out = defaultdict(float)
    for item in rows_or_marks:
        out[getattr(item, ref_attr)] += (getattr(item, mtm_attr) or 0)
    return out


@pytest.mark.parametrize(
    "broker,filename", BROKER_FILES, ids=[b.value for b, _ in BROKER_FILES]
)
def test_broker_calls_reconcile_to_alic(broker, filename, alic):
    path = REPO / filename
    if not path.exists():
        pytest.skip(f"statement not present: {filename}")

    result = route(path)
    assert result.status == "known", f"{filename} not identified (status={result.status})"

    calls, _puts = split_marks_by_option_type(result.marks)
    rows = build_rows_from_marks(calls, {"USD": 1.0})

    ours = _mtm_by_ref(rows, "mtm_local", "trade_reference")
    ref = _mtm_by_ref((r for r in alic.rows if r.broker == broker),
                      "mtm_local", "trade_ref")
    assert ref, f"no ALIC rows found for {broker.value}"

    # 1) COVERAGE: every ALIC reference is parsed.
    missing = set(ref) - set(ours)
    assert not missing, (
        f"{broker.value}: {len(missing)} ALIC refs not parsed: {sorted(missing)[:10]}"
    )

    # 2) PER-REF: MTM sums match ALIC to the cent for >= 98% of references.
    common = set(ours) & set(ref)
    to_cent = sum(1 for k in common if abs(ours[k] - ref[k]) <= 0.01)
    rate = to_cent / len(common)
    assert rate >= MIN_PER_REF_PASS, (
        f"{broker.value}: per-ref MTM only {to_cent}/{len(common)} to the cent "
        f"({rate:.1%}); overrides="
        f"{sorted(k for k in common if abs(ours[k] - ref[k]) > 0.01)[:10]}"
    )

    # 3) TOTAL: section MTM reconciles within 0.1% (manual overrides aside).
    our_total, alic_total = sum(ours.values()), sum(ref.values())
    assert abs(our_total - alic_total) <= MTM_REL_TOL * max(abs(alic_total), 1.0), (
        f"{broker.value}: MTM total {our_total:,.2f} vs ALIC {alic_total:,.2f} "
        f"(diff {our_total - alic_total:,.2f})"
    )
