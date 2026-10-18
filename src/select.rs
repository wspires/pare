//! Reading a run file back by template, severity and line range: `--only`,
//! `--except`, `--min-severity`, `--since` and `--until`. `parse_lineno()`,
//! `parse_tids()` and `select()` in `proto/pare.py`.
//!
//! A summary names its templates (`T3`) and lines (`L3001`); these flags
//! take those names and print the raw lines behind them, so an agent can
//! expand what the summary left out:
//!
//! ```text
//! pare --only T3,T7 < ~/.cache/pare/runs/1a2b3c4d5e6f.log
//! pare --since L3001 --until L3100 < run-file
//! ```

use std::collections::BTreeSet;
use std::io;
use std::path::Path;

use crate::budget::Budget;
use crate::pyclass::{py_decimal_value, py_isdigit, py_repr, py_strip};
use crate::store::Store;
use crate::summary::{fmt_int, gap_marker};
use crate::text::clip;

const LEVEL_NAME: [&str; 5] = ["debug", "info", "warn", "error", "fatal"];

/// Python's `int(s)` for a string: optional whitespace and sign, decimal
/// digits (any script) with single underscores between them.
fn py_int_literal(s: &str) -> Option<i64> {
    let s = py_strip(s);
    let (neg, digits) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return None;
    }
    let mut n: i64 = 0;
    for c in digits.chars().filter(|&c| c != '_') {
        n = n
            .checked_mul(10)?
            .checked_add(i64::from(py_decimal_value(c)?))?;
    }
    Some(if neg { -n } else { n })
}

/// A line reference as the summary prints it: `L3001` or `3001`; a
/// negative value counts from the end (`-200` is the 200th line from the
/// last). `parse_lineno()`.
///
/// # Errors
///
/// Python's message for a malformed reference or line 0.
pub fn parse_lineno(spec: Option<&str>, total: usize, flag: &str) -> Result<Option<i64>, String> {
    let Some(spec) = spec else { return Ok(None) };
    let s = py_strip(spec).to_uppercase();
    let s = s.strip_prefix('L').unwrap_or(&s);
    let Some(n) = py_int_literal(s) else {
        return Err(format!(
            "{flag} expects a line number like L3001, 3001 or -200; got {}",
            py_repr(spec)
        ));
    };
    if n == 0 {
        return Err(format!("{flag}: lines are numbered from 1"));
    }
    let total = i64::try_from(total).unwrap_or(i64::MAX);
    Ok(Some(if n < 0 { (total + n + 1).max(1) } else { n }))
}

/// Template references, `T3,T7` or `3,7`, as template indices.
/// `parse_tids()`.
///
/// # Errors
///
/// Python's message for an unknown template.
pub fn parse_tids(spec: Option<&str>, ntpl: usize) -> Result<BTreeSet<usize>, String> {
    let mut ids = BTreeSet::new();
    let Some(spec) = spec.filter(|s| !s.is_empty()) else {
        return Ok(ids);
    };
    for part in spec.split(',') {
        let part = py_strip(part).to_uppercase();
        if part.is_empty() {
            continue;
        }
        let part = part.strip_prefix('T').unwrap_or(&part);
        // Python tests `isdigit()` and then calls `int()`, which fails on a
        // digit that is not decimal (a superscript); both are unknown here.
        let n = (!part.is_empty() && part.chars().all(py_isdigit))
            .then(|| py_int_literal(part))
            .flatten()
            .and_then(|n| usize::try_from(n).ok())
            .filter(|&n| (1..=ntpl).contains(&n));
        match n {
            Some(n) => {
                ids.insert(n - 1);
            }
            None => {
                return Err(format!(
                    "unknown template {} (have T1..T{ntpl})",
                    py_repr(part)
                ));
            }
        }
    }
    Ok(ids)
}

/// What to select.
pub struct Selection {
    /// `--only`: these templates (empty: all).
    pub only: BTreeSet<usize>,
    /// `--except`: not these.
    pub exclude: BTreeSet<usize>,
    /// `--min-severity`: records whose head is at least this severe.
    pub min_severity: Option<u8>,
    /// `--since`: from this line number.
    pub since: Option<i64>,
    /// `--until`: up to and including this line number.
    pub until: Option<i64>,
}

