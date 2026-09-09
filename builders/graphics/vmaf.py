"""libvmaf component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libvmaf(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvmaf."""
    env = builder.get_build_env(component)

    libvmaf_dir = source_dir / "libvmaf"
    if not libvmaf_dir.exists():
        libvmaf_dir = source_dir

    build_dir = libvmaf_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    libvmaf_cuda_enabled = (
        builder.config.enable_libvmaf_cuda
        and builder.platform_detector.platform_info.libvmaf_cuda_supported
    )
    libvmaf_cuda_reason = builder.platform_detector.platform_info.libvmaf_cuda_reason

    if builder.on_log is not None:
        if libvmaf_cuda_enabled:
            builder.on_log("libvmaf CUDA path enabled")
        else:
            builder.on_log(f"libvmaf CUDA path disabled: {libvmaf_cuda_reason}")

    if libvmaf_cuda_enabled:
        nvcc_tokens = env.get("NVCC_PREPEND_FLAGS", "").split()

        # nvcc's host-compiler version check may reject the system default
        # compiler (e.g. gcc newer than a given CUDA toolkit supports).
        # PlatformDetector's nvcc sanity-check auto-detects a compatible
        # gcc (system versioned binary or conda/mamba env) and stores it
        # in nvcc_ccbin; forward it to every nvcc invocation via -ccbin.
        nvcc_ccbin = getattr(builder.platform_detector.platform_info, "nvcc_ccbin", None)
        if nvcc_ccbin:
            ccbin_flag = f"-ccbin={nvcc_ccbin}"
            if ccbin_flag not in nvcc_tokens:
                nvcc_tokens.append(ccbin_flag)

        if builder.config.openmp:
            # nvcc does not accept raw -fopenmp and fails with:
            # "nvcc fatal : Unknown option '-fopenmp'".
            # Keep OpenMP for host compilation by forwarding it via -Xcompiler.
            env["CFLAGS"] = builder._remove_compiler_flag(env.get("CFLAGS", ""), "-fopenmp")
            env["CXXFLAGS"] = builder._remove_compiler_flag(env.get("CXXFLAGS", ""), "-fopenmp")
            env["LDFLAGS"] = builder._remove_compiler_flag(env.get("LDFLAGS", ""), "-fopenmp")
            nvcc_flag = "-Xcompiler=-fopenmp"
            if nvcc_flag not in nvcc_tokens:
                nvcc_tokens.append(nvcc_flag)

        if nvcc_tokens:
            env["NVCC_PREPEND_FLAGS"] = " ".join(nvcc_tokens).strip()

    meson_args = [
        "meson",
        "setup",
        "build",
        f"--prefix={builder._ws_str()}",
        "--buildtype=release",
        "--default-library=static",
        f"--libdir={builder._ws_str()}/lib",
    ]
    if libvmaf_cuda_enabled:
        meson_args.append("-Denable_cuda=true")

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "meson setup build",
        "Configure failed",
        meson_args,
        "configure",
        libvmaf_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "ninja -C build",
        "Build failed",
        ["ninja", "-C", "build"],
        "build",
        libvmaf_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "ninja install",
        "Install failed",
        ["ninja", "-C", "build", "install"],
        "install",
        libvmaf_dir,
        env,
    )
