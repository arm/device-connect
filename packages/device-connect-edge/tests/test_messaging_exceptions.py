# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for device_connect_edge.messaging.exceptions module."""

from device_connect_edge.messaging.exceptions import (
    MessagingError,
    MessagingConnectionError,
    PublishError,
    SubscribeError,
    RequestTimeoutError,
    AuthenticationError,
    NotConnectedError,
)


class TestMessagingExceptionHierarchy:
    """All messaging exceptions inherit from MessagingError."""

    def test_messaging_error_is_exception(self):
        assert issubclass(MessagingError, Exception)

    def test_connection_error(self):
        err = MessagingConnectionError("cannot connect")
        assert isinstance(err, MessagingError)

    def test_publish_error(self):
        err = PublishError("publish failed")
        assert isinstance(err, MessagingError)

    def test_subscribe_error(self):
        err = SubscribeError("subscribe failed")
        assert isinstance(err, MessagingError)

    def test_request_timeout_error(self):
        err = RequestTimeoutError("timed out")
        assert isinstance(err, MessagingError)

    def test_authentication_error(self):
        err = AuthenticationError("bad creds")
        assert isinstance(err, MessagingError)

    def test_not_connected_error(self):
        err = NotConnectedError("not connected")
        assert isinstance(err, MessagingError)

    def test_catch_all(self):
        for cls in (MessagingConnectionError, PublishError, SubscribeError,
                    RequestTimeoutError, AuthenticationError, NotConnectedError):
            try:
                raise cls("test")
            except MessagingError:
                pass
