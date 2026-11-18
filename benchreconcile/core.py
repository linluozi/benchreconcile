"""Reconcile published performance claims with raw per-sample evidence.

The audit is intentionally conservative: every number printed in a README
must be reproducible from ``benchmark/raw/*.json``.  Aggregates are never
trusted; they are recomputed and compared.  A deterministic bootstrap gives
the reader a noise floor for each speedup instead of a single point estimate.
"""

from __future__ import annotations

import json
import math
import random
import re
import zlib
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ERROR = "ERROR"
WARN = "WARN"
REQUIRED_RAW_KEYS = ("impl", "workload", "rep", "seconds", "seed", "python")
DEFAULT_BOOTSTRAP = 2000
DEFAULT_SEED = 20261118
MIN_WARMUP = 3
MIN_REPS = 5
REAL_CORPUS_FLOOR = 1.15
MAX_PATCH_LINES = 80


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "location": self.location,
        }

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity} {self.code}{where}: {self.message}"


@dataclass
class Workload:
    name: str
    baseline_ms: float
    optimized_ms: float
    speedup: float
    ci_low: float
    ci_high: float
    baseline_samples: list[float] = field(default_factory=list)
    optimized_samples: list[float] = field(default_factory=list)


@dataclass
class Report:
    repo: str
    findings: list[Finding] = field(default_factory=list)
    workloads: dict[str, Workload] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add(self, code: str, severity: str, message: str, location: str = "") -> None:
        self.findings.append(Finding(code, severity, message, location))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "metadata": self.metadata,
            "workloads": {
                name: {
                    "baseline_ms": w.baseline_ms,
                    "optimized_ms": w.optimized_ms,
                    "speedup": w.speedup,
                    "speedup_ci95": [w.ci_low, w.ci_high],
                    "baseline_samples": len(w.baseline_samples),
                    "optimized_samples": len(w.optimized_samples),
                }
                for name, w in self.workloads.items()
            },
            "claims": self.claims,
            "findings": [f.to_dict() for f in self.findings],
        }


def _load_json(path: Path, report: Report) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        report.add("E000", ERROR, f"cannot read JSON: {exc}", str(path))
        return None
    if not isinstance(data, dict):
        report.add("E000", ERROR, "JSON root is not an object", str(path))
        return None
    return data


def _percentiles(samples: Iterable[float]) -> tuple[float, float, float, float]:
    values = sorted(samples)
    median = statistics.median(values)
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    mean = statistics.fmean(values)
    rel_stdev = statistics.pstdev(values) / mean if mean else float("inf")
    return median, quartiles[0], quartiles[2], rel_stdev


def _close(left: float, right: float, rel: float = 1e-6) -> bool:
    return math.isclose(left, right, rel_tol=rel, abs_tol=1e-9)


def _safe_div(num: float, den: float) -> float:
    if den == 0:
        return float("inf")
    return num / den


def _bootstrap_ci(
    baseline: list[float],
    optimized: list[float],
    *,
    rounds: int,
    seed: int,
) -> tuple[float, float]:
    """Percentile bootstrap for the ratio of medians.

    Deterministic: the generator is seeded per workload by the caller, so the
    same raw files always produce the same interval.
    """
    if not baseline or not optimized:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(rounds):
        b = statistics.median(rng.choices(baseline, k=len(baseline)))
        o = statistics.median(rng.choices(optimized, k=len(optimized)))
        ratios.append(_safe_div(b, o))
    ratios.sort()
    low = ratios[int(0.025 * (rounds - 1))]
    high = ratios[int(0.975 * (rounds - 1))]
    return (low, high)


