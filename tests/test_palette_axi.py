#!/usr/bin/env python3
"""Unit tests for the pure, network-free parts of palette-axi: the TOON encoder
(must match opp-axi's contract exactly), the null-safe JSON getters, and the
name/health helpers. Nothing here touches the network or 1Password — that's
what "ACTUALLY RUN IT" in the build task covered, live, against custeng-prod.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import urllib.error

from palette_axi import cli as palette_axi


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


class TestProjectsEndpoint(unittest.TestCase):
    """The tenant's projects moved. `GET /v1/projects` began answering
    `405: method GET is not allowed, but [POST] are`, which broke `projects` AND
    every `--project <name>` lookup, since both resolved through it.

    These drive the real code paths with `api()` stubbed, so they assert on the
    request actually issued rather than on a string in the source. A test that
    only grepped for the new path would pass while the verb still called the
    dead one from a branch it never took.
    """

    def setUp(self):
        self.calls = []
        self.bodies = []
        self._api = palette_axi.api
        self._emit = palette_axi.emit
        # cmd_projects fetches the key before it ever calls api(), and that shells
        # out to `op`. Stubbing only api() left the suite green on a box that has
        # 1Password installed and red on a runner that does not -- a test whose
        # result depends on the machine is not a test.
        self._key = palette_axi.get_api_key
        palette_axi.get_api_key = lambda tenant: "stub-key"

        def fake_api(method, path, api_key, project=None, params=None,
                     json_body=None, timeout=30):
            self.calls.append((method, path))
            self.bodies.append(json_body)
            return {"items": [{"metadata": {"name": "Example-Project",
                                            "uid": "5f1e2d3c4b5a69780a1b2c3d"},
                               "status": {"clustersHealth": {"healthy": 2, "running": 1,
                                                             "errored": 0, "unhealthy": 0},
                                          "usage": {"clusters": [{"uid": "a"}, {"uid": "b"},
                                                                 {"uid": "c"}]}}}],
                    "listmeta": {"count": 1}}

        palette_axi.api = fake_api
        self.rendered = []
        palette_axi.emit = lambda *a, **k: self.rendered.extend(
            x for x in a if isinstance(x, str))

    def tearDown(self):
        palette_axi.api = self._api
        palette_axi.emit = self._emit
        palette_axi.get_api_key = self._key

    def test_projects_does_not_call_the_retired_endpoint(self):
        palette_axi.cmd_projects(type("A", (), {"tenant": "custeng-prod"})())
        paths = [p for _, p in self.calls]
        self.assertTrue(paths, "cmd_projects issued no request at all")
        self.assertNotIn("/v1/projects", paths,
                         "still calling the endpoint that answers 405")
        self.assertIn("/v1/dashboard/projects", paths)

    def test_resolving_a_project_by_name_uses_the_live_endpoint(self):
        """The wider blast radius: every verb taking --project <name> goes here."""
        uid = palette_axi.resolve_project("Example-Project", "key")
        self.assertEqual(uid, "5f1e2d3c4b5a69780a1b2c3d")
        self.assertNotIn("/v1/projects", [p for _, p in self.calls])

    def test_cluster_count_replaces_the_column_that_no_longer_exists(self):
        """`spec.isDefault` is absent from the new payload. Reporting False for
        every project would be a quiet lie, so the column carries real numbers --
        counted from status.usage.clusters, not by summing the overlapping health
        buckets, which is what the first version of this test wrongly pinned.
        See TestProjectClusterCount for that regression."""
        p = {"status": {"clustersHealth": {"healthy": 2, "running": 1,
                                           "errored": 0, "unhealthy": 3},
                        "usage": {"clusters": [{"uid": "a"}, {"uid": "b"}]}}}
        self.assertEqual(palette_axi._project_cluster_count(p), 2)

    def test_the_rendered_header_names_the_columns_it_actually_fills(self):
        """Live catch: the row dict moved to `clusters` while the TOON field list
        still said `default`, so the column rendered empty for all 65 projects --
        a header promising a field the payload never carries. Stubbing emit and
        checking only the request path missed it entirely."""
        palette_axi.cmd_projects(type("A", (), {"tenant": "custeng-prod"})())
        block = next((r for r in self.rendered if r.startswith("projects[")), None)
        self.assertIsNotNone(block, "no projects TOON block was emitted")
        header = block.splitlines()[0]
        self.assertNotIn("default", header, "header still advertises the dead field")
        self.assertIn("clusters", header)
        self.assertIn("Example-Project,5f1e2d3c4b5a69780a1b2c3d,3", block,
                      "cluster count did not render into the row")


class TestEdgehostsEndpoint(unittest.TestCase):
    """/v1/edgehosts answers the same 405 as /v1/projects did — found by smoke-testing
    every verb live after the projects fix, rather than assuming one bug meant one
    endpoint. The replacement is POST-only, so list_all had to learn a method."""

    def setUp(self):
        self.calls = []
        self._api, self._emit = palette_axi.api, palette_axi.emit
        self._key, self._resolve = palette_axi.get_api_key, palette_axi.resolve_project
        palette_axi.get_api_key = lambda tenant: "stub-key"
        palette_axi.resolve_project = lambda ref, key: "proj-uid"

        def fake_api(method, path, api_key, project=None, params=None,
                     json_body=None, timeout=30):
            self.calls.append((method, path, json_body))
            return {"items": [{"metadata": {"name": "edge-01", "uid": "u1"},
                               "status": {"state": "ready", "health": {"state": "healthy"},
                                          "inUseClusters": [{"name": "c1"}]}}],
                    "listmeta": {"count": 1}}

        palette_axi.api = fake_api
        palette_axi.emit = lambda *a, **k: None

    def tearDown(self):
        palette_axi.api, palette_axi.emit = self._api, self._emit
        palette_axi.get_api_key, palette_axi.resolve_project = self._key, self._resolve

    def test_edgehosts_does_not_call_the_retired_endpoint(self):
        palette_axi.cmd_edgehosts(type("A", (), {"tenant": "t", "project": "p", "wide": False, "min_cores": None, "label": None})())
        paths = [p for _, p, _ in self.calls]
        self.assertTrue(paths, "cmd_edgehosts issued no request")
        self.assertNotIn("/v1/edgehosts", paths, "still calling the 405 endpoint")
        self.assertIn("/v1/dashboard/edgehosts/search", paths)

    def test_it_posts_a_search_body(self):
        """The search endpoint is POST-only; a GET returns 405 and a POST with no
        body is not what the API accepts."""
        palette_axi.cmd_edgehosts(type("A", (), {"tenant": "t", "project": "p", "wide": False, "min_cores": None, "label": None})())
        method, _, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertIsNotNone(body, "no JSON body sent to a POST-only search endpoint")
        self.assertIn("filter", body)


class TestEdgehostVerb(unittest.TestCase):
    """`edgehost <ref>`: single-host hardware inventory. Fixtures below are
    entirely synthetic (no customer values) but match the field names/types
    read from a real GET /v1/edgehosts/{uid} response, per AGENTS.md."""

    PROJECT_UID = "6720c668e9746cb63a499001"
    EDGEHOST_UID = "693064bc882df8800821e001"

    SEARCH_ITEM = {
        "metadata": {"name": "ucs-blade-01", "uid": EDGEHOST_UID, "labels": {"site": "dal"}},
        "status": {"state": "ready", "health": {"state": "healthy", "agentVersion": "4.2.0"},
                   "inUseClusters": [{"name": "edge-cluster-1", "uid": "cluster-uid-1"}]},
    }

    DESCRIBE = {
        "metadata": {"name": "ucs-blade-01", "uid": EDGEHOST_UID, "labels": {"site": "dal"},
                     "annotations": {"spectrocloud.com/deviceType": "bare-metal"},
                     "creationTimestamp": "2026-06-01T12:00:00Z"},
        "spec": {
            "device": {
                "archType": "amd64", "hostType": "agent-mode", "hostState": "paired",
                "secureBoot": True, "cpu": {"cores": 96}, "memory": {"sizeInMB": 786432},
                "os": {"family": "ubuntu", "version": "22.04", "kernelVersion": "5.15.0-91-generic"},
                "disks": [
                    {"controller": "MegaRAID", "size": 1800, "vendor": "DELL",
                     "partitions": [{"fileSystemType": "ext4", "freeSpace": 100,
                                     "mountPoint": "/", "totalSpace": 1800}]},
                    {"controller": "FC", "size": 5000, "vendor": "PURE",
                     "partitions": [{"fileSystemType": "xfs", "freeSpace": 500,
                                     "mountPoint": "/data", "totalSpace": 5000}]},
                ],
                "nics": [
                    {"nicName": "eth0", "macAddr": "aa:bb:cc:00:11:22", "ip": "10.0.0.5",
                     "subnet": "255.255.255.0", "gateway": "10.0.0.1", "dns": ["8.8.8.8"],
                     "isDefault": True},
                    {"nicName": "bond0", "macAddr": "aa:bb:cc:00:11:23", "ip": "10.0.1.5",
                     "subnet": "255.255.255.0", "gateway": "10.0.1.1", "dns": [], "isDefault": False},
                    {"nicName": "cilium_host", "macAddr": "aa:bb:cc:00:11:99", "ip": "172.16.0.1",
                     "subnet": "", "gateway": "", "dns": [], "isDefault": False},
                    {"nicName": "vethabcdef", "macAddr": "aa:bb:cc:00:11:98", "ip": "",
                     "subnet": "", "gateway": "", "dns": [], "isDefault": False},
                ],
                "gpus": [],
            },
            "host": {"hostAddress": "10.0.0.5", "macAddress": "aa:bb:cc:00:11:22", "hostUid": "host-uid-1"},
        },
        "status": {"state": "ready", "health": {"state": "healthy", "agentVersion": "4.2.0", "message": ""},
                   "inUseClusters": [{"name": "edge-cluster-1", "uid": "cluster-uid-1"}]},
    }

    def setUp(self):
        self.calls = []
        self._api, self._key = palette_axi.api, palette_axi.get_api_key
        self._resolve = palette_axi.resolve_project
        palette_axi.get_api_key = lambda tenant: "stub-key"
        palette_axi.resolve_project = lambda ref, key: self.PROJECT_UID

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path, "project": project, "body": json_body})
            if path == palette_axi.EDGEHOSTS_PATH:
                return {"items": [dict(self.SEARCH_ITEM)], "listmeta": {"count": 1}}
            if path == f"/v1/edgehosts/{self.EDGEHOST_UID}":
                return dict(self.DESCRIBE)
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.get_api_key = self._api, self._key
        palette_axi.resolve_project = self._resolve

    def _run(self, ref, project="SA-Craig-Smith", all_projects=False, all_nics=False, json_out=False):
        args = type("A", (), {"tenant": "custeng-prod", "ref": ref, "project": project,
                               "all_projects": all_projects, "all_nics": all_nics, "json": json_out})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_edgehost(args)
        return buf.getvalue()

    def test_resolves_in_project_then_describes_with_projectuid(self):
        out = self._run("ucs-blade-01")
        calls = [(c["method"], c["path"], c["project"]) for c in self.calls]
        self.assertIn(("POST", palette_axi.EDGEHOSTS_PATH, self.PROJECT_UID), calls)
        self.assertIn(("GET", f"/v1/edgehosts/{self.EDGEHOST_UID}", self.PROJECT_UID), calls,
                       "describe call must carry the resolved project as ProjectUid")
        self.assertIn("edgehost[1]", out)

    def test_summary_fields_render(self):
        out = self._run("ucs-blade-01")
        self.assertIn("ucs-blade-01", out)
        self.assertIn("agent-mode", out)
        self.assertIn("amd64", out)
        self.assertIn("ubuntu 22.04", out)
        self.assertIn("5.15.0-91-generic", out)
        self.assertIn("edge-cluster-1", out)
        self.assertIn("site=dal", out)
        self.assertIn("96", out)  # cores
        self.assertIn("768.0", out)  # 786432 MB / 1024, rounded 1dp

    def test_disks_table_shows_vendor_and_partition_count(self):
        out = self._run("ucs-blade-01")
        self.assertIn("disks[2]", out)
        self.assertIn("PURE", out)
        self.assertIn("/data", out)

    def test_virtual_nics_hidden_by_default(self):
        out = self._run("ucs-blade-01")
        self.assertIn("nics[2]", out, "expected only the 2 physical nics by default")
        self.assertIn("eth0", out)
        self.assertIn("bond0", out)
        self.assertNotIn("cilium_host", out)
        self.assertNotIn("vethabcdef", out)
        self.assertIn("+2 virtual hidden", out)

    def test_all_nics_shows_virtual_interfaces(self):
        out = self._run("ucs-blade-01", all_nics=True)
        self.assertIn("nics[4]", out)
        self.assertIn("cilium_host", out)
        self.assertIn("vethabcdef", out)

    def test_gpu_count_zero_renders(self):
        out = self._run("ucs-blade-01")
        self.assertIn("gpuCount:0", out)

    def test_json_dumps_raw_object(self):
        out = self._run("ucs-blade-01", json_out=True)
        parsed = json.loads(out)
        self.assertEqual(parsed["metadata"]["uid"], self.EDGEHOST_UID)

    def test_ambiguous_ref_in_project_exits_usage(self):
        second = {"metadata": {"name": "ucs-blade-02", "uid": "693064bc882df8800821e002",
                                "labels": {}},
                   "status": {"state": "ready", "health": {"state": "healthy"}, "inUseClusters": []}}

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path, "project": project})
            if path == palette_axi.EDGEHOSTS_PATH:
                return {"items": [dict(self.SEARCH_ITEM), second], "listmeta": {"count": 2}}
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api
        with self.assertRaises(SystemExit) as ctx:
            self._run("ucs-blade")
        self.assertEqual(ctx.exception.code, palette_axi.E_USAGE)

    def test_not_found_in_project_exits_notfound(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("does-not-exist")
        self.assertEqual(ctx.exception.code, palette_axi.E_NOTFOUND)


class TestEdgehostNullFields(unittest.TestCase):
    """Confirmed live elsewhere in this tool: objects on these endpoints carry
    explicit JSON nulls rather than omitted keys (see dget()'s own docstring).
    Every nested access cmd_edgehost makes must survive the same treatment."""

    PROJECT_UID = "6720c668e9746cb63a499003"
    EDGEHOST_UID = "693064bc882df8800821e003"

    SEARCH_ITEM = {"metadata": {"name": "bare-host", "uid": EDGEHOST_UID, "labels": None},
                   "status": {"state": "ready", "health": None, "inUseClusters": None}}

    DESCRIBE = {
        "metadata": {"name": "bare-host", "uid": EDGEHOST_UID, "labels": None,
                     "annotations": None, "creationTimestamp": None},
        "spec": {
            "device": {"archType": "amd64", "hostType": "agent-mode", "hostState": None,
                       "secureBoot": None, "cpu": None, "memory": None, "os": None,
                       "disks": None, "nics": None, "gpus": None},
            "host": None,
        },
        "status": {"state": "ready", "health": None, "inUseClusters": None},
    }

    def setUp(self):
        self.calls = []
        self._api, self._key = palette_axi.api, palette_axi.get_api_key
        self._resolve = palette_axi.resolve_project
        palette_axi.get_api_key = lambda tenant: "stub-key"
        palette_axi.resolve_project = lambda ref, key: self.PROJECT_UID

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append((method, path, project))
            if path == palette_axi.EDGEHOSTS_PATH:
                return {"items": [dict(self.SEARCH_ITEM)], "listmeta": {"count": 1}}
            if path == f"/v1/edgehosts/{self.EDGEHOST_UID}":
                return dict(self.DESCRIBE)
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.get_api_key = self._api, self._key
        palette_axi.resolve_project = self._resolve

    def test_null_device_subfields_do_not_crash(self):
        args = type("A", (), {"tenant": "custeng-prod", "ref": "bare-host", "project": "p",
                               "all_projects": False, "all_nics": False, "json": False})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_edgehost(args)
        out = buf.getvalue()
        self.assertIn("edgehost[1]", out)
        self.assertIn("disks[0]{idx,vendor,controller,sizeGB,partitions,mounts}: (none)", out)
        self.assertIn("nics[0]{name,mac,ip,subnet,gateway,default}: (none)", out)
        self.assertIn("gpuCount:0", out)


class TestEdgehostAllProjects(unittest.TestCase):
    """No --project and no $PALETTE_PROJECT: search every project and report
    which one actually had the host -- the common case an SE hits with a UID
    from a support ticket but no project name."""

    PROJECT_A = {"metadata": {"name": "Project-A", "uid": "6720c668e9746cb63a499010"}}
    PROJECT_B = {"metadata": {"name": "Project-B", "uid": "6720c668e9746cb63a499011"}}
    EDGEHOST_UID = "693064bc882df8800821e010"

    SEARCH_ITEM = {"metadata": {"name": "ucs-blade-b", "uid": EDGEHOST_UID, "labels": {}},
                   "status": {"state": "ready", "health": {"state": "healthy"}, "inUseClusters": []}}

    DESCRIBE = {
        "metadata": {"name": "ucs-blade-b", "uid": EDGEHOST_UID, "labels": {}},
        "spec": {"device": {"archType": "amd64", "hostType": "agent-mode", "cpu": {"cores": 32},
                            "memory": {"sizeInMB": 65536}, "os": {}, "disks": [], "nics": [], "gpus": []},
                 "host": {"hostAddress": "10.0.2.9"}},
        "status": {"state": "ready", "health": {"state": "healthy"}, "inUseClusters": []},
    }

    def setUp(self):
        self.calls = []
        self._api, self._key = palette_axi.api, palette_axi.get_api_key
        palette_axi.get_api_key = lambda tenant: "stub-key"

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path, "project": project})
            if path == palette_axi.PROJECTS_PATH:
                return {"items": [dict(self.PROJECT_A), dict(self.PROJECT_B)], "listmeta": {"count": 2}}
            if path == palette_axi.EDGEHOSTS_PATH and project == self.PROJECT_A["metadata"]["uid"]:
                return {"items": [], "listmeta": {"count": 0}}
            if path == palette_axi.EDGEHOSTS_PATH and project == self.PROJECT_B["metadata"]["uid"]:
                return {"items": [dict(self.SEARCH_ITEM)], "listmeta": {"count": 1}}
            if path == f"/v1/edgehosts/{self.EDGEHOST_UID}":
                return dict(self.DESCRIBE)
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.get_api_key = self._api, self._key

    def _run(self, ref, project=None, all_projects=False):
        args = type("A", (), {"tenant": "custeng-prod", "ref": ref, "project": project,
                               "all_projects": all_projects, "all_nics": False, "json": False})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_edgehost(args)
        return buf.getvalue()

    def test_no_project_flag_searches_every_project_and_finds_the_right_one(self):
        out = self._run("ucs-blade-b")
        calls = [(c["path"], c["project"]) for c in self.calls]
        self.assertIn((palette_axi.EDGEHOSTS_PATH, self.PROJECT_A["metadata"]["uid"]), calls,
                       "must have searched Project-A even though the host isn't there")
        self.assertIn((palette_axi.EDGEHOSTS_PATH, self.PROJECT_B["metadata"]["uid"]), calls)
        self.assertIn((f"/v1/edgehosts/{self.EDGEHOST_UID}", self.PROJECT_B["metadata"]["uid"]), calls,
                       "describe call must use the project that actually matched, not the first one tried")
        self.assertIn("Project-B", out, "must report which project the host was found in")
        self.assertIn("edgehost[1]", out)

    def test_all_projects_flag_forces_search_even_with_project_set(self):
        self._run("ucs-blade-b", project="Project-A", all_projects=True)
        calls = [(c["path"], c["project"]) for c in self.calls]
        self.assertIn((palette_axi.EDGEHOSTS_PATH, self.PROJECT_A["metadata"]["uid"]), calls)
        self.assertIn((palette_axi.EDGEHOSTS_PATH, self.PROJECT_B["metadata"]["uid"]), calls)

    def test_no_match_in_any_project_exits_notfound(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("nonexistent-host")
        self.assertEqual(ctx.exception.code, palette_axi.E_NOTFOUND)


class TestEdgehostsWide(unittest.TestCase):
    """edgehosts --wide / --min-cores / --label. The no-wide, no-filter path
    must stay byte-identical to what TestEdgehostsEndpoint already pins down --
    this class adds the new coverage without touching that existing test."""

    PROJECT_UID = "proj-uid"
    ITEM_NO_DEVICE = {"metadata": {"name": "edge-01", "uid": "host-1", "labels": {"env": "prod"}},
                       "status": {"state": "ready", "health": {"state": "healthy"},
                                  "inUseClusters": [{"name": "c1"}]}}
    DESCRIBE = {
        "metadata": {"name": "edge-01", "uid": "host-1"},
        "spec": {"device": {"cpu": {"cores": 96}, "memory": {"sizeInMB": 786432},
                            "secureBoot": True,
                            "disks": [{"vendor": "PURE"}, {"vendor": "DELL"}]},
                 "host": {"hostAddress": "10.0.0.9"}},
        "status": {"state": "ready", "health": {"state": "healthy"}, "inUseClusters": [{"name": "c1"}]},
    }

    def setUp(self):
        self.calls = []
        self._api, self._emit = palette_axi.api, palette_axi.emit
        self._key, self._resolve = palette_axi.get_api_key, palette_axi.resolve_project
        palette_axi.get_api_key = lambda tenant: "stub-key"
        palette_axi.resolve_project = lambda ref, key: self.PROJECT_UID

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append((method, path, project, json_body))
            if path == palette_axi.EDGEHOSTS_PATH:
                return {"items": [dict(self.ITEM_NO_DEVICE)], "listmeta": {"count": 1}}
            if path == "/v1/edgehosts/host-1":
                return dict(self.DESCRIBE)
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.emit = self._api, self._emit
        palette_axi.get_api_key, palette_axi.resolve_project = self._key, self._resolve

    def _run(self, wide=False, min_cores=None, label=None):
        args = type("A", (), {"tenant": "t", "project": "p", "wide": wide,
                               "min_cores": min_cores, "label": label})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_edgehosts(args)
        return buf.getvalue()

    def test_default_output_unchanged_no_wide_call(self):
        out = self._run()
        self.assertNotIn("/v1/edgehosts/host-1", [c[1] for c in self.calls],
                         "default (no --wide, no filters) must never fetch per-host device data")
        expected_rows = [{"name": "edge-01", "uid": "host-1", "state": "ready",
                          "health": "healthy", "cluster": "c1"}]
        expected = "\n".join([
            "tenant=t project=proj-uid",
            palette_axi.toon("edgehosts", ["name", "uid", "state", "health", "cluster"], expected_rows),
            "\ncount:1 unhealthy:0 unassigned:0",
        ]) + "\n"
        self.assertEqual(out, expected)

    def test_wide_adds_hardware_columns_via_per_host_fetch(self):
        out = self._run(wide=True)
        self.assertIn("/v1/edgehosts/host-1", [c[1] for c in self.calls],
                      "spec.device absent from the search item -- must fall back to a GET")
        self.assertIn("cores,memGB,disks,sanDisks,ip,secureBoot", out)
        self.assertIn("96,768.0,2,1,10.0.0.9,true", out)

    def test_min_cores_filters_and_reports_filtered_from(self):
        out = self._run(min_cores=100)
        self.assertIn("/v1/edgehosts/host-1", [c[1] for c in self.calls],
                      "--min-cores needs the same device fetch --wide does")
        self.assertIn("edgehosts[0]", out)
        self.assertIn("filtered_from:1", out)

    def test_label_filter_matches_kv(self):
        out = self._run(label="env=prod")
        self.assertIn("edgehosts[1]", out)
        self.assertIn("filtered_from:1", out)
        out2 = self._run(label="env=staging")
        self.assertIn("edgehosts[0]", out2)
        self.assertIn("filtered_from:1", out2)

    def test_bad_label_syntax_exits_usage(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(label="no-equals-sign")
        self.assertEqual(ctx.exception.code, palette_axi.E_USAGE)


class TestProjectClusterCount(unittest.TestCase):
    """Regression on a bug this tool shipped: summing status.clustersHealth.

    Its buckets overlap. Live on 9/8/26, CSE-Colton-Babcock reported
    running=4 unhealthy=6 while holding 6 clusters, and 17 of the first 40
    projects disagreed the same way. status.usage.clusters is one row per cluster.
    """

    def test_overlapping_health_buckets_are_not_summed(self):
        p = {"status": {"clustersHealth": {"errored": 0, "healthy": 0,
                                           "running": 4, "unhealthy": 6},
                        "usage": {"clusters": [{"uid": "a"}, {"uid": "b"}, {"uid": "c"},
                                               {"uid": "d"}, {"uid": "e"}, {"uid": "f"}]}}}
        self.assertEqual(palette_axi._project_cluster_count(p), 6,
                         "summed the overlapping health buckets again (would give 10)")

    def test_zero_and_missing_are_zero_not_a_crash(self):
        self.assertEqual(palette_axi._project_cluster_count({}), 0)
        self.assertEqual(palette_axi._project_cluster_count({"status": None}), 0)
        self.assertEqual(
            palette_axi._project_cluster_count({"status": {"usage": {"clusters": None}}}), 0)


class TestCloudAccounts(unittest.TestCase):
    """cloudaccounts: GET /v1/cloudaccounts/summary only. See the why-comment
    above cmd_cloudaccounts in cli.py for the live evidence this pins down —
    secrets live only in the per-cloud endpoints' spec, /v1/cloudaccounts/
    openstack 404s, cloudType/filters query params are silently ignored by
    summary, and ProjectUid changes scope rather than filtering rows."""

    ITEMS = [
        {"kind": "aws", "metadata": {"name": "aws-tenant", "uid": "u-aws-1",
                                      "creationTimestamp": "2026-01-02T03:04:05Z",
                                      "annotations": {"scope": "tenant", "overlordUid": ""}},
         "specSummary": {}, "status": {}},
        {"kind": "azure", "metadata": {"name": "azure-tenant", "uid": "u-azure-1",
                                        "creationTimestamp": "2026-01-03T03:04:05Z",
                                        "annotations": {"scope": "tenant"}},
         "specSummary": {}, "status": {}},
        {"kind": "vsphere", "metadata": {"name": "vsphere-proj", "uid": "u-vsphere-1",
                                          "creationTimestamp": "2026-01-04T03:04:05Z",
                                          "annotations": {"scope": "project",
                                                           "projectUid": "6720c668e9746cb63a499425",
                                                           "overlordUid": "pcg-uid-123"}},
         "specSummary": {}, "status": {}},
    ]

    def setUp(self):
        self.calls = []
        self._api, self._key = palette_axi.api, palette_axi.get_api_key
        palette_axi.get_api_key = lambda tenant: "stub-key"

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path, "project": project,
                                "params": dict(params or {})})
            return {"items": [dict(i) for i in self.ITEMS], "listmeta": {"count": len(self.ITEMS)}}

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.get_api_key = self._api, self._key

    def _run(self, project=None, cloud=None):
        args = type("A", (), {"tenant": "custeng-prod", "project": project, "cloud": cloud})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_cloudaccounts(args)
        return buf.getvalue()

    def test_tenant_scope_hits_summary_only_no_project_no_filters(self):
        out = self._run()
        self.assertEqual(len(self.calls), 1, "expected exactly one request (summary is a single call)")
        call = self.calls[0]
        self.assertEqual(call["path"], "/v1/cloudaccounts/summary")
        self.assertIsNone(call["project"], "tenant scope must not pass a ProjectUid")
        self.assertIsNone(call["params"].get("filters"), "summary ignores filters live -- never send one")
        self.assertIn("scope=tenant", out)
        self.assertIn("cloudaccounts[3]", out)
        self.assertIn("count:3 projectScoped:1 tenantScoped:2", out)
        self.assertIn("clouds: aws=1 azure=1 vsphere=1", out)

    def test_project_scope_passes_projectuid_through(self):
        uid = "6720c668e9746cb63a499425"  # real Palette-shaped uid: 24 hex chars
        out = self._run(project=uid)
        self.assertEqual(self.calls[0]["project"], uid)
        self.assertIn(f"scope=project project={uid}", out)

    def test_cloud_filter_case_insensitive(self):
        out = self._run(cloud="AWS")
        self.assertIn("cloudaccounts[1]", out)
        self.assertIn("aws-tenant", out)
        self.assertNotIn("azure-tenant", out)

    def test_cloud_filter_no_match_still_shows_unfiltered_clouds(self):
        out = self._run(cloud="gcp")
        self.assertIn("cloudaccounts[0]{name,uid,cloud,scope,pcg,created}: (none)", out)
        self.assertIn("clouds: aws=1 azure=1 vsphere=1", out)

    def test_pcg_true_false_and_absent_stay_distinct(self):
        out = self._run()
        rows = {ln.strip().split(",")[0]: ln.strip() for ln in out.splitlines()
                if ln.strip().split(",")[0] in ("aws-tenant", "azure-tenant", "vsphere-proj")}
        self.assertEqual(len(rows), 3, "expected all three fixture rows rendered")
        self.assertIn(",false,", rows["aws-tenant"], "overlordUid '' -> known-false")
        self.assertIn(",,", rows["azure-tenant"], "overlordUid absent -> empty/unknown cell")
        self.assertIn(",true,", rows["vsphere-proj"], "overlordUid set -> known-true")

    def test_secret_fields_never_reach_stdout(self):
        """specSummary is documented empty, but guard against a future response
        shape carrying spec.secretKey/secretToken the way the per-cloud
        endpoints do -- this verb must never print one."""
        leaky = [dict(i, spec={"secretKey": "SEKRIT-abc", "secretToken": "SEKRIT-def"})
                 for i in self.ITEMS]

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            return {"items": leaky, "listmeta": {"count": len(leaky)}}

        palette_axi.api = fake_api
        out = self._run()
        self.assertNotIn("SEKRIT", out)


class TestRegistries(unittest.TestCase):
    """registries: GET /v1/registries/metadata for listing (the only endpoint
    that returns pack+helm+oci together -- GET /v1/registries/oci itself
    405s, Allow: DELETE, confirmed live 9/15/26), then a per-kind GET for
    describe. See the why-comment above cmd_registries in cli.py for the
    full evidence trail these fixtures pin down."""

    METADATA_ITEMS = [
        {"kind": "pack", "name": "Public Repo", "uid": "5eecc89d0b150045ae661cef",
         "isDefault": True, "isPrivate": False, "scope": "cluster"},
        {"kind": "helm", "name": "Bitnami", "uid": "60618888279c905820a300fe",
         "isDefault": True, "isPrivate": False, "scope": "cluster"},
        {"kind": "oci", "name": "ecr-registry", "uid": "64eaff453040297344bcad5d",
         "isDefault": False, "isPrivate": True, "scope": "cluster"},
    ]

    def setUp(self):
        self.calls = []
        self._api, self._key = palette_axi.api, palette_axi.get_api_key
        palette_axi.get_api_key = lambda tenant: "stub-key"

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path, "params": dict(params or {})})
            if path == "/v1/registries/metadata":
                return {"items": [dict(i) for i in self.METADATA_ITEMS]}
            if path == "/v1/registries/pack/5eecc89d0b150045ae661cef":
                return {"kind": "pack",
                        "metadata": {"name": "Public Repo", "uid": "5eecc89d0b150045ae661cef"},
                        "spec": {"auth": {"password": "SEKRIT-pw", "token": "SEKRIT-tok", "type": "basic",
                                          "tls": {"enabled": False}},
                                 "endpoint": "https://registry.spectrocloud.com", "private": False,
                                 "scope": "cluster"},
                        "status": {"packSyncStatus": {"status": "Completed"}}}
            if path == "/v1/registries/helm/60618888279c905820a300fe":
                return {"kind": "helm",
                        "metadata": {"name": "Bitnami", "uid": "60618888279c905820a300fe"},
                        "spec": {"auth": {"type": "noAuth", "tls": {"enabled": False}},
                                 "endpoint": "https://charts.bitnami.com/bitnami", "isPrivate": False,
                                 "scope": "cluster"},
                        "status": {"helmSyncStatus": {"status": "InProgress"}}}
            if path == "/v1/registries/oci/64eaff453040297344bcad5d":
                # Flat spec object -- NO metadata/status wrapper, confirmed live.
                return {"auth": {"password": "SEKRIT-pw", "token": "SEKRIT-tok", "type": "token",
                                 "tls": {"enabled": True}},
                        "endpoint": "415789037893.dkr.ecr.us-east-1.amazonaws.com",
                        "providerType": "pack", "scope": "cluster", "type": "ecr"}
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.get_api_key = self._api, self._key

    def _run(self, ref=None, kind=None, project=None):
        args = type("A", (), {"tenant": "custeng-prod", "ref": ref, "kind": kind, "project": project})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_registries(args)
        return buf.getvalue()

    def test_list_hits_metadata_only_once_no_matter_the_kind_count(self):
        out = self._run()
        self.assertEqual(len(self.calls), 1, "list must be a single call to the metadata endpoint")
        self.assertEqual(self.calls[0]["path"], "/v1/registries/metadata")
        self.assertIn("registries[3]", out)
        self.assertIn("kinds: helm=1 oci=1 pack=1", out)

    def test_kind_filter_is_client_side_not_a_query_param(self):
        """The metadata endpoint ignores `kind=` server-side (confirmed live) --
        the filter must happen after the fetch, and the request itself must
        never claim a kind param the API would silently ignore anyway."""
        out = self._run(kind="pack")
        self.assertEqual(self.calls[0]["path"], "/v1/registries/metadata")
        self.assertNotIn("kind", self.calls[0]["params"])
        self.assertIn("registries[1]", out)
        self.assertIn("Public Repo", out)
        self.assertNotIn("Bitnami", out)
        self.assertNotIn("ecr-registry", out)

    def test_describe_pack_calls_the_pack_endpoint_not_helm_or_oci(self):
        out = self._run(ref="Public Repo")
        paths = [c["path"] for c in self.calls]
        self.assertIn("/v1/registries/pack/5eecc89d0b150045ae661cef", paths)
        self.assertNotIn("/v1/registries/helm/5eecc89d0b150045ae661cef", paths)
        self.assertIn("registry[1]", out)
        self.assertIn("Completed", out, "packSyncStatus.status should render as syncStatus")

    def test_describe_oci_uses_the_flat_endpoint_and_pool_fields_for_the_rest(self):
        """oci's own describe body has no isDefault/isPrivate/metadata -- those
        must come from the metadata pool, not be silently blank."""
        out = self._run(ref="64eaff453040297344bcad5d", kind="oci")
        self.assertEqual(self.calls[-1]["path"], "/v1/registries/oci/64eaff453040297344bcad5d")
        self.assertIn("ecr-registry", out)
        self.assertIn("ecr", out, "the oci-only `type` field (ecr) should surface as ociType")
        self.assertIn(",true,", out, "isPrivate=true from the metadata pool must reach the row")

    def test_describe_never_leaks_the_masked_auth_fields(self):
        out = self._run(ref="Public Repo")
        self.assertNotIn("SEKRIT", out)

    def test_describe_no_match_exits_notfound(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(ref="does-not-exist")
        self.assertEqual(ctx.exception.code, palette_axi.E_NOTFOUND)


class TestCloudConfig(unittest.TestCase):
    """cloudconfig: resolves a cluster's spec.cloudConfigRef {kind,uid} first
    (there is no tenant-wide cloudconfig list), then GETs
    /v1/cloudconfigs/{kind}/{uid} -- confirmed live 9/15/26 against
    rpi-inference (SA-Craig-Smith, edge-native) that this single call returns
    both clusterConfig and machinePoolConfig inline. See the why-comment
    above cmd_cloudconfig in cli.py."""

    CLUSTERS = [
        {"metadata": {"name": "rpi-inference", "uid": "693064bc882df8800821d248"},
         "spec": {"cloudType": "edge-native"}, "status": {}},
        {"metadata": {"name": "hf-connect-eks-demo", "uid": "6aa8687d58d7515b53a51dda"},
         "spec": {"cloudType": "eks"}, "status": {}},
    ]

    CLOUDCONFIG = {
        "metadata": {"name": "rpi-inference-edge-native-config", "uid": "693064bb882df8800727d3d0"},
        "spec": {
            "clusterConfig": {
                "controlPlaneEndpoint": {"host": "100.64.192.1", "type": "VIP"},
                "ntpServers": ["pool.ntp.org"],
                "overlayNetworkConfiguration": {"cidr": "100.64.192.0/23", "enable": True},
            },
            "machinePoolConfig": [
                {"name": "control-plane-pool", "size": 3, "isControlPlane": True,
                 "hosts": [{"hostAddress": "192.168.8.192"}, {"hostAddress": "192.168.8.217"},
                           {"hostAddress": "192.168.8.198"}]},
                {"name": "worker-pool", "size": 1, "hosts": [{"hostAddress": "192.168.8.220"}]},
            ],
        },
        "status": {"conditions": None},
    }

    def setUp(self):
        self.calls = []
        self._api, self._key = palette_axi.api, palette_axi.get_api_key
        self._resolve, self._listclusters = palette_axi.resolve_project, palette_axi.list_clusters
        palette_axi.get_api_key = lambda tenant: "stub-key"
        palette_axi.resolve_project = lambda ref, key: "proj-uid"
        palette_axi.list_clusters = lambda key, proj: [dict(c) for c in self.CLUSTERS]

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path})
            if path == "/v1/spectroclusters/693064bc882df8800821d248":
                return {"metadata": {"name": "rpi-inference"},
                        "spec": {"cloudType": "edge-native",
                                 "cloudConfigRef": {"kind": "edge-native",
                                                     "name": "rpi-inference-edge-native-config",
                                                     "uid": "693064bb882df8800727d3d0"}}}
            if path == "/v1/spectroclusters/6aa8687d58d7515b53a51dda":
                return {"metadata": {"name": "hf-connect-eks-demo"}, "spec": {"cloudType": "eks"}}
            if path == "/v1/cloudconfigs/edge-native/693064bb882df8800727d3d0":
                return dict(self.CLOUDCONFIG)
            raise AssertionError(f"unexpected call: {method} {path}")

        palette_axi.api = fake_api

    def tearDown(self):
        palette_axi.api, palette_axi.get_api_key = self._api, self._key
        palette_axi.resolve_project, palette_axi.list_clusters = self._resolve, self._listclusters

    def _run(self, ref, kind=None, full=False):
        args = type("A", (), {"tenant": "custeng-prod", "project": "p", "ref": ref,
                               "kind": kind, "full": full})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            palette_axi.cmd_cloudconfig(args)
        return buf.getvalue()

    def test_resolves_cluster_then_calls_cloudconfigs_by_kind_and_uid(self):
        out = self._run("rpi-inference")
        paths = [c["path"] for c in self.calls]
        self.assertIn("/v1/spectroclusters/693064bc882df8800821d248", paths)
        self.assertIn("/v1/cloudconfigs/edge-native/693064bb882df8800727d3d0", paths,
                       "must call the kind+uid from cloudConfigRef, not the cluster's own uid")
        self.assertIn("cloudconfig[1]", out)
        self.assertIn("100.64.192.1", out)

    def test_machine_pools_come_from_the_inline_config_no_extra_call(self):
        out = self._run("rpi-inference")
        self.assertIn("machinePools[2]", out)
        self.assertIn("control-plane-pool", out)
        self.assertIn("worker-pool", out)
        self.assertIn("poolCount:2", out)

    def test_cluster_with_no_cloudconfigref_exits_notfound(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("hf-connect-eks-demo")
        self.assertEqual(ctx.exception.code, palette_axi.E_NOTFOUND)

    def test_bare_uid_with_kind_skips_cluster_resolution(self):
        out = self._run("693064bb882df8800727d3d0", kind="edge-native")
        paths = [c["path"] for c in self.calls]
        self.assertNotIn("/v1/spectroclusters/693064bc882df8800821d248", paths,
                          "a bare cloudconfig uid + --kind must not require a cluster match")
        self.assertIn("/v1/cloudconfigs/edge-native/693064bb882df8800727d3d0", paths)
        self.assertIn("cloudconfig[1]", out)

    def test_no_match_and_no_kind_exits_notfound(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("nonexistent-cluster")
        self.assertEqual(ctx.exception.code, palette_axi.E_NOTFOUND)


class TestHelpForEverySubcommand(unittest.TestCase):
    """CI's own --help smoke loop lives in .github/workflows/ci.yml, a
    protected path this change does not touch, so it never learns about a
    new verb on its own. This offline test iterates the real subparser
    choices instead, so adding a verb without wiring its --help is caught
    here rather than only live."""

    def test_every_verb_help_exits_zero(self):
        real_add_subparsers = argparse.ArgumentParser.add_subparsers
        captured = {}

        def spy(self, *a, **k):
            action = real_add_subparsers(self, *a, **k)
            captured["action"] = action
            return action

        argparse.ArgumentParser.add_subparsers = spy
        old_argv = sys.argv
        try:
            sys.argv = ["palette-axi", "--help"]
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    palette_axi.main()
        finally:
            argparse.ArgumentParser.add_subparsers = real_add_subparsers
            sys.argv = old_argv

        verbs = sorted(captured["action"].choices)
        self.assertIn("cloudaccounts", verbs, "the new verb must be registered")
        self.assertIn("registries", verbs, "the new verb must be registered")
        self.assertIn("cloudconfig", verbs, "the new verb must be registered")
        self.assertIn("edgehost", verbs, "the new verb must be registered")

        for verb in verbs:
            with self.subTest(verb=verb):
                sys.argv = ["palette-axi", verb, "--help"]
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit) as ctx:
                            palette_axi.main()
                    self.assertEqual(ctx.exception.code, 0)
                finally:
                    sys.argv = old_argv


class TestKeyCache(unittest.TestCase):
    """Per-tenant API key + item-id cache in $XDG_RUNTIME_DIR/palette-axi.

    9/21/26 16:53Z: the shared 1Password service account started refusing
    item reads with "Too many requests. Your client has been rate-limited."
    Every palette-axi verb spends 2 op calls (op item list, then op item
    get) getting a key it already resolved a moment ago -- an agent running
    dozens of verbs in a few minutes burns the limit fast. This cache cuts
    that to at most one op call per TTL window, not per invocation. `op`
    itself is stubbed out (this module runs no real 1Password calls, per
    AGENTS.md's "offline tests" rule) and XDG_RUNTIME_DIR points at a temp
    dir so nothing here touches a real tmpfs or a real cache from another
    session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="palette-axi-test-")
        self._saved_env = {k: os.environ.get(k) for k in
                            ("XDG_RUNTIME_DIR", "PALETTE_AXI_KEY_TTL",
                             "PALETTE_API_KEY", "PALETTE_AXI_OP_ITEM")}
        os.environ["XDG_RUNTIME_DIR"] = self.tmp
        for k in ("PALETTE_AXI_KEY_TTL", "PALETTE_API_KEY", "PALETTE_AXI_OP_ITEM"):
            os.environ.pop(k, None)

        self.list_calls = 0
        self.get_calls = 0

        def fake_item_id(tenant):
            self.list_calls += 1
            return "item-123"

        def fake_secret(item_id):
            self.get_calls += 1
            return "secret-abc"

        self._real_item_id = palette_axi._op_item_id_for_tenant
        self._real_secret = palette_axi._op_secret_value
        palette_axi._op_item_id_for_tenant = fake_item_id
        palette_axi._op_secret_value = fake_secret
        palette_axi._LAST_KEY_CACHE_PATH = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        palette_axi._op_item_id_for_tenant = self._real_item_id
        palette_axi._op_secret_value = self._real_secret
        palette_axi._LAST_KEY_CACHE_PATH = None
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _key_path(self, tenant="acme"):
        return os.path.join(self.tmp, "palette-axi", palette_axi._key_cache_name(tenant))

    def _item_path(self, tenant="acme"):
        return os.path.join(self.tmp, "palette-axi", palette_axi._item_cache_name(tenant))

    def test_cache_hit_makes_zero_op_calls(self):
        palette_axi.get_api_key("acme")  # cold: writes both caches
        self.list_calls = self.get_calls = 0
        key = palette_axi.get_api_key("acme")
        self.assertEqual(key, "secret-abc")
        self.assertEqual((self.list_calls, self.get_calls), (0, 0),
                          "a warm key cache must not call op at all")

    def test_key_miss_with_warm_item_cache_costs_one_op_call(self):
        palette_axi.get_api_key("acme")  # warms both caches
        os.unlink(self._key_path())  # force a key-cache miss only
        self.list_calls = self.get_calls = 0
        key = palette_axi.get_api_key("acme")
        self.assertEqual(key, "secret-abc")
        self.assertEqual(self.list_calls, 0, "the item-id cache should have stayed warm")
        self.assertEqual(self.get_calls, 1, "a key miss still needs exactly one item-get call")

    def test_ttl_expiry_refetches(self):
        os.environ["PALETTE_AXI_KEY_TTL"] = "1"
        palette_axi.get_api_key("acme")
        old = time.time() - 10  # back-date past the 1s TTL instead of sleeping
        os.utime(self._key_path(), (old, old))
        self.list_calls = self.get_calls = 0
        key = palette_axi.get_api_key("acme")
        self.assertEqual(key, "secret-abc")
        self.assertEqual(self.get_calls, 1, "an expired cache entry must not be reused")

    def test_dir_and_file_modes(self):
        palette_axi.get_api_key("acme")
        d = os.path.join(self.tmp, "palette-axi")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self._key_path()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self._item_path()).st_mode), 0o600)

    def test_no_xdg_runtime_dir_means_no_file_written(self):
        del os.environ["XDG_RUNTIME_DIR"]
        key = palette_axi.get_api_key("acme")
        self.assertEqual(key, "secret-abc", "must still resolve a key, just not cache it")
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "palette-axi")))

    def test_ttl_zero_disables_caching(self):
        os.environ["PALETTE_AXI_KEY_TTL"] = "0"
        palette_axi.get_api_key("acme")
        self.assertFalse(os.path.exists(self._key_path()), "TTL=0 must not write a key cache file")
        self.list_calls = self.get_calls = 0
        palette_axi.get_api_key("acme")
        self.assertEqual(self.get_calls, 1, "TTL=0 must re-fetch the key every call")

    def test_palette_api_key_still_wins_and_skips_the_cache(self):
        os.environ["PALETTE_API_KEY"] = "env-key"
        key = palette_axi.get_api_key("acme")
        self.assertEqual(key, "env-key")
        self.assertEqual((self.list_calls, self.get_calls), (0, 0), "op must never run when the env key is set")
        self.assertFalse(os.path.exists(self._key_path()), "the env key must never be written to disk")

    def test_401_deletes_the_cached_key_file(self):
        palette_axi.get_api_key("acme")  # warm the cache
        self.assertTrue(os.path.exists(self._key_path()))
        self.list_calls = self.get_calls = 0
        cached_key = palette_axi.get_api_key("acme")  # served from cache
        self.assertEqual(self.get_calls, 0, "sanity check: this call must be a cache hit")

        def raise_401(*a, **k):
            raise urllib.error.HTTPError(
                "https://api.spectrocloud.com/v1/projects", 401, "Unauthorized",
                {}, io.BytesIO(b'{"message":"invalid api key"}'))

        real_urlopen = palette_axi.urllib.request.urlopen
        palette_axi.urllib.request.urlopen = raise_401
        try:
            with self.assertRaises(SystemExit) as ctx:
                palette_axi.api("GET", "/v1/projects", cached_key)
        finally:
            palette_axi.urllib.request.urlopen = real_urlopen

        self.assertEqual(ctx.exception.code, palette_axi.E_ERR, "the existing 401 exit code must not change")
        self.assertFalse(os.path.exists(self._key_path()), "a 401 must delete the cached key file it was serving")


