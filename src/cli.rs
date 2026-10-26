//! The command line: the options of `main()` in `reference/pare.py`, its
//! checks in the same order, and the dispatch to batch mode, command mode,
//! follow mode, the selection flags, and the `prune`, `stats` and `vocab`
//! subcommands.
//!
//! Every Python option is accepted with the same name, default and meaning,
//! so scripts and the agent hook work unchanged.

use std::ffi::OsString;
use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use crate::budget::{Budget, DEFAULT_CHARS_PER_TOKEN};
use crate::command::CommandRun;
use crate::dump;
use crate::error::{Error, Result};
use crate::follow;
use crate::pass;
use crate::runfile::{Spool, default_run_dir};
use crate::runs;
use crate::score;
use crate::select;
use crate::summary;
use crate::vocab::{self, Vocab};

/// Summarize noisy command output (tests, builds, logs) to a token budget.
///
/// noisy-command 2>&1 | pare [--budget auto|N]
/// pare [--budget auto|N] -- noisy-command args (runs it; keeps its exit code)
/// pare --only T3,T7 < run-file                 (raw lines of those templates)
/// pare prune | stats | vocab                   (run files, savings, the word lists)
// Rust note: `#[derive(Parser)]` runs a procedural macro: Rust code that
// runs at compile time, reads this struct definition and generates the
// argument parser for it. The doc comments become the help text. It is
// closer to code generation than to C++ templates or the preprocessor, and
// what it generates is ordinary, type-checked Rust.
#[derive(Parser, Debug)]
#[command(name = "pare", version, verbatim_doc_comment, infer_long_args = true, after_long_help = ENV_HELP)]
// `infer_long_args`: accept unambiguous prefixes (--bud 600), as Python's
// argparse does by default.
#[allow(clippy::struct_excessive_bools)] // one bool per flag, as the CLI has
pub struct Args {
    /// A mode that does not summarise (`prune`, `stats`, `vocab`), if one
    /// was named.
    #[command(subcommand)]
    pub cmd: Option<Cmd>,
    /// Output budget in tokens (bytes/2.8 by default). `auto` or `auto:N`
    /// stops early when little is left worth showing, at most 2000 (or N);
    /// a number is a fixed budget the summary fills
    /// [default: $PARE_BUDGET, else auto]
    // `None` until `run` resolves it: the flag, else `$PARE_BUDGET` (an
    // agent harness can set the budget once for every call), else `auto`.
    // Rust note: `value_parser` names a function from the flag's text to
    // the field's type; clap reports its `Err` as an invalid value.
    #[arg(long, allow_negative_numbers = true, value_name = "N|auto[:N]", value_parser = parse_budget, help_heading = "Summary")]
    pub budget: Option<BudgetArg>,
    /// Lines to keep from the start
    #[arg(long, default_value_t = 5, value_parser = clap::value_parser!(i64).range(0..), help_heading = "Summary")]
    pub head: i64,
    /// Lines to keep from the end
    #[arg(long, default_value_t = 10, value_parser = clap::value_parser!(i64).range(0..), help_heading = "Summary")]
    pub tail: i64,
    /// Clip lines longer than this (at least 10)
    #[arg(long, default_value_t = 200, value_parser = clap::value_parser!(i64).range(10..), help_heading = "Summary")]
    pub width: i64,
    /// Bytes per token for the budget and the token estimate (2.8, measured
    /// on common tokenizers; 4 is the prose rule of thumb)
    #[arg(long = "bytes-per-token", alias = "chars-per-token", value_name = "N", default_value_t = DEFAULT_CHARS_PER_TOKEN, help_heading = "Summary")]
    pub chars_per_token: f64,
    /// pare's own marks in ASCII (-> x | ... <=) instead of → × · ┆ … ≤
    #[arg(long, help_heading = "Summary")]
    pub ascii: bool,
    /// List at most N templates (highest salience first); 0 = all that fit
    #[arg(long, value_name = "N", default_value_t = 0, value_parser = clap::value_parser!(i64).range(0..), help_heading = "Summary")]
    pub max_templates: i64,
    /// Print raw lines of these templates only
    #[arg(
        long,
        value_name = "T<n>[,T<m>]",
        help_heading = "Selecting from a run file"
    )]
    pub only: Option<String>,
    /// Print raw lines except these templates
    #[arg(
        long = "except",
        value_name = "T<n>[,T<m>]",
        help_heading = "Selecting from a run file"
    )]
    pub exclude: Option<String>,
    /// Print raw lines from this line on: L3001, 3001, or -200 for the last 200
    #[arg(
        long,
        value_name = "L<n>",
        allow_hyphen_values = true,
        help_heading = "Selecting from a run file"
    )]
    pub since: Option<String>,
    /// Print raw lines up to and including this line (same forms)
    #[arg(
        long,
        value_name = "L<n>",
        allow_hyphen_values = true,
        help_heading = "Selecting from a run file"
    )]
    pub until: Option<String>,
    /// Print only records whose head carries a structural severity at or
    /// above LEVEL
    #[arg(long, value_name = "LEVEL", value_parser = ["debug", "info", "warn", "error", "fatal"], help_heading = "Selecting from a run file")]
    pub min_severity: Option<String>,
    /// Directory for the raw run files (default $PARE_RUN_DIR or ~/.cache/pare/runs)
    #[arg(long, global = true, help_heading = "Run files")]
    pub run_dir: Option<PathBuf>,
    /// Explicit path for the raw run file
    #[arg(long, help_heading = "Run files")]
    pub run_file: Option<PathBuf>,
    /// Do not save the raw input
    #[arg(long, help_heading = "Run files")]
    pub no_tee: bool,
    /// Summarise a stream as it arrives: one block per window
    #[arg(long, short = 'f', help_heading = "Follow mode")]
    pub follow: bool,
    /// Flush a window after N lines; also the window cap
    #[arg(
        long,
        value_name = "N",
        default_value_t = 20000,
        help_heading = "Follow mode"
    )]
    pub every: i64,
    /// Flush a non-empty window every S seconds (0 = off)
    #[arg(
        long,
        value_name = "S",
        default_value_t = 30.0,
        help_heading = "Follow mode"
    )]
    pub interval: f64,
    /// Flush soon after a line at this severity arrives
    #[arg(long, default_value = "error", value_parser = ["error", "fatal", "none"], help_heading = "Follow mode")]
    pub flush_on: String,
    /// Wait S seconds after a --flush-on line so its record can finish
    #[arg(
        long,
        value_name = "S",
        default_value_t = 0.5,
        help_heading = "Follow mode"
    )]
    pub settle: f64,
    /// At most one --flush-on flush per S seconds
    #[arg(
        long,
        value_name = "S",
        default_value_t = 2.0,
        help_heading = "Follow mode"
    )]
    pub min_interval: f64,
    /// No heartbeat line for a window with nothing new
    #[arg(long, help_heading = "Follow mode")]
    pub quiet: bool,
    /// Vocabulary file that extends or replaces the built-in word lists
    #[arg(
        long,
        value_name = "FILE",
        env = "PARE_VOCAB",
        global = true,
        help_heading = "Vocabulary"
    )]
    pub vocab: Option<PathBuf>,
}

