from kubechain.catalog import catalog
from kubechain.check import DATA_NAMES, evaluate

PROFILES = catalog()


def _deployment(name: str, volumes: list[dict] | None = None, **spec) -> dict:
    template = {
        "metadata": {"labels": {"app": name}},
        "spec": {
            "containers": [{"name": "geth", "image": "ethereum/client-go:v1.15.11"}],
        },
    }
    if volumes:
        template["spec"]["volumes"] = volumes
    body = {"replicas": 1, "template": template}
    body.update(spec)
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "nodes"},
        "spec": body,
    }


def _report(objects: list[dict], overlay: dict | None = None):
    return evaluate(objects, PROFILES, overlay, live=False, usage=None, classes_known=True)


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def test_chain_data_names_are_one_list() -> None:
    assert DATA_NAMES == {"data", "chain", "geth", "nethermind"}


def test_claim_host_path_and_emptydir_are_critical() -> None:
    volumes = [
        ("data", {"emptyDir": {}}),
        ("chain", {"hostPath": {"path": "/var/lib/chain"}}),
        ("geth", {"persistentVolumeClaim": {"claimName": "geth"}}),
        ("nethermind", {"persistentVolumeClaim": {"claimName": "nethermind"}}),
    ]
    for name, source in volumes:
        report = _report([
            _deployment("client", [{"name": name, **source}], strategy={"type": "Recreate"}),
        ])
        assert ("deployment-holds-chain-data", "critical") in _ids(report)
        assert "deployment-recreate" not in {finding.id for finding in report.findings}


def test_other_volumes_stay_a_proxy() -> None:
    for volume in (
        {"name": "cache", "emptyDir": {}},
        {"name": "data", "secret": {"secretName": "jwt-secret"}},
        {"name": "data", "configMap": {"name": "config"}},
    ):
        report = _report([_deployment("proxy", [volume])])
        ids = {finding.id for finding in report.findings}
        assert "deployment-holds-chain-data" not in ids
        assert "jwt-missing" not in ids
        assert "probes-missing" not in ids
        assert "resources-no-requests" not in ids
        assert report.verdict == "ready"


def test_a_proxy_is_not_judged_as_a_node() -> None:
    report = _report([_deployment("geth")])
    ids = {finding.id for finding in report.findings}
    assert "deployment-holds-chain-data" not in ids
    assert "jwt-missing" not in ids
    assert report.verdict == "ready"


def test_profile_volume_name_extends_the_list() -> None:
    overlay = {"comm": "geth", "data_volume": "ancient"}
    held = _report(
        [_deployment("geth", [{"name": "ancient", "emptyDir": {}}])],
        overlay,
    )
    assert ("deployment-holds-chain-data", "critical") in _ids(held)
    other = _report(
        [_deployment("geth", [{"name": "cache", "emptyDir": {}}])],
        overlay,
    )
    assert "deployment-holds-chain-data" not in {finding.id for finding in other.findings}


def test_recreate_is_info_and_single_replica_max_unavailable_is_a_warning() -> None:
    recreate = _report([_deployment("proxy", strategy={"type": "Recreate"})])
    assert recreate.verdict == "ready"
    finding = next(item for item in recreate.findings if item.id == "deployment-recreate")
    assert finding.severity == "info"

    warning = _report([
        _deployment(
            "proxy",
            replicas=1,
            strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}},
        )
    ])
    assert warning.verdict == "review"
    assert ("deployment-max-unavailable", "warning") in _ids(warning)

    pair = _report([
        _deployment(
            "proxy",
            replicas=2,
            strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}},
        )
    ])
    assert "deployment-max-unavailable" not in {finding.id for finding in pair.findings}
