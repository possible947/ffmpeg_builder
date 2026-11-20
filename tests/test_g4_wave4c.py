from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.codecs import zimg
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave4c import (
    G4_WAVE4C_COMPONENTS,
    Wave2TargetValidator,
    Wave4CTargetValidator,
    build_or_reuse,
    run_wave_components,
    select_components,
    verify_source,
)
from ffmpeg_builder.state import ComponentStatus


def test_source_and_selection_are_bounded(tmp_path):
    registry = ComponentRegistry()
    component = registry.get_by_name("zimg")
    assert G4_WAVE4C_COMPONENTS == ("zimg",)
    assert list(verify_source(registry, Path("third_party/sources"))) == ["zimg"]
    assert select_components(registry, [registry.get_by_name("x264"), component]) == [component]
    with pytest.raises(BuildError, match="only zimg"):
        run_wave_components([component, registry.get_by_name("x264")], None, None, None)
    with pytest.raises(BuildError, match="not configured"):
        select_components(registry, [])
    with pytest.raises(BuildError, match="unavailable"):
        verify_source(registry, tmp_path)
    (tmp_path / component.get_archive_filename()).write_text("bad")
    with pytest.raises(BuildError, match="checksum mismatch"):
        verify_source(registry, tmp_path)
    (tmp_path / component.get_archive_filename()).write_text(
        "version https://git-lfs.github.com/spec/v1\n"
    )
    with pytest.raises(BuildError, match="LFS"):
        verify_source(registry, tmp_path)


