//! Tokens and the budget: the same arithmetic as `tokens()` and
//! `budget_bytes_for()` in `proto/pare.py`.
//!
//! A token is estimated as `bytes / chars_per_token` (2.8 by default, the
//! rate measured on cl100k and o200k, see docs/DESIGN.md), and the budget
//! in bytes is the budget in tokens times that rate, truncated.

/// Bytes per token unless `--chars-per-token` says otherwise.
pub const DEFAULT_CHARS_PER_TOKEN: f64 = 2.8;

/// The budget and the token rate it is measured in.
// Rust note: `#[derive(...)]` asks the compiler to write the obvious
// implementations: `Clone`/`Copy` (this is two plain numbers, so copying
// is free and implicit, like a trivially copyable struct in C++), `Debug`
// for `{:?}` printing, and `PartialEq` for `==`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Budget {
    /// The ceiling on pare's output, in tokens.
    pub tokens: i64,
    /// Bytes per token.
    pub chars_per_token: f64,
}

impl Budget {
    /// The budget in bytes: `int(budget * chars_per_token)` in Python.
    #[must_use]
    pub fn bytes(&self) -> i64 {
        // Rust note: `as` between numeric types never fails and never
        // panics; float to int truncates toward zero and saturates at the
        // type's limits (no undefined behaviour, unlike an out-of-range
        // float-to-int conversion in C++). Python's `int()` also truncates
        // toward zero, so the two agree.
        #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
        let bytes = (self.tokens as f64 * self.chars_per_token) as i64;
        bytes
    }

    /// Estimated tokens in `nbytes` bytes of text.
    ///
    /// ```
    /// use pare::budget::{Budget, DEFAULT_CHARS_PER_TOKEN};
    /// let b = Budget { tokens: 2000, chars_per_token: DEFAULT_CHARS_PER_TOKEN };
    /// assert_eq!(b.tokens_in(2800), 1000); // not the 1,001 that 2800 / 2.8 gives in floating point
    /// assert_eq!(b.bytes(), 5600);
    /// ```
    #[must_use]
    pub fn tokens_in(&self, nbytes: u64) -> u64 {
        // Less a hair, as in Python, so that 2,800 bytes at 2.8 is 1,000
        // tokens: 2800.0 / 2.8 is 1000.0000000000001 in binary floating point.
        #[allow(
            clippy::cast_possible_truncation,
            clippy::cast_precision_loss,
            clippy::cast_sign_loss
        )]
        let t = (nbytes as f64 / self.chars_per_token - 1e-9)
            .ceil()
            .max(0.0) as u64;
        t
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const B: Budget = Budget {
        tokens: 600,
        chars_per_token: DEFAULT_CHARS_PER_TOKEN,
    };

    #[test]
    fn matches_python() {
        // Values from proto/pare.py: budget_bytes_for(600) and tokens(n).
        assert_eq!(B.bytes(), 1680);
        assert_eq!(B.tokens_in(0), 0);
        assert_eq!(B.tokens_in(1), 1);
        assert_eq!(B.tokens_in(19_046), 6_803);
        let four = Budget {
            tokens: 600,
            chars_per_token: 4.0,
        };
        assert_eq!(four.bytes(), 2400);
        assert_eq!(four.tokens_in(2401), 601);
    }
}
