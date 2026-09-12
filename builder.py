"""Build orchestration engine."""

import importlib.util
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from .build_steps import run_install, run_make, run_step
from .build_types import BuildError, SkipComponent
from .builders.base import (
    CARGO_C_VERSION,
    ComponentBuildContext,
    build_autotools,
    build_cargo,
    build_cmake,
    build_make_only,
    build_meson,
    dispatch_component_build,
    get_rustc_version,
    install_headers_only,
)
from .builders.codecs.jxl import build_libjxl
from .builders.codecs.vorbis import build_libvorbis
from .builders.codecs.vpx import build_libvpx
from .builders.codecs.x264 import build_x264
from .builders.codecs.x265 import build_x265
from .builders.codecs.zimg import build_zimg
from .builders.crypto.openssl import build_openssl
from .builders.ffmpeg.ffmpeg import build_ffmpeg
from .builders.graphics.glslang import build_glslang
from .builders.graphics.placebo import (
    build_libplacebo,
    patch_libplacebo_glslang_search,
    patch_libplacebo_pc,
)
from .builders.graphics.vmaf import build_libvmaf
from .builders.network.srt import build_srt
from .builders.network.zmq import build_libzmq
from .builders.tools.meson import build_meson as build_meson_component
from .builders.tools.ninja import build_ninja
from .components import BuildSystem, Component, ComponentRegistry
from .config import BuildConfig
from .downloader import AsyncDownloadManager, Downloader
from .executor import CommandExecutor, ExecutionResult
from .patches import get_patch_registry
from .platform_detect import PlatformDetector
from .platforms.context import PlatformContext
from .platforms.resolver import PlatformStrategyResolver
from .release_bundle import make_release_bundle as create_release_bundle
from .state import ComponentStatus, StateManager

# The package is the repository root (flat layout); relative
# source-archive paths are anchored to it so they do not depend on the CWD.
PROJECT_ROOT = Path(__file__).resolve().parent


def _rmtree(path: Path) -> None:
    """Remove a directory tree, handling read-only files on Windows.

    Git repositories mark objects as read-only; shutil.rmtree fails with
    [WinError 5] on Windows without an onerror handler.
    """

    def _on_error(
        func: Callable[..., Any],
        fpath: str,
        exc_info: Tuple[Type[BaseException], BaseException, TracebackType],
    ) -> None:
        # Clear the read-only bit and retry.
        try:
            os.chmod(fpath, stat.S_IWRITE)
            func(fpath)
        except Exception:
            pass  # Best-effort; ignore secondary failures.

    shutil.rmtree(path, onerror=_on_error)


