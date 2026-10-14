# pare

A Unix filter that summarizes noisy command output (test runs, builds, logs)
to fit a token budget, for AI agents and humans. Nothing is lost: the raw
input is saved to a run file, and the summary ends with what was omitted and
how to get it back.

```
pytest -v 2>&1 | pare --budget 2000
```

A 308-line, 6,803-token pytest run becomes this (1,250 tokens), exactly
as printed; the last lines name the run file on the machine it ran on:

```
# pare: 308 lines, 19,046 B (~6,803 tok) → ~1,250 tok (budget 2,000) · 35 templates
## head (L1–L5)
1: ============================= test session starts ==============================
2: platform linux -- Python 3.13.16, pytest-9.1.1, pluggy-1.6.0 -- /home/dev/proj/.venv/bin/python
3: rootdir: /home/dev/proj
4: collecting ... collected 170 items
5: 
## templates (by salience)
T17  ×1 E L177: =================================== FAILURES ===================================
T24  ×5 E <PATH>:<NUM>: <*>
     e.g. L214: /usr/lib/python3.13/json/decoder.py:363: JSONDecodeError
T13  ×1 W L171: tests/test_cart.py::test_skipped SKIPPED (flaky on CI)                   [ 97%]
T14  ×1 W L172: tests/test_cart.py::test_xfail XFAIL (known bug #42)                     [ 98%]
T19  ×4 S L183: tests/test_cart.py:14:
T27  ×1 . L218: INFO     app.core:core.py:5 parsing 6 bytes
T26  ×5 S L216: INFO     app.core:conftest.py:5 fixture setup for test
T22  ×1 . L196: self = <json.decoder.JSONDecoder object at 0x7f7e97664590>, s = '{"a": '
T23  ×1 (16 lines) E L197: idx = 0
210:         except StopIteration as err:
211: >           raise JSONDecodeError("Expecting value", s, err.value) from None
212: E           json.decoder.JSONDecodeError: Expecting value: line 1 column 7 (char 6)
  ┆ (+9 more lines)
T18  ×5 (21 lines) S ___ <*> ___
     e.g. L178: ________________________________ test_parse_bad ________________________________
  ┆ (+2 more lines)
T21  ×5 (15 lines) . <PATH>:<NUM>: in <*>
     e.g. L185: app/core.py:6: in parse
  ┆ (+2 more lines)
T25  ×6 S --- Captured log <*> ---
     e.g. L215: ------------------------------ Captured log setup ------------------------------
## selected lines
223: >       assert c.total() == 0.9
224: E       assert 0.9000000000000001 == 0.9
225: E        +  where 0.9000000000000001 = total()
226: E        +    where total = <app.core.Cart object at 0x7f7e97197d40>.total
  ┆ (1 line)
228: tests/test_cart.py:18: AssertionError
  ┆ (20 lines)
249: E       ConnectionRefusedError: [Errno 111] Connection refused
  ┆ (1 line)
251: app/core.py:15: ConnectionRefusedError
  ┆ (20 lines)
272: E       TimeoutError: timed out
  ┆ (1 line)
274: app/core.py:15: TimeoutError
  ┆ (12 lines)
287: tests/test_net.py:9: in a
  ┆ (7 lines)
295: >   def c(): raise RuntimeError("deep failure at level 3")
  ┆ (1 line)
297: E   RuntimeError: deep failure at level 3
## tail (L284–L308)
284: 
285: tests/test_net.py:12:
  ┆ (12 lines)
298: 
  ┆ (3 lines)
302: =========================== short test summary info ============================
303: FAILED tests/test_cart.py::test_parse_bad - json.decoder.JSONDecodeError: Exp...
304: FAILED tests/test_cart.py::test_total_wrong - assert 0.9000000000000001 == 0.9
305: FAILED tests/test_net.py::test_connect[127.0.0.1] - ConnectionRefusedError: [...
306: FAILED tests/test_net.py::test_connect[10.255.255.1] - TimeoutError: timed out
307: FAILED tests/test_net.py::test_deep - RuntimeError: deep failure at level 3
308: ============= 5 failed, 163 passed, 1 skipped, 1 xfailed in 0.51s ==============
## omitted 266/308 lines (86.4%): T5 ×120, T28 ×24, T18 ×16, T21 ×13, T23 ×12, T6 ×10, T7 ×10, T8 ×10, T9 ×10, T20 ×8, T25 ×5, T26 ×4, T12 ×3, T15 ×2, T19 ×2, T10 ×1, T11 ×1, T16 ×1, T24 ×1 · 23 templates not listed; 24 blank
## raw: /root/.cache/pare/runs/d0ec7c06b92d.log
   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw
```

`pare --only T5 < runfile` then prints the 120 omitted lines of one
template; `--except`, `--min-severity error` and `--since L200` narrow it
further. Template ids are stable because the pipeline is deterministic.
In command mode (`pare -- CMD`), `pare --stats` totals what the command
printed against what pare printed, over every run so far.

