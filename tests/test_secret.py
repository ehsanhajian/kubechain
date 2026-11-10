from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir
from kubechain.render import render_json, render_markdown, render_text

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
SECRET_TEXT = "example-secret-value"
SECRET_BYTES = "ZXhhbXBsZS1zZWNyZXQtdmFsdWU="
KEYSTORE_TEXT = "not-a-key"


def _report(objects: list[dict], overlay: dict | None = None):
    return evaluate(objects, PROFILES, overlay, live=False, usage=None, classes_known=True)


def _ready() -> list[dict]:
    return deepcopy(load_dir(ROOT / "examples" / "ready"))


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _pod(objects: list[dict], name: str) -> dict:
    return _kind(objects, "StatefulSet", name)["spec"]["template"]["spec"]


def _drop_jwt_mount(spec: dict) -> None:
    spec["containers"][0]["volumeMounts"] = [
        mount for mount in spec["containers"][0].get("volumeMounts") or [] if mount.get("name") != "jwt"
    ]
    spec["volumes"] = [volume for volume in spec.get("volumes") or [] if volume.get("name") != "jwt"]


def _ids(report) -> set[tuple[str, str]]:
    return {(finding.id, finding.severity) for finding in report.findings}


def _rendered(report) -> str:
    return render_text(report) + render_json(report) + render_markdown(report)


def test_ready_prints_the_secret_name_and_mount_path() -> None:
    objects = _ready()
    secret = _kind(objects, "Secret", "jwt-secret")
    secret["data"] = {"jwtsecret": SECRET_BYTES}
    secret["stringData"] = {"jwtsecret": SECRET_TEXT}
    report = _report(objects)
    assert report.verdict == "ready"
    assert "jwt geth Secret jwt-secret mounted at /secrets/jwt" in report.notes
    assert "jwt lighthouse Secret jwt-secret mounted at /secrets/jwt" in report.notes
    rendered = _rendered(report)
    assert "jwt-secret" in rendered
    assert "/secrets/jwt" in rendered
    assert SECRET_TEXT not in rendered
    assert SECRET_BYTES not in rendered
    ids = {finding.id for finding in report.findings}
    assert "jwt-from-env" not in ids
    assert "jwt-from-configmap" not in ids
    assert "jwt-missing" not in ids
    assert "key-in-configmap" not in ids


def test_broken_covers_an_env_jwt_and_a_configmap_keystore() -> None:
    report = _report(load_dir(ROOT / "examples" / "broken"))
    pairs = _ids(report)
    assert ("jwt-from-env", "critical") in pairs
    assert ("key-in-configmap", "critical") in pairs
    rendered = _rendered(report)
    assert SECRET_TEXT not in rendered
    assert SECRET_BYTES not in rendered
    assert KEYSTORE_TEXT not in rendered
    env = next(finding for finding in report.findings if finding.id == "jwt-from-env")
    assert "JWT_SECRET" in env.reason
    assert env.reason == "The JWT is set from the environment variable JWT_SECRET."


def test_configmap_and_absent_jwt_are_critical() -> None:
    configmap = _ready()
    spec = _pod(configmap, "geth")
    for volume in spec["volumes"]:
        if volume.get("name") == "jwt":
            volume.pop("secret")
            volume["configMap"] = {"name": "jwt"}
    configmap.append({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "jwt", "namespace": "nodes"},
        "data": {"jwtsecret": SECRET_TEXT},
    })
    report = _report(configmap)
    finding = next(item for item in report.findings if item.id == "jwt-from-configmap" and item.target == "geth")
    assert finding.severity == "critical"
    assert finding.reason == "The JWT comes from ConfigMap jwt mounted at /secrets/jwt."
    assert SECRET_TEXT not in _rendered(report)
    assert report.verdict == "not-ready"

    absent = _ready()
    _drop_jwt_mount(_pod(absent, "geth"))
    _drop_jwt_mount(_pod(absent, "lighthouse"))
    report = _report(absent)
    assert report.verdict == "not-ready"
    missing = [item for item in report.findings if item.id == "jwt-missing"]
    assert {item.target for item in missing} >= {"geth", "lighthouse"}
    assert all(item.severity == "critical" for item in missing)
    assert not any(note.startswith("jwt ") for note in report.notes)


