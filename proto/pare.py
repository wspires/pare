#!/usr/bin/env python3
"""pare - summarize noisy command output (tests, builds, logs) to a token budget.

Pipeline
    stdin -> one pass: tee to the run file, split, segment, mask, Drain
             templates, line scores -> budget allocation -> summary on stdout

The pass keeps a few bytes per line and reads line text back from the run
file only for the lines the summary shows, so memory stays small on large
inputs (see Store).

Usage
    noisy-command 2>&1 | pare [--budget 2000]
    pare [--budget 2000] -- noisy-command args   # runs it; keeps its exit code
    pare --only T3,T7 < run-file        # raw lines of those templates
    pare --except T4 < run-file         # raw lines of everything but T4

Output is deterministic for a given input and flags: template ids (T1, T2,
...) are numbered in order of first appearance, and the run file name is a
content hash, so a later `pare --only T3 < runfile` sees the same ids.

One file, standard library only: run it with any Python 3.10 or newer.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import heapq
import json
import math
import os
import re
import select as selectmod
import signal
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field

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
CONT_RUSTC = (
    r"|^\s*\d*\s*\|(?:\s|$)"  # rustc/gcc gutter lines
    r"|^\s*-->\s"  # rustc location
    r"|^\s*= (?:note|help|warning):"  # rustc notes
    r"|^\s*::: "  # rustc secondary location
)
CONT_PYTEST = r"|^E\s{2,}|^>\s{2,}"  # pytest failure detail and source marker lines
CONT_TOOL = CONT_RUSTC + CONT_PYTEST
CONT_GENERIC_RE = re.compile(CONT_GENERIC)
CONT_RE = re.compile(CONT_GENERIC + CONT_TOOL)
TOOL_SPECIFIC = True  # all of the rules below; False switches every one off
# One switch per rule, for bench/ablate.py to measure each on its own.
TOOL_PYTEST = True  # E and > marker lines continue the failure block; a bare E is an error level
TOOL_RUSTC = True  # gutter, -->, = note and ::: lines continue a diagnostic; error[E0308]: is a level
TOOL_LOGCAT = True  # logcat E/Tag( and E/Tag:, glog E0921 levels
TOOL_BLANK_IN_BLOCK = True  # one blank line inside an indented block does not end the record


# A logcat line's header in the threadtime format ("10-13 09:12:31.620  4321
# 4321 E AndroidRuntime: ...") or the time format ("10-13 09:12:31.620
# E/AndroidRuntime( 4321): ..."). One Log call that prints several lines (a
# stack trace) repeats the same header, timestamp included, on every line.
LOGCAT_HEADER_RX = re.compile(
    r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(?:(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]*?)\s*:"
    r"|([VDIWEF])/([^(]*)\(\s*(\d+)\):)"
)


# ActivityManager reports every process exit at INFO, routine kills of
# cached apps included; the death of a process that just crashed is the end
# of the crash.
LOGCAT_DEATH_RX = re.compile(r"Process \S+ \(pid (\d+)\) has died")


def tool_rule(flag: bool) -> bool:
    return TOOL_SPECIFIC and flag


def continuation_rx() -> re.Pattern:
    return re.compile(CONT_GENERIC + (CONT_RUSTC if tool_rule(TOOL_RUSTC) else "")
                      + (CONT_PYTEST if tool_rule(TOOL_PYTEST) else ""))
TOOL_SPECIFIC_NOTES = [
    "pytest: E and > marker lines continue the failure block; a bare E at column 0 is an error level",
    "rustc/gcc: gutter (NN |), --> location, = note/help and ::: lines continue the diagnostic; error[E0308]: is a level",
    "logcat: E/Tag( and E/Tag: at the line start are levels, a multi-line entry (one header repeated) is split by its message, and the death of a pid that logged a FATAL EXCEPTION is an error; glog: E0921 10:11:12",
    "Jest/pytest: a single blank line inside an indented block does not end the record",
]

MAX_RECORD_LINES = 400


@dataclass
class Line:
    """One input line, materialised from the spool on demand (see Store)."""
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
    template: int = -1  # template index (0-based)


@dataclass
class Template:
    idx: int  # 0-based index in its store
    text: str
    cluster_id: int = -1  # Drain's id for the cluster
    gid: int = -1  # global id, shown as T{gid+1}; equals idx except in a follow window
    count: int = 0  # records (in this store: the whole input, or one follow window)
    total: int = 0  # records seen so far in the whole input (rarity uses this)
    new: bool = False  # first seen in this follow window
    rate_note: str = ""  # follow mode: " (rate 0.2/s → 11/s)" when a template bursts
    lines: int = 0
    salience: float = 0.0
    vocab: float = 0.0
    marker: str = "."
    levels: dict = field(default_factory=dict)  # severity -> number of records
    exemplar: int = -1  # record shown for this template: the first at the highest severity
    best_sev: int = -1  # that severity (-1: no structural level seen yet)

    @property
    def tid(self) -> str:
        return f"T{(self.gid if self.gid >= 0 else self.idx) + 1}"


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


def mask(text: str, words: bool = True) -> str:
    """Mask the variable parts of a line. words=False leaves identifiers
    with digits whole (test_case_3, testIssue635): for telling names apart,
    not for clustering."""
    t = text.strip()
    for name, rx in MASKS:
        if name == "WORDNUM":
            if not words:
                continue
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


DRAIN_SIM = 0.5  # 0.4 merges lines that share only a timestamp+level prefix
DRAIN_DEPTH = 4
DRAIN_MAX_CHILDREN = 100
WILDCARD = "<*>"


class Cluster:
    """One Drain template: its tokens (wildcards as <*>), drain's id, its size."""
    __slots__ = ("tokens", "cluster_id", "size")

    def __init__(self, tokens: list[str], cluster_id: int):
        self.tokens = tuple(tokens)
        self.cluster_id = cluster_id
        self.size = 1

    def get_template(self) -> str:
        return " ".join(self.tokens)


class _Node:
    __slots__ = ("children", "cluster_ids")

    def __init__(self):
        self.children: dict[str, _Node] = {}
        self.cluster_ids: list[int] = []


class Drain:
    """The Drain log-template miner (He et al., 2017), as drain3 0.9.11
    implements it (MIT; github.com/logpai/Drain3, drain3/drain.py), cut to
    what pare uses: no masking, persistence, cluster limit or extra
    delimiters. A prefix tree keyed on token count and the first tokens
    leads to candidate clusters; a message joins the most similar one at
    or above the similarity threshold, its differing tokens becoming <*>,
    or starts a new one. Template ids depend on clustering exactly as
    drain3 does, so a change here is a change to every T<n>; test_pare.py
    checks it against drain3 when drain3 is installed."""

    def __init__(self, depth: int = DRAIN_DEPTH, sim_th: float = DRAIN_SIM, max_children: int = DRAIN_MAX_CHILDREN):
        self.max_node_depth = depth - 2  # the root is level 1, the token count level 2
        self.sim_th = sim_th
        self.max_children = max_children
        self.root = _Node()
        self.clusters: dict[int, Cluster] = {}

    def add(self, message: str) -> Cluster:
        """Cluster one message (tokens split on whitespace) and return its cluster."""
        tokens = message.split()
        cluster = self._tree_search(tokens)
        if cluster is None:
            cluster = Cluster(tokens, len(self.clusters) + 1)
            self.clusters[cluster.cluster_id] = cluster
            self._add_to_tree(cluster)
        else:
            new = tuple(t if t == m else WILDCARD for t, m in zip(cluster.tokens, tokens))
            if new != cluster.tokens:
                cluster.tokens = new
            cluster.size += 1
        return cluster

    def _tree_search(self, tokens: list[str]) -> Cluster | None:
        n = len(tokens)
        node = self.root.children.get(str(n))
        if node is None:
            return None
        if n == 0:
            return self.clusters.get(node.cluster_ids[0])
        depth = 1
        for token in tokens:
            if depth >= self.max_node_depth or depth == n:
                break
            nxt = node.children.get(token)
            if nxt is None:
                nxt = node.children.get(WILDCARD)
            if nxt is None:
                return None
            node = nxt
            depth += 1
        # The most similar candidate (ties: more wildcards); wildcards do not count as matches.
        best, best_sim, best_params = None, -1.0, -1
        for cid in node.cluster_ids:
            c = self.clusters[cid]
            same = params = 0
            for t, m in zip(c.tokens, tokens):
                if t == WILDCARD:
                    params += 1
                elif t == m:
                    same += 1
            sim = same / len(c.tokens)
            if sim > best_sim or (sim == best_sim and params > best_params):
                best, best_sim, best_params = c, sim, params
        return best if best_sim >= self.sim_th else None

    def _add_to_tree(self, cluster: Cluster) -> None:
        n = len(cluster.tokens)
        node = self.root.children.setdefault(str(n), _Node())
        if n == 0:
            node.cluster_ids = [cluster.cluster_id]
            return
        depth = 1
        for token in cluster.tokens:
            if depth >= self.max_node_depth or depth >= n:
                node.cluster_ids.append(cluster.cluster_id)
                break
            children = node.children
            if token in children:
                node = children[token]
            elif any(ch.isdigit() for ch in token):  # a token with a digit is a parameter
                node = children.setdefault(WILDCARD, _Node())
            elif WILDCARD in children:
                if len(children) < self.max_children:
                    node = children[token] = _Node()
                else:
                    node = children[WILDCARD]
            elif len(children) + 1 < self.max_children:
                node = children[token] = _Node()
            elif len(children) + 1 == self.max_children:
                node = children[WILDCARD] = _Node()
            else:
                node = children[WILDCARD]
            depth += 1


def drain_message(head_text: str) -> str:
    return " ".join(mask(head_text).split()[:DRAIN_MAX_TOKENS]) or "<EMPTY>"


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
    "advice": (-1.0, "how to get more output or help: re-run with -e, see the docs (the whole score, whatever the level)"),
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
        r"/[A-Z]\w*(?:Error|Exception|Fault|Panic|Failure)\b/",  # CamelCase exception names (JUnit's ComparisonFailure)
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
    "advice": [
        "re-run", "rerun", "for more information", "for more details", "please refer to",
        "to see the full", r"/(?i)\brun with --/", "get more help", "resume the build", "a complete log of this run",
        "for the individual test results", "see dump files", "after correcting the problems",
        r"/\[Help \d+\]/",
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
        letters = r"\[([EWIDVTF])\]"  # [I]
        if tool_rule(TOOL_LOGCAT):
            letters += (
                r"|^(?:[\d:.\-]{5,24}\s+){0,2}(?:\d+\s+\d+\s+)?([EWIDVTF])/[\w.$]+[(:]"  # logcat E/Tag( or E/Tag:
                r"|^(?:[\d:.\-]{5,24}\s+){1,2}\d+\s+\d+\s+([EWIDVF])\s+\S+\s*:"  # logcat threadtime: date time pid tid E Tag: (short tags padded to 8)
                r"|^([EWIF])\d{4}\s"  # glog E0921 10:11:12
            )
        if tool_rule(TOOL_PYTEST):
            letters += r"|^(E)\s{2,}"  # pytest
        # Built from the rule switches when the vocabulary is built.
        self.level_letter_rx = re.compile(letters)

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
                or (tool_rule(TOOL_RUSTC) and start == 0 and nxt == "[")  # rustc error[E0308]:
            )
            if ok:
                return self.level_of[tok.upper()]
        m = self.level_letter_rx.search(text[:80])
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


LEVEL_ONLY_RX = re.compile(r"^\W*(?:" + "|".join(sorted({t for c in LEVEL_CLASSES for t in DEFAULT_VOCAB[c]}, key=len, reverse=True))
                           + r")\W*$", re.I)


# A call-stack frame: Java, JavaScript and C# "at pkg.Cls.method(...)",
# Python 'File "x.py", line 3, in f'. Its words are code names, so with
# STACK_FRAME_NAMES on, its error and warn words do not count.
STACK_FRAME_NAMES = True
STACK_FRAME_RX = re.compile(
    r'^\s*(?:at\s+(?:async\s+|new\s+)?[\w$.<>\[\]/:@-]+\s*(?:\(|$|\sin\s)|File "[^"]*", line \d+)'
)


