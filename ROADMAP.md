# Roadmap

pare is built; what it lacks is evidence. Everything measured so far is a
training score on logs that were used to tune it. The stages below are in
order, and each one gates the next: no tuning before held-out numbers exist,
and no port before the numbers say the design is worth porting.

Each item says who can do it. **Repo** means it is code or docs that can be
done in this repository. **Maintainer** means it needs real sessions, real
credentials or a decision that is not the code's to make.

## 1. Validate on real data

The gate for everything after it.

- [ ] **Held-out corpus.** 15 to 20 logs from real agent sessions, keep
  lists written before pare sees them, reported separately from the dev
  corpus. Protocol: `corpus-heldout/README.md`. Fastest source: run
  `bench/harvest.py scan` over your existing Claude Code transcripts,
  review the candidates, promote the good ones. *Maintainer.*
- [x] **SWE-bench cases.** Failing test runs reproduced from SWE-bench
  tasks, labelled by the tests the dataset records as failing
  (FAIL_TO_PASS). Harness: `bench/swebench_cases.py`. First run on the
  maintainer's machine, seed 0: 16 of 20 tasks became cases (three tasks'
  failing tests were not found in the output, one output was under 40
  lines). **pare kept 60 % of the failing tests at 2,000 tokens, min 0 %**;
  tidyout kept all of them at 96 % of the input size. Committed in
  `corpus-heldout/swebench/`. After the failing-test fix (stage 3): 82.5 %
  at 2,000. *Done.*
- [x] **Defects4J cases.** Failing Java test runs rebuilt from Defects4J
  bugs (the fixed commit from upstream, its compile-error fixes, the
  revert patch), run with the project's Maven build in a JDK 8 container,
  labelled by Defects4J's trigger tests and the exception each raises.
  Harness: `bench/defects4j_cases.py`. Seed 0, one bug per project: 11 of
  14 became cases, committed in `corpus-heldout/defects4j/` (JacksonXml-1
  needs unpublished snapshot dependencies, Lang-59 an antrun feature
  removed from today's plugin, JxPath-5 has no Maven build). **pare kept
  94 % at 2,000 tokens, but most of these logs fit and pass through
  untouched; at 600, where every one is summarised, it kept 29 %.** After
  the failing-test fix: 48 % at 600, 97 % at 2,000. Ant projects
  (Closure, Chart) and Mockito (Gradle) are not covered. *Done for
  Maven.*
- [x] **Fresh held-out sample.** SWE-bench seed 1 (17 cases,
  `corpus-heldout/swebench-2/`) and Defects4J seed 2 (16 cases,
  `corpus-heldout/defects4j-2/`), drawn with `--exclude` after the
  failing-test fix and measured before anyone looked at them. pare before
  → after the fix: SWE-bench 63 → 89 % at 2,000 and 51 → 60 % at 600;
  Defects4J 30 → 51 % at 600 and 63 → 74 % at 1,000 (98 % at 2,000,
  where 10 of 16 logs pass through). The fix holds on unseen data. Per
  case and the remaining misses: `corpus-heldout/README.md`. *Done.*
- [x] **Third fresh samples.** SWE-bench seed 2 (16 cases,
  `corpus-heldout/swebench-3/`) and Defects4J seed 3 (18 bugs,
  `corpus-heldout/defects4j-3/`), drawn after every earlier set was
  excluded. With all fixes so far: SWE-bench 86 / 91 / 100 % and
  Defects4J 67 / 94 / 98 % at 600 / 1,000 / 2,000; before any held-out
  fix, 77 / 86 / 95 % and 33 / 55 / 86 %. *Done; both are spent by the
  next change measured on them.*
- [x] **Fourth fresh sample.** Defects4J seed 4 (18 bugs,
  `corpus-heldout/defects4j-4/`), drawn after the tail-window change with
  every earlier Defects4J set excluded, measured before reading. Current
  pare 92 / 100 / 100 % at 600 / 1,000 / 2,000; before any held-out fix,
  41 / 79 / 97 %. The three misses at 600 are JUnit 3 assertion messages
  whose tests are shown. *Done; spent by the next change measured on it.*
- [x] **JUnit 3 assertion messages.** Seed 4's three misses at 600.
  A stack frame's method names (`Assert.fail`, `assertEquals`) no longer
  count as error words, and a warning-level template's rarity counts three
  quarters, so a one-off POM warning no longer outranks two tests failing
  the same way. Seed 4 92 → 100 % at 600; seed 3 82 → 88 %; seed 2 80 →
  91 %; seed 0 84 → 90 %; android-logcat +1 line; no case worse. Seed 4
  is spent. Details: `docs/DESIGN.md`, "JUnit 3 assertion messages".
  *Done.*
- [x] **Fifth fresh sample.** Defects4J seed 5 (17 bugs,
  `corpus-heldout/defects4j-5/`), drawn with every earlier set excluded
  and measured before reading: 87.0 / 91.3 / 100 % at 600 / 1,000 /
  2,000 (45.7 / 67.4 / 95.7 % before any held-out fix). The JUnit 3 rules
  hold on data they were not chosen on: 80.4 → 87.0 % at 600, no case
  worse. Left: Gson-7's messages that differ in one word, Csv-3's long
  assertion message. *Done; spent by the next change measured on it.*
- [x] **SWE-bench seed 3.** 15 tasks (`corpus-heldout/swebench-4/`),
  drawn after the JUnit 3 change with every earlier SWE-bench set
  excluded, measured before reading: 94.1 / 100 / 100 % at 600 / 1,000 /
  2,000 (76.5 / 100 / 100 % before any held-out fix). The tail-window
  and JUnit 3 changes change no case; nothing got worse. One miss:
  pydata__xarray-4094's only failing test at 600. *Done.*
- [x] **Messages that differ in one word.** Gson-7 (Defects4J seed 5).
  A template seen twice lists both lines instead of its pattern and one
  example; a line is not clipped when the clip marker saves nothing; the
  Defects4J labeller accepts Surefire 3's `Cls#testX`. Seed 5 (new
  labels) 91.3 / 93.5 → 93.5 / 97.8 % at 600 / 1,000; maven-test +1 at
  1,000; no case worse anywhere. Seed 5 is spent. *Done.*
