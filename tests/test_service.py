from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()


def _report(objects: list[dict], overlay: dict | None = None):
    return evaluate(objects, PROFILES, overlay, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def _service(name: str, service_type: str, ports: list[int]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "nodes"},
        "spec": {
            "type": service_type,
            "ports": [{"port": port} for port in ports],
        },
    }


def test_ready_services_pass() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    ids = {finding.id for finding in report.findings}
    assert "engine-api-exposed" not in ids
    assert "rpc-exposed" not in ids
    assert "peer-port-missing" not in ids
    assert "statefulset-no-headless" not in ids


def test_broken_publishes_the_engine_api() -> None:
    pairs = _ids(_report(load_dir(ROOT / "examples" / "broken")))
    assert ("engine-api-exposed", "critical") in pairs


def test_engine_api_on_nodeport_or_ingress_is_critical() -> None:
    nodeport = _ready()
    nodeport.append(_service("engine", "NodePort", [8551]))
    assert ("engine-api-exposed", "critical") in _ids(_report(nodeport))

    ingress = _ready()
    ingress.append({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "engine", "namespace": "nodes"},
        "spec": {
            "rules": [{
                "http": {
                    "paths": [{
                        "path": "/",
                        "backend": {"service": {"name": "geth", "port": {"number": 8551}}},
                    }],
                },
            }],
        },
    })
    report = _report(ingress)
    assert ("engine-api-exposed", "critical") in _ids(report)
    assert _report(ingress).verdict == "not-ready"


def test_clusterip_rpc_is_not_a_finding_and_a_loadbalancer_is() -> None:
    private = _ready()
    private.append(_service("rpc", "ClusterIP", [8545, 8546]))
    report = _report(private)
    assert report.verdict == "ready"
    assert "rpc-exposed" not in {finding.id for finding in report.findings}

    public = _ready()
    public.append(_service("rpc", "LoadBalancer", [8545]))
    report = _report(public)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "rpc-exposed")
    assert finding.severity == "warning"
    assert "reachable from outside the cluster" in finding.reason

    websocket = _ready()
    websocket.append(_service("ws", "NodePort", [8546]))
    assert ("rpc-exposed", "warning") in _ids(_report(websocket))


def test_peer_port_must_be_on_the_headless_service() -> None:
    objects = _ready()
    service = _kind(objects, "Service", "geth")
    service["spec"]["ports"] = [port for port in service["spec"]["ports"] if port["port"] != 30303]
    report = _report(objects)
    assert report.verdict == "review"
    assert ("peer-port-missing", "warning") in _ids(report)

    missing = _ready()
    _kind(missing, "Service", "geth")["spec"]["clusterIP"] = "10.0.0.20"
    assert ("statefulset-no-headless", "critical") in _ids(_report(missing))


def test_profile_ports_replace_the_check() -> None:
    objects = _ready()
    service = _kind(objects, "Service", "geth")
    service["spec"]["ports"] = [
        {"name": "p2p", "port": 30304},
        {"name": "engine", "port": 8552},
        {"name": "rpc", "port": 8545},
    ]
    overlay = {"comm": "geth", "engine_port": 8552, "p2p_ports": [30304]}
    report = _report(objects, overlay)
    ids = {finding.id for finding in report.findings}
    assert "peer-port-missing" not in ids
    assert "engine-api-exposed" not in ids

    objects.append(_service("engine", "NodePort", [8552]))
    assert ("engine-api-exposed", "critical") in _ids(_report(objects, overlay))
