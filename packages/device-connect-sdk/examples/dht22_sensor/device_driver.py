#!/usr/bin/env python3
"""DHT22 Temperature & Humidity Sensor driver.

This driver runs as a Python process on the physical device (e.g. Raspberry Pi).
It reads real temperature and humidity from a DHT22 sensor and exposes the data
over the Device Connect mesh so AI agents can discover and interact with it.

Requirements (install on the device):
    pip install adafruit-circuitpython-dht

Wiring:
    DHT22 VCC  → Pi 3.3V
    DHT22 DATA → Pi GPIO4 (pin 7)
    DHT22 GND  → Pi GND

Usage:
    NATS_CREDENTIALS_FILE=~/.device-connect/credentials/sensor-001.creds.json python device_driver.py
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import os
import signal

import board
import adafruit_dht

from device_connect_sdk import DeviceRuntime
from device_connect_sdk.drivers import DeviceDriver, rpc, emit, periodic
from device_connect_sdk.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("dht22-sensor")


class DHT22Driver(DeviceDriver):
    """Real DHT22 temperature/humidity sensor driver."""

    device_type = "temperature_sensor"

    def __init__(self, gpio_pin=board.D4, interval: float = 10.0):
        super().__init__()
        self._gpio_pin = gpio_pin
        self._interval = interval
        self._sensor: adafruit_dht.DHT22 | None = None

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="temperature_sensor",
            manufacturer="Adafruit",
            model="DHT22",
            firmware_version="1.0.0",
            description="DHT22 temperature and humidity sensor",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(availability="available")

    @emit()
    async def reading_available(self, temperature: float, humidity: float):
        """A new sensor reading is available."""
        pass

    @rpc()
    async def get_reading(self) -> dict:
        """Return the current temperature and humidity."""
        try:
            temperature = self._sensor.temperature
            humidity = self._sensor.humidity
            log.info("Reading: %.1f°C, %.1f%%", temperature, humidity)
            await self.reading_available(temperature=temperature, humidity=humidity)
            return {"temperature": temperature, "humidity": humidity}
        except RuntimeError as e:
            log.warning("Sensor read failed (retryable): %s", e)
            return {"error": str(e)}

    @periodic(interval=10.0)
    async def poll(self):
        """Poll the sensor at a regular interval."""
        await self.get_reading()

    async def connect(self) -> None:
        self._sensor = adafruit_dht.DHT22(self._gpio_pin)
        self.poll.__func__._routine_interval = self._interval
        log.info("DHT22 sensor initialised on %s", self._gpio_pin)

    async def disconnect(self) -> None:
        if self._sensor:
            self._sensor.exit()
        log.info("DHT22 sensor released")


async def run(device_id: str, interval: float):
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
    credentials_file = os.getenv("NATS_CREDENTIALS_FILE")

    if not credentials_file:
        log.error("NATS_CREDENTIALS_FILE is required for real hardware devices")
        return

    driver = DHT22Driver(interval=interval)
    device = DeviceRuntime(
        driver=driver,
        device_id=device_id,
        messaging_urls=[nats_url],
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    log.info("=" * 50)
    log.info("  DHT22 Sensor Driver")
    log.info("  Device ID : %s", device_id)
    log.info("  Interval  : %.1fs", interval)
    log.info("  Press Ctrl+C to stop")
    log.info("=" * 50)

    device_task = asyncio.create_task(device.run())
    await stop_event.wait()
    await device.stop()

    if not device_task.done():
        device_task.cancel()
        try:
            await device_task
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser(description="DHT22 sensor driver")
    parser.add_argument("--device-id", default="dht22-001")
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args()
    asyncio.run(run(device_id=args.device_id, interval=args.interval))


if __name__ == "__main__":
    main()
