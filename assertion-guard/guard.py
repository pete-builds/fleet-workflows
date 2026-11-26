#!/usr/bin/env python3
"""assertion-guard: flag pull requests that make tests easier to pass.

Reads a unified diff (``git diff <base>...<head>``, or ``--diff-file`` for offline
replay) and looks ONLY at test files and at config files that carry coverage floors.
Python standard library only, so a caller installs nothing.

Five finding types, each reported with a file, a line and one plain sentence:

  net-assertion-loss      a test file that still exists loses more assertions than it gains
  loosened-assertion      an exact check replaced by a weaker one in the same change, or
                          an added check over constants only, which cannot fail
  skip-added              a skip, xfail or .only was added
  test-deleted            a test removed and not re-added anywhere in the diff; a whole
                          test file deleted unless its subject source is deleted in the
                          same diff; a test file or Test class renamed out of collection

Git mode and --diff-file mode read the same diff and decide identically: the guard never
consults the repository tree.
  coverage-floor-lowered  fail_under, --cov-fail-under, or a vitest/jest threshold lowered

Exit 0: no findings. Exit 1: one or more findings. Exit 2: the guard could not run.
The LAST line printed is always a verdict line, so silence can never read as clean:

  assertion-guard: N finding(s) in M test file(s) scanned
  assertion-guard: ERROR: <detail>            (exit 2, no verdict was reached)

There is deliberately no override label or keyword. A red guard is meant to be read.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------
# Which files are in scope
# --------------------------------------------------------------------------------------

PY_EXT = (".py",)
JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
CODE_EXT = PY_EXT + JS_EXT

# Directories never scanned, even when they contain files that look like tests. Vendored
# dependencies are skipped wherever they sit. Build output is skipped only at the repo
# root: a package or test folder that happens to be named `build` (tests/build/) is source.
EXCLUDED_ANYWHERE = ("node_modules/", ".venv/", "venv/", "site-packages/")
EXCLUDED_AT_ROOT = ("dist/", "build/")

COVERAGE_CONFIG_NAMES = {
    "pyproject.toml", "setup.cfg", ".coveragerc", "pytest.ini", "tox.ini", "package.json",
}
COVERAGE_CONFIG_RE = re.compile(r"^(vitest|vite|jest)\.config\.[cm]?[jt]s$")


def language(path: str) -> str | None:
    if path.endswith(PY_EXT):
        return "py"
    if path.endswith(JS_EXT):
        return "js"
    return None


def is_excluded(path: str) -> bool:
    p = "/" + path
    return any(("/" + d) in p for d in EXCLUDED_ANYWHERE) or path.startswith(EXCLUDED_AT_ROOT)


def is_test_file(path: str) -> bool:
    """In scope: any file a test suite is made of, helpers included."""
    if not path or is_excluded(path) or language(path) is None:
        return False
    parts = path.split("/")
    base = parts[-1]
    dirs = parts[:-1]
    if language(path) == "py":
        return (
            "tests" in dirs
            or "test" in dirs
            or base == "conftest.py"
            or (base.startswith("test_") and base.endswith(".py"))
            or base.endswith("_test.py")
        )
    # TS / JS. `test/` is mocha's default directory.
    return "__tests__" in dirs or "test" in dirs or "tests" in dirs or ".test." in base or ".spec." in base


def is_collected(path: str) -> bool:
    """Would the runner's DEFAULT discovery collect tests from this file? Narrower than
    is_test_file: tests/helpers.py is part of the suite but holds no collected tests, and
    tests/test_calc.py renamed to tests/calc_checks.py silently stops running."""
    if not path or is_excluded(path) or language(path) is None:
        return False
    parts = path.split("/")
    base = parts[-1]
    if language(path) == "py":
        return base.startswith("test_") or base.endswith("_test.py")
    return ("__tests__" in parts[:-1] or "test" in parts[:-1]
            or ".test." in base or ".spec." in base)


def in_scope(fd: "FileDiff") -> bool:
    """A file is analysed when EITHER side of it is a test file: a test file renamed out of
    the test paths must be seen, or the rename itself hides the deletion."""
    return is_test_file(fd.new_path or "") or is_test_file(fd.old_path or "")


def is_coverage_config(path: str) -> bool:
    if not path or is_excluded(path):
        return False
    base = path.split("/")[-1]
    return base in COVERAGE_CONFIG_NAMES or bool(COVERAGE_CONFIG_RE.match(base))


def subject_stem(path: str) -> str | None:
    """The name of the source module a test file covers, or None if it has no single one."""
    base = path.split("/")[-1]
    if base == "conftest.py" or base == "__init__.py":
        return None
    stem = base
    for ext in CODE_EXT:
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    for suffix in (".test", ".spec"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)] or None
    if stem.startswith("test_"):
        return stem[len("test_"):] or None
    if stem.endswith("_test"):
        return stem[: -len("_test")] or None
    if "__tests__" in path.split("/")[:-1]:
        return stem or None
    return None


# --------------------------------------------------------------------------------------
# Unified diff parsing
# --------------------------------------------------------------------------------------


@dataclass
class Line:
    kind: str  # "-", "+" or " "
    text: str
    old_no: int  # line number in the old file (for "-" and " ")
    new_no: int  # line number in the new file; for "-" it is the anchor where it stood


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[list[Line]] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def deleted(self) -> bool:
        return self.new_path is None

    @property
    def added(self) -> bool:
        return self.old_path is None

    def lines(self, kind: str):
        for hunk in self.hunks:
            for ln in hunk:
                if ln.kind == kind:
                    yield ln


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _unquote(p: str) -> str:
    p = p.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        p = p.encode("latin-1", "ignore").decode("utf-8", "replace")
    return p


def _strip_prefix(p: str) -> str | None:
    p = _unquote(p.split("\t")[0])
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: list[Line] | None = None
    old_no = new_no = 0
    old_left = new_left = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur = FileDiff(old_path=None, new_path=None)
            # Fallback paths from the header, overwritten by ---/+++ or rename lines.
            m = re.match(r'^diff --git (?:"?a/)(.*?)"? (?:"?b/)(.*?)"?$', raw)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is not None and (old_left > 0 or new_left > 0):
            if raw.startswith("\\"):
                continue
            tag = raw[:1] if raw else " "
            body = raw[1:]
            if tag == "-":
                hunk.append(Line("-", body, old_no, new_no))
                old_no += 1
                old_left -= 1
                continue
            if tag == "+":
                hunk.append(Line("+", body, old_no, new_no))
                new_no += 1
                new_left -= 1
                continue
            if tag == " ":
                hunk.append(Line(" ", body, old_no, new_no))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
                continue
            # Anything else ends the hunk early (malformed or truncated input).
            hunk = None
        if raw.startswith("new file mode"):
            cur.old_path = None
        elif raw.startswith("deleted file mode"):
            cur.new_path = None
        elif raw.startswith("rename from "):
            cur.old_path = _unquote(raw[len("rename from "):])
        elif raw.startswith("rename to "):
            cur.new_path = _unquote(raw[len("rename to "):])
        elif raw.startswith("--- "):
            cur.old_path = _strip_prefix(raw[4:])
        elif raw.startswith("+++ "):
            cur.new_path = _strip_prefix(raw[4:])
        elif raw.startswith("Binary files "):
            cur.binary = True
        else:
            m = HUNK_RE.match(raw)
            if m:
                old_no = int(m.group(1))
                old_left = int(m.group(2)) if m.group(2) is not None else 1
                new_no = int(m.group(3))
                new_left = int(m.group(4)) if m.group(4) is not None else 1
                hunk = []
                cur.hunks.append(hunk)
    return files


def change_blocks(hunk: list[Line]):
    """Yield (removed, added) runs: each maximal stretch of -/+ lines between context."""
    removed: list[Line] = []
    added: list[Line] = []
    for ln in hunk:
        if ln.kind == " ":
            if removed or added:
                yield removed, added
            removed, added = [], []
        elif ln.kind == "-":
            removed.append(ln)
        else:
            added.append(ln)
    if removed or added:
        yield removed, added


# --------------------------------------------------------------------------------------
# Line classification
# --------------------------------------------------------------------------------------


def code_part(text: str, lang: str) -> str:
    """The line with comments removed; empty for a comment-only line."""
    s = text.strip()
    if lang == "py":
        if s.startswith("#"):
            return ""
        return s.split("  #")[0].split("\t#")[0]
    if s.startswith(("//", "/*", "*")):
        return ""
    return s.split(" //")[0]


def norm(text: str) -> str:
    """Whitespace-insensitive key, so reformatting and re-indenting match."""
    return re.sub(r"\s+", "", text)


PY_ASSERT_STMT = re.compile(r"^assert\b")
# `(msg,) = warnings()` raises unless there is exactly one item: an implicit length check.
# Calibration: mcp-unifi#195 replaced `assert len(w) == 1` with it, a pure refactor.
PY_SINGLE_UNPACK = re.compile(r"^\(?\s*[A-Za-z_]\w*\s*,\s*\)?\s*=(?!=)")
PY_SELF_ASSERT = re.compile(r"\bself\.assert\w*\s*\(|\bself\.fail\w*\s*\(")
PY_RAISES = re.compile(r"\bpytest\.(raises|warns)\s*\(")
PY_APPROX = re.compile(r"\bpytest\.approx\s*\(|(?<![\w.])approx\s*\(")
JS_EXPECT = re.compile(r"(?<![\w.$])expect\s*\(")
JS_ASSERT = re.compile(r"(?<![\w.$])assert\s*(\.\w+\s*)?\(")


def count_assertions(text: str, lang: str) -> int:
    s = code_part(text, lang)
    if not s:
        return 0
    if lang == "py":
        n = (1 if PY_ASSERT_STMT.search(s) or PY_SINGLE_UNPACK.search(s) else 0)
        n += len(PY_SELF_ASSERT.findall(s))
        n += len(PY_RAISES.findall(s))
        if n == 0 and PY_APPROX.search(s):
            # A standalone approx (expected = approx(...)) feeds a later assert. Inside an
            # assert line it is part of that one check, not a second one: counting it
            # twice made "approx -> exact", a strengthening, read as a net loss.
            n = 1
        return n
    return len(JS_EXPECT.findall(s)) + len(JS_ASSERT.findall(s))


# Exact and weak checks, for the loosened-assertion pairing.
PY_EXACT = [
    re.compile(r"^assert\b.*[^=!<>]==[^=]"),
    re.compile(r"\bassert(Dict|List|Tuple|Set|Sequence|MultiLine|Count)?Equals?\s*\("),
    re.compile(r"\.assert_called(_once)?_with\s*\(|\.assert_any_call\s*\(|\.assert_has_calls\s*\("),
]
PY_WEAK = [
    re.compile(r"\.assert_called(_once)?\s*\("),  # called at all, arguments unchecked
    re.compile(r"^assert\b.*\bis\s+not\s+None\b"),
    re.compile(r"\bassertTrue\s*\("),
    re.compile(r"\bassertIsNotNone\s*\("),
    re.compile(r"\bassertIn\s*\("),
    re.compile(r"^assert\b.*(?<!\bnot)\s+in\s+"),
]
PY_INEQUALITY = re.compile(r"<=|>=|(?<![<>=!-])[<>](?![<>=])")
# `assert x` with no comparison at all: a bare truthiness check.
PY_COMPARISON = re.compile(r"==|!=|<=|>=|<|>|\bis\b|\bin\b|\bisinstance\b|\bissubclass\b|\bany\s*\(|\ball\s*\(|\bnot\b")
JS_EXACT = [
    re.compile(r"\.(toBe|toEqual|toStrictEqual|toHaveLength)\s*\("),
    re.compile(r"\.(toHaveBeenCalledWith|toHaveBeenLastCalledWith|toHaveBeenNthCalledWith|toHaveBeenCalledTimes)\s*\("),
    re.compile(r"\bassert\.(equal|strictEqual|deepEqual|deepStrictEqual)\s*\("),
]
JS_WEAK = [
    re.compile(r"\.(toBeDefined|toBeTruthy|toContain|toContainEqual|toMatch|toMatchObject)\s*\("),
    re.compile(r"\.not\.(toBeNull|toBeUndefined)\s*\("),
    re.compile(r"\.toHaveBeenCalled\s*\("),  # called at all, arguments unchecked
    re.compile(r"\.(toBeGreaterThan|toBeGreaterThanOrEqual|toBeLessThan|toBeLessThanOrEqual)\s*\("),
    re.compile(r"\bassert\.ok\s*\(|(?<![\w.$])assert\s*\("),
]


def is_exact(text: str, lang: str) -> bool:
    s = code_part(text, lang)
    pats = PY_EXACT if lang == "py" else JS_EXACT
    # A check over constants only is not exact about anything (and is reported itself).
    return bool(s) and any(p.search(s) for p in pats) and not is_tautology(text, lang)


def is_weak(text: str, lang: str) -> bool:
    s = code_part(text, lang)
    if not s:
        return False
    if lang == "py":
        if any(p.search(s) for p in PY_WEAK):
            return True
        if PY_ASSERT_STMT.search(s):
            expr = assert_condition(s)
            # `assert len(x) == 3` -> `assert len(x) >= 1`: a range where a value was.
            if PY_INEQUALITY.search(expr) and "==" not in expr:
                return True
            return not PY_COMPARISON.search(expr) and expr.strip() not in ("", "(")
        return False
    return any(p.search(s) for p in JS_WEAK)


def assert_condition(s: str) -> str:
    """The condition of `assert cond, message`: everything before the first comma at
    bracket depth zero and outside a string. Splitting on the first comma read
    `assert f(6, 3) == 2` as `assert f(6` (caught by the weakened smoke fixture)."""
    body = s[len("assert"):]
    depth = 0
    quote = ""
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return body[:i]
        i += 1
    return body


def check_subject(text: str, lang: str) -> str:
    """What an exact check is about: `assert f(x) == 3` -> `f(x)`, and
    `expect(f(x)).toBe(3)` -> `f(x)`. Whitespace-insensitive."""
    s = code_part(text, lang)
    if lang == "py":
        m = re.match(r"^assert\s+(.*?)\s*==", s)
        if m:
            return norm(m.group(1))
        m = re.match(r"^(.*?)\.assert_\w+\s*\(", s)  # mock.assert_called_with(...)
        if m:
            return norm(m.group(1))
        m = re.search(r"\bassert\w*\s*\(\s*([^,]*)", s)
        return norm(m.group(1)) if m else norm(s)
    m = re.search(r"\bexpect\s*\((.*)\)\s*\.(?:not\.)?to\w*\s*\(", s)
    if m:
        return norm(m.group(1))
    m = re.search(r"\bassert\.\w+\s*\(\s*([^,]*)", s)
    return norm(m.group(1)) if m else norm(s)


JS_LIT = r"(?:-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"|`[^`$]*`|true|false|null|undefined)"
JS_TAUTOLOGY = re.compile(
    rf"\bexpect\s*\(\s*({JS_LIT})\s*\)\s*\.(toBe|toEqual|toStrictEqual)\s*\(\s*({JS_LIT})\s*\)"
    rf"|\bexpect\s*\(\s*(true|-?[1-9]\d*|'[^']+'|\"[^\"]+\")\s*\)\s*\.(toBeTruthy|toBeDefined)\s*\(\s*\)"
)


def _constant_truth(node) -> bool | None:
    """Value of an expression built only from literals, else None. Evaluated with no
    names and no builtins, so only constants can run."""
    import ast

    # No BinOp: `9**9**9` would make the guard itself hang.
    allowed = (ast.Expression, ast.Constant, ast.Compare, ast.BoolOp, ast.UnaryOp,
               ast.Tuple, ast.List, ast.cmpop, ast.boolop, ast.unaryop, ast.Load)
    if not all(isinstance(n, allowed) for n in ast.walk(node)):
        return None
    try:
        return bool(eval(compile(node, "<literal>", "eval"), {"__builtins__": {}}, {}))  # noqa: S307
    except Exception:
        return None


def is_tautology(text: str, lang: str) -> bool:
    """An assertion over literals only, which cannot fail: `assert 3 == 3`, `assert True`,
    `self.assertEqual(1, 1)`, `expect(true).toBe(true)`."""
    import ast

    s = code_part(text, lang)
    if not s:
        return False
    if lang != "py":
        m = JS_TAUTOLOGY.search(s)
        if not m:
            return False
        if m.group(1) is not None:  # equality form: only when both sides are the same literal
            return m.group(1) == m.group(3)
        return True
    if PY_ASSERT_STMT.search(s):
        cond = assert_condition(s).strip()
        try:
            tree = ast.parse(cond, mode="eval")
        except SyntaxError:
            return False
        return _constant_truth(tree) is True
    m = re.search(r"\bself\.assert(Equal|True|IsNotNone)\s*\((.*)\)\s*$", s)
    if m:
        try:
            tree = ast.parse(f"({m.group(2)},)", mode="eval")
        except SyntaxError:
            return False
        args = tree.body.elts
        if not args or not all(isinstance(a, ast.Constant) for a in args):
            return False
        if m.group(1) == "Equal":
            return len(args) >= 2 and args[0].value == args[1].value
        return bool(args[0].value) or (m.group(1) == "IsNotNone" and args[0].value is not None)
    return False


RAISES_ARG = re.compile(r"\b(?:pytest\.raises|assertRaises(?:Regex)?)\s*\(\s*([\w.]+)")
GENERIC_EXC = {"Exception", "BaseException", "builtins.Exception", "builtins.BaseException"}
RAISES_MATCH = re.compile(r"\bpytest\.raises\s*\(.*\bmatch\s*=")
APPROX_KW = re.compile(r"\b(rel|abs)\s*=\s*([0-9][0-9_]*\.?[0-9_]*(?:[eE][-+]?\d+)?)")
CLOSE_TO = re.compile(r"\.toBeCloseTo\s*\(([^)]*)\)")


def _num(s: str) -> float | None:
    try:
        return float(s.replace("_", ""))
    except ValueError:
        return None


def close_to_digits(text: str) -> int | None:
    m = CLOSE_TO.search(text)
    if not m:
        return None
    args = [a.strip() for a in m.group(1).split(",")]
    if len(args) >= 2 and re.fullmatch(r"-?\d+", args[1]):
        return int(args[1])
    return 2  # Jest/Vitest default precision


SKIP_PY = re.compile(
    r"\bpytest\.mark\.(skip|skipif|xfail)\b|\bunittest\.(skip|skipIf|skipUnless|expectedFailure)\b"
    r"|\bpytest\.(skip|xfail)\s*\(|\bself\.skipTest\s*\(|^@(skip|skipIf|skipUnless|expectedFailure)\b"
    r"|(?<![\w.])mark\.(skip|skipif|xfail)\b"  # after `from pytest import mark`
)
SKIP_JS = re.compile(
    r"(?<![\w.$])(it|test|describe|context|suite)\.(skip|skipIf|only)\b"
    r"|(?<![\w.$])(xit|xtest|xdescribe|fit|fdescribe)\s*\("
)


def skip_marker(text: str, lang: str) -> str | None:
    s = code_part(text, lang)
    if not s:
        return None
    m = (SKIP_PY if lang == "py" else SKIP_JS).search(s)
    return m.group(0) if m else None


PY_TEST_CLASS = re.compile(r"^\s*class\s+(Test\w*)\b")
PY_ANY_CLASS = re.compile(r"^\s*class\s+(\w+)\b")
PY_TEST_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")
JS_TEST_DEF = re.compile(
    r"^(\s*)(?:it|test|xit|xtest|fit)(?:\.(?:skip|only|concurrent|skipIf\([^)]*\)|runIf\([^)]*\)))?\s*\(\s*(['\"`])(.+?)\2"
)


def test_name(text: str, lang: str) -> tuple[int, str] | None:
    """(indent, name) when the line opens a test, else None."""
    if lang == "py":
        m = PY_TEST_DEF.match(text)
        return (len(m.group(1)), m.group(2)) if m else None
    m = JS_TEST_DEF.match(text)
    return (len(m.group(1)), m.group(3)) if m else None


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str
    path: str
    line: int
    message: str


@dataclass
class TestBlock:
    """One test function seen opening on a removed or added line, with its body as seen
    in the same hunk."""

    fd: FileDiff
    line: Line
    name: str
    indent: int
    body: list[Line] = field(default_factory=list)  # lines of the same kind inside it

    def assertion_keys(self, lang: str) -> set[str]:
        return {norm(code_part(ln.text, lang)) for ln in self.body if count_assertions(ln.text, lang)}

    def n_assertions(self, lang: str) -> int:
        return sum(count_assertions(ln.text, lang) for ln in self.body)

    def literals(self) -> set[str]:
        out: set[str] = set()
        for ln in [self.line] + self.body:
            for _, lit in STRING_LIT.findall(ln.text):
                if len(lit) >= 2:
                    out.add(lit)
        out.discard(self.name)
        return out


STRING_LIT = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""")
PARAMETRIZE = re.compile(r"\bpytest\.mark\.parametrize\b|\b(it|test|describe)\.each\b|@parameterized\b|\bsubTest\(")


