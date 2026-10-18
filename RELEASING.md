# Releasing pare

A release is one tag push. `.github/workflows/release.yml` then builds the
binaries and wheels and publishes them to four places:

| channel | what users run | what the workflow does |
|---|---|---|
| GitHub release | `install.sh`, or download an archive | attaches the archives (Linux x86-64 and ARM, macOS ARM and Intel, Windows) and `SHA256SUMS` |
| Homebrew tap | `brew install wspires/tap/pare` | fills in `packaging/homebrew/pare.rb.in`, installs and tests it with Homebrew, pushes it to `wspires/homebrew-tap` |
| PyPI | `pipx install pare-cli`, `uvx pare-cli` | uploads the `pare-cli` wheels (the binary, built by maturin) and the source distribution |
| crates.io | `cargo install pare-cli`, `cargo binstall pare-cli` | `cargo publish` |

The package is `pare-cli` on PyPI and crates.io, where `pare` is taken; the
command is `pare` everywhere.

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
   PyPI trusts this workflow's identity. In this repository, create the
   environment `pypi` (Settings → Environments); adding yourself as a
   required reviewer makes every upload wait for your approval.
3. **crates.io.** Log in at crates.io with GitHub, verify your email,
   create an API token (Account Settings → API Tokens) with the scopes
   `publish-new` and `publish-update`, and add it as the Actions secret
   `CARGO_REGISTRY_TOKEN`. After the first release, narrow the token to
   the `pare-cli` crate.

## Checklist for a release

1. On a branch: set the version in `Cargo.toml` (`version = "0.3.0"`), run
   `cargo build` so `Cargo.lock` follows, and move the "Unreleased"
   section of `CHANGELOG.md` under the new version and today's date.
2. Check what CI checks, locally if you like:
   `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test`,
   and parity (`python3 bench/parity.py --candidate target/release/pare --dump`).
3. Merge it, then tag the merge commit and push the tag:

   ```
   git switch main && git pull
   git tag -a v0.3.0 -m "pare 0.3.0"
   git push origin v0.3.0
   ```

   The workflow's first job stops the release if the tag and
   `Cargo.toml`'s version differ.
4. Watch the "Release" workflow in the Actions tab. When it is done, check:
   - the release page has the five archives and `SHA256SUMS`;
   - `brew update && brew upgrade pare` (or `brew install wspires/tap/pare`);
   - `pipx install pare-cli` (or `pipx upgrade pare-cli`) and `pare --version`;
   - `cargo binstall pare-cli`.

A tag with a suffix (`v0.3.0-beta.1`) is a pre-release: a GitHub
pre-release, PyPI and crates.io pre-release versions (only installed when
asked for), and no Homebrew update.

To build the archives and wheels without publishing (to try a change to
the workflow), run the workflow by hand: Actions → Release → Run workflow.
The results are attached to the run as artifacts.

## If something fails

- **Before anything was published** (a build failed): fix it, delete the
  tag (`git push --delete origin v0.3.0; git tag -d v0.3.0`) and tag again.
- **After some channels published:** versions on PyPI and crates.io cannot
  be replaced, only yanked. Re-run just the failed jobs from the workflow
  run page (the published ones are not repeated), or fix and release the
  next patch version.
