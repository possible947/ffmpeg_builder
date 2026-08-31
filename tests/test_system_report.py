"""Tests for system report tool requirements."""

from ffmpeg_builder.platform_detect import PlatformInfo
from ffmpeg_builder.system_report import SystemReport


class TestRequiredTools:
    """M9: curl is not used by the builder (downloads go through requests)."""

    def test_missing_required_tools_excludes_curl(self):
        report = SystemReport(platform_info=PlatformInfo(), tools={})
        missing = report.get_missing_required_tools()
        assert missing == ["make", "pkg-config", "g++"]
        assert "curl" not in missing

    def test_macos_required_tools_exclude_curl(self):
        platform_info = PlatformInfo()
        platform_info.is_macos = True
        report = SystemReport(platform_info=platform_info, tools={})
        missing = report.get_missing_required_tools()
        assert missing == ["make", "pkg-config", "clang++"]
        assert "curl" not in missing
