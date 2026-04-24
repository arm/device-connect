# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""State store abstract base class.

This module defines the StateStore ABC, which provides a pluggable
interface for key-value state storage with TTL and distributed locks.

Implementations can use various backends:
    - etcd3 (recommended for distributed coordination)
    - Redis
    - PostgreSQL
    - DynamoDB

Example:
    store = EtcdStateStore(host="localhost")
    await store.connect()

    # Key-value operations
    await store.set("task/123", {"status": "running"}, ttl=300)
    data = await store.get("task/123")

    # Prefix queries
    all_tasks = await store.get_prefix("task/")

    # Distributed locks
    async with store.lock("resource/printer-1", ttl=60) as acquired:
        if acquired:
            await do_work()

    await store.close()
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional


class StateStore(ABC):
    """Abstract base class for state storage.

    Provides key-value storage with TTL and distributed locks.
    Used for experiment state, resource coordination, device
    lease management, and other distributed coordination needs.

    All methods are async to support non-blocking I/O with
    various storage backends.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to storage backend.

        Should be called before any other operations.

        Raises:
            ConnectionError: If connection fails
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close connection and cleanup resources.

        Should be called during shutdown to release connections.
        """
        pass

    @abstractmethod
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get value for key.

        Args:
            key: The key to retrieve

        Returns:
            The stored value as a dict, or None if not found
        """
        pass

    @abstractmethod
    async def get_prefix(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """Get all key-value pairs matching prefix.

        Args:
            prefix: Key prefix to match (e.g., "devices/")

        Returns:
            Dict mapping keys to their values
        """
        pass

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl: Optional[int] = None
    ) -> None:
        """Set value for key with optional TTL.

        Args:
            key: The key to set
            value: The value to store (will be serialized)
            ttl: Time-to-live in seconds (None = no expiry)
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete key.

        Args:
            key: The key to delete

        Returns:
            True if key existed and was deleted, False otherwise
        """
        pass

    @asynccontextmanager
    async def lock(
        self,
        key: str,
        ttl: int = 60
    ) -> AsyncIterator[bool]:
        """Acquire a distributed lock.

        Use as an async context manager. Yields True if lock was
        acquired, False if lock could not be acquired (already held).
        Lock is automatically released on context exit.

        Args:
            key: Lock key/name
            ttl: Lock time-to-live in seconds (auto-release safety)

        Yields:
            True if lock acquired, False otherwise

        Example:
            async with store.lock("resource/printer", ttl=60) as acquired:
                if acquired:
                    await use_printer()
                else:
                    print("Printer busy, try later")
        """
        acquired = await self._try_acquire_lock(key, ttl)
        try:
            yield acquired
        finally:
            if acquired:
                await self._release_lock(key)

    @abstractmethod
    async def _try_acquire_lock(self, key: str, ttl: int) -> bool:
        """Internal: Try to acquire lock.

        Args:
            key: Lock key
            ttl: Lock TTL in seconds

        Returns:
            True if lock acquired, False if already held
        """
        pass

    @abstractmethod
    async def _release_lock(self, key: str) -> None:
        """Internal: Release lock.

        Args:
            key: Lock key to release
        """
        pass

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh TTL on an existing key.

        Useful for keeping device leases alive via heartbeats.

        Args:
            key: The key to refresh
            ttl: New TTL in seconds

        Returns:
            True if key exists and TTL was refreshed, False otherwise
        """
        # Default implementation: get and re-set
        # Implementations should override for atomic refresh
        value = await self.get(key)
        if value is None:
            return False
        await self.set(key, value, ttl=ttl)
        return True
