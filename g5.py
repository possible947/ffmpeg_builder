"""Consumer-oriented physical Windows runtime acceptance for a G4 build."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

from .app import PROJECT_ROOT
from .build_types import BuildError
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .build_identity import runtime_identity
from .cross_environment import TargetTools, _resolve_target_tools
from .g4_final import _accepted_names, _digest, _imports, _installed_hashes, _interface_hashes
from .g4_wave6 import validate_prior_manifests
from .g5_nvidia import run_nvidia_runtime
from .g5_video import run_video_runtime
from .target_inputs import validate_target_inputs_manifest
from .windows_probe import FeatureState, WindowsInterop
from .wsl_mingw_preparation import project_material_build_inputs

NVIDIA_FEATURES = (
    "h264_nvenc",
    "hevc_nvenc",
    "h264_cuvid_decode",
    "hevc_cuvid_decode",
    "scale_cuda",
    "libvmaf_cuda",
    "av1_nvenc",
    "av1_cuvid_decode",
)

VIDEO_FEATURES = {
    "d3d11va_h264_decode": "d3d11va",
    "d3d11va_hevc_decode": "d3d11va",
    "d3d12va_h264_decode": "d3d12va",
    "d3d12va_hevc_decode": "d3d12va",
    "vulkan_h264_decode": "vulkan",
    "vulkan_hevc_decode": "vulkan",
    "vulkan_av1_decode": "vulkan",
    "h264_d3d12va_encode": "d3d12va",
    "hevc_d3d12va_encode": "d3d12va",
    "h264_vulkan_encode": "vulkan",
    "hevc_vulkan_encode": "vulkan",
    "av1_vulkan_encode": "vulkan",
    "hevc_d3d12va_roundtrip": "d3d12va",
}

OUT_OF_SCOPE_TECHNOLOGIES = frozenset(("d3d", "d3d11va", "d3d12va", "dxva2"))
OUT_OF_SCOPE_REASON = "Windows-specific D3D11VA, D3D12VA and DXVA2 are outside consumer G5."


def _out_of_scope_features() -> list[dict[str, str]]:
    return [
        {
            "technology": technology,
            "feature": "runtime_matrix",
            "status": "OUT_OF_SCOPE",
            "detail": OUT_OF_SCOPE_REASON,
        }
        for technology in ("d3d11va", "d3d12va", "dxva2")
    ]


def _deferred_features() -> list[dict[str, str]]:
    return [
        {
            "technology": "neural",
            "feature": "neural_backend",
            "status": "DEFERRED",
            "detail": "Planned neural functionality is not part of the production contract.",
        },
        {
            "technology": "interop",
            "feature": "future_consumer_chains",
            "status": "DEFERRED",
            "detail": "Unspecified future filter chains are not part of the production contract.",
        },
    ]


def validate_framemd5(
    output: str, *, frames: int = 4, dimensions: tuple[int, int] | None = None
) -> list[str]:
    """Require actual decoded frame records, not just a successful process exit."""
    records = []
    if dimensions and not re.search(
        rf"(?m)^#dimensions\s+0:\s*{dimensions[0]}x{dimensions[1]}\s*$", output
    ):
        raise ValueError(f"Missing expected frame dimensions {dimensions}")
    pts: list[int] = []
    for line in output.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        if (
            len(fields) != 6
            or not all(re.fullmatch(r"-?\d+", item) for item in fields[:5])
            or int(fields[0]) != 0
            or int(fields[3]) <= 0
            or int(fields[4]) <= 0
            or (dimensions is not None and int(fields[4]) != dimensions[0] * dimensions[1] * 3 // 2)
            or not re.fullmatch(r"[a-fA-F0-9]{32}", fields[5])
        ):
            raise ValueError(f"Invalid frame checksum record: {line!r}")
        records.append(line)
        pts.append(int(fields[2]))
    if len(records) != frames:
        raise ValueError(f"Expected {frames} frame checksum records, got {len(records)}")
    if any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError("Frame timestamps are not strictly increasing")
    return records


def unique_device_match(name: str, candidates: list[tuple[str, str]]) -> str:
    """Resolve a probe device to one FFmpeg selector, rejecting ambiguous names."""
    key = " ".join(name.casefold().split())
    matches = [
        selector
        for selector, candidate in candidates
        if " ".join(candidate.casefold().split()) == key
    ]
    if not key or len(matches) != 1:
        raise ValueError(f"Cannot uniquely map probe device {name!r} to an FFmpeg selector")
    return matches[0]


def pending_features(target: Any, manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Make incomplete coverage visible without inferring support from inventory."""
    results: list[dict[str, str]] = []

    def add(technology: str, feature: str, state: Any, devices: list[str]) -> None:
        if state == FeatureState.PRESENT and devices:
            results.extend(
                {
                    "technology": technology,
                    "feature": feature,
                    "device": device,
                    "status": "UNVALIDATED",
                    "probe_index": str(index),
                }
                for index, device in enumerate(devices)
            )
        else:
            status = {
                FeatureState.ABSENT: "ABSENT",
                FeatureState.NOT_APPLICABLE: "UNSUPPORTED",
            }.get(state, "UNVALIDATED")
            results.append({"technology": technology, "feature": feature, "status": status})

    for feature in ("scale_vulkan", "libplacebo"):
        add("vulkan", feature, target.vulkan.state, [d.name for d in target.vulkan.devices])
    add(
        "opencl",
        "avgblur_opencl",
        target.opencl.state,
        [d.name for p in target.opencl.platforms for d in p.devices],
    )
    for feature in NVIDIA_FEATURES:
        add("nvidia", feature, target.nvidia.state, [d.name for d in target.nvidia.devices])
    for gpu in target.gpus:
        if gpu.adapter_type == "hardware":
            results.append(
                {
                    "technology": "d3d",
                    "feature": "runtime_matrix",
                    "device": gpu.pnp_device_id,
                    "status": "OUT_OF_SCOPE",
                    "detail": OUT_OF_SCOPE_REASON,
                }
            )
            for feature, technology in VIDEO_FEATURES.items():
                results.append(
                    {
                        "technology": technology,
                        "feature": feature,
                        "device": gpu.name,
                        "pnp_device_id": gpu.pnp_device_id,
                        "status": (
                            "OUT_OF_SCOPE"
                            if technology in OUT_OF_SCOPE_TECHNOLOGIES
                            else {
                                FeatureState.ABSENT: "ABSENT",
                                FeatureState.NOT_APPLICABLE: "UNSUPPORTED",
                            }.get(target.vulkan.state, "UNVALIDATED")
                        ),
                        **(
                            {"detail": OUT_OF_SCOPE_REASON}
                            if technology in OUT_OF_SCOPE_TECHNOLOGIES
                            else {}
                        ),
                    }
                )
    results.extend(_out_of_scope_features())
    hardware = [gpu for gpu in target.gpus if gpu.adapter_type == "hardware"]
    vendors = [
        str(getattr(gpu, "vendor_id", "") or "").lower().removeprefix("0x") for gpu in hardware
    ]
    for technology, vendor in (("qsv", "8086"), ("amf", "1002")):
        absent = bool(vendors) and all(vendors) and vendor not in vendors
        results.append(
            {
                "technology": technology,
                "feature": "runtime_matrix",
                "status": "ABSENT" if absent else "UNVALIDATED",
                **({"detail": "No corresponding hardware GPU was probed."} if absent else {}),
            }
        )
    results.extend(_deferred_features())
    return results


