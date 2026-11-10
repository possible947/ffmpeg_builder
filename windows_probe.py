"""Fresh probe of the physical Windows target for the WSL-Mingw route.

The Windows machine running WSL is the build *target*. Its CPU/OS/GPU/runtime
state must come from Windows itself (through WSL interop) or from an explicit,
sufficient override; it must never be inferred from the WSL Linux view
(``/proc/cpuinfo``, Linux Vulkan loader, ...).

Public API:

* :class:`WindowsInterop` - the single transport boundary to ``powershell.exe``.
* :func:`probe_windows_target` - probe (and/or apply an override) and return a
  typed :class:`WindowsTargetState`.
* :func:`parse_target_state` / :func:`apply_override` - pure helpers, used by
  the probe and directly testable with fixtures.

Every optional technology (NVIDIA, Vulkan, OpenCL) carries a
:class:`FeatureState` so "absent", "not applicable" and "probe failed" are
never conflated. Every resulting field records whether it came from the fresh
probe or from an override (``WindowsTargetState.field_sources``).
"""

from __future__ import annotations

import base64
import errno
import gzip
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from .build_types import BuildError
from .windows_probe_script import PROBE_SCRIPT, PROBE_SCRIPT_VERSION

DEFAULT_POWERSHELL_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
DEFAULT_PROBE_TIMEOUT_SECONDS = 120.0
WSL_INTEROP_MARKER = Path("/proc/sys/fs/binfmt_misc/WSLInterop")

SOURCE_PROBE = "probe"
SOURCE_OVERRIDE = "override"

OVERRIDE_HINT = (
    "To build without WSL interop, provide a complete Windows target description in "
    "build_config.yaml under windows.target_override (mode: full). "
    "The WSL Linux view is never used as a substitute."
)


class ProbeFailure(str, Enum):
    """Why obtaining the Windows target state failed."""

    INTEROP_UNAVAILABLE = "interop_unavailable"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    TIMEOUT = "timeout"
    COMMAND_FAILED = "command_failed"
    MALFORMED_OUTPUT = "malformed_output"
    INCOMPLETE = "incomplete"
    INVALID_OVERRIDE = "invalid_override"


class WindowsProbeError(BuildError):
    """Actionable failure while probing the Windows target."""

    def __init__(self, kind: ProbeFailure, message: str, hint: Optional[str] = None):
        text = f"[{kind.value}] {message}"
        if hint:
            text = f"{text} {hint}"
        super().__init__("windows-target-probe", text)
        self.kind = kind
        self.detail = message
        self.hint = hint


class FeatureState(str, Enum):
    """Probe outcome for an optional technology."""

    PRESENT = "present"
    ABSENT = "absent"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"  # probe failed or was incomplete; state unknown


# --------------------------------------------------------------------------
# Interop transport
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InteropResult:
    """Raw outcome of a Windows command."""

    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[..., Any]


