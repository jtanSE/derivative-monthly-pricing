"""Per-row anomaly flagging: catch a value that parsed fine but looks wrong."""

from derivative_pricing.anomaly import Cell, robust_outlier_mask, row_warnings


def test_lone_small_strike_is_flagged():
    # the user's example: one strike of 1.0 among ~5,000s
    vals = [5000.0, 5010.0, 4990.0, 5005.0, 4995.0, 5000.0, 1.0]
    mask = robust_outlier_mask(vals)
    assert mask[-1] is True
    assert not any(mask[:-1])


def test_uniform_values_do_not_flag():
    assert not any(robust_outlier_mask([5000.0, 5000.0, 5000.0, 5000.0, 5000.0]))
    # mild spread, still no outlier
    assert not any(robust_outlier_mask([100.0, 101.0, 99.0, 102.0, 98.0, 100.5]))


def test_hair_off_value_is_not_flagged():
    # ordinary dispersion is not an anomaly -- only magnitude breaks are
    assert not any(robust_outlier_mask([2000.0] * 6 + [2000.01]))
    # even a wide-but-legitimate ~20x spread within one underlying stays quiet
    assert not any(robust_outlier_mask([100.0, 500.0, 2000.0, 1500.0, 800.0, 108.0]))


def test_small_group_flags_magnitude_break():
    # 3 positives is enough to define a scale; a 5,000x break flags
    assert robust_outlier_mask([5000.0, 5100.0, 1.0]) == [False, False, True]
    assert robust_outlier_mask([5000.0, 5100.0, 4900.0]) == [False, False, False]


def test_negative_flags_but_zero_does_not():
    # a strictly negative strike is never right -> flag
    assert robust_outlier_mask([5000.0, 5000.0, 5000.0, -1.0])[-1] is True
    # zero is a common "no strike" convention -> do NOT flag (avoids noise)
    assert robust_outlier_mask([5000.0, 5000.0, 5000.0, 0.0])[-1] is False


def test_none_values_never_flag():
    mask = robust_outlier_mask([5000.0, None, 5010.0, None, 4990.0, 5000.0, None])
    assert mask[1] is False and mask[3] is False and mask[6] is False


def test_row_warnings_groups_by_index_scale():
    # SPX (~5,000) and RUT (~2,000) each internally clean: no cross-flagging
    cells = (
        [Cell(group="SPX", strike=5000.0 + i) for i in range(6)]
        + [Cell(group="RUT", strike=2000.0 + i) for i in range(6)]
    )
    assert all(w == [] for w in row_warnings(cells))


def test_row_warnings_flags_within_group_outlier():
    cells = [Cell(group="SPX", strike=5000.0) for _ in range(6)]
    cells.append(Cell(group="SPX", strike=1.0))
    warns = row_warnings(cells)
    assert warns[-1] and "Strike" in warns[-1][0]
    assert "median 5,000.00" in warns[-1][0]
    assert all(w == [] for w in warns[:-1])


def test_row_warnings_flags_negative_strike():
    cells = [Cell(group="SPX", strike=5000.0) for _ in range(5)]
    cells.append(Cell(group="SPX", strike=-3.0))
    assert "Strike -3.00 is negative" in " ".join(row_warnings(cells)[-1])


def test_row_warnings_ignores_repeated_zero_convention():
    # a whole class of trades reporting strike 0 is a convention, not anomalies
    cells = [Cell(group="G", strike=0.0) for _ in range(8)]
    assert all(w == [] for w in row_warnings(cells))


def test_price_is_not_screened():
    # per-option price varies by orders of magnitude legitimately -> never flagged
    # (Cell carries no price; strike is the only screened field)
    cells = [Cell(group="SPX", strike=5000.0) for _ in range(6)]
    assert all(w == [] for w in row_warnings(cells))


def test_row_warnings_reasons_are_deduped():
    cells = [Cell(group="G", strike=5000.0) for _ in range(6)]
    cells.append(Cell(group="G", strike=-2.0))
    reasons = row_warnings(cells)[-1]
    assert len(reasons) == len(set(reasons))
