"""Windows MSYS2 UCRT64 platform strategy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List

from .base import BasePlatformStrategy
from .context import PlatformContext


class WindowsUcrt64Platform(BasePlatformStrategy):
    """Platform strategy for Windows MSYS2 UCRT64 toolchains."""

    def setup_environment(self, context: PlatformContext) -> Dict[str, str]:
        workspace = self.normalize_path(context.workspace, context)
        ws_msys = self._to_msys_path(workspace)
        pkg_config_paths = [
            f"{ws_msys}/lib/pkgconfig",
            f"{ws_msys}/lib64/pkgconfig",
        ]
        pkg_config = ":".join(pkg_config_paths)
        ws_bin = f"{workspace}/bin"
        base_path = os.environ.get("PATH", "")
        path_value = f"{ws_bin}{os.pathsep}{base_path}" if Path(ws_bin).exists() else base_path

        cflags = f"-I{workspace}/include -Wno-int-conversion"
        cxxflags = f"-I{workspace}/include"
        ldflags = f"-L{workspace}/lib -L{workspace}/lib64"
        ldexeflags = ""

        if context.config.native_build:
            cflags += " -march=native -mtune=native"
            cxxflags += " -march=native -mtune=native"

        if context.config.openmp:
            cflags += " -fopenmp"
            cxxflags += " -fopenmp"
            ldflags += " -fopenmp"

        env = {
            "PATH": path_value,
            "PKG_CONFIG_PATH": pkg_config,
            "CFLAGS": cflags.strip(),
            "CXXFLAGS": cxxflags.strip(),
            "LDFLAGS": ldflags.strip(),
            "LDEXEFLAGS": ldexeflags.strip(),
            "LIBS": "",
        }

        if context.config.windows.msys2_root:
            root = context.config.windows.msys2_root
            root = root.replace("\\", "/")
            env["MSYS2_ROOT"] = root
        return env

    def normalize_path(self, path: str | Path, context: PlatformContext) -> str:
        value = str(path).replace("\\", "/")
        match = re.match(r"^([A-Za-z]):/(.*)$", value)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2)
            return f"/{drive}/{rest}"
        return value

    def get_pkg_config_path(self, context: PlatformContext, for_posix_shell: bool = False) -> str:
        workspace = self.normalize_path(context.workspace, context)
        if for_posix_shell:
            return ":".join([
                f"{workspace}/lib/pkgconfig",
                f"{workspace}/lib64/pkgconfig",
                "/usr/local/lib/pkgconfig",
                "/usr/lib/pkgconfig",
            ])
        return ";".join([
            f"{workspace}/lib/pkgconfig",
            f"{workspace}/lib64/pkgconfig",
            "C:/msys64/ucrt64/lib/pkgconfig",
            "C:/msys64/ucrt64/share/pkgconfig",
        ])

    def get_cflags(self, context: PlatformContext) -> str:
        flags = f"-I{self.normalize_path(context.workspace, context)}/include -Wno-int-conversion"
        if context.config.native_build:
            flags += " -march=native -mtune=native"
        if context.config.openmp:
            flags += " -fopenmp"
        return flags.strip()

    def get_cxxflags(self, context: PlatformContext) -> str:
        flags = f"-I{self.normalize_path(context.workspace, context)}/include"
        if context.config.native_build:
            flags += " -march=native -mtune=native"
        if context.config.openmp:
            flags += " -fopenmp"
        return flags.strip()

    def get_ldflags(self, context: PlatformContext) -> str:
        flags = f"-L{self.normalize_path(context.workspace, context)}/lib -L{self.normalize_path(context.workspace, context)}/lib64"
        if context.config.openmp:
            flags += " -fopenmp"
        return flags.strip()

    def get_ldexeflags(self, context: PlatformContext) -> str:
        return ""

    def get_extralibs(self, context: PlatformContext) -> str:
        return ""

    def get_cpp_runtime_lib(self) -> str:
        return "-lstdc++"

    def get_ffmpeg_threading_flag(self) -> str:
        return "--enable-w32threads"

    def get_system_lib_paths(self, context: PlatformContext) -> List[Path]:
        root = Path(context.config.windows.msys2_root or r"C:/msys64")
        return [
            Path(context.workspace) / "lib",
            Path(context.workspace) / "lib64",
            root / "ucrt64" / "lib",
            root / "ucrt64" / "lib64",
        ]

    def _to_msys_path(self, path: str) -> str:
        normalized = path.replace("\\", "/")
        match = re.match(r"^([A-Za-z]):/(.*)$", normalized)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2)
            return f"/{drive}/{rest}"
        return normalized
