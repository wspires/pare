//! Looking after the run directory: pruning old run files (`pare prune`, and
//! automatically at most hourly) and `pare stats`, what command mode saved.
//! `prune_runs()`, `maybe_prune()`, `prune_command()` and `stats()` in
//! `reference/pare.py`.
//!
//! Only files pare named itself are ever removed: a run file
//! (`<12 hex>.log`, `follow-<time>-<pid>.log`) and a spool a killed pare
//! left behind (`.pare-<pid>.tmp`). The small `.json` record next to a
//! pruned run file stays, marked `"pruned"`, so `pare stats` keeps its
//! history.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::budget::DEFAULT_CHARS_PER_TOKEN;
use crate::pyclass::py_isspace;
use crate::pyjson::{dumps, py_float_repr};
use crate::pyre::PyRegex;
use crate::summary::fmt_int;
use crate::text::clip;

static RUN_FILE: LazyLock<PyRegex> = LazyLock::new(|| {
    PyRegex::new(r"^(?:[0-9a-f]{12}|follow-\d{8}-\d{6}-\d+)\.log$").expect("valid")
});
static STALE_SPOOL: LazyLock<PyRegex> =
    LazyLock::new(|| PyRegex::new(r"^\.pare-\d+\.tmp$").expect("valid"));

/// `$PARE_RUN_KEEP_DAYS`: run files older than this go (0: no age limit).
const PRUNE_KEEP_DAYS: f64 = 14.0;
/// `$PARE_RUN_MAX_MB`: past this total, the oldest go first (0: no limit).
const PRUNE_MAX_MB: f64 = 500.0;
/// ...but never one younger than a day: a summary may still point at it.
const PRUNE_MIN_AGE_S: f64 = 86400.0;
/// Automatic pruning runs at most this often (`$PARE_RUN_PRUNE=off`
/// disables it).
const PRUNE_EVERY_S: f64 = 3600.0;
const PRUNE_MARKER: &str = ".last-prune";

/// Seconds since the Unix epoch, as Python's `time.time()` and
/// `st_mtime` give them.
fn epoch_seconds(t: SystemTime) -> f64 {
    t.duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))`.
///
/// ```
/// use pare::runs::utc_stamp;
/// assert_eq!(utc_stamp(0.0), "1970-01-01T00:00:00Z");
/// assert_eq!(utc_stamp(1_760_745_600.9), "2025-10-18T00:00:00Z");
/// ```
#[must_use]
pub fn utc_stamp(t: f64) -> String {
    // Rust note: std has no calendar; this is Howard Hinnant's
    // days-to-civil algorithm (the one behind C++20's `<chrono>` dates),
    // which avoids pulling in a date crate for one timestamp format.
    #[allow(clippy::cast_possible_truncation)]
    let secs = t.floor() as i64;
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

/// Python's `format(x, "g")`: six significant digits, trailing zeros
/// dropped, scientific notation below 1e-4 and from 1e6.
///
/// ```
/// use pare::runs::py_g;
/// assert_eq!(py_g(14.0), "14");
/// assert_eq!(py_g(0.5), "0.5");
/// assert_eq!(py_g(1_234_567.0), "1.23457e+06");
/// assert_eq!(py_g(f64::INFINITY), "inf");
/// ```
#[must_use]
pub fn py_g(x: f64) -> String {
    py_format_g(x, 6)
}

/// Python's `format(x, ".<precision>g")`: `precision` significant digits
/// (at least 1), trailing zeros dropped, scientific notation below 1e-4
/// and from `10^precision`.
///
/// ```
/// use pare::runs::py_format_g;
/// assert_eq!(py_format_g(0.2345, 2), "0.23");
/// assert_eq!(py_format_g(11.0, 2), "11");
/// assert_eq!(py_format_g(123.0, 2), "1.2e+02");
/// ```
#[must_use]
pub fn py_format_g(x: f64, precision: usize) -> String {
    let p = precision.max(1);
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.to_owned();
    }
    let sci = format!("{x:.*e}", p - 1);
    let (mantissa, exp) = sci.split_once('e').expect("an exponent");
    let exp: i32 = exp.parse().expect("an integer exponent");
    let trim = |s: &str| {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            s.to_owned()
        }
    };
    let p_i = i32::try_from(p).unwrap_or(i32::MAX);
    if (-4..p_i).contains(&exp) {
        trim(&format!(
            "{x:.*}",
            usize::try_from(p_i - 1 - exp).unwrap_or(0)
        ))
    } else {
        format!(
            "{}e{}{:02}",
            trim(mantissa),
            if exp < 0 { '-' } else { '+' },
            exp.abs()
        )
    }
}

