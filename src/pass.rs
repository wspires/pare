//! The single pass over the input: the `Pass` class and `analyze()` of
//! `reference/pare.py`.
//!
//! Input arrives in chunks (1 MiB from a file or pipe, less from a slow
//! command). Each chunk is written to the spool and fed to the hash as it
//! is, then split into lines; a line cut by the end of a chunk is carried
//! over and completed by the next one. Each complete line is cleaned
//! ([`crate::text::clean_line`]) and handed to the [`Segmenter`], which
//! groups lines into records and clusters each record's head line.
//!
//! Records: a stack trace is one unit, as is a compiler diagnostic with its
//! code snippet. A line indented deeper than its record's head, or shaped
//! like a continuation (a stack frame, a caret marker, `Caused by:`, ...),
//! joins the record above; any other line starts a record. One blank line
//! inside an indented block does not end it; two do.
//!
//! Each line is also scored as it goes by: its structural severity and
//! its vocabulary ([`crate::vocab`]), and for a continuation line a share
//! of its record head's score ([`crate::score`]).

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::io::{self, Read, Write};
use std::sync::LazyLock;
use std::time::Instant;

use regex::Regex;
use sha2::{Digest, Sha256};

use crate::drain::Drain;
use crate::mask::drain_message;
use crate::pyclass::{py_lstrip, py_pattern, py_strip};
use crate::pyre::PyRegex;
use crate::runs::py_format_g;
use crate::score::{DEPTH_PENALTY, INHERIT, LOCATION_BONUS, py_max0};
use crate::store::{CarriedHead, HeadClasses, Store, Template};
use crate::text::{clean_line, clip, indent_of};
use crate::vocab::{Class, Vocab, vocab_score};

/// Bytes read per chunk, as `CHUNK` in Python.
pub const CHUNK: usize = 1 << 20;

/// Records are capped at this many lines; the next line starts a new one.
pub const MAX_RECORD_LINES: u32 = 400;

/// Tool-specific rules, all on (the Python version has a switch per rule
/// for `bench/ablate.py`; the port keeps them as constants).
pub(crate) const TOOL_PYTEST: bool = true;
pub(crate) const TOOL_RUSTC: bool = true;
pub(crate) const TOOL_LOGCAT: bool = true;
const TOOL_BLANK_IN_BLOCK: bool = true;

/// Lines that continue the record above whatever their indentation:
/// `CONT_GENERIC`, `CONT_RUSTC` and `CONT_PYTEST` in Python.
static CONTINUATION: LazyLock<Regex> = LazyLock::new(|| {
    let mut p = String::from(concat!(
        r#"^\s+at\s"#,                                         // JS/Java stack frame
        r#"|^\s*File "[^"]+", line \d+"#,                      // Python frame
        r#"|^\s*\^+\s*$"#,                                     // caret marker
        r#"|^\s*~+\s*$"#,                                      // tilde underline
        r#"|^Caused by:"#,                                     // Java/Rust cause chain
        r#"|^\s*\.\.\. \d+ more"#,                             // Java elided frames
        r#"|^\s*(?:Expected|Received|Actual|Got|Want|Diff):"#, // assertion detail
        r#"|^\s*[+-]{3}\s"#,                                   // diff headers
        r#"|^\s*@@ "#,                                         // diff hunk
    ));
    if TOOL_RUSTC {
        p.push_str(r#"|^\s*\d*\s*\|(?:\s|$)|^\s*-->\s|^\s*= (?:note|help|warning):|^\s*::: "#);
    }
    if TOOL_PYTEST {
        p.push_str(r#"|^E\s{2,}|^>\s{2,}"#); // pytest failure detail and source marker lines
    }
    Regex::new(&py_pattern(&p)).expect("continuation pattern is valid")
});

/// A logcat line's header, threadtime (`10-13 09:12:31.620  4321  4321 E
/// AndroidRuntime: ...`) or time format (`... E/AndroidRuntime( 4321): ...`).
/// One Log call printed over several lines repeats the same header, the
/// timestamp included, on each.
static LOGCAT_HEADER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&py_pattern(concat!(
        r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(?:(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]*?)\s*:",
        r"|([VDIWEF])/([^(]*)\(\s*(\d+)\):)",
    )))
    .expect("logcat pattern is valid")
});

/// A logcat line saying a process died: after its `FATAL EXCEPTION`, part
/// of the failure, not routine.
static LOGCAT_DEATH: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(r"Process \S+ \(pid (\d+)\) has died").expect("logcat death pattern is valid")
});

/// The fields of a logcat header, compared to tell whether two lines belong
/// to one log entry (Python compares the match's group tuple).
type LogcatKey = [Option<String>; 8];

