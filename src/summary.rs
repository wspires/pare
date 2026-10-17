//! Producing the output: `summarize()` in `proto/pare.py`. Only its first
//! branch is ported so far: input that fits the budget is passed through
//! unchanged.

use std::io::{self, Write};
use std::path::Path;

use crate::budget::Budget;
use crate::store::Store;
use crate::text::clean_line;

/// Writes the input to `out` verbatim, one cleaned line per line, if it
/// fits the budget. Returns whether it did.
///
/// A whole run that fits is the output, byte for byte: no header, and no
/// run-file line (the run file holds nothing more). The cleaned text is
/// what the summary would show for each line anyway: no ANSI codes, no
/// carriage-return redraws, no trailing whitespace.
///
/// # Errors
///
/// If reading the spool back or writing the output fails.
pub fn passthrough(
    store: &Store,
    spool: &Path,
    budget: Budget,
    out: &mut impl Write,
) -> io::Result<bool> {
    // Rust note: comparing an unsigned byte count with a signed budget
    // needs an explicit conversion; Rust has no implicit integer promotions
    // and no signed/unsigned mixing. `i64::try_from` fails instead of
    // wrapping, so a count past i64::MAX (impossible here) counts as not
    // fitting rather than as a negative number that fits.
    let fits = i64::try_from(store.verbatim_bytes).is_ok_and(|v| v <= budget.bytes());
    if !fits {
        return Ok(false);
    }
    store.for_each_raw(spool, |raw| {
        out.write_all(clean_line(raw).as_bytes())?;
        out.write_all(b"\n")
    })?;
    Ok(true)
}
