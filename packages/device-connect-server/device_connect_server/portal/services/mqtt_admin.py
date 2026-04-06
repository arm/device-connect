"""Mosquitto broker reload via Docker container SIGHUP."""

import asyncio
import logging

from .. import config

logger = logging.getLogger(__name__)


async def reload_mosquitto() -> dict:
    """Send SIGHUP to the Mosquitto container to reload password + ACL files.

    Returns status dict with {success, message}.
    """
    container = config.MQTT_CONTAINER
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", "--signal=SIGHUP", container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            logger.info("Sent SIGHUP to %s", container)
            return {
                "success": True,
                "message": f"Config reloaded, SIGHUP sent to {container}",
            }
        else:
            msg = stderr.decode().strip()
            return {"success": False, "message": f"Container signal failed: {msg}"}
    except FileNotFoundError:
        return {"success": False, "message": "docker CLI not available"}
    except asyncio.TimeoutError:
        return {"success": False, "message": "Timeout signaling container"}
