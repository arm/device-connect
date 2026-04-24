# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Exception hierarchy for Device Connect.

This module defines the exception classes used throughout the Device Connect
framework. All exceptions inherit from DeviceConnectError, making it
easy to catch Device Connect-specific errors.

Example:
    try:
        await device.run()
    except RegistrationError as e:
        logger.error(f"Failed to register: {e}")
    except DeviceConnectError as e:
        logger.error(f"Device Connect error: {e}")
"""
from __future__ import annotations


class DeviceConnectError(Exception):
    """Base exception for all Device Connect errors.

    All custom exceptions in the Device Connect framework inherit from this class,
    allowing callers to catch any Device Connect-related error with a single handler.
    """
    pass



class DeviceError(DeviceConnectError):
    """Error related to device operations.

    Raised when a device encounters an operational error, such as
    hardware failures, initialization problems, or lifecycle issues.
    """
    pass


class RegistrationError(DeviceConnectError):
    """Error during device registration.

    Raised when a device fails to register with the device registry,
    including connection failures, authentication issues, or lease
    creation problems.
    """
    pass


class FunctionInvocationError(DeviceConnectError):
    """Error during function invocation.

    Raised when a device function call fails, either due to
    validation errors, handler exceptions, or communication issues.

    Attributes:
        function_name: Name of the function that failed
        original_error: The underlying exception, if any
    """

    def __init__(
        self,
        message: str,
        function_name: str | None = None,
        original_error: Exception | None = None
    ):
        super().__init__(message)
        self.function_name = function_name
        self.original_error = original_error


class ValidationError(DeviceConnectError):
    """Error during parameter or data validation.

    Raised when input data fails validation, such as invalid
    function parameters, malformed messages, or schema violations.

    Attributes:
        field: The field that failed validation, if applicable
        errors: List of validation error details
    """

    def __init__(
        self,
        message: str,
        field: str | None = None,
        errors: list | None = None
    ):
        super().__init__(message)
        self.field = field
        self.errors = errors or []


class DeviceConnectionError(DeviceConnectError):
    """Error establishing or maintaining connection.

    Raised when the device cannot connect to the messaging backend
    or loses connection unexpectedly.
    """
    pass


class CommissioningError(DeviceConnectError):
    """Error during device commissioning.

    Raised when the commissioning process fails, such as invalid PIN,
    rate limiting, or credential generation failures.
    """
    pass


class DeviceDependencyError(DeviceConnectError):
    """Raised when a required peer device is not available within the timeout.

    Attributes:
        device_type: The device type that was not found.
        timeout: The timeout that expired.
    """

    def __init__(self, message: str, device_type: str = "", timeout: float = 0.0):
        super().__init__(message)
        self.device_type = device_type
        self.timeout = timeout
