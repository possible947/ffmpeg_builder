"""libvmaf component builder."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...state import ComponentStatus
from ..base import ComponentBuildContext, dispatch_component_build

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def _ensure_nv_codec_headers(builder: FFmpegBuilder) -> None:
    """Build nv-codec-headers ahead of time if libvmaf's CUDA path needs them.

    components.yaml declares "nv-codec" after "libvmaf", so in a normal
    build-order pass nv-codec-headers are not yet installed into the
    workspace include dir when libvmaf's meson configure runs. This only
    manifests once libvmaf's CUDA path is actually enabled (meson passes
    -Denable_cuda=true, which requires ffnvcodec/dynlink_cuda.h): CUDA
    nvcc-ccbin auto-detection (LinuxGcc15Platform-specific: it lets nvcc
    accept an auto-detected compatible gcc as -ccbin) is what makes
    libvmaf_cuda_supported true in the first place, so this ordering gap is
    scoped to that platform strategy. Building nv-codec-headers here (a
    header-only "make install", no compilation) and marking it completed
    lets the later pass in the main build loop skip it as already done.
    """
    header = builder.workspace / "include" / "ffnvcodec" / "dynlink_cuda.h"
    if header.exists():
        return

    nv_codec = builder.registry.get_nv_codec_component(builder.platform_detector.platform_info)
    if builder.state_manager.is_component_completed(nv_codec.name, nv_codec.version):
        return

    if builder.on_log is not None:
        builder.on_log(
            f"libvmaf CUDA path needs nv-codec-headers; building "
            f"{nv_codec.name} {nv_codec.version} first"
        )

    source_dir = builder.packages / nv_codec.get_target_dir()
    builder._download_and_extract(nv_codec)
    builder._apply_component_patches(nv_codec, source_dir)
    dispatch_component_build(ComponentBuildContext.from_builder(builder), nv_codec, source_dir)
    builder.state_manager.mark_component_status(
        nv_codec.name, ComponentStatus.COMPLETED, nv_codec.version
    )


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


# libvmaf's CUDA feature extractors (src/cuda/*.c) call these CudaFunctions
# members directly; libvmaf's meson.build only probes header *existence*
# (cc.has_header), not these specific symbols, so a too-old nv-codec-headers
# install passes configure but fails at compile time with "has no member
# named ..." errors. The project's nv-codec component is intentionally
# pinned to a release old enough to keep FFmpeg's own nvenc.c NVENC-SDK-13.0
# feature checks working (see components.yaml), and is older than what
# libvmaf 3.2.0 needs.
_LIBVMAF_CUDA_REQUIRED_SYMBOLS = (
    "cuMemHostAlloc",
    "cuMemFreeHost",
    "cuMemFreeAsync",
    "cuCtxGetStreamPriorityRange",
    "cuStreamCreateWithPriority",
    "cuCtxSynchronize",
)


def _libvmaf_cuda_headers_compatible(builder: FFmpegBuilder) -> Optional[str]:
    """Return None if the installed ffnvcodec headers are new enough for
    libvmaf's CUDA feature extractors, otherwise a human-readable reason."""
    loader_header = builder.workspace / "include" / "ffnvcodec" / "dynlink_loader.h"
    if not loader_header.exists():
        return "ffnvcodec/dynlink_loader.h not found"

    text = loader_header.read_text(encoding="utf-8", errors="ignore")
    missing = [s for s in _LIBVMAF_CUDA_REQUIRED_SYMBOLS if not re.search(rf"\b{s}\b", text)]
    if missing:
        return (
            "installed nv-codec-headers is missing CudaFunctions members "
            f"required by libvmaf: {', '.join(missing)}"
        )
    return None


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

    if libvmaf_cuda_enabled and type(builder.platform_strategy).__name__ == "LinuxGcc15Platform":
        _ensure_nv_codec_headers(builder)
        incompatibility_reason = _libvmaf_cuda_headers_compatible(builder)
        if incompatibility_reason is not None:
            libvmaf_cuda_enabled = False
            libvmaf_cuda_reason = incompatibility_reason
        else:
            _link_nv_codec_headers_into_source(builder, libvmaf_dir)

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
