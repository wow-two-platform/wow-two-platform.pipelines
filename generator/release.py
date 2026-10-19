#!/usr/bin/env python3
"""Builds a product's changed services from its deploy.yml and writes a Wheelhouse release bundle.

A product version is X.Y.Z: X.Y is the newest engineering/planning/version-track/v{X.Y}/ folder, and Z the next patch
after the highest vX.Y.* tag. A build of `main` is a release vX.Y.Z; a build of `dev` replaces that branch's previous
build; any other branch, or a dispatched commit, builds a candidate. Only services whose paths changed since
the base release rebuild; the rest keep the base release's image and version, so each service carries the version in
which it last changed. A base whose source commit is not in the repository's history (an author rewrite removed it) is
not used: every service rebuilds and the build warns. Descriptor 2 declares the product's sites at the top and the paths each service serves per site;
descriptor 1 (one path per service site) still reads and keeps its bundles byte for byte.
Rules: wow-two-ws/conventions/deployment/descriptor/deploy-descriptor.md.
Requires Python 3.9+, Git and, for `build` / `ci`, Docker with Buildx; the descriptor parser needs PyYAML.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile

DESCRIPTOR = "engineering/deployment/deploy.yml"
VERSION_TRACK = "engineering/planning/version-track"
ROOT = "engineering/"
SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}")
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")
BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SHA = re.compile(r"[a-f0-9]{40}")
LINE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
# Branches whose builds replace their previous build under one moving image tag.
REPOSITORY = re.compile(r"[a-z0-9][a-z0-9._/:-]*[a-z0-9]")
MEMORY = re.compile(r"[1-9][0-9]*[kmg]")
DURATION = re.compile(r"[1-9][0-9]*[smh]")
CAPABILITY = re.compile(r"[A-Z][A-Z_]{1,31}")
SITE_PATH = re.compile(r"/(?:[A-Za-z0-9._~-]+/)*(?:[A-Za-z0-9._~-]+)?")
# A descriptor 2 site path: `/`, or segments without a trailing slash; `.` and `..` segments are refused apart.
ROUTE_PATH = re.compile(r"/|(?:/[A-Za-z0-9._~-]+)+")
PLATFORMS = ("linux/amd64", "linux/arm64")
NEEDS = ("postgres", "valkey", "broker")
EXPOSURES = ("public", "private")
ENVIRONMENTS = ("dev", "prod")
SITE_KEYS = {"exposure", "primary", "environments"}
# Where a service's private settings file mounts unless its descriptor names a path; the .NET host reads it after
# appsettings.{Environment}.json. Bundles before v0.7.0 mounted /app/appsettings.Local.json, now a machine's own file.
SETTINGS_FILE = "/app/appsettings.Target.json"
SERVICE_KEYS = {"image", "build", "paths", "port", "health", "memory", "stopGrace", "settings", "volumes", "sites",
                "needs", "capabilities", "dependsOn"}
TOP_KEYS = {"descriptor", "product", "platform", "shared", "sites", "services", "dotnetEnvironment", "verify"}
# The end-to-end check after a dev rollout: how long it may run, by default and at most.
VERIFY_TIMEOUT, VERIFY_TIMEOUT_LIMIT = 300, 1800


class Invalid(ValueError):
    """The descriptor or an argument breaks a rule; the message names the rule."""


def check(condition, message):
    if not condition:
        raise Invalid(message)


# ------------------------------------------------------------------ descriptor

def parse(text):
    try:
        import yaml
    except ImportError:
        raise Invalid("Reading deploy.yml needs PyYAML (pip install pyyaml)") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise Invalid("deploy.yml is not valid YAML") from None


def relative(value, what):
    check(isinstance(value, str) and value and not value.startswith("/") and ".." not in PurePosixPath(value).parts,
          what + " must be a path relative to engineering/")
    return value


def validate(descriptor):
    """Checks a parsed descriptor and returns it with every default filled in."""
    check(isinstance(descriptor, dict), "deploy.yml must be a mapping")
    unknown = set(descriptor) - TOP_KEYS
    check(not unknown, "Unknown top-level key: " + ", ".join(sorted(map(str, unknown))))
    version = descriptor.get("descriptor")
    check(type(version) is int and version in (1, 2), "descriptor must be 1 or 2")
    check(version == 2 or "sites" not in descriptor, "Top-level sites need descriptor 2")
    check(isinstance(descriptor.get("product"), str) and SLUG.fullmatch(descriptor["product"]),
          "product must be a lowercase slug")
    platform = descriptor.get("platform", "linux/amd64")
    check(platform in PLATFORMS, "platform must be linux/amd64 or linux/arm64")
    shared = descriptor.get("shared") or []
    check(isinstance(shared, list), "shared must be a list of paths")
    services = descriptor.get("services")
    check(isinstance(services, dict) and services, "services must name at least one service")
    result = {"descriptor": version, "product": descriptor["product"], "platform": platform,
              "shared": [relative(path, "shared path") for path in shared], "services": {}}
    roles = sites_of(descriptor.get("sites")) if version == 2 else None
    # (site, path) -> the service that serves it: every request a site answers has exactly one destination.
    claimed = {}
    for name, service in services.items():
        check(isinstance(name, str) and SLUG.fullmatch(name), "Service names must be lowercase slugs")
        check(isinstance(service, dict), name + ": a service must be a mapping")
        unknown = set(service) - SERVICE_KEYS
        check(not unknown, name + ": unknown key " + ", ".join(sorted(map(str, unknown))))
        result["services"][name] = service_of(name, service, claimed, roles)
    if roles is not None:
        served = {site for site, _ in claimed}
        idle = [site for site in roles if site not in served]
        check(not idle, "No service serves site " + ", ".join(idle))
        result["sites"] = roles
    dependency_order(result["services"])
    dotnet = descriptor.get("dotnetEnvironment", False)
    check(type(dotnet) is bool, "dotnetEnvironment must be true or false")
    result["dotnetEnvironment"] = dotnet
    result["verify"] = verify_of(descriptor.get("verify"), result["services"])
    return result


def seconds(value, what):
    """A duration like 30s, 5m or 1h, in seconds."""
    check(isinstance(value, str) and DURATION.fullmatch(value), what + " must look like 30s, 5m or 1h")
    return int(value[:-1]) * {"s": 1, "m": 60, "h": 3600}[value[-1]]


def verify_of(verify, services):
    """The end-to-end check the runner runs after a dev rollout: a command in a one-off container from one service's
    release image, bounded by a timeout. None when the descriptor declares none."""
    if verify is None:
        return None
    check(isinstance(verify, dict) and set(verify) <= {"service", "command", "timeout"},
          "verify takes service, command and timeout")
    check(isinstance(verify.get("service"), str) and verify["service"] in services, "verify service must name a service")
    command = verify.get("command")
    check(isinstance(command, list) and 0 < len(command) <= 32
          and all(isinstance(part, str) and part and len(part) <= 1000 and "\0" not in part for part in command),
          "verify command must list the program and its arguments")
    timeout = seconds(verify.get("timeout", str(VERIFY_TIMEOUT) + "s"), "verify timeout")
    check(10 <= timeout <= VERIFY_TIMEOUT_LIMIT, "verify timeout must be 10s-30m")
    return {"service": verify["service"], "command": command, "timeoutSeconds": timeout}


def sites_of(declared):
    """Descriptor 2's product sites: host roles Wheelhouse binds to one host per environment. Each takes `exposure`
    (public by default), `primary` (the front door; the first site unless one says so) and `environments` (both by
    default). Hosts, zones and proxying never appear here."""
    declared = {} if declared is None else declared
    check(isinstance(declared, dict), "sites must map site names to their options")
    result = {}
    for site, options in declared.items():
        check(isinstance(site, str) and SLUG.fullmatch(site), "Site names must be lowercase slugs")
        options = {} if options is None else options
        check(isinstance(options, dict) and set(options) <= SITE_KEYS,
              "site " + site + ": takes exposure, primary and environments")
        exposure = options.get("exposure", "public")
        check(exposure in EXPOSURES, "site " + site + ": exposure must be public or private")
        check(type(options.get("primary", False)) is bool, "site " + site + ": primary must be true or false")
        environments = options.get("environments", list(ENVIRONMENTS))
        check(isinstance(environments, list) and environments and all(item in ENVIRONMENTS for item in environments)
              and len(set(environments)) == len(environments),
              "site " + site + ": environments must list dev, prod or both")
        result[site] = {"exposure": exposure, "primary": options.get("primary", False),
                        "environments": [item for item in ENVIRONMENTS if item in environments]}
    primaries = [site for site, options in result.items() if options["primary"]]
    check(len(primaries) <= 1, "Only one site may be primary: " + ", ".join(primaries))
    if result and not primaries:
        result[next(iter(result))]["primary"] = True
    return result


def route_path(value, what):
    """A descriptor 2 site path: `/` or `/a/b`, no trailing slash, no `.` or `..` segment. The longest prefix wins,
    and `/api` serves `/api` and `/api/…`, never `/apiary`."""
    check(isinstance(value, str) and len(value) <= 200 and ROUTE_PATH.fullmatch(value)
          and not {".", ".."} & set(value.split("/")),
          what + " must look like / or /api: letters, digits and . _ ~ - between slashes, no trailing slash")
    return value


def site_warnings(descriptor):
    """What the generator warns about without refusing: a site no service serves at `/` answers 404 outside its
    paths."""
    if descriptor.get("descriptor") != 2:
        return []
    paths = {}
    for service in descriptor["services"].values():
        for site, entry in service["sites"].items():
            paths.setdefault(site, []).extend(entry["paths"])
    return ["site " + site + " has no / path: its host answers 404 outside " + ", ".join(sorted(found))
            for site, found in paths.items() if "/" not in found]


def dependency_order(services):
    """The services in start order, each after the services it depends on; refuses an unknown service or a cycle.
    Dependencies name services of this bundle only: platform services (postgres, valkey, broker) go under `needs`."""
    for name, service in services.items():
        for other in service["dependsOn"]:
            check(other in services, name + ": dependsOn must name a service of this product"
                  + ("; platform services go under needs" if other in NEEDS else ""))
    placed, order = set(), []
    while len(order) < len(services):
        ready = [name for name in services if name not in placed and set(services[name]["dependsOn"]) <= placed]
        check(ready, "dependsOn forms a cycle: " + ", ".join(sorted(set(services) - placed)))
        order += ready
        placed.update(ready)
    return order


def service_of(name, service, claimed, roles=None):
    build = service.get("build")
    check(isinstance(build, dict) and set(build) <= {"dockerfile", "context", "target", "args", "contexts"},
          name + ": build takes dockerfile, context, target, args and contexts")
    args, contexts = build.get("args") or {}, build.get("contexts") or {}
    check(isinstance(args, dict) and all(isinstance(key, str) and isinstance(value, (str, int)) for key, value in args.items()),
          name + ": build args must map names to values")
    check(isinstance(contexts, dict) and all(isinstance(key, str) for key in contexts),
          name + ": build contexts must map names to paths")
    check(build.get("target") is None or isinstance(build["target"], str), name + ": build target must be a stage name")
    paths = service.get("paths")
    check(isinstance(paths, list) and paths, name + ": paths must list what changes the service")
    port = service.get("port", 8080)
    check(type(port) is int and 1 <= port <= 65535, name + ": port must be 1-65535")
    health = service.get("health")
    if isinstance(health, dict):
        command = health.get("command")
        check(set(health) == {"command"} and isinstance(command, list) and command
              and all(isinstance(part, str) for part in command), name + ": health command must be a list")
        test = ["CMD", *command]
    else:
        check(isinstance(health, str) and SITE_PATH.fullmatch(health), name + ": health must be an HTTP path or a command")
        test = ["CMD", "curl", "--fail", "--silent", "http://localhost:" + str(port) + health]
    memory, grace = str(service.get("memory", "512m")), str(service.get("stopGrace", "30s"))
    check(MEMORY.fullmatch(memory), name + ": memory must look like 512m or 1g")
    check(DURATION.fullmatch(grace), name + ": stopGrace must look like 30s")
    settings = service.get("settings")
    if settings is not None:
        check(isinstance(settings, dict) and set(settings) <= {"file", "required"}, name + ": settings takes file and required")
        file = settings.get("file", SETTINGS_FILE)
        required = settings.get("required") or []
        check(isinstance(file, str) and file.startswith("/"), name + ": settings file must be an absolute path")
        check(isinstance(required, list) and all(isinstance(key, str) and key for key in required),
              name + ": settings required must list keys")
        settings = {"file": file, "required": required}
    volumes = service.get("volumes") or {}
    check(isinstance(volumes, dict) and all(isinstance(key, str) and SLUG.fullmatch(key) and isinstance(path, str)
                                            and path.startswith("/") for key, path in volumes.items()),
          name + ": volumes must map slugs to absolute paths")
    declared_sites = service.get("sites") or {}
    check(isinstance(declared_sites, dict), name + ": sites must map site names to their options")
    sites = {}
    for site, options in declared_sites.items():
        options = options or {}
        if roles is None:
            # Descriptor 1: one path per service site, with its own exposure.
            check(isinstance(site, str) and SLUG.fullmatch(site) and isinstance(options, dict)
                  and set(options) <= {"port", "path", "exposure"}, name + ": sites map slugs to port, path and exposure")
            entry = {"port": options.get("port", port), "path": options.get("path", "/"),
                     "exposure": options.get("exposure", "public")}
            check(isinstance(entry["path"], str) and SITE_PATH.fullmatch(entry["path"]),
                  name + ": site path must start with /")
            check(entry["exposure"] in EXPOSURES, name + ": site exposure must be public or private")
            routes = [entry["path"]]
        else:
            check(isinstance(site, str) and SLUG.fullmatch(site) and isinstance(options, dict),
                  name + ": sites map site names to paths and port")
            check("path" not in options, name + ": site " + site + " lists paths, not path, in descriptor 2")
            check("exposure" not in options, name + ": site " + site + ": exposure belongs to the top-level site")
            check(set(options) <= {"port", "paths"}, name + ": site " + site + " takes paths and port")
            check(site in roles, name + ": site " + site + " is not declared under the top-level sites")
            routes = options.get("paths", ["/"])
            check(isinstance(routes, list) and routes, name + ": site " + site + " paths must list at least one path")
            routes = [route_path(path, name + ": site " + site + " path " + json.dumps(path)) for path in routes]
            check(len(set(routes)) == len(routes), name + ": site " + site + " lists a path twice")
            entry = {"port": options.get("port", port), "paths": routes}
        check(type(entry["port"]) is int and 1 <= entry["port"] <= 65535, name + ": site port must be 1-65535")
        for path in routes:
            check((site, path) not in claimed, name + ": another service (" + str(claimed.get((site, path)))
                  + ") already serves site " + site + " at " + path)
            claimed[(site, path)] = name
        sites[site] = entry
    needs = service.get("needs") or []
    check(isinstance(needs, list) and set(needs) <= set(NEEDS), name + ": needs takes postgres, valkey and broker")
    depends = service.get("dependsOn") or []
    check(isinstance(depends, list) and all(isinstance(other, str) and SLUG.fullmatch(other) for other in depends)
          and len(set(depends)) == len(depends), name + ": dependsOn must list service names once each")
    check(name not in depends, name + ": a service cannot depend on itself")
    capabilities = service.get("capabilities") or []
    check(isinstance(capabilities, list) and all(isinstance(item, str) and CAPABILITY.fullmatch(item) for item in capabilities),
          name + ": capabilities must be Linux capability names")
    image = service.get("image")
    # A registry port may carry a colon; the last path segment may not, or it would be a tag.
    check(image is None or (isinstance(image, str) and REPOSITORY.fullmatch(image) and "@" not in image
                            and ":" not in image.rsplit("/", 1)[-1]), name + ": image must be a repository without a tag")
    return {"image": image,
            "build": {"dockerfile": relative(build.get("dockerfile"), name + ": build dockerfile"),
                      "context": relative(build.get("context", "codebase"), name + ": build context"),
                      "target": build.get("target"), "args": {key: str(value) for key, value in args.items()},
                      "contexts": {key: relative(path, name + ": build context " + key) for key, path in contexts.items()}},
            "paths": [relative(path, name + ": path") for path in paths], "port": port, "health": test,
            "memory": memory, "stopGrace": grace, "settings": settings, "volumes": volumes, "sites": sites,
            "needs": needs, "capabilities": capabilities, "dependsOn": depends}


# ---------------------------------------------------------------------- change

def glob(pattern):
    """A path glob relative to engineering/: ** crosses folders, * and ? stay inside one."""
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
    return re.compile(re.escape(ROOT) + "".join(parts))


def triggers(descriptor, name):
    service = descriptor["services"][name]
    return [glob(path) for path in [*service["paths"], *descriptor["shared"], service["build"]["dockerfile"]]]


def changed_services(descriptor, changed_paths):
    return [name for name in descriptor["services"]
            if any(pattern.fullmatch(path) for pattern in triggers(descriptor, name) for path in changed_paths)]


def git(repo, *arguments):
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True)
    check(result.returncode == 0, "git " + arguments[0] + " failed: " + (result.stderr.strip().splitlines() or ["?"])[-1])
    return result.stdout


# The longest a fetch of a missing base commit may take before the base counts as lost.
FETCH_TIMEOUT = 300


def git_ok(repo, *arguments, timeout=None):
    """Whether a git command succeeds; a command that outlives `timeout` seconds fails. Never asks for credentials."""
    try:
        return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def base_commit_known(repo, source, commit):
    """Whether the base release's source commit is in this repository's history, related to `commit`.

    A commit this clone lacks is fetched from origin by SHA, and a shallow clone is deepened before the base counts as
    unrelated, so a shallow checkout still finds a reachable base. A commit an author rewrite removed from the history
    is not found, and one that shares no history with `commit` is not used. It need not be an ancestor: a candidate on
    a branch behind `main` diffs against the newest release all the same."""
    if not (isinstance(source, str) and SHA.fullmatch(source)):
        return False
    present = source + "^{commit}"
    if not git_ok(repo, "cat-file", "-e", present):
        if not (git_ok(repo, "fetch", "--quiet", "--no-tags", "origin", source, timeout=FETCH_TIMEOUT)
                and git_ok(repo, "cat-file", "-e", present)):
            return False
    if git_ok(repo, "merge-base", source, commit):
        return True
    shallow = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True).stdout.strip() == "true"
    return (shallow and git_ok(repo, "fetch", "--quiet", "--no-tags", "--unshallow", "origin", timeout=FETCH_TIMEOUT)
            and git_ok(repo, "merge-base", source, commit))


def base_changes(repo, source, commit):
    """The paths that differ between the base release's source commit and `commit`; None when the base is lost, that
    is when its commit is not in this history or the diff fails."""
    if not base_commit_known(repo, source, commit):
        return None
    try:
        return git(repo, "diff", "--name-only", source, commit).splitlines()
    except Invalid:
        return None


def plan(descriptor, repo, commit, base=None, tag=None, version=None):
    """Which services to build and every service's version; `base` is the previous release's manifest.

    `version` is the build's own version (`build_identity`); without it a release takes its tag's and a candidate
    `<base version>+<commit>`. A base whose source commit is lost (`base_changes`) is not used: every service builds,
    carries this build's version, and the plan holds a `warning` saying so."""
    check(SHA.fullmatch(commit), "Resolve the commit to its full SHA first")
    check(tag is None or TAG.fullmatch(tag), "A release tag looks like v1.2.3")
    short = commit[:7]
    descriptor_changed, warning, carried = False, None, base
    if base is not None:
        check(base.get("product") == descriptor["product"], "The base release belongs to another product")
        paths = base_changes(repo, base.get("sourceCommit"), commit)
        if paths is None:
            carried = None
            warning = ("Base " + str(base["release"]) + "'s source commit (" + str(base.get("sourceCommit"))[:7]
                       + ") is not in this history; every service was rebuilt.")
    if carried is None:
        rebuild = list(descriptor["services"])
    else:
        rebuild = changed_services(descriptor, paths)
        descriptor_changed = DESCRIPTOR in paths
        # A service the base release never shipped has no image to keep.
        rebuild += [name for name in descriptor["services"] if name not in base["images"] and name not in rebuild]
    label = tag or "sha-" + short
    base_version = TAG.fullmatch(base["release"]) and base["release"][1:] if base else None
    services = {}
    for name in descriptor["services"]:
        if name in rebuild:
            built = version or (tag[1:] if tag else (base_version or "0.0.0") + "+" + short)
            services[name] = {"build": True, "version": built, "changedIn": label}
        else:
            kept = (base.get("versions") or {}).get(name) or {"version": base_version or base["release"],
                                                               "changedIn": base["release"]}
            services[name] = {"build": False, "version": kept["version"], "changedIn": kept["changedIn"],
                              "image": base["images"][name]}
    planned = {"product": descriptor["product"], "release": label, "kind": "release" if tag else "candidate",
               "sourceCommit": commit, "base": carried["release"] if carried else None, "services": services,
               "descriptorChanged": descriptor_changed}
    if warning:
        planned["warning"] = warning
    return planned


