-- Run this first, as the role that will own the dashboard.
-- All three views must return rows. If any errors with "does not exist or not
-- authorized", run 01_grants.sql.

SELECT 'CONTRACT_ITEMS'           AS view_name, COUNT(*) AS row_count
FROM   snowflake.organization_usage.contract_items
UNION ALL
SELECT 'REMAINING_BALANCE_DAILY',  COUNT(*)
FROM   snowflake.organization_usage.remaining_balance_daily
UNION ALL
SELECT 'USAGE_IN_CURRENCY_DAILY',  COUNT(*)
FROM   snowflake.organization_usage.usage_in_currency_daily;

-- Zero rows from REMAINING_BALANCE_DAILY means one of:
--   * an on-demand (pay-as-you-go) account, which has no balance to draw down
--   * a reseller contract, which cannot read these views at all
-- The dashboard detects both and says so rather than showing empty tiles.


-- Worth looking at before you deploy: how many contracts does this view
-- actually return? The docs say only the active one. In practice it also
-- returns expired contracts and free-usage grants with a NULL contract number.
SELECT contract_number, start_date, end_date, contract_item, currency, amount
FROM   snowflake.organization_usage.contract_items
ORDER  BY start_date, contract_item;


-- And how many currencies is the organization billed in? More than one means
-- the dashboard's currency scoping is doing real work.
SELECT currency, COUNT(*) AS rows_, MIN(usage_date) AS first_day, MAX(usage_date) AS last_day
FROM   snowflake.organization_usage.usage_in_currency_daily
GROUP  BY currency
ORDER  BY rows_ DESC;
