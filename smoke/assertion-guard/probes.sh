#!/usr/bin/env bash
# The independent reviewer's fail-open probes for assertion-guard (review of PR #6), kept
# as permanent fixtures. Every probe passed GREEN against the first version of the guard.
# Each builds a real git repo, runs the guard in git mode AND on the same change through
# --diff-file, requires both to agree, and requires the exact findings listed.
#
# Usage: bash smoke/assertion-guard/probes.sh   (called by check.sh)
set -uo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
guard="$root/assertion-guard/guard.py"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fail=0
n=0

new_repo() {
  d="$work/$1"
  mkdir -p "$d" && cd "$d" || exit 1
  git init -q -b main
  git config user.email p@example.invalid
  git config user.name p
}
base() { git add -A && git commit -qm base; }
head_() { git add -A && git commit -qm head; }

# check <name> <expected exit> <expected sorted finding kinds, space separated>
# Optional 4th arg "binary-in-replay": a plain `git diff` shows the test file as binary,
# so the replay must refuse with exit 2 while git mode (which diffs with --text) reads it.
check() {
  local name="$1" want_rc="$2" want_kinds="$3" mode="${4:-}" out_g rc_g=0 out_d rc_d=0 kinds_g
  n=$((n + 1))
  out_g="$(python3 -B "$guard" --base main~1 --head main --no-annotations --summary-file '' 2>&1)" || rc_g=$?
  git diff --no-color -M main~1...main > "$work/$name.diff"
  out_d="$(python3 -B "$guard" --diff-file "$work/$name.diff" --no-annotations --summary-file '' 2>&1)" || rc_d=$?
  kinds_g="$(printf '%s\n' "$out_g" | awk -F'\t' 'NF >= 3 { print $1 }' | sort | tr '\n' ' ' | sed 's/ $//')"
  local ok=1
  [ "$rc_g" = "$want_rc" ] && [ "$kinds_g" = "$want_kinds" ] || ok=0
  if [ "$mode" = binary-in-replay ]; then
    [ "$rc_d" = 2 ] || ok=0
    case "$(printf '%s\n' "$out_d" | tail -n 1)" in "assertion-guard: ERROR:"*) ;; *) ok=0 ;; esac
  else
    [ "$rc_d" = "$rc_g" ] && [ "$out_d" = "$out_g" ] || ok=0
  fi
  if [ "$ok" = 1 ]; then
    echo "PASS probe $name: exit $rc_g [$kinds_g]$([ "$mode" = binary-in-replay ] && echo ", replay refused with exit $rc_d")"
  else
    echo "::error::probe $name: want exit $want_rc [$want_kinds]; git mode exit $rc_g [$kinds_g], replay exit $rc_d"
    printf '%s\n' "$out_g" | sed 's/^/  git: /'
    printf '%s\n' "$out_d" | tail -n 2 | sed 's/^/  replay: /'
    fail=1
  fi
}

# A. whole test file deleted, named for a feature, not a module (F1)
new_repo A; mkdir -p src/app tests
printf 'def login(u, p):\n    return u == "a" and p == "b"\n' > src/app/auth.py
printf 'from app.auth import login\n\ndef test_rejects_bad_password():\n    assert login("a", "x") is False\n\ndef test_accepts_good():\n    assert login("a", "b") is True\n' > tests/test_security.py
base; git rm -q tests/test_security.py; head_
check A-delete-feature-named-test-file 1 "test-deleted"

# B. whole test file deleted, subject is a package (F1)
new_repo B; mkdir -p src/pkg/tools tests
printf 'def run():\n    return 1\n' > src/pkg/tools/__init__.py
printf 'from pkg.tools import run\n\ndef test_run():\n    assert run() == 1\n' > tests/test_tools.py
base; git rm -q tests/test_tools.py; head_
check B-delete-test-for-package 1 "test-deleted"

# C. test file renamed out of every glob (F2)
new_repo C; mkdir -p src tests
printf 'def add(a, b):\n    return a + b\n' > src/calc.py
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_neg():\n    assert add(-1, 1) == 0\n' > tests/test_calc.py
base; mkdir -p attic; git mv tests/test_calc.py attic/calc_checks.py; head_
check C-rename-out-of-glob 1 "test-deleted"

# C2. same, and gut the assertions on the way (F2)
new_repo C2; mkdir -p src tests
printf 'def add(a, b):\n    return a + b\n' > src/calc.py
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_neg():\n    assert add(-1, 1) == 0\n\ndef test_big():\n    assert add(10**6, 1) == 10**6 + 1\n' > tests/test_calc.py
base; mkdir -p attic; git mv tests/test_calc.py attic/calc_checks.py
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_neg():\n    assert add(-1, 1) == 0\n\ndef test_big():\n    add(10**6, 1)\n' > attic/calc_checks.py
head_
check C2-rename-out-of-glob-and-gut 1 "test-deleted"

