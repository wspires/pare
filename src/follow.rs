//! Follow mode, `pare --follow`: summarise a stream as it arrives, one
//! block per window instead of one summary at the end. `follow()` in
//! `reference/pare.py`.
//!
//! A window closes after `--every` lines (where a record starts, so a
//! window boundary never splits one), after `--interval` seconds, about
//! `--settle` seconds after a line at the `--flush-on` severity (at most
//! one such flush per `--min-interval`), and at end of input. Template ids
//! stay the same across windows; a later window marks the shapes it sees
//! for the first time as new, and one with nothing worth showing is one
//! line (`--quiet`: none).

use std::io::{self, Write};
use std::path::Path;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use crate::budget::Budget;
use crate::command::{CommandRun, read_in_background};
use crate::error::{Error, Result};
use crate::pass::{Pass, Segmenter};
use crate::score::score_templates;
use crate::summary::{self, Follow, fmt_int};
use crate::vocab::Vocab;

/// Once the command has exited, stop after this long without output, as
/// in batch command mode.
const EXIT_GRACE: Duration = Duration::from_secs(1);
/// How often a command is checked on while its output is quiet.
const POLL: Duration = Duration::from_millis(200);

/// What follow mode is asked for.
pub struct Settings {
    /// The budget per window.
    pub budget: Budget,
    /// `--head`, `--tail`, `--width`, `--max-templates`.
    pub head: i64,
    /// Lines to keep from the end of each window.
    pub tail: i64,
    /// Clip lines longer than this.
    pub width: i64,
    /// List at most this many templates.
    pub max_templates: i64,
    /// `--every`: the window cap in lines.
    pub every: usize,
    /// `--interval`: flush a non-empty window every this many seconds
    /// (0: never).
    pub interval: f64,
    /// `--flush-on`: the severity that schedules a flush (`None`: none).
    pub flush_sev: Option<u8>,
    /// `--settle`: how long after such a line.
    pub settle: f64,
    /// `--min-interval`: at most one such flush per this many seconds.
    pub min_interval: f64,
    /// `--quiet`.
    pub quiet: bool,
}

/// Bytes written to the output, for the command's `--stats` record.
/// `CountingWriter` in Python.
struct Counting<W: Write> {
    out: W,
    nbytes: u64,
}

impl<W: Write> Counting<W> {
    fn write(&mut self, text: &str) -> io::Result<()> {
        self.nbytes += text.len() as u64;
        self.out.write_all(text.as_bytes())?;
        self.out.flush()
    }
}

/// The state the flushes share: `follow()`'s locals that its `flush`
/// closure changes.
struct State<'a, W: Write> {
    settings: &'a Settings,
    vocab: &'a Vocab,
    spool: &'a Path,
    run_path: Option<&'a str>,
    out: Counting<W>,
    /// Windows written so far.
    k: usize,
    last_flush: Instant,
    /// A `--flush-on` flush is scheduled for this time.
    due: Option<Instant>,
    /// The first error in a flush called from inside the pass, reported
    /// once the pass returns.
    error: Option<Error>,
}

impl<W: Write> State<'_, W> {
    /// `flush()`: cuts the window and writes its block. A window with
    /// nothing in it but blank lines prints nothing and is not cut (the
    /// blank line stays in the open window, and may join a carried record);
    /// the last window is cut whatever it holds.
    fn flush(&mut self, seg: &mut Segmenter<'_>, last: bool) {
        if let Err(e) = self.try_flush(seg, last) {
            self.error.get_or_insert(e);
        }
        self.last_flush = Instant::now();
        self.due = None;
    }

    fn try_flush(&mut self, seg: &mut Segmenter<'_>, last: bool) -> Result<()> {
        if seg.window_len() == 0 || (!seg.has_content() && !last) {
            return Ok(());
        }
        self.k += 1;
        let mut store = seg.cut();
        score_templates(&mut store, self.vocab, self.spool)
            .map_err(|e| Error::io("reading the run file back", e))?;
        let s = self.settings;
        let opts = summary::Options {
            budget: s.budget,
            head: s.head,
            tail: s.tail,
            width: s.width,
            max_templates: s.max_templates,
            run_path: self.run_path.map(str::to_owned),
            in_bytes: store.total_bytes,
            note: None,
            follow: Some(Follow {
                k: self.k,
                quiet: s.quiet,
            }),
        };
        let block = summary::summarize(&store, self.vocab, self.spool, &opts)
            .map_err(|e| Error::io("reading the run file back", e))?;
        if !block.is_empty() {
            self.out
                .write(&format!("{block}\n"))
                .map_err(|e| Error::io("writing the output", e))?;
        }
        Ok(())
    }
}

/// The stream: stdin, or a command's merged output.
enum Source<'a> {
    Stdin(Receiver<io::Result<Vec<u8>>>),
    Command(&'a mut CommandRun),
}

/// What waiting on the stream found.
enum Event {
    Data(Vec<u8>),
    End,
    Nothing,
}

impl Source<'_> {
    /// The next chunk, waiting at most `timeout` (`None`: as long as it
    /// takes). Python's `select()` and `os.read()`.
    fn next(&mut self, timeout: Option<Duration>) -> io::Result<Event> {
        let got = match (self, timeout) {
            (Source::Stdin(rx), None) => rx.recv().map_err(|_| RecvTimeoutError::Disconnected),
            (Source::Stdin(rx), Some(t)) => rx.recv_timeout(t),
            (Source::Command(run), t) => run.recv(t.unwrap_or(POLL)),
        };
        match got {
            Ok(Ok(chunk)) if chunk.is_empty() => Ok(Event::End),
            Ok(Ok(chunk)) => Ok(Event::Data(chunk)),
            Ok(Err(e)) => Err(e),
            Err(RecvTimeoutError::Timeout) => Ok(Event::Nothing),
            Err(RecvTimeoutError::Disconnected) => Ok(Event::End),
        }
    }
}

