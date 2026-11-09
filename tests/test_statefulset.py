from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
CRITICAL = {
    "chain-data-ephemeral",
    "statefulset-no-claim",
    "statefulset-no-headless",
    "statefulset-zero-replicas",
}


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def test_ready_statefulset_passes() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert not any(finding.id in CRITICAL | {"statefulset-single-replica", "statefulset-parallel"} for finding in report.findings)


def test_broken_statefulset_trips_the_critical_cases() -> None:
    pairs = _ids(_report(load_dir(ROOT / "examples" / "broken")))
    for finding_id in CRITICAL:
        assert (finding_id, "critical") in pairs


def test_emptydir_and_a_service_that_is_not_headless() -> None:
    objects = _ready()
    pod = _kind(objects, "StatefulSet", "geth")["spec"]["template"]["spec"]
    pod["volumes"].append({"name": "data", "emptyDir": {}})
    _kind(objects, "Service", "geth")["spec"]["clusterIP"] = "10.0.0.20"
    pairs = _ids(_report(objects))
    assert ("chain-data-ephemeral", "critical") in pairs
    assert ("statefulset-no-headless", "critical") in pairs


def test_host_path_is_critical() -> None:
    objects = _ready()
    pod = _kind(objects, "StatefulSet", "geth")["spec"]["template"]["spec"]
    pod["volumes"].append({"name": "data", "hostPath": {"path": "/var/lib/geth"}})
    assert ("chain-data-ephemeral", "critical") in _ids(_report(objects))


def test_missing_claim_or_service_name_is_critical() -> None:
    objects = _ready()
    spec = _kind(objects, "StatefulSet", "geth")["spec"]
    spec.pop("volumeClaimTemplates")
    spec["serviceName"] = ""
    pairs = _ids(_report(objects))
    assert ("statefulset-no-claim", "critical") in pairs
    assert ("statefulset-no-headless", "critical") in pairs


def test_zero_replicas_is_critical_and_one_replica_is_a_warning() -> None:
    stopped = _ready()
    _kind(stopped, "StatefulSet", "geth")["spec"]["replicas"] = 0
    pairs = _ids(_report(stopped))
    assert ("statefulset-zero-replicas", "critical") in pairs
    assert ("statefulset-single-replica", "warning") not in pairs

    single = _ready()
    for name in ("geth", "lighthouse"):
        _kind(single, "StatefulSet", name)["spec"]["replicas"] = 1
    report = _report(single)
    assert report.verdict == "review"
    assert ("statefulset-single-replica", "warning") in _ids(report)
    assert not any(finding.severity == "critical" for finding in report.findings)


def test_parallel_is_a_warning_and_the_default_policy_passes() -> None:
    parallel = _ready()
    _kind(parallel, "StatefulSet", "geth")["spec"]["podManagementPolicy"] = "Parallel"
    report = _report(parallel)
    assert report.verdict == "review"
    assert ("statefulset-parallel", "warning") in _ids(report)

    defaulted = _ready()
    del _kind(defaulted, "StatefulSet", "geth")["spec"]["podManagementPolicy"]
    del _kind(defaulted, "StatefulSet", "lighthouse")["spec"]["podManagementPolicy"]
    report = _report(defaulted)
    assert report.verdict == "ready"
    assert "statefulset-parallel" not in {finding.id for finding in report.findings}


def test_on_delete_is_an_operator_action_and_stays_ready() -> None:
    objects = _ready()
    _kind(objects, "StatefulSet", "geth")["spec"]["updateStrategy"] = {"type": "OnDelete"}
    report = _report(objects)
    assert report.verdict == "ready"
    finding = next(item for item in report.findings if item.id == "statefulset-on-delete")
    assert finding.severity == "info"
    assert finding.action == "Delete the pod to roll it."
