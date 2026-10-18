//! The command line: the options of `main()` in `proto/pare.py`, and the
//! dispatch to what the port supports so far.
//!
//! Every Python option is accepted with the same name, default and meaning,
//! so scripts and the agent hook work unchanged; options whose behaviour is
//! not ported yet stop with [`Error::NotPorted`] (status 70).

use std::ffi::OsString;
use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

use crate::budget::{Budget, DEFAULT_CHARS_PER_TOKEN};
use crate::dump;
use crate::error::{Error, Result};
use crate::pass;
use crate::runfile::{Spool, default_run_dir};
use crate::score;
use crate::summary;
use crate::vocab::{self, Vocab};

/// Summarize noisy command output (tests, builds, logs) to a token budget.
///
/// noisy-command 2>&1 | pare [--budget 2000]
/// pare [--budget 2000] -- noisy-command args   (runs it; keeps its exit code)
/// pare --only T3,T7 < run-file                 (raw lines of those templates)
// Rust note: `#[derive(Parser)]` runs a procedural macro: Rust code that
// runs at compile time, reads this struct definition and generates the
// argument parser for it. The doc comments become the help text. It is
// closer to code generation than to C++ templates or the preprocessor, and
// what it generates is ordinary, type-checked Rust.
#[derive(Parser, Debug)]
#[command(name = "pare", version, verbatim_doc_comment, infer_long_args = true)]
// `infer_long_args`: accept unambiguous prefixes (--bud 600), as Python's
// argparse does by default.
#[allow(clippy::struct_excessive_bools)] // one bool per flag, as the CLI has
pub struct Args {
    /// Output budget in tokens (bytes/2.8 by default)
    #[arg(long, default_value_t = 2000, allow_negative_numbers = true)]
    pub budget: i64,
    /// Lines to keep from the start
    #[arg(long, default_value_t = 5)]
    pub head: i64,
    /// Lines to keep from the end
    #[arg(long, default_value_t = 10)]
    pub tail: i64,
    /// Clip lines longer than this
    #[arg(long, default_value_t = 200)]
    pub width: i64,
    /// Bytes per token for the budget and the token estimate (2.8, measured
    /// on common tokenizers; 4 is the prose rule of thumb)
    #[arg(long, value_name = "N", default_value_t = DEFAULT_CHARS_PER_TOKEN)]
    pub chars_per_token: f64,
    /// List at most N templates (highest salience first); 0 = all that fit
    #[arg(long, value_name = "N", default_value_t = 0)]
    pub max_templates: i64,
    /// Print raw lines of these templates only
    #[arg(long, value_name = "T<n>[,T<m>]")]
    pub only: Option<String>,
    /// Print raw lines except these templates
    #[arg(long = "except", value_name = "T<n>[,T<m>]")]
    pub exclude: Option<String>,
    /// Print raw lines from this line on: L3001, 3001, or -200 for the last 200
    #[arg(long, value_name = "L<n>", allow_hyphen_values = true)]
    pub since: Option<String>,
    /// Print raw lines up to and including this line (same forms)
    #[arg(long, value_name = "L<n>", allow_hyphen_values = true)]
    pub until: Option<String>,
    /// Print only records whose head carries a structural severity at or
    /// above LEVEL
    #[arg(long, value_name = "LEVEL", value_parser = ["debug", "info", "warn", "error", "fatal"])]
    pub min_severity: Option<String>,
    /// Directory for the raw run file (default $PARE_RUN_DIR or ~/.cache/pare/runs)
    #[arg(long)]
    pub run_dir: Option<PathBuf>,
    /// Explicit path for the raw run file
    #[arg(long)]
    pub run_file: Option<PathBuf>,
    /// Do not save the raw input
    #[arg(long)]
    pub no_tee: bool,
    /// Summarise a stream as it arrives: one block per window
    #[arg(long, short = 'f')]
    pub follow: bool,
    /// Follow: flush a window after N lines; also the window cap
    #[arg(long, value_name = "N", default_value_t = 20000)]
    pub every: i64,
    /// Follow: flush a non-empty window every S seconds (0 = off)
    #[arg(long, value_name = "S", default_value_t = 0.0)]
    pub interval: f64,
    /// Follow: flush soon after a line at this severity arrives
    #[arg(long, default_value = "error", value_parser = ["error", "fatal", "none"])]
    pub flush_on: String,
    /// Follow: wait S seconds after a --flush-on line so its record can finish
    #[arg(long, value_name = "S", default_value_t = 0.5)]
    pub settle: f64,
    /// Follow: at most one --flush-on flush per S seconds
    #[arg(long, value_name = "S", default_value_t = 2.0)]
    pub min_interval: f64,
    /// Follow: no heartbeat line for a window with nothing new
    #[arg(long)]
    pub quiet: bool,
    /// Vocabulary file that extends or replaces the built-in word lists
    #[arg(long, value_name = "FILE", env = "PARE_VOCAB")]
    pub vocab: Option<PathBuf>,
    /// Print the built-in vocabulary in file format and exit
    #[arg(long)]
    pub dump_vocab: bool,
    /// Remove old run files now
    #[arg(long)]
    pub prune: bool,
    /// With --prune: list what would be removed
    #[arg(long)]
    pub dry_run: bool,
    /// Print what command mode saved, from the run directory's records
    #[arg(long)]
    pub stats: bool,
}

