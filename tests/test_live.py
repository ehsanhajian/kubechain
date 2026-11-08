import base64
import json

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.cli import main
from kubechain.live import LiveError, ReadOnly, collect
from kubechain.load import load_dir
from kubechain.render import render_json

CONFIG = """\
apiVersion: v1
kind: Config
current-context: a
contexts:
  - name: a
    context: {cluster: a, user: a}
  - name: b
    context: {cluster: b, user: b}
clusters:
  - name: a
    cluster: {server: https://a.example:6443}
  - name: b
    cluster: {server: https://b.example:6443}
users:
  - name: a
    user: {token: a-token}
  - name: b
    user: {token: b-token}
"""

SECRET_VALUE = "super-secret-value"
SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "jwt-secret", "namespace": "nodes"},
    "type": "Opaque",
    "data": {"jwtsecret": base64.b64encode(SECRET_VALUE.encode()).decode()},
    "stringData": {"jwtsecret": SECRET_VALUE},
}
POD = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "geth-0", "namespace": "nodes"},
    "spec": {
        "containers": [
            {
                "name": "geth",
                "image": "ethereum/client-go:v1.15.11",
                "volumeMounts": [{"name": "jwt", "mountPath": "/secrets/jwt"}],
            }
        ],
        "volumes": [{"name": "jwt", "secret": {"secretName": "jwt-secret"}}],
    },
}
CONFIGMAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "settings", "namespace": "nodes"},
    "data": {"chain": "mainnet"},
}


@pytest.fixture
def kubeconfig(tmp_path, monkeypatch):
    saved = client.Configuration.get_default_copy()
    path = tmp_path / "config"
    path.write_text(CONFIG)
    monkeypatch.setenv("KUBECONFIG", str(path))
    monkeypatch.setattr("kubernetes.config.kube_config.KUBE_CONFIG_DEFAULT_LOCATION", str(path))
    yield
    client.Configuration.set_default(saved)


def _install_fake(monkeypatch, fail: Exception | None = None) -> list[str]:
    calls: list[str] = []

    class Fake:
        def __init__(self, api=None) -> None:
            self.api = api

        def __getattr__(self, name: str):
            def method(*args, **kwargs):
                calls.append(name)
                if not name.startswith(("list_", "get_", "read_", "watch_", "connect_get_")):
                    raise AssertionError(f"write issued: {name}")
                if fail is not None and name == "list_namespaced_pod":
                    raise fail
                if name == "list_namespaced_custom_object":
                    raise ApiException(status=404)
                if name == "list_namespaced_secret":
                    return {"items": [SECRET]}
                if name == "list_namespaced_pod":
                    return {"items": [POD]}
                return {"items": []}

            return method

    for cls in ("CoreV1Api", "AppsV1Api", "PolicyV1Api", "NetworkingV1Api", "StorageV1Api", "CustomObjectsApi"):
        monkeypatch.setattr(f"kubernetes.client.{cls}", Fake)
    return calls


def test_read_only_client_allows_watch_and_refuses_replace() -> None:
    class Api:
        def watch_namespaced_pod(self) -> str:
            return "watch"

    reader = ReadOnly(Api())
    assert reader.watch_namespaced_pod() == "watch"
    with pytest.raises(RuntimeError, match="refusing"):
        reader.replace_namespaced_pod()


def test_namespace_read_lists_and_drops_secret_values(kubeconfig, monkeypatch) -> None:
    calls = _install_fake(monkeypatch)
    objects, _usage, _classes = collect("nodes", "b")
    assert client.Configuration.get_default_copy().host == "https://b.example:6443"
    assert calls
    assert all(name.startswith(("list_", "get_", "read_", "watch_", "connect_get_")) for name in calls)
    blob = json.dumps(objects)
    assert SECRET_VALUE not in blob
    assert base64.b64encode(SECRET_VALUE.encode()).decode() not in blob
    assert "jwt-secret" in blob
    assert "/secrets/jwt" in blob
    rendered = render_json(evaluate(objects, catalog(), live=True, usage=None, classes_known=True))
    assert SECRET_VALUE not in rendered


def test_missing_namespace_refused_auth_and_unreachable_api_exit_3(kubeconfig, monkeypatch, capsys) -> None:
    _install_fake(monkeypatch, fail=ApiException(status=404))
    with pytest.raises(LiveError, match="namespace not found: missing"):
        collect("missing", "b")
    assert main(["check", "--namespace", "missing", "--context", "b"]) == 3
    assert "namespace not found: missing" in capsys.readouterr().err

    _install_fake(monkeypatch, fail=ApiException(status=403))
    with pytest.raises(LiveError, match="access refused for namespace nodes"):
        collect("nodes", "b")
    assert main(["check", "--namespace", "nodes", "--context", "b"]) == 3
    assert "access refused" in capsys.readouterr().err

    _install_fake(monkeypatch, fail=ConnectionError("unreachable"))
    with pytest.raises(LiveError, match="cluster read failed for namespace nodes"):
        collect("nodes", "b")
    assert main(["check", "--namespace", "nodes", "--context", "b"]) == 3
    assert "cluster read failed" in capsys.readouterr().err


def test_unknown_context_exits_3(kubeconfig, capsys) -> None:
    with pytest.raises(LiveError, match="kubeconfig could not be read"):
        collect("nodes", "missing")
    assert main(["check", "--namespace", "nodes", "--context", "missing"]) == 3
    assert "kubeconfig could not be read" in capsys.readouterr().err


def test_namespace_report_matches_manifests(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("kubechain.cli.collect", lambda namespace, context: ([CONFIGMAP], None, True))
    assert main(["check", "--namespace", "nodes", "--context", "b", "--json"]) == 0
    live = json.loads(capsys.readouterr().out)
    (tmp_path / "settings.json").write_text(json.dumps(CONFIGMAP))
    offline_report = evaluate(load_dir(tmp_path), catalog(), live=False, usage=None, classes_known=True)
    offline = json.loads(render_json(offline_report))
    assert set(live) == set(offline) == {"namespace", "verdict", "objects", "rollouts", "notes", "findings"}
    assert live == offline


def test_manifests_and_a_live_read_are_separate(capsys) -> None:
    assert main(["check", "--manifests", "examples/ready", "--namespace", "nodes"]) == 3
    assert main(["check", "--manifests", "examples/ready", "--context", "b"]) == 3
    assert "either" in capsys.readouterr().err
