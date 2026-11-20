"""libvorbis component builder."""

from __future__ import annotations

from dataclasses import replace
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...cross_environment import validate_pkg_config_output
from ...state import ComponentStatus
from ..base import ComponentBuildContext, build_autotools

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libvorbis(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvorbis."""
    if builder.platform_context.cross_environment is not None:
        _require_target_packages(builder, component, ("ogg",))
        _restrict_subdirectories(
            component,
            source_dir,
            "SUBDIRS = m4 include vq lib test doc $(am__append_1)",
            "SUBDIRS = m4 include lib $(am__append_1)",
        )
        target_component = replace(
            component,
            configure_args=[
                "--prefix={workspace}",
                "--enable-static",
                "--disable-shared",
                "--disable-oggtest",
                "--disable-examples",
                "--disable-docs",
            ],
        )
        build_autotools(
            ComponentBuildContext.from_builder(builder),
            target_component,
            source_dir,
            unset_environment=_DEPENDENCY_ENVIRONMENT,
        )
        return
    env = builder.get_build_env(component)

    # The -force_cpusubtype_ALL cleanup is applied by the patch registry
    # (patches/darwin_patches.py) after extraction.
    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./autogen.sh",
        "Autogen failed",
        ["./autogen.sh", f"--prefix={builder._ws_str()}"],
        "autogen",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        [
            "./configure",
            f"--prefix={builder._ws_str()}",
            f"--with-ogg-libraries={builder._ws_str()}/lib",
            f"--with-ogg-includes={builder._ws_str()}/include/",
            "--enable-static",
            "--disable-shared",
            "--disable-oggtest",
        ],
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


def build_libtheora(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Use the shared Autotools path after validating target dependencies."""
    if builder.platform_context.cross_environment is not None:
        _require_target_packages(builder, component, ("ogg", "vorbis"))
        _restrict_subdirectories(
            component,
            source_dir,
            "SUBDIRS = lib include doc tests m4 $(EXAMPLES_DIR)",
            "SUBDIRS = lib include m4 $(EXAMPLES_DIR)",
        )
        component = replace(component, configure_args=[*component.configure_args, "--disable-doc"])
        build_autotools(
            ComponentBuildContext.from_builder(builder),
            component,
            source_dir,
            unset_environment=_DEPENDENCY_ENVIRONMENT,
        )
    else:
        build_autotools(ComponentBuildContext.from_builder(builder), component, source_dir)


_DEPENDENCY_ENVIRONMENT = tuple(
    f"{prefix}{package}_{suffix}"
    for prefix in ("", "pkg_cv_")
    for package in ("OGG", "VORBIS", "SDL", "PNG", "TIFF")
    for suffix in ("CFLAGS", "LIBS")
)


def _restrict_subdirectories(
    component: Component, source_dir: Path, original: str, replacement: str
) -> None:
    makefile = source_dir / "Makefile.in"
    try:
        content = makefile.read_text(encoding="utf-8")
        if content.count(original) != 1:
            raise BuildError(component.name, f"Unexpected pinned source layout in {makefile}.")
        makefile.write_text(content.replace(original, replacement, 1), encoding="utf-8")
    except OSError as exc:
        raise BuildError(component.name, f"Cannot adapt {makefile}: {exc}") from exc


def _require_target_packages(
    builder: FFmpegBuilder, component: Component, packages: tuple[str, ...]
) -> None:
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError(
            component.name, "Target dependency preflight requires a cross environment."
        )
    prefix = cross.paths.target_prefix.resolve()
    env = dict(cross.autotools.environment)
    for package in packages:
        expected = {
            "ogg": ("include/ogg/ogg.h", "lib/libogg.a"),
            "vorbis": ("include/vorbis/codec.h", "lib/libvorbis.a"),
        }
        for relative in expected[package]:
            path = prefix / relative
            if not path.is_file() or not path.resolve().is_relative_to(prefix):
                raise BuildError(
                    component.name,
                    f"Required target pkg-config package {package!r} is missing: {path}",
                )
        for args in (
            ["--exists", package],
            ["--variable=prefix", package],
            ["--static", "--cflags", "--libs", package],
        ):
            result = subprocess.run(
                [str(cross.pkg_config_wrapper), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if result.returncode or (
                args[0] == "--variable=prefix" and Path(result.stdout.strip()).resolve() != prefix
            ):
                raise BuildError(
                    component.name, f"Required target pkg-config package {package!r} is missing."
                )
            if args[0] == "--static":
                validate_pkg_config_output(result.stdout, (prefix,))
