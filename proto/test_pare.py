"""Tests for the pare prototype.

    .venv/bin/python -m pytest proto/
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pare  # noqa: E402

CORPUS = os.path.join(ROOT, "corpus")
LOGS = sorted(f for f in os.listdir(CORPUS) if f.endswith(".log"))
GENERATED = os.path.join(ROOT, "corpus-generated")
GUTTER = re.compile(r"^\d+: ")  # a rendered input line
GENERATED_LOGS = sorted(f for f in os.listdir(GENERATED) if f.endswith(".log"))


def run(args, data: bytes) -> bytes:
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--no-tee", *args],
                       input=data, capture_output=True, check=True)
    return p.stdout


# --- structural level detection -------------------------------------------

@pytest.mark.parametrize("line,sev", [
    ("2026-10-08T09:00:00Z ERROR [db] connection lost", 3),
    ("2026-10-08 09:00:00,123 WARN  reconnecting", 2),
    ("2026-10-08 09:00:00 INFO  started", 1),
    ("[2026-10-08] [debug] cache hit", 0),
    ("time=2026-10-08 level=error msg=\"boom\"", 3),
    ('{"ts": "2026-10-08", "msg": "x", "level": "warn"}', 2),
    ("severity=CRITICAL disk full", 4),
    ("E/ActivityManager( 1234): ANR in com.example", 3),
    ("W/System.err(  512): java.lang.NullPointerException", 2),
    ("E0921 10:11:12.123456 4182 main.cc:42] Check failed", 3),
    ("error[E0308]: mismatched types", 3),
    ("src/main.c:8:14: warning: format '%d' expects int", 2),
    ("Error: could not compile `inventory`", 3),
    ("panic: runtime error: slice bounds out of range", 4),
    ("[I] 09:00:00 health ok", 1),
    ("E       assert None is not None", 3),  # pytest failure-detail marker
])
def test_detect_level_forms(line, sev):
    assert pare.VOCAB.detect_level(line) == sev


@pytest.mark.parametrize("line", [
    "FAILED tests/test_net.py::test_connect - TimeoutError: timed out",  # word, not a level
    "    E       indented E is not the pytest marker",
    "connection error occurred while reading",  # lowercase mid-line word
    "tests/test_cart.py::test_add PASSED [ 10%]",
    "   Compiling serde v1.0.1",
    "--- FAIL: TestDivZero (0.00s)",
])
def test_detect_level_rejects_words(line):
    assert pare.VOCAB.detect_level(line) is None


@pytest.mark.parametrize("line,sev", [
    ("10.0.0.1 - - [08/Oct/2026:10:00:00 +0000] \"GET /api/cart HTTP/1.1\" 503 512", 3),
    ("INFO  [worker-1] http: GET /api/v1/orders?page=9 500 92ms rid=1386766b", 3),
    ("INFO  [worker-1] http: GET /api/v1/orders?page=9 200 92ms rid=1386766b", 1),
    ("POST /login 401 12ms", 2),
    ("request finished status=502 duration=1.2s", 3),
    ('{"msg":"done","status_code":404,"path":"/x"}', 2),
    ("upstream responded with 504", 3),
    ("HTTP/1.1 302 Found", 1),
    ("Process completed with exit code 1.", 3),
    ("shutdown complete exit=0", 1),
    ("container exited with code 137", 4),
    ("10-08 09:00:00.123  1234  1235 E ActivityManager: ANR in com.example", 3),  # adb logcat threadtime
    ("10-08 09:00:00.123  1234  1235 W PackageManager: unknown package", 2),
    ("go build failed: exit status 2", 3),
    ("command returncode=-9", 4),
])
def test_http_and_exit_code_forms(line, sev):
    assert pare.VOCAB.severity(line) == sev


@pytest.mark.parametrize("line", [
    "fetched 500 items in 2s",  # a count, not a status
    "listening on port 5000",
    "zip code 90210",  # out of status range
    "the script exits 3 times a day",
    "copyright 2026 example",
    "Successfully tagged myapp:1.4.0-rc1",  # rc1 is a release candidate, not rc=1
    "npm notice using node 20.0.0-rc2",
    "query returned 500 rows in 12ms",  # a row count, not an HTTP status
    "blk_update_request: I/O error, dev sda, sector 2048",  # I/O is not logcat's I/Tag
    "kernel: T/F flag set",
    "Process exited with 3 warnings",  # a count after "exited with", not an exit code
    "the job exited with no output",
])
def test_http_and_exit_code_rejections(line):
    assert pare.VOCAB.severity(line) is None


@pytest.mark.parametrize("line,sev", [
    ("returned HTTP 500", 3),
    ("returned status 503 from upstream", 3),
    ("rc=1", 3),
    ("returncode: 2", 3),
    ("exit 1", 3),
    ("exited with status 137", 4),
    ("E/ActivityManager( 1234): ANR in com.example", 3),
    ("10-08 11:00:00.123 W/System.err( 512): java.lang.NullPointerException", 2),
    ("I/ActivityManager: Start proc 1234", 1),
])
def test_tightened_forms_still_match(line, sev):
    assert pare.VOCAB.severity(line) == sev


def test_io_error_line_still_counts_as_error_vocabulary():
    # The old logcat letter form read "I/O" as an INFO level, which then
    # suppressed the error word on the same line.
    assert pare.vocab_score("blk_update_request: I/O error, dev sda, sector 2048") >= 3.0


@pytest.mark.parametrize("line", [
    "Found 0 errors. Watching for file changes.",  # tsc --watch
    "Tests: 0 failed, 12 passed",
    "0 failures, 0 errors, 12 tests run",
    "npm audit: found 0 vulnerabilities",
    "no errors found",
])
def test_zero_counts_are_not_errors(line):
    assert pare.vocab_score(line) < 3.0
    assert not pare.VOCAB.has("error", line)  # every path (marker, locations, pass check) agrees


def test_zero_count_line_is_not_marked_as_error_template():
    # The line sits mid-file so it is listed as a template entry rather than
    # printed by a head or tail window.
    lines = [f"building {i}/80" for i in range(40)] + ["Found 0 errors. Watching for file changes."] + [f"building {i}/80" for i in range(40, 80)]
    data = "\n".join(lines).encode() + b"\n"
    out = run(["--budget", "300"], data).decode()
    tpl = next(l for l in out.splitlines() if "Found 0 errors" in l and l.startswith("T"))
    assert " E " not in tpl


def test_nonzero_counts_still_are():
    assert pare.vocab_score("Tests: 3 failed, 12 passed") >= 3.0
    assert pare.vocab_score("Found 2 errors. Watching for file changes.") >= 3.0


def test_git_head_is_not_noise():
    assert pare.vocab_score("HEAD is now at 3e1f9a2 Merge pull request #4821") >= 0.0


def test_access_log_needle():
    """4,000 CLF lines without a log level; the twelve 5xx responses must be
    the template's exemplar and surface as a histogram."""
    lines = []
    for i in range(4000):
        code = 503 if i % 333 == 7 else 200
        lines.append(f'10.0.{i % 7}.{i % 251} - - [08/Oct/2026:10:{(i // 60) % 60:02d}:{i % 60:02d} +0000] '
                     f'"GET /api/items/{i} HTTP/1.1" {code} {100 + i % 900}')
    data = "\n".join(lines).encode() + b"\n"
    out = run(["--budget", "600"], data).decode()
    tpl = next(l for l in out.splitlines() if l.startswith("T1 "))
    assert "ERROR×12 INFO×3,988" in tpl and " E " in tpl
    assert " L8: " in tpl  # first 503 (i=7) is the exemplar (compact one-line form at this budget)
    # Identical-shape lines get diminishing returns, so the exemplar plus one
    # more 503 is expected at this budget; the histogram carries the count.
    assert out.count('" 503 ') >= 2


def test_info_level_suppresses_error_words():
    info = "2026-10-08 INFO  scan complete: 0 errors, 3 warnings"
    error = "2026-10-08 ERROR scan complete"
    plain_err = "scan failed: 3 errors"
    assert pare.vocab_score(info) < pare.vocab_score(plain_err)
    assert pare.vocab_score(error) > pare.vocab_score(info)


def test_error_level_scores_regardless_of_language():
    de = "2026-10-08 10:04:10 ERROR datenbank verbindung abgebrochen"
    assert pare.vocab_score(de) >= pare.LEVEL_SCORE[3]


def test_mixed_level_template_uses_worst_record_as_exemplar():
    lines = ["2026-10-08T09:00:%02dZ INFO  [w] job %d done" % (i, i) for i in range(60)]
    lines.insert(40, "2026-10-08T09:00:40Z ERROR [w] job 40 done")
    data = "\n".join(lines).encode() + b"\n"
    out = run(["--budget", "400"], data).decode()
    assert "ERROR [w] job 40 done" in out
    assert "ERROR×1 INFO×60" in out


