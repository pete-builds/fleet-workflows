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
import shutil
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


def run_main(argv: list[str], cwd: str | None = None) -> tuple[int, list[str]]:
    buf = io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buf):
            rc = guard.main(argv)
    finally:
        os.chdir(old)
    return rc, buf.getvalue().splitlines()


def both_modes(repo: Repo) -> tuple[int, list[str]]:
    """Run the guard on one change in BOTH modes and require identical results: git mode
    (what CI runs) and --diff-file mode (what an offline replay runs). Calibration once ran
    only --diff-file while CI ran git mode, and the two disagreed (review finding F1)."""
    text = repo.diff()
    if not text.strip():
        raise AssertionError("case produced an empty diff: the edit matched nothing")
    fd, path = tempfile.mkstemp(suffix=".diff")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    try:
        rc_d, out_d = run_main(["--diff-file", path, "--no-annotations", "--summary-file", ""])
    finally:
        os.remove(path)
    rc_g, out_g = run_main(["--base", repo.base, "--head", repo.head, "--no-annotations",
                            "--summary-file", ""], cwd=repo.dir)
    if (rc_d, out_d) != (rc_g, out_g):
        raise AssertionError(
            f"git mode and --diff-file mode disagree:\n  diff-file exit {rc_d}: {out_d}\n"
            f"  git       exit {rc_g}: {out_g}")
    return rc_g, out_g


def findings(before: dict[str, str], after: dict[str, str | None]) -> list[guard.Finding]:
    repo = Repo(before, after)
    try:
        rc, out = both_modes(repo)
        result = guard.detect(guard.parse_diff(repo.diff()))
        # The in-process result must be what main() printed, finding for finding.
        printed = [ln for ln in out if "\t" in ln]
        assert printed == [f"{f.kind}\t{f.path}:{f.line}\t{f.message}" for f in result], out
        assert rc == (1 if result else 0), (rc, out)
        return result
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
        after["tests/test_new.py"] = "import pytest\n\npytestmark = pytest.mark.skip\n\n\ndef test_n():\n    assert one()\n"
        self.assertEqual(findings(PY_BASE, after), [])

    def test_js_skip_and_focus(self):
        b = {"src/a.test.ts": "it('a', () => {\n  expect(one()).toBe(1);\n});\n"}
        for new in ("it.skip('a'", "xit('a'", "it.only('a'"):
            with self.subTest(new=new):
                a = {"src/a.test.ts": b["src/a.test.ts"].replace("it('a'", new)}
                self.assertEqual(kinds(findings(b, a)), ["skip-added"])

    def test_focus_on_a_new_test_still_fires(self):
        b = {"src/a.test.ts": "it('a', () => {\n  expect(one()).toBe(1);\n});\n"}
        a = {"src/a.test.ts": b["src/a.test.ts"] + "it.only('b', () => {\n  expect(two()).toBe(2);\n});\n"}
        self.assertEqual(kinds(findings(b, a)), ["skip-added"])

    def test_moved_skip_is_quiet(self):
        b = {"tests/test_a.py": "import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    assert one() == 1\n"}
        a = {"tests/test_a.py": None,
             "tests/test_b.py": "import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    assert one() == 1\n"}
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
        b = {"src/a.test.ts": "it('a', () => {\n  expect(one()).toBe(1);\n});\n\nit('b', () => {\n  expect(two()).toBe(2);\n});\n"}
        a = {"src/a.test.ts": "it('a', () => {\n  expect(one()).toBe(1);\n});\n"}
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


