# Changelog — adl-mcp-server

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project is pre-1.0.

## [0.2.0] — 2026-10-21

ADL REST API integration: a second, read-only way into ADL, alongside the direct
TimescaleDB connection. A national service can now hand out an ADL API key instead of
database credentials.

### Added
- `adl_api.py` — stdlib-only client for ADL's API (no new dependencies). API key
  (`Authorization: Api-Key …`) and JWT login (`POST /api/token/`, re-login on expiry).
- Nine `api_*` tools: `api_ping`, `api_connections`, `api_station_links`,
  `api_station_link`, `api_data_parameters`, `api_latest`, `api_timeseries` (paged),
  `api_qc_summary`, `api_qc_inspection`. Values are labelled with parameter names
  while keeping ADL's id-keyed originals.
- `ADL_BACKENDS` (`db` / `api` / `db,api`), `ADL_API_URL`, `ADL_API_KEY(_FILE)`,
  `ADL_API_USERNAME`, `ADL_API_PASSWORD(_FILE)`, `ADL_API_TIMEOUT_S`.
- `guards.api_path` / `guards.validate_base_url`: endpoint allow-list, integer-only
  path ids, base-URL validation.
- `tests/test_adl_api.py`: a stub ADL that copies v0.8.17's response shapes and auth
  rules (API key refused on QC endpoints; JWT expiry), plus the server in api-only mode.

### Changed
- `psycopg` is imported only when the db route is on, so an api-only deploy needs no
  Postgres driver.
- `mcp` pinned below 2 (`mcp>=1.2,<2`): the 2.x SDK renames parts of the server API.
- Default behaviour unchanged: with `ADL_API_URL` unset the server is db-only, as in 0.1.0.

### Grounding
- Endpoints, auth and response shapes from ADL **v0.8.17**: `adl/src/adl/config/urls.py`,
  `adl/src/adl/api/{urls,views,auth,serializers,pagination,utils}.py`,
  `adl/src/adl/viewer/views.py`. The QC endpoints are `IsAuthenticated` only, so they
  need the login; every other endpoint takes the API key.
- The db-route `core_*` defaults were re-checked against v0.8.17 and still hold.

### Verified
- Unit + stub: 36/36 (11 existing + 25 new), with no Postgres driver installed —
  which also shows the api-only import path is clean.
- Server loaded in db, api and db,api modes; tool lists and misconfiguration errors
  checked.
- **Not re-run:** the dockerised DB integration smoke (Docker unavailable on the build
  host this time); the db code path is unchanged apart from moving into its own function.

### Not yet done (requires a live ADL instance)
- Run DEPLOY.md Step 4b against a real ADL with a real API key.

## [0.1.0] — 2026-06-20

Initial release. A read-only MCP server over a WMO Automated Data Loader (ADL)
TimescaleDB, exposing live AWS weather observations to an LLM agent.

### Added
- `mcp_server.py` — MCP server (stdio). Tools: `ping`, `schema`, `query`,
  `hypertables`, `stations`, `observations`.
- `guards.py` — pure, unit-tested read-only / identifier guards.
- Layered read-only posture: SELECT-only DB role + session
  `default_transaction_read_only` + single-statement/keyword guard.
- Operational hardening: `ping` health tool, connect-timeout, docker-secret
  password file (`ADL_DB_PASSWORD_FILE`), stderr logging, friendly connection errors.
- `Dockerfile`, `docker-compose.test.yml`, `tests/` (unit + grounded fixture +
  container integration smoke), `Register-AdlMcp.ps1`, `sql/00_readonly_role.sql`.
- `README.md`, `DEPLOY.md`, `.env.example`, `LICENSE` (MIT).

### Grounding
- Tool defaults and the test fixture are grounded in ADL's real data model
  (ADL Core Models Reference + `adl/src/adl/core/models.py`):
  `core_observationrecord` (hypertable; `time`/`value`; integer FKs
  `station_id`/`parameter_id`/`connection_id`), `core_station` (human `station_id`),
  `core_dataparameter`, hourly continuous aggregate `obs_agg_1h_v`.
- `observations` joins obs→station→parameter so callers filter by the human
  station code / parameter name (the obs `station_id` is an integer FK, not the code).

### Verified
- Unit: 11/11 (read-only enforcement, multi-statement rejection, LIMIT capping,
  identifier validation).
- Integration: end-to-end smoke green against `timescale/timescaledb-ha:pg15`
  seeded with the grounded mini-ADL fixture (TimescaleDB 2.27.2) — ping, live
  introspection, write rejection (bare + CTE-hidden), observations join.

### Not yet done (requires a live ADL instance)
- Register with an MCP client (`Register-AdlMcp.ps1` / client config).
- Confirm the `core_*` defaults against the target deployment via `ping`/`schema`.
