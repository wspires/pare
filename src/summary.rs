//! Producing the output: `summarize()` in `reference/pare.py`, in batch mode.
//!
//! Input that fits the budget is passed through unchanged
//! ([`passthrough`]). Anything larger is summarised ([`summarize`]) into
//! sections, each a different way of choosing lines:
//!
//! ```text
//! # pare: 12,034 lines, 1,203,400 B (~429,786 tok) → ~1,998 tok (budget 2,000) · 57 templates
//! ## head (L1–L5)               the first lines, as they are
//! ## templates (by salience)    one entry per kind of line: count, marker, exemplar
//! ## selected lines             single lines worth showing, by score
//! ## tail (L12020–L12034)       the last lines, where verdicts are
//! ## omitted ...                the ledger: what was left out, per template
//! ## raw: <run file>            where everything is
//! ```
//!
//! Template entries and raw lines compete in one priority queue on the
//! same salience scale, each at its value per byte, with diminishing
//! returns for repeats of a shape already shown, until the budget is
//! spent. The result is then rendered and measured, and content is shed
//! until it fits exactly: the budget is a hard limit.
//!
//! The Python function is one long body with closures sharing its state.
//! Here that state is the private `Summarizer` struct and each closure is a
//! method of the same name, in the same order, so the two can be read side
//! by side.

use std::cmp::{Ordering, Reverse};
use std::collections::{BTreeMap, BinaryHeap, HashMap, HashSet};
use std::fs::File;
use std::io::{self, Write};
use std::path::Path;
use std::rc::Rc;
use std::sync::LazyLock;

use crate::budget::Budget;
use crate::mask::{drain_message, mask};
use crate::pyclass::{py_isdigit, py_rstrip, py_strip};
use crate::pyre::PyRegex;
use crate::store::Store;
use crate::text::{clean_line, clip};
use crate::vocab::{Class, NWORD_CLASSES, Vocab, level_only};

/// Writes the input to `out` verbatim, one cleaned line per line, and then
/// `end` if given (command mode: a failing exit status), if it all fits
/// the budget. Returns the bytes written, or `None` if it did not fit.
///
/// A whole run that fits is the output, byte for byte: no header, and no
/// run-file line (the run file holds nothing more). The cleaned text is
/// what the summary would show for each line anyway: no ANSI codes, no
/// carriage-return redraws, no trailing whitespace.
///
/// # Errors
///
/// If reading the spool back or writing the output fails.
pub fn passthrough(
    store: &Store,
    spool: &Path,
    budget: Budget,
    end: Option<&str>,
    out: &mut impl Write,
) -> io::Result<Option<u64>> {
    // Rust note: comparing an unsigned byte count with a signed budget
    // needs an explicit conversion; Rust has no implicit integer promotions
    // and no signed/unsigned mixing. `i64::try_from` fails instead of
    // wrapping, so a count past i64::MAX (impossible here) counts as not
    // fitting rather than as a negative number that fits.
    let end_bytes = end.map_or(0, |e| e.len() as u64 + 1);
    let fits = i64::try_from(store.verbatim_bytes + end_bytes)
        .is_ok_and(|v| v <= budget.passthrough_bytes());
    if !fits {
        return Ok(None);
    }
    let mut written = 0u64;
    store.for_each_raw(spool, |raw| {
        let line = clean_line(raw);
        written += line.len() as u64 + 1;
        out.write_all(line.as_bytes())?;
        out.write_all(b"\n")
    })?;
    if let Some(end) = end {
        writeln!(out, "{end}")?;
    }
    Ok(Some(written + end_bytes))
}

// The allocation's constants, as in Python. Each one survived
// bench/ablate.py: switching it off lowers recall at some budget or grows
// the output at the same recall. (The position bonuses that ablation
// removed are 0 in Python and left out here.)

/// Follow mode: salience added to a template first seen in this window.
const NOVELTY_BONUS: f64 = 1.0;
/// Head and tail windows skip lines of templates this common...
const WINDOW_SKIP_COUNT: u32 = 5;
/// ...looking at most this many window-lengths into the file.
const WINDOW_SCAN_FACTOR: i64 = 3;
/// The tail window keeps an error line of a template that common when it
/// is the only line of its template in the window's span (Rust version;
/// see `Summarizer::windowed`).
const TAIL_KEEPS_ERRORS: bool = true;
/// Templates this small are skipped when their exemplar restates a shown line.
const NEAR_DUP_MAX_COUNT: u32 = 5;
/// A raw line's priority is scaled by `(REF / (REF + len))^POWER`.
const DENSITY_REF: usize = 120;
const DENSITY_POWER: f64 = 0.35;
/// Divisor growth per already-shown line of the same repeat key.
const FILL_PENALTY: f64 = 1.5;
/// Lines scoring below this never pad the output.
const FILL_FLOOR: f64 = 1.0;
/// Template entries below this salience stay in the ledger (the first
/// entry is exempt).
const TEMPLATE_FLOOR: f64 = 1.5;
/// The same under `--budget auto` (not in Python): only error-level shapes
/// get an entry of their own. With the windows and pass-through sized
/// apart from the ceiling (`crate::budget`), this is what makes the
/// summary stop early; raising `FILL_FLOOR` as well cost more recall than
/// it saved tokens (docs/DESIGN.md, "Automatic budget").
const AUTO_TEMPLATE_FLOOR: f64 = 3.0;
/// A template whose records are at least this share distinct shapes is a
/// list of different items...
const LIST_DISTINCT: f64 = 0.5;
/// ...judged on its first this many records.
const LIST_SAMPLE: usize = 200;
/// A list of failures this long is rendered compactly: shared prefix once,
/// one item per line...
const LIST_MIN_COUNT: u32 = 3;
/// ...when the shared prefix saves at least this many characters per item.
const LIST_MIN_SHARED: usize = 16;
/// An item line: this, then the item.
fn list_item() -> String {
    format!("  {}", crate::text::glyphs().ellipsis)
}
/// The shared prefix ends after one of these.
const LIST_CUT: &str = " :/.#(";
/// Salience added to a listed failure that fails unlike most of the list...
const RARE_FAILURE_BONUS: f64 = 2.0;
/// ...when at most this share of the list fails the same way...
const RARE_FAILURE_SHARE: f64 = 0.1;
/// ...in a list of at least this many failures with a known way of failing.
const RARE_FAILURE_MIN: usize = 5;
/// Lines a failure's section is followed for before giving up.
const SECTION_MAX: usize = 400;
/// How far back a failure's section header is looked for.
const SECTION_LOOKBACK: usize = 200_000;
/// A template seen twice lists both lines instead of its pattern and one
/// example.
const PAIR_MEMBERS: bool = true;
/// Below this many tokens, multi-record entries are one line.
const COMPACT_BUDGET: i64 = 1000;
/// Below this, the usage hint at the end is dropped.
const HINT_BUDGET: i64 = 1000;
/// Near-duplicate test: lines with fewer tokens are exempt...
const NEAR_DUP_MIN_TOKENS: usize = 4;
/// ...and a line is a duplicate when this share of its tokens is in one
/// shown line.
const NEAR_DUP_CONTAINMENT: f64 = 0.8;

const LEVEL_NAME: [&str; 5] = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"];

fn run_hint() -> String {
    format!(
        "   expand with: pare --only T<n> < raw   {}   drop noise: pare --except T<n>,T<m> < raw",
        crate::text::glyphs().dot
    )
}

/// A bracketed parameter list right after a name: pytest's
/// `test_x[numpy-3-1]`, JUnit 4's `testFoo[2]`. Its digits are part of
/// the name.
static PARAMS: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(r"(?<=[\w)])\[([^\[\]\s]{1,100})\]").expect("params pattern is valid")
});

/// Python's `f"{n:,}"`: an integer with thousands separators.
///
/// ```
/// use pare::summary::fmt_int;
/// assert_eq!(fmt_int(1_234_567), "1,234,567");
/// assert_eq!(fmt_int(-1000), "-1,000");
/// assert_eq!(fmt_int(999), "999");
/// ```
#[must_use]
pub fn fmt_int(n: i64) -> String {
    fmt_int_impl(n)
}

/// The `## raw:` line of a batch run: the run file, or why there is none.
#[must_use]
pub fn raw_line(run_path: Option<&str>, not_saved: Option<&str>) -> String {
    match (run_path, not_saved) {
        (Some(p), _) => format!("## raw: {p}"),
        (None, Some(why)) => format!("## raw: (not saved: {why})"),
        (None, None) => "## raw: (not saved)".to_owned(),
    }
}

