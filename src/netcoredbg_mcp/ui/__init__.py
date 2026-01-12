"""UI Automation module for netcoredbg-mcp."""

from .automation import UIAutomation
from .errors import (
    ApplicationNotRespondingError,
    ElementNotFoundError,
    NoActiveSessionError,
    NoProcessIdError,
    UIAutomationError,
    UIOperationTimeoutError,
)
from .serialization import ElementInfo

__all__ = [
    "ApplicationNotRespondingError",
    "ElementInfo",
    "ElementNotFoundError",
    "NoActiveSessionError",
    "NoProcessIdError",
    "UIAutomation",
    "UIAutomationError",
    "UIOperationTimeoutError",
]
