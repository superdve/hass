"""Exceptions for the Ledvance/Tuya integration."""


class InvalidUserSession(Exception):
    """Raised when the Tuya session ID has expired."""


class InvalidAuthentication(Exception):
    """Raised when username or password is incorrect."""


class CannotConnect(Exception):
    """Raised when the API endpoint cannot be reached."""


class TooManyRequests(Exception):
    """Raised when the Tuya API rate-limits the request."""


class UnsupportedAPI(ValueError):
    """The OEM cloud does not expose this optional API/version."""