fn fmt_int_impl(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3 + 1);
    if n < 0 {
        out.push('-');
    }
    for (k, c) in digits.chars().enumerate() {
        if k > 0 && (digits.len() - k).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Bytes of `parts` joined as lines: each part and its newline.
/// `nbytes()`.
fn nbytes(parts: &[String]) -> i64 {
    parts.iter().map(|p| p.len() as i64 + 1).sum()
}

/// The runs of letters, digits and underscores in `text`, as a set:
/// `token_set()` (`[A-Za-z0-9_]+`).
fn token_set(text: &str) -> HashSet<String> {
    tokens(text).map(str::to_owned).collect()
}

/// The `[A-Za-z0-9_]+` runs of `text`, in order.
fn tokens(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .filter(|t| !t.is_empty())
}

/// Python's `str.isdigit()` for a token.
fn all_digits(t: &str) -> bool {
    !t.is_empty() && t.chars().all(py_isdigit)
}

/// `gap_marker()`: the marker between non-adjacent lines.
#[must_use]
/// `word` or `word`s for a count. The reference wrote the plural for every
/// count ("1 templates not listed"); under `PARE_REFERENCE_RULES` so does
/// this.
fn plural(n: usize, word: &str) -> String {
    if n == 1 && crate::pass::rust_rules() {
        word.to_owned()
    } else {
        format!("{word}s")
    }
}

/// `  ┆ (12 lines)`: the marker for lines left out between shown ones.
pub fn gap_marker(gap: usize) -> String {
    format!(
        "  {} ({} line{})",
        crate::text::glyphs().gap,
        fmt_int(gap as i64),
        if gap == 1 { "" } else { "s" }
    )
}

/// The longest common prefix of `texts`, in characters:
/// `os.path.commonprefix()`.
fn common_prefix(texts: &[Rc<str>]) -> String {
    let Some(first) = texts.first() else {
        return String::new();
    };
    let mut end = first.len();
    for t in &texts[1..] {
        let common = first
            .char_indices()
            .zip(t.chars())
            .find(|((_, a), b)| a != b)
            .map_or_else(|| first.len().min(t.len()), |((i, _), _)| i);
        end = end.min(common);
    }
    // A prefix shared with a shorter text ends on that text's boundary,
    // which is a boundary in `first` too (the bytes before it are equal).
    first[..end].to_owned()
}

/// What the summary knows about one line it has read back: its text and
/// the facts worked out from it, each on first use. `Line.memo` in Python.
#[derive(Default)]
struct Facts {
    // Rust note: `Rc<str>` is a reference-counted string, like a
    // `std::shared_ptr<const std::string>` without the atomic counter
    // (single-threaded). Handing out a clone costs an increment, so a
    // caller can keep the text while the cache is changed.
    text: Rc<str>,
    has: [Option<bool>; NWORD_CLASSES],
    mask: Option<Rc<str>>,
    name: Option<Rc<str>>,
    params: Option<Rc<[String]>>,
}

/// Lines read back from the spool, and their facts: `Store.line()` and the
/// `line_*` helpers of Python. Keyed by line number in the input, so a
/// follow window's carried head (numbered before the window) is a line
/// like any other.
struct Lines<'a> {
    store: &'a Store,
    vocab: &'a Vocab,
    file: File,
    facts: HashMap<usize, Facts>,
    /// The first error reading the spool. The lines read after it are
    /// empty, and [`summarize`] returns the error instead of its output.
    error: Option<io::Error>,
}

impl Lines<'_> {
    /// The line number of the store's first line.
    fn b1(&self) -> usize {
        self.store.base + 1
    }

    fn facts(&mut self, no: usize) -> &mut Facts {
        if !self.facts.contains_key(&no) {
            let b1 = self.b1();
            let text = if no < b1 {
                Rc::from(self.store.carry.as_ref().map_or("", |c| c.text.as_str()))
            } else {
                // Rust note: `b1` is read into a local first: the call
                // below borrows `self.file` mutably, and calling a `&self`
                // method in its arguments would borrow all of `self` too.
                match self.store.read_raw(&mut self.file, no - b1) {
                    Ok(raw) => Rc::from(&*clean_line(&raw)),
                    Err(e) => {
                        self.error.get_or_insert(e);
                        Rc::from("")
                    }
                }
            };
            self.facts.insert(
                no,
                Facts {
                    text,
                    ..Facts::default()
                },
            );
        }
        self.facts.get_mut(&no).expect("inserted above")
    }

    /// Line `no`'s cleaned text.
    fn text(&mut self, no: usize) -> Rc<str> {
        self.facts(no).text.clone()
    }

    /// Calls `f` with the store index and text of each line `lo..hi`
    /// (indices), read in
    /// large pieces and not kept: for scans over many lines.
    fn scan(&mut self, lo: usize, hi: usize, mut f: impl FnMut(usize, &str)) {
        let mut start = lo;
        while start < hi && self.error.is_none() {
            let end = hi.min(start + 4096);
            let mut i = start;
            let r = self
                .store
                .for_each_raw_in(&mut self.file, start, end, |raw| {
                    f(i, &clean_line(raw));
                    i += 1;
                    Ok(())
                });
            if let Err(e) = r {
                self.error.get_or_insert(e);
            }
            start = end;
        }
    }

    /// `line_has()`: whether line `no` has an entry of class `c`.
    fn has(&mut self, no: usize, c: Class) -> bool {
        let vocab = self.vocab;
        let facts = self.facts(no);
        if let Some(found) = facts.has[c as usize] {
            return found;
        }
        let found = vocab.line(&facts.text).has(c);
        facts.has[c as usize] = Some(found);
        found
    }

    fn sev(&self, no: usize) -> Option<u8> {
        if no < self.b1() {
            self.store.carry.as_ref().and_then(|c| c.sev)
        } else {
            self.store.sev[no - self.b1()]
        }
    }

    /// `is_chatter()`: a line that says nothing about what happened: a
    /// build tool's closing advice or a level with no message.
    fn is_chatter(&mut self, no: usize) -> bool {
        self.has(no, Class::Advice) || (self.sev(no).is_some() && level_only(&self.text(no)))
    }

    /// `is_error_line()`: error level or above, or, with no level, error
    /// vocabulary and no pass marker.
    fn is_error(&mut self, no: usize) -> bool {
        match self.sev(no) {
            Some(sev) => sev >= 3,
            None => self.has(no, Class::Error) && !self.has(no, Class::Pass),
        }
    }

    /// `line_params()`: the bracketed parameters of a name on the line
    /// that hold a digit.
    fn params(&mut self, no: usize) -> Rc<[String]> {
        if let Some(p) = &self.facts(no).params {
            return p.clone();
        }
        let text = self.text(no);
        let params: Rc<[String]> = PARAMS
            .captures_iter(&text)
            .filter_map(|g| g[1].map(|(s, e)| &text[s..e]))
            .filter(|p| p.chars().any(py_isdigit))
            .map(str::to_owned)
            .collect();
        self.facts(no).params = Some(params.clone());
        params
    }

    /// The severity and parameters a key adds to a masked shape.
    fn key_suffix(&mut self, no: usize) -> String {
        let mut out = String::new();
        if let Some(sev) = self.sev(no) {
            out.push_str(&format!(" ·sev{sev}"));
        }
        let params = self.params(no);
        if !params.is_empty() {
            out.push_str(&format!(" ·[{}]", params.join("][")));
        }
        out
    }

    /// `name_key()`: the masked shape with identifiers left whole, plus
    /// severity and parameters.
    fn name_key(&mut self, no: usize) -> Rc<str> {
        if let Some(k) = &self.facts(no).name {
            return k.clone();
        }
        let key: Rc<str> = format!(
            "{}{}",
            mask(&self.text(no), false, self.vocab.masks()),
            self.key_suffix(no)
        )
        .into();
        self.facts(no).name = Some(key.clone());
        key
    }

    /// `shape_key()`: the masked shape plus severity and parameters.
    fn shape_key(&mut self, no: usize) -> String {
        let masked = match &self.facts(no).mask {
            Some(m) => m.clone(),
            None => {
                let m: Rc<str> = mask(&self.text(no), true, self.vocab.masks()).into();
                self.facts(no).mask = Some(m.clone());
                m
            }
        };
        format!("{masked}{}", self.key_suffix(no))
    }
}

/// A queue item: a template entry (kind 0) or a raw line (kind 1), at a
/// priority. Python queues the tuple `(-priority, kind, id)`.
#[derive(Clone, Copy, Debug)]
struct Item {
    negp: f64,
    kind: u8,
    id: usize,
}

