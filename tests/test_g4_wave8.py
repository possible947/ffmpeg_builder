from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.graphics.vmaf import _adapt_cross_cuda_meson, _adapt_cross_libsvm
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave8 import Wave8Validator


def test_cross_cuda_adaptation_keeps_device_options_and_explicit_host_tools(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    meson = source / "meson.build"
    meson.write_text(
        "        cuda_lang = add_languages('cuda', required : true)\n"
        "        cuda_compiler = meson.get_compiler('cuda')\n"
        "        nvcc_exe = find_program('nvcc')\n"
        "        gencode = ['--fatbin', '-gencode=arch=compute_75,code=sm_75']\n"
        "        message(cuda_compiler.version())\n"
        "    bin2c_exe = find_program('bin2c')\n"
        "    cuda_flags = []\n"
        "        cuda_args: cuda_flags # + ['-gencode', 'arch=compute_86,code=sm_86' ] #, '--use_fast_math']\n"
    )
    tools = SimpleNamespace(
        nvcc="/sdk/bin/nvcc",
        bin2c="/sdk/bin/bin2c",
        toolkit_release="12.4",
        compute_architecture="compute_70",
        common_flags=("-ccbin=/gcc/bin/gcc", "-Xcompiler=-U_GNU_SOURCE"),
    )
    _adapt_cross_cuda_meson(tmp_path, tools, "12.4.99")
    text = meson.read_text()
    assert "add_languages('cuda'" not in text and "cuda_args:" not in text
    assert "find_program('/sdk/bin/nvcc', native: true)" in text
    assert "find_program('/sdk/bin/bin2c', native: true)" in text
    assert "compute_75,code=sm_75" in text
    assert "compute_70,code=sm_70" in text
    assert "cuda_version = '12.4.99'" in text
    assert "-ccbin=/gcc/bin/gcc" in text and "-Xcompiler=-U_GNU_SOURCE" in text
    _adapt_cross_cuda_meson(tmp_path, tools)
    assert meson.read_text() == text


def test_cross_adaptations_fail_closed_on_unexpected_source(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "meson.build").write_text("unexpected source")
    tools = SimpleNamespace(
        nvcc="nvcc",
        bin2c="bin2c",
        toolkit_release="12.4",
        compute_architecture="compute_70",
        common_flags=(),
    )
    with pytest.raises(BuildError, match="Unexpected upstream"):
        _adapt_cross_cuda_meson(tmp_path, tools)
    (source / "svm.cpp").write_text("unexpected source")
    with pytest.raises(BuildError, match="Unexpected libsvm"):
        _adapt_cross_libsvm(tmp_path)


def test_libsvm_swap_is_renamed_without_affecting_other_identifiers(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    path = source / "svm.cpp"
    path.write_text(
        "template <class T> static inline void swap(T& x, T& y) {}\n"
        "void f() { swap(a, b); other_swap(a, b); }\n"
    )
    _adapt_cross_libsvm(tmp_path)
    assert "void vmaf_swap(" in path.read_text()
    assert "vmaf_swap(a, b); other_swap(a, b)" in path.read_text()


def test_wave8_reuse_requires_source_and_output_identity(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    env = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace))
    validator = Wave8Validator(env, "identity", {"libvmaf": "source"})
    component = ComponentRegistry().get_by_name("libvmaf")
    header = prefix / "include/libvmaf/libvmaf.h"
    header.parent.mkdir(parents=True)
    header.write_text("original")
    validator.record_component(component, validator._owned_outputs(component))
    assert validator.manifest_matches(component)
    header.write_text("changed")
    assert not validator.manifest_matches(component)
    header.write_text("original")
    validator.sources = {"libvmaf": "changed"}
    assert not validator.manifest_matches(component)


def test_wave8_target_input_reuse_tracks_nv_codec_headers(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    env = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace))
    validator = Wave8Validator(env, "identity", {"nv-codec": "source"})
    component = ComponentRegistry().get_nv_codec_component()
    include = prefix / "include/ffnvcodec"
    include.mkdir(parents=True)
    for name in ("dynlink_cuda.h", "dynlink_loader.h"):
        (include / name).write_text(name)
    outputs = validator.validate_component(component)
    validator.record_component(component, outputs)
    assert validator.manifest_matches(component)
    (include / "dynlink_cuda.h").write_text("changed")
    assert not validator.manifest_matches(component)


def test_libvmaf_target_metadata_records_static_and_runtime_closure():
    metadata = ComponentRegistry().get_target_link_metadata(["libvmaf"])
    assert {"-lc++", "-lunwind", "-lpthread"}.issubset(metadata.consumer_link_requirements)
    assert {"nvcuda.dll", "libc++.dll", "libunwind.dll", "libwinpthread-1.dll"}.issubset(
        {item.name for item in metadata.runtime_dependencies}
    )
