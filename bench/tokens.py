#!/usr/bin/env python3
"""Measure pare's bytes-per-token proxy against real tokenizers.

For every corpus/<name>.log, counts the raw file and pare's output at each
budget with tiktoken's cl100k_base and o200k_base (and any Hugging Face
tokenizer JSON given with --hf), and prints bytes per token for each, next to the bytes/4 rule of thumb.
The pare runs use --bytes-per-token 4 so the proxy error is visible; with
--bytes-per-token it also runs pare calibrated to that value (pare's
default is 2.8) and reports the real token count and recall per case.

Usage
    python bench/tokens.py [--budgets 600,2000] [--bytes-per-token 2.8] [--hf tokenizer.json] [--anthropic MODEL]

Needs `pip install tiktoken` (and `tokenizers` for --hf). tiktoken fetches
its vocabularies on first use; behind a proxy, point TIKTOKEN_CACHE_DIR at
a directory holding them.
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import pare_cmd, survivors  # noqa: E402


def load_encoders(hf: str | None, anthropic_model: str | None) -> dict:
    import tiktoken

    encs = {n: tiktoken.get_encoding(f"{n}_base").encode for n in ("cl100k", "o200k")}
    if hf:
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(hf)
        encs[os.path.basename(hf)[:12]] = lambda s: tok.encode(s).ids
    if anthropic_model:
        # The tokenizer behind current Claude models is not published; the
        # count_tokens endpoint is the exact source. Needs `pip install
        # anthropic` and credentials (ANTHROPIC_API_KEY or `ant auth login`).
        import anthropic

        client = anthropic.Anthropic()

        def count(text: str, _m=anthropic_model) -> list:
            n = client.messages.count_tokens(model=_m, messages=[{"role": "user", "content": text}]).input_tokens
            return [0] * n  # callers take len()

        encs[anthropic_model[:12]] = count
    return encs


def counts(encs: dict, data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    out = {"bytes": len(data), "b/4": math.ceil(len(data) / 4)}
    for name, enc in encs.items():
        out[name] = len(enc(text))
    return out


def pare(log_path: str, budget: int, cpt: float) -> bytes:
    with open(log_path, "rb") as f:
        # --chars-per-token: the reference's name for the flag; the Rust version takes it as an alias
        return subprocess.run(pare_cmd() + ["--budget", str(budget), "--no-tee", "--chars-per-token", str(cpt)],
                              stdin=f, capture_output=True, check=True).stdout


def table(title: str, encs: dict, items: list[tuple[str, bytes]]) -> None:
    print(f"\n== {title}")
    print(f"{'case':<16}{'bytes':>9}{'b/4':>8}" + "".join(f"{n:>9}{'B/tok':>7}" for n in encs))
    total: dict = {}
    for label, data in items:
        c = counts(encs, data)
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
        print(f"{label:<16}{c['bytes']:>9}{c['b/4']:>8}" + "".join(f"{c[n]:>9}{c['bytes'] / c[n]:>7.2f}" for n in encs))
    c = total
    print(f"{'TOTAL':<16}{c['bytes']:>9}{c['b/4']:>8}" + "".join(f"{c[n]:>9}{c['bytes'] / c[n]:>7.2f}" for n in encs))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=os.path.join(ROOT, "corpus"))
    ap.add_argument("--budgets", default="600,2000")
    ap.add_argument("--bytes-per-token", "--chars-per-token", dest="chars_per_token", type=float, metavar="N",
                    help="also run pare calibrated to this value")
    ap.add_argument("--hf", help="a Hugging Face tokenizer.json to count with as well")
    ap.add_argument("--anthropic", metavar="MODEL",
                    help="also count with Anthropic's count_tokens API for this model (e.g. claude-opus-5-5); "
                         "needs the anthropic package and credentials")
    args = ap.parse_args()
    encs = load_encoders(args.hf, args.anthropic)
    budgets = [int(b) for b in args.budgets.split(",")]
    logs = sorted(glob.glob(os.path.join(args.corpus, "*.log")))
    name = lambda p: os.path.basename(p)[:-4]  # noqa: E731

    table("raw corpus", encs, [(name(p), open(p, "rb").read()) for p in logs])
    for b in budgets:
        table(f"pare --budget {b} --bytes-per-token 4", encs, [(name(p), pare(p, b, 4.0)) for p in logs])

    if args.chars_per_token:
        cpt = args.chars_per_token
        first = next(iter(encs))
        for b in budgets:
            print(f"\n== pare --budget {b} --bytes-per-token {cpt:g}: {first} tokens and recall")
            tot = hit = total_keep = 0
            worst = 1.0
            for p in logs:
                out = pare(p, b, cpt)
                n = len(encs[first](out.decode("utf-8", "replace")))
                tot += n
                keep_path = p[:-4] + ".keep"
                keep = [ln.strip() for ln in open(keep_path, encoding="utf-8") if ln.strip()] if os.path.exists(keep_path) else []
                h = sum(survivors(out, keep))
                hit += h
                total_keep += len(keep)
                if keep:
                    worst = min(worst, h / len(keep))
                print(f"{name(p):<16}{len(out):>8} B{n:>7} tok   {h}/{len(keep)}")
            print(f"{'TOTAL':<16}{'':>10}{tot:>7} tok   {hit}/{total_keep} = {hit / total_keep:.1%}, min {worst:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
