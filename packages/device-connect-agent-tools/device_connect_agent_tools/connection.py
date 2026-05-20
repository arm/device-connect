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
    """Search well-known paths for NATS credentials."""
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


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _portal_credentials_path() -> Optional[str]:
    """Return a portal-issued credential bundle path, if configured."""
    for env_name in (
        "DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE",
        "DEVICE_CONNECT_CREDENTIALS_FILE",
    ):
        value = os.getenv(env_name)
        if value:
            return value
    return None


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


def load_portal_credentials_file(path: str | Path) -> Dict[str, Any]:
    """Load a portal-issued credential bundle.

    The portal remains the authority for identity and policy, but may include
    scoped local Zenoh route material for same-LAN unicast access. The returned
    shape separates the portal route from the optional local fast path so the
    connection layer can prefer local access and still fall back to the portal.
    """
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in credentials file: {path}")

    tenant = data.get("tenant") or data.get("zone")

    nats_config = data.get("nats") or {}
    portal: Optional[Dict[str, Any]] = None
    if isinstance(nats_config, dict):
        nats_auth = {}
        if nats_config.get("jwt"):
            nats_auth["jwt"] = nats_config["jwt"]
        if nats_config.get("nkey_seed"):
            nats_auth["nkey_seed"] = nats_config["nkey_seed"]

        nats_urls = _as_list(nats_config.get("urls")) or _as_list(nats_config.get("url"))
        if nats_urls or nats_auth:
            portal = {
                "backend": "nats",
                "servers": nats_urls,
                "credentials": nats_auth or None,
                "tls": nats_config.get("tls") or None,
            }

    zenoh_config = data.get("zenoh") or {}
    if not isinstance(zenoh_config, dict):
        zenoh_config = {}
    local_config = data.get("local") or data.get("local_zenoh") or {}
    if not isinstance(local_config, dict):
        local_config = {}

    local_routes = (
        _as_list(data.get("local_routes"))
        or _as_list(local_config.get("routes"))
        or _as_list(local_config.get("urls"))
        or _as_list(zenoh_config.get("local_routes"))
    )
    local_tls = local_config.get("tls") or zenoh_config.get("tls") or None
    local: Optional[Dict[str, Any]] = None
    if local_routes:
        local = {
            "backend": "zenoh",
            "servers": local_routes,
            "credentials": local_config.get("credentials") or None,
            "tls": local_tls,
            "device_id": local_config.get("device_id") or data.get("device_id"),
            "expires_at": local_config.get("expires_at") or data.get("expires_at"),
        }

    return {"tenant": tenant, "portal": portal, "local": local}


def normalize_local_zenoh_dict(data: Any) -> Optional[Dict[str, Any]]:
    """Normalize a ``local_zenoh`` / ``local`` block into a Zenoh route config."""
    if not isinstance(data, dict):
        return None
    routes = (
        _as_list(data.get("routes"))
        or _as_list(data.get("urls"))
        or _as_list(data.get("local_routes"))
    )
    if not routes:
        return None
    return {
        "backend": "zenoh",
        "servers": routes,
        "credentials": data.get("credentials"),
        "tls": data.get("tls"),
    }


