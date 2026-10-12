#!/usr/bin/env python3
"""Collect run files into the held-out corpus without looking at pare's output.

pare saves every input it summarises to a run file (~/.cache/pare/runs by
default), and `pare -- CMD` also records the command next to it
(<hash>.json). This script turns those run files into held-out cases
following corpus-heldout/README.md: it copies the raw log, writes the
command, creates an empty keep list for you to fill in from the raw log,
and records where the case came from. It never runs pare on the log, so the
keep list is written before you have seen what pare would keep.

Usage
    python bench/collect.py list [-n 20]            recent run files, newest first
    python bench/collect.py add RUN [--name NAME] [--cmd "CMD"] [--edit]
    python bench/collect.py check                   keep lines present in their logs?

RUN is a number from `list`, a run-file hash (or a prefix of one), or a
path. The copy replaces your home directory and the command's working
directory with /home/dev and /home/dev/proj; review it for secrets before
committing, and `add` points at lines that look like credentials.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CORPUS = os.path.join(ROOT, "corpus-heldout")
MANIFEST = "MANIFEST.tsv"
MANIFEST_HEADER = "name\tcollected\tpare\tcommit\tsha256\tlines\tbytes\texit\tcommand\tsource\tlabels\n"

# Shapes that are usually credentials. Matches are reported by line number
# only; the person collecting decides what to redact.
SECRET_RX = re.compile(
    r"AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|gh[pousr]_[A-Za-z0-9]{30,}"  # GitHub tokens
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|sk-[A-Za-z0-9_-]{20,}"  # API keys in the sk- style
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[=:]\s*\S{6,})"
    r"|(?i:\bauthorization:\s*(?:bearer|basic)\s+\S{8,})"
)


# A keep line may list equivalent forms of one fact, separated by " || ":
# the same failing test as Surefire 2 and 3 print it, or an exception and
# its message as the summary repeats it. The line is kept when any form is.
KEEP_OR = " || "


def keep_forms(line: str) -> list[str]:
    return [f.strip() for f in line.split(KEEP_OR) if f.strip()]


def form_in(form: str, text: str) -> bool:
    """A form appears in the text, not run on into a longer name: the test
    testResolve1 is not shown by a line naming testResolve12."""
    left = r"(?<![A-Za-z0-9_])" if (form[:1].isalnum() or form[:1] == "_") else ""
    right = r"(?![A-Za-z0-9_])" if (form[-1:].isalnum() or form[-1:] == "_") else ""
    return re.search(left + re.escape(form) + right, text) is not None


def keep_found(line: str, text: str) -> bool:
    return any(form_in(f, text) for f in keep_forms(line))


def run_dir() -> str:
    env = os.environ.get("PARE_RUN_DIR")
    if env:
        return env
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "pare", "runs")


def runs(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    paths = [os.path.join(directory, f) for f in os.listdir(directory)
             if f.endswith(".log") and not f.startswith(".")]
    return sorted(paths, key=lambda p: (-os.path.getmtime(p), p))


def meta_of(path: str) -> dict:
    try:
        with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def count_lines(path: str) -> int:
    n = 0
    last = b"\n"
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (0 if last == b"\n" else 1)


def describe(path: str) -> tuple[str, str, str]:
    m = meta_of(path)
    cmd = shlex.join(m["command"]) if m.get("command") else "(piped; command unknown)"
    code = str(m["exit"]) if "exit" in m else "-"
    when = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    return when, code, cmd


def resolve(spec: str, directory: str) -> str:
    if os.path.isfile(spec):
        return spec
    found = runs(directory)
    if spec.isdigit() and 1 <= int(spec) <= len(found) and len(spec) < 6:
        return found[int(spec) - 1]
    matches = [p for p in found if os.path.basename(p).startswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"collect: no run file matches {spec!r} in {directory}")
    sys.exit(f"collect: {spec!r} matches {len(matches)} run files; give more of the hash")


def default_name(command: list[str] | None, corpus: str) -> str:
    command = list(command or [])
    if len(command) >= 3 and os.path.basename(command[0]) in ("sh", "bash", "zsh") and command[1] == "-c":
        try:  # name a shell one-liner after the command inside it
            command = shlex.split(command[2])
        except ValueError:
            pass
    words = [w for w in command if not w.startswith("-") and "=" not in w and w not in ("&&", "||", ";", "|")][:2]
    base = "-".join(re.sub(r"[^A-Za-z0-9]+", "", os.path.basename(w)) for w in words) or "case"
    base = f"{base}-{dt.date.today():%Y%m%d}"
    name, n = base, 2
    while os.path.exists(os.path.join(corpus, name + ".log")):
        name, n = f"{base}-{n}", n + 1
    return name


def scrub(data: bytes, cwd: str | None) -> bytes:
    """Replace the working directory and the home directory with neutral
    paths, longest first, so the corpus does not carry a username."""
    pairs = []
    home = os.path.expanduser("~")
    if cwd and cwd not in ("/", home):
        pairs.append((cwd, "/home/dev/proj"))
    if home and home != "/":
        pairs.append((home, "/home/dev"))
    for old, new in sorted(pairs, key=lambda p: -len(p[0])):
        data = data.replace(old.encode(), new.encode())
    return data


def append_manifest(corpus: str, row: list[str]) -> None:
    """One line per case: where it came from and how its keep list was made
    (source: run-file or transcript; labels: manual or reviewed proposals),
    so results can be reported by origin."""
    manifest = os.path.join(corpus, MANIFEST)
    new = not os.path.exists(manifest)
    with open(manifest, "a", encoding="utf-8") as f:
        if new:
            f.write(MANIFEST_HEADER)
        f.write("\t".join(c.replace("\t", " ").replace("\n", " ") for c in row) + "\n")


def pare_version() -> str:
    try:
        with open(os.path.join(ROOT, "proto", "pare.py"), encoding="utf-8") as f:
            m = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


def git_commit() -> str:
    try:
        p = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
        return p.stdout.strip() or "?"
    except OSError:
        return "?"


def cmd_list(args) -> int:
    found = runs(args.runs)
    if not found:
        print(f"no run files in {args.runs}")
        return 0
    print(f"run files in {args.runs}, newest first")
    print(f"{'#':>3}  {'when':<16}  {'lines':>7}  {'exit':>4}  {'hash':<12}  command")
    for i, path in enumerate(found[: args.n], 1):
        when, code, cmd = describe(path)
        h = os.path.splitext(os.path.basename(path))[0][:12]
        print(f"{i:>3}  {when:<16}  {count_lines(path):>7}  {code:>4}  {h:<12}  {cmd[:80]}")
    if len(found) > args.n:
        print(f"... {len(found) - args.n} more (-n to show them)")
    return 0


def cmd_add(args) -> int:
    src = resolve(args.run, args.runs)
    meta = meta_of(src)
    command = meta.get("command")
    if args.cmd:
        cmd_text = args.cmd
    elif command:
        cmd_text = shlex.join(command)
    elif sys.stdin.isatty():
        cmd_text = input("command that produced this output: ").strip()
    else:
        sys.exit("collect: this run was piped, so its command is unknown; pass --cmd")
    if not cmd_text:
        sys.exit("collect: a command is required (the .cmd file)")
    os.makedirs(args.corpus, exist_ok=True)
    name = args.name or default_name(command or shlex.split(cmd_text), args.corpus)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        sys.exit(f"collect: {name!r} is not a usable case name (letters, digits, . _ -)")
    log_path = os.path.join(args.corpus, name + ".log")
    keep_path = os.path.join(args.corpus, name + ".keep")
    if os.path.exists(log_path):
        sys.exit(f"collect: {log_path} exists; choose another --name")

    with open(src, "rb") as f:
        raw = f.read()
    data = raw if args.no_scrub else scrub(raw, meta.get("cwd"))
    with open(log_path, "wb") as f:
        f.write(data)
    with open(os.path.join(args.corpus, name + ".cmd"), "w", encoding="utf-8") as f:
        f.write(cmd_text + "\n")
    if not os.path.exists(keep_path):
        open(keep_path, "w").close()

    append_manifest(args.corpus, [
        name, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), pare_version(), git_commit(),
        hashlib.sha256(raw).hexdigest()[:16], str(data.count(b"\n")), str(len(data)),
        str(meta.get("exit", "")), cmd_text, "run-file", "manual"])

    suspicious = [i for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1) if SECRET_RX.search(line)]
    print(f"added {name}")
    print(f"  raw log    {log_path}")
    print(f"  keep list  {keep_path}  (empty: fill it in now)")
    print("Write the keep list from the raw log, before running pare on it: one line per")
    print("must-survive line, copied verbatim (error messages, failing test names, the")
    print("location, the final verdict). `collect.py check` confirms each line is in the log.")
    if suspicious:
        shown = ", ".join(f"L{n}" for n in suspicious[:20]) + (" ..." if len(suspicious) > 20 else "")
        print(f"possible credentials on {len(suspicious)} line(s): {shown}; redact before committing")
    else:
        print("no credential-shaped strings found; still review the log before committing")
    if args.edit:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.call(shlex.split(editor) + [keep_path, log_path])
    return 0


def cmd_check(args) -> int:
    if not os.path.isdir(args.corpus):
        sys.exit(f"collect: no corpus directory {args.corpus}")
    names = sorted(f[:-4] for f in os.listdir(args.corpus) if f.endswith(".log"))
    if not names:
        print(f"no cases in {args.corpus}")
        return 0
    problems = 0
    for name in names:
        base = os.path.join(args.corpus, name)
        issues = []
        if not os.path.exists(base + ".cmd"):
            issues.append("no .cmd")
        keep = []
        if os.path.exists(base + ".keep"):
            with open(base + ".keep", encoding="utf-8") as f:
                keep = [ln.strip() for ln in f if ln.strip()]
        if not keep:
            issues.append("keep list empty")
        with open(base + ".log", encoding="utf-8", errors="replace") as f:
            text = f.read()
        missing = [k for k in keep if not keep_found(k, text)]
        for k in missing:
            issues.append(f"keep line not in log: {k[:100]}")
        status = "ok" if not issues else "; ".join(issues[:1]) + (f" (+{len(issues) - 1} more)" if len(issues) > 1 else "")
        print(f"{name:<32} {len(keep):>3} keep  {status}")
        for extra in issues[1:]:
            print(f"{'':<32}       {extra}")
        problems += bool(issues)
    print(f"{len(names)} cases, {problems} with problems")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default=run_dir(), help="run-file directory (default $PARE_RUN_DIR or ~/.cache/pare/runs)")
    ap.add_argument("--corpus", default=DEFAULT_CORPUS, help="corpus directory (default corpus-heldout/)")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("list", help="list recent run files")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("add", help="copy a run file into the corpus")
    p.add_argument("run", help="number from `list`, hash prefix, or path")
    p.add_argument("--name", help="case name (default: from the command and today's date)")
    p.add_argument("--cmd", help="the command, when the run was piped and did not record it")
    p.add_argument("--no-scrub", action="store_true", help="copy the log byte for byte")
    p.add_argument("--edit", action="store_true", help="open the keep list and the log in $EDITOR")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("check", help="every case has a .cmd and a keep list whose lines are in its log")
    p.set_defaults(func=cmd_check)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