def announce(planned):
    """Prints the plan's warning, if any, as a GitHub Actions annotation: a line in the job log."""
    if planned.get("warning"):
        print("::warning title=release base::" + planned["warning"], file=sys.stderr)


# --------------------------------------------------------------------- version

def version_line(repo, commit):
    """(X, Y) of the newest `version-track/v{X.Y}/` folder at the commit; (0, 0) before `v0.1`."""
    listing = subprocess.run(["git", "-C", str(repo), "ls-tree", "-d", "--name-only", commit + ":" + VERSION_TRACK],
                             capture_output=True, text=True)
    if listing.returncode:
        return 0, 0
    found = [LINE.fullmatch(name.rsplit("/", 1)[-1]) for name in listing.stdout.split()]
    return max(((int(match.group(1)), int(match.group(2))) for match in found if match), default=(0, 0))


def next_patch(repo, line):
    """The patch after the highest `vX.Y.Z` release tag on the line, 0 when it has none."""
    patches = [int(match.group(3)) for match in map(TAG.fullmatch, git(repo, "tag", "--list", "v*").split())
               if match and not match.group(4) and (int(match.group(1)), int(match.group(2))) == line]
    return max(patches) + 1 if patches else 0


def tag_safe(branch):
    """A branch name as one SemVer pre-release identifier: `feat/Login` → `feat-login`."""
    safe = re.sub(r"[^a-z0-9-]+", "-", branch.lower()).strip("-")[:40].strip("-") or "branch"
    return "b" + safe if safe.isdigit() else safe


