"""Build orchestration engine."""
import os
import re
import shutil
import importlib.util
import json
import stat
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Callable, Tuple, Set
from tqdm import tqdm

from .config import BuildConfig
from .state import StateManager, ComponentStatus
from .components import Component, ComponentRegistry, BuildSystem
from .executor import CommandExecutor, ExecutionResult
from .downloader import AsyncDownloadManager, Downloader
from .platform_detect import PlatformDetector


def _rmtree(path: Path) -> None:
    """Remove a directory tree, handling read-only files on Windows.

    Git repositories mark objects as read-only; shutil.rmtree fails with
    [WinError 5] on Windows without an onerror handler.
    """
    def _on_error(func, fpath, exc_info):
        # Clear the read-only bit and retry.
        try:
            os.chmod(fpath, stat.S_IWRITE)
            func(fpath)
        except Exception:
            pass  # Best-effort; ignore secondary failures.

    shutil.rmtree(path, onerror=_on_error)


class BuildError(Exception):
    """Build error with component context."""

    def __init__(self, component: str, message: str, log_file: Optional[Path] = None):
        """Initialize build error.

        Args:
            component: Component name.
            message: Error message.
            log_file: Path to log file.
        """
        super().__init__(f"{component}: {message}")
        self.component = component
        self.log_file = log_file


