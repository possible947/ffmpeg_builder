"""Structured host prerequisites and Windows target data for WSL-Mingw."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .build_types import CATEGORY_TOOLCHAIN_INSTALL, BuildError
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
                "base_sha256": description["base_sha256"],
                "verified": description["verified"],
            },
        }
    return resolved


@dataclass(frozen=True)
class WslMingwPreparation:
    """Resolved WSL host tools kept separate from physical Windows target facts."""

    target: WindowsTargetState
    project_toolchains: Mapping[str, Mapping[str, Any]]
    cuda: Optional[ResolvedCudaBuildHostInputs]
    vulkan: Optional[ResolvedVulkanSdk]
    effective_build_inputs: Mapping[str, Any]
    diagnostic_only: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        target: WindowsTargetState,
        cuda: Optional[ResolvedCudaBuildHostInputs],
        vulkan: Optional[ResolvedVulkanSdk],
        project_toolchains: Optional[Mapping[str, Mapping[str, Any]]] = None,
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
        }
