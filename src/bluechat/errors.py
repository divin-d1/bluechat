"""Typed errors translated into short, safe CLI messages."""


class BlueChatError(Exception):
    """Base class for expected application errors."""


class ConfigurationError(BlueChatError):
    """A configuration value is invalid or cannot be stored."""


class BluetoothUnavailableError(BlueChatError):
    """No usable Bluetooth adapter/backend is available."""


class BluetoothPermissionError(BluetoothUnavailableError):
    """The operating system denied BlueChat access to Bluetooth."""


class BluetoothCapabilityError(BluetoothUnavailableError):
    """The selected adapter exists but does not support a requested operation."""


class AuthenticationError(BlueChatError):
    """A room authentication attempt failed."""


class RoomExpiredError(AuthenticationError):
    """The room code expired or was replaced."""


class AuthenticationRateLimitedError(AuthenticationError):
    """The host temporarily paused attempts after repeated code failures."""


class RoomFullError(BlueChatError):
    """The room reached its participant limit."""


class ApprovalRejectedError(BlueChatError):
    """The host declined the join request."""


class ProtocolError(BlueChatError):
    """An invalid, oversized, or incompatible protocol packet was received."""


class SecurityError(BlueChatError):
    """Secure channel setup or authenticated decryption failed."""


class TransferError(BlueChatError):
    """A file offer, transfer, validation, or storage operation failed."""


def is_bluetooth_permission_error(error: BaseException) -> bool:
    """Recognize common OS permission failures without parsing human output."""
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    if any(part in name for part in ("permission", "accessdenied", "unauthorized")):
        return True
    return any(part in message for part in ("permission denied", "access denied", "not authorized"))
