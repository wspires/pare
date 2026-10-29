"""Tests of the agent-success harness that need no model: task loading,
grading, the conditions on a saved log and on a live command, and the
fix tasks' projects (broken as shipped, fixed by the intended change).
`python -m pytest -q bench/`."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_eval as ae  # noqa: E402

PARE_BIN = os.path.join(ae.ROOT, "target", "release", "pare")
needs_pare = pytest.mark.skipif(not os.path.exists(PARE_BIN), reason="target/release/pare is not built")
needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")


def opts(**kw) -> argparse.Namespace:
    base = dict(pare_bin=PARE_BIN, budget="auto", truncate_chars=30000, command_timeout=120)
    base.update(kw)
    return argparse.Namespace(**base)


# --- tasks and grading


def test_every_shipped_task_loads():
    tasks = {t.name: t for t in ae.load_tasks()}
    assert {"pytest-ledger", "cargo-inventory", "outage-cause", "maven-test-cause"} <= set(tasks)
    for t in tasks.values():
        assert t.prompt and t.command
        if t.kind == "fix":
            assert os.path.isdir(t.workspace) and t.check
        else:
            assert os.path.exists(t.log), t.log
            assert t.expect


@pytest.mark.parametrize("test_id,terms", [
    ("org.apache.commons.cli.BasicParserTest::testOptionGroupLong", ["testOptionGroupLong", "BasicParserTest"]),
    ("test_custom_perms (auth_tests.test_auth_backends.ModelBackendTest)", ["test_custom_perms", "ModelBackendTest"]),
    ("tests/test_cart.py::TestCart::test_total", ["test_total", "TestCart"]),
    ("tests/test_cart.py::test_parse_bad[x-1]", ["test_parse_bad", "test_cart.py"]),
    ("test_alone", ["test_alone"]),
])
def test_terms_for_test(test_id, terms):
    assert ae.terms_for_test(test_id) == terms


def test_grade_counts_whole_terms_and_alternatives():
    expect = [["PricingTest", "formatsThousandsSeparator"], ["CartTest", "NullPointerException || NPE"], ["shop-core"]]
    answer = "PricingTest.formatsThousandsSeparator failed; CartTest.findMissing threw an NPE in module shop-core"
    assert ae.grade_answer(expect, answer) == (1.0, [])
    score, missed = ae.grade_answer(expect, "PricingTest formatsThousandsSeparator; CartTest ok; shop-coreextra")
    assert score == pytest.approx(1 / 3)
    assert missed == [["CartTest", "NullPointerException || NPE"], ["shop-core"]]
    # a term is not matched run on into a longer name
    assert ae.grade_answer([["testResolve1"]], "testResolve12 failed")[0] == 0.0


def test_heldout_tasks_come_from_labels():
    d = os.path.join(ae.ROOT, "corpus", "heldout", "defects4j")
    if not os.path.isdir(d):
        pytest.skip("no defects4j held-out directory")
    tasks = ae.heldout_tasks(d, sample=3, seed=7)
    assert len(tasks) == 3
    for t in tasks:
        assert t.kind == "diagnose" and os.path.isabs(t.log) and os.path.exists(t.log)
        assert all(len(terms) == 2 for terms in t.expect)
    assert [t.name for t in ae.heldout_tasks(d, sample=3, seed=7)] == [t.name for t in tasks]  # seeded


# --- conditions


def test_truncate_keeps_head_and_tail():
    text = "".join(f"line {i}\n" for i in range(5000)).encode()
    out = ae.Truncate(opts(truncate_chars=1000)).filter_bytes(text, "cmd", "")
    assert out.startswith("line 0\n") and out.endswith("line 4999\n")
    assert "characters truncated" in out and len(out) < 1200
    assert ae.Truncate(opts()).filter_bytes(b"short\n", "cmd", "") == "short\n"


def test_raw_runs_live_command_with_merged_output():
    out, code = ae.Raw(opts()).wrap_live("echo out; echo err >&2; exit 3", tempfile.gettempdir(), dict(os.environ), "")
    assert out == "out\nerr\n" and code == 3


@needs_pare
def test_pare_condition_summarises_and_expands(tmp_path):
    task = [t for t in ae.load_tasks() if t.name == "outage-cause"][0]
    cond = ae.Pare(opts(budget=600))
    work = str(tmp_path)
    summary = cond.wrap_log(task, work)
    assert summary.startswith("# pare:") and "## raw:" in summary
    assert len(summary) < 3000
    expanded = cond.expand("--since L1 --until L3", work)
    assert "starting inventory-api" in expanded
    assert "allowed here" in cond.expand("-- rm -rf /", work)


@needs_pare
def test_pare_condition_wraps_live_command_and_passes_pare_through(tmp_path):
    cond = ae.Pare(opts(budget=600))
    work = str(tmp_path)
    out, code = cond.wrap_live("seq 1 5000; echo 'ERROR boom' >&2; exit 7", str(tmp_path), dict(os.environ), work, always=True)
    assert out.startswith("# pare:") and code == 7 and "ERROR boom" in out and cond.last_wrapped
    # the agent's own commands are wrapped as the hook decides: a pipeline or cat is not, pytest is
    piped, pcode = cond.wrap_live("seq 1 5000 | tail -2", str(tmp_path), dict(os.environ), work)
    assert piped == "4999\n5000\n" and pcode == 0 and not cond.last_wrapped
    assert cond.plan("cat src/main.rs")[0] is None and cond.plan("cargo test")[0] is not None
    assert cond.plan("python3 -m pytest -q")[1] == "wrapped" and cond.plan("pare --only T3 < run.log")[0] is None
    raw_line = [l for l in out.splitlines() if l.startswith("## raw:")][0]
    runfile = raw_line.split("raw:", 1)[1].split(" · ")[0].strip()
    out2, code2 = cond.wrap_live(f"pare --since L1 --until L2 < {runfile}", str(tmp_path), dict(os.environ), work)
    assert out2.splitlines()[1:3] == ["1: 1", "2: 2"] and code2 == 0  # pare's own listing, not a second summary


# --- the shipped fix tasks: broken as given, green after the intended fix


def ledger_copy(tmp_path):
    task = [t for t in ae.load_tasks() if t.name == "pytest-ledger"][0]
    dst = tmp_path / "proj"
    shutil.copytree(task.workspace, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return task, dst


def test_pytest_ledger_fails_as_shipped_and_passes_when_fixed(tmp_path):
    pytest.importorskip("pytest")
    task, dst = ledger_copy(tmp_path)
    p = subprocess.run(task.check, shell=True, cwd=dst, capture_output=True, text=True)
    assert p.returncode != 0 and "5 failed" in p.stdout
    fixes = {
        "ledger/money.py": ('.rjust(2, "0")', '.ljust(2, "0")'),
        "ledger/accounts.py": (") >= 0", ") == 0"),
        "ledger/report.py": ("width = min(", "width = max("),
        "ledger/csvimport.py": ('row["memo"]', 'row.get("memo", "")'),
    }
    for rel, (a, b) in fixes.items():
        path = dst / rel
        s = path.read_text()
        assert a in s, rel
        path.write_text(s.replace(a, b))
    p = subprocess.run(task.check, shell=True, cwd=dst, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-800:]


@needs_cargo
def test_cargo_inventory_fails_as_shipped_and_passes_when_fixed(tmp_path):
    task = [t for t in ae.load_tasks() if t.name == "cargo-inventory"][0]
    dst = tmp_path / "proj"
    shutil.copytree(task.workspace, dst, ignore=shutil.ignore_patterns("target"))
    p = subprocess.run(task.check, shell=True, cwd=dst, capture_output=True, text=True)
    assert p.returncode != 0 and "error[E0308]" in p.stderr and "error[E0599]" in p.stderr
    fixes = {
        "src/moves.rs": [("let free: u64 =", "let free ="), (".remove(m.sku, m.qty)", ".take(m.sku, m.qty)")],
        "src/sku.rs": [("((self.weight_grams as u64 * qty as u64 + 500) / 1000) as u32",
                        "(self.weight_grams as u64 * qty as u64 / 1000) as u32")],
    }
    for rel, subs in fixes.items():
        path = dst / rel
        s = path.read_text()
        for a, b in subs:
            assert a in s, (rel, a)
            s = s.replace(a, b)
        path.write_text(s)
    p = subprocess.run(task.check, shell=True, cwd=dst, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]


# --- reporting


def test_summary_table():
    records = [
        {"task": "a", "kind": "fix", "condition": "raw", "trial": 1, "success": True, "score": 1.0,
         "usage": {"total_input": 1000, "output": 100, "turns": 3, "context_max": 800}, "tool_result_bytes": 5000,
         "prompt_tokens": 200, "net_input": 400},
        {"task": "a", "kind": "fix", "condition": "pare", "trial": 1, "success": False, "score": 0.0,
         "usage": {"total_input": 300, "output": 90, "turns": 4, "context_max": 200}, "tool_result_bytes": 900},
        {"task": "a", "kind": "fix", "condition": "pare", "trial": 2, "success": False, "score": 0.0, "error": "boom"},
    ]
    text = ae.summarize(records)
    assert "| raw | 1 | 100 % | 1.00 | 1,000 | 200 | 400 | 100 | 800 | 3.0 | 5,000 | 0 |" in text
    assert "| pare | 2 | 0 % | 0.00 | 300 | - | - | 90 | 200 | 4.0 | 900 | 1 |" in text  # no count recorded
    assert "| a | 1/1 · 1,000 tok (net 400) | 0/2 · 300 tok |" in text


# --- the loop, end to end, against a scripted stand-in for the model


class _Usage:
    def __init__(self, i, o, cr=0, cw=0):
        self.input_tokens, self.output_tokens = i, o
        self.cache_read_input_tokens, self.cache_creation_input_tokens = cr, cw


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self):
        return dict(self.__dict__)


class _Response:
    def __init__(self, content, stop_reason, usage):
        self.content, self.stop_reason, self.usage = content, stop_reason, usage


class _Stream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.response


class FakeClient:
    """Plays a script: each entry is a list of commands to run (a tool_use
    turn) or a string (the final answer)."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

        class _Messages:
            def stream(_self, **kwargs):
                self.requests.append(kwargs)
                step = self.script.pop(0)
                if isinstance(step, str):
                    return _Stream(_Response([_Block(type="text", text=step)], "end_turn", _Usage(1000, 50, cr=200)))
                blocks = [_Block(type="tool_use", id=f"t{i}", name=name, input=inp) for i, (name, inp) in enumerate(step)]
                return _Stream(_Response(blocks, "tool_use", _Usage(3000, 80, cw=500)))

        class _Beta:
            messages = _Messages()

        class _Counting:
            def count_tokens(_self, **kwargs):
                # A stand-in count: the fixed prompt is 700 tokens, plus 100 per tool.
                return _Usage(700 + 100 * len(kwargs.get("tools") or []), 0)

        self.beta = _Beta()
        self.messages = _Counting()