Run files live in `~/.cache/pare/runs` (`$PARE_RUN_DIR`). pare prunes
them itself, at most hourly: those older than 14 days go, then the oldest
until the rest fit in 500 MB, never one less than a day old
(`$PARE_RUN_KEEP_DAYS`, `$PARE_RUN_MAX_MB`, 0 for no limit;
`$PARE_RUN_PRUNE=off` stops it). `pare --prune [--dry-run]` sweeps now.
Only files pare named are touched, and a pruned run keeps its small
`.json` record, so `--stats` still counts it.

Tokens are estimated as bytes / 2.8, the rate measured with tiktoken on
pare's output (cl100k and o200k agree); `--chars-per-token` changes it,
and 4 gives the prose rule of thumb. The budget is a hard ceiling; 1,000
to 2,000 is a good default for an agent loop, and small outputs pass
through untouched. See
[docs/DESIGN.md](docs/DESIGN.md#tokens-and-the-budget).

## Command mode and agents

`pare -- CMD ARGS` runs the command itself instead of reading a pipe:

```
pare --budget 2000 -- ./gradlew build
```

The exit code survives (a pipe reports pare's), the summary ends with the
exit status (`## raw: … · exit 1`), and the command is recorded next to
the run file. Output that fits the budget is printed as it is, with no
`## raw:` line (it is already all of it), plus `## exit N` when N is not 0. stdout and stderr are merged in the order the command wrote
them. A SIGTERM from a tool's timeout is passed to the command, and pare
still summarises what it printed; once the command has exited, pare does
not wait for a background process it left holding the pipe.

[integrations/README.md](integrations/README.md) has two ways to route an
agent's noisy commands through it: an instruction for `CLAUDE.md` or
`AGENTS.md`, and a Claude Code hook that rewrites test, build and install
commands automatically. Every such run leaves a run file, and
`bench/collect.py` turns run files into held-out cases (see
[ROADMAP.md](ROADMAP.md)).

## Following a stream

`pare --follow` (or `-f`) summarises input as it arrives instead of at end
of input: a long build, a server log, a `tail -f`. Each window becomes one
block in the usual format, budgeted on its own, and the blocks are
separated by a blank line:

```
kubectl logs -f deploy/api | pare -f --interval 30 --budget 600
```

A window closes after `--every` lines (default 20,000, also the window
cap), after `--interval` seconds even while the stream is idle, half a
second after a line at `--flush-on` severity arrives (default `error`,
at most one such flush per `--min-interval` seconds), and at end of
input. A line-count cut waits for the next record head, a timed cut
carries the open record into the next window (its frames keep inheriting
the error head's salience and are attributed to the same template), and
a window holding only blank lines is neither printed nor cut, so a stack
trace is never split and the template ids are the ones a batch run over
the finished run file would assign (a randomized differential test
against the batch pass checks this). A window with no new shape and nothing above the salience
floor is one heartbeat line (`--quiet` drops it); a template first seen
in a window is marked `new`, and one arriving much faster than its
running rate gets a rate note. A block that starts inside a record opens
with `↳ continues L1: ERROR …`, naming the head its first lines belong
to. Blocks start with a `# pare #` line; split the stream on that, since
a verbatim block can contain the input's own blank lines. The run file is named up front and
appended as the stream runs, so every block's `--since/--until` range can
be expanded with the usual flags afterwards. Unix only: the loop waits
on stdin with `select`.

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
substring match, after pare's compact lists of failing tests are written
out in full; `--strict` matches the literal text; a keep line may list
equivalent forms as `a || b`) that each tool keeps. Tokens are bytes / 2.8, pare's
default and the measured cl100k/o200k rate for this text
(`bench/tokens.py`), so a budget of 2000 is 2,000 real tokens. Eight logs (a ninth, a synthetic Android logcat capture, was added later
and is reported in `docs/DESIGN.md`), 488k input tokens: real pytest, go test,
cargo, make and npm runs, plus three seeded synthetic logs (a Jest run, a
535 KB service outage, a 750 KB CI job). Versions: pare 0.1.0 / drain3
0.9.11, sqz 1.9.0, tidyout-cli 0.2.0; sqz runs one-shot
(`compress --no-cache`), tidyout with a fake command on PATH that replays
the log.

| tool | budget 2000: out tok, ratio, recall (min per case) | budget 600 |
|---|---|---|
| raw | 487,810, 100%, 100% | same |
| pare | 8,494, 1.7%, 100% (100%) | 4,352, 0.9%, 78% (60%) |
| sqz | 268,054, 55%, 55% (14%) | no budget knob |
| tidyout | 293,653, 60%, 89% (11%) | no budget knob |

A real 600-token ceiling is tight: 2,000 is the budget to use for an agent
loop, and 600 is the floor at which the skeleton (header, windows, ledger)
starts to crowd out content. `docs/DESIGN.md` has the per-case tables,
and the same tables in the bytes/4 units used while the heuristics were
developed.

On `corpus-generated/`, real Maven, tsc, Terraform and kubectl output from
small projects written to fail (seven logs, 36k input tokens, not used for
tuning): pare keeps 96% at 2000 (7,638 tok, 21%; the two misses are a
third compile error sharing a template with the second, and the reactor
summary's FAILURE line, in the cold-cache Maven build) and 60% at 600
(3,527 tok, min 11% on the Terraform apply, whose 20 near-identical plan
blocks crowd out the three check-block warnings). sqz keeps 74% at 20%, tidyout
everything at 100%. These logs are short and dense in signal, which is
where a 600-token summary has the least room.

**Read this as a training score.** The misses were fixed by looking at
these same eight cases, three of which are synthetic. The first held-out
numbers, on test runs labelled by datasets rather than by anyone working
on pare, were much lower: on 16 SWE-bench Verified tasks pare kept 60% of
the failing tests at 2,000 tokens, and on 11 Defects4J bugs 29% of the
failing tests and exceptions at 600. Both had one cause, several failing
tests named in lines of one shape and listed once with a count, which is
now fixed (`docs/DESIGN.md`, "First change from held-out evidence"). On
fresh samples drawn after the fix and never tuned on, pare keeps 89% of
17 SWE-bench tasks' failing tests at 2,000 tokens (63% before the fix,
output 11% of the input) and 51% of 16 Defects4J bugs' tests and
exceptions at 600 (30% before); tidyout keeps everything at 98 to 100% of
the input size. The misses those samples showed are fixed since: runs
with more failing tests than lines fit (now listed compactly, the shared
prefix once and one short item per test), parametrized test names that
differ only in numbers, the few failures that matter among many that fail
one way (listed first), and Maven's build chatter at small budgets. On a
third pair of samples drawn after all that and never read (16 SWE-bench
tasks, 18 Defects4J bugs), pare keeps 100% and 98% at 2,000 tokens and
86% and 67% at 600, where the same cases gave 95% and 86%, 77% and 33%
before any of these fixes (`corpus-heldout/README.md`; 76.5% on
Defects4J at 600 once a keep line accepts Maven's short form of a test
name). With all tool-specific rules off, pare reaches 71% at 600 and 96%
at 2,000 on this corpus; on held-out Defects4J the rules change nothing
and on held-out SWE-bench they are worth up to 18 points, almost all of
it from the pytest markers and the blank-line-in-a-block rule
(`docs/DESIGN.md`, "Tool-specific rules").

```
python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt -r bench/requirements.txt
.venv/bin/python bench/run.py [--budget 2000] [--corpus DIR] [--show-misses] [--json out.json]
.venv/bin/python bench/ablate.py        # recall with each heuristic switched off, or a removed one back on
.venv/bin/python bench/tokens.py        # the byte proxy vs tiktoken counts (needs tiktoken)
```

On macOS: `uv tool install tidyout-cli` puts `tidyout` on PATH; for sqz,
`brew install ojuschugh1/sqz/sqz`, `npm i -g sqz-cli` or `cargo install
sqz-cli` (not `pip install sqz`, which fetches a pinned 0.1.0 binary).

## How it works

1. **Tee** stdin to a content-hashed run file, in one pass with the steps
   below: pare keeps about 40 bytes per line in memory, not the lines,
   so a 30 MB log runs in 65 MB.
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
.venv/bin/pip install -e '.[dev]'       # pare itself and pytest (the `dev` extra)
.venv/bin/python -m pytest -q proto/
```

A few tests skip themselves when an optional tool is missing: tidyout
(`.[bench]`) and the SWE-bench package (`.[datasets]`). Extras combine:
`pip install -e '.[dev,bench,datasets]'`.

239 tests: severity forms and their false positives, vocabulary files, the
hard budget limit on every corpus file at several sizes, determinism,
pass-through, the selection flags, memory on a large input, follow mode
(including a randomized differential test against the batch pass),
command mode, the collection, harvest, SWE-bench and Defects4J helpers, and the Claude Code hook.

## Layout

```
proto/pare.py          the prototype (drain3 is its only dependency)
proto/test_pare.py     pytest suite
proto/vocab.example    example vocabulary extension (German)
bench/run.py           benchmark runner
bench/ablate.py        ablation study of the scoring heuristics
bench/tokens.py        byte proxy vs real tokenizer counts
bench/gen_synthetic.py generator for the seeded synthetic corpus files
bench/collect.py       turn run files into held-out cases (never shows pare's output)
bench/harvest.py       harvest held-out candidates from Claude Code transcripts (scrubbed, staged for review)
bench/swebench_cases.py held-out cases from SWE-bench tasks, labelled by their failing tests (needs Docker)
bench/defects4j_cases.py held-out Java cases from Defects4J bugs, labelled by their trigger tests (needs Docker)
integrations/          agent instructions and a Claude Code hook (integrations/README.md)
corpus/                dev corpus: *.log, *.keep, *.cmd
corpus-generated/      real Maven, tsc, Terraform and kubectl output from small projects in src/ (a second dev corpus, not held-out)
corpus-dev/defects4j/  Defects4J development sample (seed 1), used to tune the failing-test fix
corpus-heldout/        protocol and held-out cases: SWE-bench, Defects4J (results in its README)
docs/DESIGN.md         design notes, full results, port notes
ROADMAP.md             what comes next, in order, and who can do it
```