/// The raw lines of the selected records, within the budget: the first
/// 70 % and the last 30 % of what fits when not all of it does.
/// `select()`.
///
/// # Errors
///
/// `Err(Ok(message))` for `--since` after `--until`; `Err(Err(e))` if the
/// spool cannot be read back.
// Rust note: a nested `Result` keeps the two failures apart without a new
// error type: the outer `Err` means "no output", the inner says whether it
// is the user's mistake (a message) or the system's (an I/O error).
pub fn select(
    store: &Store,
    sel: &Selection,
    budget: Budget,
    width: i64,
    run_path: Option<&str>,
    spool: &Path,
) -> Result<String, Result<String, io::Error>> {
    let mut want: Vec<usize> = Vec::new();
    for ri in 0..store.nrecords() {
        let t = store.rtpl[ri] as usize;
        if (!sel.only.is_empty() && !sel.only.contains(&t)) || sel.exclude.contains(&t) {
            continue;
        }
        if let Some(min) = sel.min_severity {
            // Decided by the record's head line; a record with no structural
            // signal is left out.
            if store.sev[store.rstart[ri] as usize].is_none_or(|s| s < min) {
                continue;
            }
        }
        want.push(ri);
    }
    let total = store.len() as i64;
    let lo = sel.since.unwrap_or(1);
    let hi = sel.until.unwrap_or(total);
    if lo > hi {
        return Err(Ok(format!("--since L{lo} is after --until L{hi}")));
    }
    // Each wanted record's line numbers within lo..=hi.
    // Line numbers are at least 1 here (parse_lineno sees to it).
    let (lo_n, hi_n) = (
        usize::try_from(lo).unwrap_or(1),
        usize::try_from(hi).unwrap_or(1),
    );
    let span = |ri: usize| {
        let first = (store.rstart[ri] as usize + 1).max(lo_n);
        let last = (store.rend[ri] as usize).min(hi_n);
        first..=last
    };
    // Rust note: `flat_map` chains the records' ranges into one lazy
    // sequence of line numbers, Python's `forward()` generator.
    let forward = || want.iter().flat_map(|&ri| span(ri));

    let n_want: i64 = want.iter().map(|&ri| span(ri).count() as i64).sum();
    let tids = |set: &BTreeSet<usize>| {
        set.iter()
            .map(|t| format!("T{}", t + 1))
            .collect::<Vec<_>>()
            .join(",")
    };
    let mut label = if sel.only.is_empty() {
        "all".to_owned()
    } else {
        format!("--only {}", tids(&sel.only))
    };
    if !sel.exclude.is_empty() {
        label.push_str(&format!(" --except {}", tids(&sel.exclude)));
    }
    if let Some(min) = sel.min_severity {
        label.push_str(&format!(" --min-severity {}", LEVEL_NAME[usize::from(min)]));
    }
    if let Some(since) = sel.since {
        label.push_str(&format!(" --since L{since}"));
    }
    if let Some(until) = sel.until {
        label.push_str(&format!(" --until L{until}"));
    }
    let budget_bytes = budget.bytes();
    let head = format!(
        "# pare {label}: {} of {} lines",
        fmt_int(n_want),
        fmt_int(total)
    );
    let head_len = head.chars().count() as i64;
    // Size of the full rendering, from the per-line sizes and gap markers.
    let mut rows_bytes: i64 = 0;
    let mut prev: Option<usize> = None;
    for no in forward() {
        if let Some(p) = prev.filter(|&p| no != p + 1) {
            rows_bytes += gap_marker(no - p - 1).len() as i64 + 1;
        }
        rows_bytes += i64::from(store.rlen[no - 1]);
        prev = Some(no);
    }
    let kept: Vec<usize>;
    let mut omitted = 0;
    if rows_bytes + head_len + 200 > budget_bytes {
        // Keep the first 70 % and last 30 % of what fits.
        let keep_bytes = (budget_bytes - head_len - 200).max(0);
        #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
        let (first_b, last_b) = (
            (keep_bytes as f64 * 0.7) as i64,
            (keep_bytes as f64 * 0.3) as i64,
        );
        let mut first = Vec::new();
        let mut acc = 0;
        for no in forward() {
            let c = i64::from(store.rlen[no - 1]);
            if acc + c > first_b {
                break;
            }
            first.push(no);
            acc += c;
        }
        let first_set: BTreeSet<usize> = first.iter().copied().collect();
        let mut last = Vec::new();
        acc = 0;
        let backward = want.iter().rev().flat_map(|&ri| span(ri).rev());
        for no in backward {
            let c = i64::from(store.rlen[no - 1]);
            if first_set.contains(&no) || acc + c > last_b {
                break;
            }
            last.push(no);
            acc += c;
        }
        last.reverse();
        first.extend(last);
        kept = first;
        omitted = n_want - kept.len() as i64;
    } else {
        kept = forward().collect();
    }
    let mut out = head;
    out.push('\n');
    let mut file = std::fs::File::open(spool).map_err(Err)?;
    let mut prev: Option<usize> = None;
    for &no in &kept {
        if let Some(p) = prev.filter(|&p| no != p + 1) {
            out.push_str(&gap_marker(no - p - 1));
            out.push('\n');
        }
        let raw = store.read_raw(&mut file, no - 1).map_err(Err)?;
        out.push_str(&format!(
            "{no}: {}\n",
            clip(&crate::text::clean_line(&raw), width)
        ));
        prev = Some(no);
    }
    if omitted > 0 {
        out.push_str(&format!(
            "## omitted {} lines to fit budget {}; raise --budget or narrow --only\n",
            fmt_int(omitted),
            fmt_int(budget.tokens)
        ));
    }
    match run_path {
        Some(p) => out.push_str(&format!("## raw: {p}\n")),
        None => out.push_str("## raw: (not saved)\n"),
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_numbers_like_python() {
        assert_eq!(parse_lineno(Some(" l30 "), 100, "--since"), Ok(Some(30)));
        assert_eq!(parse_lineno(Some("1_0"), 100, "--since"), Ok(Some(10)));
        assert_eq!(parse_lineno(Some("-200"), 100, "--since"), Ok(Some(1)));
        assert_eq!(parse_lineno(Some("-10"), 100, "--since"), Ok(Some(91)));
        assert_eq!(parse_lineno(None, 100, "--since"), Ok(None));
        assert_eq!(
            parse_lineno(Some("0"), 100, "--until"),
            Err("--until: lines are numbered from 1".into())
        );
        assert_eq!(
            parse_lineno(Some("L3x"), 100, "--since"),
            Err("--since expects a line number like L3001, 3001 or -200; got 'L3x'".into())
        );
    }

    #[test]
    fn template_ids_like_python() {
        assert_eq!(parse_tids(Some("t1, T3,,"), 3), Ok([0, 2].into()));
        assert_eq!(parse_tids(Some(""), 3), Ok(BTreeSet::new()));
        assert_eq!(
            parse_tids(Some("T4"), 3),
            Err("unknown template '4' (have T1..T3)".into())
        );
        assert_eq!(
            parse_tids(Some("x"), 3),
            Err("unknown template 'X' (have T1..T3)".into())
        );
    }
}
