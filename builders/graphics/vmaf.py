"""libvmaf component builder."""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus
from ...cuda_prerequisites import (
    cuda_build_tree_matches,
    cuda_profile_environment,
    record_cuda_build_tree,
)

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def _link_nv_codec_headers_into_source(
    builder: FFmpegBuilder, libvmaf_dir: Path, src: Path
) -> None:
    """Expose ffnvcodec headers where libvmaf's nvcc custom_target expects them.

    libvmaf's src/meson.build compiles device code (.fatbin) via a
    custom_target whose command is a literal argv list with a hardcoded
    "-I ../include" (relative to libvmaf/build); it does not inherit CFLAGS,
    so Meson's CFLAGS never reach nvcc for that step. Upstream expects a git
    submodule at libvmaf/include/ffnvcodec; the source tarball needs a link to
    the selected driver headers instead.
    """
    dest = libvmaf_dir / "include" / "ffnvcodec"
    if dest.is_symlink():
        if dest.resolve() == src.resolve():
            return
        previous = dest.resolve()
        workspace_headers = (builder.workspace / "include" / "ffnvcodec").resolve()
        private_headers = (libvmaf_dir / ".ffmpeg-builder-cuda-headers").resolve()
        if previous != workspace_headers and not previous.is_relative_to(private_headers):
            raise BuildError("libvmaf", f"Refusing to replace unrelated header link {dest}.")
        dest.unlink()
    elif dest.exists():
        raise BuildError("libvmaf", f"Refusing to replace existing header directory {dest}.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src, target_is_directory=True)


def _libvmaf_cuda_headers(builder: FFmpegBuilder, libvmaf_dir: Path) -> Path:
    """Keep libvmaf's CUDA driver headers independent of the NVENC driver version."""
    default = builder.registry.get_nv_codec_component()
    selected = builder.registry.get_nv_codec_component(builder.platform_detector.platform_info)
    if selected.version == default.version and selected.sha256 == default.sha256:
        return builder.workspace / "include" / "ffnvcodec"
    try:
        archive = builder.downloader.download(
            default.get_url(),
            default.get_archive_filename(),
            expected_sha256=default.sha256,
            show_progress=False,
        )
        private_root = libvmaf_dir / ".ffmpeg-builder-cuda-headers"
        include = private_root / "include"
        destination = include / "ffnvcodec"
        if any(path.is_symlink() for path in (private_root, include, destination)):
            raise ValueError("private CUDA header directory is a symlink")
        destination.mkdir(parents=True, exist_ok=True)
        prefix = f"{default.get_target_dir()}/include/ffnvcodec/"
        copied = set()
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                if not member.name.startswith(prefix):
                    continue
                name = member.name[len(prefix) :]
                if not name or "/" in name or name in (".", "..") or not member.isfile():
                    continue
                source = tar.extractfile(member)
                if source is None:
                    raise OSError(f"Cannot read {member.name} from {archive}")
                target_path = destination / name
                if target_path.is_symlink():
                    raise ValueError(f"private CUDA header {target_path} is a symlink")
                with source, target_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                copied.add(name)
        if not {"dynlink_cuda.h", "dynlink_loader.h"}.issubset(copied):
            raise ValueError(f"{archive} does not contain the CUDA driver headers")
        return destination
    except (OSError, ValueError, tarfile.TarError, RuntimeError) as exc:
        raise BuildError("libvmaf", f"Cannot prepare CUDA driver headers: {exc}") from exc


def build_libvmaf(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvmaf."""
    env = builder.get_build_env(component)

    libvmaf_dir = source_dir / "libvmaf"
    if not libvmaf_dir.exists():
        libvmaf_dir = source_dir

    build_dir = libvmaf_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    inputs = getattr(builder.platform_detector.platform_info, "cuda_build_host_inputs", None)
    libvmaf_cuda_enabled = builder.config.enable_libvmaf_cuda and inputs is not None
    libvmaf_cuda_reason = builder.platform_detector.platform_info.libvmaf_cuda_reason

    if builder.on_log is not None:
        if libvmaf_cuda_enabled:
            builder.on_log("libvmaf CUDA path enabled")
        else:
            builder.on_log(f"libvmaf CUDA path disabled: {libvmaf_cuda_reason}")

    if libvmaf_cuda_enabled:
        assert inputs is not None
        env = cuda_profile_environment(env, inputs)
        assert inputs.compute_architecture is not None
        capability = inputs.compute_architecture.removeprefix("compute_")
        env[
            "NVCC_PREPEND_FLAGS"
        ] += f" -gencode=arch={inputs.compute_architecture},code=sm_{capability}"

        if builder.config.openmp:
            # nvcc does not accept raw -fopenmp and fails with:
            # "nvcc fatal : Unknown option '-fopenmp'".
            # Keep OpenMP for host compilation by forwarding it via -Xcompiler.
            env["CFLAGS"] = builder._remove_compiler_flag(env.get("CFLAGS", ""), "-fopenmp")
            env["CXXFLAGS"] = builder._remove_compiler_flag(env.get("CXXFLAGS", ""), "-fopenmp")
            env["LDFLAGS"] = builder._remove_compiler_flag(env.get("LDFLAGS", ""), "-fopenmp")
            env["NVCC_PREPEND_FLAGS"] += " -Xcompiler=-fopenmp"

        headers = _libvmaf_cuda_headers(builder, libvmaf_dir)
        if headers != builder.workspace / "include" / "ffnvcodec":
            env["CFLAGS"] = f"-I{headers.parent} {env.get('CFLAGS', '')}"
        _link_nv_codec_headers_into_source(builder, libvmaf_dir, headers)

    debug_build = builder.config.libvmaf_debug_build
    meson_args = [
        "meson",
        "setup",
        "build",
        f"--prefix={builder._ws_str()}",
        "--buildtype=debug" if debug_build else "--buildtype=release",
        "--default-library=static",
        f"--libdir={builder._ws_str()}/lib",
        "-Denable_tests=false",
        "-Denable_docs=false",
    ]
    marker = build_dir / ".ffmpeg-builder-cuda-profile.json"
    effective_inputs = inputs if libvmaf_cuda_enabled else None
    if (build_dir / "meson-private" / "coredata.dat").is_file() and not cuda_build_tree_matches(
        marker, effective_inputs
    ):
        meson_args.insert(2, "--wipe")
    if debug_build:
        # --buildtype=debug still defaults to -Og; force -O0 explicitly so no
        # optimization masks or reorders the memory behavior under investigation.
        meson_args.append("-Doptimization=0")
    if libvmaf_cuda_enabled:
        meson_args.append("-Denable_cuda=true")

    if builder.on_log is not None and debug_build:
        builder.on_log("libvmaf debug build enabled: -Doptimization=0, no NDEBUG")

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
    record_cuda_build_tree(marker, effective_inputs)

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
