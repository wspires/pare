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
//! Scores are written with six decimals, which Rust's `{:.6}` and Python's
//! `%.6f` print alike (both round the exact binary value half to even, and
//! both write `-0.000000`); -1 or `-` stands for none. A field a later port
//! does not compute can be written as `?`, which parity skips.

use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::Path;

use crate::store::Store;

/// The dump format's version, the same as `DUMP_VERSION` in Python.
pub const DUMP_VERSION: u32 = 1;

/// A severity as the dump writes it: -1 for none.
fn sev_text(sev: Option<u8>) -> i16 {
    sev.map_or(-1, i16::from)
}

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
            |r| store.templates[store.rtpl[r as usize] as usize].tid(),
        );
        writeln!(
            f,
            "L\t{}\t{rec_text}\t{}\t{}\t{}\t{tid}\t{:.6}\t{}\t{}",
            store.base + i + 1,
            store.depth[i],
            sev_text(store.sev[i]),
            u8::from(store.blank[i]),
            store.score[i],
            store.rlen[i],
            store.slen[i],
        )?;
    }
    for t in &store.templates {
        let text = t
            .text
            .replace('\\', "\\\\")
            .replace('\t', "\\t")
            .replace('\n', "\\n");
        // Records per severity, highest first: "3:2,1:40", or "-".
        let levels: Vec<String> = (0..5)
            .rev()
            .filter(|&k| t.levels[k] > 0)
            .map(|k| format!("{k}:{}", t.levels[k]))
            .collect();
        let levels = if levels.is_empty() {
            "-".to_owned()
        } else {
            levels.join(",")
        };
        writeln!(
            f,
            "T\t{}\t{}\t{}\t{}\t{}\t{}\t{:.6}\t{:.6}\t{}\t{levels}\t{text}",
            t.tid(),
            t.count,
            t.lines,
            t.total,
            sev_text(t.best_sev),
            t.marker,
            t.salience,
            t.vocab,
            store.base + store.rstart[t.exemplar as usize] as usize + 1,
        )?;
    }
    f.flush()
}
