"""Tests for Component registry, filtering, and URL helpers."""

import pytest

from ffmpeg_builder.components import (
    BuildSystem,
    Component,
    ComponentCategory,
    ComponentRegistry,
    PlatformOverride,
)


class TestComponentUrlHelpers:
    """Test URL and archive-filename derivation methods."""

    def test_get_url_substitutes_version(self):
        c = Component(
            name="test",
            version="1.2.3",
            url="https://example.org/test-{version}.tar.gz",
            category=ComponentCategory.OTHER_LIB,
            build_system=BuildSystem.AUTOTOOLS,
        )
        assert c.get_url() == "https://example.org/test-1.2.3.tar.gz"

    def test_get_archive_filename_from_url(self):
        c = Component(
            name="test",
            version="1.2.3",
            url="https://example.org/test-1.2.3.tar.gz",
            category=ComponentCategory.OTHER_LIB,
            build_system=BuildSystem.AUTOTOOLS,
        )
        assert c.get_archive_filename() == "test-1.2.3.tar.gz"

    def test_get_archive_filename_explicit(self):
        c = Component(
            name="test",
            version="abc1234",
            url="https://example.org/snapshot/abc1234.tar.gz",
            category=ComponentCategory.OTHER_LIB,
            build_system=BuildSystem.AUTOTOOLS,
            archive_filename="test-{version}.tar.gz",
        )
        assert c.get_archive_filename() == "test-abc1234.tar.gz"

    def test_get_target_dir_from_filename(self):
        c = Component(
            name="test",
            version="1.0.0",
            url="https://example.org/test-1.0.0.tar.gz",
            category=ComponentCategory.OTHER_LIB,
            build_system=BuildSystem.AUTOTOOLS,
        )
        assert c.get_target_dir() == "test-1.0.0"

    def test_get_target_dir_from_xz(self):
        c = Component(
            name="gmp",
            version="6.3.0",
            url="https://example.org/gmp-6.3.0.tar.xz",
            category=ComponentCategory.CRYPTO,
            build_system=BuildSystem.AUTOTOOLS,
        )
        assert c.get_target_dir() == "gmp-6.3.0"

    def test_get_target_dir_explicit_dirname(self):
        c = Component(
            name="zimg",
            version="3.0.6",
            url="https://github.com/sekrit-twc/zimg/archive/refs/tags/release-{version}.tar.gz",
            category=ComponentCategory.VIDEO_CODEC,
            build_system=BuildSystem.CUSTOM,
            archive_filename="zimg-{version}.tar.gz",
            archive_dirname="zimg-release-{version}",
        )
        assert c.get_target_dir() == "zimg-release-3.0.6"

    def test_get_target_dir_no_version_subst(self):
        c = Component(
            name="av1",
            version="3.12.0",
            url="https://example.org/av1.tar.gz",
            category=ComponentCategory.VIDEO_CODEC,
            build_system=BuildSystem.CMAKE,
            archive_dirname="av1",
        )
        assert c.get_target_dir() == "av1"

    def test_archive_strip_components_defaults_to_1(self):
        c = Component(
            name="test",
            version="1.0",
            url="https://example.org/test-1.0.tar.gz",
            category=ComponentCategory.OTHER_LIB,
            build_system=BuildSystem.AUTOTOOLS,
        )
        assert c.archive_strip_components == 1

    def test_archive_strip_zero(self):
        c = Component(
            name="av1",
            version="3.12.0",
            url="https://example.org/av1.tar.gz",
            category=ComponentCategory.VIDEO_CODEC,
            build_system=BuildSystem.CMAKE,
            archive_strip_components=0,
        )
        assert c.archive_strip_components == 0


