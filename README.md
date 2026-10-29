# pare

A Unix filter that summarizes noisy command output (test runs, builds, logs)
to fit a token budget, for AI agents and humans. Nothing is lost: the raw
input is saved to a run file, and the summary ends with what was omitted and
how to get it back.

```
pytest -v 2>&1 | pare --budget 2000
```

A 308-line, 9,523-token pytest run becomes this (1,763 tokens), exactly
as printed; the last lines name the run file on the machine it ran on:

```
# pare: 308 lines, 19,046 B (~9,523 tok) → ~1,763 tok (budget 2,000) · 35 templates
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
## tail (L285–L308)
285: tests/test_net.py:12:
  ┆ (12 lines)
298: 
299: tests/test_net.py:11: RuntimeError
  ┆ (2 lines)
302: =========================== short test summary info ============================
303: FAILED tests/test_cart.py::test_parse_bad - json.decoder.JSONDecodeError: Exp...
304: FAILED tests/test_cart.py::test_total_wrong - assert 0.9000000000000001 == 0.9
305: FAILED tests/test_net.py::test_connect[127.0.0.1] - ConnectionRefusedError: [...
306: FAILED tests/test_net.py::test_connect[10.255.255.1] - TimeoutError: timed out
307: FAILED tests/test_net.py::test_deep - RuntimeError: deep failure at level 3
308: ============= 5 failed, 163 passed, 1 skipped, 1 xfailed in 0.51s ==============
## omitted 266/308 lines (86.4%): T5 ×120, T28 ×24, T18 ×16, T21 ×13, T23 ×12, T6 ×10, T7 ×10, T8 ×10, T9 ×10, T20 ×8, T25 ×5, T26 ×4, T12 ×3, T15 ×2, T19 ×2, T10 ×1, T11 ×1, T16 ×1 · 23 templates not listed; 25 blank
## raw: ~/.cache/pare/runs/d0ec7c06b92d.log
   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw
```

`pare --only T5 < runfile` then prints the 120 omitted lines of one
template; `--except`, `--min-severity error` and `--since L200` narrow it
further.

## Why pare

An agent that runs a test suite or a build gets the whole output back,
and the part it needs, one failing test with its traceback or the one
compile error, is a few lines among thousands. The usual cuts lose
exactly those lines:

- `| tail -50` keeps the verdict and loses the failure above it; `| head`
  keeps the start. Neither knows where the error is.
- `| grep -i error` keeps the matching lines without their stack trace or
  the verdict, and misses tools whose failures say `FAILED` or `panic:`.
- A harness that truncates long tool output is head and tail applied
  blindly, with nothing recoverable.
- The tools' own quiet flags (`pytest -q`, `mvn -q`) cut at the source,
  each tool in its own way, and a quiet run still prints every failure
  in full.
- Asking the model to summarise spends the tokens that were to be saved,
  and what it leaves out is gone.

pare keeps the lines that differ, counts the shapes that repeat, and
fits the result to a budget. Three things make it safe to rely on:

- **Nothing is lost.** The input is saved before anything else happens,
  the ledger names each omitted template with its count, and the flags
  above pull any of it back.
- **It is deterministic.** The pipeline has no randomness, so the same
  input gives the same summary and the same template ids, and a summary
  can be quoted, diffed and expanded a day later.
