# Releasing pare

A release is one tag push. `.github/workflows/release.yml` then builds the
binaries and wheels and publishes them to four places:

| channel | what users run | what the workflow does |
|---|---|---|
| GitHub release | `install.sh`, or download an archive | attaches the archives (Linux x86-64 and ARM, macOS ARM and Intel, Windows) and `SHA256SUMS` |
| Homebrew tap | `brew install wspires/tap/pare` | fills in `packaging/homebrew/pare.rb.in` from `SHA256SUMS`, installs, tests and audits it with Homebrew, pushes it to `wspires/homebrew-tap` |
| PyPI | `pipx install pare-cli`, `uvx pare-cli` | uploads the `pare-cli` wheels (the binary, built by maturin) and the source distribution |
| crates.io | `cargo install pare-cli`, `cargo binstall pare-cli` | `cargo publish`, after the GitHub release, since `cargo binstall` downloads the archives it points at |

The package is `pare-cli` on PyPI and crates.io, where `pare` is taken; the
command is `pare` everywhere. The tag pattern is `v` and a version
(`v0.2.0`, `v0.3.0-beta.1`); the Python reference's tag, `v0.1-python`,
does not match it and never ran the workflow.

## One-time setup

Each channel needs something only the repository owner can create. A
channel that is not set up yet is skipped with a warning (Homebrew and
crates.io) or fails on its own (PyPI) without stopping the others.

1. **Homebrew tap.** Create an empty public repository named
   `homebrew-tap` under `wspires` (the `homebrew-` prefix is what makes
   `brew install wspires/tap/pare` work). Then create a fine-grained
   personal access token (GitHub → Settings → Developer settings →
   Fine-grained tokens) with access to that one repository and
   **Contents: read and write**, and add it to this repository as the
   Actions secret `HOMEBREW_TAP_TOKEN` (Settings → Secrets and variables →
   Actions).
2. **PyPI.** Create an account at pypi.org (with two-factor
   authentication). Under Account settings → Publishing, add a *pending
   trusted publisher*: project `pare-cli`, owner `wspires`, repository
   `pare`, workflow `release.yml`, environment `pypi`. No token is stored:
   PyPI trusts this workflow's identity. GitHub creates the `pypi`
   environment the first time the workflow runs; to have every upload
   wait for your approval, create it first (Settings → Environments) and
   add yourself as a required reviewer.
3. **crates.io.** Log in at crates.io with GitHub, verify your email,
   create an API token (Account Settings → API Tokens) with the scopes
   `publish-new` and `publish-update`, and add it as the Actions secret
   `CARGO_REGISTRY_TOKEN`. After the first release, narrow the token to
   the `pare-cli` crate.

## Checklist for a release

1. On a branch: set the version in `Cargo.toml` (`version = "0.3.0"`),
   run `cargo build` so `Cargo.lock` follows (the release builds and
   `cargo publish` run with `--locked`, and a stale lockfile stops the
   release at its first build), and rename the "Unreleased" section of
   `CHANGELOG.md` to the new version.
2. Check what CI checks, locally if you like. The Rust job:
   `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test`
   and `RUSTDOCFLAGS='-D warnings' cargo doc --no-deps`; parity
   (`python3 bench/parity.py --candidate target/release/pare --dump`);
   and the Packaging job, the one a release depends on: `cargo package
   --locked`, a wheel (`maturin build --release`) installed in a fresh
   venv, `shellcheck install.sh packaging/test-install.sh`,
   `packaging/test-install.sh target/release/pare`, and the formula
   rendered by `packaging/homebrew/render.py` and checked with `ruby -c`.
3. Merge it, then tag the merge commit and push the tag:

   ```
   git switch main && git pull
   git tag -a v0.3.0 -m "pare 0.3.0"
   git push origin v0.3.0
   ```

   The workflow's first job stops the release if the tag is not `v` plus
   `Cargo.toml`'s version, exactly.
4. Watch the "Release" workflow in the Actions tab. When it is done, check:
   - the release page has the five archives and `SHA256SUMS`;
   - `brew update && brew upgrade pare` (or `brew install wspires/tap/pare`);
   - `pipx install pare-cli` (or `pipx upgrade pare-cli`) and `pare --version`;
   - `cargo binstall pare-cli`.

A tag with a suffix (`v0.3.0-beta.1`) is a pre-release: `Cargo.toml` must
carry the same suffix (`version = "0.3.0-beta.1"`) for the tag check to
pass. It makes a GitHub pre-release, a crates.io pre-release and a PyPI
pre-release (maturin writes the version as `0.3.0b1`; both are installed
only when asked for by version), and no Homebrew update.

To build the archives and wheels without publishing (to try a change to
the workflow), run the workflow by hand: Actions → Release → Run workflow.
The results are attached to the run as artifacts.

## If something fails

- **Before anything was published** (a build or wheel job failed, so the
  GitHub release job never ran): fix it, delete the tag
  (`git push --delete origin v0.3.0; git tag -d v0.3.0`) and tag again.
- **After some channels published:** the GitHub release can be deleted
  from its page and made again with the tag; versions on PyPI and
  crates.io cannot be replaced, only yanked. Re-run just the failed jobs
  from the workflow run page (the published ones are not repeated), or
  fix and release the next patch version.
