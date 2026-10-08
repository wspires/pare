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
# dependencies = ["drain3==0.9.11"]
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
DEBUG = bool(os.environ.get("PARE_DEBUG"))  # trace the allocation to stderr

# --------------------------------------------------------------------------
# Lines and records
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# Lines that continue the previous record regardless of indentation.
# Generic continuation shapes: conventions shared by many tools.
CONT_GENERIC = (
    r"^\s+at\s"  # JS/Java stack frame
    r"|^\s*File \"[^\"]+\", line \d+"  # Python frame
    r"|^\s*\^+\s*$"  # caret marker
    r"|^\s*~+\s*$"  # tilde underline
    r"|^Caused by:"  # Java/Rust cause chain
    r"|^\s*\.\.\. \d+ more"  # Java elided frames
    r"|^\s*(?:Expected|Received|Actual|Got|Want|Diff):"  # assertion detail
    r"|^\s*[+-]{3}\s"  # diff headers
    r"|^\s*@@ "  # diff hunk
)
# Tool-specific continuation shapes. Everything pare knows about particular
# tools is listed here and in TOOL_SPECIFIC_NOTES, and can be switched off
# together (TOOL_SPECIFIC = False) to measure what the generic signals alone
# achieve; bench/ablate.py has an entry for that.
CONT_TOOL = (
    r"|^\s*\d*\s*\|(?:\s|$)"  # rustc/gcc gutter lines
    r"|^\s*-->\s"  # rustc location
    r"|^\s*= (?:note|help|warning):"  # rustc notes
    r"|^\s*::: "  # rustc secondary location
    r"|^E\s{2,}|^>\s{2,}"  # pytest failure detail and source marker lines
)
CONT_GENERIC_RE = re.compile(CONT_GENERIC)
CONT_RE = re.compile(CONT_GENERIC + CONT_TOOL)
TOOL_SPECIFIC = True
TOOL_SPECIFIC_NOTES = [
    "pytest: E and > marker lines continue the failure block; a bare E at column 0 is an error level",
    "rustc/gcc: gutter (NN |), --> location, = note/help and ::: lines continue the diagnostic; error[E0308]: is a level",
    "logcat: E/Tag( and E/Tag: at the line start are levels; glog: E0921 10:11:12",
    "Jest/pytest: a single blank line inside an indented block does not end the record",
]

MAX_RECORD_LINES = 400


@dataclass
class Line:
    no: int  # 1-based line number in the input
    text: str  # ANSI-stripped, \r-resolved, right-stripped
    indent: int
    record: int = -1  # record index
    depth: int = 0  # 0 for head line, >0 for continuation
    # Memoised per-line analysis (filled lazily; see line_sev and friends).
    memo: dict = field(default_factory=dict)


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
    levels: dict = field(default_factory=dict)  # severity -> number of records
    exemplar: int = -1  # record index shown for this template (highest severity)

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
            if pending_blank is not None or cur is None or not TOOL_SPECIFIC:
                cur = None
            pending_blank = ln if cur is not None else None
            continue
        head_indent = lines[cur.start].indent if cur else None
        is_cont = (
            cur is not None
            and (cur.end - cur.start) < MAX_RECORD_LINES
            and (ln.indent > head_indent or (CONT_RE if TOOL_SPECIFIC else CONT_GENERIC_RE).match(ln.text))
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
    # Rule runs (=====, _____, -----) shrink to three characters so banners of
    # different widths share a template. Applied as a plain substitution.
    ("RULE", re.compile(r"([=_\-*#~.])\1{2,}")),
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
        elif name == "RULE":
            t = rx.sub(lambda m: m.group(1) * 3, t)
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
    # Structural log levels: a token in a level position or form (bare
    # UPPERCASE, [bracketed], level=x, "level":"x", `error:` prefix). When one
    # is found it sets the line's base score and, for info/debug, suppresses
    # error-word bonuses. Entries are plain tokens, not regexes.
    "level-fatal": (4.0, "level tokens meaning fatal/critical"),
    "level-error": (3.0, "level tokens meaning error"),
    "level-warn": (1.5, "level tokens meaning warning"),
    "level-info": (-0.5, "level tokens meaning info/notice"),
    "level-debug": (-1.0, "level tokens meaning debug/trace"),
}

LEVEL_CLASSES = {"level-fatal": 4, "level-error": 3, "level-warn": 2, "level-info": 1, "level-debug": 0}
LEVEL_SCORE = {sev: VOCAB_CLASSES[cls][0] for cls, sev in LEVEL_CLASSES.items()}
LEVEL_NAME = {4: "FATAL", 3: "ERROR", 2: "WARN", 1: "INFO", 0: "DEBUG"}
SEVERITY_BY_NAME = {"debug": 0, "info": 1, "warn": 2, "error": 3, "fatal": 4}
LETTER_LEVEL = {"F": 4, "E": 3, "W": 2, "I": 1, "D": 0, "V": 0, "T": 0}

