#!/usr/bin/env bash
# Rebuild the assertion-guard smoke fixtures from real git history, so each fixture is
# genuine `git diff` output and not hand-typed diff text.
#
#   weakened.diff        every finding type, each planted once or more on purpose
#   clean-refactor.diff  a test renamed and moved between files, assertions added,
#                        reformatting, plus the shapes calibration found legitimate
#
# Usage: bash smoke/assertion-guard/build-fixtures.sh            rewrites both .diff files
#        bash smoke/assertion-guard/build-fixtures.sh --repo DIR  builds the fixture repo in
#        DIR (branches main, weakened, clean) and writes nothing else; check.sh uses this to
#        run the guard in git mode, the way CI runs it, next to the --diff-file run.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_only=""
if [ "${1:-}" = "--repo" ]; then
  repo_only="${2:?usage: build-fixtures.sh --repo DIR}"
fi
if [ -n "$repo_only" ]; then
  mkdir -p "$repo_only"
  work="$(cd "$repo_only" && pwd)"
else
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
fi
cd "$work"
git init -q -b main .
git config user.email fixture@example.invalid
git config user.name fixture

mkdir -p src tests
cat > src/calc.py <<'EOF'
def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    return a / b
EOF
cat > src/parse.py <<'EOF'
def parse(text):
    return int(text)
EOF
cat > tests/test_calc.py <<'EOF'
import pytest

from src.calc import add, divide, subtract


def test_add():
    assert add(1, 2) == 3
    assert add(-1, 1) == 0


def test_subtract():
    assert subtract(5, 3) == 2


def test_divide():
    assert divide(6, 3) == 2
    assert divide(1, 3) == pytest.approx(0.3333, rel=1e-4)


def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)


def test_multiply_table():
    assert add(2, 2) == 4
EOF
cat > tests/test_parse.py <<'EOF'
from src.parse import parse


def test_parse_int():
    assert parse("000042") == 42


def test_parse_negative():
    assert parse("-00001") == -1
EOF
cat > pyproject.toml <<'EOF'
[project]
name = "fixture"
version = "0.0.0"

[tool.coverage.report]
fail_under = 90
EOF
cat > src/math.ts <<'EOF'
export const add = (a: number, b: number): number => a + b;
export const third = (): number => 1 / 3;
EOF
cat > src/math.test.ts <<'EOF'
import { describe, expect, it } from 'vitest';
import { add, third } from './math';

describe('math', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });

  it('computes a third', () => {
    expect(third()).toBeCloseTo(0.3333, 4);
  });
});
EOF
cat > vitest.config.ts <<'EOF'
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    coverage: {
      thresholds: {
        lines: 80,
        branches: 75,
      },
    },
  },
});
EOF
git add -A
git commit -qm base
git branch weakened
git branch clean

# ---------------------------------------------------------------- weakened ------------
git checkout -q weakened
cat > tests/test_calc.py <<'EOF'
import pytest

from src.calc import add, divide, subtract


def test_add():
    assert add(1, 2) == 3


def test_divide():
    assert divide(6, 3) is not None
    assert divide(1, 3) == pytest.approx(0.3333, rel=1e-1)


def test_divide_by_zero():
    with pytest.raises(Exception):
        divide(1, 0)


@pytest.mark.skip(reason="flaky")
def test_multiply_table():
    assert add(2, 2) == 4
EOF
git rm -q tests/test_parse.py
cat > src/math.test.ts <<'EOF'
import { describe, expect, it } from 'vitest';
import { add, third } from './math';

describe('math', () => {
  it.only('adds', () => {
    expect(add(1, 2)).toBeDefined();
  });

  it('computes a third', () => {
    expect(third()).toBeCloseTo(0.3333, 1);
  });
});
EOF
sed -i.bak 's/fail_under = 90/fail_under = 70/' pyproject.toml && rm pyproject.toml.bak
sed -i.bak 's/lines: 80,/lines: 50,/' vitest.config.ts && rm vitest.config.ts.bak
git add -A
git commit -qm weakened

# ---------------------------------------------------------------- clean refactor ------
git checkout -q clean
# test_add is renamed to test_addition AND moved to a new file. test_divide is
# reformatted and gains an assertion. Five parse tests become one parametrized test
# (calibration: mcp-unifi#195). A brand-new test carries a platform skipif (calibration:
# mcp-unifi#156). `assert len(w) == 1` becomes single-element unpacking (mcp-unifi#195).
cat > tests/test_calc.py <<'EOF'
import os

import pytest

from src.calc import divide, subtract


def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(3, 5) == -2


def test_divide():
    assert (
        divide(6, 3)
        == 2
    )
    assert divide(1, 3) == pytest.approx(0.3333, rel=1e-4)
    assert divide(0, 3) == 0


def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_divide_posix_floats():
    assert divide(1.0, 4.0) == 0.25
EOF
cat > tests/test_arith.py <<'EOF'
from src.calc import add


def test_addition():
    assert add(1, 2) == 3
    assert add(-1, 1) == 0


def test_multiply_table():
    assert add(2, 2) == 4
EOF
cat > tests/test_parse.py <<'EOF'
import pytest

from src.parse import parse


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("000042", 42),
        ("-00001", -1),
    ],
)
def test_parse_values(text, expected):
    assert parse(text) == expected


def test_parse_reports_one_value():
    (value,) = [parse("7")]
    assert value == 7
EOF
cat > src/arith.test.ts <<'EOF'
import { expect, it } from 'vitest';
import { add } from './math';

it('adds two numbers', () => {
  expect(add(1, 2)).toBe(3);
  expect(add(2, 2)).toBe(4);
});
EOF
cat > src/math.test.ts <<'EOF'
import { describe, expect, it } from 'vitest';
import { third } from './math';

describe('math', () => {
  it('computes a third', () => {
    expect(third()).toBeCloseTo(0.3333, 4);
  });
});
EOF
# The floor moves from pyproject.toml to .coveragerc at the same value: not a lowering.
cat > pyproject.toml <<'EOF'
[project]
name = "fixture"
version = "0.0.0"
EOF
cat > .coveragerc <<'EOF'
[report]
fail_under = 90
EOF
git add -A
git commit -qm clean

if [ -n "$repo_only" ]; then
  git checkout -q main
  echo "built fixture repo at $work (branches main, weakened, clean)"
  exit 0
fi

git diff --no-color -M main...weakened > "$here/weakened.diff"
git diff --no-color -M main...clean > "$here/clean-refactor.diff"
echo "wrote $here/weakened.diff ($(wc -l < "$here/weakened.diff") lines)"
echo "wrote $here/clean-refactor.diff ($(wc -l < "$here/clean-refactor.diff") lines)"
