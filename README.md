# pare

A Unix filter that summarizes noisy command output (test runs, builds, logs)
to fit a token budget, for AI agents and humans. Nothing is lost: the raw
input is saved to a run file, and the summary ends with what was omitted and
how to get it back.

```
pytest -v 2>&1 | pare --budget 600
```

A 308-line, 4,762-token pytest run becomes this (596 tokens), exactly as
printed; the last line names the run file on the machine it ran on:

```
# pare: 308 lines, 19,046 B (~4,762 tok) → ~596 tok (budget 600) · 35 templates
## head (L1–L3)
     1│ ============================= test session starts ==============================
     2│ platform linux -- Python 3.13.16, pytest-9.1.1, pluggy-1.6.0 -- /home/dev/proj/.venv/bin/python
     3│ rootdir: /home/dev/proj
## templates (by salience)
T10  ×1 E L166: tests/test_cart.py::test_parse_bad FAILED                                [ 94%]
T11  ×1 E L167: tests/test_cart.py::test_total_wrong FAILED                              [ 95%]
T17  ×1 E L177: =================================== FAILURES ===================================
T30  ×1 E L303: FAILED tests/test_cart.py::test_parse_bad - json.decoder.JSONDecodeError: Exp...
T31  ×1 E L304: FAILED tests/test_cart.py::test_total_wrong - assert 0.9000000000000001 == 0.9
T24  ×5 E L214: /usr/lib/python3.13/json/decoder.py:363: JSONDecodeError
T13  ×1 W L171: tests/test_cart.py::test_skipped SKIPPED (flaky on CI)                   [ 97%]
T14  ×1 W L172: tests/test_cart.py::test_xfail XFAIL (known bug #42)                     [ 98%]
T19  ×4 S L183: tests/test_cart.py:14:
## selected lines
   210│         except StopIteration as err:
      ┆ (1 line)
   212│ E           json.decoder.JSONDecodeError: Expecting value: line 1 column 7 (char 6)
      ┆ (10 lines)
   223│ >       assert c.total() == 0.9
   224│ E       assert 0.9000000000000001 == 0.9
   225│ E        +  where 0.9000000000000001 = total()
      ┆ (23 lines)
   249│ E       ConnectionRefusedError: [Errno 111] Connection refused
      ┆ (22 lines)
   272│ E       TimeoutError: timed out
      ┆ (14 lines)
   287│ tests/test_net.py:9: in a
      ┆ (9 lines)
   297│ E   RuntimeError: deep failure at level 3
## tail (L305–L308)
   305│ FAILED tests/test_net.py::test_connect[127.0.0.1] - ConnectionRefusedError: [...
   306│ FAILED tests/test_net.py::test_connect[10.255.255.1] - TimeoutError: timed out
   307│ FAILED tests/test_net.py::test_deep - RuntimeError: deep failure at level 3
   308│ ============= 5 failed, 163 passed, 1 skipped, 1 xfailed in 0.51s ==============
## omitted 283/308 lines (91.9%): T5 ×120, T28 ×24, T18 ×18, T21 ×14, T23 ×14, T6 ×10, T7 ×10, T8 ×10 · +13 more templates; 26 templates not listed; 27 blank
## raw: /root/.cache/pare/runs/d0ec7c06b92d.log
```

`pare --only T5 < runfile` then prints the 120 omitted lines of one
template; `--except`, `--min-severity error` and `--since L200` narrow it
further. Template ids are stable because the pipeline is deterministic.

