"""Tests for WSL-Mingw CUDA SDK and NVCC host-compiler resolution."""

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import (
    CATEGORY_CUDA_COMPATIBILITY,
    CATEGORY_EXTERNAL_PREREQUISITE,
    CATEGORY_TOOLCHAIN_INSTALL,
    CATEGORY_TARGET_DATA,
    BuildError,
)
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.cuda_prerequisites import (
    ResolvedCudaBuildHostInputs,
    cuda_profile_identity,
    invalidate_cuda_resume,
    resolve_wsl_mingw_cuda_inputs,
    validate_cuda_output_modes,
    validate_cuda_ptx_compatibility,
    validate_required_cuda_components,
)
from ffmpeg_builder.external_prerequisites import (
    CODE_PREREQUISITE_INVALID_PATH,
    CODE_PREREQUISITE_MISSING,
    CODE_PREREQUISITE_VALIDATION_FAILED,
)
from ffmpeg_builder.toolchain_registry import ToolchainRegistry
from ffmpeg_builder.windows_probe import (
    CpuInfo,
    FeatureState,
    NvidiaDevice,
    NvidiaState,
    OpenCLState,
    VulkanState,
    WindowsOSInfo,
    WindowsTargetState,
)

_CUDA_124_GLIBC_CONFLICT = (
    "/usr/include/x86_64-linux-gnu/bits/mathcalls.h(83): error: exception specification "
    'is incompatible with that of previous function "cospi" '
    "(declared at line 5554 of /opt/cuda-12.4/bin/../targets/x86_64-linux/include/"
    "crt/math_functions.h)\n"
)


class _Store:
    def __init__(self, prefix: Path, compiler: Path):
        self.prefix = prefix
        self.compiler = compiler
        self.status = ("compatible", "matches registry")

    def installed_status(self, registry, profile):
        return self.status

    def describe(self, registry, profile):
        return {
            "profile": profile,
            "version": "12.5.0",
            "prefix": str(self.prefix),
            "base_sha256": "a" * 64,
            "verified": True,
        }

    def profile_paths(self, registry, profile):
        return {"CC": str(self.compiler)}


def _cuda_root(tmp_path: Path, name: str, *, nvcc: bool = True) -> Path:
    root = tmp_path / name
    (root / "bin").mkdir(parents=True)
    if nvcc:
        executable = root / "bin" / "nvcc"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    return root


def _store(tmp_path: Path) -> _Store:
    prefix = tmp_path / "toolchains" / "installed" / "gcc12"
    prefix.mkdir(parents=True)
    compiler = prefix / "bin" / "gcc"
    compiler.parent.mkdir()
    compiler.write_text("#!/bin/sh\n", encoding="utf-8")
    compiler.chmod(0o755)
    return _Store(prefix, compiler)


def _config(**paths: str) -> BuildConfig:
    return BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        external_prerequisite_paths=paths,
    )


def _mock_nvcc(monkeypatch, versions=None, returncode=0):
    versions = versions or {}
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        version = versions.get(str(args[0]), "12.4")
        output = f"Cuda compilation tools, release {version}, V{version}.99"
        return subprocess.CompletedProcess(args, returncode, stdout=output, stderr="")

    monkeypatch.setattr("ffmpeg_builder.cuda_prerequisites.subprocess.run", run)
    return seen


def _resolve(config, store, environment, required=True):
    return resolve_wsl_mingw_cuda_inputs(
        config,
        ToolchainRegistry(),
        store,
        required=required,
        environment=environment,
    )


def _windows_target(*capabilities, state=FeatureState.PRESENT):
    return WindowsTargetState(
        os=WindowsOSInfo("Windows", "AMD64", "10", 22631),
        cpu=CpuInfo("GenuineIntel", 6, 0, 0, "x64", {}),
        gpus=(),
        nvidia=NvidiaState(
            state=state,
            devices=tuple(
                NvidiaDevice(f"GPU {index}", f"GPU-{index}", "test", capability)
                for index, capability in enumerate(capabilities)
            ),
            error="Windows NVIDIA probe unavailable" if state == FeatureState.UNAVAILABLE else None,
        ),
        vulkan=VulkanState(state=FeatureState.ABSENT),
        opencl=OpenCLState(state=FeatureState.ABSENT),
    )


