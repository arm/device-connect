"""Query the device registry for live device data via etcd."""

import json
import logging

from .. import config

logger = logging.getLogger(__name__)

_DEVICES_PREFIX = "/device-connect/"


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
            devices.append({
                "device_id": data.get("device_id", "unknown"),
                "device_type": data.get("device_type", "unknown"),
                "status": data.get("status", "unknown"),
                "location": data.get("location", ""),
                "last_seen": data.get("last_heartbeat", data.get("registered_at", "")),
                "capabilities": data.get("capabilities", []),
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
    results = client.get_prefix(_DEVICES_PREFIX)
    counts: dict[str, dict] = {}

    for raw, meta in results:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            tenant = data.get("tenant", "unknown")
            if tenant not in counts:
                counts[tenant] = {"total": 0, "online": 0}
            counts[tenant]["total"] += 1
            if data.get("status") == "online":
                counts[tenant]["online"] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    return counts
