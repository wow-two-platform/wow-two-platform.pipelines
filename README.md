# wow-two-platform.pipelines

Shared GitHub Actions for wow-two products: one delivery workflow that verifies, versions, builds and publishes a
product from its `engineering/deployment/deploy.yml`, the CI and publish workflows inside it, CodeQL for public
repositories, Renovate presets, and the release generator behind them.

| Path | What it is |
|---|---|
| `.github/workflows/delivery.yml` | A product's whole push delivery: `ci`, then `publish` |
| `.github/workflows/ci.yml` | The shared CI: plan, backend, frontend, browser, gate |
| `.github/workflows/publish.yml` | Versions, builds and publishes the changed services |
| `.github/workflows/codeql.yml` | CodeQL without a build, for public repositories; also scans this one |
| `.github/workflows/test.yml` | This repo's own tests and workflow lint |
| `generator/release.py` | The release generator: versions, change detection, builds, bundles |
| `generator/ci_plan.py` | The CI planner: superseded pushes, docs-only skips, which checks run |
| `generator/test_release.py`, `generator/test_ci_plan.py` | Their tests, on throwaway Git repositories and a fake GitHub |
| `renovate/renovate-product.json`, `renovate/renovate-private.json` | Shared Renovate presets |

Rules for versions, tags and bundles: `wow-two-ws/conventions/deployment/descriptor/deploy-descriptor.md`.

## Calling it from a product

`.github/workflows/publish-docker-image.yml` is the product's one workflow and the marker Wheelhouse keys on. It
declares the product's checks; everything else is shared:

```yaml
name: publish-docker-image

on:
  push:
    branches: ['**', '!renovate/**']
  pull_request:
  workflow_dispatch:
    inputs:
      commit:
        description: The full commit SHA to build
        required: true

permissions:
  contents: write
  packages: write
  actions: read

concurrency:
  group: delivery-${{ github.repository }}-${{ inputs.commit || github.ref }}
  # main queues, so each release reads the tag the previous one created; other refs cancel a superseded run.
  cancel-in-progress: ${{ github.event_name == 'pull_request' || (github.event_name == 'push' && github.ref != 'refs/heads/main') }}
  queue: max

jobs:
  delivery:
    uses: wow-two-platform/wow-two-platform.pipelines/.github/workflows/delivery.yml@v0.10.0
    with:
      commit: ${{ inputs.commit }}
      backends: |
        [{"name": "Backend", "dotnet": "engineering/codebase/pilot.backend-services/global.json",
          "node": "engineering/codebase/pilot.frontend-services/.nvmrc",
          "run": "dotnet test engineering/codebase/pilot.backend-services/Pilot.BackendServices.slnx"}]
      frontends: |
        [{"name": "Frontend", "dir": "engineering/codebase/pilot.frontend-services",
          "scripts": ["lint", "typecheck", "test", "build"]}]
```

A push runs `current → plan → ci → publish`:

- **current:** a `main` push that a newer push already replaced verifies and publishes nothing; its gate is skipped.
- **plan:** reads the paths changed since the last delivered commit (the newest successful push run on the branch
  whose gate passed). Docs, planning and markdown alone (`**/*.md`, `docs/**`, `product/**`,
  `engineering/planning/**`, `engineering/research/**`, `.claude/**`, plus the `docs` input) build, test and publish
  nothing, and the gate still passes. A dispatched commit, a first run or an unreadable answer verifies everything.
- **ci:** each check with `paths` runs when one of them changed; one without runs on every code change; a changed path
  no check owns (a workflow, a root file) runs every check.
- **publish:** after a passing `ci`, on pushes and dispatches, never on a pull request.
- A product that deploys itself adds a job after `delivery` with
  `if: needs.delivery.outputs.published == 'true'`; Wheelhouse's `deploy-dev` is the one case.

## Shared CI

`ci.yml` takes its checks as JSON strings. Paths are relative to the repository root; `run` and `setup` are a string or
a list of lines, run with `bash -eo pipefail`.

