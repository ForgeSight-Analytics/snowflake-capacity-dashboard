"""
Snowflake Capacity, Run-Rate & Overage Dashboard
================================================

Shows how much of a prepaid Snowflake capacity contract has been consumed, the
current run rate, and when the balance is projected to run out — using only
views every Snowflake customer can query.

Data sources (all in SNOWFLAKE.ORGANIZATION_USAGE)
--------------------------------------------------
  CONTRACT_ITEMS            contract term + purchased capacity   (24h latency)
  REMAINING_BALANCE_DAILY   current balances by bucket           (72h latency)
  USAGE_IN_CURRENCY_DAILY   daily spend by balance source        (72h latency)

Reconciliation model
--------------------
  total_capacity = used_to_date + remaining_balance
  used_pct       = used_to_date / total_capacity
  adjustments    = total_capacity - capacity_purchased
                                  - rollover_granted
                                  - free_usage_granted

`adjustments` is a single reconciling residual. Snowflake applies internal
billing line items (offsets, manual adjustments, balance transfers, currency
conversion adjustments, balance expiry) that are not exposed in any customer
view. The residual is exact even though its breakdown is not available.

Scope and limitations
---------------------
  * Prepaid capacity contracts only. On-demand accounts have no balance to
    draw down and the app says so rather than inventing numbers.
  * Reseller contracts cannot read these views at all.
  * Where several organizations share a capacity contract, only the primary
    (funding) organization can read them.
  * Figures for recent days can still move until month-end close.

Requires no third-party packages: charts use altair, which ships with
Streamlit. Nothing is fetched from PyPI or Anaconda, so no external access
integration is needed on either the Warehouse or SPCS runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

st.set_page_config(
    page_title="Capacity, Run-Rate & Overage",
    page_icon=":snowflake:",
    layout="wide",
)

COLOR_ACTUAL = "#4FA3D1"
COLOR_PREDICT = "#D98324"
COLOR_CUMULATIVE = "#E6E6E6"
COLOR_CONTRACT = "#8A8A8A"
COLOR_OVERAGE = "#FF3B6B"

# Balance sources that draw down committed capacity. 'overage' is on-demand
# spend beyond the commitment; 'rebate' is a credit, not consumption.
CAPACITY_SOURCES = ("capacity", "rollover", "free usage")
CONSUMPTION_SOURCES = CAPACITY_SOURCES + ("overage",)

CAPACITY_ITEMS = ("capacity", "additional capacity")

RUN_RATE_PERIODS = [30, 60, 90, 180]
PROJECTION_MODELS = ["Flat average", "Seasonal replay", "Linear trend"]
CACHE_TTL = 3600

# Symbols for the currencies Snowflake bills in. Anything else falls back to
# the ISO code, which is correct if less pretty.
CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "AUD": "A$",
    "CAD": "C$",
    "NZD": "NZ$",
    "BRL": "R$",
}

# Identifiers interpolated into SQL are read back from Snowflake, not entered
# by a user, but they are still validated before being embedded.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class CapacityPosition:
    """Reconciled contract position as of the latest available balance date."""

    as_of: date
    currency: str
    contract_number: str
    contract_start: date
    contract_end: date
    capacity_purchased: float
    free_usage_granted: float
    rollover_granted: float
    adjustments: float
    total_capacity: float
    used_to_date: float
    remaining: float
    overage_to_date: float
    marketplace_remaining: float

    @property
    def used_pct(self) -> float:
        return self.used_to_date / self.total_capacity if self.total_capacity else 0.0


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------


def _lower(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.lower() for c in df.columns]
    return df


def _safe(identifier) -> str | None:
    """Return the identifier if it is safe to embed in SQL, else None."""
    text = str(identifier).strip()
    return text if _SAFE_IDENTIFIER.match(text) else None


def _contract_label(value) -> str:
    """
    CONTRACT_NUMBER arrives from Snowpark as a float, so a plain str() renders
    it as "100482.0". Show the integer form when there is no fractional part.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else str(value)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_contract_items() -> pd.DataFrame:
    session = get_active_session()
    return _lower(
        session.sql(
            """
            select contract_number, start_date, end_date, expiration_date,
                   contract_item, currency, amount
            from   snowflake.organization_usage.contract_items
            order  by start_date
            """
        ).to_pandas()
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_latest_balance(contract_number: str | None = None) -> pd.DataFrame:
    """
    Latest balance row, scoped to one contract.

    REMAINING_BALANCE_DAILY carries a row per contract. Taking the newest row
    across the whole view returns an arbitrary contract's balance once an
    organization has more than one, so the current contract is filtered for
    explicitly. Some organizations have a NULL contract_number here, hence the
    fallback to the unfiltered latest row.
    """
    session = get_active_session()
    columns = """date, contract_number, currency,
                 free_usage_balance,
                 capacity_balance,
                 rollover_balance,
                 on_demand_consumption_balance,
                 marketplace_capacity_drawdown_balance"""

    safe = _safe(contract_number) if contract_number else None
    if safe:
        scoped = _lower(
            session.sql(
                f"""
                select {columns}
                from   snowflake.organization_usage.remaining_balance_daily
                where  contract_number = '{safe}'
                qualify row_number() over (order by date desc) = 1
                """
            ).to_pandas()
        )
        if not scoped.empty:
            return scoped

    return _lower(
        session.sql(
            f"""
            select {columns}
            from   snowflake.organization_usage.remaining_balance_daily
            qualify row_number() over (order by date desc) = 1
            """
        ).to_pandas()
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_daily_usage(start: date, currency: str | None = None) -> pd.DataFrame:
    """
    Daily spend from the contract start date onward, in a single currency.

    An organization with accounts billed in more than one currency has a row
    per currency here. Summing across them adds unlike units, so the balance
    row's currency is used as the filter.
    """
    session = get_active_session()
    safe = _safe(currency) if currency else None
    currency_filter = f"and currency = '{safe}'" if safe else ""

    df = _lower(
        session.sql(
            f"""
            select usage_date,
                   balance_source,
                   currency,
                   sum(usage_in_currency) as spend
            from   snowflake.organization_usage.usage_in_currency_daily
            where  usage_date >= '{start:%Y-%m-%d}'
            {currency_filter}
            group  by 1, 2, 3
            order  by 1
            """
        ).to_pandas()
    )
    if df.empty:
        return df
    df["usage_date"] = pd.to_datetime(df["usage_date"]).dt.date
    df["balance_source"] = df["balance_source"].str.lower()
    return df


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def select_current_contract(contract: pd.DataFrame, today: date) -> pd.DataFrame:
    """
    Narrow CONTRACT_ITEMS to the contract in force on `today`.

    The Snowflake docs state this view "displays only the active contract", but
    in practice it also returns expired contracts and orphan free-usage grants
    with a NULL contract_number. Summing every row inflates purchased capacity
    (an expired contract's amount gets counted) and pushes the contract start
    date years too early, which in turn drags the whole usage window with it.

    A single contract legitimately has several line items — one capacity
    tranche per contract year, plus any mid-term 'additional capacity' add-ons
    — so the right unit is the contract number, not the row.
    """
    df = contract.copy()
    df["start_date"] = pd.to_datetime(df["start_date"]).dt.date
    df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
    df = df[df["contract_number"].notna()]

    if df.empty:
        return df

    covering = df[(df["start_date"] <= today) & (df["end_date"] >= today)]
    if covering.empty:
        # Between contracts, or the term data is stale: fall back to the latest.
        covering = df[df["end_date"] == df["end_date"].max()]

    number = covering.sort_values("end_date").iloc[-1]["contract_number"]
    return df[df["contract_number"] == number]


def balance_scope_status(balance: pd.DataFrame, contract_number) -> str:
    """
    Whether the balance row can be tied to the contract being reported.

    load_latest_balance falls back to the newest row across all contracts when
    the scoped query finds nothing. That fallback is correct for organizations
    that leave contract_number NULL in this view, but wrong the moment it
    returns a *different* contract's balance: the tiles would then pair one
    contract's purchased capacity with another contract's remaining balance and
    every figure would be wrong, with nothing on screen to say so. The most
    likely trigger is the days just after a renewal, when the new contract
    exists in CONTRACT_ITEMS but has no balance row yet.

    Returns "match", "unknown" (no contract number on the balance row, so the
    pairing cannot be checked) or "mismatch".
    """
    if balance.empty or "contract_number" not in balance.columns:
        return "unknown"
    raw = balance.iloc[0]["contract_number"]
    try:
        if raw is None or pd.isna(raw):
            return "unknown"
    except (TypeError, ValueError):
        return "unknown"
    return "match" if _contract_label(raw) == _contract_label(contract_number) else "mismatch"


def reconcile(
    contract: pd.DataFrame, balance: pd.DataFrame, usage: pd.DataFrame
) -> CapacityPosition:
    """
    Derive the full contract position.

    `contract` must already be narrowed by select_current_contract().
    """
    bal = balance.iloc[0]
    item = contract["contract_item"].str.lower()

    capacity_purchased = float(
        contract.loc[item.isin(CAPACITY_ITEMS), "amount"].sum()
    )
    free_usage_contract = float(contract.loc[item == "free usage", "amount"].sum())

    def spend_from(source: str) -> float:
        if usage.empty:
            return 0.0
        return float(usage.loc[usage["balance_source"] == source, "spend"].sum())

    used_to_date = (
        0.0
        if usage.empty
        else float(
            usage.loc[usage["balance_source"].isin(CAPACITY_SOURCES), "spend"].sum()
        )
    )
    overage_to_date = spend_from("overage")

    remaining = float(
        bal["capacity_balance"] + bal["rollover_balance"] + bal["free_usage_balance"]
    )

    # Granted amounts are reconstructed as (remaining in bucket) + (drawn from bucket).
    rollover_granted = float(bal["rollover_balance"]) + spend_from("rollover")
    free_usage_granted = float(bal["free_usage_balance"]) + spend_from("free usage")
    if free_usage_contract > free_usage_granted:
        free_usage_granted = free_usage_contract

    total_capacity = used_to_date + remaining
    adjustments = (
        total_capacity - capacity_purchased - rollover_granted - free_usage_granted
    )

    return CapacityPosition(
        as_of=pd.to_datetime(bal["date"]).date(),
        currency=str(bal["currency"]),
        contract_number=_contract_label(contract["contract_number"].iloc[0]),
        contract_start=pd.to_datetime(contract["start_date"].min()).date(),
        contract_end=pd.to_datetime(contract["end_date"].max()).date(),
        capacity_purchased=capacity_purchased,
        free_usage_granted=free_usage_granted,
        rollover_granted=rollover_granted,
        adjustments=adjustments,
        total_capacity=total_capacity,
        used_to_date=used_to_date,
        remaining=remaining,
        overage_to_date=overage_to_date,
        marketplace_remaining=float(bal["marketplace_capacity_drawdown_balance"]),
    )


# --------------------------------------------------------------------------
# Projection models
# --------------------------------------------------------------------------


def daily_series(usage: pd.DataFrame) -> pd.Series:
    """Total daily consumption, gap-filled with zeros."""
    if usage.empty:
        return pd.Series(dtype=float)
    s = (
        usage.loc[usage["balance_source"].isin(CONSUMPTION_SOURCES)]
        .groupby("usage_date")["spend"]
        .sum()
    )
    if s.empty:
        return s
    idx = pd.date_range(min(s.index), max(s.index), freq="D").date
    return s.reindex(idx, fill_value=0.0)


def project(history: pd.Series, window: int, horizon: int, method: str) -> pd.Series:
    """Forward daily spend for `horizon` days using the last `window` days."""
    recent = history.tail(window)
    if recent.empty or horizon <= 0:
        return pd.Series(dtype=float)

    start = max(history.index) + timedelta(days=1)
    future_idx = [start + timedelta(days=i) for i in range(horizon)]

    if method == "Flat average":
        values = np.full(horizon, recent.mean())

    elif method == "Seasonal replay":
        # Repeat the recent daily pattern forward, preserving weekday shape.
        values = np.resize(recent.to_numpy(), horizon)

    else:  # "Linear trend"
        values = _seasonal_trend(recent, horizon)

    return pd.Series(values, index=future_idx)


def _weekday_factors(values: np.ndarray, weekdays: np.ndarray) -> np.ndarray:
    """Multiplicative day-of-week factors, normalised to mean 1."""
    overall = values.mean()
    factors = np.ones(7)
    if overall <= 0:
        return factors
    for d in range(7):
        mask = weekdays == d
        if mask.any():
            factors[d] = values[mask].mean() / overall
    factors = np.where(factors <= 0, 1.0, factors)
    return factors / factors.mean()


def _seasonal_trend(recent: pd.Series, horizon: int) -> np.ndarray:
    """
    Fit the trend on a deseasonalised series, then re-apply weekday shape.

    Fitting a raw line to a short window is unstable when weekday seasonality
    is strong: a window ending on weekend days can yield a spurious negative
    slope and push the projected overage date out past the real one. Removing
    the weekly pattern first makes the slope reflect the actual trajectory.
    """
    idx = pd.DatetimeIndex(recent.index)
    y = recent.to_numpy(dtype=float)
    dow = idx.dayofweek.to_numpy()

    factors = _weekday_factors(y, dow)
    deseasonalised = y / factors[dow]

    x = np.arange(len(deseasonalised))
    slope, intercept = np.polyfit(x, deseasonalised, 1)

    # Shrink the slope by the fit's R². A short, noisy window can produce a
    # large slope that reflects nothing; scaling by explained variance collapses
    # such a trend toward zero, degrading the model gracefully to the window's
    # average rather than projecting a confident trajectory that is not there.
    fitted = slope * x + intercept
    ss_tot = float(((deseasonalised - deseasonalised.mean()) ** 2).sum())
    ss_res = float(((deseasonalised - fitted) ** 2).sum())
    r_squared = max(1.0 - ss_res / ss_tot, 0.0) if ss_tot > 0 else 0.0
    effective_slope = slope * r_squared

    # Anchor the level at the window mean so only the trend component is shrunk.
    future_x = np.arange(len(deseasonalised), len(deseasonalised) + horizon)
    baseline = deseasonalised.mean() + effective_slope * (future_x - x.mean())

    last_dow = int(dow[-1])
    future_dow = np.array([(last_dow + 1 + i) % 7 for i in range(horizon)])
    return np.clip(baseline * factors[future_dow], 0.0, None)


def overage_date(forecast: pd.Series, remaining: float) -> date | None:
    """First date on which cumulative forecast spend exceeds the remaining balance."""
    if forecast.empty or remaining <= 0:
        return None
    crossed = forecast.cumsum() > remaining
    return forecast.index[crossed.argmax()] if crossed.any() else None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def money(value: float, currency: str = "USD", decimals: int = 0) -> str:
    """Format an amount in the organization's billing currency."""
    symbol = CURRENCY_SYMBOLS.get(str(currency).upper())
    if symbol:
        return f"{symbol}{value:,.{decimals}f}"
    return f"{value:,.{decimals}f} {str(currency).upper()}"


def md_money(value: float, currency: str = "USD", decimals: int = 0) -> str:
    """
    Currency formatted for a Streamlit markdown context.

    st.caption and st.markdown treat paired '$' as LaTeX math delimiters, so a
    caption holding several dollar figures silently renders as run-together
    italic equations instead of text. Escaping the sign keeps it literal.
    """
    return money(value, currency, decimals).replace("$", "\\$")


def render_tiles(pos: CapacityPosition, projected_overage: float, days_left: int | None):
    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric(
        "Capacity Purchased",
        money(pos.capacity_purchased, pos.currency),
        f"ContractStart: {pos.contract_start:%m/%d/%Y}",
        delta_color="off",
    )
    c2.metric(
        "Total Capacity Used",
        money(pos.used_to_date, pos.currency),
        f"Used %: {pos.used_pct:.1%}",
        delta_color="off",
    )
    c3.metric(
        "Projected Net Overage",
        money(projected_overage, pos.currency),
        f"ContractEnd: {pos.contract_end:%m/%d/%Y}",
        delta_color="off",
    )
    c4.metric(
        "Days to Overage",
        f"{days_left} days" if days_left is not None else "None projected",
        "Within contract term" if days_left is not None else "Capacity sufficient",
        delta_color="off",
    )
    c5.metric(
        "Remaining Balance",
        money(pos.remaining, pos.currency),
        f"As of {pos.as_of:%m/%d/%Y}",
        delta_color="off",
    )


def render_footnote(pos: CapacityPosition):
    cur = pos.currency
    st.caption(
        f"*Note: Capacity Used % is based on Total Capacity of "
        f"**{md_money(pos.total_capacity, cur)}** = "
        f"Capacity Purchased **{md_money(pos.capacity_purchased, cur)}** + "
        f"Free Usage **{md_money(pos.free_usage_granted, cur)}** + "
        f"Rollover **{md_money(pos.rollover_granted, cur)}** + "
        f"Adjustments **{md_money(pos.adjustments, cur)}**. "
        f"Adjustments is a single reconciling residual — Snowflake does not "
        f"expose its internal billing line items (offsets, manual adjustments, "
        f"balance transfers, currency conversion adjustments, balance expiry) "
        f"in any customer view."
    )
    st.caption(
        f"Marketplace Capacity Drawdown: Remaining Balance "
        f"**{md_money(pos.marketplace_remaining, cur)}** "
        f"· On-demand overage billed to date: "
        f"**{md_money(pos.overage_to_date, cur)}** "
        f"· Contract **{pos.contract_number}** · Currency **{cur}** "
        f"· Source latency up to 72h."
    )


def render_chart(
    pos: CapacityPosition,
    history: pd.Series,
    forecast: pd.Series,
    projected_date: date | None,
):
    bars_df = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": list(history.index),
                    "spend": history.to_numpy(),
                    "series": "Actual",
                }
            ),
            pd.DataFrame(
                {
                    "date": list(forecast.index),
                    "spend": forecast.to_numpy(),
                    "series": "Prediction",
                }
            ),
        ]
    )

    cumulative = pd.concat([history, forecast]).cumsum()
    lines_df = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": list(cumulative.index),
                    "value": cumulative.to_numpy(),
                    "series": "Cumulative Consump",
                }
            ),
            # Even-burn pace line: 0 at contract start, full capacity at contract end.
            pd.DataFrame(
                {
                    "date": [pos.contract_start, pos.contract_end],
                    "value": [0.0, pos.total_capacity],
                    "series": "Capacity Contract Amt",
                }
            ),
        ]
    )

    # The forecast deliberately runs past contract end so an overage date can
    # always be found, but plotting that tail squeezes the actual history into a
    # fraction of the width. Show up to contract end.
    bars_df = bars_df[[d <= pos.contract_end for d in bars_df["date"]]]
    lines_df = lines_df[[d <= pos.contract_end for d in lines_df["date"]]]

    # One shared colour scale so both layers contribute to a single merged legend.
    scale = alt.Scale(
        domain=["Actual", "Prediction", "Cumulative Consump", "Capacity Contract Amt"],
        range=[COLOR_ACTUAL, COLOR_PREDICT, COLOR_CUMULATIVE, COLOR_CONTRACT],
    )
    legend = alt.Legend(title=None, orient="bottom")

    bars = alt.Chart(bars_df).mark_bar().encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("spend:Q", title=f"Consumption ({pos.currency})"),
        color=alt.Color("series:N", scale=scale, legend=legend),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("spend:Q", title="Spend", format=",.2f"),
            alt.Tooltip("series:N", title="Series"),
        ],
    )

    lines = alt.Chart(lines_df).mark_line(strokeWidth=2).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("value:Q", title="Cumulative Consumption"),
        color=alt.Color("series:N", scale=scale, legend=legend),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("value:Q", title="Cumulative", format=",.0f"),
        ],
    )

    layers = [bars, lines]

    if projected_date is not None:
        # No colour encoding here, so the rule stays out of the legend.
        layers.append(
            alt.Chart(pd.DataFrame({"date": [projected_date]}))
            .mark_rule(color=COLOR_OVERAGE, strokeDash=[6, 4], strokeWidth=2)
            .encode(x="date:T")
        )

    chart = (
        alt.layer(*layers).resolve_scale(y="independent").properties(height=440)
    )

    if projected_date is not None:
        st.caption(
            f"Projected overage date: **{projected_date:%m/%d/%Y}** (dashed line)"
        )
    st.altair_chart(chart, use_container_width=True)


