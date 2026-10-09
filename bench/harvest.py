#!/usr/bin/env python3
"""Harvest held-out cases from Claude Code session transcripts.

Claude Code keeps every session as JSONL under ~/.claude/projects/<project>/:
each Bash command, its output and exit status, and what the agent did next.
When an output is too large for the model's context, the full text is saved
under the session's tool-results/ folder and the model sees only a preview;
those outputs are exactly the case pare is for. This script turns them into
candidate held-out cases, in a staging folder you review before anything
enters the corpus.

Usage
    python bench/harvest.py scan [--projects DIR | --transcript FILE ...] [--min-lines 80] [--all] [--dry-run]
    python bench/harvest.py promote NAME [NAME ...]

scan writes each candidate to corpus-heldout/inbox/ (ignored by git):
    NAME.log            the full output, scrubbed (see below)
    NAME.cmd            the command
    NAME.keep.proposed  candidate keep lines: output lines the agent quoted,
                        or whose file:line, test id or file it went to next
    NAME.json           provenance (transcript, session, tool call, time,
                        exit status), what was scrubbed, and why each
                        proposed line was proposed

You then write NAME.keep from the raw log (the proposals are a starting
point: confirm, cut or add), and `promote` moves the case into
corpus-heldout/ with a MANIFEST.tsv line. promote refuses a case without a
keep list, with keep lines missing from its log, or with credential-shaped
strings left in it.

Which outputs: by default, commands that look like test runs, builds,
installs and the like (the list the Claude Code hook in integrations/ uses),
with at least --min-lines lines. --all takes any command. Outputs that are
pare's own summaries are replaced by the raw run file they name when it
still exists, and skipped otherwise. Outputs that are mostly lines from the
dev corpora (corpus/, corpus-generated/) are skipped, since pare was tuned
on those. Identical outputs are harvested once.

Proposals never use pare's scoring or vocabulary: they come only from what
the agent did after seeing the output. They lean toward what the agent saw,
which for a saved large output is a 2 KB preview unless it read further, so
they are a draft for review, not labels.

Scrubbing, applied before anything is written: user names in home paths
(/Users/NAME, /home/NAME, C:\\Users\\NAME, and Claude Code's encoded
-Users-NAME- folder names) become dev; email addresses become
user@example.com; your login name, git user.name and user.email, and any
--redact words (a GitHub handle, a company name) become dev; this
machine's host name becomes host; credential-shaped strings (tokens, keys,
password=...) become <redacted>. It cannot catch everything (a colleague's
name in a commit message, customer data in a log, internal host names):
read every log before promoting it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import collect  # noqa: E402  (SECRET_RX, manifest helpers)

HELDOUT = os.path.join(ROOT, "corpus-heldout")
INBOX = os.path.join(HELDOUT, "inbox")
DEV_CORPORA = [os.path.join(ROOT, "corpus"), os.path.join(ROOT, "corpus-generated")]
MAX_PROPOSALS = 20
FOLLOW_EVENTS = 30  # assistant blocks after the output that count as the agent's response to it


def load_hook():
    path = os.path.join(ROOT, "integrations", "claude-code", "pare-hook.py")
    spec = importlib.util.spec_from_file_location("pare_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOK = load_hook()
SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
FILE_READ = re.compile(r"^(cat|sed|head|tail|less|more|grep|rg|ag|find|ls|tree|wc|git (diff|show|log|blame)|diff|jq|awk)\b")
RAW_LINE = re.compile(r"^## raw: (\S+)", re.M)


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------

def transcripts(projects: str) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(projects):
        for f in files:
            if f.endswith(".jsonl"):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def bash_calls(path: str):
    """Yield one dict per Bash call with its output and the agent's next moves."""
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    uses: dict[str, dict] = {}
    for idx, ev in enumerate(events):
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                uses[b.get("id")] = {"command": (b.get("input") or {}).get("command", ""), "timestamp": ev.get("timestamp")}
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                call = uses.pop(b["tool_use_id"])
                yield {
                    "transcript": path,
                    "session": ev.get("sessionId"),
                    "tool_use_id": b["tool_use_id"],
                    "timestamp": call["timestamp"] or ev.get("timestamp"),
                    "cwd": ev.get("cwd"),
                    "command": call["command"],
                    "is_error": bool(b.get("is_error")),
                    "result_text": block_text(b.get("content")),
                    "tool_use_result": ev.get("toolUseResult"),
                    "follow": followup(events, idx),
                }


