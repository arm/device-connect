# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Zenoh implementation of the messaging abstraction layer.

Provides device-to-device and routed messaging with zero-config local discovery,
in-transit TLS encryption, and high-frequency streaming support.

Key characteristics:
- Synchronous Zenoh SDK bridged to asyncio via run_in_executor
- Slash-based key expressions (dots converted from NATS style)
- Device-to-device mode with multicast scouting (no router needed)
- Router mode for infrastructure deployments
- Native queryable-based request/reply (hybrid approach)
- TLS via Zenoh transport config
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Awaitable, Optional, List, Any, Dict
from urllib.parse import urlparse

try:
    import zenoh

    _ZENOH_AVAILABLE = True
except ImportError:
    zenoh = None  # type: ignore[assignment]
    _ZENOH_AVAILABLE = False

from device_connect_edge.messaging.base import MessagingClient, Subscription
from device_connect_edge.messaging.exceptions import (
    MessagingConnectionError,
    PublishError,
    SubscribeError,
    RequestTimeoutError,
    NotConnectedError,
)
from device_connect_edge.telemetry.tracer import get_tracer, SpanKind, StatusCode
from device_connect_edge.telemetry.metrics import get_metrics

# Internal prefix for queryable reply routing
_QUERY_REPLY_PREFIX = "_zenoh_query/"

# Seconds for routing-table updates to propagate through D2D mesh
# after undeclaring queryables during graceful shutdown.
_D2D_QUERYABLE_PROPAGATION_DELAY = 0.5

logger = logging.getLogger(__name__)


class ZenohSubscriptionWrapper(Subscription):
    """Wraps a Zenoh subscriber + optional queryable for unsubscribe."""

    def __init__(
        self,
        subscriber: Any,
        queryable: Any = None,
        drain_task: Optional[asyncio.Task] = None,
        adapter: Optional["ZenohAdapter"] = None,
        key_expr: Optional[str] = None,
    ):
        self._subscriber = subscriber
        self._queryable = queryable
        self._drain_task = drain_task
        self._adapter = adapter
        self._key_expr = key_expr

    async def unsubscribe(self) -> None:
        """Unsubscribe from the key expression."""
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

        loop = asyncio.get_running_loop()
        if self._subscriber is not None:
            try:
                await loop.run_in_executor(None, self._subscriber.undeclare)
            except Exception:
                logger.debug("cleanup error undeclaring subscriber", exc_info=True)
        if self._queryable is not None:
            try:
                await loop.run_in_executor(None, self._queryable.undeclare)
            except Exception:
                logger.debug("cleanup error undeclaring queryable", exc_info=True)

        if self._adapter and self._key_expr:
            self._adapter._subscriptions.pop(self._key_expr, None)