class WindowsInterop:
    """The one place that runs commands on the Windows host.

    Commands are executed with an argv list (never a shell). Scripts are
    passed as a gzip+base64 bootstrap inside ``-EncodedCommand`` so no quoting
    of user data is involved and the command line stays under the Windows
    limit.
    """

    def __init__(
        self,
        executable: Optional[str] = None,
        timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        runner: Runner = subprocess.run,
        which: Callable[[str], Optional[str]] = shutil.which,
        fallback_executable: str = DEFAULT_POWERSHELL_FALLBACK,
    ):
        self.executable = executable
        self.timeout = timeout
        self._runner = runner
        self._which = which
        self._fallback = fallback_executable

    def resolve_executable(self) -> str:
        """Locate ``powershell.exe`` (explicit path, PATH, then default mount)."""
        candidates: List[str] = []
        if self.executable:
            candidates.append(self.executable)
        found = self._which("powershell.exe")
        if found:
            candidates.append(found)
        candidates.append(self._fallback)
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
        raise WindowsProbeError(
            ProbeFailure.EXECUTABLE_NOT_FOUND,
            "powershell.exe was not found (searched: "
            + ", ".join(dict.fromkeys(candidates))
            + "). Windows interop appears unavailable in this environment.",
            OVERRIDE_HINT,
        )

    @staticmethod
    def build_command(executable: str, script: str) -> List[str]:
        """Build the argv that runs ``script`` on Windows PowerShell."""
        packed = base64.b64encode(gzip.compress(script.encode("utf-8"))).decode("ascii")
        bootstrap = (
            f"$b=[Convert]::FromBase64String('{packed}');"
            "$m=New-Object IO.MemoryStream(,$b);"
            "$g=New-Object IO.Compression.GZipStream($m,[IO.Compression.CompressionMode]::Decompress);"
            "$r=New-Object IO.StreamReader($g,[Text.Encoding]::UTF8);"
            "Invoke-Expression $r.ReadToEnd()"
        )
        encoded = base64.b64encode(bootstrap.encode("utf-16-le")).decode("ascii")
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]

    def run_powershell(self, script: str, timeout: Optional[float] = None) -> InteropResult:
        """Run a PowerShell script; return bytes output or raise on transport failure."""
        executable = self.resolve_executable()
        limit = self.timeout if timeout is None else timeout
        argv = self.build_command(executable, script)
        try:
            completed = self._runner(argv, capture_output=True, timeout=limit, check=False)
        except subprocess.TimeoutExpired as exc:
            raise WindowsProbeError(
                ProbeFailure.TIMEOUT,
                f"Windows probe did not finish within {limit:g}s.",
                "Check that the Windows host is responsive, or raise "
                "windows.probe_timeout_seconds. " + OVERRIDE_HINT,
            ) from exc
        except FileNotFoundError as exc:
            raise WindowsProbeError(
                ProbeFailure.EXECUTABLE_NOT_FOUND,
                f"{executable} could not be started (not found).",
                OVERRIDE_HINT,
            ) from exc
        except OSError as exc:
            if exc.errno in (errno.ENOEXEC, errno.EINVAL, errno.EACCES, errno.EPERM):
                raise WindowsProbeError(
                    ProbeFailure.INTEROP_UNAVAILABLE,
                    f"Windows executables cannot be launched from this environment ({exc.strerror}). "
                    "WSL interop is probably disabled "
                    f"({'binfmt entry present' if WSL_INTEROP_MARKER.exists() else 'binfmt_misc WSLInterop missing'}).",
                    "Enable interop in /etc/wsl.conf ([interop] enabled=true) and restart WSL. "
                    + OVERRIDE_HINT,
                ) from exc
            raise WindowsProbeError(
                ProbeFailure.COMMAND_FAILED,
                f"Could not start {executable}: {exc}",
                OVERRIDE_HINT,
            ) from exc
        return InteropResult(
            returncode=int(completed.returncode),
            stdout=completed.stdout or b"",
            stderr=completed.stderr or b"",
        )

    def run_json(self, script: str, timeout: Optional[float] = None) -> Any:
        """Run a script that prints one JSON document and return the parsed value."""
        result = self.run_powershell(script, timeout)
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            raise WindowsProbeError(
                ProbeFailure.COMMAND_FAILED,
                f"Windows command exited with code {result.returncode}"
                + (f": {_excerpt(stderr)}" if stderr else " with no error output."),
                OVERRIDE_HINT,
            )
        try:
            text = result.stdout.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise WindowsProbeError(
                ProbeFailure.MALFORMED_OUTPUT,
                f"Windows probe output is not valid UTF-8: {exc}",
            ) from exc
        if not text.strip():
            raise WindowsProbeError(
                ProbeFailure.MALFORMED_OUTPUT,
                "Windows probe produced no output."
                + (f" stderr: {_excerpt(stderr)}" if stderr else ""),
            )
        try:
            return json.loads(text)
        except ValueError as exc:
            raise WindowsProbeError(
                ProbeFailure.MALFORMED_OUTPUT,
                f"Windows probe output is not valid JSON ({exc}); first bytes: {_excerpt(text, 120)!r}",
            ) from exc


