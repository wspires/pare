#!/usr/bin/env python3
"""Agent-success evaluation: does an agent do its job as well on pare's
summary as on the full output, and at what token cost?

Recall (bench/run.py) measures whether the must-survive lines are present.
This measures whether an agent that gets the summary as its tool result
still fixes the build, or still names the cause, and how many tokens it
spends. Each trial runs one task under one condition:

  task       what the agent has to do; corpus/agent/<name>/task.json
             (README there), or a held-out case with its labelled failing
             tests (--heldout DIR)
  condition  what the agent sees of every command's output:
               raw       the output as printed
               truncate  the head and tail, as an agent harness cuts a
                         long tool result (--truncate-chars, 30,000)
               pare      pare's summary at --budget (default auto) of the
                         task's command and, as the Claude Code hook decides,
                         of the agent's test, build and install commands (cat,
                         grep and pipelines print as usual); the agent may
                         expand a summary with `pare --only T<n> < RUN`
               sqz       sqz compress, when sqz is installed
               tidyout   tidyout <cmd>, when tidyout is installed

Two kinds of task. A `fix` task gives the agent a copy of a project, the
output of its failing command, and a bash tool whose every result goes
through the condition; afterwards the task's check command decides success
(exit 0). A `diagnose` task replays a saved log through the condition and
grades the agent's answer against the terms it must name; the only tool is
the one pare's condition needs for expansion.

Usage
    python3 bench/agent_eval.py --list
    python3 bench/agent_eval.py --dry-run                     # the tool results each condition would give, no model calls
    python3 bench/agent_eval.py --conditions raw,pare --trials 3
    python3 bench/agent_eval.py --heldout corpus/heldout/defects4j --sample 8 --seed 1
    python3 bench/agent_eval.py --report bench/agent-runs/<stamp>/results.jsonl

Needs `pip install anthropic` and credentials (ANTHROPIC_API_KEY, or
`ant auth login`); the pare binary (--pare-bin, default target/release/pare)
for the pare condition. Every trial spends real money: --dry-run first.
Results go to bench/agent-runs/<stamp>/ (results.jsonl, one transcript per
trial, summary.md); the directory is ignored by git.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402  (the keep-line matcher: a term counts whole, not run on into a longer name)
import run as bench_run  # noqa: E402  (sqz and tidyout replay of a saved log)

def load_hook():
    """The Claude Code hook, as a module: its plan() decides which of the
    agent's commands the pare condition wraps, so the condition is the
    integration, not a stricter or looser version of it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pare_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK_DIR = os.path.join(ROOT, "corpus", "agent")
RUNS_DIR = os.path.join(ROOT, "bench", "agent-runs")
HOOK_PATH = os.path.join(ROOT, "integrations", "claude-code", "pare-hook.py")

DEFAULT_MODEL = "claude-opus-5-5"
BYTES_PER_TOKEN = 2.0  # pare's own proxy, for the dry run; real counts come from the API

SYSTEM_FIX = """You are a software engineer working in a checkout of a project, through a bash tool.
Fix what the task describes, run the project's own checks to confirm, then stop and
report in a few sentences. Keep to the task: do not change tests, do not add tools or
dependencies, do not reformat unrelated code. A long command output may be shown to you
cut or summarised; when a summary tells you how to see more of it, you may do that."""

SYSTEM_DIAGNOSE = """You are a software engineer reading the output of a command a colleague ran.
Answer the question from the output alone; you cannot run the command again. Be concrete
and quote the identifiers, messages and locations from the output. If the output is shown
to you summarised and the summary tells you how to see more of it, you may do that."""


def est_tokens(data: bytes | str) -> int:
    n = len(data.encode("utf-8", errors="replace")) if isinstance(data, str) else len(data)
    return math.ceil(n / BYTES_PER_TOKEN - 1e-9)


# ----------------------------------------------------------------------------
# Tasks


