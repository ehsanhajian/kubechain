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


def _pod(objects: list[dict], name: str) -> dict:
    return _kind(objects, "StatefulSet", name)["spec"]["template"]["spec"]


def _clear_spread(objects: list[dict]) -> None:
    for name in ("geth", "lighthouse"):
        pod = _pod(objects, name)
        pod.pop("affinity", None)
        pod.pop("topologySpreadConstraints", None)


def _ids(report) -> set[str]:
    return {finding.target for finding in report.findings if finding.id == "pods-colocated"}


def test_ready_hostname_anti_affinity_passes() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert _ids(report) == set()


def test_two_replicas_without_a_spread_is_a_warning() -> None:
    objects = _ready()
    _clear_spread(objects)
    report = _report(objects)
    assert report.verdict == "review"
    findings = [item for item in report.findings if item.id == "pods-colocated"]
    assert {item.target for item in findings} == {"geth", "lighthouse"}
    assert all(item.severity == "warning" for item in findings)
    assert all(item.reason == "Two pods can land on one node and share a disk failure." for item in findings)
    assert all("kubernetes.io/hostname" in item.action for item in findings)
    assert all("maxSkew 1" in item.action for item in findings)

    quoted = _ready()
    _clear_spread(quoted)
    _kind(quoted, "StatefulSet", "geth")["spec"]["replicas"] = "2"
    assert "geth" in _ids(_report(quoted))


def test_a_single_replica_skips_the_spread_check() -> None:
    objects = _ready()
    _clear_spread(objects)
    for name in ("geth", "lighthouse"):
        _kind(objects, "StatefulSet", name)["spec"]["replicas"] = 1
    assert _ids(_report(objects)) == set()

    omitted = _ready()
    _clear_spread(omitted)
    for name in ("geth", "lighthouse"):
        del _kind(omitted, "StatefulSet", name)["spec"]["replicas"]
    assert _ids(_report(omitted)) == set()

    zero = _ready()
    _clear_spread(zero)
    _kind(zero, "StatefulSet", "geth")["spec"]["replicas"] = 0
    assert "geth" not in _ids(_report(zero))


def test_hostname_spread_passes_and_a_zone_spread_does_not() -> None:
    preferred = _ready()
    _clear_spread(preferred)
    _pod(preferred, "geth")["affinity"] = {
        "podAntiAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [{
                "weight": 100,
                "podAffinityTerm": {"topologyKey": "kubernetes.io/hostname"},
            }],
        },
    }
    _pod(preferred, "lighthouse")["topologySpreadConstraints"] = [{
        "maxSkew": "1",
        "topologyKey": "kubernetes.io/hostname",
        "whenUnsatisfiable": "DoNotSchedule",
    }]
    assert _report(preferred).verdict == "ready"

    zone = _ready()
    _clear_spread(zone)
    for name in ("geth", "lighthouse"):
        _pod(zone, name)["affinity"] = {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [{
                    "topologyKey": "topology.kubernetes.io/zone",
                }],
            },
        }
        _pod(zone, name)["topologySpreadConstraints"] = [{
            "maxSkew": 1,
            "topologyKey": "topology.kubernetes.io/zone",
        }]
    assert _ids(_report(zone)) == {"geth", "lighthouse"}

    loose = _ready()
    _clear_spread(loose)
    _pod(loose, "geth")["topologySpreadConstraints"] = [{
        "maxSkew": 2,
        "topologyKey": "kubernetes.io/hostname",
    }]
    assert "geth" in _ids(_report(loose))


def test_a_deployment_proxy_is_outside_the_spread_check() -> None:
    proxy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "proxy", "namespace": "nodes"},
        "spec": {
            "replicas": 2,
            "template": {"spec": {"containers": [{"name": "proxy", "image": "nginx:1.27.0"}]}},
        },
    }
    report = _report([proxy])
    assert "pods-colocated" not in {item.id for item in report.findings}
    assert report.verdict == "ready"