// Rust note: `f64` is only `PartialOrd` (NaN is unordered), so a type
// holding one cannot derive `Ord`, which `BinaryHeap` needs. This compares
// as Python compares the tuples: `partial_cmp` treats -0.0 and 0.0 as
// equal, as Python does (`f64::total_cmp` would not), and the scores are
// never NaN.
impl Ord for Item {
    fn cmp(&self, other: &Self) -> Ordering {
        self.negp
            .partial_cmp(&other.negp)
            .unwrap_or(Ordering::Equal)
            .then(self.kind.cmp(&other.kind))
            .then(self.id.cmp(&other.id))
    }
}
impl PartialOrd for Item {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl PartialEq for Item {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Item {}

/// What the summary is asked for: `summarize()`'s parameters.
pub struct Options {
    /// The budget.
    pub budget: Budget,
    /// Lines to keep from the start (`--head`).
    pub head: i64,
    /// Lines to keep from the end (`--tail`).
    pub tail: i64,
    /// Clip lines longer than this (`--width`).
    pub width: i64,
    /// List at most this many templates; 0 for all that fit.
    pub max_templates: i64,
    /// The run file as the run line shows it; `None` under `--no-tee`.
    pub run_path: Option<String>,
    /// Why there is no run file, when there is a reason worth printing (a
    /// piped input over `$PARE_RUN_MAX_MB`).
    pub not_saved: Option<String>,
    /// Bytes of input.
    pub in_bytes: u64,
    /// Added to the run line (command mode: the exit status).
    pub note: Option<String>,
    /// Follow mode: which window this is, and whether a window with
    /// nothing new is left out.
    pub follow: Option<Follow>,
}

/// A follow window: `FollowInfo` in Python.
#[derive(Clone, Copy, Debug)]
pub struct Follow {
    /// The window's number, from 1.
    pub k: usize,
    /// `--quiet`: no heartbeat line for a window with nothing new.
    pub quiet: bool,
}

/// The state of one summary: `summarize()`'s locals and the closures'
/// shared variables.
// Rust note: Python's closures capture the variables of the enclosing
// function and change them freely. In Rust, a closure that mutates what it
// captures borrows it mutably, and two such closures cannot exist at once;
// with a dozen helpers calling each other, the idiomatic shape is a struct
// holding the state and methods taking `&mut self`.
struct Summarizer<'a> {
    store: &'a Store,
    lines: Lines<'a>,
    opts: &'a Options,
    total: usize,
    /// Line number of the store's first line (1, or a follow window's).
    b1: usize,
    /// Follow mode, a window after the first: the head of the input was
    /// shown before, and a template first seen in this window is news.
    later_window: bool,
    /// Template salience, with follow mode's novelty bonus.
    sal: Vec<f64>,
    /// Templates first seen in this window, in a later window.
    new_count: usize,
    budget_bytes: i64,
    compact: bool,
    run_line: String,
    show_hint: bool,
    ledger_reserve: i64,
    ledger_detail: bool,
    /// Templates by salience, highest first (ties: first seen first).
    by_sal: Vec<usize>,
    head_nos: Vec<usize>,
    tail_nos: Vec<usize>,
    /// The windows as first chosen (shedding does not change it).
    window_nos: HashSet<usize>,
    /// Line numbers already displayed.
    shown: HashSet<usize>,
    /// Repeat key → lines already shown with it.
    taken: HashMap<String, u32>,
    keys: HashMap<usize, String>,
    shown_tokens: Vec<HashSet<String>>,
    /// Template → its entry's lines. Ordered so iteration is deterministic.
    placed: BTreeMap<usize, Vec<String>>,
    pairs: HashMap<usize, Option<usize>>,
    listing: HashMap<usize, bool>,
    tpl_records: Option<HashMap<usize, Vec<usize>>>,
    affixes: HashMap<usize, Option<(String, String)>>,
    items: HashMap<usize, Option<String>>,
    rare_items: HashMap<usize, Rc<HashSet<usize>>>,
    exemplar_nos: HashSet<usize>,
    /// Raw-line candidates, highest initial priority first, and how far
    /// the queue has taken them.
    order: Vec<(usize, f64)>,
    stream_pos: usize,
    heap: BinaryHeap<Reverse<Item>>,
    deferred: Vec<Item>,
    picked: Vec<(f64, usize)>,
    picked_nos: HashSet<usize>,
    placed_order: Vec<usize>,
}

/// Summarises the input in `store` (its lines in the spool at `spool`)
/// to `opts.budget`. `summarize()` in Python. In batch mode the caller has
/// already passed through input that fits ([`passthrough`]); a follow
/// window is passed through here, and a later window with nothing new is
/// one line, or nothing with `--quiet`.
///
/// # Errors
///
/// If the spool cannot be read back.
#[allow(clippy::too_many_lines)] // summarize()'s set-up, kept in Python's order
pub fn summarize(store: &Store, vocab: &Vocab, spool: &Path, opts: &Options) -> io::Result<String> {
    let lines = Lines {
        store,
        vocab,
        file: File::open(spool)?,
        facts: HashMap::new(),
        error: None,
    };
    let budget_bytes = opts.budget.bytes();
    let later_window = opts.follow.is_some_and(|f| f.k > 1);
    // A shape first seen in this window is news; the batch summary has no
    // "before" to be new against, so this is follow-only.
    let sal: Vec<f64> = store
        .templates
        .iter()
        .map(|t| {
            if later_window && t.new {
                t.salience + NOVELTY_BONUS
            } else {
                t.salience
            }
        })
        .collect();
    let mut by_sal: Vec<usize> = (0..store.templates.len()).collect();
    // Rust note: `sort_by` is stable, like Python's `sorted`; the index is
    // part of the key anyway.
    by_sal.sort_by(|&a, &b| {
        sal[b]
            .partial_cmp(&sal[a])
            .unwrap_or(Ordering::Equal)
            .then(a.cmp(&b))
    });
    let new_count = store.templates.iter().filter(|t| t.new).count();
    let (b1, last) = (store.base + 1, store.base + store.len());
    let mut run_line = match (&opts.run_path, opts.follow) {
        (Some(p), Some(_)) => format!("## raw: {p} --since L{b1} --until L{last}"),
        (Some(p), None) => format!("## raw: {p}"),
        (None, _) => raw_line(None, opts.not_saved.as_deref()),
    };
    if let Some(note) = &opts.note {
        run_line.push_str(&format!(" {} {note}", crate::text::glyphs().dot));
    }
    if let Some(f) = opts.follow {
        let total = store.len();
        let lines_word = if total == 1 { "line" } else { "lines" };
        let g = crate::text::glyphs();
        let (dot, times, dash) = (g.dot, g.times, g.dash);
        // A window with nothing above the salience floor and no new shape is
        // one line, so a quiet stream costs almost nothing to watch.
        let anything = (0..total)
            .any(|i| store.rec[i].is_some() && !store.blank[i] && store.score[i] >= FILL_FLOOR);
        if later_window && new_count == 0 && !anything {
            if f.quiet {
                return Ok(String::new());
            }
            let mut top: Vec<usize> = (0..store.templates.len()).collect();
            top.sort_by_key(|&t| (Reverse(store.templates[t].count), t));
            top.truncate(5);
            let counts: Vec<String> = top
                .iter()
                .map(|&t| {
                    format!(
                        "{} {times}{}",
                        store.templates[t].tid(),
                        fmt_int(i64::from(store.templates[t].count))
                    )
                })
                .collect();
            let more = if store.templates.len() > top.len() {
                format!(" {dot} +{} more", store.templates.len() - top.len())
            } else {
                String::new()
            };
            return Ok(format!(
                "# pare #{}: L{}{dash}L{} {dot} {} {lines_word}, nothing new {dot} {}{more}\n",
                f.k,
                fmt_int(b1 as i64),
                fmt_int(last as i64),
                fmt_int(total as i64),
                counts.join(", ")
            ));
        }
        // Small input: never make it bigger. A follow window keeps its
        // header line so blocks stay delimited, and its run line.
        let mut verbatim = vec![format!(
            "# pare #{}: L{}{dash}L{} {dot} {} {lines_word}, verbatim",
            f.k,
            fmt_int(b1 as i64),
            fmt_int(last as i64),
            fmt_int(total as i64)
        )];
        if let Some(c) = &store.carry {
            // The window starts inside a record: say what its first lines
            // belong to.
            verbatim.push(format!(
                "  {} continues L{}: {}",
                g.cont,
                c.no,
                clip(py_strip(&c.text), opts.width)
            ));
        }
        let end = match (&opts.run_path, &opts.note) {
            (Some(_), _) => Some(run_line.clone()),
            (None, Some(note)) => Some(format!("## {note}")),
            (None, None) => None,
        };
        let size = store.verbatim_bytes as i64
            + nbytes(&verbatim)
            + end.as_ref().map_or(0, |e| e.len() as i64 + 1);
        if size <= opts.budget.passthrough_bytes() {
            let mut lines = lines;
            let mut text = verbatim.join("\n");
            text.push('\n');
            for no in b1..=last {
                text.push_str(&lines.text(no));
                text.push('\n');
            }
            if let Some(end) = end {
                text.push_str(&end);
                text.push('\n');
            }
            return match lines.error.take() {
                Some(e) => Err(e),
                None => Ok(text),
            };
        }
    }
    let tokens = opts.budget.tokens;
    let divisor = if tokens < COMPACT_BUDGET { 16 } else { 12 };
    let mut s = Summarizer {
        store,
        lines,
        opts,
        total: store.len(),
        b1,
        later_window,
        sal,
        new_count,
        budget_bytes,
        compact: tokens < COMPACT_BUDGET,
        run_line,
        // The hint says how to expand the run file; without one (--no-tee)
        // there is nothing to expand. The reference printed it regardless.
        show_hint: tokens >= HINT_BUDGET
            && !later_window
            && (opts.run_path.is_some() || !crate::pass::rust_rules()),
        // Rust note: `div_euclid` floors like Python's `//` (the budget can
        // be negative).
        ledger_reserve: budget_bytes.div_euclid(divisor).clamp(120, 400),
        ledger_detail: true,
        by_sal,
        head_nos: Vec::new(),
        tail_nos: Vec::new(),
        window_nos: HashSet::new(),
        shown: HashSet::new(),
        taken: HashMap::new(),
        keys: HashMap::new(),
        shown_tokens: Vec::new(),
        placed: BTreeMap::new(),
        pairs: HashMap::new(),
        listing: HashMap::new(),
        tpl_records: None,
        affixes: HashMap::new(),
        items: HashMap::new(),
        rare_items: HashMap::new(),
        exemplar_nos: HashSet::new(),
        order: Vec::new(),
        stream_pos: 0,
        heap: BinaryHeap::new(),
        deferred: Vec::new(),
        picked: Vec::new(),
        picked_nos: HashSet::new(),
        placed_order: Vec::new(),
    };
    let text = s.run();
    match s.lines.error.take() {
        Some(e) => Err(e),
        None => Ok(text),
    }
}

impl Summarizer<'_> {
    /// The record's head line number; a carried record's head is in the
    /// previous window. `Store.head_of()`.
    fn head_no(&self, ri: usize) -> usize {
        match &self.store.carry {
            Some(c) if ri == 0 => c.no,
            _ => self.rstart(ri) + self.b1,
        }
    }

