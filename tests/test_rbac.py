import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

PLURAL = {
    "pod": "pods",
    "service": "services",
    "secret": "secrets",
    "config_map": "configmaps",
    "persistent_volume_claim": "persistentvolumeclaims",
    "stateful_set": "statefulsets",
    "deployment": "deployments",
    "pod_disruption_budget": "poddisruptionbudgets",
    "network_policy": "networkpolicies",
    "ingress": "ingresses",
}


def test_reader_role_matches_the_live_read() -> None:
    text = (ROOT / "deploy" / "kubechain-reader.yaml").read_text()
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    assert [doc["kind"] for doc in docs] == ["Role", "RoleBinding"]
    assert "kind: ClusterRole" not in text
    assert "KubeChain does not apply this file." in text
    assert "The CLI discards them." in text
    assert "A ClusterRole with these rules would cover more than one namespace." in text
    assert "not a second manifest" in text

    resources = set()
    for rule in docs[0]["rules"]:
        assert rule["verbs"] == ["get", "list", "watch"]
        resources.update(rule["resources"])

    live = (ROOT / "src" / "kubechain" / "live.py").read_text()
    listed = set()
    for name in re.findall(r"list_namespaced_([a-z_]+)\(", live):
        if name == "custom_object":
            continue
        listed.add(PLURAL[name])
    for plural in re.findall(r'\("[^"]+", "[^"]+", "([^"]+)"\)', live):
        listed.add(plural)
    assert listed == resources
    assert "storageclasses" not in resources
    assert "storageclasses.storage.k8s.io" in text
    assert "nodes/proxy" in text

    binding = docs[1]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "kubechain-reader",
    }
    assert binding["subjects"][0]["kind"] == "ServiceAccount"

    readme = (ROOT / "README.md").read_text()
    assert "kubectl apply -f deploy/kubechain-reader.yaml -n nodes" in readme
    assert "KubeChain does not apply it." in readme
    assert "not a second manifest" in readme