def vocab_score(text: str, head: bool = True, sev=_UNSET, has=None) -> float:
    """Salience of one line's text. A structural log level, when present,
    sets the base score and decides whether error/warn words count: an INFO
    line that mentions 'error' is still info, and an ERROR line is an error
    whatever language its message is in. Summary vocabulary (totals, final
    verdicts) only counts on a record head, not inside a block."""
    if has is None:
        has = VOCAB.has
    s = 0.0
    err = has("error", text)  # Vocab.has strips zero counts ("0 errors") itself
    warn = has("warn", text)
    if sev is _UNSET:
        sev = VOCAB.severity(text)
    if sev is None:
        if has("pass", text):
            # A passing-test marker is structural: the words of a test name
            # ("throws on invalid") are not log vocabulary.
            s += VOCAB_CLASSES["pass"][0]
        else:
            if (err or warn) and STACK_FRAME_NAMES and STACK_FRAME_RX.match(text):
                # junit.framework.Assert.fail, test_raises_error: the words of
                # a frame's function name say nothing about what happened.
                err = warn = False
            if err:
                s += VOCAB_CLASSES["error"][0]
            if warn:
                s += VOCAB_CLASSES["warn"][0]
            if not err and has("noise", text):
                s += VOCAB_CLASSES["noise"][0]
    else:
        s += LEVEL_SCORE[sev]
        if sev >= 2:  # vocabulary reinforces a warning or error level
            if err:
                s += 0.5 * VOCAB_CLASSES["error"][0]
            if warn:
                s += 0.5 * VOCAB_CLASSES["warn"][0]
    if has("advice", text):
        # A build tool's closing advice ("re-run Maven with the -e switch")
        # is printed at error level on every failure; it is not the failure,
        # so its level does not count.
        return VOCAB_CLASSES["advice"][0]
    if sev is not None and LEVEL_ONLY_RX.match(text):
        return -1.0  # "[ERROR]" alone: a level with no message says nothing
    if head and has("summary", text):
        s += VOCAB_CLASSES["summary"][0]
    if has("frame", text):
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
    return 1.0 / (1.0 + math.log2(tpl.total))


def score_templates(store: Store) -> None:
    """Template salience and marker, judged by the exemplar record: the first
    record at the template's highest structural severity, so a template of
    4,000 INFO lines and 3 ERROR lines is represented by an ERROR line. The
    exemplar head is a real line with the full per-line analysis, rather than
    the masked template text (where "0 errors" has already become "<NUM>
    errors"), so its severity is the worst the template has seen."""
    for tpl in store.templates:
        head = store.head_of(tpl.exemplar)
        tpl_level = line_sev(head)
        head_vocab = line_vocab(head, head=True)
        body_err = 0.0
        for ln in store.lines(*store.body_range(tpl.exemplar)):
            if line_has(ln, "strong-error"):
                body_err = 1.0
                break
        # Salience: what the head line says, plus how unusual the shape is.
        # (Position, block-size and body-error bonuses were ablated away:
        # they changed nothing at any budget. See bench/ablate.py.)
        tpl.vocab = head_vocab
        # A warning's rarity counts for less: a one-off build warning should
        # not outrank a failure seen twice (two tests failing the same way).
        tpl.salience = head_vocab + RARITY_WEIGHT * rarity_of(tpl) * (WARN_RARITY if tpl_level == 2 else 1.0)
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


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
NEAR_DUP_MIN_TOKENS = 4
NEAR_DUP_CONTAINMENT = 0.8


def token_set(text: str) -> frozenset[str]:
    return frozenset(TOKEN_RE.findall(text))


def is_near_duplicate(toks: frozenset[str], shown: list[frozenset[str]], names: bool = False,
                      name_toks: frozenset[str] = frozenset()) -> bool:
    """True when at least NEAR_DUP_CONTAINMENT of the line's tokens already
    appear in one line on screen. Short lines are exempt: too easy to contain.
    With names=True (an error line) a line is a duplicate only when what it
    adds is numbers: "FAILED t.py::test_a" and "FAILED t.py::test_b" share
    four of five tokens, but the fifth is the news. A number in name_toks
    (from a name's bracketed parameters, test_x[3] after test_x[2]) is news
    too."""
    if len(toks) < NEAR_DUP_MIN_TOKENS:
        return False
    need = NEAR_DUP_CONTAINMENT * len(toks)
    for s in shown:
        if len(toks & s) >= need and (not names or all(t.isdigit() and t not in name_toks for t in toks - s)):
            return True
    return False


def is_chatter(ln: Line) -> bool:
    """A line that says nothing about what happened: a build tool's closing
    advice ("re-run Maven with the -e switch", "[Help 1]") or a level with no
    message ("[ERROR]"). Both score under the floor; the head and tail
    windows, which take lines by position, skip them too."""
    return line_has(ln, "advice") or (line_sev(ln) is not None and LEVEL_ONLY_RX.match(ln.text) is not None)


def is_error_line(ln: Line) -> bool:
    """A line that reports a failure: error level or above, or, with no level,
    error vocabulary and no pass marker. A level wins over the vocabulary: a
    Warning event saying "probe failed" is a warning."""
    sev = line_sev(ln)
    if sev is not None:
        return sev >= 3
    return line_has(ln, "error") and not line_has(ln, "pass")


# A bracketed parameter list right after a name: pytest's test_x[numpy-3-1],
# JUnit 4's testFoo[2]. Its digits are part of the name.
PARAMS_RX = re.compile(r"(?<=[\w)])\[([^\[\]\s]{1,100})\]")


def line_params(ln: Line) -> tuple[str, ...]:
    """The bracketed parameters of a name on this line that hold a digit,
    computed once. test_rolling_iter[numpy-3-True-1] and [numpy-3-True-2]
    mask to one shape; these say which test it is."""
    m = ln.memo
    if "params" not in m:
        m["params"] = tuple(p for p in PARAMS_RX.findall(ln.text) if any(c.isdigit() for c in p))
    return m["params"]


def name_key(ln: Line) -> str:
    """shape_key with identifiers left whole: test_endpoint_get_0 and
    test_endpoint_get_1 are two tests, where worker3 and worker7 masked to
    one shape are one kind of line. Used only to judge and key lists of
    failures, where the template's distinct-shape ratio keeps the same few
    workers, repeated, from counting as a list."""
    m = ln.memo
    if "name" not in m:
        sev = line_sev(ln)
        params = line_params(ln)
        m["name"] = (mask(ln.text, words=False) + (f" ·sev{sev}" if sev is not None else "")
                     + (" ·[" + "][".join(params) + "]" if params else ""))
    return m["name"]


def shape_key(ln: Line) -> str:
    """Dedup key for the fill phase: the masked shape plus the structural
    severity, so a 503 request line is not a duplicate of a 200 one even
    though masking turns both status codes into <NUM>. A name's bracketed
    parameters are kept as written: two parametrizations of one test are
    two tests."""
    sev = line_sev(ln)
    params = line_params(ln)
    return (line_mask(ln) + (f" ·sev{sev}" if sev is not None else "")
            + (" ·[" + "][".join(params) + "]" if params else ""))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


WINDOW_SKIP_COUNT = 5  # head/tail windows skip lines of templates this common
WINDOW_SCAN_FACTOR = 3  # ...looking at most this many window-lengths into the file
NEAR_DUP_MAX_COUNT = 5  # templates this small are skipped when their exemplar restates a shown line
# Scoring weights. Each one survived bench/ablate.py: switching it off lowers
# recall at some budget or grows the output at the same recall.
RARITY_WEIGHT = 2.0  # template salience: weight of 1/(1+log2 count)
WARN_RARITY = 0.75  # ...times this for a template at warning level
INHERIT = 0.6  # share of the record head's vocabulary a continuation line inherits
DEPTH_PENALTY = 0.25  # per level of indentation depth, up to 4
LOCATION_BONUS = 1.5  # the first source location after an error line in a record
DENSITY_REF = 120  # bytes; a raw line's priority is scaled by (REF / (REF + len))^POWER
DENSITY_POWER = 0.35
FILL_PENALTY = 1.5  # divisor growth per already-shown line of the same repeat key
FILL_FLOOR = 1.0  # lines scoring below this never pad the output
TEMPLATE_FLOOR = 1.5  # template entries below this salience stay in the ledger (the first entry is exempt)
LIST_DISTINCT = 0.5  # a template whose records are at least this share distinct shapes is a list of different items
LIST_SAMPLE = 200  # ...judged on its first this many records
LIST_MIN_COUNT = 3  # a list of failures this long is rendered compactly: shared prefix once, one item per line
LIST_MIN_SHARED = 16  # ...when the shared prefix and suffix save at least this many bytes per item
LIST_ITEM = "  …"  # an item line: the list header's text before {…}, then this item
LIST_CUT = " :/.#("  # the shared prefix ends after one of these
RARE_FAILURE_BONUS = 2.0  # salience added to a listed failure that fails unlike most of the list
RARE_FAILURE_SHARE = 0.1  # ...when at most this share of the list fails the same way
RARE_FAILURE_MIN = 5  # ...in a list of at least this many failures with a known way of failing
SECTION_MAX = 400  # lines a failure's section is followed for before giving up
PAIR_MEMBERS = True  # a template seen twice lists both lines instead of its pattern and one example
COMPACT_BUDGET = 1000  # below this many tokens, multi-record entries are one line
HINT_BUDGET = 1000  # below this, the usage hint at the end is dropped


CHARS_PER_TOKEN = 2.8  # bytes per token, measured on cl100k/o200k (bench/tokens.py); --chars-per-token overrides


def tokens(nbytes: int) -> int:
    """Token estimate: bytes / CHARS_PER_TOKEN. A proxy, not a tokenizer; the
    default 2.8 is what pare output measures on cl100k/o200k (bench/tokens.py).
    --chars-per-token 4 gives the classic bytes/4 rule of thumb."""
    # Less a hair, so that 2,800 bytes at 2.8 is 1,000 tokens, not the 1,001
    # that floating point makes of 2800 / 2.8.
    return math.ceil(nbytes / CHARS_PER_TOKEN - 1e-9)


def budget_bytes_for(budget: int) -> int:
    return int(budget * CHARS_PER_TOKEN)


def fmt_int(n: int) -> str:
    return f"{n:,}"


def clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    cut = len(text) - width + 1
    marker = f"…(+{cut}c)"
    if len(marker) >= cut:
        return text  # the marker would be no shorter than what it replaces
    return text[: width - 1].rstrip() + marker


def fmt_line(ln: Line, width: int) -> str:
    # "223: " rather than a padded "   223│ ": the gutter is on every shown
    # line, and this form is 4 % fewer tokens over the corpus (bench/tokens.py).
    return f"{ln.no}: {clip(ln.text, width)}"


def gap_marker(gap: int) -> str:
    return f"  ┆ ({fmt_int(gap)} line{'s' if gap != 1 else ''})"


def render_ranges(store: Store, nos: list[int], width: int) -> list[str]:
    """Render line numbers in order with gap markers between non-adjacent runs."""
    out = []
    prev = None
    for no in nos:
        if prev is not None and no != prev + 1:
            out.append(gap_marker(no - prev - 1))
        out.append(fmt_line(store.line(store.index(no)), width))
        prev = no
    return out


def nbytes(parts: list[str]) -> int:
    return sum(len(p.encode("utf-8")) + 1 for p in parts)


# --------------------------------------------------------------------------
# The single pass
# --------------------------------------------------------------------------
#
# Input is read once, in chunks. Each chunk goes to the spool (the run file,
# or a temporary file under --no-tee) and is split into lines, and each line
# is segmented, clustered and scored as it arrives. What the pass keeps is a
# few typed arrays per line and per record (about 40 bytes a line), the
# templates, and each line's offset in the spool; a line's text is read back
# from the spool when the allocator actually looks at it. Memory is
# proportional to the number of lines rather than their size, and the raw
# bytes are on disk the moment they arrive.

CHUNK = 1 << 20
LINE_CACHE = 4096  # Line objects kept materialised at once


