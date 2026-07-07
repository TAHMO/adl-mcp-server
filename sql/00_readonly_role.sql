-- adl-mcp-server: dedicated SELECT-only role for the MCP read surface.
-- Run ONCE against ADL's database as the DB owner / a superuser.
-- ADL stays the system of record; this role can only read.

CREATE ROLE adl_readonly LOGIN PASSWORD 'change-me';

GRANT CONNECT ON DATABASE adl TO adl_readonly;
GRANT USAGE   ON SCHEMA public TO adl_readonly;
GRANT SELECT  ON ALL TABLES IN SCHEMA public TO adl_readonly;

-- Cover tables ADL's *future* migrations create. NOTE the FOR ROLE clause:
-- default privileges only fire for objects created by the named owner, so this
-- MUST be the role ADL runs migrations as (often the compose POSTGRES_USER),
-- not the role you happen to be running this script as.
ALTER DEFAULT PRIVILEGES FOR ROLE <adl_app_owner> IN SCHEMA public
    GRANT SELECT ON TABLES TO adl_readonly;

-- Introspection used by schema() / hypertables().
GRANT USAGE ON SCHEMA timescaledb_information TO adl_readonly;

-- If ADL keeps observation tables in a non-public schema, repeat USAGE + SELECT
-- + the default-privileges block for that schema. schema() (no arg) lists them.

-- Belt-and-braces: read-only at the session level even if the connection
-- options are ever dropped.
ALTER ROLE adl_readonly SET default_transaction_read_only = on;
