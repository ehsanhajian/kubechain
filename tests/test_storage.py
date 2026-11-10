from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
STORAGE = {
    "storage-not-rwo",
    "storage-no-size",
    "storage-no-class",
    "storage-below-floor",
    "storage-bind-immediate",
    "storage-no-expansion",
    "pvc-not-bound",
}


def _report(objects: list[dict], overlay: dict | None = None, *, live: bool = False, classes_known: bool = True):
    return evaluate(objects, PROFILES, overlay, live=live, usage=None, classes_known=classes_known)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _claim(objects: list[dict], name: str) -> dict:
    return _kind(objects, "StatefulSet", name)["spec"]["volumeClaimTemplates"][0]


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def _pvc(name: str, phase: str | None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": "nodes"},
        "status": {} if phase is None else {"phase": phase},
    }


def test_ready_claims_pass() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert not any(finding.id in STORAGE for finding in report.findings)


def test_broken_claim_is_not_readwriteonce_or_sized_for_the_floor() -> None:
    report = _report(load_dir(ROOT / "examples" / "broken"))
    pairs = _ids(report)
    assert ("storage-not-rwo", "critical") in pairs
    assert ("storage-no-class", "critical") in pairs
    assert ("storage-below-floor", "warning") in pairs
    assert "storage-no-size" not in {finding.id for finding in report.findings}
    floor = next(finding for finding in report.findings if finding.id == "storage-below-floor")
    assert floor.target == "erigon"
    assert "1Gi" in floor.reason
    assert "100Gi" in floor.reason
    assert "KubeChain floor" in floor.reason


def test_access_mode_size_and_class_are_critical() -> None:
    mixed = _ready()
    _claim(mixed, "geth")["spec"]["accessModes"] = ["ReadWriteOnce", "ReadWriteMany"]
    assert ("storage-not-rwo", "critical") in _ids(_report(mixed))

    once = _ready()
    _claim(once, "geth")["spec"]["accessModes"] = ["ReadWriteOncePod"]
    report = _report(once)
    assert ("storage-not-rwo", "critical") in _ids(report)
    assert report.verdict == "not-ready"

    unsized = _ready()
    _claim(unsized, "geth")["spec"]["resources"]["requests"].pop("storage")
    report = _report(unsized)
    ids = {finding.id for finding in report.findings}
    assert ("storage-no-size", "critical") in _ids(report)
    assert "storage-below-floor" not in ids

    unnamed = _ready()
    for name in ("geth", "lighthouse"):
        _claim(unnamed, name)["spec"].pop("storageClassName")
    unnamed.remove(_kind(unnamed, "StorageClass", "fast"))
    assert ("storage-no-class", "critical") in _ids(_report(unnamed))

    default = _ready()
    _kind(default, "StorageClass", "fast")["metadata"]["annotations"] = {
        "storageclass.kubernetes.io/is-default-class": "true",
    }
    for name in ("geth", "lighthouse"):
        _claim(default, name)["spec"].pop("storageClassName")
    report = _report(default)
    assert "storage-no-class" not in {finding.id for finding in report.findings}
    assert report.verdict == "ready"

    empty = _ready()
    _claim(empty, "geth")["spec"]["storageClassName"] = ""
    _kind(empty, "StorageClass", "fast")["metadata"]["annotations"] = {
        "storageclass.kubernetes.io/is-default-class": "true",
    }
    finding = next(item for item in _report(empty).findings if item.id == "storage-no-class")
    assert finding.severity == "critical"
    assert finding.reason == "The data claim sets an empty storageClassName."


def test_immediate_binding_and_no_expansion_are_warnings() -> None:
    immediate = _ready()
    _kind(immediate, "StorageClass", "fast")["volumeBindingMode"] = "Immediate"
    report = _report(immediate)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "storage-bind-immediate")
    assert finding.severity == "warning"
    assert finding.reason == "StorageClass fast binds immediately."

    omitted = _ready()
    del _kind(omitted, "StorageClass", "fast")["volumeBindingMode"]
    assert ("storage-bind-immediate", "warning") in _ids(_report(omitted))

    frozen = _ready()
    _kind(frozen, "StorageClass", "fast")["allowVolumeExpansion"] = False
    report = _report(frozen)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "storage-no-expansion")
    assert finding.reason == "StorageClass fast does not allow volume expansion."

    absent = _ready()
    del _kind(absent, "StorageClass", "fast")["allowVolumeExpansion"]
    assert ("storage-no-expansion", "warning") in _ids(_report(absent))

    missing_class = _ready()
    missing_class.remove(_kind(missing_class, "StorageClass", "fast"))
    ids = {finding.id for finding in _report(missing_class).findings}
    assert "storage-bind-immediate" not in ids
    assert "storage-no-expansion" not in ids
    assert "storage-no-class" not in ids


