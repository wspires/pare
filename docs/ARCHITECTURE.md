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
| 2 | masking and Drain | |
| 3 | vocabulary, severity, scoring | |
| 4 | summary and allocation | |
| 5 | selection flags, command mode, follow mode, prune, stats | |
| 6 | distribution; Python moves to `reference/` | |

What is not ported exits with status 70 and a message saying so
(`Error::NotPorted`); `bench/parity.py --skip-unported` counts those runs
as skipped. After stage 1: input that fits the budget is identical to the
Python version on all 170 of the 2,496 parity runs where that applies,
including every generated edge case that fits (CRLF, ANSI codes,
carriage-return redraws, invalid UTF-8, NUL bytes, letters that case-fold
to ASCII); the other 2,326 are not ported yet.

## Crate layout

One package with a library and a binary:

```
Cargo.toml          dependencies, lints, release profile
src/main.rs         the binary: hands the arguments to cli::run, nothing else
src/lib.rs          the library root: module list and crate docs
src/cli.rs          options (every Python flag), checks, dispatch, batch mode
src/error.rs        Error (Usage, Io, NotPorted) and exit statuses
src/budget.rs       Budget: tokens <-> bytes, as tokens()/budget_bytes_for()
src/pass.rs         Pass: chunks in, lines out, spool and hash on the way
src/store.rs        Store: per-line offsets and sizes; reads lines back
src/text.rs         clean_line, py_isspace: a raw line to its text
src/runfile.rs      run directory, Spool (the file that becomes the run file)
src/summary.rs      output; so far the verbatim pass-through
tests/cli.rs        end-to-end tests of the binary
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
               clean_line(raw) ──▶ Store::push(raw_len, clean_len)
                                        (offset, length; no text kept)
end of input ─▶ Pass::finish ─▶ Store + hash + spool
                         │
     spool renamed to <run dir>/<hash12>.log (Spool::persist), unless --no-tee
                         │
                         ▼
               summary::passthrough: fits the budget?
                  yes ─▶ read lines back by offset, clean, write
                  no  ─▶ Error::NotPorted (stage 4 will summarise)
```

This is the Python version's single pass, in the same order, so the later
stages slot in where the Python code has them: segmentation, masking,
Drain and scoring happen per line inside `Pass` (stages 2 and 3), and the
allocation in `summary` (stage 4).

## Memory model

The input's text is never held in memory. The `Store` keeps, per line, a
`u64` offset into the spool and a `u32` length (12 bytes a line now;
later stages add a column each for record, depth, severity and score, as
the Python version keeps one typed array each). A line's text is read
back from the spool when the output needs it. Peak memory is the chunk
buffer (1 MiB), the per-line columns, and a few small buffers that are
reused instead of reallocated.

## Module map: Python to Rust

| Python (`proto/pare.py`) | Rust |
|---|---|
| `main()` options and checks | `cli::Args`, `cli::dispatch` |
| `main()` batch path | `cli::batch` |
| `sys.exit("pare: ...")` | `Error::Usage`, printed by `cli::run` |
| `tokens()`, `budget_bytes_for()` | `Budget::tokens_in`, `Budget::bytes` |
| `analyze()` | `pass::read_all` |
| `Pass.feed()`, `Pass.finish()` | `Pass::feed`, `Pass::finish` |
| `Pass._add()` (so far: offsets, verbatim size) | `pass::add_line` |
| `Store` offsets, `Store.raw()` | `Store`, `Store::for_each_raw` |
| `clean_line()`, `ANSI_RE` | `text::clean_line`, `text::ANSI_RE` |
| `str.rstrip()` / `isspace()` | `text::py_isspace` |
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
  the run file and its name, empty input, usage errors, status 70.
- **Parity** (`bench/parity.py --candidate target/release/pare
  --skip-unported`) compares the binary with the Python version byte for
  byte over every corpus log and the generated edge cases, under 13 flag
  sets. From stage 2 on, `--dump` also compares the per-line results.
- **CI** (`.github/workflows/ci.yml`, Linux): `cargo fmt --check`, `cargo
  clippy -D warnings`, `cargo test`, `cargo doc` with warnings as errors,
  the Python test suite, and parity.

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
| `regex` | linear-time regular expressions (RE2-style); the ANSI pattern now, the vocabulary and masks from stage 2 |
| `memchr` | SIMD byte search for line splitting; already a dependency of `regex`, so free |
| `sha2` | SHA-256 for the run file's name, as `hashlib.sha256` |

Stage 3 may add `fancy-regex` for the patterns that need lookbehind or
lookahead, as a first step; see ROADMAP.md.
