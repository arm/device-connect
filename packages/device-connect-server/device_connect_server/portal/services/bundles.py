"""Create and serve tenant credential bundles (.zip)."""

import io
import json
import zipfile
from pathlib import Path

from .. import config
from . import credentials
from .backend import get_backend


def create_bundle(tenant: str, public_host: str = "") -> bytes:
    """Create a zip bundle with all credentials for a tenant.

    Returns the zip file as bytes.
    """
    backend = get_backend()
    backend_name = backend.backend_name()
    broker_info = backend.broker_display_info()
    host = public_host or broker_info.get("host", "localhost")
    port = broker_info.get("port", "")

    buf = io.BytesIO()
    creds_list = credentials.list_credentials(tenant=tenant)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add credential files
        for cred in creds_list:
            cred_path = Path(cred["path"])
            if cred_path.exists():
                zf.write(cred_path, f"{tenant}/credentials/{cred['filename']}")

        # For Zenoh: include TLS cert files referenced in credentials
        if backend_name == "zenoh":
            _add_zenoh_certs(zf, tenant, creds_list)

        # Add tenant-config.env (backend-aware)
        env_content = _generate_env(tenant, backend_name, host, port)
        zf.writestr(f"{tenant}/tenant-config.env", env_content)

        # Add quickstart README
        readme = _generate_readme(tenant, backend_name)
        zf.writestr(f"{tenant}/README.md", readme)

    return buf.getvalue()


def _add_zenoh_certs(zf: zipfile.ZipFile, tenant: str, creds_list: list[dict]) -> None:
    """Add TLS certificates to the zip bundle for Zenoh credentials."""
    ca_cert = config.SECURITY_INFRA_DIR / "ca.pem"
    if ca_cert.exists():
        zf.write(ca_cert, f"{tenant}/certs/ca.pem")

    added = set()
    for cred in creds_list:
        cred_path = Path(cred["path"])
        if not cred_path.exists():
            continue
        try:
            data = json.loads(cred_path.read_text())
            zenoh_tls = data.get("zenoh", {}).get("tls", {})
            for key in ("cert_file", "key_file"):
                fpath = zenoh_tls.get(key, "")
                if fpath and fpath not in added:
                    p = Path(fpath)
                    if p.exists():
                        zf.write(p, f"{tenant}/certs/{p.name}")
                        added.add(fpath)
        except (json.JSONDecodeError, OSError):
            continue


def _generate_env(tenant: str, backend: str, host: str, port: str) -> str:
    """Generate tenant-config.env content."""
    lines = [
        f"# Device Connect — Tenant: {tenant}",
        "# Source this file: source tenant-config.env",
        "",
        f"export TENANT={tenant}",
        f"export MESSAGING_BACKEND={backend}",
    ]

    if backend == "zenoh":
        lines += [
            f"export ZENOH_CONNECT=tls/{host}:{port}",
            "",
            "# TLS certificates (adjust paths if needed):",
            "export MESSAGING_TLS_CA_FILE=./certs/ca.pem",
            f"# export MESSAGING_TLS_CERT_FILE=./certs/{tenant}-device-001-cert.pem",
            f"# export MESSAGING_TLS_KEY_FILE=./certs/{tenant}-device-001-key.pem",
        ]
    elif backend == "mqtt":
        lines += [
            f"export MQTT_URL=mqtt://{host}:{port}",
            "",
            "# Set this to the credentials file for your device:",
            f"# export MQTT_CREDENTIALS_FILE=./credentials/{tenant}-device-001.creds.json",
        ]
    else:
        lines += [
            f"export NATS_URL=nats://{host}:{port}",
            "",
            "# Set this to the credentials file for your device:",
            f"# export NATS_CREDENTIALS_FILE=./credentials/{tenant}-device-001.creds.json",
        ]

    return "\n".join(lines) + "\n"


def _generate_readme(tenant: str, backend: str) -> str:
    """Generate quickstart README content."""
    if backend == "zenoh":
        return (
            f"# {tenant} — Device Connect Credentials (Zenoh)\n\n"
            f"## Quick Start\n\n"
            f"1. Source the environment:\n"
            f"   ```bash\n"
            f"   source tenant-config.env\n"
            f"   ```\n\n"
            f"2. Set your device TLS credentials:\n"
            f"   ```bash\n"
            f"   export MESSAGING_TLS_CERT_FILE=./certs/{tenant}-device-001-cert.pem\n"
            f"   export MESSAGING_TLS_KEY_FILE=./certs/{tenant}-device-001-key.pem\n"
            f"   ```\n\n"
            f"3. Run your device:\n"
            f"   ```bash\n"
            f"   python your_device.py\n"
            f"   ```\n"
        )
    elif backend == "mqtt":
        return (
            f"# {tenant} — Device Connect Credentials (MQTT)\n\n"
            f"## Quick Start\n\n"
            f"1. Source the environment:\n"
            f"   ```bash\n"
            f"   source tenant-config.env\n"
            f"   ```\n\n"
            f"2. Set your device credential:\n"
            f"   ```bash\n"
            f"   export MQTT_CREDENTIALS_FILE=./credentials/{tenant}-device-001.creds.json\n"
            f"   ```\n\n"
            f"3. Run your device:\n"
            f"   ```bash\n"
            f"   python your_device.py\n"
            f"   ```\n"
        )
    else:
        return (
            f"# {tenant} — Device Connect Credentials\n\n"
            f"## Quick Start\n\n"
            f"1. Source the environment:\n"
            f"   ```bash\n"
            f"   source tenant-config.env\n"
            f"   ```\n\n"
            f"2. Set your device credential:\n"
            f"   ```bash\n"
            f"   export NATS_CREDENTIALS_FILE=./credentials/{tenant}-device-001.creds.json\n"
            f"   ```\n\n"
            f"3. Run your device:\n"
            f"   ```bash\n"
            f"   python your_device.py\n"
            f"   ```\n"
        )
