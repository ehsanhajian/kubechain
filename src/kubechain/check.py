"""Production checks for a chain workload."""

from __future__ import annotations

from kubechain.catalog import Profile, resolve
from kubechain.model import Finding, Report, Rollout

DATA_NAMES = {"data", "chain", "geth", "nethermind"}
CHAIN_VOLUME_SOURCES = ("emptyDir", "hostPath", "persistentVolumeClaim")
MOVING_TAGS = {"latest", "main", "master", "develop", "dev", "stable", "edge", "nightly", "rolling"}
_MEM = (
    ("Ki", 1024),
    ("Mi", 1024**2),
    ("Gi", 1024**3),
    ("Ti", 1024**4),
    ("K", 1000),
    ("M", 1000**2),
    ("G", 1000**3),
    ("T", 1000**4),
)


def evaluate(
    objects: list[dict],
    profiles: dict[str, Profile],
    overlay: dict | None = None,
    *,
    live: bool = False,
    usage: list[dict] | None = None,
    classes_known: bool = True,
) -> Report:
    findings: list[Finding] = []
    notes: list[str] = []
    workloads = [_workload(obj, profiles, overlay) for obj in objects if obj.get("kind") in {"StatefulSet", "Deployment"}]
    chain = [item for item in workloads if item["chain_data"] and item["kind"] == "StatefulSet"]
    findings.extend(_statefulsets(chain, objects))
    findings.extend(_deployments(workloads))
    findings.extend(_services(chain, objects))
    secret_findings, secret_notes = _secrets(workloads, objects)
    findings.extend(secret_findings)
    notes.extend(secret_notes)
    findings.extend(_storage(chain, objects, live=live, classes_known=classes_known))
    findings.extend(_images(workloads))
    findings.extend(_resources(workloads))
    findings.extend(_probes(workloads))
    pod_findings, pods_seen = _restarts(objects)
    findings.extend(pod_findings)
    if not pods_seen:
        notes.append("restarts were not read")
    if usage is None:
        notes.append("storage performance was not read")
    else:
        findings.extend(_usage(usage))
    findings.extend(_policies(chain, objects))
    findings.extend(_pair(workloads))
    findings.extend(_budgets(chain, objects))
    findings.extend(_grace(chain))
    findings.extend(_backup(chain, objects))
    findings.extend(_placement(chain))
    rollouts = _rollouts(chain)
    return Report(
        namespace=_namespace(objects),
        objects=sorted(_object_names(objects)),
        findings=findings,
        notes=notes,
        rollouts=rollouts,
    )


def _workload(obj: dict, profiles: dict[str, Profile], overlay: dict | None) -> dict:
    spec = obj.get("spec") or {}
    pod = ((spec.get("template") or {}).get("spec") or {})
    containers = list(pod.get("containers") or [])
    resolved = [(container, resolve(container, profiles, overlay)) for container in containers]
    known = [(container, profile) for container, profile in resolved if profile is not None]
    recognized = list(known)
    names = {volume.get("name") for volume in pod.get("volumes") or []}
    names.update(claim.get("metadata", {}).get("name") for claim in spec.get("volumeClaimTemplates") or [])
    data_names = set(DATA_NAMES)
    for _, profile in known:
        if profile.data_volume:
            data_names.add(profile.data_volume)
    holds_data = bool(names & data_names) or any(profile.role == "execution" for _, profile in known)
    kind = obj.get("kind")
    chain_data = (holds_data and kind == "StatefulSet") or (
        kind == "Deployment" and _holds_chain_volume(pod, spec, data_names)
    )
    if kind == "Deployment" and not chain_data:
        resolved = [(container, None) for container, _profile in resolved]
        known = []
    return {
        "kind": kind,
        "name": (obj.get("metadata") or {}).get("name") or "",
        "namespace": (obj.get("metadata") or {}).get("namespace") or "default",
        "obj": obj,
        "pod": pod,
        "containers": resolved,
        "known": known,
        "recognized": recognized,
        "chain_data": chain_data,
        "data_names": data_names,
    }


def _holds_chain_volume(pod: dict, spec: dict, data_names: set[str]) -> bool:
    for volume in pod.get("volumes") or []:
        if volume.get("name") in data_names and any(source in volume for source in CHAIN_VOLUME_SOURCES):
            return True
    for claim in spec.get("volumeClaimTemplates") or []:
        if (claim.get("metadata") or {}).get("name") in data_names:
            return True
    return False


