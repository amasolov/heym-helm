#!/usr/bin/env python3
"""Exercise real Helm/key/PVC lifecycle in a disposable Kind cluster, without LLMs.

Defaults to a read-only plan. Execution requires --execute, a Kind context with
a loopback API server, and an absent heym-chart-e2e namespace. Never use real keys.
"""

import argparse
import base64
import hashlib
import json
import re
import secrets
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "heym-lifecycle"
DB = "heym-lifecycle-db"
DB_SECRET = "heym-lifecycle-database"
EXTERNAL = "heym-lifecycle-external"
OWNER = "tests.heym.run/owner"
PG_IMAGE = "pgvector/pgvector:0.8.7-pg16"

PROBE_WRITE = """from pathlib import Path
from sqlalchemy import text
from app.db.session import sync_engine
from app.services.encryption import encrypt_config
Path('/app/data/chart-lifecycle-probe').write_text('chart-lifecycle')
with sync_engine.begin() as db:
    db.execute(text('CREATE TABLE chart_lifecycle_probe (id integer PRIMARY KEY, payload text NOT NULL)'))
    db.execute(text('INSERT INTO chart_lifecycle_probe VALUES (1, :payload)'),
               {'payload': encrypt_config({'marker': 'chart-lifecycle'})})
print('Persistent file and encrypted database probe created')
"""
PROBE_READ = """from pathlib import Path
from sqlalchemy import text
from app.db.session import sync_engine
from app.services.encryption import decrypt_config
assert Path('/app/data/chart-lifecycle-probe').read_text() == 'chart-lifecycle'
with sync_engine.connect() as db:
    payload = db.execute(text('SELECT payload FROM chart_lifecycle_probe WHERE id=1')).scalar_one()
assert decrypt_config(payload) == {'marker': 'chart-lifecycle'}
print('Persistent file and encrypted database probe verified')
"""
HEALTH = """import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:4017/api/health', timeout=15) as r:
    assert r.status == 200 and json.load(r)['status'] == 'healthy'
print('Application health passed')
"""
WORKER = RELEASE + "-worker"
WORKER_HEALTH = """import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:10105/api/health', timeout=15) as r:
    assert r.status == 200 and json.load(r)['status'] == 'healthy'
print('Worker health passed')
"""
CLUSTER_ROWS = """import json
from sqlalchemy import text
from app.db.session import sync_engine
with sync_engine.begin() as db:
    rows = db.execute(text(
        'SELECT id, role, version, schema_revision, keys_fingerprint, '
        'extract(epoch from now() - heartbeat_at) FROM cluster_instances'
    )).all()
print(json.dumps([[str(c) for c in row] for row in rows]))
"""


def validate_target(context, namespace):
    if not re.fullmatch(r"kind-[a-z0-9][a-z0-9-]*", context):
        raise ValueError("Use an explicit disposable kind-* context")
    if (
        not re.fullmatch(r"heym-chart-e2e(?:-[a-z0-9]+)*", namespace)
        or len(namespace) > 63
    ):
        raise ValueError("Namespace must be heym-chart-e2e or heym-chart-e2e-<suffix>")


def key_identity(secret):
    """Compare UID and data in memory, never emit key values or fingerprints."""
    data = secret["data"]
    for key in ("SECRET_KEY", "ENCRYPTION_KEY"):
        if not base64.b64decode(data[key], validate=True):
            raise ValueError("Application key is missing or empty")
    return secret["metadata"]["uid"], hashlib.sha256(
        json.dumps(data, sort_keys=True).encode()
    ).digest()


def test_values(phase, external=False, workers=0):
    values = {
        "fullnameOverride": RELEASE,
        "platform": "generic",
        "workers": 1,
        "sandbox": {"enabled": False},
        "secrets": {
            "generate": not external,
            "existingSecrets": ([EXTERNAL] if external else []) + [DB_SECRET],
        },
        "persistence": {
            "size": "1Gi",
            "existingClaim": RELEASE + "-data" if external else "",
        },
        "shmSize": "256Mi",
        "podAnnotations": {"tests.heym.run/phase": phase},
        "env": {
            "ALLOW_REGISTER": "false",
            "PLAYWRIGHT_INSTALL_AT_STARTUP": "false",
            "HEYM_OTEL_ENABLED": "false",
            "DOCKER_LOGS_ENABLED": "false",
        },
    }
    if workers:
        values["cluster"] = {
            "enabled": True,
            "replicas": workers,
            "processes": 1,
            "persistence": {"size": "256Mi"},
            "resources": {"requests": {"cpu": "50m", "memory": "256Mi"}},
            "terminationGracePeriodSeconds": 10,
            # Stay well inside the disposable database's 100 connections.
            "env": {
                "HEYM_DB_POOL_SIZE": "2",
                "HEYM_DB_MAX_OVERFLOW": "2",
                "HEYM_DB_SYNC_POOL_SIZE": "1",
                "HEYM_DB_SYNC_MAX_OVERFLOW": "1",
            },
        }
    return values