/// The modes that do not summarise: each reads the run directory or the
/// vocabulary and prints a report.
#[derive(Subcommand, Debug)]
pub enum Cmd {
    /// Remove old run files now (older than 14 days, then the oldest past
    /// 500 MB; automatic pruning does the same at most hourly)
    Prune {
        /// List what would be removed, removing nothing
        #[arg(long)]
        dry_run: bool,
    },
    /// Print what command mode saved, from the run directory's records
    Stats,
    /// Print the vocabulary in file format: the built-in one, or with
    /// --vocab applied
    Vocab,
}

impl Cmd {
    fn name(&self) -> &'static str {
        match self {
            Cmd::Prune { .. } => "prune",
            Cmd::Stats => "stats",
            Cmd::Vocab => "vocab",
        }
    }
}

/// The environment, listed after the long help (`--help`, not `-h`).
const ENV_HELP: &str = "\
Subcommands: pare prune [--dry-run], pare stats, pare vocab (`pare <command> --help`).

Environment:
  PARE_BUDGET         the budget when --budget is not given: N, auto or auto:N
  PARE_RUN_DIR        where run files go (default ~/.cache/pare/runs)
  PARE_RUN_KEEP_DAYS  prune run files older than this (default 14; 0 = no age limit)
  PARE_RUN_MAX_MB     ...then the oldest until the rest fit (default 500; 0 = no limit)
  PARE_RUN_PRUNE      off: never prune automatically
  PARE_VOCAB          a vocabulary file, as --vocab

