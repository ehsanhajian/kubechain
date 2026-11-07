"""Built-in client profiles and a local file that overrides one of them."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path


class ProfileError(Exception):
    """The profile file cannot be used."""


ALLOWED = {
    "comm",
    "role",
    "images",
    "p2p_ports",
    "rpc_ports",
    "engine_port",
    "jwt",
    "data_volume",
    "cpu",
    "memory",
    "storage",
    "grace_seconds",
    "exempt_probes",
    "source",
}


@dataclass(frozen=True)
class Profile:
    comm: str
    role: str
    images: tuple[str, ...]
    p2p_ports: tuple[int, ...]
    rpc_ports: tuple[int, ...]
    engine_port: int | None
    jwt: bool
    data_volume: str
    cpu: str
    memory: str
    storage: str
    grace_seconds: int
    exempt_probes: bool
    source: str


def catalog() -> dict[str, Profile]:
    raw = json.loads(files("kubechain").joinpath("clients.json").read_text())
    return {name: _profile(body) for name, body in raw.items()}


def load_profile(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ProfileError(f"profile is not JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ProfileError("profile must be a JSON object")
    extra = sorted(set(data) - ALLOWED)
    if extra:
        raise ProfileError(f"unknown profile field: {extra[0]}")
    if "comm" not in data:
        raise ProfileError("profile needs comm")
    return data


def resolve(container: dict, profiles: dict[str, Profile], overlay: dict | None) -> Profile | None:
    name = str(container.get("name") or "")
    image = str(container.get("image") or "")
    chosen = profiles.get(name) or _by_image(image, profiles)
    if overlay and overlay.get("comm") == name:
        return _merge(chosen, overlay)
    return chosen


def _by_image(image: str, profiles: dict[str, Profile]) -> Profile | None:
    path = image.split("@", 1)[0]
    slash = path.rfind("/")
    colon = path.rfind(":")
    if colon > slash:
        path = path[:colon]
    segments = path.split("/")
    best: Profile | None = None
    best_len = -1
    for profile in profiles.values():
        for hint in profile.images:
            if hint in segments and len(hint) > best_len:
                best = profile
                best_len = len(hint)
    return best


def _merge(base: Profile | None, overlay: dict) -> Profile:
    if base is None:
        base = Profile(
            comm=str(overlay["comm"]),
            role=str(overlay.get("role") or "unknown"),
            images=(),
            p2p_ports=(),
            rpc_ports=(),
            engine_port=None,
            jwt=bool(overlay.get("jwt", False)),
            data_volume=str(overlay.get("data_volume") or ""),
            cpu=str(overlay.get("cpu") or ""),
            memory=str(overlay.get("memory") or ""),
            storage=str(overlay.get("storage") or ""),
            grace_seconds=int(overlay.get("grace_seconds") or 30),
            exempt_probes=bool(overlay.get("exempt_probes", False)),
            source="Local profile.",
        )
    values = {}
    for key in ("role", "data_volume", "cpu", "memory", "storage", "source"):
        if key in overlay:
            values[key] = overlay[key]
    if "engine_port" in overlay:
        values["engine_port"] = overlay["engine_port"]
    if "jwt" in overlay:
        values["jwt"] = bool(overlay["jwt"])
    if "exempt_probes" in overlay:
        values["exempt_probes"] = bool(overlay["exempt_probes"])
    if "grace_seconds" in overlay:
        values["grace_seconds"] = int(overlay["grace_seconds"])
    if "p2p_ports" in overlay:
        values["p2p_ports"] = tuple(int(port) for port in overlay["p2p_ports"])
    if "rpc_ports" in overlay:
        values["rpc_ports"] = tuple(int(port) for port in overlay["rpc_ports"])
    if "images" in overlay:
        values["images"] = tuple(overlay["images"])
    return replace(base, **values)


def _profile(body: dict) -> Profile:
    return Profile(
        comm=body["comm"],
        role=body["role"],
        images=tuple(body.get("images") or ()),
        p2p_ports=tuple(body.get("p2p_ports") or ()),
        rpc_ports=tuple(body.get("rpc_ports") or ()),
        engine_port=body.get("engine_port"),
        jwt=bool(body.get("jwt")),
        data_volume=body.get("data_volume") or "",
        cpu=body.get("cpu") or "",
        memory=body.get("memory") or "",
        storage=body.get("storage") or "",
        grace_seconds=int(body.get("grace_seconds") or 30),
        exempt_probes=bool(body.get("exempt_probes")),
        source=body.get("source") or "KubeChain floor.",
    )
