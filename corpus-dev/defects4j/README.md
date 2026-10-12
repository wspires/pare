# Defects4J development sample

Nineteen failing Maven test runs rebuilt by `bench/defects4j_cases.py`
with `--sample 28 --seed 1` (Codec-16 removed: it is in the seed-0 set in
`corpus-heldout/defects4j/`). Used to develop the failing-test fix, so it
is a **dev** set: tune on it freely, and keep its bugs out of any new
held-out sample with `--exclude corpus-dev/defects4j`.

```
python3 bench/run.py --corpus corpus-dev/defects4j --budget 600 --show-misses
```

pare before and after the fix: 43 % → 68 % at 600, 75 % → 91 % at 1,000,
95.5 % → 100 % at 2,000.
