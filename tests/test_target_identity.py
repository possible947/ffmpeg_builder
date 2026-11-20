"""G1: Windows target probe, build/runtime identity and the reuse guard."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_identity import (
    Compatibility,
    MaterialBuildInputs,
    TargetIdentity,
    fingerprint,
    build_native_identity,
    build_windows_identity,
    compare_identities,
    compare_saved,
    target_manifest_fingerprint,
)
from ffmpeg_builder.state import ComponentStatus, StateManager
from ffmpeg_builder.target_guard import (
    IdentityError,
    identity_workspace,
    prepare_native_target,
    prepare_windows_target,
    recheck_runtime_identity,
)
from ffmpeg_builder.windows_probe import (
    ProbeFailure,
    WindowsInterop,
    WindowsProbeError,
    probe_windows_target,
)

OVERRIDE = {
    "mode": "full",
    "os": {
        "platform": "windows",
        "architecture": "x64",
        "version": "10.0.26100",
        "build_number": 26100,
    },
    "cpu": {
        "vendor": "GenuineIntel",
        "family": 6,
        "model": 85,
        "stepping": 4,
        "architecture": "x64",
        "isa_features": {"avx2": True, "sse4.2": True},
    },
    "gpus": [],
    "nvidia": {"state": "absent"},
    "vulkan": {"state": "absent"},
    "opencl": {"state": "absent"},
}
CONFIG = {"ffmpeg_version": "8.1", "native_build": False, "num_jobs": 8}


def _identity(override=None, config=None, components=None):
    state = probe_windows_target(copy.deepcopy(override or OVERRIDE))
    return build_windows_identity(
        config or CONFIG,
        state,
        components or {"x264": "1"},
        material_inputs=_test_material_inputs(),
    )


def _material_manifest():
    sources = {
        "nv-codec": {"version": "13.0.19.1", "sha256": "b" * 64},
        "vulkan-headers": {"version": "1.4.357.0", "sha256": "c" * 64},
        "opencl-headers": {"version": "2026.05.29", "sha256": "d" * 64},
    }
    interfaces = [
        {
            "dll": "vulkan-1.dll",
            "definition_source_component": "vulkan-headers",
            "definition_source_version": "1.4.357.0",
            "definition_source_sha256": "c" * 64,
            "definition_path": "registry/vk.xml",
            "definition_format": "vulkan_registry",
            "symbols": ["vkGetInstanceProcAddr"],
            "definition_sha256": "e" * 64,
            "recipe": "llvm-dlltool-x86_64-v1",
            "output": "lib/libvulkan.a",
            "output_sha256": "f" * 64,
        },
        {
            "dll": "OpenCL.dll",
            "definition_source_component": "opencl-icd-loader",
            "definition_source_version": "2026.05.29",
            "definition_source_sha256": "a" * 64,
            "definition_path": "loader/windows/OpenCL.def",
            "definition_format": "def",
            "symbols": ["clGetPlatformIDs"],
            "definition_sha256": "1" * 64,
            "recipe": "llvm-dlltool-x86_64-v1",
            "output": "lib/libOpenCL.a",
            "output_sha256": "2" * 64,
        },
    ]
    identity = {
        "target_backend": "linux-cross-mingw",
        "sources": sources,
        "interfaces": interfaces,
        "files": {
            "lib/libvulkan.a": "f" * 64,
            "lib/libOpenCL.a": "2" * 64,
        },
    }
    return {**identity, "materialization_id": target_manifest_fingerprint(identity)}


def _test_material_inputs():
    manifest = _material_manifest()
    return MaterialBuildInputs(
        target_backend="linux-cross-mingw",
        configured_capabilities={
            "cuda": {"effective": False},
            "vulkan": {"configured": False, "effective": False},
            "opencl": {"registry_selected": False},
            "nvidia_codec": {"registry_selected": False},
        },
        toolchains={
            "llvm_mingw": {
                "profile": "llvm-mingw",
                "version": "20261104",
                "target": {"triple": "x86_64-w64-mingw32", "runtime": "ucrt"},
                "provenance": {
                    "profile": "llvm-mingw",
                    "version": "20261104",
                    "source_sha256": "3" * 64,
                },
            },
            "rust": {
                "profile": "rust",
                "version": "1.99.0",
                "target": None,
                "provenance": {
                    "profile": "rust",
                    "version": "1.99.0",
                    "source_sha256": "4" * 64,
                },
            },
            "rust_target_std": {
                "target": "x86_64-pc-windows-gnullvm",
                "release": "1.99.0",
                "sha256": "5" * 64,
            },
            "cuda_ccbin": None,
        },
        effective_cuda=None,
        target_inputs={
            "materialization_id": manifest["materialization_id"],
            "manifest": {
                key: manifest[key] for key in ("target_backend", "sources", "interfaces", "files")
            },
            "vulkan_shader_compiler": None,
        },
    )


def _cuda_test_material_inputs():
    base = _test_material_inputs()
    toolchains = copy.deepcopy(base.toolchains)
    toolchains["cuda_ccbin"] = {
        "profile": "gcc12",
        "version": "12.5.0",
        "role_path": "bin/gcc",
        "provenance": {
            "profile": "gcc12",
            "version": "12.5.0",
            "source_sha256": "6" * 64,
        },
    }
    capabilities = copy.deepcopy(base.configured_capabilities)
    capabilities["cuda"]["effective"] = True
    capabilities["vulkan"].update(configured=True, effective=True)
    target_inputs = copy.deepcopy(base.target_inputs)
    target_inputs["vulkan_shader_compiler"] = {
        "tool": "glslc",
        "version": ["2023.8"],
    }
    return MaterialBuildInputs(
        target_backend=base.target_backend,
        configured_capabilities=capabilities,
        toolchains=toolchains,
        effective_cuda={
            "architecture": "compute_70",
            "compatibility_flags": ["-Xcompiler=-U_GNU_SOURCE"],
            "compatibility_workaround": "-Xcompiler=-U_GNU_SOURCE",
            "external_nvcc": {"toolkit_version": "12.4", "nvcc_version": "12.4.99"},
        },
        target_inputs=target_inputs,
    )


# --- probe / transport / overrides ----------------------------------------


def test_transport_uses_argv_without_shell_and_reports_missing_powershell(tmp_path):
    argv = WindowsInterop.build_command("powershell.exe", "Write-Output 'a b'")
    assert argv[0] == "powershell.exe" and "-EncodedCommand" in argv
    interop = WindowsInterop(
        executable=str(tmp_path / "nope.exe"),
        which=lambda name: None,
        fallback_executable=str(tmp_path / "also-nope.exe"),
    )
    with pytest.raises(WindowsProbeError) as exc:
        interop.resolve_executable()
    assert exc.value.kind == ProbeFailure.EXECUTABLE_NOT_FOUND


def test_full_override_needs_every_section_and_never_uses_linux_view():
    state = probe_windows_target(copy.deepcopy(OVERRIDE))
    assert state.os.build_number == 26100
    assert state.gpus == ()
    partial = {k: v for k, v in OVERRIDE.items() if k != "vulkan"}
    with pytest.raises(WindowsProbeError) as exc:
        probe_windows_target(partial)
    assert exc.value.kind == ProbeFailure.INVALID_OVERRIDE


def test_probe_failure_is_not_replaced_by_defaults():
    interop = WindowsInterop(
        executable="/nonexistent/powershell.exe",
        which=lambda name: None,
        fallback_executable="/nonexistent/other.exe",
    )
    with pytest.raises(WindowsProbeError):
        probe_windows_target(None, interop=interop)


# --- identity comparison ---------------------------------------------------


def test_identity_is_deterministic_and_roundtrips():
    a, b = _identity(), _identity()
    assert a.build_key == b.build_key
    assert compare_saved(a.to_dict(), b).result == Compatibility.COMPATIBLE
    assert compare_saved(json.loads(json.dumps(a.to_dict())), b).build_compatible


def test_runtime_gpu_change_keeps_build_identity_but_invalidates_acceptance():
    changed = copy.deepcopy(OVERRIDE)
    changed["gpus"] = [
        {
            "name": "GPU",
            "vendor_id": "10de",
            "device_id": "1d81",
            "adapter_type": "hardware",
            "pnp_device_id": "PCI\\VEN_10DE&DEV_1D81\\0",
        }
    ]
    old, new = _identity(), _identity(changed)
    result = compare_identities(old, new)
    assert result.build_compatible and not result.acceptance_valid
    assert old.build_key == new.build_key


def test_material_changes_alter_build_key():
    base = _identity()
    assert _identity(config={**CONFIG, "ffmpeg_version": "9.0"}).build_key != base.build_key
    assert _identity(components={"x264": "2"}).build_key != base.build_key
    cpu_changed = copy.deepcopy(OVERRIDE)
    cpu_changed["cpu"]["isa_features"] = {"avx2": True, "avx512f": True}
    native = {**CONFIG, "native_build": True}
    assert _identity(config=native).build_key != _identity(cpu_changed, native).build_key
    # generic builds do not depend on CPU specialisation
    assert _identity().build_key == _identity(cpu_changed).build_key


def test_missing_corrupt_and_tampered_identity_is_unknown():
    current = _identity()
    assert compare_saved(None, current).result == Compatibility.UNKNOWN
    assert compare_saved({"junk": 1}, current).result == Compatibility.UNKNOWN
    tampered = current.to_dict()
    tampered["build"]["ffmpeg_version"] = "9.0"
    assert compare_saved(tampered, current).result == Compatibility.UNKNOWN
    broken_manifest = current.to_dict()
    broken_manifest["build"]["material_inputs"]["target_inputs"]["manifest"]["files"][
        "include/vulkan/vulkan.h"
    ] = ("9" * 64)
    broken_manifest["fingerprints"]["build"] = fingerprint(broken_manifest["build"])
    assert compare_saved(broken_manifest, current).result == Compatibility.UNKNOWN
    inconsistent_provenance = current.to_dict()
    inconsistent_provenance["build"]["material_inputs"]["toolchains"]["llvm_mingw"]["provenance"][
        "version"
    ] = "20270101"
    inconsistent_provenance["fingerprints"]["build"] = fingerprint(inconsistent_provenance["build"])
    assert compare_saved(inconsistent_provenance, current).result == Compatibility.UNKNOWN


def test_g1_only_cross_identity_is_legacy_but_native_schema_one_remains_compatible():
    current = _identity()
    legacy = current.to_dict()
    legacy["schema_version"] = 1
    legacy["build"] = copy.deepcopy(legacy["build"])
    legacy["build"].pop("material_inputs")
    legacy["fingerprints"]["build"] = fingerprint(legacy["build"])
    assert compare_saved(legacy, current).result == Compatibility.UNKNOWN

    native = build_native_identity(CONFIG, "linux-native", "linux", "x86_64")
    native_legacy = native.to_dict()
    native_legacy["schema_version"] = 1
    assert compare_saved(native_legacy, native).build_compatible


def test_external_sdk_paths_are_diagnostic_and_material_input_drift_changes_key():
    base = _identity(config={**CONFIG, "external_prerequisite_paths": {"cuda": "/opt/cuda-a"}})
    moved = _identity(config={**CONFIG, "external_prerequisite_paths": {"cuda": "/opt/cuda-b"}})
    assert base.build_key == moved.build_key

    inputs = _test_material_inputs()
    rust = copy.deepcopy(inputs.toolchains)
    rust["rust_target_std"]["sha256"] = "9" * 64
    changed = MaterialBuildInputs(
        target_backend=inputs.target_backend,
        configured_capabilities=inputs.configured_capabilities,
        toolchains=rust,
        effective_cuda=inputs.effective_cuda,
        target_inputs=inputs.target_inputs,
    )
    other = build_windows_identity(
        CONFIG, probe_windows_target(copy.deepcopy(OVERRIDE)), material_inputs=changed
    )
    assert other.build_key != _identity().build_key


@pytest.mark.parametrize(
    "change",
    [
        lambda value: (
            value.toolchains["llvm_mingw"].update(version="20270101"),
            value.toolchains["llvm_mingw"]["provenance"].update(version="20270101"),
        ),
        lambda value: value.toolchains["llvm_mingw"]["provenance"].update(source_sha256="7" * 64),
        lambda value: (
            value.toolchains["rust"].update(version="2.0.0"),
            value.toolchains["rust"]["provenance"].update(version="2.0.0"),
            value.toolchains["rust_target_std"].update(release="2.0.0"),
        ),
        lambda value: value.toolchains["rust_target_std"].update(sha256="8" * 64),
        lambda value: (
            value.toolchains["cuda_ccbin"].update(profile="gcc13"),
            value.toolchains["cuda_ccbin"]["provenance"].update(profile="gcc13"),
        ),
        lambda value: value.toolchains["cuda_ccbin"].update(role_path="tool/bin/gcc"),
        lambda value: value.toolchains["cuda_ccbin"]["provenance"].update(source_sha256="7" * 64),
        lambda value: value.effective_cuda.update(architecture="compute_75"),
        lambda value: value.effective_cuda.update(compatibility_flags=[]),
        lambda value: value.effective_cuda.update(compatibility_workaround=None),
        lambda value: value.effective_cuda["external_nvcc"].update(nvcc_version="13.1.0"),
        lambda value: value.configured_capabilities["nvidia_codec"].update(registry_selected=True),
        lambda value: value.target_inputs["vulkan_shader_compiler"].update(version=["2024.1"]),
    ],
)
def test_each_material_toolchain_cuda_and_capability_change_changes_build_key(change):
    baseline = _cuda_test_material_inputs()
    altered = copy.deepcopy(baseline)
    change(altered)
    original = build_windows_identity(
        CONFIG, probe_windows_target(copy.deepcopy(OVERRIDE)), material_inputs=baseline
    )
    changed = build_windows_identity(
        CONFIG, probe_windows_target(copy.deepcopy(OVERRIDE)), material_inputs=altered
    )
    assert compare_saved(original.to_dict(), original).result == Compatibility.COMPATIBLE
    assert original.build_key != changed.build_key


def test_material_target_drift_isolated_and_diagnostics_do_not_block_resume(tmp_path):
    baseline = _identity()
    old_workspace = _seed(tmp_path, baseline)

    diagnostic_change = baseline.with_runtime(
        baseline.runtime,
        {**baseline.diagnostics, "diagnostic_probe_dump": {"detail": "changed"}},
    )
    reused = prepare_windows_target(tmp_path, diagnostic_change, resume=True)
    assert reused.workspace == old_workspace and reused.resumed
    assert reused.comparison.result == Compatibility.DIAGNOSTIC_ONLY

    changed_build = copy.deepcopy(baseline.build)
    changed_build["material_inputs"]["target_inputs"]["manifest"]["sources"]["nv-codec"][
        "sha256"
    ] = ("8" * 64)
    changed_manifest = changed_build["material_inputs"]["target_inputs"]["manifest"]
    changed_build["material_inputs"]["target_inputs"]["materialization_id"] = (
        target_manifest_fingerprint(
            {
                key: changed_manifest[key]
                for key in ("target_backend", "sources", "interfaces", "files")
            }
        )
    )
    changed_identity = TargetIdentity(
        backend=baseline.backend,
        build=changed_build,
        runtime=baseline.runtime,
        diagnostics=baseline.diagnostics,
    )
    isolated = prepare_windows_target(tmp_path, changed_identity, resume=True)
    assert isolated.workspace != old_workspace and not isolated.resumed
    assert (old_workspace / "bin" / "ffmpeg.exe").exists()

    changed_interface_build = copy.deepcopy(baseline.build)
    manifest = changed_interface_build["material_inputs"]["target_inputs"]["manifest"]
    manifest["interfaces"][0]["recipe"] = "llvm-dlltool-x86_64-v2"
    changed_interface_build["material_inputs"]["target_inputs"]["materialization_id"] = (
        target_manifest_fingerprint(
            {key: manifest[key] for key in ("target_backend", "sources", "interfaces", "files")}
        )
    )
    changed_interface = TargetIdentity(
        backend=baseline.backend,
        build=changed_interface_build,
        runtime=baseline.runtime,
        diagnostics=baseline.diagnostics,
    )
    interface_isolated = prepare_windows_target(tmp_path, changed_interface, resume=True)
    assert interface_isolated.workspace != old_workspace and not interface_isolated.resumed


# --- guard: workspace/state isolation --------------------------------------


def test_invalid_current_identity_does_not_create_workspace(tmp_path):
    baseline = _identity()
    incomplete_build = copy.deepcopy(baseline.build)
    incomplete_build.pop("material_inputs")
    incomplete = TargetIdentity(
        backend=baseline.backend,
        build=incomplete_build,
        runtime=baseline.runtime,
        diagnostics=baseline.diagnostics,
    )
    workspace = identity_workspace(tmp_path, incomplete)

    with pytest.raises(IdentityError) as exc:
        prepare_windows_target(tmp_path, incomplete, resume=False)

    assert exc.value.kind == "invalid_current_identity"
    assert not workspace.exists()


def _seed(base: Path, identity) -> Path:
    session = prepare_windows_target(base, identity, resume=False)
    manager = StateManager(session.workspace / "build_state.json")
    manager.get()
    manager.mark_component_status("x264", ComponentStatus.COMPLETED, "1")
    session.stamp(manager)
    (session.workspace / "bin").mkdir()
    (session.workspace / "bin" / "ffmpeg.exe").write_text("x")
    return session.workspace


def test_same_identity_resumes_and_material_change_gets_isolated_workspace(tmp_path):
    first = _identity()
    ws = _seed(tmp_path, first)
    again = prepare_windows_target(tmp_path, _identity(), resume=True)
    assert again.workspace == ws and again.resumed and not again.acceptance_invalidated

    other = _identity(components={"x264": "2"})
    session = prepare_windows_target(tmp_path, other, resume=True)
    assert session.workspace != ws and not session.resumed
    assert (ws / "bin" / "ffmpeg.exe").exists()  # old artifacts untouched


def test_runtime_drift_on_resume_reuses_artifacts_but_invalidates_acceptance(tmp_path):
    ws = _seed(tmp_path, _identity())
    changed = copy.deepcopy(OVERRIDE)
    changed["os"]["build_number"] = 26200
    session = prepare_windows_target(tmp_path, _identity(changed), resume=True)
    assert session.workspace == ws and session.acceptance_invalidated
    manager = StateManager(ws / "build_state.json")
    manager.load()
    session.stamp(manager)
    assert manager.get().acceptance["status"] == "invalidated"


def test_stale_artifacts_without_marker_are_refused_untouched(tmp_path):
    identity = _identity()
    ws = identity_workspace(tmp_path, identity)
    ws.mkdir(parents=True)
    (ws / "lib.a").write_text("stale")
    with pytest.raises(IdentityError) as exc:
        prepare_windows_target(tmp_path, identity, resume=False)
    assert exc.value.kind == "unsafe_stale_artifacts"
    assert (ws / "lib.a").read_text() == "stale"


@pytest.mark.parametrize("content,kind", [("{not json", "corrupt_state"), ("[]", "corrupt_state")])
def test_corrupt_state_is_not_a_fresh_state(tmp_path, content, kind):
    ws = _seed(tmp_path, _identity())
    state_file = ws / "build_state.json"
    state_file.write_text(content)
    with pytest.raises(IdentityError) as exc:
        prepare_windows_target(tmp_path, _identity(), resume=True)
    assert exc.value.kind == kind
    assert state_file.read_text() == content


def test_legacy_state_without_identity_is_rejected_and_preserved(tmp_path):
    ws = _seed(tmp_path, _identity())
    state_file = ws / "build_state.json"
    data = json.loads(state_file.read_text())
    data.pop("identity")
    state_file.write_text(json.dumps(data))
    before = state_file.read_text()
    with pytest.raises(IdentityError) as exc:
        prepare_windows_target(tmp_path, _identity(), resume=True)
    assert exc.value.kind == "legacy_state"
    assert state_file.read_text() == before


def test_base_level_legacy_state_is_reported_not_used(tmp_path):
    (tmp_path / "build_state.json").write_text("{}")
    session = prepare_windows_target(tmp_path, _identity(), resume=False)
    assert session.workspace != tmp_path
    assert any("legacy" in note for note in session.notes)
    assert (tmp_path / "build_state.json").read_text() == "{}"


def test_state_manager_refuses_unverified_trust_when_required(tmp_path):
    manager = StateManager(tmp_path / "s.json")
    manager.require_verified_identity = True
    with pytest.raises(Exception):
        manager.get_resume_point()
    with pytest.raises(Exception):
        manager.is_component_completed("x264")
    manager.identity_verified = True
    assert manager.get_resume_point() == "x264" or manager.get_resume_point() is None


def test_recheck_classifies_runtime_and_build_drift():
    base = _identity()
    same = recheck_runtime_identity(base, _identity())
    assert same.acceptance_valid and not same.build_unsafe
    changed = copy.deepcopy(OVERRIDE)
    changed["os"]["build_number"] = 26200
    drift = recheck_runtime_identity(base, _identity(changed))
    assert not drift.acceptance_valid and not drift.build_unsafe
    changed["os"]["architecture"] = "arm64"
    assert recheck_runtime_identity(base, _identity(changed)).build_unsafe


# --- native flows stay lenient ---------------------------------------------


def test_native_adopts_legacy_state_and_refuses_mismatched_identity(tmp_path):
    manager = StateManager(tmp_path / "build_state.json")
    manager.get()
    manager.save()
    ident = build_native_identity(CONFIG, "linux-native", "linux", "x86_64")
    session = prepare_native_target(tmp_path, manager, ident, resume=True)
    assert session.workspace == tmp_path and session.resumed

    session.stamp(manager)
    other = build_native_identity(
        {**CONFIG, "ffmpeg_version": "9.0"}, "linux-native", "linux", "x86_64"
    )
    with pytest.raises(IdentityError):
        prepare_native_target(tmp_path, manager, other, resume=True)
    # a new build is never blocked
    assert prepare_native_target(tmp_path, manager, other, resume=False).workspace == tmp_path


# --- entry points ----------------------------------------------------------


def test_config_validates_target_backend():
    from ffmpeg_builder.config import BuildConfig

    assert BuildConfig().target_backend == "native"
    assert BuildConfig(target_backend="linux-cross-mingw").target_backend == "linux-cross-mingw"
    with pytest.raises(ValueError):
        BuildConfig(target_backend="msys2")


def test_headless_and_tui_entry_points_are_blocked_by_guard(tmp_path, monkeypatch, capsys):
    import importlib.util

    import ffmpeg_builder.app as app_module
    from ffmpeg_builder.config import BuildConfig

    root = Path(app_module.__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("run_build_g1", root / "scripts" / "run_build.py")
    run_build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_build)

    identity = _identity()
    ws = identity_workspace(tmp_path, identity)
    ws.mkdir(parents=True)
    (ws / "stale.o").write_text("x")

    fake = SimpleNamespace(
        base_workspace=tmp_path,
        _get_buildable_components=lambda config: [],
        state_manager=StateManager(tmp_path / "build_state.json"),
    )

    def prepare(config, resume):
        return prepare_windows_target(tmp_path, identity, resume)

    fake.prepare_target = prepare
    config = BuildConfig(target_backend="linux-cross-mingw")
    assert run_build.run_build(fake, config, resume=False, no_release=True) == 2
    assert "identity guard" in capsys.readouterr().out
    assert not (tmp_path / "build_state.json").exists()
    assert (ws / "stale.o").exists()

    # TUI: the guard error is printed, the build is not started and nothing is reset.
    printed = []
    tui = SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **k: printed.append(str(a[0]) if a else "")),
        _get_buildable_components=lambda config: [],
        prepare_target=prepare,
        state_manager=SimpleNamespace(reset=lambda: pytest.fail("reset before guard")),
    )
    app_module.FFmpegBuilderApp._run_build(tui, config, resume=False)
    assert any("identity guard" in line for line in printed)


def test_cuda_preparation_reuses_single_g1_probe_for_ptx_validation(tmp_path, monkeypatch):
    import ffmpeg_builder.app as app_module
    from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
    from ffmpeg_builder.cuda_prerequisites import ResolvedCudaBuildHostInputs
    from ffmpeg_builder.platform_detect import PlatformInfo
    from dataclasses import replace

    override = copy.deepcopy(OVERRIDE)
    override["nvidia"] = {
        "state": "present",
        "devices": [
            {
                "name": "NVIDIA TITAN V",
                "uuid": "GPU-test",
                "driver_version": "test",
                "compute_capability": "7.0",
            }
        ],
    }
    target_state = probe_windows_target(override)
    prefix = tmp_path / "toolchains" / "gcc12"
    inputs = ResolvedCudaBuildHostInputs(
        cuda_root=tmp_path / "cuda",
        nvcc_path=tmp_path / "cuda" / "bin" / "nvcc",
        detected_cuda_version="12.4",
        ccbin_path=prefix / "bin" / "gcc",
        ccbin_profile="gcc12",
        ccbin_profile_version="12.5.0",
        ccbin_provenance={"prefix": str(prefix)},
        resolution_source="CUDA_HOME",
        diagnostics=(),
    )

    class _Detector:
        def __init__(self):
            self.platform_info = PlatformInfo(
                is_linux=True,
                build_backend=TARGET_BACKEND_CROSS_MINGW,
                cuda_available=True,
                cuda_build_host_inputs=inputs,
            )

        def get_build_backend_name(self):
            return TARGET_BACKEND_CROSS_MINGW

    app = app_module.FFmpegBuilderApp.__new__(app_module.FFmpegBuilderApp)
    app.base_workspace = tmp_path
    app.platform_detector = _Detector()
    app.system_report = SimpleNamespace(wsl_mingw_preparation=None)
    app.windows_target_state = None
    probe_calls = []

    def windows_probe(override_arg, *, timeout, **kwargs):
        probe_calls.append((override_arg, timeout))
        return target_state

    app.windows_probe = windows_probe
    app._get_buildable_components = lambda config: []
    validated_states = []

    def validate(resolved, state):
        assert resolved is inputs
        validated_states.append(state)
        return replace(
            resolved,
            compute_architecture="compute_70",
            probe_status="passed",
            effective_flags=("--ptx", "--gpu-architecture=compute_70"),
        )

    monkeypatch.setattr(app_module, "validate_cuda_ptx_compatibility", validate)
    monkeypatch.setattr(
        app_module,
        "resolve_wsl_mingw_cuda_inputs",
        lambda *args, **kwargs: inputs,
    )
    monkeypatch.setattr(
        app_module,
        "resolve_wsl_mingw_project_toolchains",
        lambda *args, **kwargs: {
            name: {
                "version": "test",
                "prefix": str(tmp_path / name),
                "paths": {},
                "target": None,
                "extras": [],
                "provenance": {},
            }
            for name in ("llvm-mingw", "rust")
        },
    )
    monkeypatch.setattr(
        app,
        "_materialize_target_input_manifest",
        lambda *args: _material_manifest(),
    )
    monkeypatch.setattr(
        app_module,
        "project_material_build_inputs",
        lambda *args: _test_material_inputs(),
    )
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        enable_libvmaf=True,
        enable_libvmaf_cuda=True,
    )

    session = app.prepare_target(config, resume=False)

    assert len(probe_calls) == 1
    assert validated_states == [target_state]
    assert app.platform_detector.platform_info.cuda_build_host_inputs.probe_status == "passed"
    assert app.platform_detector.platform_info.cuda_nvcc_supported is True
    assert app.platform_detector.platform_info.libvmaf_cuda_supported is True
    assert app.wsl_mingw_preparation.cuda.compute_architecture == "compute_70"
    assert app.wsl_mingw_preparation.effective_build_inputs["cuda"]["compute_architecture"] == (
        "compute_70"
    )
    assert session.identity.backend == TARGET_BACKEND_CROSS_MINGW


def test_required_cuda_requires_nonfree_permission_before_preparation():
    import ffmpeg_builder.app as app_module
    from ffmpeg_builder.build_types import BuildError
    from ffmpeg_builder.config import BuildConfig

    app = app_module.FFmpegBuilderApp.__new__(app_module.FFmpegBuilderApp)
    with pytest.raises(BuildError, match="allow_nonfree_cuda"):
        app.prepare_target(BuildConfig(cuda_build_mode="required"), resume=False)


def test_native_cuda_auto_and_required_handle_missing_toolkit(tmp_path, monkeypatch):
    import ffmpeg_builder.app as app_module
    from ffmpeg_builder.build_types import BuildError
    from ffmpeg_builder.config import BuildConfig
    from ffmpeg_builder.platform_detect import PlatformInfo

    for name in ("CUDA_HOME", "CUDA_PATH", "CUDA_NVCC_CCBIN"):
        monkeypatch.delenv(name, raising=False)
    app = app_module.FFmpegBuilderApp.__new__(app_module.FFmpegBuilderApp)
    app.platform_detector = SimpleNamespace(platform_info=PlatformInfo(is_linux=True))
    session = SimpleNamespace(notes=[])
    app._prepare_native_cuda(BuildConfig(), session)
    assert not app.platform_detector.platform_info.libvmaf_cuda_supported
    assert "Toolkit not found" in session.notes[0]
    with pytest.raises(BuildError, match="Toolkit not found"):
        app._prepare_native_cuda(BuildConfig(cuda_build_mode="required"), SimpleNamespace(notes=[]))


def test_native_cuda_falls_back_to_probed_system_compiler_when_mapping_missing(
    tmp_path, monkeypatch
):
    import ffmpeg_builder.app as app_module
    from ffmpeg_builder.build_types import BuildError
    from ffmpeg_builder.config import BuildConfig
    from ffmpeg_builder.cuda_prerequisites import ResolvedCudaBuildHostInputs
    from ffmpeg_builder.platform_detect import PlatformInfo

    monkeypatch.delenv("CUDA_NVCC_CCBIN", raising=False)
    inputs = ResolvedCudaBuildHostInputs(
        cuda_root=tmp_path / "cuda",
        nvcc_path=tmp_path / "cuda/bin/nvcc",
        detected_cuda_version="14.0",
        ccbin_path=tmp_path / "system/gcc",
        ccbin_profile="external",
        ccbin_profile_version="",
        ccbin_provenance={"prefix": str(tmp_path / "system"), "source": "system gcc"},
        resolution_source="config",
        diagnostics=(),
        compute_architecture="compute_70",
        probe_status="passed",
    )
    attempts = []

    def resolve(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise BuildError("CUDA", "no mapping", code="cuda_host_mapping_missing")
        return inputs

    monkeypatch.setattr(app_module, "ToolchainRegistry", lambda: None)
    monkeypatch.setattr(app_module, "ToolchainStore", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.shutil, "which", lambda name: str(inputs.ccbin_path))
    monkeypatch.setattr(app_module, "resolve_wsl_mingw_cuda_inputs", resolve)
    monkeypatch.setattr(app_module, "validate_cuda_ptx_compatibility", lambda value, *a, **k: value)
    monkeypatch.setattr(app_module, "validate_cuda_output_modes", lambda value: value)
    app = app_module.FFmpegBuilderApp.__new__(app_module.FFmpegBuilderApp)
    app.platform_detector = SimpleNamespace(platform_info=PlatformInfo(is_linux=True))
    app.workspace = tmp_path
    session = SimpleNamespace(notes=[])
    config = BuildConfig(
        enable_libvmaf=False,
        cuda_compute_capability="70",
        external_prerequisite_paths={"cuda": str(inputs.cuda_root)},
    )
    app._prepare_native_cuda(config, session)
    assert len(attempts) == 2
    assert attempts[1]["environment"]["CUDA_NVCC_CCBIN"] == str(inputs.ccbin_path)
    assert attempts[1]["host_override_source"] == "system gcc"
    assert app.platform_detector.platform_info.cuda_build_host_inputs is inputs


def test_vulkan_preparation_keeps_host_sdk_separate_from_windows_runtime(tmp_path, monkeypatch):
    import ffmpeg_builder.app as app_module
    from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
    from ffmpeg_builder.platform_detect import PlatformInfo
    from ffmpeg_builder.vulkan_prerequisites import (
        CAPABILITY_SHADER_COMPILATION,
        CAPABILITY_SPIRV_VALIDATION,
        ResolvedVulkanSdk,
    )

    target_state = probe_windows_target(copy.deepcopy(OVERRIDE))
    sdk = ResolvedVulkanSdk(
        selected_root=tmp_path / "vulkan-sdk",
        effective_sdk_root=tmp_path / "vulkan-sdk" / "x86_64",
        resolution_source="VULKAN_SDK",
        shader_compiler_path=tmp_path / "vulkan-sdk" / "x86_64" / "bin" / "glslc",
        spirv_validator_path=tmp_path / "vulkan-sdk" / "x86_64" / "bin" / "spirv-val",
        spirv_optimizer_path=None,
        validated_capabilities=frozenset(
            {CAPABILITY_SHADER_COMPILATION, CAPABILITY_SPIRV_VALIDATION}
        ),
        tool_diagnostics={},
        diagnostics=(),
        diagnostic_metadata={},
    )
    app = app_module.FFmpegBuilderApp.__new__(app_module.FFmpegBuilderApp)
    app.base_workspace = tmp_path
    app.platform_detector = SimpleNamespace(
        platform_info=PlatformInfo(is_linux=True, build_backend=TARGET_BACKEND_CROSS_MINGW),
        get_build_backend_name=lambda: TARGET_BACKEND_CROSS_MINGW,
    )
    app.system_report = SimpleNamespace(wsl_mingw_preparation=None)
    app.windows_target_state = None
    app.windows_probe = lambda override, *, timeout, **kwargs: target_state
    app._get_buildable_components = lambda config: []
    monkeypatch.setattr(
        app_module,
        "resolve_wsl_mingw_vulkan_sdk",
        lambda config, **kwargs: sdk,
    )
    monkeypatch.setattr(
        app_module,
        "resolve_wsl_mingw_project_toolchains",
        lambda *args, **kwargs: {
            name: {
                "version": "test",
                "prefix": str(tmp_path / name),
                "paths": {},
                "target": None,
                "extras": [],
                "provenance": {},
            }
            for name in ("llvm-mingw", "rust")
        },
    )
    monkeypatch.setattr(
        app,
        "_materialize_target_input_manifest",
        lambda *args: _material_manifest(),
    )
    monkeypatch.setattr(
        app_module,
        "project_material_build_inputs",
        lambda *args: _test_material_inputs(),
    )
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        enable_libvmaf_cuda=False,
        enable_libplacebo_vulkan=True,
    )

    app.prepare_target(config, resume=False)

    preparation = app.wsl_mingw_preparation
    assert preparation.vulkan is sdk
    assert preparation.target is target_state
    assert preparation.effective_build_inputs["vulkan"]["validated_capabilities"] == [
        CAPABILITY_SHADER_COMPILATION,
        CAPABILITY_SPIRV_VALIDATION,
    ]
    assert preparation.diagnostic_only["vulkan"]["resolution_source"] == "VULKAN_SDK"
    json.dumps(preparation.to_dict())
    assert app.platform_detector.platform_info.vulkan_runtime_available is False
    assert preparation.to_dict()["target"]["vulkan"]["state"] == "absent"