class TestComponentAvailability:
    """Test is_available() filtering logic."""

    def _make(self, **kwargs) -> Component:
        kw = dict(
            name="test",
            version="1.0",
            url="https://example.org/test.tar.gz",
            category=ComponentCategory.OTHER_LIB,
            build_system=BuildSystem.AUTOTOOLS,
        )
        kw.update(kwargs)
        return Component(**kw)

    def test_basic_available(self):
        c = self._make()
        assert c.is_available(gpl_enabled=False, platform="linux", tools={}) is True

    def test_gpl_only_hidden_when_gpl_disabled(self):
        c = self._make(gpl_only=True)
        assert c.is_available(gpl_enabled=False, platform="linux", tools={}) is False
        assert c.is_available(gpl_enabled=True, platform="linux", tools={}) is True

    def test_non_gpl_only_hidden_when_gpl_enabled(self):
        c = self._make(non_gpl_only=True)
        assert c.is_available(gpl_enabled=True, platform="linux", tools={}) is False
        assert c.is_available(gpl_enabled=False, platform="linux", tools={}) is True

    def test_linux_only_on_darwin(self):
        c = self._make(linux_only=True)
        assert c.is_available(gpl_enabled=False, platform="darwin", tools={}) is False
        assert c.is_available(gpl_enabled=False, platform="linux", tools={}) is True

    def test_macos_only_on_linux(self):
        c = self._make(macos_only=True)
        assert c.is_available(gpl_enabled=False, platform="linux", tools={}) is False
        assert c.is_available(gpl_enabled=False, platform="darwin", tools={}) is True


