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

use crate::pyunicode::{DECIMAL, DIGIT, PRINTABLE, SPACE, WORD};

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

/// Python's `str.rstrip()`.
#[must_use]
pub fn py_rstrip(s: &str) -> &str {
    s.trim_end_matches(py_isspace)
}

/// Python's `str.lstrip()`.
#[must_use]
pub fn py_lstrip(s: &str) -> &str {
    s.trim_start_matches(py_isspace)
}

/// The value of a decimal digit (`re`'s `\d`), as Python's `int()` reads
/// it: `٣` is 3. Every run of decimal digits in Unicode is 0 to 9 in code
/// point order, so the value is the distance from the start of its run.
#[must_use]
pub fn py_decimal_value(c: char) -> Option<u32> {
    if c.is_ascii_digit() {
        return Some(u32::from(c) - u32::from('0'));
    }
    let cp = u32::from(c);
    let at = DECIMAL.partition_point(|&(_, hi)| hi < cp);
    DECIMAL
        .get(at)
        .filter(|&&(lo, _)| lo <= cp)
        .map(|&(lo, _)| (cp - lo) % 10)
}

/// Python's `int(s)` for a string of decimal digits with an optional
/// leading `-` (what pare's patterns capture); `None` if it is not one.
#[must_use]
pub fn py_int(s: &str) -> Option<i64> {
    let (neg, digits) = s.strip_prefix('-').map_or((false, s), |d| (true, d));
    if digits.is_empty() {
        return None;
    }
    let mut n: i64 = 0;
    for c in digits.chars() {
        n = n
            .checked_mul(10)?
            .checked_add(i64::from(py_decimal_value(c)?))?;
    }
    Some(if neg { -n } else { n })
}

/// Python's `str.isupper()`: at least one uppercase letter, and no
/// lowercase ones (`"ERROR"`, `"E2E"`; not `"Error"`, not `"42"`).
#[must_use]
pub fn py_isupper(s: &str) -> bool {
    !s.chars().any(char::is_lowercase) && s.chars().any(char::is_uppercase)
}

/// Python's `str.isprintable()` for one character, which `repr()` uses.
#[must_use]
pub fn py_isprintable(c: char) -> bool {
    if c.is_ascii() {
        return (' '..='~').contains(&c);
    }
    in_table(PRINTABLE, c)
}

/// Python's `repr()` of a string, as in its error messages
/// (`f"... {line!r}"`).
///
/// ```
/// use pare::pyclass::py_repr;
/// assert_eq!(py_repr("it's"), r#""it's""#);
/// assert_eq!(py_repr("a\tb\u{7f}\u{200b}é"), r"'a\tb\x7f\u200bé'");
/// ```
#[must_use]
pub fn py_repr(s: &str) -> String {
    use std::fmt::Write;
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if py_isprintable(c) => out.push(c),
            c if u32::from(c) <= 0xff => {
                let _ = write!(out, "\\x{:02x}", u32::from(c));
            }
            c if u32::from(c) <= 0xffff => {
                let _ = write!(out, "\\u{:04x}", u32::from(c));
            }
            c => {
                let _ = write!(out, "\\U{:08x}", u32::from(c));
            }
        }
    }
    out.push(quote);
    out
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

/// The letters Python's case-insensitive matching takes for `i`: Rust's
/// does not equate the Turkish `İ` (U+0130) and `ı` (U+0131) with `i`, and
/// those are the only characters on which the two differ (measured over all
/// of Unicode against Python 3.13).
const PY_CASE_I: &str = r"\x{130}\x{131}";