# --- vocabulary file --------------------------------------------------------

def test_parse_vocab_extend_and_replace():
    v = pare.parse_vocab("[error]\nfehler\n[warn replace]\nwarnung\n", pare.DEFAULT_VOCAB, "t")
    assert "fehler" in v["error"] and "error" in v["error"]
    assert v["warn"] == ["warnung"]


@pytest.mark.parametrize("text,msg", [
    ("[bogus]\nx\n", "expected [class]"),
    ("word\n", "before any [class]"),
    ("[error]\n/(unclosed/\n", "bad regex"),
    ("[level-error]\n/re/\n", "plain tokens"),
])
def test_parse_vocab_errors(text, msg):
    with pytest.raises(SystemExit) as e:
        pare.parse_vocab(text, pare.DEFAULT_VOCAB, "t")
    assert msg in str(e.value)


def test_dump_vocab_round_trips(tmp_path):
    dump = run(["--dump-vocab"], b"")
    vf = tmp_path / "vocab"
    vf.write_bytes(dump)
    data = open(os.path.join(CORPUS, "pytest-fail.log"), "rb").read()
    assert run([], data) == run(["--vocab", str(vf)], data)


def test_vocab_level_tokens_extend(tmp_path):
    vf = tmp_path / "vocab"
    vf.write_text("[level-error]\nFEHLER\n")
    lines = ["2026-10-08 10:00:%02d INFO  auftrag %d verarbeitet" % (i % 60, i) for i in range(300)]
    lines.insert(200, "2026-10-08 10:03:20 FEHLER datenbank weg")
    data = "\n".join(lines).encode() + b"\n"
    out = run(["--budget", "300", "--vocab", str(vf)], data).decode()
    tpl_line = next(l for l in out.splitlines() if "FEHLER datenbank weg" in l and l.startswith("T"))
    assert " E " in tpl_line


# --- budget, determinism, selection -----------------------------------------

@pytest.mark.parametrize("name", LOGS)
@pytest.mark.parametrize("budget", [50, 150, 600, 2000])
def test_budget_is_a_hard_limit(name, budget):
    data = open(os.path.join(CORPUS, name), "rb").read()
    out = run(["--budget", str(budget)], data)
    assert pare.tokens(len(out)) <= budget


@pytest.mark.parametrize("name", GENERATED_LOGS)
@pytest.mark.parametrize("budget", [150, 600])
def test_budget_is_a_hard_limit_on_generated_corpus(name, budget):
    data = open(os.path.join(GENERATED, name), "rb").read()
    out = run(["--budget", str(budget)], data)
    assert pare.tokens(len(out)) <= budget


@pytest.mark.parametrize("name", LOGS)
def test_deterministic(name):
    data = open(os.path.join(CORPUS, name), "rb").read()
    assert hashlib.sha256(run([], data)).hexdigest() == hashlib.sha256(run([], data)).hexdigest()


def test_small_input_passes_through():
    data = b"one\nERROR two\nthree\n"
    out = run(["--budget", "100"], data)
    assert out == data  # no header, no growth: the input is the output
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--budget", "100", "--run-dir",
                        os.path.join(ROOT, ".pytest-run-dir-tmp")], input=data, capture_output=True, check=True)
    assert p.stdout.startswith(data) and b"## raw: " in p.stdout  # with a run file, one line is added


def test_only_and_except_select_by_template():
    data = open(os.path.join(CORPUS, "server-outage.log"), "rb").read()
    summary = run([], data).decode()
    # Find the template id of the FATAL line from the summary and expand it.
    tid = next(l.split()[0] for l in summary.splitlines() if "FATAL [db]" in l and l.startswith("T"))
    only = run(["--only", tid], data).decode()
    assert "FATAL [db] giving up" in only
    assert only.count("\n") < 10
    rest = run(["--except", tid, "--budget", "100000"], data).decode()
    assert "FATAL [db] giving up" not in rest


def access_log() -> bytes:
    lines = []
    for i in range(4000):
        code = 503 if i % 333 == 7 else (404 if i % 100 == 50 else 200)
        lines.append(f'10.0.{i % 7}.{i % 251} - - [08/Oct/2026:10:{(i // 60) % 60:02d}:{i % 60:02d} +0000] '
                     f'"GET /api/items/{i} HTTP/1.1" {code} {100 + i % 900}')
    return "\n".join(lines).encode() + b"\n"


def test_min_severity_isolates_errors_inside_a_template():
    out = run(["--only", "T1", "--min-severity", "error", "--budget", "100000"], access_log()).decode()
    body = [l for l in out.splitlines() if GUTTER.match(l)]
    assert len(body) == 12 and all('" 503 ' in l for l in body)
    assert out.splitlines()[0].startswith("# pare --only T1 --min-severity error: 12 of 4,000 lines")


def test_min_severity_alone_and_warn_threshold():
    out = run(["--min-severity", "warn", "--budget", "100000"], access_log()).decode()
    body = [l for l in out.splitlines() if GUTTER.match(l)]
    assert len(body) == 12 + 40  # 503s and 404s, no 200s
    assert not any('" 200 ' in l for l in body)


def test_min_severity_on_server_log_excludes_info():
    data = open(os.path.join(CORPUS, "server-outage.log"), "rb").read()
    out = run(["--min-severity", "warn", "--budget", "100000"], data).decode()
    body = [l for l in out.splitlines() if GUTTER.match(l)]
    assert body and all((" WARN " in l or " ERROR " in l or " FATAL " in l) for l in body if ": 20" in l)
    assert "FATAL [db] giving up" in out


# --- failing tests are a list, not repeats ---------------------------------

FAILING = [f"test_case_{w}" for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                                       "golf", "hotel", "india", "juliet", "kilo", "lima")]


def pytest_summary_log() -> bytes:
    lines = ["============================= test session starts =============================="]
    lines += [f"PASSED tests/test_mod.py::test_ok_{i}" for i in range(400)]
    lines.append("=========================== short test summary info ============================")
    for k, name in enumerate(FAILING):
        # pytest truncates the reason to the terminal width; half have none.
        lines.append(f"FAILED tests/test_mod.py::TestThing::{name}" + (" - TypeError: __in..." if k % 2 else ""))
    lines.append("======================= 12 failed, 400 passed in 3.21s ========================")
    return ("\n".join(lines) + "\n").encode()


def test_distinct_failing_tests_each_survive():
    # One drain template, twelve different tests: each name is news. Before,
    # the template's repeat penalty and the near-duplicate check (four of five
    # tokens shared) left two of them.
    out = run(["--budget", "1000"], pytest_summary_log()).decode()
    assert all(name in out for name in FAILING), out


PARAMS = ["numpy-3-True-1", "numpy-3-True-2", "numpy-7-True-1", "numpy-7-True-2", "numpy-11-False-1", "numpy-11-False-3"]


@pytest.mark.parametrize("form", [
    "FAILED tests/test_roll.py::TestRolling::test_rolling_iter[{p}]",  # pytest
    "testRollingIter[{p}](com.example.RollingTest)  Time elapsed: 0.01 sec  <<< FAILURE!",  # JUnit 4 Parameterized
])
def test_parametrized_failures_that_differ_only_in_numbers_each_survive(form):
    # Masking turns [numpy-3-True-1] and [numpy-3-True-2] into one shape, so
    # they were repeats of each other; a name's bracketed parameters are part
    # of the name.
    lines = ["============================= test session starts =============================="]
    lines += [f"PASSED tests/test_roll.py::test_ok_{i}" for i in range(400)]
    lines += [f"PASSED tests/test_roll.py::test_window[{i}-True]" for i in range(30)]
    lines += [form.format(p=p) for p in PARAMS]
    lines.append("======================= 6 failed, 430 passed in 3.21s ========================")
    out = run(["--budget", "1000"], ("\n".join(lines) + "\n").encode()).decode()
    assert all(f"[{p}]" in out for p in PARAMS), out


def test_many_failing_tests_are_listed_compactly():
    # Forty failing tests: as lines they cost ~1,200 tokens; as a list, the
    # shared "FAILED tests/test_api.py::TestClient::" once and each name under it.
    names = [f"test_endpoint_{w}_{k}" for w in ("get", "put", "post", "head", "patch") for k in range(8)]
    lines = ["============================= test session starts =============================="]
    lines += [f"PASSED tests/test_api.py::TestClient::test_ok_{i}" for i in range(300)]
    lines += [f"FAILED tests/test_api.py::TestClient::{n}" for n in names]
    lines.append("======================= 40 failed, 300 passed in 9.12s ========================")
    out = run(["--budget", "1000"], ("\n".join(lines) + "\n").encode()).decode()
    assert "FAILED tests/test_api.py::TestClient::{…}" in out
    items = [l for l in out.splitlines() if l.startswith("  …")]
    assert sorted(l[3:] for l in items) == sorted(names), out
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    import run as bench_run
    keep = [f"tests/test_api.py::TestClient::{n}" for n in names]
    assert bench_run.recall(out.encode(), keep) == (40, 40)
    assert bench_run.recall(out.encode(), keep, strict=True)[0] < 40


