# Contributing to pare

pare has two implementations: the Rust version in `src/`, which is what
is released, and the Python prototype in `reference/pare.py`, frozen as
the reference the Rust one is tested against byte for byte. This page
covers building and testing both, how to change pare's behaviour without
breaking that check, the benchmark, how to submit a change, and the
repository layout. How the Rust code is organised is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the Rust-specific parts,
explained for a C++ reader, are in [docs/RUST.md](docs/RUST.md); what
pare does and why is in [docs/DESIGN.md](docs/DESIGN.md).

## Rust

### Set up once

1. **Install Rust with rustup**, the official installer and version
   manager. It installs into your home directory (`~/.rustup`,
   `~/.cargo`), needs no `sudo`, and adds `~/.cargo/bin` to your PATH:

   ```
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```

   On Windows, use the installer from <https://rustup.rs>. Accept the
   defaults, then open a new terminal (or run `. "$HOME/.cargo/env"` in
   the current one) and check:

   ```
   rustup show        # host and active toolchain, e.g. aarch64-apple-darwin, stable
   cargo --version
   ```

   This gives you `cargo` (build tool and package manager), `rustc`,
   `rustfmt` and `clippy`. `rustup update` upgrades them;
   `rustup self uninstall` removes everything.

2. **A system linker**, as for C or C++:
   - macOS: Xcode, or the Command Line Tools (`xcode-select --install`).
     `clang --version` should run without errors.
   - Linux: `gcc` or `clang` (Debian/Ubuntu: `build-essential`).
   - Windows: the MSVC Build Tools, which the rustup installer offers.

Notes:

- **Use rustup, not a package manager's Rust.** Homebrew's `rustup` and
  `rust` formulas pull in LLVM and other dependencies, and on a machine
  Homebrew no longer has prebuilt packages for they compile it all from
  source. rustup downloads prebuilt toolchains for every supported
  platform.
- **On Apple Silicon, install from a native shell.** rustup installs the
  toolchain for the architecture the shell runs as. `uname -m` should say
  `arm64`, and `rustup show` should list `aarch64-apple-darwin`. A
  terminal set to "Open using Rosetta", or an Intel Homebrew carried over
  from an older Mac, can make it `x86_64` instead.
- **Version.** The crate needs Rust 1.87 or newer (`rust-version` in
  Cargo.toml). CI uses the latest stable, so a new clippy lint shows up
  there and on an up-to-date machine at the same time.
- **Editor.** rust-analyzer (the VS Code extension, or JetBrains
  RustRover) shows inferred types inline and jumps into library source;
  it is the quickest way to learn what the code is doing.

### Build and run

From the repository root:

```
cargo build                    # debug build    → target/debug/pare
cargo build --release          # optimised, LTO → target/release/pare

printf 'hello\nworld\n' | target/release/pare --no-tee
target/release/pare --no-tee < corpus/cargo-build.log
```

The first build downloads the dependencies (about 55 crates) from
crates.io; later builds only recompile what changed.

`cargo run` builds and runs in one step. The first `--` separates cargo's
arguments from pare's, and pare's own `--` (command mode) comes after
that:

```
cargo run --release -- --no-tee --budget 2000 < corpus/cargo-build.log
cargo run --release -- --budget 2000 -- make test     # command mode
```

Without `--no-tee`, pare saves its input as a run file in
`$PARE_RUN_DIR`, else `~/.cache/pare/runs`.

**Installing it on your PATH.** `cargo install --path .` builds a release
binary from the checkout into `~/.cargo/bin/pare` (`cargo uninstall
pare-cli` removes it). The released versions install with Homebrew, the
install script, pipx or cargo (README, "Install"). An older Python `pare`
in `~/.local/bin` is shadowed if `~/.cargo/bin` comes first on your PATH;
`pipx uninstall pare` or deleting that file removes it. The Claude Code
hook in `integrations/` finds `pare` on PATH, or at `$PARE_BIN`
(`integrations/README.md`).

