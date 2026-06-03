//! Python-compatible JSON serialisation.
//!
//! Python's `json.dumps(data, sort_keys=True)` applies `ensure_ascii=True`
//! by default, which escapes every non-ASCII character to `\uNNNN` (or
//! `\uXXXX\uYYYY` surrogate pairs for code points > U+FFFF).
//!
//! Rust's `serde_json` writes raw UTF-8.  To produce byte-identical JSON
//! we post-process the serde output and re-escape non-ASCII characters.

/// Serialise a `serde_json::Value` to a JSON string that is byte-identical
/// to Python's `json.dumps(obj, sort_keys=True)`.
///
/// Differences handled:
/// - Non-ASCII characters are escaped to `\uNNNN`.
/// - Object keys are sorted (guaranteed by BTreeMap + serde_json).
/// - Spaces after `:` and `,` match Python's default compact separator
///   (`, ` and `: ` — serde_json uses `separators=(',', ':')` i.e. NO
///   spaces, but Python adds spaces).
pub fn python_json_dumps(value: &serde_json::Value) -> String {
    // Python's json.dumps with sort_keys=True uses separators=(', ', ': ')
    // which adds a space after : and after ,
    // serde_json::to_string uses no spaces.
    // We need to build the JSON string manually.
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &serde_json::Value, out: &mut String) {
    match value {
        serde_json::Value::Null => out.push_str("null"),
        serde_json::Value::Bool(b) => {
            if *b {
                out.push_str("true");
            } else {
                out.push_str("false");
            }
        }
        serde_json::Value::Number(n) => {
            out.push_str(&n.to_string());
        }
        serde_json::Value::String(s) => {
            write_python_string(s, out);
        }
        serde_json::Value::Array(arr) => {
            out.push('[');
            for (i, v) in arr.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(v, out);
            }
            out.push(']');
        }
        serde_json::Value::Object(obj) => {
            out.push('{');
            // serde_json::Map preserves insertion order; for sorted keys
            // we rely on the caller using BTreeMap → Value conversion.
            // serde_json::Map from BTreeMap is already sorted.
            let mut first = true;
            for (k, v) in obj.iter() {
                if !first {
                    out.push_str(", ");
                }
                first = false;
                write_python_string(k, out);
                out.push_str(": ");
                write_value(v, out);
            }
            out.push('}');
        }
    }
}

/// Write a JSON-encoded string matching Python's `ensure_ascii=True`.
///
/// Escapes:
/// - `"` → `\"`
/// - `\` → `\\`
/// - Control chars (< 0x20) → `\uNNNN` or `\n`, `\r`, `\t`, etc.
/// - Non-ASCII (> 0x7F) → `\uNNNN` or surrogate pairs.
fn write_python_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\x08' => out.push_str("\\b"),
            '\x0C' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                // Other control characters
                write!(out, "\\u{:04x}", c as u32).unwrap();
            }
            c if c.is_ascii() => {
                out.push(c);
            }
            c => {
                // Non-ASCII: encode as \uNNNN (or surrogate pair for > U+FFFF)
                let cp = c as u32;
                if cp <= 0xFFFF {
                    write!(out, "\\u{:04x}", cp).unwrap();
                } else {
                    // Surrogate pair
                    let adjusted = cp - 0x10000;
                    let high = 0xD800 + (adjusted >> 10);
                    let low = 0xDC00 + (adjusted & 0x3FF);
                    write!(out, "\\u{:04x}\\u{:04x}", high, low).unwrap();
                }
            }
        }
    }
    out.push('"');
}

use std::fmt::Write;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_ascii_string() {
        let v = json!("hello");
        assert_eq!(python_json_dumps(&v), "\"hello\"");
    }

    #[test]
    fn test_em_dash_escape() {
        // Python: json.dumps("test — dash") → '"test \\u2014 dash"'
        let v = json!("test \u{2014} dash");
        assert_eq!(python_json_dumps(&v), "\"test \\u2014 dash\"");
    }

    #[test]
    fn test_object_spacing() {
        // Python: json.dumps({"a": 1, "b": 2}, sort_keys=True)
        //       → '{"a": 1, "b": 2}'
        let v = json!({"a": 1, "b": 2});
        assert_eq!(python_json_dumps(&v), "{\"a\": 1, \"b\": 2}");
    }

    #[test]
    fn test_nested_object() {
        let v = json!({"metadata": {"block": "genesis"}, "value": 0});
        let result = python_json_dumps(&v);
        assert_eq!(
            result,
            "{\"metadata\": {\"block\": \"genesis\"}, \"value\": 0}"
        );
    }

    #[test]
    fn test_array_spacing() {
        let v = json!([1, 2, 3]);
        assert_eq!(python_json_dumps(&v), "[1, 2, 3]");
    }

    #[test]
    fn test_float_representation() {
        // Python: json.dumps(1743379200.0) → '1743379200.0'
        let v = json!(1743379200.0);
        assert_eq!(python_json_dumps(&v), "1743379200.0");
    }
}
