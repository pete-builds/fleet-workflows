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

## lockfile-drift (composite action)

The paired detection half of `lockfile-recompile`: recompiles every uv lock from the
command in its own header and fails if the committed lock differs (comments stripped).
Full recompile, not pin consistency, so transitive dependencies cannot drift silently:
measured on this fleet's first recompile run, pin-consistency repos had drifted 1 to 35
packages while full-recompile repos sat at 0 to 2. Read-only; needs no token.

Caller usage (the whole per-repo file):

```yaml
name: lockfile-drift
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: pete-builds/fleet-workflows/lockfile-drift@<FULL_COMMIT_SHA> # vX.Y.Z
        with:
          uv-version: '0.12.5'   # must match the lockfile-recompile caller's pin
```

The check and the loop must resolve identically: same fresh-temp-path compile shape, same
header-derived command, same pinned uv. That agreement being structural (both read the
lock's own header and compile fresh) rather than remembered is the point of sharing the
implementation.

## assertion-guard (composite action)

Goes red when a pull request makes its tests easier to pass. An agent told to turn a red
suite green can fix the code or make the tests ask for less, and CI cannot tell those
apart. This reads the diff of the tests themselves (Python, and TypeScript/JavaScript)
plus the config files that hold coverage floors, and reports:

| Finding | Meaning |
|---|---|
| `net-assertion-loss` | a test file that still exists loses more assertions than it gains |
| `loosened-assertion` | an exact check became a weaker one: `==` to `is not None` or `>=`, `toEqual` to `toBeDefined`, `toHaveBeenCalledWith` to `toHaveBeenCalled`, a specific error to `Exception`, a wider `approx` tolerance, fewer `toBeCloseTo` digits; or an added check over constants only (`assert 3 == 3`) |
| `skip-added` | a skip or xfail on an existing test, or a `.only` anywhere |
| `test-deleted` | a test removed and not re-added anywhere in the diff (renames, moves and parametrize consolidations are matched); a whole test file deleted, unless the source file it is named for is deleted in the same diff; a test file renamed, or a `class Test*` renamed, to where the runner no longer collects it |
| `coverage-floor-lowered` | `fail_under`, `--cov-fail-under` or a vitest/jest threshold lowered or removed |

Test files are Python under `tests/` or `test/`, `test_*.py`, `*_test.py`, `conftest.py`, and
TS/JS under `__tests__/`, `test/`, `tests/`, `*.test.*`, `*.spec.*`. In git mode the diff is
read with `--text --no-textconv`, so a `.gitattributes` line cannot turn test changes into
"Binary files differ"; a replayed diff that shows a test file as binary is an error.

Each finding is an `::error` annotation on the line, a row in the step summary, and exit 1.
The last line is always a verdict, including when no test file changed:
`assertion-guard: N finding(s) in M test file(s) scanned`. A run that could not read the
diff prints `assertion-guard: ERROR: ...` and exits 2, never a clean pass. There is no
override label or keyword: a red guard is meant to be read by the reviewer, who merges past
it only with a written reason.

Caller usage (the whole per-repo file):

```yaml
name: assertion-guard
on:
  pull_request:
permissions:
  contents: read
jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: pete-builds/fleet-workflows/assertion-guard@<FULL_COMMIT_SHA> # vX.Y.Z
```

Offline replay of any diff: `gh pr diff 123 > pr.diff && python3 assertion-guard/guard.py --diff-file pr.diff`.

Controls, on every push and pull request (`smoke.yml`): the unit tests, a planted
`smoke/assertion-guard/weakened.diff` that must produce exactly the findings in
`weakened.expected`, a `clean-refactor.diff` (a test renamed and moved, assertions added,
reformatting) that must produce none, the independent reviewer's probes
(`smoke/assertion-guard/probes.sh`), and the action itself run end to end on two planted
commit pairs that must each end with exit 1 and `test-deleted`. Every fixture and probe runs
in BOTH modes, git mode on a real repo and `--diff-file` on the same change, and the two
must print identical results. `smoke/assertion-guard/build-fixtures.sh` regenerates the
fixtures from real git history.

The action's outputs are `exit-code` (0, 1 or 2), `verdict` and `kinds`.

## Releasing a change

1. Edit the action.
2. Dispatch the `smoke` workflow. `smoke/requirements.lock` is deliberately stale, so a
   correct action ALWAYS opens or updates a PR on `lockfile-recompile`; the run fails if
   it does not. That is the positive control: a green smoke with no PR can not happen.
   Close the smoke PR after reading it. Never merge it, or the control stops controlling.
3. Tag a release (`vX.Y.Z`). Dependabot in each caller repo picks up the new tag and
   opens per-repo SHA-bump PRs.

## Halt switch

One flag that stops every unattended writer in the fleet at once.

**Set it:** create a file named `HALT` at the root of `main` in this repo. Its first line is
the reason every writer prints. One commit from any device, the GitHub app included.

**Clear it:** delete `HALT` from `main`.

**How fast:** writers read `https://raw.githubusercontent.com/pete-builds/fleet-workflows/main/HALT`,
and GitHub's raw CDN caches for up to about 5 minutes. A halt takes effect within 5
minutes, and so does clearing it. That is accepted for jobs that run hourly or nightly.

**The contract** (`halt-check/halt-check.sh`, one POSIX script, prints exactly one line):

| Reader sees | Line | Exit | Writers |
|---|---|---|---|
| `HALT` answers 404 and `halt-check/ALIVE` answers 200 | `halt-check: clear` | 0 | proceed |
| `HALT` answers 200 | `halt-check: HALTED: <first line of HALT>` | 3 | write nothing |
| anything else (timeout, 5xx, DNS, `ALIVE` missing, no curl) | `halt-check: UNKNOWN: <detail>` | 4 | write nothing |

Unattended writers proceed only on exit 0, so an unreadable switch fails closed. A 404 only
counts as clear while the `ALIVE` marker answers, because a renamed, deleted or private
repo would also 404. **Do not delete `halt-check/ALIVE`.** Interactive Claude sessions deny
writes on 3 and warn on 4, because Pete is present.

`halt-check.sh` is canonical here and copied byte for byte wherever a tier cannot call the
action; the weekly drill compares every copy's sha256 with this one, and `halt-smoke` pins
the hash, so changing it means updating every copy together.

**In a workflow:**

```yaml
      - uses: pete-builds/fleet-workflows/halt-check@<FULL_COMMIT_SHA> # vX.Y.Z
        # fails the job unless clear; outputs: state (clear|halted|unknown), line
```

Set `fail-unless-clear: 'false'` to only report and act on `steps.<id>.outputs.state`
yourself. `lockfile-recompile` reads the switch right before its only write: when not
clear it pushes nothing, opens nothing, writes the reason to the step summary and a notice,
and ends green.

**Drills** never touch the real flag. `FLEET_HALT_URL` replaces the flag URL and
`FLEET_HALT_ALIVE_URL` the marker URL. `halt-drill/HALT` always exists (reads halted);
`halt-drill/CLEAR-does-not-exist` never does (reads clear while `ALIVE` answers). The
`halt-smoke` workflow runs every outcome on every push and pull request, including a
recompile that must write nothing when halted and must reach its write step when clear.

## What does not belong here

Anything needing cross-repo credentials. This repo holds logic only; every execution is
per-repo on that repo's own token. The fleet's governing rule lives in the workspace's
`docs/repo-chores.md`.
