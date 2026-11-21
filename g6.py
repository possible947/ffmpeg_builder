"""Windows runtime packaging and relocation acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .build_types import BuildError
from .builder import FFmpegBuilder
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g5 import (
    _contract_digest,
    _digest,
    _reusable_evidence,
    _windows_path,
    parse_opencl_listing,
    pending_features,
    resolve_g5_environment,
    stage_g4,
    validate_framemd5,
    verify_g4,
)
from .windows_probe import WindowsInterop


def _file_hashes(directory: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        result[path.relative_to(directory).as_posix()] = _digest(path)
    return result


def _external_runtime_prerequisites(
    app: Any, g4_manifest: dict[str, Any], g5_evidence: dict[str, Any]
) -> list[dict[str, Any]]:
    dependencies = set(g4_manifest["dependencies"])
    accepted_toolchain = {name.lower() for name in g5_evidence["stage"]["toolchain_dlls"]}
    requirements: dict[str, dict[str, Any]] = {}

    def add(name: str, classification: str, source_identity: str, consumer: str) -> None:
        key = name.lower()
        if key in accepted_toolchain:
            return
        item = requirements.setdefault(
            key,
            {
                "name": name,
                "classification": classification,
                "source_identity": source_identity,
                "consumers": [],
            },
        )
        if consumer not in item["consumers"]:
            item["consumers"].append(consumer)

    for component_name in sorted(dependencies):
        component = app.registry.get_by_name(component_name)
        if component is None:
            raise BuildError("G6", f"Accepted dependency metadata is missing: {component_name}")
        metadata = component.target_link_metadata
        if metadata is None:
            continue
        for runtime in metadata.runtime_dependencies:
            if runtime.classification == "external-runtime-prerequisite":
                add(
                    runtime.name,
                    runtime.classification,
                    runtime.source_identity,
                    component_name,
                )
        interface = component.target_import_interface
        if interface is not None:
            add(
                interface.dll,
                "external-runtime-prerequisite",
                f"Windows runtime interface for {component_name}",
                component_name,
            )

    feature_status = {item["feature"]: item["status"] for item in g5_evidence["features"]}
    if feature_status.get("h264_nvenc") == "PASS" or feature_status.get("hevc_nvenc") == "PASS":
        add(
            "nvEncodeAPI64.dll",
            "external-runtime-prerequisite",
            "NVIDIA Windows driver",
            "NVENC",
        )
    if (
        feature_status.get("h264_cuvid_decode") == "PASS"
        or feature_status.get("hevc_cuvid_decode") == "PASS"
    ):
        add(
            "nvcuvid.dll",
            "external-runtime-prerequisite",
            "NVIDIA Windows driver",
            "CUVID/NVDEC",
        )

    return [requirements[name] for name in sorted(requirements)]


def _verified_g5(
    app: Any, config: BuildConfig, session: Any, environment: Any
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Require matching accepted G5 evidence without rerunning its full matrix."""
    g4 = verify_g4(app, config, session, environment)
    manifest_path = session.workspace / "g4-final-acceptance.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    features = pending_features(app.windows_target_state, manifest)
    run_root = session.workspace / "g6-runs"
    run_root.mkdir(parents=True, exist_ok=True)
    interop = WindowsInterop()
    with tempfile.TemporaryDirectory(prefix="g6-g5-check-", dir=run_root) as temporary:
        stage = Path(temporary) / "stage"
        stage_details = stage_g4(session.workspace, environment, manifest, stage, interop)
        current = {
            "identity": session.identity.to_dict(),
            "contract": _contract_digest(),
            "g4": g4,
            "features": features,
            "stage": stage_details,
        }
        reusable = _reusable_evidence(session.workspace, current)
    if reusable is None or reusable[1].get("status") != "PASS":
        raise BuildError("G6", "No complete, matching, integrity-checked G5 PASS evidence exists.")
    if _digest(manifest_path) != g4["manifests"]["g4-final-acceptance.json"]:
        raise BuildError("G6", "G4 acceptance evidence changed during G5 verification.")
    return reusable[0], reusable[1], manifest


