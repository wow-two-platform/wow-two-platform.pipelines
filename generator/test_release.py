"""The release generator on throwaway Git repositories; nothing is built or pushed."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
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
        self.assertEqual({"file": "/app/appsettings.Local.json", "required": ["Database:Connection"]}, api["settings"])
        self.assertEqual({"app": {"port": 80, "path": "/", "exposure": "public"},
                          "landing": {"port": 80, "path": "/", "exposure": "public"}}, edge["sites"])
        self.assertEqual(["CMD", "wget", "-q", "-O-", "http://localhost/healthz"], edge["health"])

    def test_rule_breaks_name_the_rule(self):
        cases = (
            (lambda d: d.update(extra=1), "Unknown top-level key"),
            (lambda d: d.update(product="Pilot"), "product must be"),
            (lambda d: d.update(descriptor=2), "descriptor must be 1"),
            (lambda d: d["services"]["api"].pop("health"), "health must be"),
            (lambda d: d["services"]["api"].update(paths=["../outside/**"]), "relative to engineering"),
            (lambda d: d["services"]["api"].update(settings={"file": "appsettings.json"}), "absolute path"),
            (lambda d: d["services"]["api"].update(sites={"app": {}}), "already serves site app"),
            (lambda d: d["services"]["api"].update(needs=["mongo"]), "needs takes"),
            (lambda d: d["services"]["api"].update(memory="lots"), "memory must"),
            (lambda d: d["services"]["api"].update(image="ghcr.io/o/api:1"), "without a tag"),
            (lambda d: d["services"]["api"].update(unknown=True), "unknown key"),
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

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_a_yaml_descriptor_parses(self):
        text = "descriptor: 1\nproduct: pilot\nservices:\n  api:\n    build: {dockerfile: deployment/Dockerfile}\n" \
               "    paths: [codebase/**]\n    health: /health\n"
        self.assertEqual(["api"], list(release.validate(release.parse(text))["services"]))


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
        self.assertEqual("${API_SETTINGS:?Set API_SETTINGS}", api["volumes"][0]["source"])
        self.assertEqual({"type": "volume", "source": "keys", "target": "/data/keys"}, api["volumes"][1])
        self.assertEqual((["NET_BIND_SERVICE"], ["ALL"]), (edge["cap_add"], edge["cap_drop"]))
        self.assertEqual({"keys": {}}, compose["volumes"])
        self.assertEqual("main", manifest["branch"])


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
        self.built, self.moved = [], []
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

    def fake_build(self, descriptor, name, checkout, repository, tag, app_version=None, vcs_ref=None):
        self.built.append((name, tag, app_version, vcs_ref))
        return repository + "@sha256:" + hashlib.sha256((name + tag).encode()).hexdigest()

    def run_ci(self, branch, base=None):
        arguments = argparse.Namespace(repo=str(self.repo), branch=branch, main="main", registry="ghcr.io/o/pilot",
                                       output="bundle", rollback_compatible=False)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            release.ci(arguments, self.commit, self.descriptor, base)
        return json.loads(printed.getvalue()), dict(line.split("=", 1) for line in self.outputs.read_text().split())

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


if __name__ == "__main__":
    unittest.main()
