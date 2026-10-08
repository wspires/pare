# pare

A Unix filter that summarizes noisy command output (test runs, builds, logs)
to fit a token budget, for AI agents and humans. Nothing is lost: the raw
input is saved to a run file, and the summary ends with what was omitted and
how to get it back.

```
pytest -v 2>&1 | pare --budget 2000
```

A 308-line, 6,803-token pytest run becomes this (1,762 tokens), exactly
as printed; the last lines name the run file on the machine it ran on:

```
# pare: 308 lines, 19,046 B (~6,803 tok) → ~1,762 tok (budget 2,000) · 35 templates
## head (L1–L5)
     1│ ============================= test session starts ==============================
     2│ platform linux -- Python 3.13.16, pytest-9.1.1, pluggy-1.6.0 -- /home/dev/proj/.venv/bin/python
     3│ rootdir: /home/dev/proj
     4│ collecting ... collected 170 items
     5│ 
## templates (by salience)
T17  ×1 E L177: =================================== FAILURES ===================================
T24  ×5 E <PATH>:<NUM>: <*>
     e.g. L214: /usr/lib/python3.13/json/decoder.py:363: JSONDecodeError
T13  ×1 W L171: tests/test_cart.py::test_skipped SKIPPED (flaky on CI)                   [ 97%]
T14  ×1 W L172: tests/test_cart.py::test_xfail XFAIL (known bug #42)                     [ 98%]
T19  ×4 S <PATH>:<NUM>:
     e.g. L183: tests/test_cart.py:14:
T27  ×1 . L218: INFO     app.core:core.py:5 parsing 6 bytes
T26  ×5 S INFO app.core:conftest.py:<NUM> fixture setup for test
     e.g. L216: INFO     app.core:conftest.py:5 fixture setup for test
T22  ×1 . L196: self = <json.decoder.JSONDecoder object at 0x7f7e97664590>, s = '{"a": '
T23  ×1 (16 lines) E L197: idx = 0
   210│         except StopIteration as err:
   211│ >           raise JSONDecodeError("Expecting value", s, err.value) from None
   212│ E           json.decoder.JSONDecodeError: Expecting value: line 1 column 7 (char 6)
      ┆ (+9 more lines)
T18  ×5 (21 lines) S ___ <*> ___
     e.g. L178: ________________________________ test_parse_bad ________________________________
      ┆ (+2 more lines)
T21  ×5 (15 lines) . <PATH>:<NUM>: in <*>
     e.g. L185: app/core.py:6: in parse
      ┆ (+2 more lines)
T25  ×6 S --- Captured log <*> ---
     e.g. L215: ------------------------------ Captured log setup ------------------------------
T12  ×3 S <PATH>::test_add_rejects[<NUM>] PASSED [ <NUM>]
     e.g. L168: tests/test_cart.py::test_add_rejects[0] PASSED                           [ 95%]
T28  ×4 (26 lines) . host = '<IP>'
     e.g. L233: host = '127.0.0.1'
      ┆ (+3 more lines)
T20  ×6 (10 lines) . _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
     e.g. L184: _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
T6   ×10 S <PATH>::test_parse_ok[{"a": <NUM>}<NUM>] PASSED [ <NUM>]
     e.g. L126: tests/test_cart.py::test_parse_ok[{"a": 1}0] PASSED                      [ 71%]
T7   ×10 S <PATH>::test_parse_ok[[<NUM>]<NUM>] PASSED [ <NUM>]
     e.g. L127: tests/test_cart.py::test_parse_ok[[1,2,3]0] PASSED                       [ 71%]
T8   ×10 S <PATH>::test_parse_ok["s"<NUM>] PASSED [ <NUM>]
     e.g. L128: tests/test_cart.py::test_parse_ok["s"0] PASSED                           [ 72%]
T9   ×10 S <PATH>::test_parse_ok[{"nested": {"b": [<NUM>]}}<NUM>] PASSED [ <NUM>]
     e.g. L129: tests/test_cart.py::test_parse_ok[{"nested": {"b": [1,2]}}0] PASSED      [ 72%]
T5   ×120 S <PATH>::test_add_many[<NUM>] PASSED [ <NUM>]
     e.g. L6: tests/test_cart.py::test_add_many[0] PASSED                              [  0%]
## selected lines
   223│ >       assert c.total() == 0.9
   224│ E       assert 0.9000000000000001 == 0.9
   225│ E        +  where 0.9000000000000001 = total()
   226│ E        +    where total = <app.core.Cart object at 0x7f7e97197d40>.total
      ┆ (1 line)
   228│ tests/test_cart.py:18: AssertionError
      ┆ (20 lines)
   249│ E       ConnectionRefusedError: [Errno 111] Connection refused
      ┆ (22 lines)
   272│ E       TimeoutError: timed out
      ┆ (14 lines)
   287│ tests/test_net.py:9: in a
      ┆ (7 lines)
   295│ >   def c(): raise RuntimeError("deep failure at level 3")
      ┆ (1 line)
   297│ E   RuntimeError: deep failure at level 3
## tail (L284–L308)
   284│ 
   285│ tests/test_net.py:12:
      ┆ (12 lines)
   298│ 
      ┆ (3 lines)
   302│ =========================== short test summary info ============================
   303│ FAILED tests/test_cart.py::test_parse_bad - json.decoder.JSONDecodeError: Exp...
   304│ FAILED tests/test_cart.py::test_total_wrong - assert 0.9000000000000001 == 0.9
   305│ FAILED tests/test_net.py::test_connect[127.0.0.1] - ConnectionRefusedError: [...
   306│ FAILED tests/test_net.py::test_connect[10.255.255.1] - TimeoutError: timed out
   307│ FAILED tests/test_net.py::test_deep - RuntimeError: deep failure at level 3
   308│ ============= 5 failed, 163 passed, 1 skipped, 1 xfailed in 0.51s ==============
## omitted 260/308 lines (84.4%): T5 ×119, T28 ×23, T18 ×16, T21 ×13, T23 ×12, T6 ×9, T7 ×9, T8 ×9, T9 ×9, T20 ×7, T25 ×5, T26 ×4, T24 ×3, T12 ×2, T15 ×2, T19 ×2, T10 ×1, T11 ×1, T16 ×1 · 15 templates not listed; 24 blank
## raw: /root/.cache/pare/runs/d0ec7c06b92d.log
   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw
```