def test_rare_failures_among_many_come_first():
    # Sixty tests fail; fifty-seven on a missing module (a broken
    # environment), three on an assertion. Those three are the news: placed
    # first, and listed first under a header that says so.
    rare = {7, 31, 52}
    lines = ["============================= test session starts =============================="]
    lines += [f"tests/test_mod.py::test_ok_{i} PASSED" for i in range(200)]
    lines.append("=================================== FAILURES ===================================")
    for k in range(60):
        lines.append(f"____________________ test_case_{k} ____________________")
        lines += ["    def test_x():", ">       assert f() == 1"]
        if k in rare:
            lines += ["E       assert 2 == 1", f"tests/test_b.py:{k}: AssertionError"]
        else:
            lines += ["E       ImportError: No module named 'foo'", f"tests/test_b.py:{k}: ImportError"]
    lines.append("=========================== short test summary info ============================")
    lines += [f"FAILED tests/test_b.py::test_case_{k} - ..." for k in range(60)]
    lines.append("======================= 60 failed, 200 passed in 9.12s ========================")
    out = run(["--budget", "600"], ("\n".join(lines) + "\n").encode()).decode()
    assert "(3 unlike the rest, first) E FAILED tests/test_b.py::{…}" in out, out
    items = [l for l in out.splitlines() if l.startswith("  …test_case_")]
    assert [int(re.match(r"  …test_case_(\d+)", l).group(1)) for l in items[:3]] == [7, 31, 52], out


def test_bench_expands_only_the_list_it_is_under():
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    import run as bench_run
    text = "T2   ×3 E FAILED t.py::{…}\n  …test_a\n  …test_b\n  ┆ (+1 more)\n## selected lines\n  …test_c\n"
    out = bench_run.expand_lists(text)
    assert "FAILED t.py::test_a" in out and "FAILED t.py::test_b" in out
    assert "t.py::test_c" not in out  # outside the list: left as written


def test_repeated_error_events_still_diminish():
    # Crashes of the same nineteen pods, over and over. Masking leaves the pod
    # names, so each pod is its own shape, but the template has few shapes per
    # record: they stay repeats of one template, not a list of nineteen items.
    pods = [f"web-{c}{d}q" for c in "abcde" for d in "wxyz"]
    lines = []
    for i in range(400):
        lines.append(f"2026-10-08T09:{i // 60:02d}:{i % 60:02d}Z INFO  [sync] tick {i}")
        if i % 2 == 0:
            lines.append(f"2026-10-08T09:{i // 60:02d}:{i % 60:02d}Z ERROR [kube] container web in pod {pods[i % 19]}"
                         " crashed: exception in handler, exit code 137")
    out = run(["--budget", "2000"], ("\n".join(lines) + "\n").encode()).decode()
    assert "T2   ×200 E" in out
    assert len([l for l in body_lines(out) if "crashed" in l]) <= 2, out


@pytest.mark.parametrize("advice", [
    "[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.",
    "[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
    "[ERROR] For more information about the errors and possible solutions, please read the following articles:",
    "[ERROR] -> [Help 1]",
])
def test_build_tool_advice_ranks_below_a_failing_test(advice):
    failing = "testFoo(com.example.FooTest)  Time elapsed: 0.012 sec  <<< FAILURE!"
    assert pare.vocab_score(advice) < pare.vocab_score(failing)


def body_lines(out: str) -> list[str]:
    return [l for l in out.splitlines() if GUTTER.match(l)]


def test_since_until_restrict_a_selection():
    data = access_log()
    out = run(["--only", "T1", "--since", "L100", "--until", "L109", "--budget", "100000"], data).decode()
    body = body_lines(out)
    assert [int(l.split(":")[0]) for l in body] == list(range(100, 110))
    assert out.splitlines()[0].startswith("# pare --only T1 --since L100 --until L109: 10 of 4,000 lines")


def test_since_alone_and_bare_number():
    data = access_log()
    out = run(["--since", "3990", "--budget", "100000"], data).decode()
    assert [int(l.split(":")[0]) for l in body_lines(out)] == list(range(3990, 4001))


def test_since_negative_counts_from_end():
    data = access_log()
    out = run(["--since", "-5", "--budget", "100000"], data).decode()
    assert [int(l.split(":")[0]) for l in body_lines(out)] == list(range(3996, 4001))


def test_since_combines_with_min_severity():
    data = access_log()
    out = run(["--min-severity", "error", "--since", "L2000", "--budget", "100000"], data).decode()
    nos = [int(l.split(":")[0]) for l in body_lines(out)]
    assert nos and all(n >= 2000 for n in nos) and all('" 503 ' in l for l in body_lines(out))
    assert len(nos) == sum(1 for i in range(1999, 4000) if i % 333 == 7)


@pytest.mark.parametrize("args,msg", [
    (["--since", "abc"], b"expects a line number"),
    (["--since", "0"], b"numbered from 1"),
    (["--since", "50", "--until", "10"], b"is after"),
])
def test_since_until_errors(args, msg):
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--no-tee", *args],
                       input=b"x\n" * 100, capture_output=True)
    assert p.returncode == 1 and msg in p.stderr


def test_min_severity_rejects_unknown_level():
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--no-tee", "--min-severity", "loud"],
                       input=b"x\n", capture_output=True)
    assert p.returncode == 2 and b"invalid choice" in p.stderr


def test_chars_per_token_calibrates_the_budget():
    data = open(os.path.join(CORPUS, "server-outage.log"), "rb").read()
    out_default = run(["--budget", "600"], data)
    out4 = run(["--budget", "600", "--chars-per-token", "4"], data)
    assert len(out_default) <= 600 * pare.CHARS_PER_TOKEN and len(out4) <= 600 * 4
    assert pare.CHARS_PER_TOKEN == 2.8 and len(out_default) < len(out4)
    assert b"(budget 600)" in out_default  # the header speaks in tokens, not bytes


def test_memory_stays_small_on_large_input(tmp_path):
    """The single pass keeps a few bytes per line, not the lines: a 6 MB input
    (the CI log eight times over) must summarise in well under 100 MB of
    peak memory. The all-in-memory design needed about 190 MB here."""
    data = open(os.path.join(CORPUS, "ci-pipeline.log"), "rb").read() * 8
    big = tmp_path / "big.log"
    big.write_bytes(data)
    probe = (
        "import resource, subprocess, sys\n"
        f"subprocess.run([sys.executable, {os.path.join(HERE, 'pare.py')!r}, '--no-tee', '--budget', '2000'],"
        f" stdin=open({str(big)!r}, 'rb'), stdout=subprocess.DEVNULL, check=True)\n"
        "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n"
    )
    p = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    peak_kb = int(p.stdout.strip())
    assert peak_kb < 100 * 1024, f"peak RSS {peak_kb // 1024} MB"


def test_run_file_is_content_addressed(tmp_path):
    data = b"a\nb\nERROR c\n" * 50
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--run-dir", str(tmp_path)],
                       input=data, capture_output=True, check=True)
    digest = hashlib.sha256(data).hexdigest()[:12]
    assert (tmp_path / f"{digest}.log").read_bytes() == data
    assert str(tmp_path / f"{digest}.log") in p.stdout.decode()


# --- follow mode ---------------------------------------------------------------

def follow_blocks(out: str) -> list[list[str]]:
    """Split follow output into its blocks (each ends with a blank line)."""
    blocks, cur = [], []
    for line in out.split("\n"):
        if line == "" and cur:
            blocks.append(cur)
            cur = []
        elif line != "":
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def test_follow_windows_cover_the_input_and_keep_the_budget():
    data = open(os.path.join(CORPUS, "ci-pipeline.log"), "rb").read()
    out = run(["--follow", "--every", "3000", "--budget", "600"], data).decode()
    blocks = follow_blocks(out)
    windows = [b for b in blocks if b[0].startswith("# pare #")]
    assert windows and blocks[-1][0].startswith("# pare: end of input · 17,832 lines")
    ranges = [re.match(r"# pare #(\d+): L([\d,]+)–L([\d,]+)", b[0]) for b in windows]
    assert all(ranges)
    nums = [(int(m.group(1)), int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))) for m in ranges]
    assert [k for k, _, _ in nums] == list(range(1, len(nums) + 1))
    assert nums[0][1] == 1 and nums[-1][2] == 17832
    assert all(nums[i][2] + 1 == nums[i + 1][1] for i in range(len(nums) - 1))  # contiguous
    for b in windows:
        assert pare.tokens(len(("\n".join(b) + "\n").encode())) <= 600, b[0]
    # a window boundary never splits a record, so it may run past --every by one record
    assert all(3000 <= hi - lo + 1 <= 3000 + pare.MAX_RECORD_LINES for _, lo, hi in nums[:-1])
    assert "## head" in "\n".join(windows[0]) and not any("## head" in "\n".join(b) for b in windows[1:])


