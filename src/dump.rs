//! `$PARE_DUMP=FILE`: the pass's per-line and per-template results as
//! tab-separated rows, in the format of `write_dump()` in `proto/pare.py`,
//! so `bench/parity.py --dump` can compare the two implementations line by
//! line and report the first row where they part ways.
//!
//! ```text
//! L  no  record  depth  severity  blank  template  score  rendered-bytes  stripped-chars
//! T  id  records  lines  total  best-severity  marker  salience  vocab  exemplar-line  levels  text
//! ```
//!
//! Fields the port does not compute yet are written as `?`, which parity
//! skips: so far severity, score, and everything about a template that
//! depends on them (best severity, marker, salience, vocabulary, exemplar,
//! levels).

use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::Path;

use crate::store::Store;

/// The dump format's version, the same as `DUMP_VERSION` in Python.
pub const DUMP_VERSION: u32 = 1;

/// Not computed by the port yet; parity skips it.
const TODO: &str = "?";

/// Writes the dump of `store` to `path`.
///
/// # Errors
///
/// If the file cannot be written.
pub fn write_dump(store: &Store, path: &Path) -> io::Result<()> {
    let mut f = BufWriter::new(File::create(path)?);
    writeln!(f, "# pare dump v{DUMP_VERSION}")?;
    for i in 0..store.len() {
        let rec = store.rec[i];
        // Rust note: `map_or` turns the `Option` into the column's text in
        // one expression: -1 / - for no record, as in Python's format.
        let rec_text = rec.map_or_else(|| "-1".to_owned(), |r| r.to_string());
        let tid = rec.map_or_else(
            || "-".to_owned(),
            |r| format!("T{}", store.rtpl[r as usize] + 1),
        );
        writeln!(
            f,
            "L\t{}\t{rec_text}\t{}\t{TODO}\t{}\t{tid}\t{TODO}\t{}\t{}",
            i + 1,
            store.depth[i],
            u8::from(store.blank[i]),
            store.rlen[i],
            store.slen[i],
        )?;
    }
    for (idx, t) in store.templates.iter().enumerate() {
        let text = t
            .text
            .replace('\\', "\\\\")
            .replace('\t', "\\t")
            .replace('\n', "\\n");
        writeln!(
            f,
            "T\tT{}\t{}\t{}\t{}\t{TODO}\t{TODO}\t{TODO}\t{TODO}\t{TODO}\t{TODO}\t{text}",
            idx + 1,
            t.count,
            t.lines,
            t.total,
        )?;
    }
    f.flush()
}
