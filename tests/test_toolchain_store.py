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


def _skip_verification(recipe, prefix):  # noqa: ARG001 - verifier contract
    """Verifier stub for tests that only record commands (no real binaries)."""
    return []


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
        installed = store.acquire(
            registry,
            "gcc12",
            runner=_recording_runner(calls),
            verifier=_skip_verification,
        )

        assert installed.profile == "gcc12"
        gcc_configure = [cmd for cmd, _ in calls if "--enable-languages=c,c++" in cmd]
        assert gcc_configure, calls
        assert any(arg.startswith("--with-gmp=") for arg in gcc_configure[0])
        # GMP was built first and is recorded in the store.
        assert store.installed("gmp") is not None

    def test_build_resolves_jobs_and_prerequisite_placeholders(self, tmp_path):
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
                            "build_args": ["-j{jobs}"],
                        },
                    },
                },
                "mpfr": {
                    "kind": "bootstrap_lib",
                    "platforms": ["linux"],
                    "version": "4.2",
                    "source": {
                        "default": "build",
                        "build": {
                            "type": "build",
                            "recipe": "autotools",
                            "url": "https://example.org/mpfr-4.2.tar.gz",
                            "archive_filename": "mpfr-4.2.tar.gz",
                            "sha256": _SHA,
                            "bootstrap": {"compiler": "system", "requires": ["gmp"]},
                            "configure_args": ["--with-gmp={prereq:gmp}"],
                            "build_args": ["-j{jobs}"],
                        },
                    },
                },
            },
        )
        _make_tar(tmp_path / "sources" / "gmp-6.0.tar.gz", {"gmp-6.0/configure": "#!/bin/sh\n"})
        _make_tar(tmp_path / "sources" / "mpfr-4.2.tar.gz", {"mpfr-4.2/configure": "#!/bin/sh\n"})

        store = _store(tmp_path)
        calls: list = []
        store.acquire(
            registry, "mpfr", runner=_recording_runner(calls), jobs=3, verifier=_skip_verification
        )

        make_calls = [cmd for cmd, _ in calls if cmd[:1] == ["make"]]
        assert make_calls and all("-j3" in cmd for cmd in make_calls if cmd[1:2] != ["install"])
        mpfr_configure = [cmd for cmd, _ in calls if any(a.startswith("--with-gmp=") for a in cmd)]
        assert mpfr_configure, calls
        expected = str(store.installed_dir / "gmp-6.0")
        assert f"--with-gmp={expected}" in mpfr_configure[0]

    def test_verification_failure_is_not_recorded(self, tmp_path):
        registry = _write_registry(
            tmp_path,
            {
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
                        },
                    },
                },
            },
        )
        _make_tar(tmp_path / "sources" / "gcc-12.1.tar.gz", {"gcc-12.1/configure": "#!/bin/sh\n"})
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match="verification failed"):
            store.acquire(
                registry,
                "gcc12",
                runner=_recording_runner([]),
                verifier=lambda recipe, prefix: ["C compile/link failed"],
            )

        assert store.installed("gcc12") is None

    def test_verified_flag_is_stamped_and_gates_env(self, tmp_path):
        registry = _write_registry(
            tmp_path,
            {
                "gcc12": {
                    "kind": "compiler",
                    "platforms": ["linux"],
                    "version": "12.1",
                    "materialize": {"bin_subdir": "bin", "cc": "bin/gcc"},
                    "source": {
                        "default": "build",
                        "build": {
                            "type": "build",
                            "recipe": "gcc",
                            "url": "https://example.org/gcc-12.1.tar.gz",
                            "archive_filename": "gcc-12.1.tar.gz",
                            "sha256": _SHA,
                        },
                    },
                },
            },
        )
        _make_tar(tmp_path / "sources" / "gcc-12.1.tar.gz", {"gcc-12.1/configure": "#!/bin/sh\n"})
        store = _store(tmp_path)
        installed = store.acquire(
            registry, "gcc12", runner=_recording_runner([]), verifier=_skip_verification
        )
        assert installed.verified is True
        assert store.component_env(registry, "gcc12") is not None

        state = store.load_state()
        state.profiles["gcc12"].verified = False
        store.save_state(state)

        assert store.component_env(registry, "gcc12") is None
        assert store.profile_paths(registry, "gcc12") == {}

    def test_legacy_state_without_verified_flag_is_trusted(self, tmp_path):
        store = _store(tmp_path)
        store.ensure_layout()
        store.state_path.write_text(
            "profiles:\n"
            "  gcc12:\n"
            "    profile: gcc12\n"
            "    version: '12.1'\n"
            "    variant: build\n"
            "    path: /tmp/whatever\n"
            "    installed_at: '2026-01-01T00:00:00'\n",
            encoding="utf-8",
        )
        entry = store.installed("gcc12")
        assert entry is not None and entry.verified is True