def _statefulsets(chain: list[dict], objects: list[dict]) -> list[Finding]:
    findings = []
    for item in chain:
        spec = item["obj"].get("spec") or {}
        name = item["name"]
        sources = _volume_sources(item["pod"], item["data_names"])
        if "emptyDir" in sources or "hostPath" in sources:
            findings.append(_finding(
                "chain-data-ephemeral", "critical", name,
                "Chain data is on emptyDir or hostPath.",
                "Move the data volume to a PersistentVolumeClaim template.",
            ))
        claims = [
            claim for claim in spec.get("volumeClaimTemplates") or []
            if (claim.get("metadata") or {}).get("name") in item["data_names"]
        ]
        if not claims:
            findings.append(_finding(
                "statefulset-no-claim", "critical", name,
                "The StatefulSet has no volumeClaimTemplates entry for the data volume.",
                "Add a claim template for the data volume.",
            ))
        service_name = spec.get("serviceName") or ""
        service = _find(objects, "Service", service_name, item["namespace"]) if service_name else None
        if not service_name or not service or not _headless(service):
            findings.append(_finding(
                "statefulset-no-headless", "critical", name,
                "serviceName is empty or does not point at a headless Service.",
                "Set serviceName to a Service with clusterIP None.",
            ))
        replicas = spec.get("replicas", 1)
        if replicas == 0:
            findings.append(_finding(
                "statefulset-zero-replicas", "critical", name,
                "The StatefulSet has no replicas.",
                "Set replicas to at least 1.",
            ))
        elif replicas == 1:
            findings.append(_finding(
                "statefulset-single-replica", "warning", name,
                "A single pod has no failover.",
                "Raise replicas, or accept that this node has no standby.",
            ))
        policy = spec.get("podManagementPolicy") or "OrderedReady"
        if policy == "Parallel":
            findings.append(_finding(
                "statefulset-parallel", "warning", name,
                "podManagementPolicy is Parallel.",
                "Use OrderedReady so the next ordinal waits until the previous pod is ready.",
            ))
        strategy = (spec.get("updateStrategy") or {}).get("type") or "RollingUpdate"
        if strategy == "OnDelete":
            findings.append(_finding(
                "statefulset-on-delete", "info", name,
                "The update strategy is OnDelete.",
                "Delete the pod to roll it.",
            ))
        if replicas and _partition(spec) == replicas:
            findings.append(_finding(
                "rollout-not-started", "info", name,
                "The rolling update partition equals replicas.",
                "Lower the partition when you want the rollout to start.",
            ))
        if not _has_prestop(item["pod"]):
            findings.append(_finding(
                "prestop-missing", "info", name,
                "No container has a preStop hook.",
                "The grace period is the shutdown check. Add a preStop hook if the client needs one.",
            ))
    return findings


def _deployments(workloads: list[dict]) -> list[Finding]:
    findings = []
    for item in workloads:
        if item["kind"] != "Deployment":
            continue
        spec = item["obj"].get("spec") or {}
        name = item["name"]
        if item["chain_data"]:
            findings.append(_finding(
                "deployment-holds-chain-data", "critical", name,
                "A Deployment holds chain data.",
                "Run the chain client as a StatefulSet with a claim template.",
            ))
            continue
        strategy = (spec.get("strategy") or {}).get("type") or "RollingUpdate"
        if strategy == "Recreate":
            findings.append(_finding(
                "deployment-recreate", "info", name,
                "The proxy Deployment uses Recreate.",
                "Recreate is an operator choice for a stateless proxy.",
            ))
        rolling = ((spec.get("strategy") or {}).get("rollingUpdate") or {})
        unavailable = rolling.get("maxUnavailable", 0)
        replicas = spec.get("replicas", 1)
        if strategy == "RollingUpdate" and replicas <= 1 and _positive(unavailable):
            findings.append(_finding(
                "deployment-max-unavailable", "warning", name,
                "A single-replica proxy can go fully unavailable during a rollout.",
                "Set maxUnavailable to 0, or raise replicas.",
            ))
    return findings


def _services(chain: list[dict], objects: list[dict]) -> list[Finding]:
    findings = []
    engine_ports = {8551}
    rpc_ports = {8545, 8546}
    for item in chain:
        profile = _primary(item)
        if profile is None or profile.role != "execution":
            continue
        if profile.engine_port:
            engine_ports.add(profile.engine_port)
        rpc_ports.update(profile.rpc_ports)
    for service in _kind(objects, "Service"):
        published = set(_ports(service))
        name = (service.get("metadata") or {}).get("name") or ""
        stype = (service.get("spec") or {}).get("type") or "ClusterIP"
        if stype in {"LoadBalancer", "NodePort"} and published & engine_ports:
            findings.append(_finding(
                "engine-api-exposed", "critical", name,
                "The engine API is published outside the cluster.",
                "Keep the engine API on a ClusterIP or a headless Service.",
            ))
        if stype in {"LoadBalancer", "NodePort"} and published & rpc_ports:
            findings.append(_finding(
                "rpc-exposed", "warning", name,
                "RPC or WebSocket is reachable from outside the cluster.",
                "Publish RPC through a proxy you intend to expose, or use ClusterIP.",
            ))
    for ingress in _kind(objects, "Ingress"):
        for port in _ingress_ports(ingress):
            if port in engine_ports:
                findings.append(_finding(
                    "engine-api-exposed", "critical", (ingress.get("metadata") or {}).get("name") or "",
                    "The engine API is published through an Ingress.",
                    "Remove the engine API from the Ingress.",
                ))
    for item in chain:
        profile = _primary(item)
        if profile is None or not profile.p2p_ports:
            continue
        service_name = (item["obj"].get("spec") or {}).get("serviceName") or ""
        service = _find(objects, "Service", service_name, item["namespace"])
        if service and not (set(_ports(service)) & set(profile.p2p_ports)):
            findings.append(_finding(
                "peer-port-missing", "warning", item["name"],
                "No Service exposes the client peer port.",
                "Add the peer port to the headless Service.",
            ))
    return findings


