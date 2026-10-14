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

- [ ] **License.** None is declared.
- [ ] **Name.** Check that `pare` is free on PyPI and as a crate name.
- [ ] **Version.** What 0.2 means, and the compatibility promise for
  template ids (`--only T7 < runfile` must keep working).

## 5. Native port

- [ ] **Rust implementation** of the single pass and the allocator. The
  design maps directly: chunked reads, typed per-line arrays, text read
  back from the run file by offset.
- [ ] **Differential tests.** The randomized follow-versus-batch test and
  the batch equivalence sweep carry over unchanged, run against the Python
  prototype as the oracle.
- [ ] **Template id compatibility.** Decide whether the port must
  reproduce drain3 0.9.11's clustering exactly, or whether new ids are a
  breaking version.

## 6. Deferred

- [ ] **Rolling follow mode.** A whole-run summary rewritten in place, for
  agents that poll a file rather than read a stream. Design in
  `docs/DESIGN.md`. Build it only if stage 2 shows the need.

## Done

The prototype, the benchmark against sqz and tidyout, the dev and
generated corpora, real-tokenizer calibration, the single pass with
bounded memory, follow mode with record-preserving cuts, and the
differential tests. History is in `docs/DESIGN.md` and the git log.
