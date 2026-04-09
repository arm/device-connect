"""Device management views: create, list, download credentials and bundles."""

import html as _html

import aiohttp_jinja2
from aiohttp import web

from .. import config
from ..services import credentials, bundles, registry_client
from ..services.backend import get_backend, validate_name


def setup_routes(app: web.Application):
    app.router.add_get("/devices", devices_page)
    app.router.add_get("/devices/{name}", device_detail_page)
    app.router.add_post("/api/devices", create_device)
    app.router.add_get("/api/devices/starter-script", download_starter_script)
    app.router.add_get("/api/devices/agent-script", download_agent_script)
    app.router.add_get("/api/devices/agent-creds", download_agent_creds)
    app.router.add_get("/api/devices/demo-bundle", download_demo_bundle)
    app.router.add_get("/api/devices/{name}/creds", download_credential)
    app.router.add_get("/api/devices/bundle", download_bundle)


def _public_host(request: web.Request) -> str:
    """Extract the public hostname/IP from the request (strip port)."""
    return request.host.rsplit(":", 1)[0]


async def devices_page(request: web.Request):
    user = request["user"]
    tenant = user["tenant"]
    creds = credentials.list_credentials(tenant=tenant)
    backend = get_backend()
    broker_info = backend.broker_display_info()

    return aiohttp_jinja2.render_template("devices/list.html", request, {
        "user": user,
        "nav": "devices",
        "tenant": tenant,
        "credentials": creds,
        "public_host": _public_host(request),
        "nats_port": broker_info.get("port", ""),
        "readonly": False,
    })


async def device_detail_page(request: web.Request):
    user = request["user"]
    tenant = user["tenant"]
    device_name = request.match_info["name"]

    # Verify the device belongs to the requesting user's tenant (admins bypass)
    cred_data = credentials.get_credential_data(f"{device_name}.creds.json")
    if cred_data and user.get("role") != "admin":
        cred_tenant = cred_data.get("tenant", "")
        if cred_tenant != tenant:
            raise web.HTTPForbidden(text="Access denied: device belongs to another tenant")

    # Try to get live data from registry
    device = None
    try:
        device = registry_client.get_device(tenant, device_name)
    except Exception:
        pass

    if not device:
        # Fallback to credential data
        device = {
            "device_id": device_name,
            "device_type": "",
            "status": "unknown",
            "location": "",
            "last_seen": "",
            "capabilities": [],
            "tenant": cred_data.get("tenant", tenant) if cred_data else tenant,
        }

    cred_file = credentials.get_credential(f"{device_name}.creds.json")
    backend = get_backend()
    broker_info = backend.broker_display_info()

    return aiohttp_jinja2.render_template("devices/detail.html", request, {
        "user": user,
        "nav": "devices",
        "device": device,
        "cred_filename": cred_file.name if cred_file else None,
        "public_host": _public_host(request),
        "nats_port": broker_info.get("port", ""),
    })


async def create_device(request: web.Request):
    """Create a new device credential. Returns HTML fragment for htmx."""
    user = request["user"]
    tenant = user["tenant"]
    data = await request.post()
    device_name = data.get("device_name", "").strip()

    if not device_name:
        return web.Response(
            text='<div class="px-5 py-3 text-sm text-red-600">Device name is required</div>',
            content_type="text/html",
        )

    # Prefix with tenant name for uniqueness
    full_name = f"{tenant}-{device_name}"

    try:
        validate_name(full_name, "device name")
    except ValueError as e:
        return web.Response(
            text=f'<div class="px-5 py-3 text-sm text-red-600">{_html.escape(str(e))}</div>',
            content_type="text/html",
        )

    backend = get_backend()
    if not backend.is_bootstrapped():
        return web.Response(
            text='<div class="px-5 py-3 text-sm text-red-600">System not bootstrapped — ask admin to run setup first</div>',
            content_type="text/html",
        )

    try:
        broker_info = backend.broker_display_info()
        await backend.add_device(
            tenant, full_name,
            host=broker_info["host"], port=broker_info["port"],
        )
        await backend.reload_broker()
    except Exception as e:
        return web.Response(
            text=f'<div class="px-5 py-3 text-sm text-red-600">Failed to create device: {_html.escape(str(e))}</div>',
            content_type="text/html",
        )

    # Return the new row as HTML fragment
    cred = {
        "device_id": full_name,
        "filename": f"{full_name}.creds.json",
    }
    return aiohttp_jinja2.render_template("devices/_device_row.html", request, {
        "cred": cred,
        "user": user,
    })


