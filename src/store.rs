//! What the pass learned about each line, record and template: the `Store`
//! class of `reference/pare.py`.
//!
//! pare never keeps the input's text in memory. Every chunk is written to
//! the spool file (the run file, or a temporary file under `--no-tee`) as it
//! arrives, and the store records, per line, where it starts in that file
//! and how long it is, plus what segmentation found: which record the line
//! belongs to and how deep in it. A line's text is read back only when the
//! output needs it.
//!
//! Each fact is a column: one `Vec` per field, indexed by line (or by
//! record), as the Python version keeps one typed array each. A column of
//! `u32` costs 4 bytes a line; a `Vec` of per-line structs with the same
//! fields would cost the same or more and be slower to scan one field of.

use std::fs::File;
use std::io::{self, BufReader, Read, Seek, SeekFrom};
use std::path::Path;

/// One template (a Drain cluster) as this input, or this follow window,
/// uses it.
#[derive(Debug, Clone)]
pub struct Template {
    /// The template text, final once the window is cut.
    pub text: String,
    /// Drain's id for the cluster.
    pub cluster_id: usize,
    /// Its id across follow windows, shown as `T<gid+1>`; in batch mode its
    /// index in `templates`.
    pub gid: usize,
    /// Follow mode: first seen in this window.
    pub new: bool,
    /// Follow mode: `" (rate 0.2/s → 11/s)"` when the template bursts.
    pub rate_note: String,
    /// Records of this template (in this window).
    pub count: u32,
    /// Lines in those records (heads, continuations, adopted blank lines).
    pub lines: u32,
    /// Records of this template in the whole input (in batch mode, `count`).
    pub total: u32,
    /// Records of this template at each structural severity, 0 to 4.
    pub levels: [u32; 5],
    /// The record shown for this template: the first at the highest
    /// severity its records have (or its first record, if none has one).
    pub exemplar: u32,
    /// That severity; `None` while no record has had one.
    pub best_sev: Option<u8>,
    /// What the exemplar's head line says: its `vocab_score` as a head.
    pub vocab: f64,
    /// Which vocabulary classes the exemplar's head has, for the marker.
    pub head: HeadClasses,
    /// Salience: `vocab` plus how rare the template is. Set by
    /// [`crate::score::score_templates`].
    pub salience: f64,
    /// `E` error, `W` warning, `S` summary, `.` other. Set by
    /// [`crate::score::score_templates`].
    pub marker: char,
}

impl Template {
    /// A template first seen (in this window) at record `exemplar`.
    #[must_use]
    pub fn new(cluster_id: usize, gid: usize, exemplar: u32, new: bool) -> Self {
        Template {
            text: String::new(),
            cluster_id,
            gid,
            new,
            rate_note: String::new(),
            count: 0,
            lines: 0,
            total: 0,
            levels: [0; 5],
            exemplar,
            best_sev: None,
            vocab: 0.0,
            head: HeadClasses::default(),
            salience: 0.0,
            marker: '.',
        }
    }

    /// Its name in the output: `T3`.
    #[must_use]
    pub fn tid(&self) -> String {
        format!("T{}", self.gid + 1)
    }
}

/// The head of a record that a follow cut left open: what the next window
/// needs to attribute its continuation lines and to show the entry.
#[derive(Debug, Clone)]
pub struct CarriedHead {
    /// Its line number in the input.
    pub no: usize,
    /// Its text.
    pub text: String,
    /// Its severity.
    pub sev: Option<u8>,
    /// Its template's index in the new window's templates, once known.
    pub tpl_idx: usize,
    /// Its template's id across windows.
    pub gid: usize,
    /// Its template's Drain cluster.
    pub cluster_id: usize,
}

/// The vocabulary classes of a template's exemplar head that its marker
/// depends on.
#[derive(Debug, Clone, Copy, Default)]
#[allow(clippy::struct_excessive_bools)] // one flag per class
pub struct HeadClasses {
    /// The error class.
    pub error: bool,
    /// The warn class.
    pub warn: bool,
    /// The summary class.
    pub summary: bool,
    /// The pass class.
    pub pass: bool,
}