def test_fix_trial_end_to_end_with_scripted_agent(tmp_path):
    task = [t for t in ae.load_tasks() if t.name == "pytest-ledger"][0]
    edits = [
        ("bash", {"command": "sed -i 's/.rjust(2, \"0\")/.ljust(2, \"0\")/' ledger/money.py"}),
        ("bash", {"command": "sed -i 's/) >= 0/) == 0/' ledger/accounts.py"}),
        ("bash", {"command": "sed -i 's/width = min(/width = max(/' ledger/report.py"}),
        ("bash", {"command": "sed -i 's/row\\[\"memo\"\\]/row.get(\"memo\", \"\")/' ledger/csvimport.py"}),
    ]
    client = FakeClient([edits, [("bash", {"command": "python -m pytest -q -p no:cacheprovider"})], "Fixed four bugs."])
    o = opts(model="fake", effort="medium", max_turns=10, max_tokens=1000, fallbacks=False)
    r = ae.run_trial(client, o, task, ae.Raw(o), 1, str(tmp_path))
    assert "error" not in r, r
    assert r["success"] and r["ended"] == "done" and r["answer"] == "Fixed four bugs."
    assert r["usage"]["turns"] == 3 and r["usage"]["total_input"] == 3500 + 3500 + 1200
    assert r["usage"]["context_max"] == 3500 and r["usage"]["output"] == 210
    assert r["prompt_tokens"] == 800 and r["net_input"] == 8200 - 3 * 800  # one tool, three turns
    assert r["initial_result_bytes"] > 100_000  # the raw pytest run
    assert r["tool_result_bytes"] > r["initial_result_bytes"]
    assert os.path.exists(os.path.join(ae.ROOT, r["transcript"]))
    # the first request carried the task prompt and the failing run; the tool results went back as tool_result blocks
    first = client.requests[0]
    assert first["model"] == "fake" and first["tools"] == [ae.BASH_TOOL]
    assert "5 failed" in first["messages"][0]["content"]
    results = client.requests[1]["messages"][2]["content"]  # user, assistant, then the tool results (the list is shared across requests)
    assert [b["type"] for b in results] == ["tool_result"] * 4 and all(b["tool_use_id"].startswith("t") for b in results)
    assert "betas" not in first


