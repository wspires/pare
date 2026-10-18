//! Scoring constants, and template salience and markers: `score_templates()`
//! and the scoring constants of `proto/pare.py`.
//!
//! A line's score is set during the pass ([`crate::pass::Segmenter`]): its
//! vocabulary score ([`crate::vocab::vocab_score`]), and for a continuation
//! line a share of its record head's, less a little per level of depth;
//! the first source location after an error line gets a bonus. A
//! template's salience, set here after the pass, is what its exemplar's
//! head says plus how rare the template is.

use std::fs::File;
use std::io;
use std::path::Path;

use crate::store::Store;
use crate::text::clean_line;
use crate::vocab::{Class, Vocab};

/// Template salience: the weight of `1 / (1 + log2 count)`.
pub const RARITY_WEIGHT: f64 = 2.0;
/// ... times this for a template at warning level: a one-off build
/// warning should not outrank a failure seen twice.
pub const WARN_RARITY: f64 = 0.75;
/// The share of its record head's vocabulary score a continuation line
/// inherits.
pub const INHERIT: f64 = 0.6;
/// Subtracted per level of indentation depth, up to 4 levels.
pub const DEPTH_PENALTY: f64 = 0.25;
/// Added to the first source location after an error line in a record.
pub const LOCATION_BONUS: f64 = 1.5;

/// How rare a template is: 1 for a template seen once, falling with the
/// logarithm of its count. `rarity_of()`.
#[must_use]
pub fn rarity(total: u32) -> f64 {
    // Rust note: `log2` calls the platform's C library, as Python's
    // `math.log2` does, so on one machine both give the same bits.
    1.0 / (1.0 + f64::from(total).log2())
}

/// Python's `max(0.0, x)`, which returns `0.0` unless `x` is greater:
/// `f64::max` would return `x` for `x == -0.0`, a different sign of zero.
#[must_use]
pub fn py_max0(x: f64) -> f64 {
    if x > 0.0 { x } else { 0.0 }
}

/// Sets each template's salience and marker, judged by its exemplar record:
/// the first record at the template's highest severity, so a template of
/// 4,000 INFO lines and 3 ERROR lines is represented by an ERROR line. The
/// exemplar's record body is read back from the spool at `spool` to see
/// whether it holds an error. `score_templates()`.
///
/// # Errors
///
/// If the spool cannot be read.
pub fn score_templates(store: &mut Store, vocab: &Vocab, spool: &Path) -> io::Result<()> {
    let mut file: Option<File> = None;
    for t in 0..store.templates.len() {
        let tpl = &store.templates[t];
        let ri = tpl.exemplar as usize;
        let head = store.rstart[ri] as usize;
        let level = store.sev[head];
        // Whether the exemplar's body (its lines after the head, as they
        // are, not as a logcat message) has a strong error word.
        let mut body_err = false;
        let (start, end) = (head + 1, store.rend[ri] as usize);
        if start < end {
            // Rust note: the spool is opened on first need only. `if let
            // ... else` with `?` inside reads like the Python `if f is None:
            // f = open(...)`, but the `File` is closed when `file` goes out
            // of scope, not when the garbage collector gets to it.
            let f = match &mut file {
                Some(f) => f,
                None => file.insert(File::open(spool)?),
            };
            store.for_each_raw_in(f, start, end, |raw| {
                if !body_err && vocab.line(&clean_line(raw)).has(Class::StrongError) {
                    body_err = true;
                }
                Ok(())
            })?;
        }
        let tpl = &mut store.templates[t];
        let warn_factor = if level == Some(2) { WARN_RARITY } else { 1.0 };
        tpl.salience = tpl.vocab + RARITY_WEIGHT * rarity(tpl.total) * warn_factor;
        let h = tpl.head;
        tpl.marker = if level.is_some_and(|l| l <= 1) && !body_err {
            if h.summary { 'S' } else { '.' } // info/debug: structure wins
        } else if level.is_some_and(|l| l >= 3) || body_err || (h.error && !h.pass) {
            'E'
        } else if level == Some(2) || h.warn {
            'W'
        } else if h.summary {
            'S'
        } else {
            '.'
        };
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rarity_and_zero() {
        assert!((rarity(1) - 1.0).abs() < f64::EPSILON);
        assert!((rarity(4) - 1.0 / 3.0).abs() < f64::EPSILON);
        assert!(py_max0(-0.0).is_sign_positive() && py_max0(-1.0) == 0.0 && py_max0(2.5) == 2.5);
    }
}