/// Per-line, per-record and per-template results for one input, or one
/// follow window of it.
#[derive(Debug, Default)]
pub struct Store {
    /// Index of the first line in the input: 0, or a follow window's start.
    pub base: usize,
    /// Follow mode: record 0 continues a record cut by the previous window.
    pub carry: Option<CarriedHead>,
    /// Byte offset of each raw line in the spool.
    off: Vec<u64>,
    /// Length of each raw line, without its newline.
    raw_len: Vec<u32>,
    /// Offset at which the next line starts.
    end: u64,
    /// Bytes of input in all, newlines included.
    pub total_bytes: u64,
    /// Batch mode: the offset of a NUL byte in the input's first 8 KiB,
    /// which makes it binary (an executable, an image, UTF-16 text), so
    /// the lines were not segmented and there is nothing to summarise.
    pub binary_at: Option<u64>,
    /// Bytes of the cleaned text with one newline per line: the size of a
    /// verbatim pass-through.
    pub verbatim_bytes: u64,

    /// Per line: the record it belongs to, or `None` for a blank line
    /// outside any record.
    // Rust note: `Option<u32>` where Python stores -1 for "none". The
    // compiler makes every use say what happens in the `None` case, so
    // "forgot to check for -1" cannot happen. It costs 8 bytes rather than
    // 4; a `u32` with a sentinel would halve that if memory ever mattered.
    pub rec: Vec<Option<u32>>,
    /// Per line: 0 for a record's head, 1 and up for continuation lines.
    pub depth: Vec<u32>,
    /// Per line: whether it is blank.
    pub blank: Vec<bool>,
    /// Per line: bytes of the rendered line (`"<no>: <clipped text>"`) plus
    /// its newline.
    pub rlen: Vec<u32>,
    /// Per line: characters of the text after `strip()`.
    pub slen: Vec<u32>,
    /// Per line: structural severity, 0 (debug) to 4 (fatal), if any.
    pub sev: Vec<Option<u8>>,
    /// Per line: salience, its vocabulary score plus inheritance, depth
    /// and location adjustments.
    pub score: Vec<f64>,

    /// Per record: its first line.
    pub rstart: Vec<u32>,
    /// Per record: one past its last line.
    pub rend: Vec<u32>,
    /// Per record: its template, an index into `templates`.
    pub rtpl: Vec<u32>,

    /// Templates in order of first appearance: `T1` is `templates[0]`.
    pub templates: Vec<Template>,
}

impl Store {
    /// An empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// An empty store for the lines from index `base` on, the first of
    /// which starts at byte `offset` of the spool.
    #[must_use]
    pub fn starting_at(base: usize, offset: u64) -> Self {
        Store {
            base,
            end: offset,
            ..Self::default()
        }
    }

    /// The spool offset of the first line (the next line's, if none).
    #[must_use]
    pub fn first_offset(&self) -> u64 {
        self.off.first().copied().unwrap_or(self.end)
    }

    /// The spool offset after the last line and its newline.
    #[must_use]
    pub fn end_offset(&self) -> u64 {
        self.end
    }

    /// Number of lines.
    #[must_use]
    pub fn len(&self) -> usize {
        self.off.len()
    }