def collect_tests(fd: FileDiff, kind: str) -> list[TestBlock]:
    """Tests whose opening line has this kind ("-" or "+"), with the same-kind lines that
    sit inside them before the hunk returns to context or to a shallower line."""
    lang = language(fd.path)
    out: list[TestBlock] = []
    for hunk in fd.hunks:
        cur: TestBlock | None = None
        for ln in hunk:
            if ln.kind != kind:
                if ln.kind == " " and cur is not None and ln.text.strip():
                    indent = len(ln.text) - len(ln.text.lstrip())
                    if indent <= cur.indent:
                        cur = None
                continue
            t = test_name(ln.text, lang)
            if t:
                cur = TestBlock(fd, ln, t[1], t[0])
                out.append(cur)
                continue
            if cur is None or not ln.text.strip():
                continue
            indent = len(ln.text) - len(ln.text.lstrip())
            if indent <= cur.indent and not ln.text.lstrip().startswith((")", "}", "]")):
                cur = None
                continue
            cur.body.append(ln)
    return out


def renamed_in_place(fds: list[FileDiff]) -> set[int]:
    """ids of removed test-def Lines replaced one-for-one by another test def in the same
    change block: `def test_old` -> `def test_new` with the body left as context."""
    out: set[int] = set()
    for fd in fds:
        lang = language(fd.path)
        for hunk in fd.hunks:
            for removed, added in change_blocks(hunk):
                r = [ln for ln in removed if test_name(ln.text, lang)]
                a = [ln for ln in added if test_name(ln.text, lang)]
                for ln in r[: len(a)]:
                    out.add(id(ln))
    return out


