#!/usr/bin/env python3
"""Benchmark pare against sqz and tidyout on the corpus.

For every corpus/<name>.log:
  * <name>.keep  lines that must survive (one per line; a line survives if
                 its stripped text appears in the tool output, not run on
                 into a longer name; "a || b" lists equivalent forms, any of
                 which counts,
                 after pare's compact lists are expanded: a header
                 "T29 ×17 E FAILED t.py::{…}" followed by "  …test_a" stands
                 for "FAILED t.py::test_a"; --strict matches the literal text)
  * <name>.cmd   the command that produced the log (optional). tidyout only
                 applies rules keyed on the command name, so the runner puts
                 a fake binary with that name on PATH that cats the log; sqz
                 gets it as --cmd.

Usage
    python bench/run.py [--budget 2000] [--corpus corpus] [--tools pare,sqz,tidyout] [--json out.json]

Tokens are approximated as ceil(bytes / 2.8) everywhere (--chars-per-token),
matching pare's own budget accounting. Tools that are not installed are reported as "n/a".
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402  (keep-line matching)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CHARS_PER_TOKEN = 2.8  # set from --chars-per-token


def tokens(data: bytes) -> int:
    return math.ceil(len(data) / CHARS_PER_TOKEN - 1e-9)  # 2800 / 2.8 is 1000, not 1001


def which(name: str) -> str | None:
    venv = os.path.join(ROOT, ".venv", "bin", name)
    if os.path.exists(venv):
        return venv
    return shutil.which(name)


def pare_cmd() -> list[str]:
    """How to run reference/pare.py: the repo venv if there is one, else the
    current interpreter (pare needs nothing beyond the standard library)."""
    script = os.path.join(ROOT, "reference", "pare.py")
    venv = os.path.join(ROOT, ".venv", "bin", "python")
    return [venv if os.path.exists(venv) else sys.executable, script]


class Tool:
    name = "tool"

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "?"

    def run(self, log_path: str, cmd: list[str], budget: int, work: str) -> bytes:
        raise NotImplementedError


class Raw(Tool):
    name = "raw"

    def run(self, log_path, cmd, budget, work):
        with open(log_path, "rb") as f:
            return f.read()


class Pare(Tool):
    name = "pare"
    extra_args: list[str] = []  # set from --pare-args

    def version(self) -> str:
        p = subprocess.run(pare_cmd() + ["--version"], capture_output=True, text=True)
        return p.stdout.strip().replace("pare ", "")

    def run(self, log_path, cmd, budget, work):
        with open(log_path, "rb") as f:
            p = subprocess.run(
                # --no-tee: the run-file path is part of pare's output, and temp
                # paths differ in length between machines (macOS /var/folders/...).
                pare_cmd() + ["--budget", str(budget), "--no-tee", "--chars-per-token", str(CHARS_PER_TOKEN)] + self.extra_args,
                stdin=f, capture_output=True,
            )
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="replace").strip().splitlines()[-1][:120] if p.stderr else "pare failed")
        return p.stdout


class Sqz(Tool):
    name = "sqz"

    def available(self):
        return which("sqz") is not None

    def version(self):
        p = subprocess.run([which("sqz"), "--version"], capture_output=True, text=True)
        return p.stdout.strip().replace("sqz ", "") or "?"

    def run(self, log_path, cmd, budget, work):
        args = [which("sqz"), "compress", "--no-cache"]
        if cmd:
            args += ["--cmd", cmd[0]]
        env = dict(os.environ, HOME=os.path.join(work, "home"), SQZ_NO_DEDUP="1")
        os.makedirs(env["HOME"], exist_ok=True)
        with open(log_path, "rb") as f:
            p = subprocess.run(args, stdin=f, capture_output=True, env=env)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="replace")[:300])
        return p.stdout


def fake_command(log_path: str, cmd: list[str], work: str) -> tuple[str, list[str], dict]:
    """A fake program on PATH that replays the log, named like the command
    that produced it so a wrapper's per-command rules apply. Returns the name,
    its arguments and the environment to run it in."""
    # The fake goes on PATH under the program's base name: a command given
    # as a path (./tests/runtests.py) would otherwise name a directory that
    # does not exist. A shell wrapper (`cd x && ...`) is not a program, and
    # gets a neutral name that no tidyout rule matches.
    name = os.path.basename(cmd[0]) if cmd else ""
    if not re.fullmatch(r"[\w.+-]+", name) or name == "cat":
        name, cmd = "replay-log", ["replay-log"]
    fake_bin = os.path.join(work, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake = os.path.join(fake_bin, name)
    # The real cat by absolute path: a fake named like a tool the fake
    # itself calls would find itself first on PATH and exec forever.
    cat = shutil.which("cat") or "/bin/cat"
    with open(fake, "w") as f:
        f.write(f"#!/bin/sh\nexec {json.dumps(cat)} {json.dumps(log_path)}\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=fake_bin + os.pathsep + os.environ.get("PATH", ""))
    return name, cmd[1:], env


class Tidyout(Tool):
    name = "tidyout"

    def available(self):
        return which("tidyout") is not None

    def version(self):
        # tidyout has no --version flag (it would run "--version" as a command);
        # ask the interpreter named in the script's shebang for its package version.
        try:
            with open(which("tidyout"), encoding="utf-8", errors="replace") as f:
                shebang = f.readline().strip()
            py = shebang[2:].split()[0] if shebang.startswith("#!") else sys.executable
            p = subprocess.run([py, "-c", "import importlib.metadata as m; print(m.version('tidyout-cli'))"],
                               capture_output=True, text=True)
            return p.stdout.strip() or "?"
        except OSError:
            return "?"

    def run(self, log_path, cmd, budget, work):
        # tidyout wraps a command; give it a fake one that replays the log so
        # its per-command rules (pytest, cargo build, ...) apply.
        name, args, env = fake_command(log_path, cmd, work)
        try:
            p = subprocess.run([which("tidyout"), name, *args], capture_output=True, env=env,
                               stdin=subprocess.DEVNULL, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"tidyout: timed out on {log_path}", file=sys.stderr)
            return b""
        return p.stdout


TOOLS = {t.name: t for t in (Raw(), Pare(), Sqz(), Tidyout())}


LIST_ITEM = "  …"


def expand_lists(text: str) -> str:
    """pare's compact list form written out in full: under a line containing
    "{…}", each "  …item" line becomes the header's text before "{…}", the
    item, and the header's text after it. Other tools' output never has the
    form, so it is unchanged."""
    out: list[str] = []
    pre = suf = None
    for line in text.splitlines():
        if "{…}" in line:
            pre, _, suf = line.partition("{…}")
        elif pre is not None and line.startswith(LIST_ITEM):
            line = pre + line[len(LIST_ITEM):] + suf
        elif not line.startswith("  ┆"):
            pre = suf = None
        out.append(line)
    return "\n".join(out)


def survivors(output: bytes, keep: list[str], strict: bool = False) -> list[bool]:
    """Which keep lines the output shows: any of a line's " || " forms,
    not run on into a longer name (collect.keep_found)."""
    text = output.decode("utf-8", errors="replace")
    if not strict:
        text = expand_lists(text)
    return [collect.keep_found(k, text) for k in keep]


def recall(output: bytes, keep: list[str], strict: bool = False) -> tuple[int, int]:
    return sum(survivors(output, keep, strict)), len(keep)


def load_case(corpus: str, name: str):
    log_path = os.path.join(corpus, name + ".log")
    keep_path = os.path.join(corpus, name + ".keep")
    cmd_path = os.path.join(corpus, name + ".cmd")
    keep = []
    if os.path.exists(keep_path):
        with open(keep_path, encoding="utf-8") as f:
            keep = [ln.strip() for ln in f if ln.strip()]
    cmd: list[str] = []
    if os.path.exists(cmd_path):
        with open(cmd_path, encoding="utf-8") as f:
            cmd = f.read().split()
    return log_path, keep, cmd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=os.path.join(ROOT, "corpus"))
    ap.add_argument("--budget", type=int, default=2000, help="pare token budget (default 2000)")
    ap.add_argument("--tools", default="pare,sqz,tidyout", help="comma-separated subset of pare,sqz,tidyout")
    ap.add_argument("--json", help="also write results as JSON to this path")
    ap.add_argument("--show-misses", action="store_true", help="list must-survive lines each tool dropped")
    ap.add_argument("--strict", action="store_true",
                    help="match keep lines against the literal output, without expanding pare's compact lists")
    ap.add_argument("--pare-args", default="", help='extra flags for pare, e.g. "--max-templates 8"')
    ap.add_argument("--chars-per-token", type=float, default=2.8,
                    help="bytes per token for every tool's count and pare's budget (default 2.8, pare's default)")
    args = ap.parse_args()
    global CHARS_PER_TOKEN
    CHARS_PER_TOKEN = args.chars_per_token
    Pare.extra_args = shlex.split(args.pare_args)

    names = sorted(f[:-4] for f in os.listdir(args.corpus) if f.endswith(".log"))
    if not names:
        sys.exit(f"no .log files in {args.corpus}")
    tool_names = ["raw"] + [t.strip() for t in args.tools.split(",") if t.strip()]
    tools = []
    for tn in tool_names:
        if tn not in TOOLS:
            sys.exit(f"unknown tool {tn!r}; choose from {', '.join(TOOLS)}")
        tools.append(TOOLS[tn])

    rows = []
    misses: dict[tuple[str, str], list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="pare-bench-") as work:
        for name in names:
            log_path, keep, cmd = load_case(args.corpus, name)
            with open(log_path, "rb") as f:
                in_tok = tokens(f.read())
            for tool in tools:
                row = {"case": name, "tool": tool.name, "in_tok": in_tok, "keep": len(keep)}
                if not tool.available():
                    row.update(out_tok=None, hit=None, err="not installed")
                    rows.append(row)
                    continue
                try:
                    out = tool.run(log_path, cmd, args.budget, work)
                except Exception as e:  # noqa: BLE001
                    row.update(out_tok=None, hit=None, err=str(e)[:80])
                    rows.append(row)
                    continue
                found = survivors(out, keep, args.strict)
                row.update(out_tok=tokens(out), hit=sum(found))
                misses[(name, tool.name)] = [k for k, ok in zip(keep, found) if not ok]
                rows.append(row)

    # Per-case table.
    versions = ", ".join(f"{t.name} {t.version()}" for t in tools if t.name != "raw" and t.available())
    print(f"budget={args.budget} tokens (bytes/{CHARS_PER_TOKEN:g})   tools: {', '.join(t.name for t in tools)}")
    print(f"versions: {versions}")
    print("modes: pare --no-tee; sqz compress --no-cache --cmd <name> (one-shot, so its session-level "
          "dedup is not exercised); tidyout <cmd> with a fake <cmd> on PATH that replays the log\n")
    hdr = f"{'case':<16} {'tool':<8} {'in_tok':>8} {'out_tok':>8} {'ratio':>7} {'recall':>9}"
    print(hdr)
    print("-" * len(hdr))
    for row in rows:
        if row.get("err"):
            print(f"{row['case']:<16} {row['tool']:<8} {row['in_tok']:>8} {'n/a':>8} {'':>7} {'':>9}   {row['err']}")
            continue
        ratio = row["out_tok"] / row["in_tok"] if row["in_tok"] else 0.0
        rec = f"{row['hit']}/{row['keep']}" if row["keep"] else "-"
        print(f"{row['case']:<16} {row['tool']:<8} {row['in_tok']:>8} {row['out_tok']:>8} {ratio:>6.1%} {rec:>9}")

    # Aggregate per tool.
    print()
    hdr = f"{'tool':<8} {'cases':>5} {'in_tok':>9} {'out_tok':>9} {'ratio':>7} {'recall':>9} {'min-recall':>11}"
    print(hdr)
    print("-" * len(hdr))
    summary = []
    for tool in tools:
        ok = [r for r in rows if r["tool"] == tool.name and not r.get("err")]
        if not ok:
            print(f"{tool.name:<8} {0:>5}   (not installed or failed on every case)")
            continue
        in_t = sum(r["in_tok"] for r in ok)
        out_t = sum(r["out_tok"] for r in ok)
        kept = sum(r["hit"] for r in ok)
        total = sum(r["keep"] for r in ok)
        per_case = [r["hit"] / r["keep"] for r in ok if r["keep"]]
        rec = kept / total if total else 0.0
        mn = min(per_case) if per_case else 0.0
        print(f"{tool.name:<8} {len(ok):>5} {in_t:>9} {out_t:>9} {out_t / in_t:>6.1%} {rec:>8.0%} {mn:>10.0%}")
        summary.append({"tool": tool.name, "cases": len(ok), "in_tok": in_t, "out_tok": out_t,
                        "ratio": out_t / in_t, "recall": rec, "min_recall": mn})

    if args.show_misses:
        print()
        for (name, tn), lost in sorted(misses.items()):
            if lost:
                print(f"{name} / {tn} dropped {len(lost)}:")
                for k in lost:
                    print(f"    {k}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"budget": args.budget, "rows": rows, "summary": summary}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
