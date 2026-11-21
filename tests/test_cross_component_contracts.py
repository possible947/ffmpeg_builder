import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder import cross_component_contracts as contracts
from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import ComponentRegistry


@pytest.fixture
def builder(tmp_path):
    paths = SimpleNamespace(
        target_prefix=tmp_path / "target",
        workspace_root=tmp_path / "workspace",
        packages_root=tmp_path / "packages",
        generated_files_root=tmp_path / "generated",
    )
    for path in vars(paths).values():
        path.mkdir()
    tools = SimpleNamespace(
        cc=Path("/target/clang"),
        cxx=Path("/target/clang++"),
        ar=Path("/target/ar"),
        nm=Path("/target/nm"),
        readobj=Path("/target/readobj"),
        prefix=tmp_path / "llvm-mingw-20261104",
    )
    environment = SimpleNamespace(
        paths=paths,
        target_tools=tools,
        compiler_environment={"CFLAGS": "-O3 -fopenmp", "CXXFLAGS": "-O3 -fopenmp"},
        cmake_toolchain_file=tmp_path / "cross.cmake",
    )
    return SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=environment),
        config=SimpleNamespace(
            enable_libvmaf_cuda=False,
            enable_libplacebo_vulkan=False,
            native_build=False,
            openmp=True,
        ),
        registry=ComponentRegistry(),
        downloader=SimpleNamespace(),
        windows_target_state=SimpleNamespace(opencl=SimpleNamespace(platforms=[])),
        target_prefix=paths.target_prefix,
    )


@pytest.fixture
def pinned_sources(monkeypatch):
    calls = []

    def verified(builder, source):
        calls.append(source.name)
        version = source.version if hasattr(source, "version") else source.revision
        return f"{source.name}-{version}", source.sha256

    monkeypatch.setattr(contracts, "_source", verified)
    return calls


@pytest.mark.parametrize(
    "name,expected",
    [
        ("zlib", contracts.g4_wave1.Wave1TargetValidator),
        ("soxr", contracts.g4_wave2.Wave2TargetValidator),
        ("lv2", contracts.g4_wave3a.Wave3ATargetValidator),
        ("libvorbis", contracts.g4_wave3b.Wave3BTargetValidator),
        ("av1", contracts.g4_wave4a.Wave4ATargetValidator),
        ("x265", contracts.g4_wave4b.Wave4BTargetValidator),
        ("zimg", contracts.g4_wave4c.Wave4CTargetValidator),
        ("srt", contracts.g4_wave5a.Wave5ATargetValidator),
        ("libzmq", contracts.g4_wave5b.Wave5BTargetValidator),
        ("libjxl", contracts.g4_wave5c.Wave5CTargetValidator),
        ("rav1e", contracts.g4_wave6.Wave6TargetValidator),
        ("glslang", contracts.g4_wave7.Wave7Validator),
        ("nv-codec", contracts.g4_wave8.Wave8Validator),
        ("opencl-headers", contracts.g4_wave9.Wave9Validator),
    ],
)
def test_normal_dispatch_reuses_existing_validator_without_wave_profiles(
    builder, pinned_sources, name, expected
):
    component = builder.registry.get_by_name(name)
    validator = contracts.component_validator(builder, component, "identity")
    assert isinstance(validator.validator, expected)
    if name in ("srt", "libzmq"):
        assert type(validator.validator) is expected
    assert validator.manifest_path.name == "cross-components-acceptance.json"
    assert validator.validator.manifest_path == validator.manifest_path
    assert builder._wave5c_created_outputs is validator.created_outputs
    assert builder._wave6_created_outputs is validator.created_outputs
    assert builder._wave7_created_outputs is validator.created_outputs


def test_unknown_contract_fails_before_download(builder):
    component = replace(builder.registry.get_by_name("zlib"), name="future-library")
    with pytest.raises(BuildError, match="No normal Windows installed-output contract"):
        contracts.component_validator(builder, component, "identity")