def followup(events: list[dict], idx: int) -> dict:
    """What the agent wrote and did after this output, until the person's
    next message: its text, and the inputs of its tool calls."""
    texts, inputs, paths = [], [], []
    seen = 0
    for ev in events[idx + 1:]:
        msg = ev.get("message") or {}
        content = msg.get("content")
        if ev.get("type") == "user" and (isinstance(content, str) or (
                isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))):
            break  # the person spoke again
        if msg.get("role") != "assistant" or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                texts.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                inputs.append(json.dumps(inp, ensure_ascii=False))
                for k in ("file_path", "path", "notebook_path"):
                    if isinstance(inp.get(k), str):
                        paths.append(inp[k])
            seen += 1
        if seen >= FOLLOW_EVENTS:
            break
    return {"text": "\n".join(texts), "inputs": "\n".join(inputs), "paths": paths}


# --------------------------------------------------------------------------
# One call -> output, command, exit status
# --------------------------------------------------------------------------

PERSISTED = re.compile(r"Full output saved to: (\S+)")


def full_output(call: dict) -> tuple[str | None, int | None, str]:
    """(output text, exit status, where it came from)."""
    tur = call["tool_use_result"]
    text = call["result_text"]
    exit_code = 0
    if call["is_error"]:
        m = re.match(r"(?:Error: )?Exit code (\d+)\n?", text)
        if m:
            exit_code = int(m.group(1))
            text = text[m.end():]
        else:
            exit_code = None
    saved = None
    if isinstance(tur, dict) and tur.get("persistedOutputPath"):
        saved = tur["persistedOutputPath"]
    else:
        m = PERSISTED.search(text)
        saved = m.group(1) if m else None
    if saved:
        if not os.path.isfile(saved):
            return None, exit_code, "saved output file is gone"
        with open(saved, encoding="utf-8", errors="replace") as f:
            return f.read(), exit_code, "saved"
    if isinstance(tur, dict) and ("stdout" in tur or "stderr" in tur):
        out = tur.get("stdout") or ""
        err = tur.get("stderr") or ""
        return (out + ("\n" if out and err and not out.endswith("\n") else "") + err), exit_code, "transcript"
    return text, exit_code, "transcript"


def pare_inner(command: str) -> str | None:
    """CMD from `[VAR=x] pare [options] -- CMD`, or None."""
    for words in command_words(command):
        if os.path.basename(words[0]) in ("pare", "pare.py") and "--" in words:
            return shlex.join(words[words.index("--") + 1:])
    return None


def unwrap_pare(command: str, output: str) -> tuple[str | None, str | None, str]:
    """A pare-wrapped command's output is pare's summary. Return the inner
    command and the raw run file's text, when the run file still exists."""
    inner = pare_inner(command)
    raw = RAW_LINE.search(output)
    if raw and os.path.isfile(raw.group(1)):
        with open(raw.group(1), encoding="utf-8", errors="replace") as f:
            return inner, f.read(), "pare run file"
    return inner, None, "pare summary without its run file"


def command_words(command: str):
    """The words of each simple command in a compound one, without leading
    VAR=value assignments or a trailing redirection."""
    for seg in SEGMENT_SPLIT.split(command.strip()):
        seg = re.sub(r"\s+\d?>&?\d?\s*\S*$", "", seg).strip()
        m = HOOK.ENV_PREFIX.match(seg)
        rest = m.group(2) if m else seg
        try:
            words = shlex.split(rest)
        except ValueError:
            continue
        if words:
            yield words


def invokes_pare(command: str) -> bool:
    """pare itself, or a script of its benchmark, run as a command: its
    output is derived from pare and says nothing about held-out logs."""
    for words in command_words(command):
        prog = os.path.basename(words[0])
        args = [os.path.basename(w) for w in words[1:3]]
        if prog in ("pare", "pare.py") or (prog.startswith("python") and any(
                a in ("pare.py", "run.py", "ablate.py", "tokens.py", "harvest.py", "collect.py") for a in args)):
            return True
    return False


def is_noisy(command: str) -> bool:
    """Does any part of the command look like a test run, build or install?"""
    for words in command_words(command):
        norm = " ".join([os.path.basename(words[0])] + words[1:])
        if FILE_READ.match(norm):
            continue
        if any(re.match(rx, norm) for rx in HOOK.WRAP):
            return True
    return False


# --------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------

HOME_PATH = re.compile(r"(?<![\w.-])(/Users/|/home/|[A-Za-z]:\\Users\\)([^/\\\s:'\"]+)")
# Claude Code names project folders after the path with / turned into -:
# /Users/NAME/dev/x becomes -Users-NAME-dev-x.
ENCODED_HOME = re.compile(r"(?<=-)(Users|home)-([A-Za-z0-9._]+)(?=-)")
# Account names too common to redact as words: replacing them would rewrite
# ordinary log text.
GENERIC_NAMES = {"root", "user", "users", "dev", "admin", "runner", "ubuntu", "ec2-user", "vagrant", "build", "ci", "test",
                 "claude", "noreply@anthropic.com"}  # the identity of agent-made commits, not a person


