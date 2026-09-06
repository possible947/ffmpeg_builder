"""libzmq component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libzmq(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libzmq."""
    env = builder.get_build_env(component)

    if builder.platform == "darwin":
        env["XML_CATALOG_FILES"] = "/usr/local/etc/xml/catalog"

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure", f"--prefix={builder._ws_str()}", "--disable-shared", "--enable-static"],
        "configure",
        source_dir,
        env,
    )

    proxy_cpp = source_dir / "src" / "proxy.cpp"
    if proxy_cpp.exists():
        content = proxy_cpp.read_text()
        old_init = "stats_proxy stats = {0}"
        new_init = "stats_proxy stats = {{{0, 0}, {0, 0}}, {{0, 0}, {0, 0}}}"
        content = content.replace(old_init, new_init)
        proxy_cpp.write_text(content)
        builder._assert_patch_absent(
            component, proxy_cpp, old_init, "libzmq proxy.cpp stats_proxy initializer"
        )

    builder._run_make(
        component,
        ComponentStatus.BUILDING,
        f"make -j{builder.num_jobs}",
        "Build failed",
        source_dir,
        builder.num_jobs,
        env,
    )

    builder._run_install(
        component,
        ComponentStatus.INSTALLING,
        "make install",
        "Install failed",
        source_dir,
        env,
    )

    # On Windows (UCRT64/MinGW), zmq.h uses __declspec(dllimport) by
    # default which causes undefined reference to __imp_* symbols when
    # linking against the static library.  Adding -DZMQ_STATIC to Cflags
    # suppresses dllimport.  Also, libzmq autotools does not add Windows
    # socket libraries to Libs.private; add them here.
    if builder._is_windows_ucrt64_backend():
        pc_file = builder.workspace / "lib" / "pkgconfig" / "libzmq.pc"
        if pc_file.exists():
            text = pc_file.read_text()
            if "-DZMQ_STATIC" not in text:
                text = text.replace(
                    "Cflags: -I${includedir}",
                    "Cflags: -I${includedir} -DZMQ_STATIC",
                )
            for win_lib in ("-lws2_32", "-lrpcrt4"):
                if win_lib not in text:
                    text = text.replace(
                        "Libs.private:",
                        f"Libs.private: {win_lib}",
                    )
            pc_file.write_text(text)
