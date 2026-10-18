//! End-to-end tests of the `pare` binary: run it as a process, feed it
//! input, check its output, exit status and run file.
//!
//! The byte-for-byte comparison with the Python version is
//! `bench/parity.py`; these tests check the same behaviour without Python,
//! and cover the run file, which parity (always `--no-tee`) does not.

// Rust note: files in tests/ are integration tests. Each is compiled as its
// own crate that can only use the public API, or, as here, run the binary.
// Cargo builds the binary first and passes its path in the
// CARGO_BIN_EXE_<name> variable, so no extra test dependency is needed.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

fn pare(args: &[&str], input: &[u8]) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_pare"))
        .args(args)
        .env_remove("PARE_VOCAB")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the pare binary runs");
    // Rust note: `take()` moves the pipe out of the child, so it is closed
    // (dropped) at the end of this statement and pare sees end of input.
    child.stdin.take().unwrap().write_all(input).unwrap();
    child.wait_with_output().unwrap()
}

/// A fresh, empty directory for one test, removed when the guard drops.
struct TempDir(PathBuf);

impl TempDir {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("pare-it-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        TempDir(dir)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn entries(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[test]
fn empty_input() {
    let out = pare(&["--no-tee"], b"");
    assert!(out.status.success());
    assert_eq!(out.stdout, b"# pare: empty input\n");
}

#[test]
fn input_that_fits_is_passed_through_cleaned() {
    let input =
        b"\x1b[32mok\x1b[0m one\r\nprogress 10%\rprogress done\ntrailing   \nlast, no newline";
    let out = pare(&["--no-tee"], input);
    assert!(out.status.success());
    assert_eq!(
        out.stdout,
        b"ok one\nprogress done\ntrailing\nlast, no newline\n"
    );
}

#[test]
fn run_file_is_named_by_content_hash() {
    let dir = TempDir::new("run-dir");
    let input = b"hello\n";
    let out = pare(&["--run-dir", dir.path().to_str().unwrap()], input);
    assert!(out.status.success());
    // sha256(b"hello\n") starts 5891b5b522d5; the spool was renamed, not left behind.
    assert_eq!(entries(dir.path()), ["5891b5b522d5.log"]);
    assert_eq!(
        fs::read(dir.path().join("5891b5b522d5.log")).unwrap(),
        input
    );
}

#[test]
fn explicit_run_file() {
    let dir = TempDir::new("run-file");
    let target = dir.path().join("sub").join("x.log");
    let out = pare(&["--run-file", target.to_str().unwrap()], b"a\nb\n");
    assert!(out.status.success());
    assert_eq!(fs::read(&target).unwrap(), b"a\nb\n");
    assert_eq!(entries(&dir.path().join("sub")), ["x.log"]);
}

#[test]
fn empty_input_leaves_no_run_file() {
    let dir = TempDir::new("empty");
    let out = pare(&["--run-dir", dir.path().to_str().unwrap()], b"");
    assert!(out.status.success());
    assert!(entries(dir.path()).is_empty());
}

#[test]
fn unported_work_exits_70_and_says_so() {
    let big = "ERROR something\n".repeat(2000);
    for args in [
        vec!["--no-tee"],
        vec!["--no-tee", "--follow"],
        vec!["--no-tee", "--only", "T1"],
        vec!["--stats"],
        vec!["--no-tee", "--", "true"],
    ] {
        let out = pare(&args, big.as_bytes());
        assert_eq!(out.status.code(), Some(70), "{args:?}");
        assert!(
            String::from_utf8_lossy(&out.stderr).contains("not yet ported to Rust"),
            "{args:?}"
        );
    }
}

#[test]
fn usage_errors_match_python() {
    for (args, msg) in [
        (vec!["--dry-run"], "pare: --dry-run goes with --prune"),
        (vec!["--"], "pare: -- must be followed by a command to run"),
        (
            vec!["--chars-per-token", "0"],
            "pare: --chars-per-token must be positive",
        ),
        (
            vec!["--follow", "--every", "0"],
            "pare: --every must be at least 1",
        ),
    ] {
        let out = pare(&args, b"");
        assert_eq!(out.status.code(), Some(1), "{args:?}");
        assert_eq!(String::from_utf8_lossy(&out.stderr).trim_end(), msg);
    }
}

#[test]
fn version() {
    let out = pare(&["--version"], b"");
    assert!(out.status.success());
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim_end(),
        format!("pare {}", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn dump_has_python_format() {
    let dir = TempDir::new("dump");
    let dump = dir.path().join("d.tsv");
    let mut child = Command::new(env!("CARGO_BIN_EXE_pare"))
        .args(["--no-tee"])
        .env("PARE_DUMP", &dump)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"ERROR a 1\n  detail\n\nERROR a 2\n")
        .unwrap();
    assert!(child.wait().unwrap().success());
    let text = fs::read_to_string(&dump).unwrap();
    // What proto/pare.py writes for the same input.
    let want = [
        "# pare dump v1",
        // no, record, depth, severity, blank, template, score, sizes
        "L\t1\t0\t0\t3\t0\tT1\t4.500000\t13\t9",
        "L\t2\t0\t1\t-1\t0\tT1\t2.450000\t12\t6",
        "L\t3\t-1\t0\t-1\t1\t-\t0.000000\t4\t0",
        "L\t4\t1\t0\t3\t0\tT1\t4.500000\t13\t9",
        // id, records, lines, total, best severity, marker, salience, vocab,
        // exemplar line, levels, text
        "T\tT1\t2\t3\t2\t3\tE\t5.500000\t4.500000\t1\t3:2\tERROR a <NUM>",
    ];
    assert_eq!(text.lines().collect::<Vec<_>>(), want);
}

#[test]
fn dump_vocab_is_pythons() {
    let golden =
        |name: &str| fs::read(format!("{}/tests/data/{name}", env!("CARGO_MANIFEST_DIR"))).unwrap();
    let out = pare(&["--dump-vocab"], b"");
    assert!(out.status.success());
    assert_eq!(out.stdout, golden("vocab.txt"));
    let example = concat!(env!("CARGO_MANIFEST_DIR"), "/proto/vocab.example");
    let out = pare(&["--vocab", example, "--dump-vocab"], b"");
    assert_eq!(out.stdout, golden("vocab-example.txt"));
}

#[test]
fn bad_vocab_files_are_usage_errors() {
    let dir = TempDir::new("vocab");
    let bad = dir.path().join("bad.vocab");
    fs::write(&bad, "[error]\nok\n[level-warn]\n/W/\n").unwrap();
    let out = pare(&["--vocab", bad.to_str().unwrap(), "--dump-vocab"], b"");
    assert_eq!(out.status.code(), Some(1));
    let want = format!(
        "pare: {}:4: level classes take plain tokens, not regexes: '/W/'\n",
        bad.display()
    );
    assert_eq!(String::from_utf8_lossy(&out.stderr), want);
    let missing = dir.path().join("missing");
    let out = pare(&["--vocab", missing.to_str().unwrap()], b"");
    let want = format!(
        "pare: cannot read vocab file {}: No such file or directory\n",
        missing.display()
    );
    assert_eq!(String::from_utf8_lossy(&out.stderr), want);
}
