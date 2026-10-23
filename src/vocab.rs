//! The vocabulary: what words, levels and shapes say about a line. The
//! `VOCAB_CLASSES`, `DEFAULT_VOCAB`, `Vocab`, `parse_vocab`, `dump_vocab`,
//! severity and `vocab_score` parts of `reference/pare.py`.
//!
//! A line's salience comes from two kinds of evidence:
//!
//! - **Structural severity** (0 debug to 4 fatal): an explicit log level in
//!   a level position (`ERROR`, `[warn]`, `level=info`, logcat's `E/Tag:`),
//!   an HTTP status (`GET /x 503`), or an exit code (`exited with code 1`).
//!   When present it sets the base score: an INFO line that mentions
//!   "error" is still info.
//! - **Vocabulary classes**: words and phrases (matched whole and
//!   case-insensitively) and regexes, grouped into classes with weights:
//!   error, warn, summary, frame, pass, noise, advice, and strong-error
//!   (which decides whether a record body counts as an error).
//!
//! A vocabulary file (`--vocab FILE`, `$PARE_VOCAB`) extends or replaces any
//! class; `--dump-vocab` prints the classes in that format.

use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::LazyLock;

use crate::mask::UserMask;
use crate::pyclass::{py_int, py_isupper, py_repr, py_split, py_strip};
use crate::pyre::PyRegex;

/// The vocabulary classes, in Python's order (which is also the order of
/// `--dump-vocab` and of the error message listing them).
// Rust note: a C-like enum with `#[repr(usize)]` has the integer values
// 0, 1, 2, ... so `class as usize` indexes the per-class arrays below,
// like a C++ `enum` used as an index; unlike C++, an integer does not
// convert back to the enum implicitly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(usize)]
pub enum Class {
    /// Something went wrong.
    Error,
    /// Worth a look.
    Warn,
    /// Totals, section rules, final verdicts.
    Summary,
    /// Source locations and stack frames.
    Frame,
    /// Passing-test markers.
    Pass,
    /// Progress and chatter.
    Noise,
    /// How to get more output or help.
    Advice,
    /// Whether a record body counts as an error.
    StrongError,
    /// Level tokens meaning fatal.
    LevelFatal,
    /// Level tokens meaning error.
    LevelError,
    /// Level tokens meaning warning.
    LevelWarn,
    /// Level tokens meaning info.
    LevelInfo,
    /// Level tokens meaning debug.
    LevelDebug,
    /// Regexes for lines that continue the record above: stack frames,
    /// carets, a diagnostic's detail. Not in the Python version.
    Continuation,
}

/// The number of classes.
pub const NCLASSES: usize = 14;
/// The classes that are not level classes come first; this many.
pub const NWORD_CLASSES: usize = 8;
/// The level classes, after them.
const LEVEL_CLASSES: std::ops::Range<usize> = NWORD_CLASSES..NWORD_CLASSES + 5;

/// One class's fixed properties.
struct ClassInfo {
    name: &'static str,
    weight: f64,
    /// `--dump-vocab`'s comment: the description, and the weight as
    /// Python's `f"{weight:+g}"` prints it.
    comment: &'static str,
}

const INFO: [ClassInfo; NCLASSES] = [
    ClassInfo {
        name: "error",
        weight: 3.0,
        comment: "something went wrong (weight +3)",
    },
    ClassInfo {
        name: "warn",
        weight: 1.5,
        comment: "worth a look (weight +1.5)",
    },
    ClassInfo {
        name: "summary",
        weight: 1.0,
        comment: "totals, section rules, final verdicts (weight +1)",
    },
    ClassInfo {
        name: "frame",
        weight: 1.0,
        comment: "source locations and stack frames (weight +1)",
    },
    ClassInfo {
        name: "pass",
        weight: -1.0,
        comment: "passing-test markers (ignored when an error word is also present) (weight -1)",
    },
    ClassInfo {
        name: "noise",
        weight: -0.75,
        comment: "progress and chatter (ignored when an error word is also present) (weight -0.75)",
    },
    ClassInfo {
        name: "advice",
        weight: -1.0,
        comment: "how to get more output or help: re-run with -e, see the docs (the whole score, whatever the level) (weight -1)",
    },
    ClassInfo {
        name: "strong-error",
        weight: 0.0,
        comment: "decides whether a record body counts as an error; no weight",
    },
    ClassInfo {
        name: "level-fatal",
        weight: 4.0,
        comment: "level tokens meaning fatal/critical (weight +4)",
    },
    ClassInfo {
        name: "level-error",
        weight: 3.0,
        comment: "level tokens meaning error (weight +3)",
    },
    ClassInfo {
        name: "level-warn",
        weight: 1.5,
        comment: "level tokens meaning warning (weight +1.5)",
    },
    ClassInfo {
        name: "level-info",
        weight: -0.5,
        comment: "level tokens meaning info/notice (weight -0.5)",
    },
    ClassInfo {
        name: "level-debug",
        weight: -1.0,
        comment: "level tokens meaning debug/trace (weight -1)",
    },
    ClassInfo {
        name: "continuation",
        weight: 0.0,
        comment: "lines that continue the record above whatever their indentation (regexes only,\n# searched in the line: anchor them with ^); a deeper indent continues it too",
    },
];

const ALL: [Class; NCLASSES] = [
    Class::Error,
    Class::Warn,
    Class::Summary,
    Class::Frame,
    Class::Pass,
    Class::Noise,
    Class::Advice,
    Class::StrongError,
    Class::LevelFatal,
    Class::LevelError,
    Class::LevelWarn,
    Class::LevelInfo,
    Class::LevelDebug,
    Class::Continuation,
];

