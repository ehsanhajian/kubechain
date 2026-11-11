from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
POLICY_IDS = {"network-policy-missing", "engine-api-open", "peer-port-blocked"}


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _rules(objects: list[dict], name: str) -> list[dict]:
    return _kind(objects, "NetworkPolicy", name)["spec"]["ingress"]


def _ids(report) -> set[str]:
    return {finding.id for finding in report.findings}


def test_ready_policy_passes() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert not (POLICY_IDS & _ids(report))


def test_broken_leaves_the_engine_api_open_to_every_pod() -> None:
    report = _report(load_dir(ROOT / "examples" / "broken"))
    findings = [item for item in report.findings if item.id == "engine-api-open"]
    assert findings
    assert all(item.severity == "warning" for item in findings)
    assert all(item.reason == "A NetworkPolicy leaves the engine API open to every pod." for item in findings)
    assert "peer-port-blocked" in _ids(report)


def test_no_policy_is_a_warning_and_none_is_invented() -> None:
    objects = [obj for obj in _ready() if obj.get("kind") != "NetworkPolicy"]
    report = _report(objects)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "network-policy-missing")
    assert finding.severity == "warning"
    assert "no default deny" in finding.reason
    assert "CNI is not enforcing policy" in finding.reason
    assert not any(name.startswith("NetworkPolicy/") for name in report.objects)
    source = (ROOT / "src" / "kubechain" / "check.py").read_text()
    assert "kubectl" not in source
    assert "create_namespaced_network_policy" not in source


def test_engine_api_is_only_from_the_consensus_workload() -> None:
    empty_namespace = _ready()
    _rules(empty_namespace, "geth")[1]["from"] = [{"namespaceSelector": {}}]
    report = _report(empty_namespace)
    finding = next(item for item in report.findings if item.id == "engine-api-open" and item.target == "geth")
    assert finding.severity == "warning"
    assert finding.reason == "A NetworkPolicy leaves the engine API open with an empty namespace selector."

    other = _ready()
    _rules(other, "geth")[1]["from"] = [{"podSelector": {"matchLabels": {"app": "redis"}}}]
    finding = next(item for item in _report(other).findings if item.id == "engine-api-open")
    assert "consensus workload" in finding.reason

    both = _ready()
    _rules(both, "geth")[1]["from"].append({"ipBlock": {"cidr": "0.0.0.0/0"}})
    assert "engine-api-open" in _ids(_report(both))

    consensus_anywhere = _ready()
    _rules(consensus_anywhere, "geth")[1]["from"] = [{
        "namespaceSelector": {},
        "podSelector": {"matchLabels": {"app": "lighthouse"}},
    }]
    assert "engine-api-open" not in _ids(_report(consensus_anywhere))


def test_the_peer_port_must_be_allowed_from_outside() -> None:
    blocked = _ready()
    _rules(blocked, "geth")[0]["from"] = [{"podSelector": {"matchLabels": {"app": "geth"}}}]
    report = _report(blocked)
    assert report.verdict == "review"
    findings = [item for item in report.findings if item.id == "peer-port-blocked"]
    assert [(item.target, item.severity) for item in findings] == [("geth", "warning")]

    opened = _ready()
    _rules(opened, "geth")[0].pop("from")
    assert "peer-port-blocked" not in _ids(_report(opened))
