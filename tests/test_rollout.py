from copy import deepcopy
from pathlib import Path

from kubechain.catalog import catalog
from kubechain.check import evaluate
from kubechain.load import load_dir
from kubechain.render import render_json, render_markdown, render_text

ROOT = Path(__file__).resolve().parents[1]
PROFILES = catalog()


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


def test_execution_floor_is_300_unless_the_profile_says_otherwise() -> None:
    for profile in PROFILES.values():
        if profile.role == "execution":
            assert profile.grace_seconds == 300


def test_the_report_names_the_strategy_and_the_partition() -> None:
    report = _report(load_dir(ROOT / "examples" / "ready"))
    assert [(item.target, item.strategy, item.partition) for item in report.rollouts] == [
        ("geth", "RollingUpdate", 0),
        ("lighthouse", "RollingUpdate", 0),
    ]
    text = render_text(report)
    rendered = text + render_json(report) + render_markdown(report)
    assert "rollout geth RollingUpdate partition 0" in text
    assert "RollingUpdate" in rendered
    assert "partition 0" in rendered

    omitted = _ready()
    del _kind(omitted, "StatefulSet", "geth")["spec"]["updateStrategy"]["rollingUpdate"]["partition"]
    report = _report(omitted)
    assert report.rollouts[0].partition == 0
    assert "rollout-not-started" not in {finding.id for finding in report.findings}
    assert report.verdict == "ready"


def test_a_partition_equal_to_replicas_has_not_started() -> None:
    objects = _ready()
    _kind(objects, "StatefulSet", "geth")["spec"]["updateStrategy"]["rollingUpdate"]["partition"] = 2
    report = _report(objects)
    assert report.verdict == "ready"
    finding = next(item for item in report.findings if item.id == "rollout-not-started")
    assert finding.severity == "info"
    assert finding.target == "geth"
    assert report.rollouts[0].partition == 2


def test_on_delete_is_info_and_names_no_partition() -> None:
    objects = _ready()
    _kind(objects, "StatefulSet", "geth")["spec"]["updateStrategy"] = {"type": "OnDelete"}
    report = _report(objects)
    assert report.verdict == "ready"
    finding = next(item for item in report.findings if item.id == "statefulset-on-delete")
    assert finding.severity == "info"
    assert finding.action == "Delete the pod to roll it."
    assert report.rollouts[0].strategy == "OnDelete"
    assert report.rollouts[0].partition is None
    assert "rollout geth OnDelete\n" in render_text(report)
    assert "rollout-not-started" not in {item.id for item in report.findings}


def test_grace_below_the_floor_is_a_warning_and_a_missing_prestop_is_info() -> None:
    low = _ready()
    _pod(low, "geth")["terminationGracePeriodSeconds"] = 299
    report = _report(low)
    assert report.verdict == "review"
    finding = next(item for item in report.findings if item.id == "grace-below-floor")
    assert finding.severity == "warning"
    assert finding.target == "geth"
    assert "299" in finding.reason
    assert "300" in finding.reason
    assert "KubeChain floor" in finding.reason

    exact = _ready()
    _pod(exact, "geth")["terminationGracePeriodSeconds"] = "300"
    assert "grace-below-floor" not in {item.id for item in _report(exact).findings}

    consensus = _ready()
    _pod(consensus, "lighthouse")["terminationGracePeriodSeconds"] = 119
    finding = next(item for item in _report(consensus).findings if item.id == "grace-below-floor")
    assert finding.target == "lighthouse"
    assert "120" in finding.reason

    cited = _ready()
    report = _report(cited, {"comm": "geth", "grace_seconds": 600})
    finding = next(item for item in report.findings if item.id == "grace-below-floor" and item.target == "geth")
    assert "600" in finding.reason

    bare = _ready()
    for name in ("geth", "lighthouse"):
        container = _pod(bare, name)["containers"][0]
        container.pop("lifecycle")
    report = _report(bare)
    assert report.verdict == "ready"
    findings = [item for item in report.findings if item.id == "prestop-missing"]
    assert {item.target for item in findings} == {"geth", "lighthouse"}
    assert all(item.severity == "info" for item in findings)
    assert all(item.action == "The grace period is the check that matters." for item in findings)


def test_a_deployment_proxy_is_outside_the_grace_floor() -> None:
    proxy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "proxy", "namespace": "nodes"},
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": 1,
                    "containers": [{"name": "proxy", "image": "nginx:1.27.0"}],
                },
            },
        },
    }
    report = _report([proxy])
    assert "grace-below-floor" not in {item.id for item in report.findings}
    assert report.rollouts == []
    assert report.verdict == "ready"