class SkipComponent(Exception):
    """Raised when a component should be skipped (not failed)."""

    def __init__(self, component: str, message: str):
        """Initialize skip exception.

        Args:
            component: Component name.
            message: Skip reason.
        """
        super().__init__(f"{component}: {message}")
        self.component = component
        self.message = message


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
        self.source_archives = Path(config.source_archives_dir).absolute()
        self.state_manager = state_manager
        self.platform_detector = platform_detector

        self.executor = CommandExecutor(self.workspace)
        self.downloader = Downloader(
            packages_dir=self.packages,
            source_archives_dir=self.source_archives,
            allow_network_downloads=config.allow_network_downloads,
            on_log=on_log,
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
            Path("/opt/local/lib/libomp"),      # MacPorts libomp runtime
            Path("/opt/local/lib"),             # MacPorts generic lib dir
            Path("/opt/homebrew/opt/libomp/lib"),  # Homebrew on Apple Silicon
            Path("/usr/local/opt/libomp/lib"),     # Homebrew on Intel
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
            pkg_config_paths.extend([
                "/usr/local/lib/pkgconfig",
                "/usr/local/share/pkgconfig",
                "/usr/lib/pkgconfig",
                "/usr/share/pkgconfig",
                "/usr/lib64/pkgconfig",
            ])
            pkg_config_path = ":".join(pkg_config_paths)

        self.env = {
            "PATH": f"{self._ws_str()}/bin:{os.environ.get('PATH', '')}",
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

        # Vulkan paths
        if self.platform_detector.platform_info.vulkan_available:
            self.env["CFLAGS"] = self.cflags
            self.env["LDFLAGS"] = self.ldflags

        # VAAPI paths (required for QSV)
        if self.platform_detector.platform_info.vaapi_available:
            self.env["CFLAGS"] = self.cflags
            self.env["LDFLAGS"] = self.ldflags

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

    def build_all(self, components: List[Component]) -> List[str]:
        """Build all components.

        Args:
            components: List of components to build.

        Returns:
            List of successfully built component names.
        """
        built = []
        total = len(components)

        state = self.state_manager.get()
        state.config = self.config.to_dict()
        state.total_steps = total
        self.state_manager.save()

        with tqdm(total=total, desc="Building FFmpeg", unit="component") as pbar:
            for idx, component in enumerate(components, 1):
                state.current_step = idx
                self.state_manager.save()

                try:
                    self.build_component(component)
                    built.append(component.name)
                    self.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.COMPLETED,
                        component.version,
                    )
                except BuildError as e:
                    self.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.FAILED,
                        component.version,
                        str(e),
                        str(e.log_file) if e.log_file else None,
                    )
                    raise

                pbar.update(1)
                pbar.set_postfix_str(component.name)

        return built

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
            build_fn = getattr(self, component.custom_build_fn, None)
            if build_fn:
                build_fn(component, source_dir)
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

        cmd = component.post_install.replace("{workspace}", self._ws_str())
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
        import subprocess

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
                    ["pkg-config", "--exists", pkg_name],
                    capture_output=True,
                    timeout=5
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
        import shutil

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
                    for member in tar.getmembers():
                        member_path = Path(member.name)
                        if len(member_path.parts) > 1:
                            member.name = str(Path(*member_path.parts[1:]))
                            if member.name:
                                tar.extract(member, target_dir)
                else:
                    tar.extractall(target_dir)
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

        configure_args = [
            arg.replace("{workspace}", self._ws_str())
            .replace("{num_jobs}", str(self.num_jobs))
            for arg in component.configure_args
        ]

        # Apply platform-specific configure_args_override if present.
        if self.platform in component.platform_overrides:
            override = component.platform_overrides[self.platform]
            if override.configure_args_override is not None:
                configure_args = [
                    arg.replace("{workspace}", self._ws_str())
                    .replace("{num_jobs}", str(self.num_jobs))
                    for arg in override.configure_args_override
                ]

        env = self.get_build_env(component)

        result, log_file = self.executor.execute_with_log(
            ["./configure"] + configure_args,
            component.name,
            "configure",
            build_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail=f"make -j{self.num_jobs}",
        )

        result, log_file = self.executor.execute_make(
            build_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="make install",
        )

        result, log_file = self.executor.execute_install(
            build_dir,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
            arg.replace("{workspace}", self._ws_str())
            for arg in component.configure_args
        ]

        # Honour config.openmp: replace WITH_OPENMP:bool=off → on when
        # OpenMP is enabled (e.g. soxr exposes this CMake option).
        if self.config.openmp:
            cmake_args = [
                arg.replace("-DWITH_OPENMP:bool=off", "-DWITH_OPENMP:bool=on")
                for arg in cmake_args
            ]

        env = self.get_build_env(component)

        if self._is_windows_ucrt64_backend():
            # CMake calls pkg-config.EXE directly; needs Windows-style paths.
            ws = self._ws_str()
            env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

        result, log_file = self.executor.execute_with_log(
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + [str(source_dir)],
            component.name,
            "configure",
            build_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "CMake configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="cmake --build",
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
            component.name,
            "build",
            build_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="cmake --install",
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--install", "."],
            component.name,
            "install",
            build_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
            arg.replace("{workspace}", self._ws_str())
            for arg in component.configure_args
        ]

        env = self.get_build_env(component)

        if self._is_windows_ucrt64_backend():
            # Meson calls pkg-config.EXE directly (not through bash), so it
            # needs Windows-style paths (E:/...) with ';' as separator.
            ws = self._ws_str()
            env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

        result, log_file = self.executor.execute_with_log(
            ["meson", "setup", "build"] + meson_args,
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Meson configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="ninja -C build",
        )

        result, log_file = self.executor.execute_with_log(
            ["ninja", "-C", "build"],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="ninja install",
        )

        result, log_file = self.executor.execute_with_log(
            ["ninja", "-C", "build", "install"],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="make -j" + str(self.num_jobs),
        )

        build_args = [
            arg.replace("{workspace}", self._ws_str())
            for arg in component.build_args
        ]

        result, log_file = self.executor.execute_with_log(
            ["make", f"-j{self.num_jobs}"] + build_args,
            component.name,
            "build",
            build_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="make install",
        )

        install_args = [
            arg.replace("{workspace}", self._ws_str())
            for arg in component.install_args
        ]

        result, log_file = self.executor.execute_with_log(
            ["make", "install"] + install_args,
            component.name,
            "install",
            build_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
                component.name,
                "rustc is not available or version cannot be determined"
            )

        if rustc_version < (1, 95, 0):
            raise SkipComponent(
                component.name,
                f"rustc {'.'.join(map(str, rustc_version))} is too old. "
                f"cargo-c requires rustc 1.95 or newer"
            )

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="cargo install cargo-c",
        )

        result, log_file = self.executor.execute_with_log(
            ["cargo", "install", "cargo-c"],
            component.name,
            "install-cargo-c",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Failed to install cargo-c", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="cargo cinstall",
        )

        result, log_file = self.executor.execute_with_log(
            [
                "cargo", "cinstall",
                f"--prefix={self._ws_str()}",
                "--libdir=lib",
                "--library-type=staticlib",
                "--crt-static",
                "--release",
            ],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Cargo build failed", log_file)

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

    def build_giflib(self, component: Component, source_dir: Path) -> None:
        """Build giflib.

        Patches Makefile to skip documentation build (requires ImageMagick).

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        makefile = source_dir / "Makefile"
        if makefile.exists():
            content = makefile.read_text()
            content = content.replace("$(MAKE) -C doc", "")
            content = content.replace(
                "install: all install-bin install-include install-lib install-man",
                "install: all install-bin install-include install-lib"
            )
            makefile.write_text(content)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
        detail='make -jself.num_jobs',
        )

        result, log_file = self.executor.execute_make(
            source_dir,
            1,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
        detail='make install',
        )

        result, log_file = self.executor.execute_with_log(
            ["make", f"PREFIX={self._ws_str()}", "install"],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
            result2 = self.executor.execute(
                ["perl", str(configdata)],
                cwd=source_dir,
                env=env,
            )
            if not result2.success:
                raise BuildError(component.name, "configdata.pm regeneration failed")

        result, log_file = self.executor.execute_make(
            source_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
        detail='make install_sw',
        )

        result, log_file = self.executor.execute_with_log(
            ["make", "install_sw"],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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

        result, log_file = self.executor.execute_with_log(
            ["./configure"] + configure_args,
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail=f"make -j{self.num_jobs}",
        )

        result, log_file = self.executor.execute_make(
            source_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="make install",
        )

        result, log_file = self.executor.execute_install(
            source_dir,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

        result, log_file = self.executor.execute_with_log(
            ["make", "install-lib-static"],
            component.name,
            "install-lib",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install lib-static failed", log_file)

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
                cmake_args.extend([
                    "-DHIGH_BIT_DEPTH=ON",
                    "-DENABLE_HDR10_PLUS=ON",
                    "-DEXPORT_C_API=OFF",
                    "-DENABLE_CLI=OFF",
                    "-DMAIN12=ON",
                ])
            elif bitdepth == "10bit":
                cmake_args.extend([
                    "-DHIGH_BIT_DEPTH=ON",
                    "-DENABLE_HDR10_PLUS=ON",
                    "-DEXPORT_C_API=OFF",
                    "-DENABLE_CLI=OFF",
                ])
            else:
                extra_libs = "x265_main10.a;x265_main12.a"
                if self.platform == "linux":
                    extra_libs += ";-ldl"
                cmake_args.extend([
                    "-DENABLE_SHARED=OFF",
                    "-DBUILD_SHARED_LIBS=OFF",
                    f"-DEXTRA_LIB={extra_libs}",
                    "-DEXTRA_LINK_FLAGS=-L.",
                    "-DLINKED_10BIT=ON",
                    "-DLINKED_12BIT=ON",
                ])

                # Copy 10bit and 12bit libraries into 8bit build dir before linking
                shutil.copy(build_linux / "10bit" / "libx265.a", bitdepth_dir / "libx265_main10.a")
                shutil.copy(build_linux / "12bit" / "libx265.a", bitdepth_dir / "libx265_main12.a")

            result, log_file = self.executor.execute_with_log(
                ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["../../../source"],
                component.name,
                f"configure-{bitdepth}",
                bitdepth_dir,
                env,
            )

            if not result.success:
                raise BuildError(component.name, f"Configure {bitdepth} failed", log_file)

            self.state_manager.mark_component_status(
                component.name,
                ComponentStatus.BUILDING,
                component.version,
                detail='cmake --build (multi-bitdepth)',
            )

            result, log_file = self.executor.execute_with_log(
                ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
                component.name,
                f"build-{bitdepth}",
                bitdepth_dir,
                env,
            )

            if not result.success:
                raise BuildError(component.name, f"Build {bitdepth} failed", log_file)

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
                import subprocess
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

            result, log_file = self.executor.execute_with_log(
                [libtool, "-static", "-o", "libx265.a", "libx265_main.a", "libx265_main10.a", "libx265_main12.a"],
                component.name,
                "merge-libs",
                eight_dir,
                env,
            )
        else:
            m_script = "CREATE libx265.a\nADDLIB libx265_main.a\nADDLIB libx265_main10.a\nADDLIB libx265_main12.a\nSAVE\nEND\n"
            result, log_file = self.executor.execute_with_log(
                ["ar", "-M"],
                component.name,
                "merge-libs",
                eight_dir,
                env,
                stdin=m_script,
            )

        if not result.success:
            raise BuildError(component.name, "Merge libs failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail='cmake --install',
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--install", "."],
            component.name,
            "install",
            eight_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

        if self.config.full_static and self.platform == "linux":
            x265_pc = self.workspace / "lib" / "pkgconfig" / "x265.pc"
            if x265_pc.exists():
                content = x265_pc.read_text()
                content = content.replace("-lgcc_s", "-lgcc_eh")
                x265_pc.write_text(content)

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
                content = content.replace("-Wl,--no-undefined -Wl,-soname", "-Wl,-undefined,error -Wl,-install_name")
                makefile.write_text(content)

        result, log_file = self.executor.execute_with_log(
            [
                "./configure",
                f"--prefix={self._ws_str()}",
                "--disable-unit-tests",
                "--disable-shared",
                "--disable-examples",
                "--as=yasm",
                "--enable-vp9-highbitdepth",
            ],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail=f"make -j{self.num_jobs}",
        )

        result, log_file = self.executor.execute_make(
            source_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="make install",
        )

        result, log_file = self.executor.execute_install(
            source_dir,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

    def build_zimg(self, component: Component, source_dir: Path) -> None:
        """Build zimg.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        import shutil

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
            raise BuildError(component.name, "libtoolize not found (tried libtoolize and glibtoolize)")

        libtoolize_cmd = [libtoolize, "-i", "-f", "-q"]
        if self._is_windows_ucrt64_backend():
            # In MSYS2, /usr/bin/libtoolize is a shell script, not a native
            # Win32 executable. Run it through sh.exe.
            suffix = Path(libtoolize).suffix.lower()
            if suffix not in (".exe", ".bat", ".cmd"):
                sh_path = _resolve_tool_path("sh") or "sh"
                libtoolize_cmd = [sh_path, libtoolize, "-i", "-f", "-q"]

        result, log_file = self.executor.execute_with_log(
            libtoolize_cmd,
            component.name,
            "libtoolize",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Libtoolize failed", log_file)

        result, log_file = self.executor.execute_with_log(
            ["./autogen.sh", f"--prefix={self._ws_str()}"],
            component.name,
            "autogen",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Autogen failed", log_file)

        result, log_file = self.executor.execute_with_log(
            ["./configure", f"--prefix={self._ws_str()}", "--enable-static", "--disable-shared"],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
        detail='make -jself.num_jobs',
        )

        result, log_file = self.executor.execute_make(
            source_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
        detail='make install',
        )

        result, log_file = self.executor.execute_install(
            source_dir,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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

        result, log_file = self.executor.execute_with_log(
            ["./autogen.sh", f"--prefix={self._ws_str()}"],
            component.name,
            "autogen",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Autogen failed", log_file)

        result, log_file = self.executor.execute_with_log(
            [
                "./configure",
                f"--prefix={self._ws_str()}",
                f"--with-ogg-libraries={self._ws_str()}/lib",
                f"--with-ogg-includes={self._ws_str()}/include/",
                "--enable-static",
                "--disable-shared",
                "--disable-oggtest",
            ],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
        detail='make -jself.num_jobs',
        )

        result, log_file = self.executor.execute_make(
            source_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
        detail='make install',
        )

        result, log_file = self.executor.execute_install(
            source_dir,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
            if original in content:
                portable = (
                    'if command -v realpath >/dev/null 2>&1; then\n'
                    '  SELF=$(realpath "$0")\n'
                    'else\n'
                    '  SELF=$(cd -- "$(dirname -- "$0")" && pwd -P)/$(basename -- "$0")\n'
                    'fi'
                )
                deps_script.write_text(content.replace(original, portable, 1))

        result, log_file = self.executor.execute_with_log(
            ["./deps.sh"],
            component.name,
            "deps",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Deps failed", log_file)

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

        result, log_file = self.executor.execute_with_log(
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail=f'cmake --build . --parallel {self.num_jobs}',
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--build", ".", f"--parallel={self.num_jobs}"],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail='cmake --install .',
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--install", "."],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
            "meson", "setup", "build",
            f"--prefix={self._ws_str()}",
            "--buildtype=release",
            "--default-library=static",
            f"--libdir={self._ws_str()}/lib",
        ]
        if libvmaf_cuda_enabled:
            meson_args.append("-Denable_cuda=true")

        result, log_file = self.executor.execute_with_log(
            meson_args,
            component.name,
            "configure",
            libvmaf_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            "ninja -C build",
        )

        result, log_file = self.executor.execute_with_log(
            ["ninja", "-C", "build"],
            component.name,
            "build",
            libvmaf_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            "ninja install",
        )

        result, log_file = self.executor.execute_with_log(
            ["ninja", "-C", "build", "install"],
            component.name,
            "install",
            libvmaf_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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

        result, log_file = self.executor.execute_with_log(
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="cmake --build",
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="cmake --install",
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--install", "."],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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

        result, log_file = self.executor.execute_with_log(
            ["./configure", f"--prefix={self._ws_str()}", "--disable-shared", "--enable-static"],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        proxy_cpp = source_dir / "src" / "proxy.cpp"
        if proxy_cpp.exists():
            content = proxy_cpp.read_text()
            content = content.replace(
                "stats_proxy stats = {0}",
                "stats_proxy stats = {{{0, 0}, {0, 0}}, {{0, 0}, {0, 0}}}"
            )
            proxy_cpp.write_text(content)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            f"make -j{self.num_jobs}",
        )

        result, log_file = self.executor.execute_make(
            source_dir,
            self.num_jobs,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            "make install",
        )

        result, log_file = self.executor.execute_install(
            source_dir,
            env,
            component.name,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

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
        if fast_float_ws.exists() and not any(fast_float_submod.iterdir() if fast_float_submod.exists() else iter([])):
            fast_float_submod.mkdir(parents=True, exist_ok=True)
            for item in fast_float_ws.iterdir():
                dest = fast_float_submod / item.name
                if not dest.exists():
                    shutil.copy2(item, dest)

        existing_lp = env.get("LIBRARY_PATH", "")
        new_lp = f"{ws}/lib:{ws}/lib64"
        env["LIBRARY_PATH"] = f"{new_lp}:{existing_lp}" if existing_lp else new_lp

        if self.platform == "darwin" and vulkan_ok:
            # LunarG SDK installs to /usr/local/lib; extend PKG_CONFIG_PATH so
            # meson's dependency('vulkan') probe via pkg-config succeeds.
            existing_pkgcp = env.get("PKG_CONFIG_PATH", "")
            vulkan_pkgcp = "/usr/local/lib/pkgconfig"
            env["PKG_CONFIG_PATH"] = (
                f"{ws}/lib/pkgconfig:{ws}/lib64/pkgconfig:{vulkan_pkgcp}"
                + (f":{existing_pkgcp}" if existing_pkgcp else "")
            )

        if self._is_windows_ucrt64_backend():
            env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

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
            # missing dirs: — an oversight in the upstream meson.build.
            #
            # The fix: add dirs:vulkan_lib_dirs to the glslang call, mirroring
            # the SPIRV call on the preceding line.  This is the same variable
            # already defined in the file; no new logic is introduced.
            #
            # The patch is applied to the source tree before meson runs and is
            # idempotent (guarded by checking that the original text is present).
            glsl_meson = source_dir / "src" / "glsl" / "meson.build"
            if glsl_meson.exists():
                original = "cxx.find_library('glslang', required: required, static: static)"
                patched  = "cxx.find_library('glslang', required: required, static: static, dirs: vulkan_lib_dirs)"
                text = glsl_meson.read_text(encoding="utf-8")
                if original in text:
                    glsl_meson.write_text(text.replace(original, patched, 1), encoding="utf-8")

        meson_args = [
            arg.replace("{workspace}", ws) for arg in component.configure_args
        ]

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

        result, log_file = self.executor.execute_with_log(
            ["meson", "setup", "build"] + meson_args,
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Meson configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="ninja -C build",
        )

        result, log_file = self.executor.execute_with_log(
            ["ninja", "-C", "build"],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="ninja install",
        )

        result, log_file = self.executor.execute_with_log(
            ["ninja", "-C", "build", "install"],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

        self._patch_libplacebo_pc()

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
            pc_candidates.append(
                self.workspace / "lib" / multiarch / "pkgconfig" / "libplacebo.pc"
            )

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
                tokens = line[len("Libs: "):].split()
                if "-lglslang" not in tokens:
                    continue
                tokens = [
                    t for t in tokens
                    if t not in ("-lSPIRV-Tools-opt", "-lSPIRV-Tools")
                ]
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

        result, log_file = self.executor.execute_with_log(
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail="cmake --build",
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--build", ".", "--parallel", str(self.num_jobs)],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="cmake --install",
        )

        result, log_file = self.executor.execute_with_log(
            ["cmake", "--install", "."],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

    def build_ninja(self, component: Component, source_dir: Path) -> None:
        """Build ninja build system from source.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
        detail='./configure.py --bootstrap',
        )

        result, log_file = self.executor.execute_with_log(
            ["./configure.py", "--bootstrap"],
            component.name,
            "bootstrap",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Bootstrap failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
        detail='install ninja',
        )

        ninja_bin = source_dir / "ninja"
        dest = self.workspace / "bin" / "ninja"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ninja_bin, dest)

    def build_ffmpeg(self, component: Component, source_dir: Path) -> None:
        """Build FFmpeg.

        Args:
            component: Component to build.
            source_dir: Source directory.
        """
        env = self.get_build_env(component)

        built_components = [
            name for name, state in self.state_manager.get().components.items()
            if state.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM)
        ]

        # Resume builds may skip libplacebo rebuild. Re-apply pkg-config
        # normalization so FFmpeg's configure probe remains stable.
        if "libplacebo" in built_components:
            self._patch_libplacebo_pc()

        # Add libraries conditionally based on built components
        if "libvmaf" in built_components:
            if self.platform == "darwin":
                self.extralibs += " -lc++"
            else:
                self.extralibs += " -lstdc++"

        if "libjxl" in built_components:
            # libjxl_threads.a uses std::thread — needs -lstdc++ for static
            # linking on all non-Apple platforms, including MinGW/UCRT64.
            # lcms2 is a private dependency of libjxl not listed in Libs:.
            self.extralibs += " -llcms2"
            if self.platform != "darwin":
                self.extralibs += " -lstdc++"

        if "opencl-icd-loader" in built_components and self.platform == "linux":
            self.extralibs += " -lva"

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
                    self.extralibs += " -lvulkan"
                elif self.platform == "darwin":
                    self.extralibs += " -L/usr/local/lib -lvulkan"
                    # libvulkan.1.dylib is a shared library; dyld must be able
                    # to find it at runtime via @rpath.  Add the LunarG SDK lib
                    # directory so the compiler test executable doesn't crash
                    # with "Library not loaded: @rpath/libvulkan.1.dylib".
                    self.ldflags += " -Wl,-rpath,/usr/local/lib"

        # Strip leading/trailing whitespace that accumulates when starting from "".
        self.extralibs = self.extralibs.strip()

        configure_flags = self.registry.get_ffmpeg_configure_flags(
            built_components,
            self.config.gpl_enabled,
            self.platform,
        )

        configure_args = [
            "--disable-debug",
            "--disable-shared",
            "--enable-static",
            "--enable-version3",
            f"--extra-cflags={self.cflags}",
            f"--extra-ldexeflags={self.ldexeflags}",
            f"--extra-ldflags={self.ldflags}",
            f"--extra-libs={self.extralibs}",
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

        result, log_file = self.executor.execute_with_log(
            ["./configure"] + configure_args,
            component.name,
            "configure",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Configure failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.BUILDING,
            component.version,
            detail='make -j{self.num_jobs}',
        )

        result, log_file = self.executor.execute_with_log(
            ["make", f"-j{self.num_jobs}"],
            component.name,
            "build",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Build failed", log_file)

        self.state_manager.mark_component_status(
            component.name,
            ComponentStatus.INSTALLING,
            component.version,
            detail="make install",
        )

        result, log_file = self.executor.execute_with_log(
            ["make", "install"],
            component.name,
            "install",
            source_dir,
            env,
        )

        if not result.success:
            raise BuildError(component.name, "Install failed", log_file)

    def make_release_bundle(self) -> Path:
        """Create a redistributable release directory for built FFmpeg binaries."""
        backend = self.platform_detector.get_build_backend_name()
        release_dir = self.workspace / "release"

        if release_dir.exists():
            _rmtree(release_dir)

        release_dir.mkdir(parents=True, exist_ok=True)

        source_bin = self.workspace / "bin"
        source_binaries: List[Path] = []
        copied_binaries: List[str] = []
        missing_binaries: List[str] = []

        for name in ("ffmpeg", "ffprobe", "ffplay"):
            candidates = [source_bin / name]
            if self.platform == "windows":
                candidates.insert(0, source_bin / f"{name}.exe")

            source = next((candidate for candidate in candidates if candidate.exists()), None)
            if source is None:
                missing_binaries.append(name)
                continue

            destination = release_dir / source.name
            shutil.copy2(source, destination)
            source_binaries.append(source)
            copied_binaries.append(str(destination))

        if not source_binaries:
            raise BuildError("release", f"No FFmpeg binaries found in {source_bin}")

        dependencies, missing_dependencies = self._collect_runtime_dependencies(source_binaries)
        copied_dependencies: List[str] = []

        for dep in sorted(dependencies, key=lambda item: item.name.lower()):
            destination = release_dir / dep.name
            if destination.exists():
                continue
            shutil.copy2(dep, destination)
            copied_dependencies.append(str(destination))

        manifest = {
            "generated_at": datetime.now().isoformat(),
            "platform": self.platform,
            "build_backend": backend,
            "ffmpeg_version": self.config.ffmpeg_version,
            "binaries": copied_binaries,
            "missing_binaries": sorted(missing_binaries),
            "dependencies": copied_dependencies,
            "missing_dependencies": sorted(missing_dependencies),
        }
        (release_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

        return release_dir

    def _collect_runtime_dependencies(self, binaries: List[Path]) -> Tuple[Set[Path], Set[str]]:
        """Collect recursive runtime dependencies for provided binaries."""
        queue = list(binaries)
        visited: Set[str] = set()
        collected: Set[Path] = set()
        collected_keys: Set[str] = set()
        missing: Set[str] = set()

        while queue:
            current = queue.pop(0).resolve()
            current_key = self._path_key(current)
            if current_key in visited:
                continue
            visited.add(current_key)

            for dep in self._read_runtime_dependencies(current):
                resolved = self._resolve_runtime_dependency(dep, current)
                if resolved is None:
                    missing.add(dep)
                    continue

                resolved = resolved.resolve()
                resolved_key = self._path_key(resolved)
                if self._is_system_runtime_library(resolved):
                    visited.add(resolved_key)
                    continue

                if resolved_key in collected_keys:
                    continue

                collected.add(resolved)
                collected_keys.add(resolved_key)
                queue.append(resolved)

        return collected, missing

    def _read_runtime_dependencies(self, binary_path: Path) -> List[str]:
        """Read direct runtime dependencies for a binary/library."""
        if self.platform == "windows":
            return self._read_windows_dependencies(binary_path)
        if self.platform == "darwin":
            return self._read_macos_dependencies(binary_path)
        return self._read_linux_dependencies(binary_path)

    def _read_windows_dependencies(self, binary_path: Path) -> List[str]:
        """Read runtime dependency DLL names using objdump."""
        result = self.executor.execute(
            ["objdump", "-p", str(binary_path)],
            env=self.get_build_env(),
        )
        if not result.success:
            raise BuildError(
                "release",
                f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
            )

        dependencies: List[str] = []
        for line in result.stdout.splitlines():
            match = re.search(r"DLL Name:\s*(\S+)", line)
            if match:
                dependencies.append(match.group(1).strip())
        return dependencies

    def _read_linux_dependencies(self, binary_path: Path) -> List[str]:
        """Read runtime dependencies using ldd output."""
        result = self.executor.execute(
            ["ldd", str(binary_path)],
            env=self.get_build_env(),
        )
        if not result.success:
            raise BuildError(
                "release",
                f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
            )

        dependencies: List[str] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("linux-vdso"):
                continue
            if "=> not found" in line:
                dependencies.append(line.split("=>", 1)[0].strip())
                continue
            if "=>" in line:
                path = line.split("=>", 1)[1].strip().split(" ", 1)[0].strip()
                if path and path != "not":
                    dependencies.append(path)
                continue
            if line.startswith("/"):
                dependencies.append(line.split(" ", 1)[0].strip())

        return dependencies

    def _read_macos_dependencies(self, binary_path: Path) -> List[str]:
        """Read runtime dependencies using otool -L output."""
        result = self.executor.execute(
            ["otool", "-L", str(binary_path)],
            env=self.get_build_env(),
        )
        if not result.success:
            raise BuildError(
                "release",
                f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
            )

        dependencies: List[str] = []
        for index, raw_line in enumerate(result.stdout.splitlines()):
            if index == 0:
                continue
            dep = raw_line.strip().split(" (", 1)[0].strip()
            if dep:
                dependencies.append(dep)

        return dependencies

    def _resolve_runtime_dependency(self, dep: str, binary_path: Path) -> Optional[Path]:
        """Resolve dependency identifier to a concrete file path."""
        if dep.startswith("@"):
            return self._resolve_macos_dynamic_path(dep, binary_path)

        dep_path = Path(dep)
        if dep_path.is_absolute() and dep_path.exists():
            return dep_path

        if dep_path.parts and not dep_path.is_absolute():
            candidate = (binary_path.parent / dep_path).resolve()
            if candidate.exists():
                return candidate

        dep_name = dep_path.name if dep_path.name else dep
        for root in self._runtime_search_dirs(binary_path):
            candidate = root / dep_name
            if candidate.exists():
                return candidate

        return None

    def _resolve_macos_dynamic_path(self, dep: str, binary_path: Path) -> Optional[Path]:
        """Resolve @loader_path/@executable_path/@rpath paths on macOS."""
        if dep.startswith("@loader_path/"):
            candidate = binary_path.parent / dep[len("@loader_path/"):]
            if candidate.exists():
                return candidate

        if dep.startswith("@executable_path/"):
            candidate = self.workspace / "bin" / dep[len("@executable_path/"):]
            if candidate.exists():
                return candidate

        if dep.startswith("@rpath/"):
            rel = dep[len("@rpath/"):]
            for root in self._runtime_search_dirs(binary_path):
                candidate = root / rel
                if candidate.exists():
                    return candidate

        return None

    def _runtime_search_dirs(self, binary_path: Path) -> List[Path]:
        """Return ordered search locations for runtime dependencies."""
        candidates = [
            binary_path.parent,
            self.workspace / "bin",
            self.workspace / "lib",
            self.workspace / "lib64",
        ]

        if self.platform == "windows":
            msys_root = Path(self.config.windows.msys2_root)
            windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
            candidates.extend([
                msys_root / "ucrt64" / "bin",
                msys_root / "usr" / "bin",
                windows_root / "System32",
                windows_root / "SysWOW64",
            ])
        elif self.platform == "darwin":
            candidates.extend([
                Path("/opt/local/lib"),
                Path("/usr/local/lib"),
            ])

        unique: List[Path] = []
        seen: Set[str] = set()
        for path in candidates:
            key = self._path_key(path)
            if key in seen:
                continue
            seen.add(key)
            if path.exists():
                unique.append(path)
        return unique

    def _is_system_runtime_library(self, lib_path: Path) -> bool:
        """Return True if library belongs to OS/system runtime paths."""
        path = lib_path.resolve()
        workspace = self.workspace.resolve()
        if self._is_under(path, workspace):
            return False

        if self.platform == "windows":
            windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
            if self._is_under(path, windir):
                return True
            return False

        if self.platform == "linux":
            for prefix in ("/lib", "/lib64", "/usr/lib", "/usr/lib64"):
                if self._is_under(path, Path(prefix)):
                    return True
            return False

        if self.platform == "darwin":
            return self._is_under(path, Path("/usr/lib")) or self._is_under(path, Path("/System/Library"))

        return False

    def _is_under(self, child: Path, parent: Path) -> bool:
        """Return True when child path is equal to or nested under parent."""
        child_norm = str(child.resolve()).replace("\\", "/").rstrip("/").lower()
        parent_norm = str(parent.resolve()).replace("\\", "/").rstrip("/").lower()
        return child_norm == parent_norm or child_norm.startswith(f"{parent_norm}/")

    def _path_key(self, path: Path) -> str:
        """Create canonical path key for de-duplication."""
        normalized = str(path.resolve()).replace("\\", "/")
        return normalized.lower() if self.platform == "windows" else normalized