Exit status: the command's in command mode; otherwise 0, 1 for an I/O
error, 2 for a usage error.
";

/// The ceiling of `auto`, the budget when neither `--budget` nor
/// `$PARE_BUDGET` gives one (and, under `$PARE_REFERENCE_RULES`, the fixed
/// budget the Python version defaults to).
pub const DEFAULT_BUDGET: i64 = 2000;

/// A `--budget` value: a number of tokens, or `auto` with a ceiling.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BudgetArg {
    /// The budget, or an automatic budget's ceiling, in tokens.
    pub tokens: i64,
    /// `auto` or `auto:N`.
    pub auto: bool,
}

/// Parses `2000`, `auto` or `auto:4000`.
///
/// # Errors
///
/// Anything else, with a message saying what is expected.
pub fn parse_budget(text: &str) -> std::result::Result<BudgetArg, String> {
    let text = text.trim();
    let number = |t: &str| t.trim().parse::<i64>().ok();
    let parsed = match text.strip_prefix("auto") {
        Some("") => Some(BudgetArg {
            tokens: DEFAULT_BUDGET,
            auto: true,
        }),
        Some(rest) => rest
            .strip_prefix(':')
            .and_then(number)
            .map(|tokens| BudgetArg { tokens, auto: true }),
        None => number(text).map(|tokens| BudgetArg {
            tokens,
            auto: false,
        }),
    };
    match parsed {
        Some(b) if b.tokens >= 1 => Ok(b),
        _ => Err("expected a whole number of tokens (at least 1), auto, or auto:N".to_owned()),
    }
}

impl Args {
    /// The output budget in tokens (an automatic budget's ceiling), once
    /// `run` has resolved it.
    #[must_use]
    pub fn budget(&self) -> i64 {
        self.budget.map_or(DEFAULT_BUDGET, |b| b.tokens)
    }

    /// The budget for the summary, once `run` has resolved it: `auto`
    /// unless a fixed number was given (fixed 2000 under
    /// `$PARE_REFERENCE_RULES`, as in Python).
    fn budget_for_summary(&self) -> Budget {
        Budget {
            tokens: self.budget(),
            chars_per_token: self.chars_per_token,
            auto: self.budget.map_or_else(crate::pass::rust_rules, |b| b.auto),
        }
    }

    /// Fills in the budget from `$PARE_BUDGET` if the flag did not give
    /// one. An empty variable counts as unset, as `$PARE_VOCAB` does.
    ///
    /// # Errors
    ///
    /// If the variable is not a whole number, `auto` or `auto:N`.
    // Rust note: clap's own `env = ...` would do this too, but it takes an
    // empty variable as an invalid value, and its message would not say the
    // value came from the environment.
    fn resolve_budget(&mut self) -> Result<()> {
        if self.budget.is_none() {
            if let Some(v) = std::env::var_os("PARE_BUDGET").filter(|v| !v.is_empty()) {
                let text = v.to_string_lossy();
                let b = parse_budget(&text).map_err(|_| {
                    Error::Usage(format!(
                        "$PARE_BUDGET must be a whole number of tokens, auto, or auto:N; got {text:?}"
                    ))
                })?;
                self.budget = Some(b);
            }
        }
        Ok(())
    }

    fn selecting(&self) -> bool {
        self.only.is_some()
            || self.exclude.is_some()
            || self.min_severity.is_some()
            || self.since.is_some()
            || self.until.is_some()
    }
}

/// Runs pare with the process arguments (program name first) and returns
/// its exit status.
#[must_use]
pub fn run(argv: Vec<OsString>) -> ExitCode {
    // Command mode: everything after the first `--` is the command, as in
    // Python, which splits argv there before parsing the rest.
    // Rust note: arguments are `OsString`, not `String`: on Unix they are
    // arbitrary bytes and need not be valid UTF-8. Converting to `String`
    // is a separate, fallible step, so a file name with odd bytes cannot
    // crash the parser.
    let (front, command) = match argv.iter().position(|a| a == "--") {
        Some(i) => (argv[..i].to_vec(), Some(argv[i + 1..].to_vec())),
        None => (argv, None),
    };
    let mut args = match Args::try_parse_from(front) {
        Ok(args) => args,
        Err(e) => {
            // --help and --version arrive here too, with status 0.
            let _ = e.print();
            return ExitCode::from(u8::try_from(e.exit_code()).unwrap_or(2));
        }
    };
    match args
        .resolve_budget()
        .and_then(|()| dispatch(&args, command.as_deref()))
    {
        Ok(status) => ExitCode::from(status),
        Err(e) => {
            eprintln!("{e}");
            ExitCode::from(e.exit_status())
        }
    }
}