def identity_words(extra: list[str] | None = None) -> list[str]:
    """Words that identify the person running this: the login name and git's
    user.name and user.email (from the global config), plus --redact words.
    Longest first, so a full name is replaced before its parts."""
    words = set(extra or [])
    for env in ("USER", "LOGNAME", "USERNAME"):
        if os.environ.get(env):
            words.add(os.environ[env])
    try:
        import getpass
        words.add(getpass.getuser())
    except Exception:  # noqa: BLE001
        pass
    for key in ("user.name", "user.email"):
        try:
            import subprocess
            p = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True)
            value = p.stdout.strip()
        except OSError:
            value = ""
        if value:
            words.add(value)
            if key == "user.name":
                words.update(part for part in value.split() if len(part) >= 4)
    words = {w for w in words if len(w) >= 3 and w.lower() not in GENERIC_NAMES}
    return sorted(words, key=len, reverse=True)
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")


IDENTITY: list[str] = []  # set in main() from identity_words()


def scrub(text: str) -> tuple[str, dict]:
    counts = {"home paths": 0, "emails": 0, "identity": 0, "host name": 0, "credentials": 0}

    def home(m):
        if m.group(2) == "dev":
            return m.group(0)
        counts["home paths"] += 1
        return m.group(1) + "dev"

    def encoded_home(m):
        if m.group(2) == "dev":
            return m.group(0)
        counts["home paths"] += 1
        return m.group(1) + "-dev"

    text = HOME_PATH.sub(home, text)
    text = ENCODED_HOME.sub(encoded_home, text)

    def email(m):
        if m.group(0) == "user@example.com":
            return m.group(0)
        counts["emails"] += 1
        return "user@example.com"

    text = EMAIL.sub(email, text)
    for word in IDENTITY:
        text, n = re.subn(rf"(?i)(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", "dev", text)
        counts["identity"] += n
    host = socket.gethostname().split(".")[0]
    if len(host) >= 4 and host.lower() not in ("localhost", "host"):
        text, n = re.subn(rf"\b{re.escape(host)}\b", "host", text)
        counts["host name"] = n

    def secret(m):
        counts["credentials"] += 1
        return "<redacted>"

    text = collect.SECRET_RX.sub(secret, text)
    return text, {k: v for k, v in counts.items() if v}


# --------------------------------------------------------------------------
# Proposed keep lines
# --------------------------------------------------------------------------

LOCATION = re.compile(r"([\w./-]+\.[A-Za-z]{1,8})(?:[:(](\d+)|\", line (\d+)| line (\d+))")
TEST_ID = re.compile(r"[\w./-]+::[\w\[\]./-]+|\btest_\w+")
QUOTED = re.compile(r"`([^`\n]{15,})`|\"([^\"\n]{15,})\"|'([^'\n]{15,})'")


def propose(output: str, follow: dict) -> list[tuple[str, str]]:
    """(line, reason) for output lines the agent's next moves point at."""
    evidence = follow["text"] + "\n" + follow["inputs"]
    if not evidence.strip():
        return []
    fragments = [next(g for g in m.groups() if g).strip() for m in QUOTED.finditer(follow["text"])]
    touched = {os.path.basename(p) for p in follow["paths"]}
    out, seen = [], set()
    for raw in output.splitlines():
        line = raw.strip()
        if len(line) < 12 or len(line) > 400 or line in seen:
            continue
        reason = None
        if len(line) >= 20 and line in evidence:
            reason = "quoted by the agent"
        elif any(len(fr) >= 15 and fr in line for fr in fragments):
            reason = "quoted in part by the agent"
        else:
            for m in LOCATION.finditer(line):
                f, n = m.group(1), next((g for g in m.groups()[1:] if g), None)
                base = os.path.basename(f)
                if n and (f"{base}:{n}" in evidence or f"{f}:{n}" in evidence):
                    reason = f"location {base}:{n} used next"
                    break
                if n and base in touched:
                    reason = f"agent opened {base} next"
                    break
            if not reason:
                for t in TEST_ID.findall(line):
                    if len(t) >= 8 and t in follow["inputs"]:
                        reason = f"test {t} used next"
                        break
        if reason:
            seen.add(line)
            out.append((line, reason))
            if len(out) >= MAX_PROPOSALS:
                break
    return out


