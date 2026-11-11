from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _pod(name: str, status: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "nodes"},
        "spec": {"containers": [{"name": "geth", "image": "ethereum/client-go:v1.15.11"}]},
        "status": {"containerStatuses": [status]},
    }


def _status(count: int | None, current: str | None = None, last: str | None = None, *, waiting: bool = False) -> dict:
    body: dict = {"name": "geth"}
    if count is not None:
        body["restartCount"] = count
    if current is not None:
        key = "waiting" if waiting or current == "CrashLoopBackOff" else "terminated"
        body["state"] = {key: {"reason": current}}
    else:
        body["state"] = {"running": {"startedAt": "2026-11-11T00:00:00Z"}}
    if last is not None:
        body["lastState"] = {"terminated": {"reason": last}}
    return body


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def test_manifests_without_pod_status_say_restarts_were_not_read() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert "restarts were not read" in report.notes
    assert not any(finding.id.startswith("pod-") for finding in report.findings)


def test_oom_example_reports_the_critical_state() -> None:
    report = _report(load_dir(ROOT / "examples" / "oom"))
    assert report.verdict == "not-ready"
    assert "restarts were not read" not in report.notes
    oom = next(item for item in report.findings if item.id == "pod-oom")
    crash = next(item for item in report.findings if item.id == "pod-crash-loop")
    assert oom.severity == "critical"
    assert oom.target == "geth-0"
    assert oom.reason == "The last terminated reason is OOMKilled. restartCount is 4."
    assert crash.severity == "critical"
    assert crash.reason == "The current state is CrashLoopBackOff. restartCount is 4."
    assert "pod-restarts" not in {item.id for item in report.findings}


def test_current_oom_is_critical_and_three_restarts_are_not_a_warning() -> None:
    current = _report([_pod("geth-0", _status(0, "OOMKilled"))])
    finding = next(item for item in current.findings if item.id == "pod-oom")
    assert finding.severity == "critical"
    assert finding.reason == "The current state is OOMKilled. restartCount is 0."

    earlier = _report([_pod("geth-0", _status(1, last="OOMKilled"))])
    finding = next(item for item in earlier.findings if item.id == "pod-oom")
    assert finding.reason == "The last terminated reason is OOMKilled. restartCount is 1."
    assert "pod-restarts" not in {item.id for item in earlier.findings}

    quiet = _report([_pod("geth-0", _status(3, last="Error"))])
    assert "pod-restarts" not in {item.id for item in quiet.findings}
    assert quiet.verdict == "ready"


def test_more_than_three_restarts_for_another_reason_is_a_warning() -> None:
    report = _report([_pod("geth-0", _status(4, last="Error"))])
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "pod-restarts")
    assert finding.severity == "warning"
    assert finding.reason == "restartCount is 4. The last terminated reason is Error."
    assert "pod-oom" not in {item.id for item in report.findings}
    assert "pod-crash-loop" not in {item.id for item in report.findings}

    crash = _report([_pod("geth-0", _status(5, "CrashLoopBackOff", last="Error"))])
    finding = next(item for item in crash.findings if item.id == "pod-crash-loop")
    assert finding.severity == "critical"
    assert finding.reason == "The current state is CrashLoopBackOff. The last terminated reason is Error. restartCount is 5."
    assert "pod-restarts" not in {item.id for item in crash.findings}
    pairs = _ids(crash)
    assert ("pod-crash-loop", "critical") in pairs
    assert ("pod-oom", "critical") not in pairs
