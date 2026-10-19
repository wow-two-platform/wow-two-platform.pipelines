"""The CI plan against a fake GitHub API, and the shared workflows' shape; nothing reaches the network."""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
import unittest.mock
from urllib.error import URLError

import ci_plan

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
HEAD, BASE, OLDER, OTHER = "a" * 40, "b" * 40, "c" * 40, "d" * 40
BACKENDS = [{"name": "Backend", "run": "dotnet test", "dotnet": "global.json",
             "paths": ["engineering/codebase/app.backend-services/**"]},
            {"name": "Runner", "run": "python -m unittest", "python": "3.12", "pip": "pyyaml"}]
FRONTENDS = [{"name": "Frontend", "dir": "engineering/codebase/app.frontend-services", "scripts": ["test", "build"],
              "paths": ["engineering/codebase/app.frontend-services/**"]}]
BROWSER = {"name": "Browser", "dir": "engineering/codebase/app.frontend-services", "shards": 3,
           "run": "pnpm exec playwright test --shard=$SHARD/$SHARDS",
           "paths": ["engineering/codebase/app.frontend-services/**"]}


class FakeGitHub:
    """Answers the plan's REST paths from canned data and records what it was asked."""

    def __init__(self, head=HEAD, runs=(), jobs=None, compare=None, failing=()):
        self.head, self.runs, self.jobs, self.compare = head, list(runs), jobs or {}, compare or {}
        self.failing, self.asked = failing, []

    def get(self, path):
        self.asked.append(path)
        if any(path.startswith(prefix) for prefix in self.failing):
            raise URLError("502 Server Error for " + path)
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.head}}
        if re.fullmatch(r"/actions/runs/\d+", path):
            return {"id": int(path.rsplit("/", 1)[1]), "workflow_id": 7}
        if path.startswith("/actions/workflows/7/runs?"):
            return {"workflow_runs": self.runs}
        match = re.fullmatch(r"/actions/runs/(\d+)/jobs\?per_page=100", path)
        if match:
            return {"jobs": self.jobs.get(int(match.group(1)), [])}
        match = re.fullmatch(r"/compare/([^.]+)\.\.\.([a-f0-9]{40})", path)
        if match:
            return self.compare[match.group(1)]
        raise AssertionError("unexpected path " + path)


def compared(*paths, status="ahead"):
    return {"status": status, "files": [{"filename": path} for path in paths]}


def gate(conclusion="success", name="delivery / ci / gate"):
    return [{"name": "delivery / ci / plan", "conclusion": "success"}, {"name": name, "conclusion": conclusion}]


def run_plan(github, event="push", ref="refs/heads/main", **options):
    logged = []
    result = ci_plan.plan(github, event, ref, HEAD, 900, log=logged.append,
                          **{"backends": BACKENDS, "frontends": FRONTENDS, "browser": BROWSER, **options})
    return result, logged


def names(result):
    return ([entry["name"] for entry in result["backends"]], [entry["name"] for entry in result["frontends"]],
            [entry["shard"] for entry in result["browsers"]])


class GlobTests(unittest.TestCase):
    def test_globs_cross_folders_only_with_two_stars(self):
        self.assertTrue(ci_plan.matches(["**/*.md"], "README.md"))
        self.assertTrue(ci_plan.matches(["**/*.md"], "engineering/planning/v0.1/v0.1.md"))
        self.assertTrue(ci_plan.matches(["docs/**"], "docs/a/b.png"))
        self.assertFalse(ci_plan.matches(["docs/*"], "docs/a/b.png"))
        self.assertFalse(ci_plan.matches(["**/*.md"], "engineering/codebase/app/notes.mdx"))

    def test_docs_planning_research_and_product_are_docs_but_workflows_and_code_are_not(self):
        changed = ["README.md", "product/brand/logo.svg", "engineering/planning/backlog.md",
                   "engineering/research/shot.png", ".claude/rules/x.md", ".github/workflows/ci.yml",
                   "engineering/codebase/app.backend-services/Program.cs", "engineering/deployment/deploy.yml"]
        self.assertEqual([".github/workflows/ci.yml", "engineering/codebase/app.backend-services/Program.cs",
                          "engineering/deployment/deploy.yml"], ci_plan.code_paths(changed, ci_plan.DOCS))


