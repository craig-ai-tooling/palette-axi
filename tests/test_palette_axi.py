#!/usr/bin/env python3
"""Unit tests for the pure, network-free parts of palette-axi: the TOON encoder
(must match opp-axi's contract exactly), the null-safe JSON getters, and the
name/health helpers. Nothing here touches the network or 1Password — that's
what "ACTUALLY RUN IT" in the build task covered, live, against custeng-prod.
"""
import argparse
import contextlib
import io
import sys
import unittest

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
        palette_axi.cmd_edgehosts(type("A", (), {"tenant": "t", "project": "p"})())
        paths = [p for _, p, _ in self.calls]
        self.assertTrue(paths, "cmd_edgehosts issued no request")
        self.assertNotIn("/v1/edgehosts", paths, "still calling the 405 endpoint")
        self.assertIn("/v1/dashboard/edgehosts/search", paths)

    def test_it_posts_a_search_body(self):
        """The search endpoint is POST-only; a GET returns 405 and a POST with no
        body is not what the API accepts."""
        palette_axi.cmd_edgehosts(type("A", (), {"tenant": "t", "project": "p"})())
        method, _, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertIsNotNone(body, "no JSON body sent to a POST-only search endpoint")
        self.assertIn("filter", body)


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