def _resolved_probe_inputs(tmp_path, monkeypatch) -> ResolvedCudaBuildHostInputs:
    root = _cuda_root(tmp_path, "ptx-sdk")
    store = _store(tmp_path)
    _mock_nvcc(monkeypatch, {str(root / "bin" / "nvcc"): "12.4"})
    resolved = _resolve(_config(cuda=str(root)), store, {})
    assert resolved is not None
    return resolved


def _mock_ptx_probe(monkeypatch, *, compile_results=None, ptx_text=None, cuda_version="12.4"):
    compile_results = list(compile_results or [(0, "")])
    compile_commands = []

    def run(args, **kwargs):
        executable = Path(args[0])
        if args[1:] == ["--version"]:
            if executable.name == "gcc":
                return subprocess.CompletedProcess(args, 0, stdout="gcc (GCC) 12.5.0\n", stderr="")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"Cuda compilation tools, release {cuda_version}, V{cuda_version}.99\n",
                stderr="",
            )

        compile_commands.append(args)
        returncode, output = compile_results.pop(0)
        if returncode == 0 and ptx_text is not None:
            output_path = Path(args[args.index("-o") + 1])
            output_path.write_text(ptx_text, encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode, stdout="", stderr=output)

    monkeypatch.setattr("ffmpeg_builder.cuda_prerequisites.subprocess.run", run)
    return compile_commands


def _ptx(architecture="70"):
    return f".version 8.0\n.target sm_{architecture}\n.address_size 64\n.visible .entry probe() {{ ret; }}\n"


def test_cuda_124_selects_only_configured_root_and_managed_gcc(tmp_path, monkeypatch):
    configured = _cuda_root(tmp_path, "configured")
    home = _cuda_root(tmp_path, "home")
    path_cuda = _cuda_root(tmp_path, "path-cuda")
    store = _store(tmp_path)
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()
    ambient_gcc = ambient_bin / "gcc-15"
    ambient_gcc.write_text("#!/bin/sh\n", encoding="utf-8")
    ambient_gcc.chmod(0o755)
    seen = _mock_nvcc(
        monkeypatch,
        {str(configured / "bin" / "nvcc"): "12.4"},
    )
    env = {
        "CUDA_HOME": str(home),
        "CUDA_PATH": str(path_cuda),
        "CUDA_NVCC_CCBIN": str(ambient_gcc),
        "PATH": str(ambient_bin),
    }

    resolved = _resolve(
        _config(cuda=str(configured)),
        store,
        env,
    )

    assert resolved is not None
    assert resolved.cuda_root == configured.resolve()
    assert resolved.nvcc_path == (configured / "bin" / "nvcc").resolve()
    assert resolved.detected_cuda_version == "12.4"
    assert resolved.ccbin_path == store.compiler.resolve()
    assert resolved.ccbin_profile == "gcc12"
    assert resolved.ccbin_profile_version == "12.5.0"
    assert resolved.ccbin_provenance["base_sha256"] == "a" * 64
    assert resolved.resolution_source == "config"
    assert any("CUDA_NVCC_CCBIN is ignored" in item for item in resolved.diagnostics)
    assert len(seen) == 1
    assert seen[0][0][0] == str((configured / "bin" / "nvcc").resolve())
    assert ToolchainRegistry().toolchain_for_cuda("12.4") == "gcc12"


def test_cuda_home_precedes_cuda_path(tmp_path, monkeypatch):
    home = _cuda_root(tmp_path, "home")
    path_cuda = _cuda_root(tmp_path, "path-cuda")
    _mock_nvcc(monkeypatch)

    resolved = _resolve(
        _config(),
        _store(tmp_path),
        {"CUDA_HOME": str(home), "CUDA_PATH": str(path_cuda)},
    )

    assert resolved is not None
    assert resolved.cuda_root == home.resolve()
    assert resolved.resolution_source == "CUDA_HOME"


