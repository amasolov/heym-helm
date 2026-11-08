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

For a fresh install, provide your external PostgreSQL connection in an ignored
`values.private.yaml` (or supply it from an external Secret):

```yaml
secrets:
  databaseUrl: postgresql+asyncpg://USER:PASSWORD@HOST:5432/heym
```

```bash
helm install heym oci://ghcr.io/amasolov/charts/heym -n heym --create-namespace \
  -f values.private.yaml --set url=https://heym.example.com
```

### Image selection

New installs use `ghcr.io/heymrun/heym:latest` with `imagePullPolicy: Always`.
The registry resolves that tag when a pod starts. Running pods keep their image;
publishing a new Heym image does not itself trigger a Helm or Argo rollout.

Pin an exact release with:

```yaml
image:
  tag: "0.0.149"
```

For an immutable pin, also set `image.digest: sha256:<64-hex-digest>`, or use
`image.tag: "0.0.149@sha256:<64-hex-digest>"`. Version and digest pins default to
`IfNotPresent`. Set `image.pullPolicy` to override either default. An empty
`image.tag` uses this chart's `appVersion` instead of the floating tag.
Use a digest when main, workers and sandbox pods must run identical image
contents throughout an upgrade. Existing installations with explicit image pins
keep those pins when upgrading the chart.

The chart does not deploy PostgreSQL. It generates `SECRET_KEY` and a valid
32-byte Fernet `ENCRYPTION_KEY` in a create-once hook Job when needed. Keys are
created in Kubernetes, never randomized during Helm rendering. Retries, Helm
upgrades and Argo syncs reuse the existing `<fullname>-generated-keys` Secret;
they do not rotate it. The Secret is intentionally retained across uninstall.
Back up the encryption key alongside the database: replacing it loses access to
stored credentials. Losing a generated Secret is not a supported key rotation.

### Choose which secrets you control

A single pre-created Secret remains supported (no generated Secret or key Job):

```yaml
secrets:
  existingSecret: heym-secret
```

That Secret normally contains `SECRET_KEY`, `ENCRYPTION_KEY`, and `DATABASE_URL`.
For split SOPS/ESO Secrets, reference them directly and disable generation:

```yaml
secrets:
  generate: false
  existingSecrets: [heym-runtime, heym-database]
envFrom:
  - configMapRef:
      name: heym-database-host-settings
```

Together these sources must supply application keys and either `DATABASE_URL`
or Heym's `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` and
`POSTGRES_PASSWORD` settings. The chart renders references only, and does not
look up or take ownership of pre-created Secrets.

You can also control individual keys, including custom source key names:

```yaml
secrets:
  existingSecrets: [database-connection]
  keyRefs:
    ENCRYPTION_KEY:
      name: important-heym-key
      key: encryption-key
```

This example keeps the external encryption key and generates application defaults
for the remaining keys. Per-key refs use `optional: false` unless a ref sets
`optional: true`, so a missing Secret or key blocks startup. Generated defaults are loaded first, inline values next,
then `existingSecrets` in order and `envFrom`. `keyRefs` and explicit `env`
entries take precedence over those bulk sources; do not define the same key in
both `keyRefs` and `env`. Never change the encryption-key source on an existing
database unless the new source contains the same key or a separately planned
credential migration is performed.

Generation uses a restricted, non-root hook Job with its own ServiceAccount. Its
namespaced Role can get only the target Secret and create Secrets; Kubernetes
RBAC cannot constrain `create` by resource name. It has no update, delete or list
permissions. The Job logs no key values. Helm hooks and explicit Argo PreSync
annotations support either lifecycle, including apps with other Argo hooks.
The hook's ServiceAccount/Role/RoleBinding remain for subsequent runs; remove them
explicitly when retiring generation. With fully pre-created keys, disable
generation and none of those resources are rendered.

### Rotating `ENCRYPTION_KEY`