def commits_ahead(repo, commit, main):
    """How many commits the branch holds that `main` does not; 0 when no `main` ref exists."""
    for ref in ("origin/" + main, main):
        if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                          capture_output=True).returncode == 0:
            return int(git(repo, "rev-list", "--count", ref + ".." + commit).strip())
    return 0


def build_identity(repo, commit, branch=None, main="main"):
    """How a build of `commit` is published: channel, kind, version, bundle label, image tag and moving tag.

    `main` releases `X.Y.Z`; any other branch publishes a pre-release `X.Y.(Z+1)-<branch>.<n>` as a candidate tagged
    `sha-<commit>`, with no moving tag; a dispatched commit (no branch) is a candidate `X.Y.(Z+1)-g<commit>`."""
    check(SHA.fullmatch(commit), "Resolve the commit to its full SHA first")
    check(not branch or BRANCH_NAME.fullmatch(branch), "Invalid branch")
    x, y = version_line(repo, commit)
    z = next_patch(repo, (x, y))
    short = commit[:7]
    if branch == main:
        version = "{}.{}.{}".format(x, y, z)
        return {"channel": "release", "kind": "release", "version": version, "release": "v" + version,
                "tag": version, "moving": "latest"}
    if branch:
        version = "{}.{}.{}-{}.{}".format(x, y, z, tag_safe(branch), commits_ahead(repo, commit, main))
        return {"channel": "branch", "kind": "candidate", "version": version, "release": "sha-" + short,
                "tag": "sha-" + short, "moving": None}
    return {"channel": "branch", "kind": "candidate", "version": "{}.{}.{}-g{}".format(x, y, z, short),
            "release": "sha-" + short, "tag": "sha-" + short, "moving": None}


