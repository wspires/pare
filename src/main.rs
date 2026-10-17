//! The `pare` binary. Everything lives in the library crate (`src/lib.rs`);
//! this file only hands the process arguments to it and turns its result
//! into an exit status.

// Rust note: a package can hold a library crate (src/lib.rs) and a binary
// crate (src/main.rs) side by side. The binary uses the library like any
// other dependency, by its name (`pare::...`), so all the logic is testable
// and documented as an API, and main stays a few lines. In C++ terms: a
// static library plus a tiny executable that links it, with no build
// script to write.

use std::process::ExitCode;

fn main() -> ExitCode {
    // Rust note: `ExitCode` is the portable way to return a status from
    // main. `std::process::exit` would also work, but it skips destructors
    // (`Drop`), and the run-file cleanup relies on those.
    pare::cli::run(std::env::args_os().collect())
}