class Store:
    def __init__(self, spool_path: str, width: int, base: int = 0):
        self.spool_path = spool_path
        self.width = width
        self.base = base  # index of this store's first line in the input (a follow window)
        # per line
        self.off = array.array("q")  # byte offset of the raw line in the spool
        self.raw_len = array.array("i")  # its length, without the newline
        self.rec = array.array("i")  # record index; -1 for a blank line outside any record
        self.depth = array.array("b")
        self.sev = array.array("b")  # structural severity; -1 for none
        self.blank = array.array("b")
        self.rlen = array.array("i")  # bytes of the rendered line (fmt_line) plus its newline
        self.slen = array.array("i")  # characters after strip()
        self.score = array.array("d")  # line salience: line score plus the location bonus
        # per record
        self.rstart = array.array("i")
        self.rend = array.array("i")
        self.rtpl = array.array("i")
        self.templates: list[Template] = []
        self.carry: CarriedHead | None = None  # record 0 continues a record cut by the previous follow window
        self.total_bytes = 0
        self.verbatim_bytes = 0  # the cleaned text with one newline per line
        self.blank_total = 0
        self._fh = None
        self._cache: OrderedDict[int, Line] = OrderedDict()

    def __len__(self) -> int:
        return len(self.off)

    @property
    def nrecords(self) -> int:
        return len(self.rstart)

    def open(self) -> None:
        self._fh = open(self.spool_path, "rb")
        self._own_fh = True

    def use(self, fh) -> None:
        """Read through a handle someone else owns (follow mode shares one)."""
        self._fh = fh
        self._own_fh = False

    def close(self) -> None:
        if self._fh is not None and getattr(self, "_own_fh", False):
            self._fh.close()
        self._fh = None

    def no_of(self, i: int) -> int:
        return self.base + i + 1

    def head_of(self, ri: int) -> Line:
        """The head line of a record; for a record continued from the previous
        window, the head it had there."""
        if ri == 0 and self.carry is not None:
            c = self.carry
            ln = Line(no=c.no, text=c.text, indent=indent_of(c.text), record=0, depth=0)
            ln.memo["sev"] = c.sev
            return ln
        return self.line(self.rstart[ri])

    def body_range(self, ri: int) -> tuple[int, int]:
        """Indices of a record's continuation lines in this store."""
        start = self.rstart[ri]
        return (start if ri == 0 and self.carry is not None else start + 1), self.rend[ri]

    def index(self, no: int) -> int:
        return no - self.base - 1

    def record(self, ri: int) -> Record:
        return Record(ri, self.rstart[ri], self.rend[ri], self.rtpl[ri])

    def sev_of(self, i: int) -> int | None:
        s = self.sev[i]
        return s if s >= 0 else None

    def raw(self, start: int, end: int) -> list[bytes]:
        """Raw bytes of lines [start, end), read from the spool in one go."""
        if start >= end:
            return []
        last = end - 1
        self._fh.seek(self.off[start])
        data = self._fh.read(self.off[last] + self.raw_len[last] - self.off[start])
        return data.split(b"\n")

    def text(self, i: int) -> str:
        ln = self._cache.get(i)
        if ln is not None:
            return ln.text
        return clean_line(self.raw(i, i + 1)[0].decode("utf-8", errors="replace"))

    def _make(self, i: int, raw: bytes) -> Line:
        text = clean_line(raw.decode("utf-8", errors="replace"))
        ln = Line(no=self.base + i + 1, text=text, indent=indent_of(text), record=self.rec[i], depth=self.depth[i])
        ln.memo["sev"] = self.sev_of(i)
        self._cache[i] = ln
        if len(self._cache) > LINE_CACHE:
            self._cache.popitem(last=False)
        return ln

    def line(self, i: int) -> Line:
        ln = self._cache.get(i)
        if ln is not None:
            self._cache.move_to_end(i)
            return ln
        return self._make(i, self.raw(i, i + 1)[0])

    def lines(self, start: int, end: int) -> list[Line]:
        raws = self.raw(start, end)
        out = []
        for k, i in enumerate(range(start, end)):
            ln = self._cache.get(i)
            if ln is None:
                ln = self._make(i, raws[k])
            else:
                self._cache.move_to_end(i)
            out.append(ln)
        return out


