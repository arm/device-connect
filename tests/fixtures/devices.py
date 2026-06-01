# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Device spawning fixtures for cross-repo integration tests.

Uses device_connect_edge (device-connect-edge) — validates the edge SDK package.
"""

import asyncio
import logging
import uuid
from typing import Callable, List, Optional, Tuple

from device_connect_edge import DeviceRuntime

from drivers.camera import TestCameraDriver
from drivers.robot import TestRobotDriver
from drivers.sensor import TestSensorDriver

logger = logging.getLogger(__name__)

SCALE_SPAWN_CONCURRENCY = 32


class DeviceFactory:
    """Factory for spawning simulated test devices using device_connect_edge.

    Usage:
        factory = DeviceFactory(messaging_url="nats://localhost:4222")
        device, driver = await factory.spawn_camera("cam-001")
    """

    def __init__(self, messaging_url: str, tenant: str = "default", default_ttl: int = 15):
        self.messaging_url = messaging_url
        self.tenant = tenant
        self.default_ttl = default_ttl
        self._devices: List[DeviceRuntime] = []
        self._tasks: List[asyncio.Task] = []
        self._drivers: list = []

    async def _spawn(
        self,
        driver,
        device_id: str,
        wait_for_registration: bool = True,
        registration_timeout: float = 10.0,
    ) -> Tuple[DeviceRuntime, object]:
        """Common spawn logic for any driver."""
        driver._device_id = device_id

        device = DeviceRuntime(
            driver=driver,
            device_id=device_id,
            messaging_urls=[self.messaging_url],
            tenant=self.tenant,
            ttl=self.default_ttl,
            allow_insecure=True,
        )

        task = asyncio.create_task(device.run())
        self._devices.append(device)
        self._tasks.append(task)
        self._drivers.append(driver)

        if wait_for_registration:
            await self._wait_for_registration(device, registration_timeout)

        logger.info(f"Spawned {driver.__class__.__name__}: {device_id}")
        return device, driver

    async def spawn_camera(
        self,
        device_id: Optional[str] = None,
        failure_rate: float = 0.0,
        location: str = "test-zone",
        **kwargs,
    ) -> Tuple[DeviceRuntime, TestCameraDriver]:
        device_id = device_id or f"test-camera-{uuid.uuid4().hex[:6]}"
        driver = TestCameraDriver(failure_rate=failure_rate, location=location)
        return await self._spawn(driver, device_id, **kwargs)

    async def spawn_robot(
        self,
        device_id: Optional[str] = None,
        clean_duration: float = 0.5,
        failure_rate: float = 0.0,
        location: str = "test-zone",
        **kwargs,
    ) -> Tuple[DeviceRuntime, TestRobotDriver]:
        device_id = device_id or f"test-robot-{uuid.uuid4().hex[:6]}"
        driver = TestRobotDriver(clean_duration=clean_duration, failure_rate=failure_rate, location=location)
        return await self._spawn(driver, device_id, **kwargs)

    async def spawn_sensor(
        self,
        device_id: Optional[str] = None,
        failure_rate: float = 0.0,
        location: str = "test-room",
        initial_temp: float = 22.0,
        initial_humidity: float = 45.0,
        **kwargs,
    ) -> Tuple[DeviceRuntime, TestSensorDriver]:
        device_id = device_id or f"test-sensor-{uuid.uuid4().hex[:6]}"
        driver = TestSensorDriver(
            failure_rate=failure_rate, location=location,
            initial_temp=initial_temp, initial_humidity=initial_humidity,
        )
        return await self._spawn(driver, device_id, **kwargs)

    async def spawn_sensor_fleet(
        self,
        prefix: str,
        count: int,
        *,
        failure_rate: float = 0.0,
        location: str = "scale-room",
        location_for: Callable[[int], str] | None = None,
        initial_temp: float = 22.0,
        initial_humidity: float = 45.0,
        registration_timeout: float = 20.0,
        max_concurrent: int = SCALE_SPAWN_CONCURRENCY,
    ) -> list[Tuple[DeviceRuntime, TestSensorDriver]]:
        """Spawn many sensors concurrently for scale integration tests."""
        semaphore = asyncio.Semaphore(max(1, max_concurrent))

        async def spawn_one(index: int) -> Tuple[DeviceRuntime, TestSensorDriver]:
            async with semaphore:
                device, driver = await self.spawn_sensor(
                    f"{prefix}-{index:04d}",
                    failure_rate=failure_rate,
                    location=location_for(index) if location_for else location,
                    initial_temp=initial_temp + (index % 10) / 10,
                    initial_humidity=initial_humidity,
                    wait_for_registration=False,
                )
                await self._wait_for_registration(device, registration_timeout)
                return device, driver

        spawned = await asyncio.gather(*(spawn_one(i) for i in range(count)))
        return list(spawned)

    async def _wait_for_registration(self, device: DeviceRuntime, timeout: float) -> None:
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            if getattr(device, '_d2d_mode', False):
                # D2D mode: wait for announcer to start
                if getattr(device, '_d2d_announcer', None) is not None:
                    return
            else:
                # Registry mode: wait for registration ID
                if device._registration_id is not None:
                    return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Device {device.device_id} did not register within {timeout}s")

    async def cleanup(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._devices.clear()
        self._tasks.clear()
        self._drivers.clear()
