# Changelog

## Unreleased

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
- **License:** MIT OR Apache-2.0.
- The Python version is kept in `reference/` as the reference the Rust
  version is tested against; it is no longer packaged.

Differences: a follow run file is named by the time in UTC (it was local
time), and a malformed regex in a `--vocab` file is reported in the Rust
regex engine's words.

## 0.1 (`v0.1-python`)

The Python prototype: a single file, `reference/pare.py`.
