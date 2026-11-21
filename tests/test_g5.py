import json
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder import g5
from ffmpeg_builder.g5 import pending_features, run_g5, unique_device_match, validate_framemd5
from ffmpeg_builder.target_inputs import validate_target_inputs_manifest
from ffmpeg_builder.windows_probe import FeatureState


def test_frame_checksums_require_four_real_records():
    frames = [f"0, {i}, {i}, 1, 4608, 0123456789abcdef0123456789abcdef" for i in range(4)]
    assert len(validate_framemd5("#format: frame checksums\n" + "\n".join(frames))) == 4
    with pytest.raises(ValueError, match="Expected 4"):
        validate_framemd5("\n".join(frames[:3]))
    with pytest.raises(ValueError, match="Invalid frame"):
        validate_framemd5("\n".join(frames[:3] + [frames[3].replace("4608", "0")]))


def test_mapping_rejects_missing_or_ambiguous_names():
    devices = [("0.0", "TITAN V"), ("1.0", "Intel CPU")]
    assert unique_device_match(" titan  v ", devices) == "0.0"
    with pytest.raises(ValueError, match="uniquely"):
        unique_device_match("TITAN V", devices + [("2.0", "TITAN V")])
    with pytest.raises(ValueError, match="uniquely"):
        unique_device_match("missing", devices)


def test_pending_inventory_does_not_claim_runtime_success():
    target = SimpleNamespace(
        vulkan=SimpleNamespace(
            state=FeatureState.PRESENT, devices=[SimpleNamespace(name="TITAN V")]
        ),
        opencl=SimpleNamespace(
            state=FeatureState.PRESENT,
            platforms=[
                SimpleNamespace(devices=[SimpleNamespace(name="TITAN V")]),
                SimpleNamespace(devices=[SimpleNamespace(name="Intel CPU")]),
            ],
        ),
        nvidia=SimpleNamespace(state=FeatureState.UNAVAILABLE, devices=[]),
        gpus=[SimpleNamespace(adapter_type="hardware", name="TITAN V", pnp_device_id="PCI\\GPU")],
    )
    results = pending_features(target, {"configure_flags": ["--enable-libplacebo"]})
    assert [r["device"] for r in results if r["technology"] == "opencl"] == [
        "TITAN V",
        "Intel CPU",
    ]
    assert {r["feature"] for r in results if r["technology"] == "vulkan"} == {
        "scale_vulkan",
        "libplacebo",
        "vulkan_h264_decode",
        "vulkan_hevc_decode",
        "vulkan_av1_decode",
        "h264_vulkan_encode",
        "hevc_vulkan_encode",
        "av1_vulkan_encode",
    }
    assert all(
        r["status"] == "UNVALIDATED"
        for r in results
        if r["technology"] in ("vulkan", "opencl", "nvidia", "qsv", "amf")
    )
    assert all(
        r["status"] == "OUT_OF_SCOPE"
        for r in results
        if r["technology"] in g5.OUT_OF_SCOPE_TECHNOLOGIES
    )
    assert all(
        r["status"] == "DEFERRED" and r["detail"]
        for r in results
        if r["technology"] in ("neural", "interop")
    )
    neural = next(r for r in results if r["technology"] == "neural")
    assert neural["feature"] == "neural_backend"
    assert "not part" in neural["detail"]
    target.opencl.state = FeatureState.ABSENT
    assert (
        next(r for r in pending_features(target, {}) if r["technology"] == "opencl")["status"]
        == "ABSENT"
    )


def test_missing_gpu_vendor_is_absent_only_when_inventory_is_known():
    target = SimpleNamespace(
        vulkan=SimpleNamespace(state=FeatureState.ABSENT, devices=[]),
        opencl=SimpleNamespace(state=FeatureState.ABSENT, platforms=[]),
        nvidia=SimpleNamespace(state=FeatureState.ABSENT, devices=[]),
        gpus=[
            SimpleNamespace(
                adapter_type="hardware", name="NVIDIA", pnp_device_id="PCI", vendor_id="10de"
            )
        ],
    )
    result = {r["technology"]: r["status"] for r in pending_features(target, {})}
    assert result["qsv"] == result["amf"] == "ABSENT"
    target.gpus[0].vendor_id = ""
    result = {r["technology"]: r["status"] for r in pending_features(target, {})}
    assert result["qsv"] == result["amf"] == "UNVALIDATED"


def test_required_libplacebo_is_not_silently_removed_from_matrix():
    features = pending_features(_graphics_target(), {"configure_flags": []})
    item = next(f for f in features if f["feature"] == "libplacebo")
    assert item["status"] == "UNVALIDATED"


def test_preflight_failure_is_persisted_without_mutating_g4(tmp_path, monkeypatch):
    session = SimpleNamespace(
        workspace=tmp_path, identity=SimpleNamespace(to_dict=lambda: {"build": "identity"})
    )
    app = SimpleNamespace(
        prepare_target=lambda config, resume: session,
        prepare_cross_environment=lambda config, prepared: object(),
        windows_target_state=SimpleNamespace(uses_override=False),
    )
    marker = tmp_path / "g4-final-acceptance.json"
    marker.write_text("unchanged", encoding="utf-8")

    def fail(*args):
        raise BuildError("G5", "tampered G4")

    monkeypatch.setattr("ffmpeg_builder.g5.verify_g4", fail)
    monkeypatch.setattr(g5, "resolve_g5_environment", lambda *args: object())
    assert run_g5(app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)) == 2
    paths = list((tmp_path / "g5-runs").glob("*.json"))
    assert len(paths) == 1
    evidence = json.loads(paths[0].read_text())
    assert evidence["status"] == "BLOCKED"
    assert "tampered G4" in evidence["error"]
    assert {f["technology"] for f in evidence["features"] if f["status"] == "OUT_OF_SCOPE"} == {
        "d3d11va",
        "d3d12va",
        "dxva2",
    }
    assert {f["technology"] for f in evidence["features"] if f["status"] == "DEFERRED"} == {
        "neural",
        "interop",
    }
    assert marker.read_text() == "unchanged"