@pytest.mark.parametrize(
    "name,extra",
    [
        ("glslang", {"SPIRV-Tools", "SPIRV-Headers"}),
        ("libjxl", {"brotli", "highway"}),
        ("VapourSynth", {"ffmpeg"}),
        ("opencl-headers", {"opencl-icd-loader"}),
    ],
)
def test_multisource_contracts_verify_auxiliary_sources(builder, pinned_sources, name, extra):
    contracts.component_validator(builder, builder.registry.get_by_name(name), "identity")
    assert set(pinned_sources) == {name, *extra}


def test_source_contract_uses_downloader_and_rechecks_returned_file(builder, tmp_path):
    content = b"verified source archive"
    digest = hashlib.sha256(content).hexdigest()
    component = replace(builder.registry.get_by_name("zlib"), sha256=digest)
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(content)
    calls = []

    def download(*args, **kwargs):
        calls.append((args, kwargs))
        return archive

    builder.downloader.download = download
    validator = contracts.component_validator(builder, component, "identity")
    assert validator.sources == {f"zlib-{component.version}": digest}
    assert calls == [
        (
            (component.get_url(), component.get_archive_filename()),
            {"expected_sha256": digest, "show_progress": False},
        )
    ]
    archive.write_bytes(b"corrupt")
    with pytest.raises(BuildError, match="checksum mismatch"):
        contracts.component_validator(builder, component, "identity")
    archive.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    with pytest.raises(BuildError, match="Git LFS pointer"):
        contracts.component_validator(builder, component, "identity")
    with pytest.raises(BuildError, match="no pinned SHA-256"):
        contracts.component_validator(builder, replace(component, sha256=""), "identity")


def _install(validator, component):
    spec = validator.validator._spec(component)
    files = getattr(spec, "files", (*spec.headers, *spec.libraries))
    for relative in files:
        path = validator.prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    return validator._owned_outputs(component)


def test_shared_normal_manifest_tracks_sources_dependencies_and_output_integrity(
    builder, pinned_sources
):
    lcms2 = builder.registry.get_by_name("lcms2")
    lcms = contracts.component_validator(builder, lcms2, "identity")
    lcms.record_component(lcms2, _install(lcms, lcms2))
    jxl_component = builder.registry.get_by_name("libjxl")
    jxl = contracts.component_validator(builder, jxl_component, "identity")
    outputs = _install(jxl, jxl_component)
    jxl.record_component(jxl_component, outputs)
    assert lcms.manifest_matches(lcms2)
    assert jxl.manifest_matches(jxl_component)
    assert list(lcms.manifest_path.parent.glob("*acceptance.json")) == [lcms.manifest_path]
    data = json.loads(lcms.manifest_path.read_text())
    assert set(data["components"]) == {"lcms2", "libjxl"}
    assert data["components"]["libjxl"]["dependencies"]["lcms2"]
    assert set(jxl.sources) <= data["source_sha256"].keys()
    (lcms.prefix / "lib/liblcms2.a").write_text("changed")
    assert not lcms.manifest_matches(lcms2)
    assert not jxl.manifest_matches(jxl_component)


def test_reuse_rejects_source_output_and_prefix_changes(builder, pinned_sources):
    component = builder.registry.get_by_name("zlib")
    validator = contracts.component_validator(builder, component, "identity")
    outputs = _install(validator, component)
    validator.record_component(component, outputs)
    assert validator.manifest_matches(component)
    original = validator.sources.copy()
    validator.sources = {"zlib": "changed source"}
    assert not validator.manifest_matches(component)
    validator.sources = original
    (validator.prefix / outputs[0]).unlink()
    assert not validator.manifest_matches(component)
    _install(validator, component)
    manifest = json.loads(validator.manifest_path.read_text())
    manifest["target_prefix"] = "/other-prefix"
    validator.manifest_path.write_text(json.dumps(manifest))
    assert not validator.manifest_matches(component)
    with pytest.raises(BuildError, match="identity mismatch"):
        validator.record_component(component, outputs)


