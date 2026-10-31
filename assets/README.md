# Assets

The pare mark, for the README, the repository's social preview and any
other place that wants one.

- `pare.svg`: the wordmark, "pare" in a rounded monoline whose p ends in
  the dotted ┆ that a summary prints where lines were omitted. For
  headers and anywhere with width.
- `pare-mark.svg`: the p alone, for square slots (favicon, avatar,
  tile). Same construction as the wordmark.
- `plugin-icon.html`: the source of the Claude Code plugin's listing
  icon (`integrations/claude-code/.claude-plugin/icon.png`), the mark on
  a dark tile, and how to render it.
- `social-preview.png`: the 1280×640 card GitHub shows when the
  repository is linked. It is uploaded by hand under Settings → General
  → Social preview; `social-preview.html` is its source and says how to
  render it.

Both SVGs are drawn as strokes, not text, so they need no font. Inlined
in a page they take the surrounding color (`currentColor`); loaded as an
image they are dark on light and light on dark, following the viewer's
color scheme. The README loads the wordmark by relative path, which
GitHub and crates.io render; PyPI does not rewrite relative paths, so the
image is missing there until the repository is public and the README can
use the raw GitHub URL.
