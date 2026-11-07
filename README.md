# KubeChain

KubeChain answers one question: **is my Kubernetes blockchain deployment production ready?**

It reads workloads, Services, Secrets, storage, network policy, and disruption settings. A live check uses get, list, and watch. Secret bytes are discarded. Volume snapshots are observed and never created. The cluster is left as it was found.

ValidatorPulse raises an alert. ChainDiff decides whether a client upgrade is safe. ChainTrace is the view of the process. KubeChain is the view of that same node as a Kubernetes workload.

Nothing is published yet. The milestones on this repository are the work.

## Commands

| Question | Command |
| --- | --- |
| Does this manifest set pass? | `check --manifests DIR` |
| Does this namespace pass? | `check --namespace NS` |
| One line for a journal? | `check --brief` |
| Can I keep the answer? | `report` |

`--manifests` needs no cluster. `--context NAME` selects a kubeconfig context for a live read. The two modes are separate. `--json` prints one document.

## Verdicts

| Verdict | Exit | When |
| --- | --- | --- |
| READY | 0 | No warning or critical finding. An info line still exits 0. |
| REVIEW | 1 | A warning. The workload runs, and a production gap is named. |
| NOT READY | 2 | A critical finding. Chain data, a secret, or an upgrade would be unsafe. |
| ERROR | 3 | The manifests or the cluster could not be read. |

## What a ready workload looks like

A chain node is a StatefulSet with a claim template, a headless Service, and a JWT mounted as a file. Probes survive a long sync. The disruption budget matches the replica count. A backup signal exists before the next rolling upgrade. A Deployment is for a stateless proxy, not for chain data.

A single replica is a warning. It runs, and it has no failover.

## Work

1. **Read the cluster, change nothing.** A verdict from a saved manifest set or a live context.
2. **Kubernetes validation.** StatefulSet, Deployment, Service, Secret, storage, and image references.
3. **Blockchain workload checks.** Client profiles, probes, restarts, volume usage, network policy, and the execution and consensus pair.
4. **Upgrade safety.** Disruption budgets, rolling upgrade and shutdown grace, a backup signal, and placement when there is more than one pod.
5. **A report an operator can keep.** Stable finding ids, a markdown file, and a local profile.
6. **Discoverability.** After the product tasks. GitHub, PyPI, and a one-page summary.