/// A pattern written in Python's syntax, with Python's meaning for `\w`,
/// `\d`, `\s`, `\W`, `\D`, `\S`, `\b` and case-insensitive matching,
/// rewritten for Rust's `regex` and `fancy-regex` crates.
///
/// - Outside a bracket class, `\w` becomes `[...]` with Python's word
///   characters, `\W` the negated class; inside one (`[\w./-]`), `\w`
///   contributes its members. `\d` and `\s` likewise.
/// - `\b` becomes a lookaround on Python's `\w`.
/// - Where the pattern is case-insensitive (`(?i)` at its start, or inside
///   `(?i:...)`), a literal `i` or `I` also matches `İ` and `ı`, and so does
///   a bracket class that covers `i` or `I`, as in Python. Python's classes
///   are not widened by case-insensitivity, while Rust's would be (Rust
///   folds the case of every member of a class), so there they are wrapped
///   in `(?-i:...)`.
/// - `\Z` (end of text in Python) becomes Rust's `\z`.
///
/// Everything else is copied as is: the patterns pare uses mean the same in
/// both engines apart from these (leftmost-first alternation, greedy and
/// lazy quantifiers).
///
/// ```
/// use pare::pyclass::py_pattern;
/// let p = py_pattern(r"[\d.]+");
/// assert!(p.starts_with(r"[\x{30}-\x{39}") && p.ends_with(".]+"));
/// assert_eq!(py_pattern("(?i:id)A"), r"(?i:[iI\x{130}\x{131}]d)A");
/// ```
///
/// # Panics
///
/// On a pattern [`try_py_pattern`] does not translate. pare's built-in
/// patterns all translate; patterns from a vocabulary file go through
/// [`try_py_pattern`].
#[must_use]
pub fn py_pattern(pattern: &str) -> String {
    try_py_pattern(pattern).unwrap_or_else(|e| panic!("py_pattern({pattern:?}): {e}"))
}

/// [`py_pattern`], or why the pattern cannot be translated: `\B`; `\W`,
/// `\D` or `\S` inside a bracket class; or, where the pattern is
/// case-insensitive, a bracket class that mixes `\w`, `\d` or `\s` with
/// letters.
///
/// # Errors
///
/// The reason, as a message for the user.
// Rust note: the translator walks a `Vec<char>` by index rather than with
// an iterator because it needs to look a few characters ahead (group
// headers like `(?<=`), and a `Vec<char>` makes that a plain index; the
// pattern is short, so collecting its characters costs nothing.
pub fn try_py_pattern(pattern: &str) -> Result<String, String> {
    let p: Vec<char> = pattern.chars().collect();
    let mut out = String::with_capacity(pattern.len() * 4);
    let mut i = 0;
    // Whether each open group is case-insensitive; the bottom entry is the
    // whole pattern's setting, from leading global flags.
    let mut ci: Vec<bool> = vec![false];
    if let Some((flags, end)) = flag_group(&p, 0).filter(|&(_, end)| p.get(end) == Some(&')')) {
        ci[0] = flags.on.contains('i');
        out.extend(&p[..=end]);
        i = end + 1;
    }
    while i < p.len() {
        let c = p[i];
        let here = *ci.last().unwrap_or(&false);
        match c {
            '\\' => {
                let Some(&e) = p.get(i + 1) else {
                    out.push('\\');
                    break;
                };
                match e {
                    'w' => push_class(&mut out, &WORD_BODY, false, here),
                    'd' => push_class(&mut out, &DECIMAL_BODY, false, here),
                    's' => push_class(&mut out, &SPACE_BODY, false, here),
                    'W' => push_class(&mut out, &WORD_BODY, true, here),
                    'D' => push_class(&mut out, &DECIMAL_BODY, true, here),
                    'S' => push_class(&mut out, &SPACE_BODY, true, here),
                    'b' => {
                        let w = &*WORD_BODY;
                        let b = format!("(?:(?<=[{w}])(?![{w}])|(?<![{w}])(?=[{w}]))");
                        push_unfolded(&mut out, &b, here);
                    }
                    'B' => return Err("\\B is not supported".into()),
                    'Z' => out.push_str(r"\z"),
                    other => {
                        out.push('\\');
                        out.push(other);
                    }
                }
                i += 2;
            }
            '[' => i = class(&p, i, here, &mut out)?,
            '(' => {
                if p.get(i + 1) == Some(&'?') {
                    if let Some((flags, end)) =
                        flag_group(&p, i).filter(|&(_, end)| p.get(end) == Some(&':'))
                    {
                        // (?i:...), (?-i:...): a scoped flag change.
                        let next = if flags.on.contains('i') {
                            true
                        } else if flags.off.contains('i') {
                            false
                        } else {
                            here
                        };
                        ci.push(next);
                        out.extend(&p[i..=end]);
                        i = end + 1;
                        continue;
                    }
                    // (?:, (?=, (?!, (?<=, (?<!, (?P<name>, (?<name>: copy the
                    // header whole, so a group name is not read as letters.
                    let mut end = i + 2;
                    if matches!(p.get(i + 2), Some('P' | '<'))
                        && !matches!(p.get(i + 3), Some('=' | '!'))
                    {
                        while end < p.len() && p[end] != '>' {
                            end += 1;
                        }
                    } else if p.get(i + 2) == Some(&'<') {
                        end = i + 3;
                    }
                    let end = end.min(p.len() - 1);
                    out.extend(&p[i..=end]);
                    ci.push(here);
                    i = end + 1;
                } else {
                    out.push('(');
                    ci.push(here);
                    i += 1;
                }
            }
            ')' => {
                if ci.len() > 1 {
                    ci.pop();
                }
                out.push(')');
                i += 1;
            }
            'i' | 'I' if here => {
                out.push_str(&format!("[iI{PY_CASE_I}]"));
                i += 1;
            }
            _ => {
                out.push(c);
                i += 1;
            }
        }
    }
    Ok(out)
}

