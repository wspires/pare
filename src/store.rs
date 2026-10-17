//! Where each line is: the `Store` class of `proto/pare.py`, so far only
//! its offsets and byte counts.
//!
//! pare never keeps the input's text in memory. Every chunk is written to
//! the spool file (the run file, or a temporary file under `--no-tee`) as it
//! arrives, and the store records, per line, where it starts in that file
//! and how long it is. A line's text is read back only when the output
//! needs it. Later stages add more per-line columns (record, depth,
//! severity, score), one vector each, as the Python version keeps one typed
//! array each.

use std::fs::File;
use std::io::{self, BufReader, Read, Seek, SeekFrom};
use std::path::Path;

/// Per-line offsets and sizes for one input.
#[derive(Debug, Default)]
pub struct Store {
    /// Byte offset of each raw line in the spool.
    off: Vec<u64>,
    /// Length of each raw line, without its newline.
    raw_len: Vec<u32>,
    /// Offset at which the next line starts.
    end: u64,
    /// Bytes of input in all, newlines included.
    pub total_bytes: u64,
    /// Bytes of the cleaned text with one newline per line: the size of a
    /// verbatim pass-through.
    pub verbatim_bytes: u64,
}

impl Store {
    /// An empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
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

    /// Records a line of `raw_len` bytes (newline excluded) whose cleaned
    /// text is `clean_len` bytes long. Lines are pushed in input order.
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