def test_follow_template_ids_match_the_batch_pass(tmp_path):
    """Windows end at record boundaries, so segmentation and drain see the
    same head lines in the same order as one batch pass: same T ids."""
    import io
    data = open(os.path.join(CORPUS, "ci-pipeline.log"), "rb").read()

    def gids(every):
        spool = tmp_path / f"spool-{every}"
        with open(spool, "wb") as f:
            p = pare.Pass(200, f, str(spool))
            out = []
            def cut():
                st = p.cut()
                out.extend(st.templates[st.rtpl[st.rec[i]]].gid if st.rec[i] >= 0 else -1 for i in range(len(st)))
            p.every, p.on_full = every, cut
            p.feed(data)
            p.finish()
            cut()
        return out, len(p.globals)

    batch, nb = gids(0)
    windowed, nw = gids(500)
    assert len(batch) == len(windowed) == 17832
    assert batch == windowed and nb == nw


def test_follow_quiet_window_is_a_heartbeat():
    data = open(os.path.join(CORPUS, "server-outage.log"), "rb").read()
    out = run(["--follow", "--every", "1000", "--budget", "400"], data).decode()
    beats = [l for l in out.splitlines() if l.startswith("# pare #") and "nothing new" in l]
    assert beats, "a long stretch of INFO lines should be a one-line window"
    assert not [l for l in run(["--follow", "--every", "1000", "--budget", "400", "--quiet"], data).decode().splitlines()
                if "nothing new" in l]
    assert "new" in out  # the outage introduces shapes the earlier windows had not seen


def test_follow_marks_new_templates_only_after_the_first_window():
    data = open(os.path.join(CORPUS, "ci-pipeline.log"), "rb").read()
    out = run(["--follow", "--every", "4000", "--budget", "600"], data).decode()
    blocks = [b for b in follow_blocks(out) if b[0].startswith("# pare #")]
    assert " new " not in "\n".join(blocks[0]) and "new" not in blocks[0][0]
    assert any(" new " in line for line in blocks[1])


def test_follow_rejects_run_file_flags():
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--no-tee", "--follow", "--only", "T1"],
                       input=b"x\n", capture_output=True)
    assert p.returncode != 0 and b"--follow" in p.stderr


def test_follow_run_file_keeps_every_byte(tmp_path):
    data = open(os.path.join(CORPUS, "go-test.log"), "rb").read()
    run_file = tmp_path / "run.log"
    p = subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--follow", "--every", "50",
                        "--run-file", str(run_file), "--budget", "300"], input=data, capture_output=True, check=True)
    assert run_file.read_bytes() == data
    out = p.stdout.decode()
    assert f"## raw: {run_file} --since L1 --until L" in out
    assert out.rstrip().splitlines()[-1].endswith(f"raw: {run_file}")


def test_follow_timed_cut_does_not_split_a_record(tmp_path):
    """A cut that lands inside a record (an --interval or --flush-on flush
    while a trace is still arriving) carries the record into the next
    window: its frames inherit the error head's salience and are attributed
    to the same template, so the template ids match a batch pass."""
    head = b"2026-10-08T10:00:00Z ERROR worker crashed: ValueError: bad input\n    at handler (app/x.py:10)\n"
    rest = b"    at main (app/main.py:3)\n    at run (app/run.py:99)\nINFO recovered\n"

    def gids(cut_between):
        spool = tmp_path / f"spool-{int(cut_between)}"
        out, stores = [], []
        with open(spool, "wb") as f:
            p = pare.Pass(200, f, str(spool))
            def cut():
                st = p.cut()
                stores.append(st)
                out.extend(st.templates[st.rtpl[st.rec[i]]].gid if st.rec[i] >= 0 else -1 for i in range(len(st)))
            p.feed(head)
            if cut_between:
                cut()
            p.feed(rest)
            p.finish()
            cut()
        return out, len(p.globals), stores

    batch, nb, _ = gids(False)
    split, ns, stores = gids(True)
    assert batch == split == [0, 0, 0, 0, 1] and nb == ns == 2
    # the second window's record 0 continues the trace: scored from the carried ERROR head
    w2 = stores[1]
    assert w2.carry is not None and w2.carry.text.startswith("2026-10-08T10:00:00Z ERROR")
    assert w2.depth[0] > 0 and w2.score[0] > 0  # a frame, not a new head; inherits the error's vocabulary
    w2.open()
    pare.score_templates(w2)
    assert w2.templates[0].marker == "E"
    w2.close()


def test_follow_interval_cut_mid_trace_end_to_end(tmp_path):
    producer = (
        "import sys, time\n"
        "w = sys.stdout.buffer\n"
        "w.write(b'ERROR worker crashed: ValueError: bad input\\n    at handler (app/x.py:10)\\n'); w.flush()\n"
        "time.sleep(1.2)\n"
        "w.write(b''.join(b'    at frame%d (app/f%d.py:%d)\\n' % (i, i, i) for i in range(30))); w.flush()\n"
    )
    script = tmp_path / "producer.py"
    script.write_text(producer)
    cmd = (f"{sys.executable} {script} | {sys.executable} {os.path.join(HERE, 'pare.py')}"
           " -f --interval 0.4 --flush-on none --no-tee --budget 300")
    out = subprocess.run(cmd, shell=True, capture_output=True, check=True).stdout.decode()
    blocks = [b for b in follow_blocks(out) if b[0].startswith("# pare #")]
    assert len(blocks) == 2, out
    second = "\n".join(blocks[1])
    assert "(cont.)" in second and "ERROR worker crashed" in second, second  # the entry shows the carried head
    assert out.rstrip().splitlines()[-1].split("·")[2].strip() == "1 templates"


def test_follow_random_cuts_match_the_batch_pass(tmp_path):
    """Differential test: a log with traces, blank lines inside blocks, nested
    indentation and pytest E/> lines, cut into windows at random positions
    (timed cuts can land anywhere, a blank line included). Every non-blank
    line must have the same record head and global template id as one
    batch pass. Blank lines are excluded: a single blank pending at a cut is
    adopted into the record by the batch pass but stays unattributed in the
    window already printed, which only moves it between ledger counts."""
    import random
    rng = random.Random(7)
    lines = []
    for i in range(400):
        kind = rng.choice(["info", "info", "trace", "pytest", "nested"])
        if kind == "info":
            lines.append(f"2026-10-08T10:{i % 60:02d}:00Z INFO worker{i % 5} handled job {i}")
        elif kind == "trace":
            lines.append(f"2026-10-08T10:{i % 60:02d}:00Z ERROR worker{i % 5} crashed: ValueError: bad input {i}")
            for f in range(rng.randint(1, 6)):
                if f == 2 and rng.random() < 0.5:
                    lines.append("")  # a single blank line inside the block
                lines.append(f"    at f{f} (app/m{f}.py:{i + f})")
        elif kind == "pytest":
            lines.append(f"tests/test_x.py::test_{i} FAILED")
            lines.append(f">       assert total({i}) == {i + 1}")
            lines.append(f"E       assert {i} == {i + 1}")
            lines.append(f"E        +  where {i} = total({i})")
        else:
            lines.append(f"build step {i}:")
            lines.append(f"  compiling unit {i}")
            lines.append(f"    warning: unused variable v{i}")
            lines.append("")
            lines.append(f"    note: see unit {i}")
        if rng.random() < 0.15:
            lines.append("")
            if rng.random() < 0.3:
                lines.append("")
    data = ("\n".join(lines) + "\n").encode()
    n = len(lines)

    def heads(cuts):
        spool = tmp_path / f"spool-{len(cuts)}"
        out = []
        with open(spool, "wb") as f:
            p = pare.Pass(200, f, str(spool))

            def take():
                st = p.cut()
                for i in range(len(st)):
                    ri = st.rec[i]
                    if ri < 0:
                        out.append(None)
                        continue
                    head_no = st.carry.no if (ri == 0 and st.carry is not None) else st.base + st.rstart[ri] + 1
                    out.append((head_no, st.templates[st.rtpl[ri]].gid))

            for i, line in enumerate(lines):
                p.feed(line.encode() + b"\n")
                if i in cuts:
                    take()
            p.finish()
            take()
        return out, len(p.globals)

    batch, nb = heads(set())
    assert len(batch) == n
    # Blank lines inside blocks: a cut right before and right after one
    # leaves a window holding only that blank line, which must still hand
    # the open record on.
    in_block_blanks = [j for j in range(1, n - 1) if lines[j] == "" and lines[j + 1].startswith("    ")]
    assert len(in_block_blanks) >= 10
    trials = [set(range(n - 1))]  # a cut after every line
    for _ in range(12):
        cuts = set(rng.sample(range(n - 1), rng.randint(5, 40)))
        for j in rng.sample(in_block_blanks, 3):
            cuts.update((j - 1, j))
        trials.append(cuts)
    for trial, cuts in enumerate(trials):
        windowed, nw = heads(cuts)
        assert nw == nb, (trial, sorted(cuts))
        for i in range(n):
            if lines[i].strip():
                assert windowed[i] == batch[i], (trial, i, lines[i], batch[i], windowed[i], sorted(cuts))