class TestApiReadTimeout(unittest.TestCase):
    """A read timeout mid-body raises a bare TimeoutError, not URLError. Seen
    live 9/21/26 on the loves tenant: it escaped as a raw traceback."""

    def test_read_timeout_dies_cleanly_with_err_exit(self):
        class SlowResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                raise TimeoutError("The read operation timed out")

        real_urlopen = palette_axi.urllib.request.urlopen
        palette_axi.urllib.request.urlopen = lambda *a, **k: SlowResp()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                palette_axi.api("GET", "/v1/dashboard/projects", "k")
        finally:
            palette_axi.urllib.request.urlopen = real_urlopen
        self.assertEqual(ctx.exception.code, palette_axi.E_ERR)
        self.assertIn("timeout after 30s reading GET /v1/dashboard/projects", err.getvalue())


class TestKeyCacheDoctorRow(unittest.TestCase):
    """`doctor`'s config table gets one row for key-cache state -- never a
    new connector, never the key value itself."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="palette-axi-test-")
        self._saved_env = {k: os.environ.get(k) for k in
                            ("XDG_RUNTIME_DIR", "PALETTE_AXI_KEY_TTL", "PALETTE_API_KEY")}
        os.environ["XDG_RUNTIME_DIR"] = self.tmp
        for k in ("PALETTE_AXI_KEY_TTL", "PALETTE_API_KEY"):
            os.environ.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_key_cache(self, tenant, value):
        d = os.path.join(self.tmp, "palette-axi")
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, palette_axi._key_cache_name(tenant)), "w") as f:
            json.dump(value, f)

    def test_miss_when_no_cache_file(self):
        self.assertEqual(palette_axi._key_cache_status("acme"), "miss")

    def test_hit_reports_age(self):
        self._write_key_cache("acme", {"key": "x"})
        status = palette_axi._key_cache_status("acme")
        self.assertTrue(status.startswith("hit (age "), status)

    def test_disabled_when_ttl_zero(self):
        os.environ["PALETTE_AXI_KEY_TTL"] = "0"
        self.assertIn("disabled", palette_axi._key_cache_status("acme"))

    def test_disabled_when_palette_api_key_set(self):
        os.environ["PALETTE_API_KEY"] = "x"
        self.assertIn("disabled", palette_axi._key_cache_status("acme"))

    def test_disabled_when_no_xdg_runtime_dir(self):
        del os.environ["XDG_RUNTIME_DIR"]
        self.assertIn("disabled", palette_axi._key_cache_status("acme"))

    def test_status_never_contains_the_key_value(self):
        self._write_key_cache("acme", {"key": "super-secret-value"})
        status = palette_axi._key_cache_status("acme")
        self.assertNotIn("super-secret-value", status)

    def test_config_rows_includes_key_cache_row_without_the_secret(self):
        self._write_key_cache("acme", {"key": "super-secret-value"})
        rows = palette_axi._config_rows("acme")
        cache_row = next((r for r in rows if r["var"] == "key cache"), None)
        self.assertIsNotNone(cache_row, "expected a 'key cache' row in the config table")
        self.assertNotIn("super-secret-value", json.dumps(rows))
        ttl_row = next((r for r in rows if r["var"] == "PALETTE_AXI_KEY_TTL"), None)
        self.assertIsNotNone(ttl_row, "expected a PALETTE_AXI_KEY_TTL row in the config table")


class TestOpRateLimit(unittest.TestCase):
    """9/21/26 16:53Z: the shared 1Password service account started refusing
    item reads with 'Too many requests. Your client has been rate-limited.'
    and every verb died on an op failure that read identically to any other
    op error (bad vault, typo'd item, `op` logged out). die() now names the
    cause and the escape hatch instead."""

    def setUp(self):
        self._run = palette_axi._run

    def tearDown(self):
        palette_axi._run = self._run

    def _stub(self, stderr):
        def fake_run(cmd, timeout=30, input_text=None):
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": stderr})()
        palette_axi._run = fake_run

    def test_item_list_rate_limit_message(self):
        self._stub("[ERROR] 2026/09/21 16:53:02 Too many requests. "
                    "Your client has been rate-limited.")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                palette_axi._op_item_id_for_tenant("loves")
        self.assertEqual(ctx.exception.code, palette_axi.E_ERR, "exit code must not change")
        err = buf.getvalue()
        self.assertIn("rate-limited", err)
        self.assertIn("PALETTE_API_KEY", err)

    def test_item_get_rate_limit_message(self):
        self._stub("[ERROR] Too many requests. Your client has been rate-limited.")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                palette_axi._op_secret_value("item-123")
        self.assertEqual(ctx.exception.code, palette_axi.E_ERR)
        self.assertIn("rate-limited", buf.getvalue())
        self.assertIn("PALETTE_API_KEY", buf.getvalue())

    def test_non_rate_limit_op_failure_keeps_the_plain_message(self):
        self._stub("[ERROR] 401: Authentication required.")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit):
                palette_axi._op_item_id_for_tenant("loves")
        self.assertNotIn("rate-limited", buf.getvalue())
