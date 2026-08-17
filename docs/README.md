# FFmpeg Builder

Interactive Python-based build system for FFmpeg 8.1 on macOS, Linux, and **Windows 11 + MSYS2 UCRT64**.

FFmpeg Builder replaces the traditional bash `build-ffmpeg` script with a modern, interactive interface featuring real-time progress tracking, configuration management, platform-aware hardware acceleration detection, and resumable builds.

## Features

- **Interactive TUI** — Rich terminal interface with system report, component info, configuration editor, and live package-manager style build dashboard
- **Platform Detection** — Automatic detection of CPU, RAM, GPU, compilers, and build tools
- **Hardware Acceleration** — Detects and configures CUDA, Vulkan, VAAPI, AMF, and OpenCL support
- **Resumable Builds** — JSON state file tracks progress; interrupted builds can be resumed
- **Local source mirror by default** — Archives are read from `third_party/sources` first; network fallback is configurable
- **Interactive Error Handling** — On failure, choose to retry, skip component, or abort
- **YAML Configuration** — Human-readable build profiles with platform-specific settings
- **~50 Components** — All codecs, libraries, and tools built from source in correct dependency order
- **macOS Support** — Macports clang detection, OpenMP, VideoToolbox, glibtool handling
- **Linux Support** — GCC/Clang detection, C11/C++17 standards, full-static builds
- **Windows 11 + MSYS2 UCRT64 Support** — Full FFmpeg 8.1 build with CUDA/NVENC, Vulkan, OpenCL; w32threads; static codec libraries

## Requirements

### System

- **Python** >= 3.12 (required for secure `tar` extraction, PEP 706; runs on Python 3.12–3.14)
- **OS**: macOS (11.0+) or Linux (x86_64 / arm64)
- **Disk Space**: ~10 GB for sources and build artifacts

### Build Tools (auto-detected, built from source if missing)

| Tool | Purpose |
|------|---------|
| make / g++ or clang++ | Core compilation |
| pkg-config | Library discovery |
| nasm / yasm | Assembly (x264, x265, dav1d) |
| cmake | CMake-based components |
| python3 | Meson-based components |
| meson / ninja | dav1d, libvmaf, lv2 stack |
| cargo / rustc | rav1e (Rust AV1 encoder) |
| curl / git | Source downloads |

### Python Dependencies

```
rich>=13.0.0
tqdm>=4.65.0
pyyaml>=6.0
requests>=2.31.0
jinja2>=3.1.0
```

Install all dependencies:

```bash
pip install -e .
```

Or use the environment check script:

```bash
./scripts/check_python_env.sh
```

## Windows 11 + MSYS2 UCRT64

Full FFmpeg 8.1 builds are supported on Windows 11 via the MSYS2 UCRT64 toolchain (GCC 16).
All codec libraries are compiled as static archives and linked into `ffmpeg.exe`.

From Windows PowerShell (repository root):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows_msys2_ucrt64.ps1
```

What it does:

- installs required MSYS2/UCRT64 toolchain and build packages,
- installs hardware acceleration dependency packages (ffnvcodec, oneVPL/libvpl, Vulkan, OpenCL),
- installs OpenMP runtime support package (`mingw-w64-ucrt-x86_64-llvm-openmp`),
- installs Python runtime dependencies (`rich`, `tqdm`, `pyyaml`, `requests`) from MSYS2 packages,
- creates `.venv-msys2-ucrt64` in the repository (with `--system-site-packages`) if missing,
- installs project package in editable mode (`pip install -e . --no-deps`) into that venv,
- runs `scripts/check_python_env.sh`,
- detects CUDA/Vulkan/OpenCL/QSV readiness and generates `scripts/env_windows_msys2_ucrt64.sh`.

Then start MSYS2 UCRT64 and run:

```bash
cd /<drive>/<path>/ffmpeg_builder
source ./scripts/env_windows_msys2_ucrt64.sh   # optional, if generated
source ./.venv-msys2-ucrt64/bin/activate
python -m ffmpeg_builder
```

OpenMP on Windows/UCRT64 is recommended: it improves performance in components that use OpenMP-parallel code paths.  
For GCC builds, `-fopenmp` is provided by the installed UCRT64 toolchain (`libgomp` via GCC). The bootstrap additionally installs `llvm-openmp` to keep Clang/OpenMP runtime available as well.

Implementation details are documented in `docs/DeveloperReadme.md`.

## Linux (native)

For Ubuntu/Debian/Fedora/Arch-style Linux hosts, use a local Python virtual environment and sync Git LFS archives.

From a Linux shell:

```bash
# 1) System prerequisites (Debian/Ubuntu example)
sudo apt update
sudo apt install -y git git-lfs python3-venv python3-pip libgif-dev

