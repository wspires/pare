# Changelog

## 0.2.0 (2026-10-31)

pare is now a single native binary, written in Rust, and installable with
Homebrew, an install script, pipx/uv (`pare-cli`) or cargo (`pare-cli`).

- **A token is 2.0 bytes, not 2.8.** Measured through Anthropic's
  token-count API, the current Claude models tokenize pare's output at
  1.96 bytes per token, against 2.82 for OpenAI's cl100k and o200k, the
  rate the old default came from. So `--budget 2000` printed about
  2,870 Claude tokens. The default `--bytes-per-token` is now 2.0: every
  summary at a given budget is 30 % smaller, the token counts in the
  header are Claude's, and `--bytes-per-token 2.8` gives the old
  behaviour. A budget of 600 is now too small for a run with several
  failures; 1,000 to 2,000 is the working range (README, "Results").
  Renumbers nothing: template ids do not depend on the budget. The
  Python reference and the golden files changed with it.

- **Same output as before, with its own rules switched off.** Under
  `PARE_REFERENCE_RULES=1` (for testing) it prints exactly what the
  Python version did, byte for byte, on every log in the repository
  under every flag set tested, with the same exit statuses.
  What the switch turns off is listed at the end of this section.
  Template ids are the same, so `pare --only T3 < run-file` works on run
  files saved by either version.
- **The tail window keeps the verdict.** It skips lines of shapes seen
  five or more times, so that chatter does not fill it; now an error line
  of such a shape stays when it is the only one of its shape near the
  end. pytest's own test suite prints dozens of inner-run `FAILED` lines,
  and the one real `FAILED` line at the end was folded away.
- **Faster and smaller.** A 30 MB log takes about 5 s instead of about
  30 s, in less memory; start-up is about 50 ms.
- **Follow mode no longer needs `select()`**, which limited the Python
  version to Unix; it builds and should run on Windows, where it has not
  been tried yet.
- **Structured logs.** A JSON line (slog, pino, zap, bunyan, structlog)
  or a logfmt line longer than `--width` keeps its start and then the
  fields that say what went wrong (`err`, `exception`, `panic`, `stack`,
  `msg`, ...), where the plain cut dropped them; an escaped stack trace in
  such a field shows as its message and nearest frame, decoded. pino's
  numeric levels (`"level":50`) count as levels. Lines are recognized one
  by one; other lines are unchanged.
- **The budget follows the log (`auto`), by default.** The budget is a
  ceiling (2,000, or N with `--budget auto:N`) that the summary stops
  short of once what is left is below error level, so a log with one
  error gets a short summary. On four fresh held-out samples it kept
  what a fixed 2,000 kept, at 69 to 79 % of the tokens. Output up to
  twice the ceiling (4,000 tokens) passes through whole: on the agent
  runs, a summary of a log that size cost the agent a round trip to
  expand more often than it saved tokens. A number
  (`--budget 2000`) is a fixed budget the summary fills, as before; the
  Python version's default was a fixed 2,000.
- **`$PARE_BUDGET`** sets the budget when `--budget` is not given (an
  empty value counts as unset; `auto` and `auto:N` work too), so an agent
  harness can set it once.
- **The Claude Code hook** rewrites to `pare -- CMD`, so pare's default
  (or `$PARE_BUDGET`) applies; `PARE_HOOK_BUDGET` still sets one.
- **Continuation rules in the vocabulary file.** A `[continuation]`
  section adds regexes for lines that continue the record above (a Go
  panic's frames, a custom tool's detail lines), so a trace stays one
  record; the built-in rules are now that class's default entries, shown
  by `--dump-vocab`, and `[continuation replace]` can drop them.
- **Masks in the vocabulary file.** A `[mask NAME]` section of regexes
  turns what they match into `<NAME>` before lines are clustered, so
  site-specific ids (order numbers, tenant names, build hashes) do not
  split one kind of line into many templates. They run before the
  built-in masks; there are none by default.
- **The command line, before the first release.** `pare prune`,
  `pare stats` and `pare vocab` are subcommands (they were `--prune`,
  `--stats` and `--dump-vocab`); `--bytes-per-token` is the name of
  `--chars-per-token`, which stays as an alias; `--ascii` writes pare's
  own marks (`→ · × – ┆ … ≤ ↳`) as ASCII, leaving log text alone; follow
  mode's `--interval` defaults to 30 seconds instead of off; `--help`
  groups the flags and lists the environment variables and exit
  statuses. A usage error exits 2 (it was 1, the Python version's; an
  I/O error still is), a budget below 1, a width below 10 or a negative
  count is refused up front, `--no-tee` with `--run-file` is refused
  instead of ignored, the "expand with" hint is printed only when there
  is a run file to expand, and the ledger and gap markers say
  `1 template` and `1 line`.
- **Binary input is two lines, not a summary.** A NUL byte in the first
  8 KiB (an executable, an image, an archive piped in by mistake) used
  to come out as a budget's worth of NUL bytes and fragments marked as
  errors. pare now prints `# pare: binary input (NUL at byte N), … B ·
  not summarised` and the run-file line, saves the input as usual, and
  exits as usual.
- **Large inputs cost no disk.** A file on standard input (`pare <
  build.log`) is read in place instead of copied: the run line names the
  file, expanding reads it, and the run directory gets only the `.json`
  record (Linux and macOS, when the file is read from its start). A
  piped input larger than `$PARE_RUN_MAX_MB` is summarised but not
  kept, and the run line says so; `--run-file` keeps it regardless.
- **License:** MIT OR Apache-2.0.
- The Python version is kept in `reference/` as the reference the Rust
  version is tested against; it is no longer packaged.

Differences from the Python version: a follow run file is named by the
time in UTC (it was local time); a malformed regex in a `--vocab` file is
reported in the Rust regex engine's words; long JSON and logfmt lines are
clipped as above, and pino's numeric levels count; the tail window keeps
a lone verdict line of a common shape; binary input is two lines; a
usage error exits 2; the subcommands, `--ascii`, `--bytes-per-token` and
the 30-second `--interval` default are as above; and `$PARE_BUDGET`,
`--budget auto` (the default, where the Python version's was a fixed
2,000), `pare vocab`'s continuation section and the `[continuation]` and
`[mask NAME]` sections are new.

## 0.1 (`v0.1-python`)

The Python prototype: a single file, `reference/pare.py`.
