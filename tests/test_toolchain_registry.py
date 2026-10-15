"""Tests for the toolchain registry loader and its fail-fast validation."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ffmpeg_builder.toolchain_registry import ToolchainAssignment, ToolchainRegistry

_SHA = "a" * 64


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "toolchains.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _build_source(**overrides: object) -> dict:
    variant: dict = {"type": "build", "url": "https://example.org/x.tar.gz", "sha256": _SHA}
    variant.update(overrides)
    return {"default": "build", "build": variant}


class TestShippedRegistry:
    """The registry committed with the repository must load and validate."""

    def test_shipped_registry_loads_and_validates(self):
        registry = ToolchainRegistry()
        assert "gcc12" in registry.profiles
        assert "gcc13" in registry.profiles

    def test_resolve_platform_profile_and_default_toolchain(self):
        registry = ToolchainRegistry()
        profile = registry.resolve_platform_profile("8.1", "linux")
        assert profile is not None
        assert profile.fallback_toolchain == "gcc12"
        assert registry.default_toolchain_for("8.1", "linux") == "gcc12"
        # FFmpeg 9.0 on Linux uses the system compiler (no fallback).
        assert registry.default_toolchain_for("9.0", "linux") is None
        # macOS has an empty toolchain registry and relies on the system.
        assert registry.default_toolchain_for("9.0", "darwin") is None

    def test_cuda_map_resolves_declared_profiles(self):
        registry = ToolchainRegistry()
        assert registry.toolchain_for_cuda("12.2") == "gcc12"
        assert registry.toolchain_for_cuda("13.1") == "gcc13"


class TestProfileValidation:
    """Structural validation of toolchain profiles."""

    def test_profile_without_source_is_not_deployable(self, tmp_path):
        path = _write(tmp_path, {"toolchains": {"gcc12": {"kind": "compiler"}}})
        registry = ToolchainRegistry(path)
        profile = registry.get_profile("gcc12")
        assert profile is not None
        assert profile.deployable is False

    def test_valid_source_profile_is_deployable(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "gcc12": {
                        "kind": "compiler",
                        "platforms": ["linux"],
                        "source": _build_source(bootstrap={"compiler": "system", "requires": []}),
                    }
                }
            },
        )
        registry = ToolchainRegistry(path)
        profile = registry.get_profile("gcc12")
        assert profile is not None
        assert profile.deployable is True

    def test_invalid_kind_rejected(self, tmp_path):
        path = _write(tmp_path, {"toolchains": {"x": {"kind": "banana"}}})
        with pytest.raises(ValueError, match="invalid kind"):
            ToolchainRegistry(path)

    def test_unknown_platform_rejected(self, tmp_path):
        path = _write(tmp_path, {"toolchains": {"x": {"kind": "compiler", "platforms": ["plan9"]}}})
        with pytest.raises(ValueError, match="invalid platform"):
            ToolchainRegistry(path)

    def test_source_missing_sha256_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "x": {
                        "kind": "compiler",
                        "source": {
                            "default": "build",
                            "build": {"type": "build", "url": "https://example.org/x.tar.gz"},
                        },
                    }
                }
            },
        )
        with pytest.raises(ValueError, match="requires a 64-hex 'sha256'"):
            ToolchainRegistry(path)

    def test_source_invalid_type_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "x": {
                        "kind": "compiler",
                        "source": {"default": "build", "build": {"type": "build"}},
                    }
                }
            },
        )
        with pytest.raises(ValueError):
            ToolchainRegistry(path)

    def test_source_default_not_in_variants_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "x": {
                        "kind": "compiler",
                        "source": {"default": "binary", "build": {"type": "build"}},
                    }
                }
            },
        )
        with pytest.raises(ValueError, match="is not among variants"):
            ToolchainRegistry(path)

    def test_source_type_key_mismatch_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "x": {
                        "kind": "compiler",
                        "source": {
                            "default": "build",
                            "build": {"type": "binary", "url": "u", "sha256": _SHA},
                        },
                    }
                }
            },
        )
        with pytest.raises(ValueError, match="must match the variant key"):
            ToolchainRegistry(path)


class TestReferenceValidation:
    """Cross-reference integrity between sections."""

    def test_unknown_bootstrap_requirement_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "gcc12": {
                        "kind": "compiler",
                        "source": _build_source(bootstrap={"requires": ["gmp"]}),
                    }
                }
            },
        )
        with pytest.raises(ValueError, match="requires unknown profile"):
            ToolchainRegistry(path)

    def test_bootstrap_cycle_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "toolchains": {
                    "a": {
                        "kind": "compiler",
                        "source": _build_source(bootstrap={"requires": ["b"]}),
                    },
                    "b": {
                        "kind": "compiler",
                        "source": _build_source(bootstrap={"requires": ["a"]}),
                    },
                }
            },
        )
        with pytest.raises(ValueError, match="cycle detected"):
            ToolchainRegistry(path)

    def test_unknown_fallback_toolchain_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "platform_profiles": {
                    "linux-ffmpeg-8.1": {
                        "ffmpeg_version": "8.1",
                        "platform": "linux",
                        "fallback_toolchain": "gcc99",
                    }
                }
            },
        )
        with pytest.raises(ValueError, match="unknown fallback_toolchain"):
            ToolchainRegistry(path)

    def test_cuda_unknown_profile_rejected(self, tmp_path):
        path = _write(tmp_path, {"cuda": {"12.2": "gcc99"}})
        with pytest.raises(ValueError, match="unknown toolchain"):
            ToolchainRegistry(path)


class TestFileLoading:
    """Loader behaviour for missing/invalid files."""

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ToolchainRegistry(tmp_path / "does-not-exist.yaml")

    def test_top_level_not_mapping_rejected(self, tmp_path):
        path = _write(tmp_path, ["not", "a", "mapping"])
        with pytest.raises(ValueError, match="Expected top-level mapping"):
            ToolchainRegistry(path)

    def test_empty_file_loads_as_empty_registry(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        registry = ToolchainRegistry(path)
        assert registry.profiles == {}
        assert registry.platform_profiles == {}
        assert registry.cuda_map == {}


def _range_registry(tmp_path: Path) -> ToolchainRegistry:
    data = {
        "toolchains": {
            "gcc12": {"kind": "compiler", "platforms": ["linux"]},
            "gcc13": {"kind": "compiler", "platforms": ["linux"]},
        },
        "platform_profiles": {
            "linux-8.1": {
                "ffmpeg_version": "8.1",
                "platform": "linux",
                "system_tool_range": {"gcc": ">=12,<14"},
                "fallback_toolchain": "gcc12",
            },
            "linux-9.0": {
                "ffmpeg_version": "9.0",
                "platform": "linux",
                "system_tool_range": {"gcc": ">=15"},
                "fallback_toolchain": None,
            },
        },
    }
    return ToolchainRegistry(_write(tmp_path, data))


class TestResolution:
    """Effective-toolchain resolution (read-only)."""

    def test_system_in_range_uses_system(self, tmp_path):
        registry = _range_registry(tmp_path)
        assignment = registry.resolve_effective_toolchain("8.1", "linux", {"gcc": "13.2.0"})
        assert assignment == ToolchainAssignment(mode="system")

    def test_system_out_of_range_deploys_fallback(self, tmp_path):
        registry = _range_registry(tmp_path)
        assignment = registry.resolve_effective_toolchain("8.1", "linux", {"gcc": "16.2.1"})
        assert assignment == ToolchainAssignment(mode="deploy", profile="gcc12")

    def test_missing_system_version_deploys_fallback(self, tmp_path):
        registry = _range_registry(tmp_path)
        assignment = registry.resolve_effective_toolchain("8.1", "linux", {})
        assert assignment == ToolchainAssignment(mode="deploy", profile="gcc12")

    def test_out_of_range_without_fallback_is_unresolved(self, tmp_path):
        registry = _range_registry(tmp_path)
        assert registry.resolve_effective_toolchain("9.0", "linux", {"gcc": "12.0.0"}) is None

    def test_unknown_platform_profile_is_unresolved(self, tmp_path):
        registry = _range_registry(tmp_path)
        assert registry.resolve_effective_toolchain("9.0", "windows", {"gcc": "16"}) is None

    def test_explicit_lower_profile_overrides(self, tmp_path):
        registry = _range_registry(tmp_path)
        assignment = registry.resolve_component_toolchain("gcc13", "9.0", "linux", {"gcc": "16"})
        assert assignment == ToolchainAssignment(mode="deploy", profile="gcc13")

    def test_na_inherits_effective_toolchain(self, tmp_path):
        registry = _range_registry(tmp_path)
        assignment = registry.resolve_component_toolchain("n/a", "9.0", "linux", {"gcc": "16.2.1"})
        assert assignment == ToolchainAssignment(mode="system")

    def test_plan_has_one_entry_per_component(self, tmp_path):
        registry = _range_registry(tmp_path)
        components = [
            SimpleNamespace(name="x264", toolchain="n/a"),
            SimpleNamespace(name="cuda-part", toolchain="gcc12"),
        ]
        plan = registry.plan_component_toolchains(components, "8.1", "linux", {"gcc": "16"})
        assert [entry.component for entry in plan] == ["x264", "cuda-part"]
        assert plan[0].assignment == ToolchainAssignment(mode="deploy", profile="gcc12")
        assert plan[1].assignment == ToolchainAssignment(mode="deploy", profile="gcc12")


class TestRangeValidation:
    """Allowed-range strings are validated and evaluated correctly."""

    def test_invalid_range_constraint_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "platform_profiles": {
                    "p": {
                        "ffmpeg_version": "8.1",
                        "platform": "linux",
                        "system_tool_range": {"gcc": "12-14"},
                    }
                }
            },
        )
        with pytest.raises(ValueError, match="invalid version constraint"):
            ToolchainRegistry(path)

    def test_major_only_upper_bound(self):
        from ffmpeg_builder.toolchain_registry import _satisfies_range, _version_tuple

        assert _satisfies_range(_version_tuple("25.3.0"), "<=25")
        assert not _satisfies_range(_version_tuple("26.0.0"), "<=25")

    def test_open_ended_lower_bound(self):
        from ffmpeg_builder.toolchain_registry import _satisfies_range, _version_tuple

        assert _satisfies_range(_version_tuple("16.2.1"), ">=15")
        assert not _satisfies_range(_version_tuple("14.9.9"), ">=15")


class TestRecipeValidation:
    """Build recipes and build-only arguments are validated."""

    def _registry(self, tmp_path: Path, variant: dict) -> ToolchainRegistry:
        data = {
            "toolchains": {
                "x": {
                    "kind": "compiler",
                    "source": {"default": variant["type"], variant["type"]: variant},
                }
            }
        }
        return ToolchainRegistry(_write(tmp_path, data))

    def test_build_recipe_accepted(self, tmp_path):
        registry = self._registry(
            tmp_path,
            {"type": "build", "recipe": "gcc", "url": "u", "sha256": _SHA},
        )
        profile = registry.get_profile("x")
        assert profile is not None
        assert profile.sources["build"].recipe == "gcc"

    def test_unknown_recipe_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="invalid recipe"):
            self._registry(
                tmp_path,
                {"type": "build", "recipe": "bogus", "url": "u", "sha256": _SHA},
            )

    def test_build_args_on_non_build_variant_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="only for a 'build' source"):
            self._registry(
                tmp_path,
                {"type": "binary", "url": "u", "sha256": _SHA, "configure_args": ["--x"]},
            )
