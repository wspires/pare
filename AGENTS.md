# For agents working in this repository

Before every `git push` that changes anything but Markdown, run
`bench/check.sh --quick` and push only when it ends with `ok`. It runs
what CI runs (`.github/workflows/ci.yml`): the Rust checks, the Python
reference's tests, parity and packaging, and needs the `.venv` that
CONTRIBUTING.md's "Python (the reference)" section creates. A clone that
has run `git config core.hooksPath .githooks` does this on every push by
itself. CONTRIBUTING.md has the rest.