# HTTP status codes in request or key-value forms. 5xx -> error, 4xx -> warn,
# 1xx-3xx -> info (a successful request line is demoted like an INFO line).
HTTP_STATUS_RX = re.compile(
    r'\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+(?:\s+HTTP/\d(?:\.\d)?"?)?\s+(\d{3})\b'  # GET /p 500, "GET /p HTTP/1.1" 500
    r"|(?i:\b(?:status(?:_?code)?|http_?status|statuscode|response_?code|code)\b\s*[\"']?\s*[=:]\s*[\"']?(\d{3})\b)"  # status=503
    r"|\bHTTP/\d(?:\.\d)?\s+(\d{3})\b"  # HTTP/1.1 502
    r"|(?i:\b(?:returned\s+(?:http|status(?:\s+code)?)|responded(?: with)?|status code|http)\s+(\d{3})\b)"  # returned HTTP 500 (bare "returned 500" is a row count)
)
# Process exit codes. 0 -> info, 1-127 -> error, 128+ or negative (signals,
# OOM kills) -> fatal.
EXIT_CODE_RX = re.compile(
    r"(?i)\bexit(?:ed)?(?:\s+with)?\s+(?:code|status)\s*[=:]?\s*(-?\d{1,3})\b"  # exit code 1, exited with status 137
    r"|\bexit(?:ed)?\s*[=:]\s*(-?\d{1,3})\b"  # exit=0
    r"|^\s*exit\s+(-?\d{1,3})\b"  # a line-initial "exit 1" (not "exited with 3 warnings")
    r"|\b(?:exit[_ ]?code|exit[_ ]?status|return\s*code|returncode|rc)\s*[=:]\s*(-?\d{1,3})\b"  # rc=1 (not 1.4.0-rc1)
)


def http_severity(text: str) -> int | None:
    m = HTTP_STATUS_RX.search(text)
    if not m:
        return None
    code = int(next(g for g in m.groups() if g))
    if code >= 500:
        return 3
    if code >= 400:
        return 2
    if code >= 100:
        return 1
    return None


def exit_severity(text: str) -> int | None:
    m = EXIT_CODE_RX.search(text)
    if not m:
        return None
    code = int(next(g for g in m.groups() if g))
    if code == 0:
        return 1
    if code < 0 or code >= 128:
        return 4
    return 3

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
        "/✗|✘|❌/", r"/(?<![A-Za-z])FAIL(?:ED|URE|S)?\b/",  # not XFAIL; pytest's E marker is structural
        r"/^\s*[-+]?\s*(?:Expected|Received|Actual|Got|Want)\b\s*[:+-]/",  # assertion diff lines
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
        "added", "removed", "audited",  # npm-style install summaries
        "complete", "coverage", "report", r"/(?i)(?<![a-z])ran \d+/",
        r"/^\s*={3,}/", r"/^\s*-{3,}/", r"/^\s*_{3,}/", r"/^\s*#{1,3}\s/", r"/^\s*\*{3,}/",
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
        "verbose", "debug", "trace", "info", "get", "heartbeat", "ping", "pong", "poll",
        "polling", "tick",
        r"/\d+%/", r"/\.{3,}/", "/[▏▎▍▌▋▊▉█░▒▓]/", r"/\b(?:200|204|304) (?:OK|No Content|Not Modified)\b/",
    ],
    "strong-error": [
        r"/(?i)\b\w*(?:error|exception)\b/", r"/(?i)\bpanic/", r"/(?i)\btraceback\b/",
        r"/(?i)\bassert\w*\b/", r"/(?i)\bfatal\b/", r"/(?i)\bfail(?:ed|ure|s)?\b/",
        r"/^\s*E\s{2,}/", "/✕|✗|FAIL/",
    ],
    "level-fatal": ["FATAL", "CRITICAL", "CRIT", "EMERG", "EMERGENCY", "ALERT", "PANIC", "SEVERE"],
    "level-error": ["ERROR", "ERR"],
    "level-warn": ["WARN", "WARNING"],
    "level-info": ["INFO", "INFORMATION", "NOTICE"],
    "level-debug": ["DEBUG", "TRACE", "VERBOSE", "FINE", "FINER", "FINEST"],
}


_GLOBAL_FLAGS = re.compile(r"^\(\?([aimsux]+)\)")
ZERO_COUNT_RX = re.compile(  # a zero count before an error noun is a summary, not an error
    r"(?i)\b(?:0|no|zero) (?:errors?|failures?|failed|failing|problems?|issues?|warnings?|vulnerabilit(?:y|ies))\b"
)


def _is_regex_entry(entry: str) -> bool:
    return len(entry) >= 2 and entry.startswith("/") and entry.endswith("/")


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
    # inside the group, so it is case-insensitive too: not inside a word, and
    # not inside a hyphenated flag or identifier such as --timeout=300s).
    return r"(?i:(?<![a-z-])" + re.escape(entry) + r"(?![a-z-]))"


