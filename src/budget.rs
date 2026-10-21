//! Tokens and the budget: the same arithmetic as `tokens()` and
//! `budget_bytes_for()` in `reference/pare.py`.
//!
//! A token is estimated as `bytes / chars_per_token` (2.8 by default, the
//! rate measured on cl100k and o200k, see docs/DESIGN.md), and the budget
//! in bytes is the budget in tokens times that rate, truncated.
//!
//! An automatic budget (`--budget auto`, not in Python) is a ceiling the
//! summary need not reach: see [`Budget::auto`].

/// Bytes per token unless `--chars-per-token` says otherwise.
pub const DEFAULT_CHARS_PER_TOKEN: f64 = 2.8;

/// An automatic budget passes input through whole only up to this many
/// tokens (or its ceiling, if lower); a fixed budget does up to the budget.
/// Tuned with `bench/run.py`: see `docs/DESIGN.md`, "Automatic budget".
pub const AUTO_PASSTHROUGH_TOKENS: i64 = 1000;

/// An automatic budget sizes the head and tail windows as a budget of this
/// many tokens would (or its ceiling, if lower), so a higher ceiling does
/// not lengthen them.
pub const AUTO_SIZING_TOKENS: i64 = 2000;

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
    /// `--budget auto`: `tokens` is a ceiling, and the summary stops when
    /// what is left to show is below a higher salience floor, so a log
    /// with little news gets a short summary.
    pub auto: bool,
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

    /// Input up to this many bytes is the output, unchanged.
    #[must_use]
    pub fn passthrough_bytes(&self) -> i64 {
        self.capped(AUTO_PASSTHROUGH_TOKENS).bytes()
    }

    /// The budget, in bytes, that the head and tail windows are sized for.
    #[must_use]
    pub fn sizing_bytes(&self) -> i64 {
        self.capped(AUTO_SIZING_TOKENS).bytes()
    }

    /// The budget, or for an automatic one at most `cap` tokens.
    fn capped(&self, cap: i64) -> Budget {
        if self.auto {
            Budget {
                tokens: self.tokens.min(cap),
                ..*self
            }
        } else {
            *self
        }
    }

    /// Estimated tokens in `nbytes` bytes of text.
    ///
    /// ```
    /// use pare::budget::{Budget, DEFAULT_CHARS_PER_TOKEN};
    /// let b = Budget { tokens: 2000, chars_per_token: DEFAULT_CHARS_PER_TOKEN, auto: false };
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
        auto: false,
    };

    #[test]
    fn matches_python() {
        // Values from reference/pare.py: budget_bytes_for(600) and tokens(n).
        assert_eq!(B.bytes(), 1680);
        assert_eq!(B.tokens_in(0), 0);
        assert_eq!(B.tokens_in(1), 1);
        assert_eq!(B.tokens_in(19_046), 6_803);
        let four = Budget {
            tokens: 600,
            chars_per_token: 4.0,
            auto: false,
        };
        assert_eq!(four.bytes(), 2400);
        assert_eq!(four.tokens_in(2401), 601);
    }

    #[test]
    fn auto_caps_passthrough_and_sizing() {
        assert_eq!((B.passthrough_bytes(), B.sizing_bytes()), (1680, 1680));
        let auto = |tokens| Budget {
            tokens,
            auto: true,
            ..B
        };
        assert_eq!(auto(600).passthrough_bytes(), 1680);
        assert_eq!(auto(4000).passthrough_bytes(), 2800);
        assert_eq!(auto(4000).sizing_bytes(), 5600);
        assert_eq!(auto(4000).bytes(), 11_200); // still the ceiling
    }
}
