"""Shared context and standard build-system entry points."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

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
    platform_strategy: Optional["BasePlatformStrategy"]
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


def build_autotools(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    context.builder._build_autotools(component, source_dir)


def build_cmake(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_cmake(component, source_dir)


def build_meson(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_meson(component, source_dir)


def build_make_only(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    context.builder._build_make_only(component, source_dir)


def build_cargo(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    context.builder._build_cargo(component, source_dir)


def install_headers_only(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    context.builder._install_headers_only(component, source_dir)


def dispatch_component_build(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    """Dispatch a component through custom or standard build entry points."""
    if component.custom_build_fn:
        from ..component_builders import get_custom_builder

        build_fn = get_custom_builder(component.custom_build_fn)
        if build_fn is not None:
            build_fn(context.builder, component, source_dir)
            return

    runners = {
        "autotools": build_autotools,
        "cmake": build_cmake,
        "meson": build_meson,
        "make_only": build_make_only,
        "cargo": build_cargo,
    }
    runner = runners.get(component.build_system.value)
    if runner is None:
        raise ValueError(f"Unknown build system: {component.build_system}")
    runner(context, component, source_dir)