// Rust note: `Option<&[OsString]>` borrows the command (a slice of the
// vector, like a `std::span`) instead of taking the `Vec` by value; this
// function only looks at it. Clippy's `needless_pass_by_value` flags the
// by-value version, the Rust counterpart of passing a `std::vector` by
// value where a `const&` would do.
/// Runs what the arguments ask for and returns the exit status: 0, or the
/// command's own in command mode.
fn dispatch(args: &Args, command: Option<&[OsString]>) -> Result<u8> {
    // The same checks, in the same order, as Python's main().
    if command.is_some_and(<[OsString]>::is_empty) {
        return Err(Error::Usage(
            "-- must be followed by a command to run".into(),
        ));
    }
    // NaN too: Python lets it through and then fails with a traceback.
    if args.chars_per_token <= 0.0 || args.chars_per_token.is_nan() {
        return Err(Error::Usage("--bytes-per-token must be positive".into()));
    }
    crate::text::set_ascii(args.ascii);
    if args.no_tee && args.run_file.is_some() {
        return Err(Error::Usage(
            "--no-tee saves nothing; --run-file names the file to save the input in".into(),
        ));
    }
    let vocab = match args.vocab.as_deref().filter(|p| !p.as_os_str().is_empty()) {
        Some(path) => load_vocab(path)?,
        None => Vocab::builtin(),
    };
    let run_dir = || args.run_dir.clone().unwrap_or_else(default_run_dir);
    if let Some(cmd) = &args.cmd {
        if command.is_some() || args.follow || args.selecting() {
            return Err(Error::Usage(format!(
                "pare {} neither reads input nor runs a command",
                cmd.name()
            )));
        }
        let mut out = io::stdout().lock();
        match cmd {
            Cmd::Prune { dry_run } => runs::prune_command(&run_dir(), *dry_run, &mut out)
                .map_err(|e| Error::io("writing the output", e))
                .or_else(ignore_broken_pipe)?,
            Cmd::Stats => runs::stats(&run_dir(), &mut out)
                .map_err(|e| Error::io("writing the output", e))
                .or_else(ignore_broken_pipe)?,
            Cmd::Vocab => write_out(&mut out, vocab::dump_vocab(vocab.entries()).as_bytes())?,
        }
        return Ok(0);
    }
    if command.is_some() && args.selecting() {
        return Err(Error::Usage(
            "-- runs a command to summarise; --only/--except/--since/--until/--min-severity read a run file".into(),
        ));
    }
    if args.follow {
        if args.selecting() {
            return Err(Error::Usage(
                "--follow summarises a stream; --only/--except/--since/--until/--min-severity read a run file".into(),
            ));
        }
        if args.every < 1 {
            return Err(Error::Usage("--every must be at least 1".into()));
        }
    }
    let mut run = match command {
        Some(command) => match CommandRun::start(command) {
            Ok(run) => Some(run),
            Err(e) => {
                // Could not start it; the reason goes to stderr, the status
                // is the shell's.
                eprintln!("{}", e.message);
                return Ok(e.status);
            }
        },
        None => None,
    };
    if args.follow {
        return follow_mode(args, &vocab, run.as_mut());
    }
    batch(args, &vocab, run.as_mut())
}