def verify_g4(app: Any, config: BuildConfig, session: Any, environment: Any) -> dict[str, Any]:
    """Verify the same immutable FFmpeg outputs and dependency inventory as G4 reuse."""
    workspace = session.workspace
    prefix = environment.paths.target_prefix
    selection = app._get_component_selection(config)
    selected = (
        selection.target_inputs + selection.target_components_to_build + selection.mixed_components
    )
    ffmpeg = [item for item in selected if item.name == "ffmpeg"]
    if len(ffmpeg) != 1:
        raise BuildError("G5", "FFmpeg was not selected exactly once.")
    component = ffmpeg[0]
    dependencies = [item for item in selected if item.name != "ffmpeg"]
    accepted = _accepted_names(workspace)
    for item in selected:
        if not app.state_manager.is_component_completed(item.name, item.version):
            raise BuildError("G5", f"{item.name} {item.version} is not completed.")
        if item.name != "ffmpeg" and item.name not in accepted:
            raise BuildError("G5", f"{item.name} has no accepted dependency manifest.")

    manifests = sorted(workspace.glob("g4-wave*-acceptance.json"))
    final = workspace / "g4-final-acceptance.json"
    if not manifests:
        raise BuildError("G5", "G4 dependency manifests are missing.")
    before = {path.name: _digest(path) for path in [*manifests, final]}
    count = validate_prior_manifests(workspace, prefix, session.identity.build_key, "")
    manifest = json.loads(final.read_text(encoding="utf-8"))
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    source_hash = _digest(archives / component.get_archive_filename())
    binaries = {name: _digest(workspace / "bin" / name) for name in ("ffmpeg.exe", "ffprobe.exe")}
    if (
        manifest.get("identity_key") != session.identity.build_key
        or manifest.get("source_sha256") != source_hash
        or manifest.get("binaries") != binaries
        or manifest.get("dependencies") != [item.name for item in dependencies]
        or manifest.get("installed") != _installed_hashes(prefix)
        or manifest.get("interfaces") != _interface_hashes(prefix)
    ):
        raise BuildError("G5", "Accepted FFmpeg outputs or inputs changed; refusing runtime use.")
    after = {path.name: _digest(path) for path in [*manifests, final]}
    if before != after:
        raise BuildError("G5", "G4 manifests changed during verification.")
    return {
        "manifests": before,
        "binaries": binaries,
        "source_sha256": source_hash,
        "dependency_files_verified": count,
    }


