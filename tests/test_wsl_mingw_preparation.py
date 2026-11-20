"""Tests for the structured WSL-Mingw preparation data."""

from dataclasses import dataclass
import hashlib
import json

import pytest

from ffmpeg_builder.build_types import CATEGORY_TOOLCHAIN_INSTALL, BuildError
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.toolchain_store import STATUS_COMPATIBLE
from ffmpeg_builder.wsl_mingw_preparation import (
    project_material_build_inputs,
    resolve_wsl_mingw_project_toolchains,
)


def _target_manifest():
    sources = {
        name: {"version": "1.0", "sha256": str(index) * 64}
        for index, name in enumerate(("nv-codec", "vulkan-headers", "opencl-headers"), 1)
    }
    interfaces = [
        {
            "dll": "vulkan-1.dll",
            "definition_source_component": "vulkan-headers",
            "definition_source_version": "1.0",
            "definition_source_sha256": "2" * 64,
            "definition_path": "registry/vk.xml",
            "definition_format": "vulkan_registry",
            "symbols": ["vkGetInstanceProcAddr"],
            "definition_sha256": "a" * 64,
            "recipe": "llvm-dlltool-x86_64-v1",
            "output": "lib/libvulkan.a",
            "output_sha256": "b" * 64,
        },
        {
            "dll": "OpenCL.dll",
            "definition_source_component": "opencl-icd-loader",
            "definition_source_version": "1.0",
            "definition_source_sha256": "c" * 64,
            "definition_path": "loader/windows/OpenCL.def",
            "definition_format": "def",
            "symbols": ["clGetPlatformIDs"],
            "definition_sha256": "d" * 64,
            "recipe": "llvm-dlltool-x86_64-v1",
            "output": "lib/libOpenCL.a",
            "output_sha256": "e" * 64,
        },
    ]
    identity = {
        "target_backend": TARGET_BACKEND_CROSS_MINGW,
        "sources": sources,
        "interfaces": interfaces,
        "files": {
            "lib/libvulkan.a": "b" * 64,
            "lib/libOpenCL.a": "e" * 64,
        },
    }
    materialization_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**identity, "materialization_id": materialization_id}


def _resolved_profiles(prefix, rust_extras):
    return {
        "llvm-mingw": {
            "profile": "llvm-mingw",
            "version": "20261104",
            "prefix": str(prefix / "llvm-mingw"),
            "paths": {"CC": str(prefix / "llvm-mingw/bin/clang")},
            "target": {"triple": "x86_64-w64-mingw32", "runtime": "ucrt"},
            "provenance": {
                "profile": "llvm-mingw",
                "version": "20261104",
                "variant": "binary",
                "archive_filename": "llvm-mingw.tar.xz",
                "source_sha256": "f" * 64,
                "runtime": "ucrt",
                "schema_version": 2,
                "extras": [],
                "verified": True,
            },
        },
        "rust": {
            "profile": "rust",
            "version": "1.99.0",
            "prefix": str(prefix / "rust"),
            "paths": {},
            "target": None,
            "extras": rust_extras,
            "provenance": {
                "profile": "rust",
                "version": "1.99.0",
                "variant": "build",
                "archive_filename": "rust.tar.gz",
                "source_sha256": "a" * 64,
                "runtime": "",
                "schema_version": 2,
                "extras": rust_extras,
                "verified": True,
            },
        },
    }


@dataclass(frozen=True)
class _Target:
    triple: str
    arch: str
    family: str
    runtime: str


class _Registry:
    def required_toolchains_for(self, ffmpeg_version, platform, backend):
        assert ffmpeg_version == "8.1"
        assert platform == "linux"
        assert backend == "linux-cross-mingw"
        return ["llvm-mingw", "rust"]

    def get_profile(self, name):
        target = (
            _Target("x86_64-w64-mingw32", "x86_64", "mingw-w64", "ucrt")
            if name == "llvm-mingw"
            else None
        )
        return type("Profile", (), {"target": target})()


class _Store:
    def __init__(self, root, status=STATUS_COMPATIBLE):
        self.root = root
        self.status = status

    def installed_status(self, registry, name):
        return self.status, "fixture status"

    def describe(self, registry, name):
        prefix = self.root / name
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        return {
            "version": "1.0",
            "prefix": str(prefix),
            "extras": (
                [{"kind": "rust-std", "target": "x86_64-pc-windows-gnullvm"}]
                if name == "rust"
                else []
            ),
            "base_sha256": "fixture",
            "verified": True,
        }

    def profile_paths(self, registry, name):
        bin_dir = self.root / name / "bin"
        paths = {"BIN_DIR": str(bin_dir)}
        if name == "llvm-mingw":
            wrapper = bin_dir / "clang-target-wrapper.sh"
            wrapper.write_text("wrapper")
            for key, executable in (
                ("CC", "x86_64-w64-mingw32-clang"),
                ("CXX", "x86_64-w64-mingw32-clang++"),
            ):
                entrypoint = bin_dir / executable
                entrypoint.symlink_to(wrapper.name)
                paths[key] = str(entrypoint)
        return paths


