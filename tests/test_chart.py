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


class KeyGenerationTests(unittest.TestCase):
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
