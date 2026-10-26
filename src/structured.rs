//! Structured log lines: JSON objects (one per line, as slog, pino, zap,
//! bunyan and structlog write them) and logfmt (`key=value ...`, as slog's
//! text handler, logrus and Heroku write it). Not in Python.
//!
//! A line is recognized on its own, whatever the lines around it are:
//! there is no file-wide format to guess, so a log that mixes plain lines
//! with JSON ones (a test runner printing a service's output) is handled
//! line by line, and every other line is untouched.
//!
//! What recognition buys is a better [`clip`] for long lines. Structured
//! loggers put the time, level, message and request context first and
//! the fields that say what went wrong (`err`, `exception`, `stack`)
//! after them, so cutting a 1,500-character line at `--width` keeps the
//! context and drops the cause. Here the line keeps its start and then
//! the telling fields, and an escaped multi-line trace (`"stack":"...\n
//! at f (a.js:1:2)\n..."`) is shown as its message and the frame nearest
//! it, decoded.

use std::ops::Range;

/// One top-level field of a structured line, as byte ranges in the line.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Field {
    /// The key, without quotes.
    key: Range<usize>,
    /// The whole field: from the key to the end of the value.
    span: Range<usize>,
    /// The value as written: quoted for a string, braces for an object.
    value: Range<usize>,
}

/// Keys whose values say what went wrong, or what the line is about,
/// matched without regard to case. A clipped line keeps these.
const TELLING_KEYS: &[&str] = &[
    "err",
    "error",
    "errors",
    "exception",
    "exc_info",
    "panic",
    "fatal",
    "reason",
    "cause",
    "stack",
    "stacktrace",
    "stack_trace",
    "trace",
    "traceback",
    "msg",
    "message",
    "event",
];

fn is_telling(key: &str) -> bool {
    TELLING_KEYS.iter().any(|k| k.eq_ignore_ascii_case(key))
}

/// The top-level fields of a JSON object line, or `None` if the line is
/// not one. Values are skipped over, not parsed: a nested object or array
/// is one value, and a string's escapes are only stepped over.
fn json_fields(text: &str) -> Option<Vec<Field>> {
    let b = text.as_bytes();
    let ws = |i: &mut usize| {
        while *i < b.len() && b[*i].is_ascii_whitespace() {
            *i += 1;
        }
    };
    // The end of the string starting at `i` (a quote), past the closing quote.
    let string_end = |mut i: usize| -> Option<usize> {
        i += 1;
        while i < b.len() {
            match b[i] {
                b'\\' => i += 2,
                b'"' => return Some(i + 1),
                _ => i += 1,
            }
        }
        None
    };
    let mut i = 0;
    ws(&mut i);
    if b.get(i) != Some(&b'{') {
        return None;
    }
    i += 1;
    let mut out = Vec::new();
    loop {
        ws(&mut i);
        match b.get(i)? {
            b'}' if out.is_empty() => break,
            b'"' => {}
            _ => return None,
        }
        let start = i;
        let key_end = string_end(i)?;
        let key = start + 1..key_end - 1;
        i = key_end;
        ws(&mut i);
        if b.get(i) != Some(&b':') {
            return None;
        }
        i += 1;
        ws(&mut i);
        let vstart = i;
        match b.get(i)? {
            b'"' => i = string_end(i)?,
            b'{' | b'[' => {
                // Matching brackets, stepping over strings.
                let mut depth = 0usize;
                loop {
                    match b.get(i)? {
                        b'"' => {
                            i = string_end(i)?;
                            continue;
                        }
                        b'{' | b'[' => depth += 1,
                        b'}' | b']' => {
                            depth -= 1;
                            if depth == 0 {
                                i += 1;
                                break;
                            }
                        }
                        _ => {}
                    }
                    i += 1;
                }
            }
            _ => {
                // A number, true, false or null.
                while i < b.len() && !matches!(b[i], b',' | b'}') && !b[i].is_ascii_whitespace() {
                    i += 1;
                }
                if i == vstart {
                    return None;
                }
            }
        }
        out.push(Field {
            key,
            span: start..i,
            value: vstart..i,
        });
        ws(&mut i);
        match b.get(i)? {
            b',' => i += 1,
            b'}' => break,
            _ => return None,
        }
    }
    // Only whitespace may follow the closing brace.
    if text[i + 1..].trim().is_empty() {
        Some(out)
    } else {
        None
    }
}

