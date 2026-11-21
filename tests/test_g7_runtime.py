import json
from types import SimpleNamespace

import pytest

from ffmpeg_builder import g5, g6
from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.windows_probe import FeatureState


def _config(*, hardware=False):
    return BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        ffmpeg_version="9.0",
        gpl_enabled=True,
        enable_hardware_acceleration=hardware,
        enable_libplacebo_vulkan=hardware,
    )


def _frames(width, height, count):
    return f"#dimensions 0: {width}x{height}\n" + "\n".join(
        f"0, {index}, {index}, 1, {width * height * 3 // 2}, " "0123456789abcdef0123456789abcdef"
        for index in range(count)
    )


def _runner(monkeypatch, *, score="100.0"):
    calls = []

    def run(stage, label, arguments, interop, **kwargs):
        calls.append((label, arguments, kwargs))
        outputs = {
            "ffmpeg-version": "ffmpeg version 9.0 Copyright",
            "ffprobe-version": "ffprobe version 9.0 Copyright",
            "software-probe": "h264\n",
            "software-decode": _frames(128, 96, 8),
            "scale-cuda": _frames(160, 90, 4),
        }
        return {
            "returncode": 0,
            "stdout": outputs.get(label, ""),
            "stderr": f"scale_cuda libvmaf_cuda VMAF score: {score}",
        }

    monkeypatch.setattr(g5, "run_windows_ffmpeg", run)
    return calls


def _hardware_target():
    return SimpleNamespace(
        nvidia=SimpleNamespace(
            state=FeatureState.PRESENT,
            devices=[
                SimpleNamespace(
                    name="GPU",
                    uuid="uuid",
                    pci_bus_id="pci",
                    gpu_pnp_device_id="PCI\\GPU",
                )
            ],
        ),
        gpus=[SimpleNamespace(adapter_type="hardware", vendor_id="10de", pnp_device_id="PCI\\GPU")],
    )


def _graphics(monkeypatch, *, status="PASS"):
    monkeypatch.setattr(
        g5,
        "pending_features",
        lambda *args: [
            {"technology": technology, "feature": feature, "status": "UNVALIDATED"}
            for technology, feature in (
                ("vulkan", "scale_vulkan"),
                ("vulkan", "libplacebo"),
                ("opencl", "avgblur_opencl"),
                ("nvidia", "h264_nvenc"),
                ("vulkan", "vulkan_h264_decode"),
            )
        ],
    )

    def graphics(target, features, stage, interop, **kwargs):
        assert kwargs["runner"]
        for feature in features:
            if feature["technology"] != "software":
                feature["status"] = status
        return {}

    monkeypatch.setattr(g5, "graphics_runtime", graphics)