# 2) Clone repository + source archive mirror (Git LFS)
git clone <repository-url> ffmpeg_builder
cd ffmpeg_builder
git lfs install --local
git lfs pull
git lfs checkout

# 3) Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4) Install project in editable mode
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .

# 5) Validate environment and run
./scripts/check_python_env.sh
python -m ffmpeg_builder
```

Notes:

- `third_party/sources` archives are stored with Git LFS; without `git lfs pull`, builds fail later during extract/configure stages.
- On distributions with externally managed system Python (PEP 668), always use `.venv` (do not install with global `pip`).

## Windows WSL2 (Ubuntu)

WSL2 is supported as a Linux backend (`linux-wsl2`). Setup is the same as native Linux, plus optional CUDA passthrough checks.

From your WSL2 Ubuntu shell:

```bash
# 1) System prerequisites
sudo apt update
sudo apt install -y git git-lfs python3-venv python3-pip libgif-dev

# 2) Clone repository + fetch LFS archives
git clone <repository-url> ffmpeg_builder
cd ffmpeg_builder
git lfs install --local
git lfs pull
git lfs checkout

# 3) Python environment
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .

# 4) Validate and run
./scripts/check_python_env.sh
python -m ffmpeg_builder
```

WSL2 notes:

- CUDA can work in WSL2 when NVIDIA drivers/toolkit are installed correctly; OpenCL is typically unavailable in WSL2.
- If you use `/mnt/<drive>/...` paths for the repository, expect slower I/O than using the Linux filesystem (`~/...`).

## macOS + MacPorts

On macOS, use MacPorts for the build toolchain. **MacPorts clang is required** — the Apple-provided `/usr/bin/clang` shim does not support OpenMP and cannot be used for this project.

From Terminal:

```bash
# 1) Install required MacPorts packages
sudo port selfupdate
sudo port install \
  git git-lfs pkgconfig cmake meson ninja nasm yasm autoconf automake libtool gettext giflib \
  clang-17 libomp \
  python312 py312-pip py312-setuptools py312-wheel py312-rich py312-tqdm py312-yaml \
  py312-requests

# 2) Clone repository + fetch LFS archives
git clone <repository-url> ffmpeg_builder
cd ffmpeg_builder
git lfs install --local
git lfs pull
git lfs checkout

# 3) Create and activate Python environment
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .

# 4) Configure the compiler in build_config.yaml
#    Set macos.clang to the installed MacPorts clang version, e.g.:
#      macos:
#        clang: "macports-clang-17"

# 5) Validate and run
./scripts/check_python_env.sh
python -m ffmpeg_builder
```

macOS notes:

- **MacPorts clang is mandatory.** `openmp: true` (the default) requires a compiler with OpenMP support. MacPorts `clang-17` (or any other installed version) provides this; Apple's system clang does not. The builder will raise a clear error if no MacPorts clang is found when OpenMP is enabled.
- **`macos.clang` must match an installed version.** The `build_config.yaml` entry `macports-clang-17` maps to the binary `clang-mp-17` installed by MacPorts. The start screen displays the configured version (not the highest detected one) so you can verify the correct compiler is selected before starting a build.
- `third_party/sources` is Git LFS-backed; missing LFS pull/checkout leaves pointer files instead of source archives.
- If multiple Python versions are installed via MacPorts, activate the matching venv explicitly (e.g. `python3.12 -m venv .venv`).
- After modifying project source files, reinstall in the venv: `python -m pip install -e .`

```bash
# Clone the repository
git clone <repository-url>
cd ffmpeg_builder