def _excerpt(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------
# Typed models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowsOSInfo:
    platform: str
    architecture: str
    version: str
    build_number: int
    product_name: Optional[str] = None
    edition: Optional[str] = None
    ubr: Optional[int] = None
    display_version: Optional[str] = None


@dataclass(frozen=True)
class CpuInfo:
    vendor: str
    family: int
    model: int
    stepping: int
    architecture: str
    isa_features: Mapping[str, bool]
    name: Optional[str] = None
    cores: Optional[int] = None
    logical_processors: Optional[int] = None


@dataclass(frozen=True)
class GpuAdapter:
    name: str
    pnp_device_id: str
    adapter_type: str  # "hardware" | "software"
    vendor_id: Optional[str] = None
    device_id: Optional[str] = None
    subsystem_id: Optional[str] = None
    revision: Optional[str] = None
    location: Optional[str] = None
    driver_version: Optional[str] = None


@dataclass(frozen=True)
class NvidiaDevice:
    name: str
    uuid: str
    driver_version: str
    compute_capability: Optional[str] = None
    pci_bus_id: Optional[str] = None
    pci_device_id: Optional[str] = None
    pci_sub_device_id: Optional[str] = None
    # PNP id of the matching entry in the general GPU inventory; ``None`` when
    # the mapping could not be established unambiguously.
    gpu_pnp_device_id: Optional[str] = None


@dataclass(frozen=True)
class NvidiaState:
    state: FeatureState
    devices: Tuple[NvidiaDevice, ...] = ()
    api: Mapping[str, Optional[str]] = field(default_factory=dict)
    error: Optional[str] = None
    source: str = SOURCE_PROBE


@dataclass(frozen=True)
class VulkanDevice:
    name: str
    vendor_id: str
    device_id: str
    device_type: str
    api_version: str
    driver_version: Optional[str] = None


@dataclass(frozen=True)
class VulkanState:
    state: FeatureState
    loader_version: Optional[str] = None
    devices: Tuple[VulkanDevice, ...] = ()
    icd_manifests: Tuple[str, ...] = ()
    error: Optional[str] = None
    source: str = SOURCE_PROBE


@dataclass(frozen=True)
class OpenCLDevice:
    name: str
    vendor: str
    device_type: str
    vendor_id: Optional[str] = None
    version: Optional[str] = None
    driver_version: Optional[str] = None


@dataclass(frozen=True)
class OpenCLPlatform:
    name: str
    vendor: str
    version: Optional[str] = None
    devices: Tuple[OpenCLDevice, ...] = ()


@dataclass(frozen=True)
class OpenCLState:
    state: FeatureState
    dispatcher_version: Optional[str] = None
    platforms: Tuple[OpenCLPlatform, ...] = ()
    icd_vendors: Tuple[str, ...] = ()
    error: Optional[str] = None
    source: str = SOURCE_PROBE


@dataclass(frozen=True)
class WindowsTargetState:
    """Typed snapshot of the physical Windows target."""

    os: WindowsOSInfo
    cpu: CpuInfo
    gpus: Tuple[GpuAdapter, ...]
    nvidia: NvidiaState
    vulkan: VulkanState
    opencl: OpenCLState
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    field_sources: Mapping[str, str] = field(default_factory=dict)
    probed_at: Optional[str] = None

    @property
    def uses_override(self) -> bool:
        return SOURCE_OVERRIDE in self.field_sources.values()


# --------------------------------------------------------------------------
# Raw schema + validation
# --------------------------------------------------------------------------

TECH_SECTIONS = ("nvidia", "vulkan", "opencl")
REPLACED_SECTIONS = ("gpus",) + TECH_SECTIONS
MERGED_SECTIONS = ("os", "cpu")
ALL_SECTIONS = MERGED_SECTIONS + REPLACED_SECTIONS + ("diagnostics",)

_ALLOWED_KEYS: Dict[str, Set[str]] = {
    "os": {
        "platform",
        "architecture",
        "version",
        "build_number",
        "product_name",
        "edition",
        "ubr",
        "display_version",
    },
    "cpu": {
        "vendor",
        "family",
        "model",
        "stepping",
        "architecture",
        "isa_features",
        "name",
        "cores",
        "logical_processors",
    },
    "gpu": {
        "name",
        "pnp_device_id",
        "adapter_type",
        "vendor_id",
        "device_id",
        "subsystem_id",
        "revision",
        "location",
        "driver_version",
    },
    "nvidia": {"state", "devices", "api", "error"},
    "nvidia_device": {
        "name",
        "uuid",
        "driver_version",
        "compute_capability",
        "pci_bus_id",
        "pci_device_id",
        "pci_sub_device_id",
        "gpu_pnp_device_id",
    },
    "vulkan": {"state", "loader", "devices", "icd_manifests", "error"},
    "vulkan_device": {
        "name",
        "vendor_id",
        "device_id",
        "device_type",
        "api_version",
        "driver_version",
    },
    "opencl": {"state", "dispatcher", "platforms", "icd_vendors", "error"},
    "opencl_platform": {"name", "vendor", "version", "devices"},
    "opencl_device": {
        "name",
        "vendor",
        "device_type",
        "vendor_id",
        "version",
        "driver_version",
    },
}

_OVERRIDE_MODES = ("full", "supplement")


@dataclass(frozen=True)
class TargetOverride:
    """Validated explicit Windows target override."""

    mode: str
    data: Mapping[str, Any]


def _fail_invalid(message: str) -> WindowsProbeError:
    return WindowsProbeError(ProbeFailure.INVALID_OVERRIDE, message)


def _fail_incomplete(message: str) -> WindowsProbeError:
    return WindowsProbeError(ProbeFailure.INCOMPLETE, message, OVERRIDE_HINT)


def parse_override(raw: Optional[Mapping[str, Any]]) -> Optional[TargetOverride]:
    """Validate the structure of ``windows.target_override``.

    ``mode: full`` (default) means no probe is run and the override alone must
    be sufficient. ``mode: supplement`` probes first and replaces the supplied
    fields: ``os``/``cpu`` field-by-field, ``gpus``/``nvidia``/``vulkan``/
    ``opencl`` as whole sections.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _fail_invalid(f"windows.target_override must be a mapping, got {type(raw).__name__}.")
    data = dict(raw)
    mode = data.pop("mode", "full")
    if mode not in _OVERRIDE_MODES:
        raise _fail_invalid(
            f"windows.target_override.mode must be one of {_OVERRIDE_MODES}, got {mode!r}."
        )
    unknown = sorted(set(data) - set(ALL_SECTIONS))
    if unknown:
        raise _fail_invalid(
            f"Unknown windows.target_override section(s): {unknown}; allowed: {list(ALL_SECTIONS)}."
        )
    for name in MERGED_SECTIONS:
        if name in data:
            section = data[name]
            if not isinstance(section, Mapping):
                raise _fail_invalid(f"windows.target_override.{name} must be a mapping.")
            bad = sorted(set(section) - _ALLOWED_KEYS[name])
            if bad:
                raise _fail_invalid(f"Unknown field(s) in windows.target_override.{name}: {bad}.")
    if "gpus" in data and not isinstance(data["gpus"], list):
        raise _fail_invalid("windows.target_override.gpus must be a list (empty list = no GPU).")
    for name in TECH_SECTIONS:
        if name in data:
            section = data[name]
            if not isinstance(section, Mapping):
                raise _fail_invalid(f"windows.target_override.{name} must be a mapping.")
            state = section.get("state")
            if state not in (
                FeatureState.PRESENT.value,
                FeatureState.ABSENT.value,
                FeatureState.NOT_APPLICABLE.value,
            ):
                raise _fail_invalid(
                    f"windows.target_override.{name}.state must be present, absent or "
                    f"not_applicable (an override cannot be 'unavailable'); got {state!r}."
                )
    if mode == "full":
        missing = [name for name in ALL_SECTIONS[:-1] if name not in data]
        if missing:
            raise _fail_invalid(
                f"Insufficient windows.target_override (mode: full): missing section(s) {missing}. "
                "Every section must be stated explicitly; use state: absent for hardware that "
                "is not installed. Use mode: supplement to complete a fresh probe instead."
            )
    return TargetOverride(mode=mode, data=data)


def apply_override(
    probed: Optional[Mapping[str, Any]], override: TargetOverride
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Merge an override over raw probe data.

    Returns ``(merged_raw, sources)`` where ``sources`` maps ``section`` (or
    ``section.field`` for ``os``/``cpu``) to ``"probe"`` or ``"override"``.
    """
    merged: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    base = dict(probed) if probed is not None else {}
    for name in MERGED_SECTIONS:
        fields_: Dict[str, Any] = {}
        base_section = base.get(name)
        if isinstance(base_section, Mapping):
            for key, value in base_section.items():
                fields_[key] = value
                sources[f"{name}.{key}"] = SOURCE_PROBE
        over_section = override.data.get(name)
        if isinstance(over_section, Mapping):
            for key, value in over_section.items():
                fields_[key] = value
                sources[f"{name}.{key}"] = SOURCE_OVERRIDE
        if fields_ or name in base or name in override.data:
            merged[name] = fields_
    for name in REPLACED_SECTIONS:
        if name in override.data:
            merged[name] = override.data[name]
            sources[name] = SOURCE_OVERRIDE
        elif name in base:
            merged[name] = base[name]
            sources[name] = SOURCE_PROBE
    diagnostics: Dict[str, Any] = {}
    for source_data in (base, override.data):
        if isinstance(source_data.get("diagnostics"), Mapping):
            diagnostics.update(source_data["diagnostics"])
    if diagnostics:
        merged["diagnostics"] = diagnostics
    return merged, sources


# --- parsing helpers ---


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail_incomplete(f"Windows target field {path!r} is missing or not an object.")
    return value


def _check_keys(data: Mapping[str, Any], kind: str, path: str) -> None:
    unknown = sorted(set(data) - _ALLOWED_KEYS[kind])
    if unknown:
        raise _fail_incomplete(f"Unknown field(s) {unknown} in Windows target data at {path!r}.")


def _opt_str(data: Mapping[str, Any], key: str, path: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _fail_incomplete(f"Windows target field {path}.{key} must be a string.")
    return str(value).strip()


def _req_str(data: Mapping[str, Any], key: str, path: str) -> str:
    value = _opt_str(data, key, path)
    if value is None or value == "":
        raise _fail_incomplete(f"Required Windows target field {path}.{key} is missing or empty.")
    return value


def _opt_int(data: Mapping[str, Any], key: str, path: str) -> Optional[int]:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _fail_incomplete(f"Windows target field {path}.{key} must be an integer.")
    try:
        return int(str(value).strip(), 10) if not isinstance(value, int) else value
    except ValueError as exc:
        raise _fail_incomplete(
            f"Windows target field {path}.{key} must be an integer, got {value!r}."
        ) from exc


def _req_int(data: Mapping[str, Any], key: str, path: str) -> int:
    value = _opt_int(data, key, path)
    if value is None:
        raise _fail_incomplete(f"Required Windows target field {path}.{key} is missing.")
    return value


def _hex_id(data: Mapping[str, Any], key: str, path: str, width: int) -> Optional[str]:
    value = _opt_str(data, key, path)
    if value is None or value == "":
        return None
    text = value.lower()
    if text.startswith("0x"):
        text = text[2:]
    if not re.fullmatch(r"[0-9a-f]+", text):
        raise _fail_incomplete(f"Windows target field {path}.{key} is not a hex id: {value!r}.")
    return text.zfill(width)


def _list(value: Any, path: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail_incomplete(f"Windows target field {path!r} must be a list.")
    return value


def _feature_state(section: Mapping[str, Any], path: str) -> FeatureState:
    raw = section.get("state")
    try:
        return FeatureState(raw)
    except ValueError as exc:
        raise _fail_incomplete(
            f"Windows target field {path}.state must be one of "
            f"{[s.value for s in FeatureState]}, got {raw!r}."
        ) from exc


def _parse_os(raw: Any) -> WindowsOSInfo:
    data = _mapping(raw, "os")
    _check_keys(data, "os", "os")
    platform = _req_str(data, "platform", "os").lower()
    if platform != "windows":
        raise _fail_incomplete(f"Target os.platform must be 'windows', got {platform!r}.")
    return WindowsOSInfo(
        platform=platform,
        architecture=_req_str(data, "architecture", "os").lower(),
        version=_req_str(data, "version", "os"),
        build_number=_req_int(data, "build_number", "os"),
        product_name=_opt_str(data, "product_name", "os"),
        edition=_opt_str(data, "edition", "os"),
        ubr=_opt_int(data, "ubr", "os"),
        display_version=_opt_str(data, "display_version", "os"),
    )


def _parse_cpu(raw: Any) -> CpuInfo:
    data = _mapping(raw, "cpu")
    _check_keys(data, "cpu", "cpu")
    features = data.get("isa_features")
    if not isinstance(features, Mapping) or not features:
        raise _fail_incomplete(
            "Required Windows target field cpu.isa_features is missing or empty "
            "(OS-reported instruction-set state is needed for CPU specialisation)."
        )
    isa: Dict[str, bool] = {}
    for name, value in features.items():
        if not isinstance(value, bool):
            raise _fail_incomplete(f"cpu.isa_features.{name} must be true/false, got {value!r}.")
        isa[str(name).strip().lower()] = value
    return CpuInfo(
        vendor=_req_str(data, "vendor", "cpu"),
        family=_req_int(data, "family", "cpu"),
        model=_req_int(data, "model", "cpu"),
        stepping=_req_int(data, "stepping", "cpu"),
        architecture=_req_str(data, "architecture", "cpu").lower(),
        isa_features=isa,
        name=_opt_str(data, "name", "cpu"),
        cores=_opt_int(data, "cores", "cpu"),
        logical_processors=_opt_int(data, "logical_processors", "cpu"),
    )


def _parse_gpus(raw: Any) -> Tuple[GpuAdapter, ...]:
    if not isinstance(raw, list):
        raise _fail_incomplete("Required Windows target field 'gpus' is missing or not a list.")
    gpus: List[GpuAdapter] = []
    for index, item in enumerate(raw):
        path = f"gpus[{index}]"
        data = _mapping(item, path)
        _check_keys(data, "gpu", path)
        adapter_type = _req_str(data, "adapter_type", path).lower()
        if adapter_type not in ("hardware", "software"):
            raise _fail_incomplete(f"{path}.adapter_type must be hardware or software.")
        vendor_id = _hex_id(data, "vendor_id", path, 4)
        device_id = _hex_id(data, "device_id", path, 4)
        if adapter_type == "hardware" and (vendor_id is None or device_id is None):
            raise _fail_incomplete(f"Hardware adapter {path} requires vendor_id and device_id.")
        gpus.append(
            GpuAdapter(
                name=_req_str(data, "name", path),
                pnp_device_id=_req_str(data, "pnp_device_id", path).upper(),
                adapter_type=adapter_type,
                vendor_id=vendor_id,
                device_id=device_id,
                subsystem_id=_hex_id(data, "subsystem_id", path, 8),
                revision=_hex_id(data, "revision", path, 2),
                location=_opt_str(data, "location", path),
                driver_version=_opt_str(data, "driver_version", path),
            )
        )
    return tuple(gpus)


def _bus_from_location(location: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not location:
        return None
    match = re.search(r"bus\s+(\d+),\s*device\s+(\d+),\s*function\s+(\d+)", location, re.I)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _bus_from_nvidia(pci_bus_id: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not pci_bus_id:
        return None
    match = re.fullmatch(
        r"(?:[0-9a-f]{4,8}:)?([0-9a-f]{2}):([0-9a-f]{2})\.([0-9a-f])", pci_bus_id.strip(), re.I
    )
    return (int(match[1], 16), int(match[2], 16), int(match[3], 16)) if match else None


def _link_nvidia_device(device: Mapping[str, Any], gpus: Tuple[GpuAdapter, ...]) -> Optional[str]:
    """Map an NVIDIA device to the general GPU inventory (None if ambiguous)."""
    nvidia_gpus = [g for g in gpus if g.vendor_id == "10de" and g.adapter_type == "hardware"]
    bus = _bus_from_nvidia(device.get("pci_bus_id"))
    if bus is not None:
        by_bus = [g for g in nvidia_gpus if _bus_from_location(g.location) == bus]
        if len(by_bus) == 1:
            return by_bus[0].pnp_device_id
    pci_id = str(device.get("pci_device_id") or "").lower().removeprefix("0x")
    candidates = nvidia_gpus
    if re.fullmatch(r"[0-9a-f]{8}", pci_id):
        dev_id = pci_id[:4]
        candidates = [g for g in candidates if g.device_id == dev_id]
    if len(candidates) == 1:
        return candidates[0].pnp_device_id
    return None


def _parse_nvidia(raw: Any, gpus: Tuple[GpuAdapter, ...], source: str) -> NvidiaState:
    data = _mapping(raw, "nvidia")
    _check_keys(data, "nvidia", "nvidia")
    state = _feature_state(data, "nvidia")
    devices: List[NvidiaDevice] = []
    for index, item in enumerate(_list(data.get("devices"), "nvidia.devices")):
        path = f"nvidia.devices[{index}]"
        dev = _mapping(item, path)
        _check_keys(dev, "nvidia_device", path)
        explicit = _opt_str(dev, "gpu_pnp_device_id", path)
        devices.append(
            NvidiaDevice(
                name=_req_str(dev, "name", path),
                uuid=_req_str(dev, "uuid", path),
                driver_version=_req_str(dev, "driver_version", path),
                compute_capability=_opt_str(dev, "compute_capability", path),
                pci_bus_id=_opt_str(dev, "pci_bus_id", path),
                pci_device_id=_opt_str(dev, "pci_device_id", path),
                pci_sub_device_id=_opt_str(dev, "pci_sub_device_id", path),
                gpu_pnp_device_id=(
                    explicit.upper() if explicit else _link_nvidia_device(dev, gpus)
                ),
            )
        )
    if state == FeatureState.PRESENT and not devices:
        raise _fail_incomplete("nvidia.state is 'present' but no NVIDIA devices were reported.")
    api_raw = data.get("api") or {}
    if not isinstance(api_raw, Mapping):
        raise _fail_incomplete("nvidia.api must be an object.")
    api = {str(k): (None if v is None else str(v).strip()) for k, v in api_raw.items()}
    return NvidiaState(
        state=state,
        devices=tuple(devices),
        api=api,
        error=_opt_str(data, "error", "nvidia"),
        source=source,
    )


def _parse_vulkan(raw: Any, source: str) -> VulkanState:
    data = _mapping(raw, "vulkan")
    _check_keys(data, "vulkan", "vulkan")
    state = _feature_state(data, "vulkan")
    loader = data.get("loader")
    loader_version: Optional[str] = None
    if loader is not None:
        loader_version = _opt_str(_mapping(loader, "vulkan.loader"), "version", "vulkan.loader")
    devices: List[VulkanDevice] = []
    for index, item in enumerate(_list(data.get("devices"), "vulkan.devices")):
        path = f"vulkan.devices[{index}]"
        dev = _mapping(item, path)
        _check_keys(dev, "vulkan_device", path)
        vendor_id = _hex_id(dev, "vendor_id", path, 4)
        device_id = _hex_id(dev, "device_id", path, 4)
        if vendor_id is None or device_id is None:
            raise _fail_incomplete(f"{path} requires vendor_id and device_id.")
        devices.append(
            VulkanDevice(
                name=_req_str(dev, "name", path),
                vendor_id=vendor_id,
                device_id=device_id,
                device_type=_req_str(dev, "device_type", path).lower(),
                api_version=_req_str(dev, "api_version", path),
                driver_version=_opt_str(dev, "driver_version", path),
            )
        )
    if state == FeatureState.PRESENT and loader_version is None and loader is None:
        raise _fail_incomplete("vulkan.state is 'present' but vulkan.loader is missing.")
    manifests = tuple(str(x) for x in _list(data.get("icd_manifests"), "vulkan.icd_manifests"))
    return VulkanState(
        state=state,
        loader_version=loader_version,
        devices=tuple(devices),
        icd_manifests=manifests,
        error=_opt_str(data, "error", "vulkan"),
        source=source,
    )


def _parse_opencl(raw: Any, source: str) -> OpenCLState:
    data = _mapping(raw, "opencl")
    _check_keys(data, "opencl", "opencl")
    state = _feature_state(data, "opencl")
    dispatcher = data.get("dispatcher")
    dispatcher_version: Optional[str] = None
    if dispatcher is not None:
        dispatcher_version = _opt_str(
            _mapping(dispatcher, "opencl.dispatcher"), "version", "opencl.dispatcher"
        )
    platforms: List[OpenCLPlatform] = []
    for pindex, pitem in enumerate(_list(data.get("platforms"), "opencl.platforms")):
        ppath = f"opencl.platforms[{pindex}]"
        pdata = _mapping(pitem, ppath)
        _check_keys(pdata, "opencl_platform", ppath)
        devices: List[OpenCLDevice] = []
        for dindex, ditem in enumerate(_list(pdata.get("devices"), f"{ppath}.devices")):
            dpath = f"{ppath}.devices[{dindex}]"
            ddata = _mapping(ditem, dpath)
            _check_keys(ddata, "opencl_device", dpath)
            devices.append(
                OpenCLDevice(
                    name=_req_str(ddata, "name", dpath),
                    vendor=_req_str(ddata, "vendor", dpath),
                    device_type=_req_str(ddata, "device_type", dpath).lower(),
                    vendor_id=_hex_id(ddata, "vendor_id", dpath, 4),
                    version=_opt_str(ddata, "version", dpath),
                    driver_version=_opt_str(ddata, "driver_version", dpath),
                )
            )
        platforms.append(
            OpenCLPlatform(
                name=_req_str(pdata, "name", ppath),
                vendor=_req_str(pdata, "vendor", ppath),
                version=_opt_str(pdata, "version", ppath),
                devices=tuple(devices),
            )
        )
    if state == FeatureState.PRESENT and not platforms:
        raise _fail_incomplete("opencl.state is 'present' but no platforms were reported.")
    vendors = tuple(str(x) for x in _list(data.get("icd_vendors"), "opencl.icd_vendors"))
    return OpenCLState(
        state=state,
        dispatcher_version=dispatcher_version,
        platforms=tuple(platforms),
        icd_vendors=vendors,
        error=_opt_str(data, "error", "opencl"),
        source=source,
    )


def parse_target_state(
    raw: Any,
    sources: Optional[Mapping[str, str]] = None,
    probed_at: Optional[str] = None,
) -> WindowsTargetState:
    """Validate raw probe/override data and build the typed model.

    Raises :class:`WindowsProbeError` (``incomplete``) when required fields are
    missing, so an incomplete probe is never silently accepted.
    """
    if not isinstance(raw, Mapping):
        raise WindowsProbeError(
            ProbeFailure.MALFORMED_OUTPUT,
            f"Windows probe returned {type(raw).__name__}, expected a JSON object.",
        )
    unknown = sorted(set(raw) - set(ALL_SECTIONS))
    if unknown:
        raise _fail_incomplete(f"Unknown top-level section(s) in Windows target data: {unknown}.")
    missing = [name for name in ALL_SECTIONS[:-1] if name not in raw]
    if missing:
        raise _fail_incomplete(f"Windows target data is missing required section(s): {missing}.")
    field_sources = dict(sources) if sources is not None else _default_sources(raw)
    gpus = _parse_gpus(raw["gpus"])
    diagnostics = raw.get("diagnostics") or {}
    if not isinstance(diagnostics, Mapping):
        raise _fail_incomplete("Windows target diagnostics must be an object.")
    return WindowsTargetState(
        os=_parse_os(raw["os"]),
        cpu=_parse_cpu(raw["cpu"]),
        gpus=gpus,
        nvidia=_parse_nvidia(raw["nvidia"], gpus, field_sources.get("nvidia", SOURCE_PROBE)),
        vulkan=_parse_vulkan(raw["vulkan"], field_sources.get("vulkan", SOURCE_PROBE)),
        opencl=_parse_opencl(raw["opencl"], field_sources.get("opencl", SOURCE_PROBE)),
        diagnostics=dict(diagnostics),
        field_sources=field_sources,
        probed_at=probed_at,
    )


def _default_sources(raw: Mapping[str, Any]) -> Dict[str, str]:
    sources: Dict[str, str] = {}
    for name in MERGED_SECTIONS:
        section = raw.get(name)
        if isinstance(section, Mapping):
            for key in section:
                sources[f"{name}.{key}"] = SOURCE_PROBE
    for name in REPLACED_SECTIONS:
        if name in raw:
            sources[name] = SOURCE_PROBE
    return sources


# --------------------------------------------------------------------------
# Public probe API
# --------------------------------------------------------------------------


def probe_windows_target(
    override: Optional[Mapping[str, Any]] = None,
    interop: Optional[WindowsInterop] = None,
    timeout: Optional[float] = None,
) -> WindowsTargetState:
    """Obtain the current Windows target state.

    * No override: run a fresh probe through WSL interop.
    * ``mode: full`` override: no probe; the override must be sufficient.
    * ``mode: supplement`` override: fresh probe, then explicit field/section
      replacement. A failing probe is **not** rescued by a supplement.

    Never falls back to Linux/WSL state or to a previously saved snapshot.
    """
    parsed = parse_override(override)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if parsed is not None and parsed.mode == "full":
        merged, sources = apply_override(None, parsed)
        return parse_target_state(merged, sources, probed_at=now)

    transport = interop or WindowsInterop()
    raw = transport.run_json(PROBE_SCRIPT, timeout)
    if not isinstance(raw, Mapping):
        raise WindowsProbeError(
            ProbeFailure.MALFORMED_OUTPUT,
            f"Windows probe returned {type(raw).__name__}, expected a JSON object.",
        )
    if parsed is None:
        state = parse_target_state(raw, probed_at=now)
    else:
        merged, sources = apply_override(raw, parsed)
        state = parse_target_state(merged, sources, probed_at=now)
    diagnostics = dict(state.diagnostics)
    diagnostics.setdefault("probe_script_version", PROBE_SCRIPT_VERSION)
    return WindowsTargetState(
        os=state.os,
        cpu=state.cpu,
        gpus=state.gpus,
        nvidia=state.nvidia,
        vulkan=state.vulkan,
        opencl=state.opencl,
        diagnostics=diagnostics,
        field_sources=state.field_sources,
        probed_at=state.probed_at,
    )