impl Class {
    /// The class's name in a vocabulary file: `strong-error`.
    #[must_use]
    pub fn name(self) -> &'static str {
        INFO[self as usize].name
    }

    /// The class's weight in [`vocab_score`].
    #[must_use]
    pub fn weight(self) -> f64 {
        INFO[self as usize].weight
    }

    /// The class named `name`.
    #[must_use]
    pub fn from_name(name: &str) -> Option<Class> {
        ALL.into_iter().find(|c| c.name() == name)
    }

    /// For a level class, the severity its tokens mean.
    #[must_use]
    pub fn level(self) -> Option<u8> {
        match self {
            Class::LevelFatal => Some(4),
            Class::LevelError => Some(3),
            Class::LevelWarn => Some(2),
            Class::LevelInfo => Some(1),
            Class::LevelDebug => Some(0),
            _ => None,
        }
    }
}

/// The base score of a line at each structural severity, 0 to 4: the
/// weights of the level classes.
const LEVEL_SCORE: [f64; 5] = [-1.0, -0.5, 1.5, 3.0, 4.0];

/// The built-in vocabulary, one list per class in [`Class`] order, exactly
/// as `DEFAULT_VOCAB` in Python (`tests/data/vocab.txt`, Python's
/// `--dump-vocab`, checks it).
// Rust note: `&[&[&str]]` is a slice of slices of string slices, all of
// it static data in the binary: no allocation until a vocabulary is built.
#[rustfmt::skip]
pub const DEFAULT_VOCAB: [&[&str]; NCLASSES] = [
    // error
    &[
        "error", "errors", "err", "fatal", "panic", "panicked", "exception", "traceback", "assert",
        "assertion", "fail", "fails", "failed", "failure", "failures", "failing", "denied",
        "refused", "unreachable", "timeout", "timed out", "cannot", "can't", "could not",
        "couldn't", "unable", "not found", "no such", "missing", "invalid", "unexpected",
        "segfault", "segmentation fault", "abort", "aborted", "killed", "oom", "out of memory",
        "critical", "crash", "crashed", "rejected", "unhandled", "uncaught", "undefined",
        "null pointer", "stack overflow", "overflow", "exit code", "exited with", "non-zero",
        "conflict", "corrupt", "corrupted", "broken", "mismatch", "does not match", "not ok",
        "errno", "exc_info", "stacktrace", "stack trace",
        r#"/[A-Z]\w*(?:Error|Exception|Fault|Panic|Failure)\b/"#, "/✗|✘|❌/",
        r#"/(?<![A-Za-z])FAIL(?:ED|URE|S)?\b/"#,
        r#"/^\s*[-+]?\s*(?:Expected|Received|Actual|Got|Want)\b\s*[:+-]/"#,
    ],
    // warn
    &[
        "warn", "warning", "warnings", "deprecated", "deprecation", "retry", "retrying", "retried",
        "slow", "skipped", "skip", "skipping", "xfail", "xpass", "flaky", "unstable", "ignored",
        "pending", "todo", "fixme", "leak", "stale", "outdated", "insecure", "unused",
        "unreachable code", r#"/(?i)(?<![a-z])vulnerab\w*/"#, "/⚠/",
    ],
    // summary
    &[
        "passed", "test", "tests", "total", "summary", "finished", "completed", "done", "built",
        "compiled", "elapsed", "duration", "exit status", "result", "results", "success",
        "succeeded", "added", "removed", "audited", "complete", "coverage", "report",
        r#"/(?i)(?<![a-z])ran \d+/"#, r#"/^\s*={3,}/"#, r#"/^\s*-{3,}/"#, r#"/^\s*_{3,}/"#,
        r#"/^\s*#{1,3}\s/"#, r#"/^\s*\*{3,}/"#,
    ],
    // frame
    &[
        r#"/^\s*at\s/"#, r#"/^\s*File "/"#, r#"/^\s*-->/"#, r#"/^\s*>\s*\d+\s*\|/"#,
        r#"/[\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|go|rs|c|cc|cpp|h|hpp|java|kt|rb|cs|swift|php|scala|ex|exs)[:(]\d+/"#,
    ],
    // pass
    &[
        "/✓|✔/", r#"/\bPASS(?:ED)?\b/"#, r#"/^\s*ok\b/"#, r#"/\bok\s*$/"#,
    ],
    // noise
    &[
        "downloading", "downloaded", "fetching", "fetched", "compiling", "installing", "installed",
        "resolving", "resolved", "extracting", "unpacking", "progress", "collecting", "checking",
        "building", "linking", "caching", "cached", "preparing", "processing", "pulling", "pushing",
        "verbose", "debug", "trace", "info", "get", "heartbeat", "ping", "pong", "poll", "polling",
        "tick", r#"/\d+%/"#, r#"/\.{3,}/"#, "/[▏▎▍▌▋▊▉█░▒▓]/",
        r#"/\b(?:200|204|304) (?:OK|No Content|Not Modified)\b/"#,
    ],
    // advice
    &[
        "re-run", "rerun", "for more information", "for more details", "please refer to",
        "to see the full", r#"/(?i)\brun with --/"#, "get more help", "resume the build",
        "a complete log of this run", "for the individual test results", "see dump files",
        "after correcting the problems", r#"/\[Help \d+\]/"#,
    ],
    // strong-error
    &[
        r#"/(?i)\b\w*(?:error|exception)\b/"#, r#"/(?i)\bpanic/"#, r#"/(?i)\btraceback\b/"#,
        r#"/(?i)\bassert\w*\b/"#, r#"/(?i)\bfatal\b/"#, r#"/(?i)\bfail(?:ed|ure|s)?\b/"#,
        r#"/^\s*E\s{2,}/"#, "/✕|✗|FAIL/",
    ],
    // level-fatal
    &[
        "FATAL", "CRITICAL", "CRIT", "EMERG", "EMERGENCY", "ALERT", "PANIC", "SEVERE",
    ],
    // level-error
    &[
        "ERROR", "ERR",
    ],
    // level-warn
    &[
        "WARN", "WARNING",
    ],
    // level-info
    &[
        "INFO", "INFORMATION", "NOTICE",
    ],
    // level-debug
    &[
        "DEBUG", "TRACE", "VERBOSE", "FINE", "FINER", "FINEST",
    ],
    // continuation: `CONT_GENERIC`, `CONT_RUSTC` and `CONT_PYTEST` in Python
    &[
        r#"/^\s+at\s/"#,                                         // JS/Java stack frame
        r#"/^\s*File "[^"]+", line \d+/"#,                      // Python frame
        r#"/^\s*\^+\s*$/"#,                                     // caret marker
        r#"/^\s*~+\s*$/"#,                                      // tilde underline
        r#"/^Caused by:/"#,                                     // Java/Rust cause chain
        r#"/^\s*\.\.\. \d+ more/"#,                             // Java elided frames
        r#"/^\s*(?:Expected|Received|Actual|Got|Want|Diff):/"#, // assertion detail
        r#"/^\s*[+-]{3}\s/"#,                                   // diff headers
        r#"/^\s*@@ /"#,                                         // diff hunk
        r#"/^\s*\d*\s*\|(?:\s|$)/"#,                             // rustc gutter
        r#"/^\s*-->\s/"#,                                       // rustc location
        r#"/^\s*= (?:note|help|warning):/"#,                    // rustc notes
        r#"/^\s*::: /"#,                                        // rustc other location
        r#"/^E\s{2,}/"#,                                        // pytest failure detail
        r#"/^>\s{2,}/"#,                                        // pytest source marker
    ],
];

