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
pare carries its own copy of the core of
[drain3](https://github.com/logpai/Drain3) 0.9.11, the Python
implementation, MIT-licensed) is a *log parser*: given a stream of lines it decides, online,
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
  and hands Drain the masked text. The built-in copy has no masking of
  its own (nor drain3's persistence, cluster limit or extra delimiters),
  which keeps the shapes under pare's control and the clustering
  deterministic.
- **Stable ids.** Drain numbers clusters in creation order; pare renumbers
  them by first appearance, takes each cluster's final template text after
  all records are in, and clusters exactly as drain3 0.9.11 does so `T9`
  means the same thing across runs of the same input and across pare
  versions. pare depended on drain3 until the core was vendored (a single
  file with no dependencies is easier to install); `test_pare.py` checks
  the copy against drain3 on every line of every log in the repository
  and on random messages that fill a tree node past its child limit, and
  the summaries of every log, at several budgets and in follow mode, were
  byte-identical before and after.
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

Claude is a main target and its current tokenizer is none of the three
above. `bench/tokens.py --anthropic claude-opus-5-5` counts the same
outputs through Anthropic's `count_tokens` API (it needs the `anthropic`
package and credentials, which the development container did not have);
run it before trusting the 2.8 default for a Claude agent, and pass the
measured value as `--chars-per-token` if it differs much.

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

### Automatic budget

`--budget auto` (Rust version) treats the budget as a ceiling (2,000, or
N with `auto:N`) that the summary need not reach. The allocator already
places entries in order of salience; under `auto` it stops once nothing
worth an entry of its own is left, so a log with one error gets a short
summary and a log with many errors gets more, up to the ceiling. Three
things change, and nothing else:

- **Template floor 3.0 instead of 1.5.** Only shapes at error level get a
  template entry; warnings and summaries still compete as raw lines,
  under the unchanged line floor (1.0), and still appear in the ledger.
- **Pass-through up to 1,000 tokens** (or the ceiling, if lower) instead
  of up to the budget. A fixed budget of 2,000 passes a 1,900-token log
  through whole; that was 77 of the 179 benchmark cases and 41 % of the
  output tokens.
- **Head and tail windows sized for 2,000 tokens** at most, so a higher
  ceiling does not lengthen them.

Tuned with `bench/run.py --pare-bin target/release/pare` over all 179
cases (the corpus, the generated corpus, Defects4J dev, and the
held-out SWE-bench and Defects4J sets), mean output tokens per case and
must-survive lines kept:

| setting | recall | mean tokens |
|---|---|---|
| `--budget 1000` | 93.1 % | 941 |
| `--budget 1500` | 97.4 % | 1,298 |
| `--budget 2000` | 98.8 % | 1,552 |
| `--budget auto:1000` | 92.1 % | 878 |
| `--budget auto` | 97.4 % | 1,204 |
| `--budget auto:4000` | 97.4 % | 1,347 |

`auto` keeps what `--budget 1500` keeps at 7 % fewer tokens, and
costs 1.4 points of recall against `--budget 2000` for 22 % fewer. On
the 144 held-out cases alone it is 98.2 % at 1,237 against 98.5 % at
1,598; on the 35 development cases 96.2 % at 1,069 against 99.5 % at
1,363. A higher ceiling adds little (`auto:4000`: 143 more tokens on
average, same recall), which is the point: the size follows the log, not
the ceiling. A small ceiling gains little over a fixed budget of the same
mean size (`auto:1000` against the fixed curve: about half a point).

What was tried and dropped: a higher line floor (2 or 3) saved tokens but
lost the bodies of warnings (Terraform's check failures, whose lines
score as a share of their head), costing more recall than a smaller fixed
budget would; a template floor of 3.5 or 4 cut entries that carried
must-survive lines. The output's size per case now ranges from about 700
tokens (deciles: 685 to 1,984) instead of crowding at the ceiling (957 to
1,990); 26 cases still reach it, against 40.

The thresholds were tuned on every set, held-out ones included, so the
held-out numbers above are not an unbiased estimate; the next fresh
sample should be scored with `auto` before it becomes the default.
The first one, Defects4J seed 6 (19 bugs, drawn after `auto` was
tuned), keeps 40 of 40 must-survive lines with `auto` at 21,086 tokens,
as `--budget 2000` does at 29,565 (`corpus-heldout/README.md`). The
second, SWE-bench seed 4 (18 tasks), keeps 29 of 30 with `auto` at
18,698 tokens, the same as `--budget 2000` at 24,357; its one miss is
the same at every budget and is not `auto`'s.

## Pipeline

1. **Tee.** Raw stdin is saved to a run file named by content hash
   (`$PARE_RUN_DIR` or `~/.cache/pare/runs/<sha>.log`), so nothing is lost
   and the same input always maps to the same path. Run files are pruned
   after 14 days, or past 500 MB in all, oldest first and never one under
   a day old (see the README).
2. **Segment.** Lines become records: a line indented deeper than the
   previous record's head, or shaped like a continuation (stack frame,
   caret marker, `Caused by:`, assertion detail, diff hunk; plus the
   tool-specific shapes listed under **Tool-specific rules**), joins that
   record. Blank lines belong to no record; a single blank line inside an
   indented block does not end it (a tool-specific rule, for Jest and
   pytest). Records are capped at 400 lines.
3. **Mask.** A vocabulary file's own masks run first (none by default).
   Runs of rule characters shrink to three; timestamps, dates,
   times, UUIDs, IPs, URLs, hex and hashes, versions, paths, numbers with
   units, and digits inside identifiers become `<RULE>`, `<TS>`, `<IP>`,
   `<PATH>`, `<NUM>`, `<N>` and so on. Paths must contain a letter, so
   `1/30` is not a path.
