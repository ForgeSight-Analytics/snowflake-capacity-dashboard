-- ===========================================================================
-- The reconciliation, as a standalone query
-- ===========================================================================
-- Reproduces the dashboard's header tiles without deploying anything. Useful
-- as a Snowsight tile, as the body of an alert, or to check the app's numbers
-- against an invoice or a figure quoted by your Snowflake account team.
--
-- Two scoping decisions make this portable, and both matter:
--
--   current_contract  CONTRACT_ITEMS returns expired contracts and free-usage
--                     grants with a NULL contract number alongside the active
--                     one. Summing every row inflates purchased capacity and
--                     drags the contract start date years too early.
--
--   billing_currency  REMAINING_BALANCE_DAILY and USAGE_IN_CURRENCY_DAILY both
--                     carry a row per currency. Summing across them adds
--                     unlike units.
-- ===========================================================================

WITH current_contract AS (
    SELECT contract_number
    FROM   snowflake.organization_usage.contract_items
    WHERE  contract_number IS NOT NULL
      AND  CURRENT_DATE() BETWEEN start_date AND end_date
    QUALIFY ROW_NUMBER() OVER (ORDER BY end_date DESC) = 1
),
contract AS (
    SELECT ci.contract_number,
           MIN(ci.start_date) AS contract_start,
           MAX(ci.end_date)   AS contract_end,
           SUM(IFF(LOWER(ci.contract_item) IN ('capacity', 'additional capacity'),
                   ci.amount, 0)) AS capacity_purchased
    FROM   snowflake.organization_usage.contract_items ci
    JOIN   current_contract cc ON ci.contract_number = cc.contract_number
    GROUP  BY ci.contract_number
),
balance AS (
    SELECT rb.date AS as_of,
           rb.currency,
           rb.capacity_balance,
           rb.rollover_balance,
           rb.free_usage_balance,
           rb.marketplace_capacity_drawdown_balance AS marketplace,
           rb.on_demand_consumption_balance         AS on_demand
    FROM   snowflake.organization_usage.remaining_balance_daily rb
    JOIN   current_contract cc ON rb.contract_number = cc.contract_number
    QUALIFY ROW_NUMBER() OVER (ORDER BY rb.date DESC) = 1
),
usg AS (
    SELECT SUM(IFF(LOWER(balance_source) IN ('capacity', 'rollover', 'free usage'),
                   usage_in_currency, 0))                                       AS used_to_date,
           SUM(IFF(LOWER(balance_source) = 'rollover',   usage_in_currency, 0)) AS drawn_rollover,
           SUM(IFF(LOWER(balance_source) = 'free usage', usage_in_currency, 0)) AS drawn_free,
           SUM(IFF(LOWER(balance_source) = 'overage',    usage_in_currency, 0)) AS overage_to_date
    FROM   snowflake.organization_usage.usage_in_currency_daily
    WHERE  usage_date >= (SELECT contract_start FROM contract)
      AND  currency    =  (SELECT currency FROM balance)
),
run_rate AS (
    SELECT SUM(IFF(usage_date >= DATEADD(day, -30,  (SELECT as_of FROM balance)),
                   usage_in_currency, 0)) / 30  AS daily_30,
           SUM(IFF(usage_date >= DATEADD(day, -90,  (SELECT as_of FROM balance)),
                   usage_in_currency, 0)) / 90  AS daily_90,
           SUM(IFF(usage_date >= DATEADD(day, -180, (SELECT as_of FROM balance)),
                   usage_in_currency, 0)) / 180 AS daily_180
    FROM   snowflake.organization_usage.usage_in_currency_daily
    WHERE  currency = (SELECT currency FROM balance)
),
position AS (
    SELECT c.contract_number, b.as_of, b.currency,
           c.contract_start, c.contract_end, c.capacity_purchased,
           u.used_to_date, u.overage_to_date, b.marketplace, b.on_demand,
           b.capacity_balance + b.rollover_balance + b.free_usage_balance AS remaining,
           b.rollover_balance   + u.drawn_rollover                        AS rollover_granted,
           b.free_usage_balance + u.drawn_free                            AS free_usage_granted,
           r.daily_30, r.daily_90, r.daily_180
    FROM balance b, contract c, usg u, run_rate r
)
SELECT contract_number,
       as_of,
       currency,
       contract_start,
       contract_end,

       -- the five header tiles
       ROUND(capacity_purchased, 2)                      AS capacity_purchased,
       ROUND(used_to_date, 2)                            AS total_capacity_used,
       ROUND(100 * used_to_date
             / NULLIF(used_to_date + remaining, 0), 1)   AS used_pct,
       ROUND(remaining, 2)                               AS remaining_balance,

       -- how total capacity is made up
       ROUND(free_usage_granted, 2)                      AS free_usage,
       ROUND(rollover_granted, 2)                        AS rollover,
       ROUND(used_to_date + remaining
             - capacity_purchased - rollover_granted
             - free_usage_granted, 2)                    AS adjustments_residual,
       ROUND(used_to_date + remaining, 2)                AS total_capacity,
       ROUND(marketplace, 2)                             AS marketplace_remaining,
       ROUND(overage_to_date, 2)                         AS overage_billed_to_date,

       -- run rate and projection, per window
       ROUND(daily_30,  2)                               AS avg_daily_spend_30d,
       ROUND(daily_90,  2)                               AS avg_daily_spend_90d,
       ROUND(daily_180, 2)                               AS avg_daily_spend_180d,
       ROUND(daily_30  * 365, 0)                         AS run_rate_30d,
       ROUND(daily_180 * 365, 0)                         AS run_rate_180d,
       FLOOR(remaining / NULLIF(daily_30,  0))           AS days_to_overage_30d,
       FLOOR(remaining / NULLIF(daily_180, 0))           AS days_to_overage_180d,
       DATEADD(day, FLOOR(remaining / NULLIF(daily_30,  0))::INT, as_of) AS overage_date_30d,
       DATEADD(day, FLOOR(remaining / NULLIF(daily_180, 0))::INT, as_of) AS overage_date_180d
FROM position;
