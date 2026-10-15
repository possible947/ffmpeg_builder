"""Tests for the persistent toolchain store and acquisition."""

import io
import tarfile
from pathlib import Path

import pytest
import yaml

from ffmpeg_builder import toolchain_builders
from ffmpeg_builder.toolchain_registry import ToolchainRegistry
from ffmpeg_builder.toolchain_store import ToolchainStore

_SHA = "a" * 64


def _make_tar(archive_path: Path, members: dict) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as tar:
        for name, content in members.items():
            payload = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


def _write_registry(tmp_path: Path, toolchains: dict) -> ToolchainRegistry:
    path = tmp_path / "toolchains.yaml"
    path.write_text(yaml.safe_dump({"toolchains": toolchains}), encoding="utf-8")
    return ToolchainRegistry(path)


def _binary_profile(version: str = "1.0") -> dict:
    filename = f"bootstrap-{version}.tar.gz"
    return {
        "kind": "compiler",
        "platforms": ["linux"],
        "version": version,
        "source": {
            "default": "binary",
            "binary": {
                "type": "binary",
                "url": f"https://example.org/{filename}",
                "archive_filename": filename,
                "sha256": _SHA,
            },
        },
    }


def _store(tmp_path: Path, **kwargs) -> ToolchainStore:
    return ToolchainStore(
        tmp_path / "store",
        workspace=tmp_path / "workspace",
        source_archives_dir=tmp_path / "sources",
        **kwargs,
    )


class TestLayoutAndState:
    def test_ensure_layout_creates_subdirs(self, tmp_path):
        store = _store(tmp_path)
        store.ensure_layout()
        for sub in ("archives", "installed", "logs", "locks", "profiles", "build"):
            assert (store.root / sub).is_dir()
        assert store.state_path.parent == store.root

    def test_state_roundtrip(self, tmp_path):
        store = _store(tmp_path)
        store.ensure_layout()
        state = store.load_state()
        assert state.profiles == {}

    def test_lock_context(self, tmp_path):
        store = _store(tmp_path)
        with store.lock("gcc12"):
            pass
        assert (store.locks_dir / "gcc12.lock").exists()


class TestPathGuards:
    def test_store_inside_workspace_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="must not overlap"):
            ToolchainStore(tmp_path / "workspace" / "toolchains", workspace=tmp_path / "workspace")

    def test_workspace_inside_store_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="must not overlap"):
            ToolchainStore(tmp_path, workspace=tmp_path / "workspace")


class TestAcquisition:
    def test_acquire_binary_extracts_archive(self, tmp_path):
        registry = _write_registry(tmp_path, {"bootstrap": _binary_profile()})
        _make_tar(
            tmp_path / "sources" / "bootstrap-1.0.tar.gz",
            {"bootstrap-1.0/bin/tool": "#!/bin/sh\n"},
        )
        store = _store(tmp_path)
        installed = store.acquire(registry, "bootstrap")

        assert installed.variant == "binary"
        assert (store.installed_dir / "bootstrap-1.0" / "bin" / "tool").is_file()
        assert store.installed("bootstrap") == installed

    def test_acquire_local_copies_directory(self, tmp_path):
        local_dir = tmp_path / "local-toolchain"
        (local_dir / "bin").mkdir(parents=True)
        (local_dir / "bin" / "cc").write_text("#!/bin/sh\n")
        registry = _write_registry(
            tmp_path,
            {
                "localcc": {
                    "kind": "compiler",
                    "platforms": ["linux"],
                    "version": "2.0",
                    "source": {
                        "default": "local",
                        "local": {"type": "local", "path": str(local_dir)},
                    },
                }
            },
        )
        store = _store(tmp_path)
        store.acquire(registry, "localcc")
        assert (store.installed_dir / "localcc-2.0" / "bin" / "cc").is_file()

    def test_acquire_unknown_profile_rejected(self, tmp_path):
        registry = _write_registry(tmp_path, {})
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="Unknown toolchain profile"):
            store.acquire(registry, "nope")

    def test_acquire_not_deployable_rejected(self, tmp_path):
        registry = _write_registry(tmp_path, {"bare": {"kind": "compiler", "platforms": ["linux"]}})
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="not deployable"):
            store.acquire(registry, "bare")

    def test_missing_archive_offline_rejected(self, tmp_path):
        registry = _write_registry(tmp_path, {"bootstrap": _binary_profile()})
        store = _store(tmp_path)
        with pytest.raises(FileNotFoundError, match="network downloads are disabled"):
            store.acquire(registry, "bootstrap")

    def test_remove_installed_profile(self, tmp_path):
        registry = _write_registry(tmp_path, {"bootstrap": _binary_profile()})
        _make_tar(
            tmp_path / "sources" / "bootstrap-1.0.tar.gz",
            {"bootstrap-1.0/bin/tool": "#!/bin/sh\n"},
        )
        store = _store(tmp_path)
        store.acquire(registry, "bootstrap")
        assert store.remove("bootstrap") is True
        assert store.installed("bootstrap") is None
        assert not (store.installed_dir / "bootstrap-1.0").exists()
        assert store.remove("bootstrap") is False


def _recording_runner(calls: list):
    def run(command, cwd, log_file):
        calls.append((list(command), str(cwd)))

    return run