/// What a template is across follow windows: its cluster and how many
/// records it has had in the whole input so far. `GlobalTemplate` in
/// Python; its index is the template's id (`gid`).
struct Global {
    first_window: usize,
    total: u32,
}

/// A raw line cleaned and taken apart: what segmenting it needs.
struct Parsed<'a> {
    text: Cow<'a, str>,
    indent: i64,
    logcat: Option<LogcatKey>,
    /// Where the message starts in `text`: after the logcat header, or 0.
    message_at: usize,
}

impl Parsed<'_> {
    fn message(&self) -> &str {
        &self.text[self.message_at..]
    }
}

fn parse(raw: &[u8]) -> Parsed<'_> {
    let text = clean_line(raw);
    let indent = indent_of(&text);
    let mut logcat: Option<LogcatKey> = None;
    let mut message_at = 0;
    if TOOL_LOGCAT {
        if let Some(caps) = LOGCAT_HEADER.captures(&text) {
            // Rust note: `std::array::from_fn` builds the fixed-size array
            // element by element from a closure; each element is an
            // `Option<String>` because a group that did not take part in
            // the match is `None`, as in Python.
            logcat = Some(std::array::from_fn(|g| {
                caps.get(g + 1).map(|m| m.as_str().to_owned())
            }));
            let end = caps.get(0).map_or(0, |m| m.end());
            message_at = end + usize::from(text[end..].starts_with(' '));
        }
    }
    Parsed {
        text,
        indent,
        logcat,
        message_at,
    }
}

/// Groups lines into records, clusters each record's head, and scores each
/// line: `Pass._add()`, `Pass.cut()` and the segmentation state of `Pass`
/// in Python.
///
/// Batch mode cuts one window, at the end of the input. Follow mode cuts a
/// window at each flush: Drain, the templates' ids and cumulative counts,
/// the line counter and the spool offset carry across windows, and a
/// record open at a cut is carried into the next window, whose first lines
/// may continue it.
// Rust note: `<'v>` is a lifetime parameter: a `Segmenter` holds a
// reference to a `Vocab` it does not own, and the compiler checks that the
// vocabulary outlives every segmenter using it. Python reads a global
// `VOCAB`; passing it in makes the dependency visible and lets tests use
// their own.
pub struct Segmenter<'v> {
    vocab: &'v Vocab,
    /// The current window's lines, records and templates.
    store: Store,
    drain: Drain,
    width: i64,
    /// Drain cluster id − 1 → the template's id across windows.
    // Rust note: a `Vec` indexed by id rather than a `HashMap`: Drain's ids
    // are dense (1, 2, 3, ...), so the lookup is an array access.
    by_cluster: Vec<Option<usize>>,
    globals: Vec<Global>,
    /// Template id → its index in this window's templates.
    window_tpl: HashMap<usize, u32>,
    window_no: usize,
    /// Lines so far, in all windows.
    nlines: usize,
    /// Spool offset of the next line.
    pos: u64,
    /// Follow mode: the window cap in lines (0: none). See
    /// [`Segmenter::window_full`].
    pub every: usize,
    /// The open record, if any.
    cur: Option<u32>,
    /// A single blank line that may turn out to sit inside the open record.
    pending_blank: Option<u32>,
    /// Indentation of the open record's head line.
    head_indent: i64,
    /// The logcat header of the last non-blank line, if it had one.
    logcat_key: Option<LogcatKey>,
    /// The open record's head's vocabulary score, which its continuation
    /// lines inherit a share of.
    head_vocab: f64,
    /// An error line was seen in the open record and no source location
    /// since: the next one gets [`LOCATION_BONUS`].
    armed: bool,
    /// Logcat pids that printed a `FATAL EXCEPTION`.
    crashed_pids: HashSet<String>,
    /// The open record's head: its text, line number and severity, for
    /// carrying it over a cut.
    head_text: String,
    head_no: usize,
    head_sev: Option<u8>,
    /// The record open at the last cut, until a line ends it, and how many
    /// lines it already had.
    carry: Option<CarriedHead>,
    carry_len: u32,
    /// A blank line at the end of the last window that may sit inside the
    /// carried record (one more ends it).
    pending_carried: bool,
    /// What the cut saw of the open record, for the next window.
    pending_blank_at_cut: bool,
    store_prev_len: u32,
    carry_gid: usize,
    carry_cid: usize,
    /// The highest severity seen in the current window (follow triggers).
    pub worst_sev: Option<u8>,
    started: Instant,
    window_started: Instant,
}

