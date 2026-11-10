from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog, resolve
from kubechain.check import evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()

EXECUTION = {
    "geth": "ethereum/client-go:v1.15.11",
    "reth": "ghcr.io/paradigmxyz/reth:v1.0.0",
    "nethermind": "nethermind/nethermind:1.27.0",
    "erigon": "thorax/erigon:v3.0.0",
    "besu": "hyperledger/besu:24.6.0",
    "op-geth": "us-docker.pkg.dev/oplabs-tools-artifacts/images/op-geth:v1.101500.0",
}
CONSENSUS = {
    "lighthouse": ("sigp/lighthouse:v7.0.1", [9000], [5052]),
    "prysm": ("gcr.io/prysmaticlabs/prysm/beacon-chain:v5.0.0", [13000], [3500]),
    "teku": ("consensys/teku:24.6.0", [9000], [5051]),
    "nimbus": ("statusim/nimbus-eth2:24.6.0", [9000], [5052]),
    "lodestar": ("chainsafe/lodestar:v1.19.0", [9000], [9596]),
    "op-node": ("us-docker.pkg.dev/oplabs-tools-artifacts/images/op-node:v1.7.0", [9222], [9545]),
}
CLIENT_IDS = {"resources-no-requests", "resources-below-floor", "jwt-missing", "probes-missing"}


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


def _ids(report) -> set[str]:
    return {finding.id for finding in report.findings}


def _workload(name: str, image: str, *, kind: str = "StatefulSet", resources: dict | None = None) -> dict:
    container = {"name": name, "image": image}
    if resources is not None:
        container["resources"] = resources
    spec = {"template": {"spec": {"containers": [container]}}}
    if kind == "StatefulSet":
        spec["serviceName"] = name
        spec["replicas"] = 2
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "nodes"},
        "spec": spec,
    }


def test_every_client_has_ports_a_floor_and_a_source() -> None:
    assert set(PROFILES) == set(EXECUTION) | set(CONSENSUS) | {"mev-boost"}
    for name, profile in PROFILES.items():
        assert profile.comm == name
        assert profile.cpu
        assert profile.memory
        assert "\n" not in profile.source
        assert "KubeChain floor" in profile.source
    for name in EXECUTION:
        profile = PROFILES[name]
        assert profile.role == "execution"
        assert profile.p2p_ports == (30303,)
        assert profile.rpc_ports == (8545, 8546)
        assert profile.engine_port == 8551
        assert profile.jwt is True
        assert profile.data_volume == "data"
        assert profile.cpu == "2"
        assert profile.memory == "8Gi"
    for name, (_image, p2p, rpc) in CONSENSUS.items():
        profile = PROFILES[name]
        assert profile.role == "consensus"
        assert profile.p2p_ports == tuple(p2p)
        assert profile.rpc_ports == tuple(rpc)
        assert profile.engine_port == 8551
        assert profile.jwt is True
        assert profile.data_volume == "data"
        assert profile.cpu == "1"
        assert profile.memory == "2Gi"
    relay = PROFILES["mev-boost"]
    assert relay.role == "relay"
    assert relay.p2p_ports == ()
    assert relay.rpc_ports == (18550,)
    assert relay.engine_port is None
    assert relay.jwt is False
    assert relay.data_volume == ""
    assert relay.cpu == "100m"
    assert relay.memory == "256Mi"