/// Follow mode: the spool is named up front and kept (or temporary under
/// `--no-tee`), and the stream is summarised window by window.
fn follow_mode(args: &Args, vocab: &Vocab, run: Option<&mut CommandRun>) -> Result<u8> {
    let tee = !args.no_tee;
    let mut spool = if !tee {
        Spool::temporary()?
    } else if let Some(file) = &args.run_file {
        if let Some(dir) = file.parent().filter(|d| !d.as_os_str().is_empty()) {
            create_dir(dir)?;
        }
        Spool::create(file.clone())?
    } else {
        let dir = args.run_dir.clone().unwrap_or_else(default_run_dir);
        create_dir(&dir)?;
        runs::maybe_prune(&dir);
        // Named by the time it started. Python uses local time; std has no
        // time zones, so this is UTC.
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64());
        let stamp: String = runs::utc_stamp(now)
            .chars()
            .filter(|c| c.is_ascii_digit() || *c == 'T')
            .map(|c| if c == 'T' { '-' } else { c })
            .collect();
        Spool::create(dir.join(format!("follow-{stamp}-{}.log", std::process::id())))?
    };
    if tee {
        spool.keep();
    }
    let path = spool.path().to_path_buf();
    let run_path = tee.then(|| py_abspath(&path));
    let settings = follow::Settings {
        budget: args.budget_for_summary(),
        head: args.head,
        tail: args.tail,
        width: args.width,
        max_templates: args.max_templates,
        every: usize::try_from(args.every).unwrap_or(usize::MAX),
        interval: args.interval,
        flush_sev: match args.flush_on.as_str() {
            "error" => Some(3),
            "fatal" => Some(4),
            _ => None,
        },
        settle: args.settle,
        min_interval: args.min_interval,
        quiet: args.quiet,
    };
    follow::follow(
        &settings,
        vocab,
        spool.file(),
        &path,
        run_path.as_deref(),
        run,
    )
    .or_else(|e| ignore_broken_pipe(e).map(|()| 0))
}

/// The vocabulary of `--vocab FILE`: the built-in one with the file
/// applied.
fn load_vocab(path: &std::path::Path) -> Result<Vocab> {
    let source = path.to_string_lossy();
    let text = std::fs::read_to_string(path).map_err(|e| {
        // Python prints the OS's message alone (`e.strerror`); Rust's adds
        // " (os error 2)", which is cut off.
        let msg = e.to_string();
        let msg = msg
            .find(" (os error ")
            .map_or(&*msg, |at| &msg[..at])
            .to_owned();
        Error::Usage(format!("cannot read vocab file {source}: {msg}"))
    })?;
    let entries =
        vocab::parse_vocab(&text, &vocab::default_entries(), &source).map_err(Error::Usage)?;
    Vocab::new(entries).map_err(|e| Error::Usage(format!("{source}: {e}")))
}

