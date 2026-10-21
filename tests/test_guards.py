"""Unit tests for the pure query guards — no DB / deps required.
Run: python -m unittest tests.test_guards   (from the project root)
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from guards import api_path, guard_select, validate_base_url, validate_ident  # noqa: E402


class GuardSelect(unittest.TestCase):
    def test_select_gets_limit(self):
        self.assertIn("LIMIT 100", guard_select("SELECT 1", 100, 5000))

    def test_with_cte_allowed(self):
        out = guard_select("WITH x AS (SELECT 1) SELECT * FROM x", 10, 5000)
        self.assertTrue(out.lower().startswith("with"))

    def test_existing_limit_preserved(self):
        out = guard_select("SELECT * FROM t LIMIT 5", 100, 5000)
        self.assertEqual(out.upper().count("LIMIT"), 1)

    def test_limit_capped_at_max(self):
        self.assertIn("LIMIT 50", guard_select("SELECT 1", 999999, 50))

    def test_explain_not_limited(self):
        self.assertNotIn("LIMIT", guard_select("EXPLAIN SELECT 1", 10, 100).upper())

    def test_writes_rejected(self):
        for q in ["DELETE FROM t", "UPDATE t SET x=1", "INSERT INTO t VALUES (1)",
                  "DROP TABLE t", "TRUNCATE t", "REFRESH MATERIALIZED VIEW v",
                  "CREATE TABLE t (a int)", "GRANT SELECT ON t TO x",
                  "SELECT * INTO new_tbl FROM t"]:
            with self.assertRaises(ValueError, msg=q):
                guard_select(q, 10, 100)

    def test_multi_statement_rejected(self):
        with self.assertRaises(ValueError):
            guard_select("SELECT 1; SELECT 2", 10, 100)

    def test_stacked_write_rejected(self):
        with self.assertRaises(ValueError):
            guard_select("SELECT 1; DROP TABLE t", 10, 100)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            guard_select("   ", 10, 100)


class ValidateIdent(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_ident("core_observationrecord", "X"), "core_observationrecord")
        self.assertEqual(validate_ident("public.core_station", "X"), "public.core_station")

    def test_invalid(self):
        for bad in [None, "", "a b", "t;drop", "1abc", "a.b.c", "a-b", "drop table"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_ident(bad, "X")


class ApiPath(unittest.TestCase):
    def test_builds_allow_listed_paths(self):
        self.assertEqual(api_path("connections"), "/api/network-connection/")
        self.assertEqual(api_path("timeseries", station_link_id=7), "/api/data/timeseries/7/")
        self.assertEqual(api_path("station_links", connection_id="3"), "/api/network-connection/3/station-links/")

    def test_unknown_endpoint_rejected(self):
        for bad in ["token", "admin", "../etc", "cms"]:
            with self.assertRaises(ValueError, msg=bad):
                api_path(bad)

    def test_ids_must_be_positive_integers(self):
        for bad in [0, -1, "1/../../admin", "1?x=1", "abc", 1.5, True, None, "01"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                api_path("latest", station_link_id=bad)

    def test_missing_or_extra_ids_rejected(self):
        with self.assertRaises(ValueError):
            api_path("latest")
        with self.assertRaises(ValueError):
            api_path("connections", connection_id=1)


class BaseUrl(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_base_url("https://adl.example.org/"), "https://adl.example.org")
        self.assertEqual(validate_base_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000")

    def test_invalid(self):
        for bad in [None, "", "adl.example.org", "ftp://x", "https://adl.example.org/api",
                    "https://x.org/?q=1", "https://user@x.org"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_base_url(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
