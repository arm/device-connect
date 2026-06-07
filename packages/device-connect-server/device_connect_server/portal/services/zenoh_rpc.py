# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh helpers: RPC invocation and event streaming using ZenohAdapter."""

import asyncio
import json
import logging
import uuid
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

# Registry credentials (privileged, can reach all tenants)
_REGISTRY_CREDS = Path(config.CREDS_DIR) / "registry.creds.json"

# A single long-lived ZenohAdapter is cached for invoke() and reused across
# requests -- opening a fresh mTLS session per call (as the original code did)
# adds a full TLS handshake to every invocation. The adapter re-declares its
# own state on reconnect, so the cached client survives a router restart.
# The event-streaming path deliberately does NOT use this cached client: each
# stream owns its own adapter and closes it on unsubscribe (see zenoh_backend).
_invoke_client = None
_invoke_client_lock: "asyncio.Lock | None" = None


def _get_invoke_lock() -> "asyncio.Lock":
    global _invoke_client_lock
    if _invoke_client_lock is None:
        _invoke_client_lock = asyncio.Lock()
    return _invoke_client_lock


def _load_creds() -> dict:
    """Load registry credentials for Zenoh mTLS auth."""
    if _REGISTRY_CREDS.exists():
        with open(_REGISTRY_CREDS) as f:
            return json.load(f)
    return {}


async def connect():
    """Return a freshly connected ZenohAdapter using registry credentials.

    Used by the event-streaming path, which owns the returned adapter's
    lifecycle. invoke() uses the cached client from _get_invoke_client().
    """
    # ZenohAdapter is not re-exported from the package root; obtain it via the
    # messaging factory, the same way every other server module does.
    from device_connect_edge.messaging import create_client

    adapter = create_client("zenoh")
    creds = _load_creds()
    zenoh_cfg = creds.get("zenoh", {})

    servers = zenoh_cfg.get("urls", [f"zenoh+tls://{config.ZENOH_HOST}:{config.ZENOH_PORT}"])
    tls = zenoh_cfg.get("tls", {})

    await adapter.connect(servers=servers, tls_config=tls if tls else None)
    return adapter


async def _get_invoke_client():
    """Lazily open and cache a single ZenohAdapter for RPC invocations."""
    global _invoke_client
    async with _get_invoke_lock():
        if _invoke_client is None or _invoke_client.is_closed:
            _invoke_client = await connect()
            logger.info("zenoh invoke client connected; will be reused across requests")
        return _invoke_client


async def _drop_invoke_client() -> None:
    """Discard the cached client, best-effort closing whatever's there.

    Called after a hard transport failure so the next invoke() reconnects
    rather than reusing a half-dead session.
    """
    global _invoke_client
    async with _get_invoke_lock():
        stale = _invoke_client
        _invoke_client = None
    if stale is not None:
        try:
            await stale.close()
        except Exception:
            logger.debug("ignored error closing stale zenoh invoke client", exc_info=True)


async def close_invoke_client() -> None:
    """Close the cached invoke client at app shutdown (idempotent).

    Wire this into ``aiohttp.web.Application.on_cleanup`` so the long-lived
    mTLS session is released on graceful shutdown.
    """
    await _drop_invoke_client()


async def invoke(
    tenant: str, device_id: str, function: str,
    params: dict, timeout: float = 5.0,
) -> dict:
    """Send a JSON-RPC request to a device via Zenoh and return the response."""
    subject = f"device-connect.{tenant}.{device_id}.cmd"
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": function,
        "params": params,
    }
    try:
        adapter = await _get_invoke_client()
        result = await adapter.request(
            subject, json.dumps(payload).encode(), timeout=timeout,
        )
        return json.loads(result)
    except TimeoutError:
        return {"error": {"code": -2, "message": f"Request timed out after {timeout}s"}}
    except Exception as e:
        msg = str(e).lower()
        if "no respondent" in msg or "no responders" in msg or "timeout" in msg:
            return {"error": {"code": -1, "message": f"Device {device_id} is not responding"}}
        # Unknown transport-level failure -- drop the cached session so the
        # next call reconnects rather than reusing a wedged one.
        await _drop_invoke_client()
        return {"error": {"code": -3, "message": str(e)}}