class ZenohAdapter(MessagingClient):
    """
    Zenoh implementation of the MessagingClient interface.

    Provides full access to Zenoh features including:
    - Device-to-device mode with multicast scouting (no router needed)
    - Router mode for infrastructure deployments
    - TLS encryption (including mTLS)
    - Wildcard subscriptions (* and **)
    - Native queryable-based request/reply
    - High-frequency pub/sub for streaming (50Hz+)
    - Connection state callbacks
    """

    def __init__(self):
        if not _ZENOH_AVAILABLE:
            raise ImportError(
                "eclipse-zenoh library required for Zenoh backend. "
                "Install with: pip install eclipse-zenoh"
            )
        self._session: Any = None
        self._logger = logging.getLogger(__name__)
        self._connected = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="zenoh")
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        self._pending_queries: Dict[str, tuple] = {}  # reply_id -> (Query, timestamp)
        # All current accesses are in the asyncio loop, but the lock is
        # defensive in case the Zenoh callback model changes.
        self._pending_queries_lock = threading.Lock()
        self._query_ttl = 30.0  # seconds before stale queries are evicted
        self._reconnect_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._disconnect_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._d2d_mode = False
        self._d2d_retry_count = 3
        self._d2d_retry_delay = 0.3  # seconds between retries
        # Saved config + watchdog state so the session can be transparently
        # re-opened and all subscriptions/queryables re-declared if the router
        # restarts (e.g. on a tenant-ACL change). See _connection_watchdog.
        self._config_dict: Optional[Dict[str, Any]] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_interval = 3.0  # seconds between session-health checks
        self._reconnecting = False

    # ── Connection ──────────────────────────────────────────────

    async def connect(
        self,
        servers: List[str],
        credentials: Optional[Dict[str, Any]] = None,
        tls_config: Optional[Dict[str, Any]] = None,
        reconnect_cb: Optional[Callable[[], Awaitable[None]]] = None,
        disconnect_cb: Optional[Callable[[], Awaitable[None]]] = None,
        **kwargs: Any,
    ) -> None:
        """
        Establish connection to Zenoh router or start in peer mode.

        Args:
            servers: List of Zenoh endpoints. Formats:
                - ["zenoh://"]  — peer mode with multicast scouting (no router)
                - ["zenoh://host:7447"]  — connect to router
                - ["tcp/host:7447"]  — native Zenoh endpoint format
                - ["zenoh+tls://host:7447"]  — TLS connection
                - ["tls/host:7447"]  — native Zenoh TLS format
            credentials: Not used for Zenoh (auth is via TLS certs)
            tls_config: TLS configuration. Each of CA / cert / key may be given
                either as a file path or as inline PEM text:
                - ca_file / ca_pem: CA certificate
                - cert_file / cert_pem: client certificate (mTLS)
                - key_file / key_pem: client key (mTLS)
                Inline PEM is passed to Zenoh via its base64 config fields.
            reconnect_cb: Callback when reconnected
            disconnect_cb: Callback when disconnected
            **kwargs: Additional options:
                - peer_mode: bool — force peer mode with scouting (default: False)
                - listen: list[str] — endpoints to listen on
        """
        self._reconnect_cb = reconnect_cb
        self._disconnect_cb = disconnect_cb

        try:
            config_dict: Dict[str, Any] = {}

            # Parse endpoints
            endpoints = []
            peer_mode = kwargs.get("peer_mode", False)
            listen_endpoints = kwargs.get("listen", [])

            # Check ZENOH_LISTEN env var
            import os

            env_listen = os.getenv("ZENOH_LISTEN")
            if env_listen:
                listen_endpoints = [ep.strip() for ep in env_listen.split(",")]

            for server in servers:
                endpoint = self._parse_server_url(server)
                if endpoint:
                    endpoints.append(endpoint)

            # If no meaningful endpoints, use peer mode
            if not endpoints:
                peer_mode = True

            # Connect as a Zenoh *client* when targeting a router, and only as
            # a *peer* for brokerless D2D. This is security-critical for
            # router deployments: a peer participates in gossip/scouting and
            # can form DIRECT peer-to-peer links with other nodes, which
            # bypass the router entirely -- and the router is where the mTLS
            # access-control (tenant isolation) is enforced. Leaving mode
            # unset defaults to "peer", so two devices (or a device and the
            # registry) would peer up directly and never have their traffic
            # ACL-checked. A client only ever talks to its configured
            # router(s), so all traffic is brokered and policy-enforced.
            config_dict["mode"] = "peer" if peer_mode else "client"

            if endpoints:
                # Keep retrying the configured router(s) forever instead of
                # giving up on first failure. This lets a client survive a
                # router restart (e.g. when a tenant ACL rule is added): the
                # Zenoh runtime reconnects and re-declares the session's
                # primitives natively. The watchdog below is a backstop for
                # the case where the session is fully closed rather than
                # transiently disconnected.
                config_dict["connect"] = {
                    "endpoints": endpoints,
                    "exit_on_failure": False,
                    "retry": {
                        "period_init_ms": 1000,
                        "period_max_ms": 5000,
                        "period_increase_factor": 2.0,
                    },
                }

            if listen_endpoints:
                config_dict["listen"] = {"endpoints": listen_endpoints}

            # Scouting/gossip only make sense for peer (D2D) mode; a client
            # discovers nothing and must not autoconnect to peers (that would
            # re-introduce the ACL-bypassing direct links described above).
            config_dict["scouting"] = {
                "multicast": {
                    "enabled": peer_mode,
                    "autoconnect": {"router": ["peer", "router"], "peer": ["router", "peer"]},
                },
                "gossip": {
                    "enabled": peer_mode,
                    "multihop": os.getenv("ZENOH_GOSSIP_MULTIHOP", "").lower() in ("true", "1", "yes"),
                    "autoconnect": {"router": ["peer", "router"], "peer": ["router", "peer"]},
                },
            }

            # TLS configuration
            if tls_config:
                # Zenoh 1.x renamed the connect-side TLS fields:
                # client_certificate/client_private_key -> connect_certificate/
                # connect_private_key, and mutual TLS is gated on enable_mtls.
                #
                # Each of CA / cert / key may be provided either as a file path
                # (*_file) or as inline PEM text (*_pem). Inline PEM is fed to
                # Zenoh via its native base64 config fields
                # (root_ca_certificate_base64 / connect_certificate_base64 /
                # connect_private_key_base64), so a self-contained creds.json
                # connects without writing any cert material to local disk.
                import base64

                def _b64(pem: str) -> str:
                    return base64.b64encode(
                        pem.encode() if isinstance(pem, str) else pem
                    ).decode()

                tls_dict: Dict[str, Any] = {}
                # CA
                if tls_config.get("ca_file"):
                    tls_dict["root_ca_certificate"] = tls_config["ca_file"]
                elif tls_config.get("ca_pem"):
                    tls_dict["root_ca_certificate_base64"] = _b64(tls_config["ca_pem"])
                # Client certificate (mTLS)
                if tls_config.get("cert_file"):
                    tls_dict["connect_certificate"] = tls_config["cert_file"]
                elif tls_config.get("cert_pem"):
                    tls_dict["connect_certificate_base64"] = _b64(tls_config["cert_pem"])
                # Client private key (mTLS)
                if tls_config.get("key_file"):
                    tls_dict["connect_private_key"] = tls_config["key_file"]
                elif tls_config.get("key_pem"):
                    tls_dict["connect_private_key_base64"] = _b64(tls_config["key_pem"])

                has_cert = "connect_certificate" in tls_dict or "connect_certificate_base64" in tls_dict
                has_key = "connect_private_key" in tls_dict or "connect_private_key_base64" in tls_dict
                if has_cert and has_key:
                    tls_dict["enable_mtls"] = True
                if tls_dict:
                    config_dict.setdefault("transport", {}).setdefault("link", {})["tls"] = tls_dict
                    self._logger.info("TLS enabled for secure connection")

            # Save the built config so the watchdog can transparently
            # re-open the session with identical settings after a hard close.
            self._config_dict = config_dict
            self._d2d_mode = peer_mode

            # Open the session
            await self._open_session()

            mode = "peer" if peer_mode else "router"
            self._logger.debug(
                f"Connected to Zenoh ({mode} mode): {servers}"
            )

            # Start the session-health watchdog (idempotent).
            self._start_watchdog()

        except Exception as e:
            self._logger.error(f"Failed to connect to Zenoh: {e}")
            raise MessagingConnectionError(f"Failed to connect to Zenoh: {e}") from e

    async def _open_session(self) -> None:
        """Open (or re-open) the Zenoh session from the saved config."""
        if self._config_dict is None:
            raise MessagingConnectionError("connect() must be called before opening a session")
        config = zenoh.Config.from_json5(json.dumps(self._config_dict))
        loop = asyncio.get_running_loop()
        self._session = await loop.run_in_executor(
            self._executor, zenoh.open, config
        )
        self._connected = True
        self._closed = False

    def _start_watchdog(self) -> None:
        """Start the background session-health watchdog if not already running."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._connection_watchdog())

    async def _connection_watchdog(self) -> None:
        """Re-open the session and re-declare all subscriptions on a hard close.

        Zenoh's client runtime already reconnects to the router and re-declares
        primitives on a *transient* disconnect (see the connect.retry config).
        This watchdog covers the case where the session object itself reports
        closed: it re-opens a fresh session and replays every subscriber and
        queryable. Re-declaration only happens once the old session is
        confirmed closed, so there is no risk of duplicate live declarations.
        """
        while not self._closed:
            try:
                await asyncio.sleep(self._watchdog_interval)
            except asyncio.CancelledError:
                return
            if self._closed or self._reconnecting:
                continue
            sess = self._session
            if sess is None:
                continue
            try:
                closed = sess.is_closed()
            except Exception:
                closed = True
            if not closed:
                continue
            # Session is gone but we never asked to close -- reconnect.
            self._reconnecting = True
            self._connected = False
            self._logger.warning(
                "Zenoh session closed unexpectedly; reconnecting and re-declaring %d subscription(s)",
                len(self._subscriptions),
            )
            try:
                await self._open_session()
                await self._redeclare_all()
                self._logger.info("Zenoh session re-established and subscriptions re-declared")
                if self._reconnect_cb is not None:
                    try:
                        await self._reconnect_cb()
                    except Exception:
                        self._logger.debug("reconnect_cb raised", exc_info=True)
            except Exception as e:
                self._logger.error("Zenoh reconnect failed (will retry): %s", e)
            finally:
                self._reconnecting = False

    def configure_d2d_retry(self, retries: int = 3, delay: float = 0.3) -> None:
        """Configure retry behavior for D2D mode request/reply.

        Args:
            retries: Number of attempts (default 3).
            delay: Seconds between retries (default 0.3).
        """
        self._d2d_retry_count = retries
        self._d2d_retry_delay = delay

    def _evict_stale_queries(self) -> None:
        """Remove pending queries older than _query_ttl. Must be called under _pending_queries_lock."""
        cutoff = time.monotonic() - self._query_ttl
        stale = [qid for qid, (_, ts) in self._pending_queries.items() if ts < cutoff]
        for qid in stale:
            self._pending_queries.pop(qid, None)
        if stale:
            logger.debug("Evicted %d stale pending queries", len(stale))

    def _parse_server_url(self, url: str) -> Optional[str]:
        """Convert various URL formats to Zenoh endpoint format.

        Returns None for empty/peer-mode-only URLs.
        """
        url = url.strip()

        # zenoh:// with no host means peer mode — no endpoint needed
        if url in ("zenoh://", "zenoh:///"):
            return None

        # zenoh+tls://host:port → tls/host:port
        if url.startswith("zenoh+tls://"):
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 7447
            return f"tls/{host}:{port}"

        # zenoh://host:port → tcp/host:port
        if url.startswith("zenoh://"):
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 7447
            return f"tcp/{host}:{port}"

        # Already in Zenoh native format: tcp/host:port, tls/host:port, udp/host:port
        if url.startswith(("tcp/", "tls/", "udp/")):
            return url

        # Best effort: assume tcp
        return f"tcp/{url}" if "/" not in url else url

    # ── Publishing ──────────────────────────────────────────────

    async def publish(self, subject: str, data: bytes) -> None:
        """
        Publish message to Zenoh key expression.

        Subjects are auto-converted from NATS dotted notation to Zenoh slashes.
        Also intercepts replies to pending queryable queries (internal mechanism).

        Args:
            subject: NATS-style subject (dots converted to slashes)
            data: Message payload as bytes
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to Zenoh")

        key = self.convert_subject_syntax(subject)

        # Intercept query replies — these are responses to queryable hits
        if key.startswith(_QUERY_REPLY_PREFIX):
            query_id = key[len(_QUERY_REPLY_PREFIX):]
            with self._pending_queries_lock:
                entry = self._pending_queries.pop(query_id, None)
            query = entry[0] if entry is not None else None
            if query is not None:
                try:
                    # Reply and drop must run in the Zenoh executor so
                    # that the finalisation signal reaches the router
                    # promptly (zenoh#1409).
                    loop = asyncio.get_running_loop()
                    def _reply_and_drop():
                        query.reply(query.key_expr, data)
                        query.drop()
                    await loop.run_in_executor(self._executor, _reply_and_drop)
                except Exception as e:
                    self._logger.error(f"Failed to reply to query: {e}")
                    raise PublishError(f"Failed to reply to query: {e}") from e
                return
            # If query_id not found, fall through to normal publish
            self._logger.debug(
                f"Query {query_id} not found in pending queries, falling through to put"
            )

        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "zenoh.publish",
            kind=SpanKind.PRODUCER,
            attributes={"messaging.destination": subject},
        ) as span:
            try:
                t0 = time.monotonic()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    self._executor,
                    lambda: self._session.put(key, data),
                )
                metrics.msg_publish_duration.record(
                    (time.monotonic() - t0) * 1000,
                    {"messaging.destination": subject},
                )
                span.set_status(StatusCode.OK)
            except Exception as e:
                self._logger.error(f"Failed to publish to {subject}: {e}")
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                raise PublishError(f"Failed to publish: {e}") from e

    # ── Subscribing ─────────────────────────────────────────────

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[bytes, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe to Zenoh key expression with callback.

        By default, declares both a subscriber (for pub/sub) and a queryable
        (for request/reply) on the same key expression.  The queryable is a
        workaround for known Zenoh D2D queryable flakiness — having a
        queryable co-located with the subscriber improves reliability of the
        hybrid RPC pattern where callers use session.get().

        When *subscribe_only* is True the queryable is skipped, avoiding
        unnecessary resource overhead for pure pub/sub consumers
        (e.g. presence and event subscriptions) that never reply.

        Args:
            subject: NATS-style subject pattern (supports * and >)
            callback: Async callback(data: bytes, reply_subject: Optional[str])
            queue: Queue group name (logged as warning — Zenoh has no native equivalent)
            subscribe_only: If True, only declare a subscriber (no queryable).

        Returns:
            Subscription handle
        """
        return await self._install_subscription(
            subject, callback, queue=queue,
            subscribe_only=subscribe_only, with_subject=False,
        )

    async def subscribe_with_subject(
        self,
        subject: str,
        callback: Callable[[bytes, str, Optional[str]], Awaitable[None]],
        queue: Optional[str] = None,
        subscribe_only: bool = False,
    ) -> Subscription:
        """
        Subscribe with callback that receives the matched key expression.

        Args:
            subject: NATS-style subject pattern
            callback: Async callback(data: bytes, subject: str, reply: Optional[str])
            queue: Queue group name (warned — no native Zenoh support)
            subscribe_only: If True, only declare a subscriber (no queryable).

        Returns:
            Subscription handle
        """
        return await self._install_subscription(
            subject, callback, queue=queue,
            subscribe_only=subscribe_only, with_subject=True,
        )

    async def _install_subscription(
        self,
        subject: str,
        callback: Callable[..., Awaitable[None]],
        *,
        queue: Optional[str],
        subscribe_only: bool,
        with_subject: bool,
        existing_wrapper: Optional["ZenohSubscriptionWrapper"] = None,
    ) -> Subscription:
        """Declare a subscriber (+ optional queryable) and wire its drain loop.

        Shared by subscribe()/subscribe_with_subject() and reused by
        _redeclare_all() after a reconnect. The full declaration spec is stored
        in self._subscriptions so it can be replayed onto a fresh session.

        When *with_subject* is True the user callback is invoked as
        ``callback(data, matched_key, reply)``; otherwise ``callback(data, reply)``.
        When *existing_wrapper* is provided (reconnect path) its handles are
        updated in place so callers' Subscription references stay valid.
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to Zenoh")

        if queue:
            self._logger.warning(
                f"Zenoh does not natively support queue groups (requested: {queue}). "
                "All subscribers will receive all messages."
            )

        key = self.convert_subject_syntax(subject)
        store_key = f"{key}:with_subject" if with_subject else key

        try:
            loop = asyncio.get_running_loop()
            sample_queue: asyncio.Queue = asyncio.Queue()

            # Zenoh callbacks run in Zenoh's own thread — must not block.
            def on_sample(sample):
                try:
                    loop.call_soon_threadsafe(sample_queue.put_nowait, ("sample", sample))
                except RuntimeError:
                    pass  # Event loop closed during shutdown

            def on_query(query):
                try:
                    loop.call_soon_threadsafe(sample_queue.put_nowait, ("query", query))
                except RuntimeError:
                    pass  # Event loop closed during shutdown

            async def drain_loop():
                try:
                    while True:
                        msg_type, obj = await sample_queue.get()
                        try:
                            if msg_type == "sample":
                                if with_subject:
                                    await callback(bytes(obj.payload), str(obj.key_expr), None)
                                else:
                                    await callback(bytes(obj.payload), None)
                            elif msg_type == "query":
                                query_id = uuid.uuid4().hex
                                with self._pending_queries_lock:
                                    self._pending_queries[query_id] = (obj, time.monotonic())
                                    self._evict_stale_queries()
                                reply_subject = f"{_QUERY_REPLY_PREFIX}{query_id}"
                                payload = bytes(obj.payload) if obj.payload else b""
                                if with_subject:
                                    await callback(payload, str(obj.key_expr), reply_subject)
                                else:
                                    await callback(payload, reply_subject)
                        except Exception as e:
                            self._logger.error(f"Subscriber callback error on {subject}: {e}")
                except asyncio.CancelledError:
                    pass

            # Declare subscriber (always) and queryable (unless subscribe_only)
            subscriber = await loop.run_in_executor(
                self._executor,
                lambda: self._session.declare_subscriber(key, on_sample),
            )
            queryable = None
            if not subscribe_only:
                queryable = await loop.run_in_executor(
                    self._executor,
                    lambda: self._session.declare_queryable(key, on_query, complete=True),
                )

            drain_task = asyncio.create_task(drain_loop())

            if existing_wrapper is not None:
                existing_wrapper._subscriber = subscriber
                existing_wrapper._queryable = queryable
                existing_wrapper._drain_task = drain_task
                existing_wrapper._key_expr = key
                wrapper = existing_wrapper
            else:
                wrapper = ZenohSubscriptionWrapper(
                    subscriber=subscriber,
                    queryable=queryable,
                    drain_task=drain_task,
                    adapter=self,
                    key_expr=key,
                )

            self._subscriptions[store_key] = {
                "subscriber": subscriber,
                "queryable": queryable,
                "drain_task": drain_task,
                "wrapper": wrapper,
                # Redeclare spec — everything needed to replay onto a new session.
                "callback": callback,
                "subscribe_only": subscribe_only,
                "with_subject": with_subject,
                "queue": queue,
                "key": key,
            }

            self._logger.debug(
                f"Subscribed to {subject} (key: {key}, subscribe_only={subscribe_only}, "
                f"with_subject={with_subject})"
            )
            return wrapper

        except Exception as e:
            self._logger.error(f"Failed to subscribe to {subject}: {e}")
            raise SubscribeError(f"Failed to subscribe: {e}") from e

    async def _redeclare_all(self) -> None:
        """Replay every stored subscription onto the (freshly re-opened) session.

        Called by the watchdog after a reconnect. The old session's subscriber,
        queryable and drain task died with it, so we cancel the stale drain
        tasks and re-install each subscription from its saved spec, reusing the
        original wrapper so caller-held Subscription handles remain valid.
        """
        specs = list(self._subscriptions.values())
        self._subscriptions.clear()
        for spec in specs:
            old_drain = spec.get("drain_task")
            if old_drain and not old_drain.done():
                old_drain.cancel()
            try:
                await self._install_subscription(
                    spec["key"],
                    spec["callback"],
                    queue=spec.get("queue"),
                    subscribe_only=spec.get("subscribe_only", False),
                    with_subject=spec.get("with_subject", False),
                    existing_wrapper=spec.get("wrapper"),
                )
            except Exception as e:
                self._logger.error(
                    "Failed to re-declare subscription on key %s: %s", spec.get("key"), e
                )

    # ── Request/Reply ───────────────────────────────────────────

    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 5.0,
    ) -> bytes:
        """
        Send request and wait for reply via Zenoh queryable.

        Uses session.get() which triggers queryables declared by subscribe().

        Args:
            subject: NATS-style subject
            data: Request payload as bytes
            timeout: Maximum time to wait for reply in seconds

        Returns:
            Reply payload as bytes
        """
        if not self.is_connected:
            raise NotConnectedError("Not connected to Zenoh")

        key = self.convert_subject_syntax(subject)

        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.start_as_current_span(
            "zenoh.request",
            kind=SpanKind.CLIENT,
            attributes={
                "messaging.destination": subject,
                "messaging.timeout_ms": timeout * 1000,
            },
        ) as span:
            try:
                t0 = time.monotonic()
                loop = asyncio.get_running_loop()

                def _do_get():
                    # CancellationToken aborts the receiver as soon as
                    # the first valid reply arrives so callers return
                    # immediately instead of waiting for the full
                    # timeout (zenoh#1409).
                    cancel_token = zenoh.CancellationToken()
                    get_kwargs: Dict[str, Any] = dict(
                        payload=data,
                        timeout=timeout,
                        cancellation_token=cancel_token,
                    )
                    # In D2D mode, broadcast queries to all matching
                    # queryables rather than relying on potentially-stale
                    # routing table lookups (BestMatching default).
                    if self._d2d_mode:
                        get_kwargs["target"] = zenoh.QueryTarget.ALL
                        get_kwargs["consolidation"] = zenoh.ConsolidationMode.NONE
                    receiver = self._session.get(key, **get_kwargs)
                    last_err = None
                    for reply in receiver:
                        if reply.ok is not None:
                            cancel_token.cancel()
                            return ("ok", bytes(reply.ok.payload))
                        elif reply.err is not None:
                            # Zenoh may send a Timeout error before the
                            # real reply arrives — keep iterating.
                            last_err = bytes(reply.err.payload)
                    if last_err is not None:
                        return ("err", last_err)
                    return ("timeout", None)

                # In D2D mode, retry on "no responders" since multicast
                # routing tables can be transiently stale.
                max_attempts = self._d2d_retry_count if self._d2d_mode else 1

                for attempt in range(max_attempts):
                    result_type, result_data = await loop.run_in_executor(
                        self._executor, _do_get,
                    )

                    if result_type == "ok":
                        if attempt > 0:
                            self._logger.info(
                                "Request to %s succeeded on retry %d",
                                subject, attempt,
                            )
                        metrics.msg_request_duration.record(
                            (time.monotonic() - t0) * 1000,
                            {"messaging.destination": subject},
                        )
                        span.set_status(StatusCode.OK)
                        return result_data
                    elif result_type == "err":
                        err_msg = f"Query error reply: {result_data}"
                        self._logger.error(err_msg)
                        span.record_exception(Exception(err_msg))
                        span.set_status(StatusCode.ERROR, err_msg)
                        raise PublishError(err_msg)

                    # No replies — retry if attempts remain
                    if attempt < max_attempts - 1:
                        self._logger.debug(
                            "Request to %s got no responders (attempt %d/%d), "
                            "retrying in %.1fs...",
                            subject, attempt + 1, max_attempts,
                            self._d2d_retry_delay,
                        )
                        await asyncio.sleep(self._d2d_retry_delay)

                # All attempts exhausted
                span.set_status(StatusCode.ERROR, "timeout")
                raise RequestTimeoutError(
                    f"Request to {subject} timed out after {timeout}s (no responders)"
                )

            except (RequestTimeoutError, PublishError):
                raise
            except Exception as e:
                error_str = str(e).lower()
                if "timeout" in error_str:
                    span.set_status(StatusCode.ERROR, "timeout")
                    raise RequestTimeoutError(
                        f"Request to {subject} timed out after {timeout}s"
                    ) from e
                # "no responders" equivalent for Zenoh
                if "no responders" in error_str or "no queryable" in error_str:
                    self._logger.debug(f"Request to {subject} failed: {e}")
                else:
                    self._logger.error(f"Request to {subject} failed: {e}")
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                raise PublishError(f"Request failed: {e}") from e

    # ── Cleanup ─────────────────────────────────────────────────

    async def close(self) -> None:
        """Close connection to Zenoh and cleanup all resources."""
        # Signal intentional shutdown first so the watchdog does not try to
        # reconnect the session we are about to tear down, then stop it.
        self._closed = True
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        self._watchdog_task = None

        loop = asyncio.get_running_loop()
        has_queryables = False

        # Phase 1: Undeclare queryables first so peers update routing
        # tables before the session closes.
        for key, info in list(self._subscriptions.items()):
            queryable = info.get("queryable")
            if queryable is not None:
                has_queryables = True
                try:
                    await loop.run_in_executor(self._executor, queryable.undeclare)
                except Exception:
                    logger.debug("cleanup error undeclaring queryable during close", exc_info=True)
                info["queryable"] = None

        # Brief delay for queryable undeclaration to propagate in D2D mesh
        if self._d2d_mode and has_queryables:
            await asyncio.sleep(_D2D_QUERYABLE_PROPAGATION_DELAY)

        # Phase 2: Undeclare subscribers first (stops Zenoh callbacks),
        # then cancel drain tasks.
        for key, info in list(self._subscriptions.items()):
            subscriber = info.get("subscriber")
            if subscriber is not None:
                try:
                    await loop.run_in_executor(self._executor, subscriber.undeclare)
                except Exception:
                    logger.debug("cleanup error undeclaring subscriber during close", exc_info=True)

            drain_task = info.get("drain_task")
            if drain_task and not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass

        self._subscriptions.clear()
        with self._pending_queries_lock:
            self._pending_queries.clear()

        # Phase 3: Close session
        if self._session is not None and not self._session.is_closed():
            try:
                await loop.run_in_executor(self._executor, self._session.close)
            except Exception as e:
                self._logger.debug(f"Error closing Zenoh session: {e}")

        self._connected = False
        self._closed = True
        self._executor.shutdown(wait=False)
        self._logger.info("Zenoh connection closed")

    async def disconnect(self) -> None:
        """Alias for close() — disconnect from Zenoh."""
        await self.close()

    async def flush(self) -> None:
        """No-op for Zenoh — publishes are immediate."""
        pass

    async def drain(self) -> None:
        """No-op for Zenoh — no buffering concept at SDK level."""
        pass

    # ── Properties ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to Zenoh."""
        if self._session is None:
            return False
        return self._connected and not self._session.is_closed()

    @property
    def is_closed(self) -> bool:
        """Check if connection has been closed."""
        if self._closed:
            return True
        if self._session is None:
            return False
        return self._session.is_closed()

    # ── Subject Conversion ──────────────────────────────────────

    def convert_subject_syntax(self, subject: str) -> str:
        """
        Convert subject from NATS-style to Zenoh key expression format.

        Conversions:
            - Dots to slashes: device-connect.tenant.device → device-connect/tenant/device
            - NATS > (multi-level) → Zenoh ** : device-connect.tenant.> → device-connect/tenant/**
            - NATS * (single-level) stays *: device-connect.*.event → device-connect/*/event

        If the subject already contains slashes (Zenoh-native), it is returned as-is
        to allow direct Zenoh key expressions when needed.

        Args:
            subject: NATS-style subject or Zenoh key expression

        Returns:
            Zenoh key expression
        """
        # If it already contains slashes, assume Zenoh-native format
        if "/" in subject:
            return subject

        # Dot-separated NATS format → slash-separated Zenoh format
        key = subject.replace(".", "/")

        # NATS multi-level wildcard '>' → Zenoh '**'
        # Handle trailing '>' (most common: "device-connect/tenant/>")
        if key.endswith("/>"):
            key = key[:-2] + "/**"
        # Handle '>' in the middle (rare but possible)
        key = key.replace("/>", "/**")

        # NATS single-level wildcard '*' stays as '*' in Zenoh (same semantics)

        return key
