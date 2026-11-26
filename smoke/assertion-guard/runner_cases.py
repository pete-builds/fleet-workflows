#!/usr/bin/env python3
"""Collection-diff fixtures for assertion-guard: the REAL runner, pinned.

Each case is a real git repo. The guard runs in git mode with --collect-command (pytest
9.2.0 from smoke/assertion-guard/runner-py, or vitest 3.2.4 from runner-js), then the
same change is replayed with --diff-file plus the recorded --base-ids/--head-ids, and the
two must print identically. Three independent checks per case:

  1. the guard's lost IDs equal what this script itself collects with the same runner at
     the base and the head (run in the case repo, not in the guard's worktrees);
  2. they equal EXPECT, which for the reviewer's cases is the ground truth recorded in the
     round-three review (pytest --collect-only, vitest list);
  3. the exit code and finding types equal EXPECT.

A missing runner is a FAIL, never a skip.

Usage: python3 -B smoke/assertion-guard/runner_cases.py
  RUNNER_PYTHON  a python with pytest installed (default: this python)
  VITEST_BIN     vitest binary (default: runner-js/node_modules/.bin/vitest)
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GUARD = os.environ.get("GUARD", os.path.join(ROOT, "assertion-guard", "guard.py"))
PY = os.environ.get("RUNNER_PYTHON", sys.executable)
if os.sep in PY:
    # Absolute, so the pre-check and every collection (each in another directory) run the
    # same interpreter. A relative path once passed the pre-check and failed every case.
    PY = os.path.abspath(PY)
JS_NM = os.path.join(HERE, "runner-js", "node_modules")
VITEST = os.environ.get("VITEST_BIN", os.path.join(JS_NM, ".bin", "vitest"))
PY_COLLECT = f'"{PY}" -m pytest --collect-only -q -p no:cacheprovider'
JS_COLLECT = f'"{VITEST}" list'


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace")


def write(d, files):
    for p, c in files.items():
        fp = os.path.join(d, p)
        if c is None:
            if os.path.exists(fp):
                os.remove(fp)
            continue
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as fh:
            fh.write(c if isinstance(c, bytes) else c.encode())


def ground_truth(d, js):
    """The runner's own answer, run directly in the case repo."""
    cmd = JS_COLLECT if js else PY_COLLECT
    p = subprocess.run(cmd, shell=True, cwd=d, capture_output=True, text=True, errors="replace",
                       env=dict(os.environ, NO_COLOR="1", FORCE_COLOR="0"))
    if js:
        return {ln.strip() for ln in p.stdout.splitlines() if " > " in ln}, p.returncode
    return {ln.strip() for ln in p.stdout.splitlines() if "::" in ln and not ln.startswith(" ")}, p.returncode


def run_case(work, name, before, after, js, cmd=None):
    d = os.path.join(work, name)
    os.makedirs(d)
    sh(d, "git", "init", "-q", "-b", "main")
    sh(d, "git", "config", "user.email", "p@example.invalid")
    sh(d, "git", "config", "user.name", "p")
    if js:
        os.symlink(JS_NM, os.path.join(d, "node_modules"))
        write(d, {".gitignore": "node_modules\n"})
    write(d, before)
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-qm", "base")
    gt_base, _ = ground_truth(d, js)
    write(d, after)
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-qm", "head")
    gt_head, _ = ground_truth(d, js)
    ids = d + ".ids"
    g = sh(d, sys.executable, "-B", GUARD, "--base", "main~1", "--head", "main",
           "--collect-command", cmd or (JS_COLLECT if js else PY_COLLECT), "--ids-out", ids,
           "--no-annotations", "--summary-file", "")
    diff = sh(d, "git", "diff", "--no-color", "-M", "main~1...main").stdout
    with open(d + ".diff", "w") as fh:
        fh.write(diff)
    if os.path.isdir(ids):
        r = sh(work, sys.executable, "-B", GUARD, "--diff-file", d + ".diff",
               "--base-ids", os.path.join(ids, "base.ids"), "--head-ids", os.path.join(ids, "head.ids"),
               "--no-annotations", "--summary-file", "")
    else:
        r = None
    return g, r, sorted(gt_base - gt_head)


def lost_of(out):
    return sorted(ln.split("\t")[2].split("`")[1] for ln in out.splitlines()
                  if ln.startswith("test-deleted\t") and "was collected by the test runner" in ln)


def kinds_of(out):
    return sorted(ln.split("\t", 1)[0] for ln in out.splitlines() if ln.count("\t") >= 2)


# ======================================================================================
# Round-three reviewer cases, verbatim from review-fw-6-r3/hunt3.py (pytest)
# ======================================================================================
CALC = {"src/calc.py": "def add(a, b):\n    return a + b\n"}
CALC_T = ("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
          "def test_neg():\n    assert add(-1, 1) == 0\n")