From Heym 0.0.149, `ENCRYPTION_KEY` can be replaced without downtime and without
re-entering credentials ([upstream guide](https://github.com/heymrun/heym/blob/main/ENVIRONMENT-VARIABLES.md#rotating-encryption_key)).
`ENCRYPTION_KEYS_EXTRA` holds keys that decrypt but never encrypt. With the
chart, each phase is a values or Secret change followed by a restart of the
backend and every worker:

| Phase | `ENCRYPTION_KEY` | `ENCRYPTION_KEYS_EXTRA` | Then |
|---|---|---|---|
| 1. Introduce | old | new | restart everything |
| 2. Promote | new | old | restart everything |
| 3. Re-encrypt | new | old | **Settings → Encryption**, start, wait for **Completed** |
| 4. Retire | new | — | only once no backup from before phase 3 is needed |

With `secrets.existingSecret` (or `existingSecrets`), add or change the keys in
that Secret: everything in it is loaded. Changing a Secret's data does not
restart pods, so roll the Deployment and the worker StatefulSet afterwards, or
use a reloader. To keep the two keys in separate Secrets, use per-key refs:

```yaml
secrets:
  keyRefs:
    ENCRYPTION_KEY: {name: heym-key-2026, key: key}
    ENCRYPTION_KEYS_EXTRA: {name: heym-key-2025, key: key, optional: true}
```

Generated keys work the same way: point `keyRefs.ENCRYPTION_KEY` at a Secret
holding the new key, and `keyRefs.ENCRYPTION_KEYS_EXTRA` at
`<fullname>-generated-keys`, key `ENCRYPTION_KEY`. The generated Secret is never
rewritten, so it keeps the old key for as long as you reference it.

Before the server starts, Heym checks its keys against the database
(`python -m app.key_check`). The image entrypoint does that on the backend, and
the chart's worker command runs it before uvicorn. After phase 3 the old key is
refused as `ENCRYPTION_KEY`, so a restored old Secret fails fast instead of
writing values nobody else can read. If the check refuses on a database where
nothing decrypts, read the upstream guide before setting
`HEYM_ACCEPT_CURRENT_ENCRYPTION_KEY`.

### Other ways to supply keys

Existing inline `secretKey`, `encryptionKey` and `databaseUrl` values still work.
Fully supplied inline keys do not run generation. Inline Secret resources carry
a retention annotation. When moving an older chart's unannotated inline Secret
to external ownership, first upgrade with the same inline values to add retention,
then switch to `existingSecret` pointing to that same Secret. Do not combine the
single `existingSecret` mode with inline values. No existing key is automatically
copied, deleted or rotated when choosing a different source.

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

## Credentials from Kubernetes Secrets

```yaml
kubernetesCredentials:
  enabled: true
```

From Heym 0.0.149, a Secret labelled `heym.run/credential: "true"` becomes a
read-only credential. Its values stay in each process's memory, never in the
database or its backups, and changing the Secret rotates the credential without a
restart. Anything that writes a Secret works: External Secrets, SOPS through Flux
or Argo CD, Sealed Secrets, `kubectl`.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: heym-cred-llm-gateway
  labels:
    heym.run/credential: "true"
  annotations:
    heym.run/name: llm-gateway              # the credential name in Heym
    heym.run/type: custom                   # a credential type
    heym.run/owner: admin@example.com       # an existing user
    heym.run/share-users: alice@example.com # optional, comma-separated emails
    heym.run/share-teams: automation        # optional, comma-separated team names
stringData:
  base_url: http://llm-gateway.internal/v1
  api_key: sk-...
```

Each data key is a config field; for nested or non-string fields put the whole
config as JSON under one `config.json` key. Types that store refreshed OAuth
tokens (Codex, Google, BigQuery, Linear or Notion in OAuth mode) are refused.
With External Secrets, put the label and annotations on
`spec.target.template.metadata`. See the
[upstream guide](https://github.com/heymrun/heym/blob/main/deploy/kubernetes/README.md#credentials-from-kubernetes-secrets).

What the chart does:

- **`HEYM_K8S_CREDENTIALS=true` on the backend and every worker.** Each process
  watches the Secrets itself, because values never reach the database.
- **A Role with `get`, `list` and `watch` on Secrets**, bound to the backend's
  ServiceAccount, and a mounted token even with `sandbox.enabled=false`. RBAC
  can't narrow Secrets by label, so Heym can read every Secret in the watched
  namespace, including its own key Secrets. Keep other applications' Secrets out
  of it, or set `kubernetesCredentials.namespace` to a namespace that holds only
  Heym credentials (the Role is created there).
- **The API server is reachable**: with `networkPolicy.enabled`, the
  `apiServerCIDRs` rule and, on Cilium, the API-server policies are rendered for
  this feature too.
- **Refuses** `HEYM_PYTHON_TOOL_SANDBOX`, `HEYM_PLAYWRIGHT_SANDBOX` or
  `HEYM_MCP_STDIO_SANDBOX` set to `subprocess`, as Heym would at startup: user
  code would run next to the token that reads Secrets.

A pod can be Ready before it has synced: startup doesn't wait for the API
server. Until a process has synced it claims no queued runs, and a run that
uses an unsynced credential fails before any node runs. **Settings →
Kubernetes** lists every labelled Secret and its status. A deleted Secret leaves
its credential unavailable, never deleted, so a prune and re-apply restores it.

## Load distribution (workers)

```yaml
cluster:
  enabled: true
  replicas: 2
```

This turns on Heym's clustering (`HEYM_CLUSTER_ENABLED`) and adds a StatefulSet
of worker pods. Queued runs are split between main and the workers by the
weights under **Settings → Instances**: schedules, webhooks and the execute
API, chat portals, MCP, Slack, Telegram and Discord, IMAP and RabbitMQ
triggers, Heym events and error workflows.

These always stay on the main pod: the UI, API, MCP endpoint and all ingress,
editor test runs, and any workflow with a main-only node (Codex, OpenCode, an
agent with a skill, Drive, Converter, Google Drive, File Upload, Send Email,
plugins, Slack file operations, a sub-workflow chosen by an expression).

What the chart does for each worker:

- **Instance ID.** It's the pod name (`<release>-worker-0`, `-1`, …), unique and
  stable across restarts. A rescheduled worker rejoins its own row and keeps its
  weight.
- **No migrations.** Workers start uvicorn directly instead of the image
  entrypoint, which runs `alembic upgrade head`. They keep its key check
  (`python -m app.key_check`, Heym 0.0.149+), so a worker with keys that can't
  read the database stops at startup. A custom `cluster.command` should run it
  too. A worker whose version, schema
  or key digest differs from main's shows as **Mismatch** and gets no work, so
  an upgrade needs no ordering: workers stay idle until main has migrated.
- **Same secrets and Role as main.** Workers start sandbox pods too, and a
  worker with different keys would be a Mismatch.
- **Its own workspace claim** from `volumeClaimTemplates` (`cluster.persistence`).
  It only holds per-run Code node directories, so it is small, `ReadWriteOnce`
  is fine (a worker's sandboxes follow it to its node), and it is deleted with
  the worker. It can't be an `emptyDir`: sandboxes mount the workspace as a
  subPath of a claim.
- **Spread across nodes** (`cluster.spreadAcrossNodes`), and with them their
  sandboxes, which is most of the gain on `ReadWriteOnce` storage.
- **No Service and no ingress.** With `networkPolicy.enabled`, workers get the
  backend's egress, because leader duties (the scheduler, alerts, run recovery,
  the IMAP, RabbitMQ, WebSocket and Google listeners) can run on any instance.
  With `cni: cilium` they get their own API-server rule.

Worker startup and mounts can be configured with `cluster.command`,
`cluster.args`, `cluster.workingDir`, `cluster.extraVolumes` and
`cluster.extraVolumeMounts`. Backend startup and mounts are not inherited.
If your environment references a custom CA file, mount the trust inputs and
prepare that file in each worker before starting the API on port 10105.
Keep worker startup free of database migrations and frontend processes.
Compatibility patches needed by queued workflows must also be mounted on workers.

Every worker process holds its own database pool. Size `cluster.env` (for
example `HEYM_DB_POOL_SIZE`) against your database's connection limit.

**Scaling down.** Lower `cluster.replicas`; the highest-numbered workers go.
From Heym 0.0.145 the leader moves runs still queued for a removed worker to
another instance. Measured on Talos with one worker and a run every 2 seconds:
dispatch kept choosing the stopped worker until its heartbeat was 30 seconds
old (6 runs), and the leader moved all of them 25-45 seconds after they were
queued; none were lost ([heymrun/heym#832](https://github.com/heymrun/heym/issues/832#issuecomment-6629190912)).
If the worker held the leader lock, main takes it over within seconds. A run a
worker was executing when its pod stopped is still marked failed, not retried.
The removed worker's row stays under Settings → Instances as Offline; an admin
can delete it. On Heym 0.0.144 and earlier, set the departing workers' weight to
0 a minute first.

## Moving an existing install onto this chart

A Deployment's selector is immutable. For an Argo-managed plain-manifest install,
keep the same Application and resource names and set `selectorLabels` to the
**exact existing selector**, for example:

```yaml
fullnameOverride: heym
selectorLabels: {app: heym}
persistence: {existingClaim: heym-files}
secrets:
  generate: false
  existingSecrets: [heym-runtime, heym-database]
```

This preserves identity while Argo changes the rendering source. Use `envFrom`
for existing ConfigMaps and `command`, `args`, `workingDir`, `extraVolumes` and
`extraVolumeMounts` for required runtime/trust customization. `extraPorts` and
`service.extraPorts` preserve an API port used by provisioners; `service.portName`
can keep an existing Route target. Exactly one application container remains.

Do not change `selectorLabels` later. If you cannot preserve the selector or
reconciler ownership, choose a new `fullnameOverride` and plan a controlled cutover.
A native Helm install also requires explicitly adopting existing resource
ownership; a selector override alone does not transfer that ownership.

Keep the data PVC and important keys outside the release, preserve every existing
encryption key, and compare rendered object identities and Service selectors
before syncing. RWO storage and the single-replica Recreate strategy imply a
short outage. Keep sandbox policies in force during cutover; policies are
additive, so an old allow-all policy must not remain under another name.

## Values

The main ones; see [values.yaml](values.yaml) for all of them.

| Key                       | Default            | Purpose                                                                     |
| ------------------------- | ------------------ | --------------------------------------------------------------------------- |
| `platform`                | `generic`          | see above                                                                   |
| `cni`                     | `""`               | `cilium` for Cilium with kube-proxy replacement                             |
| `url`                     | `http://localhost:4017` | public URL (`FRONTEND_URL`, `CORS_ORIGINS`)                            |
| `secrets.existingSecrets` / `keyRefs` | `[]` / `{}` | split external sources and required per-key references |
| `secrets.generate` | `true` | durable create-once application keys when not fully supplied |
| `secrets.existingSecret`  | `""`               | Secret with `SECRET_KEY`, `ENCRYPTION_KEY`, `DATABASE_URL`                  |
| `image.digest`            | `""`               | pins the backend and, through it, every sandbox                             |
| `env`                     | `{}`               | extra Heym settings, e.g. `HEYM_ADMIN_EMAILS`                               |
| `persistence.accessModes` | `[ReadWriteOnce]`  | `ReadWriteMany` lets sandboxes leave the backend's node                     |
| `sandbox.podOverrides`    | `{}`               | `runtimeClassName`, `nodeSelector`, `tolerations` for sandbox pods          |
| `networkPolicy.enabled`   | `false`            | a policy for the backend too; add site-specific `extraEgress`               |
| `cluster.enabled` / `replicas` | `false` / `2` | load-distribution workers; see [Load distribution](#load-distribution-workers) |
| `kubernetesCredentials.enabled` / `namespace` | `false` / `""` | read-only credentials from labelled Secrets; see [Credentials from Kubernetes Secrets](#credentials-from-kubernetes-secrets) |
| `ingress` / `httpRoute` / `route` | off (Route on for OpenShift) | exposure                                              |

## Testing and configuration guardrails

`podLabels` may add labels or repeat a selector label with the same value. A
conflicting value fails rendering instead of producing a Deployment whose pod
does not match its selector. Labels that select sandbox NetworkPolicies
(`app.kubernetes.io/managed-by=heym` or `heym.run/network`) are rejected on the
backend. Native Helm's metadata label `app.kubernetes.io/managed-by=Helm` is fine.

`env` replaces defaults in place; each variable name appears once. Default entries
remain before extra variables so Kubernetes `$(VAR)` expansion keeps working.
When `sandbox.enabled=true`, `HEYM_SANDBOX_BACKEND` must remain `kubernetes`,
and pod scheduling overrides must use the schema-checked `sandbox.podOverrides`
map. An `env` entry and `secrets.keyRefs` for the same name are rejected. These
checks do not inspect Secret/ConfigMap contents; `envFrom` keeps its documented
source precedence. Disable `sandbox.enabled` explicitly for another runtime.

The Lint workflow runs semantic render tests, rejects duplicate YAML mapping
keys, and validates every platform/secret preset. The separate Lifecycle workflow
creates a disposable Kind cluster and exercises the actual candidate chart:

1. Install with a disposable pgvector database and generated application keys.
2. Store an encrypted database probe and a file on the chart's PVC.
3. Upgrade with a changed pod annotation, checking that a new pod uses the same
   keys and PVC and can still decrypt/read both probes.
4. Uninstall/reinstall the same release, checking retained key/PVC identities and
   successful application health and decryption.
5. Switch to pre-created split Secrets containing the same keys, then upgrade
   again and check that neither external nor unused generated keys changed.

The database role is unprivileged, with pgvector preinstalled administratively.
Passwords and key values stay in memory or test-cluster Secrets, never committed
values, command arguments or normal output. Failure logs are redacted using the
test Secrets. The default runner mode prints a plan and runs no commands:

```bash
python3 scripts/test-lifecycle.py --context kind-heym-chart-ci
# After approving this exact disposable target and its create/delete operations:
python3 scripts/test-lifecycle.py --context kind-heym-chart-ci \
  --namespace heym-chart-e2e --execute
```

Execution requires an explicit `kind-*` context with a loopback API server and a
new `heym-chart-e2e` namespace (or a suffixed name). It refuses an existing
namespace and only deletes one whose creation/ownership annotation matches this
process. CI also removes its entire disposable Kind cluster on completion.
The workflow uses Kubernetes 1.35 with a pinned node digest and has no production
cluster credentials or model-provider keys. Publishing a PR starts this CI test;
it does not deploy to the OpenShift lab or publish a chart release.

This is an install/upgrade test of the same candidate chart, not a cross-version
upgrade guarantee. It does not test Argo reconciliation, OpenShift SCC assignment,
NetworkPolicy enforcement, sandbox execution or LLM behavior. Sandbox support is
disabled because default Kind networking is not an enforcement test environment.
Keep the separate OpenShift/sandbox smoke checks before claiming that coverage.
Existing external-secret OpenShift adoption was verified in the lab; fresh
auto-generated-key OpenShift installation still needs a targeted live test.

## Publishing (maintainers)

1. Bump `version` and record the tested Heym release in `appVersion` in `Chart.yaml`. The default image follows `latest`; `appVersion` is also the empty-tag fallback.
2. Tag `chart-<version>`. CI pushes `oci://ghcr.io/amasolov/charts/heym`.
3. Artifact Hub reads the repository metadata from the OCI artifact
   `ghcr.io/amasolov/charts/heym:artifacthub.io`. The `Artifact Hub metadata`
   workflow pushes it whenever `artifacthub-repo.yml` changes.