/// Flags of a `(?...)` group header starting at `p[at]`: the letters turned
/// on and off, and the index of the character after them (`:` or `)`).
struct Flags {
    on: String,
    off: String,
}

fn flag_group(p: &[char], at: usize) -> Option<(Flags, usize)> {
    if p.get(at) != Some(&'(') || p.get(at + 1) != Some(&'?') {
        return None;
    }
    let mut flags = Flags {
        on: String::new(),
        off: String::new(),
    };
    let mut j = at + 2;
    let mut negative = false;
    while let Some(&c) = p.get(j) {
        match c {
            'a' | 'i' | 'L' | 'm' | 's' | 'u' | 'x' => {
                if negative {
                    flags.off.push(c)
                } else {
                    flags.on.push(c)
                }
            }
            '-' => negative = true,
            _ => break,
        }
        j += 1;
    }
    (j > at + 2).then_some((flags, j))
}

/// Appends `piece`, shielded from case-insensitivity when `ci` is set.
fn push_unfolded(out: &mut String, piece: &str, ci: bool) {
    if ci {
        out.push_str("(?-i:");
        out.push_str(piece);
        out.push(')');
    } else {
        out.push_str(piece);
    }
}

/// `\w` and friends outside a bracket class: the class, or its negation.
fn push_class(out: &mut String, body: &str, negated: bool, ci: bool) {
    let class = format!("{}{body}]", if negated { "[^" } else { "[" });
    push_unfolded(out, &class, ci);
}

/// Whether a character has another case, so that a case-insensitive
/// pattern widens it.
fn cased(c: char) -> bool {
    c.is_lowercase() || c.is_uppercase()
}

