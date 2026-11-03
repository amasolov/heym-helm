# Heym Helm chart (community)

A community Helm chart for [Heym](https://heym.run) that runs every Heym sandbox
as its own restricted Kubernetes pod, with no Docker socket.

```bash
helm install heym oci://ghcr.io/amasolov/charts/heym -n heym --create-namespace -f values.private.yaml
```

Fresh installs can generate durable application keys; pre-created Secrets and
per-key references are also supported. Supply an external PostgreSQL connection
in your private values or through a Secret. See the install examples below.

- Chart documentation: [charts/heym/README.md](charts/heym/README.md)
- On Artifact Hub: https://artifacthub.io/packages/helm/heym/heym
- Releases: tag `chart-<version>` (see "Publishing" in the chart README)

Not an official Heym project. Chart licensed Apache-2.0.