def test_cross_builder_bootstraps_and_uses_shared_projection(monkeypatch, tmp_path):
    api = tmp_path / "src/zimg/api/zimg.cpp"
    api.parent.mkdir(parents=True)
    api.write_text("#include <cmath>\n")
    projection = {
        "CC": "/target/clang",
        "CXX": "/target/clang++",
        "AR": "/target/ar",
        "RANLIB": "/target/ranlib",
        "STRIP": "/target/strip",
        "PKG_CONFIG": "/target/pkg-config",
        "CFLAGS": "-march=native -mtune=native",
        "CXXFLAGS": "-march=native -mtune=native",
        "LDFLAGS": "-L/target/lib",
    }
    cross = SimpleNamespace(
        autotools=SimpleNamespace(
            environment=projection,
            arguments=["--build=x86_64-linux-gnu", "--host=x86_64-w64-mingw32"],
        ),
        target_tools=SimpleNamespace(nm="/target/nm"),
    )
    calls = []

    def run_step(*args):
        calls.append(args)
        if args[5] == "install":
            lib = tmp_path / "lib"
            lib.mkdir(exist_ok=True)
            (lib / "libzimg.la").write_text(
                "inherited_linker_flags=' -pthread -fopenmp'\n" "dependency_libs=' -L/target/lib'\n"
            )

    builder = SimpleNamespace(
        get_build_env=lambda component: {"PATH": "/usr/bin"},
        platform_context=SimpleNamespace(cross_environment=cross),
        _ws_str=lambda: "/target",
        target_prefix=tmp_path,
        num_jobs=2,
        _run_step=run_step,
        _run_make=lambda *args: calls.append(args),
        _is_windows_ucrt64_backend=lambda: pytest.fail("legacy backend reached"),
    )
    monkeypatch.setattr(zimg.shutil, "which", lambda *args: pytest.fail("legacy lookup reached"))
    monkeypatch.setattr(
        zimg.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    zimg.build_zimg(builder, ComponentRegistry().get_by_name("zimg"), tmp_path)
    assert "#include <exception>" in api.read_text()
    assert calls[0][4] == ["autoreconf", "--verbose", "--install", "--force"]
    configure = calls[1][4]
    assert "--host=x86_64-w64-mingw32" in configure
    assert "--build=x86_64-linux-gnu" in configure
    assert "--enable-static" in configure and "--disable-shared" in configure
    assert "--disable-simd" not in configure
    assert all(calls[1][7][key] == value for key, value in projection.items())
    assert calls[1][7]["STL_LIBS"] == "-lc++ -lunwind"
    assert (
        "dependency_libs=' -lc++ -lunwind -L/target/lib'"
        in (tmp_path / "lib/libzimg.la").read_text()
    )
    assert "inherited_linker_flags=''" in (tmp_path / "lib/libzimg.la").read_text()
    assert calls[-1][4] == [
        "make",
        "install-libLTLIBRARIES",
        "install-includeHEADERS",
        "install-pkgconfigDATA",
    ]
    (tmp_path / "configure").touch()
    calls.clear()
    zimg.build_zimg(builder, ComponentRegistry().get_by_name("zimg"), tmp_path)
    assert calls[0][4][0] == "./configure"
    assert api.read_text().count("#include <exception>") == 1


def test_native_builder_retains_legacy_recipe(monkeypatch, tmp_path):
    calls = []
    tool = tmp_path / "libtoolize"
    tool.touch()
    builder = SimpleNamespace(
        get_build_env=lambda component: {},
        platform_context=SimpleNamespace(cross_environment=None),
        workspace=tmp_path,
        config=SimpleNamespace(windows=SimpleNamespace(msys2_root=str(tmp_path / "msys"))),
        _command_exists=lambda _: False,
        _is_windows_ucrt64_backend=lambda: False,
        _ws_str=lambda: "/native",
        num_jobs=1,
        _run_step=lambda *args: calls.append(args),
        _run_make=lambda *args: None,
        _run_install=lambda *args: None,
    )
    monkeypatch.setattr(
        zimg.shutil, "which", lambda name: str(tool) if name == "libtoolize" else None
    )
    zimg.build_zimg(builder, ComponentRegistry().get_by_name("zimg"), tmp_path)
    assert [call[5] for call in calls] == ["libtoolize", "autogen", "configure"]
    assert not any("--host=" in arg for arg in calls[2][4])
    builder._is_windows_ucrt64_backend = lambda: True
    calls.clear()
    monkeypatch.setattr(zimg.shutil, "which", lambda name: str(tool))
    zimg.build_zimg(builder, ComponentRegistry().get_by_name("zimg"), tmp_path)
    assert calls[0][4][:2] == [str(tool), str(tool)]


def test_target_links_use_distinct_drivers_and_test_registry_metadata(tmp_path, monkeypatch):
    validator = validator_at(tmp_path)
    validator.environment.paths.generated_files_root = tmp_path
    validator.environment.target_tools = SimpleNamespace(
        cc="/cross/cc", cxx="/cross/cxx", readobj="/cross/readobj"
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "/cross/readobj":
            return SimpleNamespace(returncode=0, stdout="  Name: libc++.dll\n", stderr="")
        if command[0] == "/cross/cc" and "-lc++" not in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing C++")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("ffmpeg_builder.g4_wave4c.subprocess.run", run)
    monkeypatch.setattr(Wave2TargetValidator, "_validate_executable", lambda *args: None)
    component = ComponentRegistry().get_by_name("zimg")
    validator._links(component, "-I/target/include", "-L/target/lib -lzimg -lc++ -lunwind", {})
    assert sum(command[0] == "/cross/cc" for command in commands) == 3
    assert sum(command[0] == "/cross/cxx" for command in commands) == 1
    assert not list(tmp_path.glob("g4-wave4c-*"))


def test_target_links_reject_missing_runtime_negative_that_unexpectedly_succeeds(
    tmp_path, monkeypatch
):
    validator = validator_at(tmp_path)
    validator.environment.paths.generated_files_root = tmp_path
    validator.environment.target_tools = SimpleNamespace(
        cc="/cross/cc", cxx="/cross/cxx", readobj="/cross/readobj"
    )
    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave4c.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(Wave2TargetValidator, "_validate_executable", lambda *args: None)
    with pytest.raises(BuildError, match="succeeded without C\\+\\+"):
        validator._links(
            ComponentRegistry().get_by_name("zimg"),
            "-I/target/include",
            "-L/target/lib -lzimg -lc++ -lunwind",
            {},
        )


def test_coff_archive_validation_rejects_host_members(tmp_path, monkeypatch):
    validator = validator_at(tmp_path)
    validator.environment.target_tools = SimpleNamespace(readobj="/cross/readobj")
    validator.environment.compiler_environment = {}
    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave3a.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Format: COFF-x86-64\nFormat: elf64-x86-64\n", stderr=""
        ),
    )
    with pytest.raises(BuildError, match="not all x86-64 Windows COFF"):
        validator._validate_archive("zimg", tmp_path / "libzimg.a")


class Manager:
    def __init__(self):
        self.state = SimpleNamespace(components={})
        self.statuses = []

    def get(self):
        return self.state

    def mark_component_status(self, name, status, version, *args, **kwargs):
        self.statuses.append(status)
        self.state.components[name] = SimpleNamespace(status=status)


def validator_at(tmp_path):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    return Wave4CTargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )


