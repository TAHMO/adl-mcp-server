# adl-mcp-server

**A read-only [Model Context Protocol](https://modelcontextprotocol.io) server over a
WMO [Automated Data Loader (ADL)](https://github.com/wmo-raf/adl)** — it lets an LLM agent
query live AWS weather observations that ADL has collected, either straight from ADL's
TimescaleDB or **through ADL's own REST API** (new in v0.2.0).

![license](https://img.shields.io/badge/license-MIT-blue) ·
`v0.2.0` · status: built & tested without a live ADL (unit + API stub green; dockerised DB integration green at v0.1.0)

ADL is the system of record; this server **never writes**. The schema is introspected
live, and the tool defaults are **grounded in ADL's real data model** (ADL Core Models
Reference + `adl/src/adl/core/models.py`) rather than guessed.

---

## What it does

ADL ingests observations from AWS networks into a TimescaleDB hypertable and dispatches
them downstream (wis2box / CDMS / FTP). This server adds a safe, read-only query path so
an agent can ask questions of that local store:

```
AWS vendors ──pull (Celery)──▶ ADL ──▶ TimescaleDB (core_observationrecord)
                                │             │ db route: read-only (SELECT-only role)
                                │ /api/ (GET) │
                                │ api route:  │
                                │ API key/JWT ▼
                                └────────▶ adl-mcp-server ──▶ MCP client (Claude, etc.)
```

Pick the route with `ADL_BACKENDS` (`db`, `api` or `db,api`). The **api route** suits a
national service that will issue an ADL API key but not database credentials, or a server
running away from the ADL host. The **db route** keeps free-form SQL and the hourly
aggregate. With no new settings the server behaves exactly as v0.1.0 (db only).

## Tools

### API route (`ADL_API_URL` set) — new in v0.2.0

| Tool | Purpose |
|---|---|
| `api_ping()` | Reachability, auth modes in use, connection and parameter counts |
| `api_connections()` | ADL network connections (one per station network ADL pulls from) |
| `api_station_links(connection_id)` | Stations on a connection, with the station link id the data tools take |
| `api_station_link(station_link_id)` | Station detail, reported parameters, earliest and latest observation |
| `api_data_parameters(connection_id?)` | Parameters with units and categories |
| `api_latest(station_link_id)` | Latest value per parameter, labelled by name |
| `api_timeseries(station_link_id, start?, end?, category?, page?, limit?)` | Paged time series, labelled by name |
| `api_qc_summary(month?, year?)` | ADL QC pass/suspect/fail shares per station *(needs an ADL login)* |
| `api_qc_inspection(station_id, month, year)` | Per-value QC status and flag messages *(needs an ADL login)* |

### DB route

| Tool | Purpose |
|---|---|
| `ping()` | Connectivity + server/TimescaleDB version, read-only status, table/hypertable/cagg counts |
| `schema(table?)` | Live table/view list (hypertables flagged), or one table's columns |
| `query(sql, limit?)` | Guarded read-only SELECT/WITH/EXPLAIN/SHOW — single statement, auto-LIMIT |
| `hypertables()` | Hypertables + continuous aggregates |
| `stations(limit?)` | Station metadata (defaults to `core_station`) |
| `observations(station?, parameter?, start?, end?, limit?)` | Observations joined to human station code, station name, and parameter name (defaults to the last 7 days) |

Tool defaults target ADL's documented schema (`core_observationrecord` `time`/`value`,
`core_station` human `station_id`, `core_dataparameter` `name`, hourly continuous
aggregate `obs_agg_1h_v`) and are overridable via `ADL_*` env vars — see
[`.env.example`](.env.example). Full data model + the integer-FK join rationale are in
[`DEPLOY.md`](DEPLOY.md).

## Safety model

Read-only is layered, so a bug in any one layer is not sufficient to write:

**DB route**
1. A dedicated **SELECT-only Postgres role** ([`sql/00_readonly_role.sql`](sql/00_readonly_role.sql)).
2. Every connection runs with `default_transaction_read_only = on` + `statement_timeout`.
3. The `query` tool enforces single-statement, SELECT/WITH/EXPLAIN/SHOW-only, with a write/DDL keyword scan and auto-`LIMIT`.

**API route**
1. ADL's API exposes only GET data endpoints (ADL v0.8.17).
2. The client can call only an allow-list of those endpoint templates — no free-form
   paths — and path ids must be positive integers ([`guards.py`](guards.py)).
3. The only non-GET request is the login that exchanges an ADL username/password for a
   token. Use a dedicated ADL user with no editing permissions.

## Install

```bash
pip install -r requirements.txt          # mcp + psycopg[binary]  (Python 3.11+)
                                         # api-only deploys need only mcp
```

## Configure

Set connection via environment (see [`.env.example`](.env.example)):

| Var | Default | Notes |
|---|---|---|
| `ADL_BACKENDS` | `db` (+`api` if `ADL_API_URL` set) | `db`, `api` or `db,api` |
| `ADL_API_URL` | — | ADL site root, e.g. `https://adl.example.org` (no `/api`) |
| `ADL_API_KEY` / `ADL_API_KEY_FILE` | — | ADL API key (all data tools) |
| `ADL_API_USERNAME` / `ADL_API_PASSWORD` (`_FILE`) | — | ADL login; needed only for the two QC tools |
| `ADL_API_TIMEOUT_S` | `30` | per-request timeout |
| `ADL_DB_HOST` / `ADL_DB_PORT` | `127.0.0.1` / `5432` | ADL TimescaleDB |
| `ADL_DB_NAME` | `adl` | database |
| `ADL_DB_USER` / `ADL_DB_PASSWORD` | `adl_readonly` / — | the SELECT-only role |
| `ADL_DB_PASSWORD_FILE` | — | read password from a file (docker secrets) |
| `ADL_DEFAULT_LIMIT` / `ADL_MAX_LIMIT` | `1000` / `50000` | query row caps |
| `ADL_*` table/column overrides | grounded defaults | only if a deployment customises `db_table` |

## Quick start

```bash
# logic tests — no DB needed
python -m unittest discover -s tests -p "test_*.py"

# full end-to-end against a throwaway TimescaleDB seeded with a grounded mini-ADL fixture
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from smoke
docker compose -f docker-compose.test.yml down -v
```

Register with an MCP client and deploy against a real ADL: see [`DEPLOY.md`](DEPLOY.md).

## Notes

- **Read surface only — no mirror / cache.** ADL remains canonical.
- Requires a running ADL instance: its TimescaleDB reachable (db route) or its site
  reachable over HTTPS (api route).
- See [`CHANGELOG.md`](CHANGELOG.md) for version history.

## License

MIT — see [`LICENSE`](LICENSE). Built as a companion to WMO RAF's ADL (also MIT).