impl<'v> Segmenter<'v> {
    /// A segmenter that scores with `vocab` and clips rendered lines at
    /// `width` characters.
    #[must_use]
    pub fn new(vocab: &'v Vocab, width: i64) -> Self {
        let now = Instant::now();
        Segmenter {
            vocab,
            store: Store::new(),
            drain: Drain::new(),
            width,
            by_cluster: Vec::new(),
            globals: Vec::new(),
            window_tpl: HashMap::new(),
            window_no: 0,
            nlines: 0,
            pos: 0,
            every: 0,
            cur: None,
            pending_blank: None,
            head_indent: 0,
            logcat_key: None,
            head_vocab: 0.0,
            armed: false,
            crashed_pids: HashSet::new(),
            head_text: String::new(),
            head_no: 0,
            head_sev: None,
            carry: None,
            carry_len: 0,
            pending_carried: false,
            pending_blank_at_cut: false,
            store_prev_len: 0,
            carry_gid: 0,
            carry_cid: 0,
            worst_sev: None,
            started: now,
            window_started: now,
        }
    }

    /// The current window's lines so far.
    #[must_use]
    pub fn window_len(&self) -> usize {
        self.store.len()
    }

    /// Whether the current window holds a non-blank line.
    #[must_use]
    pub fn has_content(&self) -> bool {
        self.store.blank.iter().any(|&b| !b)
    }

    /// Lines so far, in all windows.
    #[must_use]
    pub fn nlines(&self) -> usize {
        self.nlines
    }

    /// Templates so far, in all windows.
    #[must_use]
    pub fn ntemplates(&self) -> usize {
        self.globals.len()
    }

    /// Whether a line continues the open record, by its shape: deeper
    /// indentation or a continuation form. A logcat line is judged by its
    /// message, and only within one entry (same header).
    fn joins(&self, p: &Parsed<'_>) -> bool {
        match &p.logcat {
            Some(key) => {
                let message = p.message();
                self.logcat_key.as_ref() == Some(key)
                    && (indent_of(message) > self.head_indent || CONTINUATION.is_match(message))
            }
            None => p.indent > self.head_indent || CONTINUATION.is_match(&p.text),
        }
    }

    /// Follow mode: whether the window is full and `raw` starts a record,
    /// so the window should be cut before it. Cutting only where a record
    /// starts keeps the segmentation, and with it the template ids, what a
    /// batch pass over the run file finds. A window can exceed `every` by
    /// one record.
    #[must_use]
    pub fn window_full(&self, raw: &[u8]) -> bool {
        if self.every == 0 || self.store.len() < self.every {
            return false;
        }
        let p = parse(raw);
        if py_strip(&p.text).is_empty() {
            return false;
        }
        let continues = self.cur.is_some_and(|cur| {
            let c = cur as usize;
            self.store.rend[c] - self.store.rstart[c] < MAX_RECORD_LINES && self.joins(&p)
        });
        !continues
    }