/// The fields of a logfmt line (`time=... level=info msg="a b" k=v`), or
/// `None` if the line is not one: every space-separated part must be a
/// `key=value`, and there must be at least three.
fn logfmt_fields(text: &str) -> Option<Vec<Field>> {
    let b = text.as_bytes();
    let mut i = 0;
    let mut out = Vec::new();
    loop {
        while i < b.len() && b[i] == b' ' {
            i += 1;
        }
        if i == b.len() {
            break;
        }
        let start = i;
        if !(b[i].is_ascii_alphabetic() || b[i] == b'_') {
            return None;
        }
        while i < b.len() && (b[i].is_ascii_alphanumeric() || matches!(b[i], b'_' | b'.' | b'-')) {
            i += 1;
        }
        let key = start..i;
        if b.get(i) != Some(&b'=') {
            return None;
        }
        i += 1;
        let vstart = i;
        if b.get(i) == Some(&b'"') {
            i += 1;
            loop {
                match b.get(i)? {
                    b'\\' => i += 2,
                    b'"' => {
                        i += 1;
                        break;
                    }
                    _ => i += 1,
                }
            }
            if i < b.len() && b[i] != b' ' {
                return None;
            }
        } else {
            while i < b.len() && b[i] != b' ' {
                i += 1;
            }
        }
        out.push(Field {
            key,
            span: start..i,
            value: vstart..i,
        });
    }
    (out.len() >= 3).then_some(out)
}

fn fields(text: &str) -> Option<Vec<Field>> {
    match text.trim_start().as_bytes().first() {
        Some(b'{') => json_fields(text),
        Some(c) if c.is_ascii_alphabetic() || *c == b'_' => logfmt_fields(text),
        _ => None,
    }
}

/// A quoted value's text with its escapes decoded (`\n`, `\t`, `\"`,
/// `\\`, `\uXXXX`, ...); JSON's and Go's `strconv.Quote` agree on the
/// common ones. Anything else is returned as it is.
fn unquote(value: &str) -> String {
    let Some(inner) = value.strip_prefix('"').and_then(|v| v.strip_suffix('"')) else {
        return value.to_owned();
    };
    let mut out = String::with_capacity(inner.len());
    let mut chars = inner.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('n') => out.push('\n'),
            Some('t') => out.push('\t'),
            Some('r') => out.push('\r'),
            Some('u') => {
                let hex: String = chars.by_ref().take(4).collect();
                if let Some(ch) = u32::from_str_radix(&hex, 16).ok().and_then(char::from_u32) {
                    out.push(ch);
                }
            }
            Some(other) => out.push(other), // \" \\ \/ and the rest: the character itself
            None => out.push('\\'),
        }
    }
    out
}

/// A multi-line trace as two lines: its message, and the frame nearest the
/// message, which is where it was raised:
///
/// - Python (`Traceback (most recent call last):`): the last line and the
///   last `File "..."` line;
/// - JavaScript, Java, .NET (`at f (file:1:2)` frames): the first line and
///   the first `at` line;
/// - Go (`goroutine 1 [running]:`): the function and file after the last
///   `panic(` frame, else the first frame outside the runtime.
///
/// Anything else: its first two lines.
fn trace_digest(trace: &str) -> Vec<String> {
    let lines: Vec<&str> = trace
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    let Some(&first) = lines.first() else {
        return Vec::new();
    };
    if first.starts_with("Traceback") {
        let frame = lines.iter().rev().find(|l| l.starts_with("File \""));
        let last = lines[lines.len() - 1];
        return frame
            .map(|f| (*f).to_owned())
            .into_iter()
            .chain([last.to_owned()])
            .collect();
    }
    if let Some(at) = lines.iter().find(|l| l.starts_with("at ")) {
        return vec![first.to_owned(), (*at).to_owned()];
    }
    if first.starts_with("goroutine ") {
        // Frames are pairs: a function line, then its `file:line` line.
        let after_panic = lines
            .iter()
            .rposition(|l| l.starts_with("panic("))
            .map(|p| p + 2);
        let start = after_panic.unwrap_or_else(|| {
            (1..lines.len())
                .step_by(2)
                .find(|&k| !lines[k].starts_with("runtime"))
                .unwrap_or(1)
        });
        // A function line's arguments are pointers and offsets: `main.charge(…)`.
        let short = |l: &str| match l.find('(') {
            Some(p) if l.ends_with(')') => {
                format!("{}({})", &l[..p], crate::text::glyphs().ellipsis)
            }
            _ => l.to_owned(),
        };
        return lines.iter().skip(start).take(2).map(|l| short(l)).collect();
    }
    lines.iter().take(2).map(|l| (*l).to_owned()).collect()
}

