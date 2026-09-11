"""libvmaf component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def _link_nv_codec_headers_into_source(builder: FFmpegBuilder, libvmaf_dir: Path) -> None:
    """Expose ffnvcodec headers where libvmaf's nvcc custom_target expects them.

    libvmaf's src/meson.build compiles device code (.fatbin) via a
    custom_target whose command is a literal argv list with a hardcoded
    "-I ../include" (relative to libvmaf/build); it does not inherit CFLAGS,
    so our workspace's -I<workspace>/include (which satisfies meson's own
    cc.has_header() probe) never reaches nvcc for that step. Upstream expects
    a git submodule checkout at libvmaf/include/ffnvcodec; a source tarball
    build (as used here) has no submodule, so symlink our just-installed
    headers into that expected location instead.
    """
    dest = libvmaf_dir / "include" / "ffnvcodec"
    src = builder.workspace / "include" / "ffnvcodec"
    if dest.exists() or dest.is_symlink():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src, target_is_directory=True)


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

        _link_nv_codec_headers_into_source(builder, libvmaf_dir)

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
