"""Shared eval tooling: datasets and a scorecard that writes JSON + Markdown reports (and the CI summary).

Offline suites (``evals/offline``) are deterministic and gate every CI run. Live suites (``evals/live``)
call Groq and run nightly or on demand. Reports land in ``evals/reports/`` (git-ignored); on GitHub
Actions the Markdown is also appended to the job summary.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATASETS = ROOT / "datasets"
REPORTS = ROOT / "reports"


def load_jsonl(name: str) -> list[dict[str, Any]]:
    rows = []
    for line in (DATASETS / name).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("//"):
            rows.append(json.loads(line))
    return rows


def env_value(name: str) -> str | None:
    """From the environment (CI secret), else from the repo's git-ignored .env (local runs)."""
    if value := os.environ.get(name):
        return value
    env_file = ROOT.parent / ".env"
    if env_file.exists():
        match = re.search(rf"^{name}=([^\s#]+)", env_file.read_text(encoding="utf-8"), re.M)
        if match:
            return match.group(1)
    return None


@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scorecard:
    suite: str
    results: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)

    def add(self, result: CaseResult) -> None:
        self.results.append(result)

    def pass_rate(self, category: str | None = None) -> float:
        rows = [r for r in self.results if category is None or r.category == category]
        return sum(r.passed for r in rows) / len(rows) if rows else 1.0

    def failing_thresholds(self) -> list[str]:
        return [
            f"{name}: {self.metrics.get(name, 0.0):.3f} < {minimum:.3f}"
            for name, minimum in self.thresholds.items()
            if self.metrics.get(name, 0.0) < minimum
        ]

    def write(self) -> Path:
        REPORTS.mkdir(exist_ok=True)
        payload = {
            "suite": self.suite,
            "metrics": self.metrics,
            "thresholds": self.thresholds,
            "results": [asdict(r) for r in self.results],
        }
        (REPORTS / f"{self.suite}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        markdown = self.markdown()
        path = REPORTS / f"{self.suite}.md"
        path.write_text(markdown, encoding="utf-8")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(markdown + "\n")
        return path

    def markdown(self) -> str:
        lines = [f"### Eval: {self.suite}", "", "| Metric | Value | Threshold |", "|---|---|---|"]
        for name, value in self.metrics.items():
            minimum = self.thresholds.get(name)
            mark = "" if minimum is None else (" ✅" if value >= minimum else " ❌")
            lines.append(f"| {name} | {value:.3f} | {'' if minimum is None else f'≥ {minimum:.2f}'}{mark} |")
        failures = [r for r in self.results if not r.passed]
        if failures:
            lines += ["", "| Failed case | Category | Why |", "|---|---|---|"]
            lines += [f"| {r.case_id} | {r.category} | {'; '.join(r.reasons)[:200]} |" for r in failures]
        return "\n".join(lines)