def collect_local_route_candidates_from_devices(
    devices: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Collect unique LAN Zenoh route configs advertised in registry device records."""
    seen: set[tuple[str, ...]] = set()
    candidates: List[Dict[str, Any]] = []
    for raw in devices:
        if not isinstance(raw, dict):
            continue
        status = raw.get("status") or {}
        for source in (status.get("local_zenoh"), raw.get("local_zenoh")):
            cfg = normalize_local_zenoh_dict(source)
            if not cfg:
                continue
            key = tuple(cfg["servers"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(cfg)
    return candidates


def _resolve_portal_credentials() -> Optional[Dict[str, Any]]:
    path = _portal_credentials_path()
    if not path:
        return None
    return load_portal_credentials_file(path)


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
        "local_zenoh": status.get("local_zenoh"),
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
        zone: str = "default",
        credentials: Optional[Dict[str, Any]] = None,
        tls_config: Optional[Dict[str, Any]] = None,
        request_timeout: float = 30.0,
    ):
        portal = _resolve_portal_credentials()
        if zone == "default" and portal and portal.get("tenant"):
            zone = portal["tenant"]
        self.zone = zone
        self._request_timeout = request_timeout

        env_has_urls = any(
            os.getenv(name)
            for name in ("ZENOH_CONNECT", "MESSAGING_URLS", "NATS_URL", "NATS_URLS")
        )
        portal_cfg = (portal or {}).get("portal") or {}
        local_cfg = (portal or {}).get("local") or {}
        prefer_local = _env_flag("DEVICE_CONNECT_PREFER_LOCAL", True)
        can_use_portal_bundle = portal is not None and not nats_url and not env_has_urls

        config_backend: Optional[str] = None
        config_servers: Optional[List[str]] = [nats_url] if nats_url else None
        config_credentials = credentials
        config_tls = tls_config
        self._using_local_route = False
        self._fallback_config: Optional[Dict[str, Any]] = None
        self._registry_local_discovery = False
        self._stored_portal_cfg: Optional[Dict[str, Any]] = None

        discover_local_from_registry = _env_flag(
            "DEVICE_CONNECT_DISCOVER_LOCAL_FROM_REGISTRY", True,
        )

        if can_use_portal_bundle and prefer_local and local_cfg.get("servers"):
            config_backend = "zenoh"
            config_servers = local_cfg.get("servers")
            config_credentials = local_cfg.get("credentials")
            config_tls = local_cfg.get("tls")
            self._using_local_route = True
            if portal_cfg.get("servers"):
                self._fallback_config = portal_cfg
        elif (
            can_use_portal_bundle
            and prefer_local
            and discover_local_from_registry
            and portal_cfg.get("servers")
        ):
            self._registry_local_discovery = True
            self._stored_portal_cfg = dict(portal_cfg)
            self._fallback_config = dict(portal_cfg)
            config_backend = portal_cfg.get("backend")
            config_servers = portal_cfg.get("servers")
            config_credentials = credentials or portal_cfg.get("credentials")
            config_tls = tls_config or portal_cfg.get("tls")
        elif can_use_portal_bundle and portal_cfg.get("servers"):
            config_backend = portal_cfg.get("backend")
            config_servers = portal_cfg.get("servers")
            config_credentials = credentials or portal_cfg.get("credentials")
            config_tls = tls_config or portal_cfg.get("tls")

        # Resolve config: explicit params -> env vars (via MessagingConfig) -> auto-discovery
        config_kwargs = {
            "servers": config_servers,
            "credentials": config_credentials,
            "tls_config": config_tls,
        }
        if config_backend:
            config_kwargs["backend"] = config_backend
        config = MessagingConfig(**config_kwargs)

        self._backend = config.backend  # "nats", "zenoh", or "mqtt" (auto-detected)
        self._servers = config.servers
        self._credentials = config.credentials
        self._tls_config = config.tls_config

        # If MessagingConfig didn't find credentials/TLS from env, try auto-discovery
        if self._credentials is None:
            self._credentials = _auto_discover_credentials()
        if self._tls_config is None:
            self._tls_config = _auto_discover_tls()

        # If no explicit server URL was given but TLS was discovered,
        # default to tls:// instead of nats://
        if (
            not self._using_local_route
            and not nats_url
            and not os.getenv("NATS_URL")
            and not os.getenv("NATS_URLS")
            and not os.getenv("MESSAGING_URLS")
            and not os.getenv("ZENOH_CONNECT")
        ):
            if self._tls_config:
                self._servers = ["tls://localhost:4222"]

        self._client: Optional[MessagingClient] = None
        self._provider: Optional[DiscoveryProvider] = None
        self._inbox: Dict[str, List[Dict[str, Any]]] = {}
        self._sync_subs: Dict[str, Any] = {}

        # D2D mode: discover devices via presence instead of registry
        no_explicit_urls = (
            not nats_url
            and not os.getenv("ZENOH_CONNECT")
            and not os.getenv("MESSAGING_URLS")
            and not os.getenv("NATS_URL")
            and not os.getenv("NATS_URLS")
        )
        self._d2d_mode = (
            os.getenv("DEVICE_CONNECT_DISCOVERY_MODE", "").lower() in ("d2d", "p2p")
            or self._using_local_route
            or (self._backend == "zenoh" and no_explicit_urls and not portal_cfg)
        )
        self._d2d_collector = None  # lazy-initialized PresenceCollector

        # In D2D mode with Zenoh and no explicit URLs, use empty servers (multicast scouting).
        # When DEVICE_CONNECT_DISCOVERY_MODE=d2d is forced alongside a router URL (ZENOH_CONNECT),
        # keep the router URL so we can still communicate with devices connected to it.
        if (
            self._d2d_mode
            and self._backend == "zenoh"
            and no_explicit_urls
            and not self._using_local_route
        ):
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

    def _apply_local_route(
        self,
        local_cfg: Dict[str, Any],
        *,
        portal_fallback: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Configure this connection for a local Zenoh route with optional portal fallback."""
        self._backend = local_cfg.get("backend") or "zenoh"
        self._servers = local_cfg.get("servers") or []
        self._credentials = local_cfg.get("credentials")
        self._tls_config = local_cfg.get("tls")
        self._using_local_route = True
        self._d2d_mode = True
        if portal_fallback and portal_fallback.get("servers"):
            self._fallback_config = portal_fallback

    def _apply_portal_route(self, portal_cfg: Dict[str, Any]) -> None:
        """Configure this connection for the portal (remote) route only."""
        self._backend = portal_cfg.get("backend") or "nats"
        self._servers = portal_cfg.get("servers") or []
        self._credentials = portal_cfg.get("credentials")
        self._tls_config = portal_cfg.get("tls")
        self._using_local_route = False
        self._d2d_mode = False
        self._fallback_config = None

    async def _fetch_registry_local_route_candidates(
        self,
        portal_cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Query the registry over the portal route for devices advertising ``local_zenoh``."""
        client = create_client(backend=portal_cfg.get("backend") or "nats")
        try:
            await client.connect(
                servers=portal_cfg.get("servers") or [],
                credentials=portal_cfg.get("credentials"),
                tls_config=portal_cfg.get("tls"),
            )
            registry = _SDKRegistryClient(
                client,
                tenant=self.zone,
                timeout=self._request_timeout,
                cache_ttl=0,
            )
            devices = await registry.list_devices()
            candidates = collect_local_route_candidates_from_devices(devices)
            logger.info(
                "Discovered %d registry-advertised local Zenoh route(s)",
                len(candidates),
            )
            return candidates
        finally:
            try:
                await client.close()
            except Exception:
                logger.debug("cleanup error closing registry probe client", exc_info=True)

    async def _async_connect_registry_local_discovery(self) -> None:
        """Probe registry for ``local_zenoh``, try each route, else use portal NATS."""
        portal = self._stored_portal_cfg or self._fallback_config or {}
        candidates: List[Dict[str, Any]] = []
        try:
            candidates = await self._fetch_registry_local_route_candidates(portal)
        except Exception as e:
            logger.warning("Registry local-route discovery failed: %s", e)

        for local_cfg in candidates:
            self._apply_local_route(local_cfg, portal_fallback=portal)
            try:
                await self._async_connect_current()
                self._registry_local_discovery = False
                return
            except Exception as e:
                logger.info(
                    "Registry-advertised local route %s failed: %s",
                    local_cfg.get("servers"),
                    e,
                )
                if self._client:
                    try:
                        await self._client.close()
                    except Exception:
                        logger.debug(
                            "cleanup error closing failed local client",
                            exc_info=True,
                        )
                    self._client = None

        logger.info("No usable registry local route; connecting via portal")
        self._apply_portal_route(portal)
        self._registry_local_discovery = False
        await self._async_connect_current()

    async def _async_connect(self) -> None:
        if self._registry_local_discovery:
            await self._async_connect_registry_local_discovery()
            return
        try:
            await self._async_connect_current()
        except Exception:
            if not self._fallback_config:
                raise
            logger.info("Local Device Connect route failed; falling back to portal route")
            if self._client:
                try:
                    await self._client.close()
                except Exception:
                    logger.debug("cleanup error closing failed local client", exc_info=True)

            fallback = self._fallback_config
            self._fallback_config = None
            self._using_local_route = False
            self._d2d_mode = False
            self._backend = fallback.get("backend") or "nats"
            self._servers = fallback.get("servers") or []
            self._credentials = fallback.get("credentials")
            self._tls_config = fallback.get("tls")
            await self._async_connect_current()

    async def _async_connect_current(self) -> None:
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
    zone: str = "default",
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
      - TENANT            — Device Connect zone/namespace (default: "default")

    Args:
        nats_url: Broker URL (works for any backend despite the name).
        zone: Device Connect tenant/zone namespace.
        credentials: Auth credentials dict.
        tls_config: TLS configuration dict.
        request_timeout: Default timeout for device RPC calls.
    """
    global _connection
    with _lock:
        if _connection is not None:
            return
        zone = zone or os.environ.get("TENANT", "default")
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
