# Snowflake Capacity, Run-Rate & Overage Dashboard

A single-file Streamlit-in-Snowflake app that answers two questions a prepaid
Snowflake customer cannot easily answer from Snowsight:

1. **How much of our capacity contract is left?**
2. **When are we projected to run out?**

It reads only `SNOWFLAKE.ORGANIZATION_USAGE`, needs no third-party packages,
and writes nothing.

**What it looks like:** five KPI tiles across the top — Capacity Purchased,
Total Capacity Used, Projected Net Overage, Days to Overage, Remaining Balance
— above a reconciliation footnote, a run-rate period selector, and a combined
chart of daily actuals (blue), forecast (orange), cumulative consumption and
the even-burn pace line, with the projected overage date marked.

> Add a screenshot at `docs/screenshot.png` and restore the image tag here if
> you want one in the README; it is omitted rather than committing a picture of
> someone's real contract figures.

---

## Why this exists

Snowsight shows contract balance under **Admin → Cost Management → Organization
Overview**, but it has no run rate and no projection, and the page is hidden
entirely for on-demand and reseller accounts. Snowflake account teams have an
internal tool that does show a projection. This reproduces the customer-visible
parts of that view from documented views, so you can check the number yourself
rather than waiting to be told it.

---

## What it shows

| Tile | Meaning |
|---|---|
| **Capacity Purchased** | Sum of `capacity` + `additional capacity` line items on the contract in force today |
| **Total Capacity Used** | Cumulative spend drawn from capacity, rollover and free-usage buckets since contract start |
| **Projected Net Overage** | Forecast spend through contract end, less the remaining balance |
| **Days to Overage** | Days from the balance date until the projection exhausts the balance |
| **Remaining Balance** | `capacity_balance + rollover_balance + free_usage_balance` |

Plus a 30/60/90/180-day run-rate selector, three projection models, and a chart
of daily actuals, forecast, cumulative consumption and the even-burn pace line.

### The reconciliation model

```
total_capacity = used_to_date + remaining_balance
used_pct       = used_to_date / total_capacity
adjustments    = total_capacity − capacity_purchased − rollover_granted − free_usage_granted
```

`adjustments` is a single reconciling residual. Snowflake applies internal
billing line items — offsets, manual adjustments, balance transfers, currency
conversion adjustments, balance expiry — that are **not exposed in any customer
view**. The residual is exact even though its breakdown is not available. In
practice it is small (often zero); if yours is large, that is worth a question
to your account team.

### The projection models

| Model | Behaviour | Use when |
|---|---|---|
| **Flat average** | Window mean, repeated forward | Default. Stable, no assumptions |
| **Seasonal replay** | Replays the window's daily pattern, preserving weekday shape | You want the forecast to look like real usage |
| **Linear trend** | Trend fitted on a deseasonalised series, slope shrunk by the fit's R² | Consumption is genuinely growing or shrinking |

Two things about **Linear trend** are deliberate and worth understanding:

- The trend is fitted **after removing weekday seasonality**. Fitting a raw line
  to a short window where weekends are quiet can produce a spurious *negative*
  slope and push the overage date later than reality — the dangerous direction.
- The slope is **scaled by the fit's R²**. On noisy, trendless data an unshrunk
  slope invents a trajectory that is not there. Shrinking means the model
  degrades gracefully to the flat average instead. A consequence: on 30 days of
  a gentle ramp it will often report *no* detectable trend. That is correct —
  30 days does not contain the signal. Use 90 or 180 days.

If another source quotes a different overage date, open **Compare projection
models** — it shows all three side by side, and the implied daily rate, which
usually explains the gap in one line.

---

## Requirements

- A **prepaid capacity contract**. On-demand accounts have no balance to draw
  down; the app says so rather than inventing numbers.
- **Not a reseller contract** — those cannot read these views at all.
- Where several organizations share a capacity contract, only the **primary
  (funding) organization** can read them.
- A role with the organization-usage grants (see below) and a warehouse. XS is
  plenty.

---

## Install

### 1. Verify access

Run [`sql/00_verify_access.sql`](sql/00_verify_access.sql). All three views
must return rows.

If they error, run [`sql/01_grants.sql`](sql/01_grants.sql):

```sql
USE ROLE ORGADMIN;
GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_USAGE_VIEWER   TO ROLE SYSADMIN;
GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_BILLING_VIEWER TO ROLE SYSADMIN;
```

> **`IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE` is not sufficient.** Several
> community guides say it covers organization usage. It does not — that grant
> covers `ACCOUNT_USAGE` only. These are separate access paths.

### 2. Deploy

**Snowsight → Projects → Streamlit → + Streamlit App**, then paste
[`streamlit_app.py`](streamlit_app.py) over the generated file and click **Run**.

No packages to add. The app charts with `altair`, which ships with Streamlit,
so nothing is fetched from PyPI or Anaconda and no external access integration
is needed on either the Warehouse or SPCS runtime.

### 3. Share

Viewers need the organization-usage grants too — a Streamlit app queries with
the **caller's** privileges, not the owner's. Granting `USAGE` on the app alone
gets them an app that renders an error. See the sharing block in
[`sql/01_grants.sql`](sql/01_grants.sql).

---

## Gotchas worth knowing before you trust the numbers

**`CONTRACT_ITEMS` does not return only the active contract.** The
documentation says it does. In practice it also returns expired contracts and
orphan free-usage grants with a `NULL` contract number. Summing every row is
the obvious implementation and it is wrong. On the account this was first
built against, summing every row overstated purchased capacity by roughly 70%
— it counted a long-expired contract — and set the contract start date two
years early, which then dragged the whole usage window with it. The app pins the contract in force today and keeps only its line
items. A single contract legitimately has several: one capacity tranche per
contract year, plus any mid-term `additional capacity` add-ons.

**Balances and usage are per-contract and per-currency.** Both
`REMAINING_BALANCE_DAILY` and `USAGE_IN_CURRENCY_DAILY` carry a row per
contract and per currency. "Newest row in the view" returns an arbitrary
contract's balance once you have more than one, and summing spend across
currencies adds unlike units. The app scopes both.

**Latency is up to 72 hours,** and figures for recent days can still move until
month-end close. The as-of date is shown on the Remaining Balance tile.

**`rebate` is a credit, not consumption.** It is excluded from the run rate.

**Currency.** Amounts are formatted in the organization's billing currency, not
assumed to be USD.

---

## Tests

The test suite is the useful part if you plan to modify this. It encodes the
reconciliation contract — what the tiles must add up to — and pins the two
projection bugs that were found and fixed during development.

```bash
pip install -r requirements-dev.txt
pytest
```

87 tests, no Snowflake connection required: `streamlit`, `altair` and
`snowflake.snowpark` are stubbed, and a fake session records SQL so the
contract and currency filters can be asserted offline.

The fixtures deliberately reproduce a **messy** `CONTRACT_ITEMS` — multiple
contracts, one expired, one NULL-contract grant — because a tidy
single-contract fixture cannot catch the bug that view actually causes.

---

## Limitations

- Projections are arithmetic on historical spend. They do not know about
  workloads you are about to onboard, a migration landing next quarter, or a
  warehouse someone is about to resize.
- Marketplace drawdown balance is shown but not included in the remaining
  balance used for the projection, since it can only be spent with
  participating providers.
- Snowflake's own **Budgets** feature is the right tool for alerting on a spend
  threshold. This is for understanding the contract position; use both.

---

## License

MIT — see [LICENSE](LICENSE).

Original work. It is not derived from any Snowflake-internal tool, and the
reconciliation model was reconstructed from the public view documentation.