/// How a telling field is shown: as written, or for a value holding an
/// escaped trace (a string with `\n`, or an object with such a `stack`),
/// the key and the trace's digest, decoded.
fn show_field(text: &str, f: &Field) -> String {
    let span = &text[f.span.clone()];
    let value = &text[f.value.clone()];
    let trace = if value.starts_with('"') && value.contains("\\n") {
        Some(unquote(value))
    } else if value.starts_with('{') {
        json_fields(value).and_then(|inner| {
            inner
                .iter()
                .find(|g| {
                    let k = &value[g.key.clone()];
                    k.eq_ignore_ascii_case("stack") || k.eq_ignore_ascii_case("stacktrace")
                })
                .map(|g| &value[g.value.clone()])
                .filter(|v| v.contains("\\n"))
                .map(unquote)
        })
    } else {
        None
    };
    match trace {
        Some(t) => {
            let key = &text[f.span.start..f.value.start];
            let g = crate::text::glyphs();
            format!(
                "{key}{}{}{}",
                g.open,
                trace_digest(&t).join(&format!(" {} ", g.newline)),
                g.close
            )
        }
        None => span.to_owned(),
    }
}

/// `text` cut to about `width` characters keeping its start and then its
/// telling fields (see the module docs), with `…(+Nc)` at the end for the
/// N characters left out. `None` when the line is not structured, has no
/// telling field past the plain cut, or would come out no shorter, so
/// that [`crate::text::clip`] cuts it as usual.
///
/// ```
/// use pare::structured::clip;
/// let line = format!(
///     r#"time=2026-10-07T09:00:00Z level=ERROR msg="order failed" req={} err="db: connection refused""#,
///     "x".repeat(150)
/// );
/// assert_eq!(
///     clip(&line, 120).unwrap(),
///     r#"time=2026-10-07T09:00:00Z level=ERROR msg="order failed" … err="db: connection refused"…(+156c)"#
/// );
/// ```
#[must_use]
pub fn clip(text: &str, width: i64) -> Option<String> {
    let width = usize::try_from(width).ok()?;
    let len = text.chars().count();
    if len <= width || width < 40 {
        return None;
    }
    let fields = fields(text)?;
    // A byte offset as a character count (the same for ASCII, the usual case).
    let ascii = text.is_ascii();
    let char_at = |byte: usize| {
        if ascii {
            byte
        } else {
            text[..byte].chars().count()
        }
    };
    // Telling fields the plain cut would lose.
    let cut = width - 1;
    let telling: Vec<&Field> = fields
        .iter()
        .filter(|f| is_telling(&text[f.key.clone()]) && char_at(f.span.end) > cut)
        .collect();
    if telling.is_empty() {
        return None;
    }
    let tail: Vec<String> = telling.iter().map(|f| show_field(text, f)).collect();
    // The telling part gets up to three quarters of the width; the start
    // of the line gets the rest, in whole fields.
    let sep = if text.trim_start().starts_with('{') {
        ","
    } else {
        " "
    };
    let tail_text = tail.join(sep);
    let tail_room = (width * 3 / 4).min(tail_text.chars().count());
    let head_room = width.saturating_sub(tail_room + 3);
    let mut head_end = 0;
    for f in &fields {
        if char_at(f.span.end) > head_room || telling.iter().any(|t| t.span == f.span) {
            break;
        }
        head_end = f.span.end;
    }
    if head_end == 0 {
        // Not even the first field fits: cut inside it.
        head_end = text
            .char_indices()
            .nth(head_room)
            .map_or(text.len(), |(b, _)| b);
    }
    let head = text[..head_end].trim_end_matches([',', ' ']);
    let tail_cut: String = tail_text.chars().take(tail_room).collect();
    let shown = head.chars().count() + tail_cut.chars().count();
    let left_out = len.saturating_sub(shown);
    if left_out == 0 {
        return None;
    }
    let e = crate::text::glyphs().ellipsis;
    let out = if sep == "," {
        format!("{head},{e}{tail_cut}{e}(+{left_out}c)")
    } else {
        format!("{head} {e} {tail_cut}{e}(+{left_out}c)")
    };
    (out.chars().count() < len).then_some(out)
}

