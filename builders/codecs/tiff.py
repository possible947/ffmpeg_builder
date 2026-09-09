"""libtiff component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libtiff(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libtiff via autotools."""
    env = builder.get_build_env(component)
    if type(builder.platform_strategy).__name__ == "LinuxGcc15Platform":
        env["CFLAGS"] = env.get("CFLAGS", "") + " -std=gnu11"

    configure_args = [
        arg.replace("{workspace}", builder._ws_str()) for arg in component.configure_args
    ]

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure"] + configure_args,
        "configure",
        source_dir,
        env,
    )

    builder._run_make(
        component,
        ComponentStatus.BUILDING,
        f"make -j{builder.num_jobs}",
        "Build failed",
        source_dir,
        builder.num_jobs,
        env,
    )

    builder._run_install(
        component,
        ComponentStatus.INSTALLING,
        "make install",
        "Install failed",
        source_dir,
        env,
    )
