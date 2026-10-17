//! Python's character classes, for code that must classify characters
//! exactly as `proto/pare.py` does.
//!
//! The tables are in [`crate::pyunicode`], generated from Python by
//! `bench/gen_pyunicode.py`. This module looks characters up in them and
//! translates Python-style patterns (with `\w`, `\d`, `\s`, `\b`) into
//! patterns for the `fancy-regex` crate with those classes spelled out.
//!
//! Why: Rust's `regex` crate and Python's `re` disagree on thousands of
//! characters. Rust's `\w` includes combining marks and connector
//! punctuation that Python's does not, and leaves out "other numbers" such
//! as superscripts that Python's includes; `\d` follows each language's
//! Unicode version; Python's `\s` has four ASCII separators Rust's lacks.
//! Logs are mostly ASCII, but a template id must not depend on which
//! language read the line.

use std::sync::LazyLock;

use crate::pyunicode::{DECIMAL, DIGIT, SPACE, WORD};

/// Whether `c` is in a table of sorted, inclusive ranges.
// Rust note: `binary_search_by` returns `Ok(index)` when the comparison
// says "equal" (here: the range contains `c`) and `Err(insert point)`
// otherwise; `is_ok()` is the answer. A `&[(u32, u32)]` is a slice of
// tuples: the table is a `const` array baked into the binary, borrowed
// with no copying or allocation.
fn in_table(table: &[(u32, u32)], c: char) -> bool {
    let cp = u32::from(c);
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < cp {
                std::cmp::Ordering::Less
            } else if lo > cp {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// Python's `str.isspace()` (and `re`'s `\s`), which counts U+001C to
/// U+001F as whitespace where Rust's `char::is_whitespace` does not.
///
/// ```
/// use pare::pyclass::py_isspace;
/// assert!(py_isspace('\u{1f}') && !'\u{1f}'.is_whitespace());
/// assert!(py_isspace('\u{3000}') && !py_isspace('x'));
/// ```
#[must_use]
pub fn py_isspace(c: char) -> bool {
    // ASCII first: nearly every character asked about is.
    if c.is_ascii() {
        return matches!(c, '\t'..='\r' | '\u{1c}'..='\u{20}');
    }
    in_table(SPACE, c)
}

/// Python's `str.isdigit()`: decimal digits and other digits such as
/// superscripts (`²`), wider than `re`'s `\d`.
///
/// ```
/// use pare::pyclass::py_isdigit;
/// assert!(py_isdigit('7') && py_isdigit('²') && !py_isdigit('x'));
/// ```
#[must_use]
pub fn py_isdigit(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_digit();
    }
    in_table(DIGIT, c)
}

/// Python's `str.isdecimal()` (and `re`'s `\d`): decimal digits only.
#[must_use]
pub fn py_isdecimal(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_digit();
    }
    in_table(DECIMAL, c)
}

/// Python's `str.strip()`: `s` without leading and trailing whitespace, as
/// Python defines whitespace.
///
/// ```
/// use pare::pyclass::py_strip;
/// assert_eq!(py_strip("\u{1f} x y \t"), "x y");
/// ```
#[must_use]
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// Python's `str.split()` with no arguments: the words between runs of
/// whitespace, as Python defines whitespace.
///
/// ```
/// use pare::pyclass::py_split;
/// let words: Vec<&str> = py_split("  a\u{1c}b  c ").collect();
/// assert_eq!(words, ["a", "b", "c"]);
/// ```
// Rust note: `impl Iterator<Item = &str>` returns some iterator type
// without naming it (it is a long, compiler-made type). The words are
// slices of `s`, produced lazily as the caller asks for them; nothing is
// collected unless the caller collects. The `'_` ties them to `s`.
pub fn py_split(s: &str) -> impl Iterator<Item = &str> + '_ {
    s.split(py_isspace).filter(|w| !w.is_empty())
}

/// The members of a table in regex class syntax, without the brackets.
fn class_body(table: &[(u32, u32)]) -> String {
    let mut out = String::with_capacity(table.len() * 20);
    for &(lo, hi) in table {
        // Rust note: `write!` into a `String` formats in place, without a
        // temporary string per range; `fmt::Write` is the trait that lets a
        // `String` be a formatting target, like an `ostringstream`.
        use std::fmt::Write;
        if lo == hi {
            let _ = write!(out, "\\x{{{lo:X}}}");
        } else {
            let _ = write!(out, "\\x{{{lo:X}}}-\\x{{{hi:X}}}");
        }
    }
    out
}

