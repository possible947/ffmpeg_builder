"""G5 checks for Vulkan hardware video decoding and encoding."""

from __future__ import annotations

import re
from typing import Any, Callable


def run_video_runtime(
    run: Callable[[str, list[str]], dict[str, Any]],
    device_name: str,
    validate_frames: Callable[..., list[str]],
) -> list[dict[str, Any]]:
    """Run independent H.264, HEVC and AV1 hardware paths on one selected GPU.

    The caller provides a private scratch working directory through ``run``.
    A decoder must output actual downloaded frames and report its hardware
    pixel format. An encoder must both emit frames and produce a stream that
    can be independently decoded.
    """
    results: list[dict[str, Any]] = []
    sources: dict[str, bool] = {}
    for codec, encoder in (("h264", "libx264"), ("hevc", "libx265"), ("av1", "libaom-av1")):
        command = [
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=8",
            "-frames:v",
            "8",
            "-threads",
            "1",
            "-c:v",
            encoder,
            "-y",
            f"g5-source-{codec}.{'mkv' if codec == 'av1' else 'mp4'}",
        ]
        if codec == "av1":
            command[-2:-2] = ["-cpu-used", "8"]
        result = run(f"video-source-{codec}", command)
        sources[codec] = result["returncode"] == 0 and bool(
            re.search(r"8 frames encoded", result["stderr"])
        )

    for api, pixel_format in (("vulkan", "vulkan"),):
        for codec in ("h264", "hevc", "av1"):
            feature = f"{api}_{codec}_decode"
            item: dict[str, Any] = {
                "technology": api,
                "feature": feature,
                "device": device_name,
                "status": "FAIL",
            }
            results.append(item)
            if not sources[codec]:
                item["detail"] = f"Could not generate software {codec} input."
                continue
            result = run(
                feature.replace("_", "-"),
                [
                    "-hide_banner",
                    "-loglevel",
                    "verbose",
                    "-init_hw_device",
                    f"{api}=dev:0",
                    "-hwaccel",
                    api,
                    "-hwaccel_device",
                    "dev",
                    "-hwaccel_output_format",
                    pixel_format,
                    "-i",
                    f"g5-source-{codec}.{'mkv' if codec == 'av1' else 'mp4'}",
                    "-frames:v",
                    "8",
                    "-vf",
                    "hwdownload,format=nv12",
                    "-f",
                    "framemd5",
                    "-",
                ],
            )
            log = result["stderr"]
            if (
                codec == "av1"
                and result["returncode"]
                and re.search(r"Device does not support decoding av1!", log, re.IGNORECASE)
            ):
                item.update(
                    status="UNSUPPORTED", detail="Selected device explicitly rejected AV1 decoding."
                )
                continue
            try:
                if result["returncode"]:
                    raise ValueError(f"FFmpeg exited {result['returncode']}: {log[-1500:]}")
                if device_name.casefold() not in log.casefold():
                    raise ValueError("Selected GPU was not identified in the runtime log.")
                if not re.search(rf"(?:pix_fmt|pixfmt):\s*{pixel_format}\b", log):
                    raise ValueError("Hardware decode pixel format was not reported.")
                validate_frames(result["stdout"], frames=8, dimensions=(320, 240))
            except ValueError as exc:
                item["detail"] = str(exc)
            else:
                item.update(status="PASS", detail="8 hardware-decoded frames downloaded.")

    for api in ("vulkan",):
        for codec in ("h264", "hevc", "av1"):
            feature = f"{codec}_{api}_encode"
            item = {
                "technology": api,
                "feature": feature,
                "device": device_name,
                "status": "FAIL",
            }
            results.append(item)
            output = f"g5-{codec}-{api}.mp4"
            result = run(
                feature.replace("_", "-"),
                [
                    "-hide_banner",
                    "-loglevel",
                    "verbose",
                    "-init_hw_device",
                    f"{api}=dev:0",
                    "-filter_hw_device",
                    "dev",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x240:rate=8",
                    "-frames:v",
                    "8",
                    "-vf",
                    "format=nv12,hwupload",
                    "-c:v",
                    f"{codec}_{api}",
                    "-y",
                    output,
                ],
            )
            log = result["stderr"]
            if (
                codec == "av1"
                and result["returncode"]
                and re.search(r"Device does not support encoding av1!", log, re.IGNORECASE)
            ):
                item.update(
                    status="UNSUPPORTED", detail="Selected device explicitly rejected AV1 encoding."
                )
                continue
            if (
                result["returncode"]
                or device_name.casefold() not in log.casefold()
                or not re.search(r"8 frames encoded", log)
            ):
                item["detail"] = f"Hardware encode not verified: {log[-1500:]}"
                continue
            decoded = run(
                feature.replace("_", "-") + "-verify",
                [
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    output,
                    "-frames:v",
                    "8",
                    "-f",
                    "framemd5",
                    "-",
                ],
            )
            try:
                if decoded["returncode"]:
                    raise ValueError(f"Encoded stream could not be decoded: {decoded['stderr']}")
                validate_frames(decoded["stdout"], frames=8, dimensions=(320, 240))
            except ValueError as exc:
                item["detail"] = str(exc)
            else:
                item.update(status="PASS", detail="8 hardware-encoded frames decoded.")
    return results
