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
    ConnectionError,
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

        loop = asyncio.get_event_loop()
        if self._subscriber is not None:
            try:
                await loop.run_in_executor(None, self._subscriber.undeclare)
            except Exception:
                pass
        if self._queryable is not None:
            try:
                await loop.run_in_executor(None, self._queryable.undeclare)
            except Exception:
                pass

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
        self._pending_queries: Dict[str, Any] = {}  # reply_id -> Query object
        self._reconnect_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._disconnect_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._d2d_mode = False
        self._d2d_retry_count = 3
        self._d2d_retry_delay = 0.3  # seconds between retries

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
            tls_config: TLS configuration
                - ca_file: Path to CA certificate
                - cert_file: Path to client certificate (mTLS)
                - key_file: Path to client key (mTLS)
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

            if endpoints:
                config_dict["connect"] = {"endpoints": endpoints}

            if listen_endpoints:
                config_dict["listen"] = {"endpoints": listen_endpoints}

            # Scouting config — enable multicast + gossip in peer mode
            config_dict["scouting"] = {
                "multicast": {
                    "enabled": peer_mode,
                    "autoconnect": {"router": ["peer", "router"], "peer": ["router", "peer"]},
                },
                "gossip": {
                    "enabled": True,
                    "multihop": os.getenv("ZENOH_GOSSIP_MULTIHOP", "").lower() in ("true", "1", "yes"),
                    "autoconnect": {"router": ["peer", "router"], "peer": ["router", "peer"]},
                },
            }

            # TLS configuration
            if tls_config:
                tls_dict: Dict[str, Any] = {}
                if tls_config.get("ca_file"):
                    tls_dict["root_ca_certificate"] = tls_config["ca_file"]
                if tls_config.get("cert_file"):
                    tls_dict["client_certificate"] = tls_config["cert_file"]
                if tls_config.get("key_file"):
                    tls_dict["client_private_key"] = tls_config["key_file"]
                if tls_dict:
                    config_dict.setdefault("transport", {}).setdefault("link", {})["tls"] = tls_dict
                    self._logger.info("TLS enabled for secure connection")

            # Build config
            config = zenoh.Config.from_json5(json.dumps(config_dict))

            # Open session in executor (blocking call)
            loop = asyncio.get_event_loop()
            self._session = await loop.run_in_executor(
                self._executor, zenoh.open, config
            )
            self._connected = True
            self._closed = False
            self._d2d_mode = peer_mode

            mode = "peer" if peer_mode else "router"
            self._logger.debug(
                f"Connected to Zenoh ({mode} mode): {servers}"
            )

        except Exception as e:
            self._logger.error(f"Failed to connect to Zenoh: {e}")
            raise ConnectionError(f"Failed to connect to Zenoh: {e}") from e

    def configure_d2d_retry(self, retries: int = 3, delay: float = 0.3) -> None:
        """Configure retry behavior for D2D mode request/reply.

        Args:
            retries: Number of attempts (default 3).
            delay: Seconds between retries (default 0.3).
        """
        self._d2d_retry_count = retries
        self._d2d_retry_delay = delay

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
            query = self._pending_queries.pop(query_id, None)
            if query is not None:
                try:
                    # Reply and drop must run in the Zenoh executor so
                    # that the finalisation signal reaches the router
                    # promptly (zenoh#1409).
                    loop = asyncio.get_event_loop()
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
                loop = asyncio.get_event_loop()
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
        if not self.is_connected:
            raise NotConnectedError("Not connected to Zenoh")

        if queue:
            self._logger.warning(
                f"Zenoh does not natively support queue groups (requested: {queue}). "
                "All subscribers will receive all messages."
            )

        key = self.convert_subject_syntax(subject)

        try:
            loop = asyncio.get_event_loop()
            sample_queue: asyncio.Queue = asyncio.Queue()

            # Zenoh subscriber callback (runs in Zenoh's thread — must not block)
            def on_sample(sample):
                try:
                    loop.call_soon_threadsafe(sample_queue.put_nowait, ("sample", sample))
                except RuntimeError:
                    pass  # Event loop closed during shutdown

            # Zenoh queryable callback (runs in Zenoh's thread)
            def on_query(query):
                try:
                    loop.call_soon_threadsafe(sample_queue.put_nowait, ("query", query))
                except RuntimeError:
                    pass  # Event loop closed during shutdown

            # Background asyncio task to drain queue into user callback
            async def drain_loop():
                try:
                    while True:
                        msg_type, obj = await sample_queue.get()
                        try:
                            if msg_type == "sample":
                                await callback(bytes(obj.payload), None)
                            elif msg_type == "query":
                                query_id = uuid.uuid4().hex
                                self._pending_queries[query_id] = obj
                                reply_subject = f"{_QUERY_REPLY_PREFIX}{query_id}"
                                payload = bytes(obj.payload) if obj.payload else b""
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

            wrapper = ZenohSubscriptionWrapper(
                subscriber=subscriber,
                queryable=queryable,
                drain_task=drain_task,
                adapter=self,
                key_expr=key,
            )

            self._subscriptions[key] = {
                "subscriber": subscriber,
                "queryable": queryable,
                "drain_task": drain_task,
                "wrapper": wrapper,
            }

            self._logger.debug(f"Subscribed to {subject} (key: {key}, subscribe_only={subscribe_only})")
            return wrapper

        except Exception as e:
            self._logger.error(f"Failed to subscribe to {subject}: {e}")
            raise SubscribeError(f"Failed to subscribe: {e}") from e

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
        if not self.is_connected:
            raise NotConnectedError("Not connected to Zenoh")

        if queue:
            self._logger.warning(
                f"Zenoh does not natively support queue groups (requested: {queue}). "
                "All subscribers will receive all messages."
            )

        key = self.convert_subject_syntax(subject)

        try:
            loop = asyncio.get_event_loop()
            sample_queue: asyncio.Queue = asyncio.Queue()

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
                                matched_key = str(obj.key_expr)
                                await callback(bytes(obj.payload), matched_key, None)
                            elif msg_type == "query":
                                query_id = uuid.uuid4().hex
                                self._pending_queries[query_id] = obj
                                reply_subject = f"{_QUERY_REPLY_PREFIX}{query_id}"
                                matched_key = str(obj.key_expr)
                                payload = bytes(obj.payload) if obj.payload else b""
                                await callback(payload, matched_key, reply_subject)
                        except Exception as e:
                            self._logger.error(f"Subscriber callback error on {subject}: {e}")
                except asyncio.CancelledError:
                    pass

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

            wrapper = ZenohSubscriptionWrapper(
                subscriber=subscriber,
                queryable=queryable,
                drain_task=drain_task,
                adapter=self,
                key_expr=key,
            )

            sub_key = f"{key}:with_subject"
            self._subscriptions[sub_key] = {
                "subscriber": subscriber,
                "queryable": queryable,
                "drain_task": drain_task,
                "wrapper": wrapper,
            }

            self._logger.debug(f"Subscribed to {subject} with subject callback (key: {key}, subscribe_only={subscribe_only})")
            return wrapper

        except Exception as e:
            self._logger.error(f"Failed to subscribe to {subject}: {e}")
            raise SubscribeError(f"Failed to subscribe: {e}") from e

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
                loop = asyncio.get_event_loop()

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
        loop = asyncio.get_event_loop()
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
                    pass
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
                    pass

            drain_task = info.get("drain_task")
            if drain_task and not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass

        self._subscriptions.clear()
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