class CheckTests(unittest.TestCase):
    def test_entries_fill_their_defaults(self):
        backends, frontends, shards = ci_plan.checks(BACKENDS, FRONTENDS, BROWSER)
        backend, runner = backends
        self.assertEqual((".", 30, 1, "", "global.json", "", True), (
            backend["dir"], backend["timeout"], backend["fetch-depth"], backend["lfs"], backend["dotnet-json"],
            backend["dotnet-version"], backend["dotnet"]))
        self.assertEqual(("", False, "3.12", "pyyaml"), (runner["node"], runner["dotnet"], runner["python"], runner["pip"]))
        self.assertEqual("pnpm run test\npnpm run build", frontends[0]["run"])
        self.assertEqual(("backend", "frontend"), (backend["slug"], frontends[0]["slug"]))
        self.assertEqual("engineering/codebase/app.frontend-services/.nvmrc", frontends[0]["node"])
        self.assertEqual("engineering/codebase/app.frontend-services/pnpm-lock.yaml", frontends[0]["pnpm-lock"])
        self.assertEqual([(1, 3), (2, 3), (3, 3)], [(entry["shard"], entry["shards"]) for entry in shards])

    def test_a_backend_with_node_takes_the_lockfile_beside_its_nvmrc_and_a_version_is_not_a_global_json(self):
        entry = ci_plan.backend({"name": "Api", "run": "dotnet test", "dotnet": "10.0.x", "history": True,
                                 "node": "engineering/codebase/app.frontend-services/.nvmrc"})
        self.assertEqual(("", "10.0.x", 0), (entry["dotnet-json"], entry["dotnet-version"], entry["fetch-depth"]))
        self.assertEqual("engineering/codebase/app.frontend-services/pnpm-lock.yaml", entry["pnpm-lock"])

    def test_lfs_fetches_everything_or_only_the_given_patterns_and_a_backend_may_name_its_lockfile(self):
        self.assertEqual("*", ci_plan.backend({"name": "A", "run": "x", "lfs": True})["lfs"])
        self.assertEqual("engineering/codebase/**",
                         ci_plan.backend({"name": "A", "run": "x", "lfs": "engineering/codebase/**"})["lfs"])
        entry = ci_plan.backend({"name": "A", "run": "x", "node": ".nvmrc", "pnpm-lock": "web/pnpm-lock.yaml"})
        self.assertEqual(("pnpm-lock.yaml", "web/pnpm-lock.yaml"),
                         (ci_plan.backend({"name": "A", "run": "x", "node": ".nvmrc"})["pnpm-lock"], entry["pnpm-lock"]))
        with self.assertRaisesRegex(ci_plan.Invalid, "lfs is true"):
            ci_plan.backend({"name": "A", "run": "x", "lfs": 3})

    def test_commands_may_be_a_list_of_lines(self):
        entry = ci_plan.backend({"name": "Runner (VPS kit)", "run": ["bash -n a.sh", "python -m unittest"]})
        self.assertEqual(("bash -n a.sh\npython -m unittest", "runner-vps-kit"), (entry["run"], entry["slug"]))
        shards = ci_plan.browsers({"name": "B", "dir": "web", "run": "x", "setup": ["one", "two"], "shards": 2})
        self.assertEqual(["one\ntwo"] * 2, [shard["setup"] for shard in shards])

    def test_bad_entries_are_refused_with_the_rule(self):
        for entry, rule in (({"name": "A"}, "run names its commands"),
                            ({"name": "A", "run": "x", "color": "red"}, "takes no color"),
                            ({"name": "", "run": "x"}, "needs a name"),
                            ({"name": "A", "run": "x", "dir": "../other"}, "relative path"),
                            ({"name": "A", "run": "x", "timeout": 500}, "1-120"),
                            ({"name": "A", "run": "x", "pip": "pyyaml"}, "pip needs python"),
                            ({"name": "A", "run": "x", "paths": "src/**"}, "list of non-empty strings")):
            with self.assertRaisesRegex(ci_plan.Invalid, rule):
                ci_plan.backend(entry)
        with self.assertRaisesRegex(ci_plan.Invalid, "names are unique"):
            ci_plan.checks([{"name": "A", "run": "x"}], [{"name": "A", "dir": "web"}], None)
        with self.assertRaisesRegex(ci_plan.Invalid, "shards is 1-10"):
            ci_plan.browsers({"name": "B", "dir": "web", "run": "x", "shards": 0})
        with self.assertRaisesRegex(ci_plan.Invalid, "script names"):
            ci_plan.frontend({"name": "F", "dir": "web", "scripts": ["test && rm -rf /"]})


