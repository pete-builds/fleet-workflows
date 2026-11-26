#!/usr/bin/env python3
"""Permanent fixtures from the round-two review of PR #6, plus the rename-rule ratchet.

Every case is a (before, after) pair of file maps, built into a real git repo. The guard
runs in git mode AND on the same change through --diff-file, the two must agree, and the
result must match EXPECT exactly: exit code and the sorted list of finding types.

Cases whose expected result differs from the ideal are marked KNOWN LIMIT and listed in the
README. If a change fixes one, this fixture goes red on purpose: update EXPECT and the
README together.

The case bodies below are the reviewer's hunt.py, copied verbatim.

Usage: python3 -B smoke/assertion-guard/cases.py   (called by check.sh)
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.environ.get("GUARD", os.path.join(ROOT, "assertion-guard", "guard.py"))


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


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True)


def run(work, name, before, after):
    """(git exit, git stdout, replay exit, replay stdout) for one case."""
    d = os.path.join(work, name)
    os.makedirs(d)
    sh(d, "git", "init", "-q", "-b", "main")
    sh(d, "git", "config", "user.email", "p@example.invalid")
    sh(d, "git", "config", "user.name", "p")
    write(d, before)
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-qm", "base")
    write(d, after)
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-qm", "head")
    g = sh(d, sys.executable, "-B", GUARD, "--base", "main~1", "--head", "main",
           "--no-annotations", "--summary-file", "")
    diff = sh(d, "git", "diff", "--no-color", "-M", "main~1...main").stdout
    df = d + ".diff"
    with open(df, "wb") as fh:
        fh.write(diff)
    r = sh(work, sys.executable, "-B", GUARD, "--diff-file", df, "--no-annotations",
           "--summary-file", "")
    return g.returncode, g.stdout.decode(errors="replace"), r.returncode, r.stdout.decode(errors="replace")


def kinds_of(out):
    return sorted(ln.split("\t", 1)[0] for ln in out.splitlines() if ln.count("\t") >= 2)


# ======================================================================================
# The reviewer's cases, verbatim from review-fw-6-r2/hunt.py
# ======================================================================================
SRC = {"src/app/auth.py": "def login(u, p):\n    return u == 'a' and p == 'b'\n"}

cases = []

# ---- F5 rename rule --------------------------------------------------------------
LONG_OLD = '''def test_rejects_bad_password(client):
    client.reset()
    user = make_user("alice")
    client.set_header("X-Trace", "1")
    r = client.post("/login", json={"u": user.name, "p": "wrong"})
    assert r.status_code == 401
'''
LONG_NEW = '''def test_login_rejects(client):
    client.reset()
    user = make_user("alice")
    client.set_header("X-Trace", "1")
    r = client.post("/login", json={"u": user.name})  # "wrong"
    assert r.status_code == 401
'''
KEEP = "def test_other(client):\n    assert client.get('/x').status_code == 200\n\n\n"
cases.append(("R1-rename-with-input-changed",
              {**SRC, "tests/test_auth.py": KEEP + LONG_OLD},
              {"tests/test_auth.py": KEEP + LONG_NEW},
              "residual: green (same as editing a test in place)"))

# Same edit without renaming: the guard never sees in-place edits either.
cases.append(("R1b-same-edit-in-place-no-rename",
              {**SRC, "tests/test_auth.py": KEEP + LONG_OLD},
              {"tests/test_auth.py": KEEP + LONG_OLD.replace('"p": "wrong"', '"p": "b"')},
              "control: green by design"))

# Near-identical copy that drops a distinct assertion.
OLD2 = LONG_OLD + '    assert r.json()["error"] == "bad password"\n'
cases.append(("R2-copy-drops-distinct-assert",
              {**SRC, "tests/test_auth.py": KEEP + OLD2},
              {"tests/test_auth.py": KEEP + LONG_OLD.replace("test_rejects_bad_password", "test_login_rejects")},
              "red"))

# Legit rename that strengthens one assertion (adds a message).
cases.append(("R3-rename-and-strengthen-assert (FP?)",
              {**SRC, "tests/test_auth.py": KEEP + LONG_OLD},
              {"tests/test_auth.py": KEEP + LONG_OLD.replace("test_rejects_bad_password", "test_login_rejects_wrong_password")
               .replace("assert r.status_code == 401", "assert r.status_code == 401, r.text")},
              "green ideally"))

# Legit rename that fixes the expected value string (still exact).
cases.append(("R3b-rename-and-change-expected-literal (FP?)",
              {**SRC, "tests/test_auth.py": KEEP + OLD2},
              {"tests/test_auth.py": KEEP + OLD2.replace("test_rejects_bad_password", "test_login_rejects")
               .replace('"bad password"', '"invalid credentials"')},
              "arguably green"))

# Assertion-less test (helper asserts) deleted, unrelated test added in the same block.
cases.append(("R4-helper-assert-test-swapped-in-place",
              {**SRC, "tests/test_auth.py": KEEP + "def test_admin_flow(client):\n    check_admin_requires_auth(client, '/admin')\n    check_admin_audit_log(client)\n"},
              {"tests/test_auth.py": KEEP + "def test_version(client):\n    client.get('/version')\n"},
              "red"))

# ---- consolidated rule -----------------------------------------------------------
API_OLD = ("def test_admin_requires_auth(client):\n    r = client.get(\"/admin\")\n    assert r.status_code == 401\n\n\n"
           "def test_health(client):\n    r = client.get(\"/health\")\n    assert r.status_code == 200\n")
API_NEW = ("import pytest\n\n\n@pytest.mark.parametrize(\"path\", [\"/admin\", \"/health\"])\n"
           "def test_pages_load_for_admin(admin_client, path):\n    assert admin_client.get(path).status_code == 200\n\n\n"
           "def test_health(client):\n    r = client.get(\"/health\")\n    assert r.status_code == 200\n")
cases.append(("R5-auth-test-deleted-literal-reused-in-parametrize",
              {"src/api.py": "x = 1\n", "tests/test_api.py": API_OLD},
              {"tests/test_api.py": API_NEW},
              "red (the 401 check is gone)"))

# ---- F2 via delete+add instead of git rename -------------------------------------
CALC_T = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"
PAD = "".join(f"# note line {i}: unrelated helper text to push similarity under 50%\n" for i in range(30))
cases.append(("R6-moved-to-uncollected-file-not-a-git-rename",
              {"src/calc.py": "def add(a, b):\n    return a + b\n", "tests/test_calc.py": CALC_T},
              {"tests/test_calc.py": None, "tests/calc_checks.py": PAD + CALC_T},
              "red (pytest no longer collects them)"))
cases.append(("R6b-tests-moved-into-existing-helper-module",
              {"src/calc.py": "def add(a, b):\n    return a + b\n", "tests/test_calc.py": CALC_T,
               "tests/helpers.py": "def mk():\n    return 1\n" + PAD},
              {"tests/test_calc.py": None, "tests/helpers.py": "def mk():\n    return 1\n" + PAD + "\n\n" + CALC_T},
              "red"))

# ---- F1 excuse via a non-source file of the same stem ----------------------------
SEC_T = "def test_rejects():\n    assert login('a', 'x') is False\n"
cases.append(("R7-test-deleted-with-same-stem-doc-deleted",
              {**SRC, "tests/test_security.py": SEC_T, "docs/security.md": "# Security\n"},
              {"tests/test_security.py": None, "docs/security.md": None},
              "red (the code under test remains)"))

# ---- constant-assert false positives ---------------------------------------------
BASE_F = {"src/f.py": "def f():\n    return 3\n", "tests/test_f.py": "def test_f():\n    assert f() == 3\n"}
cases.append(("K1-assert-x-eq-x-and-enum-consts",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3\n\n\ndef test_g():\n    x = f()\n    assert x == x\n    assert Color.RED == Color.RED\n    assert Color.RED.value == 1\n"},
              "green"))
cases.append(("K2-pytester-source-in-string (FP?)",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3\n\n\ndef test_plugin(pytester):\n    pytester.makepyfile(\n        '''\ndef test_inner():\n    assert 1 == 1\n'''\n    )\n    pytester.runpytest().assert_outcomes(passed=1)\n"},
              "green ideally"))
cases.append(("K3-new-smoke-test-assert-True",
              BASE_F,
              {"tests/test_import.py": "def test_import():\n    import f  # noqa\n    assert True\n"},
              "fires by documented design"))

# ---- >= rule ---------------------------------------------------------------------
cases.append(("G1-new-test-with-ge-alone",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3\n\n\ndef test_nonneg():\n    n = f()\n    assert n >= 0\n"},
              "green"))
cases.append(("G2-exact-strengthened-plus-ge-added-same-block",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3, 'f'\n    assert f() >= 0\n"},
              "green ideally (exact kept, extra range added)"))
cases.append(("G3-exact-replaced-by-ne",
              BASE_F, {"tests/test_f.py": "def test_f():\n    assert f() != 0\n"}, "red"))
cases.append(("G4-exact-replaced-by-isinstance",
              BASE_F, {"tests/test_f.py": "def test_f():\n    assert isinstance(f(), int)\n"}, "red"))

# ---- mock assertions are not counted as assertions -------------------------------
MOCK_OLD = ("def test_sends(mailer):\n    notify('x')\n    mailer.send.assert_called_once_with('x')\n    assert True is not False\n")
cases.append(("M1-mock-assert-line-deleted",
              {"src/n.py": "def notify(x):\n    pass\n",
               "tests/test_n.py": "def test_sends(mailer):\n    notify('x')\n    mailer.send.assert_called_once_with('x')\n    mailer.log.assert_called_once_with('sent x')\n"},
              {"tests/test_n.py": "def test_sends(mailer):\n    notify('x')\n    mailer.send.assert_called_once_with('x')\n"},
              "red"))
cases.append(("M2-js-toThrow-message-dropped",
              {"src/a.test.ts": "it('a', () => {\n  expect(() => parse('')).toThrow('empty input');\n});\n"},
              {"src/a.test.ts": "it('a', () => {\n  expect(() => parse('')).toThrow();\n});\n"},
              "red"))
cases.append(("M3-assertRaisesRegex-to-assertRaises",
              {"tests/test_p.py": "class T(unittest.TestCase):\n    def test_p(self):\n        with self.assertRaisesRegex(ValueError, 'empty input'):\n            parse('')\n"},
              {"tests/test_p.py": "class T(unittest.TestCase):\n    def test_p(self):\n        with self.assertRaises(ValueError):\n            parse('')\n"},
              "red"))
cases.append(("M4-raise-SkipTest-added",
              BASE_F, {"tests/test_f.py": "import unittest\n\n\ndef test_f():\n    raise unittest.SkipTest('later')\n    assert f() == 3\n"},
              "red"))
cases.append(("M5-vitest-runIf-false",
              {"src/a.test.ts": "test('a', () => {\n  expect(one()).toBe(1);\n});\n"},
              {"src/a.test.ts": "test.runIf(false)('a', () => {\n  expect(one()).toBe(1);\n});\n"},
              "red"))

# ---- exit-2 paths ----------------------------------------------------------------
cases.append(("X1-invalid-utf8-in-test-file",
              BASE_F,
              {"tests/test_f.py": b"def test_f():\n    assert f() == 3\n\n\ndef test_bytes():\n    assert raw() == b'\xff\xfe'  # \xff\n"},
              "exit 0"))
cases.append(("X2-syntax-error-mid-edit",
              BASE_F,
              {"tests/test_f.py": "def test_f(:\n    assert f() == 3\n    assert (f() ==\n\n\ndef test_g()\n    assert self.assertEqual(\n"},
              "exit 0 or 1, not 2"))
cases.append(("X3-ternary-self-assert",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3\n\n\nclass T(unittest.TestCase):\n    def test_t(self):\n        self.assertEqual(len(a), 1) if a else self.fail('empty')\n"},
              "exit 0 or 1, not 2"))
cases.append(("X4-nul-byte-in-assert",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3\n    assert '\x00' == '\x00'\n"},
              "exit 1, not 2"))
cases.append(("X5-huge-literal-compare",
              BASE_F,
              {"tests/test_f.py": "def test_f():\n    assert f() == 3\n    assert (1,) * 3 == (1, 1, 1)\n    assert 10**10**10 > 0\n"},
              "must not hang"))


extra = []
CONF = "import pytest\n\n\n@pytest.fixture\ndef client():\n    return make_client()\n"
extra.append(("R6c-tests-moved-into-conftest",
              {"src/calc.py": "def add(a, b):\n    return a + b\n", "tests/test_calc.py": CALC_T, "tests/conftest.py": CONF},
              {"tests/test_calc.py": None, "tests/conftest.py": CONF + "\n\n" + CALC_T},
              "red (pytest never collects tests from conftest.py)"))
extra.append(("R6d-one-test-moved-into-conftest-file-kept",
              {"src/calc.py": "def add(a, b):\n    return a + b\n", "tests/test_calc.py": CALC_T, "tests/conftest.py": CONF},
              {"tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
               "tests/conftest.py": CONF + "\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"},
              "red"))
extra.append(("R4b-mock-only-test-swapped-in-place",
              {"tests/test_n.py": KEEP + "def test_sends(mailer):\n    notify('x')\n    mailer.send.assert_called_once_with('x')\n"},
              {"tests/test_n.py": KEEP + "def test_version(client):\n    client.get('/version')\n"},
              "red"))

# ======================================================================================
# The rename-rule ratchet: one case per condition of looks_like_rename, where ONLY that
# condition stands between a deleted test and a false "it was renamed". Each was green
# with its condition deleted before these cases existed (round two, finding 2).
# ======================================================================================

# T1 (reviewer's thr_case.py): only the 80% body threshold blocks. Every assertion and
# every string literal carries over (the literal "wrong" survives in a comment); 4 of 6
# body lines do not.
T1_OLD = '''def test_rejects_bad_password(client):
    client.reset()
    user = make_user("alice")
    client.set_header("X-Trace", "1")
    client.login(user.name, "wrong")
    r = client.get("/me")
    assert r.status_code == 401
'''
T1_NEW = '''def test_me_requires_login(client):
    client.reset()
    user = make_user("alice")
    client.set_header("X-Trace", "1")
    client.logout()  # "wrong"
    r = client.get("/me", auth=None)
    assert r.status_code == 401
'''
extra.append(("T1-only-the-80pct-threshold-blocks",
              {"tests/test_auth.py": KEEP + T1_OLD},
              {"tests/test_auth.py": KEEP, "tests/test_me.py": T1_NEW}, "red"))

# T2: only the string-literal check blocks. Five body lines, one string literal changed:
# 4 of 5 lines carry over (exactly the 80% threshold), every assertion carries over.
T2_OLD = '''def test_admin_requires_auth(client):
    client.reset()
    user = make_user("alice")
    client.set_header("X-Trace", "1")
    r = client.get("/admin")
    assert r.status_code == 401
'''
T2_NEW = T2_OLD.replace("test_admin_requires_auth", "test_ping_requires_auth").replace('"/admin"', '"/ping"')
extra.append(("T2-only-the-literal-check-blocks",
              {"tests/test_auth.py": KEEP + T2_OLD},
              {"tests/test_auth.py": KEEP, "tests/test_ping.py": T2_NEW}, "red"))

# T3: only the assertion-subset check blocks. A dropped assertion whose only string literal
# is one character long (the literal check ignores 1-character strings); 5 of 6 lines
# carry over, above the threshold.
T3_OLD = '''def test_flags(client):
    client.reset()
    user = make_user("alice")
    client.set_header("X-Trace", "1")
    r = client.get("/flags")
    assert r.json()["a"] == 1
    assert r.status_code == 200
'''
T3_NEW = T3_OLD.replace("test_flags", "test_flags_load").replace('    assert r.json()["a"] == 1\n', "")
extra.append(("T3-only-the-assertion-check-blocks",
              {"tests/test_auth.py": KEEP + T3_OLD},
              {"tests/test_auth.py": KEEP, "tests/test_flags.py": T3_NEW}, "red"))

# T4: the control for T1 to T3. The same move with the body intact is a rename: green.
extra.append(("T4-control-real-rename-and-move",
              {"tests/test_auth.py": KEEP + T2_OLD},
              {"tests/test_auth.py": KEEP, "tests/test_admin.py": T2_OLD.replace(
                  "test_admin_requires_auth", "test_admin_needs_login")}, "green"))

# T5: the invariant protects what the runner COLLECTED at the base. A test_* function
# removed from a helper module that pytest never collected is not a lost identity.
extra.append(("T5-function-removed-from-uncollected-helper",
              {"tests/test_calc.py": CALC_T,
               "tests/helpers.py": "def test_payload():\n    return {'a': 1}\n\n\ndef mk():\n    return 1\n"},
              {"tests/helpers.py": "def mk():\n    return 1\n"}, "green"))

# T6 (calibration, mcp-unifi#227): a test renamed in place with a multi-line signature,
# its fixture argument and one body line changed, every assertion left as context. A
# rename in place, so green. The unchanged `) -> None:` once ended the test early.
T6_OLD = """import pytest


