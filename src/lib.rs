//! pare: summarize noisy command output (test runs, builds, logs) to a
//! token budget, for AI agents and humans.
//!
//! This crate is the Rust version of pare. It began as a port of
//! `reference/pare.py`, the Python prototype, which is kept frozen as the
//! reference it is tested against byte for byte (`bench/parity.py`;
//! ROADMAP.md, stage 5, lists the port's stages). Rules added since the
//! port (structured lines, the tail verdict rule, `--budget auto` as the
//! default) are switched off by `PARE_REFERENCE_RULES=1` for that check.
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
pub mod command;
pub mod drain;
pub mod dump;
pub mod error;
pub mod follow;
pub mod mask;
pub mod pass;
pub mod pyclass;
pub mod pyjson;
pub mod pyre;
pub mod pyunicode;
pub mod runfile;
pub mod runs;
pub mod score;
pub mod select;
pub mod store;
pub mod structured;
pub mod summary;
pub mod text;
pub mod vocab;

/// The version, from Cargo.toml (`pare --version` prints it).
// Rust note: `env!` reads an environment variable at compile time; Cargo
// sets CARGO_PKG_VERSION from the manifest, so the version has one source.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
