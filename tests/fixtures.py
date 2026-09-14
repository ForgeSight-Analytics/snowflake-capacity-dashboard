"""
Fixture builders.

The figures here are invented, but the *shape* is taken from a real account:
`messy_contract_items()` reproduces what CONTRACT_ITEMS actually returns —
several contracts, one of them expired, plus an orphan free-usage grant with a
NULL contract number. A tidy single-contract fixture cannot catch the class of
bug that view causes, so the messy one is the default everywhere.

The numbers are chosen so the reconciliation closes exactly:

    capacity_purchased   75,000   (four line items on the current contract)
  + rollover_granted     18,000
  + free_usage_granted        0
  + adjustments               0
  ----------------------------------
    total_capacity       93,000   = used_to_date 76,000 + remaining 17,000
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

AS_OF = date(2026, 9, 11)

CONTRACT = "100482"
EXPIRED_CONTRACT = "100119"
CONTRACT_START = date(2025, 1, 1)
CONTRACT_END = date(2026, 12, 31)

CAPACITY_PURCHASED = 75_000.00
EXPIRED_CAPACITY = 50_000.00
ROLLOVER_GRANTED = 18_000.00
USED_TO_DATE = 76_000.00
CAPACITY_BALANCE = 17_000.00
TOTAL_CAPACITY = 93_000.00
MARKETPLACE = 2_000.00
DAILY_30 = 215.69


def messy_contract_items() -> pd.DataFrame:
    """CONTRACT_ITEMS with an expired contract and a NULL-contract grant."""
    return pd.DataFrame(
        [
            dict(
                contract_number=None,
                start_date=date(2023, 10, 31),
                end_date=date(2023, 12, 30),
                expiration_date=date(2023, 12, 15),
                contract_item="Free Usage",
                currency="USD",
                amount=400.00,
            ),
            dict(
                contract_number=EXPIRED_CONTRACT,
                start_date=date(2023, 12, 15),
                end_date=date(2024, 12, 31),
                expiration_date=date(2024, 12, 31),
                contract_item="Capacity",
                currency="USD",
                amount=EXPIRED_CAPACITY,
            ),
            # Current contract: one tranche per contract year ...
            dict(
                contract_number=CONTRACT,
                start_date=CONTRACT_START,
                end_date=date(2025, 12, 31),
                expiration_date=None,
                contract_item="Capacity",
                currency="USD",
                amount=25_000.00,
            ),
            dict(
                contract_number=CONTRACT,
                start_date=date(2026, 1, 1),
                end_date=CONTRACT_END,
                expiration_date=None,
                contract_item="Capacity",
                currency="USD",
                amount=25_000.00,
            ),
            # ... plus mid-term add-ons, which belong to the same contract.
            dict(
                contract_number=CONTRACT,
                start_date=date(2026, 6, 1),
                end_date=CONTRACT_END,
                expiration_date=None,
                contract_item="Additional Capacity",
                currency="USD",
                amount=10_000.00,
            ),
            dict(
                contract_number=CONTRACT,
                start_date=date(2026, 8, 1),
                end_date=CONTRACT_END,
                expiration_date=None,
                contract_item="Additional Capacity",
                currency="USD",
                amount=15_000.00,
            ),
        ]
    )


def balance_row(currency: str = "USD") -> pd.DataFrame:
    """Latest REMAINING_BALANCE_DAILY row: rollover spent, capacity remaining."""
    return pd.DataFrame(
        [
            dict(
                date=AS_OF,
                contract_number=CONTRACT,
                currency=currency,
                free_usage_balance=0.00,
                capacity_balance=CAPACITY_BALANCE,
                rollover_balance=0.00,
                on_demand_consumption_balance=0.00,
                marketplace_capacity_drawdown_balance=MARKETPLACE,
            )
        ]
    )


def usage_rows(currency: str = "USD") -> pd.DataFrame:
    """
    Daily usage summing to USED_TO_DATE, with the rollover bucket fully drawn
    and the last 30 days averaging DAILY_30.
    """
    drawn_capacity = USED_TO_DATE - ROLLOVER_GRANTED
    days = list(pd.date_range(CONTRACT_START, AS_OF, freq="D").date)
    tail = 30
    head_daily = (drawn_capacity - DAILY_30 * tail) / (len(days) - tail)

    rows = [
        dict(
            usage_date=day,
            balance_source="capacity",
            currency=currency,
            spend=DAILY_30 if i >= len(days) - tail else head_daily,
        )
        for i, day in enumerate(days)
    ]
    rollover_daily = ROLLOVER_GRANTED / 200
    rows.extend(
        dict(
            usage_date=day,
            balance_source="rollover",
            currency=currency,
            spend=rollover_daily,
        )
        for day in days[:200]
    )
    return pd.DataFrame(rows)


def synthetic_series(seed: int, start_level: float, end_level: float) -> pd.Series:
    """
    Daily spend with a linear trend, weekday seasonality and one spike —
    the shape real consumption tends to have.
    """
    rng = np.random.default_rng(seed)
    days = list(pd.date_range(date(2026, 1, 1), AS_OF, freq="D").date)
    base = np.linspace(start_level, end_level, len(days))
    weekday = np.array([1.0 if d.weekday() < 5 else 0.55 for d in days])
    values = np.clip(base * weekday + rng.normal(0, 12, len(days)), 0, None)
    values[100] = 480.0  # a spike, as real usage has
    return pd.Series(values, index=days)


def flat_series(seed: int, level: float = 200.0) -> pd.Series:
    return synthetic_series(seed, level, level)