def _runtime_policy(
    app: Any,
    g4_manifest: dict[str, Any],
    g5_evidence: dict[str, Any],
) -> dict[str, object]:
    return {
        "pe_imports": g4_manifest["runtime"]["imports"],
        "toolchain_dlls": g5_evidence["stage"]["toolchain_dlls"],
        "system_imports": g5_evidence["stage"]["system_imports"],
        "external_runtime_prerequisites": _external_runtime_prerequisites(
            app, g4_manifest, g5_evidence
        ),
    }


def _powershell_array(values: list[str]) -> str:
    return "@(" + ",".join("'" + value.replace("'", "''") + "'" for value in values) + ")"


def _run_relocated_smoke(bundle: Path, work: Path, expected_device: str) -> dict[str, Any]:
    work.mkdir(parents=True, exist_ok=False)
    bundle_windows = _windows_path(bundle)
    work_windows = _windows_path(work)
    ffmpeg = bundle_windows + r"\ffmpeg.exe"
    ffprobe = bundle_windows + r"\ffprobe.exe"
    ps_quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = f"""
$ErrorActionPreference = 'Stop'
$bundle = {ps_quote(bundle_windows)}
$work = {ps_quote(work_windows)}
$env:PATH = $bundle + ';' + $env:SystemRoot + '\\System32'
Set-Location -LiteralPath $work
$ffmpeg = {ps_quote(ffmpeg)}
$ffprobe = {ps_quote(ffprobe)}
function Invoke-Native([string]$name, [string]$exe, [string[]]$arguments) {{
    $stdout = Join-Path $work ($name + '.stdout')
    $stderr = Join-Path $work ($name + '.stderr')
    $process = Start-Process -FilePath $exe -WorkingDirectory $work `
        -ArgumentList $arguments -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -NoNewWindow -Wait -PassThru
    if (!$process) {{ throw "Could not start $name" }}
    return @{{ returncode = $process.ExitCode; stdout = $stdout; stderr = $stderr }}
}}
$tests = [ordered]@{{}}
$tests.ffmpeg_version = Invoke-Native 'ffmpeg-version' $ffmpeg @('-hide_banner','-version')
$tests.ffprobe_version = Invoke-Native 'ffprobe-version' $ffprobe @('-hide_banner','-version')
$tests.software_encode = Invoke-Native 'software-encode' $ffmpeg @(
    '-hide_banner','-v','error','-f','lavfi','-i','testsrc2=size=128x96:rate=5',
    '-frames:v','8','-c:v','libx264','-preset','ultrafast','-y','smoke.mp4')
$tests.software_probe = Invoke-Native 'software-probe' $ffprobe @(
    '-v','error','-select_streams','v:0','-show_entries','stream=codec_name',
    '-of','csv=p=0','smoke.mp4')
$tests.software_decode = Invoke-Native 'software-decode' $ffmpeg @(
    '-hide_banner','-v','error','-i','smoke.mp4','-f','null','-')
$tests.cuda_scale = Invoke-Native 'cuda-scale' $ffmpeg @(
    '-hide_banner','-loglevel','verbose','-init_hw_device','cuda=g6cuda:0',
    '-filter_hw_device','g6cuda','-f','lavfi','-i',
    'testsrc2=size=640x360:rate=30:duration=0.133333','-frames:v','4','-an','-vf',
    'format=nv12,hwupload_cuda,scale_cuda=320:180,hwdownload,format=nv12,format=yuv420p',
    '-f','framemd5','-')
$tests.cuda_libvmaf = Invoke-Native 'cuda-libvmaf' $ffmpeg @(
    '-hide_banner','-loglevel','verbose','-init_hw_device','cuda=g6cuda:0',
    '-filter_hw_device','g6cuda','-f','lavfi','-i',
    'testsrc2=size=320x180:rate=8:duration=1','-f','lavfi','-i',
    'testsrc2=size=320x180:rate=8:duration=1','-filter_complex',
    '[0:v]format=yuv420p,hwupload_cuda[main];[1:v]format=yuv420p,hwupload_cuda[ref];[main][ref]libvmaf_cuda=shortest=1[out]',
    '-map','[out]','-frames:v','8','-an','-f','null','-')
$tests.vulkan_scale = Invoke-Native 'vulkan-scale' $ffmpeg @(
    '-hide_banner','-v','verbose','-init_hw_device','vulkan=vk:0',
    '-filter_hw_device','vk','-f','lavfi','-i','testsrc2=size=128x96:rate=5',
    '-frames:v','4','-vf','format=yuv420p,hwupload,scale_vulkan=w=64:h=48,hwdownload,format=yuv420p',
    '-f','framemd5','-')
$tests.opencl_filter = Invoke-Native 'opencl-filter' $ffmpeg @(
    '-hide_banner','-v','verbose','-init_hw_device','opencl=ocl:0.0',
    '-filter_hw_device','ocl','-f','lavfi','-i','testsrc2=size=128x96:rate=5',
    '-frames:v','4','-vf','format=yuv420p,hwupload,avgblur_opencl,hwdownload,format=yuv420p',
    '-f','framemd5','-')
Write-Output (ConvertTo-Json -InputObject $tests -Compress -Depth 4)
"""
    try:
        result = WindowsInterop().run_powershell(script, timeout=600)
    except Exception as exc:
        raise BuildError("G6", f"Relocated Windows runtime launch failed: {exc}") from exc
    if result.returncode:
        raise BuildError(
            "G6",
            "Relocated Windows smoke process failed: "
            f"{result.stdout.decode(errors='replace')} {result.stderr.decode(errors='replace')}",
        )
    try:
        tests = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise BuildError(
            "G6", f"Malformed relocated Windows smoke result: {result.stdout!r}"
        ) from exc

    def output(label: str, stream: str) -> str:
        path = work / f"{label}.{stream}"
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    for name, result_data in tests.items():
        if int(result_data["returncode"]) != 0:
            raise BuildError(
                "G6",
                f"Relocated {name} failed ({result_data['returncode']}): "
                f"{output(name.replace('_', '-'), 'stderr')}",
            )

    if "ffmpeg version 9.0 " not in output("ffmpeg-version", "stdout"):
        raise BuildError("G6", "Relocated ffmpeg.exe did not report version 9.0.")
    if "ffprobe version 9.0 " not in output("ffprobe-version", "stdout"):
        raise BuildError("G6", "Relocated ffprobe.exe did not report version 9.0.")
    if output("software-probe", "stdout").strip() != "h264":
        raise BuildError("G6", "Relocated software encode/probe did not produce H.264.")

    validate_framemd5(output("cuda-scale", "stdout"), frames=4, dimensions=(320, 180))
    cuda_log = output("cuda-scale", "stderr")
    if "scale_cuda" not in cuda_log:
        raise BuildError("G6", "Relocated CUDA scale did not execute the accepted CUDA filter.")
    vmaf_log = output("cuda-libvmaf", "stderr")
    score = re.search(r"VMAF score:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", vmaf_log)
    if "libvmaf_cuda" not in vmaf_log or score is None or not math.isfinite(float(score.group(1))):
        raise BuildError("G6", "Relocated CUDA libvmaf did not complete on the accepted device.")

    validate_framemd5(output("vulkan-scale", "stdout"), frames=4, dimensions=(64, 48))
    vulkan_log = output("vulkan-scale", "stderr")
    if (
        "Device 0 selected:" not in vulkan_log
        or expected_device.casefold() not in vulkan_log.casefold()
    ):
        raise BuildError("G6", "Relocated Vulkan scale did not select the accepted GPU.")
    validate_framemd5(output("opencl-filter", "stdout"), frames=4, dimensions=(128, 96))
    opencl_devices = parse_opencl_listing(output("opencl-filter", "stderr"))
    if not any(
        selector == "0.0" and expected_device.casefold() in device.casefold()
        for selector, _platform, device in opencl_devices
    ):
        raise BuildError("G6", "Relocated OpenCL filter did not select the accepted GPU.")
    return {
        "status": "PASS",
        "controlled_path": [str(bundle), "%SystemRoot%\\System32"],
        "software": ["ffmpeg -version", "ffprobe -version", "libx264 encode", "ffprobe", "decode"],
        "hardware": ["scale_cuda", "libvmaf_cuda", "scale_vulkan", "avgblur_opencl"],
        "hardware_device": expected_device,
        "cuda_ordinal": 0,
        "tests": tests,
    }