def render_reconciliation(
    history: pd.Series, pos: CapacityPosition, window: int, horizon: int
):
    """Show every model's overage date side by side, for comparing sources."""
    rows = []
    for method in PROJECTION_MODELS:
        fc = project(history, window, horizon, method)
        d = overage_date(fc, pos.remaining)
        days = (d - pos.as_of).days if d else None
        rows.append(
            {
                "Model": method,
                "Avg daily spend": (
                    money(fc.mean(), pos.currency, 2) if not fc.empty else "—"
                ),
                "Overage date": f"{d:%m/%d/%Y}" if d else "Not within horizon",
                "Days from as-of": days if days is not None else "—",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    if pos.remaining > 0:
        st.caption(
            "If another source quotes a different overage date, the implied "
            f"daily rate is **{md_money(pos.remaining, pos.currency, 2)} ÷ "
            "(days to that date)**. A materially higher implied rate than the "
            "flat average usually means that model assumes growth, or is using "
            "a longer run-rate window than the one selected here."
        )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
    st.title("Capacity, Run-Rate & Overage")

    try:
        contract = load_contract_items()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read SNOWFLAKE.ORGANIZATION_USAGE: {exc}")
        st.info(
            "These views need ORGADMIN plus an explicit grant:\n\n"
            "```sql\n"
            "GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_USAGE_VIEWER "
            "TO ROLE <your_role>;\n"
            "GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_BILLING_VIEWER "
            "TO ROLE <your_role>;\n"
            "```\n\n"
            "IMPORTED PRIVILEGES on the SNOWFLAKE database is *not* sufficient: "
            "that covers ACCOUNT_USAGE only. Reseller contracts cannot access "
            "these views at all."
        )
        return

    if contract.empty:
        st.warning(
            "No capacity contract found. This dashboard only applies to prepaid "
            "capacity contracts — on-demand accounts have no balance to draw down."
        )
        return

    current = select_current_contract(contract, date.today())
    if current.empty:
        st.warning("No contract with a contract number was found.")
        return

    contract_number = current["contract_number"].iloc[0]
    balance = load_latest_balance(contract_number)

    if balance.empty:
        st.warning(
            "No balance rows in REMAINING_BALANCE_DAILY. This is expected for "
            "on-demand accounts and for reseller contracts."
        )
        return

    scope = balance_scope_status(balance, contract_number)
    if scope == "mismatch":
        st.error(
            f"The latest balance row belongs to contract "
            f"**{_contract_label(balance.iloc[0]['contract_number'])}**, not to "
            f"**{_contract_label(contract_number)}**, the contract in force today. "
            "Reporting them together would pair one contract's purchased capacity "
            "with another contract's remaining balance, so no figures are shown."
        )
        st.info(
            "This usually means REMAINING_BALANCE_DAILY has no rows yet for the "
            "current contract, which is common in the days after a renewal given "
            "the view's 72-hour latency. Try again once the first balance row for "
            "the new contract lands."
        )
        return

    currency = str(balance.iloc[0]["currency"])
    contract_start = min(current["start_date"])
    usage = load_daily_usage(contract_start, currency)
    pos = reconcile(current, balance, usage)
    history = daily_series(usage)

    if history.empty:
        st.warning("No usage recorded against this contract yet.")
        return

    # Reserve the tile row above the controls: Streamlit paints in creation
    # order, so the columns must not be built before the tiles are placed.
    tiles_slot = st.container()
    footnote_slot = st.container()
    st.divider()

    left, right = st.columns([1, 4])

    with left:
        st.markdown("**Select a Run Rate period**")
        window = st.radio(
            "Run rate period",
            RUN_RATE_PERIODS,
            format_func=lambda d: f"{d} days",
            label_visibility="collapsed",
        )
        method = st.selectbox("Projection model", PROJECTION_MODELS)
        run_rate = history.tail(window).mean() * 365
        st.metric(
            "Consumption Run Rate",
            money(run_rate, pos.currency),
            "Avg Daily Consump. × 365",
            delta_color="off",
        )

    horizon = max((pos.contract_end - max(history.index)).days, 0) + 400
    forecast = project(history, window, horizon, method)
    proj_date = overage_date(forecast, pos.remaining)
    days_left = (proj_date - pos.as_of).days if proj_date else None

    # Projected overage = forecast spend through contract end, less what's left.
    through_end = forecast[[d <= pos.contract_end for d in forecast.index]]
    projected_overage = max(through_end.sum() - pos.remaining, 0.0)

    with tiles_slot:
        render_tiles(pos, projected_overage, days_left)
    with footnote_slot:
        render_footnote(pos)
        if scope == "unknown":
            st.caption(
                ":warning: The balance row carries no contract number, so it "
                "could not be tied to this contract. Figures are shown on the "
                "assumption that the organization has a single active contract."
            )

    with right:
        render_chart(pos, history, forecast, proj_date)

    with st.expander("Compare projection models"):
        render_reconciliation(history, pos, window, horizon)


if __name__ == "__main__":
    main()
