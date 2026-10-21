# adl-mcp-server — deployment runbook

Read-only MCP over a WMO **Automated Data Loader (ADL)**. ADL is the system of record;
this server only ever reads. It reaches ADL one of two ways, or both:

| Route | Needs | Best when |
|---|---|---|
| **db** — direct TimescaleDB | a SELECT-only Postgres role and network reach to ADL's DB | you run the ADL host, or want free-form SQL and the hourly aggregate |
| **api** — ADL's REST API *(v0.2.0)* | an ADL API key (and, for QC, an ADL login) and HTTPS reach to the ADL site | a national service will share an API key but not database access; or the server runs off-site |

Choose with `ADL_BACKENDS` (`db`, `api` or `db,api`). Default: `db`, plus `api` when
`ADL_API_URL` is set — so a v0.1.0 deployment behaves exactly as before.

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

## ADL REST API (grounded — what the api_* tools call)

Source: ADL **v0.8.17** — `adl/src/adl/config/urls.py`, `adl/src/adl/api/{urls,views,auth,
serializers,pagination}.py`, `adl/src/adl/viewer/views.py`. Every data endpoint is a GET.

| Tool | ADL endpoint | ADL auth |
|---|---|---|
| `api_connections` | `GET /api/network-connection/` | API key or login |
| `api_station_links(connection_id)` | `GET /api/network-connection/{id}/station-links/` | API key or login |
| `api_data_parameters(connection_id?)` | `GET /api/data-parameters/` · `GET /api/network-connection/{id}/data-parameters/` | API key or login |
| `api_station_link(station_link_id)` | `GET /api/station-link/{id}/` | API key or login |
| `api_latest(station_link_id)` | `GET /api/data/latest/{id}/` | API key or login |
| `api_timeseries(station_link_id, start?, end?, category?, page?, limit?)` | `GET /api/data/timeseries/{id}/?paginate=true` | API key or login |
| `api_qc_summary(month?, year?)` | `GET /api/qc/summary/` | **login only** |
| `api_qc_inspection(station_id, month, year)` | `GET /api/qc/inspection/{station_id}/` | **login only** |
| `api_ping` | the first two calls above | — |

How ADL authenticates:
- **API key** → header `Authorization: Api-Key <key>` (the `djangorestframework-api-key`
  default; ADL sets no custom header). Create one in ADL's Django admin at
  `/debug/django-admin/` → **API Keys**, or in `manage.py shell`
  (`from rest_framework_api_key.models import APIKey; APIKey.objects.create_key(name="adl-mcp")`).
  The key is shown once.
- **Login (JWT)** → `POST /api/token/` with an ADL username and password returns an access
  token, sent as `Authorization: Bearer …`. The QC endpoints accept nothing else. Use a
  **dedicated ADL user with no editing permissions**: the password is also an ADL admin
  login. The server logs in again by itself when the token expires.

Things the API route deliberately does not do: free-form SQL, the hourly continuous
aggregate, or station lookup by human code — use the db route for those. Data tools take
ADL's integer **station link** id (from `api_station_links`), except `api_qc_inspection`,
which takes ADL's **station** id (`station.id` in the same response). Values come back
keyed by parameter id as ADL sends them, plus a `values` / `parameter` copy keyed by name.

> Verified against ADL's source at v0.8.17 and a stub that copies its responses and auth
> rules (`tests/test_adl_api.py`). **Not yet run against a live ADL instance** — do that
> first (Step 4b).

---

## Tools

DB route: `ping` · `schema(table?)` · `query(sql, limit?)` · `hypertables()` ·
`stations(limit?)` · `observations(station?, parameter?, start?, end?, limit?)`

API route: `api_ping` · `api_connections` · `api_station_links` · `api_station_link` ·
`api_data_parameters` · `api_latest` · `api_timeseries` · `api_qc_summary` · `api_qc_inspection`

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

**API route only** (no database access needed, no Postgres driver imported):
```bash
claude mcp add adl-mcp-server \
  -e ADL_BACKENDS=api -e ADL_API_URL=https://adl.example.org \
  -e ADL_API_KEY=*** \
  -e ADL_API_USERNAME=adl-reader -e ADL_API_PASSWORD=*** \
  -- python /abs/path/to/adl-mcp-server/mcp_server.py
```
`ADL_API_URL` is the site root (no trailing `/api`). Omit the username/password if you do
not need the two QC tools. `ADL_API_KEY_FILE` / `ADL_API_PASSWORD_FILE` read secrets
from files instead.

## Step 4 — verify

Restart the client (MCP servers connect on startup), then call `ping` (expect
`connected: true`, `read_only: true`, a `timescaledb_version`, non-zero `hypertables`),
then `hypertables()` and a small `observations(station=…, limit=5)`.

**Step 4b — API route.** Call `api_ping` (expect `reachable: true`, the auth modes you
set, non-zero `network_connections`). Then `api_connections` → `api_station_links` →
`api_latest(station_link_id=…)`, and one `api_timeseries` for the last day. If you set a
login, `api_qc_summary()`. Confirm one known-bad request is refused, for example an API
key you revoked (expect status 403).

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
needed (`DROP ROLE adl_readonly;` after revoking grants). For the API route, revoke the
API key in ADL's admin (**API Keys** → revoked) and deactivate the dedicated ADL user.
To go back to v0.1.0 behaviour without uninstalling, unset `ADL_API_URL` (or set
`ADL_BACKENDS=db`). Nothing in ADL is mutated by this server.