def test_verified_preflight_still_blocks_without_runtime_tests(tmp_path, monkeypatch):
    session = SimpleNamespace(
        workspace=tmp_path,
        identity=SimpleNamespace(to_dict=lambda: {"build": "identity"}, runtime={}),
    )
    target = SimpleNamespace(
        uses_override=False,
        vulkan=SimpleNamespace(state=FeatureState.PRESENT, devices=[SimpleNamespace(name="GPU")]),
        opencl=SimpleNamespace(state=FeatureState.ABSENT, platforms=[]),
        nvidia=SimpleNamespace(state=FeatureState.UNAVAILABLE, devices=[]),
        gpus=[],
    )
    app = SimpleNamespace(
        prepare_target=lambda config, resume: session,
        prepare_cross_environment=lambda config, prepared: object(),
        windows_target_state=target,
        _probe_windows_target=lambda config: target,
    )
    (tmp_path / "g4-final-acceptance.json").write_text('{"configure_flags": []}')
    digest = g5._digest(tmp_path / "g4-final-acceptance.json")
    environment = object()
    monkeypatch.setattr(g5, "resolve_g5_environment", lambda *args: environment)
    monkeypatch.setattr(
        g5,
        "verify_g4",
        lambda *args: {
            "binaries": {"ffmpeg.exe": "hash"},
            "manifests": {"g4-final-acceptance.json": digest},
        },
    )
    monkeypatch.setattr(g5, "stage_g4", lambda *args: {})
    monkeypatch.setattr(g5, "verify_stage", lambda *args: None)
    monkeypatch.setattr(g5, "graphics_runtime", lambda *args: {})
    monkeypatch.setattr(g5, "video_runtime", lambda *args: {})
    monkeypatch.setattr(g5, "runtime_identity", lambda *args: {})
    assert run_g5(app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)) == 2
    evidence = json.loads(next((tmp_path / "g5-runs").glob("*.json")).read_text())
    assert evidence["status"] == "BLOCKED"
    assert evidence["g4"]["binaries"]["ffmpeg.exe"] == "hash"
    assert evidence["g4_after"] == evidence["g4"]
    assert evidence["environment_rechecked"] is True
    assert (
        next(f for f in evidence["features"] if f["feature"] == "scale_vulkan")["status"]
        == "UNVALIDATED"
    )


def test_post_runtime_g4_drift_invalidates_feature_passes(tmp_path, monkeypatch):
    session = SimpleNamespace(
        workspace=tmp_path, identity=SimpleNamespace(to_dict=lambda: {}, runtime={})
    )
    target = _graphics_target()
    target.uses_override = False
    app = SimpleNamespace(
        prepare_target=lambda config, resume: session,
        prepare_cross_environment=lambda config, prepared: object(),
        windows_target_state=target,
    )
    manifest_path = tmp_path / "g4-final-acceptance.json"
    manifest_path.write_text('{"configure_flags": []}')
    original = {
        "manifests": {"g4-final-acceptance.json": g5._digest(manifest_path)},
        "binaries": {"ffmpeg.exe": "original"},
    }
    snapshots = iter([original, {**original, "binaries": {"ffmpeg.exe": "changed"}}])
    monkeypatch.setattr(g5, "resolve_g5_environment", lambda *args: object())
    monkeypatch.setattr(g5, "verify_g4", lambda *args: next(snapshots))
    monkeypatch.setattr(g5, "stage_g4", lambda *args: {})
    monkeypatch.setattr(g5, "verify_stage", lambda *args: None)

    def graphics(target, features, stage, interop):
        next(f for f in features if f["feature"] == "scale_vulkan")["status"] = "PASS"
        return {}

    monkeypatch.setattr(g5, "graphics_runtime", graphics)
    assert run_g5(app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)) == 2
    evidence = json.loads(next((tmp_path / "g5-runs").glob("*.json")).read_text())
    assert "changed during runtime" in evidence["error"]
    assert (
        next(f for f in evidence["features"] if f["feature"] == "scale_vulkan")["status"]
        == "UNVALIDATED"
    )
    assert evidence["g4_after"]["binaries"]["ffmpeg.exe"] == "changed"


def _framemd5(width, height):
    size = width * height * 3 // 2
    return f"#dimensions 0: {width}x{height}\n" + "\n".join(
        f"0, {i}, {i}, 1, {size}, 0123456789abcdef0123456789abcdef" for i in range(4)
    )


def _graphics_target():
    return SimpleNamespace(
        vulkan=SimpleNamespace(
            state=FeatureState.PRESENT,
            devices=[SimpleNamespace(name="TITAN V", device_id="1db1")],
        ),
        opencl=SimpleNamespace(
            state=FeatureState.PRESENT,
            platforms=[
                SimpleNamespace(name="NVIDIA CUDA", devices=[SimpleNamespace(name="TITAN V")]),
                SimpleNamespace(name="Intel OpenCL", devices=[SimpleNamespace(name="CPU")]),
            ],
        ),
        nvidia=SimpleNamespace(state=FeatureState.ABSENT, devices=[]),
        gpus=[],
    )


def _nvidia_target():
    device = SimpleNamespace(
        name="NVIDIA TITAN V",
        uuid="GPU-unique",
        pci_bus_id="00000000:65:00.0",
        gpu_pnp_device_id="PCI\\GPU",
    )
    gpu = SimpleNamespace(
        adapter_type="hardware",
        vendor_id="10de",
        device_id="1db1",
        name="NVIDIA TITAN V",
        pnp_device_id="pci\\gpu",
    )
    target = _graphics_target()
    target.nvidia = SimpleNamespace(state=FeatureState.PRESENT, devices=[device])
    target.gpus = [gpu]
    return target