| Input | Keys |
|---|---|
| `backends` (list) | `name`, `run`; `dir` (`.`), `dotnet` (a `global.json` or an SDK version), `node` (an `.nvmrc`; adds pnpm), `pnpm-lock` (beside the `.nvmrc`), `python`, `pip` (install arguments), `pip-cache` (requirements glob), `lfs`, `history` (full clone), `pipelines` (the generator at this tag in `$PIPELINES_DIR`), `paths`, `timeout` (30) |
| `frontends` (list) | `name`, `dir`; `scripts` (`lint typecheck test build`), `node` (`<dir>/.nvmrc`), `lfs`, `paths`, `timeout` (20) |
| `browser` (object) | `name`, `dir`, `run` (gets `$SHARD`, `$SHARDS`); `shards` (1), `setup`, `dotnet`, `node`, `lfs`, `artifact` (kept on failure), `paths`, `timeout` (30) |
| `docs` (list) | extra globs that never build |

- `lfs` is `true` (every LFS file) or `git lfs pull --include` patterns, such as `engineering/codebase/**`; without it
  a check fetches none.
- Every .NET job restores a NuGet cache keyed on its lockfiles and project files; every Node job a pnpm store cache.
- The `gate` job (`ci`'s one result) passes when every selected check passed; it is skipped only on a superseded push.
- Every job has a timeout; every action is pinned to a commit.

## Publish

| Built from | Version | Image tags | Bundle |
|---|---|---|---|
| `main` | `X.Y.Z` | `X.Y.Z`, `latest` | GitHub release `vX.Y.Z` asset `<product>-release.tar.gz` |
| any other branch | `X.Y.(Z+1)-<branch>.<n>` | `sha-<commit>` | Actions artifact `bundle-<commit>` |
| a dispatched commit | `X.Y.(Z+1)-g<commit>` | `sha-<commit>` | Actions artifact `bundle-<commit>` |

- `X.Y` is the newest `engineering/planning/version-track/v{X.Y}/` folder; `Z` follows the highest `vX.Y.*` tag.
- A push that changes no service and not the descriptor publishes nothing.
- Each image build receives `--build-arg APP_VERSION=<version>`; Dockerfiles pass it to `-p:Version` and the SPA.
- Each image build also receives `--build-arg VCS_REF=<full commit SHA>`, the commit it was built from.
- The checkout fetches the Git LFS files under `engineering/`, the only folder build contexts can reach, so images
  carry real assets instead of pointer files; a repository without LFS fetches nothing.
- A `main` release's notes list the commits since the previous release (`release.py notes`, 100 at most, merges left
  out), then GitHub's generated notes from that release on (merged pull requests and the compare link). Wheelhouse
  shows them before a promotion.
- Products pin a tag of this repository; a change here ships as a new tag, never by moving one.
- From `v0.5.0` a descriptor may set `dotnetEnvironment: true` (the runner runs dev as .NET `Development`, prod as
  `Production`) and declare a `verify` dev check (`service`, `command`, `timeout`); each reaches `release.json` only
  when declared, so other bundles stay byte for byte the same. An older tag refuses both keys.
- From `v0.6.0` a service may declare `dependsOn: [<service>, …]`, other services of the same product (platform
  services stay under `needs`); an unknown name or a cycle is refused. It renders as Compose
  `depends_on: { <service>: { condition: service_healthy } }`, never `restart: true`, so Wheelhouse starts a release in
  dependency order and a dependency's update never restarts its dependents. A bundle that declares none is byte for
  byte the same; an older tag refuses the key.
- From `v0.7.0` a service's settings file mounts at `/app/appsettings.Target.json` unless `settings.file` names
  another path; older tags mount it at `/app/appsettings.Local.json`, which is now a developer machine's own file. The be
  sdk reads the target's file (`AddSettingsFiles`, called by `AddApiDefaults`) and, for one release window, a legacy
  `appsettings.Local.json` on a deployed host without it. Move the pin together with that be sdk.