/// Copies the bracket class starting at `p[at]` (a `[`) to `out`, with
/// Python's classes spelled out and, when case-insensitive and the class
/// covers `i` or `I`, `İ` and `ı` added. Returns the index after its `]`.
fn class(p: &[char], at: usize, ci: bool, out: &mut String) -> Result<usize, String> {
    let mut j = at + 1;
    let mut body = String::from("[");
    if p.get(j) == Some(&'^') {
        body.push('^');
        j += 1;
    }
    let mut covers_i = false;
    let mut has_cased = false; // a literal member with another case
    let mut has_class = false; // \w, \d or \s
    let mut prev: Option<char> = None; // the last literal, a range's possible start
    if p.get(j) == Some(&']') {
        body.push(']');
        prev = Some(']');
        j += 1;
    }
    while let Some(&c) = p.get(j) {
        match c {
            ']' => break,
            '\\' => {
                let e = p.get(j + 1).copied().unwrap_or('\\');
                match e {
                    'w' => body.push_str(&WORD_BODY),
                    'd' => body.push_str(&DECIMAL_BODY),
                    's' => body.push_str(&SPACE_BODY),
                    'W' | 'D' | 'S' => {
                        return Err(format!("\\{e} inside a [...] class is not supported"));
                    }
                    _ => {
                        body.push('\\');
                        body.push(e);
                    }
                }
                has_class |= matches!(e, 'w' | 'd' | 's');
                // An escaped punctuation character is a literal that can
                // start a range; a class escape cannot.
                prev = (!e.is_ascii_alphanumeric()).then_some(e);
                j += 2;
            }
            '-' if prev.is_some() && !matches!(p.get(j + 1), Some(']') | None) => {
                let (hi, len) = match p.get(j + 1) {
                    Some('\\') => (p.get(j + 2).copied().unwrap_or('\\'), 3),
                    Some(&h) => (h, 2),
                    None => unreachable!("checked above"),
                };
                let lo = prev.unwrap_or(hi);
                covers_i |= (lo..=hi).contains(&'i') || (lo..=hi).contains(&'I');
                has_cased |= (lo..=hi).any(cased);
                body.extend(&p[j..j + len]);
                prev = None;
                j += len;
            }
            _ => {
                covers_i |= c == 'i' || c == 'I';
                has_cased |= cased(c);
                body.push(c);
                prev = Some(c);
                j += 1;
            }
        }
    }
    if ci && has_class {
        // Python's \w, \d, \s ignore case-insensitivity, Rust's would not,
        // and a Rust class takes one setting for all its members.
        if has_cased {
            return Err("a case-insensitive [...] class mixing \\w, \\d or \\s with letters is not supported".into());
        }
        body.push(']');
        push_unfolded(out, &body, true);
    } else {
        if ci && covers_i {
            body.push_str(PY_CASE_I);
        }
        body.push(']');
        out.push_str(&body);
    }
    Ok(j + 1)
}