def nothing_to_publish(planned):
    """True when a pushed build would publish only the base release again."""
    return (planned["base"] is not None and not planned["descriptorChanged"]
            and not any(entry["build"] for entry in planned["services"].values()))


# ---------------------------------------------------------------------- bundle

def render(descriptor, planned, images, branch=None, rollback_compatible=False, identity=None):
    """compose.json and release.json for the planned release, given every service's digest-pinned image."""
    product, platform = descriptor["product"], descriptor["platform"]
    compose_services, volumes = {}, {}
    for name, service in descriptor["services"].items():
        mounts = []
        if service["settings"]:
            variable = name.upper().replace("-", "_") + "_SETTINGS"
            mounts.append({"type": "bind", "source": "${" + variable + ":?Set " + variable + "}",
                           "target": service["settings"]["file"], "read_only": True})
        for volume, path in service["volumes"].items():
            mounts.append({"type": "volume", "source": volume, "target": path})
            volumes[volume] = {}
        definition = {"image": images[name], "platform": platform, "restart": "unless-stopped",
                      "mem_limit": service["memory"], "init": True, "stop_grace_period": service["stopGrace"],
                      "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"],
                      "healthcheck": {"test": service["health"], "interval": "10s", "timeout": "5s",
                                      "start_period": "60s", "retries": 6},
                      # The ingress and sibling services reach it as <product>-<environment>-<service>.
                      "networks": {"platform": {"aliases": [product + "-${DEPLOY_ENVIRONMENT}-" + name]}},
                      "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
        if service["capabilities"]:
            definition["cap_add"] = service["capabilities"]
        if service["dependsOn"]:
            # Compose starts the service once each dependency is healthy. No `restart: true`: a dependency's update
            # never restarts its dependents. Written only when declared, so other bundles stay byte for byte the same.
            definition["depends_on"] = {other: {"condition": "service_healthy"} for other in sorted(service["dependsOn"])}
        if mounts:
            definition["volumes"] = mounts
        compose_services[name] = definition
    compose = {"services": compose_services,
               "networks": {"platform": {"external": True, "name": "${PLATFORM_NETWORK:?Set PLATFORM_NETWORK}"}}}
    if volumes:
        compose["volumes"] = volumes
    compose_bytes = (json.dumps(compose, indent=2) + "\n").encode()
    # Descriptor 2 writes schema 2: `sites` holds the product's host roles and `routes` each service's paths per site.
    # A descriptor 1 bundle keeps schema 1, byte for byte: `sites` holds each service's sites with one path each.
    second = descriptor.get("descriptor") == 2
    served = {name: service["sites"] for name, service in descriptor["services"].items() if service["sites"]}
    manifest = {"schemaVersion": 2 if second else 1, "product": product, "release": planned["release"],
                "kind": planned["kind"], "sourceCommit": planned["sourceCommit"], "platform": platform,
                "rollbackCompatible": rollback_compatible, "composeSha256": hashlib.sha256(compose_bytes).hexdigest(),
                "images": {name: images[name] for name in descriptor["services"]},
                "requiredConfiguration": {name: service["settings"]["required"]
                                          for name, service in descriptor["services"].items() if service["settings"]},
                "versions": {name: {"version": entry["version"], "changedIn": entry["changedIn"]}
                             for name, entry in planned["services"].items()},
                "ports": {name: service["port"] for name, service in descriptor["services"].items()},
                "sites": descriptor["sites"] if second else served}
    if second:
        manifest["routes"] = served
    manifest["needs"] = {name: service["needs"] for name, service in descriptor["services"].items() if service["needs"]}
    # Written only when declared, so a release that declares neither keeps its bundle byte for byte.
    if descriptor.get("dotnetEnvironment"):
        manifest["dotnetEnvironment"] = True
    if descriptor.get("verify"):
        manifest["verify"] = descriptor["verify"]
    if branch:
        manifest["branch"] = branch
    if identity:
        manifest["version"], manifest["channel"] = identity["version"], identity["channel"]
    return compose_bytes, manifest


