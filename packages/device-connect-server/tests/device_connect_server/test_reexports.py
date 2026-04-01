"""Verify that device_connect_server.* re-exports resolve to the same objects as device_connect_edge.*."""

import device_connect_server
import device_connect_edge
from device_connect_server import drivers as server_drivers
from device_connect_edge import drivers as device_drivers
from device_connect_server import messaging as server_messaging
from device_connect_edge import messaging as device_messaging
from device_connect_server import telemetry as server_telemetry
from device_connect_edge import telemetry as device_telemetry


class TestTopLevelReExports:
    def test_device_runtime_is_same(self):
        assert device_connect_server.DeviceRuntime is device_connect_edge.DeviceRuntime

    def test_build_rpc_response(self):
        assert device_connect_server.build_rpc_response is device_connect_edge.build_rpc_response

    def test_build_rpc_error(self):
        assert device_connect_server.build_rpc_error is device_connect_edge.build_rpc_error

    def test_types(self):
        assert device_connect_server.DeviceState is device_connect_edge.DeviceState
        assert device_connect_server.DeviceCapabilities is device_connect_edge.DeviceCapabilities
        assert device_connect_server.DeviceIdentity is device_connect_edge.DeviceIdentity
        assert device_connect_server.DeviceStatus is device_connect_edge.DeviceStatus
        assert device_connect_server.FunctionDef is device_connect_edge.FunctionDef
        assert device_connect_server.EventDef is device_connect_edge.EventDef

    def test_errors(self):
        assert device_connect_server.DeviceConnectError is device_connect_edge.DeviceConnectError
        assert device_connect_server.DeviceError is device_connect_edge.DeviceError
        assert device_connect_server.RegistrationError is device_connect_edge.RegistrationError
        assert device_connect_server.FunctionInvocationError is device_connect_edge.FunctionInvocationError
        assert device_connect_server.ValidationError is device_connect_edge.ValidationError
        assert device_connect_server.CommissioningError is device_connect_edge.CommissioningError


class TestDriversReExports:
    def test_device_driver_is_same(self):
        assert server_drivers.DeviceDriver is device_drivers.DeviceDriver

    def test_decorators(self):
        assert server_drivers.rpc is device_drivers.rpc
        assert server_drivers.emit is device_drivers.emit
        assert server_drivers.before_emit is device_drivers.before_emit
        assert server_drivers.periodic is device_drivers.periodic
        assert server_drivers.on is device_drivers.on

    def test_schema_builders(self):
        assert server_drivers.build_function_schema is device_drivers.build_function_schema
        assert server_drivers.build_event_schema is device_drivers.build_event_schema

    def test_core_only_exports_exist(self):
        assert hasattr(server_drivers, "CapabilityLoader")
        assert hasattr(server_drivers, "CapabilityDriverMixin")


class TestMessagingReExports:
    def test_messaging_client_is_same(self):
        assert server_messaging.MessagingClient is device_messaging.MessagingClient

    def test_create_client_is_same(self):
        assert server_messaging.create_client is device_messaging.create_client

    def test_subscription_is_same(self):
        assert server_messaging.Subscription is device_messaging.Subscription

    def test_exceptions(self):
        assert server_messaging.MessagingError is device_messaging.MessagingError
        assert server_messaging.PublishError is device_messaging.PublishError
        assert server_messaging.SubscribeError is device_messaging.SubscribeError
        assert server_messaging.RequestTimeoutError is device_messaging.RequestTimeoutError


class TestTelemetryReExports:
    def test_device_connect_telemetry_is_same(self):
        assert server_telemetry.DeviceConnectTelemetry is device_telemetry.DeviceConnectTelemetry

    def test_helpers(self):
        assert server_telemetry.get_tracer is device_telemetry.get_tracer
        assert server_telemetry.get_metrics is device_telemetry.get_metrics
        assert server_telemetry.get_current_trace_id is device_telemetry.get_current_trace_id
        assert server_telemetry.is_enabled is device_telemetry.is_enabled
