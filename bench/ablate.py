#!/usr/bin/env python3
"""Ablation study for pare's scoring heuristics.

    .venv/bin/python bench/ablate.py [--budgets 600,2000] [--corpus corpus]

Runs the pipeline in-process on every corpus file, once with the defaults
and once per heuristic with that heuristic switched off (by overriding the
module constant that controls it), and prints recall per budget. A
heuristic whose removal does not lower recall is a candidate for removal.
"""

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "proto"))
import pare  # noqa: E402

ABLATIONS = {
    "default": {},
    "no rarity (templates)": {"RARITY_WEIGHT": 0.0},
    "no inheritance": {"INHERIT": 0.0},
    "no depth penalty": {"DEPTH_PENALTY": 0.0},
    "no location bonus": {"LOCATION_BONUS": 0.0},
    "no density factor": {"DENSITY_POWER": 0.0},
    "no repeat penalty": {"FILL_PENALTY": 0.0},
    "repeat penalty 0.75": {"FILL_PENALTY": 0.75},
    "no salience floor": {"FILL_FLOOR": -99.0},
    "no near-dup suppression": {"NEAR_DUP_CONTAINMENT": 9.0, "NEAR_DUP_MIN_TOKENS": 10**9},
    "no window skipping": {"WINDOW_SKIP_COUNT": 10**9},
    "no compact mode": {"COMPACT_BUDGET": 0, "HINT_BUDGET": 0},
    "generic signals only": {"TOOL_SPECIFIC": False},
}


def run_case(log_path, keep, budget):
    fd, spool = tempfile.mkstemp(prefix="pare-ablate-", suffix=".spool")
    os.close(fd)
    try:
        with open(log_path, "rb") as f:
            store, _ = pare.analyze(f, width=200, spool_path=spool)
        store.open()
        pare.score_templates(store)
        out = pare.summarize(store, budget=budget, head_n=5, tail_n=10, width=200,
                             run_path=None, in_bytes=store.total_bytes)
        store.close()
    finally:
        os.unlink(spool)
    hit = sum(1 for k in keep if k in out)
    return hit, len(keep), pare.tokens(len(out.encode()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=os.path.join(ROOT, "corpus"))
    ap.add_argument("--budgets", default="600,2000")
    ap.add_argument("--only", help="comma-separated ablation names to run (default: all)")
    args = ap.parse_args()
    budgets = [int(b) for b in args.budgets.split(",")]
    names = sorted(f[:-4] for f in os.listdir(args.corpus) if f.endswith(".log"))
    cases = []
    for name in names:
        keep_path = os.path.join(args.corpus, name + ".keep")
        keep = [l.strip() for l in open(keep_path, encoding="utf-8")] if os.path.exists(keep_path) else []
        cases.append((name, os.path.join(args.corpus, name + ".log"), [k for k in keep if k]))

    # Cache the expensive, heuristic-independent stages per file.
    defaults = {k: getattr(pare, k) for v in ABLATIONS.values() for k in v}
    wanted = [n for n in ABLATIONS if not args.only or n in args.only.split(",")]
    hdr = f"{'ablation':<28}" + "".join(f"{'recall@' + str(b):>12}{'out tok':>9}" for b in budgets) + "   worst case"
    print(hdr)
    print("-" * len(hdr))
    for name in wanted:
        for k, v in defaults.items():
            setattr(pare, k, v)
        for k, v in ABLATIONS[name].items():
            setattr(pare, k, v)
        pare.VOCAB = pare.Vocab(pare.DEFAULT_VOCAB)
        row = f"{name:<28}"
        worst = ("", 1.0)
        for b in budgets:
            hit = tot = toks = 0
            for cname, path, keep in cases:
                h, t, tk = run_case(path, keep, b)
                hit += h
                tot += t
                toks += tk
                if t and h / t < worst[1]:
                    worst = (f"{cname}@{b} {h}/{t}", h / t)
            row += f"{100 * hit / tot:>11.1f}%{toks:>9}"
        print(row + "   " + worst[0])
        sys.stdout.flush()


if __name__ == "__main__":
    main()
