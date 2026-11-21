from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.codecs import vpx, x264, x265
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave4b import (
    G4_WAVE4B_COMPONENTS,
    Wave4BTargetValidator,
    build_or_reuse,
    run_wave_components,
    select_components,
    verify_sources,
)
from ffmpeg_builder.state import ComponentStatus


def test_source_and_selection_boundaries():
    registry = ComponentRegistry()
    assert set(verify_sources(registry, Path("third_party/sources"))) == set(G4_WAVE4B_COMPONENTS)
    selected = [registry.get_by_name(name) for name in ("av1", "libvpx", "x265", "x264")]
    assert [item.name for item in select_components(registry, selected)] == list(
        G4_WAVE4B_COMPONENTS
    )
    with pytest.raises(BuildError, match="only x264, x265 and libvpx"):
        run_wave_components(selected, object(), object(), object())
    with pytest.raises(BuildError, match="not configured"):
        select_components(registry, selected[:-1])


@pytest.mark.parametrize("module,name", [(x264, "x264"), (vpx, "libvpx")])
def test_custom_configure_projects_cross_tools(monkeypatch, tmp_path, module, name):
    cross = SimpleNamespace(
        pkg_config_wrapper="/target/pkg-config",
        paths=SimpleNamespace(generated_files_root=tmp_path),
    )
    env = {"PATH": "/usr/bin", "CC": "/target/clang", "CFLAGS": "-march=native -mtune=native"}
    calls = []
    builder = SimpleNamespace(
        get_build_env=lambda component: env.copy(),
        platform_context=SimpleNamespace(cross_environment=cross),
        platform="linux",
        num_jobs=2,
        _ws_str=lambda: "/target/prefix",
        _run_step=lambda *args: calls.append(args),
        _run_make=lambda *args: None,
        _run_install=lambda *args: None,
    )
    monkeypatch.setattr(module.shutil, "which", lambda *args, **kwargs: "/usr/bin/nasm")
    component = ComponentRegistry().get_by_name(name)
    getattr(module, "build_" + name)(builder, component, tmp_path)
    command, child_env = calls[0][4], calls[0][7]
    assert child_env["CC"] == "/target/clang"
    assert child_env["CFLAGS"] == "-march=native -mtune=native"
    if name == "x264":
        assert "--host=x86_64-w64-mingw32" in command
        assert "--disable-opencl" in command
        assert child_env["AS"] == "/usr/bin/nasm"
    else:
        assert "--target=x86_64-win64-gcc" in command
        assert "--as=nasm" in command
        assert "--enable-vp9-highbitdepth" in command
        assert Path(child_env["TMPDIR"]).parent == tmp_path
        assert not Path(child_env["TMPDIR"]).exists()


@pytest.mark.parametrize("module,name", [(x264, "x264"), (vpx, "libvpx")])
def test_native_custom_builder_keeps_native_configuration(tmp_path, module, name):
    calls = []
    builder = SimpleNamespace(
        get_build_env=lambda component: {"CXXFLAGS": "-O2"},
        platform_context=SimpleNamespace(cross_environment=None),
        platform="linux",
        num_jobs=2,
        _ws_str=lambda: "/native/prefix",
        _run_step=lambda *args: calls.append(args),
        _run_make=lambda *args: None,
        _run_install=lambda *args: None,
    )
    getattr(module, "build_" + name)(builder, ComponentRegistry().get_by_name(name), tmp_path)
    command, env = calls[0][4], calls[0][7]
    assert not any("mingw" in arg for arg in command)
    if name == "x264":
        assert env["CXXFLAGS"] == "-fPIC -O2"
    else:
        assert "--as=yasm" in command


def test_x265_rejects_non_coff_variant(monkeypatch, tmp_path):
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(
                target_tools=SimpleNamespace(ar="/target/ar", readobj="/target/readobj")
            )
        )
    )
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="one.o\ntwo.o\n"),
            SimpleNamespace(returncode=0, stdout="Format: COFF-x86-64\nFormat: elf64-x86-64\n"),
        ]
    )
    monkeypatch.setattr(x265.subprocess, "run", lambda *args, **kwargs: next(results))
    with pytest.raises(BuildError, match="Invalid target archive"):
        x265._archive_members(builder, ComponentRegistry().get_by_name("x265"), tmp_path / "lib.a")


def _x265_identity_source(root, tag="4.0", build=216):
    (root / "source").mkdir(parents=True, exist_ok=True)
    (root / "x265Version.txt").write_text(
        f"#Attribute:         Values\nrepositorychangeset: 6318f22\n"
        f"releasetagdistance: 1\nreleasetag: {tag}\n"
    )
    (root / "source" / "CMakeLists.txt").write_text(f"set(X265_BUILD {build})\n")


