#!/usr/bin/env python3
"""Compare two pare implementations byte for byte.

    python bench/parity.py --candidate target/release/pare
    python bench/parity.py --candidate target/release/pare --dump      # also per-line results
    python bench/parity.py --candidate ./pare --quick --show 3          # a fast subset

The reference is reference/pare.py (the frozen Python version) unless
--reference names another command. Every log in the corpora, plus a set of
generated edge cases (empty input, no final newline, CRLF, ANSI colours,
invalid UTF-8, NUL bytes, a very long line, letters that case-fold to
ASCII), is run through both under each flag set in CASES, with --no-tee so
no run file is written. The outputs and exit codes must be identical.

With --dump, batch cases also compare the pass's per-line and per-template
results ($PARE_DUMP, see write_dump in reference/pare.py), and a difference is
reported as the first row that differs, which says where two
implementations part ways long before the summaries show it.

With --skip-unported, a candidate run that exits with status 70 and says
"not yet ported" has its output skipped rather than counted as a
difference, so a port can be checked on what it does so far. Its dump is
still compared if it wrote one, and a dump field the candidate writes as
"?" (not computed yet) is not compared.

The candidate runs with PARE_REFERENCE_RULES=1, which turns off the Rust
version's own rules that change default output (structured JSON and logfmt
lines, the tail window's verdict rule): what is compared is everything
else, which must still match the Python version byte for byte. The
Rust-only rules have tests of their own (cargo test).

Exits 1 if anything differs.
"""

import argparse
import concurrent.futures
import difflib
import glob
import os
import shlex
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Flag sets: budgets, a narrow width, the prose token rate, a template cap,
# a vocabulary file, small windows, every selection flag, and follow mode.
CASES = [
    ["--budget", "2000"],
    ["--budget", "1000"],
    ["--budget", "600"],
    ["--budget", "300", "--width", "80"],
    ["--budget", "600", "--chars-per-token", "4"],
    ["--budget", "1000", "--max-templates", "5"],
    ["--budget", "1000", "--head", "2", "--tail", "3"],
    ["--budget", "1000", "--vocab", os.path.join(ROOT, "reference", "vocab.example")],
    ["--only", "T1,T2,T3", "--budget", "100000"],
    ["--except", "T1", "--budget", "1000"],
    ["--min-severity", "error", "--budget", "1000"],
    ["--since", "L10", "--until", "L200", "--budget", "100000"],
    ["--follow", "--every", "300", "--budget", "600", "--quiet"],
    ["--follow", "--every", "5000", "--budget", "1000"],
]
QUICK = [CASES[0], CASES[2], CASES[8], CASES[12]]
SELECTING = ("--only", "--except", "--min-severity", "--since", "--until", "--follow")
NOT_PORTED = 70  # the Rust port's status for what it does not do yet (src/error.rs)


def corpus_logs() -> list[str]:
    pats = ["corpus/*.log", "corpus-generated/*.log", "corpus-dev/*/*.log", "corpus-heldout/*/*.log"]
    out = []
    for p in pats:
        out += [f for f in glob.glob(os.path.join(ROOT, p)) if "/inbox/" not in f and "/_failed/" not in f]
    return sorted(out)


