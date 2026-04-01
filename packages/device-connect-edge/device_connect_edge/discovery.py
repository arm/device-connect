"""Device-to-device discovery via presence announcements.

When no infrastructure (registry, etcd, router) is available, devices can
discover each other directly using presence messages over the messaging layer.

Zenoh supports device-to-device multicast scouting out of the box — devices on
the same LAN find each other without any broker.  This module adds structured
presence on top of that raw connectivity so that ``discover_devices()`` and
``DeviceDriver.list_devices()`` work without a registry service.

Three classes:

* ``PresenceAnnouncer`` — publishes device metadata periodically
* ``PresenceCollector`` — subscribes to presence and maintains a peer table
* ``D2DRegistry`` — drop-in replacement for ``RegistryClient`` backed by
  the collector

Usage from DeviceRuntime (automatic when D2D mode is detected)::

    announcer = PresenceAnnouncer(messaging, device_id, tenant, caps, identity, status)
    collector = PresenceCollector(messaging, tenant)
    await announcer.start()
    await collector.start()

    # Same interface as RegistryClient
    registry = D2DRegistry(collector)
    devices = await registry.list_devices()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Timing constants ─────────────────────────────────────────────

BURST_INTERVAL = 0.5       # 2 Hz during startup burst
BURST_DURATION = 10.0      # seconds of fast announcements after start / new peer
STEADY_INTERVAL = 5.0      # slow cadence once stable
PEER_TIMEOUT = 30.0        # prune peers not seen for this long


class PresenceAnnouncer:
    """Publishes device presence metadata at an adaptive rate.

    Starts with a 2 Hz burst for fast discovery, then backs off to once
    every 5 s.  A burst is re-triggered when the companion
    ``PresenceCollector`` detects a new peer (call ``trigger_burst()``).

    Args:
        messaging: Connected ``MessagingClient`` instance.
        device_id: This device's unique identifier.
        tenant: Device Connect tenant/namespace.
        capabilities: ``DeviceCapabilities.model_dump()`` dict.
        identity: Device identity dict.
        status: Device status dict.
    """

    def __init__(
        self,
        messaging,  # MessagingClient (avoid import for loose coupling)
        device_id: str,
        tenant: str,
        capabilities: dict,
        identity: dict,
        status: dict,
    ):
        self._messaging = messaging
        self._device_id = device_id
        self._tenant = tenant
        self._capabilities = capabilities
        self._identity = identity
        self._status = status
        self._task: Optional[asyncio.Task] = None
        self._probe_sub = None
        self._burst_until: float = 0.0

    @property
    def subject(self) -> str:
        return f"device-connect.{self._tenant}.{self._device_id}.presence"

    def _build_payload(self) -> bytes:
        payload = {
            "device_id": self._device_id,
            "capabilities": self._capabilities,
            "identity": self._identity,
            "status": {
                **self._status,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "ts": time.time(),
            "d2d": True,
        }
        return json.dumps(payload).encode()

    def trigger_burst(self) -> None:
        """Re-enter the fast announcement phase (called on new peer)."""
        self._burst_until = time.time() + BURST_DURATION

    @property
    def probe_subject(self) -> str:
        return f"device-connect.{self._tenant}.discovery.probe"

    async def start(self) -> None:
        if self._task is not None:
            return
        self._burst_until = time.time() + BURST_DURATION
        self._probe_sub = await self._messaging.subscribe(
            self.probe_subject, self._on_probe, subscribe_only=True,
        )
        self._task = asyncio.create_task(self._loop())
        logger.info("D2D presence announcer started for %s", self._device_id)

    async def _on_probe(self, data: bytes, reply_subject: Optional[str] = None) -> None:
        """Respond to a discovery probe with an immediate presence publish."""
        logger.debug("Discovery probe received, responding for %s", self._device_id)
        try:
            await self._messaging.publish(self.subject, self._build_payload())
        except Exception as exc:
            logger.debug("Immediate presence publish failed: %s", exc)

    async def stop(self) -> None:
        if self._probe_sub is not None:
            try:
                await self._probe_sub.unsubscribe()
            except Exception:
                pass
            self._probe_sub = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self._messaging.publish(self.subject, self._build_payload())
                except Exception as exc:
                    logger.debug("Presence publish failed: %s", exc)

                if time.time() < self._burst_until:
                    await asyncio.sleep(BURST_INTERVAL)
                else:
                    await asyncio.sleep(STEADY_INTERVAL)
        except asyncio.CancelledError:
            raise


class PresenceCollector:
    """Subscribes to presence messages and maintains an in-memory peer table.

    Args:
        messaging: Connected ``MessagingClient`` instance.
        tenant: Device Connect tenant/namespace.
        on_new_peer: Optional callback invoked with ``device_id`` when a
            previously-unknown peer is seen.
    """

    def __init__(
        self,
        messaging,
        tenant: str,
        on_new_peer: Optional[Callable[[str], None]] = None,
        device_id: str = "",
    ):
        self._messaging = messaging
        self._tenant = tenant
        self._on_new_peer = on_new_peer
        self._device_id = device_id
        self._log_tag = f"D2D [{device_id}]" if device_id else "D2D"
        self._peers: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._sub = None
        self._prune_task: Optional[asyncio.Task] = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        subject = f"device-connect.{self._tenant}.*.presence"
        self._sub = await self._messaging.subscribe(subject, self._on_presence, subscribe_only=True)
        self._prune_task = asyncio.create_task(self._prune_loop())
        self._started = True
        logger.info("%s: listening on %s", self._log_tag, subject)

    async def stop(self) -> None:
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
            self._sub = None
        if self._prune_task is not None:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
            self._prune_task = None
        self._started = False

    async def _on_presence(self, data: bytes, reply_subject: Optional[str] = None) -> None:
        try:
            payload = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        device_id = payload.get("device_id")
        if not device_id:
            return

        # Handle graceful departure announcements
        if payload.get("departing"):
            async with self._lock:
                removed = self._peers.pop(device_id, None)
            if removed:
                logger.info("%s: peer %s departed gracefully", self._log_tag, device_id)
            return

        payload["_last_seen"] = time.time()

        async with self._lock:
            is_new = device_id not in self._peers
            self._peers[device_id] = payload

        if is_new:
            logger.info("%s: discovered peer %s", self._log_tag, device_id)
            if self._on_new_peer:
                try:
                    self._on_new_peer(device_id)
                except Exception:
                    pass

    async def _prune_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(PEER_TIMEOUT / 2)
                now = time.time()
                async with self._lock:
                    stale = [
                        did for did, info in self._peers.items()
                        if now - info.get("_last_seen", 0) > PEER_TIMEOUT
                    ]
                    for did in stale:
                        del self._peers[did]
                        logger.info("%s: peer %s timed out", self._log_tag, did)
        except asyncio.CancelledError:
            raise

    async def list_devices(
        self,
        *,
        device_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return currently known peers, optionally filtered by device_type."""
        async with self._lock:
            results = list(self._peers.values())

        if device_type:
            filtered = []
            for d in results:
                dt = (d.get("identity") or {}).get("device_type", "")
                if device_type.lower() in dt.lower():
                    filtered.append(d)
            results = filtered

        return results

    async def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return self._peers.get(device_id)

    async def send_discovery_probe(self) -> None:
        """Publish a discovery probe to trigger immediate presence responses."""
        subject = f"device-connect.{self._tenant}.discovery.probe"
        payload = json.dumps({"probe": True, "ts": time.time()}).encode()
        try:
            await self._messaging.publish(subject, payload)
        except Exception as exc:
            logger.debug("Discovery probe publish failed: %s", exc)

    async def wait_for_peers(self, timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Wait up to *timeout* seconds for at least one peer, then return all."""
        await self.send_discovery_probe()
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with self._lock:
                if self._peers:
                    return list(self._peers.values())
            await asyncio.sleep(0.25)
        async with self._lock:
            return list(self._peers.values())

    async def wait_for_device_type(
        self, device_type: str, timeout: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        """Wait until a peer of the given *device_type* appears.

        Sends a discovery probe and polls the peer table at 0.25 s intervals.

        Returns:
            The first matching peer dict, or ``None`` if *timeout* expires.

        Raises:
            RuntimeError: If the collector has not been started.
        """
        if not self._started:
            raise RuntimeError("PresenceCollector not started — call start() first")
        await self.send_discovery_probe()
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = await self.list_devices(device_type=device_type)
            if matches:
                return matches[0]
            await asyncio.sleep(0.25)
        return None

    async def wait_for_device_id(
        self, device_id: str, timeout: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        """Wait until a specific *device_id* appears in the peer table.

        Returns:
            The peer dict, or ``None`` if *timeout* expires.

        Raises:
            RuntimeError: If the collector has not been started.
        """
        if not self._started:
            raise RuntimeError("PresenceCollector not started — call start() first")
        await self.send_discovery_probe()
        deadline = time.time() + timeout
        while time.time() < deadline:
            peer = await self.get_device(device_id)
            if peer is not None:
                return peer
            await asyncio.sleep(0.25)
        return None


class D2DRegistry:
    """Drop-in replacement for ``RegistryClient`` backed by a ``PresenceCollector``.

    Exposes the same ``list_devices`` / ``get_device`` interface so that
    ``DeviceDriver.list_devices()`` works transparently in D2D mode.
    """

    def __init__(self, collector: PresenceCollector):
        self._collector = collector

    async def list_devices(
        self,
        *,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        return await self._collector.list_devices(device_type=device_type)

    async def get_device(
        self,
        device_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self._collector.get_device(device_id)