def _nvidia_result():
    features = {
        feature: {"status": "PASS", "diagnostics": f"{feature} ran"}
        for feature in g5.NVIDIA_FEATURES
    }
    features["feature_inventory"] = {"status": "PASS", "diagnostics": "listing only"}
    features["av1_nvenc"] = {"status": "UNSUPPORTED", "diagnostics": "device refused AV1"}
    features["hevc_nvenc"] = {"status": "FAIL", "diagnostics": "encode failed"}
    return {
        "status": "FAIL",
        "device": {"index": 0, "name": "NVIDIA TITAN V"},
        "features": features,
    }


def test_nvidia_runtime_maps_unique_physical_gpu_and_keeps_feature_results(tmp_path, monkeypatch):
    target = _nvidia_target()
    features = pending_features(target, {})
    calls = []

    def run(exe, scratch, device_index, expected_name, *, interop):
        calls.append((exe, scratch, device_index, expected_name, interop))
        return _nvidia_result()

    monkeypatch.setattr(g5, "run_nvidia_runtime", run)
    evidence = g5.nvidia_runtime(target, features, tmp_path, "interop")
    assert calls == [(tmp_path / "ffmpeg.exe", tmp_path, 0, "NVIDIA TITAN V", "interop")]
    assert evidence["status"] == "FAIL"
    statuses = {f["feature"]: f["status"] for f in features if f["technology"] == "nvidia"}
    assert statuses["h264_nvenc"] == "PASS"
    assert statuses["hevc_nvenc"] == "FAIL"
    assert statuses["av1_nvenc"] == "UNSUPPORTED"
    assert "feature_inventory" not in statuses


def test_nvidia_mapping_blocks_multiple_or_unmatched_physical_gpus(tmp_path, monkeypatch):
    target = _nvidia_target()
    monkeypatch.setattr(g5, "run_nvidia_runtime", lambda *args, **kwargs: pytest.fail("ambiguous"))
    target.nvidia.devices = target.nvidia.devices * 2
    features = pending_features(target, {})
    assert g5.nvidia_runtime(target, features, tmp_path, None)["status"] == "BLOCKED"
    assert all(f["status"] == "UNVALIDATED" for f in features if f["technology"] == "nvidia")
    target.nvidia.devices = target.nvidia.devices[:1]
    target.nvidia.devices[0].gpu_pnp_device_id = "PCI\\OTHER"
    features = pending_features(target, {})
    assert g5.nvidia_runtime(target, features, tmp_path, None)["status"] == "BLOCKED"
    target.nvidia.state = FeatureState.ABSENT
    features = pending_features(target, {})
    assert g5.nvidia_runtime(target, features, tmp_path, None)["status"] == "BLOCKED"
    assert all(f["status"] == "UNVALIDATED" for f in features if f["technology"] == "nvidia")


def test_nvidia_runtime_does_not_hide_inventory_failure_or_wrong_device(tmp_path, monkeypatch):
    target = _nvidia_target()
    result = _nvidia_result()
    result["features"]["feature_inventory"] = {"status": "FAIL", "diagnostics": "listing failed"}
    result["commands"] = {"inventory": {"stderr": "listing failed"}}
    monkeypatch.setattr(g5, "run_nvidia_runtime", lambda *args, **kwargs: result)
    features = pending_features(target, {})
    assert (
        g5.nvidia_runtime(target, features, tmp_path, None)["commands"]["inventory"]["stderr"]
        == "listing failed"
    )
    assert all(f["status"] == "FAIL" for f in features if f["technology"] == "nvidia")
    result["features"]["feature_inventory"]["status"] = "PASS"
    result["device"]["name"] = "different GPU"
    features = pending_features(target, {})
    g5.nvidia_runtime(target, features, tmp_path, None)
    assert all(f["status"] == "FAIL" for f in features if f["technology"] == "nvidia")


def test_nvidia_unsupported_is_not_reported_as_matrix_pass(tmp_path, monkeypatch):
    target = _nvidia_target()
    result = _nvidia_result()
    result["status"] = "PASS"
    result["features"]["hevc_nvenc"]["status"] = "PASS"
    monkeypatch.setattr(g5, "run_nvidia_runtime", lambda *args, **kwargs: result)
    features = pending_features(target, {})
    summary = g5.nvidia_runtime(target, features, tmp_path, None)
    assert summary["helper_status"] == "PASS"
    assert summary["status"] == "UNSUPPORTED"
    assert next(f for f in features if f["feature"] == "av1_nvenc")["status"] == "UNSUPPORTED"


def _video_target():
    target = _nvidia_target()
    target.vulkan = SimpleNamespace(
        state=FeatureState.PRESENT,
        devices=[SimpleNamespace(name="NVIDIA TITAN V", vendor_id="10de", device_id="1db1")],
    )
    return target


def test_video_independently_preserves_known_failures_and_commands(tmp_path, monkeypatch):
    target = _video_target()
    features = pending_features(target, {})
    calls = []
    vk = (
        "[Vulkan] 0: NVIDIA TITAN V (discrete) (0x1db1)\n"
        "[Vulkan] Device 0 selected: NVIDIA TITAN V (discrete) (0x1db1)\n"
    )

    def run(stage, label, args, interop):
        calls.append((label, args))
        if label.startswith("video-source-"):
            return {"returncode": 0, "stdout": "", "stderr": "8 frames encoded"}
        if label.endswith("-verify"):
            return {
                "returncode": 0,
                "stdout": _framemd5(320, 240).replace(
                    "\n".join(
                        f"0, {i}, {i}, 1, 115200, 0123456789abcdef0123456789abcdef"
                        for i in range(4)
                    ),
                    "\n".join(
                        f"0, {i}, {i}, 1, 115200, 0123456789abcdef0123456789abcdef"
                        for i in range(8)
                    ),
                ),
                "stderr": "",
            }
        spec = args[args.index("-init_hw_device") + 1]
        assert spec == "vulkan=dev:0"
        log = vk
        if label == "h264-vulkan-encode":
            return {"returncode": 1, "stdout": "", "stderr": log + "hardware failure"}
        if "decode" in label:
            pixel_format = args[args.index("-hwaccel_output_format") + 1]
            output = "#dimensions 0: 320x240\n" + "\n".join(
                f"0, {i}, {i}, 1, 115200, 0123456789abcdef0123456789abcdef" for i in range(8)
            )
            return {"returncode": 0, "stdout": output, "stderr": log + f"pixfmt:{pixel_format}"}
        return {"returncode": 0, "stdout": "", "stderr": log + "8 frames encoded"}

    monkeypatch.setattr(g5, "run_windows_ffmpeg", run)
    evidence = g5.video_runtime(target, features, tmp_path, None)
    by_name = {f["feature"]: f for f in features if f["feature"] in g5.VIDEO_FEATURES}
    assert len(by_name) == 13
    assert by_name["h264_d3d12va_encode"]["status"] == "OUT_OF_SCOPE"
    assert by_name["hevc_d3d12va_roundtrip"]["status"] == "OUT_OF_SCOPE"
    assert by_name["hevc_vulkan_encode"]["status"] == "PASS"
    assert by_name["h264_vulkan_encode"]["status"] == "FAIL"
    assert by_name["vulkan_h264_decode"]["status"] == "PASS"
    assert "hardware failure" in evidence["commands"]["h264-vulkan-encode"]["stderr"]
    assert all("d3d" not in label and "dxva" not in label for label, _ in calls)