# --------------------------------------------------------------------------
# scan / promote
# --------------------------------------------------------------------------

def dev_lines() -> set[str]:
    lines: set[str] = set()
    for d in DEV_CORPORA:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".log"):
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    lines.update(ln.strip() for ln in fh if len(ln.strip()) >= 8)
    return lines


def known_hashes() -> set[str]:
    seen = set()
    for d in (INBOX_DIR, HELDOUT_DIR):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".json"):
                try:
                    seen.add(json.load(open(os.path.join(d, f)))["sha256"])
                except (OSError, ValueError, KeyError):
                    pass
        manifest = os.path.join(d, collect.MANIFEST)
        if os.path.exists(manifest):
            for row in open(manifest, encoding="utf-8").read().splitlines()[1:]:
                parts = row.split("\t")
                if len(parts) > 4:
                    seen.add(parts[4])
    return seen


def case_name(command: str, timestamp: str | None, tool_use_id: str) -> str:
    words = [w for w in (shlex.split(command, posix=True) if command else []) if not w.startswith("-") and "=" not in w]
    words = [os.path.basename(w) for w in words[:2]]
    base = "-".join(re.sub(r"[^A-Za-z0-9]+", "", w) for w in words if re.sub(r"[^A-Za-z0-9]+", "", w)) or "case"
    day = (timestamp or "")[:10].replace("-", "") or dt.date.today().strftime("%Y%m%d")
    return f"{base[:40]}-{day}-{re.sub(r'[^A-Za-z0-9]', '', tool_use_id)[-6:]}"


