# Rust for C++ developers, through pare

A guide to the Rust in this repository for a reader who knows C++. It
skips what carries over (memory layout, RAII, templates, why allocation
matters) and covers what Rust does differently, each time pointing at
where pare does it and why. It grows with the port: each stage adds the
concepts its code introduces.

In the code, comments marked `// Rust note:` sit where a Rust-specific
choice is made. `grep -rn "Rust note" src tests` lists them; read in the
order of the sections below, they make a short tour.

Contents: [Tooling](#tooling) · [Crates and modules](#crates-and-modules)
· [Ownership and moves](#ownership-and-moves) · [Drop](#drop-raii-without-surprises)
· [Borrowing](#borrowing-one-writer-or-many-readers) · [Slices and lifetimes](#slices-and-lifetimes)
· [Strings](#strings-bytes-and-utf-8) · [Cow](#cow-borrow-when-you-can-own-when-you-must)
· [Errors](#errors-are-values) · [Enums and match](#enums-and-match)
· [Generics and traits](#generics-and-traits) · [Closures](#closures)
· [Iterators](#iterators) · [Collections and indices](#collections-and-indices)
· [Numbers](#numbers-no-implicit-conversions) · [Statics](#statics-and-initialisation)
· [Macros](#macros) · [Regular expressions](#regular-expressions-two-engines)
· [Generated code](#generated-code) · [I/O details](#io-details) · [Tests](#tests)
· [Performance](#performance-notes)

## Tooling

| C++ | Rust |
|---|---|
| CMake, Make, Bazel | `cargo build` (no build script for a crate like this) |
| Conan, vcpkg, git submodules | `[dependencies]` in Cargo.toml, versions locked in `Cargo.lock` |
| `-O2`, LTO flags | `[profile.release]` in Cargo.toml (`lto = true`, `codegen-units = 1`) |
| clang-format | `cargo fmt` (one style, no configuration needed) |
| clang-tidy | `cargo clippy` (pare runs it with `-D warnings`) |
| Doxygen | `cargo doc` from `///` comments; examples in them are compiled and run |
| GoogleTest, Catch2 | built in: `#[test]`, `cargo test` |
| sanitizers for UB | not needed for safe code; pare has `#![forbid(unsafe_code)]` (in Cargo.toml as `unsafe_code = "forbid"`) |

`Cargo.lock` is committed because pare is a binary: every build uses the
same dependency versions. `edition = "2024"` in Cargo.toml selects the
language edition (like `-std=c++20`, but editions interoperate: a crate
on one edition can use crates on another). `rust-version` is the oldest
compiler the crate promises to build with.

Cross-compiling and static linking are where the build pain you know from
C++ disappears: with no C dependencies, `cargo build --release --target
x86_64-unknown-linux-musl` gives a statically linked binary with no extra
packages installed. `.github/workflows/release.yml` builds Linux, macOS
and Windows binaries that way.

## Crates and modules

A *crate* is a compilation unit: a library or a binary. pare's package has
both: `src/lib.rs` (the library, all the logic) and `src/main.rs` (a
binary that calls `pare::cli::run`). There are no headers. `pub mod text;`
in lib.rs declares the module whose source is `src/text.rs`; items marked
`pub` are its interface and everything else is private to the module.
Declaration order never matters, so there are no forward declarations.

Where: `src/lib.rs`, `src/main.rs`.

## Ownership and moves

Every value has one owner. Assignment and passing by value *move*: the
source can no longer be used, and the compiler enforces it. Copies are
explicit (`.clone()`), except for small plain types that opt in to
implicit copying (`#[derive(Clone, Copy)]`, like `Budget`).

The C++ difference: a moved-from `std::string` still exists in a "valid
but unspecified" state and can be misused; a moved-from Rust value is
gone as far as the compiler is concerned.

Where:
- `Spool::persist(self, ...)` in `src/runfile.rs` takes the spool *by
  value*. Renaming the spool to the run file consumes it, so nothing can
  write to it or persist it twice afterwards: that would not compile.
- `Pass::finish(self)` in `src/pass.rs` consumes the pass and hands back
  its parts.

Pitfall: a method that takes `self` by value when it only needs to look
(`&self`) or change (`&mut self`) forces callers to give up the value.
Clippy's `needless_pass_by_value` (enabled in Cargo.toml) flags it; see
`cli::dispatch`, which borrows the command instead.

## Drop: RAII without surprises

`impl Drop` is the destructor; it runs when the owner goes out of scope,
on every path: normal return, an early `return`, an error passed up with
`?`, or a panic. Because moved-from values are not dropped, there is no
double destruction and no need for a moved-from state.

Where: `Spool` removes its file in `drop` unless it was persisted, which
replaces the Python version's `try`/`finally` cleanup. The test helper
`TempDir` in `tests/cli.rs` does the same for test directories.

Pitfalls:
- **Fields drop after `drop()` runs.** `Spool` keeps its file in an
  `Option` so `drop` can close it first (`self.file.take()`): Windows will
  not remove an open file.
- **`let _ = value;` drops immediately**, while `let _guard = value;`
  keeps it to the end of the scope. `cli::batch` keeps a `--no-tee` spool
  alive as `_temp` for exactly this reason.
- `std::process::exit` skips destructors; `main` returns an `ExitCode`
  instead.

## Borrowing: one writer or many readers

A reference is `&T` (shared, read-only, like `const T&`) or `&mut T`
(exclusive, like `T&` with a guarantee that nothing else can see the
value meanwhile). At any moment a value has any number of `&T` or exactly
one `&mut T`, never both. This one rule is what makes iterator
invalidation, aliasing bugs and data races compile errors.

Where: in `Pass::feed` (`src/pass.rs`), `self.seg.add_line(&self.rest)`
borrows two *different fields* of `self`, one mutably and one not. That
is allowed, because the compiler can see they do not overlap. A
`&mut self` method on `Pass` taking `&self.rest` would need all of
`self` exclusively while part of it is borrowed, and would not compile.
`Drain::add_to_tree` (`src/drain.rs`) borrows the tree mutably and a
cluster's tokens immutably at once for the same reason: different fields.

Pitfall: fighting the borrow checker with clones or `Rc<RefCell<...>>`.
The usual fix is structural: split the borrow by field, pass the piece
that is needed, or use indices into a vector instead of references
between elements. pare does that for records, templates and Drain's
clusters (see [Collections and indices](#collections-and-indices)).

## Slices and lifetimes

`&[u8]` and `&str` are *slices*: a pointer and a length into memory owned
by something else, like `std::span` and `std::string_view`. The
difference is that the compiler tracks how long the borrowed memory
lives (its *lifetime*) and rejects any use of a slice after the owner is
gone. A dangling `string_view` cannot be written.

Where:
- `Pass::feed` splits each chunk into lines that are slices of the chunk:
  no copies.
- `clean_line(raw: &[u8]) -> Cow<'_, str>`: the `'_` says the result may
  borrow from `raw`, so the compiler will not let it outlive `raw`.

Most of the time lifetimes are inferred and never written; they appear
in signatures only when a function returns a borrow.

## Strings, bytes and UTF-8

| Rust | C++ | note |
|---|---|---|
| `String` | `std::string` | owned, growable, **always valid UTF-8** |
| `&str` | `std::string_view` | borrowed, always valid UTF-8 |
| `Vec<u8>`, `&[u8]` | `std::vector<char>`, `std::span<const char>` | bytes, no encoding |
| `OsString`, `&OsStr` | platform strings | what the OS gives: arbitrary bytes on Unix |
| `PathBuf`, `&Path` | `std::filesystem::path` | |

Logs are bytes and not always valid UTF-8, so pare reads `&[u8]` and
decodes each line with `String::from_utf8_lossy`, which replaces invalid
sequences with U+FFFD exactly as Python's `decode("utf-8", "replace")`
does. Command-line arguments are `OsString` (`cli::run`), so an argument
that is not UTF-8 cannot crash the parser.

Pitfall, met in this port: Unicode is not one definition of "whitespace".
Python's `str.isspace()` counts U+001C to U+001F; Rust's
`char::is_whitespace()` does not. Porting `rstrip()` as `trim_end()`
would have changed the output for lines ending in those characters, so
`pyclass::py_isspace` uses Python's set. The same goes for `\w`, `\d` and
`isdigit()` (see [Generated code](#generated-code)): `py_strip` and
`py_split` are `strip()` and `split()` on Python's whitespace.

Indexing is by byte, never by character: `s[i]` does not exist for a
`str`, `s.len()` is bytes, and a slice `&s[a..b]` must start and end on
character boundaries or it panics. Python's `len(s)` is
`s.chars().count()`, which is O(n) because UTF-8 does not store it.
`text::clip` needs Python's `s[:n]` with its negative-index rules, so
`py_prefix` counts characters and finds the n-th one's byte offset with
`char_indices()`.

## Cow: borrow when you can, own when you must

`Cow<'a, str>` ("clone on write") is either a borrowed `&'a str` or an
owned `String`. A function that usually returns its input unchanged but
sometimes has to build a new string can return `Cow` and allocate only
in the second case.

Where: `clean_line`. Most log lines are valid UTF-8, with no carriage
return, no escape codes and no trailing spaces; for those it returns a
borrowed slice of the input and allocates nothing. `from_utf8_lossy` and
`Regex::replace_all` return `Cow` for the same reason. A test checks
that a plain line comes back `Borrowed`.

## Errors are values

There are no exceptions. A function that can fail returns `Result<T, E>`
(like `std::expected`); one that may have no answer returns `Option<T>`
(like `std::optional`, but there is no null to forget to check). The `?`
operator returns early with the error if there is one, so error handling
reads like the happy path:

```rust
let file = File::create(&path).map_err(|e| Error::io("cannot create ...", e))?;
```

Where: `src/error.rs` defines one `Error` enum with a variant per kind of
failure (`Usage`, `Io`, `NotPorted`). Everything returns
`error::Result<T>`, and `cli::run` is the one place that prints an error
and picks the exit status. `Display` (the `{}` format, like
`operator<<`) gives the message; `std::error::Error` makes it usable
with other error handling.

Panics (`unwrap`, `expect`, a failed bounds check) are for bugs, not for
expected failures; they unwind and run destructors, like an exception
nobody is meant to catch. pare uses `expect` only where failure means a
broken assumption (a line of 4 GiB, an invalid built-in regex).

## Enums and match

A Rust `enum` is a tagged union whose variants can carry data: closer to
`std::variant` than to a C++ `enum`. `match` takes it apart, and must
cover every variant, so adding one makes the compiler list every place
that needs to handle it.

Where: `Error` and its `match`es in `exit_status` and `Display`;
`ignore_broken_pipe` in `src/cli.rs` matches one variant with a
condition (`Error::Io { source, .. } if source.kind() == BrokenPipe`).
`Option` and `Result` are ordinary enums.

## Generics and traits

A *trait* is an interface: a set of methods a type promises. Generics
are like templates, with one big difference: the bounds are checked
where the generic code is written. `fn f<W: Write>(w: W)` can only call
what `Write` provides, and a caller passing a type that is not `Write`
gets an error at the call, not pages of instantiation errors. Generic
code is *monomorphised* (one copy per type, direct calls), as templates
are; `dyn Trait` is the explicit opt-in to a vtable.

Where:
- `Pass<W: Write>` writes the spool to a `File` in pare and to a
  `Vec<u8>` in the tests, with no virtual calls.
- `Error::io(context: impl Into<String>, ...)` accepts `&str` or
  `String`; `impl Trait` in argument position is a generic parameter
  without a name.
- `Store::for_each_raw<F: FnMut(&[u8]) -> io::Result<()>>` takes a
  closure (next section).
- `#[derive(Debug, Clone, Copy, PartialEq)]` implements standard traits
  for a type automatically.

## Closures

A closure (`|raw| { ... }`) is a lambda. Rust infers how it captures:
by reference, by mutable reference, or by value (`move |..|`). Which
traits it implements says how it may be called: `Fn` (any number of
times, shared), `FnMut` (any number of times, may change its captures),
`FnOnce` (once).

Where: `summary::passthrough` passes `Store::for_each_raw` a closure that
writes each line to `out`; it captures `out` by mutable reference, so it
is `FnMut`. Iterator adapters (`filter`, `map`, `rfind`, `zip`) take
closures too: `default_run_dir` and `clean_line` use them.

## Iterators

Rust's iterators are lazy, like C++20 ranges: adapters (`map`, `filter`,
`zip`, `take`, `enumerate`) build a pipeline that does nothing until
something consumes it (`for`, `collect`, `count`, `any`). They compile to
the loop you would have written, usually without bounds checks.

Where:
- `py_split` returns `impl Iterator<Item = &str>`: some iterator type,
  unnamed, producing slices of the input on demand. `drain_message` takes
  the first 40 with `.take(40)` and joins them; nothing else is built.
- Drain's loops use `(1..).zip(tokens)` for a counter starting at 1
  instead of a `depth += 1` at the bottom (clippy's
  `explicit_counter_loop`), and `cluster.tokens.iter_mut().zip(&tokens)`
  to update a template in place.
- `std::array::from_fn(|g| ...)` builds a fixed-size array from a closure
  (the logcat header's eight capture groups).
- `chars().peekable()` gives one character of look-ahead (`py_pattern`,
  `rules`).

## Collections and indices

`Vec<T>` is `std::vector`, `HashMap<K, V>` is `std::unordered_map`. Two
patterns from the port:

- **A tree that owns its children.** Drain's prefix tree is
  `struct Node { children: HashMap<String, Node>, ... }`: each node owns
  its children by value, inside the map's heap storage, so no pointers or
  reference counting are needed for a tree walked only from the root
  down. (A struct cannot contain itself directly, which would make it
  infinitely large; through a `HashMap`, `Vec` or `Box` it can.)
  `entry(key).or_default()` is "find or insert", one lookup.
- **Indices instead of references.** Records refer to templates by index
  (`Store::rtpl`), Drain's tree refers to clusters by id, and the
  segmenter maps cluster ids to templates with a `Vec` indexed by id. A
  reference into a `Vec` that is still growing would be invalidated when
  it reallocates, which in C++ is a dangling pointer and in Rust a
  compile error. Indices sidestep it, and are what the Python version
  uses too.
- **`Option` instead of a sentinel.** Python keeps -1 for "no record";
  `Store::rec` is a `Vec<Option<u32>>`, and the compiler makes each use
  handle `None`. It costs 8 bytes a line instead of 4. A `u32` with a
  sentinel, or `Option<NonZeroU32>` (which is 4 bytes, since 0 can mean
  `None`), would halve that if memory mattered.

## Numbers: no implicit conversions

No integer promotions, no implicit narrowing, no signed/unsigned mixing:
every conversion is written. `u64::from(x)` for conversions that cannot
lose information, `u32::try_from(x)` for ones that can (it returns a
`Result`), and `as` for deliberate casts. `as` never invokes undefined
behaviour: integer casts wrap or truncate as documented, and float to
integer truncates toward zero and saturates at the type's limits.
Integer overflow panics in debug builds and wraps in release builds,
unless you ask explicitly (`checked_add`, `wrapping_add`, ...).

Where: `Store::push` (`u32::try_from` for a line length), `Budget::bytes`
(float to integer with `as`, which matches Python's `int()`),
`summary::passthrough` (`i64::try_from` before comparing a byte count
with the budget). Clippy's `cast_*` lints, enabled in Cargo.toml, ask for
a reason (`#[allow(...)]`) wherever `as` could lose information.

**Division rounds like C++, not like Python.** `/` on integers truncates
toward zero (`-3 / 2 == -1`); Python's `//` rounds toward minus infinity
(`-3 // 2 == -2`). `div_euclid` is Python's behaviour for a positive
divisor. The segmenter's depth, `(indent - head_indent + 1) // 2` in
Python, is `div_euclid(2)` in `Segmenter::add_line`. There a `.max(1)`
happens to make the two agree, but the faithful translation keeps it
right if the clamp ever changes. `%` likewise follows the dividend's sign,
like C++; `rem_euclid` is Python's `%`.

## Statics and initialisation

Rust runs no code before `main`: a `static` must be a constant, or be
built lazily. `LazyLock<T>` builds its value on first use, once, safely
from any thread, like a function-local static in C++, and there is no
static initialisation order problem. Atomics (`AtomicU32`) behave as
`std::atomic`.

Where: `ANSI_RE` in `src/text.rs` is a `LazyLock<Regex>`, compiled once;
`Spool::temporary` numbers temporary files with an `AtomicU32`.

## Macros

Macros end in `!` and are expanded by the compiler on syntax, not text:
they cannot capture names by accident or break operator precedence.

- `format!`, `println!`, `write!` check the format string against the
  arguments at compile time.
- `matches!(c, 'a'..='z' | ' ')` tests a value against a pattern
  (`py_isspace`).
- `env!("CARGO_PKG_VERSION")` reads a variable at compile time
  (`pare::VERSION`).
- `#[derive(Parser)]` (in `src/cli.rs`) is a *procedural macro*: Rust code
  that runs during compilation, reads the `Args` struct and generates the
  argument parser, with the doc comments as help text. The generated code
  is ordinary type-checked Rust.

## Regular expressions: two engines

| crate | engine | supports | cost |
|---|---|---|---|
| `regex` | finite automata (RE2-style) | classes, alternation, repetition, captures, `^ $ \b` | linear in the text, guaranteed |
| `fancy-regex` | backtracking (like Python's `re`, PCRE, `std::regex`) | all that, plus lookahead, lookbehind, back-references, `\G` | can be much slower; pare sets a backtracking limit |

`std::regex` in C++ is a backtracking engine, and slow. Rust's standard
choice, `regex`, deliberately leaves out the features that need
backtracking so that matching can never take exponential time. pare's
masks need lookaround (`\b` on Python's word characters, `(?<![\w.])`),
so they need `fancy-regex`. Scanning whole lines with it made the pass
*slower* than Python.

The fix (`pyre::PyRegex`, used by every Python pattern in pare): `regex`
scans for the leftmost position where a relaxed version of the pattern
matches (`pyclass::erase_assertions` removes `\b`, lookahead and
lookbehind), and `fancy-regex` tries the real pattern only there,
anchored with `\G`. Erasing an assertion only removes a condition, so
the relaxed pattern matches wherever the real one can, and no match is
missed. A pattern with no assertions runs on `regex` alone; one that
`regex` cannot compile even relaxed (a back-reference in a vocabulary
file) runs on `fancy-regex` alone. Each variant of the private
`Engine` enum is one of those cases. The unit test
`two_engines_match_as_backtracking_alone` checks that the masks find
exactly what `fancy-regex` alone finds, on corpus lines. Masking got 14
times faster, and the result is Python's exactly (golden files of
Python's results check every row).

Case-insensitivity is the other trap. Rust's `(?i)` folds the case of
every member of a class, so `(?i)[\x{...}]` with Python's word table
gains characters such as U+0345, which folds to a Greek iota; Python's
`(?i)` leaves `\w`, `\d` and `\s` alone. `py_pattern` wraps Python's
classes in `(?-i:...)` where the pattern is case-insensitive, and adds
`İ` and `ı` wherever an `i` can match, which Python's `(?i)` equates and
Rust's does not.

Two smaller points:
- Compiling a regex is expensive, so each one is compiled once, in a
  `LazyLock` static.
- Replacement: `fancy-regex` and `regex` expand `$name` in a replacement
  string; `NoExpand(&s)` or a closure avoids surprises. `replace_all`
  returns a `Cow`: borrowed when nothing matched.

## Floating point: the same bits

Scores are `f64` in both languages, and the dump compares them to six
decimals, so they must be computed identically, not just closely:

- **Same operations, same order.** IEEE 754 arithmetic is deterministic,
  but `a + b + c` and `a + (b + c)` can differ in the last bit. The port
  copies Python's expressions term for term (`vocab_score`,
  `score_templates`), and a golden file holds Python's scores as `repr()`
  strings, which read back to the exact same `f64`; the test compares
  `to_bits()`.
- **`max` is not `max`.** Python's `max(0.0, x)` returns `0.0` unless `x`
  is greater, so `max(0.0, -0.0)` is `0.0`; Rust's `f64::max` may return
  either zero. `score::py_max0` spells out Python's rule.
- **Library functions.** `log2` comes from the platform's C library in
  both languages, so on one machine they agree. A static musl build uses
  musl's `log2`, which could differ from glibc's in the last bit for some
  counts; parity runs on glibc.
- **Formatting.** Rust's `{:.6}` and Python's `%.6f` both round the exact
  binary value half to even and both print `-0.000000`, so the text
  matches too.

## Lifetimes in a struct

`pass::Segmenter<'v>` holds `vocab: &'v Vocab`: a reference to a
vocabulary it does not own. The lifetime parameter `'v` is how the type
says so, and the compiler checks that no segmenter outlives its
vocabulary; in C++ it would be a `const Vocab&` member with the same rule
left to the programmer. `vocab::Line<'v, 't>` has two: it borrows the
vocabulary and the line's text. Python reads a global `VOCAB` that
`--vocab` reassigns; passing the vocabulary in instead makes the
dependency visible and lets tests build their own.

## Ordering `Option`

`Option<T>` is ordered with `None` below every `Some`, so where Python
writes `-1` for "no severity" and compares integers, Rust compares
`Option<u8>` directly: `sev > tpl.best_sev`, and
`detect_level(t).max(http_severity(t))` is Python's `max` over the
signals that are present. The type keeps "none" distinct from 0 (debug).

## Generated code

`src/pyunicode.rs` is generated by `bench/gen_pyunicode.py` from Python
and checked in, like a generated header. It holds Python's character
classes as `const` arrays of `(u32, u32)` ranges, baked into the binary,
with no allocation or start-up cost. `pyclass::in_table` looks a
character up with `binary_search_by`. CI regenerates the file and fails
if it differs, so it cannot drift from the Python version it describes.
`#[rustfmt::skip]` keeps `cargo fmt` from reflowing the tables.

The alternative, a `build.rs` build script that generates code at
compile time, would make every build depend on Python; a checked-in file
does not. Another option is a crate that ships Unicode tables (as
`regex-syntax` does internally), but none ships *Python's* definitions,
which are the point here.

## I/O details

- **Interrupted reads.** `read` can fail with `ErrorKind::Interrupted`
  when a signal arrives; that means "try again", and `pass::read_all`
  does. Python retries for you.
- **SIGPIPE.** Rust ignores SIGPIPE, so writing to a closed pipe
  (`pare ... | head -1`) returns an `Err(BrokenPipe)` instead of killing
  the process. `cli::ignore_broken_pipe` treats that one error as
  success.
- **Locking stdout.** `io::stdout().lock()` takes the lock once instead
  of on every write, and a `BufWriter` around it batches the writes.

## Tests

- Unit tests live in the module they test, in `#[cfg(test)] mod tests`,
  and can see its private items.
- Integration tests (`tests/*.rs`) are separate crates that see only the
  public API, or, in `tests/cli.rs`, run the binary, whose path Cargo
  provides as `env!("CARGO_BIN_EXE_pare")`.
- Examples in `///` doc comments are compiled and run by `cargo test`, so
  the documentation cannot drift from the code (`Budget::tokens_in`,
  `clean_line`, `py_isspace`, `mask`, `Drain::add`).
- Golden files: `tests/data/mask.tsv`, `vocab.tsv`, `vocab.txt` and
  `vocab-example.txt` hold Python's results, and the unit tests read them with `concat!(env!("CARGO_MANIFEST_DIR"), "/tests/...")`,
  a path fixed at compile time so the test works from any directory.
- A test that checks the tests: breaking a fast mask pattern on purpose
  makes four tests fail; that is how the superset test was checked.

## Performance notes

- **Finding lines.** `memchr::memchr_iter` finds newlines with SIMD;
  lines are slices of the chunk, never copied.
- **Allocation.** `clean_line` allocates only for lines it has to change
  (`Cow`); `Store::for_each_raw` reads every line into one reused buffer.
- **Bounds checks.** Indexing a slice (`&chunk[a..b]`) is checked and
  panics if out of range. The checks are cheap and often optimised away;
  iterators avoid them entirely. pare keeps them and forbids `unsafe`.
- **Release profile.** `lto = true` and `codegen-units = 1` let the
  optimiser see the whole program at the cost of build time; `strip`
  drops symbols from the binary.
- **Measure before guessing.** The first masking code was slower than
  Python's. A small benchmark binary (a scratch crate depending on `pare`
  by path, timing one function on real lines) showed the Python classes
  were not the cost, the backtracking engine was, which led to the
  two-engine design. After it, the segmentation pass over a 30 MB log
  took 2.1 s; with severity and scoring (stage 3) it takes 5.4 s and
  51 MB, against Python's 28.5 s and 65 MB.
- **Start-up counts too.** pare runs on every command an agent sends
  through it, mostly short ones. Python's classes make the patterns large
  (`\b` is four copies of a 12 KB class), and `fancy-regex` takes
  milliseconds to compile one; compiling them all cost 110 ms per run.
  `PyRegex` compiles its backtracking half on the first candidate
  (`OnceLock`), and start-up went back to about 50 ms.
- **ASCII first.** `py_isspace` and friends answer for ASCII characters
  with a `match` before falling back to a binary search in the tables;
  nearly every character in a log is ASCII.
