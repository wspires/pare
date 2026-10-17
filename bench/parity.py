#!/usr/bin/env python3
"""Compare two pare implementations byte for byte.

    python bench/parity.py --candidate target/release/pare
    python bench/parity.py --candidate target/release/pare --dump      # also per-line results
    python bench/parity.py --candidate ./pare --quick --show 3          # a fast subset

The reference is proto/pare.py (the frozen Python version) unless
--reference names another command. Every log in the corpora, plus a set of
generated edge cases (empty input, no final newline, CRLF, ANSI colours,
invalid UTF-8, NUL bytes, a very long line, letters that case-fold to
ASCII), is run through both under each flag set in CASES, with --no-tee so
no run file is written. The outputs and exit codes must be identical.

With --dump, batch cases also compare the pass's per-line and per-template
results ($PARE_DUMP, see write_dump in proto/pare.py), and a difference is
reported as the first row that differs, which says where two
implementations part ways long before the summaries show it.

With --skip-unported, a candidate run that exits with status 70 and says
"not yet ported" counts as skipped rather than as a difference, so a port
can be checked on what it does so far.

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
# small windows, every selection flag, and follow mode.
CASES = [
    ["--budget", "2000"],
    ["--budget", "1000"],
    ["--budget", "600"],
    ["--budget", "300", "--width", "80"],
    ["--budget", "600", "--chars-per-token", "4"],
    ["--budget", "1000", "--max-templates", "5"],
    ["--budget", "1000", "--head", "2", "--tail", "3"],
    ["--only", "T1,T2,T3", "--budget", "100000"],
    ["--except", "T1", "--budget", "1000"],
    ["--min-severity", "error", "--budget", "1000"],
    ["--since", "L10", "--until", "L200", "--budget", "100000"],
    ["--follow", "--every", "300", "--budget", "600", "--quiet"],
    ["--follow", "--every", "5000", "--budget", "1000"],
]
QUICK = [CASES[0], CASES[2], CASES[7], CASES[11]]
SELECTING = ("--only", "--except", "--min-severity", "--since", "--until", "--follow")
NOT_PORTED = 70  # the Rust port's status for what it does not do yet (src/error.rs)
SKIPPED = "skipped"


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


def compare(job) -> tuple[str, list[str]] | str | None:
    """None if the runs agree, SKIPPED for an unported candidate run under
    --skip-unported, else (name, problems)."""
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
    if skip_unported and c[0] == NOT_PORTED and b"not yet ported" in c[2]:
        for d in dumps:
            if d:
                os.unlink(d)
        return SKIPPED
    problems = []
    if r[0] != c[0]:
        problems.append(f"  exit code: reference {r[0]}, candidate {c[0]}"
                        + (f"\n  candidate stderr: {c[2].decode('utf-8', 'replace').strip()[:500]}" if c[2] else ""))
    if want_dump:
        with open(dumps[0], "rb") as f:
            rd = f.read()
        with open(dumps[1], "rb") as f:
            cd = f.read()
        if rd != cd:
            problems.append(first_difference(rd, cd, "per-line dump (first rows that differ)", context=1))
    if r[1] != c[1]:
        problems.append(first_difference(r[1], c[1], "output"))
    for d in dumps:
        if d:
            os.unlink(d)
    return (name, problems) if problems else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", required=True, help="command for the implementation under test")
    ap.add_argument("--reference", default=None, help="command for the reference (default: proto/pare.py)")
    ap.add_argument("--dump", action="store_true", help="also compare per-line results ($PARE_DUMP) in batch cases")
    ap.add_argument("--quick", action="store_true", help=f"{len(QUICK)} flag sets instead of {len(CASES)}")
    ap.add_argument("--logs", nargs="*", help="these logs instead of the corpora")
    ap.add_argument("--no-edge", action="store_true", help="leave out the generated edge cases")
    ap.add_argument("--skip-unported", action="store_true",
                    help=f"count candidate runs that exit {NOT_PORTED} as 'not yet ported' as skipped")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--show", type=int, default=5, help="differences to print in full (default 5)")
    args = ap.parse_args()

    ref = shlex.split(args.reference) if args.reference else [sys.executable, os.path.join(ROOT, "proto", "pare.py")]
    cand = shlex.split(args.candidate)
    cases = QUICK if args.quick else CASES
    with tempfile.TemporaryDirectory(prefix="pare-parity-") as tmp:
        logs = [os.path.abspath(p) for p in args.logs] if args.logs else corpus_logs()
        if not args.no_edge:
            logs += edge_cases(tmp)
        jobs = [(ref, cand, log, case, args.dump, args.skip_unported, tmp) for log in logs for case in cases]
        failures = []
        skipped = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for res in pool.map(compare, jobs):
                if res == SKIPPED:
                    skipped += 1
                elif res:
                    failures.append(res)
        for name, problems in failures[: args.show]:
            print(f"DIFF {name}")
            print("\n".join(problems))
        for name, _ in failures[args.show:]:
            print(f"DIFF {name}")
        print(f"# parity: {len(jobs)} runs ({len(logs)} inputs x {len(cases)} flag sets), "
              f"{len(failures)} differ" + (f", {skipped} skipped (not yet ported)" if args.skip_unported else "")
              + (" (per-line dumps compared)" if args.dump else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
