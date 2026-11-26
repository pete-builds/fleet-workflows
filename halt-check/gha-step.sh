#!/usr/bin/env bash
# gha-step.sh: run halt-check.sh inside a GitHub Actions step and publish its verdict.
#
# This wrapper is for Actions only. halt-check.sh is the canonical reader that other tiers
# vendor byte for byte; this file is not vendored anywhere.
#
# It never fails the step. It writes `state` (clear, halted or unknown) and `line` to
# $GITHUB_OUTPUT, prints the line, and when the switch is not clear adds a ::notice:: and
# a step-summary section saying what the caller does instead. Each caller decides what
# "not clear" means for it: halt-check/action.yml fails, lockfile-recompile skips writing.
#
# Usage: gha-step.sh "<what this run does instead when not clear>"
set -uo pipefail

consequence="${1:-}"
here="$(cd "$(dirname "$0")" && pwd)"

status=0
out="$(sh "$here/halt-check.sh")" || status=$?
line="$(printf '%s\n' "$out" | head -n 1)"

# Only the exact contract lines count. Exit 0 with any other text is a reader that broke
# its own contract, and that reads as UNKNOWN: fail closed.
case "$status:$line" in
  "0:halt-check: clear") state=clear ;;
  "3:halt-check: HALTED: "*) state=halted ;;
  *)
    state=unknown
    [ -n "$line" ] || line="halt-check: UNKNOWN: the reader printed nothing (exit $status)"
    ;;
esac

echo "$line"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  { echo "state=$state"; echo "line=$line"; } >> "$GITHUB_OUTPUT"
fi

if [ "$state" = clear ]; then
  exit 0
fi

msg="$line"
[ -z "$consequence" ] || msg="$line. $consequence"
echo "::notice title=fleet halt switch::$(printf '%s' "$msg" | sed 's/%/%25/g')"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  # shellcheck disable=SC2016  # the backticks are literal Markdown, not a command
  printf '### Fleet halt switch: %s\n\n`%s`\n\n%s\n\n' "$state" "$line" "$consequence" \
    >> "$GITHUB_STEP_SUMMARY"
fi
exit 0
