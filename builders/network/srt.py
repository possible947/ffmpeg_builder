"""SRT component builder."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import TYPE_CHECKING

from ...state import ComponentStatus
from ...build_types import BuildError

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_srt(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build srt."""
    env = builder.get_build_env(component)
    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        prefix = cross.paths.target_prefix
        for relative in (
            "include/openssl/ssl.h",
            "lib/libssl.a",
            "lib/libcrypto.a",
            "lib/pkgconfig/openssl.pc",
            "lib/pkgconfig/libcrypto.pc",
        ):
            path = prefix / relative
            if not path.is_file() or not path.resolve().is_relative_to(prefix.resolve()):
                raise BuildError(component.name, f"Required target OpenSSL missing: {path}")
        cmake_file = source_dir / "CMakeLists.txt"
        script_install = (
            "if(NOT NEED_DESTINATION)\n"
            "\tinstall(PROGRAMS scripts/srt-ffplay TYPE BIN)\n"
            "elseif (DEFINED CMAKE_INSTALL_BINDIR)\n"
            "\tinstall(PROGRAMS scripts/srt-ffplay DESTINATION ${CMAKE_INSTALL_BINDIR})\n"
            "else()\n"
            '\tmessage(WARNING "No location to install scripts/srt-ffplay")\n'
            "endif()\n"
        )
        try:
            text = cmake_file.read_text(encoding="utf-8")
            if text.count(script_install) == 1:
                cmake_file.write_text(text.replace(script_install, "", 1), encoding="utf-8")
            elif "scripts/srt-ffplay" in text:
                raise BuildError(component.name, "Unexpected SRT script install declaration.")
        except OSError as exc:
            raise BuildError(component.name, f"Cannot restrict SRT install: {exc}") from exc
        env.update(cross.compiler_environment)
        for key in list(env):
            if key.startswith(("OPENSSL_", "SSL_", "pkg_cv_SSL_")):
                env.pop(key)
        build_dir = source_dir / "build-cross-mingw"
        build_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "cmake",
            "-S",
            str(source_dir),
            "-B",
            str(build_dir),
            "-G",
            "Ninja",
            f"-DCMAKE_TOOLCHAIN_FILE={cross.cmake_toolchain_file}",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DENABLE_SHARED=OFF",
            "-DENABLE_STATIC=ON",
            "-DENABLE_APPS=OFF",
            "-DENABLE_TESTING=OFF",
            "-DENABLE_ENCRYPTION=ON",
            "-DUSE_ENCLIB=openssl-evp",
            "-DUSE_STATIC_LIBSTDCXX=OFF",
            "-DUSE_OPENSSL_PC=ON",
            "-DSRT_USE_OPENSSL_STATIC_LIBS=ON",
            f"-DOPENSSL_ROOT_DIR={prefix}",
            f"-DCMAKE_PREFIX_PATH={prefix}",
        ]
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "cmake target configure",
            "Configure failed",
            command,
            "configure",
            source_dir,
            env,
        )
        builder._run_step(
            component,
            ComponentStatus.BUILDING,
            "cmake target build",
            "Build failed",
            ["cmake", "--build", str(build_dir), "--parallel", str(builder.num_jobs)],
            "build",
            source_dir,
            env,
        )
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "cmake target install",
            "Install failed",
            ["cmake", "--install", str(build_dir)],
            "install",
            source_dir,
            env,
        )
        _normalize_cross_metadata(builder, component)
        return
    env["OPENSSL_ROOT_DIR"] = builder._ws_str()
    env["OPENSSL_LIB_DIR"] = f"{builder._ws_str()}/lib"
    env["OPENSSL_INCLUDE_DIR"] = f"{builder._ws_str()}/include"

    cmake_args = [
        f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_INSTALL_BINDIR=bin",
        "-DCMAKE_INSTALL_INCLUDEDIR=include",
        "-DENABLE_SHARED=OFF",
        "-DENABLE_STATIC=ON",
        "-DENABLE_APPS=OFF",
        "-DUSE_STATIC_LIBSTDCXX=ON",
    ]

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "cmake .",
        "Configure failed",
        ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["."],
        "configure",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "cmake --build",
        "Build failed",
        ["cmake", "--build", ".", "--parallel", str(builder.num_jobs)],
        "build",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "cmake --install",
        "Install failed",
        ["cmake", "--install", "."],
        "install",
        source_dir,
        env,
    )

    if builder.config.full_static and builder.platform == "linux":
        srt_pc = builder.target_prefix / "lib" / "pkgconfig" / "srt.pc"
        if srt_pc.exists():
            content = srt_pc.read_text()
            content = content.replace("-lgcc_s", "-lgcc_eh")
            srt_pc.write_text(content)


def _normalize_cross_metadata(builder: FFmpegBuilder, component: Component) -> None:
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError(component.name, "Cross environment is required for SRT metadata.")
    archive = builder.target_prefix / "lib/libsrt.a"
    result = subprocess.run(
        [str(cross.target_tools.nm), "-u", str(archive)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise BuildError(component.name, "Cannot inspect SRT target archive symbols.")
    if re.search(r"__kmpc|GOMP_", result.stdout):
        raise BuildError(component.name, "SRT unexpectedly requires OpenMP runtime.")
    for name in ("srt.pc", "haisrt.pc"):
        pc = builder.target_prefix / "lib/pkgconfig" / name
        try:
            lines = pc.read_text(encoding="utf-8").splitlines()
            normalized = []
            found = False
            for line in lines:
                if line.startswith("Libs.private:"):
                    found = True
                    line = "Libs.private: " + " ".join(
                        token for token in line.split(":", 1)[1].split() if token != "-lomp"
                    )
                normalized.append(line)
            if not found:
                raise BuildError(component.name, f"Missing Libs.private in {pc}")
            pc.write_text("\n".join(normalized) + "\n", encoding="utf-8")
        except OSError as exc:
            raise BuildError(component.name, f"Cannot normalize SRT metadata: {exc}") from exc
