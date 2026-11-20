"""libvpx component builder."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libvpx(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvpx."""
    env = builder.get_build_env(component)

    if builder.platform == "darwin":
        makefile = source_dir / "build" / "make" / "Makefile"
        if makefile.exists():
            content = makefile.read_text()
            content = content.replace(",--version-script", "")
            content = content.replace(
                "-Wl,--no-undefined -Wl,-soname", "-Wl,-undefined,error -Wl,-install_name"
            )
            makefile.write_text(content)
            builder._assert_patch_absent(
                component, makefile, ",--version-script", "libvpx darwin Makefile link flags"
            )
            builder._assert_patch_absent(
                component,
                makefile,
                "-Wl,--no-undefined -Wl,-soname",
                "libvpx darwin Makefile link flags",
            )

    configure_args = [
        f"--prefix={builder._ws_str()}",
        "--disable-unit-tests",
        "--disable-shared",
        "--disable-examples",
        "--enable-vp9-highbitdepth",
    ]
    cross = builder.platform_context.cross_environment
    if cross is not None:
        if not shutil.which("nasm", path=env.get("PATH")):
            raise BuildError(component.name, "Required host NASM is unavailable.")
        configure_args += [
            "--target=x86_64-win64-gcc",
            "--as=nasm",
            "--disable-tools",
            "--disable-docs",
        ]
        with tempfile.TemporaryDirectory(
            prefix="libvpx-probes-", dir=cross.paths.generated_files_root
        ) as directory:
            env["TMPDIR"] = directory
            _run_libvpx(builder, component, source_dir, env, configure_args)
        return
    else:
        configure_args.append("--as=yasm")

    _run_libvpx(builder, component, source_dir, env, configure_args)


def _run_libvpx(
    builder: FFmpegBuilder,
    component: Component,
    source_dir: Path,
    env: dict[str, str],
    configure_args: list[str],
) -> None:
    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure", *configure_args],
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
