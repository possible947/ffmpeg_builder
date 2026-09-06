"""glslang component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_glslang(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build glslang."""
    env = builder.get_build_env(component)

    result, log_file = builder.executor.execute_with_log(
        ["./update_glslang_sources.py"],
        component.name,
        "update-sources",
        source_dir,
        env,
    )

    if not result.success:
        raise BuildError(component.name, "Update sources failed", log_file)

    cmake_args = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DENABLE_SHARED=OFF",
        "-DBUILD_SHARED_LIBS=OFF",
        f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
    ]

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "cmake .",
        "Configure failed",
        ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
        "configure",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "cmake --build",
        "Build failed",
        ["cmake", "--build", ".", "--parallel", str(builder.num_jobs)],
        "build",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "cmake --install",
        "Install failed",
        ["cmake", "--install", "."],
        "install",
        source_dir,
        env,
    )