- From `v0.8.0` a descriptor may be `descriptor: 2`: the product lists its `sites` (host roles with `exposure`,
  `primary` and `environments`) at the top, and each service lists the `paths` it serves per site. The generator
  refuses an undeclared site, a site no service serves, a second claim on one site path (one service serves `/` per
  site) and a path other than `/` or `/a/b`; `validate` warns about a site without `/`. A descriptor 2 bundle writes
  `release.json` schema 2 (`sites` the roles, `routes` each service's ports and paths); Wheelhouse's runner routes it
  by host and path, longest prefix first. Descriptor 1 is deprecated and keeps its schema 1 bundles byte for byte;
  an older tag refuses descriptor 2.
- From `v0.8.1` a base release whose `sourceCommit` is not in the repository's history (an author rewrite removed it)
  no longer fails the build with `git diff failed: fatal: bad object`. The checkout is full (`fetch-depth: 0`) and a
  shallow one is deepened; a commit still missing, sharing no history with the build, or failing the diff is lost. A
  lost base is not used: every service rebuilds, carries the build's version and `changedIn` the new release, and no
  image comes from the base. The job log gets a `::warning` annotation and the release notes lead with
  `**Warning:** Base vX.Y.Z's source commit (<sha>) is not in this history; every service was rebuilt.` A base that
  is in the history plans and bundles byte for byte as before.
- From `v0.9.0` products call `delivery.yml` instead of `publish.yml` and drop their own `ci.yml` or `verify.yml`.
  Publish builds through the GitHub Actions layer cache (`release.py ci --cache gha`, `mode=min`, one scope per
  service; a failed export never fails the build) and outputs `status`, `kind` and `release`. Bundles are byte for
  byte as before.
- From `v0.9.1` every action in the shared workflows runs on Node 24 (checkout 7, setup-node 7, setup-dotnet 6,
  setup-python 7, cache 6, upload-artifact 7, pnpm/action-setup 6, docker buildx 4, login 4). Behavior is unchanged.
- From `v0.10.0` a service may type its setting keys under `settings.keys`: each `type` (`text`, `number`, `boolean`,
  `choice` with `values`, `url`, `cron`, `date` with a `format`, `email`), `required` or `secret`, and a `default` for
  an optional plain key, checked against its type. Required keys join `settings.required`; the bundle's
  `release.json` carries the rest under `settings.<service>` (`optional`, `secrets`, `allowed`, `types`), which
  Wheelhouse's runner reads and its console checks saved values against. A bundle without typed keys is byte for byte
  as before; an older tag refuses the key, and a runner older than Wheelhouse v0.5 refuses `types`.

## Security checks

- **CodeQL:** a public repository adds `.github/workflows/codeql.yml` calling `codeql.yml@v0.10.0` with its
  `languages`, on `main` pushes that change more than markdown and weekly. It runs beside delivery, never inside it,
  and needs `security-events: write`. Private repositories on the Free plan get no code scanning; add nothing there.
- **Renovate:** each repository's `.github/renovate.json` extends
  `github>wow-two-platform/wow-two-platform.pipelines//renovate/renovate-product` (public) or `…/renovate-private`
  (private: nothing is branched until the owner ticks it in the Dependency Dashboard, so no Actions minutes). Mend's
  hosted Renovate app runs outside Actions; delivery ignores `renovate/**` pushes and verifies their pull requests.
  Updates come weekly and grouped, wait 3 days after release, and keep commits as `chore: updated <dep> …`; the be
  sdk, the fe sdk and this repository's tags skip the wait.

## Running the generator locally

```bash
python3 generator/release.py plan --repo <product> --commit HEAD
python3 generator/release.py notes --repo <product> --commit HEAD --previous v0.4.2
python3 -B -m unittest -v test_release test_ci_plan   # from generator/
```
