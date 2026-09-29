# wow-two-platform.pipelines

## What is this

Shared GitHub Actions for every wow-two product: the reusable `publish` workflow and the release generator
(`generator/release.py`). Public repository; products call it by tag. Usage: `README.md`.

## Key paths

- `.github/workflows/publish.yml` — reusable build-and-publish workflow (`workflow_call`)
- `.github/workflows/test.yml` — this repo's tests
- `generator/release.py` + `generator/test_release.py` — versions, change detection, builds, bundles

## Rules

- Versions, tags and bundles follow `wow-two-ws/conventions/deployment/descriptor/deploy-descriptor.md`.
- Run `python3 -B -m unittest -v test_release` in `generator/` before every commit.
- Release a change as a new `vX.Y.Z` tag; never move a published tag, since products pin them.
- Wheelhouse's runner consumes the bundles: a bundle change needs its contract test there to pass.
- Never print or commit a secret; the workflow runs in public repositories.
- Commit style: `{type}: {past-tense verb} {what}`, subject only.
