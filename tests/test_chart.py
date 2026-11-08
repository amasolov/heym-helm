"""Semantic render and key-lifecycle tests. No cluster access or real secrets."""

import base64
import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "keygen", ROOT / "charts/heym/files/generate-keys.py"
)
keygen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keygen)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML mapping keys instead of silently keeping the last."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key, _value in node.value:
            name = self.construct_object(key, deep=deep)
            if name in seen:
                raise AssertionError(f"Duplicate YAML mapping key: {name}")
            seen.add(name)
        return super().construct_mapping(node, deep=deep)


def render(values, success=True):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
        json.dump(values, file)
        file.flush()
        result = subprocess.run(
            [
                "helm",
                "template",
                "heym",
                str(ROOT / "charts/heym"),
                "-n",
                "heym",
                "-f",
                file.name,
            ],
            capture_output=True,
            text=True,
        )
    if not success:
        if result.returncode == 0:
            raise AssertionError("Invalid secret configuration was accepted")
        return result.stderr
    if result.returncode:
        raise AssertionError(result.stderr)
    return [d for d in yaml.load_all(result.stdout, Loader=UniqueKeyLoader) if d]


def deployment(docs):
    return next(d for d in docs if d["kind"] == "Deployment")


class ChartTests(unittest.TestCase):
    def test_image_selection_is_consistent_for_main_workers_and_key_hook(self):
        digest = "sha256:" + "a" * 64
        app_version = yaml.safe_load((ROOT / "charts/heym/Chart.yaml").read_text())[
            "appVersion"
        ]
        cases = [
            ({}, "latest", "Always"),
            ({"tag": "0.0.145"}, "0.0.145", "IfNotPresent"),
            ({"tag": "", "pullPolicy": ""}, app_version, "IfNotPresent"),
            ({"tag": "0.0.145", "digest": digest}, "0.0.145@" + digest, "IfNotPresent"),
            ({"digest": digest}, "latest@" + digest, "IfNotPresent"),
            ({"tag": "0.0.145@" + digest}, "0.0.145@" + digest, "IfNotPresent"),
            ({"pullPolicy": "Never"}, "latest", "Never"),
            ({"tag": "0.0.145", "pullPolicy": "Always"}, "0.0.145", "Always"),
        ]
        for image, reference, policy in cases:
            with self.subTest(image=image):
                docs = render(
                    dict(
                        image=image,
                        secrets=dict(existingSecrets=["database"]),
                        cluster=dict(enabled=True),
                    )
                )
                containers = [
                    d["spec"]["template"]["spec"]["containers"][0]
                    for d in docs
                    if d["kind"] in ("Deployment", "StatefulSet", "Job")
                ]
                self.assertEqual(len(containers), 3)
                for container in containers:
                    self.assertEqual(
                        container["image"], "ghcr.io/heymrun/heym:" + reference
                    )
                    self.assertEqual(container["imagePullPolicy"], policy)

    def test_selector_overrides_are_rejected_but_identical_labels_are_deduplicated(
        self,
    ):
        for selector, labels in (
            ({}, {"app.kubernetes.io/name": "different"}),
            ({"app": "heym"}, {"app": "different"}),
        ):
            error = render(
                dict(
                    secrets=dict(existingSecret="test"),
                    selectorLabels=selector,
                    podLabels=labels,
                ),
                success=False,
            )
            self.assertIn("must not override Deployment selector label", error)
        docs = render(
            dict(
                secrets=dict(existingSecret="test"),
                selectorLabels={"app": "heym"},
                podLabels={"app": "heym", "tier": "web"},
            )
        )
        pod_labels = deployment(docs)["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(pod_labels, {"app": "heym", "tier": "web"})

    def test_backend_cannot_match_sandbox_network_policies(self):
        for labels in (
            {"app.kubernetes.io/managed-by": "heym"},
            {"heym.run/network": "none"},
        ):
            error = render(
                dict(secrets=dict(existingSecret="test"), podLabels=labels),
                success=False,
            )
            self.assertIn("Backend labels must not select sandbox", error)

    def test_env_overrides_render_once_and_keep_default_before_custom_order(self):
        docs = render(
            dict(
                secrets=dict(existingSecret="test"),
                env={
                    "FRONTEND_URL": "https://custom.example.invalid",
                    "BACKEND_WORKERS": 3,
                    "HEYM_SANDBOX_BACKEND": "kubernetes",
                    "MY_URL": "$(FRONTEND_URL)",
                },
            )
        )
        env = deployment(docs)["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [item["name"] for item in env]
        self.assertEqual(len(names), len(set(names)))
        values = {item["name"]: item.get("value") for item in env}
        self.assertEqual(values["FRONTEND_URL"], "https://custom.example.invalid")
        self.assertEqual(values["BACKEND_WORKERS"], "3")
        self.assertLess(names.index("FRONTEND_URL"), names.index("MY_URL"))

    def test_sandbox_backend_and_typed_overrides_cannot_be_bypassed(self):
        for env in (
            {"HEYM_SANDBOX_BACKEND": "subprocess"},
            {"HEYM_SANDBOX_K8S_POD_OVERRIDES": "{}"},
        ):
            render(dict(secrets=dict(existingSecret="test"), env=env), success=False)
        docs = render(
            dict(
                secrets=dict(existingSecret="test"),
                sandbox=dict(enabled=False),
                env={"HEYM_SANDBOX_BACKEND": "docker"},
            )
        )
        env = deployment(docs)["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual(
            [e["value"] for e in env if e["name"] == "HEYM_SANDBOX_BACKEND"], ["docker"]
        )

    def test_key_reference_and_env_value_for_the_same_key_are_rejected(self):
        error = render(
            dict(
                secrets=dict(
                    existingSecret="test",
                    keyRefs={"ENCRYPTION_KEY": dict(name="external", key="key")},
                ),
                env={"ENCRYPTION_KEY": "test-only-placeholder"},
            ),
            success=False,
        )
        self.assertIn("either secrets.keyRefs or env, not both", error)

    def test_per_key_references_are_required_and_do_not_generate_replacements(self):
        refs = {
            key: dict(name="external", key=key.lower())
            for key in ("SECRET_KEY", "ENCRYPTION_KEY", "DATABASE_URL")
        }
        docs = render(dict(secrets=dict(keyRefs=refs)))
        self.assertFalse(any(d["kind"] in ("Secret", "Job") for d in docs))
        container = deployment(docs)["spec"]["template"]["spec"]["containers"][0]
        entries = {e["name"]: e for e in container["env"]}
        for key, ref in refs.items():
            self.assertEqual(
                entries[key]["valueFrom"]["secretKeyRef"], dict(ref, optional=False)
            )
        self.assertNotIn(
            "envFrom", container
        )  # No empty or nonexistent generated Secret reference.

    def test_precreated_split_secrets_are_references_only(self):
        docs = render(
            dict(
                platform="openshift",
                secrets=dict(generate=False, existingSecrets=["runtime", "database"]),
            )
        )
        self.assertFalse(any(d["kind"] in ("Secret", "Job") for d in docs))
        container = deployment(docs)["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["envFrom"],
            [
                dict(secretRef=dict(name="runtime")),
                dict(secretRef=dict(name="database")),
            ],
        )

    def test_single_existing_secret_keeps_backward_compatibility(self):
        docs = render(dict(secrets=dict(existingSecret="existing")))
        self.assertFalse(any(d["kind"] in ("Secret", "Job") for d in docs))
        account = next(d for d in docs if d["kind"] == "ServiceAccount")
        self.assertFalse(account["automountServiceAccountToken"])
        self.assertTrue(
            deployment(docs)["spec"]["template"]["spec"]["automountServiceAccountToken"]
        )
        container = deployment(docs)["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["envFrom"][0]["secretRef"]["name"], "existing")

    def test_generated_mode_is_deterministic_and_openshift_restricted(self):
        values = dict(
            platform="openshift",
            secrets=dict(databaseUrl="postgresql+asyncpg://example.invalid/heym"),
        )
        docs = render(values)
        self.assertEqual(docs, render(values))
        job = next(d for d in docs if d["kind"] == "Job")
        self.assertEqual(
            job["metadata"]["annotations"]["argocd.argoproj.io/hook"], "PreSync"
        )
        for item in (deployment(docs), job):
            pod = item["spec"]["template"]["spec"]
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            for key in ("runAsUser", "runAsGroup", "fsGroup"):
                self.assertNotIn(key, pod["securityContext"])
            security = pod["containers"][0]["securityContext"]
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertTrue(security["readOnlyRootFilesystem"])
            self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        secrets = [d for d in docs if d["kind"] == "Secret"]
        self.assertEqual(set(secrets[0]["stringData"]), {"DATABASE_URL"})
        role = next(
            d
            for d in docs
            if d["kind"] == "Role" and d["metadata"]["name"].endswith("keygen")
        )
        self.assertEqual([r["verbs"] for r in role["rules"]], [["get"], ["create"]])

    def test_important_precreated_key_can_override_generated_defaults(self):
        docs = render(
            dict(secrets=dict(existingSecrets=["important-encryption-key", "database"]))
        )
        container = deployment(docs)["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            [r["secretRef"]["name"] for r in container["envFrom"]],
            ["heym-generated-keys", "important-encryption-key", "database"],
        )
        self.assertFalse(any(d["kind"] == "Secret" for d in docs))

    def test_adoption_preserves_selector_pvc_ports_and_runtime(self):
        values = dict(
            platform="openshift",
            secrets=dict(existingSecret="runtime"),
            persistence=dict(existingClaim="existing-files"),
            selectorLabels=dict(app="heym"),
            command=["/bin/bash", "/opt/heym-code/start.sh"],
            workingDir="/app/backend",
            service=dict(
                portName="frontend",
                extraPorts=[dict(name="api", port=10105, targetPort="api")],
            ),
            extraPorts=[dict(name="api", containerPort=10105)],
            extraVolumes=[dict(name="code", configMap=dict(name="code"))],
            extraVolumeMounts=[
                dict(name="code", mountPath="/opt/heym-code", readOnly=True)
            ],
        )
        docs = render(values)
        deploy = deployment(docs)
        self.assertEqual(deploy["spec"]["selector"]["matchLabels"], {"app": "heym"})
        pod = deploy["spec"]["template"]["spec"]
        self.assertEqual(
            pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "existing-files"
        )
        self.assertFalse(any(d["kind"] == "PersistentVolumeClaim" for d in docs))
        self.assertEqual(pod["containers"][0]["command"], values["command"])
        route = next(d for d in docs if d["kind"] == "Route")
        self.assertEqual(route["spec"]["port"]["targetPort"], "frontend")
        policy = next(
            d
            for d in docs
            if d["kind"] == "NetworkPolicy"
            and d["metadata"]["name"].endswith("sandbox-egress")
        )
        self.assertNotIn({}, policy["spec"]["egress"])
        self.assertIn(
            {"port": 5353, "protocol": "UDP"}, policy["spec"]["egress"][0]["ports"]
        )

    def test_ambiguous_or_missing_secret_configuration_fails(self):
        render(
            dict(
                secrets=dict(
                    existingSecret="external", secretKey="test-only-placeholder"
                )
            ),
            success=False,
        )
        render(dict(secrets=dict(generate=False)), success=False)
        render({}, success=False)  # External database configuration is required.


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def env_map(container):
    return {e["name"]: e for e in container["env"]}


CLUSTER = dict(
    secrets=dict(generate=True, existingSecrets=["heym-db"]),
    cni="cilium",
    networkPolicy=dict(enabled=True),
    cluster=dict(enabled=True, replicas=2, env={"HEYM_DB_POOL_SIZE": "3"}),
)


class ClusterTests(unittest.TestCase):
    def test_disabled_by_default_renders_no_workers_or_cluster_env(self):
        docs = render(dict(secrets=dict(existingSecret="test")))
        self.assertFalse(by_kind(docs, "StatefulSet"))
        env = env_map(deployment(docs)["spec"]["template"]["spec"]["containers"][0])
        self.assertNotIn("HEYM_CLUSTER_ENABLED", env)
        self.assertNotIn("HEYM_INSTANCE_ROLE", env)

    def test_workers_get_stable_unique_ids_and_never_migrate(self):
        docs = render(CLUSTER)
        sts = by_kind(docs, "StatefulSet")[0]
        pod = sts["spec"]["template"]["spec"]
        self.assertEqual(sts["spec"]["replicas"], 2)
        self.assertEqual(len(pod["containers"]), 1)  # sandbox image discovery
        worker = pod["containers"][0]
        env = env_map(worker)
        self.assertEqual(env["HEYM_INSTANCE_ROLE"]["value"], "worker")
        self.assertEqual(env["HEYM_CLUSTER_ENABLED"]["value"], "true")
        for key in ("HEYM_INSTANCE_ID", "HEYM_INSTANCE_NAME"):
            self.assertEqual(
                env[key]["valueFrom"]["fieldRef"]["fieldPath"], "metadata.name"
            )
        self.assertEqual(env["HEYM_DB_POOL_SIZE"]["value"], "3")
        self.assertEqual(env["HEYM_SANDBOX_BACKEND"]["value"], "kubernetes")
        # The image entrypoint would run alembic; workers start uvicorn directly,
        # after the key check the entrypoint also runs (Heym 0.0.149+).
        command = " ".join(worker["command"])
        self.assertIn("uvicorn", command)
        self.assertNotIn("alembic", command)
        check, start = command.split("fi;")
        # Guarded: images before 0.0.149 have no app.key_check.
        self.assertIn("[ -f /app/backend/app/key_check.py ]", check)
        self.assertIn("-m app.key_check || exit 1", check)
        self.assertIn("exec /app/backend/.venv/bin/python -m uvicorn", start)
        self.assertIn("--workers 2", start)
        main = env_map(deployment(docs)["spec"]["template"]["spec"]["containers"][0])
        self.assertEqual(main["HEYM_INSTANCE_ROLE"]["value"], "main")
        self.assertNotIn("HEYM_INSTANCE_ID", main)
        self.assertNotIn("HEYM_DB_POOL_SIZE", main)

    def test_openshift_workers_support_trust_startup_without_backend_command(self):
        values = copy.deepcopy(CLUSTER)
        values.update(platform="openshift", cni="", command=["backend-only"])
        values["cluster"].update(
            command=["/bin/bash", "/opt/runtime/worker.sh"],
            args=["--example"],
            workingDir="/app/backend",
            extraVolumes=[dict(name="trust", configMap=dict(name="corporate-ca"))],
            extraVolumeMounts=[
                dict(name="trust", mountPath="/opt/runtime", readOnly=True)
            ],
        )
        docs = render(values)
        pod = by_kind(docs, "StatefulSet")[0]["spec"]["template"]["spec"]
        worker = pod["containers"][0]
        self.assertEqual(worker["command"], values["cluster"]["command"])
        self.assertEqual(worker["args"], ["--example"])
        self.assertIn(values["cluster"]["extraVolumes"][0], pod["volumes"])
        self.assertIn(values["cluster"]["extraVolumeMounts"][0], worker["volumeMounts"])
        for field in ("runAsUser", "runAsGroup", "fsGroup"):
            self.assertNotIn(field, pod["securityContext"])
        self.assertTrue(worker["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(worker["securityContext"]["allowPrivilegeEscalation"])
        policy = next(
            p
            for p in by_kind(docs, "NetworkPolicy")
            if p["metadata"]["name"] == "heym-worker"
        )
        dns = policy["spec"]["egress"][0]
        self.assertEqual(
            dns["to"][0]["namespaceSelector"]["matchLabels"],
            {"kubernetes.io/metadata.name": "openshift-dns"},
        )
        self.assertIn(dict(port=5353, protocol="UDP"), dns["ports"])

    def test_workers_share_secret_sources_and_service_account(self):
        docs = render(CLUSTER)
        main = deployment(docs)["spec"]["template"]["spec"]
        worker = by_kind(docs, "StatefulSet")[0]["spec"]["template"]["spec"]
        self.assertEqual(
            main["containers"][0]["envFrom"], worker["containers"][0]["envFrom"]
        )
        self.assertEqual(main["serviceAccountName"], worker["serviceAccountName"])
        self.assertTrue(worker["automountServiceAccountToken"])

    def test_workspace_is_a_claim_per_worker_not_an_emptydir(self):
        sts = by_kind(render(CLUSTER), "StatefulSet")[0]
        mounts = {
            m["mountPath"]: m["name"]
            for m in sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        }
        for claim in sts["spec"]["volumeClaimTemplates"]:
            # Kubernetes adds these fields; Argo preserves them in its normalized
            # live state, so omitting them causes perpetual OutOfSync.
            self.assertEqual(claim["apiVersion"], "v1")
            self.assertEqual(claim["kind"], "PersistentVolumeClaim")
        claims = [t["metadata"]["name"] for t in sts["spec"]["volumeClaimTemplates"]]
        self.assertIn(mounts["/app/data"], claims)
        volumes = {v["name"] for v in sts["spec"]["template"]["spec"]["volumes"]}
        self.assertNotIn(mounts["/app/data"], volumes)

    def test_service_and_backend_policy_never_select_workers(self):
        docs = render(CLUSTER)
        labels = by_kind(docs, "StatefulSet")[0]["spec"]["template"]["metadata"][
            "labels"
        ]
        service = by_kind(docs, "Service")[0]["spec"]["selector"]
        self.assertFalse(service.items() <= labels.items())
        policies = {
            p["metadata"]["name"]: p["spec"] for p in by_kind(docs, "NetworkPolicy")
        }
        backend = policies["heym"]["podSelector"]["matchLabels"]
        self.assertFalse(backend.items() <= labels.items())
        worker = policies["heym-worker"]
        self.assertTrue(worker["podSelector"]["matchLabels"].items() <= labels.items())
        self.assertEqual(worker["ingress"], [])
        self.assertEqual(worker["egress"], policies["heym"]["egress"])

    def test_cilium_api_server_rule_covers_workers(self):
        docs = render(CLUSTER)
        labels = by_kind(docs, "StatefulSet")[0]["spec"]["template"]["metadata"][
            "labels"
        ]
        selectors = [
            c["spec"]["endpointSelector"]["matchLabels"]
            for c in by_kind(docs, "CiliumNetworkPolicy")
        ]
        self.assertTrue(any(s.items() <= labels.items() for s in selectors))

    def test_reserved_cluster_env_and_overlapping_labels_are_rejected(self):
        for key in ("HEYM_INSTANCE_ID", "HEYM_INSTANCE_ROLE", "HEYM_CLUSTER_ENABLED"):
            for where in ("env", "cluster"):
                values = copy.deepcopy(CLUSTER)
                if where == "env":
                    values["env"] = {key: "x"}
                else:
                    values["cluster"]["env"] = {key: "x"}
                error = render(values, success=False)
                self.assertIn(key, error)
        values = copy.deepcopy(CLUSTER)
        values["selectorLabels"] = {"app": "heym"}
        values["cluster"]["podLabels"] = {"app": "heym"}
        self.assertIn("Service would route to workers", render(values, success=False))
        values = copy.deepcopy(CLUSTER)
        values["cluster"]["podLabels"] = {"heym.run/network": "egress"}
        self.assertIn("sandbox", render(values, success=False))


class KeyGenerationTests(unittest.TestCase):
    def test_kubernetes_credentials_are_off_by_default(self):
        docs = render(CLUSTER)
        self.assertFalse(
            [r for r in by_kind(docs, "Role") if r["metadata"]["name"].endswith("-credentials")]
        )
        for pod in (deployment(docs), by_kind(docs, "StatefulSet")[0]):
            env = env_map(pod["spec"]["template"]["spec"]["containers"][0])
            self.assertNotIn("HEYM_K8S_CREDENTIALS", env)

    def test_kubernetes_credentials_role_env_and_token_for_main_and_workers(self):
        values = copy.deepcopy(CLUSTER)
        values["kubernetesCredentials"] = dict(enabled=True)
        # Without sandboxes, the token and API-server rule are still needed.
        values["sandbox"] = dict(enabled=False)
        docs = render(values)
        role = next(r for r in by_kind(docs, "Role") if r["metadata"]["name"] == "heym-credentials")
        self.assertEqual(role["metadata"]["namespace"], "heym")
        self.assertEqual(
            role["rules"],
            [dict(apiGroups=[""], resources=["secrets"], verbs=["get", "list", "watch"])],
        )
        binding = next(
            b for b in by_kind(docs, "RoleBinding") if b["metadata"]["name"] == "heym-credentials"
        )
        self.assertEqual(
            binding["subjects"], [dict(kind="ServiceAccount", name="heym", namespace="heym")]
        )
        for pod in (deployment(docs), by_kind(docs, "StatefulSet")[0]):
            spec = pod["spec"]["template"]["spec"]
            self.assertTrue(spec["automountServiceAccountToken"])
            self.assertEqual(spec["serviceAccountName"], "heym")
            env = env_map(spec["containers"][0])
            self.assertEqual(env["HEYM_K8S_CREDENTIALS"]["value"], "true")
            self.assertNotIn("HEYM_K8S_CREDENTIALS_NAMESPACE", env)
        # No sandbox Role, but both API-server policies remain (cni: cilium).
        self.assertFalse(
            [r for r in by_kind(docs, "Role") if r["metadata"]["name"] == "heym-sandbox"]
        )
        self.assertEqual(
            sorted(p["metadata"]["name"] for p in by_kind(docs, "CiliumNetworkPolicy")),
            ["heym-kube-apiserver", "heym-worker-kube-apiserver"],
        )

    def test_kubernetes_credentials_in_another_namespace(self):
        values = dict(
            secrets=dict(existingSecret="test"),
            kubernetesCredentials=dict(enabled=True, namespace="heym-credentials"),
        )
        docs = render(values)
        for kind in ("Role", "RoleBinding"):
            obj = next(o for o in by_kind(docs, kind) if o["metadata"]["name"] == "heym-credentials")
            self.assertEqual(obj["metadata"]["namespace"], "heym-credentials")
        binding = next(
            b for b in by_kind(docs, "RoleBinding") if b["metadata"]["name"] == "heym-credentials"
        )
        self.assertEqual(binding["subjects"][0]["namespace"], "heym")
        env = env_map(deployment(docs)["spec"]["template"]["spec"]["containers"][0])
        self.assertEqual(env["HEYM_K8S_CREDENTIALS_NAMESPACE"]["value"], "heym-credentials")

    def test_kubernetes_credentials_refuse_chart_owned_env_and_subprocess_sandboxes(self):
        base = dict(secrets=dict(existingSecret="test"), kubernetesCredentials=dict(enabled=True))
        for env, worker_env, expected in (
            ({"HEYM_K8S_CREDENTIALS": "true"}, {}, "set by the chart"),
            ({}, {"HEYM_K8S_CREDENTIALS_NAMESPACE": "x"}, "set by the chart"),
            ({"HEYM_MCP_STDIO_SANDBOX": " Subprocess "}, {}, "HEYM_MCP_STDIO_SANDBOX=subprocess"),
            ({}, {"HEYM_PYTHON_TOOL_SANDBOX": "subprocess"}, "HEYM_PYTHON_TOOL_SANDBOX=subprocess"),
        ):
            with self.subTest(env=env, worker_env=worker_env):
                values = copy.deepcopy(base)
                values["env"] = env
                values["cluster"] = dict(enabled=True, env=worker_env)
                self.assertIn(expected, render(values, success=False))
        # Without the feature, a subprocess sandbox is the operator's choice.
        render(dict(secrets=dict(existingSecret="test"), env={"HEYM_MCP_STDIO_SANDBOX": "subprocess"}))

    def test_key_refs_can_be_optional_for_rotation_keys(self):
        docs = render(
            dict(
                secrets=dict(
                    existingSecret="test",
                    keyRefs=dict(
                        ENCRYPTION_KEY=dict(name="new-key", key="key"),
                        ENCRYPTION_KEYS_EXTRA=dict(name="old-key", key="key", optional=True),
                    ),
                ),
                cluster=dict(enabled=True),
            )
        )
        for pod in (deployment(docs), by_kind(docs, "StatefulSet")[0]):
            env = env_map(pod["spec"]["template"]["spec"]["containers"][0])
            self.assertEqual(
                env["ENCRYPTION_KEY"]["valueFrom"]["secretKeyRef"],
                dict(name="new-key", key="key", optional=False),
            )
            self.assertEqual(
                env["ENCRYPTION_KEYS_EXTRA"]["valueFrom"]["secretKeyRef"],
                dict(name="old-key", key="key", optional=True),
            )

    def test_keys_are_created_once_and_never_rotated(self):
        stored = {}
        writes = []

        def request(method, path, payload=None):
            if method == "GET":
                return stored.get("secret")
            writes.append(copy.deepcopy(payload))
            stored["secret"] = payload
            return payload

        keygen.ensure_keys(request, "test", "test")
        original = copy.deepcopy(stored)
        keygen.ensure_keys(request, "test", "test")
        self.assertEqual(len(writes), 1)
        self.assertEqual(stored, original)
        encoded = base64.b64decode(stored["secret"]["data"]["ENCRYPTION_KEY"])
        self.assertEqual(len(base64.urlsafe_b64decode(encoded)), 32)

    def test_concurrent_creation_adopts_the_winner_without_update(self):
        winner = dict(
            data=dict(SECRET_KEY="test-placeholder", ENCRYPTION_KEY="test-placeholder")
        )
        calls = []

        def request(method, path, payload=None):
            calls.append(method)
            return winner if len(calls) == 3 else None

        keygen.ensure_keys(request, "test", "test")
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_incomplete_existing_secret_fails_without_writes(self):
        def request(method, path, payload=None):
            self.assertEqual(method, "GET")
            return dict(data=dict(SECRET_KEY="test-placeholder"))

        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            keygen.ensure_keys(request, "test", "test")


if __name__ == "__main__":
    unittest.main()
