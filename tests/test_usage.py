import json
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.live import _usage
from kubechain.load import load_dir
from kubechain.model import exit_code

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()


def _report(objects: list[dict], usage: list[dict] | None):
    return evaluate(objects, PROFILES, live=usage is not None, usage=usage, classes_known=True)


def _pvc(name: str, size: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": "nodes"},
        "spec": {"resources": {"requests": {"storage": size}}},
    }


def _row(claim: str, used: int) -> dict:
    return {"claim": claim, "used": used}


def test_manifests_skip_volume_usage() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"), None)
    assert exit_code(report) == 0
    assert "storage performance was not read" in report.notes
    assert "restarts were not read" in report.notes
    assert not any(finding.id in {"storage-filling", "storage-almost-full"} for finding in report.findings)


def test_used_bytes_are_compared_with_the_pvc_request() -> None:
    claims = [_pvc("data-geth-0", "100"), _pvc("data-lighthouse-0", "100Gi")]
    quiet = _report(claims, [_row("data-geth-0", 80), _row("data-lighthouse-0", 80)])
    assert not any(finding.id in {"storage-filling", "storage-almost-full"} for finding in quiet.findings)

    filling = _report(claims, [_row("data-geth-0", 81)])
    finding = next(item for item in filling.findings if item.id == "storage-filling")
    assert finding.severity == "warning"
    assert finding.target == "data-geth-0"
    assert finding.reason == "Used 81 bytes of the 100 PVC request, above 80%."
    assert exit_code(filling) == 1

    at_95 = _report(claims, [_row("data-geth-0", 95)])
    assert {item.id for item in at_95.findings} == {"storage-filling"}

    full = _report(claims, [_row("data-geth-0", 96), _row("data-geth-0", 90)])
    findings = [item for item in full.findings if item.target == "data-geth-0"]
    assert [(item.id, item.severity) for item in findings] == [("storage-almost-full", "critical")]
    assert findings[0].reason == "Used 96 bytes of the 100 PVC request, above 95%."
    assert exit_code(full) == 2

    unknown = _report([], [_row("data-geth-0", 99)])
    assert not any(finding.id in {"storage-filling", "storage-almost-full"} for finding in unknown.findings)


def test_a_missing_stats_endpoint_is_not_a_finding() -> None:
    class Down:
        def connect_get_node_proxy_with_path(self, node: str, path: str) -> str:
            raise ConnectionError(node)

    pods = [
        {"kind": "Pod", "metadata": {"name": "geth-0"}, "spec": {"nodeName": "node-a"}},
        {"kind": "Pod", "metadata": {"name": "geth-1"}, "spec": {}},
    ]
    assert _usage(Down(), "nodes", pods) is None
    assert _usage(Down(), "nodes", [{"kind": "Pod", "spec": {}}]) is None
    report = _report([_pvc("data-geth-0", "100Gi")], None)
    assert "storage performance was not read" in report.notes
    assert not any(finding.id in {"storage-filling", "storage-almost-full"} for finding in report.findings)
    assert exit_code(report) == 0

    class Mixed:
        def connect_get_node_proxy_with_path(self, node: str, path: str) -> str:
            assert path == "stats/summary"
            if node == "bad":
                raise ConnectionError("stats down")
            return json.dumps({
                "pods": [{
                    "podRef": {"name": "geth-0", "namespace": "nodes"},
                    "volume": [{
                        "pvcRef": {"name": "data-geth-0"},
                        "usedBytes": 90,
                        "capacityBytes": 1000,
                    }],
                }]
            })

    rows = _usage(Mixed(), "nodes", [
        {"kind": "Pod", "spec": {"nodeName": "bad"}},
        {"kind": "Pod", "spec": {"nodeName": "node-a"}},
    ])
    assert rows == [{"claim": "data-geth-0", "used": 90}]
    report = _report([_pvc("data-geth-0", "100")], rows)
    assert "storage performance was not read" not in report.notes
    assert any(finding.id == "storage-filling" for finding in report.findings)


def test_volume_usage_does_not_use_prometheus() -> None:
    for name in ("check.py", "live.py"):
        source = (ROOT / "src" / "kubechain" / name).read_text().lower()
        assert "prometheus" not in source