def test_video_blocks_ambiguous_gpu_and_unverified_ordinal(tmp_path, monkeypatch):
    target = _video_target()
    features = pending_features(target, {})
    target.gpus.append(target.gpus[0])
    monkeypatch.setattr(g5, "run_windows_ffmpeg", lambda *args: pytest.fail("ambiguous device"))
    assert g5.video_runtime(target, features, tmp_path, None)["status"] == "BLOCKED"
    assert all(
        f["status"] == ("UNVALIDATED" if f["technology"] == "vulkan" else "OUT_OF_SCOPE")
        for f in features
        if f["feature"] in g5.VIDEO_FEATURES
    )
    target.gpus.pop()
    calls = []

    def run(stage, label, args, interop):
        calls.append(label)
        return {"returncode": 0, "stderr": "Using device 10de:9999 (Other GPU).", "stdout": ""}

    monkeypatch.setattr(g5, "run_windows_ffmpeg", run)
    evidence = g5.video_runtime(target, features, tmp_path, None)
    assert set(evidence["mapping_errors"]) == {"vulkan"}
    assert all(
        f["status"] == ("UNVALIDATED" if f["technology"] == "vulkan" else "OUT_OF_SCOPE")
        for f in features
        if f["feature"] in g5.VIDEO_FEATURES
    )
    assert calls == [
        "video-map-vulkan",
        "video-source-h264",
        "video-source-hevc",
        "video-source-av1",
    ]


def test_video_rejects_pass_without_runtime_commands(tmp_path, monkeypatch):
    target = _video_target()
    features = pending_features(target, {})
    d3d = "Using device 10de:1db1 (NVIDIA TITAN V)."
    vk = (
        "[Vulkan] 0: NVIDIA TITAN V (discrete) (0x1db1)\n"
        "[Vulkan] Device 0 selected: NVIDIA TITAN V (discrete) (0x1db1)"
    )
    monkeypatch.setattr(
        g5,
        "run_windows_ffmpeg",
        lambda stage, label, args, interop: {
            "returncode": 0,
            "stderr": vk if label == "video-map-vulkan" else d3d,
            "stdout": "",
        },
    )
    monkeypatch.setattr(
        g5,
        "run_video_runtime",
        lambda run, name, validate: [
            {"technology": api, "feature": feature, "device": name, "status": "PASS"}
            for feature, api in g5.VIDEO_FEATURES.items()
            if api == "vulkan"
        ],
    )
    g5.video_runtime(target, features, tmp_path, None)
    assert all(
        f["status"] == ("FAIL" if f["technology"] == "vulkan" else "OUT_OF_SCOPE")
        for f in features
        if f["feature"] in g5.VIDEO_FEATURES
    )


def test_video_records_unrun_vulkan_feature(tmp_path, monkeypatch):
    target = _video_target()
    features = pending_features(target, {})
    d3d = "Using device 10de:1db1 (NVIDIA TITAN V)."
    vk = (
        "[Vulkan] 0: NVIDIA TITAN V (discrete) (0x1db1)\n"
        "[Vulkan] Device 0 selected: NVIDIA TITAN V (discrete) (0x1db1)"
    )
    monkeypatch.setattr(
        g5,
        "run_windows_ffmpeg",
        lambda stage, label, args, interop: {
            "returncode": 0,
            "stderr": vk if label == "video-map-vulkan" else d3d,
            "stdout": "",
        },
    )
    monkeypatch.setattr(
        g5,
        "run_video_runtime",
        lambda run, name, validate: [
            {"technology": api, "feature": feature, "device": name, "status": "FAIL"}
            for feature, api in g5.VIDEO_FEATURES.items()
            if api == "vulkan" and feature != "hevc_vulkan_encode"
        ],
    )
    g5.video_runtime(target, features, tmp_path, None)
    missing = next(f for f in features if f["feature"] == "hevc_vulkan_encode")
    assert missing["status"] == "UNVALIDATED"


def test_video_failure_persists_overall_fail(tmp_path, monkeypatch):
    target = _video_target()
    target.uses_override = False
    session = SimpleNamespace(
        workspace=tmp_path, identity=SimpleNamespace(to_dict=lambda: {}, runtime={})
    )
    app = SimpleNamespace(
        prepare_target=lambda config, resume: session,
        windows_target_state=target,
        _probe_windows_target=lambda config: target,
    )
    manifest_path = tmp_path / "g4-final-acceptance.json"
    manifest_path.write_text('{"configure_flags": []}')
    g4 = {"manifests": {"g4-final-acceptance.json": g5._digest(manifest_path)}}
    environment = object()
    monkeypatch.setattr(g5, "resolve_g5_environment", lambda *args: environment)
    monkeypatch.setattr(g5, "verify_g4", lambda *args: g4)
    monkeypatch.setattr(g5, "stage_g4", lambda *args: {})
    monkeypatch.setattr(g5, "verify_stage", lambda *args: None)
    monkeypatch.setattr(g5, "graphics_runtime", lambda *args: {})
    monkeypatch.setattr(g5, "nvidia_runtime", lambda *args: {})
    monkeypatch.setattr(g5, "runtime_identity", lambda *args: {})

    def video(target, features, stage, interop):
        item = next(f for f in features if f["feature"] == "h264_vulkan_encode")
        item.update(status="FAIL", detail="actual encode failed")
        return {"commands": {"encode": {"returncode": 1}}}

    monkeypatch.setattr(g5, "video_runtime", video)
    assert run_g5(app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)) == 1
    evidence = json.loads(next((tmp_path / "g5-runs").glob("*.json")).read_text())
    assert evidence["status"] == "FAIL"
    assert evidence["video"]["commands"]["encode"]["returncode"] == 1
    assert evidence["g4"] == evidence["g4_after"]
    assert "h264_vulkan_encode" in evidence["error"]


