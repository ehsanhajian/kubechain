from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
SYNC = "A long sync will fail the probe and the kubelet will restart the node."


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _container(objects: list[dict], name: str) -> dict:
    return _kind(objects, "StatefulSet", name)["spec"]["template"]["spec"]["containers"][0]


def _http(port: object) -> dict:
    return {"httpGet": {"path": "/", "port": port}}


def _clear(container: dict) -> None:
    for key in ("livenessProbe", "readinessProbe", "startupProbe"):
        container.pop(key, None)


def _ids(report) -> set[str]:
    return {finding.id for finding in report.findings}


def test_ready_probes_pass() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert "liveness-kills-sync" not in _ids(report)
    assert "probes-missing" not in _ids(report)


def test_http_liveness_on_the_rpc_port_without_startup_is_a_warning() -> None:
    objects = _ready()
    container = _container(objects, "geth")
    container.pop("startupProbe")
    container["livenessProbe"] = _http(8545)
    container["readinessProbe"] = _http(8545)
    report = _report(objects)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "liveness-kills-sync")
    assert finding.severity == "warning"
    assert finding.target == "geth"
    assert finding.reason == SYNC

    websocket = _ready()
    container = _container(websocket, "geth")
    container.pop("startupProbe")
    container["livenessProbe"] = _http("8546")
    assert next(item for item in _report(websocket).findings if item.id == "liveness-kills-sync").reason == SYNC

    named = _ready()
    container = _container(named, "geth")
    container.pop("startupProbe")
    container["ports"] = [{"name": "rpc", "containerPort": 8545}]
    container["livenessProbe"] = _http("rpc")
    assert ("liveness-kills-sync", "geth") in {(item.id, item.target) for item in _report(named).findings}


def test_startup_or_a_different_port_is_acceptable_and_readiness_is_not_a_finding() -> None:
    startup = _ready()
    container = _container(startup, "geth")
    container["livenessProbe"] = _http(8545)
    container["startupProbe"] = _http(8545)
    assert "liveness-kills-sync" not in _ids(_report(startup))

    metrics = _ready()
    container = _container(metrics, "geth")
    container.pop("startupProbe")
    container["livenessProbe"] = _http(6060)
    container["readinessProbe"] = _http(8545)
    report = _report(metrics)
    assert report.verdict == "ready"
    assert "liveness-kills-sync" not in _ids(report)
    assert "probes-missing" not in _ids(report)

    engine = _ready()
    container = _container(engine, "geth")
    container.pop("startupProbe")
    container["livenessProbe"] = _http(8551)
    assert "liveness-kills-sync" not in _ids(_report(engine))

    tcp = _ready()
    container = _container(tcp, "geth")
    container.pop("startupProbe")
    container["livenessProbe"] = {"tcpSocket": {"port": 8545}}
    assert "liveness-kills-sync" not in _ids(_report(tcp))


def test_consensus_http_api_gets_the_same_warning() -> None:
    objects = _ready()
    container = _container(objects, "lighthouse")
    container.pop("startupProbe")
    container["livenessProbe"] = _http(5052)
    report = _report(objects)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "liveness-kills-sync")
    assert finding.target == "lighthouse"
    assert finding.reason == SYNC

    covered = _ready()
    container = _container(covered, "lighthouse")
    container["livenessProbe"] = _http(5052)
    container["startupProbe"] = _http(5054)
    assert "liveness-kills-sync" not in _ids(_report(covered))


def test_no_probes_on_a_recognized_client_is_a_warning() -> None:
    objects = _ready()
    _clear(_container(objects, "geth"))
    report = _report(objects)
    assert report.verdict == "review"
    findings = [item for item in report.findings if item.id == "probes-missing"]
    assert [(item.target, item.severity) for item in findings] == [("geth", "warning")]
    assert "liveness-kills-sync" not in _ids(report)


def test_mev_boost_is_exempt() -> None:
    container = {
        "name": "mev-boost",
        "image": "flashbots/mev-boost:1.7",
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "100m", "memory": "256Mi"},
        },
        "livenessProbe": _http(18550),
    }
    workload = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "mev-boost", "namespace": "nodes"},
        "spec": {
            "serviceName": "mev-boost",
            "replicas": 2,
            "template": {"spec": {"containers": [container]}},
        },
    }
    report = _report([workload])
    assert "liveness-kills-sync" not in _ids(report)
    assert "probes-missing" not in _ids(report)
    _clear(container)
    report = _report([workload])
    assert "probes-missing" not in _ids(report)
    assert report.verdict == "ready"
