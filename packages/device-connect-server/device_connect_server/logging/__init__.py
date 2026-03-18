"""Logging framework for Device Connect.

This module provides pluggable audit logging with multiple backend support.
The MongoAuditLogger preserves the existing log format for UI compatibility.

Log Entry Types:
    - event: Device events received by orchestrator
    - tool_call: Function invocations on devices
    - assistant: LLM/orchestrator responses
    - event_subscription_change: Subscription changes

Example:
    from device_connect_server.logging import AuditLogger, MongoAuditLogger

    # Use MongoDB backend
    logger = MongoAuditLogger(mongo_uri="mongodb://localhost:27017/device_connect")
    await logger.connect()

    # Log an event
    await logger.log_event(
        device_id="camera-001",
        event_name="event/objectDetected",
        event_id="evt-123",
        event_params={"label": "person", "confidence": 0.95}
    )

    # Log a tool call
    await logger.log_tool_call(
        device_id="robot-001",
        function_name="dispatchRobot",
        parameters={"zone": "A"},
        result={"status": "dispatched"}
    )
"""
from device_connect_server.logging.base import AuditLogger, LogEntry
from device_connect_server.logging.mongo import MongoAuditLogger

__all__ = [
    "AuditLogger",
    "LogEntry",
    "MongoAuditLogger",
]
