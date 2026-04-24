# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""NATS config regeneration and container reload."""

import asyncio
import logging

from .. import config
from . import nsc

logger = logging.getLogger(__name__)


async def reload_nats() -> dict:
    """Regenerate NATS config and send SIGHUP to the running container.

    Returns status dict.
    """
    await nsc.regenerate_config()

    # Try to signal the NATS container
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", "--signal=SIGHUP", config.NATS_CONTAINER,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            logger.info("Sent SIGHUP to %s", config.NATS_CONTAINER)
            return {"success": True, "message": f"Config reloaded, SIGHUP sent to {config.NATS_CONTAINER}"}
        else:
            msg = stderr.decode().strip()
            return {"success": False, "message": f"Container signal failed: {msg}"}
    except FileNotFoundError:
        return {"success": False, "message": "docker CLI not available"}
    except asyncio.TimeoutError:
        return {"success": False, "message": "Timeout signaling container"}
