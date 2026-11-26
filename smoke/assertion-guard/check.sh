#!/usr/bin/env bash
# PERMANENT CONTROLS for assertion-guard, run by .github/workflows/smoke.yml on every push
# and pull request, and runnable locally: bash smoke/assertion-guard/check.sh
#
#   weakened.diff        MUST exit 1 with exactly the findings in weakened.expected
#                        (a guard that cannot say no is not a guard)
#   clean-refactor.diff  MUST exit 0 with zero findings (a guard that fires on a rename
#                        gets ignored, which is the same as no guard)
#
# The last line is always a verdict. No verdict line means the control did not run.
set -uo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
guard="$root/assertion-guard/guard.py"
fx="$root/smoke/assertion-guard"
fail=0

run() {  # run <diff>: prints guard output, sets $rc
  out="$(python3 -B "$guard" --diff-file "$1" --no-annotations --summary-file '' 2>&1)"
  rc=$?
}

# --- weakened: exact findings, exit 1 ------------------------------------------------
run "$fx/weakened.diff"
printf '%s\n' "$out"
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
run "$fx/clean-refactor.diff"
printf '%s\n' "$out"
if [ "$rc" -ne 0 ]; then
  echo "::error::clean-refactor fixture: guard exited $rc, expected 0 (a rename or move fired)"
  fail=1
fi
case "$(printf '%s\n' "$out" | tail -n 1)" in
  "assertion-guard: 0 finding(s) in "[1-9]*) ;;
  *) echo "::error::clean-refactor fixture: verdict is not '0 finding(s)' over a non-zero file count"; fail=1 ;;
esac

if [ "$fail" -ne 0 ]; then
  echo "assertion-guard smoke: FAIL"
  exit 1
fi
echo "assertion-guard smoke: PASS (weakened: $n_want expected findings, exit 1; clean refactor: 0 findings, exit 0)"