- **It is one filter for every tool.** The signals are structural (log
  levels, HTTP statuses, exit codes, indentation, stack frames) with a
  vocabulary on top, so Maven, pytest, Terraform and a JSON log get the
  same treatment; the tool-specific rules add a few points on one
  dataset and nothing on the other ([Results](#results)).

Use it for anything whose output can be long and whose failures are the
point: test suites, builds, linters, package installs, `terraform plan`,
`kubectl logs`, CI runs. Do not use it for output you need exactly: a
file you are reading, `git diff`, `grep`, anything you will edit from.
Source code is not a log either; pare summarises it, pointlessly. A
binary file (an executable, an image, an archive) gets one line saying
so, not a summary. Output that already fits the budget passes through
untouched, so wrapping a short command costs nothing but its run file.

## Install

pare is a single binary with no dependencies. Any of these installs the
`pare` command (the package is `pare-cli` where `pare` was taken):

```
brew install wspires/tap/pare                    # macOS and Linux (Homebrew)
curl -sSfL https://raw.githubusercontent.com/wspires/pare/main/install.sh | sh
                                                 # macOS and Linux: to ~/.local/bin
pipx install pare-cli                            # or: uv tool install pare-cli, uvx pare-cli
cargo binstall pare-cli                          # the release binary, via cargo
cargo install pare-cli                           # built from source (Rust 1.87+)
```

Or download an archive for Linux (x86-64 and ARM, statically linked),
macOS (Apple Silicon and Intel) or Windows from the
[releases](https://github.com/wspires/pare/releases) page; each has a
`SHA256SUMS` entry.

Building from a checkout is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Use

### The budget

The budget is a hard ceiling in tokens on what pare prints. Tokens are
estimated as bytes / 2.0, the rate measured on pare's output with the
tokenizer of the current Claude models, through Anthropic's token-count
API (`--bytes-per-token` changes it: 2.8 is the rate for OpenAI's cl100k
and o200k tokenizers, and the default until October 2026).

By default the size follows the log (`auto`): output under 4,000
tokens is printed whole, since on the agent runs a summary of a log
that size cost a round trip more often than it saved tokens; above
that pare summarises, and stops once what is left is below error level,
at most 2,000 tokens. On the latest held-out samples it keeps what a
fixed 2,000 keeps ([Results](#results)).
`--budget auto:4000` raises the ceiling (and the pass-through, which is
twice it); `--budget N` is a fixed budget
the summary fills (1,000 to 2,000 suits an agent loop; 600 is tight, see
[Results](#results)). Without
`--budget`, pare takes `$PARE_BUDGET`, else `auto`, so a harness can set
it once for every call. `--ascii` writes pare's own marks (`→ · × ┆ …`)
as ASCII; the log's text is never changed. Details:
[docs/DESIGN.md](docs/DESIGN.md#tokens-and-the-budget) and
[docs/USAGE.md](docs/USAGE.md).

### Running the command

`pare -- CMD ARGS` runs the command itself instead of reading a pipe:

```
pare -- ./gradlew build
```

The command's exit code survives (a pipe would report pare's), the
summary ends with the exit status, stdout and stderr are merged in the
order the command wrote them, and the command is recorded next to the run
file. `pare stats` totals what commands printed against what pare
printed, over every run so far.

[integrations/README.md](integrations/README.md) has two ways to route an
agent's noisy commands through pare: an instruction for `CLAUDE.md` or
`AGENTS.md`, and a Claude Code hook that rewrites test, build and install
commands automatically.

### Following a stream

`pare --follow` (or `-f`) summarises input as it arrives: a long build, a
server log, a `tail -f`. Each window becomes one block in the usual
format, budgeted on its own:

```
kubectl logs -f deploy/api | pare -f --budget 600
```

A window closes after `--every` lines, after `--interval` seconds (30 by
default), or half a second after a line at `--flush-on` severity
(default `error`) arrives. A stack trace is never split across windows,
and template ids are the ones a batch run over the finished run file
would assign, so every block can be expanded afterwards with `--only`
and `--since`. Details: [docs/USAGE.md](docs/USAGE.md#follow-mode).

### Run files

Run files live in `~/.cache/pare/runs` (`$PARE_RUN_DIR`). pare prunes
them itself: runs older than 14 days go, then the oldest until the rest
fit in 500 MB (`$PARE_RUN_KEEP_DAYS`, `$PARE_RUN_MAX_MB`;
`$PARE_RUN_PRUNE=off` stops it; `pare prune [--dry-run]` sweeps now).
Details: [docs/USAGE.md](docs/USAGE.md#run-files).

## Teaching pare your logs

pare needs no configuration: it learns each input's kinds of lines as it
reads them. What it knows in advance is a vocabulary (error, warning,
summary and noise words, log-level tokens, the shapes of stack frames)
which a file can extend for your tools or language:

```
pare vocab > my.vocab                 # every built-in entry, in file format
pare --vocab my.vocab ...             # or: export PARE_VOCAB=my.vocab
```

In the file, `[error]` adds entries to a class and `[error replace]`
replaces its built-in ones; a plain line is a word or phrase (matched
whole, case-insensitively) and a `/.../` line a regular expression.
[reference/vocab.example](reference/vocab.example) adds German.

The `continuation` class says which lines continue the record above
whatever their indentation, so a stack trace stays one record. pare knows
Java, JavaScript, Python, rustc and pytest shapes; for others, add
regexes. A Go panic prints its frames at column 0:

```
[continuation]
/^goroutine \d+ \[/
/^[\w./*()]+\(.*\)$/
/^\t/
```

A `[mask NAME]` section turns what its regexes match into `<NAME>` before
lines are clustered, for ids pare cannot recognize as variable. When a
tenant name leads each line, every tenant gets a template of its own
(`acme: nightly sync failed`, `globex: nightly sync failed`, ...); with

```
[mask TENANT]
/^[a-z]+(?=:)/
```

they are one template. Masks run before the built-in ones (timestamps,
numbers, ...), on the line as written. Template ids depend on the masks,
so `--only T3` on a saved run file needs the same `--vocab`.

Structured logs need nothing either. A JSON line (slog, pino, zap,
bunyan, structlog) or a logfmt line (`level=error msg="..." err="..."`)
is recognized on its own, and when it is longer than `--width` pare keeps
its start and then the fields that say what went wrong (`err`,
`exception`, `panic`, `stack`, `msg`, ...) instead of cutting them off.
An escaped stack trace inside a field is shown decoded, as its message
and the frame nearest it; pino's and bunyan's numeric levels
(`"level":50`) count as levels. `pare --only T7` prints the whole line.

## How it works

1. **Tee** stdin to a content-hashed run file, in one pass with the steps
   below: pare keeps a few dozen bytes per line in memory, not the
   lines, so time is linear in the input and spent as it arrives, and
   memory stays small ([docs/DESIGN.md](docs/DESIGN.md#performance)).
2. **Segment** lines into records: indented continuations, stack frames and
   compiler gutters join the line above.
3. **Mask** timestamps, IPs, hashes, paths and numbers, then **cluster** the
   masked head lines into templates with Drain (built in, from
   [drain3](https://github.com/logpai/Drain3)).
4. **Score** each line by its structural severity (log level, HTTP status,
   exit code) and vocabulary (error, warning, summary, frame, pass, noise),
   and each template by its head line plus its rarity.
5. **Allocate** the budget: head and tail windows, then one priority queue
   in which template entries and raw lines compete on the same scale, with
   diminishing returns for repeats and a hard limit on size.
6. **End** with a ledger of what was omitted and the run-file path.

Every scoring term survived an ablation; nine that did not were removed.
The details are in [docs/DESIGN.md](docs/DESIGN.md); the experiments
behind each rule, in order, in [docs/HISTORY.md](docs/HISTORY.md).

## Results

Recall is the share of must-survive lines (the failing tests, the error
messages, the verdict) that a tool's output keeps; `bench/run.py`
measures it. Two kinds of number, reported apart:

**The development corpus** (`corpus/dev/`, nine logs, 1.6 MB: real
pytest, go test, cargo, make and npm runs, three seeded synthetic logs
and a synthetic Android logcat), against two other filters written for
agents: [sqz](https://github.com/ojuschugh1/sqz), which compresses
output before it reaches the model and deduplicates across a session's
calls, and [tidyout](https://github.com/sathish-2000/tidyout), which
wraps a command and strips its output with rules keyed on the command's
name, passing unknown commands through. Neither takes a budget. The
misses here were fixed by looking at these cases, so pare's rows are
training scores.

| tool | output tokens | share of input | lines kept |
|---|---|---|---|
| pare 0.2.0, `--budget 2000` | 12,812 | 1.6 % | 99 % |
| pare 0.2.0, `--budget auto` | 12,388 | 1.5 % | 98 % |
| pare 0.2.0, `--budget 600` | 5,017 | 0.6 % | 53 % |
| sqz 1.9.0 | 375,276 | 55 % | 55 % |
| tidyout 0.2.0 | 411,114 | 60 % | 89 % |

Tokens here are bytes / 2.0, the Claude rate, for every tool. A budget
is a smaller summary than it was at the old rate (2.8 bytes per token:
a 2,000 budget was 5,600 bytes and is 4,000), so 600 now keeps about half
of the must-survive lines where it kept 78 %; 1,000 to 2,000 is the
working range. (The sqz and tidyout rows were measured in October 2026
on the eight logs the corpus then had, 683k tokens at this rate, with
pare 0.1.0 at 100 % and 79.5 % for budgets 2,000 and 600 at the old
rate; their token counts are restated at 2.0.)

**Held-out samples**, labelled by datasets rather than by anyone working
on pare: Defects4J bugs (real Java builds, labelled with the failing tests
and their exceptions) and SWE-bench Verified tasks (real Python test
runs, labelled with the tests the fix makes pass). Each sample is drawn
fresh with earlier ones excluded and measured before anyone reads it;
once a miss has been fixed by looking at it, the sample counts as
training data and a new one is drawn. The latest, measured blind:

| sample | cases | pare 0.2.0 (600 / 1,000 / 2,000) | `auto` |
|---|---:|---|---|
| Defects4J, seed 7 | 16 | 86 / 92 / 100 % | 100 % at 125 % of the tokens of 2,000 |
| SWE-bench Verified, seed 5 | 17 | 56 / 70 / 100 % | 100 % at 96 % |

`auto` prints more than a fixed 2,000 on the Defects4J sample because
most of those builds are under 8 KB and pass through whole, which the
agent runs showed to be cheaper than a summary the agent then expands
([docs/DESIGN.md](docs/DESIGN.md#automatic-budget)).

Measured at the Claude rate of 2.0 bytes per token, the default since
29 October 2026; the same samples at the old rate, and the numbers
before each held-out fix, are in the record. Seven Defects4J and six
SWE-bench samples so far; the full record, with every miss and what was
done about it, is in
[corpus/heldout/README.md](corpus/heldout/README.md). The tool-specific
rules (pytest markers, rustc gutters, logcat levels) earn several points
at 600 on SWE-bench and nothing on Defects4J
([docs/DESIGN.md](docs/DESIGN.md#tool-specific-rules)).

## Contributing

pare is written in Rust. `bench/check.sh` runs what CI runs: the Rust
tests and lints, the tests of the Python prototype pare began as, kept
in [reference/](reference/pare.py), the parity check that holds the
binary to it byte for byte over every log in the repository, and
packaging; `bench/run.py` is the benchmark. Setting up, the checks, the
benchmark tools and the repository layout are in
[CONTRIBUTING.md](CONTRIBUTING.md); what comes next is in
[ROADMAP.md](ROADMAP.md).

## License

Licensed under either of [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE),
at your option. The built-in Drain is adapted from
[drain3](https://github.com/logpai/Drain3) (MIT; see
[THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES)). Unless you say otherwise, a
contribution you submit for inclusion is dual-licensed as above, without
additional terms.
