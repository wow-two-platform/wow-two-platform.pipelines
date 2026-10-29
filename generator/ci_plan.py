#!/usr/bin/env python3
"""Decides what one run of the shared `ci` workflow verifies, before any runner checks out the product.

The run's own push is skipped when a newer one has already reached `main` (the `supersede` input). Otherwise the
paths that changed since the last delivered commit decide the work:
- only docs, planning or markdown changed: nothing builds, tests or publishes, and the gate still passes;
- otherwise each declared check runs when a changed path matches its `paths`, when it declares no `paths`, or when a
  changed path belongs to no check's `paths` (a shared file runs everything).

The last delivered commit is the head of the newest successful push run of the same workflow on the same branch whose
gate (or, before v0.9.0, whose publish) succeeded; a superseded run skips its gate, so it never counts. A dispatched
commit, a missing or unrelated base and any unreadable answer from GitHub verify everything.

A push to a Renovate branch (`renovate/…`) verifies and never publishes: Renovate merges a branch it automerges once
the push's checks pass. When a pull request run of the same workflow exists for the pushed commit, the push leaves the
verification to it, so one commit is verified once, and skips its gate like a superseded push.

Publish runs only when `publish` is true: code changed, and the run is not a pull request or a Renovate branch push.
Only the Python standard library is used, so the plan job installs nothing.
Rules: wow-two-ws/conventions/deployment/descriptor/deploy-descriptor.md § CI workflow.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Paths that never build, test or publish. A product adds its own through the `docs` input.
DOCS = ("**/*.md", "docs/**", "product/**", "engineering/planning/**", "engineering/research/**", ".claude/**",
        "LICENSE", "LICENSE.*")
# GitHub's compare API returns at most 300 files; a list that long may be cut, so it verifies everything.
COMPARE_LIMIT = 300
# How many successful runs to inspect for the last delivered commit.
RUN_LOOKBACK = 20
# A job whose success marks its run's commit delivered: the shared gate, or a pre-v0.9.0 publish.
DELIVERED_JOB = re.compile(r"(?:^|/ )(?:gate|publish / bundle)$")
# Renovate's branch prefix: a push there verifies and publishes nothing.
RENOVATE = "renovate/"
# Renovate opens a branch's pull request seconds after pushing the branch, so a push that finds no pull request run for
# its commit looks again once after this many seconds before it verifies.
PULL_WAIT = 30
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,59}")
SHA = re.compile(r"[a-f0-9]{40}")

BACKEND_KEYS = {"name", "run", "dir", "dotnet", "node", "pnpm-lock", "python", "pip", "pip-cache", "lfs", "history",
                "pipelines", "paths", "timeout"}
FRONTEND_KEYS = {"name", "dir", "scripts", "node", "lfs", "paths", "timeout"}
BROWSER_KEYS = {"name", "dir", "shards", "setup", "run", "dotnet", "node", "lfs", "paths", "artifact", "timeout"}


class Invalid(ValueError):
    """An input breaks a rule; the message names the rule."""


def check(condition, message):
    if not condition:
        raise Invalid(message)


# ---------------------------------------------------------------------- paths

def glob(pattern):
    """A path glob relative to the repository root: ** crosses folders, * and ? stay inside one."""
    parts, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))


def matches(patterns, path):
    return any(glob(pattern).fullmatch(path) for pattern in patterns)


def code_paths(changed, docs):
    """The changed paths that are not docs, planning or markdown."""
    return [path for path in changed if not matches(docs, path)]


# --------------------------------------------------------------------- checks

def strings(value, what):
    check(isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value),
          what + " must be a list of non-empty strings")
    return list(value)


def script(entry, key, name, required):
    """Shell commands as one string or a list of lines; the job runs them with bash -eo pipefail."""
    value = entry.get(key, "")
    if isinstance(value, list):
        value = "\n".join(strings(value, name + ": " + key))
    check(isinstance(value, str), name + ": " + key + " is shell commands, a string or a list of lines")
    check(not required or value.strip(), name + ": " + key + " names its commands")
    return value


def common(entry, keys, what, default_timeout):
    check(isinstance(entry, dict), what + " must be an object")
    unknown = set(entry) - keys
    check(not unknown, what + " takes no " + ", ".join(sorted(unknown)))
    name = entry.get("name")
    check(isinstance(name, str) and NAME.fullmatch(name), what + " needs a name of letters, digits, spaces and ._()/-")
    for key in ("history", "pipelines"):
        check(isinstance(entry.get(key, False), bool), name + ": " + key + " is true or false")
    lfs = entry.get("lfs", False)
    check(isinstance(lfs, bool) or (isinstance(lfs, str) and lfs.strip()),
          name + ": lfs is true (every LFS file) or the git lfs --include patterns to fetch")
    timeout = entry.get("timeout", default_timeout)
    check(type(timeout) is int and 1 <= timeout <= 120, name + ": timeout is 1-120 minutes")
    directory = entry.get("dir", ".")
    check(isinstance(directory, str) and directory and not directory.startswith("/") and ".." not in directory.split("/"),
          name + ": dir is a relative path inside the repository")
    return {"name": name, "slug": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "dir": directory.rstrip("/") or ".", "lfs": "*" if lfs is True else lfs or "",
            "timeout": timeout, "paths": strings(entry["paths"], name + ": paths") if "paths" in entry else None}


def dotnet_of(entry, name):
    """setup-dotnet reads either a global.json or a version; the job passes both and one stays empty."""
    value = entry.get("dotnet", "")
    check(isinstance(value, str), name + ": dotnet is a global.json path or an SDK version")
    is_file = value.endswith(".json")
    return {"dotnet": bool(value), "dotnet-json": value if is_file else "", "dotnet-version": "" if is_file else value}


def node_of(entry, name, directory, default):
    """The .nvmrc that sets up Node and pnpm: the entry's own, else the one in its folder when Node is the default."""
    if "node" in entry:
        check(isinstance(entry["node"], str) and entry["node"], name + ": node is the path of an .nvmrc")
        return entry["node"]
    if not default:
        return ""
    return directory + "/.nvmrc" if directory != "." else ".nvmrc"


