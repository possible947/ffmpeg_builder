from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ffmpeg_builder.g4_wave2 as wave2
from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders import base as shared_builders
from ffmpeg_builder.builders.codecs import tiff as tiff_builder
from ffmpeg_builder.builders.codecs import lcms2 as lcms2_builder
from ffmpeg_builder.builders.tools import gettext as gettext_builder
from ffmpeg_builder.builders.tools import pcre as pcre_builder
from ffmpeg_builder.builder import FFmpegBuilder
from ffmpeg_builder.builders.codecs import soxr as soxr_builder
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.g4_wave2 import (
    G4_WAVE2_COMPONENTS,
    Wave2TargetValidator,
    accept_openmp_profile,
    order_wave_components,
    verify_wave_sources,
)


def _fake_validator(tmp_path: Path) -> Wave2TargetValidator:
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
        target_tools=SimpleNamespace(prefix=Path("/toolchains/llvm-mingw-20261104")),
    )
    return Wave2TargetValidator(environment, "identity-test")


def test_wave2_subset_is_exact_and_dependency_ordered():
    registry = ComponentRegistry()
    selected = [registry.get_by_name(name) for name in (*G4_WAVE2_COMPONENTS, "zlib")]
    assert all(selected)

    ordered = order_wave_components(
        registry,
        [component for component in selected if component is not None],
        accepted_external_dependencies=("zlib",),
    )

    assert {component.name for component in ordered} == set(G4_WAVE2_COMPONENTS)
    assert "zlib" not in {component.name for component in ordered}
    assert "libtiff" in {component.name for component in ordered}


def test_sdl_cross_build_disables_host_config_script():
    component = ComponentRegistry().get_by_name("libsdl")
    assert component is not None
    assert "--disable-sdl2-config" in component.configure_args


def test_wave2_rejects_unaccepted_wave1_dependency():
    registry = ComponentRegistry()
    selected = [registry.get_by_name(name) for name in G4_WAVE2_COMPONENTS]
    with pytest.raises(BuildError, match="has not been accepted"):
        order_wave_components(registry, [c for c in selected if c is not None])


def test_wave2_sources_are_registered_and_checksum_verified():
    verified = verify_wave_sources(ComponentRegistry(), Path("third_party/sources"))
    assert set(verified) == set(G4_WAVE2_COMPONENTS)
    assert all(len(value) == 64 for value in verified.values())


@pytest.mark.parametrize(
    ("openmp", "expected"),
    ((True, "-DWITH_OPENMP:bool=ON"), (False, "-DWITH_OPENMP:bool=OFF")),
)
def test_cmake_openmp_option_follows_config(openmp, expected, tmp_path, monkeypatch):
    soxr = ComponentRegistry().get_by_name("soxr")
    assert soxr is not None
    calls = []

    class FakeBuilder:
        config = SimpleNamespace(openmp=openmp)
        platform_context = SimpleNamespace(cross_environment=None)
        platform_strategy = None
        num_jobs = 2

        @staticmethod
        def _ws_str():
            return "/target/prefix"

        @staticmethod
        def get_build_env(_component):
            return {}

        @staticmethod
        def _is_windows_ucrt64_backend():
            return False

    monkeypatch.setattr(shared_builders, "run_step", lambda *args: calls.append(args))
    shared_builders.build_cmake(
        SimpleNamespace(builder=FakeBuilder()), soxr, tmp_path / "soxr-source"
    )

    configure_args = calls[0][5]
    assert expected in configure_args
    effective_openmp = [item for item in configure_args if item.lower().startswith("-dwith_openmp")]
    assert effective_openmp == [expected]


def test_autotools_uses_cross_backend_configure_override(monkeypatch, tmp_path):
    component = ComponentRegistry().get_by_name("lame")
    assert component is not None
    calls = []
    cross = SimpleNamespace(
        backend=TARGET_BACKEND_CROSS_MINGW,
        autotools=SimpleNamespace(environment={}, arguments=("--host=x86_64-w64-mingw32",)),
    )

    class FakeBuilder:
        platform = "linux"
        platform_context = SimpleNamespace(cross_environment=cross)
        num_jobs = 2

        @staticmethod
        def get_build_env(_component):
            return {}

        @staticmethod
        def _ws_str():
            return "/target/prefix"

    monkeypatch.setattr(shared_builders, "run_step", lambda *args: calls.append(args))
    monkeypatch.setattr(shared_builders, "run_make", lambda *args: None)
    monkeypatch.setattr(shared_builders, "run_install", lambda *args: None)

    shared_builders.build_autotools(
        SimpleNamespace(builder=FakeBuilder()), component, tmp_path / "lame"
    )

    assert "--disable-frontend" in calls[0][5]


