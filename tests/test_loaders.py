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


# -- the balance must belong to the contract being reported -----------------
#
# Regression: the fallback in load_latest_balance silently returned the newest
# row from *any* contract. After a renewal — new contract in CONTRACT_ITEMS, no
# balance row for it yet — that paired the new contract's purchased capacity
# with the old contract's remaining balance and every tile was wrong, with no
# warning. The app now refuses to render instead.


def test_scope_status_match(app):
    assert app.balance_scope_status(fx.balance_row(), fx.CONTRACT) == "match"


def test_scope_status_match_when_snowpark_returns_a_float(app):
    balance = fx.balance_row()
    balance["contract_number"] = balance["contract_number"].astype(object)
    balance.loc[0, "contract_number"] = float(fx.CONTRACT)
    assert app.balance_scope_status(balance, fx.CONTRACT) == "match"


def test_scope_status_mismatch_is_detected(app):
    balance = fx.balance_row()
    balance.loc[0, "contract_number"] = fx.EXPIRED_CONTRACT
    assert app.balance_scope_status(balance, fx.CONTRACT) == "mismatch"


@pytest.mark.parametrize("value", [None, float("nan")])
def test_scope_status_unknown_when_contract_number_is_null(app, value):
    balance = fx.balance_row()
    balance["contract_number"] = balance["contract_number"].astype(object)
    balance.loc[0, "contract_number"] = value
    assert app.balance_scope_status(balance, fx.CONTRACT) == "unknown"


def test_scope_status_unknown_when_column_absent(app):
    assert app.balance_scope_status(
        fx.balance_row().drop(columns=["contract_number"]), fx.CONTRACT
    ) == "unknown"


def test_scope_status_unknown_on_empty_frame(app):
    assert app.balance_scope_status(pd.DataFrame(), fx.CONTRACT) == "unknown"


def test_renewal_scenario_is_caught(app, fake):
    """
    End to end: the scoped query finds nothing for the new contract, the loader
    falls back to the old contract's row, and the scope check catches it.
    """
    old_balance = fx.balance_row()
    old_balance.loc[0, "contract_number"] = fx.EXPIRED_CONTRACT
    fake(pd.DataFrame(), upper(old_balance))

    balance = app.load_latest_balance(fx.CONTRACT)
    assert not balance.empty
    assert app.balance_scope_status(balance, fx.CONTRACT) == "mismatch"
