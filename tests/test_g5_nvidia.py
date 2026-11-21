import struct
from pathlib import Path

import pytest

from ffmpeg_builder.g5_nvidia import CommandResult, run_nvidia_runtime

DEVICE_NAME = "NVIDIA TITAN V"


def _framemd5(width: int, height: int, count: int) -> str:
    size = width * height * 3 // 2
    header = (
        "#format: frame checksums\n"
        "#version: 2\n"
        "#hash: MD5\n"
        f"#dimensions 0: {width}x{height}\n"
    )
    records = "".join(f"0, {pts}, {pts}, 1, {size}, {pts + 1:032x}\n" for pts in range(count))
    return header + records


def _ivf_file(path: Path) -> None:
    header = struct.pack("<HH4sHHIIII", 0, 32, b"AV01", 128, 96, 5, 1, 4, 0)
    frames = b"".join(struct.pack("<IQ", 3, pts) + b"av1" for pts in range(4))
    path.write_bytes(b"DKIF" + header + frames)


class FakeRunner:
    def __init__(
        self,
        *,
        omit: str | None = None,
        short_h264_decode: bool = False,
        nvenc_error: str | None = None,
        wrong_device: bool = False,
        av1_decode_unsupported: bool = True,
        av1_capabilities: tuple[int, int, int] = (0, 0, 0),
        invalid_av1_source: bool = False,
    ):
        self.omit = omit
        self.short_h264_decode = short_h264_decode
        self.nvenc_error = nvenc_error
        self.wrong_device = wrong_device
        self.av1_decode_unsupported = av1_decode_unsupported
        self.av1_capabilities = av1_capabilities
        self.invalid_av1_source = invalid_av1_source
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(
        self,
        executable: Path,
        arguments: list[str],
        cwd: Path,
        label: str,
        timeout: float,
    ) -> CommandResult:
        del executable, timeout
        self.calls.append((label, arguments))
        if "-version" in arguments:
            return CommandResult(0, stdout="ffmpeg version 9.0\n")
        if "-encoders" in arguments:
            encoders = " ".join(
                name
                for name in ("h264_nvenc", "hevc_nvenc", "av1_nvenc", "libaom-av1")
                if name != self.omit
            )
            return CommandResult(0, stdout=encoders)
        if "-decoders" in arguments:
            decoders = " ".join(
                name for name in ("h264_cuvid", "hevc_cuvid", "av1_cuvid") if name != self.omit
            )
            return CommandResult(0, stdout=decoders)
        if "-filters" in arguments:
            filters = " ".join(
                name
                for name in ("hwupload_cuda", "scale_cuda", "libvmaf_cuda")
                if name != self.omit
            )
            return CommandResult(0, stdout=filters)
        if "-hwaccels" in arguments:
            return CommandResult(0, stdout="Hardware acceleration methods:\ncuda\n")

        joined = " ".join(arguments)
        device_index = 1 if self.wrong_device else 0
        device_line = f"[ GPU #{device_index} - < {DEVICE_NAME} > has Compute SM 7.0 ]\n"
        if "h264_nvenc" in arguments or "hevc_nvenc" in arguments:
            Path(arguments[-1]).write_bytes(b"encoded media")
            codec = "h264_nvenc" if "h264_nvenc" in arguments else "hevc_nvenc"
            return CommandResult(
                0,
                stderr=(
                    device_line
                    + f"[{codec}] initialized successfully\n"
                    + "Output stream #0:0 (video): 30 frames encoded\n"
                ),
            )
        if "av1_nvenc" in arguments:
            if self.nvenc_error:
                return CommandResult(1, stderr=device_line + self.nvenc_error)
            return CommandResult(
                1,
                stderr=device_line + "[av1_nvenc] Codec not supported\n",
            )
        if "libaom-av1" in arguments:
            if self.invalid_av1_source:
                Path(arguments[-1]).write_bytes(b"not an IVF stream")
            else:
                _ivf_file(Path(arguments[-1]))
            return CommandResult(0, stderr="Output stream #0:0 (video): 4 frames encoded\n")
        if "av1_cuvid" in arguments:
            if self.av1_decode_unsupported:
                return CommandResult(
                    1,
                    stderr=(
                        "[av1_cuvid] CUVID capabilities for av1_cuvid:\n"
                        f"[av1_cuvid] 8 bit: supported: {self.av1_capabilities[0]}\n"
                        f"[av1_cuvid] 10 bit: supported: {self.av1_capabilities[1]}\n"
                        f"[av1_cuvid] 12 bit: supported: {self.av1_capabilities[2]}\n"
                        "[av1_cuvid] Codec not supported\n"
                    ),
                )
            return CommandResult(
                0,
                stdout=_framemd5(128, 96, 4),
                stderr="Stream mapping: av1 (av1_cuvid) -> output\n",
            )
        if "h264_cuvid" in arguments:
            count = 29 if self.short_h264_decode else 30
            return CommandResult(
                0,
                stdout=_framemd5(640, 360, count),
                stderr="Stream mapping: h264 (h264_cuvid) -> output\n",
            )
        if "hevc_cuvid" in arguments:
            return CommandResult(
                0,
                stdout=_framemd5(640, 360, 30),
                stderr="Stream mapping: hevc (hevc_cuvid) -> output\n",
            )
        if "scale_cuda" in joined:
            return CommandResult(
                0,
                stdout=_framemd5(320, 180, 30),
                stderr="Parsed_scale_cuda configured\n",
            )
        if "libvmaf_cuda" in joined:
            return CommandResult(
                0,
                stderr=(
                    "Parsed_libvmaf_cuda VMAF score: 99.678546\n"
                    "Output stream #0:0 (video): 8 frames encoded; 8 packets muxed\n"
                ),
            )
        raise AssertionError(f"Unexpected FFmpeg invocation: {arguments!r}")