/// The level of a JSON line with a numeric `"level"` (pino, bunyan:
/// 10 trace, 20 debug, 30 info, 40 warn, 50 error, 60 fatal), on pare's
/// scale (0 debug ... 4 fatal), or `None`.
///
/// ```
/// use pare::structured::numeric_level;
/// assert_eq!(numeric_level(r#"{"level":50,"time":1,"msg":"job failed"}"#), Some(3));
/// assert_eq!(numeric_level(r#"{"level":30,"msg":"ok"}"#), Some(1));
/// assert_eq!(numeric_level(r#"{"level":35}"#), None);
/// assert_eq!(numeric_level("level=50 a=1 b=2"), None);
/// ```
#[must_use]
pub fn numeric_level(text: &str) -> Option<u8> {
    if !text.trim_start().starts_with('{') {
        return None;
    }
    // Cheap test first: this runs on every line.
    let head = &text.as_bytes()[..text.len().min(200)];
    memchr::memmem::find(head, b"\"level\"")?;
    let fields = json_fields(text)?;
    let f = fields
        .iter()
        .take(8)
        .find(|f| &text[f.key.clone()] == "level")?;
    match &text[f.value.clone()] {
        "10" | "20" => Some(0),
        "30" => Some(1),
        "40" => Some(2),
        "50" => Some(3),
        "60" => Some(4),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn keys(text: &str) -> Vec<&str> {
        fields(text)
            .unwrap()
            .into_iter()
            .map(|f| &text[f.key])
            .collect()
    }

    #[test]
    fn recognizes_json_and_logfmt() {
        assert_eq!(
            keys(r#"{"a":1, "b":"x,}\"y", "c":{"d":[1,{"e":"}"}]}, "f":null}"#),
            ["a", "b", "c", "f"]
        );
        assert_eq!(
            keys(r#"time=1 level=info msg="a \"b\" c" k.v=2"#),
            ["time", "level", "msg", "k.v"]
        );
        for not in [
            "{not json}",
            r#"{"a":1} trailing"#,
            r#"{"a":1"#,
            "a=1 b=2",          // too few fields for logfmt
            "a=1 b=2 and more", // a bare word
            "INFO a=1 b=2 c=3",
            "",
        ] {
            assert!(fields(not).is_none(), "{not:?}");
        }
    }

    #[test]
    fn traces_become_message_and_frame() {
        let py = "Traceback (most recent call last):\n  File \"a.py\", line 3, in main\n    f()\n  File \"a.py\", line 1, in f\n    x = {}['k']\nKeyError: 'k'";
        assert_eq!(
            trace_digest(py),
            [r#"File "a.py", line 1, in f"#, "KeyError: 'k'"]
        );
        let js =
            "TypeError: x is undefined\n    at f (file:///a.js:4:11)\n    at g (file:///a.js:9:2)";
        assert_eq!(
            trace_digest(js),
            ["TypeError: x is undefined", "at f (file:///a.js:4:11)"]
        );
        let go = "goroutine 1 [running]:\nruntime/debug.Stack()\n\t/go/src/runtime/debug/stack.go:26\nmain.recoverer()\n\t/p/main.go:114\npanic({0x1, 0x2})\n\t/go/src/runtime/panic.go:792\nmain.charge(0x1)\n\t/p/main.go:67\nmain.handler()\n\t/p/main.go:91";
        assert_eq!(trace_digest(go), ["main.charge(…)", "/p/main.go:67"]);
    }

    #[test]
    fn clips_keep_the_cause() {
        let ctx = "y".repeat(120);
        let json = format!(
            r#"{{"level":50,"time":1,"ctx":"{ctx}","err":{{"type":"TypeError","message":"m","stack":"TypeError: m\n    at f (a.js:1:2)\n    at g (a.js:3:4)"}},"msg":"job failed"}}"#
        );
        let got = clip(&json, 200).unwrap();
        assert!(
            got.starts_with(r#"{"level":50,"time":1,…"err":‹TypeError: m ⏎ at f (a.js:1:2)›,"msg":"job failed"…(+"#),
            "{got}"
        );
        // The cause already shows, or the line is plain: the usual cut.
        let early = format!(r#"{{"err":"boom","pad":"{}"}}"#, "z".repeat(300));
        assert_eq!(clip(&early, 200), None);
        assert_eq!(clip(&format!("plain {}", "w ".repeat(200)), 200), None);
        assert_eq!(clip(r#"{"a":1}"#, 200), None);
    }

    #[test]
    fn unquotes_like_json() {
        assert_eq!(unquote(r#""a\n\tb \"c\" \\ é""#), "a\n\tb \"c\" \\ é");
        assert_eq!(unquote("bare"), "bare");
    }
}
