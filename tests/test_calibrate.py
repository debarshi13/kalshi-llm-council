from council.trading.calibrate import brier, extremize


def test_extremize_pushes_away_from_half():
    assert extremize(0.70, 1.3) > 0.70
    assert extremize(0.30, 1.3) < 0.30


def test_extremize_fixed_points_and_identity():
    assert extremize(0.5, 1.3) == 0.5
    assert abs(extremize(0.7, 1.0) - 0.7) < 1e-9


def test_extremize_clamps_extremes():
    assert 0.0 < extremize(0.999, 2.0) < 1.0
    assert 0.0 < extremize(0.001, 2.0) < 1.0


def test_extremize_extreme_alpha_stays_in_open_interval():
    assert 0.0 < extremize(0.999999, 10.0) < 1.0
    assert 0.0 < extremize(1e-6, 100.0) < 1.0      # must not raise OverflowError
    assert 0.0 < extremize(0.9, 1000.0) < 1.0
    assert 0.0 < extremize(0.1, 1000.0) < 1.0


def test_brier():
    assert brier(0.8, 1) == round((0.8 - 1) ** 2, 10)
    assert brier(0.8, 0) == round(0.8 ** 2, 10)
