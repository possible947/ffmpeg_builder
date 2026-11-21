"""libzmq component builder."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libzmq(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libzmq."""
    env = builder.get_build_env(component)

    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        env.update(cross.autotools.environment)
        polling_header = source_dir / "src" / "polling_util.hpp"
        try:
            text = polling_header.read_text()
            if "#include <new>\n" not in text:
                if text.count("#include <vector>\n") != 1:
                    raise BuildError(component.name, "Unexpected libzmq polling header.")
                polling_header.write_text(
                    text.replace("#include <vector>\n", "#include <new>\n#include <vector>\n", 1)
                )
        except OSError as exc:
            raise BuildError(
                component.name, f"Cannot prepare libzmq polling header: {exc}"
            ) from exc
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "./configure",
            "Configure failed",
            [
                "./configure",
                *cross.autotools.arguments,
                f"--prefix={builder._ws_str()}",
                "--disable-shared",
                "--enable-static",
                "--disable-perf",
                "--disable-curve-keygen",
                "--disable-drafts",
                "--disable-ws",
                "--disable-libunwind",
                "--disable-libbsd",
                "--without-docs",
                "--without-libsodium",
                "--without-libgssapi_krb5",
                "--without-pgm",
                "--without-norm",
                "--without-vmci",
                "--without-tls",
            ],
            "configure",
            source_dir,
            env,
        )
        proxy_cpp = source_dir / "src" / "proxy.cpp"
        if proxy_cpp.exists():
            content = proxy_cpp.read_text()
            old_init = "stats_proxy stats = {0}"
            new_init = "stats_proxy stats = {{{0, 0}, {0, 0}}, {{0, 0}, {0, 0}}}"
            proxy_cpp.write_text(content.replace(old_init, new_init))
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
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "install library and headers",
            "Install failed",
            ["make", "install-libLTLIBRARIES", "install-includeHEADERS", "install-pkgconfigDATA"],
            "install",
            source_dir,
            env,
        )
        _normalize_cross_metadata(builder, component)
        return

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
        pc_file = builder.target_prefix / "lib" / "pkgconfig" / "libzmq.pc"
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


def _normalize_cross_metadata(builder: FFmpegBuilder, component: Component) -> None:
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError(component.name, "Cross environment required for target metadata.")
    prefix = builder.target_prefix
    archive = prefix / "lib/libzmq.a"
    result = subprocess.run(
        [str(cross.target_tools.nm), "-u", str(archive)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode or re.search(r"__kmpc|GOMP_", result.stdout):
        raise BuildError(component.name, "Cannot establish libzmq OpenMP-free target metadata.")
    pc = prefix / "lib/pkgconfig/libzmq.pc"
    la = prefix / "lib/libzmq.la"
    try:
        text = pc.read_text()
        if text.count("Libs.private:") != 1 or text.count("Cflags: -I${includedir}") != 1:
            raise BuildError(component.name, "Unexpected libzmq pkg-config metadata.")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("Libs.private:"):
                lines[i] = "Libs.private: -lc++ -lunwind -liphlpapi -lpthread -lws2_32"
            elif line.startswith("Cflags:"):
                if "-DZMQ_STATIC" not in line.split():
                    lines[i] = line.rstrip() + " -DZMQ_STATIC"
        pc.write_text("\n".join(lines) + "\n")
        text = la.read_text()
        if (
            text.count("inherited_linker_flags=' -fopenmp'")
            + text.count("inherited_linker_flags=''")
            != 1
        ):
            raise BuildError(component.name, "Unexpected libzmq libtool flags.")
        if text.count("dependency_libs='") != 1:
            raise BuildError(component.name, "Unexpected libzmq libtool dependencies.")
        text = text.replace("inherited_linker_flags=' -fopenmp'", "inherited_linker_flags=''")
        if re.search(r"\b(?:Uuid|Rpc)[A-Za-z0-9_]*", result.stdout):
            raise BuildError(component.name, "Unexpected libzmq RPC references.")
        text = text.replace(" -lrpcrt4", "")
        if "dependency_libs=' -lc++ -lunwind" not in text:
            text = text.replace("dependency_libs='", "dependency_libs=' -lc++ -lunwind", 1)
        la.write_text(text)
    except OSError as exc:
        raise BuildError(component.name, f"Cannot normalize libzmq metadata: {exc}") from exc