def looks_like_rename(old: TestBlock, new: TestBlock) -> bool:
    """Is `new` the same test as `old` under another name? Matching on assertion lines
    alone let an unrelated new test that happened to repeat a generic check
    (`assert r.status_code == 401`) hide a deleted one (review finding F5). So the whole
    body must carry over: every assertion, every string literal, and at least 80% of the
    removed code lines (setup included), or the identical body reformatted."""
    ol, nl = language(old.fd.path), language(new.fd.path)
    if not old.assertion_keys(ol) <= new.assertion_keys(nl):
        return False
    if not old.literals() <= new.literals() | {new.name}:
        return False
    old_lines = [norm(code_part(ln.text, ol)) for ln in old.body]
    old_lines = [x for x in old_lines if x]
    new_lines = {norm(code_part(ln.text, nl)) for ln in new.body}
    if not old_lines:
        return False
    if "".join(old_lines) in "".join(norm(code_part(ln.text, nl)) for ln in new.body):
        return True  # same body, line breaks moved by a formatter
    kept = sum(1 for x in old_lines if x in new_lines)
    return kept / len(old_lines) >= 0.8


def classify_removed_tests(tests: list[FileDiff]):
    """Decide what happened to every removed test.

    Returns (status, partner) per removed TestBlock, keyed by id(block.line):
      "same-file"     re-added under the same name in the same file (rewritten in place)
      "in-place"      its def line was replaced one-for-one by another (a rename)
      "moved"         re-added under the same name in another file
      "renamed"       its assertions re-appear together inside an added test of another name
      "consolidated"  folded into a parametrized test: every string it used is re-added
      "gone"          none of the above
    """
    removed = [b for fd in tests for b in collect_tests(fd, "-")]
    added = [b for fd in tests for b in collect_tests(fd, "+")]
    in_place = renamed_in_place(tests)
    unclaimed = list(added)
    added_text = "\n".join(ln.text for fd in tests for ln in fd.lines("+"))
    has_param = any(PARAMETRIZE.search(ln.text) for fd in tests for ln in fd.lines("+"))
    result: dict[int, tuple[str, TestBlock | None]] = {}

    def claim(pred) -> TestBlock | None:
        for b in unclaimed:
            if pred(b):
                unclaimed.remove(b)
                return b
        return None

    for rb in removed:
        lang = language(rb.fd.path)
        # A def swapped for another def is a rename only when its body stayed as context.
        # If its assertions were removed too, it is judged by the rules below instead, so
        # swapping a strict test for an unrelated weak one cannot pass as a rename.
        if id(rb.line) in in_place and not rb.assertion_keys(lang):
            result[id(rb.line)] = ("in-place", None)
            continue
        partner = claim(lambda b: b.name == rb.name and b.fd is rb.fd)
        if partner:
            result[id(rb.line)] = ("same-file", partner)
            continue
        partner = claim(lambda b: b.name == rb.name)
        if partner:
            result[id(rb.line)] = ("moved", partner)
            continue
        if rb.assertion_keys(lang):
            partner = claim(lambda b: looks_like_rename(rb, b))
            if partner:
                result[id(rb.line)] = ("renamed", partner)
                continue
        # Calibration: mcp-unifi#195 folded five one-case tests into one parametrized test.
        # Every message and input survived as a row of the parameter table.
        lits = rb.literals()
        if has_param and lits and any(len(x) >= 6 for x in lits) and all(x in added_text for x in lits):
            result[id(rb.line)] = ("consolidated", None)
            continue
        result[id(rb.line)] = ("gone", None)
    return removed, result


