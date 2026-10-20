# Using pare from an agent

Two ways to route an agent's noisy commands through pare. Both use command
mode, `pare -- CMD ARGS`, which runs the command itself: the exit code
survives (a pipe would report pare's), the summary ends with the exit
status, and the command is recorded next to the run file so
`bench/collect.py` can turn it into a held-out case later.

Either way, every summarised run leaves a run file in `~/.cache/pare/runs`.
That is the point for this stage of the roadmap: normal agent work becomes
the held-out corpus (see `corpus-heldout/README.md` and
`python bench/collect.py list`).

## 1. An instruction (any agent)

Add this to the agent's instructions file (`CLAUDE.md`, `AGENTS.md`, or the
equivalent):

```markdown
## Long command output

For commands whose output can be long (test suites, builds, linters,
package installs, `terraform plan`, `kubectl logs`), run them as
`pare -- <command>`, for example `pare -- pytest -x` or
`pare --budget 1000 -- ./gradlew build`. pare prints a summary that fits a
token budget, ends with the exit status, and exits with the command's exit
code. The full output is in the run file named on its `## raw:` line;
expand it with `pare --only T<n> < <run file>` (a template from the
summary) or `pare --since L<a> --until L<b> < <run file>` (a line range).

Do not use pare for output you need exactly: reading files, `git diff`,
`grep`, or anything you will edit from.
```

This works with any agent and changes nothing about permissions. The
agent decides when to use it.

## 2. A Claude Code hook (automatic)

`claude-code/pare-hook.py` is a PreToolUse hook for the Bash tool. When the
command is a single simple command from its list (pytest, npm/pnpm/yarn
test and build, cargo, go test, make, Maven, Gradle, dotnet, terraform
plan, docker build, kubectl logs, and others), it rewrites it to
`pare --budget 2000 -- <command>`. It leaves alone:

- compound commands: `&&`, `||`, `;`, pipes, redirections, `$(...)`, `cd`.
  Wrapping them would run them in a subshell. A trailing `2>&1` is the one
  exception; it is dropped, since pare merges stderr itself;
- watch modes, followed logs and dev servers, which never exit;
- everything not on the list, including `cat`, `git diff` and `grep`;
- commands started with `run_in_background`.

Check what it would do with a command:

```
integrations/claude-code/pare-hook.py --explain "npm test -- --runInBand"
```

Register it in `.claude/settings.json` (project) or
`~/.claude/settings.json` (user), with an absolute path:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "/path/to/pare/integrations/claude-code/pare-hook.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

`pare` must be on the PATH that Claude Code's hooks and Bash tool see
(any install in the README's "Install" section puts it there, or set
`PARE_BIN` to its path). If it is not, the hook does nothing.

### Permissions: read this before choosing a mode

The hook returns the rewritten command in `updatedInput`. Per Claude
Code's hooks reference, permission rules are then evaluated against the
rewritten command, not the one the agent wrote, and the hook may also
return a permission decision. `PARE_HOOK_DECISION` picks what it sends:

| `PARE_HOOK_DECISION` | effect |
|---|---|
| unset (default) | No decision is sent; the normal permission flow applies to `pare --budget 2000 -- npm test`. An allow rule written for `npm test` will not match it, so add rules for the wrapped form, for example `Bash(pare --budget 2000 -- npm test:*)`. |
| `ask` | Every rewritten command prompts, showing the rewritten form. |
| `allow` | Every rewritten command runs without a prompt. Your allow and ask rules no longer gate the commands on the hook's list; deny rules still apply. Use it only if you would allow everything on that list anyway. |

Verified in a Claude Code session (cloud, October 2026): with no decision
sent, the rewrite is applied. A listed command came back as pare's output,
ending with its `## raw: … · exit 0` line (a run that fits now ends
without it; see below). The hook also took effect as
soon as `.claude/settings.json` was written, without restarting the
session. That session's permission mode allowed the command either way,
so how the rewrite meets a narrower allow rule is still to be seen; if a
rewrite were ignored, nothing would break, as the command runs unwrapped.

Claude Code hands the hook a command without a leading `cd` into the
current directory, so `cd <project> && pytest` is wrapped like `pytest`.

This repository registers the hook for itself in `.claude/settings.json`
(through `$CLAUDE_PROJECT_DIR`, so the same file works in any checkout);
it does nothing where `pare` is not on PATH. To have it run the code being
changed, point `PARE_BIN` at the checkout's build (`target/release/pare`)
or rerun `cargo install --path .` after a change.

Other notes from the hooks reference: a hook that times out does not block
the call (the command then runs unwrapped); the Bash tool's own timeout
still applies to the wrapped command, and when it fires pare forwards the
signal to the command and still prints a summary of what it saw.

### Settings

| variable | default | |
|---|---|---|
| `PARE_BIN` | `pare` | the pare executable |
| `PARE_HOOK_BUDGET` | `2000` | `--budget` for rewritten commands (the hook always passes one, so `$PARE_BUDGET` does not change hook-wrapped commands; it applies where pare is called without `--budget`) |
| `PARE_HOOK_DECISION` | unset | `ask` or `allow`; see above |
| `PARE_HOOK_EXTRA` | unset | an extra regex of commands to wrap, matched against the command without its path, e.g. `^(just|nx) ` |
| `PARE_HOOK_DISABLE` | unset | any value turns the hook off |

### What it saved

Command mode records, next to each run file, how many bytes the command
printed and how many pare printed. `pare --stats` totals them:

```
$ pare --stats
# pare --stats: 14 command runs in /root/.cache/pare/runs
raw ~212,480 tok → printed ~18,233 tok · saved ~194,247 tok (91.4 %)
3 runs fit the budget and passed through whole
## largest
...
```

The saving is an upper bound on what the agent avoided reading. Without
pare it might have cut the output itself (`| tail`, `| grep`, or the
harness truncating a long result), and with pare it may go back to the run
file for more (`--only`, `--since`). A run that fits the budget is printed
as it is and costs nothing extra (a failing one adds `## exit N`). Runs from before
pare recorded its output size are skipped.

### Run files do not pile up

Every wrapped command leaves a run file, so with the hook on they add up.
pare prunes them itself, at most once an hour: run files older than 14
days, then the oldest until the rest fit in 500 MB, but never one less
than a day old, since a summary in the current session may point at it.
`PARE_RUN_KEEP_DAYS`, `PARE_RUN_MAX_MB` (0 for no limit) and
`PARE_RUN_PRUNE=off` change that; `pare --prune --dry-run` shows what a
sweep would remove. A run file worth keeping as a held-out case should be
copied with `bench/collect.py add` before it ages out.