class FFmpegBuilder:
    """Orchestrates FFmpeg build process."""

    def __init__(
        self,
        config: BuildConfig,
        workspace: Path,
        packages: Path,
        state_manager: StateManager,
        platform_detector: PlatformDetector,
        on_download_status: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_download_progress: Optional[Callable[[str, int, int], None]] = None,
    ):
        """Initialize builder.

        Args:
            config: Build configuration.
            workspace: Workspace directory.
            packages: Packages directory.
            state_manager: State manager instance.
            platform_detector: Platform detector instance.
            on_download_status: Optional download status callback.
            on_log: Optional message callback.
            on_download_progress: Optional per-component download progress
                callback receiving (component_name, downloaded_bytes, total_bytes).
        """
        self.config = config
        self.workspace = workspace.absolute()
        self.packages = packages.absolute()
        archives_dir = Path(config.source_archives_dir)
        if archives_dir.is_absolute():
            self.source_archives = archives_dir
        else:
            self.source_archives = PROJECT_ROOT / archives_dir
        self.state_manager = state_manager
        self.platform_detector = platform_detector

        self.executor = CommandExecutor(self.workspace)
        self.downloader = Downloader(
            packages_dir=self.packages,
            source_archives_dir=self.source_archives,
            allow_network_downloads=config.allow_network_downloads,
            on_log=on_log,
            require_sha256_for_network=config.require_sha256_for_network,
        )
        self.on_download_status = on_download_status
        self.on_log = on_log
        self.on_download_progress = on_download_progress
        self.async_download_manager = None
        if config.async_downloads:
            self.async_download_manager = AsyncDownloadManager(
                self.downloader,
                config.download_workers,
                on_download_status,
                on_log,
                on_download_progress,
            )
        self.registry = ComponentRegistry()

        self.num_jobs = platform_detector.get_num_jobs(config.num_jobs)
        self.platform = platform_detector.get_platform_name()
        self.platform_context = PlatformContext(
            config=config,
            workspace=self.workspace,
            packages=self.packages,
            platform_detector=self.platform_detector,
            platform_info=self.platform_detector.platform_info,
            num_jobs=self.num_jobs,
        )
        self.platform_strategy = PlatformStrategyResolver().resolve(self.platform_context)

        self._setup_environment()

    @staticmethod
    def _rmtree(path: Path) -> None:
        _rmtree(path)

    # ------------------------------------------------------------------
    # Build-step orchestration helpers (Fix #6)
    # ------------------------------------------------------------------

    def _run_step(
        self,
        component: Component,
        status: ComponentStatus,
        detail: str,
        error_msg: str,
        command: List[str],
        step_name: str,
        work_dir: Path,
        env: Dict[str, str],
        stdin: Optional[str] = None,
    ) -> Tuple[ExecutionResult, Path]:
        """Mark status, execute a shell command, and raise on failure.

        Replaces the three-line pattern that appeared ~40 times across all
        build functions::

            self.state_manager.mark_component_status(...)
            result, log_file = self.executor.execute_with_log(...)
            if not result.success:
                raise BuildError(...)

        Args:
            component: Component being built.
            status: Status to mark (BUILDING or INSTALLING).
            detail: Human-readable detail string shown in the UI / logs.
            error_msg: Error message used when raising ``BuildError``.
            command: Command line to run.
            step_name: Internal step identifier (used for log-file naming).
            work_dir: Working directory for execution.
            env: Environment variables.
            stdin: Optional string passed as process stdin (e.g. ar script).

        Returns:
            ``(result, log_file)`` on success.

        Raises:
            BuildError: When the command fails (non-zero exit code).
        """
        return run_step(
            self, component, status, detail, error_msg, command, step_name, work_dir, env, stdin
        )

    def _run_make(
        self,
        component: Component,
        status: ComponentStatus,
        detail: str,
        error_msg: str,
        work_dir: Path,
        jobs: int,
        env: Dict[str, str],
        timeout: Optional[int] = None,
    ) -> Tuple[ExecutionResult, Path]:
        """Mark status, run ``make``, and raise on failure.

        Thin wrapper around :meth:`_run_step` that delegates to the executor's
        ``execute_make`` helper (which constructs ``make -jN``).

        Returns ``(result, log_file)`` on success; raises ``BuildError``
        otherwise.
        """
        configured_timeout = getattr(self.config, "make_timeout_seconds", 0)
        effective_timeout = timeout
        if effective_timeout is None and configured_timeout > 0:
            effective_timeout = configured_timeout
        return run_make(
            self,
            component,
            status,
            detail,
            error_msg,
            work_dir,
            jobs,
            env,
            timeout=effective_timeout,
        )

    def _run_install(
        self,
        component: Component,
        status: ComponentStatus,
        detail: str,
        error_msg: str,
        work_dir: Path,
        env: Dict[str, str],
        timeout: Optional[int] = None,
    ) -> Tuple[ExecutionResult, Path]:
        """Mark status, run ``make install``, and raise on failure.

        Thin wrapper around :meth:`_run_step` that delegates to the executor's
        ``execute_install`` helper (which constructs ``make install``).

        Returns ``(result, log_file)`` on success; raises ``BuildError``
        otherwise.
        """
        configured_timeout = getattr(self.config, "install_timeout_seconds", 0)
        effective_timeout = timeout
        if effective_timeout is None and configured_timeout > 0:
            effective_timeout = configured_timeout
        return run_install(
            self,
            component,
            status,
            detail,
            error_msg,
            work_dir,
            env,
            timeout=effective_timeout,
        )

    def _is_windows_ucrt64_backend(self) -> bool:
        """Return True only for Windows + MSYS2 UCRT64 backend."""
        pi = self.platform_detector.platform_info
        return (
            self.platform == "windows"
            and getattr(pi, "is_msys2", False)
            and getattr(pi, "is_ucrt64", False)
            and self.config.windows.backend == "msys2-ucrt64"
        )

    def _prefer_system_packages(self) -> bool:
        """Return whether system packages must be used for system components."""
        return self._is_windows_ucrt64_backend() and self.config.windows.prefer_system_packages

    def _should_use_system_component(self, component: Component) -> bool:
        """Return whether component should be treated as system-provided."""
        if component.system_component:
            return True
        if not self._prefer_system_packages():
            return False
        return component.name in {"gettext", "openssl"}

    def _assert_patch_absent(
        self, component: Component, path: Path, marker: str, context: str
    ) -> None:
        """Verify a source patch removed `marker` from `path`.

        Raises BuildError if the marker is still present, so a component
        version bump that changed the patched source fails fast with a
        clear message instead of an obscure compile/link error.
        """
        text = path.read_text(encoding="utf-8")
        if marker in text:
            raise BuildError(
                component.name,
                f"Source patch did not take effect in {path}: '{marker}' still present "
                f"({context}). The component version may have changed.",
            )

    def _assert_patch_present(
        self, component: Component, path: Path, marker: str, context: str
    ) -> None:
        """Verify a source patch added `marker` to `path`.

        Raises BuildError if the marker is missing, so a component version
        bump that moved the patch anchor fails fast with a clear message
        instead of an obscure compile error.
        """
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            raise BuildError(
                component.name,
                f"Source patch did not take effect in {path}: '{marker}' missing "
                f"({context}). The component version may have changed.",
            )

    def _normalize_windows_path_for_flags(self, path: str) -> str:
        """Normalize Windows path for shell-expanded build flags.

        For MSYS2/UCRT64 builds, values are often expanded through Make
        variables, where shell quotes embedded in env vars are not preserved.
        Prefer 8.3 short paths to remove spaces; if unavailable, escape spaces.
        """
        if self.platform_strategy is not None:
            return self.platform_strategy.normalize_path(path, self.platform_context)

        if not self._is_windows_ucrt64_backend():
            return path

        native = path.replace("/", "\\")
        normalized = native.replace("\\", "/")
        if " " not in normalized:
            return normalized

        if sys.platform == "win32":
            try:
                import ctypes

                buffer = ctypes.create_unicode_buffer(32768)
                result = ctypes.windll.kernel32.GetShortPathNameW(native, buffer, len(buffer))
                if result:
                    short_path = buffer.value.replace("\\", "/")
                    if short_path:
                        return short_path
            except Exception:
                pass

        return normalized.replace(" ", "\\ ")

    def _to_msys_path(self, path: str) -> str:
        """Convert Windows path to MSYS style when running UCRT64 backend."""
        if self.platform_strategy is not None and self.platform == "windows":
            return self.platform_strategy.normalize_path(path, self.platform_context)

        normalized = path.replace("\\", "/")
        if not self._is_windows_ucrt64_backend():
            return normalized
        match = re.match(r"^([A-Za-z]):/(.*)$", normalized)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2)
            return f"/{drive}/{rest}"
        return normalized

    @staticmethod
    def _remove_compiler_flag(flags: str, flag: str) -> str:
        """Remove exact compiler flag token from a whitespace-delimited string."""
        if not flags:
            return flags
        return " ".join(token for token in flags.split() if token != flag)

    @staticmethod
    def _resolve_darwin_openmp_runtime() -> Tuple[Optional[str], Optional[str]]:
        """Resolve macOS OpenMP runtime library directory and linker flag.

        Returns:
            Tuple of (library_dir, linker_flag), for example
            ("/opt/local/lib/libomp", "-lomp"). Returns (None, None) when no
            compatible OpenMP runtime library is found in known locations.
        """
        runtime_candidates = [
            ("libomp.dylib", "-lomp"),
            ("libgomp.dylib", "-lgomp"),
            ("libiomp5.dylib", "-liomp5"),
        ]
        search_dirs = [
            Path("/opt/local/lib/libomp"),  # MacPorts libomp runtime
            Path("/opt/local/lib"),  # MacPorts generic lib dir
            Path("/opt/homebrew/opt/libomp/lib"),  # Homebrew on Apple Silicon
            Path("/usr/local/opt/libomp/lib"),  # Homebrew on Intel
        ]

        for directory in search_dirs:
            for library_name, linker_flag in runtime_candidates:
                if (directory / library_name).exists():
                    return str(directory), linker_flag

        return None, None

    def _ws_str(self) -> str:
        """Return workspace path as a forward-slash string.

        On Windows backslashes in path strings can be interpreted as escape
        characters by POSIX shells (sh.exe/bash) or cause issues with MSYS2
        tools.  Always use forward slashes so the path is safe in both
        POSIX-shell contexts (FFmpeg ./configure, autotools) and native Windows
        tool arguments (CMake, Meson prefix, pkg-config).
        """
        if self.platform_strategy is not None:
            return self.platform_strategy.normalize_path(self.workspace, self.platform_context)
        return str(self.workspace).replace("\\", "/")

    def _normalize_pkg_config_path_for_msys(self, value: str) -> str:
        """Normalize PKG_CONFIG_PATH to MSYS style for UCRT64 subprocesses.

        Replaces Windows drive-letter prefixes (C:/ or E:/) that appear at
        the start of the string or immediately after a path separator (: / ;)
        with the MSYS equivalent (/c/ or /e/).  Processing via a regex anchor
        on (^|:) avoids corrupting path components that happen to end with a
        letter (e.g. "pkgconfig:/next/path").
        """
        if self.platform_strategy is not None:
            context = self.platform_context
            return self.platform_strategy.get_pkg_config_path(context, for_posix_shell=True)

        value = value.replace("\\", "/").replace(";", ":")
        return re.sub(
            r"(^|:)([A-Za-z]):/",
            lambda m: m.group(1) + f"/{m.group(2).lower()}/",
            value,
        )

    @staticmethod
    def _normalize_pkg_config_path_for_windows(value: str) -> str:
        """Normalize PKG_CONFIG_PATH to Windows-style entries for Meson."""
        if not value:
            return ""

        separator = ";" if ";" in value else ":"
        normalized = []
        for part in value.split(separator):
            part = part.strip().replace("\\", "/")
            if not part:
                continue
            match = re.match(r"^/([A-Za-z])/(.*)$", part)
            if match:
                part = f"{match.group(1).upper()}:/{match.group(2)}"
            if part not in normalized:
                normalized.append(part)
        return ";".join(normalized)

    def _setup_environment(self) -> None:
        """Setup build environment."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.packages.mkdir(parents=True, exist_ok=True)

        ws = self._ws_str()
        self.cflags = f"-I{ws}/include -Wno-int-conversion"
        self.ldflags = f"-L{ws}/lib -L{ws}/lib64"
        self.ldexeflags = ""
        self.extralibs = "-ldl -lpthread -lm -lz"
        # Keep C and C++ include roots aligned so Meson/CMake C++ probes can
        # resolve headers from workspace-installed dependencies.
        self.cxxflags = f"-I{ws}/include"
        if self._is_windows_ucrt64_backend():
            # -ldl / -lpthread / -lm / -lz are Linux-only; skip on Windows.
            self.extralibs = ""

        if self.platform == "linux":
            self.cflags += f" -std={self.config.linux.c_standard}"
            self.cxxflags += f" -std={self.config.linux.cxx_standard}"

        if self.config.full_static:
            if self.platform == "linux":
                self.ldexeflags = "-static -fPIC"
                self.cflags += " -fPIC"
                self.cxxflags += " -fPIC"

        if self.config.native_build:
            self.cflags += " -march=native -mtune=native"
            self.cxxflags += " -march=native -mtune=native"

        if self.config.openmp:
            if self.platform == "darwin":
                # MacPorts clang supports -fopenmp natively (Apple clang does
                # not).  libomp is installed by MacPorts under /opt/local.
                self.cflags += " -fopenmp"
                self.cxxflags += " -fopenmp"
                omp_lib_dir, omp_link_flag = self._resolve_darwin_openmp_runtime()
                if omp_lib_dir is None or omp_link_flag is None:
                    raise RuntimeError(
                        "openmp=true on macOS but no OpenMP runtime library was found. "
                        "Install libomp for your toolchain (e.g. `sudo port install libomp`) "
                        "or disable OpenMP in build_config.yaml (`openmp: false`)."
                    )
                self.ldflags += f" -L{omp_lib_dir} -Wl,-rpath,{omp_lib_dir} {omp_link_flag}"
            else:
                # GCC on Linux and MinGW/UCRT64: passing -fopenmp to the
                # compiler and linker driver is sufficient; GCC automatically
                # links against libgomp.
                self.cflags += " -fopenmp"
                self.cxxflags += " -fopenmp"
                self.ldflags += " -fopenmp"

        if self._is_windows_ucrt64_backend():
            # Two contexts require different PKG_CONFIG_PATH formats on UCRT64:
            #
            # 1. POSIX-shell context (FFmpeg ./configure, autotools via sh.exe):
            #    MSYS2 bash treats ';' as a command separator inside variable
            #    assignments.  Paths must use MSYS-style (/e/...) with ':'.
            #    MSYS2's runtime layer translates /e/... to E:/... for native
            #    executables called from bash (including pkg-config.EXE).
            #
            # 2. Direct Python-subprocess context (Meson setup):
            #    pkg-config.EXE is called directly — it needs Windows paths
            #    (E:/...) with ';' separator.
            #
            # self.env stores the POSIX variant for (1).
            # _build_meson() overrides it with the Windows variant for (2).
            ws_msys = self._to_msys_path(self._ws_str())  # /e/Projects/.../workspace
            pkg_config_paths = [
                f"{ws_msys}/lib/pkgconfig",
                f"{ws_msys}/lib64/pkgconfig",
            ]
            pkg_config_path = ":".join(pkg_config_paths)
        else:
            pkg_config_paths = [
                f"{self.workspace}/lib/pkgconfig",
                # Some CMake-based components (e.g. SVT-AV1) honour
                # CMAKE_INSTALL_LIBDIR and install to <prefix>/lib64 on
                # distributions like Fedora.
                f"{self.workspace}/lib64/pkgconfig",
            ]

            # Add architecture-specific paths for Linux
            if self.platform == "linux":
                multiarch = self.platform_detector.get_multiarch_dir()
                if multiarch:
                    # Meson/GNUInstallDirs may install into
                    # <prefix>/lib/<multiarch>/pkgconfig (e.g. libplacebo).
                    pkg_config_paths.append(f"{self.workspace}/lib/{multiarch}/pkgconfig")
                    pkg_config_paths.append(f"/usr/local/lib/{multiarch}/pkgconfig")
                    pkg_config_paths.append(f"/usr/lib/{multiarch}/pkgconfig")

            # Add generic paths
            pkg_config_paths.extend(
                [
                    "/usr/local/lib/pkgconfig",
                    "/usr/local/share/pkgconfig",
                    "/usr/lib/pkgconfig",
                    "/usr/share/pkgconfig",
                    "/usr/lib64/pkgconfig",
                ]
            )
            pkg_config_path = ":".join(pkg_config_paths)

        ws_bin = f"{self._ws_str()}/bin"
        inherited_path = os.environ.get("PATH", "")
        if os.path.isdir(ws_bin):
            path_value = f"{ws_bin}{os.pathsep}{inherited_path}"
        else:
            path_value = inherited_path

        self.env = {
            "PATH": path_value,
            "PKG_CONFIG_PATH": pkg_config_path,
            "CFLAGS": self.cflags,
            "CXXFLAGS": self.cxxflags,
            "LDFLAGS": self.ldflags,
            "LDEXEFLAGS": self.ldexeflags,
        }

        if self.platform == "darwin":
            # Honour configured macOS compiler first; fallback to auto-detected
            # MacPorts clang. This avoids FFmpeg defaulting to /usr/bin/gcc
            # (Apple clang shim), which does not accept -fopenmp.
            #
            # MacPorts names the binary clang-mp-N, but build_config.yaml
            # stores it as macports-clang-N (human-readable alias).  Translate
            # both forms so that shutil.which() can resolve the path.
            configured_name = self.config.macos.clang
            configured_cc = shutil.which(configured_name)
            if not configured_cc and configured_name.startswith("macports-clang-"):
                ver = configured_name.removeprefix("macports-clang-")
                configured_cc = shutil.which(f"clang-mp-{ver}")

            detected = self.platform_detector.platform_info.macports_clang
            clang_path = configured_cc or (detected.path if detected else None)
            clangxx_path = None
            if clang_path:
                clangxx_path = clang_path.replace("clang", "clang++")
                if not Path(clangxx_path).exists():
                    clangxx_path = None

            if clang_path and clangxx_path:
                self.env["CC"] = clang_path
                self.env["CXX"] = clangxx_path
            elif self.config.openmp:
                raise RuntimeError(
                    "openmp=true on macOS requires a compiler with OpenMP support "
                    f"(configured compiler '{self.config.macos.clang}' was not found). "
                    "Install MacPorts clang (e.g. `sudo port install clang-17`) and "
                    "set macos.clang accordingly, or disable OpenMP."
                )

        # CUDA paths
        if self.platform_detector.platform_info.cuda_available:
            cuda_path = self.platform_detector.platform_info.cuda_path
            if cuda_path:
                cuda_home = str(Path(cuda_path).parent.parent)
                cuda_home = self._normalize_windows_path_for_flags(cuda_home)
                cuda_include = f"{cuda_home}/include"
                cuda_lib64 = f"{cuda_home}/lib64"

                self.cflags += f" -I{cuda_include}"
                self.ldflags += f" -L{cuda_lib64}"
                if self.platform_detector.platform_info.is_wsl2:
                    self.ldflags += " -L/usr/lib/wsl/lib"
                self.env["PATH"] = f"{cuda_home}/bin:{self.env['PATH']}"
                self.env["CFLAGS"] = self.cflags
                self.env["LDFLAGS"] = self.ldflags

        # Vulkan SDK paths: when a LunarG SDK is activated via VULKAN_SDK
        # (e.g. by sourcing setup-env.sh), propagate it into the build
        # environment. self.env is an explicit allow-list (not a copy of
        # os.environ), so without this, VULKAN_SDK/lib and its pkgconfig
        # directory would be invisible to subprocesses even though the
        # detector (platform_detect.py::_check_vulkan) already recognises
        # this as a second, independent Vulkan "environment" alongside the
        # system loader/driver install.
        vulkan_info = self.platform_detector.platform_info
        vulkan_sdk_path = getattr(vulkan_info, "vulkan_sdk_path", None)
        if getattr(vulkan_info, "vulkan_sdk_available", False) and vulkan_sdk_path:
            sdk_root = Path(vulkan_sdk_path)
            sdk_include = sdk_root / "include"
            sdk_lib = sdk_root / "lib"
            sdk_bin = sdk_root / "bin"
            sdk_pkgconfig = sdk_lib / "pkgconfig"

            self.env["VULKAN_SDK"] = str(sdk_root)
            if sdk_include.is_dir():
                self.cflags += f" -I{sdk_include}"
                self.cxxflags += f" -I{sdk_include}"
            if sdk_lib.is_dir():
                self.ldflags += f" -L{sdk_lib}"
            if sdk_pkgconfig.is_dir():
                existing_pkg_config = self.env.get("PKG_CONFIG_PATH", "")
                self.env["PKG_CONFIG_PATH"] = (
                    f"{sdk_pkgconfig}{os.pathsep}{existing_pkg_config}"
                    if existing_pkg_config
                    else str(sdk_pkgconfig)
                )
            if sdk_bin.is_dir():
                self.env["PATH"] = f"{sdk_bin}{os.pathsep}{self.env['PATH']}"

            self.env["CFLAGS"] = self.cflags
            self.env["CXXFLAGS"] = self.cxxflags
            self.env["LDFLAGS"] = self.ldflags

        # VAAPI (libva) needs no extra environment wiring here: it is
        # resolved entirely through the system PKG_CONFIG_PATH entries
        # already populated above (including the Linux multiarch pkgconfig
        # directories), which is where libva.pc normally lives.

    def get_build_env(self, component: Optional[Component] = None) -> Dict[str, str]:
        """Get build environment for a component.

        Args:
            component: Component instance.

        Returns:
            Environment dictionary.
        """
        env = self.env.copy()

        if component:
            if self.platform in component.platform_overrides:
                override = component.platform_overrides[self.platform]
                env.update(override.extra_env)

                if override.extra_cflags:
                    env["CFLAGS"] += f" {override.extra_cflags}"
                if override.extra_cxxflags:
                    env["CXXFLAGS"] += f" {override.extra_cxxflags}"
                if override.extra_ldflags:
                    env["LDFLAGS"] += f" {override.extra_ldflags}"

            env.update(component.extra_env)

        return env

    @staticmethod
    def _prepend_python_module_parent_to_pythonpath(
        env: Dict[str, str],
        module_name: str,
    ) -> None:
        """Add module parent dir to PYTHONPATH if module is importable.

        Meson may pick `/usr/bin/python3` for custom commands even when the
        builder itself runs from another Python environment. This keeps Python
        module lookups (e.g. jinja2 for libplacebo shaders) aligned.
        """
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return

        parent_dir: Optional[Path] = None
        if spec.submodule_search_locations:
            first = next(iter(spec.submodule_search_locations), None)
            if first:
                parent_dir = Path(first).resolve().parent
        elif spec.origin and spec.origin != "built-in":
            parent_dir = Path(spec.origin).resolve().parent

        if parent_dir is None:
            return

        existing = env.get("PYTHONPATH", "")
        paths = [str(parent_dir)]
        if existing:
            paths.extend(p for p in existing.split(os.pathsep) if p)
        deduped = []
        for path in paths:
            if path not in deduped:
                deduped.append(path)
        env["PYTHONPATH"] = os.pathsep.join(deduped)

    @staticmethod
    def _merge_path_list(existing: str, new_paths: List[str], separator: str) -> str:
        """Prepend paths while preserving any existing entries.

        The first occurrences win and duplicates are removed while preserving
        order.
        """
        merged: List[str] = []
        for path in new_paths:
            if path and path not in merged:
                merged.append(path)
        if existing:
            for path in existing.split(separator):
                if path and path not in merged:
                    merged.append(path)
        return separator.join(merged)

    def prefetch_downloads(self, components: List[Component]) -> None:
        """Start background downloads for buildable source archives.

        Args:
            components: Components to prefetch.
        """
        if self.async_download_manager is None:
            return

        prefetch_components = []
        for component in components:
            if self.state_manager.is_component_completed(component.name, component.version):
                continue
            if component.name == "giflib":
                # giflib is now always system-provided on all platforms.
                continue
            if self._should_use_system_component(component) and self._prefer_system_packages():
                continue
            if self._should_use_system_component(component):
                tool = component.system_tool_name or component.name
                if self._is_system_component_available(tool):
                    continue
            prefetch_components.append(component)

        self.async_download_manager.prefetch(prefetch_components)
        if self.on_log is not None and prefetch_components:
            self.on_log(f"Prefetch queued {len(prefetch_components)} archives")

    def retry_download(self, component: Component) -> None:
        """Retry a background download for a component.

        Args:
            component: Component to retry.
        """
        if self.async_download_manager is not None:
            self.async_download_manager.retry(component)

    def shutdown_downloads(self, wait: bool = True) -> None:
        """Shutdown background download workers.

        Args:
            wait: Whether to wait for running downloads.
        """
        if self.async_download_manager is not None:
            self.async_download_manager.shutdown(wait)

    def build_component(self, component: Component) -> None:
        """Build a single component.

        Args:
            component: Component to build.
        """
        if self.state_manager.is_component_completed(component.name, component.version):
            return

        if component.name == "giflib":
            tool = component.system_tool_name or component.name
            if self._is_system_component_available(tool):
                self.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.SYSTEM,
                    component.version,
                )
                return
            raise BuildError(
                component.name,
                (
                    "Required system component 'giflib' is not available. "
                    "Install giflib development/runtime packages for your platform "
                    "(for example: libgif-dev on Debian/Ubuntu, giflib on MacPorts, "
                    "or mingw-w64-ucrt-x86_64-giflib on MSYS2 UCRT64)."
                ),
            )

        # System-package mode for Windows MSYS2/UCRT64: do not fallback to
        # source downloads for declared system components.
        if self._should_use_system_component(component) and self._prefer_system_packages():
            tool = component.system_tool_name or component.name
            if self._is_system_component_available(tool):
                self.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.SYSTEM,
                    component.version,
                )
                return
            raise BuildError(
                component.name,
                (
                    f"Required system component '{tool}' is not available in MSYS2 UCRT64 "
                    "environment. Install it with pacman (or disable "
                    "windows.prefer_system_packages)."
                ),
            )

        # Skip system components if already available
        if self._should_use_system_component(component):
            tool = component.system_tool_name or component.name
            if self._is_system_component_available(tool):
                self.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.SYSTEM,
                    component.version,
                )
                return

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.DOWNLOADING,
            component.version,
            detail="queued" if self.async_download_manager is not None else "starting",
        )

        archive_path = self._download_and_extract(component)
        source_dir = self.packages / component.get_target_dir()

        self._apply_component_patches(component, source_dir)
        build_context = ComponentBuildContext.from_builder(self)

        if component.build_system == BuildSystem.HEADERS_ONLY:
            self.state_manager.mark_component_status(
                component.name,
                ComponentStatus.INSTALLING,
                component.version,
                detail="install headers",
            )
            install_headers_only(build_context, component, source_dir)
            self._execute_post_install(component, source_dir)
            self.state_manager.mark_component_status(
                component.name,
                ComponentStatus.COMPLETED,
                component.version,
            )
            return

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.CONFIGURING,
            component.version,
            detail=self._configure_detail(component),
        )

        try:
            dispatch_component_build(build_context, component, source_dir)
        except ValueError as exc:
            raise BuildError(component.name, str(exc)) from exc

        self._execute_post_install(component, source_dir)

    def _configure_detail(self, component: Component) -> str:
        """Return a short human-readable description of the configure step."""
        system = component.build_system
        if system == BuildSystem.AUTOTOOLS:
            return "./configure"
        if system == BuildSystem.CMAKE:
            return "cmake"
        if system == BuildSystem.MESON:
            return "meson setup"
        if system == BuildSystem.MAKE_ONLY:
            return "make"
        if system == BuildSystem.CARGO:
            return "cargo"
        return ""

    def _apply_component_patches(self, component: Component, source_dir: Path) -> None:
        """Apply the declarative patch registry before configure/build steps."""
        get_patch_registry().apply_patches(component, source_dir, self.platform_strategy)

    def _execute_post_install(self, component: Component, source_dir: Path) -> None:
        """Execute post-install commands if defined.

        Args:
            component: Component to process.
            source_dir: Source directory.
        """
        if not component.post_install:
            return

        cmd = component.post_install.replace("{workspace}", shlex.quote(self._ws_str()))
        env = self.get_build_env(component)

        result, log_file = self.executor.execute_with_log(
            ["sh", "-c", cmd],
            component.name,
            "post-install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, f"Post-install failed: {cmd}", log_file)

    def _is_system_component_available(self, tool_name: str) -> bool:
        """Check if a system component is already available.

        Args:
            tool_name: Name of the tool/library to check.

        Returns:
            True if available in system, False otherwise.
        """
        # Check if it's a known tool
        if tool_name in self.platform_detector.tools:
            tool_info = self.platform_detector.tools[tool_name]
            if tool_info.available:
                return True

        tool_aliases = {
            "pkg-config": ["pkg-config", "pkgconf"],
            "libtool": ["libtool", "libtoolize"],
            "automake": ["automake", "automake-1.18", "automake-1.17", "automake-1.16"],
            "autoconf": ["autoconf", "autoconf-2.72", "autoconf-2.71"],
        }
        for candidate in tool_aliases.get(tool_name, [tool_name]):
            if self._command_exists(candidate):
                return True

        pkg_names = [tool_name]
        if tool_name == "giflib":
            pkg_names = ["giflib", "gif"]
        for pkg_name in pkg_names:
            try:
                result = subprocess.run(
                    ["pkg-config", "--exists", pkg_name], capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass

        # Check for common library headers
        lib_headers = [
            Path("/usr/include"),
            Path("/usr/local/include"),
            Path("/opt/local/include"),
            Path("/opt/homebrew/include"),
        ]
        mingw_prefix = os.environ.get("MINGW_PREFIX")
        if mingw_prefix:
            lib_headers.append(Path(mingw_prefix) / "include")
        if self._is_windows_ucrt64_backend():
            lib_headers.append(Path(self.config.windows.msys2_root) / "ucrt64" / "include")
            lib_headers.append(Path("/ucrt64/include"))

        header_by_tool = {
            "giflib": "gif_lib.h",
            "zlib": "zlib.h",
        }

        header_name = header_by_tool.get(tool_name)
        if header_name:
            for include_root in lib_headers:
                if (Path(include_root) / header_name).exists():
                    return True

        if tool_name == "giflib":
            lib_roots = [
                Path("/usr/lib"),
                Path("/usr/local/lib"),
                Path("/opt/local/lib"),
                Path("/opt/homebrew/lib"),
            ]
            if mingw_prefix:
                lib_roots.append(Path(mingw_prefix) / "lib")
            if self._is_windows_ucrt64_backend():
                lib_roots.append(Path(self.config.windows.msys2_root) / "ucrt64" / "lib")
                lib_roots.append(Path("/ucrt64/lib"))
            for root in lib_roots:
                for name in ("libgif.a", "libgif.so", "libgif.dylib", "libgif.dll.a"):
                    if (root / name).exists():
                        return True

        return False

    def _command_exists(self, command: str) -> bool:
        """Check command availability with MSYS2/UCRT64-aware PATH probing."""
        if shutil.which(command):
            return True

        search_dirs = []
        path_env = os.environ.get("PATH", "")
        separators = [os.pathsep]
        if self._is_windows_ucrt64_backend():
            separators.extend([":", ";"])

        for sep in separators:
            for item in path_env.split(sep):
                item = item.strip()
                if item and item not in search_dirs:
                    search_dirs.append(item)

        if self._is_windows_ucrt64_backend():
            msys_root = Path(self.config.windows.msys2_root)
            for extra in (
                msys_root / "usr" / "bin",
                msys_root / "ucrt64" / "bin",
                Path("/usr/bin"),
                Path("/ucrt64/bin"),
            ):
                extra_str = str(extra)
                if extra_str not in search_dirs:
                    search_dirs.append(extra_str)

        suffixes = ["", ".exe", ".cmd", ".bat"]
        for base in search_dirs:
            base_path = Path(base)
            for suffix in suffixes:
                candidate = base_path / f"{command}{suffix}"
                if candidate.exists():
                    return True

        return False

    def _download_and_extract(self, component: Component) -> Path:
        """Download and extract component source.

        Args:
            component: Component to download.

        Returns:
            Path to extracted source.
        """
        url = component.get_url()
        filename = component.get_archive_filename()

        try:
            if self.async_download_manager is None:
                if self.on_download_status is not None:
                    self.on_download_status(component.name, "downloading")
                archive_path = self.downloader.download(
                    url,
                    filename,
                    expected_sha256=component.sha256,
                    show_progress=self.on_download_status is None,
                )
            else:
                archive_path = self.async_download_manager.get(component)
        except Exception as e:
            raise BuildError(component.name, f"Failed to download archive: {e}")

        target_dir = self.packages / component.get_target_dir()
        if target_dir.exists():
            _rmtree(target_dir)
        target_dir.mkdir(parents=True)

        try:
            with tarfile.open(archive_path, "r:*") as tar:
                if component.archive_strip_components == 1:
                    # Extract to a temporary staging directory, then promote
                    # the top-level directory contents into target_dir. This
                    # avoids in-place mutation of tar member objects which is
                    # error-prone across tarfile versions.
                    staging = target_dir.parent / (target_dir.name + "_staging")
                    if staging.exists():
                        _rmtree(staging)
                    staging.mkdir(parents=True)
                    tar.extractall(staging, filter="data")

                    # Move contents of the top-level directory into target_dir.
                    # When strip_components=1, archives typically contain a single
                    # top-level directory whose contents should be promoted.
                    top_items = list(staging.iterdir())
                    if len(top_items) == 1 and top_items[0].is_dir():
                        # Strip the top-level directory: move its contents up.
                        for child in sorted(top_items[0].iterdir()):
                            child.rename(target_dir / child.name)
                    else:
                        # Fallback: move items as-is (should not happen normally).
                        for item in sorted(staging.iterdir()):
                            item.rename(target_dir / item.name)

                    if staging.exists():
                        _rmtree(staging)
                else:
                    tar.extractall(target_dir, filter="data")
        except Exception as e:
            raise BuildError(component.name, f"Failed to extract archive: {e}")

        return archive_path

    def _build_autotools(self, component: Component, source_dir: Path) -> None:
        """Build component with autotools."""
        build_autotools(ComponentBuildContext.from_builder(self), component, source_dir)

    def _build_cmake(self, component: Component, source_dir: Path) -> None:
        """Build component with CMake."""
        build_cmake(ComponentBuildContext.from_builder(self), component, source_dir)

    def _build_meson(self, component: Component, source_dir: Path) -> None:
        """Build component with Meson."""
        build_meson(ComponentBuildContext.from_builder(self), component, source_dir)

    def _build_make_only(self, component: Component, source_dir: Path) -> None:
        """Build component with make only."""
        build_make_only(ComponentBuildContext.from_builder(self), component, source_dir)

    def _get_rustc_version(self) -> Optional[Tuple[int, int, int]]:
        """Get installed rustc version."""
        return get_rustc_version(ComponentBuildContext.from_builder(self))

    def _build_cargo(self, component: Component, source_dir: Path) -> None:
        """Build component with Cargo."""
        build_cargo(ComponentBuildContext.from_builder(self), component, source_dir)

    def _install_headers_only(self, component: Component, source_dir: Path) -> None:
        """Install headers only."""
        install_headers_only(ComponentBuildContext.from_builder(self), component, source_dir)

    def build_openssl(self, component: Component, source_dir: Path) -> None:
        """Build OpenSSL."""
        build_openssl(self, component, source_dir)

    def build_x264(self, component: Component, source_dir: Path) -> None:
        """Build x264."""
        build_x264(self, component, source_dir)

    def build_x265(self, component: Component, source_dir: Path) -> None:
        """Build x265 (multi-bitdepth)."""
        build_x265(self, component, source_dir)

    def build_libvpx(self, component: Component, source_dir: Path) -> None:
        """Build libvpx."""
        build_libvpx(self, component, source_dir)

    def build_zimg(self, component: Component, source_dir: Path) -> None:
        """Build zimg."""
        build_zimg(self, component, source_dir)

    def build_libvorbis(self, component: Component, source_dir: Path) -> None:
        """Build libvorbis."""
        build_libvorbis(self, component, source_dir)

    def build_libjxl(self, component: Component, source_dir: Path) -> None:
        """Build libjxl."""
        build_libjxl(self, component, source_dir)

    def build_libvmaf(self, component: Component, source_dir: Path) -> None:
        """Build libvmaf."""
        build_libvmaf(self, component, source_dir)

    def build_srt(self, component: Component, source_dir: Path) -> None:
        """Build srt."""
        build_srt(self, component, source_dir)

    def build_libzmq(self, component: Component, source_dir: Path) -> None:
        """Build libzmq."""
        build_libzmq(self, component, source_dir)

    def build_libplacebo(self, component: Component, source_dir: Path) -> None:
        """Build libplacebo with optional Vulkan acceleration."""
        build_libplacebo(self, component, source_dir)

    def _patch_libplacebo_glslang_search(self, source_dir: Path) -> None:
        """Patch libplacebo's glslang lookup to honor Vulkan SDK library dirs."""
        patch_libplacebo_glslang_search(source_dir)

    def _patch_libplacebo_pc(self) -> None:
        """Normalize libplacebo pkg-config metadata for FFmpeg probing."""
        patch_libplacebo_pc(self)

    def build_glslang(self, component: Component, source_dir: Path) -> None:
        """Build glslang."""
        build_glslang(self, component, source_dir)

    def build_ninja(self, component: Component, source_dir: Path) -> None:
        """Build ninja build system from source."""
        build_ninja(self, component, source_dir)

    def build_meson(self, component: Component, source_dir: Path) -> None:
        """Install meson from source when the system package is unavailable."""
        build_meson_component(self, component, source_dir)

    def build_ffmpeg(self, component: Component, source_dir: Path) -> None:
        """Build FFmpeg."""
        build_ffmpeg(self, component, source_dir)

    def make_release_bundle(self) -> Path:
        """Create a redistributable release directory for built FFmpeg binaries."""
        return create_release_bundle(self)