    /// Takes one raw line (without its newline).
    #[allow(clippy::too_many_lines)] // one step of the pass, kept whole as in Python
    pub fn add_line(&mut self, raw: &[u8]) {
        let p = parse(raw);
        let text: &str = &p.text;
        let indent = p.indent;
        let message = p.message();
        let stripped = py_strip(text);

        let i = u32::try_from(self.store.len()).expect("fewer than 4 billion lines");
        let base = self.store.base;
        let store = &mut self.store;
        store.push(raw.len(), text.len());
        self.pos += raw.len() as u64 + 1;
        self.nlines += 1;
        let rendered = format!("{}: {}", base + i as usize + 1, clip(text, self.width));
        store
            .rlen
            .push(u32::try_from(rendered.len() + 1).unwrap_or(u32::MAX));
        store
            .slen
            .push(u32::try_from(stripped.chars().count()).unwrap_or(u32::MAX));
        let mut sev = self.vocab.severity(text);
        if let Some(key) = &p.logcat {
            // Threadtime or time format: the pid is group 2 or group 8.
            let pid = key[1]
                .as_ref()
                .filter(|p| !p.is_empty())
                .or(key[7].as_ref());
            if message.starts_with("FATAL EXCEPTION") {
                self.crashed_pids.extend(pid.cloned());
            } else if !self.crashed_pids.is_empty() {
                if let Some(groups) = LOGCAT_DEATH.captures_at(message, 0) {
                    let (s, e) = groups[1].expect("the pid group is not optional");
                    if self.crashed_pids.contains(&message[s..e]) {
                        // The crashed process is gone: part of the failure.
                        sev = Some(sev.unwrap_or(0).max(3));
                    }
                }
            }
        }
        store.sev.push(sev);
        if sev > self.worst_sev {
            self.worst_sev = sev;
        }

        if stripped.is_empty() {
            // Blank lines end a record and belong to none, except that a
            // single blank line may sit inside an indented block.
            self.logcat_key = None;
            store.blank.push(true);
            store.rec.push(None);
            store.depth.push(0);
            store.score.push(0.0);
            if self.carry.is_some() && self.cur.is_none() {
                // Same rule for the carried record: one blank keeps it open.
                if self.pending_carried || !TOOL_BLANK_IN_BLOCK {
                    self.carry = None;
                } else {
                    self.pending_carried = true;
                }
                return;
            }
            if self.pending_blank.is_some() || self.cur.is_none() || !TOOL_BLANK_IN_BLOCK {
                self.cur = None;
            }
            self.pending_blank = self.cur.map(|_| i);
            return;
        }
        store.blank.push(false);

        if self.cur.is_none() {
            if let Some(mut carry) = self.carry.take() {
                // The first non-blank line of a window after a cut that left
                // a record open: does it continue that record? The same test
                // as below, against the carried head and the record's length
                // so far.
                if self.carry_len < MAX_RECORD_LINES && self.joins(&p) {
                    self.continue_carried(&mut carry, i);
                }
            }
        }
        // Segmentation: a deeper indent or a known continuation shape joins
        // the line to the record above.
        let is_cont = self.cur.is_some_and(|cur| {
            let c = cur as usize;
            let carried = if c == 0 && self.store.carry.is_some() {
                self.carry_len
            } else {
                0
            };
            self.store.rend[c] - self.store.rstart[c] + carried < MAX_RECORD_LINES && self.joins(&p)
        });
        // The text the line is scored by: a logcat continuation line is
        // judged by its message alone (a frame is a frame, not an error
        // line), and gets the entry's weight by inheritance.
        let scored: &str = if is_cont && p.logcat.is_some() {
            sev = self.vocab.severity(message);
            *self.store.sev.last_mut().expect("pushed above") = sev;
            message
        } else {
            text
        };
        let mut line = self.vocab.line(scored);
        let store = &mut self.store;
        let mut s;
        if let (true, Some(cur)) = (is_cont, self.cur) {
            let c = cur as usize;
            let t = store.rtpl[c] as usize;
            if let Some(pb) = self.pending_blank {
                // The blank line before this one turned out to be inside
                // the block: it joins the record too.
                store.rec[pb as usize] = Some(cur);
                store.depth[pb as usize] = 1;
                store.templates[t].lines += 1;
            }
            store.rend[c] = i + 1;
            let own_indent = if p.logcat.is_some() {
                indent_of(message)
            } else {
                indent
            };
            // Rust note: `div_euclid` rounds toward minus infinity, as
            // Python's `//` does; Rust's `/` truncates toward zero, as C++
            // does. They differ for negative values (a line indented less
            // than its head, joined by its shape), though `.max(1)` then
            // makes both 1: `div_euclid` is the faithful translation, and
            // stays right if the clamp ever changes.
            let d = (own_indent - self.head_indent + 1).div_euclid(2).max(1);
            store.rec.push(Some(cur));
            store.depth.push(u32::try_from(d).unwrap_or(u32::MAX));
            store.templates[t].lines += 1;
            // Its own vocabulary, plus a share of its record head's, less a
            // little per level of depth.
            // Rust note: `as f64` converts the integer exactly (it is at
            // most 4); Python multiplies the float by the int the same way.
            s = vocab_score(&mut line, false, sev);
            s -= DEPTH_PENALTY * d.min(4) as f64;
            if !(line.has(Class::Pass) && !line.has(Class::Error)) {
                s += INHERIT * py_max0(self.head_vocab);
            }
        } else {
            let cur = u32::try_from(store.rstart.len()).expect("fewer than 4 billion records");
            self.cur = Some(cur);
            store.rstart.push(i);
            store.rend.push(i + 1);
            self.head_indent = if p.logcat.is_some() {
                indent_of(message)
            } else {
                indent
            };
            self.head_text = text.to_owned();
            self.head_no = base + i as usize + 1;
            self.head_sev = sev;
            // Cluster the masked head line.
            let cluster = self.drain.add(&drain_message(text));
            if self.by_cluster.len() < cluster {
                self.by_cluster.resize(cluster, None);
            }
            let window_no = self.window_no;
            let globals = &mut self.globals;
            let gid = *self.by_cluster[cluster - 1].get_or_insert_with(|| {
                globals.push(Global {
                    first_window: window_no,
                    total: 0,
                });
                globals.len() - 1
            });
            let g = &mut self.globals[gid];
            g.total += 1;
            let new = g.first_window == window_no;
            let mut exemplar = false;
            let t = *self.window_tpl.entry(gid).or_insert_with(|| {
                let t =
                    u32::try_from(store.templates.len()).expect("fewer than 4 billion templates");
                store.templates.push(Template::new(cluster, gid, cur, new));
                exemplar = true;
                t
            });
            store.rtpl.push(t);
            let tpl = &mut store.templates[t as usize];
            tpl.count += 1;
            tpl.lines += 1;
            if let Some(level) = sev {
                tpl.levels[usize::from(level)] += 1;
            }
            // Rust note: `Option<u8>` compares with `None` below any
            // `Some`, which is Python's `(sev or -1) > best_sev` with -1
            // for none.
            if sev > tpl.best_sev {
                tpl.best_sev = sev;
                tpl.exemplar = cur;
                exemplar = true;
            }
            store.rec.push(Some(cur));
            store.depth.push(0);
            self.armed = false;
            s = vocab_score(&mut line, true, sev);
            self.head_vocab = s;
            if exemplar {
                // What score_templates needs of the exemplar head, while
                // its facts are at hand.
                tpl.vocab = s;
                tpl.head = head_classes(&mut line);
            }
        }
        // The first source location after an error line within a record
        // (the `at file:line` under an exception, the `-->` under a rustc
        // error) says where the failure is; the frames after it say how it
        // got there.
        let is_loc = line.has(Class::Frame) && !py_lstrip(scored).starts_with('>');
        if self.armed && is_loc {
            s += LOCATION_BONUS;
            self.armed = false;
        } else if sev.is_some_and(|l| l >= 3) || (line.has(Class::Error) && !is_loc) {
            self.armed = true;
        }
        store.score.push(s);
        self.logcat_key = p.logcat;
        self.pending_blank = None;
    }