def write_bundle(output, compose_bytes, manifest, archive=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "compose.json").write_bytes(compose_bytes)
    (output / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if archive:
        with tarfile.open(archive, "w:gz") as package:
            for name in ("release.json", "compose.json"):
                payload = (output / name).read_bytes()
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                package.addfile(member, io.BytesIO(payload))


# ----------------------------------------------------------------------- build

def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True)
    check(result.returncode == 0, "docker " + arguments[0] + " failed: "
          + (result.stderr.strip().splitlines() or ["?"])[-1][:200])
    return result.stdout


def repository_of(descriptor, name, registry):
    return descriptor["services"][name]["image"] or registry.rstrip("/") + "/" + name


def push_local(reference, repository, tag):
    """Publishes an image that already exists locally (a local server, or a CI job that built it itself)."""
    remote = repository + ":" + tag
    docker("tag", reference, remote)
    docker("push", remote)
    digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", remote))
    return next(item for item in digests if item.startswith(repository + "@"))


def cache_flags(cache, scope):
    """Buildx flags for a layer cache: "gha" reads and writes one GitHub Actions cache scope per service. mode=min keeps
    the final image's layers (system packages, runtimes), which rarely change; a failed export never fails the build."""
    if cache != "gha":
        return []
    return ["--cache-from", "type=gha,scope=" + scope,
            "--cache-to", "type=gha,mode=min,scope=" + scope + ",ignore-error=true"]


