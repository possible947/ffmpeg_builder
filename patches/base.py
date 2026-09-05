"""Base types and assertion helpers for declarative source patching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from build_types import BuildError


@dataclass(frozen=True)
class SourcePatch:
    """Declarative source patch definition for one component."""

    name: str
    component_name: str
    target_rel_path: str
    apply_fn: Callable[[Path, object, object], None]
    condition: Optional[Callable[[object, object], bool]] = None


def assert_patch_present(component_name: str, path: Path, marker: str, context_desc: str) -> None:
    """Assert a marker is present after applying a patch."""
    text = path.read_text(encoding="utf-8")
    if marker not in text:
        raise BuildError(
            component_name,
            f"Source patch did not take effect in {path}: '{marker}' missing "
            f"({context_desc}). The component version may have changed.",
        )


def assert_patch_absent(component_name: str, path: Path, marker: str, context_desc: str) -> None:
    """Assert a marker is absent after applying a patch to revert a legacy code path."""
    text = path.read_text(encoding="utf-8")
    if marker in text:
        raise BuildError(
            component_name,
            f"Source patch did not take effect in {path}: '{marker}' still present "
            f"({context_desc}). The component version may have changed.",
        )