/// Batch mode: read all of the input (stdin, or the command's output),
/// save it, and write the summary. Returns the exit status: the command's
/// in command mode, else 0.
#[allow(clippy::too_many_lines)] // the batch path of Python's main(), kept whole
fn batch(args: &Args, vocab: &Vocab, mut run: Option<&mut CommandRun>) -> Result<u8> {
    let budget = args.budget_for_summary();
    let tee = !args.no_tee;
    // Where the input goes while it is read, and the run directory it will
    // be renamed into (None: --no-tee, or an explicit --run-file).
    let (mut spool, run_dir) = if !tee {
        (Spool::temporary()?, None)
    } else if let Some(file) = &args.run_file {
        if let Some(dir) = file.parent().filter(|d| !d.as_os_str().is_empty()) {
            create_dir(dir)?;
        }
        let mut tmp = file.clone().into_os_string();
        tmp.push(".tmp");
        (Spool::create(PathBuf::from(tmp))?, None)
    } else {
        let dir = args.run_dir.clone().unwrap_or_else(default_run_dir);
        create_dir(&dir)?;
        runs::maybe_prune(&dir);
        let tmp = dir.join(format!(".pare-{}.tmp", std::process::id()));
        (Spool::create(tmp)?, Some(dir))
    };

    let read = match run.as_deref_mut() {
        Some(r) => pass::read_all(r.output(), spool.file(), vocab, args.width),
        None => pass::read_all(io::stdin().lock(), spool.file(), vocab, args.width),
    };
    let (mut store, digest, _) = read.map_err(|e| Error::io("reading the input", e))?;
    let exit_code = match run.as_deref_mut() {
        Some(r) => r
            .wait()
            .map_err(|e| Error::io("waiting for the command", e))?,
        None => 0,
    };
    let note = run.as_ref().map(|_| format!("exit {exit_code}"));

    let stdout = io::stdout().lock();
    let mut out = io::BufWriter::new(stdout);
    if store.total_bytes == 0 {
        // The spool is dropped, and with it removed, on the way out.
        let msg = match &note {
            Some(note) => format!("# pare: no output {} {note}\n", crate::text::glyphs().dot),
            None => "# pare: empty input\n".to_owned(),
        };
        write_out(&mut out, msg.as_bytes())?;
        return Ok(exit_code);
    }

    // Rust note: `_temp` keeps a `--no-tee` spool alive until the end of
    // the function, when its `Drop` removes it. A plain `_` would not bind
    // it at all, and the value would be dropped (and the file removed) on
    // that very line.
    let (spool_path, _temp) = if tee {
        let target = match (&args.run_file, &run_dir) {
            (Some(file), _) => file.clone(),
            (None, Some(dir)) => dir.join(format!("{digest}.log")),
            (None, None) => unreachable!("a teed run has a run file or a run directory"),
        };
        (spool.persist(&target)?, None)
    } else {
        (spool.path().to_path_buf(), Some(spool))
    };
    let run_path = tee.then(|| py_abspath(&spool_path));
    if let (Some(r), true) = (run.as_deref(), tee) {
        r.write_meta(&spool_path, store.len(), store.total_bytes, None);
    }

    score::score_templates(&mut store, vocab, &spool_path)
        .map_err(|e| Error::io("reading the run file back", e))?;

    // The pass's results for bench/parity.py --dump, as Python writes them
    // before summarising.
    if let Some(path) = std::env::var_os("PARE_DUMP").filter(|p| !p.is_empty()) {
        dump::write_dump(&store, std::path::Path::new(&path))
            .map_err(|e| Error::io("writing $PARE_DUMP", e))?;
    }

    if args.selecting() {
        // Checked in Python's order, after the input is read and saved.
        let ntpl = store.templates.len();
        let sel = select::Selection {
            only: select::parse_tids(args.only.as_deref(), ntpl).map_err(Error::Usage)?,
            exclude: select::parse_tids(args.exclude.as_deref(), ntpl).map_err(Error::Usage)?,
            min_severity: args.min_severity.as_deref().map(severity_by_name),
            since: select::parse_lineno(args.since.as_deref(), store.len(), "--since")
                .map_err(Error::Usage)?,
            until: select::parse_lineno(args.until.as_deref(), store.len(), "--until")
                .map_err(Error::Usage)?,
        };
        let text = select::select(
            &store,
            &sel,
            budget,
            args.width,
            run_path.as_deref(),
            &spool_path,
        )
        .map_err(|e| match e {
            Ok(msg) => Error::Usage(msg),
            Err(e) => Error::io("reading the run file back", e),
        })?;
        write_out(&mut out, text.as_bytes())?;
        return Ok(exit_code);
    }
    // A whole run that fits is the output, byte for byte; only a failing
    // exit status is added.
    let verbatim_end = note
        .as_deref()
        .filter(|n| *n != "exit 0")
        .map(|n| format!("## {n}"));
    let wrote = summary::passthrough(
        &store,
        &spool_path,
        budget,
        verbatim_end.as_deref(),
        &mut out,
    )
    .map_err(|e| Error::io("writing the output", e))
    .or_else(|e| ignore_broken_pipe(e).map(|()| Some(0)))?;
    let out_bytes = match wrote {
        Some(n) => n,
        None => {
            let opts = summary::Options {
                budget,
                head: args.head,
                tail: args.tail,
                width: args.width,
                max_templates: args.max_templates,
                run_path,
                in_bytes: store.total_bytes,
                note,
                follow: None,
            };
            let text = summary::summarize(&store, vocab, &spool_path, &opts)
                .map_err(|e| Error::io("reading the run file back", e))?;
            out.write_all(text.as_bytes())
                .map_err(|e| Error::io("writing the output", e))
                .or_else(ignore_broken_pipe)?;
            text.len() as u64
        }
    };
    out.flush()
        .map_err(|e| Error::io("writing the output", e))
        .or_else(ignore_broken_pipe)?;
    if let (Some(r), true) = (run.as_deref(), tee) {
        // What the command printed against what pare printed, for `pare stats`.
        r.write_meta(
            &spool_path,
            store.len(),
            store.total_bytes,
            Some((out_bytes, args.budget(), args.chars_per_token)),
        );
    }
    Ok(exit_code)
}

/// `SEVERITY_BY_NAME`: a `--min-severity` level (clap has checked it).
fn severity_by_name(name: &str) -> u8 {
    match name {
        "debug" => 0,
        "info" => 1,
        "warn" => 2,
        "error" => 3,
        _ => 4,
    }
}

