"""
Custom exceptions for the messaging abstraction layer.
"""


class MessagingError(Exception):
    """Base exception for all messaging-related errors."""
    pass


class MessagingConnectionError(MessagingError):
    """Raised when connection to messaging broker fails."""
    pass


class PublishError(MessagingError):
    """Raised when publishing a message fails."""
    pass


class SubscribeError(MessagingError):
    """Raised when subscribing to a topic/subject fails."""
    pass


class RequestTimeoutError(MessagingError):
    """Raised when a request times out waiting for a reply."""
    pass


class AuthenticationError(MessagingError):
    """Raised when authentication with broker fails."""
    pass


class NotConnectedError(MessagingError):
    """Raised when attempting operations while not connected."""
    pass
