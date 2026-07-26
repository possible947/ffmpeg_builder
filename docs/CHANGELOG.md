# Changelog

All notable changes to the FFmpeg Builder project.

## [Unreleased]

### Planned — libplacebo Vulkan integration (3 phases)

> **Stage 1 ✅ released 2026-07-25** — Linux + Windows WSL2 + Windows MSYS2-UCRT64. No lcms2 colour-management. libplacebo is disabled by default (`enable_libplacebo: false`). Only activates when `vulkan_available` is detected at runtime. Disabled automatically when `full_static: true` on Linux (system `libvulkan.so` is required at link time and cannot be bundled statically). Verified: Windows 11 + MSYS2 UCRT64 / GCC 16.1.0 / Intel Arc A750 + NVIDIA TITAN V.
>
> **Stage 2 (next)** — Same platform set, plus `liblcms2` as a build component providing colour-space conversion inside libplacebo. Adds `liblcms2` component (Meson/autotools), `-Dlcms=enabled` in libplacebo configure, and `-llcms2` extralibs pass through to FFmpeg. Stage 1 full_static restriction carries over.
>
> **Stage 3** — Add macOS as a supported platform (MoltenVK backend). Requires detecting MoltenVK/`VK_ICD_FILENAMES` at platform-detect time. Remove `linux_only` restriction; add macOS-specific configure overrides and link flags. Stage 1/2 full_static restriction does not apply on macOS (frameworks handle Vulkan).

### Rules

- **libplacebo + liblcms2 are always disabled when `full_static: true` (Linux)** — `libvulkan.so` (system Vulkan ICD loader) is a runtime shared library with no static archive; linking FFmpeg fully statically against it is not supported. When `full_static: true` the builder skips `libplacebo` (and `liblcms2` in Stage 2) even if `enable_libplacebo: true` and `vulkan_available: true`.

### Added

- **libplacebo Vulkan integration (Stage 1)** — New optional component `libplacebo` v7.360.1 (Meson). Enabled when `enable_libplacebo: true` (default `false`) **and** `vulkan_available` is detected at runtime. Disabled automatically when `full_static: true` on Linux. Supports Linux, Windows WSL2, and Windows MSYS2-UCRT64. Depends on `vulkan-headers` and `glslang` (already built). FFmpeg configure flag: `--enable-libplacebo`. Adds `BuildConfig.enable_libplacebo: bool = False`; ConfigScreen and start-screen config table expose the new option. On Linux, `-lvulkan` is appended to FFmpeg `extralibs` when libplacebo is in the build set (and `full_static` is off). Meson flags: `-Dopengl=disabled -Dd3d11=disabled -Dshaderc=disabled -Dlibdovi=disabled -Dlcms=disabled`. Requires Python `jinja2` for GLSL preprocessing (project dependency via `python -m pip install -e .`; MSYS2 package `mingw-w64-ucrt-x86_64-python-jinja` in Windows bootstrap). **Verified 2026-07-25** on Windows 11 + MSYS2 UCRT64 and **verified 2026-07-26** on Fedora Linux 44: end-to-end FFmpeg 8.1 build completes successfully with libplacebo enabled

- **Cross-platform OpenMP support** — `BuildConfig.openmp: bool = True` (top-level, all platforms). When enabled, `-fopenmp` is added to `CFLAGS`/`CXXFLAGS`; the linker receives `-fopenmp` (GCC auto-links `libgomp`) on Linux and Windows UCRT64, or `-L/opt/local/lib -lomp` (MacPorts libomp) on macOS. `soxr`'s `-DWITH_OPENMP:bool=off` CMake flag is flipped to `on` automatically when `openmp` is enabled. `MacOSConfig.openmp` removed (replaced by top-level field). UI now shows and toggles the `OpenMP` setting

- **Windows UCRT64 verified build** — End-to-end FFmpeg 8.1 build confirmed working on Windows 11 + MSYS2 UCRT64 (GCC 16.1.0). A test build was successfully completed on **Windows 11 / Intel Core i9-7980XE / Intel Arc A750 / NVIDIA TITAN V** with **OpenMP, CUDA, NVENC/NVDEC, Intel dec/enc, Vulkan, and OpenCL** enabled. All configured components build and link correctly. See **Verified Environments** in the README for the full feature list
- **macOS verified build (current session)** — End-to-end FFmpeg 8.1 build now completes successfully on macOS after applying the OpenMP runtime linking and FFmpeg compiler-selection fixes (`gcc`/Apple clang fallback eliminated for OpenMP builds).

- **Windows UCRT64 Intel QSV enablement** — Added QSV support path for `windows-msys2-ucrt64`: `onevpl` is now allowed by Windows HW-accel policy, QSV detection on UCRT64 requires Intel GPU plus pkg-config oneVPL module (`vpl`/`libvpl`), and FFmpeg configure now enables `libvpl` on supported UCRT64 setups instead of forcing `--disable-libvpl`
- **Windows bootstrap QSV readiness check** — `scripts/setup_windows_msys2_ucrt64.ps1` now validates Intel GPU presence plus oneVPL pkg-config module (`vpl`/`libvpl`) and reports explicit QSV prerequisite status after environment setup
- **Linux + WSL2 bootstrap guides in documentation** — Added dedicated setup sections to root `README.md` and `docs/README.md` for local repository cloning with Git LFS, Python virtual environment creation, editable install, and first-run validation (`check_python_env.sh`, `python -m ffmpeg_builder`)

