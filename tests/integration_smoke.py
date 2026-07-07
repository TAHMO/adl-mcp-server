#!/usr/bin/env python3
"""End-to-end smoke test for adl-mcp-server against a live TimescaleDB seeded with the
grounded mini-ADL fixture. Exercises the REAL tool dispatch (call_tool), so it
validates psycopg wiring, the read-only guards, live introspection, and the
observations join — all against ADL's actual core_* table/column names.

Exit 0 = pass (or SKIP if no DB reachable); exit 1 = a check failed.
Usually run via docker-compose.test.yml (see DEPLOY.md), which provides the DB.
"""
import asyncio, json, os, sys, time

try:
    import psycopg
except ModuleNotFoundError:
    print("SKIP: psycopg not installed — run via docker-compose.test.yml.")
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _txt(res):
    return res[0].text


async def run(srv):
    out = []

    p = json.loads(_txt(await srv.call_tool("ping", {})))
    assert p.get("connected") is True, p
    assert p.get("read_only") is True, p
    assert p.get("timescaledb_version"), p
    assert p["counts"]["hypertables"] >= 1, p
    out.append(f"ping: connected, read_only, timescaledb {p['timescaledb_version']}, "
               f"{p['counts']['hypertables']} hypertable(s)")

    s = _txt(await srv.call_tool("schema", {}))
    assert "core_observationrecord" in s and "core_station" in s, s[:400]
    out.append("schema: core_observationrecord + core_station present")

    h = json.loads(_txt(await srv.call_tool("hypertables", {})))
    assert any(x["hypertable_name"] == "core_observationrecord" for x in h["hypertables"]), h
    assert any((x.get("view_name") or "") == "obs_agg_1h_v" for x in h["continuous_aggregates"]), h
    out.append("hypertables: core_observationrecord hypertable + obs_agg_1h_v cagg")

    # bare write: rejected by the SELECT/WITH/EXPLAIN/SHOW prefix guard
    g = _txt(await srv.call_tool("query", {"sql": "DELETE FROM core_observationrecord"}))
    assert g.lower().startswith("error"), g
    # write hidden in a CTE: passes the prefix check, caught by the keyword scan
    g2 = _txt(await srv.call_tool("query", {"sql":
        "WITH d AS (DELETE FROM core_observationrecord RETURNING 1) SELECT count(*) FROM d"}))
    assert "read-only" in g2.lower(), g2
    out.append("query guard: bare write + CTE-hidden write both rejected")

    q = json.loads(_txt(await srv.call_tool("query", {"sql": "SELECT count(*) AS n FROM core_observationrecord"})))
    n = q["rows"][0]["n"]
    assert n > 0, q
    out.append(f"query: {n} observation rows")

    o = json.loads(_txt(await srv.call_tool(
        "observations", {"station": "ET-ADD-001", "parameter": "air_temperature", "limit": 5})))
    assert o and o[0]["station"] == "ET-ADD-001" and o[0]["parameter"] == "air_temperature", o
    assert "value" in o[0] and "station_name" in o[0], o
    out.append(f"observations: join resolves station code + parameter ({len(o)} rows)")

    st = json.loads(_txt(await srv.call_tool("stations", {})))
    assert len(st) == 3, st
    out.append(f"stations: {len(st)} rows")

    return out


def main():
    cfg = dict(host=os.environ.get("ADL_DB_HOST", "127.0.0.1"),
               port=os.environ.get("ADL_DB_PORT", "5432"),
               dbname=os.environ.get("ADL_DB_NAME", "adl"),
               user=os.environ.get("ADL_DB_USER", "adl_readonly"),
               password=os.environ.get("ADL_DB_PASSWORD", ""))
    # Wait through the DB's init->restart window (TimescaleDB/postgres restarts
    # once after running init scripts; a real ADL also takes time to come up).
    deadline, last = time.time() + 90, None
    while time.time() < deadline:
        try:
            psycopg.connect(connect_timeout=5, **cfg).close()
            last = None
            break
        except Exception as e:
            last = e
            time.sleep(2)
    if last is not None:
        print(f"SKIP: no TimescaleDB reachable at {cfg['host']}:{cfg['port']} "
              f"({last.__class__.__name__}); harness ready, integration not run.")
        return 0

    import mcp_server as srv  # reads ADL_DB_* / convenience env at import
    try:
        results = asyncio.run(run(srv))
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1

    print("PASS — adl-mcp-server end-to-end smoke (grounded ADL fixture):")
    for r in results:
        print("  ✓", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
