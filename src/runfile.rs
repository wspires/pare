//! The run directory and the spool file that becomes the run file.
//!
//! pare saves its whole input so that nothing a summary omits is lost. The
//! input is written to a spool file as it is read; once the content hash is
//! known, the spool is renamed to `<run dir>/<first 12 hex digits of its
//! SHA-256>.log`. Under `--no-tee` the spool is a temporary file, removed
//! when pare exits. The Python version does the same in `main()`.
//!
//! Not ported yet: pruning old run files, and the `.json` record command
//! mode writes beside the run file.

use std::env;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

use crate::error::{Error, Result};

/// The run directory: `$PARE_RUN_DIR`, else `$XDG_CACHE_HOME/pare/runs`,
/// else `~/.cache/pare/runs`.
#[must_use]
pub fn default_run_dir() -> PathBuf {
    // Rust note: `env::var_os` returns `Option<OsString>`: `None` when the
    // variable is unset. Absence is a value to handle, not a null pointer
    // to forget to check. `filter` then treats an empty value as unset, as
    // Python's `os.environ.get(...) or ...` does.
    if let Some(dir) = env::var_os("PARE_RUN_DIR").filter(|v| !v.is_empty()) {
        return PathBuf::from(dir);
    }
    let cache = env::var_os("XDG_CACHE_HOME")
        .filter(|v| !v.is_empty())
        .map_or_else(|| home_dir().join(".cache"), PathBuf::from);
    cache.join("pare").join("runs")
}

/// The user's home directory, as Python's `os.path.expanduser("~")` finds it.
fn home_dir() -> PathBuf {
    let var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    env::var_os(var).map_or_else(|| PathBuf::from("."), PathBuf::from)
}

/// A file the input is written to, removed when dropped unless it was kept
/// with [`Spool::persist`].
#[derive(Debug)]
pub struct Spool {
    path: PathBuf,
    // Rust note: `Option` so that `Drop` can close the file (by taking it
    // out and dropping it) before removing the path; Windows cannot remove
    // a file that is still open.
    file: Option<File>,
    keep: bool,
}

impl Spool {
    /// A spool at `path`, created or truncated. Its directory must exist.
    ///
    /// # Errors
    ///
    /// If the file cannot be created.
    pub fn create(path: PathBuf) -> Result<Self> {
        let file = File::create(&path)
            .map_err(|e| Error::io(format!("cannot create {}", path.display()), e))?;
        Ok(Spool {
            path,
            file: Some(file),
            keep: false,
        })
    }

    /// A new temporary spool in the system's temporary directory, for
    /// `--no-tee`.
    ///
    /// # Errors
    ///
    /// If no file can be created there.
    pub fn temporary() -> Result<Self> {
        // Rust note: a process-wide counter without a lock. Atomics give
        // the same guarantees as `std::atomic` in C++; `Relaxed` is enough
        // because only uniqueness matters, not ordering with other memory.
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let dir = env::temp_dir();
        loop {
            let n = NEXT.fetch_add(1, Ordering::Relaxed);
            let path = dir.join(format!("pare-{}-{n}.spool", std::process::id()));
            // `create_new` fails if the file exists, so a stale file with
            // the same name is never overwritten or shared.
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(file) => {
                    return Ok(Spool {
                        path,
                        file: Some(file),
                        keep: false,
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(e) => return Err(Error::io(format!("cannot create {}", path.display()), e)),
            }
        }
    }

    /// Where the spool is.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The open file, for writing the input into.
    ///
    /// # Panics
    ///
    /// After [`Spool::persist`], which consumes the spool, so it cannot be
    /// called then; the `Option` is only ever empty inside `Drop`.
    pub fn file(&mut self) -> &mut File {
        self.file.as_mut().expect("the spool is open until dropped")
    }

    /// Closes the spool and renames it to `to`, which it then is: the run
    /// file. Returns the new path.
    ///
    /// # Errors
    ///
    /// If the rename fails; the spool is then removed as usual.
    // Rust note: `self` by value, not `&mut self`: persisting consumes the
    // spool. Afterwards the caller has no `Spool` left to write to or to
    // persist twice; the compiler rejects any later use of the moved
    // value. A C++ moved-from object would still be there to misuse.
    pub fn persist(mut self, to: &Path) -> Result<PathBuf> {
        drop(self.file.take()); // close before renaming
        fs::rename(&self.path, to).map_err(|e| {
            Error::io(
                format!("cannot rename {} to {}", self.path.display(), to.display()),
                e,
            )
        })?;
        self.keep = true;
        Ok(to.to_path_buf())
    }
}

// Rust note: `Drop` is the destructor. It runs when the value goes out of
// scope, on every path out of the function: an early `return`, a `?` that
// passes an error up, or a panic unwinding. It is RAII as in C++, with the
// difference that a moved-from value is not dropped again, so there is no
// "valid but unspecified" state to guard against.
impl Drop for Spool {
    fn drop(&mut self) {
        drop(self.file.take());
        if !self.keep {
            // Nothing useful can be done about a failure here.
            let _ = fs::remove_file(&self.path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn temporary_spool_is_removed_on_drop() {
        let mut spool = Spool::temporary().unwrap();
        spool.file().write_all(b"x").unwrap();
        let path = spool.path().to_path_buf();
        assert!(path.exists());
        drop(spool);
        assert!(!path.exists());
    }

    #[test]
    fn persisted_spool_stays() {
        let mut spool = Spool::temporary().unwrap();
        spool.file().write_all(b"kept").unwrap();
        let to = spool.path().with_extension("log");
        let kept = spool.persist(&to).unwrap();
        assert_eq!(fs::read(&kept).unwrap(), b"kept");
        fs::remove_file(kept).unwrap();
    }
}
