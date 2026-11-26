#!/usr/bin/env bash
# PERMANENT CONTROLS for assertion-guard, run by .github/workflows/smoke.yml on every push
# and pull request, and runnable locally: bash smoke/assertion-guard/check.sh
#
#   weakened             MUST exit 1 with exactly the findings in weakened.expected
#                        (a guard that cannot say no is not a guard)
#   clean refactor       MUST exit 0 with zero findings (a guard that fires on a rename
#                        gets ignored, which is the same as no guard)
#
# EVERY fixture runs in BOTH modes and the two must print identical results:
#   --diff-file  on the committed .diff, as an offline replay runs it
#   git mode     on a real repo rebuilt by build-fixtures.sh --repo, as CI runs it
# The first calibration ran only --diff-file while CI ran git mode, and the two modes
# disagreed on deleted test files (review finding F1). A fixture proven in one mode only
# proves nothing about the other.
#
# Then probes.sh (round-one review probes) and cases.py (round-two review cases and the
# rename-rule ratchet): each a real repo, both modes, exact expected findings.
#
# The last line is always a verdict. No verdict line means the control did not run.
set -uo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
guard="$root/assertion-guard/guard.py"
fx="$root/smoke/assertion-guard"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fail=0

bash "$fx/build-fixtures.sh" --repo "$work/repo" >/dev/null || { echo "::error::could not build the fixture repo"; fail=1; }

# run_both <name> <diff file> <head branch>: sets $out and $rc, failing if modes disagree.
run_both() {
  local name="$1" diff_file="$2" branch="$3" out_d rc_d=0 out_g rc_g=0
  out_d="$(python3 -B "$guard" --diff-file "$diff_file" --no-annotations --summary-file '' 2>&1)" || rc_d=$?
  out_g="$(cd "$work/repo" && python3 -B "$guard" --base main --head "$branch" \
             --no-annotations --summary-file '' 2>&1)" || rc_g=$?
  printf '%s\n' "$out_g"
  if [ "$rc_d" != "$rc_g" ] || [ "$out_d" != "$out_g" ]; then
    echo "::error::$name: git mode (exit $rc_g) and --diff-file mode (exit $rc_d) disagree"
    diff <(printf '%s\n' "$out_d") <(printf '%s\n' "$out_g") | sed 's/^/  /'
    fail=1
  else
    echo "  ($name: git mode and --diff-file mode identical, exit $rc_g)"
  fi
  out="$out_g"
  rc="$rc_g"
}

# --- weakened: exact findings, exit 1 ------------------------------------------------
run_both weakened "$fx/weakened.diff" weakened
got="$(printf '%s\n' "$out" | awk -F'\t' 'NF >= 3 { print $1 "\t" $2 }' | sort)"
want="$(grep -v '^#' "$fx/weakened.expected" | grep -v '^$' | sort)"
if [ "$rc" -ne 1 ]; then
  echo "::error::weakened fixture: guard exited $rc, expected 1"
  fail=1
fi
if [ "$got" != "$want" ]; then
  echo "::error::weakened fixture: findings differ from weakened.expected"
  diff <(printf '%s\n' "$want") <(printf '%s\n' "$got") | sed 's/^/  /'
  fail=1
fi
types="$(printf '%s\n' "$got" | cut -f1 | sort -u | tr '\n' ' ')"
for t in net-assertion-loss loosened-assertion skip-added test-deleted coverage-floor-lowered; do
  case " $types" in *" $t "*) ;; *) echo "::error::weakened fixture: no $t finding"; fail=1 ;; esac
done
n_want="$(printf '%s\n' "$want" | grep -c .)"
case "$(printf '%s\n' "$out" | tail -n 1)" in
  "assertion-guard: $n_want finding(s) in "*) ;;
  *) echo "::error::weakened fixture: verdict line does not report $n_want finding(s)"; fail=1 ;;
esac

# --- clean refactor: zero findings, exit 0 -------------------------------------------
run_both clean-refactor "$fx/clean-refactor.diff" clean
if [ "$rc" -ne 0 ]; then
  echo "::error::clean-refactor fixture: guard exited $rc, expected 0 (a rename or move fired)"
  fail=1
fi
case "$(printf '%s\n' "$out" | tail -n 1)" in
  "assertion-guard: 0 finding(s) in "[1-9]*) ;;
  *) echo "::error::clean-refactor fixture: verdict is not '0 finding(s)' over a non-zero file count"; fail=1 ;;
esac

# --- the reviewers' probes and cases --------------------------------------------------
if ! bash "$fx/probes.sh"; then
  fail=1
fi
if ! python3 -B "$fx/cases.py"; then
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "assertion-guard smoke: FAIL"
  exit 1
fi
echo "assertion-guard smoke: PASS (weakened: $n_want expected findings, exit 1; clean refactor: 0 findings, exit 0; both modes identical; probes and cases pass)"