### Changed

- **`--enable-pthreads` → `--enable-w32threads` on UCRT64** — POSIX pthreads are not a system library on MSYS2 UCRT64; the builder now passes `--enable-w32threads` to FFmpeg configure when the backend is `windows-msys2-ucrt64`. All other platforms continue to use `--enable-pthreads`
- **System giflib policy across all platforms** — `giflib` is now treated as a required system-provided component across Linux, macOS, Windows WSL2, and Windows MSYS2-UCRT64. Source-download/build fallback for giflib is removed.

### Fixed

- **macOS OpenMP runtime linker resolution (`ld: library not found for -lomp`)** — OpenMP setup now resolves the runtime library location dynamically (`libomp`/`libgomp`/`libiomp5`) across common macOS toolchain paths (MacPorts/Homebrew), adds the matching `-L` and `-Wl,-rpath` flags, and surfaces a clear configuration error when no compatible runtime is installed.

- **macOS x264 build failure in CLI path (GPAC `strcpy` macro conflict)** — x264 custom build now passes `--disable-cli` so the FFmpeg library build no longer pulls CLI-only GPAC/lavf code paths that fail on recent macOS GPAC headers.

- **macOS FFmpeg configure compiler mismatch (`gcc is unable to create an executable file`)** — FFmpeg configure now receives explicit `--cc/--cxx` from builder environment and macOS compiler resolution now prefers configured/auto-detected MacPorts clang. This prevents fallback to `/usr/bin/gcc` (Apple clang shim without OpenMP), fixing the `C compiler test failed` path when `openmp: true`.

- **giflib system detection on macOS/MSYS2 paths** — System-component probing now recognizes giflib from common non-`/usr/include` prefixes and pkg-config variants (`giflib`, `gif`), plus MSYS2 UCRT64 include/lib locations, preventing false “missing giflib” detection on valid setups.

- **MSYS2 bootstrap Git LFS package target** — `scripts/setup_windows_msys2_ucrt64.ps1` now installs `mingw-w64-ucrt-x86_64-git-lfs` instead of `git-lfs`, which is not a valid target in current MSYS2 repositories. This removes repeated bootstrap failures during `pacman -S --needed ...` with `error: target not found: git-lfs`

- **libvmaf CUDA build with OpenMP (`nvcc fatal: Unknown option '-fopenmp'`)** — For libvmaf CUDA path, the builder now strips raw `-fopenmp` from inherited `CFLAGS`/`CXXFLAGS`/`LDFLAGS` and forwards OpenMP to nvcc host compilation via `NVCC_PREPEND_FLAGS=-Xcompiler=-fopenmp`

- **PKG_CONFIG_PATH corruption in MSYS path normalizer** — Regex `([A-Za-z]):/` matched mid-word (e.g. `pkgconfi` + `g:/`), corrupting paths containing drive-letter sequences. Fixed with `(^|:)([A-Za-z]):/` anchor so only actual Windows drive prefixes are converted

- **`pkg-config.EXE` path format on UCRT64** — Native `pkg-config.EXE` requires Windows-style paths (`E:/…`) with `;` separator when invoked directly from a Python subprocess. FFmpeg `./configure` runs under bash and needs MSYS-style paths (`/e/…`) with `:`. Implemented dual-context strategy: `self.env["PKG_CONFIG_PATH"]` uses POSIX format for bash-based tools; `_build_meson()` and `_build_cmake()` override with Windows format for direct subprocess calls

- **Backslash paths in shell commands** — `str(Path(…))` on Windows returns backslash-separated paths that act as escape sequences in POSIX shell. Added `_ws_str()` helper returning the workspace path with forward slashes; applied to all 14+ command-argument substitutions

- **Linux-only `extralibs` on Windows** — `-ldl -lpthread -lm -lz` do not exist on Windows. For `windows-msys2-ucrt64` the initial `extralibs` is now `""` and only Windows-compatible additions (`-lstdc++`, `-llcms2`, `-lws2_32`, etc.) are appended

- **CUDA `nvcc` flags on UCRT64** — `nvcc` on Windows requires MSVC `cl.exe`, which is absent from the MSYS2 toolchain. `--enable-cuda-nvcc` and `--enable-cuda-llvm` are now skipped on UCRT64. Hardware encode/decode APIs (`cuvid`, `nvdec`, `nvenc`, `ffnvcodec`) that work via CUDA headers with GCC remain enabled

- **`libjxl` build used `make` instead of CMake** — `build_libjxl()` called `execute_make()` / `execute_install()` but the libjxl CMake configure generates Ninja (not Makefiles) on MSYS2. Replaced with `cmake --build . --parallel N` / `cmake --install .`

- **`build_ffmpeg()` used CMake instead of autotools** — FFmpeg uses `./configure + make`; the function was incorrectly calling `cmake --build/--install`. Replaced with `make -j{N}` / `make install`
- **BOM in generated MSYS2 environment script** — `scripts/env_windows_msys2_ucrt64.sh` is now written as UTF-8 without BOM so `source ./scripts/env_windows_msys2_ucrt64.sh` no longer fails with `#!/usr/bin/env` parsing errors in bash

