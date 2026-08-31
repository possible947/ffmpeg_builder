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
from .component_builders import get_custom_builder
from .components import BuildSystem, Component, ComponentRegistry
from .config import BuildConfig
from .downloader import AsyncDownloadManager, Downloader
from .executor import CommandExecutor, ExecutionResult
from .platform_detect import PlatformDetector
from .release_bundle import make_release_bundle as create_release_bundle
from .state import ComponentStatus, StateManager

# The package is the repository root (flat layout); relative
# source-archive paths are anchored to it so they do not depend on the CWD.
PROJECT_ROOT = Path(__file__).resolve().parent

# Pinned so builds are reproducible; bump deliberately when a new cargo-c
# release is validated.
CARGO_C_VERSION = "0.10.25"


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
        return str(self.workspace).replace("\\", "/")

    def _normalize_pkg_config_path_for_msys(self, value: str) -> str:
        """Normalize PKG_CONFIG_PATH to MSYS style for UCRT64 subprocesses.

        Replaces Windows drive-letter prefixes (C:/ or E:/) that appear at
        the start of the string or immediately after a path separator (: / ;)
        with the MSYS equivalent (/c/ or /e/).  Processing via a regex anchor
        on (^|:) avoids corrupting path components that happen to end with a
        letter (e.g. "pkgconfig:/next/path").
        """
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

        if component.build_system == BuildSystem.HEADERS_ONLY:
            self.state_manager.mark_component_status(
                component.name,
                ComponentStatus.INSTALLING,
                component.version,
                detail="install headers",
            )
            self._install_headers_only(component, source_dir)
            self._execute_post_install(component, source_dir)
            return

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.CONFIGURING,
            component.version,
            detail=self._configure_detail(component),
        )

        if component.custom_build_fn:
            build_fn = get_custom_builder(component.custom_build_fn)
            if build_fn:
                build_fn(self, component, source_dir)
                self._execute_post_install(component, source_dir)
                return

        if component.build_system == BuildSystem.AUTOTOOLS:
            self._build_autotools(component, source_dir)
        elif component.build_system == BuildSystem.CMAKE:
            self._build_cmake(component, source_dir)
        elif component.build_system == BuildSystem.MESON:
            self._build_meson(component, source_dir)
        elif component.build_system == BuildSystem.MAKE_ONLY:
            self._build_make_only(component, source_dir)
        elif component.build_system == BuildSystem.CARGO:
            self._build_cargo(component, source_dir)
        else:
            raise BuildError(component.name, f"Unknown build system: {component.build_system}")

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
        """Build component with autotools.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        build_dir = source_dir
        if component.workdir:
            build_dir = source_dir / component.workdir

        if component.name == "xvidcore" and self._is_windows_ucrt64_backend():
            # xvidcore 1.3.7 defines `bool` via typedef in encoder.h.
            # GCC 16 defaults to C23 where `bool` is a keyword, so this
            # typedef is rejected. Gate the typedef out for C23+.
            encoder_h = source_dir / "src" / "encoder.h"
            if encoder_h.exists():
                content = encoder_h.read_text()
                legacy = "typedef int bool;"
                patched = (
                    "#if !defined(__STDC_VERSION__) || __STDC_VERSION__ < 202311L\n"
                    "typedef int bool;\n"
                    "#endif"
                )
                if legacy in content and patched not in content:
                    encoder_h.write_text(content.replace(legacy, patched, 1))
                # An unguarded `typedef int bool;` breaks GCC 16 (C23)
                # builds, so fail loudly if the patch did not take effect.
                final = encoder_h.read_text()
                if legacy in final and patched not in final:
                    raise BuildError(
                        component.name,
                        f"Source patch did not take effect in {encoder_h}: unguarded "
                        f"'{legacy}' still present (C23 bool typedef gate). "
                        f"The xvidcore version may have changed.",
                    )

        configure_args = [
            arg.replace("{workspace}", self._ws_str()).replace("{num_jobs}", str(self.num_jobs))
            for arg in component.configure_args
        ]

        # Apply platform-specific configure_args_override if present.
        if self.platform in component.platform_overrides:
            override = component.platform_overrides[self.platform]
            if override.configure_args_override is not None:
                configure_args = [
                    arg.replace("{workspace}", self._ws_str()).replace(
                        "{num_jobs}", str(self.num_jobs)
                    )
                    for arg in override.configure_args_override
                ]

        env = self.get_build_env(component)

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            f"./configure",
            "Configure failed",
            ["./configure"] + configure_args,
            "configure",
            build_dir,
            env,
        )

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            build_dir,
            self.num_jobs,
            env,
        )

        self._run_install(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            build_dir,
            env,
        )

    def _build_cmake(self, component: Component, source_dir: Path) -> None:
        """Build component with CMake.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        build_dir = source_dir
        if component.workdir:
            build_dir = source_dir / component.workdir
            build_dir.mkdir(parents=True, exist_ok=True)

        cmake_args = [
            arg.replace("{workspace}", self._ws_str()) for arg in component.configure_args
        ]

        # Honour config.openmp: replace WITH_OPENMP:bool=off → on when
        # OpenMP is enabled (e.g. soxr exposes this CMake option).
        if self.config.openmp:
            cmake_args = [
                arg.replace("-DWITH_OPENMP:bool=off", "-DWITH_OPENMP:bool=on") for arg in cmake_args
            ]

        env = self.get_build_env(component)

        if self._is_windows_ucrt64_backend():
            # CMake calls pkg-config.EXE directly; needs Windows-style paths.
            ws = self._ws_str()
            env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

        cmake_cmd = ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + [str(source_dir)]
        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "cmake <source>",
            "CMake configure failed",
            cmake_cmd,
            "configure",
            build_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "cmake --build",
            "Build failed",
            ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
            "build",
            build_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake --install",
            "Install failed",
            ["cmake", "--install", "."],
            "install",
            build_dir,
            env,
        )

    def _build_meson(self, component: Component, source_dir: Path) -> None:
        """Build component with Meson.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        build_dir = source_dir / "build"
        if build_dir.exists():
            _rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        meson_args = [
            arg.replace("{workspace}", self._ws_str()) for arg in component.configure_args
        ]

        env = self.get_build_env(component)

        if self._is_windows_ucrt64_backend():
            # Meson calls pkg-config.EXE directly (not through bash), so it
            # needs Windows-style paths (E:/...) with ';' as separator.
            ws = self._ws_str()
            env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "meson setup build",
            "Meson configure failed",
            ["meson", "setup", "build"] + meson_args,
            "configure",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "ninja -C build",
            "Build failed",
            ["ninja", "-C", "build"],
            "build",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "ninja install",
            "Install failed",
            ["ninja", "-C", "build", "install"],
            "install",
            source_dir,
            env,
        )

    def _build_make_only(self, component: Component, source_dir: Path) -> None:
        """Build component with make only.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        build_dir = source_dir
        if component.workdir:
            build_dir = source_dir / component.workdir

        env = self.get_build_env(component)

        build_args = [arg.replace("{workspace}", self._ws_str()) for arg in component.build_args]

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs} {' '.join(build_args)}",
            "Build failed",
            ["make", f"-j{self.num_jobs}"] + build_args,
            "build",
            build_dir,
            env,
        )

        install_args = [
            arg.replace("{workspace}", self._ws_str()) for arg in component.install_args
        ]

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            f"make install {' '.join(install_args)}",
            "Install failed",
            ["make", "install"] + install_args,
            "install",
            build_dir,
            env,
        )

    def _get_rustc_version(self) -> Optional[Tuple[int, int, int]]:
        """Get installed rustc version.

        Returns:
            Tuple of (major, minor, patch) or None if not available.
        """
        env = self.get_build_env()
        result = self.executor.execute(["rustc", "--version"], env=env)
        if not result.success:
            return None
        match = re.search(r"rustc\s+(\d+)\.(\d+)\.(\d+)", result.stdout)
        if not match:
            return None
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def _build_cargo(self, component: Component, source_dir: Path) -> None:
        """Build component with Cargo.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)
        env["RUSTFLAGS"] = "-C target-cpu=native"

        rustc_version = self._get_rustc_version()
        if rustc_version is None:
            raise SkipComponent(
                component.name, "rustc is not available or version cannot be determined"
            )

        if rustc_version < (1, 95, 0):
            raise SkipComponent(
                component.name,
                f"rustc {'.'.join(map(str, rustc_version))} is too old. "
                f"cargo-c requires rustc 1.95 or newer",
            )

        cargo_c_path = shutil.which("cargo-c", path=env.get("PATH"))
        if cargo_c_path is None:
            self._run_step(
                component,
                ComponentStatus.BUILDING,
                f"cargo install cargo-c --version {CARGO_C_VERSION}",
                "Failed to install cargo-c",
                ["cargo", "install", "cargo-c", "--version", CARGO_C_VERSION],
                "install-cargo-c",
                source_dir,
                env,
            )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cargo cinstall",
            "Cargo build failed",
            [
                "cargo",
                "cinstall",
                f"--prefix={self._ws_str()}",
                "--libdir=lib",
                "--library-type=staticlib",
                "--crt-static",
                "--release",
            ],
            "build",
            source_dir,
            env,
        )

    def _install_headers_only(self, component: Component, source_dir: Path) -> None:
        """Install headers only.

        Args:
            component: Component to install.
            source_dir: Source directory.
        """
        if component.name == "VapourSynth":
            dest = self.workspace / "include" / "vapoursynth"
            dest.mkdir(parents=True, exist_ok=True)
            src = source_dir / "include"
            if src.exists():
                for item in src.iterdir():
                    dest_item = dest / item.name
                    if item.is_file():
                        shutil.copy2(item, dest_item)
                    elif item.is_dir():
                        shutil.copytree(item, dest_item, dirs_exist_ok=True)

        elif component.name == "fast-float":
            # fast_float is a header-only library used by libplacebo.
            # Install include/fast_float/ to workspace/include/fast_float/ so
            # build_libplacebo() can populate the libplacebo submodule dir.
            dest = self.workspace / "include" / "fast_float"
            dest.mkdir(parents=True, exist_ok=True)
            src = source_dir / "include" / "fast_float"
            if src.exists():
                for item in src.iterdir():
                    dest_item = dest / item.name
                    shutil.copy2(item, dest_item)

        elif component.name == "amf":
            dest = self.workspace / "include" / "AMF"
            if dest.exists():
                _rmtree(dest)
            dest.mkdir(parents=True)
            src = source_dir / "amf" / "public" / "include"
            if src.exists():
                for item in src.iterdir():
                    dest_item = dest / item.name
                    if item.is_file():
                        shutil.copy2(item, dest_item)
                    elif item.is_dir():
                        shutil.copytree(item, dest_item, dirs_exist_ok=True)

    def build_openssl(self, component: Component, source_dir: Path) -> None:
        """Build OpenSSL.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        result, log_file = self.executor.execute_with_log(
            [
                "./Configure",
                f"--prefix={self._ws_str()}",
                f"--openssldir={self._ws_str()}",
                "--libdir=lib",
                f"--with-zlib-include={self._ws_str()}/include/",
                f"--with-zlib-lib={self._ws_str()}/lib",
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

        # OpenSSL Configure forces -std=c11 on x86_64, which breaks GCC 16's
        # handling of inline assembly in crypto/bn/asm/x86_64-gcc.c. Replace it
        # with -std=gnu11 in the generated config and regenerate the Makefile.
        configdata = source_dir / "configdata.pm"
        if configdata.exists():
            content = configdata.read_text()
            content = content.replace("-std=c11", "-std=gnu11")
            configdata.write_text(content)
            self._assert_patch_absent(
                component, configdata, "-std=c11", "configdata.pm -std=c11 -> -std=gnu11"
            )
            result2 = self.executor.execute(
                ["perl", str(configdata)],
                cwd=source_dir,
                env=env,
            )
            if not result2.success:
                raise BuildError(component.name, "configdata.pm regeneration failed")

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            source_dir,
            self.num_jobs,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "make install_sw",
            "Install failed",
            ["make", "install_sw"],
            "install",
            source_dir,
            env,
        )

    def build_x264(self, component: Component, source_dir: Path) -> None:
        """Build x264.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        configure_args = [
            f"--prefix={self._ws_str()}",
            "--enable-static",
            "--enable-pic",
            "--disable-cli",
        ]

        if self.platform == "linux":
            env["CXXFLAGS"] = f"-fPIC {env.get('CXXFLAGS', '')}"

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            ["./configure"] + configure_args,
            "configure",
            source_dir,
            env,
        )

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            source_dir,
            self.num_jobs,
            env,
        )

        self._run_install(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "make install-lib-static",
            "Install lib-static failed",
            ["make", "install-lib-static"],
            "install-lib",
            source_dir,
            env,
        )

    def build_x265(self, component: Component, source_dir: Path) -> None:
        """Build x265 (multi-bitdepth).

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        if self.platform == "darwin" and self.platform_detector.platform_info.is_arm64:
            env["CXXFLAGS"] = f"-DHAVE_NEON=1 {env.get('CXXFLAGS', '')}"

        # Patch json11.cpp to include <cstdint>. Newer libstdc++ (GCC 15/16)
        # no longer transitively pulls <cstdint> via <limits>, so uint8_t
        # becomes undeclared and the dynamicHDR10 helper fails to compile.
        # Mirrors the original bash script's sed patch.
        json11_cpp = source_dir / "source" / "dynamicHDR10" / "json11" / "json11.cpp"
        if json11_cpp.exists():
            content = json11_cpp.read_text()
            if "#include <cstdint>" not in content:
                lines = content.split("\n")
                insert_idx = None
                for i, line in enumerate(lines):
                    if line.strip() == "#include <limits>":
                        insert_idx = i + 1
                        break
                if insert_idx is not None:
                    lines.insert(insert_idx, "#include <cstdint>")
                    json11_cpp.write_text("\n".join(lines))
            # uint8_t is undeclared without <cstdint> on GCC 15/16, so fail
            # loudly if the include could not be added (e.g. the anchor
            # `#include <limits>` moved in a newer x265).
            self._assert_patch_present(
                component, json11_cpp, "#include <cstdint>", "json11.cpp cstdint include"
            )

        build_linux = source_dir / "build" / "linux"
        if not build_linux.exists():
            raise BuildError(component.name, "Build directory not found")

        for bitdepth in ["12bit", "10bit", "8bit"]:
            bitdepth_dir = build_linux / bitdepth
            bitdepth_dir.mkdir(parents=True, exist_ok=True)

            cmake_args = [
                f"-DCMAKE_INSTALL_PREFIX={self._ws_str()}",
                "-DENABLE_SHARED=OFF",
                "-DBUILD_SHARED_LIBS=OFF",
            ]

            if bitdepth == "12bit":
                cmake_args.extend(
                    [
                        "-DHIGH_BIT_DEPTH=ON",
                        "-DENABLE_HDR10_PLUS=ON",
                        "-DEXPORT_C_API=OFF",
                        "-DENABLE_CLI=OFF",
                        "-DMAIN12=ON",
                    ]
                )
            elif bitdepth == "10bit":
                cmake_args.extend(
                    [
                        "-DHIGH_BIT_DEPTH=ON",
                        "-DENABLE_HDR10_PLUS=ON",
                        "-DEXPORT_C_API=OFF",
                        "-DENABLE_CLI=OFF",
                    ]
                )
            else:
                extra_libs = "x265_main10.a;x265_main12.a"
                if self.platform == "linux":
                    extra_libs += ";-ldl"
                cmake_args.extend(
                    [
                        "-DENABLE_SHARED=OFF",
                        "-DBUILD_SHARED_LIBS=OFF",
                        f"-DEXTRA_LIB={extra_libs}",
                        "-DEXTRA_LINK_FLAGS=-L.",
                        "-DLINKED_10BIT=ON",
                        "-DLINKED_12BIT=ON",
                    ]
                )

                # Copy 10bit and 12bit libraries into 8bit build dir before linking
                shutil.copy(build_linux / "10bit" / "libx265.a", bitdepth_dir / "libx265_main10.a")
                shutil.copy(build_linux / "12bit" / "libx265.a", bitdepth_dir / "libx265_main12.a")

            self._run_step(
                component,
                ComponentStatus.CONFIGURING,
                f"cmake (configure-{bitdepth})",
                f"Configure {bitdepth} failed",
                ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["../../../source"],
                f"configure-{bitdepth}",
                bitdepth_dir,
                env,
            )

            self._run_step(
                component,
                ComponentStatus.BUILDING,
                "cmake --build (multi-bitdepth)",
                f"Build {bitdepth} failed",
                ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
                f"build-{bitdepth}",
                bitdepth_dir,
                env,
            )

        eight_dir = build_linux / "8bit"
        lib_main = eight_dir / "libx265.a"
        lib_main10 = eight_dir / "libx265_main10.a"
        lib_main12 = eight_dir / "libx265_main12.a"

        shutil.copy(build_linux / "10bit" / "libx265.a", lib_main10)
        shutil.copy(build_linux / "12bit" / "libx265.a", lib_main12)

        # Rename 8bit library before merging (matching original build-ffmpeg script)
        lib_main_renamed = eight_dir / "libx265_main.a"
        shutil.move(str(lib_main), str(lib_main_renamed))

        if self.platform == "darwin":
            # x265 multi-bitdepth merge on macOS requires Apple libtool.
            # GNU libtool (glibtool) fails for this static archive merge.
            libtool = "libtool"
            if shutil.which("xcrun"):
                xcrun_result = subprocess.run(
                    ["xcrun", "-f", "libtool"],
                    capture_output=True,
                    text=True,
                )
                if xcrun_result.returncode == 0:
                    resolved = xcrun_result.stdout.strip()
                    if resolved:
                        libtool = resolved
            elif Path("/usr/bin/libtool").exists():
                libtool = "/usr/bin/libtool"
            if libtool == "libtool" and Path("/usr/bin/libtool").exists():
                libtool = "/usr/bin/libtool"

            self._run_step(
                component,
                ComponentStatus.BUILDING,
                "merge-libs",
                "Merge libs failed",
                [
                    libtool,
                    "-static",
                    "-o",
                    "libx265.a",
                    "libx265_main.a",
                    "libx265_main10.a",
                    "libx265_main12.a",
                ],
                "merge-libs",
                eight_dir,
                env,
            )
        else:
            m_script = "CREATE libx265.a\nADDLIB libx265_main.a\nADDLIB libx265_main10.a\nADDLIB libx265_main12.a\nSAVE\nEND\n"
            self._run_step(
                component,
                ComponentStatus.BUILDING,
                "merge-libs",
                "Merge libs failed",
                ["ar", "-M"],
                "merge-libs",
                eight_dir,
                env,
                stdin=m_script,
            )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake --install",
            "Install failed",
            ["cmake", "--install", "."],
            "install",
            eight_dir,
            env,
        )

        if self.config.full_static and self.platform == "linux":
            x265_pc = self.workspace / "lib" / "pkgconfig" / "x265.pc"
            if x265_pc.exists():
                content = x265_pc.read_text()
                content = content.replace("-lgcc_s", "-lgcc_eh")
                x265_pc.write_text(content)
                self._assert_patch_absent(
                    component, x265_pc, "-lgcc_s", "x265.pc -lgcc_s -> -lgcc_eh (full_static)"
                )

    def build_libvpx(self, component: Component, source_dir: Path) -> None:
        """Build libvpx.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        if self.platform == "darwin":
            makefile = source_dir / "build" / "make" / "Makefile"
            if makefile.exists():
                content = makefile.read_text()
                content = content.replace(",--version-script", "")
                content = content.replace(
                    "-Wl,--no-undefined -Wl,-soname", "-Wl,-undefined,error -Wl,-install_name"
                )
                makefile.write_text(content)
                self._assert_patch_absent(
                    component, makefile, ",--version-script", "libvpx darwin Makefile link flags"
                )
                self._assert_patch_absent(
                    component,
                    makefile,
                    "-Wl,--no-undefined -Wl,-soname",
                    "libvpx darwin Makefile link flags",
                )

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            [
                "./configure",
                f"--prefix={self._ws_str()}",
                "--disable-unit-tests",
                "--disable-shared",
                "--disable-examples",
                "--as=yasm",
                "--enable-vp9-highbitdepth",
            ],
            "configure",
            source_dir,
            env,
        )

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            source_dir,
            self.num_jobs,
            env,
        )

        self._run_install(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            source_dir,
            env,
        )

    def build_zimg(self, component: Component, source_dir: Path) -> None:
        """Build zimg.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        def _resolve_tool_path(tool: str) -> Optional[str]:
            """Resolve executable path across native Windows and MSYS2 paths."""
            raw = shutil.which(tool)
            if raw:
                return raw

            msys_root = Path(self.config.windows.msys2_root)
            candidates = [
                msys_root / "usr" / "bin" / tool,
                msys_root / "ucrt64" / "bin" / tool,
                msys_root / "usr" / "bin" / f"{tool}.exe",
                msys_root / "ucrt64" / "bin" / f"{tool}.exe",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
            return None

        # Use workspace GNU libtoolize first. On macOS it is commonly
        # installed as `glibtoolize` (Homebrew/MacPorts naming).
        candidates = [
            str(self.workspace / "bin" / "libtoolize"),
            str(self.workspace / "bin" / "glibtoolize"),
            _resolve_tool_path("libtoolize") or "",
            _resolve_tool_path("glibtoolize") or "",
        ]
        libtoolize = next((c for c in candidates if c and Path(c).exists()), None)
        if libtoolize is None:
            # Final fallback for environments where command is resolvable by
            # shell but not by absolute path probing.
            if self._command_exists("libtoolize"):
                libtoolize = "libtoolize"
            elif self._command_exists("glibtoolize"):
                libtoolize = "glibtoolize"
        if libtoolize is None:
            raise BuildError(
                component.name, "libtoolize not found (tried libtoolize and glibtoolize)"
            )

        libtoolize_cmd = [libtoolize, "-i", "-f", "-q"]
        if self._is_windows_ucrt64_backend():
            # In MSYS2, /usr/bin/libtoolize is a shell script, not a native
            # Win32 executable. Run it through sh.exe.
            suffix = Path(libtoolize).suffix.lower()
            if suffix not in (".exe", ".bat", ".cmd"):
                sh_path = _resolve_tool_path("sh") or "sh"
                libtoolize_cmd = [sh_path, libtoolize, "-i", "-f", "-q"]

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "libtoolize",
            "Libtoolize failed",
            libtoolize_cmd,
            "libtoolize",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./autogen.sh",
            "Autogen failed",
            ["./autogen.sh", f"--prefix={self._ws_str()}"],
            "autogen",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            ["./configure", f"--prefix={self._ws_str()}", "--enable-static", "--disable-shared"],
            "configure",
            source_dir,
            env,
        )

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            source_dir,
            self.num_jobs,
            env,
        )

        self._run_install(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            source_dir,
            env,
        )

    def build_libvorbis(self, component: Component, source_dir: Path) -> None:
        """Build libvorbis.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        configure_ac = source_dir / "configure.ac"
        if configure_ac.exists():
            content = configure_ac.read_text()
            content = content.replace("-force_cpusubtype_ALL", "")
            configure_ac.write_text(content)
            self._assert_patch_absent(
                component,
                configure_ac,
                "-force_cpusubtype_ALL",
                "libvorbis configure.ac cpusubtype",
            )

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./autogen.sh",
            "Autogen failed",
            ["./autogen.sh", f"--prefix={self._ws_str()}"],
            "autogen",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            [
                "./configure",
                f"--prefix={self._ws_str()}",
                f"--with-ogg-libraries={self._ws_str()}/lib",
                f"--with-ogg-includes={self._ws_str()}/include/",
                "--enable-static",
                "--disable-shared",
                "--disable-oggtest",
            ],
            "configure",
            source_dir,
            env,
        )

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            source_dir,
            self.num_jobs,
            env,
        )

        self._run_install(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            source_dir,
            env,
        )

    def build_libjxl(self, component: Component, source_dir: Path) -> None:
        """Build libjxl.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        # On some macOS setups `realpath` is missing, while libjxl's deps.sh
        # assumes it exists. Patch the script to a portable path resolution.
        deps_script = source_dir / "deps.sh"
        if self.platform == "darwin" and deps_script.exists() and shutil.which("realpath") is None:
            content = deps_script.read_text()
            original = 'SELF=$(realpath "$0")'
            guard = "command -v realpath"
            if original in content and guard not in content:
                portable = (
                    "if command -v realpath >/dev/null 2>&1; then\n"
                    '  SELF=$(realpath "$0")\n'
                    "else\n"
                    '  SELF=$(cd -- "$(dirname -- "$0")" && pwd -P)/$(basename -- "$0")\n'
                    "fi"
                )
                deps_script.write_text(content.replace(original, portable, 1))
            # Without realpath on this system, an unguarded
            # `SELF=$(realpath "$0")` makes deps.sh fail, so verify the guard
            # is in place (the anchor may have moved in a newer libjxl).
            final = deps_script.read_text()
            if original in final and guard not in final:
                raise BuildError(
                    component.name,
                    f"Source patch did not take effect in {deps_script}: unguarded "
                    f"'{original}' still present (realpath missing on this system). "
                    f"The libjxl version may have changed.",
                )

        self._run_step(
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
            f"-DCMAKE_INSTALL_PREFIX={self._ws_str()}",
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

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "cmake .",
            "Configure failed",
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
            "configure",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            f"cmake --build . --parallel {self.num_jobs}",
            "Build failed",
            ["cmake", "--build", ".", f"--parallel={self.num_jobs}"],
            "build",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake --install .",
            "Install failed",
            ["cmake", "--install", "."],
            "install",
            source_dir,
            env,
        )

    def build_libvmaf(self, component: Component, source_dir: Path) -> None:
        """Build libvmaf.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        libvmaf_dir = source_dir / "libvmaf"
        if not libvmaf_dir.exists():
            libvmaf_dir = source_dir

        build_dir = libvmaf_dir / "build"
        build_dir.mkdir(parents=True, exist_ok=True)

        libvmaf_cuda_enabled = (
            self.config.enable_libvmaf_cuda
            and self.platform_detector.platform_info.libvmaf_cuda_supported
        )
        libvmaf_cuda_reason = self.platform_detector.platform_info.libvmaf_cuda_reason
        if self.on_log is not None:
            if libvmaf_cuda_enabled:
                self.on_log("libvmaf CUDA path enabled")
            else:
                self.on_log(f"libvmaf CUDA path disabled: {libvmaf_cuda_reason}")

        if libvmaf_cuda_enabled and self.config.openmp:
            # nvcc does not accept raw -fopenmp and fails with:
            # "nvcc fatal : Unknown option '-fopenmp'".
            # Keep OpenMP for host compilation by forwarding it via -Xcompiler.
            env["CFLAGS"] = self._remove_compiler_flag(env.get("CFLAGS", ""), "-fopenmp")
            env["CXXFLAGS"] = self._remove_compiler_flag(env.get("CXXFLAGS", ""), "-fopenmp")
            env["LDFLAGS"] = self._remove_compiler_flag(env.get("LDFLAGS", ""), "-fopenmp")
            nvcc_flag = "-Xcompiler=-fopenmp"
            nvcc_tokens = env.get("NVCC_PREPEND_FLAGS", "").split()
            if nvcc_flag not in nvcc_tokens:
                env["NVCC_PREPEND_FLAGS"] = " ".join([nvcc_flag, *nvcc_tokens]).strip()

        meson_args = [
            "meson",
            "setup",
            "build",
            f"--prefix={self._ws_str()}",
            "--buildtype=release",
            "--default-library=static",
            f"--libdir={self._ws_str()}/lib",
        ]
        if libvmaf_cuda_enabled:
            meson_args.append("-Denable_cuda=true")

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "meson setup build",
            "Configure failed",
            meson_args,
            "configure",
            libvmaf_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "ninja -C build",
            "Build failed",
            ["ninja", "-C", "build"],
            "build",
            libvmaf_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "ninja install",
            "Install failed",
            ["ninja", "-C", "build", "install"],
            "install",
            libvmaf_dir,
            env,
        )

    def build_srt(self, component: Component, source_dir: Path) -> None:
        """Build srt.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)
        env["OPENSSL_ROOT_DIR"] = self._ws_str()
        env["OPENSSL_LIB_DIR"] = f"{self._ws_str()}/lib"
        env["OPENSSL_INCLUDE_DIR"] = f"{self._ws_str()}/include"

        cmake_args = [
            f"-DCMAKE_INSTALL_PREFIX={self._ws_str()}",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DCMAKE_INSTALL_BINDIR=bin",
            "-DCMAKE_INSTALL_INCLUDEDIR=include",
            "-DENABLE_SHARED=OFF",
            "-DENABLE_STATIC=ON",
            "-DENABLE_APPS=OFF",
            "-DUSE_STATIC_LIBSTDCXX=ON",
        ]

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "cmake .",
            "Configure failed",
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
            "configure",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "cmake --build",
            "Build failed",
            ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
            "build",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake --install",
            "Install failed",
            ["cmake", "--install", "."],
            "install",
            source_dir,
            env,
        )

        if self.config.full_static and self.platform == "linux":
            srt_pc = self.workspace / "lib" / "pkgconfig" / "srt.pc"
            if srt_pc.exists():
                content = srt_pc.read_text()
                content = content.replace("-lgcc_s", "-lgcc_eh")
                srt_pc.write_text(content)

    def build_libzmq(self, component: Component, source_dir: Path) -> None:
        """Build libzmq.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        if self.platform == "darwin":
            env["XML_CATALOG_FILES"] = "/usr/local/etc/xml/catalog"

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            ["./configure", f"--prefix={self._ws_str()}", "--disable-shared", "--enable-static"],
            "configure",
            source_dir,
            env,
        )

        proxy_cpp = source_dir / "src" / "proxy.cpp"
        if proxy_cpp.exists():
            content = proxy_cpp.read_text()
            old_init = "stats_proxy stats = {0}"
            new_init = "stats_proxy stats = {{{0, 0}, {0, 0}}, {{0, 0}, {0, 0}}}"
            content = content.replace(old_init, new_init)
            proxy_cpp.write_text(content)
            self._assert_patch_absent(
                component, proxy_cpp, old_init, "libzmq proxy.cpp stats_proxy initializer"
            )

        self._run_make(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            source_dir,
            self.num_jobs,
            env,
        )

        self._run_install(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            source_dir,
            env,
        )

        # On Windows (UCRT64/MinGW), zmq.h uses __declspec(dllimport) by
        # default which causes undefined reference to __imp_* symbols when
        # linking against the static library.  Adding -DZMQ_STATIC to Cflags
        # suppresses dllimport.  Also, libzmq autotools does not add Windows
        # socket libraries to Libs.private; add them here.
        if self._is_windows_ucrt64_backend():
            pc_file = self.workspace / "lib" / "pkgconfig" / "libzmq.pc"
            if pc_file.exists():
                text = pc_file.read_text()
                if "-DZMQ_STATIC" not in text:
                    text = text.replace(
                        "Cflags: -I${includedir}",
                        "Cflags: -I${includedir} -DZMQ_STATIC",
                    )
                for win_lib in ("-lws2_32", "-lrpcrt4"):
                    if win_lib not in text:
                        text = text.replace(
                            "Libs.private:",
                            f"Libs.private: {win_lib}",
                        )
                pc_file.write_text(text)

    def build_libplacebo(self, component: Component, source_dir: Path) -> None:
        """Build libplacebo with optional Vulkan acceleration.

        Vulkan support is enabled when all three conditions hold:
          - config.enable_libplacebo_vulkan is True
          - platform_info.vulkan_available is True
          - NOT (full_static AND Linux) — no static libvulkan.so exists on Linux

        On macOS the system Vulkan ICD loader (libvulkan.dylib from the LunarG
        SDK) is used; no static restriction applies.  LIBRARY_PATH and
        PKG_CONFIG_PATH are extended to include system Vulkan locations so that
        Meson's dependency('vulkan') probe succeeds.

        Uses a custom build path to set LIBRARY_PATH so Meson's
        cxx.find_library() prefers the workspace-built static glslang/SPIRV
        archives over any system-installed shared variants.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        build_dir = source_dir / "build"
        if build_dir.exists():
            _rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        env = self.get_build_env(component)
        self._prepend_python_module_parent_to_pythonpath(env, "jinja2")

        result, log_file = self.executor.execute_with_log(
            ["python3", "-c", "import jinja2"],
            component.name,
            "check-jinja2",
            source_dir,
            env,
        )
        if not result.success:
            raise BuildError(
                component.name,
                "Missing Python module 'jinja2' required by libplacebo "
                "(install project deps via `pip install -e .` or distro package `python3-jinja2`)",
                log_file,
            )

        # Determine whether to enable Vulkan inside libplacebo.
        pi = self.platform_detector.platform_info
        vulkan_ok = (
            self.config.enable_libplacebo_vulkan
            and pi.vulkan_available
            and not (self.config.full_static and self.platform == "linux")
        )

        ws = self._ws_str()

        # libplacebo's tarball (GitHub archive) does not include git submodules,
        # so 3rdparty/fast_float/include/ is an empty directory.  Populate it
        # from workspace/include/fast_float/ (installed by the fast-float
        # component) before calling meson, so that meson's fs.is_dir() check
        # in src/meson.build succeeds and the header is added to inc_dirs.
        fast_float_submod = source_dir / "3rdparty" / "fast_float" / "include" / "fast_float"
        fast_float_ws = self.workspace / "include" / "fast_float"
        if fast_float_ws.exists() and not any(
            fast_float_submod.iterdir() if fast_float_submod.exists() else iter([])
        ):
            fast_float_submod.mkdir(parents=True, exist_ok=True)
            for item in fast_float_ws.iterdir():
                dest = fast_float_submod / item.name
                if not dest.exists():
                    shutil.copy2(item, dest)

        existing_lp = env.get("LIBRARY_PATH", "")
        env["LIBRARY_PATH"] = self._merge_path_list(
            existing_lp,
            [f"{ws}/lib", f"{ws}/lib64"],
            ":",
        )

        if self.platform == "darwin" and vulkan_ok:
            # LunarG SDK installs to /usr/local/lib; extend PKG_CONFIG_PATH so
            # meson's dependency('vulkan') probe via pkg-config succeeds.
            existing_pkgcp = env.get("PKG_CONFIG_PATH", "")
            vulkan_pkgcp = "/usr/local/lib/pkgconfig"
            env["PKG_CONFIG_PATH"] = self._merge_path_list(
                existing_pkgcp,
                [f"{ws}/lib/pkgconfig", f"{ws}/lib64/pkgconfig", vulkan_pkgcp],
                ":",
            )

        if self._is_windows_ucrt64_backend():
            existing_pkgcp = self._normalize_pkg_config_path_for_windows(
                env.get("PKG_CONFIG_PATH", "")
            )
            env["PKG_CONFIG_PATH"] = self._merge_path_list(
                existing_pkgcp,
                [f"{ws}/lib/pkgconfig", f"{ws}/lib64/pkgconfig"],
                ";",
            )

        if vulkan_ok:
            # Patch libplacebo's src/glsl/meson.build so that glslang is found
            # reliably regardless of platform or compiler toolchain.
            #
            # Root cause: meson's find_library() with static:true performs a
            # FILE SEARCH (not a linker test).  It searches only dirs: param +
            # compiler system dirs (from clang -print-search-dirs).  LDFLAGS,
            # LIBRARY_PATH and cpp_link_args are completely ignored for static
            # library detection.
            #
            # libplacebo passes dirs:vulkan_lib_dirs to find_library('SPIRV')
            # (which is why SPIRV is found when -Dvulkan-sdk={workspace} is set),
            # but the immediately following find_library('glslang') call is
            # missing dirs: -- an oversight in the upstream meson.build.
            #
            # The fix: add dirs:vulkan_lib_dirs to the glslang call, mirroring
            # the SPIRV call on the preceding line.  This is the same variable
            # already defined in the file; no new logic is introduced.
            #
            self._patch_libplacebo_glslang_search(source_dir)

        meson_args = [arg.replace("{workspace}", ws) for arg in component.configure_args]

        if vulkan_ok:
            # -Dvulkan-sdk tells libplacebo where to find SPIRV/glslang static
            # libs; vulkan_lib_dirs = [vulkan-sdk/lib] is added to dirs: in the
            # find_library() searches.  -Dglslang is only meaningful with Vulkan.
            meson_args += [
                "-Dvulkan=enabled",
                f"-Dvulkan-sdk={ws}",
                "-Dglslang=enabled",
            ]
        else:
            meson_args += [
                "-Dvulkan=disabled",
                "-Dglslang=disabled",
            ]

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "meson setup build",
            "Meson configure failed",
            ["meson", "setup", "build"] + meson_args,
            "configure",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "ninja -C build",
            "Build failed",
            ["ninja", "-C", "build"],
            "build",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "ninja install",
            "Install failed",
            ["ninja", "-C", "build", "install"],
            "install",
            source_dir,
            env,
        )

        self._patch_libplacebo_pc()

    def _patch_libplacebo_glslang_search(self, source_dir: Path) -> None:
        """Patch libplacebo's glslang lookup to honor Vulkan SDK library dirs."""
        glsl_meson = source_dir / "src" / "glsl" / "meson.build"
        if not glsl_meson.exists():
            return

        original = "cxx.find_library('glslang', required: required, static: static)"
        patched = (
            "cxx.find_library('glslang', required: required, static: static, dirs: vulkan_lib_dirs)"
        )
        text = glsl_meson.read_text(encoding="utf-8")
        if patched in text:
            return
        if original not in text:
            raise BuildError(
                "libplacebo",
                f"Expected glslang lookup pattern not found in {glsl_meson}",
            )
        glsl_meson.write_text(text.replace(original, patched, 1), encoding="utf-8")

    def _patch_libplacebo_pc(self) -> None:
        """Normalize libplacebo pkg-config metadata for FFmpeg probing.

        1. Rewrite absolute SPIRV/glslang archive/import-library paths to -l*
           flags so FFmpeg configure treats them as linker inputs.
        2. Ensure SPIRV-Tools transitive static deps are present.
        """
        pc_candidates = [
            self.workspace / "lib" / "pkgconfig" / "libplacebo.pc",
            self.workspace / "lib64" / "pkgconfig" / "libplacebo.pc",
        ]
        multiarch = self.platform_detector.get_multiarch_dir()
        if multiarch:
            pc_candidates.append(self.workspace / "lib" / multiarch / "pkgconfig" / "libplacebo.pc")

        for pc_file in pc_candidates:
            if not pc_file.exists():
                continue
            text = pc_file.read_text(encoding="utf-8")
            if self._is_windows_ucrt64_backend():
                text = re.sub(
                    r"\s+\S+[/\\]lib([A-Za-z0-9_-]+)\.dll\.a",
                    lambda m: f" -l{m.group(1)}",
                    text,
                )
            text = re.sub(
                r"\s+\S+[/\\]lib([A-Za-z0-9_+-]+)\.a",
                lambda m: f" -l{m.group(1)}",
                text,
            )
            # Ensure SPIRV-Tools transitive deps are present and ordered for
            # static linking: libSPIRV-Tools-opt depends on libSPIRV-Tools.
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if not line.startswith("Libs: "):
                    continue
                tokens = line[len("Libs: ") :].split()
                if "-lglslang" not in tokens:
                    continue
                tokens = [t for t in tokens if t not in ("-lSPIRV-Tools-opt", "-lSPIRV-Tools")]
                insert_at = tokens.index("-lglslang") + 1
                tokens[insert_at:insert_at] = ["-lSPIRV-Tools-opt", "-lSPIRV-Tools"]
                lines[i] = "Libs: " + " ".join(tokens)
                break
            text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            pc_file.write_text(text, encoding="utf-8")

    def build_glslang(self, component: Component, source_dir: Path) -> None:
        """Build glslang.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        result, log_file = self.executor.execute_with_log(
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
            f"-DCMAKE_INSTALL_PREFIX={self._ws_str()}",
        ]

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "cmake .",
            "Configure failed",
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
            "configure",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "cmake --build",
            "Build failed",
            ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
            "build",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake --install",
            "Install failed",
            ["cmake", "--install", "."],
            "install",
            source_dir,
            env,
        )

    def build_ninja(self, component: Component, source_dir: Path) -> None:
        """Build ninja build system from source.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            "./configure.py --bootstrap",
            "Bootstrap failed",
            ["./configure.py", "--bootstrap"],
            "bootstrap",
            source_dir,
            env,
        )

        ninja_bin = source_dir / "ninja"
        dest = self.workspace / "bin" / "ninja"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ninja_bin, dest)

    def build_meson(self, component: Component, source_dir: Path) -> None:
        """Install meson from source when the system package is unavailable."""
        env = self.get_build_env(component)
        python_bin = shutil.which("python3", path=env.get("PATH")) or shutil.which(
            "python", path=env.get("PATH")
        )
        if python_bin is None:
            raise BuildError(component.name, "Python interpreter is required to build meson")

        setup_py = source_dir / "setup.py"
        if setup_py.exists():
            self._run_step(
                component,
                ComponentStatus.INSTALLING,
                "python setup.py install",
                "Meson install failed",
                [python_bin, "setup.py", "install", f"--prefix={self._ws_str()}"],
                "install",
                source_dir,
                env,
            )
            return

        meson_py = source_dir / "meson.py"
        if meson_py.exists():
            self._run_step(
                component,
                ComponentStatus.CONFIGURING,
                "python meson.py setup build",
                "Meson bootstrap configure failed",
                [python_bin, "meson.py", "setup", "build", f"--prefix={self._ws_str()}"],
                "configure",
                source_dir,
                env,
            )
            self._run_step(
                component,
                ComponentStatus.INSTALLING,
                "python meson.py install -C build",
                "Meson bootstrap install failed",
                [python_bin, "meson.py", "install", "-C", "build"],
                "install",
                source_dir,
                env,
            )
            return

        raise BuildError(
            component.name,
            "Meson source archive does not contain setup.py or meson.py bootstrap entrypoint",
        )

    def build_ffmpeg(self, component: Component, source_dir: Path) -> None:
        """Build FFmpeg.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        built_components = [
            name
            for name, state in self.state_manager.get().components.items()
            if state.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM)
        ]

        # Resume builds may skip libplacebo rebuild. Re-apply pkg-config
        # normalization so FFmpeg's configure probe remains stable.
        if "libplacebo" in built_components:
            self._patch_libplacebo_pc()

        extra_libs = self.extralibs
        extra_ldflags = self.ldflags

        # Add libraries conditionally based on built components
        if "libvmaf" in built_components:
            if self.platform == "darwin":
                extra_libs += " -lc++"
            else:
                extra_libs += " -lstdc++"

        if "libjxl" in built_components:
            # libjxl_threads.a uses std::thread and omits the C++ runtime from
            # its static pkg-config metadata.
            # lcms2 is a private dependency of libjxl not listed in Libs:.
            extra_libs += " -llcms2"
            extra_libs += " -lc++" if self.platform == "darwin" else " -lstdc++"

        # libplacebo links against the system Vulkan ICD loader at runtime.
        # On Linux the loader is libvulkan.so; it must appear in extralibs so
        # the static FFmpeg binary resolves Vulkan symbols at link time.
        # On macOS the loader is libvulkan.dylib (LunarG SDK in /usr/local/lib);
        # -L/usr/local/lib is added so the linker finds it.
        # On Windows UCRT64 the loader (vulkan-1.dll) is auto-discovered via
        # pkg-config Libs, so no extra flag is needed there.
        if "libplacebo" in built_components:
            pi = self.platform_detector.platform_info
            placebo_vulkan = (
                self.config.enable_libplacebo_vulkan
                and pi.vulkan_available
                and not (self.config.full_static and self.platform == "linux")
            )
            if placebo_vulkan:
                if self.platform == "linux":
                    extra_libs += " -lvulkan"
                elif self.platform == "darwin":
                    extra_libs += " -L/usr/local/lib -lvulkan"
                    # libvulkan.1.dylib is a shared library; dyld must be able
                    # to find it at runtime via @rpath.  Add the LunarG SDK lib
                    # directory so the compiler test executable doesn't crash
                    # with "Library not loaded: @rpath/libvulkan.1.dylib".
                    extra_ldflags += " -Wl,-rpath,/usr/local/lib"

        # Strip leading/trailing whitespace that accumulates when starting from "".
        extra_libs = extra_libs.strip()

        configure_flags = self.registry.get_ffmpeg_configure_flags(
            built_components,
            self.config.gpl_enabled,
            self.platform,
            self.platform_detector.platform_info,
            self.config.ffmpeg_version,
        )

        configure_args = [
            "--disable-debug",
            "--disable-shared",
            "--enable-static",
            "--enable-version3",
            f"--extra-cflags={self.cflags}",
            f"--extra-ldexeflags={self.ldexeflags}",
            f"--extra-ldflags={extra_ldflags}",
            f"--extra-libs={extra_libs}",
            # FFmpeg's configure is a POSIX shell script (runs via sh.exe).
            # Use MSYS-style path so bash doesn't misinterpret drive letters.
            f"--pkgconfigdir={self._to_msys_path(self._ws_str())}/lib/pkgconfig",
            "--pkg-config-flags=--static",
            f"--prefix={self._ws_str()}",
        ]
        if "CC" in env:
            configure_args.append(f"--cc={env['CC']}")
        if "CXX" in env:
            configure_args.append(f"--cxx={env['CXX']}")

        # On UCRT64/MinGW, POSIX pthreads are not available as a system
        # library; use native Windows threads (w32threads) instead.
        if self._is_windows_ucrt64_backend():
            configure_args.append("--enable-w32threads")
        else:
            configure_args.append("--enable-pthreads")

        if self.config.gpl_enabled:
            configure_args.append("--enable-gpl")
            configure_args.append("--enable-nonfree")

        # CUDA support
        if self.platform_detector.platform_info.cuda_available:
            # cuda-nvcc requires MSVC cl.exe on Windows which is not available
            # in the MSYS2 UCRT64 toolchain.  Hardware encode/decode APIs
            # (cuvid/nvdec/nvenc) and ffnvcodec headers work fine with GCC.
            if not self._is_windows_ucrt64_backend():
                configure_args.append("--enable-cuda-nvcc")
                configure_args.append("--enable-cuda-llvm")
                cuda_cc = os.environ.get("CUDA_COMPUTE_CAPABILITY")
                if not cuda_cc:
                    cuda_cc = self.platform_detector.platform_info.cuda_compute_capability
                if not cuda_cc:
                    cuda_cc = "52"
                configure_args.append(
                    f"--nvccflags=-gencode arch=compute_{cuda_cc},code=sm_{cuda_cc} -O2"
                )
            configure_args.append("--enable-cuvid")
            configure_args.append("--enable-nvdec")
            configure_args.append("--enable-nvenc")
            configure_args.append("--enable-ffnvcodec")
        else:
            configure_args.append("--disable-ffnvcodec")

        # VAAPI support (Linux-only policy in current implementation).
        if (
            self.platform == "linux"
            and self.platform_detector.platform_info.vaapi_available
            and not self.config.full_static
        ):
            configure_args.append("--enable-vaapi")

        qsv_available = self.platform_detector.platform_info.qsv_available
        windows_ucrt64_qsv = self._is_windows_ucrt64_backend() and qsv_available

        # Intel QSV support.
        if self.platform == "linux" and qsv_available:
            configure_args.append("--enable-libvpl")
        if windows_ucrt64_qsv:
            configure_args.append("--enable-libvpl")

        # Keep Linux/macOS-only acceleration paths disabled on Windows.
        if self.platform == "windows":
            configure_args.append("--disable-vaapi")
            configure_args.append("--disable-videotoolbox")
            if not windows_ucrt64_qsv:
                configure_args.append("--disable-libvpl")

        if self.platform == "darwin":
            configure_args.append(f"--extra-version={component.version}")

        configure_args.extend(configure_flags)

        self._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            ["./configure"] + configure_args,
            "configure",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.BUILDING,
            f"make -j{self.num_jobs}",
            "Build failed",
            ["make", f"-j{self.num_jobs}"],
            "build",
            source_dir,
            env,
        )

        self._run_step(
            component,
            ComponentStatus.INSTALLING,
            "make install",
            "Install failed",
            ["make", "install"],
            "install",
            source_dir,
            env,
        )

    def make_release_bundle(self) -> Path:
        """Create a redistributable release directory for built FFmpeg binaries."""
        return create_release_bundle(self)