async def download_credential(request: web.Request):
    """Download a single credential file."""
    user = request["user"]
    tenant = user["tenant"]
    device_name = request.match_info["name"]
    filename = f"{device_name}.creds.json"

    # Verify the credential belongs to the requesting user's tenant (admins bypass)
    cred_data = credentials.get_credential_data(filename)
    if cred_data and user.get("role") != "admin":
        cred_tenant = cred_data.get("tenant", "")
        if cred_tenant != tenant:
            raise web.HTTPForbidden(text="Access denied: credential belongs to another tenant")

    cred_path = credentials.get_credential(filename)
    if not cred_path:
        raise web.HTTPNotFound(text=f"Credential file not found: {filename}")

    return web.FileResponse(
        cred_path,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def download_bundle(request: web.Request):
    """Download a tenant credential bundle as .zip."""
    user = request["user"]
    tenant = request.query.get("tenant") or user["tenant"]

    # Non-admin users can only download their own tenant's bundle
    if tenant != user["tenant"] and user.get("role") != "admin":
        raise web.HTTPForbidden(text="Access denied: cannot download another tenant's bundle")

    bundle_bytes = bundles.create_bundle(tenant, public_host=_public_host(request))
    return web.Response(
        body=bundle_bytes,
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{tenant}-credentials.zip"',
        },
    )


STARTER_SCRIPT = '''\
#!/usr/bin/env python3
"""Device Connect — starter device script.

Usage:
    export NATS_CREDENTIALS_FILE=./your-device.creds.json
    export NATS_URL=nats://your-server:4222
    python my_device.py

The device ID and tenant are read automatically from the credentials file.
"""

import asyncio
import logging
import signal

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit, periodic
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("my-device")


class MyDeviceDriver(DeviceDriver):
    """Replace with your own device logic."""

    device_type = "my_device"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="my_device",
            manufacturer="My Company",
            model="v1",
            firmware_version="0.1.0",
            description="My custom device",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="lab", availability="available")

    # # ── RPC functions (uncomment to enable) ──────────────────────
    #
    # @rpc()
    # async def hello(self, name: str = "world") -> dict:
    #     """Example RPC — callable by agents or other devices."""
    #     return {"message": f"Hello, {name}!"}
    #
    # @rpc()
    # async def get_status(self) -> dict:
    #     """Return device status."""
    #     return {"status": "ok"}

    # # ── Events (uncomment to enable) ───────────────────────────────
    #
    # @emit()
    # async def measurement_taken(self, value: float, unit: str):
    #     """Emitted when a new measurement is taken."""
    #     pass  # framework broadcasts the event automatically
    #
    # # Then call it from any method:  await self.measurement_taken(value=23.5, unit="C")

    # # ── Periodic tasks (uncomment to enable) ─────────────────────
    #
    # @periodic(interval=10.0)
    # async def heartbeat(self):
    #     """Runs every 10 seconds."""
    #     log.info("heartbeat")
    #     # await self.measurement_taken(value=23.5, unit="C")  # emit an event

    async def connect(self) -> None:
        log.info("Device connected")

    async def disconnect(self) -> None:
        log.info("Device disconnecting")


async def run():
    driver = MyDeviceDriver()

    # device_id and tenant are auto-detected from NATS_CREDENTIALS_FILE
    device = DeviceRuntime(driver=driver)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("Starting device %s …", device.device_id)
    task = asyncio.create_task(device.run())
    await stop.wait()
    await device.stop()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(run())
'''


