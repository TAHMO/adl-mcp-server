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