def skip_targets_new_test(fd: FileDiff, hunk: list[Line], idx: int, removed_names: set[str]) -> bool:
    """True when an added skip applies only to a test this same diff introduces. Skipping a
    test that never ran before weakens nothing that existed (mcp-unifi#156, #239: a
    platform skipif on a brand-new test)."""
    lang = language(fd.path)
    ln = hunk[idx]
    target: Line | None = None
    if test_name(ln.text, lang):
        target = ln
    elif ln.text.lstrip().startswith("@"):
        indent = len(ln.text) - len(ln.text.lstrip())
        for nxt in hunk[idx + 1: idx + 20]:
            if test_name(nxt.text, lang):
                target = nxt
                break
            s = nxt.text.strip()
            if s and len(nxt.text) - len(nxt.text.lstrip()) <= indent and not s.startswith(("@", ")")):
                break
    else:
        indent = len(ln.text) - len(ln.text.lstrip())
        for prev in reversed(hunk[:idx]):
            t = test_name(prev.text, lang)
            if t and t[0] < indent:
                target = prev
                break
    if target is None:
        # Module-level (pytestmark, describe.skip): new only if the whole file is new and
        # none of its tests came from elsewhere.
        if not fd.added:
            return False
        names = {b.name for b in collect_tests(fd, "+")}
        return not (names & removed_names)
    t = test_name(target.text, lang)
    return target.kind == "+" and t is not None and t[1] not in removed_names