    /// Signed `tokens()`: the header quotes negative budgets as they are.
    fn tokens(&self, nbytes: i64) -> i64 {
        // Rust note: `as` from f64 to i64 truncates and saturates; the value
        // has just been rounded up by `ceil`, so nothing is lost.
        #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
        let t = (nbytes as f64 / self.opts.budget.chars_per_token - 1e-9).ceil() as i64;
        t
    }

    /// `"L1–L300 · 300 lines"`: a follow window's span.
    fn span(&self) -> String {
        let total = self.total as i64;
        let g = crate::text::glyphs();
        format!(
            "L{}{}L{} {} {} line{}",
            fmt_int(self.b1 as i64),
            g.dash,
            fmt_int((self.store.base + self.total) as i64),
            g.dot,
            fmt_int(total),
            if total == 1 { "" } else { "s" }
        )
    }

    fn header(&self, out_bytes: i64) -> String {
        let total = self.total as i64;
        let in_bytes = i64::try_from(self.opts.in_bytes).unwrap_or(i64::MAX);
        let g = crate::text::glyphs();
        let (arrow, dot) = (g.arrow, g.dot);
        if let Some(f) = &self.opts.follow {
            let news = if self.later_window && self.new_count > 0 {
                format!(", {} new", self.new_count)
            } else {
                String::new()
            };
            return format!(
                "# pare #{}: {}, {} B (~{} tok) {arrow} ~{} tok (budget {}) {dot} {} templates{news}",
                f.k,
                self.span(),
                fmt_int(in_bytes),
                fmt_int(self.tokens(in_bytes)),
                fmt_int(self.tokens(out_bytes)),
                self.budget_label(),
                self.store.templates.len(),
            );
        }
        format!(
            "# pare: {} line{}, {} B (~{} tok) {arrow} ~{} tok (budget {}) {dot} {} templates",
            fmt_int(total),
            if total == 1 { "" } else { "s" },
            fmt_int(in_bytes),
            fmt_int(self.tokens(in_bytes)),
            fmt_int(self.tokens(out_bytes)),
            self.budget_label(),
            self.store.templates.len(),
        )
    }

    /// The budget as the header quotes it: `2,000`, or `auto ≤2,000`.
    fn budget_label(&self) -> String {
        let tokens = fmt_int(self.opts.budget.tokens);
        if self.opts.budget.auto {
            format!("auto {}{tokens}", crate::text::glyphs().le)
        } else {
            tokens
        }
    }

    fn rec(&self, i: usize) -> Option<usize> {
        self.store.rec[i].map(|r| r as usize)
    }

    fn rtpl(&self, ri: usize) -> usize {
        self.store.rtpl[ri] as usize
    }

    fn rstart(&self, ri: usize) -> usize {
        self.store.rstart[ri] as usize
    }

    fn rend(&self, ri: usize) -> usize {
        self.store.rend[ri] as usize
    }

    /// The line number of a template's exemplar head.
    fn exemplar_no(&self, t: usize) -> usize {
        self.head_no(self.store.templates[t].exemplar as usize)
    }

    /// `fmt_line()`: a line as the summary shows it, `"223: text"`.
    fn fmt_line(&mut self, no: usize) -> String {
        let text = self.lines.text(no);
        format!("{no}: {}", clip(&text, self.opts.width))
    }

    /// `render_ranges()`: lines in order, with gap markers between runs.
    fn render_ranges(&mut self, nos: &[usize]) -> Vec<String> {
        let mut out = Vec::with_capacity(nos.len());
        let mut prev: Option<usize> = None;
        for &no in nos {
            if let Some(p) = prev {
                if no != p + 1 {
                    out.push(gap_marker(no - p - 1));
                }
            }
            out.push(self.fmt_line(no));
            prev = Some(no);
        }
        out
    }

    /// `windowed()`: the head or tail window's line numbers from the line
    /// indices `idxs`, skipping chatter and lines of frequent templates or
    /// long records, except the very first and last lines. With
    /// `keep_errors` (the tail window), an error line of a frequent
    /// template is kept when no other line of its template is in `idxs`.
    ///
    /// The skip is for chatter that recurs to the end (Maven's download
    /// lines, an emulator's routine errors); its cost was the verdict that
    /// repeats a shape seen earlier: pytest's own suite prints dozens of
    /// `FAILED` lines from the inner runs it drives, and the one real
    /// `FAILED` line near the end was folded away (SWE-bench
    /// pytest-dev__pytest-8399, `corpus/heldout/README.md`).
    fn windowed(
        &mut self,
        idxs: &[usize],
        n: i64,
        keep_no: usize,
        keep_errors: bool,
    ) -> Vec<usize> {
        let mut out = Vec::new();
        // Lines per template in the scanned span.
        let mut near_end: HashMap<usize, u32> = HashMap::new();
        if keep_errors {
            for &i in idxs {
                if let Some(ri) = self.rec(i) {
                    *near_end.entry(self.rtpl(ri)).or_default() += 1;
                }
            }
        }
        for &i in idxs {
            if out.len() as i64 >= n {
                break;
            }
            let ri = self.rec(i);
            let no = i + self.b1;
            if ri.is_some() && self.lines.is_chatter(no) {
                continue; // advice or a bare level says nothing, even first or last
            }
            let Some(ri) = ri.filter(|_| no != keep_no) else {
                out.push(no);
                continue;
            };
            // The tail window keeps an error line of a common shape when it
            // is the only line of that shape near the end: a verdict that
            // repeats a shape seen earlier (pytest's FAILED after a suite's
            // inner runs printed dozens), not noise that recurs to the end.
            // Not in Python.
            let verdict =
                keep_errors && near_end.get(&self.rtpl(ri)) == Some(&1) && self.lines.is_error(no);
            let frequent =
                !verdict && self.store.templates[self.rtpl(ri)].count >= WINDOW_SKIP_COUNT;
            let long_body = self.store.depth[i] > 0
                && self.rend(ri) - self.rstart(ri) >= WINDOW_SKIP_COUNT as usize;
            if !frequent && !long_body {
                out.push(no);
            }
        }
        out
    }

    /// `records_of()`: the first records of template `t`, at most
    /// [`LIST_SAMPLE`].
    fn records_of(&mut self, t: usize) -> Vec<usize> {
        let store = self.store;
        let all = self.tpl_records.get_or_insert_with(|| {
            let mut m: HashMap<usize, Vec<usize>> = HashMap::new();
            for ri in 0..store.nrecords() {
                let rs = m.entry(store.rtpl[ri] as usize).or_default();
                if rs.len() < LIST_SAMPLE {
                    rs.push(ri);
                }
            }
            m
        });
        all.get(&t).cloned().unwrap_or_default()
    }

    /// `lists_items()`: whether most of a template's records differ in
    /// shape (a list of failing tests, each naming its own).
    fn lists_items(&mut self, t: usize) -> bool {
        if let Some(&l) = self.listing.get(&t) {
            return l;
        }
        let heads = self.records_of(t);
        let mut shapes: HashSet<Rc<str>> = HashSet::new();
        for &ri in &heads {
            let h = self.head_no(ri);
            shapes.insert(self.lines.name_key(h));
        }
        let l = shapes.len() as f64 >= LIST_DISTINCT * heads.len() as f64;
        self.listing.insert(t, l);
        l
    }

    /// `list_affixes()`: for a list of failures, the prefix every record
    /// head shares, cut after a separator, and an empty suffix; `None`
    /// when the template is no such list or the prefix is too short.
    fn list_affixes(&mut self, t: usize) -> Option<(String, String)> {
        if let Some(a) = self.affixes.get(&t) {
            return a.clone();
        }
        let tpl = &self.store.templates[t];
        let (count, marker, ex) = (tpl.count, tpl.marker, self.exemplar_no(t));
        let mut form = None;
        if count >= LIST_MIN_COUNT
            && marker == 'E'
            && self.lists_items(t)
            && self.lines.is_error(ex)
        {
            let recs = self.records_of(t);
            let texts: Vec<Rc<str>> = recs
                .iter()
                .map(|&ri| {
                    let text = self.lines.text(self.head_no(ri));
                    Rc::from(py_strip(&text))
                })
                .collect();
            let mut pre = common_prefix(&texts);
            // Cut after a separator, never inside a name or a value.
            while let Some(c) = pre.chars().next_back() {
                if LIST_CUT.contains(c) {
                    break;
                }
                pre.pop();
            }
            let n = pre.chars().count();
            let shortest = texts.iter().map(|x| x.chars().count()).min().unwrap_or(0);
            if shortest > n && n >= LIST_MIN_SHARED {
                form = Some((pre, String::new()));
            }
        }
        self.affixes.insert(t, form.clone());
        form
    }

