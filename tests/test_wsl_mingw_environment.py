"""Tests for WSL-Mingw host/target environment isolation."""

import os
from pathlib import Path

from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.platform_detect import PlatformDetector, PlatformInfo
from ffmpeg_builder.platforms.context import PlatformContext
from ffmpeg_builder.platforms.linux_gcc13 import LinuxGcc13Platform
from ffmpeg_builder.wsl_mingw_environment import filter_wsl_mingw_target_environment


def test_filters_mixed_cuda_vulkan_contamination_and_preserves_safe_paths(tmp_path: Path):
    cuda = tmp_path / "cuda"
    vulkan = tmp_path / "vulkan"
    toolchains = tmp_path / "toolchains"
    target_prefix = tmp_path / "windows-target"
    unrelated = tmp_path / "unrelated-sdk"
    values = {
        "PATH": os.pathsep.join((str(cuda / "bin"), str(vulkan / "bin"), str(toolchains / "bin"))),
        "CUDA_HOME": str(cuda),
        "CUDA_PATH": str(cuda),
        "CUDA_NVCC_CCBIN": str(toolchains / "bin" / "gcc"),
        "NVCC_PREPEND_FLAGS": "--use_fast_math",
        "VULKAN_SDK": str(vulkan),
        "LD_LIBRARY_PATH": os.pathsep.join(
            (
                str(cuda / "lib64"),
                str(vulkan / "lib"),
                "/usr/lib",
                str(target_prefix / "lib"),
            )
        ),
        "LIBRARY_PATH": os.pathsep.join(
            (str(cuda / "targets/x86_64-linux/lib/stubs"), str(toolchains / "lib"))
        ),
        "PKG_CONFIG_PATH": os.pathsep.join(
            (
                str(vulkan / "lib/pkgconfig"),
                str(target_prefix / "lib/pkgconfig"),
                str(target_prefix / "lib/pkgconfig"),
            )
        ),
        "PKG_CONFIG_LIBDIR": str(vulkan / "share/pkgconfig"),
        "CMAKE_PREFIX_PATH": os.pathsep.join(
            (str(vulkan), str(target_prefix), str(unrelated), str(toolchains))
        ),
        "VK_LAYER_PATH": str(vulkan / "etc/vulkan/explicit_layer.d"),
        "VK_ADD_LAYER_PATH": str(vulkan / "etc/vulkan/implicit_layer.d"),
        "VK_ICD_FILENAMES": str(vulkan / "etc/vulkan/icd.d/nvidia_icd.json"),
        "UNRELATED": "preserved",
    }
    original = dict(values)

    filtered = filter_wsl_mingw_target_environment(
        values,
        {
            "cuda": (cuda,),
            "vulkan": (vulkan,),
        },
        target_input_roots=(target_prefix,),
    )
    result = filtered.environment

    assert values == original
    assert result["PATH"] == original["PATH"]
    assert result["UNRELATED"] == "preserved"
    assert result["LD_LIBRARY_PATH"] == str(target_prefix / "lib")
    assert result["CMAKE_PREFIX_PATH"] == str(target_prefix)
    assert result["PKG_CONFIG_PATH"] == str(target_prefix / "lib/pkgconfig")
    assert "LIBRARY_PATH" not in result
    assert "PKG_CONFIG_LIBDIR" not in result
    assert not {
        "CUDA_HOME",
        "CUDA_PATH",
        "CUDA_NVCC_CCBIN",
        "NVCC_PREPEND_FLAGS",
        "VULKAN_SDK",
        "VK_LAYER_PATH",
        "VK_ADD_LAYER_PATH",
        "VK_ICD_FILENAMES",
    }.intersection(result)
    assert any(
        item.variable == "PKG_CONFIG_PATH"
        and item.prerequisite == "vulkan"
        and item.value == str(vulkan / "lib/pkgconfig")
        for item in filtered.removed_paths
    )
    assert any(
        item.variable == "LIBRARY_PATH" and item.prerequisite == "cuda"
        for item in filtered.removed_paths
    )
    assert any(
        item.variable == "VK_LAYER_PATH" and item.prerequisite == "vulkan"
        for item in filtered.removed_paths
    )


def test_filters_sdk_paths_through_symlink_and_handles_missing_variables(tmp_path: Path):
    sdk = tmp_path / "sdk"
    alias = tmp_path / "sdk-alias"
    (sdk / "lib/pkgconfig").mkdir(parents=True)
    alias.symlink_to(sdk, target_is_directory=True)
    env = {
        "PKG_CONFIG_PATH": os.pathsep.join(
            (str(alias / "lib/pkgconfig"), str(tmp_path / "target-pc"))
        ),
        "LD_LIBRARY_PATH": "",
    }

    target_prefix = tmp_path / "target-prefix"
    filtered = filter_wsl_mingw_target_environment(
        env, {"vulkan": (sdk,)}, target_input_roots=(target_prefix,)
    )

    assert filtered.environment == {}
    assert env["PKG_CONFIG_PATH"].startswith(str(alias))
    assert filtered.removed_paths[0].prerequisite == "vulkan"


def test_filter_does_not_mutate_parent_environment():
    environment = {
        "VULKAN_SDK": "/opt/vulkan",
        "PKG_CONFIG_PATH": "/opt/vulkan/lib/pkgconfig",
        "LD_LIBRARY_PATH": "/opt/vulkan/lib",
    }

    filtered = filter_wsl_mingw_target_environment(
        environment,
        {"vulkan": ("/opt/vulkan",)},
        target_input_roots=("/target-prefix",),
    )

    assert environment == {
        "VULKAN_SDK": "/opt/vulkan",
        "PKG_CONFIG_PATH": "/opt/vulkan/lib/pkgconfig",
        "LD_LIBRARY_PATH": "/opt/vulkan/lib",
    }
    assert not filtered.environment.get("VULKAN_SDK")
    assert "PKG_CONFIG_PATH" not in filtered.environment
    assert "LD_LIBRARY_PATH" not in filtered.environment


def test_linux_platform_strategy_does_not_export_vulkan_sdk_for_cross_target(tmp_path: Path):
    sdk = tmp_path / "vulkan-sdk"
    for relative in ("include", "lib"):
        (sdk / relative).mkdir(parents=True)
    config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW, openmp=False)
    detector = PlatformDetector(config)
    detector.system_info.architecture = "x86_64"
    platform_info = PlatformInfo(
        platform="linux",
        is_linux=True,
        vulkan_sdk_available=True,
        vulkan_sdk_path=str(sdk),
    )
    context = PlatformContext(
        config=config,
        workspace=tmp_path / "workspace",
        packages=tmp_path / "packages",
        platform_detector=detector,
        platform_info=platform_info,
        num_jobs=1,
    )

    env = LinuxGcc13Platform().setup_environment(context)

    assert "VULKAN_SDK" not in env
    assert str(sdk) not in env["CFLAGS"]
    assert str(sdk) not in env["CXXFLAGS"]
    assert str(sdk) not in env["LDFLAGS"]