fn secs(s: f64) -> Duration {
    Duration::try_from_secs_f64(s.max(0.0)).unwrap_or(Duration::MAX)
}

/// Runs follow mode: reads the stream (stdin, or `run`'s output) into the
/// spool at `spool` (through `file`), writes a block per window to stdout,
/// and returns the exit status (the command's in command mode).
///
/// # Errors
///
/// If reading, writing the spool, or writing the output fails.
pub fn follow(
    settings: &Settings,
    vocab: &Vocab,
    file: &mut std::fs::File,
    spool: &Path,
    run_path: Option<&str>,
    run: Option<&mut CommandRun>,
) -> Result<u8> {
    let mut pass = Pass::new(file, vocab, settings.width);
    pass.segmenter().every = settings.every;
    let mut state = State {
        settings,
        vocab,
        spool,
        run_path,
        out: Counting {
            out: io::BufWriter::new(io::stdout().lock()),
            nbytes: 0,
        },
        k: 0,
        last_flush: Instant::now(),
        due: None,
        error: None,
    };
    let is_command = run.is_some();
    let mut source = match run {
        Some(r) => Source::Command(r),
        None => Source::Stdin(read_in_background(io::stdin())),
    };
    let interval = secs(settings.interval);
    let mut quiet_since: Option<Instant> = None;
    loop {
        let now = Instant::now();
        let mut deadlines: Vec<Instant> = state.due.into_iter().collect();
        if settings.interval > 0.0 && pass.segmenter().window_len() > 0 {
            deadlines.push(state.last_flush + interval);
        }
        let mut timeout = deadlines
            .iter()
            .min()
            .map(|d| d.saturating_duration_since(now));
        if is_command {
            timeout = Some(timeout.map_or(POLL, |t| t.min(POLL)));
        }
        let event = source
            .next(timeout)
            .map_err(|e| Error::io("reading the input", e))?;
        if let (Event::Nothing, Source::Command(r)) = (&event, &mut source) {
            if r.exited()
                .map_err(|e| Error::io("waiting for the command", e))?
            {
                // The command is gone; whatever still holds the pipe is not
                // its output.
                if quiet_since.get_or_insert_with(Instant::now).elapsed() >= EXIT_GRACE {
                    break;
                }
            }
        }
        match event {
            Event::End => break,
            Event::Data(chunk) => {
                quiet_since = None;
                // The pass cuts and flushes on its own when the window cap
                // is reached.
                pass.feed_with(&chunk, &mut |seg| state.flush(seg, false))
                    .map_err(|e| Error::io("writing the run file", e))?;
                if let Some(e) = state.error.take() {
                    return Err(e);
                }
                let worst = pass.segmenter().worst_sev;
                if state.due.is_none()
                    && settings
                        .flush_sev
                        .is_some_and(|f| worst.is_some_and(|w| w >= f))
                {
                    let at = Instant::now() + secs(settings.settle);
                    state.due = Some(at.max(state.last_flush + secs(settings.min_interval)));
                }
            }
            Event::Nothing => {}
        }
        // A scheduled --flush-on flush that is due, or the --interval.
        let now = Instant::now();
        let due = state.due.is_some_and(|d| now >= d);
        let timed = settings.interval > 0.0
            && pass.segmenter().window_len() > 0
            && now >= state.last_flush + interval;
        if due || timed {
            state.flush(pass.segmenter(), false);
        }
        if let Some(e) = state.error.take() {
            return Err(e);
        }
    }
    pass.finish_lines(&mut |seg| state.flush(seg, false));
    state.flush(pass.segmenter(), true);
    if let Some(e) = state.error.take() {
        return Err(e);
    }
    let exit_code = match &mut source {
        Source::Command(r) => Some(
            r.wait()
                .map_err(|e| Error::io("waiting for the command", e))?,
        ),
        Source::Stdin(_) => None,
    };
    let total_bytes = pass.total_bytes();
    let seg = pass.segmenter();
    let mut end = format!(
        "# pare: end of input · {} lines, {} B (~{} tok) · {} templates · ",
        fmt_int(seg.nlines() as i64),
        fmt_int(i64::try_from(total_bytes).unwrap_or(i64::MAX)),
        fmt_int(i64::try_from(settings.budget.tokens_in(total_bytes)).unwrap_or(i64::MAX)),
        seg.ntemplates()
    );
    match run_path {
        Some(p) => end.push_str(&format!("raw: {p}")),
        None => end.push_str("raw: (not saved)"),
    }
    if let Some(code) = exit_code {
        end.push_str(&format!(" · exit {code}"));
    }
    end.push('\n');
    state
        .out
        .write(&end)
        .map_err(|e| Error::io("writing the output", e))?;
    if let (Source::Command(r), Some(_)) = (&source, run_path) {
        r.write_meta(
            spool,
            seg.nlines(),
            total_bytes,
            Some((
                state.out.nbytes,
                settings.budget.tokens,
                settings.budget.chars_per_token,
            )),
        );
    }
    Ok(exit_code.unwrap_or(0))
}
