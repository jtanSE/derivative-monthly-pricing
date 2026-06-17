"""Pricing engine: deterministic re-implementation of the workbook formulas.

Rule numbers (F1-F5) refer to SYSTEMIMPLEMENTATION.md section 1.3, extracted
verbatim from the Lead Operations Analyst's workbook:

  F1  Notional (Ccy) = Shares x month-end index price
      workbook: G7 =+K7*VLOOKUP(B7,$W$8:$X$888,2,FALSE)
  F2  Notional (USD) = ROUND(Notional Ccy x FX rate, 2)
      workbook: H7 =ROUND(+G7*VLOOKUP($F7,$T$8:$U$50,2,FALSE),2)
  F4  MTM (USD) = ROUND(MTM Ccy x FX rate, 2)   (same shape as F2)
  F5  Price (Ccy) = MTM (Ccy) / Shares
      workbook: L7 =+I7/K7

F3 (broker sign convention) lives in parsers.base, next to the parser layer,
because it is a property of how each broker reports, not of the math.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def excel_round(value: float, digits: int = 2) -> float:
    """Excel ROUND(): ties round away from zero.

    Python's built-in round() uses banker's rounding, which diverges from
    Excel on .5 ties; the parallel-run acceptance gate requires Excel
    semantics exactly.
    """
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def notional_local(shares: float, index_price: float) -> float:
    """F1: Notional (Ccy) = Shares x month-end index price."""
    return shares * index_price


def to_usd(amount_local: float, fx_rate_to_usd: float) -> float:
    """F2 / F4: USD value = ROUND(local amount x FX rate, 2)."""
    return excel_round(amount_local * fx_rate_to_usd, 2)


def price_per_unit(mtm_local: float, shares: float) -> float:
    """F5: normalized per-unit price = MTM (Ccy) / Shares.

    Raises ZeroDivisionError for zero shares; callers must treat that row as
    an exception, never emit a silent placeholder.
    """
    return mtm_local / shares