# Install Python dependencies
pip install -e .

# Check your environment
./scripts/check_python_env.sh

# Run the builder
./ffmpeg_builder
# or
python -m ffmpeg_builder
```

## Usage

## Builder Architecture

The build engine now uses a split architecture so the main orchestration path stays smaller and the reusable helper layers are easier to maintain.

### High-level flow

```text
python -m ffmpeg_builder
  -> __main__.py
  -> app.py (FFmpegBuilderApp)
  -> builder.py (FFmpegBuilder)
     -> build_steps.py
     -> component_builders.py
     -> release_bundle.py
     -> executor.py / downloader.py / state.py / components.py / platform_detect.py
```

### Module responsibilities

| Module | Responsibility |
|--------|----------------|
| `builder.py` | Main orchestration entry point. Handles environment setup, component lifecycle flow, download/extract, generic build-system dispatch, and helper-module coordination. |
| `build_types.py` | Shared builder exception types used across the build engine. |
| `build_steps.py` | Reusable helpers for the common mark-status -> run command -> validate result pattern. |
| `component_builders.py` | Central registry for `custom_build_fn` dispatch, replacing implicit `getattr()` lookups. |
| `release_bundle.py` | Portable release directory creation and runtime dependency discovery/copying. |

### Design notes

- `FFmpegBuilder` remains the public entry point used by `app.py`.
- Shared exceptions and step helpers now live outside `builder.py`, reducing hidden coupling.
- Release packaging is isolated from the main component build path.
- This split prepares the codebase for a later move of large custom component build bodies into dedicated modules without changing user-facing behavior.

### Interactive Mode

```bash
python -m ffmpeg_builder
```

The application starts with a **System Report** screen showing:

- Hardware information (CPU, cores, RAM, GPU)
- Software information (OS, compiler, architecture)
- Available build tools and their versions
- Hardware acceleration status (CUDA, Vulkan, VAAPI, AMF, OpenCL)
- Current build configuration

From the main menu, type a letter key and press Enter:

| Key | Action | Description |
|-----|--------|-------------|
| `b` | **Start new build** | Begin building all components from scratch (resets previous state) |
| `r` | **Resume previous build** | Continue from the last interrupted build when state exists |
| `c` | **Edit configuration** | Modify build settings interactively |
| `w` | **Cleanup workspace** | Remove all build artifacts and state |
| `i` | **Component info** | Show the current buildable component set and non-selected components |
| `h` | **Help** | Show the full key reference for all screens (start, info, error prompt, dashboard) |
| `q` | **Exit** | Exit the application |

During a build, the live dashboard shows a compact header line (`FFmpeg Builder X.Y - Building | Elapsed: HH:MM:SS`), a per-component table with `n/total`, name, a 10-segment progress bar, percentage, status (`pending`/`system`/`downloading`/`config`/`build`/`install`/`complete`/`fail`/`skip`), and a Detail column showing the running command (e.g. `make -j40`) or live download progress (e.g. `12.3/45.7 MB (27%)`). The component table occupies a fixed-height area and scrolls upward as new rows appear, so the messages panel below it stays anchored at the bottom with a fixed size (8 lines of content). The viewport follows the active component in build order, keeping the progression readable while the current phase remains visible. Rows appear as downloads are queued and builds start (not pre-populated). The layout automatically adapts to the terminal height. All status colors follow the same legend: complete green, build magenta, config yellow, downloading/system cyan, install blue, fail red, skip yellow dim, pending dim.

On failure, type `r` to retry, `s` to skip, `a` to abort, or `l` to view the full log when available.

### Configuration

Build configuration is stored in `build_config.yaml`:

```yaml
ffmpeg_version: "8.1"
gpl_enabled: false
make_release: false
native_build: false
full_static: false
openmp: true
enable_libvmaf: true
enable_libvmaf_cuda: true
enable_libplacebo_vulkan: false
disable_lv2: false
num_jobs: "auto"
make_timeout_seconds: 0
install_timeout_seconds: 0
async_downloads: true
download_workers: 4
source_archives_dir: "third_party/sources"
allow_network_downloads: false