class Vocab:
    """Compiled vocabulary: one regex per class, or None for an empty class."""

    def __init__(self, entries: dict[str, list[str]]):
        self.entries = entries
        self.rx: dict[str, re.Pattern | None] = {}
        for cls in VOCAB_CLASSES:
            if cls in LEVEL_CLASSES:
                continue
            items = entries.get(cls, [])
            # All plain words of a class share one boundary group (one
            # lookbehind for the whole alternation, not one per word), which
            # is several times faster to scan than a branch per word.
            words = [e for e in items if not _is_regex_entry(e)]
            parts = [_entry_regex(e) for e in items if _is_regex_entry(e)]
            if words:
                alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                parts.insert(0, r"(?i:(?<![a-z-])(?:" + alt + r")(?![a-z-]))")
            self.rx[cls] = re.compile("|".join(f"(?:{p})" for p in parts)) if parts else None
        # Level tokens, longest first so WARNING wins over WARN.
        self.level_of: dict[str, int] = {}
        for cls, sev in LEVEL_CLASSES.items():
            for tok in entries.get(cls, []):
                self.level_of[tok.upper()] = sev
        alt = "|".join(re.escape(t) for t in sorted(self.level_of, key=len, reverse=True)) or "(?!x)x"
        # key=value forms anywhere on the line: level=error, "level": "warn", severity=INFO
        self.level_key_rx = re.compile(
            r"(?i)\b(?:level|lvl|loglevel|log_level|severity|sev|priority)\b\s*[\"']?\s*[=:]\s*[\"']?(" + alt + r")(?![A-Za-z])"
        )
        # bare or bracketed forms near the start of the line
        self.level_bare_rx = re.compile(
            r"(?i)(?:^|[\s\[(<|{\"':,=/-])(" + alt + r")(?=$|[\s\])>|:,\"'/\[(.-])"
        )
        # single-letter forms: logcat E/Tag, glog E0921 10:11:12, [E]
        # ...anchored to the line start (after an optional timestamp and
        # pid/tid), so "I/O error" and "T/F flag" are not levels; and pytest's
        # failure-detail marker, a bare E at column 0 followed by two spaces.
        self.level_letter_rx = re.compile(
            r"^(?:[\d:.\-]{5,24}\s+){0,2}(?:\d+\s+\d+\s+)?([EWIDVTF])/[\w.$]+[(:]"  # logcat E/Tag( or E/Tag:
            r"|^(?:[\d:.\-]{5,24}\s+){1,2}\d+\s+\d+\s+([EWIDVF])\s+\S+:"  # logcat threadtime: date time pid tid E Tag:
            r"|^([EWIF])\d{4}\s"  # glog E0921 10:11:12
            r"|\[([EWIDVTF])\]"  # [I]
            r"|^(E)\s{2,}"  # pytest
        )
        self.level_letter_generic_rx = re.compile(r"\[([EWIDVTF])\]")

    def has(self, cls: str, text: str) -> bool:
        rx = self.rx[cls]
        if rx is None:
            return False
        if cls in ("error", "warn", "strong-error"):
            text = ZERO_COUNT_RX.sub(" ", text)  # "0 errors", "no errors": a summary, not an error
        return bool(rx.search(text))

    def detect_level(self, text: str) -> int | None:
        """Severity 0-4 of an explicit log-level token, or None. Structural:
        the token must be in a level form, not just a word in the message."""
        m = self.level_key_rx.search(text)
        if m:
            return self.level_of[m.group(1).upper()]
        head = text[:LEVEL_SCAN_CHARS]
        for m in self.level_bare_rx.finditer(head):
            tok = m.group(1)
            start, end = m.start(1), m.end(1)
            nxt = head[end : end + 1]
            ok = (
                tok.isupper()  # ERROR, WARN: an uppercase token is a level
                or (start > 0 and head[start - 1] in "[(<|")  # [error], (warn)
                or (nxt == ":" and start <= 60)  # error: ..., Warning: ...
                or (TOOL_SPECIFIC and start == 0 and nxt == "[")  # rustc error[E0308]:
            )
            if ok:
                return self.level_of[tok.upper()]
        m = (self.level_letter_rx if TOOL_SPECIFIC else self.level_letter_generic_rx).search(text[:80])
        if m:
            return LETTER_LEVEL[next(g for g in m.groups() if g)]
        return None

    def severity(self, text: str) -> int | None:
        """Strongest structural signal on the line: log level, HTTP status
        code, or exit code. None when the line carries none of them."""
        found = [s for s in (self.detect_level(text), http_severity(text), exit_severity(text)) if s is not None]
        return max(found) if found else None


LEVEL_SCAN_CHARS = 120


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
            if cls in LEVEL_CLASSES:
                sys.exit(f"pare: {source}:{n}: level classes take plain tokens, not regexes: {line!r}")
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


_UNSET = object()


def vocab_score(text: str, head: bool = True, sev=_UNSET) -> float:
    """Salience of one line's text. A structural log level, when present,
    sets the base score and decides whether error/warn words count: an INFO
    line that mentions 'error' is still info, and an ERROR line is an error
    whatever language its message is in. Summary vocabulary (totals, final
    verdicts) only counts on a record head, not inside a block."""
    s = 0.0
    err = VOCAB.has("error", text)  # Vocab.has strips zero counts ("0 errors") itself
    warn = VOCAB.has("warn", text)
    if sev is _UNSET:
        sev = VOCAB.severity(text)
    if sev is None:
        if VOCAB.has("pass", text):
            # A passing-test marker is structural: the words of a test name
            # ("throws on invalid") are not log vocabulary.
            s += VOCAB_CLASSES["pass"][0]
        else:
            if err:
                s += VOCAB_CLASSES["error"][0]
            if warn:
                s += VOCAB_CLASSES["warn"][0]
            if not err and VOCAB.has("noise", text):
                s += VOCAB_CLASSES["noise"][0]
    else:
        s += LEVEL_SCORE[sev]
        if sev >= 2:  # vocabulary reinforces a warning or error level
            if err:
                s += 0.5 * VOCAB_CLASSES["error"][0]
            if warn:
                s += 0.5 * VOCAB_CLASSES["warn"][0]
    if head and VOCAB.has("summary", text):
        s += VOCAB_CLASSES["summary"][0]
    if VOCAB.has("frame", text):
        s += VOCAB_CLASSES["frame"][0]
    if len(text.strip()) < 4:
        s -= 1.0
    return s


