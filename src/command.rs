//! Command mode, `pare [options] -- CMD [ARGS...]`: pare runs the command
//! itself, so its exit code and its command line survive (a pipe keeps
//! neither). `CommandRun` and `CommandOutput` in `reference/pare.py`.
//!
//! stdout and stderr are merged into one pipe, in the order the command
//! wrote them; stdin is inherited. While the command runs, pare ignores
//! SIGINT: a Ctrl-C from the terminal reaches the command too (same
//! process group), the command exits, and pare still summarises what it
//! printed and exits with its status. SIGTERM and SIGHUP (a tool's
//! timeout, a closed terminal) may be aimed at pare alone, so pare passes
//! them on to the command and keeps reading.

use std::ffi::OsString;
use std::io::{self, Read};
use std::path::Path;
use std::process::{Child, Command};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use serde_json::{Map, Value};

use crate::pass::CHUNK;
use crate::pyjson::dumps;
use crate::runs::utc_stamp;

/// Once the command has exited, stop reading after this long without
/// output, even if the pipe is still open: a process it left behind (a
/// daemon started with `&`) can hold the pipe open indefinitely.
const EXIT_GRACE: Duration = Duration::from_secs(1);
/// How often the reader checks whether the command has exited.
const POLL: Duration = Duration::from_millis(200);

/// A running command.
pub struct CommandRun {
    command: Vec<OsString>,
    child: Child,
    /// The merged output, read on a thread and handed over in chunks.
    chunks: Receiver<io::Result<Vec<u8>>>,
    started: SystemTime,
    cwd: String,
    exit_code: u8,
    #[cfg(unix)]
    signals: Option<signal_hook::iterator::Handle>,
}

/// Why the command did not start: the message and pare's exit status, as
/// a shell reports them (127 not found, 126 not executable).
pub struct StartError {
    /// What to print on stderr.
    pub message: String,
    /// The exit status.
    pub status: u8,
}

impl CommandRun {
    /// Starts `command`.
    ///
    /// # Errors
    ///
    /// If it cannot be started.
    pub fn start(command: &[OsString]) -> Result<CommandRun, StartError> {
        let name = command[0].to_string_lossy().into_owned();
        let fail = |e: io::Error| match e.kind() {
            io::ErrorKind::NotFound => StartError {
                message: format!("pare: {name}: command not found"),
                status: 127,
            },
            io::ErrorKind::PermissionDenied => StartError {
                message: format!("pare: {name}: permission denied"),
                status: 126,
            },
            _ => StartError {
                message: format!("pare: {name}: {e}"),
                status: 126,
            },
        };
        // Rust note: `io::pipe()` makes an anonymous pipe; the command gets
        // the write end twice, as stdout and as stderr, which merges them
        // in the order written (`2>&1`).
        let (reader, writer) = io::pipe().map_err(fail)?;
        let mut cmd = Command::new(&command[0]);
        cmd.args(&command[1..])
            .stdout(writer.try_clone().map_err(fail)?)
            .stderr(writer);
        let started = SystemTime::now();
        let child = cmd.spawn().map_err(fail)?;
        // The `Command` holds the parent's copies of the write end; until
        // they are closed, the pipe never reports end of file.
        drop(cmd);
        let chunks = read_in_background(reader);
        let cwd = std::env::current_dir()
            .map_or_else(|_| String::new(), |d| d.to_string_lossy().into_owned());
        let mut run = CommandRun {
            command: command.to_vec(),
            child,
            chunks,
            started,
            cwd,
            exit_code: 0,
            #[cfg(unix)]
            signals: None,
        };
        run.handle_signals();
        Ok(run)
    }

