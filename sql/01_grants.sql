-- Only needed if 00_verify_access.sql failed.
--
-- IMPORTANT: ORGANIZATION_USAGE is NOT covered by
--   GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE
-- despite what several community guides say. That grant covers ACCOUNT_USAGE
-- only. Organization usage needs the database roles below, granted by ORGADMIN.

USE ROLE ORGADMIN;

GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_USAGE_VIEWER   TO ROLE SYSADMIN;
GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_BILLING_VIEWER TO ROLE SYSADMIN;

-- On a newer organization account, application roles are used instead:
--   GRANT APPLICATION ROLE SNOWFLAKE.ORGANIZATION_BILLING_VIEWER TO ROLE SYSADMIN;
--   GRANT APPLICATION ROLE SNOWFLAKE.ORG_USAGE_ADMIN            TO ROLE SYSADMIN;

-- Replace SYSADMIN with whichever role will own and run the app.


-- ---------------------------------------------------------------------------
-- Sharing the deployed app
-- ---------------------------------------------------------------------------
-- Viewers need the organization-usage grants too: a Streamlit app queries with
-- the caller's privileges, not the owner's. Granting USAGE on the app alone
-- gets them an app that renders the "cannot read ORGANIZATION_USAGE" error.

-- USE ROLE SYSADMIN;
-- GRANT USAGE ON DATABASE  <db>                      TO ROLE FINANCE_VIEWER;
-- GRANT USAGE ON SCHEMA    <db>.<schema>             TO ROLE FINANCE_VIEWER;
-- GRANT USAGE ON WAREHOUSE <warehouse>               TO ROLE FINANCE_VIEWER;
-- GRANT USAGE ON STREAMLIT <db>.<schema>.<app_name>  TO ROLE FINANCE_VIEWER;
-- USE ROLE ORGADMIN;
-- GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_USAGE_VIEWER TO ROLE FINANCE_VIEWER;
