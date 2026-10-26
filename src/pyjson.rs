//! JSON as Python's `json.dump(value, f, indent=2)` writes it, for the
//! `.json` record next to a run file (command mode writes it, `pare prune`
//! rewrites it, `pare stats` reads it).
//!
//! Parsing is `serde_json`'s; writing is done here, because the files must
//! be byte for byte what the Python version writes, and Python's defaults
//! differ from `serde_json`'s: every non-ASCII character is escaped
//! (`ensure_ascii`), and floats are written as Python's `repr()` writes
//! them (`2.0`, `1e-05`, `1e+16`).

use std::fmt::Write;

use serde_json::Value;

/// Python's `repr()` of a float: the shortest digits that read back to
/// the same value, in fixed notation for exponents from -4 to 15 and in
/// scientific notation (two-digit exponent with a sign) otherwise.
///
/// ```
/// use pare::pyjson::py_float_repr;
/// assert_eq!(py_float_repr(2.0), "2.0");
/// assert_eq!(py_float_repr(0.1), "0.1");
/// assert_eq!(py_float_repr(1e-5), "1e-05");
/// assert_eq!(py_float_repr(1.5e16), "1.5e+16");
/// assert_eq!(py_float_repr(123456.75), "123456.75");
/// ```
#[must_use]
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity" } else { "-Infinity" }.to_owned();
    }
    // Rust note: `{:e}` prints the shortest round-trip digits in scientific
    // notation ("1.2345e-5"), the same digits Python's repr uses; only the
    // layout differs, and that is rebuilt here.
    let sci = format!("{:e}", x.abs());
    let (mantissa, exp) = sci.split_once('e').expect("{:e} has an exponent");
    let exp: i32 = exp.parse().expect("an integer exponent");
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if x.is_sign_negative() { "-" } else { "" };
    if (-4..16).contains(&exp) {
        // Rust note: `unsigned_abs` and `try_from` keep the arithmetic in
        // unsigned types without casts that could wrap; the exponent is
        // known to be small here.
        let n = digits.len();
        let int_len = usize::try_from(exp + 1).unwrap_or(0); // digits before the point
        let body = if exp < 0 {
            format!("0.{}{digits}", "0".repeat(exp.unsigned_abs() as usize - 1))
        } else if int_len >= n {
            format!("{digits}{}.0", "0".repeat(int_len - n))
        } else {
            let (int, frac) = digits.split_at(int_len);
            format!("{int}.{frac}")
        };
        format!("{sign}{body}")
    } else {
        let mantissa = if digits.len() == 1 {
            digits
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        format!(
            "{sign}{mantissa}e{}{:02}",
            if exp < 0 { '-' } else { '+' },
            exp.abs()
        )
    }
}

/// `json.dumps(value, indent=2)`.
///
/// ```
/// use pare::pyjson::dumps;
/// let v = serde_json::json!({"cmd": ["make", "é"], "n": 3, "s": 0.5, "e": []});
/// assert_eq!(dumps(&v), "{\n  \"cmd\": [\n    \"make\",\n    \"\\u00e9\"\n  ],\n  \"n\": 3,\n  \"s\": 0.5,\n  \"e\": []\n}");
/// ```
#[must_use]
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, 0);
    out
}

fn write_value(out: &mut String, value: &Value, level: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if n.is_f64() {
                out.push_str(&py_float_repr(n.as_f64().unwrap_or(0.0)));
            } else {
                out.push_str(&n.to_string());
            }
        }
        Value::String(s) => write_string(out, s),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(map) if map.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push('[');
            for (k, item) in items.iter().enumerate() {
                out.push_str(if k == 0 { "\n" } else { ",\n" });
                indent(out, level + 1);
                write_value(out, item, level + 1);
            }
            out.push('\n');
            indent(out, level);
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (k, (key, item)) in map.iter().enumerate() {
                out.push_str(if k == 0 { "\n" } else { ",\n" });
                indent(out, level + 1);
                write_string(out, key);
                out.push_str(": ");
                write_value(out, item, level + 1);
            }
            out.push('\n');
            indent(out, level);
            out.push('}');
        }
    }
}

fn indent(out: &mut String, level: usize) {
    out.extend(std::iter::repeat_n(' ', 2 * level));
}

/// A string with Python's escapes: `\"`, `\\`, `\n`, `\r`, `\t`, `\b`,
/// `\f`, and `\uXXXX` for other control characters and anything outside
/// ASCII (a surrogate pair beyond U+FFFF).
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' | '\u{7f}' => out.push(c),
            _ => {
                // Rust note: `encode_utf16` gives one unit for a character
                // in the Basic Multilingual Plane and a surrogate pair for
                // one beyond it, which is what `\u` escapes are made of.
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn like_python() {
        // json.dumps({...}, indent=2) in Python 3.13.
        let v = serde_json::json!({"a": [1, 2.0, 1e-05, 1e16, "é\u{1}\u{1F600}"], "b": [], "c": {}, "d": null, "e": true});
        let want = "{\n  \"a\": [\n    1,\n    2.0,\n    1e-05,\n    1e+16,\n    \"\\u00e9\\u0001\\ud83d\\ude00\"\n  ],\n  \"b\": [],\n  \"c\": {},\n  \"d\": null,\n  \"e\": true\n}";
        assert_eq!(dumps(&v), want);
        for (x, s) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (12.34, "12.34"),
            (0.0001, "0.0001"),
            (1e15, "1000000000000000.0"),
            (-2.5e-7, "-2.5e-07"),
        ] {
            assert_eq!(py_float_repr(x), s);
        }
    }
}
