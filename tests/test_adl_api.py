"""Tests for the ADL REST API route — no ADL, no DB, no network beyond localhost.

A stub HTTP server stands in for ADL. Its responses copy the shapes of ADL v0.8.17's
views (adl/src/adl/api/views.py, adl/src/adl/viewer/views.py) and it enforces ADL's
auth rules: API key (`Authorization: Api-Key ...`) on every endpoint except the two
QC endpoints, which take only a JWT obtained from POST /api/token/.

Run: python -m unittest discover -s tests -p "test_*.py"   (from the project root)
"""
import asyncio, importlib, json, os, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from adl_api import ADLApiClient, ADLApiError, label_values  # noqa: E402

API_KEY = "test-key.abc123"
USER, PASSWORD = "reader", "s3cret"

PARAMS = {
    "categories": [{"id": "TMP", "name": "Temperature"}, {"id": "PRC", "name": "Precipitation"}],
    "data_parameters": [
        {"id": 1, "name": "air_temperature", "unit": {"id": 1, "name": "degC", "symbol": "°C",
                                                       "description": ""}, "description": "", "category": "TMP"},
        {"id": 2, "name": "precipitation", "unit": {"id": 2, "name": "mm", "symbol": "mm",
                                                     "description": ""}, "description": "", "category": "PRC"},
    ],
}
STATION = {"id": 11, "name": "Machakos AWS", "network": {"id": 1, "name": "TAHMO"},
           "wigos_id": "0-404-0-TA00001", "location": {"latitude": -1.5, "longitude": 37.3},
           "station_height_above_msl": 1600}