class TestRelocation:
    """Store relocation: resolve entries through a moved store root."""

    def _state_with_lib(self, tmp_path: Path) -> tuple:
        from ffmpeg_builder.toolchain_store import InstalledToolchain, ToolchainState

        origin = tmp_path / "probe-1.0"
        lib = origin / "lib"
        (origin / "include").mkdir(parents=True)
        (origin / "include" / "lib.h").write_text("/* probe */\n", encoding="utf-8")
        lib.mkdir(parents=True)
        (lib / "libprobe.a").write_text("", encoding="utf-8")
        store = _store(tmp_path)
        entry = InstalledToolchain(
            profile="probe",
            version="1.0",
            variant="local",
            path=str(origin),
            installed_at="2026-01-01T00:00:00",
        )
        store.save_state(ToolchainState(profiles={"probe": entry}))
        return store, origin, entry

    def _autotools_profile(self, name: str = "probe"):
        local_dir = Path("/nonexistent")  # unused: entries are pre-recorded
        _ = local_dir
        return {
            name: {
                "kind": "bootstrap_lib",
                "platforms": ["linux"],
                "version": "1.0",
                "source": {
                    "default": "build",
                    "build": {
                        "type": "build",
                        "recipe": "autotools",
                        "url": "https://example.org/lib.tar.gz",
                        "sha256": _SHA,
                    },
                },
            }
        }

    def test_recorded_path_wins_when_present(self, tmp_path):
        store, origin, entry = self._state_with_lib(tmp_path)
        assert store.resolve_entry_path(entry) == origin

    def test_missing_recorded_path_falls_back_to_new_root(self, tmp_path):
        import shutil

        store, origin, entry = self._state_with_lib(tmp_path)
        moved = tmp_path / "moved"
        moved_installed = moved / "installed" / origin.name
        moved_installed.parent.mkdir(parents=True)
        shutil.copytree(origin, moved_installed, dirs_exist_ok=True)
        shutil.copy(store.state_path, moved / "state.json")
        # The original is gone: only the moved copy can satisfy the entry.
        shutil.rmtree(origin)
        moved_store = ToolchainStore(moved)
        assert moved_store.resolve_entry_path(entry) == moved_installed

    def test_reverify_marks_failures_and_adopt_rewrites_paths(self, tmp_path):
        import shutil

        store, origin, _entry = self._state_with_lib(tmp_path)
        registry = _write_registry(tmp_path, self._autotools_profile())
        moved = tmp_path / "moved"
        moved_installed = moved / "installed" / origin.name
        moved_installed.parent.mkdir(parents=True)
        shutil.copytree(origin, moved_installed, dirs_exist_ok=True)
        shutil.copy(store.state_path, moved / "state.json")
        # The original is gone: verification must resolve through the new root.
        shutil.rmtree(origin)
        moved_store = ToolchainStore(moved)

        assert moved_store.reverify(registry) == []
        # Not adopted yet: the recorded path still points at the origin.
        assert moved_store.load_state().profiles["probe"].path == str(origin)

        assert moved_store.reverify(registry, adopt_relocated=True) == []
        assert moved_store.load_state().profiles["probe"].path == str(moved_installed)

    def test_reverify_marks_missing_prefix_unverified(self, tmp_path):
        from ffmpeg_builder.toolchain_store import InstalledToolchain, ToolchainState

        store = _store(tmp_path)
        store.save_state(
            ToolchainState(
                profiles={
                    "probe": InstalledToolchain(
                        profile="probe",
                        version="1.0",
                        variant="local",
                        path=str(tmp_path / "gone"),
                        installed_at="2026-01-01T00:00:00",
                    )
                }
            )
        )
        registry = _write_registry(tmp_path, self._autotools_profile())
        assert store.reverify(registry) == ["probe"]
        assert store.load_state().profiles["probe"].verified is False
        # Filtered from consumers until re-acquired.
        assert store.installed("probe") is None

    def test_mark_unverified_keeps_entry_but_hides_it(self, tmp_path):
        store, _origin, _entry = self._state_with_lib(tmp_path)
        store.mark_unverified("probe")
        assert store.load_state().profiles["probe"].verified is False
        assert store.installed("probe") is None


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

    def test_build_rust_standalone_runs_installer(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        (source_dir / "install.sh").write_text("#!/bin/sh\n")
        calls: list = []
        toolchain_builders.build_rust_standalone(
            source_dir, tmp_path / "prefix", tmp_path / "logs", _recording_runner(calls)
        )
        assert calls[0][0][0] == "sh"
        assert str(calls[0][0][1]).endswith("install.sh")
        assert any(arg.startswith("--prefix=") for arg in calls[0][0])

    def test_build_rust_standalone_missing_installer_raises(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            toolchain_builders.build_rust_standalone(
                source_dir, tmp_path / "prefix", tmp_path / "logs", _recording_runner([])
            )


class TestArgResolution:
    def test_expands_prefix_jobs_and_prerequisites(self, tmp_path):
        resolved = toolchain_builders.resolve_args(
            ["--with-gmp={prereq:gmp}", "--prefix={prefix}", "-j{jobs}"],
            tmp_path / "prefix",
            {"gmp": tmp_path / "installed" / "gmp-6.3.0"},
            jobs=8,
        )
        assert resolved == [
            f"--with-gmp={tmp_path / 'installed' / 'gmp-6.3.0'}",
            f"--prefix={tmp_path / 'prefix'}",
            "-j8",
        ]

    def test_unknown_prerequisite_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not installed in the store"):
            toolchain_builders.resolve_args(["{prereq:gmp}"], tmp_path, {}, jobs=1)

    def test_clean_env_drops_build_and_vendor_flags(self, monkeypatch):
        monkeypatch.setenv("CFLAGS", "-O3 -march=native")
        monkeypatch.setenv("RUSTFLAGS", "-C target-cpu=native")
        monkeypatch.setenv("CUDA_HOME", "/opt/cuda")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = toolchain_builders.clean_env()
        assert "CFLAGS" not in env
        assert "RUSTFLAGS" not in env
        assert "CUDA_HOME" not in env
        assert env["PATH"] == "/usr/bin"


class TestVerification:
    def _fake_tool(self, prefix: Path, name: str, exit_code: int) -> None:
        bin_dir = prefix / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        tool = bin_dir / name
        tool.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        tool.chmod(0o755)

    def test_unknown_recipe_requires_binaries(self, tmp_path):
        prefix = tmp_path / "p"
        prefix.mkdir()
        assert toolchain_builders.verify_profile(None, prefix)
        self._fake_tool(prefix, "tool", 0)
        assert toolchain_builders.verify_profile(None, prefix) == []

    def test_autotools_requires_headers_and_archive(self, tmp_path):
        prefix = tmp_path / "p"
        (prefix / "include").mkdir(parents=True)
        assert any(
            "no static or shared library" in item
            for item in toolchain_builders.verify_profile("autotools", prefix)
        )

        (prefix / "lib").mkdir()
        (prefix / "lib" / "libfoo.a").write_text("", encoding="utf-8")
        assert toolchain_builders.verify_profile("autotools", prefix) == []

    def test_rust_requires_rustc_and_cargo(self, tmp_path):
        prefix = tmp_path / "p"
        (prefix / "bin").mkdir(parents=True)
        failures = toolchain_builders.verify_profile("rust_standalone", prefix)
        assert any("rustc" in item for item in failures)
        assert any("cargo" in item for item in failures)

        self._fake_tool(prefix, "rustc", 0)
        self._fake_tool(prefix, "cargo", 0)
        assert toolchain_builders.verify_profile("rust_standalone", prefix) == []

    def test_compiler_probe_reports_failing_tools(self, tmp_path):
        prefix = tmp_path / "p"
        self._fake_tool(prefix, "gcc", 1)
        self._fake_tool(prefix, "g++", 0)
        failures = toolchain_builders.verify_profile("gcc", prefix)
        assert failures  # the failing `gcc --version` probe is reported

    def test_compiler_probe_passes_with_working_tools(self, tmp_path):
        prefix = tmp_path / "p"
        self._fake_tool(prefix, "gcc", 0)
        self._fake_tool(prefix, "g++", 0)
        assert toolchain_builders.verify_profile("gcc", prefix) == []

    def test_compiler_missing_binary_is_reported(self, tmp_path):
        prefix = tmp_path / "p"
        prefix.mkdir()
        failures = toolchain_builders.verify_profile("gcc", prefix)
        assert any("missing" in item for item in failures)


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