def run_g6(app: Any, config: BuildConfig) -> int:
    """Package verified G4/G5 outputs and accept their relocated Windows runtime."""
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW or config.ffmpeg_version != "9.0":
        print("BLOCKED: G6 requires the accepted FFmpeg 9.0 linux-cross-mingw target.", flush=True)
        return 2
    builder: FFmpegBuilder | None = None
    try:
        session = app.prepare_target(config, resume=True)
        cross = app.prepare_cross_environment(config, session)
        g5_environment = resolve_g5_environment(app, config, session)
        g4 = verify_g4(app, config, session, g5_environment)
        g5_path, g5_evidence, g4_manifest = _verified_g5(app, config, session, g5_environment)
        g5_digest = _digest(g5_path)
        if g5_evidence["g4"] != g4:
            raise BuildError("G6", "G5 acceptance does not match the current G4 outputs.")
        expected_device = next(
            (
                str(item["device"])
                for item in g5_evidence["features"]
                if item["technology"] == "nvidia"
                and item["feature"] == "scale_cuda"
                and item["status"] == "PASS"
            ),
            "",
        )
        required_features = {
            ("nvidia", "scale_cuda"),
            ("nvidia", "libvmaf_cuda"),
            ("vulkan", "scale_vulkan"),
            ("opencl", "avgblur_opencl"),
        }
        accepted = {(item["technology"], item["feature"]): item for item in g5_evidence["features"]}
        if not expected_device or any(
            accepted.get(feature, {}).get("status") != "PASS" for feature in required_features
        ):
            raise BuildError("G6", "G5 lacks a PASS for each planned relocation smoke feature.")

        selection = app._get_component_selection(config)
        selected = (
            selection.target_inputs
            + selection.target_components_to_build
            + selection.mixed_components
        )
        ffmpeg = [item for item in selected if item.name == "ffmpeg"]
        if len(ffmpeg) != 1:
            raise BuildError("G6", "FFmpeg was not selected exactly once.")
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, ffmpeg),
            cross_environment=cross,
        )
        policy = _runtime_policy(app, g4_manifest, g5_evidence)
        release_dir = builder.make_release_bundle(policy)
        first_files = _file_hashes(release_dir)
        first_hashes = {
            name: digest for name, digest in first_files.items() if name != "manifest.json"
        }
        first_manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))

        repeated_dir = builder.make_release_bundle(policy)
        repeated_files = _file_hashes(repeated_dir)
        repeated_hashes = {
            name: digest for name, digest in repeated_files.items() if name != "manifest.json"
        }
        repeated_manifest = json.loads((repeated_dir / "manifest.json").read_text(encoding="utf-8"))
        first_manifest.pop("generated_at", None)
        repeated_manifest.pop("generated_at", None)
        if first_hashes != repeated_hashes or first_manifest != repeated_manifest:
            raise BuildError("G6", "Repeated production packaging changed bundle contents.")

        run_id = uuid.uuid4().hex
        run_dir = session.workspace / "g6-runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        relocated = run_dir / "bundle"
        shutil.copytree(repeated_dir, relocated)
        if _file_hashes(relocated) != repeated_files:
            raise BuildError("G6", "Relocated bundle hashes differ from the production package.")
        smoke = _run_relocated_smoke(relocated, run_dir / "work", expected_device)
        if _file_hashes(relocated) != repeated_files:
            raise BuildError("G6", "Relocated runtime smoke changed package contents.")

        g4_after = verify_g4(app, config, session, g5_environment)
        if g4_after != g4 or _digest(g5_path) != g5_digest:
            raise BuildError("G6", "Accepted G4/G5 inputs changed during packaging acceptance.")
        report = {
            "schema_version": 1,
            "status": "PASS",
            "identity_key": session.identity.build_key,
            "g4": g4,
            "g5_evidence": str(g5_path),
            "g5_evidence_sha256": g5_digest,
            "package": str(release_dir),
            "package_file_sha256": repeated_hashes,
            "package_manifest_sha256": _digest(release_dir / "manifest.json"),
            "repeat_package_identical": True,
            "relocated_bundle": str(relocated),
            "runtime_smoke": smoke,
            "redistribution": {
                "scope": "internal-target-specific",
                "redistributable": False,
            },
        }
        report_path = session.workspace / "g6-acceptance.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"PASS: G6 Windows bundle accepted: {release_dir}", flush=True)
        print(f"Relocated runtime: {relocated}", flush=True)
        return 0
    except (BuildError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAIL: G6 Windows packaging acceptance: {exc}", flush=True)
        return 1
    finally:
        if builder is not None:
            builder.shutdown_downloads(wait=False)
