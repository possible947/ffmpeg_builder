"""Structured host prerequisites and Windows target data for WSL-Mingw."""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from .build_identity import BACKEND_CROSS_MINGW, MaterialBuildInputs, canonical_json
from .build_types import CATEGORY_TOOLCHAIN_INSTALL, BuildError
from .config import BuildConfig
from .cuda_prerequisites import ResolvedCudaBuildHostInputs
from .toolchain_registry import ToolchainRegistry
from .toolchain_store import STATUS_COMPATIBLE, ToolchainStore
from .vulkan_prerequisites import ResolvedVulkanSdk
from .windows_probe import WindowsTargetState


def resolve_wsl_mingw_project_toolchains(
    ffmpeg_version: str,
    registry: ToolchainRegistry,
    store: ToolchainStore,
) -> dict[str, dict[str, Any]]:
    """Resolve the required project-owned toolchains for the WSL-Mingw profile."""
    names = registry.required_toolchains_for(ffmpeg_version, "linux", "linux-cross-mingw")
    if not names:
        raise BuildError(
            "WSL-Mingw toolchain profile",
            f"No required project toolchains are declared for FFmpeg {ffmpeg_version}.",
            category=CATEGORY_TOOLCHAIN_INSTALL,
            code="wsl_mingw_toolchain_profile_missing",
        )

    resolved: dict[str, dict[str, Any]] = {}
    for name in names:
        profile = registry.get_profile(name)
        if profile is None:
            raise BuildError(
                "WSL-Mingw toolchain profile",
                f"Required project toolchain {name!r} is not declared.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_toolchain_profile_missing",
            )
        status, reason = store.installed_status(registry, name)
        if status != STATUS_COMPATIBLE:
            raise BuildError(
                name,
                f"Required project-managed toolchain {name!r} is {status}: {reason}. "
                f"Acquire or verify it with the Toolchain Store.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code=f"wsl_mingw_{name.replace('-', '_')}_toolchain_{status}",
            )
        description = store.describe(registry, name)
        if description is None:
            raise BuildError(
                name,
                f"Required project-managed toolchain {name!r} passed compatibility "
                "checking but its installation could not be described.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_toolchain_provenance_missing",
            )
        try:
            prefix = Path(str(description["prefix"])).resolve(strict=True)
            resolved_paths: dict[str, str] = {}
            for key, value in store.profile_paths(registry, name).items():
                path = Path(value).absolute()
                path.resolve(strict=True).relative_to(prefix)
                # Compiler wrappers can select target and driver mode from the link name.
                resolved_paths[key] = str(path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BuildError(
                name,
                f"Required project-managed toolchain {name!r} has missing or "
                f"out-of-prefix materialized paths: {exc}. Re-acquire or verify it "
                "with the Toolchain Store.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_toolchain_paths_invalid",
            ) from exc
        resolved[name] = {
            "profile": name,
            "version": str(description["version"]),
            "prefix": str(prefix),
            "paths": resolved_paths,
            "target": asdict(profile.target) if profile.target is not None else None,
            "extras": [dict(extra) for extra in description["extras"]],
            "provenance": {
                **(
                    dict(description["provenance"])
                    if isinstance(description.get("provenance"), Mapping)
                    else {
                        "profile": name,
                        "version": str(description["version"]),
                        "source_sha256": str(description["base_sha256"]),
                        "extras": [dict(extra) for extra in description["extras"]],
                    }
                ),
                "verified": description["verified"],
            },
        }
    return resolved


def project_material_build_inputs(
    config: BuildConfig,
    components: list[Any],
    project_toolchains: Mapping[str, Mapping[str, Any]],
    cuda: Optional[ResolvedCudaBuildHostInputs],
    vulkan: Optional[ResolvedVulkanSdk],
    target_input_manifest: Mapping[str, Any],
) -> MaterialBuildInputs:
    """Project the already-resolved production inputs into semantic identity."""
    llvm = project_toolchains.get("llvm-mingw")
    rust = project_toolchains.get("rust")
    if llvm is None or rust is None:
        raise BuildError(
            "WSL-Mingw identity",
            "Resolved LLVM-MinGW and Rust toolchain descriptions are required.",
            category=CATEGORY_TOOLCHAIN_INSTALL,
            code="wsl_mingw_identity_toolchain_missing",
        )
    llvm_target = llvm.get("target")
    if (
        not isinstance(llvm_target, Mapping)
        or llvm_target.get("triple") != "x86_64-w64-mingw32"
        or llvm_target.get("runtime") != "ucrt"
    ):
        raise BuildError(
            "WSL-Mingw identity",
            "The resolved LLVM-MinGW profile does not match the required x86-64 UCRT target.",
            category=CATEGORY_TOOLCHAIN_INSTALL,
            code="wsl_mingw_identity_target_toolchain_invalid",
        )

    def profile_identity(name: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        provenance = profile.get("provenance")
        if not isinstance(provenance, Mapping):
            raise BuildError(
                "WSL-Mingw identity",
                f"Project toolchain {name!r} has no verified source provenance.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_identity_provenance_missing",
            )
        stable_provenance = {key: value for key, value in provenance.items() if key != "verified"}
        extras = stable_provenance.get("extras")
        if isinstance(extras, list):
            stable_provenance["extras"] = sorted(
                extras,
                key=lambda item: (
                    (
                        str(item.get("kind", "")),
                        str(item.get("target", "")),
                        canonical_json(item),
                    )
                    if isinstance(item, Mapping)
                    else ("", "", canonical_json(item))
                ),
            )
        source_hash = stable_provenance.get("source_sha256")
        if (
            not isinstance(source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_hash.lower()) is None
            or stable_provenance.get("profile") != name
            or stable_provenance.get("version") != profile.get("version")
        ):
            raise BuildError(
                "WSL-Mingw identity",
                f"Project toolchain {name!r} has incomplete or inconsistent provenance.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_identity_provenance_invalid",
            )
        return {
            "profile": profile.get("profile", name),
            "version": profile.get("version"),
            "target": profile.get("target"),
            "provenance": stable_provenance,
        }

    rust_provenance = rust.get("provenance")
    rust_extras = rust_provenance.get("extras", []) if isinstance(rust_provenance, Mapping) else []
    target_std = [
        dict(extra)
        for extra in rust_extras
        if isinstance(extra, Mapping)
        and extra.get("kind") == "rust-std"
        and extra.get("target") == "x86_64-pc-windows-gnullvm"
    ]
    if len(target_std) != 1 or target_std[0].get("release") != rust.get("version"):
        raise BuildError(
            "WSL-Mingw identity",
            "Rust provenance does not identify exactly one matching "
            "x86_64-pc-windows-gnullvm target standard library.",
            category=CATEGORY_TOOLCHAIN_INSTALL,
            code="wsl_mingw_identity_rust_target_std_invalid",
        )
    if re.fullmatch(r"[0-9a-f]{64}", str(target_std[0].get("sha256", "")).lower()) is None:
        raise BuildError(
            "WSL-Mingw identity",
            "Rust target-std provenance has no valid source checksum.",
            category=CATEGORY_TOOLCHAIN_INSTALL,
            code="wsl_mingw_identity_rust_target_std_invalid",
        )
    rust_std_identity = {
        key: target_std[0].get(key)
        for key in ("kind", "target", "release", "component", "archive_filename", "sha256")
    }

    toolchains: dict[str, Any] = {
        "llvm_mingw": profile_identity("llvm-mingw", llvm),
        "rust": profile_identity("rust", rust),
        "rust_target_std": rust_std_identity,
        "cuda_ccbin": None,
    }
    effective_cuda: Optional[dict[str, Any]] = None
    if cuda is not None:
        if not cuda.compute_architecture or cuda.probe_status != "passed":
            raise BuildError(
                "WSL-Mingw identity",
                "CUDA is enabled but its effective architecture or compatibility probe is missing.",
                code="wsl_mingw_identity_cuda_incomplete",
            )
        if cuda.ccbin_profile == "external":
            raise BuildError(
                "WSL-Mingw identity",
                "WSL-Mingw CUDA requires the project-managed ToolchainStore ccbin.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_identity_ccbin_external",
            )
        try:
            prefix = Path(str(cuda.ccbin_provenance["prefix"])).resolve(strict=True)
            compiler = cuda.ccbin_path.resolve(strict=True)
            role_path = compiler.relative_to(prefix).as_posix()
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise BuildError(
                "WSL-Mingw identity",
                f"Project-managed CUDA ccbin provenance is incomplete: {exc}",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_identity_ccbin_provenance_invalid",
            ) from exc
        ccbin_provenance = cuda.ccbin_provenance.get("provenance")
        if not isinstance(ccbin_provenance, Mapping):
            raise BuildError(
                "WSL-Mingw identity",
                "Project-managed CUDA ccbin has no stable source provenance.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_identity_ccbin_provenance_missing",
            )
        ccbin_source_hash = ccbin_provenance.get("source_sha256")
        if (
            not isinstance(ccbin_source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", ccbin_source_hash.lower()) is None
            or ccbin_provenance.get("profile") != cuda.ccbin_profile
            or ccbin_provenance.get("version") != cuda.ccbin_profile_version
        ):
            raise BuildError(
                "WSL-Mingw identity",
                "Project-managed CUDA ccbin has invalid source checksum provenance.",
                category=CATEGORY_TOOLCHAIN_INSTALL,
                code="wsl_mingw_identity_ccbin_provenance_invalid",
            )
        toolchains["cuda_ccbin"] = {
            "profile": cuda.ccbin_profile,
            "version": cuda.ccbin_profile_version,
            "role_path": role_path,
            "provenance": {
                key: value for key, value in ccbin_provenance.items() if key != "verified"
            },
        }
        effective_cuda = {
            "architecture": cuda.compute_architecture,
            "compatibility_flags": [
                flag for flag in cuda.common_flags() if not flag.startswith("-ccbin=")
            ],
            "compatibility_workaround": cuda.compatibility_workaround,
            "external_nvcc": {
                "toolkit_version": cuda.detected_cuda_version,
                "nvcc_version": cuda.nvcc_version,
            },
        }

    def tool_version(output: str, name: str) -> list[str]:
        versions = sorted(set(re.findall(r"(?<![A-Za-z])v?\d+\.\d+(?:\.\d+)?", output)))
        if not versions:
            raise BuildError(
                "WSL-Mingw identity",
                f"The selected Vulkan shader compiler {name!r} did not report a stable version.",
                code="wsl_mingw_identity_shader_compiler_version_missing",
            )
        return versions

    shader_tool: Optional[dict[str, Any]] = None
    if vulkan is not None:
        if vulkan.shader_compiler_path is None:
            raise BuildError(
                "WSL-Mingw identity",
                "Vulkan is enabled but its selected shader compiler is missing.",
                code="wsl_mingw_identity_shader_compiler_missing",
            )
        shader_name = vulkan.shader_compiler_path.name
        shader_tool = {
            "tool": shader_name,
            "version": tool_version(
                str(vulkan.tool_diagnostics.get("shader_compiler", "")), shader_name
            ),
        }

    source_identities = target_input_manifest.get("sources")
    interface_identities = target_input_manifest.get("interfaces")
    file_identities = target_input_manifest.get("files")
    if (
        not isinstance(source_identities, Mapping)
        or not isinstance(interface_identities, list)
        or not isinstance(file_identities, Mapping)
    ):
        raise BuildError("WSL-Mingw identity", "Target-input provenance is incomplete.")
    required_sources = {"nv-codec", "vulkan-headers", "opencl-headers"}
    if not required_sources <= set(source_identities):
        raise BuildError(
            "WSL-Mingw identity",
            "Target-input provenance is missing a required header or interface source.",
            code="wsl_mingw_identity_target_sources_incomplete",
        )
    required_interfaces = {"lib/libvulkan.a", "lib/libOpenCL.a"}
    if not required_interfaces <= {
        interface.get("output")
        for interface in interface_identities
        if isinstance(interface, Mapping)
    }:
        raise BuildError(
            "WSL-Mingw identity",
            "Target-input provenance is missing a required Vulkan/OpenCL interface.",
            code="wsl_mingw_identity_target_interfaces_incomplete",
        )
    opencl_definition = next(
        (
            interface
            for interface in interface_identities
            if isinstance(interface, Mapping) and interface.get("output") == "lib/libOpenCL.a"
        ),
        None,
    )
    if (
        not isinstance(opencl_definition, Mapping)
        or opencl_definition.get("definition_source_component") != "opencl-icd-loader"
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(opencl_definition.get("definition_source_sha256", "")).lower(),
        )
        is None
    ):
        raise BuildError(
            "WSL-Mingw identity",
            "OpenCL import-interface provenance is missing its pinned ICD Loader definition source.",
            code="wsl_mingw_identity_opencl_interface_incomplete",
        )

    manifest_content = {
        key: target_input_manifest.get(key)
        for key in ("target_backend", "sources", "interfaces", "files")
    }
    target_input_identity = {
        "materialization_id": target_input_manifest.get("materialization_id"),
        "manifest": manifest_content,
    }
    capabilities = {
        "cuda": {
            "build_mode": config.cuda_build_mode,
            "libvmaf": config.enable_libvmaf,
            "libvmaf_cuda": config.enable_libvmaf and config.enable_libvmaf_cuda,
            "ffmpeg_nvcc": config.gpl_enabled or config.allow_nonfree_cuda,
            "effective": cuda is not None,
        },
        "vulkan": {
            "configured": config.enable_libplacebo_vulkan,
            "effective": vulkan is not None,
        },
        "opencl": {
            "registry_selected": any(
                getattr(component, "name", None) == "opencl-headers" for component in components
            ),
        },
        "nvidia_codec": {
            "registry_selected": any(
                getattr(component, "name", None) == "nv-codec" for component in components
            ),
        },
    }
    return MaterialBuildInputs(
        target_backend=BACKEND_CROSS_MINGW,
        configured_capabilities=capabilities,
        toolchains=toolchains,
        effective_cuda=effective_cuda,
        target_inputs={
            "materialization_id": target_input_identity["materialization_id"],
            "manifest": target_input_identity["manifest"],
            "vulkan_shader_compiler": shader_tool,
        },
    )


@dataclass(frozen=True)
class WslMingwPreparation:
    """Resolved WSL host tools kept separate from physical Windows target facts."""

    target: WindowsTargetState
    project_toolchains: Mapping[str, Mapping[str, Any]]
    cuda: Optional[ResolvedCudaBuildHostInputs]
    vulkan: Optional[ResolvedVulkanSdk]
    effective_build_inputs: Mapping[str, Any]
    diagnostic_only: Mapping[str, Any]
    identity_summary: Optional[Mapping[str, Any]] = None

    @classmethod
    def create(
        cls,
        target: WindowsTargetState,
        cuda: Optional[ResolvedCudaBuildHostInputs],
        vulkan: Optional[ResolvedVulkanSdk],
        project_toolchains: Optional[Mapping[str, Mapping[str, Any]]] = None,
        identity_summary: Optional[Mapping[str, Any]] = None,
    ) -> "WslMingwPreparation":
        effective: dict[str, Any] = {}
        diagnostics: dict[str, Any] = {}
        toolchains = dict(project_toolchains or {})
        effective["project_toolchains"] = {
            name: {
                "version": profile["version"],
                "prefix": profile["prefix"],
                "paths": dict(profile["paths"]),
                "target": profile["target"],
                "extras": list(profile["extras"]),
            }
            for name, profile in toolchains.items()
        }
        diagnostics["project_toolchains"] = {
            name: dict(profile["provenance"]) for name, profile in toolchains.items()
        }
        if cuda is not None:
            effective["cuda"] = {
                "ccbin_path": str(cuda.ccbin_path),
                "ccbin_profile": cuda.ccbin_profile,
                "ccbin_profile_version": cuda.ccbin_profile_version,
                "compute_architecture": cuda.compute_architecture,
                "effective_flags": list(cuda.effective_flags),
                "compatibility_workaround": cuda.compatibility_workaround,
                "probe_status": cuda.probe_status,
            }
            diagnostics["cuda"] = {
                "sdk_root": str(cuda.cuda_root),
                "nvcc_path": str(cuda.nvcc_path),
                "resolution_source": cuda.resolution_source,
                "toolkit_version": cuda.detected_cuda_version,
                "nvcc_version": cuda.nvcc_version,
                "ccbin_provenance": dict(cuda.ccbin_provenance),
                "diagnostics": list(cuda.diagnostics),
                "probe_diagnostics": list(cuda.probe_diagnostics),
            }
        if vulkan is not None:
            effective["vulkan"] = {
                "validated_capabilities": sorted(vulkan.validated_capabilities),
            }
            diagnostics["vulkan"] = {
                "selected_root": str(vulkan.selected_root),
                "effective_sdk_root": str(vulkan.effective_sdk_root),
                "resolution_source": vulkan.resolution_source,
                "shader_compiler": (
                    str(vulkan.shader_compiler_path) if vulkan.shader_compiler_path else None
                ),
                "spirv_validator": (
                    str(vulkan.spirv_validator_path) if vulkan.spirv_validator_path else None
                ),
                "spirv_optimizer": (
                    str(vulkan.spirv_optimizer_path) if vulkan.spirv_optimizer_path else None
                ),
                "tool_diagnostics": dict(vulkan.tool_diagnostics),
                "diagnostics": list(vulkan.diagnostics),
            }
        return cls(
            target=target,
            project_toolchains=toolchains,
            cuda=cuda,
            vulkan=vulkan,
            effective_build_inputs=effective,
            diagnostic_only=diagnostics,
            identity_summary=identity_summary,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a structured diagnostic view without flattening host and target data."""
        return {
            "target": {
                "os": asdict(self.target.os),
                "cpu": asdict(self.target.cpu),
                "gpus": [asdict(gpu) for gpu in self.target.gpus],
                "nvidia": asdict(self.target.nvidia),
                "vulkan": asdict(self.target.vulkan),
                "opencl": asdict(self.target.opencl),
                "field_sources": dict(self.target.field_sources),
            },
            "project_toolchains": (
                {
                    **{name: dict(profile) for name, profile in self.project_toolchains.items()},
                    **(
                        {
                            "cuda_ccbin": {
                                "profile": self.cuda.ccbin_profile,
                                "path": str(self.cuda.ccbin_path),
                                "version": self.cuda.ccbin_profile_version,
                            }
                        }
                        if self.cuda is not None
                        else {}
                    ),
                }
            ),
            "external_prerequisites": {
                "cuda": self.cuda.to_dict() if self.cuda is not None else None,
                "vulkan": self.vulkan.to_dict() if self.vulkan is not None else None,
            },
            "effective_build_inputs": dict(self.effective_build_inputs),
            "diagnostic_only": dict(self.diagnostic_only),
            "identity": (
                dict(self.identity_summary) if self.identity_summary is not None else None
            ),
        }