class TestBuildAcquisition:
    def test_build_gcc_links_gmp_prerequisite(self, tmp_path):
        registry = _write_registry(
            tmp_path,
            {
                "gmp": {
                    "kind": "bootstrap_lib",
                    "platforms": ["linux"],
                    "version": "6.0",
                    "source": {
                        "default": "build",
                        "build": {
                            "type": "build",
                            "recipe": "autotools",
                            "url": "https://example.org/gmp-6.0.tar.gz",
                            "archive_filename": "gmp-6.0.tar.gz",
                            "sha256": _SHA,
                        },
                    },
                },
                "gcc12": {
                    "kind": "compiler",
                    "platforms": ["linux"],
                    "version": "12.1",
                    "source": {
                        "default": "build",
                        "build": {
                            "type": "build",
                            "recipe": "gcc",
                            "url": "https://example.org/gcc-12.1.tar.gz",
                            "archive_filename": "gcc-12.1.tar.gz",
                            "sha256": _SHA,
                            "bootstrap": {"compiler": "system", "requires": ["gmp"]},
                        },
                    },
                },
            },
        )
        _make_tar(tmp_path / "sources" / "gmp-6.0.tar.gz", {"gmp-6.0/configure": "#!/bin/sh\n"})
        _make_tar(tmp_path / "sources" / "gcc-12.1.tar.gz", {"gcc-12.1/configure": "#!/bin/sh\n"})

        store = _store(tmp_path)
        calls: list = []
        installed = store.acquire(registry, "gcc12", runner=_recording_runner(calls))

        assert installed.profile == "gcc12"
        gcc_configure = [cmd for cmd, _ in calls if "--enable-languages=c,c++" in cmd]
        assert gcc_configure, calls
        assert any(arg.startswith("--with-gmp=") for arg in gcc_configure[0])
        # GMP was built first and is recorded in the store.
        assert store.installed("gmp") is not None


class TestBuilders:
    def test_build_autotools_runs_configure_make_install(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        (source_dir / "configure").write_text("#!/bin/sh\n")
        calls: list = []
        toolchain_builders.build_autotools(
            source_dir, tmp_path / "prefix", tmp_path / "logs", _recording_runner(calls), [], [], []
        )
        assert calls[0][0][0] == "./configure"
        assert calls[1][0][:1] == ["make"]
        assert calls[2][0][:2] == ["make", "install"]

    def test_build_autotools_missing_configure_raises(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            toolchain_builders.build_autotools(
                source_dir,
                tmp_path / "prefix",
                tmp_path / "logs",
                _recording_runner([]),
                [],
                [],
                [],
            )


class TestComponentEnv:
    def test_none_when_not_installed(self, tmp_path):
        registry = _write_registry(
            tmp_path,
            {
                "gcc12": {
                    "kind": "compiler",
                    "platforms": ["linux"],
                    "materialize": {"bin_subdir": "bin"},
                }
            },
        )
        store = _store(tmp_path)
        assert store.component_env(registry, "gcc12") is None

    def test_builds_path_and_compiler_overrides(self, tmp_path):
        local_dir = tmp_path / "tc"
        (local_dir / "bin").mkdir(parents=True)
        registry = _write_registry(
            tmp_path,
            {
                "gcc12": {
                    "kind": "compiler",
                    "platforms": ["linux"],
                    "version": "12.1",
                    "materialize": {"bin_subdir": "bin", "cc": "bin/gcc", "cxx": "bin/g++"},
                    "source": {
                        "default": "local",
                        "local": {"type": "local", "path": str(local_dir)},
                    },
                }
            },
        )
        store = _store(tmp_path)
        store.acquire(registry, "gcc12")
        env = store.component_env(registry, "gcc12")
        assert env is not None
        assert env["PATH_PREPEND"].endswith("/bin")
        assert env["CC"].endswith("/bin/gcc")
        assert env["CXX"].endswith("/bin/g++")


class TestCudaHostCompiler:
    def _registry(self, tmp_path: Path, cuda_table: dict) -> ToolchainRegistry:
        path = tmp_path / "toolchains.yaml"
        data = {
            "toolchains": {
                "gcc12": {
                    "kind": "compiler",
                    "platforms": ["linux"],
                    "version": "12.1",
                    "provides": ["gcc", "g++"],
                    "materialize": {"bin_subdir": "bin", "cc": "bin/gcc"},
                    "source": {
                        "default": "local",
                        "local": {"type": "local", "path": str(tmp_path / "tc")},
                    },
                }
            },
            "cuda": dict(cuda_table),
        }
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return ToolchainRegistry(path)

    def _store_with_gcc(self, tmp_path: Path) -> ToolchainStore:
        local_dir = tmp_path / "tc"
        (local_dir / "bin").mkdir(parents=True)
        (local_dir / "bin" / "gcc").write_text("#!/bin/sh\n")
        store = _store(tmp_path)
        registry = self._registry(tmp_path, {"12.2": "gcc12"})
        store.acquire(registry, "gcc12")
        return store

    def test_mapped_and_installed_resolves_cc(self, tmp_path):
        store = self._store_with_gcc(tmp_path)
        registry = self._registry(tmp_path, {"12.2": "gcc12"})
        cc = store.cuda_host_compiler(registry, "12.2")
        assert cc is not None and cc.endswith("/bin/gcc")

    def test_unmapped_version_resolves_none(self, tmp_path):
        store = self._store_with_gcc(tmp_path)
        registry = self._registry(tmp_path, {"12.2": "gcc12"})
        assert store.cuda_host_compiler(registry, "99.9") is None

    def test_missing_version_resolves_none(self, tmp_path):
        store = self._store_with_gcc(tmp_path)
        registry = self._registry(tmp_path, {"12.2": "gcc12"})
        assert store.cuda_host_compiler(registry, None) is None

    def test_mapped_but_not_installed_resolves_none(self, tmp_path):
        store = _store(tmp_path)
        registry = self._registry(tmp_path, {"12.2": "gcc12"})
        assert store.cuda_host_compiler(registry, "12.2") is None
