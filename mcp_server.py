#!/usr/bin/env python3
"""
adl-mcp-server
Read-only MCP query surface over a WMO Automated Data Loader (ADL) — live AWS
weather observations collected by ADL. Two ways in, either or both:

  db   direct, read-only connection to ADL's TimescaleDB (SELECT-only role)
  api  ADL's own read-only REST API (an ADL API key and/or a JWT login)

ADL is the system of record; this server NEVER writes. The schema is introspected
live (information_schema + timescaledb_information) so it survives ADL's
Django/Wagtail migrations — nothing about ADL's table layout is hardcoded here.

DB tools:  ping, schema, query, hypertables, stations, observations
API tools: api_ping, api_connections, api_station_links, api_station_link,
           api_data_parameters, api_latest, api_timeseries, api_qc_summary,
           api_qc_inspection
"""
import asyncio, json, logging, os, sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from guards import guard_select, validate_ident
from adl_api import ADLApiClient, ADLApiError, JWT_ONLY, label_values

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s adl-mcp-server %(levelname)s %(message)s")
log = logging.getLogger("adl-mcp-server")


def _read_secret(var):
    """Env var, or the contents of <var>_FILE if that is set (docker secrets)."""
    path = os.environ.get(f"{var}_FILE")
    if path and os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return os.environ.get(var, "")


# --- which ways in are enabled ----------------------------------------------
API_URL      = os.environ.get("ADL_API_URL", "").strip()
API_KEY      = _read_secret("ADL_API_KEY")
API_USER     = os.environ.get("ADL_API_USERNAME", "")
API_PASS     = _read_secret("ADL_API_PASSWORD")
API_TIMEOUT  = int(os.environ.get("ADL_API_TIMEOUT_S", "30"))
# Default: db always (v0.1 behaviour), plus api when ADL_API_URL is set.
_DEFAULT_BACKENDS = "db,api" if API_URL else "db"
BACKENDS = {b.strip().lower() for b in os.environ.get("ADL_BACKENDS", _DEFAULT_BACKENDS).split(",") if b.strip()}
_UNKNOWN = BACKENDS - {"db", "api"}
if _UNKNOWN:
    raise SystemExit(f"ADL_BACKENDS has unknown value(s) {sorted(_UNKNOWN)}; use db, api or db,api.")
if not BACKENDS:
    raise SystemExit("ADL_BACKENDS is empty; use db, api or db,api.")
if "api" in BACKENDS and not API_URL:
    raise SystemExit("ADL_BACKENDS includes 'api' but ADL_API_URL is not set.")

if "db" in BACKENDS:
    # Imported only when the DB route is on, so an API-only deploy needs no Postgres driver.
    import psycopg
    from psycopg.rows import dict_row

_api_client = None


def api():
    global _api_client
    if _api_client is None:
        _api_client = ADLApiClient(API_URL, api_key=API_KEY, username=API_USER,
                                   password=API_PASS, timeout=API_TIMEOUT)
    return _api_client


# --- DB config (env-driven; nothing about ADL's schema is hardcoded) ---------
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


def _db_tools():
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


_ID = {"type": "integer", "minimum": 1}
_MONTH_YEAR = {
    "month": {"type": "integer", "minimum": 1, "maximum": 12},
    "year":  {"type": "integer", "minimum": 1900, "maximum": 2100},
}


