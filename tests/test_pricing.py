from derivative_pricing.parsers.base import apply_sign_convention
from derivative_pricing.pricing import excel_round, notional_local, price_per_unit, to_usd
from derivative_pricing.schema import Broker


def test_excel_round_half_away_from_zero():
    # Python's round(2.675, 2) gives 2.67 (banker's); Excel gives 2.68
    assert excel_round(2.675, 2) == 2.68
    assert excel_round(-2.675, 2) == -2.68
    assert excel_round(0.125, 2) == 0.13
    assert excel_round(1.005, 2) == 1.01


def test_notional_local_is_shares_times_price():
    # F1, values from workbook row 7: K7=5286.35 shares, price 181.391 -> G7
    assert abs(notional_local(5286.35, 181.391) - 958897.0) < 1.0


def test_to_usd_rounds_to_cents():
    assert to_usd(100.0, 1.1553) == 115.53
    assert to_usd(1000.456, 1.0) == 1000.46


def test_price_per_unit():
    # F5, workbook row 7: I7=6737.23 / K7=5286.35 -> L7=1.27446
    assert abs(price_per_unit(6737.23, 5286.35) - 1.27446) < 1e-4


def test_sign_convention():
    # F3: Barclays and Nomura report flipped sign; all others pass through
    assert apply_sign_convention(Broker.BARCLAYS, -6737.23) == 6737.23
    assert apply_sign_convention(Broker.NOMURA, -6733.531) == 6733.531
    assert apply_sign_convention(Broker.GOLDMAN_SACHS, -3818.02) == -3818.02
    assert apply_sign_convention(Broker.HSBC, 88203.1) == 88203.1