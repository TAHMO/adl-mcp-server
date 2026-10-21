"""Pure, dependency-free query guards for adl-mcp-server.

Kept separate from mcp_server.py so the security-critical logic is unit-testable
without psycopg / mcp installed. The SELECT-only DB role is the real boundary;
these are fail-fast convenience checks on top of it.
"""
import re

WRITE_KW = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
            "GRANT", "REVOKE", "COPY", "VACUUM", "REFRESH", "CALL", "MERGE",
            "COMMENT", "DO", "LOCK", "SET", "INTO"}  # INTO catches `SELECT ... INTO newtable`
_TOKENS = re.compile(r"[A-Za-z_]+")
_IDENT  = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_HAS_LIMIT = re.compile(r"\blimit\b", re.IGNORECASE)


def guard_select(sql: str, limit, max_limit: int) -> str:
    """Return a single, read-only, auto-LIMIT'd statement or raise ValueError."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise ValueError("Empty query.")
    if ";" in s:
        raise ValueError("Only a single statement is allowed.")
    head = s.lower().lstrip("(")
    if not head.startswith(("select", "with", "explain", "show")):
        raise ValueError("Only SELECT / WITH / EXPLAIN / SHOW queries are permitted.")
    bad = {t.upper() for t in _TOKENS.findall(s)} & WRITE_KW
    if bad:
        raise ValueError(f"Write/DDL keyword(s) detected ({', '.join(sorted(bad))}) — read-only surface.")
    capped = max(1, min(int(limit), int(max_limit)))
    if not head.startswith("explain") and not _HAS_LIMIT.search(s):
        s = f"{s}\nLIMIT {capped}"
    return s


def validate_ident(name, env_var: str) -> str:
    """Validate an operator-supplied table/column identifier from env."""
    if not name or not _IDENT.match(name):
        raise ValueError(
            f"{env_var} is not set (or not a valid identifier). Run schema()/hypertables() "
            f"against the live ADL DB, then set {env_var} to the real table/column name."
        )
    return name


# --- ADL REST API guards ---------------------------------------------------
# The read-only endpoints ADL serves under /api/, as of ADL v0.8.17
# (adl/src/adl/api/urls.py + adl/src/adl/config/urls.py). Every one is a GET.
# The client may only call these templates — no free-form paths, no other verbs.
API_ENDPOINTS = {
    "connections":         "/api/network-connection/",
    "station_links":       "/api/network-connection/{connection_id}/station-links/",
    "connection_params":   "/api/network-connection/{connection_id}/data-parameters/",
    "station_link":        "/api/station-link/{station_link_id}/",
    "data_parameters":     "/api/data-parameters/",
    "latest":              "/api/data/latest/{station_link_id}/",
    "timeseries":          "/api/data/timeseries/{station_link_id}/",
    "qc_summary":          "/api/qc/summary/",
    "qc_inspection":       "/api/qc/inspection/{station_id}/",
}
# The one non-GET call allowed: exchanging credentials for a JWT (auth, not data).
API_TOKEN_PATH = "/api/token/"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def api_path(endpoint: str, **ids) -> str:
    """Build an allow-listed ADL API path; path ids must be positive integers."""
    if endpoint not in API_ENDPOINTS:
        raise ValueError(f"Unknown ADL API endpoint {endpoint!r}.")
    template = API_ENDPOINTS[endpoint]
    needed = set(_PLACEHOLDER.findall(template))
    if set(ids) != needed:
        raise ValueError(f"{endpoint} needs exactly {sorted(needed) or 'no ids'}.")
    clean = {}
    for key, val in ids.items():
        if isinstance(val, bool):
            raise ValueError(f"{key} must be a positive integer.")
        try:
            num = int(str(val).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a positive integer.") from None
        if num < 1 or str(num) != str(val).strip():
            raise ValueError(f"{key} must be a positive integer.")
        clean[key] = num
    return template.format(**clean)


def validate_base_url(url) -> str:
    """Validate the operator-supplied ADL base URL (scheme + host, no path tricks)."""
    if not url:
        raise ValueError("ADL_API_URL is not set.")
    u = str(url).strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._\-/]*)?$", u):
        raise ValueError("ADL_API_URL must look like https://adl.example.org (optionally with a path prefix).")
    if u.endswith("/api"):
        raise ValueError("ADL_API_URL is the site root, not the /api path — drop the trailing /api.")
    return u