- **FFmpeg configure failure with `--enable-openmp` on UCRT64** — FFmpeg 8.1 does not support this configure option. Removed `--enable-openmp` from `build_ffmpeg()` argument generation; OpenMP remains enabled via compiler/linker flags (`-fopenmp`) and component-level build flags.

- **`lame` build failure: `langinfo.h` not found** — `langinfo.h` is a POSIX-only header used solely by the LAME frontend. Added a Windows `platform_overrides` entry with `configure_args_override: ["--disable-frontend"]` in `components.py`

- **`xvidcore` C23 `bool` typedef conflict** — GCC 16 defaults to C23 where `bool` is a keyword; `encoder.h` contained `typedef int bool;`. Patched with an `#if` guard before the typedef that checks `__STDC_VERSION__ < 202311L`

- **`zimg` / `sord` / `lv2` stack: `libtoolize` and Meson path issues** — Ensured `libtool` is installed via system packages check; `sord` Meson configure fixed via dual PKG_CONFIG_PATH strategy (POSIX for shell tools, Windows for direct subprocess)

- **glslang `update_glslang_sources.py` not executable** — `executor.py` wrapped all `./` scripts with `sh.exe`; Python scripts cannot be run by `sh`. Added `.py` extension detection: scripts ending in `.py` are now invoked via `sys.executable` instead of `sh.exe`

- **`libzmq` static link failures on Windows** — `libzmq.pc` installed by autotools on Windows was missing `-DZMQ_STATIC` in `Cflags` (causing `__imp_zmq_ctx_new` undefined reference) and `-lws2_32 -lrpcrt4` in `Libs.private` (Windows Sockets / RPC). `build_libzmq()` now applies a post-install patch to `libzmq.pc` on UCRT64

- **`libjxl_threads` not found by FFmpeg configure** — `libjxl_threads.a` uses `std::thread` but `libjxl_threads.pc` omits `-lstdc++` from `Libs.private`. Added `-lstdc++` and `-llcms2` to FFmpeg `extralibs` when `libjxl` is in the built component set (all non-Apple platforms)

- **libplacebo.pc missing SPIRV-Tools transitive dependencies** — FFmpeg configure link test failed with hundreds of `undefined reference to spv*` / `spvtools::*` symbols. `libglslang.a(SpvTools.cpp.obj)` calls into SPIRV-Tools C/C++ APIs (`spvContextCreate`, `spvBinaryToText`, `spvtools::Optimizer`) but Meson does not list `libSPIRV-Tools.a` / `libSPIRV-Tools-opt.a` as direct dependencies of libplacebo in the generated `.pc` file. FFmpeg configure reports this as `ERROR: libplacebo >= 5.229.0 not found using pkg-config`. Fix: `build_libplacebo()` post-install patch now appends `-lSPIRV-Tools-opt -lSPIRV-Tools` after `-lglslang` in `libplacebo.pc` when not already present (applied on all platforms)

- **libplacebo.pc absolute DLL import library paths on MSYS2** — Meson's `cxx.find_library('SPIRV', static: true)` on MSYS2 UCRT64 resolves to `/ucrt64/lib/libSPIRV.dll.a` (DLL import stub) instead of the workspace static `.a`. The absolute path ends up verbatim in `libplacebo.pc` `Libs:`, causing FFmpeg to link against the dynamic stub. Fix: `build_libplacebo()` post-install regex patch rewrites `\S+[/\\]lib*.dll.a` → `-l*` in `libplacebo.pc` (UCRT64 only)
- **libplacebo build include path for C++ sources** — Added `-I{workspace}/include` to global `CXXFLAGS` so Meson C++ compilation for libplacebo can resolve glslang headers (`glslang/Public/ShaderLang.h`) on Linux/WSL2 and macOS builds
- **Linux multiarch pkg-config visibility for libplacebo** — Added `{workspace}/lib/<multiarch>/pkgconfig` to `PKG_CONFIG_PATH` on Linux, so FFmpeg configure can resolve `libplacebo.pc` when Meson installs it under GNU multiarch paths (for example `lib/x86_64-linux-gnu/pkgconfig`)
- **False `libplacebo >= 5.229.0 not found` on FFmpeg resume/retry** — Extracted libplacebo `.pc` normalization into a reusable helper and now run it both after libplacebo install and before FFmpeg configure. This fixes stale-state runs where libplacebo was already marked completed but metadata still contained unsafe static archive path forms or incorrect SPIRV link ordering
- **Linux libplacebo build stopped in shader-generation step (`ModuleNotFoundError: jinja2`)** — `build_libplacebo()` now performs an explicit preflight check (`python3 -c "import jinja2"`) with actionable error text, and prepends the active Python environment's module path to `PYTHONPATH` so Meson custom commands using `/usr/bin/python3` can still import `jinja2` from the project environment