def test_soxr_cross_builder_publishes_pkg_config_openmp_requirement(monkeypatch, tmp_path):
    component = ComponentRegistry().get_by_name("soxr")
    assert component is not None
    prefix = tmp_path / "prefix"
    docs = prefix / "doc"
    docs.mkdir(parents=True)
    for name in ("README", "LICENCE", "NEWS"):
        (docs / name).write_text("upstream documentation", encoding="utf-8")
    monkeypatch.setattr(soxr_builder, "build_cmake", lambda *args: None)
    builder = SimpleNamespace(
        config=SimpleNamespace(target_backend=TARGET_BACKEND_CROSS_MINGW),
        platform_context=SimpleNamespace(cross_environment=object()),
        platform_strategy=None,
        executor=None,
        state_manager=None,
        _ws_str=lambda: str(prefix),
    )

    soxr_builder.build_soxr(builder, component, tmp_path / "source")

    metadata = (prefix / "lib/pkgconfig/soxr.pc").read_text()
    assert not any((docs / name).exists() for name in ("README", "LICENCE", "NEWS"))
    assert f"prefix={prefix}" in metadata
    assert "Libs: -L${libdir} -lsoxr" in metadata
    assert "Libs.private: -fopenmp" in metadata


def test_openmp_acceptance_rejects_primary_profile_downgrade():
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        native_build=False,
        openmp=True,
    )
    with pytest.raises(BuildError, match="native_build=true and openmp=true"):
        accept_openmp_profile(None, config, "identity")
    assert config.native_build is False
    assert config.openmp is True


@pytest.mark.parametrize(
    ("native_build", "openmp"),
    ((False, True), (True, False), (False, False)),
)
def test_wave2_never_retries_with_compatibility_profile(native_build, openmp):
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        native_build=native_build,
        openmp=openmp,
    )

    with pytest.raises(BuildError, match="native_build=true and openmp=true"):
        wave2.run_g4_wave2(None, config)


def test_gettext_builds_only_the_runtime_library(tmp_path):
    component = ComponentRegistry().get_by_name("gettext")
    assert component is not None
    calls = []

    class FakeBuilder:
        platform = "linux"
        platform_strategy = None
        platform_context = SimpleNamespace(cross_environment=None)
        num_jobs = 2

        def _run_step(self, *args):
            calls.append(("configure", args))

        def _run_make(self, *args):
            calls.append(("build", args))

        def _run_install(self, *args):
            calls.append(("install", args))

        @staticmethod
        def get_build_env(_component):
            return {}

        @staticmethod
        def _ws_str():
            return "/target/prefix"

    builder = FakeBuilder()
    source_dir = tmp_path / "gettext"
    gettext_builder.build_gettext(builder, component, source_dir)

    assert calls[0][0] == "configure"
    assert calls[0][1][-2] == source_dir / "gettext-runtime"
    assert calls[1][0] == "build"
    assert calls[1][1][4] == source_dir / "gettext-runtime" / "intl"
    assert calls[2][0] == "install"
    assert calls[2][1][4] == source_dir / "gettext-runtime" / "intl"
    assert component.workdir == "gettext-runtime"


def test_pcre_builds_and_installs_only_its_static_library(tmp_path):
    component = ComponentRegistry().get_by_name("pcre")
    assert component is not None
    calls = []

    class FakeBuilder:
        platform = "linux"
        platform_context = SimpleNamespace(cross_environment=None)
        num_jobs = 2

        @staticmethod
        def get_build_env(_component):
            return {}

        @staticmethod
        def _ws_str():
            return "/target/prefix"

        def _run_step(self, *args):
            calls.append(args)

    pcre_builder.build_pcre(FakeBuilder(), component, tmp_path / "pcre")

    assert calls[1][4] == ["make", "-j2", "libpcre.la"]
    assert "install-binPROGRAMS" not in calls[2][4]
    assert "lib_LTLIBRARIES=libpcre.la" in calls[2][4]
    assert "pkgconfig_DATA=libpcre.pc" in calls[2][4]


def test_soxr_declares_target_openmp_link_and_runtime_metadata(tmp_path):
    validator = _fake_validator(tmp_path)
    validator.require_openmp_metadata(ComponentRegistry())


def test_wave2_manifest_rejects_missing_or_corrupt_outputs(tmp_path):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("pcre")
    assert component is not None
    for relative in validator._spec(component).files:
        output = validator.prefix / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(relative, encoding="utf-8")
    validator.record_component(component, validator._spec(component).files)
    assert validator.manifest_matches(component)

    (validator.prefix / "lib/libpcre.a").write_text("corrupt", encoding="utf-8")
    assert not validator.manifest_matches(component)
    (validator.prefix / "lib/libpcre.a").unlink()
    assert not validator.manifest_matches(component)


def test_prefix_validator_rejects_unexpected_gettext_tools(tmp_path):
    validator = _fake_validator(tmp_path)
    unexpected = validator.prefix / "bin/gettext.exe"
    unexpected.parent.mkdir()
    unexpected.write_bytes(b"PE")
    with pytest.raises(BuildError, match="Unexpected shared library, executable"):
        validator.validate_prefix()


