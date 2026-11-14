import json
import os
import subprocess
import sys
from pathlib import Path

from kubechain import __version__

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "kubechain", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or ROOT,
    )


def test_version() -> None:
    result = _run("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"kubechain {__version__}"
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in pyproject


def test_ready_example() -> None:
    result = _run("check", "--manifests", "examples/ready")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verdict ready" in result.stdout
    assert "warning " not in result.stdout
    assert "critical " not in result.stdout
    assert "note restarts were not read" in result.stdout
    assert "note storage performance was not read" in result.stdout
    assert "rollout geth RollingUpdate partition 0" in result.stdout


def test_ready_profile_and_brief_json() -> None:
    result = _run("check", "--manifests", "examples/ready", "--profile", "examples/geth.json", "--brief", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["verdict"] == "ready"
    assert document["namespace"] == "nodes"
    assert "StatefulSet/geth" in document["objects"]
    assert result.stdout.strip().startswith("{")


def test_broken_example_hides_secret_bytes() -> None:
    result = _run("check", "--manifests", "examples/broken")
    assert result.returncode == 2, result.stdout + result.stderr
    text = result.stdout + result.stderr
    for finding in (
        "deployment-holds-chain-data",
        "jwt-from-env",
        "key-in-configmap",
        "engine-api-exposed",
        "chain-data-ephemeral",
        "statefulset-no-claim",
        "statefulset-zero-replicas",
        "no-backup-signal",
    ):
        assert finding in text
    assert "example-secret-value" not in text
    assert "ZXhhbXBsZS1zZWNyZXQtdmFsdWU=" not in text


def test_oom_example() -> None:
    result = _run("check", "--manifests", "examples/oom")
    assert result.returncode == 2, result.stdout
    assert "pod-oom" in result.stdout
    assert "pod-crash-loop" in result.stdout
    assert "restarts were not read" not in result.stdout


def test_report_writes_markdown(tmp_path: Path) -> None:
    output = tmp_path / "out.md"
    result = _run("report", "--manifests", "examples/ready", "--output", str(output), "--json")
    assert result.returncode == 0, result.stderr
    body = output.read_text()
    assert body.startswith("verdict ready\n")
    assert json.loads(result.stdout)["verdict"] == "ready"


def test_report_refuses_a_directory(tmp_path: Path) -> None:
    result = _run("report", "--manifests", "examples/ready", "--output", str(tmp_path))
    assert result.returncode == 3
    assert "directory" in result.stderr


def test_empty_and_unknown_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run("check", "--manifests", str(empty))
    assert result.returncode == 3
    assert "empty" in result.stderr
    bad = tmp_path / "notes"
    bad.mkdir()
    (bad / "notes.txt").write_text("hello")
    result = _run("check", "--manifests", str(bad))
    assert result.returncode == 3
    assert "not YAML or JSON" in result.stderr


def test_usage_and_profile_errors(tmp_path: Path) -> None:
    result = _run("check")
    assert result.returncode == 3
    result = _run("check", "--manifests", "examples/ready", "--namespace", "nodes")
    assert result.returncode == 3
    profile = tmp_path / "profile.json"
    profile.write_text('{"comm": "geth", "tier": "huge"}')
    result = _run("check", "--manifests", "examples/ready", "--profile", str(profile))
    assert result.returncode == 3
    assert "unknown profile field" in result.stderr


def test_brief_line() -> None:
    result = _run("check", "--manifests", "examples/ready", "--brief")
    assert result.returncode == 0
    assert result.stdout == "namespace nodes verdict ready\n"


_EXIT = {"ready": 0, "review": 1, "not-ready": 2}


def _assert_output_formats(manifests: str) -> None:
    default = _run("check", "--manifests", manifests)
    brief = _run("check", "--manifests", manifests, "--brief")
    as_json = _run("check", "--manifests", manifests, "--json")
    both = _run("check", "--manifests", manifests, "--brief", "--json")
    document = json.loads(as_json.stdout)
    verdict = document["verdict"]
    code = _EXIT[verdict]
    assert default.returncode == brief.returncode == as_json.returncode == both.returncode == code
    assert default.stdout.startswith("verdict ")
    assert not default.stdout.lstrip().startswith("{")
    assert brief.stdout == f"namespace {document['namespace']} verdict {verdict}\n"
    assert {"verdict", "objects", "findings"} <= set(document)
    assert isinstance(document["objects"], list)
    assert isinstance(document["findings"], list)
    assert json.loads(both.stdout) == document
    assert not both.stdout.startswith("namespace ")


def test_json_and_brief_keep_the_exit_code(tmp_path: Path) -> None:
    _assert_output_formats("examples/ready")
    _assert_output_formats("examples/broken")
    (tmp_path / "proxy.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: proxy
  namespace: nodes
spec:
  replicas: 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
  template:
    spec:
      containers:
        - name: proxy
          image: nginx:1.27.0
"""
    )
    _assert_output_formats(str(tmp_path))
    seen = set()
    for manifests in ("examples/ready", "examples/broken", str(tmp_path)):
        document = json.loads(_run("check", "--manifests", manifests, "--json").stdout)
        seen.add(document["verdict"])
    assert seen == {"ready", "review", "not-ready"}


def test_yaml_and_json_are_one_report(tmp_path: Path) -> None:
    (tmp_path / "stack.yaml").write_text(
        """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: nodes
---
apiVersion: v1
kind: Service
metadata:
  name: rpc
  namespace: nodes
spec:
  ports:
    - port: 8545
---
apiVersion: v1
kind: Secret
metadata:
  name: jwt-secret
  namespace: nodes
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: proxy
  namespace: nodes
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: proxy
          image: nginx:1.27.0
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: unused
  namespace: nodes
spec:
  replicas: 0
  serviceName: missing
  selector:
    matchLabels:
      app: unused
  template:
    metadata:
      labels:
        app: unused
    spec:
      containers:
        - name: geth
          image: ethereum/client-go:v1.15.11
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: unused
  namespace: nodes
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: unused
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: unused
  namespace: nodes
spec:
  podSelector:
    matchLabels:
      app: unused
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fast
provisioner: kubernetes.io/no-provisioner
"""
    )
    (tmp_path / "claim.json").write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "data-unused-0", "namespace": "nodes"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "10Gi"}},
                },
            }
        )
    )
    result = _run("check", "--manifests", str(tmp_path), "--json")
    assert result.returncode in {0, 1, 2}, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["objects"] == [
        "ConfigMap/settings",
        "Deployment/proxy",
        "NetworkPolicy/unused",
        "PersistentVolumeClaim/data-unused-0",
        "PodDisruptionBudget/unused",
        "Secret/jwt-secret",
        "Service/rpc",
        "StatefulSet/unused",
        "StorageClass/fast",
    ]
    assert "critical statefulset-zero-replicas unused" not in result.stdout
    assert any(finding["id"] == "statefulset-zero-replicas" for finding in document["findings"])


def test_review_exits_1(tmp_path: Path) -> None:
    (tmp_path / "proxy.yaml").write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: proxy
  namespace: nodes
spec:
  replicas: 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
  template:
    spec:
      containers:
        - name: proxy
          image: nginx:1.27.0
"""
    )
    result = _run("check", "--manifests", str(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "warning deployment-max-unavailable proxy" in result.stdout
    assert "critical " not in result.stdout


def test_manifest_read_does_not_shell_out() -> None:
    source = "\n".join(path.read_text() for path in (ROOT / "src" / "kubechain").glob("*.py"))
    assert "kubectl" not in source
    assert "subprocess" not in source