def test_record_fails_closed_for_unaccepted_dependency_or_corrupt_manifest(builder, pinned_sources):
    component = builder.registry.get_by_name("libjxl")
    validator = contracts.component_validator(builder, component, "identity")
    outputs = _install(validator, component)
    with pytest.raises(BuildError, match="Dependency has no normal output acceptance: lcms2"):
        validator.record_component(component, outputs)
    validator.manifest_path.write_text("invalid json")
    with pytest.raises(BuildError, match="manifest is unreadable"):
        validator.record_component(component, outputs)


@pytest.mark.parametrize("name", ["libvmaf", "libplacebo"])
def test_software_contract_needs_no_cuda_or_vulkan_headers_and_no_hardware_smoke(
    builder, pinned_sources, monkeypatch, name
):
    component = builder.registry.get_by_name(name)
    validator = contracts.component_validator(builder, component, "identity")
    _install(validator, component)
    if name == "libvmaf":
        pc = validator.prefix / "lib/pkgconfig/libvmaf.pc"
        pc.parent.mkdir(parents=True, exist_ok=True)
        pc.write_text("metadata")
        options = (
            validator.environment.paths.packages_root
            / component.get_target_dir()
            / "build/meson-info/intro-buildoptions.json"
        )
        options.parent.mkdir(parents=True)
        options.write_text(json.dumps([{"name": "enable_cuda", "value": False}]))
        assert "nv-codec" not in pinned_sources
    else:
        assert "vulkan-headers" not in validator.dependencies
        assert "glslang" not in validator.dependencies
    calls = []
    monkeypatch.setattr(validator.validator, "_validate_archive", lambda *args: None)
    monkeypatch.setattr(validator.validator, "_pkg_config", lambda *args: "0")
    monkeypatch.setattr(contracts, "_software_smoke", lambda *args: calls.append(args[2]))
    outputs = validator.validate_component(component)
    assert outputs
    assert len(calls) == 1
    assert "vmaf_cuda" not in calls[0]
    assert "pl_vulkan" not in calls[0]
    assert not (validator.prefix / "include/libvmaf/libvmaf_cuda.h").exists()
    assert not (validator.prefix / "include/libplacebo/vulkan.h").exists()


def test_cuda_and_vulkan_profiles_keep_historical_hardware_validators(builder, pinned_sources):
    builder.config.enable_libvmaf_cuda = True
    builder.config.enable_libplacebo_vulkan = True
    vmaf = contracts.component_validator(
        builder, builder.registry.get_by_name("libvmaf"), "identity"
    )
    placebo = contracts.component_validator(
        builder, builder.registry.get_by_name("libplacebo"), "identity"
    )
    assert type(vmaf.validator) is contracts.g4_wave8.Wave8Validator
    assert type(placebo.validator) is contracts.g4_wave7.Wave7Validator
    assert "nv-codec" in pinned_sources


@pytest.mark.parametrize("name", ["openssl", "srt"])
@pytest.mark.parametrize(
    "variable,tuning",
    [("CFLAGS", "-march=native -mtune=native"), ("CXXFLAGS", "-mcpu=x86-64-v3")],
)
def test_network_configuration_accepts_portable_openmp_profile_and_checks_actual_cpu_flags(
    builder, pinned_sources, name, variable, tuning
):
    component = builder.registry.get_by_name(name)
    validator = contracts.component_validator(builder, component, "identity").validator
    source = validator.environment.paths.packages_root / component.get_target_dir()
    if name == "openssl":
        source.mkdir()
        evidence = source / "Makefile"
        evidence.write_text(str(validator.environment.target_tools.cc))
        logs = validator.workspace / "logs"
        logs.mkdir()
        (logs / "openssl_configure.log").write_text(
            "Configure mingw64 no-shared no-module --with-zlib-lib=/target"
        )
    else:
        source = source / "build-cross-mingw"
        source.mkdir(parents=True)
        evidence = source / "CMakeCache.txt"
        evidence.write_text(
            "ENABLE_ENCRYPTION:BOOL=ON\nENABLE_SHARED:BOOL=OFF\nENABLE_STATIC:BOOL=ON\n"
            "USE_ENCLIB:STRING=openssl-evp\nSSL_FOUND:INTERNAL=1\n"
            f"{validator.environment.cmake_toolchain_file}\n{validator.prefix}"
        )
    validator._validate_configuration(component)
    validator.environment.compiler_environment[variable] += f" {tuning}"
    with pytest.raises(BuildError, match=f"Missing .*: {re.escape(tuning.split()[0])}"):
        validator._validate_configuration(component)
    original = evidence.read_text()
    evidence.write_text(f"{original}\n{tuning}")
    validator._validate_configuration(component)
    if len(tuning.split()) > 1:
        evidence.write_text(f"{original}\n{tuning.split()[0]}")
        with pytest.raises(BuildError, match=f"Missing .*: {re.escape(tuning.split()[1])}"):
            validator._validate_configuration(component)


