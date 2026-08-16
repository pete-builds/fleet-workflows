# fleet-workflows

Centralized GitHub Actions maintenance logic for the pete-builds fleet. One SHA-pinned
implementation replaces per-repo copies of the same automation, which had become their own
drift problem: automation was needed to keep the automation identical.

## lockfile-recompile (composite action)

Closes the one dependency chore nothing owned: Dependabot's pip updater edits
`requirements.in` and cannot regenerate the hash-pinned `requirements.lock` the published
image installs from. This action regenerates every tracked `*.lock` from the compile
command recorded in each lock's own header, then opens or updates a PR if anything moved.

Caller usage (the whole per-repo file):

```yaml
name: lockfile-recompile
on:
  schedule:
    - cron: '0 20 * * 4'   # Thu, after Dependabot 09:30 ET + cypher's merge cycle land
  workflow_dispatch:
permissions:
  contents: write
  pull-requests: write
concurrency:
  group: lockfile-recompile
  cancel-in-progress: false
jobs:
  recompile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: pete-builds/fleet-workflows/lockfile-recompile@<FULL_COMMIT_SHA> # vX.Y.Z
        with:
          uv-version: '0.12.5'   # bump together with this repo's ci.yml, never alone
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

### The security model, unchanged from the distributed version

- Every run uses the calling repo's built-in `GITHUB_TOKEN`. Blast radius: one repository.
- Callers pin this action to a full commit SHA. Dependabot bumps that SHA per repo as a
  normal workflow-file PR, which the fleet's merge automation refuses to auto-merge, so a
  logic change here still lands through per-repo human review.
- `uv-version` has no default by design. The pin must move together with the same pin in
  the caller's ci.yml; a central default would let a SHA bump silently migrate every
  repo's resolver at once.

### Behavior guarantees (each traceable to a defect found live)

- Compiles to a fresh temp path, never onto the existing lock, which uv would read as
  preferences and keep stale pins while reporting success.
- Compiles twice and requires both fresh resolutions to agree (comments stripped) before
  committing either. If uv itself regresses, this fails closed.
- Restores the real `-o <lock>` path into the header, or the lock would permanently
  reference a temp path that never existed, invisibly, because CI strips comments.
- A lock with no uv header is skipped and named, never guessed at.
- PR lookup filters on `--state open`, or one closed PR would make every future run
  report success while opening nothing.
- Reports a package-level delta in the PR body: a large bump count is a dependency
  upgrade, not a lock sync, and deserves a real review.

## Releasing a change

1. Edit the action.
2. Dispatch the `smoke` workflow. `smoke/requirements.lock` is deliberately stale, so a
   correct action ALWAYS opens or updates a PR on `lockfile-recompile`; the run fails if
   it does not. That is the positive control: a green smoke with no PR can not happen.
   Close the smoke PR after reading it. Never merge it, or the control stops controlling.
3. Tag a release (`vX.Y.Z`). Dependabot in each caller repo picks up the new tag and
   opens per-repo SHA-bump PRs.

## What does not belong here

Anything needing cross-repo credentials. This repo holds logic only; every execution is
per-repo on that repo's own token. The fleet's governing rule lives in the workspace's
`docs/repo-chores.md`.
