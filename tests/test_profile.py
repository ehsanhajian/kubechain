import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog, load_profile, resolve

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()
SAMPLE = ROOT / "examples" / "geth.json"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "kubechain", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )


def _kind(objects: list[dict], kind: str, name: str) -> dict:
    for obj in objects:
        if obj.get("kind") == kind and (obj.get("metadata") or {}).get("name") == name:
            return obj
    raise AssertionError(f"{kind}/{name}")


def _pod(objects: list[dict], name: str) -> dict:
    return _kind(objects, "StatefulSet", name)["spec"]["template"]["spec"]


def _container(objects: list[dict], name: str) -> dict:
    return _pod(objects, name)["containers"][0]


def _write_manifests(directory: Path, objects: list[dict]) -> None:
    directory.mkdir()
    for index, obj in enumerate(objects):
        (directory / f"{index:02d}.json").write_text(json.dumps(obj))


def _shifted(directory: Path) -> None:
    from kubechain.load import load_dir

    objects = deepcopy(load_dir(ROOT / "examples" / "ready"))
    geth = _pod(objects, "geth")
    geth["terminationGracePeriodSeconds"] = 400
    container = geth["containers"][0]
    container["args"] = ["--authrpc.addr=0.0.0.0", "--authrpc.port=8552"]
    container["volumeMounts"] = [mount for mount in container["volumeMounts"] if mount["name"] != "jwt"]
    for mount in container["volumeMounts"]:
        if mount["name"] == "data":
            mount["name"] = "ancient"
    resources = container["resources"]
    resources["requests"] = {"cpu": "3", "memory": "10Gi"}
    resources["limits"] = {"cpu": "3", "memory": "10Gi"}
    geth["volumes"] = [volume for volume in geth["volumes"] if volume["name"] != "jwt"]
    claim = _kind(objects, "StatefulSet", "geth")["spec"]["volumeClaimTemplates"][0]
    claim["metadata"]["name"] = "ancient"
    for port in _kind(objects, "Service", "geth")["spec"]["ports"]:
        if port["port"] == 30303:
            port["port"] = 30304
        elif port["port"] == 8551:
            port["port"] = 8552
    for rule in _kind(objects, "NetworkPolicy", "geth")["spec"]["ingress"]:
        for entry in rule["ports"]:
            if entry["port"] == 30303:
                entry["port"] = 30304
            elif entry["port"] == 8551:
                entry["port"] = 8552
    lighthouse = _container(objects, "lighthouse")
    lighthouse["args"] = ["--execution-endpoint=http://geth:8552", "--execution-jwt=/secrets/jwt"]
    _write_manifests(directory, objects)


def _profile(directory: Path) -> Path:
    path = directory / "geth.json"
    path.write_text(json.dumps({
        "comm": "geth",
        "p2p_ports": [30304],
        "rpc_ports": [18545],
        "engine_port": 8552,
        "jwt": False,
        "data_volume": "ancient",
        "cpu": "4",
        "memory": "16Gi",
        "storage": "300Gi",
        "grace_seconds": 600,
    }))
    return path


def _ids(text: str, finding_id: str) -> list[str]:
    targets = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == finding_id:
            targets.append(parts[2])
    return targets


def test_the_sample_matches_the_readme_and_keeps_a_ready_stack() -> None:
    sample = SAMPLE.read_text().strip()
    assert sample in (ROOT / "README.md").read_text()
    loaded = load_profile(SAMPLE)
    assert set(loaded) == {
        "comm", "p2p_ports", "rpc_ports", "engine_port", "jwt",
        "data_volume", "cpu", "memory", "storage", "grace_seconds",
    }
    result = _run("check", "--manifests", "examples/ready", "--profile", "examples/geth.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verdict ready" in result.stdout


def test_a_matching_name_replaces_ports_jwt_volume_floor_and_grace(tmp_path: Path) -> None:
    manifests = tmp_path / "manifests"
    _shifted(manifests)
    profile = _profile(tmp_path)
    replaced = _run("check", "--manifests", str(manifests), "--profile", str(profile))
    text = replaced.stdout
    assert replaced.returncode == 1, text + replaced.stderr
    assert _ids(text, "peer-port-missing") == []
    assert _ids(text, "engine-endpoint-missing") == []
    assert _ids(text, "statefulset-no-claim") == []
    assert _ids(text, "jwt-missing") == []
    assert _ids(text, "jwt-secret-mismatch") == []
    assert _ids(text, "grace-below-floor") == ["geth"]
    assert _ids(text, "storage-below-floor") == ["geth"]
    assert set(_ids(text, "resources-below-floor")) == {"geth/cpu", "geth/memory"}
    assert "600" in text
    assert "300Gi" in text
    assert "floor 4" in text
    assert "floor 16Gi" in text

    builtin = _run("check", "--manifests", str(manifests))
    builtin_text = builtin.stdout
    assert _ids(builtin_text, "peer-port-missing") == ["geth"]
    assert _ids(builtin_text, "engine-endpoint-missing") == ["lighthouse"]
    assert _ids(builtin_text, "statefulset-no-claim") == ["geth"]
    assert "geth" in _ids(builtin_text, "jwt-missing")
    assert _ids(builtin_text, "grace-below-floor") == []
    assert _ids(builtin_text, "storage-below-floor") == []
    assert _ids(builtin_text, "resources-below-floor") == []

    overlay = load_profile(profile)
    assert resolve({"name": "geth", "image": "library/pause:3.10"}, PROFILES, overlay).cpu == "4"
    assert resolve({"name": "op-geth", "image": "library/pause:3.10"}, PROFILES, overlay).cpu == "2"
    assert resolve({"name": "lighthouse", "image": "library/pause:3.10"}, PROFILES, overlay).grace_seconds == 120


def test_an_unknown_field_is_exit_3(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text('{"comm": "geth", "tier": "huge"}')
    result = _run("check", "--manifests", "examples/ready", "--profile", str(profile))
    assert result.returncode == 3
    assert "unknown profile field" in result.stderr


def test_an_unrecognized_workload_keeps_the_generic_checks(tmp_path: Path) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests, [{
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "redis", "namespace": "nodes"},
        "spec": {
            "replicas": 2,
            "template": {"spec": {"containers": [{"name": "redis", "image": "redis:latest"}]}},
        },
    }])
    result = _run("check", "--manifests", str(manifests), "--profile", str(SAMPLE))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _ids(result.stdout, "image-moving-tag") == ["redis"]
    assert _ids(result.stdout, "resources-no-requests") == []
    assert _ids(result.stdout, "jwt-missing") == []
    assert _ids(result.stdout, "probes-missing") == []
    assert resolve({"name": "redis", "image": "redis:7.2.4"}, PROFILES, load_profile(SAMPLE)) is None
