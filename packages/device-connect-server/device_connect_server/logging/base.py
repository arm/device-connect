# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Abstract base class for audit logging.

This module defines the AuditLogger interface for pluggable logging
backends. Implementations can use MongoDB, PostgreSQL, S3, or other
storage systems.

Log Entry Types:
    - event: Device events (objectDetected, motionDetected, etc.)
    - tool_call: Function invocations on devices
    - assistant: LLM/orchestrator responses and reasoning
    - event_subscription_change: Subscription state changes
    - device_list_change: Device online/offline status

The log format is designed for UI compatibility with the orchestrator
dashboard, which expects specific field structures.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LogEntryType(str, Enum):
    """Types of audit log entries."""

    EVENT = "event"
    TOOL_CALL = "tool_call"
    ASSISTANT = "assistant"
    EVENT_SUBSCRIPTION_CHANGE = "event_subscription_change"
    DEVICE_LIST_CHANGE = "device_list_change"


@dataclass
class LogEntry:
    """A single audit log entry.

    This dataclass represents a log entry with all fields needed
    for UI compatibility. The `type` field determines which other
    fields are relevant.

    Attributes:
        timestamp: Unix timestamp of the log entry
        type: Log entry type (event, tool_call, assistant, etc.)
        device_id: Device identifier (for event, tool_call)
        event_name: Event name (for event entries)
        event_id: Unique event identifier
        event_params: Event parameters (for event entries)
        function_name: Function name (for tool_call entries)
        parameters: Function parameters (for tool_call entries)
        result: Function result (for tool_call entries)
        log_entry: LLM reasoning/explanation (for tool_call entries)
        tool_call_id: Tool call ID (for tool_call entries)
        round: LLM conversation round (for assistant entries)
        content: LLM response content (for assistant entries)
        tool_calls: List of tool calls made (for assistant entries)
        subscribed: Newly subscribed events (for subscription changes)
        unsubscribed: Newly unsubscribed events (for subscription changes)
        status: Device status (for device_list_change)
        extra: Additional fields for extensibility
    """

    timestamp: float = field(default_factory=time.time)
    type: LogEntryType = LogEntryType.EVENT

    # Event fields
    device_id: Optional[str] = None
    event_name: Optional[str] = None
    event_id: Optional[str] = None
    event_params: Optional[Dict[str, Any]] = None

    # Tool call fields
    function_name: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
    log_entry: Optional[str] = None
    tool_call_id: Optional[str] = None

    # Assistant fields
    round: Optional[int] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

    # Subscription change fields
    subscribed: Optional[List[Dict[str, str]]] = None
    unsubscribed: Optional[List[Dict[str, str]]] = None

    # Device list change fields
    status: Optional[str] = None

    # Extensibility
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage.

        Returns a dict with only non-None fields, matching the
        existing MongoDB log format.
        """
        result: Dict[str, Any] = {
            "timestamp": self.timestamp,
            "type": self.type.value if isinstance(self.type, LogEntryType) else self.type,
        }

        # Add non-None fields
        for field_name in [
            "device_id", "event_name", "event_id", "event_params",
            "function_name", "parameters", "result", "log_entry", "tool_call_id",
            "round", "content", "tool_calls",
            "subscribed", "unsubscribed", "status",
        ]:
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value

        # Merge extra fields
        result.update(self.extra)

        return result


class AuditLogger(ABC):
    """Abstract base class for audit logging.

    Implementations must provide connect, close, and log methods.
    Convenience methods for specific log types are provided.

    Example:
        class FileAuditLogger(AuditLogger):
            async def connect(self) -> None:
                self._file = open("audit.log", "a")

            async def close(self) -> None:
                self._file.close()

            async def log(self, entry: LogEntry) -> None:
                self._file.write(json.dumps(entry.to_dict()) + "\\n")
    """

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the logging backend.

        Should be called before logging any entries.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the connection to the logging backend."""
        pass

    @abstractmethod
    async def log(self, entry: LogEntry) -> None:
        """Log an entry to the backend.

        Args:
            entry: The log entry to store
        """
        pass

    async def __aenter__(self) -> "AuditLogger":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    # Convenience methods for specific log types

    async def log_event(
        self,
        device_id: str,
        event_name: str,
        event_id: Optional[str] = None,
        event_params: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Log a device event.

        Args:
            device_id: Device that emitted the event
            event_name: Event name (e.g., "event/objectDetected")
            event_id: Unique event identifier
            event_params: Event parameters
            timestamp: Event timestamp (default: now)
        """
        entry = LogEntry(
            timestamp=timestamp or time.time(),
            type=LogEntryType.EVENT,
            device_id=device_id,
            event_name=event_name,
            event_id=event_id,
            event_params=event_params,
        )
        await self.log(entry)

    async def log_tool_call(
        self,
        device_id: str,
        function_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        result: Optional[Any] = None,
        log_entry: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Log a function invocation on a device.

        Args:
            device_id: Target device
            function_name: Function that was called
            parameters: Function parameters
            result: Function result
            log_entry: LLM reasoning for the call
            tool_call_id: Tool call ID from LLM
            event_id: Related event ID
            timestamp: Call timestamp (default: now)
        """
        entry = LogEntry(
            timestamp=timestamp or time.time(),
            type=LogEntryType.TOOL_CALL,
            device_id=device_id,
            function_name=function_name,
            parameters=parameters,
            result=result,
            log_entry=log_entry,
            tool_call_id=tool_call_id,
            event_id=event_id,
        )
        await self.log(entry)

    async def log_assistant(
        self,
        event_id: Optional[str] = None,
        round_num: Optional[int] = None,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Log an LLM/assistant response.

        Args:
            event_id: Related event ID
            round_num: Conversation round number
            content: Assistant response text
            tool_calls: List of tool calls made
            timestamp: Response timestamp (default: now)
        """
        entry = LogEntry(
            timestamp=timestamp or time.time(),
            type=LogEntryType.ASSISTANT,
            event_id=event_id,
            round=round_num,
            content=content,
            tool_calls=tool_calls,
        )
        await self.log(entry)

    async def log_subscription_change(
        self,
        subscribed: Optional[List[Dict[str, str]]] = None,
        unsubscribed: Optional[List[Dict[str, str]]] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Log event subscription changes.

        Args:
            subscribed: List of {"device_id", "event_name"} newly subscribed
            unsubscribed: List of {"device_id", "event_name"} unsubscribed
            timestamp: Change timestamp (default: now)
        """
        entry = LogEntry(
            timestamp=timestamp or time.time(),
            type=LogEntryType.EVENT_SUBSCRIPTION_CHANGE,
            subscribed=subscribed or [],
            unsubscribed=unsubscribed or [],
        )
        await self.log(entry)

    async def log_device_status(
        self,
        device_id: str,
        status: str,
        timestamp: Optional[float] = None,
    ) -> None:
        """Log a device online/offline status change.

        Args:
            device_id: Device identifier
            status: Status string ("online" or "offline")
            timestamp: Change timestamp (default: now)
        """
        entry = LogEntry(
            timestamp=timestamp or time.time(),
            type=LogEntryType.DEVICE_LIST_CHANGE,
            device_id=device_id,
            status=status,
        )
        await self.log(entry)
