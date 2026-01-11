"""UI Automation specific exceptions."""


class UIAutomationError(Exception):
    """Base exception for UI automation errors."""

    pass


class NoActiveSessionError(UIAutomationError):
    """Raised when no debug session is active."""

    pass


class NoProcessIdError(UIAutomationError):
    """Raised when process ID is not available."""

    pass


class ElementNotFoundError(UIAutomationError):
    """Raised when UI element cannot be found."""

    pass


class ApplicationNotRespondingError(UIAutomationError):
    """Raised when application is not responding."""

    pass


class UIOperationTimeoutError(UIAutomationError):
    """Raised when UI operation times out."""

    pass