CALC_T_ONE = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
CONF = "import sys, os\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
BASE = {**CALC, "tests/test_calc.py": CALC_T, "tests/conftest.py": CONF}

cases = []

# H1: module-level tests grouped into a class pytest does not collect (no Test prefix).
cases.append(("H1-grouped-into-non-Test-class", BASE, {
    "tests/test_calc.py": "from calc import add\n\n\nclass CalcTests:\n    def test_add(self):\n"
                          "        assert add(1, 2) == 3\n\n    def test_neg(self):\n"
                          "        assert add(-1, 1) == 0\n"}))

# H1b: one test moved into a non-Test helper class in another collected file.
cases.append(("H1b-moved-into-non-Test-class-other-file", BASE, {
    "tests/test_calc.py": CALC_T_ONE,
    "tests/test_more.py": "from calc import add\n\n\nclass Checks:\n    def test_neg(self):\n"
                          "        assert add(-1, 1) == 0\n"}))

# H1c: unittest mixin pattern: methods moved off the TestCase into a mixin nobody mixes in.
UT = ("import unittest\nfrom calc import add\n\n\nclass TestCalc(unittest.TestCase):\n"
      "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
      "    def test_neg(self):\n        self.assertEqual(add(-1, 1), 0)\n")
cases.append(("H1c-unittest-method-moved-to-mixin", {**BASE, "tests/test_calc.py": UT}, {
    "tests/test_calc.py": ("import unittest\nfrom calc import add\n\n\nclass NegMixin:\n"
                           "    def test_neg(self):\n        self.assertEqual(add(-1, 1), 0)\n\n\n"
                           "class TestCalc(unittest.TestCase):\n"
                           "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n")}))

# H2: moved into tests/build/, which pytest's default norecursedirs skips at any depth.
cases.append(("H2-moved-under-tests-build", BASE, {
    "tests/test_calc.py": CALC_T_ONE,
    "tests/build/test_neg.py": "from calc import add\n\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"}))

# H3: the repo's own testpaths (mcp-unifi, mcp-phish: testpaths = ["tests"]; CI runs bare pytest).
PYPROJ = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n'
cases.append(("H3-moved-outside-testpaths", {**BASE, "pyproject.toml": PYPROJ}, {
    "tests/test_calc.py": CALC_T_ONE,
    "src/test_neg.py": "from calc import add\n\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"}))

# H4: R5 again, the 401 survives only in a comment.
API_SRC = {"src/api.py": "x = 1\n", "tests/conftest.py": CONF}
API_OLD = ("def test_admin_requires_auth(client):\n    r = client.get(\"/admin\")\n    assert r.status_code == 401\n\n\n"
           "def test_health(client):\n    r = client.get(\"/health\")\n    assert r.status_code == 200\n")
API_NEW = ("import pytest\n\n\n# anonymous callers used to get a 401 here\n"
           "@pytest.mark.parametrize(\"path\", [\"/admin\", \"/health\"])\n"
           "def test_pages_load_for_admin(admin_client, path):\n    assert admin_client.get(path).status_code == 200\n\n\n"
           "def test_health(client):\n    r = client.get(\"/health\")\n    assert r.status_code == 200\n")
cases.append(("H4-consolidated-expected-value-only-in-comment",
              {**API_SRC, "tests/test_api.py": API_OLD}, {"tests/test_api.py": API_NEW}))

# H4b: the 401 survives as an expected value for a DIFFERENT path; /admin now expects 200.
API_NEW_B = ("import pytest\n\n\n"
             "@pytest.mark.parametrize(\"path,code\", [(\"/admin\", 200), (\"/health\", 200), (\"/login\", 401)])\n"
             "def test_status_codes(admin_client, path, code):\n    assert admin_client.get(path).status_code == code\n\n\n"
             "def test_health(client):\n    r = client.get(\"/health\")\n    assert r.status_code == 200\n")
cases.append(("H4b-consolidated-value-in-another-row",
              {**API_SRC, "tests/test_api.py": API_OLD}, {"tests/test_api.py": API_NEW_B}))

# H4c: a raises-only test (no == so no expected value is demanded) gone; its message
# survives only as a parametrize id in an unrelated test.
P_OLD = ("import pytest\nfrom calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
         "def test_rejects_text():\n    with pytest.raises(TypeError, match=\"unsupported operand\"):\n"
         "        add(1, \"x\")\n")
P_NEW = ("import pytest\nfrom calc import add\n\n\n"
         "@pytest.mark.parametrize(\"a,b,want\", [(1, 2, 3), (2, 2, 4)], ids=[\"small\", \"unsupported operand\"])\n"
         "def test_add(a, b, want):\n    assert add(a, b) == want\n")
