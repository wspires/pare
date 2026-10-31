---
name: pare
description: Use when running a command whose output can be long (a test suite, a build, a linter, a package install, terraform plan, kubectl logs), and when reading a pare summary to get back lines it left out. Run such commands as `pare -- <command>`; expand a summary from its run file with `--only T<n>` or `--since L<a> --until L<b>`.
---

# pare: long command output to a token budget

pare runs a command, prints a summary of its output that fits a token
budget, and saves the full output to a run file. Nothing is lost: the
summary ends with what it omitted and where the raw output is.

## Running a command

For commands whose output can be long, run them as `pare -- <command>`:

```
pare -- pytest -x
pare -- cargo test
pare --budget 1000 -- ./gradlew build
```

pare ends the summary with the exit status and exits with the command's
exit code, so a failure is still a failure. The plugin's hook already
rewrites common commands (pytest, npm test, cargo build, go test, make,
terraform plan and others) this way, so a command may come back as a
pare summary even when you did not write `pare --`.

Do not use pare for output you need exactly: reading files, `git diff`,
`grep`, or anything you will edit from.

## Reading a summary

- The first line gives the size of the output and of the summary.
- `## templates` lists line patterns as `T<n>`, most important first,
  with a count, a severity mark (`E` error, `W` warning) and an example
  line number `L<n>`.
- `## selected lines` shows the lines pare judged most informative, with
  `┆` where lines were left out.
- `## omitted` says how much was left out, and `## raw:` names the run
  file that holds all of it.

## Getting omitted lines back

Read from the run file named on the `## raw:` line instead of rerunning
the command:

```
pare --only T24 < <run file>              # every line of template T24
pare --only T3,T7 < <run file>            # several templates
pare --since L200 --until L260 < <run file>   # a line range
pare --since -100 < <run file>            # the last 100 lines
pare --min-severity error < <run file>    # error records only
```

Expand only what the task needs. If a failure's cause is not in the
summary, look at the templates marked `E` first, then the lines around
the first error.
