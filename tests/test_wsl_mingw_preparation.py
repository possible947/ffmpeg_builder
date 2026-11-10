"""Tests for the structured WSL-Mingw preparation data."""

from dataclasses import dataclass

import pytest

from ffmpeg_builder.build_types import CATEGORY_TOOLCHAIN_INSTALL, BuildError
from ffmpeg_builder.toolchain_store import STATUS_COMPATIBLE
from ffmpeg_builder.wsl_mingw_preparation import resolve_wsl_mingw_project_toolchains


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
