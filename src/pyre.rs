//! Python regular expressions, matched as Python's `re` matches them, at
//! the speed of Rust's linear-time `regex` crate where possible.
//!
//! pare's patterns are written in Python's syntax (`reference/pare.py` is the
//! reference) and use what `re` has and the `regex` crate lacks:
//! lookahead, lookbehind, and `\b` on Python's word characters (see
//! [`crate::pyclass`]). The backtracking `fancy-regex` crate has all of
//! that, but scanning every line with it is slow: before this design, the
//! pass was slower than the Python version.
//!
//! So a [`PyRegex`] uses two engines. The `regex` crate scans the line for
//! the pattern with its assertions erased
//! ([`crate::pyclass::erase_assertions`]), which matches wherever the real
//! pattern can start a match, and more. Only at a place it finds does
//! `fancy-regex` try the real pattern, anchored there with `\G` (it still
//! sees the text before, for lookbehind). If it does not match there, the
//! scan resumes one character later. Most lines have no candidate at all
//! and never reach the slow engine.
//!
//! A pattern without assertions runs on the `regex` crate alone; one the
//! `regex` crate cannot compile even relaxed (a back-reference, say, from a
//! vocabulary file) runs on `fancy-regex` alone.

use std::borrow::Cow;
use std::sync::OnceLock;

use crate::pyclass::{erase_assertions, try_py_pattern};

/// A compiled Python pattern. See the module documentation.
#[derive(Debug)]
pub struct PyRegex {
    engine: Engine,
}

// Rust note: an `enum` whose variants carry data is a tagged union, like
// `std::variant`, but with names and with `match` checking that every
// case is handled. Each `PyRegex` is one of these three shapes.
#[derive(Debug)]
enum Engine {
    /// No assertions: the `regex` crate gives Python's result by itself.
    Linear(regex::Regex),
    /// `fast` finds candidates; `exact` (`\G(?:pattern)`, compiled from
    /// `source` when the first candidate turns up) decides at each.
    TwoStage {
        fast: regex::Regex,
        source: String,
        exact: OnceLock<fancy_regex::Regex>,
    },
    /// Neither form compiles for the `regex` crate: backtracking only.
    Backtrack(fancy_regex::Regex),
}

/// The backtracking engine's step limit. Python's `re` has none; pare's
/// patterns never backtrack far, so a generous limit only guards against
/// a pathological pattern from a vocabulary file, which then counts as
/// not matching instead of hanging.
const BACKTRACK_LIMIT: usize = 100_000_000;

/// Capture groups of a match, as byte ranges; group 0 is the whole match.
pub type Groups = Vec<Option<(usize, usize)>>;

impl PyRegex {
    /// Compiles a pattern written in Python's syntax.
    ///
    /// # Errors
    ///
    /// A message if the pattern cannot be translated or compiled.
    pub fn new(pattern: &str) -> Result<Self, String> {
        let exact = try_py_pattern(pattern)?;
        if let Ok(rx) = regex::Regex::new(&exact) {
            return Ok(PyRegex {
                engine: Engine::Linear(rx),
            });
        }
        let relaxed = try_py_pattern(&erase_assertions(pattern))
            .ok()
            .and_then(|p| regex::Regex::new(&p).ok());
        let engine = match relaxed {
            // Python's classes make these patterns large (`\b` alone is four
            // copies of the word class, 50 KB), and `fancy-regex` takes
            // milliseconds to compile one. Most never see a candidate on a
            // short input, so the exact half is compiled on first use.
            Some(fast) => Engine::TwoStage {
                fast,
                source: format!(r"\G(?:{exact})"),
                exact: OnceLock::new(),
            },
            None => Engine::Backtrack(fancy(&exact)?),
        };
        Ok(PyRegex { engine })
    }

