# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""NATS helpers: RPC invocation and event streaming."""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import nats

from .. import config

logger = logging.getLogger(__name__)

# Registry credentials (privileged, can reach all tenants)
_REGISTRY_CREDS = Path(config.CREDS_DIR) / "registry.creds.json"

# Long-lived client reused across all invoke() calls. The portal used to
# open and close a fresh NATS connection per RPC, which added a TCP +
# JWT-auth handshake to every dashboard "Run" click. The connection is
# concurrent-safe (each nc.request creates its own inbox subscription)
# so a single cached client serves the whole portal.
_invoke_client: "nats.aio.client.Client | None" = None

# Exception types that mean "the cached NATS client is no longer usable"
# — i.e. the next request must reconnect. We deliberately do NOT include
# every nats.errors.Error subclass: BadSubjectError, MaxPayloadError,
# AuthorizationError etc. are caller / payload bugs that don't kill the
# connection, so dropping the client on them would churn the socket on
# every malformed request. Native OSError / ConnectionError covers
# socket-level failures the NATS client may not have wrapped yet.
#
# Review notes (do not re-litigate without reading these):
# - ``ConnectionReconnectingError`` is intentionally absent: it means the
#   client is *already* reconnecting itself. Dropping + close()-ing in
#   that state preempts the nats-py reconnect machinery, forces a fresh
#   handshake on every queued request, and amplifies broker flaps. Let
#   the existing client recover; the next ``nc.request`` either succeeds
#   post-reconnect or raises something more terminal that *is* in this
#   set. Past review round suggested adding it -- don't.
# - ``ProtocolError`` and ``NoRespondersError`` are payload-level signals
#   over a healthy socket; covered by their own branches / left to the
#   default handler without dropping the client. See ``test_nats_rpc``.
_TRANSPORT_FATAL_ERRORS: tuple = (
    nats.errors.ConnectionClosedError,
    nats.errors.ConnectionDrainingError,
    nats.errors.StaleConnectionError,
    nats.errors.NoServersError,
    nats.errors.OutboundBufferLimitError,
    nats.errors.SecureConnFailedError,
    ConnectionError,
    OSError,
)
# Lock is created lazily inside _get_invoke_lock() rather than at import
# time. asyncio.Lock() binds to whatever event loop is current when it's
# constructed; constructing it here would break tests (and any future
# code) that runs this module under a fresh loop.
_invoke_client_lock: "asyncio.Lock | None" = None


def _get_invoke_lock() -> asyncio.Lock:
    """Return the module-level invoke lock, creating it on first use."""
    global _invoke_client_lock
    if _invoke_client_lock is None:
        _invoke_client_lock = asyncio.Lock()
    return _invoke_client_lock


def _load_creds() -> dict:
    """Load registry credentials for NATS auth."""
    if _REGISTRY_CREDS.exists():
        with open(_REGISTRY_CREDS) as f:
            return json.load(f)
    return {}


async def _get_invoke_client():
    """Lazily open and cache a single NATS client for RPC invocations."""
    global _invoke_client
    async with _get_invoke_lock():
        if _invoke_client is None or _invoke_client.is_closed:
            _invoke_client = await connect()
            logger.info("invoke client connected; will be reused across requests")
        return _invoke_client


async def _drop_invoke_client() -> None:
    """Discard the cached client, best-effort closing whatever's there.

    Called after a hard transport failure so the next invoke() reconnects
    rather than reusing a half-dead client. The ``close()`` is wrapped in
    a broad try/except because the connection is already known to be in
    a bad state — we just want to release sockets if we can.
    """
    global _invoke_client
    async with _get_invoke_lock():
        stale = _invoke_client
        _invoke_client = None
    if stale is not None:
        try:
            await stale.close()
        except Exception:
            logger.debug("ignored error closing stale invoke client", exc_info=True)


async def close_invoke_client() -> None:
    """Close the cached invoke client at app shutdown.

    Wire this into ``aiohttp.web.Application.on_cleanup``: without it the
    long-lived socket leaks on graceful shutdown (the cached client is
    module-level state, not tied to the app's lifecycle). Idempotent —
    calling twice is a no-op because ``_drop_invoke_client`` nils the
    global first.
    """
    await _drop_invoke_client()


async def connect():
    """Return a connected NATS client using registry credentials."""
    creds = _load_creds()
    nats_cfg = creds.get("nats", {})

    servers = nats_cfg.get("urls", [f"nats://{config.NATS_HOST}:{config.NATS_PORT}"])
    connect_opts = {"servers": servers}

    jwt_token = nats_cfg.get("jwt")
    nkey_seed = nats_cfg.get("nkey_seed")
    if jwt_token and nkey_seed:
        import base64
        import nkeys

        sk = nkeys.from_seed(nkey_seed.encode())

        def _sign(nonce):
            nonce_bytes = nonce.encode() if isinstance(nonce, str) else nonce
            return base64.b64encode(sk.sign(nonce_bytes))

        connect_opts["user_jwt_cb"] = lambda: jwt_token.encode()
        connect_opts["signature_cb"] = _sign

    return await nats.connect(**connect_opts)


async def invoke(tenant: str, device_id: str, function: str, params: dict, timeout: float = 5.0) -> dict:
    """Send a JSON-RPC request to a device and return the response."""
    t0 = time.monotonic()
    subject = f"device-connect.{tenant}.{device_id}.cmd"
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": function,
        "params": params,
    }
    try:
        nc = await _get_invoke_client()
        msg = await nc.request(subject, json.dumps(payload).encode(), timeout=timeout)
        logger.info(
            "invoke %s/%s.%s ok in %.1fms",
            tenant, device_id, function, (time.monotonic() - t0) * 1000,
        )
        return json.loads(msg.data)
    except nats.errors.NoRespondersError:
        logger.warning(
            "invoke %s/%s.%s no-responders in %.1fms",
            tenant, device_id, function, (time.monotonic() - t0) * 1000,
        )
        return {"error": {"code": -1, "message": f"Device {device_id} is not responding"}}
    except nats.errors.TimeoutError:
        logger.warning(
            "invoke %s/%s.%s timeout in %.1fms",
            tenant, device_id, function, (time.monotonic() - t0) * 1000,
        )
        return {"error": {"code": -2, "message": f"Request timed out after {timeout}s"}}
    except Exception as e:
        # Only drop the cached client on transport-level failures so a
        # payload / programmer bug (BadSubject, MaxPayload, KeyError in
        # our own code, ...) doesn't churn the connection on every call.
        if isinstance(e, _TRANSPORT_FATAL_ERRORS):
            await _drop_invoke_client()
        logger.exception(
            "invoke %s/%s.%s error in %.1fms: %s",
            tenant, device_id, function, (time.monotonic() - t0) * 1000, e,
        )
        return {"error": {"code": -3, "message": str(e)}}