/// A vocabulary's entries: one list per class, indexed by `Class as
/// usize`, and the file's own masks.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entries {
    /// One list per class, in [`Class`] order.
    pub classes: Vec<Vec<String>>,
    /// `[mask NAME]` sections, in the order their names first appear, each
    /// with its `/regex/` entries. None are built in. Not in Python.
    pub masks: Vec<(String, Vec<String>)>,
}

// Rust note: implementing `Index` makes `entries[c]` work as it would on
// the `Vec` inside, like overloading `operator[]` in C++; `IndexMut` is the
// non-const overload.
impl std::ops::Index<usize> for Entries {
    type Output = Vec<String>;
    fn index(&self, c: usize) -> &Vec<String> {
        &self.classes[c]
    }
}

impl std::ops::IndexMut<usize> for Entries {
    fn index_mut(&mut self, c: usize) -> &mut Vec<String> {
        &mut self.classes[c]
    }
}

/// The built-in entries, as owned lists to extend.
#[must_use]
pub fn default_entries() -> Entries {
    Entries {
        classes: DEFAULT_VOCAB
            .iter()
            .map(|c| c.iter().map(|&e| e.to_owned()).collect())
            .collect(),
        masks: Vec::new(),
    }
}

/// Whether `name` can name a mask: letters and `_`, starting with a letter.
/// No digits, so that the built-in `WORDNUM` mask, which runs after, cannot
/// turn `<ID2>` into `<ID<N>>`.
fn is_mask_name(name: &str) -> bool {
    name.starts_with(|c: char| c.is_ascii_alphabetic())
        && name.chars().all(|c| c.is_ascii_alphabetic() || c == '_')
}

fn is_regex_entry(entry: &str) -> bool {
    entry.len() >= 2 && entry.starts_with('/') && entry.ends_with('/')
}

/// One `/regex/` entry as a fragment to join with others by `|`: leading
/// global flags such as `(?i)` become a scoped group, since a global flag
/// is only allowed at the start of a whole pattern. `_entry_regex()`.
fn entry_regex(entry: &str) -> String {
    let rx = &entry[1..entry.len() - 1];
    if let Some(rest) = rx.strip_prefix("(?") {
        let flags: String = rest.chars().take_while(|c| "aimsux".contains(*c)).collect();
        if !flags.is_empty() && rest[flags.len()..].starts_with(')') {
            return format!("(?{flags}:{})", &rest[flags.len() + 1..]);
        }
    }
    rx.to_owned()
}

/// A word or phrase as a pattern: `re.escape()`. The `regex` crate's
/// escaping covers the same characters as Python's, apart from whitespace,
/// which is literal unescaped in both.
fn escape(word: &str) -> String {
    regex::escape(word)
}

/// Python patterns pare uses, compiled once.
fn rx(pattern: &str) -> PyRegex {
    PyRegex::new(pattern).unwrap_or_else(|e| panic!("built-in pattern {pattern:?}: {e}"))
}

/// A zero count before an error noun is a summary, not an error.
static ZERO_COUNT: LazyLock<PyRegex> = LazyLock::new(|| {
    rx(
        r"(?i)\b(?:0|no|zero) (?:errors?|failures?|failed|failing|problems?|issues?|warnings?|vulnerabilit(?:y|ies))\b",
    )
});

/// HTTP status codes in request or key-value forms.
static HTTP_STATUS: LazyLock<PyRegex> = LazyLock::new(|| {
    rx(concat!(
        r#"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+(?:\s+HTTP/\d(?:\.\d)?"?)?\s+(\d{3})\b"#,
        r#"|(?i:\b(?:status(?:_?code)?|http_?status|statuscode|response_?code|code)\b\s*["']?\s*[=:]\s*["']?(\d{3})\b)"#,
        r"|\bHTTP/\d(?:\.\d)?\s+(\d{3})\b",
        r"|(?i:\b(?:returned\s+(?:http|status(?:\s+code)?)|responded(?: with)?|status code|http)\s+(\d{3})\b)",
    ))
});

/// Process exit codes.
static EXIT_CODE: LazyLock<PyRegex> = LazyLock::new(|| {
    rx(concat!(
        r"(?i)\bexit(?:ed)?(?:\s+with)?\s+(?:code|status)\s*[=:]?\s*(-?\d{1,3})\b",
        r"|\bexit(?:ed)?\s*[=:]\s*(-?\d{1,3})\b",
        r"|^\s*exit\s+(-?\d{1,3})\b",
        r"|\b(?:exit[_ ]?code|exit[_ ]?status|return\s*code|returncode|rc)\s*[=:]\s*(-?\d{1,3})\b",
    ))
});

