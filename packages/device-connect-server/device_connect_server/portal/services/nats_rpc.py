# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""NATS helpers: RPC invocation and event streaming."""

import json
import logging
import uuid
from pathlib import Path

import nats

from .. import config

logger = logging.getLogger(__name__)

# Registry credentials (privileged, can reach all tenants)
_REGISTRY_CREDS = Path(config.CREDS_DIR) / "registry.creds.json"


def _load_creds() -> dict:
    """Load registry credentials for NATS auth."""
    if _REGISTRY_CREDS.exists():
        with open(_REGISTRY_CREDS) as f:
            return json.load(f)
    return {}


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
    nc = await connect()
    try:
        subject = f"device-connect.{tenant}.{device_id}.cmd"
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": function,
            "params": params,
        }

        msg = await nc.request(subject, json.dumps(payload).encode(), timeout=timeout)
        return json.loads(msg.data)
    except nats.errors.NoRespondersError:
        return {"error": {"code": -1, "message": f"Device {device_id} is not responding"}}
    except nats.errors.TimeoutError:
        return {"error": {"code": -2, "message": f"Request timed out after {timeout}s"}}
    except Exception as e:
        return {"error": {"code": -3, "message": str(e)}}
    finally:
        await nc.close()
