# Shared GitHub workflows

## Release workflow

`.github/workflows/release.yml` validates `Bump X.Y.Z` pull requests and publishes
GitHub Releases when those PRs merge. It operates on the **calling repository**,
using its PR event and `GITHUB_TOKEN`; no personal access token is required.

Add this caller to `.github/workflows/release.yml` in each repository:

```yaml
name: Release

on:
  pull_request:
    branches: [jazzy]
    types: [opened, synchronize, edited, reopened, ready_for_review, closed]

permissions:
  contents: read

jobs:
  release:
    permissions:
      contents: write
    uses: ROBOTIS-GIT/github-workflows/.github/workflows/release.yml@main
    with:
      source_branch: main
      target_branch: jazzy
```

The branch inputs default to `main` and `jazzy`. When changing `target_branch`,
also update the caller's `pull_request.branches` filter. Keep all event types so
changes to PR titles, descriptions, and commits trigger validation.

Every same-repository PR from `source_branch` to `target_branch` is validated,
regardless of its title. Validation requires a strict `Bump X.Y.Z` title, a version greater than the latest
published release, no existing tag or release for that version, and a description
containing more than whitespace or HTML comments. A missing or malformed title
fails validation instead of skipping it. PRs from other branch pairs or forks pass without creating
a release. Versions are stable three-part numbers; prerelease suffixes are not
supported. The latest release tag may have a `v` prefix, but new tags use `X.Y.Z`.

On merge, validation runs again before publishing a release at the PR's merge
commit, with the PR description as its release notes. Existing releases are not
overwritten; rerunning after publication fails duplicate/version validation.
The validation job has read-only permissions; only publication has write access.
This publishes GitHub Releases, not ROS packages.

## Rollout and updates

1. Publish this reusable workflow to this repository's `main` branch first.
2. Ensure repository/organization Actions settings permit callers to use it. If
   this repository is private, configure its Actions access policy and confirm
   the caller is eligible to access private reusable workflows.
3. Add the caller workflow to each consuming repository and its target branch.
4. After its first run, select the reusable workflow's validation check in the
   target branch's required status checks. Its displayed name may include the
   caller job prefix; replace any previous standalone validation check selection.

Callers reference `@main`, so updates merged here apply to their **subsequent
workflow runs** without editing every repository. Updating this repository does
not itself trigger releases in callers. Review and test central changes before
merging because all callers share this reference. Pinning a commit SHA is an
alternative when a repository needs fixed behavior instead of automatic updates.

## Docker Hub publishing workflow

`.github/workflows/docker-publish.yml` builds the calling repository on native
AMD64 (`ubuntu-24.04`) and ARM64 (`ubuntu-24.04-arm`) runners. It pushes
`amd64-X.Y.Z` and `arm64-X.Y.Z`, then combines the exact build digests into
`X.Y.Z`. Both builds must succeed before the shared version tag is published.
It verifies that the resulting index contains both Linux architectures.

The `version` must identify an existing, published, non-prerelease GitHub Release
in the calling repository. `latest` is updated only when that version is still
the repository's latest GitHub Release at publication time. Set `publish_latest:
false` to publish only the version tags. Failed builds may leave architecture
tags in Docker Hub, but do not update the shared version or `latest` tags.

### Inputs and credentials

| Input | Default | Purpose |
| --- | --- | --- |
| `image` | Required | Docker Hub `namespace/repository`, without a tag |
| `version` | Required | Published release tag, strictly `X.Y.Z` |
| `checkout_ref` | `jazzy` | Branch, tag, or commit of the calling repository |
| `context` | `.` | Build context relative to checkout |
| `dockerfile` | `Dockerfile` | Dockerfile relative to checkout, not context |
| `dockerfile_amd64` | Empty | Optional override for AMD64 |
| `dockerfile_arm64` | Empty | Optional override for ARM64 |
| `build_args` | Empty | Newline-separated non-secret build arguments |
| `no_cache` | `true` | Disable build layer cache |
| `publish_latest` | `true` | Allow latest-tag publication |

Pass `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` explicitly as workflow secrets.
The token needs push access to the requested Docker Hub repository. Organization
secrets must be made available to each calling repository; storing secrets only
in this shared-workflows repository does not make them available to callers.
The caller needs only `contents: read` for Docker publishing.

The checkout ref is resolved once and reused by both build jobs. Dockerfiles
that clone branches internally retain their existing behavior. No ORIN, SSH
credentials, QEMU, or GitHub personal access token is required.

### Manual caller for an existing release

For `robotis_applications`, save this as a workflow in the calling repository
and run it with a published version. It also provides a way to retry Docker
publishing without rerunning release creation.

```yaml
name: Publish Docker image
on:
  workflow_dispatch:
    inputs:
      version:
        description: Existing release version (X.Y.Z)
        required: true
        type: string
