"""Tests for external Vulkan SDK resolution and host-tool validation."""

import os
import sys
from pathlib import Path

import pytest

from ffmpeg_builder.build_types import CATEGORY_EXTERNAL_PREREQUISITE, BuildError
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.vulkan_prerequisites import (
    CAPABILITY_SHADER_COMPILATION,
    CAPABILITY_SPIRV_OPTIMIZATION,
    CAPABILITY_SPIRV_VALIDATION,
    resolve_wsl_mingw_vulkan_sdk,
)


def _sdk_root(base: Path, *, package_root: bool = True) -> tuple[Path, Path]:
    selected = base / "sdk"
    effective = selected / "x86_64" if package_root else selected
    (effective / "bin").mkdir(parents=True)
    header = effective / "include" / "vulkan" / "vulkan.h"
    header.parent.mkdir(parents=True)
    header.write_text("/* Vulkan SDK layout marker */\n", encoding="utf-8")
    (selected / "setup-env.sh").write_text(
        "#!/bin/sh\n" + f"touch '{base / 'setup-env-ran'}'\n",
        encoding="utf-8",
    )
    return selected, effective


def _write_tool(effective: Path, name: str, mode: str = "success") -> Path:
    executable = effective / "bin" / name
    executable.write_text(
        f"""#!{sys.executable}
import pathlib
import sys

args = sys.argv[1:]
mode = {mode!r}
tool = {name!r}
if args in (["--version"], ["-v"]):
    if mode == "version-fail":
        print("version probe failed", file=sys.stderr)
        raise SystemExit(4)
    print(tool + " test-version 1.0")
    raise SystemExit(0)
if tool in ("glslc", "glslangValidator"):
    if mode == "compile-fail":
        print("shader compile failed", file=sys.stderr)
        raise SystemExit(2)
    if mode != "empty-output":
        output = pathlib.Path(args[args.index("-o") + 1])
        output.write_bytes(b"not-SPIR-V" if mode == "invalid-output" else b"\\x03\\x02#\\x07SPIR-V")
    raise SystemExit(0)
if tool == "spirv-val":
    if mode == "validation-fail":
        print("invalid SPIR-V", file=sys.stderr)
        raise SystemExit(3)
    raise SystemExit(0)
if tool == "spirv-opt":
    if mode == "optimization-fail":
        print("optimization failed", file=sys.stderr)
        raise SystemExit(5)
    output = pathlib.Path(args[args.index("-o") + 1])
    output.write_bytes(b"optimized SPIR-V")
    raise SystemExit(0)
raise SystemExit(6)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _populate_sdk(effective: Path, *, optimizer: bool = False, mode: str = "success") -> None:
    _write_tool(effective, "glslc", mode)
    _write_tool(effective, "spirv-val", mode)
    if optimizer:
        _write_tool(effective, "spirv-opt", mode)


def _config(**paths: str) -> BuildConfig:
    return BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        external_prerequisite_paths=paths,
    )


def _resolve(config, environment, **kwargs):
    return resolve_wsl_mingw_vulkan_sdk(config, environment=environment, **kwargs)


def test_configured_package_root_precedes_vulkan_sdk_and_probes_selected_tools(
    tmp_path, monkeypatch
):
    selected, effective = _sdk_root(tmp_path)
    environment_root, environment_effective = _sdk_root(tmp_path / "env")
    _populate_sdk(effective)
    _populate_sdk(environment_effective)
    environment = {
        "VULKAN_SDK": str(environment_root),
        "LD_LIBRARY_PATH": "/host/vulkan/lib",
        "PKG_CONFIG_PATH": "/host/vulkan/lib/pkgconfig",
        "CMAKE_PREFIX_PATH": "/host/vulkan",
        "VK_LAYER_PATH": "/host/vulkan/layers",
    }
    environment_before = dict(environment)
    monkeypatch.setenv("PATH", str(environment_effective / "bin"))
    before = dict(os.environ)

    resolved = _resolve(_config(vulkan=str(selected)), environment)

    assert resolved is not None
    assert resolved.selected_root == selected.resolve()
    assert resolved.configured_root == selected.resolve()
    assert resolved.effective_sdk_root == effective.resolve()
    assert resolved.resolution_source == "config"
    assert resolved.shader_compiler_path == (effective / "bin" / "glslc").resolve()
    assert resolved.spirv_validator_path == (effective / "bin" / "spirv-val").resolve()
    assert resolved.spirv_optimizer_path is None
    assert resolved.validated_capabilities == frozenset(
        {CAPABILITY_SHADER_COMPILATION, CAPABILITY_SPIRV_VALIDATION}
    )
    assert all("test-version 1.0" in value for value in resolved.tool_diagnostics.values())
    assert any("Shader probe compiled" in item for item in resolved.diagnostics)
    assert not (tmp_path / "setup-env-ran").exists()
    assert environment == environment_before
    assert dict(os.environ) == before
    assert not {
        "LD_LIBRARY_PATH",
        "PKG_CONFIG_PATH",
        "CMAKE_PREFIX_PATH",
        "VK_LAYER_PATH",
    }.intersection(resolved.to_dict())


def test_vulkan_sdk_environment_root_can_be_effective_architecture_root(tmp_path):
    _selected, effective = _sdk_root(tmp_path, package_root=False)
    _populate_sdk(effective)

    resolved = _resolve(_config(), {"VULKAN_SDK": str(effective)})

    assert resolved is not None
    assert resolved.selected_root == effective.resolve()
    assert resolved.effective_sdk_root == effective.resolve()
    assert resolved.configured_root is None
    assert resolved.resolution_source == "VULKAN_SDK"
    assert resolved.diagnostic_metadata["environment_variable"] == "VULKAN_SDK"


def test_optional_missing_sdk_returns_none_and_required_sdk_has_distinct_error():
    assert _resolve(_config(), {}, required=False) is None

    with pytest.raises(BuildError) as raised:
        _resolve(_config(), {})

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == "vulkan_sdk_missing"
    assert "VULKAN_SDK" in str(raised.value)


def test_invalid_explicit_sdk_does_not_fall_back_to_environment_or_path(tmp_path, monkeypatch):
    _ambient, ambient_effective = _sdk_root(tmp_path / "ambient", package_root=False)
    _populate_sdk(ambient_effective)
    invalid = tmp_path / "missing-config"
    monkeypatch.setenv("PATH", str(ambient_effective / "bin"))

    with pytest.raises(BuildError) as raised:
        _resolve(
            _config(vulkan=str(invalid)),
            {"VULKAN_SDK": str(ambient_effective), "PATH": str(ambient_effective / "bin")},
        )

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == "vulkan_sdk_invalid_path"
    assert str(invalid) in str(raised.value)


def test_selected_sdk_missing_tool_does_not_use_tool_from_path(tmp_path, monkeypatch):
    _selected, effective = _sdk_root(tmp_path / "selected", package_root=False)
    ambient_root, ambient_effective = _sdk_root(tmp_path / "ambient", package_root=False)
    _populate_sdk(ambient_effective)
    monkeypatch.setenv("PATH", str(ambient_effective / "bin"))

    with pytest.raises(BuildError) as raised:
        _resolve(
            _config(vulkan=str(effective)),
            {"PATH": str(ambient_effective / "bin")},
        )

    assert raised.value.code == "vulkan_shader_compiler_missing"
    assert str(effective) in str(raised.value)
    assert str(ambient_root.resolve()) not in str(raised.value)


def test_glslang_validator_is_supported_as_shader_compiler(tmp_path):
    _selected, effective = _sdk_root(tmp_path)
    _write_tool(effective, "glslangValidator")
    _write_tool(effective, "spirv-val")

    resolved = _resolve(_config(vulkan=str(effective.parent)), {})

    assert resolved is not None
    assert resolved.shader_compiler_path == (effective / "bin" / "glslangValidator").resolve()
    assert CAPABILITY_SHADER_COMPILATION in resolved.validated_capabilities
    assert CAPABILITY_SPIRV_VALIDATION in resolved.validated_capabilities


def test_unrelated_bin_directory_is_not_accepted_as_an_sdk_layout(tmp_path):
    unrelated = tmp_path / "unrelated"
    (unrelated / "bin").mkdir(parents=True)

    with pytest.raises(BuildError) as raised:
        _resolve(_config(vulkan=str(unrelated)), {})

    assert raised.value.code == "vulkan_sdk_layout_invalid"
    assert "include/vulkan/vulkan.h" in str(raised.value)


def test_tool_must_be_executable_and_must_run(tmp_path):
    _selected, effective = _sdk_root(tmp_path)
    shader = _write_tool(effective, "glslc")
    _write_tool(effective, "spirv-val")
    shader.chmod(0o644)

    with pytest.raises(BuildError) as raised:
        _resolve(_config(vulkan=str(effective)), {})
    assert raised.value.code == "vulkan_tool_invalid"

    shader.chmod(0o755)
    shader.write_text("#!/definitely/not/an/interpreter\n", encoding="utf-8")
    shader.chmod(0o755)
    with pytest.raises(BuildError) as raised:
        _resolve(_config(vulkan=str(effective)), {})
    assert raised.value.code == "vulkan_tool_execution_failed"


@pytest.mark.parametrize(
    ("mode", "expected_code", "message"),
    [
        ("compile-fail", "vulkan_shader_compile_failed", "shader compile failed"),
        ("empty-output", "vulkan_shader_output_invalid", "non-empty SPIR-V"),
        ("invalid-output", "vulkan_shader_output_invalid", "SPIR-V magic number"),
        ("validation-fail", "vulkan_spirv_validation_failed", "invalid SPIR-V"),
    ],
)
def test_shader_and_spirv_probe_failures_are_actionable(tmp_path, mode, expected_code, message):
    _selected, effective = _sdk_root(tmp_path)
    _populate_sdk(effective, mode=mode)

    with pytest.raises(BuildError) as raised:
        _resolve(_config(vulkan=str(effective)), {})

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == expected_code
    assert message in str(raised.value)


def test_optimizer_is_only_required_and_probed_when_requested(tmp_path):
    _selected, effective = _sdk_root(tmp_path)
    _populate_sdk(effective)
    config = _config(vulkan=str(effective))
    with pytest.raises(BuildError) as raised:
        _resolve(
            config,
            {},
            required_capabilities=(
                CAPABILITY_SHADER_COMPILATION,
                CAPABILITY_SPIRV_VALIDATION,
                CAPABILITY_SPIRV_OPTIMIZATION,
            ),
        )
    assert raised.value.code == "vulkan_spirv_optimizer_missing"

    _write_tool(effective, "spirv-opt")
    resolved = _resolve(
        config,
        {},
        required_capabilities=(
            CAPABILITY_SHADER_COMPILATION,
            CAPABILITY_SPIRV_VALIDATION,
            CAPABILITY_SPIRV_OPTIMIZATION,
        ),
    )
    assert resolved is not None
    assert resolved.spirv_optimizer_path == (effective / "bin" / "spirv-opt").resolve()
    assert resolved.validated_capabilities == frozenset(
        {
            CAPABILITY_SHADER_COMPILATION,
            CAPABILITY_SPIRV_VALIDATION,
            CAPABILITY_SPIRV_OPTIMIZATION,
        }
    )


def test_optimizer_failure_is_reported_only_when_optimizer_is_required(tmp_path):
    _selected, effective = _sdk_root(tmp_path)
    _populate_sdk(effective, optimizer=True, mode="optimization-fail")

    with pytest.raises(BuildError) as raised:
        _resolve(
            _config(vulkan=str(effective)),
            {},
            required_capabilities=(
                CAPABILITY_SHADER_COMPILATION,
                CAPABILITY_SPIRV_VALIDATION,
                CAPABILITY_SPIRV_OPTIMIZATION,
            ),
        )

    assert raised.value.code == "vulkan_spirv_optimization_failed"
    assert "optimization failed" in str(raised.value)


def test_optimizer_capability_requires_compilation_and_validation():
    with pytest.raises(ValueError, match="requires shader_compilation"):
        resolve_wsl_mingw_vulkan_sdk(
            _config(),
            required=False,
            required_capabilities=(CAPABILITY_SPIRV_OPTIMIZATION,),
            environment={},
        )


def test_invalid_architecture_layout_and_unknown_capability_fail_early(tmp_path):
    with pytest.raises(ValueError, match="single directory"):
        resolve_wsl_mingw_vulkan_sdk(_config(), architecture="../x86_64", environment={})
    with pytest.raises(ValueError, match="Unsupported Vulkan SDK capabilities"):
        resolve_wsl_mingw_vulkan_sdk(
            _config(), required_capabilities=("gpu_runtime",), environment={}
        )
