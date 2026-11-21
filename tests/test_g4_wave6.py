import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave6 import (
    G4_WAVE6_COMPONENTS,
    Wave6TargetValidator,
    build_or_reuse,
    select_components,
    validate_prior_manifests,
    verify_source,
)
from ffmpeg_builder.state import ComponentStatus


def _validator(tmp_path, source_sha256="source"):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    generated = tmp_path / "generated"
    for path in (prefix, workspace, generated):
        path.mkdir()
    tools = SimpleNamespace(
        prefix=tmp_path / "toolchain",
        ar=tmp_path / "ar",
        readobj=tmp_path / "readobj",
    )
    environment = SimpleNamespace(
        paths=SimpleNamespace(
            target_prefix=prefix, workspace_root=workspace, generated_files_root=generated
        ),
        target_tools=tools,
        compiler_environment={},
    )
    return Wave6TargetValidator(environment, "identity", source_sha256)


def test_wave6_subset_and_registry_contract():
    registry = ComponentRegistry()
    component = registry.get_by_name("rav1e")
    assert G4_WAVE6_COMPONENTS == ("rav1e",)
    assert select_components(registry, [component]) == [component]
    assert "-lunwind" in component.target_link_metadata.private_transitive_link_requirements
    assert any(
        runtime.name == "libunwind.dll"
        for runtime in component.target_link_metadata.runtime_dependencies
    )
    with pytest.raises(BuildError, match="exactly once"):
        select_components(registry, [component, component])
    with pytest.raises(BuildError, match="exactly once"):
        select_components(registry, [])


def test_source_verification_rejects_lfs_pointer_and_bad_checksum(tmp_path):
    component = ComponentRegistry().get_by_name("rav1e")
    archive = tmp_path / component.get_archive_filename()
    archive.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    with pytest.raises(BuildError, match="LFS pointer"):
        verify_source(ComponentRegistry(), tmp_path)
    archive.write_bytes(b"not the registered archive")
    with pytest.raises(BuildError, match="checksum mismatch"):
        verify_source(ComponentRegistry(), tmp_path)


def test_source_verification_uses_configured_project_downloader(tmp_path, monkeypatch):
    component = ComponentRegistry().get_by_name("rav1e")
    archive = tmp_path / "rav1e.tar.gz"
    archive.write_bytes(b"verified archive")
    calls = []

    class Downloader:
        def download(self, url, filename, **kwargs):
            calls.append((url, filename, kwargs))
            return archive

    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave6.hashlib.file_digest",
        lambda stream, algorithm: SimpleNamespace(hexdigest=lambda: component.sha256),
    )
    assert verify_source(ComponentRegistry(), tmp_path, Downloader()) == {"rav1e": component.sha256}
    assert calls == [
        (
            component.get_url(),
            component.get_archive_filename(),
            {"expected_sha256": component.sha256, "show_progress": False},
        )
    ]


def test_source_verification_reports_downloader_failure(tmp_path):
    class Downloader:
        def download(self, *args, **kwargs):
            raise RuntimeError("offline source missing")

    with pytest.raises(BuildError, match="offline source missing"):
        verify_source(ComponentRegistry(), tmp_path, Downloader())