async def download_starter_script(request: web.Request):
    """Download a blank starter device script."""
    return web.Response(
        text=STARTER_SCRIPT,
        content_type="text/x-python",
        headers={
            "Content-Disposition": 'attachment; filename="my_device.py"',
        },
    )


AGENT_SCRIPT = '''\
#!/usr/bin/env python3
"""Device Connect — starter AI agent (Strands + OpenAI).

Connects to Device Connect, discovers your fleet, and reacts to device
events by calling tools (list_devices, get_device_functions, invoke_device).
LLM inference runs through the Arm internal OpenAI proxy.

Usage:
    pip install \\
        'device-connect-edge@git+https://github.com/arm/device-connect.git@main#subdirectory=packages/device-connect-edge' \\
        'device-connect-agent-tools[strands]@git+https://github.com/arm/device-connect.git@main#subdirectory=packages/device-connect-agent-tools' \\
        'strands-agents[openai]'

    export MESSAGING_BACKEND=nats
    export NATS_URL=nats://<host>:<port>
    export NATS_CREDENTIALS_FILE=./<your-tenant>-agent.creds.json
    export DEVICE_CONNECT_ZONE=<your-tenant>
    export OPENAI_API_KEY=<arm-proxy-token>
    export OPENAI_BASE_URL=https://openai-api-proxy.geo.arm.com/api/providers/openai-eu/v1
    export OPENAI_INSECURE=1

    python run_agent.py
"""

import asyncio
import logging
import os
import signal
from collections import defaultdict
from typing import Any, Dict, Optional

# ── 1. Force NATS backend BEFORE strands/openai import ──────────────
os.environ.setdefault("MESSAGING_BACKEND", "nats")

# ── 2. Disable SSL verification globally for the Arm internal proxy ──
#     (must run BEFORE openai/httpx imports so the patched default sticks)
if os.environ.get("OPENAI_INSECURE") == "1":
    import httpx
    _orig_async = httpx.AsyncClient.__init__
    _orig_sync = httpx.Client.__init__

    def _patched_async(self, *a, **kw):
        kw.setdefault("verify", False)
        _orig_async(self, *a, **kw)

    def _patched_sync(self, *a, **kw):
        kw.setdefault("verify", False)
        _orig_sync(self, *a, **kw)

    httpx.AsyncClient.__init__ = _patched_async
    httpx.Client.__init__ = _patched_sync
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

from device_connect_agent_tools.agent import DeviceConnectAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("agent")


class StrandsOpenAIDeviceConnectAgent(DeviceConnectAgent):
    """DeviceConnectAgent that uses Strands Agent with an OpenAI model."""

    def __init__(
        self,
        goal: str,
        model_id: str = "gpt-4o",
        max_tokens: int = 4096,
        client_args: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        super().__init__(goal=goal, **kwargs)
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._client_args = client_args or {}
        self._agent = None

    async def prepare(self) -> Dict[str, Any]:
        from strands import Agent
        from strands.models.openai import OpenAIModel
        from device_connect_agent_tools.adapters.strands import (
            describe_fleet, list_devices, get_device_functions,
            invoke_device, invoke_device_with_fallback, get_device_status,
        )

        result = await super().prepare()

        self._agent = Agent(
            model=OpenAIModel(
                client_args=self._client_args,
                model_id=self._model_id,
                params={"max_tokens": self._max_tokens},
            ),
            tools=[
                describe_fleet, list_devices, get_device_functions,
                invoke_device, invoke_device_with_fallback, get_device_status,
            ],
            system_prompt=self._build_system_prompt(),
        )
        return result

    def _run_agent_sync(self, prompt: str) -> str:
        log.info("Sending prompt to LLM (%d chars)", len(prompt))
        response = str(self._agent(prompt))
        log.info("Agent response: %s", response[:200])
        return response

    def _build_system_prompt(self) -> str:
        by_type: dict = defaultdict(lambda: {"count": 0, "locations": set()})
        for d in self.devices:
            dt = d.get("device_type") or d.get("identity", {}).get("device_type") or "?"
            loc = d.get("location") or d.get("status", {}).get("location") or "?"
            by_type[dt]["count"] += 1
            by_type[dt]["locations"].add(loc)

        lines = []
        for dt, info in sorted(by_type.items()):
            locs = ", ".join(sorted(info["locations"]))
            lines.append(f"  - {info['count']}x {dt} (at: {locs})")
        fleet = "\\n".join(lines) or "  (none yet — call describe_fleet() to refresh)"

        return (
            f"You are an AI agent connected to the Device Connect IoT network.\\n\\n"
            f"YOUR GOAL: {self.goal}\\n\\n"
            f"FLEET OVERVIEW ({len(self.devices)} devices):\\n{fleet}\\n\\n"
            f"DISCOVERY TOOLS:\\n"
            f"  - describe_fleet() — fleet summary\\n"
            f"  - list_devices(device_type=..., location=...) — browse devices\\n"
            f"  - get_device_functions(device_id) — see what a device can do\\n"
            f"  - invoke_device(device_id, function, params) — call a device function\\n\\n"
            f"INSTRUCTIONS:\\n"
            f"When you receive device events, you MUST:\\n"
            f"1. Analyze the events\\n"
            f"2. Use get_device_functions() to check available functions if needed\\n"
            f"3. Use invoke_device() to interact with devices\\n"
            f"4. Report what you found and what actions you took\\n\\n"
            f"Always provide llm_reasoning when invoking devices.\\n"
            f"Always call at least one tool per batch of events."
        )


async def main():
    client_args = {
        "api_key": os.environ["OPENAI_API_KEY"],
        "base_url": os.environ.get(
            "OPENAI_BASE_URL",
            "https://openai-api-proxy.geo.arm.com/api/providers/openai-eu/v1",
        ),
    }
    log.info("Using OpenAI base_url=%s", client_args["base_url"])

    agent = StrandsOpenAIDeviceConnectAgent(
        goal="Monitor the IoT fleet and react to events by calling device tools.",
        model_id=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        nats_url=os.environ.get("NATS_URL"),
        zone=os.environ.get("DEVICE_CONNECT_ZONE", "default"),
        client_args=client_args,
    )

    async with agent:
        log.info("Agent ready — discovered %d devices", len(agent.devices))
        for d in agent.devices:
            log.info("  - %s", d.get("device_id") or d)

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        run_task = asyncio.create_task(agent.run())
        await stop.wait()
        await agent.stop()
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
'''