def build_image(descriptor, name, checkout, repository, tag, app_version=None, vcs_ref=None, cache=None):
    """Builds and pushes one service; `cache` "gha" reads and writes its layers in the GitHub Actions cache."""
    service = descriptor["services"][name]["build"]
    engineering = Path(checkout) / "engineering"
    with tempfile.TemporaryDirectory() as directory:
        metadata = Path(directory) / "metadata.json"
        arguments = ["buildx", "build", "--platform", descriptor["platform"], "--push", "--metadata-file", str(metadata),
                     "-f", str(engineering / service["dockerfile"]), "-t", repository + ":" + tag]
        if service["target"]:
            arguments += ["--target", service["target"]]
        for key, value in sorted(service["args"].items()):
            arguments += ["--build-arg", key + "=" + value]
        if app_version:
            # The build stamps its own version into the backend (-p:Version) and the SPA (__APP_VERSION__).
            arguments += ["--build-arg", "APP_VERSION=" + app_version]
        if vcs_ref:
            # The full commit SHA the image is built from, so the image can report its source commit.
            arguments += ["--build-arg", "VCS_REF=" + vcs_ref]
        for key, path in sorted(service["contexts"].items()):
            arguments += ["--build-context", key + "=" + str(engineering / path)]
        arguments += cache_flags(cache, name)
        docker(*arguments, str(engineering / service["context"]))
        digest = json.loads(metadata.read_text()).get("containerimage.digest", "")
    check(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), name + ": the build reported no image digest")
    return repository + "@" + digest


