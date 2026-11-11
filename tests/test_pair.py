from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
PAIR_IDS = {"consensus-missing", "jwt-secret-mismatch", "engine-endpoint-missing"}


def _report(objects: list[dict], overlay: dict | None = None):
    return evaluate(objects, PROFILES, overlay, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _container(objects: list[dict], name: str) -> dict:
    return _kind(objects, "StatefulSet", name)["spec"]["template"]["spec"]["containers"][0]


def _ids(report) -> set[str]:
    return {finding.id for finding in report.findings}


def _client(name: str, secret: str, args: list[str]) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": "nodes"},
        "spec": {
            "serviceName": name,
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [{
                        "name": name,
                        "image": f"client/{name}:v1.0.0",
                        "args": args,
                        "volumeMounts": [{"name": "jwt", "mountPath": "/secrets/jwt"}],
                    }],
                    "volumes": [{"name": "jwt", "secret": {"secretName": secret}}],
                },
            },
        },
    }


def test_ready_pair_shares_the_jwt_and_the_engine_endpoint() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert not (PAIR_IDS & _ids(report))

    named = _ready()
    _container(named, "lighthouse")["args"] = ["--execution-endpoint=http://geth.nodes.svc:8551"]
    assert "engine-endpoint-missing" not in _ids(_report(named))


def test_execution_without_consensus_names_the_other_namespace() -> None:
    objects = [obj for obj in _ready() if (obj.get("metadata") or {}).get("name") != "lighthouse" or obj.get("kind") != "StatefulSet"]
    report = _report(objects)
    finding = next(item for item in report.findings if item.id == "consensus-missing")
    assert finding.severity == "warning"
    assert finding.target == "geth"
    assert "another namespace" in finding.reason
    assert "all-namespaces" not in (ROOT / "src" / "kubechain" / "cli.py").read_text()


def test_different_jwt_secret_names_are_critical() -> None:
    objects = _ready()
    spec = _kind(objects, "StatefulSet", "lighthouse")["spec"]["template"]["spec"]
    spec["volumes"][0]["secret"]["secretName"] = "jwt-consensus"
    report = _report(objects)
    finding = next(item for item in report.findings if item.id == "jwt-secret-mismatch")
    assert finding.severity == "critical"
    assert "jwt-secret" in finding.reason
    assert "jwt-consensus" in finding.reason
    assert report.verdict == "not-ready"

    bare = _ready()
    spec = _kind(bare, "StatefulSet", "lighthouse")["spec"]["template"]["spec"]
    spec["containers"][0]["volumeMounts"] = [
        mount for mount in spec["containers"][0]["volumeMounts"] if mount["name"] != "jwt"
    ]
    spec["volumes"] = []
    finding = next(item for item in _report(bare).findings if item.id == "jwt-secret-mismatch")
    assert "Execution is jwt-secret." in finding.reason
    assert "Consensus is none." in finding.reason


def test_the_engine_endpoint_must_name_the_service_and_the_profile_port() -> None:
    wrong_port = _ready()
    _container(wrong_port, "lighthouse")["args"] = ["--execution-endpoint=http://geth:8545"]
    report = _report(wrong_port)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "engine-endpoint-missing")
    assert finding.severity == "warning"
    assert finding.target == "lighthouse"

    other_service = _ready()
    _kind(other_service, "StatefulSet", "geth")["spec"]["serviceName"] = "execution"
    assert "engine-endpoint-missing" in _ids(_report(other_service))
    _container(other_service, "lighthouse")["args"] = ["--execution-endpoint=http://execution:8551"]
    assert "engine-endpoint-missing" not in _ids(_report(other_service))

    longer = _ready()
    _container(longer, "lighthouse")["args"] = ["--execution-endpoint=http://geth:85510"]
    assert "engine-endpoint-missing" in _ids(_report(longer))

    moved = _ready()
    assert "engine-endpoint-missing" in _ids(_report(moved, {"comm": "geth", "engine_port": 8552}))
    _container(moved, "lighthouse")["args"] = ["--execution-endpoint=http://geth:8552"]
    assert "engine-endpoint-missing" not in _ids(_report(moved, {"comm": "geth", "engine_port": 8552}))


def test_op_node_is_the_consensus_side_for_op_geth() -> None:
    execution = _client("op-geth", "jwt-secret", [])
    report = _report([execution])
    assert "consensus-missing" in _ids(report)

    consensus = _client("op-node", "jwt-secret", ["--l2.engine=http://op-geth:8551"])
    report = _report([execution, consensus])
    assert not (PAIR_IDS & _ids(report))

    consensus["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] = "jwt-other"
    finding = next(item for item in _report([execution, consensus]).findings if item.id == "jwt-secret-mismatch")
    assert finding.severity == "critical"
    assert "jwt-other" in finding.reason