- **Windows UCRT64 HW-acceleration policy (phase 3)** — Added explicit Windows component constraints in `ComponentRegistry`: for `HW_ACCEL` category only `nv-codec`, `vulkan-headers`, `glslang`, `opencl-headers`, and `opencl-icd-loader` are eligible, and only when runtime is detected as UCRT64 (`MSYSTEM=UCRT64`)
- **Windows platform model (phase 1)** — Added explicit normalized platform value (`linux` / `darwin` / `windows`) to `PlatformInfo`, plus Windows runtime flags (`is_windows`, `is_msys2`, `is_ucrt64`, `msystem`) and detector plumbing so platform-dependent code no longer assumes “not darwin = linux”
- **Windows config section** — Extended `BuildConfig` with a `windows` subsection (`backend`, `command_mode`, `msys2_root`, `prefer_system_packages`) and updated `build_config.yaml` defaults for UCRT64 mode

- **Windows UCRT64 bootstrap script** — Added `scripts/setup_windows_msys2_ucrt64.ps1` to prepare the full Windows 11 + MSYS2 UCRT64 build environment for `ffmpeg_builder`: validates active UCRT64 toolchain, installs required build tooling, installs Python runtime dependencies from MSYS2 packages, creates/repairs `.venv-msys2-ucrt64`, runs `check_python_env.sh`, detects CUDA/Vulkan/OpenCL, and generates `scripts/env_windows_msys2_ucrt64.sh`
- **Windows acceleration package baseline** — Bootstrap now installs FFmpeg hardware acceleration dependencies for Windows builds: `ffnvcodec-headers` (NVIDIA), `libvpl` (Intel QSV/oneVPL), Vulkan headers/loader/validation stack, OpenCL headers/ICD loader, and shader toolchain packages (`shaderc`, `glslang`)
- **OpenMP package on Windows bootstrap** — Added `mingw-w64-ucrt-x86_64-llvm-openmp` to keep OpenMP runtime available in UCRT64 environments (useful for GCC/Clang parity with Linux/macOS OpenMP-enabled workflows)

- **Package-manager style TUI** — Replaced the single-component progress screen with a live dashboard showing all buildable components, their statuses, and a service message log. The start screen now uses letter hotkeys (`b`/`r`/`c`/`w`/`i`/`q` + Enter) and a new `InfoScreen` displays the full component list with pagination. Component statuses are now driven by real builder phases: `pending`, `system`, `downloading`, `config`, `build`, `install`, `complete`, `fail`, `skip`. Added `ComponentStatus.SYSTEM` for components available on the host. The `BUILDING` status is now set after configure succeeds in all build paths (autotools, cmake, meson, make-only, cargo, custom). Download callbacks update the dashboard without tqdm interference. Error handler uses letter keys (`r`/`s`/`a`/`l`). All UI strings are in English.

- **Incremental dashboard rows** — `BuildDashboard` rows now appear in the table only after a component receives its first status update. On a fresh build the table grows as the async download pool queues each archive; on resume the rows restored from `state.components` are revealed immediately so prior progress is visible from the first frame. The viewport pins in-progress rows (downloading / configuring / building / installing) so the user always sees what is currently happening even when the table is taller than the terminal

- **Per-component download progress** — `Downloader.download()` now accepts a `progress_cb(downloaded, total)` callback. `AsyncDownloadManager` synthesises a per-component callback (throttled to 4 Hz) and forwards it to the dashboard, which renders a live `12.3/45.7 MB (27%)` string in the Detail column and updates the step bar. tqdm is suppressed when a callback is provided so the dashboard is the single source of progress for both compile and download phases

- **Phase detail strings** — `StateManager.mark_component_status` accepts a transient `detail` argument that is forwarded to listeners but not persisted. The builder now passes the running command for each phase (e.g. `make -j40`, `cmake`, `ninja -C build`, `ninja install`, `cargo cinstall`, `install headers`), and the dispatcher stamps `queued`/`starting` on the `downloading` row depending on whether the async manager is active

- **Tools / HW acceleration summary on start screen** — The start screen now collapses the per-tool availability table and the HW acceleration table into a single compact "Available Tools" row (`12/14 available · missing: cargo, rustc · HW accel: VAAPI, AMF`) so the screen fits without scrolling on common terminals. The full per-tool listing is still available via the `i` (Component info) screen

- **Key reference help screen** — Every interactive screen now documents its key bindings. The start screen renders an "Actions" table with three columns (Key / Action / Description) so each key's purpose is obvious at a glance. A new `HelpScreen` (key `h`) lists the full key reference for all screens (start, component info, error prompt, build dashboard). The component info screen and the error prompt each have an `h` option to open the same reference inline. The build dashboard row in the reference explains that the dashboard refreshes automatically and that `Ctrl+C` aborts the build

- **Async source downloads** — Source archives are now downloaded in a background thread pool while the previous component is being built. The build loop only blocks on the download for the component it is about to assemble, so network I/O and CPU compilation overlap. New `BuildConfig` fields control the feature: `async_downloads: bool` (default `true`) and `download_workers: int` (default `4`). Both `build_config.yaml` and `profiles/default.yaml` are updated. The interactive `ConfigScreen` exposes the new settings alongside the existing build flags. Implemented as `AsyncDownloadManager` in `ffmpeg_builder/downloader.py`; the per-file lock in `Downloader` and atomic `<archive>.part → <archive>` rename make the background downloads safe to share with the rest of the system. `FFmpegBuilder` gains `prefetch_downloads()`, `retry_download()`, and `shutdown_downloads()`, and the build loop in `app.py` now prefetches all buildable archives up-front, re-queues a download on retry, and stops the executor in a `finally` block on abort/error

