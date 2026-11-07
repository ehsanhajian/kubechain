"""Read a directory of YAML and JSON manifests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


class LoadError(Exception):
    """The manifest directory cannot be read."""


_SUFFIXES = {".yaml", ".yml", ".json"}


def load_dir(path: Path) -> list[dict]:
    if not path.is_dir():
        raise LoadError(f"manifest directory not found: {path}")
    files = sorted(item for item in path.iterdir() if item.is_file() and not item.name.startswith("."))
    if not files:
        raise LoadError("empty manifest directory")
    objects: list[dict] = []
    for file in files:
        if file.suffix.lower() not in _SUFFIXES:
            raise LoadError(f"not YAML or JSON: {file.name}")
        objects.extend(_documents(file))
    if not objects:
        raise LoadError("empty manifest directory")
    return objects


def _documents(file: Path) -> list[dict]:
    text = file.read_text()
    try:
        if file.suffix.lower() == ".json":
            parsed = [json.loads(text)]
        else:
            parsed = list(yaml.safe_load_all(text))
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise LoadError(f"cannot read {file.name}") from exc
    documents = []
    for document in parsed:
        if document is None:
            continue
        if not isinstance(document, dict) or "kind" not in document:
            raise LoadError(f"not a Kubernetes object: {file.name}")
        documents.append(document)
    return documents
