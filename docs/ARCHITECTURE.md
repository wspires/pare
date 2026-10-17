# pare: architecture of the Rust port

How the Rust code is organised, how data flows through it, and how it
maps onto the Python reference (`proto/pare.py`). What the algorithm does
and why is in [DESIGN.md](DESIGN.md); what is Rust-specific, explained for
a C++ reader, is in [RUST.md](RUST.md). The port's plan and its stages are
in [ROADMAP.md](../ROADMAP.md), stage 5; setting up, building and
testing are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Status

| stage | what | state |
|---|---|---|
| 0 | parity tooling (`bench/parity.py`, `$PARE_DUMP`) | done |
| 1 | crate, options, input, run file, pass-through, CI, release builds | done |
| 2 | segmentation, masking, Drain: template ids | done |
| 3 | vocabulary, severity, scoring | |
| 4 | summary and allocation | |
| 5 | selection flags, command mode, follow mode, prune, stats | |
| 6 | distribution; Python moves to `reference/` | |

What is not ported exits with status 70 and a message saying so
(`Error::NotPorted`); `bench/parity.py --skip-unported` skips those runs'
output, and `--dump` still compares the pass's per-line results, with the
fields the port does not compute yet written as `?`.

After stage 2, on 2,522 parity runs (194 inputs, 13 flag sets): input
that fits the budget passes through identically (177 runs), and on all
1,351 batch runs every line's record, depth, blank flag, template,
rendered size and stripped length, and every template's counts and text,
are identical to the Python version's. That includes the generated edge
cases (CRLF, ANSI codes, carriage returns, invalid UTF-8, NUL bytes,
case-folding letters, deep indentation, and characters on which Python's
and Rust's Unicode classes disagree). Severity and scores (stage 3) are
the `?` fields.

## Crate layout

One package with a library and a binary:

```
Cargo.toml          dependencies, lints, release profile
src/main.rs         the binary: hands the arguments to cli::run, nothing else
src/lib.rs          the library root: module list and crate docs
src/cli.rs          options (every Python flag), checks, dispatch, batch mode
src/error.rs        Error (Usage, Io, NotPorted) and exit statuses
src/budget.rs       Budget: tokens <-> bytes, as tokens()/budget_bytes_for()
src/pass.rs         Pass: chunks in, lines out, spool and hash on the way;
                    Segmenter: lines into records, heads into templates
src/store.rs        Store: per-line, per-record and per-template columns
src/text.rs         clean_line, indent_of, clip: a raw line to its text
src/mask.rs         the masks (<TS>, <NUM>, ...) and drain_message
src/drain.rs        Drain, the template miner (as drain3 0.9.11)
src/pyclass.rs      Python's \w, \d, \s, isdigit, strip, split; py_pattern
src/pyunicode.rs    the tables behind them, generated from Python 3.13
src/dump.rs         $PARE_DUMP, in the Python version's format
src/runfile.rs      run directory, Spool (the file that becomes the run file)
src/summary.rs      output; so far the verbatim pass-through
tests/cli.rs        end-to-end tests of the binary
tests/data/mask.tsv Python's mask() on 4,045 real record heads (golden file)
```

The library holds everything so that each piece has unit tests next to it
and documentation as an API (`cargo doc --open`); `main.rs` is a few
lines.

## Data flow (batch mode)

```
stdin ──read 1 MiB──▶ Pass::feed ──▶ spool file (write as read)
                         │       └──▶ SHA-256 (hash as read)
                         ▼
               split on '\n' (memchr), a line cut by a chunk carried over
                         │
                         ▼
               Segmenter::add_line
                 clean_line(raw); offset, length, rendered and stripped sizes
                 blank?           ─▶ ends the record (one blank may stay in a block)
                 continuation?    ─▶ joins the open record: deeper indent, or a
                                     frame/caret/`Caused by:`/... shape (CONTINUATION),
                                     within one logcat entry (LOGCAT_HEADER)
                 otherwise        ─▶ starts a record: drain_message(text)
                                     = mask(text) ─▶ Drain::add ─▶ template
end of input ─▶ Pass::finish ─▶ Store + hash + spool
                 (each template takes its cluster's final text)
                         │
     spool renamed to <run dir>/<hash12>.log (Spool::persist), unless --no-tee
                         │
                         ▼
               $PARE_DUMP set? ─▶ dump::write_dump
                         │
               summary::passthrough: fits the budget?
                  yes ─▶ read lines back by offset, clean, write
                  no  ─▶ Error::NotPorted (stage 4 will summarise)
```

This is the Python version's single pass, in the same order, so the later
stages slot in where the Python code has them: severity and scoring per
line in `Segmenter::add_line` (stage 3), the allocation in `summary`
(stage 4).

## Matching Python's character classes and regexes

Two things had to be exactly Python's for template ids to match:

- **Character classes.** Python's `re` and Rust's `regex` disagree on
  `\w` for about 8,000 characters (Rust counts combining marks and
  connector punctuation, Python counts superscripts and other numbers),
  on `\d` for Unicode 16's new digits, and on whitespace for U+001C to
  U+001F. `bench/gen_pyunicode.py` writes Python 3.13's classes into
  `src/pyunicode.rs` as range tables; `pyclass::py_pattern` rewrites a
  pattern's `\w`, `\d`, `\s` and `\b` to use them, and `py_isdigit`,
  `py_isspace`, `py_strip` and `py_split` look them up. CI checks the file
  is current. In `mask("x\u{301}5")` the 5 is a number to Python and not
  to Rust's built-in `\w`; a unit test pins it.
- **Lookaround.** Python's masks use `\b`, lookahead and lookbehind,
  which the linear-time `regex` crate does not have and the backtracking
  `fancy-regex` crate does. Scanning a line with `fancy-regex` made the
  pass slower than Python's (25 s on 30 MB). So each mask has two
  patterns (`mask::SPECS`): Python's, and the same with its assertions
  erased. `regex` scans for the leftmost place the relaxed pattern
  matches; `fancy-regex` tries Python's pattern only there, anchored with
  `\G` (it still sees the text before, for lookbehind). Erasing
  assertions only relaxes a pattern, so every place Python's can match is
  a candidate and none is missed; a test checks that on corpus lines, and
  a golden file of Python's results checks the outcome. Masking became 14
  times faster, the pass 12 times.

The segmentation patterns (`CONTINUATION`, `LOGCAT_HEADER`) have no
lookaround and run on `regex` alone, with Python's classes.

## Memory model

The input's text is never held in memory. The `Store` keeps, per line, a
`u64` offset into the spool, a `u32` length, its record (`Option<u32>`),
depth, rendered and stripped sizes (`u32`) and a blank flag: about 33
bytes a line; per record its first and last line and template (12 bytes);
and the templates. Stage 3 adds severity and score. A line's text is read
back from the spool when the output needs it. Drain keeps its clusters'
tokens and its prefix tree, which grow with the number of distinct
shapes, not with the input. On a 30 MB log the process peaks at 31 MB
(the Python version: 65 MB).

## Module map: Python to Rust

| Python (`proto/pare.py`) | Rust |
|---|---|
| `main()` options and checks | `cli::Args`, `cli::dispatch` |
| `main()` batch path | `cli::batch` |
| `sys.exit("pare: ...")` | `Error::Usage`, printed by `cli::run` |
| `tokens()`, `budget_bytes_for()` | `Budget::tokens_in`, `Budget::bytes` |
| `analyze()` | `pass::read_all` |
| `Pass.feed()`, `Pass.finish()` | `Pass::feed`, `Pass::finish` |
| `Pass._add()` (so far without severity and scores) | `pass::Segmenter::add_line` |
| `Pass._joins()`, `continuation_rx()`, `LOGCAT_HEADER_RX` | `Segmenter::joins`, `pass::CONTINUATION`, `pass::LOGCAT_HEADER` |
| `Pass.cut()` (template texts and totals) | `Segmenter::finish` |
| `Store` arrays, `Store.raw()` | `Store` columns, `Store::for_each_raw` |
| `Template` | `store::Template` |
| `clean_line()`, `ANSI_RE`, `indent_of()`, `clip()` | `text::clean_line`, `text::ANSI_RE`, `text::indent_of`, `text::clip` |
| `MASKS`, `MASK_NEEDS`, `mask()`, `drain_message()` | `mask::SPECS`, `mask::mask`, `mask::drain_message` |
| `Drain` | `drain::Drain` |
| `str.isspace()`, `.isdigit()`, `.strip()`, `.split()`, re's `\w` `\d` `\s` `\b` | `pyclass::py_isspace`, `py_isdigit`, `py_strip`, `py_split`, `py_pattern` |
| `write_dump()` | `dump::write_dump` |
| `default_run_dir()` | `runfile::default_run_dir` |
| spool `.pare-<pid>.tmp`, `os.replace`, cleanup in `finally` | `runfile::Spool` (`persist`, `Drop`) |
| `summarize()` pass-through branch | `summary::passthrough` |

## Errors and exit statuses

Failures are values (`error::Error`), passed up with `?` and reported in
one place, `cli::run`, which prints `pare: ...` on stderr and returns the
status:

| status | meaning |
|---|---|
| 0 | success (also `--help`, `--version`, and a reader that closed the pipe) |
| 1 | a usage error or an I/O error; messages match the Python version's |
| 2 | the arguments could not be parsed (clap's message) |
| 70 | not ported yet |

## Testing

- **Unit tests** sit at the bottom of each module (`#[cfg(test)] mod
  tests`), and code examples in `///` docs are compiled and run as tests.
- **Integration tests** in `tests/cli.rs` run the binary: pass-through,
  the run file and its name, empty input, usage errors, status 70, the
  dump's format.
- **Golden file.** `tests/data/mask.tsv` holds Python's `mask()` on 4,045
  real record heads and awkward Unicode lines (`bench/gen_fixtures.py`
  writes it); a unit test checks the Rust masks against every row.
- **Parity** (`bench/parity.py --candidate target/release/pare
  --skip-unported`) compares the binary with the Python version byte for
  byte over every corpus log and the generated edge cases, under 13 flag
  sets; `--dump` also compares the per-line results and reports the first
  row that differs.
- **CI** (`.github/workflows/ci.yml`, Linux): `cargo fmt --check`, `cargo
  clippy -D warnings`, `cargo test`, `cargo doc` with warnings as errors,
  the Python test suite on Python 3.13, that `src/pyunicode.rs` is what
  `bench/gen_pyunicode.py` writes, and parity with `--dump`.

## Builds and releases

`cargo build --release` gives `target/release/pare`, about 2.3 MB,
stripped, with link-time optimisation. `.github/workflows/release.yml`
builds on each version tag (`v0.2.0`, not `v0.1-python`) and attaches the
archives and their SHA-256 sums to a GitHub release:

- `x86_64-unknown-linux-musl`: statically linked, runs on any x86-64 Linux;
- `aarch64-apple-darwin` and `x86_64-apple-darwin`;
- `x86_64-pc-windows-msvc`.

It can be run by hand (Actions, "Release", "Run workflow") to build the
archives without publishing. ARM Linux is not built yet.

## Dependencies

Each one earns its place; the binary has no C dependencies, which is what
makes the static build and cross-compilation trivial.

| crate | why |
|---|---|
| `clap` (derive, env) | the standard argument parser; derives the parser and help from the `Args` struct, accepts unambiguous prefixes like argparse, reads `$PARE_VOCAB` |
| `regex` | linear-time regular expressions (RE2-style): the ANSI pattern, segmentation, and the scanning half of every mask |
| `fancy-regex` | backtracking regular expressions with lookaround and `\G`, like Python's `re`: the exact half of the masks, tried only at the positions `regex` finds |
| `memchr` | SIMD byte search for line splitting; already a dependency of `regex`, so free |
| `sha2` | SHA-256 for the run file's name, as `hashlib.sha256` |