### Changed

- **Windows FFmpeg configure policy (phase 3)** — `build_ffmpeg()` now keeps `VAAPI`, `VideoToolbox`, and `libvpl` explicitly disabled for Windows backend while leaving CUDA/NVENC, Vulkan, and OpenCL paths available when corresponding components are selected
- **Linux-only HW components with explicit UCRT64 override** — `nv-codec`, `opencl-headers`, and `opencl-icd-loader` remain `linux_only` (preserving WSL2/Linux behavior) and are enabled on Windows only via a dedicated `windows_ucrt64_supported` gate under `windows-msys2-ucrt64` backend
- **Platform selection in app/builder** — `FFmpegBuilderApp` and `FFmpegBuilder` now use `PlatformDetector.get_platform_name()` instead of binary darwin/linux fallback, enabling explicit Windows path through component filtering and build orchestration
- **System report for backend separation** — Start screen now shows both normalized build platform and resolved backend (`linux-wsl2`, `windows-msys2-ucrt64`, etc.) to make WSL2 vs MSYS2-UCRT64 mode explicit
- **Windows documentation scope** — Expanded Windows docs with a dedicated implementation plan (`ffmpeg_builder/docs/Windows-UCRT64-Implementation-Plan.md`) and fixed hardware target requirements for Windows adaptation (dual GPU: NVIDIA Titan V + Intel Arc A750; required CUDA/NVENC+NVDEC, Intel QSV/oneVPL, Vulkan, OpenCL)
- **Build dashboard layout** — The header is now a single line (`FFmpeg Builder X.Y - Building | Elapsed: HH:MM:SS`) instead of a multi-line panel. The messages panel is fixed at 8 content lines (10 lines including borders) and stays anchored at the bottom of the screen. The component list occupies the remaining fixed-height area and scrolls upward as new rows appear, so the messages panel is never pushed down by a growing table
- **Viewport follows build order** — The visible component rows are now centered on the active component in the original build order (`1/N`, `2/N`, …) rather than being reordered to pin in-progress rows at the top. This keeps the progression readable while still making the active phase visible

### Fixed

- **MSYS2 toolchain activation in bootstrap** — `setup_windows_msys2_ucrt64.ps1` now forces UCRT64 shell initialization (`MSYSTEM=UCRT64` + `/etc/profile`) before running commands. This prevents accidental use of `/usr/bin/gcc` (Cygwin target) and ensures `/ucrt64/bin/gcc` is used for package installs and Python builds
- **`psutil` installation failure in UCRT64 venv** — Switched bootstrap Python dependency strategy to MSYS2 runtime packages + `pip install -e . --no-deps`, avoiding local `psutil` wheel builds that fail against current Python 3.14/UCRT64 headers
- **MSYS2 venv visibility of system packages** — `.venv-msys2-ucrt64` is now created with `--system-site-packages` and its `pyvenv.cfg` is repaired to `include-system-site-packages = true`, so `check_python_env.sh` correctly detects dependencies installed via MSYS2 package manager
- **Transient pacman network failures during bootstrap** — Added retry wrapper for package installation phase to reduce setup failures from temporary mirror/download timeouts
- **macOS x265 merge step** — Multi-bitdepth static archive merge now explicitly uses Apple `libtool` (`xcrun -f libtool` / `/usr/bin/libtool` fallback) instead of GNU `glibtool`, which caused `x265: Merge libs failed` on macOS
- **zimg bootstrap tool detection on macOS** — `build_zimg()` now accepts both `libtoolize` and `glibtoolize` (workspace and system locations). This fixes `libtoolize not found` failures on Homebrew/MacPorts setups where GNU libtool is exposed as `glibtoolize`
- **libjxl deps script on macOS without `realpath`** — `build_libjxl()` now patches `deps.sh` to a portable self-path resolution when `realpath` is unavailable, fixing `libjxl: Deps failed` with `./deps.sh: line 12: realpath: command not found`
- **Xiph mirror download fallback** — Downloader now retries Xiph/OSUOSL archives through HTTP fallback URLs when HTTPS mirror TLS chain validation fails. This stabilizes fetches for `opus`, `libogg`, `libvorbis`, and `libtheora` in affected environments

- **Syntax errors in `BuildDashboard`** — Incorrect indentation in `_header` and `_visible_rows`, and an inverted guard in the `add` helper, prevented the application from starting. The dashboard now compiles and renders correctly
- **`build_libvmaf` skipped `BUILDING` phase** — The status went straight from `configuring` to `installing` between `meson setup` and `ninja -C build`, so the dashboard showed `install` while the long compile was actually running. Now the `BUILDING` status is set after configure succeeds, with detail `ninja -C build`

- **`build_libzmq` skipped `BUILDING` phase** — Same issue as `build_libvmaf`: the status jumped from `configuring` to `installing` between `./configure` and `make`. `BUILDING` is now set with detail `make -jN`

### Notes

- **Verified build still passes** — No functional regressions in the build engine; the [1.0.6] Fedora 44 verified run remains the reference for a complete end-to-end build with these UI changes applied