class ReviewFindingTests(unittest.TestCase):
    """One case per finding from the independent review of PR #6, each through both modes
    (via findings()), next to the legitimate change it must leave alone."""

    def test_f1_feature_named_test_file_deleted_fires(self):
        b = {"src/app/auth.py": "def login(u, p):\n    return u == 'a'\n",
             "tests/test_security.py": "def test_rejects():\n    assert login('a', 'x') is False\n"}
        self.assertEqual(kinds(findings(b, {"tests/test_security.py": None})), ["test-deleted"])

    def test_f1_test_for_a_package_deleted_fires(self):
        b = {"src/pkg/tools/__init__.py": "def run():\n    return 1\n",
             "tests/test_tools.py": "def test_run():\n    assert run() == 1\n"}
        self.assertEqual(kinds(findings(b, {"tests/test_tools.py": None})), ["test-deleted"])

    def test_f1_subject_deleted_in_the_same_diff_is_quiet(self):
        b = {"src/app/security.py": "def ok():\n    return True\n",
             "tests/test_security.py": "def test_ok():\n    assert ok() is True\n"}
        self.assertEqual(findings(b, {"tests/test_security.py": None, "src/app/security.py": None}), [])

    CALC = {"src/calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": "def test_add():\n    assert add(1, 2) == 3\n"}

    def _rename(self, new_path: str, content: str | None = None) -> list[guard.Finding]:
        after = {"tests/test_calc.py": None, new_path: content or self.CALC["tests/test_calc.py"]}
        return findings(self.CALC, after)

    def test_f2_renamed_out_of_the_test_paths_fires(self):
        fs = self._rename("attic/calc_checks.py")
        self.assertEqual(kinds(fs), ["test-deleted"])
        self.assertIn("no longer collects", fs[0].message)

    def test_f2_renamed_to_an_uncollected_name_inside_tests_fires(self):
        self.assertEqual(kinds(self._rename("tests/calc_checks.py")), ["test-deleted"])

    def test_f2_renamed_and_gutted_fires(self):
        self.assertEqual(kinds(self._rename("attic/calc_checks.py", "def test_add():\n    add(1, 2)\n")),
                         ["test-deleted"])

    def test_f2_renamed_within_collection_is_quiet(self):
        self.assertEqual(self._rename("tests/unit/test_calc.py"), [])
        self.assertEqual(self._rename("tests/calc_test.py"), [])

    def test_f3_gitattributes_cannot_hide_test_changes(self):
        b = dict(self.CALC)
        b["tests/test_calc.py"] = "def test_add():\n    assert add(1, 2) == 3\n    assert add(2, 2) == 4\n"
        a = {".gitattributes": "tests/** -diff\n", "tests/test_calc.py": "def test_add():\n    add(1, 2)\n"}
        repo = Repo(b, a)
        try:
            # git mode reads through the attribute with --text.
            rc, out = run_main(["--base", repo.base, "--head", repo.head, "--no-annotations",
                                "--summary-file", ""], cwd=repo.dir)
            self.assertEqual(rc, 1, out)
            self.assertIn("net-assertion-loss", "\n".join(out))
            # A plain `git diff` (what a replay file holds) shows "Binary files differ":
            # that is an error, never "no test files changed".
            text = repo.diff()
            self.assertIn("Binary files", text)
            fd, path = tempfile.mkstemp(suffix=".diff")
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            try:
                rc, out = run_main(["--diff-file", path, "--summary-file", ""])
            finally:
                os.remove(path)
            self.assertEqual(rc, 2, out)
            self.assertTrue(out[-1].startswith("assertion-guard: ERROR:"), out)
        finally:
            repo.close()

    def test_f4_singular_test_directory(self):
        b = {"test/add.js": "it('adds', () => {\n  assert.strictEqual(add(1, 2), 3);\n  assert.strictEqual(add(2, 2), 4);\n});\n"}
        a = {"test/add.js": "it('adds', () => {\n  assert.strictEqual(add(1, 2), 3);\n});\n"}
        self.assertEqual(kinds(findings(b, a)), ["net-assertion-loss"])
        bp = {"test/test_x.py": "def test_x():\n    assert f() == 1\n    assert g() == 2\n"}
        ap = {"test/test_x.py": "def test_x():\n    assert f() == 1\n"}
        self.assertEqual(kinds(findings(bp, ap)), ["net-assertion-loss"])

    def test_f5_generic_assertion_does_not_make_a_rename(self):
        b = {"tests/test_api.py": (
            "def test_admin_requires_auth(client):\n    r = client.get('/admin')\n    assert r.status_code == 401\n\n\n"
            "def test_health(client):\n    r = client.get('/health')\n    assert r.status_code == 200\n")}
        a = {"tests/test_api.py": (
            "def test_health(client):\n    r = client.get('/health')\n    assert r.status_code == 200\n\n\n"
            "def test_ping(client):\n    r = client.get('/ping')\n    assert r.status_code == 401\n")}
        fs = findings(b, a)
        self.assertEqual(kinds(fs), ["test-deleted"])
        self.assertIn("test_admin_requires_auth", fs[0].message)

    def test_f5_real_rename_with_setup_is_still_quiet(self):
        b = {"tests/test_api.py": "def test_admin(client):\n    r = client.get('/admin')\n    assert r.status_code == 401\n"}
        a = {"tests/test_api.py": None,
             "tests/test_auth.py": "def test_admin_requires_auth(client):\n    r = client.get('/admin')\n    assert r.status_code == 401\n"}
        self.assertEqual(findings(b, a), [])

    def test_f7_build_folder_inside_tests_is_in_scope(self):
        b = {"tests/build/test_mk.py": "def test_mk():\n    assert mk() == 1\n\n\ndef test_mk2():\n    assert mk() == 2\n"}
        a = {"tests/build/test_mk.py": "def test_mk():\n    assert mk() == 1\n"}
        self.assertEqual(kinds(findings(b, a)), ["test-deleted"])
        self.assertFalse(guard.is_test_file("build/lib/tests/test_mk.py"))
        self.assertFalse(guard.is_test_file("dist/x.test.js"))
        self.assertTrue(guard.is_test_file("src/build/x.test.js"))

    def test_f6_mark_skip_after_import(self):
        b = {"tests/test_m.py": "from pytest import mark\n\n\ndef test_a():\n    assert f() == 3\n"}
        a = {"tests/test_m.py": "from pytest import mark\n\n\n@mark.skip(reason='x')\ndef test_a():\n    assert f() == 3\n"}
        self.assertEqual(kinds(findings(b, a)), ["skip-added"])

    def test_f6_constant_assertions(self):
        for old, new in (("assert f() == 3", "assert 3 == 3"), ("assert f() == 3", "assert True"),
                         ("self.assertEqual(f(), 3)", "self.assertEqual(3, 3)")):
            with self.subTest(new=new):
                b = {"tests/test_c.py": f"def test_c():\n    {old}\n"}
                a = {"tests/test_c.py": f"def test_c():\n    {new}\n"}
                self.assertIn("loosened-assertion", kinds(findings(b, a)))
        b = {"src/c.test.ts": "it('c', () => {\n  expect(f()).toBe(3);\n});\n"}
        a = {"src/c.test.ts": "it('c', () => {\n  expect(true).toBe(true);\n});\n"}
        self.assertIn("loosened-assertion", kinds(findings(b, a)))

    def test_f6_constant_lookalikes_are_not_tautologies(self):
        for line, lang in (('assert False, "unreachable"', "py"), ("assert f() == 3", "py"),
                           ("assert 3 == x", "py"), ("expect(true).toBe(false);", "js"),
                           ("expect(x).toBe(true);", "js"), ("assert 9**9**9 == 1", "py")):
            with self.subTest(line=line):
                self.assertFalse(guard.is_tautology(line, lang))

    def test_f6_call_checks_lose_their_arguments(self):
        b = {"src/m.test.ts": "it('m', () => {\n  expect(g).toHaveBeenCalledWith(1, 2);\n});\n"}
        a = {"src/m.test.ts": "it('m', () => {\n  expect(g).toHaveBeenCalled();\n});\n"}
        self.assertEqual(kinds(findings(b, a)), ["loosened-assertion"])
        self.assertEqual(findings(a, b), [])
        bp = {"tests/test_m.py": "def test_m(m):\n    m.assert_called_once_with(1, 2)\n"}
        ap = {"tests/test_m.py": "def test_m(m):\n    m.assert_called_once()\n"}
        self.assertEqual(kinds(findings(bp, ap)), ["loosened-assertion"])

    def test_f6_equality_becomes_a_range(self):
        b = {"tests/test_l.py": "def test_l(items):\n    assert len(items) == 3\n"}
        a = {"tests/test_l.py": "def test_l(items):\n    assert len(items) >= 1\n"}
        self.assertEqual(kinds(findings(b, a)), ["loosened-assertion"])
        self.assertEqual(findings(a, b), [])
        bj = {"src/l.test.ts": "it('l', () => {\n  expect(n()).toBe(3);\n});\n"}
        aj = {"src/l.test.ts": "it('l', () => {\n  expect(n()).toBeGreaterThan(0);\n});\n"}
        self.assertEqual(kinds(findings(bj, aj)), ["loosened-assertion"])

    def test_f6_test_class_renamed_out_of_collection(self):
        b = {"tests/test_k.py": "class TestB:\n    def test_b(self):\n        assert f() == 3\n"}
        for new, want in (("class B:", ["test-deleted"]), ("class TestBee:", []),
                          ("class B(unittest.TestCase):", [])):
            with self.subTest(new=new):
                a = {"tests/test_k.py": b["tests/test_k.py"].replace("class TestB:", new)}
                self.assertEqual(kinds(findings(b, a)), want)

    def test_a_crash_is_exit_2_never_exit_1(self):
        real = guard.detect
        guard.detect = lambda fds: 1 / 0
        try:
            fd, path = tempfile.mkstemp(suffix=".diff")
            with os.fdopen(fd, "w") as fh:
                fh.write(open(os.path.join(FIXTURES, "weakened.diff")).read())
            rc, out = run_main(["--diff-file", path, "--summary-file", ""])
            os.remove(path)
        finally:
            guard.detect = real
        self.assertEqual(rc, 2)
        self.assertIn("internal failure", out[-1])


