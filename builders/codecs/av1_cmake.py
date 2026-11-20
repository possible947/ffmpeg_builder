"""CMake adapters for cross-built AV1 codec libraries."""

from dataclasses import replace
from pathlib import Path
import shutil
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ..base import ComponentBuildContext, build_cmake

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_av1_cmake(builder: "FFmpegBuilder", component: "Component", source_dir: Path) -> None:
    cross = builder.platform_context.cross_environment
    if cross is not None:
        nasm = shutil.which("nasm", path=cross.compiler_environment.get("PATH"))
        if not nasm:
            raise BuildError(component.name, "Required host NASM is unavailable.")
        options = [
            "-G",
            "Ninja",
            f"-DCMAKE_ASM_NASM_COMPILER={nasm}",
            "-DCMAKE_ASM_NASM_OBJECT_FORMAT=win64",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
        ]
        if component.name == "svtav1":
            options += ["-DBUILD_APPS=OFF", "-DBUILD_TESTING=OFF", "-DSVT_AV1_LTO=OFF"]
        elif component.name == "av1":
            options += [
                "-DENABLE_NASM=ON",
                "-DENABLE_TESTS=OFF",
                "-DENABLE_TESTDATA=OFF",
                "-DENABLE_EXAMPLES=OFF",
                "-DENABLE_TOOLS=OFF",
                "-DENABLE_DOCS=OFF",
            ]
        else:
            raise BuildError(component.name, "Unsupported AV1 CMake adapter.")
        component = replace(component, configure_args=[*component.configure_args, *options])
    build_cmake(ComponentBuildContext.from_builder(builder), component, source_dir)
