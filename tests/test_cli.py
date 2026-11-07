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
    assert body.startswith("# KubeChain\n")
    assert "verdict ready" in body
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
