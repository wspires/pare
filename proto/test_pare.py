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
