from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.live import ReadOnly, strip_secret, usage_from_summary


def test_catalog_floors_have_a_source() -> None:
    profiles = catalog()
    assert set(profiles) >= {
        "geth", "reth", "nethermind", "erigon", "besu", "op-geth",
        "lighthouse", "prysm", "teku", "nimbus", "lodestar", "op-node", "mev-boost",
    }
    for profile in profiles.values():
        assert profile.source
        assert profile.cpu
        assert profile.memory


def test_volume_usage_thresholds() -> None:
    summary = {
        "pods": [
            {
                "podRef": {"name": "geth-0", "namespace": "nodes"},
                "volume": [
                    {"pvcRef": {"name": "data-geth-0"}, "usedBytes": 81, "capacityBytes": 100},
                    {"pvcRef": {"name": "data-geth-1"}, "usedBytes": 96, "capacityBytes": 100},
                ],
            }
        ]
    }
    rows = usage_from_summary(summary, "nodes")
    report = evaluate([], catalog(), live=True, usage=rows, classes_known=True)
    ids = {finding.id for finding in report.findings}
    assert ids == {"storage-filling", "storage-almost-full"}
    assert report.verdict == "not-ready"
    assert "storage performance was not read" not in report.notes


def test_secret_bytes_are_discarded() -> None:
    stripped = strip_secret({
        "kind": "Secret",
        "metadata": {"name": "jwt", "managedFields": [{"manager": "kubectl"}]},
        "data": {"jwtsecret": "ZXhhbXBsZQ=="},
        "stringData": {"jwtsecret": "example"},
    })
    assert "data" not in stripped
    assert "stringData" not in stripped
    assert "managedFields" not in stripped["metadata"]


def test_read_only_client_refuses_writes() -> None:
    class Api:
        def list_namespaced_pod(self, namespace: str) -> str:
            return namespace

        def create_namespaced_pod(self) -> None:
            raise AssertionError("write")

        def delete_collection_namespaced_pod(self) -> None:
            raise AssertionError("write")

    client = ReadOnly(Api())
    assert client.list_namespaced_pod("nodes") == "nodes"
    for name in ("create_namespaced_pod", "update_namespaced_pod", "patch_namespaced_pod", "delete_collection_namespaced_pod"):
        try:
            getattr(client, name)
        except RuntimeError as exc:
            assert "refusing" in str(exc)
        else:
            raise AssertionError(name)