def test_name_or_image_selects_the_client() -> None:
    images = {name: image for name, image in EXECUTION.items()}
    images.update({name: image for name, (image, _p2p, _rpc) in CONSENSUS.items()})
    images["mev-boost"] = "flashbots/mev-boost:1.7"
    for name, image in images.items():
        by_name = resolve({"name": name, "image": "library/pause:3.10"}, PROFILES, None)
        by_image = resolve({"name": "client", "image": image}, PROFILES, None)
        assert by_name is not None and by_name.comm == name
        assert by_image is not None and by_image.comm == name
    assert resolve({"name": "Geth", "image": "library/pause:3.10"}, PROFILES, None).comm == "geth"
    assert resolve({"name": "geth", "image": "sigp/lighthouse:v7.0.1"}, PROFILES, None).comm == "geth"
    assert resolve({"name": "client", "image": images["op-geth"]}, PROFILES, None).comm == "op-geth"
    assert resolve({"name": "op-geth", "image": images["geth"]}, PROFILES, None).comm == "op-geth"
    assert resolve({"name": "client", "image": images["nimbus"]}, PROFILES, None).comm == "nimbus"


def test_ready_requests_and_equal_limits_are_not_findings() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert not (CLIENT_IDS & _ids(report))


def test_missing_or_low_resources_are_warnings() -> None:
    objects = _ready()
    resources = _container(objects, "geth")["resources"]
    resources["requests"].pop("cpu")
    report = _report(objects)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "resources-no-requests")
    assert finding.severity == "warning"
    assert finding.target == "geth"
    assert finding.reason.startswith("Missing cpu requests.")
    assert "KubeChain floor" in finding.reason

    objects = _ready()
    resources = _container(objects, "geth")["resources"]
    resources["requests"]["cpu"] = "500m"
    resources["limits"]["cpu"] = "500m"
    findings = [item for item in _report(objects).findings if item.id == "resources-below-floor"]
    assert [item.reason.split(".")[0] for item in findings] == [
        "The cpu request 500m is below the floor 2",
        "The cpu limit 500m is below the floor 2",
    ]
    assert all(item.severity == "warning" and item.target == "geth/cpu" for item in findings)
    assert all("equal" not in item.reason for item in findings)

    objects = _ready()
    resources = _container(objects, "geth")["resources"]
    resources["requests"]["memory"] = "8192Mi"
    resources["limits"]["cpu"] = "2000m"
    resources["limits"]["memory"] = "8Gi"
    assert "resources-below-floor" not in _ids(_report(objects))

    objects = _ready()
    resources = _container(objects, "lighthouse")["resources"]
    resources["limits"]["memory"] = "1Gi"
    finding = next(item for item in _report(objects).findings if item.id == "resources-below-floor")
    assert finding.target == "lighthouse/memory"
    assert "The memory limit 1Gi is below the floor 2Gi." in finding.reason
    assert "KubeChain floor" in finding.reason


def test_mev_boost_uses_its_floor_without_chain_data() -> None:
    bare = _workload("mev-boost", "flashbots/mev-boost:1.7", kind="Deployment")
    report = _report([bare])
    finding = next(item for item in report.findings if item.id == "resources-no-requests")
    assert finding.severity == "warning"
    assert "Missing cpu and memory requests." in finding.reason
    assert "KubeChain floor" in finding.reason
    assert "jwt-missing" not in _ids(report)
    assert "probes-missing" not in _ids(report)

    sized = _workload(
        "mev-boost",
        "flashbots/mev-boost:1.7",
        kind="Deployment",
        resources={
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "100m", "memory": "256Mi"},
        },
    )
    assert not (CLIENT_IDS & _ids(_report([sized])))

    low = _workload(
        "mev-boost",
        "flashbots/mev-boost:1.7",
        kind="Deployment",
        resources={
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "50m", "memory": "256Mi"},
        },
    )
    finding = next(item for item in _report([low]).findings if item.id == "resources-below-floor")
    assert "The cpu limit 50m is below the floor 100m." in finding.reason


def test_an_unrecognized_workload_stays_on_generic_checks() -> None:
    workload = _workload("redis", "redis:latest", kind="Deployment")
    report = _report([workload])
    assert "Deployment/redis" in report.objects
    assert ("image-moving-tag", "info") in {(item.id, item.severity) for item in report.findings}
    assert not (CLIENT_IDS & _ids(report))
    assert resolve({"name": "redis", "image": "redis:7.2.4"}, PROFILES, None) is None
