from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders import base as shared_builders
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave1 import (
    G4_WAVE1_COMPONENTS,
    Wave1TargetValidator,
    build_or_reuse_component,
    order_wave_components,
    verify_wave_sources,
)
from ffmpeg_builder.state import ComponentStatus


def _fake_validator(tmp_path: Path) -> Wave1TargetValidator:
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    generated = workspace / "generated"
    prefix.mkdir()
    generated.mkdir(parents=True)
    environment = SimpleNamespace(
        paths=SimpleNamespace(
            target_prefix=prefix,
            workspace_root=workspace,
            generated_files_root=generated,
        ),
        target_tools=SimpleNamespace(readobj=Path("/unused/llvm-readobj")),
        pkg_config_wrapper=Path("/unused/pkg-config"),
        compiler_environment={},
    )
    return Wave1TargetValidator(environment, "identity-test")


def test_wave1_selection_is_exact_and_dependency_ordered():
    registry = ComponentRegistry()
    selected = [registry.get_by_name(name) for name in G4_WAVE1_COMPONENTS]
    assert all(selected)

    ordered = order_wave_components(registry, [c for c in selected if c is not None])

    assert {component.name for component in ordered} == set(G4_WAVE1_COMPONENTS)
    names = [component.name for component in ordered]
    assert names.index("zlib") < names.index("libpng") < names.index("FreeType2")


def test_wave1_rejects_an_unbuilt_dependency_outside_the_requested_subset():
    registry = ComponentRegistry()
    libpng = registry.get_by_name("libpng")
    assert libpng is not None

    with pytest.raises(BuildError, match="has not been accepted"):
        order_wave_components(registry, [libpng], requested_names=("libpng",))


def test_wave_sources_are_local_and_match_registered_checksums():
    registry = ComponentRegistry()

    verified = verify_wave_sources(registry, Path("third_party/sources"))

    assert set(verified) == set(G4_WAVE1_COMPONENTS)
    assert all(len(checksum) == 64 for checksum in verified.values())