## [1.0.6] — 2026-07-19

### Fixed

- **svtav1 on GCC 16 / glibc 2.43** — SVT-AV1 4.0.1 includes `<sched.h>`/`<pthread.h>` from a project header (`Source/Lib/Codec/svt_threads.h`) and defines `_GNU_SOURCE` there. On modern glibc this is ignored when the translation unit is compiled with `-std=c11` because GCC defines `__STRICT_ANSI__`, which hides GNU/POSIX extensions (`locale_t`, `clockid_t`, `posix_memalign`, `strcasecmp`, etc.). Added a Linux `platform_overrides` entry that appends `-std=gnu11` to svtav1 CFLAGS, mirroring the existing `gettext` workaround. The same override is what the original `build-ffmpeg` script relies on
- **Out-of-sync default C standard** — `ffmpeg_builder/build_config.yaml` still set `linux.c_standard: c11` despite the documented default in `profiles/default.yaml` and the [1.0.5] release notes already declaring `gnu11` as the default. The runtime state file (`workspace/build_state.json`) inherited the stale `c11` value, which is why the failing build used `-std=c11`. Synced `build_config.yaml` to `gnu11` so a fresh checkout also gets the working default
- **x265 on GCC 15/16 / libstdc++** — `source/dynamicHDR10/json11/json11.cpp` uses `uint8_t` but only includes `<limits>`. Starting with libstdc++ shipped in GCC 15, `<limits>` no longer transitively pulls in `<cstdint>`, so `uint8_t` is undeclared and the 12-bit x265 build fails. Added a source patch in `build_x265()` that injects `#include <cstdint>` right after `<limits>`, matching the `sed -i '23a #include <cstdint>'` line from the original `build-ffmpeg` script
- **FFmpeg configure: `libjxl_threads >= 0.7.0 not found`** — libjxl 0.11.2 ships a `libjxl_threads.pc` that omits `-lstdc++` from `Libs`/`Libs.private`, so FFmpeg's `require_pkg_config` link test fails with undefined references to `std::condition_variable`/`operator new`. Added `-lstdc++` to `extralibs` whenever `libjxl` is in the build set (Linux only; macOS already uses `-lc++` via the `libvmaf` branch). libjxl itself is a C++ library and the C++ runtime is needed for the threads runner even though the pkg-config file does not declare it
- **FFmpeg configure: `SvtAv1Enc >= 0.9.0 not found` and vid.stab link error** — On Fedora/RHEL-family distributions, CMake's default `CMAKE_INSTALL_LIBDIR` is `lib64`, so SVT-AV1 4.0.1 and vid.stab install their libraries and pkg-config files to `<workspace>/lib64/` and `<workspace>/lib64/pkgconfig/`, but the builder only searched `<workspace>/lib/`. Added `<workspace>/lib64/pkgconfig` to `PKG_CONFIG_PATH` and `<workspace>/lib64` to `LDFLAGS` so FFmpeg's `require_pkg_config` resolves `SvtAv1Enc.pc` and the linker finds `libSvtAv1Enc.a` and `libvidstab.a`

### Notes

- **Target environment** — Debugging performed on Fedora Linux 44, dual AMD Instinct MI50, dual Intel Xeon Broadwell, GCC 16.1.1, glibc 2.43, Python 3.14. The svtav1 and x265 failures reproduce deterministically on this environment and are fixed by the changes above
- **Successful Fedora 44 build** — Full FFmpeg 8.1 build (45/57 components, 12 LV2/OpenCL/AMF/VapourSynth-system items skipped on this environment) completed end-to-end on Fedora Linux 44 with GCC 16.1.1 and glibc 2.43. The resulting `ffmpeg` binary statically links `libsvtav1`, `libx264`, `libx265` (multi-bitdepth), `libaom`, `libdav1d`, `libjxl`, `libfdk_aac`, `libvpx`, `libmp3lame`, `libopus`, `libvorbis`, `libtheora`, `libsrt`, `libzmq`, `librav1e`, `libvmaf`, `libwebp`, `libfreetype`, `vid.stab`, `libssl`, `libcrypto`, `libsdl`, `libzmq`, `libopencore-amrnb/wb`, and all GPL/non-free codecs enabled. The build is the first verified run on this exact hardware (AMD Instinct MI50 + Intel Xeon Broadwell, Fedora 44)

## [1.0.5] — 2026-07-19

### Fixed