    /// Whether there are no lines.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.off.is_empty()
    }

    /// Number of records.
    #[must_use]
    pub fn nrecords(&self) -> usize {
        self.rstart.len()
    }

    /// Records where a line of `raw_len` bytes (newline excluded) is, and
    /// that its cleaned text is `clean_len` bytes long. Lines are pushed in
    /// input order; the other per-line columns are filled by the pass.
    ///
    /// # Panics
    ///
    /// If one line is 4 GiB or longer, which no log has; the Python version
    /// keeps line lengths in a 32-bit array as well.
    pub fn push(&mut self, raw_len: usize, clean_len: usize) {
        // Rust note: `u32::try_from` is a checked conversion: it fails
        // instead of silently wrapping, as `static_cast<uint32_t>` would.
        // Here an overflow is a bug in the assumptions, so it stops loudly.
        let len = u32::try_from(raw_len).expect("a line shorter than 4 GiB");
        self.off.push(self.end);
        self.raw_len.push(len);
        self.end += u64::from(len) + 1;
        self.verbatim_bytes += clean_len as u64 + 1;
    }

    /// Calls `f` with each raw line (without its newline), in order, read
    /// back from the spool at `path`.
    ///
    /// # Errors
    ///
    /// If the spool cannot be opened or read, or `f` fails.
    // Rust note: `f` is any closure that can be called repeatedly and may
    // change what it captures (`FnMut`). The function is generic over its
    // type, so each call site gets its own compiled copy with the closure
    // inlined: what a C++ template taking a lambda does, with the
    // signature checked here rather than at instantiation.
    pub fn for_each_raw<F>(&self, path: &Path, mut f: F) -> io::Result<()>
    where
        F: FnMut(&[u8]) -> io::Result<()>,
    {
        let mut reader = BufReader::with_capacity(1 << 16, File::open(path)?);
        let mut pos = 0u64;
        // One buffer for every line: it grows to the longest line and is
        // reused, so reading back does not allocate per line.
        let mut buf = Vec::new();
        for (&off, &len) in self.off.iter().zip(&self.raw_len) {
            if off != pos {
                reader.seek(SeekFrom::Start(off))?;
            }
            buf.resize(len as usize, 0);
            reader.read_exact(&mut buf)?;
            pos = off + u64::from(len);
            f(&buf)?;
        }
        Ok(())
    }

    /// The raw bytes of line `i` (without its newline), read from `file`,
    /// the spool.
    ///
    /// # Errors
    ///
    /// If reading fails.
    pub fn read_raw(&self, file: &mut File, i: usize) -> io::Result<Vec<u8>> {
        let mut buf = vec![0u8; self.raw_len[i] as usize];
        file.seek(SeekFrom::Start(self.off[i]))?;
        file.read_exact(&mut buf)?;
        Ok(buf)
    }

    /// Calls `f` with each raw line of lines `start..end` (indices), read
    /// from `file`, the spool, in one go.
    ///
    /// # Errors
    ///
    /// If reading fails, or `f` does.
    pub fn for_each_raw_in<F>(
        &self,
        file: &mut File,
        start: usize,
        end: usize,
        mut f: F,
    ) -> io::Result<()>
    where
        F: FnMut(&[u8]) -> io::Result<()>,
    {
        if start >= end {
            return Ok(());
        }
        let from = self.off[start];
        let to = self.off[end - 1] + u64::from(self.raw_len[end - 1]);
        let mut buf = vec![0u8; usize::try_from(to - from).expect("a record fits in memory")];
        file.seek(SeekFrom::Start(from))?;
        file.read_exact(&mut buf)?;
        for line in buf.split(|&b| b == b'\n') {
            f(line)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn offsets_follow_lines_and_read_back() {
        let dir = std::env::temp_dir().join(format!("pare-store-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("spool");
        let data = b"first\n\nthird line\nno newline";
        File::create(&path).unwrap().write_all(data).unwrap();

        let mut store = Store::new();
        for line in data.split(|&b| b == b'\n') {
            store.push(line.len(), line.len());
        }
        assert_eq!(store.len(), 4);
        assert_eq!(store.off, [0, 6, 7, 18]);

        let mut lines = Vec::new();
        store
            .for_each_raw(&path, |raw| {
                lines.push(raw.to_vec());
                Ok(())
            })
            .unwrap();
        assert_eq!(lines, [&b"first"[..], b"", b"third line", b"no newline"]);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