def lockfile(directory):
    return (directory + "/" if directory != "." else "") + "pnpm-lock.yaml"


def backend(entry):
    base = common(entry, BACKEND_KEYS, "a backend check", 30)
    name = base["name"]
    run = script(entry, "run", name, True)
    for key in ("python", "pip", "pip-cache"):
        check(isinstance(entry.get(key, ""), str), name + ": " + key + " is a string")
    check(not entry.get("pip") or entry.get("python"), name + ": pip needs python")
    check(not entry.get("pip-cache") or entry.get("python"), name + ": pip-cache needs python")
    node = node_of(entry, name, base["dir"], False)
    lock = entry.get("pnpm-lock", lockfile(str(Path(node).parent)) if node else "")
    check(isinstance(lock, str) and (lock or not node), name + ": pnpm-lock is the pnpm-lock.yaml the Node cache keys on")
    return {**base, **dotnet_of(entry, name), "run": run, "node": node, "pnpm-lock": lock,
            "python": entry.get("python", ""), "pip": entry.get("pip", ""), "pip-cache": entry.get("pip-cache", ""),
            "fetch-depth": 0 if entry.get("history") else 1, "pipelines": entry.get("pipelines", False)}


def frontend(entry):
    base = common(entry, FRONTEND_KEYS, "a frontend check", 20)
    name = base["name"]
    check(base["dir"] != ".", name + ": dir names the frontend workspace")
    scripts = strings(entry.get("scripts", ["lint", "typecheck", "test", "build"]), name + ": scripts")
    check(scripts and all(re.fullmatch(r"[A-Za-z0-9:_-]+", script) for script in scripts),
          name + ": scripts are package.json script names")
    return {**base, "node": node_of(entry, name, base["dir"], True), "pnpm-lock": lockfile(base["dir"]),
            "run": "\n".join("pnpm run " + script for script in scripts)}


def browsers(entry):
    """One matrix entry per shard; each knows its shard and the shard count."""
    if entry in (None, "", {}):
        return []
    base = common(entry, BROWSER_KEYS, "the browser check", 30)
    name = base["name"]
    shards = entry.get("shards", 1)
    check(type(shards) is int and 1 <= shards <= 10, name + ": shards is 1-10")
    run, setup = script(entry, "run", name, True), script(entry, "setup", name, False)
    check(isinstance(entry.get("artifact", ""), str), name + ": artifact is a path")
    shared = {**base, **dotnet_of(entry, name), "node": node_of(entry, name, base["dir"], True),
              "pnpm-lock": lockfile(base["dir"]), "setup": setup, "run": run,
              "artifact": entry.get("artifact", ""), "shards": shards}
    return [{**shared, "shard": index} for index in range(1, shards + 1)]


def checks(backends, frontends, browser):
    """Every declared check, validated, as the jobs' matrix entries."""
    backends = [backend(entry) for entry in backends or []]
    frontends = [frontend(entry) for entry in frontends or []]
    shards = browsers(browser)
    names = [entry["name"] for entry in [*backends, *frontends]] + ([shards[0]["name"]] if shards else [])
    check(len(names) == len(set(names)), "check names are unique")
    return backends, frontends, shards


