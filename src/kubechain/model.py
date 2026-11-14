"""Findings and the verdict they produce."""

from __future__ import annotations

from dataclasses import dataclass


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    target: str
    reason: str
    action: str

    def to_json(self) -> dict[str, str]:
        return {
            "id": self.id,
            "severity": self.severity,
            "object": self.target,
            "reason": self.reason,
            "action": self.action,
        }


@dataclass(frozen=True)
class Rollout:
    target: str
    strategy: str
    partition: int | None

    def to_json(self) -> dict[str, str | int | None]:
        return {"object": self.target, "strategy": self.strategy, "partition": self.partition}


@dataclass
class Report:
    namespace: str
    objects: list[str]
    findings: list[Finding]
    notes: list[str]
    rollouts: list[Rollout]

    def __post_init__(self) -> None:
        self.findings = [finding for finding in self.findings if _complete(finding)]
        self.findings.sort(key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.id, finding.target))

    @property
    def verdict(self) -> str:
        severities = {finding.severity for finding in self.findings}
        if "critical" in severities:
            return "not-ready"
        if "warning" in severities:
            return "review"
        return "ready"

    def to_json(self) -> dict:
        return {
            "namespace": self.namespace,
            "verdict": self.verdict,
            "objects": self.objects,
            "rollouts": [rollout.to_json() for rollout in self.rollouts],
            "notes": self.notes,
            "findings": [finding.to_json() for finding in self.findings],
        }


def exit_code(report: Report) -> int:
    return {"ready": 0, "review": 1, "not-ready": 2}[report.verdict]


def _complete(finding: Finding) -> bool:
    fields = (finding.id, finding.severity, finding.target, finding.reason, finding.action)
    if not all(isinstance(field, str) and field.strip() for field in fields):
        return False
    return finding.severity in SEVERITY_ORDER
