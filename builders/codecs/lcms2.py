"""Little CMS target-library builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_lcms2(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build and install only the library, headers, and pkg-config metadata."""
    env = builder.get_build_env(component)
    cross = builder.platform_context.cross_environment
    cross_arguments: list[str] = []
    if cross is not None and component.build_role.value == "target":
        env.update(cross.autotools.environment)
        cross_arguments.extend(cross.autotools.arguments)

    configure_args = [
        arg.replace("{workspace}", builder._ws_str()) for arg in component.configure_args
    ]
    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure", *cross_arguments, *configure_args],
        "configure",
        source_dir,
        env,
    )

    builder._run_make(
        component,
        ComponentStatus.BUILDING,
        "make library",
        "Library build failed",
        source_dir / "src",
        builder.num_jobs,
        env,
    )
    builder._run_install(
        component,
        ComponentStatus.INSTALLING,
        "Install library failed",
        "Library install failed",
        source_dir / "src",
        env,
    )
    builder._run_install(
        component,
        ComponentStatus.INSTALLING,
        "Install headers failed",
        "Header install failed",
        source_dir / "include",
        env,
    )
    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "Install pkg-config metadata failed",
        "pkg-config metadata install failed",
        ["make", "install-pkgconfigDATA"],
        "install pkg-config metadata",
        source_dir,
        env,
    )
