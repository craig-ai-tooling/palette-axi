#!/usr/bin/env python3
"""Unit tests for the pure, network-free parts of palette-axi: the TOON encoder
(must match opp-axi's contract exactly), the null-safe JSON getters, and the
name/health helpers. Nothing here touches the network or 1Password — that's
what "ACTUALLY RUN IT" in the build task covered, live, against custeng-prod.
"""
import importlib.util
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
loader = SourceFileLoader("palette_axi", os.path.join(HERE, "palette-axi"))
spec = importlib.util.spec_from_loader("palette_axi", loader)
palette_axi = importlib.util.module_from_spec(spec)
sys.modules["palette_axi"] = palette_axi
loader.exec_module(palette_axi)


class TestTV(unittest.TestCase):
    def test_none_is_empty(self):
        self.assertEqual(palette_axi._tv(None), "")

    def test_true_false_are_distinct_from_empty(self):
        self.assertEqual(palette_axi._tv(True), "true")
        self.assertEqual(palette_axi._tv(False), "false")
        self.assertNotEqual(palette_axi._tv(False), palette_axi._tv(None))

    def test_quotes_only_when_needed(self):
        self.assertEqual(palette_axi._tv("plain"), "plain")
        self.assertEqual(palette_axi._tv("a,b"), '"a,b"')
        self.assertEqual(palette_axi._tv('a"b'), '"a""b"')
        self.assertEqual(palette_axi._tv("a\nb"), '"a\\nb"')


class TestToon(unittest.TestCase):
    def test_empty_rows_are_definitive(self):
        out = palette_axi.toon("clusters", ["name", "uid"], [])
        self.assertEqual(out, "clusters[0]{name,uid}: (none)")

    def test_header_and_rows(self):
        rows = [{"name": "c1", "uid": "abc"}, {"name": "c2", "uid": "def"}]
        out = palette_axi.toon("clusters", ["name", "uid"], rows)
        self.assertEqual(out, "clusters[2]{name,uid}:\n  c1,abc\n  c2,def")


class TestDget(unittest.TestCase):
    def test_missing_key_returns_empty_dict(self):
        self.assertEqual(palette_axi.dget({}, "status"), {})

    def test_explicit_null_returns_empty_dict(self):
        """Confirmed live: pack objects carry "status": null, not an omitted key —
        plain .get(key, {}) does not protect against this."""
        self.assertEqual(palette_axi.dget({"status": None}, "status"), {})

    def test_present_value_passes_through(self):
        self.assertEqual(palette_axi.dget({"status": {"state": "Running"}}, "status"),
                          {"state": "Running"})

    def test_none_container_is_safe(self):
        self.assertEqual(palette_axi.dget(None, "status"), {})


class TestIsUnhealthy(unittest.TestCase):
    def test_empty_is_not_unhealthy(self):
        """Empty means unknown, not broken — matches the _tv() None/False contract."""
        self.assertFalse(palette_axi.is_unhealthy(None))
        self.assertFalse(palette_axi.is_unhealthy(""))

    def test_healthy_case_insensitive(self):
        """Cluster health is 'Healthy'; edge host health is 'healthy' (confirmed
        live on both APIs) — comparison must not depend on casing."""
        self.assertFalse(palette_axi.is_unhealthy("Healthy"))
        self.assertFalse(palette_axi.is_unhealthy("healthy"))

    def test_unhealthy_states_flagged(self):
        self.assertTrue(palette_axi.is_unhealthy("Unhealthy"))
        self.assertTrue(palette_axi.is_unhealthy("unhealthy"))
        self.assertTrue(palette_axi.is_unhealthy("Unknown"))


class TestResolveByName(unittest.TestCase):
    UID_A = "693064bc882df8800821d248"  # real Palette-shaped uid: 24 hex chars
    UID_B = "6a8e7bc239a12d9008cab580"

    def setUp(self):
        self.items = [
            {"metadata": {"name": "rpi-inference", "uid": self.UID_A}},
            {"metadata": {"name": "edge-first-cluster", "uid": self.UID_B}},
        ]

    def test_uid_match(self):
        hit = palette_axi.resolve_by_name(self.UID_A, self.items, "cluster")
        self.assertEqual(hit["metadata"]["name"], "rpi-inference")

    def test_exact_name_match(self):
        hit = palette_axi.resolve_by_name("edge-first-cluster", self.items, "cluster")
        self.assertEqual(hit["metadata"]["uid"], self.UID_B)

    def test_unique_substring_match(self):
        hit = palette_axi.resolve_by_name("rpi", self.items, "cluster")
        self.assertEqual(hit["metadata"]["uid"], self.UID_A)

    def test_no_match_exits_notfound(self):
        with self.assertRaises(SystemExit) as ctx:
            palette_axi.resolve_by_name("nope", self.items, "cluster")
        self.assertEqual(ctx.exception.code, palette_axi.E_NOTFOUND)

    def test_ambiguous_match_exits_usage(self):
        items = [{"metadata": {"name": "foo-a", "uid": "1"}},
                 {"metadata": {"name": "foo-b", "uid": "2"}}]
        with self.assertRaises(SystemExit) as ctx:
            palette_axi.resolve_by_name("foo", items, "cluster")
        self.assertEqual(ctx.exception.code, palette_axi.E_USAGE)


if __name__ == "__main__":
    unittest.main()
