//! Masking the variable parts of a line: `MASKS` and `mask()` in
//! `proto/pare.py`.
//!
//! Before a record's head line is clustered, the parts that differ between
//! otherwise identical lines (timestamps, dates, ids, addresses, versions,
//! paths, numbers) are replaced by placeholders such as `<TS>` and `<NUM>`,
//! so that `ERROR [worker-3] GET /api/cart 500 1137ms` and the same line a
//! second later from worker 5 look alike. The masks run in a fixed order,
//! each on what the previous ones left.

use std::borrow::Cow;
use std::sync::LazyLock;

use crate::pyclass::{py_isdecimal, py_split, py_strip};
use crate::pyre::PyRegex;

/// One mask's definition.
struct Spec {
    /// The placeholder name: `TS` masks to `<TS>`.
    name: &'static str,
    /// Python's pattern, as written in `proto/pare.py`.
    exact: &'static str,
    /// The line must contain a decimal digit for the mask to apply.
    needs_digit: bool,
    /// ... and one of these strings (empty: no such test).
    needs_any: &'static [&'static str],
}

const MONTHS_AND_SEPARATORS: &[&str] = &[
    "-", "/", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// The masks after `RULE` (which is a hand-written loop, see [`rules`]),
/// in Python's order.
// Rust note: `concat!` joins string literals at compile time, so a long
// pattern can be split over lines without any run-time cost, and the
// result is still a `&'static str`. Raw strings (`r"..."`) take
// backslashes literally, as Python's `r"..."` does.
const SPECS: &[Spec] = &[
    Spec {
        name: "TS",
        exact: r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        needs_digit: true,
        needs_any: &["-"],
    },
    Spec {
        name: "DATE",
        exact: concat!(
            r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b",
            r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),? (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2}\b",
            r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2},? \d{4}\b",
        ),
        needs_digit: true,
        needs_any: MONTHS_AND_SEPARATORS,
    },
    Spec {
        name: "TIME",
        exact: r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b",
        needs_digit: true,
        needs_any: &[":"],
    },
    Spec {
        name: "UUID",
        exact: r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        needs_digit: false,
        needs_any: &["-"],
    },
    Spec {
        name: "IP",
        exact: r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b",
        needs_digit: true,
        needs_any: &["."],
    },
    Spec {
        name: "URL",
        exact: r#"\b[a-z][a-z0-9+.-]*://[^\s'"<>]+"#,
        needs_digit: false,
        needs_any: &["://"],
    },
    Spec {
        name: "HEX",
        exact: r"\b0[xX][0-9a-fA-F]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b",
        needs_digit: true,
        needs_any: &[],
    },
    Spec {
        name: "VER",
        exact: r"\bv?\d+\.\d+(?:\.\d+)+(?:[-+][\w.]+)?\b",
        needs_digit: true,
        needs_any: &["."],
    },
    Spec {
        name: "PATH",
        exact: concat!(
            r"(?=[\w./+@%~-]*[A-Za-z])", // must contain a letter (so 1/30 is not a path)
            r"(?:(?:~|\.{1,2})?(?:/[\w.+@%~-]+)+/?", // absolute or ./relative
            r"|\b[\w.+-]+(?:/[\w.+@%~-]+)+\b)", // a/b/c
        ),
        needs_digit: false,
        needs_any: &["/"],
    },
    Spec {
        name: "NUM",
        exact: concat!(
            r"(?<![\w.])[-+]?\d+(?:[.,]\d+)*",
            r"(?:\s?(?:ms|us|µs|ns|secs?|s|mins?|m|h|[KMGT]i?B|[kKMG]?b|B|%|px|x))?(?![\w.])",
        ),
        needs_digit: true,
        needs_any: &[],
    },
    Spec {
        name: "WORDNUM",
        exact: r"\b[A-Za-z_]+\d+[A-Za-z0-9_]*\b",
        needs_digit: true,
        needs_any: &[],
    },
];

/// A compiled mask.
struct Mask {
    spec: &'static Spec,
    rx: PyRegex,
}

// Rust note: `LazyLock` compiles the masks on first use, once, thread-safely
// (like a function-local `static` in C++). See `crate::pyre` for how a
// Python pattern with lookaround runs fast.
static MASKS: LazyLock<Vec<Mask>> = LazyLock::new(|| {
    SPECS
        .iter()
        .map(|spec| Mask {
            spec,
            rx: PyRegex::new(spec.exact).expect("built-in mask patterns are valid"),
        })
        .collect()
});