def _secrets(workloads: list[dict], objects: list[dict]) -> tuple[list[Finding], list[str]]:
    findings = []
    notes = []
    for item in workloads:
        for container, profile in item["known"]:
            if profile is None or not profile.jwt:
                continue
            mounted, problem = _jwt_state(item["pod"], container)
            target = item["name"]
            if problem is not None:
                kind, detail = problem
                if kind == "env":
                    reason = f"The JWT is set from {detail}." if detail else "The JWT is set from the environment."
                    findings.append(_finding(
                        "jwt-from-env", "critical", target, reason,
                        "Mount the JWT Secret as a file.",
                    ))
                else:
                    reason = f"The JWT comes from {detail}." if detail else "The JWT comes from a ConfigMap."
                    findings.append(_finding(
                        "jwt-from-configmap", "critical", target, reason,
                        "Move the JWT to a Secret and mount it as a file.",
                    ))
            elif not mounted:
                findings.append(_finding(
                    "jwt-missing", "critical", target,
                    "The client requires a JWT and no Secret is mounted as a file.",
                    "Mount the JWT Secret as a file.",
                ))
            else:
                for secret_name, path in mounted:
                    if not _find(objects, "Secret", secret_name, item["namespace"]):
                        findings.append(_finding(
                            "jwt-missing", "critical", target,
                            f"Secret {secret_name} mounted at {path} is missing.",
                            "Create the Secret, or fix the volume so it names the Secret that exists.",
                        ))
                    else:
                        note = f"jwt {target} Secret {secret_name} mounted at {path}"
                        if note not in notes:
                            notes.append(note)
    for config in _kind(objects, "ConfigMap"):
        keys = list((config.get("data") or {})) + list((config.get("binaryData") or {}))
        name = (config.get("metadata") or {}).get("name") or ""
        if any(_sensitive(key) or _sensitive(name) for key in keys) or _sensitive(name):
            findings.append(_finding(
                "key-in-configmap", "critical", name,
                "A node key or keystore is stored in a ConfigMap.",
                "Move the key material to a Secret.",
            ))
    return findings, notes


def _storage(chain: list[dict], objects: list[dict], *, live: bool, classes_known: bool) -> list[Finding]:
    findings = []
    classes = {
        (item.get("metadata") or {}).get("name"): item
        for item in _kind(objects, "StorageClass")
        if (item.get("metadata") or {}).get("name")
    }
    defaults = [item for item in classes.values() if _default_class(item)]
    for item in chain:
        for claim in (item["obj"].get("spec") or {}).get("volumeClaimTemplates") or []:
            claim_name = (claim.get("metadata") or {}).get("name") or ""
            if claim_name not in item["data_names"]:
                continue
            findings.extend(_claim_findings(item["name"], claim, classes, defaults, classes_known, _primary(item)))
            if live:
                findings.extend(_pvc_findings(item, claim_name, objects))
    return findings


def _claim_findings(
    target: str,
    claim: dict,
    classes: dict,
    defaults: list[dict],
    classes_known: bool,
    profile: Profile | None,
) -> list[Finding]:
    findings = []
    spec = claim.get("spec") or {}
    if (spec.get("accessModes") or []) != ["ReadWriteOnce"]:
        findings.append(_finding(
            "storage-not-rwo", "critical", target,
            "Chain data is not ReadWriteOnce.",
            "Set the claim access mode to ReadWriteOnce.",
        ))
    size = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    if not size:
        findings.append(_finding(
            "storage-no-size", "critical", target,
            "The data claim does not request a size.",
            "Set resources.requests.storage.",
        ))
    elif profile and profile.storage and _bytes(size) < _bytes(profile.storage):
        findings.append(_finding(
            "storage-below-floor", "warning", target,
            f"The data claim is {size}, below the {profile.storage} floor. {profile.source}",
            "Raise the claim size to the floor.",
        ))
    class_name = spec.get("storageClassName")
    chosen: list[dict] = []
    if class_name:
        if class_name in classes:
            chosen = [classes[class_name]]
    elif class_name == "":
        findings.append(_finding(
            "storage-no-class", "critical", target,
            "The data claim sets an empty storageClassName.",
            "Set storageClassName, or omit it to use the default StorageClass.",
        ))
    elif defaults:
        chosen = defaults
    elif classes_known:
        findings.append(_finding(
            "storage-no-class", "critical", target,
            "The data claim has no StorageClass and no default class is set.",
            "Set storageClassName, or mark a default StorageClass.",
        ))
    for storage_class in chosen:
        findings.extend(_class_findings(target, storage_class))
    return findings


