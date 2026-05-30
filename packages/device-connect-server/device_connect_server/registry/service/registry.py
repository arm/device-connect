# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

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
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import etcd3gw
from requests.adapters import HTTPAdapter

_logger = logging.getLogger(__name__)

ETCD_HOST = os.getenv("ETCD_HOST", "localhost")
ETCD_PORT = int(os.getenv("ETCD_PORT", "2379"))

# Size of the urllib3 connection pool the etcd3gw client uses. The
# default (10) caps concurrent HTTP-to-etcd round-trips and bottlenecks
# the registry under a registration herd — every lease+put is two
# sequential HTTP calls, so 1400 phones at startup queue thousands of
# requests behind 10 sockets. 64 keeps the registry processor-bound on
# realistic hardware while staying well under etcd's connection ceiling.
_ETCD_POOL_SIZE = int(os.getenv("DC_ETCD_POOL_SIZE", "64"))

# Short-lived in-memory snapshot of a tenant's decoded device list. A
# paged walk over a 1400-device fleet in pages of 100 used to do a full
# ``etcd get_prefix`` + JSON-decode of all 1400 records on every one of
# the ~14 page requests (~19,600 decodes per walk), because each page
# call re-scanned from scratch. Caching the decoded fleet for a beat
# collapses a walk to a single scan+decode and, as a bonus, makes the
# walk internally consistent (every page reads the same snapshot, so a
# concurrent registration can't shift records across pages mid-walk).
#
# TTL is deliberately small (default 2s, below the dashboard's 10s poll
# and human perception) so a just-registered device still surfaces
# within one poll. Set ``DC_FLEET_CACHE_TTL=0`` to disable and restore
# the always-fresh scan-per-call behavior. Should comfortably exceed a
# single walk's wall-clock so later pages hit the cache.
_FLEET_CACHE_TTL = float(os.getenv("DC_FLEET_CACHE_TTL", "2.0"))


def _enlarge_etcd_pool(client: Any, pool_size: int) -> None:
    """Replace the etcd3gw client's HTTPAdapters with larger-pool ones.

    We mount the adapter onto the already-constructed ``client.session``
    instead of passing ``session=`` to ``etcd3gw.client(...)`` so the
    fix works against etcd3gw 2.5.x (no ``session`` kwarg) and 2.6+.

    If the etcd3gw client stops exposing ``session`` (e.g. a future
    refactor wraps it), log a warning so the silently-degraded pool
    doesn't reintroduce the registration-storm bottleneck without any
    operator-visible signal.
    """
    if not hasattr(client, "session"):
        _logger.warning(
            "etcd3gw client has no ``session`` attribute; HTTP pool "
            "size remains at the urllib3 default (10). This will "
            "bottleneck the registry under registration herds. "
            "Inspect etcd3gw internals and update _enlarge_etcd_pool.",
        )
        return
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    client.session.mount("http://", adapter)
    client.session.mount("https://", adapter)


def _kv_key(kv: dict) -> str:
    """Extract the key string from an etcd3gw KV metadata dict."""
    raw = kv.get("key", "")
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw if isinstance(raw, str) else str(raw)


def summarize_fleet(devices: list[dict]) -> dict:
    """Aggregate a device list into a fleet summary.

    Returns dict with ``total_devices``, ``total_functions``,
    ``by_type``, and ``by_location``.
    """
    by_type: dict[str, dict] = {}
    by_location: dict[str, dict] = {}
    total_functions = 0

    for d in devices:
        dt = (d.get("identity") or {}).get("device_type") or "unknown"
        loc = (d.get("status") or {}).get("location") or "unknown"
        funcs = (d.get("capabilities") or {}).get("functions", [])
        total_functions += len(funcs)

        if dt not in by_type:
            by_type[dt] = {"count": 0, "locations": set()}
        by_type[dt]["count"] += 1
        by_type[dt]["locations"].add(loc)

        if loc not in by_location:
            by_location[loc] = {"count": 0, "types": set()}
        by_location[loc]["count"] += 1
        by_location[loc]["types"].add(dt)

    for info in by_type.values():
        info["locations"] = sorted(info["locations"])
    for info in by_location.values():
        info["types"] = sorted(info["types"])

    return {
        "total_devices": len(devices),
        "total_functions": total_functions,
        "by_type": by_type,
        "by_location": by_location,
    }


