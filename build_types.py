"""Shared builder exception types."""

from pathlib import Path
from typing import Optional


class BuildError(Exception):
    """Build error with component context."""

    def __init__(self, component: str, message: str, log_file: Optional[Path] = None):
        """Initialize build error."""
        super().__init__(f"{component}: {message}")
        self.component = component
        self.log_file = log_file


class SkipComponent(Exception):
    """Raised when a component should be skipped (not failed)."""

    def __init__(self, component: str, message: str):
        """Initialize skip exception."""
        super().__init__(f"{component}: {message}")
        self.component = component
        self.message = message