class TestComponentRegistry:
    """Test ComponentRegistry methods."""

    @pytest.fixture(autouse=True)
    def _registry(self):
        self.registry = ComponentRegistry()

    def test_get_all_returns_list(self):
        all_c = self.registry.get_all()
        assert isinstance(all_c, list)
        assert len(all_c) >= 40  # ~60 components expected

    def test_contains_ffmpeg_target(self):
        ffmpeg = self.registry.get_by_name("ffmpeg")
        assert ffmpeg is not None
        assert ffmpeg.version == "8.1"
        assert ffmpeg.category == ComponentCategory.TARGET

    def test_get_by_name_returns_none_for_unknown(self):
        assert self.registry.get_by_name("nonexistent_component_xyz") is None

    def test_get_by_category(self):
        video = self.registry.get_by_category(ComponentCategory.VIDEO_CODEC)
        assert len(video) >= 8
        names = {c.name for c in video}
        assert "x264" in names
        assert "dav1d" in names

    def test_system_components(self):
        sys_comps = self.registry.get_system_components()
        names = {c.name for c in sys_comps}
        assert "pkg-config" in names
        assert "cmake" in names
        assert "meson" in names

    def test_source_components_excludes_system(self):
        src_comps = self.registry.get_source_components()
        names = {c.name for c in src_comps}
        # ffmpeg is not a system component
        assert "ffmpeg" in names

    def test_get_buildable_gpl_disabled_filters_gpl(self, mock_platform_info, mock_tools):
        buildable = self.registry.get_buildable(
            gpl_enabled=False,
            platform="linux",
            tools=mock_tools,
            platform_info=mock_platform_info,
        )
        names = {c.name for c in buildable}
        assert "x264" not in names  # GPL-only
        assert "x265" not in names
        assert "gmp" in names  # non-GPL

    def test_get_buildable_gpl_enabled_includes_gpl(self, mock_platform_info, mock_tools):
        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=mock_tools,
            platform_info=mock_platform_info,
        )
        names = {c.name for c in buildable}
        assert "x264" in names

    def test_get_buildable_skip_lv2(self, mock_platform_info, mock_tools):
        with_lv2 = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=mock_tools,
            disable_lv2=False,
            platform_info=mock_platform_info,
        )
        without_lv2 = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=mock_tools,
            disable_lv2=True,
            platform_info=mock_platform_info,
        )
        lv2_names_with = {c.name for c in with_lv2}
        lv2_names_without = {c.name for c in without_lv2}
        assert "lilv" in lv2_names_with
        assert "lilv" not in lv2_names_without

    def test_get_buildable_disable_libvmaf(self, mock_platform_info, mock_tools):
        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=mock_tools,
            enable_libvmaf=False,
            platform_info=mock_platform_info,
        )
        names = {c.name for c in buildable}
        assert "libvmaf" not in names

    def test_get_buildable_cuda_off_excludes_nv_codec(self, mock_tools):
        class NoCuda:
            cuda_available = False
            vulkan_available = True
            amf_available = False
            opencl_available = True
            opencl_runtime_available = True
            opencl_dev_available = True
            qsv_available = True
            is_msys2 = False
            is_ucrt64 = False
            build_backend = "linux-native"

        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=mock_tools,
            platform_info=NoCuda(),
        )
        names = {c.name for c in buildable}
        assert "nv-codec" not in names

    def test_get_buildable_windows_hw_accel_policy(self, mock_platform_info_windows, mock_tools):
        """On Windows MSYS2 UCRT64, only listed HW accel components pass."""
        from ffmpeg_builder.components import WINDOWS_UCRT64_HW_ACCEL_COMPONENTS

        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="windows",
            tools=mock_tools,
            platform_info=mock_platform_info_windows,
        )
        hw_names = {c.name for c in buildable if c.category == ComponentCategory.HW_ACCEL}
        # Should be a subset of the allowed list
        assert hw_names.issubset(WINDOWS_UCRT64_HW_ACCEL_COMPONENTS)

    def test_get_buildable_opencl_runtime_includes_opencl_components(self, mock_tools):
        class RuntimeOnlyOpenCL:
            cuda_available = False
            vulkan_available = True
            amf_available = False
            opencl_available = False
            opencl_runtime_available = True
            opencl_dev_available = False
            qsv_available = False
            is_msys2 = False
            is_ucrt64 = False
            build_backend = "linux-native"

        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=mock_tools,
            platform_info=RuntimeOnlyOpenCL(),
        )
        names = {c.name for c in buildable}
        assert "opencl-headers" in names
        assert "opencl-icd-loader" in names

    # ------------------------------------------------------------------
    # H2: tool-aware buildability (build-provided tools satisfy later needs)
    # ------------------------------------------------------------------

    @staticmethod
    def _tools(**availability):
        """Build a tools dict where named tools are unavailable unless set True."""

        class Tool:
            def __init__(self, available):
                self.available = available

        defaults = {name: True for name in ("python3", "meson", "ninja", "cargo", "rustc", "cmake")}
        defaults.update(availability)
        return {name: Tool(available) for name, available in defaults.items()}

    def test_get_buildable_uses_build_provided_meson_ninja(self, mock_platform_info):
        """Without system meson/ninja, consumers are still buildable because the
        meson/ninja components earlier in order provide them (H2)."""
        tools = self._tools(meson=False, ninja=False)
        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=tools,
            platform_info=mock_platform_info,
        )
        names = {c.name for c in buildable}
        # Providers are present...
        assert "meson" in names
        assert "ninja" in names
        # ...so their consumers must be included, not silently dropped.
        for consumer in ("dav1d", "lv2", "serd", "zix", "sord", "sratom", "lilv", "libvmaf"):
            assert consumer in names, f"{consumer} should be buildable via built meson/ninja"

    def test_get_buildable_still_excludes_when_provider_absent(self, mock_platform_info):
        """A tool with no provider component (python3) still gates on the system."""
        tools = self._tools(python3=False)
        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=tools,
            platform_info=mock_platform_info,
        )
        names = {c.name for c in buildable}
        # glslang requires python3 and nothing in the registry provides it.
        assert "glslang" not in names
        # dav1d also requires python3 -> excluded even though meson/ninja exist.
        assert "dav1d" not in names

    def test_get_buildable_rav1e_special_case_preserved(self, mock_platform_info):
        """rav1e is kept in the list even without cargo/rustc (skipped at build time)."""
        tools = self._tools(cargo=False, rustc=False)
        buildable = self.registry.get_buildable(
            gpl_enabled=True,
            platform="linux",
            tools=tools,
            platform_info=mock_platform_info,
        )
        names = {c.name for c in buildable}
        assert "rav1e" in names

    def test_tool_provided_by_system_component(self):
        reg = ComponentRegistry()
        assert reg._tool_provided_by(reg.get_by_name("meson")) == "meson"
        assert reg._tool_provided_by(reg.get_by_name("onevpl")) == "vpl"
        # Non-system components do not provide reusable tools.
        assert reg._tool_provided_by(reg.get_by_name("dav1d")) is None


