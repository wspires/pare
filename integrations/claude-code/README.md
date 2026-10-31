# pare plugin for Claude Code

This plugin routes noisy Bash commands through
[pare](https://github.com/wspires/pare), a Unix filter that summarizes
command output (test runs, builds, logs) to fit a token budget. The agent
sees a short summary instead of thousands of lines, and nothing is lost:
the full output is saved to a run file that the summary names and the
agent can expand.

## What it adds

- **A PreToolUse hook on Bash** (`pare-hook.py`). When the agent runs a
  single simple command that usually prints a lot (pytest, npm test,
  cargo build, go test, make, Maven, Gradle, terraform plan, kubectl
  logs and others), the hook rewrites it to `pare -- <command>`. It
  leaves compound commands, watch modes, background commands and
  anything not on its list alone, and does nothing if `pare` is not
  installed.
- **A skill** (`/pare:pare`) that tells the agent how to run commands
  through pare, read a summary, and expand a run file.

The hook never fails closed: on any error the command runs unchanged.

## Requirements

The plugin does not include pare itself. Install it first, so `pare` is
on the PATH that Claude Code sees:

```
brew install wspires/tap/pare                    # macOS and Linux
pipx install pare-cli                            # or: uv tool install pare-cli
cargo binstall pare-cli                          # or: cargo install pare-cli
```

The hook also needs `python3`.

## Install

```
claude plugin marketplace add wspires/pare
claude plugin install pare@pare
```

Or, inside a session, `/plugin marketplace add wspires/pare` and then
`/plugin install pare@pare`.

## Settings

The hook reads these environment variables:

| Variable | Effect |
|---|---|
| `PARE_BIN` | Path to the pare executable (default: `pare` on PATH). |
| `PARE_HOOK_BUDGET` | `--budget` for rewritten commands: a number, `auto` or `auto:N`. |
| `PARE_HOOK_DECISION` | Unset, `ask` or `allow`: the permission decision sent with a rewrite. |
| `PARE_HOOK_EXTRA` | An extra regex of commands to wrap. |
| `PARE_HOOK_DISABLE` | Set it to turn the hook off without uninstalling. |

Permissions matter here: by default no decision is sent, so an allow
rule written for `npm test` does not match the rewritten
`pare -- npm test`. Read "Permissions" in the repository's
[integrations guide](https://github.com/wspires/pare/blob/main/integrations/README.md)
before choosing `allow`.

Check what the hook would do with a command:

```
python3 pare-hook.py --explain "npm test -- --runInBand"
```

## License

MIT or Apache-2.0, at your option, the same as pare.
