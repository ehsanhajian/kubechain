from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
BUDGET_IDS = {"pdb-missing", "pdb-blocks-drain", "pdb-selector-empty"}


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _ids(report) -> set[str]:
    return {finding.id for finding in report.findings}


def _replicas(objects: list[dict], name: str, replicas: int) -> None:
    _kind(objects, "StatefulSet", name)["spec"]["replicas"] = replicas


def _budget(objects: list[dict], name: str) -> dict:
    return _kind(objects, "PodDisruptionBudget", name)["spec"]


def test_ready_budgets_pass() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert not (BUDGET_IDS & _ids(report))


def test_max_unavailable_one_passes_for_one_or_more_replicas() -> None:
    objects = _ready()
    _replicas(objects, "geth", 1)
    _replicas(objects, "lighthouse", 1)
    assert "pdb-blocks-drain" not in _ids(_report(objects))
    assert "pdb-missing" not in _ids(_report(objects))


def test_min_available_equal_to_replicas_blocks_a_drain() -> None:
    single = _ready()
    _replicas(single, "geth", 1)
    spec = _budget(single, "geth")
    spec.pop("maxUnavailable")
    spec["minAvailable"] = 1
    report = _report(single)
    finding = next(item for item in report.findings if item.id == "pdb-blocks-drain" and item.target == "geth")
    assert finding.severity == "warning"
    assert finding.reason == "minAvailable equals replicas, so a voluntary drain cannot proceed."
    assert finding.action == "A single replica cannot be evicted, so a node upgrade needs a plan other than a drain."

    pair = _ready()
    spec = _budget(pair, "geth")
    spec.pop("maxUnavailable")
    spec["minAvailable"] = 2
    finding = next(item for item in _report(pair).findings if item.id == "pdb-blocks-drain")
    assert finding.target == "geth"
    assert "voluntary drain cannot proceed" in finding.reason

    allowed = _ready()
    spec = _budget(allowed, "geth")
    spec.pop("maxUnavailable")
    spec["minAvailable"] = 1
    assert "pdb-blocks-drain" not in _ids(_report(allowed))

    text = _ready()
    _replicas(text, "lighthouse", 1)
    spec = _budget(text, "lighthouse")
    spec.pop("maxUnavailable")
    spec["minAvailable"] = "1"
    assert ("pdb-blocks-drain", "lighthouse") in {(item.id, item.target) for item in _report(text).findings}

    full = _ready()
    spec = _budget(full, "lighthouse")
    spec.pop("maxUnavailable")
    spec["minAvailable"] = "100%"
    assert any(item.id == "pdb-blocks-drain" and item.target == "lighthouse" for item in _report(full).findings)

    half = _ready()
    spec = _budget(half, "lighthouse")
    spec.pop("maxUnavailable")
    spec["minAvailable"] = "50%"
    assert not any(item.id == "pdb-blocks-drain" and item.target == "lighthouse" for item in _report(half).findings)


def test_a_missing_budget_or_a_selector_that_matches_nothing_is_a_warning() -> None:
    missing = [obj for obj in _ready() if obj.get("kind") != "PodDisruptionBudget"]
    report = _report(missing)
    assert report.verdict == "review"
    findings = [item for item in report.findings if item.id == "pdb-missing"]
    assert {item.target for item in findings} == {"geth", "lighthouse"}
    assert all(item.severity == "warning" for item in findings)
    assert not any(name.startswith("PodDisruptionBudget/") for name in report.objects)
    source = (ROOT / "src" / "kubechain" / "check.py").read_text()
    assert "create_namespaced_pod_disruption_budget" not in source

    missed = _ready()
    _budget(missed, "geth")["selector"] = {"matchLabels": {"app": "other"}}
    report = _report(missed)
    assert ("pdb-selector-empty", "geth") in {(item.id, item.target) for item in report.findings}
    assert ("pdb-missing", "geth") in {(item.id, item.target) for item in report.findings}

    extra = _ready()
    extra.append({
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": "spare", "namespace": "nodes"},
        "spec": {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "spare"}}},
    })
    findings = [item for item in _report(extra).findings if item.id == "pdb-selector-empty"]
    assert [(item.target, item.severity) for item in findings] == [("spare", "warning")]
    assert "pdb-missing" not in _ids(_report(extra))