def _class_findings(target: str, storage_class: dict) -> list[Finding]:
    findings = []
    name = (storage_class.get("metadata") or {}).get("name") or "The StorageClass"
    mode = storage_class.get("volumeBindingMode") or "Immediate"
    if mode != "WaitForFirstConsumer":
        reason = f"StorageClass {name} binds immediately." if mode == "Immediate" else f"StorageClass {name} volumeBindingMode is {mode}."
        findings.append(_finding(
            "storage-bind-immediate", "warning", target, reason,
            "Use WaitForFirstConsumer so the volume is created with the pod.",
        ))
    if storage_class.get("allowVolumeExpansion") is not True:
        findings.append(_finding(
            "storage-no-expansion", "warning", target,
            f"StorageClass {name} does not allow volume expansion.",
            "Enable allowVolumeExpansion so a growing chain can be resized.",
        ))
    return findings


def _pvc_findings(item: dict, claim_name: str, objects: list[dict]) -> list[Finding]:
    findings = []
    prefix = f"{claim_name}-{item['name']}-"
    for pvc in _kind(objects, "PersistentVolumeClaim"):
        meta = pvc.get("metadata") or {}
        name = meta.get("name") or ""
        namespace = meta.get("namespace") or "default"
        ordinal = name[len(prefix):] if name.startswith(prefix) else ""
        if namespace != item["namespace"] or not ordinal.isdigit():
            continue
        phase = (pvc.get("status") or {}).get("phase") or "unset"
        if phase != "Bound":
            findings.append(_finding(
                "pvc-not-bound", "critical", name,
                f"The PersistentVolumeClaim phase is {phase}.",
                "Bind the claim before the node is treated as ready.",
            ))
    return findings


def _images(workloads: list[dict]) -> list[Finding]:
    findings = []
    for item in workloads:
        proxy = item["kind"] == "Deployment" and not item["chain_data"]
        for container, profile in item["containers"]:
            image = str(container.get("image") or "")
            if not image or _image_ok(image):
                continue
            if proxy:
                findings.append(_finding(
                    "image-moving-tag", "info", item["name"],
                    f"The proxy image {image} is not pinned.",
                    "Pin the proxy image when you want a reviewed release.",
                ))
                continue
            if profile is None and not _mounts_chain_data(container, item):
                continue
            findings.append(_finding(
                "image-moving-tag", "warning", item["name"],
                f"The client image {image} is a moving tag.",
                "Move the node on a reviewed release tag or a digest, not on a moving tag.",
            ))
    return findings


def _resources(workloads: list[dict]) -> list[Finding]:
    findings = []
    for item in workloads:
        for container, profile in _resource_clients(item):
            requests = ((container.get("resources") or {}).get("requests") or {})
            limits = ((container.get("resources") or {}).get("limits") or {})
            missing = [key for key in ("cpu", "memory") if key not in requests]
            if missing:
                findings.append(_finding(
                    "resources-no-requests", "warning", item["name"],
                    f"Missing {' and '.join(missing)} requests. {profile.source}",
                    "Set CPU and memory requests for this client.",
                ))
            for key, floor in (("cpu", profile.cpu), ("memory", profile.memory)):
                if not floor:
                    continue
                if key in requests and _below_floor(key, requests[key], floor):
                    findings.append(_finding(
                        "resources-below-floor", "warning", f"{item['name']}/{key}",
                        f"The {key} request {requests[key]} is below the floor {floor}. {profile.source}",
                        "Raise the request to the floor.",
                    ))
                if key in limits and _below_floor(key, limits[key], floor):
                    findings.append(_finding(
                        "resources-below-floor", "warning", f"{item['name']}/{key}",
                        f"The {key} limit {limits[key]} is below the floor {floor}. {profile.source}",
                        "Raise the limit to the floor.",
                    ))
    return findings


def _resource_clients(item: dict) -> list[tuple[dict, Profile]]:
    if item["known"]:
        return item["known"]
    if item["kind"] == "Deployment" and not item["chain_data"]:
        return [
            (container, profile)
            for container, profile in item.get("recognized") or []
            if profile is not None and profile.role == "relay"
        ]
    return []


def _below_floor(key: str, value: object, floor: str) -> bool:
    if key == "cpu":
        return _cpu(value) < _cpu(floor)
    return _bytes(value) < _bytes(floor)


