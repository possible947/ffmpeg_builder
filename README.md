# FFmpeg Builder 4.0.0

FFmpeg Builder is an interactive and headless build system for FFmpeg 8.1
(default) and 9.0. It supports native Linux, native Linux builds in WSL, and
macOS. The supported Windows route is **WSL-Mingw**, using the internal
`linux-cross-mingw` backend for FFmpeg 9.0. The former MSYS2 route is
unsupported legacy code.

The builder tracks component progress, provisions project-managed toolchains,
and guards build/resume state against incompatible inputs. Sources are
offline-first: Git LFS archives in `third_party/sources` are used before
optional network downloads. See the [full guide](docs/README.md),
[developer guide](docs/DeveloperReadme.md), and
[changelog](docs/CHANGELOG.md).

## Windows quick start: WSL-Mingw

```text
Windows host → WSL2 Ubuntu → LLVM-MinGW/UCRT → Windows x86-64 FFmpeg
```

WSL runs the build; the executables target Windows. The physical Windows
machine running WSL supplies target hardware, drivers and runtime capability
information. Builds are host-specific, not promised portable to unrelated
machines. Material host or build changes can require a rebuild.

### Prerequisites

- WSL2 Ubuntu with Python 3.12 or newer, Git and Git LFS; a checkout with its
  LFS source archives available.
- Normal WSL host tools for the selected components, including a C/C++
  build environment, make, CMake, Meson, Ninja, pkg-config, NASM/Yasm and
  Perl. The project toolchain store provisions LLVM-MinGW, Rust with Windows
  target support, cargo-c and any required managed compiler profiles.
- For configured CUDA/NVCC work, an externally installed CUDA SDK in WSL and
  its compatible host compiler. For configured Vulkan/libplacebo Vulkan,
  the WSL Vulkan SDK's shader and SPIR-V tools are host prerequisites.
  Configure optional SDK roots using `external_prerequisite_paths.cuda` and
  `.vulkan`, or use `CUDA_HOME`/`CUDA_PATH` and `VULKAN_SDK`.
- Windows supplies UCRT/system runtime, GPU drivers, NVIDIA driver/CUDA
  runtime, Vulkan loader/vendor ICD and OpenCL dispatcher/vendor ICD as
  applicable. Optional VapourSynth requires `VSScript.dll` on Windows.

Do not use Linux SDK libraries as Windows target libraries or globally inject
their pkg-config/CMake/library paths into target discovery. Target headers and
import interfaces are project-managed. Lack of OpenCL in WSL does **not** mean
Windows lacks OpenCL; a Linux OpenCL runtime is not required for this target.

### Setup

From the repository root in WSL, after installing the host prerequisites:

```bash
git lfs pull
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
./scripts/check_python_env.sh
python scripts/prepare_toolchains.py --backend linux-cross-mingw --ffmpeg-version 9.0 --dry-run
python scripts/prepare_toolchains.py --backend linux-cross-mingw --ffmpeg-version 9.0
python -m ffmpeg_builder
```

Toolchain provisioning uses the local archive mirror unless network downloads
are explicitly enabled. The TUI can also prepare recommended profiles via
the toolchain store.

### TUI workflow

1. In **System Report**, choose `c` and select `9.0-windows` at **FFmpeg
   version**. This saves `ffmpeg_version: "9.0"` and
   `target_backend: linux-cross-mingw`. The `8.1` and `9.0` choices are
   native builds; FFmpeg 8.1 is not supported by this Windows backend.
2. Inspect Windows target, NVIDIA/CUDA, Vulkan and OpenCL runtime results in
   the system report. OpenCL target-interface readiness is shown separately
   from WSL diagnostics; SDK host tools are validated during preparation.
   Use `i` for component information, `t` for the toolchain plan, and
   `p` then `p` to prepare recommended toolchains.
3. Use `b` to start the Windows build or `r` to resume when **Previous Build**
   is shown. Both validate target and input identity before reusing outputs;
   previous history alone does not guarantee reuse. The dashboard shows
   component statuses, progress, command details and preparation/acceptance
   messages. The final report lists Windows executables and errors.
