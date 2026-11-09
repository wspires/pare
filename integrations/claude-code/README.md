# pare plugin for Claude Code

This plugin teaches Claude Code to use
[pare](https://github.com/wspires/pare), a Unix filter that summarizes
command output (test runs, builds, logs) to fit a token budget. The
agent sees a short summary instead of thousands of lines, and nothing is
lost: the full output is saved to a run file that the summary names and
the agent can expand.

## What it adds

A skill (`/pare:pare`) that tells the agent to run commands whose output
can be long as `pare -- <command>`, how to read the summary, and how to
get omitted lines back from the run file with `--only` and
`--since`/`--until`. The plugin has no hooks, scripts or programs: it is
instructions only.

## Requirements

The plugin does not include pare itself. Install it first, so `pare` is
on the PATH that Claude Code sees:

```
brew install wspires/tap/pare                    # macOS and Linux
pipx install pare-cli                            # or: uv tool install pare-cli
cargo binstall pare-cli                          # or: cargo install pare-cli
```

## Install

```
claude plugin marketplace add wspires/pare
claude plugin install pare@pare
```

Or, inside a session, `/plugin marketplace add wspires/pare` and then
`/plugin install pare@pare`.

## Automatic rewriting

With the skill alone the agent decides when to use pare. For every
listed command (pytest, npm test, cargo build, go test, make, terraform
plan and others) to go through pare without the agent choosing, also
install the `pare-hook` plugin from the same marketplace:

```
claude plugin install pare-hook@pare
```

It is a separate plugin because Anthropic's plugin directory accepts
hooks and scripts only from verified organizations
(`integrations/claude-code-hook/README.md` in the repository).

## License

MIT or Apache-2.0, at your option, the same as pare.