/// Python's `os.path.abspath()`: the path made absolute against the
/// current directory and normalised (`.` and `..` resolved by name, not
/// by following symlinks), as the run line shows it.
fn py_abspath(path: &std::path::Path) -> String {
    use std::path::Component;
    let joined = std::env::current_dir().map_or_else(|_| path.to_path_buf(), |cwd| cwd.join(path));
    let mut parts: Vec<std::ffi::OsString> = Vec::new();
    let mut root = String::new();
    // Rust note: `components()` already drops `.` and repeated separators;
    // `..` is left for the caller to resolve, as `normpath` does by name.
    for c in joined.components() {
        match c {
            Component::Prefix(p) => root.push_str(&p.as_os_str().to_string_lossy()),
            Component::RootDir => root.push(std::path::MAIN_SEPARATOR),
            Component::CurDir => {}
            Component::ParentDir => {
                parts.pop();
            }
            Component::Normal(n) => parts.push(n.to_os_string()),
        }
    }
    let rest: Vec<String> = parts
        .iter()
        .map(|p| p.to_string_lossy().into_owned())
        .collect();
    format!("{root}{}", rest.join(std::path::MAIN_SEPARATOR_STR))
}

fn create_dir(dir: &std::path::Path) -> Result<()> {
    std::fs::create_dir_all(dir)
        .map_err(|e| Error::io(format!("cannot create {}", dir.display()), e))
}

fn write_out(out: &mut impl Write, bytes: &[u8]) -> Result<()> {
    out.write_all(bytes)
        .and_then(|()| out.flush())
        .map_err(|e| Error::io("writing the output", e))
        .or_else(ignore_broken_pipe)
}

/// A reader that stopped reading (`pare ... | head`) is not a failure.
// Rust note: Rust ignores SIGPIPE, so a write to a closed pipe returns an
// `Err(BrokenPipe)` instead of killing the process; treating that one
// error as success is the usual way for a filter to exit quietly.
fn ignore_broken_pipe(e: Error) -> Result<()> {
    match &e {
        Error::Io { source, .. } if source.kind() == io::ErrorKind::BrokenPipe => Ok(()),
        _ => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Args {
        Args::try_parse_from(std::iter::once("pare").chain(args.iter().copied())).unwrap()
    }

    #[test]
    fn defaults_match_python() {
        let a = parse(&[]);
        assert_eq!(
            (a.budget(), a.head, a.tail, a.width, a.every),
            (2000, 5, 10, 200, 20000)
        );
        assert!((a.chars_per_token - 2.8).abs() < f64::EPSILON);
        assert_eq!(a.flush_on, "error");
    }

    #[test]
    fn prefixes_and_short_flags() {
        let a = parse(&["--bud", "600", "-f", "--since", "-200"]);
        assert_eq!(
            a.budget,
            Some(BudgetArg {
                tokens: 600,
                auto: false
            })
        );
        assert!(a.follow);
        assert_eq!(a.since.as_deref(), Some("-200"));
    }

    #[test]
    fn budget_values() {
        let b = |tokens, auto| Ok(BudgetArg { tokens, auto });
        assert_eq!(parse_budget("600"), b(600, false));
        assert_eq!(parse_budget("1"), b(1, false));
        assert_eq!(parse_budget("auto"), b(2000, true));
        assert_eq!(parse_budget(" auto:4000 "), b(4000, true));
        for bad in [
            "", "auto:", "auto4000", "autox", "lots", "1.5", "0", "-5", "auto:0",
        ] {
            assert!(parse_budget(bad).is_err(), "{bad:?}");
        }
        assert!(parse(&["--budget", "auto"]).budget_for_summary().auto);
        assert!(parse(&[]).budget_for_summary().auto); // the default
        assert!(!parse(&["--budget", "2000"]).budget_for_summary().auto);
    }

    #[test]
    fn command_after_double_dash() {
        let argv: Vec<OsString> = ["pare", "--budget", "9", "--", "make", "--only"]
            .map(OsString::from)
            .to_vec();
        let i = argv.iter().position(|a| a == "--").unwrap();
        assert_eq!(&argv[i + 1..], ["make", "--only"].map(OsString::from));
    }
}
