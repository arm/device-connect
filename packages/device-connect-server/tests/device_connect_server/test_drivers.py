"""Tests for device_connect_server.drivers module."""
import asyncio
import pytest
from device_connect_server.drivers import (
    DeviceDriver,
    rpc,
    emit,
    build_function_schema,
    build_event_schema,
)


class TestRpc:
    """Tests for @rpc decorator."""

    def test_decorator_marks_function(self):
        """Test that decorator marks function correctly."""

        @rpc()
        async def my_func(self, x: int) -> dict:
            """Do something."""
            return {"x": x}

        assert my_func._is_device_function is True
        assert my_func._function_name == "my_func"

    def test_custom_name(self):
        """Test custom function name."""

        @rpc(name="customName")
        async def my_func(self) -> dict:
            """A function."""
            return {}

        assert my_func._function_name == "customName"

    def test_description_from_docstring(self):
        """Test description extraction from docstring."""

        @rpc()
        async def capture(self, resolution: str = "1080p") -> dict:
            """Capture an image from the camera.

            Args:
                resolution: Image resolution
            """
            return {}

        assert capture._description == "Capture an image from the camera."

    def test_custom_description(self):
        """Test custom description override."""

        @rpc(description="Custom description")
        async def func(self) -> dict:
            """Original docstring."""
            return {}

        assert func._description == "Custom description"


class TestEmit:
    """Tests for @emit decorator."""

    def test_decorator_marks_event(self):
        """Test that decorator marks event correctly."""

        @emit("object_detected")
        async def object_detected(self, label: str):
            """Object detected."""
            pass

        assert object_detected._is_device_event is True
        assert object_detected._event_name == "object_detected"

    def test_event_description(self):
        """Test event description extraction."""

        @emit("motion_detected")
        async def motion_detected(self, zone: str):
            """Motion detected in zone.

            Args:
                zone: Zone identifier
            """
            pass

        assert motion_detected._event_description == "Motion detected in zone."


class TestBuildFunctionSchema:
    """Tests for build_function_schema function."""

    def test_simple_schema(self):
        """Test schema for simple function."""

        @rpc()
        async def func(self, name: str, count: int = 10) -> dict:
            """A function."""
            return {}

        schema = build_function_schema(func)
        assert schema["type"] == "object"
        assert "name" in schema["properties"]
        assert "count" in schema["properties"]
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert "name" in schema["required"]
        assert "count" not in schema["required"]  # Has default

    def test_schema_with_descriptions(self):
        """Test schema includes docstring descriptions."""

        @rpc()
        async def func(self, resolution: str = "1080p") -> dict:
            """Capture image.

            Args:
                resolution: Image resolution like 720p or 1080p
            """
            return {}

        schema = build_function_schema(func)
        assert "description" in schema["properties"]["resolution"]


class TestBuildEventSchema:
    """Tests for build_event_schema function."""

    def test_event_schema(self):
        """Test schema for event payload."""

        @emit("object_detected")
        async def object_detected(self, label: str, confidence: float):
            """Object detected.

            Args:
                label: Object class label
                confidence: Detection confidence
            """
            pass

        schema = build_event_schema(object_detected)
        assert schema["type"] == "object"
        assert "label" in schema["properties"]
        assert "confidence" in schema["properties"]
        assert schema["properties"]["confidence"]["type"] == "number"


class TestDeviceDriverCapabilities:
    """Tests for DeviceDriver capability discovery."""

    def test_capabilities_discovery(self):
        """Test that capabilities are discovered from decorators."""

        class CameraDriver(DeviceDriver):
            """A test camera driver."""
            device_type = "camera"

            @rpc()
            async def capture(self, resolution: str = "1080p") -> dict:
                """Capture image."""
                return {}

            @emit("image_captured")
            async def image_captured(self, url: str):
                """Image captured."""
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = CameraDriver()
        caps = driver.capabilities

        assert len(caps.functions) == 1
        assert caps.functions[0].name == "capture"

        assert len(caps.events) == 1
        assert caps.events[0].name == "image_captured"

    @pytest.mark.asyncio
    async def test_invoke_function(self):
        """Test invoking a function by name."""

        class TestDriver(DeviceDriver):
            device_type = "test"

            @rpc()
            async def add(self, a: int, b: int) -> dict:
                """Add two numbers."""
                return {"sum": a + b}

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        result = await driver.invoke("add", a=2, b=3)
        assert result["sum"] == 5

    @pytest.mark.asyncio
    async def test_invoke_unknown_function(self):
        """Test invoking unknown function raises error."""
        from device_connect_server.errors import FunctionInvocationError

        class TestDriver(DeviceDriver):
            device_type = "test"

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        with pytest.raises(FunctionInvocationError):
            await driver.invoke("nonexistent")