def verify_production_outputs(
    app: Any, config: BuildConfig, session: Any, environment: Any, manifest_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify normal-workflow outputs without requiring historical G4 wave runs."""
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or manifest_path.resolve().parent != session.workspace.resolve():
        raise BuildError("G7", "Production acceptance must belong to the authorized workspace.")
    before = _digest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selection = app._get_component_selection(config)
    selected = (
        selection.target_inputs + selection.target_components_to_build + selection.mixed_components
    )
    ffmpeg = [item for item in selected if item.name == "ffmpeg"]
    if len(ffmpeg) != 1:
        raise BuildError("G7", "FFmpeg was not selected exactly once.")
    for item in selected:
        if not app.state_manager.is_component_completed(item.name, item.version):
            raise BuildError("G7", f"{item.name} {item.version} is not completed.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    source_hash = _digest(archives / ffmpeg[0].get_archive_filename())
    binaries = {
        name: _digest(session.workspace / "bin" / name) for name in ("ffmpeg.exe", "ffprobe.exe")
    }
    prefix = environment.paths.target_prefix
    if (
        manifest.get("identity_key") != session.identity.build_key
        or manifest.get("source_sha256") != source_hash
        or manifest.get("binaries") != binaries
        or manifest.get("dependencies") != [item.name for item in selected if item.name != "ffmpeg"]
        or manifest.get("installed") != _installed_hashes(prefix)
        or manifest.get("interfaces") != _interface_hashes(prefix)
        or _digest(manifest_path) != before
    ):
        raise BuildError("G7", "Production FFmpeg outputs or material inputs changed.")
    return {
        "manifests": {manifest_path.name: before},
        "binaries": binaries,
        "source_sha256": source_hash,
    }, manifest


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_path(path: Path) -> str:
    result = subprocess.run(
        ["wslpath", "-w", str(path.resolve())],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def stage_g4(
    workspace: Path, environment: Any, manifest: dict[str, Any], stage: Path, interop: Any
) -> dict[str, Any]:
    """Stage only G4-accepted binaries and their transitive LLVM-MinGW DLL closure."""
    runtime = manifest["runtime"]
    approved = {name.lower() for name in runtime["toolchain_dlls"]}
    runtime_dir = environment.target_tools.prefix / "x86_64-w64-mingw32/bin"
    available: dict[str, Path] = {}
    for path in runtime_dir.glob("*.dll"):
        key = path.name.lower()
        if key in available:
            raise BuildError("G5", f"Ambiguous toolchain DLL: {key}")
        available[key] = path
    stage.mkdir(parents=True, exist_ok=False)
    roots: set[str] = set()
    binaries = {}
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        source = workspace / "bin" / name
        expected = manifest["binaries"][name]
        if _digest(source) != expected:
            raise BuildError("G5", f"{name} changed before staging.")
        destination = stage / name
        shutil.copyfile(source, destination)
        if _digest(destination) != expected:
            raise BuildError("G5", f"{name} changed during staging.")
        binaries[name] = expected
        roots.update(name.lower() for name in _imports(environment, destination))
    if roots != {name.lower() for name in runtime["imports"]}:
        raise BuildError("G5", "Executable imports differ from the accepted G4 manifest.")
    pending = list(roots)
    processed: set[str] = set()
    system: set[str] = set()
    copied: dict[str, str] = {}
    while pending:
        name = pending.pop().lower()
        if not re.fullmatch(r"[a-z0-9_+.-]+\.dll", name):
            raise BuildError("G5", f"Invalid DLL import name: {name!r}")
        if name in processed:
            continue
        processed.add(name)
        if name not in available:
            if name in approved:
                raise BuildError("G5", f"Accepted toolchain DLL missing: {name}")
            system.add(name)
            continue
        if name not in approved:
            raise BuildError("G5", f"Unexpected toolchain DLL in closure: {name}")
        source = available[name]
        digest = _digest(source)
        destination = stage / source.name
        shutil.copyfile(source, destination)
        if _digest(destination) != digest or _digest(source) != digest:
            raise BuildError("G5", f"Toolchain DLL changed during staging: {name}")
        copied[name] = digest
        pending.extend(_imports(environment, destination))
    if set(copied) != approved:
        raise BuildError("G5", "G4 accepted toolchain DLL closure changed.")
    files = sorted(name for name in system if not name.startswith(("api-ms-win-", "ext-ms-win-")))
    script = (
        "$names=@("
        + ",".join(_ps_quote(name) for name in files)
        + "); foreach ($n in $names) { if (!(Test-Path -LiteralPath "
        "(Join-Path $env:SystemRoot ('System32\\' + $n)) -PathType Leaf)) { "
        "Write-Output $n } }"
    )
    result = interop.run_powershell(script, timeout=30)
    if result.returncode or result.stdout.strip():
        raise BuildError(
            "G5", f"Windows system DLL closure unresolved: {result.stdout!r} {result.stderr!r}"
        )
    return {"binaries": binaries, "toolchain_dlls": copied, "system_imports": sorted(system)}


def verify_stage(stage: Path, environment: Any, evidence: dict[str, Any]) -> None:
    for name, expected in evidence["binaries"].items():
        if _digest(stage / name) != expected:
            raise BuildError("G5", f"Staged executable changed during runtime tests: {name}")
    runtime_dir = environment.target_tools.prefix / "x86_64-w64-mingw32/bin"
    source_files = {path.name.lower(): path for path in runtime_dir.glob("*.dll")}
    stage_files = {path.name.lower(): path for path in stage.glob("*.dll")}
    for name, expected in evidence["toolchain_dlls"].items():
        if (
            name not in source_files
            or name not in stage_files
            or _digest(source_files[name]) != expected
            or _digest(stage_files[name]) != expected
        ):
            raise BuildError("G5", f"Runtime DLL changed during runtime tests: {name}")


def run_windows_ffmpeg(
    stage: Path,
    label: str,
    arguments: list[str],
    interop: Any,
    *,
    executable: str = "ffmpeg.exe",
    work: Path | None = None,
    controlled_path: bool = False,
) -> dict[str, Any]:
    """Run a staged executable and retain its exact output and invocation."""
    if not re.fullmatch(r"[a-z0-9_-]+", label):
        raise ValueError("Invalid runtime log label")
    if executable not in ("ffmpeg.exe", "ffprobe.exe"):
        raise ValueError("Invalid runtime executable")
    work = work if work is not None else stage
    output = work / f"{label}.stdout"
    error = work / f"{label}.stderr"
    win_stage = _windows_path(stage)
    win_work = _windows_path(work) if work != stage else win_stage
    ps_args = _ps_quote(subprocess.list2cmdline(arguments))
    script = (
        "$p=Start-Process -FilePath "
        + _ps_quote(win_stage + "\\" + executable)
        + " -WorkingDirectory "
        + _ps_quote(win_work)
        + " -ArgumentList "
        + ps_args
        + " -RedirectStandardOutput "
        + _ps_quote(win_work + "\\" + output.name)
        + " -RedirectStandardError "
        + _ps_quote(win_work + "\\" + error.name)
        + " -NoNewWindow -Wait -PassThru; if (!$p) { exit 255 }; exit $p.ExitCode"
    )
    if controlled_path:
        script = (
            "$env:PATH=" + _ps_quote(win_stage) + "+';'+$env:SystemRoot+'\\System32'; " + script
        )
    result = interop.run_powershell(script, timeout=180)
    return {
        "arguments": arguments,
        "returncode": result.returncode,
        "stdout": output.read_text(encoding="utf-8", errors="replace") if output.exists() else "",
        "stderr": error.read_text(encoding="utf-8", errors="replace") if error.exists() else "",
        "transport_stderr": result.stderr.decode("utf-8", errors="replace"),
    }


def parse_vulkan_listing(log: str) -> list[tuple[str, str, int]]:
    return [
        (match.group(1), match.group(2).strip(), int(match.group(3), 16))
        for line in log.splitlines()
        if (match := re.search(r"\]\s+(\d+): (.+) \([^()]*\) \(0x([0-9a-fA-F]+)\)\s*$", line))
    ]


def parse_opencl_listing(log: str) -> list[tuple[str, str, str]]:
    return [
        (match.group(1), match.group(2).strip(), match.group(3).strip())
        for line in log.splitlines()
        if (match := re.search(r"\]\s+(\d+\.\d+): (.+) / (.+)\s*$", line))
    ]


def _normal_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _selector(
    probe_key: tuple[Any, ...], probe_keys: list[tuple[Any, ...]], candidates: list[tuple[Any, ...]]
) -> str:
    if probe_keys.count(probe_key) != 1:
        raise ValueError(f"Ambiguous physical probe device: {probe_key}")
    found = [candidate[0] for candidate in candidates if candidate[1:] == probe_key]
    if len(found) != 1:
        raise ValueError(f"Cannot uniquely map probe device to FFmpeg listing: {probe_key}")
    return str(found[0])


def graphics_runtime(
    target: Any,
    features: list[dict[str, str]],
    stage: Path,
    interop: Any,
    *,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Test each probe device independently; listing is used only for mapping."""
    discovery: dict[str, Any] = {}
    run = runner if runner is not None else run_windows_ffmpeg
    work = [
        ("vulkan", target.vulkan.state),
        ("opencl", target.opencl.state),
    ]
    for technology, state in work:
        relevant = [
            item
            for item in features
            if item["technology"] == technology
            and item["status"] == "UNVALIDATED"
            and item["feature"] in ("scale_vulkan", "libplacebo", "avgblur_opencl")
        ]
        if state != FeatureState.PRESENT or not relevant:
            continue
        try:
            spec = "vulkan=vk:0" if technology == "vulkan" else "opencl=list"
            listing = run(
                stage,
                f"{technology}-listing",
                [
                    "-hide_banner",
                    "-v",
                    "verbose",
                    "-init_hw_device",
                    spec,
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x96:rate=5",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                interop,
            )
            discovery[technology] = listing
            candidates: list[tuple[Any, ...]]
            devices: list[tuple[str, tuple[Any, ...]]]
            if technology == "vulkan":
                candidates = [
                    (i, _normal_name(name), device_id)
                    for i, name, device_id in parse_vulkan_listing(listing["stderr"])
                ]
                devices = [
                    (device.name, (_normal_name(device.name), int(device.device_id, 16)))
                    for device in target.vulkan.devices
                ]
            else:
                candidates = [
                    (i, _normal_name(platform), _normal_name(name))
                    for i, platform, name in parse_opencl_listing(listing["stderr"])
                ]
                devices = [
                    (device.name, (_normal_name(platform.name), _normal_name(device.name)))
                    for platform in target.opencl.platforms
                    for device in platform.devices
                ]
            probe_keys = [key for _, key in devices]
            if len(devices) != len(relevant) // (
                2
                if technology == "vulkan"
                and any(item["feature"] == "libplacebo" for item in relevant)
                else 1
            ):
                raise ValueError("Probe inventory cannot be associated with result entries")
        except (BuildError, OSError, ValueError, subprocess.SubprocessError) as exc:
            for item in relevant:
                item.update(status="FAIL", detail=f"Device discovery failed: {exc}")
            continue
        for index, (device_name, key) in enumerate(devices):
            device_features = [item for item in relevant if item.get("probe_index") == str(index)]
            try:
                selector = _selector(key, probe_keys, candidates)
            except ValueError as exc:
                for item in device_features:
                    item.update(status="FAIL", detail=str(exc))
                continue
            for item in device_features:
                feature = item["feature"]
                label = f"{technology}-{index}-{feature}"
                dimensions = (64, 48) if technology == "vulkan" else (128, 96)
                filter_name = {
                    "scale_vulkan": "scale_vulkan=w=64:h=48",
                    "libplacebo": "libplacebo=w=64:h=48",
                    "avgblur_opencl": "avgblur_opencl",
                }[feature]
                device_spec = (
                    f"vulkan=vk:{selector}" if technology == "vulkan" else f"opencl=ocl:{selector}"
                )
                device_alias = "vk" if technology == "vulkan" else "ocl"
                args = [
                    "-hide_banner",
                    "-v",
                    "verbose",
                    "-init_hw_device",
                    device_spec,
                    "-filter_hw_device",
                    device_alias,
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x96:rate=5",
                    "-frames:v",
                    "4",
                    "-vf",
                    f"format=yuv420p,hwupload,{filter_name},hwdownload,format=yuv420p",
                    "-f",
                    "framemd5",
                    "-",
                ]
                try:
                    result = run(stage, label, args, interop)
                    discovery[label] = result
                    if result["returncode"]:
                        raise ValueError(f"FFmpeg exited {result['returncode']}")
                    if technology == "vulkan":
                        selected = re.findall(
                            r"Device (\d+) selected: (.+) \([^()]*\) \(0x([0-9a-fA-F]+)\)",
                            result["stderr"],
                        )
                        if len(selected) != 1 or (
                            selected[0][0],
                            _normal_name(selected[0][1]),
                            int(selected[0][2], 16),
                        ) != (selector, *key):
                            raise ValueError(
                                "Vulkan runtime selected a different or unclear device"
                            )
                    else:
                        selected_opencl = [
                            (i, _normal_name(platform), _normal_name(name))
                            for i, platform, name in parse_opencl_listing(result["stderr"])
                        ]
                        if selected_opencl != [(selector, *key)]:
                            raise ValueError(
                                "OpenCL runtime selected a different or unclear device"
                            )
                    validate_framemd5(result["stdout"], dimensions=dimensions)
                    item.update(
                        status="PASS",
                        selector=selector,
                        detail=f"4 {dimensions[0]}x{dimensions[1]} checksummed frames",
                    )
                except (BuildError, OSError, ValueError, subprocess.SubprocessError) as exc:
                    item.update(status="FAIL", selector=selector, detail=str(exc))
    return discovery


def _nvidia_device(target: Any) -> Any:
    physical = [
        gpu
        for gpu in target.gpus
        if gpu.adapter_type == "hardware"
        and (gpu.vendor_id or "").lower().removeprefix("0x") == "10de"
    ]
    devices = target.nvidia.devices
    if target.nvidia.state != FeatureState.PRESENT or len(devices) != 1 or len(physical) != 1:
        raise ValueError("NVIDIA ordinal mapping requires exactly one probed NVIDIA GPU.")
    device = devices[0]
    if (
        not device.uuid
        or not device.pci_bus_id
        or not device.gpu_pnp_device_id
        or not physical[0].pnp_device_id
        or _normal_name(device.gpu_pnp_device_id) != _normal_name(physical[0].pnp_device_id)
        or not _normal_name(device.name)
    ):
        raise ValueError("NVIDIA GPU UUID, PCI bus, or physical adapter mapping is unclear.")
    return device


def profile_runtime(
    config: BuildConfig,
    target: Any,
    stage: Path,
    work: Path,
    interop: Any,
    *,
    controlled_path: bool = True,
) -> dict[str, Any]:
    """Run the production software contract and configured representative GPU filters.

    Unlike G5, this deliberately does not claim codec/device-matrix acceptance.
    The same commands run before packaging and after controlled relocation.
    """
    if not config.gpl_enabled:
        raise BuildError("G7", "The production full-GPL software contract requires GPL enabled.")
    work.mkdir(parents=True, exist_ok=False)
    tests: dict[str, Any] = {}
    features: list[dict[str, str]] = []

    def run(
        _stage: Path, label: str, arguments: list[str], _interop: Any, *, probe: bool = False
    ) -> dict[str, Any]:
        result = run_windows_ffmpeg(
            stage,
            label,
            arguments,
            interop,
            executable="ffprobe.exe" if probe else "ffmpeg.exe",
            work=work,
            controlled_path=controlled_path,
        )
        tests[label] = result
        return result

    def checked(label: str, arguments: list[str], *, probe: bool = False) -> dict[str, Any]:
        result = run(stage, label, arguments, interop, probe=probe)
        if result["returncode"]:
            raise BuildError("G7", f"{label} exited {result['returncode']}: {result['stderr']}")
        return result

    for executable, is_probe in (("ffmpeg", False), ("ffprobe", True)):
        result = checked(executable + "-version", ["-hide_banner", "-version"], probe=is_probe)
        if f"{executable} version {config.ffmpeg_version} " not in result["stdout"]:
            raise BuildError("G7", f"{executable} did not report version {config.ffmpeg_version}.")
    checked(
        "software-encode",
        [
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x96:rate=5",
            "-frames:v",
            "8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-y",
            "smoke.mp4",
        ],
    )
    probe = checked(
        "software-probe",
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            "smoke.mp4",
        ],
        probe=True,
    )
    if probe["stdout"].strip() != "h264":
        raise BuildError("G7", "Software encode/probe did not produce H.264.")
    decoded = checked(
        "software-decode",
        [
            "-hide_banner",
            "-v",
            "error",
            "-i",
            "smoke.mp4",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "framemd5",
            "-",
        ],
    )
    validate_framemd5(decoded["stdout"], frames=8, dimensions=(128, 96))
    features.append({"technology": "software", "feature": "libx264_roundtrip", "status": "PASS"})
    hardware = bool(getattr(config, "enable_hardware_acceleration", True))
    device_name = ""
    if hardware:
        required = {"scale_vulkan", "avgblur_opencl"}
        if config.enable_libplacebo_vulkan:
            required.add("libplacebo")
        features.extend(
            item for item in pending_features(target, {}) if item["feature"] in required
        )
        graphics_runtime(target, features, stage, interop, runner=run)
        if config.cuda_build_mode != "disabled":
            device_name = _nvidia_device(target).name
            cuda_filters = ["scale_cuda"]
            if config.enable_libvmaf_cuda:
                cuda_filters.append("libvmaf_cuda")
            for name in cuda_filters:
                args = [
                    "-hide_banner",
                    "-loglevel",
                    "verbose",
                    "-init_hw_device",
                    "cuda=g7cuda:0",
                    "-filter_hw_device",
                    "g7cuda",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=8:duration=1",
                ]
                if name == "scale_cuda":
                    args += [
                        "-frames:v",
                        "4",
                        "-an",
                        "-vf",
                        "format=nv12,hwupload_cuda,scale_cuda=160:90,hwdownload,format=nv12,format=yuv420p",
                        "-f",
                        "framemd5",
                        "-",
                    ]
                else:
                    args += [
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=size=320x180:rate=8:duration=1",
                        "-filter_complex",
                        "[0:v]format=yuv420p,hwupload_cuda[main];[1:v]format=yuv420p,hwupload_cuda[ref];[main][ref]libvmaf_cuda=shortest=1[out]",
                        "-map",
                        "[out]",
                        "-frames:v",
                        "8",
                        "-an",
                        "-f",
                        "null",
                        "-",
                    ]
                result = checked(name.replace("_", "-"), args)
                if name not in result["stderr"]:
                    raise BuildError("G7", f"CUDA runtime did not execute {name}.")
                if name == "scale_cuda":
                    validate_framemd5(result["stdout"], frames=4, dimensions=(160, 90))
                else:
                    score = re.search(
                        r"VMAF score:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", result["stderr"]
                    )
                    if score is None or not math.isfinite(float(score.group(1))):
                        raise BuildError("G7", "CUDA libvmaf did not report a finite score.")
                features.append(
                    {
                        "technology": "nvidia",
                        "feature": name,
                        "device": device_name,
                        "selector": "0",
                        "status": "PASS",
                    }
                )
        invalid = [item for item in features if item["status"] != "PASS"]
        if invalid or not required.issubset({item["feature"] for item in features}):
            raise BuildError("G7", f"Configured GPU runtime acceptance incomplete: {invalid}")
    return {
        "status": "PASS",
        "scope": "production-representative",
        "hardware_enabled": hardware,
        "controlled_path": [str(stage), "%SystemRoot%\\System32"] if controlled_path else None,
        "features": features,
        "hardware_device": device_name,
        "tests": tests,
    }


