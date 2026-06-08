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


# Backend sections whose TLS material we know how to inline.
_TLS_BACKEND_KEYS = ("zenoh", "nats", "mqtt")
_TLS_FILE_TO_PEM = (("ca_file", "ca_pem"), ("cert_file", "cert_pem"), ("key_file", "key_pem"))


def inline_tls_material(cred_data: dict) -> dict:
    """Return a copy of *cred_data* with referenced TLS PEM files inlined.

    mTLS credentials (e.g. Zenoh) store ``tls.{ca,cert,key}_file`` as
    *absolute host paths* that only exist on the portal machine. A device
    downloading its ``*.creds.json`` to another host can't resolve them. This
    reads each referenced PEM and inlines it as ``tls.{ca,cert,key}_pem``,
    dropping the now-meaningless host paths, so the served credential is
    self-contained and portable. The edge runtime understands the ``*_pem``
    form (Zenoh feeds it to the router via base64 config fields).

    Non-mTLS credentials (NATS JWT) carry their secret inline already and are
    returned unchanged. Missing/unreadable PEM files are left as-is rather than
    raising, so a partially-provisioned credential still downloads.
    """
    if not isinstance(cred_data, dict):
        return cred_data

    # Work on a deep copy so we never mutate the on-disk-backed dict.
    out = json.loads(json.dumps(cred_data))

    for backend_key in _TLS_BACKEND_KEYS:
        section = out.get(backend_key)
        if not isinstance(section, dict):
            continue
        tls = section.get("tls")
        if not isinstance(tls, dict):
            continue
        for file_key, pem_key in _TLS_FILE_TO_PEM:
            if tls.get(pem_key):
                continue  # already inlined
            fpath = tls.get(file_key)
            if not fpath:
                continue
            try:
                tls[pem_key] = Path(fpath).read_text()
                del tls[file_key]  # host-absolute path is useless off-host
            except OSError:
                logger.warning("could not inline TLS material from %s", fpath)

    return out


def delete_credential(filename: str) -> bool:
    """Remove a credential file from disk.

    Returns True if a file was deleted, False if no such file existed.
    Uses the same path-traversal guard as :func:`get_credential`, so a
    crafted ``filename`` that resolves outside ``CREDS_DIR`` is rejected.
    """
    path = get_credential(filename)
    if not path:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        logger.exception("failed to remove credential %s", filename)
        return False


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