cases.append(("H4c-raises-test-consolidated-by-id-string",
              {**BASE, "tests/test_calc.py": P_OLD}, {"tests/test_calc.py": P_NEW}))

# H5: excusal (a) when the code MOVED: utils.py folded into an existing text.py.
U_BASE = {"src/text.py": "def upper(s):\n    return s.upper()\n" + "".join(f"# filler {i}\n" for i in range(20)),
          "src/utils.py": "def slugify(s):\n    return s.lower().replace(' ', '-')\n",
          "tests/conftest.py": CONF,
          "tests/test_utils.py": "from utils import slugify\n\n\ndef test_slugify():\n    assert slugify('A B') == 'a-b'\n",
          "tests/test_text.py": "from text import upper\n\n\ndef test_upper():\n    assert upper('a') == 'A'\n"}
cases.append(("H5-excusal-a-code-moved-not-deleted", U_BASE, {
    "src/utils.py": None, "tests/test_utils.py": None,
    "src/text.py": U_BASE["src/text.py"] + "\n\ndef slugify(s):\n    return s.lower().replace(' ', '-')\n"}))

# H5b: excusal (a) by an unrelated same-stem file in another directory.
A_BASE = {"src/auth.py": "def login(u, p):\n    return u == 'a' and p == 'b'\n",
          "scripts/auth.py": "print('one-off migration')\n",
          "tests/conftest.py": CONF,
          "tests/test_auth.py": "from auth import login\n\n\ndef test_rejects():\n    assert login('a', 'x') is False\n",
          "tests/test_other.py": "def test_ok():\n    assert 1 + 1 == 2\n"}
cases.append(("H5b-excusal-a-unrelated-same-stem-script", A_BASE,
              {"scripts/auth.py": None, "tests/test_auth.py": None}))

# H6: moved by name into a file that already defines that name: the move shadows the
# existing test, which stops running.
B_BASE = {**CALC, "tests/conftest.py": CONF,
          "tests/test_a.py": "from calc import add\n\n\ndef test_init():\n    assert add(0, 0) == 0\n\n\ndef test_keep_a():\n    assert add(2, 2) == 4\n",
          "tests/test_b.py": "from calc import add\n\n\ndef test_init():\n    assert add(5, 5) == 10\n\n\ndef test_keep_b():\n    assert add(3, 3) == 6\n"}
cases.append(("H6-move-shadows-same-name", B_BASE, {
    "tests/test_a.py": "from calc import add\n\n\ndef test_keep_a():\n    assert add(2, 2) == 4\n",
    "tests/test_b.py": B_BASE["tests/test_b.py"] + "\n\ndef test_init():\n    assert add(0, 0) == 0\n"}))

# H6b: no move at all: a duplicate def appended shadows the original.
cases.append(("H6b-duplicate-def-shadows-original", B_BASE, {
    "tests/test_b.py": B_BASE["tests/test_b.py"] + "\n\ndef test_init():\n    assert add(0, 0) == 0\n"}))

# H7: one parametrize row removed (the failing case).
PR_OLD = ("import pytest\nfrom calc import add\n\n\n@pytest.mark.parametrize(\"a,b,want\", [\n"
          "    (1, 2, 3),\n    (-1, 1, 0),\n    (2, 2, 4),\n])\ndef test_add(a, b, want):\n    assert add(a, b) == want\n")
cases.append(("H7-parametrize-row-removed", {**BASE, "tests/test_calc.py": PR_OLD},
              {"tests/test_calc.py": PR_OLD.replace("    (-1, 1, 0),\n", "")}))

# H8: rename in place that swaps the subject on one line (assertion kept as context).
R_OLD = ("from calc import add\n\n\ndef test_add():\n    result = add(1, 2)\n    assert result == 3\n\n\n"
         "def test_neg():\n    assert add(-1, 1) == 0\n")
cases.append(("H8-rename-in-place-subject-swapped", {**BASE, "tests/test_calc.py": R_OLD},
              {"tests/test_calc.py": R_OLD.replace("def test_add():\n    result = add(1, 2)",
                                                   "def test_three():\n    result = 3")}))
cases.append(("H8b-control-same-edit-no-rename", {**BASE, "tests/test_calc.py": R_OLD},
              {"tests/test_calc.py": R_OLD.replace("    result = add(1, 2)", "    result = 3")}))

# H9: wrapped in `if False:` (re-indented, same file, same name).
cases.append(("H9-wrapped-in-if-False", BASE, {
    "tests/test_calc.py": CALC_T_ONE + "\n\nif False:\n    def test_neg():\n        assert add(-1, 1) == 0\n"}))