def line_sev(ln: Line) -> int | None:
    """Structural severity of a line, computed once."""
    m = ln.memo
    if "sev" not in m:
        m["sev"] = VOCAB.severity(ln.text)
    return m["sev"]


def line_has(ln: Line, cls: str) -> bool:
    """Vocabulary class membership of a line, computed once per class."""
    m = ln.memo
    key = "has:" + cls
    if key not in m:
        m[key] = VOCAB.has(cls, ln.text)
    return m[key]


def line_vocab(ln: Line, head: bool) -> float:
    """vocab_score of a line, computed once per (line, head)."""
    m = ln.memo
    key = "vocab:head" if head else "vocab:body"
    if key not in m:
        m[key] = vocab_score(ln.text, head=head, sev=line_sev(ln))
    return m[key]


def line_mask(ln: Line) -> str:
    """Masked text of a line, computed once."""
    m = ln.memo
    if "mask" not in m:
        m["mask"] = mask(ln.text)
    return m["mask"]


def rarity_of(tpl: Template) -> float:
    return 1.0 / (1.0 + math.log2(len(tpl.records)))


def score_templates(lines: list[Line], records: list[Record], templates: list[Template]) -> None:
    for tpl in templates:
        # Structural levels of the member records. The exemplar is the first
        # record at the highest severity, so a template of 4,000 INFO lines
        # and 3 ERROR lines is represented by an ERROR line.
        tpl.levels = {}
        best_sev, best_rec = -1, tpl.records[0]
        for ri in tpl.records:
            sev = line_sev(lines[records[ri].start])
            if sev is not None:
                tpl.levels[sev] = tpl.levels.get(sev, 0) + 1
            if (sev if sev is not None else -1) > best_sev:
                best_sev, best_rec = (sev if sev is not None else -1), ri
        tpl.exemplar = best_rec
        # The template is judged by its exemplar head line, a real line with
        # the full per-line analysis, rather than by the masked template text
        # (where "0 errors" has already become "<NUM> errors"). The exemplar is
        # the highest-severity record, so its severity is the worst seen.
        head = lines[records[tpl.exemplar].start]
        tpl_level = line_sev(head)
        head_vocab = line_vocab(head, head=True)
        body_err = 0.0
        rec = records[tpl.exemplar]
        for ln in lines[rec.start + 1 : rec.end]:
            if line_has(ln, "strong-error"):
                body_err = 1.0
                break
        # Salience: what the head line says, plus how unusual the shape is.
        # (Position, block-size and body-error bonuses were ablated away:
        # they changed nothing at any budget. See bench/ablate.py.)
        tpl.vocab = head_vocab
        tpl.salience = head_vocab + RARITY_WEIGHT * rarity_of(tpl)
        if tpl_level is not None and tpl_level <= 1 and not body_err:
            tpl.marker = "S" if line_has(head, "summary") else "."  # info/debug: structure wins
        elif (tpl_level is not None and tpl_level >= 3) or body_err or (
            line_has(head, "error") and not line_has(head, "pass")
        ):
            tpl.marker = "E"
        elif tpl_level == 2 or line_has(head, "warn"):
            tpl.marker = "W"
        elif line_has(head, "summary"):
            tpl.marker = "S"
        else:
            tpl.marker = "."


def line_score(ln: Line, head: Line) -> float:
    """Salience of one raw line: its own vocabulary plus a share of its
    record head's vocabulary, less a little per level of depth."""
    s = line_vocab(ln, head=ln.depth == 0)
    if ln.depth > 0:
        s -= DEPTH_PENALTY * min(ln.depth, 4)
        if not (line_has(ln, "pass") and not line_has(ln, "error")):
            s += INHERIT * max(0.0, line_vocab(head, head=True))
    return s


def error_locations(lines: list[Line], records: list[Record]) -> set[int]:
    """Line numbers of the first source location after an error line within
    a record: the 'at file:line' under a Jest or Python exception, the '-->'
    under a rustc error, the innermost frame under a logged exception. That
    frame says where the failure is; the ones after it say how it got there."""
    out: set[int] = set()
    for rec in records:
        armed = False
        for ln in lines[rec.start : rec.end]:
            t = ln.text
            if not t.strip():
                continue
            sev = line_sev(ln)
            is_loc = line_has(ln, "frame") and not t.lstrip().startswith(">")
            if armed and is_loc:
                out.add(ln.no)
                armed = False
            elif (sev is not None and sev >= 3) or (line_has(ln, "error") and not is_loc):
                armed = True
    return out


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
NEAR_DUP_MIN_TOKENS = 4
NEAR_DUP_CONTAINMENT = 0.8


def token_set(text: str) -> frozenset[str]:
    return frozenset(TOKEN_RE.findall(text))


