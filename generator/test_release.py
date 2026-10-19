"""The release generator on throwaway Git repositories; nothing is built or pushed."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
import release

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

DIGEST = "@sha256:" + "a" * 64
DESCRIPTOR = {
    "descriptor": 1, "product": "pilot",
    "shared": ["codebase/pilot.backend-services/Directory.*.props"],
    "services": {
        "api": {"build": {"dockerfile": "deployment/backend.Dockerfile", "args": {"SERVICE": "Api"}},
                "paths": ["codebase/pilot.backend-services/Pilot.Api/**"], "health": "/health",
                "settings": {"required": ["Database:Connection"]}, "volumes": {"keys": "/data/keys"},
                "sites": {"app": {"path": "/api"}}, "needs": ["postgres"]},
        "edge": {"build": {"dockerfile": "deployment/edge.Dockerfile", "contexts": {"deployment": "deployment"}},
                 "paths": ["codebase/pilot.frontend-services/**", "deployment/edge/**"], "port": 80,
                 "health": {"command": ["wget", "-q", "-O-", "http://localhost/healthz"]},
                 "capabilities": ["NET_BIND_SERVICE"], "sites": {"app": None, "landing": {"exposure": "public"}}},
    },
}


def git(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout.strip()


class DescriptorTests(unittest.TestCase):
    def test_defaults_fill_every_service(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        api, edge = descriptor["services"]["api"], descriptor["services"]["edge"]
        self.assertEqual(("linux/amd64", 8080, "512m", "30s", "codebase"),
                         (descriptor["platform"], api["port"], api["memory"], api["stopGrace"], api["build"]["context"]))
        self.assertEqual(["CMD", "curl", "--fail", "--silent", "http://localhost:8080/health"], api["health"])
        self.assertEqual({"file": "/app/appsettings.Target.json", "required": ["Database:Connection"]}, api["settings"])
        self.assertEqual({"app": {"port": 80, "path": "/", "exposure": "public"},
                          "landing": {"port": 80, "path": "/", "exposure": "public"}}, edge["sites"])
        self.assertEqual(["CMD", "wget", "-q", "-O-", "http://localhost/healthz"], edge["health"])

    def test_an_explicit_settings_file_keeps_its_path(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["file"] = "/app/appsettings.Local.json"
        api = release.validate(declared)["services"]["api"]
        self.assertEqual({"file": "/app/appsettings.Local.json", "required": ["Database:Connection"]}, api["settings"])

    def test_rule_breaks_name_the_rule(self):
        cases = (
            (lambda d: d.update(extra=1), "Unknown top-level key"),
            (lambda d: d.update(product="Pilot"), "product must be"),
            (lambda d: d.update(descriptor=3), "descriptor must be 1 or 2"),
            (lambda d: d.update(sites={"app": {}}), "Top-level sites need descriptor 2"),
            (lambda d: d["services"]["api"].pop("health"), "health must be"),
            (lambda d: d["services"]["api"].update(paths=["../outside/**"]), "relative to engineering"),
            (lambda d: d["services"]["api"].update(settings={"file": "appsettings.json"}), "absolute path"),
            (lambda d: d["services"]["api"].update(sites={"app": {}}), "already serves site app"),
            (lambda d: d["services"]["api"].update(needs=["mongo"]), "needs takes"),
            (lambda d: d["services"]["api"].update(memory="lots"), "memory must"),
            (lambda d: d["services"]["api"].update(image="ghcr.io/o/api:1"), "without a tag"),
            (lambda d: d["services"]["api"].update(unknown=True), "unknown key"),
            (lambda d: d.update(dotnetEnvironment="yes"), "dotnetEnvironment must be"),
            (lambda d: d.update(verify={"service": "worker", "command": ["true"]}), "verify service must"),
            (lambda d: d.update(verify={"service": "api", "command": "curl"}), "verify command must"),
            (lambda d: d.update(verify={"service": "api", "command": ["true"], "timeout": "2h"}), "verify timeout must be"),
            (lambda d: d.update(verify={"service": "api", "command": ["true"], "timeout": 60}), "verify timeout must look"),
            (lambda d: d.update(verify={"service": "api", "command": ["true"], "image": "x"}), "verify takes"),
            (lambda d: d["services"]["edge"].update(dependsOn="api"), "dependsOn must list"),
            (lambda d: d["services"]["edge"].update(dependsOn=["api", "api"]), "dependsOn must list"),
            (lambda d: d["services"]["edge"].update(dependsOn=["edge"]), "cannot depend on itself"),
            (lambda d: d["services"]["edge"].update(dependsOn=["worker"]), "dependsOn must name a service of this product$"),
            (lambda d: d["services"]["api"].update(dependsOn=["postgres"]), "platform services go under needs"),
            (lambda d: (d["services"]["edge"].update(dependsOn=["api"]), d["services"]["api"].update(dependsOn=["edge"])),
             "dependsOn forms a cycle: api, edge"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                descriptor = copy.deepcopy(DESCRIPTOR)
                mutate(descriptor)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(descriptor)

    def test_globs_match_inside_engineering_only(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.assertEqual(["api"], release.changed_services(descriptor, [
            "engineering/codebase/pilot.backend-services/Pilot.Api/Controllers/Home.cs"]))
        self.assertEqual(["edge"], release.changed_services(descriptor, ["engineering/deployment/edge/Caddyfile"]))
        self.assertEqual(["api", "edge"], release.changed_services(descriptor, [
            "engineering/deployment/backend.Dockerfile", "engineering/deployment/edge.Dockerfile"]))
        self.assertEqual([], release.changed_services(descriptor, ["README.md", "codebase/pilot.backend-services/x.cs"]))

    def test_a_shared_path_changes_every_service(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.assertEqual(["api", "edge"], release.changed_services(descriptor, [
            "engineering/codebase/pilot.backend-services/Directory.Packages.props"]))

    def test_the_environment_mapping_and_the_dev_check_are_opt_in(self):
        plain = release.validate(copy.deepcopy(DESCRIPTOR))
        self.assertEqual((False, None), (plain["dotnetEnvironment"], plain["verify"]))
        declared = copy.deepcopy(DESCRIPTOR)
        declared.update(dotnetEnvironment=True, verify={"service": "api", "command": ["dotnet", "Pilot.Verify.dll"]})
        descriptor = release.validate(declared)
        self.assertTrue(descriptor["dotnetEnvironment"])
        self.assertEqual({"service": "api", "command": ["dotnet", "Pilot.Verify.dll"], "timeoutSeconds": 300},
                         descriptor["verify"])
        declared["verify"]["timeout"] = "90s"
        self.assertEqual(90, release.validate(declared)["verify"]["timeoutSeconds"])

    def test_dependencies_are_opt_in_and_ordered(self):
        self.assertEqual([], release.validate(copy.deepcopy(DESCRIPTOR))["services"]["edge"]["dependsOn"])
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["worker"] = {"build": {"dockerfile": "deployment/backend.Dockerfile"},
                                          "paths": ["codebase/pilot.worker/**"], "health": "/health",
                                          "dependsOn": ["api", "edge"]}
        declared["services"]["edge"]["dependsOn"] = ["api"]
        descriptor = release.validate(declared)
        self.assertEqual(["api", "edge"], descriptor["services"]["worker"]["dependsOn"])
        self.assertEqual(["api", "edge", "worker"], release.dependency_order(descriptor["services"]))

    def test_typed_keys_join_the_required_list_and_take_checked_defaults(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Billing:Mode": {"type": "choice", "values": ["test", "live"], "default": "test"},
            "Stripe:ApiKey": {"secret": True, "required": True},
            "Jobs:Cleanup": {"type": "cron", "default": "0 3 * * *"},
            "Promo:EndsOn": {"type": "date", "format": "dd.MM.yyyy"},
            "Limits:MaxUsers": {"type": "number", "default": 50},
            "Features:Beta": {"type": "boolean", "default": False},
            "Support:Url": {"type": "url", "required": True},
            "Support:Email": {"type": "email"},
            "Notes:Banner": None,
        }
        settings = release.validate(declared)["services"]["api"]["settings"]
        self.assertEqual(["Database:Connection", "Stripe:ApiKey", "Support:Url"], settings["required"])
        keys = settings["keys"]
        self.assertEqual({"type": "choice", "required": False, "secret": False, "default": "test", "values": ["test", "live"]},
                         keys["Billing:Mode"])
        self.assertEqual(("50", "false"), (keys["Limits:MaxUsers"]["default"], keys["Features:Beta"]["default"]))
        self.assertEqual("dd.MM.yyyy", keys["Promo:EndsOn"]["format"])
        self.assertEqual({"type": "text", "required": False, "secret": False}, keys["Notes:Banner"])
        # A descriptor without typed keys validates as it always did.
        self.assertNotIn("keys", release.validate(copy.deepcopy(DESCRIPTOR))["services"]["api"]["settings"])

    def test_typed_key_rule_breaks_name_the_rule(self):
        cases = (
            ({"Bad Key": {}}, "must be a configuration path"),
            ({"A:B": {"type": "colour"}}, "type must be one of"),
            ({"A:B": {"kind": "text"}}, "takes type, required, secret"),
            ({"A:B": {"type": "choice"}}, "a choice lists 1-50 values"),
            ({"A:B": {"type": "choice", "values": ["x", "x"]}}, "must be distinct"),
            ({"A:B": {"values": ["x"]}}, "only a choice lists values"),
            ({"A:B": {"format": "yyyy"}}, "only a date takes a format"),
            ({"A:B": {"type": "date", "format": "%Y-%m"}}, "a date format uses"),
            ({"A:B": {"secret": True, "default": "x"}}, "a secret takes no default"),
            ({"A:B": {"required": True, "default": "x"}}, "a required key takes no default"),
            ({"A:B": {"required": "yes"}}, "required and secret must be true or false"),
            ({"A:B": {"type": "number", "default": "ten"}}, "default must be a number"),
            ({"A:B": {"type": "boolean", "default": "yes"}}, "default must be true or false"),
            ({"A:B": {"type": "choice", "values": ["a"], "default": "b"}}, "default must be one of the values"),
            ({"A:B": {"type": "url", "default": "example.com"}}, "default must be an absolute http"),
            ({"A:B": {"type": "cron", "default": "61 * * * *"}}, "default must be a cron expression"),
            ({"A:B": {"type": "date", "default": "2026-02-30"}}, "default must be a date as yyyy-MM-dd"),
            ({"A:B": {"type": "email", "default": "owner"}}, "default must be an email address"),
            ([], "settings keys must map keys"),
        )
        for keys, message in cases:
            with self.subTest(message=message):
                declared = copy.deepcopy(DESCRIPTOR)
                declared["services"]["api"]["settings"]["keys"] = keys
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(declared)

    def test_cron_takes_five_or_six_fields_names_steps_and_macros(self):
        for value in ("0 3 * * *", "*/15 9-17 * * MON-FRI", "0 0 1 JAN,JUL *", "0 30 2 L * ?", "0 0 * * 5#2", "@daily"):
            self.assertTrue(release.is_cron(value), value)
        for value in ("", "* * * *", "60 * * * *", "* 24 * * *", "0 0 0 * *", "5-1 * * * *", "*/0 * * * *", "@often",
                      " 0 3 * * *"):
            self.assertFalse(release.is_cron(value), value)

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_a_yaml_descriptor_parses(self):
        text = "descriptor: 1\nproduct: pilot\nservices:\n  api:\n    build: {dockerfile: deployment/Dockerfile}\n" \
               "    paths: [codebase/**]\n    health: /health\n"
        self.assertEqual(["api"], list(release.validate(release.parse(text))["services"]))


DESCRIPTOR_2 = {
    "descriptor": 2, "product": "pilot",
    "sites": {"app": None, "admin": {"exposure": "private", "environments": ["dev"]}},
    "services": {
        "api": {"build": {"dockerfile": "deployment/backend.Dockerfile"},
                "paths": ["codebase/pilot.backend-services/**"], "health": "/health",
                "sites": {"app": {"paths": ["/api", "/hubs"]}, "admin": {"paths": ["/api"], "port": 8081}}},
        "edge": {"build": {"dockerfile": "deployment/edge.Dockerfile"},
                 "paths": ["codebase/pilot.frontend-services/**"], "port": 80, "health": "/healthz",
                 "sites": {"app": None, "admin": {}}},
        "worker": {"build": {"dockerfile": "deployment/backend.Dockerfile"},
                   "paths": ["codebase/pilot.worker/**"], "health": "/health"},
    },
}


class SitesTests(unittest.TestCase):
    """Descriptor 2: the product declares its sites; each service lists the paths it serves per site."""

    def test_sites_and_paths_take_their_defaults(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR_2))
        self.assertEqual(2, descriptor["descriptor"])
        self.assertEqual({"app": {"exposure": "public", "primary": True, "environments": ["dev", "prod"]},
                          "admin": {"exposure": "private", "primary": False, "environments": ["dev"]}},
                         descriptor["sites"])
        services = descriptor["services"]
        self.assertEqual({"app": {"port": 8080, "paths": ["/api", "/hubs"]}, "admin": {"port": 8081, "paths": ["/api"]}},
                         services["api"]["sites"])
        self.assertEqual({"app": {"port": 80, "paths": ["/"]}, "admin": {"port": 80, "paths": ["/"]}},
                         services["edge"]["sites"])
        self.assertEqual({}, services["worker"]["sites"])

    def test_one_site_may_be_marked_primary_and_environments_keep_their_order(self):
        declared = copy.deepcopy(DESCRIPTOR_2)
        declared["sites"] = {"app": {"environments": ["prod", "dev"]}, "admin": {"primary": True}}
        sites = release.validate(declared)["sites"]
        self.assertEqual((False, True), (sites["app"]["primary"], sites["admin"]["primary"]))
        self.assertEqual(["dev", "prod"], sites["app"]["environments"])

    def test_rule_breaks_name_the_rule(self):
        api = lambda d: d["services"]["api"]
        cases = (
            # Exactly one service serves `/` per site: api claims it first, so edge's `/` is the second claim.
            (lambda d: api(d)["sites"]["app"]["paths"].append("/"),
             r"edge: another service \(api\) already serves site app at /$"),
            (lambda d: d["services"]["edge"]["sites"].update(app={"paths": ["/", "/api"]}),
             r"edge: another service \(api\) already serves site app at /api$"),
            (lambda d: api(d)["sites"]["app"].update(paths=["/api", "/api"]), "site app lists a path twice"),
            (lambda d: api(d)["sites"].update(web={}), "site web is not declared under the top-level sites"),
            (lambda d: d["sites"].update(web={}), "No service serves site web"),
            (lambda d: api(d)["sites"]["app"].update(path="/api"), "lists paths, not path, in descriptor 2"),
            (lambda d: api(d)["sites"]["app"].update(exposure="private"), "exposure belongs to the top-level site"),
            (lambda d: api(d)["sites"]["app"].update(host="app.example.com"), "site app takes paths and port"),
            (lambda d: api(d)["sites"]["app"].update(paths=[]), "paths must list at least one path"),
            (lambda d: api(d)["sites"]["app"].update(paths="/api"), "paths must list at least one path"),
            (lambda d: api(d)["sites"]["app"].update(port=0), "site port must be 1-65535"),
            (lambda d: d["sites"]["admin"].update(exposure="world"), "site admin: exposure must be public or private"),
            (lambda d: d["sites"]["admin"].update(environments=["test"]), "environments must list dev, prod or both"),
            (lambda d: d["sites"]["admin"].update(environments=[]), "environments must list dev, prod or both"),
            (lambda d: d["sites"]["admin"].update(host="admin.example.com"), "takes exposure, primary and environments"),
            (lambda d: d["sites"]["admin"].update(primary="yes"), "primary must be true or false"),
            (lambda d: d["sites"].update(app={"primary": True}, admin={"primary": True}),
             "Only one site may be primary: app, admin"),
            (lambda d: d.update(sites=["app"]), "sites must map site names"),
            (lambda d: d["sites"].update(Admin={}), "Site names must be lowercase slugs"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                descriptor = copy.deepcopy(DESCRIPTOR_2)
                mutate(descriptor)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(descriptor)

    def test_a_bad_path_is_refused(self):
        for path in ("api", "/api/", "/a//b", "/../x", "/a/./b", "/a b", "/api?x=1", "", "/" + "a" * 200, 7):
            with self.subTest(path=path):
                descriptor = copy.deepcopy(DESCRIPTOR_2)
                descriptor["services"]["api"]["sites"]["app"]["paths"] = [path]
                with self.assertRaisesRegex(release.Invalid, "must look like / or /api"):
                    release.validate(descriptor)
        for path in ("/", "/api", "/api/v2", "/.well-known", "/a~b_c.d-e"):
            with self.subTest(path=path):
                descriptor = copy.deepcopy(DESCRIPTOR_2)
                descriptor["services"]["api"]["sites"]["app"]["paths"] = [path]
                descriptor["services"]["edge"]["sites"]["app"] = {"paths": ["/web"]} if path == "/" else None
                self.assertEqual([path], release.validate(descriptor)["services"]["api"]["sites"]["app"]["paths"])

    def test_a_site_without_a_root_path_warns(self):
        self.assertEqual([], release.site_warnings(release.validate(copy.deepcopy(DESCRIPTOR_2))))
        declared = copy.deepcopy(DESCRIPTOR_2)
        declared["services"]["edge"]["sites"]["admin"] = {"paths": ["/ops"]}
        descriptor = release.validate(declared)
        self.assertEqual(["site admin has no / path: its host answers 404 outside /api, /ops"],
                         release.site_warnings(descriptor))
        with contextlib.redirect_stderr(io.StringIO()) as printed:
            release.warned(descriptor)
        self.assertEqual("::warning title=deploy.yml::site admin has no / path: its host answers 404 outside /api, /ops\n",
                         printed.getvalue())
        self.assertEqual([], release.site_warnings(release.validate(copy.deepcopy(DESCRIPTOR))))

    def test_a_service_without_sites_needs_no_top_level_sites(self):
        declared = copy.deepcopy(DESCRIPTOR_2)
        del declared["sites"]
        for service in declared["services"].values():
            service.pop("sites", None)
        descriptor = release.validate(declared)
        self.assertEqual({}, descriptor["sites"])

    def test_a_bundle_carries_the_sites_and_each_services_paths(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR_2))
        planned = {"release": "v1.0.0", "kind": "release", "sourceCommit": "c" * 40,
                   "services": {name: {"version": "1.0.0", "changedIn": "v1.0.0"} for name in descriptor["services"]}}
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        compose_bytes, manifest = release.render(descriptor, planned, images)
        self.assertEqual(2, manifest["schemaVersion"])
        self.assertEqual(descriptor["sites"], manifest["sites"])
        self.assertEqual({"api": {"app": {"port": 8080, "paths": ["/api", "/hubs"]},
                                  "admin": {"port": 8081, "paths": ["/api"]}},
                          "edge": {"app": {"port": 80, "paths": ["/"]}, "admin": {"port": 80, "paths": ["/"]}}},
                         manifest["routes"])
        self.assertEqual(["sites", "routes", "needs"], list(manifest)[list(manifest).index("sites"):][:3])
        # Sites never reach Compose: the ingress routes them, so no service publishes a port.
        self.assertNotIn("ports", json.dumps(json.loads(compose_bytes)))

    def test_a_descriptor_1_bundle_keeps_schema_1(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        planned = {"release": "v1.0.0", "kind": "release", "sourceCommit": "c" * 40,
                   "services": {name: {"version": "1.0.0", "changedIn": "v1.0.0"} for name in descriptor["services"]}}
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        self.assertEqual(1, manifest["schemaVersion"])
        self.assertNotIn("routes", manifest)
        self.assertEqual({"api": {"app": {"port": 8080, "path": "/api", "exposure": "public"}},
                          "edge": {"app": {"port": 80, "path": "/", "exposure": "public"},
                                   "landing": {"port": 80, "path": "/", "exposure": "public"}}}, manifest["sites"])

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_the_documented_example_parses(self):
        text = """
