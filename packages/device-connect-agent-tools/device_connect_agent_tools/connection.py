# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Messaging connection management for Device Connect tools.

Uses device_connect_edge.messaging for the underlying connection, credential
resolution, and TLS setup.  Adds a sync-to-async bridge so that Strands
@tool functions (which must be synchronous) can call async operations,
plus auto-discovery of credentials from well-known project paths.

Supports NATS, Zenoh, and MQTT backends.  The backend is auto-detected
from environment variables (MESSAGING_BACKEND, ZENOH_CONNECT) or can
be set explicitly.

Usage:
    from device_connect_agent_tools.connection import connect, disconnect, get_connection

    connect()  # auto-detects backend from env
    conn = get_connection()
    devices = conn.list_devices()
    result = conn.invoke("camera-001", "capture_image", params={"resolution": "1080p"})
    disconnect()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from device_connect_edge.messaging import create_client, MessagingClient
from device_connect_edge.messaging.config import MessagingConfig
from device_connect_edge.discovery_provider import DiscoveryProvider
from device_connect_edge.registry_client import RegistryClient as _SDKRegistryClient

logger = logging.getLogger(__name__)

# ── Module-level singleton ──────────────────────────────────────────

_lock = threading.Lock()
_connection: Optional[DeviceConnection] = None  # forward ref resolved below


# ── Well-known search paths for auto-discovery ─────────────────────

# Credential file names to look for (in order of preference)
_WELL_KNOWN_CRED_FILES = [
    "orchestrator.creds.json",
    "devctl.creds.json",
    "orchestrator.creds",
    "devctl.creds",
]

_WELL_KNOWN_CA_FILES = [
    "ca-cert.pem",
    "ca.pem",
]


def _find_device_connect_root() -> Optional[Path]:
    """Walk up from CWD looking for a Device Connect project root.

    Heuristic: a directory that contains ``security_infra/credentials/``.
    """
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        if (d / "security_infra" / "credentials").is_dir():
            return d
        if (d / "core" / "security_infra" / "credentials").is_dir():
            return d / "core"
        # Stop once we hit the home directory
        if d == Path.home():
            break
    return None


def _auto_discover_credentials() -> Optional[Dict[str, Any]]:
    """Search well-known paths for a messaging credential (any backend)."""
    root = _find_device_connect_root()
    if root is None:
        return None

    creds_dir = root / "security_infra" / "credentials"
    for name in _WELL_KNOWN_CRED_FILES:
        path = creds_dir / name
        if path.exists():
            logger.debug("Auto-discovered credentials: %s", path)
            return MessagingConfig._load_credentials_file(str(path))
    return None


def _auto_discover_tls() -> Optional[Dict[str, Any]]:
    """Search well-known paths for the CA certificate."""
    root = _find_device_connect_root()
    if root is None:
        return None

    certs_dir = root / "security_infra" / "certs"
    for name in _WELL_KNOWN_CA_FILES:
        path = certs_dir / name
        if path.exists():
            logger.debug("Auto-discovered CA cert: %s", path)
            return {"ca_file": str(path)}
    return None


def _resolve_credentials_file_env() -> Optional[str]:
    """Path to the messaging credentials file from the environment.

    ``MESSAGING_CREDENTIALS_FILE`` is the backend-neutral name; the older
    ``NATS_CREDENTIALS_FILE`` is honored as a deprecated alias (mirrors the
    edge device runtime). The credential isn't backend-specific -- the same
    ``*.creds.json`` carries a per-backend sub-object (zenoh/nats/mqtt).
    """
    new = os.getenv("MESSAGING_CREDENTIALS_FILE")
    if new:
        return new
    old = os.getenv("NATS_CREDENTIALS_FILE")
    if old:
        logger.warning(
            "NATS_CREDENTIALS_FILE is deprecated; set MESSAGING_CREDENTIALS_FILE "
            "instead (the old name still works for now)."
        )
    return old