/// A level with no message (`[ERROR]` alone), from the built-in level
/// tokens whatever the vocabulary: `LEVEL_ONLY_RX`.
static LEVEL_ONLY: LazyLock<PyRegex> = LazyLock::new(|| {
    let mut tokens: Vec<&str> = DEFAULT_VOCAB[LEVEL_CLASSES]
        .iter()
        .flat_map(|c| c.iter().copied())
        .collect();
    tokens.sort_by_key(|t| std::cmp::Reverse(t.chars().count()));
    rx(&format!(r"(?i)^\W*(?:{})\W*$", tokens.join("|")))
});

/// Whether a line with a structural level says nothing else (`[ERROR]`
/// alone): `LEVEL_ONLY_RX.match(text)`.
#[must_use]
pub fn level_only(text: &str) -> bool {
    LEVEL_ONLY.match_start(text).is_some()
}

/// A call-stack frame: `at pkg.Cls.method(...)`, `File "x.py", line 3`.
static STACK_FRAME: LazyLock<PyRegex> = LazyLock::new(|| {
    rx(
        r#"^\s*(?:at\s+(?:async\s+|new\s+)?[\w$.<>\[\]/:@-]+\s*(?:\(|$|\sin\s)|File "[^"]*", line \d+)"#,
    )
});

/// The first non-empty group of a match (Python's `next(g for g in
/// m.groups() if g)`).
fn first_group<'t>(text: &'t str, groups: &crate::pyre::Groups) -> Option<&'t str> {
    groups[1..]
        .iter()
        .flatten()
        .map(|&(s, e)| &text[s..e])
        .find(|g| !g.is_empty())
}

/// Severity of an HTTP status on the line: 5xx error, 4xx warn, 1xx to
/// 3xx info. `http_severity()`.
#[must_use]
pub fn http_severity(text: &str) -> Option<u8> {
    let groups = HTTP_STATUS.captures_at(text, 0)?;
    let code = py_int(first_group(text, &groups)?)?;
    match code {
        500.. => Some(3),
        400.. => Some(2),
        100.. => Some(1),
        _ => None,
    }
}

/// Severity of an exit code on the line: 0 info, 1 to 127 error, 128 and
/// up or negative (signals, OOM kills) fatal. `exit_severity()`.
#[must_use]
pub fn exit_severity(text: &str) -> Option<u8> {
    let groups = EXIT_CODE.captures_at(text, 0)?;
    let code = py_int(first_group(text, &groups)?)?;
    Some(match code {
        0 => 1,
        c if !(0..128).contains(&c) => 4,
        _ => 3,
    })
}

/// The first `n` characters of `s` (Python's `s[:n]`).
fn head_chars(s: &str, n: usize) -> &str {
    s.char_indices().nth(n).map_or(s, |(i, _)| &s[..i])
}

/// Bare level tokens are looked for in this many characters at the start.
const LEVEL_SCAN_CHARS: usize = 120;

/// A compiled vocabulary. `Vocab` in Python.
pub struct Vocab {
    entries: Entries,
    /// Per class (not the level classes): its plain words and phrases as
    /// one alternation, and its `/regex/` entries as another.
    // Rust note: `[Option<PyRegex>; N]` is a fixed-size array stored inline,
    // like `std::array<std::optional<T>, N>`; `None` for a class with no
    // entries of that kind.
    words: [Option<PyRegex>; NWORD_CLASSES],
    others: [Option<PyRegex>; NWORD_CLASSES],
    /// Level token, uppercased → severity.
    level_of: HashMap<String, u8>,
    level_key: PyRegex,
    level_bare: PyRegex,
    level_letter: PyRegex,
    /// The continuation regexes as one alternation; `None` if there are none.
    continuation: Option<PyRegex>,
    /// The file's masks, compiled.
    masks: Vec<UserMask>,
}

