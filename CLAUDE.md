# adl-mcp-server — context for Claude Code

## What this is
A **read-only** MCP server over a WMO **Automated Data Loader (ADL)** TimescaleDB —
live AWS weather observations. ADL is the system of record. **This server never
writes** — do not add write tools, and do not let it become a source of truth or a
cache. It is a read surface only.

## Layout
| File | Role |
|---|---|
| `mcp_server.py` | MCP server (low-level `Server`, stdio). Tools: ping, schema, query, hypertables, stations, observations. |
| `guards.py` | Pure read-only / identifier guards. **Unit-tested — keep it dependency-free.** |
| `sql/00_readonly_role.sql` | SELECT-only role for ADL's DB (run once). |
| `Dockerfile` / `docker-compose.test.yml` | Container deploy + the integration harness. |
| `tests/` | `test_guards.py` (no DB), `fixtures/seed_fixture.sql` (grounded mini-ADL), `integration_smoke.py`. |
| `Register-AdlMcp.ps1` | `claude mcp add/remove` wrapper (PowerShell). |
| `README.md` / `DEPLOY.md` / `CHANGELOG.md` / `LICENSE` | Docs + grounded data model + runbook + MIT. |

## Conventions
- **Read-only is layered**: SELECT-only role → session `default_transaction_read_only`
  → single-statement/keyword guard. Don't weaken any layer.
- **Schema is grounded, not guessed.** Defaults (`core_observationrecord`, `core_station`,
  `core_dataparameter`, cagg `obs_agg_1h_v`) come from ADL's Core Models Reference +
  `adl/src/adl/core/models.py`. All are `ADL_*` env-overridable. If you change a default,
  cite the source. The obs `station_id` is an **integer FK** — `observations` joins to
  resolve the human code; preserve that join.
- Connection-per-call; errors returned as `Error: …` text; `json.dumps(..., default=str)`.

## Working here
```bash
python -m unittest discover -s tests -p "test_*.py"                       # logic, no DB
docker compose -f docker-compose.test.yml up --build \
  --abort-on-container-exit --exit-code-from smoke                        # full e2e
docker compose -f docker-compose.test.yml down -v
```
Deploy/registration is in `DEPLOY.md`.

## Status
v0.1.0 — built and tested without a live ADL (unit + dockerised integration green).
Pending a real instance: registration with an MCP client and a `ping`/`schema`
confirmation that the `core_*` defaults match that deployment.