4. **Cluster.** The masked head line of each record (first 40 tokens) goes
   through Drain (built in, as drain3 0.9.11 implements it) with
   similarity 0.5, depth 4, max 100 children. Clusters are renumbered `T1,
   T2, …` in order of first appearance; a cluster's final template text is
   taken after all records are in. The clustering must not change, because
   template ids depend on it and `--only T3 < runfile` relies on them.
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
| `error` | +3 | error, failed, refused, panic, CamelCase `*Error`, `*Exception`, `*Failure` (JUnit's `ComparisonFailure`), `FAIL`, assertion-diff lines (`Expected:`, `Received:`), … |
| `warn` | +1.5 | warning, deprecated, retry, skipped, flaky, … |
| `summary` | +1 | passed, total, finished, `added N packages`, `====` rules, … (record heads only) |
| `frame` | +1 | stack frames and `file.ext:line` locations |
| `pass` | −1 | ✓, PASS, ok: structural, so words in a test name (`✓ throws on invalid`) do not count |
| `noise` | −0.75 | downloading, compiling, debug, progress bars, … (ignored when the line also has an error word) |
| (level only) | −1 | a line that is nothing but a level token (`[ERROR]`, `[WARNING]`, `ERROR:`) says nothing, whatever the level |
| `advice` | −1 | re-run, for more information, please refer to, see dump files, `run with --`, `[Help 1]`, …: a build tool's closing advice. Its score replaces the line's, level included, so an `[ERROR]` advice line stays under the floor |
| `strong-error` | — | decides whether a record *body* makes its template an error |
| `level-fatal` … `level-debug` | +4 … −1 | plain level tokens for the structural detection above |
| `continuation` | — | regexes for lines that continue the record above whatever their indentation: stack frames, carets, `Caused by:`, diff and rustc and pytest detail (segmentation, not scoring; Rust version only) |

Words match whole, case-insensitively, and not inside a hyphenated flag or
identifier (`--timeout=300s` is not an error). A zero count before an error
noun (`0 errors`, `0 failed`) is removed before matching. `pare
--dump-vocab` prints the lists as a file; `--vocab FILE` or `$PARE_VOCAB`
loads one back, where `[error]` adds to a class and `[error replace]`
replaces it, a plain line is a phrase and a `/.../` line a regex. See
`reference/vocab.example` for a German extension. The `continuation`
class (Rust version) takes regexes only: a `[continuation]` section adds
record shapes the built-in ones do not know, such as a Go panic's frames
at column 0, and `[continuation replace]` can drop built-in ones.
A `[mask NAME]` section (Rust version) is a mask of the file's own: what
its regexes match becomes `<NAME>` before the built-in masks run, for
site-specific ids such as order numbers, tenant names or build hashes
that would otherwise split one kind of line into many templates. There
are no built-in ones, so the default dump has no such section.

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
"did not help here", not "proven useless". The position terms were the
kind that could matter on logs this corpus did not have: the
**end-of-output bonus** (a final verdict with no error vocabulary, in a
tool whose summary line is not in the tail window) and the
**first-occurrence bonus** (the first instance of a repeated warning when
later copies happen to sit near the end). The others are less likely to
come back: the template bonus and body-error bonus duplicated signals the
queue already has, and the record decay and coherence bonus were
superseded by the repeat penalty and exemplar bodies.

**Re-ablated on nine sets.** The four position terms are back in the code
at weight 0 (`END_TPL_BONUS`, `START_TPL_BONUS`, `END_LINE_BONUS`,
`FIRST_BONUS`), and `bench/ablate.py --corpus DIR ...` switches each on at
the weight it had ("with ..." rows), reporting each set on its own line.
Change in recall, percentage points at 600 / 1,000 / 2,000 (* = the
latest fresh samples; Defects4J with alternative-form labels):

| term (old weight) | dev corpus | generated | D4J s1 | D4J s0 | D4J s2 | D4J s3* | SWE s0 | SWE s1 | SWE s2* |
|---|---|---|---|---|---|---|---|---|---|
| end zone, templates (0.5, 1.0 if all records) | 0 / 0 / 0 | +5.3 / −1.7 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | −2.5 / +2.5 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| start zone, templates (0.4) | −1.4 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| end zone, lines (0.5) | −1.4 / 0 / 0 | +1.7 / −1.7 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / −2.3 / 0 | 0 / −3.9 / 0 | 0 / +7.5 / 0 | 0 / +5.7 / 0 | 0 / 0 / 0 |
| first occurrence (0.5) | 0 / 0 / −1.4 | +1.7 / −3.5 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | −2.0 / −3.9 / 0 | +2.5 / 0 / −2.5 | −2.8 / 0 / −14.3 | −9.1 / 0 / 0 |
| all four | +1.4 / 0 / −1.4 | +5.3 / −5.3 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / −2.3 / 0 | −2.0 / −3.9 / 0 | 0 / +2.5 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |

None earns its place, so all stay at 0. First occurrence is harmful
outright: almost every line of a traceback is the first of its shape
(`def test_none_timeout(self):`, `>  assert r.status_code == 200`,
`E  + where 503 = …`), so the bonus lifts traceback lines over the list of
failing tests; psf__requests-2317 falls from 8 to 3 of 8 at 2,000. The end-zone
line bonus is the only term with a consistent gain on one source
(SWE-bench at 1,000: pytest ends with its verdict and the short summary)
and a consistent loss on another (Defects4J at 1,000: Maven ends with its
build verdict and chatter), so it is a per-tool signal rather than a
general one; the tail window already covers the end. The output-size
columns, not shown, grow with every term on.

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
- Repeats: a raw line's priority is divided by `1 + 1.5 × n`, where n
  counts lines already shown with the same repeat key. A record head's key
  is its drain template and severity (two pods' identical warnings are one
  shape however the pod names mask); a continuation line's is its masked
  text. The exception is an error line in a template whose records are a
  list of different things: when at least half of the template's records
  (the first 200) have distinct masked shapes, an error head repeats only
  by its masked text. Twelve `FAILED t.py::test_x` lines are one drain
  template with twelve shapes, and each test name is news; crashes of the
  same few pods, over and over, are one template with few shapes per
  record, and stay repeats. A name's bracketed parameters (pytest's
  `test_rolling_iter[numpy-3-True-1]`, JUnit 4's `testX[2]`) stay as
  written in the masked text, so two parametrizations of one test are two
  shapes, and a number inside them is news to the restatement guard
  below.
- Lists of failures are rendered compactly. A list template (as above,
  error lines of mostly distinct shapes, at least 3 records) whose record
  heads share a prefix of 16 bytes or more, cut after a separator, is one
  header with the prefix and `{…}`, then one item line per shown record
  with only the rest:

  ```
  T29  ×17 E FAILED test_requests.py::RequestsTestCase::{…}
    …test_DIGESTAUTH_WRONG_HTTP_401_GET
    …test_HTTP_200_OK_GET_ALTERNATIVE
    ┆ (+15 more)
  ```

  Items still compete one by one in the queue, at their own score, but
  cost (and are valued per byte at) only their rendered length, so about
  twice as many names fit: psf__requests-2317 lists 32 failing tests,
  with pytest's truncated reasons, in about 540 tokens, where as lines
  they cost about 1,000. Judging a list, names are compared with
  identifiers whole (`test_endpoint_get_0` and `test_endpoint_get_1` are
  two tests, where clustering masks `worker3` to `worker<N>`).
  `bench/run.py` expands the form before matching keep lines (`--strict`
  matches the literal text).
- Guards against restatement: a one-line record whose tokens are mostly
  (80 %) contained in a shown line, or a continuation line that is a pure
  token subset of one, is skipped (an error line only when what it adds is
  numbers: `FAILED t.py::test_a` and `FAILED t.py::test_b` share four of
  five tokens, but the fifth is the test); an exemplar the head or tail window
  already prints is referenced (`L308 (in tail)`), not repeated; a
  singleton whose whole record a window prints is not listed.
- A template floor (`TEMPLATE_FLOOR`, 1.5): an entry below that salience
  stays in the ledger, where its count still appears. The first entry is
  exempt. Passing-test templates (vocabulary −1 plus a rarity term that
  cannot reach 2.5 past one record) and `_ _ _ _` separators fall under
  it; a neutral singleton (salience 2.0) does not. Evaluated on both
  corpora: recall unchanged or up at every budget, 10 % fewer output
  tokens at 2,000 (floors of 2.0 and above start losing lines).
- A template whose text equals its masked exemplar (no `<*>` wildcard,
  only mask placeholders) is one line, `T19 ×4 S L183: tests/test_cart.py:14:`,
  since the template text would only repeat the exemplar.
- Rendered lines carry a `223: ` gutter rather than a padded `   223│ `:
  the gutter is on every shown line, and the compact form is 4 % fewer
  tokens over the corpus outputs (`bench/tokens.py` measured the forms).
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

## Command mode

`pare [options] -- CMD ARGS` runs the command instead of reading stdin.
A pipe loses two things an agent needs: the command's exit code (the
shell reports pare's) and the command line itself. In command mode:

- **Output.** stdout and stderr share one pipe, so they interleave in the
  order the command wrote them; stdin is inherited.
- **Exit status.** pare exits with the command's code (128 + n when a
  signal killed it, 127 when it could not be found) and appends it to the
  run line, `## raw: … · exit 1`, or `## exit 1` when nothing was saved.
  The note is part of the budgeted output. A run whose whole output fits
  is printed byte for byte, with no run line, and only a non-zero status
  added (`## exit 1`): exit 0 adds nothing.
