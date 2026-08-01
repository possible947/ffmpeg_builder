"""Component registry for FFmpeg builder."""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any, Callable
from pathlib import Path

WINDOWS_UCRT64_HW_ACCEL_COMPONENTS = {
    "nv-codec",
    "vulkan-headers",
    "glslang",
    "libplacebo",
    "opencl-headers",
    "opencl-icd-loader",
    "onevpl",
}


class BuildSystem(str, Enum):
    """Build system type."""
    AUTOTOOLS = "autotools"
    CMAKE = "cmake"
    MESON = "meson"
    CUSTOM = "custom"
    HEADERS_ONLY = "headers_only"
    MAKE_ONLY = "make_only"
    CARGO = "cargo"


class ComponentCategory(str, Enum):
    """Component category."""
    BUILD_TOOL = "build_tool"
    CRYPTO = "crypto"
    VIDEO_CODEC = "video_codec"
    AUDIO_CODEC = "audio_codec"
    IMAGE_CODEC = "image_codec"
    OTHER_LIB = "other_lib"
    HW_ACCEL = "hw_accel"
    TARGET = "target"


@dataclass
class PlatformOverride:
    """Platform-specific overrides."""
    extra_env: Dict[str, str] = field(default_factory=dict)
    extra_cflags: str = ""
    extra_cxxflags: str = ""
    extra_ldflags: str = ""
    patches: List[str] = field(default_factory=list)
    configure_args_override: Optional[List[str]] = None


@dataclass
class Component:
    """Component definition."""
    name: str
    version: str
    url: str
    category: ComponentCategory
    build_system: BuildSystem
    configure_args: List[str] = field(default_factory=list)
    build_args: List[str] = field(default_factory=list)
    install_args: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    requires_tools: List[str] = field(default_factory=list)
    gpl_only: bool = False
    non_gpl_only: bool = False
    linux_only: bool = False
    macos_only: bool = False
    windows_ucrt64_supported: bool = False
    system_component: bool = False
    system_tool_name: Optional[str] = None
    archive_strip_components: int = 1
    archive_dirname: Optional[str] = None
    archive_filename: Optional[str] = None
    workdir: Optional[str] = None
    platform_overrides: Dict[str, PlatformOverride] = field(default_factory=dict)
    extra_env: Dict[str, str] = field(default_factory=dict)
    post_install: Optional[str] = ""
    custom_build_fn: Optional[str] = None
    ffmpeg_configure_flag: Optional[str] = None
    skip_condition: Optional[str] = None
    extra_libs: str = ""
    sed_patches: Dict[str, str] = field(default_factory=dict)
    
    def get_url(self) -> str:
        """Get download URL with version substituted."""
        return self.url.replace("{version}", self.version)
    
    def get_archive_filename(self) -> str:
        """Get archive filename."""
        if self.archive_filename:
            return self.archive_filename.replace("{version}", self.version)
        url = self.get_url()
        return url.split("/")[-1]
    
    def get_target_dir(self) -> str:
        """Get target directory name."""
        if self.archive_dirname:
            return self.archive_dirname.replace("{version}", self.version)
        fname = self.get_archive_filename()
        name = fname
        for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz"):
            if name.endswith(ext):
                name = name[:-len(ext)]
                break
        return name
    
    def is_available(
        self,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        platform_info: Optional[Any] = None,
    ) -> bool:
        """Check if component should be built.
        
        Args:
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name ("linux", "darwin", "windows").
            tools: Available tools dict.
            
        Returns:
            True if component should be built.
        """
        if self.gpl_only and not gpl_enabled:
            return False
        if self.non_gpl_only and gpl_enabled:
            return False
        if self.linux_only and platform != "linux":
            is_windows_ucrt64 = (
                platform == "windows"
                and self.windows_ucrt64_supported
                and platform_info is not None
                and getattr(platform_info, "is_msys2", False)
                and getattr(platform_info, "is_ucrt64", False)
            )
            if not is_windows_ucrt64:
                return False
        if self.macos_only and platform != "darwin":
            return False
        return True


