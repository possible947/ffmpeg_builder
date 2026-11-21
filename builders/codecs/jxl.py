"""libjxl component builder."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus
from .jxl_sources import prepare_libjxl_sources

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


_LIBRARIES = (
    "libjxl.a",
    "libjxl_cms.a",
    "libjxl_threads.a",
    "libhwy.a",
    "libbrotlicommon.a",
    "libbrotlidec.a",
    "libbrotlienc.a",
)


def normalize_cross_metadata(prefix: Path) -> bool:
    """Supply static C++ runtime requirements omitted by upstream metadata."""
    changed = False
    for package, flags in (
        ("libjxl", ("-lunwind",)),
        ("libjxl_threads", ("-lc++", "-lunwind")),
    ):
        path = prefix / "lib" / "pkgconfig" / f"{package}.pc"
        lines = path.read_text(encoding="utf-8").splitlines()
        matches = [index for index, line in enumerate(lines) if line.startswith("Libs.private:")]
        if len(matches) != 1:
            raise BuildError("libjxl", f"Unexpected pkg-config metadata: {path}")
        index = matches[0]
        tokens = lines[index].split()[1:]
        for flag in flags:
            if flag not in tokens:
                tokens.append(flag)
        updated = "Libs.private: " + " ".join(tokens)
        if lines[index] != updated:
            lines[index] = updated
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            changed = True
    return changed


def _build_cross(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    cross = builder.platform_context.cross_environment
    assert cross is not None
    prepare_libjxl_sources(builder, source_dir)
    env = builder.get_build_env(component)
    build_dir = source_dir / "build"
    build_dir.mkdir()
    options = [
        "-DBUILD_SHARED_LIBS=OFF",
        "-DBUILD_TESTING=OFF",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={builder.target_prefix}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        f"-DCMAKE_TOOLCHAIN_FILE={cross.cmake_toolchain_file}",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER",
        "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY",
        "-DJPEGXL_ENABLE_BENCHMARK=OFF",
        "-DJPEGXL_ENABLE_DOXYGEN=OFF",
        "-DJPEGXL_ENABLE_MANPAGES=OFF",
        "-DJPEGXL_ENABLE_TOOLS=OFF",
        "-DJPEGXL_ENABLE_EXAMPLES=OFF",
        "-DJPEGXL_ENABLE_DEVTOOLS=OFF",
        "-DJPEGXL_ENABLE_FUZZERS=OFF",
        "-DJPEGXL_ENABLE_VIEWERS=OFF",
        "-DJPEGXL_ENABLE_PLUGINS=OFF",
        "-DJPEGXL_ENABLE_JNI=OFF",
        "-DJPEGXL_ENABLE_SJPEG=OFF",
        "-DJPEGXL_ENABLE_OPENEXR=OFF",
        "-DJPEGXL_ENABLE_SKCMS=OFF",
        "-DJPEGXL_FORCE_SYSTEM_LCMS2=ON",
    ]
    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "cmake target configure",
        "Configure failed",
        ["cmake", "-G", "Ninja", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", *options, str(source_dir)],
        "configure",
        build_dir,
        env,
    )
    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "cmake target build",
        "Build failed",
        ["cmake", "--build", ".", "--parallel", str(builder.num_jobs)],
        "build",
        build_dir,
        env,
    )
    with tempfile.TemporaryDirectory(
        prefix="libjxl-install-", dir=cross.paths.generated_files_root
    ) as temporary:
        stage = Path(temporary)
        install_env = dict(env, DESTDIR=str(stage))
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake staged install",
            "Install failed",
            ["cmake", "--install", "."],
            "install",
            build_dir,
            install_env,
        )
        staged_prefix = stage / builder.target_prefix.relative_to("/")
        normalize_cross_metadata(staged_prefix)
        _promote_cross_outputs(component, staged_prefix, builder.target_prefix, builder)


def _promote_cross_outputs(
    component: Component, staged: Path, prefix: Path, builder: FFmpegBuilder
) -> None:
    roots = [staged / "include" / name for name in ("jxl", "brotli", "hwy")] + [
        staged / "lib" / name for name in _LIBRARIES
    ]
    pkg = staged / "lib" / "pkgconfig"
    roots.extend(
        pkg / f"{name}.pc"
        for name in (
            "libjxl",
            "libjxl_cms",
            "libjxl_threads",
            "libhwy",
            "libbrotlicommon",
            "libbrotlidec",
            "libbrotlienc",
        )
    )
    cmake = staged / "lib" / "cmake"
    if cmake.is_dir():
        roots.extend(
            cmake / name for name in ("hwy", "libjxl", "brotli") if (cmake / name).is_dir()
        )
    files = []
    for root in roots:
        if not root.exists():
            raise BuildError(component.name, f"Missing staged target output: {root}")
        files.extend(
            sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else [root]
        )
    for path in files:
        if path.is_symlink():
            raise BuildError(component.name, f"Unexpected staged symlink: {path}")
        destination = prefix / path.relative_to(staged)
        if destination.exists() or destination.is_symlink():
            raise BuildError(component.name, f"Refusing to overwrite target output: {destination}")
        if not destination.resolve().is_relative_to(prefix.resolve()):
            raise BuildError(component.name, f"Target output escapes prefix: {destination}")
    for path in files:
        destination = prefix / path.relative_to(staged)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source, destination.open("xb") as target:
            created = getattr(builder, "_wave5c_created_outputs", None)
            if created is not None:
                created.append(destination)
            shutil.copyfileobj(source, target)
        shutil.copystat(path, destination)


def build_libjxl(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libjxl."""
    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        _build_cross(builder, component, source_dir)
        return
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
