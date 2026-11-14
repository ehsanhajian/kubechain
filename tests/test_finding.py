import json
import os
import re
import subprocess
import sys
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir
from kubechain.model import Finding, Report, exit_code
from kubechain.render import render_json

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
FIELDS = ("id", "severity", "object", "reason", "action")


def _report(*findings: Finding) -> Report:
    return Report("nodes", [], list(findings), [], [])


def _finding(finding_id: str, severity: str) -> Finding:
    return Finding(finding_id, severity, "geth", "The check found a gap.", "Change the object.")


def _evaluate(name: str) -> Report:
    return evaluate(load_dir(ROOT / "examples" / name), PROFILES, live=False, usage=None, classes_known=True)


def test_severity_sets_the_verdict_and_the_worst_one_wins() -> None:
    info = _report(_finding("prestop-missing", "info"))
    assert info.verdict == "ready"
    assert exit_code(info) == 0

    warning = _report(_finding("prestop-missing", "info"), _finding("pods-colocated", "warning"))
    assert warning.verdict == "review"
    assert exit_code(warning) == 1

    critical = _report(
        _finding("prestop-missing", "info"),
        _finding("pods-colocated", "warning"),
        _finding("statefulset-no-claim", "critical"),
    )
    assert critical.verdict == "not-ready"
    assert exit_code(critical) == 2
    assert [item.severity for item in critical.findings] == ["critical", "warning", "info"]


def test_a_check_without_an_action_does_not_emit_a_finding() -> None:
    report = _report(
        _finding("prestop-missing", "info"),
        Finding("no-action", "critical", "geth", "The object is wrong.", ""),
        Finding("no-object", "critical", "  ", "The object is wrong.", "Name the object."),
        Finding("no-reason", "warning", "geth", "", "Name the reason."),
        Finding("", "warning", "geth", "The object is wrong.", "Name the id."),
        Finding("bad-severity", "error", "geth", "The object is wrong.", "Use a known severity."),
    )
    assert [item.id for item in report.findings] == ["prestop-missing"]
    assert report.verdict == "ready"
    assert exit_code(report) == 0


def test_every_emitted_finding_has_the_same_json_fields() -> None:
    broken = _evaluate("broken")
    oom = _evaluate("oom")
    assert broken.verdict == "not-ready"
    assert exit_code(broken) == 2
    assert {item.severity for item in broken.findings} >= {"warning", "critical"}
    assert {"statefulset-no-claim", "jwt-from-env", "no-backup-signal"} <= {item.id for item in broken.findings}

    rendered = json.loads(render_json(broken))
    assert rendered["findings"]
    for finding in rendered["findings"]:
        assert tuple(finding) == FIELDS
        assert all(isinstance(finding[key], str) and finding[key].strip() for key in FIELDS)
        assert finding["severity"] in {"info", "warning", "critical"}
        assert ID.match(finding["id"])

    for report in (broken, oom, _evaluate("ready")):
        for finding in report.findings:
            assert ID.match(finding.id)
            assert finding.severity in {"info", "warning", "critical"}
            assert finding.target.strip() and finding.reason.strip() and finding.action.strip()

    source = (ROOT / "src" / "kubechain" / "check.py").read_text()
    ids = re.findall(r'_finding\(\s*"([^"]+)"', source)
    assert {"statefulset-no-claim", "jwt-from-env", "no-backup-signal"} <= set(ids)
    assert all(ID.match(finding_id) for finding_id in ids)


def test_json_reports_the_finding_fields() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "kubechain", "check", "--manifests", "examples/broken", "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    assert result.returncode == 2
    document = json.loads(result.stdout)
    assert document["verdict"] == "not-ready"
    found = {item["id"]: item for item in document["findings"]}
    for finding_id in ("statefulset-no-claim", "jwt-from-env", "no-backup-signal"):
        finding = found[finding_id]
        assert tuple(finding) == FIELDS
        assert finding["object"]
        assert finding["reason"]
        assert finding["action"]
