from ffmpeg_builder.g5 import validate_framemd5
from ffmpeg_builder.g5_video import run_video_runtime


def _frames():
    return "#dimensions 0: 320x240\n" + "\n".join(
        f"0, {i}, {i}, 1, 115200, 0123456789abcdef0123456789abcdef" for i in range(8)
    )


def test_video_matrix_requires_hardware_and_real_output():
    calls = []

    def run(label, args):
        calls.append((label, args))
        if label.startswith("video-source-"):
            return {"returncode": 0, "stderr": "8 frames encoded", "stdout": ""}
        if label.endswith("-verify"):
            return {"returncode": 0, "stderr": "", "stdout": _frames()}
        if "decode" in label:
            pixel_format = args[args.index("-hwaccel_output_format") + 1]
            return {
                "returncode": 0,
                "stderr": f"NVIDIA TITAN V pixfmt:{pixel_format}",
                "stdout": _frames(),
            }
        return {"returncode": 0, "stderr": "NVIDIA TITAN V 8 frames encoded", "stdout": ""}

    results = run_video_runtime(run, "NVIDIA TITAN V", validate_framemd5)
    assert len(results) == 6
    assert all(result["status"] == "PASS" for result in results)
    assert all(
        not any(api in " ".join([label, *args]).lower() for api in ("d3d", "dxva"))
        for label, args in calls
    )


def test_video_matrix_does_not_hide_default_hardware_encoder_failure():
    def run(label, args):
        if label.startswith("video-source-"):
            return {"returncode": 0, "stderr": "8 frames encoded", "stdout": ""}
        if label == "h264-vulkan-encode":
            return {"returncode": 1, "stderr": "Encode failed", "stdout": ""}
        return {"returncode": 0, "stderr": "software fallback", "stdout": _frames()}

    results = run_video_runtime(run, "NVIDIA TITAN V", validate_framemd5)
    by_name = {result["feature"]: result for result in results}
    assert by_name["h264_vulkan_encode"]["status"] == "FAIL"
    assert "Encode failed" in by_name["h264_vulkan_encode"]["detail"]
    assert by_name["vulkan_h264_decode"]["status"] == "FAIL"


def test_vulkan_av1_requires_explicit_device_rejection_for_unsupported():
    def run(label, args):
        if label.startswith("video-source-"):
            return {"returncode": 0, "stderr": "8 frames encoded", "stdout": ""}
        if label == "vulkan-av1-decode":
            return {
                "returncode": 1,
                "stderr": "Device does not support decoding av1!",
                "stdout": "",
            }
        if label == "av1-vulkan-encode":
            return {
                "returncode": 1,
                "stderr": "Device does not support encoding av1!",
                "stdout": "",
            }
        return {"returncode": 1, "stderr": "Unexpected error", "stdout": ""}

    results = {r["feature"]: r for r in run_video_runtime(run, "GPU", validate_framemd5)}
    assert results["vulkan_av1_decode"]["status"] == "UNSUPPORTED"
    assert results["av1_vulkan_encode"]["status"] == "UNSUPPORTED"
    assert results["vulkan_h264_decode"]["status"] == "FAIL"