def test_cuda_path_is_used_when_cuda_home_is_absent(tmp_path, monkeypatch):
    path_cuda = _cuda_root(tmp_path, "path-cuda")
    _mock_nvcc(monkeypatch)

    resolved = _resolve(_config(), _store(tmp_path), {"CUDA_PATH": str(path_cuda)})

    assert resolved is not None
    assert resolved.cuda_root == path_cuda.resolve()
    assert resolved.resolution_source == "CUDA_PATH"


def test_missing_required_cuda_names_all_supported_settings(tmp_path):
    with pytest.raises(BuildError) as raised:
        _resolve(_config(), _store(tmp_path), {})

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_MISSING
    assert "external_prerequisite_paths.cuda" in str(raised.value)
    assert "CUDA_HOME" in str(raised.value)
    assert "CUDA_PATH" in str(raised.value)


def test_invalid_explicit_root_does_not_fall_back_to_environment_or_path(tmp_path, monkeypatch):
    env_root = _cuda_root(tmp_path, "environment")
    store = _store(tmp_path)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    ambient_nvcc = ambient / "nvcc"
    ambient_nvcc.write_text("#!/bin/sh\n", encoding="utf-8")
    ambient_nvcc.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient))
    _mock_nvcc(monkeypatch)

    with pytest.raises(BuildError) as raised:
        _resolve(
            _config(cuda=str(tmp_path / "invalid-explicit")),
            store,
            {"CUDA_HOME": str(env_root), "PATH": str(ambient)},
        )

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_INVALID_PATH


@pytest.mark.parametrize(
    ("root_kind", "diagnostic"),
    [
        ("missing-nvcc", "expected executable"),
        ("non-executable-nvcc", "not executable"),
        ("unrecognized-version", "could not identify"),
    ],
)
def test_selected_nvcc_validation_failures_are_external_errors(
    tmp_path, monkeypatch, root_kind, diagnostic
):
    root = _cuda_root(tmp_path, root_kind, nvcc=root_kind != "missing-nvcc")
    if root_kind == "non-executable-nvcc":
        (root / "bin" / "nvcc").chmod(0o644)
    store = _store(tmp_path)
    _mock_nvcc(monkeypatch)
    if root_kind == "unrecognized-version":
        monkeypatch.setattr(
            "ffmpeg_builder.cuda_prerequisites.subprocess.run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="unknown"),
        )

    with pytest.raises(BuildError) as raised:
        _resolve(_config(cuda=str(root)), store, {})

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_VALIDATION_FAILED
    assert diagnostic in str(raised.value)


def test_nvcc_symlink_outside_selected_sdk_is_rejected(tmp_path, monkeypatch):
    root = _cuda_root(tmp_path, "sdk", nvcc=False)
    outside = tmp_path / "other-nvcc"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    (root / "bin" / "nvcc").symlink_to(outside)
    _mock_nvcc(monkeypatch)

    with pytest.raises(BuildError) as raised:
        _resolve(_config(cuda=str(root)), _store(tmp_path), {})

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_VALIDATION_FAILED
    assert str(outside) in str(raised.value)


@pytest.mark.parametrize(
    ("status", "code", "detail"),
    [
        (("absent", "not installed"), "cuda_host_toolchain_not_installed", "Provision"),
        (("stale", "registry version changed"), "cuda_host_toolchain_invalid", "Re-acquire"),
    ],
)
def test_missing_or_stale_managed_gcc_is_a_toolchain_error(
    tmp_path, monkeypatch, status, code, detail
):
    root = _cuda_root(tmp_path, "sdk")
    store = _store(tmp_path)
    store.status = status
    _mock_nvcc(monkeypatch)

    with pytest.raises(BuildError) as raised:
        _resolve(_config(cuda=str(root)), store, {})

    assert raised.value.category == CATEGORY_TOOLCHAIN_INSTALL
    assert raised.value.code == code
    assert detail in str(raised.value)
    assert "CUDA 12.4" in str(raised.value)


def test_missing_project_compiler_path_is_a_toolchain_error(tmp_path, monkeypatch):
    root = _cuda_root(tmp_path, "sdk")
    store = _store(tmp_path)
    store.compiler = store.prefix / "bin" / "missing-gcc"
    _mock_nvcc(monkeypatch)

    with pytest.raises(BuildError) as raised:
        _resolve(_config(cuda=str(root)), store, {})

    assert raised.value.category == CATEGORY_TOOLCHAIN_INSTALL
    assert raised.value.code == "cuda_host_compiler_invalid"
    assert "project-managed CC path" in str(raised.value)