class Task:
    def __init__(self, name: str, spec: dict, base: str):
        self.name = name
        self.kind = spec["kind"]
        self.prompt = spec["prompt"]
        self.command = spec["command"]
        self.requires = spec.get("requires", [])
        self.notes = spec.get("notes", "")
        if self.kind == "fix":
            self.workspace = os.path.join(base, spec["workspace"])
            self.check = spec["check"]
            self.log = None
            self.expect = []
        elif self.kind == "diagnose":
            self.workspace = None
            self.check = None
            self.log = os.path.normpath(os.path.join(base, spec["log"]))
            # Each entry is a finding: a list of terms that must all appear
            # (a string is one term); a term is "a || b" forms, any of which counts.
            self.expect = [[e] if isinstance(e, str) else list(e) for e in spec["expect"]]
        else:
            raise ValueError(f"{name}: unknown kind {self.kind!r}")

    def missing_tools(self) -> list[str]:
        """The `requires` entries not satisfied: a program not on PATH, or
        for `prog:module` a Python interpreter that cannot import the module."""
        missing = []
        for req in self.requires:
            prog, _, module = req.partition(":")
            if shutil.which(prog) is None:
                missing.append(prog)
            elif module:
                p = subprocess.run([prog, "-c", f"import {module}"], capture_output=True)
                if p.returncode != 0:
                    missing.append(f"{module} for {prog}")
        return missing

    def __repr__(self):
        return f"Task({self.name}, {self.kind})"


def load_tasks(task_dir: str = TASK_DIR) -> list[Task]:
    tasks = []
    for spec_path in sorted(glob.glob(os.path.join(task_dir, "*", "task.json"))):
        base = os.path.dirname(spec_path)
        with open(spec_path, encoding="utf-8") as f:
            spec = json.load(f)
        tasks.append(Task(os.path.basename(base), spec, base))
    return tasks


def terms_for_test(test_id: str) -> list[str]:
    """The terms an answer must contain to count as naming a failing test
    from a held-out label: the method and its class or file.
      org.apache.commons.cli.BasicParserTest::testOptionGroupLong  -> [testOptionGroupLong, BasicParserTest]
      test_custom_perms (auth_tests.test_auth_backends.ModelBackendTest) -> [test_custom_perms, ModelBackendTest]
      tests/test_cart.py::TestCart::test_total                     -> [test_total, TestCart]
      tests/test_cart.py::test_parse_bad[x]                        -> [test_parse_bad, test_cart.py]
    """
    m = re.fullmatch(r"\s*(\S+) \((\S+)\)\s*", test_id)  # unittest: method (dotted.Class)
    if m:
        return [m.group(1), m.group(2).rsplit(".", 1)[-1]]
    parts = test_id.split("::")
    method = re.sub(r"\[.*\]$", "", parts[-1])
    if len(parts) >= 2:
        container = parts[-2]
        container = os.path.basename(container) if "/" in container or container.endswith(".py") else container.rsplit(".", 1)[-1]
        return [method, container]
    return [method]


def heldout_tasks(directory: str, sample: int | None, seed: int) -> list[Task]:
    """A diagnose task per held-out case: the saved log, its command, and
    the failing tests its .json labels (the dataset's labels, not pare's)."""
    tasks = []
    for json_path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(json_path, encoding="utf-8") as f:
            meta = json.load(f)
        failed = meta.get("failed") or []
        name = os.path.splitext(os.path.basename(json_path))[0]
        log = os.path.abspath(os.path.join(directory, name + ".log"))
        if not failed or not os.path.exists(log):
            continue
        cmd_path = os.path.join(directory, name + ".cmd")
        command = open(cmd_path, encoding="utf-8").read().strip() if os.path.exists(cmd_path) else shlex.join(meta.get("command", []))
        spec = {
            "kind": "diagnose",
            "log": log,
            "command": command,
            "prompt": "This test run failed. List every test that failed, each with its class or file and "
                      "its method name, and the exception or assertion message for each.",
            "expect": [terms_for_test(t) for t in failed],
            "notes": f"held-out case {name}: {len(failed)} failing test(s) labelled by the dataset",
        }
        tasks.append(Task(f"{os.path.basename(directory.rstrip('/'))}/{name}", spec, "/"))
    if sample is not None and sample < len(tasks):
        tasks = random.Random(seed).sample(tasks, sample)
        tasks.sort(key=lambda t: t.name)
    return tasks


def grade_answer(expect: list[list[str]], text: str) -> tuple[float, list[list[str]]]:
    """Share of findings whose every term appears in the text, and the
    findings that did not."""
    if not expect:
        return 1.0, []
    missed = [terms for terms in expect if not all(collect.keep_found(t, text) for t in terms)]
    return 1 - len(missed) / len(expect), missed


# ----------------------------------------------------------------------------
# Conditions: what the agent sees of a command's output


