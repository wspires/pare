//! Errors and the exit statuses they map to.
//!
//! The Python version stops with `sys.exit("pare: ...")` (status 1) for a
//! usage error and lets an I/O error raise. Here every failure is a value of
//! [`Error`], returned up the call stack and turned into a message and a
//! status in one place ([`crate::cli::run`]).

use std::fmt;
use std::io;

/// Status for a mode or flag the port does not support yet (EX_SOFTWARE).
/// `bench/parity.py --skip-unported` counts these runs as skipped.
pub const EXIT_NOT_PORTED: u8 = 70;

/// Everything that can make pare stop early.
// Rust note: an enum whose variants carry data, like a `std::variant` of
// structs. A `match` on it must handle every variant or the code does not
// compile, so adding a variant shows every place that has to deal with it.
#[derive(Debug)]
pub enum Error {
    /// A combination of flags that makes no sense; the message says why.
    Usage(String),
    /// Reading the input, or writing the run file or the output, failed.
    Io {
        /// What pare was doing, for the message.
        context: String,
        /// The operating system's error.
        source: io::Error,
    },
    /// The Rust port does not do this yet; the Python version does.
    NotPorted(&'static str),
}

impl Error {
    /// The process exit status for this error.
    #[must_use]
    pub fn exit_status(&self) -> u8 {
        match self {
            Error::Usage(_) | Error::Io { .. } => 1,
            Error::NotPorted(_) => EXIT_NOT_PORTED,
        }
    }

    /// Wraps an I/O error with a description of what failed.
    // Rust note: `impl Into<String>` accepts a `&str` or a `String`; the
    // caller does not have to write `.to_string()`. It is a generic
    // parameter in disguise: the compiler makes one copy per argument type,
    // as a C++ template would.
    pub fn io(context: impl Into<String>, source: io::Error) -> Self {
        Error::Io {
            context: context.into(),
            source,
        }
    }
}

// Rust note: `Display` is the trait behind `{}` in `format!` and
// `println!`, roughly `operator<<` for an ostream. Implementing it is how a
// type says how it reads to a person.
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Usage(msg) => write!(f, "pare: {msg}"),
            Error::Io { context, source } => write!(f, "pare: {context}: {source}"),
            Error::NotPorted(what) => write!(
                f,
                "pare: not yet ported to Rust: {what} (proto/pare.py does it meanwhile)"
            ),
        }
    }
}

// Rust note: implementing `std::error::Error` makes this type usable
// wherever the standard library expects "some error", and `source()` lets
// a reader walk the chain of causes.
impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io { source, .. } => Some(source),
            Error::Usage(_) | Error::NotPorted(_) => None,
        }
    }
}

/// Shorthand for results whose error is [`Error`].
// Rust note: a type alias with one generic parameter left open, so a
// function signature reads `Result<Store>` instead of
// `Result<Store, Error>`. The standard library does the same in `io`.
pub type Result<T> = std::result::Result<T, Error>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statuses_and_messages() {
        let e = Error::NotPorted("--follow");
        assert_eq!(e.exit_status(), EXIT_NOT_PORTED);
        assert!(e.to_string().contains("not yet ported to Rust: --follow"));
        let e = Error::io("reading the input", io::Error::other("boom"));
        assert_eq!(e.exit_status(), 1);
        assert_eq!(e.to_string(), "pare: reading the input: boom");
        assert!(std::error::Error::source(&e).is_some());
    }
}