    /// Compiles whatever [`PyRegex::new`] left for first use, to report an
    /// error now: for patterns from a vocabulary file.
    ///
    /// # Errors
    ///
    /// The backtracking engine's message, if the pattern does not compile.
    pub fn validate(&self) -> Result<(), String> {
        match &self.engine {
            Engine::TwoStage { source, .. } => fancy(source).map(|_| ()),
            _ => Ok(()),
        }
    }

    /// The exact half of a two-stage pattern, compiled on first use.
    // Rust note: `OnceLock::get_or_init` runs the closure once, even with
    // several threads asking at the same time, and hands out a shared
    // reference afterwards: a lazily initialised member without a mutex
    // on every access. `&self` suffices because `OnceLock` provides the
    // interior mutability, like a `mutable std::once_flag` pair in C++.
    fn exact<'a>(source: &str, cell: &'a OnceLock<fancy_regex::Regex>) -> &'a fancy_regex::Regex {
        cell.get_or_init(|| fancy(source).unwrap_or_else(|e| panic!("{source:?} compiles: {e}")))
    }

    /// Python's `rx.search(text, pos)`, with its groups: the leftmost
    /// match starting at or after byte `pos`.
    #[must_use]
    pub fn captures_at(&self, text: &str, pos: usize) -> Option<Groups> {
        let groups = |len: usize, get: &dyn Fn(usize) -> Option<(usize, usize)>| {
            (0..len).map(get).collect::<Groups>()
        };
        match &self.engine {
            Engine::Linear(rx) => rx
                .captures_at(text, pos)
                .map(|c| groups(c.len(), &|g| c.get(g).map(|m| (m.start(), m.end())))),
            Engine::Backtrack(rx) => rx
                .captures_from_pos(text, pos)
                .ok()
                .flatten()
                .map(|c| groups(c.len(), &|g| c.get(g).map(|m| (m.start(), m.end())))),
            Engine::TwoStage {
                fast,
                source,
                exact,
            } => {
                let mut pos = pos;
                loop {
                    let start = fast.find_at(text, pos)?.start();
                    let exact = Self::exact(source, exact);
                    if let Some(c) = exact.captures_from_pos(text, start).ok().flatten() {
                        return Some(groups(c.len(), &|g| c.get(g).map(|m| (m.start(), m.end()))));
                    }
                    pos = next_char(text, start)?;
                }
            }
        }
    }

    /// The leftmost match at or after byte `pos`, as a byte range.
    #[must_use]
    pub fn find_at(&self, text: &str, pos: usize) -> Option<(usize, usize)> {
        match &self.engine {
            Engine::Linear(rx) => rx.find_at(text, pos).map(|m| (m.start(), m.end())),
            Engine::Backtrack(rx) => rx
                .find_from_pos(text, pos)
                .ok()
                .flatten()
                .map(|m| (m.start(), m.end())),
            Engine::TwoStage {
                fast,
                source,
                exact,
            } => {
                let mut pos = pos;
                loop {
                    let start = fast.find_at(text, pos)?.start();
                    let exact = Self::exact(source, exact);
                    if let Some(m) = exact.find_from_pos(text, start).ok().flatten() {
                        return Some((m.start(), m.end()));
                    }
                    pos = next_char(text, start)?;
                }
            }
        }
    }

    /// Python's `bool(rx.search(text))`.
    #[must_use]
    pub fn is_match(&self, text: &str) -> bool {
        match &self.engine {
            Engine::Linear(rx) => rx.is_match(text),
            _ => self.find_at(text, 0).is_some(),
        }
    }

    /// Python's `rx.match(text)`: a match that starts at the beginning.
    #[must_use]
    pub fn match_start(&self, text: &str) -> Option<(usize, usize)> {
        // The leftmost match starts at 0 if any match does.
        self.find_at(text, 0).filter(|&(s, _)| s == 0)
    }

    /// Python's `rx.finditer(text)`: the non-overlapping matches, left to
    /// right, with their groups.
    pub fn captures_iter<'a>(&'a self, text: &'a str) -> impl Iterator<Item = Groups> + 'a {
        let mut pos = Some(0);
        std::iter::from_fn(move || {
            let groups = self.captures_at(text, pos?)?;
            let (s, e) = groups[0].expect("group 0 is the match");
            // After an empty match, the next one starts a character later.
            pos = if e > s { Some(e) } else { next_char(text, e) };
            Some(groups)
        })
    }

    /// Python's `rx.sub(replace, text)`: every non-overlapping match, left
    /// to right, replaced by what `replace` makes of it. `None` when
    /// nothing matched, so the caller can keep the text it has.
    pub fn replace_all(
        &self,
        text: &str,
        replace: impl Fn(&str) -> Cow<'static, str>,
    ) -> Option<String> {
        let mut out: Option<String> = None;
        let mut last = 0;
        let mut pos = Some(0);
        while let Some((start, end)) = pos.and_then(|p| self.find_at(text, p)) {
            let o = out.get_or_insert_with(|| String::with_capacity(text.len()));
            o.push_str(&text[last..start]);
            o.push_str(&replace(&text[start..end]));
            last = end;
            pos = if end > start {
                Some(end)
            } else {
                next_char(text, end)
            };
        }
        out.map(|mut o| {
            o.push_str(&text[last..]);
            o
        })
    }
}