def test_out_of_scope_failures_do_not_block_and_old_evidence_is_preserved(tmp_path, monkeypatch):
    target = _video_target()
    target.uses_override = False
    session = SimpleNamespace(
        workspace=tmp_path, identity=SimpleNamespace(to_dict=lambda: {}, runtime={})
    )
    app = SimpleNamespace(
        prepare_target=lambda config, resume: session,
        windows_target_state=target,
        _probe_windows_target=lambda config: target,
    )
    manifest_path = tmp_path / "g4-final-acceptance.json"
    manifest_path.write_text('{"configure_flags": []}')
    g4 = {"manifests": {"g4-final-acceptance.json": g5._digest(manifest_path)}}
    old_dir = tmp_path / "g5-runs"
    old_dir.mkdir()
    old_evidence = old_dir / "previous.json"
    old_evidence.write_text('{"status":"FAIL","feature":"d3d12va"}')
    old_stage = old_dir / "stage-previous"
    old_stage.mkdir()
    old_artifact = old_stage / "runtime.log"
    old_artifact.write_bytes(b"old runtime evidence")
    environment = object()
    monkeypatch.setattr(g5, "resolve_g5_environment", lambda *args: environment)
    monkeypatch.setattr(g5, "verify_g4", lambda *args: g4)
    monkeypatch.setattr(g5, "stage_g4", lambda *args: {})
    monkeypatch.setattr(g5, "verify_stage", lambda *args: None)
    monkeypatch.setattr(g5, "runtime_identity", lambda *args: {})

    def graphics(target, features, stage, interop):
        for item in features:
            if item["status"] == "UNVALIDATED":
                item["status"] = "PASS"
            if item["technology"] in g5.OUT_OF_SCOPE_TECHNOLOGIES:
                item["status"] = "FAIL"
        return {}

    monkeypatch.setattr(g5, "graphics_runtime", graphics)
    monkeypatch.setattr(g5, "nvidia_runtime", lambda *args: {})
    monkeypatch.setattr(g5, "video_runtime", lambda *args: {})
    assert run_g5(app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)) == 0
    new_path = next(path for path in old_dir.glob("*.json") if path != old_evidence)
    evidence = json.loads(new_path.read_text())
    assert evidence["status"] == "PASS"
    assert evidence["reuse"]["status"] == "NOT_USED"
    assert not evidence["error"]
    assert all(
        item["status"] == "OUT_OF_SCOPE"
        for item in evidence["features"]
        if item["technology"] in g5.OUT_OF_SCOPE_TECHNOLOGIES
    )
    assert old_evidence.read_text() == '{"status":"FAIL","feature":"d3d12va"}'
    assert old_artifact.read_bytes() == b"old runtime evidence"


def test_reuse_requires_latest_matching_identity_contract_and_complete_matrix(tmp_path):
    directory = tmp_path / "g5-runs"
    directory.mkdir()
    current = {
        "identity": {"runtime": {"driver": "1"}, "build": {"key": "a"}},
        "contract": "contract-a",
        "g4": {"binaries": {"ffmpeg.exe": "abc"}},
        "stage": {"binaries": {}, "toolchain_dlls": {}, "system_imports": []},
        "features": [
            {"technology": "nvidia", "feature": "scale_cuda", "status": "UNVALIDATED"},
            {"technology": "neural", "feature": "neural_backend", "status": "DEFERRED"},
        ],
    }
    previous = {
        **current,
        "started_at": "2026-01-01",
        "status": "PASS",
        "g4_after": current["g4"],
        "runtime_identity_rechecked": True,
        "environment_rechecked": True,
        "stage": {**current["stage"], "rechecked": True},
        "graphics": {},
        "nvidia": {},
        "video": {},
        "features": [
            {**current["features"][0], "status": "PASS"},
            current["features"][1],
        ],
    }
    path = directory / "accepted.json"
    path.write_text(json.dumps(previous))
    assert g5._reusable_evidence(tmp_path, current)[0] == path
    for changed in (
        {**current, "contract": "changed"},
        {**current, "identity": {"runtime": {"driver": "2"}, "build": {"key": "a"}}},
        {**current, "g4": {"binaries": {"ffmpeg.exe": "different"}}},
        {**current, "stage": {**current["stage"], "toolchain_dlls": {"x": "different"}}},
        {**current, "features": current["features"] + [{"feature": "new"}]},
    ):
        assert g5._reusable_evidence(tmp_path, changed) is None
    latest = {**previous, "started_at": "2026-01-02", "status": "FAIL"}
    (directory / "later.json").write_text(json.dumps(latest))
    assert g5._reusable_evidence(tmp_path, current) is None


