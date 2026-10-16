"""Tests for scripts/prepare_toolchains.py (headless toolchain provisioning)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import yaml

from ffmpeg_builder.toolchain_registry import ToolchainRegistry

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "prepare_toolchains", ROOT / "scripts" / "prepare_toolchains.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare_toolchains = _load_module()


def _args(**overrides) -> SimpleNamespace:
    defaults = {
        "profiles": None,
        "all": False,
        "ffmpeg_version": None,
        "platform": None,
        "jobs": None,
        "store": None,
        "allow_network": False,
        "dry_run": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _registry(tmp_path: Path) -> ToolchainRegistry:
    data = {
        "toolchains": {
            "gmp": {
                "kind": "bootstrap_lib",
                "platforms": ["linux"],
                "source": {
                    "default": "build",
                    "build": {
                        "type": "build",
                        "recipe": "autotools",
                        "url": "https://example.org/gmp.tar.gz",
                        "sha256": "a" * 64,
                    },
                },
            },
            "gcc12": {
                "kind": "compiler",
                "platforms": ["linux"],
                "source": {
                    "default": "build",
                    "build": {
                        "type": "build",
                        "recipe": "gcc",
                        "url": "https://example.org/gcc.tar.gz",
                        "sha256": "b" * 64,
                    },
                },
            },
            "darwin-only": {
                "kind": "compiler",
                "platforms": ["darwin"],
                "source": {
                    "default": "build",
                    "build": {
                        "type": "build",
                        "recipe": "gcc",
                        "url": "https://example.org/darwin.tar.gz",
                        "sha256": "c" * 64,
                    },
                },
            },
        },
        "platform_profiles": {
            "linux-ffmpeg-8.1": {
                "ffmpeg_version": "8.1",
                "platform": "linux",
                "system_tool_range": {"gcc": ">=12,<14"},
                "fallback_toolchain": "gcc12",
            },
        },
    }
    path = tmp_path / "toolchains.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return ToolchainRegistry(path)


def test_explicit_profiles_preserve_given_order(tmp_path):
    selected = prepare_toolchains.select_profiles(
        _registry(tmp_path),
        _args(profiles=["gcc12", "gmp"]),
        "linux",
        "8.1",
        {"gcc": "16"},
        [],
        None,
    )
    assert selected == ["gcc12", "gmp"]


def test_all_selects_deployable_profiles_for_the_platform(tmp_path):
    selected = prepare_toolchains.select_profiles(
        _registry(tmp_path), _args(all=True), "linux", "8.1", {"gcc": "16"}, [], None
    )
    assert selected == ["gcc12", "gmp"]


def test_default_uses_system_analysis_recommendations(tmp_path):
    selected = prepare_toolchains.select_profiles(
        _registry(tmp_path), _args(), "linux", "8.1", {"gcc": "16"}, [], None
    )
    assert selected == ["gcc12"]


def test_default_is_empty_when_system_toolchain_is_in_range(tmp_path):
    selected = prepare_toolchains.select_profiles(
        _registry(tmp_path), _args(), "linux", "8.1", {"gcc": "13"}, [], None
    )
    assert selected == []


def test_main_dry_run_reports_order_without_building(capsys):
    exit_code = prepare_toolchains.main(
        ["--dry-run", "--platform", "linux", "--ffmpeg-version", "9.0"]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Acquisition order" in output


def _make_lib(root: Path) -> None:
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "include" / "lib.h").write_text("/* probe */\n", encoding="utf-8")
    (root / "lib" / "libprobe.a").write_text("", encoding="utf-8")


def _local_registry(tmp_path: Path) -> ToolchainRegistry:
    local_dir = tmp_path / "local-cc"
    _make_lib(local_dir)
    (local_dir / "bin").mkdir(parents=True, exist_ok=True)
    (local_dir / "bin" / "cc").write_text("#!/bin/sh\n", encoding="utf-8")
    path = tmp_path / "toolchains.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "toolchains": {
                    "goodcc": {
                        "kind": "compiler",
                        "platforms": ["linux"],
                        "version": "1.0",
                        "source": {
                            "default": "build",
                            "build": {
                                "type": "build",
                                "recipe": "autotools",
                                "url": "https://example.org/lib.tar.gz",
                                "sha256": "a" * 64,
                            },
                        },
                    },
                    "brokencc": {
                        "kind": "compiler",
                        "platforms": ["linux"],
                        "version": "1.0",
                        "source": {
                            "default": "build",
                            "build": {
                                "type": "build",
                                "recipe": "autotools",
                                "url": "https://example.org/lib.tar.gz",
                                "sha256": "a" * 64,
                            },
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return ToolchainRegistry(path)


def test_verify_installed_reports_ok_and_failures(tmp_path, capsys, monkeypatch):
    import shutil

    from ffmpeg_builder.toolchain_store import InstalledToolchain, ToolchainState, ToolchainStore

    registry = _local_registry(tmp_path)
    store = ToolchainStore(tmp_path / "store")
    good = tmp_path / "local-cc"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "include").mkdir()
    # broken has headers but no library archive, so verification fails.
    store.save_state(
        ToolchainState(
            profiles={
                "goodcc": InstalledToolchain(
                    profile="goodcc",
                    version="1.0",
                    variant="local",
                    path=str(good),
                    installed_at="2026-01-01T00:00:00",
                ),
                "brokencc": InstalledToolchain(
                    profile="brokencc",
                    version="1.0",
                    variant="local",
                    path=str(broken),
                    installed_at="2026-01-01T00:00:00",
                ),
            }
        )
    )

    monkeypatch.setattr(prepare_toolchains, "ToolchainRegistry", lambda *a, **k: registry)

    exit_code = prepare_toolchains.verify_installed(store)
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ok     goodcc" in output
    assert "FAILED brokencc" in output
    assert store.load_state().profiles["brokencc"].verified is False
    assert store.load_state().profiles["goodcc"].verified is True

    # Rewriting through a moved store adopts the new root.
    relocated = tmp_path / "store2"
    relocated_good = relocated / "installed" / good.name
    relocated_broken = relocated / "installed" / broken.name
    relocated_good.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(good, relocated_good, dirs_exist_ok=True)
    shutil.copytree(broken, relocated_broken, dirs_exist_ok=True)
    # Repair the broken install at its relocated path so adoption succeeds.
    (relocated_broken / "lib").mkdir(parents=True, exist_ok=True)
    (relocated_broken / "lib" / "libprobe.a").write_text("", encoding="utf-8")
    fixed_state = store.load_state()
    fixed_state.profiles["goodcc"].path = str(relocated_good)
    fixed_state.profiles["brokencc"].path = str(relocated_broken)
    fixed_state.profiles["brokencc"].verified = True
    store.save_state(fixed_state)
    shutil.copy(store.state_path, relocated / "state.json")
    # The originals no longer exist, forcing resolution through the new root.
    shutil.rmtree(good)
    shutil.rmtree(broken)
    store2 = ToolchainStore(relocated)
    assert prepare_toolchains.verify_installed(store2, adopt_relocated=True) == 0
    assert store2.load_state().profiles["goodcc"].path == str(relocated_good)
    assert store2.load_state().profiles["brokencc"].path == str(relocated_broken)


def test_verify_installed_without_entries(tmp_path, capsys):
    from ffmpeg_builder.toolchain_store import ToolchainStore

    store = ToolchainStore(tmp_path / "store")
    assert prepare_toolchains.verify_installed(store) == 0
    assert "Nothing installed" in capsys.readouterr().out


def test_parse_args_accepts_verify_only():
    args = prepare_toolchains.parse_args(["--verify-only"])
    assert args.verify_only is True
