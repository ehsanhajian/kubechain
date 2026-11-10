from copy import deepcopy
from pathlib import Path

import pytest

from kubechain.catalog import catalog
from kubechain.check import MOVING_TAGS, evaluate
from kubechain.load import load_dir

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
DIGEST = "a" * 64


def _report(objects: list[dict]):
    return evaluate(objects, PROFILES, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _container(objects: list[dict], workload: str) -> dict:
    return _kind(objects, "StatefulSet", workload)["spec"]["template"]["spec"]["containers"][0]


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def _image_findings(report):
    return [finding for finding in report.findings if finding.id == "image-moving-tag"]


def test_ready_release_tags_are_not_findings() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert report.verdict == "ready"
    assert _image_findings(report) == []


def test_broken_latest_tags_are_warnings() -> None:
    findings = _image_findings(_report(load_dir(ROOT / "examples" / "broken")))
    assert {finding.target for finding in findings} >= {"geth", "erigon", "nethermind"}
    assert {finding.severity for finding in findings} == {"warning"}
    assert all("moving tag" in finding.action for finding in findings)


@pytest.mark.parametrize("tag", sorted(MOVING_TAGS | {""}))
def test_latest_branch_and_no_tag_are_warnings(tag: str) -> None:
    objects = _ready()
    image = "ethereum/client-go" if tag == "" else f"ethereum/client-go:{tag}"
    _container(objects, "geth")["image"] = image
    report = _report(objects)
    assert report.verdict == "review"
    finding = next(item for item in _image_findings(report) if item.target == "geth")
    assert finding.severity == "warning"
    assert image in finding.reason
    assert "moving tag" in finding.action


def test_a_digest_pin_is_not_a_finding() -> None:
    pins = [
        f"ethereum/client-go@sha256:{DIGEST}",
        f"ethereum/client-go:latest@sha256:{DIGEST}",
        f"ethereum/client-go:latest@SHA256:{DIGEST.upper()}",
        f"localhost:5000/ethereum/client-go@sha256:{DIGEST}",
        "localhost:5000/ethereum/client-go:v1.15.11",
        f"ethereum/client-go:v1.15.11@sha256:{'ab' * 4}",
    ]
    for image in pins:
        objects = _ready()
        _container(objects, "geth")["image"] = image
        report = _report(objects)
        assert _image_findings(report) == [], image
        assert report.verdict == "ready"

    moving = _ready()
    _container(moving, "geth")["image"] = f"ethereum/client-go:latest@sha256:{'ab' * 4}"
    assert ("image-moving-tag", "warning") in _ids(_report(moving))
    untagged = _ready()
    _container(untagged, "geth")["image"] = "localhost:5000/ethereum/client-go"
    assert ("image-moving-tag", "warning") in _ids(_report(untagged))


def test_a_stateless_proxy_is_info() -> None:
    proxy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "proxy", "namespace": "nodes"},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "proxy", "image": "nginx:latest"}]}},
        },
    }
    report = _report([proxy])
    findings = _image_findings(report)
    assert [(finding.severity, finding.reason) for finding in findings] == [
        ("info", "The proxy image nginx:latest is not pinned."),
    ]
    assert report.verdict == "ready"

    proxy["spec"]["template"]["spec"]["containers"][0]["image"] = "nginx:1.27.0"
    assert _image_findings(_report([proxy])) == []
    proxy["spec"]["template"]["spec"]["containers"][0]["image"] = f"nginx@sha256:{DIGEST}"
    assert _image_findings(_report([proxy])) == []


def test_chain_data_uses_a_warning_and_a_sidecar_does_not() -> None:
    held = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "geth", "namespace": "nodes"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": "geth",
                        "image": "ethereum/client-go:latest",
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    }],
                    "volumes": [{"name": "data", "emptyDir": {}}],
                },
            },
        },
    }
    findings = _image_findings(_report([held]))
    assert [(finding.target, finding.severity) for finding in findings] == [("geth", "warning")]

    objects = _ready()
    spec = _kind(objects, "StatefulSet", "geth")["spec"]["template"]["spec"]
    spec["containers"].append({"name": "metrics", "image": "busybox:latest"})
    assert _image_findings(_report(objects)) == []

    unknown = _ready()
    container = _container(unknown, "geth")
    container["name"] = "node"
    container["image"] = "example/node:main"
    findings = _image_findings(_report(unknown))
    assert [(finding.target, finding.severity) for finding in findings] == [("geth", "warning")]
    assert "example/node:main" in findings[0].reason


def test_image_check_does_not_call_a_registry() -> None:
    source = (ROOT / "src" / "kubechain" / "check.py").read_text()
    for word in ("urllib", "urlopen", "http.client", "import socket", "import requests", "requests.get"):
        assert word not in source