def test_vulkan_materialization_is_automatic_and_tracks_cleanup(
    builder, pinned_sources, monkeypatch
):
    component = builder.registry.get_by_name("vulkan-headers")
    validator = contracts.component_validator(builder, component, "identity")
    calls = []

    def install(builder, component):
        calls.append(component.name)
        for relative in ("share/vulkan/registry/vk.xml", "lib/pkgconfig/vulkan.pc"):
            path = builder.target_prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("matching metadata")
            builder._wave7_created_outputs.append(path)

    monkeypatch.setattr(contracts.g4_wave7, "install_vulkan", install)
    _install(validator, component)
    validator.validate_component(component)
    validator.validate_component(component)
    assert calls == ["vulkan-headers"]
    assert len(validator.created_outputs) == 2


def test_opencl_contract_requires_physical_target_evidence(builder, pinned_sources):
    del builder.windows_target_state
    with pytest.raises(BuildError, match="Physical Windows OpenCL evidence"):
        contracts.component_validator(
            builder, builder.registry.get_by_name("opencl-headers"), "identity"
        )


def test_software_vmaf_fails_closed_when_actual_build_enabled_cuda(
    builder, pinned_sources, monkeypatch
):
    component = builder.registry.get_by_name("libvmaf")
    validator = contracts.component_validator(builder, component, "identity").validator
    with pytest.raises(BuildError, match="Missing software libvmaf build evidence"):
        validator._smoke(component)
    options = (
        validator.environment.paths.packages_root
        / component.get_target_dir()
        / "libvmaf/build/meson-info/intro-buildoptions.json"
    )
    options.parent.mkdir(parents=True)
    options.write_text(json.dumps([{"name": "enable_cuda", "value": True}]))
    with pytest.raises(BuildError, match="CUDA enabled"):
        validator._smoke(component)