4. Enable release creation in configuration to produce the bundle described
   below. The default Windows workspace is separate from native 9.0.

The primary host-optimized Windows profile requires `native_build: true` and
`openmp: true`. With native tuning, target C/C++ uses `-march=native
-mtune=native`; OpenMP is a required configured capability when enabled.
Compatibility configurations can disable these options, but their success
does not replace primary-profile acceptance. The compiler interprets `native`;
the project does not independently reconstruct its selected ISA features.

### Headless workflow

After selecting `9.0-windows` in the TUI, the saved `build_config.yaml` can
also drive the normal headless route:

```bash
python scripts/run_build.py --dry-run
python scripts/run_build.py
python scripts/run_build.py --resume
```

Alternatively, supply an existing YAML profile containing
`ffmpeg_version: "9.0"` and `target_backend: linux-cross-mingw`:

```bash
python scripts/run_build.py --config /path/to/profile.yaml --workspace workspace_windows --dry-run
python scripts/run_build.py --config /path/to/profile.yaml --workspace workspace_windows
python scripts/run_build.py --config /path/to/profile.yaml --workspace workspace_windows --resume
```

Set `make_release: true` in the chosen configuration for a release directory.
The normal route builds the selected dependency graph automatically; separate
engineering acceptance waves are not required. Dry-run performs target
preparation and reports selection, but does not build components.

### Output and runtime

With the default Windows workspace, `<target>` below is
`workspace_90_windows/targets/linux-cross-mingw-<identity>`; a custom
`--workspace` replaces `workspace_90_windows`.

| Path | Contents |
|---|---|
| `<target>/bin/ffmpeg.exe`, `<target>/bin/ffprobe.exe` | Built Windows binaries |
| `<target>/release/` | Bundle and `manifest.json`, when release creation is enabled |
| `<target>/logs/<component>_<step>.log` | Component logs |
| `<target>/build_state.json` | Identity-specific progress and resume state |
| `<target>/prefix/` | Windows target headers, libraries and metadata |

The accepted Windows production bundle contains `ffmpeg.exe`, `ffprobe.exe`
and the verified LLVM-MinGW `libc++.dll`, `libomp.dll`, `libunwind.dll` and
`libwinpthread-1.dll`; it does not include `ffplay.exe`. Windows system/UCRT,
GPU-driver and Vulkan/OpenCL runtimes remain external. The accepted
GPL/nonfree configuration is internal and **not redistributable**.
Clean Linux/native FFmpeg 8.1 and 9.0 builds and a clean `9.0-windows` build
have passed through the normal TUI; the Windows build used production
`linux-cross-mingw` orchestration. The normal CLI production workflow,
identity-checked reuse, runtime acceptance, packaging and relocation are also
accepted. On the recorded Windows target, CUDA/NVENC/NVDEC/CUVID, CUDA filters,
CUDA libvmaf, Vulkan/libplacebo and OpenCL have consumer-driven runtime
evidence. Absent, unsupported or out-of-scope hardware is classified under
the established policy and is not by itself a release blocker. Neural/DNN
support is planned, not part of 4.0. See the
[Windows strategy](docs/windows_build_strategy.md) for scope and evidence.

## Native Linux, WSL/Linux and macOS

Create a Python 3.12+ virtual environment, install with
`python -m pip install -e .`, run `./scripts/check_python_env.sh`, and launch
`python -m ffmpeg_builder`. Native Linux and macOS support FFmpeg 8.1 and 9.0;
native WSL builds produce Linux binaries, not Windows executables. The
[full guide](docs/README.md) covers platform tools, configuration and
toolchain provisioning. Native output defaults to `workspace_81/` or
`workspace_90/`.

Intel oneAPI/ICX production work is future work and is not part of 4.0.0
acceptance. The project source is MIT licensed; FFmpeg and each selected
dependency have their own licensing terms.
