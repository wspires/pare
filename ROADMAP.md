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
- [ ] **SWE-bench cases.** Failing test runs reproduced from SWE-bench
  tasks, which record a repository at a buggy commit and the tests that
  fail there (FAIL_TO_PASS). The keep list is those test ids as they appear
  in the output, so no one's judgement is involved and the labels are
  blind to pare by construction. Reported as its own source (`dataset` in
  the manifest), never merged with transcript cases. The harness is built:
  `bench/swebench_cases.py run --sample 20` pulls each task's published
  image, runs its eval script without the fix, checks with SWE-bench's own
  parser that the failing tests did fail, writes the case, and removes the
  image. Verified end to end in the development container on one task
  rebuilt from its upstream pull request (Hugging Face, where the dataset
  lives, is blocked there). Running the 20-task sample needs Docker, the
  dataset, and about 4 GB of disk per task at a time. *Maintainer, on a
  machine with Docker and Hugging Face access.*
- [ ] **Defects4J cases.** The same idea for Java (Maven and Ant), for
  build-tool coverage SWE-bench lacks. *Repo, after the SWE-bench run.*
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
- [ ] **Unused budget while same-shape failures are omitted.** On the
  reconstructed SWE-bench task (pytest, 22 failures, network cut off),
  pare listed the `FAILED ...` lines once as a template with a count and
  never showed the one failing test the task is about, at 600 and at 2000,
  where it used about half the budget. If the real SWE-bench cases show
  the same, failing-test summary lines need to compete individually.
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