def test_size_below_the_profile_floor_is_a_warning() -> None:
    exact = _ready()
    _claim(exact, "geth")["spec"]["resources"]["requests"]["storage"] = "100Gi"
    _claim(exact, "lighthouse")["spec"]["resources"]["requests"]["storage"] = "50Gi"
    assert "storage-below-floor" not in {finding.id for finding in _report(exact).findings}

    short = _ready()
    _claim(short, "geth")["spec"]["resources"]["requests"]["storage"] = "99Gi"
    report = _report(short)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "storage-below-floor" and item.target == "geth")
    assert finding.severity == "warning"
    assert "99Gi" in finding.reason
    assert "100Gi" in finding.reason
    assert "KubeChain floor" in finding.reason

    consensus = _ready()
    _claim(consensus, "lighthouse")["spec"]["resources"]["requests"]["storage"] = "49Gi"
    finding = next(item for item in _report(consensus).findings if item.id == "storage-below-floor")
    assert finding.target == "lighthouse"
    assert "50Gi" in finding.reason
    assert "KubeChain floor" in finding.reason

    raised = _ready()
    report = _report(raised, {"comm": "geth", "storage": "300Gi"})
    finding = next(item for item in report.findings if item.id == "storage-below-floor" and item.target == "geth")
    assert "200Gi" in finding.reason
    assert "300Gi" in finding.reason


def test_live_phase_is_critical_only_for_chain_claims() -> None:
    offline = _ready()
    offline.append(_pvc("data-geth-0", "Pending"))
    assert "pvc-not-bound" not in {finding.id for finding in _report(offline).findings}

    bound = _ready()
    bound.extend([_pvc("data-geth-0", "Bound"), _pvc("data-lighthouse-1", "Bound"), _pvc("scratch", "Pending")])
    report = _report(bound, live=True)
    assert "pvc-not-bound" not in {finding.id for finding in report.findings}
    assert report.verdict == "ready"

    pending = _ready()
    pending.extend([_pvc("data-geth-0", "Pending"), _pvc("data-geth-1", "Bound"), _pvc("scratch", "Pending")])
    report = _report(pending, live=True)
    findings = [item for item in report.findings if item.id == "pvc-not-bound"]
    assert [(item.target, item.severity, item.reason) for item in findings] == [
        ("data-geth-0", "critical", "The PersistentVolumeClaim phase is Pending."),
    ]
    assert report.verdict == "not-ready"

    unset = _ready()
    unset.append(_pvc("data-lighthouse-0", None))
    finding = next(item for item in _report(unset, live=True).findings if item.id == "pvc-not-bound")
    assert finding.reason == "The PersistentVolumeClaim phase is unset."


def test_a_non_data_claim_is_not_judged() -> None:
    objects = _ready()
    templates = _kind(objects, "StatefulSet", "geth")["spec"]["volumeClaimTemplates"]
    templates.append({
        "metadata": {"name": "cache"},
        "spec": {"accessModes": ["ReadWriteMany"]},
    })
    report = _report(objects)
    assert report.verdict == "ready"
    assert not any(finding.id in STORAGE for finding in report.findings)


def test_a_missing_cluster_class_list_is_not_a_missing_default() -> None:
    objects = _ready()
    for name in ("geth", "lighthouse"):
        _claim(objects, name)["spec"].pop("storageClassName")
    objects.remove(_kind(objects, "StorageClass", "fast"))
    hidden = _report(objects, classes_known=False)
    assert "storage-no-class" not in {finding.id for finding in hidden.findings}
    assert ("storage-no-class", "critical") in _ids(_report(objects))

    opted_out = _ready()
    _claim(opted_out, "geth")["spec"]["storageClassName"] = ""
    assert ("storage-no-class", "critical") in _ids(_report(opted_out, classes_known=False))