class Pass:
    """The single pass as an object: feed it chunks, cut windows off it.

    Batch mode feeds the whole input and cuts one window at the end. Follow
    mode cuts a window at each flush. The Drain miner, the cumulative
    per-template counts, the line counter and the spool position carry
    across windows; the per-line arrays live in the current window's Store.
    """

    def __init__(self, width: int, spool, spool_path: str, hasher=None):
        self.width = width
        self.spool = spool
        self.spool_path = spool_path
        self.hasher = hasher
        self.drain = Drain()
        self.by_cluster: dict[int, GlobalTemplate] = {}
        self.globals: list[GlobalTemplate] = []
        self.total_bytes = 0
        self.nlines = 0
        self.pos = 0  # spool offset of the next line
        self.rest = b""  # bytes after the last newline, not yet a line
        self.window_no = 0
        self.started = time.monotonic()
        self.worst_sev = -1  # highest severity seen in the current window (follow triggers)
        self.cont_rx = continuation_rx()
        self.every = 0  # follow: window cap in lines; on_full is called when it is reached
        self.on_full = None
        self.carry: CarriedHead | None = None  # the record open at the last cut, until a line ends it
        self.carry_len = 0  # lines it already had
        self.head_text = ""
        self.head_no = 0
        self.head_sev: int | None = None
        self.logcat_key: tuple | None = None  # the logcat header of the last non-blank line
        self.crashed_pids: set[str] = set()  # logcat: pids that logged a FATAL EXCEPTION
        self.cur = -1
        self.head_indent = 0
        self.head_vocab = 0.0
        self.armed = False
        self.pending_blank = -1
        self.pending_blank_at_cut = False
        self.pending_carried = False
        self._new_window()

    def _new_window(self) -> None:
        self.store = Store(self.spool_path, self.width, base=self.nlines)
        self.window_tpl: dict[int, Template] = {}  # gid -> this window's Template
        self.window_started = time.monotonic()
        self.worst_sev = -1
        # Segmentation state. A record open at a cut is carried: head_indent,
        # head_vocab and armed keep their values, and the first lines of the
        # new window may continue it (see _add); only cur is reset, since the
        # record's lines so far belong to the previous store.
        if self.cur != -1:
            self.carry = CarriedHead(no=self.head_no, text=self.head_text, sev=self.head_sev,
                                     tpl_idx=-1, gid=self.carry_gid, cluster_id=self.carry_cid)
            self.carry_len = self.store_prev_len
            self.pending_carried = self.pending_blank_at_cut
        elif self.carry is not None:
            # No line of the window ended the carried record (the window held
            # only the blank line that keeps it open): carry on unchanged,
            # length and pending-blank state included.
            pass
        else:
            self.carry = None
            self.carry_len = 0
            self.head_indent = 0
            self.head_vocab = 0.0
            self.armed = False
            self.pending_carried = False
        self.cur = -1
        self.pending_blank = -1  # a single blank line that may turn out to sit inside a block

    def feed(self, chunk: bytes) -> None:
        self.spool.write(chunk)
        if self.hasher is not None:
            self.hasher.update(chunk)
        self.total_bytes += len(chunk)
        parts = (self.rest + chunk).split(b"\n")
        self.rest = parts.pop()
        for part in parts:
            self._add(part)

    def finish(self) -> None:
        """End of input: the trailing partial line, if any, is a line."""
        if self.rest:
            self._add(self.rest)
            self.rest = b""

    @property
    def has_content(self) -> bool:
        """Whether the current window holds a non-blank line."""
        return len(self.store) > self.store.blank_total

    def cut(self) -> Store:
        """Close the current window and return it, ready to summarise."""
        self.spool.flush()
        store = self.store
        if self.cur != -1:
            self.store_prev_len = store.rend[self.cur] - store.rstart[self.cur] + self.carry_len
            self.carry_gid = store.templates[store.rtpl[self.cur]].gid
            self.carry_cid = store.templates[store.rtpl[self.cur]].cluster_id
        self.pending_blank_at_cut = self.pending_blank != -1
        store.total_bytes = (self.pos - store.off[0]) if len(store) else 0
        for tpl in store.templates:
            g = self.globals[tpl.gid]
            c = self.drain.clusters.get(tpl.cluster_id)
            if c is not None:
                tpl.text = c.get_template()
            tpl.total = g.total
            g.windows += 1
        self.window_no += 1
        self._rate_notes(store)
        self._new_window()
        return store

    def _rate_notes(self, store: Store) -> None:
        """A template arriving much faster than its running average is what
        an outage looks like before the first ERROR line. Needs real time on
        both sides (a replayed file gets no notes), so this is deterministic
        for tests and informative on a live stream."""
        now = time.monotonic()
        window_s = now - self.window_started
        before_s = self.window_started - self.started
        if window_s < 1.0 or before_s < 1.0:
            return
        for tpl in store.templates:
            before = tpl.total - tpl.count
            if tpl.count < RATE_MIN_COUNT or before < 1:
                continue
            now_r, before_r = tpl.count / window_s, before / before_s
            if now_r >= RATE_FACTOR * before_r:
                tpl.rate_note = f" (rate {before_r:.2g}/s → {now_r:.2g}/s)"

    def _joins(self, text: str, indent: int, logcat: tuple | None, message: str) -> bool:
        """Whether a line continues the open record, by its shape: deeper
        indentation or a known continuation form. A logcat line is judged by
        its message, since the header in front of every line means nothing
        in it is indented, and only within one entry: one Log call printed
        over several lines repeats the same header, so a new header always
        starts a new record. Inside an entry ("FATAL EXCEPTION: main",
        "Process: ...", the exception, its frames) the exception heads its
        record and the frames continue it, as in any Java trace."""
        if logcat is not None:
            return logcat == self.logcat_key and (indent_of(message) > self.head_indent
                                                  or bool(self.cont_rx.match(message)))
        return indent > self.head_indent or bool(self.cont_rx.match(text))

    def _add(self, raw: bytes) -> None:
        text = clean_line(raw.decode("utf-8", errors="replace"))
        indent = indent_of(text)
        stripped = text.strip()
        logcat = None
        message = text
        if tool_rule(TOOL_LOGCAT):
            m = LOGCAT_HEADER_RX.match(text)
            if m:
                logcat = m.groups()
                message = text[m.end():].removeprefix(" ")
        if self.every and self.on_full is not None and stripped and len(self.store) >= self.every and not (
            self.cur != -1
            and (self.store.rend[self.cur] - self.store.rstart[self.cur]) < MAX_RECORD_LINES
            and self._joins(text, indent, logcat, message)
        ):
            # The window is full and this line starts a record: cut here, so
            # a window boundary never splits a record and the segmentation,
            # and with it the template ids, match a batch pass over the run
            # file. A window can exceed --every by one record.
            self.on_full()
        store = self.store
        i = len(store)
        store.off.append(self.pos)
        store.raw_len.append(len(raw))
        self.pos += len(raw) + 1
        self.nlines += 1
        store.verbatim_bytes += len(text.encode("utf-8")) + 1
        store.rlen.append(len(f"{store.base + i + 1}: {clip(text, self.width)}".encode("utf-8")) + 1)
        store.slen.append(len(stripped))
        sev = VOCAB.severity(text)
        if logcat is not None:
            pid = logcat[1] or logcat[7]  # threadtime or time format
            if message.startswith("FATAL EXCEPTION"):
                self.crashed_pids.add(pid)
            elif self.crashed_pids:
                m = LOGCAT_DEATH_RX.search(message)
                if m and m.group(1) in self.crashed_pids:
                    sev = max(3, sev or 0)  # the crashed process is gone: part of the failure, not routine
        store.sev.append(-1 if sev is None else sev)
        if sev is not None and sev > self.worst_sev:
            self.worst_sev = sev
        if not stripped:
            # Blank lines end a record and belong to none, except that a
            # single blank line may sit inside an indented block.
            store.blank_total += 1
            self.logcat_key = None
            store.blank.append(1)
            store.rec.append(-1)
            store.depth.append(0)
            store.score.append(0.0)
            if self.carry is not None and self.cur == -1:
                # Same rule for the carried record: one blank keeps it open.
                if self.pending_carried or not tool_rule(TOOL_BLANK_IN_BLOCK):
                    self.carry = None
                else:
                    self.pending_carried = True
                return
            if self.pending_blank != -1 or self.cur == -1 or not tool_rule(TOOL_BLANK_IN_BLOCK):
                self.cur = -1
            self.pending_blank = i if self.cur != -1 else -1
            return
        store.blank.append(0)
        memo: dict[str, bool] = {}
        vocab_has = VOCAB.has

        def has(cls: str, _text: str = "") -> bool:  # Vocab.has, memoised for this line
            v = memo.get(cls)
            if v is None:
                v = memo[cls] = vocab_has(cls, text)
            return v

        cur = self.cur
        rstart, rend, rtpl = store.rstart, store.rend, store.rtpl
        if self.carry is not None and cur == -1:
            # First non-blank line of a window after a cut that left a record
            # open: does it continue that record? Same test as below, against
            # the carried head and the record's length so far.
            if self.carry_len < MAX_RECORD_LINES and self._joins(text, indent, logcat, message):
                g = self.globals[self.carry.gid]
                tpl = self.window_tpl.get(g.gid)
                if tpl is None:
                    tpl = Template(idx=len(store.templates), text="", cluster_id=self.carry.cluster_id, gid=g.gid,
                                   exemplar=0, new=False)
                    store.templates.append(tpl)
                    self.window_tpl[g.gid] = tpl
                self.carry.tpl_idx = tpl.idx
                store.carry = self.carry
                cur = self.cur = len(rstart)  # record 0 of this store
                rstart.append(i)
                rend.append(i)  # the continuation below extends it
                rtpl.append(tpl.idx)
                tpl.count += 1  # a record this window touches; the cumulative total is unchanged
                if self.carry.sev is not None:
                    tpl.levels[self.carry.sev] = tpl.levels.get(self.carry.sev, 0) + 1
                tpl.best_sev = self.carry.sev if self.carry.sev is not None else -1
            self.carry = None
        # Segmentation: a deeper indent or a known continuation shape joins
        # the line to the record above.
        is_cont = (
            cur != -1
            and (rend[cur] - rstart[cur] + (self.carry_len if cur == 0 and store.carry is not None else 0)) < MAX_RECORD_LINES
            and self._joins(text, indent, logcat, message)
        )
        self.logcat_key = logcat
        if is_cont:
            if logcat is not None:
                # The entry's level is its head's; a line of its message is
                # judged by the message alone (a frame is a frame, not an
                # error line) and gets the entry's weight by inheritance.
                text = message
                sev = VOCAB.severity(text)
                store.sev[i] = -1 if sev is None else sev
                memo.clear()
            tpl = store.templates[rtpl[cur]]
            if self.pending_blank != -1:
                store.rec[self.pending_blank] = cur
                store.depth[self.pending_blank] = 1
                tpl.lines += 1
            rend[cur] = i + 1
            d = max(1, ((indent_of(message) if logcat is not None else indent) - self.head_indent + 1) // 2)
            store.rec.append(cur)
            store.depth.append(d)
            tpl.lines += 1
            # A continuation line: its own vocabulary plus a share of its
            # record head's, less a little per level of depth.
            s = vocab_score(text, head=False, sev=sev, has=has)
            s -= DEPTH_PENALTY * min(d, 4)
            if not (has("pass") and not has("error")):
                s += INHERIT * max(0.0, self.head_vocab)
        else:
            cur = self.cur = len(rstart)
            rstart.append(i)
            rend.append(i + 1)
            self.head_indent = indent_of(message) if logcat is not None else indent
            self.head_text, self.head_no, self.head_sev = text, store.base + i + 1, sev
            # Cluster the masked head line.
            cluster = self.drain.add(drain_message(text))
            cid = cluster.cluster_id
            g = self.by_cluster.get(cid)
            if g is None:
                g = GlobalTemplate(gid=len(self.globals), cluster_id=cid, first_window=self.window_no)
                self.globals.append(g)
                self.by_cluster[cid] = g
            g.total += 1
            tpl = self.window_tpl.get(g.gid)
            if tpl is None:
                tpl = Template(idx=len(store.templates), text=cluster.get_template(), cluster_id=cid, gid=g.gid,
                               exemplar=cur, new=g.first_window == self.window_no)
                store.templates.append(tpl)
                self.window_tpl[g.gid] = tpl
            rtpl.append(tpl.idx)
            tpl.count += 1
            tpl.lines += 1
            if sev is not None:
                tpl.levels[sev] = tpl.levels.get(sev, 0) + 1
            if (sev if sev is not None else -1) > tpl.best_sev:
                tpl.best_sev = sev if sev is not None else -1
                tpl.exemplar = cur
            store.rec.append(cur)
            store.depth.append(0)
            self.armed = False
            self.head_vocab = s = vocab_score(text, head=True, sev=sev, has=has)
        self.pending_blank = -1
        # The first source location after an error line within a record (the
        # 'at file:line' under an exception, the '-->' under a rustc error,
        # the innermost frame) says where the failure is; the frames after it
        # say how it got there.
        is_loc = has("frame") and not text.lstrip().startswith(">")
        if self.armed and is_loc:
            s += LOCATION_BONUS
            self.armed = False
        elif (sev is not None and sev >= 3) or (has("error") and not is_loc):
            self.armed = True
        store.score.append(s)


@dataclass
class CarriedHead:
    """The head of a record that a follow cut left open: what the next
    window needs to attribute its continuation lines and to show the entry."""
    no: int
    text: str
    sev: int | None
    tpl_idx: int  # index in the new window's templates
    gid: int
    cluster_id: int


@dataclass
class GlobalTemplate:
    """What a template is across follow windows: its id, its cluster and
    how many records it has had in the whole input so far."""
    gid: int
    cluster_id: int
    first_window: int
    total: int = 0
    windows: int = 0


RATE_MIN_COUNT = 20  # a rate note needs this many records in the window
RATE_FACTOR = 3.0  # ...arriving at least this many times the running rate


def analyze(stream, *, width: int, spool_path: str) -> tuple[Store, str]:
    """Run the pass over a byte stream, spooling it to spool_path. Returns the
    store and the content hash that names the run file."""
    hasher = hashlib.sha256()
    with open(spool_path, "wb") as spool:
        p = Pass(width, spool, spool_path, hasher)
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            p.feed(chunk)
        p.finish()
        store = p.cut()
    store.total_bytes = p.total_bytes
    return store, hasher.hexdigest()[:12]


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

NOVELTY_BONUS = 1.0  # follow mode: salience added to a template first seen in this window

# Terms removed by ablation on the dev corpus (commit 95c77c6), kept at
# weight 0 so bench/ablate.py can re-test them on new data ("with ..." rows).
# The weights they had are in the comments.
END_TPL_BONUS = 0.0  # template salience: its last record in the end zone (0.5), all of them (twice that)
START_TPL_BONUS = 0.0  # template salience: its first record in the start zone (0.4)
END_LINE_BONUS = 0.0  # line score: the line is in the end zone (0.5)
FIRST_BONUS = 0.0  # line score: first record of its template, or first line of its shape at its depth in it (0.5)
END_ZONE_FRACTION, END_ZONE_MAX = 0.1, 60  # the end zone: the last 10 % of records (or lines), at most 60
START_ZONE_FRACTION, START_ZONE_MAX = 0.05, 30  # the start zone: the first 5 %, at most 30
ZONE_MIN = 10  # a zone is never shorter than this


def zone_len(n: int, fraction: float, cap: int) -> int:
    return max(min(n, ZONE_MIN), min(int(n * fraction), cap))


@dataclass
class FollowInfo:
    k: int  # window number, from 1
    quiet: bool = False  # drop heartbeat lines for windows with nothing new


def summarize(
    store: Store,
    *,
    budget: int,
    head_n: int,
    tail_n: int,
    width: int,
    max_templates: int = 0,
    run_path: str | None,
    in_bytes: int,
    follow: FollowInfo | None = None,
    note: str | None = None,
) -> str:
    templates = store.templates
    budget_bytes = budget_bytes_for(budget)
    total = len(store)
    b1 = store.base + 1  # line number of index 0
    rec, rtpl, rstart, rend, blank = store.rec, store.rtpl, store.rstart, store.rend, store.blank
    scores, slen, rlen = store.score, store.slen, store.rlen
    later_window = follow is not None and follow.k > 1
    if later_window:
        # A shape first seen in this window is news; the batch summary has
        # no "before" to be new against, so this is follow-only.
        for tpl in templates:
            if tpl.new:
                tpl.salience += NOVELTY_BONUS
    if END_TPL_BONUS or START_TPL_BONUS:  # ablation candidates, off by default
        nrec = max(1, store.nrecords)
        end_zone = nrec - zone_len(nrec, END_ZONE_FRACTION, END_ZONE_MAX)
        start_zone = zone_len(nrec, START_ZONE_FRACTION, START_ZONE_MAX)
        first_rec: dict[int, int] = {}
        last_rec: dict[int, int] = {}
        for ri in range(store.nrecords):
            first_rec.setdefault(rtpl[ri], ri)
            last_rec[rtpl[ri]] = ri
        for tpl in templates:
            if tpl.idx in first_rec:
                f, l = first_rec[tpl.idx], last_rec[tpl.idx]
                if l >= end_zone:
                    tpl.salience += END_TPL_BONUS * (2 if f >= end_zone else 1)
                if f < start_zone:
                    tpl.salience += START_TPL_BONUS
    line_zone = total - zone_len(total, END_ZONE_FRACTION, END_ZONE_MAX)
    firsts: set[int] = set()
    if FIRST_BONUS:  # ablation candidate, off by default
        seen: set[tuple] = set()
        for i in range(total):
            if rec[i] < 0 or blank[i]:
                continue
            t = rtpl[rec[i]]
            key = (t, 0) if store.depth[i] == 0 else (t, store.depth[i], shape_key(store.line(i)))
            if key not in seen:
                seen.add(key)
                firsts.add(i)

    def position_bonus(i: int) -> float:
        return (END_LINE_BONUS if i >= line_zone else 0.0) + (FIRST_BONUS if i in firsts else 0.0)

    by_sal = sorted(templates, key=lambda t: (-t.salience, t.idx))
    new_count = sum(1 for t in templates if t.new)

    def header(out_bytes: int) -> str:
        if follow is not None:
            news = f", {new_count} new" if later_window and new_count else ""
            return (
                f"# pare #{follow.k}: L{fmt_int(b1)}–L{fmt_int(store.base + total)} · {fmt_int(total)} line{'s' if total != 1 else ''},"
                f" {fmt_int(in_bytes)} B (~{fmt_int(tokens(in_bytes))} tok) → ~{fmt_int(tokens(out_bytes))} tok"
                f" (budget {fmt_int(budget)}) · {len(templates)} templates{news}"
            )
        return (
            f"# pare: {fmt_int(total)} line{'s' if total != 1 else ''}, {fmt_int(in_bytes)} B (~{fmt_int(tokens(in_bytes))} tok)"
            f" → ~{fmt_int(tokens(out_bytes))} tok (budget {fmt_int(budget)}) · {len(templates)} templates"
        )

    if follow is not None and run_path:
        run_line = f"## raw: {run_path} --since L{b1} --until L{store.base + total}"
    else:
        run_line = f"## raw: {run_path}" if run_path else "## raw: (not saved)"
    if note:
        run_line += f" · {note}"  # command mode: the exit status
    # The line a verbatim pass-through ends with. A whole run that fits is the
    # output, byte for byte: the run file adds nothing to it, so it is not
    # named, and only a failing exit status is added. A follow window keeps
    # its run line: it is one part of a stream.
    if follow is not None:
        verbatim_end = run_line if run_path else (f"## {note}" if note else None)
    else:
        verbatim_end = f"## {note}" if note and note != "exit 0" else None
    run_hint = "   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw"

    # Follow mode: a window with nothing above the salience floor and no new
    # shape is one line, so a quiet stream costs almost nothing to watch.
    if later_window and not new_count and not any(
        scores[i] >= FILL_FLOOR for i in range(total) if rec[i] >= 0 and not blank[i]
    ):
        if follow.quiet:
            return ""
        top = sorted(templates, key=lambda t: (-t.count, t.idx))[:5]
        counts = ", ".join(f"{t.tid} ×{fmt_int(t.count)}" for t in top)
        more = f" · +{len(templates) - len(top)} more" if len(templates) > len(top) else ""
        return (f"# pare #{follow.k}: L{fmt_int(b1)}–L{fmt_int(store.base + total)} · {fmt_int(total)} line{'s' if total != 1 else ''},"
                f" nothing new · {counts}{more}\n")

    # Small input: never make it bigger. Pass it through verbatim if it fits,
    # with no header; the only addition is the run-file line when one exists.
    # (A follow window keeps its header line so blocks stay delimited.)
    verbatim_head = [f"# pare #{follow.k}: L{fmt_int(b1)}–L{fmt_int(store.base + total)} · {fmt_int(total)} line{'s' if total != 1 else ''}, verbatim"] if follow else []
    if follow and store.carry is not None:
        # The window starts inside a record: say what its first lines belong to.
        verbatim_head.append(f"  ↳ continues L{store.carry.no}: {clip(store.carry.text.strip(), width)}")
    if store.verbatim_bytes + nbytes(verbatim_head) + (len(verbatim_end.encode("utf-8")) + 1 if verbatim_end else 0) <= budget_bytes:
        verbatim = list(verbatim_head)
        for start in range(0, total, 4096):
            verbatim.extend(clean_line(r.decode("utf-8", errors="replace")) for r in store.raw(start, min(total, start + 4096)))
        if verbatim_end:
            verbatim.append(verbatim_end)
        return "\n".join(verbatim) + "\n"

    # Fixed overhead: header + run lines + section titles + ledger reserve.
    ledger_reserve = min(400, max(120, budget_bytes // (16 if budget < COMPACT_BUDGET else 12)))
    show_hint = budget >= HINT_BUDGET and not later_window
    fixed = nbytes([header(budget_bytes), run_line, run_hint if show_hint else "", "## head", "## templates (by salience)",
                    "## selected lines", "## tail"]) + ledger_reserve
    avail = max(0, budget_bytes - fixed)

    shown: set[int] = set()  # line numbers already displayed

    # Head / tail windows: at most 30% of the budget together.
    # Head and tail windows skip lines that belong to a high-frequency
    # template (the template table already stands for those), except the
    # very first and last lines of the input. The scan is bounded so the
    # window stays a window and does not wander into the middle of the file.
    def windowed(idxs, n: int, keep_no: int) -> list[int]:
        out: list[int] = []
        for i in idxs:
            if len(out) >= n:
                break
            ri = rec[i]
            if ri >= 0 and is_chatter(store.line(i)):
                continue  # advice or a bare level: says nothing, even as the first or last line
            if i + b1 == keep_no or ri < 0:
                out.append(i + b1)
                continue
            frequent = templates[rtpl[ri]].count >= WINDOW_SKIP_COUNT
            long_body = store.depth[i] > 0 and rend[ri] - rstart[ri] >= WINDOW_SKIP_COUNT
            if not frequent and not long_body:
                out.append(i + b1)
        return out

    if budget < COMPACT_BUDGET:
        head_n = min(head_n, 3)  # a small budget gets a smaller head window (the tail keeps the verdicts)
    if later_window:
        head_n = 0  # the head of the input was shown in the first window
    head_span = max(head_n, WINDOW_SCAN_FACTOR * head_n)
    tail_span = max(tail_n, WINDOW_SCAN_FACTOR * tail_n)
    head_nos = windowed(range(0, min(total, head_span)), head_n, b1)
    tail_nos = sorted(windowed(range(total - 1, max(-1, total - 1 - tail_span), -1), tail_n, store.base + total))
    tail_nos = [no for no in tail_nos if no not in head_nos]
    while head_nos or tail_nos:
        cost = nbytes(render_ranges(store, head_nos, width)) + nbytes(render_ranges(store, tail_nos, width))
        if cost <= avail * 0.3:
            break
        if len(tail_nos) >= len(head_nos) and tail_nos:
            tail_nos.pop(0)
        elif head_nos:
            head_nos.pop()
    head_block = render_ranges(store, head_nos, width)
    tail_block = render_ranges(store, tail_nos, width)
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
        ex = store.head_of(tpl.exemplar)
        count = tpl.count
        extra = f" ({fmt_int(tpl.lines)} lines)" if tpl.lines != count else ""
        if ex.no < b1:
            extra += " (cont.)"  # the record began in the previous window
        if len(tpl.levels) > 1:  # mixed levels inside one template: say so
            hist = " ".join(f"{LEVEL_NAME[s]}×{fmt_int(c)}" for s, c in sorted(tpl.levels.items(), reverse=True))
            extra += f" [{hist}]"
        if later_window and tpl.new:
            extra += " new"
        extra += tpl.rate_note
        form = list_affixes(tpl.idx)
        if form:
            # A list of failures: the shared text once, the items under it,
            # those that fail unlike the rest first.
            n_rare = len(rare_failures(tpl.idx))
            if n_rare:
                extra += f" ({n_rare} unlike the rest, first)"
            return [f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {form[0]}{{…}}{form[1]}"]
        # An exemplar that the head or tail window already prints is referenced,
        # not repeated.
        ref = ref_of(ex.no, ex.text)
        other = pair_of(tpl)
        if other is not None:
            # Seen twice: both lines cost about what the pattern and one
            # example do, and say what the pattern's <*> hides ("Expected an
            # int" and "Expected a long").
            first, second = sorted((ex.no, other))
            return [f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {ref_of(first, text_of(first))}",
                    f"     {ref_of(second, text_of(second))}"]
        if count == 1 or compact or tpl.text == drain_message(ex.text):
            # One line: a singleton, a small budget, or a template with no
            # variables, whose text would only repeat the exemplar.
            return [f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {ref}"]
        return [f"{tpl.tid:<4} ×{fmt_int(count)}{extra} {tpl.marker} {clip(tpl.text, width)}", f"     e.g. {ref}"]

    def ref_of(no: int, text: str) -> str:
        """A line as a template entry shows it: referenced when the head or
        tail window already prints it, else in full."""
        where = "head" if no in head_nos else "tail" if no in tail_nos else None
        return f"L{no} (in {where})" if where else f"L{no}: {clip(text.strip(), width)}"

    pairs: dict[int, int | None] = {}

    def pair_of(tpl: Template) -> int | None:
        """For a template seen exactly twice in this window, the line number
        of the record head that is not its exemplar; None otherwise, at a
        compact budget (one line per entry), or when the two heads read the
        same."""
        if tpl.idx not in pairs:
            other = None
            if PAIR_MEMBERS and tpl.count == 2 and not compact and tpl.text != drain_message(store.head_of(tpl.exemplar).text):
                rs = [ri for ri in records_of(tpl.idx) if ri != tpl.exemplar]
                if len(rs) == 1:
                    ln = store.head_of(rs[0])
                    if ln.no >= b1 and ln.text.strip() != store.head_of(tpl.exemplar).text.strip():
                        other = ln.no
            pairs[tpl.idx] = other
        return pairs[tpl.idx]

    def repeat_key(no: int) -> str:
        """What counts as 'another one of these' for diminishing returns: for a
        record head, its drain template plus severity (drain already decided
        that pod/storefront-x2k9p and pod/storefront-q7m2z are one shape);
        for a continuation line, its masked shape plus severity."""
        i = no - b1
        if store.depth[i] == 0 and rec[i] >= 0 and not (is_error_line(store.line(i)) and lists_items(rtpl[rec[i]])):
            return f"tpl{rtpl[rec[i]]}·sev{store.sev_of(i)}"
        if store.depth[i] == 0 and rec[i] >= 0:
            # An error line of a template whose records each name something
            # different repeats only its own name: ten failing tests are one
            # drain template, but each test name is news.
            return name_key(store.line(i))
        return shape_key(store.line(i))  # a continuation line: its masked shape

    listing: dict[int, bool] = {}
    tpl_records: dict[int, list[int]] = {}

    def records_of(t: int) -> list[int]:
        """The first records of template t, at most LIST_SAMPLE."""
        if not tpl_records:  # filled for every template at once
            for ri in range(store.nrecords):
                rs = tpl_records.setdefault(rtpl[ri], [])
                if len(rs) < LIST_SAMPLE:
                    rs.append(ri)
        return tpl_records.get(t, [])

    def lists_items(t: int) -> bool:
        """True when most of a template's records differ in masked shape: a
        list of failing tests, each naming its own test. Events about the same
        three pods, repeated, share a few shapes and are repeats."""
        if t not in listing:
            heads = records_of(t)
            shapes = {name_key(store.head_of(ri)) for ri in heads}
            listing[t] = len(shapes) >= LIST_DISTINCT * len(heads)
        return listing[t]

    affixes: dict[int, tuple[str, str] | None] = {}

    def list_affixes(t: int) -> tuple[str, str] | None:
        """For a list of failures (an error template whose records are mostly
        distinct shapes), the prefix every record head shares, cut after a
        separator: "FAILED test_requests.py::RequestsTestCase::" for forty
        failing tests. Returned as (prefix, suffix) with an empty suffix: a
        shared ending is rarely more than a fragment of a value. None when
        the template is not such a list or the shared text is too short to be
        worth factoring out."""
        if t not in affixes:
            tpl = templates[t]
            form = None
            if (tpl.count >= LIST_MIN_COUNT and tpl.marker == "E" and lists_items(t)
                    and is_error_line(store.head_of(tpl.exemplar))):
                texts = [store.head_of(ri).text.strip() for ri in tpl_records[t]]
                pre = os.path.commonprefix(texts)
                while pre and pre[-1] not in LIST_CUT:
                    pre = pre[:-1]  # cut after a separator, never inside a name or a value
                if min(len(x) for x in texts) > len(pre) >= LIST_MIN_SHARED:
                    form = (pre, "")
            affixes[t] = form
        return affixes[t]

    def item_of(no: int) -> str | None:
        """A list template's record head as an item: its text between the
        shared prefix and suffix. None for any other line."""
        if no < b1:
            return None
        i = no - b1
        ri = rec[i]
        if ri < 0 or rstart[ri] != i:
            return None
        form = list_affixes(rtpl[ri])
        if form is None:
            return None
        pre, suf = form
        text = store.line(i).text.strip()
        if not (text.startswith(pre) and text.endswith(suf)) or len(text) <= len(pre) + len(suf):
            return None
        return clip(text[len(pre):len(text) - len(suf)], width)

    rare_items: dict[int, set[int]] = {}

    def rare_failures(t: int) -> set[int]:
        """Line numbers of the listed failures in list template t that fail
        unlike most of the list. Most failing tests in a broken environment
        fail one way (74 with the same missing pandas option); the two that
        fail on an assertion are the news. Each test's failure section is
        found by name: the last earlier record head that names it (pytest's
        "___ Class.test_x ___", Surefire's "testX(...) <<< FAILURE!"), or
        the item itself, up to the next head of the same kind. Its way of
        failing is the masked shape of the section's last top-level error
        line ("<PATH>:<NUM>: OptionError")."""
        if t in rare_items:
            return rare_items[t]
        rare_items[t] = set()
        if list_affixes(t) is None:
            return rare_items[t]
        heads = [rstart[ri] for ri in tpl_records[t]]
        names: dict[str, list[int]] = {}  # name token -> items naming it
        params: dict[int, tuple[str, ...]] = {}
        for h in heads:
            toks = [tk for tk in TOKEN_RE.findall(item_of(h + b1) or "") if not tk.isdigit()]
            if toks:
                names.setdefault(max(toks, key=len), []).append(h)
                params[h] = line_params(store.line(h))
        if len(names) < RARE_FAILURE_MIN:
            return rare_items[t]
        occ = {h: h for h in params}  # the section header naming each item; itself by default
        lo = max(0, heads[0] - 200_000)
        for i in range(lo, heads[-1]):
            ri = rec[i]
            if ri < 0 or rstart[ri] != i or rtpl[ri] == t:
                continue
            ln = store.line(i)
            for tk in set(TOKEN_RE.findall(ln.text)) & names.keys():
                for h in names[tk]:
                    if i < h and all(p in ln.text for p in params[h]):
                        occ[h] = i  # ascending: the last one before the item wins
        starts = set(occ.values())
        ways: dict[int, str] = {}
        for h, o in occ.items():
            kind = rtpl[rec[o]]
            way = None
            for j in range(o + 1, min(total, o + SECTION_MAX)):
                rj = rec[j]
                if j in starts or (rj >= 0 and rstart[rj] == j and rtpl[rj] in (kind, t)):
                    break  # the next section, or the list itself
                if rj >= 0 and store.depth[j] == 0 and not blank[j]:
                    lj = store.line(j)
                    if is_error_line(lj):
                        way = shape_key(lj)
            if way is not None:
                ways[h] = way
        if len(ways) >= RARE_FAILURE_MIN:
            sizes: dict[str, int] = {}
            for w in ways.values():
                sizes[w] = sizes.get(w, 0) + 1
            limit = max(1, int(len(ways) * RARE_FAILURE_SHARE))
            rare_items[t] = {h + b1 for h, w in ways.items() if sizes[w] <= limit}
        return rare_items[t]

    def rare_bonus(i: int) -> float:
        ri = rec[i]
        if ri < 0 or rstart[ri] != i or list_affixes(rtpl[ri]) is None:
            return 0.0
        return RARE_FAILURE_BONUS if (i + b1) in rare_failures(rtpl[ri]) else 0.0

    def item_cost(no: int) -> int:
        return nbytes([LIST_ITEM + item_of(no)])

    def as_item(no: int) -> bool:
        """Whether a line renders as an item of its placed list entry."""
        i = no - b1
        return no >= b1 and rec[i] >= 0 and rtpl[rec[i]] in placed and item_of(no) is not None

    keys: dict[int, str] = {}

    def key_of(no: int) -> str:
        k = keys.get(no)
        if k is None:
            if no < b1:  # the carried head: its line is in the previous window
                k = f"tpl{store.carry.tpl_idx}·sev{store.carry.sev}"
            else:
                k = repeat_key(no)
            keys[no] = k
        return k

    shown_tokens: list[frozenset[str]] = []

    def text_of(no: int) -> str:
        return store.carry.text if no < b1 else store.line(no - b1).text

    def note_shown(no: int) -> None:
        shown.add(no)
        key = key_of(no)
        taken[key] = taken.get(key, 0) + 1
        shown_tokens.append(token_set(text_of(no)))

    shown_tokens.extend(token_set(store.line(no - b1).text) for no in sorted(shown))
    exemplar_nos = {store.head_of(t.exemplar).no for t in templates}

    # Raw-line candidates: everything not in a window that scores at least
    # the floor (below it a line never pads the output, so it is not queued).
    # A template's exemplar head is a candidate too, so it can still appear
    # when the template entry itself does not make the cut; if the entry is
    # placed later it takes over that line.
    def initial(i: int) -> float:
        # Value per byte: a 180-byte advice line with a URL is worth less than
        # a 50-byte exception line of the same salience.
        return (scores[i] + rare_bonus(i) + position_bonus(i)) * (DENSITY_REF / (DENSITY_REF + slen[i])) ** DENSITY_POWER

    order = [i for i in range(total)
             if (i + b1) not in shown and rec[i] >= 0 and not blank[i] and scores[i] + position_bonus(i) >= FILL_FLOOR]
    order.sort(key=lambda i: -initial(i))  # stable: ties stay in line order

    def effective(no: int) -> float:
        i = no - b1
        # Each repeat of a shape already on screen is worth less.
        e = (scores[i] + rare_bonus(i) + position_bonus(i)) / (1.0 + FILL_PENALTY * taken.get(key_of(no), 0))
        # Value per rendered byte: an item of a placed list costs only its
        # distinct part.
        size = len(item_of(no).encode("utf-8")) if as_item(no) else slen[i]
        e *= (DENSITY_REF / (DENSITY_REF + size)) ** DENSITY_POWER
        return e

    # Queue items: (-priority, kind, id); kind 0 = template entry, 1 = line,
    # so a template wins a tie and ordering is deterministic. Lines enter the
    # queue from `order` at their initial priority as the queue drains down
    # to them, which is the same sequence a queue holding them all would
    # produce; re-queued and deferred items live in the heap.
    heap: list[tuple[float, int, int]] = []
    for tpl in templates:
        heapq.heappush(heap, (-tpl.salience, 0, tpl.idx))
    stream_pos = 0

    def pending() -> bool:
        return bool(heap) or stream_pos < len(order)

    def next_item() -> tuple[float, int, int]:
        nonlocal stream_pos
        nxt = None
        if stream_pos < len(order):
            i = order[stream_pos]
            nxt = (-initial(i), 1, i + b1)
        if heap and (nxt is None or heap[0] < nxt):
            return heapq.heappop(heap)
        stream_pos += 1
        return nxt

    picked: list[tuple[float, Line]] = []
    picked_nos: set[int] = set()
    deferred: list[tuple[float, int, int]] = []  # items that did not fit; retried against measured slack

    def place(avail: int) -> int:
        """Greedy pass over the queue. Returns the budget left over."""
        while pending() and avail >= 40:
            negp, kind, ident = next_item()
            if kind == 0:
                tpl = templates[ident]
                if max_templates and len(placed) >= max_templates:
                    continue
                if tpl.salience < TEMPLATE_FLOOR and placed:
                    continue  # low-salience shape: the ledger's count is enough
                ex_start, ex_end = rstart[tpl.exemplar], rend[tpl.exemplar]
                ex_line = store.head_of(tpl.exemplar)
                if tpl.count == 1 and all((j + b1) in window_nos for j in range(ex_start, ex_end) if not blank[j]):
                    continue  # a singleton whose whole record the head or tail window already prints
                if tpl.count <= NEAR_DUP_MAX_COUNT and ex_line.no not in shown and ex_end - ex_start == 1:
                    # A rare one-line record that restates something already on
                    # screen (a progress line repeating the summary line) stays in
                    # the ledger. A multi-line record carries more than its head.
                    if is_near_duplicate(token_set(ex_line.text), shown_tokens, names=is_error_line(ex_line),
                                         name_toks=frozenset(t for p in line_params(ex_line) for t in TOKEN_RE.findall(p))):
                        continue
                entry = tpl_head(tpl)
                cost = nbytes(entry)
                if tpl.marker in ("E", "W") and ex_end - ex_start > 1:
                    cost += 30  # room for the "(+N more lines)" marker under it
                is_list = list_affixes(tpl.idx) is not None
                if is_list:
                    cost += 30  # the "(+N more)" marker under the items
                    if ex_line.no not in window_nos and item_of(ex_line.no) is not None:
                        cost += item_cost(ex_line.no)  # the exemplar is the first item
                if cost > avail and placed:
                    deferred.append((negp, kind, ident))
                    if DEBUG:
                        print(f"defer tpl {tpl.tid:<4} prio={-negp:6.2f} cost={cost:4d} left={avail:5d}", file=sys.stderr)
                    continue  # the first template always gets a line
                placed[tpl.idx] = entry
                avail -= cost
                if DEBUG:
                    print(f"place tpl {tpl.tid:<4} prio={-negp:6.2f} cost={cost:4d} left={avail:5d} {ex_line.text.strip()[:60]!r}", file=sys.stderr)
                for no in (ex_line.no, pair_of(tpl)):
                    if no is None:
                        continue
                    if no not in shown:
                        note_shown(no)
                    elif no in picked_nos:  # the entry now shows this line
                        picked[:] = [(p, l) for p, l in picked if l.no != no]
                        picked_nos.discard(no)
                        avail += rlen[no - b1] + 20
                if is_list:  # lines of the list picked before it: now items, and cheaper
                    for _, l in picked:
                        if as_item(l.no):
                            avail += rlen[l.no - b1] + 20 - item_cost(l.no)
                continue
            no = ident
            cur = effective(no)
            if cur != -negp:  # stale: re-queue at the current score
                heapq.heappush(heap, (-cur, 1, no))
                continue
            if cur < FILL_FLOOR or no in shown:
                continue  # below the floor a line never pads the output
            if no in exemplar_nos and rtpl[rec[no - b1]] in placed:
                continue  # its template entry already shows it
            cost = item_cost(no) if as_item(no) else rlen[no - b1] + 20  # + gap marker allowance
            if cost > avail:
                deferred.append((negp, kind, ident))
                if DEBUG:
                    print(f"defer L{no:<7} prio={cur:6.2f} cost={cost:4d} left={avail:5d} {store.text(no - b1).strip()[:60]!r}", file=sys.stderr)
                continue
            ln = store.line(no - b1)
            toks = token_set(ln.text)
            if ln.depth == 0 and is_near_duplicate(
                    toks, shown_tokens, names=is_error_line(ln),
                    name_toks=frozenset(t for p in line_params(ln) for t in TOKEN_RE.findall(p))):
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
        lo, end = store.body_range(templates[ti].exemplar)
        nos = sorted(ln.no for _, ln in picked_lines if lo <= ln.no - b1 < end)
        out = render_ranges(store, nos, width) if nos else []
        rest = sum(1 for j in range(lo, end) if not blank[j] and (j + b1) not in window_nos) - len(nos)
        if rest > 0:
            out.append(f"  ┆ (+{fmt_int(rest)} more lines)")
        return out, set(nos)

    ledger_detail = True  # per-template list; dropped only as a last resort

    def ledger(shown_set: set[int]) -> list[str]:
        tpl_omitted = len(templates) - len(placed)
        omitted_total = total - sum(1 for no in shown_set if no >= b1)
        shown_in: dict[int, int] = {}
        blank_shown = 0
        for no in shown_set:
            j = no - b1
            if j < 0:
                continue  # a carried head is the previous window's line
            if blank[j]:
                blank_shown += 1
            ri = rec[j]
            if ri >= 0:
                shown_in[rtpl[ri]] = shown_in.get(rtpl[ri], 0) + 1
        per_tpl = []
        for tpl in templates:
            om = tpl.lines - shown_in.get(tpl.idx, 0)
            if om:
                per_tpl.append((om, tpl))
        per_tpl.sort(key=lambda x: (-x[0], x[1].idx))
        pct = 100.0 * omitted_total / total if total else 0.0
        parts = [f"{t.tid} ×{fmt_int(om)}" for om, t in per_tpl]
        head = f"## omitted {fmt_int(omitted_total)}/{fmt_int(total)} lines ({pct:.1f}%)"
        tail_bits = []
        if tpl_omitted:
            tail_bits.append(f"{tpl_omitted} templates not listed")
        blank_om = store.blank_total - blank_shown
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
            out.extend(render_ranges(store, head_nos, width))
        in_bodies: set[int] = set()
        if placed:
            out.append("## templates (by salience)")
            for tpl in by_sal:
                if tpl.idx not in placed:
                    continue
                out.extend(placed[tpl.idx])
                if list_affixes(tpl.idx):
                    items = sorted(ln.no for _, ln in picked_lines if as_item(ln.no) and rtpl[rec[ln.no - b1]] == tpl.idx)
                    ex_no = store.head_of(tpl.exemplar).no
                    if ex_no not in window_nos and item_of(ex_no) is not None:
                        items = sorted(set(items) | {ex_no})
                    rare = rare_failures(tpl.idx)
                    items.sort(key=lambda no: (no not in rare, no))
                    out.extend(LIST_ITEM + item_of(no) for no in items)
                    in_windows = sum(1 for no in window_nos
                                     if no >= b1 and rec[no - b1] >= 0 and rtpl[rec[no - b1]] == tpl.idx
                                     and rstart[rec[no - b1]] == no - b1)
                    rest = tpl.count - len(items) - in_windows - (1 if ex_no not in items and ex_no not in window_nos else 0)
                    if rest > 0:
                        out.append(f"  ┆ (+{fmt_int(rest)} more)")
                    in_bodies |= set(items)
                body, nos = body_of(tpl.idx, picked_lines)
                out.extend(body)
                in_bodies |= nos
        rest_nos = sorted(ln.no for _, ln in picked_lines if ln.no not in in_bodies)
        if rest_nos:
            out.append("## selected lines")
            out.extend(render_ranges(store, rest_nos, width))
        if tail_nos:
            out.append(f"## tail (L{tail_nos[0]}–L{tail_nos[-1]})")
            out.extend(render_ranges(store, tail_nos, width))
        shown_set = set(head_nos) | set(tail_nos) | {ln.no for _, ln in picked_lines}
        for ti in placed:
            shown_set.add(store.head_of(templates[ti].exemplar).no)
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
        if slack < 40 or not (deferred or pending()):
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
    # Absurdly small budget: even the skeleton does not fit. Cut from the end,
    # but keep the run line to the last: in a summary this small it is the
    # most useful line, the way to everything else.
    while out_bytes > budget_bytes and body:
        if body[-1] == run_line and len(body) > 1:
            del body[-2]
        else:
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
    store: Store,
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
    templates = store.templates
    rstart, rend, rtpl, rlen = store.rstart, store.rend, store.rtpl, store.rlen
    want_recs = array.array("i")
    for ri in range(store.nrecords):
        t = rtpl[ri]
        if only and t not in only:
            continue
        if t in exclude:
            continue
        if min_severity is not None:
            # Decided by the record's head line; a record with no structural
            # signal (no level, status or exit code) is left out.
            sev = store.sev_of(rstart[ri])
            if sev is None or sev < min_severity:
                continue
        want_recs.append(ri)
    total = len(store)
    lo = since if since is not None else 1
    hi = until if until is not None else total
    if lo > hi:
        sys.exit(f"pare: --since L{lo} is after --until L{hi}")

    def forward():
        for ri in want_recs:
            for no in range(max(rstart[ri] + 1, lo), min(rend[ri], hi) + 1):
                yield no

    def backward():
        for k in range(len(want_recs) - 1, -1, -1):
            ri = want_recs[k]
            for no in range(min(rend[ri], hi), max(rstart[ri] + 1, lo) - 1, -1):
                yield no

    n_want = sum(max(0, min(rend[ri], hi) - max(rstart[ri] + 1, lo) + 1) for ri in want_recs)
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
    head = f"# pare {label}: {fmt_int(n_want)} of {fmt_int(total)} lines"
    # Size of the full rendering, from the per-line sizes and the gap markers.
    rows_bytes = 0
    prev = None
    for no in forward():
        if prev is not None and no != prev + 1:
            rows_bytes += len(gap_marker(no - prev - 1).encode("utf-8")) + 1
        rows_bytes += rlen[no - 1]
        prev = no
    omitted = 0
    if rows_bytes + len(head) + 200 > budget_bytes:
        # Keep the first 70% and last 30% of what fits.
        keep_bytes = max(0, budget_bytes - len(head) - 200)
        first_b, last_b = int(keep_bytes * 0.7), int(keep_bytes * 0.3)
        first, acc = [], 0
        for no in forward():
            c = rlen[no - 1]
            if acc + c > first_b:
                break
            first.append(no)
            acc += c
        first_set = set(first)
        last, acc = [], 0
        for no in backward():
            if no in first_set:
                break
            c = rlen[no - 1]
            if acc + c > last_b:
                break
            last.append(no)
            acc += c
        last.reverse()
        kept = first + last
        omitted = n_want - len(kept)
        body = render_ranges(store, kept, width)
        if omitted:
            body.append(f"## omitted {fmt_int(omitted)} lines to fit budget {fmt_int(budget)}; raise --budget or narrow --only")
    else:
        body = render_ranges(store, list(forward()), width)
    body.append(f"## raw: {run_path}" if run_path else "## raw: (not saved)")
    return "\n".join([head] + body) + "\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


# Run-file pruning. Only files pare named itself are ever removed: a run
# file (<12 hex>.log, follow-<time>-<pid>.log) and a spool a killed pare left
# behind (.pare-<pid>.tmp). The small .json record next to a pruned run file
# stays, marked "pruned", so --stats keeps its history.
RUN_FILE_RX = re.compile(r"^(?:[0-9a-f]{12}|follow-\d{8}-\d{6}-\d+)\.log$")
STALE_SPOOL_RX = re.compile(r"^\.pare-\d+\.tmp$")
PRUNE_KEEP_DAYS = 14.0  # $PARE_RUN_KEEP_DAYS: run files older than this go (0: no age limit)
PRUNE_MAX_MB = 500.0  # $PARE_RUN_MAX_MB: past this total, the oldest go first (0: no size limit)
PRUNE_MIN_AGE_S = 86400  # ...but never one younger than a day: a summary may still point at it
PRUNE_EVERY_S = 3600  # automatic pruning runs at most this often ($PARE_RUN_PRUNE=off disables it)
PRUNE_MARKER = ".last-prune"


def prune_limits() -> tuple[float, float]:
    def number(var: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(var, default)))
        except ValueError:
            return default
    return number("PARE_RUN_KEEP_DAYS", PRUNE_KEEP_DAYS), number("PARE_RUN_MAX_MB", PRUNE_MAX_MB)


def prune_runs(run_dir: str, *, keep_days: float, max_mb: float, now: float | None = None,
               dry_run: bool = False) -> list[tuple[str, int, str]]:
    """Remove old run files: those older than keep_days, then, oldest first,
    as many as it takes to bring the rest under max_mb, sparing any younger
    than a day. A rerun with the same output rewrites its run file, so
    recently used output counts as new. Returns (path, bytes, reason) for
    each file removed, or that would be with dry_run."""
    now = time.time() if now is None else now
    try:
        names = os.listdir(run_dir)
    except OSError:
        return []
    removed: list[tuple[str, int, str]] = []
    runs: list[tuple[float, int, str]] = []
    for name in names:
        path = os.path.join(run_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if STALE_SPOOL_RX.match(name):
            if now - st.st_mtime > PRUNE_MIN_AGE_S:
                removed.append((path, st.st_size, "spool left by an interrupted run"))
        elif RUN_FILE_RX.match(name):
            runs.append((st.st_mtime, st.st_size, path))
    runs.sort()  # oldest first
    kept = []
    for mtime, size, path in runs:
        if keep_days and now - mtime > keep_days * 86400:
            removed.append((path, size, f"older than {keep_days:g} days"))
        else:
            kept.append((mtime, size, path))
    if max_mb:
        total = sum(size for _, size, _ in kept)
        for mtime, size, path in kept:
            if total <= max_mb * 1024 * 1024 or now - mtime < PRUNE_MIN_AGE_S:
                break
            removed.append((path, size, f"over {max_mb:g} MB in all"))
            total -= size
    if not dry_run:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        for path, _, _ in removed:
            try:
                os.unlink(path)
            except OSError:
                continue
            meta_path = os.path.splitext(path)[0] + ".json"
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                meta["pruned"] = stamp
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2)
                    f.write("\n")
            except (OSError, ValueError):
                pass
    return removed


def maybe_prune(run_dir: str) -> None:
    """Automatic pruning, at most once per PRUNE_EVERY_S; never in the way of
    the summary."""
    if os.environ.get("PARE_RUN_PRUNE", "").lower() in ("off", "0", "no", "false"):
        return
    marker = os.path.join(run_dir, PRUNE_MARKER)
    try:
        if time.time() - os.stat(marker).st_mtime < PRUNE_EVERY_S:
            return
    except OSError:
        pass
    try:
        with open(marker, "w"):
            pass
        keep_days, max_mb = prune_limits()
        prune_runs(run_dir, keep_days=keep_days, max_mb=max_mb)
    except OSError:
        pass


def prune_command(run_dir: str, dry_run: bool, out=sys.stdout) -> int:
    keep_days, max_mb = prune_limits()
    removed = prune_runs(run_dir, keep_days=keep_days, max_mb=max_mb, dry_run=dry_run)
    limits = f"keep {keep_days:g} days, at most {max_mb:g} MB; $PARE_RUN_KEEP_DAYS, $PARE_RUN_MAX_MB"
    verb = "would remove" if dry_run else "removed"
    total = sum(size for _, size, _ in removed)
    out.write(f"# pare --prune: {verb} {len(removed)} file{'s' if len(removed) != 1 else ''},"
              f" {total / 1024 / 1024:.1f} MB, in {run_dir} ({limits})\n")
    for path, size, reason in removed:
        out.write(f"{os.path.basename(path)}  {size / 1024:,.0f} KB  {reason}\n")
    return 0


def default_run_dir() -> str:
    env = os.environ.get("PARE_RUN_DIR")
    if env:
        return env
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "pare", "runs")


def main(argv: list[str] | None = None) -> int:
    global VOCAB, CHARS_PER_TOKEN
    ap = argparse.ArgumentParser(prog="pare", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=int, default=2000, help="output budget in tokens (bytes/2.8 by default); default 2000")
    ap.add_argument("--head", type=int, default=5, help="lines to keep from the start; default 5")
    ap.add_argument("--tail", type=int, default=10, help="lines to keep from the end; default 10")
    ap.add_argument("--width", type=int, default=200, help="clip lines longer than this; default 200")
    ap.add_argument("--chars-per-token", type=float, default=CHARS_PER_TOKEN, metavar="N",
                    help="bytes per token for the budget and the token estimate (default 2.8, measured "
                         "on common tokenizers; 4 is the prose rule of thumb)")
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
    ap.add_argument("--follow", "-f", action="store_true",
                    help="summarise a stream as it arrives: one block per window instead of one summary at "
                         "end of input (see --every, --interval, --flush-on)")
    ap.add_argument("--every", type=int, default=20000, metavar="N",
                    help="follow: flush a window after N lines (default 20000); also the window cap")
    ap.add_argument("--interval", type=float, default=0.0, metavar="S",
                    help="follow: flush a non-empty window every S seconds, even while the stream is idle (default off)")
    ap.add_argument("--flush-on", choices=["error", "fatal", "none"], default="error",
                    help="follow: flush soon after a line at this severity arrives (default error)")
    ap.add_argument("--settle", type=float, default=0.5, metavar="S",
                    help="follow: wait S seconds after a --flush-on line so its record can finish arriving (default 0.5)")
    ap.add_argument("--min-interval", type=float, default=2.0, metavar="S",
                    help="follow: at most one --flush-on flush per S seconds (default 2)")
    ap.add_argument("--quiet", action="store_true", help="follow: no heartbeat line for a window with nothing new")
    ap.add_argument("--vocab", metavar="FILE", default=os.environ.get("PARE_VOCAB"),
                    help="vocabulary file that extends or replaces the built-in error/warn/... word "
                         "lists (default: $PARE_VOCAB); see --dump-vocab for the format")
    ap.add_argument("--dump-vocab", action="store_true",
                    help="print the built-in vocabulary (after applying --vocab) in file format and exit")
    ap.add_argument("--prune", action="store_true",
                    help="remove old run files now: older than $PARE_RUN_KEEP_DAYS (14), or past $PARE_RUN_MAX_MB (500) in all;"
                         " pare also does this on its own, at most hourly ($PARE_RUN_PRUNE=off to stop it)")
    ap.add_argument("--dry-run", action="store_true", help="with --prune: list what would be removed")
    ap.add_argument("--stats", action="store_true",
                    help="print what command mode saved: raw output against pare's, from the run directory's records")
    ap.add_argument("--version", action="version", version=f"pare {__version__}")
    ap.epilog = ("command mode: pare [options] -- CMD [ARGS...] runs CMD with stdout and stderr merged, "
                 "summarises the output, ends the summary with its exit status, records the command "
                 "next to the run file (<hash>.json), and exits with CMD's exit code")
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] | None = None
    if "--" in argv:
        cut_at = argv.index("--")
        command, argv = argv[cut_at + 1:], argv[:cut_at]
        if not command:
            sys.exit("pare: -- must be followed by a command to run")
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
    if args.stats:
        return stats(args.run_dir or default_run_dir())
    if args.dry_run and not args.prune:
        sys.exit("pare: --dry-run goes with --prune")
    if args.prune:
        return prune_command(args.run_dir or default_run_dir(), args.dry_run)
    if args.dump_vocab:
        sys.stdout.write(dump_vocab(VOCAB.entries))
        return 0

    selecting = bool(args.only or args.exclude or args.min_severity or args.since or args.until)
    if command and selecting:
        sys.exit("pare: -- runs a command to summarise; --only/--except/--since/--until/--min-severity read a run file")
    run = None
    if command:
        run = CommandRun.start(command)
        if run.proc is None:
            return run.exit_code  # could not start it; the reason is on stderr

    # The spool receives the input as it is read. With tee it becomes the run
    # file, renamed to its content hash once the hash is known; without, it
    # is a temporary file removed on exit. Follow mode names the run file up
    # front (the hash is not known while the stream runs) and never renames.
    tee = not args.no_tee
    if not tee:
        fd, spool_path = tempfile.mkstemp(prefix="pare-", suffix=".spool")
        os.close(fd)
    elif args.run_file:
        d = os.path.dirname(args.run_file)
        if d:
            os.makedirs(d, exist_ok=True)
        spool_path = args.run_file if args.follow else args.run_file + ".tmp"
    else:
        run_dir = args.run_dir or default_run_dir()
        os.makedirs(run_dir, exist_ok=True)
        maybe_prune(run_dir)
        if args.follow:
            spool_path = os.path.join(run_dir, time.strftime("follow-%Y%m%d-%H%M%S-") + f"{os.getpid()}.log")
        else:
            spool_path = os.path.join(run_dir, f".pare-{os.getpid()}.tmp")
    if args.follow:
        if args.only or args.exclude or args.min_severity or args.since or args.until:
            sys.exit("pare: --follow summarises a stream; --only/--except/--since/--until/--min-severity read a run file")
        if args.every < 1:
            sys.exit("pare: --every must be at least 1")
        try:
            return follow(args, spool_path, os.path.abspath(spool_path) if tee else None, run)
        finally:
            if not tee:
                try:
                    os.unlink(spool_path)
                except OSError:
                    pass
    store = None
    run_path = None
    try:
        store, digest = analyze(run.output() if run else sys.stdin.buffer, width=args.width, spool_path=spool_path)
        exit_code = run.wait() if run else 0
        note = f"exit {exit_code}" if run else None
        if store.total_bytes == 0:
            sys.stdout.write(f"# pare: no output · {note}\n" if run else "# pare: empty input\n")
            return exit_code
        if tee:
            final = args.run_file or os.path.join(run_dir, f"{digest}.log")
            os.replace(spool_path, final)
            spool_path = store.spool_path = final
            run_path = os.path.abspath(final)
            if run:
                run.write_meta(run_path, lines=len(store), nbytes=store.total_bytes)
        store.open()
        score_templates(store)

        if selecting:
            out = select(
                store,
                only=parse_tids(args.only, len(store.templates)),
                exclude=parse_tids(args.exclude, len(store.templates)),
                min_severity=SEVERITY_BY_NAME[args.min_severity] if args.min_severity else None,
                since=parse_lineno(args.since, len(store), "--since"),
                until=parse_lineno(args.until, len(store), "--until"),
                budget=args.budget, width=args.width, run_path=run_path,
            )
        else:
            out = summarize(
                store,
                budget=args.budget, head_n=args.head, tail_n=args.tail, width=args.width,
                max_templates=args.max_templates, run_path=run_path, in_bytes=store.total_bytes,
                note=note,
            )
        sys.stdout.write(out)
        sys.stdout.flush()
        if run and run_path:  # what the command printed against what pare printed, for --stats
            run.write_meta(run_path, lines=len(store), nbytes=store.total_bytes,
                           out_bytes=len(out.encode("utf-8")), budget=args.budget)
        return exit_code
    finally:
        if store is not None:
            store.close()
        if not tee or run_path is None:
            try:
                os.unlink(spool_path)
            except OSError:
                pass


class CountingWriter:
    """A text stream that counts the bytes written through it (follow mode's
    output, for the run's --stats record)."""

    def __init__(self, stream):
        self.stream = stream
        self.nbytes = 0

    def write(self, text: str) -> int:
        self.nbytes += len(text.encode("utf-8"))
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()


def stats(run_dir: str, out=sys.stdout) -> int:
    """--stats: what command mode saved, from the .json records next to the
    run files: per command, what it printed against what pare printed.
    The saving is an upper bound on what an agent avoided reading: without
    pare it might have cut the output itself (tail, grep, a truncating
    harness), and with pare it may read more of the run file afterwards."""
    rows = []
    try:
        names = sorted(f for f in os.listdir(run_dir) if f.endswith(".json"))
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(run_dir, name), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or m.get("out_bytes") is None:
            continue  # written before pare recorded its output, or still running
        cpt = m.get("chars_per_token") or CHARS_PER_TOKEN
        rows.append((m.get("started", ""), re.sub(r"\s+", " ", " ".join(m.get("command") or [])), m.get("bytes", 0), m["out_bytes"], cpt,
                     m.get("exit")))
    if not rows:
        out.write(f"# pare --stats: no command-mode runs with output sizes in {run_dir}\n")
        return 0
    tok = lambda b, cpt: math.ceil(b / cpt)
    raw = sum(tok(r[2], r[4]) for r in rows)
    shown = sum(tok(r[3], r[4]) for r in rows)
    out.write(f"# pare --stats: {len(rows)} command run{'s' if len(rows) != 1 else ''} in {run_dir}\n")
    out.write(f"raw ~{fmt_int(raw)} tok → printed ~{fmt_int(shown)} tok"
              f" · saved ~{fmt_int(max(0, raw - shown))} tok ({100.0 * (raw - shown) / raw if raw else 0.0:.1f} %)\n")
    passthrough = sum(1 for r in rows if r[3] >= r[2])
    if passthrough:
        out.write(f"{passthrough} run{'s' if passthrough != 1 else ''} fit the budget and passed through whole\n")
    out.write("## largest\n")
    for started, cmd, nb, ob, cpt, code in sorted(rows, key=lambda r: -(r[2] - r[3]))[:10]:
        out.write(f"{started}  ~{fmt_int(tok(nb, cpt)):>8} → ~{fmt_int(tok(ob, cpt)):>6} tok  exit {code}  {clip(cmd, 80)}\n")
    return 0


class CommandRun:
    """Command mode: pare runs the command itself, so its exit code and its
    command line survive (a pipe keeps neither). stdout and stderr are
    merged into one pipe, in the order the command wrote them; stdin is
    inherited. While the command runs, pare ignores SIGINT: a Ctrl-C reaches
    the command (same process group), the command exits, and pare still
    summarises what it printed and exits with its status."""

    def __init__(self, command: list[str]):
        self.command = command
        self.proc: subprocess.Popen | None = None
        self.exit_code = 0
        self.started = time.time()
        self.cwd = os.getcwd()
        self._old_handlers: dict = {}

    @classmethod
    def start(cls, command: list[str]) -> "CommandRun":
        run = cls(command)
        try:
            run.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            sys.stderr.write(f"pare: {command[0]}: command not found\n")
            run.exit_code = 127
            return run
        except PermissionError:
            sys.stderr.write(f"pare: {command[0]}: permission denied\n")
            run.exit_code = 126
            return run
        # After the spawn, so the command does not inherit these handlers.
        # SIGINT from a terminal reaches the whole process group, so pare
        # only ignores it. SIGTERM and SIGHUP (a tool's timeout, a closed
        # terminal) may be aimed at pare alone, so pare passes them on and
        # keeps reading: the command exits, and its output is still
        # summarised instead of lost.
        def forward(signum, _frame, _pid=run.proc.pid):
            try:
                os.kill(_pid, signum)
            except OSError:
                pass

        try:
            run._old_handlers = {
                signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN),
                signal.SIGTERM: signal.signal(signal.SIGTERM, forward),
                signal.SIGHUP: signal.signal(signal.SIGHUP, forward),
            }
        except ValueError:  # not the main thread (pare used as a library)
            run._old_handlers = {}
        return run

    def exited(self) -> bool:
        return self.proc.poll() is not None

    def output(self) -> "CommandOutput":
        return CommandOutput(self)

    def wait(self) -> int:
        rc = self.proc.wait()
        for sig, handler in self._old_handlers.items():
            signal.signal(sig, handler)
        self._old_handlers = {}
        # A command killed by a signal exits 128 + n, as a shell reports it.
        self.exit_code = 128 - rc if rc < 0 else rc
        return self.exit_code

    def write_meta(self, run_path: str, *, lines: int, nbytes: int, out_bytes: int | None = None,
                   budget: int | None = None) -> None:
        """<run file>.json next to the run file: what produced it, and how
        much of it pare printed. Read by bench/collect.py to fill in a
        held-out case's .cmd, and by --stats."""
        meta = {
            "command": self.command,
            "cwd": self.cwd,
            "exit": self.exit_code,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)),
            "seconds": round(time.time() - self.started, 2),
            "lines": lines,
            "bytes": nbytes,
            "pare": __version__,
        }
        if out_bytes is not None:
            meta.update(out_bytes=out_bytes, budget=budget, chars_per_token=CHARS_PER_TOKEN)
        try:
            with open(os.path.splitext(run_path)[0] + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
                f.write("\n")
        except OSError:
            pass  # the summary matters more than the metadata


# Once the command has exited, stop reading after this long without output,
# even if the pipe is still open: a process it left behind (a daemon started
# with &, a grandchild that outlived a killed parent) can hold the pipe open
# indefinitely, and the summary of what the command printed should not wait
# for it.
EXIT_GRACE = 1.0


class CommandOutput:
    """The command's merged output as a stream for analyze(): read(n)
    returns what is available, and b"" at end of file or once the command
    has exited and the pipe has been quiet for EXIT_GRACE seconds."""

    def __init__(self, run: CommandRun):
        self.run = run
        self.fd = run.proc.stdout.fileno()

    def read(self, n: int) -> bytes:
        quiet_since = None
        while True:
            ready, _, _ = selectmod.select([self.fd], [], [], 0.2)
            if ready:
                return os.read(self.fd, n)
            if self.run.exited():
                now = time.monotonic()
                quiet_since = quiet_since or now
                if now - quiet_since >= EXIT_GRACE:
                    return b""


def follow(args, spool_path: str, run_path: str | None, run: CommandRun | None = None) -> int:
    """Follow mode: feed stdin to the pass as it arrives and write one block
    per window. A window closes after --every lines, after --interval
    seconds, --settle seconds after a --flush-on line (at most one such
    flush per --min-interval), and at end of input."""
    flush_sev = {"error": 3, "fatal": 4, "none": 99}[args.flush_on]
    fd = run.proc.stdout.fileno() if run else sys.stdin.fileno()
    out = CountingWriter(sys.stdout)
    k = 0
    with open(spool_path, "wb") as spool, open(spool_path, "rb") as reader:
        p = Pass(args.width, spool, spool_path)
        last_flush = time.monotonic()
        due: float | None = None  # a --flush-on flush is scheduled for this time
        p.every = args.every

        def flush(final: bool = False) -> None:
            nonlocal k, last_flush, due
            # A window with nothing in it but blank lines prints nothing and
            # is not cut: the blank line stays in the open window (and, if it
            # sits inside a carried record, is adopted into it as in a batch
            # pass). The last window is cut whatever it holds.
            if not len(p.store) or (not p.has_content and not final):
                last_flush = time.monotonic()
                due = None
                return
            k += 1
            store = p.cut()
            store.use(reader)
            score_templates(store)
            block = summarize(
                store, budget=args.budget, head_n=args.head, tail_n=args.tail, width=args.width,
                max_templates=args.max_templates, run_path=run_path, in_bytes=store.total_bytes,
                follow=FollowInfo(k=k, quiet=args.quiet),
            )
            if block:
                out.write(block + "\n")
                out.flush()
            last_flush = time.monotonic()
            due = None

        p.on_full = flush
        quiet_since: float | None = None
        while True:
            now = time.monotonic()
            deadlines = []
            if due is not None:
                deadlines.append(due)
            if args.interval > 0 and len(p.store):
                deadlines.append(last_flush + args.interval)
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            if run is not None:
                timeout = 0.2 if timeout is None else min(timeout, 0.2)
            try:
                ready, _, _ = selectmod.select([fd], [], [], timeout)
            except InterruptedError:
                continue
            if not ready and run is not None and run.exited():
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= EXIT_GRACE:
                    break  # the command is gone; whatever still holds the pipe is not its output
            if ready:
                quiet_since = None
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                p.feed(chunk)  # cuts and flushes on its own when the window cap is reached
                if p.worst_sev >= flush_sev and due is None:
                    due = max(time.monotonic() + args.settle, last_flush + args.min_interval)
            now = time.monotonic()
            if due is not None and now >= due:
                flush()
            elif args.interval > 0 and len(p.store) and now >= last_flush + args.interval:
                flush()
        p.finish()
        flush(final=True)
        exit_code = run.wait() if run else 0
        out.write(f"# pare: end of input · {fmt_int(p.nlines)} lines, {fmt_int(p.total_bytes)} B"
                  f" (~{fmt_int(tokens(p.total_bytes))} tok) · {len(p.globals)} templates · "
                  + (f"raw: {run_path}" if run_path else "raw: (not saved)")
                  + (f" · exit {exit_code}" if run else "") + "\n")
        out.flush()
        if run and run_path:
            run.write_meta(run_path, lines=p.nlines, nbytes=p.total_bytes, out_bytes=out.nbytes, budget=args.budget)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
