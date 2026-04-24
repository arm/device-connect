# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""OpenTelemetry integration for Device Connect.

Provides distributed tracing, metrics, and context propagation
for the Device Connect device framework. Aligned with the Strands Agents
SDK telemetry patterns (strands.telemetry).

All OpenTelemetry imports are guarded — when the opentelemetry packages
are not installed, every public function returns a no-op. Device code
works identically with or without OTel.

Quick start:
    # In DeviceRuntime.run() — called automatically:
    from device_connect_edge.telemetry import DeviceConnectTelemetry
    DeviceConnectTelemetry().setup_otlp_exporter()

    # Manual setup with chaining (like StrandsTelemetry):
    from device_connect_edge.telemetry import DeviceConnectTelemetry
    telemetry = DeviceConnectTelemetry(service_name="my-device")
    telemetry.setup_otlp_exporter().setup_console_exporter()

    # Accessing tracer/meter:
    from device_connect_edge.telemetry import get_tracer, get_metrics
    tracer = get_tracer()
    metrics = get_metrics()

Environment variables:
    OTEL_EXPORTER_OTLP_ENDPOINT: Collector endpoint (default: http://localhost:4317)
    OTEL_SERVICE_NAME: Override service name
    OTEL_SDK_DISABLED: Set to "true" to disable telemetry
    OTEL_TRACES_EXPORTER: "otlp", "console", or "none"
    OTEL_METRICS_EXPORTER: "otlp", "console", or "none"
    DEVICE_CONNECT_TELEMETRY_BUFFER_DIR: Disk buffer for offline resilience
    DEVICE_CONNECT_TELEMETRY_BUFFER_MAX_MB: Max buffer disk usage (default: 100)
"""

from device_connect_edge.telemetry.config import DeviceConnectTelemetry, is_enabled
from device_connect_edge.telemetry.tracer import get_tracer, get_current_trace_id
from device_connect_edge.telemetry.metrics import get_metrics

__all__ = [
    "DeviceConnectTelemetry",
    "is_enabled",
    "get_tracer",
    "get_current_trace_id",
    "get_metrics",
]
