"""Query the device registry for live device data via etcd."""

import json
import logging
import time

from .. import config

logger = logging.getLogger(__name__)

_DEVICES_PREFIX = "/device-connect/"


def _format_ts(ts) -> str:
    """Format a unix timestamp as a relative 'ago' string, or empty if missing."""
    if not ts:
        return ""
    try:
        delta = int(time.time() - float(ts))
        if delta < 5:
            return "just now"
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        return f"{delta // 3600}h ago"
    except (ValueError, TypeError):
        return ""


def _etcd_client():
    from etcd3gw import Etcd3Client
    return Etcd3Client(host=config.ETCD_HOST, port=config.ETCD_PORT)


def list_live_devices(tenant: str) -> list[dict]:
    """Query etcd for all registered devices in a tenant namespace.

    Returns list of device dicts with id, type, status, location, last_seen, capabilities.
    """
    client = _etcd_client()
    prefix = f"{_DEVICES_PREFIX}{tenant}/devices/"

    results = client.get_prefix(prefix)
    devices = []
    for raw, meta in results:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            status = data.get("status") or {}
            identity = data.get("identity") or {}
            reg = data.get("registry") or {}
            devices.append({
                "device_id": data.get("device_id", "unknown"),
                "device_type": identity.get("device_type", "unknown"),
                "status": status.get("availability", "unknown"),
                "location": status.get("location", ""),
                "last_seen": _format_ts(status.get("ts")) or reg.get("registered_at", ""),
                "capabilities": data.get("capabilities", {}),
                "_raw": data,
            })
        except (json.JSONDecodeError, TypeError):
            continue

    return devices


def get_device(tenant: str, device_id: str) -> dict | None:
    """Get a single device's registration data."""
    client = _etcd_client()
    key = f"{_DEVICES_PREFIX}{tenant}/devices/{device_id}"
    values = client.get(key)
    if not values:
        return None
    raw = values[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def count_all_devices() -> dict[str, dict]:
    """Count live devices across all tenants.

    Returns: {tenant: {total: N, online: N}}
    """
    client = _etcd_client()
    # Only query device keys, not portal/users or other data
    device_prefix = f"{_DEVICES_PREFIX}"
    results = client.get_prefix(device_prefix)
    counts: dict[str, dict] = {}

    for raw, meta in results:
        # Filter to device keys by checking the key path
        key = ""
        if hasattr(meta, "key"):
            key = meta.key if isinstance(meta.key, str) else meta.key.decode()
        elif isinstance(meta, dict):
            k = meta.get("key", b"")
            key = k if isinstance(k, str) else k.decode()
        if "/devices/" not in key:
            continue

        try:
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            # Extract tenant from the key path: /device-connect/{tenant}/devices/{id}
            parts = key.split("/")
            # ['', 'device-connect', '{tenant}', 'devices', '{id}']
            tenant = parts[2] if len(parts) >= 4 else data.get("tenant", "unknown")
            if tenant not in counts:
                counts[tenant] = {"total": 0, "online": 0}
            counts[tenant]["total"] += 1
            status = data.get("status") or {}
            if status.get("availability") == "available":
                counts[tenant]["online"] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    return counts
