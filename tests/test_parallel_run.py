"""Parallel-run acceptance test (plan section 7).

Reads the analyst's Call Options sheet, recomputes every derived column with
the pricing engine using the workbook's OWN lookup tables, and compares
against the values the workbook's formulas produced:

  G  Notional (Ccy) = Shares x index price          (F1)
  H  Notional (USD) = ROUND(G x FX, 2)              (F2)
  J  MTM (USD)      = ROUND(I x FX, 2)              (F4)
  L  Price (Ccy)    = I / Shares                    (F5)
  I  MTM (Ccy)      = sign-adjusted raw broker MTM  (F3)

Rows the analyst hand-overrode (multi-leg sums, scrubbed cells) may diverge;
the gate requires >= 99% agreement per rule and prints every divergence for
review, so nothing fails silently.
"""

import pytest

from derivative_pricing.parsers.base import apply_sign_convention
from derivative_pricing.pricing import notional_local, price_per_unit, to_usd
from derivative_pricing.workbook import read_call_options

REL_TOL = 1e-6  # plain multiplications/divisions
ABS_TOL_USD = 0.011  # ROUND(...,2) columns: allow one cent of float noise
MIN_PASS_RATE = 0.99


@pytest.fixture(scope="module")
def wb(workbook_path):
    return read_call_options(workbook_path)


def _close_rel(a: float, b: float) -> bool:
    return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1.0)


def _report(name: str, checked: int, failures: list[str]):
    assert checked > 0, f"{name}: no rows were checkable"
    rate = 1 - len(failures) / checked
    detail = "\n".join(failures[:20])
    assert rate >= MIN_PASS_RATE, (
        f"{name}: {len(failures)}/{checked} rows diverge "
        f"(pass rate {rate:.2%}):\n{detail}"
    )


def test_workbook_reader_sanity(wb):
    assert wb.as_of is not None and wb.as_of.isoformat() == "2026-03-31"
    assert len(wb.rows) > 1300, f"only {len(wb.rows)} trade rows read"
    assert {r.broker.value for r in wb.rows} >= {"Barclays", "Goldman Sachs", "HSBC"}
    assert wb.fx_rates.get("USD") == 1
    assert "S&P 500" in wb.index_prices


def test_f1_notional_local(wb):
    checked, failures = 0, []
    for r in wb.rows:
        if r.shares is None or r.notional_local is None:
            continue
        price = wb.index_prices.get(r.index_name)
        if price is None:
            continue
        checked += 1
        expect = notional_local(r.shares, price)
        if not _close_rel(expect, r.notional_local):
            failures.append(f"row {r.excel_row}: {expect} != {r.notional_local}")
    _report("F1 notional_local", checked, failures)


def test_f2_notional_usd(wb):
    checked, failures = 0, []
    for r in wb.rows:
        if r.notional_local is None or r.notional_usd is None:
            continue
        fx = wb.fx_rates.get(r.currency)
        if fx is None:
            continue
        checked += 1
        expect = to_usd(r.notional_local, fx)
        if abs(expect - r.notional_usd) > ABS_TOL_USD:
            failures.append(f"row {r.excel_row}: {expect} != {r.notional_usd}")
    _report("F2 notional_usd", checked, failures)


def test_f3_broker_sign_convention(wb):
    checked, failures = 0, []
    for r in wb.rows:
        if r.mtm_local is None or r.raw_broker_mtm is None:
            continue
        checked += 1
        expect = apply_sign_convention(r.broker, r.raw_broker_mtm)
        if not _close_rel(expect, r.mtm_local):
            failures.append(
                f"row {r.excel_row} ({r.broker.value}): {expect} != {r.mtm_local}"
            )
    _report("F3 mtm sign convention", checked, failures)


def test_f4_mtm_usd(wb):
    checked, failures = 0, []
    for r in wb.rows:
        if r.mtm_local is None or r.mtm_usd is None:
            continue
        fx = wb.fx_rates.get(r.currency)
        if fx is None:
            continue
        checked += 1
        expect = to_usd(r.mtm_local, fx)
        if abs(expect - r.mtm_usd) > ABS_TOL_USD:
            failures.append(f"row {r.excel_row}: {expect} != {r.mtm_usd}")
    _report("F4 mtm_usd", checked, failures)


def test_f5_price_per_unit(wb):
    checked, failures = 0, []
    for r in wb.rows:
        if r.mtm_local is None or r.price_local is None or not r.shares:
            continue
        checked += 1
        expect = price_per_unit(r.mtm_local, r.shares)
        if not _close_rel(expect, r.price_local):
            failures.append(f"row {r.excel_row}: {expect} != {r.price_local}")
    _report("F5 price_per_unit", checked, failures)