def nvidia_runtime(
    target: Any, features: list[dict[str, str]], stage: Path, interop: Any
) -> dict[str, Any]:
    """Run NVIDIA features only when the physical GPU has an unambiguous ordinal."""
    entries = [item for item in features if item["technology"] == "nvidia"]
    physical = [
        gpu
        for gpu in target.gpus
        if gpu.adapter_type == "hardware"
        and (gpu.vendor_id or "").lower().removeprefix("0x") == "10de"
    ]
    if target.nvidia.state != FeatureState.PRESENT:
        if physical and target.nvidia.state in (FeatureState.ABSENT, FeatureState.NOT_APPLICABLE):
            for item in entries:
                item.update(status="UNVALIDATED", detail="NVIDIA probe contradicts GPU inventory.")
            return {"status": "BLOCKED", "error": "NVIDIA probe contradicts GPU inventory."}
        return {"status": "NOT_RUN", "reason": f"NVIDIA probe: {target.nvidia.state.value}"}
    try:
        device = _nvidia_device(target)
    except (ValueError, AttributeError, TypeError) as exc:
        for item in entries:
            item.update(status="UNVALIDATED", detail=f"NVIDIA mapping blocked: {exc}")
        return {"status": "BLOCKED", "error": str(exc)}
    try:
        result = run_nvidia_runtime(stage / "ffmpeg.exe", stage, 0, device.name, interop=interop)
        if not isinstance(result, dict) or not isinstance(result.get("features"), dict):
            raise ValueError("NVIDIA runtime helper returned malformed feature results.")
        details = result["features"]
        actual = result.get("device")
        verified_device = (
            isinstance(actual, dict)
            and actual.get("index") == 0
            and _normal_name(str(actual.get("name", ""))) == _normal_name(device.name)
        )
        inventory = details.get("feature_inventory")
        if not isinstance(inventory, dict) or inventory.get("status") != "PASS":
            for item in entries:
                item.update(
                    status="FAIL", detail="NVIDIA runtime inventory failed or was not reported."
                )
            return {**result, "helper_status": result.get("status"), "status": "FAIL"}
        for item in entries:
            record = details.get(item["feature"])
            if not isinstance(record, dict):
                item.update(status="FAIL", detail="NVIDIA runtime did not report this feature.")
                continue
            status = record.get("status")
            if status not in ("PASS", "FAIL", "UNSUPPORTED"):
                status = "FAIL"
                detail = "NVIDIA runtime returned an invalid feature status."
            elif status in ("PASS", "UNSUPPORTED") and not verified_device:
                status = "FAIL"
                detail = "NVIDIA runtime did not confirm the selected physical device."
            else:
                detail = str(record.get("diagnostics", ""))
            item.update(status=status, selector="0", detail=detail)
        for name, record in details.items():
            if name in NVIDIA_FEATURES or name == "feature_inventory":
                continue
            status = record.get("status") if isinstance(record, dict) else None
            if status not in ("PASS", "FAIL", "UNSUPPORTED") or (
                status in ("PASS", "UNSUPPORTED") and not verified_device
            ):
                status = "FAIL"
            features.append(
                {
                    "technology": "nvidia",
                    "feature": str(name),
                    "device": device.name,
                    "selector": "0",
                    "status": status,
                    "detail": (
                        str(record.get("diagnostics", ""))
                        if isinstance(record, dict)
                        else "Malformed result"
                    ),
                }
            )
        if result.get("status") != "PASS" and not any(
            item["status"] == "FAIL" for item in features if item["technology"] == "nvidia"
        ):
            features.append(
                {
                    "technology": "nvidia",
                    "feature": "runtime_helper",
                    "status": "FAIL",
                    "detail": "NVIDIA runtime helper did not report a complete pass.",
                }
            )
        statuses = [item["status"] for item in features if item["technology"] == "nvidia"]
        status = (
            "FAIL" if "FAIL" in statuses else "UNSUPPORTED" if "UNSUPPORTED" in statuses else "PASS"
        )
        return {**result, "helper_status": result.get("status"), "status": status}
    except Exception as exc:
        for item in entries:
            item.update(status="FAIL", detail=f"NVIDIA mapping/runtime failed: {exc}")
        return {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}