def _resolve(repo: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else repo / path


def _load_impl(
    repo: Path,
    path: Path,
    expected_impl: str,
    report: Report,
) -> dict[str, list[float]]:
    data = _load_json(path, report)
    if data is None:
        return {}
    impl = data.get("impl")
    if impl != expected_impl:
        report.add(
            "E001",
            ERROR,
            f"aggregate says impl={impl!r}, expected {expected_impl!r}",
            str(path),
        )
    scales = data.get("scales")
    if not isinstance(scales, list) or not scales:
        report.add("E001", ERROR, "aggregate has no non-empty 'scales' list", str(path))
        return {}

    samples_by_workload: dict[str, list[float]] = {}
    for scale in scales:
        if not isinstance(scale, dict):
            report.add("E001", ERROR, "scale entry is not an object", str(path))
            continue
        workload = str(scale.get("workload", "?"))
        where = f"{path.name}:{workload}"
        raw_list = scale.get("raw")
        if not isinstance(raw_list, list) or not raw_list:
            report.add("E002", ERROR, "scale has no raw sample files", where)
            continue
        warmup = scale.get("warmup")
        if not isinstance(warmup, int) or warmup < MIN_WARMUP:
            report.add(
                "E003",
                ERROR,
                f"warmup={warmup!r} is below the required {MIN_WARMUP}",
                where,
            )
        reps = scale.get("reps")
        if not isinstance(reps, int) or reps < MIN_REPS:
            report.add("E002", ERROR, f"reps={reps!r} is below {MIN_REPS}", where)
        if isinstance(reps, int) and len(raw_list) != reps:
            report.add(
                "W003",
                WARN,
                f"{len(raw_list)} raw files listed but reps={reps}",
                where,
            )

        values: list[float] = []
        seeds: set[int] = set()
        impls: set[str] = set()
        for raw_ref in raw_list:
            raw_path = _resolve(repo, str(raw_ref))
            raw = _load_json(raw_path, report)
            if raw is None:
                continue
            missing = [k for k in REQUIRED_RAW_KEYS if k not in raw]
            if missing:
                report.add(
                    "E004",
                    ERROR,
                    f"raw sample misses keys {missing}",
                    str(raw_path),
                )
                continue
            seconds = raw.get("seconds")
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                report.add("E004", ERROR, f"invalid seconds={seconds!r}", str(raw_path))
                continue
            if raw.get("workload") != workload:
                report.add(
                    "E004",
                    ERROR,
                    f"raw workload={raw.get('workload')!r} != scale {workload!r}",
                    str(raw_path),
                )
            if not isinstance(raw.get("seed"), int):
                report.add("E004", ERROR, "raw sample has no integer seed", str(raw_path))
            if str(raw.get("impl")) != expected_impl:
                report.add(
                    "E004",
                    ERROR,
                    f"raw impl={raw.get('impl')!r} != aggregate {expected_impl!r}",
                    str(raw_path),
                )
            values.append(float(seconds))
            seeds.add(int(raw.get("seed", -1)))
            impls.add(str(raw.get("impl")))

        if len(values) < MIN_REPS:
            report.add(
                "E002",
                ERROR,
                f"only {len(values)} usable raw samples, need >= {MIN_REPS}",
                where,
            )
        if len(seeds) > 1:
            report.add(
                "W003",
                WARN,
                f"raw samples use {len(seeds)} different seeds: {sorted(seeds)}",
                where,
            )
        if len(values) >= 2:
            median, p25, p75, rel_stdev = _percentiles(values)
            for key, actual in (
                ("median_ms", median * 1e3),
                ("p25_ms", p25 * 1e3),
                ("p75_ms", p75 * 1e3),
                ("rel_stdev", rel_stdev),
            ):
                if key in scale and not _close(float(scale[key]), actual):
                    report.add(
                        "E005",
                        ERROR,
                        f"committed {key}={scale[key]!r} but raw gives {actual!r}",
                        where,
                    )
            if values and _close(float(scale.get("median_ms", -1)), min(values) * 1e3):
                report.add(
                    "W002",
                    WARN,
                    "committed median equals the single fastest sample",
                    where,
                )
            if rel_stdev > 0.10:
                report.add(
                    "W001",
                    WARN,
                    f"relative stdev {rel_stdev:.2%} exceeds 10%",
                    where,
                )
        samples_by_workload[workload] = values
    return samples_by_workload


def _readme_claims(readme: str) -> tuple[list[dict[str, Any]], list[float]]:
    """Extract speedup cells and a median-of-scales claim from Markdown."""
    claims: list[dict[str, Any]] = []
    for line in readme.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 3:
            continue
        cells = [c.strip().strip("*").strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*x", cells[-1])
        if not match:
            continue
        claims.append(
            {
                "label": cells[0],
                "value": float(match.group(1)),
                "raw_row": stripped,
            }
        )
    medians: list[float] = []
    for match in re.finditer(
        r"[Mm]edian of the (?:three|3) (?:scale )?speedups?\s*[:=]?\s*"
        r"\**\s*([0-9]+(?:\.[0-9]+)?)\s*x",
        readme,
    ):
        medians.append(float(match.group(1)))
    return claims, medians


def _match_workload(label: str, names: Iterable[str]) -> str | None:
    low = label.lower()
    for name in sorted(names, key=len, reverse=True):
        if name.lower() in low:
            return name
    return None


def audit(
    repo: str | Path,
    *,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
) -> Report:
    root = Path(repo).resolve()
    report = Report(repo=str(root))
    bench = root / "benchmark"
    if not bench.is_dir():
        bench = root / "bench"
    if not bench.is_dir():
        report.add("E001", ERROR, "no benchmark/ (or bench/) directory", str(root))
        return report

    baseline_path = bench / "baseline.json"
    optimized_path = bench / "optimized.json"
    for path in (baseline_path, optimized_path):
        if not path.is_file():
            report.add("E001", ERROR, "required aggregate is missing", str(path))
    readme_path = root / "README.md"
    if not readme_path.is_file():
        report.add("E001", ERROR, "README.md is missing", str(root))
        readme = ""
    else:
        readme = readme_path.read_text(encoding="utf-8", errors="replace")
    for name in ("patch.diff", "LICENSE", "tests/test_diff.py"):
        if not (root / name).is_file():
            report.add("E010", ERROR, f"required evidence file missing: {name}", str(root))

    if readme:
        head = readme[:2500]
        if not re.search(r"(?im)^\s*Upstream\s*:", head):
            report.add(
                "E011",
                ERROR,
                "README first 2500 chars have no 'Upstream:' provenance line",
                str(readme_path),
            )

    baseline = _load_impl(root, baseline_path, "baseline", report) if baseline_path.is_file() else {}
    optimized = _load_impl(root, optimized_path, "optimized", report) if optimized_path.is_file() else {}

    for name in sorted(set(baseline) & set(optimized)):
        b = baseline[name]
        o = optimized[name]
        if not b or not o:
            continue
        b_med = statistics.median(b) * 1e3
        o_med = statistics.median(o) * 1e3
        speedup = _safe_div(b_med, o_med)
        low, high = _bootstrap_ci(
            b, o, rounds=bootstrap, seed=seed + (zlib.crc32(name.encode()) % 100_000)
        )
        report.workloads[name] = Workload(
            name=name,
            baseline_ms=b_med,
            optimized_ms=o_med,
            speedup=speedup,
            ci_low=low,
            ci_high=high,
            baseline_samples=b,
            optimized_samples=o,
        )
        if not (low <= 1.0 <= high):
            continue
        report.add(
            "W006",
            WARN,
            f"95% bootstrap CI [{low:.3f}, {high:.3f}] includes 1.0",
            f"workload:{name}",
        )

    claims, median_claims = _readme_claims(readme)
    report.claims = claims
    names = set(report.workloads)
    for claim in claims:
        label = claim["label"]
        workload = _match_workload(label, names)
        if workload:
            actual = report.workloads[workload].speedup
            claim["computed"] = actual
            if abs(claim["value"] - actual) > max(0.02 * actual, 0.01):
                report.add(
                    "E006",
                    ERROR,
                    f"README says {claim['value']}x for {workload}, raw gives {actual:.3f}x",
                    "README.md",
                )
        elif "real" in label.lower():
            claim["real_corpus"] = True
            if claim["value"] < REAL_CORPUS_FLOOR:
                report.add(
                    "E008",
                    ERROR,
                    f"real-corpus claim {claim['value']}x is below {REAL_CORPUS_FLOOR}x",
                    "README.md",
                )

    repo_name = root.name.lower()
    has_real_claim = any(c.get("real_corpus") for c in claims)
    if repo_name.endswith("-opt-lab") and claims and not has_real_claim:
        report.add(
            "E007",
            ERROR,
            "repo is named -opt-lab but README has no real-corpus speedup row",
            "README.md",
        )
    if median_claims:
        scale_values = [
            w.speedup
            for name, w in report.workloads.items()
            if "real" not in name.lower()
        ]
        if scale_values:
            computed = statistics.median(scale_values)
            for claimed in median_claims:
                if abs(claimed - computed) > max(0.02 * computed, 0.01):
                    report.add(
                        "E006",
                        ERROR,
                        f"README median claim {claimed}x, computed {computed:.3f}x",
                        "README.md",
                    )

    if readme:
        for name, workload in report.workloads.items():
            if (
                workload.baseline_samples
                and workload.optimized_samples
                and len(workload.baseline_samples) != len(workload.optimized_samples)
            ):
                report.add(
                    "W005",
                    WARN,
                    f"baseline has {len(workload.baseline_samples)} samples but "
                    f"optimized has {len(workload.optimized_samples)}",
                    f"workload:{name}",
                )
        if any(f.code == "W001" for f in report.findings) and not re.search(
            r"(?i)noise|stdev|variance", readme
        ):
            report.add(
                "W004",
                WARN,
                "a workload has >10% relative stdev and README does not discuss noise",
                "README.md",
            )

    patch_path = root / "patch.diff"
    if patch_path.is_file():
        changed = sum(
            1
            for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        report.metadata["patch_lines"] = changed
        if changed > MAX_PATCH_LINES:
            report.add(
                "W007",
                WARN,
                f"patch touches {changed} lines (> {MAX_PATCH_LINES})",
                "patch.diff",
            )

    report.metadata["readme_chars"] = len(readme)
    report.metadata["workloads_computed"] = len(report.workloads)
    report.metadata["bootstrap_rounds"] = bootstrap
    return report


def format_report(report: Report) -> str:
    lines = [f"benchreconcile: {report.repo}"]
    if report.workloads:
        lines.append("workload                 baseline ms   optimized ms   speedup   95% CI")
        for name in sorted(report.workloads):
            w = report.workloads[name]
            lines.append(
                f"{name:<24} {w.baseline_ms:>11.3f} {w.optimized_ms:>14.3f} "
                f"{w.speedup:>8.3f}x  [{w.ci_low:.2f}, {w.ci_high:.2f}]"
            )
    if report.findings:
        lines.append("")
        lines.extend(str(f) for f in report.findings)
    lines.append("")
    lines.append(
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s); "
        f"status={'PASS' if report.ok else 'FAIL'}"
    )
    return "\n".join(lines)