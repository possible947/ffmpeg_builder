"""x264 component builder."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
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

    cross = builder.platform_context.cross_environment
    if cross is not None:
        nasm = shutil.which("nasm", path=env.get("PATH"))
        if not nasm:
            raise BuildError(component.name, "Required host NASM is unavailable.")
        env["AS"] = nasm
        env["PKGCONFIG"] = str(cross.pkg_config_wrapper)
        configure_args += [
            "--host=x86_64-w64-mingw32",
            "--disable-opencl",
            "--disable-bashcompletion",
        ]
    elif builder.platform == "linux":
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
