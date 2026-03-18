"""MongoDB implementation of the AuditLogger.

This module provides MongoAuditLogger, which stores audit logs in
MongoDB. The log format is compatible with the orchestrator UI.

Example:
    from device_connect_server.logging import MongoAuditLogger

    logger = MongoAuditLogger(
        mongo_uri="mongodb://user:pass@localhost:27017/device_connect"
    )

    async with logger:
        await logger.log_event(
            device_id="camera-001",
            event_name="event/objectDetected",
            event_params={"label": "person"}
        )
"""
from __future__ import annotations

import asyncio
import logging

from device_connect_server.logging.base import AuditLogger, LogEntry


class MongoAuditLogger(AuditLogger):
    """MongoDB-backed audit logger.

    Stores audit logs in a MongoDB collection with the format expected
    by the orchestrator UI dashboard.

    Args:
        mongo_uri: MongoDB connection URI
        database: Database name (default: "orchestrator_ui")
        collection: Collection name (default: "logs")
        connect_timeout_ms: Connection timeout (default: 5000)

    Example:
        logger = MongoAuditLogger(
            mongo_uri="mongodb://localhost:27017/device_connect",
            database="orchestrator_ui",
            collection="logs"
        )

        async with logger:
            await logger.log_event(...)
    """

    def __init__(
        self,
        mongo_uri: str,
        database: str = "orchestrator_ui",
        collection: str = "logs",
        connect_timeout_ms: int = 5000,
    ):
        """Initialize the MongoDB audit logger.

        Args:
            mongo_uri: MongoDB connection URI
            database: Database name
            collection: Collection name
            connect_timeout_ms: Connection timeout in milliseconds
        """
        self._mongo_uri = mongo_uri
        self._database_name = database
        self._collection_name = collection
        self._connect_timeout_ms = connect_timeout_ms
        self._client = None
        self._collection = None
        self._logger = logging.getLogger(f"{__name__}.MongoAuditLogger")

    async def connect(self) -> None:
        """Connect to MongoDB.

        Establishes connection and verifies it with a ping command.

        Raises:
            RuntimeError: If pymongo is not installed
            Exception: If connection fails
        """
        try:
            from pymongo import MongoClient
        except ImportError:
            raise RuntimeError(
                "pymongo is required for MongoAuditLogger. "
                "Install with: pip install pymongo"
            )

        self._client = MongoClient(
            self._mongo_uri,
            serverSelectionTimeoutMS=self._connect_timeout_ms,
        )

        # Test connection
        await asyncio.to_thread(self._client.admin.command, "ping")

        db = self._client[self._database_name]
        self._collection = db[self._collection_name]
        self._logger.info(
            "Connected to MongoDB: %s/%s",
            self._database_name, self._collection_name
        )

    async def close(self) -> None:
        """Close the MongoDB connection."""
        if self._client:
            self._client.close()
            self._client = None
            self._collection = None
            self._logger.info("Disconnected from MongoDB")

    async def log(self, entry: LogEntry) -> None:
        """Log an entry to MongoDB.

        Args:
            entry: The log entry to store

        Note:
            If MongoDB is unavailable, logs error and continues
            (does not raise exception to avoid disrupting the caller).
        """
        if self._collection is None:
            self._logger.warning("MongoDB not connected, skipping log entry")
            return

        try:
            doc = entry.to_dict()
            await asyncio.to_thread(self._collection.insert_one, doc)
        except Exception as e:
            self._logger.error("Failed to write log entry: %s", e)

    @property
    def is_connected(self) -> bool:
        """Check if connected to MongoDB."""
        return self._collection is not None


class NullAuditLogger(AuditLogger):
    """No-op audit logger for testing or when logging is disabled.

    All log operations are silently ignored.

    Example:
        logger = NullAuditLogger()
        await logger.log_event(...)  # Does nothing
    """

    async def connect(self) -> None:
        """No-op connect."""
        pass

    async def close(self) -> None:
        """No-op close."""
        pass

    async def log(self, entry: LogEntry) -> None:
        """No-op log."""
        pass