def _probes(workloads: list[dict]) -> list[Finding]:
    findings = []
    for item in workloads:
        for container, profile in item["known"]:
            if profile.exempt_probes:
                continue
            probes = ("livenessProbe", "readinessProbe", "startupProbe")
            if not any(container.get(name) for name in probes):
                findings.append(_finding(
                    "probes-missing", "warning", item["name"],
                    "The client has no probes.",
                    "Add a startup probe so a long sync is not killed.",
                ))
                continue
            liveness = container.get("livenessProbe") or {}
            port = _probe_port(liveness)
            if port in profile.rpc_ports and not container.get("startupProbe"):
                findings.append(_finding(
                    "liveness-kills-sync", "warning", item["name"],
                    "Liveness uses the RPC port and there is no startup probe.",
                    "Add a startup probe. A long sync will otherwise fail liveness and the kubelet will restart the node.",
                ))
    return findings


def _restarts(objects: list[dict]) -> tuple[list[Finding], bool]:
    findings = []
    pods = _kind(objects, "Pod")
    for pod in pods:
        name = (pod.get("metadata") or {}).get("name") or ""
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            waiting = ((status.get("state") or {}).get("waiting") or {}).get("reason")
            terminated = ((status.get("lastState") or {}).get("terminated") or {}).get("reason")
            current = ((status.get("state") or {}).get("terminated") or {}).get("reason")
            reason = terminated or current
            if reason == "OOMKilled" or waiting == "OOMKilled":
                findings.append(_finding(
                    "pod-oom", "critical", name,
                    "A container was OOMKilled.",
                    "Raise the memory limit above the client floor and inspect the node.",
                ))
            if waiting == "CrashLoopBackOff":
                findings.append(_finding(
                    "pod-crash-loop", "critical", name,
                    "A container is in CrashLoopBackOff.",
                    "Read the previous logs and fix the crash before treating the node as ready.",
                ))
            restart_count = status.get("restartCount") or 0
            if restart_count > 3 and reason != "OOMKilled" and waiting not in {"CrashLoopBackOff", "OOMKilled"}:
                findings.append(_finding(
                    "pod-restarts", "warning", name,
                    f"The container has restarted {restart_count} times.",
                    "Inspect the previous exit before the next upgrade.",
                ))
    return findings, bool(pods)


def _usage(rows: list[dict]) -> list[Finding]:
    findings = []
    for row in rows:
        ratio = row["used"] / row["capacity"]
        if ratio > 0.95:
            findings.append(_finding(
                "storage-almost-full", "critical", row["claim"],
                "Volume usage is above 95% of the claim.",
                "Expand the volume, or prune, before the node stops.",
            ))
        elif ratio > 0.80:
            findings.append(_finding(
                "storage-filling", "warning", row["claim"],
                "Volume usage is above 80% of the claim.",
                "Plan an expansion before the next upgrade.",
            ))
    return findings


def _policies(chain: list[dict], objects: list[dict]) -> list[Finding]:
    policies = _kind(objects, "NetworkPolicy")
    if not policies:
        if chain:
            return [_finding(
                "network-policy-missing", "warning", chain[0]["namespace"],
                "No NetworkPolicy was found. This check assumes the CNI is not enforcing policy.",
                "Add a policy for the peer port and the engine API, or confirm the cluster enforces policy another way.",
            )]
        return []
    findings = []
    for item in chain:
        profile = _primary(item)
        if profile is None:
            continue
        selected = [policy for policy in policies if _selects(policy, _labels(item))]
        if profile.engine_port and profile.role == "execution" and _port_open_to_all(selected, profile.engine_port):
            findings.append(_finding(
                "engine-api-open", "warning", item["name"],
                "A NetworkPolicy leaves the engine API open to every pod.",
                "Allow the engine API only from the consensus workload.",
            ))
        if profile.p2p_ports and not any(_port_allowed(selected, port) for port in profile.p2p_ports):
            findings.append(_finding(
                "peer-port-blocked", "warning", item["name"],
                "No NetworkPolicy allows the peer port.",
                "Allow the peer port from outside the node.",
            ))
    return findings


def _pair(workloads: list[dict]) -> list[Finding]:
    execution = [item for item in workloads if _role(item) == "execution"]
    consensus = [item for item in workloads if _role(item) == "consensus"]
    findings = []
    if execution and not consensus:
        findings.append(_finding(
            "consensus-missing", "warning", execution[0]["name"],
            "An execution client is present and no consensus client is in this namespace.",
            "Confirm the consensus client, or pass the namespace where it runs.",
        ))
    exec_secrets = {name for item in execution for name in _jwt_names(item)}
    cons_secrets = {name for item in consensus for name in _jwt_names(item)}
    if exec_secrets and cons_secrets and exec_secrets != cons_secrets:
        findings.append(_finding(
            "jwt-secret-mismatch", "critical", execution[0]["name"],
            "The execution and consensus clients mount different JWT Secrets.",
            "Mount the same JWT Secret on both.",
        ))
    if execution and consensus:
        service_names = {(item["obj"].get("spec") or {}).get("serviceName") or item["name"] for item in execution}
        ports = {(_primary(item).engine_port if _primary(item) else None) for item in execution}
        ports.discard(None)
        for item in consensus:
            text = _command_text(item["pod"])
            if not any(service in text and f"{port}" in text for service in service_names for port in ports):
                findings.append(_finding(
                    "engine-endpoint-missing", "warning", item["name"],
                    "The consensus client does not target the execution Service and engine port.",
                    "Set the execution endpoint to the execution Service and the engine port.",
                ))
    return findings