    /// Record 0 of this window continues the carried record, starting at
    /// line `i`; its template is the carried head's.
    fn continue_carried(&mut self, carry: &mut CarriedHead, i: u32) {
        let store = &mut self.store;
        let t = *self.window_tpl.entry(carry.gid).or_insert_with(|| {
            let t = u32::try_from(store.templates.len()).expect("fewer than 4 billion templates");
            let mut tpl = Template::new(carry.cluster_id, carry.gid, 0, false);
            // What score_templates needs of the exemplar head, the carried
            // one: Python reads it from the carried head.
            let mut line = self.vocab.line(&carry.text);
            tpl.vocab = vocab_score(&mut line, true, carry.sev);
            tpl.head = head_classes(&mut line);
            store.templates.push(tpl);
            t
        });
        carry.tpl_idx = t as usize;
        store.carry = Some(carry.clone());
        self.cur = Some(u32::try_from(store.rstart.len()).expect("fewer than 4 billion records"));
        store.rstart.push(i);
        store.rend.push(i); // the continuation extends it
        store.rtpl.push(t);
        let tpl = &mut store.templates[t as usize];
        // A record this window touches; the cumulative total is unchanged.
        tpl.count += 1;
        if let Some(level) = carry.sev {
            tpl.levels[usize::from(level)] += 1;
        }
        tpl.best_sev = carry.sev;
    }

    /// Closes the current window and returns it, ready to score and
    /// summarise: each template takes its cluster's text and its total so
    /// far. `Pass.cut()`.
    pub fn cut(&mut self) -> Store {
        if let Some(cur) = self.cur {
            let store = &self.store;
            let c = cur as usize;
            self.store_prev_len = store.rend[c] - store.rstart[c] + self.carry_len;
            let tpl = &store.templates[store.rtpl[c] as usize];
            self.carry_gid = tpl.gid;
            self.carry_cid = tpl.cluster_id;
        }
        self.pending_blank_at_cut = self.pending_blank.is_some();
        let next = Store::starting_at(self.nlines, self.pos);
        let mut store = std::mem::replace(&mut self.store, next);
        store.total_bytes = if store.is_empty() {
            0
        } else {
            self.pos - store.first_offset()
        };
        for tpl in &mut store.templates {
            tpl.text = self.drain.cluster(tpl.cluster_id).template();
            tpl.total = self.globals[tpl.gid].total;
        }
        self.window_no += 1;
        self.rate_notes(&mut store);
        self.new_window();
        store
    }