/// Rule runs (`=====`, `_____`, `-----`, ...) shrink to three characters,
/// so banners of different widths share a template. Python does this with
/// the pattern `([=_\-*#~.])\1{2,}`, whose back-reference neither Rust
/// regex crate's fast engine supports; a loop over the characters is
/// simpler and does the same.
// Rust note: returns `Cow::Borrowed` (no allocation) when the line has no
// run to shrink, which is most lines.
fn rules(t: &str) -> Cow<'_, str> {
    const RULE_CHARS: &str = "=_-*#~.";
    let has_run = RULE_CHARS
        .chars()
        .any(|c| t.contains(&*std::iter::repeat_n(c, 3).collect::<String>()));
    if !has_run {
        return Cow::Borrowed(t);
    }
    let mut out = String::with_capacity(t.len());
    let mut chars = t.chars().peekable();
    while let Some(c) = chars.next() {
        if !RULE_CHARS.contains(c) {
            out.push(c);
            continue;
        }
        let mut run = 1;
        while chars.peek() == Some(&c) {
            chars.next();
            run += 1;
        }
        for _ in 0..run.min(3) {
            out.push(c);
        }
    }
    Cow::Owned(out)
}

/// Python's `re.sub(r"\d+", "<N>", s)`: each run of decimal digits becomes
/// `<N>` (`worker3` → `worker<N>`).
fn digits_to_n(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 4);
    let mut in_digits = false;
    for c in s.chars() {
        if py_isdecimal(c) {
            if !in_digits {
                out.push_str("<N>");
                in_digits = true;
            }
        } else {
            in_digits = false;
            out.push(c);
        }
    }
    out
}

/// The line with its variable parts masked. With `words` false,
/// identifiers with digits in them (`test_case_3`) are left whole: for
/// telling names apart, not for clustering.
///
/// ```
/// use pare::mask::mask;
/// assert_eq!(
///     mask("2026-10-07T09:00:01Z ERROR [worker-3] GET /api/cart 500 1137ms", true),
///     "<TS> ERROR [worker-<NUM>] GET <PATH> <NUM> <NUM>"
/// );
/// assert_eq!(mask("job42 failed ======", true), "job<N> failed ===");
/// assert_eq!(mask("job42 failed", false), "job42 failed");
/// ```
///
/// # Panics
///
/// If a mask's backtracking exceeds its (very high) limit, which would be
/// a bug in a pattern.
#[must_use]
pub fn mask(text: &str, words: bool) -> String {
    let mut t: String = rules(py_strip(text)).into_owned();
    // Whether `t` has a decimal digit; worked out when a mask needs to
    // know, and forgotten when a mask changes `t`.
    let mut digit: Option<bool> = None;
    for m in MASKS.iter() {
        let spec = m.spec;
        if !spec.needs_any.is_empty() && !spec.needs_any.iter().any(|n| t.contains(n)) {
            continue;
        }
        if spec.needs_digit && !*digit.get_or_insert_with(|| t.chars().any(py_isdecimal)) {
            continue;
        }
        let new = if spec.name == "WORDNUM" {
            if !words {
                continue;
            }
            m.rx.replace_all(&t, |s| Cow::Owned(digits_to_n(s)))
        } else {
            let placeholder = format!("<{}>", spec.name);
            m.rx.replace_all(&t, |_| Cow::Owned(placeholder.clone()))
        };
        if let Some(changed) = new {
            t = changed;
            digit = None;
        }
    }
    t
}

/// Tokens of a record head that Drain sees, at most.
pub const DRAIN_MAX_TOKENS: usize = 40;

