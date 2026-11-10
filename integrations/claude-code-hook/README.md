# pare hook for Claude Code

A PreToolUse hook that routes noisy Bash commands through
[pare](https://github.com/wspires/pare). When the agent runs a single
simple command that usually prints a lot (pytest, npm test, cargo
build, go test, make, Maven, Gradle, terraform plan, kubectl logs and
others), the hook rewrites it to `pare -- <command>`, so the agent sees
a summary that fits a token budget. The full output stays in a run file
the summary names.

It leaves compound commands, watch modes, background commands and
anything not on its list alone. It never fails closed: on any error,
or when `pare` is not installed, the command runs unchanged.

This is a separate plugin from `pare`, the skill that tells the agent
how to read a summary and expand a run file. Anthropic's plugin
directory accepts hooks and scripts only from verified organizations,
so the skill is listed there and the hook is installed from this
repository's marketplace. Install both:

```
claude plugin marketplace add wspires/pare
claude plugin install pare@pare
claude plugin install pare-hook@pare
```

## Requirements

`pare` on the PATH that Claude Code sees (the repository's README,
"Install"), and `python3`.

## Settings

| Variable | Effect |
|---|---|
| `PARE_BIN` | Path to the pare executable (default: `pare` on PATH). |
| `PARE_HOOK_BUDGET` | `--budget` for rewritten commands: a number, `auto` or `auto:N`. |
| `PARE_HOOK_DECISION` | Unset, `ask` or `allow`: the permission decision sent with a rewrite. |
| `PARE_HOOK_EXTRA` | An extra regex of commands to wrap. |
| `PARE_HOOK_DISABLE` | Set it to turn the hook off without uninstalling. |

By default no permission decision is sent, so an allow rule written for
`npm test` does not match the rewritten `pare -- npm test`. Read
"Permissions" in the
[integrations guide](https://github.com/wspires/pare/blob/main/integrations/README.md)
before choosing `allow`.

Check what the hook would do with a command:

```
python3 pare-hook.py --explain "npm test -- --runInBand"
```

## License

MIT or Apache-2.0, at your option, the same as pare.