    /// A template arriving much faster than its running average is what an
    /// outage looks like before the first ERROR line. Needs real time on
    /// both sides (a replayed file gets no notes). `Pass._rate_notes()`.
    fn rate_notes(&self, store: &mut Store) {
        let now = Instant::now();
        let window_s = now.duration_since(self.window_started).as_secs_f64();
        let before_s = self
            .window_started
            .duration_since(self.started)
            .as_secs_f64();
        if window_s < 1.0 || before_s < 1.0 {
            return;
        }
        for tpl in &mut store.templates {
            let before = tpl.total - tpl.count;
            if tpl.count < RATE_MIN_COUNT || before < 1 {
                continue;
            }
            let (now_r, before_r) = (
                f64::from(tpl.count) / window_s,
                f64::from(before) / before_s,
            );
            if now_r >= RATE_FACTOR * before_r {
                tpl.rate_note = format!(
                    " (rate {}/s → {}/s)",
                    py_format_g(before_r, 2),
                    py_format_g(now_r, 2)
                );
            }
        }
    }

    /// `Pass._new_window()`: the state a new window starts with. A record
    /// open at the cut is carried: its head's indentation, vocabulary score
    /// and the location bonus's state keep their values, and only `cur` is
    /// reset, since its lines so far belong to the previous window.
    fn new_window(&mut self) {
        self.window_tpl.clear();
        self.window_started = Instant::now();
        self.worst_sev = None;
        if self.cur.is_some() {
            self.carry = Some(CarriedHead {
                no: self.head_no,
                text: self.head_text.clone(),
                sev: self.head_sev,
                tpl_idx: 0,
                gid: self.carry_gid,
                cluster_id: self.carry_cid,
            });
            self.carry_len = self.store_prev_len;
            self.pending_carried = self.pending_blank_at_cut;
        } else if self.carry.is_some() {
            // No line of the window ended the carried record (the window
            // held only the blank line that keeps it open): carry on
            // unchanged, length and pending-blank state included.
        } else {
            self.carry_len = 0;
            self.head_indent = 0;
            self.head_vocab = 0.0;
            self.armed = false;
            self.pending_carried = false;
        }
        self.cur = None;
        self.pending_blank = None;
    }
}

/// The classes of a template's exemplar head that its marker depends on.
fn head_classes(line: &mut crate::vocab::Line<'_, '_>) -> HeadClasses {
    HeadClasses {
        error: line.has(Class::Error),
        warn: line.has(Class::Warn),
        summary: line.has(Class::Summary),
        pass: line.has(Class::Pass),
    }
}

/// A rate note needs this many records in the window...
const RATE_MIN_COUNT: u32 = 20;
/// ...arriving at least this many times the running rate.
const RATE_FACTOR: f64 = 3.0;

/// The pass in progress: reading, spooling and hashing, then segmenting.
// Rust note: `Pass<W: Write>` is generic over where the spool goes: a
// `File` in pare, a `Vec<u8>` in the tests. As with a C++ template, the
// compiler makes one copy per type, so the calls are direct, not virtual;
// unlike a template, the bound (`Write`) is checked here, once.
pub struct Pass<'v, W: Write> {
    spool: W,
    hasher: Sha256,
    total_bytes: u64,
    /// The start of a line that the last chunk cut off.
    rest: Vec<u8>,
    seg: Segmenter<'v>,
}

impl<'v, W: Write> Pass<'v, W> {
    /// A pass that writes the input to `spool`, scores with `vocab`, and
    /// clips rendered lines at `width` characters.
    pub fn new(spool: W, vocab: &'v Vocab, width: i64) -> Self {
        Pass {
            spool,
            hasher: Sha256::new(),
            total_bytes: 0,
            rest: Vec::new(),
            seg: Segmenter::new(vocab, width),
        }
    }

    /// Takes the next chunk of input.
    ///
    /// # Errors
    ///
    /// If writing to the spool fails.
    pub fn feed(&mut self, chunk: &[u8]) -> io::Result<()> {
        self.feed_with(chunk, &mut |_| {})
    }

