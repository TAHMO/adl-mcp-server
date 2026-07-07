#!/usr/bin/env python3
"""
adl-mcp-server
Read-only MCP query surface over a WMO Automated Data Loader (ADL) TimescaleDB
— live AWS weather observations collected by ADL.

ADL is the system of record; this server NEVER writes. The schema is introspected
live (information_schema + timescaledb_information) so it survives ADL's
Django/Wagtail migrations — nothing about ADL's table layout is hardcoded here.

Tools: ping, schema, query, hypertables, stations, observations
"""
import asyncio, json, logging, os, sys
import psycopg
from psycopg.rows import dict_row
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from guards import guard_select, validate_ident

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s adl-mcp-server %(levelname)s %(message)s")
log = logging.getLogger("adl-mcp-server")

# --- config (env-driven; nothing about ADL's schema is hardcoded) ----------
DB_HOST = os.environ.get("ADL_DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("ADL_DB_PORT", "5432")
DB_NAME = os.environ.get("ADL_DB_NAME", "adl")
DB_USER = os.environ.get("ADL_DB_USER", "adl_readonly")
DB_PASS = os.environ.get("ADL_DB_PASSWORD", "")
# Docker-secret friendly: ADL_DB_PASSWORD_FILE wins if present.
_PW_FILE = os.environ.get("ADL_DB_PASSWORD_FILE")
if _PW_FILE and os.path.exists(_PW_FILE):
    with open(_PW_FILE) as fh:
        DB_PASS = fh.read().strip()

DEFAULT_LIMIT   = int(os.environ.get("ADL_DEFAULT_LIMIT", "1000"))
MAX_LIMIT       = int(os.environ.get("ADL_MAX_LIMIT", "50000"))
STMT_TIMEOUT    = os.environ.get("ADL_STMT_TIMEOUT_MS", "30000")
CONNECT_TIMEOUT = int(os.environ.get("ADL_CONNECT_TIMEOUT_S", "10"))

# Convenience-tool wiring — GROUNDED in ADL's real data model, env-overridable.
# Provenance: ADL Core Models Reference + adl/src/adl/core/models.py
#   ObservationRecord -> table `core_observationrecord` (TimescaleDB hypertable);
#     cols `time` (UTC) / `value`; FKs `station_id` / `parameter_id` / `connection_id`;
#     unique (time, station, connection, parameter). NOTE the station FK is the
#     INTEGER pk of core_station, NOT the human code — observations() joins to resolve it.
#   Station -> `core_station` (`station_id` = human code, `name`, `location` PostGIS).
#   DataParameter -> `core_dataparameter` (`name`). Hourly cagg view: `obs_agg_1h_v`.
# db_table is not customised in ADL, so these defaults hold for a standard deploy;
# override any of them via env if a deployment differs (verify with schema()).
OBS_TABLE      = os.environ.get("ADL_OBS_TABLE",        "core_observationrecord")
OBS_TIME_COL   = os.environ.get("ADL_OBS_TIME_COL",     "time")
OBS_VALUE_COL  = os.environ.get("ADL_OBS_VALUE_COL",    "value")
OBS_STATION_FK = os.environ.get("ADL_OBS_STATION_FK",   "station_id")
OBS_PARAM_FK   = os.environ.get("ADL_OBS_PARAM_FK",     "parameter_id")
STATION_TABLE  = os.environ.get("ADL_STATION_TABLE",    "core_station")
STATION_PK     = os.environ.get("ADL_STATION_PK",       "id")
STATION_CODE   = os.environ.get("ADL_STATION_CODE_COL", "station_id")
STATION_NAME   = os.environ.get("ADL_STATION_NAME_COL", "name")
PARAM_TABLE    = os.environ.get("ADL_PARAM_TABLE",      "core_dataparameter")
PARAM_PK       = os.environ.get("ADL_PARAM_PK",         "id")
PARAM_NAME     = os.environ.get("ADL_PARAM_NAME_COL",   "name")

# Read-only + time-bounded at the wire level — defence in depth on top of the
# SELECT-only role (the role is the real boundary; this just fails faster).
_CONN_OPTS = (
    f"-c default_transaction_read_only=on "
    f"-c statement_timeout={STMT_TIMEOUT} "
    f"-c idle_in_transaction_session_timeout=60000"
)

# system / timescale-internal schemas hidden from schema() and counts
_HIDDEN_SCHEMAS = ("pg_catalog", "information_schema", "_timescaledb_catalog",
                   "_timescaledb_internal", "_timescaledb_config", "timescaledb_information")

app = Server("adl-mcp-server")

SCHEMA_HEADER = (
    "ADL TimescaleDB (PostgreSQL, read-only) — live AWS weather observations.\n"
    "Schema introspected live below. For the time-series tables you actually want,\n"
    "call hypertables(): observations live in TimescaleDB hypertables, and any\n"
    "multi-month range should hit a continuous aggregate, not the raw hypertable.\n"
    "ALWAYS bound queries with a time predicate, e.g.\n"
    "  WHERE <time_col> >= now() - interval '7 days'\n"
)


def get_con():
    # Connection-per-call (cheap for a stdio server; no pool to manage or leak).
    return psycopg.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        autocommit=True, row_factory=dict_row, options=_CONN_OPTS,
        connect_timeout=CONNECT_TIMEOUT,
    )