descriptor: 2
product: ocharo-studio
sites:
  studio: { primary: true }
services:
  studio:
    build: { dockerfile: deployment/Dockerfile }
    paths: [codebase/**]
    health: /health
    sites:
      studio: { paths: [/] }
"""
        descriptor = release.validate(release.parse(text))
        self.assertEqual({"studio": {"exposure": "public", "primary": True, "environments": ["dev", "prod"]}},
                         descriptor["sites"])
        self.assertEqual({"studio": {"port": 8080, "paths": ["/"]}}, descriptor["services"]["studio"]["sites"])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "one")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "one")
        self.first = self.commit("first")
        self.descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.base = {"product": "pilot", "release": "v1.0.0", "sourceCommit": self.first,
                     "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST},
                     "versions": {"api": {"version": "1.0.0", "changedIn": "v1.0.0"},
                                  "edge": {"version": "0.9.0", "changedIn": "v0.9.0"}}}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_a_candidate_rebuilds_changed_services_and_keeps_the_rest(self):
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("api change")
        planned = release.plan(self.descriptor, self.repo, second, self.base)
        short = second[:7]
        self.assertEqual(("sha-" + short, "candidate", "v1.0.0"), (planned["release"], planned["kind"], planned["base"]))
        self.assertEqual({"build": True, "version": "1.0.0+" + short, "changedIn": "sha-" + short},
                         planned["services"]["api"])
        self.assertEqual({"build": False, "version": "0.9.0", "changedIn": "v0.9.0",
                          "image": "ghcr.io/o/pilot/edge" + DIGEST}, planned["services"]["edge"])

    def test_a_release_versions_changed_services_with_its_tag(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        planned = release.plan(self.descriptor, self.repo, second, self.base, "v1.1.0")
        self.assertEqual(("v1.1.0", "release"), (planned["release"], planned["kind"]))
        self.assertEqual(("1.1.0", "v1.1.0"), (planned["services"]["edge"]["version"], planned["services"]["edge"]["changedIn"]))
        self.assertEqual(("1.0.0", False), (planned["services"]["api"]["version"], planned["services"]["api"]["build"]))

    def test_without_a_base_every_service_builds(self):
        planned = release.plan(self.descriptor, self.repo, self.first)
        self.assertTrue(all(entry["build"] for entry in planned["services"].values()))
        self.assertEqual("0.0.0+" + self.first[:7], planned["services"]["api"]["version"])

    def test_a_service_the_base_never_shipped_builds(self):
        base = copy.deepcopy(self.base)
        del base["images"]["edge"], base["versions"]["edge"]
        planned = release.plan(self.descriptor, self.repo, self.first, base)
        self.assertEqual({"edge"}, {name for name, entry in planned["services"].items() if entry["build"]})

    def test_a_base_of_another_product_is_refused(self):
        with self.assertRaisesRegex(release.Invalid, "another product"):
            release.plan(self.descriptor, self.repo, self.first, {**self.base, "product": "other"})

    def assert_rebuilt_without_the_base(self, planned, label, version, release_name="v1.0.0"):
        self.assertEqual({"api", "edge"}, set(planned["services"]))
        for name, entry in planned["services"].items():
            with self.subTest(service=name):
                self.assertEqual({"build": True, "version": version, "changedIn": label}, entry)
        self.assertIsNone(planned["base"])
        self.assertFalse(planned["descriptorChanged"])
        self.assertEqual("Base " + release_name + "'s source commit (" + self.lost[:7]
                         + ") is not in this history; every service was rebuilt.", planned["warning"])
        self.assertFalse(release.nothing_to_publish(planned))

    lost = "1bcb4722ec27bbe4dff2f955f2ea50572f0af591"

    def test_a_base_commit_missing_from_the_history_rebuilds_every_service_with_a_warning(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        lost = {**self.base, "sourceCommit": self.lost}
        released = release.plan(self.descriptor, self.repo, second, lost, "v1.1.0")
        self.assert_rebuilt_without_the_base(released, "v1.1.0", "1.1.0")
        self.assertEqual("release", released["kind"])
        candidate = release.plan(self.descriptor, self.repo, second, lost)
        self.assert_rebuilt_without_the_base(candidate, "sha-" + second[:7], "1.0.0+" + second[:7])
        built = release.plan(self.descriptor, self.repo, second, lost, "v1.1.0", "1.1.0")
        self.assertEqual(released, built)

    def test_a_base_without_a_usable_source_commit_counts_as_lost(self):
        for source in (None, "", "main", "--output=x", "1bcb472"):
            with self.subTest(source=source):
                base = {key: value for key, value in self.base.items() if key != "sourceCommit"}
                if source is not None:
                    base["sourceCommit"] = source
                planned = release.plan(self.descriptor, self.repo, self.first, base, "v1.1.0")
                self.assertTrue(all(entry["build"] for entry in planned["services"].values()))
                self.assertIn("every service was rebuilt", planned["warning"])

    def test_a_base_commit_that_shares_no_history_counts_as_lost(self):
        tree = git(self.repo, "rev-parse", self.first + "^{tree}")
        twin = git(self.repo, "commit-tree", tree, "-m", "the same tree under another history")
        self.lost = twin
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        planned = release.plan(self.descriptor, self.repo, second, {**self.base, "sourceCommit": twin}, "v1.1.0")
        self.assert_rebuilt_without_the_base(planned, "v1.1.0", "1.1.0")

    def test_a_failing_diff_counts_as_lost(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        with unittest.mock.patch.object(release, "base_commit_known", return_value=True):
            planned = release.plan(self.descriptor, self.repo, second, {**self.base, "sourceCommit": self.lost}, "v1.1.0")
        self.assert_rebuilt_without_the_base(planned, "v1.1.0", "1.1.0")

    def test_a_base_in_the_history_plans_as_before_without_a_warning(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        planned = release.plan(self.descriptor, self.repo, second, self.base, "v1.1.0")
        self.assertNotIn("warning", planned)
        self.assertEqual({"product": "pilot", "release": "v1.1.0", "kind": "release", "sourceCommit": second,
                          "base": "v1.0.0", "descriptorChanged": False,
                          "services": {"api": {"build": False, "version": "1.0.0", "changedIn": "v1.0.0",
                                               "image": "ghcr.io/o/pilot/api" + DIGEST},
                                       "edge": {"build": True, "version": "1.1.0", "changedIn": "v1.1.0"}}}, planned)

    def test_a_base_on_another_branch_still_diffs_when_it_shares_history(self):
        # A candidate on a branch behind main: the newest release is not its ancestor, but the trees still compare.
        git(self.repo, "checkout", "-q", "-b", "side")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "side")
        side = self.commit("side change")
        git(self.repo, "checkout", "-q", "main")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "main")
        head = self.commit("api change")
        planned = release.plan(self.descriptor, self.repo, head, {**self.base, "sourceCommit": side})
        self.assertNotIn("warning", planned)
        self.assertEqual({"api": True, "edge": True}, {name: entry["build"] for name, entry in planned["services"].items()})
        unchanged = release.plan(self.descriptor, self.repo, self.first, {**self.base, "sourceCommit": self.first})
        self.assertEqual({"api": False, "edge": False}, {name: entry["build"] for name, entry in unchanged["services"].items()})

    def test_a_shallow_clone_still_finds_a_reachable_base_commit(self):
        git(self.repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        self.commit("edge change")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "three")
        head = self.commit("edge change again")
        clone = Path(self.temporary.name + "-shallow")
        self.addCleanup(shutil.rmtree, clone, True)
        git(self.repo, "clone", "-q", "--depth", "1", self.repo.as_uri(), str(clone))
        self.assertEqual("true", git(clone, "rev-parse", "--is-shallow-repository"))
        self.assertNotEqual(0, subprocess.run(["git", "-C", str(clone), "cat-file", "-e", self.first + "^{commit}"],
                                              capture_output=True).returncode)
        planned = release.plan(self.descriptor, clone, head, self.base, "v1.1.0")
        self.assertNotIn("warning", planned)
        self.assertEqual({"api": False, "edge": True}, {name: entry["build"] for name, entry in planned["services"].items()})
        self.assertEqual("v1.0.0", planned["base"])
        self.assertEqual(self.first, git(clone, "rev-parse", self.first))

    def test_a_shallow_clone_without_the_base_commit_anywhere_counts_as_lost(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        head = self.commit("edge change")
        clone = Path(self.temporary.name + "-shallow")
        self.addCleanup(shutil.rmtree, clone, True)
        git(self.repo, "clone", "-q", "--depth", "1", self.repo.as_uri(), str(clone))
        planned = release.plan(self.descriptor, clone, head, {**self.base, "sourceCommit": self.lost}, "v1.1.0")
        self.assert_rebuilt_without_the_base(planned, "v1.1.0", "1.1.0")

    def test_the_rendered_bundle_pins_every_image_and_its_compose_hash(self):
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("api change")
        planned = release.plan(self.descriptor, self.repo, second, self.base)
        images = {"api": "ghcr.io/o/pilot/api@sha256:" + "b" * 64, "edge": planned["services"]["edge"]["image"]}
        compose_bytes, manifest = release.render(self.descriptor, planned, images, branch="main")
        output = self.repo / "bundle"
        release.write_bundle(output, compose_bytes, manifest, self.repo / "bundle.tar.gz")
        written = json.loads((output / "release.json").read_text())
        self.assertEqual(hashlib.sha256((output / "compose.json").read_bytes()).hexdigest(), written["composeSha256"])
        self.assertEqual(images, written["images"])
        self.assertEqual({"api": ["Database:Connection"]}, written["requiredConfiguration"])
        self.assertEqual({"api": {"version": "1.0.0+" + second[:7], "changedIn": "sha-" + second[:7]},
                          "edge": {"version": "0.9.0", "changedIn": "v0.9.0"}}, written["versions"])
        self.assertEqual({"api": 8080, "edge": 80}, written["ports"])
        compose = json.loads(compose_bytes)
        api, edge = compose["services"]["api"], compose["services"]["edge"]
        self.assertEqual(["pilot-${DEPLOY_ENVIRONMENT}-api"], api["networks"]["platform"]["aliases"])
        self.assertEqual({"type": "bind", "source": "${API_SETTINGS:?Set API_SETTINGS}",
                          "target": "/app/appsettings.Target.json", "read_only": True}, api["volumes"][0])
        self.assertEqual({"type": "volume", "source": "keys", "target": "/data/keys"}, api["volumes"][1])
        self.assertEqual((["NET_BIND_SERVICE"], ["ALL"]), (edge["cap_add"], edge["cap_drop"]))
        self.assertEqual({"keys": {}}, compose["volumes"])
        self.assertEqual("main", manifest["branch"])
        # Neither opt-in is declared, so the bundle carries neither key, and no service depends on another.
        self.assertNotIn("dotnetEnvironment", written)
        self.assertNotIn("verify", written)
        self.assertEqual(set(), {name for name, service in compose["services"].items() if "depends_on" in service})

    def test_declared_dependencies_render_as_health_gated_compose_dependencies(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["edge"]["dependsOn"] = ["api"]
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        compose_bytes, manifest = release.render(descriptor, planned, images)
        compose = json.loads(compose_bytes)
        # Health-gated, and never `restart: true`: a dependency's update does not restart its dependents.
        self.assertEqual({"api": {"condition": "service_healthy"}}, compose["services"]["edge"]["depends_on"])
        self.assertNotIn("depends_on", compose["services"]["api"])
        # Compose carries the order; the manifest needs no second copy of it.
        self.assertNotIn("dependsOn", json.dumps(manifest))

    def test_a_bundle_carries_typed_keys_for_the_runner_and_untyped_bundles_stay_the_same(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Billing:Mode": {"type": "choice", "values": ["test", "live"], "default": "test"},
            "Stripe:ApiKey": {"secret": True, "required": True},
            "Promo:EndsOn": {"type": "date"},
        }
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        self.assertEqual({"api": ["Database:Connection", "Stripe:ApiKey"]}, manifest["requiredConfiguration"])
        self.assertEqual({"api": {"optional": {"Billing:Mode": "test"}, "secrets": ["Stripe:ApiKey"],
                                  "allowed": {"Billing:Mode": ["test", "live"]},
                                  "types": {"Billing:Mode": {"type": "choice"}, "Stripe:ApiKey": {"type": "text"},
                                            "Promo:EndsOn": {"type": "date", "format": "yyyy-MM-dd"}}}},
                         manifest["settings"])
        plain = release.validate(copy.deepcopy(DESCRIPTOR))
        _, untyped = release.render(plain, release.plan(plain, self.repo, self.first, None), images)
        self.assertNotIn("settings", untyped)

    def test_a_bundle_carries_the_declared_environment_mapping_and_dev_check(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared.update(dotnetEnvironment=True,
                        verify={"service": "api", "command": ["sh", "-c", "curl --fail $VERIFY_SERVICE_API/health"],
                                "timeout": "2m"})
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + "@sha256:" + "c" * 64 for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        self.assertTrue(manifest["dotnetEnvironment"])
        self.assertEqual({"service": "api", "command": ["sh", "-c", "curl --fail $VERIFY_SERVICE_API/health"],
                          "timeoutSeconds": 120}, manifest["verify"])


class VersionTests(unittest.TestCase):
    """X.Y from the newest version-track folder, Z from the release tags, the rest from the branch."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "one")
        self.write("engineering/planning/version-track/version-track.md", "lead")
        self.write("engineering/planning/version-track/v0.2/v0.2.md", "old")
        self.write("engineering/planning/version-track/v0.10/v0.10.md", "active")
        self.first = self.commit("first")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_the_line_is_the_newest_version_folder_compared_as_numbers(self):
        self.assertEqual((0, 10), release.version_line(self.repo, self.first))

    def test_a_repository_without_a_version_track_is_line_zero(self):
        git(self.repo, "rm", "-q", "-r", "engineering/planning")
        second = self.commit("no track")
        self.assertEqual((0, 0), release.version_line(self.repo, second))

    def test_the_patch_follows_the_highest_release_tag_of_the_line(self):
        self.assertEqual(0, release.next_patch(self.repo, (0, 10)))
        for tag in ("v0.10.0", "v0.10.3", "v0.10.4-rc.1", "v0.9.7"):
            git(self.repo, "tag", tag)
        self.assertEqual(4, release.next_patch(self.repo, (0, 10)))
        self.assertEqual(8, release.next_patch(self.repo, (0, 9)))

    def test_main_releases_the_next_patch(self):
        git(self.repo, "tag", "v0.10.0")
        identity = release.build_identity(self.repo, self.first, "main")
        self.assertEqual({"channel": "release", "kind": "release", "version": "0.10.1", "release": "v0.10.1",
                          "tag": "0.10.1", "moving": "latest"}, identity)

    def test_a_dev_branch_builds_an_ordinary_candidate(self):
        git(self.repo, "checkout", "-q", "-b", "dev")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("dev work")
        dev = release.build_identity(self.repo, second, "dev")
        self.assertEqual(("branch", "candidate", "0.10.0-dev.1", "sha-" + second[:7], "sha-" + second[:7], None),
                         (dev["channel"], dev["kind"], dev["version"], dev["release"], dev["tag"], dev["moving"]))

    def test_a_test_branch_builds_an_ordinary_candidate(self):
        git(self.repo, "checkout", "-q", "-b", "test")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("test work")
        test = release.build_identity(self.repo, second, "test")
        self.assertEqual(("branch", "candidate", "0.10.0-test.1", "sha-" + second[:7], "sha-" + second[:7], None),
                         (test["channel"], test["kind"], test["version"], test["release"], test["tag"], test["moving"]))

    def test_any_other_branch_builds_a_candidate_named_after_it(self):
        git(self.repo, "checkout", "-q", "-b", "feat/Login")
        for step in ("a", "b"):
            self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", step)
            head = self.commit(step)
        identity = release.build_identity(self.repo, head, "feat/Login")
        self.assertEqual(("branch", "0.10.0-feat-login.2", "sha-" + head[:7], None),
                         (identity["channel"], identity["version"], identity["tag"], identity["moving"]))
        self.assertEqual("b2024", release.tag_safe("2024"))

    def test_a_dispatched_commit_carries_its_hash(self):
        identity = release.build_identity(self.repo, self.first)
        self.assertEqual(("candidate", "0.10.0-g" + self.first[:7]), (identity["kind"], identity["version"]))

    def test_a_docs_only_push_publishes_nothing_but_a_descriptor_change_does(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        base = {"product": "pilot", "release": "v0.10.0", "sourceCommit": self.first,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        self.write("README.md", "docs")
        docs = self.commit("docs")
        self.assertTrue(release.nothing_to_publish(release.plan(descriptor, self.repo, docs, base, "v0.10.1")))
        self.write("engineering/deployment/deploy.yml", "descriptor: 1")
        changed = self.commit("descriptor")
        planned = release.plan(descriptor, self.repo, changed, base, "v0.10.1", "0.10.1")
        self.assertTrue(planned["descriptorChanged"])
        self.assertFalse(release.nothing_to_publish(planned))

    def test_the_build_version_names_each_rebuilt_service(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        planned = release.plan(descriptor, self.repo, self.first, None, None, "0.10.0-dev.3")
        self.assertEqual({"0.10.0-dev.3"}, {entry["version"] for entry in planned["services"].values()})
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        identity = {"version": "0.10.0-dev.3", "channel": "branch"}
        _, manifest = release.render(descriptor, planned, images, "feat/login", identity=identity)
        self.assertEqual(("0.10.0-dev.3", "branch", "feat/login"),
                         (manifest["version"], manifest["channel"], manifest["branch"]))


class NotesTests(unittest.TestCase):
    """The commits a main release's notes list above GitHub's generated notes."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, message):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_notes_list_the_commits_since_the_previous_release_newest_first(self):
        self.commit("feat: first page")
        git(self.repo, "tag", "v0.1.0")
        self.commit("fix: kept the login\n\nA longer body that stays out of the notes.")
        head = self.commit("feat: added billing")
        short = git(self.repo, "rev-parse", "--short", "HEAD")
        notes = release.release_notes(self.repo, head, "v0.1.0").splitlines()
        self.assertEqual("Built by wow-two-platform.pipelines from " + head + ".", notes[0])
        self.assertEqual("## Commits since v0.1.0", notes[2])
        self.assertEqual(["- feat: added billing (" + short + ")", "- fix: kept the login"],
                         [notes[4], notes[5].rsplit(" (", 1)[0]])
        self.assertEqual(6, len(notes))

    def test_a_first_release_lists_its_history_and_a_long_one_is_cut_at_the_limit(self):
        for number in range(5):
            head = self.commit("feat: step " + str(number))
        with unittest.mock.patch.object(release, "NOTES_LIMIT", 3):
            notes = release.release_notes(self.repo, head).splitlines()
        self.assertEqual("## Commits", notes[2])
        self.assertEqual(["- feat: step 4", "- feat: step 3", "- feat: step 2"],
                         [line.rsplit(" (", 1)[0] for line in notes[4:7]])
        self.assertEqual("- …and 2 more", notes[7])

    def test_a_lost_base_warning_leads_the_notes_and_no_warning_leaves_them_unchanged(self):
        self.commit("feat: first page")
        git(self.repo, "tag", "v0.1.0")
        head = self.commit("feat: added billing")
        plain = release.release_notes(self.repo, head, "v0.1.0")
        self.assertEqual(plain, release.release_notes(self.repo, head, "v0.1.0", None))
        warning = "Base v0.1.0's source commit (1bcb472) is not in this history; every service was rebuilt."
        warned = release.release_notes(self.repo, head, "v0.1.0", warning).splitlines()
        self.assertEqual(["Built by wow-two-platform.pipelines from " + head + ".", "", "**Warning:** " + warning, "",
                          "## Commits since v0.1.0"], warned[:5])
        self.assertEqual(plain.splitlines()[3:], warned[5:])

    def test_no_commit_since_the_previous_release_says_so_and_an_unknown_tag_is_refused(self):
        head = self.commit("feat: only")
        git(self.repo, "tag", "v0.1.0")
        self.assertIn("- No commits since v0.1.0.", release.release_notes(self.repo, head, "v0.1.0"))
        with self.assertRaisesRegex(release.Invalid, "--previous names no commit"):
            release.release_notes(self.repo, head, "v9.9.9")

    @unittest.skipUnless(HAS_YAML, "PyYAML reads the workflow")
    def test_the_publish_workflow_writes_the_notes_and_github_generated_notes_on_a_main_release(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").read_text())
        steps = workflow["jobs"]["bundle"]["steps"]
        fetch = next(step for step in steps if step.get("name", "").startswith("Fetch the previous release"))
        self.assertIn('echo "PREVIOUS_RELEASE=$previous" >> "$GITHUB_ENV"', fetch["run"])
        publish = next(step for step in steps if step.get("name", "").startswith("Publish the release"))
        self.assertIn("steps.build.outputs.kind == 'release'", publish["if"])
        for part in ("release.py \"${notes[@]}\"", "--previous \"$PREVIOUS_RELEASE\"", "--notes-file",
                     "--generate-notes", "--notes-start-tag \"$PREVIOUS_RELEASE\""):
            self.assertIn(part, publish["run"])
        self.assertNotIn("--notes \"", publish["run"])
        # The lost-base warning travels build output -> environment -> `notes --warning`, never into the shell text.
        self.assertEqual("${{ steps.build.outputs.warning }}", publish["env"]["BASE_WARNING"])
        self.assertIn('--warning "$BASE_WARNING"', publish["run"])
        self.assertNotIn("outputs.warning", publish["run"])
        self.assertEqual(0, next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
                         ["with"]["fetch-depth"])


class CiTests(unittest.TestCase):
    """The workflow's `ci` step with Docker stubbed: what it publishes and what it reports."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        for path, content in (("engineering/codebase/pilot.backend-services/Program.cs", "one"),
                              ("engineering/planning/version-track/v0.4/v0.4.md", "active")):
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.built, self.moved, self.caches = [], [], []
        self.stubs = [unittest.mock.patch.object(release, "build_image", self.fake_build),
                      unittest.mock.patch.object(release, "move_tag", lambda image, tag: self.moved.append((image, tag)))]
        for stub in self.stubs:
            stub.start()
        self.outputs = Path(self.temporary.name) / "outputs"
        self.environment = unittest.mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(self.outputs)})
        self.environment.start()
        self.cwd = os.getcwd()
        os.chdir(self.temporary.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.environment.stop()
        for stub in self.stubs:
            stub.stop()
        self.temporary.cleanup()

    def fake_build(self, descriptor, name, checkout, repository, tag, app_version=None, vcs_ref=None, cache=None):
        self.built.append((name, tag, app_version, vcs_ref))
        self.caches.append(cache)
        return repository + "@sha256:" + hashlib.sha256((name + tag).encode()).hexdigest()

    def run_ci(self, branch, base=None, cache=None):
        arguments = argparse.Namespace(repo=str(self.repo), branch=branch, main="main", registry="ghcr.io/o/pilot",
                                       output="bundle", rollback_compatible=False, cache=cache)
        with contextlib.redirect_stdout(io.StringIO()) as printed, contextlib.redirect_stderr(io.StringIO()) as logged:
            release.ci(arguments, self.commit, self.descriptor, base)
        self.logged = logged.getvalue()
        return json.loads(printed.getvalue()), dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())

    def test_main_releases_tags_every_image_latest_and_writes_the_archive(self):
        result, outputs = self.run_ci("main")
        self.assertEqual({"status": "built", "kind": "release", "channel": "release", "version": "0.4.0",
                          "release": "v0.4.0", "archive": "pilot-release.tar.gz"}, outputs)
        self.assertEqual({("api", "0.4.0", "0.4.0", self.commit), ("edge", "0.4.0", "0.4.0", self.commit)},
                         set(self.built))
        self.assertEqual({"latest"}, {tag for _, tag in self.moved})
        self.assertTrue(Path(self.temporary.name, "pilot-release.tar.gz").is_file())
        self.assertEqual("0.4.0", json.loads(Path(self.temporary.name, "bundle", "release.json").read_text())["version"])

    def test_a_branch_build_tags_its_commit_and_writes_no_archive(self):
        result, outputs = self.run_ci("dev")
        self.assertEqual(("built", "candidate", "branch", ""), (outputs["status"], outputs["kind"], outputs["channel"],
                                                                outputs.get("archive", "")))
        self.assertEqual({"sha-" + self.commit[:7]}, {tag for _, tag, _, _ in self.built})
        self.assertEqual(set(), {tag for _, tag in self.moved})

    def test_a_lost_base_rebuilds_every_service_and_warns_in_the_log_and_the_outputs(self):
        base = {"product": "pilot", "release": "v0.3.0", "sourceCommit": "1bcb4722ec27bbe4dff2f955f2ea50572f0af591",
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST},
                "versions": {"api": {"version": "0.3.0", "changedIn": "v0.3.0"},
                             "edge": {"version": "0.2.0", "changedIn": "v0.2.0"}}}
        result, outputs = self.run_ci("main", base)
        warning = "Base v0.3.0's source commit (1bcb472) is not in this history; every service was rebuilt."
        self.assertEqual((warning, "built", "v0.4.0"), (outputs["warning"], outputs["status"], outputs["release"]))
        self.assertEqual(warning, result["warning"])
        self.assertIn("::warning title=release base::" + warning + "\n", self.logged)
        self.assertEqual({"api", "edge"}, {name for name, _, _, _ in self.built})
        manifest = json.loads(Path(self.temporary.name, "bundle", "release.json").read_text())
        self.assertEqual({"api": {"version": "0.4.0", "changedIn": "v0.4.0"},
                          "edge": {"version": "0.4.0", "changedIn": "v0.4.0"}}, manifest["versions"])
        self.assertTrue(all(DIGEST not in image for image in manifest["images"].values()))

    def test_a_lost_base_still_builds_a_branch_push_a_known_base_would_skip(self):
        base = {"product": "pilot", "release": "v0.3.0", "sourceCommit": "1" * 40,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        result, outputs = self.run_ci("dev", base)
        self.assertEqual("built", outputs["status"])
        self.assertEqual({"api", "edge"}, {name for name, _, _, _ in self.built})

    def test_a_known_base_adds_no_warning_to_the_log_the_outputs_or_the_result(self):
        base = {"product": "pilot", "release": "v0.4.0", "sourceCommit": self.commit,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        result, outputs = self.run_ci("main", base)
        self.assertEqual({"status", "kind", "channel", "version", "release", "archive"}, set(outputs))
        self.assertNotIn("warning", result)
        self.assertEqual("", self.logged)

    def test_the_layer_cache_reaches_every_build_and_no_cache_is_the_default(self):
        self.run_ci("main")
        self.assertEqual([None, None], self.caches)
        self.caches.clear()
        self.run_ci("main", cache="gha")
        self.assertEqual(["gha", "gha"], self.caches)

    def test_a_push_that_changes_nothing_is_skipped(self):
        base = {"product": "pilot", "release": "v0.4.0", "sourceCommit": self.commit,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        result, outputs = self.run_ci("main", base)
        self.assertEqual("skipped", outputs["status"])
        self.assertEqual([], self.built)


class BuildImageTests(unittest.TestCase):
    """The docker buildx call for one service, with Docker stubbed."""

    def test_a_build_receives_its_version_and_source_commit(self):
        calls = []

        def fake_docker(*arguments):
            calls.append(arguments)
            metadata = arguments[arguments.index("--metadata-file") + 1]
            Path(metadata).write_text(json.dumps({"containerimage.digest": DIGEST[1:]}))
            return ""

        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        commit = "c" * 40
        with unittest.mock.patch.object(release, "docker", fake_docker):
            image = release.build_image(descriptor, "api", "/checkout", "ghcr.io/o/pilot/api", "0.4.0", "0.4.0", commit)
        self.assertEqual("ghcr.io/o/pilot/api" + DIGEST, image)
        arguments = list(calls[0])
        build_args = [arguments[index + 1] for index, item in enumerate(arguments) if item == "--build-arg"]
        self.assertEqual(["SERVICE=Api", "APP_VERSION=0.4.0", "VCS_REF=" + commit], build_args)
        self.assertNotIn("--cache-from", arguments)

    def test_the_gha_cache_reads_and_writes_one_scope_per_service_and_never_fails_the_build(self):
        calls = []

        def fake_docker(*arguments):
            calls.append(arguments)
            Path(arguments[arguments.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": DIGEST[1:]}))
            return ""

        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        with unittest.mock.patch.object(release, "docker", fake_docker):
            release.build_image(descriptor, "api", "/checkout", "ghcr.io/o/pilot/api", "0.4.0", "0.4.0", "c" * 40,
                                "gha")
        arguments = list(calls[0])
        self.assertEqual("type=gha,scope=api", arguments[arguments.index("--cache-from") + 1])
        self.assertEqual("type=gha,mode=min,scope=api,ignore-error=true", arguments[arguments.index("--cache-to") + 1])
        self.assertEqual("/checkout/engineering/codebase", arguments[-1])


if __name__ == "__main__":
    unittest.main()