    /// Takes the next chunk of input, calling `on_full` with the segmenter
    /// before a line that should start a new window (follow mode; see
    /// [`Segmenter::window_full`]).
    ///
    /// # Errors
    ///
    /// If writing to the spool fails.
    // Rust note: `&mut dyn FnMut(...)` is a closure behind a pointer, called
    // through a vtable like a `std::function&`: one compiled `feed_with` for
    // every caller, which is all this per-line callback needs.
    pub fn feed_with(
        &mut self,
        chunk: &[u8],
        on_full: &mut dyn FnMut(&mut Segmenter<'v>),
    ) -> io::Result<()> {
        self.spool.write_all(chunk)?;
        self.hasher.update(chunk);
        self.total_bytes += chunk.len() as u64;

        let mut start = 0;
        // Rust note: `memchr_iter` finds every newline with SIMD, the way
        // `memchr(3)` does, and the lines are slices of `chunk`: borrowed
        // views, no copies. Each `&chunk[a..b]` is bounds-checked; the
        // checks are cheap here and keep the code free of `unsafe`.
        for nl in memchr::memchr_iter(b'\n', chunk) {
            let line = &chunk[start..nl];
            if self.rest.is_empty() {
                take_line(&mut self.seg, line, on_full);
            } else {
                self.rest.extend_from_slice(line);
                // Rust note: `take_line(&mut self.seg, &self.rest, ..)`
                // borrows two different fields of `self`, one mutably and one
                // not, which the borrow checker allows. A `&mut self` method
                // on `Pass` taking `&self.rest` would need all of `self`
                // mutably while part of it is borrowed, and would not compile.
                take_line(&mut self.seg, &self.rest, on_full);
                self.rest.clear();
            }
            start = nl + 1;
        }
        self.rest.extend_from_slice(&chunk[start..]);
        Ok(())
    }

    /// Ends the input: a last line without a newline is still a line.
    /// Returns the store, the content hash's first 12 hex digits (the run
    /// file's name), and the spool writer, flushed.
    ///
    /// # Errors
    ///
    /// If flushing the spool fails.
    pub fn finish(mut self) -> io::Result<(Store, String, W)> {
        self.finish_lines(&mut |_| {});
        self.spool.flush()?;
        let digest = self.hasher.finalize();
        let hex: String = digest.iter().take(6).map(|b| format!("{b:02x}")).collect();
        let mut store = self.seg.cut();
        store.total_bytes = self.total_bytes;
        Ok((store, hex, self.spool))
    }

    /// End of input without ending the pass (follow mode): a last line
    /// without a newline is still a line.
    pub fn finish_lines(&mut self, on_full: &mut dyn FnMut(&mut Segmenter<'v>)) {
        if !self.rest.is_empty() {
            let rest = std::mem::take(&mut self.rest);
            take_line(&mut self.seg, &rest, on_full);
        }
    }

    /// The segmenter, for follow mode's flushes.
    pub fn segmenter(&mut self) -> &mut Segmenter<'v> {
        &mut self.seg
    }

    /// Bytes of input so far.
    #[must_use]
    pub fn total_bytes(&self) -> u64 {
        self.total_bytes
    }

    /// Flushes the spool, so a window's lines can be read back.
    ///
    /// # Errors
    ///
    /// If flushing fails.
    pub fn flush_spool(&mut self) -> io::Result<()> {
        self.spool.flush()
    }
}

/// One line into the segmenter, cutting the window first if it is full.
fn take_line<'v>(
    seg: &mut Segmenter<'v>,
    line: &[u8],
    on_full: &mut dyn FnMut(&mut Segmenter<'v>),
) {
    if seg.window_full(line) {
        on_full(seg);
    }
    seg.add_line(line);
}

/// Reads all of `input` through a pass that writes it to `spool`.
///
/// # Errors
///
/// If reading the input or writing the spool fails.
pub fn read_all<R: Read, W: Write>(
    mut input: R,
    spool: W,
    vocab: &Vocab,
    width: i64,
) -> io::Result<(Store, String, W)> {
    let mut pass = Pass::new(spool, vocab, width);
    let mut buf = vec![0u8; CHUNK];
    loop {
        // Rust note: `read` returns how many bytes it filled; 0 means end
        // of input. A signal can interrupt it (`Interrupted`), which is not
        // an error, just a reason to read again; Python retries for you.
        let n = match input.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        };
        pass.feed(&buf[..n])?;
    }
    pass.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    static VOCAB: LazyLock<Vocab> = LazyLock::new(Vocab::builtin);

    fn run(chunks: &[&[u8]]) -> (Store, String, Vec<u8>) {
        let mut pass = Pass::new(Vec::new(), &VOCAB, 200);
        for c in chunks {
            pass.feed(c).unwrap();
        }
        pass.finish().unwrap()
    }

    #[test]
    fn lines_cut_by_chunks_are_rejoined() {
        let (whole, h1, spool1) = run(&[b"alpha\nbeta\ngamma"]);
        let (split, h2, spool2) = run(&[b"al", b"pha\nbe", b"ta\n", b"gam", b"ma"]);
        assert_eq!((whole.len(), split.len()), (3, 3));
        assert_eq!(whole.verbatim_bytes, split.verbatim_bytes);
        assert_eq!(h1, h2);
        assert_eq!(spool1, spool2);
    }

    #[test]
    fn trailing_newline_makes_no_extra_line() {
        assert_eq!(run(&[b"a\nb\n"]).0.len(), 2);
        assert_eq!(run(&[b"a\nb"]).0.len(), 2);
        assert_eq!(run(&[b"\n\n"]).0.len(), 2);
        assert_eq!(run(&[b""]).0.len(), 0);
    }