def detect(fds: list[FileDiff]) -> list[Finding]:
    """The findings for one diff. Reads the diff only, never the repository tree, so git
    mode and --diff-file mode decide identically on the same diff."""
    findings: list[Finding] = []
    tests = [fd for fd in fds if not fd.binary and in_scope(fd)]

    # ---- a test file renamed to where the runner no longer collects it ----------------
    # `git mv tests/test_calc.py attic/calc_checks.py` keeps every line and runs none of
    # them (review finding F2). Reported as a deletion; the file is not analysed further.
    for fd in [fd for fd in tests if fd.old_path and fd.new_path and fd.old_path != fd.new_path
               and is_collected(fd.old_path) and not is_collected(fd.new_path)]:
        tests.remove(fd)
        findings.append(Finding(
            "test-deleted", fd.new_path, 1,
            f"The test file {fd.old_path} was renamed to {fd.new_path}, where the test "
            f"runner no longer collects it, so its tests stop running.",
        ))
    configs = [fd for fd in fds if not fd.binary and is_coverage_config(fd.path)]

    removed_tests, status = classify_removed_tests(tests)
    removed_names = {b.name for b in removed_tests}

    # ---- moves: identical (normalized) assertion lines added in another file ----------
    added_assert_by_file: dict[str, Counter] = {}
    for fd in tests:
        lang = language(fd.path)
        c: Counter = Counter()
        for ln in fd.lines("+"):
            if count_assertions(ln.text, lang):
                c[norm(code_part(ln.text, lang))] += count_assertions(ln.text, lang)
        added_assert_by_file[fd.path] = c

    # Lines inside a removed test that is gone (reported as test-deleted) or that left
    # this file (moved, renamed, consolidated) are not counted as this file's loss: the
    # first is reported once under its own name, the second is judged where it landed.
    accounted: set[int] = set()
    for rb in removed_tests:
        st, _ = status[id(rb.line)]
        if st in ("gone", "moved", "renamed", "consolidated"):
            accounted.update(id(ln) for ln in rb.body)

    # ---- 1. net-assertion-loss -------------------------------------------------------
    for fd in tests:
        if fd.deleted:
            continue
        lang = language(fd.path)
        removed_n = added_n = 0
        first_loss: Line | None = None
        for ln in fd.lines("-"):
            if id(ln) in accounted:
                continue
            n = count_assertions(ln.text, lang)
            if not n:
                continue
            key = norm(code_part(ln.text, lang))
            # A line removed here and added verbatim in another test file moved, not vanished.
            for other, c in added_assert_by_file.items():
                if other != fd.path and c[key] > 0:
                    take = min(n, c[key])
                    c[key] -= take
                    n -= take
                    if n == 0:
                        break
            removed_n += n
            if n and first_loss is None:
                first_loss = ln
        for ln in fd.lines("+"):
            added_n += count_assertions(ln.text, lang)
        # Tests that arrived from another file are judged against their old selves below,
        # not credited here, or a move would mask a loss elsewhere in this file.
        for rb in removed_tests:
            st, partner = status[id(rb.line)]
            if st in ("moved", "renamed") and partner is not None and partner.fd is fd:
                added_n -= partner.n_assertions(lang)
        if removed_n > added_n:
            line = first_loss.new_no if first_loss else 1
            findings.append(Finding(
                "net-assertion-loss", fd.path, max(1, line),
                f"This test file loses {removed_n - added_n} assertion(s) on net "
                f"({removed_n} removed, {max(added_n, 0)} added), so it now checks less than before.",
            ))
    for rb in removed_tests:
        st, partner = status[id(rb.line)]
        if st in ("moved", "renamed") and partner is not None:
            before = rb.n_assertions(language(rb.fd.path))
            after = partner.n_assertions(language(partner.fd.path))
            if after < before:
                findings.append(Finding(
                    "net-assertion-loss", partner.fd.path, max(1, partner.line.new_no),
                    f"The test `{partner.name}` arrived from `{rb.name}` in {rb.fd.path} with "
                    f"fewer assertions ({before} before, {after} now).",
                ))

    # ---- 2. loosened-assertion -------------------------------------------------------
    # An added assertion over literals only cannot fail, whatever the code does.
    for fd in tests:
        lang = language(fd.path)
        for ln in fd.lines("+"):
            if is_tautology(ln.text, lang):
                findings.append(Finding(
                    "loosened-assertion", fd.path, ln.new_no,
                    f"`{ln.text.strip()[:80]}` compares constants, so it passes whatever "
                    f"the code does.",
                ))
    for fd in tests:
        lang = language(fd.path)
        for hunk in fd.hunks:
            for removed, added in change_blocks(hunk):
                if not removed or not added:
                    continue
                # Exact check replaced by a weaker one: only when the change dropped exact
                # checks overall AND added weak ones, so adding an `is not None` next to a
                # kept `==` (a strengthening) does not fire.
                ex_r = [ln for ln in removed if is_exact(ln.text, lang)]
                ex_a = [ln for ln in added if is_exact(ln.text, lang)]
                wk_r = [ln for ln in removed if is_weak(ln.text, lang)]
                wk_a = [ln for ln in added if is_weak(ln.text, lang)]
                lost = len(ex_r) - len(ex_a)
                gained = len(wk_a) - len(wk_r)
                if lost > 0 and gained > 0:
                    removed_keys = Counter(norm(code_part(ln.text, lang)) for ln in wk_r)
                    fresh = []
                    for ln in wk_a:
                        k = norm(code_part(ln.text, lang))
                        if removed_keys[k]:
                            removed_keys[k] -= 1
                        else:
                            fresh.append(ln)
                    # The exact checks that lost their counterpart: those whose subject
                    # (the part before the comparison) no longer has an exact check.
                    kept = Counter(check_subject(ln.text, lang) for ln in ex_a)
                    orphans = []
                    for ln in ex_r:
                        subj = check_subject(ln.text, lang)
                        if kept[subj]:
                            kept[subj] -= 1
                        else:
                            orphans.append(ln)
                    # Pair on a shared subject first, so the message quotes the check that
                    # was really replaced; leftovers pair in order.
                    pairs = []
                    for new in list(fresh):
                        old = next((o for o in orphans if check_subject(o.text, lang)
                                    and check_subject(o.text, lang) in norm(new.text)), None)
                        if old is not None:
                            orphans.remove(old)
                            fresh.remove(new)
                            pairs.append((old, new))
                    pairs += list(zip(orphans, fresh))
                    pairs = sorted(pairs[:lost], key=lambda p: p[1].new_no)
                    for old, new in pairs:
                        findings.append(Finding(
                            "loosened-assertion", fd.path, new.new_no,
                            f"An exact check (`{old.text.strip()[:80]}`) was replaced by a "
                            f"weaker one (`{new.text.strip()[:80]}`) that passes for more results.",
                        ))
                # Specific expected error widened to a generic one.
                r_exc = [(ln, RAISES_ARG.search(ln.text)) for ln in removed]
                a_exc = [(ln, RAISES_ARG.search(ln.text)) for ln in added]
                r_spec = [(ln, m) for ln, m in r_exc if m and m.group(1) not in GENERIC_EXC]
                a_gen = [(ln, m) for ln, m in a_exc if m and m.group(1) in GENERIC_EXC]
                r_gen = [1 for _, m in r_exc if m and m.group(1) in GENERIC_EXC]
                for (old, om), (new, nm) in zip(r_spec, a_gen[len(r_gen):]):
                    findings.append(Finding(
                        "loosened-assertion", fd.path, new.new_no,
                        f"The expected error was widened from {om.group(1)} to {nm.group(1)}, "
                        f"so the test now passes on any error, including the wrong one.",
                    ))
                # pytest.raises(..., match=...) lost its match.
                r_m = [ln for ln in removed if RAISES_MATCH.search(ln.text)]
                a_raises = [ln for ln in added if re.search(r"\bpytest\.raises\s*\(", ln.text)]
                a_m = [ln for ln in added if RAISES_MATCH.search(ln.text)]
                a_nomatch = [ln for ln in a_raises if not RAISES_MATCH.search(ln.text)]
                for old, new in zip(r_m[len(a_m):], a_nomatch):
                    findings.append(Finding(
                        "loosened-assertion", fd.path, new.new_no,
                        "The expected error lost its `match=` message check, so any error of "
                        "that type now passes.",
                    ))
                # approx tolerance widened.
                r_tol = [(ln, dict(APPROX_KW.findall(ln.text))) for ln in removed if "approx" in ln.text]
                a_tol = [(ln, dict(APPROX_KW.findall(ln.text))) for ln in added if "approx" in ln.text]
                for (old, ot), (new, nt) in zip(r_tol, a_tol):
                    for kw in ("rel", "abs"):
                        o, n = _num(ot.get(kw, "")), _num(nt.get(kw, ""))
                        if o is not None and n is not None and n > o:
                            findings.append(Finding(
                                "loosened-assertion", fd.path, new.new_no,
                                f"The approx tolerance {kw}= was widened from {ot[kw]} to "
                                f"{nt[kw]}, so a less accurate result now passes.",
                            ))
                # toBeCloseTo precision lowered.
                r_ct = [(ln, close_to_digits(ln.text)) for ln in removed if CLOSE_TO.search(ln.text)]
                a_ct = [(ln, close_to_digits(ln.text)) for ln in added if CLOSE_TO.search(ln.text)]
                for (old, od), (new, nd) in zip(r_ct, a_ct):
                    if od is not None and nd is not None and nd < od:
                        findings.append(Finding(
                            "loosened-assertion", fd.path, new.new_no,
                            f"toBeCloseTo precision was lowered from {od} to {nd} digits, so a "
                            f"less accurate result now passes.",
                        ))

    # ---- 3. skip-added ---------------------------------------------------------------
    removed_skips: Counter = Counter()
    for fd in tests:
        lang = language(fd.path)
        for ln in fd.lines("-"):
            if skip_marker(ln.text, lang):
                removed_skips[norm(code_part(ln.text, lang))] += 1
    for fd in tests:
        if fd.deleted:
            continue
        lang = language(fd.path)
        for hunk in fd.hunks:
            for idx, ln in enumerate(hunk):
                if ln.kind != "+":
                    continue
                marker = skip_marker(ln.text, lang)
                if not marker:
                    continue
                key = norm(code_part(ln.text, lang))
                if removed_skips[key]:
                    removed_skips[key] -= 1  # moved or re-indented, not new
                    continue
                focus = ".only" in marker or marker.startswith(("fit", "fdescribe"))
                # A focus always fires: it silences every OTHER test, new or old.
                if not focus and skip_targets_new_test(fd, hunk, idx, removed_names):
                    continue
                what = ("focuses the run on one test, so every other test stops running"
                        if focus else "stops an existing test from running or from being able to fail")
                findings.append(Finding(
                    "skip-added", fd.path, ln.new_no,
                    f"`{marker.strip()}` was added, which {what}.",
                ))

    # ---- 4. test-deleted -------------------------------------------------------------
    deleted_source_stems = {
        fd.old_path.split("/")[-1].rsplit(".", 1)[0]
        for fd in fds if fd.deleted and fd.old_path and not is_test_file(fd.old_path)
    }

    def subject_remains(path: str) -> bool:
        # Only a source file deleted in THIS diff excuses a deleted test file. The tree
        # never does: test files are often named for a feature (test_security.py) rather
        # than a module, so "no file of that name remains" proved nothing, and in git mode
        # it let 48 of mcp-unifi's 94 test files be deleted whole with a green guard
        # (review finding F1).
        stem = subject_stem(path)
        return stem is None or stem not in deleted_source_stems

    gone_by_file: dict[str, list[TestBlock]] = {}
    for rb in removed_tests:
        if status[id(rb.line)][0] == "gone":
            gone_by_file.setdefault(rb.fd.path, []).append(rb)
    for fd in tests:
        if fd.deleted:
            gone = gone_by_file.get(fd.path, [])
            if gone and subject_remains(fd.old_path or fd.path):
                findings.append(Finding(
                    "test-deleted", fd.old_path or fd.path, 1,
                    f"This whole test file was deleted ({len(gone)} test(s) not re-added "
                    f"anywhere) while the code it tests is still there.",
                ))
            continue
        for rb in gone_by_file.get(fd.path, []):
            findings.append(Finding(
                "test-deleted", fd.path, max(1, rb.line.new_no),
                f"The test `{rb.name}` was removed and is not re-added anywhere in this change.",
            ))

    # A `class Test*` renamed to a name pytest does not collect: every method in it stops
    # running while the diff shows one changed line.
    readded_classes = {
        m.group(1) for fd in tests if language(fd.path) == "py" for ln in fd.lines("+")
        if (m := PY_TEST_CLASS.match(ln.text))
    }
    for fd in tests:
        if language(fd.path) != "py":
            continue
        for hunk in fd.hunks:
            for removed, added in change_blocks(hunk):
                old = [m.group(1) for ln in removed if (m := PY_TEST_CLASS.match(ln.text))]
                new = [(ln, m.group(1)) for ln in added
                       if (m := PY_ANY_CLASS.match(ln.text)) and not m.group(1).startswith("Test")
                       and "TestCase" not in ln.text]
                for name, (ln, to) in zip([n for n in old if n not in readded_classes], new):
                    findings.append(Finding(
                        "test-deleted", fd.path, ln.new_no,
                        f"The test class {name} was renamed to {to}, which pytest does not "
                        f"collect, so every test in it stops running.",
                    ))

    # ---- 5. coverage-floor-lowered ---------------------------------------------------
    findings.extend(coverage_findings(configs, fds))

    findings.sort(key=lambda f: (f.path, f.line, f.kind))
    return findings


