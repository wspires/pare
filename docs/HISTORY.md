# pare: history of the heuristics

What was tried, in the order it was tried, with the numbers at the time.
[DESIGN.md](DESIGN.md) states each rule as it stands now and points here
for its evidence; `corpus/heldout/README.md` has every held-out sample
with its misses; ROADMAP.md says what came next. Sections are kept as
written, with a note where a measurement has since been superseded.
Budgets are in tokens at bytes / 2.8, the rate until 29 October 2026
(DESIGN.md, "Tokens and the budget"), unless a section says bytes/4, the
unit the heuristics were first developed in.

## The benchmark when the heuristics were frozen

*pare 0.1.0, the Python prototype, October 2026, on the eight logs
`corpus/dev/` then had. Current numbers: the README.*

Methodology: CONTRIBUTING.md, "Benchmark". Versions: pare 0.1.0 with drain3 0.9.11 (now built in), sqz
1.9.0, tidyout-cli 0.2.0. Modes: pare `--no-tee`; sqz
`compress --no-cache --cmd <name>`, one-shot, so its session-level dedup is
not exercised; tidyout `<cmd>` with a fake `<cmd>` on PATH that replays the
log, so its per-command rules apply.

### Budget 2000, per case (bytes/4 units, `--chars-per-token 4`)

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

### Budget 600 (bytes/4 units)

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

*Moved here from DESIGN.md when the default became 2.0 (29 October 2026);
the current measurement, on Claude's tokenizer, is in DESIGN.md, "Tokens
and the budget".*

Why 2.8: `bench/tokens.py` counted the corpus and pare's output with
OpenAI's cl100k_base and o200k_base through tiktoken, and with the Claude
tokenizer JSON that litellm ships (the older public one with a 65k
vocabulary; the tokenizer behind current Claude models is not published).
Bytes per token, totals over the eight corpus files the corpus then had:

| text | cl100k | o200k | Claude (legacy) |
|---|---|---|---|
| raw corpus, 1.37 MB | 2.59 | 2.59 | 2.43 |
| pare output at budget 600 | 2.86 | 2.85 | 2.80 |
| pare output at budget 2000 | 2.84 | 2.83 | 2.74 |

Per file the raw logs ranged from 2.1 bytes per token (the synthetic
service log) to 3.8 (the pytest run); pare's output sat in a narrower
band of 2.6 to 3.2. So bytes/4 undercounted pare's output by about
40 %: a budget of 600 produced roughly 850 real tokens and 2000 roughly
2,850. The raw input is denser still, so the compression ratios were, if
anything, conservative: in cl100k tokens pare's budget-2000 output was
2.0 % of the input (10,665 of 527,288) against 2.2 % by bytes/4, and the
budget-600 output 1.1 % against 1.3 %. One file (go test, 2.6 bytes per
token) still exceeded 600 by 7 % at 2.8.

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
| android-logcat (added later; not in the totals above) | 95,542 | 1,979, 8/8 | 596, 4/8 (5/8 since "JUnit 3 assertion messages" below) |

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

The failing-test fix ("First change from held-out evidence" below) cost
one line here: at 600, pytest-fail now shows the failing assertion's
location (`tests/test_cart.py:18: AssertionError`, a different exception
type from the template's exemplar, so no longer a repeat) and the last
exception line misses the budget by 11 bytes; 9/11 became 8/11.

## How the misses were fixed

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

## Validation status when the heuristics were frozen

*As written before any held-out sample existed. Item 1 was answered in
part: the held-out samples that follow are drawn from datasets, and logs
from real agent sessions are still wanted (ROADMAP.md, "Real logs").
Item 2 was run on 29 October 2026 with `bench/agent_eval.py`, twice
(`corpus/heldout/README.md`, "Agent-success evaluation"); what is still
open is the same run on held-out cases (ROADMAP.md).*

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
   `corpus/heldout/README.md` has the protocol; `bench/run.py --corpus
   corpus/heldout` reports it separately.
2. An agent-success evaluation: a few real tasks (fix a failing test, fix a
   broken build, find an outage cause) run with the full output, with pare,
   with sqz and with tidyout in the agent's context, reporting task success
   and tokens side by side.

## First change from held-out evidence

The first held-out numbers (SWE-bench Verified and Defects4J, labelled by
the datasets; `corpus/heldout/README.md`) showed one failure on both
sources: a run with several failing tests lists each in a line of the
same shape, and pare showed one exemplar and a count. Two changes, both
in DESIGN.md, "Allocation" and "Vocabulary":

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
held-out set: `corpus/defects4j-dev/`) and checked against both dev
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
`corpus/heldout/README.md`.

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
listing them compactly (DESIGN.md, "Allocation", the list form). Developed on the
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