# --- command mode ---------------------------------------------------------------

def pare_cmd(args, tmp_path=None, **kw):
    base = [sys.executable, os.path.join(HERE, "pare.py")]
    base += ["--run-dir", str(tmp_path)] if tmp_path else ["--no-tee"]
    return subprocess.run(base + args, capture_output=True, **kw)


def test_command_mode_keeps_the_exit_code_and_records_the_command(tmp_path):
    p = pare_cmd(["--", "sh", "-c", "echo hello; echo oops >&2; exit 3"], tmp_path)
    assert p.returncode == 3
    out = p.stdout.decode().splitlines()
    assert out[:2] == ["hello", "oops"]  # stdout and stderr merged, in order
    assert out[-1].startswith("## raw: ") and out[-1].endswith(" · exit 3")
    meta_files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
    assert len(meta_files) == 1
    meta = json.load(open(tmp_path / meta_files[0]))
    assert meta["command"] == ["sh", "-c", "echo hello; echo oops >&2; exit 3"] and meta["exit"] == 3
    assert meta["lines"] == 2 and os.path.exists(tmp_path / meta_files[0].replace(".json", ".log"))


def test_command_mode_summary_ends_with_the_exit_status():
    log = os.path.join(CORPUS, "pytest-fail.log")
    p = pare_cmd(["--budget", "300", "--", "sh", "-c", f"cat {log}; exit 1"])
    assert p.returncode == 1
    text = p.stdout.decode()
    assert text.startswith("# pare: 308 lines")
    assert text.rstrip().splitlines()[-1] == "## raw: (not saved) · exit 1"
    assert pare.tokens(len(p.stdout)) <= 300


def test_command_mode_errors_and_signals():
    p = pare_cmd(["--", "no-such-command-for-pare-tests"])
    assert p.returncode == 127 and b"command not found" in p.stderr
    p = pare_cmd(["--", "true"])
    assert p.returncode == 0 and p.stdout == b"# pare: no output \xc2\xb7 exit 0\n"
    p = pare_cmd(["--", "sh", "-c", "echo before; kill -9 $$"])
    assert p.returncode == 137 and p.stdout.decode().splitlines() == ["before", "## exit 137"]
    p = pare_cmd(["--only", "T1", "--", "true"])
    assert p.returncode != 0 and b"read a run file" in p.stderr


def test_command_mode_survives_a_timeout_and_a_leftover_background_job():
    import signal
    import time
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "pare.py"), "--no-tee", "--", "sh", "-c",
                          "echo started; echo ERROR stuck; sleep 30"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    time.sleep(1.0)
    os.kill(p.pid, signal.SIGTERM)  # aimed at pare alone, as a tool timeout might be
    out, _ = p.communicate(timeout=15)
    assert p.returncode == 143 and out.decode().splitlines() == ["started", "ERROR stuck", "## exit 143"]
    t = time.time()
    p = pare_cmd(["--", "sh", "-c", "echo up; sleep 30 & echo done"], timeout=15)
    assert p.returncode == 0 and time.time() - t < 10  # the background sleep holds the pipe; pare does not wait for it
    assert p.stdout.decode().splitlines() == ["up", "done", "## exit 0"]


def test_command_mode_with_follow():
    p = pare_cmd(["-f", "--every", "100", "--budget", "200", "--", "sh", "-c",
                  "for i in $(seq 1 250); do echo \"INFO step $i\"; done; echo ERROR boom; exit 2"])
    assert p.returncode == 2
    last = p.stdout.decode().rstrip().splitlines()[-1]
    assert last.startswith("# pare: end of input · 251 lines") and last.endswith(" · exit 2")


# --- bench/collect.py -------------------------------------------------------------

def collect(args, **kw):
    return subprocess.run([sys.executable, os.path.join(ROOT, "bench", "collect.py")] + args,
                          capture_output=True, text=True, **kw)


def test_collect_turns_a_run_file_into_a_held_out_case(tmp_path):
    runs, corpus = tmp_path / "runs", tmp_path / "heldout"
    secret = tmp_path / "out.txt"
    cwd_marker = os.getcwd()
    secret.write_text(f"ERROR in {cwd_marker}/src/x.py\napi_key=abcdef123456\n")
    p = pare_cmd(["--", "sh", "-c", f"cat {secret}; exit 1"], runs)
    assert p.returncode == 1
    listing = collect(["--runs", str(runs), "list"])
    assert "sh -c" in listing.stdout and "   1" in listing.stdout
    added = collect(["--runs", str(runs), "--corpus", str(corpus), "add", "1", "--name", "case-a"])
    assert added.returncode == 0, added.stderr
    assert "possible credentials on 1 line(s): L2" in added.stdout
    log = (corpus / "case-a.log").read_text()
    assert "ERROR in /home/dev/proj/src/x.py" in log and cwd_marker not in log  # working directory scrubbed
    assert (corpus / "case-a.cmd").read_text().startswith("sh -c ")
    assert (corpus / "case-a.keep").read_text() == ""
    manifest = (corpus / "MANIFEST.tsv").read_text().splitlines()
    assert manifest[0].startswith("name\tcollected\tpare") and manifest[1].startswith("case-a\t")
    # never shows pare's summary: no template ids, no section headers
    assert "## " not in added.stdout and not re.search(r"\bT\d+\b", added.stdout)
    assert collect(["--corpus", str(corpus), "check"]).returncode == 1  # empty keep list
    (corpus / "case-a.keep").write_text("ERROR in /home/dev/proj/src/x.py\n")
    assert collect(["--corpus", str(corpus), "check"]).returncode == 0
    (corpus / "case-a.keep").write_text("a line that is not in the log\n")
    bad = collect(["--corpus", str(corpus), "check"])
    assert bad.returncode == 1 and "keep line not in log" in bad.stdout
    again = collect(["--runs", str(runs), "--corpus", str(corpus), "add", "1", "--name", "case-a"])
    assert again.returncode != 0 and "exists" in again.stderr


def test_collect_needs_the_command_for_a_piped_run(tmp_path):
    runs = tmp_path / "runs"
    subprocess.run([sys.executable, os.path.join(HERE, "pare.py"), "--run-dir", str(runs)],
                   input=b"ERROR x\n", capture_output=True, check=True)
    p = collect(["--runs", str(runs), "--corpus", str(tmp_path / "c"), "add", "1"], stdin=subprocess.DEVNULL)
    assert p.returncode != 0 and "--cmd" in p.stderr
    p = collect(["--runs", str(runs), "--corpus", str(tmp_path / "c"), "add", "1", "--cmd", "make"],
                stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and (tmp_path / "c" / "make-{}.cmd".format(__import__("datetime").date.today().strftime("%Y%m%d"))).exists()


# --- integrations/claude-code/pare-hook.py ----------------------------------------

HOOK = os.path.join(ROOT, "integrations", "claude-code", "pare-hook.py")


def load_hook():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pare_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command,expected", [
    ("pytest -x tests/", "{pare} --budget 2000 -- pytest -x tests/"),
    ("pytest 2>&1", "{pare} --budget 2000 -- pytest"),
    ("CI=1 npx jest --runInBand", "CI=1 {pare} --budget 2000 -- npx jest --runInBand"),
    ("npm run test:unit", "{pare} --budget 2000 -- npm run test:unit"),
    ("pytest -k 'a or b'", "{pare} --budget 2000 -- pytest -k 'a or b'"),
    ("./gradlew build", "{pare} --budget 2000 -- ./gradlew build"),
    ("terraform apply -auto-approve", "{pare} --budget 2000 -- terraform apply -auto-approve"),
    ("npm run dev", None), ("make dev", None), ("tsc --watch", None), ("kubectl logs -f deploy/api", None),
    ("./gradlew build --continuous", None), ("terraform apply", None),
    ("npm test 2>&1 | tail -50", None), ("cd app && pytest", None), ("pytest > out.txt", None),
    ("pytest $(ls tests)", None), ("cat big.log", None), ("git diff", None), ("pare -- pytest", None),
    ("pytest 'unbalanced", None),
])
def test_hook_wraps_only_simple_noisy_commands(command, expected, monkeypatch):
    hook = load_hook()
    monkeypatch.setenv("PARE_BIN", sys.executable)
    monkeypatch.delenv("PARE_HOOK_DISABLE", raising=False)
    monkeypatch.delenv("PARE_HOOK_EXTRA", raising=False)
    new, why = hook.plan(command)
    assert new == (expected.format(pare=__import__("shlex").quote(sys.executable)) if expected else None), why