# H10: collect_ignore added to conftest (nothing removed).
cases.append(("H10-collect_ignore-added", BASE, {
    "tests/conftest.py": CONF + "collect_ignore = ['test_calc.py']\n"}))

# H11: addopts --deselect added to pyproject (nothing removed).
cases.append(("H11-addopts-deselect-added", {**BASE, "pyproject.toml": PYPROJ}, {
    "pyproject.toml": PYPROJ + 'addopts = ["--deselect", "tests/test_calc.py::test_neg"]\n'}))

# H12: __init__ added to a Test class (pytest refuses to collect the class).
C_OLD = ("from calc import add\n\n\nclass TestCalc:\n    def test_add(self):\n        assert add(1, 2) == 3\n\n"
         "    def test_neg(self):\n        assert add(-1, 1) == 0\n")
cases.append(("H12-init-added-to-Test-class", {**BASE, "tests/test_calc.py": C_OLD}, {
    "tests/test_calc.py": C_OLD.replace("class TestCalc:\n", "class TestCalc:\n    def __init__(self):\n        self.n = 0\n\n")}))

# H13: control. A genuine move into a collected Test class in another file stays green.
cases.append(("H13-control-move-into-Test-class", BASE, {
    "tests/test_calc.py": CALC_T_ONE,
    "tests/test_more.py": "from calc import add\n\n\nclass TestMore:\n    def test_neg(self):\n"
                          "        assert add(-1, 1) == 0\n"}))

JS_SRC = {"package.json": '{"name":"x","type":"module","private":true}\n',
       "src/calc.js": "export const add = (a, b) => a + b;\n"}
JS_T = ("import { it, expect } from 'vitest';\nimport { add } from './calc.js';\n\n"
     "it('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n\n"
     "it('handles negatives', () => {\n  expect(add(-1, 1)).toBe(0);\n});\n")
JS_T1 = ("import { it, expect } from 'vitest';\nimport { add } from './calc.js';\n\n"
      "it('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n")
JS_HELP = ("import { it, expect } from 'vitest';\nimport { add } from '../src/calc.js';\n\n"
        "export const mk = () => 1;\n\n"
        "it('handles negatives', () => {\n  expect(add(-1, 1)).toBe(0);\n});\n")


py_cases = [(n, b, a, False) for n, b, a in cases]

# ======================================================================================
# Round-three reviewer cases, verbatim from review-fw-6-r3/jshunt.py (vitest)
# ======================================================================================
js_base = {**JS_SRC, "src/calc.test.js": JS_T}
js_cases = [
    ("J1-moved-into-test-dir-helper", js_base, {"src/calc.test.js": JS_T1, "test/helpers.js": JS_HELP}, True),
    ("J2-moved-into-__tests__-helper", js_base, {"src/calc.test.js": JS_T1, "src/__tests__/helpers.js": JS_HELP.replace("../src/calc.js", "../calc.js")}, True),
    ("J3-control-moved-into-test-file", js_base, {"src/calc.test.js": JS_T1, "test/neg.test.js": JS_HELP}, True),
]

# ======================================================================================
# The earlier R and T series (cases.py), now judged by the runner. pythonpath=src lets
# pytest import the code under test, as the repos in this fleet configure it.
# ======================================================================================
_spec = importlib.util.spec_from_file_location("ag_cases", os.path.join(HERE, "cases.py"))
_cases_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases_mod)
# The static cases never had to import: the runner does. So every one gets the code it
# imports (src/calc.py) and the two names a test annotates with (FastMCP, Client), unless
# the case brings its own file of that name.
PYPATH = {
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/conftest.py": "import builtins\n\nbuiltins.FastMCP = object\nbuiltins.Client = object\n",
}
rt_cases = []
for _n, _b, _a, _ in _cases_mod.cases + _cases_mod.extra:
    if _n[0] in "RT" and not any(p.endswith((".js", ".ts", ".tsx", ".jsx", ".mjs")) for p in list(_b) + list(_a)):
        rt_cases.append(("rt-" + _n.split(" ")[0], {**PYPATH, **_b}, _a, False))

# ======================================================================================
# The runner cannot collect: exit 2 with its error tail, never a fallback to static.
# ======================================================================================
fail_cases = [
    ("C1-collection-fails-at-head", BASE,
     {"tests/test_calc.py": CALC_T + "\n\ndef test_broken(:\n    pass\n"}, False),
]

# ======================================================================================
# Round-four review cases (review-fw-6-r4: case1.sh, case5.sh, casejs.sh, casejs3.sh,
# casepoly.sh). File contents copied from the scripts; each script's runs are cases here.
# ======================================================================================
R4_PY_BASE = {
    "tests/test_a.py": ("import pytest\n\ndef test_one():\n    assert 1 == 1\n\ndef test_important():\n"
                        "    assert 2 + 2 == 4\n\n@pytest.mark.parametrize(\"x\", [1, 2])\ndef test_param(x):\n"
                        "    assert x > 0\n"),
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
}
R4_PY_HEAD = {"tests/test_a.py": R4_PY_BASE["tests/test_a.py"].replace(
    "def test_important():\n    assert 2 + 2 == 4\n\n", "")}