# D. mocha-style test/ directory, JS, assertion removed (F4)
new_repo D; mkdir -p src test
printf 'exports.add = (a, b) => a + b;\n' > src/add.js
printf 'const assert = require("assert");\nconst { add } = require("../src/add");\ndescribe("add", () => {\n  it("adds", () => {\n    assert.strictEqual(add(1, 2), 3);\n    assert.strictEqual(add(2, 2), 4);\n  });\n});\n' > test/add.js
base
printf 'const assert = require("assert");\nconst { add } = require("../src/add");\ndescribe("add", () => {\n  it("adds", () => {\n    add(1, 2);\n  });\n});\n' > test/add.js
head_
check D-mocha-test-dir 1 "net-assertion-loss"

# E. delete a test; add an unrelated test reusing a generic assertion line (F5)
new_repo E; mkdir -p src tests
printf 'x = 1\n' > src/api.py
printf 'def test_admin_requires_auth(client):\n    r = client.get("/admin")\n    assert r.status_code == 401\n\ndef test_health(client):\n    r = client.get("/health")\n    assert r.status_code == 200\n' > tests/test_api.py
base
printf 'def test_health(client):\n    r = client.get("/health")\n    assert r.status_code == 200\n\ndef test_ping(client):\n    r = client.get("/ping")\n    assert r.status_code == 401\n' > tests/test_api.py
head_
check E-deleted-test-masked-by-generic-assert 1 "test-deleted"

# F. loosenings outside the first exact/weak lists (F6)
new_repo F; mkdir -p src tests
printf 'def f():\n    return 3\n' > src/f.py
printf 'def test_len(items):\n    assert len(items) == 3\n\ndef test_val():\n    assert f() == 3\n' > tests/test_f.py
printf 'test("f", () => {\n  expect(f()).toBe(3);\n  expect(g()).toHaveBeenCalledWith(1, 2);\n});\n' > src/f.test.ts
base
printf 'def test_len(items):\n    assert len(items) >= 1\n\ndef test_val():\n    assert 3 == 3\n' > tests/test_f.py
printf 'test("f", () => {\n  expect(true).toBe(true);\n  expect(g()).toHaveBeenCalled();\n});\n' > src/f.test.ts
head_
check F-loosenings-outside-lists 1 "loosened-assertion loosened-assertion loosened-assertion loosened-assertion"

# G. skip via an imported `mark`, and a class renamed out of collection (F6)
new_repo G; mkdir -p src tests
printf 'def f():\n    return 3\n' > src/f.py
printf 'from pytest import mark\n\n\ndef test_a():\n    assert f() == 3\n\n\nclass TestB:\n    def test_b(self):\n        assert f() == 3\n' > tests/test_f.py
base
printf 'from pytest import mark\n\n\n@mark.skip(reason="flaky")\ndef test_a():\n    assert f() == 3\n\n\nclass B:\n    def test_b(self):\n        assert f() == 3\n' > tests/test_f.py
head_
check G-mark-skip-and-class-rename 1 "skip-added test-deleted"

# I. test under a build/ subpackage (F7)
new_repo I; mkdir -p src/build tests/build
printf 'def mk():\n    return 1\n' > src/build/mk.py
printf 'def test_mk():\n    assert mk() == 1\n\ndef test_mk2():\n    assert mk() + 1 == 2\n' > tests/build/test_mk.py
base
printf 'def test_mk():\n    assert mk() == 1\n' > tests/build/test_mk.py
head_
check I-tests-under-build-dir 1 "test-deleted"

# J. the PR adds `tests/** -diff` to .gitattributes and guts the tests (F3)
new_repo J; mkdir -p src tests
printf 'def add(a, b):\n    return a + b\n' > src/calc.py
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_neg():\n    assert add(-1, 1) == 0\n' > tests/test_calc.py
base
printf 'tests/** -diff\n' > .gitattributes
printf 'from calc import add\n\ndef test_add():\n    pass\n' > tests/test_calc.py
head_
check J-gitattributes-binary-test-diff 1 "net-assertion-loss test-deleted" binary-in-replay

if [ "$fail" -ne 0 ]; then
  echo "assertion-guard probes: FAIL ($n probes run)"
  exit 1
fi
echo "assertion-guard probes: PASS ($n probes run, each in both modes)"