    /// After the spawn, so the command does not inherit the handling.
    #[cfg(unix)]
    fn handle_signals(&mut self) {
        use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};
        let Ok(mut signals) = signal_hook::iterator::Signals::new([SIGINT, SIGTERM, SIGHUP]) else {
            return;
        };
        self.signals = Some(signals.handle());
        let pid = nix::unistd::Pid::from_raw(i32::try_from(self.child.id()).unwrap_or(0));
        // Rust note: signal-hook runs the real handler (which may only do
        // async-signal-safe things) for us and delivers the signals to this
        // iterator on an ordinary thread, where any code may run: here,
        // passing SIGTERM and SIGHUP on. Registering SIGINT and doing
        // nothing with it is how it is ignored.
        thread::spawn(move || {
            for sig in signals.forever() {
                if sig != SIGINT {
                    if let Ok(s) = nix::sys::signal::Signal::try_from(sig) {
                        let _ = nix::sys::signal::kill(pid, s);
                    }
                }
            }
        });
    }

    #[cfg(not(unix))]
    fn handle_signals(&mut self) {}

    /// The next chunk of output, waiting at most `timeout`: `Ok(chunk)`, an
    /// empty chunk at end of file, or `Err(Timeout)`.
    ///
    /// # Errors
    ///
    /// `Timeout` if nothing arrived in time; `Disconnected` after the end.
    pub fn recv(&self, timeout: Duration) -> Result<io::Result<Vec<u8>>, RecvTimeoutError> {
        self.chunks.recv_timeout(timeout)
    }

    /// Whether the command has exited.
    ///
    /// # Errors
    ///
    /// If its status cannot be read.
    pub fn exited(&mut self) -> io::Result<bool> {
        Ok(self.child.try_wait()?.is_some())
    }

    /// The merged output as a reader for the pass.
    pub fn output(&mut self) -> CommandOutput<'_> {
        CommandOutput {
            run: self,
            pending: Vec::new(),
            pos: 0,
            done: false,
        }
    }

    /// Waits for the command and returns its exit status; one killed by a
    /// signal exits 128 + its number, as a shell reports it.
    ///
    /// # Errors
    ///
    /// If waiting fails.
    pub fn wait(&mut self) -> io::Result<u8> {
        let status = self.child.wait()?;
        #[cfg(unix)]
        if let Some(handle) = self.signals.take() {
            handle.close();
        }
        let code = status.code().unwrap_or_else(|| killed_status(status));
        // Rust note: a process status is 0 to 255 on Unix; `as u8` keeps the
        // low byte, as the shell does for a wider value on Windows.
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        let code = code as u8;
        self.exit_code = code;
        Ok(code)
    }

    /// `write_meta()`: `<run file>.json` next to the run file: what produced
    /// it, and how much of it pare printed (`out` is the bytes printed, the
    /// budget and the token rate). Read by `--stats` and
    /// `bench/collect.py`. Errors are ignored: the summary matters more.
    pub fn write_meta(
        &self,
        run_path: &Path,
        lines: usize,
        nbytes: u64,
        out: Option<(u64, i64, f64)>,
    ) {
        let mut meta = Map::new();
        let command: Vec<Value> = self
            .command
            .iter()
            .map(|a| Value::String(a.to_string_lossy().into_owned()))
            .collect();
        meta.insert("command".into(), Value::Array(command));
        meta.insert("cwd".into(), Value::String(self.cwd.clone()));
        meta.insert("exit".into(), Value::from(self.exit_code));
        meta.insert(
            "started".into(),
            Value::String(utc_stamp(epoch(self.started))),
        );
        // Python's round(seconds, 2).
        let seconds = epoch(SystemTime::now()) - epoch(self.started);
        let seconds: f64 = format!("{seconds:.2}").parse().unwrap_or(0.0);
        meta.insert("seconds".into(), Value::from(seconds));
        meta.insert("lines".into(), Value::from(lines));
        meta.insert("bytes".into(), Value::from(nbytes));
        meta.insert("pare".into(), Value::String(crate::VERSION.to_owned()));
        if let Some((out_bytes, budget, cpt)) = out {
            meta.insert("out_bytes".into(), Value::from(out_bytes));
            meta.insert("budget".into(), Value::from(budget));
            meta.insert("chars_per_token".into(), Value::from(cpt));
        }
        let _ = std::fs::write(
            run_path.with_extension("json"),
            dumps(&Value::Object(meta)) + "\n",
        );
    }
}

/// Reads `reader` on a thread of its own and hands over what it reads, in
/// chunks of up to [`CHUNK`] bytes; an empty chunk means end of file.
// Rust note: a blocking read on its own thread, handing chunks over a
// channel, stands in for Python's `select()` with a timeout: the main
// thread waits on the channel with `recv_timeout` and can do other things
// (check a deadline, look at the command) in between. If a process the
// command left behind keeps the pipe open, the thread just stays blocked
// until pare exits. The channel is bounded, so a fast input waits for the
// pass instead of piling up in memory.
pub fn read_in_background(reader: impl Read + Send + 'static) -> Receiver<io::Result<Vec<u8>>> {
    let (tx, rx) = mpsc::sync_channel(4);
    thread::spawn(move || {
        let mut reader = reader;
        let mut buf = vec![0u8; CHUNK];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => {
                    let _ = tx.send(Ok(Vec::new()));
                    return;
                }
                Ok(n) => {
                    if tx.send(Ok(buf[..n].to_vec())).is_err() {
                        return;
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => {
                    let _ = tx.send(Err(e));
                    return;
                }
            }
        }
    });
    rx
}

/// The status of a process that has no exit code: on Unix, one killed by
/// signal n, which a shell reports as 128 + n.
// Rust note: `#[cfg(...)]` on whole functions picks one definition per
// platform at compile time, like `#ifdef`, but each version is still
// parsed and has to be valid Rust.
#[cfg(unix)]
fn killed_status(status: std::process::ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    128 + status.signal().unwrap_or(0)
}

#[cfg(not(unix))]
fn killed_status(_status: std::process::ExitStatus) -> i32 {
    1
}

fn epoch(t: SystemTime) -> f64 {
    t.duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// The command's merged output as a stream for the pass: what is
/// available, and end of file at the pipe's end or once the command has
/// exited and the pipe has been quiet for a second (`EXIT_GRACE`).
pub struct CommandOutput<'a> {
    run: &'a mut CommandRun,
    pending: Vec<u8>,
    pos: usize,
    done: bool,
}

impl Read for CommandOutput<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.pos < self.pending.len() {
            let n = buf.len().min(self.pending.len() - self.pos);
            buf[..n].copy_from_slice(&self.pending[self.pos..self.pos + n]);
            self.pos += n;
            return Ok(n);
        }
        if self.done {
            return Ok(0);
        }
        let mut quiet_since: Option<Instant> = None;
        loop {
            match self.run.chunks.recv_timeout(POLL) {
                Ok(Ok(chunk)) if chunk.is_empty() => {
                    self.done = true;
                    return Ok(0);
                }
                Ok(Ok(chunk)) => {
                    self.pending = chunk;
                    self.pos = 0;
                    return self.read(buf);
                }
                Ok(Err(e)) => return Err(e),
                Err(RecvTimeoutError::Disconnected) => {
                    self.done = true;
                    return Ok(0);
                }
                Err(RecvTimeoutError::Timeout) => {
                    if self.run.child.try_wait()?.is_some() {
                        let since = *quiet_since.get_or_insert_with(Instant::now);
                        if since.elapsed() >= EXIT_GRACE {
                            self.done = true;
                            return Ok(0);
                        }
                    }
                }
            }
        }
    }
}