class ComponentRegistry:
    """Registry of all build components."""
    
    def __init__(self):
        """Initialize component registry."""
        self._components: List[Component] = []
        self._build_components()
    
    def _build_components(self) -> None:
        """Build the component list in build order."""
        self._add_build_tools()
        self._add_crypto()
        self._add_video_codecs()
        self._add_audio_codecs()
        self._add_image_codecs()
        self._add_other_libs()
        self._add_hw_accel()
        self._add_target()
    
    def _add_build_tools(self) -> None:
        """Add build tool components."""
        self._components.extend([
            Component(
                name="giflib",
                version="5.2.2",
                url="https://sf-eu-introserv-1.dl.sourceforge.net/project/giflib/giflib-5.x/giflib-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_giflib",
                system_component=True,
                system_tool_name="giflib",
            ),
            Component(
                name="pkg-config",
                version="0.29.2",
                url="https://pkgconfig.freedesktop.org/releases/pkg-config-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--with-pc-path={workspace}/lib/pkgconfig",
                    "--with-internal-glib",
                ],
                system_component=True,
                system_tool_name="pkg-config",
            ),
            Component(
                name="yasm",
                version="1.3.0",
                url="https://github.com/yasm/yasm/releases/download/v{version}/yasm-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=["--prefix={workspace}"],
                system_component=True,
                system_tool_name="yasm",
            ),
            Component(
                name="nasm",
                version="3.01",
                url="https://www.nasm.us/pub/nasm/releasebuilds/{version}/nasm-{version}.tar.xz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                system_component=True,
                system_tool_name="nasm",
            ),
            Component(
                name="zlib",
                version="1.3.2",
                url="https://github.com/madler/zlib/releases/download/v{version}/zlib-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=["--static", "--prefix={workspace}"],
                system_component=True,
                system_tool_name="zlib",
            ),
            Component(
                name="m4",
                version="1.4.20",
                url="https://ftpmirror.gnu.org/gnu/m4/m4-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=["--prefix={workspace}"],
                system_component=True,
                system_tool_name="m4",
            ),
            Component(
                name="autoconf",
                version="2.72",
                url="https://ftpmirror.gnu.org/gnu/autoconf/autoconf-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=["--prefix={workspace}"],
                depends_on=["m4"],
                system_component=True,
                system_tool_name="autoconf",
            ),
            Component(
                name="automake",
                version="1.18.1",
                url="https://ftpmirror.gnu.org/gnu/automake/automake-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=["--prefix={workspace}"],
                depends_on=["autoconf"],
                system_component=True,
                system_tool_name="automake",
            ),
            Component(
                name="libtool",
                version="2.5.4",
                url="https://ftpmirror.gnu.org/libtool/libtool-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--enable-static",
                    "--disable-shared",
                ],
                system_component=True,
                system_tool_name="libtool",
            ),
            Component(
                name="cmake",
                version="4.2.3",
                url="https://github.com/Kitware/CMake/releases/download/v{version}/cmake-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--parallel={num_jobs}",
                    "--", "-DCMAKE_USE_OPENSSL=OFF",
                ],
                extra_env={"CXXFLAGS_EXTRA": "-std=c++11"},
                system_component=True,
                system_tool_name="cmake",
            ),
            Component(
                name="meson",
                version="1.8.2",
                url="https://github.com/mesonbuild/meson/releases/download/{version}/meson-{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=["--prefix={workspace}"],
                system_component=True,
                system_tool_name="meson",
            ),
            Component(
                name="ninja",
                version="1.12.1",
                url="https://github.com/ninja-build/ninja/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.BUILD_TOOL,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_ninja",
                system_component=True,
                system_tool_name="ninja",
            ),
        ])
    
    def _add_crypto(self) -> None:
        """Add crypto components."""
        self._components.extend([
            Component(
                name="gettext",
                version="0.22.5",
                url="https://ftpmirror.gnu.org/gettext/gettext-{version}.tar.gz",
                category=ComponentCategory.CRYPTO,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--enable-static",
                    "--disable-shared",
                    "--without-libiconv-prefix",
                    "--without-libintl-prefix",
                    "--disable-c++",
                ],
                platform_overrides={
                    "linux": PlatformOverride(
                        extra_cflags="-std=gnu11",
                    ),
                },
                gpl_only=True,
            ),
            Component(
                name="openssl",
                version="3.6.1",
                url="https://github.com/openssl/openssl/archive/refs/tags/openssl-{version}.tar.gz",
                category=ComponentCategory.CRYPTO,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_openssl",
                gpl_only=True,
                archive_filename="openssl-{version}.tar.gz",
                ffmpeg_configure_flag="--enable-openssl",
            ),
            Component(
                name="gmp",
                version="6.3.0",
                url="https://ftpmirror.gnu.org/gnu/gmp/gmp-{version}.tar.xz",
                category=ComponentCategory.CRYPTO,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                non_gpl_only=True,
            ),
            Component(
                name="nettle",
                version="3.10.2",
                url="https://ftpmirror.gnu.org/gnu/nettle/nettle-{version}.tar.gz",
                category=ComponentCategory.CRYPTO,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                    "--disable-openssl",
                    "--disable-documentation",
                    "--libdir={workspace}/lib",
                ],
                depends_on=["gmp"],
                non_gpl_only=True,
            ),
            Component(
                name="gnutls",
                version="3.8.12",
                url="https://www.gnupg.org/ftp/gcrypt/gnutls/v3.8/gnutls-{version}.tar.xz",
                category=ComponentCategory.CRYPTO,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                    "--disable-doc",
                    "--disable-tools",
                    "--disable-cxx",
                    "--disable-tests",
                    "--disable-gtk-doc-html",
                    "--disable-libdane",
                    "--disable-nls",
                    "--enable-local-libopts",
                    "--disable-guile",
                    "--with-included-libtasn1",
                    "--with-included-unistring",
                    "--without-p11-kit",
                ],
                depends_on=["nettle"],
                non_gpl_only=True,
            ),
        ])
    
    def _add_video_codecs(self) -> None:
        """Add video codec components."""
        self._components.extend([
            Component(
                name="dav1d",
                version="1.5.3",
                url="https://code.videolan.org/videolan/dav1d/-/archive/{version}/dav1d-{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                ],
                requires_tools=["python3", "meson", "ninja"],
                ffmpeg_configure_flag="--enable-libdav1d",
            ),
            Component(
                name="svtav1",
                version="4.0.1",
                url="https://gitlab.com/AOMediaCodec/SVT-AV1/-/archive/v{version}/SVT-AV1-v{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DENABLE_SHARED=off",
                    "-DBUILD_SHARED_LIBS=OFF",
                    "-DCMAKE_BUILD_TYPE=Release",
                ],
                workdir="Build/linux",
                archive_filename="svtav1-{version}.tar.gz",
                platform_overrides={
                    "linux": PlatformOverride(
                        # SVT-AV1 4.0.1 includes <sched.h>/<pthread.h> from a header
                        # (svt_threads.h) and relies on _GNU_SOURCE exposing
                        # locale_t, clockid_t, posix_memalign, strcasecmp etc.
                        # On modern glibc (2.43) with -std=c11 these GNU/POSIX
                        # extensions are hidden by __STRICT_ANSI__, so the
                        # build fails. Same workaround as the gettext override.
                        extra_cflags="-std=gnu11",
                    ),
                },
                ffmpeg_configure_flag="--enable-libsvtav1",
            ),
            Component(
                name="rav1e",
                version="0.8.1",
                url="https://github.com/xiph/rav1e/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CARGO,
                requires_tools=["cargo", "rustc"],
                ffmpeg_configure_flag="--enable-librav1e",
            ),
            Component(
                name="x264",
                version="0480cb05",
                url="https://code.videolan.org/videolan/x264/-/archive/{version}/x264-{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_x264",
                archive_filename="x264-{version}.tar.gz",
                gpl_only=True,
                ffmpeg_configure_flag="--enable-libx264",
            ),
            Component(
                name="x265",
                version="8be7dbf",
                url="https://bitbucket.org/multicoreware/x265_git/get/8be7dbf8159ddfceea4115675a6d48e1611b8baa.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_x265",
                archive_filename="x265-{version}.tar.gz",
                gpl_only=True,
                ffmpeg_configure_flag="--enable-libx265",
            ),
            Component(
                name="libvpx",
                version="1.16.0",
                url="https://github.com/webmproject/libvpx/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_libvpx",
                archive_filename="libvpx-{version}.tar.gz",
                ffmpeg_configure_flag="--enable-libvpx",
            ),
            Component(
                name="xvidcore",
                version="1.3.7",
                url="https://downloads.xvid.com/downloads/xvidcore-{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                platform_overrides={
                    "windows": PlatformOverride(
                        # xvidcore 1.3.7 defines `bool` via typedef in headers.
                        # GCC 16 defaults to C23 where `bool` is a keyword, so
                        # force a pre-C23 dialect on Windows/UCRT64.
                        extra_cflags="-std=gnu11",
                    ),
                },
                workdir="build/generic",
                gpl_only=True,
                ffmpeg_configure_flag="--enable-libxvid",
            ),
            Component(
                name="vid_stab",
                version="1.1.1",
                url="https://github.com/georgmartius/vid.stab/archive/v{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DBUILD_SHARED_LIBS=OFF",
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DUSE_OMP=OFF",
                    "-DENABLE_SHARED=off",
                ],
                archive_filename="vid.stab-{version}.tar.gz",
                gpl_only=True,
                ffmpeg_configure_flag="--enable-libvidstab",
            ),
            Component(
                name="av1",
                version="3.12.0",
                url="https://aomedia.googlesource.com/aom/+archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CMAKE,
                workdir="aom_build",
                configure_args=[
                    "-DENABLE_TESTS=0",
                    "-DENABLE_EXAMPLES=0",
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DCMAKE_INSTALL_LIBDIR=lib",
                ],
                archive_dirname="av1",
                archive_filename="av1-{version}.tar.gz",
                archive_strip_components=0,
                ffmpeg_configure_flag="--enable-libaom",
            ),
            Component(
                name="zimg",
                version="3.0.6",
                url="https://github.com/sekrit-twc/zimg/archive/refs/tags/release-{version}.tar.gz",
                category=ComponentCategory.VIDEO_CODEC,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_zimg",
                archive_filename="zimg-{version}.tar.gz",
                archive_dirname="zimg-release-{version}",
                ffmpeg_configure_flag="--enable-libzimg",
            ),
        ])
    
    def _add_audio_codecs(self) -> None:
        """Add audio codec components."""
        self._components.extend([
            Component(
                name="lv2",
                version="1.18.10",
                url="https://lv2plug.in/spec/lv2-{version}.tar.xz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                ],
                requires_tools=["python3", "meson", "ninja"],
                skip_condition="disable_lv2",
            ),
            Component(
                name="waflib",
                version="b600c92",
                url="https://gitlab.com/drobilla/autowaf/-/archive/{version}/autowaf-{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.HEADERS_ONLY,
                archive_filename="autowaf.tar.gz",
                archive_dirname="autowaf-{version}",
                skip_condition="disable_lv2",
            ),
            Component(
                name="serd",
                version="0.32.8",
                url="https://gitlab.com/drobilla/serd/-/archive/v{version}/serd-v{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                ],
                requires_tools=["python3", "meson", "ninja"],
                skip_condition="disable_lv2",
            ),
            Component(
                name="pcre",
                version="8.45",
                url="https://altushost-swe.dl.sourceforge.net/project/pcre/pcre/{version}/pcre-{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                archive_filename="pcre-{version}.tar.gz",
                skip_condition="disable_lv2",
            ),
            Component(
                name="zix",
                version="0.8.0",
                url="https://gitlab.com/drobilla/zix/-/archive/v{version}/zix-v{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                ],
                requires_tools=["python3", "meson", "ninja"],
                skip_condition="disable_lv2",
            ),
            Component(
                name="sord",
                version="0.16.22",
                url="https://gitlab.com/drobilla/sord/-/archive/v{version}/sord-v{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                ],
                requires_tools=["python3", "meson", "ninja"],
                skip_condition="disable_lv2",
            ),
            Component(
                name="sratom",
                version="0.6.22",
                url="https://gitlab.com/lv2/sratom/-/archive/v{version}/sratom-v{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "-Ddocs=disabled",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                ],
                requires_tools=["python3", "meson", "ninja"],
                skip_condition="disable_lv2",
            ),
            Component(
                name="lilv",
                version="0.26.4",
                url="https://gitlab.com/lv2/lilv/-/archive/v{version}/lilv-v{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.MESON,
                configure_args=[
                    "--prefix={workspace}",
                    "-Ddocs=disabled",
                    "--buildtype=release",
                    "--default-library=static",
                    "--libdir={workspace}/lib",
                    "-Dcpp_std=c++11",
                ],
                requires_tools=["python3", "meson", "ninja"],
                skip_condition="disable_lv2",
                ffmpeg_configure_flag="--enable-lv2",
            ),
            Component(
                name="opencore",
                version="0.1.6",
                url="https://deac-ams.dl.sourceforge.net/project/opencore-amr/opencore-amr/opencore-amr-{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                archive_filename="opencore-amr-{version}.tar.gz",
                ffmpeg_configure_flag="--enable-libopencore_amrnb --enable-libopencore_amrwb",
            ),
            Component(
                name="lame",
                version="3.100",
                url="https://sourceforge.net/projects/lame/files/lame/{version}/lame-{version}.tar.gz/download?use_mirror=gigenet",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                archive_filename="lame-{version}.tar.gz",
                ffmpeg_configure_flag="--enable-libmp3lame",
                platform_overrides={
                    "windows": PlatformOverride(
                        # langinfo.h is a POSIX-only header not available on
                        # Windows/MinGW.  The LAME frontend (parse.c) includes
                        # it; we only need libmp3lame.a for FFmpeg, so disable
                        # the frontend entirely on Windows.
                        configure_args_override=[
                            "--prefix={workspace}",
                            "--disable-shared",
                            "--enable-static",
                            "--disable-frontend",
                        ],
                    ),
                },
            ),
            Component(
                name="opus",
                version="1.6.1",
                url="https://downloads.xiph.org/releases/opus/opus-{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                ffmpeg_configure_flag="--enable-libopus",
            ),
            Component(
                name="libogg",
                version="1.3.6",
                url="https://ftp.osuosl.org/pub/xiph/releases/ogg/libogg-{version}.tar.xz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
            ),
            Component(
                name="libvorbis",
                version="1.3.7",
                url="https://ftp.osuosl.org/pub/xiph/releases/vorbis/libvorbis-{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_libvorbis",
                depends_on=["libogg"],
                ffmpeg_configure_flag="--enable-libvorbis",
            ),
            Component(
                name="libtheora",
                version="1.2.0",
                url="https://ftp.osuosl.org/pub/xiph/releases/theora/libtheora-{version}.tar.gz",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--with-ogg-libraries={workspace}/lib",
                    "--with-ogg-includes={workspace}/include/",
                    "--with-vorbis-libraries={workspace}/lib",
                    "--with-vorbis-includes={workspace}/include/",
                    "--enable-static",
                    "--disable-shared",
                    "--disable-oggtest",
                    "--disable-vorbistest",
                    "--disable-examples",
                    "--disable-spec",
                ],
                depends_on=["libogg", "libvorbis"],
                ffmpeg_configure_flag="--enable-libtheora",
            ),
            Component(
                name="fdk_aac",
                version="2.0.3",
                url="https://sourceforge.net/projects/opencore-amr/files/fdk-aac/fdk-aac-{version}.tar.gz/download?use_mirror=gigenet",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                    "--enable-pic",
                ],
                archive_filename="fdk-aac-{version}.tar.gz",
                gpl_only=True,
                ffmpeg_configure_flag="--enable-libfdk-aac",
            ),
            Component(
                name="soxr",
                version="0.1.3",
                url="https://sourceforge.net/projects/soxr/files/soxr-{version}-Source.tar.xz/download?use_mirror=gigenet",
                category=ComponentCategory.AUDIO_CODEC,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DBUILD_SHARED_LIBS:bool=off",
                    "-DWITH_OPENMP:bool=off",
                    "-DBUILD_TESTS:bool=off",
                    "-Wno-dev",
                ],
                archive_filename="soxr-{version}.tar.xz",
                workdir="build",
                ffmpeg_configure_flag="--enable-libsoxr",
            ),
        ])
    
    def _add_image_codecs(self) -> None:
        """Add image codec components."""
        self._components.extend([
            Component(
                name="libtiff",
                version="4.7.1",
                url="https://download.osgeo.org/libtiff/tiff-{version}.tar.xz",
                category=ComponentCategory.IMAGE_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                    "--disable-dependency-tracking",
                    "--disable-lzma",
                    "--disable-webp",
                    "--disable-zstd",
                    "--without-x",
                ],
            ),
            Component(
                name="libpng",
                version="1.6.55",
                url="https://sourceforge.net/projects/libpng/files/libpng16/{version}/libpng-{version}.tar.gz",
                category=ComponentCategory.IMAGE_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                archive_filename="libpng-{version}.tar.gz",
            ),
            Component(
                name="lcms2",
                version="2.18",
                url="https://github.com/mm2/Little-CMS/releases/download/lcms{version}/lcms2-{version}.tar.gz",
                category=ComponentCategory.IMAGE_CODEC,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
            ),
            Component(
                name="libjxl",
                version="0.11.2",
                url="https://github.com/libjxl/libjxl/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.IMAGE_CODEC,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_libjxl",
                archive_filename="libjxl-{version}.tar.gz",
                ffmpeg_configure_flag="--enable-libjxl",
                extra_libs="-llcms2",
            ),
            Component(
                name="libwebp",
                version="1.6.0",
                url="https://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-{version}.tar.gz",
                category=ComponentCategory.IMAGE_CODEC,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DCMAKE_INSTALL_LIBDIR=lib",
                    "-DCMAKE_INSTALL_BINDIR=bin",
                    "-DCMAKE_INSTALL_INCLUDEDIR=include",
                    "-DENABLE_SHARED=OFF",
                    "-DENABLE_STATIC=ON",
                    "-DWEBP_BUILD_CWEBP=OFF",
                    "-DWEBP_BUILD_DWEBP=OFF",
                    "-DWEBP_BUILD_GIF2WEBP=OFF",
                    "-DWEBP_BUILD_IMG2WEBP=OFF",
                    "-DWEBP_BUILD_VWEBP=OFF",
                    "-DWEBP_BUILD_ANIM_UTILS=OFF",
                    "-DWEBP_BUILD_WEBPINFO=OFF",
                    "-DWEBP_BUILD_WEBPMUX=OFF",
                    "-DWEBP_BUILD_EXTRAS=OFF",
                ],
                archive_filename="libwebp-{version}.tar.gz",
                workdir="build",
                ffmpeg_configure_flag="--enable-libwebp",
            ),
        ])
    
    def _add_other_libs(self) -> None:
        """Add other library components."""
        self._components.extend([
            Component(
                name="libsdl",
                version="2.30.12",
                url="https://github.com/libsdl-org/SDL/releases/download/release-{version}/SDL2-{version}.tar.gz",
                category=ComponentCategory.OTHER_LIB,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
            ),
            Component(
                name="FreeType2",
                version="2.14.2",
                url="https://downloads.sourceforge.net/freetype/freetype-{version}.tar.xz",
                category=ComponentCategory.OTHER_LIB,
                build_system=BuildSystem.AUTOTOOLS,
                configure_args=[
                    "--prefix={workspace}",
                    "--disable-shared",
                    "--enable-static",
                ],
                ffmpeg_configure_flag="--enable-libfreetype",
            ),
            Component(
                name="VapourSynth",
                version="73",
                url="https://github.com/vapoursynth/vapoursynth/archive/R{version}.tar.gz",
                category=ComponentCategory.OTHER_LIB,
                build_system=BuildSystem.HEADERS_ONLY,
                ffmpeg_configure_flag="--enable-vapoursynth",
            ),
            Component(
                name="libvmaf",
                version="3.0.0",
                url="https://github.com/Netflix/vmaf/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.OTHER_LIB,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_libvmaf",
                archive_filename="vmaf-{version}.tar.gz",
                archive_dirname="libvmaf",
                requires_tools=["python3", "meson", "ninja"],
                ffmpeg_configure_flag="--enable-libvmaf",
            ),
            Component(
                name="srt",
                version="1.5.4",
                url="https://github.com/Haivision/srt/archive/v{version}.tar.gz",
                category=ComponentCategory.OTHER_LIB,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_srt",
                archive_filename="srt-{version}.tar.gz",
                gpl_only=True,
                ffmpeg_configure_flag="--enable-libsrt",
            ),
            Component(
                name="libzmq",
                version="4.3.5",
                url="https://github.com/zeromq/libzmq/releases/download/v{version}/zeromq-{version}.tar.gz",
                category=ComponentCategory.OTHER_LIB,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_libzmq",
                ffmpeg_configure_flag="--enable-libzmq",
            ),
        ])
    
    def _add_hw_accel(self) -> None:
        """Add hardware acceleration components."""
        self._components.extend([
            Component(
                name="vulkan-headers",
                version="1.4.341.0",
                url="https://github.com/KhronosGroup/Vulkan-Headers/archive/refs/tags/vulkan-sdk-{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                ],
                archive_filename="Vulkan-Headers-{version}.tar.gz",
                workdir="build",
                ffmpeg_configure_flag="--enable-vulkan",
            ),
            Component(
                name="glslang",
                version="16.2.0",
                url="https://github.com/KhronosGroup/glslang/archive/refs/tags/{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_glslang",
                requires_tools=["python3"],
                ffmpeg_configure_flag="--enable-libglslang",
            ),
            Component(
                name="fast-float",
                version="6.1.6",
                url="https://github.com/fastfloat/fast_float/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.HEADERS_ONLY,
                archive_filename="fast_float-{version}.tar.gz",
                archive_dirname="fast_float-{version}",
            ),
            Component(
                name="libplacebo",
                version="7.360.1",
                url="https://github.com/haasn/libplacebo/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.MESON,
                custom_build_fn="build_libplacebo",
                configure_args=[
                    "--prefix={workspace}",
                    "--default-library=static",
                    "-Dshaderc=disabled",
                    "-Dopengl=disabled",
                    "-Dd3d11=disabled",
                    "-Dlcms=disabled",
                    "-Dlibdovi=disabled",
                    "-Dtests=false",
                    "-Ddemos=false",
                ],
                depends_on=["vulkan-headers", "glslang", "fast-float"],
                windows_ucrt64_supported=True,
                ffmpeg_configure_flag="--enable-libplacebo",
            ),
            Component(
                name="nv-codec",
                version="13.0.19.0",
                url="https://github.com/FFmpeg/nv-codec-headers/releases/download/n{version}/nv-codec-headers-{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.MAKE_ONLY,
                build_args=["PREFIX={workspace}"],
                install_args=["PREFIX={workspace}"],
                linux_only=True,
                windows_ucrt64_supported=True,
            ),
            Component(
                name="amf",
                version="1.5.0",
                url="https://github.com/GPUOpen-LibrariesAndSDKs/AMF/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.HEADERS_ONLY,
                archive_filename="AMF-{version}.tar.gz",
                archive_dirname="AMF-{version}",
                linux_only=True,
                ffmpeg_configure_flag="--enable-amf",
            ),
            Component(
                name="opencl-headers",
                version="2025.07.22",
                url="https://github.com/KhronosGroup/OpenCL-Headers/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                ],
                archive_filename="OpenCL-Headers-{version}.tar.gz",
                workdir="build",
                linux_only=True,
                windows_ucrt64_supported=True,
            ),
            Component(
                name="opencl-icd-loader",
                version="2025.07.22",
                url="https://github.com/KhronosGroup/OpenCL-ICD-Loader/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_PREFIX_PATH={workspace}",
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DENABLE_SHARED=OFF",
                    "-DBUILD_SHARED_LIBS=OFF",
                ],
                archive_filename="OpenCL-ICD-Loader-{version}.tar.gz",
                workdir="build",
                linux_only=True,
                windows_ucrt64_supported=True,
                depends_on=["opencl-headers"],
                ffmpeg_configure_flag="--enable-opencl",
            ),
            Component(
                name="onevpl",
                version="2.17.0",
                url="https://github.com/intel/libvpl/archive/refs/tags/v{version}.tar.gz",
                category=ComponentCategory.HW_ACCEL,
                build_system=BuildSystem.CMAKE,
                configure_args=[
                    "-DCMAKE_INSTALL_PREFIX={workspace}",
                    "-DBUILD_SHARED_LIBS=OFF",
                    "-DENABLE_SHARED=OFF",
                    "-DCMAKE_BUILD_TYPE=Release",
                ],
                linux_only=True,
                windows_ucrt64_supported=True,
                system_component=True,
                system_tool_name="vpl",
                ffmpeg_configure_flag="--enable-libvpl",
            ),
        ])
    
    def _add_target(self) -> None:
        """Add FFmpeg target component."""
        self._components.append(
            Component(
                name="ffmpeg",
                version="8.1",
                url="https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n{version}.tar.gz",
                category=ComponentCategory.TARGET,
                build_system=BuildSystem.CUSTOM,
                custom_build_fn="build_ffmpeg",
                archive_filename="FFmpeg-release-{version}.tar.gz",
            ),
        )
    
    def get_all(self) -> List[Component]:
        """Get all components in build order."""
        return list(self._components)
    
    def get_by_name(self, name: str) -> Optional[Component]:
        """Get component by name."""
        for comp in self._components:
            if comp.name == name:
                return comp
        return None
    
    def get_by_category(self, category: ComponentCategory) -> List[Component]:
        """Get components by category."""
        return [c for c in self._components if c.category == category]
    
    def get_buildable(
        self,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        disable_lv2: bool = False,
        enable_libvmaf: bool = True,
        platform_info: Optional[Any] = None,
        full_static: bool = False,
    ) -> List[Component]:
        """Get list of components that should be built.
        
        Args:
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name ("linux", "darwin", "windows").
            tools: Available tools dict.
            disable_lv2: Whether LV2 is disabled.
            enable_libvmaf: Whether libvmaf is enabled.
            platform_info: PlatformInfo for HW acceleration filtering.
            
        Returns:
            List of components to build.
        """
        result = []
        
        for comp in self._components:
            if not comp.is_available(gpl_enabled, platform, tools, platform_info):
                continue

            if not self._is_component_allowed_for_platform_policy(comp, platform, platform_info):
                continue
            
            if comp.skip_condition == "disable_lv2" and disable_lv2:
                continue
            
            if comp.name == "libvmaf" and not enable_libvmaf:
                continue
            
            if comp.requires_tools:
                has_tools = all(
                    tools.get(tool, type("", (), {"available": False})).available
                    for tool in comp.requires_tools
                )
                if not has_tools and comp.name not in ("rav1e",):
                    continue
            
            # Filter HW acceleration components by availability
            if platform_info is not None:
                if comp.name == "nv-codec" and not platform_info.cuda_available:
                    continue
                if comp.name in ("vulkan-headers", "glslang") and not platform_info.vulkan_available:
                    continue
                if comp.name == "amf" and not platform_info.amf_available:
                    continue
                if comp.name in ("opencl-headers", "opencl-icd-loader"):
                    if not platform_info.opencl_available:
                        continue
                if comp.name == "onevpl" and not platform_info.qsv_available:
                    continue

            # libplacebo: permanent component; Vulkan acceleration is opt-in.
            # On Linux full_static, Vulkan must be disabled (no static libvulkan.so).
            # On all other platforms (macOS, Windows UCRT64), Vulkan follows
            # enable_libplacebo_vulkan + vulkan_available.
            if comp.name == "libplacebo":
                # Always include; -Dvulkan= flag is resolved in build_libplacebo()
                pass
            
            result.append(comp)
        
        return result

    def _is_component_allowed_for_platform_policy(
        self,
        component: Component,
        platform: str,
        platform_info: Optional[Any],
    ) -> bool:
        """Apply explicit platform/backend policy constraints."""
        if platform != "windows":
            return True

        if component.category != ComponentCategory.HW_ACCEL:
            return True

        if platform_info is None:
            return False

        backend = getattr(platform_info, "build_backend", "")
        if backend != "windows-msys2-ucrt64":
            return False

        if component.name not in WINDOWS_UCRT64_HW_ACCEL_COMPONENTS:
            return False

        return True
    
    def get_system_components(self) -> List[Component]:
        """Get components that can be provided by the system."""
        return [c for c in self._components if c.system_component]
    
    def get_source_components(self) -> List[Component]:
        """Get components that must be built from source."""
        return [c for c in self._components if not c.system_component]
    
    def get_ffmpeg_configure_flags(
        self,
        built_components: List[str],
        gpl_enabled: bool,
        platform: str,
    ) -> List[str]:
        """Get FFmpeg configure flags based on built components.
        
        Args:
            built_components: List of successfully built component names.
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name.
            
        Returns:
            List of configure flags.
        """
        flags = []
        
        for comp in self._components:
            if comp.name in built_components and comp.ffmpeg_configure_flag:
                flags.extend(comp.ffmpeg_configure_flag.split())
        
        if platform == "darwin":
            flags.append("--enable-videotoolbox")
            flags.append("--enable-opencl")
        
        return flags
