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
    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        prefix = cross.paths.target_prefix
        for relative in (
            "include/zlib.h",
            "include/zconf.h",
            "lib/libz.a",
            "lib/pkgconfig/zlib.pc",
        ):
            path = prefix / relative
            if not path.is_file() or not path.resolve().is_relative_to(prefix.resolve()):
                raise BuildError(component.name, f"Required target zlib missing: {path}")
        perl = cross.host_tools["perl"]
        if not perl:
            raise BuildError(component.name, "Host Perl is required.")
        env.update(cross.autotools.environment)
        env.pop("CROSS_COMPILE", None)
        env.pop("PERL5OPT", None)
        env["RC"] = str(cross.target_tools.windres)
        command = [
            perl,
            "Configure",
            "mingw64",
            f"--prefix={prefix}",
            f"--openssldir={prefix}/ssl",
            "--libdir=lib",
            f"--with-zlib-include={prefix}/include",
            f"--with-zlib-lib={prefix}/lib/libz.a",
            "no-shared",
            "no-tests",
            "no-apps",
            "no-module",
            "zlib",
        ]
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "Configure mingw64",
            "Configure failed",
            command,
            "configure",
            source_dir,
            env,
        )
        builder._run_make(
            component,
            ComponentStatus.BUILDING,
            "make libraries",
            "Build failed",
            source_dir,
            builder.num_jobs,
            env,
        )
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "make install_dev",
            "Install failed",
            ["make", "install_dev"],
            "install",
            source_dir,
            env,
        )
        return
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