def _api_tools():
    return [
        Tool(
            name="api_ping",
            description=(
                "Health check for the ADL REST API route: confirms the API is reachable with the "
                "configured credentials and reports which auth modes are set (API key, JWT login) "
                "and how many network connections and data parameters ADL reports."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="api_connections",
            description=(
                "List ADL network connections (one per station network ADL pulls from, e.g. a "
                "TAHMO or Vaisala connection). Start here: station links hang off a connection."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="api_station_links",
            description=(
                "List the station links of one network connection: each has an id (the "
                "station_link_id the data tools need) plus station name, WIGOS id, location and height."
            ),
            inputSchema={"type": "object", "properties": {"connection_id": _ID}, "required": ["connection_id"]},
        ),
        Tool(
            name="api_station_link",
            description=(
                "Detail for one station link: station metadata, the parameters it reports, their "
                "categories, and the earliest and latest observation times held."
            ),
            inputSchema={"type": "object", "properties": {"station_link_id": _ID}, "required": ["station_link_id"]},
        ),
        Tool(
            name="api_data_parameters",
            description=(
                "List ADL data parameters (id, name, unit, category) and the parameter categories. "
                "With connection_id, only the parameters that connection's stations map."
            ),
            inputSchema={"type": "object", "properties": {"connection_id": _ID}},
        ),
        Tool(
            name="api_latest",
            description=(
                "Latest observation for each parameter at a station link, labelled with parameter names."
            ),
            inputSchema={"type": "object", "properties": {"station_link_id": _ID}, "required": ["station_link_id"]},
        ),
        Tool(
            name="api_timeseries",
            description=(
                "Observations for a station link grouped by time, labelled with parameter names, "
                "newest first, one page at a time. ADL's defaults: start = 24 h ago, end = start + "
                "30 days (never later than now). Optional category filters by parameter category "
                "(see api_data_parameters). Page through long ranges rather than widening them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "station_link_id": _ID,
                    "start":    {"type": "string", "description": "ISO 8601 start, e.g. 2026-10-01T00:00:00Z."},
                    "end":      {"type": "string", "description": "ISO 8601 end."},
                    "category": {"type": "string", "description": "Parameter category id, e.g. TMP."},
                    "page":     {"type": "integer", "minimum": 1, "description": "Page number (default 1)."},
                    "limit":    {"type": "integer", "minimum": 1, "maximum": 1000,
                                 "description": "Time steps per page (default 200, ADL max 1000)."},
                },
                "required": ["station_link_id"],
            },
        ),
        Tool(
            name="api_qc_summary",
            description=(
                "ADL quality-control summary per station for one month: record counts and the share "
                "that passed, were suspect, failed or were not evaluated. Defaults to the latest month "
                "with data. Needs a JWT login (ADL_API_USERNAME/PASSWORD); API keys are refused by ADL here."
            ),
            inputSchema={"type": "object", "properties": dict(_MONTH_YEAR)},
        ),
        Tool(
            name="api_qc_inspection",
            description=(
                "QC detail for one station and month: each value with its QC status, plus the QC flag "
                "messages. station_id is ADL's station id (from api_station_links → station.id), not the "
                "station link id. Needs a JWT login. Returns at most `limit` records."
            ),
            inputSchema={
                "type": "object",
                "properties": {"station_id": _ID, **_MONTH_YEAR,
                               "limit": {"type": "integer", "minimum": 1,
                                         "description": f"Max records returned (default {DEFAULT_LIMIT})."}},
                "required": ["station_id", "month", "year"],
            },
        ),
    ]


@app.list_tools()
async def list_tools():
    tools = []
    if "db" in BACKENDS:
        tools += _db_tools()
    if "api" in BACKENDS:
        tools += _api_tools()
    return tools


def _api_call(name, arguments):
    """Dispatch one api_* tool. Pure function of the client, for testability."""
    c = api()
    if name == "api_ping":
        conns = c.get("connections") or []
        params = c.get("data_parameters") or {}
        return {"reachable": True, "base_url": c.base_url, "auth_modes": c.auth_modes(),
                "qc_tools_available": c.has_jwt,
                "network_connections": len(conns),
                "data_parameters": len(params.get("data_parameters", []))}
    if name == "api_connections":
        return c.get("connections")
    if name == "api_station_links":
        return c.get("station_links", connection_id=arguments["connection_id"])
    if name == "api_station_link":
        return c.get("station_link", station_link_id=arguments["station_link_id"])
    if name == "api_data_parameters":
        if arguments.get("connection_id"):
            return c.get("connection_params", connection_id=arguments["connection_id"])
        return c.get("data_parameters")
    if name == "api_latest":
        data = c.get("latest", station_link_id=arguments["station_link_id"])
        data["data"] = label_values(data.get("data", []), c.parameter_names())
        return data
    if name == "api_timeseries":
        limit = max(1, min(int(arguments.get("limit") or 200), 1000))
        page = max(1, int(arguments.get("page") or 1))
        data = c.get("timeseries", station_link_id=arguments["station_link_id"],
                     params={"start_date": arguments.get("start"), "end_date": arguments.get("end"),
                             "category": arguments.get("category"),
                             "paginate": "true", "page": page, "limit": limit})
        data["results"] = label_values(data.get("results", []), c.parameter_names())
        return data
    if name == "api_qc_summary":
        return c.get("qc_summary", params={"month": arguments.get("month"), "year": arguments.get("year")})
    if name == "api_qc_inspection":
        data = c.get("qc_inspection", station_id=arguments["station_id"],
                     params={"month": arguments["month"], "year": arguments["year"]})
        limit = max(1, min(int(arguments.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        records = data.get("records", [])
        data["record_count"] = len(records)
        data["records"] = records[:limit]
        data["truncated"] = len(records) > limit
        return data
    raise ValueError(f"Unknown tool: {name}")


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    arguments = arguments or {}
    if name.startswith("api_"):
        if "api" not in BACKENDS:
            return [TextContent(type="text", text="Error: the ADL API route is off (set ADL_API_URL / ADL_BACKENDS).")]
        try:
            return _text(_api_call(name, arguments))
        except ADLApiError as e:
            log.warning("ADL API failure: %s", e)
            hint = None
            if e.status in (401, 403):
                hint = ("Check ADL_API_KEY (created in ADL admin under API Keys) or ADL_API_USERNAME/"
                        "ADL_API_PASSWORD." + (" QC tools need the JWT login; ADL refuses API keys there."
                                               if name.replace("api_", "") in JWT_ONLY else ""))
            elif e.status == 404:
                hint = "ADL found no such object, or no observations for these filters."
            return _text({"error": str(e), "status": e.status, "detail": e.detail, "hint": hint})
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {str(e)}")]
    if "db" not in BACKENDS:
        return [TextContent(type="text", text="Error: the database route is off (ADL_BACKENDS=api).")]
    return await _call_db_tool(name, arguments)


async def _call_db_tool(name: str, arguments: dict):
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
    if "db" in BACKENDS:
        log.info("db route on; target %s:%s/%s as %s", DB_HOST, DB_PORT, DB_NAME, DB_USER)
    if "api" in BACKENDS:
        modes = [m for m, ok in (("api_key", API_KEY), ("jwt", API_USER and API_PASS)) if ok]
        log.info("api route on; target %s; auth %s", API_URL, modes or "NONE SET")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
