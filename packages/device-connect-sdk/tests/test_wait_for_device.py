"""Tests for DeviceDriver.wait_for_device() and depends_on startup gating."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from device_connect_sdk.drivers import DeviceDriver, rpc
from device_connect_sdk.errors import DeviceDependencyError


class SimpleDriver(DeviceDriver):
    device_type = "test"

    @rpc()
    async def ping(self) -> dict:
        return {"pong": True}

    async def connect(self):
        pass

    async def disconnect(self):
        pass


class DependentDriver(DeviceDriver):
    device_type = "orchestrator"
    depends_on = ("robot", "speaker")

    async def connect(self):
        pass

    async def disconnect(self):
        pass


class TestDependsOn:
    def test_default_is_empty(self):
        driver = SimpleDriver()
        assert driver.depends_on == ()

    def test_subclass_can_override(self):
        driver = DependentDriver()
        assert driver.depends_on == ("robot", "speaker")


class TestWaitForDevice:
    @pytest.mark.asyncio
    async def test_raises_if_no_filter(self):
        driver = SimpleDriver()
        driver._registry = AsyncMock()
        with pytest.raises(ValueError):
            await driver.wait_for_device()

    @pytest.mark.asyncio
    async def test_raises_if_no_registry(self):
        driver = SimpleDriver()
        with pytest.raises(RuntimeError):
            await driver.wait_for_device(device_type="robot")

    @pytest.mark.asyncio
    async def test_d2d_mode_delegates_to_collector_by_type(self):
        driver = SimpleDriver()
        driver._registry = AsyncMock()
        mock_collector = AsyncMock()
        mock_collector.wait_for_device_type = AsyncMock(
            return_value={"device_id": "robot-01"}
        )
        mock_runtime = MagicMock()
        mock_runtime._d2d_collector = mock_collector
        driver._device = mock_runtime

        result = await driver.wait_for_device(device_type="robot", timeout=5.0)
        assert result["device_id"] == "robot-01"
        mock_collector.wait_for_device_type.assert_called_once_with(
            "robot", timeout=5.0
        )

    @pytest.mark.asyncio
    async def test_d2d_mode_delegates_to_collector_by_id(self):
        driver = SimpleDriver()
        driver._registry = AsyncMock()
        mock_collector = AsyncMock()
        mock_collector.wait_for_device_id = AsyncMock(
            return_value={"device_id": "robot-01"}
        )
        mock_runtime = MagicMock()
        mock_runtime._d2d_collector = mock_collector
        driver._device = mock_runtime

        result = await driver.wait_for_device(device_id="robot-01", timeout=5.0)
        assert result["device_id"] == "robot-01"
        mock_collector.wait_for_device_id.assert_called_once_with(
            "robot-01", timeout=5.0
        )

    @pytest.mark.asyncio
    async def test_d2d_mode_raises_on_timeout(self):
        driver = SimpleDriver()
        driver._registry = AsyncMock()
        mock_collector = AsyncMock()
        mock_collector.wait_for_device_type = AsyncMock(return_value=None)
        mock_runtime = MagicMock()
        mock_runtime._d2d_collector = mock_collector
        driver._device = mock_runtime

        with pytest.raises(DeviceDependencyError) as exc_info:
            await driver.wait_for_device(device_type="robot", timeout=0.5)
        assert exc_info.value.device_type == "robot"
        assert exc_info.value.timeout == 0.5

    @pytest.mark.asyncio
    async def test_registry_mode_polls_list_devices(self):
        driver = SimpleDriver()
        call_count = 0

        async def mock_list(device_type=None, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return [{"device_id": "robot-01"}]
            return []

        driver._registry = AsyncMock()
        driver._registry.list_devices = mock_list
        # No _d2d_collector → falls through to registry path
        driver._device = MagicMock(spec=[])

        result = await driver.wait_for_device(device_type="robot", timeout=5.0)
        assert result["device_id"] == "robot-01"

    @pytest.mark.asyncio
    async def test_registry_mode_raises_on_timeout(self):
        driver = SimpleDriver()
        driver._registry = AsyncMock()
        driver._registry.list_devices = AsyncMock(return_value=[])
        driver._device = MagicMock(spec=[])

        with pytest.raises(DeviceDependencyError):
            await driver.wait_for_device(device_type="robot", timeout=0.3)