def _budgets(chain: list[dict], objects: list[dict]) -> list[Finding]:
    findings = []
    budgets = _kind(objects, "PodDisruptionBudget")
    for item in chain:
        labels = _labels(item)
        matched = [budget for budget in budgets if _label_match((budget.get("spec") or {}).get("selector") or {}, labels)]
        replicas = (item["obj"].get("spec") or {}).get("replicas", 1)
        if not matched:
            findings.append(_finding(
                "pdb-missing", "warning", item["name"],
                "The chain StatefulSet has no PodDisruptionBudget.",
                "Add a budget with maxUnavailable 1.",
            ))
            continue
        for budget in matched:
            spec = budget.get("spec") or {}
            name = (budget.get("metadata") or {}).get("name") or item["name"]
            if "selector" in spec and not _selector_matches_any(spec.get("selector") or {}, chain):
                findings.append(_finding(
                    "pdb-selector-empty", "warning", name,
                    "The disruption budget selector matches no chain pod.",
                    "Point the selector at the StatefulSet pod labels.",
                ))
            minimum = spec.get("minAvailable")
            if _blocks_drain(minimum, replicas):
                findings.append(_finding(
                    "pdb-blocks-drain", "warning", name,
                    "minAvailable equals replicas, so a voluntary drain cannot proceed.",
                    "Use maxUnavailable 1. A single replica needs an upgrade plan other than eviction.",
                ))
    return findings


def _grace(chain: list[dict]) -> list[Finding]:
    findings = []
    for item in chain:
        profile = _primary(item)
        if profile is None or profile.role == "relay":
            continue
        seconds = item["pod"].get("terminationGracePeriodSeconds", 30)
        if seconds < profile.grace_seconds:
            findings.append(_finding(
                "grace-below-floor", "warning", item["name"],
                f"terminationGracePeriodSeconds is {seconds}, below the floor of {profile.grace_seconds}. {profile.source}",
                "Raise the grace period so the client can close the database.",
            ))
    return findings


def _backup(chain: list[dict], objects: list[dict]) -> list[Finding]:
    findings = []
    snapshots = _kind(objects, "VolumeSnapshot")
    velero = [obj for obj in objects if str(obj.get("apiVersion") or "").startswith("velero.io")]
    for item in chain:
        claims = [
            claim for claim in (item["obj"].get("spec") or {}).get("volumeClaimTemplates") or []
            if (claim.get("metadata") or {}).get("name") in item["data_names"]
        ]
        if not claims:
            continue
        if all(_backed_up(claim, item, snapshots, velero) for claim in claims):
            continue
        findings.append(_finding(
            "no-backup-signal", "warning", item["name"],
            "No VolumeSnapshot, Velero schedule, or kubechain.io/backup annotation was found.",
            "Record a backup, or set kubechain.io/backup on the claim template to the runbook.",
        ))
    return findings


def _placement(chain: list[dict]) -> list[Finding]:
    findings = []
    for item in chain:
        replicas = (item["obj"].get("spec") or {}).get("replicas", 1)
        if not isinstance(replicas, int) or replicas <= 1:
            continue
        if _spread(item["pod"]):
            continue
        findings.append(_finding(
            "pods-colocated", "warning", item["name"],
            "Two pods can land on one node and share a disk failure.",
            "Set pod anti-affinity on kubernetes.io/hostname, or a topology spread with maxSkew 1.",
        ))
    return findings


def _rollouts(chain: list[dict]) -> list[Rollout]:
    rows = []
    for item in chain:
        spec = item["obj"].get("spec") or {}
        strategy = (spec.get("updateStrategy") or {}).get("type") or "RollingUpdate"
        rows.append(Rollout(item["name"], strategy, _partition(spec) if strategy == "RollingUpdate" else None))
    return rows


def _backed_up(claim: dict, item: dict, snapshots: list[dict], velero: list[dict]) -> bool:
    annotations = (claim.get("metadata") or {}).get("annotations") or {}
    if annotations.get("kubechain.io/backup"):
        return True
    claim_name = (claim.get("metadata") or {}).get("name") or ""
    prefix = f"{claim_name}-{item['name']}-"
    for snapshot in snapshots:
        source = ((snapshot.get("spec") or {}).get("source") or {}).get("persistentVolumeClaimName") or ""
        if source == claim_name or source.startswith(prefix):
            return True
    for item_obj in velero:
        if item["namespace"] == ((item_obj.get("metadata") or {}).get("namespace") or item["namespace"]):
            return True
    return False