    /// `item_of()`: a list template's record head as an item, its text
    /// between the shared prefix and suffix; `None` for any other line.
    fn item_of(&mut self, no: usize) -> Option<String> {
        if let Some(item) = self.items.get(&no) {
            return item.clone();
        }
        if no < self.b1 {
            return None; // the carried head: the previous window's line
        }
        let i = no - self.b1;
        let item = self
            .rec(i)
            .filter(|&ri| self.rstart(ri) == i)
            .and_then(|ri| {
                let (pre, suf) = self.list_affixes(self.rtpl(ri))?;
                let text = self.lines.text(no);
                let text = py_strip(&text);
                let (n, np, ns) = (
                    text.chars().count(),
                    pre.chars().count(),
                    suf.chars().count(),
                );
                if !(text.starts_with(&pre) && text.ends_with(&suf)) || n <= np + ns {
                    return None;
                }
                Some(clip(&text[pre.len()..text.len() - suf.len()], self.opts.width).into_owned())
            });
        self.items.insert(no, item.clone());
        item
    }

    /// `rare_failures()`: line numbers of the listed failures in list
    /// template `t` that fail unlike most of the list. Each test's failure
    /// section is found by name: the last earlier record head that names
    /// it, or the item itself, up to the next head of the same kind. Its
    /// way of failing is the shape of the section's last top-level error
    /// line.
    fn rare_failures(&mut self, t: usize) -> Rc<HashSet<usize>> {
        if let Some(r) = self.rare_items.get(&t) {
            return r.clone();
        }
        let rare = Rc::new(self.find_rare_failures(t));
        self.rare_items.insert(t, rare.clone());
        rare
    }

    fn find_rare_failures(&mut self, t: usize) -> HashSet<usize> {
        if self.list_affixes(t).is_none() {
            return HashSet::new();
        }
        let heads: Vec<usize> = self
            .records_of(t)
            .iter()
            .map(|&ri| self.rstart(ri))
            .collect();
        // Name token → the items naming it; each item's parameters.
        let mut names: HashMap<String, Vec<usize>> = HashMap::new();
        let mut params: HashMap<usize, Rc<[String]>> = HashMap::new();
        for &h in &heads {
            let item = self.item_of(h + self.b1).unwrap_or_default();
            // Python's `max(toks, key=len)`: the first of the longest.
            let mut best: Option<&str> = None;
            for tk in tokens(&item).filter(|tk| !all_digits(tk)) {
                if best.is_none_or(|b| tk.len() > b.len()) {
                    best = Some(tk);
                }
            }
            if let Some(name) = best {
                names.entry(name.to_owned()).or_default().push(h);
                params.insert(h, self.lines.params(h + self.b1));
            }
        }
        if names.len() < RARE_FAILURE_MIN {
            return HashSet::new();
        }
        // The section header naming each item; itself by default.
        let mut occ: HashMap<usize, usize> = params.keys().map(|&h| (h, h)).collect();
        let (first, last) = (heads[0], *heads.last().expect("a list has records"));
        let lo = first.saturating_sub(SECTION_LOOKBACK);
        let store = self.store;
        self.lines.scan(lo, last, |i, text| {
            let Some(ri) = store.rec[i].map(|r| r as usize) else {
                return;
            };
            if store.rstart[ri] as usize != i || store.rtpl[ri] as usize == t {
                return;
            }
            let toks: HashSet<&str> = tokens(text).collect();
            for tk in toks {
                for &h in names.get(tk).into_iter().flatten() {
                    if i < h && params[&h].iter().all(|p| text.contains(p.as_str())) {
                        occ.insert(h, i); // ascending: the last one before the item wins
                    }
                }
            }
        });
        let starts: HashSet<usize> = occ.values().copied().collect();
        let mut ways: HashMap<usize, String> = HashMap::new();
        for (&h, &o) in &occ {
            let kind = self.rtpl(self.rec(o).expect("a record head"));
            let mut way = None;
            for j in o + 1..self.total.min(o + SECTION_MAX) {
                let rj = self.rec(j);
                if starts.contains(&j)
                    || rj.is_some_and(|r| {
                        self.rstart(r) == j && (self.rtpl(r) == kind || self.rtpl(r) == t)
                    })
                {
                    break; // the next section, or the list itself
                }
                if rj.is_some()
                    && self.store.depth[j] == 0
                    && !self.store.blank[j]
                    && self.lines.is_error(j + self.b1)
                {
                    way = Some(self.lines.shape_key(j + self.b1));
                }
            }
            if let Some(w) = way {
                ways.insert(h, w);
            }
        }
        if ways.len() < RARE_FAILURE_MIN {
            return HashSet::new();
        }
        let mut sizes: HashMap<&str, usize> = HashMap::new();
        for w in ways.values() {
            *sizes.entry(w).or_default() += 1;
        }
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        let limit = ((ways.len() as f64 * RARE_FAILURE_SHARE) as usize).max(1);
        ways.iter()
            .filter(|(_, w)| sizes[w.as_str()] <= limit)
            .map(|(&h, _)| h + self.b1)
            .collect()
    }

    fn rare_bonus(&mut self, i: usize) -> f64 {
        let Some(ri) = self.rec(i).filter(|&ri| self.rstart(ri) == i) else {
            return 0.0;
        };
        let t = self.rtpl(ri);
        if self.list_affixes(t).is_none() {
            return 0.0;
        }
        if self.rare_failures(t).contains(&(i + self.b1)) {
            RARE_FAILURE_BONUS
        } else {
            0.0
        }
    }

    fn item_cost(&mut self, no: usize) -> i64 {
        let item = self.item_of(no).unwrap_or_default();
        (list_item().len() + item.len()) as i64 + 1
    }

    /// `as_item()`: whether a line renders as an item of its placed list.
    fn as_item(&mut self, no: usize) -> bool {
        if no < self.b1 {
            return false;
        }
        let Some(ri) = self.rec(no - self.b1) else {
            return false;
        };
        self.placed.contains_key(&self.rtpl(ri)) && self.item_of(no).is_some()
    }

    /// `repeat_key()`: what counts as another one of these, for
    /// diminishing returns.
    fn repeat_key(&mut self, no: usize) -> String {
        let i = no - self.b1;
        if self.store.depth[i] == 0 {
            if let Some(ri) = self.rec(i) {
                let t = self.rtpl(ri);
                if !(self.lines.is_error(no) && self.lists_items(t)) {
                    let sev =
                        self.store.sev[i].map_or_else(|| "None".to_owned(), |s| s.to_string());
                    return format!("tpl{t}·sev{sev}");
                }
                // An error line of a template whose records each name
                // something different repeats only its own name.
                return self.lines.name_key(no).to_string();
            }
        }
        self.lines.shape_key(no) // a continuation line: its masked shape
    }

    fn key_of(&mut self, no: usize) -> String {
        if let Some(k) = self.keys.get(&no) {
            return k.clone();
        }
        let k = match &self.store.carry {
            // The carried head: its line is in the previous window.
            Some(c) if no < self.b1 => {
                let sev = c.sev.map_or_else(|| "None".to_owned(), |s| s.to_string());
                format!("tpl{}·sev{sev}", c.tpl_idx)
            }
            _ => self.repeat_key(no),
        };
        self.keys.insert(no, k.clone());
        k
    }

    fn note_shown(&mut self, no: usize) {
        self.shown.insert(no);
        let key = self.key_of(no);
        *self.taken.entry(key).or_default() += 1;
        let text = self.lines.text(no);
        self.shown_tokens.push(token_set(&text));
    }

    /// `is_near_duplicate()`: whether at least [`NEAR_DUP_CONTAINMENT`] of
    /// the line's tokens are in one line on screen. With `names` (an error
    /// line), only when what it adds is numbers that are not in its name's
    /// parameters.
    fn is_near_duplicate(
        &self,
        toks: &HashSet<String>,
        names: bool,
        name_toks: &HashSet<String>,
    ) -> bool {
        if toks.len() < NEAR_DUP_MIN_TOKENS {
            return false;
        }
        let need = NEAR_DUP_CONTAINMENT * toks.len() as f64;
        self.shown_tokens.iter().any(|s| {
            toks.intersection(s).count() as f64 >= need
                && (!names
                    || toks
                        .difference(s)
                        .all(|t| all_digits(t) && !name_toks.contains(t)))
        })
    }

    /// The tokens of a line's name parameters.
    fn name_toks(&mut self, no: usize) -> HashSet<String> {
        self.lines
            .params(no)
            .iter()
            .flat_map(|p| tokens(p).map(str::to_owned).collect::<Vec<_>>())
            .collect()
    }

    /// `ref_of()`: a line as a template entry shows it: referenced when the
    /// head or tail window prints it, else in full.
    fn ref_of(&mut self, no: usize) -> String {
        let where_ = if self.head_nos.contains(&no) {
            Some("head")
        } else if self.tail_nos.contains(&no) {
            Some("tail")
        } else {
            None
        };
        match where_ {
            Some(w) => format!("L{no} (in {w})"),
            None => {
                let text = self.lines.text(no);
                format!("L{no}: {}", clip(py_strip(&text), self.opts.width))
            }
        }
    }