class TestComponentRegistryToolAwareness:
    """Ordering guarantee: a provider must precede its consumer to count."""

    @pytest.fixture(autouse=True)
    def _registry(self):
        self.registry = ComponentRegistry()

    def test_provider_before_consumer_in_registry_order(self):
        """meson/ninja appear before every component that consumes them."""
        all_c = self.registry.get_all()
        order = {c.name: i for i, c in enumerate(all_c)}
        for consumer in ("dav1d", "lv2", "serd", "zix", "sord", "sratom", "lilv", "libvmaf"):
            assert order["meson"] < order[consumer], f"meson must precede {consumer}"
            assert order["ninja"] < order[consumer], f"ninja must precede {consumer}"


class TestFfmpegConfigureFlags:
    """Tests for get_ffmpeg_configure_flags."""

    @pytest.fixture(autouse=True)
    def _registry(self):
        self.registry = ComponentRegistry()

    def test_get_ffmpeg_configure_flags(self):
        flags = self.registry.get_ffmpeg_configure_flags(
            built_components=["x264", "libvpx", "opus"],
            gpl_enabled=True,
            platform="linux",
        )
        assert "--enable-libx264" in flags
        assert "--enable-libvpx" in flags
        assert "--enable-libopus" in flags

    def test_get_ffmpeg_configure_flags_darwin_extra(self):
        flags = self.registry.get_ffmpeg_configure_flags(
            built_components=[],
            gpl_enabled=False,
            platform="darwin",
        )
        assert "--enable-videotoolbox" in flags
        assert "--enable-opencl" in flags

    def test_get_ffmpeg_configure_flags_opencl_from_components(self):
        flags = self.registry.get_ffmpeg_configure_flags(
            built_components=["opencl-headers", "opencl-icd-loader"],
            gpl_enabled=True,
            platform="linux",
        )
        assert "--enable-opencl" in flags

    def test_get_ffmpeg_configure_flags_opencl_from_system_ready(self):
        class OpenCLSystemReady:
            opencl_available = False
            opencl_runtime_available = True
            opencl_dev_available = True

        flags = self.registry.get_ffmpeg_configure_flags(
            built_components=[],
            gpl_enabled=True,
            platform="linux",
            platform_info=OpenCLSystemReady(),
        )
        assert "--enable-opencl" in flags


class TestFfmpegTargetVersions:
    """Tests for selecting declared FFmpeg source profiles."""

    @pytest.fixture(autouse=True)
    def _registry(self):
        self.registry = ComponentRegistry()

    @pytest.mark.parametrize(
        ("version", "sha256"),
        (
            ("8.1", "dd308201bb1239a1b73185f80c6b4121f4efdfa424a009ce544fd00bf736bb2e"),
            ("9.0", "d97647ace36a307f17ba2bca052d68937487bed8682e1eb9b6737076a9c442b7"),
        ),
    )
    def test_get_ffmpeg_component_resolves_declared_source(self, version, sha256):
        component = self.registry.get_ffmpeg_component(version)

        assert component.version == version
        assert component.sha256 == sha256
        assert component.get_url().endswith(f"n{version}.tar.gz")
        assert component.get_archive_filename() == f"FFmpeg-release-{version}.tar.gz"

    def test_get_ffmpeg_component_rejects_unsupported_version(self):
        with pytest.raises(ValueError, match="Unsupported ffmpeg version"):
            self.registry.get_ffmpeg_component("10.0")

    def test_get_buildable_resolves_configured_ffmpeg_version(self, mock_tools, mock_platform_info):
        components = self.registry.get_buildable(
            gpl_enabled=False,
            platform="linux",
            tools=mock_tools,
            platform_info=mock_platform_info,
            ffmpeg_version="9.0",
        )

        ffmpeg = next(component for component in components if component.name == "ffmpeg")
        assert ffmpeg.version == "9.0"


class TestBuildOrder:
    """Tests for registry build ordering."""

    @pytest.fixture(autouse=True)
    def _registry(self):
        self.registry = ComponentRegistry()

    def test_build_order_ffmpeg_last(self):
        all_c = self.registry.get_all()
        assert all_c[-1].name == "ffmpeg"