- **Executable entry point** — Added `ffmpeg_builder/ffmpeg_builder` wrapper so the application can be launched directly without `python -m`
- **gettext on GCC 16 / glibc 2.43** — Added Linux platform override that appends `-std=gnu11` to `gettext` CFLAGS. This works around `__builtin_va_arg_pack()` errors caused by the combination of `gettext 0.22.5`, GCC 16, and glibc 2.43
- **Default Linux C standard** — Changed default `linux.c_standard` from `c11` to `gnu11` in `build_config.yaml` and `profiles/default.yaml` for broader compatibility with modern glibc headers
- **GPU detection** — `system_info.gpu_info` was never populated; now detected via `lspci -nn` (with DRM sysfs fallback) so the system report shows the actual GPU models
- **AMF detection on AMD GPUs** — AMF is now enabled when an AMD GPU is detected, because the `amf` component downloads the required headers from GPUOpen. Previously it was only enabled if system headers existed in `/usr/include/AMF`, which prevented AMF from being built on clean AMD systems
- **Intel QSV false positive** — PCI vendor check now restricts matching to display/3D class devices only. Previously any Intel PCI device (chipset, USB, MEI, etc.) incorrectly enabled QSV, causing `onevpl` to be built on Xeon + AMD systems
- **rav1e build function** — Removed non-existent `custom_build_fn="build_rav1e"`; rav1e now uses the generic `_build_cargo` path as intended
- **Previous build progress display** — Fixed UI to show progress against `total_steps` (e.g., 13/57) instead of only the number of tracked components
- **Platform override application** — Fixed `get_build_env()` condition from `component.name in component.platform_overrides` to `self.platform in component.platform_overrides`. Previously overrides (including the `gettext` `-std=gnu11` fix) were never applied
- **OpenSSL on GCC 16** — OpenSSL `./Configure` forces `-std=c11` on x86_64, which breaks GCC 16's inline-assembly handling in `crypto/bn/asm/x86_64-gcc.c`. Added a post-configure patch that replaces `-std=c11` with `-std=gnu11` in `Makefile` and `configdata.pm`

### Notes

- **Successful WSL2 build** — Full FFmpeg 8.1 build completed successfully on WSL2 (Ubuntu 24.04) with NVIDIA CUDA, Vulkan, and all configured codecs enabled

## [1.0.3] — 2026-07-18

### Fixed

- **CUDA configure flags** — Дополнен набор флагов FFmpeg для CUDA до соответствия оригинальному скрипту: добавлены `--enable-nvdec`, `--enable-cuda-llvm`, `--enable-ffnvcodec`. Флаг `--cuda-sdk` не добавлен (устарел в FFmpeg, используется `--enable-cuda-nvcc`). При недоступности CUDA добавлен флаг `--disable-ffnvcodec`
- **CUDA compute capability auto-detection** — Добавлено автоматическое определение compute capability через `nvidia-smi`. Приоритет: переменная окружения `CUDA_COMPUTE_CAPABILITY` → автоопределение через nvidia-smi → значение по умолчанию 52. Поддержка нескольких GPU (выбирается минимальное значение для совместимости)

## [1.0.2] — 2026-07-18

### Fixed

- **EXTRALIBS conditional linking** — Библиотеки `-lcuda`, `-lvulkan`, `-lva` теперь добавляются только если соответствующие компоненты собраны (nv-codec, vulkan-headers, opencl-icd-loader). Ранее они добавлялись безусловно при наличии аппаратного обеспечения, что могло приводить к ошибкам линковки
- **libvmaf C++ runtime** — Добавлена библиотека `-lstdc++` (Linux) или `-lc++` (macOS) при сборке libvmaf, что необходимо для корректной линковки C++ кода библиотеки
- **libjxl lcms2 dependency** — Добавлена библиотека `-llcms2` при сборке libjxl, которая требуется для работы с цветовыми профилями

## [1.0.1] — 2026-07-18

### Fixed

- **Full-static CXXFLAGS** — Добавлен флаг `-fPIC` для CXXFLAGS при full-static сборке (ранее применялся только к CFLAGS, что вызывало ошибки линковки C++ компонентов)
- **Full-static CXXFLAGS order** — Исправлен порядок инициализации CXXFLAGS: теперь стандарт C++ устанавливается до добавления full-static флагов, предотвращая перезапись
- **x265 full-static patch** — Добавлен sed-патч для x265.pkg: замена `-lgcc_s` на `-lgcc_eh` при full-static сборке (аналогично оригинальному скрипту)
- **VAAPI full-static** — VAAPI теперь отключается при full-static сборке (оригинальный скрипт не поддерживает статическую линковку libva)
- **Native build CXXFLAGS** — Флаги `-march=native -mtune=native` теперь применяются и к CXXFLAGS (ранее только к CFLAGS)

### Added

- **srt component** — Добавлен компонент SRT (Secure Reliable Transport) версии 1.5.4 (GPL). Включает sed-патч для full-static сборки (`-lgcc_s` → `-lgcc_eh`). Добавляет `--enable-libsrt` в конфигурацию FFmpeg

### Notes

- **C++17 standard** — Проверена совместимость c++17 со всеми C++ компонентами (x265, glslang, libvmaf, srt и др.). Все компоненты поддерживают C++11/C++14, поэтому c++17 безопасен и оставлен без изменений

## [1.0.0] — 2026-07-18

### Added

- **Core build system** — Python-based interactive FFmpeg 8.1 builder replacing the bash `build-ffmpeg` script
- **Interactive TUI** — Rich terminal interface with system report, configuration editor, build progress, and final report screens
- **Platform detection** — Automatic detection of OS, architecture, CPU, RAM, compiler, and 16 build tools
- **Hardware acceleration detection**:
  - CUDA — searches PATH and `/usr/local/cuda*/bin/nvcc`
  - Vulkan — pkg-config, headers, vulkaninfo
  - VAAPI — pkg-config libva
  - AMF — header path detection
  - OpenCL — headers + ICD loader + vendor ICD files; WSL2-aware
