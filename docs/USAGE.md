# Using pare: the details

What the README leaves out: the exact behaviour of the budget, what pare
prints, command mode, follow mode, run files and the subcommands, what
it returns and reads from the environment, and what a script or an agent
can rely on. `pare --help` lists every flag and variable;
[DESIGN.md](DESIGN.md) says why things work as they do.

## Tokens and the budget

Tokens are estimated as bytes / 2.0, the rate measured on pare's output
with the tokenizer of the current Claude models through Anthropic's
token-count API (OpenAI's cl100k and o200k give 2.8, the default before
October 2026). `--bytes-per-token` changes it
(`--chars-per-token` is accepted as an alias): 4 gives the prose rule of
thumb, 2.5 a strict ceiling on dense logs (timestamps, IPs and hex on
every line), where the real rate is lower and a budget-600 summary can
run a few per cent over.

The budget is a hard ceiling on the rendered output, header and ledger
included, and must be at least 1. 1,000 to 2,000 is the working range;
600 is too little for a run with several failures, and below about 600
the fixed skeleton (header, head and tail windows, ledger) is most of
the output ([DESIGN.md](DESIGN.md#tokens-and-the-budget)). Output that
already fits the budget is printed as it is, with no `## raw:` line (it
is already all of it).

Without `--budget`, pare reads `$PARE_BUDGET` (an empty value counts as
unset; `auto` and `auto:N` work there too), else uses `auto`.

`auto`, the default, treats the budget as a ceiling (2,000, or N with
`--budget auto:N`) that the summary stops short of once nothing at error
level is left to show: only error-level shapes get a template entry,
output up to 1,000 tokens passes through, and the head and tail windows
are sized for 2,000 whatever the ceiling. A number, `--budget 2000`, is a
fixed budget the summary fills, as the Python version's default was. The
measurements behind it are in [DESIGN.md](DESIGN.md#automatic-budget).

## What pare prints

A summary is plain text in sections, each starting with `#`:

```
# pare: 308 lines, 19,046 B (~9,523 tok) → ~1,763 tok (budget 2,000) · 35 templates
## head (L1–L5)          the first lines
## templates (by salience)   one entry per shape, with its count and an example
## selected lines        other lines worth their cost, with ┆ gap markers
## tail (L285–L308)      the last lines
## omitted 266/308 lines (86.4%): T5 ×120, …   the ledger
## raw: ~/.cache/pare/runs/d0ec7c06b92d.log    the run file (and · exit N in command mode)
   expand with: pare --only T<n> < raw …       a hint, at budgets of 1,000 and up, when there is a run file
```

Its marks (`→ · × – ┆ … ≤ ↳`) are a few per cent of a small summary's
bytes. `--ascii` writes them as `-> | x - | ... <= ->` instead; only
pare's own marks change, never a line of the log. With `--ascii` the
budget still holds, since the marks are measured as written.

A follow window starts with `# pare #N:`; `pare --only` and the other
selection flags start with `# pare --only T1: 215 of 239 lines`; empty
input prints `# pare: empty input`; `pare stats` and `pare prune` start
with `# pare stats:` and `# pare prune:`. The ledger and the gap markers
spell their counts out (`1 template not listed`, `(+1 more line)`).

Binary input is not summarised. A NUL byte in the first 8 KiB, the test
git and grep use, marks an executable, an image, an archive or UTF-16
text as binary, and pare prints two lines instead:
`# pare: binary input (NUL at byte 3), 300,000 B · not summarised` and
the `## raw:` line (`· exit N` in command mode). The input is still saved
in full and the exit status is the usual one. Follow mode takes its
stream to be text.

## Command mode

`pare [flags] -- CMD ARGS` runs the command instead of reading stdin.

- The command's exit code is pare's exit code (a pipe would report
  pare's own).
- stdout and stderr are merged in the order the command wrote them, and
  the summary ends with the exit status (`## raw: … · exit 1`). When the
  output fits the budget and passes through, `## exit N` is printed only
  when N is not 0.
- The command line is recorded in a `.json` sidecar next to the run file,
  with the input and output sizes and the budget; `pare stats` totals
  these over every run so far: what commands printed against what pare
  printed.
- A SIGTERM from a tool's timeout is passed to the command, and pare still
  summarises what it printed. Once the command has exited, pare does not
  wait for a background process it left holding the pipe.

## Follow mode

`pare --follow` (or `-f`) summarises input as it arrives instead of at
end of input. Each window becomes one block in the usual format, budgeted
on its own, and blocks are separated by a blank line. Every block starts
with a `# pare #N:` line; split the stream on that, since a verbatim
block can contain the input's own blank lines.

A window closes:

- after `--every` lines (default 20,000, which is also the window cap);
- after `--interval` seconds (default 30; 0 turns it off), even while
  the stream is idle, so a quiet log still reports;
- half a second (`--settle`) after a line at `--flush-on` severity
  arrives (default `error`; `fatal` or `none`), at most one such flush
  per `--min-interval` seconds;
- at end of input.

A line-count cut waits for the next record head, and a timed cut carries
the open record into the next window, where its frames keep inheriting
the error head's salience and belong to the same template; so a stack
trace is never split, and the template ids are the ones a batch run over
the finished run file would assign (a randomized differential test
against the batch pass checks this). A block that starts inside a record
opens with `↳ continues L1: ERROR …`, naming the head its first lines
belong to.

One stream, cut four ways:

```
input   ERROR a       ┊ at Foo.bar      ERROR b           20,000 lines  FAILED
        at Foo.run    ┊ at Foo.main     at Bar.go         of chatter    1 test
                      ┊
time  ────────────────┼───────────────┼─────────────────┼─────────────┼─▶
                      │               │                 │             │
cut          30 s idle       ½ s after         20,000 lines        end of
             (--interval):   ERROR b arrived   since the last      input
             the open record (--settle; at     cut, at the
             is carried      most once per     next record
                             --min-interval)   head (--every)

block   # pare #1       # pare #2       # pare #3         # pare #4
                        ↳ continues L1:
                          ERROR a
```

The frames after the first cut belong to ERROR a's template, which the
second block lists as `(cont.)`.

A window holding only blank lines is neither printed nor cut. A window
with no new shape and nothing above the salience floor is one heartbeat
line (`--quiet` drops it). A template first seen in a window is marked
`new`, and one arriving much faster than its running rate gets a rate
note.

The run file is named up front (`follow-YYYYMMDD-HHMMSS-pid.log`, UTC)
and appended as the stream runs, so every block's `--since/--until` range
can be expanded with the usual flags afterwards.

## Run files

Every run saves its input to a run file, named by a hash of the content,
in `~/.cache/pare/runs` (`$PARE_RUN_DIR`, or `--run-dir`; `--run-file`
names one file; `--no-tee` saves nothing, and refuses `--run-file`). The
summary's last lines name the file and show how to expand it:

```
pare --only T5 < runfile                 # the raw lines of one template
pare --except T4,T9 < runfile            # everything but those templates
pare --min-severity error < runfile      # error lines and above
pare --since L200 --until L400 < runfile # a line range (or -200: the last 200)
```

Template ids are stable for a given input and flags: the pipeline is
deterministic and the ids are numbered in order of first appearance. They
depend on the masks in use, so a run file saved with `--vocab` needs the
same `--vocab` to expand, and a pare version that clusters differently
(its CHANGELOG entry says "renumbers templates") numbers an old run file
differently.

pare prunes its own run directory, at most once an hour, when it writes a
run file:

- runs older than 14 days are removed (`$PARE_RUN_KEEP_DAYS`; 0 for no
  age limit);
- then the oldest go until the rest fit in 500 MB (`$PARE_RUN_MAX_MB`; 0
  for no size limit), never a file less than a day old, since a summary
  may still point at it;
- `$PARE_RUN_PRUNE=off` disables it; `pare prune [--dry-run]` sweeps now
  and reports what goes.

Only files pare named are touched: run files, follow run files and stale
spool files. A pruned run keeps its small `.json` record, with a
`"pruned"` timestamp, so `pare stats` still counts it.

## Subcommands

The modes that do not summarise are subcommands; each takes `--run-dir`
or `--vocab` as the summary does, and `--help`:

| command | prints |
|---|---|
| `pare prune [--dry-run]` | what it removed from the run directory, or with `--dry-run` what it would, and the limits in force |
| `pare stats` | what command mode saved: raw tokens against printed tokens over every recorded run, and the ten largest |
| `pare vocab` | the vocabulary in file format, built in or with `--vocab FILE` applied, to edit and pass back with `--vocab` |

## Exit status

| status | when |
|---|---|
| the command's | command mode (`pare -- CMD`), whatever pare printed; 127 when the command could not be found, 126 when it could not be run, 128 + n when a signal killed it |
| 0 | a summary, a selection, a subcommand, `--help`, `--version` |
| 1 | an I/O error: the input could not be read, or the run file or the output written |
| 2 | a usage error: an unknown flag or value, flags that contradict each other (`--follow` with `--only`, `--no-tee` with `--run-file`), a budget or width below its floor, a bad `--vocab` file or `$PARE_BUDGET` |

A broken pipe on stdout (`pare … | head`) ends the output quietly with
status 0.

## Environment

| variable | meaning |
|---|---|
| `PARE_BUDGET` | the budget when `--budget` is not given: a number, `auto` or `auto:N`; empty counts as unset |
| `PARE_RUN_DIR` | where run files go (default `~/.cache/pare/runs`); `--run-dir` overrides it |
| `PARE_RUN_KEEP_DAYS` | prune run files older than this many days (default 14; 0 for no age limit) |
| `PARE_RUN_MAX_MB` | then remove the oldest until the rest fit (default 500; 0 for no size limit) |
| `PARE_RUN_PRUNE` | `off`: never prune automatically |
| `PARE_VOCAB` | a vocabulary file, as `--vocab`; empty counts as unset |

Two more exist for testing pare itself: `PARE_REFERENCE_RULES=1` turns
off the rules added since the Python version, so the output is the
reference's (the parity check's setting), and `PARE_DUMP=FILE` also
writes every line's analysis (record, severity, template, score) to
FILE, in the reference's format, which the parity check compares.

## What you can rely on

pare's version is 0.MINOR.PATCH, and the number says what a release may
change. This is the promise from 0.2.0 on; a release that breaks any
line of it is a mistake to report.

**A patch release changes no output.** For the same input, flags and
vocabulary, a patch prints the same bytes as the release before it, with
the same template ids and exit status. Patches fix crashes, I/O errors,
packaging and documentation. A fix that changes a summary, even to
improve it, waits for a minor release.

**A minor release may change what a summary holds**: which lines and
templates are chosen, their scores, the wording of pare's own lines (the
header, the ledger, the hint, the marks), the built-in vocabulary and
masks, and a flag's default. Every such change is a CHANGELOG entry.
Template ids are kept unless a change to clustering, to the built-in
masks or to segmentation renumbers them; then the entry says
"renumbers templates", and `--only T<n>` on a run file from an earlier
minor version selects something else. The `.json` record next to a run
file names the pare version that wrote it, so the mismatch can be seen.

**Across every 0.x release, these hold:**

- The section markers: `# pare:` opens a summary, `# pare #N:` a follow
  block, `## ` each section, `## raw:` the run file line, `## omitted`
  the ledger, `## exit N` a non-zero status on a pass-through. Split a
  stream on `# pare` and a summary on `## `.
- Every flag, subcommand and environment variable in `pare --help`
  keeps working. A renamed flag keeps its old name as an alias for the
  rest of 0.x (`--chars-per-token` for `--bytes-per-token`).
- The exit statuses in the table above.
- The `.json` record's fields (`command`, `cwd`, `exit`, `started`,
  `seconds`, `lines`, `bytes`, `out_bytes`, `budget`, `chars_per_token`,
  `pare`),
  which `pare stats` and `bench/collect.py` read. Fields may be added;
  none is removed or changes meaning.
- Output that fits the budget is printed as it is, byte for byte, and
  a selection (`--only`, `--since`, ...) prints the log's own bytes: pare
  rewrites none of the input, only its own lines change between
  versions.
- Template ids within one version, for one input and one vocabulary.

**Not promised:** which lines a summary selects, the token estimate (the
bytes-per-token rate may be re-measured), the built-in vocabulary's
words, and anything behind `PARE_REFERENCE_RULES` or `PARE_DUMP`, which
exist for testing pare itself.

**At 1.0** "the rest of 0.x" becomes "until 2.0", and a change that
renumbers templates needs a major version.