def _read_creds_file(path: str) -> Dict[str, Any]:
    """Load a ``*.creds.json`` file as a dict (empty dict on any error)."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _creds_backend_section(creds: Dict[str, Any], backend: Optional[str]) -> Dict[str, Any]:
    """The credential's per-backend sub-object (zenoh/nats/mqtt)."""
    section = creds.get(backend or "") or creds.get("zenoh") or creds.get("nats") or {}
    return section if isinstance(section, dict) else {}


def _tls_config_from_creds(creds: Dict[str, Any], backend: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract a tls_config from a credential's per-backend ``tls`` block.

    Backend-neutral: honors both file paths (ca_file/cert_file/key_file) and
    inline PEM (ca_pem/cert_pem/key_pem), exactly like the edge device runtime,
    so Zenoh mTLS works straight from a downloaded credential -- no explicit
    tls_config needed.
    """
    tls = _creds_backend_section(creds, backend).get("tls")
    if not isinstance(tls, dict):
        return None
    out = {
        k: tls[k]
        for k in ("ca_file", "cert_file", "key_file", "ca_pem", "cert_pem", "key_pem")
        if tls.get(k)
    }
    return out or None


# ── Device payload helper ──────────────────────────────────────────


def flatten_device(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a raw registry payload into a consistent device dict.

    The registry stores device_type inside ``identity`` and location
    inside ``status``.  Promote them to the top level so callers can
    use ``d["device_type"]`` directly (matching the core RegistryClient
    DeviceInfo convention).
    """
    identity = raw.get("identity") or {}
    status = raw.get("status") or {}
    caps = raw.get("capabilities") or {}

    # Mirror the legacy DeviceStatus.location field into labels["location"]
    # when the driver did not declare it via DeviceCapabilities.labels. Drivers
    # using only the legacy field would otherwise be invisible to selector
    # queries on location.
    legacy_location = raw.get("location") or status.get("location")
    caps_labels = caps.get("labels")
    merged_labels = caps_labels
    if legacy_location and (not caps_labels or "location" not in caps_labels):
        merged_labels = {**(caps_labels or {}), "location": legacy_location}

    # NOTE: The raw ``capabilities`` dict is intentionally NOT included in
    # the flattened output.  ``functions`` and ``events`` are extracted to
    # the top level for direct access.  Including both would duplicate data
    # and waste LLM context tokens.  (Tried and reverted — do not re-add.)
    return {
        "device_id": raw.get("device_id"),
        "device_type": raw.get("device_type") or identity.get("device_type"),
        "location": legacy_location,
        "status": status,
        "identity": identity,
        "functions": caps.get("functions", []),
        "events": caps.get("events", []),
        # Discovery labels declared by the driver (DeviceCapabilities.labels),
        # with status.location mirrored in when caps did not carry it. None
        # when neither source provided any label -- discover() treats that
        # as "no label-based match," not "matches everything."
        "labels": merged_labels,
    }


# ── Message parsing helpers (extracted for testability) ─────────────


def parse_buffered_payload(data: bytes) -> dict:
    """Parse raw bytes into a payload dict for buffered subscriptions.

    Always returns a dict — falls back to ``{"raw": ...}`` on any error.
    """
    try:
        payload = json.loads(data.decode())
        if not isinstance(payload, dict):
            payload = {"raw": str(payload)[:500]}
    except Exception:
        payload = {"raw": data.decode("utf-8", errors="replace")[:500]}
    return payload


def parse_event_payload(data: bytes) -> dict:
    """Parse raw bytes into a normalized event dict.

    Returns dict with keys ``device_id``, ``event_name``, ``params``.
    Raises on malformed input (caller should catch).
    """
    payload = json.loads(data.decode())
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    method = payload.get("method", "")
    # JSON-RPC allows ``params`` to be omitted, null, an object, or an array.
    # ``.get(default)`` only fires when the key is absent — an explicit ``null``
    # returns ``None``, which would crash a chained ``.get``. Same for array
    # params. Normalize to a dict so the device_id lookup is safe.
    raw_params = payload.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    dev_id = params.get("device_id", "unknown")
    return {"device_id": dev_id, "event_name": method, "params": params}


# ── Connection class ────────────────────────────────────────────────


class DeviceConnection:
    """Async messaging client with a dedicated event loop thread for sync callers."""

    def __init__(
        self,
        nats_url: Optional[str] = None,
        zone: Optional[str] = None,
        credentials: Optional[Dict[str, Any]] = None,
        tls_config: Optional[Dict[str, Any]] = None,
        request_timeout: float = 30.0,
    ):
        self._request_timeout = request_timeout

        # Resolve config: explicit params -> env vars (via MessagingConfig) -> auto-discovery
        config = MessagingConfig(
            servers=[nats_url] if nats_url else None,
            credentials=credentials,
            tls_config=tls_config,
        )

        self._backend = config.backend  # "nats", "zenoh", or "mqtt" (auto-detected)
        self._servers = config.servers
        self._credentials = config.credentials
        self._tls_config = config.tls_config

        env_has_urls = bool(
            nats_url or os.getenv("ZENOH_CONNECT") or os.getenv("MESSAGING_URLS")
            or os.getenv("NATS_URL") or os.getenv("NATS_URLS")
        )

        # Read the messaging credentials file once (MESSAGING_CREDENTIALS_FILE,
        # or the deprecated NATS_CREDENTIALS_FILE). The same *.creds.json a
        # device uses carries the tenant, per-backend mTLS material, and the
        # broker URL -- so an external agent self-configures from it the same
        # way a device does. Explicit params and env URLs/TLS still take
        # precedence.
        file_data: Dict[str, Any] = {}
        creds_file = _resolve_credentials_file_env()
        if creds_file and Path(creds_file).exists():
            file_data = _read_creds_file(creds_file)

        # Zone/tenant: explicit arg -> the credential's tenant -> TENANT env ->
        # "default". A bare "default" parameter default would have masked the
        # credential's tenant, silently sending external agents to
        # device-connect.default.* instead of their actual tenant.
        self.zone = zone or file_data.get("tenant") or os.environ.get("TENANT") or "default"

        creds_has_url = False
        if file_data and (credentials is None or tls_config is None):
            if self._tls_config is None:
                self._tls_config = _tls_config_from_creds(file_data, self._backend)
            if not env_has_urls:
                creds_urls = _creds_backend_section(file_data, self._backend).get("urls")
                if creds_urls:
                    self._servers = list(creds_urls)
                    creds_has_url = True

        # Last resort: well-known-path auto-discovery.
        if self._credentials is None:
            self._credentials = _auto_discover_credentials()
        if self._tls_config is None:
            self._tls_config = _auto_discover_tls()

        # If no explicit/credential server URL was given but TLS was discovered,
        # default to tls:// instead of nats://
        if not env_has_urls and not creds_has_url and self._tls_config:
            self._servers = ["tls://localhost:4222"]

        self._client: Optional[MessagingClient] = None
        self._provider: Optional[DiscoveryProvider] = None
        self._inbox: Dict[str, List[Dict[str, Any]]] = {}
        self._sync_subs: Dict[str, Any] = {}

        # D2D mode: discover devices via presence instead of a registry/router.
        # A router URL from env OR the credential means we are NOT in D2D.
        no_explicit_urls = not env_has_urls and not creds_has_url
        self._d2d_mode = (
            os.getenv("DEVICE_CONNECT_DISCOVERY_MODE", "").lower() in ("d2d", "p2p")
            or (self._backend == "zenoh" and no_explicit_urls)
        )
        self._d2d_collector = None  # lazy-initialized PresenceCollector

        # In D2D mode with Zenoh and no explicit URLs, use empty servers (multicast scouting).
        # When DEVICE_CONNECT_DISCOVERY_MODE=d2d is forced alongside a router URL,
        # keep the URL so we can still communicate with devices connected to it.
        if self._d2d_mode and self._backend == "zenoh" and no_explicit_urls:
            self._servers = []

        # Dedicated event loop for sync-to-async bridging
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="device-connect-agent-tools-loop",
        )
        self._thread.start()

    def _run(self, coro):
        """Run an async coroutine from a sync context."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def connect(self) -> None:
        """Establish the messaging connection."""
        self._run(self._async_connect())

    async def _async_connect(self) -> None:
        self._client = create_client(backend=self._backend)
        await self._client.connect(
            servers=self._servers,
            credentials=self._credentials,
            tls_config=self._tls_config,
        )
        logger.info("Connected to %s at %s", self._backend, self._servers)

        # Initialize discovery provider
        if self._d2d_mode:
            from device_connect_edge.discovery import PresenceCollector, D2DRegistry
            self._d2d_collector = PresenceCollector(self._client, self.zone)
            await self._d2d_collector.start()
            await self._d2d_collector.wait_for_peers(timeout=3.0)
            self._provider = D2DRegistry(self._d2d_collector)
        else:
            self._provider = _SDKRegistryClient(
                self._client,
                tenant=self.zone,
                timeout=self._request_timeout,
                cache_ttl=30.0,
            )

    def close(self) -> None:
        """Close the connection and shut down the event loop thread."""
        if self._loop is None or self._loop.is_closed():
            return

        self._loop.set_exception_handler(lambda loop, ctx: None)

        try:
            future = asyncio.run_coroutine_threadsafe(self._async_close(), self._loop)
            future.result(timeout=2.0)
        except Exception:
            logger.debug("cleanup error during async close", exc_info=True)

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

        try:
            self._loop.close()
        except Exception:
            logger.debug("cleanup error closing event loop", exc_info=True)
        finally:
            self._loop = None

    async def _async_close(self) -> None:
        if self._d2d_collector:
            try:
                await self._d2d_collector.stop()
            except Exception:
                logger.debug("cleanup error stopping D2D collector", exc_info=True)
            self._d2d_collector = None
        self._provider = None
        if self._client:
            try:
                await asyncio.wait_for(self._client.close(), timeout=2.0)
            except Exception:
                logger.debug("cleanup error closing messaging client", exc_info=True)
        self._client = None

    # ── Device operations (sync wrappers) ───────────────────────────

    def list_devices(
        self,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List devices via the discovery provider (D2D or registry)."""
        return self._run(self._async_list_devices(device_type, location))

    async def _async_list_devices(
        self,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if self._provider is None:
            raise RuntimeError("Not connected — call connect() first")
        devices = await self._provider.list_devices(
            device_type=device_type, location=location,
        )
        return [flatten_device(d) for d in devices]

    def invalidate_cache(self) -> None:
        """Invalidate the provider's device cache, if supported."""
        if self._provider and hasattr(self._provider, "invalidate_cache"):
            self._provider.invalidate_cache()

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific device by ID."""
        return self._run(self._async_get_device(device_id))

    async def _async_get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        if self._provider is None:
            raise RuntimeError("Not connected — call connect() first")
        device = await self._provider.get_device(device_id)
        if device:
            return flatten_device(device)
        return None

    def invoke(
        self,
        device_id: str,
        function: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Invoke a function on a device via JSON-RPC."""
        return self._run(
            self._async_invoke(device_id, function, params, timeout)
        )

    async def _async_invoke(
        self,
        device_id: str,
        function: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        timeout = timeout or self._request_timeout
        subject = f"device-connect.{self.zone}.{device_id}.cmd"
        req_id = f"d2d-{uuid.uuid4().hex[:12]}"

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": function,
            "params": params or {},
        }

        response_data = await self._client.request(
            subject, json.dumps(rpc_payload).encode(), timeout=timeout,
        )
        return json.loads(response_data)

    # ── Broadcast ────────────────────────────────────────────────────

    def publish_broadcast(self, envelope: Dict[str, Any]) -> None:
        """Publish a selector-driven broadcast envelope to the fanout subject.

        The envelope shape is documented in
        ``device_connect_edge.device.DeviceRuntime._broadcast_subscription``;
        every device subscribed to ``device-connect.<tenant>.broadcast``
        receives the message and self-elects via ``targets`` and
        the optional ``where`` predicate.
        """
        return self._run(self._async_publish_broadcast(envelope))

    async def _async_publish_broadcast(self, envelope: Dict[str, Any]) -> None:
        subject = f"device-connect.{self.zone}.broadcast"
        await self._client.publish(subject, json.dumps(envelope).encode())

    def broadcast(
        self,
        function: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> List[Dict[str, Any]]:
        """Invoke a function on all discovered devices and collect results.

        Sequential sync fan-out (one invoke per device). Predates the
        selector-driven broadcast tool; left in place for callers that want
        a simple "call this on everyone" without setting up subscriptions.
        """
        devices = self.list_devices()
        results = []
        for d in devices:
            device_id = d["device_id"]
            try:
                r = self.invoke(device_id, function, params, timeout=timeout)
                results.append({"device_id": device_id, "result": r})
            except Exception as e:
                results.append({"device_id": device_id, "error": str(e)})
        return results

    # ── Sync subscription + inbox ────────────────────────────────────

    def subscribe_buffered(
        self,
        subject: str,
        name: Optional[str] = None,
    ) -> str:
        """Subscribe to a messaging subject, buffering messages in the inbox.

        Args:
            subject: Subject pattern (supports ``*`` and ``>`` wildcards).
            name: Inbox key for buffered messages (defaults to subject).

        Returns:
            The inbox name.
        """
        name = name or subject
        self._inbox[name] = []

        async def _do_subscribe():
            async def _on_msg(data: bytes, msg_subject: str, reply: str = ""):
                payload = parse_buffered_payload(data)
                # Store as (subject, data) tuple
                self._inbox[name].append((msg_subject, payload))
                # Trim to prevent unbounded growth
                if len(self._inbox[name]) > 1000:
                    self._inbox[name] = self._inbox[name][-500:]

            return await self._client.subscribe_with_subject(subject, callback=_on_msg)

        self._sync_subs[name] = self._run(_do_subscribe())
        logger.debug("Sync subscription created: %s -> inbox[%s]", subject, name)
        return name

    def unsubscribe_buffered(self, name: str) -> None:
        """Unsubscribe a buffered subscription by inbox name."""
        sub = self._sync_subs.pop(name, None)
        if sub is not None:
            try:
                self._run(sub.unsubscribe())
            except Exception:
                logger.debug("cleanup error during buffered unsubscribe", exc_info=True)
        self._inbox.pop(name, None)

    def get_inbox(
        self, name: Optional[str] = None,
    ) -> Dict[str, list]:
        """Get buffered messages from sync subscriptions.

        Args:
            name: Specific inbox key, or None for all inboxes.

        Returns:
            Dict mapping inbox names to lists of ``(subject, data)`` tuples.
        """
        if name is not None:
            return {name: list(self._inbox.get(name, []))}
        return {k: list(v) for k, v in self._inbox.items()}

    # ── Async subscription ──────────────────────────────────────────

    async def async_subscribe(self, subject: str, callback: Callable) -> Any:
        """Subscribe to a messaging subject with an async callback.

        Args:
            subject: Subject pattern (supports * and > wildcards).
            callback: Async function receiving (data: bytes, reply: str).

        Returns:
            Subscription handle.
        """
        return await self._client.subscribe(subject, callback=callback)

    async def async_subscribe_with_subject(self, subject: str, callback: Callable) -> Any:
        """Subscribe to a messaging subject with a callback that receives the subject.

        Args:
            subject: Subject pattern (supports * and > wildcards).
            callback: Async function receiving (data: bytes, subject: str, reply: str).

        Returns:
            Subscription handle.
        """
        return await self._client.subscribe_with_subject(subject, callback=callback)

    async def subscribe_events(
        self,
        batch_window: float = 12.0,
        device_id: Optional[str] = None,
    ):
        """Subscribe to device events and yield parsed batches.

        An async generator that subscribes to Device Connect device events,
        collects them into time-windowed batches, and yields each
        batch as a list of dicts.

        Args:
            batch_window: Seconds to collect events before yielding a
                batch (default: 12).
            device_id: Optional device ID filter. If ``None``, subscribes
                to events from all devices.

        Yields:
            List of event dicts, each with keys:
              - ``device_id`` (str)
              - ``event_name`` (str)
              - ``params`` (dict)

        Example::

            conn = get_connection()
            async for batch in conn.subscribe_events(batch_window=15):
                for event in batch:
                    print(f"{event['device_id']}::{event['event_name']}")
        """
        buffer: asyncio.Queue = asyncio.Queue()

        async def _on_msg(data: bytes, reply: str = ""):
            try:
                event = parse_event_payload(data)
                logger.info(
                    "EVENT <- %s::%s  %s",
                    event["device_id"], event["event_name"],
                    json.dumps(event["params"], default=str),
                )
                await buffer.put(event)
            except Exception as e:
                logger.error("Error parsing event: %s", e)

        # Build subject pattern
        dev_pattern = device_id if device_id else "*"
        subject = f"device-connect.{self.zone}.{dev_pattern}.event.>"
        sub = await self._client.subscribe(subject, callback=_on_msg)
        logger.info("Subscribed to %s", subject)

        try:
            while True:
                # Wait for the first event
                try:
                    first = await asyncio.wait_for(buffer.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                batch = [first]

                # Collect more events within the batch window
                await asyncio.sleep(batch_window)
                while not buffer.empty():
                    try:
                        batch.append(buffer.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                yield batch
        finally:
            await sub.unsubscribe()

    # ── Properties ──────────────────────────────────────────────────

    @property
    def nc(self) -> Optional[Any]:
        """Raw underlying client (for advanced use).

        .. deprecated:: Use ``messaging_client`` instead.
        """
        import warnings
        warnings.warn(
            "conn.nc is deprecated. Use conn.messaging_client instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if self._client and hasattr(self._client, '_nc'):
            return self._client._nc
        return None

    @property
    def messaging_client(self) -> Optional[MessagingClient]:
        """The device_connect_edge MessagingClient instance."""
        return self._client

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop


# ── Public API ──────────────────────────────────────────────────────


def connect(
    nats_url: Optional[str] = None,
    zone: Optional[str] = None,
    credentials: Optional[Dict[str, Any]] = None,
    tls_config: Optional[Dict[str, Any]] = None,
    request_timeout: float = 30.0,
) -> None:
    """Initialize the messaging connection.

    The backend (NATS, Zenoh, MQTT) is auto-detected from environment
    variables or can be set via MESSAGING_BACKEND.

    Resolution order (for each setting):
      1. Explicit parameter
      2. Environment variable
      3. Auto-discovery from well-known paths

    Environment variables:
      - MESSAGING_BACKEND — "nats", "zenoh", or "mqtt" (auto-detected)
      - MESSAGING_URLS    — broker URLs (comma-separated)
      - ZENOH_CONNECT     — Zenoh endpoints (auto-selects zenoh backend)
      - NATS_URL          — NATS broker URL (legacy)
      - TENANT            — Device Connect zone/namespace

    Args:
        nats_url: Broker URL (works for any backend despite the name).
        zone: Device Connect tenant/zone namespace. When omitted, resolved from
            the credential's ``tenant`` field, then the TENANT env var, then
            "default".
        credentials: Auth credentials dict.
        tls_config: TLS configuration dict.
        request_timeout: Default timeout for device RPC calls.
    """
    global _connection
    with _lock:
        if _connection is not None:
            return
        # zone resolution (explicit -> credential tenant -> TENANT env ->
        # "default") happens in DeviceConnection, which can see the creds file.
        conn = DeviceConnection(
            nats_url=nats_url,
            zone=zone,
            credentials=credentials,
            tls_config=tls_config,
            request_timeout=request_timeout,
        )
        conn.connect()
        _connection = conn


def disconnect() -> None:
    """Close the messaging connection and release resources."""
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def get_connection() -> DeviceConnection:
    """Get the current connection, auto-connecting if needed."""
    global _connection
    if _connection is None:
        connect()
    return _connection
