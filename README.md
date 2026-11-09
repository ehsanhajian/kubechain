# KubeChain

KubeChain answers one question: **is my Kubernetes blockchain deployment production ready?**

It reads workloads, Services, Secrets, storage, network policy, and disruption settings. A live check uses get, list, and watch. Secret bytes are discarded. Volume snapshots are observed and never created. The cluster is left as it was found.

ValidatorPulse raises an alert. ChainDiff decides whether a client upgrade is safe. ChainTrace is the view of the process. KubeChain is the view of that same node as a Kubernetes workload.

## Install

Python 3.11 or newer.

```bash
pip install kubechain
kubechain --version
```

The package is published from a GitHub release to [PyPI](https://pypi.org/project/kubechain/). This README is the reference.

## Commands

| Question | Command |
| --- | --- |
| Does this manifest set pass? | `check --manifests DIR` |
| Does this namespace pass? | `check --namespace NS` |
| One line for a journal? | `check --brief` |
| Can I keep the answer? | `report` |

`--manifests` needs no cluster. `--context NAME` selects a kubeconfig context for a live read. The two modes are separate. `--json` prints one document. `--brief` and `--json` together print the JSON document only.

## Examples

`examples/ready` is a geth and lighthouse pair that passes. `examples/broken` is the cases that are not ready. `examples/oom` is a saved pod status.

```bash
kubechain check --manifests examples/ready
kubechain check --manifests examples/broken
kubechain check --manifests examples/ready --brief
kubechain check --manifests examples/ready --profile examples/geth.json
kubechain report --manifests examples/ready
kubechain check --namespace nodes
```

`report` writes `kubechain-report.md`. `--output PATH` chooses the file. A directory is refused. An existing file is overwritten.

## Verdicts

| Verdict | Exit | When |
| --- | --- | --- |
| READY | 0 | No warning or critical finding. An info line still exits 0. |
| REVIEW | 1 | A warning. The workload runs, and a production gap is named. |
| NOT READY | 2 | A critical finding. Chain data, a secret, or an upgrade would be unsafe. |
| ERROR | 3 | The manifests, the profile, or the cluster could not be read. |

Every finding has an id, a severity, the object name, a reason, and an action. A check that cannot name an action does not emit a finding.

## What a ready workload looks like

A chain node is a StatefulSet with a claim template, a headless Service, and a JWT mounted as a file. Probes survive a long sync. The disruption budget matches the replica count. A backup signal exists before the next rolling upgrade. A Deployment is for a stateless proxy, not for chain data.

A single replica is a warning. It runs, and it has no failover.

`--manifests` has no pod status and no kubelet stats. The report says restarts were not read, and storage performance was not read. That absence is not a finding. A live read reports OOMKilled, CrashLoopBackOff, and restart counts. Volume usage is reported when kubelet stats can be read. Above 80% is a warning. Above 95% is critical. When the stats endpoint is unavailable, the report says storage performance was not read.

## Clients

The catalog recognizes geth, reth, nethermind, erigon, besu, op-geth, lighthouse, prysm, teku, nimbus, lodestar, op-node, and mev-boost. Each profile sets ports, the data volume, whether a JWT is required, and a floor for CPU, memory, storage, and shutdown grace. The floor is a KubeChain floor. The source line is in `clients.json`.

An unrecognized workload is still listed and is judged only by the generic Kubernetes checks. op-node is the consensus side for op-geth. mev-boost does not sync and does not hold chain data.

`--profile FILE` replaces the built-in profile when the container name matches `comm`. `examples/geth.json` is the sample.

```json
{
  "comm": "geth",
  "p2p_ports": [30303],
  "rpc_ports": [8545, 8546],
  "engine_port": 8551,
  "jwt": true,
  "data_volume": "data",
  "cpu": "2",
  "memory": "8Gi",
  "storage": "100Gi",
  "grace_seconds": 300
}
```

An unknown field is exit 3.

## Live read

`deploy/kubechain-reader.yaml` is a Role and a binding for the object kinds a live check reads. The verbs are get, list, and watch. Apply it yourself. KubeChain does not apply it.

```bash
kubectl apply -f deploy/kubechain-reader.yaml -n nodes
```

Listing a Secret returns its bytes from the API. The CLI discards them. The report prints the Secret name and the mount path.

The example is one namespace. A ClusterRole for more than one namespace is a comment in that file, not a second manifest.

StorageClass and kubelet volume stats are cluster-scoped. The Role comment says which extra rules those reads need. If they are not granted, the report says so and continues.

## Development

```bash
pip install -e ".[dev]"
pytest
```
