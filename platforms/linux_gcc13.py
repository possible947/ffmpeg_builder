"""Linux GCC 13 platform strategy."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

from .base import BasePlatformStrategy
from .context import PlatformContext


class LinuxGcc13Platform(BasePlatformStrategy):
    """Default platform strategy for native Linux and WSL2 builds."""

    def setup_environment(self, context: PlatformContext) -> Dict[str, str]:
        workspace = self.normalize_path(context.workspace, context)
        ws_bin = f"{workspace}/bin"
        base_path = os.environ.get("PATH", "")
        path_value = f"{ws_bin}{os.pathsep}{base_path}" if Path(ws_bin).exists() else base_path

        cflags = f"-I{workspace}/include -Wno-int-conversion"
        cxxflags = f"-I{workspace}/include"
        ldflags = f"-L{workspace}/lib -L{workspace}/lib64"
        ldexeflags = ""
        extralibs = "-ldl -lpthread -lm -lz"

        if context.config.full_static:
            ldexeflags = "-static -fPIC"
            cflags += " -fPIC"
            cxxflags += " -fPIC"

        if context.config.native_build:
            cflags += " -march=native -mtune=native"
            cxxflags += " -march=native -mtune=native"

        if context.config.openmp:
            cflags += " -fopenmp"
            cxxflags += " -fopenmp"
            ldflags += " -fopenmp"

        if context.platform_info.platform == "linux":
            cflags += f" -std={context.config.linux.c_standard}"
            cxxflags += f" -std={context.config.linux.cxx_standard}"

        env = {
            "PATH": path_value,
            "PKG_CONFIG_PATH": self.get_pkg_config_path(context),
            "CFLAGS": cflags.strip(),
            "CXXFLAGS": cxxflags.strip(),
            "LDFLAGS": ldflags.strip(),
            "LDEXEFLAGS": ldexeflags.strip(),
            "LIBS": extralibs,
        }

        vulkan_sdk_path = getattr(context.platform_info, "vulkan_sdk_path", None)
        if getattr(context.platform_info, "vulkan_sdk_available", False) and vulkan_sdk_path:
            sdk_root = Path(vulkan_sdk_path)
            sdk_lib = sdk_root / "lib"
            sdk_include = sdk_root / "include"
            env["VULKAN_SDK"] = str(sdk_root)
            if sdk_include.is_dir():
                env["CFLAGS"] += f" -I{sdk_include}"
                env["CXXFLAGS"] += f" -I{sdk_include}"
            if sdk_lib.is_dir():
                env["LDFLAGS"] += f" -L{sdk_lib}"

        return env

    def normalize_path(self, path: str | Path, context: PlatformContext) -> str:
        return Path(path).as_posix()

    def get_pkg_config_path(self, context: PlatformContext, for_posix_shell: bool = False) -> str:
        workspace = self.normalize_path(context.workspace, context)
        paths = [
            f"{workspace}/lib/pkgconfig",
            f"{workspace}/lib64/pkgconfig",
            (
                f"{workspace}/lib/{self._multiarch_dir(context)}/pkgconfig"
                if self._multiarch_dir(context)
                else ""
            ),
            "/usr/local/lib/pkgconfig",
            "/usr/local/share/pkgconfig",
            "/usr/lib/pkgconfig",
            "/usr/share/pkgconfig",
            "/usr/lib64/pkgconfig",
            (
                "/usr/local/lib/{}/pkgconfig".format(self._multiarch_dir(context))
                if self._multiarch_dir(context)
                else ""
            ),
            (
                "/usr/lib/{}/pkgconfig".format(self._multiarch_dir(context))
                if self._multiarch_dir(context)
                else ""
            ),
        ]
        return ":".join(part for part in paths if part)

    def get_cflags(self, context: PlatformContext) -> str:
        flags = f"-I{self.normalize_path(context.workspace, context)}/include -Wno-int-conversion"
        if context.config.full_static:
            flags += " -fPIC"
        if context.config.native_build:
            flags += " -march=native -mtune=native"
        if context.config.openmp:
            flags += " -fopenmp"
        if context.platform_info.platform == "linux":
            flags += f" -std={context.config.linux.c_standard}"
        return flags.strip()

    def get_cxxflags(self, context: PlatformContext) -> str:
        flags = f"-I{self.normalize_path(context.workspace, context)}/include"
        if context.config.full_static:
            flags += " -fPIC"
        if context.config.native_build:
            flags += " -march=native -mtune=native"
        if context.config.openmp:
            flags += " -fopenmp"
        if context.platform_info.platform == "linux":
            flags += f" -std={context.config.linux.cxx_standard}"
        return flags.strip()

    def get_ldflags(self, context: PlatformContext) -> str:
        flags = f"-L{self.normalize_path(context.workspace, context)}/lib -L{self.normalize_path(context.workspace, context)}/lib64"
        if context.config.full_static:
            flags += " -static -fPIC"
        if context.config.openmp:
            flags += " -fopenmp"
        return flags.strip()

    def get_ldexeflags(self, context: PlatformContext) -> str:
        return "-static -fPIC" if context.config.full_static else ""

    def get_extralibs(self, context: PlatformContext) -> str:
        return "-ldl -lpthread -lm -lz"

    def get_cpp_runtime_lib(self) -> str:
        return "-lstdc++"

    def get_ffmpeg_threading_flag(self) -> str:
        return "--enable-pthreads"

    def get_system_lib_paths(self, context: PlatformContext) -> List[Path]:
        ws = Path(context.workspace)
        paths = [ws / "lib", ws / "lib64"]
        multiarch = self._multiarch_dir(context)
        if multiarch:
            paths.append(ws / "lib" / multiarch)
        paths.extend([Path("/usr/local/lib"), Path("/usr/lib"), Path("/usr/lib64")])
        if multiarch:
            paths.extend([Path(f"/usr/local/lib/{multiarch}"), Path(f"/usr/lib/{multiarch}")])
        return paths

    def _multiarch_dir(self, context: PlatformContext) -> str:
        arch = getattr(context.platform_detector.system_info, "architecture", "")
        mapping = {
            "x86_64": "x86_64-linux-gnu",
            "aarch64": "aarch64-linux-gnu",
            "arm64": "aarch64-linux-gnu",
            "armv7l": "arm-linux-gnueabihf",
            "i386": "i386-linux-gnu",
            "i686": "i386-linux-gnu",
        }
        return mapping.get(arch, "")
