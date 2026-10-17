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
· [Numbers](#numbers-no-implicit-conversions) · [Statics](#statics-and-initialisation)
· [Macros](#macros) · [I/O details](#io-details) · [Tests](#tests)
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

Where: `add_line` in `src/pass.rs` is a free function taking
`&mut Store`, not a `&mut self` method on `Pass`. In `feed`, the line
being added can be a borrow of `self.rest`; a `&mut self` method would
need all of `self` exclusively while part of it is borrowed. Borrowing
two *different fields* (`&mut self.store` and `&self.rest`) is allowed,
because the compiler can see they do not overlap.

Pitfall: fighting the borrow checker with clones or `Rc<RefCell<...>>`.
The usual fix is structural: split the borrow by field, pass the piece
that is needed, or use indices into a vector instead of references
between elements. Later stages use indices for records and templates,
as the Python version does.

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
`text::py_isspace` lists Python's set explicitly. String indexing is by
byte, not character, and `s.len()` is bytes: Python's `len(s)` is
`s.chars().count()`.

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
  `clean_line`, `py_isspace`).

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
  drops symbols from the binary (2.3 MB).
