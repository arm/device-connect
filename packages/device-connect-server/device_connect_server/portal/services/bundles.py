"""Create and serve tenant credential bundles (.zip)."""

import io
import json
import zipfile
from pathlib import Path

from .. import config
from . import credentials


def create_bundle(tenant: str, public_host: str = "") -> bytes:
    """Create a zip bundle with all credentials for a tenant.

    Returns the zip file as bytes.
    """
    nats_host = public_host or config.NATS_HOST
    buf = io.BytesIO()
    creds = credentials.list_credentials(tenant=tenant)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add credential files
        for cred in creds:
            cred_path = Path(cred["path"])
            if cred_path.exists():
                zf.write(cred_path, f"{tenant}/credentials/{cred['filename']}")

        # Add tenant-config.env
        env_content = (
            f"# Device Connect — Tenant: {tenant}\n"
            f"# Source this file: source tenant-config.env\n\n"
            f"export TENANT={tenant}\n"
            f"export NATS_URL=nats://{nats_host}:{config.NATS_PORT}\n"
            f"export MESSAGING_BACKEND=nats\n\n"
            f"# Set this to the credentials file for your device:\n"
            f"# export NATS_CREDENTIALS_FILE=./credentials/{tenant}-device-001.creds.json\n"
        )
        zf.writestr(f"{tenant}/tenant-config.env", env_content)

        # Add quickstart README
        readme = (
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
        zf.writestr(f"{tenant}/README.md", readme)

    return buf.getvalue()
