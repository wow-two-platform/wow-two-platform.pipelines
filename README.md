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
    uses: wow-two-platform/wow-two-platform.pipelines/.github/workflows/publish.yml@v0.3.0
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
- Products pin a tag of this repository; a change here ships as a new tag, never by moving one.

## Running the generator locally

```bash
python3 generator/release.py plan --repo <product> --commit HEAD
python3 -B -m unittest -v test_release        # from generator/
```