class CollectionDiffTests(unittest.TestCase):
    """The deletion side when a collect command is set: the runner decides, the guard diffs.
    A fake runner (`cat COLLECT.txt`) prints the IDs each commit declares, so these cases
    test the collection-diff invariant itself, with no excusal anywhere."""

    CMD = "cat COLLECT.txt"

    def run_collect(self, before, after, cmd=None, extra=()):
        repo = Repo(before, after)
        ids = tempfile.mkdtemp()
        try:
            rc, out = run_main(["--base", repo.base, "--head", repo.head, "--collect-command",
                                cmd or self.CMD, "--ids-out", ids, "--no-annotations",
                                "--summary-file", "", *extra], cwd=repo.dir)
            # Replay the same change from the recorded IDs: it must print identically.
            if rc != 2:
                fd, path = tempfile.mkstemp(suffix=".diff")
                with os.fdopen(fd, "w") as fh:
                    fh.write(repo.diff())
                rc_r, out_r = run_main(["--diff-file", path, "--base-ids", os.path.join(ids, "base.ids"),
                                        "--head-ids", os.path.join(ids, "head.ids"),
                                        "--no-annotations", "--summary-file", ""])
                os.remove(path)
                self.assertEqual((rc_r, out_r), (rc, out), "replay from recorded IDs differs")
            return rc, out
        finally:
            repo.close()
            shutil.rmtree(ids, ignore_errors=True)

    @staticmethod
    def lost(out):
        return sorted(ln.split("`")[1] for ln in out if ln.startswith("test-deleted\t"))

    T = "def test_a():\n    assert f() == 1\n"

    def test_every_lost_id_is_reported(self):
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "tests/test_a.py::test_a\ntests/test_a.py::test_b\n"}
        a = {"COLLECT.txt": "tests/test_a.py::test_a\n"}
        rc, out = self.run_collect(b, a)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.lost(out), ["tests/test_a.py::test_b"])
        self.assertIn("test deletions checked by the runner (2 collected at the merge base, 1 at head)", out[-1])

    def test_a_rename_is_red_and_both_ids_are_listed(self):
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "tests/test_a.py::test_old\n"}
        a = {"tests/test_a.py": self.T.replace("test_a", "test_new"), "COLLECT.txt": "tests/test_a.py::test_new\n"}
        rc, out = self.run_collect(b, a)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.lost(out), ["tests/test_a.py::test_old"])
        self.assertIn("test IDs added by this change (renamed or consolidated? A human reads this):", out)
        self.assertIn("  + tests/test_a.py::test_new", out)

    def test_a_move_that_keeps_the_name_is_still_red(self):
        # The same test name collected somewhere else is not the same test: no excusal by
        # name, class or file (round three H6, H13). Both IDs listed, the run red.
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "tests/test_a.py::test_x\ntests/test_b.py::test_y\n"}
        a = {"COLLECT.txt": "tests/test_b.py::test_y\ntests/test_b.py::TestMoved::test_x\n"}
        rc, out = self.run_collect(b, a)
        self.assertEqual((rc, self.lost(out)), (1, ["tests/test_a.py::test_x"]))
        self.assertIn("  + tests/test_b.py::TestMoved::test_x", out)

    def test_a_branch_behind_main_is_compared_from_the_merge_base(self):
        # main gains test t3 after the branch point; the branch adds t2. Collecting at the
        # base TIP would report t3 as lost by a PR that never touched it (mcp-unifi#231).
        tmp = tempfile.mkdtemp()
        try:
            for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                         ["config", "user.name", "t"]):
                git(tmp, *args)

            def commit(text, msg):
                with open(os.path.join(tmp, "COLLECT.txt"), "w") as fh:
                    fh.write(text)
                git(tmp, "add", "-A")
                git(tmp, "commit", "-qm", msg)
                return git(tmp, "rev-parse", "HEAD").strip()

            commit("t.py::t1\n", "branch point")
            git(tmp, "checkout", "-q", "-b", "pr")
            head = commit("t.py::t1\nt.py::t2\n", "pr adds t2")
            git(tmp, "checkout", "-q", "main")
            base = commit("t.py::t1\nt.py::t3\n", "main adds t3 later")
            rc, out = run_main(["--base", base, "--head", head, "--collect-command", self.CMD,
                                "--no-annotations", "--summary-file", ""], cwd=tmp)
            self.assertEqual(rc, 0, out)
            self.assertIn("(1 collected at the merge base, 2 at head)", out[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_excusal_even_when_the_code_file_is_deleted(self):
        b = {"src/calc.py": "x = 1\n", "tests/test_calc.py": self.T, "COLLECT.txt": "tests/test_calc.py::test_a\n"}
        a = {"src/calc.py": None, "tests/test_calc.py": None, "COLLECT.txt": ""}
        rc, out = self.run_collect(b, a)
        self.assertEqual(self.lost(out), ["tests/test_calc.py::test_a"])
        self.assertEqual(rc, 1)

    def test_the_runner_overrules_the_static_model(self):
        # The diff deletes a test file, but the runner still collects the same IDs (say the
        # tests live on elsewhere): no test-deleted. And the reverse: nothing in the diff
        # removes a test, but the runner lost one: red.
        b = {"tests/test_a.py": self.T, "tests/test_b.py": self.T, "COLLECT.txt": "x.py::t1\n"}
        rc, out = self.run_collect(b, {"tests/test_b.py": None})
        self.assertEqual(rc, 0, out)
        rc, out = self.run_collect(b, {"COLLECT.txt": ""})
        self.assertEqual((rc, self.lost(out)), (1, ["x.py::t1"]))

    def test_added_only_is_green(self):
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "t.py::a\n"}
        rc, out = self.run_collect(b, {"COLLECT.txt": "t.py::a\nt.py::b\n"})
        self.assertEqual(rc, 0, out)
        self.assertIn("test IDs added by this change (new tests):", out)

    def test_collection_failure_is_exit_2_with_the_runner_tail(self):
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "t.py::a\n"}
        rc, out = self.run_collect(b, {"COLLECT.txt": "t.py::a\n"}, cmd="echo 'ImportError: boom' >&2; exit 4")
        self.assertEqual(rc, 2, out)
        self.assertIn("ImportError: boom", "\n".join(out))
        self.assertTrue(out[-1].startswith("assertion-guard: ERROR:"), out)

    def test_pytest_exit_5_means_nothing_collected(self):
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "t.py::a\n"}
        rc, out = self.run_collect(b, {"COLLECT.txt": ""}, cmd="cat COLLECT.txt; test -s COLLECT.txt || exit 5")
        self.assertEqual((rc, self.lost(out)), (1, ["t.py::a"]))

    def test_unreadable_collect_output_is_exit_2(self):
        b = {"tests/test_a.py": self.T, "COLLECT.txt": "<Module test_a.py>\n3 tests collected\n"}
        rc, out = self.run_collect(b, {"COLLECT.txt": "<Module test_a.py>\n2 tests collected\n"})
        self.assertEqual(rc, 2, out)
        self.assertIn("no test ID could be read", "\n".join(out))

    def test_collect_command_needs_git_mode(self):
        rc, out = run_main(["--diff-file", os.path.join(FIXTURES, "weakened.diff"),
                            "--collect-command", "true", "--summary-file", ""])
        self.assertEqual(rc, 2)

    def test_parse_pytest_and_vitest_output(self):
        pytest_out = ("tests/test_a.py::test_x\ntests/test_a.py::TestK::test_y[unsupported operand]\n"
                      "  tests/test_a.py::indented_warning_line\n\n3 tests collected in 0.01s\n"
                      "ERROR tests/test_b.py::boom - nope\n")
        self.assertEqual(guard.parse_collected(pytest_out), {
            "tests/test_a.py::test_x", "tests/test_a.py::TestK::test_y[unsupported operand]"})
        vitest_out = ("\x1b[32msrc/calc.test.js > adds\x1b[0m\nsrc/calc.test.js > suite > handles negatives\n"
                      "[proj] test/x.spec.ts > ok\n RUN  v3.2.4 /tmp\n")
        self.assertEqual(guard.parse_collected(vitest_out), {
            "src/calc.test.js > adds", "src/calc.test.js > suite > handles negatives", "[proj] test/x.spec.ts > ok"})
        self.assertEqual(guard.id_path("[proj] test/x.spec.ts > ok"), "test/x.spec.ts")
        self.assertEqual(guard.id_path("tests/a.py::T::t[1]"), "tests/a.py")

    def test_static_mode_says_it_is_static(self):
        rc, out = run_main(["--diff-file", os.path.join(FIXTURES, "weakened.diff"), "--summary-file", ""])
        self.assertTrue(out[-1].endswith("test deletions checked statically, not by the runner; set collect-command"))


