"""Standalone NVIDIA runtime acceptance for a staged Windows FFmpeg build.

The caller supplies the staged executable, a scratch directory, and an
explicit CUDA device ordinal plus its expected probed name. All generated
media and captured process output stay under that scratch directory. Failures
are reported as failures with diagnostics; this module does not infer that
hardware is unsupported from an unsuccessful operation.
"""

from __future__ import annotations

import math
import re
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .windows_probe import WindowsInterop

SCHEMA_VERSION = 1
DEFAULT_FRAME_COUNT = 30
DEFAULT_TIMEOUT_SECONDS = 180.0
_FEATURES = (
    "h264_nvenc",
    "hevc_nvenc",
    "h264_cuvid_decode",
    "hevc_cuvid_decode",
    "scale_cuda",
    "libvmaf_cuda",
    "av1_nvenc",
    "av1_cuvid_decode",
)


@dataclass(frozen=True)
class CommandResult:
    """Captured FFmpeg process result used by the default and test runners."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Path, list[str], Path, str, float], CommandResult]


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


class _WindowsFfmpegRunner:
    def __init__(self, interop: WindowsInterop):
        self._interop = interop

    def __call__(
        self, executable: Path, arguments: list[str], cwd: Path, label: str, timeout: float
    ) -> CommandResult:
        if not re.fullmatch(r"[a-z0-9_-]+", label):
            raise ValueError(f"Invalid NVIDIA runtime command label: {label!r}")
        stdout_path = cwd / f"{label}.stdout"
        stderr_path = cwd / f"{label}.stderr"
        quoted_arguments = [
            _ps_quote(subprocess.list2cmdline([str(argument)])) for argument in arguments
        ]
        script = (
            "$p=Start-Process -FilePath "
            + _ps_quote(_windows_path(executable))
            + " -WorkingDirectory "
            + _ps_quote(_windows_path(cwd))
            + " -ArgumentList @("
            + ",".join(quoted_arguments)
            + ") -RedirectStandardOutput "
            + _ps_quote(_windows_path(stdout_path))
            + " -RedirectStandardError "
            + _ps_quote(_windows_path(stderr_path))
            + " -NoNewWindow -Wait -PassThru; "
            "if (!$p) { exit 255 }; exit $p.ExitCode"
        )
        result = self._interop.run_powershell(script, timeout=timeout)
        return CommandResult(
            returncode=result.returncode,
            stdout=(
                stdout_path.read_text(encoding="utf-8", errors="replace")
                if stdout_path.is_file()
                else ""
            ),
            stderr=(
                stderr_path.read_text(encoding="utf-8", errors="replace")
                if stderr_path.is_file()
                else ""
            )
            + result.stderr.decode("utf-8", errors="replace"),
        )


def _records(text: str, *, count: int, width: int, height: int) -> list[dict[str, Any]]:
    expected_size = width * height * 3 // 2
    if not re.search(rf"(?m)^#dimensions\s+0:\s*{width}x{height}\s*$", text):
        raise ValueError(f"framemd5 output is missing dimensions {width}x{height}")
    rows: list[dict[str, Any]] = []
    previous_pts: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [part.strip() for part in line.split(",")]
        if (
            len(fields) != 6
            or not all(re.fullmatch(r"-?\d+", part) for part in fields[:5])
            or int(fields[0]) != 0
            or int(fields[3]) <= 0
            or int(fields[4]) != expected_size
            or not re.fullmatch(r"[0-9a-fA-F]{32}", fields[5])
        ):
            raise ValueError(f"Invalid framemd5 record: {line!r}")
        pts = int(fields[2])
        if previous_pts is not None and pts <= previous_pts:
            raise ValueError("framemd5 timestamps are not strictly increasing")
        previous_pts = pts
        rows.append({"pts": pts, "size": int(fields[4]), "md5": fields[5].lower()})
    if len(rows) != count:
        raise ValueError(f"Expected {count} decoded frames, got {len(rows)}")
    return rows


def _ivf_frame_count(path: Path, *, width: int, height: int) -> int:
    """Validate a generated AV1 IVF stream and count complete frame records."""
    data = path.read_bytes()
    if len(data) < 32 or data[:4] != b"DKIF" or data[8:12] != b"AV01":
        raise ValueError("Generated AV1 probe is not a valid IVF/AV1 stream")
    actual_width, actual_height = struct.unpack_from("<HH", data, 12)
    declared_count = struct.unpack_from("<I", data, 24)[0]
    if (actual_width, actual_height) != (width, height):
        raise ValueError(
            f"Generated AV1 probe dimensions are {actual_width}x{actual_height}, "
            f"expected {width}x{height}"
        )
    offset = 32
    actual_count = 0
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Generated AV1 IVF stream has a truncated frame header")
        frame_size = struct.unpack_from("<I", data, offset)[0]
        if frame_size <= 0 or offset + 12 + frame_size > len(data):
            raise ValueError("Generated AV1 IVF stream has a truncated or empty frame")
        actual_count += 1
        offset += 12 + frame_size
    if offset != len(data) or actual_count != declared_count:
        raise ValueError(
            f"Generated AV1 IVF frame count mismatch: header={declared_count}, "
            f"actual={actual_count}"
        )
    return actual_count


def _device_details(log: str, *, index: int, expected_name: str) -> dict[str, Any]:
    matches = re.findall(
        r"GPU\s*#\s*(\d+)\s*-\s*<\s*(.*?)\s*>\s*has Compute SM\s*([0-9.]+)",
        log,
        flags=re.IGNORECASE,
    )
    expected = " ".join(expected_name.casefold().split())
    for ordinal, name, capability in matches:
        normalized = " ".join(name.casefold().split())
        if int(ordinal) == index and normalized == expected:
            return {
                "index": index,
                "name": name,
                "compute_capability": capability,
            }
    if matches:
        observed = [
            {"index": int(ordinal), "name": name, "compute_capability": capability}
            for ordinal, name, capability in matches
        ]
        raise ValueError(
            f"NVENC runtime selected a different device than CUDA index {index} "
            f"({expected_name!r}); observed {observed!r}"
        )
    raise ValueError(
        "NVENC output did not report the selected NVIDIA device and compute capability"
    )


def _feature_result(status: str, *, diagnostics: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "diagnostics": diagnostics, **details}


def _logged_frame_count(log: str) -> int | None:
    match = re.search(r"Output stream #0:0 \(video\):\s*(\d+)\s+frames encoded\b", log)
    return int(match.group(1)) if match else None


def run_nvidia_runtime(
    ffmpeg_exe: str | Path,
    scratch_dir: str | Path,
    device_index: int,
    expected_device_name: str,
    *,
    frame_count: int = DEFAULT_FRAME_COUNT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner | None = None,
    interop: WindowsInterop | None = None,
) -> dict[str, Any]:
    """Run NVIDIA encode, decode, CUDA scale, and CUDA VMAF tests.

    Args:
        ffmpeg_exe: Existing staged, accepted ``ffmpeg.exe``. It is read only.
        scratch_dir: Existing caller-owned scratch directory. A unique run
            directory and every generated media/log file are created beneath it.
        device_index: Explicit CUDA/NVENC/CUVID ordinal; automatic device choice
            is never used.
        expected_device_name: Probed NVIDIA name that the NVENC runtime must
            report for ``device_index``.
        frame_count: Number of generated encode/decode/scale frames.
        timeout: Per-process timeout in seconds.
        runner: Optional process runner for integration/testing. It receives
            ``(ffmpeg_exe, arguments, cwd, label, timeout)`` and returns a
            :class:`CommandResult`.
        interop: Optional Windows interop transport used by the default runner.

    Returns a JSON-serializable mapping with overall ``status``, runtime and
    feature inventory, per-feature ``PASS``/``FAIL``/evidence-backed
    ``UNSUPPORTED`` results, commands, checksum records, and the run directory.
    Overall PASS means every matrix item was either exercised successfully or
    classified unsupported from explicit selected-device runtime evidence.
    """
    executable = Path(ffmpeg_exe).resolve()
    scratch = Path(scratch_dir).resolve()
    if executable.name.casefold() != "ffmpeg.exe" or not executable.is_file():
        raise ValueError(f"Expected an existing staged ffmpeg.exe, got {executable}")
    if not scratch.is_dir():
        raise ValueError(f"Caller-provided scratch directory does not exist: {scratch}")
    if isinstance(device_index, bool) or not isinstance(device_index, int) or device_index < 0:
        raise ValueError("device_index must be a non-negative integer")
    expected_device_name = " ".join(expected_device_name.split())
    if not expected_device_name:
        raise ValueError("expected_device_name must not be empty")
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count < 1:
        raise ValueError("frame_count must be a positive integer")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")

    run_dir = scratch / f"g5-nvidia-{uuid.uuid4().hex}"
    run_dir.mkdir()
    execute = runner or _WindowsFfmpegRunner(interop or WindowsInterop())
    commands: dict[str, dict[str, Any]] = {}

    def invoke(label: str, arguments: list[str]) -> CommandResult:
        try:
            result = execute(executable, arguments, run_dir, label, timeout)
            if not isinstance(result, CommandResult):
                raise TypeError("runner must return a CommandResult")
        except Exception as exc:
            result = CommandResult(returncode=-1, stderr=f"{type(exc).__name__}: {exc}")
        commands[label] = {
            "arguments": arguments,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        return result

    inventories: dict[str, str] = {}
    inventory_errors: list[str] = []
    for label, option in (
        ("version", "-version"),
        ("encoders", "-encoders"),
        ("decoders", "-decoders"),
        ("filters", "-filters"),
        ("hwaccels", "-hwaccels"),
    ):
        result = invoke(f"inventory-{label}", ["-hide_banner", option])
        output = result.stdout + result.stderr
        inventories[label] = output
        if result.returncode:
            inventory_errors.append(
                f"{option} exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
            )

    combined_inventory = "\n".join(inventories.values())
    availability = {
        "h264_nvenc": bool(re.search(r"\bh264_nvenc\b", inventories["encoders"])),
        "hevc_nvenc": bool(re.search(r"\bhevc_nvenc\b", inventories["encoders"])),
        "av1_nvenc": bool(re.search(r"\bav1_nvenc\b", inventories["encoders"])),
        "libaom_av1": bool(re.search(r"\blibaom-av1\b", inventories["encoders"])),
        "h264_cuvid": bool(re.search(r"\bh264_cuvid\b", inventories["decoders"])),
        "hevc_cuvid": bool(re.search(r"\bhevc_cuvid\b", inventories["decoders"])),
        "av1_cuvid": bool(re.search(r"\bav1_cuvid\b", inventories["decoders"])),
        "hwupload_cuda": bool(re.search(r"\bhwupload_cuda\b", inventories["filters"])),
        "scale_cuda": bool(re.search(r"\bscale_cuda\b", inventories["filters"])),
        "libvmaf_cuda": bool(re.search(r"\blibvmaf_cuda\b", inventories["filters"])),
        "cuda": bool(re.search(r"(?im)^\s*cuda\s*$", inventories["hwaccels"])),
    }
    if inventory_errors:
        for key in availability:
            availability[key] = False
    feature_results: dict[str, dict[str, Any]] = {}
    device: dict[str, Any] = {
        "index": device_index,
        "expected_name": expected_device_name,
    }
    inventory_status = "PASS" if not inventory_errors else "FAIL"
    feature_results["feature_inventory"] = _feature_result(
        inventory_status,
        diagnostics=(
            "; ".join(inventory_errors)
            if inventory_errors
            else "FFmpeg feature inventory collected."
        ),
        available=availability,
    )

    outputs = {
        "h264": run_dir / "nvidia-h264.mkv",
        "hevc": run_dir / "nvidia-hevc.mkv",
    }

    def failed_feature(name: str, message: str, **details: Any) -> None:
        feature_results[name] = _feature_result("FAIL", diagnostics=message, **details)

    def preflight(name: str, required: tuple[str, ...]) -> bool:
        missing = [feature for feature in required if not availability.get(feature, False)]
        if inventory_errors:
            failed_feature(name, "Feature inventory failed; runtime support is unknown.")
            return False
        if missing:
            failed_feature(
                name, f"Required FFmpeg feature(s) missing from inventory: {', '.join(missing)}"
            )
            return False
        if device_identity_conflict:
            failed_feature(
                name,
                "The selected NVIDIA ordinal did not consistently match the expected physical device.",
            )
            return False
        return True

    def selected_device_preflight(name: str, required: tuple[str, ...]) -> bool:
        if not preflight(name, required):
            return False
        if not device_identity_verified:
            failed_feature(
                name,
                "The requested NVIDIA ordinal could not be matched to the expected physical "
                "device by FFmpeg runtime output.",
            )
            return False
        return True

    device_identity_verified = False
    device_identity_conflict = False
    for codec, encoder in (("h264", "h264_nvenc"), ("hevc", "hevc_nvenc")):
        name = f"{codec}_nvenc"
        if not preflight(name, (encoder, "cuda")):
            continue
        args = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=30:duration={frame_count / 30:g}",
            "-frames:v",
            str(frame_count),
            "-an",
            "-c:v",
            encoder,
            "-gpu",
            str(device_index),
            "-preset",
            "p4",
            "-f",
            "matroska",
            "-y",
            _windows_path(outputs[codec]),
        ]
        process = invoke(name, args)
        log = process.stdout + process.stderr
        try:
            details = _device_details(log, index=device_index, expected_name=expected_device_name)
            device_identity_verified = True
            device.update(details)
        except ValueError as exc:
            if "different device" in str(exc):
                device_identity_conflict = True
            failed_feature(name, str(exc), returncode=process.returncode, command=commands[name])
            continue
        try:
            if process.returncode:
                raise ValueError(f"FFmpeg exited {process.returncode}: {process.stderr.strip()}")
            if not outputs[codec].is_file() or outputs[codec].stat().st_size <= 0:
                raise ValueError("NVENC returned success but produced no media bytes")
            encoded = re.search(r"\b(\d+)\s+frames encoded\b", log)
            if encoded is None or int(encoded.group(1)) != frame_count:
                raise ValueError(
                    f"Expected {frame_count} encoded frames; runtime log did not confirm that count"
                )
            feature_results[name] = _feature_result(
                "PASS",
                diagnostics=f"Encoded {frame_count} generated 640x360 frames.",
                frames=frame_count,
                bytes=outputs[codec].stat().st_size,
                device=details,
            )
        except (OSError, ValueError) as exc:
            failed_feature(name, str(exc), returncode=process.returncode, command=commands[name])

    av1_encoder = "av1_nvenc"
    if preflight("av1_nvenc", (av1_encoder, "cuda")):
        av1_args = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x96:rate=5:duration=0.8",
            "-frames:v",
            "4",
            "-an",
            "-c:v",
            av1_encoder,
            "-gpu",
            str(device_index),
            "-f",
            "null",
            "-",
        ]
        process = invoke("av1_nvenc", av1_args)
        log = process.stdout + process.stderr
        try:
            details = _device_details(log, index=device_index, expected_name=expected_device_name)
            device_identity_verified = True
            device.update(details)
        except ValueError as exc:
            if "different device" in str(exc):
                device_identity_conflict = True
            failed_feature(
                "av1_nvenc",
                str(exc),
                returncode=process.returncode,
                command=commands["av1_nvenc"],
            )
        else:
            if process.returncode == 0:
                encoded = re.search(r"\b4\s+frames encoded\b", log)
                if encoded is None:
                    failed_feature(
                        "av1_nvenc",
                        "FFmpeg exited successfully but did not confirm four encoded frames.",
                        returncode=0,
                        command=commands["av1_nvenc"],
                        device=details,
                    )
                else:
                    feature_results["av1_nvenc"] = _feature_result(
                        "PASS",
                        diagnostics="AV1 NVENC encoded four generated frames on the selected device.",
                        frames=4,
                        device=details,
                    )
            elif "codec not supported" in log.casefold():
                feature_results["av1_nvenc"] = _feature_result(
                    "UNSUPPORTED",
                    diagnostics=(
                        "The verified selected NVIDIA device explicitly rejected AV1 NVENC: "
                        "Codec not supported."
                    ),
                    evidence={
                        "device_verified": details,
                        "runtime_message": "Codec not supported",
                    },
                    returncode=process.returncode,
                )
            else:
                failed_feature(
                    "av1_nvenc",
                    f"AV1 NVENC failed without explicit device capability evidence "
                    f"(exit {process.returncode}): {process.stderr.strip()}",
                    returncode=process.returncode,
                    command=commands["av1_nvenc"],
                    device=details,
                )

    for codec, decoder in (("h264", "h264_cuvid"), ("hevc", "hevc_cuvid")):
        name = f"{decoder}_decode"
        if not selected_device_preflight(name, (decoder, "cuda")):
            continue
        source = outputs[codec]
        if not source.is_file() or source.stat().st_size <= 0:
            failed_feature(name, f"Independent decode input was not produced: {source.name}")
            continue
        args = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-c:v",
            decoder,
            "-gpu",
            str(device_index),
            "-i",
            _windows_path(source),
            "-frames:v",
            str(frame_count),
            "-an",
            "-f",
            "framemd5",
            "-",
        ]
        process = invoke(name, args)
        log = process.stdout + process.stderr
        try:
            if process.returncode:
                raise ValueError(f"FFmpeg exited {process.returncode}: {process.stderr.strip()}")
            if decoder not in log:
                raise ValueError(f"Runtime log did not confirm use of {decoder}")
            checksum_records = _records(process.stdout, count=frame_count, width=640, height=360)
            feature_results[name] = _feature_result(
                "PASS",
                diagnostics=f"{decoder} independently decoded {frame_count} checksummed frames.",
                frames=frame_count,
                dimensions=[640, 360],
                checksums=checksum_records,
            )
        except ValueError as exc:
            failed_feature(name, str(exc), returncode=process.returncode, command=commands[name])

    scale_name = "scale_cuda"
    if selected_device_preflight(scale_name, ("hwupload_cuda", "scale_cuda", "cuda")):
        args = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-init_hw_device",
            f"cuda=g5cuda:{device_index}",
            "-filter_hw_device",
            "g5cuda",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=30:duration={frame_count / 30:g}",
            "-frames:v",
            str(frame_count),
            "-an",
            "-vf",
            "format=nv12,hwupload_cuda,scale_cuda=320:180," "hwdownload,format=nv12,format=yuv420p",
            "-f",
            "framemd5",
            "-",
        ]
        process = invoke(scale_name, args)
        log = process.stdout + process.stderr
        try:
            if process.returncode:
                raise ValueError(f"FFmpeg exited {process.returncode}: {process.stderr.strip()}")
            if "scale_cuda" not in log:
                raise ValueError("Runtime log did not confirm use of scale_cuda")
            checksum_records = _records(process.stdout, count=frame_count, width=320, height=180)
            feature_results[scale_name] = _feature_result(
                "PASS",
                diagnostics=f"scale_cuda produced {frame_count} checksummed 320x180 frames.",
                frames=frame_count,
                dimensions=[320, 180],
                checksums=checksum_records,
            )
        except ValueError as exc:
            failed_feature(
                scale_name, str(exc), returncode=process.returncode, command=commands[scale_name]
            )

    vmaf_name = "libvmaf_cuda"
    if selected_device_preflight(vmaf_name, ("hwupload_cuda", "libvmaf_cuda", "cuda")):
        vmaf_frames = min(8, frame_count)
        args = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-init_hw_device",
            f"cuda=g5cuda:{device_index}",
            "-filter_hw_device",
            "g5cuda",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=8:duration={vmaf_frames / 8:g}",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=8:duration={vmaf_frames / 8:g}",
            "-filter_complex",
            "[0:v]format=yuv420p,hwupload_cuda[main];"
            "[1:v]format=yuv420p,hwupload_cuda[ref];"
            "[main][ref]libvmaf_cuda=shortest=1[out]",
            "-map",
            "[out]",
            "-frames:v",
            str(vmaf_frames),
            "-an",
            "-f",
            "null",
            "-",
        ]
        process = invoke(vmaf_name, args)
        log = process.stdout + process.stderr
        try:
            if process.returncode:
                raise ValueError(f"FFmpeg exited {process.returncode}: {process.stderr.strip()}")
            if "libvmaf_cuda" not in log:
                raise ValueError("Runtime log did not confirm use of libvmaf_cuda")
            output_frames = _logged_frame_count(log)
            if output_frames != vmaf_frames:
                raise ValueError(
                    f"Expected {vmaf_frames} libvmaf_cuda output frames, "
                    f"got {output_frames if output_frames is not None else 'no frame count'}"
                )
            score_match = re.search(r"VMAF score:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", log)
            if score_match is None or not math.isfinite(float(score_match.group(1))):
                raise ValueError("Runtime log did not contain a finite CUDA VMAF score")
            feature_results[vmaf_name] = _feature_result(
                "PASS",
                diagnostics=f"libvmaf_cuda scored {vmaf_frames} generated frames.",
                frames=vmaf_frames,
                vmaf_score=float(score_match.group(1)),
            )
        except ValueError as exc:
            failed_feature(
                vmaf_name, str(exc), returncode=process.returncode, command=commands[vmaf_name]
            )

    av1_decode_name = "av1_cuvid_decode"
    if selected_device_preflight(av1_decode_name, ("av1_cuvid", "libaom_av1", "cuda")):
        av1_media = run_dir / "nvidia-av1-probe.ivf"
        generate_args = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x96:rate=5:duration=0.8",
            "-frames:v",
            "4",
            "-an",
            "-c:v",
            "libaom-av1",
            "-cpu-used",
            "8",
            "-crf",
            "35",
            "-b:v",
            "0",
            "-f",
            "ivf",
            "-y",
            _windows_path(av1_media),
        ]
        generated = invoke("generate-av1-probe", generate_args)
        try:
            if generated.returncode:
                raise ValueError(
                    f"Could not generate valid AV1 probe media (exit {generated.returncode}): "
                    f"{generated.stderr.strip()}"
                )
            if not av1_media.is_file():
                raise ValueError("AV1 probe encoder reported success but produced no media file")
            source_frames = _ivf_frame_count(av1_media, width=128, height=96)
            if source_frames != 4:
                raise ValueError(f"Expected four source AV1 frames, got {source_frames}")
        except (OSError, ValueError) as exc:
            failed_feature(av1_decode_name, str(exc), command=commands["generate-av1-probe"])
        else:
            decode_args = [
                "-hide_banner",
                "-loglevel",
                "verbose",
                "-c:v",
                "av1_cuvid",
                "-gpu",
                str(device_index),
                "-i",
                _windows_path(av1_media),
                "-frames:v",
                "4",
                "-an",
                "-f",
                "framemd5",
                "-",
            ]
            decoded = invoke(av1_decode_name, decode_args)
            log = decoded.stdout + decoded.stderr
            try:
                if decoded.returncode:
                    raise ValueError(
                        f"FFmpeg exited {decoded.returncode}: {decoded.stderr.strip()}"
                    )
                if "av1_cuvid" not in log:
                    raise ValueError("Runtime log did not confirm use of av1_cuvid")
                checksum_records = _records(decoded.stdout, count=4, width=128, height=96)
                feature_results[av1_decode_name] = _feature_result(
                    "PASS",
                    diagnostics="av1_cuvid decoded four valid AV1 frames.",
                    frames=4,
                    dimensions=[128, 96],
                    checksums=checksum_records,
                )
            except ValueError as exc:
                capability = {
                    int(depth): int(supported)
                    for depth, supported in re.findall(
                        r"(?im)^\[.*?\]\s*(8|10|12)\s*bit:\s*supported:\s*([01])\b",
                        log,
                    )
                }
                refusal = re.search(
                    r"(?:not supported|unsupported|cannot decode|failed to decode|no decoder)",
                    log,
                    re.IGNORECASE,
                )
                device_is_verified = (
                    device_identity_verified
                    and not device_identity_conflict
                    and device.get("index") == device_index
                    and " ".join(str(device.get("name", "")).casefold().split())
                    == " ".join(expected_device_name.casefold().split())
                )
                if device_is_verified and capability == {8: 0, 10: 0, 12: 0} and refusal:
                    feature_results[av1_decode_name] = _feature_result(
                        "UNSUPPORTED",
                        diagnostics=(
                            "The verified selected NVIDIA device reports AV1 CUVID support "
                            "as 0 for 8/10/12-bit and explicitly refuses this valid AV1 stream."
                        ),
                        evidence={
                            "device_verified": device,
                            "bit_depth_capabilities": capability,
                            "valid_source_frames": source_frames,
                            "decoder_refusal": refusal.group(0),
                        },
                        returncode=decoded.returncode,
                    )
                else:
                    failed_feature(
                        av1_decode_name,
                        str(exc),
                        returncode=decoded.returncode,
                        command=commands[av1_decode_name],
                        bit_depth_capabilities=capability,
                        device_verified=device_is_verified,
                    )

    statuses = [feature_results.get(name, {}).get("status") for name in _FEATURES]
    complete = all(status in ("PASS", "UNSUPPORTED") for status in statuses)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if complete else "FAIL",
        "device": device,
        "runtime": {
            "ffmpeg_exe": str(executable),
            "feature_inventory": inventories,
            "scratch_run_dir": str(run_dir),
        },
        "features": feature_results,
        "commands": commands,
    }
