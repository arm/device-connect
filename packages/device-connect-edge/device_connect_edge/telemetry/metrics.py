"""MetricsClient singleton for Device Connect.

Aligned with strands.telemetry.MetricsClient — singleton pattern
with lazy instrument creation.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from opentelemetry import metrics as metrics_api

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


_METER_NAME = "device_connect"
_METER_VERSION = "0.2.2"

# Singleton instance
_metrics_instance: Optional["MetricsClient"] = None


def get_metrics() -> "MetricsClient":
    """Get the singleton MetricsClient.

    Always returns a usable client — instruments are no-ops
    when OTel is not installed.
    """
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = MetricsClient()
    return _metrics_instance


class _NoOpInstrument:
    """No-op metric instrument for when OTel is not installed."""

    def add(self, amount: float = 1, attributes: Any = None) -> None:
        pass

    def record(self, amount: float = 0, attributes: Any = None) -> None:
        pass


class MetricsClient:
    """Singleton metrics client for Device Connect.

    Mirrors strands.telemetry.MetricsClient pattern.
    All instruments use the "device_connect." prefix.
    """

    def __init__(self) -> None:
        if _OTEL_AVAILABLE:
            meter = metrics_api.get_meter(_METER_NAME, _METER_VERSION)

            # RPC metrics
            self.rpc_duration = meter.create_histogram(
                name="device_connect.rpc.duration",
                description="Duration of RPC calls on device",
                unit="ms",
            )
            self.rpc_count = meter.create_counter(
                name="device_connect.rpc.count",
                description="Number of RPC calls processed",
                unit="1",
            )
            self.rpc_active = meter.create_up_down_counter(
                name="device_connect.rpc.active",
                description="Number of RPC calls currently in progress",
                unit="1",
            )

            # Event metrics
            self.event_count = meter.create_counter(
                name="device_connect.event.count",
                description="Number of events emitted",
                unit="1",
            )

            # Messaging metrics
            self.msg_publish_duration = meter.create_histogram(
                name="device_connect.messaging.publish.duration",
                description="Duration of messaging publish operations",
                unit="ms",
            )
            self.msg_request_duration = meter.create_histogram(
                name="device_connect.messaging.request.duration",
                description="Duration of messaging request/reply operations",
                unit="ms",
            )

            # State store metrics
            self.state_op_duration = meter.create_histogram(
                name="device_connect.state.operation.duration",
                description="Duration of state store operations",
                unit="ms",
            )

            # Device lifecycle metrics
            self.registration_count = meter.create_counter(
                name="device_connect.device.registration.count",
                description="Number of device registration attempts",
                unit="1",
            )
            self.heartbeat_count = meter.create_counter(
                name="device_connect.device.heartbeat.count",
                description="Number of heartbeats sent",
                unit="1",
            )

            # Orchestration metrics (for cmu-cloudlab DX)
            self.experiment_duration = meter.create_histogram(
                name="device_connect.experiment.duration",
                description="Duration of full experiment execution",
                unit="ms",
            )
            self.experiment_step_duration = meter.create_histogram(
                name="device_connect.experiment.step.duration",
                description="Duration of individual experiment steps",
                unit="ms",
            )
            self.retry_count = meter.create_counter(
                name="device_connect.experiment.retry.count",
                description="Number of retries in invoke_and_wait",
                unit="1",
            )
        else:
            # No-op instruments
            noop = _NoOpInstrument()
            self.rpc_duration = noop
            self.rpc_count = noop
            self.rpc_active = noop
            self.event_count = noop
            self.msg_publish_duration = noop
            self.msg_request_duration = noop
            self.state_op_duration = noop
            self.registration_count = noop
            self.heartbeat_count = noop
            self.experiment_duration = noop
            self.experiment_step_duration = noop
            self.retry_count = noop