`pare --only T5 < runfile` then prints the 119 omitted lines of one
template; `--except`, `--min-severity error` and `--since L200` narrow it
further. Template ids are stable because the pipeline is deterministic.

Tokens are estimated as bytes / 2.8, the rate measured with tiktoken on
pare's output (cl100k and o200k agree); `--chars-per-token` changes it,
and 4 gives the prose rule of thumb. The budget is a hard ceiling; 1,000
to 2,000 is a good default for an agent loop, and small outputs pass
through untouched. See
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
reports output tokens and the share of must-survive lines (`<name>.keep`,
substring match) that each tool keeps. Tokens are bytes / 2.8, pare's
default and the measured cl100k/o200k rate for this text
(`bench/tokens.py`), so a budget of 2000 is 2,000 real tokens. Eight logs, 488k input tokens: real pytest, go test,
cargo, make and npm runs, plus three seeded synthetic logs (a Jest run, a
535 KB service outage, a 750 KB CI job). Versions: pare 0.1.0 / drain3
0.9.11, sqz 1.9.0, tidyout-cli 0.2.0; sqz runs one-shot
(`compress --no-cache`), tidyout with a fake command on PATH that replays
the log.

| tool | budget 2000: out tok, ratio, recall (min per case) | budget 600 |
|---|---|---|
| raw | 487,810, 100%, 100% | same |
| pare | 9,961, 2.0%, 99% (93%) | 4,549, 0.9%, 75% (53%) |
| sqz | 268,048, 55%, 55% (14%) | no budget knob |
| tidyout | 293,653, 60%, 89% (11%) | no budget knob |

At 2000 the one miss is a stack-frame location in the CI log. A real
600-token ceiling is tight: 2,000 is the budget to use for an agent loop,
and 600 is the floor at which the skeleton (header, windows, ledger) starts
to crowd out content. In the bytes/4 units used while the heuristics were
developed (`--chars-per-token 4`), pare scores 100% at 2000 (7,567 tok,
2.2%) and 97% (min 87%) at 600 (4,295 tok, 1.3%), about 850 real tokens;
`docs/DESIGN.md` has the per-case tables in both units.

**Read this as a training score.** The misses were fixed by looking at
these same eight cases, three of which are synthetic. The heuristics are
now frozen; the next numbers should come from a held-out corpus of real
logs with keep lists written before pare sees them
(`corpus-heldout/README.md` has the protocol) and from an agent-success
evaluation. With all tool-specific rules off, pare still reaches 88% at 600
and 94.5% at 2000 (bytes/4 units) on this corpus; the generic signals do
most of the work.

```
python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt -r bench/requirements.txt
.venv/bin/python bench/run.py [--budget 2000] [--corpus DIR] [--show-misses] [--json out.json]
.venv/bin/python bench/ablate.py        # recall with each heuristic switched off
.venv/bin/python bench/tokens.py        # the byte proxy vs tiktoken counts (needs tiktoken)
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
bench/tokens.py        byte proxy vs real tokenizer counts
bench/gen_synthetic.py generator for the seeded synthetic corpus files
corpus/                dev corpus: *.log, *.keep, *.cmd
corpus-generated/      real Maven, tsc, Terraform and kubectl output from small projects in src/ (a second dev corpus, not held-out)
corpus-heldout/        protocol for the held-out corpus (empty until collected)
docs/DESIGN.md         design notes, full results, port notes
```