def _spread(pod: dict) -> bool:
    affinity = (((pod.get("affinity") or {}).get("podAntiAffinity") or {}))
    terms = list(affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or [])
    terms.extend(affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or [])
    for term in terms:
        body = term.get("podAffinityTerm") or term
        if body.get("topologyKey") == "kubernetes.io/hostname":
            return True
    for constraint in pod.get("topologySpreadConstraints") or []:
        if constraint.get("maxSkew") == 1:
            return True
    return False


def _jwt_state(pod: dict, container: dict) -> tuple[list[tuple[str, str]], tuple[str, str] | None]:
    volumes = {volume.get("name"): volume for volume in pod.get("volumes") or []}
    mounted: list[tuple[str, str]] = []
    for mount in container.get("volumeMounts") or []:
        volume = volumes.get(mount.get("name")) or {}
        path = str(mount.get("mountPath") or "")
        config_name = str((volume.get("configMap") or {}).get("name") or "")
        if "configMap" in volume and (_jwt_name(config_name) or "jwt" in path.lower()):
            where = f"ConfigMap {config_name} mounted at {path}" if config_name else f"a ConfigMap mounted at {path}"
            return mounted, ("configmap", where)
        for secret_name in _volume_secret_names(volume):
            if secret_name and (_jwt_name(secret_name) or "jwt" in path.lower()):
                mounted.append((secret_name, path))
    for env in container.get("env") or []:
        name = str(env.get("name") or "")
        source = env.get("valueFrom") or {}
        secret_ref = source.get("secretKeyRef") or {}
        config_ref = source.get("configMapKeyRef") or {}
        ref_name = str(secret_ref.get("name") or config_ref.get("name") or "")
        ref_key = str(secret_ref.get("key") or config_ref.get("key") or "")
        if not any(_jwt_name(part) or "jwt" in part.lower() for part in (name, ref_name, ref_key)):
            continue
        if config_ref:
            where = f"ConfigMap {config_ref.get('name')}" if config_ref.get("name") else "a ConfigMap"
            return mounted, ("configmap", where)
        if name:
            return mounted, ("env", f"the environment variable {name}")
        return mounted, ("env", "the environment")
    for source in container.get("envFrom") or []:
        config_name = str((source.get("configMapRef") or {}).get("name") or "")
        secret_name = str((source.get("secretRef") or {}).get("name") or "")
        if "configMapRef" in source and _jwt_name(config_name):
            return mounted, ("configmap", f"ConfigMap {config_name}" if config_name else "a ConfigMap")
        if "secretRef" in source and _jwt_name(secret_name):
            return mounted, ("env", f"Secret {secret_name} through the environment" if secret_name else "the environment")
    return mounted, None


def _volume_secret_names(volume: dict) -> list[str]:
    names = []
    if "secret" in volume:
        names.append(str((volume.get("secret") or {}).get("secretName") or ""))
    for source in (volume.get("projected") or {}).get("sources") or []:
        if "secret" in source:
            names.append(str((source.get("secret") or {}).get("name") or ""))
    return names


def _jwt_names(item: dict) -> list[str]:
    names = []
    for container, profile in item["known"]:
        if profile and profile.jwt:
            mounted, _problem = _jwt_state(item["pod"], container)
            names.extend(name for name, _path in mounted)
    return names


def _volume_sources(pod: dict, data_names: set[str]) -> set[str]:
    sources = set()
    for volume in pod.get("volumes") or []:
        if volume.get("name") not in data_names:
            continue
        for key in ("emptyDir", "hostPath", "persistentVolumeClaim", "configMap", "secret"):
            if key in volume:
                sources.add(key)
    return sources


def _has_prestop(pod: dict) -> bool:
    return any(((container.get("lifecycle") or {}).get("preStop")) for container in pod.get("containers") or [])


def _partition(spec: dict) -> int | None:
    rolling = ((spec.get("updateStrategy") or {}).get("rollingUpdate") or {})
    partition = rolling.get("partition")
    return partition if isinstance(partition, int) else None


def _headless(service: dict) -> bool:
    return (service.get("spec") or {}).get("clusterIP") == "None"


def _ports(service: dict) -> list[int]:
    ports = []
    for entry in (service.get("spec") or {}).get("ports") or []:
        for key in ("port", "targetPort"):
            if isinstance(entry.get(key), int):
                ports.append(entry[key])
    return ports


def _ingress_ports(ingress: dict) -> list[int]:
    ports = []
    for rule in (ingress.get("spec") or {}).get("rules") or []:
        for path in ((rule.get("http") or {}).get("paths") or []):
            number = (((path.get("backend") or {}).get("service") or {}).get("port") or {}).get("number")
            if isinstance(number, int):
                ports.append(number)
    return ports


def _primary(item: dict) -> Profile | None:
    for _, profile in item["known"]:
        if profile and profile.role in {"execution", "consensus"}:
            return profile
    return item["known"][0][1] if item["known"] else None


def _role(item: dict) -> str | None:
    profile = _primary(item)
    return profile.role if profile else None


