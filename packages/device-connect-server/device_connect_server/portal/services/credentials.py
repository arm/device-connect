# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Read and list credential JSON files from the credentials directory."""

import json
import logging
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)


def list_credentials(tenant: str | None = None) -> list[dict]:
    """List all credential files, optionally filtered by tenant.

    Returns list of dicts with keys: device_id, tenant, auth_type, filename, path.
    """
    if not config.CREDS_DIR.exists():
        return []

    creds = []
    for f in sorted(config.CREDS_DIR.glob("*.creds.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if tenant and data.get("tenant") != tenant:
            continue

        creds.append({
            "device_id": data.get("device_id", f.stem),
            "tenant": data.get("tenant", "unknown"),
            "auth_type": data.get("auth_type", "unknown"),
            "filename": f.name,
            "path": str(f),
        })

    return creds


def get_credential(filename: str) -> Path | None:
    """Get the full path to a credential file. Returns None if not found."""
    path = (config.CREDS_DIR / filename).resolve()
    # Prevent path traversal — resolved path must stay inside CREDS_DIR
    if not path.is_relative_to(config.CREDS_DIR.resolve()):
        return None
    if path.exists() and path.suffix == ".json":
        return path
    return None


def get_credential_data(filename: str) -> dict | None:
    """Read and return credential JSON data."""
    path = get_credential(filename)
    if not path:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def get_tenants_summary() -> dict[str, dict]:
    """Get a summary of all tenants and their device counts.

    Returns dict: tenant_name -> {device_count, devices: [device_id, ...]}
    """
    tenants: dict[str, dict] = {}
    for cred in list_credentials():
        t = cred["tenant"]
        if t == "default":
            continue  # skip privileged roles
        if t not in tenants:
            tenants[t] = {"device_count": 0, "devices": []}
        tenants[t]["device_count"] += 1
        tenants[t]["devices"].append(cred["device_id"])
    return tenants
