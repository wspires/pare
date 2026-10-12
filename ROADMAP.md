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
- [ ] **Try the hook in a real Claude Code session.** Unverified here:
  whether a rewrite sent without a permission decision is applied, and
  how it interacts with your permission rules. *Maintainer.*
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
- [ ] **Which of many failures matter, and listing them compactly.**
  When a run has more failing tests than the budget holds, pare cannot
  show every name as a line. psf__requests-2317 has 40 failing tests and
  the dataset wants 8 (0 at 600 and 1,000, 6 at 2,000); requests-1724 has
  32 and wants 6; seaborn-3069 has 76, 74 failing the same way. Two
  candidates, measured separately: render a list template compactly (the
  shared prefix once, then only the parts that differ, about a third of
  the size), and group failing tests by how they fail (the `E` line or
  exception under each) so the small groups come first.
- [ ] **Names that differ only in numbers.** Parametrized test ids
  (`test_rolling_iter[numpy-3-True-1]`, `[numpy-3-True-2]`) mask to one
  shape, so they are repeats, not a list (xarray-6744, 1 of 3 at every
  budget); likewise `expected:<16384> but was:<32768>` and
  `expected:<1024> but was:<2048>` (JacksonCore-10). Candidate: digits
  inside a test id's brackets are part of the name. The rule that a
  number alone is not news stays for everything else.
- [ ] **Maven chatter at 600.** Error- and warning-level lines that are
  neither advice nor failures (`[ERROR] Errors:`, `See dump files`, the
  effective-model warnings) still take budget; Defects4J-2 is 51 % at
  600.
- [ ] **Keep lines with alternative forms.** A test named in Maven's
  short form (`Test.testX » StackOverflow`) does not match a keep line
  written with the full class name, so the bench counts a named test as
  missed (Gson-16, JacksonCore-10). Let a keep line list equivalent forms;
  report both scores the first time, since it changes the numbers.
  *Repo.*
- [ ] **Re-ablate removed terms.** The end-of-output position bonus and
  the first-occurrence bonus were removed by ablation on the dev corpus;
  re-test them on the held-out set.
- [ ] **Tool-specific rules.** Recheck which earn their place; generic
  signals alone reach 88 % at 600 and 94.5 % at 2000 on the dev corpus.

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