def run_shell(command: str, cwd: str | None, env: dict | None, timeout: float) -> tuple[bytes, int]:
    """Run a shell command, stdout and stderr merged, as an agent's bash tool would."""
    try:
        p = subprocess.run(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        return out + f"\n[command timed out after {timeout:.0f}s]\n".encode(), 124


class Condition:
    name = "condition"

    def __init__(self, opts: argparse.Namespace):
        self.opts = opts

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return ""

    def instructions(self) -> str:
        """Appended to the task prompt: how this condition shows output."""
        return ""

    def tools(self, task: Task) -> list[dict]:
        """Extra tools this condition gives a diagnose task (a fix task has bash)."""
        return []

    def wrap_live(self, command: str, cwd: str, env: dict, work: str) -> tuple[str, int]:
        """Run a command in the workspace; return what the agent sees and the exit code."""
        out, code = run_shell(command, cwd, env, self.opts.command_timeout)
        return self.filter_bytes(out, command, work), code

    def wrap_log(self, task: Task, work: str) -> str:
        """What the agent sees of a saved log."""
        with open(task.log, "rb") as f:
            return self.filter_bytes(f.read(), task.command, work)

    def filter_bytes(self, data: bytes, command: str, work: str) -> str:
        return data.decode("utf-8", errors="replace")


class Raw(Condition):
    name = "raw"


class Truncate(Condition):
    name = "truncate"

    def filter_bytes(self, data, command, work):
        text = data.decode("utf-8", errors="replace")
        limit = self.opts.truncate_chars
        if len(text) <= limit:
            return text
        head = limit // 2
        tail = limit - head
        cut = len(text) - head - tail
        return text[:head] + f"\n\n... [{cut} characters truncated] ...\n\n" + text[-tail:]


class Pare(Condition):
    name = "pare"

    def available(self):
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why the pare condition cannot run: no binary, or one too old for
        the budget it would be given (an older build rejects `auto`)."""
        if not os.path.exists(self.opts.pare_bin):
            return f"{self.opts.pare_bin} does not exist: cargo build --release, or pass --pare-bin"
        p = subprocess.run([self.opts.pare_bin, "--budget", str(self.opts.budget), "--no-tee"],
                           input=b"x\n", capture_output=True)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip().splitlines()
            return (f"{self.opts.pare_bin} (pare {self.version() or '?'}) rejects --budget {self.opts.budget}: "
                    f"{err[0] if err else 'exit ' + str(p.returncode)}. Rebuild it (cargo build --release) "
                    f"or pass a numeric --budget")
        return None

    def version(self):
        p = subprocess.run([self.opts.pare_bin, "--version"], capture_output=True, text=True)
        return p.stdout.strip().replace("pare ", "")

    def instructions(self):
        return ("The output of test, build and install commands is summarised by `pare` to fit a token "
                "budget; other commands print as usual. The summary's "
                "last lines name the run file holding the full output; `pare --only T<n> < RUNFILE` "
                "prints every line of one template, and `pare --since L<a> --until L<b> < RUNFILE` "
                "a line range.")

    def tools(self, task):
        return [PARE_TOOL]

    def _env(self, work: str, env: dict | None = None) -> dict:
        env = dict(env or os.environ)
        env["PARE_RUN_DIR"] = os.path.join(work, "pare-runs")
        env["PARE_BUDGET"] = str(self.opts.budget)
        env["PATH"] = os.path.dirname(os.path.abspath(self.opts.pare_bin)) + os.pathsep + env.get("PATH", "")
        return env

    def plan(self, command: str) -> tuple[str | None, str]:
        """What the Claude Code hook would do with the command: a rewrite to
        `pare -- ...`, or None and why not (cat, grep, sed, a pipeline, cd ...)."""
        hook = getattr(self, "_hook", None) or load_hook()
        self._hook = hook
        saved = {k: os.environ.get(k) for k in ("PARE_BIN", "PARE_HOOK_BUDGET", "PARE_HOOK_DISABLE", "PARE_HOOK_EXTRA")}
        os.environ["PARE_BIN"] = os.path.abspath(self.opts.pare_bin)
        os.environ["PARE_HOOK_BUDGET"] = str(self.opts.budget)
        os.environ.pop("PARE_HOOK_DISABLE", None)
        os.environ.pop("PARE_HOOK_EXTRA", None)
        try:
            return hook.plan(command)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def wrap_live(self, command, cwd, env, work, always=False):
        """Run the agent's command as the hook would: wrapped in pare when it
        is a test, build or install command, untouched otherwise (`cat`,
        `grep`, `sed`, pipelines, and pare itself expanding a summary). The
        task's own command is always wrapped (`always`): it is the noisy one
        by definition."""
        env = self._env(work, env)
        rewritten, reason = self.plan(command)
        if always and rewritten is None:
            rewritten = f"{shlex.quote(self.opts.pare_bin)} --budget {self.opts.budget} -- sh -c {shlex.quote(command)}"
            reason = "the task's command"
        self.last_wrapped = rewritten is not None
        if rewritten is None:
            out, code = run_shell(command, cwd, env, self.opts.command_timeout)
        else:
            out, code = run_shell(rewritten, cwd, env, self.opts.command_timeout + 30)
        return out.decode("utf-8", errors="replace"), code

    def wrap_log(self, task, work):
        env = self._env(work)
        with open(task.log, "rb") as f:
            p = subprocess.run([self.opts.pare_bin, "--budget", str(self.opts.budget)], stdin=f,
                               capture_output=True, env=env, timeout=120)
        if p.returncode != 0:
            raise RuntimeError("pare failed: " + p.stderr.decode(errors="replace")[-300:])
        return p.stdout.decode("utf-8", errors="replace")

    def expand(self, args: str, work: str) -> str:
        """The diagnose task's one tool: pare's own flags over the run file."""
        env = self._env(work)
        runs = sorted(glob.glob(os.path.join(env["PARE_RUN_DIR"], "*.log")))
        if not runs:
            return "no run file"
        argv = shlex.split(args)
        if any(a in ("--", "-f", "--follow", "stats", "prune", "vocab") for a in argv):
            return "only pare's read flags (--only, --except, --since, --until, --min-severity, --budget) are allowed here"
        with open(runs[-1], "rb") as f:
            p = subprocess.run([self.opts.pare_bin, *argv], stdin=f, capture_output=True, env=env, timeout=120)
        return (p.stdout + p.stderr).decode("utf-8", errors="replace")


class Sqz(Condition):
    name = "sqz"

    def available(self):
        return bench_run.which("sqz") is not None

    def version(self):
        return bench_run.TOOLS["sqz"].version()

    def wrap_live(self, command, cwd, env, work):
        out, code = run_shell(command, cwd, env, self.opts.command_timeout)
        return self.filter_bytes(out, command, work), code

    def filter_bytes(self, data, command, work):
        log = os.path.join(work, "sqz-in.log")
        with open(log, "wb") as f:
            f.write(data)
        argv = shlex.split(command) if command.strip() else []
        return bench_run.TOOLS["sqz"].run(log, argv, self.opts.budget, work).decode("utf-8", errors="replace")


class Tidyout(Condition):
    name = "tidyout"

    def available(self):
        return bench_run.which("tidyout") is not None

    def version(self):
        return bench_run.TOOLS["tidyout"].version()

    def wrap_live(self, command, cwd, env, work):
        # tidyout wraps a command and keys its rules on the command's name,
        # so a simple command is handed to it as is; a shell construct is
        # run first and its output replayed under the first word's name.
        argv = shlex.split(command) if command.strip() else []
        simple = argv and not re.search(r"[|&;<>$`()\n]", command)
        if simple:
            try:
                p = subprocess.run([bench_run.which("tidyout"), *argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=self.opts.command_timeout)
            except subprocess.TimeoutExpired:
                return f"[tidyout {command}: timed out]", 124
            return p.stdout.decode("utf-8", errors="replace"), p.returncode
        out, code = run_shell(command, cwd, env, self.opts.command_timeout)
        return self.filter_bytes(out, command, work), code

    def filter_bytes(self, data, command, work):
        log = os.path.join(work, "tidyout-in.log")
        with open(log, "wb") as f:
            f.write(data)
        argv = shlex.split(command) if command.strip() else []
        return bench_run.TOOLS["tidyout"].run(log, argv, self.opts.budget, work).decode("utf-8", errors="replace")


CONDITIONS = {c.name: c for c in (Raw, Truncate, Pare, Sqz, Tidyout)}

BASH_TOOL = {
    "name": "bash",
    "description": "Run a shell command in the project directory and return its output (stdout and stderr merged) and exit code.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The command, as for `sh -c`."}},
        "required": ["command"],
        "additionalProperties": False,
    },
    "strict": True,
}

PARE_TOOL = {
    "name": "pare",
    "description": "Show more of the command output summarised above: runs `pare <args> < RUNFILE` over the saved full output. "
                   "Examples of args: `--only T5` (every line of template T5), `--since L200 --until L260` (a line range), "
                   "`--except T1,T2 --budget 4000` (a larger summary without those templates).",
    "input_schema": {
        "type": "object",
        "properties": {"args": {"type": "string", "description": "pare's flags, without the input redirection."}},
        "required": ["args"],
        "additionalProperties": False,
    },
    "strict": True,
}


# ----------------------------------------------------------------------------
# The agent


class Usage:
    def __init__(self):
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.turns = 0
        self.context_max = 0  # the largest prompt sent, in tokens: how far the context grew

    def add(self, usage) -> None:
        self.turns += 1
        i = usage.input_tokens or 0
        cr = getattr(usage, "cache_read_input_tokens", None) or 0
        cw = getattr(usage, "cache_creation_input_tokens", None) or 0
        self.input += i
        self.output += usage.output_tokens or 0
        self.cache_read += cr
        self.cache_write += cw
        self.context_max = max(self.context_max, i + cr + cw)

    @property
    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_write

    def as_dict(self) -> dict:
        return {"input": self.input, "cache_read": self.cache_read, "cache_write": self.cache_write,
                "total_input": self.total_input, "output": self.output, "turns": self.turns,
                "context_max": self.context_max}


def run_agent(client, opts, system: str, user: str, tools: list[dict], execute) -> tuple[str, Usage, list, str]:
    """A manual tool loop. `execute(name, input) -> (text, is_error)` runs a
    tool. Returns the final text, the usage, the transcript, and how the
    run ended (done, max_turns, max_tokens, refusal)."""
    messages = [{"role": "user", "content": user}]
    usage = Usage()
    final = ""
    ended = "done"
    while True:
        if usage.turns >= opts.max_turns:
            ended = "max_turns"
            break
        kwargs = dict(
            model=opts.model,
            max_tokens=opts.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            output_config={"effort": opts.effort},
        )
        if opts.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        with client.beta.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()
        usage.add(response.usage)
        messages.append({"role": "assistant", "content": response.content})
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if text:
            final = text
        if response.stop_reason == "refusal":
            ended = "refusal"
            break
        if response.stop_reason == "max_tokens":
            ended = "max_tokens"
            break
        if response.stop_reason == "pause_turn":
            continue
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            break
        results = []
        for call in calls:
            try:
                out, is_error = execute(call.name, call.input)
            except Exception as e:  # a tool that failed is a result, not a crash
                out, is_error = f"tool error: {e}", True
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return final, usage, messages, ended


def transcript_json(messages: list) -> list:
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = []
        for b in content:
            if isinstance(b, dict):
                blocks.append(b)
            else:
                d = b.to_dict() if hasattr(b, "to_dict") else {"type": getattr(b, "type", "?")}
                if d.get("type") == "thinking":
                    d = {"type": "thinking"}  # not returned in the clear; keep the shape only
                blocks.append(d)
        out.append({"role": m["role"], "content": blocks})
    return out


# ----------------------------------------------------------------------------
# Trials


def make_client(model: str):
    """A client whose credentials have been checked with one free call
    (count_tokens), so a missing key fails here, once, not in every trial."""
    try:
        import anthropic
    except ImportError:
        sys.exit("the anthropic package is missing: pip install anthropic (or .venv/bin/pip install -r bench/requirements.txt)")
    client = anthropic.Anthropic()
    try:
        client.messages.count_tokens(model=model, messages=[{"role": "user", "content": "ping"}])
    except anthropic.AuthenticationError as e:
        sys.exit(f"credentials rejected: {e.message}")
    except anthropic.NotFoundError:
        sys.exit(f"unknown model {model!r}")
    except anthropic.APIConnectionError as e:
        sys.exit(f"cannot reach the API: {e}")
    except TypeError as e:  # the SDK's error when it finds no credentials at all
        sys.exit(f"{e}\nSet ANTHROPIC_API_KEY, or run `ant auth login`; --dry-run needs neither.")
    return client


def initial_result(task: Task, cond: Condition, workspace: str | None, work: str, env: dict) -> tuple[str, int | None]:
    if task.kind == "fix":
        if isinstance(cond, Pare):
            return cond.wrap_live(task.command, workspace, env, work, always=True)
        return cond.wrap_live(task.command, workspace, env, work)
    return cond.wrap_log(task, work), None


def prompt_tokens(client, opts, system: str, tools: list[dict], user: str) -> int | None:
    """What the fixed part of a trial's prompt costs, in the model's own
    count: the system text, the tool schemas and the task prompt with no
    command output in it. It is paid on every turn, and it differs by
    condition (the pare condition carries a tool and a paragraph on
    expanding), so the per-task tables show tokens net of it. One free
    count_tokens call; None when the client cannot count."""
    try:
        return client.messages.count_tokens(
            model=opts.model, system=[{"type": "text", "text": system}], tools=tools,
            messages=[{"role": "user", "content": user}]).input_tokens
    except Exception:  # an older SDK, or a stand-in for the client
        return None


def user_message(task: Task, cond: Condition, output: str, code: int | None) -> str:
    parts = [task.prompt]
    if cond.instructions():
        parts.append(cond.instructions())
    status = f" (exit code {code})" if code is not None else ""
    parts.append(f"Output of `{task.command}`{status}:\n\n```\n{output}\n```")
    return "\n\n".join(parts)


def run_trial(client, opts, task: Task, cond: Condition, trial: int, out_dir: str) -> dict:
    work = tempfile.mkdtemp(prefix="agent-eval-")
    env = dict(os.environ)
    env.pop("PARE_BUDGET", None)
    started = time.time()
    tool_bytes = 0
    record = {"task": task.name, "kind": task.kind, "condition": cond.name, "trial": trial,
              "model": opts.model, "effort": opts.effort, "budget": str(opts.budget)}
    try:
        workspace = None
        if task.kind == "fix":
            workspace = os.path.join(work, "proj")
            shutil.copytree(task.workspace, workspace, ignore=shutil.ignore_patterns("target", "__pycache__", ".pytest_cache", "node_modules"))
        output, code = initial_result(task, cond, workspace, work, env)
        tool_bytes += len(output.encode("utf-8"))
        record["initial_result_bytes"] = len(output.encode("utf-8"))

        commands = {"run": 0, "wrapped": 0}

        def execute(name, inp):
            nonlocal tool_bytes
            if name == "bash" and task.kind == "fix":
                text, rc = cond.wrap_live(inp["command"], workspace, env, work)
                commands["run"] += 1
                commands["wrapped"] += int(getattr(cond, "last_wrapped", False))
                text = text or "(no output)"
                text += f"\n[exit code {rc}]"
                tool_bytes += len(text.encode("utf-8"))
                return text, rc != 0
            if name == "pare" and isinstance(cond, Pare):
                text = cond.expand(inp["args"], work)
                tool_bytes += len(text.encode("utf-8"))
                return text, False
            return f"unknown tool {name}", True

        tools = [BASH_TOOL] if task.kind == "fix" else cond.tools(task)
        system = SYSTEM_FIX if task.kind == "fix" else SYSTEM_DIAGNOSE
        fixed = prompt_tokens(client, opts, system, tools, user_message(task, cond, "", code))
        final, usage, messages, ended = run_agent(client, opts, system, user_message(task, cond, output, code), tools, execute)
        record.update(usage=usage.as_dict(), ended=ended, tool_result_bytes=tool_bytes, answer=final,
                      commands=commands["run"], commands_wrapped=commands["wrapped"], prompt_tokens=fixed)
        if fixed is not None:
            # The input the trial spent on command output and conversation: the
            # fixed prompt is resent on every turn, so it comes off once per turn.
            record["net_input"] = usage.total_input - fixed * usage.turns
        if task.kind == "fix":
            check_out, check_code = run_shell(task.check, workspace, env, opts.command_timeout)
            record["success"] = check_code == 0
            record["score"] = 1.0 if check_code == 0 else 0.0
            record["check_tail"] = check_out.decode("utf-8", errors="replace")[-600:]
        else:
            score, missed = grade_answer(task.expect, final)
            record["success"] = not missed
            record["score"] = score
            record["missed"] = missed
        path = os.path.join(out_dir, "transcripts", f"{task.name.replace('/', '_')}--{cond.name}--{trial}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"record": record, "messages": transcript_json(messages)}, f, indent=1)
        record["transcript"] = os.path.relpath(path, ROOT)
    except Exception as e:
        record.update(error=f"{type(e).__name__}: {e}", success=False, score=0.0)
    finally:
        record["seconds"] = round(time.time() - started, 1)
        shutil.rmtree(work, ignore_errors=True)
    return record


# ----------------------------------------------------------------------------
# Reporting


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def summarize(records: list[dict]) -> str:
    """One table per task kind: a row per condition, with success rate,
    mean score, tokens and turns; then a row per task and condition."""
    lines = []
    kinds = sorted({r["kind"] for r in records})
    conds = [c for c in CONDITIONS if any(r["condition"] == c for r in records)]
    for kind in kinds:
        rows = [r for r in records if r["kind"] == kind]
        lines.append(f"### {kind} tasks ({len({r['task'] for r in rows})} tasks, {len(rows)} trials)\n")
        lines.append("| condition | trials | success | score | input tok | prompt tok | net input | output tok | context max | turns | tool bytes | errors |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for c in conds:
            rs = [r for r in rows if r["condition"] == c]
            if not rs:
                continue
            ok = [r for r in rs if "usage" in r]
            errs = len(rs) - len(ok)
            counted = [r for r in ok if r.get("net_input") is not None]
            fixed = f"{mean(r['prompt_tokens'] for r in counted):,.0f}" if counted else "-"
            net = f"{mean(r['net_input'] for r in counted):,.0f}" if counted else "-"
            lines.append(
                f"| {c} | {len(rs)} | {100 * mean(r['success'] for r in rs):.0f} % | {mean(r['score'] for r in rs):.2f} "
                f"| {mean(r['usage']['total_input'] for r in ok):,.0f} | {fixed} | {net} | {mean(r['usage']['output'] for r in ok):,.0f} "
                f"| {mean(r['usage']['context_max'] for r in ok):,.0f} | {mean(r['usage']['turns'] for r in ok):.1f} "
                f"| {mean(r['tool_result_bytes'] for r in ok):,.0f} | {errs} |")
        lines.append("")
        lines.append("| task | " + " | ".join(conds) + " |")
        lines.append("|---|" + "---:|" * len(conds))
        for task in sorted({r["task"] for r in rows}):
            cells = []
            for c in conds:
                rs = [r for r in rows if r["task"] == task and r["condition"] == c]
                if not rs:
                    cells.append("")
                    continue
                ok = [r for r in rs if "usage" in r]
                tok = f"{mean(r['usage']['total_input'] for r in ok):,.0f}" if ok else "err"
                counted = [r for r in ok if r.get("net_input") is not None]
                net = f" (net {mean(r['net_input'] for r in counted):,.0f})" if counted else ""
                cells.append(f"{sum(r['success'] for r in rs)}/{len(rs)} · {tok} tok{net}")
            lines.append(f"| {task} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("Success: a fix task's check command exits 0; a diagnose task's answer names every expected finding. "
                 "Input tokens count every turn's prompt (cache reads and writes included). Prompt tok is the fixed "
                 "part of a trial's prompt (system text, tool schemas, the task prompt without the command output), "
                 "which differs by condition and is resent every turn; net input is input tokens less that, once per "
                 "turn: what the trial spent on command output and conversation. Context max is the largest single "
                 "prompt. Tool bytes is what the agent was shown of command output.")
    return "\n".join(lines)


def load_records(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("Usage", 1)[1])
    ap.add_argument("--tasks", default="", help="comma-separated task names or substrings (default: every task)")
    ap.add_argument("--heldout", action="append", default=[], metavar="DIR",
                    help="add a diagnose task per held-out case in DIR (corpus/heldout/defects4j, ...); repeatable")
    ap.add_argument("--sample", type=int, default=None, help="with --heldout, draw this many cases per directory")
    ap.add_argument("--seed", type=int, default=1, help="seed for --sample")
    ap.add_argument("--conditions", default="raw,truncate,pare", help="comma-separated subset of " + ",".join(CONDITIONS))
    ap.add_argument("--trials", type=int, default=1, help="trials per task and condition")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-turns", type=int, default=25, help="model calls per trial")
    ap.add_argument("--max-tokens", type=int, default=16000, help="output tokens per model call")
    ap.add_argument("--no-fallbacks", dest="fallbacks", action="store_false",
                    help="do not send the server-side refusal fallback (on by default; needs the Claude API)")
    ap.add_argument("--budget", default="auto", help="pare's --budget for the pare condition (and $PARE_BUDGET)")
    ap.add_argument("--pare-bin", default=os.path.join(ROOT, "target", "release", "pare"))
    ap.add_argument("--truncate-chars", type=int, default=30000, help="the truncate condition's limit")
    ap.add_argument("--command-timeout", type=float, default=300, help="seconds per command the agent runs")
    ap.add_argument("--out", default=None, help="results directory (default bench/agent-runs/<stamp>)")
    ap.add_argument("--list", action="store_true", help="list the tasks and exit")
    ap.add_argument("--dry-run", action="store_true", help="show each condition's initial tool result size; no model calls")
    ap.add_argument("--show", action="store_true", help="with --dry-run, print the tool results themselves")
    ap.add_argument("--report", metavar="RESULTS.jsonl", help="summarise an earlier run and exit")
    opts = ap.parse_args()

    if opts.report:
        print(summarize(load_records(opts.report)))
        return 0

    tasks = load_tasks()
    for d in opts.heldout:
        tasks += heldout_tasks(d, opts.sample, opts.seed)
    if opts.tasks:
        wanted = [w.strip() for w in opts.tasks.split(",") if w.strip()]
        tasks = [t for t in tasks if any(w in t.name for w in wanted)]
    if not tasks:
        print("no tasks", file=sys.stderr)
        return 2

    if opts.list:
        for t in tasks:
            missing = t.missing_tools()
            note = f"  (skipped: {', '.join(missing)} not installed)" if missing else ""
            print(f"{t.name:40} {t.kind:9} {t.command}{note}")
            if t.notes:
                print(f"{'':40} {t.notes}")
        return 0

    conds = []
    for name in opts.conditions.split(","):
        name = name.strip()
        if name not in CONDITIONS:
            print(f"unknown condition {name!r}; choose from {', '.join(CONDITIONS)}", file=sys.stderr)
            return 2
        c = CONDITIONS[name](opts)
        if not c.available():
            reason = c.unavailable_reason() if isinstance(c, Pare) else "not installed"
            print(f"{name}: {reason}; skipped", file=sys.stderr)
            continue
        conds.append(c)
    if not conds:
        print("no conditions available", file=sys.stderr)
        return 2

    runnable = []
    for t in tasks:
        missing = t.missing_tools()
        if missing:
            print(f"{t.name}: skipped, needs {', '.join(missing)}", file=sys.stderr)
        else:
            runnable.append(t)
    tasks = runnable

    if opts.dry_run:
        print(f"{'task':40} {'condition':9} {'bytes':>9} {'~tokens':>8}  exit")
        for t in tasks:
            for c in conds:
                work = tempfile.mkdtemp(prefix="agent-eval-dry-")
                try:
                    ws = None
                    if t.kind == "fix":
                        ws = os.path.join(work, "proj")
                        shutil.copytree(t.workspace, ws, ignore=shutil.ignore_patterns("target", "__pycache__"))
                    out, code = initial_result(t, c, ws, work, dict(os.environ))
                    print(f"{t.name:40} {c.name:9} {len(out.encode()):9,} {est_tokens(out):8,}  {code if code is not None else '-'}")
                    if opts.show:
                        print(out.rstrip("\n") + "\n" + "-" * 78)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
        return 0

    out_dir = opts.out or os.path.join(RUNS_DIR, dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({**vars(opts), "tasks": [t.name for t in tasks],
                   "versions": {c.name: c.version() for c in conds}}, f, indent=1, default=str)
    client = make_client(opts.model)
    results_path = os.path.join(out_dir, "results.jsonl")
    records = []
    total = len(tasks) * len(conds) * opts.trials
    n = 0
    for trial in range(1, opts.trials + 1):
        for t in tasks:
            for c in conds:
                n += 1
                print(f"[{n}/{total}] {t.name} / {c.name} / trial {trial} ...", end=" ", flush=True)
                r = run_trial(client, opts, t, c, trial, out_dir)
                records.append(r)
                with open(results_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
                if "error" in r:
                    print(f"error: {r['error']}")
                else:
                    u = r["usage"]
                    print(f"{'ok' if r['success'] else 'FAIL'} score {r['score']:.2f}, {u['turns']} turns, "
                          f"{u['total_input']:,} in / {u['output']:,} out, {r['seconds']}s")
    summary = summarize(records)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
    print()
    print(summary)
    print(f"\nresults: {os.path.relpath(results_path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