/// Python's `f"{x:,.0f}"`: rounded to an integer, with thousands
/// separators.
fn fmt_thousands_f(x: f64) -> String {
    let s = format!("{x:.0}");
    let (sign, digits) = s.strip_prefix('-').map_or(("", s.as_str()), |d| ("-", d));
    let mut out = String::from(sign);
    for (k, c) in digits.chars().enumerate() {
        if k > 0 && (digits.len() - k).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// `prune_limits()`: the age and size limits from the environment.
fn prune_limits() -> (f64, f64) {
    let number = |var: &str, default: f64| match std::env::var(var) {
        // Python's float() strips whitespace and accepts "inf" and "nan";
        // its max(0.0, x) turns NaN into 0.0.
        Ok(v) => py_strip_parse(&v).map_or(default, |x| if x > 0.0 { x } else { 0.0 }),
        Err(_) => default,
    };
    (
        number("PARE_RUN_KEEP_DAYS", PRUNE_KEEP_DAYS),
        number("PARE_RUN_MAX_MB", PRUNE_MAX_MB),
    )
}

fn py_strip_parse(v: &str) -> Option<f64> {
    v.trim_matches(py_isspace).replace('_', "").parse().ok()
}

/// A file removed (or that would be): its path, size and why.
pub struct Removed {
    path: PathBuf,
    size: u64,
    reason: String,
}

/// `prune_runs()`: removes run files older than `keep_days`, then, oldest
/// first, as many as it takes to bring the rest under `max_mb`, sparing
/// any younger than a day. Returns what it removed (with `dry_run`, what
/// it would remove).
#[must_use]
pub fn prune_runs(
    run_dir: &Path,
    keep_days: f64,
    max_mb: f64,
    now: f64,
    dry_run: bool,
) -> Vec<Removed> {
    let Ok(entries) = fs::read_dir(run_dir) else {
        return Vec::new();
    };
    let mut removed = Vec::new();
    let mut runs: Vec<(f64, u64, PathBuf)> = Vec::new();
    for entry in entries.flatten() {
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let path = run_dir.join(&name);
        // Rust note: `fs::metadata` follows symlinks, as Python's `os.stat`.
        let Ok(st) = fs::metadata(&path) else {
            continue;
        };
        let mtime = st.modified().map_or(0.0, epoch_seconds);
        if STALE_SPOOL.is_match(&name) {
            if now - mtime > PRUNE_MIN_AGE_S {
                removed.push(Removed {
                    path,
                    size: st.len(),
                    reason: "spool left by an interrupted run".into(),
                });
            }
        } else if RUN_FILE.is_match(&name) {
            runs.push((mtime, st.len(), path));
        }
    }
    // Oldest first; Python sorts the (mtime, size, path) tuples.
    runs.sort_by(|a, b| {
        a.0.total_cmp(&b.0)
            .then(a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(&b.2))
    });
    let mut kept = Vec::new();
    for (mtime, size, path) in runs {
        if keep_days != 0.0 && now - mtime > keep_days * 86400.0 {
            removed.push(Removed {
                path,
                size,
                reason: format!("older than {} days", py_g(keep_days)),
            });
        } else {
            kept.push((mtime, size, path));
        }
    }
    if max_mb != 0.0 {
        let mut total: u64 = kept.iter().map(|k| k.1).sum();
        for (mtime, size, path) in kept {
            #[allow(clippy::cast_precision_loss)]
            if total as f64 <= max_mb * 1024.0 * 1024.0 || now - mtime < PRUNE_MIN_AGE_S {
                break;
            }
            removed.push(Removed {
                path,
                size,
                reason: format!("over {} MB in all", py_g(max_mb)),
            });
            total -= size;
        }
    }
    if !dry_run {
        let stamp = utc_stamp(now);
        for r in &removed {
            if fs::remove_file(&r.path).is_err() {
                continue;
            }
            // The record next to it stays, marked as pruned.
            let meta = r.path.with_extension("json");
            if let Ok(Value::Object(mut map)) = fs::read_to_string(&meta)
                .map_err(|_| ())
                .and_then(|t| serde_json::from_str(&t).map_err(|_| ()))
            {
                map.insert("pruned".into(), Value::String(stamp.clone()));
                let _ = fs::write(&meta, dumps(&Value::Object(map)) + "\n");
            }
        }
    }
    removed
}

/// `maybe_prune()`: automatic pruning, at most once per hour; never in
/// the way of the summary (errors are ignored).
pub fn maybe_prune(run_dir: &Path) {
    let off = std::env::var("PARE_RUN_PRUNE")
        .unwrap_or_default()
        .to_lowercase();
    if ["off", "0", "no", "false"].contains(&off.as_str()) {
        return;
    }
    let marker = run_dir.join(PRUNE_MARKER);
    let now = epoch_seconds(SystemTime::now());
    if let Ok(st) = fs::metadata(&marker) {
        if now - st.modified().map_or(0.0, epoch_seconds) < PRUNE_EVERY_S {
            return;
        }
    }
    if fs::File::create(&marker).is_ok() {
        let (keep_days, max_mb) = prune_limits();
        let _ = prune_runs(run_dir, keep_days, max_mb, now, false);
    }
}

/// `pare prune [--dry-run]`: prunes now and lists what went.
///
/// # Errors
///
/// If writing to `out` fails.
pub fn prune_command(run_dir: &Path, dry_run: bool, out: &mut impl Write) -> io::Result<()> {
    let (keep_days, max_mb) = prune_limits();
    let removed = prune_runs(
        run_dir,
        keep_days,
        max_mb,
        epoch_seconds(SystemTime::now()),
        dry_run,
    );
    let limits = format!(
        "keep {} days, at most {} MB; $PARE_RUN_KEEP_DAYS, $PARE_RUN_MAX_MB",
        py_g(keep_days),
        py_g(max_mb)
    );
    let total: u64 = removed.iter().map(|r| r.size).sum();
    #[allow(clippy::cast_precision_loss)]
    let mb = total as f64 / 1024.0 / 1024.0;
    writeln!(
        out,
        "# pare prune: {} {} file{}, {mb:.1} MB, in {} ({limits})",
        if dry_run { "would remove" } else { "removed" },
        removed.len(),
        if removed.len() == 1 { "" } else { "s" },
        run_dir.display()
    )?;
    for r in &removed {
        let name = r
            .path
            .file_name()
            .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
        #[allow(clippy::cast_precision_loss)]
        let kb = fmt_thousands_f(r.size as f64 / 1024.0);
        writeln!(out, "{name}  {kb} KB  {}", r.reason)?;
    }
    Ok(())
}

/// A JSON value as Python's `str()` prints it in an f-string.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".to_owned(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(Value::Number(n)) if n.is_f64() => py_float_repr(n.as_f64().unwrap_or(0.0)),
        Some(other) => other.to_string(),
    }
}