def test_base_acceptance_never_queries_or_runs_hardware(tmp_path, monkeypatch):
    calls = _runner(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("BASE must not inspect or run GPU acceptance")

    monkeypatch.setattr(g5, "pending_features", forbidden)
    monkeypatch.setattr(g5, "graphics_runtime", forbidden)
    monkeypatch.setattr(g5, "_nvidia_device", forbidden)
    result = g5.profile_runtime(_config(), None, tmp_path / "bundle", tmp_path / "work", object())
    assert result["status"] == "PASS"
    assert result["hardware_enabled"] is False
    assert [feature["feature"] for feature in result["features"]] == ["libx264_roundtrip"]
    assert len(calls) == 5
    assert all(call[2]["controlled_path"] for call in calls)
    assert all(call[2]["work"] == tmp_path / "work" for call in calls)
    assert calls[1][2]["executable"] == "ffprobe.exe"


def test_preferred_uses_representative_filters_not_full_g5_matrix(tmp_path, monkeypatch):
    calls = _runner(monkeypatch)
    _graphics(monkeypatch)
    result = g5.profile_runtime(
        _config(hardware=True), _hardware_target(), tmp_path, tmp_path / "work", object()
    )
    assert {feature["feature"] for feature in result["features"]} == {
        "libx264_roundtrip",
        "scale_cuda",
        "libvmaf_cuda",
        "scale_vulkan",
        "libplacebo",
        "avgblur_opencl",
    }
    assert [call[0] for call in calls][-2:] == ["scale-cuda", "libvmaf-cuda"]
    assert result["hardware_device"] == "GPU"


def test_effective_config_disables_optional_cuda_and_placebo_smokes(tmp_path, monkeypatch):
    calls = _runner(monkeypatch)
    _graphics(monkeypatch)
    config = _config(hardware=True)
    config.cuda_build_mode = "disabled"
    config.enable_libvmaf_cuda = False
    config.enable_libplacebo_vulkan = False
    result = g5.profile_runtime(config, None, tmp_path, tmp_path / "work", object())
    assert {feature["feature"] for feature in result["features"]} == {
        "libx264_roundtrip",
        "scale_vulkan",
        "avgblur_opencl",
    }
    assert len(calls) == 5


@pytest.mark.parametrize("failure", ["graphics", "vmaf", "mapping"])
def test_preferred_rejects_unaccepted_hardware(tmp_path, monkeypatch, failure):
    _runner(monkeypatch, score="nan" if failure == "vmaf" else "100")
    _graphics(monkeypatch, status="FAIL" if failure == "graphics" else "PASS")
    target = _hardware_target()
    if failure == "mapping":
        target.nvidia.devices[0].gpu_pnp_device_id = "PCI\\OTHER"
    with pytest.raises((BuildError, ValueError)):
        g5.profile_runtime(_config(hardware=True), target, tmp_path, tmp_path / "work", object())


def test_software_requires_decoded_frames_not_only_successful_processes(tmp_path, monkeypatch):
    _runner(monkeypatch)
    original = g5.run_windows_ffmpeg

    def truncated(stage, label, arguments, interop, **kwargs):
        result = original(stage, label, arguments, interop, **kwargs)
        if label == "software-decode":
            result["stdout"] = _frames(128, 96, 7)
        return result

    monkeypatch.setattr(g5, "run_windows_ffmpeg", truncated)
    with pytest.raises(ValueError, match="Expected 8"):
        g5.profile_runtime(_config(), None, tmp_path, tmp_path / "work", object())


def test_controlled_runtime_launch_keeps_work_and_logs_outside_bundle(tmp_path, monkeypatch):
    scripts = []
    monkeypatch.setattr(g5, "_windows_path", lambda path: "C:\\" + path.name)

    def powershell(script, **kwargs):
        scripts.append(script)
        return SimpleNamespace(returncode=0, stderr=b"")

    work = tmp_path / "work"
    work.mkdir()
    g5.run_windows_ffmpeg(
        tmp_path / "bundle",
        "probe",
        ["-version"],
        SimpleNamespace(run_powershell=powershell),
        executable="ffprobe.exe",
        work=work,
        controlled_path=True,
    )
    script = scripts[0]
    assert "$env:PATH='C:\\bundle'+';'+$env:SystemRoot+'\\System32'" in script
    assert "-FilePath 'C:\\bundle\\ffprobe.exe'" in script
    assert "-WorkingDirectory 'C:\\work'" in script
    assert "-RedirectStandardOutput 'C:\\work\\probe.stdout'" in script
    assert "-RedirectStandardError 'C:\\work\\probe.stderr'" in script


def test_production_manifest_verification_does_not_require_historical_waves(tmp_path, monkeypatch):
    config = _config()
    config.source_archives_dir = str(tmp_path / "sources")
    source = tmp_path / "sources" / "ffmpeg.tar"
    source.parent.mkdir()
    source.write_bytes(b"source")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (binaries / name).write_bytes(name.encode())
    component = SimpleNamespace(
        name="ffmpeg", version="9.0", get_archive_filename=lambda: "ffmpeg.tar"
    )
    app = SimpleNamespace(
        _get_component_selection=lambda config: SimpleNamespace(
            target_inputs=[], target_components_to_build=[component], mixed_components=[]
        ),
        state_manager=SimpleNamespace(is_component_completed=lambda *args: True),
    )
    session = SimpleNamespace(workspace=tmp_path, identity=SimpleNamespace(build_key="identity"))
    environment = SimpleNamespace(paths=SimpleNamespace(target_prefix=tmp_path / "prefix"))
    monkeypatch.setattr(g5, "_installed_hashes", lambda prefix: {"lib.a": "accepted"})
    monkeypatch.setattr(g5, "_interface_hashes", lambda prefix: {})
    manifest = {
        "identity_key": "identity",
        "source_sha256": g5._digest(source),
        "binaries": {
            name: g5._digest(path)
            for name, path in (
                ("ffmpeg.exe", binaries / "ffmpeg.exe"),
                ("ffprobe.exe", binaries / "ffprobe.exe"),
            )
        },
        "dependencies": [],
        "installed": {"lib.a": "accepted"},
        "interfaces": {},
    }
    path = tmp_path / "production-final.json"
    path.write_text(json.dumps(manifest))
    fingerprint, accepted = g5.verify_production_outputs(app, config, session, environment, path)
    assert accepted == manifest
    assert fingerprint["manifests"] == {path.name: g5._digest(path)}
    (binaries / "ffmpeg.exe").write_bytes(b"modified")
    with pytest.raises(BuildError, match="changed"):
        g5.verify_production_outputs(app, config, session, environment, path)


@pytest.mark.parametrize("package", [True, False])
@pytest.mark.parametrize("hardware", [True, False])
def test_normal_package_reuses_g6_without_requiring_historical_g5(
    tmp_path, monkeypatch, package, hardware
):
    target = SimpleNamespace(uses_override=False)
    session = SimpleNamespace(
        workspace=tmp_path,
        identity=SimpleNamespace(
            build_key="identity", runtime={}, to_dict=lambda: {"build": "identity"}
        ),
    )
    environment = object()
    app = SimpleNamespace(
        workspace=tmp_path,
        packages=tmp_path,
        state_manager=object(),
        platform_detector=object(),
        windows_target_state=target,
        _probe_windows_target=lambda config: target,
        _component_toolchain_env=lambda *args: {},
        _get_component_selection=lambda config: SimpleNamespace(
            target_inputs=[],
            mixed_components=[],
            target_components_to_build=[SimpleNamespace(name="ffmpeg")],
        ),
    )
    fingerprint = {"manifests": {"normal-final.json": "hash"}}
    manifest = {"dependencies": [], "runtime": {"imports": []}}
    monkeypatch.setattr(g6, "resolve_g5_environment", lambda *args: environment)
    monkeypatch.setattr(g6, "runtime_identity", lambda target: {})
    monkeypatch.setattr(g6, "verify_production_outputs", lambda *args: (fingerprint, manifest))

    def forbidden(*args):
        pytest.fail("Normal acceptance must not depend on the full historical G5 matrix")

    monkeypatch.setattr(g6, "_verified_g5", forbidden)
    monkeypatch.setattr(g6, "verify_g4", forbidden)
    monkeypatch.setattr(g6, "verify_stage", lambda *args: None)

    def stage(workspace, environment, manifest, directory, interop):
        directory.mkdir(parents=True)
        return {"toolchain_dlls": {}, "system_imports": [], "binaries": {}}

    monkeypatch.setattr(g6, "stage_g4", stage)
    runtime_configs = []

    def runtime(config, *args):
        runtime_configs.append(config)
        return {
            "status": "PASS",
            "features": [
                {"technology": "software", "feature": "libx264_roundtrip", "status": "PASS"}
            ],
        }

    monkeypatch.setattr(g6, "profile_runtime", runtime)
    relocated = []

    def smoke(bundle, work, expected_device, **kwargs):
        relocated.append((bundle, kwargs))
        return {"status": "PASS", "hardware_enabled": hardware}

    monkeypatch.setattr(g6, "_run_relocated_smoke", smoke)

    class Builder:
        def __init__(self, *args, **kwargs):
            assert package, "Runtime-only acceptance must not initialize packaging"

        def make_release_bundle(self, policy):
            release = tmp_path / "release"
            release.mkdir(exist_ok=True)
            (release / "ffmpeg.exe").write_bytes(b"exe")
            (release / "manifest.json").write_text(json.dumps({"generated_at": "now"}))
            return release

        def shutdown_downloads(self, **kwargs):
            pass

    monkeypatch.setattr(g6, "FFmpegBuilder", Builder)
    config = _config(hardware=hardware)
    assert g6.run_g7_package(app, config, session, environment, package=package) == 0
    assert runtime_configs == [config]
    assert json.loads((tmp_path / "g7-runtime-acceptance.json").read_text())["status"] == "PASS"
    if not package:
        assert not relocated
        assert not (tmp_path / "release").exists()
        assert not (tmp_path / "g7-package-acceptance.json").exists()
        return
    report = json.loads((tmp_path / "g7-package-acceptance.json").read_text())
    assert report["status"] == "PASS"
    assert report["hardware_enabled"] is hardware
    assert report["repeat_package_identical"] is True
    assert report["runtime_evidence"].endswith("g7-runtime-acceptance.json")
    assert relocated[0][1]["config"].enable_hardware_acceleration is hardware
    assert not (tmp_path / "g6-acceptance.json").exists()
    assert not (tmp_path / "g7-acceptance.json").exists()


def test_failed_normal_acceptance_does_not_leave_stale_package_pass(tmp_path, monkeypatch):
    session = SimpleNamespace(workspace=tmp_path, identity=SimpleNamespace(build_key="identity"))
    (tmp_path / "g7-package-acceptance.json").write_text('{"status": "PASS"}')
    aggregate = tmp_path / "g7-acceptance.json"
    aggregate.write_text('{"status": "IN_PROGRESS"}')
    monkeypatch.setattr(g6, "resolve_g5_environment", lambda *args: object())

    def changed(*args):
        raise BuildError("G7", "Production outputs changed")

    monkeypatch.setattr(g6, "verify_production_outputs", changed)
    assert (
        g6.run_g6(
            object(),
            _config(),
            production_evidence=tmp_path / "normal-final.json",
            session=session,
            environment=object(),
        )
        == 1
    )
    report = json.loads((tmp_path / "g7-package-acceptance.json").read_text())
    assert report["status"] == "FAIL"
    assert "outputs changed" in report["error"]
    assert aggregate.read_text() == '{"status": "IN_PROGRESS"}'


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([], set()),
        (["--enable-nvenc"], {"nvEncodeAPI64.dll"}),
        (["--enable-cuvid"], {"nvcuvid.dll"}),
        (["--enable-nvenc", "--enable-cuvid"], {"nvEncodeAPI64.dll", "nvcuvid.dll"}),
    ],
)
def test_runtime_prerequisites_include_configured_unprobed_nvidia_codecs(flags, expected):
    evidence = {
        "stage": {"toolchain_dlls": {}},
        "features": [
            {"technology": "nvidia", "feature": "scale_cuda", "status": "PASS"},
            {"technology": "nvidia", "feature": "libvmaf_cuda", "status": "PASS"},
        ],
    }
    requirements = g6._external_runtime_prerequisites(
        object(), {"dependencies": [], "configure_flags": flags}, evidence
    )
    assert {item["name"] for item in requirements} == expected


def test_runtime_prerequisites_preserve_historical_g5_codec_declarations():
    evidence = {
        "stage": {"toolchain_dlls": {}},
        "features": [
            {"feature": "h264_nvenc", "status": "PASS"},
            {"feature": "hevc_cuvid_decode", "status": "PASS"},
        ],
    }
    requirements = g6._external_runtime_prerequisites(object(), {"dependencies": []}, evidence)
    assert {item["name"] for item in requirements} == {"nvEncodeAPI64.dll", "nvcuvid.dll"}
