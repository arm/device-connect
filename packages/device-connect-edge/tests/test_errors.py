# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.errors module."""

from device_connect_edge.errors import (
    DeviceConnectError,
    DeviceError,
    RegistrationError,
    FunctionInvocationError,
    ValidationError,
    CommissioningError,
)


class TestErrorHierarchy:
    def test_device_connect_error_is_exception(self):
        assert issubclass(DeviceConnectError, Exception)

    def test_device_error_inherits_device_connect(self):
        assert issubclass(DeviceError, DeviceConnectError)

    def test_registration_error(self):
        err = RegistrationError("Failed to register")
        assert str(err) == "Failed to register"
        assert isinstance(err, DeviceConnectError)

    def test_function_invocation_error(self):
        err = FunctionInvocationError("RPC failed")
        assert isinstance(err, DeviceConnectError)

    def test_validation_error(self):
        err = ValidationError("Bad params")
        assert isinstance(err, DeviceConnectError)

    def test_commissioning_error(self):
        err = CommissioningError("Commissioning failed")
        assert isinstance(err, DeviceConnectError)
