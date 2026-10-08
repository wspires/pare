# pare

A Unix filter that summarizes noisy command output (test runs, builds, logs)
to fit a token budget, for AI agents and humans.

```
pytest -v 2>&1 | pare --budget 2000
```

`proto/` is a Python prototype; `bench/` compares it with
[sqz](https://github.com/ojuschugh1/sqz) and
[tidyout](https://github.com/sathish-2000/tidyout) on the logs in `corpus/`.

## Prototype

The only dependency is [drain3](https://github.com/logpai/Drain3). Pick one:

```
# 1. uv (recommended): the script declares its dependency inline (PEP 723),
#    so uv installs drain3 into a cached environment on first run.
pytest -v 2>&1 | uv run proto/pare.py --budget 2000

# 2. a virtualenv, if you don't use uv
python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt
pytest -v 2>&1 | .venv/bin/python proto/pare.py --budget 2000
```

`proto/pare.py` is executable, so `./proto/pare.py` works once drain3 is
importable by `python3`. A bare `python3 proto/pare.py` with a system or
Homebrew Python will stop with a message pointing at the two options
above rather than a traceback. Python 3.10 or newer is required.

To have it on your PATH as `pare`: `uv tool install --editable .` is not
wired up yet (the prototype has no `pyproject.toml`); for now, a symlink or
`alias pare='uv run ~/dev/pare/proto/pare.py'` does the job.

What it does, in order:

1. **Tee.** Raw stdin is saved to a run file named by content hash
   (`$PARE_RUN_DIR` or `~/.cache/pare/runs/<sha>.log`), so nothing is lost
   and the same input always maps to the same path.
2. **Segment.** Lines become records: a line indented deeper than the
   previous record's head, or shaped like a stack frame / compiler gutter /
   diff hunk, or pytest's `E` / `>` marker, joins that record. A single
   blank line inside an indented block does not break it (Jest, pytest).
   Blank lines belong to no record.
3. **Mask.** Timestamps, dates, UUIDs, IPs, URLs, hex and hashes, versions,
   paths, numbers with units, and digits inside identifiers are replaced by
   `<TS>`, `<IP>`, `<PATH>`, `<NUM>` and so on.
4. **Cluster.** The masked head line of each record goes through
   [drain3](https://github.com/logpai/Drain3) (sim 0.5, depth 4). Clusters
   are renumbered `T1, T2, …` in order of first appearance.
5. **Score.** Each template gets a salience: error vocabulary (+3), warning
   vocabulary (+1.5), summary vocabulary (+1), noise vocabulary (−0.75),
   passing-test markers (−1), rarity `2 / (1 + log2 count)`, a bonus for
   sitting in the last 10 % or first 5 % of the output, and a bonus for
   multi-line records (tracebacks).
6. **Allocate** `--budget` tokens (`bytes / 4`):
   - a head and a tail window (`--head 5`, `--tail 10`, at most 30 % of the
     budget). Lines of templates with 20 or more records, and continuation
     lines of records 20 or more lines long, are skipped inside the windows,
     since the table already stands for them; the first and last lines
     always stay;
   - then one priority queue for template entries and raw lines, on the
     same salience scale. A template entry (count, marker, exemplar head
     line) competes at its salience plus a small bonus, so every template
     is listed once if the budget allows, highest salience first; a raw
     line competes at its own score: its vocabulary, a share of its record
     head's vocabulary, template rarity, position, a bonus for being the
     first occurrence of its template, and a bonus for continuing a record
     whose head is already on screen. So a high-salience exception line
     beats a low-salience singleton template, and a warning template beats
     the sixth frame of a trace. Lines that land in a placed template's
     exemplar record are rendered under it as its body (the traceback, the
     compiler snippet); the rest go to "selected lines";
   - diminishing returns keep repetition out: a line with the same masked
     shape and severity as one already shown is worth less each time, more
     gently when it is a distinct fact (an error-level record head, a source
     location) and fully when it is an exact repeat; a record head whose
     tokens are mostly contained in a shown line is skipped; a very long
     record yields its lines with decaying returns after eight; and lines
     under a salience floor never pad the output. An exemplar the head or
     tail window already prints is referenced, not repeated, and a rare
     line that restates one already on screen is left to the ledger;
   - the per-item allowances are conservative, so a second pass re-offers
     what did not fit against the measured slack, and a final loop sheds
     content until the rendering fits. The budget is a hard limit;
   - an input that already fits the budget is passed through verbatim.
7. **Ledger.** The output ends with how many lines were omitted and from
   which templates, plus the run file path.

`pare --only T3,T7 < runfile` prints the raw lines of those templates;
`pare --except T4 < runfile` prints everything but. Template ids are stable
because the pipeline is deterministic for a given input. `--max-templates N`
lists at most N templates (highest salience first); the rest still show up
in the omitted ledger.

`--min-severity LEVEL` (debug, info, warn, error, fatal) narrows a
selection to records whose head line carries a structural severity at or
above LEVEL, so the twelve 503s hiding in a 4,000-line access-log template
come out with `pare --only T1 --min-severity error < runfile`. On its own it
applies to every template: `pare --min-severity warn < runfile` is "all
the warnings and worse". Records with no structural signal at all (a
pytest `E` line, a bare traceback) are left out, since there is nothing to
compare against the threshold; use `--only` for those.

`--since L<n>` and `--until L<n>` restrict a selection to a line range,
using the numbers the summary prints (the `L` is optional; a negative
value counts from the end, so `--since -200` is the last 200 lines). They
apply after the template and severity filters and work alone too:
`pare --since L3000 --until L3100 < runfile` is that slice of the raw file
with line numbers, and `pare --only T9 --since L3001 --min-severity error`
is the 5xx lines of one template after the outage began.

### Log levels (structural severity)

Before any words are weighed, pare looks for an explicit log level in a
level *form*: a bare uppercase token near the start of the line (`ERROR`,
`WARN`), a bracketed one (`[error]`, `(warn)`), a key-value pair anywhere
(`level=error`, `"level": "warn"`, `severity=CRITICAL`), a prefix with a
colon (`error:`, `Warning:`, rustc's `error[E0308]:`), or the single-letter
forms of logcat (`E/Tag`), glog (`E0921 10:11:12`) and `[I]`. A level word
in the middle of a message (`connection error occurred`) is vocabulary, not
structure, and is not matched here.

Two more structural signals use the same scale. HTTP status codes are read
from request lines (`GET /path 503`, `"GET /path HTTP/1.1" 503`), key-value
pairs (`status=502`, `"status_code": 404`) and phrases (`responded with
504`, `HTTP/1.1 302`): 5xx is error, 4xx is warn, 1xx–3xx is info. Exit
codes are read from `exit code 1`, `exit status 2`, `exited with code 137`,
`exit=0`, `rc=1`, `returncode=-9`: 0 is info, 1–127 is error, 128 and up or
negative (signals, OOM kills) is fatal. A line's severity is the strongest
signal on it, so an access log with no level field still ranks its 5xx
lines first and its 200s last.

When a severity is found it sets the line's base score (fatal +4, error +3,
warn +1.5, info −0.5, debug −1) and decides whether error words count: an
INFO line saying `0 errors` stays info, and an ERROR line is an error in
any language. Per template, the levels of all member records are tallied;
the record with the highest severity becomes the exemplar, and a template
whose level varies shows the histogram (`[ERROR×3 INFO×4,340]`). The
level tokens live in the vocabulary file as `level-fatal` … `level-debug`
classes, so `FEHLER` or `ERRO` can be added with `--vocab`.

### Vocabulary

The salience vocabulary is the one prior in the pipeline that is not learned
from the input, and it is English and developer-tool flavoured. It is
organised as classes with fixed weights:

| class | weight | holds |
|---|---:|---|
| `error` | +3 | error, failed, refused, panic, CamelCase `*Error`, `FAIL`, … |
| `warn` | +1.5 | warning, deprecated, retry, skipped, flaky, … |
| `summary` | +1 | passed, total, finished, `====` rules, … |
| `frame` | +1 | stack frames and `file.ext:line` locations |
| `pass` | −1 | ✓, PASS, ok (ignored when the line also has an error word) |
| `noise` | −0.75 | downloading, compiling, debug, progress bars, … (same) |
| `strong-error` | — | decides whether a record *body* makes its template an error |
| `level-fatal` … `level-debug` | +4 … −1 | plain level tokens for the structural detection above |

`pare --dump-vocab` prints the built-in lists as a file you can edit; pass it
back with `--vocab FILE` or `$PARE_VOCAB`. A section `[error]` adds to the
built-in class, `[error replace]` discards it first. A plain line is a word
or phrase matched whole and case-insensitively; a `/.../` line is a regex
used as written. See `proto/vocab.example` for a German-log example:

```
[error]
fehler
abgebrochen
/(?i)zeitüberschreitung/
[warn]
warnung
langsam
```

Without it, a `FEHLER datenbank verbindung abgebrochen` line is listed only
because it is rare; with it, it is ranked first and marked `E`.

Example (a 4,813-line service log, budget 2000):

```
# pare: 4,813 lines, 535,403 B (~133,851 tok) → ~1,422 tok (budget 2,000) · 14 templates
## head (L1–L5)
     1│ 2026-10-07T09:00:00.000Z INFO  [main] starting inventory-api v2.14.3 (git 9f1c2ab) pid=4182
     ...
## templates (by salience)
T10  ×6 (42 lines) E <TS> ERROR [worker-<NUM>] unhandled exception in handler OrdersHandler.list
     e.g. L3003: 2026-10-07T09:01:01.288Z ERROR [worker-0] unhandled exception in handler OrdersHandler.list
  3004│     psycopg.OperationalError: connection failed: server closed the connection unexpectedly
  3005│         at inventory/db/pool.py:212 in acquire
     ...
T9   ×180 E <TS> ERROR [worker-<NUM>] http: GET <PATH> <NUM> <NUM> rid=<HEX> err="pool exhausted: ..."
T4   ×4,340 . <TS> INFO [worker-<NUM>] http: <*> <PATH>?page=<NUM> <NUM> <NUM> rid=<HEX> ip=<IP>
     ...
## selected lines
     ...
## tail (L4804–L4813)
     ...
## omitted 4,767/4,813 lines (99.0%): T4 ×4,329, T9 ×177, T5 ×169, ...
## raw: /home/me/.cache/pare/runs/054ad7ad6155.log
   expand with: pare --only T<n> < raw   ·   drop noise: pare --except T<n>,T<m> < raw
```

## Benchmark

```
python3 -m venv .venv && .venv/bin/pip install -r proto/requirements.txt -r bench/requirements.txt
# sqz: npm i -g sqz-cli, cargo install sqz-cli, or drop the release binary in .venv/bin
.venv/bin/python bench/run.py [--budget 2000] [--show-misses] [--json out.json] [--pare-args "..."]
```

The runner looks for `sqz` and `tidyout` in `.venv/bin` and then on `PATH`,
and runs pare with `.venv/bin/python` when that exists, else `uv run`.
Missing tools are reported as `n/a` rather than failing the run.

For each `corpus/<name>.log`, `<name>.keep` lists lines that must survive
(a line survives if its stripped text is a substring of the tool's output)
and `<name>.cmd` records the command that produced the log. tidyout only
compresses output of commands it knows, so the runner puts a fake binary
with that name on `PATH` that replays the log; sqz runs as
`sqz compress --no-cache --cmd <name>`. Tokens are `bytes / 4` for every
tool. `raw` is the unfiltered input.

Corpus: real `pytest -v`, `go test -v`, `cargo build`, `make -k` (gcc) and
`npm install --loglevel verbose` runs captured in a container, plus two
seeded synthetic logs (`bench/gen_synthetic.py`): a Jest run with three
failing suites and a 535 KB service log with a database outage.

### budget 2000

| case | tool | in tok | out tok | ratio | recall |
|---|---|---:|---:|---:|---:|
| cargo-build | pare | 945 | 973 | 103.0% | 8/8 |
| cargo-build | sqz | 945 | 763 | 80.7% | 4/8 |
| cargo-build | tidyout | 945 | 697 | 73.8% | 8/8 |
| go-test | pare | 2212 | 731 | 33.0% | 11/11 |
| go-test | sqz | 2212 | 2212 | 100.0% | 11/11 |
| go-test | tidyout | 2212 | 2230 | 100.8% | 11/11 |
| jest-fail | pare | 4396 | 867 | 19.7% | 10/10 |
| jest-fail | sqz | 4396 | 2099 | 47.7% | 2/10 |
| jest-fail | tidyout | 4396 | 4402 | 100.1% | 10/10 |
| make-build | pare | 2318 | 953 | 41.1% | 7/7 |
| make-build | sqz | 2318 | 990 | 42.7% | 1/7 |
| make-build | tidyout | 2318 | 2336 | 100.8% | 7/7 |
| npm-install | pare | 5459 | 518 | 9.5% | 2/2 |
| npm-install | sqz | 5459 | 4902 | 89.8% | 2/2 |
| npm-install | tidyout | 5459 | 5477 | 100.3% | 2/2 |
| pytest-fail | pare | 4762 | 1667 | 35.0% | 11/11 |
| pytest-fail | sqz | 4762 | 401 | 8.4% | 8/11 |
| pytest-fail | tidyout | 4762 | 1483 | 31.1% | 11/11 |
| server-outage | pare | 133851 | 1056 | 0.8% | 9/9 |
| server-outage | sqz | 133851 | 63390 | 47.4% | 5/9 |
| server-outage | tidyout | 133851 | 1388 | 1.0% | 1/9 |

| tool | cases | in tok | out tok | ratio | recall | min recall |
|---|---:|---:|---:|---:|---:|---:|
| raw | 7 | 153943 | 153943 | 100.0% | 100% | 100% |
| pare | 7 | 153943 | 6765 | 4.4% | 100% | 100% |
| sqz | 7 | 153943 | 74755 | 48.6% | 57% | 14% |
| tidyout | 7 | 153943 | 18013 | 11.7% | 86% | 11% |

### budget 600

| tool | cases | in tok | out tok | ratio | recall | min recall |
|---|---:|---:|---:|---:|---:|---:|
| raw | 7 | 153943 | 153943 | 100.0% | 100% | 100% |
| pare | 7 | 153943 | 4027 | 2.6% | 100% | 100% |
| sqz | 7 | 153943 | 74755 | 48.6% | 57% | 14% |
| tidyout | 7 | 153943 | 18013 | 11.7% | 86% | 11% |

At 600 every corpus file keeps every must-survive line, and each output
lands within a few tokens of the budget (591 to 600) rather than leaving
slack unused.

Notes on reading this:

- sqz and tidyout have no budget knob, so their rows are the same at both
  budgets. tidyout is a pass-through for commands it has no rule for
  (go test, make, the service log) and strips well for the ones it knows
  (pytest, cargo); sqz is a general compressor that drops error lines as
  readily as noise.
- pare keeps every must-survive line at budget 2000. At 600 (2,400 bytes)
  it used to drop the per-failure `E` lines of pytest; four things fixed
  that without touching the keep lists. The tail window was repeating lines
  the template table had already listed; pytest's `E` lines began at column
  0 and so were treated as records of their own instead of part of the
  failure block, and their marker was counted as both a structural level
  and an error word; progress lines (`test_x FAILED [ 95%]`) that restate
  the summary lines were listed as templates of their own; and the
  `total()` in `E + where … = total()` earned a summary bonus inside a
  block. The go and make misses had the same flavour: goroutine-frame
  singletons already printed by the tail window still got reference lines,
  a later copy of a gcc warning beat the first one only because it sat in
  the last 10 % of the file, and the third `FAIL <package>` line fell under
  the salience floor because two same-shape lines were already shown. Now a
  singleton whose whole record a window prints is not listed, the first
  occurrence of a template gets the same bonus as a near-the-end position,
  and error-level record heads (each a distinct fact) see gentler
  diminishing returns than repeated chatter. The last two misses, both in
  the service log, exposed that a fixed split between the template table
  and the fill phase cannot be right at every budget: any share that lists
  the slow-cache warning starves the pytest exception lines. Replacing the
  split with one priority queue (see step 6 above) took the 600 recall from
  79 % to 100 %. `--max-templates N` now only costs recall at 600:

  | `--max-templates` | out tok | recall | min recall |
  |---|---:|---:|---:|
  | 0 (all that fit) | 4,027 | 100% | 100% |
  | 6 | 4,025 | 97% | 86% |
- The cargo log (945 tokens) fits the budget, so pare passes it through with
  a two-line header; it never makes small inputs bigger than a fixed overhead.

## Tests

```
.venv/bin/pip install pytest && .venv/bin/python -m pytest proto/
```

Covers log-level, HTTP-status and exit-code detection forms and their
rejections, vocabulary parsing and round-tripping, the hard budget limit at
several sizes on every corpus file, determinism, pass-through of small
inputs, `--only`/`--except`, `--min-severity`, `--since`/`--until`, and the
run file.

## Layout

```
proto/pare.py          the prototype (drain3 is its only dependency)
proto/test_pare.py     pytest suite
proto/vocab.example    example vocabulary extension (German)
bench/run.py           benchmark runner
bench/gen_synthetic.py generator for the seeded synthetic corpus files
corpus/*.log|.keep|.cmd
```