    /// `pair_of()`: for a template seen exactly twice, the line number of
    /// the record head that is not its exemplar; `None` otherwise, at a
    /// compact budget, or when the two heads read the same.
    fn pair_of(&mut self, t: usize) -> Option<usize> {
        if let Some(&p) = self.pairs.get(&t) {
            return p;
        }
        let tpl = &self.store.templates[t];
        let (count, exemplar) = (tpl.count, tpl.exemplar as usize);
        let ex = self.exemplar_no(t);
        let mut other = None;
        if PAIR_MEMBERS
            && count == 2
            && !self.compact
            && tpl.text != drain_message(&self.lines.text(ex), self.lines.vocab.masks())
        {
            let rs: Vec<usize> = self
                .records_of(t)
                .into_iter()
                .filter(|&ri| ri != exemplar)
                .collect();
            if let [ri] = rs[..] {
                let h = self.head_no(ri);
                if h >= self.b1 && py_strip(&self.lines.text(h)) != py_strip(&self.lines.text(ex)) {
                    other = Some(h);
                }
            }
        }
        self.pairs.insert(t, other);
        other
    }

    /// `tpl_head()`: a template's entry lines.
    fn tpl_head(&mut self, t: usize) -> Vec<String> {
        let ex_no = self.exemplar_no(t);
        let tpl = &self.store.templates[t];
        let count = tpl.count;
        let tid = tpl.tid();
        let marker = tpl.marker;
        let mut extra = String::new();
        if tpl.lines != count {
            extra.push_str(&format!(" ({} lines)", fmt_int(i64::from(tpl.lines))));
        }
        if ex_no < self.b1 {
            extra.push_str(" (cont.)"); // the record began in the previous window
        }
        let levels: Vec<usize> = (0..5).rev().filter(|&k| tpl.levels[k] > 0).collect();
        if levels.len() > 1 {
            // Mixed levels inside one template: say so.
            let hist: Vec<String> = levels
                .iter()
                .map(|&k| {
                    format!(
                        "{}{}{}",
                        LEVEL_NAME[k],
                        crate::text::glyphs().times,
                        fmt_int(i64::from(tpl.levels[k]))
                    )
                })
                .collect();
            extra.push_str(&format!(" [{}]", hist.join(" ")));
        }
        if self.later_window && tpl.new {
            extra.push_str(" new");
        }
        extra.push_str(&tpl.rate_note);
        let g = crate::text::glyphs();
        let times = g.times;
        let head = format!(
            "{tid:<4} {times}{}{extra} {marker}",
            fmt_int(i64::from(count))
        );
        if let Some((pre, suf)) = self.list_affixes(t) {
            // A list of failures: the shared text once, the items under it,
            // those that fail unlike the rest first.
            let n_rare = self.rare_failures(t).len();
            let rare = if n_rare > 0 {
                format!(" ({n_rare} unlike the rest, first)")
            } else {
                String::new()
            };
            let head = format!(
                "{tid:<4} {times}{}{extra}{rare} {marker}",
                fmt_int(i64::from(count))
            );
            return vec![format!("{head} {pre}{{{}}}{suf}", g.ellipsis)];
        }
        let ex_ref = self.ref_of(ex_no);
        if let Some(other) = self.pair_of(t) {
            // Seen twice: both lines say what the pattern's <*> hides.
            let (first, second) = (ex_no.min(other), ex_no.max(other));
            let (a, b) = (self.ref_of(first), self.ref_of(second));
            return vec![format!("{head} {a}"), format!("     {b}")];
        }
        let tpl = &self.store.templates[t];
        if count == 1
            || self.compact
            || tpl.text == drain_message(&self.lines.text(ex_no), self.lines.vocab.masks())
        {
            // One line: a singleton, a small budget, or a template with no
            // variables, whose text would only repeat the exemplar.
            return vec![format!("{head} {ex_ref}")];
        }
        vec![
            format!("{head} {}", clip(&tpl.text, self.opts.width)),
            format!("     e.g. {ex_ref}"),
        ]
    }

    /// `initial()`: a line's priority before any repeat: its score per
    /// byte.
    fn initial(&mut self, i: usize) -> f64 {
        (self.store.score[i] + self.rare_bonus(i)) * density(self.store.slen[i] as usize)
    }

    /// `effective()`: a line's priority now, less for each repeat of its
    /// shape already shown; an item of a placed list costs only its part.
    fn effective(&mut self, no: usize) -> f64 {
        let i = no - self.b1;
        let key = self.key_of(no);
        let taken = f64::from(self.taken.get(&key).copied().unwrap_or(0));
        let mut e = (self.store.score[i] + self.rare_bonus(i)) / (1.0 + FILL_PENALTY * taken);
        let size = if self.as_item(no) {
            self.item_of(no).map_or(0, |s| s.len())
        } else {
            self.store.slen[i] as usize
        };
        e *= density(size);
        e
    }

    fn pending(&self) -> bool {
        !self.heap.is_empty() || self.stream_pos < self.order.len()
    }

    /// `next_item()`: the queue's next item, from the heap or from the
    /// stream of raw-line candidates, whichever comes first.
    fn next_item(&mut self) -> Item {
        let b1 = self.b1;
        let next = self.order.get(self.stream_pos).map(|&(i, p)| Item {
            negp: -p,
            kind: 1,
            id: i + b1,
        });
        if let Some(Reverse(top)) = self.heap.peek() {
            if next.is_none_or(|n| *top < n) {
                return self.heap.pop().expect("peeked").0;
            }
        }
        self.stream_pos += 1;
        next.expect("pending() was checked")
    }

    /// `place()`: the greedy pass over the queue. Returns the budget left.
    #[allow(clippy::too_many_lines)] // one loop, kept whole as in Python
    fn place(&mut self, mut avail: i64) -> i64 {
        while self.pending() && avail >= 40 {
            let item = self.next_item();
            if item.kind == 0 {
                let t = item.id;
                let tpl = &self.store.templates[t];
                let max = self.opts.max_templates;
                if max != 0 && self.placed.len() as i64 >= max {
                    continue;
                }
                let floor = if self.opts.budget.auto {
                    AUTO_TEMPLATE_FLOOR
                } else {
                    TEMPLATE_FLOOR
                };
                if self.sal[t] < floor && !self.placed.is_empty() {
                    continue; // low-salience shape: the ledger's count is enough
                }
                let ex = tpl.exemplar as usize;
                let (ex_start, ex_end) = (self.rstart(ex), self.rend(ex));
                let ex_no = self.exemplar_no(t);
                let count = tpl.count;
                let marker = tpl.marker;
                if count == 1
                    && (ex_start..ex_end)
                        .filter(|&j| !self.store.blank[j])
                        .all(|j| self.window_nos.contains(&(j + self.b1)))
                {
                    continue; // a singleton the head or tail window already prints whole
                }
                if count <= NEAR_DUP_MAX_COUNT
                    && !self.shown.contains(&ex_no)
                    && ex_end - ex_start == 1
                {
                    // A rare one-line record that restates something already
                    // on screen stays in the ledger.
                    let toks = token_set(&self.lines.text(ex_no));
                    let names = self.lines.is_error(ex_no);
                    let name_toks = self.name_toks(ex_no);
                    if self.is_near_duplicate(&toks, names, &name_toks) {
                        continue;
                    }
                }
                let entry = self.tpl_head(t);
                let mut cost = nbytes(&entry);
                if (marker == 'E' || marker == 'W') && ex_end - ex_start > 1 {
                    cost += 30; // room for the "(+N more lines)" marker under it
                }
                let is_list = self.list_affixes(t).is_some();
                if is_list {
                    cost += 30; // the "(+N more)" marker under the items
                    if !self.window_nos.contains(&ex_no) && self.item_of(ex_no).is_some() {
                        cost += self.item_cost(ex_no); // the exemplar is the first item
                    }
                }
                if cost > avail && !self.placed.is_empty() {
                    self.deferred.push(item);
                    continue; // the first template always gets a line
                }
                self.placed.insert(t, entry);
                avail -= cost;
                for no in [Some(ex_no), self.pair_of(t)].into_iter().flatten() {
                    if !self.shown.contains(&no) {
                        self.note_shown(no);
                    } else if self.picked_nos.remove(&no) {
                        // The entry now shows this line.
                        self.picked.retain(|&(_, n)| n != no);
                        avail += i64::from(self.store.rlen[no - self.b1]) + 20;
                    }
                }
                if is_list {
                    // Lines of the list picked before it: now items, and cheaper.
                    let picked: Vec<usize> = self.picked.iter().map(|&(_, n)| n).collect();
                    for n in picked {
                        if self.as_item(n) {
                            avail +=
                                i64::from(self.store.rlen[n - self.b1]) + 20 - self.item_cost(n);
                        }
                    }
                }
                continue;
            }
            let no = item.id;
            let cur = self.effective(no);
            // Rust note: exact float comparison on purpose: a stale priority
            // is one computed before a repeat was shown, and recomputing an
            // unchanged one gives the same bits.
            #[allow(clippy::float_cmp)]
            if cur != -item.negp {
                self.heap.push(Reverse(Item {
                    negp: -cur,
                    kind: 1,
                    id: no,
                }));
                continue; // stale: re-queued at the current score
            }
            if cur < FILL_FLOOR || self.shown.contains(&no) {
                continue; // below the floor a line never pads the output
            }
            let i = no - self.b1;
            let ri = self.rec(i).expect("candidates are in records");
            if self.exemplar_nos.contains(&no) && self.placed.contains_key(&self.rtpl(ri)) {
                continue; // its template entry already shows it
            }
            let cost = if self.as_item(no) {
                self.item_cost(no)
            } else {
                i64::from(self.store.rlen[i]) + 20
            };
            if cost > avail {
                self.deferred.push(item);
                continue;
            }
            let toks = token_set(&self.lines.text(no));
            if self.store.depth[i] == 0 {
                let names = self.lines.is_error(no);
                let name_toks = self.name_toks(no);
                if self.is_near_duplicate(&toks, names, &name_toks) {
                    continue;
                }
            }
            if self.store.depth[i] > 0
                && toks.len() >= NEAR_DUP_MIN_TOKENS
                && self.shown_tokens.iter().any(|t| toks.is_subset(t))
            {
                continue; // a continuation line that restates a shown line
            }
            self.picked.push((cur, no));
            self.picked_nos.insert(no);
            self.note_shown(no);
            avail -= cost;
        }
        avail
    }

