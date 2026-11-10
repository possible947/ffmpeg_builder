"""G1: Windows target probe, build/runtime identity and the reuse guard."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_identity import (
    Compatibility,
    build_native_identity,
    build_windows_identity,
    compare_identities,
    compare_saved,
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
    return build_windows_identity(config or CONFIG, state, components or {"x264": "1"})


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


# --- guard: workspace/state isolation --------------------------------------


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
