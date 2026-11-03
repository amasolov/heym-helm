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
- **A kubelet `podPidsLimit`.** Pods have no PID limit of their own. Talos
  ships with none (`-1`).

## Platforms

`platform` only changes defaults; every one of them can also be set
explicitly.

| `platform`  | What it changes                                                                                                                       |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `generic`   | Nothing: uid 1000, `kube-dns` in `kube-system`.                                                                                       |
| `talos`     | With `networkPolicy.enabled`, adds a CiliumNetworkPolicy so the backend can reach the API server under kube-proxy replacement.      |
| `openshift` | Leaves the uid, gid and fsGroup to `restricted-v2`. Allows DNS to `openshift-dns` on 53 and **5353**, the port the DNS pods actually listen on. Creates a Route. |
| `eks`       | Nothing extra. The VPC CNI enforces NetworkPolicy only with its network-policy feature on; IMDS and Pod Identity are link-local, so already blocked. |
| `aks`       | Also blocks Azure WireServer `168.63.129.16`, a public address the private-range rules miss.                                          |
| `gke`       | Nothing extra. Dataplane V2 enforces policy. For GKE Sandbox, set `sandbox.podOverrides.runtimeClassName: gvisor`.                  |

On AKS, Kata isolation is `runtimeClassName: kata-vm-isolation`. On OpenShift
it is `kata`. On Talos, add the `siderolabs/gvisor` system extension.

**Status:** tested on Talos 1.14 with Cilium, using upstream's smoke test;
11/12 checks pass, the twelfth is
[a known upstream bug](https://github.com/heymrun/heym/issues/832). Tested on
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

## Values

The main ones; see [values.yaml](charts/heym/values.yaml) for all of them.

| Key                       | Default            | Purpose                                                                     |
| ------------------------- | ------------------ | --------------------------------------------------------------------------- |
| `platform`                | `generic`          | see above                                                                   |
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
   `ghcr.io/amasolov/charts/heym:artifacthub.io`:

   ```bash
   oras push ghcr.io/amasolov/charts/heym:artifacthub.io \
     --config /dev/null:application/vnd.cncf.artifacthub.config.v1+yaml \
     artifacthub-repo.yml:application/vnd.cncf.artifacthub.repository-metadata.layer.v1.yaml
   ```
