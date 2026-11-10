"""Tests for externally managed build-host prerequisite resolution."""

import os
from pathlib import Path

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.external_prerequisites import (
    CODE_PREREQUISITE_INVALID_PATH,
    CODE_PREREQUISITE_MISSING,
    CODE_PREREQUISITE_VALIDATION_FAILED,
    CATEGORY_EXTERNAL_PREREQUISITE,
    ExternalPrerequisiteValidation,
    RESOLUTION_CONFIG,
    RESOLUTION_ENVIRONMENT,
    resolve_external_prerequisite,
)

ENV_VAR = "FFMPEG_BUILDER_TEST_SDK_ROOT"


def _root(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_configured_root_is_used_before_environment(tmp_path):
    configured = _root(tmp_path, "configured")
    environment = _root(tmp_path, "environment")

    resolved = resolve_external_prerequisite(
        "test_sdk",
        str(configured),
        ENV_VAR,
        environment={ENV_VAR: str(environment)},
    )

    assert resolved is not None
    assert resolved.root == configured.resolve()
    assert resolved.resolution_source == RESOLUTION_CONFIG


def test_environment_fallback_tracks_source_and_validation_results(tmp_path):
    root = _root(tmp_path, "sdk")
    compiler = root / "bin" / "compiler"
    compiler.parent.mkdir()
    compiler.touch()
    environment_before = dict(os.environ)

    resolved = resolve_external_prerequisite(
        "test_sdk",
        environment_variable=ENV_VAR,
        environment={ENV_VAR: str(root)},
        validator=lambda selected: ExternalPrerequisiteValidation(
            host_tools={"compiler": selected / "bin" / "compiler"},
            diagnostics=("test validation passed",),
            diagnostic_metadata={"checked": True},
            effective_values={"target": "example"},
        ),
    )

    assert resolved is not None
    assert resolved.root == root.resolve()
    assert resolved.resolution_source == RESOLUTION_ENVIRONMENT
    assert resolved.host_tools == {"compiler": root / "bin" / "compiler"}
    assert resolved.usable is True
    assert resolved.diagnostics == ("test validation passed",)
    assert resolved.diagnostic_metadata == {
        "selected_value": str(root),
        "configuration_key": "external_prerequisite_paths.test_sdk",
        "environment_variable": ENV_VAR,
        "checked": True,
    }
    assert resolved.effective_values == {"target": "example"}
    assert dict(os.environ) == environment_before


def test_explicit_invalid_root_does_not_fall_back_to_environment_or_path(tmp_path, monkeypatch):
    fallback = _root(tmp_path, "environment")
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    (path_dir / "nvcc").touch()
    monkeypatch.setenv("PATH", str(path_dir))

    with pytest.raises(BuildError) as raised:
        resolve_external_prerequisite(
            "cuda",
            str(tmp_path / "missing-configured-root"),
            ENV_VAR,
            environment={ENV_VAR: str(fallback)},
        )

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_INVALID_PATH
    assert "config" in str(raised.value)
    assert str(tmp_path / "missing-configured-root") in str(raised.value)
    assert raised.value.__cause__ is not None


def test_required_missing_prerequisite_has_actionable_error():
    with pytest.raises(BuildError) as raised:
        resolve_external_prerequisite(
            "vulkan",
            environment_variable=ENV_VAR,
            environment={},
        )

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_MISSING
    assert "external_prerequisite_paths.vulkan" in str(raised.value)
    assert ENV_VAR in str(raised.value)


@pytest.mark.parametrize("invalid", ["", "  ", "\0"])
def test_malformed_path_is_reported_as_external_prerequisite_error(invalid):
    with pytest.raises(BuildError) as raised:
        resolve_external_prerequisite("test_sdk", invalid)

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_INVALID_PATH


def test_file_cannot_be_used_as_prerequisite_root(tmp_path):
    path = tmp_path / "not-a-directory"
    path.touch()

    with pytest.raises(BuildError) as raised:
        resolve_external_prerequisite("test_sdk", str(path))

    assert raised.value.code == CODE_PREREQUISITE_INVALID_PATH
    assert "not a directory" in str(raised.value)


def test_failed_technology_validation_has_distinct_actionable_error(tmp_path):
    root = _root(tmp_path, "sdk")

    with pytest.raises(BuildError) as raised:
        resolve_external_prerequisite(
            "test_sdk",
            str(root),
            ENV_VAR,
            validator=lambda _: ExternalPrerequisiteValidation(
                usable=False,
                diagnostics=("required compiler is missing",),
            ),
        )

    assert raised.value.category == CATEGORY_EXTERNAL_PREREQUISITE
    assert raised.value.code == CODE_PREREQUISITE_VALIDATION_FAILED
    assert "required compiler is missing" in str(raised.value)
    assert str(root.resolve()) in str(raised.value)
    assert "external_prerequisite_paths.test_sdk" in str(raised.value)


def test_optional_unconfigured_prerequisite_returns_none():
    assert resolve_external_prerequisite("test_sdk", required=False, environment={}) is None
