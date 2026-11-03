"""Create durable Heym application keys once. Never log, replace or rotate keys."""

import base64
import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
from pathlib import Path


def ensure_keys(request, name, namespace):
    """Handle retries and concurrent installs without overwriting an existing Secret."""
    path = f"/api/v1/namespaces/{namespace}/secrets"
    existing = request("GET", f"{path}/{name}")
    if existing is None:
        data = {
            "SECRET_KEY": secrets.token_urlsafe(48).encode(),
            "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)),
        }
        payload = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": {"helm.sh/resource-policy": "keep"},
            },
            "data": {
                key: base64.b64encode(value).decode() for key, value in data.items()
            },
        }
        # A concurrent creator returns None on HTTP 409. Read and validate its keys.
        existing = request("POST", path, payload) or request("GET", f"{path}/{name}")
    if not existing or not all(
        existing.get("data", {}).get(key) for key in ("SECRET_KEY", "ENCRYPTION_KEY")
    ):
        raise RuntimeError(
            "Existing application-key Secret is incomplete; refusing to rotate it"
        )


def main():
    directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    token = (directory / "token").read_text().strip()
    namespace = (directory / "namespace").read_text().strip()
    name = os.environ["KEY_SECRET_NAME"]
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    if ":" in host:
        host = f"[{host}]"
    server = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')}"
    context = ssl.create_default_context(cafile=str(directory / "ca.crt"))

    def request(method, path, data=None):
        req = urllib.request.Request(
            server + path,
            method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, context=context, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if (method, error.code) in (("GET", 404), ("POST", 409)):
                return None
            raise RuntimeError(
                f"Kubernetes Secret request failed with HTTP {error.code}"
            ) from None

    ensure_keys(request, name, namespace)
    print("Application keys are ready; existing values were preserved")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Application key setup failed; no existing keys were changed"
        ) from None