class PlanTests(unittest.TestCase):
    def test_a_superseded_main_push_verifies_and_publishes_nothing(self):
        result, _ = run_plan(FakeGitHub(head=OTHER), supersede=True)
        self.assertEqual((True, False, ([], [], [])), (result["superseded"], result["changed"], names(result)))

    def test_supersede_applies_to_main_pushes_only(self):
        github = FakeGitHub(head=OTHER, compare={"main": compared("src/x.cs")})
        result, _ = run_plan(github, ref="refs/heads/feature", supersede=True)
        self.assertFalse(result["superseded"])
        result, _ = run_plan(FakeGitHub(head=OTHER), event="workflow_dispatch", supersede=True, dispatched=True)
        self.assertEqual((False, True), (result["superseded"], result["changed"]))

    def test_an_unreadable_head_verifies_the_push_with_a_warning(self):
        result, logged = run_plan(FakeGitHub(failing=("/git/ref/",), runs=[]), supersede=True)
        self.assertEqual((False, True), (result["superseded"], result["changed"]))
        self.assertIn("Could not read the head", logged[0])

    def test_a_docs_only_push_since_the_last_delivered_run_verifies_nothing(self):
        github = FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()},
                            compare={BASE: compared("README.md", "engineering/planning/backlog.md")})
        result, _ = run_plan(github, supersede=True)
        self.assertEqual((False, False, ([], [], [])), (result["superseded"], result["changed"], names(result)))
        self.assertIn("only docs changed (2 paths)", result["reason"])

    def test_a_superseded_run_is_never_the_base_so_its_code_still_ships(self):
        # Run 6 was superseded: its gate was skipped. Run 5 delivered; the code since 5 still needs a run.
        github = FakeGitHub(runs=[{"id": 6, "head_sha": BASE}, {"id": 5, "head_sha": OLDER}],
                            jobs={6: gate("skipped"), 5: gate()},
                            compare={OLDER: compared("README.md", "engineering/codebase/app.backend-services/A.cs")})
        result, _ = run_plan(github)
        self.assertTrue(result["changed"])
        self.assertEqual("changes since " + OLDER[:7], result["reason"])
        self.assertEqual((["Backend", "Runner"], [], []), names(result))

    def test_a_publish_from_before_v0_9_0_also_marks_its_commit_delivered(self):
        github = FakeGitHub(runs=[{"id": 900, "head_sha": HEAD}, {"id": 4, "head_sha": BASE}],
                            jobs={4: [{"name": "verify / backend", "conclusion": "success"},
                                      {"name": "publish / bundle", "conclusion": "success"}]},
                            compare={BASE: compared("docs/x.md")})
        result, _ = run_plan(github)
        self.assertFalse(result["changed"])
        self.assertNotIn("/actions/runs/900/jobs?per_page=100", github.asked)

    def test_a_check_runs_only_for_its_own_paths(self):
        github = FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()},
                            compare={BASE: compared("engineering/codebase/app.frontend-services/src/App.vue")})
        result, _ = run_plan(github)
        # Runner declares no paths, so it runs on every code change.
        self.assertEqual((["Runner"], ["Frontend"], [1, 2, 3]), names(result))

    def test_a_shared_path_no_check_owns_runs_every_check(self):
        github = FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()},
                            compare={BASE: compared(".github/workflows/publish-docker-image.yml")})
        result, _ = run_plan(github)
        self.assertEqual((["Backend", "Runner"], ["Frontend"], [1, 2, 3]), names(result))

    def test_a_rename_counts_both_paths(self):
        github = FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()},
                            compare={BASE: {"status": "ahead", "files": [
                                {"filename": "docs/moved.md",
                                 "previous_filename": "engineering/codebase/app.backend-services/A.cs"}]}})
        result, _ = run_plan(github)
        self.assertEqual((["Backend", "Runner"], [], []), names(result))

    def test_no_delivered_run_on_main_an_unrelated_base_or_a_cut_list_verifies_everything(self):
        everything = (["Backend", "Runner"], ["Frontend"], [1, 2, 3])
        result, _ = run_plan(FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate("failure")}))
        self.assertEqual((True, everything), (result["changed"], names(result)))
        result, _ = run_plan(FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()},
                                        compare={BASE: compared("README.md", status="diverged")}))
        self.assertEqual((True, everything), (result["changed"], names(result)))
        many = compared(*["docs/" + str(index) + ".md" for index in range(ci_plan.COMPARE_LIMIT)])
        result, _ = run_plan(FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()}, compare={BASE: many}))
        self.assertEqual((True, everything), (result["changed"], names(result)))

    def test_unreadable_changes_verify_everything_with_a_warning(self):
        result, logged = run_plan(FakeGitHub(failing=("/actions/",)))
        self.assertEqual((True, (["Backend", "Runner"], ["Frontend"], [1, 2, 3])), (result["changed"], names(result)))
        self.assertIn("Could not read the changes", logged[0])

    def test_a_dispatched_commit_verifies_everything_without_reading_changes(self):
        github = FakeGitHub()
        result, _ = run_plan(github, event="workflow_dispatch", dispatched=True)
        self.assertEqual((True, (["Backend", "Runner"], ["Frontend"], [1, 2, 3])), (result["changed"], names(result)))
        self.assertEqual([], github.asked)

    def test_a_new_branch_compares_with_main_and_a_pull_request_with_its_base(self):
        github = FakeGitHub(runs=[], compare={"main": compared("engineering/research/r.md")})
        result, _ = run_plan(github, ref="refs/heads/renovate/nuget")
        self.assertEqual((False, "only docs changed (1 paths): changes since the branch left main"),
                         (result["changed"], result["reason"]))
        github = FakeGitHub(compare={BASE: compared("engineering/codebase/app.backend-services/packages.lock.json")})
        result, _ = run_plan(github, event="pull_request", ref="refs/pull/3/merge", pull_base=BASE)
        self.assertEqual((["Backend", "Runner"], [], []), names(result))

    def test_a_product_adds_its_own_docs_paths(self):
        github = FakeGitHub(runs=[{"id": 5, "head_sha": BASE}], jobs={5: gate()},
                            compare={BASE: compared("engineering/operations/runbook.txt")})
        self.assertTrue(run_plan(github)[0]["changed"])
        self.assertFalse(run_plan(github, docs=["engineering/operations/**"])[0]["changed"])