class TestBeforeEmit:
    """Tests for @before_emit decorator."""

    def test_decorator_marks_handler(self):
        """Test that decorator marks handler correctly."""
        from device_connect_server.drivers import before_emit

        @before_emit("mess_detected")
        async def on_mess(self, zone: str, severity: str, **kwargs):
            pass

        assert on_mess._is_internal_handler is True
        assert on_mess._internal_event_name == "mess_detected"
        assert on_mess._suppress_propagation is False

    def test_suppress_propagation_flag(self):
        """Test suppress_propagation decorator parameter."""
        from device_connect_server.drivers import before_emit

        @before_emit("event", suppress_propagation=True)
        async def handler(self, **kwargs):
            pass

        assert handler._suppress_propagation is True

    @pytest.mark.asyncio
    async def test_internal_handler_collection(self):
        """Test that internal handlers are collected."""

        from device_connect_server.drivers import before_emit

        class TestDriver(DeviceDriver):
            device_type = "test"

            @before_emit("event_a")
            async def handle_a(self, **kwargs):
                pass

            @before_emit("event_b")
            async def handle_b(self, **kwargs):
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        driver._collect_internal_handlers()

        assert "event_a" in driver._internal_handlers
        assert "event_b" in driver._internal_handlers
        assert len(driver._internal_handlers["event_a"]) == 1
        assert len(driver._internal_handlers["event_b"]) == 1

    @pytest.mark.asyncio
    async def test_internal_handler_dispatch_propagate(self):
        """Test dispatch returns True for propagation by default."""

        from device_connect_server.drivers import before_emit

        class TestDriver(DeviceDriver):
            device_type = "test"
            handler_called = False

            @before_emit("test_event")
            async def handle_event(self, value: int, **kwargs):
                self.handler_called = True

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        should_propagate, payload = await driver._dispatch_internal_event(
            "test_event", {"value": 42}
        )

        assert driver.handler_called is True
        assert should_propagate is True
        assert payload["value"] == 42

    @pytest.mark.asyncio
    async def test_internal_handler_suppress_with_false(self):
        """Test handler returning False suppresses propagation."""

        from device_connect_server.drivers import before_emit

        class TestDriver(DeviceDriver):
            device_type = "test"

            @before_emit("test_event")
            async def handle_event(self, **kwargs):
                return False  # Suppress

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        should_propagate, payload = await driver._dispatch_internal_event(
            "test_event", {"value": 1}
        )

        assert should_propagate is False

    @pytest.mark.asyncio
    async def test_internal_handler_modify_payload(self):
        """Test handler returning dict modifies payload."""

        from device_connect_server.drivers import before_emit

        class TestDriver(DeviceDriver):
            device_type = "test"

            @before_emit("test_event")
            async def handle_event(self, value: int, **kwargs):
                return {"value": value * 2, "modified": True}

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        should_propagate, payload = await driver._dispatch_internal_event(
            "test_event", {"value": 10}
        )

        assert should_propagate is True
        assert payload["value"] == 20
        assert payload["modified"] is True

    @pytest.mark.asyncio
    async def test_internal_handler_error_continues(self):
        """Test that handler errors don't break event flow."""

        from device_connect_server.drivers import before_emit

        class TestDriver(DeviceDriver):
            device_type = "test"
            second_called = False

            @before_emit("test_event")
            async def handle_first(self, **kwargs):
                raise RuntimeError("Handler error")

            @before_emit("test_event")
            async def handle_second(self, **kwargs):
                self.second_called = True

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        should_propagate, _ = await driver._dispatch_internal_event(
            "test_event", {}
        )

        # Event should still propagate despite error
        assert should_propagate is True
        assert driver.second_called is True


