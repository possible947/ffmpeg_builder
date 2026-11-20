"""gettext runtime component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...components import BuildRole
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_gettext(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build and install only gettext's target libintl library and header."""
    build_dir = source_dir / component.workdir if component.workdir else source_dir
    environment = builder.get_build_env(component)
    cross = builder.platform_context.cross_environment
    cross_arguments: list[str] = []
    if cross is not None and component.build_role == BuildRole.TARGET_COMPONENT:
        environment.update(cross.autotools.environment)
        cross_arguments.extend(cross.autotools.arguments)

    configure_args = [
        argument.replace("{workspace}", builder._ws_str()).replace(
            "{num_jobs}", str(builder.num_jobs)
        )
        for argument in component.configure_args
    ]
    override = component.platform_overrides.get(builder.platform)
    if override is not None and override.configure_args_override is not None:
        configure_args = [
            argument.replace("{workspace}", builder._ws_str()).replace(
                "{num_jobs}", str(builder.num_jobs)
            )
            for argument in override.configure_args_override
        ]

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure", *cross_arguments, *configure_args],
        "configure",
        build_dir,
        environment,
    )
    intl_dir = build_dir / "intl"
    builder._run_make(
        component,
        ComponentStatus.BUILDING,
        f"make -j{builder.num_jobs}",
        "Build failed",
        intl_dir,
        builder.num_jobs,
        environment,
    )
    builder._run_install(
        component,
        ComponentStatus.INSTALLING,
        "make install",
        "Install failed",
        intl_dir,
        environment,
    )
