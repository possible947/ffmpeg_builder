"""libjxl component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libjxl(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libjxl."""
    env = builder.get_build_env(component)

    # The deps.sh realpath guard (macOS without coreutils) is applied by
    # the patch registry (patches/darwin_patches.py) after extraction.
    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./deps.sh",
        "Deps failed",
        ["./deps.sh"],
        "deps",
        source_dir,
        env,
    )

    cmake_args = [
        "-DBUILD_SHARED_LIBS=OFF",
        f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_INSTALL_BINDIR=bin",
        "-DCMAKE_INSTALL_INCLUDEDIR=include",
        "-DENABLE_SHARED=off",
        "-DENABLE_STATIC=ON",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DJPEGXL_ENABLE_BENCHMARK=OFF",
        "-DJPEGXL_ENABLE_DOXYGEN=OFF",
        "-DJPEGXL_ENABLE_MANPAGES=OFF",
        "-DJPEGXL_ENABLE_TOOLS=OFF",
        "-DJPEGXL_ENABLE_EXAMPLES=OFF",
        "-DJPEGXL_ENABLE_JPEGLI_LIBJPEG=OFF",
        "-DJPEGXL_ENABLE_JPEGLI=ON",
        "-DJPEGXL_TEST_TOOLS=OFF",
        "-DJPEGXL_ENABLE_JNI=OFF",
        "-DBUILD_TESTING=OFF",
        "-DJPEGXL_ENABLE_SKCMS=OFF",
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
        f"cmake --build . --parallel {builder.num_jobs}",
        "Build failed",
        ["cmake", "--build", ".", f"--parallel={builder.num_jobs}"],
        "build",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "cmake --install .",
        "Install failed",
        ["cmake", "--install", "."],
        "install",
        source_dir,
        env,
    )