PY_FLOOR = re.compile(
    r"(?:\bfail_under\s*=\s*|--cov-fail-under[= ]\s*|--fail-under[= ]\s*)[\"']?(\d+(?:\.\d+)?)"
)
JS_FLOOR = re.compile(
    r"[\"']?\b(lines|statements|functions|branches)\b[\"']?\s*:\s*(-?\d+(?:\.\d+)?)"
)
WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$|(^|/)Makefile$|(^|/)noxfile\.py$")


def coverage_findings(configs: list[FileDiff], fds: list[FileDiff]) -> list[Finding]:
    out: list[Finding] = []
    # The Python floor is one number wherever it lives, so it is matched across files:
    # moving fail_under from .coveragerc to pyproject.toml is not a lowering.
    py_files = configs + [fd for fd in fds if WORKFLOW_RE.search(fd.path) and fd not in configs]
    r_py: list[tuple[FileDiff, Line, float]] = []
    a_py: list[tuple[FileDiff, Line, float]] = []
    for fd in py_files:
        for ln in fd.lines("-"):
            for v in PY_FLOOR.findall(ln.text):
                r_py.append((fd, ln, float(v)))
        for ln in fd.lines("+"):
            for v in PY_FLOOR.findall(ln.text):
                a_py.append((fd, ln, float(v)))
    for i, (fd, ln, old) in enumerate(r_py):
        if i < len(a_py):
            nfd, nln, new = a_py[i]
            if new < old:
                out.append(Finding(
                    "coverage-floor-lowered", nfd.path, nln.new_no,
                    f"The coverage floor was lowered from {old:g}% to {new:g}%, so less "
                    f"tested code now passes CI.",
                ))
        else:
            out.append(Finding(
                "coverage-floor-lowered", fd.path, max(1, ln.new_no),
                f"The coverage floor of {old:g}% was removed and not set again anywhere "
                f"in this change.",
            ))
    for fd in configs:
        if language(fd.path) is None and not fd.path.endswith("package.json"):
            continue
        r_js: dict[str, list[tuple[Line, float]]] = {}
        a_js: dict[str, list[tuple[Line, float]]] = {}
        for kind, bucket in (("-", r_js), ("+", a_js)):
            for ln in fd.lines(kind):
                for key, v in JS_FLOOR.findall(ln.text):
                    bucket.setdefault(key, []).append((ln, float(v)))
        for key, olds in r_js.items():
            news = a_js.get(key, [])
            for i, (ln, old) in enumerate(olds):
                if i < len(news):
                    nln, new = news[i]
                    if new < old:
                        out.append(Finding(
                            "coverage-floor-lowered", fd.path, nln.new_no,
                            f"The {key} coverage threshold was lowered from {old:g} to "
                            f"{new:g}, so less tested code now passes CI.",
                        ))
                else:
                    out.append(Finding(
                        "coverage-floor-lowered", fd.path, max(1, ln.new_no),
                        f"The {key} coverage threshold of {old:g} was removed.",
                    ))
    return out


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def _esc_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _esc_prop(s: str) -> str:
    return _esc_data(s).replace(":", "%3A").replace(",", "%2C")


