//! The single pass over the input: the `Pass` class and `analyze()` of
//! `proto/pare.py`, so far without segmentation, clustering or scoring.
//!
//! Input arrives in chunks (1 MiB from a file or pipe, less from a slow
//! command). Each chunk is written to the spool and fed to the hash as it
//! is, then split into lines; a line cut by the end of a chunk is carried
//! over and completed by the next one. Each complete line is cleaned
//! ([`crate::text::clean_line`]) and recorded in the [`Store`].

use std::io::{self, Read, Write};

use sha2::{Digest, Sha256};

use crate::store::Store;
use crate::text::clean_line;

/// Bytes read per chunk, as `CHUNK` in Python.
pub const CHUNK: usize = 1 << 20;

/// The pass in progress.
// Rust note: `Pass<W: Write>` is generic over where the spool goes: a
// `File` in pare, a `Vec<u8>` in the tests. As with a C++ template, the
// compiler makes one copy per type, so the calls are direct, not virtual;
// unlike a template, the bound (`Write`) is checked here, once.
pub struct Pass<W: Write> {
    spool: W,
    hasher: Sha256,
    /// The start of a line that the last chunk cut off.
    rest: Vec<u8>,
    store: Store,
}

impl<W: Write> Pass<W> {
    /// A pass that writes the input to `spool`.
    pub fn new(spool: W) -> Self {
        Pass {
            spool,
            hasher: Sha256::new(),
            rest: Vec::new(),
            store: Store::new(),
        }
    }

    /// Takes the next chunk of input.
    ///
    /// # Errors
    ///
    /// If writing to the spool fails.
    pub fn feed(&mut self, chunk: &[u8]) -> io::Result<()> {
        self.spool.write_all(chunk)?;
        self.hasher.update(chunk);
        self.store.total_bytes += chunk.len() as u64;

        let mut start = 0;
        // Rust note: `memchr_iter` finds every newline with SIMD, the way
        // `memchr(3)` does, and the lines are slices of `chunk`: borrowed
        // views, no copies. Each `&chunk[a..b]` is bounds-checked; the
        // checks are cheap here and keep the code free of `unsafe`.
        for nl in memchr::memchr_iter(b'\n', chunk) {
            let line = &chunk[start..nl];
            if self.rest.is_empty() {
                add_line(&mut self.store, line);
            } else {
                self.rest.extend_from_slice(line);
                add_line(&mut self.store, &self.rest);
                self.rest.clear();
            }
            start = nl + 1;
        }
        self.rest.extend_from_slice(&chunk[start..]);
        Ok(())
    }

    /// Ends the input: a last line without a newline is still a line.
    /// Returns the store, the content hash's first 12 hex digits (the run
    /// file's name), and the spool writer, flushed.
    ///
    /// # Errors
    ///
    /// If flushing the spool fails.
    pub fn finish(mut self) -> io::Result<(Store, String, W)> {
        if !self.rest.is_empty() {
            add_line(&mut self.store, &self.rest);
        }
        self.spool.flush()?;
        let digest = self.hasher.finalize();
        let hex: String = digest.iter().take(6).map(|b| format!("{b:02x}")).collect();
        Ok((self.store, hex, self.spool))
    }
}

// Rust note: this is a free function taking the store, not a method taking
// `&mut self`, because of the borrow rules. In `feed`, the line can be a
// borrow of `self.rest`; a `&mut self` method would need all of `self`
// mutably while part of it is borrowed, which the compiler refuses.
// Passing just `&mut self.store` borrows a different field, and disjoint
// fields can be borrowed separately. In C++ this would compile either way
// and be correct only as long as nobody later makes the method touch `rest`.
fn add_line(store: &mut Store, raw: &[u8]) {
    let clean = clean_line(raw);
    store.push(raw.len(), clean.len());
}

/// Reads all of `input` through a pass that writes it to `spool`.
///
/// # Errors
///
/// If reading the input or writing the spool fails.
pub fn read_all<R: Read, W: Write>(mut input: R, spool: W) -> io::Result<(Store, String, W)> {
    let mut pass = Pass::new(spool);
    let mut buf = vec![0u8; CHUNK];
    loop {
        // Rust note: `read` returns how many bytes it filled; 0 means end
        // of input. A signal can interrupt it (`Interrupted`), which is not
        // an error, just a reason to read again; Python retries for you.
        let n = match input.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        };
        pass.feed(&buf[..n])?;
    }
    pass.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(chunks: &[&[u8]]) -> (Store, String, Vec<u8>) {
        let mut pass = Pass::new(Vec::new());
        for c in chunks {
            pass.feed(c).unwrap();
        }
        pass.finish().unwrap()
    }

    #[test]
    fn lines_cut_by_chunks_are_rejoined() {
        let (whole, h1, spool1) = run(&[b"alpha\nbeta\ngamma"]);
        let (split, h2, spool2) = run(&[b"al", b"pha\nbe", b"ta\n", b"gam", b"ma"]);
        assert_eq!((whole.len(), split.len()), (3, 3));
        assert_eq!(whole.verbatim_bytes, split.verbatim_bytes);
        assert_eq!(h1, h2);
        assert_eq!(spool1, spool2);
    }

    #[test]
    fn trailing_newline_makes_no_extra_line() {
        assert_eq!(run(&[b"a\nb\n"]).0.len(), 2);
        assert_eq!(run(&[b"a\nb"]).0.len(), 2);
        assert_eq!(run(&[b"\n\n"]).0.len(), 2);
        assert_eq!(run(&[b""]).0.len(), 0);
    }

    #[test]
    fn hash_matches_python() {
        // Python: hashlib.sha256(b"hello\n").hexdigest()[:12]
        assert_eq!(run(&[b"hello\n"]).1, "5891b5b522d5");
    }
}