- [x] **Sixth fresh Defects4J sample.** Seed 6, every earlier set
  excluded, to measure the pair and clip rules on data they were not
  chosen on. 19 bugs (`corpus-heldout/defects4j-6/`), measured before
  reading: 92.5 / 100 / 100 % at 600 / 1,000 / 2,000, and 100 % with
  `--budget auto` at 51 % of the input against 72 % at 2,000 (60.0 /
  90.0 / 97.5 % before any held-out fix). The pair rule earns one line
  (Gson-17 at 1,000), the clip rule none. Misses at 600: failures
  unrelated to the bug (locale tests) crowding Gson-17's list, and
  Gson-12's bare exception. *Done; spent by the next change measured on
  it.*
- [x] **SWE-bench seed 4.** 18 tasks (`corpus-heldout/swebench-5/`),
  drawn after 0.2.0 with every earlier SWE-bench set excluded, measured
  before reading: 90.0 / 96.7 / 96.7 % at 600 / 1,000 / 2,000, and 96.7 %
  with `--budget auto` at 77 % of the tokens of 2,000 (70.0 / 80.0 /
  83.3 % before any held-out fix). One task, pytest-dev__pytest-8399,
  loses its only failing test at every budget: the tail window skips its
  `FAILED` line because the inner pytest runs in pytest's own suite made
  that shape common. *Done; spent by the next change measured on it.*