class MainTests(unittest.TestCase):
    def test_main_writes_outputs_and_a_summary_and_refuses_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs, summary = Path(directory) / "out", Path(directory) / "summary"
            environment = {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "workflow_dispatch",
                           "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "1", "PLAN_COMMIT": HEAD,
                           "PLAN_DISPATCHED": "true", "PLAN_BACKENDS": json.dumps(BACKENDS),
                           "PLAN_BROWSER": json.dumps(BROWSER), "GITHUB_OUTPUT": str(outputs),
                           "GITHUB_STEP_SUMMARY": str(summary)}
            with unittest.mock.patch.dict(os.environ, environment, clear=True), \
                    unittest.mock.patch("sys.stdout"), unittest.mock.patch("sys.stderr"):
                self.assertEqual(0, ci_plan.main())
                written = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
                self.assertEqual(("false", "true", "[]"), (written["superseded"], written["changed"], written["frontends"]))
                self.assertEqual(["Backend", "Runner"], [entry["name"] for entry in json.loads(written["backends"])])
                self.assertEqual(3, len(json.loads(written["browsers"])))
                self.assertIn("browsers: Browser 1/3, Browser 2/3, Browser 3/3", summary.read_text())
                with unittest.mock.patch.dict(os.environ, {"PLAN_BACKENDS": "[not json"}):
                    self.assertEqual(1, ci_plan.main())