permissions:
  contents: read
jobs:
  docker-publish:
    uses: ROBOTIS-GIT/github-workflows/.github/workflows/docker-publish.yml@main
    with:
      image: robotis/robotis-applications
      version: ${{ inputs.version }}
      checkout_ref: jazzy
      context: ./docker
      dockerfile: ./docker/Dockerfile
    secrets:
      DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
      DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
```

### Automatic release integration

The shared `release.yml` exposes `published` and `version` outputs only after
successful release creation. Skipped publication leaves both outputs empty.
Add the Docker caller job to the same caller workflow with `needs: release`, a condition of
`needs.release.outputs.published == 'true'`, and version from
`needs.release.outputs.version`. Do not rely on `release: published` to chain
from releases created with `GITHUB_TOKEN`.

Publication is serialized per image within the calling repository. GitHub
concurrency retains at most one running and one pending run; a newer queued
run can replace an older pending one. Use the manual caller to recover skipped
versions. Assign each Docker Hub image to one calling repository because this
lock does not coordinate different repositories. Reruns can overwrite image tags.
`latest` selection follows GitHub's latest-release designation, not a separate
Docker Hub version comparison.

Setting `no_cache: false` permits builder-local cache use but does not configure
persistent caching across hosted runner jobs. Initial builds should verify disk
usage and duration against hosted runner limits. Live builds and Docker Hub
publishing require running this workflow in a configured calling repository.

## Discord release notifications

`.github/workflows/discord-notify.yml` posts an existing GitHub Release's notes
and link to a Discord channel through an incoming webhook. The card title
includes the calling repository's name so multiple repositories can share a
channel. Draft releases are rejected. No checkout, Discord bot, or PAT is needed.

Create a Discord incoming webhook for the destination channel, then configure
`DISCORD_WEBHOOK_URL` as an Actions repository secret or organization secret
available to each calling repository. Secrets stored only in this shared-workflows
repository are not available to callers. Treat the webhook URL as a credential.

### Automatic release integration

Publish this reusable workflow to this repository's `main` branch before adding
callers. In each repository's existing release caller, add this job alongside
`jobs.release` (the release caller example above uses that job ID):

```yaml
  discord-notify:
    needs: release
    if: needs.release.outputs.published == 'true'
    permissions:
      contents: read
    uses: ROBOTIS-GIT/github-workflows/.github/workflows/discord-notify.yml@main
    with:
      version: ${{ needs.release.outputs.version }}
    secrets:
      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

This posts only after successful release creation and runs independently of
Docker publishing. Do not use a separate `release: published` event to chain
from the current release workflow: releases created with `GITHUB_TOKEN` do not
trigger it. Notification failure fails the notification job; the published
GitHub Release remains available.

### Manual posting or retry

For an existing release, save this caller as a separate workflow on the calling
repository's default branch, then run it with the release tag. This retries
notification without recreating the release (rerunning release creation fails
its duplicate/version validation).

```yaml
name: Notify Discord
on:
  workflow_dispatch:
    inputs:
      version:
        description: Existing published release tag
        required: true
        type: string
permissions:
  contents: read
jobs:
  discord-notify:
    uses: ROBOTIS-GIT/github-workflows/.github/workflows/discord-notify.yml@main
    with:
      version: ${{ inputs.version }}
    secrets:
      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

Long notes are truncated to 1,900 characters plus a link reminder; the title
is limited to 120 characters. These conservative limits accommodate emoji
within Discord embed limits. Empty notes show a link reminder. Mentions are
disabled. Repeated calls post new messages, so check the channel before retrying
a request whose result is uncertain. The workflow does not retry automatically.

Local payload checks: `python3 -m unittest discover -s tests -v` (requires
Python 3, PyYAML, and jq). Live delivery requires a configured calling repository.