@pytest.mark.parametrize("gpu_flags,gpu_dll", [("-lcuda", ""), ("", "nvcuda.dll"), ("", "")])
def test_software_consumer_links_runs_on_cpu_and_rejects_gpu_dependencies(
    builder, pinned_sources, monkeypatch, gpu_flags, gpu_dll
):
    component = builder.registry.get_by_name("libvmaf")
    validator = contracts.component_validator(builder, component, "identity").validator
    monkeypatch.setattr(
        contracts.g4_wave1.Wave1TargetValidator, "_validate_metadata_paths", lambda *args: None
    )
    monkeypatch.setattr(
        contracts.g4_wave2.Wave2TargetValidator, "_validate_executable", lambda *args: None
    )
    monkeypatch.setattr(validator, "_validate_target_flags", lambda *args: None)
    monkeypatch.setattr(
        validator,
        "_pkg_config",
        lambda *args: f"-lvmaf -lc++ -lunwind {gpu_flags}",
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "wslpath":
            output = r"C:\consumer.exe"
        elif "--coff-imports" in command:
            output = f"Name: {gpu_dll}\n" if gpu_dll else "Name: KERNEL32.dll\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(contracts.subprocess, "run", run)
    runtime_calls = []
    monkeypatch.setattr(
        contracts,
        "WindowsInterop",
        lambda: SimpleNamespace(
            run_powershell=lambda *args, **kwargs: (
                runtime_calls.append(args[0]) or SimpleNamespace(returncode=0, stderr=b"")
            )
        ),
    )
    if gpu_flags or gpu_dll:
        with pytest.raises(BuildError, match="GPU"):
            contracts._software_smoke(validator, component, "int main(void) { return 0; }")
        assert not runtime_calls
    else:
        contracts._software_smoke(validator, component, "int main(void) { return 0; }")
        assert len(runtime_calls) == 1
        assert "-fopenmp" in commands[0]
        assert "-lcuda" not in commands[0]


@pytest.mark.parametrize("tuning", ["", "-march=native -mtune=native", "-mcpu=x86-64-v3"])
def test_libzmq_reuses_original_archive_metadata_and_effective_cpu_contracts(
    builder, pinned_sources, monkeypatch, tuning
):
    component = builder.registry.get_by_name("libzmq")
    contract = contracts.component_validator(builder, component, "identity")
    validator = contract.validator
    validator.environment.compiler_environment["CFLAGS"] += f" {tuning}"
    _install(contract, component)
    (contract.prefix / "lib/libzmq.la").write_text("dependency_libs='-lc++ -lunwind'")
    (contract.prefix / "lib/pkgconfig").mkdir()
    (contract.prefix / "lib/pkgconfig/libzmq.pc").write_text(
        f"Name: libzmq\nVersion: {component.version}\nCflags: -DZMQ_STATIC\n"
    )
    source = validator.environment.paths.packages_root / component.get_target_dir()
    (source / "src").mkdir(parents=True)
    (source / "config.log").write_text(
        "checking whether we are cross compiling\nconfigure:17: result: yes"
    )
    (source / "src/platform.hpp").write_text(
        "#define ZMQ_HAVE_WINDOWS 1\n#define ZMQ_HAVE_MINGW 1\n"
        "#define ZMQ_POLL_BASED_ON_SELECT 1\n"
    )
    (source / "Makefile").write_text(
        f"CC={validator.environment.target_tools.cc}\n"
        f"CXX={validator.environment.target_tools.cxx}\n{tuning}\n"
    )
    monkeypatch.setattr(validator, "_validate_archive", lambda *args: None)
    monkeypatch.setattr(validator, "_validate_target_flags", lambda *args: None)
    monkeypatch.setattr(
        contracts.g4_wave1.Wave1TargetValidator, "_validate_metadata_paths", lambda *args: None
    )

    def inspect(command, **kwargs):
        if command[1] == "t":
            output = "message.obj\n"
        elif command[1] == "--file-headers":
            output = "Format: COFF-x86-64\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(contracts.g4_wave5b.subprocess, "run", inspect)

    def pkg_config(component, args, env):
        if args[0].startswith("--variable="):
            name = args[0].split("=")[1]
            return str(
                validator.prefix / {"prefix": "", "includedir": "include", "libdir": "lib"}[name]
            )
        return "-lzmq -DZMQ_STATIC -lc++ -lunwind -lpthread -liphlpapi -lws2_32"

    monkeypatch.setattr(validator, "_pkg_config", pkg_config)
    linked = []
    monkeypatch.setattr(validator, "_link_and_runtime", lambda *args: linked.append(args[0].name))
    assert contract.validate_component(component)
    assert linked == ["libzmq"]
    validator.environment.compiler_environment["CFLAGS"] += " -march=x86-64-v3"
    with pytest.raises(BuildError, match="Missing target build setting: -march=x86-64-v3"):
        contract.validate_component(component)
    if tuning:
        (source / "Makefile").write_text(
            f"CC={validator.environment.target_tools.cc}\n"
            f"CXX={validator.environment.target_tools.cxx}\n-march=x86-64-v3\n"
        )
        with pytest.raises(
            BuildError, match=f"Missing target build setting: {re.escape(tuning.split()[0])}"
        ):
            contract.validate_component(component)
