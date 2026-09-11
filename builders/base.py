"""Shared context and standard build-system entry points."""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Type

from ..build_steps import BuildStepContext, run_install, run_make, run_step
from ..build_types import SkipComponent
from ..executor import CommandExecutor
from ..state import ComponentStatus, StateManager

if TYPE_CHECKING:
    from ..builder import FFmpegBuilder
    from ..components import Component
    from ..platforms.base import BasePlatformStrategy

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


@dataclass(frozen=True)
class ComponentBuildContext(BuildStepContext):
    """Dependencies exposed to modular component builders."""

    builder: "FFmpegBuilder"
    platform_strategy: Optional["BasePlatformStrategy"]
    executor: CommandExecutor
    state_manager: StateManager

    @classmethod
    def from_builder(cls, builder: "FFmpegBuilder") -> "ComponentBuildContext":
        return cls(
            builder=builder,
            platform_strategy=builder.platform_strategy,
            executor=builder.executor,
            state_manager=builder.state_manager,
        )


def build_autotools(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    """Build component with autotools."""
    build_dir = source_dir
    if component.workdir:
        build_dir = source_dir / component.workdir

    env = context.builder.get_build_env(component)

    configure_args = [
        arg.replace("{workspace}", context.builder._ws_str()).replace(
            "{num_jobs}", str(context.builder.num_jobs)
        )
        for arg in component.configure_args
    ]

    # Apply platform-specific configure_args_override if present.
    if context.builder.platform in component.platform_overrides:
        override = component.platform_overrides[context.builder.platform]
        if override.configure_args_override is not None:
            configure_args = [
                arg.replace("{workspace}", context.builder._ws_str()).replace(
                    "{num_jobs}", str(context.builder.num_jobs)
                )
                for arg in override.configure_args_override
            ]

    run_step(
        context,
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure"] + configure_args,
        "configure",
        build_dir,
        env,
    )

    run_make(
        context,
        component,
        ComponentStatus.BUILDING,
        f"make -j{context.builder.num_jobs}",
        "Build failed",
        build_dir,
        context.builder.num_jobs,
        env,
    )

    run_install(
        context,
        component,
        ComponentStatus.INSTALLING,
        "make install",
        "Install failed",
        build_dir,
        env,
    )


def build_cmake(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    """Build component with CMake."""
    build_dir = source_dir
    if component.workdir:
        build_dir = source_dir / component.workdir
        build_dir.mkdir(parents=True, exist_ok=True)

    cmake_args = [
        arg.replace("{workspace}", context.builder._ws_str()) for arg in component.configure_args
    ]

    if component.name == "opencl-icd-loader":
        # OpenCL-ICD-Loader's CMakeLists.txt adds its test/ subdirectory
        # whenever built as the top-level project (CMAKE_PROJECT_NAME ==
        # PROJECT_NAME, always true for our standalone build) regardless of
        # its own OPENCL_ICD_LOADER_BUILD_TESTING option, because that
        # option is OR'd with the top-level-project check; the include(CTest)
        # call it makes defaults BUILD_TESTING to ON, so the guard's AND
        # BUILD_TESTING clause doesn't help either. Its test targets also
        # require PIC static libraries (they link libOpenCL.a into shared
        # test modules) which this project's static-only build doesn't
        # provide, and some need GL/EGL headers not present in a headless
        # build environment. We only need the OpenCL library itself, not
        # its test suite, so disable CTest's BUILD_TESTING outright to skip
        # that subdirectory.
        cmake_args = [*cmake_args, "-DBUILD_TESTING=OFF"]

    # Honour config.openmp: replace WITH_OPENMP:bool=off → on when
    # OpenMP is enabled (e.g. soxr exposes this CMake option).
    if context.builder.config.openmp:
        cmake_args = [
            arg.replace("-DWITH_OPENMP:bool=off", "-DWITH_OPENMP:bool=on") for arg in cmake_args
        ]

    env = context.builder.get_build_env(component)

    if component.name == "opencl-icd-loader":
        # The project's global CFLAGS include "-I<cuda>/include" so
        # CUDA-aware components (nvenc, libvmaf's CUDA path, ...) can find
        # cuda_runtime.h. The CUDA SDK also ships its own bundled, older
        # CL/cl.h under that same include dir. Because CMake's imported
        # OpenCLHeaders target adds our freshly-built opencl-headers as an
        # -isystem path (deduplicated against the plain -I<workspace>/include
        # our global CFLAGS also add), that -isystem entry loses its
        # earlier position and ends up searched after all plain -I dirs,
        # so the stale CUDA-bundled cl.h/cl_version.h (older
        # CL_TARGET_OPENCL_VERSION default, missing newer macros) shadows
        # our opencl-headers install. This component doesn't use CUDA at
        # all, so simply drop the CUDA include path for its build.
        cuda_path = context.builder.platform_detector.platform_info.cuda_path
        if cuda_path:
            cuda_home = Path(cuda_path).parent.parent
            cuda_include_flag = f"-I{cuda_home}/include"
            for key in ("CFLAGS", "CXXFLAGS"):
                if key in env:
                    env[key] = context.builder._remove_compiler_flag(env[key], cuda_include_flag)

    if context.builder._is_windows_ucrt64_backend():
        # CMake calls pkg-config.EXE directly; needs Windows-style paths.
        ws = context.builder._ws_str()
        env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

    cmake_cmd = ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + [str(source_dir)]
    run_step(
        context,
        component,
        ComponentStatus.CONFIGURING,
        "cmake <source>",
        "CMake configure failed",
        cmake_cmd,
        "configure",
        build_dir,
        env,
    )

    run_step(
        context,
        component,
        ComponentStatus.BUILDING,
        "cmake --build",
        "Build failed",
        ["cmake", "--build", ".", "--parallel", str(context.builder.num_jobs)],
        "build",
        build_dir,
        env,
    )

    run_step(
        context,
        component,
        ComponentStatus.INSTALLING,
        "cmake --install",
        "Install failed",
        ["cmake", "--install", "."],
        "install",
        build_dir,
        env,
    )


def build_meson(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    """Build component with Meson."""
    build_dir = source_dir / "build"
    if build_dir.exists():
        _rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    meson_args = [
        arg.replace("{workspace}", context.builder._ws_str()) for arg in component.configure_args
    ]

    env = context.builder.get_build_env(component)

    if context.builder._is_windows_ucrt64_backend():
        # Meson calls pkg-config.EXE directly (not through bash), so it
        # needs Windows-style paths (E:/...) with ';' as separator.
        ws = context.builder._ws_str()
        env["PKG_CONFIG_PATH"] = f"{ws}/lib/pkgconfig;{ws}/lib64/pkgconfig"

    run_step(
        context,
        component,
        ComponentStatus.CONFIGURING,
        "meson setup build",
        "Meson configure failed",
        ["meson", "setup", "build"] + meson_args,
        "configure",
        source_dir,
        env,
    )

    run_step(
        context,
        component,
        ComponentStatus.BUILDING,
        "ninja -C build",
        "Build failed",
        ["ninja", "-C", "build"],
        "build",
        source_dir,
        env,
    )

    run_step(
        context,
        component,
        ComponentStatus.INSTALLING,
        "ninja install",
        "Install failed",
        ["ninja", "-C", "build", "install"],
        "install",
        source_dir,
        env,
    )


def build_make_only(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    """Build component with make only."""
    build_dir = source_dir
    if component.workdir:
        build_dir = source_dir / component.workdir

    env = context.builder.get_build_env(component)

    build_args = [
        arg.replace("{workspace}", context.builder._ws_str()) for arg in component.build_args
    ]

    run_step(
        context,
        component,
        ComponentStatus.BUILDING,
        f"make -j{context.builder.num_jobs} {' '.join(build_args)}",
        "Build failed",
        ["make", f"-j{context.builder.num_jobs}"] + build_args,
        "build",
        build_dir,
        env,
    )

    install_args = [
        arg.replace("{workspace}", context.builder._ws_str()) for arg in component.install_args
    ]

    run_step(
        context,
        component,
        ComponentStatus.INSTALLING,
        f"make install {' '.join(install_args)}",
        "Install failed",
        ["make", "install"] + install_args,
        "install",
        build_dir,
        env,
    )


def get_rustc_version(context: ComponentBuildContext) -> Optional[Tuple[int, int, int]]:
    """Get installed rustc version.

    Returns:
        Tuple of (major, minor, patch) or None if not available.
    """
    env = context.builder.get_build_env()
    result = context.executor.execute(["rustc", "--version"], env=env)
    if not result.success:
        return None
    match = re.search(r"rustc\s+(\d+)\.(\d+)\.(\d+)", result.stdout)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def build_cargo(context: ComponentBuildContext, component: "Component", source_dir: Path) -> None:
    """Build component with Cargo."""
    env = context.builder.get_build_env(component)
    env["RUSTFLAGS"] = "-C target-cpu=native"

    if type(context.platform_strategy).__name__ == "LinuxGcc15Platform" and "CFLAGS" in env:
        env["CFLAGS"] = env["CFLAGS"].replace("-std=c11", "-std=gnu11")

    rustc_version = get_rustc_version(context)
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
        run_step(
            context,
            component,
            ComponentStatus.BUILDING,
            f"cargo install cargo-c --version {CARGO_C_VERSION}",
            "Failed to install cargo-c",
            ["cargo", "install", "cargo-c", "--version", CARGO_C_VERSION],
            "install-cargo-c",
            source_dir,
            env,
        )

    run_step(
        context,
        component,
        ComponentStatus.INSTALLING,
        "cargo cinstall",
        "Cargo build failed",
        [
            "cargo",
            "cinstall",
            f"--prefix={context.builder._ws_str()}",
            "--libdir=lib",
            "--library-type=staticlib",
            "--crt-static",
            "--release",
        ],
        "build",
        source_dir,
        env,
    )


def install_headers_only(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    """Install headers only."""
    if component.name == "VapourSynth":
        dest = context.builder.workspace / "include" / "vapoursynth"
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
        dest = context.builder.workspace / "include" / "fast_float"
        dest.mkdir(parents=True, exist_ok=True)
        src = source_dir / "include" / "fast_float"
        if src.exists():
            for item in src.iterdir():
                dest_item = dest / item.name
                shutil.copy2(item, dest_item)

    elif component.name == "amf":
        dest = context.builder.workspace / "include" / "AMF"
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


def dispatch_component_build(
    context: ComponentBuildContext, component: "Component", source_dir: Path
) -> None:
    """Dispatch a component through custom or standard build entry points."""
    if component.custom_build_fn:
        from ..component_builders import get_custom_builder

        build_fn = get_custom_builder(component.custom_build_fn)
        if build_fn is not None:
            build_fn(context.builder, component, source_dir)
            return

    runners = {
        "autotools": build_autotools,
        "cmake": build_cmake,
        "meson": build_meson,
        "make_only": build_make_only,
        "cargo": build_cargo,
    }
    runner = runners.get(component.build_system.value)
    if runner is None:
        raise ValueError(f"Unknown build system: {component.build_system}")
    runner(context, component, source_dir)
