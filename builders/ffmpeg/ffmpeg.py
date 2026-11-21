"""FFmpeg target component builder."""

from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ...state import ComponentStatus
from ...build_types import BuildError
from ...config import TARGET_BACKEND_CROSS_MINGW
from ...cross_environment import validate_pkg_config_output
from ...cuda_prerequisites import (
    cuda_build_tree_matches,
    cuda_profile_environment,
    record_cuda_build_tree,
)
from ..graphics.placebo import patch_libplacebo_pc

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def _target_link_path(builder: FFmpegBuilder, value: str, component: Component) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise BuildError(
            component.name,
            f"Target link metadata contains an unsafe prefix-relative path: {value!r}.",
            code="wsl_mingw_target_link_metadata_invalid",
        )
    return builder.target_prefix.joinpath(*path.parts)


def build_ffmpeg(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build FFmpeg."""
    env = builder.get_build_env(component)
    cross = builder.platform_context.cross_environment
    if cross is not None:
        env["CFLAGS"] = cross.autotools.environment["CFLAGS"]
        env["CXXFLAGS"] = cross.autotools.environment["CXXFLAGS"]

    built_components = [
        name
        for name, state in builder.state_manager.get().components.items()
        if state.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM)
    ]

    # Resume builds may skip libplacebo rebuild. Re-apply pkg-config
    # normalization so FFmpeg's configure probe remains stable.
    if "libplacebo" in built_components:
        builder._patch_libplacebo_pc()

    extra_libs = builder.extralibs
    extra_ldflags = builder.ldflags
    placebo_vulkan = False

    # Add libraries conditionally based on built components
    if "libvmaf" in built_components:
        if builder.platform == "darwin":
            extra_libs += " -lc++"
        else:
            extra_libs += " -lstdc++"

    if "libjxl" in built_components and cross is None:
        # libjxl_threads.a uses std::thread and omits the C++ runtime from
        # its static pkg-config metadata.
        # lcms2 is a private dependency of libjxl not listed in Libs:.
        extra_libs += " -llcms2"
        extra_libs += " -lc++" if builder.platform == "darwin" else " -lstdc++"

    # libplacebo links against the system Vulkan ICD loader at runtime.
    # On Linux the loader is libvulkan.so; it must appear in extralibs so
    # the static FFmpeg binary resolves Vulkan symbols at link time.
    # On macOS the loader is libvulkan.dylib (LunarG SDK in /usr/local/lib);
    # -L/usr/local/lib is added so the linker finds it.
    # On Windows UCRT64 the loader (vulkan-1.dll) is auto-discovered via
    # pkg-config Libs, so no extra flag is needed there.
    if "libplacebo" in built_components:
        pi = builder.platform_detector.platform_info
        if cross is not None:
            placebo_vulkan = builder.config.enable_libplacebo_vulkan
            if placebo_vulkan and not cross.target_interfaces.vulkan_import_library.is_file():
                raise BuildError(
                    component.name,
                    "Configured target Vulkan support lacks its verified import interface.",
                    code="wsl_mingw_vulkan_interface_incomplete",
                )
        else:
            placebo_vulkan = (
                builder.config.enable_libplacebo_vulkan
                and pi.vulkan_available
                and not (builder.config.full_static and builder.platform == "linux")
            )
        if placebo_vulkan:
            if builder.platform == "linux":
                extra_libs += " -lvulkan"
            elif builder.platform == "darwin":
                extra_libs += " -L/usr/local/lib -lvulkan"
                # libvulkan.1.dylib is a shared library; dyld must be able
                # to find it at runtime via @rpath.  Add the LunarG SDK lib
                # directory so the compiler test executable doesn't crash
                # with "Library not loaded: @rpath/libvulkan.1.dylib".
                extra_ldflags += " -Wl,-rpath,/usr/local/lib"

    target_metadata = None
    if cross is not None and builder.config.target_backend == TARGET_BACKEND_CROSS_MINGW:
        target_metadata = builder.registry.get_target_link_metadata(built_components)
        include_roots = [
            _target_link_path(builder, root, component) for root in target_metadata.include_roots
        ]
        library_roots = [
            _target_link_path(builder, root, component) for root in target_metadata.library_roots
        ]
        if include_roots:
            env["CFLAGS"] = " ".join(
                (env.get("CFLAGS", ""), *(f"-I{path}" for path in include_roots))
            ).strip()
        if library_roots:
            extra_ldflags += " " + " ".join(f"-L{path}" for path in library_roots)
        static_libraries = []
        for library in target_metadata.static_libraries:
            if library.startswith("-"):
                static_libraries.append(library)
            else:
                static_libraries.append(str(_target_link_path(builder, library, component)))
        import_interfaces = [
            str(_target_link_path(builder, item, component))
            for item in target_metadata.import_interfaces
        ]
        if static_libraries or import_interfaces:
            extra_libs += " " + " ".join((*static_libraries, *import_interfaces))
        requirements = target_metadata.consumer_link_requirements
        if requirements:
            extra_libs += " " + " ".join(requirements)
        validate_pkg_config_output(
            " ".join(
                [
                    *(f"-I{path}" for path in include_roots),
                    *(f"-L{path}" for path in library_roots),
                    *static_libraries,
                    *import_interfaces,
                    *requirements,
                ]
            ),
            cross.approved_target_roots,
        )

    # Strip leading/trailing whitespace that accumulates when starting from "".
    extra_libs = extra_libs.strip()

    configure_flags = builder.registry.get_ffmpeg_configure_flags(
        built_components,
        builder.config.gpl_enabled,
        builder.platform,
        builder.platform_detector.platform_info,
        builder.config.ffmpeg_version,
    )
    if (
        cross is not None
        and "libplacebo" in built_components
        and builder.config.enable_libplacebo_vulkan
        and "--enable-vulkan" not in configure_flags
    ):
        configure_flags.append("--enable-vulkan")
    if "libplacebo" in built_components and not placebo_vulkan:
        configure_flags = [flag for flag in configure_flags if flag != "--enable-libplacebo"]

    pkgconfig_dir = builder._ws_str()
    if builder._is_windows_ucrt64_backend():
        pkgconfig_dir = builder._to_msys_path(pkgconfig_dir)

    configure_args = [
        "--disable-debug",
        "--disable-shared",
        "--enable-static",
        "--enable-version3",
        # Use the component-scoped CFLAGS (includes any per-component or
        # per-toolchain platform overrides), not the global builder flags, so
        # toolchain compatibility flags (e.g. an ICX -ffp-model baseline)
        # reach FFmpeg's own TU compilation as well.
        f"--extra-cflags={env.get('CFLAGS', builder.cflags)}",
        f"--extra-ldexeflags={builder.ldexeflags}",
        f"--extra-ldflags={extra_ldflags}",
        f"--extra-libs={extra_libs}",
        f"--pkgconfigdir={pkgconfig_dir}/lib/pkgconfig",
        "--pkg-config-flags=--static",
        f"--prefix={builder._ws_str()}",
    ]
    if "CC" in env:
        configure_args.append(f"--cc={env['CC']}")
    if "CXX" in env:
        configure_args.append(f"--cxx={env['CXX']}")

    # On UCRT64/MinGW, POSIX pthreads are not available as a system
    # library; use native Windows threads (w32threads) instead.
    if builder._is_windows_ucrt64_backend():
        configure_args.append("--enable-w32threads")
    else:
        configure_args.append("--enable-pthreads")

    if builder.config.gpl_enabled:
        configure_args.append("--enable-gpl")
        configure_args.append("--enable-nonfree")

    platform_info = builder.platform_detector.platform_info
    inputs = getattr(platform_info, "cuda_build_host_inputs", None)
    cross_cuda = cross.cuda_host_tools if cross is not None else None
    cuda_available_for_build = inputs is not None or (
        cross_cuda is not None and cross_cuda.nvcc is not None
    )
    use_nvcc = inputs is not None and (
        builder.config.gpl_enabled or builder.config.allow_nonfree_cuda
    )
    if cross is not None:
        use_nvcc = cuda_available_for_build and (
            builder.config.gpl_enabled or builder.config.allow_nonfree_cuda
        )
        if use_nvcc:
            raise BuildError(
                component.name,
                "The scoped CUDA PTX/fatbin host probes do not prove that NVCC can emit "
                "a Windows COFF host object for FFmpeg. CUDA compilation is blocked until "
                "that target-object flow is validated.",
                code="wsl_mingw_ffmpeg_cuda_coff_unverified",
            )
    configure_args.append("--disable-cuda-llvm")
    # CUDA compilation and hardware encode/decode are separate capabilities.
    if builder.platform_detector.platform_info.cuda_available or getattr(
        platform_info, "nvenc_api_version", None
    ):
        # cuda-nvcc requires MSVC cl.exe on Windows which is not available
        # in the MSYS2 UCRT64 toolchain.  Hardware encode/decode APIs
        # (cuvid/nvdec/nvenc) and ffnvcodec headers work fine with GCC.
        if not builder._is_windows_ucrt64_backend():
            if use_nvcc and inputs is not None:
                env = cuda_profile_environment(env, inputs)
                env["NVCC_PREPEND_FLAGS"] = ""
                configure_args.append("--enable-cuda-nvcc")
                if not builder.config.gpl_enabled:
                    configure_args.append("--enable-nonfree")
                configure_args.append(f"--nvcc={inputs.nvcc_path}")
                assert inputs.compute_architecture is not None
                cuda_cc = inputs.compute_architecture.removeprefix("compute_")
                flags = [
                    *inputs.common_flags(),
                    "-gencode",
                    f"arch=compute_{cuda_cc},code=sm_{cuda_cc}",
                    "-O2",
                ]
                configure_args.append(f"--nvccflags={shlex.join(flags)}")
            else:
                configure_args.append("--disable-cuda-nvcc")
        else:
            configure_args.append("--disable-cuda-nvcc")
        configure_args.append("--enable-cuvid")
        configure_args.append("--enable-nvdec")
        configure_args.append("--enable-nvenc")
        configure_args.append("--enable-ffnvcodec")
    else:
        configure_args.append("--disable-cuda-nvcc")
        configure_args.append("--disable-ffnvcodec")

    # VAAPI support (Linux-only policy in current implementation).
    if (
        builder.platform == "linux"
        and builder.platform_detector.platform_info.vaapi_available
        and not builder.config.full_static
    ):
        configure_args.append("--enable-vaapi")

    qsv_available = builder.platform_detector.platform_info.qsv_available
    windows_ucrt64_qsv = builder._is_windows_ucrt64_backend() and qsv_available

    # Intel QSV support.
    if builder.platform == "linux" and qsv_available:
        configure_args.append("--enable-libvpl")
    if windows_ucrt64_qsv:
        configure_args.append("--enable-libvpl")

    # Keep Linux/macOS-only acceleration paths disabled on Windows.
    if builder.platform == "windows":
        configure_args.append("--disable-vaapi")
        configure_args.append("--disable-videotoolbox")
        if not windows_ucrt64_qsv:
            configure_args.append("--disable-libvpl")

    if builder.platform == "darwin":
        configure_args.append(f"--extra-version={component.version}")

    configure_args.extend(configure_flags)

    marker = source_dir / ".ffmpeg-builder-cuda-profile.json"
    tree_inputs = inputs if use_nvcc else None
    if (source_dir / "ffbuild" / "config.mak").is_file() and not cuda_build_tree_matches(
        marker, tree_inputs
    ):
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "make clean",
            "Cleaning stale CUDA build failed",
            ["make", "clean"],
            "clean",
            source_dir,
            env,
        )

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure"] + configure_args,
        "configure",
        source_dir,
        env,
    )
    record_cuda_build_tree(marker, tree_inputs)

    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        f"make -j{builder.num_jobs}",
        "Build failed",
        ["make", f"-j{builder.num_jobs}"],
        "build",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "make install",
        "Install failed",
        ["make", "install"],
        "install",
        source_dir,
        env,
    )
