from pathlib import Path

import pytest

from ffmpeg_builder.config import BuildConfig
from ffmpeg_builder.platform_detect import PlatformDetector, PlatformInfo


def test_platform_context_round_trip():
    from ffmpeg_builder.platforms.context import PlatformContext

    config = BuildConfig()
    workspace = Path("/tmp/workspace")
    packages = Path("/tmp/packages")
    detector = PlatformDetector()

    context = PlatformContext(
        config=config,
        workspace=workspace,
        packages=packages,
        platform_detector=detector,
        platform_info=detector.platform_info,
        num_jobs=4,
    )

    assert context.config is config
    assert context.workspace == workspace.resolve()
    assert context.packages == packages.resolve()
    assert context.platform_detector is detector
    assert context.num_jobs == 4


def test_base_platform_strategy_must_be_implemented():
    from ffmpeg_builder.platforms.base import BasePlatformStrategy

    with pytest.raises(TypeError):
        BasePlatformStrategy()  # type: ignore[abstract]


def test_platform_detector_tracks_gcc_major_and_c23_defaults(monkeypatch):
    detector = PlatformDetector()
    detector.platform_info = PlatformInfo()

    def fake_detect():
        detector.platform_info.gcc_major_version = 15
        detector.platform_info.is_c23_default = True

    monkeypatch.setattr(detector, "_detect_compiler_info", fake_detect)

    detector._detect_compiler_info()

    assert detector.platform_info.gcc_major_version == 15
    assert detector.platform_info.is_c23_default is True


def test_platform_detector_parses_gcc_output_variants(monkeypatch):
    detector = PlatformDetector()
    detector.platform_info = PlatformInfo()

    cases = [
        ("gcc (GCC) 13.3.0", 13, False),
        ("gcc (Ubuntu 14.2.0-4ubuntu2) 14.2.0", 14, False),
        ("gcc (GCC) 16.0.0", 16, True),
    ]

    for raw, expected_major, expected_c23 in cases:
        major, c23 = detector._parse_compiler_version(raw)
        assert major == expected_major
        assert c23 is expected_c23