static WORD_BODY: LazyLock<String> = LazyLock::new(|| class_body(WORD));
static DECIMAL_BODY: LazyLock<String> = LazyLock::new(|| class_body(DECIMAL));
static SPACE_BODY: LazyLock<String> = LazyLock::new(|| class_body(SPACE));

/// A pattern written in Python's syntax, with Python's meaning for `\w`,
/// `\d`, `\s` and `\b`, rewritten for `fancy-regex`.
///
/// Outside a bracket class, `\w` becomes `[...]` with Python's word
/// characters; inside one (`[\w./-]`), it contributes its members. `\b`
/// becomes a lookaround on Python's `\w`. Everything else is copied as is:
/// the patterns pare uses mean the same in both engines apart from these
/// classes (leftmost-first alternation, greedy and lazy quantifiers).
///
/// ```
/// use pare::pyclass::py_pattern;
/// let p = py_pattern(r"[\d.]+");
/// assert!(p.starts_with(r"[\x{30}-\x{39}") && p.ends_with(".]+"));
/// ```
///
/// # Panics
///
/// On `\W`, `\D`, `\S` or `\B`, which pare's patterns do not use and this
/// translation does not handle.
#[must_use]
pub fn py_pattern(pattern: &str) -> String {
    let mut out = String::with_capacity(pattern.len() * 4);
    let mut in_class = false;
    // Rust note: `chars()` iterates over Unicode scalar values, decoding
    // UTF-8 as it goes; `peekable()` adds a one-item look-ahead. Indexing a
    // `str` by position (`s[i]`) is not allowed at all, because a byte
    // index may fall inside a multi-byte character.
    let mut chars = pattern.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\\' => {
                let Some(e) = chars.next() else {
                    out.push('\\');
                    break;
                };
                let body = match e {
                    'w' => Some(&*WORD_BODY),
                    'd' => Some(&*DECIMAL_BODY),
                    's' => Some(&*SPACE_BODY),
                    'b' if !in_class => {
                        let w = &*WORD_BODY;
                        out.push_str(&format!("(?:(?<=[{w}])(?![{w}])|(?<![{w}])(?=[{w}]))"));
                        None
                    }
                    'W' | 'D' | 'S' | 'B' => panic!("py_pattern does not translate \\{e}"),
                    other => {
                        out.push('\\');
                        out.push(other);
                        None
                    }
                };
                if let Some(body) = body {
                    if in_class {
                        out.push_str(body);
                    } else {
                        out.push('[');
                        out.push_str(body);
                        out.push(']');
                    }
                }
            }
            '[' if !in_class => {
                in_class = true;
                out.push('[');
                // `[^...]` and a leading `]` are part of the class opening.
                if chars.peek() == Some(&'^') {
                    out.push(chars.next().unwrap_or('^'));
                }
                if chars.peek() == Some(&']') {
                    out.push(chars.next().unwrap_or(']'));
                }
            }
            ']' if in_class => {
                in_class = false;
                out.push(']');
            }
            _ => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tables_and_shortcuts_agree() {
        for c in (0..0x3001u32).filter_map(char::from_u32) {
            assert_eq!(py_isspace(c), in_table(SPACE, c), "{c:?}");
            assert_eq!(py_isdigit(c), in_table(DIGIT, c), "{c:?}");
        }
    }

    #[test]
    fn python_and_rust_differ_where_expected() {
        let w = fancy_regex::Regex::new(&py_pattern(r"^\w$")).unwrap();
        assert!(w.is_match("é").unwrap() && w.is_match("_").unwrap() && w.is_match("²").unwrap());
        assert!(!w.is_match("\u{301}").unwrap()); // a combining mark: Rust's \w has it, Python's not
        let b = fancy_regex::Regex::new(&py_pattern(r"\bab\b")).unwrap();
        assert!(b.is_match("x ab y").unwrap() && !b.is_match("xab").unwrap());
    }

    #[test]
    fn classes_inside_brackets() {
        let p = fancy_regex::Regex::new(&py_pattern(r"^[^\s'<]+$")).unwrap();
        assert!(p.is_match("abc").unwrap() && !p.is_match("a\u{1f}b").unwrap());
        assert_eq!(py_pattern(r"a\.b\("), r"a\.b\(");
    }
}
