# Changelog

## 0.2.0

pare is now a single native binary, written in Rust, and installable with
Homebrew, an install script, pipx/uv (`pare-cli`) or cargo (`pare-cli`).

- **Same output as before, apart from two new rules.** With the Rust
  version's own rules switched off (`PARE_REFERENCE_RULES=1`, for testing)
  it prints exactly what the Python version did, byte for byte, on every
  log in the repository under every flag set tested (3,304 runs), with the
  same exit statuses. The two rules are the structured-log clip below and
  a tail window that keeps a lone verdict line. Template ids are the
  same, so `pare --only T3 < run-file` works on run files saved by either
  version.
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
- **`--budget auto`.** The budget becomes a ceiling (2,000, or N with
  `auto:N`) that the summary stops short of once what is left is below
  error level, so a log with one error gets a short summary. On the
  benchmark: 97.4 % of must-survive lines at a mean of 1,204 tokens,
  against 98.8 % at 1,552 for `--budget 2000`. Opt-in.
- **`$PARE_BUDGET`** sets the budget when `--budget` is not given (an
  empty value counts as unset; `auto` and `auto:N` work too), so an agent
  harness can set it once; the default is still 2,000.
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
- **License:** MIT OR Apache-2.0.
- The Python version is kept in `reference/` as the reference the Rust
  version is tested against; it is no longer packaged.

Differences from the Python version: a follow run file is named by the
time in UTC (it was local time); a malformed regex in a `--vocab` file is
reported in the Rust regex engine's words; long JSON and logfmt lines are
clipped as above, and pino's numeric levels count; the tail window keeps
a lone verdict line of a common shape; and `$PARE_BUDGET`,
`--budget auto`, `--dump-vocab`'s continuation section and the
`[continuation]` and `[mask NAME]` sections are new.

## 0.1 (`v0.1-python`)

The Python prototype: a single file, `reference/pare.py`.