def _x265_installed_prefix(root, version="4.0", generated=b"4.0+1-6318f22"):
    (root / "lib" / "pkgconfig").mkdir(parents=True)
    (root / "include").mkdir()
    (root / "lib" / "pkgconfig" / "x265.pc").write_text(f"Name: x265\nVersion: {version}\n")
    (root / "include" / "x265_config.h").write_text("#define X265_BUILD 216\n")
    (root / "lib" / "libx265.a").write_bytes(b"!<arch>\n" + generated)
    return SimpleNamespace(target_prefix=root)


def test_x265_source_identity_is_recorded_and_fail_closed(tmp_path):
    component = ComponentRegistry().get_by_name("x265")
    identity = x265.SOURCE_IDENTITIES[component.version]
    assert identity.upstream_commit.startswith("e444744c0397")
    assert component.sha256 == "0a3d41f6b4e2fe5a49d783d6631bd1a49c44cdf999074a3ff09e2cb71ac8ed33"
    _x265_identity_source(tmp_path / "good")
    assert x265._verify_source_identity(component, tmp_path / "good") == identity
    for name, kwargs in (("tag", {"tag": "4.2"}), ("api", {"build": 215})):
        _x265_identity_source(tmp_path / name, **kwargs)
        with pytest.raises(BuildError, match="recorded upstream identity"):
            x265._verify_source_identity(component, tmp_path / name)
    with pytest.raises(BuildError, match="source version data is missing"):
        x265._verify_source_identity(component, tmp_path / "missing")


def test_x265_installed_identity_requires_archive_generated_metadata(tmp_path):
    component = ComponentRegistry().get_by_name("x265")
    identity = x265.SOURCE_IDENTITIES[component.version]
    x265._verify_installed_identity(_x265_installed_prefix(tmp_path / "ok"), component, identity)
    for name, kwargs in (
        ("version", {"version": "4.2"}),
        ("empty", {"generated": b"no version"}),
    ):
        builder = _x265_installed_prefix(tmp_path / name, **kwargs)
        with pytest.raises(BuildError, match="needs a host git executable"):
            x265._verify_installed_identity(builder, component, identity)
    with pytest.raises(BuildError, match="metadata is missing"):
        x265._verify_installed_identity(
            SimpleNamespace(target_prefix=tmp_path / "absent"), component, identity
        )


def test_x265_variant_failure_stops_merge_and_install(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _x265_identity_source(tmp_path)
    component = ComponentRegistry().get_by_name("x265")
    cross = SimpleNamespace(
        cmake_toolchain_file="/target/toolchain.cmake",
        target_tools=SimpleNamespace(ar="/target/ar", ranlib="/target/ranlib"),
    )
    commands = []

    def run_step(component, status, detail, error, command, step, directory, env, **kwargs):
        commands.append((step, command))
        if step == "build-12bit":
            (directory / "libx265.a").write_bytes(b"variant")
        if step == "build-10bit":
            raise BuildError(component.name, "variant failed")

    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        num_jobs=2,
        _ws_str=lambda: "/target/prefix",
        _run_step=run_step,
    )
    monkeypatch.setattr(x265.shutil, "which", lambda *args, **kwargs: "/usr/bin/nasm")
    monkeypatch.setattr(x265, "_archive_members", lambda *args: ["variant.obj"])
    with pytest.raises(BuildError, match="variant failed"):
        x265._build_cross_x265(builder, component, tmp_path, {"PATH": "/usr/bin"})
    assert [step for step, _ in commands] == [
        "configure-12bit",
        "build-12bit",
        "configure-10bit",
        "build-10bit",
    ]
    for step, command in commands:
        if step.startswith("configure"):
            assert "-DCMAKE_TOOLCHAIN_FILE=/target/toolchain.cmake" in command
            assert "-DCMAKE_ASM_NASM_OBJECT_FORMAT=win64" in command
            assert "-DENABLE_CLI=OFF" in command


def test_x265_merge_failure_never_installs(monkeypatch, tmp_path):
    _x265_identity_source(tmp_path)
    component = ComponentRegistry().get_by_name("x265")
    cross = SimpleNamespace(
        cmake_toolchain_file="/target/toolchain.cmake",
        target_tools=SimpleNamespace(ar="/target/ar", ranlib="/target/ranlib"),
    )
    steps = []

    def run_step(component, status, detail, error, command, step, directory, env, **kwargs):
        steps.append(step)
        if step.startswith("build-"):
            (directory / "libx265.a").write_bytes(b"variant")
        if step == "merge-libs":
            raise BuildError(component.name, "merge failed")

    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        num_jobs=2,
        _ws_str=lambda: "/target/prefix",
        _run_step=run_step,
    )
    monkeypatch.setattr(x265.shutil, "which", lambda *args, **kwargs: "/usr/bin/nasm")
    monkeypatch.setattr(x265, "_archive_members", lambda *args: ["variant.obj"])
    with pytest.raises(BuildError, match="merge failed"):
        x265._build_cross_x265(builder, component, tmp_path, {"PATH": "/usr/bin"})
    assert steps[-1] == "merge-libs"
    assert "install" not in steps