def select(backends, frontends, shards, code):
    """The checks a run performs for these changed code paths; None means every check."""
    if code is None:
        return backends, frontends, shards
    entries = [*backends, *frontends, *shards]
    owned = [pattern for entry in entries for pattern in entry["paths"] or []]
    unowned = any(not matches(owned, path) for path in code)

    def wanted(entry):
        return entry["paths"] is None or unowned or any(matches(entry["paths"], path) for path in code)

    return ([entry for entry in backends if wanted(entry)], [entry for entry in frontends if wanted(entry)],
            [entry for entry in shards if wanted(entry)])


# --------------------------------------------------------------------- github

class GitHub:
    """The few read-only REST calls the plan makes, with the job's token."""

    def __init__(self, repository, token, api="https://api.github.com"):
        self.repository, self.token, self.api = repository, token, api.rstrip("/")

    def get(self, path):
        request = Request(self.api + "/repos/" + self.repository + path,
                          headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                   **({"Authorization": "Bearer " + self.token} if self.token else {})})
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())


def head_of(github, branch):
    return github.get("/git/ref/heads/" + quote(branch, safe=""))["object"]["sha"]


def workflow_of(github, run_id):
    return github.get("/actions/runs/" + str(run_id))["workflow_id"]


def pull_request_run(github, workflow, commit):
    """The newest pull request run of `workflow` for `commit`, as "pull request #<n>" or "run <id>", else None."""
    listing = github.get("/actions/workflows/" + str(workflow) + "/runs?event=pull_request&per_page=1&head_sha="
                         + quote(commit, safe=""))
    runs = listing.get("workflow_runs") or []
    if not runs:
        return None
    pulls = runs[0].get("pull_requests") or []
    return "pull request #" + str(pulls[0]["number"]) if pulls else "run " + str(runs[0]["id"])


def delivered_base(github, run_id, branch, commit):
    """The head of the newest successful push run on `branch` whose gate or publish succeeded, else None."""
    workflow = workflow_of(github, run_id)
    listing = github.get("/actions/workflows/" + str(workflow) + "/runs?event=push&status=success&per_page="
                         + str(RUN_LOOKBACK) + "&branch=" + quote(branch, safe=""))
    for run in listing.get("workflow_runs") or []:
        if run.get("id") == run_id or not SHA.fullmatch(str(run.get("head_sha"))):
            continue
        jobs = github.get("/actions/runs/" + str(run["id"]) + "/jobs?per_page=100").get("jobs") or []
        if any(DELIVERED_JOB.search(str(job.get("name"))) and job.get("conclusion") == "success" for job in jobs):
            return run["head_sha"]
    return None


def changed_since(github, base, commit, related_only):
    """The paths changed from `base` to `commit`; None when the base is unrelated or the list may be cut."""
    compare = github.get("/compare/" + quote(base, safe="") + "..." + quote(commit, safe=""))
    if related_only and compare.get("status") not in ("ahead", "identical"):
        return None
    files = compare.get("files") or []
    if len(files) >= COMPARE_LIMIT:
        return None
    paths = set()
    for item in files:
        paths.add(item["filename"])
        if item.get("previous_filename"):
            paths.add(item["previous_filename"])
    return sorted(paths)


# ----------------------------------------------------------------------- plan