## Maven chatter

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

### The verdict that repeats a shape

SWE-bench seed 4
found the cost of the windows' skip: pytest's own test suite prints the
output of the inner pytest runs it drives, so pytest-dev__pytest-8399's
log has dozens of `FAILED` lines from those, and the one real `FAILED
testing/test_unittest.py::test_fixtures_setup_setUpClass_issue8394`, line
1,416 of 1,417, was folded into `┆ (1 line)` at every budget while the
`PASSED` and `SKIPPED` lines around it were shown. Two rules were tried
on all 220 cases at 600, 1,000, 2,000 and `auto`:

| tail-window rule | lines gained | lost | outputs changed |
|---|---|---|---|
| keep every error line, however common | 6 | 8 (android-logcat, ci-pipeline, matplotlib-25775, django-13212) | 61 of 880 |
| keep an error line when it is the only one of its template in the window's span | 4 (pytest-8399 at each budget) | 1 (ci-pipeline at 1,000) | 12 of 880 |

The first undoes what the skip was for: an emulator's routine errors
(`GnssHAL`, `netmgr`) recur to the last line of android-logcat, and kept,
they fill its tail. What tells the verdict apart is that its shape is
common earlier but appears once near the end; noise that recurs keeps
recurring. So the second rule shipped. Its one loss is a budget edge: at
1,000 ci-pipeline's tail gains a kubectl `Warning BackOff … Back-off
restarting failed container` event (one of its shape near the end), and
a stack frame no longer fits. Totals: 600 501 → 502, 1,000 559 → 559,
2,000 588 → 589, `auto` 581 → 582 of 595, output sizes within 0.1 %.
SWE-bench seed 4 is spent by this change; the rule's fresh test is the
next sample.

It is the first change to the default output of plain-text logs since
the Python version was frozen, so `bench/parity.py` and the golden-file
test run with `PARE_REFERENCE_RULES=1`, which turns off the Rust-only
rules that change default output (this one, and structured lines) and
holds everything else to the Python version byte for byte; the rules
have tests of their own.

## Android logcat entries

*Found on the synthetic logcat case (`corpus/dev/android-logcat`), added to
exercise the logcat level rule when the tool-specific rules were
rechecked (DESIGN.md, "Tool-specific rules").*

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

## Performance of the Python version

*The Python reference, `reference/pare.py`, when it was the shipped
version. The Rust version's numbers are in DESIGN.md, "Performance".*

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
endless stream never gets one. That is a different mode: follow mode (DESIGN.md).

## Notes for a native port

*Written before the port, as guidance for it. What was built is in
ARCHITECTURE.md; the port's stages are in ROADMAP.md, stage 5.*

`bench/parity.py --candidate CMD [--dump]` is the check for a port: every
corpus log and a set of generated edge cases under 14 flag sets, outputs
and exit codes byte for byte, and with `--dump` the pass's per-line and
per-template results (`$PARE_DUMP`), reported from the first row that
differs.

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

## JUnit 3 assertion messages

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
1,000 or 2,000, no case worse (`corpus/heldout/README.md`).

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

## Messages that differ in one word

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
  (`corpus/heldout/README.md`, "Labels"): Gson-7's third test was on
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

## Generated corpus: real tools, chosen failures

`corpus/generated/` holds output captured from real Maven, tsc, Terraform
and kubectl runs on small projects written to fail (sources in its `src/`,
provenance in its README). The tools and their formatting are genuine; the
failures and the keep lists are not blind, so this is a second development
corpus for formats `corpus/dev/` lacks, and it does not count toward the
held-out set. Nothing was tuned on it. Scores as of the tail-window
change, seven cases (four structured-log cases were added later:
DESIGN.md, "Structured lines"):

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

## The Claude token rate

*29 October 2026. The measurement and the new default are in DESIGN.md,
"Tokens and the budget"; the first calibration is above, "Calibrated to
real tokens".*

`bench/tokens.py --anthropic claude-opus-5-5` counted pare's output
through Anthropic's `count_tokens` API: 1.96 bytes per token on the nine
dev-corpus files, against 2.82 for cl100k and o200k, so a `--budget
2000` summary had been reaching a Claude agent as about 2,870 tokens.
The default moved from 2.8 to 2.0 the same day. At the same nominal
budgets the summaries are 30 % smaller, and the recall benchmark pays
for it where the budget was already tight: 25 points at 600 on the dev
corpus, 16 and 17 on the latest Defects4J and SWE-bench samples, one to
five at 2,000. Every number in this file and in the held-out record
before that date is at 2.8; the README's tables were restated at 2.0,
and 600 stopped being a budget to recommend. The agent-success
evaluation was rerun at the new rate (the held-out README's second run)
with the same outcome on every task at fewer tokens overall.