    /// `body_of()`: a placed template's body: the picked lines inside its
    /// exemplar record, and a marker for the rest.
    fn body_of(&mut self, t: usize) -> (Vec<String>, HashSet<usize>) {
        let ex = self.store.templates[t].exemplar as usize;
        // `Store.body_range()`: a carried record's lines here are all body.
        let carried = ex == 0 && self.store.carry.is_some();
        let (lo, end) = (self.rstart(ex) + usize::from(!carried), self.rend(ex));
        let b1 = self.b1;
        let mut nos: Vec<usize> = self
            .picked
            .iter()
            .map(|&(_, n)| n)
            .filter(|&n| n >= b1 && (lo..end).contains(&(n - b1)))
            .collect();
        nos.sort_unstable();
        let mut out = if nos.is_empty() {
            Vec::new()
        } else {
            self.render_ranges(&nos)
        };
        let unseen = (lo..end)
            .filter(|&j| !self.store.blank[j] && !self.window_nos.contains(&(j + b1)))
            .count();
        if unseen > nos.len() {
            out.push(format!(
                "  {} (+{} more {})",
                crate::text::glyphs().gap,
                fmt_int((unseen - nos.len()) as i64),
                plural(unseen - nos.len(), "line")
            ));
        }
        (out, nos.into_iter().collect())
    }

    /// `ledger()`: what was left out, per template.
    fn ledger(&self, shown_set: &HashSet<usize>) -> String {
        let store = self.store;
        let tpl_omitted = store.templates.len() - self.placed.len();
        let omitted_total = self.total - shown_set.iter().filter(|&&no| no >= self.b1).count();
        let mut shown_in: HashMap<usize, i64> = HashMap::new();
        let mut blank_shown = 0;
        for &no in shown_set {
            if no < self.b1 {
                continue; // a carried head is the previous window's line
            }
            let j = no - self.b1;
            if store.blank[j] {
                blank_shown += 1;
            }
            if let Some(ri) = self.rec(j) {
                *shown_in.entry(self.rtpl(ri)).or_default() += 1;
            }
        }
        let mut per_tpl: Vec<(i64, usize)> = store
            .templates
            .iter()
            .enumerate()
            .map(|(t, tpl)| {
                (
                    i64::from(tpl.lines) - shown_in.get(&t).copied().unwrap_or(0),
                    t,
                )
            })
            .filter(|&(om, _)| om != 0)
            .collect();
        per_tpl.sort_by_key(|&(om, t)| (Reverse(om), t));
        #[allow(clippy::cast_precision_loss)]
        let pct = if self.total > 0 {
            100.0 * omitted_total as f64 / self.total as f64
        } else {
            0.0
        };
        let head = format!(
            "## omitted {}/{} lines ({pct:.1}%)",
            fmt_int(omitted_total as i64),
            fmt_int(self.total as i64)
        );
        let mut tail_bits: Vec<String> = Vec::new();
        if tpl_omitted > 0 {
            tail_bits.push(format!(
                "{tpl_omitted} {} not listed",
                plural(tpl_omitted, "template")
            ));
        }
        let blank_total = store.blank.iter().filter(|&&b| b).count();
        if blank_total > blank_shown {
            tail_bits.push(format!(
                "{} blank",
                fmt_int((blank_total - blank_shown) as i64)
            ));
        }
        // Fit the per-template list into the reserve (lengths in characters,
        // as Python's len()).
        let mut shown_parts: Vec<String> = Vec::new();
        let mut used = head.chars().count() as i64 + 2;
        for &(om, t) in &per_tpl {
            let p = format!(
                "{} {}{}",
                store.templates[t].tid(),
                crate::text::glyphs().times,
                fmt_int(om)
            );
            let len = p.chars().count() as i64;
            if !self.ledger_detail || used + len + 2 > self.ledger_reserve - 40 {
                break;
            }
            shown_parts.push(p);
            used += len + 2;
        }
        let rest = per_tpl.len() - shown_parts.len();
        if rest > 0 {
            tail_bits.insert(0, format!("+{rest} more templates"));
        }
        let mut line = head;
        if !shown_parts.is_empty() {
            line.push_str(": ");
            line.push_str(&shown_parts.join(", "));
        }
        if !tail_bits.is_empty() {
            line.push_str(&format!(" {} ", crate::text::glyphs().dot));
            line.push_str(&tail_bits.join("; "));
        }
        line
    }

    /// `render()`: the summary's lines below the header.
    fn render(&mut self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        if let (Some(&first), Some(&last)) = (self.head_nos.first(), self.head_nos.last()) {
            out.push(format!(
                "## head (L{first}{}L{last})",
                crate::text::glyphs().dash
            ));
            let head = self.head_nos.clone();
            out.extend(self.render_ranges(&head));
        }
        let mut in_bodies: HashSet<usize> = HashSet::new();
        if !self.placed.is_empty() {
            out.push("## templates (by salience)".to_owned());
            for t in self.by_sal.clone() {
                let Some(entry) = self.placed.get(&t) else {
                    continue;
                };
                out.extend(entry.iter().cloned());
                if self.list_affixes(t).is_some() {
                    let mut items: Vec<usize> = Vec::new();
                    for (_, n) in self.picked.clone() {
                        if self.as_item(n)
                            && self.rtpl(self.rec(n - self.b1).expect("an item is a head")) == t
                        {
                            items.push(n);
                        }
                    }
                    let ex_no = self.exemplar_no(t);
                    if !self.window_nos.contains(&ex_no)
                        && self.item_of(ex_no).is_some()
                        && !items.contains(&ex_no)
                    {
                        items.push(ex_no);
                    }
                    let rare = self.rare_failures(t);
                    items.sort_by_key(|n| (!rare.contains(n), *n));
                    for &n in &items {
                        let item = self.item_of(n).unwrap_or_default();
                        out.push(format!("{}{item}", list_item()));
                    }
                    let in_windows = self
                        .window_nos
                        .iter()
                        .filter(|&&n| {
                            n >= self.b1
                                && self.rec(n - self.b1).is_some_and(|ri| {
                                    self.rtpl(ri) == t && self.rstart(ri) == n - self.b1
                                })
                        })
                        .count() as i64;
                    let ex_extra =
                        i64::from(!items.contains(&ex_no) && !self.window_nos.contains(&ex_no));
                    let rest = i64::from(self.store.templates[t].count)
                        - items.len() as i64
                        - in_windows
                        - ex_extra;
                    if rest > 0 {
                        out.push(format!(
                            "  {} (+{} more)",
                            crate::text::glyphs().gap,
                            fmt_int(rest)
                        ));
                    }
                    in_bodies.extend(items);
                }
                let (body, nos) = self.body_of(t);
                out.extend(body);
                in_bodies.extend(nos);
            }
        }
        let mut rest_nos: Vec<usize> = self
            .picked
            .iter()
            .map(|&(_, n)| n)
            .filter(|n| !in_bodies.contains(n))
            .collect();
        rest_nos.sort_unstable();
        if !rest_nos.is_empty() {
            out.push("## selected lines".to_owned());
            out.extend(self.render_ranges(&rest_nos));
        }
        if let (Some(&first), Some(&last)) = (self.tail_nos.first(), self.tail_nos.last()) {
            out.push(format!(
                "## tail (L{first}{}L{last})",
                crate::text::glyphs().dash
            ));
            let tail = self.tail_nos.clone();
            out.extend(self.render_ranges(&tail));
        }
        let mut shown_set: HashSet<usize> = self
            .head_nos
            .iter()
            .chain(&self.tail_nos)
            .copied()
            .collect();
        shown_set.extend(self.picked.iter().map(|&(_, n)| n));
        for &t in self.placed.keys() {
            shown_set.insert(self.exemplar_no(t));
        }
        out.push(self.ledger(&shown_set));
        out.push(self.run_line.clone());
        if self.show_hint {
            out.push(run_hint());
        }
        out
    }

    /// `shed()`: drops the next thing to give up for the exact fit; false
    /// when nothing is left to drop.
    fn shed(&mut self) -> bool {
        if self.picked.pop().is_some() {
        } else if !self.tail_nos.is_empty() {
            self.tail_nos.remove(0);
        } else if self.head_nos.pop().is_some() {
        } else if self.placed_order.len() > 1 {
            let t = self.placed_order.pop().expect("checked");
            self.placed.remove(&t);
        } else if self.ledger_detail {
            self.ledger_detail = false;
        } else if self.show_hint {
            self.show_hint = false;
        } else if let Some(t) = self.placed_order.pop() {
            self.placed.remove(&t);
        } else {
            return false;
        }
        true
    }

