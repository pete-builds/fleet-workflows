#!/usr/bin/env python3
"""Unit tests for assertion-guard. Run: python3 -B assertion-guard/test_guard.py

Most cases build a throwaway git repo, commit a "before" and an "after", and feed the real
`git diff` output to the guard, so every case exercises the same parser a pull request
does. Each rule is tested in both directions: a weakening it must catch, and a nearby
legitimate change it must leave alone.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guard  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "smoke", "assertion-guard")


def git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class Repo:
    """A temporary git repo with a base commit and a head commit."""

    def __init__(self, before: dict[str, str], after: dict[str, str | None]):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "config", "user.email", "t@example.invalid")
        git(self.dir, "config", "user.name", "t")
        self._write(before)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", "base", "--allow-empty")
        self.base = git(self.dir, "rev-parse", "HEAD").strip()
        self._write(after)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", "head", "--allow-empty")
        self.head = git(self.dir, "rev-parse", "HEAD").strip()

    def _write(self, files: dict[str, str | None]) -> None:
        for rel, content in files.items():
            p = os.path.join(self.dir, rel)
            if content is None:
                os.remove(p)
                continue
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(content).lstrip("\n"))

    def diff(self) -> str:
        return git(self.dir, "diff", "--no-color", "-M", f"{self.base}...{self.head}")

    def close(self) -> None:
        self._tmp.cleanup()


def findings(before: dict[str, str], after: dict[str, str | None]) -> list[guard.Finding]:
    repo = Repo(before, after)
    try:
        text = repo.diff()
        if not text.strip():
            raise AssertionError("case produced an empty diff: the edit matched nothing")
        return guard.detect(guard.parse_diff(text))
    finally:
        repo.close()


def kinds(fs: list[guard.Finding]) -> list[str]:
    return sorted(f.kind for f in fs)


PY_BASE = {
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    # Dedented here, once, so the cases' str.replace / split calls match real text.
    "tests/test_calc.py": textwrap.dedent("""
        import pytest

        from src.calc import add


        def test_add():
            assert add(1, 2) == 3
            assert add(2, 2) == 4


        def test_neg():
            assert add(-1, 1) == 0
        """).lstrip("\n"),
}


class Fixture(unittest.TestCase):
    def test_every_case_really_changes_the_file(self):
        # A control on the controls: a replace() that matches nothing makes "after"
        # equal "before", and every negative case would then pass vacuously.
        self.assertTrue(PY_BASE["tests/test_calc.py"].startswith("import pytest\n"))
        self.assertIn("\n\ndef test_neg", PY_BASE["tests/test_calc.py"])
        self.assertIn("    assert add(2, 2) == 4\n", PY_BASE["tests/test_calc.py"])


class ParseTests(unittest.TestCase):
    def test_line_numbers_and_kinds(self):
        text = textwrap.dedent("""\
            diff --git a/tests/test_x.py b/tests/test_x.py
            index 1..2 100644
            --- a/tests/test_x.py
            +++ b/tests/test_x.py
            @@ -10,3 +10,3 @@ def f():
             keep
            -old
            +new
             tail
            """)
        (fd,) = guard.parse_diff(text)
        self.assertEqual(fd.path, "tests/test_x.py")
        (removed,) = list(fd.lines("-"))
        (added,) = list(fd.lines("+"))
        self.assertEqual((removed.old_no, removed.new_no), (11, 11))
        self.assertEqual(added.new_no, 11)

    def test_new_deleted_renamed(self):
        repo = Repo(
            {"a/test_one.py": "x = 1\n", "b/gone_test.py": "y = 2\n"},
            {"a/test_one.py": None, "a/test_two.py": "x = 1\n", "b/gone_test.py": None,
             "c/test_new.py": "z = 3\n"},
        )
        try:
            fds = {(fd.old_path, fd.new_path) for fd in guard.parse_diff(repo.diff())}
        finally:
            repo.close()
        self.assertIn(("a/test_one.py", "a/test_two.py"), fds)
        self.assertIn(("b/gone_test.py", None), fds)
        self.assertIn((None, "c/test_new.py"), fds)

    def test_file_classification(self):
        yes = ["tests/helpers.py", "pkg/test_mod.py", "pkg/mod_test.py", "conftest.py",
               "src/a.test.ts", "src/a.spec.js", "src/__tests__/a.tsx", "web/x.test.mjs"]
        no = ["src/mod.py", "src/testing.py", "node_modules/x/a.test.js", "tests/data.json",
              "src/contest.ts", "README.md", "x_test.go"]
        for p in yes:
            self.assertTrue(guard.is_test_file(p), p)
        for p in no:
            self.assertFalse(guard.is_test_file(p), p)
        for p in ["pyproject.toml", "setup.cfg", ".coveragerc", "pytest.ini", "tox.ini",
                  "package.json", "vitest.config.ts", "jest.config.js", "vite.config.mts"]:
            self.assertTrue(guard.is_coverage_config(p), p)
        self.assertFalse(guard.is_coverage_config("src/config.ts"))

    def test_assert_condition_ignores_commas_inside_calls(self):
        self.assertEqual(guard.assert_condition("assert f(6, 3) == 2").strip(), "f(6, 3) == 2")
        self.assertEqual(guard.assert_condition('assert x, "a, b"').strip(), "x")
        self.assertFalse(guard.is_weak("assert f(6, 3) == 2", "py"))
        self.assertTrue(guard.is_weak("assert f(6, 3)", "py"))
        self.assertTrue(guard.is_weak('assert ok, "msg, with comma"', "py"))


class NetAssertionLossTests(unittest.TestCase):
    def test_fires_when_a_surviving_test_loses_an_assertion(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace(
            "    assert add(2, 2) == 4\n", "")
        self.assertEqual(kinds(findings(PY_BASE, after)), ["net-assertion-loss"])

    def test_quiet_when_assertions_are_added(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"] + "    assert add(0, 0) == 0\n"
        self.assertEqual(findings(PY_BASE, after), [])

    def test_quiet_on_reformatting(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace(
            "    assert add(1, 2) == 3\n", "    assert (\n        add(1, 2)\n        == 3\n    )\n")
        self.assertEqual(findings(PY_BASE, after), [])

    def test_single_element_unpack_counts_as_an_assertion(self):
        before = {"tests/test_w.py": "def test_w():\n    w = get()\n    assert len(w) == 1\n"}
        after = {"tests/test_w.py": "def test_w():\n    (w,) = get()\n"}
        self.assertEqual(findings(before, after), [])

    def test_js_expect_loss(self):
        before = {"src/a.test.ts": "it('a', () => {\n  expect(f()).toBe(1);\n  expect(g()).toBe(2);\n});\n"}
        after = {"src/a.test.ts": "it('a', () => {\n  expect(f()).toBe(1);\n});\n"}
        self.assertEqual(kinds(findings(before, after)), ["net-assertion-loss"])

    def test_moved_test_that_lost_assertions_on_the_way_fires(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].split("def test_add")[0]
        after["tests/test_calc.py"] += "def test_neg():\n    assert add(-1, 1) == 0\n"
        after["tests/test_other.py"] = "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        fs = findings(PY_BASE, after)
        self.assertEqual(kinds(fs), ["net-assertion-loss"])
        self.assertEqual(fs[0].path, "tests/test_other.py")

    def test_approx_inside_assert_counts_once(self):
        before = {"tests/test_f.py": "def test_f():\n    assert f() == pytest.approx(0.5)\n"}
        after = {"tests/test_f.py": "def test_f():\n    assert f() == 0.5\n"}
        self.assertEqual(findings(before, after), [])


class LoosenedAssertionTests(unittest.TestCase):
    def _swap(self, old: str, new: str, path: str = "tests/test_s.py") -> list[str]:
        if path.endswith(".py"):
            b = {path: f"def test_s():\n    {old}\n"}
            a = {path: f"def test_s():\n    {new}\n"}
        else:
            b = {path: f"it('s', () => {{\n  {old}\n}});\n"}
            a = {path: f"it('s', () => {{\n  {new}\n}});\n"}
        return kinds(findings(b, a))

    def test_python_equality_to_weaker(self):
        for new in ("assert r is not None", "assert r", "assert 3 in r",
                    "self.assertTrue(r)", "self.assertIsNotNone(r)", "self.assertIn(3, r)"):
            with self.subTest(new=new):
                self.assertEqual(self._swap("assert r == 3", new), ["loosened-assertion"])
        self.assertEqual(self._swap("self.assertEqual(r, 3)", "self.assertTrue(r)"),
                         ["loosened-assertion"])

    def test_python_equality_kept_is_quiet(self):
        self.assertEqual(self._swap("assert r == 3", "assert r == 4"), [])
        self.assertEqual(self._swap("assert r == 3", "assert r is not None\n    assert r == 3"), [])
        self.assertEqual(self._swap("assert r is not None", "assert r == 3"), [])

    def test_js_exact_to_weaker(self):
        for old, new in (("expect(r).toBe(3);", "expect(r).toBeDefined();"),
                         ("expect(r).toEqual([1]);", "expect(r).toContain(1);"),
                         ("expect(r).toStrictEqual({a: 1});", "expect(r).toBeTruthy();"),
                         ("expect(s).toBe('abc');", "expect(s).toMatch(/a/);")):
            with self.subTest(old=old):
                self.assertEqual(self._swap(old, new, "src/s.test.ts"), ["loosened-assertion"])
        self.assertEqual(self._swap("expect(r).toBeTruthy();", "expect(r).toBe(3);", "src/s.test.ts"), [])

    def test_raises_widened(self):
        b = {"tests/test_r.py": "def test_r():\n    with pytest.raises(KeyError):\n        f()\n"}
        for wide in ("Exception", "BaseException"):
            a = {"tests/test_r.py": f"def test_r():\n    with pytest.raises({wide}):\n        f()\n"}
            self.assertEqual(kinds(findings(b, a)), ["loosened-assertion"])
        a = {"tests/test_r.py": "def test_r():\n    with pytest.raises(LookupError):\n        f()\n"}
        self.assertEqual(findings(b, a), [])

    def test_raises_lost_match(self):
        b = {"tests/test_r.py": "def test_r():\n    with pytest.raises(KeyError, match='x'):\n        f()\n"}
        a = {"tests/test_r.py": "def test_r():\n    with pytest.raises(KeyError):\n        f()\n"}
        self.assertEqual(kinds(findings(b, a)), ["loosened-assertion"])
        self.assertEqual(findings(a, b), [])

    def test_approx_tolerance(self):
        self.assertEqual(self._swap("assert x == pytest.approx(1, rel=1e-6)",
                                    "assert x == pytest.approx(1, rel=1e-3)"), ["loosened-assertion"])
        self.assertEqual(self._swap("assert x == pytest.approx(1, abs=0.01)",
                                    "assert x == pytest.approx(1, abs=0.5)"), ["loosened-assertion"])
        self.assertEqual(self._swap("assert x == pytest.approx(1, rel=1e-3)",
                                    "assert x == pytest.approx(1, rel=1e-6)"), [])

    def test_to_be_close_to(self):
        p = "src/c.test.ts"
        self.assertEqual(self._swap("expect(x).toBeCloseTo(1, 5);", "expect(x).toBeCloseTo(1, 2);", p),
                         ["loosened-assertion"])
        self.assertEqual(self._swap("expect(x).toBeCloseTo(1, 5);", "expect(x).toBeCloseTo(1);", p),
                         ["loosened-assertion"])
        self.assertEqual(self._swap("expect(x).toBeCloseTo(1, 2);", "expect(x).toBeCloseTo(1, 6);", p), [])


class SkipAddedTests(unittest.TestCase):
    def test_skip_on_existing_test_fires(self):
        for deco in ("@pytest.mark.skip", "@pytest.mark.skipif(True, reason='x')",
                     "@pytest.mark.xfail", "@unittest.skip('x')"):
            with self.subTest(deco=deco):
                after = dict(PY_BASE)
                after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace(
                    "def test_neg", f"{deco}\ndef test_neg")
                self.assertEqual(kinds(findings(PY_BASE, after)), ["skip-added"])

    def test_imperative_skip_inside_existing_test_fires(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace(
            "def test_neg():\n", "def test_neg():\n    pytest.skip('later')\n")
        self.assertEqual(kinds(findings(PY_BASE, after)), ["skip-added"])

    def test_skip_on_a_brand_new_test_is_quiet(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"] + (
            "\n\n@pytest.mark.skipif(os.name == 'nt', reason='posix')\ndef test_posix():\n    assert add(1, 1) == 2\n")
        self.assertEqual(findings(PY_BASE, after), [])

    def test_module_skip_in_existing_file_fires_but_not_in_new_file(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace(
            "import pytest\n", "import pytest\n\npytestmark = pytest.mark.skip\n")
        self.assertEqual(kinds(findings(PY_BASE, after)), ["skip-added"])
        after = dict(PY_BASE)
        after["tests/test_new.py"] = "import pytest\n\npytestmark = pytest.mark.skip\n\n\ndef test_n():\n    assert 1\n"
        self.assertEqual(findings(PY_BASE, after), [])

    def test_js_skip_and_focus(self):
        b = {"src/a.test.ts": "it('a', () => {\n  expect(1).toBe(1);\n});\n"}
        for new in ("it.skip('a'", "xit('a'", "it.only('a'"):
            with self.subTest(new=new):
                a = {"src/a.test.ts": b["src/a.test.ts"].replace("it('a'", new)}
                self.assertEqual(kinds(findings(b, a)), ["skip-added"])

    def test_focus_on_a_new_test_still_fires(self):
        b = {"src/a.test.ts": "it('a', () => {\n  expect(1).toBe(1);\n});\n"}
        a = {"src/a.test.ts": b["src/a.test.ts"] + "it.only('b', () => {\n  expect(2).toBe(2);\n});\n"}
        self.assertEqual(kinds(findings(b, a)), ["skip-added"])

    def test_moved_skip_is_quiet(self):
        b = {"tests/test_a.py": "import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    assert 1 == 1\n"}
        a = {"tests/test_a.py": None,
             "tests/test_b.py": "import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    assert 1 == 1\n"}
        self.assertEqual(findings(b, a), [])


class TestDeletedTests(unittest.TestCase):
    def test_removed_test_fires(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].split("\n\ndef test_neg")[0] + "\n"
        self.assertEqual(kinds(findings(PY_BASE, after)), ["test-deleted"])

    def test_moved_between_files_is_quiet(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].split("\n\ndef test_neg")[0] + "\n"
        after["tests/test_neg.py"] = "from src.calc import add\n\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"
        self.assertEqual(findings(PY_BASE, after), [])

    def test_moved_by_name_with_an_edited_assertion_is_quiet(self):
        # Only name matching resolves this: the body changed, so body matching cannot.
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].split("\n\ndef test_neg")[0] + "\n"
        after["tests/test_neg.py"] = "from src.calc import add\n\n\ndef test_neg():\n    assert add(-2, 2) == 0\n"
        self.assertEqual(findings(PY_BASE, after), [])

    def test_renamed_in_place_is_quiet(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace("def test_neg", "def test_negative")
        self.assertEqual(findings(PY_BASE, after), [])

    def test_renamed_and_moved_is_quiet(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].split("\n\ndef test_neg")[0] + "\n"
        after["tests/test_signs.py"] = "from src.calc import add\n\n\ndef test_signs_cancel():\n    assert add(-1, 1) == 0\n"
        self.assertEqual(findings(PY_BASE, after), [])

    def test_strict_test_swapped_for_unrelated_weak_one_fires(self):
        after = dict(PY_BASE)
        after["tests/test_calc.py"] = PY_BASE["tests/test_calc.py"].replace(
            "def test_neg():\n    assert add(-1, 1) == 0", "def test_runs():\n    add(-1, 1)")
        self.assertIn("test-deleted", kinds(findings(PY_BASE, after)))

    def test_parametrize_consolidation_is_quiet(self):
        b = {"tests/test_p.py": (
            "def test_empty():\n    with pytest.raises(ValueError, match='is empty'):\n        load('')\n\n\n"
            "def test_garbage():\n    with pytest.raises(ValueError, match='not a certificate'):\n        load('garbage-in')\n")}
        a = {"tests/test_p.py": (
            "@pytest.mark.parametrize(('src', 'msg'), [('', 'is empty'), ('garbage-in', 'not a certificate')])\n"
            "def test_load_fails(src, msg):\n    with pytest.raises(ValueError, match=msg):\n        load(src)\n")}
        self.assertEqual(findings(b, a), [])
        # Control: drop one row and the dropped case is a deleted test.
        a2 = {"tests/test_p.py": a["tests/test_p.py"].replace(", ('garbage-in', 'not a certificate')", "")}
        self.assertEqual(kinds(findings(b, a2)), ["test-deleted"])

    def test_whole_file_deleted_fires_unless_subject_went_too(self):
        b = {"src/parse.py": "def parse(t):\n    return int(t)\n",
             "tests/test_parse.py": "def test_parse():\n    assert parse('1') == 1\n"}
        self.assertEqual(kinds(findings(b, {"tests/test_parse.py": None})), ["test-deleted"])
        self.assertEqual(findings(b, {"tests/test_parse.py": None, "src/parse.py": None}), [])

    def test_js_test_removed(self):
        b = {"src/a.test.ts": "it('a', () => {\n  expect(1).toBe(1);\n});\n\nit('b', () => {\n  expect(2).toBe(2);\n});\n"}
        a = {"src/a.test.ts": "it('a', () => {\n  expect(1).toBe(1);\n});\n"}
        self.assertEqual(kinds(findings(b, a)), ["test-deleted"])


class CoverageFloorTests(unittest.TestCase):
    def test_python_floor(self):
        b = {"pyproject.toml": "[tool.coverage.report]\nfail_under = 90\n"}
        self.assertEqual(kinds(findings(b, {"pyproject.toml": "[tool.coverage.report]\nfail_under = 80\n"})),
                         ["coverage-floor-lowered"])
        self.assertEqual(findings(b, {"pyproject.toml": "[tool.coverage.report]\nfail_under = 95\n"}), [])
        self.assertEqual(kinds(findings(b, {"pyproject.toml": "[tool.coverage.report]\n"})),
                         ["coverage-floor-lowered"])

    def test_floor_moved_between_files_is_quiet(self):
        b = {"pyproject.toml": "[tool.coverage.report]\nfail_under = 90\n"}
        a = {"pyproject.toml": "[project]\n", ".coveragerc": "[report]\nfail_under = 90\n"}
        self.assertEqual(findings(b, a), [])

    def test_cov_fail_under_flag(self):
        b = {"pytest.ini": "[pytest]\naddopts = --cov=src --cov-fail-under=85\n"}
        a = {"pytest.ini": "[pytest]\naddopts = --cov=src --cov-fail-under=60\n"}
        self.assertEqual(kinds(findings(b, a)), ["coverage-floor-lowered"])

    def test_js_thresholds(self):
        b = {"package.json": '{\n  "jest": {\n    "coverageThreshold": {\n      "global": {\n        "branches": 80\n      }\n    }\n  }\n}\n'}
        a = {"package.json": b["package.json"].replace('"branches": 80', '"branches": 40')}
        self.assertEqual(kinds(findings(b, a)), ["coverage-floor-lowered"])
        self.assertEqual(findings(a, b), [])
        bv = {"vitest.config.ts": "export default { test: { coverage: { thresholds: { lines: 90 } } } };\n"}
        av = {"vitest.config.ts": bv["vitest.config.ts"].replace("lines: 90", "lines: 10")}
        self.assertEqual(kinds(findings(bv, av)), ["coverage-floor-lowered"])


class OutputAndExitTests(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = guard.main(argv)
        return rc, buf.getvalue().splitlines()

    def write(self, text: str) -> str:
        fd, p = tempfile.mkstemp(suffix=".diff")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.remove, p)
        return p

    def test_zero_test_files_says_so(self):
        repo = Repo({"README.md": "a\n"}, {"README.md": "b\n"})
        try:
            p = self.write(repo.diff())
        finally:
            repo.close()
        rc, out = self.run_main(["--diff-file", p, "--summary-file", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(out[-1], "assertion-guard: 0 finding(s) in 0 test file(s) scanned "
                                  "(no test files changed in this diff, so no test could have been weakened)")

    def test_empty_diff_says_so(self):
        rc, out = self.run_main(["--diff-file", self.write(""), "--summary-file", ""])
        self.assertEqual(rc, 0)
        self.assertIn("the diff is empty", out[-1])

    def test_not_a_diff_is_an_error_not_clean(self):
        rc, out = self.run_main(["--diff-file", self.write("hello\n"), "--summary-file", ""])
        self.assertEqual(rc, 2)
        self.assertTrue(out[-1].startswith("assertion-guard: ERROR:"))

    def test_no_input_is_an_error(self):
        rc, out = self.run_main(["--base", "", "--head", "", "--summary-file", ""])
        self.assertEqual(rc, 2)
        self.assertTrue(out[-1].startswith("assertion-guard: ERROR:"))

    def test_missing_file_is_an_error(self):
        rc, out = self.run_main(["--diff-file", "/nonexistent/x.diff", "--summary-file", ""])
        self.assertEqual(rc, 2)
        self.assertTrue(out[-1].startswith("assertion-guard: ERROR:"))

    def test_findings_annotate_summarize_and_exit_1(self):
        summary = self.write("")
        rc, out = self.run_main(["--diff-file", os.path.join(FIXTURES, "weakened.diff"),
                                 "--summary-file", summary])
        self.assertEqual(rc, 1)
        self.assertTrue(out[-1].startswith("assertion-guard: 12 finding(s) in 3 test file(s) scanned"))
        errors = [ln for ln in out if ln.startswith("::error file=")]
        self.assertEqual(len(errors), 12)
        self.assertRegex(errors[0], r"^::error file=[^,]+,line=\d+,title=assertion-guard [a-z-]+::")
        with open(summary) as fh:
            text = fh.read()
        self.assertIn("| Finding | File | Line | What changed |", text)
        self.assertIn("`test-deleted`", text)

    def test_clean_fixture_exits_0(self):
        rc, out = self.run_main(["--diff-file", os.path.join(FIXTURES, "clean-refactor.diff"),
                                 "--summary-file", ""])
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[-1].startswith("assertion-guard: 0 finding(s) in 5 test file(s) scanned"))

    def test_annotation_escaping(self):
        self.assertEqual(guard._esc_prop("a,b:c%"), "a%2Cb%3Ac%25")
        self.assertEqual(guard._esc_data("x\ny"), "x%0Ay")

    def test_git_mode_reads_the_three_dot_diff_and_the_tree(self):
        b = {"src/parse.py": "def parse(t):\n    return int(t)\n",
             "tests/test_parse.py": "def test_parse():\n    assert parse('1') == 1\n"}
        repo = Repo(b, {"tests/test_parse.py": None})
        cwd = os.getcwd()
        try:
            os.chdir(repo.dir)
            rc, out = self.run_main(["--base", repo.base, "--head", repo.head, "--summary-file", ""])
        finally:
            os.chdir(cwd)
            repo.close()
        self.assertEqual(rc, 1, out)
        self.assertIn("test-deleted", out[0])

    def test_git_mode_bad_revision_is_an_error(self):
        repo = Repo({"a.txt": "a\n"}, {"a.txt": "b\n"})
        cwd = os.getcwd()
        try:
            os.chdir(repo.dir)
            rc, out = self.run_main(["--base", "0" * 40, "--head", repo.head, "--summary-file", ""])
        finally:
            os.chdir(cwd)
            repo.close()
        self.assertEqual(rc, 2)
        self.assertTrue(out[-1].startswith("assertion-guard: ERROR:"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