def test_fix_trial_unfixed_is_a_failure_not_an_error(tmp_path):
    task = [t for t in ae.load_tasks() if t.name == "pytest-ledger"][0]
    o = opts(model="fake", effort="low", max_turns=10, max_tokens=1000, fallbacks=False)
    r = ae.run_trial(FakeClient(["I give up."]), o, task, ae.Truncate(o), 1, str(tmp_path))
    assert "error" not in r and r["success"] is False and r["score"] == 0.0
    assert r["initial_result_bytes"] <= 30100


@needs_pare
def test_diagnose_trial_with_pare_expansion(tmp_path):
    task = [t for t in ae.load_tasks() if t.name == "maven-test-cause"][0]
    o = opts(model="fake", effort="low", max_turns=10, max_tokens=1000, fallbacks=True, budget=600)
    answer = ("PricingTest.formatsThousandsSeparator: expected: <$1,250.00> but was: <$1250.00>; "
              "CartTest.findMissingSkuReturnsNullNotThrows: NullPointerException; module shop-core")
    client = FakeClient([[("pare", {"args": "--only T3"})], answer])
    r = ae.run_trial(client, o, task, ae.Pare(o), 1, str(tmp_path))
    assert "error" not in r, r
    assert r["success"] and r["score"] == 1.0 and r["missed"] == []
    assert client.requests[0]["tools"] == [ae.PARE_TOOL] and client.requests[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert r["prompt_tokens"] == 800 and r["net_input"] == r["usage"]["total_input"] - 800 * r["usage"]["turns"]
    assert "summarised by `pare`" in client.requests[0]["messages"][0]["content"]
    r2 = ae.run_trial(FakeClient(["PricingTest failed."]), o, task, ae.Pare(o), 2, str(tmp_path))
    assert not r2["success"] and r2["score"] == pytest.approx(0.0) and len(r2["missed"]) == 3


# --- preflight: requirements, an old pare, missing credentials


def test_requires_checks_programs_and_modules():
    t = ae.Task("x", {"kind": "diagnose", "log": "nope.log", "command": "c", "prompt": "p", "expect": ["a"],
                      "requires": ["python3:pytest", "python3:no_such_module_zz", "no-such-program-zz"]}, "/")
    assert t.missing_tools() == ["no_such_module_zz for python3", "no-such-program-zz"]


def test_pare_condition_refuses_a_binary_that_rejects_the_budget(tmp_path):
    fake = tmp_path / "pare"
    fake.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = auto ] && { echo \"error: invalid value 'auto' for '--budget <BUDGET>'\" >&2; exit 2; }; done\n"
                    "[ \"$1\" = --version ] && echo 'pare 0.1.0'\ncat\n")
    fake.chmod(0o755)
    old = ae.Pare(opts(pare_bin=str(fake), budget="auto"))
    assert not old.available()
    assert "rejects --budget auto" in old.unavailable_reason() and "cargo build --release" in old.unavailable_reason()
    assert ae.Pare(opts(pare_bin=str(fake), budget=2000)).available()
    assert "does not exist" in ae.Pare(opts(pare_bin=str(tmp_path / "missing"))).unavailable_reason()


def test_missing_credentials_fail_once_before_any_trial(monkeypatch):
    pytest.importorskip("anthropic")
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", tempfile.mkdtemp())  # no `ant auth login` profile on disk
    with pytest.raises(SystemExit) as e:
        ae.make_client("claude-opus-5-5")
    assert "ANTHROPIC_API_KEY" in str(e.value) or "credentials" in str(e.value).lower()