class TestPeriodic:
    """Tests for @periodic decorator."""

    def test_decorator_marks_routine(self):
        """Test that decorator marks routine correctly."""
        from device_connect_server.drivers import periodic

        @periodic(interval=5.0, wait_for_completion=True)
        async def detection_loop(self):
            pass

        assert detection_loop._is_device_routine is True
        assert detection_loop._routine_interval == 5.0
        assert detection_loop._routine_wait_for_completion is True
        assert detection_loop._routine_start_on_connect is True
        assert detection_loop._routine_name == "detection_loop"

    def test_custom_name(self):
        """Test custom routine name."""
        from device_connect_server.drivers import periodic

        @periodic(name="custom_loop")
        async def my_routine(self):
            pass

        assert my_routine._routine_name == "custom_loop"

    def test_start_on_connect_false(self):
        """Test start_on_connect parameter."""
        from device_connect_server.drivers import periodic

        @periodic(start_on_connect=False)
        async def manual_routine(self):
            pass

        assert manual_routine._routine_start_on_connect is False

    @pytest.mark.asyncio
    async def test_routine_collection(self):
        """Test that routines are collected."""
        from device_connect_server.drivers import periodic

        class TestDriver(DeviceDriver):
            device_type = "test"

            @periodic(interval=1.0)
            async def routine_a(self):
                pass

            @periodic(interval=2.0, start_on_connect=False)
            async def routine_b(self):
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        driver._collect_routines()

        assert "routine_a" in driver._routines
        assert "routine_b" in driver._routines
        assert driver._routines["routine_a"]["interval"] == 1.0
        assert driver._routines["routine_b"]["start_on_connect"] is False

    @pytest.mark.asyncio
    async def test_start_and_stop_routine(self):
        """Test starting and stopping a routine."""
        from device_connect_server.drivers import periodic

        class TestDriver(DeviceDriver):
            device_type = "test"
            run_count = 0

            @periodic(interval=0.01, start_on_connect=False)
            async def counter(self):
                self.run_count += 1

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()

        # Start routine
        await driver.start_routine("counter")
        await asyncio.sleep(0.05)  # Let it run a few times
        await driver.stop_routine("counter")

        assert driver.run_count > 0

    @pytest.mark.asyncio
    async def test_start_routines_auto(self):
        """Test that routines auto-start based on start_on_connect."""
        from device_connect_server.drivers import periodic

        class TestDriver(DeviceDriver):
            device_type = "test"
            auto_count = 0
            manual_count = 0

            @periodic(interval=0.01, start_on_connect=True)
            async def auto_routine(self):
                self.auto_count += 1

            @periodic(interval=0.01, start_on_connect=False)
            async def manual_routine(self):
                self.manual_count += 1

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        await driver._start_routines()
        await asyncio.sleep(0.05)
        await driver._stop_routines()

        assert driver.auto_count > 0
        assert driver.manual_count == 0

    @pytest.mark.asyncio
    async def test_routine_error_continues(self):
        """Test that routine errors don't stop the routine."""
        from device_connect_server.drivers import periodic

        class TestDriver(DeviceDriver):
            device_type = "test"
            run_count = 0

            @periodic(interval=0.01, start_on_connect=False)
            async def error_routine(self):
                self.run_count += 1
                if self.run_count < 3:
                    raise RuntimeError("Test error")

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        await driver.start_routine("error_routine")
        await asyncio.sleep(0.05)
        await driver.stop_routine("error_routine")

        # Should have continued despite errors
        assert driver.run_count >= 3

    @pytest.mark.asyncio
    async def test_get_routine_status(self):
        """Test get_routine_status method."""
        from device_connect_server.drivers import periodic

        class TestDriver(DeviceDriver):
            device_type = "test"

            @periodic(interval=1.0)
            async def my_routine(self):
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        status = driver.get_routine_status()

        assert "my_routine" in status
        assert status["my_routine"]["interval"] == 1.0
        assert status["my_routine"]["running"] is False

        await driver.start_routine("my_routine")
        status = driver.get_routine_status()
        assert status["my_routine"]["running"] is True

        await driver.stop_routine("my_routine")
