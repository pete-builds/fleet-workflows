#!/usr/bin/env bash
# Detect drift in every uv lockfile: recompile each from the command recorded in its own
# header and diff against what is committed, comments stripped.
#
# FULL RECOMPILE, NOT PIN CONSISTENCY. A check that only verifies the `==` pins in
# requirements.in appear in the lock lets TRANSITIVE dependencies drift arbitrarily far
# while staying green. Measured on this fleet's first recompile run, pin-consistency repos
# had drifted 1 to 35 packages; full-recompile repos, 0 to 2. Only a fresh resolution
# compared against the committed lock bounds the whole tree.
#
# WHY IT COMPILES TO A TEMP PATH
#   `uv pip compile -o <existing lock>` reads that lock as PREFERENCES and keeps old pins,
#   so compiling onto the lock would pass while checking nothing. Same rule as the
#   recompile loop; the two MUST share this shape or they disagree about what "current"
#   means.
#
# WHY THE COMMAND IS READ OUT OF THE LOCK
#   The fleet does not share one compile command (--universal and --python-platform linux
#   are both live, and one repo compiles from pyproject.toml). The header uv writes is the
#   authoritative per-repo answer. A lock with no header is SKIPPED AND NAMED, never
#   guessed at: a guessed command produces a diff against the wrong resolution, which
#   fails healthy repos.
#
# Exit: 0 all locks current, 1 drift found or a real error. Never a silent pass: if no
# lock was checked at all, that is an error, because a drift check that checks nothing is
# indistinguishable from a passing one.

set -euo pipefail

drifted=""
skipped=""
checked=0

for lock in $(git ls-files '*.lock' | sort); do
  cmd="$(sed -n 's/^#[[:space:]]*\(uv pip compile .*\)$/\1/p' "$lock" | head -1)"
  if [ -z "$cmd" ]; then
    skipped="$skipped $lock"
    continue
  fi

  fresh="$(mktemp)"; rm -f "$fresh"
  regen="$(printf '%s' "$cmd" | sed -E "s#-o[[:space:]]+[^[:space:]]+#-o $fresh#")"
  if [ "$regen" = "$cmd" ]; then
    echo "error: could not find the -o target in: $cmd" >&2
    exit 1
  fi

  eval "$regen --quiet"
  # An empty or missing output must never be compared against: the diff below would
  # "pass" while comparing nothing.
  test -s "$fresh"
  checked=$((checked + 1))

  if ! diff -u <(grep -v '^#' "$lock") <(grep -v '^#' "$fresh"); then
    drifted="$drifted $lock"
  fi
done

if [ -n "$skipped" ]; then
  echo "skipped (no uv header, cannot check):${skipped}"
fi

if [ "$checked" -eq 0 ]; then
  echo "::error::no uv lockfile was checked. If this repo has no uv locks, remove this" \
       "workflow; a drift check that checks nothing reads as coverage while providing none."
  exit 1
fi

if [ -n "$drifted" ]; then
  echo "::error::lockfile drift detected:${drifted}"
  echo "::error::Fix: dispatch the lockfile-recompile workflow (gh workflow run" \
       "lockfile-recompile.yml), which recompiles from each lock's own header and opens a PR."
  echo "::error::Or locally: rerun the header command with --upgrade added."
  echo "::error::If this appeared with no dependency change, compare uv versions between" \
       "this check and the recompile loop; they must be pinned to the same version."
  exit 1
fi

echo "all ${checked} lock(s) current"
