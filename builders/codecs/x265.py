"""x265 component builder."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_x265(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build x265 (multi-bitdepth)."""
    env = builder.get_build_env(component)

    if builder.platform == "darwin" and builder.platform_detector.platform_info.is_arm64:
        env["CXXFLAGS"] = f"-DHAVE_NEON=1 {env.get('CXXFLAGS', '')}"

    # The json11.cpp <cstdint> include for GCC 15/16 libstdc++ is applied
    # by the patch registry (patches/cxx_headers.py) after extraction.
    build_linux = source_dir / "build" / "linux"
    if not build_linux.exists():
        raise BuildError(component.name, "Build directory not found")

    for bitdepth in ["12bit", "10bit", "8bit"]:
        bitdepth_dir = build_linux / bitdepth
        bitdepth_dir.mkdir(parents=True, exist_ok=True)

        cmake_args = [
            f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
            "-DENABLE_SHARED=OFF",
            "-DBUILD_SHARED_LIBS=OFF",
        ]

        if bitdepth == "12bit":
            cmake_args.extend(
                [
                    "-DHIGH_BIT_DEPTH=ON",
                    "-DENABLE_HDR10_PLUS=ON",
                    "-DEXPORT_C_API=OFF",
                    "-DENABLE_CLI=OFF",
                    "-DMAIN12=ON",
                ]
            )
        elif bitdepth == "10bit":
            cmake_args.extend(
                [
                    "-DHIGH_BIT_DEPTH=ON",
                    "-DENABLE_HDR10_PLUS=ON",
                    "-DEXPORT_C_API=OFF",
                    "-DENABLE_CLI=OFF",
                ]
            )
        else:
            extra_libs = "x265_main10.a;x265_main12.a"
            if builder.platform == "linux":
                extra_libs += ";-ldl"
            cmake_args.extend(
                [
                    "-DENABLE_SHARED=OFF",
                    "-DBUILD_SHARED_LIBS=OFF",
                    f"-DEXTRA_LIB={extra_libs}",
                    "-DEXTRA_LINK_FLAGS=-L.",
                    "-DLINKED_10BIT=ON",
                    "-DLINKED_12BIT=ON",
                ]
            )

            # Copy 10bit and 12bit libraries into 8bit build dir before linking
            shutil.copy(build_linux / "10bit" / "libx265.a", bitdepth_dir / "libx265_main10.a")
            shutil.copy(build_linux / "12bit" / "libx265.a", bitdepth_dir / "libx265_main12.a")

        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            f"cmake (configure-{bitdepth})",
            f"Configure {bitdepth} failed",
            ["cmake", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"] + cmake_args + ["../../../source"],
            f"configure-{bitdepth}",
            bitdepth_dir,
            env,
        )

        builder._run_step(
            component,
            ComponentStatus.BUILDING,
            "cmake --build (multi-bitdepth)",
            f"Build {bitdepth} failed",
            ["cmake", "--build", ".", "--parallel", str(builder.num_jobs)],
            f"build-{bitdepth}",
            bitdepth_dir,
            env,
        )

    eight_dir = build_linux / "8bit"
    lib_main = eight_dir / "libx265.a"
    lib_main10 = eight_dir / "libx265_main10.a"
    lib_main12 = eight_dir / "libx265_main12.a"

    shutil.copy(build_linux / "10bit" / "libx265.a", lib_main10)
    shutil.copy(build_linux / "12bit" / "libx265.a", lib_main12)

    # Rename 8bit library before merging (matching original build-ffmpeg script)
    lib_main_renamed = eight_dir / "libx265_main.a"
    shutil.move(str(lib_main), str(lib_main_renamed))

    if builder.platform == "darwin":
        # x265 multi-bitdepth merge on macOS requires Apple libtool.
        # GNU libtool (glibtool) fails for this static archive merge.
        libtool = "libtool"
        if shutil.which("xcrun"):
            xcrun_result = subprocess.run(
                ["xcrun", "-f", "libtool"],
                capture_output=True,
                text=True,
            )
            if xcrun_result.returncode == 0:
                resolved = xcrun_result.stdout.strip()
                if resolved:
                    libtool = resolved
        elif Path("/usr/bin/libtool").exists():
            libtool = "/usr/bin/libtool"
        if libtool == "libtool" and Path("/usr/bin/libtool").exists():
            libtool = "/usr/bin/libtool"

        builder._run_step(
            component,
            ComponentStatus.BUILDING,
            "merge-libs",
            "Merge libs failed",
            [
                libtool,
                "-static",
                "-o",
                "libx265.a",
                "libx265_main.a",
                "libx265_main10.a",
                "libx265_main12.a",
            ],
            "merge-libs",
            eight_dir,
            env,
        )
    else:
        m_script = "CREATE libx265.a\nADDLIB libx265_main.a\nADDLIB libx265_main10.a\nADDLIB libx265_main12.a\nSAVE\nEND\n"
        builder._run_step(
            component,
            ComponentStatus.BUILDING,
            "merge-libs",
            "Merge libs failed",
            ["ar", "-M"],
            "merge-libs",
            eight_dir,
            env,
            stdin=m_script,
        )

    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "cmake --install",
        "Install failed",
        ["cmake", "--install", "."],
        "install",
        eight_dir,
        env,
    )

    if builder.config.full_static and builder.platform == "linux":
        x265_pc = builder.workspace / "lib" / "pkgconfig" / "x265.pc"
        if x265_pc.exists():
            content = x265_pc.read_text()
            content = content.replace("-lgcc_s", "-lgcc_eh")
            x265_pc.write_text(content)
            builder._assert_patch_absent(
                component, x265_pc, "-lgcc_s", "x265.pc -lgcc_s -> -lgcc_eh (full_static)"
            )