def move_tag(image, tag):
    """Points `tag` of the image's repository at the image's digest, without pulling or rebuilding it."""
    repository = image.split("@", 1)[0]
    docker("buildx", "imagetools", "create", "--tag", repository + ":" + tag, image)


def build(descriptor, planned, repo, registry, local_images, checkout=None, tag=None, app_version=None, cache=None):
    """Builds and pushes every planned service; returns each service's digest-pinned image."""
    images = {name: entry["image"] for name, entry in planned["services"].items() if not entry["build"]}
    tag = tag or (planned["release"][1:] if planned["kind"] == "release" else planned["release"])
    pending = [name for name, entry in planned["services"].items() if entry["build"]]
    worktree = None
    try:
        for name in pending:
            repository = repository_of(descriptor, name, registry)
            if name in local_images:
                images[name] = push_local(local_images[name], repository, tag)
                continue
            if checkout is None and worktree is None:
                worktree = tempfile.mkdtemp(prefix="wheelhouse-build-")
                git(repo, "worktree", "add", "--detach", worktree, planned["sourceCommit"])
            images[name] = build_image(descriptor, name, checkout or worktree, repository, tag, app_version,
                                       planned["sourceCommit"], cache)
    finally:
        if worktree:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", worktree], capture_output=True)
    return images


# ----------------------------------------------------------------------- notes

NOTES_LIMIT = 100


def release_notes(repo, commit, previous=None, warning=None):
    """The changelog a main release carries above GitHub's generated notes (pull requests and the compare link): the
    commits since the previous release, newest first and one line each, 100 at most; merge commits are left out. A
    first release lists its history up to the limit. A `warning` (a lost base release) leads the notes. Wheelhouse
    shows these bodies before a promotion."""
    revisions = previous + ".." + commit if previous else commit
    if previous:
        check(subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", previous + "^{commit}"],
                             capture_output=True).returncode == 0, "--previous names no commit: " + previous)
    lines = [line for line in git(repo, "log", "--no-merges", "--format=%h%x09%s", "--max-count=" + str(NOTES_LIMIT),
                                  revisions).splitlines() if line]
    total = int(git(repo, "rev-list", "--count", "--no-merges", revisions).strip() or 0)
    notes = ["Built by wow-two-platform.pipelines from " + commit + ".", ""]
    if warning:
        notes += ["**Warning:** " + warning, ""]
    notes.append("## Commits since " + previous if previous else "## Commits")
    notes.append("")
    for line in lines:
        short, _, subject = line.partition("\t")
        notes.append("- " + subject.strip() + " (" + short + ")")
    if not lines:
        notes.append("- No commits since " + previous + "." if previous else "- No commits.")
    if total > len(lines):
        notes.append("- …and " + str(total - len(lines)) + " more")
    return "\n".join(notes) + "\n"


# ------------------------------------------------------------------------- cli

def load(repo, commit, descriptor_path=None):
    text = Path(descriptor_path).read_text() if descriptor_path else git(repo, "show", commit + ":" + DESCRIPTOR)
    return validate(parse(text))


