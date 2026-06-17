"""Reconciliation engine: derived vs broker MTM, and control totals.

Tolerance thresholds are an open question (plan Q3) -- the framework takes
them as explicit parameters so no threshold is ever baked in silently.

Control totals replicate the workbook's verified subtotal semantics (F7):
- notional and shares subtotals are POSITIVE-ONLY sums (SUMIFS ... ">0");
  the business reason is plan Q8, but the behavior is replicated exactly.
- MTM subtotals are plain sums.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from derivative_pricing.schema import ReconStatus


@dataclass
class ReconResult:
    status: ReconStatus
    diff_local: float | None  # derived - broker
    tolerance_abs: float | None


def reconcile_mtm(
    mtm_derived_local: float | None,
    mtm_broker_local: float | None,
    tolerance_abs: float,
) -> ReconResult:
    if mtm_derived_local is None or mtm_broker_local is None:
        return ReconResult(ReconStatus.NOT_COMPARABLE, None, tolerance_abs)
    diff = mtm_derived_local - mtm_broker_local
    status = ReconStatus.OK if abs(diff) <= tolerance_abs else ReconStatus.OUT_OF_TOLERANCE
    return ReconResult(status, diff, tolerance_abs)


@dataclass
class SectionTotals:
    notional_local: float  # positive-only (F7)
    notional_usd: float  # positive-only (F7)
    shares: float  # positive-only (F7)
    mtm_local: float  # plain sum
    mtm_usd: float  # plain sum


def _pos_sum(values: Iterable[float | None]) -> float:
    return sum(v for v in values if v is not None and v > 0)


def _sum(values: Iterable[float | None]) -> float:
    return sum(v for v in values if v is not None)


def section_totals(rows) -> SectionTotals:
    """Per-broker control totals over objects exposing the canonical value
    fields (works for both CallOptionRow and CanonicalRecord)."""
    return SectionTotals(
        notional_local=_pos_sum(r.notional_local for r in rows),
        notional_usd=_pos_sum(r.notional_usd for r in rows),
        shares=_pos_sum(r.shares for r in rows),
        mtm_local=_sum(r.mtm_local for r in rows),
        mtm_usd=_sum(r.mtm_usd for r in rows),
    )