def test_hook_event_io(monkeypatch):
    env = dict(os.environ, PARE_BIN=sys.executable)
    env.pop("PARE_HOOK_DECISION", None)
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "cargo test", "description": "Run tests", "timeout": 120000}}
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(event), capture_output=True, text=True, env=env)
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and "permissionDecision" not in out
    assert out["updatedInput"] == {**event["tool_input"], "command": f"{sys.executable} --budget 2000 -- cargo test"}
    env["PARE_HOOK_DECISION"] = "ask"
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(event), capture_output=True, text=True, env=env)
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    for other in ({**event, "tool_name": "Read"}, {**event, "tool_input": {"command": "ls -la"}},
                  {**event, "tool_input": {"command": "cargo test", "run_in_background": True}}):
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(other), capture_output=True, text=True, env=env)
        assert p.returncode == 0 and p.stdout == ""
    p = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout == ""  # never breaks the tool call
    env["PARE_BIN"] = "no-such-pare-binary"
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(event), capture_output=True, text=True, env=env)
    assert p.stdout == ""  # pare missing: leave the command alone


# --- bench/harvest.py -------------------------------------------------------------

def harvest(args, **kw):
    return subprocess.run([sys.executable, os.path.join(ROOT, "bench", "harvest.py")] + args,
                          capture_output=True, text=True, **kw)


def test_harvest_builds_scrubbed_candidates_from_a_transcript(tmp_path):
    """A synthetic Claude Code transcript with one of each case the harvester
    handles: a failing test run, a saved large output, a pare-wrapped run
    with its run file, a file read, a dev-corpus replay, a duplicate."""
    saved = tmp_path / "tool-results" / "big.txt"
    saved.parent.mkdir()
    saved.write_text("".join(f"Compiling module {i} at /Users/alice/work/app/src/m{i}.rs\n" for i in range(150))
                     + "error[E0308]: mismatched types\n --> src/m7.rs:12:5\n")
    run_file = tmp_path / "runs" / "abc123.log"
    run_file.parent.mkdir()
    run_file.write_text("".join(f"INFO step {i}\n" for i in range(120)) + "FAILED tests/test_api.py::test_login - KeyError\n")
    pytest_out = ("".join(f"tests/test_m.py::test_case_{i} PASSED\n" for i in range(100))
                  + "tests/test_m.py::test_total FAILED\n"
                  + "E       assert 0.9000000000000001 == 0.9\n"
                  + "tests/test_m.py:18: AssertionError\n"
                  + "author alice.smith@corp.example.org pushed from /home/alice/proj\n"
                  + "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
                  + "== 1 failed, 100 passed ==\n")
    dev_replay = open(os.path.join(CORPUS, "go-test.log")).read()
    events = []

    def call(i, command, *, result, is_error=False, tur=None, follow=()):
        tid = f"toolu_{i:024d}"
        events.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}})
        events.append({"type": "user", "sessionId": "s1", "timestamp": "2026-10-01T10:00:00Z",
                       "cwd": "/Users/alice/work/app",
                       "message": {"role": "user", "content": [
                           {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}]},
                       "toolUseResult": tur if tur is not None else result})
        for f in follow:
            events.append({"type": "assistant", "message": {"role": "assistant", "content": [f]}})
        events.append({"type": "user", "message": {"role": "user", "content": "next please"}})

    call(1, "cd /Users/alice/work/app && pytest -q 2>&1", result="Exit code 1\n" + pytest_out, is_error=True,
         follow=[{"type": "text", "text": "The failure is `E       assert 0.9000000000000001 == 0.9` in tests/test_m.py:18."},
                 {"type": "tool_use", "id": "x1", "name": "Read", "input": {"file_path": "/Users/alice/work/app/tests/test_m.py"}}])
    call(2, "cargo build", result="<persisted-output>\nOutput too large. Full output saved to: " + str(saved),
         tur={"stdout": "Compiling...", "stderr": "", "persistedOutputPath": str(saved)},
         follow=[{"type": "tool_use", "id": "x2", "name": "Read", "input": {"file_path": "/Users/alice/work/app/src/m7.rs"}}])
    call(3, "pare --budget 600 -- npm test", result=f"# pare: 121 lines ...\n## raw: {run_file} · exit 1",
         tur={"stdout": f"# pare: 121 lines ...\n## raw: {run_file} · exit 1", "stderr": ""},
         follow=[{"type": "tool_use", "id": "x3", "name": "Bash", "input": {"command": "pytest tests/test_api.py::test_login"}}])
    call(4, "cat src/lib.rs", result="".join(f"line {i}\n" for i in range(200)))
    call(5, "go test ./...", result=dev_replay, tur={"stdout": dev_replay, "stderr": ""})
    call(6, "pytest -q", result="Exit code 1\n" + pytest_out, is_error=True)  # same output again
    transcript = tmp_path / "projects" / "-Users-alice-work-app" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    corpus = tmp_path / "heldout"

    p = harvest(["--corpus", str(corpus), "scan", "--projects", str(tmp_path / "projects"),
                 "--no-identity", "--redact", "alice"])
    assert p.returncode == 0, p.stderr
    assert "3 candidate(s)" in p.stdout, p.stdout
    assert "not a build/test-like command" in p.stdout and "mostly dev-corpus lines" in p.stdout
    assert "already harvested" in p.stdout
    inbox = corpus / "inbox"
    names = sorted(f[:-4] for f in os.listdir(inbox) if f.endswith(".log"))
    assert len(names) == 3
    by_cmd = {(inbox / f"{n}.cmd").read_text().strip(): n for n in names}
    assert set(by_cmd) == {"cd /Users/dev/work/app && pytest -q 2>&1", "cargo build", "npm test"}

    py = by_cmd["cd /Users/dev/work/app && pytest -q 2>&1"]
    log = (inbox / f"{py}.log").read_text()
    assert "alice" not in log.lower() and "user@example.com" in log and "/home/dev/proj" in log
    assert "ghp_" not in log and "<redacted>" in log
    meta = json.loads((inbox / f"{py}.json").read_text())
    assert meta["exit"] == 1 and meta["scrubbed"]["credentials"] == 1 and meta["scrubbed"]["emails"] == 1
    proposed = (inbox / f"{py}.keep.proposed").read_text().splitlines()
    assert "E       assert 0.9000000000000001 == 0.9" in proposed  # quoted by the agent
    assert "tests/test_m.py:18: AssertionError" in proposed  # its location used next
    assert not any("PASSED" in line for line in proposed)

    cargo = by_cmd["cargo build"]
    assert json.loads((inbox / f"{cargo}.json").read_text())["output_from"] == "saved"
    assert "src/m7.rs:12:5" in (inbox / f"{cargo}.keep.proposed").read_text()  # the agent opened m7.rs next
    assert "/Users/dev/work/app/src/m0.rs" in (inbox / f"{cargo}.log").read_text()

    npm = by_cmd["npm test"]
    assert (inbox / f"{npm}.log").read_text() == run_file.read_text()  # the raw run file, not pare's summary
    assert (inbox / f"{npm}.keep.proposed").read_text().strip() == "FAILED tests/test_api.py::test_login - KeyError"

    again = harvest(["--corpus", str(corpus), "scan", "--projects", str(tmp_path / "projects"), "--no-identity"])
    assert "0 candidate(s)" in again.stdout  # idempotent

    assert harvest(["--corpus", str(corpus), "promote", py]).returncode == 1  # no keep list yet
    (inbox / f"{py}.keep").write_text("E       assert 0.9000000000000001 == 0.9\n== 1 failed, 100 passed ==\n")
    p = harvest(["--corpus", str(corpus), "promote", py])
    assert p.returncode == 0, p.stdout
    assert (corpus / f"{py}.log").exists() and not (inbox / f"{py}.log").exists()
    row = (corpus / "MANIFEST.tsv").read_text().splitlines()[-1].split("\t")
    assert row[0] == py and row[-2:] == ["transcript", "reviewed proposals"]
    assert collect(["--corpus", str(corpus), "check"]).returncode == 0