def warned(descriptor):
    """Prints each warning as a GitHub Actions annotation on stderr, leaving stdout to the action's result. `validate`
    prints them, once per build: the publish workflow runs it first."""
    for message in site_warnings(descriptor):
        print("::warning title=deploy.yml::" + message, file=sys.stderr)
    return descriptor


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["validate", "plan", "build", "ci", "notes"])
    parser.add_argument("--repo", default=".", help="the product repository")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--descriptor", help="a deploy.yml on disk instead of the one at the commit")
    parser.add_argument("--base", help="the previous release's bundle folder; without it every service builds")
    parser.add_argument("--tag", help="the release tag (vX.Y.Z); without it the build is a candidate")
    parser.add_argument("--branch", help="the branch the commit was built from")
    parser.add_argument("--main", default="main", help="ci: the branch whose builds are releases")
    parser.add_argument("--platform", choices=PLATFORMS, help="override the descriptor's platform (a local server)")
    parser.add_argument("--registry", help="image prefix, e.g. ghcr.io/owner/repo; each service is <prefix>/<name>")
    parser.add_argument("--image", action="append", default=[], metavar="SERVICE=REF",
                        help="publish an image that already exists locally instead of building the service")
    parser.add_argument("--checkout", action="store_true", help="build from --repo as checked out (CI)")
    parser.add_argument("--rollback-compatible", action="store_true")
    parser.add_argument("--output", help="bundle folder to write")
    parser.add_argument("--archive", help="also write a .tar.gz of the bundle for import")
    parser.add_argument("--previous", help="notes: the previous release's tag; the notes list the commits since it")
    parser.add_argument("--warning", help="notes: a line the notes lead with, the build's base warning")
    parser.add_argument("--cache", choices=["gha"], help="build, ci: read and write image layers in this cache")
    args = parser.parse_args()
    try:
        if args.action == "validate":
            descriptor = warned(validate(parse(Path(args.descriptor or Path(args.repo) / DESCRIPTOR).read_text())))
            print(json.dumps({"product": descriptor["product"], "services": sorted(descriptor["services"])}))
            return 0
        commit = git(args.repo, "rev-parse", "--verify", args.commit + "^{commit}").strip()
        if args.action == "notes":
            sys.stdout.write(release_notes(args.repo, commit, args.previous or None, args.warning or None))
            return 0
        descriptor = load(args.repo, commit, args.descriptor)
        if args.platform:
            descriptor["platform"] = args.platform
        base = json.loads((Path(args.base) / "release.json").read_text()) if args.base else None
        if args.action == "ci":
            return ci(args, commit, descriptor, base)
        planned = plan(descriptor, args.repo, commit, base, args.tag)
        announce(planned)
        if args.action == "plan":
            print(json.dumps(planned, indent=2))
            return 0
        check(args.registry and args.output, "build needs --registry and --output")
        local_images = dict(item.split("=", 1) for item in args.image)
        check(set(local_images) <= set(descriptor["services"]), "--image names an unknown service")
        images = build(descriptor, planned, args.repo, args.registry, local_images, args.repo if args.checkout else None,
                       cache=args.cache)
        compose_bytes, manifest = render(descriptor, planned, images, args.branch, args.rollback_compatible)
        write_bundle(args.output, compose_bytes, manifest, args.archive)
        print(json.dumps({"release": manifest["release"], "kind": manifest["kind"], "output": args.output,
                          "built": [name for name, entry in planned["services"].items() if entry["build"]]}))
        return 0
    except Invalid as error:
        print(json.dumps({"status": "rejected", "reason": str(error)}), file=sys.stderr)
        return 1


def ci(args, commit, descriptor, base):
    """The shared workflow's step: version the build, build and tag the changed services, write the bundle.

    Writes `status`, `kind`, `channel`, `version`, `release` and `archive` to `$GITHUB_OUTPUT` when it is set, and
    `warning` too when the base release is lost (every service rebuilt)."""
    check(args.registry and args.output, "ci needs --registry and --output")
    identity = build_identity(args.repo, commit, args.branch or None, args.main)
    release_tag = identity["release"] if identity["kind"] == "release" else None
    planned = plan(descriptor, args.repo, commit, base, release_tag, identity["version"])
    result = {key: identity[key] for key in ("kind", "channel", "version", "release")}
    announce(planned)
    if planned.get("warning"):
        result["warning"] = planned["warning"]
    # A push that changes no service and no descriptor publishes nothing; a dispatched commit always builds.
    if args.branch and nothing_to_publish(planned):
        result.update(status="skipped", archive="")
    else:
        images = build(descriptor, planned, args.repo, args.registry, {}, args.repo, identity["tag"],
                       identity["version"], getattr(args, "cache", None))
        if identity["moving"]:
            for image in images.values():
                move_tag(image, identity["moving"])
        compose_bytes, manifest = render(descriptor, planned, images, args.branch or None, args.rollback_compatible,
                                         identity)
        archive = descriptor["product"] + "-release.tar.gz" if identity["kind"] == "release" else ""
        write_bundle(args.output, compose_bytes, manifest, archive or None)
        result.update(status="built", archive=archive,
                      built=[name for name, entry in planned["services"].items() if entry["build"]])
    outputs = os.environ.get("GITHUB_OUTPUT")
    if outputs:
        with open(outputs, "a") as stream:
            for key in ("status", "kind", "channel", "version", "release", "archive", "warning"):
                if key in result:
                    stream.write(key + "=" + str(result[key]) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