def test_prior_manifest_validation_skips_wave6_and_preserves_earlier_outputs(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    output = prefix / "prior.a"
    output.write_bytes(b"prior output")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    earlier = {
        "identity_key": "identity",
        "target_prefix": str(prefix),
        "components": {"prior": {"sha256": {"prior.a": digest}}},
    }
    (workspace / "g4-wave5c-acceptance.json").write_text(json.dumps(earlier))
    (workspace / "g4-wave6-acceptance.json").write_text("{}")
    assert validate_prior_manifests(workspace, prefix, "identity") == 1
    output.write_bytes(b"corrupt")
    with pytest.raises(BuildError, match="Prior output changed"):
        validate_prior_manifests(workspace, prefix, "identity")
    assert (workspace / "g4-wave5c-acceptance.json").read_text() == json.dumps(earlier)


def test_manifest_reuse_requires_matching_recipe_source_and_hashes(tmp_path):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("rav1e")
    header = validator.prefix / "include/rav1e/rav1e.h"
    header.parent.mkdir(parents=True)
    header.write_text("header")
    outputs = validator._owned_outputs(component)
    validator.record_component(component, outputs)
    assert validator.manifest_matches(component)
    header.write_text("changed")
    assert not validator.manifest_matches(component)
    header.write_text("header")
    validator.source_sha256 = "different source"
    assert not validator.manifest_matches(component)


def test_installed_contract_checks_static_archive_and_target_pkg_config(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("rav1e")
    for relative, content in (
        ("include/rav1e/rav1e.h", "header"),
        ("lib/librav1e.a", "archive"),
        (
            "lib/pkgconfig/rav1e.pc",
            "Name: rav1e\nDescription: test\nVersion: 0.8.1\n"
            "Cflags: -I${includedir}/rav1e\n"
            "Libs: -L${libdir} -lrav1e -lkernel32 -lntdll -luserenv -lws2_32 -ldbghelp -lunwind\n"
            "Libs.private: -lkernel32 -lntdll -luserenv -lws2_32 -ldbghelp -lunwind\n",
        ),
    ):
        output = validator.prefix / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["t"]:
            return SimpleNamespace(
                returncode=0,
                stdout="rav1e.o\nkernel32.dll\nkernel32.dll\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Format: COFF-x86-64\n" "Format: COFF-x86-64\n" "Format: COFF-import-file-x86-64\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("ffmpeg_builder.g4_wave6.subprocess.run", run)
    monkeypatch.setattr(
        validator,
        "_pkg_config",
        lambda name, args, env: {
            "--variable=prefix rav1e": str(validator.prefix) + "\n",
            "--variable=includedir rav1e": str(validator.prefix / "include") + "\n",
            "--variable=libdir rav1e": str(validator.prefix / "lib") + "\n",
            "--static --cflags --libs rav1e": (
                f"-I{validator.prefix}/include/rav1e -L{validator.prefix}/lib "
                "-lrav1e -lkernel32 -lntdll -luserenv -lws2_32 -ldbghelp -lunwind"
            ),
        }[" ".join(args)],
    )
    linked = []
    validator._link_and_run = lambda *args: linked.append(args)
    outputs = validator.validate_component(component)
    assert set(outputs) == {
        "include/rav1e/rav1e.h",
        "lib/librav1e.a",
        "lib/pkgconfig/rav1e.pc",
    }
    assert linked and calls


def test_build_failure_removes_only_new_rav1e_outputs(tmp_path):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("rav1e")
    prior = validator.prefix / "lib/prior.a"
    prior.parent.mkdir()
    prior.write_text("preserve")
    own = validator.prefix / "lib/librav1e.a"

    class Manager:
        def __init__(self):
            self.state = SimpleNamespace(components={})
            self.statuses = []

        def get(self):
            return self.state

        def mark_component_status(self, name, status, *args, **kwargs):
            self.statuses.append(status)

    manager = Manager()

    def build(_component):
        own.write_text("partial")
        builder._wave6_created_outputs.append(own)

    builder = SimpleNamespace(build_component=build, _wave6_created_outputs=[])
    with pytest.raises(BuildError, match="Missing target output"):
        build_or_reuse(
            component,
            builder,
            manager,
            validator,
        )
    assert manager.statuses[-1] == ComponentStatus.FAILED
    assert not own.exists()
    assert prior.read_text() == "preserve"


def test_unaccepted_preexisting_header_is_preserved_and_refused(tmp_path):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("rav1e")
    header = validator.prefix / "include/rav1e/rav1e.h"
    header.parent.mkdir(parents=True)
    header.write_text("preexisting")

    class Manager:
        def get(self):
            return SimpleNamespace(components={})

        def mark_component_status(self, *args, **kwargs):
            pytest.fail("build should not start with unaccepted output")

    builder = SimpleNamespace(
        build_component=lambda item: pytest.fail("unexpected build"),
        _wave6_created_outputs=[],
    )
    with pytest.raises(BuildError, match="Unaccepted rav1e outputs"):
        build_or_reuse(component, builder, Manager(), validator)
    assert header.read_text() == "preexisting"


def test_build_or_reuse_revalidates_accepted_state_without_build(tmp_path):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("rav1e")

    class Manager:
        def __init__(self):
            self.state = SimpleNamespace(
                components={component.name: SimpleNamespace(status=ComponentStatus.COMPLETED)}
            )

        def get(self):
            return self.state

    validator.manifest_matches = lambda item: True
    checked = []
    validator.validate_component = lambda item: checked.append("component")
    validator.validate_prefix = lambda: checked.append("prefix")
    builder = SimpleNamespace(build_component=lambda item: pytest.fail("unexpected rebuild"))
    assert not build_or_reuse(component, builder, Manager(), validator)
    assert checked == ["component", "prefix"]
