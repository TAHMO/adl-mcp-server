"""Read-only client for the ADL REST API (stdlib only, no extra dependencies).

ADL (v0.8.x) serves a read-only API under /api/. This client is the second way the
server reaches ADL, alongside the direct TimescaleDB connection: a country can hand
out an ADL API key instead of database credentials.

Read-only by construction:
  * only the allow-listed GET templates in guards.API_ENDPOINTS can be called;
  * path ids must be positive integers (guards.api_path);
  * the single non-GET request is the JWT login at /api/token/ (auth, not data).

Auth, grounded in ADL's code (adl/src/adl/api/auth.py, config/settings/base.py):
  * API key -> header `Authorization: Api-Key <key>` (djangorestframework-api-key
    default header; ADL does not set API_KEY_CUSTOM_HEADER). Accepted by every
    endpoint except the two QC endpoints.
  * JWT -> `Authorization: Bearer <access>` from POST /api/token/ with a username
    and password. Required for qc/summary and qc/inspection (IsAuthenticated only).
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from guards import API_TOKEN_PATH, api_path, validate_base_url

# endpoints that reject API keys (permission_classes = [IsAuthenticated])
JWT_ONLY = {"qc_summary", "qc_inspection"}


class ADLApiError(Exception):
    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class ADLApiClient:
    def __init__(self, base_url, api_key=None, username=None, password=None,
                 timeout=30, opener=None):
        self.base_url = validate_base_url(base_url)
        self.api_key = api_key or None
        self.username = username or None
        self.password = password or None
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._access = None

    # ---- auth --------------------------------------------------------------
    @property
    def has_jwt(self):
        return bool(self.username and self.password)

    def auth_modes(self):
        return [m for m, ok in (("api_key", self.api_key), ("jwt", self.has_jwt)) if ok]

    def _login(self):
        body = json.dumps({"username": self.username, "password": self.password}).encode()
        req = urllib.request.Request(self.base_url + API_TOKEN_PATH, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
        data = self._send(req, what="login")
        token = data.get("access") if isinstance(data, dict) else None
        if not token:
            raise ADLApiError("ADL login returned no access token.", detail=data)
        self._access = token

    def _auth_header(self, endpoint, fresh=False):
        if endpoint in JWT_ONLY or not self.api_key:
            if not self.has_jwt:
                need = ("ADL_API_USERNAME + ADL_API_PASSWORD (QC endpoints do not accept API keys)"
                        if endpoint in JWT_ONLY else "ADL_API_KEY or ADL_API_USERNAME + ADL_API_PASSWORD")
                raise ADLApiError(f"No credentials for {endpoint}: set {need}.")
            if fresh or not self._access:
                self._login()
            return "jwt", f"Bearer {self._access}"
        return "api_key", f"Api-Key {self.api_key}"

    # ---- transport ---------------------------------------------------------
    def _send(self, req, what):
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = None
            try:
                detail = json.loads(e.read() or b"null")
            except Exception:
                pass
            raise ADLApiError(f"ADL API {what} failed: HTTP {e.code}", status=e.code, detail=detail) from None
        except urllib.error.URLError as e:
            raise ADLApiError(f"Cannot reach ADL API at {self.base_url}: {e.reason}") from None
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise ADLApiError(f"ADL API {what} returned non-JSON content.") from None

    def get(self, endpoint, params=None, **ids):
        path = api_path(endpoint, **ids)
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.base_url + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        mode, header = self._auth_header(endpoint)
        try:
            return self._send(self._request(url, header), what=endpoint)
        except ADLApiError as e:
            # an expired JWT: log in again once and retry
            if e.status == 401 and mode == "jwt":
                _, header = self._auth_header(endpoint, fresh=True)
                return self._send(self._request(url, header), what=endpoint)
            raise

    @staticmethod
    def _request(url, auth_header):
        return urllib.request.Request(url, method="GET",
                                      headers={"Authorization": auth_header,
                                               "Accept": "application/json"})

    # ---- convenience -------------------------------------------------------
    def parameter_names(self):
        """Map ADL parameter id -> {name, unit, category} for labelling values."""
        data = self.get("data_parameters") or {}
        out = {}
        for p in data.get("data_parameters", []):
            unit = p.get("unit") or {}
            out[p.get("id")] = {"name": p.get("name"), "unit": unit.get("symbol") or unit.get("name"),
                                "category": p.get("category")}
        return out


def label_values(records, names):
    """Add parameter names to ADL's id-keyed values without dropping anything."""
    def lab(pid):
        meta = names.get(pid) or names.get(_as_int(pid)) or {}
        return meta.get("name") or f"parameter_{pid}"
    labelled = []
    for r in records:
        r = dict(r)
        if isinstance(r.get("data"), dict):  # timeseries rows: {"data": {pid: value}}
            r["values"] = {lab(pid): v for pid, v in r["data"].items()}
        if "parameter_id" in r:              # latest rows: {"parameter_id": pid, ...}
            r["parameter"] = lab(r["parameter_id"])
        labelled.append(r)
    return labelled


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
