# Building, running and testing pare

pare has two implementations while it is being ported (ROADMAP.md, stage
5): the Rust version in `src/`, in progress, and the Python version in
`reference/pare.py`, which is the reference the Rust one must match byte for
byte. This page covers both. How the Rust code is organised is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the Rust-specific parts,
explained for a C++ reader, are in [docs/RUST.md](docs/RUST.md).

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

The first build downloads the dependencies (about 30 crates, 3 MB) from
crates.io; later builds only recompile what changed.

`cargo run` builds and runs in one step. The first `--` separates cargo's
arguments from pare's, and pare's own `--` (command mode) comes after
that:

```
cargo run --release -- --no-tee --budget 2000 < corpus/cargo-build.log
cargo run --release -- --budget 2000 -- make test     # command mode
```

The Rust version does everything the Python version does, with the
same output (docs/ARCHITECTURE.md, "Status").

Without `--no-tee`, pare saves its input as a run file, as the Python
version does: in `$PARE_RUN_DIR`, else `~/.cache/pare/runs`.

**Installing it on your PATH.** `cargo install --path .` builds a release
binary from the checkout into `~/.cargo/bin/pare` (`cargo uninstall
pare-cli` removes it). The released versions install with Homebrew, the
install script, pipx or cargo (README, "Install"). An older Python `pare`
in `~/.local/bin` is shadowed if `~/.cargo/bin` comes first on your PATH;
`pipx uninstall pare` or deleting that file removes it.

### Test

```
cargo test                     # everything: unit tests, tests/cli.rs, doc examples
cargo test clean_line          # only tests whose name contains "clean_line"
cargo test --test cli          # only the end-to-end tests in tests/cli.rs
cargo test --doc               # only the examples in /// doc comments
cargo test -- --nocapture      # show println! output from tests
```

Unit tests sit at the bottom of each module (`#[cfg(test)] mod tests`);
`tests/cli.rs` runs the built binary; examples in `///` docs are compiled
and run, so the docs cannot drift from the code.

### The checks CI runs

`.github/workflows/ci.yml` runs these on every pull request, and they
should pass locally before pushing:

```
cargo fmt --check                              # or `cargo fmt` to format in place
cargo clippy --all-targets -- -D warnings      # lints; warnings are errors
cargo test
RUSTDOCFLAGS="-D warnings" cargo doc --no-deps
```

`cargo doc --no-deps --open` opens the API documentation in a browser:
module docs, item docs and examples, a readable way into the code.

### Parity with the Python version

The check that the port is right: both versions over every corpus log
plus generated edge cases, under 14 flag sets, compared byte for byte.
It needs Python 3.13 (any 3.10 or newer runs it, but the port's
character classes are 3.13's) and nothing else: `reference/pare.py` and
`bench/parity.py` use only the standard library.

```
cargo build --release
python3 bench/parity.py --candidate target/release/pare --dump
python3 bench/parity.py --candidate target/release/pare --dump --quick --show 3   # a faster subset
```

The full run takes about two minutes on four cores and must end with
`0 differ`. `--dump` also compares the per-line results (record, depth,
severity, template, score, sizes, and each template's salience and
marker) and points at the first line where the two versions part ways.
(`--skip-unported`, for a candidate that exits 70 on what it does not do
yet, and `?` for a dump field it does not compute, served the port while
it was partial.)

Use Python 3.13 for parity, as CI does: the Rust port's character classes
are Python 3.13's (next section).

### Generated files

These files are generated from the Python version and checked in. Rerun
the scripts after changing what they are made from:

- `src/pyunicode.rs`, Python's character classes (`\w`, `\d`, `\s`,
  `isdigit()`, `isprintable()`), which differ from Rust's:
  `python3.13 bench/gen_pyunicode.py > src/pyunicode.rs`. CI fails if the
  file is not what the script writes.
- `tests/data/*`, golden files for the unit tests: Python's `mask()` on
  4,045 real record heads (`mask.tsv`); severity, vocabulary classes and
  scores of 6,032 lines (`vocab.tsv`); `--dump-vocab` (`vocab.txt`, and
  with `reference/vocab.example`, `vocab-example.txt`); whole summaries of
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

`reference/pare.py` runs on any Python 3.10 or newer with nothing installed:
`python3 reference/pare.py --budget 2000 < some.log`. For its tests, the
fixture and parity scripts and the benchmark tools, use a virtual
environment:

```
python3 -m venv .venv
.venv/bin/pip install -r reference/requirements.txt   # pytest, and drain3 (the reference for the built-in Drain)
.venv/bin/pip install -r bench/requirements.txt       # the benchmark's comparison tools, if you run bench/run.py
.venv/bin/python -m pytest -q reference/
```

The reference is frozen: it is the record of what pare did when the port
was made, and CI holds the Rust version to it byte for byte. A change to
pare's behaviour is made in the Rust version; parity then shows exactly
what changed, and the change also updates the golden files and the parity
check (ROADMAP.md says how).