impl Vocab {
    /// Compiles a vocabulary.
    ///
    /// # Errors
    ///
    /// If one of its regexes does not compile (which [`parse_vocab`] has
    /// already checked for the entries it read).
    pub fn new(entries: Entries) -> Result<Vocab, String> {
        let mut words: [Option<PyRegex>; NWORD_CLASSES] = Default::default();
        let mut others: [Option<PyRegex>; NWORD_CLASSES] = Default::default();
        for c in 0..NWORD_CLASSES {
            let items = &entries[c];
            let parts: Vec<String> = items
                .iter()
                .filter(|e| is_regex_entry(e))
                .map(|e| format!("(?:{})", entry_regex(e)))
                .collect();
            if !parts.is_empty() {
                others[c] = Some(PyRegex::new(&parts.join("|"))?);
            }
            // All plain words of a class share one boundary check (one
            // lookbehind for the whole alternation), as in Python.
            let mut plain: Vec<&String> = items.iter().filter(|e| !is_regex_entry(e)).collect();
            if !plain.is_empty() {
                // Rust note: `sort_by_key` is a stable sort, as Python's
                // `sorted` is, so words of equal length keep their order.
                plain.sort_by_key(|w| std::cmp::Reverse(w.chars().count()));
                let alt: Vec<String> = plain.iter().map(|w| escape(w)).collect();
                words[c] = Some(PyRegex::new(&format!(
                    r"(?i:(?<![a-z-])(?:{})(?![a-z-]))",
                    alt.join("|")
                ))?);
            }
        }
        // Level tokens; a later class wins a token that two classes share,
        // and a token keeps the place it was first seen in.
        let mut level_of: HashMap<String, u8> = HashMap::new();
        let mut order: Vec<String> = Vec::new();
        for c in &ALL[LEVEL_CLASSES] {
            for tok in &entries[*c as usize] {
                let up = tok.to_uppercase();
                if level_of
                    .insert(up.clone(), c.level().unwrap_or(0))
                    .is_none()
                {
                    order.push(up);
                }
            }
        }
        order.sort_by_key(|t| std::cmp::Reverse(t.chars().count()));
        let alt = if order.is_empty() {
            "(?!x)x".to_owned()
        } else {
            order
                .iter()
                .map(|t| escape(t))
                .collect::<Vec<_>>()
                .join("|")
        };
        let level_key = PyRegex::new(&format!(
            r#"(?i)\b(?:level|lvl|loglevel|log_level|severity|sev|priority)\b\s*["']?\s*[=:]\s*["']?({alt})(?![A-Za-z])"#
        ))?;
        let level_bare = PyRegex::new(&format!(
            r#"(?i)(?:^|[\s\[(<|{{"':,=/-])({alt})(?=$|[\s\])>|:,"'/\[(.-])"#
        ))?;
        let mut letters = String::from(r"\[([EWIDVTF])\]");
        if crate::pass::TOOL_LOGCAT {
            letters.push_str(concat!(
                r"|^(?:[\d:.\-]{5,24}\s+){0,2}(?:\d+\s+\d+\s+)?([EWIDVTF])/[\w.$]+[(:]",
                r"|^(?:[\d:.\-]{5,24}\s+){1,2}\d+\s+\d+\s+([EWIDVF])\s+\S+\s*:",
                r"|^([EWIF])\d{4}\s",
            ));
        }
        if crate::pass::TOOL_PYTEST {
            letters.push_str(r"|^(E)\s{2,}");
        }
        let level_letter = PyRegex::new(&letters)?;
        let parts: Vec<String> = entries[Class::Continuation as usize]
            .iter()
            .filter(|e| is_regex_entry(e))
            .map(|e| format!("(?:{})", entry_regex(e)))
            .collect();
        let continuation = if parts.is_empty() {
            None
        } else {
            Some(PyRegex::new(&parts.join("|"))?)
        };
        let masks = entries
            .masks
            .iter()
            .map(|(name, items)| {
                let rxs: Vec<String> = items.iter().map(|e| entry_regex(e)).collect();
                UserMask::new(name, &rxs)
            })
            .collect::<Result<_, _>>()?;
        Ok(Vocab {
            continuation,
            masks,
            entries,
            words,
            others,
            level_of,
            level_key,
            level_bare,
            level_letter,
        })
    }

    /// Whether `text` has the shape of a line that continues the record
    /// above: a match for one of the continuation regexes. `Pass._joins()`
    /// in Python, which has the built-in ones only.
    #[must_use]
    pub fn continues(&self, text: &str) -> bool {
        self.continuation
            .as_ref()
            .is_some_and(|rx| rx.is_match(text))
    }

    /// The vocabulary file's masks, for [`mask`](crate::mask::mask) and
    /// [`drain_message`](crate::mask::drain_message): none by default.
    #[must_use]
    pub fn masks(&self) -> &[UserMask] {
        &self.masks
    }

    /// The built-in vocabulary.
    ///
    /// # Panics
    ///
    /// Never: the built-in patterns compile (a test checks).
    #[must_use]
    pub fn builtin() -> Vocab {
        Vocab::new(default_entries()).expect("the built-in vocabulary compiles")
    }

    /// The entries it was built from.
    #[must_use]
    pub fn entries(&self) -> &Entries {
        &self.entries
    }

    /// The facts about one line, worked out as they are asked for.
    #[must_use]
    pub fn line<'v, 't>(&'v self, text: &'t str) -> Line<'v, 't> {
        Line {
            vocab: self,
            text,
            zeroed: None,
            found: [None; NWORD_CLASSES],
        }
    }

    /// Severity 0 to 4 of an explicit log-level token, or `None`.
    /// Structural: the token must be in a level form, not just a word in
    /// the message. `Vocab.detect_level()`.
    #[must_use]
    pub fn detect_level(&self, text: &str) -> Option<u8> {
        // pino and bunyan write numbers: "level":50 (not in Python).
        if crate::pass::TOOL_STRUCTURED && crate::pass::rust_rules() {
            if let Some(level) = crate::structured::numeric_level(text) {
                return Some(level);
            }
        }
        // key=value forms anywhere: level=error, "level": "warn".
        if let Some(groups) = self.level_key.captures_at(text, 0) {
            if let Some((s, e)) = groups[1] {
                // Case-insensitive matching takes the Turkish İ for an i, so
                // the token may not be one of ours once uppercased.
                if let Some(&level) = self.level_of.get(&text[s..e].to_uppercase()) {
                    return Some(level);
                }
            }
        }
        // Bare or bracketed forms near the start of the line.
        let head = head_chars(text, LEVEL_SCAN_CHARS);
        for groups in self.level_bare.captures_iter(head) {
            let Some((start, end)) = groups[1] else {
                continue;
            };
            let tok = &head[start..end];
            let next = head[end..].chars().next();
            let ok = py_isupper(tok) // ERROR, WARN: an uppercase token is a level
                || head[..start].chars().next_back().is_some_and(|c| "[(<|".contains(c)) // [error], (warn)
                || (next == Some(':') && head[..start].chars().count() <= 60) // error: ..., Warning: ...
                || (crate::pass::TOOL_RUSTC && start == 0 && next == Some('[')); // rustc error[E0308]:
            if ok {
                if let Some(&level) = self.level_of.get(&tok.to_uppercase()) {
                    return Some(level);
                }
            }
        }
        // Single letters: logcat E/Tag, glog E0921, [E], pytest's E.
        let head = head_chars(text, 80);
        let groups = self.level_letter.captures_at(head, 0)?;
        match first_group(head, &groups)? {
            "F" => Some(4),
            "E" => Some(3),
            "W" => Some(2),
            "I" => Some(1),
            _ => Some(0), // D, V, T
        }
    }

    /// The strongest structural signal on the line: log level, HTTP status
    /// or exit code. `Vocab.severity()`.
    #[must_use]
    pub fn severity(&self, text: &str) -> Option<u8> {
        // Rust note: `Option<u8>` orders `None` below every `Some`, so `max`
        // over them is Python's max over the signals that are present.
        self.detect_level(text)
            .max(http_severity(text))
            .max(exit_severity(text))
    }
}

