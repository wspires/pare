//! Turning a raw input line into the text pare works with: `clean_line()`
//! in `reference/pare.py`.
//!
//! Input is bytes, and logs are not always valid UTF-8. A line is decoded
//! with invalid sequences replaced by U+FFFD, reduced to what a terminal
//! would show after its last carriage return, stripped of ANSI escape
//! sequences, and right-trimmed, all exactly as the Python version does it.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;

/// ANSI escape sequences: CSI (`ESC [ ... final`), OSC (`ESC ] ... BEL` or
/// `ESC ] ... ESC \`), and two-byte escapes. Same pattern as `ANSI_RE`.
// Rust note: `LazyLock` builds the value on first use, once, safely across
// threads. A C++ function-local static does the same; a global with a
// constructor would risk the static initialisation order problem, which
// Rust sidesteps by not running code before main at all.
static ANSI_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
        .expect("ANSI pattern is valid")
});

pub use crate::pyclass::py_isspace;

/// The cleaned text of one raw line (without its `\n`).
///
/// ```
/// use pare::text::clean_line;
/// assert_eq!(clean_line(b"downloading 10%\rdone  "), "done");
/// assert_eq!(clean_line(b"\x1b[31mFAIL\x1b[0m test_b"), "FAIL test_b");
/// assert_eq!(clean_line(b"bad \xff byte"), "bad \u{fffd} byte");
/// ```
// Rust note: the return type is `Cow<'_, str>`, "clone on write": either a
// borrowed `&str` pointing into `raw` or an owned `String`. Most log lines
// are valid UTF-8 with no carriage return and no escape codes, so for them
// nothing is allocated or copied; only a line that needs changing pays for
// a new string. The `'_` lifetime ties a borrowed result to `raw`: the
// compiler will not let the result outlive the buffer it points into,
// which is the dangling-`string_view` bug made impossible.
#[must_use]
pub fn clean_line(raw: &[u8]) -> Cow<'_, str> {
    // Python: raw.decode("utf-8", errors="replace"). Both replace each
    // maximal invalid subsequence with one U+FFFD, as the Unicode standard
    // recommends, so the two agree byte for byte.
    let mut text = String::from_utf8_lossy(raw);

    // A terminal shows whatever followed the last carriage return
    // (progress bars redraw a line this way); empty segments do not count.
    if text.contains('\r') {
        let last = text.split('\r').rfind(|s| !s.is_empty()).unwrap_or("");
        text = Cow::Owned(last.to_owned());
    }
    if text.contains('\x1b') {
        // Rust note: `replace_all` also returns a `Cow`: borrowed when
        // nothing matched. `into_owned()` makes it a `String` either way,
        // because the borrow would otherwise point into `text`, which is
        // about to be replaced.
        let stripped = ANSI_RE.replace_all(&text, "").into_owned();
        text = Cow::Owned(stripped);
    }
    // Python: .rstrip(). Trimming a borrowed string borrows a shorter
    // slice of it; trimming an owned one has to copy (or truncate).
    match text {
        Cow::Borrowed(s) => Cow::Borrowed(s.trim_end_matches(py_isspace)),
        Cow::Owned(mut s) => {
            let keep = s.trim_end_matches(py_isspace).len();
            s.truncate(keep);
            Cow::Owned(s)
        }
    }
}

/// Indentation in columns: a space counts 1, a tab 4, up to the first
/// other character. As `indent_of()` in Python.
///
/// ```
/// assert_eq!(pare::text::indent_of("\t  at x"), 6);
/// ```
#[must_use]
pub fn indent_of(text: &str) -> i64 {
    let mut n = 0;
    for c in text.chars() {
        match c {
            ' ' => n += 1,
            '\t' => n += 4,
            _ => break,
        }
    }
    n
}

/// The first `stop` characters of `s`, with Python's slice semantics for
/// `s[:stop]`: a negative `stop` counts from the end, and out-of-range
/// values are clamped.
fn py_prefix(s: &str, stop: i64) -> &str {
    // Rust note: `chars().count()` is O(n): a `str` is UTF-8 bytes, so the
    // number of characters is not stored, unlike Python's `len()`. And a
    // slice must end on a character boundary, so the byte offset of the
    // n-th character is looked up with `char_indices`.
    let n = i64::try_from(s.chars().count()).unwrap_or(i64::MAX);
    let keep = if stop < 0 {
        (n + stop).max(0)
    } else {
        stop.min(n)
    };
    let keep = usize::try_from(keep).unwrap_or(0);
    match s.char_indices().nth(keep) {
        Some((byte, _)) => &s[..byte],
        None => s,
    }
}