/// What Drain clusters for a record head: its masked text, at most
/// [`DRAIN_MAX_TOKENS`] tokens joined by single spaces, or `<EMPTY>`.
/// `drain_message()` in Python.
#[must_use]
pub fn drain_message(head: &str) -> String {
    let masked = mask(head, true);
    let message = py_split(&masked)
        .take(DRAIN_MAX_TOKENS)
        .collect::<Vec<_>>()
        .join(" ");
    if message.is_empty() {
        "<EMPTY>".to_owned()
    } else {
        message
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rule_runs_shrink_to_three() {
        assert_eq!(rules("=== x ======= y __"), "=== x === y __");
        assert_eq!(rules("--==--"), "--==--");
        assert_eq!(rules("......"), "...");
        assert!(matches!(rules("no runs"), Cow::Borrowed(_)));
    }

    fn unescape(s: &str) -> String {
        let mut out = String::with_capacity(s.len());
        let mut chars = s.chars();
        while let Some(c) = chars.next() {
            if c == '\\' {
                match chars.next() {
                    Some('t') => out.push('\t'),
                    Some('n') => out.push('\n'),
                    Some(other) => out.push(other),
                    None => out.push('\\'),
                }
            } else {
                out.push(c);
            }
        }
        out
    }

    /// The rows of tests/data/mask.tsv: (input, mask, mask without words).
    fn golden() -> Vec<(String, String, String)> {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/data/mask.tsv");
        std::fs::read_to_string(path)
            .expect("tests/data/mask.tsv (bench/gen_fixtures.py writes it)")
            .lines()
            .filter(|l| !l.starts_with('#'))
            .map(|l| {
                let mut f = l.split('\t').map(unescape);
                (f.next().unwrap(), f.next().unwrap(), f.next().unwrap())
            })
            .collect()
    }

    #[test]
    fn matches_python_on_real_record_heads() {
        let rows = golden();
        assert!(rows.len() > 4000);
        for (input, want, want_nowords) in &rows {
            assert_eq!(&mask(input, true), want, "input: {input:?}");
            assert_eq!(
                &mask(input, false),
                want_nowords,
                "input: {input:?} (words=false)"
            );
        }
    }

    #[test]
    fn two_engines_match_as_backtracking_alone() {
        // Each mask, scanned with candidates from the fast engine, finds
        // what Python's pattern finds scanned by the backtracking engine
        // alone, on what the mask actually sees: the line as the earlier
        // masks left it.
        let slow: Vec<fancy_regex::Regex> = SPECS
            .iter()
            .map(|s| fancy_regex::Regex::new(&crate::pyclass::py_pattern(s.exact)).unwrap())
            .collect();
        for (input, _, _) in golden().iter().take(1500) {
            let mut t = rules(py_strip(input)).into_owned();
            for (m, slow) in MASKS.iter().zip(&slow) {
                let want: Vec<_> = slow
                    .find_iter(&t)
                    .map(|f| {
                        let f = f.unwrap();
                        (f.start(), f.end())
                    })
                    .collect();
                let mut got = Vec::new();
                let mut pos = 0;
                while let Some((s, e)) = m.rx.find_at(&t, pos) {
                    got.push((s, e));
                    pos = e.max(s + 1);
                }
                assert_eq!(got, want, "{} in {t:?}", m.spec.name);
                let placeholder = format!("<{}>", m.spec.name);
                if let Some(changed) = m.rx.replace_all(&t, |_| Cow::Owned(placeholder.clone())) {
                    t = changed;
                }
            }
        }
    }

    #[test]
    fn python_classes_not_rusts() {
        // A combining accent is not a word character to Python, so the 5
        // after it is a number; to Rust's built-in `\w` it is one, and the
        // lookbehind would refuse the match. Python decides.
        assert_eq!(mask("x\u{301}5", true), "x\u{301}<NUM>");
        let native = fancy_regex::Regex::new(r"(?<![\w.])\d+").unwrap();
        assert!(!native.is_match("x\u{301}5").unwrap());
        // A superscript is a word character to Python: "5²" is not a number.
        assert_eq!(mask("5\u{b2}", true), "5\u{b2}");
    }

    #[test]
    fn examples_from_python() {
        // Expected values from proto/pare.py's mask().
        let cases = [
            ("  10.0.0.1:8080 refused  ", "<IP> refused"),
            ("see http://x.y/z?q=1 now", "see <URL> now"),
            ("deadbeef1 and 0xFF", "<HEX> and <HEX>"),
            ("v1.2.3-rc1 released", "<VER> released"),
            ("1/30 done, at ./src/a.py", "<NUM>/<NUM> done, at <PATH>"), // no year, no letter
            ("took 12.5 ms, 3x faster", "took <NUM>, <NUM> faster"),
            ("Tue Oct 7 started", "<DATE> started"),
            ("abc123def", "<HEX>"), // all hex digits, one of them a digit
        ];
        for (input, want) in cases {
            assert_eq!(mask(input, true), want, "{input:?}");
        }
    }
}