def test_unmapped_cuda_version_is_a_toolchain_policy_error(tmp_path, monkeypatch):
    root = _cuda_root(tmp_path, "sdk")
    store = _store(tmp_path)
    _mock_nvcc(monkeypatch, {str(root / "bin" / "nvcc"): "11.8"})

    with pytest.raises(BuildError) as raised:
        _resolve(_config(cuda=str(root)), store, {})

    assert raised.value.category == CATEGORY_TOOLCHAIN_INSTALL
    assert raised.value.code == "cuda_host_mapping_missing"
    assert "CUDA 11.8" in str(raised.value)


def test_mapping_to_missing_profile_is_a_toolchain_error(tmp_path, monkeypatch):
    class _Registry:
        def toolchain_for_cuda(self, version):
            return "missing-profile"

        def get_profile(self, name):
            return None

    root = _cuda_root(tmp_path, "sdk")
    store = _store(tmp_path)
    _mock_nvcc(monkeypatch)

    with pytest.raises(BuildError) as raised:
        resolve_wsl_mingw_cuda_inputs(
            _config(cuda=str(root)),
            _Registry(),
            store,
            environment={},
        )

    assert raised.value.category == CATEGORY_TOOLCHAIN_INSTALL
    assert raised.value.code == "cuda_host_profile_missing"
    assert "missing-profile" in str(raised.value)


def test_optional_unconfigured_cuda_returns_none(tmp_path):
    assert _resolve(_config(), _store(tmp_path), {}, required=False) is None


def test_resolving_cuda_does_not_mutate_process_environment(tmp_path, monkeypatch):
    root = _cuda_root(tmp_path, "sdk")
    _mock_nvcc(monkeypatch)
    before = dict(os.environ)

    _resolve(_config(cuda=str(root)), _store(tmp_path), {})

    assert dict(os.environ) == before


def test_ptx_probe_uses_g1_target_and_project_ccbin(tmp_path, monkeypatch):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()
    ambient = ambient_bin / "gcc"
    ambient.write_text("#!/bin/sh\n", encoding="utf-8")
    ambient.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient_bin))
    monkeypatch.setenv("CUDA_NVCC_CCBIN", str(ambient))
    environment_before = dict(os.environ)
    commands = _mock_ptx_probe(
        monkeypatch,
        ptx_text=_ptx("70"),
    )

    result = validate_cuda_ptx_compatibility(
        inputs,
        _windows_target("8.6", "7.0"),
    )

    assert result.probe_status == "passed"
    assert result.compute_architecture == "compute_70"
    assert result.effective_flags == (
        "--ptx",
        "--gpu-architecture=compute_70",
        f"-ccbin={inputs.ccbin_path}",
    )
    assert result.compatibility_workaround is None
    assert result.nvcc_version == "12.4.99"
    assert result.ccbin_version == "gcc (GCC) 12.5.0"
    assert commands[0][0] == str(inputs.nvcc_path)
    assert f"-ccbin={inputs.ccbin_path}" in commands[0]
    assert str(ambient) not in " ".join(commands[0])
    assert dict(os.environ) == environment_before


def test_ptx_probe_removes_temporary_source_and_output(tmp_path, monkeypatch):
    import tempfile

    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    original_temporary_directory = tempfile.TemporaryDirectory
    temp_paths = []

    def recording_temporary_directory(*args, **kwargs):
        temporary_directory = original_temporary_directory(*args, **kwargs)
        temp_paths.append(Path(temporary_directory.name))
        return temporary_directory

    monkeypatch.setattr(
        "ffmpeg_builder.cuda_prerequisites.tempfile.TemporaryDirectory",
        recording_temporary_directory,
    )
    _mock_ptx_probe(monkeypatch, ptx_text=_ptx())

    validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert len(temp_paths) == 1
    assert not temp_paths[0].exists()