/// What the vocabulary says about one line, remembered per class: a line
/// is asked about several classes in a row. `Vocab.has()` with its cache.
// Rust note: two lifetimes: the `Line` borrows the vocabulary (`'v`) and the
// line's text (`'t`), and cannot outlive either. The compiler checks this;
// in C++ the same struct would hold two raw references and trust the caller.
pub struct Line<'v, 't> {
    vocab: &'v Vocab,
    text: &'t str,
    /// The text with zero counts ("0 errors") blanked, for the error and
    /// warning classes; worked out on first use.
    zeroed: Option<Cow<'t, str>>,
    found: [Option<bool>; NWORD_CLASSES],
}

impl Line<'_, '_> {
    /// The line's text.
    #[must_use]
    pub fn text(&self) -> &str {
        self.text
    }

    /// Whether the line has an entry of class `c` (not a level class).
    ///
    /// # Panics
    ///
    /// On a level class, which has tokens, not entries to match.
    pub fn has(&mut self, c: Class) -> bool {
        let i = c as usize;
        assert!(i < NWORD_CLASSES, "has() of a level class");
        if let Some(found) = self.found[i] {
            return found;
        }
        let text: &str = if matches!(c, Class::Error | Class::Warn | Class::StrongError) {
            let original = self.text;
            self.zeroed.get_or_insert_with(|| {
                ZERO_COUNT
                    .replace_all(original, |_| Cow::Borrowed(" "))
                    .map_or(Cow::Borrowed(original), Cow::Owned)
            })
        } else {
            self.text
        };
        let v = self.vocab;
        let found = v.others[i].as_ref().is_some_and(|rx| rx.is_match(text))
            || v.words[i].as_ref().is_some_and(|rx| rx.is_match(text));
        self.found[i] = Some(found);
        found
    }
}

/// Salience of one line's text. A structural level, when present, sets
/// the base score and decides whether error and warning words count;
/// summary vocabulary only counts on a record head. `vocab_score()`.
///
/// The arithmetic is Python's, operation for operation, so that the
/// floating-point results are identical to the bit.
#[must_use]
pub fn vocab_score(line: &mut Line<'_, '_>, head: bool, sev: Option<u8>) -> f64 {
    let text = line.text;
    let mut s = 0.0;
    let mut err = line.has(Class::Error);
    let mut warn = line.has(Class::Warn);
    match sev {
        None => {
            if line.has(Class::Pass) {
                // A passing-test marker is structural: the words of a test
                // name ("throws on invalid") are not log vocabulary.
                s += Class::Pass.weight();
            } else {
                if (err || warn) && STACK_FRAME.match_start(text).is_some() {
                    // The words of a frame's function name say nothing
                    // about what happened.
                    err = false;
                    warn = false;
                }
                if err {
                    s += Class::Error.weight();
                }
                if warn {
                    s += Class::Warn.weight();
                }
                if !err && line.has(Class::Noise) {
                    s += Class::Noise.weight();
                }
            }
        }
        Some(sev) => {
            s += LEVEL_SCORE[usize::from(sev)];
            if sev >= 2 {
                // Vocabulary reinforces a warning or error level.
                if err {
                    s += 0.5 * Class::Error.weight();
                }
                if warn {
                    s += 0.5 * Class::Warn.weight();
                }
            }
        }
    }
    if line.has(Class::Advice) {
        // A build tool's closing advice is printed at error level on every
        // failure; it is not the failure.
        return Class::Advice.weight();
    }
    if sev.is_some() && LEVEL_ONLY.match_start(text).is_some() {
        return -1.0; // "[ERROR]" alone says nothing
    }
    if head && line.has(Class::Summary) {
        s += Class::Summary.weight();
    }
    if line.has(Class::Frame) {
        s += Class::Frame.weight();
    }
    if py_strip(text).chars().count() < 4 {
        s -= 1.0;
    }
    s
}

/// Parses a vocabulary file on top of `base`. `parse_vocab()`:
///
/// ```text
/// # comment
/// [error]            entries below extend the built-in class
/// [error replace]    entries below replace it
/// timeout            a word or phrase (whole-word, case-insensitive)
/// /(?i)\bfehler\b/   a regex, used as written
/// [mask ORDER]       regexes below mask to <ORDER> (not in Python)
/// ```
///
/// # Errors
///
/// A message in Python's words for a malformed line; `source` names the
/// file in it.
pub fn parse_vocab(text: &str, base: &Entries, source: &str) -> Result<Entries, String> {
    // Rust note: a local enum is fine inside a function; it is private to it.
    #[derive(Clone, Copy)]
    enum Header {
        None,
        Class(Class),
        /// An index into `out.masks`.
        Mask(usize),
    }
    let mut out = base.clone();
    let mut header = Header::None;
    for (n, raw) in (1..).zip(py_splitlines(text)) {
        let line = py_strip(raw);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') && line.len() >= 2 {
            let parts: Vec<&str> = py_split(&line[1..line.len() - 1]).collect();
            if parts.first() == Some(&"mask") {
                if parts.len() != 2 || !is_mask_name(parts[1]) {
                    return Err(format!(
                        "{source}:{n}: expected [mask NAME] with NAME made of letters and _; got {}",
                        py_repr(line)
                    ));
                }
                let name = parts[1];
                let at = match out.masks.iter().position(|(m, _)| m == name) {
                    Some(at) => at,
                    None => {
                        out.masks.push((name.to_owned(), Vec::new()));
                        out.masks.len() - 1
                    }
                };
                header = Header::Mask(at);
                continue;
            }
            let class = parts.first().and_then(|p| Class::from_name(p));
            let Some(class) =
                class.filter(|_| parts.len() <= 2 && (parts.len() < 2 || parts[1] == "replace"))
            else {
                let names: Vec<&str> = ALL.iter().map(|c| c.name()).collect();
                return Err(format!(
                    "{source}:{n}: expected [class] or [class replace] with class in {}; got {}",
                    names.join(", "),
                    py_repr(line)
                ));
            };
            header = Header::Class(class);
            if parts.len() == 2 {
                out[class as usize].clear();
            }
            continue;
        }
        let class = match header {
            Header::None => {
                return Err(format!(
                    "{source}:{n}: entry {} appears before any [class] header",
                    py_repr(line)
                ));
            }
            Header::Mask(at) => {
                if !is_regex_entry(line) {
                    return Err(format!(
                        "{source}:{n}: [mask {}] takes /regexes/, not words: {}",
                        out.masks[at].0,
                        py_repr(line)
                    ));
                }
                if let Err(e) = PyRegex::new(&entry_regex(line)).and_then(|rx| rx.validate()) {
                    return Err(format!("{source}:{n}: bad regex {}: {e}", py_repr(line)));
                }
                out.masks[at].1.push(line.to_owned());
                continue;
            }
            Header::Class(class) => class,
        };
        if class == Class::Continuation && !is_regex_entry(line) {
            return Err(format!(
                "{source}:{n}: the continuation class takes /regexes/, not words: {}",
                py_repr(line)
            ));
        }
        if is_regex_entry(line) {
            if class.level().is_some() {
                return Err(format!(
                    "{source}:{n}: level classes take plain tokens, not regexes: {}",
                    py_repr(line)
                ));
            }
            if let Err(e) = PyRegex::new(&line[1..line.len() - 1]).and_then(|rx| rx.validate()) {
                return Err(format!("{source}:{n}: bad regex {}: {e}", py_repr(line)));
            }
        }
        out[class as usize].push(line.to_owned());
    }
    Ok(out)
}

