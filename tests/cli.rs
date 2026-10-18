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
        // No automatic pruning, so a run directory holds only what the test made.
        .env("PARE_RUN_PRUNE", "off")
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
    let out = pare(&["--no-tee", "--follow"], b"ERROR something\n");
    assert_eq!(out.status.code(), Some(70));
    assert!(String::from_utf8_lossy(&out.stderr).contains("not yet ported to Rust"));
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

#[test]
fn summaries_are_pythons() {
    // tests/data/summary/: proto/pare.py's whole output for corpus logs at
    // a few budgets (bench/gen_fixtures.py writes them).
    let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/data/summary");
    let cases = fs::read_to_string(format!("{dir}/cases.tsv")).unwrap();
    let mut n = 0;
    for row in cases.lines().skip(1) {
        let f: Vec<&str> = row.split('\t').collect();
        let input = fs::read(format!("{}/corpus/{}", env!("CARGO_MANIFEST_DIR"), f[0])).unwrap();
        let mut args = vec!["--no-tee"];
        args.extend(f[1].split(' '));
        let out = pare(&args, &input);
        assert!(out.status.success(), "{row}");
        let want = fs::read(format!("{dir}/{}", f[2])).unwrap();
        assert!(
            out.stdout == want,
            "{row}: output differs from Python's\n--- python\n{}\n--- rust\n{}",
            String::from_utf8_lossy(&want),
            String::from_utf8_lossy(&out.stdout)
        );
        n += 1;
    }
    assert_eq!(n, 15);
}

#[test]
fn summary_fits_the_budget_and_names_the_run_file() {
    let input =
        "INFO step\n".repeat(3000) + "ERROR disk full on /var\n" + &"INFO step\n".repeat(100);
    // (At --budget 0 the Python version, and so the port, overshoots: its
    // last-resort cut `[: budget_bytes - 1]` keeps all but one byte.)
    for (budget, limit) in [("50", 140), ("300", 840), ("600", 1680), ("2000", 5600)] {
        let out = pare(&["--no-tee", "--budget", budget], input.as_bytes());
        assert!(out.status.success());
        assert!(
            out.stdout.len() <= limit.max(1),
            "budget {budget}: {} bytes",
            out.stdout.len()
        );
    }
    let text = String::from_utf8(pare(&["--no-tee"], input.as_bytes()).stdout).unwrap();
    assert!(text.starts_with("# pare: 3,101 lines"), "{text}");
    assert!(text.contains("ERROR disk full on /var") && text.contains("## raw: (not saved)"));

    let dir = TempDir::new("summary-run");
    let out = pare(
        &["--run-dir", dir.path().to_str().unwrap()],
        input.as_bytes(),
    );
    let text = String::from_utf8(out.stdout).unwrap();
    let run = entries(dir.path());
    assert_eq!(run.len(), 1);
    assert!(
        text.contains(&format!("## raw: {}", dir.path().join(&run[0]).display())),
        "{text}"
    );
}

/// Runs pare with a run directory and returns (status, stdout).
#[cfg(unix)]
fn pare_in(dir: &Path, args: &[&str], env: &[(&str, &str)]) -> (Option<i32>, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_pare"))
        .arg("--run-dir")
        .arg(dir)
        .args(args)
        .env_remove("PARE_VOCAB")
        .envs(env.iter().copied())
        .stdin(Stdio::null())
        .output()
        .unwrap();
    (
        out.status.code(),
        String::from_utf8_lossy(&out.stdout).into_owned(),
    )
}

#[test]
#[cfg(unix)]
fn command_mode_keeps_the_exit_code_and_records_the_run() {
    let dir = TempDir::new("command");
    let d = dir.path();
    // Output that fits passes through, with a failing status added.
    let (code, out) = pare_in(
        d,
        &["--", "sh", "-c", "echo hello; echo err >&2; exit 3"],
        &[],
    );
    assert_eq!((code, out.as_str()), (Some(3), "hello\nerr\n## exit 3\n"));
    // A summary ends its run line with the status.
    let script = "for i in $(seq 1 400); do echo \"INFO step $i\"; done; echo ERROR boom";
    let (code, out) = pare_in(d, &["--budget", "300", "--", "sh", "-c", script], &[]);
    assert_eq!(code, Some(0));
    assert!(
        out.lines().last().unwrap().ends_with(".log · exit 0"),
        "{out}"
    );
    let (code, out) = pare_in(d, &["--", "true"], &[]);
    assert_eq!(
        (code, out.as_str()),
        (Some(0), "# pare: no output · exit 0\n")
    );
    let (code, _) = pare_in(d, &["--", "/nonexistent/command"], &[]);
    assert_eq!(code, Some(127));
    // The record next to the run file, as Python writes it.
    let json: Vec<String> = entries(d)
        .into_iter()
        .filter(|n| n.ends_with(".json"))
        .collect();
    assert_eq!(json.len(), 2, "{json:?}");
    let text = fs::read_to_string(d.join("72d6e231d174.json")).unwrap();
    assert!(
        text.starts_with("{\n  \"command\": [\n    \"sh\",\n    \"-c\",\n"),
        "{text}"
    );
    assert!(
        text.contains("\n  \"exit\": 3,\n") && text.contains("\n  \"chars_per_token\": 2.8\n}\n"),
        "{text}"
    );
    // --stats reads the records back.
    let (code, out) = pare_in(d, &["--stats"], &[]);
    assert_eq!(code, Some(0));
    assert!(
        out.starts_with("# pare --stats: 2 command runs in "),
        "{out}"
    );
    assert!(
        out.contains("1 run fit the budget and passed through whole\n## largest\n"),
        "{out}"
    );
}

#[test]
#[cfg(unix)]
fn prune_removes_old_and_excess_run_files() {
    use std::time::{Duration, SystemTime};
    let dir = TempDir::new("prune");
    let d = dir.path();
    let day = Duration::from_secs(86400);
    for (name, age) in [
        ("aaaaaaaaaaaa.log", 20),
        ("bbbbbbbbbbbb.log", 3),
        ("cccccccccccc.log", 0),
        ("keep.log", 30),
    ] {
        let f = fs::File::create(d.join(name)).unwrap();
        f.set_len(300_000).unwrap();
        f.set_modified(SystemTime::now() - day * age).unwrap();
    }
    fs::write(d.join("aaaaaaaaaaaa.json"), "{\"exit\": 1}\n").unwrap();
    let (_, out) = pare_in(d, &["--prune", "--dry-run"], &[]);
    assert!(
        out.starts_with("# pare --prune: would remove 1 file, 0.3 MB, in "),
        "{out}"
    );
    assert!(out.ends_with("(keep 14 days, at most 500 MB; $PARE_RUN_KEEP_DAYS, $PARE_RUN_MAX_MB)\naaaaaaaaaaaa.log  293 KB  older than 14 days\n"), "{out}");
    let (_, out) = pare_in(d, &["--prune"], &[("PARE_RUN_MAX_MB", "0.5")]);
    assert!(
        out.contains("bbbbbbbbbbbb.log  293 KB  over 0.5 MB in all"),
        "{out}"
    );
    assert_eq!(
        entries(d),
        ["aaaaaaaaaaaa.json", "cccccccccccc.log", "keep.log"]
    );
    let meta = fs::read_to_string(d.join("aaaaaaaaaaaa.json")).unwrap();
    assert!(
        meta.starts_with("{\n  \"exit\": 1,\n  \"pruned\": \"") && meta.ends_with("Z\"\n}\n"),
        "{meta}"
    );
}
