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

IDENTITY_SCHEMA_VERSION = 1
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
    rendered as strings so no binary rounding leaks into comparisons.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else format(value, ".12g")
    if isinstance(value, str):
        return _collapse(value)
    if isinstance(value, Mapping):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    raise TypeError(f"Unsupported identity value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable serialisation used for sorting and fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


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
            "build": self.build,
            "runtime": self.runtime,
            "diagnostics": self.diagnostics,
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
    if version != IDENTITY_SCHEMA_VERSION:
        return IdentityLoad(
            IdentityLoadStatus.UNSUPPORTED_SCHEMA,
            None,
            f"identity schema_version {version} is not supported "
            f"(this version understands {IDENTITY_SCHEMA_VERSION})",
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


def _material_config(config_dict: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: normalize(value)
        for key, value in sorted(config_dict.items())
        if key not in NON_MATERIAL_CONFIG_FIELDS
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
) -> TargetIdentity:
    """Identity for the WSL-Mingw (``linux-cross-mingw``) route.

    ``components`` (name -> version) and ``toolchains`` (component -> profile)
    are the project-selected inputs already resolved by the application. Later
    gates add their material fields (LLVM-MinGW, Rust target std, CUDA
    architecture, ``-ccbin``, ...) under new keys; an old saved identity then
    lacks them and is treated as build-incompatible rather than accidentally
    compatible.
    """
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
        "config": _material_config(config_dict),
        "cpu_specialization": cpu_spec,
        "components": {k: normalize(v) for k, v in sorted((components or {}).items())},
        "toolchains": {k: normalize(v) for k, v in sorted((toolchains or {}).items())},
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
