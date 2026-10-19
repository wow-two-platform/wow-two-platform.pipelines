# wow-two-platform.pipelines

## What is this

Shared GitHub Actions for every wow-two product: the reusable `delivery` workflow (`ci` then `publish`), CodeQL,
Renovate presets, the CI planner (`generator/ci_plan.py`) and the release generator (`generator/release.py`). Public
repository; products call it by tag. Usage: `README.md`.

## Key paths

- `.github/workflows/delivery.yml` — a product's push delivery: `ci.yml`, then `publish.yml` (`workflow_call`)
- `.github/workflows/ci.yml` — shared CI: plan, backend, frontend, browser, gate
- `.github/workflows/publish.yml` — reusable build-and-publish workflow
- `.github/workflows/codeql.yml` — CodeQL for public repositories, and this one's own scan
- `.github/workflows/test.yml` — this repo's tests and actionlint
- `generator/release.py` + `generator/test_release.py` — versions, change detection, builds, bundles
- `generator/ci_plan.py` + `generator/test_ci_plan.py` — superseded pushes, docs-only skips, check selection
- `renovate/` — shared Renovate presets (`renovate-product`, `renovate-private`)

## Rules

- Versions, tags and bundles follow `wow-two-ws/conventions/deployment/descriptor/deploy-descriptor.md`.
- Run `python3 -B -m unittest -v test_release test_ci_plan` in `generator/` before every commit.
- `delivery.yml` calls `ci.yml` and `publish.yml` by `./` path, so a tag carries all three together.
- Release a change as a new `vX.Y.Z` tag; never move a published tag, since products pin them.
- Wheelhouse's runner consumes the bundles: a bundle change needs its contract test there to pass.
- Never print or commit a secret; the workflow runs in public repositories.
- Commit style: `{type}: {past-tense verb} {what}`, subject only.
