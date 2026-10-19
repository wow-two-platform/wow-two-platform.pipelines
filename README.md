# wow-two-platform.pipelines

Shared GitHub Actions for wow-two products: one reusable workflow that versions, builds and publishes a product from
its `engineering/deployment/deploy.yml`, and the release generator behind it.

| Path | What it is |
|---|---|
| `.github/workflows/publish.yml` | The reusable build-and-publish workflow products call |
| `.github/workflows/test.yml` | This repo's own tests |
| `generator/release.py` | The release generator: versions, change detection, builds, bundles |
| `generator/test_release.py` | Its tests, on throwaway Git repositories |

Rules for versions, tags and bundles: `wow-two-ws/conventions/deployment/descriptor/deploy-descriptor.md`.

## Calling it from a product

`.github/workflows/publish-docker-image.yml` in the product is the marker Wheelhouse keys on:

```yaml
name: publish-docker-image

on:
  push:
    branches: ['**']
  workflow_dispatch:
    inputs:
      commit:
        description: The full commit SHA to build
        required: true

permissions:
  contents: write
  packages: write
  actions: read

jobs:
  publish:
    uses: wow-two-platform/wow-two-platform.pipelines/.github/workflows/publish.yml@v0.8.0
    with:
      commit: ${{ inputs.commit }}
```

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

## Running the generator locally

```bash
python3 generator/release.py plan --repo <product> --commit HEAD
python3 generator/release.py notes --repo <product> --commit HEAD --previous v0.4.2
python3 -B -m unittest -v test_release        # from generator/
```
