# pare: design notes

The README has the pitch, an example, install and the summary numbers. This
file has the rest: the algorithm at a high level, how it differs from Drain,
what "tokens" means here and how to pick a budget, the pipeline in detail,
the structural and vocabulary signals, the full benchmark tables and their
history, what is validated and what is not, and notes for a native port.

## The algorithm at a high level

The core idea: noisy command output is mostly a small number of line
*shapes* repeated thousands of times, plus a handful of lines that are
actually different. pare finds the shapes, counts them, and spends the
token budget on the handful.

1. **Save everything first.** Stdin is written to a run file named by its
   content hash before anything else happens. The summary is a view, not a
   replacement; every line it omits can be pulled back by template id or
   line range from that file.
2. **Group lines into records.** A log is not really line-oriented. A stack
   trace is one unit, so is a compiler diagnostic with its code snippet. A
   line indented deeper than the line above, or shaped like a frame
   (`at foo.py:12`), a caret marker or a `Caused by:`, is attached to the
   record above it. Each record has a head line, which is what gets
   clustered, and a body.
3. **Mask the variable parts.** Timestamps, IPs, hashes, paths, numbers and
   runs of rule characters become placeholders, so
   `2026-10-07 ERROR [worker-3] GET /api/cart 500 1137ms` and the same line
   from worker-5 a second later look identical:
   `<TS> ERROR [worker-<N>] GET <PATH> <NUM> <NUM>`.
4. **Cluster into templates.** The masked head lines go through Drain, a
   streaming log-template miner. It keys a parse tree on token count and
   the first tokens, and merges lines whose tokens agree above a similarity
   threshold, turning the differing tokens into wildcards. The result is a
   few dozen templates with counts: `T4 ×4,340 INFO http …`,
   `T9 ×180 ERROR http … 500`, `T12 ×1 FATAL giving up`. The 4,340 INFO
   lines have just become one line of output.
5. **Score salience.** Two signals, in priority order. First, *structural*
   severity: an explicit log level in a level position, an HTTP status in a
   request line, an exit code. These are language-independent and override
   words: an INFO line that says `0 errors` is info; an ERROR line is an
   error in German. Second, *vocabulary*: error words +3, warning words
   +1.5, summary words +1, stack frames +1, passing-test markers and
   progress chatter negative. A template's salience is its exemplar head's
   score plus a rarity term, `2 / (1 + log2 count)`, so a line seen once
   outranks a shape seen a thousand times. A raw line's score is its own
   vocabulary plus 0.6 of its record head's, so a bland stack frame under
   an ERROR head still rates, plus a bonus if it is the first location after
   an error message.
6. **Fill the budget with one priority queue.** Head and tail windows are
   placed first, since the command line and the final verdict usually live
   there. Then template entries and individual raw lines compete in a
   single queue on the same scale. A template entry costs one line and buys
   a count and an exemplar. A raw line is worth its score divided by
   `1 + 1.5 × (lines of that shape already shown)`, so the first `503` is
   valuable, the second less, the twentieth nothing. Lines under a floor
   never pad the output. Lines that land inside a placed template's
   exemplar record render under it as its body, which is how a traceback
   appears as one block. Guards drop restatements: a progress line that
   repeats the summary line, an exemplar already in the tail window.
7. **Enforce the limit and end with a ledger.** Allowances are
   conservative, so a second pass re-offers what did not fit against the
   measured slack; then a final loop sheds the lowest-priority items until
   the rendering fits. The budget is a hard ceiling. The last lines say what
   was omitted and from which templates, and where the raw file is.

Two properties fall out of this. It is deterministic, so template ids are
stable and `pare --only T9` means the same thing tomorrow. And it is
learned per input rather than per tool: nothing in it knows what pytest or
cargo output looks like except a short list of continuation shapes, and
with those switched off it still keeps 88 % of must-survive lines at a
600-token budget. The limits: the vocabulary is an English prior, the
severity forms are a finite list, and it only makes sense on line-oriented,
repetitive input. On a diff, a JSON blob or prose there is nothing to
cluster and it degrades to head, tail and whatever matches the word lists.

## How this differs from Drain