def _labels(item: dict) -> dict:
    return ((item["obj"].get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels") or {}


def _selects(policy: dict, labels: dict) -> bool:
    return _label_match((policy.get("spec") or {}).get("podSelector") or {}, labels)


def _label_match(selector: dict, labels: dict) -> bool:
    match = selector.get("matchLabels") or {}
    if not match and not selector:
        return True
    return all(labels.get(key) == value for key, value in match.items())


def _selector_matches_any(selector: dict, chain: list[dict]) -> bool:
    return any(_label_match(selector, _labels(item)) for item in chain)


def _port_open_to_all(policies: list[dict], port: int) -> bool:
    for policy in policies:
        for rule in (policy.get("spec") or {}).get("ingress") or []:
            if not _rule_has_port(rule, port):
                continue
            sources = rule.get("from")
            if not sources or any(_from_everyone(entry) for entry in sources):
                return True
    return False


def _port_allowed(policies: list[dict], port: int) -> bool:
    for policy in policies:
        for rule in (policy.get("spec") or {}).get("ingress") or []:
            if _rule_has_port(rule, port):
                return True
    return False


def _rule_has_port(rule: dict, port: int) -> bool:
    ports = rule.get("ports")
    if not ports:
        return True
    return any(entry.get("port") == port for entry in ports)


def _from_everyone(entry: dict) -> bool:
    if not entry:
        return True
    if "podSelector" in entry and not entry["podSelector"]:
        return True
    if "namespaceSelector" in entry and not entry["namespaceSelector"]:
        return True
    return False


def _command_text(pod: dict) -> str:
    chunks = []
    for container in pod.get("containers") or []:
        chunks.extend(str(part) for part in container.get("command") or [])
        chunks.extend(str(part) for part in container.get("args") or [])
        for env in container.get("env") or []:
            if "value" in env:
                chunks.append(str(env["value"]))
    return " ".join(chunks)


def _blocks_drain(minimum: object, replicas: int) -> bool:
    if isinstance(minimum, int):
        return minimum == replicas
    if isinstance(minimum, str) and minimum.endswith("%"):
        try:
            return int(minimum[:-1]) >= 100
        except ValueError:
            return False
    return False


def _positive(value: object) -> bool:
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.endswith("%"):
        try:
            return int(value[:-1]) > 0
        except ValueError:
            return False
    return False


def _mounts_chain_data(container: dict, item: dict) -> bool:
    names = item["data_names"]
    return any(mount.get("name") in names for mount in container.get("volumeMounts") or [])


def _image_ok(image: str) -> bool:
    reference = image.strip()
    if "@" in reference:
        name, digest = reference.rsplit("@", 1)
        algorithm, separator, value = digest.partition(":")
        pinned = separator and algorithm.lower() == "sha256" and len(value) == 64
        if pinned and all(character in "0123456789abcdefABCDEF" for character in value):
            return True
        reference = name
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    if colon <= slash:
        return False
    tag = reference[colon + 1 :]
    if tag.lower() in MOVING_TAGS:
        return False
    return any(character.isdigit() for character in tag)


def _probe_port(probe: dict) -> int | None:
    http = probe.get("httpGet") or {}
    port = http.get("port")
    return port if isinstance(port, int) else None


def _default_class(storage_class: dict) -> bool:
    annotations = (storage_class.get("metadata") or {}).get("annotations") or {}
    return annotations.get("storageclass.kubernetes.io/is-default-class") == "true"


def _sensitive(value: str) -> bool:
    lowered = value.lower()
    return "keystore" in lowered or "nodekey" in lowered or lowered.startswith("utc-")


def _jwt_name(name: str) -> bool:
    return "jwt" in name.lower()


def _cpu(value: object) -> float:
    text = str(value)
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    return float(text)


def _bytes(value: object) -> int:
    text = str(value)
    for suffix, multiplier in _MEM:
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(text)


def _kind(objects: list[dict], kind: str) -> list[dict]:
    return [obj for obj in objects if obj.get("kind") == kind]


def _find(objects: list[dict], kind: str, name: str, namespace: str) -> dict | None:
    for obj in objects:
        meta = obj.get("metadata") or {}
        if obj.get("kind") == kind and meta.get("name") == name and (meta.get("namespace") or "default") == namespace:
            return obj
    return None


def _namespace(objects: list[dict]) -> str:
    names = {(obj.get("metadata") or {}).get("namespace") for obj in objects}
    names.discard(None)
    if not names:
        return "default"
    if len(names) == 1:
        return next(iter(names))
    return "multiple"


def _object_names(objects: list[dict]) -> list[str]:
    names = []
    for obj in objects:
        kind = obj.get("kind") or "Object"
        name = (obj.get("metadata") or {}).get("name") or ""
        names.append(f"{kind}/{name}")
    return names


def _finding(finding_id: str, severity: str, target: str, reason: str, action: str) -> Finding:
    return Finding(finding_id, severity, target, reason, action)
