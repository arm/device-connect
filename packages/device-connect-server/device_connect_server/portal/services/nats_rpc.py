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
_invoke_client_lock = asyncio.Lock()


def _load_creds() -> dict:
    """Load registry credentials for NATS auth."""
    if _REGISTRY_CREDS.exists():
        with open(_REGISTRY_CREDS) as f:
            return json.load(f)
    return {}


async def _get_invoke_client():
    """Lazily open and cache a single NATS client for RPC invocations."""
    global _invoke_client
    async with _invoke_client_lock:
        if _invoke_client is None or _invoke_client.is_closed:
            _invoke_client = await connect()
            logger.info("invoke client connected; will be reused across requests")
        return _invoke_client


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
        # On a hard transport failure, drop the cached client so the next
        # call reconnects rather than reusing a dead connection.
        global _invoke_client
        async with _invoke_client_lock:
            _invoke_client = None
        logger.exception(
            "invoke %s/%s.%s error in %.1fms: %s",
            tenant, device_id, function, (time.monotonic() - t0) * 1000, e,
        )
        return {"error": {"code": -3, "message": str(e)}}