macos:
  clang: "macports-clang-17"

linux:
  c_standard: "gnu11"
  cxx_standard: "c++17"

windows:
  backend: "msys2-ucrt64"
  command_mode: "posix"
  msys2_root: "C:\msys64"
  prefer_system_packages: true
```

By default, builds are **offline-first**: every archive must exist in `third_party/sources`.
To allow fetching missing archives from the network, set `allow_network_downloads: true`.
For archive integrity validation, components in `components.yaml` may define optional
`sha256` checksums; when present, downloads are verified before extraction.

### Command Line

```bash
# Launch interactive UI (no CLI arguments supported)
python -m ffmpeg_builder
```

## Components

### Build Tools (system or built from source)

giflib, pkg-config, yasm, nasm, zlib, m4, autoconf, automake, libtool, cmake, meson, ninja

### Crypto

| GPL | Non-GPL |
|-----|---------|
| gettext, openssl | gmp, nettle, gnutls |

### Video Codecs

dav1d, svtav1, rav1e, x264 (GPL), x265 (GPL), libvpx, xvidcore (GPL), vid.stab (GPL), aom, zimg

### Audio Codecs

lv2 stack (lv2, serd, pcre, zix, sord, sratom, lilv), opencore, lame, opus, libogg, libvorbis, libtheora, fdk_aac (GPL), soxr

### Image Codecs

libtiff, libpng, lcms2, libjxl, libwebp

### Other Libraries

libsdl, freetype, vapoursynth, libvmaf, srt (GPL), libzmq, giflib

### Hardware Acceleration

vulkan-headers, glslang, fast-float (libplacebo dependency), libplacebo (always built, Vulkan GPU processing opt-in via `enable_libplacebo_vulkan`), nv-codec (Linux/Windows UCRT64), amf (Linux), opencl-headers (Linux/Windows UCRT64), opencl-icd-loader (Linux/Windows UCRT64), onevpl (Linux)

### Target

FFmpeg 8.1

## Hardware Acceleration

The builder automatically detects available hardware acceleration and configures FFmpeg accordingly:

| Technology | Platform | Detection Method |
|------------|----------|------------------|
| **CUDA** | Linux/Windows | nvcc in PATH, then platform-specific default install paths |
| **Vulkan** | Linux/macOS/Windows | pkg-config, headers, vulkaninfo |
| **libplacebo** | Linux/macOS/Windows UCRT64/WSL2 | always built; `enable_libplacebo_vulkan` controls Vulkan GPU backend; disabled on `full_static` Linux |
| **VAAPI** | Linux | pkg-config libva |
| **Intel QSV** | Linux/Windows UCRT64 | Linux: vainfo or PCI Intel GPU (requires VAAPI, disabled in WSL2). Windows UCRT64: Intel GPU + pkg-config oneVPL (`vpl`/`libvpl`) |
| **AMF** | Linux | AMD GPU detected via `lspci` or DRM sysfs; AMF headers are downloaded from GPUOpen |
| **OpenCL** | Linux/macOS/Windows UCRT64 | Linux/Windows: headers + ICD vendor files. macOS: `OpenCL.framework` (always available) |
| **VideoToolbox** | macOS | Always available |

Windows phase-3 policy:

- enabled for UCRT64: CUDA/NVENC path (`nv-codec`), Vulkan (`vulkan-headers`/`glslang`), OpenCL (`opencl-headers`/`opencl-icd-loader`), Intel QSV (`onevpl`/`libvpl`) when Intel GPU + oneVPL pkg-config module are detected.
- explicitly disabled for now: VAAPI, VideoToolbox.
- build backend is explicit: `linux-wsl2` and `windows-msys2-ucrt64` are treated as separate modes with different component eligibility rules.

### CUDA Notes

- CUDA toolkit must be installed (not just the driver)
- On WSL2, OpenCL is not available through the paravirtualized driver
- When CUDA is detected, the builder adds: `--enable-cuda-nvcc`, `--enable-cuvid`, `--enable-nvdec`, `--enable-nvenc`, `--enable-cuda-llvm`, `--enable-ffnvcodec`
- `enable_libvmaf_cuda: true` enables libvmaf CUDA path only when backend is `linux-native` or `linux-wsl2` and an NVCC compile sanity-check passes
- On `windows-msys2-ucrt64`, libvmaf CUDA path remains disabled by policy/toolchain limits; CPU `libvmaf` still works
- For `libvmaf` CUDA builds with `openmp: true`, the builder forwards OpenMP to NVCC host compilation via `NVCC_PREPEND_FLAGS=-Xcompiler=-fopenmp` and removes raw `-fopenmp` from inherited compile/link flags to avoid `nvcc fatal: Unknown option '-fopenmp'`
- CUDA compute capability is automatically detected via `nvidia-smi` (queries all GPUs and uses the minimum value for compatibility)
- Priority: `CUDA_COMPUTE_CAPABILITY` environment variable → auto-detection via nvidia-smi → default value 52
- Example: `export CUDA_COMPUTE_CAPABILITY=75` to override for Turing GPUs
- The builder passes `--nvccflags=-gencode arch=compute_XX,code=sm_XX -O2` to FFmpeg configure
- When CUDA is not available, `--disable-ffnvcodec` is added to prevent build failures
- CUDA include/lib paths are automatically added to the build environment

### Vulkan Notes

- Requires Vulkan SDK or at minimum vulkan-headers and loader
- The builder compiles vulkan-headers and glslang from source when Vulkan is available
- Adds `--enable-vulkan` and `--enable-libglslang` to FFmpeg configure

### libplacebo Notes

- GPU-accelerated video/image rendering and processing library (Vulkan backend)
- **Always built** on all platforms (Linux, macOS, Windows UCRT64/WSL2) — no opt-in required
- `enable_libplacebo_vulkan: true` in `build_config.yaml` enables Vulkan GPU acceleration inside libplacebo; software features (tone mapping, colour space conversion, scaling) are always available regardless
- Disabled when `full_static: true` on Linux (system `libvulkan.so` cannot be bundled statically); on macOS `libvulkan.dylib` is linked normally
- On macOS with Vulkan enabled, `LIBRARY_PATH` and `PKG_CONFIG_PATH` are extended with LunarG SDK paths (`/usr/local/lib`, `/usr/local/lib/pkgconfig`)
- Adds `--enable-libplacebo` to FFmpeg configure; exposes the `libplacebo` filter (`N->V`)
- Built with: `-Dopengl=disabled -Dd3d11=disabled -Dshaderc=disabled -Dlibdovi=disabled -Dlcms=disabled`
- Requires Python `jinja2` for GLSL preprocessing; installed with project dependencies (`pip install -e .`) or via distro package `python3-jinja2`
- Requires `mingw-w64-ucrt-x86_64-python-jinja` on MSYS2 UCRT64 (GLSL preprocessor); installed by the bootstrap script automatically
- Depends on `fast-float` v6.1.6 headers (GitHub release tarballs omit this git submodule; the builder provides it as a separate `HEADERS_ONLY` component)
- Version: 7.360.1

### Intel QSV Notes

- Linux: Intel QSV uses VAAPI backend (`libva`) and is disabled in WSL2.
- Windows UCRT64: Intel QSV uses oneVPL (`vpl`/`libvpl`) and does not require VAAPI.
- Detection is backend-aware: Intel GPU presence + pkg-config oneVPL module check on UCRT64.
- Adds `--enable-libvpl` when QSV is available.

## Build Output

After a successful build, binaries are located in:

```
workspace/bin/ffmpeg
workspace/bin/ffprobe
workspace/bin/ffplay
```

If `make_release: true`, an additional portable release folder is generated:

```
workspace/release/
```

The release folder contains:

- `ffmpeg`, `ffprobe`, `ffplay` (or `.exe` on Windows)
- recursively collected non-system runtime libraries in the same `release` directory
- `manifest.json` with included and missing dependencies

Build logs are stored in:

```
workspace/logs/<component>_<step>.log
```

## Verified Environments

The following environments have been verified to complete a full FFmpeg build:

| Date | OS | Environment | Configuration | Result |
|------|------|-------------|---------------|--------|
| 2026-08-10 | Fedora Linux 44 | x86_64, GCC 16, Python 3.14 (system), full HW acceleration stack | GPL + non-free, native build, openmp, full HW accel, `make_release: true` | Successful full build of FFmpeg 8.1 (`58/58` components) in **20.5 minutes**. Fresh Python venv, fresh config, full HW acceleration stack (CUDA, Vulkan, OpenCL, AMF, libplacebo). |
| 2026-08-02 | macOS 15.5 (Sequoia) | x86_64, Intel Core i7-6950X, AMD Radeon RX Vega 64 8 GB, LunarG Vulkan SDK 1.4.350.1, MacPorts clang-17 | GPL + non-free, native build, openmp, **libplacebo + Vulkan** (`enable_libplacebo_vulkan: true`) | Successful full build of FFmpeg 8.1 (`56/56` components). HW accels: `videotoolbox`, `opencl`, `vulkan`. libplacebo 7.360.1 with Vulkan GPU backend compiled in. VideoToolbox encode/decode, OpenCL filters, Vulkan filters, libplacebo filter all confirmed present. |
| 2026-08-01 | macOS 15.5 (Sequoia) | x86_64, Intel Core i7-6950X, AMD Radeon RX Vega 64, LunarG Vulkan SDK 1.4.350.1, MacPorts clang-17 | GPL + non-free, native build, openmp, `make_release: false` | Successful full build of FFmpeg 8.1 (`52/52` buildable components without libplacebo/Vulkan). HW accels confirmed: `videotoolbox`, `opencl`, `vulkan`. |
| 2026-07-25 | Windows 11 + MSYS2 UCRT64 | x86_64, GCC 16.1.0, Intel Arc A750 + NVIDIA TITAN V (CUDA 12.2) | GPL + non-free, native build, openmp, **libplacebo**, `make_release: true` | Successful full build of FFmpeg 8.1 (`59/59` components) with all configured components including libplacebo 7.360.1; release bundle generated in `workspace/release` (`ffmpeg.exe`, `ffprobe.exe`, `ffplay.exe`, runtime DLL set, `manifest.json`) |
| 2026-07-19 | Ubuntu 24.04 (WSL2) | x86_64, NVIDIA CUDA | GPL + non-free, native build | Successful build of FFmpeg 8.1 with all configured components enabled |
| 2026-07-19 | Fedora Linux 44 | x86_64, dual AMD Instinct MI50, dual Intel Xeon Broadwell, GCC 16.1.1, glibc 2.43 | GPL + non-free, native build | Successful build of FFmpeg 8.1 (45/57 components; 12 LV2/OpenCL/Vulkan/AMF/VapourSynth items not built because the corresponding runtime libraries are not present on this system) |

Build configuration for the verified run:

```yaml
ffmpeg_version: "8.1"
gpl_enabled: true
native_build: true
full_static: false
make_release: true
openmp: true
enable_libvmaf: true
enable_libvmaf_cuda: true
enable_libplacebo_vulkan: false
disable_lv2: false
num_jobs: auto
```

Verified FFmpeg capabilities included:

- CUDA/NVENC/NVDEC: `--enable-cuda-nvcc`, `--enable-cuvid`, `--enable-nvdec`, `--enable-nvenc`, `--enable-cuda-llvm`, `--enable-ffnvcodec`
- Video codecs: `libx264`, `libx265`, `libvpx`, `libaom`, `libsvtav1`, `libdav1d`, `libxvid`, `libwebp`, `libjxl`, `libzimg`
- Audio codecs: `libmp3lame`, `libopus`, `libvorbis`, `libtheora`, `libfdk-aac`, `libsoxr`, `libopencore_amrnb`, `libopencore_amrwb`
- Streaming/protocols: `openssl`, `libsrt`, `libzmq`
- GPU processing: `vulkan`, `libglslang`, `libplacebo` (filter `libplacebo N->V`)
- Other: `libvmaf`, `libvidstab`, `libfreetype`, `vapoursynth`, `lv2`

## Project Structure

```
ffmpeg_builder/
    __main__.py              Entry point
    app.py                   Main application class, screen orchestration
    build_types.py           Shared builder exceptions
    build_steps.py           Shared configure/build/install step helpers
    config.py                YAML configuration management
    state.py                 JSON build state management
    system_report.py         Environment report generation
    components.py            Component registry (~50 components)
    builder.py               Build orchestration entry point
    component_builders.py    Explicit custom builder dispatch registry
    platform_detect.py       OS, architecture, tools, HW acceleration detection
    executor.py              Subprocess wrapper with logging
    downloader.py            File downloads with progress (requests + tqdm)
    release_bundle.py        Release bundle packaging and runtime dependency collection
    ui/
        screens.py           TUI screens: system report, config, info, final
        dashboard.py         Live build dashboard (header / table / messages)
        error_handler.py     Interactive error handling (retry/skip/abort)
    profiles/
        default.yaml         Default build profile
