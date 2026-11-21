"""libplacebo component builder."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus
from .vulkan_headers import _digest, invalidate_vulkan_resume, vulkan_headers_valid
from .cross_install import promote_staged

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
    if original in text:
        text = text.replace(original, patched, 1)
    elif patched not in text:
        raise BuildError(
            "libplacebo",
            f"Expected glslang lookup pattern not found in {glsl_meson}",
        )
    # glslang's CMake install uses GNUInstallDirs, which selects lib64 on
    # Fedora/RHEL and lib elsewhere. libplacebo derives its search dirs from
    # `vulkan_sdk / 'lib'` only; GCC's find_library still locates the archive
    # via LIBRARY_PATH, but clang/icx's static search (`--print-search-dirs`)
    # does not consult LIBRARY_PATH, so add lib64 explicitly when the pattern
    # is present (older/newer libplacebo revisions may differ).
    dirs_original = "vulkan_lib_dirs += [vulkan_sdk / 'lib']"
    dirs_patched = "vulkan_lib_dirs += [vulkan_sdk / 'lib', vulkan_sdk / 'lib64']"
    if dirs_original in text:
        text = text.replace(dirs_original, dirs_patched, 1)
    glsl_meson.write_text(text, encoding="utf-8")


def patch_libplacebo_pc(builder: FFmpegBuilder) -> None:
    """Normalize libplacebo pkg-config metadata for FFmpeg probing.

    1. Rewrite absolute SPIRV/glslang archive/import-library paths to -l*
       flags so FFmpeg configure treats them as linker inputs.
    2. Ensure SPIRV-Tools transitive static deps are present.
    """
    pc_candidates = [
        builder.target_prefix / "lib" / "pkgconfig" / "libplacebo.pc",
        builder.target_prefix / "lib64" / "pkgconfig" / "libplacebo.pc",
    ]
    multiarch = builder.platform_detector.get_multiarch_dir()
    if multiarch:
        pc_candidates.append(
            builder.target_prefix / "lib" / multiarch / "pkgconfig" / "libplacebo.pc"
        )

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

    vulkan_headers = builder.registry.get_by_name("vulkan-headers")
    if vulkan_headers is None:
        raise BuildError(component.name, "vulkan-headers component is missing")
    target_prefix = builder.target_prefix
    cross = builder.platform_context.cross_environment
    if cross is None:
        if not vulkan_headers_valid(target_prefix, vulkan_headers):
            invalidate_vulkan_resume(builder.state_manager, target_prefix, [vulkan_headers])
            builder.build_component(vulkan_headers)
        if not vulkan_headers_valid(target_prefix, vulkan_headers):
            raise BuildError(
                component.name, "Vulkan headers and registry are incomplete or inconsistent"
            )
    elif not (
        cross.target_interfaces.vulkan_headers.is_dir()
        and cross.target_interfaces.vulkan_registry.is_file()
    ):
        raise BuildError(component.name, "Verified target Vulkan headers/registry are unavailable")

    # libplacebo gives its bundled Vulkan-Headers priority over loader metadata.
    # Populate that supported dependency from the verified workspace installation.
    bundled = source_dir / "3rdparty" / "Vulkan-Headers"
    bundled_include = bundled / "include"
    bundled_registry = bundled / "registry" / "vk.xml"
    workspace_registry = target_prefix / "share" / "vulkan" / "registry" / "vk.xml"
    if bundled_include.exists():
        try:
            if _digest(bundled_include, bundled_registry) != _digest(
                target_prefix / "include", workspace_registry
            ):
                raise ValueError("bundled Vulkan-Headers differ from the workspace")
        except (OSError, ValueError) as exc:
            raise BuildError(component.name, f"Conflicting bundled Vulkan-Headers: {exc}") from exc
    else:
        try:
            bundled_include.mkdir(parents=True, exist_ok=True)
            for name in ("vulkan", "vk_video"):
                shutil.copytree(target_prefix / "include" / name, bundled_include / name)
            bundled_registry.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workspace_registry, bundled_registry)
        except OSError as exc:
            raise BuildError(component.name, f"Cannot stage Vulkan headers: {exc}") from exc

    # Determine whether to enable Vulkan inside libplacebo.
    pi = builder.platform_detector.platform_info
    if cross is not None:
        vulkan_ok = builder.config.enable_libplacebo_vulkan
        if vulkan_ok and (
            not cross.target_interfaces.vulkan_import_library.is_file()
            or not cross.target_interfaces.host_shader_tools.get("shader_compiler")
        ):
            raise BuildError(
                component.name,
                "Configured target Vulkan support lacks its verified import interface "
                "or selected host shader compiler.",
                code="wsl_mingw_vulkan_interface_incomplete",
            )
    else:
        vulkan_ok = (
            builder.config.enable_libplacebo_vulkan
            and pi.vulkan_available
            and not (builder.config.full_static and builder.platform == "linux")
        )

    ws = (
        str(target_prefix).replace("\\", "/")
        if builder._is_windows_ucrt64_backend()
        else builder._ws_str()
    )

    fast_float_submod = source_dir / "3rdparty" / "fast_float" / "include" / "fast_float"
    fast_float_ws = target_prefix / "include" / "fast_float"
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
        ";" if builder._is_windows_ucrt64_backend() else ":",
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
            f"-Dvulkan-registry={ws}/share/vulkan/registry/vk.xml",
            "-Dglslang=enabled",
        ]
    else:
        meson_args += [
            "-Dvulkan=disabled",
            "-Dglslang=disabled",
        ]
    if cross is not None and component.build_role.value == "target":
        meson_args = ["--cross-file", str(cross.meson_cross_file), *meson_args]
        env["PATH"] = str(cross.target_tools.prefix / "bin") + ":" + env.get("PATH", "")
        shader_tool = cross.target_interfaces.host_shader_tools.get("shader_compiler")
        if shader_tool:
            env["GLSLANG_VALIDATOR"] = shader_tool

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

    if cross is not None and component.build_role.value == "target":
        with tempfile.TemporaryDirectory(
            prefix="placebo-install-", dir=cross.paths.generated_files_root
        ) as tmp:
            stage = Path(tmp)
            builder._run_step(
                component,
                ComponentStatus.INSTALLING,
                "ninja staged install",
                "Install failed",
                ["ninja", "-C", "build", "install"],
                "install",
                source_dir,
                dict(env, DESTDIR=str(stage)),
            )
            staged = stage / builder.target_prefix.relative_to("/")
            promote_staged(component.name, staged, builder.target_prefix, builder)
        builder._patch_libplacebo_pc()
        pc = target_prefix / "lib" / "pkgconfig" / "libplacebo.pc"
        text = pc.read_text(encoding="utf-8")
        text += "Libs.private: -lc++ -lunwind\n"
        pc.write_text(text, encoding="utf-8")
    else:
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
