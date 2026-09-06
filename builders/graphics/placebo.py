"""libplacebo component builder."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def patch_libplacebo_glslang_search(source_dir: Path) -> None:
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


def patch_libplacebo_pc(builder: FFmpegBuilder) -> None:
    """Normalize libplacebo pkg-config metadata for FFmpeg probing.

    1. Rewrite absolute SPIRV/glslang archive/import-library paths to -l*
       flags so FFmpeg configure treats them as linker inputs.
    2. Ensure SPIRV-Tools transitive static deps are present.
    """
    pc_candidates = [
        builder.workspace / "lib" / "pkgconfig" / "libplacebo.pc",
        builder.workspace / "lib64" / "pkgconfig" / "libplacebo.pc",
    ]
    multiarch = builder.platform_detector.get_multiarch_dir()
    if multiarch:
        pc_candidates.append(builder.workspace / "lib" / multiarch / "pkgconfig" / "libplacebo.pc")

    for pc_file in pc_candidates:
        if not pc_file.exists():
            continue
        text = pc_file.read_text(encoding="utf-8")
        if builder._is_windows_ucrt64_backend():
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


def build_libplacebo(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libplacebo with optional Vulkan acceleration."""
    build_dir = source_dir / "build"
    if build_dir.exists():
        from ..base import _rmtree

        _rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    env = builder.get_build_env(component)
    builder._prepend_python_module_parent_to_pythonpath(env, "jinja2")

    result, log_file = builder.executor.execute_with_log(
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
    pi = builder.platform_detector.platform_info
    vulkan_ok = (
        builder.config.enable_libplacebo_vulkan
        and pi.vulkan_available
        and not (builder.config.full_static and builder.platform == "linux")
    )

    ws = builder._ws_str()

    fast_float_submod = source_dir / "3rdparty" / "fast_float" / "include" / "fast_float"
    fast_float_ws = builder.workspace / "include" / "fast_float"
    if fast_float_ws.exists() and not any(
        fast_float_submod.iterdir() if fast_float_submod.exists() else iter([])
    ):
        fast_float_submod.mkdir(parents=True, exist_ok=True)
        for item in fast_float_ws.iterdir():
            dest = fast_float_submod / item.name
            if not dest.exists():
                shutil.copy2(item, dest)

    existing_lp = env.get("LIBRARY_PATH", "")
    env["LIBRARY_PATH"] = builder._merge_path_list(
        existing_lp,
        [f"{ws}/lib", f"{ws}/lib64"],
        ":",
    )

    if builder.platform == "darwin" and vulkan_ok:
        existing_pkgcp = env.get("PKG_CONFIG_PATH", "")
        vulkan_pkgcp = "/usr/local/lib/pkgconfig"
        env["PKG_CONFIG_PATH"] = builder._merge_path_list(
            existing_pkgcp,
            [f"{ws}/lib/pkgconfig", f"{ws}/lib64/pkgconfig", vulkan_pkgcp],
            ":",
        )

    if builder._is_windows_ucrt64_backend():
        existing_pkgcp = builder._normalize_pkg_config_path_for_windows(
            env.get("PKG_CONFIG_PATH", "")
        )
        env["PKG_CONFIG_PATH"] = builder._merge_path_list(
            existing_pkgcp,
            [f"{ws}/lib/pkgconfig", f"{ws}/lib64/pkgconfig"],
            ";",
        )

    if vulkan_ok:
        builder._patch_libplacebo_glslang_search(source_dir)

    meson_args = [arg.replace("{workspace}", ws) for arg in component.configure_args]

    if vulkan_ok:
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

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "meson setup build",
        "Meson configure failed",
        ["meson", "setup", "build"] + meson_args,
        "configure",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "ninja -C build",
        "Build failed",
        ["ninja", "-C", "build"],
        "build",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "ninja install",
        "Install failed",
        ["ninja", "-C", "build", "install"],
        "install",
        source_dir,
        env,
    )

    builder._patch_libplacebo_pc()
