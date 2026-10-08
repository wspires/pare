# Held-out corpus

The dev corpus in `corpus/` was used to fix pare's misses, so its recall is
a training score. This directory is for logs pare has never been tuned on.
It is empty until real logs are collected; the protocol matters more than
the directory.

## Protocol

1. **Collect real logs from actual agent sessions.** Save the raw output of
   the command the agent ran (`cmd 2>&1 > name.log`, or the tool-result text
   from the session transcript), untouched: no trimming, no scrubbing beyond
   secrets. Prefer tools the dev corpus does not cover: Maven, Gradle,
   Terraform, kubectl, Bazel, Next.js build, tsc, eslint, docker compose,
   pip install -v, GitHub Actions runs. Aim for 15 to 20 files.
2. **Write `name.keep` before running pare on the log.** One line per
   must-survive line: the lines a person fixing the problem would need (the
   error messages, the failing test names, the location, the final
   verdict). Copy them verbatim from the log; the check is a substring
   match on the stripped text. Record `name.cmd` with the command.
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