def _md(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def verdict_line(n_findings: int, n_tests: int, n_configs: int, n_files: int) -> str:
    line = f"assertion-guard: {n_findings} finding(s) in {n_tests} test file(s) scanned"
    if n_configs:
        line += f", plus {n_configs} coverage config file(s)"
    if n_files == 0:
        line += " (the diff is empty: no files changed at all)"
    elif n_tests == 0:
        line += " (no test files changed in this diff, so no test could have been weakened)"
    return line


def write_summary(path: str, findings: list[Finding], verdict: str) -> None:
    rows = ["## assertion-guard", ""]
    if findings:
        rows += [
            "A red guard means this change makes at least one test easier to pass. "
            "Read each row before merging.",
            "",
            "| Finding | File | Line | What changed |",
            "|---|---|---|---|",
        ]
        rows += [f"| `{f.kind}` | `{_md(f.path)}` | {f.line} | {_md(f.message)} |" for f in findings]
    else:
        rows.append("No weakened tests found.")
    rows += ["", f"`{verdict}`", ""]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")


def git_diff(base: str, head: str) -> str:
    # --text, --no-textconv and --no-ext-diff: a PR can add `tests/** -diff` or a diff
    # driver to .gitattributes, and without these its own test changes would arrive as
    # "Binary files differ" or as converted text (review finding F3).
    cmd = ["git", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "-M",
           f"{base}...{head}"]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed: {p.stderr.strip()[:300]}")
    return p.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--diff-file", help="read a unified diff from this file instead of git")
    ap.add_argument("--base", default=os.environ.get("GUARD_BASE_SHA", ""))
    ap.add_argument("--head", default=os.environ.get("GUARD_HEAD_SHA", ""))
    ap.add_argument("--summary-file", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    ap.add_argument("--no-annotations", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.diff_file:
            with open(args.diff_file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            if not args.base or not args.head:
                raise RuntimeError(
                    "no diff to read: pass --diff-file, or --base and --head (the action "
                    "fills these from the pull_request event)"
                )
            text = git_diff(args.base, args.head)
        fds = parse_diff(text)
        if text.strip() and not fds:
            raise RuntimeError("input is not empty but contains no `diff --git` sections")
        unreadable = [fd.path for fd in fds if fd.binary and in_scope(fd)]
        if unreadable:
            raise RuntimeError(
                "test file(s) arrived as binary, so their changes cannot be read: "
                + ", ".join(unreadable[:5])
            )
        findings = detect(fds)
    except (OSError, RuntimeError) as exc:
        print(f"assertion-guard: ERROR: {exc}")
        return 2
    except Exception as exc:  # a crash must never exit 1 and read as "findings"
        print(f"assertion-guard: ERROR: internal failure, no verdict: {type(exc).__name__}: {exc}")
        return 2

    n_tests = sum(1 for fd in fds if not fd.binary and in_scope(fd))
    n_cfg = sum(1 for fd in fds if not fd.binary and is_coverage_config(fd.path))
    verdict = verdict_line(len(findings), n_tests, n_cfg, len(fds))

    for f in findings:
        print(f"{f.kind}\t{f.path}:{f.line}\t{f.message}")
        if not args.no_annotations:
            print(
                f"::error file={_esc_prop(f.path)},line={f.line},"
                f"title={_esc_prop('assertion-guard ' + f.kind)}::{_esc_data(f.message)}"
            )
    if args.summary_file:
        try:
            write_summary(args.summary_file, findings, verdict)
        except OSError as exc:
            print(f"assertion-guard: warning: could not write step summary: {exc}")
    print(verdict)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
