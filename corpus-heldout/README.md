# Held-out corpus

The dev corpus in `corpus/` was used to fix pare's misses, so its recall is
a training score. This directory is for logs pare has never been tuned on.
It is empty until real logs are collected; the protocol matters more than
the directory.

## Harvesting from existing transcripts

You probably have candidate logs already. Claude Code keeps every session
under `~/.claude/projects/`, including the full text of outputs too large
for the model's context.

```
python bench/harvest.py scan --redact <your-github-handle>   # candidates -> corpus-heldout/inbox/
# for each candidate: read NAME.log, write NAME.keep (NAME.keep.proposed is a draft)
python bench/harvest.py promote NAME [NAME ...]
python bench/collect.py check
```

`inbox/` is ignored by git: it holds scrubbed but unreviewed text from
your sessions. The scrub replaces home paths, email addresses, your login
name and git identity, `--redact` words and credential-shaped strings, and
prints the words it redacts; it cannot know a colleague's name in a commit
message, a customer's data in a log, or an internal host name, so read
each log before promoting it. The proposed keep lines come only from what
the agent did next (lines it quoted, locations and files it opened, tests
it re-ran), never from pare, so they are blind to pare; they do lean
toward what the agent saw, which for a saved large output was a 2 KB
preview. Promoted cases record `transcript` as their source and whether
the keep list started from the proposals, and results should be reported
by source.

## SWE-bench cases

`bench/swebench_cases.py` produces held-out cases with labels nobody
wrote: each SWE-bench task records which tests fail at its buggy commit
(FAIL_TO_PASS), and the harness reruns them in the task's published
Docker image.

```
pip install -e '.[datasets]'          # the swebench package
python bench/swebench_cases.py run --sample 20 --dry-run   # which tasks, which images
python bench/swebench_cases.py run --sample 20             # writes corpus-heldout/swebench/
python bench/run.py --corpus corpus-heldout/swebench --budget 2000 --show-misses
```

The sample is seeded and spread across repositories, among tasks with at
most 10 failing tests. For each task: pull the image (1 to 4 GB), run the
task's eval script without the fix, confirm with SWE-bench's log parser
that the FAIL_TO_PASS tests failed, and write the case: the test command's
output, the command, and a keep list of the failing test ids as they
appear in the output. A task whose tests did not fail, whose eval did not
run cleanly, or whose output is under 40 lines (pare passes those through
unchanged) is skipped and reported; full logs of failed runs go to
`swebench/_failed/`, which git ignores. Images this run pulled are removed
afterwards unless `--keep-images`. `--dataset` takes a local
`.jsonl`/`.json`/`.parquet` with SWE-bench's fields when Hugging Face is out
of reach.

These are a different kind of keep list from the transcript cases: they
only ask whether the summary names every failing test, not whether it
keeps the assertion or the location. Report them as their own source.

## Defects4J cases