R4_PYQ = f'"{PY}" -m pytest --collect-only -q'
r4_cases = [
    ("r4-case1-plain", R4_PY_BASE, R4_PY_HEAD, False, R4_PYQ),
    ("r4-case1-nosuchdir", R4_PY_BASE, R4_PY_HEAD, False, R4_PYQ + " nosuchdir"),
    ("r4-case1-k-nomatch", R4_PY_BASE, R4_PY_HEAD, False, R4_PYQ + " -k nomatch"),
    ("r4-case1-qq", R4_PY_BASE, R4_PY_HEAD, False, f'"{PY}" -m pytest --collect-only -qq'),
    ("r4-case1-no-terminal", R4_PY_BASE, R4_PY_HEAD, False, R4_PYQ + " -p no:terminal"),
    ("r4-case1-head-3", R4_PY_BASE, R4_PY_HEAD, False, R4_PYQ + " | head -3"),
    ("r4-case1-m-slow", R4_PY_BASE, R4_PY_HEAD, False, R4_PYQ + " --co -m slow"),
    ("r4-case1-echo", R4_PY_BASE, R4_PY_HEAD, False, "echo tests collected fine"),
    ("r4-case1-true", R4_PY_BASE, R4_PY_HEAD, False, "true"),
]
R4_C5_A = ('import pytest, warnings\n\nwarnings.warn("collection-time warning", UserWarning)\n\n'
           'def test_one():\n    assert True\n\n'
           '@pytest.mark.parametrize("v", ["a::b", "c d", "[e]", "x\\ny"], ids=["a::b", "c d", "[e]", None])\n'
           'def test_odd_ids(v):\n    assert v\n')
R4_C5_B = ("def test_spaced_keep():\n    assert True\n\ndef test_spaced_important():\n"
           "    assert 1 + 1 == 2\n")
r4_cases.append(("r4-case5-spaced-path-and-odd-ids",
                 {"pyproject.toml": R4_PY_BASE["pyproject.toml"], "tests/test_a.py": R4_C5_A,
                  "tests/my dir/test_b.py": R4_C5_B},
                 {"tests/my dir/test_b.py": R4_C5_B.replace("def test_spaced_important():\n    assert 1 + 1 == 2\n", ""),
                  "tests/test_a.py": R4_C5_A.replace('"[e]", "x\\ny"]', '"[e]"]').replace('"[e]", None]', '"[e]"]')},
                 False, R4_PYQ))
R4_JS_PKG = {"package.json": open(os.path.join(HERE, "runner-js", "package.json")).read()}
R4_JS_A = ("import { describe, it, expect } from 'vitest'\ndescribe('math', () => {\n"
           "  it('one', () => { expect(1).toBe(1) })\n  it('important', () => { expect(2 + 2).toBe(4) })\n})\n")
R4_JS_B = ("import { it, expect } from 'vitest'\nit('spaced keep', () => { expect(1).toBe(1) })\n"
           "it('spaced important', () => { expect(1).toBe(1) })\n")
R4_JS_BASE = {**R4_JS_PKG, "test/a.test.ts": R4_JS_A, "test/my dir/b.test.ts": R4_JS_B}
R4_JS_F1 = {"test/a.test.ts": "".join(ln for ln in R4_JS_A.splitlines(True) if "'important'" not in ln)}
R4_JS_F2 = {"test/my dir/b.test.ts": "".join(ln for ln in R4_JS_B.splitlines(True) if "'spaced important'" not in ln)}
R4_VL = f'"{VITEST}" list'
r4_cases += [
    ("r4-casejs-feature", R4_JS_BASE, R4_JS_F1, True, R4_VL),
    ("r4-casejs-feature2-spaced", R4_JS_BASE, R4_JS_F2, True, R4_VL),
    ("r4-casejs-json", R4_JS_BASE, R4_JS_F1, True, R4_VL + " --json"),
    ("r4-casejs-t-nomatch", R4_JS_BASE, R4_JS_F1, True, R4_VL + " -t nomatch"),
    ("r4-casejs-filesOnly", R4_JS_BASE, R4_JS_F1, True, R4_VL + " --filesOnly"),
    ("r4-casejs-nosuchdir", R4_JS_BASE, R4_JS_F1, True, R4_VL + " nosuchdir"),
]
R4_J3 = ("import { it, expect } from 'vitest'\nconst add = (a: number, b: number) => a + b\n\n"
         "it('keep', () => {\n  expect(add(1, 1)).toBe(2)\n})\n\n"
         "it('important', () => {\n  expect(add(2, 2)).toBe(4)\n})\n")
