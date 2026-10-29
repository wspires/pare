#!/bin/sh
# What CI runs (.github/workflows/ci.yml), locally, in the same order, and
# stopping at the first failure: the Rust job (fmt, clippy, tests, docs),
# the Python reference's tests, the Unicode tables and parity, packaging.
#
#   bench/check.sh            everything, as CI does (about eight minutes)
#   bench/check.sh --quick    parity under 4 flag sets instead of 14 (five)
#   bench/check.sh --rust     the Rust job only (under a minute)
#
# A packaging tool that is not installed (maturin, shellcheck, ruby) is
# reported and skipped; CI still runs it. .githooks/pre-push runs this
# with --quick before every push that changes more than Markdown.
set -eu

cd "$(dirname "$0")/.."
quick=; rust_only=
for a in "$@"; do
    case "$a" in
        --quick) quick=1 ;;
        --rust) rust_only=1 ;;
        *) echo "usage: bench/check.sh [--quick | --rust]" >&2; exit 2 ;;
    esac
done
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi

step() { printf '\n== %s\n' "$*"; }
skip() { printf '   skipped: %s (CI runs it)\n' "$*"; }

step "cargo fmt --check";                         cargo fmt --check
step "cargo clippy --all-targets -- -D warnings"; cargo clippy --all-targets -q -- -D warnings
step "cargo test";                                cargo test -q
step "cargo doc --no-deps (warnings are errors)"; RUSTDOCFLAGS="-D warnings" cargo doc --no-deps -q
if [ -n "$rust_only" ]; then printf '\nok: the Rust job\n'; exit 0; fi

step "python -m pytest -q reference/ bench/"
"$py" -c 'import pytest, drain3' 2>/dev/null || {
    echo "pytest or drain3 is missing: python3 -m venv .venv && .venv/bin/pip install -r reference/requirements.txt" >&2; exit 1; }
"$py" -m pytest -q reference/ bench/

step "src/pyunicode.rs is what bench/gen_pyunicode.py writes"
case "$("$py" --version)" in
    "Python 3.13"*) "$py" bench/gen_pyunicode.py | diff - src/pyunicode.rs && echo "   same" ;;
    *) skip "needs Python 3.13, this is $("$py" --version)" ;;
esac

step "parity with the Python reference${quick:+ (--quick)}"
cargo build --release -q
"$py" bench/parity.py --candidate target/release/pare --dump --show 3 ${quick:+--quick}

step "cargo package --locked";                    cargo package --locked -q --allow-dirty
step "the PyPI wheel (maturin build)"
if command -v maturin >/dev/null; then
    rm -rf target/check-wheels target/check-venv
    maturin build --release -q --out target/check-wheels
    "$py" -m venv target/check-venv && target/check-venv/bin/pip install -q target/check-wheels/*.whl && target/check-venv/bin/pare --version
else skip "maturin is not installed"; fi
step "shellcheck install.sh packaging/test-install.sh"
if command -v shellcheck >/dev/null; then shellcheck install.sh packaging/test-install.sh; else skip "shellcheck is not installed"; fi
step "install.sh against a release laid out on disk";  packaging/test-install.sh target/release/pare
step "the Homebrew formula renders"
for t in aarch64-apple-darwin x86_64-apple-darwin aarch64-unknown-linux-musl x86_64-unknown-linux-musl; do
    echo "0000  pare-v1.2.3-$t.tar.gz"
done > target/check-sums
"$py" packaging/homebrew/render.py 1.2.3 target/check-sums > target/check-pare.rb
if command -v ruby >/dev/null; then ruby -c target/check-pare.rb; else skip "ruby is not installed"; fi

printf '\nok: everything CI checks\n'