`bench/defects4j_cases.py` does the same for Java. Defects4J records, per
bug, the commit that fixed it, a patch that reverts only the fix, and the
tests that fail on the buggy version with the exception each raises. The
harness rebuilds the buggy version from the upstream project on GitHub
(Defects4J's own installation and download server are not needed), runs
the failing test classes with the project's Maven build in a JDK 8
container, and labels the case with those tests and exceptions as the
output shows them (the assertion message alone when JUnit 4 prints it
under another exception class than Defects4J recorded).

```
python3 bench/defects4j_cases.py run --sample 20 --dry-run
python3 bench/defects4j_cases.py run --sample 20        # writes corpus-heldout/defects4j/
python3 bench/run.py --corpus corpus-heldout/defects4j --budget 600 --show-misses
```

Maven projects only (14 of Defects4J's 17). Docker Hub rate-limits
anonymous pulls; `--image mirror.gcr.io/library/maven:3.9-eclipse-temurin-8`
pulls the same image from Google's mirror. Behind a proxy, pass it to the
container with `--docker-arg` and `--pass-env MAVEN_OPTS`.

## Results so far

Reported per source, never merged, and with the dev corpus kept apart.
Recall is the share of keep lines in pare's output.

| source | cases | budget 2,000 | budget 600 |
|---|---:|---|---|
| SWE-bench Verified, seed 0 (maintainer's run) | 16 | 60 % → 82.5 % (min 0 %) | 47.5 % → 45 % |
| Defects4J, seed 0, one bug per project | 11 | 94 % → 97 % (min 75 %), mostly pass-through | 29 % → 48 % (min 25 %) |
| dev corpus, for comparison (tuned on) | 8 | 100 % | 79.5 % → 78 % |

The first numbers are pare before the failing-test fix, the second after.
Both sources missed in the same way: a run with several failing tests
names each in a line of the same shape, pare listed that shape once with a
count, and the tests themselves were dropped; on Maven at 600, the build's
closing `[ERROR]` advice took the budget as well. The fix was developed on
a separate Defects4J sample (`corpus-dev/defects4j/`), but these sets
found the problem and were checked during the work, so **they are no
longer held out**. The after-fix numbers are a dev score; a fresh sample
is the honest one. Draw it without the tasks that already have cases:

```
python3 bench/swebench_cases.py run --seed 1 --exclude corpus-heldout/swebench --out corpus-heldout/swebench-2
python3 bench/defects4j_cases.py run --seed 2 --exclude corpus-heldout/defects4j --exclude corpus-dev/defects4j --out corpus-heldout/defects4j-2
```

## Protocol

1. **Collect real logs from actual agent sessions.** The easy way: have
   the agent run its noisy commands as `pare -- CMD` (by instruction or
   with the hook in `integrations/`); each run leaves a raw run file and a
   record of the command. Then `python bench/collect.py list` shows recent
   runs and `python bench/collect.py add N` copies one here with its
   `.cmd`, an empty `.keep` and a `MANIFEST.tsv` line (pare version and
   commit at collection). It replaces your home and working directories
   with `/home/dev` and `/home/dev/proj` and points at credential-shaped
   lines; nothing else is changed. Without pare in the loop, save the raw
   output of the command the agent ran (`cmd > name.log 2>&1`, or the
   tool-result text from the session transcript), untouched: no trimming,
   no scrubbing beyond secrets and paths. Prefer tools the dev corpus does not cover: Maven, Gradle,
   Terraform, kubectl, Bazel, Next.js build, tsc, eslint, docker compose,
   pip install -v, GitHub Actions runs. Aim for 15 to 20 files.
2. **Write `name.keep` before running pare on the log.** One line per
   must-survive line: the lines a person fixing the problem would need (the
   error messages, the failing test names, the location, the final
   verdict). Copy them verbatim from the log; the check is a substring
   match on the stripped text. Record `name.cmd` with the command.
   `python bench/collect.py check` confirms every case has a command and a
   keep list whose lines are in its log.
3. **Do not look at pare's output while writing the keep list.** If a keep
   line turns out to be unreasonable after the fact, note it in this file
   rather than editing the list.
4. **Run and report separately:**

   ```
   .venv/bin/python bench/run.py --corpus corpus-heldout --budget 2000
   .venv/bin/python bench/run.py --corpus corpus-heldout --budget 600 --show-misses
   ```

   Report these numbers next to the dev-corpus numbers, never merged with
   them.
5. **Do not tune on this corpus.** If a held-out miss reveals a bug (a false
   positive in severity detection, a segmentation error), fix it with a
   regression test and note that the held-out set has now influenced the
   code; after enough such fixes, collect a fresh held-out set.

## Agent-success evaluation

Recall measures presence, not usefulness. The complementary test: take a
few real tasks (fix a failing test, fix a broken build, find the cause of an
outage from a log) and run an agent on each with the full output, with
pare's summary, with sqz's output and with tidyout's output as the tool
result. Report task success and tokens side by side. A summary that keeps
every must-survive line but buries it in forty others may still lose here.
