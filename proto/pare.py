#!/usr/bin/env python3
"""pare - summarize noisy command output (tests, builds, logs) to a token budget.

Pipeline
    stdin -> tee to run file -> lines -> records -> mask -> drain3 templates
          -> salience -> budget allocation -> summary on stdout

Usage
    noisy-command 2>&1 | pare [--budget 2000]
    pare --only T3,T7 < run-file        # raw lines of those templates
    pare --except T4 < run-file         # raw lines of everything but T4

Output is deterministic for a given input and flags: template ids (T1, T2,
...) are numbered in order of first appearance, and the run file name is a
content hash, so a later `pare --only T3 < runfile` sees the same ids.

Install: `uv run proto/pare.py` picks up the dependency block below, or
`python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt`.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["drain3>=0.9.11"]
# ///

from __future__ import annotations

import argparse
import hashlib
import heapq
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field

logging.getLogger("drain3").setLevel(logging.ERROR)

try:
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig
except ModuleNotFoundError:
    sys.exit(
        "pare: the 'drain3' package is not installed for this Python.\n"
        "  run it with uv (installs drain3 into a cached env):  uv run proto/pare.py\n"
        "  or in a venv:  python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt\n"
        "                 .venv/bin/python proto/pare.py"
    )

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# Lines and records
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# Lines that continue the previous record regardless of indentation.
CONT_RE = re.compile(
    r"^\s+at\s"  # JS/Java stack frame
    r"|^\s*File \"[^\"]+\", line \d+"  # Python frame
    r"|^\s*\^+\s*$"  # caret marker
    r"|^\s*~+\s*$"  # tilde underline
    r"|^\s*\d*\s*\|(?:\s|$)"  # rustc/gcc gutter lines
    r"|^\s*-->\s"  # rustc location
    r"|^\s*= (?:note|help|warning):"  # rustc notes
    r"|^\s*::: "  # rustc secondary location
    r"|^Caused by:"  # Java/Rust cause chain
    r"|^\s*\.\.\. \d+ more"  # Java elided frames
    r"|^\s*(?:Expected|Received|Actual|Got|Want|Diff):"  # assertion detail
    r"|^\s*[+-]{3}\s"  # diff headers
    r"|^\s*@@ "  # diff hunk
)

MAX_RECORD_LINES = 400


@dataclass
class Line:
    no: int  # 1-based line number in the input
    text: str  # ANSI-stripped, \r-resolved, right-stripped
    indent: int
    record: int = -1  # record index
    depth: int = 0  # 0 for head line, >0 for continuation


@dataclass
class Record:
    idx: int
    start: int  # 0-based index of head line
    end: int  # exclusive
    template: int = -1  # template index (0-based), -1 before clustering


@dataclass
class Template:
    idx: int  # 0-based; shown as T{idx+1}
    text: str
    records: list = field(default_factory=list)
    lines: int = 0
    salience: float = 0.0
    vocab: float = 0.0
    marker: str = "."

    @property
    def tid(self) -> str:
        return f"T{self.idx + 1}"


def clean_line(raw: str) -> str:
    # A terminal shows whatever followed the last carriage return.
    if "\r" in raw:
        parts = [p for p in raw.split("\r") if p != ""]
        raw = parts[-1] if parts else ""
    raw = ANSI_RE.sub("", raw)
    return raw.rstrip()


def indent_of(text: str) -> int:
    n = 0
    for ch in text:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def split_lines(data: bytes) -> list[Line]:
    text = data.decode("utf-8", errors="replace")
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    lines = []
    for i, raw in enumerate(raw_lines):
        t = clean_line(raw)
        lines.append(Line(no=i + 1, text=t, indent=indent_of(t)))
    return lines


def segment(lines: list[Line]) -> list[Record]:
    """Join continuation lines (deeper indentation or known frame shapes) to
    the preceding head line. Blank lines end a record and belong to none."""
    records: list[Record] = []
    cur: Record | None = None
    pending_blank: Line | None = None  # a single blank line may sit inside a block
    for i, ln in enumerate(lines):
        if ln.text.strip() == "":
            if pending_blank is not None or cur is None:
                cur = None
            pending_blank = ln if cur is not None else None
            continue
        head_indent = lines[cur.start].indent if cur else None
        is_cont = (
            cur is not None
            and (cur.end - cur.start) < MAX_RECORD_LINES
            and (ln.indent > head_indent or CONT_RE.match(ln.text))
        )
        if is_cont and cur is not None:
            if pending_blank is not None:
                pending_blank.record = cur.idx
                pending_blank.depth = 1
            cur.end = i + 1
            ln.record = cur.idx
            ln.depth = max(1, (ln.indent - head_indent + 1) // 2)
        else:
            if pending_blank is not None:
                cur = None
            pending_blank = None
            cur = Record(idx=len(records), start=i, end=i + 1)
            records.append(cur)
            ln.record = cur.idx
            ln.depth = 0
        pending_blank = None
    return records


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------

MASKS = [
    ("TS", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")),
    ("DATE", re.compile(
        r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
        r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),? (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2}\b"
        r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2},? \d{4}\b")),
    ("TIME", re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b")),
    ("UUID", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("IP", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")),
    ("URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+")),
    ("HEX", re.compile(r"\b0[xX][0-9a-fA-F]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b")),
    ("VER", re.compile(r"\bv?\d+\.\d+(?:\.\d+)+(?:[-+][\w.]+)?\b")),
    ("PATH", re.compile(
        r"(?=[\w./+@%~-]*[A-Za-z])"  # must contain a letter (so 1/30 is not a path)
        r"(?:(?:~|\.{1,2})?(?:/[\w.+@%~-]+)+/?"  # absolute or ./relative
        r"|\b[\w.+-]+(?:/[\w.+@%~-]+)+\b)")),  # a/b/c
    ("NUM", re.compile(
        r"(?<![\w.])[-+]?\d+(?:[.,]\d+)*"
        r"(?:\s?(?:ms|us|µs|ns|secs?|s|mins?|m|h|[KMGT]i?B|[kKMG]?b|B|%|px|x))?(?![\w.])")),
    ("WORDNUM", re.compile(r"\b[A-Za-z_]+\d+[A-Za-z0-9_]*\b")),  # worker3, job42 -> keep word, mask digits
]


def mask(text: str) -> str:
    t = text.strip()
    for name, rx in MASKS:
        if name == "WORDNUM":
            t = rx.sub(lambda m: re.sub(r"\d+", "<N>", m.group(0)), t)
        else:
            t = rx.sub(f"<{name}>", t)
    return t


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------

DRAIN_MAX_TOKENS = 40


def cluster(lines: list[Line], records: list[Record]) -> list[Template]:
    cfg = TemplateMinerConfig()
    cfg.masking_instructions = []
    cfg.profiling_enabled = False
    cfg.drain_sim_th = 0.5  # 0.4 merges lines that share only a timestamp+level prefix
    cfg.drain_depth = 4
    cfg.drain_max_children = 100
    cfg.parametrize_numeric_tokens = True
    miner = TemplateMiner(None, cfg)

    cluster_to_tpl: dict[int, Template] = {}
    templates: list[Template] = []
    for rec in records:
        head = lines[rec.start].text
        msg = " ".join(mask(head).split()[:DRAIN_MAX_TOKENS]) or "<EMPTY>"
        res = miner.add_log_message(msg)
        cid = res["cluster_id"]
        tpl = cluster_to_tpl.get(cid)
        if tpl is None:
            tpl = Template(idx=len(templates), text=res["template_mined"])
            templates.append(tpl)
            cluster_to_tpl[cid] = tpl
        rec.template = tpl.idx
        tpl.records.append(rec.idx)
        tpl.lines += rec.end - rec.start
    # Templates evolve as members join; take the final form.
    for cid, tpl in cluster_to_tpl.items():
        c = miner.drain.id_to_cluster.get(cid)
        if c is not None:
            tpl.text = c.get_template()
    return templates


# --------------------------------------------------------------------------
# Salience
# --------------------------------------------------------------------------

# The vocabulary is the one part of the pipeline that is a prior rather than
# learned from the input. It is organised as classes of entries; each entry is
# a word or phrase (matched whole, case-insensitively, not inside another
# word) or a /regex/ used as written. A user file (--vocab, $PARE_VOCAB)
# extends or replaces any class; --dump-vocab prints these defaults in that
# format.

VOCAB_CLASSES = {
    # class: (weight, description)
    "error": (3.0, "something went wrong"),
    "warn": (1.5, "worth a look"),
    "summary": (1.0, "totals, section rules, final verdicts"),
    "frame": (1.0, "source locations and stack frames"),
    "pass": (-1.0, "passing-test markers (ignored when an error word is also present)"),
    "noise": (-0.75, "progress and chatter (ignored when an error word is also present)"),
    "strong-error": (0.0, "decides whether a record body counts as an error; no weight"),
}

DEFAULT_VOCAB: dict[str, list[str]] = {
    "error": [
        "error", "errors", "err", "fatal", "panic", "panicked", "exception", "traceback", "assert",
        "assertion", "fail", "fails", "failed", "failure", "failures", "failing", "denied", "refused",
        "unreachable", "timeout", "timed out", "cannot", "can't", "could not", "couldn't", "unable",
        "not found", "no such", "missing", "invalid", "unexpected", "segfault", "segmentation fault",
        "abort", "aborted", "killed", "oom", "out of memory", "critical", "crash", "crashed", "rejected",
        "unhandled", "uncaught", "undefined", "null pointer", "stack overflow", "overflow", "exit code",
        "exited with", "non-zero", "conflict", "corrupt", "corrupted", "broken", "mismatch",
        "does not match", "not ok", "errno", "exc_info", "stacktrace", "stack trace",
        r"/[A-Z]\w*(?:Error|Exception|Fault|Panic)\b/",  # CamelCase exception names
        "/✗|✘|❌/", r"/^\s*E\s{2,}/", r"/FAIL(?:ED|URE|S)?\b/",
    ],
    "warn": [
        "warn", "warning", "warnings", "deprecated", "deprecation", "retry", "retrying", "retried",
        "slow", "skipped", "skip", "skipping", "xfail", "xpass", "flaky", "unstable", "ignored",
        "pending", "todo", "fixme", "leak", "stale", "outdated", "insecure", "unused",
        "unreachable code", r"/(?i)(?<![a-z])vulnerab\w*/", "/⚠/",
    ],
    "summary": [
        "passed", "test", "tests", "total", "summary", "finished", "completed", "done", "built",
        "compiled", "elapsed", "duration", "exit status", "result", "results", "success", "succeeded",
        "complete", "coverage", "report", r"/(?i)(?<![a-z])ran \d+/",
        r"/^\s*={3,}/", r"/^\s*-{3,}/", r"/^\s*#{1,3}\s/", r"/^\s*\*{3,}/",
    ],
    "frame": [
        r"/^\s*at\s/", r'/^\s*File "/', r"/^\s*-->/", r"/^\s*>\s*\d+\s*\|/",
        r"/[\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|go|rs|c|cc|cpp|h|hpp|java|kt|rb|cs|swift|php|scala|ex|exs)[:(]\d+/",
    ],
    "pass": ["/✓|✔/", r"/\bPASS(?:ED)?\b/", r"/^\s*ok\b/", r"/\bok\s*$/"],
    "noise": [
        "downloading", "downloaded", "fetching", "fetched", "compiling", "installing", "installed",
        "resolving", "resolved", "extracting", "unpacking", "progress", "collecting", "checking",
        "building", "linking", "caching", "cached", "preparing", "processing", "pulling", "pushing",
        "verbose", "debug", "trace", "info", "get", "head", "heartbeat", "ping", "pong", "poll",
        "polling", "tick",
        r"/\d+%/", r"/\.{3,}/", "/[▏▎▍▌▋▊▉█░▒▓]/", r"/\b(?:200|204|304) (?:OK|No Content|Not Modified)\b/",
    ],
    "strong-error": [
        r"/(?i)\b\w*(?:error|exception)\b/", r"/(?i)\bpanic/", r"/(?i)\btraceback\b/",
        r"/(?i)\bassert\w*\b/", r"/(?i)\bfatal\b/", r"/(?i)\bfail(?:ed|ure|s)?\b/",
        r"/^\s*E\s{2,}/", "/✕|✗|FAIL/",
    ],
}


_GLOBAL_FLAGS = re.compile(r"^\(\?([aimsux]+)\)")


def _entry_regex(entry: str) -> str:
    """One entry as a regex fragment that can be joined with others by '|'.
    Leading global flags like (?i) are rewritten to a scoped group (?i:...),
    since Python only allows global flags at the start of a whole pattern."""
    if len(entry) >= 2 and entry.startswith("/") and entry.endswith("/"):
        rx = entry[1:-1]
        m = _GLOBAL_FLAGS.match(rx)
        if m:
            rx = f"(?{m.group(1)}:{rx[m.end():]})"
        return rx
    # A word or phrase: whole-word, case-insensitive (the boundary check is
    # inside the group, so it is case-insensitive too: not inside a word).
    return r"(?i:(?<![a-z])" + re.escape(entry) + r"(?![a-z]))"


class Vocab:
    """Compiled vocabulary: one regex per class, or None for an empty class."""

    def __init__(self, entries: dict[str, list[str]]):
        self.entries = entries
        self.rx: dict[str, re.Pattern | None] = {}
        for cls in VOCAB_CLASSES:
            items = entries.get(cls, [])
            self.rx[cls] = re.compile("|".join(f"(?:{_entry_regex(e)})" for e in items)) if items else None

    def has(self, cls: str, text: str) -> bool:
        rx = self.rx[cls]
        return bool(rx and rx.search(text))


def parse_vocab(text: str, base: dict[str, list[str]], source: str = "vocab") -> dict[str, list[str]]:
    """Parse a vocab file on top of `base`.

        # comment
        [error]            entries below extend the built-in class
        [error replace]    entries below replace it
        timeout            a word or phrase (whole-word, case-insensitive)
        /(?i)\\bfehler\\b/ a regex, used as written
    """
    out = {cls: list(v) for cls, v in base.items()}
    cls = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            parts = line[1:-1].split()
            if not parts or parts[0] not in VOCAB_CLASSES or len(parts) > 2 or (len(parts) == 2 and parts[1] != "replace"):
                sys.exit(f"pare: {source}:{n}: expected [class] or [class replace] with class in "
                         f"{', '.join(VOCAB_CLASSES)}; got {line!r}")
            cls = parts[0]
            if len(parts) == 2:
                out[cls] = []
            continue
        if cls is None:
            sys.exit(f"pare: {source}:{n}: entry {line!r} appears before any [class] header")
        if line.startswith("/") and line.endswith("/") and len(line) >= 2:
            try:
                re.compile(line[1:-1])
            except re.error as e:
                sys.exit(f"pare: {source}:{n}: bad regex {line!r}: {e}")
        out[cls].append(line)
    return out


def dump_vocab(entries: dict[str, list[str]]) -> str:
    lines = [
        "# pare vocabulary. Edit and pass with --vocab FILE (or $PARE_VOCAB).",
        "# A plain line is a word or phrase, matched whole and case-insensitively.",
        "# A /.../ line is a regular expression used as written.",
        "# [class] adds entries to the built-in class; [class replace] discards the",
        "# built-in entries first. This dump uses 'replace' so it reproduces the",
        "# defaults exactly; switch a header to plain [class] to keep the defaults",
        "# and add to them.",
    ]
    for cls, (weight, desc) in VOCAB_CLASSES.items():
        lines.append("")
        lines.append(f"# {desc}" + (f" (weight {weight:+g})" if weight else ""))
        lines.append(f"[{cls} replace]")
        lines.extend(entries.get(cls, []))
    return "\n".join(lines) + "\n"


VOCAB = Vocab(DEFAULT_VOCAB)


def vocab_score(text: str) -> float:
    s = 0.0
    err = VOCAB.has("error", text)
    if err:
        s += VOCAB_CLASSES["error"][0]
    if VOCAB.has("warn", text):
        s += VOCAB_CLASSES["warn"][0]
    if VOCAB.has("summary", text):
        s += VOCAB_CLASSES["summary"][0]
    if VOCAB.has("frame", text):
        s += VOCAB_CLASSES["frame"][0]
    if not err and VOCAB.has("pass", text):
        s += VOCAB_CLASSES["pass"][0]
    if not err and VOCAB.has("noise", text):
        s += VOCAB_CLASSES["noise"][0]
    if len(text.strip()) < 4:
        s -= 1.0
    return s


def rarity_of(tpl: Template) -> float:
    return 1.0 / (1.0 + math.log2(len(tpl.records)))


def score_templates(lines: list[Line], records: list[Record], templates: list[Template]) -> None:
    n = max(1, len(records))
    for tpl in templates:
        # Vocabulary: the template text, plus one point if the exemplar's body
        # carries a strong error signal (a traceback under a bland head line).
        head_vocab = vocab_score(tpl.text)
        body_err = 0.0
        rec = records[tpl.records[0]]
        for ln in lines[rec.start + 1 : rec.end]:
            if VOCAB.has("strong-error", ln.text):
                body_err = 1.0
                break
        vocab = head_vocab + body_err
        count = len(tpl.records)
        rarity = rarity_of(tpl)
        first = tpl.records[0] / n
        last = tpl.records[-1] / n
        position = 0.0
        if last >= 0.9:
            position += 1.0 if first >= 0.9 else 0.5
        if first <= 0.05:
            position += 0.4
        avg_cont = tpl.lines / count - 1.0
        block = 0.75 if avg_cont >= 2 else 0.0
        tpl.vocab = vocab
        tpl.salience = vocab + 2.0 * rarity + position + block
        if (VOCAB.has("error", tpl.text) and not VOCAB.has("pass", tpl.text)) or body_err:
            tpl.marker = "E"
        elif VOCAB.has("warn", tpl.text):
            tpl.marker = "W"
        elif VOCAB.has("summary", tpl.text):
            tpl.marker = "S"
        else:
            tpl.marker = "."


def line_score(ln: Line, tpl: Template, head: Line, total: int) -> float:
    """Salience of one raw line for the fill phase: its own vocabulary, a share
    of its record head's vocabulary, the rarity of its template, and a bonus
    for sitting at the end of the output."""
    s = vocab_score(ln.text)
    if ln.depth > 0:
        s -= 0.25 * min(ln.depth, 4)
        if not (VOCAB.has("pass", ln.text) and not VOCAB.has("error", ln.text)):
            s += 0.6 * max(0.0, vocab_score(head.text))
    s += rarity_of(tpl)
    if total and ln.no / total >= 0.9:
        s += 0.5
    return s


def exemplar_body(lines: list[Line], rec: Record, limit: int) -> list[Line]:
    """The most salient continuation lines of a record, in original order."""
    body = [b for b in lines[rec.start + 1 : rec.end] if b.text.strip()]
    ranked = sorted(body, key=lambda b: (-vocab_score(b.text), b.no))[:limit]
    return sorted(ranked, key=lambda b: b.no)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


EXEMPLAR_BODY = 10  # continuation lines shown for an error/warning exemplar
FILL_PENALTY = 1.5  # divisor growth per already-shown line of the same masked shape
FILL_FLOOR = 1.0  # lines scoring below this never pad the output


def tokens(nbytes: int) -> int:
    return math.ceil(nbytes / 4)


def fmt_int(n: int) -> str:
    return f"{n:,}"


def clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + f"…(+{len(text) - width + 1}c)"


def fmt_line(ln: Line, width: int) -> str:
    return f"{ln.no:>6}│ {clip(ln.text, width)}"


def render_ranges(lines: list[Line], nos: list[int], width: int) -> list[str]:
    """Render line numbers in order with gap markers between non-adjacent runs."""
    out = []
    prev = None
    for no in nos:
        if prev is not None and no != prev + 1:
            gap = no - prev - 1
            out.append(f"      ┆ ({fmt_int(gap)} line{'s' if gap != 1 else ''})")
        out.append(fmt_line(lines[no - 1], width))
        prev = no
    return out


def nbytes(parts: list[str]) -> int:
    return sum(len(p.encode("utf-8")) + 1 for p in parts)


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------


def summarize(
    lines: list[Line],
    records: list[Record],
    templates: list[Template],
    *,
    budget: int,
    head_n: int,
    tail_n: int,
    width: int,
    max_templates: int = 0,
    run_path: str | None,
    in_bytes: int,
) -> str:
    budget_bytes = budget * 4
    total = len(lines)
    blank = sum(1 for ln in lines if ln.text.strip() == "")
    by_sal = sorted(templates, key=lambda t: (-t.salience, t.idx))

    def header(out_bytes: int) -> str:
        return (
            f"# pare: {fmt_int(total)} lines, {fmt_int(in_bytes)} B (~{fmt_int(tokens(in_bytes))} tok)"
            f" → ~{fmt_int(tokens(out_bytes))} tok (budget {fmt_int(budget)}) · {len(templates)} templates"
        )

    run_line = f"## raw: {run_path}" if run_path else "## raw: (not saved)"
    run_hint = "   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw"

    # Small input: never make it bigger. Pass it through verbatim if it fits.
    verbatim = [ln.text for ln in lines] + [run_line]
    verbatim_bytes = nbytes(verbatim) + 60
    if verbatim_bytes <= budget_bytes:
        head_v = f"# pare: {fmt_int(total)} lines (~{fmt_int(tokens(in_bytes))} tok) fit budget {fmt_int(budget)}; verbatim"
        return "\n".join([head_v] + verbatim) + "\n"

    # Fixed overhead: header + run lines + section titles + ledger reserve.
    ledger_reserve = min(400, max(120, budget_bytes // 8))
    fixed = nbytes([header(budget_bytes), run_line, run_hint, "## head", "## templates (by salience)",
                    "## selected lines", "## tail"]) + ledger_reserve
    avail = max(0, budget_bytes - fixed)

    shown: set[int] = set()  # line numbers already displayed

    # Head / tail windows: at most 30% of the budget together.
    head_nos = [ln.no for ln in lines[:head_n]]
    tail_nos = [ln.no for ln in lines[max(head_n, total - tail_n):]]
    while head_nos or tail_nos:
        cost = nbytes(render_ranges(lines, head_nos, width)) + nbytes(render_ranges(lines, tail_nos, width))
        if cost <= avail * 0.3:
            break
        if len(tail_nos) >= len(head_nos) and tail_nos:
            tail_nos.pop(0)
        elif head_nos:
            head_nos.pop()
    head_block = render_ranges(lines, head_nos, width)
    tail_block = render_ranges(lines, tail_nos, width)
    shown.update(head_nos)
    shown.update(tail_nos)
    avail -= nbytes(head_block) + nbytes(tail_block)

    # Template table: every template once, in salience order, up to 60% of what
    # is left. Error/warning templates whose exemplar is a multi-line record
    # (a stack trace, a compiler diagnostic) show the body of that exemplar.
    # Each entry is (template idx, rendered lines, body line numbers shown).
    tpl_entries: list[tuple[int, list[str], list[int]]] = []
    tpl_budget = avail * 0.6
    taken: dict[str, int] = {}  # masked text -> lines already shown with that shape

    def tpl_entry(tpl: Template, body_limit: int) -> tuple[list[str], list[int]]:
        rec = records[tpl.records[0]]
        ex = lines[rec.start]
        count = len(tpl.records)
        extra = f" ({fmt_int(tpl.lines)} lines)" if tpl.lines != count else ""
        if count == 1:
            entry = [f"{tpl.tid:<4} ×{count}{extra} {tpl.marker} L{ex.no}: {clip(ex.text.strip(), width)}"]
        else:
            entry = [
                f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {clip(tpl.text, width)}",
                f"     e.g. L{ex.no}: {clip(ex.text.strip(), width)}",
            ]
        body_nos: list[int] = []
        if tpl.marker in ("E", "W") and rec.end - rec.start > 1 and body_limit > 0:
            body = exemplar_body(lines, rec, body_limit)
            body_nos = [b.no for b in body]
            entry.extend(render_ranges(lines, body_nos, width))
            rest = sum(1 for b in lines[rec.start + 1 : rec.end] if b.text.strip()) - len(body)
            if rest > 0:
                entry.append(f"      ┆ (+{fmt_int(rest)} more lines in this record)")
        return entry, body_nos

    for tpl in by_sal:
        if max_templates and len(tpl_entries) >= max_templates:
            break
        entry, body_nos = tpl_entry(tpl, EXEMPLAR_BODY)
        cost = nbytes(entry)
        if cost > tpl_budget:
            if tpl_entries:
                continue
            # The first template always gets a line; shrink its body to fit.
            limit = len(body_nos)
            while limit > 0 and cost > tpl_budget:
                limit -= 1
                entry, body_nos = tpl_entry(tpl, limit)
                cost = nbytes(entry)
        tpl_entries.append((tpl.idx, entry, body_nos))
        tpl_budget -= cost
        ex_no = lines[records[tpl.records[0]].start].no
        shown.add(ex_no)
        shown.update(body_nos)
        for no in [ex_no] + body_nos:
            key = mask(lines[no - 1].text)
            taken[key] = taken.get(key, 0) + 1
    avail -= sum(nbytes(e) for _, e, _ in tpl_entries)

    # Fill with the highest-salience raw lines not yet shown. Lines with the
    # same masked shape as one already shown are worth less each time
    # (diminishing returns), and lines below the salience floor never pad the
    # output just to use up the budget.
    def effective(s: float, key: str) -> float:
        return s / (1.0 + FILL_PENALTY * taken.get(key, 0))

    heap: list[tuple[float, int]] = []
    base: dict[int, tuple[float, Line, str]] = {}
    for ln in lines:
        if ln.no in shown or ln.record < 0 or not ln.text.strip():
            continue
        rec = records[ln.record]
        tpl = templates[rec.template]
        s = line_score(ln, tpl, lines[rec.start], total)
        key = mask(ln.text)
        base[ln.no] = (s, ln, key)
        heapq.heappush(heap, (-effective(s, key), ln.no))
    picked: list[tuple[float, Line]] = []
    fill_avail = avail
    while heap and fill_avail >= 40:
        neg, no = heapq.heappop(heap)
        s, ln, key = base[no]
        cur = effective(s, key)
        if cur != -neg:  # stale entry: re-queue with the current score
            heapq.heappush(heap, (-cur, no))
            continue
        if cur < FILL_FLOOR:
            break
        cost = len(fmt_line(ln, width).encode()) + 1 + 20  # + gap marker allowance
        if cost > fill_avail:
            continue
        picked.append((cur, ln))
        shown.add(ln.no)
        taken[key] = taken.get(key, 0) + 1
        fill_avail -= cost

    ledger_detail = True  # per-template list; dropped only as a last resort

    def ledger(shown_set: set[int]) -> list[str]:
        tpl_omitted = len(templates) - len(tpl_entries)
        omitted_total = total - len(shown_set)
        per_tpl = []
        for tpl in templates:
            om = 0
            for ri in tpl.records:
                r = records[ri]
                om += sum(1 for ln in lines[r.start:r.end] if ln.no not in shown_set)
            if om:
                per_tpl.append((om, tpl))
        per_tpl.sort(key=lambda x: (-x[0], x[1].idx))
        pct = 100.0 * omitted_total / total if total else 0.0
        parts = [f"{t.tid} ×{fmt_int(om)}" for om, t in per_tpl]
        head = f"## omitted {fmt_int(omitted_total)}/{fmt_int(total)} lines ({pct:.1f}%)"
        tail_bits = []
        if tpl_omitted:
            tail_bits.append(f"{tpl_omitted} templates not listed")
        blank_om = sum(1 for ln in lines if ln.text.strip() == "" and ln.no not in shown_set)
        if blank_om:
            tail_bits.append(f"{fmt_int(blank_om)} blank")
        # Fit the per-template list into the reserve.
        shown_parts: list[str] = []
        used = len(head) + 2
        for p in parts:
            if not ledger_detail or used + len(p) + 2 > ledger_reserve - 40:
                break
            shown_parts.append(p)
            used += len(p) + 2
        rest = len(parts) - len(shown_parts)
        if rest > 0:
            tail_bits.insert(0, f"+{rest} more templates")
        line = head
        if shown_parts:
            line += ": " + ", ".join(shown_parts)
        if tail_bits:
            line += " · " + "; ".join(tail_bits)
        return [line]

    show_hint = True

    def render(picked_lines: list[tuple[float, Line]]) -> list[str]:
        out: list[str] = []
        if head_nos:
            out.append(f"## head (L{head_nos[0]}–L{head_nos[-1]})")
            out.extend(render_ranges(lines, head_nos, width))
        if tpl_entries:
            out.append("## templates (by salience)")
            for _, entry, _ in tpl_entries:
                out.extend(entry)
        if picked_lines:
            out.append("## selected lines")
            out.extend(render_ranges(lines, sorted(ln.no for _, ln in picked_lines), width))
        if tail_nos:
            out.append(f"## tail (L{tail_nos[0]}–L{tail_nos[-1]})")
            out.extend(render_ranges(lines, tail_nos, width))
        shown_set = set(head_nos) | set(tail_nos) | {ln.no for _, ln in picked_lines}
        for ti, _, body_nos in tpl_entries:
            shown_set.add(lines[records[templates[ti].records[0]].start].no)
            shown_set.update(body_nos)
        out.extend(ledger(shown_set))
        out.append(run_line)
        if show_hint:
            out.append(run_hint)
        return out

    # Exact fit. The estimates above are close but not exact (gap markers, the
    # ledger, a first template entry larger than its share), so shed content
    # until the rendering fits: lowest-scoring fill lines first, then tail and
    # head lines, then template entries from the bottom of the table, then the
    # ledger detail and the usage hint. The budget is a hard limit.
    picked.sort(key=lambda p: (-p[0], p[1].no))

    def shed() -> bool:
        nonlocal ledger_detail, show_hint
        if picked:
            picked.pop()
        elif tail_nos:
            tail_nos.pop(0)
        elif head_nos:
            head_nos.pop()
        elif len(tpl_entries) > 1:
            tpl_entries.pop()
        elif tpl_entries and tpl_entries[0][2]:
            ti, _, body_nos = tpl_entries[0]
            tpl_entries[0] = (ti, *tpl_entry(templates[ti], len(body_nos) - 1))
        elif ledger_detail:
            ledger_detail = False
        elif show_hint:
            show_hint = False
        elif tpl_entries:
            tpl_entries.pop()
        else:
            return False
        return True

    def measure(body: list[str]) -> int:
        # The header quotes the output size, so its own length depends on the
        # answer; iterate to a fixed point (digit count settles in a pass or two).
        ob = nbytes(body) + len(header(0).encode()) + 1
        for _ in range(4):
            ob2 = nbytes(body) + len(header(ob).encode()) + 1
            if ob2 == ob:
                break
            ob = ob2
        return ob

    while True:
        body = render(picked)
        out_bytes = measure(body)
        if out_bytes <= budget_bytes or not shed():
            break
    # Absurdly small budget: even the skeleton does not fit. Cut from the end.
    while out_bytes > budget_bytes and body:
        body.pop()
        out_bytes = measure(body)
    text = "\n".join([header(out_bytes)] + body) + "\n"
    if len(text.encode()) > budget_bytes:
        text = text.encode()[: budget_bytes - 1].decode("utf-8", errors="ignore").rstrip() + "\n"
    return text


# --------------------------------------------------------------------------
# --only / --except
# --------------------------------------------------------------------------


def parse_tids(spec: str | None, ntpl: int) -> set[int]:
    ids: set[int] = set()
    if not spec:
        return ids
    for part in spec.split(","):
        part = part.strip().upper()
        if not part:
            continue
        if part.startswith("T"):
            part = part[1:]
        if not part.isdigit() or not (1 <= int(part) <= ntpl):
            sys.exit(f"pare: unknown template {part!r} (have T1..T{ntpl})")
        ids.add(int(part) - 1)
    return ids


def select(
    lines: list[Line],
    records: list[Record],
    templates: list[Template],
    *,
    only: set[int],
    exclude: set[int],
    budget: int,
    width: int,
    run_path: str | None,
) -> str:
    want = []
    for rec in records:
        if only and rec.template not in only:
            continue
        if rec.template in exclude:
            continue
        want.extend(ln.no for ln in lines[rec.start:rec.end])
    label = (
        f"--only {','.join(templates[i].tid for i in sorted(only))}" if only else "all"
    )
    if exclude:
        label += f" --except {','.join(templates[i].tid for i in sorted(exclude))}"
    budget_bytes = budget * 4
    head = f"# pare {label}: {fmt_int(len(want))} of {fmt_int(len(lines))} lines"
    rows = render_ranges(lines, want, width)
    body = rows
    omitted = 0
    if nbytes(rows) + len(head) + 200 > budget_bytes:
        # Keep the first 70% and last 30% of what fits.
        keep_bytes = max(0, budget_bytes - len(head) - 200)
        first_b, last_b = int(keep_bytes * 0.7), int(keep_bytes * 0.3)
        first, acc = [], 0
        for no in want:
            c = len(fmt_line(lines[no - 1], width).encode()) + 1
            if acc + c > first_b:
                break
            first.append(no)
            acc += c
        last, acc = [], 0
        for no in reversed(want):
            if no in first:
                break
            c = len(fmt_line(lines[no - 1], width).encode()) + 1
            if acc + c > last_b:
                break
            last.append(no)
            acc += c
        last.reverse()
        kept = first + last
        omitted = len(want) - len(kept)
        body = render_ranges(lines, kept, width)
        if omitted:
            body.append(f"## omitted {fmt_int(omitted)} lines to fit budget {fmt_int(budget)}; raise --budget or narrow --only")
    body.append(f"## raw: {run_path}" if run_path else "## raw: (not saved)")
    return "\n".join([head] + body) + "\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def default_run_dir() -> str:
    env = os.environ.get("PARE_RUN_DIR")
    if env:
        return env
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "pare", "runs")


def tee(data: bytes, run_dir: str | None, run_file: str | None) -> str | None:
    if run_file is None and run_dir is None:
        return None
    if run_file is None:
        digest = hashlib.sha256(data).hexdigest()[:12]
        os.makedirs(run_dir, exist_ok=True)
        run_file = os.path.join(run_dir, f"{digest}.log")
    else:
        d = os.path.dirname(run_file)
        if d:
            os.makedirs(d, exist_ok=True)
    tmp = run_file + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, run_file)
    return os.path.abspath(run_file)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pare", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=int, default=2000, help="output budget in tokens (~bytes/4); default 2000")
    ap.add_argument("--head", type=int, default=5, help="lines to keep from the start; default 5")
    ap.add_argument("--tail", type=int, default=10, help="lines to keep from the end; default 10")
    ap.add_argument("--width", type=int, default=200, help="clip lines longer than this; default 200")
    ap.add_argument("--max-templates", type=int, default=0, metavar="N",
                    help="list at most N templates (highest salience first); 0 = all that fit. "
                         "Unlisted templates still appear in the omitted ledger")
    ap.add_argument("--only", metavar="T<n>[,T<m>]", help="print raw lines of these templates only")
    ap.add_argument("--except", dest="exclude", metavar="T<n>[,T<m>]", help="print raw lines except these templates")
    ap.add_argument("--run-dir", default=None, help="directory for the raw run file (default $PARE_RUN_DIR or ~/.cache/pare/runs)")
    ap.add_argument("--run-file", default=None, help="explicit path for the raw run file")
    ap.add_argument("--no-tee", action="store_true", help="do not save the raw input")
    ap.add_argument("--vocab", metavar="FILE", default=os.environ.get("PARE_VOCAB"),
                    help="vocabulary file that extends or replaces the built-in error/warn/... word "
                         "lists (default: $PARE_VOCAB); see --dump-vocab for the format")
    ap.add_argument("--dump-vocab", action="store_true",
                    help="print the built-in vocabulary (after applying --vocab) in file format and exit")
    ap.add_argument("--version", action="version", version=f"pare {__version__}")
    args = ap.parse_args(argv)

    global VOCAB
    if args.vocab:
        try:
            with open(args.vocab, encoding="utf-8") as f:
                vocab_text = f.read()
        except OSError as e:
            sys.exit(f"pare: cannot read vocab file {args.vocab}: {e.strerror}")
        VOCAB = Vocab(parse_vocab(vocab_text, DEFAULT_VOCAB, source=args.vocab))
    if args.dump_vocab:
        sys.stdout.write(dump_vocab(VOCAB.entries))
        return 0

    data = sys.stdin.buffer.read()
    run_path = None
    if not args.no_tee and data:
        run_path = tee(data, args.run_dir or default_run_dir(), args.run_file)

    lines = split_lines(data)
    if not lines:
        sys.stdout.write("# pare: empty input\n")
        return 0
    records = segment(lines)
    templates = cluster(lines, records)
    score_templates(lines, records, templates)

    if args.only or args.exclude:
        out = select(
            lines, records, templates,
            only=parse_tids(args.only, len(templates)),
            exclude=parse_tids(args.exclude, len(templates)),
            budget=args.budget, width=args.width, run_path=run_path,
        )
    else:
        out = summarize(
            lines, records, templates,
            budget=args.budget, head_n=args.head, tail_n=args.tail, width=args.width,
            max_templates=args.max_templates, run_path=run_path, in_bytes=len(data),
        )
    sys.stdout.write(out)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