R4_J3_FOLD = ("import { it, expect } from 'vitest'\nconst add = (a: number, b: number) => a + b\n\n"
              "it('keep', () => {\n  expect(add(1, 1)).toBe(2)\n  expect(add(2, 2)).toBe(4)\n})\n")
R4_J3_BASE = {**R4_JS_PKG, "test/my dir/b.test.ts": R4_J3, "test/c.test.ts": R4_J3}
r4_cases += [
    ("r4-casejs3-plain", R4_J3_BASE, {"test/c.test.ts": R4_J3_FOLD}, True, R4_VL),
    ("r4-casejs3-spaced", R4_J3_BASE, {"test/my dir/b.test.ts": R4_J3_FOLD}, True, R4_VL),
    ("r4-casejs3-nosuchdir", R4_J3_BASE, {"test/c.test.ts": R4_J3_FOLD}, True, R4_VL + " nosuchdir"),
    ("r4-casejs3-t-nomatch", R4_J3_BASE, {"test/c.test.ts": R4_J3_FOLD}, True, R4_VL + " -t nomatch"),
    ("r4-casejs3-json", R4_J3_BASE, {"test/c.test.ts": R4_J3_FOLD}, True, R4_VL + " --json"),
]
R4_POLY_UI = ("import { it, expect } from 'vitest'\nimport { add } from '../src/add'\n\n"
              "it('keep', () => {\n  expect(add(1, 1)).toBe(2)\n})\n\n"
              "it('important', () => {\n  expect(add(2, 2)).toBe(4)\n})\n")
R4_POLY_FOLD = ("import { it, expect } from 'vitest'\nimport { add } from '../src/add'\n\n"
                "it('keep', () => {\n  expect(add(1, 1)).toBe(2)\n  expect(add(2, 2)).toBe(4)\n})\n")
r4_cases.append(("r4-casepoly-python-runner-js-fold", {**R4_PY_BASE, "web/test/ui.test.ts": R4_POLY_UI},
                 {"web/test/ui.test.ts": R4_POLY_FOLD}, False, R4_PYQ))

TD = "test-deleted"
NOTHING = "the collect command collects nothing"
EXPECT_R4 = {
    "r4-case1-plain": (1, [TD], ["tests/test_a.py::test_important"], ""),
    "r4-case1-nosuchdir": (2, [], [], "", "failed (exit 4)"),
    "r4-case1-k-nomatch": (2, [], [], "", "every test was deselected at both ends"),
    "r4-case1-qq": (2, [], [], "", NOTHING),
    "r4-case1-no-terminal": (2, [], [], "", "failed (exit 4)"),
    "r4-case1-head-3": (2, [], [], "", "no 'N tests collected' line"),
    "r4-case1-m-slow": (2, [], [], "", "every test was deselected at both ends"),
    "r4-case1-echo": (2, [], [], "", NOTHING),
    "r4-case1-true": (2, [], [], "", NOTHING),
    "r4-case5-spaced-path-and-odd-ids": (1, [TD, TD], ["tests/my dir/test_b.py::test_spaced_important",
                                                       "tests/test_a.py::test_odd_ids[x\\ny]"], ""),
    "r4-casejs-feature": (1, [TD, "net-assertion-loss"], ["test/a.test.ts > math > important"], ""),
    "r4-casejs-feature2-spaced": (1, [TD, "net-assertion-loss"], ["test/my dir/b.test.ts > spaced important"], ""),
    "r4-casejs-json": (2, [], [], "", NOTHING),
    "r4-casejs-t-nomatch": (2, [], [], "", NOTHING),
    "r4-casejs-filesOnly": (2, [], [], "", NOTHING),
    "r4-casejs-nosuchdir": (2, [], [], "", NOTHING),
    "r4-casejs3-plain": (1, [TD], ["test/c.test.ts > important"], ""),
    "r4-casejs3-spaced": (1, [TD], ["test/my dir/b.test.ts > important"], ""),
    "r4-casejs3-nosuchdir": (2, [], [], "", NOTHING),
    "r4-casejs3-t-nomatch": (2, [], [], "", NOTHING),
    "r4-casejs3-json": (2, [], [], "", NOTHING),
    "r4-casepoly-python-runner-js-fold": (1, [TD], [], "the JS test is caught statically",
                                          "statically for TS/JS, not by the runner"),
}