def test_required_project_toolchains_are_resolved_with_target_and_extras(tmp_path):
    resolved = resolve_wsl_mingw_project_toolchains("8.1", _Registry(), _Store(tmp_path))

    assert resolved["llvm-mingw"]["target"]["triple"] == "x86_64-w64-mingw32"
    assert resolved["llvm-mingw"]["paths"]["BIN_DIR"] == str(tmp_path / "llvm-mingw" / "bin")
    assert resolved["llvm-mingw"]["paths"]["CC"].endswith("/x86_64-w64-mingw32-clang")
    assert resolved["llvm-mingw"]["paths"]["CXX"].endswith("/x86_64-w64-mingw32-clang++")
    assert resolved["rust"]["extras"][0]["target"] == "x86_64-pc-windows-gnullvm"
    assert resolved["rust"]["provenance"]["verified"] is True


def test_unavailable_required_project_toolchain_fails_preparation(tmp_path):
    with pytest.raises(BuildError) as raised:
        resolve_wsl_mingw_project_toolchains("8.1", _Registry(), _Store(tmp_path, "absent"))

    assert raised.value.category == CATEGORY_TOOLCHAIN_INSTALL
    assert "Acquire or verify" in str(raised.value)


def test_project_toolchain_entrypoint_cannot_resolve_outside_prefix(tmp_path):
    class EscapingStore(_Store):
        def profile_paths(self, registry, name):
            external = self.root / "outside"
            external.write_text("outside")
            entrypoint = self.root / name / "bin" / "compiler"
            entrypoint.symlink_to(external)
            return {"CC": str(entrypoint)}

    with pytest.raises(BuildError) as raised:
        resolve_wsl_mingw_project_toolchains("8.1", _Registry(), EscapingStore(tmp_path))

    assert raised.value.code == "wsl_mingw_toolchain_paths_invalid"


def test_material_projection_is_relocation_safe_and_sorts_rust_extras(tmp_path):
    rust_std = {
        "kind": "rust-std",
        "target": "x86_64-pc-windows-gnullvm",
        "release": "1.99.0",
        "component": "rust-std-x86_64-pc-windows-gnullvm",
        "archive_filename": "rust-std.tar.xz",
        "sha256": "1" * 64,
    }
    other_extra = {
        "kind": "rust-docs",
        "target": "x86_64-unknown-linux-gnu",
        "release": "1.99.0",
        "component": "rust-docs",
        "archive_filename": "rust-docs.tar.xz",
        "sha256": "2" * 64,
    }
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        cuda_build_mode="disabled",
        enable_libvmaf=False,
        enable_libvmaf_cuda=False,
    )
    manifest = _target_manifest()
    first = project_material_build_inputs(
        config,
        [],
        _resolved_profiles(tmp_path / "store-a", [rust_std, other_extra]),
        None,
        None,
        manifest,
    )
    second = project_material_build_inputs(
        config,
        [],
        _resolved_profiles(tmp_path / "store-b", [other_extra, rust_std]),
        None,
        None,
        manifest,
    )
    reordered_manifest = {
        **manifest,
        "sources": dict(reversed(list(manifest["sources"].items()))),
    }
    third = project_material_build_inputs(
        config,
        [],
        _resolved_profiles(tmp_path / "store-c", [rust_std, other_extra]),
        None,
        None,
        reordered_manifest,
    )

    assert first.to_dict() == second.to_dict()
    assert json.dumps(first.to_dict()) == json.dumps(third.to_dict())
    assert first.toolchains["rust_target_std"]["target"] == "x86_64-pc-windows-gnullvm"
    assert first.toolchains["llvm_mingw"]["provenance"]["source_sha256"] == "f" * 64
    assert "store-a" not in json.dumps(first.to_dict())
    assert "store-b" not in json.dumps(second.to_dict())


def test_material_projection_rejects_mismatched_rust_target_std(tmp_path):
    extras = [
        {
            "kind": "rust-std",
            "target": "x86_64-pc-windows-gnullvm",
            "release": "1.98.0",
            "component": "rust-std",
            "archive_filename": "rust-std.tar.xz",
            "sha256": "1" * 64,
        }
    ]
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        cuda_build_mode="disabled",
        enable_libvmaf=False,
        enable_libvmaf_cuda=False,
    )
    with pytest.raises(BuildError, match="matching x86_64-pc-windows-gnullvm"):
        project_material_build_inputs(
            config,
            [],
            _resolved_profiles(tmp_path, extras),
            None,
            None,
            _target_manifest(),
        )