def edge_cases(directory: str) -> list[str]:
    """Small inputs the corpus does not have, written once per run."""
    trace = "Traceback (most recent call last):\n  File \"a.py\", line 3, in <module>\n    f()\nValueError: bad\n"
    cases = {
        "empty": b"",
        "one-line-no-newline": b"ERROR something broke",
        "blank-only": b"\n\n\n\n",
        "crlf": ("INFO start\r\n" * 50 + "ERROR failed to connect\r\n" + "INFO retry\r\n" * 50).encode(),
        "carriage-return-progress": ("downloading 10%\rdownloading 50%\rdownloading 100%\n" * 30
                                     + "ERROR checksum mismatch\n").encode(),
        "ansi": ("\x1b[32mPASS\x1b[0m test_a\n" * 80 + "\x1b[31mFAIL\x1b[0m test_b\n" + trace).encode(),
        "invalid-utf8": b"INFO ok \xff\xfe bytes\n" * 60 + b"ERROR bad \xc3\x28 sequence\n" + b"\x80\x81 tail\n",
        "nul-bytes": b"INFO a\x00b\n" * 60 + b"ERROR c\x00d\n",
        "long-line": ("x" * 20000 + " error at the end\n" + "INFO short\n" * 100).encode(),
        "tabs": ("\tat foo(Bar.java:1)\n" * 5 + "ERROR x\n\tat a(B.java:2)\n\t\tat c(D.java:3)\n" * 40).encode(),
        "case-folding": ("İNFO: started\nlevel=İnfo x\nKelvin error\nſkipped\n"
                         "straße warning\nﬁle not found\n" * 30).encode(),
        "no-trailing-newline-big": ("2026-10-07T09:00:00Z INFO request ok\n" * 3000
                                    + "2026-10-07T09:00:01Z ERROR worker crashed").encode(),
        "repeated-trace": (trace * 200 + "=== 200 failed ===\n").encode(),
        # Continuation lines 300 columns deeper than their head: the depth
        # overflowed a signed byte in the Python version until it was fixed.
        # Characters where Python's \\w, \\d and isdigit() differ from Rust's,
        # next to numbers and in paths, on record heads so the masks see them.
        "unicode-classes": "".join(
            f"{h} job{i} x\u0301{i} 5\u00b2 v1.2\u0301 a\u0301/b {i}\u00b9 \u0e50{i} {d}{i} "
            f"\u203f{i} 10.0.0.{i}\u0301 2026-10-0{i % 9 + 1}\u0301 0xff\u00b2\n"
            for i in range(60) for h, d in (("ERROR", "\U00016ac0"), ("WARN", "\U0001e4f0"))).encode(),
        # German log vocabulary, which the --vocab flag set's file knows.
        "vocab-german": ("INFO lade Daten\n" * 40 + "Fehler: Zeitüberschreitung beim Verbinden\n"
                         "  bei a.b(C.java:1)\nWarnung: veraltet\nAUSNAHME aufgetreten\n"
                         "0 Fehler, zusammenfassung beendet\n").encode(),
        "deep-indent": ("ERROR head\n" + " " * 300 + "deep line\n" + "\t" * 80 + "deeper\nINFO next\n").encode(),
    }
    paths = []
    for name, data in cases.items():
        path = os.path.join(directory, f"edge-{name}.log")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return paths


def run(cmd: list[str], args: list[str], log: str, dump: str | None) -> tuple[int, bytes, bytes]:
    env = dict(os.environ, PARE_RUN_PRUNE="off")
    env.pop("PARE_DEBUG", None)
    env.pop("PARE_BUDGET", None)  # the Rust version reads it; the reference does not
    env["PARE_REFERENCE_RULES"] = "1"  # the Rust version's own rules off; the reference ignores it
    if dump:
        env["PARE_DUMP"] = dump
    else:
        env.pop("PARE_DUMP", None)
    with open(log, "rb") as f:
        p = subprocess.run(cmd + ["--no-tee"] + args, stdin=f, capture_output=True, env=env, timeout=600)
    return p.returncode, p.stdout, p.stderr


def first_difference(a: bytes, b: bytes, label: str, context: int = 3) -> str:
    al = a.decode("utf-8", "replace").splitlines()
    bl = b.decode("utf-8", "replace").splitlines()
    diff = list(difflib.unified_diff(al, bl, "reference", "candidate", n=context, lineterm=""))
    return f"  {label}:\n" + "\n".join("    " + d for d in diff[:40]) + ("\n    ..." if len(diff) > 40 else "")


TODO_FIELD = "?"  # a dump field the candidate does not compute yet (src/dump.rs)


def compare_dumps(ref: bytes, cand: bytes) -> tuple[str | None, int]:
    """The first rows where two dumps differ, ignoring fields the candidate
    writes as TODO_FIELD, and how many fields were ignored."""
    rl = ref.decode("utf-8", "replace").splitlines()
    cl = cand.decode("utf-8", "replace").splitlines()
    skipped = 0
    shown = []
    for i in range(max(len(rl), len(cl))):
        r = rl[i] if i < len(rl) else "(no row)"
        c = cl[i] if i < len(cl) else "(no row)"
        rf, cf = r.split("\t"), c.split("\t")
        skipped += sum(1 for x in cf if x == TODO_FIELD)
        same = len(rf) == len(cf) and all(y == TODO_FIELD or x == y for x, y in zip(rf, cf))
        if not same and len(shown) < 3:
            # Label by the row's own key: "L 229" (input line 229), "T T8".
            label = " ".join(rf[:2]) if len(rf) >= 2 and rf[0] in ("L", "T") else f"row {i + 1}"
            shown.append(f"    at {label}:\n      reference {r[:300]}\n      candidate {c[:300]}")
    if not shown:
        return None, skipped
    return "  per-line dump (first rows that differ; ? = not computed by the candidate yet):\n" + "\n".join(shown), skipped


