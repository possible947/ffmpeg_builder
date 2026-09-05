"""Shared context and standard build-system entry points."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..builder import FFmpegBuilder
    from ..components import Component
    from ..executor import CommandExecutor
    from ..platforms.base import BasePlatformStrategy
    from ..state import StateManager


@dataclass(frozen=True)
class ComponentBuildContext:
    """Dependencies exposed to modular component builders."""

    builder: "FFmpegBuilder"
    platform_strategy: "BasePlatformStrategy"
    executor: "CommandExecutor"
    state_manager: "StateManager"

    @classmethod
    def from_builder(cls, builder: "FFmpegBuilder") -> "ComponentBuildContext":
        return cls(
            builder=builder,
            platform_strategy=builder.platform_strategy,
            executor=builder.executor,
            state_manager=builder.state_manager,
        )


def build_autotools(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_autotools(component, source_dir)


def build_cmake(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_cmake(component, source_dir)


def build_meson(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_meson(component, source_dir)


def build_make_only(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_make_only(component, source_dir)


def build_cargo(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_cargo(component, source_dir)


def install_headers_only(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    context.builder._install_headers_only(component, source_dir)
