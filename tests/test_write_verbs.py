#!/usr/bin/env python3
"""Offline tests for the 0.6.0 verbs: request building and the safety checks.

Every test drives the real CLI entry point (main() with a real argv) against a
stubbed api()/api_raw(), and asserts on the requests actually issued -- never on
source text. Fixtures are synthetic; uids are 24-hex like Palette's."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error

from palette_axi import cli

PROJ = "6720c668e9746cb63a490001"
OTHER = "6720c668e9746cb63a490002"
CL = "693064bc882df8800821a001"
PR1 = "693064bc882df8800821b001"
PR2 = "693064bc882df8800821b002"
NEW = "693064bc882df8800821b0ff"


def ann(project=PROJ, scope="project"):
    return {"annotations": {"scope": scope, "projectUid": project}}


class Harness(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.routes = {}
        self.raw = {}
        self._saved = {n: getattr(cli, n) for n in ("api", "api_raw", "get_api_key", "resolve_project")}
        cli.get_api_key = lambda tenant: "stub-key"
        cli.resolve_project = lambda ref, key: PROJ if ref else cli.die("no project", cli.E_USAGE)

        def fake_api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
            self.calls.append({"method": method, "path": path, "project": project,
                               "params": params, "body": json_body})
            r = self.routes.get((method, path))
            if r is None:
                raise AssertionError(f"unexpected call: {method} {path}")
            return r(json_body) if callable(r) else json.loads(json.dumps(r))

        def fake_raw(method, path, api_key, project=None, params=None, timeout=60):
            self.calls.append({"method": method, "path": path, "project": project, "params": params,
                               "body": None, "raw": True})
            if path not in self.raw:
                raise AssertionError(f"unexpected raw call: {path}")
            return self.raw[path]

        cli.api, cli.api_raw = fake_api, fake_raw

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(cli, n, v)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["palette-axi", *argv]
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main()
        except SystemExit as e:
            code = e.code or 0
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def writes(self):
        return [c for c in self.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")
                and not c["path"].startswith("/v1/dashboard/")]


class TestRewriteArgv(unittest.TestCase):
    def test_two_and_three_word_forms(self):
        self.assertEqual(cli.rewrite_argv(["profile", "create", "--file", "x"]), ["profile-create", "--file", "x"])
        self.assertEqual(cli.rewrite_argv(["profile", "variables", "set", "p"]), ["profile-variables-set", "p"])
        self.assertEqual(cli.rewrite_argv(["cluster", "profiles", "swap", "c"]), ["cluster-profiles-swap", "c"])
        self.assertEqual(cli.rewrite_argv(["cluster", "rbac", "c"]), ["cluster-rbac", "c"])
        self.assertEqual(cli.rewrite_argv(["cluster", "rbac", "set", "c"]), ["cluster-rbac-set", "c"])

    def test_global_tenant_flag_is_skipped(self):
        self.assertEqual(cli.rewrite_argv(["--tenant", "t", "cluster", "delete", "c"]),
                         ["--tenant", "t", "cluster-delete", "c"])

    def test_existing_read_verbs_untouched(self):
        for argv in (["profile", "craig"], ["cluster", "rpi", "--profiles"], ["clusters"]):
            self.assertEqual(cli.rewrite_argv(argv), argv)


class TestRedact(unittest.TestCase):
    def test_nested_secret_keys_masked(self):
        out = cli.redact({"spec": {"apiKey": "s3cret", "apiEndpoint": "http://x"},
                          "list": [{"password": "p"}, {"token": ""}]})
        self.assertEqual(out["spec"]["apiKey"], "********")
        self.assertEqual(out["spec"]["apiEndpoint"], "http://x")
        self.assertEqual(out["list"][0]["password"], "********")
        self.assertNotIn("s3cret", json.dumps(out))


class TestListProfiles(Harness):
    def test_versions_expand_newest_first(self):
        self.routes[("POST", "/v1/dashboard/clusterprofiles")] = {
            "items": [{"metadata": {"name": "p", "uid": PR2},
                       "specSummary": {"published": {"type": "add-on", "cloudType": "all"},
                                       "versions": [{"uid": PR1, "version": "1.0.9"},
                                                    {"uid": PR2, "version": "1.0.10"}]}}],
            "listmeta": {"count": 1}}
        rows = cli.list_profiles("k", PROJ)
        self.assertEqual([r["metadata"]["uid"] for r in rows], [PR2, PR1])
        self.assertEqual(rows[0]["spec"]["published"]["type"], "add-on")
        self.assertNotIn(("GET", "/v1/clusterprofiles"), [(c["method"], c["path"]) for c in self.calls])


class TestWriteProjectRequired(Harness):
    def test_env_project_is_not_enough_for_a_write(self):
        os.environ["PALETTE_PROJECT"] = PROJ
        try:
            code, _, err = self.run_cli("cluster", "delete", CL, "--yes")
        finally:
            del os.environ["PALETTE_PROJECT"]
        self.assertEqual(code, cli.E_USAGE)
        self.assertIn("--project", err)
        self.assertEqual(self.writes(), [])


class TestProfileCreate(Harness):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.body = {"metadata": {"name": "p"}, "spec": {"version": "1.0.0", "template": {"packs": []},
                                                         "variables": [{"name": "v", "defaultValue": "x"}]}}
        self.file = os.path.join(self.tmp.name, "b.json")
        json.dump(self.body, open(self.file, "w"))
        self.routes[("POST", "/v1/clusterprofiles")] = {"uid": NEW}
        self.routes[("PUT", f"/v1/clusterprofiles/{NEW}/variables")] = {}
        self.routes[("PATCH", f"/v1/clusterprofiles/{NEW}/publish")] = {}

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_create_puts_variables_then_publishes(self):
        code, out, _ = self.run_cli("profile", "create", "--file", self.file, "--project", "p", "--publish")
        self.assertEqual(code, 0)
        seq = [(c["method"], c["path"]) for c in self.writes()]
        self.assertEqual(seq, [("POST", "/v1/clusterprofiles"),
                               ("PUT", f"/v1/clusterprofiles/{NEW}/variables"),
                               ("PATCH", f"/v1/clusterprofiles/{NEW}/publish")])
        self.assertTrue(all(c["project"] == PROJ for c in self.writes()))
        self.assertEqual(self.writes()[1]["body"], {"variables": self.body["spec"]["variables"]})
        self.assertIn("changed[1]", out)

    def test_dry_run_sends_nothing_and_prints_the_plan(self):
        code, out, _ = self.run_cli("profile", "create", "--file", self.file, "--project", "p", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("POST,/v1/clusterprofiles," + PROJ, out)
        self.assertIn('"name": "p"', out)


class TestProfileVersion(Harness):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.values = os.path.join(self.tmp.name, "v.yaml")
        open(self.values, "w").write("a: 2\n")
        self.routes[("GET", f"/v1/clusterprofiles/{PR1}")] = {
            "metadata": {"name": "infra", "uid": PR1, **ann()},
            "spec": {"version": "1.0.0", "versions": [{"version": "1.0.0"}],
                     "published": {"packs": [{"name": "edge-k8s", "packUid": "pk1", "tag": "1.33.5",
                                              "type": "spectro", "layer": "k8s", "values": "a: 1\n"}]}},
            "status": {}}
        self.routes[("POST", f"/v1/clusterprofiles/{PR1}/clone")] = {"uid": NEW}
        self.routes[("PUT", f"/v1/clusterprofiles/{NEW}/packs/edge-k8s")] = {}
        self.routes[("PATCH", f"/v1/clusterprofiles/{NEW}/publish")] = {}

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_clone_then_pack_values_on_new_uid_then_publish(self):
        code, _, err = self.run_cli("profile", "version", "--from", PR1, "--version", "1.0.1",
                                    "--set-pack-values", f"edge-k8s={self.values}", "--project", "p")
        self.assertEqual(code, 0, err)
        w = self.writes()
        self.assertEqual(w[0]["path"], f"/v1/clusterprofiles/{PR1}/clone")
        self.assertEqual(w[0]["body"], {"metadata": {"name": "infra", "version": "1.0.1"}})
        self.assertEqual(w[1]["path"], f"/v1/clusterprofiles/{NEW}/packs/edge-k8s")
        self.assertEqual(w[1]["body"]["pack"]["values"], "a: 2\n")
        self.assertEqual(w[1]["body"]["pack"]["uid"], "pk1")
        self.assertEqual(w[2]["path"], f"/v1/clusterprofiles/{NEW}/publish")
        self.assertNotIn(PR1, "".join(c["path"] for c in w[1:]), "the source version must never be edited")

    def test_existing_version_refused(self):
        code, _, _ = self.run_cli("profile", "version", PR1, "--version", "1.0.0", "--project", "p")
        self.assertEqual(code, cli.E_USAGE)
        self.assertEqual(self.writes(), [])


class TestProfileDelete(Harness):
    def prof(self, project=PROJ, in_use=()):
        self.routes[("GET", f"/v1/clusterprofiles/{PR1}")] = {
            "metadata": {"name": "p", "uid": PR1, **ann(project)}, "spec": {"version": "1.0.0"},
            "status": {"inUseClusterUids": list(in_use)}}
        self.routes[("DELETE", f"/v1/clusterprofiles/{PR1}")] = {}

    def test_needs_yes(self):
        self.prof()
        code, _, err = self.run_cli("profile", "delete", PR1, "--project", "p")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertEqual(self.writes(), [])

    def test_refuses_other_project(self):
        self.prof(project=OTHER)
        code, _, err = self.run_cli("profile", "delete", PR1, "--project", "p", "--yes")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertIn("outside --project", err)
        self.assertEqual(self.writes(), [])

    def test_refuses_in_use(self):
        self.prof(in_use=[CL])
        code, _, _ = self.run_cli("profile", "delete", PR1, "--project", "p", "--yes")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertEqual(self.writes(), [])

    def test_deletes_with_yes(self):
        self.prof()
        code, _, _ = self.run_cli("profile", "delete", PR1, "--project", "p", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual([(c["method"], c["path"], c["project"]) for c in self.writes()],
                         [("DELETE", f"/v1/clusterprofiles/{PR1}", PROJ)])


class TestClusterDelete(Harness):
    def cluster(self, state="Running", project=PROJ, scope="project"):
        self.routes[("GET", f"/v1/spectroclusters/{CL}")] = {
            "metadata": {"name": "c1", "uid": CL, **ann(project, scope)}, "status": {"state": state}}
        self.routes[("DELETE", f"/v1/spectroclusters/{CL}")] = {}

    def test_force_refused_unless_deleting(self):
        self.cluster("Running")
        code, _, err = self.run_cli("cluster", "delete", CL, "--project", "p", "--yes", "--force")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertIn("not Deleting", err)
        self.assertEqual(self.writes(), [])

    def test_force_on_deleting_sends_force_param(self):
        self.cluster("Deleting")
        code, _, _ = self.run_cli("cluster", "delete", CL, "--project", "p", "--yes", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(self.writes()[0]["params"], {"forceDelete": "true"})

    def test_tenant_scoped_refused(self):
        self.cluster(scope="tenant")
        code, _, _ = self.run_cli("cluster", "delete", CL, "--project", "p", "--yes")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertEqual(self.writes(), [])

    def test_dry_run_without_yes_prints_plan(self):
        self.cluster()
        code, out, _ = self.run_cli("cluster", "delete", CL, "--project", "p", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"DELETE,/v1/spectroclusters/{CL},{PROJ}", out)
        self.assertEqual(self.writes(), [])


class TestClusterCreate(Harness):
    def file(self, body):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_edge_native_vip_is_refused(self):
        f = self.file({"metadata": {"name": "c"}, "spec": {"cloudConfig": {"vip": "10.0.0.5"}}})
        code, _, err = self.run_cli("cluster", "create", "edge-native", "--file", f, "--project", "p")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertIn("controlPlaneEndpoint", err)
        self.assertEqual(self.writes(), [])

    def test_create_posts_to_kind_path(self):
        self.routes[("POST", "/v1/spectroclusters/maas")] = {"uid": CL}
        f = self.file({"metadata": {"name": "c"}, "spec": {}})
        code, out, _ = self.run_cli("cluster", "create", "maas", "--file", f, "--project", "p")
        self.assertEqual(code, 0)
        self.assertEqual(self.writes()[0]["project"], PROJ)
        self.assertIn(CL, out)


class TestProfileSwap(Harness):
    def setUp(self):
        super().setUp()
        self.routes[("GET", f"/v1/spectroclusters/{CL}")] = {
            "metadata": {"name": "c1", "uid": CL, **ann()},
            "spec": {"clusterProfileTemplates": [{"uid": PR1}, {"uid": "693064bc882df8800821b0aa"}]}}
        self.routes[("PUT", f"/v1/spectroclusters/{CL}/profiles")] = {}

    def vars(self, sensitive=False):
        self.routes[("GET", f"/v1/spectroclusters/{CL}/variables")] = [
            {"profileUid": PR1, "variables": [
                {"name": "range", "value": "10.1.1.80-89", "isSensitive": False},
                {"name": "pw", "value": "********", "isSensitive": sensitive}]}]

    def test_carries_variables_and_keeps_other_profiles(self):
        self.vars()
        code, _, err = self.run_cli("cluster", "profiles", "swap", CL, "--from", PR1, "--to", PR2,
                                    "--var", "iface=br0", "--project", "p")
        self.assertEqual(code, 0, err)
        body = self.writes()[0]["body"]
        self.assertEqual([p["uid"] for p in body["profiles"]], [PR2, "693064bc882df8800821b0aa"])
        got = {v["name"]: v["value"] for v in body["profiles"][0]["variables"]}
        self.assertEqual(got["range"], "10.1.1.80-89")
        self.assertEqual(got["iface"], "br0")

    def test_masked_sensitive_variable_refuses(self):
        self.vars(sensitive=True)
        code, _, err = self.run_cli("cluster", "profiles", "swap", CL, "--from", PR1, "--to", PR2,
                                    "--project", "p")
        self.assertEqual(code, cli.E_REFUSED)
        self.assertIn("pw", err)
        self.assertEqual(self.writes(), [])

    def test_sensitive_resupplied_proceeds(self):
        self.vars(sensitive=True)
        code, _, _ = self.run_cli("cluster", "profiles", "swap", CL, "--from", PR1, "--to", PR2,
                                  "--var", "pw=real", "--project", "p")
        self.assertEqual(code, 0)


class TestKubeconfig(Harness):
    def setUp(self):
        super().setUp()
        self.routes[("GET", f"/v1/spectroclusters/{CL}")] = {"metadata": {"name": "c1", "uid": CL}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_admin_written_mode_600_not_stdout(self):
        self.raw[f"/v1/spectroclusters/{CL}/assets/adminKubeconfig"] = b"apiVersion: v1\nclusters: []\n"
        out_path = os.path.join(self.tmp.name, "k.yaml")
        code, out, _ = self.run_cli("cluster", "kubeconfig", CL, "--project", "p", "--admin", "--out", out_path)
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)
        self.assertNotIn("clusters: []", out)

    def test_reader_placeholder_is_an_error(self):
        self.raw[f"/v1/spectroclusters/{CL}/assets/kubeconfig"] = b'{"Reader":{}}'
        code, _, err = self.run_cli("cluster", "kubeconfig", CL, "--project", "p",
                                    "--out", os.path.join(self.tmp.name, "k"))
        self.assertEqual(code, cli.E_ERR)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "k")))


class TestCloudAccountCreate(Harness):
    def test_key_from_env_and_masked_in_dry_run(self):
        os.environ["PAXI_TEST_MAAS_KEY"] = "consumer:token:secret"
        self.addCleanup(os.environ.pop, "PAXI_TEST_MAAS_KEY")
        self.routes[("GET", "/v1/overlords")] = {"items": [{"metadata": {"name": "pcg1", "uid": NEW}}]}
        code, out, _ = self.run_cli("cloudaccount", "create", "maas", "--project", "p", "--name", "m",
                                    "--endpoint", "http://x:5240/MAAS", "--api-key-env", "PAXI_TEST_MAAS_KEY",
                                    "--pcg", "pcg1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertNotIn("consumer:token:secret", out)
        self.assertIn(f"privateGateway={NEW}", out)
        self.assertEqual(self.writes(), [])

    def test_key_required_off_argv(self):
        code, _, err = self.run_cli("cloudaccount", "create", "maas", "--project", "p", "--name", "m",
                                    "--endpoint", "http://x")
        self.assertEqual(code, cli.E_USAGE)


class TestRetry429(unittest.TestCase):
    def test_429_is_retried_then_succeeds(self):
        attempts = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=30):
            attempts.append(req.get_method())
            if len(attempts) < 3:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "0"},
                                             io.BytesIO(b'{"code":"TooManyRequests"}'))
            return Resp()

        saved_open, saved_sleep = cli.urllib.request.urlopen, cli.time.sleep
        cli.urllib.request.urlopen, cli.time.sleep = fake_urlopen, lambda s: None
        try:
            self.assertEqual(cli.api("POST", "/v1/x", "k", json_body={}), {"ok": True})
        finally:
            cli.urllib.request.urlopen, cli.time.sleep = saved_open, saved_sleep
        self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