def test_reuse_rejects_missing_integrity_or_feature_results(tmp_path):
    directory = tmp_path / "g5-runs"
    directory.mkdir()
    stage = {"binaries": {}, "toolchain_dlls": {}, "system_imports": []}
    feature = {"technology": "vulkan", "feature": "scale_vulkan", "status": "UNVALIDATED"}
    current = {"identity": {}, "contract": "a", "g4": {}, "stage": stage, "features": [feature]}
    previous = {
        **current,
        "started_at": "2026",
        "status": "PASS",
        "g4_after": {},
        "environment_rechecked": True,
        "runtime_identity_rechecked": True,
        "stage": {**stage, "rechecked": True},
        "features": [{**feature, "status": "PASS"}],
        "graphics": {},
        "nvidia": {},
        "video": {},
    }
    path = directory / "accepted.json"
    for changed in (
        {**previous, "runtime_identity_rechecked": False},
        {**previous, "g4_after": {"changed": True}},
        {**previous, "features": []},
        {**previous, "features": [{**feature, "status": "FAIL"}]},
        {**previous, "features": [{**feature, "status": "PASS"}] * 2},
    ):
        path.write_text(json.dumps(changed))
        assert g5._reusable_evidence(tmp_path, current) is None


def test_evidence_writer_never_overwrites_or_deletes_existing_files(tmp_path, monkeypatch):
    directory = tmp_path / "g5-runs"
    directory.mkdir()
    monkeypatch.setattr(g5.uuid, "uuid4", lambda: SimpleNamespace(hex="existing"))
    old_evidence = directory / "existing.json"
    old_evidence.write_text("original evidence")
    with pytest.raises(FileExistsError):
        g5._write_evidence(tmp_path, {"status": "BLOCKED"})
    assert old_evidence.read_text() == "original evidence"
    temporary = directory / ".existing.tmp"
    temporary.write_text("original temporary evidence")
    with pytest.raises(FileExistsError):
        g5._write_evidence(tmp_path, {"status": "BLOCKED"})
    assert temporary.read_text() == "original temporary evidence"


def test_runtime_feature_failure_persists_fail_instead_of_blocked(tmp_path, monkeypatch):
    target = _nvidia_target()
    target.uses_override = False
    session = SimpleNamespace(
        workspace=tmp_path, identity=SimpleNamespace(to_dict=lambda: {}, runtime={})
    )
    app = SimpleNamespace(
        prepare_target=lambda config, resume: session,
        windows_target_state=target,
        _probe_windows_target=lambda config: target,
    )
    manifest_path = tmp_path / "g4-final-acceptance.json"
    manifest_path.write_text('{"configure_flags": []}')
    g4 = {"manifests": {"g4-final-acceptance.json": g5._digest(manifest_path)}}
    environment = object()
    monkeypatch.setattr(g5, "resolve_g5_environment", lambda *args: environment)
    monkeypatch.setattr(g5, "verify_g4", lambda *args: g4)
    monkeypatch.setattr(g5, "stage_g4", lambda *args: {})
    monkeypatch.setattr(g5, "verify_stage", lambda *args: None)
    monkeypatch.setattr(g5, "graphics_runtime", lambda *args: {})
    monkeypatch.setattr(g5, "runtime_identity", lambda *args: {})
    monkeypatch.setattr(g5, "run_nvidia_runtime", lambda *args, **kwargs: _nvidia_result())
    monkeypatch.setattr(g5, "video_runtime", lambda *args: {})
    assert run_g5(app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)) == 1
    evidence = json.loads(next((tmp_path / "g5-runs").glob("*.json")).read_text())
    assert evidence["status"] == "FAIL"
    assert "hevc_nvenc" in evidence["error"]
    assert (
        next(f for f in evidence["features"] if f["feature"] == "av1_nvenc")["status"]
        == "UNSUPPORTED"
    )


def test_graphics_maps_probe_to_listing_and_runs_each_filter_and_device(monkeypatch, tmp_path):
    target = _graphics_target()
    features = pending_features(target, {"configure_flags": ["--enable-libplacebo"]})
    calls = []

    def run(stage, label, args, interop):
        calls.append((label, args))
        if label == "vulkan-listing":
            log = "[AVHWDeviceContext @ ab]    3: TITAN V (discrete) (0x1db1)\n"
            return {"stderr": log, "stdout": "", "returncode": 0}
        if label == "opencl-listing":
            log = (
                "[AVHWDeviceContext @ ab] 1.0: NVIDIA CUDA / TITAN V\n"
                "[AVHWDeviceContext @ ab] 0.0: Intel OpenCL / CPU\n"
            )
            return {"stderr": log, "stdout": "", "returncode": 1}
        if label.startswith("vulkan"):
            return {
                "stderr": "[AVHWDeviceContext @ ab] Device 3 selected: TITAN V (discrete) (0x1db1)",
                "stdout": _framemd5(64, 48),
                "returncode": 0,
            }
        name, platform, selector = (
            ("TITAN V", "NVIDIA CUDA", "1.0")
            if label == "opencl-0-avgblur_opencl"
            else ("CPU", "Intel OpenCL", "0.0")
        )
        return {
            "stderr": f"[AVHWDeviceContext @ ab] {selector}: {platform} / {name}",
            "stdout": _framemd5(128, 96),
            "returncode": 0,
        }

    monkeypatch.setattr(g5, "run_windows_ffmpeg", run)
    graphics = g5.graphics_runtime(target, features, tmp_path, None)
    tested = [
        f for f in features if f["feature"] in ("scale_vulkan", "libplacebo", "avgblur_opencl")
    ]
    assert len(tested) == 4 and all(f["status"] == "PASS" for f in tested)
    assert [f["selector"] for f in tested if f["technology"] == "opencl"] == ["1.0", "0.0"]
    assert {name for name, _ in calls} >= {
        "vulkan-0-scale_vulkan",
        "vulkan-0-libplacebo",
        "opencl-0-avgblur_opencl",
        "opencl-1-avgblur_opencl",
    }
    assert graphics["opencl"]["returncode"] == 1