/// A line cut to `width` characters with a marker saying how much went:
/// `clip()` in Python. The line stays whole when it fits, or when the
/// marker would be no shorter than what it replaces. A long JSON or
/// logfmt line is cut by [`crate::structured::clip`] instead, which keeps
/// the fields that say what went wrong.
///
/// ```
/// use pare::text::clip;
/// assert_eq!(clip("héllo wörld", 5), "héll…(+7c)");
/// assert_eq!(clip("short", 200), "short");
/// ```
#[must_use]
pub fn clip(text: &str, width: i64) -> Cow<'_, str> {
    let len = i64::try_from(text.chars().count()).unwrap_or(i64::MAX);
    if len <= width {
        return Cow::Borrowed(text);
    }
    // A JSON or logfmt line keeps its telling fields (not in Python).
    if crate::pass::TOOL_STRUCTURED && crate::pass::rust_rules() {
        if let Some(clipped) = crate::structured::clip(text, width) {
            return Cow::Owned(clipped);
        }
    }
    let cut = len - width + 1;
    let marker = format!("…(+{cut}c)");
    if i64::try_from(marker.chars().count()).unwrap_or(0) >= cut {
        return Cow::Borrowed(text);
    }
    let head = py_prefix(text, width - 1).trim_end_matches(py_isspace);
    Cow::Owned(format!("{head}{marker}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn carriage_returns() {
        assert_eq!(clean_line(b"a\rb\rc"), "c");
        assert_eq!(clean_line(b"abc\r"), "abc"); // the empty last segment does not count
        assert_eq!(clean_line(b"\r\r"), "");
        assert_eq!(clean_line(b"INFO start\r"), "INFO start"); // CRLF input
    }

    #[test]
    fn ansi_sequences() {
        assert_eq!(clean_line(b"\x1b[1;32mok\x1b[0m"), "ok");
        assert_eq!(clean_line(b"\x1b]0;title\x07text"), "text");
        assert_eq!(clean_line(b"\x1b]8;;http://x\x1b\\link"), "link");
        assert_eq!(clean_line(b"\x1bMx"), "x");
    }

    #[test]
    fn right_trim_follows_python() {
        assert_eq!(clean_line(b"x \t\x1c\x1f"), "x");
        assert_eq!(clean_line("x\u{a0}\u{3000}".as_bytes()), "x");
        assert_eq!(clean_line(b"  lead kept"), "  lead kept");
        assert_eq!(clean_line(b"x\x00"), "x\x00"); // NUL is not whitespace
    }

    #[test]
    fn borrows_when_nothing_changes() {
        assert!(matches!(clean_line(b"plain line"), Cow::Borrowed(_)));
        assert!(matches!(clean_line(b"trailing   "), Cow::Borrowed(_)));
        assert!(matches!(clean_line(b"cr\rhere"), Cow::Owned(_)));
    }

    #[test]
    fn clip_like_python() {
        // Values from reference/pare.py's clip().
        let x300 = "abcdefghij".repeat(30);
        let cases: [(&str, i64, String); 7] = [
            ("xxxxxxxxxx", 10, "xxxxxxxxxx".into()),
            ("xxxxxxxxxxx", 10, "xxxxxxxxxxx".into()), // the marker would be longer than the cut
            (&x300, 200, format!("{}…(+101c)", &x300[..199])),
            ("héllo wörld", 5, "héll…(+7c)".into()),
            (
                "aaaaaaaaaaaaaaaaaaaa",
                0,
                format!("{}…(+21c)", "a".repeat(19)),
            ), // Python's text[:-1]
            (
                "aaaaaaaaaaaaaaaaaaaa",
                -3,
                format!("{}…(+24c)", "a".repeat(16)),
            ),
            (&"a".repeat(300)[..], 1, "…(+300c)".into()),
        ];
        for (text, width, want) in cases {
            assert_eq!(clip(text, width), want, "{width}");
        }
        assert_eq!(
            clip(&format!("abc   {}", "d".repeat(300)), 8),
            "abc   d…(+299c)"
        );
    }

    #[test]
    fn invalid_utf8_like_python() {
        // Python: b"\xc3\x28".decode("utf-8", "replace") == "�("
        assert_eq!(clean_line(b"\xc3\x28"), "\u{fffd}(");
        // A truncated 3-byte sequence is one replacement, not two.
        assert_eq!(clean_line(b"a\xe2\x82"), "a\u{fffd}");
        assert_eq!(clean_line(b"\x80\x81"), "\u{fffd}\u{fffd}");
    }
}
