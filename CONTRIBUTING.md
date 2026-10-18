# Building, running and testing pare

pare has two implementations while it is being ported (ROADMAP.md, stage
5): the Rust version in `src/`, in progress, and the Python version in
`proto/pare.py`, which is the reference the Rust one must match byte for
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
- **Version.** The crate needs Rust 1.85 or newer (`rust-version` in
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
cargo run --release -- --budget 2000 -- make test     # once command mode is ported
```

What the Rust version does so far is listed in docs/ARCHITECTURE.md
("Status"). Anything not ported yet stops with status 70 and says so:

```
$ target/release/pare --no-tee < corpus/ci-pipeline.log
pare: not yet ported to Rust: summaries of input larger than the budget (proto/pare.py does it meanwhile)
```

Without `--no-tee`, pare saves its input as a run file, as the Python
version does: in `$PARE_RUN_DIR`, else `~/.cache/pare/runs`.

**Installing it on your PATH.** `cargo install --path .` builds a release
binary into `~/.cargo/bin/pare` (`cargo uninstall pare` removes it). Wait
until the port is complete: until then it handles small inputs only, and
it shadows a Python `pare` in `~/.local/bin` if `~/.cargo/bin` comes
first on your PATH.

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
plus generated edge cases, under 13 flag sets, compared byte for byte.
It needs Python 3.13 (any 3.10 or newer runs it, but the port's
character classes are 3.13's) and nothing else: `proto/pare.py` and
`bench/parity.py` use only the standard library.

```
cargo build --release
python3 bench/parity.py --candidate target/release/pare --skip-unported --dump
python3 bench/parity.py --candidate target/release/pare --skip-unported --dump --quick --show 3   # a faster subset
```

The full run takes about two minutes on four cores and must end with
`0 differ`. `--skip-unported` counts runs the port does not handle yet as
skipped; `--dump` also compares the per-line results (record, depth,
template, sizes; severity and scores once ported) and points at the first
line where the two versions part ways. Fields the port does not compute
yet are written as `?` and not compared.

Use Python 3.13 for parity, as CI does: the Rust port's character classes
are Python 3.13's (next section).

### Generated files

Two files are generated from the Python version and checked in. Rerun the
scripts after changing what they are made from:

- `src/pyunicode.rs`, Python's character classes (`\w`, `\d`, `\s`,
  `isdigit()`), which differ from Rust's:
  `python3.13 bench/gen_pyunicode.py > src/pyunicode.rs`. CI fails if the
  file is not what the script writes.
- `tests/data/mask.tsv`, Python's `mask()` on 4,045 real record heads, the
  golden file for the Rust masks' unit test:
  `python3 bench/gen_fixtures.py`.

### Release builds

`.github/workflows/release.yml` builds static Linux (musl), macOS (Apple
Silicon and Intel) and Windows binaries when a version tag (`v0.2.0`) is
pushed, and attaches them to a GitHub release. It can also be run by hand
from the Actions tab to build the archives without publishing. To build
the static Linux binary locally:

```
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl
```

## Python (the reference)

`proto/pare.py` runs on any Python 3.10 or newer with nothing installed:
`python3 proto/pare.py --budget 2000 < some.log`. For its tests and the
benchmark tools, use a virtual environment:

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'          # pare, pytest, and drain3 (the reference for the built-in Drain)
.venv/bin/python -m pytest -q proto/
```

The rules are frozen during the port (ROADMAP.md, stage 5): the Python
version changes only to fix a bug the Rust version must then match, so
the target does not move.
