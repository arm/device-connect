"""Light-weight interface to etcd for storing device information.

This module exposes simple helper functions that wrap the ``etcd3gw`` client
used by the device registry service. Device entries are stored under
``/device-connect/{tenant}/devices/{device_id}`` with a TTL lease. The lease is
refreshed on heartbeats.

Multi-tenant: all functions accept a ``tenant`` parameter that namespaces
etcd keys. A single registry instance can serve multiple tenants.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

import etcd3gw

ETCD_HOST = os.getenv("ETCD_HOST", "localhost")
ETCD_PORT = int(os.getenv("ETCD_PORT", "2379"))


def _kv_key(kv: dict) -> str:
    """Extract the key string from an etcd3gw KV metadata dict."""
    raw = kv.get("key", "")
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw if isinstance(raw, str) else str(raw)


@dataclass
class DeviceRegistry:
    """Wrapper around ``etcd3gw`` that tracks leases per device."""

    host: str
    port: int
    client: Any = field(init=False)
    leases: Dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:  # pragma: no cover - thin wrapper
        self.client = etcd3gw.client(host=self.host, port=self.port)

    def _key(self, tenant: str, device_id: str) -> str:
        return f"/device-connect/{tenant}/devices/{device_id}"

    def _lease_key(self, tenant: str, device_id: str) -> str:
        return f"{tenant}/{device_id}"

    def register(self, tenant: str, device_id: str, payload: dict, ttl: int) -> None:
        """Register ``device_id`` with ``payload`` using ``ttl`` seconds."""
        lease = self.client.lease(ttl=ttl)
        self.client.put(self._key(tenant, device_id), json.dumps(payload), lease=lease)
        self.leases[self._lease_key(tenant, device_id)] = lease

    def refresh(self, tenant: str, device_id: str) -> None:
        """Refresh the lease for ``device_id`` if it exists."""
        lease = self.leases.get(self._lease_key(tenant, device_id))
        if lease:
            lease.refresh()

    def list_devices(self, tenant: str) -> List[dict]:
        """Return all registered device payloads for ``tenant``."""
        prefix = f"/device-connect/{tenant}/devices/"
        devices: List[dict] = []
        for value, _kv in self.client.get_prefix(prefix):
            try:
                devices.append(json.loads(value))
            except json.JSONDecodeError:
                continue
        return devices

    def update_status(self, tenant: str, device_id: str, status: dict) -> None:
        """Update the ``status`` section of a device entry.

        Merges the new status with existing status to preserve fields
        like battery and online that aren't included in heartbeats.
        """
        key = self._key(tenant, device_id)
        results = self.client.get(key)
        if not results:
            return  # unknown device, ignore
        doc = json.loads(results[0])
        # Merge new status with existing status (new values override)
        existing_status = doc.get("status", {})
        existing_status.update(status)
        doc["status"] = existing_status
        lease = self.leases.get(self._lease_key(tenant, device_id))
        if lease:
            self.client.put(key, json.dumps(doc), lease=lease)
        else:
            self.client.put(key, json.dumps(doc))


# Global instance used by module level helpers
_REGISTRY = DeviceRegistry(ETCD_HOST, ETCD_PORT)


def register(tenant: str, device_id: str, payload: dict, ttl: int) -> None:
    """Register a device using the provided ``ttl``."""
    _REGISTRY.register(tenant, device_id, payload, ttl)


def refresh(tenant: str, device_id: str) -> None:
    """Refresh the lease for ``device_id`` if present."""
    _REGISTRY.refresh(tenant, device_id)


def list_devices(tenant: str) -> List[dict]:
    """Return a list of all registered devices for ``tenant``."""
    return _REGISTRY.list_devices(tenant)


def update_status(tenant: str, device_id: str, status: dict) -> None:
    """Update the ``status`` section for ``device_id``."""
    _REGISTRY.update_status(tenant, device_id, status)
