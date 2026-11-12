from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _claim(objects: list[dict], workload: str, name: str = "data") -> dict:
    for claim in _kind(objects, "StatefulSet", workload)["spec"]["volumeClaimTemplates"]:
        if claim["metadata"]["name"] == name:
            return claim
    raise AssertionError(f"{workload}/{name}")


def _clear_backup(objects: list[dict]) -> None:
    for workload in ("geth", "lighthouse"):
        _claim(objects, workload)["metadata"].pop("annotations", None)


def _ids(report, finding_id: str) -> list[str]:
    return [finding.target for finding in report.findings if finding.id == finding_id]


def _snapshot(name: str, pvc: str, namespace: str = "nodes") -> dict:
    return {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"source": {"persistentVolumeClaimName": pvc}},
    }


def _backup(**spec: object) -> dict:
    return {
        "apiVersion": "velero.io/v1",
        "kind": "Backup",
        "metadata": {"name": "chain", "namespace": "velero"},
        "spec": spec,
    }


def test_ready_annotation_is_the_backup_signal() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert _ids(report, "no-backup-signal") == []

    noted = _ready()
    _clear_backup(noted)
    _claim(noted, "geth")["metadata"]["annotations"] = {"kubechain.io/backup": ""}
    _claim(noted, "lighthouse")["metadata"]["annotations"] = {
        "kubechain.io/backup": "https://example.com/runbook",
    }
    assert _report(noted).verdict == "ready"

    on_set = _ready()
    _clear_backup(on_set)
    _kind(on_set, "StatefulSet", "geth")["metadata"]["annotations"] = {"kubechain.io/backup": "ticket-1"}
    _kind(on_set, "StatefulSet", "lighthouse")["metadata"]["annotations"] = {"kubechain.io/backup": "ticket-2"}
    report = _report(on_set)
    assert report.verdict == "review"
    assert set(_ids(report, "no-backup-signal")) == {"geth", "lighthouse"}


def test_missing_signal_is_a_warning_and_a_snapshot_must_name_the_claim() -> None:
    bare = _ready()
    _clear_backup(bare)
    report = _report(bare)
    assert report.verdict == "review"
    findings = [item for item in report.findings if item.id == "no-backup-signal"]
    assert {item.target for item in findings} == {"geth", "lighthouse"}
    assert all(item.severity == "warning" for item in findings)

    other = _ready()
    _clear_backup(other)
    other.append(_snapshot("other", "logs-geth-0"))
    other.append(_snapshot("template-name", "data"))
    other.append(_snapshot("elsewhere", "data-geth-0", namespace="other"))
    content = _snapshot("content", "data-geth-0")
    content["spec"]["source"] = {"volumeSnapshotContentName": "snapcontent-data"}
    other.append(content)
    assert set(_ids(_report(other), "no-backup-signal")) == {"geth", "lighthouse"}

    matched = _ready()
    _clear_backup(matched)
    matched.append(_snapshot("geth-data", "data-geth-0"))
    assert _ids(_report(matched), "no-backup-signal") == ["lighthouse"]


def test_velero_counts_only_when_it_selects_the_claim() -> None:
    objects = _ready()
    _clear_backup(objects)
    objects.append(_backup(includedNamespaces=["other"]))
    assert set(_ids(_report(objects), "no-backup-signal")) == {"geth", "lighthouse"}

    selected = _ready()
    _clear_backup(selected)
    selected.append(_backup(includedNamespaces=["nodes"], includedResources=["persistentvolumeclaims"]))
    assert _report(selected).verdict == "ready"

    schedule = _ready()
    _clear_backup(schedule)
    schedule.append({
        "apiVersion": "velero.io/v1",
        "kind": "Schedule",
        "metadata": {"name": "nightly", "namespace": "velero"},
        "spec": {"schedule": "0 1 * * *", "template": {"includedNamespaces": ["nodes"]}},
    })
    assert _report(schedule).verdict == "ready"

    labeled = _ready()
    _clear_backup(labeled)
    _claim(labeled, "geth")["metadata"]["labels"] = {"app": "geth"}
    labeled.append(_backup(includedNamespaces=["nodes"], labelSelector={"matchLabels": {"app": "geth"}}))
    assert _ids(_report(labeled), "no-backup-signal") == ["lighthouse"]

    pod_labels = _ready()
    _clear_backup(pod_labels)
    pod_labels.append(_backup(includedNamespaces=["nodes"], orLabelSelectors=[{"matchLabels": {"app": "geth"}}]))
    assert set(_ids(_report(pod_labels), "no-backup-signal")) == {"geth", "lighthouse"}

    skipped = _ready()
    _clear_backup(skipped)
    skipped.append(_backup(includedNamespaces=["nodes"], includedResources=["deployments"]))
    skipped.append(_backup(excludedNamespaces=["nodes"]))
    skipped.append(_backup(excludedResources=["persistentvolumeclaims"]))
    skipped.append({
        "apiVersion": "velero.io/v1",
        "kind": "BackupStorageLocation",
        "metadata": {"name": "default", "namespace": "velero"},
        "spec": {},
    })
    assert set(_ids(_report(skipped), "no-backup-signal")) == {"geth", "lighthouse"}


def test_one_uncovered_claim_keeps_the_warning() -> None:
    objects = _ready()
    templates = _kind(objects, "StatefulSet", "geth")["spec"]["volumeClaimTemplates"]
    templates.append({"metadata": {"name": "chain"}, "spec": {"accessModes": ["ReadWriteOnce"]}})
    assert _ids(_report(objects), "no-backup-signal") == ["geth"]


def test_broken_has_no_signal_and_the_check_does_not_create_one() -> None:
    report = _report(load_dir(ROOT / "examples" / "broken"))
    assert "erigon" in _ids(report, "no-backup-signal")
    assert "nethermind" not in _ids(report, "no-backup-signal")
    source = (ROOT / "src" / "kubechain" / "check.py").read_text()
    source += (ROOT / "src" / "kubechain" / "live.py").read_text()
    assert "create_" not in source
    assert "urlopen" not in source
    assert "urllib" not in source
    assert "http.client" not in source
