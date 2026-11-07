"""Read a namespace. The client is limited to get, list, and watch."""

from __future__ import annotations

import json


class LiveError(Exception):
    """The cluster could not be read."""


_ALLOWED = ("list_", "get_", "read_", "connect_get_")


class ReadOnly:
    """Refuse create, update, patch, delete, and deletecollection."""

    def __init__(self, api: object) -> None:
        self._api = api

    def __getattr__(self, name: str):
        if not name.startswith(_ALLOWED):
            raise RuntimeError(f"refusing {name}")
        return getattr(self._api, name)


def collect(namespace: str, context: str | None) -> tuple[list[dict], list[dict] | None, bool]:
    """Return objects, volume usage, and whether StorageClass was readable.

    Volume usage is None when kubelet stats could not be read.
    """
    try:
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException
    except ImportError as exc:
        raise LiveError("the kubernetes client is not installed") from exc
    try:
        config.load_kube_config(context=context)
    except Exception as exc:
        raise LiveError(f"kubeconfig could not be read: {exc}") from exc

    api = client.ApiClient()
    core = ReadOnly(client.CoreV1Api(api))
    apps = ReadOnly(client.AppsV1Api(api))
    policy = ReadOnly(client.PolicyV1Api(api))
    networking = ReadOnly(client.NetworkingV1Api(api))
    storage = ReadOnly(client.StorageV1Api(api))
    objects: list[dict] = []
    try:
        objects.extend(_items(api, core.list_namespaced_pod(namespace)))
        objects.extend(_items(api, core.list_namespaced_service(namespace)))
        objects.extend(_items(api, core.list_namespaced_secret(namespace)))
        objects.extend(_items(api, core.list_namespaced_config_map(namespace)))
        objects.extend(_items(api, core.list_namespaced_persistent_volume_claim(namespace)))
        objects.extend(_items(api, apps.list_namespaced_stateful_set(namespace)))
        objects.extend(_items(api, apps.list_namespaced_deployment(namespace)))
        objects.extend(_items(api, policy.list_namespaced_pod_disruption_budget(namespace)))
        objects.extend(_items(api, networking.list_namespaced_network_policy(namespace)))
        objects.extend(_items(api, networking.list_namespaced_ingress(namespace)))
    except ApiException as exc:
        raise LiveError(_api_message(exc, namespace)) from exc

    classes_known = True
    try:
        objects.extend(_items(api, storage.list_storage_class()))
    except ApiException:
        classes_known = False
    _optional(objects, api, namespace)
    usage = _usage(core, namespace, objects)
    return objects, usage, classes_known


def strip_secret(obj: dict) -> dict:
    if obj.get("kind") == "Secret":
        obj = dict(obj)
        obj.pop("data", None)
        obj.pop("stringData", None)
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("managedFields", None)
    return obj


def usage_from_summary(summary: dict, namespace: str) -> list[dict]:
    rows = []
    for pod in summary.get("pods") or []:
        ref = pod.get("podRef") or {}
        if ref.get("namespace") != namespace:
            continue
        for volume in pod.get("volume") or []:
            pvc = (volume.get("pvcRef") or {}).get("name")
            used = volume.get("usedBytes")
            capacity = volume.get("capacityBytes")
            if pvc and isinstance(used, int) and isinstance(capacity, int) and capacity > 0:
                rows.append({"claim": pvc, "used": used, "capacity": capacity})
    return rows


def _items(api: object, listed: object) -> list[dict]:
    raw = api.sanitize_for_serialization(listed) or {}
    return [strip_secret(item) for item in raw.get("items") or []]


def _optional(objects: list[dict], api: object, namespace: str) -> None:
    from kubernetes import client

    custom = ReadOnly(client.CustomObjectsApi(api))
    queries = (
        ("snapshot.storage.k8s.io", "v1", "volumesnapshots"),
        ("velero.io", "v1", "backups"),
        ("velero.io", "v1", "schedules"),
    )
    for group, version, plural in queries:
        try:
            listed = custom.list_namespaced_custom_object(group, version, namespace, plural)
        except Exception:
            continue
        if not isinstance(listed, dict):
            continue
        for item in listed.get("items") or []:
            if isinstance(item, dict):
                objects.append(strip_secret(item))


def _usage(core: ReadOnly, namespace: str, objects: list[dict]) -> list[dict] | None:
    nodes = set()
    for obj in objects:
        if obj.get("kind") == "Pod":
            node = (obj.get("spec") or {}).get("nodeName")
            if node:
                nodes.add(node)
    if not nodes:
        return None
    rows: list[dict] = []
    try:
        for node in sorted(nodes):
            response = core.connect_get_node_proxy_with_path(node, "stats/summary")
            payload = response.data if hasattr(response, "data") else response
            if isinstance(payload, bytes):
                payload = payload.decode()
            summary = json.loads(payload) if isinstance(payload, str) else payload
            rows.extend(usage_from_summary(summary, namespace))
    except Exception:
        return None
    return rows


def _api_message(exc: object, namespace: str) -> str:
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return f"access refused for namespace {namespace}"
    if status == 404:
        return f"namespace not found: {namespace}"
    return f"cluster read failed for namespace {namespace}"