### Test

```
cargo test                     # everything: unit tests, tests/cli.rs, doc examples
cargo test text::              # only the unit tests of one module (src/text.rs)
cargo test --test cli          # only the end-to-end tests in tests/cli.rs
cargo test --doc               # only the examples in /// doc comments
cargo test -- --nocapture      # show println! output from tests
```

Unit tests sit at the bottom of each module (`#[cfg(test)] mod tests`),
some against golden files in `tests/data/` written by the Python version
(masks, severity, scores, `pare vocab`; "Generated files" below);
`tests/cli.rs` runs the built binary end to end, including whole
summaries of five corpus logs against golden files; examples in `///`
docs are compiled and run, so the docs cannot drift from the code.

### The checks CI runs

`.github/workflows/ci.yml` runs four jobs on every pull request and
every push to main, and a pull request needs all four green. Locally:

```
cargo fmt --check                              # or `cargo fmt` to format in place
cargo clippy --all-targets -- -D warnings      # lints; warnings are errors
cargo test
RUSTDOCFLAGS="-D warnings" cargo doc --no-deps
```

are the Rust job; the others are the Python reference's tests, parity
and packaging (below). `cargo doc --no-deps --open` opens the API
documentation in a browser: module docs, item docs and examples, a
readable way into the code.

### Parity with the Python version