- **Component registry** — ~50 components with version, URL, build system, dependencies, and platform filtering
- **Build engine** — Supports autotools, CMake, Meson, Cargo, make-only, headers-only, and custom build functions
- **State management** — JSON state file with per-component status tracking; supports build resume after interruption
- **Interactive error handling** — On build failure: retry, skip component, abort, or view full log
- **YAML configuration** — Build profiles with GPL, native build, full static, libvmaf, LV2, parallel jobs settings
- **macOS support** — Macports clang detection, OpenMP, VideoToolbox, glibtool for x265 static lib merge
- **Linux support** — GCC/Clang detection, C11/C++17 standards, full-static builds, CUDA/Vulkan integration
- **CUDA build integration** — Adds CUDA paths to CFLAGS/LDFLAGS/PATH, passes `--enable-cuda-nvcc`, `--enable-cuvid`, `--enable-nvenc`, `--cuda-sdk` to FFmpeg configure
- **Vulkan build integration** — Links libvulkan, builds vulkan-headers and glslang from source, passes `--enable-vulkan`, `--enable-libglslang` to FFmpeg
- **HW component filtering** — nv-codec, vulkan-headers, glslang, amf, opencl-headers, opencl-icd-loader are only built when the corresponding HW acceleration is detected
- **Download manager** — File downloads with progress bars (tqdm), retry logic (3 attempts), and integrity checks
- **Command executor** — Subprocess wrapper with log file generation, timeout support, and stdin support (for `ar -M`)
- **Environment check script** — `scripts/check_python_env.sh` verifies Python environment, checks all dependencies, and suggests installation commands for pip, apt, dnf, pacman, zypper, and MacPorts
- **OS detection fix** — Reads `/etc/os-release` for proper distribution name on Linux (e.g., "Ubuntu 24.04.4 LTS" instead of kernel string)
- **Compiler version fix** — Strips trailing `)` from GCC version strings
- **Default build profile** — `ffmpeg_builder/profiles/default.yaml`

### Fixed

- **Retry logic** — Replaced `for`/`enumerate` loop with `while` loop so that retrying a failed component actually re-builds it (previously `idx -= 1` was silently overwritten by `enumerate`)
- **Cleanup order** — `state_file.unlink()` now runs before `shutil.rmtree(workspace)` to avoid accessing files inside a deleted directory
- **x265 `ar -M`** — The merge script for multi-bitdepth x265 on Linux is now passed via stdin (previously the script was constructed but never sent to the process, causing a hang)
- **gettext version** — Changed from non-existent `1.0` to `0.22.5`
- **waflib archive path** — Added `archive_dirname="autowaf-{version}"` so the extracted directory matches the expected source path
- **FFmpeg GPL flags** — Added `--enable-gpl` and `--enable-nonfree` to FFmpeg configure when GPL is enabled (previously only `--enable-version3` was passed)
- **Config mutation** — `BuildConfig.from_dict()` now uses `.get()` instead of `.pop()` to avoid mutating the input dictionary
- **Private method access** — Renamed `_build_component()` to public `build_component()` in builder; app.py calls the public method
- **Meson and ninja** — Added as system components in the build tools registry with a `build_ninja()` custom build function
- **OpenCL detection** — Rewrote to check headers + ICD loader + vendor ICD files; correctly reports unavailable on WSL2 where NVIDIA does not expose OpenCL through the paravirtualized driver
- **Dead code removal** — Removed unused `ui/progress.py` module

### Component Versions

| Category | Components |
|----------|-----------|
| Build tools | giflib 5.2.2, pkg-config 0.29.2, yasm 1.3.0, nasm 3.01, zlib 1.3.2, m4 1.4.20, autoconf 2.72, automake 1.18.1, libtool 2.5.4, cmake 4.2.3, meson 1.8.2, ninja 1.12.1 |
| Crypto (GPL) | gettext 0.22.5, openssl 3.6.1 |
| Crypto (non-GPL) | gmp 6.3.0, nettle 3.10.2, gnutls 3.8.12 |
| Video | dav1d 1.5.3, svtav1 4.0.1, rav1e 0.8.1, x264 0480cb05, x265 8be7dbf, libvpx 1.16.0, xvidcore 1.3.7, vid.stab 1.1.1, aom 3.12.0, zimg 3.0.6 |
| Audio | lv2 1.18.10, serd 0.32.8, pcre 8.45, zix 0.8.0, sord 0.16.22, sratom 0.6.22, lilv 0.26.4, opencore 0.1.6, lame 3.100, opus 1.6.1, libogg 1.3.6, libvorbis 1.3.7, libtheora 1.2.0, fdk_aac 2.0.3, soxr 0.1.3 |
| Image | libtiff 4.7.1, libpng 1.6.55, lcms2 2.18, libjxl 0.11.2, libwebp 1.6.0 |
| Other | libsdl 2.30.12, freetype 2.14.2, vapoursynth 73, libvmaf 3.0.0, srt 1.5.4, libzmq 4.3.5 |
| HW accel | vulkan-headers 1.4.341.0, glslang 16.2.0, nv-codec 13.0.19.0, amf 1.5.0, opencl-headers 2025.07.22, opencl-icd-loader 2025.07.22 |
| Target | FFmpeg 8.1 |
