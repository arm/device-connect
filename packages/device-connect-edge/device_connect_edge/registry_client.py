# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Lightweight registry client for device discovery via JSON-RPC.

Sends ``discovery/listDevices`` and ``discovery/getDevice`` requests
over the pluggable messaging layer to the registry service. Conforms
to :class:`~device_connect_edge.discovery_provider.DiscoveryProvider`.

This client is transport-only — it formats JSON-RPC messages and sends
them via :class:`~device_connect_edge.messaging.base.MessagingClient`.
Actual device data lives in the registry service (backed by etcd).

Usage::

    from device_connect_edge.messaging import create_client
    from device_connect_edge.registry_client import RegistryClient

    messaging = create_client("nats")
    await messaging.connect(servers=["nats://localhost:4222"])

    registry = RegistryClient(messaging, tenant="default")
    devices = await registry.list_devices(device_type="camera")
    camera = await registry.get_device("camera-001")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from device_connect_edge.messaging.base import MessagingClient
from device_connect_edge.messaging.exceptions import RequestTimeoutError

logger = logging.getLogger(__name__)

# Per-page chunk size when the client transparently iterates the full fleet.
# Sized to keep one JSON-RPC reply well under the default NATS max_payload
# of 1 MB even when device records carry rich function schemas (~10 KB each
# in the worst case observed): 100 * ~10 KB = ~1 MB, with the actual upper
# bound for typical records (~6 KB) landing at ~600 KB. Operators on
# unusually rich schemas can drop this via DEVICE_CONNECT_LIST_PAGE_SIZE.
_DEFAULT_LIST_PAGE_SIZE = int(os.getenv("DEVICE_CONNECT_LIST_PAGE_SIZE", "100"))