def is_near_duplicate(toks: frozenset[str], shown: list[frozenset[str]]) -> bool:
    """True when at least NEAR_DUP_CONTAINMENT of the line's tokens already
    appear in one line on screen. Short lines are exempt: too easy to contain."""
    if len(toks) < NEAR_DUP_MIN_TOKENS:
        return False
    need = NEAR_DUP_CONTAINMENT * len(toks)
    return any(len(toks & s) >= need for s in shown)


def shape_key(ln: Line) -> str:
    """Dedup key for the fill phase: the masked shape plus the structural
    severity, so a 503 request line is not a duplicate of a 200 one even
    though masking turns both status codes into <NUM>."""
    sev = line_sev(ln)
    return line_mask(ln) + (f" ·sev{sev}" if sev is not None else "")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


WINDOW_SKIP_COUNT = 5  # head/tail windows skip lines of templates this common
WINDOW_SCAN_FACTOR = 3  # ...looking at most this many window-lengths into the file
NEAR_DUP_MAX_COUNT = 5  # templates this small are skipped when their exemplar restates a shown line
# Scoring weights. Each one survived bench/ablate.py: switching it off lowers
# recall at some budget or grows the output at the same recall.
RARITY_WEIGHT = 2.0  # template salience: weight of 1/(1+log2 count)
INHERIT = 0.6  # share of the record head's vocabulary a continuation line inherits
DEPTH_PENALTY = 0.25  # per level of indentation depth, up to 4
LOCATION_BONUS = 1.5  # the first source location after an error line in a record
DENSITY_REF = 120  # bytes; a raw line's priority is scaled by (REF / (REF + len))^POWER
DENSITY_POWER = 0.35
FILL_PENALTY = 1.5  # divisor growth per already-shown line of the same repeat key
FILL_FLOOR = 1.0  # lines scoring below this never pad the output
COMPACT_BUDGET = 1000  # below this many tokens, multi-record entries are one line
HINT_BUDGET = 1000  # below this, the usage hint at the end is dropped


CHARS_PER_TOKEN = 4.0  # bytes per token for the estimate; --chars-per-token calibrates it


def tokens(nbytes: int) -> int:
    """Token estimate: bytes / CHARS_PER_TOKEN. A proxy, not a tokenizer; pare
    output measures about 2.85 bytes per token on cl100k/o200k (bench/tokens.py),
    so pass --chars-per-token 2.8 to budget in real units."""
    return math.ceil(nbytes / CHARS_PER_TOKEN)