impl Args {
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
    let args = match Args::try_parse_from(front) {
        Ok(args) => args,
        Err(e) => {
            // --help and --version arrive here too, with status 0.
            let _ = e.print();
            return ExitCode::from(u8::try_from(e.exit_code()).unwrap_or(2));
        }
    };
    match dispatch(&args, command.as_deref()) {
        Ok(()) => ExitCode::SUCCESS,
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
fn dispatch(args: &Args, command: Option<&[OsString]>) -> Result<()> {
    // The same checks, in the same order, as Python's main().
    if command.is_some_and(<[OsString]>::is_empty) {
        return Err(Error::Usage(
            "-- must be followed by a command to run".into(),
        ));
    }
    // NaN too: Python lets it through and then fails with a traceback.
    if args.chars_per_token <= 0.0 || args.chars_per_token.is_nan() {
        return Err(Error::Usage("--chars-per-token must be positive".into()));
    }
    let vocab = match args.vocab.as_deref().filter(|p| !p.as_os_str().is_empty()) {
        Some(path) => load_vocab(path)?,
        None => Vocab::builtin(),
    };
    if args.stats {
        return Err(Error::NotPorted("--stats"));
    }
    if args.dry_run && !args.prune {
        return Err(Error::Usage("--dry-run goes with --prune".into()));
    }
    if args.prune {
        return Err(Error::NotPorted("--prune"));
    }
    if args.dump_vocab {
        return write_out(
            &mut io::stdout().lock(),
            vocab::dump_vocab(vocab.entries()).as_bytes(),
        );
    }
    if command.is_some() && args.selecting() {
        return Err(Error::Usage(
            "-- runs a command to summarise; --only/--except/--since/--until/--min-severity read a run file".into(),
        ));
    }
    if command.is_some() {
        return Err(Error::NotPorted("command mode (pare -- CMD)"));
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
        return Err(Error::NotPorted("--follow"));
    }
    if args.selecting() {
        return Err(Error::NotPorted(
            "--only/--except/--since/--until/--min-severity",
        ));
    }
    batch(args, &vocab)
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

/// Batch mode: read all of stdin, save it, and write the summary.
fn batch(args: &Args, vocab: &Vocab) -> Result<()> {
    let budget = Budget {
        tokens: args.budget,
        chars_per_token: args.chars_per_token,
    };
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
        let tmp = dir.join(format!(".pare-{}.tmp", std::process::id()));
        (Spool::create(tmp)?, Some(dir))
    };

    let stdin = io::stdin().lock();
    let (mut store, digest, _) = pass::read_all(stdin, spool.file(), vocab, args.width)
        .map_err(|e| Error::io("reading the input", e))?;

    let stdout = io::stdout().lock();
    let mut out = io::BufWriter::new(stdout);
    if store.total_bytes == 0 {
        // The spool is dropped, and with it removed, on the way out.
        return write_out(&mut out, b"# pare: empty input\n");
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

    score::score_templates(&mut store, vocab, &spool_path)
        .map_err(|e| Error::io("reading the run file back", e))?;

    // The pass's results for bench/parity.py --dump, as Python writes them
    // before summarising (and so even when the summary is not ported yet).
    if let Some(path) = std::env::var_os("PARE_DUMP").filter(|p| !p.is_empty()) {
        dump::write_dump(&store, std::path::Path::new(&path))
            .map_err(|e| Error::io("writing $PARE_DUMP", e))?;
    }

    let wrote = summary::passthrough(&store, &spool_path, budget, &mut out)
        .map_err(|e| Error::io("writing the output", e))?;
    if !wrote {
        let opts = summary::Options {
            budget,
            head: args.head,
            tail: args.tail,
            width: args.width,
            max_templates: args.max_templates,
            run_path: tee.then(|| py_abspath(&spool_path)),
            in_bytes: store.total_bytes,
            note: None,
        };
        let text = summary::summarize(&store, vocab, &spool_path, &opts)
            .map_err(|e| Error::io("reading the run file back", e))?;
        out.write_all(text.as_bytes())
            .map_err(|e| Error::io("writing the output", e))
            .or_else(ignore_broken_pipe)?;
    }
    out.flush()
        .map_err(|e| Error::io("writing the output", e))
        .or_else(ignore_broken_pipe)
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
            (a.budget, a.head, a.tail, a.width, a.every),
            (2000, 5, 10, 200, 20000)
        );
        assert!((a.chars_per_token - 2.8).abs() < f64::EPSILON);
        assert_eq!(a.flush_on, "error");
    }

    #[test]
    fn prefixes_and_short_flags() {
        let a = parse(&["--bud", "600", "-f", "--since", "-200"]);
        assert_eq!(a.budget, 600);
        assert!(a.follow);
        assert_eq!(a.since.as_deref(), Some("-200"));
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
