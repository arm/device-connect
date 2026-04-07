"""Device management views: create, list, download credentials and bundles."""

import aiohttp_jinja2
from aiohttp import web

from .. import config
from ..services import credentials, bundles, registry_client
from ..services.backend import get_backend


def setup_routes(app: web.Application):
    app.router.add_get("/devices", devices_page)
    app.router.add_get("/devices/{name}", device_detail_page)
    app.router.add_post("/api/devices", create_device)
    app.router.add_get("/api/devices/starter-script", download_starter_script)
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

    # Try to get live data from registry
    device = None
    try:
        device = registry_client.get_device(tenant, device_name)
    except Exception:
        pass

    if not device:
        # Fallback to credential data
        cred_data = credentials.get_credential_data(f"{device_name}.creds.json")
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
            text=f'<div class="px-5 py-3 text-sm text-red-600">Failed to create device: {e}</div>',
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
    device_name = request.match_info["name"]
    filename = f"{device_name}.creds.json"
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
    tenant = request.query.get("tenant")
    if not tenant:
        user = request["user"]
        tenant = user["tenant"]

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