@unittest.skipUnless(HAS_YAML, "PyYAML reads the workflows")
class WorkflowTests(unittest.TestCase):
    """The shared workflows' shape: every job bounded, every action pinned, the gate covering every check."""

    def load(self, name):
        text = (WORKFLOWS / name).read_text()
        return yaml.safe_load(text), text

    def test_every_job_has_a_timeout_and_every_action_is_pinned_to_a_commit(self):
        for name in ("ci.yml", "delivery.yml", "publish.yml", "codeql.yml", "test.yml"):
            workflow, text = self.load(name)
            for job_name, job in workflow["jobs"].items():
                if "uses" not in job:
                    self.assertIn("timeout-minutes", job, name + ": " + job_name)
            for action in re.findall(r"uses: ([^\s#]+)", text):
                if action.startswith("./"):
                    continue
                self.assertRegex(action, r"@[a-f0-9]{40}$", name + ": " + action)

    def test_the_gate_needs_every_check_and_skips_only_a_superseded_run(self):
        workflow, _ = self.load("ci.yml")
        jobs = workflow["jobs"]
        self.assertEqual({"plan", "backend", "frontend", "browser"}, set(jobs["gate"]["needs"]))
        self.assertIn("always()", jobs["gate"]["if"])
        self.assertIn("needs.plan.outputs.superseded != 'true'", jobs["gate"]["if"])
        for check in ("backend", "frontend", "browser"):
            self.assertEqual("plan", jobs[check]["needs"])
        # PyYAML reads the `on` key as YAML 1.1's true.
        self.assertEqual("${{ jobs.plan.outputs.changed }}", workflow[True]["workflow_call"]["outputs"]["changed"]["value"])

    def test_delivery_publishes_only_a_verified_code_change_outside_pull_requests(self):
        workflow, _ = self.load("delivery.yml")
        publish = workflow["jobs"]["publish"]
        self.assertEqual("ci", publish["needs"])
        self.assertEqual("./.github/workflows/publish.yml", publish["uses"])
        for part in ("needs.ci.outputs.changed == 'true'", "github.event_name != 'pull_request'"):
            self.assertIn(part, publish["if"])
        self.assertEqual("./.github/workflows/ci.yml", workflow["jobs"]["ci"]["uses"])
        self.assertTrue(workflow["jobs"]["ci"]["with"]["supersede"])

    def test_publish_builds_with_the_layer_cache(self):
        workflow, _ = self.load("publish.yml")
        steps = workflow["jobs"]["bundle"]["steps"]
        build = next(step for step in steps if step.get("id") == "build")
        self.assertIn("--cache gha", build["run"])
        runtime = [index for index, step in enumerate(steps) if "ghaction-github-runtime" in step.get("uses", "")]
        self.assertTrue(runtime and runtime[0] < steps.index(build))


if __name__ == "__main__":
    unittest.main()
