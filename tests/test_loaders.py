"""
Loader tests.

These cover the two assumptions that hold in a single-contract, single-currency
organization and break everywhere else:

  * REMAINING_BALANCE_DAILY has a row per contract, so "newest row in the view"
    returns an arbitrary contract once there is more than one.
  * USAGE_IN_CURRENCY_DAILY has a row per currency, so summing across them adds
    unlike units.

A fake session records the SQL instead of running it, so the filters can be
asserted without a Snowflake connection.
"""

from datetime import date

import pandas as pd
import pytest

import fixtures as fx


class FakeResult:
    def __init__(self, frame):
        self._frame = frame

    def to_pandas(self):
        return self._frame.copy()


class FakeSession:
    """Records every query and returns the queued frames in order."""

    def __init__(self, *frames):
        self.queries = []
        self._frames = list(frames)

    def sql(self, query):
        self.queries.append(" ".join(query.split()))
        frame = self._frames.pop(0) if self._frames else pd.DataFrame()
        return FakeResult(frame)


@pytest.fixture
def fake(app, monkeypatch):
    """Install a fake session and hand back a factory for it."""

    def install(*frames):
        session = FakeSession(*frames)
        monkeypatch.setattr(app, "get_active_session", lambda: session)
        return session

    return install


def upper(df):
    """Snowflake returns uppercase column names; mimic that."""
    out = df.copy()
    out.columns = [c.upper() for c in out.columns]
    return out


# -- balance: scoped to one contract ----------------------------------------


def test_balance_is_filtered_to_the_given_contract(app, fake):
    session = fake(upper(fx.balance_row()))
    app.load_latest_balance(fx.CONTRACT)
    assert len(session.queries) == 1
    assert f"contract_number = '{fx.CONTRACT}'" in session.queries[0]


def test_balance_falls_back_when_the_scoped_query_is_empty(app, fake):
    """Some organizations leave contract_number NULL here."""
    session = fake(pd.DataFrame(), upper(fx.balance_row()))
    result = app.load_latest_balance(fx.CONTRACT)
    assert len(session.queries) == 2
    assert "contract_number =" in session.queries[0]
    assert "contract_number =" not in session.queries[1]
    assert not result.empty


def test_balance_skips_the_filter_for_an_unsafe_contract_number(app, fake):
    session = fake(upper(fx.balance_row()))
    app.load_latest_balance("'; drop table t --")
    assert len(session.queries) == 1
    assert "drop table" not in session.queries[0]


def test_balance_columns_are_lowercased(app, fake):
    fake(upper(fx.balance_row()))
    result = app.load_latest_balance(fx.CONTRACT)
    assert "capacity_balance" in result.columns


# -- usage: scoped to one currency ------------------------------------------


def test_usage_is_filtered_to_one_currency(app, fake):
    session = fake(upper(fx.usage_rows("EUR")))
    app.load_daily_usage(fx.CONTRACT_START, "EUR")
    assert "currency = 'EUR'" in session.queries[0]


def test_usage_starts_at_the_contract_start_date(app, fake):
    session = fake(upper(fx.usage_rows()))
    app.load_daily_usage(date(2024, 12, 13), "USD")
    assert "usage_date >= '2024-12-13'" in session.queries[0]


def test_usage_omits_the_currency_filter_when_not_given(app, fake):
    session = fake(upper(fx.usage_rows()))
    app.load_daily_usage(fx.CONTRACT_START, None)
    assert "currency =" not in session.queries[0]


def test_usage_normalises_types(app, fake):
    fake(upper(fx.usage_rows()))
    result = app.load_daily_usage(fx.CONTRACT_START, "USD")
    assert isinstance(result["usage_date"].iloc[0], date)
    assert result["balance_source"].str.islower().all()


def test_usage_handles_an_empty_result(app, fake):
    fake(pd.DataFrame())
    assert app.load_daily_usage(fx.CONTRACT_START, "USD").empty


# -- the bug these fixes prevent --------------------------------------------


def test_mixing_currencies_would_overstate_spend(app):
    """
    Demonstrates why the currency filter matters: without it, a EUR row and a
    USD row are summed as if they were the same unit.
    """
    usd = fx.usage_rows("USD")
    eur = fx.usage_rows("EUR")
    mixed = pd.concat([usd, eur])

    single = app.daily_series(usd).sum()
    combined = app.daily_series(mixed).sum()
    assert combined == pytest.approx(single * 2)
    assert combined > single
