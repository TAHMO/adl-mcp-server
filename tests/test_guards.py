"""Unit tests for the pure query guards — no DB / deps required.
Run: python -m unittest tests.test_guards   (from the project root)
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from guards import guard_select, validate_ident  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