/// Python's `str.splitlines()`: lines split at any of Python's line
/// boundaries, a trailing one not making an empty last line.
fn py_splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            out.push(&text[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|&(_, n)| n == '\n') {
                chars.next();
                start += 1;
            }
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// The vocabulary in file format, as `--dump-vocab` prints it.
/// `dump_vocab()`.
#[must_use]
pub fn dump_vocab(entries: &Entries) -> String {
    let mut out = String::from(concat!(
        "# pare vocabulary. Edit and pass with --vocab FILE (or $PARE_VOCAB).\n",
        "# A plain line is a word or phrase, matched whole and case-insensitively.\n",
        "# A /.../ line is a regular expression used as written.\n",
        "# [class] adds entries to the built-in class; [class replace] discards the\n",
        "# built-in entries first. This dump uses 'replace' so it reproduces the\n",
        "# defaults exactly; switch a header to plain [class] to keep the defaults\n",
        "# and add to them.\n",
    ));
    for (info, items) in INFO.iter().zip(&entries.classes) {
        out.push_str(&format!("\n# {}\n[{} replace]\n", info.comment, info.name));
        for e in items {
            out.push_str(e);
            out.push('\n');
        }
    }
    // None by default, so the default dump has no such section.
    for (name, items) in &entries.masks {
        out.push_str(&format!(
            "\n# matches become <{name}> before lines are clustered (regexes only)\n[mask {name}]\n"
        ));
        for e in items {
            out.push_str(e);
            out.push('\n');
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_compiles_and_scores() {
        let v = Vocab::builtin();
        assert_eq!(v.severity("2026-10-07 ERROR [db] boom"), Some(3));
        assert_eq!(v.severity("GET /api 503 12ms"), Some(3));
        assert_eq!(v.severity("process exited with code 137"), Some(4));
        assert_eq!(v.severity("all good"), None);
        // pino's numeric levels (Rust version).
        assert_eq!(
            v.severity(r#"{"level":50,"time":1,"msg":"job failed"}"#),
            Some(3)
        );
        assert_eq!(v.severity(r#"{"level":30,"time":1,"msg":"ok"}"#), Some(1));
        let mut line = v.line("0 errors, 3 tests passed");
        assert!(!line.has(Class::Error) && line.has(Class::Summary));
        let mut line = v.line("ERROR: connection refused");
        let sev = v.severity(line.text());
        assert!((vocab_score(&mut line, true, sev) - 4.5).abs() < 1e-12);
    }

    fn unescape(s: &str) -> String {
        let mut out = String::with_capacity(s.len());
        let mut chars = s.chars();
        while let Some(c) = chars.next() {
            if c != '\\' {
                out.push(c);
                continue;
            }
            out.push(match chars.next() {
                Some('t') => '\t',
                Some('n') => '\n',
                Some(other) => other,
                None => '\\',
            });
        }
        out
    }

    #[test]
    fn matches_python_on_real_lines() {
        // tests/data/vocab.tsv: Python's severity, classes and scores.
        const LETTERS: [(char, Class); 8] = [
            ('e', Class::Error),
            ('w', Class::Warn),
            ('s', Class::Summary),
            ('f', Class::Frame),
            ('p', Class::Pass),
            ('n', Class::Noise),
            ('a', Class::Advice),
            ('x', Class::StrongError),
        ];
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/data/vocab.tsv");
        let data = std::fs::read_to_string(path)
            .expect("tests/data/vocab.tsv (bench/gen_fixtures.py writes it)");
        let v = Vocab::builtin();
        let mut rows = 0;
        for row in data.lines().skip(1) {
            let f: Vec<&str> = row.split('\t').collect();
            let text = unescape(f[0]);
            let want_sev: i8 = f[1].parse().unwrap();
            let sev = v.severity(&text);
            assert_eq!(
                sev.map_or(-1, |s| s as i8),
                want_sev,
                "severity of {text:?}"
            );
            let mut line = v.line(&text);
            let classes: String = LETTERS
                .iter()
                .filter(|(_, c)| line.has(*c))
                .map(|(l, _)| *l)
                .collect();
            assert_eq!(
                if classes.is_empty() { "-" } else { &classes },
                f[2],
                "classes of {text:?}"
            );
            let head: f64 = f[3].parse().unwrap();
            let body: f64 = f[4].parse().unwrap();
            assert_eq!(
                vocab_score(&mut line, true, sev).to_bits(),
                head.to_bits(),
                "head score of {text:?}"
            );
            assert_eq!(
                vocab_score(&mut line, false, sev).to_bits(),
                body.to_bits(),
                "body score of {text:?}"
            );
            rows += 1;
        }
        assert!(rows > 6000);
    }

    #[test]
    fn dump_is_pythons() {
        // Python's dump, then the continuation class, which it does not have.
        let golden = |name: &str| {
            std::fs::read_to_string(format!("{}/tests/data/{name}", env!("CARGO_MANIFEST_DIR")))
                .unwrap()
                + &continuation_section()
        };
        assert_eq!(dump_vocab(&default_entries()), golden("vocab.txt"));
        let example = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/reference/vocab.example"
        ))
        .unwrap();
        let entries = parse_vocab(&example, &default_entries(), "reference/vocab.example").unwrap();
        assert_eq!(dump_vocab(&entries), golden("vocab-example.txt"));
        Vocab::new(entries).unwrap();
    }

    /// The continuation class as `--dump-vocab` prints it by default.
    fn continuation_section() -> String {
        let info = &INFO[Class::Continuation as usize];
        let mut s = format!("\n# {}\n[{} replace]\n", info.comment, info.name);
        for e in DEFAULT_VOCAB[Class::Continuation as usize] {
            s.push_str(e);
            s.push('\n');
        }
        s
    }

    #[test]
    fn continuation_rules_are_entries() {
        let v = Vocab::builtin();
        assert!(
            v.continues("    at a.B.c(B.java:1)")
                && v.continues("Caused by: x")
                && !v.continues("main.main()")
        );
        // A file adds to them, or replaces them.
        let base = default_entries();
        let go = parse_vocab(
            "[continuation]\n/^goroutine \\d+ \\[/\n/^[\\w./]+\\(.*\\)$/\n",
            &base,
            "f",
        )
        .unwrap();
        let v = Vocab::new(go).unwrap();
        assert!(
            v.continues("main.main()")
                && v.continues("goroutine 1 [running]:")
                && v.continues("Caused by: x")
        );
        let none =
            Vocab::new(parse_vocab("[continuation replace]\n", &base, "f").unwrap()).unwrap();
        assert!(!none.continues("Caused by: x"));
        let e = parse_vocab("[continuation]\ngoroutine\n", &base, "f").unwrap_err();
        assert_eq!(
            e,
            "f:2: the continuation class takes /regexes/, not words: 'goroutine'"
        );
    }

    #[test]
    fn masks_from_a_file() {
        let base = default_entries();
        let e = parse_vocab(
            "[mask ORDER]\n/\\bORD-\\d+/\n[warn]\nslowish\n[mask TENANT]\n/(?i)tenant=\\w+/\n[mask ORDER]\n/\\bPO\\d+/\n",
            &base,
            "f",
        )
        .unwrap();
        assert_eq!(
            e.masks,
            [
                (
                    "ORDER".to_owned(),
                    vec![r"/\bORD-\d+/".to_owned(), r"/\bPO\d+/".to_owned()]
                ),
                ("TENANT".to_owned(), vec![r"/(?i)tenant=\w+/".to_owned()]),
            ]
        );
        assert_eq!(e[Class::Warn as usize].last().unwrap(), "slowish");
        // The dump reads back as the same entries.
        assert_eq!(parse_vocab(&dump_vocab(&e), &base, "dump").unwrap(), e);
        let v = Vocab::new(e).unwrap();
        assert_eq!(
            crate::mask::mask("PO77 and ORD-1 for TENANT=x", true, v.masks()),
            "<ORDER> and <ORDER> for <TENANT>"
        );
        assert!(Vocab::builtin().masks().is_empty());
        for (file, want) in [
            (
                "[mask]\n",
                "f:1: expected [mask NAME] with NAME made of letters and _; got '[mask]'",
            ),
            (
                "[mask ID2]\n",
                "f:1: expected [mask NAME] with NAME made of letters and _; got '[mask ID2]'",
            ),
            (
                "[mask ID replace]\n",
                "f:1: expected [mask NAME] with NAME made of letters and _; got '[mask ID replace]'",
            ),
            (
                "[mask ID]\nord\n",
                "f:2: [mask ID] takes /regexes/, not words: 'ord'",
            ),
        ] {
            assert_eq!(parse_vocab(file, &base, "f").unwrap_err(), want);
        }
        assert!(
            parse_vocab("[mask ID]\n/(/\n", &base, "f")
                .unwrap_err()
                .starts_with("f:2: bad regex '/(/'")
        );
    }

    #[test]
    fn splitlines_like_python() {
        assert_eq!(py_splitlines("a\r\nb\rc\x1cd\n"), ["a", "b", "c", "d"]);
        assert_eq!(py_splitlines("a\n\nb"), ["a", "", "b"]);
    }

    #[test]
    fn parse_errors_name_the_line() {
        let base = default_entries();
        let e = parse_vocab("x\n", &base, "f").unwrap_err();
        assert_eq!(e, "f:1: entry 'x' appears before any [class] header");
        let e = parse_vocab("[level-error]\n/x/\n", &base, "f").unwrap_err();
        assert!(e.starts_with("f:2: level classes take plain tokens"));
        let e = parse_vocab("\n[nope]\n", &base, "f").unwrap_err();
        assert!(
            e.starts_with("f:2: expected [class] or [class replace] with class in error, warn,")
        );
        let v = parse_vocab(
            "[warn replace]\nslowish\n[error]\n/(?i)\\bfehler\\b/\n",
            &base,
            "f",
        )
        .unwrap();
        assert_eq!(v[Class::Warn as usize], ["slowish"]);
        assert_eq!(
            v[Class::Error as usize].last().unwrap(),
            r"/(?i)\bfehler\b/"
        );
    }
}