The check that the Rust version does what the reference did: both over
every corpus log plus generated edge cases (empty input, CRLF, ANSI,
invalid UTF-8, NUL bytes, a 20,000-character line, letters that case-fold
to ASCII, ...), under 14 flag sets, compared byte for byte, output and
exit status. The candidate runs with `PARE_REFERENCE_RULES=1`, which turns
off the rules the Rust version added after the port ("Changing pare's
behaviour" below), so what is compared is everything else.

```
cargo build --release
python3 bench/parity.py --candidate target/release/pare --dump             # about four minutes on four cores
python3 bench/parity.py --candidate target/release/pare --dump --quick --show 3   # 4 flag sets, about a minute
```

It must end with `0 differ`. `--dump` also compares the pass's per-line
results (record, depth, severity, template, score, sizes, and each
template's salience and marker) and points at the first line where the
two versions part ways. Use Python 3.13, as CI does: the Rust version's
character classes are Python 3.13's (next section); any 3.10 or newer
runs the script, which, like the reference, needs nothing installed.
(`--skip-unported`, for a candidate that exits 70 on what it does not do
yet, served the port while it was partial.)

### Generated files

These files are generated from the Python version and checked in. Rerun
the scripts after changing what they are made from:

- `src/pyunicode.rs`, Python's character classes (`\w`, `\d`, `\s`,
  `isdigit()`, `isprintable()`), which differ from Rust's:
  `python3.13 bench/gen_pyunicode.py > src/pyunicode.rs`. CI fails if the
  file is not what the script writes.
- `tests/data/*`, golden files: Python's `mask()` on 4,045 real record
  heads (`mask.tsv`); severity, vocabulary classes and scores of 6,032
  lines (`vocab.tsv`); `pare vocab` (`vocab.txt`, and with
  `reference/vocab.example`, `vocab-example.txt`); whole summaries of
  five corpus logs under three flag sets (`summary/`). All from
  `python3.13 bench/gen_fixtures.py`.

### Releases and packaging

Releases are made by pushing a version tag; RELEASING.md has the
checklist and the one-time setup. `.github/workflows/release.yml` builds
static Linux (musl, x86-64 and ARM), macOS (Apple Silicon and Intel) and
Windows binaries and the PyPI wheels, and publishes the GitHub release,
the Homebrew formula, the wheels and the crate. CI's "Packaging" job
checks on every pull request that the crate packages (`cargo package`),
that the wheel builds and runs (`maturin build`), that `install.sh`
installs and verifies a release laid out on disk
(`packaging/test-install.sh`), and that the Homebrew formula renders. To
build the static Linux binary locally:

```
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl
```

## Python (the reference)

`reference/pare.py` runs on any Python 3.10 or newer with nothing
installed: `python3 reference/pare.py --budget 2000 < some.log`. Its
tests, the fixture and parity scripts and the benchmark tools use a
virtual environment:

```
python3 -m venv .venv
.venv/bin/pip install -r reference/requirements.txt   # pytest, and drain3 (the reference for the built-in Drain)
.venv/bin/python -m pytest -q reference/              # about two minutes
.venv/bin/python -m pytest -q reference/ -k follow    # tests whose name mentions follow mode
```

The reference is frozen: it is the record of what pare did when the port
was made, and it is not maintained or packaged. A change to pare is made
in the Rust version.

## Changing pare's behaviour

Three things hold pare's output still, and a change has to get past
each on purpose rather than by accident:

1. **Parity and the golden files.** A rule that changes the default
   output of plain-text logs (as structured lines, the tail window's
   verdict rule and `auto` as the default budget did) goes behind
   `pass::rust_rules()`, which `PARE_REFERENCE_RULES=1` turns off; parity
   and the golden-file test in `tests/cli.rs` run with it set, so they
   keep checking everything else, and the new rule gets tests of its own
   (`tests/cli.rs` has one per rule to copy). Regenerating the golden
   files from the Rust version instead is the planned end of parity
   (ROADMAP.md, "Retire parity"), not something a single change does.
2. **The evidence.** Every rule in pare was measured before it shipped:
   `bench/run.py --pare-bin target/release/pare` over `corpus/`,
   `corpus-generated/`, `corpus-dev/defects4j/` and every set under
   `corpus-heldout/`, at 600, 1,000, 2,000 and `auto`, before and after.
   A change should not lose a must-survive line on any set at any budget
   without saying why, and a held-out sample a change was judged on is
   spent: record it as such in `corpus-heldout/README.md` and draw a
   fresh one to confirm the change (its "Protocol" section). Write the
   result into `docs/DESIGN.md` as a rule and into `docs/HISTORY.md` as
   what was tried.
3. **Template ids.** `--only T3 < runfile` relies on the clustering and
   the masks being what they were when the run file was written. A
   change to `drain.rs`, to the built-in masks or to segmentation
   renumbers templates on existing run files; it is a breaking change
   and must say so in the CHANGELOG.

Anything a user can see (a flag, an output form, a changed default) gets
a line in `CHANGELOG.md`, under an "Unreleased" heading until the next
release moves it (RELEASING.md).

## Benchmark

`bench/run.py` runs pare, [sqz](https://github.com/ojuschugh1/sqz) and
[tidyout](https://github.com/sathish-2000/tidyout) over a corpus
directory and reports output tokens and the share of must-survive lines
(`<name>.keep`) each tool keeps. A keep line matches as a substring of
the stripped output, after pare's compact lists of failing tests are
written out in full; `--strict` matches the literal text; a keep line may
list equivalent forms as `a || b`. Tokens are bytes / 2.8, so a budget of
2,000 is 2,000 real tokens (`bench/tokens.py` checks the rate against
tiktoken).

```
.venv/bin/pip install -r bench/requirements.txt    # tidyout; see below for sqz
.venv/bin/python bench/run.py --tools pare --pare-bin target/release/pare [--budget 2000|auto] [--corpus DIR] [--show-misses] [--json out.json]
.venv/bin/python bench/run.py                      # all three tools, the Python reference for pare
.venv/bin/python bench/ablate.py                   # recall with each heuristic switched off, or a removed one back on (the reference)
.venv/bin/python bench/tokens.py                   # the byte proxy vs tiktoken counts (needs tiktoken)
```

sqz runs one-shot (`compress --no-cache`); tidyout runs with a fake
command on PATH that replays the log. On macOS: `uv tool install
tidyout-cli` puts `tidyout` on PATH; for sqz, `brew install
ojuschugh1/sqz/sqz`, `npm i -g sqz-cli` or `cargo install sqz-cli` (not
`pip install sqz`, which fetches a pinned 0.1.0 binary).

The corpora: `corpus/` is the development corpus (tuned on);
`corpus-generated/` holds real Maven, tsc, Terraform, kubectl and
structured-logger output from small projects written to fail (a second
development corpus); `corpus-dev/defects4j/` is the Defects4J sample the
failing-test fix was tuned on; `corpus-heldout/` holds the held-out
SWE-bench and Defects4J samples, with the protocol and every result in
its README. Report held-out numbers apart from the others.

## Submitting a change

Branch from `main` and open a pull request; the four CI jobs must pass.
Say in the pull request what the change does, what it was measured on
and what moved (a before/after table from `bench/run.py` for anything
that touches scoring, segmentation or allocation), and give any new
dependency a reason. Keep the comment density and style of the code
around the change (`// Rust note:` comments are for choices a C++ reader
would ask about, not for what the code says). Docs that describe what
changed go in the same pull request: the CHANGELOG line, the DESIGN rule,
the HISTORY entry.

## Layout

```
src/                      the Rust version: the pare library and command (docs/ARCHITECTURE.md)
tests/cli.rs              end-to-end tests of the binary
tests/data/               golden files written by the Python version (bench/gen_fixtures.py)
Cargo.toml                the crate (pare-cli on crates.io), its dependencies, lints and release profile
pyproject.toml            the PyPI wheels (pare-cli): the binary packaged by maturin
install.sh                the install script for the release binaries
packaging/                the Homebrew formula template, the install script's test
reference/pare.py         the Python prototype (one file, standard library only); the reference for the Rust version
reference/test_pare.py    its pytest suite
reference/vocab.example   example vocabulary extension (German)
bench/run.py              benchmark runner
bench/ablate.py           ablation study of the scoring heuristics (runs the reference)
bench/tokens.py           byte proxy vs real tokenizer counts
bench/gen_synthetic.py    generator for the seeded synthetic corpus files
bench/collect.py          turn run files into held-out cases (never shows pare's output)
bench/harvest.py          harvest held-out candidates from Claude Code transcripts (scrubbed, staged for review)
bench/swebench_cases.py   held-out cases from SWE-bench tasks, labelled by their failing tests (needs Docker)
bench/defects4j_cases.py  held-out Java cases from Defects4J bugs, labelled by their trigger tests (needs Docker)
bench/parity.py           compare two pare implementations byte for byte (the Rust version against reference/pare.py)
bench/gen_fixtures.py     write tests/data/ from the reference
bench/gen_pyunicode.py    write src/pyunicode.rs, Python's character classes
integrations/             agent instructions and a Claude Code hook (integrations/README.md)
.claude/settings.json     registers that hook for work in this repository
corpus/                   dev corpus: *.log, *.keep, *.cmd
corpus-generated/         real tool output from small projects in its src/ (a second dev corpus)
corpus-dev/defects4j/     Defects4J development sample (seed 1)
corpus-heldout/           protocol and held-out cases: SWE-bench, Defects4J (results in its README)
docs/USAGE.md             command mode, follow mode, run files and tokens in detail
docs/DESIGN.md            design notes: how pare works and why, each rule with its evidence in brief
docs/HISTORY.md           the experiments behind the rules, in order, with the numbers of the time
docs/ARCHITECTURE.md      the Rust code: crate layout, data flow, memory model, Python-to-Rust map
docs/RUST.md              Rust for C++ developers, through pare's code
.github/workflows/        CI (Rust checks, Python tests, parity, packaging) and the release
ROADMAP.md                what comes next, in order, and who can do it
RELEASING.md              how to make a release, and the one-time setup of the channels
CHANGELOG.md              what changed in each release
```