async def test_end_to_end_summary_reaches_the_text(
    stdio_server: FastMCP,
) -> None:
    async with Client(stdio_server) as client:
        networks = await client.call_tool("list_networks", {})
        result = await client.call_tool("update_vlan", {"id": networks[0]})
        assert result.structured_content["verified"] is True
        assert "verified" in result.content[0].text
"""
T6_NEW = (T6_OLD.replace("test_end_to_end_summary_reaches_the_text(", "test_end_to_end_compact_summary_reaches_the_text(")
          .replace("    stdio_server: FastMCP,", "    compact_stdio_server: FastMCP,")
          .replace("Client(stdio_server)", "Client(compact_stdio_server)"))
extra.append(("T6-rename-in-place-multiline-signature",
              {"tests/test_adaptive.py": T6_OLD}, {"tests/test_adaptive.py": T6_NEW}, "green"))

# ======================================================================================
# Expected result per case: (exit code, sorted finding types, replay mode, note).
# replay mode "same": --diff-file must print exactly what git mode printed.
# replay mode "binary": a plain `git diff` shows the test file as binary, so the replay
# must refuse with exit 2, while git mode (which diffs with --text) reads it.
# ======================================================================================
TD = ["test-deleted"]
LA = ["loosened-assertion"]
EXPECT = {
    "R1-rename-with-input-changed": (0, [], "same", "rename plus one edited setup line: same as an in-place edit"),
    "R1b-same-edit-in-place-no-rename": (0, [], "same", "control"),
    "R2-copy-drops-distinct-assert": (1, TD, "same", ""),
    "R3-rename-and-strengthen-assert (FP?)": (1, TD, "same", "KNOWN LIMIT: false positive, rename that strengthens an assertion"),
    "R3b-rename-and-change-expected-literal (FP?)": (1, TD, "same", "KNOWN LIMIT: false positive, rename that changes an expected value"),
    "R4-helper-assert-test-swapped-in-place": (1, TD, "same", ""),
    "R5-auth-test-deleted-literal-reused-in-parametrize": (1, TD, "same", ""),
    "R6-moved-to-uncollected-file-not-a-git-rename": (1, TD, "same", ""),
    "R6b-tests-moved-into-existing-helper-module": (1, TD, "same", ""),
    "R7-test-deleted-with-same-stem-doc-deleted": (1, TD, "same", ""),
    "K1-assert-x-eq-x-and-enum-consts": (0, [], "same", ""),
    "K2-pytester-source-in-string (FP?)": (1, LA, "same", "KNOWN LIMIT: false positive, test source inside a string"),
    "K3-new-smoke-test-assert-True": (1, LA, "same", "by design: an added constant assertion always fires"),
    "G1-new-test-with-ge-alone": (0, [], "same", ""),
    "G2-exact-strengthened-plus-ge-added-same-block": (0, [], "same", ""),
    "G3-exact-replaced-by-ne": (0, [], "same", "KNOWN LIMIT: `== 3` to `!= 0` not detected"),
    "G4-exact-replaced-by-isinstance": (0, [], "same", "KNOWN LIMIT: `== 3` to isinstance not detected"),
    "M1-mock-assert-line-deleted": (1, ["net-assertion-loss"], "same", ""),
    "M2-js-toThrow-message-dropped": (1, LA, "same", ""),
    "M3-assertRaisesRegex-to-assertRaises": (1, LA, "same", ""),
    "M4-raise-SkipTest-added": (0, [], "same", "KNOWN LIMIT: raise SkipTest not detected"),
    "M5-vitest-runIf-false": (0, [], "same", "KNOWN LIMIT: test.runIf(false) not detected"),
    "X1-invalid-utf8-in-test-file": (0, [], "same", ""),
    "X2-syntax-error-mid-edit": (0, [], "same", ""),
    "X3-ternary-self-assert": (0, [], "same", "an optional classifier must never exit 2"),
    "X4-nul-byte-in-assert": (1, LA, "binary", ""),
    "X5-huge-literal-compare": (0, [], "same", "must not hang"),
    "R6c-tests-moved-into-conftest": (1, TD, "same", ""),
    "R6d-one-test-moved-into-conftest-file-kept": (1, TD, "same", ""),
    "R4b-mock-only-test-swapped-in-place": (1, TD, "same", ""),
    "T1-only-the-80pct-threshold-blocks": (1, TD, "same", "ratchet: the 80% threshold"),
    "T2-only-the-literal-check-blocks": (1, TD, "same", "ratchet: the string-literal check"),
    "T3-only-the-assertion-check-blocks": (1, TD, "same", "ratchet: the assertion-subset check"),
    "T4-control-real-rename-and-move": (0, [], "same", "control for T1 to T3"),
    "T5-function-removed-from-uncollected-helper": (0, [], "same", "never collected, so not an identity"),
    "T6-rename-in-place-multiline-signature": (0, [], "same", "calibration mcp-unifi#227: a rename in place"),
}


def main():
    all_cases = cases + extra
    names = [c[0] for c in all_cases]
    missing = sorted(set(names) ^ set(EXPECT))
    if missing or len(names) != len(set(names)):
        print(f"::error::cases and EXPECT do not match one to one: {missing}")
        print("assertion-guard cases: FAIL (fixture table broken)")
        return 1
    fail = 0
    work = tempfile.mkdtemp()
    try:
        for name, before, after, _ in all_cases:
            want_rc, want_kinds, mode, note = EXPECT[name]
            rc_g, out_g, rc_r, out_r = run(work, name.split(" ")[0], before, after)
            got = kinds_of(out_g)
            ok = rc_g == want_rc and got == want_kinds
            if mode == "same":
                ok = ok and (rc_g, out_g) == (rc_r, out_r)
                replay = "replay identical" if (rc_g, out_g) == (rc_r, out_r) else f"replay DIFFERS (exit {rc_r})"
            else:
                ok = ok and rc_r == 2 and out_r.splitlines()[-1].startswith("assertion-guard: ERROR:")
                replay = f"replay refused, exit {rc_r}"
            tag = f"  ({note})" if note else ""
            if ok:
                print(f"PASS case {name}: exit {rc_g} {got}, {replay}{tag}")
            else:
                fail = 1
                print(f"::error::case {name}: want exit {want_rc} {want_kinds}; git exit {rc_g} {got}, {replay}{tag}")
                for ln in out_g.splitlines():
                    print(f"  git: {ln[:180]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    n = len(all_cases)
    if fail:
        print(f"assertion-guard cases: FAIL ({n} cases run)")
        return 1
    print(f"assertion-guard cases: PASS ({n} cases run, each in both modes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