# --- bench/swebench_cases.py --------------------------------------------------------

def load_swebench_cases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("swebench_cases", os.path.join(ROOT, "bench", "swebench_cases.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    spec.loader.exec_module(mod)
    return mod


def fake_task(iid, repo="acme/widgets", f2p=("tests/test_w.py::test_new",)):
    script = "\n".join([
        "#!/bin/bash", "set -uxo pipefail", "cd /testbed", "git apply -v - <<'EOF_1'", "diff", "EOF_1",
        ": '>>>>> Start Test Output'", "pytest -rA tests/test_w.py", ": '>>>>> End Test Output'",
        "git checkout HEAD -- tests/test_w.py"]) + "\n"
    return {"instance_id": iid, "repo": repo, "version": "1.0", "base_commit": "abc",
            "image": f"example/{iid}:latest", "FAIL_TO_PASS": json.dumps(list(f2p)), "PASS_TO_PASS": "[]",
            "log_parser": "parse_log_pytest", "eval_type": "pass_and_fail", "eval_script": script}


def fake_log(failing, passing=(), filler=60):
    body = ["============================= test session starts =============================="]
    body += [f"tests/test_w.py::test_old_{i} PASSED" for i in range(filler)]
    body += ["=========================== short test summary info ============================"]
    body += [f"PASSED {t}" for t in passing] + [f"FAILED {t} - AssertionError: boom" for t in failing]
    body += [f"========================= {len(failing)} failed, {filler} passed in 0.5s =========================="]
    code = 1 if failing else 0
    return "\n".join(["+ cd /testbed", "Applied patch tests/test_w.py cleanly.",
                      "+ : '>>>>> Start Test Output'", "+ pytest -rA tests/test_w.py", *body,
                      f"+ SWEBENCH_TEST_EXIT_CODE={code}", "+ : '>>>>> End Test Output'",
                      f">>>>> Test Exit Code: {code}", "+ git checkout HEAD -- tests/test_w.py"]) + "\n"


class FakeDocker:
    def __init__(self, logs, present=(), fail_pull=()):
        self.logs, self.present, self.fail_pull = logs, set(present), set(fail_pull)
        self.pulled, self.removed, self.scripts = [], [], []

    def has_image(self, image):
        return image in self.present

    def pull(self, image):
        if image in self.fail_pull:
            return False, "manifest unknown"
        self.pulled.append(image)
        return True, ""

    def run(self, image, script, *, timeout, network):
        self.scripts.append(script)
        return self.logs[image], False

    def remove_image(self, image):
        self.removed.append(image)


def test_swebench_selection_is_seeded_and_spread_across_repos():
    pytest.importorskip("swebench")
    sc = load_swebench_cases()
    tasks = [fake_task(f"{r}__x-{i}", repo=r) for r in ("a/a", "b/b", "c/c") for i in range(5)]
    tasks.append(fake_task("a/a__x-99", repo="a/a", f2p=[f"t{i}" for i in range(30)]))  # too many failing tests
    one = sc.select(tasks, ids=None, sample=6, seed=3, max_fail=10)
    assert [t["repo"] for t in one] == ["a/a", "b/b", "c/c"] * 2
    assert [t["instance_id"] for t in one] == [t["instance_id"] for t in sc.select(tasks, ids=None, sample=6, seed=3, max_fail=10)]
    assert "a/a__x-99" not in {t["instance_id"] for t in sc.select(tasks, ids=None, sample=99, seed=0, max_fail=10)}
    assert [t["instance_id"] for t in sc.select(tasks, ids=["c/c__x-1", "a/a__x-2"], sample=0, seed=0, max_fail=10)] \
        == ["c/c__x-1", "a/a__x-2"]
    # a fresh sample leaves out the tasks that already have cases
    taken = {t["instance_id"] for t in one}
    fresh = sc.select(tasks, ids=None, sample=99, seed=3, max_fail=10, exclude=taken)
    assert len(fresh) == 15 - 6 and not taken & {t["instance_id"] for t in fresh}


def test_swebench_output_slicing_and_test_id_forms():
    sc = load_swebench_cases()
    task = fake_task("acme__widgets-1")
    log = fake_log(["tests/test_w.py::test_new"])
    out = sc.test_output(log)
    assert out.startswith("=====") and "+ pytest" not in out and "SWEBENCH_TEST_EXIT_CODE" not in out
    assert "Applied patch" not in out and ">>>>>" not in out
    assert sc.test_command(task["eval_script"]) == "pytest -rA tests/test_w.py"
    assert sc.locate("test_add (tests.math.Calc)", "test_add (tests.math.Calc.test_add) ... FAIL") \
        == "test_add (tests.math.Calc.test_add)"  # newer Django spelling
    assert sc.locate("test_add (tests.math.Calc)", "test_add (tests.math.Calc) ... FAIL") == "test_add (tests.math.Calc)"
    assert sc.locate("missing", "nothing here") is None
    assert sc.image_of({"instance_id": "django__django-11099"}) == \
        "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


def test_swebench_harness_writes_cases_and_skips_bad_runs(tmp_path):
    pytest.importorskip("swebench")
    sc = load_swebench_cases()
    good = fake_task("acme__widgets-1")
    passed = fake_task("acme__widgets-2", f2p=("tests/test_w.py::test_fixed",))
    noimg = fake_task("acme__widgets-3")
    short = fake_task("acme__widgets-4")
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text("".join(json.dumps(t) + "\n" for t in (good, passed, noimg, short)))
    docker = FakeDocker({
        good["image"]: fake_log(["tests/test_w.py::test_new"]),
        passed["image"]: fake_log([], passing=["tests/test_w.py::test_fixed"]),
        short["image"]: fake_log(["tests/test_w.py::test_new"], filler=2),
    }, present=[good["image"]], fail_pull=[noimg["image"]])
    out = tmp_path / "out"
    argv = ["run", "--dataset", str(dataset), "--instances", "acme__widgets-1,acme__widgets-2,acme__widgets-3,acme__widgets-4",
            "--out", str(out)]
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sc.main(argv, docker=docker)
    text = buf.getvalue()
    assert rc == 0, text
    assert "acme__widgets-1" in text and "written" in text
    assert "FAIL_TO_PASS tests did not fail" in text and "image pull failed" in text and "under 40 lines" in text
    assert (out / "acme__widgets-1.keep").read_text() == "tests/test_w.py::test_new\n"
    assert (out / "acme__widgets-1.cmd").read_text() == "pytest -rA tests/test_w.py\n"
    log = (out / "acme__widgets-1.log").read_text()
    assert log.startswith("=====") and "FAILED tests/test_w.py::test_new" in log
    meta = json.loads((out / "acme__widgets-1.json").read_text())
    assert meta["failed"] == ["tests/test_w.py::test_new"] and meta["unlocated"] == []
    row = (out / "MANIFEST.tsv").read_text().splitlines()[1].split("\t")
    assert row[0] == "acme__widgets-1" and row[-2:] == ["dataset", "dataset FAIL_TO_PASS"]
    assert not (out / "acme__widgets-2.log").exists() and (out / "_failed" / "acme__widgets-2.eval.log").exists()
    # the script run is SWE-bench's wrapping, which records the test exit code for the grader
    assert all("SWEBENCH_TEST_EXIT_CODE=$?" in s for s in docker.scripts)
    # images it pulled are removed; the one that was already there is kept
    assert set(docker.removed) == {passed["image"], short["image"]} and good["image"] not in docker.removed
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sc.main(argv, docker=docker)
    assert "acme__widgets-1" in buf.getvalue() and "already collected" in buf.getvalue()  # idempotent


# --- bench/defects4j_cases.py ------------------------------------------------------

def load_defects4j_cases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("defects4j_cases", os.path.join(ROOT, "bench", "defects4j_cases.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    spec.loader.exec_module(mod)
    return mod


def git_in(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_d4j_fixture(tmp_path):
    """A tiny upstream project and the Defects4J metadata for one bug in it:
    the fixed commit returns 2, the revert patch makes it return 1 (the bug),
    and a compile-error fix for bugs 1-2 edits another file."""
    up = tmp_path / "upstream"
    (up / "src" / "main" / "java" / "p").mkdir(parents=True)
    (up / "pom.xml").write_text("<project/>\n")
    calc = up / "src" / "main" / "java" / "p" / "Calc.java"
    calc.write_text("package p; class Calc { int two() { return 2; } }\n")
    (up / "Old.java").write_text("class Old { int x; }\n")
    git_in(tmp_path, "init", "-q", str(up))
    git_in(up, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git_in(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixed")
    fixed = git_in(up, "rev-parse", "HEAD")
    calc.write_text("package p; class Calc { int two() { return 1; } }\n")
    revert = git_in(up, "diff")
    git_in(up, "checkout", "-q", "--", ".")
    (up / "Old.java").write_text("class Old { int y; }\n")
    compile_fix = git_in(up, "diff")
    git_in(up, "checkout", "-q", "--", ".")
    meta = tmp_path / "d4j" / "framework" / "projects" / "Lang"
    for sub in ("patches", "trigger_tests", "compile-errors"):
        (meta / sub).mkdir(parents=True)
    (meta / "active-bugs.csv").write_text(
        "bug.id,revision.id.buggy,revision.id.fixed,report.id,report.url\n"
        f"1,0000000,{fixed},LANG-1,https://example.org/LANG-1\n"
        f"2,0000000,{'f' * 40},LANG-2,https://example.org/LANG-2\n")
    (meta / "patches" / "1.src.patch").write_text(revert + "\n")
    (meta / "patches" / "2.src.patch").write_text(revert + "\n")
    (meta / "compile-errors" / "test-1-2.diff").write_text(compile_fix + "\n")
    (meta / "compile-errors" / "test-5-9.diff").write_text("not applied to bug 1\n")
    trig = ("--- p.CalcTest::testTwo\njava.lang.AssertionError: expected:<2> but was:<1>\n"
            "\tat p.CalcTest.testTwo(CalcTest.java:7)\n")
    (meta / "trigger_tests" / "1").write_text(trig)
    (meta / "trigger_tests" / "2").write_text(trig)
    return up, tmp_path / "d4j"


class FakeMavenDocker:
    image = "fake/maven:8"

    def __init__(self, fail=True):
        self.fail, self.seen = fail, []

    def ensure_image(self):
        return True, ""

    def run(self, work, command, *, timeout):
        self.seen.append({"calc": open(os.path.join(work, "src/main/java/p/Calc.java")).read(),
                          "old": open(os.path.join(work, "Old.java")).read(), "command": command})
        lines = ["[INFO] Building widgets 1.0"] + [f"[INFO] Compiling {i} source files" for i in range(45)]
        lines += ["Running p.CalcTest"]
        if self.fail:
            lines += ["Tests run: 3, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.01 sec <<< FAILURE!",
                      "testTwo(p.CalcTest)  Time elapsed: 0.001 sec  <<< FAILURE!",
                      "java.lang.AssertionError: expected:<2> but was:<1>", "\tat p.CalcTest.testTwo(CalcTest.java:7)",
                      "Tests run: 3, Failures: 1, Errors: 0, Skipped: 0", "[INFO] BUILD FAILURE"]
        else:
            lines += ["Tests run: 3, Failures: 0, Errors: 0, Skipped: 0", "[INFO] BUILD SUCCESS"]
        code = 1 if self.fail else 0
        return "downloading noise\n>>>>> pare start\n" + "\n".join(lines) + f"\n>>>>> pare exit {code}\n", False


def test_defects4j_metadata_parsing_and_selection(tmp_path):
    d4 = load_defects4j_cases()
    _, meta = make_d4j_fixture(tmp_path)
    t = d4.trigger_tests(str(meta), "Lang", 1)
    assert t == [{"class": "p.CalcTest", "method": "testTwo", "exception": "java.lang.AssertionError: expected:<2> but was:<1>"}]
    assert [os.path.basename(f) for f in d4.compile_fixes(str(meta), "Lang", 1)] == ["test-1-2.diff"]
    assert d4.compile_fixes(str(meta), "Lang", 3) == []
    assert d4.test_id_forms("p.CalcTest", "testTwo") == ["testTwo(p.CalcTest)", "p.CalcTest.testTwo", "CalcTest.testTwo", "p.CalcTest::testTwo"]
    assert d4.parse_bug_ids("Lang-1, Codec-12") == [("Lang", 1), ("Codec", 12)]
    picked = d4.select(str(meta), ids=None, sample=5, seed=0, max_fail=5)
    assert {b["bug"] for b in picked} == {1, 2} and all(b["project"] == "Lang" for b in picked)
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    (case_dir / "Lang-1.log").write_text("x\n")
    assert [b["bug"] for b in d4.select(str(meta), ids=None, sample=5, seed=0, max_fail=5,
                                        exclude=d4.existing_cases([str(case_dir)]))] == [2]
    assert d4.mvn_command(picked[0])[-2:] == ["-Dtest=CalcTest", "test"]
    out, code = d4.split_output("noise\n>>>>> pare start\nline\n>>>>> pare exit 1\n")
    assert out == "line\n" and code == 1
    # the exception line as Defects4J recorded it, or its message under JUnit 4's class
    out = "java.lang.AssertionError: expected:<1> but was:<0>\n"
    assert d4.locate_exception("java.lang.AssertionError: expected:<1> but was:<0>", out).startswith("java.lang")
    assert d4.locate_exception("junit.framework.AssertionFailedError: expected:<1> but was:<0>", out) == "expected:<1> but was:<0>"
    assert d4.locate_exception("junit.framework.AssertionFailedError: short", "short") is None  # too short to mean anything
    assert d4.locate_exception("java.lang.AssertionError: Out of bounds (0.52 > 0.5)", "Out of bounds (0.68 > 0.5)") is None


def test_defects4j_harness_rebuilds_the_buggy_version_and_writes_a_case(tmp_path):
    import contextlib
    import io
    d4 = load_defects4j_cases()
    up, meta = make_d4j_fixture(tmp_path)
    out = tmp_path / "out"
    docker = FakeMavenDocker()
    argv = ["run", "--defects4j", str(meta), "--bugs", "Lang-1,Lang-2", "--upstream", f"Lang={up}", "--out", str(out)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = d4.main(argv, docker=docker)
    text = buf.getvalue()
    assert rc == 0, text
    assert "Lang-1" in text and "written" in text and "fixed revision not found upstream" in text
    # the build saw the buggy version (fix reverted) with the compile-error fix applied
    assert docker.seen[0]["calc"].strip().endswith("return 1; } }") and "int y;" in docker.seen[0]["old"]
    assert (out / "Lang-1.keep").read_text().splitlines() == [
        "testTwo(p.CalcTest)", "java.lang.AssertionError: expected:<2> but was:<1>"]
    assert (out / "Lang-1.cmd").read_text().strip() == "mvn -B -Dtest=CalcTest test"
    log = (out / "Lang-1.log").read_text()
    assert log.startswith("[INFO] Building") and "downloading noise" not in log and ">>>>>" not in log
    meta_json = json.loads((out / "Lang-1.json").read_text())
    assert meta_json["fixes_applied"] == ["test-1-2.diff"] and meta_json["exit"] == 1
    row = (out / "MANIFEST.tsv").read_text().splitlines()[1].split("\t")
    assert row[0] == "Lang-1" and row[-2:] == ["dataset", "dataset trigger tests"]

    passing = FakeMavenDocker(fail=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d4.main(argv + ["--force", "--out", str(tmp_path / "out2")], docker=passing)
    assert "trigger tests did not fail" in buf.getvalue()
    assert (tmp_path / "out2" / "_failed" / "Lang-1.run.log").exists()


def test_swebench_absent_tests_are_reported_as_not_run(tmp_path):
    pytest.importorskip("swebench")
    import contextlib
    import io
    sc = load_swebench_cases()
    task = fake_task("acme__widgets-9", f2p=("tests/test_w.py::test_never_collected",))
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps(task) + "\n")
    docker = FakeDocker({task["image"]: fake_log(["tests/test_w.py::test_something_else"])}, present=[task["image"]])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sc.main(["run", "--dataset", str(dataset), "--instances", "acme__widgets-9", "--out", str(tmp_path / "o")], docker=docker)
    assert "not in the test output" in buf.getvalue() and "test_never_collected" in buf.getvalue()


def test_bench_tidyout_handles_a_command_given_as_a_path(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    import run as bench_run
    if not bench_run.which("tidyout"):
        pytest.skip("tidyout not installed")
    log = tmp_path / "x.log"
    log.write_text("hello from the log\n")
    out = bench_run.Tidyout().run(str(log), ["./tests/runtests.py", "--verbosity", "2"], 2000, str(tmp_path))
    assert b"hello from the log" in out


@pytest.mark.parametrize("cmd", [[], ["cat", "x"], ["cd", "tests", "&&", "pytest"], ["./tests/runtests.py", "-v"], ["pytest", "-rA"]])
def test_bench_fake_command_replays_the_log_and_terminates(tmp_path, cmd):
    # A fake named "cat" that called cat found itself on PATH and exec'd forever.
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    import run as bench_run
    log = tmp_path / "x.log"
    log.write_text("hello from the log\n")
    name, args, env = bench_run.fake_command(str(log), cmd, str(tmp_path))
    p = subprocess.run([name, *args], env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
    assert p.stdout == b"hello from the log\n"
