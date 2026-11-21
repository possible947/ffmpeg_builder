"""libvmaf component builder."""

from __future__ import annotations

import shutil
import tarfile
import tempfile
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus
from .cross_install import promote_staged
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
        workspace_headers = (builder.target_prefix / "include" / "ffnvcodec").resolve()
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
        return builder.target_prefix / "include" / "ffnvcodec"
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


def _adapt_cross_cuda_meson(libvmaf_dir: Path, tools: object, nvcc_version: str = "") -> None:
    """Keep NVCC as a host device-code generator, not a Meson target compiler."""
    path = libvmaf_dir / "src" / "meson.build"
    text = path.read_text(encoding="utf-8")
    marker = "# ffmpeg-builder cross CUDA host tools"
    if marker in text:
        return
    nvcc = getattr(tools, "nvcc", None)
    bin2c = getattr(tools, "bin2c", None)
    version = nvcc_version or getattr(tools, "toolkit_release", None)
    architecture = getattr(tools, "compute_architecture", None)
    flags = getattr(tools, "common_flags", ())
    if not nvcc or not bin2c or not version or not architecture:
        raise BuildError("libvmaf", "Verified CUDA host tools are incomplete.")

    def meson_string(value: object) -> str:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

    old = """        cuda_lang = add_languages('cuda', required : true)
        cuda_compiler = meson.get_compiler('cuda')
        nvcc_exe = find_program('nvcc')"""
    new = (
        f"        {marker}\n"
        f"        nvcc_exe = find_program({meson_string(nvcc)}, native: true)\n"
        f"        cuda_version = {meson_string(version)}"
    )
    replacements = (
        (old, new),
        ("cuda_compiler.version()", "cuda_version"),
        (
            "    bin2c_exe = find_program('bin2c')",
            f"    bin2c_exe = find_program({meson_string(bin2c)}, native: true)",
        ),
        (
            "    cuda_flags = []",
            "    cuda_flags = ["
            + ", ".join(
                meson_string(f)
                for f in (
                    *flags,
                    f"-gencode=arch={architecture},code=sm_{architecture.removeprefix('compute_')}",
                )
            )
            + "]",
        ),
        (
            "        cuda_args: cuda_flags # + ['-gencode', 'arch=compute_86,code=sm_86' ] #, '--use_fast_math']\n",
            "",
        ),
    )
    for before, after in replacements:
        if before not in text:
            raise BuildError("libvmaf", f"Unexpected upstream CUDA Meson file: {before!r}")
        text = text.replace(before, after)
    path.write_text(text, encoding="utf-8")


def _adapt_cross_libsvm(libvmaf_dir: Path) -> None:
    path = libvmaf_dir / "src" / "svm.cpp"
    text = path.read_text(encoding="utf-8")
    signature = "template <class T> static inline void swap("
    if signature not in text:
        raise BuildError("libvmaf", "Unexpected libsvm swap implementation.")
    path.write_text(re.sub(r"\bswap(?=\s*\()", "vmaf_swap", text), encoding="utf-8")


def build_libvmaf(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvmaf."""
    env = builder.get_build_env(component)

    libvmaf_dir = source_dir / "libvmaf"
    if not libvmaf_dir.exists():
        libvmaf_dir = source_dir

    build_dir = libvmaf_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    inputs = getattr(builder.platform_detector.platform_info, "cuda_build_host_inputs", None)
    cross = builder.platform_context.cross_environment
    if cross is not None:
        _adapt_cross_libsvm(libvmaf_dir)
    if cross is not None and builder.config.enable_libvmaf_cuda:
        if cross.cuda_host_tools.nvcc is None or inputs is None:
            raise BuildError(
                component.name,
                "libvmaf CUDA is configured, but the verified CUDA host-tool projection is absent.",
                code="wsl_mingw_cuda_inputs_missing",
            )
        _adapt_cross_cuda_meson(libvmaf_dir, cross.cuda_host_tools, inputs.nvcc_version)
    libvmaf_cuda_enabled = builder.config.enable_libvmaf_cuda and inputs is not None
    libvmaf_cuda_reason = builder.platform_detector.platform_info.libvmaf_cuda_reason

    if builder.on_log is not None:
        if libvmaf_cuda_enabled:
            builder.on_log("libvmaf CUDA path enabled")
        else:
            builder.on_log(f"libvmaf CUDA path disabled: {libvmaf_cuda_reason}")

    if libvmaf_cuda_enabled:
        assert inputs is not None
        if cross is None:
            env = cuda_profile_environment(env, inputs)
        assert inputs.compute_architecture is not None
        capability = inputs.compute_architecture.removeprefix("compute_")
        if cross is None:
            env[
                "NVCC_PREPEND_FLAGS"
            ] += f" -gencode=arch={inputs.compute_architecture},code=sm_{capability}"

        if builder.config.openmp and cross is None:
            # nvcc does not accept raw -fopenmp and fails with:
            # "nvcc fatal : Unknown option '-fopenmp'".
            # Keep OpenMP for host compilation by forwarding it via -Xcompiler.
            env["CFLAGS"] = builder._remove_compiler_flag(env.get("CFLAGS", ""), "-fopenmp")
            env["CXXFLAGS"] = builder._remove_compiler_flag(env.get("CXXFLAGS", ""), "-fopenmp")
            env["LDFLAGS"] = builder._remove_compiler_flag(env.get("LDFLAGS", ""), "-fopenmp")
            env["NVCC_PREPEND_FLAGS"] += " -Xcompiler=-fopenmp"

        headers = _libvmaf_cuda_headers(builder, libvmaf_dir)
        if headers != builder.target_prefix / "include" / "ffnvcodec":
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
    if cross is not None and component.build_role.value == "target":
        meson_args.extend(["--cross-file", str(cross.meson_cross_file)])
        meson_args.append("-Denable_tools=false")

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

    if cross is not None:
        with tempfile.TemporaryDirectory(
            prefix="vmaf-install-", dir=cross.paths.generated_files_root
        ) as tmp:
            stage = Path(tmp)
            builder._run_step(
                component,
                ComponentStatus.INSTALLING,
                "ninja staged install",
                "Install failed",
                ["ninja", "-C", "build", "install"],
                "install",
                libvmaf_dir,
                dict(env, DESTDIR=str(stage)),
            )
            staged = stage / builder.target_prefix.relative_to("/")
            pc = staged / "lib/pkgconfig/libvmaf.pc"
            with pc.open("a", encoding="utf-8") as file:
                file.write("Libs.private: -lc++ -lunwind\n")
            promote_staged(component.name, staged, builder.target_prefix, builder)
    else:
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