def plan(github, event, ref, commit, run_id, main="main", supersede=False, dispatched=False, pull_base="",
         docs=(), backends=(), frontends=(), browser=None, log=print, wait=time.sleep):
    """What this run verifies: {superseded, deferred, changed, publish, reason, backends, frontends, browsers}."""
    backends, frontends, shards = checks(backends, frontends, browser)
    docs = [*DOCS, *strings(list(docs), "docs")]
    nothing = {"deferred": False, "publish": False, "backends": [], "frontends": [], "browsers": []}
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    renovate = event == "push" and branch != main and branch.startswith(RENOVATE)
    if supersede and event == "push" and branch == main:
        try:
            head = head_of(github, main)
        except (HTTPError, URLError, OSError, KeyError, ValueError) as error:
            log("::warning title=ci plan::Could not read the head of " + main + " (" + str(error)[:120]
                + "); verifying this push")
            head = commit
        if head != commit:
            return {"superseded": True, "changed": False, "reason": "superseded by " + head[:7], **nothing}
    if renovate:
        try:
            workflow = workflow_of(github, run_id)
            pull = pull_request_run(github, workflow, commit)
            if pull is None:
                wait(PULL_WAIT)
                pull = pull_request_run(github, workflow, commit)
        except (HTTPError, URLError, OSError, KeyError, ValueError, TypeError) as error:
            log("::warning title=ci plan::Could not read the pull request runs of " + commit[:7] + " ("
                + str(error)[:120] + "); verifying this push")
            pull = None
        if pull is not None:
            return {"superseded": False, "changed": False, "reason": pull + " verifies " + commit[:7] + " of " + branch,
                    **nothing, "deferred": True}
    changed, reason = None, "verifying everything"
    try:
        if dispatched:
            reason = "a dispatched commit verifies everything"
        elif event == "pull_request" and SHA.fullmatch(pull_base or ""):
            changed = changed_since(github, pull_base, commit, related_only=False)
            reason = "changes in the pull request"
        elif event == "push" and branch:
            base = delivered_base(github, run_id, branch, commit)
            if base:
                changed = changed_since(github, base, commit, related_only=True)
                reason = ("changes since " + base[:7] if changed is not None
                          else "base " + base[:7] + " is unrelated or too far behind; verifying everything")
            elif branch != main:
                changed = changed_since(github, main, commit, related_only=False)
                reason = "changes since the branch left " + main
            else:
                reason = "no delivered run on " + main + "; verifying everything"
    except (HTTPError, URLError, OSError, KeyError, ValueError) as error:
        log("::warning title=ci plan::Could not read the changes (" + str(error)[:120] + "); verifying everything")
        changed, reason = None, "changes unreadable; verifying everything"
    if changed is None:
        selected = select(backends, frontends, shards, None)
    else:
        code = code_paths(changed, docs)
        if not code:
            return {"superseded": False, "changed": False,
                    "reason": "only docs changed (" + str(len(changed)) + " paths): " + reason, **nothing}
        selected = select(backends, frontends, shards, code)
    return {"superseded": False, "deferred": False, "changed": True,
            "publish": event != "pull_request" and not renovate,
            "reason": reason + ("; a Renovate branch publishes nothing" if renovate else ""),
            "backends": selected[0], "frontends": selected[1], "browsers": selected[2]}


def summary(result):
    lines = ["### CI plan", "", "- " + result["reason"]]
    if result["superseded"]:
        lines.append("- Superseded push: nothing verified or published.")
    elif result["deferred"]:
        lines.append("- Verified by the pull request's run: nothing verified or published here.")
    elif not result["changed"]:
        lines.append("- Docs only: nothing built, tested or published.")
    else:
        for kind in ("backends", "frontends", "browsers"):
            names = [entry["name"] + (" " + str(entry["shard"]) + "/" + str(entry["shards"]) if "shard" in entry else "")
                     for entry in result[kind]]
            lines.append("- " + kind + ": " + (", ".join(names) if names else "none"))
        lines.append("- publish: " + ("after the checks pass" if result["publish"] else "no"))
    return "\n".join(lines) + "\n"


def parsed(name, default):
    text = os.environ.get(name, "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        raise Invalid(name + " is not valid JSON") from None


def main():
    environment = os.environ
    try:
        github = GitHub(environment["GITHUB_REPOSITORY"], environment.get("GH_TOKEN", ""),
                        environment.get("GITHUB_API_URL", "https://api.github.com"))
        commit = environment["PLAN_COMMIT"]
        check(SHA.fullmatch(commit), "PLAN_COMMIT is a full commit SHA")
        result = plan(github, environment["GITHUB_EVENT_NAME"], environment.get("GITHUB_REF", ""), commit,
                      int(environment.get("GITHUB_RUN_ID", "0")), environment.get("PLAN_MAIN") or "main",
                      environment.get("PLAN_SUPERSEDE") == "true", environment.get("PLAN_DISPATCHED") == "true",
                      environment.get("PLAN_PULL_BASE", ""), parsed("PLAN_DOCS", []), parsed("PLAN_BACKENDS", []),
                      parsed("PLAN_FRONTENDS", []), parsed("PLAN_BROWSER", None),
                      log=lambda line: print(line, file=sys.stderr))
    except Invalid as error:
        print("::error title=ci plan::" + str(error), file=sys.stderr)
        return 1
    outputs = {"superseded": str(result["superseded"]).lower(), "deferred": str(result["deferred"]).lower(),
               "changed": str(result["changed"]).lower(), "publish": str(result["publish"]).lower(),
               "backends": json.dumps(result["backends"]), "frontends": json.dumps(result["frontends"]),
               "browsers": json.dumps(result["browsers"])}
    if environment.get("GITHUB_OUTPUT"):
        with open(environment["GITHUB_OUTPUT"], "a") as stream:
            for key, value in outputs.items():
                stream.write(key + "=" + value + "\n")
    if environment.get("GITHUB_STEP_SUMMARY"):
        with open(environment["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(summary(result))
    print(json.dumps({key: result[key] for key in ("superseded", "deferred", "changed", "publish", "reason")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