Tokens are estimated as bytes / 4, a proxy. Measured with tiktoken, pare's
output runs 2.85 bytes per token (cl100k and o200k), so a budget of 600 is
about 850 real tokens and 2000 about 2,850; `--chars-per-token 2.8` budgets
in real units. The budget is a hard ceiling; 1,000 to 2,000 is a good
default for an agent loop, and small outputs pass through untouched. See
[docs/DESIGN.md](docs/DESIGN.md#tokens-and-the-budget).

## Install

The only dependency is [drain3](https://github.com/logpai/Drain3) (pinned).
Python 3.10 or newer.

```
# a `pare` command on your PATH (add --editable to track the checkout)
uv tool install .            # or: pipx install .
pytest -v 2>&1 | pare --budget 2000

# or run the script directly; it declares its dependency inline (PEP 723)
pytest -v 2>&1 | uv run proto/pare.py --budget 2000

# or a virtualenv
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,bench]'
pytest -v 2>&1 | .venv/bin/pare --budget 2000
```

`proto/pare.py` is executable; a bare `python3 proto/pare.py` without
drain3 stops with these options rather than a traceback.

## Benchmark

`bench/run.py` runs pare, [sqz](https://github.com/ojuschugh1/sqz) and
[tidyout](https://github.com/sathish-2000/tidyout) over `corpus/` and
reports output tokens (`bytes / 4`; real tokenizers count about 40 % more,
see `bench/tokens.py`) and the share of must-survive lines (`<name>.keep`,
substring match) that each tool keeps. Eight logs, 341k
input tokens: real pytest, go test, cargo, make and npm runs, plus three
seeded synthetic logs (a Jest run, a 535 KB service outage, a 750 KB CI
job). Versions: pare 0.1.0 / drain3 0.9.11, sqz 1.9.0, tidyout-cli 0.2.0;
sqz runs one-shot (`compress --no-cache`), tidyout with a fake command on
PATH that replays the log.

| tool | budget 2000: out tok, ratio, recall (min per case) | budget 600 |
|---|---|---|
| raw | 341,467, 100%, 100% | same |
| pare | 7,567, 2.2%, 100% (100%) | 4,295, 1.3%, 97% (87%) |
| sqz | 187,634, 55%, 55% (14%) | no budget knob |
| tidyout | 205,556, 60%, 89% (11%) | no budget knob |

**Read this as a training score.** The misses were fixed by looking at
these same eight cases, three of which are synthetic. The heuristics are
now frozen; the next numbers should come from a held-out corpus of real
logs with keep lists written before pare sees them
(`corpus-heldout/README.md` has the protocol) and from an agent-success
evaluation. With all tool-specific rules off, pare still reaches 88% at 600
and 94.5% at 2000 on this corpus; the generic signals do most of the work.

```
python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt -r bench/requirements.txt
.venv/bin/python bench/run.py [--budget 2000] [--corpus DIR] [--show-misses] [--json out.json]
.venv/bin/python bench/ablate.py        # recall with each heuristic switched off
.venv/bin/python bench/tokens.py        # bytes/4 vs tiktoken counts (needs tiktoken)
```

On macOS: `uv tool install tidyout-cli` puts `tidyout` on PATH; for sqz,
`brew install ojuschugh1/sqz/sqz`, `npm i -g sqz-cli` or `cargo install
sqz-cli` (not `pip install sqz`, which fetches a pinned 0.1.0 binary).

## How it works

1. **Tee** stdin to a content-hashed run file.
2. **Segment** lines into records: indented continuations, stack frames and
   compiler gutters join the line above.
3. **Mask** timestamps, IPs, hashes, paths and numbers, then **cluster** the
   masked head lines into templates with drain3.
4. **Score** each line by its structural severity (log level, HTTP status,
   exit code) and vocabulary (error, warning, summary, frame, pass, noise),
   and each template by its head line plus its rarity.
5. **Allocate** the budget: head and tail windows, then one priority queue
   in which template entries and raw lines compete on the same scale, with
   diminishing returns for repeats and a hard limit on size.
6. **End** with a ledger of what was omitted and the run-file path.

Every scoring term survived an ablation; nine that did not were removed.
The details, the full per-case tables, the miss-by-miss history and notes
for a native port are in [docs/DESIGN.md](docs/DESIGN.md).

## Tests

```
.venv/bin/pip install pytest && .venv/bin/python -m pytest proto/
```

133 tests: severity forms and their false positives, vocabulary files, the
hard budget limit on every corpus file at several sizes, determinism,
pass-through, and the selection flags.

## Layout

```
proto/pare.py          the prototype (drain3 is its only dependency)
proto/test_pare.py     pytest suite
proto/vocab.example    example vocabulary extension (German)
bench/run.py           benchmark runner
bench/ablate.py        ablation study of the scoring heuristics
bench/tokens.py        bytes/4 proxy vs real tokenizer counts
bench/gen_synthetic.py generator for the seeded synthetic corpus files
corpus/                dev corpus: *.log, *.keep, *.cmd
corpus-heldout/        protocol for the held-out corpus (empty until collected)
docs/DESIGN.md         design notes, full results, port notes
```