def compare(job) -> dict:
    """Runs reference and candidate on one input and one flag set. The
    result says what differed (problems), whether the candidate's output was
    skipped as not yet ported, and whether and how its dump was compared."""
    ref, cand, log, args, use_dump, skip_unported, tmp = job
    name = f"{os.path.relpath(log, ROOT) if log.startswith(ROOT) else os.path.basename(log)} {' '.join(args)}"
    want_dump = use_dump and not any(a in SELECTING for a in args)
    dumps = [None, None]
    if want_dump:
        fd, dumps[0] = tempfile.mkstemp(dir=tmp, suffix=".ref.tsv")
        os.close(fd)
        fd, dumps[1] = tempfile.mkstemp(dir=tmp, suffix=".cand.tsv")
        os.close(fd)
    r = run(ref, args, log, dumps[0])
    c = run(cand, args, log, dumps[1])
    unported = skip_unported and c[0] == NOT_PORTED and b"not yet ported" in c[2]
    res = {"name": name, "problems": [], "skipped": unported, "dumped": False, "todo_fields": 0}
    if want_dump:
        with open(dumps[0], "rb") as f:
            rd = f.read()
        with open(dumps[1], "rb") as f:
            cd = f.read()
        # A run the candidate does not finish can still have dumped the pass:
        # compare that, so a port is checked line by line before it summarises.
        if rd or cd:
            res["dumped"] = True
            problem, res["todo_fields"] = compare_dumps(rd, cd)
            if problem:
                res["problems"].append(problem)
    if not unported:
        if r[0] != c[0]:
            res["problems"].append(f"  exit code: reference {r[0]}, candidate {c[0]}"
                                   + (f"\n  candidate stderr: {c[2].decode('utf-8', 'replace').strip()[:500]}"
                                      if c[2] else ""))
        if r[1] != c[1]:
            res["problems"].append(first_difference(r[1], c[1], "output"))
    for d in dumps:
        if d:
            os.unlink(d)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", required=True, help="command for the implementation under test")
    ap.add_argument("--reference", default=None, help="command for the reference (default: reference/pare.py)")
    ap.add_argument("--dump", action="store_true", help="also compare per-line results ($PARE_DUMP) in batch cases")
    ap.add_argument("--quick", action="store_true", help=f"{len(QUICK)} flag sets instead of {len(CASES)}")
    ap.add_argument("--logs", nargs="*", help="these logs instead of the corpora")
    ap.add_argument("--no-edge", action="store_true", help="leave out the generated edge cases")
    ap.add_argument("--skip-unported", action="store_true",
                    help=f"count candidate runs that exit {NOT_PORTED} as 'not yet ported' as skipped")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--show", type=int, default=5, help="differences to print in full (default 5)")
    args = ap.parse_args()

    ref = shlex.split(args.reference) if args.reference else [sys.executable, os.path.join(ROOT, "reference", "pare.py")]
    cand = shlex.split(args.candidate)
    cases = QUICK if args.quick else CASES
    with tempfile.TemporaryDirectory(prefix="pare-parity-") as tmp:
        logs = [os.path.abspath(p) for p in args.logs] if args.logs else corpus_logs()
        if not args.no_edge:
            logs += edge_cases(tmp)
        jobs = [(ref, cand, log, case, args.dump, args.skip_unported, tmp) for log in logs for case in cases]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(compare, jobs))
        failures = [r for r in results if r["problems"]]
        skipped = sum(1 for r in results if r["skipped"] and not r["problems"])
        dumped = sum(1 for r in results if r["dumped"])
        todo = sum(r["todo_fields"] for r in results)
        for r in failures[: args.show]:
            print(f"DIFF {r['name']}")
            print("\n".join(r["problems"]))
        for r in failures[args.show:]:
            print(f"DIFF {r['name']}")
        summary = f"# parity: {len(jobs)} runs ({len(logs)} inputs x {len(cases)} flag sets), {len(failures)} differ"
        if args.skip_unported:
            summary += f", {skipped} with output not yet ported"
        if args.dump:
            summary += f"; per-line dumps compared on {dumped} runs"
            if todo:
                summary += f" ({todo:,} fields not computed by the candidate yet)"
        print(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
