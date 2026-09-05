"""macOS platform strategy."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List

from .base import BasePlatformStrategy
from .context import PlatformContext


class MacOSPlatform(BasePlatformStrategy):
    """Platform-specific behavior for macOS / Darwin builds."""

    def setup_environment(self, context: PlatformContext) -> Dict[str, str]:
        workspace = self.normalize_path(context.workspace, context)
        packages = self.normalize_path(context.packages, context)
        base_path = os.environ.get("PATH", "")
        ws_bin = f"{workspace}/bin"
        path_value = f"{ws_bin}{os.pathsep}{base_path}" if Path(ws_bin).exists() else base_path

        cflags = f"-I{workspace}/include -Wno-int-conversion"
        cxxflags = f"-I{workspace}/include"
        ldflags = f"-L{workspace}/lib -L{workspace}/lib64"
        ldexeflags = ""
        extralibs = "-ldl -lpthread -lm -lz"

        if context.config.openmp:
            cflags += " -fopenmp"
            cxxflags += " -fopenmp"
            omp_dir = self._resolve_openmp_runtime_dir()
            if omp_dir is None:
                raise RuntimeError(
                    "openmp=true on macOS but no OpenMP runtime library was found. "
                    "Install libomp or disable OpenMP."
                )
            ldflags += f" -L{omp_dir} -Wl,-rpath,{omp_dir} -lomp"

        env = {
            "PATH": path_value,
            "PKG_CONFIG_PATH": self.get_pkg_config_path(context),
            "CFLAGS": cflags,
            "CXXFLAGS": cxxflags,
            "LDFLAGS": ldflags,
            "LDEXEFLAGS": ldexeflags,
            "LIBS": extralibs,
        }

        configured_name = context.config.macos.clang
        configured_cc = shutil.which(configured_name)
        if not configured_cc and configured_name.startswith("macports-clang-"):
            version = configured_name.removeprefix("macports-clang-")
            configured_cc = shutil.which(f"clang-mp-{version}")

        detected = getattr(context.platform_info, "macports_clang", None)
        clang_path = configured_cc or (detected.path if detected else None)
        clangxx_path = None
        if clang_path:
            clangxx_path = clang_path.replace("clang", "clang++")
            if not Path(clangxx_path).exists():
                clangxx_path = None

        if clang_path and clangxx_path:
            env["CC"] = clang_path
            env["CXX"] = clangxx_path
        elif context.config.openmp:
            raise RuntimeError(
                "openmp=true on macOS requires a compiler with OpenMP support "
                f"(configured compiler '{context.config.macos.clang}' was not found)."
            )

        if getattr(context.platform_info, "vulkan_sdk_available", False):
            sdk_root = Path(context.platform_info.vulkan_sdk_path)
            sdk_lib = sdk_root / "lib"
            sdk_include = sdk_root / "include"
            env["VULKAN_SDK"] = str(sdk_root)
            if sdk_include.is_dir():
                env["CFLAGS"] += f" -I{sdk_include}"
                env["CXXFLAGS"] += f" -I{sdk_include}"
            if sdk_lib.is_dir():
                env["LDFLAGS"] += f" -L{sdk_lib}"

        env["CFLAGS"] = env["CFLAGS"].strip()
        env["CXXFLAGS"] = env["CXXFLAGS"].strip()
        env["LDFLAGS"] = env["LDFLAGS"].strip()
        return env

    def normalize_path(self, path: str | Path, context: PlatformContext) -> str:
        return str(Path(path)).as_posix()

    def get_pkg_config_path(self, context: PlatformContext, for_posix_shell: bool = False) -> str:
        workspace = self.normalize_path(context.workspace, context)
        packages = self.normalize_path(context.packages, context)
        paths = [
            f"{workspace}/lib/pkgconfig",
            f"{workspace}/lib64/pkgconfig",
            f"{packages}/pkgconfig",
            "/opt/local/lib/pkgconfig",
            "/usr/local/lib/pkgconfig",
            "/usr/local/share/pkgconfig",
            "/usr/lib/pkgconfig",
            "/usr/share/pkgconfig",
            "/usr/lib64/pkgconfig",
        ]
        return ":".join(part for part in paths if part)

    def get_cflags(self, context: PlatformContext) -> str:
        flags = f"-I{self.normalize_path(context.workspace, context)}/include -Wno-int-conversion"
        if context.config.openmp:
            flags += " -fopenmp"
        return flags.strip()

    def get_cxxflags(self, context: PlatformContext) -> str:
        flags = f"-I{self.normalize_path(context.workspace, context)}/include"
        if context.config.openmp:
            flags += " -fopenmp"
        return flags.strip()

    def get_ldflags(self, context: PlatformContext) -> str:
        flags = f"-L{self.normalize_path(context.workspace, context)}/lib -L{self.normalize_path(context.workspace, context)}/lib64"
        if context.config.openmp:
            omp_dir = self._resolve_openmp_runtime_dir()
            if omp_dir:
                flags += f" -L{omp_dir} -Wl,-rpath,{omp_dir} -lomp"
        return flags.strip()

    def get_ldexeflags(self, context: PlatformContext) -> str:
        return ""

    def get_extralibs(self, context: PlatformContext) -> str:
        return "-ldl -lpthread -lm -lz"

    def get_cpp_runtime_lib(self) -> str:
        return "-lc++"

    def get_ffmpeg_threading_flag(self) -> str:
        return "--enable-pthreads"

    def get_system_lib_paths(self, context: PlatformContext) -> List[Path]:
        ws = Path(context.workspace)
        return [ws / "lib", ws / "lib64", Path("/opt/local/lib"), Path("/usr/local/lib")]

    @staticmethod
    def _resolve_openmp_runtime_dir() -> str | None:
        candidates = [
            Path("/opt/local/lib/libomp"),
            Path("/opt/local/lib"),
            Path("/opt/homebrew/opt/libomp/lib"),
            Path("/usr/local/opt/libomp/lib"),
        ]
        for directory in candidates:
            if (directory / "libomp.dylib").exists():
                return str(directory)
        return None
