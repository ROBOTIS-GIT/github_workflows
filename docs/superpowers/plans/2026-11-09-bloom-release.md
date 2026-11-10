# Reusable Bloom Release Implementation Plan

> **For agentic workers:** Use inline execution for this cohesive change.

**Goal:** Reusable, signed multi-distro bloom publication from a GitHub Release.
**Architecture:** workflow_call installs bloom and signing credentials; a Python
helper generates release branches locally and verifies atomic pushes. A Git hook
also guards bloom's rosdistro PR pushes.
**Tech Stack:** GitHub Actions, Python stdlib, PyYAML, bloom, Git, GPG, gh.
**Spec:** docs/superpowers/specs/2026-11-09-bloom-release-design.md

## Global Constraints

Existing tracks only; strict X.Y.Z; same tag in all tracks; no unsigned fallback.
No automatic live publication while implementing. Keep credentials out of URLs
and generated files in the checkout. Do not change existing release behavior.

## Review Focus

Version mismatch, duplicate release retries, partial generation, missing tracks,
and unsigned or missing-DCO pushes must be exercised locally.

## Task 1: Runner and signed push guard

- [x] Write failing tests for input validation, track pinning and retry selection,
  unsigned/missing-DCO rejection, and local Git hook behavior.
- [x] Run tests and observe failure before implementing scripts/bloom_release.py.
- [x] Implement generation, atomic release push, verification, PR-only retry and
  per-distro result collection. Run focused tests.

## Task 2: Workflow and consumer documentation

- [x] Add .github/workflows/bloom-release.yml with explicit inputs/secrets and cleanup.
- [x] Add automated-release and manual-retry caller examples to README.md.
- [x] Run the full unittest suite, YAML/shell parsing and actionlint if available.
- [x] Review the diff for credentials, quoting, retry and signing failure behavior.

## Verification record

17 unittest checks and actionlint pass. The real push guard accepted the existing registered-key
signed DynamixelSDK commit d929333 in an isolated local object fixture; no network
push was performed. Independent review identified import-tag leases, intentional
package removals, HTTPS normalization and older-upstream retry metadata; fixes and
regression checks are included. Live generation and publication remain untested
without a configured Actions caller.
