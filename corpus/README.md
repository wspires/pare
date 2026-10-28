# The corpora

Logs pare is measured on. Each case is three files with one name:
`<name>.log`, the raw output as the tool printed it; `<name>.keep`, the
lines that must survive summarisation, one per line; and `<name>.cmd`,
the command that produced the log. A keep line counts when its stripped
text appears whole in the output, not run on into a longer name, and
`a || b` lists forms that each count; `bench/run.py` applies the rule
and its docstring has the details. A directory may also carry a
`MANIFEST.tsv` (where each case came from) and a `README.md`.

| directory | what it is | tuned on? |
|---|---|---|
| `dev/` | nine logs: real pytest, go test, cargo, make and npm runs, three seeded synthetic logs and a synthetic Android logcat | yes: the misses here were fixed by looking at them |
| `generated/` | real Maven, tsc, Terraform, kubectl and structured-log output from the small projects in its `src/` ([README](generated/README.md)) | yes: a second development corpus |
| `defects4j-dev/` | the Defects4J development sample, seed 1 ([README](defects4j-dev/README.md)) | yes |
| `heldout/` | SWE-bench and Defects4J samples drawn fresh and measured before anyone reads them ([README](heldout/README.md), with the protocol and every result) | no: a sample is spent once a change has been judged on it |

```
.venv/bin/python bench/run.py --corpus corpus/dev --pare-bin target/release/pare
.venv/bin/python bench/run.py --corpus corpus/heldout/swebench-6 --pare-bin target/release/pare --budget 600 --show-misses
```

The README's Results section reports the development and held-out
numbers apart, and never merges them.