@dataclass
class DeviceRegistry:
    """Wrapper around ``etcd3gw`` that tracks leases per device."""

    host: str
    port: int
    client: Any = field(init=False)
    leases: Dict[str, Any] = field(default_factory=dict, init=False)
    # tenant -> (monotonic_timestamp, decoded device list). See
    # _FLEET_CACHE_TTL for the rationale.
    _fleet_cache: Dict[str, Tuple[float, List[dict]]] = field(
        default_factory=dict, init=False, repr=False,
    )

    def __post_init__(self) -> None:  # pragma: no cover - thin wrapper
        self.client = etcd3gw.client(host=self.host, port=self.port)
        _enlarge_etcd_pool(self.client, _ETCD_POOL_SIZE)

    def _decoded_fleet(self, tenant: str) -> List[dict]:
        """Return the decoded device list for ``tenant``, scanning etcd at
        most once per ``_FLEET_CACHE_TTL`` window.

        The returned list (and the dicts in it) is the shared cached
        snapshot — callers must treat it as read-only. ``list_devices``
        copies before filtering; other callers only read.
        """
        ttl = _FLEET_CACHE_TTL
        if ttl > 0:
            entry = self._fleet_cache.get(tenant)
            if entry is not None and (time.monotonic() - entry[0]) < ttl:
                return entry[1]

        prefix = f"/device-connect/{tenant}/devices/"
        devices: List[dict] = []
        for value, _kv in self.client.get_prefix(prefix):
            try:
                devices.append(json.loads(value))
            except json.JSONDecodeError:
                continue

        if ttl > 0:
            self._fleet_cache[tenant] = (time.monotonic(), devices)
        return devices

    def _invalidate_fleet_cache(self, tenant: str) -> None:
        """Drop the cached snapshot for ``tenant`` after a write so this
        process's own registrations/updates are never masked by the TTL.
        Lease expirations happen in etcd out-of-band and are only bounded
        by ``_FLEET_CACHE_TTL``."""
        self._fleet_cache.pop(tenant, None)

    def _key(self, tenant: str, device_id: str) -> str:
        return f"/device-connect/{tenant}/devices/{device_id}"

    def _lease_key(self, tenant: str, device_id: str) -> str:
        return f"{tenant}/{device_id}"

    def register(self, tenant: str, device_id: str, payload: dict, ttl: int) -> None:
        """Register ``device_id`` with ``payload`` using ``ttl`` seconds."""
        lease = self.client.lease(ttl=ttl)
        self.client.put(self._key(tenant, device_id), json.dumps(payload), lease=lease)
        self.leases[self._lease_key(tenant, device_id)] = lease
        self._invalidate_fleet_cache(tenant)

    def refresh(self, tenant: str, device_id: str, ttl: int | None = None) -> None:
        """Refresh the lease for ``device_id``, recovering if the lease handle was lost.

        After a registry service restart the in-memory ``leases`` dict is
        empty.  If a heartbeat arrives for a device that still has data in
        etcd but no lease handle, we create a fresh lease and re-store the
        data so the device stays alive instead of silently expiring.
        """
        lk = self._lease_key(tenant, device_id)
        lease = self.leases.get(lk)
        if lease:
            # Lease can die in etcd (TTL-expired) while we still hold the stale
            # handle. etcd3gw's refresh() returns the new TTL, or -1 when the
            # lease has already expired -- it does NOT raise in that case. A
            # transport/server error does raise. Either way, drop the stale
            # handle and fall through to recovery so has_lease() reports False
            # and the server can ask the device to re-register.
            try:
                new_ttl = lease.refresh()
            except Exception:
                new_ttl = -1
            if new_ttl >= 0:
                return
            self.leases.pop(lk, None)
            _logger.info(
                "stale lease for %s/%s dropped; attempting recovery",
                tenant,
                device_id,
            )

        # No lease handle — attempt recovery
        if ttl is None:
            return  # Cannot recover without TTL
        key = self._key(tenant, device_id)
        results = self.client.get(key)
        if not results:
            return  # Device data already gone from etcd
        try:
            doc = json.loads(results[0])
        except (json.JSONDecodeError, TypeError):
            return
        new_lease = self.client.lease(ttl=ttl)
        self.client.put(key, json.dumps(doc), lease=new_lease)
        self.leases[lk] = new_lease
        _logger.info("recovered lease for %s/%s (ttl=%d)", tenant, device_id, ttl)

    def has_lease(self, tenant: str, device_id: str) -> bool:
        """Check whether an active lease handle exists for ``device_id``."""
        return self._lease_key(tenant, device_id) in self.leases

    def list_devices(
        self,
        tenant: str,
        device_type: str | None = None,
        location: str | None = None,
    ) -> List[dict]:
        """Return registered device payloads for ``tenant``, optionally filtered.

        Args:
            tenant: Tenant namespace.
            device_type: Filter by device type (case-insensitive substring match).
            location: Filter by device location (case-insensitive substring match).
        """
        # Shallow-copy the cached snapshot so the filter rebinds below
        # never mutate the shared cache entry (the dicts are read-only).
        devices: List[dict] = list(self._decoded_fleet(tenant))

        if device_type:
            dt = device_type.lower()
            devices = [
                d for d in devices
                if dt in (d.get("identity", {}).get("device_type") or "").lower()
            ]
        if location:
            loc = location.lower()
            devices = [
                d for d in devices
                if loc in (d.get("status", {}).get("location") or "").lower()
            ]
        return devices

    def list_devices_page(
        self,
        tenant: str,
        *,
        device_type: str | None = None,
        location: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> Tuple[List[dict], int | None, int]:
        """Return one page of registered device payloads plus pagination metadata.

        Slices the filtered fleet by ``offset`` and ``limit``; the reply
        carries only the requested page, keeping NATS payloads bounded
        regardless of fleet size.

        Stability: device order follows etcd key order, which is deterministic
        for a steady-state fleet; concurrent registrations/expirations can
        shift records across pages, producing transient duplicates or skips
        in a walk that spans many round-trips. The fleet-snapshot cache (see
        ``_FLEET_CACHE_TTL``) makes a walk that completes within one TTL
        window read a single consistent snapshot, which largely closes this
        gap in practice; callers that need a strictly stable snapshot across
        TTL boundaries must still filter duplicates by ``device_id`` after
        the walk. (Keyset pagination would remove the caveat entirely; offset
        is used here for simplicity.)

        Returns:
            (devices_page, next_offset, total_matched).
            ``next_offset`` is None when the page reaches the end of the
            filtered list. ``total_matched`` is the size after the
            ``device_type``/``location`` filters and before pagination.
            ACL filtering, when enabled at the handler layer, runs after
            this method returns and can further shrink the page.

        Cost model:
            Page slicing runs over the decoded fleet returned by
            ``list_devices`` -> ``_decoded_fleet``, which scans+decodes
            the tenant prefix at most once per ``_FLEET_CACHE_TTL`` window
            (default 2s). A walk over N devices in pages of P that fits in
            one TTL window therefore does ONE ``etcd get_prefix`` + N
            decodes total, not ``O(ceil(N / P))`` full scans. With the
            cache disabled (``DC_FLEET_CACHE_TTL=0``) it reverts to a full
            scan+decode per page. Even cached, registry CPU/etcd traffic
            still scale with fleet size per TTL window; selector pushdown /
            keyset pagination (out-of-scope in PR #38) remains the path for
            fleets materially larger than the current ~1400 devices.
        """
        all_devices = self.list_devices(
            tenant, device_type=device_type, location=location,
        )
        total = len(all_devices)
        safe_offset = max(0, int(offset or 0))
        if limit is None or limit <= 0:
            page = all_devices[safe_offset:]
            next_offset: int | None = None
        else:
            end = safe_offset + int(limit)
            page = all_devices[safe_offset:end]
            next_offset = end if end < total else None
        return page, next_offset, total

    def get_device(self, tenant: str, device_id: str) -> dict | None:
        """Return a single device payload by direct key lookup (O(1)).

        Args:
            tenant: Tenant namespace.
            device_id: Device identifier.

        Returns:
            Device payload dict, or ``None`` if not found.
        """
        key = self._key(tenant, device_id)
        results = self.client.get(key)
        if not results:
            return None
        try:
            return json.loads(results[0])
        except (json.JSONDecodeError, IndexError):
            return None

    def describe_fleet(self, tenant: str) -> dict:
        """Return aggregated fleet summary (counts by type and location).

        Args:
            tenant: Tenant namespace.

        Returns:
            Dict with ``total_devices``, ``total_functions``,
            ``by_type``, and ``by_location``.
        """
        return summarize_fleet(self.list_devices(tenant))

    def update_status(self, tenant: str, device_id: str, status: dict) -> None:
        """Update the ``status`` section of a device entry.

        Merges the new status with existing status to preserve fields
        like battery and online that aren't included in heartbeats.
        Skips the write if the merged result would be identical,
        avoiding unnecessary etcd revisions.
        """
        if not status:
            return  # nothing to update
        key = self._key(tenant, device_id)
        results = self.client.get(key)
        if not results:
            return  # unknown device, ignore
        doc = json.loads(results[0])
        existing_status = doc.get("status", {})
        # Skip write if all incoming fields already match
        if all(existing_status.get(k) == v for k, v in status.items()):
            return
        # Merge new status with existing status (new values override)
        existing_status.update(status)
        doc["status"] = existing_status
        lease = self.leases.get(self._lease_key(tenant, device_id))
        if lease:
            self.client.put(key, json.dumps(doc), lease=lease)
        else:
            self.client.put(key, json.dumps(doc))
        self._invalidate_fleet_cache(tenant)


# Global instance used by module level helpers
_REGISTRY = DeviceRegistry(ETCD_HOST, ETCD_PORT)


def register(tenant: str, device_id: str, payload: dict, ttl: int) -> None:
    """Register a device using the provided ``ttl``."""
    _REGISTRY.register(tenant, device_id, payload, ttl)


def refresh(tenant: str, device_id: str, ttl: int | None = None) -> None:
    """Refresh the lease for ``device_id`` if present."""
    _REGISTRY.refresh(tenant, device_id, ttl=ttl)


def list_devices(
    tenant: str,
    device_type: str | None = None,
    location: str | None = None,
) -> List[dict]:
    """Return a list of registered devices for ``tenant``, optionally filtered."""
    return _REGISTRY.list_devices(tenant, device_type=device_type, location=location)


def list_devices_page(
    tenant: str,
    *,
    device_type: str | None = None,
    location: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> Tuple[List[dict], int | None, int]:
    """Module-level wrapper for :meth:`DeviceRegistry.list_devices_page`."""
    return _REGISTRY.list_devices_page(
        tenant,
        device_type=device_type,
        location=location,
        offset=offset,
        limit=limit,
    )


def get_device(tenant: str, device_id: str) -> dict | None:
    """Return a single device by direct key lookup."""
    return _REGISTRY.get_device(tenant, device_id)


def has_lease(tenant: str, device_id: str) -> bool:
    """Check whether an active lease handle exists for ``device_id``."""
    return _REGISTRY.has_lease(tenant, device_id)


def describe_fleet(tenant: str) -> dict:
    """Return aggregated fleet summary for ``tenant``."""
    return _REGISTRY.describe_fleet(tenant)


def update_status(tenant: str, device_id: str, status: dict) -> None:
    """Update the ``status`` section for ``device_id``."""
    _REGISTRY.update_status(tenant, device_id, status)
