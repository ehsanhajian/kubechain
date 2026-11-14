"""Text, JSON, and markdown for one report."""

from __future__ import annotations

import json

from kubechain.model import Report


def render_text(report: Report) -> str:
    lines = [
        f"verdict {report.verdict}",
        f"namespace {report.namespace}",
        f"objects {len(report.objects)}",
    ]
    for rollout in report.rollouts:
        partition = "" if rollout.partition is None else f" partition {rollout.partition}"
        lines.append(f"rollout {rollout.target} {rollout.strategy}{partition}")
    for note in report.notes:
        lines.append(f"note {note}")
    for finding in report.findings:
        lines.append(f"{finding.severity} {finding.id} {finding.target}")
        lines.append(f"  {finding.reason}")
        lines.append(f"  {finding.action}")
    return "\n".join(lines) + "\n"


def render_brief(report: Report) -> str:
    return f"namespace {report.namespace} verdict {report.verdict}\n"


def render_json(report: Report) -> str:
    return json.dumps(report.to_json(), indent=2) + "\n"


def render_markdown(report: Report) -> str:
    lines = [
        f"verdict {report.verdict}",
        f"namespace {report.namespace}",
        "",
    ]
    for rollout in report.rollouts:
        partition = "" if rollout.partition is None else f" partition {rollout.partition}"
        lines.append(f"rollout {rollout.target} {rollout.strategy}{partition}")
    if report.rollouts:
        lines.append("")
    for note in report.notes:
        lines.append(f"note {note}")
    if report.notes:
        lines.append("")
    for finding in report.findings:
        lines.extend([
            f"## {finding.id}",
            "",
            f"severity {finding.severity}",
            f"object {finding.target}",
            "",
            finding.reason,
            "",
            finding.action,
            "",
        ])
    if not report.findings:
        lines.append("No findings.")
        lines.append("")
    return "\n".join(lines)