@pytest.fixture
def staged_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    exe = tmp_path / "stage" / "ffmpeg.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"accepted executable fixture")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("ffmpeg_builder.g5_nvidia._windows_path", lambda path: str(path))
    return exe, scratch


def test_full_runtime_matrix_checks_selected_gpu_frames_and_ivf(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    runner = FakeRunner()

    result = run_nvidia_runtime(executable, scratch, 0, DEVICE_NAME, runner=runner)
    assert result["status"] == "PASS", {
        key: (value["status"], value["diagnostics"]) for key, value in result["features"].items()
    }
    assert result["device"] == {
        "index": 0,
        "expected_name": DEVICE_NAME,
        "name": DEVICE_NAME,
        "compute_capability": "7.0",
    }
    assert {name: item["status"] for name, item in result["features"].items()} == {
        "feature_inventory": "PASS",
        "h264_nvenc": "PASS",
        "hevc_nvenc": "PASS",
        "h264_cuvid_decode": "PASS",
        "hevc_cuvid_decode": "PASS",
        "scale_cuda": "PASS",
        "libvmaf_cuda": "PASS",
        "av1_nvenc": "UNSUPPORTED",
        "av1_cuvid_decode": "UNSUPPORTED",
    }
    assert len(result["features"]["h264_cuvid_decode"]["checksums"]) == 30
    assert len(result["features"]["scale_cuda"]["checksums"]) == 30
    assert result["features"]["libvmaf_cuda"]["frames"] == 8
    assert result["features"]["libvmaf_cuda"]["vmaf_score"] == pytest.approx(99.678546)
    assert result["features"]["av1_cuvid_decode"]["evidence"]["valid_source_frames"] == 4
    assert result["features"]["av1_nvenc"]["evidence"]["runtime_message"] == ("Codec not supported")
    assert Path(result["runtime"]["scratch_run_dir"]).parent == scratch
    assert all(
        Path(result["runtime"]["scratch_run_dir"]) in path.parents
        for path in Path(result["runtime"]["scratch_run_dir"]).rglob("*")
    )
    for label, args in runner.calls:
        if label in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
            assert args[args.index("-gpu") + 1] == "0"
        if label in ("scale_cuda", "libvmaf_cuda"):
            assert args[args.index("-init_hw_device") + 1] == "cuda=g5cuda:0"
        if label in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
            assert "-init_hw_device" not in args
    scale_args = next(args for label, args in runner.calls if label == "scale_cuda")
    assert "hwdownload,format=nv12,format=yuv420p" in " ".join(scale_args)
    vmaf_args = next(args for label, args in runner.calls if label == "libvmaf_cuda")
    assert "[main][ref]libvmaf_cuda=shortest=1[out]" in " ".join(vmaf_args)
    assert "[score]hwdownload" not in " ".join(vmaf_args)
    assert vmaf_args[-2:] == ["null", "-"]


def test_missing_filter_is_fail_not_unsupported_and_is_not_run(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    runner = FakeRunner(omit="scale_cuda")

    result = run_nvidia_runtime(executable, scratch, 0, DEVICE_NAME, runner=runner)

    assert result["status"] == "FAIL"
    assert result["features"]["scale_cuda"]["status"] == "FAIL"
    assert "missing from inventory" in result["features"]["scale_cuda"]["diagnostics"]
    assert not any(label == "scale_cuda" for label, _ in runner.calls)


def test_zero_exit_with_missing_decoded_frame_fails_checksum_validation(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    result = run_nvidia_runtime(
        executable,
        scratch,
        0,
        DEVICE_NAME,
        runner=FakeRunner(short_h264_decode=True),
    )

    feature = result["features"]["h264_cuvid_decode"]
    assert result["status"] == "FAIL"
    assert feature["status"] == "FAIL"
    assert "Expected 30 decoded frames, got 29" in feature["diagnostics"]


def test_av1_failure_is_not_unsupported_without_device_capability_evidence(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    result = run_nvidia_runtime(
        executable,
        scratch,
        0,
        DEVICE_NAME,
        runner=FakeRunner(nvenc_error="CUDA out of memory\n"),
    )

    assert result["features"]["av1_nvenc"]["status"] == "FAIL"
    assert (
        "without explicit device capability evidence"
        in result["features"]["av1_nvenc"]["diagnostics"]
    )


def test_av1_unsupported_requires_expected_physical_device(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    result = run_nvidia_runtime(
        executable,
        scratch,
        0,
        DEVICE_NAME,
        runner=FakeRunner(wrong_device=True),
    )

    assert result["features"]["h264_nvenc"]["status"] == "FAIL"
    assert result["features"]["av1_nvenc"]["status"] == "FAIL"
    assert result["features"]["av1_cuvid_decode"]["status"] == "FAIL"
    assert "UNSUPPORTED" not in {item["status"] for item in result["features"].values()}


def test_av1_decode_capability_claim_requires_all_explicit_zero_depths(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    result = run_nvidia_runtime(
        executable,
        scratch,
        0,
        DEVICE_NAME,
        runner=FakeRunner(av1_capabilities=(0, 1, 0)),
    )

    feature = result["features"]["av1_cuvid_decode"]
    assert feature["status"] == "FAIL"
    assert feature["device_verified"] is True
    assert feature["bit_depth_capabilities"] == {8: 0, 10: 1, 12: 0}


def test_av1_decode_refusal_is_not_unsupported_without_valid_probe_media(
    staged_executable: tuple[Path, Path],
):
    executable, scratch = staged_executable
    runner = FakeRunner(invalid_av1_source=True)

    result = run_nvidia_runtime(executable, scratch, 0, DEVICE_NAME, runner=runner)

    feature = result["features"]["av1_cuvid_decode"]
    assert feature["status"] == "FAIL"
    assert "not a valid IVF/AV1 stream" in feature["diagnostics"]
    assert not any(label == "av1_cuvid_decode" for label, _ in runner.calls)


@pytest.mark.parametrize(
    ("device_index", "name", "frame_count", "message"),
    [
        (-1, DEVICE_NAME, 30, "device_index"),
        (0, " ", 30, "expected_device_name"),
        (0, DEVICE_NAME, 0, "frame_count"),
    ],
)
def test_invalid_invocation_parameters_fail_before_running(
    staged_executable: tuple[Path, Path],
    device_index: int,
    name: str,
    frame_count: int,
    message: str,
):
    executable, scratch = staged_executable
    runner = FakeRunner()

    with pytest.raises(ValueError, match=message):
        run_nvidia_runtime(
            executable,
            scratch,
            device_index,
            name,
            frame_count=frame_count,
            runner=runner,
        )
    assert runner.calls == []
