"""Shared builder exception types."""

from pathlib import Path
from typing import Optional

# Failure categories for project-managed inputs (see ``BuildError.category``).
CATEGORY_PROJECT_SOURCE = "project_source"
CATEGORY_TOOLCHAIN_INSTALL = "toolchain_install"


class BuildError(Exception):
    """Build error with component context.

    ``category`` and ``code`` are optional machine-readable classifiers (for
    example ``project_source`` / ``source_checksum_mismatch``) so callers can
    react to a failure kind without parsing the message.
    """

    def __init__(
        self,
        component: str,
        message: str,
        log_file: Optional[Path] = None,
        *,
        category: Optional[str] = None,
        code: Optional[str] = None,
    ):
        """Initialize build error."""
        super().__init__(f"{component}: {message}")
        self.component = component
        self.log_file = log_file
        self.category = category
        self.code = code


class SkipComponent(Exception):
    """Raised when a component should be skipped (not failed)."""

    def __init__(self, component: str, message: str):
        """Initialize skip exception."""
        super().__init__(f"{component}: {message}")
        self.component = component
        self.message = message