def test_graphics_ambiguous_mapping_and_failed_output_are_independent(monkeypatch, tmp_path):
    target = _graphics_target()
    features = pending_features(target, {"configure_flags": ["--enable-libplacebo"]})

    def run(stage, label, args, interop):
        if label == "vulkan-listing":
            return {
                "stderr": "[AVHWDeviceContext @ a] 0: TITAN V (discrete) (0x1db1)\n",
                "returncode": 0,
            }
        if label == "opencl-listing":
            return {
                "stderr": "[AVHWDeviceContext @ a] 0.0: NVIDIA CUDA / TITAN V\n"
                "[AVHWDeviceContext @ a] 1.0: NVIDIA CUDA / TITAN V\n"
                "[AVHWDeviceContext @ a] 2.0: Intel OpenCL / CPU\n",
                "returncode": 1,
            }
        if label == "vulkan-0-scale_vulkan":
            return {
                "stderr": "Device 0 selected: TITAN V (discrete) (0x1db1)",
                "stdout": "",
                "returncode": 1,
            }
        if label == "vulkan-0-libplacebo":
            return {
                "stderr": "Device 0 selected: TITAN V (discrete) (0x1db1)",
                "stdout": _framemd5(64, 48),
                "returncode": 0,
            }
        return {
            "stderr": "[AVHWDeviceContext @ a] 2.0: Intel OpenCL / CPU",
            "stdout": _framemd5(128, 96),
            "returncode": 0,
        }

    monkeypatch.setattr(g5, "run_windows_ffmpeg", run)
    g5.graphics_runtime(target, features, tmp_path, None)
    statuses = {(f["feature"], f.get("device")): f["status"] for f in features}
    assert statuses["scale_vulkan", "TITAN V"] == "FAIL"
    assert statuses["libplacebo", "TITAN V"] == "PASS"
    assert statuses["avgblur_opencl", "TITAN V"] == "FAIL"
    assert statuses["avgblur_opencl", "CPU"] == "PASS"


def test_staging_verifies_accepted_import_closure_and_rechecks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    binaries = workspace / "bin"
    runtime = tmp_path / "toolchain/x86_64-w64-mingw32/bin"
    binaries.mkdir(parents=True)
    runtime.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (binaries / name).write_bytes(name.encode())
    (runtime / "libc++.dll").write_bytes(b"runtime")
    environment = SimpleNamespace(target_tools=SimpleNamespace(prefix=tmp_path / "toolchain"))
    manifest = {
        "binaries": {name: g5._digest(binaries / name) for name in ("ffmpeg.exe", "ffprobe.exe")},
        "runtime": {"imports": ["kernel32.dll", "libc++.dll"], "toolchain_dlls": ["libc++.dll"]},
    }

    def imports(environment, path):
        return {"kernel32.dll"} if path.suffix == ".dll" else {"KERNEL32.dll", "libc++.dll"}

    monkeypatch.setattr(g5, "_imports", imports)
    interop = SimpleNamespace(
        run_powershell=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    )
    stage = tmp_path / "stage"
    evidence = g5.stage_g4(workspace, environment, manifest, stage, interop)
    g5.verify_stage(stage, environment, evidence)
    (runtime / "libc++.dll").write_bytes(b"changed")
    with pytest.raises(BuildError, match="DLL changed"):
        g5.verify_stage(stage, environment, evidence)
    manifest["runtime"]["toolchain_dlls"] = []
    with pytest.raises(BuildError, match="Unexpected toolchain DLL"):
        g5.stage_g4(workspace, environment, manifest, tmp_path / "other", interop)


def test_windows_runner_quotes_arguments_as_windows_argv(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(g5, "_windows_path", lambda path: r"C:\test path")
    interop = SimpleNamespace(
        run_powershell=lambda script, timeout: (
            seen.append(script) or SimpleNamespace(returncode=0, stderr=b"")
        )
    )
    result = g5.run_windows_ffmpeg(tmp_path, "quoted", ["-i", r"C:\a b.mp4"], interop)
    assert result["returncode"] == 0
    assert """-ArgumentList '-i "C:\\a b.mp4"'""" in seen[0]


def _readonly_resolver_fixture(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "target-inputs.json").write_text('{"materialization_id":"inputs"}')
    identity = SimpleNamespace(
        backend=TARGET_BACKEND_CROSS_MINGW,
        schema_version=2,
        build_fingerprint="fingerprint",
        build_key="key",
        runtime={"device": "physical"},
        build={"material_inputs": {"expected": "material"}},
    )
    target = SimpleNamespace(uses_override=False, runtime=identity.runtime)
    summary = {
        "schema_version": 2,
        "backend": TARGET_BACKEND_CROSS_MINGW,
        "digest": "fingerprint",
        "build_key": "key",
        "target_input_materialization_id": "inputs",
    }
    prep = SimpleNamespace(
        identity_summary=summary,
        target=target,
        project_toolchains={"llvm-mingw": {}},
        cuda=None,
        vulkan=None,
    )
    session = SimpleNamespace(
        workspace=tmp_path,
        backend=TARGET_BACKEND_CROSS_MINGW,
        uses_isolated_workspace=True,
        identity=identity,
    )
    app = SimpleNamespace(
        workspace=tmp_path,
        wsl_mingw_preparation=prep,
        windows_target_state=target,
        _get_buildable_components=lambda config: [],
        prepare_cross_environment=lambda *args: pytest.fail("G3 preparation must not run"),
    )
    monkeypatch.setattr(g5, "runtime_identity", lambda target: target.runtime)
    monkeypatch.setattr(g5, "validate_target_inputs_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        g5,
        "project_material_build_inputs",
        lambda *args: SimpleNamespace(to_dict=lambda: {"expected": "material"}),
    )
    return app, session, prep


def test_readonly_resolution_preserves_concurrent_generated_edits(tmp_path, monkeypatch):
    app, session, prep = _readonly_resolver_fixture(tmp_path, monkeypatch)
    generated = tmp_path / "generated"
    generated.mkdir()
    wrapper = generated / "pkg-config"
    wrapper.write_text("original")
    wrapper.chmod(0o751)
    (tmp_path / "prefix/lib/pkgconfig").mkdir(parents=True)
    scratch = tmp_path / "prefix/lib/pkgconfig/g3-probe.pc"
    scratch.write_text("existing")

    def resolve(preparation):
        wrapper.write_text("concurrent edit")
        return SimpleNamespace(prefix=tmp_path / "verified-toolchain")

    monkeypatch.setattr(g5, "_resolve_target_tools", resolve)
    environment = g5.resolve_g5_environment(
        app, BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW), session
    )
    assert environment.paths.target_prefix == tmp_path / "prefix"
    assert environment.target_tools.prefix == tmp_path / "verified-toolchain"
    assert wrapper.read_text() == "concurrent edit"
    assert wrapper.stat().st_mode & 0o777 == 0o751
    assert scratch.read_text() == "existing"