/// One command-mode run, as `pare stats` sees it.
struct Row {
    started: String,
    command: String,
    bytes: f64,
    out_bytes: f64,
    cpt: f64,
    exit: String,
}

/// `pare stats`: what command mode saved, from the `.json` records next to
/// the run files: per command, what it printed against what pare printed.
///
/// # Errors
///
/// If writing to `out` fails.
pub fn stats(run_dir: &Path, out: &mut impl Write) -> io::Result<()> {
    let mut names: Vec<String> = fs::read_dir(run_dir)
        .map(|d| {
            d.flatten()
                .filter_map(|e| e.file_name().to_str().map(str::to_owned))
                .filter(|n| n.ends_with(".json"))
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    let mut rows = Vec::new();
    for name in names {
        let Ok(text) = fs::read_to_string(run_dir.join(&name)) else {
            continue;
        };
        let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let Some(out_bytes) = m.get("out_bytes").and_then(Value::as_f64) else {
            continue; // written before pare recorded its output, or still running
        };
        let cpt = m
            .get("chars_per_token")
            .and_then(Value::as_f64)
            .filter(|&c| c != 0.0)
            .unwrap_or(DEFAULT_CHARS_PER_TOKEN);
        let words: Vec<&str> = m
            .get("command")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        rows.push(Row {
            started: m
                .get("started")
                .map_or_else(String::new, |v| py_str(Some(v))),
            command: collapse_space(&words.join(" ")),
            bytes: m.get("bytes").and_then(Value::as_f64).unwrap_or(0.0),
            out_bytes,
            cpt,
            exit: py_str(m.get("exit")),
        });
    }
    if rows.is_empty() {
        return writeln!(
            out,
            "# pare stats: no command-mode runs with output sizes in {}",
            run_dir.display()
        );
    }
    #[allow(clippy::cast_possible_truncation)]
    let tok = |b: f64, cpt: f64| (b / cpt).ceil() as i64;
    let raw: i64 = rows.iter().map(|r| tok(r.bytes, r.cpt)).sum();
    let shown: i64 = rows.iter().map(|r| tok(r.out_bytes, r.cpt)).sum();
    writeln!(
        out,
        "# pare stats: {} command run{} in {}",
        rows.len(),
        if rows.len() == 1 { "" } else { "s" },
        run_dir.display()
    )?;
    #[allow(clippy::cast_precision_loss)]
    let pct = if raw != 0 {
        100.0 * (raw - shown) as f64 / raw as f64
    } else {
        0.0
    };
    let g = crate::text::glyphs();
    let (arrow, dot) = (g.arrow, g.dot);
    writeln!(
        out,
        "raw ~{} tok {arrow} printed ~{} tok {dot} saved ~{} tok ({pct:.1} %)",
        fmt_int(raw),
        fmt_int(shown),
        fmt_int((raw - shown).max(0))
    )?;
    let passthrough = rows.iter().filter(|r| r.out_bytes >= r.bytes).count();
    if passthrough > 0 {
        writeln!(
            out,
            "{passthrough} run{} fit the budget and passed through whole",
            if passthrough == 1 { "" } else { "s" }
        )?;
    }
    writeln!(out, "## largest")?;
    // Rust note: `sort_by` is stable, as Python's `sorted`, so runs that
    // saved the same keep their order (by file name).
    rows.sort_by(|a, b| (b.bytes - b.out_bytes).total_cmp(&(a.bytes - a.out_bytes)));
    for r in rows.iter().take(10) {
        writeln!(
            out,
            "{}  ~{:>8} {arrow} ~{:>6} tok  exit {}  {}",
            r.started,
            fmt_int(tok(r.bytes, r.cpt)),
            fmt_int(tok(r.out_bytes, r.cpt)),
            r.exit,
            clip(&r.command, 80)
        )?;
    }
    Ok(())
}

/// `re.sub(r"\s+", " ", s)`.
fn collapse_space(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_space = false;
    for c in s.chars() {
        if py_isspace(c) {
            if !in_space {
                out.push(' ');
            }
            in_space = true;
        } else {
            out.push(c);
            in_space = false;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_like_python() {
        assert_eq!(py_g(0.0001), "0.0001");
        assert_eq!(py_g(0.00001), "1e-05");
        assert_eq!(py_g(999_999.5), "1e+06");
        assert_eq!(py_g(123_456.0), "123456");
        assert_eq!(fmt_thousands_f(1_234_567.5), "1,234,568");
        assert_eq!(fmt_thousands_f(0.4), "0");
        assert_eq!(collapse_space("a \t b\u{1f}c"), "a b c");
        assert_eq!(utc_stamp(951_782_400.0), "2000-02-29T00:00:00Z");
    }
}
