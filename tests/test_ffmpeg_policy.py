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


def test_linux_gcc15_keeps_ffmpeg81_via_deployable_fallback():
    """Registry policy: out-of-range system GCC is OK when the fallback is deployable."""
    cfg = BuildConfig(ffmpeg_version="8.1")
    info = PlatformInfo(is_linux=True, platform="linux", gcc_major_version=15)

    policy = evaluate_ffmpeg_policy(cfg, info, _tools(gcc_version="15.2.1"))

    assert policy.available_versions == ["8.1", "9.0"]
    assert policy.selected_version_allowed is True
    assert policy.blocked_reason == ""


def test_non_macos_blocks_ffmpeg81_when_fallback_not_deployable(monkeypatch):
    """Without a deployable fallback, out-of-range system GCC blocks FFmpeg 8.1."""
    import ffmpeg_builder.ffmpeg_policy as policy_module
    from ffmpeg_builder.toolchain_registry import ToolchainRegistry

    cfg = BuildConfig(ffmpeg_version="8.1")
    info = PlatformInfo(is_linux=True, platform="linux", gcc_major_version=14)
    real_init = ToolchainRegistry.__init__

    def _undeployable(self, yaml_path=None):
        real_init(self, yaml_path)
        for profile in self.profiles.values():
            object.__setattr__(profile, "default_source", None)

    monkeypatch.setattr(ToolchainRegistry, "__init__", _undeployable)

    policy = policy_module.evaluate_ffmpeg_policy(cfg, info, _tools(gcc_version="14.1.0"))

    # GCC 14 is outside both version ranges, so with no deployable fallback
    # neither version is available.
    assert policy.available_versions == []
    assert policy.selected_version_allowed is False
    assert "GCC 14" in policy.blocked_reason


def test_non_macos_blocks_ffmpeg81_on_new_clang(monkeypatch):
    """Clang out of range blocks 8.1 when the fallback cannot cover it."""
    import ffmpeg_builder.ffmpeg_policy as policy_module
    from ffmpeg_builder.toolchain_registry import ToolchainRegistry

    cfg = BuildConfig(ffmpeg_version="8.1")
    info = PlatformInfo(is_linux=True, platform="linux")
    real_init = ToolchainRegistry.__init__

    def _undeployable(self, yaml_path=None):
        real_init(self, yaml_path)
        for profile in self.profiles.values():
            object.__setattr__(profile, "default_source", None)

    monkeypatch.setattr(ToolchainRegistry, "__init__", _undeployable)

    policy = policy_module.evaluate_ffmpeg_policy(cfg, info, _tools(clang_version="26.0.0"))

    assert "8.1" not in policy.available_versions
    assert policy.selected_version_allowed is False
    assert "Clang 26" in policy.blocked_reason


def test_macos_allows_ffmpeg81_only_with_macports17(monkeypatch):
    cfg = BuildConfig(ffmpeg_version="8.1")
    cfg.macos.clang = "macports-clang-17"
    info = PlatformInfo(is_macos=True, platform="darwin")

    def fake_which(name):
        return "/opt/local/bin/clang-mp-17" if name == "clang-mp-17" else None

    monkeypatch.setattr("shutil.which", fake_which)

    policy = evaluate_ffmpeg_policy(cfg, info, _tools(clang_version="26.0.0"))

    assert "8.1" in policy.available_versions
    assert policy.selected_version_allowed is True