def qj(con, sql, params=None):
    with con.cursor() as cur:
        cur.execute(sql, params or [])
        return [dict(r) for r in cur.fetchall()]


def _text(obj):
    return [TextContent(type="text", text=json.dumps(obj, indent=2, default=str))]


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="ping",
            description=(
                "Health check: confirm connectivity to the ADL TimescaleDB and report "
                "server version, TimescaleDB extension version, current user, read-only "
                "status, and table/hypertable/continuous-aggregate counts. No schema knowledge needed."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="schema",
            description=(
                "Live introspection. No args: list all tables/views (system + timescale-internal "
                "schemas excluded) with a hypertable flag. With `table`: list that table's columns."
            ),
            inputSchema={
                "type": "object",
                "properties": {"table": {"type": "string", "description": "Optional 'schema.table' (defaults schema=public)."}},
            },
        ),
        Tool(
            name="query",
            description=(
                "Execute a read-only SELECT/WITH/EXPLAIN/SHOW against the ADL TimescaleDB. "
                "Single statement only; auto-LIMIT if omitted; server-side statement_timeout. "
                "ALWAYS bound hypertable scans with a time predicate and prefer continuous "
                "aggregates (see hypertables()) for long ranges. Returns JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql":   {"type": "string"},
                    "limit": {"type": "integer", "description": f"Row cap (default {DEFAULT_LIMIT}, max {MAX_LIMIT})."},
                },
                "required": ["sql"],
            },
        ),
        Tool(
            name="hypertables",
            description=(
                "List TimescaleDB hypertables and continuous aggregates — the objects you "
                "actually want for observations. Use continuous aggregates for multi-month ranges."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="stations",
            description=(
                "List station metadata rows. Defaults to ADL's core_station; "
                "override the table via ADL_STATION_TABLE if a deployment differs."
            ),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": f"Max rows (default {DEFAULT_LIMIT})."}},
            },
        ),
        Tool(
            name="observations",
            description=(
                "Time-bounded observations joined to human-readable station code, station name "
                "and parameter name. Defaults to ADL's core schema (core_observationrecord ⋈ "
                "core_station ⋈ core_dataparameter); override any table/column via ADL_* env. "
                "Filter by station code and/or parameter name; defaults to the last 7 days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "station":   {"type": "string",  "description": "Human station code (core_station.station_id). Omit for all."},
                    "parameter": {"type": "string",  "description": "Parameter name (core_dataparameter.name), e.g. air_temperature. Omit for all."},
                    "start":     {"type": "string",  "description": "ISO timestamp lower bound (inclusive). Default: now()-7d."},
                    "end":       {"type": "string",  "description": "ISO timestamp upper bound (exclusive). Default: now()."},
                    "limit":     {"type": "integer", "description": f"Max rows (default {DEFAULT_LIMIT}, max {MAX_LIMIT})."},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "ping":
            con = get_con()
            try:
                info = qj(con, """
                    SELECT version() AS pg_version, current_user AS db_user,
                           current_database() AS db,
                           current_setting('transaction_read_only') AS read_only""")[0]
                ext = qj(con, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
                counts = qj(con, f"""
                    SELECT
                      (SELECT count(*) FROM information_schema.tables
                         WHERE table_schema NOT IN {_HIDDEN_SCHEMAS}) AS tables,
                      (SELECT count(*) FROM timescaledb_information.hypertables) AS hypertables,
                      (SELECT count(*) FROM timescaledb_information.continuous_aggregates) AS continuous_aggregates
                """)[0]
                return _text({
                    "connected": True,
                    "read_only": str(info["read_only"]).lower() in ("on", "true"),
                    "pg_version": info["pg_version"],
                    "timescaledb_version": ext[0]["extversion"] if ext else None,
                    "user": info["db_user"], "database": info["db"],
                    "target": f"{DB_HOST}:{DB_PORT}", "counts": counts,
                })
            finally:
                con.close()

        elif name == "schema":
            con = get_con()
            try:
                table = arguments.get("table")
                if not table:
                    rows = qj(con, f"""
                        SELECT t.table_schema, t.table_name, t.table_type,
                               (h.hypertable_name IS NOT NULL) AS is_hypertable
                        FROM information_schema.tables t
                        LEFT JOIN timescaledb_information.hypertables h
                          ON h.hypertable_schema = t.table_schema
                         AND h.hypertable_name   = t.table_name
                        WHERE t.table_schema NOT IN {_HIDDEN_SCHEMAS}
                        ORDER BY t.table_schema, t.table_name
                    """)
                    return [TextContent(type="text", text=SCHEMA_HEADER + "\n" + json.dumps(rows, indent=2, default=str))]
                sch, _, tbl = table.partition(".")
                if not tbl:
                    sch, tbl = "public", sch
                rows = qj(con, """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """, [sch, tbl])
                return _text(rows)
            finally:
                con.close()

        elif name == "query":
            safe = guard_select(arguments["sql"], arguments.get("limit", DEFAULT_LIMIT), MAX_LIMIT)
            con = get_con()
            try:
                rows = qj(con, safe)
                return _text({"row_count": len(rows), "sql": safe, "rows": rows})
            finally:
                con.close()

        elif name == "hypertables":
            con = get_con()
            try:
                return _text({
                    "hypertables": qj(con, """
                        SELECT hypertable_schema, hypertable_name, num_dimensions
                        FROM timescaledb_information.hypertables ORDER BY 1, 2"""),
                    "continuous_aggregates": qj(con, """
                        SELECT view_schema, view_name, materialization_hypertable_name
                        FROM timescaledb_information.continuous_aggregates ORDER BY 1, 2"""),
                })
            finally:
                con.close()

        elif name == "stations":
            tbl = validate_ident(STATION_TABLE, "ADL_STATION_TABLE")
            limit = max(1, min(int(arguments.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))
            con = get_con()
            try:
                return _text(qj(con, f"SELECT * FROM {tbl} ORDER BY 1 LIMIT {limit}"))
            finally:
                con.close()

        elif name == "observations":
            o    = validate_ident(OBS_TABLE, "ADL_OBS_TABLE")
            tcol = validate_ident(OBS_TIME_COL, "ADL_OBS_TIME_COL")
            vcol = validate_ident(OBS_VALUE_COL, "ADL_OBS_VALUE_COL")
            sfk  = validate_ident(OBS_STATION_FK, "ADL_OBS_STATION_FK")
            pfk  = validate_ident(OBS_PARAM_FK, "ADL_OBS_PARAM_FK")
            st   = validate_ident(STATION_TABLE, "ADL_STATION_TABLE")
            spk  = validate_ident(STATION_PK, "ADL_STATION_PK")
            scd  = validate_ident(STATION_CODE, "ADL_STATION_CODE_COL")
            snm  = validate_ident(STATION_NAME, "ADL_STATION_NAME_COL")
            pt   = validate_ident(PARAM_TABLE, "ADL_PARAM_TABLE")
            ppk  = validate_ident(PARAM_PK, "ADL_PARAM_PK")
            pnm  = validate_ident(PARAM_NAME, "ADL_PARAM_NAME_COL")
            limit = max(1, min(int(arguments.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))
            clauses, params = [], []
            if arguments.get("start"):
                clauses.append(f"o.{tcol} >= %s::timestamptz"); params.append(arguments["start"])
            else:
                clauses.append(f"o.{tcol} >= now() - interval '7 days'")
            if arguments.get("end"):
                clauses.append(f"o.{tcol} <  %s::timestamptz"); params.append(arguments["end"])
            if arguments.get("station"):
                clauses.append(f"s.{scd} = %s"); params.append(arguments["station"])
            if arguments.get("parameter"):
                clauses.append(f"p.{pnm} = %s"); params.append(arguments["parameter"])
            sql = (
                f"SELECT o.{tcol} AS time, s.{scd} AS station, s.{snm} AS station_name, "
                f"p.{pnm} AS parameter, o.{vcol} AS value "
                f"FROM {o} o "
                f"JOIN {st} s ON s.{spk} = o.{sfk} "
                f"JOIN {pt} p ON p.{ppk} = o.{pfk} "
                f"WHERE {' AND '.join(clauses)} "
                f"ORDER BY o.{tcol} DESC LIMIT {limit}"
            )
            con = get_con()
            try:
                return _text(qj(con, sql, params))
            finally:
                con.close()

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except psycopg.OperationalError as e:
        log.warning("connection failure: %s", str(e).strip())
        return _text({
            "error": "cannot reach ADL TimescaleDB",
            "detail": str(e).strip(),
            "target": f"{DB_HOST}:{DB_PORT}", "database": DB_NAME, "user": DB_USER,
            "hint": "Is ADL running and the port published (e.g. '127.0.0.1:5432:5432')? "
                    "Is ADL_DB_PASSWORD / ADL_DB_PASSWORD_FILE set correctly?",
        })
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    log.info("starting; target %s:%s/%s as %s", DB_HOST, DB_PORT, DB_NAME, DB_USER)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
