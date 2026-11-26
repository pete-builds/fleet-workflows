#!/usr/bin/env bash
# PERMANENT CONTROLS for the fleet halt reader, every outcome of the contract, live over
# the network exactly as a writer reads it. Run by .github/workflows/halt-smoke.yml.
#
# Usage: bash smoke/halt-check/check.sh <raw-base-url>
#   <raw-base-url> is https://raw.githubusercontent.com/pete-builds/fleet-workflows/<sha>
#   for a commit that contains halt-drill/HALT and halt-check/ALIVE. On a pull request
#   those files are not on main yet, so CI passes the head commit's SHA.
#
# The last line is always a verdict. No verdict line means the control did not run.
set -uo pipefail

raw="${1:?usage: check.sh <raw-base-url>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
reader="$root/halt-check/halt-check.sh"
wrapper="$root/halt-check/gha-step.sh"
fail=0
ran=0

# The canonical reader is vendored byte for byte elsewhere and the weekly drill compares
# sha256. Editing it here silently makes every vendored copy drift, so the hash is pinned:
# change it only together with every vendored copy.
want_sha="44f7d6d3acd9ffe81ba6cf0b7b0e9d2104db798d997cda4a7425bee4cb59c984"
got_sha="$( (sha256sum "$reader" 2>/dev/null || shasum -a 256 "$reader") | cut -d' ' -f1)"
if [ "$got_sha" = "$want_sha" ]; then
  echo "PASS canonical reader sha256 $got_sha"
else
  echo "::error::halt-check.sh sha256 is $got_sha, pinned $want_sha: every vendored copy is now drift"
  fail=1
fi

# case <name> <expected exit> <expected line prefix> [VAR=value ...]
case_() {
  local name="$1" want_rc="$2" want_line="$3"
  shift 3
  local out rc=0
  out="$(env "$@" sh "$reader")" || rc=$?
  ran=$((ran + 1))
  local lines
  lines="$(printf '%s\n' "$out" | grep -c .)"
  if [ "$rc" = "$want_rc" ] && [ "$lines" = 1 ] && [ "${out#"$want_line"}" != "$out" ]; then
    echo "PASS $name: exit $rc, $out"
  else
    echo "::error::$name: expected exit $want_rc and one line starting '$want_line', got exit $rc, $lines line(s): $out"
    fail=1
  fi
}

case_ "drill flag reads halted" 3 \
  "halt-check: HALTED: Drill flag: always halted. This is not the real switch." \
  FLEET_HALT_URL="$raw/halt-drill/HALT"
case_ "absent flag with ALIVE marker reads clear" 0 "halt-check: clear" \
  FLEET_HALT_URL="$raw/halt-drill/CLEAR-does-not-exist" \
  FLEET_HALT_ALIVE_URL="$raw/halt-check/ALIVE"
case_ "absent flag with ALIVE missing reads unknown" 4 "halt-check: UNKNOWN: HALT absent but ALIVE marker answered HTTP 404" \
  FLEET_HALT_URL="$raw/halt-drill/CLEAR-does-not-exist" \
  FLEET_HALT_ALIVE_URL="$raw/halt-check/ALIVE-does-not-exist"
case_ "unreachable host reads unknown" 4 "halt-check: UNKNOWN: curl exit" \
  FLEET_HALT_URL="https://127.0.0.1:9/x"

# The Actions wrapper must map each outcome to the right state output and never fail.
wrap() {  # wrap <name> <expected state> [VAR=value ...]
  local name="$1" want_state="$2"
  shift 2
  local outf rc=0 state
  outf="$(mktemp)"
  env GITHUB_OUTPUT="$outf" GITHUB_STEP_SUMMARY=/dev/null "$@" bash "$wrapper" "test" >/dev/null || rc=$?
  ran=$((ran + 1))
  state="$(sed -n 's/^state=//p' "$outf")"
  rm -f "$outf"
  if [ "$rc" = 0 ] && [ "$state" = "$want_state" ]; then
    echo "PASS wrapper $name: state=$state, exit 0"
  else
    echo "::error::wrapper $name: expected state=$want_state exit 0, got state=$state exit $rc"
    fail=1
  fi
}
wrap halted halted FLEET_HALT_URL="$raw/halt-drill/HALT"
wrap clear clear FLEET_HALT_URL="$raw/halt-drill/CLEAR-does-not-exist" FLEET_HALT_ALIVE_URL="$raw/halt-check/ALIVE"
wrap unknown unknown FLEET_HALT_URL="https://127.0.0.1:9/x"

if [ "$fail" -ne 0 ]; then
  echo "halt-check smoke: FAIL ($ran cases run)"
  exit 1
fi
echo "halt-check smoke: PASS ($ran cases run, sha256 pinned)"