# ======================================================================================
# EXPECT: (exit code, sorted finding types, sorted lost IDs, note)
# ======================================================================================
TD = "test-deleted"
CC = "collection-config-changed"
EXPECT = {
    # --- round-three holes: every one red, judged by the runner --------------------------
    "H1-grouped-into-non-Test-class": (1, [TD, TD], ["tests/test_calc.py::test_add", "tests/test_calc.py::test_neg"], ""),
    "H1b-moved-into-non-Test-class-other-file": (1, [TD], ["tests/test_calc.py::test_neg"], ""),
    "H1c-unittest-method-moved-to-mixin": (1, [TD], ["tests/test_calc.py::TestCalc::test_neg"], ""),
    "H2-moved-under-tests-build": (1, [TD], ["tests/test_calc.py::test_neg"], ""),
    "H3-moved-outside-testpaths": (1, [TD], ["tests/test_calc.py::test_neg"], ""),
    "H4-consolidated-expected-value-only-in-comment": (1, [TD], ["tests/test_api.py::test_admin_requires_auth"], ""),
    "H4b-consolidated-value-in-another-row": (1, [TD], ["tests/test_api.py::test_admin_requires_auth"], ""),
    "H4c-raises-test-consolidated-by-id-string": (1, [TD, TD], ["tests/test_calc.py::test_add", "tests/test_calc.py::test_rejects_text"], ""),
    "H5-excusal-a-code-moved-not-deleted": (1, [TD], ["tests/test_utils.py::test_slugify"], ""),
    "H5b-excusal-a-unrelated-same-stem-script": (1, [TD], ["tests/test_auth.py::test_rejects"], ""),
    "H6-move-shadows-same-name": (1, [TD], ["tests/test_a.py::test_init"], ""),
    "H6b-duplicate-def-shadows-original": (0, [], [], "KNOWN LIMIT: the runner still collects the name; the shadowed body is invisible to both"),
    "H7-parametrize-row-removed": (1, [TD], ["tests/test_calc.py::test_add[-1-1-0]"], ""),
    "H8-rename-in-place-subject-swapped": (1, [TD], ["tests/test_calc.py::test_add"], "rename: red by design"),
    "H8b-control-same-edit-no-rename": (0, [], [], "control"),
    "H9-wrapped-in-if-False": (1, [TD], ["tests/test_calc.py::test_neg"], ""),
    "H10-collect_ignore-added": (1, [CC, TD, TD], ["tests/test_calc.py::test_add", "tests/test_calc.py::test_neg"], ""),
    "H11-addopts-deselect-added": (1, [CC, TD], ["tests/test_calc.py::test_neg"], ""),
    "H12-init-added-to-Test-class": (1, [TD, TD], ["tests/test_calc.py::TestCalc::test_add", "tests/test_calc.py::TestCalc::test_neg"], ""),
    "H13-control-move-into-Test-class": (1, [TD], ["tests/test_calc.py::test_neg"], "move: red by design"),
    "J1-moved-into-test-dir-helper": (1, [TD], ["src/calc.test.js > handles negatives"], ""),
    "J2-moved-into-__tests__-helper": (1, [TD], ["src/calc.test.js > handles negatives"], ""),
    "J3-control-moved-into-test-file": (1, [TD], ["src/calc.test.js > handles negatives"], "move: red by design"),
    "C1-collection-fails-at-head": (2, [], [], "the runner cannot collect: exit 2, no verdict"),
}
# The earlier R and T series, judged by the runner. Renames and moves are now red by design
# (R1, R3, R3b, T4, T6): a lost ID plus a new one, read by a human.
EXPECT_RT = {
    "rt-R1-rename-with-input-changed": (1, [TD], ["tests/test_auth.py::test_rejects_bad_password"], "rename: red by design"),
    "rt-R1b-same-edit-in-place-no-rename": (0, [], [], "control"),
    "rt-R2-copy-drops-distinct-assert": (1, [TD], ["tests/test_auth.py::test_rejects_bad_password"], ""),
    "rt-R3-rename-and-strengthen-assert": (1, [TD], ["tests/test_auth.py::test_rejects_bad_password"], "rename: red by design"),
    "rt-R3b-rename-and-change-expected-literal": (1, [TD], ["tests/test_auth.py::test_rejects_bad_password"], "rename: red by design"),
    "rt-R4-helper-assert-test-swapped-in-place": (1, [TD], ["tests/test_auth.py::test_admin_flow"], ""),
    "rt-R5-auth-test-deleted-literal-reused-in-parametrize": (1, [TD], ["tests/test_api.py::test_admin_requires_auth"], ""),
    "rt-R6-moved-to-uncollected-file-not-a-git-rename": (1, [TD, TD], ["tests/test_calc.py::test_add", "tests/test_calc.py::test_neg"], ""),
    "rt-R6b-tests-moved-into-existing-helper-module": (1, [TD, TD], ["tests/test_calc.py::test_add", "tests/test_calc.py::test_neg"], ""),
    "rt-R6c-tests-moved-into-conftest": (1, [TD, TD], ["tests/test_calc.py::test_add", "tests/test_calc.py::test_neg"], ""),
    "rt-R6d-one-test-moved-into-conftest-file-kept": (1, [TD], ["tests/test_calc.py::test_neg"], ""),
    "rt-R7-test-deleted-with-same-stem-doc-deleted": (1, [TD], ["tests/test_security.py::test_rejects"], ""),
    "rt-R4b-mock-only-test-swapped-in-place": (1, [TD], ["tests/test_n.py::test_sends"], ""),
    "rt-T1-only-the-80pct-threshold-blocks": (1, [TD], ["tests/test_auth.py::test_rejects_bad_password"], ""),
    "rt-T2-only-the-literal-check-blocks": (1, [TD], ["tests/test_auth.py::test_admin_requires_auth"], ""),
    "rt-T3-only-the-assertion-check-blocks": (1, [TD], ["tests/test_auth.py::test_flags"], ""),
    "rt-T4-control-real-rename-and-move": (1, [TD], ["tests/test_auth.py::test_admin_requires_auth"], "rename and move: red by design"),
    "rt-T5-function-removed-from-uncollected-helper": (0, [], [], "never collected, so nothing lost"),
    "rt-T6-rename-in-place-multiline-signature": (1, [TD], ["tests/test_adaptive.py::test_end_to_end_summary_reaches_the_text"], "rename: red by design"),
}