class CollectionConfigTests(unittest.TestCase):
    def test_collection_config_changes_are_named(self):
        for before, after in (
            ({"pyproject.toml": "[tool.pytest.ini_options]\n"},
             {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'}),
            ({"pytest.ini": "[pytest]\naddopts = -q\n"}, {"pytest.ini": "[pytest]\naddopts = -q --deselect tests/test_a.py::t\n"}),
            ({"setup.cfg": "[tool:pytest]\n"}, {"setup.cfg": "[tool:pytest]\nnorecursedirs = legacy\n"}),
            ({"tests/conftest.py": "import os\n"}, {"tests/conftest.py": "import os\ncollect_ignore = ['x.py']\n"}),
            ({"vitest.config.ts": "export default { test: {} };\n"},
             {"vitest.config.ts": "export default { test: {\n  exclude: ['test/**'],\n} };\n"}),
            ({"package.json": '{\n  "jest": {}\n}\n'}, {"package.json": '{\n  "jest": {\n    "testMatch": ["x"]\n  }\n}\n'}),
        ):
            with self.subTest(after=after):
                self.assertIn("collection-config-changed", kinds(findings(before, after)))

    def test_unrelated_config_is_quiet(self):
        b = {"pyproject.toml": '[project]\nname = "x"\n'}
        a = {"pyproject.toml": '[project]\nname = "y"\n'}
        self.assertEqual(findings(b, a), [])


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
                                  "(no test files changed in this diff); test deletions checked "
                                  "statically, not by the runner; set collect-command")

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

    def test_git_mode_reads_the_three_dot_diff_of_a_deleted_test_file(self):
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