def test_zlib_cross_configure_uses_explicit_tools_without_autoconf_tuples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    registry = ComponentRegistry()
    zlib = registry.get_by_name("zlib")
    assert zlib is not None
    calls = []

    class FakeBuilder:
        platform = "linux"
        num_jobs = 2
        platform_context = SimpleNamespace(
            cross_environment=SimpleNamespace(
                autotools=SimpleNamespace(
                    environment={"CC": "/target/clang", "AR": "/target/llvm-ar"},
                    arguments=("--build=x86_64-linux", "--host=x86_64-w64-mingw32"),
                )
            )
        )

        @staticmethod
        def get_build_env(_component):
            return {"CC": "/target/clang", "AR": "/target/llvm-ar"}

        @staticmethod
        def _ws_str():
            return "/identity/prefix"

    context = SimpleNamespace(builder=FakeBuilder())
    monkeypatch.setattr(shared_builders, "run_step", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(shared_builders, "run_make", lambda *args, **kwargs: None)
    monkeypatch.setattr(shared_builders, "run_install", lambda *args, **kwargs: None)

    shared_builders.build_autotools(context, zlib, tmp_path / "zlib")

    configure = calls[0]
    assert configure[5] == ["./configure", "--static", "--prefix=/identity/prefix"]
    assert configure[8]["CC"] == "/target/clang"
    assert configure[8]["AR"] == "/target/llvm-ar"


def test_wave1_manifest_rejects_missing_or_corrupt_installed_outputs(tmp_path: Path):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("zlib")
    assert component is not None

    for relative in (
        "include/zlib.h",
        "include/zconf.h",
        "lib/libz.a",
        "lib/pkgconfig/zlib.pc",
    ):
        output = validator.prefix / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(relative, encoding="utf-8")
    files = (
        "include/zlib.h",
        "include/zconf.h",
        "lib/libz.a",
        "lib/pkgconfig/zlib.pc",
    )
    validator.record_component(component, files)
    assert validator.manifest_matches(component)

    (validator.prefix / "lib/libz.a").write_text("corrupt", encoding="utf-8")
    assert not validator.manifest_matches(component)
    (validator.prefix / "lib/libz.a").unlink()
    assert not validator.manifest_matches(component)


def test_libpng_cleanup_removes_known_non_target_install_outputs(tmp_path: Path):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("libpng")
    assert component is not None
    paths = (
        "bin/pngfix.exe",
        "bin/libpng16-config",
        "include/libpng16/png.h",
        "include/png.h",
        "lib/libpng16.a",
        "lib/libpng16.la",
        "lib/pkgconfig/libpng.pc",
    )
    for relative in paths:
        output = validator.prefix / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(relative, encoding="utf-8")

    validator.remove_component_outputs(component)

    assert not any((validator.prefix / relative).exists() for relative in paths)


def test_freetype_metadata_must_publish_declared_static_dependencies(tmp_path: Path):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("FreeType2")
    assert component is not None
    metadata = validator.prefix / "lib/pkgconfig/freetype2.pc"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text("Requires: zlib\n", encoding="utf-8")

    with pytest.raises(BuildError, match="omits required dependency 'libpng'"):
        validator._validate_pkg_config_dependencies(component.name, validator._spec(component))

    metadata.write_text("Requires: zlib\nRequires.private: libpng\n", encoding="utf-8")
    validator._validate_pkg_config_dependencies(component.name, validator._spec(component))


def test_wave1_component_is_completed_only_after_acceptance(tmp_path: Path):
    component = ComponentRegistry().get_by_name("zlib")
    assert component is not None
    events = []
    current = SimpleNamespace(status=ComponentStatus.PENDING)

    class FakeState:
        def get(self):
            return SimpleNamespace(components={"zlib": current})

        def mark_component_status(self, name, status, version, *args, **kwargs):
            events.append(status)
            current.status = status

    class FakeBuilder:
        def build_component(self, _component):
            events.append("build")

    class FakeValidator:
        def remove_component_outputs(self, _component):
            events.append("cleanup")

        def validate_component(self, _component):
            events.append("validate")
            return ["lib/libz.a"]

        def validate_prefix(self):
            events.append("prefix")

        def record_component(self, _component, _outputs):
            events.append("manifest")

        def manifest_matches(self, _component):
            return False

    assert build_or_reuse_component(component, FakeBuilder(), FakeState(), FakeValidator())
    assert events.index("validate") < events.index("manifest")
    assert events.index("manifest") < events.index(ComponentStatus.COMPLETED)


def test_wave1_validation_failure_marks_only_current_component_failed(tmp_path: Path):
    component = ComponentRegistry().get_by_name("zlib")
    assert component is not None
    statuses = []

    class FakeState:
        def get(self):
            return SimpleNamespace(components={})

        def mark_component_status(self, _name, status, *_args, **_kwargs):
            statuses.append(status)

    class FakeBuilder:
        def build_component(self, _component):
            pass

    class FakeValidator:
        def remove_component_outputs(self, _component):
            pass

        def validate_component(self, _component):
            raise BuildError("zlib", "invalid target archive")

    with pytest.raises(BuildError, match="invalid target archive"):
        build_or_reuse_component(component, FakeBuilder(), FakeState(), FakeValidator())

    assert statuses == [ComponentStatus.PENDING, ComponentStatus.FAILED]


def test_invalid_completed_output_is_rebuilt_instead_of_reused():
    component = ComponentRegistry().get_by_name("zlib")
    assert component is not None
    current = SimpleNamespace(status=ComponentStatus.COMPLETED)
    statuses = []
    builds = []

    class FakeState:
        def get(self):
            return SimpleNamespace(components={"zlib": current})

        def mark_component_status(self, _name, status, *_args, **_kwargs):
            statuses.append(status)
            current.status = status

    class FakeBuilder:
        def build_component(self, built_component):
            builds.append(built_component.name)

    class FakeValidator:
        validations = 0

        def manifest_matches(self, _component):
            return True

        def validate_component(self, _component):
            self.validations += 1
            if self.validations == 1:
                raise BuildError("zlib", "installed output is missing or corrupt")
            return ["lib/libz.a"]

        def remove_component_outputs(self, _component):
            pass

        def validate_prefix(self):
            pass

        def record_component(self, _component, _outputs):
            pass

    assert build_or_reuse_component(component, FakeBuilder(), FakeState(), FakeValidator())
    assert builds == ["zlib"]
    assert statuses == [ComponentStatus.PENDING, ComponentStatus.COMPLETED]


def test_archive_validator_accepts_coff_and_rejects_elf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("zlib")
    assert component is not None
    archive = tmp_path / "libz.a"
    archive.touch()

    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave1.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=("Format: COFF-x86-64\n" "Format: COFF-import-file-x86-64\n"),
            stderr="",
        ),
    )
    validator._validate_archive(component.name, archive)

    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave1.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Format: elf64-x86-64\n", stderr=""
        ),
    )
    with pytest.raises(BuildError, match="not all x86-64 Windows COFF"):
        validator._validate_archive(component.name, archive)


def test_installed_metadata_rejects_host_paths(tmp_path: Path):
    validator = _fake_validator(tmp_path)
    metadata = validator.prefix / "lib/pkgconfig/contaminated.pc"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Libs: -L/usr/lib -lbad\n", encoding="utf-8")

    with pytest.raises(BuildError, match="non-target absolute path"):
        validator._validate_metadata_paths(metadata)


def test_installed_metadata_allows_urls_and_target_prefix_paths(tmp_path: Path):
    validator = _fake_validator(tmp_path)
    metadata = validator.prefix / "lib/pkgconfig/valid.pc"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        "URL: https://opus-codec.org/\n"
        f"prefix={validator.prefix}\n"
        f"Libs: -L{validator.prefix}/lib -lopus\n",
        encoding="utf-8",
    )

    validator._validate_metadata_paths(metadata)
