import json
import os
import subprocess
import sys
from pathlib import Path

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


def _sections(body: str) -> list[tuple[str, str]]:
    preamble, *parts = body.split("\n## ")
    assert preamble.startswith("verdict ")
    sections = []
    for part in parts:
        heading, _, rest = part.partition("\n")
        sections.append((heading, rest))
    return sections


def test_report_writes_the_default_file_and_matches_check(tmp_path: Path) -> None:
    manifests = str(ROOT / "examples" / "broken")
    checked = _run("check", "--manifests", manifests, "--json")
    reported = _run("report", "--manifests", manifests, cwd=tmp_path)
    assert checked.returncode == reported.returncode == 2
    assert reported.stdout == ""
    body = (tmp_path / "kubechain-report.md").read_text()
    document = json.loads(checked.stdout)
    assert body.startswith(f"verdict {document['verdict']}\n")
    sections = _sections(body)
    assert [heading for heading, _rest in sections] == [item["id"] for item in document["findings"]]
    assert len(sections) == len(document["findings"])
    for finding, (heading, rest) in zip(document["findings"], sections, strict=True):
        assert heading == finding["id"]
        assert f"severity {finding['severity']}" in rest
        assert f"object {finding['object']}" in rest
        assert finding["reason"] in rest
        assert finding["action"] in rest


def test_output_chooses_the_file_and_an_existing_file_is_overwritten(tmp_path: Path) -> None:
    chosen = tmp_path / "custom.md"
    chosen.write_text("stale\n")
    result = _run("report", "--manifests", str(ROOT / "examples" / "ready"), "--output", str(chosen))
    assert result.returncode == 0
    assert result.stdout == ""
    body = chosen.read_text()
    assert body.startswith("verdict ready\n")
    assert "stale" not in body
    assert "No findings." in body
    assert not (tmp_path / "kubechain-report.md").exists()


def test_a_directory_is_refused(tmp_path: Path) -> None:
    output = tmp_path / "reports"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep\n")
    result = _run("report", "--manifests", str(ROOT / "examples" / "ready"), "--output", str(output))
    assert result.returncode == 3
    assert "directory" in result.stderr
    assert marker.read_text() == "keep\n"
    assert list(output.iterdir()) == [marker]


def test_json_prints_the_document_and_still_writes_markdown(tmp_path: Path) -> None:
    manifests = str(ROOT / "examples" / "broken")
    output = tmp_path / "out.md"
    checked = _run("check", "--manifests", manifests, "--json")
    reported = _run("report", "--manifests", manifests, "--output", str(output), "--json")
    assert reported.returncode == checked.returncode == 2
    assert json.loads(reported.stdout) == json.loads(checked.stdout)
    assert output.read_text().startswith("verdict not-ready\n")
    assert not output.read_text().lstrip().startswith("{")
