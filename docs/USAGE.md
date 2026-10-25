# Using pare: the details

What the README leaves out: the exact behaviour of command mode, follow
mode and run files, and how tokens are counted. `pare --help` lists every
flag; [DESIGN.md](DESIGN.md) says why things work as they do.

## Tokens and the budget

Tokens are estimated as bytes / 2.8, the rate measured with tiktoken on
pare's output (cl100k and o200k agree to within 1 %; the older public
Claude tokenizer gives 2.74 to 2.80). `--chars-per-token` changes it: 4
gives the prose rule of thumb, 2.5 a strict ceiling on dense logs
(timestamps, IPs and hex on every line), where the real rate is lower
and a budget-600 summary can run a few per cent over.

The budget is a hard ceiling on the rendered output, header and ledger
included. Below about 1,000 tokens it gets tight, and below about 400 the
fixed skeleton (header, head and tail windows, ledger) is most of the
output. Output that already fits the budget is printed as it is, with no
`## raw:` line (it is already all of it).

Without `--budget`, pare reads `$PARE_BUDGET` (an empty value counts as
unset; `auto` and `auto:N` work there too), else uses `auto`.

`auto`, the default, treats the budget as a ceiling (2,000, or N with
`--budget auto:N`) that the summary stops short of once nothing at error
level is left to show: only error-level shapes get a template entry,
output up to 1,000 tokens passes through, and the head and tail windows
are sized for 2,000 whatever the ceiling. A number, `--budget 2000`, is a
fixed budget the summary fills, as the Python version's default was. The
measurements behind it are in [DESIGN.md](DESIGN.md#automatic-budget).

## Command mode

`pare [flags] -- CMD ARGS` runs the command instead of reading stdin.

- The command's exit code is pare's exit code (a pipe would report
  pare's own).
- stdout and stderr are merged in the order the command wrote them, and
  the summary ends with the exit status (`## raw: … · exit 1`). When the
  output fits the budget and passes through, `## exit N` is printed only
  when N is not 0.
- The command line is recorded in a `.json` sidecar next to the run file,
  with the input and output sizes and the budget; `pare --stats` totals
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
- after `--interval` seconds, even while the stream is idle;
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
names one file; `--no-tee` saves nothing). The summary's last lines name
the file and show how to expand it:

```
pare --only T5 < runfile                 # the raw lines of one template
pare --except T4,T9 < runfile            # everything but those templates
pare --min-severity error < runfile      # error lines and above
pare --since L200 --until L400 < runfile # a line range (or -200: the last 200)
```

Template ids are stable for a given input and flags: the pipeline is
deterministic and the ids are numbered in order of first appearance. They
depend on the masks in use, so a run file saved with `--vocab` needs the
same `--vocab` to expand.

pare prunes its own run directory, at most once an hour, when it writes a
run file:

- runs older than 14 days are removed (`$PARE_RUN_KEEP_DAYS`; 0 for no
  age limit);
- then the oldest go until the rest fit in 500 MB (`$PARE_RUN_MAX_MB`; 0
  for no size limit), never a file less than a day old, since a summary
  may still point at it;
- `$PARE_RUN_PRUNE=off` disables it; `pare --prune [--dry-run]` sweeps
  now and reports what goes.

Only files pare named are touched: run files, follow run files and stale
spool files. A pruned run keeps its small `.json` record, with a
`"pruned"` timestamp, so `--stats` still counts it.
