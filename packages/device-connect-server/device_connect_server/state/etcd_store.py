"""EtcdStateStore — concrete StateStore implementation backed by etcd3.

This module provides a production-ready state store using etcd3 via the
etcd3gw HTTP gateway client for key-value storage with TTL-based
expiration and distributed locks. It reuses the same etcd instance used
by the device registry service.

Key namespaces used by CMU CloudLab:
    /device-connect/state/experiments/{exp_id}     — experiment lifecycle state
    /device-connect/state/device_locks/{device_id} — device reservation locks
    /device-connect/state/plates/{plate_id}        — plate location tracking

Example:
    from device_connect_server.state import EtcdStateStore

    store = EtcdStateStore(host="localhost", port=2379)
    await store.connect()

    # Store experiment state
    await store.set("experiments/EXP-001", {
        "status": "running",
        "plate_id": "P003",
        "current_step": 1,
    })

    # Query experiment
    exp = await store.get("experiments/EXP-001")

    # Distributed lock for device reservation
    async with store.lock("device_locks/cybio-felix-1", ttl=300) as acquired:
        if acquired:
            await run_experiment()

    await store.close()
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from device_connect_server.state.base import StateStore
from device_connect_edge.telemetry.tracer import get_tracer, StatusCode
from device_connect_edge.telemetry.metrics import get_metrics

logger = logging.getLogger(__name__)

ETCD_HOST = os.getenv("ETCD_HOST", "localhost")
ETCD_PORT = int(os.getenv("ETCD_PORT", "2379"))


def _kv_key(kv: dict) -> str:
    """Extract the key string from an etcd3gw KV metadata dict."""
    raw = kv.get("key", "")
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw if isinstance(raw, str) else str(raw)


class EtcdStateStore(StateStore):
    """State store backed by etcd3 via the etcd3gw HTTP gateway.

    Provides key-value storage with TTL and distributed locks using
    etcd3gw's create() for atomic operations.

    Args:
        host: etcd server hostname
        port: etcd server port
        key_prefix: Prefix for all keys (default: "/device-connect/state/")

    Example:
        store = EtcdStateStore()
        await store.connect()
        await store.set("my/key", {"data": "value"}, ttl=60)
        result = await store.get("my/key")
        await store.close()
    """

    def __init__(
        self,
        host: str = ETCD_HOST,
        port: int = ETCD_PORT,
        key_prefix: str = "/device-connect/state/",
    ):
        self._host = host
        self._port = port
        self._key_prefix = key_prefix
        self._lock_prefix = "/device-connect/locks/"
        self._client = None
        self._leases: Dict[str, Any] = {}
        self._lock_owner_id = uuid.uuid4().hex[:12]

    def _full_key(self, key: str) -> str:
        """Build the full etcd key with prefix."""
        return f"{self._key_prefix}{key}"

    def _lock_key(self, key: str) -> str:
        """Build the full lock key."""
        return f"{self._lock_prefix}{key}"

    async def connect(self) -> None:
        """Establish connection to etcd via HTTP gateway."""
        import etcd3gw

        loop = asyncio.get_event_loop()
        self._client = await loop.run_in_executor(
            None, lambda: etcd3gw.client(host=self._host, port=self._port)
        )
        logger.info("EtcdStateStore connected to %s:%d", self._host, self._port)

    async def close(self) -> None:
        """Cleanup leases and release client."""
        if self._client is not None:
            loop = asyncio.get_event_loop()
            for lease in self._leases.values():
                try:
                    await loop.run_in_executor(None, lease.revoke)
                except Exception:
                    logger.debug("cleanup error revoking lease during close", exc_info=True)
            self._leases.clear()
            self._client = None
            logger.info("EtcdStateStore closed")

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get value for key.

        Args:
            key: The key to retrieve (without prefix)

        Returns:
            The stored value as a dict, or None if not found
        """
        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "etcd.get",
            attributes={"db.operation": "get", "db.key": key},
        ) as span:
            full_key = self._full_key(key)
            loop = asyncio.get_event_loop()
            t0 = time.monotonic()
            results = await loop.run_in_executor(
                None, self._client.get, full_key
            )
            metrics.state_op_duration.record(
                (time.monotonic() - t0) * 1000,
                {"db.operation": "get"},
            )
            if not results:
                span.set_status(StatusCode.OK)
                return None
            try:
                result = json.loads(results[0])
                span.set_status(StatusCode.OK)
                return result
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to decode value for key %s", full_key)
                return None

    async def get_prefix(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """Get all key-value pairs matching prefix.

        Args:
            prefix: Key prefix to match (e.g., "experiments/")

        Returns:
            Dict mapping keys (without global prefix) to their values
        """
        full_prefix = self._full_key(prefix)
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, self._client.get_prefix, full_prefix
        )
        out: Dict[str, Dict[str, Any]] = {}
        for value, kv in results:
            try:
                raw_key = _kv_key(kv)
                # Strip the global prefix to return relative keys
                relative_key = raw_key
                if raw_key.startswith(self._key_prefix):
                    relative_key = raw_key[len(self._key_prefix):]
                out[relative_key] = json.loads(value)
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return out

    async def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl: Optional[int] = None,
    ) -> None:
        """Set value for key with optional TTL.

        Args:
            key: The key to set (without prefix)
            value: The value to store
            ttl: Time-to-live in seconds (None = no expiry)
        """
        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "etcd.set",
            attributes={"db.operation": "set", "db.key": key},
        ) as span:
            full_key = self._full_key(key)
            encoded = json.dumps(value)
            loop = asyncio.get_event_loop()
            t0 = time.monotonic()

            if ttl is not None:
                lease = await loop.run_in_executor(
                    None, lambda: self._client.lease(ttl=ttl)
                )
                await loop.run_in_executor(
                    None, lambda: self._client.put(full_key, encoded, lease=lease)
                )
                self._leases[key] = lease
            else:
                await loop.run_in_executor(
                    None, self._client.put, full_key, encoded
                )

            metrics.state_op_duration.record(
                (time.monotonic() - t0) * 1000,
                {"db.operation": "set"},
            )
            span.set_status(StatusCode.OK)

    async def delete(self, key: str) -> bool:
        """Delete key.

        Args:
            key: The key to delete (without prefix)

        Returns:
            True if key existed and was deleted
        """
        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "etcd.delete",
            attributes={"db.operation": "delete", "db.key": key},
        ) as span:
            full_key = self._full_key(key)
            loop = asyncio.get_event_loop()
            t0 = time.monotonic()
            deleted = await loop.run_in_executor(
                None, self._client.delete, full_key
            )
            metrics.state_op_duration.record(
                (time.monotonic() - t0) * 1000,
                {"db.operation": "delete"},
            )
            # Revoke associated lease if any
            lease = self._leases.pop(key, None)
            if lease:
                try:
                    await loop.run_in_executor(None, lease.revoke)
                except Exception:
                    logger.debug("cleanup error revoking lease after delete", exc_info=True)
            span.set_status(StatusCode.OK)
            return bool(deleted)

    async def _try_acquire_lock(self, key: str, ttl: int) -> bool:
        """Acquire a distributed lock using etcd3gw atomic create.

        Uses create() which only sets the key if it does not already
        exist (create_revision == 0).

        Args:
            key: Lock key (relative, will be prefixed)
            ttl: Lock TTL in seconds

        Returns:
            True if lock acquired, False if already held
        """
        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "etcd.lock.acquire",
            attributes={"db.operation": "lock_acquire", "db.key": key},
        ) as span:
            lock_key = self._lock_key(key)
            owner_value = json.dumps({"owner": self._lock_owner_id})
            loop = asyncio.get_event_loop()

            t0 = time.monotonic()
            lease = await loop.run_in_executor(
                None, lambda: self._client.lease(ttl=ttl)
            )

            acquired = await loop.run_in_executor(
                None,
                lambda: self._client.create(lock_key, owner_value, lease=lease),
            )
            metrics.state_op_duration.record(
                (time.monotonic() - t0) * 1000,
                {"db.operation": "lock_acquire"},
            )

            if acquired:
                self._leases[f"_lock_{key}"] = lease
                logger.debug("Lock acquired: %s (owner=%s)", key, self._lock_owner_id)
                span.set_attribute("db.lock.acquired", True)
            else:
                # Revoke the unused lease
                try:
                    await loop.run_in_executor(None, lease.revoke)
                except Exception:
                    logger.debug("cleanup error revoking unused lease after lock contention", exc_info=True)
                logger.debug("Lock contention: %s", key)
                span.set_attribute("db.lock.acquired", False)

            span.set_status(StatusCode.OK)
            return acquired

    async def _release_lock(self, key: str) -> None:
        """Release a distributed lock.

        Args:
            key: Lock key (relative)
        """
        tracer = get_tracer()
        with tracer.start_as_current_span(
            "etcd.lock.release",
            attributes={"db.operation": "lock_release", "db.key": key},
        ) as span:
            lock_key = self._lock_key(key)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._client.delete, lock_key
            )
            lease = self._leases.pop(f"_lock_{key}", None)
            if lease:
                try:
                    await loop.run_in_executor(None, lease.revoke)
                except Exception:
                    logger.debug("cleanup error revoking lease during lock release", exc_info=True)
            logger.debug("Lock released: %s", key)
            span.set_status(StatusCode.OK)

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh TTL on an existing key by refreshing its lease.

        Args:
            key: The key to refresh (without prefix)
            ttl: New TTL in seconds

        Returns:
            True if key exists and TTL was refreshed
        """
        lease = self._leases.get(key)
        if lease is not None:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lease.refresh)
                return True
            except Exception:
                logger.warning("Lease refresh failed for key %s, falling back to re-write", key, exc_info=True)

        # Fallback: re-read and re-write with new TTL
        value = await self.get(key)
        if value is None:
            return False
        await self.set(key, value, ttl=ttl)
        return True
