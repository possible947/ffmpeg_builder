"""Resolver API for selecting a platform strategy.

Concrete platform implementations are intentionally introduced in later phases.
This stub preserves the contract expected by the architecture plan while
leaving the actual selection logic to the future platform-specific modules.
"""

from __future__ import annotations

from typing import Optional

from .base import BasePlatformStrategy
from .context import PlatformContext


class PlatformStrategyResolver:
    """Placeholder resolver for platform strategy selection."""

    def resolve(self, context: PlatformContext) -> Optional[BasePlatformStrategy]:
        """Resolve the correct platform strategy for the current context."""
        raise NotImplementedError(
            "Concrete platform strategy selection is implemented in later phases."
        )


def resolve_platform_strategy(context: PlatformContext) -> Optional[BasePlatformStrategy]:
    """Convenience function for platform strategy resolution."""
    return PlatformStrategyResolver().resolve(context)
