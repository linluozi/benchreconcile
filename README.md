# benchreconcile

`benchreconcile` audits a performance-optimisation repository by **recomputing its
numbers from the raw per-sample timings** and comparing them with the committed
aggregates and the Markdown table in the README. It is built for the common failure
mode where a lab writes only `baseline.json` / `optimized.json`: those aggregates are
not evidence, they are claims about evidence.

Upstream: none

The tool never trusts an aggregate. For every workload it re-reads
`benchmark/raw/*.json`, checks that warm-up and sample counts meet the stated
minimums, recomputes median / p25 / p75 / relative stdev with the same
`statistics` definitions used to write the aggregate, and reports a deterministic
percentile-bootstrap 95% confidence interval for each speedup. A README row whose
number cannot be reproduced from the raw files is an error, not a rounding note.

## Install and run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest
python -m pytest -q                       # 11 tests
python -m benchreconcile path/to/lab-repo
```

Machine-readable report, and a strict run where warnings also fail:

```bash
python -m benchreconcile path/to/lab-repo --json report.json
python -m benchreconcile path/to/lab-repo --strict
```

Exit status is `0` for a clean audit, `1` for any error (or for any warning under
`--strict`). A tampered aggregate cannot pass silently: the raw files are the source
of truth.

## What is checked

| code | severity | check |
|---|---|---|
| E001 | error | required aggregate / directory missing |
| E002 | error | fewer than 5 usable raw samples, or no `raw` list |
| E003 | error | warm-up below 3 |
| E004 | error | raw sample lacks `impl`/`workload`/`rep`/`seconds`/`seed`/`python`, or impl/workload disagree with the aggregate |
| E005 | error | committed median / p25 / p75 / rel-stdev cannot be recomputed from raw |
| E006 | error | README speedup or "median of the three scales" differs from raw by >2% |
| E007 | error | repo named `*-opt-lab` has no real-corpus speedup row |
| E008 | error | real-corpus speedup is below 1.15x |
| E010 | error | `patch.diff`, `LICENSE` or `tests/test_diff.py` missing |
| E011 | error | no `Upstream:` line in the first 2500 README characters |
| W001 | warn | relative stdev >10% |
| W002 | warn | committed median equals the single fastest sample |
| W003 | warn | raw-file count, reps, or seeds are inconsistent |
| W004 | warn | >10% stdev is not discussed in the README |
| W005 | warn | baseline and optimised sample counts differ |
| W006 | warn | 95% bootstrap CI for the speedup includes 1.0 |
| W007 | warn | `patch.diff` touches more than 80 lines |

## Real dogfood run

The first real target is `linluozi/inflect-opt-lab` (upstream `jaraco/inflect`,
commit `2084a52`), cloned at the time of the audit. Command and full output are in
`evidence/inflect-audit.txt` / `evidence/inflect-audit.json`:

```text
$ python -m benchreconcile /tmp/ev-inflect --bootstrap 2000
workload                 baseline ms   optimized ms   speedup   95% CI
large                         74.897         26.819    2.793x  [2.75, 2.89]
medium                        29.570          9.913    2.983x  [2.92, 3.08]
real_mixed                   134.823         69.774    1.932x  [1.88, 2.01]
small                          9.148          3.042    3.007x  [2.56, 3.25]

0 error(s), 0 warning(s); status=PASS
```

All four README rows and the "median of the three scale speedups: 2.983x" claim were
reproduced from the 28 raw sample files, not from the aggregates. This is a dogfood
case, not an independent third-party audit: the lab and the auditor are by the same
author.

To show the auditor is not a rubber stamp, a **copy** of that repo had
`optimized.json`'s `small.median_ms` changed from 3.042 to 1.5 while every raw file
was left untouched. The committed median no longer matched the raw samples and the
audit failed with exit 1 (`evidence/tamper-detection.txt`):

```text
ERROR E005 [optimized.json:small]: committed median_ms=1.5 but raw gives 3.042277880012989
1 error(s), 0 warning(s); status=FAIL
```

## Scope and limitations

- The schema is deliberately small: `benchmark/baseline.json`,
  `benchmark/optimized.json`, and raw samples with `seconds` plus provenance fields.
  Repos that only publish aggregates need an adapter first.
- The bootstrap interval describes sampling noise **within the supplied raw files**.
  It cannot detect a benchmark that was run on a different machine, with a different
  seed policy, or against the wrong tree; that requires re-running the benchmark.
- W006 is a warning, not proof of fraud: a true 1.05x effect can legitimately have a
  CI that includes 1.0. It is a prompt to collect more samples.
- README parsing understands the five-column `| workload | n | base | opt | Nx |`
  table and an explicit "median of the three scale speedups" sentence. Free-form
  prose claims are not parsed; absence of a parsed claim is not a pass.
- It does not execute upstream tests or `git apply --check`; those are separate gates.

## 本轮创新

以前只有 asv / pytest-benchmark 这类**产出**数据的工具，或人工阅读 README；没有工具把
「README 声明 → 聚合 JSON → 逐样本 raw」三方逐条对账，并用确定性 bootstrap 给出速度比的
95% 置信区间。新在把性能声明当成可证伪命题：改一个聚合数字、删一个 raw 文件、把
median 换成 min、或让 CI 跨过 1.0，都会被具体 code 定位。与台账里对第三方库做优化/审计的
条目不同，这是对**优化证据本身**的审计器。

## License

MIT, original work. No upstream code is vendored.