# Heym Helm chart (community)

A community Helm chart for [Heym](https://heym.run). It runs every Heym sandbox
as its own Kubernetes pod, with no Docker socket:

- Python tools and skills
- the Code node
- MCP stdio servers
- Playwright
- Codex and OpenCode

This uses Heym's Kubernetes sandbox backend, which shipped in 0.0.144
([heymrun/heym#848](https://github.com/heymrun/heym/pull/848)).

This is not an official Heym project. Heym itself is MIT with the Commons
Clause; this chart is Apache-2.0.

## Install

```bash
kubectl create namespace heym
kubectl -n heym create secret generic heym-secret \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=ENCRYPTION_KEY="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL='postgresql+asyncpg://heym:PASSWORD@HOST:5432/heym'
helm install heym oci://ghcr.io/amasolov/charts/heym -n heym \
  --set platform=generic --set secrets.existingSecret=heym-secret \
  --set url=https://heym.example.com
```

**Back up `ENCRYPTION_KEY`.** It encrypts every stored credential. The chart
never generates it, because GitOps tools render charts without `lookup`, so a
generated key would change on every sync.

## Requirements

- **Kubernetes 1.30 or newer.**
- **A CNI that enforces NetworkPolicy.** Heym checks this with a canary pod
  and refuses offline sandboxes if the canary can connect anywhere.
- **PostgreSQL with `pgvector`.** If Heym's database role can't create
  extensions, install `vector` as a superuser first.
- **A kubelet `podPidsLimit`.** Pods have no PID limit of their own.

## Platforms

`platform` and `cni` only change defaults; every one of them can also be
set explicitly. They're separate because Cilium runs on many platforms, and
most platforms offer several CNIs.

| `platform`  | What it changes                                                                                                                       |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `generic`   | Nothing: uid 1000, `kube-dns` in `kube-system`. Use it for Talos, k3s, kubeadm and so on.                                         |
| `openshift` | Leaves the uid, gid and fsGroup to `restricted-v2`. Allows DNS to `openshift-dns` on 53 and **5353**, the port the DNS pods actually listen on. Creates a Route. |
| `eks`       | Nothing extra. The VPC CNI enforces NetworkPolicy only with its network-policy feature on; IMDS and Pod Identity are link-local, so already blocked. |
| `aks`       | Also blocks Azure WireServer `168.63.129.16`, a public address the private-range rules miss.                                          |
| `gke`       | Nothing extra. Dataplane V2 enforces policy. For GKE Sandbox, set `sandbox.podOverrides.runtimeClassName: gvisor`.                  |

| `cni`    | What it changes                                                                                                                        |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `""`     | Nothing CNI-specific.                                                                                                                  |
| `cilium` | With `networkPolicy.enabled`, adds a CiliumNetworkPolicy so the backend can reach the API server. Kube-proxy replacement rewrites that address to the API server's own reserved identity, which an `ipBlock` rule can't match. |

On AKS, Kata isolation is `runtimeClassName: kata-vm-isolation`. On OpenShift
it is `kata`.

### Talos

Talos needs no platform preset, but its defaults matter:

- **Default CNI.** Talos ships Flannel, which doesn't enforce NetworkPolicy,
  so Heym refuses offline sandboxes there (Python tools, the Code node
  without network). Install Cilium or another enforcing CNI. With Cilium, set
  `cni: cilium`.
- **No PID limit.** The kubelet has no `podPidsLimit` (`-1`). Set one in the
  machine config (`machine.kubelet.extraConfig.podPidsLimit`).
- **gVisor.** For gVisor sandboxes, add the `siderolabs/gvisor` system
  extension and a `RuntimeClass`, then set
  `sandbox.podOverrides.runtimeClassName`.

**Status:** this chart runs in production on Talos 1.14 with Cilium (`cni: cilium`).
Upstream's sandbox smoke test passes 12/12 there; the agent-skill check can
fail intermittently because of
[an upstream attach race](https://github.com/heymrun/heym/issues/832). Tested on
OpenShift 4.22 with equivalent manifests. Rendered but untested on EKS, AKS
and GKE; reports welcome.

## How sandboxes work

Heym creates one pod per run in the release namespace, labelled
`app.kubernetes.io/managed-by=heym`. Each sandbox pod:

- runs under Pod Security `restricted`, with no ServiceAccount token
- mounts only its own run's directory of the data volume
- is deleted when the run ends

The chart's NetworkPolicies give sandboxes no ingress. Offline sandboxes get
no egress at all. Online sandboxes get DNS and the internet, but nothing
in-cluster.

The backend's ServiceAccount gets a namespaced Role covering pods, pod attach,
pod logs, and reading PVCs. RBAC can't restrict which pods it creates, so keep
nothing else in Heym's namespace.

With a `ReadWriteOnce` data volume, sandboxes run on the backend's node. Use
`ReadWriteMany` to spread them across nodes.

## Moving an existing install onto this chart

A Deployment's selector is immutable. If Heym already runs from another chart
(app-template, say) or from plain manifests, the chart can't take over the
existing Deployment in place. Set `fullnameOverride` to a new name, such as
`heym-app`, so nothing collides:

- **Plain `helm upgrade` on the same release:** Helm creates the new objects
  and deletes the old ones in one step.
- **Flux:** when a HelmRelease's chart name changes, Flux uninstalls the old
  release and installs the new one. The new name still keeps the new objects
  clear of anything the old release or your own manifests own.

Either way:

- **Keep the data volume outside the release** (`persistence.existingClaim`),
  so neither path can delete it. With `ReadWriteOnce`, expect a short outage
  while the volume moves to the new pod.
- **Keep your existing NetworkPolicies until the chart's are in place.**
  Policies are additive and the new names differ, so leaving the old ones
  during the switch means the pods are never unfenced. Remove them afterwards.
- **Update anything that targets the old name,** such as a VPA, a
  ServiceMonitor, or a Reloader or PDB selector.

## Values

The main ones; see [values.yaml](values.yaml) for all of them.

| Key                       | Default            | Purpose                                                                     |
| ------------------------- | ------------------ | --------------------------------------------------------------------------- |
| `platform`                | `generic`          | see above                                                                   |
| `cni`                     | `""`               | `cilium` for Cilium with kube-proxy replacement                             |
| `url`                     | `http://localhost:4017` | public URL (`FRONTEND_URL`, `CORS_ORIGINS`)                            |
| `secrets.existingSecret`  | `""`               | Secret with `SECRET_KEY`, `ENCRYPTION_KEY`, `DATABASE_URL`                  |
| `image.digest`            | `""`               | pins the backend and, through it, every sandbox                             |
| `env`                     | `{}`               | extra Heym settings, e.g. `HEYM_ADMIN_EMAILS`                               |
| `persistence.accessModes` | `[ReadWriteOnce]`  | `ReadWriteMany` lets sandboxes leave the backend's node                     |
| `sandbox.podOverrides`    | `{}`               | `runtimeClassName`, `nodeSelector`, `tolerations` for sandbox pods          |
| `networkPolicy.enabled`   | `false`            | a policy for the backend too; add site-specific `extraEgress`               |
| `ingress` / `httpRoute` / `route` | off (Route on for OpenShift) | exposure                                              |

## Publishing (maintainers)

1. Bump `version` (and `appVersion` for a new Heym) in `Chart.yaml`.
2. Tag `chart-<version>`. CI pushes `oci://ghcr.io/amasolov/charts/heym`.
3. Artifact Hub reads the repository metadata from the OCI artifact
   `ghcr.io/amasolov/charts/heym:artifacthub.io`. The `Artifact Hub metadata`
   workflow pushes it whenever `artifacthub-repo.yml` changes.
