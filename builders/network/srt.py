"""SRT component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_srt(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build srt."""
    env = builder.get_build_env(component)
    env["OPENSSL_ROOT_DIR"] = builder._ws_str()
    env["OPENSSL_LIB_DIR"] = f"{builder._ws_str()}/lib"
    env["OPENSSL_INCLUDE_DIR"] = f"{builder._ws_str()}/include"

    cmake_args = [
        f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_INSTALL_BINDIR=bin",
        "-DCMAKE_INSTALL_INCLUDEDIR=include",
        "-DENABLE_SHARED=OFF",
        "-DENABLE_STATIC=ON",
        "-DENABLE_APPS=OFF",
        "-DUSE_STATIC_LIBSTDCXX=ON",
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

    if builder.config.full_static and builder.platform == "linux":
        srt_pc = builder.workspace / "lib" / "pkgconfig" / "srt.pc"
        if srt_pc.exists():
            content = srt_pc.read_text()
            content = content.replace("-lgcc_s", "-lgcc_eh")
            srt_pc.write_text(content)