    /// Segments `text` and returns, per line, (record, depth), with -1 for none.
    fn segment(text: &str) -> (Vec<(i64, u32)>, Store) {
        let mut seg = Segmenter::new(&VOCAB, 200);
        for line in text.split('\n') {
            seg.add_line(line.as_bytes());
        }
        let store = seg.cut();
        let rows = (0..store.len())
            .map(|i| (store.rec[i].map_or(-1, i64::from), store.depth[i]))
            .collect();
        (rows, store)
    }

    #[test]
    fn indented_lines_and_frames_continue_a_record() {
        let (rows, store) =
            segment("ERROR boom\n  detail\nCaused by: x\nat_col0\n    at f(A.java:1)");
        assert_eq!(rows, [(0, 0), (0, 1), (0, 1), (1, 0), (1, 2)]);
        assert_eq!(store.nrecords(), 2);
    }

    #[test]
    fn one_blank_line_inside_a_block_is_kept_two_end_it() {
        let (rows, _) = segment("ERROR x\n  a\n\n  b\n\n\n  c");
        // The single blank joins record 0; after two blanks "  c" starts a record.
        assert_eq!(
            rows,
            [(0, 0), (0, 1), (0, 1), (0, 1), (-1, 0), (-1, 0), (1, 0)]
        );
    }

    #[test]
    fn records_are_capped() {
        let text = format!("head\n{}", "  more\n".repeat(MAX_RECORD_LINES as usize));
        let (rows, store) = segment(text.trim_end());
        assert_eq!(store.rend[0] - store.rstart[0], MAX_RECORD_LINES);
        assert_eq!(rows.last(), Some(&(1, 0))); // line 401 starts a new record
    }

    #[test]
    fn logcat_entries_split_on_a_new_header() {
        let line = |ts: &str, msg: &str| {
            format!("10-13 09:12:31.{ts}  4321  4321 E AndroidRuntime: {msg}")
        };
        let text = [
            line("620", "FATAL EXCEPTION: main"),
            line("620", "java.lang.NullPointerException: x"),
            line("620", "\tat a.B.c(B.java:1)"),
            line("621", "\tat a.B.d(B.java:2)"), // a new header: a new record
        ]
        .join("\n");
        let (rows, _) = segment(&text);
        // The exception heads a record within the entry and its frame
        // continues it; the frame under a new header does not.
        assert_eq!(rows, [(0, 0), (1, 0), (1, 2), (2, 0)]);
    }

    #[test]
    fn heads_cluster_into_templates() {
        let (_, store) = segment("ERROR worker 3 crashed\nERROR worker 5 crashed\nINFO started");
        assert_eq!(store.templates.len(), 2);
        assert_eq!(store.templates[0].text, "ERROR worker <NUM> crashed");
        assert_eq!((store.templates[0].count, store.templates[0].total), (2, 2));
        assert_eq!(store.rtpl, [0, 0, 1]);
    }

    /// Per line: (template id, depth) of its record, or `None` for a blank
    /// line outside any record, over all of `stores`.
    fn by_line(stores: &[Store]) -> Vec<Option<(usize, u32)>> {
        let mut out = Vec::new();
        for st in stores {
            for i in 0..st.len() {
                out.push(
                    st.rec[i]
                        .map(|r| (st.templates[st.rtpl[r as usize] as usize].gid, st.depth[i])),
                );
            }
        }
        out
    }

    #[test]
    fn follow_windows_segment_as_batch_does() {
        // A window is cut only where a record starts, and a record open at a
        // cut is carried, so every line ends up in the same template at the
        // same depth as in one pass over the whole input.
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/corpus/android-logcat.log");
        let data = std::fs::read(path).unwrap();
        let (batch, _, _) = run(&[&data]);
        for every in [1, 7, 50] {
            let mut pass = Pass::new(Vec::new(), &VOCAB, 200);
            pass.segmenter().every = every;
            let mut windows = Vec::new();
            for chunk in data.chunks(4096) {
                pass.feed_with(chunk, &mut |seg| windows.push(seg.cut()))
                    .unwrap();
            }
            pass.finish_lines(&mut |seg| windows.push(seg.cut()));
            windows.push(pass.segmenter().cut());
            assert!(windows.len() > 2, "every {every}");
            assert_eq!(
                by_line(&windows),
                by_line(std::slice::from_ref(&batch)),
                "every {every}"
            );
            let lines: usize = windows.iter().map(Store::len).sum();
            assert_eq!(lines, batch.len());
        }
    }

    #[test]
    fn hash_matches_python() {
        // Python: hashlib.sha256(b"hello\n").hexdigest()[:12]
        assert_eq!(run(&[b"hello\n"]).1, "5891b5b522d5");
    }
}