def cmd_scan(args) -> int:
    files = args.transcript or transcripts(args.projects)
    if not files:
        print(f"no transcripts under {args.projects}")
        return 0
    os.makedirs(INBOX_DIR, exist_ok=True)
    dev = dev_lines()
    seen = known_hashes()
    stats: dict[str, int] = {}
    written = []

    def skip(why):
        stats[why] = stats.get(why, 0) + 1

    for path in files:
        for call in bash_calls(path):
            command = call["command"]
            output, exit_code, origin = full_output(call)
            if output is None:
                skip(origin)
                continue
            if pare_inner(command) or output.lstrip().startswith("# pare"):
                inner, raw, origin = unwrap_pare(command, output)
                if raw is None:
                    skip("pare summary, run file gone")
                    continue
                command, output = inner or command, raw
            elif invokes_pare(command):
                skip("other pare invocation")
                continue
            nlines = output.count("\n") + (0 if output.endswith("\n") or not output else 1)
            if nlines < args.min_lines:
                skip(f"under {args.min_lines} lines")
                continue
            if not args.all and not is_noisy(command):
                skip("not a build/test-like command")
                continue
            distinct = {ln.strip() for ln in output.splitlines() if len(ln.strip()) >= 8}
            if distinct and len(distinct & dev) >= 0.5 * len(distinct):
                skip("mostly dev-corpus lines")
                continue
            digest = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()[:16]
            if digest in seen:
                skip("already harvested")
                continue
            seen.add(digest)
            clean, scrubbed = scrub(output)
            clean_cmd, cmd_scrubbed = scrub(command)
            for k, v in cmd_scrubbed.items():
                scrubbed[k] = scrubbed.get(k, 0) + v
            proposals = propose(clean, call["follow"])
            name = case_name(clean_cmd, call["timestamp"], call["tool_use_id"])
            written.append((name, nlines, exit_code, len(proposals), clean_cmd, scrubbed))
            if args.dry_run:
                continue
            base = os.path.join(INBOX_DIR, name)
            with open(base + ".log", "w", encoding="utf-8") as f:
                f.write(clean if clean.endswith("\n") else clean + "\n")
            with open(base + ".cmd", "w", encoding="utf-8") as f:
                f.write(clean_cmd.strip() + "\n")
            with open(base + ".keep.proposed", "w", encoding="utf-8") as f:
                f.writelines(line + "\n" for line, _ in proposals)
            meta = {
                "sha256": digest, "transcript": scrub(call["transcript"])[0], "session": call["session"],
                "tool_use_id": call["tool_use_id"], "timestamp": call["timestamp"], "exit": exit_code,
                "lines": nlines, "output_from": origin, "scrubbed": scrubbed,
                "proposed": [{"line": line, "why": why} for line, why in proposals],
                "harvested": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pare": collect.pare_version(), "commit": collect.git_commit(),
            }
            with open(base + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
                f.write("\n")

    total = sum(stats.values()) + len(written)
    print(f"{len(files)} transcript(s), {total} Bash calls; {len(written)} candidate(s)"
          + (" (dry run, nothing written)" if args.dry_run else f" written to {INBOX_DIR}"))
    for name, nlines, code, nprop, cmd, scrubbed in written:
        note = ", ".join(f"{v} {k}" for k, v in scrubbed.items())
        print(f"  {name:<44} {nlines:>6} lines  exit {code if code is not None else '?':<3} {nprop:>2} proposed"
              + (f"  scrubbed: {note}" if note else "") + f"\n      {cmd[:100]}")
    if stats:
        print("skipped: " + "; ".join(f"{v} {k}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1])))
    if written and not args.dry_run:
        print("Next: read each log, write NAME.keep from it (NAME.keep.proposed is a draft), then promote.")
    return 0


def cmd_promote(args) -> int:
    rc = 0
    for name in args.names:
        base = os.path.join(INBOX_DIR, name)
        if not os.path.exists(base + ".log"):
            print(f"{name}: not in {INBOX_DIR}")
            rc = 1
            continue
        keep = []
        if os.path.exists(base + ".keep"):
            keep = [ln.strip() for ln in open(base + ".keep", encoding="utf-8") if ln.strip()]
        text = open(base + ".log", encoding="utf-8", errors="replace").read()
        problems = []
        if not keep:
            problems.append("no keep list (write NAME.keep from the log; NAME.keep.proposed is only a draft)")
        missing = [k for k in keep if k not in text]
        if missing:
            problems.append(f"{len(missing)} keep line(s) not in the log, e.g. {missing[0][:80]!r}")
        secrets = [i for i, ln in enumerate(text.splitlines(), 1) if collect.SECRET_RX.search(ln)]
        if secrets and not args.force:
            problems.append(f"credential-shaped strings on lines {secrets[:10]} (redact, or --force)")
        dest = os.path.join(HELDOUT_DIR, name)
        if os.path.exists(dest + ".log"):
            problems.append("a case with this name is already in the corpus")
        if problems:
            print(f"{name}: not promoted: " + "; ".join(problems))
            rc = 1
            continue
        meta = json.load(open(base + ".json", encoding="utf-8"))
        proposed = {p["line"] for p in meta.get("proposed", [])}
        labels = "reviewed proposals" if proposed and set(keep) & proposed else "manual"
        for ext in (".log", ".cmd", ".keep"):
            shutil.move(base + ext, dest + ext)
        cmd = open(dest + ".cmd", encoding="utf-8").read().strip()
        collect.append_manifest(HELDOUT_DIR, [
            name, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), meta.get("pare", "?"),
            meta.get("commit", "?"), meta["sha256"], str(meta.get("lines", "")), str(len(text.encode())),
            str(meta.get("exit", "")), cmd, "transcript", labels])
        archive = os.path.join(INBOX_DIR, "promoted")
        os.makedirs(archive, exist_ok=True)
        for ext in (".json", ".keep.proposed"):
            if os.path.exists(base + ext):
                shutil.move(base + ext, os.path.join(archive, name + ext))
        print(f"{name}: promoted ({len(keep)} keep lines, labels: {labels})")
    return rc


INBOX_DIR = INBOX
HELDOUT_DIR = HELDOUT


def main() -> int:
    global INBOX_DIR, HELDOUT_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=HELDOUT, help="held-out corpus directory (default corpus-heldout/); "
                                                     "candidates go to its inbox/ subfolder")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("scan", help="find candidate cases in transcripts")
    p.add_argument("--projects", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"),
                   help="Claude Code projects directory (default ~/.claude/projects)")
    p.add_argument("--transcript", action="append", help="a single transcript .jsonl (repeatable)")
    p.add_argument("--min-lines", type=int, default=80)
    p.add_argument("--all", action="store_true", help="any command, not only build/test-like ones")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--redact", action="append", default=[], metavar="WORD",
                   help="also replace WORD (e.g. a GitHub handle or company name) with dev; repeatable. "
                        "Your login name and git user.name/user.email are added automatically")
    p.add_argument("--no-identity", action="store_true",
                   help="do not add the login name and git identity to the redaction list")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("promote", help="move reviewed cases into the corpus")
    p.add_argument("names", nargs="+")
    p.add_argument("--force", action="store_true", help="promote even with credential-shaped strings left")
    p.set_defaults(func=cmd_promote)
    args = ap.parse_args()
    HELDOUT_DIR = args.corpus
    INBOX_DIR = os.path.join(args.corpus, "inbox")
    if args.action == "scan":
        IDENTITY[:] = list(args.redact) if args.no_identity else identity_words(args.redact)
        if IDENTITY:
            print("redacting: " + ", ".join(IDENTITY))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
