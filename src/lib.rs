//! pare: summarize noisy command output (test runs, builds, logs) to a
//! token budget, for AI agents and humans.
//!
//! This crate is the Rust port of `proto/pare.py`, which stays the
//! reference until the two produce identical output (`bench/parity.py`).
//! The port is staged (ROADMAP.md, stage 5); what is ported so far:
//!
//! - reading input in chunks, splitting it into lines, saving it to a
//!   content-hashed run file ([`pass`], [`store`], [`runfile`]);
//! - cleaning a line as the Python version does ([`text`]);
//! - passing input that fits the budget through unchanged ([`summary`]).
//!
//! Anything else exits with status 70 and says it is not ported yet.
//!
//! `docs/ARCHITECTURE.md` describes the layout and data flow, and
//! `docs/RUST.md` explains the Rust-specific choices for a C++ reader;
//! comments marked `Rust note:` in the code point at those choices where
//! they are made.

// Rust note: `pub mod x;` declares a module and tells the compiler its
// source is in src/x.rs. There are no headers: a module's public items
// (`pub`) are its interface, and everything else is private to it. The
// order of declarations does not matter.
pub mod budget;
pub mod cli;
pub mod drain;
pub mod dump;
pub mod error;
pub mod mask;
pub mod pass;
pub mod pyclass;
pub mod pyre;
pub mod pyunicode;
pub mod runfile;
pub mod score;
pub mod select;
pub mod store;
pub mod summary;
pub mod text;
pub mod vocab;

/// The version, from Cargo.toml (`pare --version` prints it).
// Rust note: `env!` reads an environment variable at compile time; Cargo
// sets CARGO_PKG_VERSION from the manifest, so the version has one source.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
