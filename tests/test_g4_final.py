from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.ffmpeg.ffmpeg import build_ffmpeg
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.g4_final import _installed_hashes, _validate_config
from ffmpeg_builder.state import ComponentStatus


def test_ffmpeg_install_hashes_detect_changes(tmp_path):
    with pytest.raises(BuildError, match="missing"):
        _installed_hashes(tmp_path)
    archive = tmp_path / "lib/libavcodec.a"
    header = tmp_path / "include/libavcodec/avcodec.h"
    for path in (archive, header):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original")
    before = _installed_hashes(tmp_path)
    header.write_text("changed")
    assert _installed_hashes(tmp_path) != before


def test_final_config_requires_all_selected_features_and_cuda_filters(tmp_path):
    (tmp_path / "config.h").write_text(
        "".join(
            f"#define CONFIG_{name} 1\n"
            for name in (
                "LIBX264",
                "VULKAN",
                "CUDA_NVCC",
                "FFNVCODEC",
                "CUVID",
                "NVDEC",
                "NVENC",
            )
        )
        + "#define HAVE_SPIRV_UNIFIED1_SPIRV_H 1\n"
    )
    components = tmp_path / "config_components.h"
    components.write_text(
        "#define CONFIG_SCALE_CUDA_FILTER 1\n"
        "#define CONFIG_LIBVMAF_CUDA_FILTER 1\n"
        "#define CONFIG_H264_NVENC_ENCODER 1\n"
        "#define CONFIG_H264_CUVID_DECODER 1\n"
        "#define CONFIG_SCALE_VULKAN_FILTER 1\n"
    )
    _validate_config(tmp_path, ["--enable-libx264", "--enable-vulkan"], True, True)
    with pytest.raises(BuildError, match="LIBX265"):
        _validate_config(tmp_path, ["--enable-libx265"], False, False)
    components.write_text(
        components.read_text().replace("CONFIG_SCALE_CUDA_FILTER 1", "CONFIG_SCALE_CUDA_FILTER 0")
    )
    with pytest.raises(BuildError, match="SCALE_CUDA_FILTER"):
        _validate_config(tmp_path, ["--enable-libx264"], True, True)


def test_ffmpeg_cross_uses_target_tools_and_scoped_host_cuda_vulkan(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    vulkan = prefix / "libvulkan.a"
    vulkan.touch()
    tools = SimpleNamespace(
        ar="target-ar", ranlib="target-ranlib", strip="target-strip", windres="target-windres"
    )
    cross = SimpleNamespace(
        autotools=SimpleNamespace(
            environment={"CFLAGS": "-march=native", "CXXFLAGS": "-march=native"}
        ),
        target_tools=tools,
        target_interfaces=SimpleNamespace(
            vulkan_import_library=vulkan, host_shader_tools={"shader_compiler": "/host/glslc"}
        ),
        paths=SimpleNamespace(workspace_root=tmp_path),
        pkg_config_wrapper="/target/pkg-config",
        approved_target_roots=(prefix,),
        cuda_host_tools=SimpleNamespace(
            nvcc="/host/nvcc",
            common_flags=("-ccbin=/host/gcc",),
            compute_architecture="compute_70",
        ),
        cuda_environment={"PATH": "/host/cuda/bin"},
    )
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW, gpl_enabled=True, enable_libplacebo_vulkan=True
    )
    states = {
        name: SimpleNamespace(status=ComponentStatus.COMPLETED)
        for name in ("libvmaf", "libplacebo")
    }
    calls = []
    builder = SimpleNamespace(
        config=config,
        platform_context=SimpleNamespace(cross_environment=cross),
        get_build_env=lambda component: {"CC": "/target/clang", "CXX": "/target/clang++"},
        state_manager=SimpleNamespace(get=lambda: SimpleNamespace(components=states)),
        registry=ComponentRegistry(),
        target_prefix=prefix,
        extralibs="",
        ldflags="",
        ldexeflags="",
        platform="linux",
        platform_detector=SimpleNamespace(
            platform_info=SimpleNamespace(cuda_available=True, qsv_available=False)
        ),
        _ws_str=lambda: str(prefix),
        _is_windows_ucrt64_backend=lambda: False,
        _run_step=lambda *args: calls.append(args),
        num_jobs=2,
        cflags="",
    )
    build_ffmpeg(builder, SimpleNamespace(name="ffmpeg", version="9.0"), tmp_path)
    configure = next(call[4] for call in calls if call[5] == "configure")
    environment = next(call[7] for call in calls if call[5] == "configure")
    assert "--enable-cross-compile" in configure
    assert "--target-os=mingw32" in configure
    assert "--enable-w32threads" in configure
    assert "--enable-cuda-nvcc" in configure
    assert "--glslc=/host/glslc" in configure
    assert "--nvcc=/host/nvcc" in configure
    assert "--enable-vaapi" not in configure
    assert environment["PATH"] == "/host/cuda/bin"
    assert environment["REVISION"] == "9.0"
    assert "-lstdc++" not in next(arg for arg in configure if arg.startswith("--extra-libs="))
