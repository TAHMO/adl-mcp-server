# adl-mcp-server — deployment runbook

Read-only MCP over a WMO **Automated Data Loader (ADL)** TimescaleDB. ADL is the
system of record; this server only ever reads.

---

## ADL data model (grounded — what the tools target)

Source: ADL **Core Models Reference** + `adl/src/adl/core/models.py`
(`github.com/wmo-raf/adl`). `db_table` is not customised, so these `core_*` names
hold for a standard ADL deploy.

| Entity | Postgres table | Key columns |
|---|---|---|
| ObservationRecord *(hypertable)* | `core_observationrecord` | `time` (UTC), `value`, FK `station_id`→`core_station.id`, FK `parameter_id`→`core_dataparameter.id`, FK `connection_id`, `is_daily`, `qc_status`; unique `(time, station, connection, parameter)` |
| Station | `core_station` | `id` (PK), `station_id` (**human code**), `name`, `location` (PostGIS Point), WIGOS/WMO ids, heights |
| DataParameter | `core_dataparameter` | `id`, `name`, `unit_id`, `aggregation_method` |
| Unit | `core_unit` | `id`, `name`, `symbol` |
| Network | `core_network` | `id`, `name`, `type` |
| HourlyObsAgg *(continuous aggregate)* | view `obs_agg_1h_v` | `bucket`, `min/max/avg/sum_value`, `records_count`, FK station/connection/parameter |

> **The trap this avoids:** `core_observationrecord.station_id` is the *integer FK*,
> not the human code (which lives on `core_station.station_id`). The `observations`
> tool joins obs→station→parameter so you filter by the human code and get readable rows.

ADL ships on `timescale/timescaledb-ha:pg15` (TimescaleDB + PostGIS). None of ADL's
ingest/dispatch path is in this server's scope — it reads the DB only.

---

## Tools

`ping` · `schema(table?)` · `query(sql, limit?)` · `hypertables()` ·
`stations(limit?)` · `observations(station?, parameter?, start?, end?, limit?)`

Defaults are grounded (above) and overridable via `ADL_*` env (see `.env.example`).

---

## Prerequisites

- A running ADL stack with its TimescaleDB (`adl_db`) container.
- Network reach from where the server runs to that DB (see the two deploy modes below).
- Python 3.11+ with `mcp` + `psycopg` (`pip install -r requirements.txt`), unless using the container deploy.

## Step 1 — create the read-only role (once, in ADL's DB)

```bash
psql -h 127.0.0.1 -U <adl_owner> -d adl -f sql/00_readonly_role.sql
```
Edit `<adl_app_owner>` and the password in that file first. (The `FOR ROLE` clause
must name the role ADL runs migrations as, or future tables won't be readable.)

## Step 2 — pick a deploy mode

**Mode A — host-launched stdio (simplest).** Bind ADL's DB port to loopback only:
```yaml
# in ADL's docker-compose.yml
adl_db:
  image: timescale/timescaledb-ha:pg15
  ports: ["127.0.0.1:5432:5432"]
```
Then configure the client with `ADL_DB_HOST=127.0.0.1`.

**Mode B — container on ADL's docker network (no port exposed).** Build and run on
ADL's compose network:
```bash
docker build -t adl-mcp-server .
# the client launches:  docker run -i --rm --network <adl_network> -e ADL_DB_HOST=adl_db ... adl-mcp-server
```

## Step 3 — register with your MCP client

**Claude Code** (CLI):
```bash
claude mcp add adl-mcp-server \
  -e ADL_DB_HOST=127.0.0.1 -e ADL_DB_PORT=5432 -e ADL_DB_NAME=adl \
  -e ADL_DB_USER=adl_readonly -e ADL_DB_PASSWORD=*** \
  -- python /abs/path/to/adl-mcp-server/mcp_server.py
```
A Windows/PowerShell wrapper is provided: `./Register-AdlMcp.ps1 -DbPassword '***'`.

**Claude Desktop / generic MCP client** (`mcpServers` config):
```json
{
  "mcpServers": {
    "adl-mcp-server": {
      "command": "python",
      "args": ["/abs/path/to/adl-mcp-server/mcp_server.py"],
      "env": {
        "ADL_DB_HOST": "127.0.0.1",
        "ADL_DB_NAME": "adl",
        "ADL_DB_USER": "adl_readonly",
        "ADL_DB_PASSWORD": "***"
      }
    }
  }
}
```

## Step 4 — verify

Restart the client (MCP servers connect on startup), then call `ping` (expect
`connected: true`, `read_only: true`, a `timescaledb_version`, non-zero `hypertables`),
then `hypertables()` and a small `observations(station=…, limit=5)`.

---

## Test without a real ADL (CI-grade)

Unit tests (no DB/deps):
```bash
python -m unittest discover -s tests -p "test_*.py"
```
End-to-end against a throwaway TimescaleDB seeded with the grounded mini-ADL fixture:
```bash
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from smoke
docker compose -f docker-compose.test.yml down -v
```
A green `smoke` run exercises the real tool dispatch against ADL's actual `core_*`
schema (ping, live introspection, the read-only guard, and the observations join).

A GitHub Actions workflow can run both with no extra setup — the integration job just
needs Docker, which `ubuntu-latest` provides.

---

## Rollback

Remove the server from your client (e.g. `claude mcp remove adl-mcp-server`, or delete
the `mcpServers` entry). The read-only role can be dropped in ADL's DB if no longer
needed (`DROP ROLE adl_readonly;` after revoking grants). Nothing in ADL is mutated by
this server.
