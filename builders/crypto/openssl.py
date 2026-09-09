"""OpenSSL component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_openssl(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build OpenSSL."""
    env = builder.get_build_env(component)

    result, log_file = builder.executor.execute_with_log(
        [
            "./Configure",
            f"--prefix={builder._ws_str()}",
            f"--openssldir={builder._ws_str()}",
            "--libdir=lib",
            f"--with-zlib-include={builder._ws_str()}/include/",
            f"--with-zlib-lib={builder._ws_str()}/lib",
            "no-shared",
            "zlib",
        ],
        component.name,
        "configure",
        source_dir,
        env,
    )

    if not result.success:
        raise BuildError(component.name, "Configure failed", log_file)

    builder._run_make(
        component,
        ComponentStatus.BUILDING,
        f"make -j{builder.num_jobs}",
        "Build failed",
        source_dir,
        builder.num_jobs,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "make install_sw",
        "Install failed",
        ["make", "install_sw"],
        "install",
        source_dir,
        env,
    )
