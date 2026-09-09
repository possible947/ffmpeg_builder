from ffmpeg_builder.config import BuildConfig
from ffmpeg_builder.ffmpeg_policy import evaluate_ffmpeg_policy
from ffmpeg_builder.platform_detect import PlatformInfo, ToolInfo


def _tools(gcc_version=None, clang_version=None):
    tools = {}
    if gcc_version is not None:
        tools["gcc"] = ToolInfo(name="gcc", version=gcc_version, available=True)
    if clang_version is not None:
        tools["clang"] = ToolInfo(name="clang", version=clang_version, available=True)
    return tools


def test_linux_gcc15_only_ffmpeg9_policy():
    cfg = BuildConfig(ffmpeg_version="8.1")
    info = PlatformInfo(is_linux=True, platform="linux", gcc_major_version=15)

    policy = evaluate_ffmpeg_policy(cfg, info, _tools(gcc_version="15.2.1"))

    assert policy.available_versions == ["9.0"]
    assert policy.selected_version_allowed is False
    assert "FFmpeg 9.0" in " ".join(policy.notes)


def test_non_macos_blocks_ffmpeg81_on_new_gcc():
    cfg = BuildConfig(ffmpeg_version="8.1")
    info = PlatformInfo(is_linux=True, platform="linux", gcc_major_version=14)

    policy = evaluate_ffmpeg_policy(cfg, info, _tools(gcc_version="14.1.0"))

    assert "8.1" not in policy.available_versions
    assert policy.selected_version_allowed is False
    assert "GCC 14" in policy.blocked_reason


def test_non_macos_blocks_ffmpeg81_on_new_clang():
    cfg = BuildConfig(ffmpeg_version="8.1")
    info = PlatformInfo(is_linux=True, platform="linux")

    policy = evaluate_ffmpeg_policy(cfg, info, _tools(clang_version="26.0.0"))

    assert "8.1" not in policy.available_versions
    assert policy.selected_version_allowed is False
    assert "Clang 26" in policy.blocked_reason


def test_macos_allows_ffmpeg81_only_with_macports17(monkeypatch):
    cfg = BuildConfig(ffmpeg_version="8.1")
    cfg.macos.clang = "macports-clang-17"
    info = PlatformInfo(is_macos=True, platform="darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/opt/local/bin/clang-mp-17")

    policy = evaluate_ffmpeg_policy(cfg, info, _tools(clang_version="26.0.0"))

    assert "8.1" in policy.available_versions
    assert policy.selected_version_allowed is True
