# Reusable bloom releases

Expose a workflow_call workflow for any GitHub source repository. Inputs identify
its rosdistro repository key, existing GitHub release repository, published X.Y.Z
tag, Git identity, and whitespace-separated distro tracks (default humble jazzy
kilted lyrical rolling). Use the same upstream tag for every selected track.

Run tracks sequentially because they share release repository branches. Continue
other tracks after a failure and fail the job with a per-track summary. Reuse an
existing generated release on retry; do not increment its packaging revision.
Create rosdistro PRs, but do not merge them or claim apt publication.

Use a caller-provided PAT and registered GPG signing key. Sign off and sign all
new commits; verify before pushing and check GitHub verification afterward.
Require existing tracks and initial rosdistro entries; first-time onboarding is
manual. Serialize runs within each calling repository; a release repository must
have one automation owner across callers.

A central reusable workflow plus a checked-out Python helper was chosen over
copying workflow bodies into consumers. The helper ref defaults to main and must
be pinned with the workflow ref when callers need immutable behavior. No caller
or credentials are changed during implementation. Live publication needs caller
secrets and is not part of local verification.