/// The pattern with its assertions erased: `\b`, lookahead and lookbehind
/// groups removed, in Python syntax (before [`py_pattern`]). Erasing an
/// assertion only drops a condition, so the result matches wherever the
/// original can start a match, and more; [`crate::pyre::PyRegex`] scans
/// with it in the fast engine and checks the original only where it
/// matches.
///
/// ```
/// use pare::pyclass::erase_assertions;
/// assert_eq!(erase_assertions(r"(?<![\w.])\d+(?![\w(])\b"), r"\d+");
/// assert_eq!(erase_assertions(r"[(?=]x"), r"[(?=]x");
/// ```
#[must_use]
pub fn erase_assertions(pattern: &str) -> String {
    let p: Vec<char> = pattern.chars().collect();
    let mut out = String::with_capacity(pattern.len());
    let mut i = 0;
    while i < p.len() {
        match p[i] {
            '\\' if matches!(p.get(i + 1), Some('b' | 'B')) => i += 2,
            '\\' => {
                out.extend(&p[i..(i + 2).min(p.len())]);
                i += 2;
            }
            '[' => {
                let end = class_end(&p, i);
                out.extend(&p[i..end]);
                i = end;
            }
            '(' if is_lookaround(&p, i) => i = group_end(&p, i),
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    out
}

fn is_lookaround(p: &[char], at: usize) -> bool {
    p.get(at + 1) == Some(&'?')
        && (matches!(p.get(at + 2), Some('=' | '!'))
            || (p.get(at + 2) == Some(&'<') && matches!(p.get(at + 3), Some('=' | '!'))))
}

/// The index after the bracket class starting at `p[at]`.
fn class_end(p: &[char], at: usize) -> usize {
    let mut j = at + 1;
    if p.get(j) == Some(&'^') {
        j += 1;
    }
    if p.get(j) == Some(&']') {
        j += 1;
    }
    while j < p.len() && p[j] != ']' {
        j += if p[j] == '\\' { 2 } else { 1 };
    }
    (j + 1).min(p.len())
}

/// The index after the group starting at `p[at]` (a `(`).
fn group_end(p: &[char], at: usize) -> usize {
    let mut depth = 0;
    let mut j = at;
    while j < p.len() {
        match p[j] {
            '\\' => j += 2,
            '[' => j = class_end(p, j),
            '(' => {
                depth += 1;
                j += 1;
            }
            ')' => {
                depth -= 1;
                j += 1;
                if depth == 0 {
                    return j;
                }
            }
            _ => j += 1,
        }
    }
    p.len()
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
    fn case_insensitive_scopes_widen_i() {
        // Only inside (?i) or (?i:...), and classes only when they cover i.
        assert_eq!(py_pattern("(?i)ab"), "(?i)ab");
        assert_eq!(py_pattern("(?i)hi"), r"(?i)h[iI\x{130}\x{131}]");
        assert_eq!(py_pattern("I(?i:I)I"), r"I(?i:[iI\x{130}\x{131}])I");
        assert_eq!(py_pattern("(?i:[a-z-])"), r"(?i:[a-z-\x{130}\x{131}])");
        assert_eq!(py_pattern("(?i:[0-9])"), "(?i:[0-9])");
        assert_eq!(py_pattern("[a-z]"), "[a-z]");
        let re = regex::Regex::new(&py_pattern("(?i)^info$")).unwrap();
        assert!(re.is_match("\u{130}NFO") && re.is_match("\u{131}nfo") && re.is_match("InFo"));
        let lb = fancy_regex::Regex::new(&py_pattern("(?i:(?<![a-z-])err)")).unwrap();
        assert!(!lb.is_match("\u{131}err").unwrap()); // ı is a letter to Python's (?i)[a-z]
        assert!(lb.is_match("1err").unwrap());
    }

    #[test]
    fn negated_classes_and_group_headers() {
        let w = regex::Regex::new(&py_pattern(r"^\W+$")).unwrap();
        assert!(w.is_match(" -[]") && !w.is_match("a"));
        // Group names and lookbehind headers are not letters to widen.
        assert_eq!(py_pattern("(?i)(?P<id>x)"), "(?i)(?P<id>x)");
        assert_eq!(py_pattern("(?i)(?<!i)"), r"(?i)(?<![iI\x{130}\x{131}])");
    }

    #[test]
    fn python_classes_ignore_case_insensitivity() {
        // U+0345 folds to a Greek iota in Rust's (?i), but is not \w in Python.
        let w = regex::Regex::new(&py_pattern(r"(?i)^\w$")).unwrap();
        assert!(!w.is_match("\u{345}") && w.is_match("x"));
        let c = regex::Regex::new(&py_pattern(r"(?i)^[\s,]$")).unwrap();
        assert!(c.is_match(",") && c.is_match("\u{1f}"));
        assert!(try_py_pattern(r"(?i)[\wk]").is_err());
        assert!(try_py_pattern(r"\B").is_err());
        assert_eq!(py_pattern(r"a\Z"), r"a\z");
    }

    #[test]
    fn assertions_erased() {
        assert_eq!(
            erase_assertions(r"(?i:(?<![a-z-])(?:err|warn)(?![a-z-]))"),
            "(?i:(?:err|warn))"
        );
        assert_eq!(
            erase_assertions(r"(?=[0-9a-f]*\d)[0-9a-f]{7}"),
            "[0-9a-f]{7}"
        );
        assert_eq!(erase_assertions(r"(?=(a)\))b"), "b");
        assert_eq!(erase_assertions(r"\\b"), r"\\b"); // an escaped backslash, then b
    }

    #[test]
    fn digits_and_case() {
        assert_eq!(py_int("٣٠٢"), Some(302));
        assert_eq!(py_int("-12"), Some(-12));
        assert_eq!(py_int("1a"), None);
        assert!(py_isupper("E2E") && !py_isupper("Error") && !py_isupper("42"));
        assert!(py_isupper("\u{130}NFO"));
    }

    #[test]
    fn classes_inside_brackets() {
        let p = fancy_regex::Regex::new(&py_pattern(r"^[^\s'<]+$")).unwrap();
        assert!(p.is_match("abc").unwrap() && !p.is_match("a\u{1f}b").unwrap());
        assert_eq!(py_pattern(r"a\.b\("), r"a\.b\(");
    }
}