def test_changed_g1_capability_changes_requested_ptx_architecture(tmp_path, monkeypatch):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    commands = _mock_ptx_probe(monkeypatch, ptx_text=_ptx("86"))

    result = validate_cuda_ptx_compatibility(inputs, _windows_target("8.6"))

    assert result.compute_architecture == "compute_86"
    assert "--gpu-architecture=compute_86" in commands[0]


@pytest.mark.parametrize(
    ("target", "code", "detail"),
    [
        (_windows_target(state=FeatureState.ABSENT), "cuda_target_gpu_missing", "NVIDIA"),
        (_windows_target(None), "cuda_target_compute_capability_invalid", "invalid"),
        (_windows_target("unknown"), "cuda_target_compute_capability_invalid", "invalid"),
    ],
)
def test_ptx_probe_rejects_missing_or_invalid_g1_target_data(
    tmp_path, monkeypatch, target, code, detail
):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    commands = _mock_ptx_probe(monkeypatch, ptx_text=_ptx())

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, target)

    assert raised.value.category == CATEGORY_TARGET_DATA
    assert raised.value.code == code
    assert detail in str(raised.value)
    assert commands == []


@pytest.mark.parametrize(
    ("ptx", "detail"),
    [
        ("", "empty PTX"),
        (None, "no PTX file"),
        (".version 8.0\n.target sm_75\n.visible .entry probe() { ret; }\n", "mismatched PTX"),
        (".version 8.0\n.target sm_70\n", "malformed or mismatched PTX"),
    ],
)
def test_ptx_probe_rejects_missing_empty_or_mismatched_ptx(tmp_path, monkeypatch, ptx, detail):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    _mock_ptx_probe(monkeypatch, ptx_text=ptx)

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert raised.value.category == CATEGORY_CUDA_COMPATIBILITY
    assert raised.value.code == "cuda_ptx_invalid"
    assert detail in str(raised.value)


def test_ptx_probe_retries_only_known_cuda_124_glibc_conflict(tmp_path, monkeypatch):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    commands = _mock_ptx_probe(
        monkeypatch,
        compile_results=[(1, _CUDA_124_GLIBC_CONFLICT), (0, "")],
        ptx_text=_ptx("70"),
    )

    result = validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert result.probe_status == "passed"
    assert result.compatibility_workaround == "-Xcompiler=-U_GNU_SOURCE"
    assert result.effective_flags[-1] == "-Xcompiler=-U_GNU_SOURCE"
    assert len(commands) == 2
    assert "-Xcompiler=-U_GNU_SOURCE" not in commands[0]
    assert "-Xcompiler=-U_GNU_SOURCE" in commands[1]


def test_ptx_probe_does_not_retry_unrelated_nvcc_failure(tmp_path, monkeypatch):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    commands = _mock_ptx_probe(
        monkeypatch,
        compile_results=[(1, "unsupported option: --bad-option")],
    )

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert raised.value.category == CATEGORY_CUDA_COMPATIBILITY
    assert raised.value.code == "cuda_ptx_probe_failed"
    assert "unsupported option: --bad-option" in str(raised.value)
    assert len(commands) == 1


@pytest.mark.parametrize(
    "diagnostic",
    [
        "/usr/include/features.h: error: conflicting declaration of '__gnu_mode'\n",
        _CUDA_124_GLIBC_CONFLICT.replace('"cospi"', '"printf"'),
        _CUDA_124_GLIBC_CONFLICT.replace("crt/math_functions.h", "crt/other_functions.h"),
        _CUDA_124_GLIBC_CONFLICT + "probe.cu(10): error: expected an expression\n",
        _CUDA_124_GLIBC_CONFLICT + "probe.cu(10): error #123: unexpected token\n",
        _CUDA_124_GLIBC_CONFLICT + "nvcc fatal : unrelated option failure\n",
    ],
)
def test_ptx_probe_does_not_retry_other_glibc_declaration_errors(tmp_path, monkeypatch, diagnostic):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    commands = _mock_ptx_probe(
        monkeypatch,
        compile_results=[(1, diagnostic), (1, "unexpected retry")],
    )

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert raised.value.code == "cuda_ptx_probe_failed"
    assert diagnostic.strip() in str(raised.value)
    assert len(commands) == 1