def budget_bytes_for(budget: int) -> int:
    return int(budget * CHARS_PER_TOKEN)


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
    budget_bytes = budget_bytes_for(budget)
    total = len(lines)
    by_sal = sorted(templates, key=lambda t: (-t.salience, t.idx))

    def header(out_bytes: int) -> str:
        return (
            f"# pare: {fmt_int(total)} lines, {fmt_int(in_bytes)} B (~{fmt_int(tokens(in_bytes))} tok)"
            f" → ~{fmt_int(tokens(out_bytes))} tok (budget {fmt_int(budget)}) · {len(templates)} templates"
        )

    run_line = f"## raw: {run_path}" if run_path else "## raw: (not saved)"
    run_hint = "   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw"

    # Small input: never make it bigger. Pass it through verbatim if it fits,
    # with no header; the only addition is the run-file line when one exists.
    verbatim = [ln.text for ln in lines] + ([run_line] if run_path else [])
    if nbytes(verbatim) <= budget_bytes:
        return "\n".join(verbatim) + "\n"

    # Fixed overhead: header + run lines + section titles + ledger reserve.
    ledger_reserve = min(400, max(120, budget_bytes // (16 if budget < COMPACT_BUDGET else 12)))
    show_hint = budget >= HINT_BUDGET
    fixed = nbytes([header(budget_bytes), run_line, run_hint if show_hint else "", "## head", "## templates (by salience)",
                    "## selected lines", "## tail"]) + ledger_reserve
    avail = max(0, budget_bytes - fixed)

    shown: set[int] = set()  # line numbers already displayed

    # Head / tail windows: at most 30% of the budget together.
    # Head and tail windows skip lines that belong to a high-frequency
    # template (the template table already stands for those), except the
    # very first and last lines of the input. The scan is bounded so the
    # window stays a window and does not wander into the middle of the file.
    def windowed(seq: list[Line], n: int, keep_no: int) -> list[int]:
        out: list[int] = []
        for ln in seq[: max(n, WINDOW_SCAN_FACTOR * n)]:
            if len(out) >= n:
                break
            if ln.no == keep_no or ln.record < 0:
                out.append(ln.no)
                continue
            rec = records[ln.record]
            frequent = len(templates[rec.template].records) >= WINDOW_SKIP_COUNT
            long_body = ln.depth > 0 and rec.end - rec.start >= WINDOW_SKIP_COUNT
            if not frequent and not long_body:
                out.append(ln.no)
        return out

    if budget < COMPACT_BUDGET:
        head_n = min(head_n, 3)  # a small budget gets a smaller head window (the tail keeps the verdicts)
    head_nos = windowed(lines, head_n, 1)
    tail_nos = sorted(windowed(list(reversed(lines)), tail_n, total))
    tail_nos = [no for no in tail_nos if no not in head_nos]
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

    # One priority queue for template entries and raw lines, on the same
    # salience scale. A template entry (its count, marker and exemplar head
    # line) competes at tpl.salience; a raw line at its line score with
    # diminishing returns for repeated shapes.
    # So at any budget a high-salience exception line beats a low-salience
    # singleton template, and a warning template beats the sixth frame of a
    # stack trace. Lines that land in a placed template's exemplar record are
    # rendered under that entry as its body; the rest go to "selected lines".
    # At small budgets a multi-record entry is one line (count + exemplar)
    # instead of template text plus exemplar.
    placed: dict[int, list[str]] = {}  # template idx -> head entry lines
    taken: dict[str, int] = {}  # repeat key -> lines already shown with it
    compact = budget < COMPACT_BUDGET
    window_nos = set(head_nos) | set(tail_nos)

    def tpl_head(tpl: Template) -> list[str]:
        rec = records[tpl.exemplar]
        ex = lines[rec.start]
        count = len(tpl.records)
        extra = f" ({fmt_int(tpl.lines)} lines)" if tpl.lines != count else ""
        if len(tpl.levels) > 1:  # mixed levels inside one template: say so
            hist = " ".join(f"{LEVEL_NAME[s]}×{fmt_int(c)}" for s, c in sorted(tpl.levels.items(), reverse=True))
            extra += f" [{hist}]"
        # An exemplar that the head or tail window already prints is referenced,
        # not repeated.
        where = "head" if ex.no in head_nos else "tail" if ex.no in tail_nos else None
        ref = f"L{ex.no} (in {where})" if where else f"L{ex.no}: {clip(ex.text.strip(), width)}"
        if count == 1 or compact:
            return [f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {ref}"]
        return [f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {clip(tpl.text, width)}", f"     e.g. {ref}"]

    def repeat_key(ln: Line) -> str:
        """What counts as 'another one of these' for diminishing returns: for a
        record head, its drain template plus severity (drain already decided
        that pod/storefront-x2k9p and pod/storefront-q7m2z are one shape);
        for a continuation line, its masked shape plus severity."""
        if ln.depth == 0 and ln.record >= 0:
            return f"tpl{records[ln.record].template}·sev{line_sev(ln)}"
        return shape_key(ln)

    def note_shown(no: int) -> None:
        ln = lines[no - 1]
        shown.add(no)
        key = repeat_key(ln)
        taken[key] = taken.get(key, 0) + 1
        shown_tokens.append(token_set(ln.text))

    shown_tokens = [token_set(lines[no - 1].text) for no in sorted(shown)]
    exemplar_nos = {lines[records[t.exemplar].start].no for t in templates}

    # Raw-line candidates: everything not in a window. A template's exemplar
    # head is a candidate too, so it can still appear when the template entry
    # itself does not make the cut; if the entry is placed later it takes
    # over that line.
    base: dict[int, tuple[float, Line, str]] = {}
    locations = error_locations(lines, records)
    for ln in lines:
        if ln.no in shown or ln.record < 0 or not ln.text.strip():
            continue
        rec = records[ln.record]
        tpl = templates[rec.template]
        s = line_score(ln, lines[rec.start])
        if ln.no in locations:
            s += LOCATION_BONUS
        key = repeat_key(ln)
        base[ln.no] = (s, ln, key)

    def effective(no: int) -> float:
        s, ln, key = base[no]
        # Each repeat of a shape already on screen is worth less.
        e = s / (1.0 + FILL_PENALTY * taken.get(key, 0))
        # Value per byte: a 180-byte advice line with a URL is worth less than
        # a 50-byte exception line of the same salience.
        e *= (DENSITY_REF / (DENSITY_REF + len(ln.text.strip()))) ** DENSITY_POWER
        return e

    # Heap items: (-priority, kind, id); kind 0 = template entry, 1 = line,
    # so a template wins a tie and ordering is deterministic.
    heap: list[tuple[float, int, int]] = []
    for tpl in templates:
        heapq.heappush(heap, (-tpl.salience, 0, tpl.idx))
    for no in base:
        heapq.heappush(heap, (-effective(no), 1, no))

    picked: list[tuple[float, Line]] = []
    picked_nos: set[int] = set()
    deferred: list[tuple[float, int, int]] = []  # items that did not fit; retried against measured slack

    def place(avail: int) -> int:
        """Greedy pass over the heap. Returns the budget left over."""
        while heap and avail >= 40:
            negp, kind, ident = heapq.heappop(heap)
            if kind == 0:
                tpl = templates[ident]
                if max_templates and len(placed) >= max_templates:
                    continue
                ex_rec = records[tpl.exemplar]
                ex_line = lines[ex_rec.start]
                if len(tpl.records) == 1 and all(ln.no in window_nos for ln in lines[ex_rec.start : ex_rec.end] if ln.text.strip()):
                    continue  # a singleton whose whole record the head or tail window already prints
                if len(tpl.records) <= NEAR_DUP_MAX_COUNT and ex_line.no not in shown and ex_rec.end - ex_rec.start == 1:
                    # A rare one-line record that restates something already on
                    # screen (a progress line repeating the summary line) stays in
                    # the ledger. A multi-line record carries more than its head.
                    if is_near_duplicate(token_set(ex_line.text), shown_tokens):
                        continue
                entry = tpl_head(tpl)
                cost = nbytes(entry)
                if tpl.marker in ("E", "W") and ex_rec.end - ex_rec.start > 1:
                    cost += 30  # room for the "(+N more lines)" marker under it
                if cost > avail and placed:
                    deferred.append((negp, kind, ident))
                    if DEBUG:
                        print(f"defer tpl {tpl.tid:<4} prio={-negp:6.2f} cost={cost:4d} left={avail:5d}", file=sys.stderr)
                    continue  # the first template always gets a line
                placed[tpl.idx] = entry
                avail -= cost
                if DEBUG:
                    print(f"place tpl {tpl.tid:<4} prio={-negp:6.2f} cost={cost:4d} left={avail:5d} {ex_line.text.strip()[:60]!r}", file=sys.stderr)
                if ex_line.no not in shown:
                    note_shown(ex_line.no)
                elif ex_line.no in picked_nos:  # the entry now shows this line
                    picked[:] = [(p, l) for p, l in picked if l.no != ex_line.no]
                    picked_nos.discard(ex_line.no)
                    avail += len(fmt_line(ex_line, width).encode()) + 1 + 20
                continue
            no = ident
            cur = effective(no)
            if cur != -negp:  # stale: re-queue at the current score
                heapq.heappush(heap, (-cur, 1, no))
                continue
            if cur < FILL_FLOOR or no in shown:
                continue  # below the floor a line never pads the output
            if no in exemplar_nos and records[lines[no - 1].record].template in placed:
                continue  # its template entry already shows it
            s, ln, key = base[no]
            cost = len(fmt_line(ln, width).encode()) + 1 + 20  # + gap marker allowance
            if cost > avail:
                deferred.append((negp, kind, ident))
                if DEBUG:
                    print(f"defer L{no:<7} prio={cur:6.2f} cost={cost:4d} left={avail:5d} {ln.text.strip()[:60]!r}", file=sys.stderr)
                continue
            toks = token_set(ln.text)
            if ln.depth == 0 and is_near_duplicate(toks, shown_tokens):
                continue
            if ln.depth > 0 and len(toks) >= NEAR_DUP_MIN_TOKENS and any(toks <= t for t in shown_tokens):
                continue  # a continuation line that is a pure restatement of a shown line
            picked.append((cur, ln))
            picked_nos.add(no)
            note_shown(no)
            avail -= cost
            if DEBUG:
                print(f"place L{no:<7} prio={cur:6.2f} cost={cost:4d} left={avail:5d} {ln.text.strip()[:60]!r}", file=sys.stderr)
        return avail

    avail = place(avail)

    def body_of(ti: int, picked_lines: list[tuple[float, Line]]) -> tuple[list[str], set[int]]:
        """Rendered body of a placed template: the picked lines inside its
        exemplar record, plus a marker for what is left unseen."""
        rec = records[templates[ti].exemplar]
        nos = sorted(ln.no for _, ln in picked_lines if rec.start < ln.no - 1 < rec.end)
        out = render_ranges(lines, nos, width) if nos else []
        rest = sum(1 for b in lines[rec.start + 1 : rec.end] if b.text.strip() and b.no not in window_nos) - len(nos)
        if rest > 0:
            out.append(f"      ┆ (+{fmt_int(rest)} more lines)")
        return out, set(nos)

    ledger_detail = True  # per-template list; dropped only as a last resort

    def ledger(shown_set: set[int]) -> list[str]:
        tpl_omitted = len(templates) - len(placed)
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

    def render(picked_lines: list[tuple[float, Line]]) -> list[str]:
        out: list[str] = []
        if head_nos:
            out.append(f"## head (L{head_nos[0]}–L{head_nos[-1]})")
            out.extend(render_ranges(lines, head_nos, width))
        in_bodies: set[int] = set()
        if placed:
            out.append("## templates (by salience)")
            for tpl in by_sal:
                if tpl.idx not in placed:
                    continue
                out.extend(placed[tpl.idx])
                body, nos = body_of(tpl.idx, picked_lines)
                out.extend(body)
                in_bodies |= nos
        rest_nos = sorted(ln.no for _, ln in picked_lines if ln.no not in in_bodies)
        if rest_nos:
            out.append("## selected lines")
            out.extend(render_ranges(lines, rest_nos, width))
        if tail_nos:
            out.append(f"## tail (L{tail_nos[0]}–L{tail_nos[-1]})")
            out.extend(render_ranges(lines, tail_nos, width))
        shown_set = set(head_nos) | set(tail_nos) | {ln.no for _, ln in picked_lines}
        for ti in placed:
            shown_set.add(lines[records[templates[ti].exemplar].start].no)
        out.extend(ledger(shown_set))
        out.append(run_line)
        if show_hint:
            out.append(run_hint)
        return out

    # Exact fit. The estimates above are close but not exact (gap markers, the
    # ledger, a first template entry larger than the budget), so shed content
    # until the rendering fits: lowest-scoring picked lines first, then tail
    # and head lines, then template entries from the bottom of the table,
    # then the ledger detail and the usage hint. The budget is a hard limit.
    picked.sort(key=lambda p: (-p[0], p[1].no))
    placed_order = [t.idx for t in by_sal if t.idx in placed]

    def shed() -> bool:
        nonlocal ledger_detail, show_hint
        if picked:
            picked.pop()
        elif tail_nos:
            tail_nos.pop(0)
        elif head_nos:
            head_nos.pop()
        elif len(placed_order) > 1:
            placed.pop(placed_order.pop())
        elif ledger_detail:
            ledger_detail = False
        elif show_hint:
            show_hint = False
        elif placed_order:
            placed.pop(placed_order.pop())
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

    # The per-item allowances (gap markers, record markers) are conservative,
    # so the first pass usually leaves slack. Offer the deferred items and
    # whatever is still queued against the measured slack, a few times.
    for _ in range(3):
        slack = budget_bytes - measure(render(picked))
        if slack < 40 or not (deferred or heap):
            break
        for item in deferred:
            heapq.heappush(heap, item)
        deferred.clear()
        placed_before, picked_before = len(placed), len(picked)
        place(slack)
        if len(placed) == placed_before and len(picked) == picked_before:
            break
        picked.sort(key=lambda p: (-p[0], p[1].no))
        placed_order[:] = [t.idx for t in by_sal if t.idx in placed]

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


def parse_lineno(spec: str | None, total: int, flag: str) -> int | None:
    """A line reference as the summary prints it: L3001 or 3001. A negative
    value counts from the end (-200 is the 200th-from-last line)."""
    if spec is None:
        return None
    s = spec.strip().upper()
    if s.startswith("L"):
        s = s[1:]
    try:
        n = int(s)
    except ValueError:
        sys.exit(f"pare: {flag} expects a line number like L3001, 3001 or -200; got {spec!r}")
    if n == 0:
        sys.exit(f"pare: {flag}: lines are numbered from 1")
    if n < 0:
        n = max(1, total + n + 1)
    return n


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
    min_severity: int | None = None,
    since: int | None = None,
    until: int | None = None,
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
        if min_severity is not None:
            # Decided by the record's head line; a record with no structural
            # signal (no level, status or exit code) is left out.
            sev = VOCAB.severity(lines[rec.start].text)
            if sev is None or sev < min_severity:
                continue
        want.extend(ln.no for ln in lines[rec.start:rec.end])
    lo = since if since is not None else 1
    hi = until if until is not None else len(lines)
    if lo > hi:
        sys.exit(f"pare: --since L{lo} is after --until L{hi}")
    want = [no for no in want if lo <= no <= hi]
    label = (
        f"--only {','.join(templates[i].tid for i in sorted(only))}" if only else "all"
    )
    if exclude:
        label += f" --except {','.join(templates[i].tid for i in sorted(exclude))}"
    if min_severity is not None:
        label += f" --min-severity {LEVEL_NAME[min_severity].lower()}"
    if since is not None:
        label += f" --since L{since}"
    if until is not None:
        label += f" --until L{until}"
    budget_bytes = budget_bytes_for(budget)
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
    global VOCAB, CHARS_PER_TOKEN
    ap = argparse.ArgumentParser(prog="pare", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=int, default=2000, help="output budget in tokens (~bytes/4); default 2000")
    ap.add_argument("--head", type=int, default=5, help="lines to keep from the start; default 5")
    ap.add_argument("--tail", type=int, default=10, help="lines to keep from the end; default 10")
    ap.add_argument("--width", type=int, default=200, help="clip lines longer than this; default 200")
    ap.add_argument("--chars-per-token", type=float, default=CHARS_PER_TOKEN, metavar="N",
                    help="bytes per token for the budget and the token estimate (default 4; measured "
                         "2.8 on common tokenizers, see docs)")
    ap.add_argument("--max-templates", type=int, default=0, metavar="N",
                    help="list at most N templates (highest salience first); 0 = all that fit. "
                         "Unlisted templates still appear in the omitted ledger")
    ap.add_argument("--only", metavar="T<n>[,T<m>]", help="print raw lines of these templates only")
    ap.add_argument("--except", dest="exclude", metavar="T<n>[,T<m>]", help="print raw lines except these templates")
    ap.add_argument("--since", metavar="L<n>",
                    help="with --only/--except (or alone): print raw lines from this line on; L3001, 3001, "
                         "or -200 for the last 200 lines")
    ap.add_argument("--until", metavar="L<n>", help="print raw lines up to and including this line (same forms)")
    ap.add_argument("--min-severity", choices=list(SEVERITY_BY_NAME), metavar="LEVEL",
                    help="with --only/--except (or alone, for all templates): print only records whose head "
                         "line carries a structural severity (log level, HTTP status, exit code) at or above "
                         "LEVEL: debug, info, warn, error or fatal")
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

    if args.chars_per_token <= 0:
        sys.exit("pare: --chars-per-token must be positive")
    CHARS_PER_TOKEN = args.chars_per_token
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

    if args.only or args.exclude or args.min_severity or args.since or args.until:
        out = select(
            lines, records, templates,
            only=parse_tids(args.only, len(templates)),
            exclude=parse_tids(args.exclude, len(templates)),
            min_severity=SEVERITY_BY_NAME[args.min_severity] if args.min_severity else None,
            since=parse_lineno(args.since, len(lines), "--since"),
            until=parse_lineno(args.until, len(lines), "--until"),
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