class StubADL(BaseHTTPRequestHandler):
    issued = []       # access tokens handed out
    expired = set()   # tokens the stub now treats as expired
    log = []          # (method, path, auth) seen

    def log_message(self, *a):  # keep test output quiet
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        h = self.headers.get("Authorization", "")
        if h == f"Api-Key {API_KEY}":
            return "api_key"
        if h.startswith("Bearer "):
            tok = h[len("Bearer "):]
            if tok in self.issued and tok not in self.expired:
                return "jwt"
            return "expired"
        return None

    def do_POST(self):
        StubADL.log.append(("POST", self.path, None))
        if self.path != "/api/token/":
            return self._send(405, {"detail": "Method not allowed"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if body.get("username") == USER and body.get("password") == PASSWORD:
            tok = f"jwt-{len(self.issued) + 1}"
            StubADL.issued.append(tok)
            return self._send(200, {"access": tok, "refresh": "r"})
        return self._send(401, {"detail": "No active account found with the given credentials"})

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        auth = self._auth()
        StubADL.log.append(("GET", url.path, auth))
        qc = url.path.startswith("/api/qc/")
        if auth == "expired":
            return self._send(401, {"detail": "Given token not valid for any token type"})
        if auth is None or (qc and auth != "jwt"):
            return self._send(403, {"detail": "Authentication credentials were not provided."})
        p = url.path
        if p == "/api/network-connection/":
            return self._send(200, [{"id": 3, "name": "TAHMO Kenya", "network": 1}])
        if p == "/api/network-connection/3/station-links/":
            return self._send(200, [{"id": 7, "network_connection": 3, "station": STATION}])
        if p == "/api/station-link/7/":
            return self._send(200, {"id": 7, "network_connection": 3, "station": STATION,
                                    "data_dates": {"earliest_time": "2026-01-01T00:00:00+00:00",
                                                   "latest_time": "2026-10-21T06:00:00+00:00"},
                                    "data_parameters": PARAMS["data_parameters"],
                                    "data_categories": PARAMS["categories"]})
        if p == "/api/data-parameters/":
            return self._send(200, PARAMS)
        if p == "/api/data/latest/7/":
            return self._send(200, {"station_id": 11, "connection_id": 3, "data": [
                {"time": "2026-10-21T06:00:00+00:00", "parameter_id": 1, "value": 18.4},
                {"time": "2026-10-21T06:00:00+00:00", "parameter_id": 2, "value": 0.2}]})
        if p == "/api/data/latest/99/":
            return self._send(404, {"error": "No observation records found for the given station link."})
        if p == "/api/data/timeseries/7/":
            StubADL.last_query = q
            rows = [{"station_id": 11, "connection_id": 3, "time": "2026-10-21T06:00:00+00:00",
                     "data": {"1": 18.4, "2": 0.2}}]
            return self._send(200, {"count": 1, "next": None, "previous": None, "results": rows})
        if p == "/api/qc/summary/":
            return self._send(200, {"month": int(q.get("month", 10)), "year": int(q.get("year", 2026)),
                                    "data": [{"station_id": 11, "station_name": "Machakos AWS",
                                              "record_count": 100, "pass_pct": 97.0}]})
        if p == "/api/qc/inspection/11/":
            recs = [{"x": i * 1000, "y": 18.0, "status": 1} for i in range(5)]
            return self._send(200, {"station_id": 11, "records": recs, "flags": []})
        return self._send(404, {"detail": "Not found."})


class _Server:
    def __enter__(self):
        StubADL.issued, StubADL.expired, StubADL.log = [], set(), []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubADL)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


class Client(unittest.TestCase):
    def setUp(self):
        self.srv = _Server().__enter__()

    def tearDown(self):
        self.srv.__exit__()

    def test_api_key_header_and_get_only(self):
        c = ADLApiClient(self.srv.url, api_key=API_KEY)
        self.assertEqual(c.get("connections")[0]["name"], "TAHMO Kenya")
        self.assertEqual(StubADL.log, [("GET", "/api/network-connection/", "api_key")])

    def test_qc_uses_jwt_even_when_key_set(self):
        c = ADLApiClient(self.srv.url, api_key=API_KEY, username=USER, password=PASSWORD)
        c.get("qc_summary", params={"month": 9, "year": 2026})
        self.assertEqual([e[0] for e in StubADL.log], ["POST", "GET"])
        self.assertEqual(StubADL.log[1], ("GET", "/api/qc/summary/", "jwt"))

    def test_qc_without_login_fails_clearly(self):
        c = ADLApiClient(self.srv.url, api_key=API_KEY)
        with self.assertRaises(ADLApiError) as cm:
            c.get("qc_summary")
        self.assertIn("do not accept API keys", str(cm.exception))
        self.assertEqual(StubADL.log, [])  # refused before any request went out

    def test_jwt_only_client_and_expiry_retry(self):
        c = ADLApiClient(self.srv.url, username=USER, password=PASSWORD)
        c.get("connections")
        StubADL.expired.add(StubADL.issued[-1])
        self.assertEqual(c.get("connections")[0]["id"], 3)  # re-logged in transparently
        self.assertEqual(len(StubADL.issued), 2)

    def test_bad_key_surfaces_status(self):
        c = ADLApiClient(self.srv.url, api_key="wrong")
        with self.assertRaises(ADLApiError) as cm:
            c.get("connections")
        self.assertEqual(cm.exception.status, 403)

    def test_bad_login(self):
        c = ADLApiClient(self.srv.url, username=USER, password="nope")
        with self.assertRaises(ADLApiError) as cm:
            c.get("connections")
        self.assertEqual(cm.exception.status, 401)

    def test_no_credentials(self):
        c = ADLApiClient(self.srv.url)
        with self.assertRaises(ADLApiError):
            c.get("connections")

    def test_unreachable(self):
        c = ADLApiClient("http://127.0.0.1:9", api_key=API_KEY, timeout=2)
        with self.assertRaises(ADLApiError) as cm:
            c.get("connections")
        self.assertIn("Cannot reach", str(cm.exception))

    def test_free_form_path_impossible(self):
        c = ADLApiClient(self.srv.url, api_key=API_KEY)
        with self.assertRaises(ValueError):
            c.get("latest", station_link_id="7/../../admin")
        self.assertEqual(StubADL.log, [])


class Labels(unittest.TestCase):
    def test_labels_keep_raw_ids(self):
        names = {1: {"name": "air_temperature"}, 2: {"name": "precipitation"}}
        rows = label_values([{"data": {"1": 18.4, "2": 0.2}}, {"parameter_id": 1, "value": 3}], names)
        self.assertEqual(rows[0]["values"], {"air_temperature": 18.4, "precipitation": 0.2})
        self.assertEqual(rows[0]["data"], {"1": 18.4, "2": 0.2})
        self.assertEqual(rows[1]["parameter"], "air_temperature")

    def test_unknown_id_not_dropped(self):
        rows = label_values([{"data": {"42": 1.0}}], {})
        self.assertEqual(rows[0]["values"], {"parameter_42": 1.0})


class ServerApiOnly(unittest.TestCase):
    """The MCP server in api-only mode: tools listed, dispatch, and no Postgres driver needed."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server().__enter__()
        os.environ.update({"ADL_BACKENDS": "api", "ADL_API_URL": cls.srv.url, "ADL_API_KEY": API_KEY,
                           "ADL_API_USERNAME": USER, "ADL_API_PASSWORD": PASSWORD})
        sys.modules.pop("mcp_server", None)
        cls.ms = importlib.import_module("mcp_server")

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()
        for k in ("ADL_BACKENDS", "ADL_API_URL", "ADL_API_KEY", "ADL_API_USERNAME", "ADL_API_PASSWORD"):
            os.environ.pop(k, None)
        sys.modules.pop("mcp_server", None)

    def call(self, name, args=None):
        out = asyncio.run(self.ms.call_tool(name, args or {}))
        return json.loads(out[0].text) if out[0].text.lstrip().startswith(("{", "[")) else out[0].text

    def test_only_api_tools_listed(self):
        names = [t.name for t in asyncio.run(self.ms.list_tools())]
        self.assertTrue(names and all(n.startswith("api_") for n in names))
        self.assertIn("api_timeseries", names)

    def test_db_tool_refused(self):
        self.assertIn("database route is off", self.call("query", {"sql": "SELECT 1"}))

    def test_ping(self):
        r = self.call("api_ping")
        self.assertTrue(r["reachable"])
        self.assertEqual(r["auth_modes"], ["api_key", "jwt"])
        self.assertEqual((r["network_connections"], r["data_parameters"]), (1, 2))

    def test_latest_labelled(self):
        r = self.call("api_latest", {"station_link_id": 7})
        self.assertEqual({d["parameter"] for d in r["data"]}, {"air_temperature", "precipitation"})

    def test_timeseries_paginates_and_labels(self):
        r = self.call("api_timeseries", {"station_link_id": 7, "start": "2026-10-20T00:00:00Z",
                                         "limit": 5000, "page": 2})
        self.assertEqual(StubADL.last_query, {"start_date": "2026-10-20T00:00:00Z", "paginate": "true",
                                              "page": "2", "limit": "1000"})
        self.assertEqual(r["results"][0]["values"]["air_temperature"], 18.4)

    def test_qc_inspection_capped(self):
        r = self.call("api_qc_inspection", {"station_id": 11, "month": 10, "year": 2026, "limit": 2})
        self.assertEqual((r["record_count"], len(r["records"]), r["truncated"]), (5, 2, True))

    def test_404_reported_with_hint(self):
        r = self.call("api_latest", {"station_link_id": 99})
        self.assertEqual(r["status"], 404)
        self.assertIn("no observations", r["hint"])

    def test_bad_id_rejected(self):
        self.assertIn("positive integer", self.call("api_station_link", {"station_link_id": -3}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