@pytest.mark.parametrize("failure", ["configure", "build", "install", "validation"])
def test_failure_never_accepts_or_removes_other_outputs(tmp_path, monkeypatch, failure):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("zimg")
    sibling = validator.prefix / "lib" / "other.a"
    sibling.parent.mkdir()
    sibling.write_bytes(b"prior")
    target = validator.prefix / "lib/libzimg.a"
    manager = Manager()

    def build(item):
        target.write_bytes(b"partial")
        if failure != "validation":
            raise BuildError(item.name, failure)

    monkeypatch.setattr(
        validator,
        "validate_component",
        lambda item: (_ for _ in ()).throw(BuildError(item.name, failure)),
    )
    with pytest.raises(BuildError):
        build_or_reuse(component, SimpleNamespace(build_component=build), manager, validator)
    assert manager.statuses[-1] == ComponentStatus.FAILED
    assert ComponentStatus.COMPLETED not in manager.statuses
    assert not target.exists() and sibling.read_bytes() == b"prior"


@pytest.mark.parametrize("failure", ["configure", "build", "install", "validation"])
def test_real_recipe_failure_isolated_from_prior_prefix(tmp_path, monkeypatch, failure):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("zimg")
    source = tmp_path / "source"
    api = source / "src/zimg/api/zimg.cpp"
    api.parent.mkdir(parents=True)
    api.write_text("#include <cmath>\n")
    (source / "configure").touch()
    sibling = validator.prefix / "lib/prior.a"
    sibling.parent.mkdir()
    sibling.write_bytes(b"accepted prior output")
    archive = validator.prefix / "lib/libzimg.a"
    steps = []
    cross = SimpleNamespace(
        autotools=SimpleNamespace(environment={}, arguments=["--build=host", "--host=target"]),
        target_tools=SimpleNamespace(nm="/target/nm"),
    )

    def run_step(*args):
        step = args[5]
        steps.append(step)
        if step == "install":
            archive.write_bytes(b"partial")
            if failure != "install":
                (validator.prefix / "lib/libzimg.la").write_text(
                    "inherited_linker_flags=''\ndependency_libs=''\n"
                )
        if step == failure:
            raise BuildError("zimg", f"{step} failed")

    def run_make(*args):
        steps.append("build")
        if failure == "build":
            raise BuildError("zimg", "build failed")

    builder = SimpleNamespace(
        get_build_env=lambda item: {},
        platform_context=SimpleNamespace(cross_environment=cross),
        target_prefix=validator.prefix,
        _ws_str=lambda: str(validator.prefix),
        num_jobs=1,
        _run_step=run_step,
        _run_make=run_make,
    )
    builder.build_component = lambda item: zimg.build_zimg(builder, item, source)
    monkeypatch.setattr(
        zimg.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        validator,
        "validate_component",
        lambda item: (_ for _ in ()).throw(BuildError(item.name, "validation failed")),
    )
    manager = Manager()
    with pytest.raises(BuildError, match="failed"):
        build_or_reuse(component, builder, manager, validator)
    assert manager.statuses[-1] == ComponentStatus.FAILED
    assert ComponentStatus.COMPLETED not in manager.statuses
    assert sibling.read_bytes() == b"accepted prior output"
    assert not archive.exists()
    assert steps[0] == "configure"
    if failure == "configure":
        assert steps == ["configure"]
    if failure == "build":
        assert steps == ["configure", "build"]
    if failure in ("install", "validation"):
        assert steps == ["configure", "build", "install"]


@pytest.mark.parametrize("condition", ["valid", "corrupt", "missing"])
def test_reuse_revalidates_and_missing_or_corrupt_rebuilds(tmp_path, monkeypatch, condition):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("zimg")
    target = validator.prefix / "lib/libzimg.a"
    target.parent.mkdir()
    target.write_bytes(b"accepted")
    validator.record_component(component, ["lib/libzimg.a"])
    manager = Manager()
    manager.state.components["zimg"] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    checks = []
    monkeypatch.setattr(
        validator,
        "validate_component",
        lambda item: checks.append("component") or ["lib/libzimg.a"],
    )
    monkeypatch.setattr(validator, "validate_prefix", lambda: checks.append("prefix"))
    builder = SimpleNamespace(build_component=lambda item: target.write_bytes(b"rebuilt"))
    if condition != "valid":
        if condition == "corrupt":
            target.write_bytes(b"bad")
        else:
            target.unlink()
        assert build_or_reuse(component, builder, manager, validator)
        assert target.read_bytes() == b"rebuilt"
        assert manager.statuses[-1] == ComponentStatus.COMPLETED
    else:
        assert not build_or_reuse(component, builder, manager, validator)
        assert target.read_bytes() == b"accepted"
    assert checks == ["component", "prefix"]


def test_registry_publishes_verified_cpp_runtime():
    component = ComponentRegistry().get_by_name("zimg")
    assert {"-lc++", "-lunwind"}.issubset(component.target_link_metadata.consumer_link_requirements)
    assert {item.name for item in component.target_link_metadata.runtime_dependencies} == {
        "libc++.dll",
        "libunwind.dll",
    }
