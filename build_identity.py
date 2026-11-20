"""Semantic build/runtime identity and comparison for target builds.

An identity answers one question: *may previously produced state and artifacts
be reused for the current build?* It is deliberately not a hash of raw probe
output. Three categories are kept apart:

* **build** - inputs that change what is built (backend, FFmpeg version,
  material configuration, component/toolchain selection and, only when the
  build is CPU-specialised, the effective CPU). A difference means existing
  artifacts and completion statuses are unsafe to reuse.
* **runtime** - Windows runtime state that decides whether an earlier runtime
  *acceptance* still holds (OS build, CPU/OS-enabled ISA, GPU inventory, NVIDIA
  driver, Vulkan and OpenCL devices). A difference keeps compiled artifacts
  usable but invalidates prior acceptance.
* **diagnostics** - audit data (product names, UBR, probe provenance, ICD
  manifests, errors). Never affects compatibility.

The persisted form is human-readable JSON with an explicit schema version.
Fingerprints are derived values for directory naming and corruption
detection; the semantic fields remain the source of every mismatch report.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .windows_probe import FeatureState, WindowsTargetState

IDENTITY_SCHEMA_VERSION = 2
LEGACY_IDENTITY_SCHEMA_VERSION = 1
BACKEND_CROSS_MINGW = "linux-cross-mingw"

# BuildConfig fields that do not change produced artifacts (execution tuning,
# acquisition policy, packaging) and the nested sections that do not apply to
# the Windows target. Everything else is build-material by default so a newly
# added option is conservatively treated as material.
NON_MATERIAL_CONFIG_FIELDS = frozenset(
    {
        "num_jobs",
        "make_timeout_seconds",
        "install_timeout_seconds",
        "async_downloads",
        "download_workers",
        "source_archives_dir",
        "allow_network_downloads",
        "require_sha256_for_network",
        "make_release",
        "target_backend",
        "macos",
        "windows",
    }
)

# Diagnostic keys that never take part in comparison (volatile by nature).
VOLATILE_DIAGNOSTIC_KEYS = frozenset({"probed_at"})


class Compatibility(str, Enum):
    """Overall result of comparing a saved identity with the current one."""

    COMPATIBLE = "compatible"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    RUNTIME_CHANGED = "runtime_changed"
    BUILD_INCOMPATIBLE = "build_incompatible"
    UNKNOWN = "unknown"  # legacy, corrupt, unsupported or missing identity


class Category(str, Enum):
    BUILD = "build"
    RUNTIME = "runtime"
    DIAGNOSTIC = "diagnostic"
    IDENTITY = "identity"


class RequiredAction(str, Enum):
    NONE = "none"
    NEW_ACCEPTANCE = "new_acceptance"
    REBUILD = "rebuild"
    FRESH_BUILD = "fresh_build"


_ACTION_BY_CATEGORY = {
    Category.BUILD: RequiredAction.REBUILD,
    Category.RUNTIME: RequiredAction.NEW_ACCEPTANCE,
    Category.DIAGNOSTIC: RequiredAction.NONE,
    Category.IDENTITY: RequiredAction.FRESH_BUILD,
}
_REASON_BY_CATEGORY = {
    Category.BUILD: "build-material input changed; saved statuses and artifacts cannot be reused",
    Category.RUNTIME: "runtime state changed; earlier runtime acceptance is no longer valid",
    Category.DIAGNOSTIC: "diagnostic-only difference; no action required",
    Category.IDENTITY: "identity cannot be trusted",
}
_SEVERITY = {
    Compatibility.COMPATIBLE: 0,
    Compatibility.DIAGNOSTIC_ONLY: 1,
    Compatibility.RUNTIME_CHANGED: 2,
    Compatibility.BUILD_INCOMPATIBLE: 3,
    Compatibility.UNKNOWN: 4,
}
_COMPAT_BY_CATEGORY = {
    Category.DIAGNOSTIC: Compatibility.DIAGNOSTIC_ONLY,
    Category.RUNTIME: Compatibility.RUNTIME_CHANGED,
    Category.BUILD: Compatibility.BUILD_INCOMPATIBLE,
    Category.IDENTITY: Compatibility.UNKNOWN,
}


# --------------------------------------------------------------------------
# Normalisation and serialisation
# --------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split())


def normalize(value: Any) -> Any:
    """Deterministic JSON-compatible normalisation.

    Strings are whitespace-collapsed (case is preserved here; identifiers that
    are case-insensitive are lower-cased by the identity builders). ``None`` and
    ``""`` stay distinct. Dict keys are sorted at serialisation time. Floats are
    rendered as strings so no binary rounding leaks into comparisons. Mapping
    keys are emitted in sorted order so persisted identity evidence is canonical.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else format(value, ".12g")
    if isinstance(value, str):
        return _collapse(value)
    if isinstance(value, Mapping):
        return {
            str(k): normalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    raise TypeError(f"Unsupported identity value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable serialisation used for sorting and fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def target_manifest_fingerprint(value: Mapping[str, Any]) -> str:
    """Hash G2.6 manifest content using its canonical compact JSON encoding."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value.lower()) is not None


def _sorted_items(items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Sort order-insensitive collections without merging duplicates."""
    return sorted((dict(item) for item in items), key=canonical_json)


def _lower(value: Optional[str]) -> Optional[str]:
    return None if value is None else _collapse(value).lower()


# --------------------------------------------------------------------------
# Identity model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetIdentity:
    """Versioned semantic identity (build + runtime) plus diagnostics."""

    backend: str
    build: Dict[str, Any]
    runtime: Optional[Dict[str, Any]] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = IDENTITY_SCHEMA_VERSION

    @property
    def build_fingerprint(self) -> str:
        return fingerprint(self.build)

    @property
    def runtime_fingerprint(self) -> Optional[str]:
        return None if self.runtime is None else fingerprint(self.runtime)

    @property
    def build_key(self) -> str:
        """Short identity-derived directory key (first 16 hex of the build fingerprint)."""
        return self.build_fingerprint[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend": self.backend,
            "build": normalize(self.build),
            "runtime": normalize(self.runtime),
            "diagnostics": normalize(self.diagnostics),
            "fingerprints": {
                "build": self.build_fingerprint,
                "runtime": self.runtime_fingerprint,
            },
        }

    def with_runtime(
        self, runtime: Optional[Dict[str, Any]], diagnostics: Optional[Dict[str, Any]] = None
    ) -> "TargetIdentity":
        return TargetIdentity(
            backend=self.backend,
            build=self.build,
            runtime=runtime,
            diagnostics=self.diagnostics if diagnostics is None else diagnostics,
            schema_version=self.schema_version,
        )


@dataclass(frozen=True)
class MaterialBuildInputs:
    """Canonical G2 material-input projection for the WSL-Mingw build."""

    target_backend: str
    configured_capabilities: Mapping[str, Any]
    toolchains: Mapping[str, Any]
    effective_cuda: Optional[Mapping[str, Any]]
    target_inputs: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        if self.target_backend != BACKEND_CROSS_MINGW:
            raise ValueError(f"unsupported material-input backend: {self.target_backend}")
        if not {"llvm_mingw", "rust", "rust_target_std"} <= set(self.toolchains):
            raise ValueError("material toolchain identity is incomplete")
        if not self.configured_capabilities:
            raise ValueError("configured capability identity is missing")
        manifest = self.target_inputs.get("manifest")
        materialization_id = self.target_inputs.get("materialization_id")
        if not isinstance(materialization_id, str) or not isinstance(manifest, Mapping):
            raise ValueError("target-input materialisation identity is missing")
        manifest_identity = {
            key: manifest.get(key) for key in ("target_backend", "sources", "interfaces", "files")
        }
        if (
            manifest.get("target_backend") != BACKEND_CROSS_MINGW
            or not isinstance(manifest.get("sources"), dict)
            or not isinstance(manifest.get("interfaces"), list)
            or not isinstance(manifest.get("files"), dict)
        ):
            raise ValueError("target-input manifest is incomplete or has the wrong backend")
        if target_manifest_fingerprint(manifest_identity) != materialization_id:
            raise ValueError("target-input materialisation ID does not match its manifest")
        normalized = normalize(
            {
                "target_backend": self.target_backend,
                "configured_capabilities": self.configured_capabilities,
                "toolchains": self.toolchains,
                "effective_cuda": self.effective_cuda,
                "target_inputs": self.target_inputs,
            }
        )
        if not isinstance(normalized, dict):
            raise TypeError("normalized material-input identity must be an object")
        return normalized


class IdentityLoadStatus(str, Enum):
    OK = "ok"
    LEGACY = "legacy"  # no semantic identity recorded
    CORRUPT = "corrupt"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


@dataclass(frozen=True)
class IdentityLoad:
    status: IdentityLoadStatus
    identity: Optional[TargetIdentity]
    reason: str


def parse_identity(data: Any) -> IdentityLoad:
    """Validate a persisted identity. Never repairs or guesses material fields."""
    if data is None:
        return IdentityLoad(
            IdentityLoadStatus.LEGACY,
            None,
            "no semantic identity is recorded (state predates identity tracking)",
        )

    def corrupt(reason: str) -> IdentityLoad:
        return IdentityLoad(IdentityLoadStatus.CORRUPT, None, reason)

    if not isinstance(data, dict):
        return corrupt(f"identity is a {type(data).__name__}, expected an object")
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return corrupt("identity has no integer schema_version")
    if version not in (LEGACY_IDENTITY_SCHEMA_VERSION, IDENTITY_SCHEMA_VERSION):
        return IdentityLoad(
            IdentityLoadStatus.UNSUPPORTED_SCHEMA,
            None,
            f"identity schema_version {version} is not supported "
            f"(this version understands {LEGACY_IDENTITY_SCHEMA_VERSION} and "
            f"{IDENTITY_SCHEMA_VERSION})",
        )
    backend = data.get("backend")
    build = data.get("build")
    runtime = data.get("runtime")
    diagnostics = data.get("diagnostics", {})
    fingerprints = data.get("fingerprints")
    if not isinstance(backend, str) or not backend:
        return corrupt("identity.backend is missing")
    if not isinstance(build, dict) or not build:
        return corrupt("identity.build is missing or empty")
    if runtime is not None and not isinstance(runtime, dict):
        return corrupt("identity.runtime must be an object or null")
    if backend == BACKEND_CROSS_MINGW and not runtime:
        return corrupt("identity.runtime is required for the linux-cross-mingw backend")
    if backend == BACKEND_CROSS_MINGW:
        if version != IDENTITY_SCHEMA_VERSION:
            return IdentityLoad(
                IdentityLoadStatus.UNSUPPORTED_SCHEMA,
                None,
                "saved linux-cross-mingw identity is G1-only and has no G2 material-input "
                "projection; mutable artifacts cannot be proven compatible",
            )
        material_inputs = build.get("material_inputs")
        if not isinstance(material_inputs, dict):
            return corrupt("identity.build.material_inputs is missing")
        required_material = {
            "target_backend",
            "configured_capabilities",
            "toolchains",
            "effective_cuda",
            "target_inputs",
        }
        if not required_material <= set(material_inputs):
            return corrupt("identity.build.material_inputs is incomplete")
        if material_inputs.get("target_backend") != BACKEND_CROSS_MINGW:
            return corrupt("identity.build.material_inputs.target_backend is inconsistent")
        if not isinstance(material_inputs.get("configured_capabilities"), dict):
            return corrupt("identity.build.material_inputs.configured_capabilities is invalid")
        capabilities = material_inputs["configured_capabilities"]
        if not {"cuda", "vulkan", "opencl", "nvidia_codec"} <= set(capabilities):
            return corrupt("identity.build.material_inputs.configured_capabilities is incomplete")
        toolchains = material_inputs.get("toolchains")
        if not isinstance(toolchains, dict) or not {
            "llvm_mingw",
            "rust",
            "rust_target_std",
        } <= set(toolchains):
            return corrupt("identity.build.material_inputs.toolchains is incomplete")
        expected_profiles = {"llvm_mingw": "llvm-mingw", "rust": "rust"}
        for name, expected_profile in expected_profiles.items():
            identity = toolchains.get(name)
            provenance = identity.get("provenance") if isinstance(identity, dict) else None
            if (
                not isinstance(identity, dict)
                or identity.get("profile") != expected_profile
                or not identity.get("version")
                or not isinstance(provenance, dict)
                or provenance.get("profile") != expected_profile
                or provenance.get("version") != identity.get("version")
                or not _is_sha256(provenance.get("source_sha256"))
            ):
                return corrupt(f"identity.build.material_inputs.toolchains.{name} is incomplete")
        llvm_target = toolchains["llvm_mingw"].get("target")
        if (
            not isinstance(llvm_target, dict)
            or llvm_target.get("triple") != "x86_64-w64-mingw32"
            or llvm_target.get("runtime") != "ucrt"
        ):
            return corrupt("identity.build.material_inputs LLVM-MinGW target is invalid")
        rust_std = toolchains.get("rust_target_std")
        if (
            not isinstance(rust_std, dict)
            or rust_std.get("target") != "x86_64-pc-windows-gnullvm"
            or rust_std.get("release") != toolchains["rust"].get("version")
            or not _is_sha256(rust_std.get("sha256"))
        ):
            return corrupt("identity.build.material_inputs Rust target std is incomplete")
        cuda = material_inputs.get("effective_cuda")
        cuda_capability = capabilities.get("cuda")
        if not isinstance(cuda_capability, dict):
            return corrupt("identity.build.material_inputs CUDA capability is invalid")
        if cuda_capability.get("effective"):
            ccbin = toolchains.get("cuda_ccbin")
            if (
                not isinstance(cuda, dict)
                or not isinstance(cuda.get("architecture"), str)
                or not cuda.get("architecture")
                or not isinstance(cuda.get("compatibility_flags"), list)
                or not isinstance(cuda.get("external_nvcc"), dict)
                or not cuda["external_nvcc"].get("nvcc_version")
                or not isinstance(ccbin, dict)
                or not ccbin.get("profile")
                or not ccbin.get("role_path")
                or not isinstance(ccbin.get("provenance"), dict)
                or ccbin["provenance"].get("profile") != ccbin.get("profile")
                or ccbin["provenance"].get("version") != ccbin.get("version")
                or not _is_sha256(ccbin["provenance"].get("source_sha256"))
            ):
                return corrupt(
                    "identity.build.material_inputs effective CUDA inputs are incomplete"
                )
        elif cuda is not None:
            return corrupt("identity.build.material_inputs has CUDA inputs while CUDA is disabled")
        target_inputs = material_inputs.get("target_inputs")
        if (
            not isinstance(target_inputs, dict)
            or not isinstance(target_inputs.get("materialization_id"), str)
            or not _is_sha256(target_inputs.get("materialization_id"))
            or not isinstance(target_inputs.get("manifest"), dict)
        ):
            return corrupt("identity.build.material_inputs.target_inputs is incomplete")
        manifest = target_inputs["manifest"]
        manifest_identity = {
            key: manifest.get(key) for key in ("target_backend", "sources", "interfaces", "files")
        }
        sources = manifest.get("sources")
        interfaces = manifest.get("interfaces")
        files = manifest.get("files")
        if (
            manifest.get("target_backend") != BACKEND_CROSS_MINGW
            or not isinstance(sources, dict)
            or not isinstance(interfaces, list)
            or not isinstance(files, dict)
        ):
            return corrupt("identity.build.material_inputs target-input manifest is invalid")
        required_sources = {"nv-codec", "vulkan-headers", "opencl-headers"}
        if not required_sources <= set(sources):
            return corrupt("identity target-input manifest is missing required header sources")
        if target_manifest_fingerprint(manifest_identity) != target_inputs["materialization_id"]:
            return corrupt(
                "identity.build.material_inputs.target_inputs materialisation ID does not "
                "match its manifest"
            )
        for source_name, source in sources.items():
            digest = source.get("sha256") if isinstance(source, dict) else None
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                return corrupt(f"identity target source checksum is invalid: {source_name}")
        for interface in interfaces:
            if not isinstance(interface, dict):
                return corrupt("identity target interface metadata is invalid")
            output = interface.get("output")
            output_digest = interface.get("output_sha256")
            if (
                not isinstance(output, str)
                or not isinstance(interface.get("recipe"), str)
                or not isinstance(output_digest, str)
                or files.get(output) != output_digest
            ):
                return corrupt("identity generated interface hash disagrees with its file identity")
        if any(
            not isinstance(path, str)
            or path.startswith("/")
            or ".." in path.split("/")
            or not path.startswith(("include/", "lib/"))
            or not _is_sha256(digest)
            for path, digest in files.items()
        ):
            return corrupt("identity target-input file hashes are invalid")
        if not {"lib/libvulkan.a", "lib/libOpenCL.a"} <= {
            interface.get("output") for interface in interfaces if isinstance(interface, dict)
        }:
            return corrupt("identity target-input manifest is missing generated interfaces")
        opencl_interface = next(
            (
                interface
                for interface in interfaces
                if isinstance(interface, dict) and interface.get("output") == "lib/libOpenCL.a"
            ),
            None,
        )
        if (
            opencl_interface is None
            or opencl_interface.get("definition_source_component") != "opencl-icd-loader"
            or not _is_sha256(opencl_interface.get("definition_source_sha256"))
        ):
            return corrupt("identity OpenCL interface definition source is incomplete")
        vulkan_capability = capabilities.get("vulkan")
        shader_compiler = target_inputs.get("vulkan_shader_compiler")
        if (
            isinstance(vulkan_capability, dict)
            and vulkan_capability.get("effective")
            and (
                not isinstance(shader_compiler, dict)
                or not shader_compiler.get("tool")
                or not shader_compiler.get("version")
            )
        ):
            return corrupt("identity Vulkan shader compiler material identity is incomplete")
    if not isinstance(diagnostics, dict):
        return corrupt("identity.diagnostics must be an object")
    if not isinstance(fingerprints, dict):
        return corrupt("identity.fingerprints is missing")
    identity = TargetIdentity(
        backend=backend,
        build=build,
        runtime=runtime,
        diagnostics=diagnostics,
        schema_version=version,
    )
    if fingerprints.get("build") != identity.build_fingerprint:
        return corrupt("identity.fingerprints.build does not match the recorded build fields")
    if fingerprints.get("runtime") != identity.runtime_fingerprint:
        return corrupt("identity.fingerprints.runtime does not match the recorded runtime fields")
    return IdentityLoad(IdentityLoadStatus.OK, identity, "ok")


# --------------------------------------------------------------------------
# Identity builders
# --------------------------------------------------------------------------


def _material_config(
    config_dict: Mapping[str, Any], *, omit_external_prerequisite_paths: bool = False
) -> Dict[str, Any]:
    return {
        key: normalize(value)
        for key, value in sorted(config_dict.items())
        if key not in NON_MATERIAL_CONFIG_FIELDS
        and not (omit_external_prerequisite_paths and key == "external_prerequisite_paths")
    }


def build_native_identity(
    config_dict: Mapping[str, Any], backend: str, target_os: str, machine: str
) -> TargetIdentity:
    """Identity for native Linux/WSL/macOS builds.

    Deliberately minimal: the native flows keep their historical semantics and
    never require Windows probing. Only facts that make saved state meaningless
    on a different backend/architecture/FFmpeg version are recorded.
    """
    build = {
        "backend": backend,
        "target": {"os": _lower(target_os), "architecture": _lower(machine)},
        "ffmpeg_version": normalize(config_dict.get("ffmpeg_version")),
    }
    return TargetIdentity(backend=backend, build=build, runtime=None)


def runtime_identity(state: WindowsTargetState) -> Dict[str, Any]:
    """Runtime-acceptance identity from a typed Windows target state."""
    gpus = _sorted_items(
        [
            {
                "name": normalize(g.name),
                "vendor_id": g.vendor_id,
                "device_id": g.device_id,
                "subsystem_id": g.subsystem_id,
                "revision": g.revision,
                "pnp_device_id": _lower(g.pnp_device_id),
                "adapter_type": g.adapter_type,
                "driver_version": normalize(g.driver_version),
            }
            for g in state.gpus
        ]
    )
    nvidia: Dict[str, Any] = {
        "state": state.nvidia.state.value,
        "devices": _sorted_items(
            [
                {
                    "name": normalize(d.name),
                    "uuid": _lower(d.uuid),
                    "driver_version": normalize(d.driver_version),
                    "compute_capability": normalize(d.compute_capability),
                    "pci_bus_id": _lower(d.pci_bus_id),
                    "gpu_pnp_device_id": _lower(d.gpu_pnp_device_id),
                }
                for d in state.nvidia.devices
            ]
        ),
        "api": {k: normalize(v) for k, v in sorted(state.nvidia.api.items())},
    }
    vulkan: Dict[str, Any] = {
        "state": state.vulkan.state.value,
        "loader_version": normalize(state.vulkan.loader_version),
        "devices": _sorted_items(
            [
                {
                    "name": normalize(d.name),
                    "vendor_id": d.vendor_id,
                    "device_id": d.device_id,
                    "device_type": d.device_type,
                    "api_version": normalize(d.api_version),
                    "driver_version": normalize(d.driver_version),
                }
                for d in state.vulkan.devices
            ]
        ),
    }
    opencl_platforms = _sorted_items(
        [
            {
                "name": normalize(p.name),
                "vendor": normalize(p.vendor),
                "version": normalize(p.version),
                "devices": _sorted_items(
                    [
                        {
                            "name": normalize(d.name),
                            "vendor": normalize(d.vendor),
                            "vendor_id": d.vendor_id,
                            "device_type": d.device_type,
                            "version": normalize(d.version),
                            "driver_version": normalize(d.driver_version),
                        }
                        for d in p.devices
                    ]
                ),
            }
            for p in state.opencl.platforms
        ]
    )
    opencl: Dict[str, Any] = {
        "state": state.opencl.state.value,
        "dispatcher_version": normalize(state.opencl.dispatcher_version),
        "platforms": opencl_platforms,
    }
    return {
        "os": {
            "platform": state.os.platform,
            "architecture": state.os.architecture,
            "version": normalize(state.os.version),
            "build_number": state.os.build_number,
        },
        "cpu": _cpu_identity(state),
        "gpus": gpus,
        "nvidia": nvidia,
        "vulkan": vulkan,
        "opencl": opencl,
    }


def _cpu_identity(state: WindowsTargetState) -> Dict[str, Any]:
    cpu = state.cpu
    return {
        "vendor": _lower(cpu.vendor),
        "family": cpu.family,
        "model": cpu.model,
        "stepping": cpu.stepping,
        "architecture": cpu.architecture,
        "isa_features": {k: v for k, v in sorted(cpu.isa_features.items())},
    }


def diagnostic_snapshot(
    state: WindowsTargetState, host: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Bounded audit data; never part of compatibility."""
    snapshot: Dict[str, Any] = normalize(
        {
            "os": {
                "product_name": state.os.product_name,
                "edition": state.os.edition,
                "ubr": state.os.ubr,
                "display_version": state.os.display_version,
            },
            "cpu": {
                "name": state.cpu.name,
                "cores": state.cpu.cores,
                "logical_processors": state.cpu.logical_processors,
            },
            "vulkan": {
                "icd_manifests": sorted(state.vulkan.icd_manifests),
                "error": state.vulkan.error,
            },
            "opencl": {
                "icd_vendors": sorted(state.opencl.icd_vendors),
                "error": state.opencl.error,
            },
            "nvidia": {"error": state.nvidia.error},
            "field_sources": dict(sorted(state.field_sources.items())),
            "probe": dict(sorted(state.diagnostics.items())),
            "build_host": dict(sorted((host or {}).items())),
            "probed_at": state.probed_at,
        }
    )
    return snapshot


def build_windows_identity(
    config_dict: Mapping[str, Any],
    state: WindowsTargetState,
    components: Optional[Mapping[str, str]] = None,
    toolchains: Optional[Mapping[str, str]] = None,
    host: Optional[Mapping[str, Any]] = None,
    material_inputs: Optional[MaterialBuildInputs] = None,
) -> TargetIdentity:
    """Identity for the WSL-Mingw (``linux-cross-mingw``) route.

    ``material_inputs`` is required for a current reusable WSL-Mingw identity.
    Omitting it creates a G1-only identity solely for legacy diagnostics; its
    schema is rejected by :func:`parse_identity` for reuse.
    """
    if material_inputs is None:
        raise ValueError("linux-cross-mingw identity requires resolved G2 material inputs")
    material_projection = material_inputs.to_dict()
    native = bool(config_dict.get("native_build"))
    cpu_spec: Dict[str, Any]
    if native:
        cpu_spec = {"policy": "native", "cpu": _cpu_identity(state)}
    else:
        cpu_spec = {"policy": "generic", "cpu": None}
    build: Dict[str, Any] = {
        "backend": BACKEND_CROSS_MINGW,
        "target": {"os": state.os.platform, "architecture": state.os.architecture},
        "ffmpeg_version": normalize(config_dict.get("ffmpeg_version")),
        "config": _material_config(config_dict, omit_external_prerequisite_paths=True),
        "cpu_specialization": cpu_spec,
        "components": {k: normalize(v) for k, v in sorted((components or {}).items())},
        "toolchains": {k: normalize(v) for k, v in sorted((toolchains or {}).items())},
        "material_inputs": material_projection,
    }
    return TargetIdentity(
        backend=BACKEND_CROSS_MINGW,
        build=build,
        runtime=runtime_identity(state),
        diagnostics=diagnostic_snapshot(state, host),
    )


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityDifference:
    category: Category
    path: str
    saved: Any
    current: Any
    reason: str
    action: RequiredAction

    def format(self) -> str:
        return (
            f"[{self.category.value}] {self.path}: saved={_show(self.saved)} "
            f"current={_show(self.current)} - {self.reason}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "path": self.path,
            "saved": self.saved,
            "current": self.current,
            "reason": self.reason,
            "action": self.action.value,
        }


@dataclass(frozen=True)
class IdentityComparison:
    result: Compatibility
    differences: Sequence[IdentityDifference] = ()

    @property
    def build_compatible(self) -> bool:
        """True when existing artifacts/statuses may be reused."""
        return self.result in (
            Compatibility.COMPATIBLE,
            Compatibility.DIAGNOSTIC_ONLY,
            Compatibility.RUNTIME_CHANGED,
        )

    @property
    def acceptance_valid(self) -> bool:
        return self.result in (Compatibility.COMPATIBLE, Compatibility.DIAGNOSTIC_ONLY)

    @property
    def required_action(self) -> RequiredAction:
        actions = {d.action for d in self.differences}
        for action in (
            RequiredAction.FRESH_BUILD,
            RequiredAction.REBUILD,
            RequiredAction.NEW_ACCEPTANCE,
        ):
            if action in actions:
                return action
        return RequiredAction.NONE

    def affecting(self, *categories: Category) -> List[IdentityDifference]:
        return [d for d in self.differences if d.category in categories]

    def format(self, limit: int = 20) -> str:
        lines = [f"identity comparison: {self.result.value}"]
        shown = [d for d in self.differences if d.category != Category.DIAGNOSTIC] or list(
            self.differences
        )
        for diff in list(shown)[:limit]:
            lines.append("  " + diff.format())
        if len(shown) > limit:
            lines.append(f"  ... and {len(shown) - limit} more")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result.value,
            "differences": [d.to_dict() for d in self.differences],
        }


_MISSING = object()


def _show(value: Any) -> str:
    if value is _MISSING:
        return "<missing>"
    text = canonical_json(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _diff(
    saved: Any, current: Any, path: str, category: Category, out: List[IdentityDifference]
) -> None:
    if path.endswith(".manifest.files") and isinstance(saved, dict) and isinstance(current, dict):
        if canonical_json(saved) != canonical_json(current):
            out.append(
                IdentityDifference(
                    category,
                    path,
                    {"count": len(saved), "sha256": fingerprint(saved)},
                    {"count": len(current), "sha256": fingerprint(current)},
                    _REASON_BY_CATEGORY[category],
                    _ACTION_BY_CATEGORY[category],
                )
            )
        return
    if isinstance(saved, dict) and isinstance(current, dict):
        for key in sorted(set(saved) | set(current)):
            sub = f"{path}.{key}" if path else key
            if category == Category.DIAGNOSTIC and key in VOLATILE_DIAGNOSTIC_KEYS:
                continue
            _diff(saved.get(key, _MISSING), current.get(key, _MISSING), sub, category, out)
        return
    if isinstance(saved, list) and isinstance(current, list):
        for index in range(max(len(saved), len(current))):
            _diff(
                saved[index] if index < len(saved) else _MISSING,
                current[index] if index < len(current) else _MISSING,
                f"{path}[{index}]",
                category,
                out,
            )
        return
    if saved is _MISSING or current is _MISSING or canonical_json(saved) != canonical_json(current):
        out.append(
            IdentityDifference(
                category=category,
                path=path,
                saved=None if saved is _MISSING else saved,
                current=None if current is _MISSING else current,
                reason=(
                    "field removed"
                    if current is _MISSING
                    else "field added" if saved is _MISSING else _REASON_BY_CATEGORY[category]
                ),
                action=_ACTION_BY_CATEGORY[category],
            )
        )


def _unknown_runtime_differences(
    saved: Mapping[str, Any], current: Mapping[str, Any], already: Sequence[IdentityDifference]
) -> List[IdentityDifference]:
    """An 'unavailable' technology state is unknown and can never match itself."""
    extra: List[IdentityDifference] = []
    for section in ("nvidia", "vulkan", "opencl"):
        for side in (saved, current):
            sec = side.get(section)
            if isinstance(sec, dict) and sec.get("state") == FeatureState.UNAVAILABLE.value:
                path = f"runtime.{section}.state"
                if not any(d.path.startswith(f"runtime.{section}") for d in already):
                    extra.append(
                        IdentityDifference(
                            Category.RUNTIME,
                            path,
                            (
                                saved.get(section, {}).get("state")
                                if isinstance(saved.get(section), dict)
                                else None
                            ),
                            (
                                current.get(section, {}).get("state")
                                if isinstance(current.get(section), dict)
                                else None
                            ),
                            f"{section} state is unknown (probe unavailable); prior acceptance "
                            "cannot be confirmed",
                            RequiredAction.NEW_ACCEPTANCE,
                        )
                    )
                break
    return extra


def compare_identities(
    saved: TargetIdentity, current: TargetIdentity, build_only: bool = False
) -> IdentityComparison:
    """Field-based comparison with explicit per-category semantics."""
    diffs: List[IdentityDifference] = []
    if saved.backend != current.backend:
        diffs.append(
            IdentityDifference(
                Category.BUILD,
                "backend",
                saved.backend,
                current.backend,
                "different backend",
                RequiredAction.REBUILD,
            )
        )
    _diff(saved.build, current.build, "build", Category.BUILD, diffs)
    if not build_only:
        if saved.runtime is not None or current.runtime is not None:
            if saved.runtime is None or current.runtime is None:
                diffs.append(
                    IdentityDifference(
                        Category.RUNTIME,
                        "runtime",
                        saved.runtime,
                        current.runtime,
                        "runtime identity present on only one side",
                        RequiredAction.NEW_ACCEPTANCE,
                    )
                )
            else:
                runtime_diffs: List[IdentityDifference] = []
                _diff(saved.runtime, current.runtime, "runtime", Category.RUNTIME, runtime_diffs)
                runtime_diffs += _unknown_runtime_differences(
                    saved.runtime, current.runtime, runtime_diffs
                )
                diffs += runtime_diffs
        _diff(saved.diagnostics, current.diagnostics, "diagnostics", Category.DIAGNOSTIC, diffs)
    result = Compatibility.COMPATIBLE
    for diff in diffs:
        candidate = _COMPAT_BY_CATEGORY[diff.category]
        if _SEVERITY[candidate] > _SEVERITY[result]:
            result = candidate
    return IdentityComparison(result, tuple(diffs))


def compare_saved(
    saved_raw: Any, current: TargetIdentity, build_only: bool = False
) -> IdentityComparison:
    """Compare a persisted identity (raw JSON value) with the current one.

    Missing/legacy/corrupt/unsupported saved identity yields ``UNKNOWN``; it is
    never treated as a match and never repaired from ``current``.
    """
    loaded = parse_identity(saved_raw)
    if loaded.identity is None:
        return IdentityComparison(
            Compatibility.UNKNOWN,
            (
                IdentityDifference(
                    Category.IDENTITY,
                    "identity",
                    loaded.status.value,
                    None,
                    loaded.reason,
                    RequiredAction.FRESH_BUILD,
                ),
            ),
        )
    return compare_identities(loaded.identity, current, build_only=build_only)


_HEX_RE = re.compile(r"[0-9a-f]+")


def is_valid_key(key: str) -> bool:
    return bool(_HEX_RE.fullmatch(key)) and len(key) == 16
