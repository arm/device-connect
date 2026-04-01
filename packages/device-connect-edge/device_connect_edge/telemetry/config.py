"""DeviceConnectTelemetry — OpenTelemetry SDK setup for Device Connect.

Aligned with strands.telemetry.StrandsTelemetry:
- Fluent API for exporter configuration
- Detects and reuses an existing global TracerProvider (e.g. from Strands)
- Environment-variable-driven with sensible defaults
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Global state
_enabled = False
_initialized = False

try:
    from opentelemetry import trace as trace_api
    from opentelemetry import metrics as metrics_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    from opentelemetry.baggage.propagation import W3CBaggagePropagator

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


def is_enabled() -> bool:
    """Check if OpenTelemetry is active."""
    return _enabled


class DeviceConnectTelemetry:
    """Configure OpenTelemetry for Device Connect.

    Follows the same pattern as strands.telemetry.StrandsTelemetry:
    - Creates TracerProvider + MeterProvider with Device Connect resource attributes
    - If a global TracerProvider already exists (e.g. from Strands), reuses it
    - Fluent API for exporter setup

    Usage:
        telemetry = DeviceConnectTelemetry(service_name="device-connect-camera-001")
        telemetry.setup_otlp_exporter()

        # Or with chaining:
        DeviceConnectTelemetry().setup_otlp_exporter().setup_console_exporter()

    Args:
        service_name: OTel service name. Defaults to OTEL_SERVICE_NAME env var
            or "device-connect".
        device_id: Device Connect device ID (added as resource attribute).
        device_type: Device Connect device type (added as resource attribute).
        tenant: Device Connect tenant namespace (added as resource attribute).
        tracer_provider: Optional existing TracerProvider to use instead of
            creating a new one.
        meter_provider: Optional existing MeterProvider to use.
    """

    def __init__(
        self,
        service_name: Optional[str] = None,
        device_id: Optional[str] = None,
        device_type: Optional[str] = None,
        tenant: Optional[str] = None,
        tracer_provider: Optional[Any] = None,
        meter_provider: Optional[Any] = None,
    ):
        global _enabled, _initialized

        if not _OTEL_AVAILABLE:
            logger.debug("OpenTelemetry packages not installed — telemetry disabled")
            return

        if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
            logger.debug("OTEL_SDK_DISABLED=true — telemetry disabled")
            return

        # Build resource with Device Connect-specific attributes
        svc = service_name or os.getenv("OTEL_SERVICE_NAME", "device-connect")
        resource_attrs = {SERVICE_NAME: svc}
        if device_id:
            resource_attrs["device_connect.device.id"] = device_id
        if device_type:
            resource_attrs["device_connect.device.type"] = device_type
        if tenant:
            resource_attrs["device_connect.tenant"] = tenant

        self._resource = Resource.create(resource_attrs)

        # TracerProvider: reuse existing global if set (e.g. by Strands)
        if tracer_provider is not None:
            self._tracer_provider = tracer_provider
        else:
            current = trace_api.get_tracer_provider()
            # ProxyTracerProvider is the default (uninitialized) provider
            if isinstance(current, TracerProvider):
                # Already configured (e.g. by Strands) — reuse
                self._tracer_provider = current
                logger.debug("Reusing existing TracerProvider")
            else:
                self._tracer_provider = TracerProvider(resource=self._resource)
                trace_api.set_tracer_provider(self._tracer_provider)

        # MeterProvider
        if meter_provider is not None:
            self._meter_provider = meter_provider
        else:
            self._meter_provider = None  # Created on first exporter setup

        # Configure W3C propagators (TraceContext + Baggage)
        set_global_textmap(CompositePropagator([
            TraceContextTextMapPropagator(),
            W3CBaggagePropagator(),
        ]))

        _enabled = True
        _initialized = True
        logger.info("DeviceConnectTelemetry initialized (service=%s)", svc)

    def setup_otlp_exporter(
        self,
        buffer_dir: Optional[str] = None,
        max_buffer_mb: int = 100,
        **kwargs: Any,
    ) -> "DeviceConnectTelemetry":
        """Configure OTLP exporter with automatic file buffering.

        Uses OTEL_EXPORTER_OTLP_ENDPOINT env var for the endpoint.
        Wraps the OTLP exporter in FileBufferSpanExporter for offline
        resilience.

        Args:
            buffer_dir: Directory for disk-backed span buffer.
                Defaults to DEVICE_CONNECT_TELEMETRY_BUFFER_DIR env var or
                ~/.device-connect/telemetry-buffer/.
            max_buffer_mb: Maximum disk usage for buffer (default: 100 MB).
            **kwargs: Passed to OTLPSpanExporter.

        Returns:
            self (for chaining)
        """
        if not _OTEL_AVAILABLE or not _enabled:
            return self

        # Only configure OTLP when an endpoint is explicitly provided
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            logger.info(
                "OTEL_EXPORTER_OTLP_ENDPOINT not set — skipping OTLP exporter "
                "(set this env var to enable trace/metric export)"
            )
            return self

        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
        except ImportError:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
            except ImportError:
                logger.warning("No OTLP exporter package found — skipping OTLP setup")
                return self

        # Wrap with file buffer for connectivity resilience
        from device_connect_edge.telemetry.file_buffer_exporter import FileBufferSpanExporter

        otlp_exporter = OTLPSpanExporter(**kwargs)
        buffered_exporter = FileBufferSpanExporter(
            delegate=otlp_exporter,
            buffer_dir=buffer_dir or os.getenv(
                "DEVICE_CONNECT_TELEMETRY_BUFFER_DIR",
                os.path.expanduser("~/.device-connect/telemetry-buffer"),
            ),
            max_buffer_mb=max_buffer_mb,
        )

        # Tune batch processor for better resilience
        processor = BatchSpanProcessor(
            buffered_exporter,
            max_queue_size=int(os.getenv("OTEL_BSP_MAX_QUEUE_SIZE", "65536")),
            max_export_batch_size=int(os.getenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "512")),
            schedule_delay_millis=int(os.getenv("OTEL_BSP_SCHEDULE_DELAY", "5000")),
        )

        if isinstance(self._tracer_provider, TracerProvider):
            self._tracer_provider.add_span_processor(processor)

        # Metrics via OTLP
        metric_exporter = OTLPMetricExporter(**kwargs)
        reader = PeriodicExportingMetricReader(
            metric_exporter, export_interval_millis=60000
        )
        self._meter_provider = MeterProvider(
            resource=self._resource, metric_readers=[reader]
        )
        metrics_api.set_meter_provider(self._meter_provider)

        logger.info("OTLP exporter configured with file buffer")
        return self

    def setup_console_exporter(self) -> "DeviceConnectTelemetry":
        """Configure console exporter for debugging.

        Returns:
            self (for chaining)
        """
        if not _OTEL_AVAILABLE or not _enabled:
            return self

        processor = SimpleSpanProcessor(ConsoleSpanExporter())
        if isinstance(self._tracer_provider, TracerProvider):
            self._tracer_provider.add_span_processor(processor)

        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=60000
        )
        if self._meter_provider is None:
            self._meter_provider = MeterProvider(
                resource=self._resource, metric_readers=[reader]
            )
            metrics_api.set_meter_provider(self._meter_provider)

        logger.info("Console exporter configured")
        return self
