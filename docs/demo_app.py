"""
Run the dashboard against synthetic data, for screenshots and local preview.

    streamlit run docs/demo_app.py --theme.base dark

Nothing here touches Snowflake. The loaders are replaced with generated frames,
so the screenshot in the README is a genuine render of the real app — the
figures are invented, not retouched.

That distinction matters for anyone screenshotting their own instance: editing
the tile numbers in an image editor does not anonymise a dashboard, because the
chart still plots the organization's actual daily consumption curve. Spikes,
step changes and weekday rhythm are a recognisable fingerprint of real spend.
Render synthetic data instead.

The figures are chosen so the reconciliation closes exactly:

    capacity_purchased  75,000   (four line items on one contract)
  + rollover_granted    18,000
  ------------------------------
    total_capacity      93,000   = used_to_date + remaining_balance
"""

from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# Stub Snowpark before importing the app, so this runs with no Snowflake.
# --------------------------------------------------------------------------

_snowflake = sys.modules.setdefault("snowflake", types.ModuleType("snowflake"))
_snowpark = sys.modules.setdefault(
    "snowflake.snowpark", types.ModuleType("snowflake.snowpark")
)
_context = types.ModuleType("snowflake.snowpark.context")


def _no_session():
    raise RuntimeError("demo_app runs on generated data; there is no session")


_context.get_active_session = _no_session
sys.modules["snowflake.snowpark.context"] = _context
_snowpark.context = _context
_snowflake.snowpark = _snowpark

sys.path.insert(0, str(REPO_ROOT))

# Streamlit re-runs this script per session but keeps sys.modules, so a cached
# streamlit_app would skip its set_page_config on every run after the first and
# the page would fall back to the narrow default layout. Re-import it each run.
sys.modules.pop("streamlit_app", None)

import streamlit_app as app  # noqa: E402

# --------------------------------------------------------------------------
# Synthetic organization
# --------------------------------------------------------------------------

CONTRACT = "100482"
CONTRACT_START = date(2025, 1, 1)
CONTRACT_END = date(2026, 12, 31)
AS_OF = date(2026, 9, 14)

CAPACITY_PURCHASED = 75_000.00
ROLLOVER_GRANTED = 18_000.00
TOTAL_CAPACITY = CAPACITY_PURCHASED + ROLLOVER_GRANTED
USED_TO_DATE = 76_000.00
MARKETPLACE = 2_000.00


def _daily_series() -> pd.Series:
    """
    A consumption curve with the shape real usage tends to have: a gradual
    ramp, weekday seasonality, one spike, and a step change partway through.
    Scaled so the total equals USED_TO_DATE exactly.
    """
    rng = np.random.default_rng(20260914)
    days = list(pd.date_range(CONTRACT_START, AS_OF, freq="D").date)
    n = len(days)

    ramp = np.linspace(0.62, 1.00, n)
    step = np.where(np.arange(n) > int(n * 0.72), 1.28, 1.0)
    weekday = np.array([1.0 if d.weekday() < 5 else 0.55 for d in days])
    noise = rng.normal(1.0, 0.09, n)

    values = np.clip(ramp * step * weekday * noise, 0.05, None)
    values[int(n * 0.34)] *= 3.4  # a one-off spike
    values[int(n * 0.35)] *= 2.1

    values *= USED_TO_DATE / values.sum()
    return pd.Series(values, index=days)


SERIES = _daily_series()
REMAINING = TOTAL_CAPACITY - USED_TO_DATE


def contract_items() -> pd.DataFrame:
    common = dict(contract_number=CONTRACT, expiration_date=None, currency="USD")
    return pd.DataFrame(
        [
            dict(start_date=CONTRACT_START, end_date=date(2025, 12, 31),
                 contract_item="Capacity", amount=25_000.00, **common),
            dict(start_date=date(2026, 1, 1), end_date=CONTRACT_END,
                 contract_item="Capacity", amount=25_000.00, **common),
            dict(start_date=date(2026, 6, 1), end_date=CONTRACT_END,
                 contract_item="Additional Capacity", amount=10_000.00, **common),
            dict(start_date=date(2026, 8, 1), end_date=CONTRACT_END,
                 contract_item="Additional Capacity", amount=15_000.00, **common),
        ]
    )


def latest_balance(contract_number=None) -> pd.DataFrame:
    return pd.DataFrame(
        [
            dict(
                date=AS_OF,
                contract_number=CONTRACT,
                currency="USD",
                free_usage_balance=0.00,
                capacity_balance=REMAINING,
                rollover_balance=0.00,
                on_demand_consumption_balance=0.00,
                marketplace_capacity_drawdown_balance=MARKETPLACE,
            )
        ]
    )


def daily_usage(start=None, currency=None) -> pd.DataFrame:
    """Rollover drawn first, then capacity — the order Snowflake applies."""
    rows, drawn = [], 0.0
    for day, spend in SERIES.items():
        source = "rollover" if drawn < ROLLOVER_GRANTED else "capacity"
        if source == "rollover" and drawn + spend > ROLLOVER_GRANTED:
            split = ROLLOVER_GRANTED - drawn
            rows.append(dict(usage_date=day, balance_source="rollover",
                             currency="USD", spend=split))
            rows.append(dict(usage_date=day, balance_source="capacity",
                             currency="USD", spend=spend - split))
        else:
            rows.append(dict(usage_date=day, balance_source=source,
                             currency="USD", spend=spend))
        drawn += spend
    return pd.DataFrame(rows)


app.load_contract_items = contract_items
app.load_latest_balance = latest_balance
app.load_daily_usage = daily_usage

app.main()
