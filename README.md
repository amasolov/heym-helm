# Heym Helm chart (community)

A community Helm chart for [Heym](https://heym.run) that runs every Heym sandbox
as its own restricted Kubernetes pod, with no Docker socket.

```bash
helm install heym oci://ghcr.io/amasolov/charts/heym -n heym --set secrets.existingSecret=heym-secret
```

- Chart documentation: [charts/heym/README.md](charts/heym/README.md)
- On Artifact Hub: https://artifacthub.io/packages/helm/heym/heym
- Releases: tag `chart-<version>` (see "Publishing" in the chart README)

Not an official Heym project. Chart licensed Apache-2.0.
