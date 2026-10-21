# Changelog

## Unreleased

- **`--budget auto`.** The budget becomes a ceiling (2,000, or N with
  `auto:N`) that the summary stops short of once what is left is below
  error level, so a log with one error gets a short summary. On the
  benchmark: 97.4 % of must-survive lines at a mean of 1,204 tokens,
  against 98.8 % at 1,552 for `--budget 2000`. `$PARE_BUDGET=auto` works
  too. Opt-in; output without it is unchanged.
- **Continuation rules in the vocabulary file.** A `[continuation]`
  section adds regexes for lines that continue the record above (a Go
  panic's frames, a custom tool's detail lines), so a trace stays one
  record; the built-in rules are now that class's default entries, shown
  by `--dump-vocab`, and `[continuation replace]` can drop them. Output is
  unchanged unless a file uses the class.
- **Masks in the vocabulary file.** A `[mask NAME]` section of regexes
  turns what they match into `<NAME>` before lines are clustered, so
  site-specific ids (order numbers, tenant names, build hashes) do not
  split one kind of line into many templates. They run before the
  built-in masks; there are none by default, so output is unchanged.

## 0.2.0

pare is now a single native binary, written in Rust, and installable with
Homebrew, an install script, pipx/uv (`pare-cli`) or cargo (`pare-cli`).

- **Same output as before.** The Rust version prints exactly what the
  Python version did, byte for byte, on every log in the repository under
  every flag set tested (2,730 runs), with the same exit statuses; template
  ids are the same, so `pare --only T3 < run-file` works on run files
  saved by either version.
- **Faster and smaller.** A 30 MB log takes about 5 s instead of about
  30 s, in less memory; start-up is about 50 ms.
- **Follow mode no longer needs `select()`**, which limited the Python
  version to Unix; it builds and should run on Windows, where it has not
  been tried yet.
- **`$PARE_BUDGET`** sets the budget when `--budget` is not given (an
  empty value counts as unset), so an agent harness can set it once; the
  default is still 2,000.
- **License:** MIT OR Apache-2.0.
- The Python version is kept in `reference/` as the reference the Rust
  version is tested against; it is no longer packaged.

Differences: a follow run file is named by the time in UTC (it was local
time), a malformed regex in a `--vocab` file is reported in the Rust
regex engine's words, and the Python version does not read `$PARE_BUDGET`.

## 0.1 (`v0.1-python`)

The Python prototype: a single file, `reference/pare.py`.