def video_runtime(
    target: Any, features: list[dict[str, str]], stage: Path, interop: Any
) -> dict[str, Any]:
    """Run video paths only after mapping the helper's ordinal to one physical GPU."""
    entries = [
        item
        for item in features
        if item["feature"] in VIDEO_FEATURES
        and VIDEO_FEATURES[item["feature"]] == "vulkan"
        and item["status"] == "UNVALIDATED"
    ]
    if not entries:
        return {"status": "NOT_RUN", "reason": "No applicable Vulkan Video devices."}
    physical = [gpu for gpu in target.gpus if gpu.adapter_type == "hardware"]
    if len(physical) != 1:
        for item in entries:
            item.update(status="UNVALIDATED", detail="Video requires exactly one physical GPU.")
        return {"status": "BLOCKED", "error": "Video requires exactly one physical GPU."}
    gpu = physical[0]
    try:
        name = _normal_name(gpu.name)
        vendor = int(gpu.vendor_id, 16)
        device_id = int(gpu.device_id, 16)
        if not name or not gpu.pnp_device_id:
            raise ValueError("Physical GPU name or PNP identity is missing.")
    except (AttributeError, TypeError, ValueError) as exc:
        for item in entries:
            item.update(status="UNVALIDATED", detail=f"GPU identity is unclear: {exc}")
        return {"status": "BLOCKED", "error": str(exc)}
    if any(item.get("pnp_device_id") != gpu.pnp_device_id for item in entries):
        for item in entries:
            item.update(
                status="UNVALIDATED", detail="Video feature inventory does not match the GPU."
            )
        return {"status": "BLOCKED", "error": "Video feature inventory does not match the GPU."}

    commands: dict[str, Any] = {}
    mapping: dict[str, str] = {}
    try:
        vulkan_matches = (
            [
                d
                for d in target.vulkan.devices
                if _normal_name(d.name) == name
                and int(d.vendor_id, 16) == vendor
                and int(d.device_id, 16) == device_id
            ]
            if target.vulkan.state == FeatureState.PRESENT
            else []
        )
    except (AttributeError, TypeError, ValueError):
        vulkan_matches = []

    def selected(api: str, log: str) -> bool:
        if api != "vulkan":
            return False
        listing = parse_vulkan_listing(log)
        matches = [
            (index, _normal_name(device_name), did)
            for index, device_name, did in listing
            if _normal_name(device_name) == name and did == device_id
        ]
        chosen = re.findall(r"Device (\d+) selected: (.+) \([^()]*\) \(0x([0-9a-fA-F]+)\)", log)
        return (
            len(vulkan_matches) == 1
            and matches == [("0", name, device_id)]
            and len(chosen) == 1
            and (chosen[0][0], _normal_name(chosen[0][1]), int(chosen[0][2], 16))
            == ("0", name, device_id)
        )

    for api in ("vulkan",):
        label = f"video-map-{api}"
        try:
            command = [
                "-hide_banner",
                "-loglevel",
                "verbose",
                "-init_hw_device",
                f"{api}=dev:0",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=128x96:rate=5",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ]
            result = run_windows_ffmpeg(stage, label, command, interop)
            commands[label] = result
            if not selected(api, result["stderr"]):
                raise ValueError("Ordinal 0 could not be matched to the physical GPU.")
        except (BuildError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            mapping[api] = str(exc)

    blocked_commands: set[str] = set()

    def run(label: str, args: list[str]) -> dict[str, Any]:
        api = None
        if "-init_hw_device" in args:
            spec = args[args.index("-init_hw_device") + 1]
            api = spec.split("=", 1)[0]
            if api != "vulkan" or spec != "vulkan=dev:0":
                blocked_commands.add(label)
                return {"returncode": 1, "stdout": "", "stderr": "Unexpected device selector."}
            if api in mapping:
                blocked_commands.add(label)
                return {"returncode": 1, "stdout": "", "stderr": mapping[api]}
        try:
            result = run_windows_ffmpeg(stage, label, args, interop)
        except (BuildError, OSError, ValueError, subprocess.SubprocessError) as exc:
            result = {"arguments": args, "returncode": 1, "stdout": "", "stderr": str(exc)}
        commands[label] = result
        if api and not selected(api, result["stderr"]):
            blocked_commands.add(label)
            return {
                **result,
                "returncode": 1,
                "stderr": result["stderr"] + "\nG5: selected GPU could not be verified.",
            }
        return result

    try:
        records = run_video_runtime(run, gpu.name, validate_framemd5)
        if not isinstance(records, list):
            raise ValueError("Video helper returned malformed results.")
        by_name: dict[str, dict[str, Any]] = {}
        for record in records:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("feature"), str)
                or VIDEO_FEATURES.get(record["feature"]) != "vulkan"
            ):
                raise ValueError(f"Video helper returned an unexpected feature: {record!r}")
            feature = record["feature"]
            if feature in by_name:
                raise ValueError(f"Video helper returned duplicate feature {feature}.")
            by_name[feature] = record
        for item in entries:
            feature = item["feature"]
            api = VIDEO_FEATURES[feature]
            feature_record = by_name.get(feature)
            label = feature.replace("_", "-")
            if api in mapping or label in blocked_commands:
                item.update(
                    status="UNVALIDATED",
                    detail=f"Physical GPU mapping not verified: {mapping.get(api, label)}",
                )
            elif feature_record is None:
                item.update(
                    status="UNVALIDATED", detail="Video helper did not execute this feature."
                )
            elif (
                feature_record.get("technology") != api
                or _normal_name(str(feature_record.get("device", ""))) != name
                or feature_record.get("status") not in ("PASS", "FAIL", "UNSUPPORTED")
                or (
                    feature_record.get("status") in ("PASS", "UNSUPPORTED")
                    and (
                        label not in commands
                        or (
                            feature_record.get("status") == "PASS"
                            and feature.endswith("_encode")
                            and label + "-verify" not in commands
                        )
                    )
                )
            ):
                item.update(status="FAIL", detail="Video helper returned inconsistent evidence.")
            else:
                item.update(
                    status=feature_record["status"], detail=str(feature_record.get("detail", ""))
                )
        return {"mapping_errors": mapping, "commands": commands, "results": records}
    except Exception as exc:
        for item in entries:
            if item["technology"] not in mapping:
                item.update(status="FAIL", detail=f"Video runtime helper failed: {exc}")
            else:
                item.update(
                    status="UNVALIDATED", detail=f"Physical GPU mapping not verified: {exc}"
                )
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "mapping_errors": mapping,
            "commands": commands,
        }


