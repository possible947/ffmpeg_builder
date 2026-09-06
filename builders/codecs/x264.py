"""x264 component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_x264(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build x264."""
    env = builder.get_build_env(component)

    configure_args = [
        f"--prefix={builder._ws_str()}",
        "--enable-static",
        "--enable-pic",
        "--disable-cli",
    ]

    if builder.platform == "linux":
        env["CXXFLAGS"] = f"-fPIC {env.get('CXXFLAGS', '')}"

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

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "make install-lib-static",
        "Install lib-static failed",
        ["make", "install-lib-static"],
        "install-lib",
        source_dir,
        env,
    )
