#!/usr/bin/env python3
"""Held-out cases from SWE-bench: real failing test runs, labelled by the dataset.

Each SWE-bench task records a repository at a buggy commit, a test patch,
and FAIL_TO_PASS: the tests that fail before the fix. SWE-bench publishes a
Docker image per task with the repository checked out and its environment
installed, and an eval script that applies the test patch and runs the
tests. Running that script without the fix produces exactly the output an
agent sees when it runs the failing tests, and FAIL_TO_PASS says which
tests a useful summary of it must name. Nobody's judgement is involved, so
the keep lists are blind to pare by construction.

For each selected task this script pulls the image, runs the eval script in
a container, checks with SWE-bench's own log parser that the FAIL_TO_PASS
tests did fail (a run where they did not is an environment problem, and is
skipped), and writes a case:

    NAME.log   the test command's output (between SWE-bench's markers)
    NAME.cmd   the test command
    NAME.keep  each failing FAIL_TO_PASS test id, as it appears in the log
    NAME.json  task metadata, what was checked, what could not be located

Cases go to corpus-heldout/swebench/ with a MANIFEST.tsv line whose source is
"dataset". Report them on their own: `bench/run.py --corpus
corpus-heldout/swebench`.

Usage
    python bench/swebench_cases.py run --sample 20 [--seed 0] [--dry-run]
    python bench/swebench_cases.py run --instances django__django-11099,psf__requests-1142
    python bench/swebench_cases.py run --dataset tasks.jsonl --instances ...

Needs Docker and the swebench package (`pip install swebench`, or the
`datasets` extra of this repository). The dataset loads from Hugging Face
(SWE-bench/SWE-bench_Verified by default) or a local .jsonl/.json/.parquet
with the same fields. Images are 1-4 GB each; ones this script pulls are
removed after use unless --keep-images.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import collect  # noqa: E402  (manifest helpers, pare version)

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_OUT = os.path.join(ROOT, "corpus-heldout", "swebench")
START = ">>>>> Start Test Output"
END = ">>>>> End Test Output"


def need_swebench():
    try:
        import swebench  # noqa: F401
        from swebench.harness import grading, utils  # noqa: F401
    except ImportError:
        sys.exit("swebench_cases: the swebench package is needed: pip install swebench "
                 "(or pip install -e '.[datasets]' in this repository)")


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def load_tasks(name: str, split: str) -> list[dict]:
    need_swebench()
    from swebench.harness.utils import load_swebench_dataset
    try:
        return [dict(t) for t in load_swebench_dataset(name, split)]
    except Exception as e:  # noqa: BLE001 - network, auth, missing file: one message
        local = name.endswith((".jsonl", ".json", ".parquet"))
        hint = ("" if local else
                "; the dataset comes from Hugging Face: check access to huggingface.co, or pass a local "
                ".jsonl/.json/.parquet with the same fields via --dataset")
        sys.exit(f"swebench_cases: could not load {name} ({type(e).__name__}: {str(e)[:200]}){hint}")


def fail_to_pass(task: dict) -> list[str]:
    v = task.get("FAIL_TO_PASS") or []
    return json.loads(v) if isinstance(v, str) else list(v)


def select(tasks: list[dict], *, ids: list[str] | None, sample: int, seed: int, max_fail: int) -> list[dict]:
    """The requested ids in order, or a seeded sample spread evenly across
    repositories (round robin), among tasks with 1..max_fail failing tests
    (a task with fifty failing tests would dominate the recall total)."""
    by_id = {t["instance_id"]: t for t in tasks}
    if ids:
        missing = [i for i in ids if i not in by_id]
        if missing:
            sys.exit(f"swebench_cases: not in the dataset: {', '.join(missing)}")
        return [by_id[i] for i in ids]
    rng = random.Random(seed)
    pools: dict[str, list[dict]] = {}
    for t in sorted(tasks, key=lambda t: t["instance_id"]):
        if 1 <= len(fail_to_pass(t)) <= max_fail:
            pools.setdefault(t["repo"], []).append(t)
    for pool in pools.values():
        rng.shuffle(pool)
    picked: list[dict] = []
    repos = sorted(pools)
    while len(picked) < sample and any(pools[r] for r in repos):
        for r in repos:
            if pools[r] and len(picked) < sample:
                picked.append(pools[r].pop())
    return picked


def image_of(task: dict) -> str:
    if task.get("image"):
        return task["image"]
    return f"swebench/sweb.eval.x86_64.{task['instance_id']}:latest".replace("__", "_1776_").lower()


def test_command(eval_script: str) -> str:
    """The command(s) the eval script runs between its output markers."""
    lines = eval_script.splitlines()
    inside, cmds = False, []
    for ln in lines:
        if START in ln:
            inside = True
            continue
        if END in ln:
            break
        if inside and ln.strip() and not ln.strip().startswith("#") and "TEST_EXIT_CODE" not in ln:
            cmds.append(ln.strip())
    return " && ".join(cmds)


def test_output(log: str) -> str:
    """The log between the markers, without the marker lines and the shell's
    trace of the test command itself (the eval script runs under set -x)."""
    out, inside = [], False
    for ln in log.splitlines():
        if START in ln:
            inside = True
            continue
        if END in ln:
            break
        if inside:
            out.append(ln)
    while out and out[0].startswith("+ "):
        out.pop(0)
    while out and out[-1].startswith("+ ") and "TEST_EXIT_CODE" in out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


DJANGO_ID = re.compile(r"^(\w+) \(([\w.]+)\)$")


def locate(test_id: str, text: str) -> str | None:
    """The form of a test id that appears in the log: the id itself, or
    Django's newer `name (module.Class.name)` spelling of `name
    (module.Class)`. The keep line is what a summary must contain."""
    candidates = [test_id]
    m = DJANGO_ID.match(test_id)
    if m:
        candidates.append(f"{m.group(1)} ({m.group(2)}.{m.group(1)})")
    for c in candidates:
        if c in text:
            return c
    return None


# --------------------------------------------------------------------------
# Docker
# --------------------------------------------------------------------------

class Docker:
    """The four docker operations the harness needs; a test replaces it."""

    def has_image(self, image: str) -> bool:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0

    def pull(self, image: str) -> tuple[bool, str]:
        p = subprocess.run(["docker", "pull", "-q", "--platform", "linux/amd64", image], capture_output=True, text=True)
        return p.returncode == 0, (p.stderr or p.stdout).strip()[-300:]

    def run(self, image: str, script: str, *, timeout: int, network: str) -> tuple[str, bool]:
        """Run the eval script in a fresh container; (merged output, timed out)."""
        with tempfile.TemporaryDirectory(prefix="pare-swebench-") as d:
            path = os.path.join(d, "eval.sh")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            name = f"pare-swebench-{uuid.uuid4().hex[:12]}"
            cmd = ["docker", "run", "--rm", "--name", name, "--platform", "linux/amd64", "--network", network,
                   "-v", f"{d}:/pare-eval:ro", "--entrypoint", "/bin/bash", image, "-c", "bash /pare-eval/eval.sh 2>&1"]
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
                return p.stdout.decode("utf-8", "replace"), False
            except subprocess.TimeoutExpired as e:
                subprocess.run(["docker", "kill", name], capture_output=True)
                return (e.stdout or b"").decode("utf-8", "replace"), True

    def remove_image(self, image: str) -> None:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)


# --------------------------------------------------------------------------
# One task
# --------------------------------------------------------------------------

def evaluate(task: dict, log: str) -> tuple[bool, list[str]]:
    """Did the eval run cleanly, and which FAIL_TO_PASS tests failed, by
    SWE-bench's own parser for this repository."""
    from swebench.harness.grading import get_logs_eval, test_failed
    from swebench.harness.utils import make_test_spec
    spec = make_test_spec(task)
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write(log)
        path = f.name
    try:
        status, ok = get_logs_eval(spec, path)
    finally:
        os.unlink(path)
    if not ok:
        return False, []
    return True, [t for t in fail_to_pass(task) if test_failed(t, status)]


def make_case(task: dict, docker: Docker, args) -> tuple[str, dict]:
    """Run one task. Returns (status, details): status "written" or the
    reason it was skipped."""
    iid = task["instance_id"]
    image = image_of(task)
    details: dict = {"instance_id": iid, "image": image}
    pulled = False
    if not docker.has_image(image):
        ok, msg = docker.pull(image)
        if not ok:
            return "image pull failed", {**details, "error": msg}
        pulled = True
    try:
        t0 = time.time()
        # SWE-bench's own wrapping of the dataset's script, which also records
        # the test command's exit status for the grader.
        from swebench.harness.utils import make_test_spec
        script = make_test_spec(task).eval_script
        log, timed_out = docker.run(image, script, timeout=args.timeout, network=args.network)
        details["seconds"] = round(time.time() - t0, 1)
    finally:
        if pulled and not args.keep_images:
            docker.remove_image(image)
    if timed_out:
        save_failed(args.out, iid, log)
        return "timed out", details
    ok, failing = evaluate(task, log)
    if not ok:
        save_failed(args.out, iid, log)
        return "eval did not run cleanly", details
    if not failing:
        save_failed(args.out, iid, log)
        return "FAIL_TO_PASS tests did not fail", details
    output = test_output(log)
    nlines = output.count("\n")
    if nlines < args.min_lines:
        return f"under {args.min_lines} lines", {**details, "lines": nlines}
    keep, unlocated = [], []
    for t in failing:
        form = locate(t, output)
        (keep.append(form) if form else unlocated.append(t))
    if not keep:
        save_failed(args.out, iid, log)
        return "no failing test id found in the output", {**details, "unlocated": unlocated}
    base = os.path.join(args.out, iid)
    with open(base + ".log", "w", encoding="utf-8") as f:
        f.write(output)
    with open(base + ".keep", "w", encoding="utf-8") as f:
        f.writelines(k + "\n" for k in keep)
    command = test_command(task["eval_script"])
    with open(base + ".cmd", "w", encoding="utf-8") as f:
        f.write(command + "\n")
    meta = {
        "instance_id": iid, "repo": task.get("repo"), "version": task.get("version"),
        "base_commit": task.get("base_commit"), "image": image, "dataset": args.dataset, "split": args.split,
        "fail_to_pass": fail_to_pass(task), "failed": failing, "keep": keep, "unlocated": unlocated,
        "pass_to_pass": len(json.loads(task["PASS_TO_PASS"]) if isinstance(task.get("PASS_TO_PASS"), str)
                            else task.get("PASS_TO_PASS") or []),
        "lines": nlines, "seconds": details.get("seconds"), "network": args.network,
        "collected": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pare": collect.pare_version(), "commit": collect.git_commit(), "swebench": swebench_version(),
    }
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    collect.append_manifest(args.out, [
        iid, meta["collected"], meta["pare"], meta["commit"], "", str(nlines), str(len(output.encode())),
        "", command, "dataset", "dataset FAIL_TO_PASS"])
    return "written", {**details, "lines": nlines, "keep": len(keep), "unlocated": len(unlocated)}


def save_failed(out: str, iid: str, log: str) -> None:
    """Keep the full eval log of a task that did not produce a case, for
    debugging; the directory is ignored by git."""
    d = os.path.join(out, "_failed")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, iid + ".eval.log"), "w", encoding="utf-8") as f:
        f.write(log)


def swebench_version() -> str:
    try:
        import importlib.metadata as m
        return m.version("swebench")
    except Exception:  # noqa: BLE001
        return "?"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_run(args, docker: Docker | None = None) -> int:
    tasks = load_tasks(args.dataset, args.split)
    ids = [i.strip() for i in args.instances.split(",") if i.strip()] if args.instances else None
    chosen = select(tasks, ids=ids, sample=args.sample, seed=args.seed, max_fail=args.max_fail_tests)
    print(f"{len(chosen)} task(s) from {args.dataset} ({args.split}), "
          f"{len({t['repo'] for t in chosen})} repositories")
    if args.dry_run:
        for t in chosen:
            print(f"  {t['instance_id']:<40} {len(fail_to_pass(t)):>2} failing  {image_of(t)}")
        return 0
    os.makedirs(args.out, exist_ok=True)
    docker = docker or Docker()
    counts: dict[str, int] = {}
    for n, task in enumerate(chosen, 1):
        iid = task["instance_id"]
        if os.path.exists(os.path.join(args.out, iid + ".log")) and not args.force:
            status, details = "already collected", {}
        else:
            status, details = make_case(task, docker, args)
        counts[status] = counts.get(status, 0) + 1
        extra = ", ".join(f"{k} {v}" for k, v in details.items() if k in ("lines", "keep", "unlocated", "seconds", "error"))
        print(f"[{n}/{len(chosen)}] {iid:<40} {status}" + (f"  ({extra})" if extra else ""), flush=True)
    print("; ".join(f"{v} {k}" for k, v in counts.items()))
    return 0 if counts.get("written", 0) or counts.get("already collected", 0) else 1


def main(argv: list[str] | None = None, docker: Docker | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run", help="collect cases from SWE-bench tasks")
    p.add_argument("--dataset", default=DEFAULT_DATASET, help=f"Hugging Face name or local .jsonl/.json/.parquet (default {DEFAULT_DATASET})")
    p.add_argument("--split", default="test")
    p.add_argument("--instances", help="comma-separated instance ids (default: a seeded sample)")
    p.add_argument("--sample", type=int, default=20, help="tasks to sample, spread across repositories (default 20)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-fail-tests", type=int, default=10, help="skip tasks with more failing tests than this when sampling")
    p.add_argument("--min-lines", type=int, default=40,
                   help="skip outputs shorter than this; pare passes small outputs through unchanged (default 40)")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per task (default 1800)")
    p.add_argument("--network", default="bridge", help="docker network for the container (default bridge; 'none' to cut it off)")
    p.add_argument("--keep-images", action="store_true", help="keep images this run pulled (they are 1-4 GB each)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--force", action="store_true", help="redo tasks that already have a case")
    p.add_argument("--dry-run", action="store_true", help="show the selection and stop")
    p.set_defaults(func=cmd_run)
    args = ap.parse_args(argv)
    return args.func(args, docker)


if __name__ == "__main__":
    sys.exit(main())