    /// `measure()`: the output's size with this body. The header quotes the
    /// size, so its own length depends on the answer: iterated to a fixed
    /// point.
    fn measure(&self, body: &[String]) -> i64 {
        let mut ob = nbytes(body) + self.header(0).len() as i64 + 1;
        for _ in 0..4 {
            let ob2 = nbytes(body) + self.header(ob).len() as i64 + 1;
            if ob2 == ob {
                break;
            }
            ob = ob2;
        }
        ob
    }

    fn sort_picked(&mut self) {
        self.picked.sort_by(|a, b| {
            b.0.partial_cmp(&a.0)
                .unwrap_or(Ordering::Equal)
                .then(a.1.cmp(&b.1))
        });
        let placed = &self.placed;
        self.placed_order = self
            .by_sal
            .iter()
            .copied()
            .filter(|t| placed.contains_key(t))
            .collect();
    }

    /// The summary. The rest of `summarize()` after its locals.
    fn run(&mut self) -> String {
        let total = self.total;
        let tokens = self.opts.budget.tokens;
        // Fixed overhead: header, run line, section titles, ledger reserve.
        let fixed_parts: Vec<String> = [
            self.header(self.budget_bytes),
            self.run_line.clone(),
            if self.show_hint {
                run_hint()
            } else {
                String::new()
            },
            "## head".to_owned(),
            "## templates (by salience)".to_owned(),
            "## selected lines".to_owned(),
            "## tail".to_owned(),
        ]
        .into();
        let fixed = nbytes(&fixed_parts) + self.ledger_reserve;
        let mut avail = (self.budget_bytes - fixed).max(0);

        // Head and tail windows: at most 30 % of the budget together (for
        // an automatic budget, of the budget they are sized for).
        let window_avail = (self.opts.budget.sizing_bytes() - fixed).clamp(0, avail);
        let mut head_n = self.opts.head;
        if tokens < COMPACT_BUDGET {
            head_n = head_n.min(3); // a small budget gets a smaller head (the tail keeps the verdicts)
        }
        if self.later_window {
            head_n = 0; // the head of the input was shown in the first window
        }
        let tail_n = self.opts.tail;
        let head_span = head_n.max(WINDOW_SCAN_FACTOR * head_n);
        let tail_span = tail_n.max(WINDOW_SCAN_FACTOR * tail_n);
        let total_i = total as i64;
        // Rust note: Python's `range()` with a negative or empty span is
        // just empty; here the bounds are worked out in `i64` and turned
        // into indices (`try_from` fails only for a negative bound, which
        // means an empty range).
        let head_end = usize::try_from(total_i.min(head_span)).unwrap_or(0);
        let head_idxs: Vec<usize> = (0..head_end).collect();
        let tail_start = usize::try_from((total_i - 1 - tail_span).max(-1) + 1).unwrap_or(0);
        let tail_idxs: Vec<usize> = (tail_start..total).rev().collect();
        let mut head_nos = self.windowed(&head_idxs, head_n, self.b1, false);
        let mut tail_nos = self.windowed(
            &tail_idxs,
            tail_n,
            self.store.base + total,
            TAIL_KEEPS_ERRORS && crate::pass::rust_rules(),
        );
        tail_nos.sort_unstable();
        tail_nos.retain(|n| !head_nos.contains(n));
        while !head_nos.is_empty() || !tail_nos.is_empty() {
            let cost =
                nbytes(&self.render_ranges(&head_nos)) + nbytes(&self.render_ranges(&tail_nos));
            #[allow(clippy::cast_precision_loss)]
            if (cost as f64) <= window_avail as f64 * 0.3 {
                break;
            }
            if tail_nos.len() >= head_nos.len() && !tail_nos.is_empty() {
                tail_nos.remove(0);
            } else if !head_nos.is_empty() {
                head_nos.pop();
            }
        }
        let head_block = self.render_ranges(&head_nos);
        let tail_block = self.render_ranges(&tail_nos);
        avail -= nbytes(&head_block) + nbytes(&tail_block);
        self.shown.extend(head_nos.iter().chain(&tail_nos));
        self.window_nos = self.shown.clone();
        self.head_nos = head_nos;
        self.tail_nos = tail_nos;

        let mut sorted_shown: Vec<usize> = self.shown.iter().copied().collect();
        sorted_shown.sort_unstable();
        for no in sorted_shown {
            let text = self.lines.text(no);
            self.shown_tokens.push(token_set(&text));
        }
        self.exemplar_nos = (0..self.store.templates.len())
            .map(|t| self.exemplar_no(t))
            .collect();

        // Raw-line candidates: everything not in a window that scores at
        // least the floor; a template's exemplar head too, so it can still
        // appear when its entry does not make the cut.
        let mut order: Vec<(usize, f64)> = Vec::new();
        for i in 0..total {
            if !self.shown.contains(&(i + self.b1))
                && self.store.rec[i].is_some()
                && !self.store.blank[i]
                && self.store.score[i] >= FILL_FLOOR
            {
                let p = self.initial(i);
                order.push((i, p));
            }
        }
        // Stable: ties stay in line order.
        order.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(Ordering::Equal));
        self.order = order;
        for (t, &sal) in self.sal.iter().enumerate() {
            self.heap.push(Reverse(Item {
                negp: -sal,
                kind: 0,
                id: t,
            }));
        }

        self.place(avail);

        // Exact fit, after offering what did not fit against the measured
        // slack a few times: the per-item allowances are conservative.
        self.sort_picked();
        for _ in 0..3 {
            let body = self.render();
            let slack = self.budget_bytes - self.measure(&body);
            if slack < 40 || !(!self.deferred.is_empty() || self.pending()) {
                break;
            }
            for item in self.deferred.drain(..) {
                self.heap.push(Reverse(item));
            }
            let (placed_before, picked_before) = (self.placed.len(), self.picked.len());
            self.place(slack);
            if self.placed.len() == placed_before && self.picked.len() == picked_before {
                break;
            }
            self.sort_picked();
        }

        let (mut body, mut out_bytes);
        loop {
            body = self.render();
            out_bytes = self.measure(&body);
            if out_bytes <= self.budget_bytes || !self.shed() {
                break;
            }
        }
        // Absurdly small budget: even the skeleton does not fit. Cut from
        // the end, but keep the run line to the last.
        while out_bytes > self.budget_bytes && !body.is_empty() {
            if body.last() == Some(&self.run_line) && body.len() > 1 {
                body.remove(body.len() - 2);
            } else {
                body.pop();
            }
            out_bytes = self.measure(&body);
        }
        let mut text = self.header(out_bytes);
        text.push('\n');
        for line in &body {
            text.push_str(line);
            text.push('\n');
        }
        if text.len() as i64 > self.budget_bytes {
            text = truncate(&text, self.budget_bytes - 1);
        }
        text
    }
}

/// `(REF / (REF + size)) ** POWER`: value per byte.
fn density(size: usize) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let ratio = DENSITY_REF as f64 / (DENSITY_REF + size) as f64;
    ratio.powf(DENSITY_POWER)
}

/// The first `stop` bytes of `text` (a negative `stop` counts from the
/// end), without a character the cut split, right-stripped, and a newline:
/// Python's `text.encode()[:stop].decode("utf-8", errors="ignore")
/// .rstrip() + "\n"`.
fn truncate(text: &str, stop: i64) -> String {
    let len = text.len() as i64;
    let stop = if stop < 0 {
        (len + stop).max(0)
    } else {
        stop.min(len)
    };
    let mut end = usize::try_from(stop).unwrap_or(0);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}\n", py_rstrip(&text[..end]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn helpers_match_python() {
        assert_eq!(
            common_prefix(&[Rc::from("abc"), Rc::from("abd"), Rc::from("ab")]),
            "ab"
        );
        assert_eq!(common_prefix(&[Rc::from("é1"), Rc::from("é2")]), "é");
        assert_eq!(plural(1, "template"), "template");
        assert_eq!(plural(2, "template"), "templates");
        assert_eq!(gap_marker(1), "  ┆ (1 line)");
        assert_eq!(gap_marker(1200), "  ┆ (1,200 lines)");
        assert_eq!(truncate("ab é", 4), "ab\n"); // the cut splits é; the space is stripped
        assert_eq!(truncate("abc", -1), "ab\n");
        let toks: Vec<&str> = tokens("FAILED t.py::test_a[1-2] é_x").collect();
        assert_eq!(toks, ["FAILED", "t", "py", "test_a", "1", "2", "_x"]);
        assert!(all_digits("42") && !all_digits("4a") && !all_digits(""));
    }

    #[test]
    fn queue_orders_like_python_tuples() {
        let a = Item {
            negp: -2.0,
            kind: 1,
            id: 9,
        };
        let b = Item {
            negp: -2.0,
            kind: 0,
            id: 3,
        };
        let c = Item {
            negp: -0.0,
            kind: 1,
            id: 1,
        };
        let d = Item {
            negp: 0.0,
            kind: 1,
            id: 1,
        };
        assert!(b < a && a < c && c == d);
        let mut heap: BinaryHeap<Reverse<Item>> = [a, b, c].into_iter().map(Reverse).collect();
        assert_eq!(heap.pop().map(|r| r.0.id), Some(3)); // the template wins a tie
    }
}
