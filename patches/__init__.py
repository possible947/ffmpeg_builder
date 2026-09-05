"""Declarative source patch registry for versioned component fixes."""

from .base import SourcePatch, assert_patch_absent, assert_patch_present
from .registry import PatchRegistry, get_patch_registry

__all__ = [
    "SourcePatch",
    "PatchRegistry",
    "get_patch_registry",
    "assert_patch_present",
    "assert_patch_absent",
]