@pytest.mark.parametrize(
    "references,expected",
    [("", "-lc++ -lunwind"), (" U __kmpc_fork_call\n", "-lc++ -lomp -lunwind -lunwind")],
)
def test_x265_normalises_only_unused_openmp(monkeypatch, tmp_path, references, expected):
    pc = tmp_path / "lib/pkgconfig/x265.pc"
    pc.parent.mkdir(parents=True)
    pc.write_text("Name: x265\nLibs.private: -lc++ -lomp -lunwind -lunwind\n")
    builder = SimpleNamespace(
        target_prefix=tmp_path,
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(target_tools=SimpleNamespace(nm="/target/nm"))
        ),
    )
    monkeypatch.setattr(
        x265.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=references),
    )
    x265._normalise_cross_pc(builder, ComponentRegistry().get_by_name("x265"))
    assert f"Libs.private: {expected}" in pc.read_text()


def _validator(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    return Wave4BTargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )


class Manager:
    def __init__(self):
        self.state = SimpleNamespace(components={})

    def get(self):
        return self.state

    def mark_component_status(self, name, status, version, *args, **kwargs):
        self.state.components[name] = SimpleNamespace(status=status)


@pytest.mark.parametrize("name,output", [("x264", "lib/libx264.a"), ("libvpx", "lib/libvpx.a")])
def test_corrupt_output_only_rebuilds_its_owner(tmp_path, monkeypatch, name, output):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name(name)
    target, sibling = validator.prefix / output, validator.prefix / "lib/libx265.a"
    target.parent.mkdir()
    target.write_bytes(b"accepted")
    sibling.write_bytes(b"preserved")
    validator.record_component(component, [output])
    target.write_bytes(b"corrupt")
    manager = Manager()
    manager.state.components[name] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    monkeypatch.setattr(validator, "validate_component", lambda item: [output])
    monkeypatch.setattr(validator, "validate_prefix", lambda: None)
    builder = SimpleNamespace(build_component=lambda item: target.write_bytes(b"rebuilt"))
    assert build_or_reuse(component, builder, manager, validator)
    assert validator.manifest_matches(component)
    assert sibling.read_bytes() == b"preserved"


def test_missing_libvpx_metadata_invalidates_manifest(tmp_path):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("libvpx")
    output = validator.prefix / "lib/pkgconfig/vpx.pc"
    output.parent.mkdir(parents=True)
    output.write_text("accepted")
    validator.record_component(component, ["lib/pkgconfig/vpx.pc"])
    output.unlink()
    assert not validator.manifest_matches(component)


def test_failed_component_cannot_invalidate_prior_output(tmp_path):
    validator = _validator(tmp_path)
    registry = ComponentRegistry()
    components = [registry.get_by_name(name) for name in G4_WAVE4B_COMPONENTS]
    prior = validator.prefix / "lib/libSvtAv1Enc.a"
    prior.parent.mkdir()
    prior.write_bytes(b"accepted")
    manager = Manager()
    called = []

    def fail(component):
        called.append(component.name)
        raise BuildError(component.name, "failed")

    with pytest.raises(BuildError):
        run_wave_components(components, SimpleNamespace(build_component=fail), manager, validator)
    assert called == ["x264"]
    assert manager.state.components["x264"].status == ComponentStatus.FAILED
    assert prior.read_bytes() == b"accepted"


def test_corrupt_x265_intermediate_cannot_be_reused(tmp_path):
    validator = _validator(tmp_path)
    validator.environment.paths.packages_root = tmp_path / "packages"
    component = ComponentRegistry().get_by_name("x265")
    output = validator.prefix / "lib/libx265.a"
    output.parent.mkdir()
    output.write_bytes(b"combined")
    for archive in validator._variant_archives(component):
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"variant")
    validator.record_component(component, ["lib/libx265.a"])
    assert validator.manifest_matches(component)
    validator._variant_archives(component)[1].write_bytes(b"corrupt")
    assert not validator.manifest_matches(component)


def test_failed_revalidation_never_leaves_completed_status(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("x264")
    output = validator.prefix / "lib/libx264.a"
    output.parent.mkdir()
    output.write_bytes(b"accepted")
    validator.record_component(component, ["lib/libx264.a"])
    manager = Manager()
    manager.state.components["x264"] = SimpleNamespace(status=ComponentStatus.COMPLETED)

    def fail(component):
        raise BuildError(component.name, "invalid target")

    monkeypatch.setattr(validator, "validate_component", fail)
    with pytest.raises(BuildError, match="invalid target"):
        build_or_reuse(component, object(), manager, validator)
    assert manager.state.components["x264"].status == ComponentStatus.FAILED
    assert output.exists()
