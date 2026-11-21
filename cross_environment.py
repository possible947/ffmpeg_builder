"""Identity-local Linux-to-Windows cross-environment construction and probes."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .build_types import CATEGORY_TARGET_DATA, CATEGORY_TOOLCHAIN_INSTALL, BuildError
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .components import ComponentRegistry
from .cross_build_contracts import (
    RuntimeDependency,
    TargetInterfaceProjection,
    TargetLinkMetadata,
    compose_target_link_metadata,
)
from .target_inputs import materialize_target_inputs, validate_target_inputs_manifest
from .wsl_mingw_environment import filter_wsl_mingw_target_environment
from .wsl_mingw_preparation import WslMingwPreparation

TARGET_TRIPLE = "x86_64-w64-mingw32"
RUST_TARGET = "x86_64-pc-windows-gnullvm"
CPU_TUNING_FLAGS = ("-march=native", "-mtune=native")
_TARGET_ENV_VARS = (
    "CC",
    "CXX",
    "CPP",
    "LD",
    "AR",
    "AS",
    "NM",
    "RANLIB",
    "STRIP",
    "OBJCOPY",
    "RC",
    "WINDRES",
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "LIBS",
    "PKG_CONFIG",
    "PKG_CONFIG_PATH",
    "PKG_CONFIG_LIBDIR",
    "PKG_CONFIG_SYSROOT_DIR",
    "PKG_CONFIG_DIR",
    "CMAKE_PREFIX_PATH",
    "CMAKE_TOOLCHAIN_FILE",
    "CMAKE_FIND_ROOT_PATH",
    "MESON_CROSS_FILE",
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "CUDA_HOME",
    "CUDA_PATH",
    "CUDACXX",
    "CUDA_NVCC_CCBIN",
    "NVCC_PREPEND_FLAGS",
    "NVCC_APPEND_FLAGS",
    "VULKAN_SDK",
    "VK_LAYER_PATH",
    "VK_ADD_LAYER_PATH",
    "VK_ICD_FILENAMES",
    "VK_DRIVER_FILES",
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_BUILD_TARGET",
    "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER",
    "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_AR",
)
_FORBIDDEN_PATHS = ("/usr/include", "/usr/lib", "/usr/lib64", "/lib", "/lib64")


@dataclass(frozen=True)
class CrossPaths:
    """Mutable and generated paths owned by one guarded target identity."""

    workspace_root: Path
    target_prefix: Path
    build_root: Path
    generated_files_root: Path
    packages_root: Path
    diagnostics_root: Path


@dataclass(frozen=True)
class TargetTools:
    """Explicit target compiler and binutils entrypoints from LLVM-MinGW."""

    cc: Path
    cxx: Path
    ar: Path
    ranlib: Path
    strip: Path
    windres: Path
    dlltool: Path
    linker: Path
    assembler: Path
    nm: Path
    objcopy: Path
    objdump: Path
    readobj: Path
    prefix: Path
    target_sysroot: Path
    generic_headers: Path
    clang_resource: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "CC": str(self.cc),
            "CXX": str(self.cxx),
            "AR": str(self.ar),
            "RANLIB": str(self.ranlib),
            "STRIP": str(self.strip),
            "WINDRES": str(self.windres),
            "DLLTOOL": str(self.dlltool),
            "LD": str(self.linker),
            "AS": str(self.assembler),
            "NM": str(self.nm),
            "OBJCOPY": str(self.objcopy),
            "OBJDUMP": str(self.objdump),
            "READOBJ": str(self.readobj),
        }


@dataclass(frozen=True)
class HostTools:
    """Host-native tools used to drive configuration and build systems."""

    paths: Mapping[str, str]

    def __getitem__(self, name: str) -> str:
        return self.paths[name]


@dataclass(frozen=True)
class RustTargetTools:
    """Project-managed Rust compiler projection for later Cargo consumers."""

    rustc: Path
    cargo: Path
    target: str
    linker: Path
    archiver: Path
    target_std: Path

    def environment(self) -> dict[str, str]:
        return {
            "RUSTC": str(self.rustc),
            "CARGO": str(self.cargo),
            "CARGO_BUILD_TARGET": self.target,
            "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER": str(self.linker),
            "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_AR": str(self.archiver),
        }


@dataclass(frozen=True)
class DeferredCudaHostTools:
    """Validated host-only CUDA tools, intentionally not exported to target builds."""

    nvcc: Optional[Path] = None
    toolkit_release: Optional[str] = None
    ccbin: Optional[Path] = None
    common_flags: tuple[str, ...] = ()
    compute_architecture: Optional[str] = None
    compatibility_workaround: Optional[str] = None
    bin2c: Optional[Path] = None
    diagnostics: tuple[str, ...] = ()

    def device_mode_flags(self, mode: str) -> tuple[str, ...]:
        """Return output-mode/device flags without host-compiler options."""
        match = re.fullmatch(r"compute_(\d+)", self.compute_architecture or "")
        if match is None:
            raise ValueError("CUDA compute architecture is not validated")
        if mode == "ptx":
            return (
                "--ptx",
                "-gencode",
                f"arch={self.compute_architecture},code={self.compute_architecture}",
            )
        if mode == "fatbin":
            return (
                "--fatbin",
                "-gencode",
                f"arch={self.compute_architecture},code=sm_{match.group(1)}",
            )
        raise ValueError(f"unsupported CUDA output mode: {mode}")


@dataclass(frozen=True)
class CrossDiagnostic:
    """Structured environment-filtering or validation diagnostic."""

    variable: str
    value: str
    reason: str


class TargetDiscoveryError(BuildError):
    """Target dependency path contamination with structured source details."""

    def __init__(self, value: str, reason: str):
        super().__init__(
            "Target pkg-config contamination",
            f"{reason}: {value!r}.",
            category=CATEGORY_TARGET_DATA,
            code="wsl_mingw_pkg_config_contamination",
        )
        self.diagnostic = CrossDiagnostic("pkg-config output", value, reason)


@dataclass(frozen=True)
class AutotoolsProjection:
    """Deterministic configure tuple, arguments, and environment projection."""

    build_tuple: str
    host_tuple: str
    arguments: tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class CrossEnvironment:
    """Typed host/target projections for one guarded cross-build workspace."""

    backend: str
    build_machine: str
    host_machine: str
    target_triple: str
    paths: CrossPaths
    host_tools: HostTools
    target_tools: TargetTools
    cpu_tuning_flags: tuple[str, ...]
    host_environment: Mapping[str, str]
    compiler_environment: Mapping[str, str]
    target_discovery_environment: Mapping[str, str]
    pkg_config_wrapper: Path
    cmake_toolchain_file: Path
    meson_cross_file: Path
    target_input_manifest: Mapping[str, Any]
    rust_tools: RustTargetTools
    rust_environment: Mapping[str, str]
    cargo_environment: Mapping[str, str]
    cuda_host_tools: DeferredCudaHostTools
    cuda_environment: Mapping[str, str]
    cuda_acceptance: Mapping[str, Any]
    target_interfaces: TargetInterfaceProjection
    autotools: AutotoolsProjection
    approved_target_roots: tuple[Path, ...]
    diagnostics: tuple[CrossDiagnostic, ...] = ()
    manifest_path: Optional[Path] = None

    def semantic_projection(self) -> dict[str, Any]:
        """Return deterministic, path-aware projections for equality checks."""
        return {
            "backend": self.backend,
            "build_machine": self.build_machine,
            "host_machine": self.host_machine,
            "target_triple": self.target_triple,
            "target_tools": self.target_tools.as_dict(),
            "cpu_tuning_flags": list(self.cpu_tuning_flags),
            "host_tools": dict(sorted(self.host_tools.paths.items())),
            "target_interfaces": {
                "prefix": str(self.target_interfaces.prefix),
                "host_shader_tools": dict(sorted(self.target_interfaces.host_shader_tools.items())),
            },
            "compiler_environment": dict(sorted(self.compiler_environment.items())),
            "target_discovery_environment": dict(sorted(self.target_discovery_environment.items())),
            "autotools": {
                "build_tuple": self.autotools.build_tuple,
                "host_tuple": self.autotools.host_tuple,
                "arguments": list(self.autotools.arguments),
                "environment": dict(sorted(self.autotools.environment.items())),
            },
            "target_materialization_id": self.target_input_manifest.get("materialization_id"),
        }


def _build_error(component: str, message: str, category: str, code: str) -> BuildError:
    return BuildError(component, message, category=category, code=code)


def _clean_target_probe_environment(parent: Mapping[str, str]) -> dict[str, str]:
    environment = dict(parent)
    for name in _TARGET_ENV_VARS:
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith("PKG_CONFIG_"):
            environment.pop(name, None)
    environment.pop("CCACHE", None)
    return environment


def _host_environment(parent: Mapping[str, str]) -> dict[str, str]:
    environment = dict(parent)
    for name in _TARGET_ENV_VARS:
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith("CARGO_TARGET_") or name.startswith("CMAKE_CROSSCOMPILING"):
            environment.pop(name, None)
    return environment


def _host_tool_path(
    paths: Mapping[str, str],
    parent_path: str,
    external_sdk_roots: Sequence[str | Path] = (),
) -> str:
    directories = list(dict.fromkeys(str(Path(path).parent) for path in paths.values()))
    if parent_path:
        for item in parent_path.split(os.pathsep):
            if not item or item in directories:
                continue
            if any(_inside(Path(item), Path(root)) for root in external_sdk_roots):
                continue
            directories.append(item)
    return os.pathsep.join(directories)


def _validate_cpu_tuning(
    tools: TargetTools, flags: tuple[str, ...], parent_environment: Mapping[str, str]
) -> None:
    if not flags:
        return
    environment = _clean_target_probe_environment(parent_environment)
    for language, compiler in (("C", tools.cc), ("C++", tools.cxx)):
        command = [
            str(compiler),
            *flags,
            "-fsyntax-only",
            "-x",
            "c" if language == "C" else "c++",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                input="",
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _build_error(
                f"LLVM-MinGW {language} compiler probe",
                f"Could not run {' '.join(command)}: {exc}",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_cpu_tuning_probe_failed",
            ) from exc
        if result.returncode:
            output = (result.stdout + result.stderr).strip()
            raise _build_error(
                f"LLVM-MinGW {language} compiler probe",
                f"The selected compiler rejected {' '.join(flags)}."
                + (f"\n{output[-2000:]}" if output else ""),
                CATEGORY_TARGET_DATA,
                "wsl_mingw_cpu_tuning_probe_failed",
            )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _require_executable(path: Path, label: str, prefix: Optional[Path] = None) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _build_error(
            "LLVM-MinGW toolchain",
            f"Required {label} is missing: {path} ({exc}). Re-verify or reacquire "
            "the project-managed llvm-mingw profile.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_tool_missing",
        ) from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise _build_error(
            "LLVM-MinGW toolchain",
            f"Required {label} is not an executable file: {path}.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_tool_missing",
        )
    if prefix is not None and not _inside(resolved, prefix):
        raise _build_error(
            "LLVM-MinGW toolchain",
            f"Resolved {label} escapes the verified LLVM-MinGW prefix: {path} -> {resolved}.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_tool_outside_prefix",
        )
    return path


def _resolve_host_tools(parent_environment: Mapping[str, str]) -> HostTools:
    path_value = parent_environment.get("PATH", os.defpath)
    required = (
        "cc",
        "c++",
        "cmake",
        "meson",
        "ninja",
        "make",
        "pkg-config",
        "perl",
        "sh",
        "autoconf",
        "autoreconf",
    )
    resolved: dict[str, str] = {}
    for name in required:
        path = shutil.which(name, path=path_value)
        if path is None:
            raise _build_error(
                "WSL-Mingw cross environment",
                f"Required WSL/Linux host tool {name!r} was not found on the host PATH.",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_host_tool_missing",
            )
        resolved[name] = str(Path(path).resolve())
    # Keep the venv entrypoint spelling: resolving its symlink can escape the
    # venv and lose the project's installed package/development tools.
    python_path = str(Path(sys.executable).absolute())
    if not Path(python_path).is_file():
        raise _build_error(
            "WSL-Mingw cross environment",
            f"Host Python executable is unavailable: {python_path}.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_host_tool_missing",
        )
    resolved["python"] = python_path
    for name in ("nasm", "yasm", "config.guess"):
        path = shutil.which(name, path=path_value)
        if path:
            resolved[name] = str(Path(path).resolve())
    return HostTools(paths=resolved)


def _resolve_target_tools(preparation: WslMingwPreparation) -> TargetTools:
    profile = preparation.project_toolchains.get("llvm-mingw")
    if not isinstance(profile, Mapping):
        raise _build_error(
            "LLVM-MinGW toolchain",
            "G2 preparation did not provide the verified llvm-mingw profile.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_toolchain_missing",
        )
    target = profile.get("target")
    provenance = profile.get("provenance")
    if (
        not isinstance(target, Mapping)
        or target.get("triple") != TARGET_TRIPLE
        or target.get("runtime") != "ucrt"
        or not isinstance(provenance, Mapping)
        or provenance.get("verified") is not True
    ):
        raise _build_error(
            "LLVM-MinGW toolchain",
            "The prepared LLVM-MinGW profile is not the verified x86-64/UCRT toolchain.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_toolchain_incompatible",
        )
    prefix_value = profile.get("prefix")
    paths = profile.get("paths")
    if not isinstance(prefix_value, str) or not isinstance(paths, Mapping):
        raise _build_error(
            "LLVM-MinGW toolchain",
            "The prepared LLVM-MinGW profile has no prefix or materialized tool map.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_toolchain_incomplete",
        )
    prefix = Path(prefix_value).resolve(strict=True)
    expected = {
        "cc": "CC",
        "cxx": "CXX",
        "ar": "AR",
        "ranlib": "RANLIB",
        "strip": "STRIP",
        "windres": "WINDRES",
        "dlltool": "DLLTOOL",
        "ld": "LD",
        "as": "AS",
        "nm": "NM",
        "objcopy": "OBJCOPY",
        "objdump": "OBJDUMP",
        "readobj": "READOBJ",
    }
    found: dict[str, Path] = {}
    for role, key in expected.items():
        value = paths.get(key)
        if not isinstance(value, str):
            raise _build_error(
                "LLVM-MinGW toolchain",
                f"The verified llvm-mingw registry mapping for {role} ({key}) is missing.",
                CATEGORY_TOOLCHAIN_INSTALL,
                "wsl_mingw_target_tool_mapping_missing",
            )
        found[role] = _require_executable(Path(value), role, prefix)
    cc = found["cc"]
    cxx = found["cxx"]
    for name, tool in (("CC", cc), ("CXX", cxx)):
        result = subprocess.run(
            [str(tool), "--version"], capture_output=True, text=True, check=False
        )
        if result.returncode or "clang version 23.1.5" not in result.stdout + result.stderr:
            raise _build_error(
                "LLVM-MinGW toolchain",
                f"{name} does not match the G2-verified LLVM-MinGW compiler: {tool}.",
                CATEGORY_TOOLCHAIN_INSTALL,
                "wsl_mingw_target_compiler_incompatible",
            )
        if "x86_64-w64-windows-gnu" not in result.stdout + result.stderr:
            raise _build_error(
                "LLVM-MinGW toolchain",
                f"{name} does not report the expected Windows x86-64 target: {tool}.",
                CATEGORY_TOOLCHAIN_INSTALL,
                "wsl_mingw_target_compiler_incompatible",
            )
    target_root = prefix / "x86_64-w64-mingw32"
    generic_headers = prefix / "generic-w64-mingw32" / "include"
    resource_result = subprocess.run(
        [str(cc), "-print-resource-dir"], capture_output=True, text=True, check=False
    )
    if resource_result.returncode:
        raise _build_error(
            "LLVM-MinGW toolchain",
            f"Could not resolve Clang's target resource directory with {cc}: "
            f"{resource_result.stderr.strip()}",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_toolchain_incomplete",
        )
    resource = Path(resource_result.stdout.strip())
    if not resource.is_absolute() or not _inside(resource, prefix):
        raise _build_error(
            "LLVM-MinGW toolchain",
            f"Clang resource directory is outside the verified prefix: {resource}.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_target_toolchain_incomplete",
        )
    for label, root in (
        ("Windows target sysroot", target_root),
        ("generic Windows headers", generic_headers),
        ("Clang resource directory", resource),
    ):
        if not root.is_dir():
            raise _build_error(
                "LLVM-MinGW toolchain",
                f"Expected {label} is missing from the verified profile: {root}.",
                CATEGORY_TOOLCHAIN_INSTALL,
                "wsl_mingw_target_toolchain_incomplete",
            )
    return TargetTools(
        cc=cc,
        cxx=cxx,
        ar=found["ar"],
        ranlib=found["ranlib"],
        strip=found["strip"],
        windres=found["windres"],
        dlltool=found["dlltool"],
        linker=found["ld"],
        assembler=found["as"],
        nm=found["nm"],
        objcopy=found["objcopy"],
        objdump=found["objdump"],
        readobj=found["readobj"],
        prefix=prefix,
        target_sysroot=target_root,
        generic_headers=generic_headers,
        clang_resource=resource,
    )


def _resolve_rust_tools(
    preparation: WslMingwPreparation, target_tools: TargetTools
) -> RustTargetTools:
    profile = preparation.project_toolchains.get("rust")
    if not isinstance(profile, Mapping) or not isinstance(profile.get("prefix"), str):
        raise _build_error(
            "Rust target toolchain",
            "G2 preparation did not provide the verified project-managed Rust profile.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_rust_toolchain_missing",
        )
    prefix = Path(str(profile["prefix"])).resolve(strict=True)
    rustc = _require_executable(prefix / "bin" / "rustc", "rustc", prefix)
    cargo = _require_executable(prefix / "bin" / "cargo", "cargo", prefix)
    target_std = prefix / "lib" / "rustlib" / RUST_TARGET
    if not target_std.is_dir():
        raise _build_error(
            "Rust target toolchain",
            f"Required project-managed Rust target standard library is missing: {target_std}.",
            CATEGORY_TOOLCHAIN_INSTALL,
            "wsl_mingw_rust_target_std_missing",
        )
    return RustTargetTools(
        rustc=rustc,
        cargo=cargo,
        target=RUST_TARGET,
        linker=target_tools.cc,
        archiver=target_tools.ar,
        target_std=target_std,
    )


def _cuda_host_tools(preparation: WslMingwPreparation) -> DeferredCudaHostTools:
    cuda = preparation.cuda
    if cuda is None:
        return DeferredCudaHostTools()
    nvcc = Path(cuda.nvcc_path).resolve(strict=True)
    ccbin = Path(cuda.ccbin_path).resolve(strict=True)
    root = Path(cuda.cuda_root).resolve(strict=True)
    bin2c = root / "bin" / "bin2c"
    if not _inside(nvcc, root) or not ccbin.is_file() or not os.access(ccbin, os.X_OK):
        raise _build_error(
            "CUDA host tools",
            "The selected NVCC escaped its SDK root or the prepared host compiler is unavailable.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cuda_host_tool_outside_root",
        )
    if not bin2c.is_file() or not os.access(bin2c, os.X_OK) or not _inside(bin2c, root):
        raise _build_error(
            "CUDA host tools",
            f"The selected CUDA toolkit does not provide an executable bin2c: {bin2c}.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cuda_bin2c_missing",
        )
    return DeferredCudaHostTools(
        nvcc=nvcc,
        toolkit_release=cuda.detected_cuda_version,
        ccbin=ccbin,
        common_flags=tuple(cuda.common_flags()),
        compute_architecture=cuda.compute_architecture,
        compatibility_workaround=cuda.compatibility_workaround,
        bin2c=bin2c.resolve(strict=True),
        diagnostics=tuple(cuda.probe_diagnostics),
    )


def _cuda_host_environment(
    tools: DeferredCudaHostTools,
    preparation: WslMingwPreparation,
    host_environment: Mapping[str, str],
) -> dict[str, str]:
    if tools.nvcc is None or preparation.cuda is None:
        return {}
    environment = _host_environment(host_environment)
    root = str(Path(preparation.cuda.cuda_root).resolve(strict=True))
    environment.update(
        {
            "CUDA_HOME": root,
            "CUDA_PATH": root,
            "CUDACXX": str(tools.nvcc),
            "CUDA_NVCC_CCBIN": str(tools.ccbin),
            "NVCC_PREPEND_FLAGS": shlex.join(tools.common_flags),
        }
    )
    if tools.nvcc.parent.is_dir():
        path = environment.get("PATH", "")
        environment["PATH"] = str(tools.nvcc.parent) + (os.pathsep + path if path else "")
    return environment


def _validate_cuda_host_tools(
    tools: DeferredCudaHostTools,
    environment: Mapping[str, str],
    scratch_root: Path,
    target_tools: TargetTools,
    target_environment: Mapping[str, str],
) -> Mapping[str, Any]:
    if tools.nvcc is None:
        return {"configured": False}
    if (
        re.fullmatch(r"compute_(\d+)", tools.compute_architecture or "") is None
        or tools.bin2c is None
        or tools.ccbin is None
        or tools.toolkit_release is None
    ):
        raise _build_error(
            "CUDA host tools",
            "The prepared CUDA profile lacks its release, architecture, ccbin, or bin2c.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cuda_host_tools_incomplete",
        )
    probe_environment = dict(environment)
    probe_environment["NVCC_PREPEND_FLAGS"] = ""
    with tempfile.TemporaryDirectory(prefix="cuda-consumer-probe-", dir=scratch_root) as temporary:
        scratch = Path(temporary)
        source = scratch / "probe.cu"
        source.write_text(
            'extern "C" __global__ void g3_probe() {}\n',
            encoding="utf-8",
        )
        common = [str(tools.nvcc), *tools.common_flags]
        outcomes: dict[str, Any] = {
            "configured": True,
            "toolkit_release": tools.toolkit_release,
            "nvcc": str(tools.nvcc),
            "ccbin": str(tools.ccbin),
            "architecture": tools.compute_architecture,
            "common_flags": list(tools.common_flags),
            "compatibility_workaround": tools.compatibility_workaround,
            "bin2c": str(tools.bin2c),
            "diagnostics": list(tools.diagnostics),
            "modes": [],
        }
        for mode, output, code in (
            ("ptx", scratch / "probe.ptx", "ptx"),
            ("fatbin", scratch / "probe.fatbin", "fatbin"),
        ):
            mode_args = list(tools.device_mode_flags(mode))
            command = [*common, *mode_args, str(source), "-o", str(output)]
            try:
                result = subprocess.run(
                    command,
                    env=probe_environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise _build_error(
                    f"CUDA {mode} consumer probe",
                    f"Could not run selected NVCC: {exc}",
                    CATEGORY_TARGET_DATA,
                    f"wsl_mingw_cuda_{code}_probe_failed",
                ) from exc
            if result.returncode or not output.is_file() or output.stat().st_size == 0:
                detail = (result.stdout + result.stderr).strip()[-2000:]
                raise _build_error(
                    f"CUDA {mode} consumer probe",
                    f"Selected NVCC failed to produce {mode} using the prepared architecture."
                    + (f"\n{detail}" if detail else ""),
                    CATEGORY_TARGET_DATA,
                    f"wsl_mingw_cuda_{code}_probe_failed",
                )
            outcomes["modes"].append(
                {"mode": mode, "suffix": output.suffix, "size": output.stat().st_size}
            )
            if mode == "fatbin":
                generated_c = scratch / "probe.c"
                command = [
                    str(tools.bin2c),
                    "-c",
                    "-n",
                    "g3_probe",
                    str(output),
                ]
                try:
                    result = subprocess.run(
                        command,
                        env=probe_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise _build_error(
                        "CUDA bin2c consumer probe",
                        f"Could not run selected bin2c: {exc}",
                        CATEGORY_TARGET_DATA,
                        "wsl_mingw_cuda_bin2c_probe_failed",
                    ) from exc
                if result.returncode or not result.stdout.strip():
                    detail = (result.stdout + result.stderr).strip()[-2000:]
                    raise _build_error(
                        "CUDA bin2c consumer probe",
                        "Selected bin2c failed to convert the NVCC fatbin."
                        + (f"\n{detail}" if detail else ""),
                        CATEGORY_TARGET_DATA,
                        "wsl_mingw_cuda_bin2c_probe_failed",
                    )
                generated_c.write_text(result.stdout, encoding="utf-8")
                if "g3_probe" not in result.stdout:
                    raise _build_error(
                        "CUDA bin2c consumer probe",
                        "Selected bin2c output does not contain the requested data symbol.",
                        CATEGORY_TARGET_DATA,
                        "wsl_mingw_cuda_bin2c_probe_failed",
                    )
                object_file = scratch / "probe-coff.o"
                target_cflags = shlex.split(target_environment.get("CFLAGS", ""))
                try:
                    compile_result = subprocess.run(
                        [
                            str(target_tools.cc),
                            *target_cflags,
                            "-c",
                            str(generated_c),
                            "-o",
                            str(object_file),
                        ],
                        env=dict(target_environment),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise _build_error(
                        "CUDA generated-C target probe",
                        f"Could not run LLVM-MinGW: {exc}",
                        CATEGORY_TARGET_DATA,
                        "wsl_mingw_cuda_coff_probe_failed",
                    ) from exc
                if compile_result.returncode or not object_file.is_file():
                    detail = (compile_result.stdout + compile_result.stderr).strip()[-2000:]
                    raise _build_error(
                        "CUDA generated-C target probe",
                        "LLVM-MinGW failed to compile bin2c output to a target object."
                        + (f"\n{detail}" if detail else ""),
                        CATEGORY_TARGET_DATA,
                        "wsl_mingw_cuda_coff_probe_failed",
                    )
                _inspect_artifact(target_tools, object_file, executable=False)
                harness = scratch / "probe-main.c"
                harness.write_text(
                    "extern const unsigned char g3_probe[];\n"
                    "int main(void) { return g3_probe[0] == 0; }\n",
                    encoding="utf-8",
                )
                executable = scratch / "probe.exe"
                _run(
                    [
                        str(target_tools.cc),
                        *target_cflags,
                        str(harness),
                        str(object_file),
                        "-o",
                        str(executable),
                    ],
                    env=target_environment,
                )
                _inspect_artifact(target_tools, executable, executable=True)
                imports = _run(
                    [str(target_tools.objdump), "-p", str(executable)],
                    env=target_environment,
                ).stdout.lower()
                if ".so" in imports or "/usr/lib" in imports or "/opt/cuda" in imports:
                    raise _build_error(
                        "CUDA generated-C target probe",
                        "The PE link contains a Linux or CUDA SDK runtime dependency.",
                        CATEGORY_TARGET_DATA,
                        "wsl_mingw_cuda_target_link_contamination",
                    )
                outcomes["generated_c"] = generated_c.name
                outcomes["target_object"] = "COFF-x86-64"
                outcomes["target_executable"] = "PE32+ x86-64"
                outcomes["linux_runtime_leakage"] = False
        return outcomes


def _target_interface_projection(
    prefix: Path, preparation: WslMingwPreparation
) -> TargetInterfaceProjection:
    vulkan = preparation.vulkan
    shader_tools = {}
    if vulkan is not None:
        shader_tools = {
            name: str(path)
            for name, path in (
                ("shader_compiler", vulkan.shader_compiler_path),
                ("spirv_validator", vulkan.spirv_validator_path),
                ("spirv_optimizer", vulkan.spirv_optimizer_path),
            )
            if path is not None
        }
    return TargetInterfaceProjection(
        prefix=prefix,
        vulkan_headers=prefix / "include" / "vulkan",
        vulkan_registry=prefix / "share" / "vulkan" / "registry" / "vk.xml",
        vulkan_import_library=prefix / "lib" / "libvulkan.a",
        opencl_headers=prefix / "include" / "CL",
        opencl_import_library=prefix / "lib" / "libOpenCL.a",
        host_shader_tools=shader_tools,
        runtime_dependencies=(),
    )


def _build_tuple() -> str:
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if platform.system() != "Linux":
        raise _build_error(
            "WSL-Mingw cross environment",
            "linux-cross-mingw requires a Linux/WSL build machine.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_build_machine_unsupported",
        )
    return f"{machine}-pc-linux-gnu"


def _autotools_projection(
    environment: Mapping[str, str],
    build_tuple: str,
) -> AutotoolsProjection:
    host_tuple = TARGET_TRIPLE
    projected = {
        name: environment[name]
        for name in (
            "CC",
            "CXX",
            "AR",
            "RANLIB",
            "STRIP",
            "WINDRES",
            "RC",
            "PKG_CONFIG",
            "CPPFLAGS",
            "CFLAGS",
            "CXXFLAGS",
            "LDFLAGS",
            "LIBS",
            "PATH",
        )
    }
    return AutotoolsProjection(
        build_tuple=build_tuple,
        host_tuple=host_tuple,
        arguments=(f"--build={build_tuple}", f"--host={host_tuple}"),
        environment=projected,
    )


def _pkg_config_main(pkg_config: str, libdirs: Sequence[str], approved_roots: Sequence[str]) -> int:
    child_env = dict(os.environ)
    for name in tuple(child_env):
        if name.startswith("PKG_CONFIG_") or name in {
            "PKG_CONFIG_PATH",
            "PKG_CONFIG_LIBDIR",
            "PKG_CONFIG_SYSROOT_DIR",
            "PKG_CONFIG_DIR",
        }:
            child_env.pop(name, None)
    child_env["PKG_CONFIG_LIBDIR"] = os.pathsep.join(libdirs)
    child_env["PKG_CONFIG"] = pkg_config
    child_env.pop("PKG_CONFIG_SYSROOT_DIR", None)
    result = subprocess.run(
        [pkg_config, *sys.argv[1:]], env=child_env, capture_output=True, text=True, check=False
    )
    if result.returncode:
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode
    try:
        validate_pkg_config_output(result.stdout, approved_roots)
    except BuildError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return 0


def validate_pkg_config_output(output: str, approved_roots: Sequence[str | Path]) -> None:
    """Reject absolute target include/library paths outside approved roots."""
    roots = tuple(Path(root).resolve(strict=False) for root in approved_roots)

    def reject(path_text: str) -> None:
        if re.match(r"^[A-Za-z]:[\\/]", path_text):
            raise TargetDiscoveryError(
                path_text, "Windows/MSYS path is not a valid WSL target dependency path"
            )
        candidate = Path(path_text)
        if not candidate.is_absolute():
            return
        resolved = candidate.resolve(strict=False)
        if any(_inside(resolved, root) for root in roots):
            return
        raise TargetDiscoveryError(
            path_text,
            f"pkg-config returned a path outside approved roots "
            f"({', '.join(str(root) for root in roots)})",
        )

    tokens = shlex.split(output)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-I", "-L", "-isystem", "-iquote"):
            index += 1
            if index < len(tokens):
                reject(tokens[index])
        elif token.startswith(("-I", "-L", "-isystem", "-iquote")):
            for prefix in ("-isystem", "-iquote", "-I", "-L"):
                if token.startswith(prefix) and len(token) > len(prefix):
                    reject(token[len(prefix) :])
                    break
        elif token.startswith("-Wl,"):
            for item in token[4:].split(","):
                if item.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", item):
                    reject(item)
                embedded = re.search(r"(?:^|=)(/[^\s,;]*)", item)
                if embedded is not None:
                    reject(embedded.group(1))
        elif token.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", token):
            reject(token)
        else:
            embedded = re.search(r"(?:^|[=,:])(/[^\s,;]*)", token)
            if embedded is not None:
                reject(embedded.group(1))
        index += 1


def _write_pkg_config_wrapper(
    wrapper: Path,
    host_tools: HostTools,
    paths: CrossPaths,
    approved_roots: Sequence[Path],
) -> tuple[Path, ...]:
    libdirs = (
        paths.target_prefix / "lib" / "pkgconfig",
        paths.target_prefix / "lib64" / "pkgconfig",
        paths.target_prefix / "share" / "pkgconfig",
    )
    for directory in libdirs:
        directory.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parent
    roots_json = json.dumps([str(root) for root in approved_roots], separators=(",", ":"))
    dirs_json = json.dumps([str(directory) for directory in libdirs], separators=(",", ":"))
    content = (
        f"#!{host_tools['python']}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(source_root)!r})\n"
        "from ffmpeg_builder.cross_environment import _pkg_config_main\n"
        f"raise SystemExit(_pkg_config_main({host_tools['pkg-config']!r}, {dirs_json}, {roots_json}))\n"
    )
    wrapper.write_text(content, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return libdirs


def _cmake_toolchain_content(
    tools: TargetTools,
    paths: CrossPaths,
    roots: Sequence[Path],
) -> str:
    cmake_roots = ";".join(str(root) for root in roots)
    return (
        "set(CMAKE_SYSTEM_NAME Windows)\n"
        "set(CMAKE_SYSTEM_PROCESSOR x86_64)\n"
        f'set(CMAKE_C_COMPILER "{tools.cc}")\n'
        f'set(CMAKE_CXX_COMPILER "{tools.cxx}")\n'
        f'set(CMAKE_AR "{tools.ar}")\n'
        f'set(CMAKE_RANLIB "{tools.ranlib}")\n'
        f'set(CMAKE_RC_COMPILER "{tools.windres}")\n'
        f'set(CMAKE_STRIP "{tools.strip}")\n'
        f'set(CMAKE_FIND_ROOT_PATH "{cmake_roots}")\n'
        "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)\n"
        "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)\n"
        "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)\n"
        "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)\n"
        "set(CMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY TRUE)\n"
        "set(CMAKE_FIND_USE_PACKAGE_REGISTRY FALSE)\n"
        "set(CMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY FALSE)\n"
        f'set(CMAKE_PREFIX_PATH "{paths.target_prefix}")\n'
        f"set(PKG_CONFIG_EXECUTABLE \"{paths.generated_files_root / 'pkg-config'}\" CACHE FILEPATH \"target pkg-config\" FORCE)\n"
        "set(CMAKE_TRY_COMPILE_TARGET_TYPE EXECUTABLE)\n"
    )


def _meson_cross_content(tools: TargetTools, paths: CrossPaths) -> str:
    return (
        "[binaries]\n"
        f"c = {str(tools.cc)!r}\n"
        f"cpp = {str(tools.cxx)!r}\n"
        f"ar = {str(tools.ar)!r}\n"
        f"strip = {str(tools.strip)!r}\n"
        f"windres = {str(tools.windres)!r}\n"
        f"pkgconfig = {str(paths.generated_files_root / 'pkg-config')!r}\n"
        "\n[host_machine]\n"
        "system = 'windows'\n"
        "cpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\n"
        "endian = 'little'\n"
        "\n[properties]\n"
        "needs_exe_wrapper = true\n"
        "\n[built-in options]\n"
        f"prefix = {str(paths.target_prefix)!r}\n"
        "libdir = 'lib'\n"
        "includedir = 'include'\n"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(
    command: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise _build_error(
            "G3 configure/probe",
            f"Command failed ({result.returncode}): {shlex.join(command)}"
            + (f"\n{detail[-3000:]}" if detail else ""),
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cross_probe_failed",
        )
    return result


def _inspect_artifact(tools: TargetTools, artifact: Path, executable: bool) -> str:
    result = _run([str(tools.readobj), "--file-headers", str(artifact)])
    output = result.stdout + result.stderr
    if "COFF-x86-64" not in output or "ELF" in output:
        raise _build_error(
            "G3 artifact probe",
            f"Artifact is not Windows x86-64 COFF: {artifact}.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_artifact_format_invalid",
        )
    if executable and "Magic: 0x20B" not in output and "PE32+" not in output:
        raise _build_error(
            "G3 artifact probe",
            f"Linked output is not a PE executable: {artifact}.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_artifact_format_invalid",
        )
    return output


def _write_manifest(environment: CrossEnvironment, diagnostics: Mapping[str, Any]) -> Path:
    generated = (
        environment.pkg_config_wrapper,
        environment.cmake_toolchain_file,
        environment.meson_cross_file,
    )
    file_hashes = {path.name: _sha256(path.read_bytes()) for path in generated}
    payload = {
        "backend": environment.backend,
        "target_triple": environment.target_triple,
        "toolchain_provenance": dict(
            environment.target_input_manifest.get("toolchain_provenance", {})
        ),
        "workspace_root": str(environment.paths.workspace_root),
        "target_prefix": str(environment.paths.target_prefix),
        "generated_files": file_hashes,
        "target_tools": environment.target_tools.as_dict(),
        "host_tools": dict(sorted(environment.host_tools.paths.items())),
        "cpu_tuning_flags": list(environment.cpu_tuning_flags),
        "cargo_target": environment.rust_tools.target,
        "cuda_host_contract": dict(environment.cuda_acceptance),
        "approved_target_roots": [str(root) for root in environment.approved_target_roots],
        "pkg_config_policy": {
            "libdirs": [
                str(environment.paths.target_prefix / "lib" / "pkgconfig"),
                str(environment.paths.target_prefix / "lib64" / "pkgconfig"),
                str(environment.paths.target_prefix / "share" / "pkgconfig"),
            ],
            "sysroot": None,
        },
        "cmake_policy": {
            "root_path": [str(root) for root in environment.approved_target_roots],
            "program": "NEVER",
            "library": "ONLY",
            "include": "ONLY",
            "package": "ONLY",
        },
        "meson_policy": {
            "build_machine": environment.build_machine,
            "host_machine": environment.host_machine,
            "needs_exe_wrapper": True,
        },
        "target_input_materialization_id": environment.target_input_manifest.get(
            "materialization_id"
        ),
        "g4_boundary": [
            "full configured component stack and per-component adapters",
            "physical Windows runtime acceptance and packaging",
        ],
        "diagnostics": diagnostics,
    }
    path = environment.paths.diagnostics_root / "g3-environment.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def construct_cross_environment(
    preparation: WslMingwPreparation,
    config: BuildConfig,
    workspace: Path,
    source_archives_dir: Path,
    parent_environment: Optional[Mapping[str, str]] = None,
    registry: Optional[ComponentRegistry] = None,
) -> CrossEnvironment:
    """Create the identity-local target prefix, generated files, and typed context.

    Call only after G1/G2 have selected and authorized ``workspace``.
    """
    parent = dict(os.environ if parent_environment is None else parent_environment)
    workspace = Path(workspace).resolve()
    paths = CrossPaths(
        workspace_root=workspace,
        target_prefix=workspace / "prefix",
        build_root=workspace / "build",
        generated_files_root=workspace / "generated",
        packages_root=workspace / "packages",
        diagnostics_root=workspace / "diagnostics",
    )
    for path in (
        paths.target_prefix,
        paths.build_root,
        paths.generated_files_root,
        paths.packages_root,
        paths.diagnostics_root,
    ):
        if (
            path.is_symlink()
            or not _inside(path.parent, workspace)
            or (path.exists() and not _inside(path, workspace))
        ):
            raise _build_error(
                "G3 cross environment",
                f"Identity-local path is redirected outside the selected workspace: {path}.",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_cross_path_unsafe",
            )
    for directory in (
        paths.build_root,
        paths.generated_files_root,
        paths.packages_root,
        paths.diagnostics_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    target_tools = _resolve_target_tools(preparation)
    cpu_tuning_flags = CPU_TUNING_FLAGS if config.native_build else ()
    _validate_cpu_tuning(target_tools, cpu_tuning_flags, parent)
    host_tools = _resolve_host_tools(parent)
    rust_tools = _resolve_rust_tools(preparation, target_tools)
    cuda_tools = _cuda_host_tools(preparation)
    host_sdk_roots: list[Path] = []
    if preparation.cuda is not None:
        host_sdk_roots.append(Path(preparation.cuda.cuda_root))
    if preparation.vulkan is not None:
        host_sdk_roots.extend(
            (preparation.vulkan.selected_root, preparation.vulkan.effective_sdk_root)
        )
    host_environment = _host_environment(parent)
    host_environment["PATH"] = _host_tool_path(
        host_tools.paths, host_environment.get("PATH", os.defpath), host_sdk_roots
    )
    cuda_environment = _cuda_host_environment(cuda_tools, preparation, host_environment)
    input_identity = preparation.identity_summary or {}
    expected_materialization = input_identity.get("target_input_materialization_id")
    manifest_path = paths.target_prefix / "target-inputs.json"
    if paths.target_prefix.exists() and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_target_inputs_manifest(
                manifest, paths.target_prefix, allow_component_outputs=True
            )
        except (OSError, ValueError, BuildError) as exc:
            raise _build_error(
                "G3 target inputs",
                f"Existing target-prefix materialisation is invalid and was left untouched: "
                f"{paths.target_prefix} ({exc}).",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_target_inputs_invalid",
            ) from exc
    else:
        if paths.target_prefix.exists() and any(paths.target_prefix.iterdir()):
            raise _build_error(
                "G3 target inputs",
                f"Target prefix contains files but no trusted G2 target-input manifest: "
                f"{paths.target_prefix}. Existing data was left untouched.",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_target_inputs_manifest_missing",
            )
        if paths.target_prefix.exists():
            paths.target_prefix.rmdir()
        selected_registry = registry or ComponentRegistry()
        manifest = materialize_target_inputs(
            registry=selected_registry,
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            source_archives_dir=source_archives_dir,
            packages_dir=paths.packages_root,
            target_prefix=paths.target_prefix,
            toolchain_root=target_tools.prefix,
            allow_network_downloads=config.allow_network_downloads,
        )
        validate_target_inputs_manifest(manifest, paths.target_prefix)
    if not isinstance(expected_materialization, str) or (
        manifest.get("materialization_id") != expected_materialization
    ):
        raise _build_error(
            "G3 target inputs",
            "Materialized target inputs do not match the G2 semantic identity "
            f"(expected {expected_materialization!r}, got {manifest.get('materialization_id')!r}).",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_target_inputs_identity_mismatch",
        )

    approved_roots = tuple(
        root.resolve(strict=True)
        for root in (
            paths.target_prefix,
            target_tools.target_sysroot,
            target_tools.generic_headers.parent,
        )
    )
    external_roots: dict[str, Sequence[str | Path]] = {}
    if preparation.cuda is not None:
        external_roots["cuda"] = (preparation.cuda.cuda_root,)
    if preparation.vulkan is not None:
        external_roots["vulkan"] = (
            preparation.vulkan.selected_root,
            preparation.vulkan.effective_sdk_root,
        )
    filtered = filter_wsl_mingw_target_environment(
        parent,
        external_roots,
        target_input_roots=(paths.target_prefix,),
    )
    environment = dict(filtered.environment)
    for name in _TARGET_ENV_VARS:
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith("PKG_CONFIG_"):
            environment.pop(name, None)

    pkg_config_wrapper = paths.generated_files_root / "pkg-config"
    pkg_config_libdirs = _write_pkg_config_wrapper(
        pkg_config_wrapper, host_tools, paths, approved_roots
    )
    cmake_file = paths.generated_files_root / "cmake-toolchain.cmake"
    meson_file = paths.generated_files_root / "meson-cross.ini"
    cmake_file.write_text(
        _cmake_toolchain_content(target_tools, paths, approved_roots),
        encoding="utf-8",
    )
    meson_file.write_text(_meson_cross_content(target_tools, paths), encoding="utf-8")

    explicit_tools = target_tools.as_dict()
    include_flags = f"-I{paths.target_prefix / 'include'}"
    tuning_flags = " ".join(cpu_tuning_flags)
    openmp_flags = ("-fopenmp",) if config.openmp else ()
    target_cflags = " ".join(part for part in (tuning_flags, *openmp_flags, include_flags) if part)
    target_ldflags = " ".join(
        part
        for part in (
            f"-L{paths.target_prefix / 'lib'}",
            *openmp_flags,
        )
        if part
    )
    environment.update(
        {
            **explicit_tools,
            "CPP": f"{target_tools.cc} -E",
            "RC": str(target_tools.windres),
            "CFLAGS": target_cflags,
            "CXXFLAGS": target_cflags,
            "CPPFLAGS": include_flags,
            "LDFLAGS": target_ldflags,
            "LIBS": "",
            "PKG_CONFIG": str(pkg_config_wrapper),
            "PKG_CONFIG_LIBDIR": os.pathsep.join(str(item) for item in pkg_config_libdirs),
            "CMAKE_PREFIX_PATH": str(paths.target_prefix),
            "CMAKE_TOOLCHAIN_FILE": str(cmake_file),
            "CMAKE_FIND_ROOT_PATH": ";".join(str(root) for root in approved_roots),
            "MESON_CROSS_FILE": str(meson_file),
        }
    )
    # Host PATH remains usable but is never used to select target tools.
    if "PATH" in host_environment:
        environment["PATH"] = host_environment["PATH"]
    else:
        environment.pop("PATH", None)

    build_tuple = _build_tuple()
    autotools = _autotools_projection(environment, build_tuple)
    cargo_environment = dict(host_environment)
    cargo_environment.update(rust_tools.environment())
    rust_bin = str(rust_tools.cargo.parent)
    cargo_environment["PATH"] = rust_bin + (
        os.pathsep + cargo_environment["PATH"] if cargo_environment.get("PATH") else ""
    )
    target_suffix = rust_tools.target.replace("-", "_")
    cargo_environment.update(
        {
            "CARGO_HOME": str(paths.generated_files_root / "cargo-home"),
            "CARGO_TARGET_DIR": str(paths.build_root / "cargo-target"),
            "PKG_CONFIG": str(pkg_config_wrapper),
            "HOST_PKG_CONFIG": host_tools.paths["pkg-config"],
            "TARGET_PKG_CONFIG": str(pkg_config_wrapper),
            "TARGET_PKG_CONFIG_LIBDIR": os.pathsep.join(str(item) for item in pkg_config_libdirs),
            "PKG_CONFIG_ALLOW_CROSS": "1",
            f"CC_{target_suffix}": str(target_tools.cc),
            f"CXX_{target_suffix}": str(target_tools.cxx),
            f"AR_{target_suffix}": str(target_tools.ar),
            f"CFLAGS_{target_suffix}": target_cflags,
            f"CXXFLAGS_{target_suffix}": target_cflags,
        }
    )
    cargo_environment.pop("PKG_CONFIG_LIBDIR", None)
    cuda_acceptance = _validate_cuda_host_tools(
        cuda_tools, cuda_environment, paths.generated_files_root, target_tools, environment
    )
    discovery_environment = {
        name: environment[name]
        for name in (
            "PKG_CONFIG",
            "PKG_CONFIG_LIBDIR",
            "CMAKE_PREFIX_PATH",
            "CMAKE_TOOLCHAIN_FILE",
            "CMAKE_FIND_ROOT_PATH",
            "MESON_CROSS_FILE",
        )
    }
    diagnostics = tuple(
        CrossDiagnostic(item.variable, item.value, item.reason) for item in filtered.removed_paths
    )
    manifest_payload = dict(manifest)
    manifest_payload["toolchain_provenance"] = dict(
        preparation.diagnostic_only.get("project_toolchains", {})
    )
    result = CrossEnvironment(
        backend=TARGET_BACKEND_CROSS_MINGW,
        build_machine=build_tuple,
        host_machine="windows-x86_64",
        target_triple=TARGET_TRIPLE,
        paths=paths,
        host_tools=host_tools,
        target_tools=target_tools,
        cpu_tuning_flags=cpu_tuning_flags,
        host_environment=host_environment,
        compiler_environment=environment,
        target_discovery_environment=discovery_environment,
        pkg_config_wrapper=pkg_config_wrapper,
        cmake_toolchain_file=cmake_file,
        meson_cross_file=meson_file,
        target_input_manifest=manifest_payload,
        rust_tools=rust_tools,
        rust_environment=rust_tools.environment(),
        cargo_environment=cargo_environment,
        cuda_host_tools=cuda_tools,
        cuda_environment=cuda_environment,
        cuda_acceptance=cuda_acceptance,
        target_interfaces=_target_interface_projection(paths.target_prefix, preparation),
        autotools=autotools,
        approved_target_roots=approved_roots,
        diagnostics=diagnostics,
        manifest_path=paths.diagnostics_root / "g3-environment.json",
    )
    _write_manifest(
        result,
        {
            "filtered": [
                {"variable": item.variable, "value": item.value, "reason": item.reason}
                for item in diagnostics
            ]
        },
    )
    if dict(parent) != dict(os.environ if parent_environment is None else parent_environment):
        raise _build_error(
            "G3 environment",
            "Cross-environment construction modified its parent environment.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_parent_environment_mutated",
        )
    return result


def _write_pkg_fixtures(environment: CrossEnvironment, host_root: Path) -> tuple[Path, ...]:
    target_pc = environment.paths.target_prefix / "lib" / "pkgconfig"
    host_pc = host_root / "lib" / "pkgconfig"
    host_pc.mkdir(parents=True, exist_ok=True)
    files = (
        target_pc / "g3-probe.pc",
        target_pc / "g3-malicious.pc",
        host_pc / "g3-probe.pc",
        host_pc / "g3-host-only.pc",
    )
    target_prefix = environment.paths.target_prefix
    (target_pc / "g3-probe.pc").write_text(
        "prefix=" + str(target_prefix) + "\n"
        "Name: g3-probe\nDescription: target fixture\nVersion: 1.0\n"
        "Cflags: -I${prefix}/include\n"
        "Libs: -L${prefix}/lib -lg3probe -lvulkan\n",
        encoding="utf-8",
    )
    (target_pc / "g3-malicious.pc").write_text(
        "Name: g3-malicious\nDescription: contamination fixture\nVersion: 1.0\n"
        "Cflags: -I/opt/cuda/include\nLibs: -L/usr/lib\n",
        encoding="utf-8",
    )
    (host_pc / "g3-probe.pc").write_text(
        "Name: g3-probe\nDescription: host fixture\nVersion: 9.9\n"
        "Cflags: -I/usr/include\nLibs: -L/usr/lib -lhost\n",
        encoding="utf-8",
    )
    (host_pc / "g3-host-only.pc").write_text(
        "Name: g3-host-only\nDescription: host-only fixture\nVersion: 1.0\n"
        "Cflags: -I/usr/include\nLibs: -L/usr/lib -lhost-only\n",
        encoding="utf-8",
    )
    return files


def _run_pkg_config_probes(
    environment: CrossEnvironment, scratch: Path, fixtures: Sequence[Path]
) -> dict[str, Any]:
    host_root = scratch / "host-pkgconfig"
    env = dict(environment.compiler_environment)
    env["PKG_CONFIG_PATH"] = str(host_root / "lib" / "pkgconfig")
    outcomes: dict[str, Any] = {}
    found = _run(
        [str(environment.pkg_config_wrapper), "--cflags", "--libs", "g3-probe"],
        env=env,
    )
    if str(environment.paths.target_prefix) not in found.stdout:
        raise _build_error(
            "Target pkg-config acceptance",
            f"Target fixture did not resolve under the target prefix: {found.stdout.strip()}",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_pkg_config_target_missing",
        )
    outcomes["target_package"] = found.stdout.strip()
    missing = subprocess.run(
        [str(environment.pkg_config_wrapper), "--exists", "g3-host-only"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if missing.returncode == 0:
        raise _build_error(
            "Target pkg-config acceptance",
            "A host-only package was visible through target pkg-config.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_pkg_config_host_fallback",
        )
    outcomes["host_fallback_blocked"] = True
    malicious = subprocess.run(
        [str(environment.pkg_config_wrapper), "--cflags", "--libs", "g3-malicious"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if malicious.returncode == 0 or "contamination" not in malicious.stderr.lower():
        raise _build_error(
            "Target pkg-config acceptance",
            "A target .pc file with host/SDK paths was not rejected.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_pkg_config_contamination_not_rejected",
        )
    outcomes["malicious_path_rejected"] = malicious.stderr.strip()
    return outcomes


def _run_compiler_probes(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    tools = environment.target_tools
    env = environment.compiler_environment
    source_c = scratch / "probe.c"
    source_c.write_text('#include <stdio.h>\nint main(void) { puts("g3"); return 0; }\n')
    source_cpp = scratch / "probe.cpp"
    source_cpp.write_text(
        '#include <string>\nint main() { std::string value("g3"); return value.size() != 2; }\n'
    )
    c_object, cpp_object = scratch / "probe-c.o", scratch / "probe-cpp.o"
    c_exe, cpp_exe = scratch / "probe-c.exe", scratch / "probe-cpp.exe"
    _run([str(tools.cc), "-c", str(source_c), "-o", str(c_object)], env=env)
    _run([str(tools.cc), str(source_c), "-o", str(c_exe)], env=env)
    _run([str(tools.cxx), "-c", str(source_cpp), "-o", str(cpp_object)], env=env)
    _run([str(tools.cxx), str(source_cpp), "-o", str(cpp_exe)], env=env)
    _inspect_artifact(tools, c_object, executable=False)
    _inspect_artifact(tools, cpp_object, executable=False)
    _inspect_artifact(tools, c_exe, executable=True)
    _inspect_artifact(tools, cpp_exe, executable=True)

    lib_source = scratch / "archive.c"
    lib_source.write_text("int g3_value(void) { return 7; }\n")
    lib_object, archive = scratch / "archive.o", scratch / "libg3.a"
    _run([str(tools.cc), "-c", str(lib_source), "-o", str(lib_object)], env=env)
    _run([str(tools.ar), "rcs", str(archive), str(lib_object)], env=env)
    _run([str(tools.ranlib), str(archive)], env=env)
    archive_main = scratch / "archive-main.c"
    archive_main.write_text("int g3_value(void); int main(void) { return g3_value() != 7; }\n")
    archive_exe = scratch / "archive.exe"
    _run(
        [str(tools.cc), str(archive_main), str(archive), "-o", str(archive_exe)],
        env=env,
    )
    _inspect_artifact(tools, lib_object, executable=False)
    _inspect_artifact(tools, archive_exe, executable=True)

    resource = scratch / "probe.rc"
    resource.write_text('1 RCDATA { "g3" }\n')
    resource_object, resource_exe = scratch / "resource.o", scratch / "resource.exe"
    _run(
        [str(tools.windres), str(resource), "-O", "coff", "-o", str(resource_object)],
        env=env,
    )
    _run(
        [str(tools.cc), str(source_c), str(resource_object), "-o", str(resource_exe)],
        env=env,
    )
    _inspect_artifact(tools, resource_object, executable=False)
    _inspect_artifact(tools, resource_exe, executable=True)

    imports = _run([str(tools.objdump), "-p", str(c_exe)], env=env).stdout.lower()
    if "api-ms-win-crt-" not in imports and "ucrtbase.dll" not in imports:
        raise _build_error(
            "G3 artifact probe",
            "The linked Windows executable does not show the expected UCRT/API-set imports.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_ucrt_import_missing",
        )
    return {
        "c": str(c_exe),
        "cxx": str(cpp_exe),
        "static_archive": str(archive),
        "resource": str(resource_exe),
        "ucrt_import": "api-ms-win-crt-*",
    }


def _run_cmake_probe(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    project = scratch / "cmake-project"
    build = scratch / "cmake-build"
    (project / "src").mkdir(parents=True)
    host_prefix = scratch / "host-cmake-prefix"
    host_config = host_prefix / "lib" / "cmake" / "G3HostOnly"
    host_config.mkdir(parents=True)
    (host_config / "G3HostOnlyConfig.cmake").write_text(
        "set(G3HostOnly_FOUND TRUE)\n", encoding="utf-8"
    )
    (project / "main.c").write_text(
        "#include <vulkan/vulkan.h>\n"
        "int g3_probe(void);\n"
        "int main(void) { (void)vkGetInstanceProcAddr(VK_NULL_HANDLE, "
        '"vkCreateInstance"); return g3_probe(); }\n',
        encoding="utf-8",
    )
    (project / "main.cpp").write_text(
        "#include <CL/cl.h>\n"
        "int main() { cl_uint count = 0; "
        "return clGetPlatformIDs(0, nullptr, &count); }\n",
        encoding="utf-8",
    )
    (project / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(g3_probe C CXX)\n"
        "find_path(VULKAN_INCLUDE_DIR vulkan/vulkan.h REQUIRED)\n"
        "find_path(OPENCL_INCLUDE_DIR CL/cl.h REQUIRED)\n"
        "find_library(VULKAN_IMPORT_LIBRARY vulkan REQUIRED)\n"
        "find_library(OPENCL_IMPORT_LIBRARY OpenCL REQUIRED)\n"
        "find_package(PkgConfig REQUIRED)\n"
        "pkg_check_modules(G3_PROBE REQUIRED IMPORTED_TARGET g3-probe)\n"
        "find_package(G3HostOnly CONFIG QUIET)\n"
        'if(G3HostOnly_FOUND)\n  message(FATAL_ERROR "host-only target package was found")\nendif()\n'
        "add_executable(g3_c main.c)\n"
        "add_executable(g3_cpp main.cpp)\n"
        "target_include_directories(g3_c PRIVATE ${VULKAN_INCLUDE_DIR})\n"
        "target_include_directories(g3_cpp PRIVATE ${OPENCL_INCLUDE_DIR})\n"
        "target_link_libraries(g3_c PRIVATE ${VULKAN_IMPORT_LIBRARY})\n"
        "target_link_libraries(g3_c PRIVATE PkgConfig::G3_PROBE)\n"
        "target_link_libraries(g3_cpp PRIVATE ${OPENCL_IMPORT_LIBRARY})\n",
        encoding="utf-8",
    )
    env = dict(environment.compiler_environment)
    env["PKG_CONFIG"] = str(environment.pkg_config_wrapper)
    _run(
        [
            environment.host_tools["cmake"],
            "-S",
            str(project),
            "-B",
            str(build),
            "-G",
            "Ninja",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            f"-DCMAKE_TOOLCHAIN_FILE={environment.cmake_toolchain_file}",
            f"-DCMAKE_PREFIX_PATH={host_prefix}",
        ],
        env=env,
    )
    info = _run([environment.host_tools["cmake"], "--build", str(build)], env=env)
    c_exe, cpp_exe = build / "g3_c.exe", build / "g3_cpp.exe"
    _inspect_artifact(environment.target_tools, c_exe, executable=True)
    _inspect_artifact(environment.target_tools, cpp_exe, executable=True)
    compile_commands = json.loads((build / "compile_commands.json").read_text(encoding="utf-8"))
    if len(compile_commands) != 2 or any(
        any(
            shlex.split(entry["command"]).count(flag) != int(flag in environment.cpu_tuning_flags)
            for flag in CPU_TUNING_FLAGS
        )
        for entry in compile_commands
    ):
        raise _build_error(
            "G3 CMake acceptance",
            "CMake did not apply each target CPU tuning flag exactly once to C and C++.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cmake_target_flags_invalid",
        )
    compiler_files = list((build / "CMakeFiles").rglob("CMakeCCompiler.cmake"))
    cxx_compiler_files = list((build / "CMakeFiles").rglob("CMakeCXXCompiler.cmake"))
    c_compiler_record = compiler_files[0].read_text(encoding="utf-8") if compiler_files else ""
    cxx_compiler_record = (
        cxx_compiler_files[0].read_text(encoding="utf-8") if cxx_compiler_files else ""
    )
    if (
        f'set(CMAKE_C_COMPILER "{environment.target_tools.cc}")' not in c_compiler_record
        or f'set(CMAKE_CXX_COMPILER "{environment.target_tools.cxx}")' not in cxx_compiler_record
    ):
        raise _build_error(
            "G3 CMake acceptance",
            "CMake did not retain the explicit LLVM-MinGW C/C++ compiler entrypoints.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cmake_compiler_mismatch",
        )
    target_discovery = c_compiler_record + cxx_compiler_record
    for forbidden in (
        "/usr/include",
        "/usr/lib",
        "/lib/x86_64-linux",
        "/opt/cuda",
        "/opt/vulkan-sdk",
    ):
        if forbidden in target_discovery:
            raise _build_error(
                "G3 CMake acceptance",
                f"CMake compiler discovery leaked a host/SDK target path: {forbidden}.",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_cmake_host_path_leak",
            )
    return {
        "configured": True,
        "built": True,
        "executables": [str(c_exe), str(cpp_exe)],
        "host_only_package_blocked": True,
        "output": info.stdout[-1200:],
    }


def _run_meson_probe(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    project = scratch / "meson-project"
    build = scratch / "meson-build"
    project.mkdir()
    (project / "main.c").write_text(
        "#include <vulkan/vulkan.h>\n"
        "int g3_probe(void);\n"
        "int main(void) { (void)vkGetInstanceProcAddr(VK_NULL_HANDLE, "
        '"vkCreateInstance"); return g3_probe(); }\n',
        encoding="utf-8",
    )
    (project / "meson.build").write_text(
        "project('g3_probe', 'c', default_options: ['warning_level=0'])\n"
        "probe = dependency('g3-probe', method: 'pkg-config', required: true)\n"
        "executable('g3_meson', 'main.c', dependencies: probe)\n",
        encoding="utf-8",
    )
    env = dict(environment.compiler_environment)
    env["PKG_CONFIG_PATH"] = ""
    _run(
        [
            environment.host_tools["meson"],
            "setup",
            str(build),
            str(project),
            "--cross-file",
            str(environment.meson_cross_file),
            "--backend",
            "ninja",
        ],
        env=env,
    )
    _run([environment.host_tools["meson"], "compile", "-C", str(build)], env=env)
    command_lines = _run(
        [environment.host_tools["ninja"], "-C", str(build), "-t", "commands"], env=env
    ).stdout.splitlines()
    if len(command_lines) != 2 or any(
        any(
            shlex.split(line).count(flag) != int(flag in environment.cpu_tuning_flags)
            for flag in CPU_TUNING_FLAGS
        )
        for line in command_lines
    ):
        raise _build_error(
            "G3 Meson acceptance",
            "Meson did not apply each target CPU tuning flag exactly once.\n"
            f"Commands:\n{chr(10).join(command_lines)[-3000:]}",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_meson_target_flags_invalid",
        )
    machines = json.loads(
        _run(
            [environment.host_tools["meson"], "introspect", "--machines", str(build)],
            env=env,
        ).stdout
    )
    host = machines.get("host", {})
    build_machine = machines.get("build", {})
    if (
        host.get("system") != "windows"
        or host.get("cpu_family") != "x86_64"
        or build_machine.get("system") != "linux"
    ):
        raise _build_error(
            "G3 Meson acceptance",
            f"Meson reported unexpected build/host machines: {build_machine} / {host}.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_meson_machine_mismatch",
        )
    executable = build / "g3_meson.exe"
    _inspect_artifact(environment.target_tools, executable, executable=True)
    return {
        "configured": True,
        "built": True,
        "build_machine": build_machine,
        "host_machine": host,
        "executable": str(executable),
    }


def _run_autotools_probe(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    project = scratch / "autotools-project"
    project.mkdir()
    (project / "configure.ac").write_text(
        "AC_INIT([g3-probe], [1.0])\n"
        "AC_CONFIG_SRCDIR([main.c])\n"
        "AC_CANONICAL_BUILD\n"
        "AC_CANONICAL_HOST\n"
        "AC_PROG_CC\n"
        "AC_PROG_CXX\n"
        "AC_MSG_CHECKING([target pkg-config])\n"
        "if $PKG_CONFIG --exists g3-probe; then AC_MSG_RESULT([yes]); "
        "else AC_MSG_ERROR([target pkg-config unavailable]); fi\n"
        "AC_RUN_IFELSE([AC_LANG_PROGRAM([], [])], "
        "[AC_MSG_ERROR([unexpected target execution])], [], "
        "[AC_MSG_NOTICE([cross compile confirmed])])\n"
        "AC_OUTPUT\n",
        encoding="utf-8",
    )
    (project / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _run([environment.host_tools["autoreconf"], "-fi"], cwd=project)
    env = dict(environment.autotools.environment)
    env["PKG_CONFIG_PATH"] = ""
    _run(
        [
            str(project / "configure"),
            *environment.autotools.arguments,
        ],
        cwd=project,
        env=env,
    )
    config_log = (project / "config.log").read_text(encoding="utf-8")
    if "cross_compiling=yes" not in config_log and "cross compiling... yes" not in config_log:
        # Autoconf versions differ in config.log formatting; the run-test branch
        # is the stronger assertion that it recognized the distinct host tuple.
        if "cross compile confirmed" not in config_log:
            raise _build_error(
                "G3 Autotools acceptance",
                "The configure fixture did not enter cross-compiling mode.",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_autotools_not_cross_compiling",
            )
    return {
        "configured": True,
        "build_tuple": environment.autotools.build_tuple,
        "host_tuple": environment.autotools.host_tuple,
        "cross_compiling": True,
        "pkg_config": environment.autotools.environment["PKG_CONFIG"],
    }


def _run_mixed_role_probe(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    host_environment = dict(environment.host_environment)
    if any(name in host_environment for name in ("CC", "CFLAGS", "CXXFLAGS", "RUSTFLAGS")):
        raise _build_error(
            "G3 mixed-role acceptance",
            "Host-tool environment contains target compiler or tuning variables.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_mixed_host_environment_contaminated",
        )
    generator_source = scratch / "host-generator.c"
    generator_source.write_text(
        "#include <stdio.h>\n"
        "int main(int argc, char **argv) {\n"
        "  if (argc != 2) return 2;\n"
        '  FILE *out = fopen(argv[1], "w");\n'
        "  if (!out) return 3;\n"
        '  fputs("int g3_generated_value(void) { return 73; }\\n", out);\n'
        "  return fclose(out) != 0;\n"
        "}\n",
        encoding="utf-8",
    )
    generator = scratch / "host-generator"
    _run(
        [environment.host_tools["cc"], str(generator_source), "-o", str(generator)],
        env=host_environment,
    )
    host_headers = _run(["readelf", "-h", str(generator)], env=host_environment).stdout
    if "ELF64" not in host_headers or "Advanced Micro Devices X86-64" not in host_headers:
        raise _build_error(
            "G3 mixed-role acceptance",
            "Host generator is not a WSL-runnable x86-64 ELF executable.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_mixed_host_artifact_invalid",
        )
    generated_source = scratch / "generated-target.c"
    _run([str(generator), str(generated_source)], env=host_environment)
    generated_text = generated_source.read_text(encoding="utf-8")
    if generated_text != "int g3_generated_value(void) { return 73; }\n":
        raise _build_error(
            "G3 mixed-role acceptance",
            "The host generator did not emit the expected source-only boundary artifact.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_mixed_generated_source_invalid",
        )
    target_flags = shlex.split(environment.compiler_environment.get("CFLAGS", ""))
    target_object = scratch / "generated-target.o"
    target_executable = scratch / "generated-target.exe"
    target_main = scratch / "target-main.c"
    target_main.write_text(
        "int g3_generated_value(void);\n" "int main(void) { return g3_generated_value() != 73; }\n",
        encoding="utf-8",
    )
    _run(
        [
            str(environment.target_tools.cc),
            *target_flags,
            "-c",
            str(generated_source),
            "-o",
            str(target_object),
        ],
        env=environment.compiler_environment,
    )
    _run(
        [
            str(environment.target_tools.cc),
            *target_flags,
            str(target_main),
            str(target_object),
            "-o",
            str(target_executable),
        ],
        env=environment.compiler_environment,
        timeout=120,
    )
    _inspect_artifact(environment.target_tools, target_object, executable=False)
    _inspect_artifact(environment.target_tools, target_executable, executable=True)
    return {
        "host_generator": "ELF64 x86-64 and executed on WSL",
        "generated_boundary": "plain C source",
        "target_object": "COFF-x86-64",
        "target_executable": "PE32+ x86-64",
        "target_compiler": str(environment.target_tools.cc),
        "host_compiler": environment.host_tools["cc"],
        "host_target_library_leakage": False,
    }


def _run_role_environment_probe(environment: CrossEnvironment) -> dict[str, Any]:
    from .builder import FFmpegBuilder
    from .components import BuildRole, BuildSystem, Component, ComponentCategory
    from .platforms.linux_cross_mingw import LinuxCrossMingwPlatform

    builder = object.__new__(FFmpegBuilder)
    builder.platform_strategy = LinuxCrossMingwPlatform()
    builder.platform_context = type("Context", (), {"cross_environment": environment})()
    builder.toolchain_env = {}
    builder.config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)
    builder.env = {}
    roles = {
        system: Component(
            name=f"g3-{system.value}",
            version="1",
            url="https://invalid.example/g3-probe.tar.gz",
            category=ComponentCategory.BUILD_TOOL,
            build_system=system,
            build_role=BuildRole.TARGET,
        )
        for system in (
            BuildSystem.AUTOTOOLS,
            BuildSystem.CMAKE,
            BuildSystem.MESON,
            BuildSystem.MAKE_ONLY,
        )
    }
    host_component = Component(
        name="g3-host-generator",
        version="1",
        url="https://invalid.example/g3-generator.tar.gz",
        category=ComponentCategory.BUILD_TOOL,
        build_system=BuildSystem.CUSTOM,
        build_role=BuildRole.HOST,
    )
    mixed_component = Component(
        name="g3-mixed-recipe",
        version="1",
        url="https://invalid.example/g3-mixed.tar.gz",
        category=ComponentCategory.OTHER_LIB,
        build_system=BuildSystem.CUSTOM,
        build_role=BuildRole.MIXED,
    )
    cargo_component = Component(
        name="g3-cargo-target",
        version="1",
        url="https://invalid.example/g3-cargo.tar.gz",
        category=ComponentCategory.OTHER_LIB,
        build_system=BuildSystem.CARGO,
        build_role=BuildRole.TARGET,
    )
    target_environments = {
        system.value: builder.get_build_env(component) for system, component in roles.items()
    }
    for label, projected in target_environments.items():
        if (
            projected.get("CC") != str(environment.target_tools.cc)
            or any(
                shlex.split(projected.get("CFLAGS", "")).count(flag)
                != int(flag in environment.cpu_tuning_flags)
                for flag in CPU_TUNING_FLAGS
            )
            or projected.get("PKG_CONFIG") != str(environment.pkg_config_wrapper)
        ):
            raise _build_error(
                "G3 builder role acceptance",
                f"The {label} target helper did not consume the target compiler, flags, "
                "and target pkg-config projection.",
                CATEGORY_TARGET_DATA,
                "wsl_mingw_builder_target_projection_invalid",
            )
    host_environment = builder.get_build_env(host_component)
    if (
        "CC" in host_environment
        or "PKG_CONFIG" in host_environment
        or "-march=native" in host_environment.get("CFLAGS", "")
        or "-mtune=native" in host_environment.get("CFLAGS", "")
        or "-march=native" in host_environment.get("CXXFLAGS", "")
        or "-mtune=native" in host_environment.get("CXXFLAGS", "")
    ):
        raise _build_error(
            "G3 builder role acceptance",
            "HOST_TOOL received target compiler, flags, or pkg-config settings.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_builder_host_projection_invalid",
        )
    cargo_environment = builder.get_build_env(cargo_component)
    if (
        cargo_environment.get("CARGO_BUILD_TARGET") != environment.rust_tools.target
        or cargo_environment.get("RUSTC") != str(environment.rust_tools.rustc)
        or "CFLAGS" in cargo_environment
        or "RUSTFLAGS" in cargo_environment
        or not _inside(
            Path(cargo_environment["CARGO_TARGET_DIR"]), environment.paths.workspace_root
        )
    ):
        raise _build_error(
            "G3 Cargo builder acceptance",
            "Cargo does not use the project-managed target contract and identity-local target dir.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cargo_builder_projection_invalid",
        )
    try:
        builder.get_build_env(mixed_component)
    except BuildError as exc:
        if exc.code != "wsl_mingw_mixed_role_requires_recipe":
            raise
    else:
        raise _build_error(
            "G3 mixed-role builder acceptance",
            "MIXED components must not receive one ambiguous shared environment.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_mixed_role_projection_ambiguous",
        )
    mixed_host, mixed_target = builder.get_mixed_build_environments(mixed_component)
    if (
        "CC" in mixed_host
        or "-march=native" in mixed_host.get("CFLAGS", "")
        or mixed_target.get("CC") != str(environment.target_tools.cc)
    ):
        raise _build_error(
            "G3 mixed-role builder acceptance",
            "MIXED recipe contexts did not separate host and target tools.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_mixed_role_projection_invalid",
        )
    return {
        "target_build_systems": sorted(target_environments),
        "host_tool": "host environment and host prefix",
        "mixed": "separate host and target environments",
        "cargo": "project-managed target tools and identity-local Cargo target directory",
        "msys_semantics": False,
    }


def _run_cargo_probe(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    project = scratch / "cargo-project"
    source_dir = project / "src"
    source_dir.mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        '[package]\nname = "g3_rust_probe"\nversion = "0.1.0"\nedition = "2021"\n'
        '[lib]\ncrate-type = ["staticlib"]\n'
        'build = "build.rs"\n',
        encoding="utf-8",
    )
    (project / "build.rs").write_text(
        "fn main() {\n"
        '  let host = std::env::var("HOST").expect("Cargo HOST");\n'
        '  let target = std::env::var("TARGET").expect("Cargo TARGET");\n'
        '  assert_ne!(host, target, "Cargo build script must run for the host");\n'
        '  println!("cargo:warning=G3_HOST_TARGET={host}|{target}");\n'
        "}\n",
        encoding="utf-8",
    )
    (source_dir / "lib.rs").write_text(
        "#[no_mangle]\n" 'pub extern "C" fn g3_rust_value() -> i32 { 73 }\n',
        encoding="utf-8",
    )
    cargo_environment = dict(environment.cargo_environment)
    cargo_environment["CARGO_HOME"] = str(scratch / "cargo-home")
    cargo_environment["CARGO_TARGET_DIR"] = str(scratch / "cargo-target")
    cargo_environment["CARGO_NET_OFFLINE"] = "true"
    if any(key in cargo_environment for key in ("CC", "CFLAGS", "CXXFLAGS", "RUSTFLAGS")):
        raise _build_error(
            "G3 Cargo acceptance",
            "Cargo's host environment contains generic target compiler or tuning variables.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cargo_host_environment_contaminated",
        )
    result = _run(
        [
            str(environment.rust_tools.cargo),
            "build",
            "--offline",
            "--release",
            "--target",
            environment.rust_tools.target,
            "--manifest-path",
            str(project / "Cargo.toml"),
            "-vv",
        ],
        env=cargo_environment,
        timeout=180,
    )
    output = result.stdout + result.stderr
    host_target_line = next((line for line in output.splitlines() if "G3_HOST_TARGET=" in line), "")
    if not host_target_line or environment.rust_tools.target not in host_target_line:
        raise _build_error(
            "G3 Cargo acceptance",
            "Cargo did not run its build script for the host while targeting Windows.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cargo_host_target_boundary_invalid",
        )
    archive = (
        Path(cargo_environment["CARGO_TARGET_DIR"])
        / environment.rust_tools.target
        / "release"
        / "libg3_rust_probe.a"
    )
    if not archive.is_file():
        raise _build_error(
            "G3 Cargo acceptance",
            "Cargo did not produce the expected Windows static library.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cargo_staticlib_missing",
        )
    archive_headers = _run(
        [str(environment.target_tools.readobj), "--file-headers", str(archive)],
        env=environment.compiler_environment,
    ).stdout
    if "ELF" in archive_headers or "COFF-x86-64" not in archive_headers:
        raise _build_error(
            "G3 Cargo acceptance",
            "The Rust static library contains non-Windows or non-x86-64 objects.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cargo_staticlib_format_invalid",
        )
    harness = scratch / "rust-harness.c"
    harness.write_text(
        "extern int g3_rust_value(void);\n" "int main(void) { return g3_rust_value() != 73; }\n",
        encoding="utf-8",
    )
    executable = scratch / "rust-harness.exe"
    _run(
        [
            str(environment.target_tools.cc),
            *shlex.split(environment.compiler_environment.get("CFLAGS", "")),
            str(harness),
            str(archive),
            "-o",
            str(executable),
        ],
        env=environment.compiler_environment,
    )
    _inspect_artifact(environment.target_tools, executable, executable=True)
    return {
        "cargo": str(environment.rust_tools.cargo),
        "rustc": str(environment.rust_tools.rustc),
        "target": environment.rust_tools.target,
        "target_std": str(environment.rust_tools.target_std),
        "identity_local_cargo_home": cargo_environment["CARGO_HOME"],
        "identity_local_target_dir": cargo_environment["CARGO_TARGET_DIR"],
        "build_script_host_target": host_target_line.rsplit("G3_HOST_TARGET=", 1)[-1],
        "staticlib": "COFF-x86-64",
        "c_harness": "PE32+ x86-64",
        "ambient_rustup_used": False,
    }


def _run_cpp_metadata_probe(environment: CrossEnvironment, scratch: Path) -> dict[str, Any]:
    include_flags = shlex.split(environment.compiler_environment.get("CXXFLAGS", ""))
    cc = str(environment.target_tools.cc)
    cxx = str(environment.target_tools.cxx)
    source_b = scratch / "cpp-b.cpp"
    source_a = scratch / "cpp-a.cpp"
    source_main = scratch / "cpp-main.c"
    source_b.write_text(
        '#include <string>\nextern "C" int g3_b_value(void) '
        '{ std::string text("g3"); return static_cast<int>(text.size()); }\n',
        encoding="utf-8",
    )
    source_a.write_text(
        'extern "C" int g3_b_value(void);\n'
        'extern "C" int g3_a_value(void) { return g3_b_value(); }\n',
        encoding="utf-8",
    )
    source_main.write_text(
        "extern int g3_a_value(void);\n" "int main(void) { return g3_a_value() != 2; }\n",
        encoding="utf-8",
    )
    object_b, object_a = scratch / "cpp-b.o", scratch / "cpp-a.o"
    archive_b, archive_a = scratch / "libg3b.a", scratch / "libg3a.a"
    _run(
        [cxx, *include_flags, "-c", str(source_b), "-o", str(object_b)],
        env=environment.compiler_environment,
    )
    _run(
        [str(environment.target_tools.ar), "rcs", str(archive_b), str(object_b)],
        env=environment.compiler_environment,
    )
    _run(
        [cxx, *include_flags, "-c", str(source_a), "-o", str(object_a)],
        env=environment.compiler_environment,
    )
    _run(
        [str(environment.target_tools.ar), "rcs", str(archive_a), str(object_a)],
        env=environment.compiler_environment,
    )

    metadata_a = TargetLinkMetadata(
        library_roots=(str(scratch),),
        public_link_requirements=("-lg3b",),
        provenance=("G3 static A-to-B fixture",),
    )
    metadata_b = TargetLinkMetadata(
        private_transitive_link_requirements=("-lc++", "-lunwind"),
        runtime_dependencies=(
            RuntimeDependency("libc++.dll", "target-runtime", "llvm-mingw-ucrt"),
            RuntimeDependency("libunwind.dll", "target-runtime", "llvm-mingw-ucrt"),
        ),
        provenance=("G3 static C++ runtime fixture",),
    )
    metadata = compose_target_link_metadata(metadata_a, metadata_b)
    roots = tuple(f"-L{root}" for root in metadata.library_roots)
    executable = scratch / "cpp-metadata.exe"
    link_command = [
        cc,
        *shlex.split(environment.compiler_environment.get("CFLAGS", "")),
        str(source_main),
        str(archive_a),
        *roots,
        *metadata.consumer_link_requirements,
        "-o",
        str(executable),
    ]
    _run(link_command, env=environment.compiler_environment)
    _inspect_artifact(environment.target_tools, executable, executable=True)
    negative = subprocess.run(
        [
            cc,
            *shlex.split(environment.compiler_environment.get("CFLAGS", "")),
            str(source_main),
            str(archive_a),
            *roots,
            "-lg3b",
            "-o",
            str(scratch / "cpp-metadata-negative.exe"),
        ],
        env=dict(environment.compiler_environment),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if negative.returncode == 0 or not any(
        marker in negative.stderr for marker in ("std::__1", "_ZNSt3__1", "operator new")
    ):
        raise _build_error(
            "G3 target-link metadata acceptance",
            "Removing transitive C++ runtime metadata did not fail for a missing C++ runtime "
            f"symbol as expected.\n{(negative.stdout + negative.stderr)[-2000:]}",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cpp_link_negative_case_invalid",
        )
    imports = _run(
        [str(environment.target_tools.objdump), "-p", str(executable)],
        env=environment.compiler_environment,
    ).stdout
    if any(value in imports.lower() for value in (".so", "/usr/lib", "/opt/cuda")):
        raise _build_error(
            "G3 target-link metadata acceptance",
            "The PE output imports a Linux or CUDA SDK runtime dependency.",
            CATEGORY_TARGET_DATA,
            "wsl_mingw_cpp_link_contamination",
        )
    return {
        "transitive_requirements": list(metadata.consumer_link_requirements),
        "runtime_dependencies": [
            {
                "name": item.name,
                "classification": item.classification,
                "source_identity": item.source_identity,
            }
            for item in metadata.runtime_dependencies
        ],
        "positive_link": "PE32+ x86-64",
        "negative_link": negative.stderr[-1500:],
        "runtime_imports": [
            line.strip() for line in imports.splitlines() if ".dll" in line.lower()
        ],
        "linux_runtime_leakage": False,
    }


def run_cross_environment_acceptance(environment: CrossEnvironment) -> Mapping[str, Any]:
    """Run isolated compiler, build-system, role, Rust, and target-link probes."""
    root = environment.paths.diagnostics_root
    outcome: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="g3-acceptance-", dir=root) as temporary:
        scratch = Path(temporary)
        fixtures = _write_pkg_fixtures(environment, scratch / "host-pkgconfig")
        probe_source = scratch / "g3probe.c"
        probe_source.write_text("int g3_probe(void) { return 0; }\n", encoding="utf-8")
        probe_object = scratch / "g3probe.o"
        probe_library = environment.paths.target_prefix / "lib" / "libg3probe.a"
        _run(
            [
                str(environment.target_tools.cc),
                *shlex.split(environment.compiler_environment.get("CFLAGS", "")),
                "-c",
                str(probe_source),
                "-o",
                str(probe_object),
            ],
            env=environment.compiler_environment,
        )
        _run(
            [str(environment.target_tools.ar), "rcs", str(probe_library), str(probe_object)],
            env=environment.compiler_environment,
        )
        try:
            outcome["pkg_config"] = _run_pkg_config_probes(environment, scratch, fixtures)
            outcome["compiler"] = _run_compiler_probes(environment, scratch)
            outcome["builder_roles"] = _run_role_environment_probe(environment)
            outcome["cmake"] = _run_cmake_probe(environment, scratch)
            outcome["meson"] = _run_meson_probe(environment, scratch)
            outcome["autotools"] = _run_autotools_probe(environment, scratch)
            outcome["mixed_role"] = _run_mixed_role_probe(environment, scratch)
            outcome["cargo"] = _run_cargo_probe(environment, scratch)
            outcome["transitive_cpp_link"] = _run_cpp_metadata_probe(environment, scratch)
            outcome["cuda"] = environment.cuda_acceptance
        finally:
            for path in fixtures:
                path.unlink(missing_ok=True)
            probe_library.unlink(missing_ok=True)
            for directory in (
                environment.paths.target_prefix / "lib" / "pkgconfig",
                environment.paths.target_prefix / "lib64" / "pkgconfig",
                environment.paths.target_prefix / "share" / "pkgconfig",
            ):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            for directory in (
                environment.paths.target_prefix / "lib64",
                environment.paths.target_prefix / "share",
            ):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
    acceptance = root / "g3-acceptance.json"
    acceptance.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if environment.manifest_path is not None:
        _write_manifest(environment, {"acceptance": outcome})
    return outcome