- **Metadata.** `<hash>.json` next to the run file records the command,
  working directory, exit code, start time, duration, line and byte counts
  and pare version. `bench/collect.py` reads it to fill in a held-out
  case's `.cmd`. The file is content-addressed like the run file, so two
  runs with identical output share one record, the later one.
- **Signals.** Ctrl-C reaches the whole process group from the terminal,
  so pare ignores SIGINT and lets the command exit. SIGTERM and SIGHUP may
  be aimed at pare alone (a tool's timeout), so pare forwards them to the
  command and keeps reading. Either way the output so far is summarised.
- **Leftover processes.** Once the command has exited, pare stops reading
  after one second without output (`EXIT_GRACE`), even if the pipe is
  still open: a background job the command started, or a grandchild of a
  killed shell, can hold the pipe open indefinitely. The same rule applies
  in follow mode.
- **Not combined** with `--only/--except/--since/--until/--min-severity`,
  which read a run file.

`integrations/` builds on it: an instruction for agents' instruction files,
and a Claude Code PreToolUse hook (`integrations/claude-code/pare-hook.py`)
that rewrites a single, simple test, build or install command to
`pare --budget 2000 -- CMD`. The hook skips compound commands (a subshell
would change what persists, such as `cd`), watch modes and dev servers
(which never exit), and anything whose exact output an agent needs. It
fails open: an error or a missing pare means no rewrite. Its permission
behaviour is described in `integrations/README.md`.

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
be measured with and without it (`TOOL_SPECIFIC = False` in `reference/pare.py`,
or the "generic signals only" row of `bench/ablate.py`), and each rule on
its own (`TOOL_PYTEST`, `TOOL_RUSTC`, `TOOL_LOGCAT`, `TOOL_BLANK_IN_BLOCK`;
the "no ... rules" rows):

- pytest: `E` and `>` marker lines continue the failure block; a bare `E`
  at column 0 is an error level.
- rustc/gcc: gutter (`NN |`), `-->` location, `= note/help` and `:::` lines
  continue the diagnostic; `error[E0308]:` is a level.
- logcat: `E/Tag(` and `E/Tag:` at the line start are levels, and a
  multi-line entry (one header repeated on each line) is split by its
  message, not its header, and ActivityManager's `Process … (pid N) has
  died` (INFO) is an error when pid N logged a `FATAL EXCEPTION`; glog:
  `E0921 10:11:12`.
- Jest, pytest: a single blank line inside an indented block does not end
  the record.
- Structured lines (Rust version, `TOOL_STRUCTURED`, `src/structured.rs`):
  see **Structured lines** below.

When the rules were frozen, the dev corpus without them gave 87.7 % at 600
and 94.5 % at 2000 (bytes/4 units; with them 97.3 % and 100 %).

**Rechecked on nine sets**, each rule switched off on its own. Change in
recall, percentage points at 600 / 1,000 / 2,000 (* = the latest fresh
samples; output size where it moved):

| switched off | dev corpus | generated | D4J s1, s0, s2, s3* | SWE s0 | SWE s1 | SWE s2* |
|---|---|---|---|---|---|---|
| (default recall) | 78.1 / 95.9 / 100 | 57.9 / 84.2 / 96.5 | 68–91 / 93–100 / 98–100 | 62.5 / 75 / 90 | 65.7 / 71.4 / 100 | 86.4 / 90.9 / 100 |
| all rules | −6.9 / −5.5 / −4.1 | 0 / −5.3 / −5.3 | 0 everywhere | −10.0 / −2.5 / −5.0 | −11.4 / 0 / −17.1 | −18.2 / −18.2 / 0 |
| pytest markers | 0 / 0 / 0 | 0 | 0 | −5.0 / 0 / −2.5 | −8.6 / 0 / −14.3 | −13.7 / −13.6 / 0 |
| rustc/gcc | 0 / −1.4 / 0 | 0 | 0 | 0 | 0 | 0 |
| logcat/glog levels | 0 | 0 | 0 | 0 | 0 | 0 |
| blank line in a block | −4.1 / −4.1 / −4.1 | 0 / −5.3 / −5.3 | 0 | −7.5 / −2.5 / 0 | −8.6 / 0 / −17.1 | −4.6 / 0 / 0 |

- **pytest markers earn their place on held-out data**, by up to 14
  points; the dev corpus hardly noticed them (its pytest log is small).
  Without them `E` lines are records of their own and the failure blocks
  fall apart.
- **The blank-line rule is the most valuable rule**, and not only for
  pytest and Jest: it costs 4 to 17 points wherever it is off, and the
  output grows by up to 970 tokens per set (summed over the three
  budgets).
- **rustc/gcc** shows only on the dev corpus (cargo and make, one line at
  1,000); no held-out set has Rust or C output. Kept, on that evidence.
- **logcat/glog levels change nothing on these sets because none has
  Android or glog output.** A synthetic logcat case added since
  (`corpus/android-logcat`, threadtime format) shows the logcat rule
  working: 75 % at 1,000 with it, 62.5 % without (87.5 % with it since
  the entry fix below). It also found that the
  rule missed every line with a tag shorter than eight characters, which
  logcat pads before the colon (`E netmgr  : …`): 663 of 2,510 lines had
  no level. Fixed; no other log changed. glog is still untested.

  **logcat entries.** logcat prints one Log call over several lines by
  repeating its header, timestamp included, on each, so nothing in a
  stack trace is indented and every frame looked like a record of its
  own: frames of different traces clustered into one template and the
  app's frame was lost. Now a logcat line is judged by its message, inside
  its entry: a new header always starts a record, and within an entry
  the generic rules apply to the message, so the exception heads its
  record and its frames continue it, as in any Java trace. A continuation
  line is also scored by its message (the `E` in the header belongs to the
  entry; a frame is a frame). Two intermediate versions were worse and
  were dropped: one record per entry, with every line scored at the
  header's level, put 20 frames ahead of the exception (25 / 37.5 /
  62.5 %); one record per entry scored by message still lost the
  exception at 600 (37.5 %). On the case, with its generator fixed to give
  every line of an entry one timestamp, as real logcat does: 50 / 75 /
  87.5 % before, 50 / 87.5 / 87.5 % after, at 600 / 1,000 / 2,000; the
  NullPointerException now heads its record with
  `NoteListAdapter.kt:58` under it. No other log's output changed.

  **A crashed process's death.** ActivityManager logs every process exit
  at INFO (`Process com.example.notes (pid 4321) has died: fg TOP`),
  routine kills of cached apps included, so the line scored −0.5 and was
  lost at every budget. Making every death an error would flood a real
  capture with those kills; instead pare remembers the pids that logged a
  `FATAL EXCEPTION` and raises a later death of one of them to error
  level. The case goes to 100 % at 1,000 and 2,000 (from 87.5 %); at 600
  it does not fit. No other log's output changed.
- **Maven needs none of them.** Every Defects4J set is unchanged with all
  rules off: Java output is carried by the generic signals.

All four stay. Tool knowledge also lives in the vocabulary (the `advice`
phrases name Maven, Gradle and npm wordings; `ComparisonFailure`), which
is the prior by design and outside this switch.

### Structured lines

The roadmap asked for format inference (detect logfmt, JSON lines or a
known tool from the first few hundred lines and switch rules on) only if
evidence showed formats the rules miss. No log in any corpus or held-out
set was JSON or logfmt, and no held-out miss was a format problem, so
four cases were made the way `corpus-generated/` makes them: real
loggers in small programs written to fail in chosen ways (Go's
`log/slog` in both its JSON and text handlers, pino 9 in Node, structlog
24 in Python). Before the change pare kept 8 of their 18 must-survive
lines, at every budget from 600 to 2,000. Severity and clustering were
mostly fine (logfmt request lines form one template with wildcards); every
miss was the same one: structured loggers write time, level, message and
request context first and the cause (`err`, `exception`, `panic`,
`stack`) after them, often with a whole stack trace escaped in one string,
so the 200-character clip cut the line before the cause. pino's numeric
levels (`"level":50`) were not read as levels at all; the vocabulary
(`err`, `slow`) happened to give its lines the right markers anyway.

What was built is narrower than format inference, and per line rather
than per file:

- **Recognition per line.** A line is structured if it is a JSON object
  (only whitespace after its closing brace) or logfmt (at least three
  space-separated `key=value` parts and nothing else). Nothing is
  inferred about the file, so a mixed log (a test runner printing a
  service's JSON output) is handled line by line, and no guess can turn
  rules on or off for the lines around it.
- **A clip that keeps the cause.** When a structured line is longer than
  `--width` and a telling field (`err`, `error`, `exception`, `exc_info`,
  `panic`, `fatal`, `reason`, `cause`, `stack`, `stacktrace`, `trace`,
  `traceback`, `msg`, `message`, `event`) lies past the plain cut, the line
  keeps whole fields from its start and then the telling fields, which get
  up to three quarters of the width: `{"level":50,"time":…,…"err":…`. The
  `…(+Nc)` marker still counts what was left out, and costs are computed
  from the clipped text as before, so the budget stays exact. A line whose
  cause already shows is cut as usual.
- **Escaped traces as message and frame.** A telling string with escaped
  newlines (or an object with such a `stack`, as pino writes `err`) is
  decoded and shown as two lines joined by ` ⏎ `, between `‹…›`: for a
  Python traceback the last `File "..."` line and the exception; for
  JavaScript, Java or .NET the message and the first `at` frame; for a Go
  `debug.Stack()` the function and file after the last `panic(` frame,
  with the function's pointer arguments dropped.
- **Numeric levels.** A JSON line whose `"level"` is 10 to 60 in steps of
  10 has that level (10 and 20 debug, 30 info, 40 warn, 50 error, 60
  fatal).

After the change the four cases keep 18 of 18 at 600, 1,000 and 2,000;
the other 179 cases are unchanged at every budget (the Python version's
output, byte for byte, on all 195 parity inputs; `bench/parity.py` leaves
the four Rust-only logs out). A 33 MB JSON-lines log runs about 3 %
slower, within run-to-run noise on plain logs.

The cases were written by someone who knew the fix was coming, so they
show the miss exists and is fixed, not how often it matters; real
structured logs from a held-out sample are the test that is still owed.
Not done: reading fields for clustering (a JSON line without spaces is
one Drain token, so `"method":"GET"` and `"method":"POST"` request lines
are two templates rather than one with a wildcard). That costs a few
templates, not recall, on these cases.

## Benchmark detail

Methodology is in the README. Versions: pare 0.1.0 with drain3 0.9.11 (now built in), sqz
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
| pare | 8,494, 1.7%, 100% (100%) | 4,341, 0.9%, 79.5% (60%) |
| sqz | 268,054, 55%, 55% (14%) | no budget knob |
| tidyout | 293,653, 60%, 89% (11%) | no budget knob |

Per case, pare:

| case | in tok | 2000: out tok, recall | 600: out tok, recall |
|---|---:|---|---|
| cargo-build | 1,350 | pass-through, 8/8 | 587, 8/8 |
| ci-pipeline | 267,891 | 1,990, 15/15 | 600, 9/15 |
| go-test | 3,160 | 717, 11/11 | 585, 8/11 |
| jest-fail | 6,280 | 1,043, 10/10 | 596, 10/10 |
| make-build | 3,311 | 957, 7/7 | 598, 7/7 |
| npm-install | 7,799 | 309, 2/2 | 212, 2/2 |
| pytest-fail | 6,803 | 1,240, 11/11 | 578, 8/11 |
| server-outage | 191,216 | 888, 9/9 | 585, 6/9 |
| android-logcat (added later; not in the totals above) | 95,542 | 1,979, 8/8 | 596, 4/8 (593, 5/8 since "JUnit 3 assertion messages") |

Two changes after the heuristics were frozen moved these numbers, both
format rather than scoring and both evaluated on both corpora before
adoption: the compact `223: ` gutter (4 % fewer tokens per shown line)
and the template floor (entries under salience 1.5 stay in the ledger).
Together they took the dev corpus at 2,000 from 72 of 73 lines to all
73 with 16 % less output, and at a real 600 from 75 % to 79 %. The
losses at 600 are still spread over five files: one cargo location, six
of fifteen in the CI log, two go frames and a FAIL line, two pytest
exception lines, and three server-outage lines. Nothing else was tuned
at this setting.

The failing-test fix (below, "First change from held-out evidence") cost
one line here: at 600, pytest-fail now shows the failing assertion's
location (`tests/test_cart.py:18: AssertionError`, a different exception
type from the template's exemplar, so no longer a repeat) and the last
exception line misses the budget by 11 bytes; 9/11 became 8/11.

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
- Several failing tests (`FAILED path::test`, `testX(pkg.Class) <<<
  FAILURE!`) shared one drain template, so the repeat penalty and the
  near-duplicate guard left one or two of them; an error line in a
  template of mostly distinct shapes now repeats only by its own shape.
  Maven's closing `[ERROR]` advice took the 600-token budget from the test
  names; an `advice` vocabulary class. Found on held-out sets; see below.

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

### First change from held-out evidence

The first held-out numbers (SWE-bench Verified and Defects4J, labelled by
the datasets; `corpus-heldout/README.md`) showed one failure on both
sources: a run with several failing tests lists each in a line of the
same shape, and pare showed one exemplar and a count. Two changes, both
in "Allocation" and "Vocabulary" above:

1. **Failing tests are a list, not repeats.** An error line in a template
   whose records are mostly distinct masked shapes repeats only by its
   own shape, and the near-duplicate guard drops an error line only when
   what it adds is numbers. The guard against the opposite mistake (the
   same few pods' crashes counted as news) is the distinct-shape ratio.
2. **Build-tool advice is not the failure.** Maven ends every failed
   build with eight or so `[ERROR]` lines of advice (`re-run Maven with
   the -e switch`, `[Help 1]`), each a singleton template at error level,
   so each outranked the failing tests at 600. The `advice` class (−3)
   counts even on an error line. (Strengthened later: see "Maven
   chatter".)

Developed on a separate Defects4J sample (seed 1, 19 bugs, none in the
held-out set: `corpus-dev/defects4j/`) and checked against both dev
corpora. Recall, before → after:

| set | 600 | 1,000 | 2,000 |
|---|---|---|---|
| dev corpus (8) | 79.5 → 78.1 % | 95.9 → 95.9 % | 100 → 100 % |
| generated corpus (7) | 56.1 → 59.6 % | 82.5 → 84.2 % | 96.5 → 96.5 % |
| Defects4J dev, seed 1 (19) | 43.2 → 68.2 % | 75.0 → 90.9 % | 95.5 → 100 % |
| *held-out* SWE-bench, seed 0 (16) | 47.5 → 45.0 % | 55.0 → 65.0 % | 60.0 → 82.5 % |
| *held-out* Defects4J, seed 0 (11) | 29.0 → 48.4 % | 54.8 → 77.4 % | 93.5 → 96.8 % |

The held-out sets are no longer held out: they found the problem, and the
first version of change 1 was checked on them before the seed-1 dev set
existed. Fresh samples drawn afterwards with `--exclude` (SWE-bench seed
1, 17 tasks; Defects4J seed 2, 16 bugs) measure it honestly, before →
after:

| set | 600 | 1,000 | 2,000 |
|---|---|---|---|
| *fresh* SWE-bench, seed 1 (17) | 51 → 60 % | 60 → 60 % | 63 → 89 % |
| *fresh* Defects4J, seed 2 (16) | 30 → 51 % | 63 → 74 % | 86 → 98 % |

The gains match the burned sets, so the fix was not fitted to them. The
fresh misses, with the candidates in `ROADMAP.md` stage 3, are in
`corpus-heldout/README.md`.

One of those misses is fixed since: parametrized test ids that differ
only in numbers (`[numpy-3-True-1]`, `[numpy-3-True-2]`) masked to one
shape, so they were repeats. A name's bracketed parameters now stay as
written in the shape key. Developed on a synthetic log, it changed no
output byte on any dev set (both corpora, the Defects4J dev sample, the
two first held-out sets); on the fresh sets, xarray-6744 went from 1 to
3 of 3 at 1,000 and 2,000 and nothing else moved. Assertion messages that
differ only in numbers stay repeats: making a number in a message news
would undo the repeat rule for every `timeout after 3012 ms`.

The other, more failing tests than the budget holds as lines, is fixed by
listing them compactly ("Allocation", the list form). Developed on the
first held-out sets (now dev data) and a synthetic log; no dev case got
worse and four got better, and on the fresh sets SWE-bench seed 1 went
from 94 to 100 % at 2,000 (psf__requests-2317, 6 → 8 of 8; every case
complete) and Defects4J seed 2 from 74 to 77 % at 1,000. 600 and 1,000 on
SWE-bench did not move: there the tracebacks outrank the names.

Which of many failures matter: in a broken environment most failing
tests fail one way, and the few that fail otherwise are the news. On
mwaskom__seaborn-3069, 74 of 76 fail on a missing pandas option and the
dataset wants the two assertion failures; on psf__requests-1142, 21 of 22
fail on a `TypeError` and it wants the one `AssertionError`. A summary
line says nothing of this (pytest cuts the reason at `- as...`), so each
listed failure's own section is found: the last earlier record head that
names it (pytest's `___ Class.test_x ___`, Surefire's `testX(...) <<<
FAILURE!`), or the item itself, up to the next head of the same kind. Its
way of failing is the masked shape of the section's last top-level error
line (`<PATH>:<NUM>: OptionError`). In a list of at least 5 such
failures, one whose way is shared by at most a tenth of the list gets +2
salience, and is listed first:

```
T7   ×60 (3 unlike the rest, first) E FAILED tests/test_b.py::{…}
  …test_case_7 - ...
  …test_case_31 - ...
  …test_case_52 - ...
  …test_case_0 - ...
```

No penalty for the common way: at 2,000 the list form still shows all
of a large list. Dev: both cases above now name their wanted tests at 600
and 1,000 (SWE-bench seed 0, 47.5 → 57.5 % at 600, 67.5 → 75 % at
1,000), no case worse, and the Defects4J sets are untouched (few failures
per bug). Fresh: psf__requests-2317 0 → 2 of 8 at 600 and 1,000. Its
other six wanted tests fail with the forty the missing network breaks,
in the same ways, so no ranking can find them. The section search scans
up to 200,000 record heads before the list; on a 300,000-line log it
added 3 % to the run time.

### Maven chatter

On Maven runs at 600 tokens the template table filled with build
chatter. Over the 30 Maven dev logs (the Defects4J dev sample and the
seed-0 set) a bare `[ERROR]` was placed in every one, `[ERROR] -> [Help
1]` in 24, `Please refer to … for the individual test results` in 21:
the −3 advice weight still left a singleton advice line at salience 3.5
(error level 3, rarity 2). Tracing the misses found two more causes:

1. **Level-only lines.** `[ERROR]`, `[WARNING]` and `ERROR:` alone now
   score −1.
2. **Advice replaces the score.** An advice line scores −1 whatever its
   level, so with the rarity term it stays under the template floor and
   never pads the fill. The phrases were tightened so a real error that
   mentions running is not advice (`run with --`, not `run with`), and
   Maven 3.9's wordings added (`see dump files`, `for the individual test
   results`, `after correcting the problems`).
3. **A failing test is not a restatement of its class summary.**
   `testEmpty1256(…TestJDKAtomicTypes) <<< FAILURE!` shares 85 % of its
   tokens with `Tests run: … <<< FAILURE! - in …TestJDKAtomicTypes`, and
   the template-entry guard dropped it. The guard now treats a new name
   as news for template exemplars too, as it already did for raw lines
   (an inconsistency left by the failing-test fix).
4. **`ComparisonFailure` is an error.** The CamelCase exception names
   covered `*Error` and `*Exception` but not `*Failure`, so JUnit's
   `junit.framework.ComparisonFailure: expected:<…>` scored 0.

**The windows too.** The head and tail windows take lines by position,
so the tail of a failed Maven build was still its closing advice (`[ERROR]
-> [Help 1]`, `re-run Maven with the -e switch`, bare `[ERROR]` lines).
The windows now skip both kinds of chatter as well, even as the first or
last line, and the tail reaches the verdict: the test counts, the reactor
summary, `BUILD FAILURE`, `Failed to execute goal … There are test
failures.` Defects4J seed 0 at 600: 67.7 → 83.9 %; the dev sample 90.9 →
100 %; the fresh seed 3, 76.5 → 82.4 %; seed 2, 75 → 79.5 %; each
generated Maven case gains a line, and cargo-build gains rustc's last
line at 600 (its `For more information … try rustc --explain` is advice
too). No set lost a line, and many 2,000-token outputs got smaller.
SWE-bench is unchanged.

Build warnings (`'build.plugins.plugin.version' … is missing`,
`bootstrap class path not set`) stay: they are real warnings, and the dev
corpus has warnings that matter (gcc's).

| set | 600 | 1,000 | 2,000 |
|---|---|---|---|
| Defects4J dev, seed 1 (19) | 68 → 91 % | 91 → 95.5 % | 100 % |
| Defects4J, seed 0 (11, dev now) | 48 → 65 % | 84 → 93.5 % | 97 → 100 % |
| Defects4J, seed 2 (16, found the problem) | 51 → 72 % | 77 → 93 % | 98 % |
| SWE-bench, seed 0 (16, dev now) | 57.5 → 62.5 % | 75 % | 90 % |
| dev corpus (8) | 78 % | 96 % | 100 % |
| generated corpus (7) | 60 → 58 % | 84 % | 96.5 % |
| *fresh* Defects4J, seed 3 (18) | 47 → **67 %** | 71 → **94 %** | 94 → 98 % |
| *fresh* SWE-bench, seed 2 (16) | 82 → 86 % | 86 → 91 % | 95 → 100 % |

Two dev cases lost a line at 600, both to a failure line the change made
visible: maven-test shows its failing test's name instead of the
exception message, and Collections-7 shows a `ComparisonFailure` from a
test Defects4J does not list instead of the `NoSuchElementException` it
does.

What the first held-out sets still show:

- **600 on SWE-bench.** pytest's per-test sections and summary both name
  the test; at 600 the budget holds the first failure's traceback and a
  few names. django__django-11451 went from 2 of 6 to 1 of 6.

### JUnit 3 assertion messages

Found on Defects4J seed 4, the fourth fresh sample (it is spent now). Its
three misses at 600 were all JUnit 3 assertion messages, and JUnit 3 under
old Surefire prints a failing test's message only on the exception line:
the "Failed tests:" summary lists names alone. Two things kept that line
out.

**Frame names read as error words.** `at junit.framework.Assert.fail(…)`,
`Assert.failNotEquals`, `Assert.assertEquals`: the method names contain
`fail` and `assert`, so each frame scored as an error line (6.8 for the
first, with the location bonus, against 3.0 for the exception line above
it), and two or three of them took the room the second message needed.
A stack frame's words are code names, so on a frame line (`at pkg.Cls.m(…)`
in Java, JavaScript and C#, Python's `File "…", line N, in f`) the error
and warn vocabulary no longer counts (`STACK_FRAME_NAMES`). A compiler's
`src/x.c:8:14: error: …` is a location, not a frame, and keeps its score.

**A one-off warning outranked a failure seen twice.** Rarity adds 2.0 to a
template seen once and 1.0 to one seen twice, so Maven's POM warnings
(`'build.plugins.plugin.version' … is missing`, `${pom.version} is
deprecated`, each printed once) beat two tests failing with the same
message (JxPath-19). A warning-level template's rarity now counts three
quarters (`WARN_RARITY`): a singleton warning (2.25 + 1.5) ranks below an
error seen twice (3.0 + 1.0). Half was tried first; it gained the same
Defects4J lines but cost android-logcat a real warning at 1,000 (`note 117
has no 'updated' field, skipping`), and three quarters gains them all
without that loss. The value was chosen on these sets, so it was a
training number until a fresh sample said otherwise. Defects4J seed 5,
drawn afterwards and measured before reading, says it holds: 80.4 → 87.0 %
at 600 (Codec-5, Math-101 and Cli-12 one line each), nothing changed at
1,000 or 2,000, no case worse (`corpus-heldout/README.md`).

Gains per set, before → after (pare `821097f` → this change); no case lost
a line anywhere, at any budget:

| set | 600 | 1,000 | 2,000 |
|---|---|---|---|
| Defects4J seed 4 (18, found the problem) | 92.3 → **100 %** | 100 % | 100 % |
| Defects4J seed 3 (18) | 82.4 → 88.2 % | 98 % | 98 % |
| Defects4J seed 2 (16) | 79.5 → 90.9 % | 93.2 → 95.5 % | 100 % |
| Defects4J seed 0 (11, dev) | 83.9 → 90.3 % | 100 % | 100 % |
| Defects4J dev, seed 1 (19) | 100 % | 100 % | 100 % |
| dev corpus (8) | 76.5 → 77.8 % | 96.3 % | 100 % |
| generated corpus (7), SWE-bench seeds 0–2 | unchanged | unchanged | unchanged |

Seed 4 alone: the frame rule gives Codec-2 its message (92.3 → 94.9 %);
the warning discount gives JxPath-19 its message and Gson-8 its Surefire
summary line (→ 100 %). JacksonCore-10, whose `expected:<1024> but
was:<2048>` messages were left alone earlier as numbers-only repeats,
gains one at 600 and one at 1,000 from the frame rule. Both rules have
ablation rows (`no warning rarity discount`, `frame names as
vocabulary`), and each costs seed 4 lines when switched off.

### Messages that differ in one word

Found on Gson-7 (Defects4J seed 5, spent now). Two tests fail with
`JsonSyntaxException … Expected an int but was STRING …` and `… Expected
a long but was STRING …`, and Surefire 3's error summary prints each on
one line. drain makes the two lines one template, and pare showed it as
the template's pattern (`Expected <*> <*> but was STRING …`) and one
example: the word that differs was the one the pattern hid. Three
causes, one fixed in the labels:

- **A template seen twice lists both lines** (`PAIR_MEMBERS`), at budgets
  where entries get more than one line (1,000 and up). The pattern and an
  example cost what two lines do, and the pattern only says what the two
  share. A template seen three times or more keeps its pattern, and one
  whose two lines read the same shows one.
- **A clip that saves nothing is not made.** At `--width 200` a 202-
  character line became `…path…(+2c)`, longer than the line, and lost
  the `$.` its keep line ends with. `clip` now leaves a line whole when
  the marker would be no shorter than what it cuts (at most 6 characters
  over the width).
- **Surefire 3's `Cls#testX` form was not a label form**
  (`corpus-heldout/README.md`, "Labels"): Gson-7's third test was on
  screen and scored as missing.

With the new labels on every set, before → after (pare `27c905c` → this
change); no case lost a line on any set at any budget:

| set | 600 | 1,000 | 2,000 |
|---|---|---|---|
| Defects4J seed 5 (17, found the problem) | 91.3 → 93.5 % | 93.5 → **97.8 %** | 100 % |
| generated corpus (7) | 59.6 % | 87.7 → 89.5 % (maven-test) | 98.2 % |
| every other set (Defects4J 0–4, dev, SWE-bench 0–3) | unchanged | unchanged | unchanged |

The pair rule gives Gson-7 its `Expected a long` line at 1,000 and
maven-test one line; the clip gives Gson-7 its `Expected an int` line at
600 and 1,000. Outputs at 2,000 move by under 1 %. At 600 Gson-7 still
misses its third test's message: entries are one line there, and the
second summary line (240 bytes) does not fit. The ablation row `pattern
for a pair` switches the pair rule off.

### Generated corpus: real tools, chosen failures

`corpus-generated/` holds output captured from real Maven, tsc, Terraform
and kubectl runs on small projects written to fail (sources in its `src/`,
provenance in its README). The tools and their formatting are genuine; the
failures and the keep lists are not blind, so this is a second development
corpus for formats `corpus/` lacks, and it does not count toward the
held-out set. Nothing was tuned on it. Current pare scores:

| case | in tok | budget 2000: out, recall | budget 600: out, recall |
|---|---:|---|---|
| maven-compile (cold cache, 495 lines) | 22,628 | 910, 9/10 | 593, 7/10 |
| maven-test (Surefire, 128 lines) | 3,763 | 1,559, 9/9 | 593, 4/9 |
| tsc-build (35 errors, 49 lines) | 1,614 | pass-through, 13/13 | 598, 7/13 |
| terraform-init (20 lines) | 287 | pass-through, 4/4 | pass-through, 4/4 |
| terraform-validate (37 lines) | 473 | pass-through, 8/8 | pass-through, 8/8 |
| terraform-apply (plan + apply, 462 lines) | 5,922 | 1,086, 9/9 | 597, 1/9 |
| kubectl-apply (unreachable API server, 18 lines) | 1,388 | pass-through, 4/4 | 391, 3/4 |

Overall 98 % at 2,000 (7,317 tokens, 20 % of the input) and 60 % at 600
(3,532 tokens). When the heuristics were frozen it was 96 % (7,555
tokens) and 56 %; the gain since is the tail-window change ("Maven
chatter", "The windows too"), which gave each Maven case a line at 600
and maven-compile its third compile error at 2,000. sqz keeps 74 % of
these lines at 20 % of the input,
tidyout everything at 100 %: short, signal-dense logs are where a
600-token summary has the least room, and where sqz's line-level
compression competes. Observations, recorded rather than fixed:

- **maven-compile at 2000** misses one line: the reactor summary's
  `shop-api ... FAILURE` is a plain `[INFO]` line whose only signal is
  the word FAILURE inside a dotted leader, and it ranks below the error
  entries that are shown. Until the tail-window change it also missed a
  compile error: the second and third share one template (`incompatible
  types: <*> cannot be converted to <*>`, ×4 because Maven prints the
  block twice), so only the exemplar was shown and the `[22,16]` sibling
  was in the ledger. With the closing advice skipped, the tail window
  reaches Maven's second printing of the errors and shows both. Before
  the template floor the entry table (43 templates) exhausted the budget
  with no selected lines; the floor halved the output.
- **maven-test at 600** keeps the Surefire results block (both failure
  summaries with file:line) and, since the tail-window change, `There
  are test failures.`, but not the stack-trace lines; 600 real tokens
  is below the skeleton plus nine keep lines here.
- **terraform-apply at 600** keeps the `Warning: Check block assertion
  failed` template but loses the three assertion messages and the plan and
  apply summary lines; the plan diff supplies 20 near-identical resource
  blocks that the template table spends the budget on. With the gutter
  change the `Apply complete!` line dropped out too (1 of 9): what fits
  shifted by a few bytes, in the wrong direction here.
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

About 0.9 s on the 750 KB / 17,832-line CI log (1.5 s before the
shortcuts below). Clustering itself is under 0.05 s of that (the built-in
Drain over every line of the log; drain3 took 0.065 s); the rest is
masking, the vocabulary and severity checks, and Python's per-line
overhead. Start-up, with nothing to read, is about 0.08 s (0.12 s while
pare imported drain3).

**Shortcuts in the per-line checks.** Each line was searched by about
twenty regexes: one per vocabulary class (a case-insensitive alternation
of its words, tried at every position, was the slowest), the severity
forms, the zero-count rule, and on record heads twelve masks. Most lines
match almost none of them, so each now has a cheap test that must pass
before the regex runs, and every test is a necessary condition, so no
result changes:

- A class's plain words: a word matches only where the line has a whole
  run of letters equal to the word's first run (a non-letter on each side),
  so the line's runs of letters are collected once and a class none of
  whose first runs is among them is skipped.
- Each built-in `/regex/` entry, the HTTP status, exit code and level-key
  forms and the zero-count rule: a short list of lowercase strings one of
  which the line must contain (`REGEX_NEEDS`, `HTTP_NEEDS`, ...).
- A bare level token is a whole run of letters in the scanned head.
- Each mask: a digit, a character or a word the line must contain (a `:`
  for a time, a `.` for an IP or a version, `-`, `/` or a month name for a
  date), checked against the line as the earlier masks left it, so a time
  pass is skipped once the timestamp mask took the colons.
- ANSI stripping only on lines with an escape character.

The letter-run and lowercase tests hold for lines whose non-ASCII
characters cannot match an ASCII letter case-insensitively and are not
whitespace (`plain_text`: a ✓ is fine, the Kelvin sign, ſ, ß and ligatures
are not, judged per character by the running Python); other lines take
every regex as before. A user vocabulary's `/regex/` entries have no
strings listed and always run. `test_pare.py` checks every test against its
regex on sampled corpus lines and on random awkward lines, and checks that
every class and the severity come out as without shortcuts, with and
without a user vocabulary; every log in the repository, at four budgets,
with `--only` and in follow mode, gives byte-identical output before and
after. The random lines found one crash, now fixed: case-insensitive
matching takes the Turkish İ for an i, so `İNFO:` matched a level token
whose uppercase form is not a level, and pare stopped with a `KeyError`.

**Memory.** The prototype makes a single pass over stdin. Each chunk is
written to the spool (the run file, or a temporary file under `--no-tee`)
as it is read, split into lines, and each line is segmented, clustered and
scored on arrival. What survives the pass is a set of typed arrays with
about 40 bytes per line (spool offset, record, depth, severity, rendered
size, score) and per record, plus the templates; a line's text is read
back from the spool only when the allocator looks at it, through a small
cache of materialised lines. The output is byte-identical to the earlier
all-in-memory design (checked against it on every corpus file, budget and
flag combination). Measured on the CI log concatenated to size, when the
single pass was built:

| input | wall time | peak memory | before (all in memory) |
|---|---|---|---|
| 0.75 MB | 1.9 s | 29 MB | 2.3 s, 46 MB |
| 7.5 MB | 13.6 s | 41 MB | 19 s, 231 MB |
| 30 MB | 54 s | 65 MB | 76 s, 849 MB |

With the shortcuts, on a 30.8 MB log of real corpus logs (CI, service,
logcat, pytest) repeated with each copy's digits rotated, so fewer lines
repeat exactly, and its first 7.5 MB:

| input | wall time | peak memory | before the shortcuts |
|---|---|---|---|
| 0.75 MB (CI log) | 0.9 s | 30 MB | 1.5 s, 29 MB |
| 7.5 MB | 6.6 s | 40 MB | 12.8 s, 40 MB |
| 30.8 MB | 27.7 s | 64 MB | 54.7 s, 63 MB |

Time is still linear in the input, now about 0.9 s per MB, and it is
spent as the input arrives: of the 27.7 s on 30.8 MB, 0.3 s comes after
end of input (the summary itself). A command that prints slower than
about 1 MB/s, most builds and test runs, gets its summary a fraction of
a second after it exits; the whole time shows when the input is already
on disk (`pare < big.log`) or arrives faster than that.

What remains is Python per line, and most of it is regex work on lines
that do hold a candidate: record heads nearly all carry a timestamp, so
most masks run on them, and sharper mask tests saved 8 % of mask time.
File I/O is small: reading 30 MB in 1 MB chunks, writing the run file,
hashing it and splitting it into lines takes about 1.4 s, 5 % of the
total, and reading lines back for the summary is part of the 0.3 s after
end of input. Two larger levers are left. The per-line checks (severity,
vocabulary, masks: about two thirds of the time) depend only on a line's
text, so worker processes could compute them in parallel ahead of the
sequential pass; that helps only input arriving faster than pare reads
it, at the cost of a second code path. And a native port, where the
next large factor is. The candidate list for the
allocator holds every non-blank line scoring at or above the floor, as
one integer per line, which is the one structure still proportional to
line count; the raw bytes are never held in memory beyond one chunk.

pare still reads to end-of-file before it writes anything, because the
template counts, the tail window and the rarity term all depend on the
whole input. A long-running producer sees the summary when it exits; an
endless stream never gets one. That is a different mode, sketched below.

## Follow mode

Batch pare answers "what happened" once the command has exited. A
`tail -f`, a long server log or a two-hour build needs a different
question answered on a schedule: "what has happened since I last looked".
`pare --follow` is that mode.

**Shape.** The pass is an object (`Pass`) that is fed chunks and cut into
windows. Follow mode reads stdin with `select` and `os.read`, feeds each
chunk, and at each flush cuts the current window into a `Store` and
summarises it with the batch summariser, which knows the window's line
offset. Each block is in the batch format, budgeted on its own, and ends
with a blank line so a consumer can split the stream:

```
# pare #2: L4,001–L8,023 · 4,023 lines, 152,938 B (~54,621 tok) → ~591 tok (budget 600) · 13 templates, 10 new
## templates (by salience)
T26  ×1 new E L5297: webpack 5.95.0 compiled with 1 error and 1 warning in 94213 ms
T24  ×1 new E L5288: ERROR in ./src/store/cartSlice.ts 88:31
T30  ×1 (34 lines) new E L6888: FAIL src/api/useFetch3.test.tsx
  6914│   ✕ formats total #25 (22 ms)
  ...
## tail (L7994–L8023)
  ...
## omitted 4,007/4,023 lines (99.6%): T29 ×2,557, T20 ×1,223, T23 ×60, T30 ×29 · +2 more templates; 4 templates not listed; 135 blank
## raw: /tmp/claude-0/-home-user-pare/851c91f1-3f2c-5677-9ebd-8a9b07052856/scratchpad/runs/follow-20261009-005020-1315.log --since L4001 --until L8023
```

**Flush conditions.** Any of: `--every N` lines (default 20,000, which
is also the window cap when another trigger is in force), `--interval S`
seconds (the read loop's `select` timeout, so an idle stream still
flushes on time), end of input, and a line at `--flush-on` severity
(default `error`; `fatal` or `none`), which schedules a flush `--settle`
seconds later (default 0.5, so the rest of the record can arrive on a
live pipe that delivers a line at a time) and no sooner than
`--min-interval` after the previous flush (default 2 s), so an error
storm is not a flush storm.

**Windows end at record boundaries, or carry the record.** The line
cap is checked when a line arrives that starts a record; a continuation
line never closes a window, which may therefore run past `--every` by one
record (at most `MAX_RECORD_LINES`). A timed cut (`--interval`,
`--flush-on`) cannot wait, so it leaves the open record open: the pass
keeps the head's indent, vocabulary, severity and the error-location
state, and the first lines of the next window that would have continued
the record form its continuation there (`Store.carry`), attributed to the
same template without another drain call. Those frames inherit the error
head's salience as they would have in one pass, the entry shows the
carried head (`T2 ×1 (34 lines) (cont.) E L2: ERROR worker crashed …`),
and the segmentation, so the head lines drain sees and their order, is
the same as in one batch pass over the finished run file. A carried
record that no line of the next window has ended yet (the window held
only the blank line that keeps it open) is handed on again unchanged,
length and pending-blank state included; and follow mode neither prints
nor cuts a window that holds only blank lines, so the blank stays in the
open window and is adopted into the record as in a batch pass. The
template ids match: one test checks it line by line on the CI log at a
500-line window, another cuts inside a trace and compares with the batch
pass, an end-to-end test streams a trace through `--interval` with a
pause in the middle, and a randomized differential test (a generated log
with traces, blank lines inside blocks, nested indentation and pytest
`E`/`>` lines; a cut after every line, then a dozen seeded trials of
random cuts plus cut pairs around in-block blanks) asserts that every
non-blank line has the same record head and global template id as one
batch pass. That test found the blank-only-window case that the
hand-written ones had not, and it carries over to a native port as is.

The one documented difference: a single blank line pending at the moment
of a cut is adopted into the record by the batch pass but stays
unattributed in the window already printed, since that block is out. It
moves that line between the ledger's record count and its blank count,
nothing else.

**What carries across windows.** The Drain miner, one small record per
template (`GlobalTemplate`: id, cluster, cumulative count, first window),
the line counter, the spool position, and the run file. The run file is
named up front (`follow-<date>-<pid>.log` under the run directory, or
`--run-file`), since the content hash is not known while the stream
runs, and appended as chunks arrive; every block names its
`--since/--until` range for it.

**What changes in scoring.** Rarity uses the cumulative count
(`Template.total`), not the window's. From the second window on, a
template first seen in the window is marked `new` and its salience gets
`NOVELTY_BONUS` (1.0); the head window is dropped (the input's head was
in the first block) and so is the usage hint. A template with at least
`RATE_MIN_COUNT` (20) records in the window arriving at `RATE_FACTOR` (3)
times its running rate gets a rate note (`(rate 0.2/s → 11/s)`); rates
need at least a second on each side, so a replayed file gets none and
tests stay deterministic.

**Heartbeats.** From the second window on, a window with no new template
and no line at or above the salience floor is one line: `# pare #4:
L6,731–L8,002 · 1,272 lines, nothing new · T3 ×1,270, T5 ×2`. `--quiet`
drops it. A window that fits the budget is passed through verbatim under
its header line, as in batch mode; when it starts inside a carried
record, a second line `↳ continues L1: ERROR worker crashed: …` names
the head its first lines belong to, so a reader or an agent is not
handed bare stack frames. Since a verbatim block can contain the input's
own blank lines, a consumer should split the stream on the `# pare #`
header lines rather than on blank lines.

**Memory.** The per-window arrays are the single pass's arrays, dropped
at each cut, so memory is bounded by the window size; cumulative state
grows only with new shapes. The 30 MB input from the table above runs in
34 MB of peak memory in follow mode with 20,000-line windows (36 blocks,
60 KB of output in total at budget 600), against 65 MB in batch mode.

**Not built: rolling mode.** A delta stream suits a terminal or a harness
that streams tool output. An agent that runs a long command in the
background and polls a file would be better served by a budgeted summary
of the *whole* run so far, rewritten in place at each flush
(`--rolling --out digest.md`). That needs a bounded reservoir of
candidate lines instead of the window arrays: for each repeat key the K
highest-scoring lines, with whole keys evicted by their best score when
the key count passes a cap. The rolling summary would approximate the
batch summary rather than match it; batch mode on the run file afterwards
is still the exact one.

## Notes for a native port

`bench/parity.py --candidate CMD [--dump]` is the check for a port: every
corpus log and a set of generated edge cases under 14 flag sets, outputs
and exit codes byte for byte, and with `--dump` the pass's per-line and
per-template results (`$PARE_DUMP`), reported from the first row that
differs. The plan and its stages are in `ROADMAP.md`, stage 5.


- Template ids must stay stable across versions, or `--only T3 < runfile`
  breaks: a native Drain must match the built-in one (drain3 0.9.11's
  clustering) exactly, or the change is a breaking one and should say so.
  The built-in `Drain` class, about 100 lines, is the reference, and its
  test against drain3 is the template for the port's.
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
