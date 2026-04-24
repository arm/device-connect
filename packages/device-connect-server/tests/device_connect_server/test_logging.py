# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for device_connect_server.logging module."""
import pytest
from device_connect_server.logging import (
    AuditLogger,
    LogEntry,
    MongoAuditLogger,
)
from device_connect_server.logging.base import LogEntryType
from device_connect_server.logging.mongo import NullAuditLogger


class TestLogEntry:
    """Tests for LogEntry dataclass."""

    def test_default_entry(self):
        """Test default log entry values."""
        entry = LogEntry()
        assert entry.type == LogEntryType.EVENT
        assert entry.device_id is None
        assert entry.event_name is None
        assert isinstance(entry.timestamp, float)

    def test_event_entry(self):
        """Test event log entry."""
        entry = LogEntry(
            type=LogEntryType.EVENT,
            device_id="camera-001",
            event_name="event/detected",
            event_id="evt-123",
            event_params={"label": "person"},
        )
        assert entry.device_id == "camera-001"
        assert entry.event_name == "event/detected"
        assert entry.event_params["label"] == "person"

    def test_tool_call_entry(self):
        """Test tool call log entry."""
        entry = LogEntry(
            type=LogEntryType.TOOL_CALL,
            device_id="robot-001",
            function_name="dispatchRobot",
            parameters={"zone_id": "A"},
            result={"status": "queued"},
            tool_call_id="call-456",
        )
        assert entry.function_name == "dispatchRobot"
        assert entry.parameters["zone_id"] == "A"
        assert entry.result["status"] == "queued"

    def test_assistant_entry(self):
        """Test assistant log entry."""
        entry = LogEntry(
            type=LogEntryType.ASSISTANT,
            event_id="evt-123",
            round=1,
            content="Dispatching robot to zone A",
            tool_calls=[{"name": "dispatch", "args": {}}],
        )
        assert entry.round == 1
        assert "robot" in entry.content
        assert len(entry.tool_calls) == 1

    def test_subscription_change_entry(self):
        """Test subscription change log entry."""
        entry = LogEntry(
            type=LogEntryType.EVENT_SUBSCRIPTION_CHANGE,
            subscribed=[{"device_id": "camera-001", "event_name": "event/detected"}],
            unsubscribed=[],
        )
        assert len(entry.subscribed) == 1
        assert entry.subscribed[0]["device_id"] == "camera-001"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        entry = LogEntry(
            type=LogEntryType.EVENT,
            device_id="camera-001",
            event_name="event/detected",
        )
        d = entry.to_dict()
        assert d["type"] == "event"
        assert d["device_id"] == "camera-001"
        assert "timestamp" in d
        # None values should not be in dict
        assert "result" not in d

    def test_to_dict_with_extra(self):
        """Test extra fields are merged."""
        entry = LogEntry(
            type=LogEntryType.EVENT,
            device_id="test",
            extra={"custom_field": "value"},
        )
        d = entry.to_dict()
        assert d["custom_field"] == "value"


class TestNullAuditLogger:
    """Tests for NullAuditLogger."""

    @pytest.mark.asyncio
    async def test_null_logger_operations(self):
        """Test null logger doesn't raise errors."""
        logger = NullAuditLogger()
        await logger.connect()

        entry = LogEntry(type=LogEntryType.EVENT)
        await logger.log(entry)

        await logger.close()

    @pytest.mark.asyncio
    async def test_null_logger_context_manager(self):
        """Test null logger as context manager."""
        async with NullAuditLogger() as logger:
            await logger.log_event("camera-001", "event/detected")
            await logger.log_tool_call("robot-001", "dispatch", {})


class TestAuditLoggerConvenienceMethods:
    """Tests for AuditLogger convenience methods."""

    @pytest.mark.asyncio
    async def test_log_event(self):
        """Test log_event convenience method."""
        logged = []

        class TestLogger(AuditLogger):
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def log(self, entry: LogEntry) -> None:
                logged.append(entry)

        logger = TestLogger()
        await logger.log_event(
            device_id="camera-001",
            event_name="event/detected",
            event_id="evt-123",
            event_params={"label": "person"},
        )

        assert len(logged) == 1
        assert logged[0].type == LogEntryType.EVENT
        assert logged[0].device_id == "camera-001"

    @pytest.mark.asyncio
    async def test_log_tool_call(self):
        """Test log_tool_call convenience method."""
        logged = []

        class TestLogger(AuditLogger):
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def log(self, entry: LogEntry) -> None:
                logged.append(entry)

        logger = TestLogger()
        await logger.log_tool_call(
            device_id="robot-001",
            function_name="dispatchRobot",
            parameters={"zone_id": "A"},
            result={"status": "ok"},
        )

        assert len(logged) == 1
        assert logged[0].type == LogEntryType.TOOL_CALL
        assert logged[0].function_name == "dispatchRobot"

    @pytest.mark.asyncio
    async def test_log_subscription_change(self):
        """Test log_subscription_change convenience method."""
        logged = []

        class TestLogger(AuditLogger):
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def log(self, entry: LogEntry) -> None:
                logged.append(entry)

        logger = TestLogger()
        await logger.log_subscription_change(
            subscribed=[{"device_id": "camera-001", "event_name": "event/detected"}],
            unsubscribed=[{"device_id": "camera-002", "event_name": "event/motion"}],
        )

        assert len(logged) == 1
        assert logged[0].type == LogEntryType.EVENT_SUBSCRIPTION_CHANGE
        assert len(logged[0].subscribed) == 1
        assert len(logged[0].unsubscribed) == 1


class TestMongoAuditLogger:
    """Tests for MongoAuditLogger (without actual MongoDB)."""

    def test_initialization(self):
        """Test logger initialization."""
        logger = MongoAuditLogger(
            mongo_uri="mongodb://localhost:27017/test",
            database="test_db",
            collection="test_logs",
        )
        assert logger._mongo_uri == "mongodb://localhost:27017/test"
        assert logger._database_name == "test_db"
        assert logger._collection_name == "test_logs"

    def test_is_connected_false_initially(self):
        """Test is_connected is False before connect."""
        logger = MongoAuditLogger("mongodb://localhost:27017/test")
        assert logger.is_connected is False