def test_pkg_config_cflags_are_used_for_static_link_smoke(tmp_path, monkeypatch):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("pcre")
    assert component is not None
    validator.environment = SimpleNamespace(
        paths=validator.environment.paths,
        compiler_environment={"CFLAGS": "-O2", "LDFLAGS": ""},
        pkg_config_wrapper=Path("/project/pkg-config"),
        target_tools=SimpleNamespace(cc=Path("/target/clang"), cxx=Path("/target/clang++")),
    )
    commands = []

    def pkg_config(_component, args, _environment, _failure):
        if "--cflags" in args:
            return f"-I{validator.prefix / 'include'} -DPCRE_STATIC"
        return f"-L{validator.prefix / 'lib'} -lpcre"

    monkeypatch.setattr(validator, "_run", pkg_config)
    monkeypatch.setattr(validator, "_validate_executable", lambda *_args: None)
    monkeypatch.setattr(
        wave2.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    validator._link_smoke("pcre", validator._spec(component))

    assert "-DPCRE_STATIC" in commands[0]


def test_libtiff_builder_uses_cross_autotools_projection(tmp_path):
    component = ComponentRegistry().get_by_name("libtiff")
    assert component is not None
    calls = []
    cross = SimpleNamespace(
        autotools=SimpleNamespace(
            environment={"CC": "/target/clang", "PKG_CONFIG": "/target/pkg-config"},
            arguments=("--build=x86_64-linux", "--host=x86_64-w64-mingw32"),
        )
    )

    class FakeBuilder:
        platform_strategy = None
        platform_context = SimpleNamespace(cross_environment=cross)
        num_jobs = 2

        @staticmethod
        def get_build_env(_component):
            return {}

        @staticmethod
        def _ws_str():
            return "/identity/prefix"

        def _run_step(self, *args):
            calls.append(args)

        @staticmethod
        def _run_make(*_args):
            pass

        @staticmethod
        def _run_install(*_args):
            pass

    tiff_builder.build_libtiff(FakeBuilder(), component, tmp_path)

    assert calls[0][4][:3] == [
        "./configure",
        "--build=x86_64-linux",
        "--host=x86_64-w64-mingw32",
    ]
    assert {
        "--disable-tools",
        "--disable-tests",
        "--disable-docs",
        "--disable-cxx",
    }.issubset(calls[0][4])
    assert calls[0][-1]["CC"] == "/target/clang"
    assert calls[0][-1]["PKG_CONFIG"] == "/target/pkg-config"


def test_lcms2_builder_installs_only_library_headers_and_pkg_config(tmp_path):
    component = ComponentRegistry().get_by_name("lcms2")
    assert component is not None
    calls = []
    cross = SimpleNamespace(
        autotools=SimpleNamespace(
            environment={"CC": "/target/clang"},
            arguments=("--build=x86_64-linux", "--host=x86_64-w64-mingw32"),
        )
    )

    class FakeBuilder:
        platform_context = SimpleNamespace(cross_environment=cross)
        platform_strategy = None
        num_jobs = 2

        @staticmethod
        def get_build_env(_component):
            return {}

        @staticmethod
        def _ws_str():
            return "/identity/prefix"

        def _run_step(self, *args):
            calls.append(("step", args))

        def _run_make(self, *args):
            calls.append(("make", args))

        def _run_install(self, *args):
            calls.append(("install", args))

    lcms2_builder.build_lcms2(FakeBuilder(), component, tmp_path / "lcms2")

    assert calls[0][1][4][:3] == [
        "./configure",
        "--build=x86_64-linux",
        "--host=x86_64-w64-mingw32",
    ]
    assert calls[1][0] == "make"
    assert calls[1][1][4] == tmp_path / "lcms2" / "src"
    assert calls[2][0] == "install"
    assert calls[2][1][4] == tmp_path / "lcms2" / "src"
    assert calls[3][0] == "install"
    assert calls[3][1][4] == tmp_path / "lcms2" / "include"
    assert calls[4][0] == "step"
    assert calls[4][1][4] == ["make", "install-pkgconfigDATA"]
    assert calls[4][1][6] == tmp_path / "lcms2"


def test_xvidcore_cross_install_keeps_only_the_linkable_static_archive(tmp_path):
    component = ComponentRegistry().get_by_name("xvidcore")
    assert component is not None and component.post_install
    prefix = tmp_path / "prefix"
    (prefix / "lib").mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "lib/xvidcore.a").write_bytes(b"static archive")
    (prefix / "lib/xvidcore.dll.a").write_bytes(b"import archive")
    (prefix / "bin/xvidcore.dll").write_bytes(b"target dll")
    builder = FFmpegBuilder.__new__(FFmpegBuilder)
    builder.config = SimpleNamespace(target_backend=TARGET_BACKEND_CROSS_MINGW)
    builder._ws_str = lambda: str(prefix)

    builder._execute_post_install(component, tmp_path)

    assert (prefix / "lib/libxvidcore.a").read_bytes() == b"static archive"
    assert not (prefix / "lib/xvidcore.a").exists()
    assert not (prefix / "lib/xvidcore.dll.a").exists()
    assert not (prefix / "bin/xvidcore.dll").exists()
