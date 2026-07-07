# adl-mcp-server

**A read-only [Model Context Protocol](https://modelcontextprotocol.io) server over a
WMO [Automated Data Loader (ADL)](https://github.com/wmo-raf/adl) TimescaleDB** — it lets
an LLM agent query live AWS weather observations that ADL has collected.

![license](https://img.shields.io/badge/license-MIT-blue) ·
`v0.1.0` · status: built & tested without a live ADL (unit + dockerised integration green)

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
                                              │ read-only (SELECT-only role)
                                              ▼
                                       adl-mcp-server ──▶ MCP client (Claude, etc.)
```

## Tools

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

1. A dedicated **SELECT-only Postgres role** ([`sql/00_readonly_role.sql`](sql/00_readonly_role.sql)).
2. Every connection runs with `default_transaction_read_only = on` + `statement_timeout`.
3. The `query` tool enforces single-statement, SELECT/WITH/EXPLAIN/SHOW-only, with a write/DDL keyword scan and auto-`LIMIT`.

## Install

```bash
pip install -r requirements.txt          # mcp + psycopg[binary]  (Python 3.11+)
```

## Configure

Set connection via environment (see [`.env.example`](.env.example)):

| Var | Default | Notes |
|---|---|---|
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
- Requires a running ADL instance with its TimescaleDB reachable.
- See [`CHANGELOG.md`](CHANGELOG.md) for version history.

## License

MIT — see [`LICENSE`](LICENSE). Built as a companion to WMO RAF's ADL (also MIT).
