//! Turning a raw input line into the text pare works with: `clean_line()`
//! in `proto/pare.py`.
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

/// Whether Python's `str.isspace()` is true for `c`.
///
/// Rust's `char::is_whitespace` follows Unicode's `White_Space` property;
/// Python's `isspace` also counts U+001C to U+001F (the ASCII file, group,
/// record and unit separators), so `str.rstrip()` would strip them and
/// `str::trim_end()` would not. The list was generated from Python 3.13:
/// `[c for c in range(0x110000) if chr(c).isspace()]`.
///
/// ```
/// use pare::text::py_isspace;
/// assert!(py_isspace('\u{1f}') && !'\u{1f}'.is_whitespace());
/// assert!(py_isspace('\u{3000}') && !py_isspace('x'));
/// ```
// Rust note: `matches!` tests a value against a pattern, here a set of
// ranges, and compiles to the same comparisons a hand-written switch would.
#[must_use]
pub fn py_isspace(c: char) -> bool {
    matches!(
        c,
        '\t'..='\r'
            | '\u{1c}'..='\u{20}'
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

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
    fn invalid_utf8_like_python() {
        // Python: b"\xc3\x28".decode("utf-8", "replace") == "�("
        assert_eq!(clean_line(b"\xc3\x28"), "\u{fffd}(");
        // A truncated 3-byte sequence is one replacement, not two.
        assert_eq!(clean_line(b"a\xe2\x82"), "a\u{fffd}");
        assert_eq!(clean_line(b"\x80\x81"), "\u{fffd}\u{fffd}");
    }
}
