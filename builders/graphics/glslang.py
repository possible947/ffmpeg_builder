"""glslang component builder."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus
from .cross_install import promote_staged
from .glslang_sources import prepare_glslang_sources

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_glslang(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build glslang."""
    env = builder.get_build_env(component)
    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        prepare_glslang_sources(builder, source_dir)
        spirv_cmake = source_dir / "External/spirv-tools/source/CMakeLists.txt"
        text = spirv_cmake.read_text(encoding="utf-8")
        replacements = {
            "add_library(${SPIRV_TOOLS}-shared SHARED ${SPIRV_SOURCES})": "add_library(${SPIRV_TOOLS}-shared SHARED EXCLUDE_FROM_ALL ${SPIRV_SOURCES})",
            "set(SPIRV_TOOLS_TARGETS ${SPIRV_TOOLS} ${SPIRV_TOOLS}-shared)": "set(SPIRV_TOOLS_TARGETS ${SPIRV_TOOLS})",
        }
        for original, replacement in replacements.items():
            if text.count(original) != 1:
                raise BuildError(component.name, "Unexpected SPIRV-Tools CMake structure.")
            text = text.replace(original, replacement)
        spirv_cmake.write_text(text, encoding="utf-8")
        build_dir = source_dir / "build"
        build_dir.mkdir()
        args = [
            "cmake",
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DENABLE_SHARED=OFF",
            "-DGLSLANG_TESTS=OFF",
            "-DENABLE_GLSLANG_BINARIES=OFF",
            "-DSPIRV_SKIP_EXECUTABLES=ON",
            "-DSPIRV_TOOLS_BUILD_STATIC=OFF",
            "-DENABLE_OPT=ON",
            "-DENABLE_PCH=OFF",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            f"-DCMAKE_INSTALL_PREFIX={builder.target_prefix}",
            f"-DCMAKE_TOOLCHAIN_FILE={cross.cmake_toolchain_file}",
            str(source_dir),
        ]
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "cmake target configure",
            "Configure failed",
            args,
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
            prefix="glslang-install-", dir=cross.paths.generated_files_root
        ) as tmp:
            stage = Path(tmp)
            builder._run_step(
                component,
                ComponentStatus.INSTALLING,
                "cmake staged install",
                "Install failed",
                ["cmake", "--install", "."],
                "install",
                build_dir,
                dict(env, DESTDIR=str(stage)),
            )
            staged_prefix = stage / builder.target_prefix.relative_to("/")
            (staged_prefix / "lib/pkgconfig/SPIRV-Tools-shared.pc").unlink(missing_ok=True)
            promote_staged(
                component.name,
                staged_prefix,
                builder.target_prefix,
                builder,
            )
        return

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
    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        cmake_args.extend(
            [
                f"-DCMAKE_TOOLCHAIN_FILE={cross.cmake_toolchain_file}",
                "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER",
                "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
                "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
                "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY",
            ]
        )

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
