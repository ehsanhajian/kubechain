from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _about() -> str:
    paragraphs = []
    current: list[str] = []
    for line in (ROOT / "README.md").read_text().splitlines():
        if line.startswith("#"):
            continue
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current))
                current = []
            if len(paragraphs) == 2:
                break
            continue
        current.append(line.replace("**", ""))
    text = " ".join(paragraphs)
    return " ".join(text.split())


def test_the_readme_opens_with_the_question_and_the_read_only_rule() -> None:
    about = _about()
    assert about.startswith("KubeChain answers one question: is my Kubernetes blockchain deployment production ready?")
    assert "A live check uses get, list, and watch." in about
    assert "The cluster is left as it was found." in about
    assert len(about) <= 350


def test_ci_runs_the_ready_and_broken_fixtures() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "kubechain --version" in workflow
    assert "kubechain check --manifests examples/ready" in workflow
    assert "kubechain check --manifests examples/broken" in workflow
    assert 'test "$status" -eq 2' in workflow


def test_the_page_says_the_tool_only_reads() -> None:
    page = (ROOT / "docs" / "index.html").read_text()
    assert "KubeChain only reads." in page
    assert "get, list, and watch" in page
    assert "does not apply a manifest" in page
    assert "does not create a snapshot" in page
    assert "pip install kubechain" in page
    assert "kubechain --version" in page
    assert "https://ehsanhajian.github.io/kubechain/" in page