def test_ptx_probe_does_not_retry_known_conflict_on_other_cuda_version(tmp_path, monkeypatch):
    inputs = replace(_resolved_probe_inputs(tmp_path, monkeypatch), detected_cuda_version="12.5")
    commands = _mock_ptx_probe(
        monkeypatch,
        compile_results=[(1, _CUDA_124_GLIBC_CONFLICT), (1, "unexpected retry")],
        cuda_version="12.5",
    )

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert raised.value.code == "cuda_ptx_probe_failed"
    assert len(commands) == 1


def test_ptx_probe_preserves_original_and_retry_diagnostics(tmp_path, monkeypatch):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    original = _CUDA_124_GLIBC_CONFLICT
    retry = "nvcc fatal: host compiler rejected retry\n"
    commands = _mock_ptx_probe(
        monkeypatch,
        compile_results=[(1, original), (1, retry)],
    )

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert raised.value.code == "cuda_ptx_probe_failed"
    assert original in str(raised.value)
    assert retry.strip() in str(raised.value)
    assert len(commands) == 2


@pytest.mark.parametrize("missing", ["nvcc", "ccbin"])
def test_ptx_probe_reports_missing_selected_executable(tmp_path, monkeypatch, missing):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    if missing == "nvcc":
        inputs = replace(inputs, nvcc_path=inputs.cuda_root / "bin" / "missing-nvcc")
        expected_category = CATEGORY_EXTERNAL_PREREQUISITE
        expected_code = "cuda_nvcc_unavailable"
    else:
        inputs = replace(inputs, ccbin_path=inputs.ccbin_path.parent / "missing-gcc")
        expected_category = CATEGORY_TOOLCHAIN_INSTALL
        expected_code = "cuda_ccbin_unavailable"
    _mock_ptx_probe(monkeypatch, ptx_text=_ptx())

    with pytest.raises(BuildError) as raised:
        validate_cuda_ptx_compatibility(inputs, _windows_target("7.0"))

    assert raised.value.category == expected_category
    assert raised.value.code == expected_code


def test_native_ptx_architecture_does_not_require_gpu(tmp_path, monkeypatch):
    inputs = _resolved_probe_inputs(tmp_path, monkeypatch)
    _mock_ptx_probe(monkeypatch, ptx_text=_ptx("70"))

    profile = validate_cuda_ptx_compatibility(inputs, None, architecture="7.0")

    assert profile.compute_architecture == "compute_70"
    assert profile.probe_status == "passed"


def test_native_explicit_host_compiler_is_authoritative(tmp_path, monkeypatch):
    root = _cuda_root(tmp_path, "sdk")
    store = _store(tmp_path)
    compiler = tmp_path / "explicit-gcc"
    compiler.write_text("#!/bin/sh\n", encoding="utf-8")
    compiler.chmod(0o755)
    _mock_nvcc(monkeypatch)
    profile = resolve_wsl_mingw_cuda_inputs(
        _config(cuda=str(root)),
        ToolchainRegistry(),
        store,
        environment={"CUDA_NVCC_CCBIN": str(compiler)},
        allow_host_override=True,
    )
    assert profile is not None
    assert profile.ccbin_path == compiler
    assert profile.ccbin_provenance["source"] == "CUDA_NVCC_CCBIN"

    with pytest.raises(BuildError, match="CUDA_NVCC_CCBIN"):
        resolve_wsl_mingw_cuda_inputs(
            _config(cuda=str(root)),
            ToolchainRegistry(),
            store,
            environment={"CUDA_NVCC_CCBIN": str(tmp_path / "missing")},
            allow_host_override=True,
        )


def test_required_cuda_does_not_silently_exclude_libvmaf(tmp_path, monkeypatch):
    profile = replace(
        _resolved_probe_inputs(tmp_path, monkeypatch),
        compute_architecture="compute_70",
        probe_status="passed",
    )
    config = BuildConfig(cuda_build_mode="required")
    components = [SimpleNamespace(name="nv-codec"), SimpleNamespace(name="ffmpeg")]
    with pytest.raises(BuildError, match="libvmaf is not buildable"):
        validate_required_cuda_components(config, components, profile)
    components.append(SimpleNamespace(name="libvmaf"))
    validate_required_cuda_components(config, components, profile)


