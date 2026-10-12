"""
Tile rendering.

The third tile reports the projected position at contract end. It used to clamp
at zero, so an organization heading for an *underage* saw "Projected Net Overage
$0" and had no way to see how much capacity it was on course to leave unused.
The figure is now signed, and the label follows the sign.
"""

from datetime import date

import pytest

import fixtures as fx


class Recorder:
    """Stands in for a Streamlit column and records what was written to it."""

    def __init__(self):
        self.metrics = []

    def metric(self, label, value, delta=None, **kwargs):
        self.metrics.append((label, value, delta))


@pytest.fixture
def tiles(app, monkeypatch):
    """Render the tiles and hand back the (label, value, delta) of each."""

    def render(projected_net, proj_date=None, **overrides):
        columns = [Recorder() for _ in range(5)]
        monkeypatch.setattr(app.st, "columns", lambda *a, **k: columns)
        app.render_tiles(position(app, **overrides), projected_net, proj_date)
        return [c.metrics[0] for c in columns]

    return render


def position(app, **overrides):
    values = dict(
        as_of=fx.AS_OF,
        currency="USD",
        contract_number=fx.CONTRACT,
        contract_start=fx.CONTRACT_START,
        contract_end=fx.CONTRACT_END,
        capacity_purchased=fx.CAPACITY_PURCHASED,
        free_usage_granted=0.0,
        rollover_granted=fx.ROLLOVER_GRANTED,
        adjustments=0.0,
        total_capacity=fx.TOTAL_CAPACITY,
        used_to_date=fx.USED_TO_DATE,
        remaining=fx.CAPACITY_BALANCE,
        overage_to_date=0.0,
        marketplace_remaining=fx.MARKETPLACE,
    )
    values.update(overrides)
    return app.CapacityPosition(**values)


def net_tile(rows):
    return rows[2]


def test_overage_keeps_its_label_and_sign(tiles):
    label, value, _ = net_tile(tiles(1_636.0, proj_date=date(2026, 12, 20)))
    assert label == "Projected Net Overage"
    assert value == "$1,636"


def test_underage_flips_the_label_and_shows_a_negative(tiles):
    label, value, _ = net_tile(tiles(-8_750.0))
    assert label == "Projected Net Underage"
    assert value == "$-8,750"


def test_exactly_zero_reads_as_an_overage_of_nothing(tiles):
    label, value, _ = net_tile(tiles(0.0))
    assert label == "Projected Net Overage"
    assert value == "$0"


def test_the_underage_is_not_clamped_away(tiles):
    """The regression: a large underage must not render as $0."""
    _, value, _ = net_tile(tiles(-42_000.0))
    assert value != "$0"
    assert value == "$-42,000"


def test_the_contract_end_subtitle_is_unchanged(tiles):
    _, _, delta = net_tile(tiles(-8_750.0))
    assert delta == f"ContractEnd: {fx.CONTRACT_END:%m/%d/%Y}"


def test_a_non_usd_underage_uses_the_right_symbol(tiles):
    _, value, _ = net_tile(tiles(-8_750.0, currency="EUR"))
    assert value == "€-8,750"


# -- days to overage ---------------------------------------------------------
#
# Regression: the forecast runs 400 days past contract end so that a zero-balance
# date always exists to quote. The tile treated any such date as an overage
# "within contract term" and counted down to it, so an account comfortably under
# its commitment saw "177 days / Within contract term" while the same dashboard
# reported no overage. A date past contract end is not an overage on this
# contract.


def test_days_to_overage_counts_down_to_a_date_inside_the_term(tiles):
    label, value, delta = tiles(1_636.0, proj_date=date(2026, 12, 20))[3]
    assert label == "Days to Overage"
    assert value == f"{(date(2026, 12, 20) - fx.AS_OF).days} days"
    assert delta == "Within contract term"


def test_a_date_past_contract_end_is_not_counted_as_an_overage(tiles):
    _, value, delta = tiles(-8_750.0, proj_date=date(2027, 4, 7))[3]
    assert value == "None in term"
    assert delta == "At this rate: 04/07/2027"


def test_the_last_day_of_the_term_still_counts(tiles):
    _, value, delta = tiles(10.0, proj_date=fx.CONTRACT_END)[3]
    assert value.endswith("days")
    assert delta == "Within contract term"


def test_no_projected_date_at_all(tiles):
    _, value, delta = tiles(-8_750.0, proj_date=None)[3]
    assert value == "None projected"
    assert delta == "Capacity sufficient"
