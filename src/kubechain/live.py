"""Read a namespace. The client is limited to get, list, and watch."""

from __future__ import annotations

import json


class LiveError(Exception):
    """The cluster could not be read."""


_ALLOWED = ("list_", "get_", "read_", "watch_", "connect_get_")


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
    core, apps, policy, networking, storage, custom, api = _clients(context)
    return _gather(namespace, core, apps, policy, networking, storage, custom, api)


def _clients(context: str | None):
    try:
        from kubernetes import client, config
    except ImportError as exc:
        raise LiveError("the kubernetes client is not installed") from exc
    try:
        config.load_kube_config(context=context)
    except Exception as exc:
        raise LiveError(f"kubeconfig could not be read: {exc}") from exc
    api = client.ApiClient()
    return (
        ReadOnly(client.CoreV1Api(api)),
        ReadOnly(client.AppsV1Api(api)),
        ReadOnly(client.PolicyV1Api(api)),
        ReadOnly(client.NetworkingV1Api(api)),
        ReadOnly(client.StorageV1Api(api)),
        ReadOnly(client.CustomObjectsApi(api)),
        api,
    )


def _gather(namespace, core, apps, policy, networking, storage, custom, api):
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
    except Exception as exc:
        _reraise(exc, namespace)

    classes_known = True
    try:
        objects.extend(_items(api, storage.list_storage_class()))
    except Exception as exc:
        if _fatal(exc):
            raise
        classes_known = False
    _optional(objects, custom, namespace)
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
            if pvc and isinstance(used, int) and used >= 0:
                rows.append({"claim": pvc, "used": used})
    return rows


def _items(api: object, listed: object) -> list[dict]:
    if isinstance(listed, dict):
        raw = listed
    else:
        raw = api.sanitize_for_serialization(listed) or {}
    return [strip_secret(item) for item in raw.get("items") or [] if isinstance(item, dict)]


def _optional(objects: list[dict], custom: ReadOnly, namespace: str) -> None:
    queries = (
        ("snapshot.storage.k8s.io", "v1", "volumesnapshots"),
        ("velero.io", "v1", "backups"),
        ("velero.io", "v1", "schedules"),
    )
    for group, version, plural in queries:
        try:
            listed = custom.list_namespaced_custom_object(group, version, namespace, plural)
        except Exception as exc:
            if _fatal(exc):
                raise
            continue
        if not isinstance(listed, dict):
            continue
        for item in listed.get("items") or []:
            if isinstance(item, dict):
                objects.append(strip_secret(item))


def _reraise(exc: Exception, namespace: str) -> None:
    if _fatal(exc):
        raise exc
    raise LiveError(_api_message(exc, namespace)) from exc


def _fatal(exc: Exception) -> bool:
    if isinstance(exc, (AssertionError, KeyboardInterrupt, SystemExit)):
        return True
    return isinstance(exc, RuntimeError) and str(exc).startswith("refusing")


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
    read = False
    for node in sorted(nodes):
        try:
            response = core.connect_get_node_proxy_with_path(node, "stats/summary")
            payload = response.data if hasattr(response, "data") else response
            if isinstance(payload, bytes):
                payload = payload.decode()
            summary = json.loads(payload) if isinstance(payload, str) else payload
            rows.extend(usage_from_summary(summary, namespace))
            read = True
        except Exception as exc:
            if _fatal(exc):
                raise
    return rows if read else None


def _api_message(exc: object, namespace: str) -> str:
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return f"access refused for namespace {namespace}"
    if status == 404:
        return f"namespace not found: {namespace}"
    return f"cluster read failed for namespace {namespace}"
