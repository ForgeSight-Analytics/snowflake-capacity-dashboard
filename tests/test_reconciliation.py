"""
Reconciliation tests.

These encode the contract the dashboard is making: that the five header tiles
tie out against CONTRACT_ITEMS and REMAINING_BALANCE_DAILY. If you adapt the
dashboard, keep these passing — they are what stops it quietly disagreeing
with your Snowflake invoice.
"""

from datetime import date

import pytest

import fixtures as fx


# -- contract selection -----------------------------------------------------


def test_picks_the_contract_in_force(app):
    current = app.select_current_contract(fx.messy_contract_items(), fx.AS_OF)
    assert current["contract_number"].nunique() == 1
    assert current["contract_number"].iloc[0] == fx.CONTRACT


def test_keeps_every_line_item_of_that_contract(app):
    """Annual tranches plus mid-term add-ons all belong to the same contract."""
    current = app.select_current_contract(fx.messy_contract_items(), fx.AS_OF)
    assert len(current) == 4
    assert current["amount"].sum() == pytest.approx(fx.CAPACITY_PURCHASED)


def test_excludes_expired_contract_and_null_contract_grant(app):
    """
    The regression this exists for: summing every row counts the expired
    contract's capacity too, and takes its start date as the contract start.
    """
    items = fx.messy_contract_items()
    naive_total = items.loc[
        items["contract_item"].str.lower().isin(app.CAPACITY_ITEMS), "amount"
    ].sum()
    assert naive_total == pytest.approx(fx.CAPACITY_PURCHASED + fx.EXPIRED_CAPACITY)

    current = app.select_current_contract(items, fx.AS_OF)
    assert fx.EXPIRED_CONTRACT not in set(current["contract_number"])
    assert current["contract_number"].notna().all()
    assert min(current["start_date"]) == fx.CONTRACT_START
    assert max(current["end_date"]) == fx.CONTRACT_END


def test_falls_back_to_latest_contract_when_none_covers_today(app):
    """Between contracts, report the most recent rather than failing."""
    current = app.select_current_contract(fx.messy_contract_items(), date(2030, 1, 1))
    assert not current.empty
    assert current["contract_number"].iloc[0] == fx.CONTRACT


def test_returns_empty_when_every_contract_number_is_null(app):
    items = fx.messy_contract_items()
    items["contract_number"] = None
    assert app.select_current_contract(items, fx.AS_OF).empty


# -- the five tiles ---------------------------------------------------------


@pytest.fixture(scope="module")
def position(app):
    current = app.select_current_contract(fx.messy_contract_items(), fx.AS_OF)
    return app.reconcile(current, fx.balance_row(), fx.usage_rows())


@pytest.mark.parametrize(
    "attribute, expected",
    [
        ("capacity_purchased", fx.CAPACITY_PURCHASED),
        ("rollover_granted", fx.ROLLOVER_GRANTED),
        ("free_usage_granted", 0.0),
        ("used_to_date", fx.USED_TO_DATE),
        ("remaining", fx.CAPACITY_BALANCE),
        ("total_capacity", fx.TOTAL_CAPACITY),
        ("adjustments", 0.0),
        ("marketplace_remaining", fx.MARKETPLACE),
        ("overage_to_date", 0.0),
    ],
)
def test_tile_values(position, attribute, expected):
    assert getattr(position, attribute) == pytest.approx(expected, abs=0.75)


def test_used_percentage(position):
    assert position.used_pct * 100 == pytest.approx(
        100 * fx.USED_TO_DATE / fx.TOTAL_CAPACITY, abs=0.02
    )


def test_identity_total_equals_used_plus_remaining(position):
    assert position.total_capacity == pytest.approx(
        position.used_to_date + position.remaining
    )


def test_adjustments_is_the_reconciling_residual(position):
    """Whatever the internal line items are, the residual closes the books."""
    rebuilt = (
        position.capacity_purchased
        + position.rollover_granted
        + position.free_usage_granted
        + position.adjustments
    )
    assert rebuilt == pytest.approx(position.total_capacity)


def test_contract_number_is_not_rendered_as_a_float(app):
    """Snowpark hands back 100482.0; the tile should not show the decimal."""
    items = fx.messy_contract_items()
    items["contract_number"] = items["contract_number"].astype("float64", errors="ignore")
    current = app.select_current_contract(items, fx.AS_OF)
    pos = app.reconcile(current, fx.balance_row(), fx.usage_rows())
    assert pos.contract_number == fx.CONTRACT
    assert "." not in pos.contract_number


# -- currency ---------------------------------------------------------------


def test_position_carries_the_billing_currency(app):
    current = app.select_current_contract(fx.messy_contract_items(), fx.AS_OF)
    pos = app.reconcile(current, fx.balance_row("EUR"), fx.usage_rows("EUR"))
    assert pos.currency == "EUR"


@pytest.mark.parametrize(
    "currency, expected",
    [
        ("USD", "$1,234"),
        ("EUR", "€1,234"),
        ("GBP", "£1,234"),
        ("CHF", "1,234 CHF"),
        ("SEK", "1,234 SEK"),
    ],
)
def test_money_formats_per_currency(app, currency, expected):
    """Non-USD organizations must not see a dollar sign."""
    assert app.money(1234.0, currency) == expected


def test_md_money_escapes_dollar_for_markdown(app):
    """
    Unescaped, paired '$' in a caption is parsed as LaTeX and the whole
    footnote renders as italic equations.
    """
    assert app.md_money(1234.0, "USD") == "\\$1,234"
    assert app.md_money(1234.0, "EUR") == "€1,234"


# -- degenerate inputs ------------------------------------------------------


def test_empty_usage_does_not_raise(app):
    import pandas as pd

    current = app.select_current_contract(fx.messy_contract_items(), fx.AS_OF)
    empty = pd.DataFrame(columns=["usage_date", "balance_source", "currency", "spend"])
    pos = app.reconcile(current, fx.balance_row(), empty)
    assert pos.used_to_date == 0.0
    assert pos.used_pct == pytest.approx(0.0)


def test_zero_total_capacity_does_not_divide_by_zero(app):
    import pandas as pd

    current = app.select_current_contract(fx.messy_contract_items(), fx.AS_OF)
    balance = fx.balance_row()
    balance.loc[0, "capacity_balance"] = 0.0
    empty = pd.DataFrame(columns=["usage_date", "balance_source", "currency", "spend"])
    pos = app.reconcile(current, balance, empty)
    assert pos.used_pct == 0.0


# -- SQL identifier guard ---------------------------------------------------


@pytest.mark.parametrize("value", ["100482", "ABC-123", "a.b_c"])
def test_safe_identifier_accepts_real_contract_numbers(app, value):
    assert app._safe(value) == value


@pytest.mark.parametrize("value", ["'; drop table t --", "a b", "x" * 80, "a'b"])
def test_safe_identifier_rejects_anything_else(app, value):
    assert app._safe(value) is None