class RegistryClient:
    """JSON-RPC client for the device registry service.

    Implements the :class:`~device_connect_edge.discovery_provider.DiscoveryProvider`
    protocol for infra-mode device discovery.

    Args:
        messaging_client: Connected ``MessagingClient`` instance.
        tenant: Device Connect tenant/namespace (default: ``"default"``).
        timeout: Default request timeout in seconds.
        cache_ttl: TTL for cached device list in seconds (0 = no caching).
    """

    def __init__(
        self,
        messaging_client: MessagingClient,
        tenant: str = "default",
        timeout: float = 5.0,
        cache_ttl: float = 0,
    ):
        self._client = messaging_client
        self._tenant = tenant
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_time: float = 0

    @property
    def tenant(self) -> str:
        return self._tenant

    # ── JSON-RPC transport ───────────────────────────────────────────

    async def _request(
        self,
        subject: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        retries: int = 3,
    ) -> Any:
        """Send a JSON-RPC 2.0 request and return the result.

        Retries on ``RequestTimeoutError`` with exponential backoff.
        Server-side errors (``RuntimeError``) are raised immediately.
        """
        timeout = timeout or self._timeout
        delay = 1
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            req_id = f"rpc-{uuid.uuid4().hex[:12]}"
            payload: Dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params:
                payload["params"] = params

            try:
                response_data = await self._client.request(
                    subject, json.dumps(payload).encode(), timeout=timeout,
                )
                response = json.loads(response_data)

                if "error" in response:
                    error = response["error"]
                    raise RuntimeError(
                        f"Registry error ({error.get('code', -1)}): "
                        f"{error.get('message', 'Unknown error')}"
                    )
                return response.get("result")
            except RequestTimeoutError as e:
                last_err = e
                if attempt < retries:
                    logger.warning(
                        "Registry request %s timed out (attempt %d/%d), retrying in %ss",
                        method, attempt, retries, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        raise last_err  # type: ignore[misc]

    # ── DiscoveryProvider interface ──────────────────────────────────

    async def list_devices(
        self,
        *,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """List devices from the registry service.

        Sends ``discovery/listDevices`` JSON-RPC over the messaging layer.
        Filter params are forwarded to the server (requires server-side
        filtering support).

        Args:
            device_type: Filter by device type.
            location: Filter by location.
            capabilities: Filter by required capabilities.
            timeout: Override default timeout.

        Returns:
            List of device dictionaries with full registration data.
        """
        # Check cache
        if self._cache_ttl > 0 and self._cache is not None:
            age = time.time() - self._cache_time
            if age < self._cache_ttl:
                logger.debug("Using cached device list (age: %.1fs)", age)
                return self._filter_devices(
                    self._cache, device_type, location, capabilities,
                )

        # Page through the registry transparently so the wire never carries
        # a fleet-sized reply (NATS default max_payload is 1 MB and was
        # being exceeded at ~1400 devices). Older servers that don't
        # understand ``limit`` just return everything in one reply with
        # ``next_offset`` absent, so the loop exits after a single
        # iteration — fully backward compatible.
        devices: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page, next_offset, _total = await self._list_devices_page(
                device_type=device_type,
                location=location,
                capabilities=capabilities,
                offset=offset,
                limit=_DEFAULT_LIST_PAGE_SIZE,
                timeout=timeout,
            )
            devices.extend(page)
            if next_offset is None:
                break
            offset = next_offset
        logger.debug("Discovered %d devices from registry", len(devices))

        # Update cache (store unfiltered if we fetched without filters)
        if (
            self._cache_ttl > 0
            and device_type is None
            and location is None
            and not capabilities
        ):
            self._cache = devices
            self._cache_time = time.time()

        return devices

    async def list_devices_page(
        self,
        *,
        offset: int = 0,
        limit: int = _DEFAULT_LIST_PAGE_SIZE,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[int], int]:
        """Fetch a single page of devices with pagination metadata.

        Use this when you want to display a paged UI or stream results;
        most callers should stick with :meth:`list_devices`, which loops
        internally and returns the full fleet.

        Returns:
            ``(devices, next_offset, total_matched)`` where ``next_offset``
            is ``None`` on the final page.

        ACL caveat:
            When the registry has ACLs enabled, server-side filtering
            runs *after* slicing. As a result ``len(devices)`` for a
            given page may be smaller than ``limit`` even when more
            pages follow, and ``total_matched`` is the unfiltered total
            (before the caller's ACL applies). UIs should treat
            ``total_matched`` as an upper bound on what the caller will
            ever see, and must not assume ``len(devices) == limit``
            implies a full page.
        """
        return await self._list_devices_page(
            device_type=device_type,
            location=location,
            capabilities=capabilities,
            offset=offset,
            limit=limit,
            timeout=timeout,
        )

    async def _list_devices_page(
        self,
        *,
        device_type: Optional[str],
        location: Optional[str],
        capabilities: Optional[List[str]],
        offset: int,
        limit: int,
        timeout: Optional[float],
    ) -> Tuple[List[Dict[str, Any]], Optional[int], int]:
        subject = f"device-connect.{self._tenant}.discovery"
        params: Dict[str, Any] = {"offset": int(offset), "limit": int(limit)}
        if device_type:
            params["device_type"] = device_type
        if location:
            params["location"] = location
        if capabilities:
            params["capabilities"] = capabilities

        result = await self._request(
            subject, "discovery/listDevices", params, timeout,
        )
        devices = result.get("devices", [])
        next_offset = result.get("next_offset")
        total = result.get("total_matched", len(devices))
        return devices, next_offset, total

    async def get_device(
        self,
        device_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a specific device by ID.

        Tries ``discovery/getDevice`` RPC first. Falls back to iterating
        ``list_devices`` if the server doesn't support it.

        Args:
            device_id: Device identifier.
            timeout: Override default timeout.

        Returns:
            Device dictionary, or ``None`` if not found.
        """
        # Try dedicated RPC first.  Use a short timeout for the probe —
        # if the server supports getDevice the response is near-instant
        # (O(1) etcd lookup).  A long timeout here causes cascading delays
        # when the registry is an older version that silently drops the RPC.
        subject = f"device-connect.{self._tenant}.discovery"
        probe_timeout = min(timeout or self._timeout, 5.0)
        try:
            result = await self._request(
                subject,
                "discovery/getDevice",
                {"device_id": device_id},
                probe_timeout,
            )
            if result:
                return result.get("device")
        except (RuntimeError, RequestTimeoutError) as e:
            # RuntimeError = server returned JSON-RPC error (method not found)
            # RequestTimeoutError = server didn't respond (RPC not supported)
            logger.debug("discovery/getDevice not available, falling back to list: %s", e)

        # Fallback: iterate list
        devices = await self.list_devices(timeout=timeout)
        for device in devices:
            if device.get("device_id") == device_id:
                return device
        return None

    def invalidate_cache(self) -> None:
        """Force cache invalidation."""
        self._cache = None
        self._cache_time = 0

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _filter_devices(
        devices: List[Dict[str, Any]],
        device_type: Optional[str],
        location: Optional[str],
        capabilities: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        """Apply client-side filters to a cached device list."""
        result = devices
        if device_type:
            dt = device_type.lower()
            result = [
                d for d in result
                if dt in (
                    (d.get("identity") or {}).get("device_type", "")
                    or d.get("device_type", "")
                ).lower()
            ]
        if location:
            loc = location.lower()
            result = [
                d for d in result
                if loc in (
                    (d.get("status") or {}).get("location", "")
                    or d.get("location", "")
                ).lower()
            ]
        if capabilities:
            caps_set = set(c.lower() for c in capabilities)
            result = [
                d for d in result
                if caps_set.issubset(
                    set(
                        (f["name"] if isinstance(f, dict) else f).lower()
                        for f in (d.get("capabilities") or {}).get("functions", [])
                        if (f["name"] if isinstance(f, dict) else f)
                    )
                )
            ]
        return result
