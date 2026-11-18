"""Behavioural tests for the claim/raw reconciliation audit.

Fixtures are generated into tmp_path; no timing numbers are hard-coded as
"real".  Each test builds a repository with a controlled relationship between
raw samples, committed aggregates and README claims, then asserts the exact
finding code the auditor must emit.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from benchreconcile.cli import main
from benchreconcile.core import audit

SEED = 20261118


def _stats(values: list[float]) -> dict[str, float]:
    q = statistics.quantiles(values, n=4, method="inclusive")
    mean = statistics.fmean(values)
    return {
        "median_ms": statistics.median(values) * 1e3,
        "p25_ms": q[0] * 1e3,
        "p75_ms": q[2] * 1e3,
        "rel_stdev": statistics.pstdev(values) / mean,
    }


def _write_impl(
    root: Path,
    impl: str,
    samples: dict[str, list[float]],
    *,
    warmup: int = 3,
    override_median: float | None = None,
) -> None:
    scales = []
    for workload, values in samples.items():
        raw_refs = []
        for i, seconds in enumerate(values):
            ref = f"benchmark/raw/{impl}-{workload}-{i}.json"
            raw_path = root / ref
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                json.dumps(
                    {
                        "impl": impl,
                        "workload": workload,
                        "rep": i,
                        "seconds": seconds,
                        "seed": SEED,
                        "python": "3.12.3",
                        "rss_kb": 40000,
                    }
                ),
                encoding="utf-8",
            )
            raw_refs.append(ref)
        scale = {
            "workload": workload,
            "warmup": warmup,
            "reps": len(values),
            "raw": raw_refs,
        }
        scale.update(_stats(values))
        if override_median is not None and workload == "small":
            scale["median_ms"] = override_median
        scales.append(scale)
    (root / "benchmark" / f"{impl}.json").write_text(
        json.dumps({"impl": impl, "scales": scales, "reps": 7}), encoding="utf-8"
    )


def _readme(
    root: Path,
    rows: list[tuple[str, float]],
    *,
    median_claim: float | None = None,
    upstream: str | None = "Upstream: none",
) -> None:
    lines = ["# fixture-opt-lab", ""]
    if upstream:
        lines.append(upstream)
    lines += [
        "",
        "| workload | n | baseline | optimized | speedup |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, value in rows:
        lines.append(f"| {label} | 1 | 1.000 | 0.500 | **{value:.3f}x** |")
    if median_claim is not None:
        lines += ["", f"Median of the three scale speedups: **{median_claim:.3f}x**"]
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_repo(tmp_path: Path, name: str = "fixture-opt-lab") -> Path:
    root = tmp_path / name
    root.mkdir()
    base = {
        "small": [0.0100, 0.0102, 0.0098, 0.0101, 0.0099, 0.0103, 0.0100],
        "medium": [0.0200, 0.0204, 0.0197, 0.0202, 0.0199, 0.0201, 0.0203],
        "large": [0.0400, 0.0405, 0.0398, 0.0402, 0.0399, 0.0401, 0.0403],
    }
    opt = {
        "small": [0.0050, 0.0051, 0.0049, 0.0050, 0.0051, 0.0049, 0.0050],
        "medium": [0.0080, 0.0081, 0.0079, 0.0080, 0.0081, 0.0079, 0.0080],
        "large": [0.0130, 0.0131, 0.0129, 0.0130, 0.0131, 0.0129, 0.0130],
    }
    _write_impl(root, "baseline", base)
    _write_impl(root, "optimized", opt)
    speedups = {
        name: _stats(base[name])["median_ms"] / _stats(opt[name])["median_ms"]
        for name in base
    }
    rows = [(name, speedups[name]) for name in ("small", "medium", "large")]
    rows.append(("real corpus, mixed", 1.6))
    _readme(
        root,
        rows,
        median_claim=statistics.median(list(speedups.values())),
    )
    (root / "patch.diff").write_text(
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8"
    )
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_diff.py").write_text(
        "import hashlib\n", encoding="utf-8"
    )
    return root


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def test_clean_repo_has_no_errors(tmp_path: Path) -> None:
    report = audit(make_repo(tmp_path), bootstrap=400)
    assert report.ok, [str(f) for f in report.errors]
    assert report.workloads["small"].speedup == pytest.approx(2.0, rel=0.01)
    assert report.workloads["large"].speedup == pytest.approx(3.085, rel=0.01)
    assert "E007" not in codes(report)  # real-corpus row is present


def test_committed_median_tamper_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _write_impl(
        root,
        "baseline",
        {
            "small": [0.0100, 0.0102, 0.0098, 0.0101, 0.0099, 0.0103, 0.0100],
            "medium": [0.0200, 0.0204, 0.0197, 0.0202, 0.0199, 0.0201, 0.0203],
            "large": [0.0400, 0.0405, 0.0398, 0.0402, 0.0399, 0.0401, 0.0403],
        },
        override_median=6.0,
    )
    report = audit(root, bootstrap=200)
    assert not report.ok
    assert "E005" in codes(report)


def test_too_few_raw_samples_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _write_impl(root, "optimized", {"small": [0.005, 0.0051, 0.0049]})
    report = audit(root, bootstrap=200)
    assert "E002" in codes(report)


def test_warmup_below_three_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _write_impl(
        root,
        "baseline",
        {"small": [0.0100, 0.0102, 0.0098, 0.0101, 0.0099]},
        warmup=1,
    )
    report = audit(root, bootstrap=200)
    assert "E003" in codes(report)


def test_readme_speedup_mismatch_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _readme(
        root,
        [("small", 9.0), ("medium", 2.5), ("large", 3.0),
         ("real corpus, mixed", 1.6)],
        median_claim=2.5,
    )
    report = audit(root, bootstrap=200)
    assert "E006" in codes(report)


def test_opt_lab_without_real_corpus_row_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _readme(root, [("small", 2.0), ("medium", 2.5), ("large", 3.0)],
            median_claim=2.5)
    report = audit(root, bootstrap=200)
    assert "E007" in codes(report)


def test_low_real_corpus_claim_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _readme(
        root,
        [("small", 2.0), ("medium", 2.5), ("large", 3.0),
         ("real corpus, mixed", 1.02)],
        median_claim=2.5,
    )
    report = audit(root, bootstrap=200)
    assert "E008" in codes(report)


def test_ci_including_one_is_flagged(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    same = [0.0100, 0.0102, 0.0098, 0.0101, 0.0099, 0.0103, 0.0100]
    _write_impl(root, "baseline", {"small": same})
    _write_impl(root, "optimized", {"small": same})
    _readme(root, [("small", 1.0)],
            median_claim=None)
    report = audit(root, bootstrap=800)
    assert "W006" in codes(report)


def test_missing_upstream_line_is_detected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _readme(
        root,
        [("small", 2.0), ("medium", 2.5), ("large", 3.0),
         ("real corpus, mixed", 1.6)],
        median_claim=2.5,
        upstream=None,
    )
    report = audit(root, bootstrap=200)
    assert "E011" in codes(report)


def test_patch_line_budget_warning(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "patch.diff").write_text(
        "\n".join(["+x"] * 90) + "\n", encoding="utf-8"
    )
    report = audit(root, bootstrap=200)
    assert "W007" in codes(report)


def test_cli_exit_codes_and_json(tmp_path: Path, capsys) -> None:
    good = make_repo(tmp_path, "good-opt-lab")
    out = tmp_path / "report.json"
    assert main([str(good), "--bootstrap", "200", "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["workloads"]["small"]["speedup"] == pytest.approx(2.0, rel=0.01)

    bad = make_repo(tmp_path, "bad-opt-lab")
    _write_impl(bad, "optimized", {"small": [0.005, 0.0051, 0.0049]})
    assert main([str(bad), "--bootstrap", "200"]) == 1
    assert "FAIL" in capsys.readouterr().out