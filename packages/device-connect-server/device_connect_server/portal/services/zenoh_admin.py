"""Zenoh router lifecycle — config reload via Docker container restart."""

import asyncio
import logging

from .. import config

logger = logging.getLogger(__name__)


async def reload_zenoh() -> dict:
    """Restart the Zenoh router container to pick up config changes.

    Returns status dict with {success, message}.
    """
    container = config.ZENOH_CONTAINER
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            logger.info("Restarted Zenoh container: %s", container)
            return {
                "success": True,
                "message": f"Zenoh container '{container}' restarted with updated config",
            }
        else:
            msg = stderr.decode().strip()
            return {"success": False, "message": f"Container restart failed: {msg}"}
    except FileNotFoundError:
        return {"success": False, "message": "docker CLI not available"}
    except asyncio.TimeoutError:
        return {"success": False, "message": "Timeout restarting container"}