Drain ([He et al., 2017](https://jiemingzhu.github.io/pub/pjhe_icws2017.pdf);
[drain3](https://github.com/logpai/Drain3) is the Python implementation
pare uses) is a *log parser*: given a stream of lines it decides, online,
which template each line belongs to and which tokens are parameters. Its
output is the complete set of templates and a cluster id per line. It has
no notion of which templates matter, no budget, and no output format
beyond that.

pare uses Drain for exactly step 4 above and supplies everything around it:

- **Records, not lines.** Drain sees one string per call. pare segments
  the input into records first and clusters only the head line of each,
  so a 40-line stack trace is one record under one template rather than
  40 clusters of frames. The body travels with the head.
- **Masking before Drain, on pare's terms.** drain3 has its own masking
  hooks; pare masks first with placeholders chosen for developer-tool
  output (paths that must contain a letter, versions, units, rule runs)
  and then hands Drain the masked text with drain3's masking disabled.
  This keeps the shapes under pare's control and the clustering
  deterministic.
- **Stable ids.** Drain numbers clusters in creation order; pare renumbers
  them by first appearance, takes each cluster's final template text after
  all records are in, and pins the drain3 version so `T9` means the same
  thing across runs of the same input.
- **Salience and allocation.** Everything from step 5 on is pare: the
  structural severity, the vocabulary, the exemplar choice (the
  highest-severity record of each template), the priority queue, the
  diminishing returns, the hard budget, the ledger and the selection
  modes. Drain would list the 4,340 INFO lines' template and the FATAL
  line's template as peers; pare decides that one of them is worth 120
  bytes of a 2,400-byte budget and the other is worth one line in the
  ledger.

Drain could be swapped for any template miner that gives stable clusters
over masked head lines; the native-port notes below consider writing one
that fuses masking and clustering in a single pass.

## Tokens and the budget

**What a token is here.** pare counts tokens as `ceil(bytes / 2.8)` of
UTF-8 text, for its own output and for the budget, and the benchmark uses
the same rule for every tool. This is a proxy, not a tokenizer; 2.8 is the
measured rate below, and `--chars-per-token` changes it. Four bytes per
token is the usual rule of thumb for English prose, and the default while
the heuristics were developed; log text is denser in tokens because
paths, hashes, hex, punctuation and digits split into many short pieces.

**Measured.** `bench/tokens.py` counts the corpus and pare's output with
real tokenizers: OpenAI's cl100k_base and o200k_base through tiktoken, and
the Claude tokenizer JSON that litellm ships (the older public one with a
65k vocabulary; the tokenizer behind current Claude models is not
published, and Anthropic's `count_tokens` API is the exact source).
Bytes per token, totals over the eight corpus files:

| text | cl100k | o200k | Claude (legacy) |
|---|---|---|---|
| raw corpus, 1.37 MB | 2.59 | 2.59 | 2.43 |
| pare output at budget 600 | 2.86 | 2.85 | 2.80 |
| pare output at budget 2000 | 2.84 | 2.83 | 2.74 |

Per file the raw logs range from 2.1 bytes per token (the synthetic
service log: timestamps, IPs and hex on every line) to 3.8 (the pytest
run); pare's output sits in a narrower band of 2.6 to 3.2. So bytes/4
undercounts pare's output by about 40 %: a budget of 600 produces
roughly 850 real tokens and 2000 roughly 2,850. The raw input is denser
still, so the benchmark's compression ratios are, if anything,
conservative: in cl100k tokens pare's budget-2000 output is 2.0 % of the
input (10,665 of 527,288) against 2.2 % by bytes/4, and the budget-600
output 1.1 % against 1.3 %.

So the default is `--chars-per-token 2.8`, the measured value for these
tokenizers on this corpus, and the budget is a real-token ceiling. At
`--budget 2000` that produced at most 1,982 cl100k tokens per file (9,794
in total) and kept 72 of 73 must-survive lines (98.6 %, min 93 %). At 600
it is 30 % less text than the bytes/4 setting and recall drops to 75 %
(min 53 %): the tight setting in the bytes/4 tables below is really about
850 tokens. One file (go test, 2.6 bytes per token) still exceeded 600 by
7 %; set `--chars-per-token 2.5` for a strict ceiling on dense logs, or
`4` to reproduce the bytes/4 tables. The bench has the same flag.

**How to set the budget.** The budget is a hard ceiling on what reaches
the agent's context, and the ledger plus run file make everything above it
recoverable on demand, so the question is how much context one tool
result should cost by default:

- For an agent loop, 1,000 to 2,000 tokens per tool call is a good
  default. At 2,000 pare kept 72 of 73 must-survive lines on the eight
  corpus files, including the 750 KB CI log, at 2.0 % of the input (in
  bytes/4 units, about 2,850 real tokens, it kept all 73).
- Below about 1,000 the budget gets tight. At a real 600 the corpus held
  75 %; the bytes/4 600 setting, about 850 real tokens, held 97 % with
  two stack-frame misses in the largest log. Below about 400 the fixed
  skeleton (header, windows, ledger) is most of the output.
- Small outputs are passed through untouched when they fit, so a high
  budget costs nothing on a 50-line command; the budget only bites on
  output that needs it.
- Spend more only when needed: start at the default, and when the summary
  points at a template or line range, pay for that slice with
  `--only T9 --min-severity error` or `--since L3001 --until L3100` rather
  than by raising the budget for every call.

The right number ultimately depends on the context window and how many
tool calls a task takes; a 200k-token window with 40 tool calls at 2,000
each spends 80k on tool output, which is the kind of arithmetic the budget
exists to make explicit.

## Pipeline

1. **Tee.** Raw stdin is saved to a run file named by content hash
   (`$PARE_RUN_DIR` or `~/.cache/pare/runs/<sha>.log`), so nothing is lost
   and the same input always maps to the same path.
2. **Segment.** Lines become records: a line indented deeper than the
   previous record's head, or shaped like a continuation (stack frame,
   caret marker, `Caused by:`, assertion detail, diff hunk; plus the
   tool-specific shapes listed under **Tool-specific rules**), joins that
   record. Blank lines belong to no record; a single blank line inside an
   indented block does not end it (a tool-specific rule, for Jest and
   pytest). Records are capped at 400 lines.
3. **Mask.** Runs of rule characters shrink to three; timestamps, dates,
   times, UUIDs, IPs, URLs, hex and hashes, versions, paths, numbers with
   units, and digits inside identifiers become `<RULE>`, `<TS>`, `<IP>`,
   `<PATH>`, `<NUM>`, `<N>` and so on. Paths must contain a letter, so
   `1/30` is not a path.
4. **Cluster.** The masked head line of each record (first 40 tokens) goes
   through [drain3](https://github.com/logpai/Drain3) with similarity 0.5,
   depth 4, max 100 children. Clusters are renumbered `T1, T2, …` in order
   of first appearance; a cluster's final template text is taken after all
   records are in. drain3 is pinned to 0.9.11 because template ids depend on
   its exact clustering and `--only T3 < runfile` relies on them.
5. **Score.** See **Scoring model**.
6. **Allocate.** See **Allocation**.
7. **Ledger.** The output ends with how many lines were omitted and from
   which templates, the run-file path, and (above 1,000 tokens) a usage
   hint.

## Structural severity

Before any words are weighed, each line is checked for an explicit severity
in a structural *form*. A level word in the middle of a message
(`connection error occurred`) is vocabulary, not structure.

- **Log levels.** A bare uppercase token near the start of the line
  (`ERROR`, `WARN`), a bracketed one (`[error]`, `(warn)`), a key-value pair
  anywhere (`level=error`, `"level": "warn"`, `severity=CRITICAL`), a prefix
  with a colon (`error:`, `Warning:`), `[I]`, and (tool-specific) rustc's
  `error[E0308]:`, logcat's `E/Tag(` or `E/Tag:` at the line start after an
  optional timestamp and pid/tid, glog's `E0921 10:11:12`, and pytest's bare
  `E` at column 0. The tokens live in the vocabulary file as `level-fatal`
  … `level-debug` so `FEHLER` or `ERRO` can be added with `--vocab`.
- **HTTP status codes** from request lines (`GET /path 503`,
  `"GET /path HTTP/1.1" 503`), key-value pairs (`status=502`,
  `"status_code": 404`) and phrases (`responded with 504`, `returned HTTP
  500`, `HTTP/1.1 302`): 5xx error, 4xx warn, 1xx–3xx info. Bare
  `returned 500` is a row count and does not match.
- **Exit codes** from `exit code 1`, `exit status 2`, `exited with code 137`,
  `exit=0`, `rc=1`, `returncode: 2`: 0 info, 1–127 error, 128 and up or
  negative (signals, OOM kills) fatal. The `rc`, `returncode`, `exit_code`
  and `exit_status` forms require `=` or `:` so `1.4.0-rc1` is not an exit
  code.

A line's severity is the strongest signal on it. It sets the base score
(fatal +4, error +3, warn +1.5, info −0.5, debug −1) and decides whether
error words count: an INFO line saying `0 errors` stays info, an ERROR line
is an error in any language. Per template, the severities of all member
records are tallied; the highest-severity record becomes the exemplar, and
a template whose severity varies shows the histogram (`[ERROR×12 INFO×3,988]`).

## Vocabulary

The salience vocabulary is the one prior in the pipeline that is not learned
from the input, and it is English and developer-tool flavoured. Classes and
weights:

| class | weight | holds |
|---|---:|---|
| `error` | +3 | error, failed, refused, panic, CamelCase `*Error`, `FAIL`, assertion-diff lines (`Expected:`, `Received:`), … |
| `warn` | +1.5 | warning, deprecated, retry, skipped, flaky, … |
| `summary` | +1 | passed, total, finished, `added N packages`, `====` rules, … (record heads only) |
| `frame` | +1 | stack frames and `file.ext:line` locations |
| `pass` | −1 | ✓, PASS, ok: structural, so words in a test name (`✓ throws on invalid`) do not count |
| `noise` | −0.75 | downloading, compiling, debug, progress bars, … (ignored when the line also has an error word) |
| `strong-error` | — | decides whether a record *body* makes its template an error |
| `level-fatal` … `level-debug` | +4 … −1 | plain level tokens for the structural detection above |

Words match whole, case-insensitively, and not inside a hyphenated flag or
identifier (`--timeout=300s` is not an error). A zero count before an error
noun (`0 errors`, `0 failed`) is removed before matching. `pare
--dump-vocab` prints the lists as a file; `--vocab FILE` or `$PARE_VOCAB`
loads one back, where `[error]` adds to a class and `[error replace]`
replaces it, a plain line is a phrase and a `/.../` line a regex. See
`proto/vocab.example` for a German extension.

## Scoring model

A template's salience is what its exemplar head line says plus how unusual
its shape is (the exemplar is the highest-severity record; a real line is
judged, not the masked template text, where `0 errors` has already become
`<NUM> errors`):

    salience = vocab(exemplar head) + 2 / (1 + log2 count)

A raw line's score is its own vocabulary plus a share of its record head's:

    score = vocab(line) + 0.6 × vocab(head) − 0.25 × min(depth, 4)
          + 1.5 if it is the first source location after an error line in its record

where `vocab()` is the structural-severity base plus class weights above
(summary words only on heads). In the queue a raw line's priority is

    score / (1 + 1.5 × repeats of its shape already shown) × (120 / (120 + bytes))^0.35

A record head "repeats" by its drain template and severity (two pods'
readiness warnings are one shape); a continuation line by its masked
shape. Lines under a salience floor of 1.0 never pad the output.

Every term survived an ablation (`bench/ablate.py`, run in bytes/4 units,
the default at the time): switching it off
lowers recall at some budget or grows the output at the same recall. Nine
earlier heuristics did neither and were removed one or two at a time: a
bonus for template entries over lines, start/end position zones, a bonus
for multi-line templates, a bonus for a body with error vocabulary, a bonus
for continuing a record already on screen, decaying returns within one
record, a gentler repeat penalty for error lines, line-level template
rarity, and a first-occurrence bonus.

Those removals were judged on the same eight dev cases, so "removed" means
"did not help here", not "proven useless". Two of them are the kind that
could matter on logs this corpus does not have and should be re-ablated
once the held-out set exists: the **end-of-output position bonus** (a final
verdict with no error vocabulary, in a tool whose summary line is not in
the tail window) and the **first-occurrence bonus** (the first instance of
a repeated warning when later copies happen to sit near the end). The
others are less likely to come back: the template bonus and body-error
bonus duplicated signals the queue already has, and the record decay and
coherence bonus were superseded by the repeat penalty and exemplar bodies.

| heuristic | without it: recall@600 / @2000 (out tok @2000 where that is the reason) |
|---|---|
| (the full model) | 97.3% / 100%, 7,567 |
| template rarity | 93.2% / 100% |
| inheritance from the record head | 87.7% / 97.3% |
| depth penalty | 97.3% / 100%, 7,727 |
| error-location bonus | 94.5% / 97.3% |
| value-per-byte factor | 94.5% / 100% |
| repeat penalty | 93.2% / 100%, 9,362 |
| salience floor | 97.3% / 100%, 14,547 |
| near-duplicate suppression | 97.3% / 100%, 7,913 |
| window skipping of frequent templates | 94.5% / 100% |
| compact mode below 1,000 tokens | 89.0% / 100% |
| tool-specific rules (see below) | 87.7% / 94.5% |

## Allocation

The budget is `--budget` tokens at `bytes / 2.8` (`--chars-per-token`),
and it is a hard limit.

- A head and a tail window (`--head 5`, `--tail 10`, at most 30 % of the
  budget; the head shrinks to 3 below 1,000 tokens). Lines of templates
  with 5 or more records, and continuation lines of records 20 or more
  lines long, are skipped inside the windows, since the table already
  stands for them; the first and last lines always stay.
- Then one priority queue for template entries and raw lines on the same
  salience scale. A template entry (count, marker, exemplar head line)
  competes at its salience, so every template is listed once if the budget
  allows, highest first; a raw line competes at the priority above. So a
  high-salience exception line beats a low-salience singleton template, and
  a warning template beats the sixth frame of a trace. Lines that land in a
  placed template's exemplar record are rendered under it as its body (the
  traceback, the compiler snippet); the rest go to "selected lines". A
  template's exemplar head is itself a line candidate, so it can appear even
  when the entry does not make the cut; the entry takes the line over if
  placed later.
- Guards against restatement: a one-line record whose tokens are mostly
  (80 %) contained in a shown line, or a continuation line that is a pure
  token subset of one, is skipped; an exemplar the head or tail window
  already prints is referenced (`L308 (in tail)`), not repeated; a
  singleton whose whole record a window prints is not listed.
- Below 1,000 tokens, multi-record entries are one line (count + exemplar)
  instead of template text plus exemplar, the ledger reserve shrinks, and
  the usage hint is dropped.
- The per-item allowances (gap markers, record markers) are conservative,
  so a second pass re-offers what did not fit against the measured slack;
  then a final loop sheds content, lowest priority first, until the
  rendering fits, cutting from the end as a last resort.
- An input that already fits the budget is returned exactly, plus the
  run-file line when there is one.

`PARE_DEBUG=1` traces every placement and deferral to stderr.

## Selection modes

`pare --only T3,T7 < runfile` prints the raw lines of those templates;
`--except T4` prints everything but. `--min-severity LEVEL` (debug, info,
warn, error, fatal) keeps records whose head carries a structural severity
at or above LEVEL, so the twelve 503s hiding in a 4,000-line access-log
template come out with `--only T1 --min-severity error`; alone it applies
to every template. `--since L<n>` / `--until L<n>` restrict to a line range
(the `L` is optional; negative counts from the end). All three compose and
the budget still applies. `--max-templates N` caps the table.

## Tool-specific rules

Everything pare knows about particular tools, in one place, so recall can
be measured with and without it (`TOOL_SPECIFIC = False` in `proto/pare.py`,
or the "generic signals only" row of `bench/ablate.py`):

- pytest: `E` and `>` marker lines continue the failure block; a bare `E`
  at column 0 is an error level.
- rustc/gcc: gutter (`NN |`), `-->` location, `= note/help` and `:::` lines
  continue the diagnostic; `error[E0308]:` is a level.
- logcat: `E/Tag(` and `E/Tag:` at the line start are levels; glog:
  `E0921 10:11:12`.
- Jest, pytest: a single blank line inside an indented block does not end
  the record.

Without them: 87.7 % at 600 and 94.5 % at 2000 on the dev corpus (with: 97.3
% and 100 %). The generic signals (levels, status codes, exit codes,
vocabulary, rarity, inheritance) are the real story; the tool rules add 5 to
8 points on this corpus.

## Benchmark detail

Methodology is in the README. Versions: pare 0.1.0 with drain3 0.9.11, sqz
1.9.0, tidyout-cli 0.2.0. Modes: pare `--no-tee`; sqz
`compress --no-cache --cmd <name>`, one-shot, so its session-level dedup is
not exercised; tidyout `<cmd>` with a fake `<cmd>` on PATH that replays the
log, so its per-command rules apply.

### budget 2000, per case (bytes/4 units, `--chars-per-token 4`)

| case | tool | in tok | out tok | ratio | recall |
|---|---|---:|---:|---:|---:|
| cargo-build | pare | 945 | 945 | 100.0% | 8/8 |
| cargo-build | sqz | 945 | 763 | 80.7% | 4/8 |
| cargo-build | tidyout | 945 | 697 | 73.8% | 8/8 |
| ci-pipeline | pare | 187524 | 1977 | 1.1% | 15/15 |
| ci-pipeline | sqz | 187524 | 112882 | 60.2% | 7/15 |
| ci-pipeline | tidyout | 187524 | 187543 | 100.0% | 15/15 |
| go-test | pare | 2212 | 566 | 25.6% | 11/11 |
| go-test | sqz | 2212 | 2212 | 100.0% | 11/11 |
| go-test | tidyout | 2212 | 2230 | 100.8% | 11/11 |
| jest-fail | pare | 4396 | 843 | 19.2% | 10/10 |
| jest-fail | sqz | 4396 | 2099 | 47.7% | 2/10 |
| jest-fail | tidyout | 4396 | 4402 | 100.1% | 10/10 |
| make-build | pare | 2318 | 800 | 34.5% | 7/7 |
| make-build | sqz | 2318 | 990 | 42.7% | 1/7 |
| make-build | tidyout | 2318 | 2336 | 100.8% | 7/7 |
| npm-install | pare | 5459 | 347 | 6.4% | 2/2 |
| npm-install | sqz | 5459 | 4902 | 89.8% | 2/2 |
| npm-install | tidyout | 5459 | 5477 | 100.3% | 2/2 |
| pytest-fail | pare | 4762 | 1227 | 25.8% | 11/11 |
| pytest-fail | sqz | 4762 | 401 | 8.4% | 8/11 |
| pytest-fail | tidyout | 4762 | 1483 | 31.1% | 11/11 |
| server-outage | pare | 133851 | 862 | 0.6% | 9/9 |
| server-outage | sqz | 133851 | 63390 | 47.4% | 5/9 |
| server-outage | tidyout | 133851 | 1388 | 1.0% | 1/9 |

### budget 600 (bytes/4 units)

At 600 the seven smaller files keep every must-survive line, and each
output lands within a few tokens of the budget rather than leaving slack
unused. The CI log keeps 13 of 15: its keep list spans four failing
sections (webpack, Jest, docker, the rollout), and the fifteen lines alone
are about 1,300 bytes of a 2,400-byte budget whose skeleton (header,
windows, ledger) takes about 800. The two that go are the `at file:line`
locations of two of the four Jest failures; the rollout failure, the
webpack error and warning, the four Jest failure messages, the totals, the
npm summary and the exit code all survive. Shifting the weights to fit
those two frames pushes out a failure message instead, so the budget there
is saturated rather than misallocated. `--max-templates 6` lowers the 600
recall to 93 %.

### Calibrated to real tokens (bytes/2.8)

The same runs with `--chars-per-token 2.8`, so every count is within a
few percent of a cl100k/o200k count and pare's budget is a real-token
ceiling. sqz and tidyout have no budget, so only their counts change.

| tool | budget 2000: out tok, ratio, recall (min) | budget 600 |
|---|---|---|
| raw | 487,810, 100%, 100% | same |
| pare | 9,961, 2.0%, 99% (93%) | 4,549, 0.9%, 75% (53%) |
| sqz | 268,048, 55%, 55% (14%) | no budget knob |
| tidyout | 293,653, 60%, 89% (11%) | no budget knob |

Per case, pare at budget 2000:

| case | in tok | out tok | ratio | recall |
|---|---:|---:|---:|---:|
| cargo-build | 1350 | 1350 | 100.0% | 8/8 |
| ci-pipeline | 267891 | 1974 | 0.7% | 14/15 |
| go-test | 3160 | 809 | 25.6% | 11/11 |
| jest-fail | 6280 | 1205 | 19.2% | 10/10 |
| make-build | 3311 | 1144 | 34.6% | 7/7 |
| npm-install | 7799 | 495 | 6.3% | 2/2 |
| pytest-fail | 6803 | 1752 | 25.8% | 11/11 |
| server-outage | 191216 | 1232 | 0.6% | 9/9 |

The 2000 miss is one Jest frame location in the CI log
(`at src/hooks/useFetch24.test.tsx:40:3`), the same saturation as the
bytes/4 600 case. At a real 600 the output is 30 % shorter than the
bytes/4 600 output and the losses are spread over six files: two cargo
lines (an expected/found note and its location), seven of fifteen in the
CI log, two go frames and a FAIL line, one make warning, two pytest
exception lines, and three server-outage lines (the slow-cache warning,
a handler frame, the reconnect). Nothing was tuned at this setting; the
heuristics are frozen.

### How the misses were fixed (history)

Each round traced where the budget went with `PARE_DEBUG=1` and fixed a
cause rather than a weight:

- The tail window repeated lines the template table had already listed;
  exemplars inside a window are now referenced, not repeated.
- pytest's `E` lines began at column 0 and were records of their own; they
  now join the failure block, and the `E` marker is a structural level
  rather than an error word counted twice.
- Progress lines (`test_x FAILED [ 95%]`) that restate the summary lines
  were listed as templates of their own; near-duplicate suppression.
- `total()` inside a failure block earned a summary bonus; summary words
  now count only on record heads.
- A later copy of a gcc warning beat the first because it sat in the last
  10 % of the file; the position bonus was later removed entirely.
- Two pods' readiness warnings masked to different shapes, so the second
  escaped the repeat penalty; heads now repeat by drain template.
- A fixed split between the template table and the fill phase could not be
  right at every budget; one priority queue replaced it.
- `✓ throws on invalid` outranked real failure detail through the word
  `invalid`; a pass marker is now structural.
- The `kubectl … --timeout=300s` command echo ranked as an error; word
  boundaries exclude `-`.

## Validation status

The numbers above are a **training score**. The misses were fixed by
looking at the same eight cases, and three of the eight (jest-fail,
server-outage, ci-pipeline) are synthetic, generated by
`bench/gen_synthetic.py`, including the two largest wins. Recall also
measures presence, not usefulness.

The heuristics are **frozen** as of this document. The next evidence should
come from:

1. A held-out corpus of 15 to 20 real logs from actual agent sessions,
   ideally tools not yet covered (Maven, Terraform, kubectl, Bazel, Next.js
   build, tsc), with `.keep` files written **before** pare is run on them.
   `corpus-heldout/README.md` has the protocol; `bench/run.py --corpus
   corpus-heldout` reports it separately.
2. An agent-success evaluation: a few real tasks (fix a failing test, fix a
   broken build, find an outage cause) run with the full output, with pare,
   with sqz and with tidyout in the agent's context, reporting task success
   and tokens side by side.

### Generated corpus: real tools, chosen failures

`corpus-generated/` holds output captured from real Maven, tsc, Terraform
and kubectl runs on small projects written to fail (sources in its `src/`,
provenance in its README). The tools and their formatting are genuine; the
failures and the keep lists are not blind, so this is a second development
corpus for formats `corpus/` lacks, and it does not count toward the
held-out set. Nothing was tuned on it. Pare, frozen, scores:

| case | in tok | budget 2000: out, recall | budget 600: out, recall |
|---|---:|---|---|
| maven-compile (cold cache, 495 lines) | 22,628 | 1,857, 8/10 | 598, 6/10 |
| maven-test (Surefire, 128 lines) | 3,763 | 1,998, 9/9 | 596, 3/9 |
| tsc-build (35 errors, 49 lines) | 1,614 | pass-through, 13/13 | 578, 8/13 |
| terraform-init (20 lines) | 287 | pass-through, 4/4 | pass-through, 4/4 |
| terraform-validate (37 lines) | 473 | pass-through, 8/8 | pass-through, 8/8 |
| terraform-apply (plan + apply, 462 lines) | 5,922 | 1,401, 9/9 | 596, 2/9 |
| kubectl-apply (unreachable API server, 18 lines) | 1,388 | pass-through, 4/4 | 318, 3/4 |

Observations, recorded rather than fixed:

- **maven-compile at 2000** lists all 43 templates and runs out of budget
  before any selected lines: the second and third compile errors share
  one template (`incompatible types: <*> cannot be converted to <*>`, ×4
  because Maven prints the block twice), so only the exemplar is shown and
  the `[22,16]` sibling is in the ledger. The reactor summary's
  `shop-api ... FAILURE` line is a plain `[INFO]` line whose only signal
  is the word FAILURE inside a dotted leader; it ranked below the download
  templates' entries. A cap on template entries when the table alone
  would exhaust the budget is the obvious candidate, to be tested on the
  held-out set.
- **maven-test at 600** keeps the Surefire results block (both failure
  summaries with file:line) but not the stack-trace lines; 600 real tokens
  is below the skeleton plus nine keep lines here.
- **terraform-apply at 600** keeps the `Warning: Check block assertion
  failed` template but loses the three assertion messages and the plan and
  apply summary lines; the plan diff supplies 20 near-identical resource
  blocks that the template table spends the budget on.
- **kubectl-apply at 600** keeps the three error shapes (validation
  failure, glog `E1008 …` retries, `Unable to connect`) but the one keep
  line it loses is the second half of a 230-character validation error,
  cut by the default `--width 200`; wide single-line errors are the case
  where clipping costs content.
- Surefire's `[ERROR] Tests run: … <<< FAILURE!` lines, glog-style
  `E1008 19:43:51 …` lines from kubectl, Terraform's `Error:` /
  `Warning:` blocks with `on main.tf line N` context, and tsc's indented
  continuation messages all segment and level-detect as intended; no
  regex change was needed for these four formats.

## Performance

About 1.7 s on the 750 KB / 17,832-line CI log. drain3 is 0.35 s of that;
the rest is the vocabulary and severity regexes, one pass per class per
line.

**Memory.** The prototype makes a single pass over stdin. Each chunk is
written to the spool (the run file, or a temporary file under `--no-tee`)
as it is read, split into lines, and each line is segmented, clustered and
scored on arrival. What survives the pass is a set of typed arrays with
about 40 bytes per line (spool offset, record, depth, severity, rendered
size, score) and per record, plus the templates; a line's text is read
back from the spool only when the allocator looks at it, through a small
cache of materialised lines. The output is byte-identical to the earlier
all-in-memory design (checked against it on every corpus file, budget and
flag combination). Measured on the CI log concatenated to size:

| input | wall time | peak memory | before (all in memory) |
|---|---|---|---|
| 0.75 MB | 1.9 s | 29 MB | 2.3 s, 46 MB |
| 7.5 MB | 13.6 s | 41 MB | 19 s, 231 MB |
| 30 MB | 54 s | 65 MB | 76 s, 849 MB |

Time is still linear in the input, about 1.8 s per MB, and that is now
the limit on large files rather than memory. The candidate list for the
allocator holds every non-blank line scoring at or above the floor, as
one integer per line, which is the one structure still proportional to
line count; the raw bytes are never held in memory beyond one chunk.

pare still reads to end-of-file before it writes anything, because the
template counts, the tail window and the rarity term all depend on the
whole input. A long-running producer sees the summary when it exits; an
endless stream never gets one. That is a different mode, sketched below.

## Follow mode (design, not built)

Batch pare answers "what happened" once the command has exited. A
`tail -f`, a long server log or a two-hour build needs a different
question answered on a schedule: "what has happened since I last looked".
This section is the design for that mode; nothing of it is implemented.

**Shape.** `pare --follow` reads stdin as it arrives and writes a *delta
summary* each time a flush condition is met, then clears its per-window
state. Each delta is a self-contained block in the batch format, budgeted
by `--budget` on its own, ending with a blank line so a consumer can split
the stream:

```
# pare follow #3: L4,201–L6,730 (2,530 new, 6,730 total) · 2 new templates
## templates (in this window, by salience)
T19  ×1 E new L5,912: ERROR [db] connection to db-primary.internal:5432 lost: ...
T7   ×340 W L4,310: WARN  [cache] redis GET slow key=<*> took <NUM>ms      (0.2/s before, 11/s now)
T3   ×2,180 . L4,201: INFO  [http] GET <PATH> <NUM> <NUM>ms
## selected lines
  5913│     psycopg.OperationalError: connection failed: server closed the connection unexpectedly
## tail (L6,728–L6,730)
  ...
## omitted 2,514/2,530 lines in this window · raw: /path/run.log --since L4201 --until L6730
```

**Flush conditions.** Any of: `--every N` lines, `--interval S` seconds
(checked while waiting for input, so an idle stream still flushes on
time), end of input, and, with `--flush-on error` (default) a line at
error severity or above, debounced to at most one flush per
`--min-interval` so an error storm does not become a flush storm. A
window with nothing above the salience floor and no new template emits a
one-line heartbeat (`# pare follow #4: L6,731–L8,002, nothing new (T3
×1,270)`) unless `--quiet`; a heartbeat is how a reader knows the stream
is alive.

**What carries across windows.** The drain3 miner and its cumulative
per-template counts, the line counter, and the run file (the spool is
appended to as in batch mode, so every window's raw lines stay
recoverable with `--since/--until`). Template ids are assigned at first
appearance and never renumbered, so `T7` means the same thing in every
flush and in a later `pare --only T7 < run.log`, because drain is online
and deterministic in arrival order: a batch pass over the finished run
file assigns the same ids.

**What changes in scoring.** Rarity uses the cumulative count, not the
window's: a template seen 100,000 times before the window is a known
quantity even if it appears once more. Two signals that batch mode does
not have become the point of follow mode: *novelty* (a template first
seen in this window is marked `new` and gets a bonus) and *rate change*
(a template whose per-second rate in the window is well above its running
average is marked with both rates, which is what an outage looks like
before the first ERROR line). Head window only on the first flush; the
tail window is the last lines of each window, since verdicts tend to be
at the end of a run.

**Memory.** Per-window arrays are the ones the single pass already keeps,
cleared at each flush, so memory is bounded by the window size. Window
size is capped at `--every` lines (default 20,000) even when
`--interval` is the trigger. Cumulative state is one small record per
template, which grows only with new shapes.

**Rolling mode.** A delta stream suits a human terminal or a harness that
streams tool output. An agent that runs a long command in the background
and polls a file is better served by `--follow --rolling --out
digest.md`: a budgeted summary of the *whole* run so far, rewritten in
place at each flush. That needs a bounded reservoir of candidate lines
instead of the window arrays: for each repeat key keep the K highest
scoring lines (K a little above the number that could ever be shown at
the budget), and evict whole keys by their best score when the key count
passes a cap. The rolling summary approximates the batch summary rather
than matching it; batch mode on the run file afterwards is still the
exact one.

**Implementation path.** `ingest` already works line by line; it becomes
an object with `feed(chunk)` and `flush()` that owns the miner and the
cumulative counts and hands a window `Store` (with a line-number offset)
to `summarize`. The read loop uses non-blocking reads with a poll timeout
so `--interval` fires on an idle pipe. With `--every` as the only trigger
the output is deterministic, which is how tests should drive it; with
`--interval` it depends on timing, by design.

## Notes for a native port

- Template ids must stay stable across versions, or `--only T3 < runfile`
  breaks: a native Drain must match drain3 0.9.11's clustering exactly, or
  the change is a breaking one and should say so.
- The single pass is already the streaming shape a native port needs:
  tee, segment, cluster and score on arrival, keep per-line numbers and
  per-template statistics, read line text back from the run file by
  offset at allocation time. A native port can replace the typed arrays
  with packed structs and the spool reads with mmap.
- Fuse masking and clustering in one pass; with the regex costs fixed,
  drain is the remaining per-line cost and a native implementation can
  tokenize and mask once.
- The vocabulary classes compile to one alternation per class; a native
  port can use an Aho-Corasick scan for the plain words and keep the few
  regex entries separate.
