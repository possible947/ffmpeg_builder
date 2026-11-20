"""x265 component builder."""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
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

    if builder.platform_context.cross_environment is not None:
        _build_cross_x265(builder, component, source_dir, env)
        return

    if builder.platform == "darwin" and builder.platform_detector.platform_info.is_arm64:
        env["CXXFLAGS"] = f"-DHAVE_NEON=1 {env.get('CXXFLAGS', '')}"

    # The json11.cpp <cstdint> include for GCC 15/16 libstdc++ is applied
    # by the patch registry (patches/cxx_headers.py) after extraction.
    build_linux = source_dir / "build" / "linux"
    if not build_linux.exists():
        raise BuildError(component.name, "Build directory not found")

    # x265 only recognizes GNU/Clang/Intel-classic compiler IDs; CMake reports
    # the oneAPI icx/icpx compiler as "IntelLLVM", so x265 takes neither its
    # GCC nor its Clang branch and never compiles the SSE3/SSSE3/SSE4.1
    # intrinsic objects (undefined setupIntrinsicDCT_* references at link
    # time). Forcing INTEL_CXX makes x265 take its GCC-like path, which does
    # enable those objects; icx accepts the resulting -msse3/-mssse3/-msse4.1.
    compiler_names = {Path(env.get("CC", "")).name, Path(env.get("CXX", "")).name}
    intel_llvm_flags = (
        ["-DINTEL_CXX=1"]
        if any(name.startswith(("icx", "icpx")) for name in compiler_names)
        else []
    )

    for bitdepth in ["12bit", "10bit", "8bit"]:
        bitdepth_dir = build_linux / bitdepth
        bitdepth_dir.mkdir(parents=True, exist_ok=True)

        cmake_args = [
            f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
            "-DENABLE_SHARED=OFF",
            "-DBUILD_SHARED_LIBS=OFF",
            *intel_llvm_flags,
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
        x265_pc = builder.target_prefix / "lib" / "pkgconfig" / "x265.pc"
        if x265_pc.exists():
            content = x265_pc.read_text()
            content = content.replace("-lgcc_s", "-lgcc_eh")
            x265_pc.write_text(content)
            builder._assert_patch_absent(
                component, x265_pc, "-lgcc_s", "x265.pc -lgcc_s -> -lgcc_eh (full_static)"
            )


def _archive_members(builder: FFmpegBuilder, component: Component, archive: Path) -> list[str]:
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError(component.name, "Cross environment is required.")
    result = subprocess.run(
        [str(cross.target_tools.ar), "t", str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    inspection = subprocess.run(
        [str(cross.target_tools.readobj), "--file-headers", str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    members: list[str] = result.stdout.splitlines()
    formats = [
        line.split(":", 1)[1].strip()
        for line in inspection.stdout.splitlines()
        if line.startswith("Format:")
    ]
    if (
        result.returncode
        or inspection.returncode
        or not members
        or len(formats) != len(members)
        or any(fmt != "COFF-x86-64" for fmt in formats)
        or any(name.endswith(".a") for name in members)
    ):
        raise BuildError(component.name, f"Invalid target archive: {archive}")
    return members


def _build_cross_x265(
    builder: FFmpegBuilder, component: Component, source_dir: Path, env: dict[str, str]
) -> None:
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError(component.name, "Cross environment is required.")
    nasm = shutil.which("nasm", path=env.get("PATH"))
    if not nasm:
        raise BuildError(component.name, "Required host NASM is unavailable.")
    root = source_dir / "build" / "cross-mingw"
    source = source_dir / "source"
    if not source.is_dir():
        raise BuildError(component.name, "x265 CMake source directory not found.")
    for bitdepth in ("12bit", "10bit", "8bit"):
        directory = root / bitdepth
        directory.mkdir(parents=True, exist_ok=True)
        options = [
            "-G",
            "Ninja",
            f"-DCMAKE_TOOLCHAIN_FILE={cross.cmake_toolchain_file}",
            f"-DCMAKE_INSTALL_PREFIX={builder._ws_str()}",
            f"-DNASM_EXECUTABLE={nasm}",
            f"-DCMAKE_ASM_NASM_COMPILER={nasm}",
            "-DCMAKE_ASM_NASM_OBJECT_FORMAT=win64",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            "-DENABLE_SHARED=OFF",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DENABLE_CLI=OFF",
            "-DENABLE_HDR10_PLUS=ON",
            "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
        ]
        if bitdepth != "8bit":
            options += ["-DHIGH_BIT_DEPTH=ON", "-DEXPORT_C_API=OFF"]
            if bitdepth == "12bit":
                options.append("-DMAIN12=ON")
        else:
            (directory / "libx265.a").unlink(missing_ok=True)
            for other in ("10bit", "12bit"):
                shutil.copyfile(
                    root / other / "libx265.a", directory / f"libx265_main{other[:2]}.a"
                )
            options += [
                "-DEXTRA_LIB=x265_main10.a;x265_main12.a",
                "-DEXTRA_LINK_FLAGS=-L.",
                "-DLINKED_10BIT=ON",
                "-DLINKED_12BIT=ON",
            ]
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            f"cmake (configure-{bitdepth})",
            f"Configure {bitdepth} failed",
            ["cmake", *options, str(source)],
            f"configure-{bitdepth}",
            directory,
            env,
        )
        builder._run_step(
            component,
            ComponentStatus.BUILDING,
            f"cmake --build ({bitdepth})",
            f"Build {bitdepth} failed",
            ["cmake", "--build", ".", "--parallel", str(builder.num_jobs)],
            f"build-{bitdepth}",
            directory,
            env,
        )
        _archive_members(builder, component, directory / "libx265.a")

    directory = root / "8bit"
    main = directory / "libx265_main.a"
    shutil.copyfile(directory / "libx265.a", main)
    archives = [main, directory / "libx265_main10.a", directory / "libx265_main12.a"]
    expected = Counter(
        member for archive in archives for member in _archive_members(builder, component, archive)
    )
    script = (
        "CREATE libx265_combined.a\n"
        + "".join(f"ADDLIB {archive.name}\n" for archive in archives)
        + "SAVE\nEND\n"
    )
    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "merge target bit-depth archives",
        "Merge target bit-depth archives failed",
        [str(cross.target_tools.ar), "-M"],
        "merge-libs",
        directory,
        env,
        stdin=script,
    )
    merged = directory / "libx265_combined.a"
    if Counter(_archive_members(builder, component, merged)) != expected:
        raise BuildError(component.name, "x265 archive merge lost or added members.")
    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "index combined archive",
        "Index combined archive failed",
        [str(cross.target_tools.ranlib), str(merged)],
        "ranlib",
        directory,
        env,
    )
    merged.replace(directory / "libx265.a")
    builder._run_step(
        component,
        ComponentStatus.INSTALLING,
        "cmake --install",
        "Install failed",
        ["cmake", "--install", "."],
        "install",
        directory,
        env,
    )
    _normalise_cross_pc(builder, component)


def _normalise_cross_pc(builder: FFmpegBuilder, component: Component) -> None:
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError(component.name, "Cross environment is required.")
    archives = [builder.target_prefix / "lib" / name for name in ("libx265.a", "libhdr10plus.a")]
    for archive in archives:
        result = subprocess.run(
            [str(cross.target_tools.nm), "-u", str(archive)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise BuildError(component.name, f"Cannot inspect runtime references: {archive}")
        if "__kmpc_" in result.stdout or "omp_" in result.stdout:
            return
    pc = builder.target_prefix / "lib" / "pkgconfig" / "x265.pc"
    text = pc.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if line.startswith("Libs.private:"):
            flags = line.split()[1:]
            unique = list(dict.fromkeys(flag for flag in flags if flag != "-lomp"))
            line = "Libs.private: " + " ".join(unique)
        lines.append(line)
    pc.write_text("\n".join(lines) + "\n", encoding="utf-8")
