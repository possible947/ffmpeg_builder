"""OpenSSL component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...perl_shims import apply_perl_shims_to_env
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_openssl(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build OpenSSL."""
    env = builder.get_build_env(component)

    # Fedora 41+ unbundles historically-core Perl modules (FindBin,
    # IPC::Cmd, Time::Piece) into separate packages that are absent from a
    # minimal install; OpenSSL's Configure/Makefile.in machinery requires
    # all three. Scoped to LinuxGcc15Platform: that strategy already
    # signals "modern rolling-release Linux toolchain", the same
    # environment class where this gap has been observed.
    if type(builder.platform_strategy).__name__ == "LinuxGcc15Platform":
        apply_perl_shims_to_env(builder.workspace, env, on_log=builder.on_log)

    result, log_file = builder.executor.execute_with_log(
        [
            "./Configure",
            f"--prefix={builder._ws_str()}",
            f"--openssldir={builder._ws_str()}",
            "--libdir=lib",
            f"--with-zlib-include={builder._ws_str()}/include/",
            f"--with-zlib-lib={builder._ws_str()}/lib",
            "no-shared",
            "zlib",
        ],
        component.name,
        "configure",
        source_dir,
        env,
    )

    if not result.success:
        raise BuildError(component.name, "Configure failed", log_file)

    # OpenSSL Configure forces -std=c11 on x86_64, which breaks GCC 16's
    # handling of inline assembly in crypto/bn/asm/x86_64-gcc.c. Replace it
    # with -std=gnu11 in the generated configdata.pm and regenerate the
    # Makefile. (configdata.pm only exists post-Configure, so this stays
    # here rather than in the pre-configure patch registry.)
    configdata = source_dir / "configdata.pm"
    if configdata.exists():
        content = configdata.read_text()
        content = content.replace("-std=c11", "-std=gnu11")
        configdata.write_text(content)
        builder._assert_patch_absent(
            component, configdata, "-std=c11", "configdata.pm -std=c11 -> -std=gnu11"
        )
        result2 = builder.executor.execute(
            ["perl", str(configdata)],
            cwd=source_dir,
            env=env,
        )
        if not result2.success:
            raise BuildError(component.name, "configdata.pm regeneration failed")

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
        "make install_sw",
        "Install failed",
        ["make", "install_sw"],
        "install",
        source_dir,
        env,
    )