def test_readonly_resolution_rejects_mismatched_metadata(tmp_path, monkeypatch):
    app, session, prep = _readonly_resolver_fixture(tmp_path, monkeypatch)
    config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)
    monkeypatch.setattr(g5, "_resolve_target_tools", lambda prep: pytest.fail("untrusted tools"))
    prep.identity_summary["digest"] = "wrong"
    with pytest.raises(BuildError, match="authorized session identity"):
        g5.resolve_g5_environment(app, config, session)
    prep.identity_summary["digest"] = "fingerprint"
    prep.identity_summary["target_input_materialization_id"] = "wrong"
    with pytest.raises(BuildError, match="materialization"):
        g5.resolve_g5_environment(app, config, session)
    prep.identity_summary["target_input_materialization_id"] = "inputs"
    monkeypatch.setattr(
        g5, "project_material_build_inputs", lambda *args: SimpleNamespace(to_dict=lambda: {})
    )
    with pytest.raises(BuildError, match="toolchain provenance"):
        g5.resolve_g5_environment(app, config, session)


def test_readonly_resolution_checks_actual_target_input_hashes(tmp_path, monkeypatch):
    app, session, prep = _readonly_resolver_fixture(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    header = prefix / "include" / "test.h"
    header.parent.mkdir()
    header.write_text("original")
    manifest = {
        "target_backend": TARGET_BACKEND_CROSS_MINGW,
        "sources": {},
        "interfaces": [],
        "files": {"include/test.h": g5._digest(header)},
    }
    from ffmpeg_builder.build_identity import target_manifest_fingerprint

    manifest["materialization_id"] = target_manifest_fingerprint(manifest)
    (prefix / "target-inputs.json").write_text(json.dumps(manifest))
    prep.identity_summary["target_input_materialization_id"] = manifest["materialization_id"]
    monkeypatch.setattr(g5, "validate_target_inputs_manifest", validate_target_inputs_manifest)
    monkeypatch.setattr(g5, "_resolve_target_tools", lambda prep: SimpleNamespace(prefix=tmp_path))
    config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)
    g5.resolve_g5_environment(app, config, session)
    header.write_text("modified")
    with pytest.raises(BuildError, match="files do not match"):
        g5.resolve_g5_environment(app, config, session)


def test_readonly_resolution_rejects_runtime_or_manifest_drift(tmp_path, monkeypatch):
    app, session, prep = _readonly_resolver_fixture(tmp_path, monkeypatch)
    config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW)
    prep.target.runtime = {"device": "different"}
    with pytest.raises(BuildError, match="runtime identity"):
        g5.resolve_g5_environment(app, config, session)
    prep.target.runtime = session.identity.runtime

    def mutate(preparation):
        (tmp_path / "prefix/target-inputs.json").write_text('{"materialization_id":"changed"}')
        return SimpleNamespace(prefix=tmp_path)

    monkeypatch.setattr(g5, "_resolve_target_tools", mutate)
    with pytest.raises(BuildError, match="changed during read-only resolution"):
        g5.resolve_g5_environment(app, config, session)


def test_g4_preflight_rejects_modified_binary_and_dependency(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    prefix = tmp_path / "prefix"
    archives = tmp_path / "archives"
    for path in (workspace / "bin", prefix, archives):
        path.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (workspace / "bin" / name).write_bytes(name.encode())
    (archives / "ffmpeg.tar").write_bytes(b"source")
    (workspace / "g4-wave1-acceptance.json").write_text("{}")
    component = SimpleNamespace(
        name="ffmpeg", version="9.0", get_archive_filename=lambda: "ffmpeg.tar"
    )
    dependency = SimpleNamespace(name="zlib", version="1")
    app = SimpleNamespace(
        _get_component_selection=lambda config: SimpleNamespace(
            target_inputs=[],
            target_components_to_build=[dependency, component],
            mixed_components=[],
        ),
        state_manager=SimpleNamespace(is_component_completed=lambda name, version: True),
    )
    session = SimpleNamespace(workspace=workspace, identity=SimpleNamespace(build_key="identity"))
    environment = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix))
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW, source_archives_dir=str(archives)
    )
    monkeypatch.setattr(g5, "_accepted_names", lambda workspace: {"zlib"})
    monkeypatch.setattr(g5, "validate_prior_manifests", lambda *args: 1)
    monkeypatch.setattr(g5, "_installed_hashes", lambda prefix: {"lib": "installed"})
    monkeypatch.setattr(g5, "_interface_hashes", lambda prefix: {"interface": "hash"})
    manifest = {
        "identity_key": "identity",
        "source_sha256": g5._digest(archives / "ffmpeg.tar"),
        "binaries": {
            name: g5._digest(workspace / "bin" / name) for name in ("ffmpeg.exe", "ffprobe.exe")
        },
        "dependencies": ["zlib"],
        "installed": {"lib": "installed"},
        "interfaces": {"interface": "hash"},
    }
    (workspace / "g4-final-acceptance.json").write_text(json.dumps(manifest))
    assert g5.verify_g4(app, config, session, environment)["dependency_files_verified"] == 1
    (workspace / "bin" / "ffmpeg.exe").write_bytes(b"modified")
    with pytest.raises(BuildError, match="changed"):
        g5.verify_g4(app, config, session, environment)
    (workspace / "bin" / "ffmpeg.exe").write_bytes(b"ffmpeg.exe")
    monkeypatch.setattr(g5, "_accepted_names", lambda workspace: set())
    with pytest.raises(BuildError, match="no accepted dependency"):
        g5.verify_g4(app, config, session, environment)