class Lifecycle:
    def __init__(self, context, namespace, chart):
        validate_target(context, namespace)
        self.context, self.namespace, self.chart = context, namespace, chart
        self.owner = uuid.uuid4().hex
        self.created = False
        self.sensitive = set()

    def command(self, args, data=None, check=True, timeout=900):
        result = subprocess.run(
            args, input=data, capture_output=True, text=True, timeout=timeout
        )
        if check and result.returncode:
            # Commands may process Secret/SQL data. Report only safe summaries;
            # diagnostics below collect known keys before printing redacted logs.
            raise RuntimeError(
                f"{Path(args[0]).name} operation failed (exit {result.returncode})"
            )
        return result

    def kube(self, *args, data=None, check=True, namespace=True):
        command = ["kubectl", "--context", self.context]
        if namespace:
            command += ["--namespace", self.namespace]
        return self.command(command + list(args), data=data, check=check)

    def get(self, kind, name):
        return json.loads(self.kube("get", kind, name, "-o", "json").stdout)

    def apply(self, manifest):
        self.kube("apply", "-f", "-", data=json.dumps(manifest))

    def remember(self, value):
        self.sensitive.add(value)
        self.sensitive.add(base64.b64encode(value.encode()).decode())

    def redact(self, value):
        for secret in sorted(self.sensitive, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def secret(self, name):
        secret = self.get("secret", name)
        for value in secret.get("data", {}).values():
            self.sensitive.add(value)
            self.remember(base64.b64decode(value).decode())
        return secret

    def create_namespace(self):
        config = json.loads(
            self.kube(
                "config", "view", "--minify", "-o", "json", namespace=False
            ).stdout
        )
        host = urllib.parse.urlparse(
            config["clusters"][0]["cluster"]["server"]
        ).hostname
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Refusing a non-loopback Kubernetes API server")
        existing = self.kube(
            "get",
            "namespace",
            self.namespace,
            "--ignore-not-found",
            "-o",
            "name",
            namespace=False,
        )
        if existing.stdout.strip():
            raise ValueError(
                "Test namespace already exists; refusing to reuse or delete it"
            )
        self.kube(
            "create",
            "-f",
            "-",
            namespace=False,
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": self.namespace,
                        "annotations": {OWNER: self.owner},
                        "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
                    },
                }
            ),
        )
        self.created = True

    def setup_database(self):
        admin_password, app_password = secrets.token_hex(32), secrets.token_hex(32)
        self.remember(admin_password)
        self.remember(app_password)
        url = f"postgresql+asyncpg://heym:{app_password}@{DB}:5432/heym"
        self.remember(url)
        for name, data in (
            (DB + "-admin", {"password": admin_password}),
            (DB_SECRET, {"DATABASE_URL": url}),
        ):
            self.apply(
                dict(
                    apiVersion="v1",
                    kind="Secret",
                    metadata=dict(name=name),
                    stringData=data,
                )
            )
        self.apply(
            dict(
                apiVersion="v1",
                kind="Service",
                metadata=dict(name=DB),
                spec=dict(
                    selector={"app": DB}, ports=[dict(port=5432, targetPort=5432)]
                ),
            )
        )
        self.apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": DB},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": DB}},
                    "template": {
                        "metadata": {"labels": {"app": DB}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 999,
                                "runAsGroup": 999,
                                "fsGroup": 999,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "postgres",
                                    "image": PG_IMAGE,
                                    "env": [
                                        {
                                            "name": "POSTGRES_PASSWORD",
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "name": DB + "-admin",
                                                    "key": "password",
                                                }
                                            },
                                        },
                                        {
                                            "name": "PGDATA",
                                            "value": "/var/lib/postgresql/data/pgdata",
                                        },
                                    ],
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "256Mi"},
                                        "limits": {"cpu": "1", "memory": "512Mi"},
                                    },
                                    "readinessProbe": {
                                        "exec": {
                                            "command": [
                                                "pg_isready",
                                                "-h",
                                                "127.0.0.1",
                                                "-U",
                                                "postgres",
                                            ]
                                        },
                                        "periodSeconds": 2,
                                    },
                                    "volumeMounts": [
                                        {
                                            "name": "data",
                                            "mountPath": "/var/lib/postgresql/data",
                                        },
                                        {"name": "tmp", "mountPath": "/tmp"},
                                        {
                                            "name": "run",
                                            "mountPath": "/var/run/postgresql",
                                        },
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": name, "emptyDir": {}}
                                for name in ("data", "tmp", "run")
                            ],
                        },
                    },
                },
            }
        )
        self.kube("rollout", "status", "deployment/" + DB, "--timeout=240s")
        # Passwords enter through stdin, never CLI arguments, files or logs.
        sql = (
            f"CREATE ROLE heym LOGIN PASSWORD '{app_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
            "CREATE DATABASE heym OWNER heym;\n\\connect heym\nCREATE EXTENSION vector;\n"
        )
        self.kube(
            "exec",
            "-i",
            "deployment/" + DB,
            "--",
            "psql",
            "-q",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            data=sql,
        )

    def helm(self, action, values=None):
        command = [
            "helm",
            action,
            RELEASE,
            "--kube-context",
            self.context,
            "--namespace",
            self.namespace,
            "--wait",
            "--timeout",
            "10m",
        ]
        if values is None:
            self.command(command)
            return
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
            json.dump(values, file)
            file.flush()
            self.command(command + [str(self.chart), "-f", file.name])

    def backend(self, code):
        return self.kube(
            "exec",
            "deployment/" + RELEASE,
            "-c",
            "heym",
            "--",
            "/app/backend/.venv/bin/python",
            "-c",
            code,
        )

    def worker(self, index, code):
        return self.kube(
            "exec",
            f"{WORKER}-{index}",
            "-c",
            "heym",
            "--",
            "/app/backend/.venv/bin/python",
            "-c",
            code,
        )

    def verify_workers(self, count):
        """Each worker is healthy, holds its own claim and joined under its
        pod name with main's version, schema and key digest; removed workers
        lose their claim."""
        for index in range(count):
            self.worker(index, WORKER_HEALTH)
            pvc = self.get("pvc", f"data-{WORKER}-{index}")
            if pvc["status"]["phase"] != "Bound":
                raise RuntimeError(f"Worker {index} workspace claim is not bound")
        wanted = {f"{WORKER}-{index}" for index in range(count)}
        deadline = time.monotonic() + 120
        while True:
            rows = json.loads(self.backend(CLUSTER_ROWS).stdout.strip().splitlines()[-1])
            main = [r for r in rows if r[1] == "main"]
            live = {
                r[0]: r for r in rows if r[1] == "worker" and float(r[5]) < 60
            }
            if main and wanted <= live.keys():
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Workers did not join the cluster under their pod names")
            time.sleep(5)
        for name in wanted:
            if live[name][2:5] != main[0][2:5]:
                raise RuntimeError(f"{name} differs from main (version, schema or keys)")
        deadline = time.monotonic() + 120
        while True:
            claims = json.loads(self.kube("get", "pvc", "-o", "json").stdout)["items"]
            stale = [
                c["metadata"]["name"]
                for c in claims
                if c["metadata"]["name"].startswith(f"data-{WORKER}-")
                and int(c["metadata"]["name"].rsplit("-", 1)[1]) >= count
                and not c["metadata"].get("deletionTimestamp")
            ]
            if not stale:
                return
            if time.monotonic() > deadline:
                raise RuntimeError("Scaled-down worker claims were not deleted")
            time.sleep(5)

    def pod_uid(self):
        pods = json.loads(
            self.kube(
                "get",
                "pods",
                "-l",
                f"app.kubernetes.io/name=heym,app.kubernetes.io/instance={RELEASE}",
                "-o",
                "json",
            ).stdout
        )
        uids = [
            p["metadata"]["uid"]
            for p in pods["items"]
            if not p["metadata"].get("deletionTimestamp")
        ]
        if len(uids) != 1:
            raise RuntimeError("Expected exactly one application pod")
        return uids[0]

    def verify(self, key_name, identity, pvc_uid):
        if key_identity(self.secret(key_name)) != identity:
            raise RuntimeError("Application keys changed during lifecycle test")
        pvc = self.get("pvc", RELEASE + "-data")
        if pvc["metadata"]["uid"] != pvc_uid or pvc["status"]["phase"] != "Bound":
            raise RuntimeError("Application PVC changed or lost its binding")
        self.backend(HEALTH)
        self.backend(PROBE_READ)

    def run(self):
        self.create_namespace()
        print("Created isolated test namespace")
        self.setup_database()
        print(
            "Prepared disposable pgvector database with an unprivileged application role"
        )
        self.helm("install", test_values("install"))
        self.backend(HEALTH)
        self.backend(PROBE_WRITE)
        generated = RELEASE + "-generated-keys"
        original = self.secret(generated)
        identity = key_identity(original)
        pvc_uid = self.get("pvc", RELEASE + "-data")["metadata"]["uid"]
        pod_uid = self.pod_uid()
        self.helm("upgrade", test_values("upgrade"))
        self.verify(generated, identity, pvc_uid)
        if self.pod_uid() == pod_uid:
            raise RuntimeError("Upgrade did not exercise a new application pod")
        print(
            "Install/upgrade preserved generated keys, encrypted DB data and PVC file"
        )
        self.helm("uninstall")
        if (
            key_identity(self.secret(generated)) != identity
            or self.get("pvc", RELEASE + "-data")["metadata"]["uid"] != pvc_uid
        ):
            raise RuntimeError("Uninstall removed retained keys or PVC")
        self.helm("install", test_values("reinstall"))
        self.verify(generated, identity, pvc_uid)
        print(
            "Uninstall/reinstall retained keys and recovered encrypted DB data and PVC file"
        )
        self.helm("uninstall")
        self.apply(
            dict(
                apiVersion="v1",
                kind="Secret",
                metadata=dict(name=EXTERNAL),
                data=original["data"],
            )
        )
        external_identity = key_identity(self.secret(EXTERNAL))
        self.helm("install", test_values("external-install", external=True))
        self.verify(EXTERNAL, external_identity, pvc_uid)
        self.helm("upgrade", test_values("external-upgrade", external=True))
        self.verify(EXTERNAL, external_identity, pvc_uid)
        if key_identity(self.secret(generated)) != identity:
            raise RuntimeError("External mode modified the unused generated-key Secret")
        print(
            "Pre-created Secret mode preserved external keys and decrypted the original data"
        )
        self.helm("upgrade", test_values("cluster", external=True, workers=2))
        self.verify(EXTERNAL, external_identity, pvc_uid)
        self.verify_workers(2)
        self.helm("upgrade", test_values("cluster-scaled", external=True, workers=1))
        self.verify_workers(1)
        print(
            "Two workers joined under their pod names with main's keys and schema; "
            "scaling to one removed the other's claim"
        )

    def owns_namespace(self):
        if not self.created:
            return False
        result = self.kube(
            "get",
            "namespace",
            self.namespace,
            "-o",
            "json",
            namespace=False,
            check=False,
        )
        return (
            result.returncode == 0
            and json.loads(result.stdout)["metadata"].get("annotations", {}).get(OWNER)
            == self.owner
        )

    def diagnostics(self):
        if not self.owns_namespace():
            return
        for name in (RELEASE + "-generated-keys", EXTERNAL, DB_SECRET, DB + "-admin"):
            try:
                self.secret(name)
            except Exception:
                pass
        for args in (
            ("get", "pods"),
            ("get", "events", "--sort-by=.lastTimestamp"),
            ("logs", "deployment/" + RELEASE, "--tail=80"),
            ("logs", "deployment/" + DB, "--tail=80"),
            ("logs", "statefulset/" + WORKER, "--tail=80"),
        ):
            result = self.kube(*args, check=False)
            print(self.redact(result.stdout + result.stderr))

    def cleanup(self):
        if self.owns_namespace():
            self.kube(
                "delete",
                "namespace",
                self.namespace,
                "--wait=true",
                "--timeout=180s",
                namespace=False,
            )
            print("Removed only the test-owned namespace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="heym-chart-e2e")
    parser.add_argument("--chart", type=Path, default=ROOT / "charts/heym")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create/delete test resources; otherwise print a plan only",
    )
    args = parser.parse_args()
    validate_target(args.context, args.namespace)
    if not args.execute:
        print(
            json.dumps(
                {
                    "context": args.context,
                    "namespace": args.namespace,
                    "chart": str(args.chart),
                    "release": RELEASE,
                    "database_image": PG_IMAGE,
                    "resources": [
                        "new restricted namespace",
                        "disposable pgvector Deployment/Service/Secrets",
                        "Helm application, PVC and create-once key hooks",
                    ],
                    "steps": [
                        "install",
                        "forced-pod-roll upgrade",
                        "uninstall/reinstall",
                        "pre-created-secret install/upgrade",
                        "two cluster workers, then scale to one",
                    ],
                    "checks": [
                        "health",
                        "key identity",
                        "encrypted DB probe",
                        "PVC identity and file",
                        "worker health, IDs, key digest and claims",
                    ],
                    "cleanup": "delete namespace only if this process created and still owns it",
                    "model_calls": 0,
                    "production_access": False,
                },
                indent=2,
            )
        )
        return
    runner = Lifecycle(args.context, args.namespace, args.chart)
    try:
        runner.run()
    except Exception as error:
        runner.diagnostics()
        raise SystemExit(
            "Lifecycle test failed: " + runner.redact(str(error))
        ) from None
    finally:
        runner.cleanup()


if __name__ == "__main__":
    main()