def main():
    discover = "--discover" in sys.argv
    missing = [p for p in (PY,) if not os.path.exists(p) and os.sep in p]
    if subprocess.run([PY, "-m", "pytest", "--version"], capture_output=True).returncode != 0:
        missing.append(f"pytest under {PY}")
    if not os.path.exists(VITEST):
        missing.append(VITEST)
    if missing:
        print(f"::error::runner missing, so the collection fixtures cannot run: {missing}")
        print("assertion-guard runner cases: FAIL (no runner)")
        return 1
    all_cases = [c if len(c) == 5 else (*c, None)
                 for c in py_cases + js_cases + rt_cases + fail_cases + r4_cases]
    fail = 0
    work = tempfile.mkdtemp()
    try:
        for name, before, after, js, cmd in all_cases:
            g, r, gt_lost = run_case(work, name, before, after, js, cmd)
            got_lost = lost_of(g.stdout)
            got_kinds = kinds_of(g.stdout)
            replay_same = r is not None and (r.returncode, r.stdout) == (g.returncode, g.stdout)
            if discover:
                print(f"{name}: exit {g.returncode} kinds {got_kinds} lost {got_lost} truth {gt_lost} replay_same {replay_same}")
                if g.returncode == 2:
                    print("   " + g.stdout.strip().splitlines()[-1][:160])
                continue
            want = EXPECT.get(name) or EXPECT_RT.get(name) or EXPECT_R4.get(name)
            if want is None:
                print(f"::error::case {name} has no EXPECT row")
                fail = 1
                continue
            want_rc, want_kinds, want_lost, note = want[:4]
            must_say = want[4] if len(want) > 4 else ""
            problems = []
            if must_say and must_say not in g.stdout:
                problems.append(f"output does not say {must_say!r}")
            if g.returncode != want_rc:
                problems.append(f"exit {g.returncode}, want {want_rc}")
            if got_kinds != sorted(want_kinds):
                problems.append(f"findings {got_kinds}, want {sorted(want_kinds)}")
            if want_rc != 2:
                if got_lost != sorted(want_lost):
                    problems.append(f"lost {got_lost}, want {sorted(want_lost)}")
                if got_lost != gt_lost:
                    problems.append(f"lost {got_lost} but the runner itself lost {gt_lost}")
                if not replay_same:
                    problems.append("replay with the recorded IDs did not print identically")
                if "test deletions checked by the runner" not in g.stdout.splitlines()[-1]:
                    problems.append("verdict does not say the runner checked deletions")
            elif "ERROR" not in g.stdout.splitlines()[-1]:
                problems.append("exit 2 without an ERROR verdict line")
            tag = f"  ({note})" if note else ""
            if problems:
                fail = 1
                print(f"::error::runner case {name}: " + "; ".join(problems) + tag)
                for ln in g.stdout.splitlines()[-8:]:
                    print(f"  git: {ln[:170]}")
            else:
                print(f"PASS runner case {name}: exit {g.returncode} {got_kinds} lost {got_lost}{tag}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if discover:
        return 0
    n = len(all_cases)
    if fail:
        print(f"assertion-guard runner cases: FAIL ({n} cases run)")
        return 1
    print(f"assertion-guard runner cases: PASS ({n} cases run with the real runner, lost IDs "
          f"matched the runner's own answer, replay identical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
