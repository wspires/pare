# Roadmap

pare 0.2.0 is built: a single Rust binary, verified byte for byte against
the Python prototype it replaced, measured on held-out samples drawn
blind from SWE-bench and Defects4J, and not yet released. This file
says what comes next, in order, and keeps one line per finished item with
a pointer to its record. The experiments are in `docs/HISTORY.md`, the
held-out samples in `corpus/heldout/README.md`, the changes in
`CHANGELOG.md`.

Each item says who can do it. **Repo** means it is code or docs that can
be done in this repository. **Maintainer** means it needs real sessions,
real credentials or a decision that is not the code's to make.

## Next

### Release

- [ ] **First release.** The one-time setup in RELEASING.md (the tap
  repository, two secrets, PyPI's trusted publisher), then tag `v0.2.0`.
  Until then every install line in the README fails. *Maintainer.*
- [ ] **Version and the compatibility promise.** Drafted in
  `docs/USAGE.md`, "What you can rely on": a patch release prints the
  same bytes; a minor release may change what a summary holds and says
  so in the CHANGELOG, and says "renumbers templates" when it does; the
  section markers, flags (renamed ones keep an alias), exit statuses and
  the `.json` record's fields hold across 0.x; the record already names
  the pare version that wrote a run file. To accept or amend before
  0.2.0, since the release makes the promise. *Maintainer.*

### Evidence

- [ ] **Real logs.** Every held-out case so far is a dataset test run; no
  one has measured pare on a Terraform apply, a Next.js build or a flaky
  CI job from real work. With the Claude Code hook on for a week
  (`integrations/`), run files accumulate; `bench/harvest.py scan` then
  ranks first the outputs where the agent went past pare's summary
  (expanded it, read the run file, reran without pare, opened a file the
  summary did not show), and `bench/collect.py` turns the reviewed ones
  into held-out cases with keep lists written before pare's output is
  looked at (`corpus/heldout/README.md`, "Protocol"). Aim for 15 to 20.
  *Maintainer, on a machine whose transcripts persist.*
- [x] **Claude token rate.** Measured on 29 October 2026: Claude Opus
  5.5 tokenizes pare's output at 1.96 bytes per token against 2.82 for
  cl100k and o200k, so a `--budget 2000` summary was about 2,870 Claude
  tokens. The default is 2.0 since: summaries are 30 % smaller at the
  same budget, the README's tables are restated, and 600 is no longer a
  budget to recommend (`docs/DESIGN.md`, "Tokens and the budget";
  CHANGELOG).
- [x] **Agent-success eval.** `bench/agent_eval.py` runs an agent on each
  task in `corpus/agent/` with the raw output, a truncation, pare's
  summary, sqz or tidyout as its tool result (`corpus/agent/README.md`).
  First run, 36 trials: pare matched the raw output on every task at
  8 % of the tokens on the diagnose tasks and 30 % on the fix tasks;
  truncation missed the outage cause every time; on a 4 KB cargo output
  pare cost more turns than raw (`corpus/heldout/README.md`,
  "Agent-success evaluation").
- [x] **Agent-success eval on held-out cases.** Sixteen cases from
  Defects4J seed 7 and SWE-bench seed 5, 144 trials: every condition
  named every failing test, and pare cost less than raw only on the two
  logs over 60 KB; on the fourteen of 2 to 19 KB it cost 21 % more, at
  1.6 turns against 1.0, because the agent expanded the summary on
  seven of sixteen cases (`corpus/heldout/README.md`, "Third run").
- [x] **Raise `auto`'s pass-through.** From 1,000 tokens to twice the
  ceiling, 4,000 by default, on the three agent runs' evidence
  (`docs/DESIGN.md`, "The pass-through, from the agent runs"). The
  recall benchmark is unchanged by it.
- [x] **Measure the new pass-through with the agent harness.** Done
  the same day (`corpus/heldout/README.md`, "Fourth run"): still 48 of
  48, pare's mean fell from 4,593 to 4,208 tokens and 1.6 to 1.5 turns,
  the two 4 KB cases that expanded every trial print whole and take one
  turn, and the 8 to 20 KB cases split three to two for the summary, so
  the factor stays at two.
- [x] **State the harness's own overhead.** Each trial counts its
  fixed prompt (system text, tool schemas, the task prompt with no
  output) with one free `count_tokens` call; the summary shows it and
  the input net of it, once per turn, in the condition table and the
  per-task cells (`corpus/agent/README.md`).

### Candidates, not before evidence asks

Each is a miss the current sets show. None changes before a fresh sample
or real logs say it matters, and a change spends the sample it is
measured on (`corpus/heldout/README.md`).

- [ ] **Inner runs in pytest's own suite.** Both pytest misses on fresh
  data (pytest-8399, pytest-7205) come from the inner pytest sessions
  that pytester prints: their one-off `FAILED`/`ERROR` lines rank as rare
  errors and crowd out the real failures. A candidate: recognize an inner
  session (a `test session starts` banner after the first, inside a
  captured-output section) and score its lines as body, not verdicts.
  Narrow (pytest's own repository, and plugins tested with pytester).
- [ ] **The template table at 600.** On the cold-cache Maven build
  (`corpus/generated/maven-compile`) pare 0.2.0 at 600 shows twelve
  template entries and no selected lines, and misses a compile error, the
  reactor's `FAILURE` line and `BUILD FAILURE`; the Terraform plan spends
  its 600 on twenty near-identical resource blocks. Candidate: cap the
  table when it alone would fill the budget. The template floor halved
  the problem at 2,000 (`docs/HISTORY.md`, "Generated corpus: real tools,
  chosen failures").
- [ ] **Wide plain-text errors.** kubectl-apply's 230-character validation
  error loses its tail (`dial tcp …: i/o timeout`) to `--width 200` at
  600. The structured-line clip keeps the telling fields of a JSON or
  logfmt line; a plain line is still cut at the width. Candidate: cut a
  long error line at a separator and keep its last clause.
- [ ] **Budget squeezes at 600.** Math-66's assertion messages folded into
  a `Failed tests:` block, Gson-17's trigger tests crowded out by locale
  failures from the capture environment, Csv-3's long assertion message:
  each the last few lines a 600-token summary cannot hold. A fixed budget
  below 1,000 is where these show; `auto`, the default, only stops early
  and does not change what fits.

### Later

- [ ] **Per-line checks on worker threads.** They depend only on the
  line's text, so the pass keeps them a pure function; worth it only for
  input arriving faster than pare reads it (about 7 MB/s today).
- [ ] **Rolling follow mode.** A whole-run summary rewritten in place, for
  agents that poll a file rather than read a stream. Design:
  `docs/DESIGN.md`, "Follow mode". Build it only if real use shows the
  need.
- [ ] **Retire parity.** `bench/parity.py` holds the Rust version to the
  Python reference, with `PARE_REFERENCE_RULES=1` turning off the
  Rust-only rules (structured lines, the tail verdict rule, `auto` as the
  default, the binary-input check). Once those rules outnumber what
  parity still checks,
  regenerate the golden files from the Rust version and drop the
  reference from CI. *Repo.*

## Done

One line per finished item, in the order of the original stages, with
where its record is. The stage headings are kept, in the past tense, so
that references to "stage 3" and "stage 5" elsewhere still land.

### Stage 1: validate on real data

The gate for everything after it: no tuning before held-out numbers
existed. Thirteen blind samples were drawn, seven from Defects4J and six
from SWE-bench Verified; each was measured before anyone read it, and
counted as spent once a change was measured on it.

| done | what | record |
|---|---|---|
| SWE-bench cases | `bench/swebench_cases.py`: failing test runs reproduced from SWE-bench tasks, labelled by FAIL_TO_PASS. Seed 0: 60 % at 2,000, which found the failing-test collapse | `corpus/heldout/README.md`, "SWE-bench cases" and "SWE-bench seed 0 and Defects4J seed 0" |
| Defects4J cases | `bench/defects4j_cases.py`: Java test runs rebuilt from Defects4J bugs in a JDK 8 container, labelled by trigger tests and exceptions. Seed 0: 29 % at 600. Maven projects only | same, "Defects4J cases" |
| SWE-bench seed 1, Defects4J seed 2 | the failing-test fix holds on unseen data: 63 → 89 % and 30 → 51 % | same, "SWE-bench seed 1 and Defects4J seed 2" |
| SWE-bench seed 2, Defects4J seed 3 | 86 / 91 / 100 % and 67 / 94 / 98 % at 600 / 1,000 / 2,000 | same, "SWE-bench seed 2 and Defects4J seed 3" |
| Defects4J seed 4 | 92 / 100 / 100 %; its misses led to the JUnit 3 rules | same, "Defects4J seed 4" |
| Defects4J seed 5 | 87 / 91 / 100 %; the JUnit 3 rules hold (80 → 87 % at 600); its misses led to the pair and clip rules | same, "Defects4J seed 5" |
| SWE-bench seed 3 | 94 / 100 / 100 %; none of the Java-motivated changes cost pytest a line | same, "SWE-bench seed 3" |
| Defects4J seed 6 | 92.5 / 100 / 100 %, and 100 % with `auto` at 71 % of the tokens of 2,000; the pair rule earns one line | same, "Defects4J seed 6" |
| SWE-bench seed 4 | 90 / 97 / 97 %; its miss (pytest-8399) led to the tail verdict rule | same, "SWE-bench seed 4" |
| Defects4J seed 7 | 92 / 100 / 100 %, and 100 % with `auto` at 69 %; the verdict rule changes nothing on Java | same, "Defects4J seed 7" |
| SWE-bench seed 5 | 67 / 70 / 100 %, and 100 % with `auto`; every miss is pytest-7205's inner runs | same, "SWE-bench seed 5" |

### Stage 2: make collection a side effect of use

The blocker for stage 1 was friction, so this came first in practice.

| done | what | record |
|---|---|---|
| Command mode | `pare -- CMD`: the exit code survives and the command is recorded next to the run file | `docs/DESIGN.md`, "Command mode" |
| `bench/collect.py` | turns a run file into a held-out case with its `.cmd`, scrubbed, never showing pare's summary | `corpus/heldout/README.md`, "Protocol" |
| Agent integration | an instruction for `CLAUDE.md` or `AGENTS.md`, and a Claude Code hook that rewrites test, build and install commands; tried in a real session and registered for this repository | `integrations/README.md` |
| `bench/harvest.py` | mines Claude Code transcripts for long outputs, scrubs them, proposes keep lines from what the agent did next, and ranks first the outputs where the agent went past pare's summary | `corpus/heldout/README.md`, "Harvesting from existing transcripts" |
| `pare stats` | totals what commands printed against what pare printed, an upper bound on the saving | `integrations/README.md` |

### Stage 3: fix only what the held-out set showed

Each change came from a held-out miss, was checked on every set, and was
measured on the next fresh sample.

| done | what | record |
|---|---|---|
| Failing tests collapsed into one entry | an error line in a template of mostly distinct shapes is a list item, not a repeat; an `advice` class for build-tool closing advice. SWE-bench 60 → 82.5 % at 2,000, Defects4J 29 → 48 % at 600 | `docs/HISTORY.md`, "First change from held-out evidence" |
| Compact lists of failures | one header with the shared prefix and one short item per test, about half the size | same |
| Which of many failures matter | a failure that fails unlike most of its list gets +2 salience and is listed first | same |
| Names that differ only in numbers | a name's bracketed parameters stay in the shape key | same |
| Maven chatter at 600 | level-only and advice lines score −1 whatever their level, and the head and tail windows skip them; `ComparisonFailure` is an error | `docs/HISTORY.md`, "Maven chatter" |
| Keep lines with alternative forms | `a \|\| b` in a keep line counts when either appears; the Defects4J labeller lists every id form the log shows | `corpus/heldout/README.md`, "Labels" |
| Removed terms re-ablated | the four position terms, back at weight 0, earn nothing on nine sets | `docs/DESIGN.md`, "Scoring model" |
| Tool-specific rules rechecked | each rule has a switch and an ablation row; the pytest markers and the blank-in-block rule earn their place, Maven needs none | `docs/DESIGN.md`, "Tool-specific rules" |
| Android logcat | a synthetic threadtime case: padded tags, entries split by message, a crashed process's death as an error. A real capture is still wanted | `docs/HISTORY.md`, "Android logcat entries" |
| JUnit 3 assertion messages | a frame's method names are not error words; a warning's rarity counts three quarters. Seed 4 92 → 100 % at 600; holds on seed 5 | `docs/HISTORY.md`, "JUnit 3 assertion messages" |
| Messages that differ in one word | a template seen twice lists both lines; a clip that saves nothing is not made; Surefire 3's `Cls#testX` is a label form | `docs/HISTORY.md`, "Messages that differ in one word" |
| The tail verdict | an error line of a common shape stays in the tail window when it is the only one of its shape there | `docs/HISTORY.md`, "The verdict that repeats a shape" |

### Stage 4: release decisions

| done | what |
|---|---|
| License | MIT OR Apache-2.0; drain3's MIT notice in `THIRD-PARTY-NOTICES` |
| Name | `pare` is taken on PyPI and crates.io; the package is `pare-cli` there and the command is `pare` everywhere |

### Stage 5: Rust port

pare moved to Rust for a single-binary install and speed on large logs,
and as a way to learn Rust from C++. The Python version was the reference
until the Rust one matched it byte for byte; it is kept, frozen, in
`reference/`. Template ids stayed identical (Drain as drain3 0.9.11
clusters), which is what made the comparison meaningful. The code is
written to be read: `//!` module docs naming the Python counterpart,
`// Rust note:` comments for a C++ reader, `docs/ARCHITECTURE.md` and
`docs/RUST.md`, `#![forbid(unsafe_code)]`, clippy with warnings as errors.

| done | what | record |
|---|---|---|
| 0. Comparison tooling | `bench/parity.py`: outputs, exit codes and per-line dumps (`$PARE_DUMP`) byte for byte over every log and generated edge case under 14 flag sets | `CONTRIBUTING.md`, "Parity with the Python version" |
| 1. Crate skeleton | options, input, run file, pass-through, CI, release builds | `CHANGELOG.md`, 0.2.0; `docs/ARCHITECTURE.md` |
| 2. Segmentation, masking, Drain | template ids match; Python's character classes generated from 3.13 (`src/pyunicode.rs`); a two-engine regex scan for lookaround | same |
| 3. Vocabulary, severity, scoring | the whole dump matches, scores to the bit | same |
| 4. Summary and allocation | batch output identical on every run | same |
| 5. Selection, command mode, follow mode, prune, stats | the port complete: 0 of 2,730 parity runs differ. Follow mode reads with a thread and a channel, not `select()` | same; `CHANGELOG.md`, 0.2.0 |
| 6. Distribution | `pare-cli` on PyPI (maturin wheels with the binary) and crates.io, a Homebrew tap, `install.sh`, static Linux, macOS and Windows archives; one tag push publishes all four | `RELEASING.md` |
| The first change in behaviour | the reference stays frozen; `PARE_REFERENCE_RULES=1` turns off the Rust-only rules so parity still runs, and the rules have tests of their own | `docs/HISTORY.md`, "The verdict that repeats a shape" |

### Stage 6: after the port

Each changes nothing unless it is used, except `auto`, which became the
default, and the command-line changes made before the first release.

| done | what | record |
|---|---|---|
| `$PARE_BUDGET` | the budget when `--budget` is not given | `docs/USAGE.md` |
| Configurable continuation rules | a `[continuation]` class in the vocabulary file; the built-in rules became its defaults | `docs/DESIGN.md`, "Vocabulary" |
| Configurable masks | `[mask NAME]` sections, run before the built-in masks | same |
| `--budget auto` | the budget as a ceiling the summary stops short of; the default since four fresh samples kept what 2,000 kept at 69 to 79 % of its tokens | `docs/DESIGN.md`, "Automatic budget" |
| The command line | `prune`, `stats` and `vocab` as subcommands, `--ascii`, `--bytes-per-token`, grouped `--help`, usage errors exit 2, `--interval` defaults to 30 | `CHANGELOG.md`, Unreleased; `docs/USAGE.md` |
| Binary input | a NUL byte in the first 8 KiB: saved, not summarised, two lines out | `docs/USAGE.md`, "What pare prints" |
| Structured lines | JSON and logfmt lines recognized one by one and clipped keeping the fields that say what went wrong; pino's numeric levels count. Real structured logs in a held-out sample are still owed | `docs/DESIGN.md`, "Structured lines" |

Before all of that: the Python prototype, the benchmark against sqz and
tidyout, the dev and generated corpora, real-tokenizer calibration, the
single pass with bounded memory, follow mode with record-preserving cuts
and the differential tests (`docs/HISTORY.md`).