def _write_evidence(workspace: Path, evidence: dict[str, Any]) -> Path:
    directory = workspace / "g5-runs"
    directory.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    path = directory / f"{run_id}.json"
    temporary = directory / f".{run_id}.tmp"
    created = False
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            created = True
            json.dump(evidence, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return path


def _contract_digest() -> str:
    """Bind reusable results to the acceptance and verification implementation."""
    digest = hashlib.sha256()
    for name in (
        "g5.py",
        "g5_nvidia.py",
        "g5_video.py",
        "g4_final.py",
        "g4_wave6.py",
        "build_identity.py",
        "cross_environment.py",
        "target_inputs.py",
        "windows_probe.py",
        "wsl_mingw_preparation.py",
    ):
        digest.update(name.encode("ascii"))
        digest.update((PROJECT_ROOT / name).read_bytes())
    return digest.hexdigest()


def _feature_key(item: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item.get(key, ""))
        for key in ("technology", "feature", "device", "pnp_device_id", "probe_index")
    )


def _reusable_evidence(
    workspace: Path, current: dict[str, Any]
) -> tuple[Path, dict[str, Any]] | None:
    """Reuse only the latest matching, complete and integrity-checked contract."""
    candidates = []
    for path in (workspace / "g5-runs").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            identity = data["identity"]
            if (
                data["contract"] != current["contract"]
                or any(
                    identity.get(key) != current["identity"].get(key)
                    for key in ("schema_version", "backend", "build", "runtime", "fingerprints")
                )
                or data["g4"] != current["g4"]
            ):
                continue
            candidates.append((data["started_at"], path, data))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    if not candidates:
        return None
    _, path, data = max(candidates, key=lambda item: item[0])
    try:
        if (
            data["status"] != "PASS"
            or data["g4_after"] != current["g4"]
            or not data["runtime_identity_rechecked"]
            or not data["environment_rechecked"]
            or not data["stage"]["rechecked"]
            or any(
                data["stage"][key] != current["stage"][key]
                for key in ("binaries", "toolchain_dlls", "system_imports")
            )
            or not all(isinstance(data[key], dict) for key in ("graphics", "nvidia", "video"))
        ):
            return None
        expected = {_feature_key(item): item for item in current["features"]}
        actual = {_feature_key(item): item for item in data["features"]}
        if (
            len(expected) != len(current["features"])
            or len(actual) != len(data["features"])
            or expected.keys() != actual.keys()
        ):
            return None
        for key, item in expected.items():
            status = actual[key]["status"]
            if item["status"] == "UNVALIDATED":
                if status not in ("PASS", "UNSUPPORTED"):
                    return None
            elif status != item["status"]:
                return None
        return path, data
    except (KeyError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class G5Paths:
    target_prefix: Path


@dataclass(frozen=True)
class G5Environment:
    paths: G5Paths
    target_tools: TargetTools


def resolve_g5_environment(app: Any, config: BuildConfig, session: Any) -> G5Environment:
    """Resolve and verify the G5 inputs without generating or restoring G3 files."""
    preparation = app.wsl_mingw_preparation
    identity = session.identity
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or session.backend != TARGET_BACKEND_CROSS_MINGW
        or identity.backend != TARGET_BACKEND_CROSS_MINGW
        or not session.uses_isolated_workspace
        or session.workspace != app.workspace
        or preparation is None
    ):
        raise BuildError("G5", "The G1/G2-authorized Windows target session is unavailable.")
    summary = preparation.identity_summary
    if not isinstance(summary, Mapping) or (
        summary.get("schema_version") != identity.schema_version
        or summary.get("backend") != identity.backend
        or summary.get("digest") != identity.build_fingerprint
        or summary.get("build_key") != identity.build_key
    ):
        raise BuildError("G5", "G2 preparation does not match the authorized session identity.")
    if (
        preparation.target.uses_override
        or runtime_identity(preparation.target) != identity.runtime
        or runtime_identity(app.windows_target_state) != identity.runtime
    ):
        raise BuildError("G5", "G2 preparation does not match the physical runtime identity.")

    workspace = session.workspace
    prefix = workspace / "prefix"
    manifest_path = prefix / "target-inputs.json"
    if (
        workspace.is_symlink()
        or prefix.is_symlink()
        or manifest_path.is_symlink()
        or not prefix.is_dir()
        or not manifest_path.is_file()
        or prefix.resolve().parent != workspace.resolve()
    ):
        raise BuildError("G5", "The authorized target-input prefix is missing or redirected.")
    manifest_digest = _digest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_target_inputs_manifest(manifest, prefix, allow_component_outputs=True)
    if manifest.get("materialization_id") != summary.get("target_input_materialization_id"):
        raise BuildError("G5", "Target-input materialization differs from G2 preparation.")
    material = project_material_build_inputs(
        config,
        app._get_buildable_components(config),
        preparation.project_toolchains,
        preparation.cuda,
        preparation.vulkan,
        manifest,
    )
    if material.to_dict() != identity.build.get("material_inputs"):
        raise BuildError(
            "G5", "Target inputs or toolchain provenance differ from the build identity."
        )
    tools = _resolve_target_tools(preparation)
    if _digest(manifest_path) != manifest_digest:
        raise BuildError("G5", "Target-input manifest changed during read-only resolution.")
    return G5Environment(paths=G5Paths(target_prefix=prefix), target_tools=tools)