/// Compiles a pattern for the backtracking engine.
fn fancy(pattern: &str) -> Result<fancy_regex::Regex, String> {
    fancy_regex::RegexBuilder::new(pattern)
        .backtrack_limit(BACKTRACK_LIMIT)
        .build()
        .map_err(|e| e.to_string())
}

/// The byte index after the character at `at`, or `None` at the end.
fn next_char(text: &str, at: usize) -> Option<usize> {
    text[at..].chars().next().map(|c| at + c.len_utf8())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn engines_are_chosen_by_what_compiles() {
        assert!(matches!(
            PyRegex::new(r"\d+").unwrap().engine,
            Engine::Linear(_)
        ));
        assert!(matches!(
            PyRegex::new(r"\bx").unwrap().engine,
            Engine::TwoStage { .. }
        ));
        assert!(matches!(
            PyRegex::new(r"(a)\1").unwrap().engine,
            Engine::Backtrack(_)
        ));
        assert!(PyRegex::new(r"(").is_err());
        assert!(PyRegex::new(r"(?<![a-z])b").unwrap().validate().is_ok());
    }

    #[test]
    fn two_stage_matches_as_backtracking_alone() {
        // The candidate at 0 fails the lookbehind-free check; the real match is later.
        let rx = PyRegex::new(r"(?<![a-z])err(?![a-z])").unwrap();
        assert_eq!(rx.find_at("xerr err", 0), Some((5, 8)));
        assert!(!rx.is_match("errors terror"));
        let groups = PyRegex::new(r"\b(?:GET)\s+\S+\s+(\d{3})\b")
            .unwrap()
            .captures_at("GET /a 503", 0);
        assert_eq!(groups.unwrap()[1], Some((7, 10)));
    }

    #[test]
    fn finditer_and_sub() {
        let rx = PyRegex::new(r"(?<=\[)(\w+)(?=\])").unwrap();
        let found: Vec<_> = rx
            .captures_iter("[a] [bc] [")
            .map(|g| g[1].unwrap())
            .collect();
        assert_eq!(found, [(1, 2), (5, 7)]);
        let sub = rx.replace_all("[a] [bc]", |_| Cow::Borrowed("X"));
        assert_eq!(sub.as_deref(), Some("[X] [X]"));
        assert_eq!(rx.replace_all("none", |_| Cow::Borrowed("X")), None);
        assert_eq!(
            PyRegex::new("x*")
                .unwrap()
                .replace_all("ab", |_| Cow::Borrowed("-"))
                .as_deref(),
            Some("-a-b-")
        );
    }
}