scripts/
    check_python_env.sh      Environment and dependency checker
```

## Troubleshooting

### Build fails at a specific component

1. Check the log file: `workspace/logs/<component>_<step>.log`
2. Use the interactive error handler to retry or skip
3. Resume the build after fixing the issue

### OpenSSL fails on GCC 15/16

If OpenSSL fails with errors in `crypto/bn/asm/x86_64-gcc.c` (e.g. `expected ')' before ':' token`), the OpenSSL `./Configure` script has forced `-std=c11`. The builder now patches the generated `configdata.pm` and regenerates the `Makefile` with `-std=gnu11` automatically.

### x265 fails with "uint8_t does not name a type" in `json11.cpp`

On GCC 15/16 with the bundled libstdc++, `<limits>` no longer transitively pulls in `<cstdint>`, so `uint8_t` is undeclared in `source/dynamicHDR10/json11/json11.cpp`. The builder now injects `#include <cstdint>` after `#include <limits>` before configuring the build, matching the original `build-ffmpeg` script.

### SVT-AV1 fails with "unknown type name 'locale_t' / 'clockid_t'"

SVT-AV1 4.0.1 includes `<sched.h>`/`<pthread.h>` from a project header (`Source/Lib/Codec/svt_threads.h`) and defines `_GNU_SOURCE` there. On modern glibc this is ignored when compiled with `-std=c11` because GCC defines `__STRICT_ANSI__`. The builder applies a Linux `platform_overrides` entry that appends `-std=gnu11` to svtav1 CFLAGS, the same workaround already used for `gettext`.

### FFmpeg configure fails with "libplacebo >= 5.229.0 not found using pkg-config"

If `libplacebo` was already built, FFmpeg can still fail this check when `libplacebo.pc` contains absolute static archive paths or stale dependency ordering from a previous run. The builder now auto-normalizes `libplacebo.pc` before FFmpeg configure (including resume runs), rewrites absolute archives to `-l...` flags, and enforces SPIRV static dependency order.

### CUDA not detected

- Ensure CUDA toolkit is installed (not just the driver)
- Check that `nvcc` is in PATH or at `/usr/local/cuda/bin/nvcc`
- On WSL2, CUDA is available but OpenCL is not

### OpenCL not detected

- Install OpenCL headers: `sudo apt install opencl-headers ocl-icd-dev`
- On WSL2, OpenCL is not available through the paravirtualized driver
- On native Linux with NVIDIA: `sudo apt install nvidia-opencl-icd-<driver-version>`

### Missing Python dependencies

```bash
./scripts/check_python_env.sh    # Check what's missing
pip install -e .                  # Install all dependencies
```

## License

MIT