def test_output_modes_reuse_common_flags_without_ptx(tmp_path, monkeypatch):
    inputs = replace(
        _resolved_probe_inputs(tmp_path, monkeypatch),
        compute_architecture="compute_70",
        probe_status="passed",
        compatibility_workaround="-Xcompiler=-U_GNU_SOURCE",
    )
    commands = []

    def run(args, **kwargs):
        commands.append((args, kwargs))
        Path(args[args.index("-o") + 1]).write_bytes(b"compiled")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ffmpeg_builder.cuda_prerequisites.subprocess.run", run)
    monkeypatch.setenv("NVCC_PREPEND_FLAGS", "--untrusted-flag")
    validate_cuda_output_modes(inputs)

    assert [
        next(flag for flag in args if flag in ("--compile", "--fatbin")) for args, _ in commands
    ] == [
        "--compile",
        "--fatbin",
    ]
    for args, kwargs in commands:
        assert "--ptx" not in args
        assert f"-ccbin={inputs.ccbin_path}" in args
        assert "-Xcompiler=-U_GNU_SOURCE" in args
        assert "--gpu-architecture=compute_70" in args
        assert "NVCC_PREPEND_FLAGS" not in kwargs["env"]


def test_output_mode_probe_preserves_compiler_diagnostic(tmp_path, monkeypatch):
    inputs = replace(
        _resolved_probe_inputs(tmp_path, monkeypatch),
        compute_architecture="compute_70",
        probe_status="passed",
    )
    monkeypatch.setattr(
        "ffmpeg_builder.cuda_prerequisites.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="nvcc failure"
        ),
    )
    with pytest.raises(BuildError, match="nvcc failure") as raised:
        validate_cuda_output_modes(inputs)
    assert raised.value.code == "cuda_output_probe_failed"


def test_cuda_resume_invalidates_legacy_or_changed_consumers_only(tmp_path, monkeypatch):
    from ffmpeg_builder.state import ComponentStatus, StateManager

    inputs = replace(
        _resolved_probe_inputs(tmp_path, monkeypatch),
        compute_architecture="compute_70",
        probe_status="passed",
    )
    manager = StateManager(tmp_path / "state.json")
    for name in ("libvmaf", "ffmpeg", "x264"):
        manager.mark_component_status(name, ComponentStatus.COMPLETED, "1")

    assert invalidate_cuda_resume(manager, inputs, libvmaf_cuda=True)
    assert manager.get().components["libvmaf"].status == ComponentStatus.PENDING
    assert manager.get().components["ffmpeg"].status == ComponentStatus.PENDING
    assert manager.get().components["x264"].status == ComponentStatus.COMPLETED

    for name in ("libvmaf", "ffmpeg"):
        manager.mark_component_status(name, ComponentStatus.COMPLETED, "1")
    manager.get().cuda_profile = cuda_profile_identity(inputs, libvmaf_cuda=True)
    manager.save()
    reloaded = StateManager(tmp_path / "state.json")
    assert not invalidate_cuda_resume(reloaded, inputs, libvmaf_cuda=True)
    assert invalidate_cuda_resume(reloaded, inputs, libvmaf_cuda=False)


def test_cuda_resume_invalidates_changed_nvcodec_headers(tmp_path, monkeypatch):
    from ffmpeg_builder.state import ComponentStatus, StateManager

    inputs = replace(
        _resolved_probe_inputs(tmp_path, monkeypatch),
        compute_architecture="compute_70",
        probe_status="passed",
    )
    manager = StateManager(tmp_path / "state.json")
    for name in ("nv-codec", "libvmaf", "ffmpeg"):
        manager.mark_component_status(name, ComponentStatus.COMPLETED, "1")
    old_headers = {"version": "12.2", "sha256": "old"}
    new_headers = {"version": "13.0", "sha256": "new"}
    manager.get().cuda_profile = cuda_profile_identity(
        inputs, libvmaf_cuda=True, nv_codec=old_headers
    )

    assert invalidate_cuda_resume(manager, inputs, libvmaf_cuda=True, nv_codec=new_headers)
    assert all(
        manager.get().components[name].status == ComponentStatus.PENDING
        for name in ("nv-codec", "libvmaf", "ffmpeg")
    )