- [x] **Window skip against a common verdict.** The windows skip lines
  of templates seen five or more times (for Maven's chatter); on
  pytest-dev__pytest-8399 that hid the one real `FAILED` line. Keeping
  every error line in the tail lost 8 lines elsewhere (an emulator's
  routine errors recur to the end); keeping an error line that is the
  only one of its template near the end gains pytest-8399 at every budget
  and loses one line (ci-pipeline at 1,000, a budget edge). Shipped the
  second (`docs/DESIGN.md`, "The verdict that repeats a shape"). SWE-bench
  seed 4 is spent; the next fresh samples are its test. *Done.*
- [x] **Seventh fresh Defects4J sample.** Seed 7, 16 bugs
  (`corpus-heldout/defects4j-7/`), drawn after the verdict rule and
  measured before reading: 91.9 / 100 / 100 % at 600 / 1,000 / 2,000, and
  100 % with `--budget auto` at 69 % of the tokens of 2,000 (54.1 / 78.4
  / 89.2 % before any held-out fix). The verdict rule changes no output
  on it. One miss: Math-66's assertion messages at 600. *Done; spent by
  the next change measured on it.*
- [x] **SWE-bench seed 5.** 17 tasks (`corpus-heldout/swebench-6/`),
  drawn after the verdict rule and measured before reading: 66.7 / 70.4 /
  100 % at 600 / 1,000 / 2,000, and 100 % with `--budget auto` at 79 % of
  the tokens of 2,000 (66.7 / 70.4 / 70.4 % before any held-out fix).
  16 of 17 tasks keep every test at every budget; all misses are
  pytest-dev__pytest-7205's. The verdict rule changes no output on it.
  *Done; spent by the next change measured on it.*
- [ ] **Inner runs in pytest's own suite.** Both pytest misses on fresh
  data (pytest-8399, pytest-7205) come from the inner pytest sessions
  that pytester prints: their one-off `FAILED`/`ERROR` lines rank as
  rare errors and crowd out the real failures. A candidate: recognize an
  inner session (a `test session starts` banner after the first, inside
  a captured-output section) and score its lines as body, not verdicts.
  Narrow (pytest's own repository, and plugins tested with pytester);
  decide on more samples first.
- [ ] **Claude token rate.** `bench/tokens.py --anthropic claude-opus-5-5`
  on a machine with credentials. If the rate differs much from the 2.8
  bytes per token measured on cl100k and o200k, change the default.
  *Maintainer.*
- [ ] **Agent-success eval.** A few real tasks (fix a failing test, fix a
  broken build, find an outage's cause) run with raw output, pare, sqz and
  tidyout as the tool result; report task success and tokens side by side.
  Design: `corpus-heldout/README.md`. *Maintainer, with a harness from the
  repo.*

## 2. Make collection a side effect of use

The blocker for stage 1 is friction, so this comes first in practice.

- [x] **Command mode.** `pare -- <cmd> <args>` runs the command, summarises
  its output, keeps its exit code, and records the command next to the run
  file. Piping (`cmd | pare`) loses both. *Repo.*
- [x] **`bench/collect.py`.** Lists recent run files with their commands,
  copies one into `corpus-heldout/` with its `.cmd`, scrubs the home and
  working directories, and creates an empty keep list next to the raw log,
  without ever showing pare's summary. Records a manifest line with the
  pare version and commit at collection. *Repo.*
- [x] **Agent integration recipe.** `integrations/README.md`: an
  instruction for the agent's instructions file, and a Claude Code
  PreToolUse hook that rewrites test, build and install commands. *Repo.*
- [x] **`bench/harvest.py`.** Mines existing Claude Code transcripts
  (`~/.claude/projects`) for long test, build and install outputs,
  including the full text of outputs Claude Code saved because they were
  too large for context. Scrubs home paths, emails, your login and git
  identity and credential-shaped strings; proposes keep lines only from
  what the agent did next (never from pare's scoring); stages candidates
  in a git-ignored inbox; `promote` refuses a case without a reviewed keep
  list or with credentials left in it. *Repo.*
- [x] **Try the hook in a real Claude Code session.** A rewrite sent
  without a permission decision is applied, and the hook took effect as
  soon as `.claude/settings.json` was written. This repository now
  registers it for itself. Still to see: a narrow allow rule against the
  rewritten form. *Done.*
- [x] **Measure what it saves.** Command mode records the command's
  output size and pare's; `pare --stats` totals them (an upper bound:
  see `integrations/README.md`). *Done.*
- [ ] **Use it for a while.** Run files accumulate from normal work; then
  `bench/collect.py` turns the interesting ones into held-out cases.
  *Maintainer.*

## 3. Fix only what the held-out set shows

Candidates already recorded in `docs/DESIGN.md`. None changes before
held-out evidence asks for it.

- [ ] **Template table exhausting the budget.** Seen on the cold-cache
  Maven build and the Terraform plan: many low-value entries crowd out the
  error lines. Candidate: cap the table when it alone would fill the budget.
- [ ] **Wide single-line errors.** A 230-character kubectl error loses its
  tail to `--width 200`.
- [x] **Failing tests collapse into one template entry.** Found on both
  held-out sources: several failing tests (`FAILED path::test - ...`,
  Django's `test_x (module.Class)`, Surefire's `testX(pkg.Class)`) share
  one shape, so pare showed one exemplar and a count. Fixed by treating an
  error line in a template of mostly distinct shapes as a list item rather
  than a repeat, and by an `advice` vocabulary class for build-tool
  closing advice (Maven's `re-run with -e`, `[Help 1]`), which took the
  600-token budget on Maven runs. SWE-bench 60 → 82.5 % at 2,000;
  Defects4J 29 → 48 % at 600; a separate Defects4J dev sample 43 → 68 %
  at 600; dev corpus 79.5 → 78 % at 600 (one pytest line), unchanged at
  2,000. Details in `docs/DESIGN.md`, "First change from held-out
  evidence". *Done.*
- [x] **List many failing tests compactly.** A list of failures is one
  header with the shared prefix (`FAILED test_requests.py::RequestsTestCase::{…}`)
  and one short item per test, about half the size (requests-2317: 32
  tests in about 540 tokens, about 1,000 as lines). No dev case got worse, four got better
  (seaborn-3069 0 → 2 of 2 at 2,000); SWE-bench seed 1 went from 94 to
  100 % at 2,000 (requests-2317 6 → 8 of 8). The bench expands the form
  before matching; `--strict` matches literal text and counts compacted
  names as missed (SWE-bench seed 1 at 2,000: 57 % strict). *Done.*
- [x] **Which of many failures matter.** When most failing tests fail one
  way (a broken environment), the few that fail otherwise are the news. A
  listed failure's section is found by name and its way of failing is the
  shape of the section's last top-level error line; a failure whose way
  is shared by at most a tenth of a list of 5 or more gets +2 salience,
  and is listed first under a header that says so. Dev: seaborn-3069 and
  requests-1142 now name their wanted tests at 600 and 1,000 (SWE-bench
  seed 0: 47.5 → 57.5 % at 600, 67.5 → 75 % at 1,000), no losses. Fresh:
  requests-2317 0 → 2 of 8 at 600 and 1,000. The other six fail with the
  forty tests the missing network breaks, so nothing in the output can
  single them out; at 2,000 the list form shows all 40. *Done.*
- [x] **Names that differ only in numbers.** Parametrized test ids
  (`test_rolling_iter[numpy-3-True-1]`, `[numpy-3-True-2]`) masked to one
  shape, so they were repeats, not a list. A name's bracketed parameters
  (pytest's `test_x[...]`, JUnit 4's `testX[2]`) now stay as written in
  the shape key, and a number inside them is news to the near-duplicate
  guard. No change on any dev set; on SWE-bench seed 1, xarray-6744 went
  from 1 to 3 of 3 at 1,000 and 2,000 (at 600 the budget goes to `XPASS`
  lines first). Left alone on purpose: assertion messages that differ
  only in numbers (JacksonCore-10's `expected:<1024> but was:<2048>`).
  Their values are on screen in Surefire's summary line, and making a
  number in a message news would undo the repeat rule for every
  `timeout after 3012 ms`. *Done.*
- [x] **Maven chatter at 600.** Level-only lines (`[ERROR]`) and build
  advice (`Please refer to …`, `[Help 1]`, `See dump files`) now score
  under the floor whatever their level; a failing test's line is no
  longer dropped as a restatement of its class summary; JUnit's
  `ComparisonFailure` counts as an error. Fresh Defects4J seed 3: 47 →
  67 % at 600, 71 → 94 % at 1,000, no case worse. Build warnings stay.
  Details: `docs/DESIGN.md`, "Maven chatter". *Done.*
- [x] **Keep lines with alternative forms.** A keep line may list
  equivalent forms separated by ` || `; it counts when any form appears,
  and a form must not run on into a longer name (`…Resolve1` is not shown
  by `…Resolve12`). The Defects4J harness labels each test with every id
  form the log shows and each exception with its message and Surefire's
  `Cls.testX » Abbrev` summary form, taken from the log and the dataset,
  never from pare's output; `defects4j_cases.py relabel DIR...` rewrote
  the 64 existing cases. Both scores in `corpus-heldout/README.md`
  ("Labels"): Defects4J seed 3 at 600, 67 → 76.5 %. *Done.*
- [x] **Re-ablate removed terms.** The four position terms removed in
  95c77c6 (end zone and start zone for templates, end zone for lines,
  first occurrence) are back in the code at weight 0, and `bench/ablate.py`
  switches each on at its old weight ("with ..." rows) per set. On nine
  sets none earns its place: first occurrence costs up to 14 points
  (SWE-bench seed 1 at 2,000); the end-zone line bonus helps SWE-bench at
  1,000 (+7.5, +5.7) and hurts Defects4J there (−2.3, −3.9). All stay off.
  Table: `docs/DESIGN.md`, "Scoring model". *Done.*
- [x] **Tool-specific rules.** Each rule now has its own switch and
  ablation row. On nine sets: the pytest markers (up to 14 points on
  held-out SWE-bench) and the blank-line-in-a-block rule (4 to 17 points
  on every Python and dev set) earn their place; rustc/gcc shows only on
  the dev corpus; logcat/glog levels change nothing because no set has
  that output (kept, untested); Defects4J needs none of them. All kept.
  Table: `docs/DESIGN.md`, "Tool-specific rules". *Done.*
- [x] **An Android logcat case.** `corpus/android-logcat`, seeded and
  synthetic (`bench/gen_synthetic.py`), in Android's threadtime format: it
  found that pare missed every level on lines whose tag is shorter than
  eight characters (logcat pads it before the colon; 663 of 2,510 lines),
  now fixed. With the logcat rule: 50 / 75 / 87.5 % at 600 / 1,000 /
  2,000 (87.5 % at 1,000 since the entry fix below); without it, 62.5 %
  at 1,000. *Done; a real capture and a glog
  log are still wanted (Maintainer).*
- [x] **logcat stack traces.** A logcat line is now judged by its
  message inside its entry (one header repeated): a new header starts a
  record, and the exception heads its record with its frames under it.
  The case goes from 75 to 87.5 % at 1,000 (50 % at 600 and 87.5 % at
  2,000 either way), and no other log's output changed. *Done.*
- [x] **logcat process death.** The death of a pid that logged a
  `FATAL EXCEPTION` is now an error (other deaths, routine kills of cached
  apps, stay INFO). The logcat case is 100 % at 1,000 and 2,000. *Done;
  a real capture should confirm the line forms.*

## 4. Release

Decisions, not code. *Maintainer.*

- [x] **License.** MIT OR Apache-2.0. *Done.*
- [x] **Name.** `pare` is taken on PyPI and crates.io; the package is
  `pare-cli` there, and the command is `pare` everywhere. *Done.*
- [ ] **Version.** What 0.2 means, and the compatibility promise for
  template ids (`--only T7 < runfile` must keep working).

## 5. Rust port

Decided: pare moves to Rust for a single-binary install, speed on large
logs (an estimated 15 to 50 times the Python version), and as a way to
learn Rust from C++. The Python version is the reference until the Rust
one matches it byte for byte, then it is kept as `reference/pare.py`,
unmaintained. The rules are frozen during the port: no tuning until the
two agree, so the target does not move. Template ids stay identical
(Drain as drain3 0.9.11 clusters), which is what makes the comparison
meaningful.

The code is written to be read: `//!` module docs that name the Python
counterpart, `///` docs with compiled examples on public items, and
`// Rust note:` comments where a choice is Rust-specific, written for a
C++ reader. `docs/ARCHITECTURE.md` (crate layout, data flow, memory
model, module map) and `docs/RUST.md` (Rust for C++ developers, through
pare) come with the crate. `#![forbid(unsafe_code)]`, rustfmt and clippy
with warnings as errors, few dependencies, each one justified.

- [x] **0. Comparison tooling.** `bench/parity.py` runs a reference and a
  candidate over every corpus log and 13 generated edge cases (empty
  input, CRLF, ANSI, invalid UTF-8, NUL bytes, a 20,000-character line,
  letters that case-fold to ASCII, ...) under 13 flag sets (budgets,
  width, token rate, template cap, windows, every selection flag, follow
  mode) and compares outputs and exit codes byte for byte. With `--dump`
  it also compares the pass's per-line and per-template results
  (`$PARE_DUMP`, `write_dump` in `reference/pare.py`) and reports the first
  row that differs, so a divergence is found at the line where it starts.
  *Done.*
- [x] **1. Crate skeleton.** A library crate with a thin binary
  (`src/`): every Python option with its default (`clap`), the checks and
  messages of Python's `main()`, chunked reading and line splitting, the
  spool and run file named by SHA-256, and pass-through of input that
  fits the budget, cleaned as `clean_line()` cleans it. What is not ported
  exits 70 and `bench/parity.py --skip-unported` skips it: 170 of 2,496
  runs compare, all identical, including every generated edge case that
  fits. Unit, doc and end-to-end tests (`tests/cli.rs`); CI runs fmt,
  clippy, tests, docs, the Python suite and parity; `release.yml` builds
  static Linux (musl), macOS (both) and Windows archives on version tags.
  `docs/ARCHITECTURE.md` and `docs/RUST.md` started. Not ported with it:
  run-file pruning and the command-mode `.json` record (stage 5). *Done.*
- [x] **2. Segmentation, masking and Drain.** Lines into records (indent,
  continuation shapes, logcat entries, one blank inside a block, the
  400-line cap), the twelve masks, `drain_message` and Drain, with the
  per-line and per-template columns and `$PARE_DUMP` in Python's format
  (`?` for severity and scores). Parity with `--dump`: 0 of 2,522 runs
  differ, and every comparable field of all 1,351 batch dumps matches
  (records, depth, templates and their text, rendered sizes). Python's
  `\w`, `\d`, `\s` and `isdigit()` differ from Rust's on about 8,000
  characters, so their tables are generated from Python 3.13
  (`bench/gen_pyunicode.py`, `src/pyunicode.rs`, checked in CI). Masks
  need lookaround: the `regex` crate scans for candidates and
  `fancy-regex` checks Python's pattern only there (14 times faster than
  `fancy-regex` alone; the pass over 30 MB takes 2.1 s, Python's full
  run 28.5 s). A golden file of Python's `mask()` on 4,045 record heads
  (`bench/gen_fixtures.py`). Found in the reference: a line indented 256
  columns past its head crashed it (depth was a signed byte); fixed, with
  a test. *Done.*
- [x] **3. Vocabulary, severity, scoring.** The whole per-line dump matches.
  Severity (level tokens, HTTP statuses, exit codes, logcat crashes),
  the vocabulary classes, `vocab_score`, inheritance, depth and location
  adjustments, template salience and markers; `--vocab`, `$PARE_VOCAB`
  and `--dump-vocab`. Parity with `--dump`: 0 of 2,730 runs differ (a
  14th flag set uses `reference/vocab.example`), and every field of all
  1,552 batch dumps matches, scores to the bit. The two-engine scan of
  stage 2 became `PyRegex`, for every Python pattern; Python's `(?i)`
  needed two more corrections (`İ`/`ı`, and classes that Rust's `(?i)`
  would widen). Golden files of Python's severity, classes and scores on
  6,032 lines and of `--dump-vocab`. The pass over 30 MB takes 5.4 s
  (Python: 28.5 s); start-up 50 ms, the `fancy-regex` halves compiled on
  first use. *Done.*
- [x] **4. Summary and allocation.** Full output identical on every run.
  Head and tail windows, the one queue of template entries and raw lines,
  diminishing returns, near duplicates, pairs, lists of failures and the
  ones that fail unlike the rest, the ledger, the exact fit and shedding.
  Parity: 0 of 2,730 runs differ, and every batch run (1,560) now
  compares its output, byte for byte. `summarize()` and its closures
  became a struct with one method per closure. Whole summaries of five
  corpus logs are golden files. The 30 MB log: 5.8 s for the whole run
  (Python: 28.5 to 41 s on the same machine), 52 MB. Matched, not fixed:
  at `--budget 0` the Python version's last-resort cut
  (`[: budget_bytes - 1]`) keeps all but one byte, so the output overshoots
  the budget; the port does the same until the reference changes.
  *Done.*
- [x] **5. The rest.** Selection flags, command mode, follow mode, prune,
  stats: the port is complete. Parity: 0 of 2,730 runs differ and none
  is skipped (CI no longer passes `--skip-unported`; status 70 is gone).
  Follow mode needed the pass to cut windows: line numbers and spool
  offsets continue across them, template ids are global, and a record
  open at a cut is carried, so windows segment exactly as a batch pass
  does (a unit test checks it). Python's `select()` with a timeout became
  a reader thread and a bounded channel. Command mode merges stdout and
  stderr through `std::io::pipe` (Rust 1.87), ignores SIGINT and passes
  SIGTERM/SIGHUP on (`signal-hook`, `nix`, no `unsafe`); its `.json`
  record is written as Python's `json.dump` writes it (`pyjson`).
  Command mode, `--prune`, `--stats` and timed flushes were compared with
  the Python version by hand and have integration tests. Differences
  kept: a follow run file is named in UTC (Python: local time; std has
  no time zones), and a bad `--vocab` regex is reported in the regex
  engine's words. *Done.*
- [x] **6. Distribution.** Licensed MIT OR Apache-2.0 (drain3's MIT
  notice in `THIRD-PARTY-NOTICES`). The package is `pare-cli` (`pare` is
  taken on crates.io and PyPI); the command is `pare`. One tag push
  publishes four channels (`release.yml`): a GitHub release (Linux x86-64
  and ARM static builds, macOS ARM and Intel, Windows, `SHA256SUMS`), a
  Homebrew formula tested with Homebrew and pushed to `wspires/homebrew-tap`,
  PyPI wheels with the binary (maturin, trusted publishing) and the crate
  (with `cargo binstall` metadata). `install.sh` installs a release and
  checks its checksum. CI's "Packaging" job checks the crate, the wheel,
  the install script and the formula on every pull request. The Python
  version moved to `reference/`, no longer packaged. RELEASING.md has the
  checklist and the one-time setup (the tap repository, two secrets,
  PyPI's trusted publisher), which only the owner can do. Version 0.2.0.
  *Done.*
- [ ] **First release.** The one-time setup in RELEASING.md, then tag
  `v0.2.0`.
- [x] **After the first change in behaviour.** The reference is frozen;
  parity holds the Rust version to it. When pare's output changes on
  purpose (the `--budget 0` overshoot is the first candidate), parity will
  report it. Then either keep parity for the rest by teaching
  `bench/parity.py` the intended difference, or retire the parity job and
  regenerate the golden files from the Rust version, which becomes its own
  reference. The first change was the tail window's verdict rule. Parity
  is kept: `PARE_REFERENCE_RULES=1` turns off the Rust-only rules that
  change default output (structured lines, the verdict rule), parity and
  the golden-file test run with it, and the rules have tests of their
  own. *Done.* Retire parity instead once the Rust-only rules outnumber
  what it still checks.
- [ ] **Later, if needed.** Per-line checks on worker threads: they depend
  only on the line's text, so the pass is written to keep them a pure
  function from the start.

## 6. After the first release

Ideas from reviewing the release, in rough order of value for effort.
Each changes nothing unless it is used, except the budget experiment,
which changes output and so comes after the parity decision above.

- [x] **`$PARE_BUDGET`.** The budget when `--budget` is not given, so an
  agent harness sets it once. *Done.*
- [x] **Configurable continuation rules.** A `[continuation]` class of
  regexes in the vocabulary file: lines matching one continue the record
  above, for a stack-trace or diagnostic format the built-in rules do not
  know. The built-in rules became the class's default entries (so
  `--dump-vocab` lists them and `[continuation replace]` can drop one);
  parity is unchanged. *Done.*
- [x] **Configurable masks.** A `[mask NAME]` section: matches become
  `<NAME>` before clustering, for site-specific ids (order numbers,
  tenant ids, build hashes) that should not split one kind of line into
  many templates. They run *before* the built-in masks, not after as
  first planned: after, `NUM` would already have turned `ORD-1234` into
  `ORD-<NUM>`, and a user's pattern should see the line as written.
  *Done.*
- [x] **A budget that stops when the news is shown.** Today the budget is
  a ceiling that a large log always fills. The allocator already ranks
  every entry by salience; it could stop once nothing above a salience
  threshold is left, so a log with one error gets a short summary and one
  with many gets more, still capped by `--budget`. Changes output: tune
  the threshold with `bench/run.py` (recall and tokens at each budget) and
  ship it behind a flag (`--budget auto`?) until it earns being the
  default. A simpler variant scales the budget with the input's size
  between a floor and a ceiling, at the cost of less predictable output
  sizes.
  Shipped as `--budget auto` / `auto:N`: a template floor of 3.0, pass-through
  up to 1,000 tokens and windows sized for 2,000. 97.4 % recall at a mean
  1,204 tokens, against 98.8 % at 1,552 for `--budget 2000`
  (`docs/DESIGN.md`, "Automatic budget"). *Done.* The default since four
  fresh held-out samples confirmed it: the first one, Defects4J seed 6, keeps
  100 % with `auto` at 71 % of the tokens of `--budget 2000`, and
  SWE-bench seed 4 the same recall as 2,000 (96.7 %) at 77 % of its
  tokens.
- [x] **Inferring a log's format.** Detect formats from the first few
  hundred lines (logfmt, JSON lines, a known tool) and switch rules on
  accordingly. Possible, but output would then depend on the detection;
  Drain already learns most of what format detection would give. Only if
  the held-out set shows formats the rules miss. No corpus had a JSON or
  logfmt log, so four were generated from real loggers (slog JSON and
  text, pino, structlog): pare kept 8 of 18 must-survive lines, every miss
  a cause cut off by the width clip. Built per line instead of per file:
  structured lines are recognized on their own and clipped keeping their
  telling fields, escaped traces show as message and frame, and pino's
  numeric levels count. 18 of 18 after, everything else unchanged
  (`docs/DESIGN.md`, "Structured lines"). *Done.* Still owed: real
  structured logs in a held-out sample.

## 7. Deferred

- [ ] **Rolling follow mode.** A whole-run summary rewritten in place, for
  agents that poll a file rather than read a stream. Design in
  `docs/DESIGN.md`. Build it only if stage 2 shows the need.

## Done

The prototype, the benchmark against sqz and tidyout, the dev and
generated corpora, real-tokenizer calibration, the single pass with
bounded memory, follow mode with record-preserving cuts, and the
differential tests. History is in `docs/DESIGN.md` and the git log.
