"""
Projection tests.

The failure mode that matters here is under-predicting: a model that pushes the
overage date later than reality tells you that you are fine when you are not.
Two of these tests exist because earlier versions did exactly that.
"""

import numpy as np
import pandas as pd
import pytest

import fixtures as fx

WINDOWS = [30, 60, 90, 180]
HORIZON = 400
REMAINING = fx.CAPACITY_BALANCE


def mean_of(app, series, window, method):
    return app.project(series, window, HORIZON, method).mean()


# -- shape ------------------------------------------------------------------


def test_flat_average_is_constant(app):
    fc = app.project(fx.flat_series(1), 30, HORIZON, "Flat average")
    assert fc.nunique() == 1
    assert len(fc) == HORIZON


def test_seasonal_replay_repeats_the_window(app):
    fc = app.project(fx.flat_series(1), 30, HORIZON, "Seasonal replay")
    assert np.allclose(fc.iloc[:30].to_numpy(), fc.iloc[30:60].to_numpy())
    assert fc.nunique() > 1


def test_seasonal_replay_preserves_the_window_mean(app):
    series = fx.flat_series(1)
    flat = mean_of(app, series, 30, "Flat average")
    seasonal = mean_of(app, series, 30, "Seasonal replay")
    assert seasonal == pytest.approx(flat, rel=0.01)


def test_forecast_starts_the_day_after_history_ends(app):
    series = fx.flat_series(1)
    fc = app.project(series, 30, 10, "Flat average")
    assert fc.index[0] == max(series.index) + pd.Timedelta(days=1).to_pytimedelta()


# -- trend direction (regression) -------------------------------------------


@pytest.mark.parametrize("window", WINDOWS)
def test_trend_never_falls_below_flat_on_growing_data(app, window):
    """
    Regression: fitting a raw line to a short window with weekday seasonality
    produced a spurious negative slope, projecting $137/day against a true
    $217/day and moving the overage date a week later than reality.
    """
    series = fx.synthetic_series(7, 120, 260)
    assert mean_of(app, series, window, "Linear trend") >= mean_of(
        app, series, window, "Flat average"
    ) * 0.97


@pytest.mark.parametrize("window", WINDOWS)
def test_trend_is_below_flat_on_declining_data(app, window):
    series = fx.synthetic_series(3, 260, 120)
    assert mean_of(app, series, window, "Linear trend") < mean_of(
        app, series, window, "Flat average"
    )


def test_trend_detects_growth_given_a_long_enough_window(app):
    """30 days of a gentle ramp is genuinely not enough signal; 90 is."""
    series = fx.synthetic_series(7, 120, 260)
    ratio = mean_of(app, series, 90, "Linear trend") / mean_of(
        app, series, 90, "Flat average"
    )
    assert ratio > 1.1


# -- slope shrinkage (regression) -------------------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_flat_data_does_not_invent_a_trend(app, seed):
    """
    Regression: an unshrunk slope on flat, noisy data projected $290/day
    against a true $174/day. The slope is scaled by the fit's R², so a
    meaningless trend collapses back to the window average.
    """
    series = fx.flat_series(seed)
    ratio = mean_of(app, series, 30, "Linear trend") / mean_of(
        app, series, 30, "Flat average"
    )
    assert 0.75 < ratio < 1.3


@pytest.mark.parametrize("seed", range(6))
def test_trend_never_projects_negative_spend(app, seed):
    series = fx.synthetic_series(seed, 260, 120)
    fc = app.project(series, 30, 800, "Linear trend")
    assert (fc >= 0).all()


# -- overage date -----------------------------------------------------------


@pytest.mark.parametrize("method", ["Flat average", "Seasonal replay", "Linear trend"])
def test_overage_date_is_after_the_history(app, method):
    series = fx.flat_series(1)
    fc = app.project(series, 30, HORIZON, method)
    found = app.overage_date(fc, REMAINING)
    assert found is not None
    assert found > max(series.index)


def test_overage_date_is_where_cumulative_crosses_the_balance(app):
    series = fx.flat_series(1)
    fc = app.project(series, 30, HORIZON, "Flat average")
    found = app.overage_date(fc, REMAINING)
    cumulative = fc.cumsum()
    assert cumulative.loc[found] > REMAINING
    earlier = cumulative[cumulative.index < found]
    assert earlier.empty or earlier.iloc[-1] <= REMAINING


def test_a_smaller_balance_runs_out_sooner(app):
    series = fx.flat_series(1)
    fc = app.project(series, 30, HORIZON, "Flat average")
    assert app.overage_date(fc, REMAINING / 4) < app.overage_date(fc, REMAINING)


def test_no_overage_when_nothing_is_left(app):
    fc = app.project(fx.flat_series(1), 30, 50, "Flat average")
    assert app.overage_date(fc, 0) is None
    assert app.overage_date(fc, -5) is None


def test_no_overage_when_the_balance_outlasts_the_horizon(app):
    fc = app.project(fx.flat_series(1), 30, 50, "Flat average")
    assert app.overage_date(fc, 1e9) is None


# -- guards -----------------------------------------------------------------


def test_empty_history_yields_empty_forecast(app):
    assert app.project(pd.Series(dtype=float), 30, 10, "Flat average").empty


def test_window_longer_than_history_is_allowed(app):
    assert not app.project(fx.flat_series(1), 9999, 10, "Linear trend").empty


def test_non_positive_horizon_yields_empty_forecast(app):
    assert app.project(fx.flat_series(1), 30, 0, "Flat average").empty


def test_daily_series_gap_fills_with_zeros(app):
    """A day with no usage row is zero spend, not a missing point."""
    usage = fx.usage_rows()
    usage = usage[usage["usage_date"] != fx.AS_OF.replace(day=1)]
    series = app.daily_series(usage)
    expected = (max(series.index) - min(series.index)).days + 1
    assert len(series) == expected


def test_daily_series_excludes_rebates(app):
    """A rebate is a credit, not consumption, and must not inflate the run rate."""
    usage = fx.usage_rows()
    baseline = app.daily_series(usage).sum()
    with_rebate = pd.concat(
        [
            usage,
            pd.DataFrame(
                [
                    dict(
                        usage_date=fx.AS_OF,
                        balance_source="rebate",
                        currency="USD",
                        spend=5_000.0,
                    )
                ]
            ),
        ]
    )
    assert app.daily_series(with_rebate).sum() == pytest.approx(baseline)
