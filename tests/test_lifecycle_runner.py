"""Ensure lifecycle test plans cannot accidentally mutate a non-test cluster."""

import base64
import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "lifecycle", ROOT / "scripts/test-lifecycle.py"
)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


class LifecycleRunnerTests(unittest.TestCase):
    def runner(self):
        return lifecycle.Lifecycle(
            "kind-chart-test", "heym-chart-e2e", ROOT / "charts/heym"
        )

    def test_default_plan_runs_no_commands(self):
        output = io.StringIO()
        with (
            patch("sys.argv", ["test-lifecycle.py", "--context", "kind-chart-test"]),
            patch("subprocess.run") as command,
            contextlib.redirect_stdout(output),
        ):
            lifecycle.main()
        command.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["model_calls"], 0)
        self.assertFalse(plan["production_access"])

    def test_production_context_and_namespace_are_rejected(self):
        for context, namespace in (
            ("admin", "heym-chart-e2e"),
            ("kind-test", "heym"),
            ("kind-test", "default"),
            ("kind-test", "heym-chart-e2e;echo"),
        ):
            with self.assertRaises(ValueError):
                lifecycle.validate_target(context, namespace)

    def test_remote_api_is_rejected_even_when_context_is_named_kind(self):
        runner = self.runner()
        runner.kube = Mock(
            return_value=SimpleNamespace(
                stdout=json.dumps(
                    {
                        "clusters": [
                            {
                                "cluster": {
                                    "server": "https://api.production.invalid:6443"
                                }
                            }
                        ]
                    }
                )
            )
        )
        with self.assertRaisesRegex(ValueError, "non-loopback"):
            runner.create_namespace()
        self.assertFalse(runner.created)
        self.assertEqual(runner.kube.call_count, 1)

    def test_existing_namespace_is_never_reused_or_deleted(self):
        runner = self.runner()
        runner.kube = Mock(
            side_effect=[
                SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "clusters": [
                                {"cluster": {"server": "https://127.0.0.1:6443"}}
                            ]
                        }
                    )
                ),
                SimpleNamespace(stdout="namespace/heym-chart-e2e"),
            ]
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            runner.create_namespace()
        runner.cleanup()
        self.assertFalse(runner.created)
        self.assertEqual(runner.kube.call_count, 2)

    def test_cleanup_requires_matching_process_ownership(self):
        runner = self.runner()
        runner.created = True
        runner.kube = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"metadata": {"annotations": {lifecycle.OWNER: "another-process"}}}
                ),
            )
        )
        runner.cleanup()
        self.assertEqual(runner.kube.call_count, 1)
        self.assertEqual(runner.kube.call_args.args[0], "get")

    def test_key_identity_detects_both_rotation_and_recreation(self):
        secret = dict(
            metadata=dict(uid="original"),
            data={
                name: base64.b64encode(b"test-only-fixture").decode()
                for name in ("SECRET_KEY", "ENCRYPTION_KEY")
            },
        )
        identity = lifecycle.key_identity(secret)
        secret["metadata"]["uid"] = "replacement"
        self.assertNotEqual(identity, lifecycle.key_identity(secret))
        secret["metadata"]["uid"] = "original"
        secret["data"]["SECRET_KEY"] = base64.b64encode(
            b"different-test-fixture"
        ).decode()
        self.assertNotEqual(identity, lifecycle.key_identity(secret))

    def test_private_values_are_references_only_and_inference_is_disabled(self):
        for external in (False, True):
            values = lifecycle.test_values("test", external)
            self.assertFalse(values["sandbox"]["enabled"])
            self.assertEqual(values["env"]["ALLOW_REGISTER"], "false")
            self.assertNotIn("databaseUrl", values["secrets"])
            self.assertNotIn("secretKey", values["secrets"])
            self.assertNotIn("encryptionKey", values["secrets"])


if __name__ == "__main__":
    unittest.main()