async def download_agent_script(request: web.Request):
    """Download a starter AI agent script (Strands + OpenAI via Arm proxy)."""
    return web.Response(
        text=AGENT_SCRIPT,
        content_type="text/x-python",
        headers={
            "Content-Disposition": 'attachment; filename="run_agent.py"',
        },
    )


async def download_agent_creds(request: web.Request):
    """Get-or-create the per-tenant agent credential and stream it back.

    The agent uses the same JWT scope as a device (``device-connect.{tenant}.>``)
    but is named ``{tenant}-agent`` so it doesn't collide with real device IDs
    or get counted in the device list.
    """
    user = request["user"]
    tenant = user["tenant"]
    agent_name = f"{tenant}-agent"
    filename = f"{agent_name}.creds.json"

    cred_path = credentials.get_credential(filename)
    if not cred_path:
        backend = get_backend()
        if not backend.is_bootstrapped():
            raise web.HTTPServiceUnavailable(
                text="System not bootstrapped — ask admin to run setup first",
            )
        try:
            broker_info = backend.broker_display_info()
            await backend.add_device(
                tenant, agent_name,
                host=broker_info["host"], port=broker_info["port"],
            )
            await backend.reload_broker()
        except Exception as e:
            raise web.HTTPInternalServerError(
                text=f"Failed to create agent credential: {e}",
            )
        cred_path = credentials.get_credential(filename)
        if not cred_path:
            raise web.HTTPInternalServerError(
                text="Agent credential created but file not found",
            )

    return web.FileResponse(
        cred_path,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ── Smart Greenhouse demo bundle ────────────────────────────────

DEMO_SOIL_SENSOR = '''\
#!/usr/bin/env python3
"""Smart Greenhouse Demo — Soil Sensor

Periodically emits soil_reading events with temperature and humidity.
Other devices can subscribe to these readings via @on(device_type="soil_sensor").
"""

import asyncio
import logging
import random
import signal

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit, periodic
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s")
log = logging.getLogger("soil-sensor")


class SoilSensorDriver(DeviceDriver):
    """Simulated soil temperature & humidity sensor."""

    device_type = "soil_sensor"

    def __init__(self):
        super().__init__()
        self._base_temp = 25.0
        self._base_humidity = 60.0

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="soil_sensor",
            manufacturer="GreenTech",
            model="ST-200",
            firmware_version="1.0.0",
            description="Soil temperature and humidity sensor",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="greenhouse-zone-A", availability="available")

    @emit()
    async def soil_reading(self, temp: float, humidity: float):
        """Emitted every 5 seconds with current soil readings."""
        pass

    @rpc()
    async def get_reading(self) -> dict:
        """Return the latest sensor reading on demand."""
        temp = round(self._base_temp + random.uniform(-2, 8), 1)
        humidity = round(self._base_humidity + random.uniform(-10, 10), 1)
        return {"temp": temp, "humidity": humidity}

    @periodic(interval=5.0)
    async def emit_reading(self):
        """Take a reading and broadcast it."""
        temp = round(self._base_temp + random.uniform(-2, 8), 1)
        humidity = round(self._base_humidity + random.uniform(-10, 10), 1)
        log.info("soil reading: temp=%.1f°C humidity=%.1f%%", temp, humidity)
        await self.soil_reading(temp=temp, humidity=humidity)

    async def connect(self) -> None:
        log.info("Soil sensor online")

    async def disconnect(self) -> None:
        log.info("Soil sensor offline")


async def run():
    device = DeviceRuntime(driver=SoilSensorDriver(), ttl=60)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig_ in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig_, stop.set)
    log.info("Starting %s …", device.device_id)
    task = asyncio.create_task(device.run())
    await stop.wait()
    await device.stop()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(run())
'''

DEMO_IRRIGATION_PUMP = '''\
#!/usr/bin/env python3
"""Smart Greenhouse Demo — Irrigation Pump

Exposes RPC functions to control watering. Can be called directly
from the portal UI or by other devices (like the greenhouse controller).
"""

import asyncio
import logging
import signal

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s")
log = logging.getLogger("irrigation-pump")


class IrrigationPumpDriver(DeviceDriver):
    """Simulated irrigation pump with on/off control."""

    device_type = "irrigation_pump"

    def __init__(self):
        super().__init__()
        self._is_running = False
        self._total_liters = 0.0

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="irrigation_pump",
            manufacturer="AquaFlow",
            model="IP-100",
            firmware_version="2.1.0",
            description="Drip irrigation pump with flow control",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="greenhouse-zone-A", availability="available")

    @emit()
    async def pump_state_changed(self, running: bool, total_liters: float):
        """Emitted when the pump starts or stops."""
        pass

    @rpc()
    async def water_on(self, duration: int = 10) -> dict:
        """Start watering for the given duration (seconds)."""
        if self._is_running:
            return {"status": "already_running"}
        self._is_running = True
        log.info(">>> PUMP ON for %ds", duration)
        await self.pump_state_changed(running=True, total_liters=self._total_liters)
        # Simulate watering
        await asyncio.sleep(duration)
        self._total_liters += duration * 0.5  # 0.5 L/s
        self._is_running = False
        log.info(">>> PUMP OFF (delivered %.1fL)", duration * 0.5)
        await self.pump_state_changed(running=False, total_liters=self._total_liters)
        return {"status": "complete", "liters_delivered": duration * 0.5}

    @rpc()
    async def water_off(self) -> dict:
        """Emergency stop."""
        self._is_running = False
        log.info(">>> PUMP EMERGENCY STOP")
        await self.pump_state_changed(running=False, total_liters=self._total_liters)
        return {"status": "stopped"}

    @rpc()
    async def get_flow(self) -> dict:
        """Return pump status and total liters delivered."""
        return {
            "running": self._is_running,
            "total_liters": round(self._total_liters, 1),
        }

    async def connect(self) -> None:
        log.info("Irrigation pump online")

    async def disconnect(self) -> None:
        log.info("Irrigation pump offline")


async def run():
    device = DeviceRuntime(driver=IrrigationPumpDriver(), ttl=60)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig_ in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig_, stop.set)
    log.info("Starting %s …", device.device_id)
    task = asyncio.create_task(device.run())
    await stop.wait()
    await device.stop()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(run())
'''

DEMO_GREENHOUSE_CTRL = '''\
#!/usr/bin/env python3
"""Smart Greenhouse Demo — Greenhouse Controller (Orchestrator)

Subscribes to soil sensor readings via @on decorator (D2D).
When temperature exceeds the threshold, invokes the irrigation
pump's water_on RPC and emits an alert event.

This demonstrates:
  - @on: subscribing to events from other device types
  - invoke_remote: calling RPCs on other devices
  - @emit: broadcasting events for the portal to display
  - @rpc: manual override from portal UI
"""

import asyncio
import logging
import signal

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit, on
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s")
log = logging.getLogger("greenhouse-ctrl")

TEMP_THRESHOLD = 30.0


class GreenhouseControllerDriver(DeviceDriver):
    """Orchestrates sensor readings and pump control."""

    device_type = "greenhouse_controller"

    def __init__(self):
        super().__init__()
        self._last_temp = 0.0
        self._last_humidity = 0.0
        self._watering_count = 0

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="greenhouse_controller",
            manufacturer="GreenTech",
            model="GC-500",
            firmware_version="1.0.0",
            description="Greenhouse automation controller",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="greenhouse-control-room", availability="available")

    @emit()
    async def alert(self, reason: str, action: str, temp: float):
        """Emitted when the controller takes an automated action."""
        pass

    @rpc()
    async def get_state(self) -> dict:
        """Return current controller state."""
        return {
            "last_temp": self._last_temp,
            "last_humidity": self._last_humidity,
            "watering_count": self._watering_count,
            "threshold": TEMP_THRESHOLD,
        }

    async def _find_pump(self) -> str | None:
        """Discover the first irrigation pump on the network (with retry)."""
        for attempt in range(3):
            try:
                pumps = await self.list_devices(device_type="irrigation_pump")
                if pumps:
                    return pumps[0]["device_id"]
            except Exception as e:
                log.warning("Pump discovery attempt %d failed: %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(2)
        return None

    @rpc()
    async def trigger_water(self, duration: int = 10) -> dict:
        """Manual override: trigger watering from the portal."""
        pump_id = await self._find_pump()
        if not pump_id:
            return {"error": "No irrigation pump found on network"}
        log.info("Manual water trigger for %ds via %s", duration, pump_id)
        result = await self.invoke_remote(pump_id, "water_on", duration=duration)
        self._watering_count += 1
        await self.alert(
            reason="manual_override",
            action=f"watering {duration}s",
            temp=self._last_temp,
        )
        return {"status": "triggered", "pump_response": result}

    @on(device_type="soil_sensor", event_name="soil_reading")
    async def on_soil_reading(self, device_id: str, event_name: str, payload: dict):
        """React to soil sensor readings — water if too hot."""
        temp = payload.get("temp", 0)
        humidity = payload.get("humidity", 0)
        self._last_temp = temp
        self._last_humidity = humidity

        log.info("Received soil reading from %s: temp=%.1f humidity=%.1f", device_id, temp, humidity)

        if temp > TEMP_THRESHOLD:
            pump_id = await self._find_pump()
            if not pump_id:
                log.error("Temperature high but no pump found!")
                return
            log.warning("Temperature %.1f > %.1f — triggering %s!", temp, TEMP_THRESHOLD, pump_id)
            self._watering_count += 1
            await self.alert(
                reason=f"temp {temp}°C > {TEMP_THRESHOLD}°C",
                action="auto-watering 10s",
                temp=temp,
            )
            result = await self.invoke_remote(pump_id, "water_on", duration=10)
            log.info("Pump response: %s", result)

    async def connect(self) -> None:
        log.info("Greenhouse controller online (threshold=%.1f°C)", TEMP_THRESHOLD)

    async def disconnect(self) -> None:
        log.info("Greenhouse controller offline")


async def run():
    device = DeviceRuntime(driver=GreenhouseControllerDriver(), ttl=60)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig_ in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig_, stop.set)
    log.info("Starting %s …", device.device_id)
    task = asyncio.create_task(device.run())
    await stop.wait()
    await device.stop()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(run())
'''

def _demo_readme(tenant: str, public_host: str, nats_port: str, cred_names: list[str]) -> str:
    """Generate the demo README with real tenant/host/credential names."""
    cred1 = cred_names[0] if len(cred_names) > 0 else f"{tenant}-device-001.creds.json"
    cred2 = cred_names[1] if len(cred_names) > 1 else f"{tenant}-device-002.creds.json"
    cred3 = cred_names[2] if len(cred_names) > 2 else f"{tenant}-device-003.creds.json"
    nats_url = f"nats://{public_host}:{nats_port}"

    return f"""\
# Smart Greenhouse Demo

Three devices that demonstrate Device Connect's key features:

## Devices

| Script | Device Type | Role |
|--------|-------------|------|
| `soil_sensor.py` | soil_sensor | Emits temperature & humidity readings every 5s |
| `irrigation_pump.py` | irrigation_pump | Exposes water_on/water_off/get_flow RPCs |
| `greenhouse_ctrl.py` | greenhouse_controller | Orchestrates: reacts to sensor, controls pump |

## What it demonstrates

- **@periodic** — soil sensor emits readings on a timer
- **@emit** — events broadcast to all subscribers
- **@on** — controller subscribes to sensor events (D2D)
- **@rpc** — pump exposes callable functions
- **invoke_remote** — controller calls pump RPCs across devices
- **Portal UI** — invoke RPCs, watch live event streams

## Quick start

Each device needs its own credentials file. Assign one credential per terminal:

```bash
# Terminal 1 — Soil Sensor
export NATS_CREDENTIALS_FILE=./{cred1}
export NATS_URL={nats_url}
python soil_sensor.py

# Terminal 2 — Irrigation Pump
export NATS_CREDENTIALS_FILE=./{cred2}
export NATS_URL={nats_url}
python irrigation_pump.py

# Terminal 3 — Greenhouse Controller
export NATS_CREDENTIALS_FILE=./{cred3}
export NATS_URL={nats_url}
python greenhouse_ctrl.py
```

## What happens

1. Soil sensor emits `soil_reading` every 5 seconds (temp randomly 23-33°C)
2. Greenhouse controller receives readings via `@on` decorator
3. When temp > 30°C, controller calls `irrigation_pump.water_on(duration=10)`
4. Controller emits `alert` event (visible in portal "Live log")
5. Use the portal to invoke RPCs directly (e.g., pump `get_flow`)
"""


async def download_demo_bundle(request: web.Request):
    """Download the Smart Greenhouse demo as a .zip bundle."""
    import io
    import zipfile

    user = request["user"]
    tenant = user["tenant"]
    host = _public_host(request)
    creds = credentials.list_credentials(tenant=tenant)
    cred_names = [c["filename"] for c in creds[:3]]

    readme = _demo_readme(tenant, host, config.NATS_PORT, cred_names)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("greenhouse-demo/soil_sensor.py", DEMO_SOIL_SENSOR)
        zf.writestr("greenhouse-demo/irrigation_pump.py", DEMO_IRRIGATION_PUMP)
        zf.writestr("greenhouse-demo/greenhouse_ctrl.py", DEMO_GREENHOUSE_CTRL)
        zf.writestr("greenhouse-demo/README.md", readme)

    return web.Response(
        body=buf.getvalue(),
        content_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="greenhouse-demo.zip"',
        },
    )