def run_g5(app: Any, config: BuildConfig) -> int:
    """Accept or safely reuse the consumer-relevant hardware matrix."""
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW:
        print("BLOCKED: G5 requires linux-cross-mingw.", flush=True)
        return 2
    try:
        session = app.prepare_target(config, resume=True)
    except (BuildError, OSError, ValueError) as exc:
        print(f"BLOCKED: G5 preparation failed: {exc}", flush=True)
        return 2
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "identity": session.identity.to_dict(),
        "contract": _contract_digest(),
        "status": "BLOCKED",
        "reuse": {
            "status": "NOT_USED",
            "reason": "No matching complete acceptance evidence.",
        },
        "features": _out_of_scope_features()
        + _deferred_features()
        + [
            {
                "technology": "coverage",
                "feature": "all_runtime_features",
                "status": "UNVALIDATED",
            }
        ],
    }
    try:
        if app.windows_target_state is None or app.windows_target_state.uses_override:
            raise BuildError("G5", "A fresh physical Windows probe without overrides is required.")
        environment = resolve_g5_environment(app, config, session)
        evidence["g4"] = verify_g4(app, config, session, environment)
        manifest_path = session.workspace / "g4-final-acceptance.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if _digest(manifest_path) != evidence["g4"]["manifests"]["g4-final-acceptance.json"]:
            raise BuildError("G5", "G4 final manifest changed after verification.")
        evidence["features"] = pending_features(app.windows_target_state, manifest)
        stage = session.workspace / "g5-runs" / ("stage-" + uuid.uuid4().hex)
        interop = WindowsInterop()
        evidence["stage"] = {
            "path": str(stage),
            **stage_g4(session.workspace, environment, manifest, stage, interop),
        }
        try:
            reusable = _reusable_evidence(session.workspace, evidence)
            if reusable:
                path, previous = reusable
                evidence["reuse"] = {"status": "REUSED", "evidence": str(path)}
                for key in ("features", "graphics", "nvidia", "video"):
                    evidence[key] = previous[key]
            else:
                evidence["graphics"] = graphics_runtime(
                    app.windows_target_state, evidence["features"], stage, interop
                )
                evidence["nvidia"] = nvidia_runtime(
                    app.windows_target_state, evidence["features"], stage, interop
                )
                evidence["video"] = video_runtime(
                    app.windows_target_state, evidence["features"], stage, interop
                )
        finally:
            after = verify_g4(app, config, session, environment)
            evidence["g4_after"] = after
            if after != evidence["g4"]:
                raise BuildError("G5", "G4 artifacts or manifests changed during runtime tests.")
            verify_stage(stage, environment, evidence["stage"])
            evidence["stage"]["rechecked"] = True
            if resolve_g5_environment(app, config, session) != environment:
                raise BuildError("G5", "Resolved target tools changed during runtime tests.")
            evidence["environment_rechecked"] = True
        observed = app._probe_windows_target(config)
        if observed.uses_override or runtime_identity(observed) != session.identity.runtime:
            raise BuildError("G5", "Windows runtime identity changed during acceptance.")
        evidence["runtime_identity_rechecked"] = True
        for item in evidence["features"]:
            if item["technology"] in OUT_OF_SCOPE_TECHNOLOGIES:
                item.update(status="OUT_OF_SCOPE", detail=OUT_OF_SCOPE_REASON)
        failures = [
            f"{item['feature']} ({item.get('device', item['technology'])})"
            for item in evidence["features"]
            if item["status"] == "FAIL"
        ]
        blockers = [
            f"{item['technology']}/{item['feature']}: {item.get('detail', 'Not validated.')}"
            for item in evidence["features"]
            if item["status"] in ("BLOCKED", "UNVALIDATED")
        ]
        evidence["error"] = (
            "Runtime failures: " + ", ".join(failures) + ". " if failures else ""
        ) + ("Coverage blocked: " + "; ".join(blockers) if blockers else "")
        if failures:
            evidence["status"] = "FAIL"
        elif not blockers:
            evidence["status"] = "PASS"
    except (BuildError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        for item in evidence["features"]:
            if item["status"] == "PASS":
                item.update(status="UNVALIDATED", detail="Run integrity or identity check failed")
        evidence["error"] = str(exc)
    try:
        path = _write_evidence(session.workspace, evidence)
    except OSError as exc:
        print(f"FAIL: G5 evidence could not be persisted: {exc}", flush=True)
        return 1
    for item in evidence["features"]:
        print(
            f"{item['status']}: {item['technology']}/{item['feature']} "
            f"{item.get('device', '')} {item.get('detail', '')}".strip(),
            flush=True,
        )
    print(f"{evidence['status']}: {evidence['error']} Evidence: {path}", flush=True)
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[evidence["status"]]
