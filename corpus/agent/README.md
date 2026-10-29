# corpus/agent

Tasks for the agent-success evaluation, `bench/agent_eval.py`. Recall
(`bench/run.py`) says whether the must-survive lines are present in a
summary; this says whether an agent that gets the summary as its tool
result still does the job, and what it spends. Each trial runs one task
under one condition (the full output, a head-and-tail truncation, pare's
summary, sqz, tidyout) and records success, tokens and turns.

## The tasks

| task | kind | what the agent does | output |
|---|---|---|---|
| `pytest-ledger` | fix | fix a Python ledger library (four seeded bugs in four modules) until its 112 tests pass; `pytest.ini` turns on `log_cli`, so the run is about 1,900 lines | 123 KB |
| `cargo-inventory` | fix | make a Rust crate compile (two errors among unused-variable warnings) and then fix the one unit test that fails | 4 KB |
| `outage-cause` | diagnose | from `corpus/dev/server-outage.log`, say what took the service down, how it responded and how it recovered | 535 KB |
| `maven-test-cause` | diagnose | from `corpus/generated/maven-test.log`, list the failing tests with their exceptions and the failing module | 10 KB |

`--heldout DIR` adds a diagnose task per held-out case in a directory
(`corpus/heldout/defects4j`, `corpus/heldout/swebench`): list every
failing test with its class or file and the error. The expected findings
come from the case's `.json`, the dataset's labels, so there are 214 such
tasks at hand, and `--sample N --seed S` draws a fixed subset.

## A task

A directory with `task.json`:

```json
{
  "kind": "fix",
  "workspace": "proj",
  "command": "python3 -m pytest",
  "check": "python3 -m pytest -q -p no:cacheprovider",
  "requires": ["python3:pytest"],
  "prompt": "The test suite of this small Python ledger library fails. Fix the library ...",
  "notes": "what is broken, for the reader; the agent never sees this"
}
```

A **fix** task copies `workspace` to a temporary directory, runs
`command` there through the condition, and gives the agent that output
and a `bash` tool. Every bash result goes through the condition too; in
the pare condition that means exactly what the Claude Code hook does
(`integrations/claude-code/pare-hook.py`, whose `plan()` the harness
calls): test, build and install commands are wrapped, and `cat`,
`grep`, `sed`, pipelines and `pare` itself expanding a summary print as
usual. The record counts the agent's commands and how many were
wrapped. Afterwards `check` runs in the
workspace and exit 0 is success. `requires` lists programs the task
needs, and `prog:module` a Python interpreter that can import a module;
a task whose requirements are missing is skipped, saying which.

A **diagnose** task has no workspace:

```json
{
  "kind": "diagnose",
  "log": "../../dev/server-outage.log",
  "command": "docker logs inventory-api",
  "prompt": "An API service went down and came back. From the output ...",
  "expect": [
    ["db-primary.internal:5432", "connection reset by peer || connection to db-primary.internal:5432 lost"],
    ["pool exhausted"],
    "reconnected"
  ]
}
```

The log is replayed through the condition and the agent answers from
it; in the pare condition it also gets a `pare` tool that runs pare's
read flags over the run file. The answer is graded against `expect`: a
list of findings, each a term or a list of terms that must all appear,
and a term lists alternative forms as `a || b`. Terms match whole, as
keep lines do (`bench/collect.py`), so `testResolve1` is not credited by
`testResolve12`. The score is the share of findings named; success is
all of them.

## Adding a task

Take a real failure, not one invented to suit a heuristic: a project
whose build or tests break in a way that happened, with output long
enough that something has to give. Write `prompt` as the person who hit
it would brief a colleague, not as a hint about where the bug is. For a
fix task, check that the project is broken as shipped and that the
intended fix makes `check` pass (`bench/test_agent_eval.py` does this
for the shipped ones). For a diagnose task, write `expect` before
running pare on the log, as with a keep list. Then
`python3 bench/agent_eval.py --tasks NAME --dry-run --show` prints what
each condition would give the agent.

## Running it

```
python3 bench/agent_eval.py --list
python3 bench/agent_eval.py --dry-run                                    # no model calls
python3 bench/agent_eval.py --conditions raw,truncate,pare --trials 3
python3 bench/agent_eval.py --heldout corpus/heldout/defects4j --sample 8 --conditions raw,pare
python3 bench/agent_eval.py --report bench/agent-runs/<stamp>/results.jsonl
```

Needs `pip install anthropic` and credentials (`ANTHROPIC_API_KEY`, or
`ant auth login`), checked once with a free call before any trial runs;
the pare condition needs a build that takes the budget it is given
(`--budget auto` needs pare 0.2.0 or later: `cargo build --release`).
Every trial spends money: a `raw` trial on `outage-cause` costs about
300k input tokens, the 535 KB log on every turn (the record's first
run). Results land in `bench/agent-runs/<stamp>/`: `results.jsonl` (one
record per trial), a transcript per trial, `summary.md`. The directory
is ignored by git; copy a summary into `corpus/heldout/README.md` under
"Agent-success evaluation" when it is worth keeping, with the model,
effort and pare version from `config.json`.

The output of one run is a table per task kind: success rate, mean
score, input tokens over the whole trial (cache reads and writes
included), output tokens, the largest single prompt, turns, and the
bytes of command output the agent was shown.

A note on what this can and cannot show. Success on a fix task is the
check command passing, which an agent can reach by a route the summary
did not help with (reading the source). A diagnose score is the share
of findings named, not whether the explanation was right. The numbers
compare conditions on the same tasks with the same model; they are not
a measure of the model, and a handful of tasks with one trial each is a
smoke test, not a result. Run `--trials 3` or more before quoting
anything.