def test_a_missing_secret_names_the_secret_and_the_path() -> None:
    objects = _ready()
    objects.remove(_kind(objects, "Secret", "jwt-secret"))
    report = _report(objects)
    assert report.verdict == "not-ready"
    missing = [item for item in report.findings if item.id == "jwt-missing"]
    assert missing
    assert all(item.severity == "critical" for item in missing)
    assert all(item.reason == "Secret jwt-secret mounted at /secrets/jwt is missing." for item in missing)

    other_namespace = _ready()
    _kind(other_namespace, "Secret", "jwt-secret")["metadata"]["namespace"] = "other"
    report = _report(other_namespace)
    assert ("jwt-missing", "critical") in _ids(report)


def test_a_projected_secret_file_is_accepted() -> None:
    objects = _ready()
    for name in ("geth", "lighthouse"):
        for volume in _pod(objects, name)["volumes"]:
            if volume.get("name") == "jwt":
                volume.pop("secret")
                volume["projected"] = {"sources": [{"secret": {"name": "jwt-secret"}}]}
    report = _report(objects)
    assert report.verdict == "ready"
    assert "jwt geth Secret jwt-secret mounted at /secrets/jwt" in report.notes
    assert "jwt-missing" not in {finding.id for finding in report.findings}


def test_env_from_a_secret_or_a_configmap_is_critical() -> None:
    secret_env = _ready()
    spec = _pod(secret_env, "geth")
    _drop_jwt_mount(spec)
    spec["containers"][0]["envFrom"] = [{"secretRef": {"name": "jwt-secret"}}]
    _kind(secret_env, "Secret", "jwt-secret")["stringData"] = {"jwtsecret": SECRET_TEXT}
    report = _report(secret_env)
    finding = next(item for item in report.findings if item.id == "jwt-from-env" and item.target == "geth")
    assert finding.severity == "critical"
    assert finding.reason == "The JWT is set from Secret jwt-secret through the environment."
    assert SECRET_TEXT not in _rendered(report)

    key_ref = _ready()
    spec = _pod(key_ref, "geth")
    _drop_jwt_mount(spec)
    spec["containers"][0]["env"] = [{
        "name": "JWT_SECRET",
        "valueFrom": {"secretKeyRef": {"name": "jwt-secret", "key": "jwtsecret"}},
    }]
    report = _report(key_ref)
    finding = next(item for item in report.findings if item.id == "jwt-from-env" and item.target == "geth")
    assert finding.reason == "The JWT is set from the environment variable JWT_SECRET."

    config_ref = _ready()
    spec = _pod(config_ref, "geth")
    _drop_jwt_mount(spec)
    spec["containers"][0]["env"] = [{
        "name": "ENGINE_SECRET",
        "valueFrom": {"configMapKeyRef": {"name": "jwt-config", "key": "jwtsecret"}},
    }]
    config_ref.append({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "jwt-config", "namespace": "nodes"},
        "data": {"jwtsecret": SECRET_TEXT},
    })
    report = _report(config_ref)
    finding = next(item for item in report.findings if item.id == "jwt-from-configmap" and item.target == "geth")
    assert finding.reason == "The JWT comes from ConfigMap jwt-config."
    assert SECRET_TEXT not in _rendered(report)


def test_node_key_names_in_a_configmap_are_critical() -> None:
    def configmap(name: str, data: dict | None = None, binary: dict | None = None) -> dict:
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "nodes"},
        }
        if data is not None:
            body["data"] = data
        if binary is not None:
            body["binaryData"] = binary
        return body

    for obj in (
        configmap("keys", {"nodekey": KEYSTORE_TEXT}),
        configmap("keys", {"UTC--2024": KEYSTORE_TEXT}),
        configmap("keystore", {"note": "plain"}),
        configmap("keys", binary={"keystore": KEYSTORE_TEXT}),
    ):
        report = _report([obj])
        assert ("key-in-configmap", "critical") in _ids(report)
        assert KEYSTORE_TEXT not in _rendered(report)

    clean = _report([configmap("config", {"http": "8545"})])
    assert "key-in-configmap" not in {finding.id for finding in clean.findings}
    assert clean.verdict == "ready"


def test_a_profile_without_jwt_skips_the_mount() -> None:
    objects = _ready()
    _drop_jwt_mount(_pod(objects, "geth"))
    report = _report(objects, {"comm": "geth", "jwt": False})
    missing = [item for item in report.findings if item.id == "jwt-missing"]
    assert missing == []
    assert "jwt lighthouse Secret jwt-secret mounted at /secrets/jwt" in report.notes
    assert "jwt geth Secret jwt-secret mounted at /secrets/jwt" not in report.notes
    assert report.verdict == "ready"
