# Windows 11 + MSYS2 UCRT64 — implementation plan

This document describes the staged migration path for running the existing `ffmpeg_builder` application on Windows using MSYS2 UCRT64 while preserving the same user workflow as Linux/macOS.

## Goals

1. Keep one codebase and one UI flow (`python -m ffmpeg_builder`) across platforms.
2. Add Windows support through an explicit platform/toolchain backend, not a forked application.
3. Make environment preparation reproducible with a single bootstrap script.
4. Ensure Windows build targets dual-GPU hardware acceleration (NVIDIA + Intel) for the main production use case.

## Windows hardware acceleration target (fixed requirement)

Primary Windows target machine:

- NVIDIA Titan V
- Intel Arc A750

Required FFmpeg 8.1 capabilities in one build:

1. NVIDIA encode/decode (`nvenc`/`nvdec`) + CUDA filters.
2. Intel encode/decode (QSV via oneVPL/libvpl).
3. Vulkan acceleration path.
4. OpenCL acceleration path.

Acceptance criteria for Windows adaptation:

- `ffmpeg -encoders` exposes both NVIDIA and Intel hardware encoders.
- `ffmpeg -decoders` / `-hwaccels` expose required hardware backends.
- CUDA, Vulkan, and OpenCL filter paths are available (`ffmpeg -filters`).
- Runtime device selection works per pipeline (can explicitly target NVIDIA vs Intel adapter).

## Current baseline and constraints

The current code assumes Linux/macOS in several core places:

- platform selection defaults to `darwin` else `linux`,
- tooling assumes POSIX commands (`./configure`, `make`, `sh -c`),
- path/tool probing uses Linux/macOS conventions (`/usr/...`, `pkg-config`, `lspci`, `/proc`),
- hardware-acceleration toggles are Linux/macOS-specific (VAAPI, VideoToolbox, WSL checks).

This means Windows support is feasible, but it is a platform-extension project, not a small patch.

## Recommended architecture for Windows

Use **MSYS2 UCRT64 as the build runtime** and run the same Python app inside that shell:

- Host: Windows PowerShell (bootstrap, SDK discovery, orchestration helpers)
- Build runtime: MSYS2 UCRT64 (`bash`, GNU tools, UCRT toolchain)
- App invocation: `python -m ffmpeg_builder` inside UCRT64 venv

This keeps command semantics close to existing Linux/macOS behavior and minimizes invasive changes.

## Phase plan

### Phase 0 — environment bootstrap (this stage)

Deliverables:

- `scripts/setup_windows_msys2_ucrt64.ps1`:
  - verifies MSYS2 installation,
  - installs required UCRT64 packages,
  - installs Python runtime dependencies from MSYS2 packages (including `python-psutil`),
  - creates `.venv-msys2-ucrt64` if absent,
  - installs project package with `pip install -e . --no-deps`,
  - runs `scripts/check_python_env.sh`,
  - detects CUDA/Vulkan/OpenCL and generates `scripts/env_windows_msys2_ucrt64.sh`.

- `scripts/check_python_env.sh` enhancement:
  - detects MSYS2/MSYSTEM,
  - prints MSYS2-specific package hints,
  - links bootstrap script as the full setup path.

### Phase 1 — platform model extension

Code changes:

1. Add Windows flag(s) in `PlatformInfo` and detector methods.
2. Introduce explicit platform enum/value: `linux`, `darwin`, `windows`.
3. Extend config model with `windows` section (toolchain/runtime flags).
4. Update UI/reporting to display Windows/MSYS2 mode clearly.

Risk:

- Low/medium. Mostly structural, but touches many platform checks.

### Phase 2 — builder command/backend adaptation

Code changes:

1. Add Windows-specific environment normalization in `FFmpegBuilder._setup_environment`.
2. Introduce backend-specific command adapters where behavior diverges:
   - POSIX in MSYS2 (primary path),
   - optional future MSVC backend (separate mode).
3. Replace hardcoded Linux-specific assumptions (`Build/linux`, `/usr/include`) with backend-aware logic.

Risk:

- Medium/high. Many components have build-system-specific edge cases.

### Phase 3 — component registry and HW acceleration policy

Code changes:

1. Add component availability constraints for Windows/UCRT64.
2. Define initial supported matrix for Windows:
   - include: CUDA/NVENC, Vulkan, OpenCL where available,
   - exclude initially: VAAPI, VideoToolbox, Linux-only oneVPL paths unless validated.
3. Adjust FFmpeg configure flags for Windows backend.

Risk:

- High. Different dependencies ship different behavior on UCRT64.

### Phase 4 — CI and verification

Deliverables:

1. Add Windows GitHub Actions job using MSYS2 UCRT64 shell.
2. Run smoke build (minimal profile), then expanded profiles.
3. Produce verified matrix in docs.

Risk:

- Medium. CI minutes and cache strategy needed.

## Complexity analysis

### Technical complexity drivers

1. **Platform coupling in core classes**: platform assumptions are spread across detector, builder, component filtering, and reporting.
2. **Dependency heterogeneity**: ~50 components include autotools/cmake/meson/custom flows; each may behave differently on UCRT64.
3. **Hardware SDK integration**: CUDA/Vulkan/OpenCL need robust path normalization between Windows and MSYS2.
4. **Static/shared linking differences**: linker flags and runtime linkage behavior differ from Linux/macOS conventions.

### Operational complexity drivers

1. MSYS2 package set drift over time.
2. Optional SDK availability differs machine-to-machine.
3. Interactive user flow must remain simple despite platform-specific internals.

## Proposed implementation strategy

1. Keep one app, one UI, one state manager.
2. Add a backend abstraction layer only where command semantics diverge.
3. Start with MSYS2 UCRT64 path first; defer MSVC backend until UCRT64 path is stable.
4. Establish a minimal Windows build profile before enabling full component set.
5. Treat SDKs as optional capabilities with explicit diagnostics, not hard requirements.

## Recommended Windows codec/filter stack

Build-time focus:

- NVIDIA: `--enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuda-nvcc` (+ `--enable-libnpp` when available)
- Intel: `--enable-libvpl` and D3D11-based QSV path
- Vulkan: `--enable-vulkan` (and glslang stack when needed)
- OpenCL: `--enable-opencl`
- OpenMP: enable where supported to keep parity with Linux/macOS performance behavior

Package baseline in MSYS2 UCRT64:

- `mingw-w64-ucrt-x86_64-ffnvcodec-headers`
- `mingw-w64-ucrt-x86_64-libvpl`
- `mingw-w64-ucrt-x86_64-vulkan-headers`
- `mingw-w64-ucrt-x86_64-vulkan-loader`
- `mingw-w64-ucrt-x86_64-opencl-headers`
- `mingw-w64-ucrt-x86_64-opencl-icd`
- `mingw-w64-ucrt-x86_64-llvm-openmp`

OpenMP note:

- For GCC-based UCRT64 builds, OpenMP runtime is provided by the toolchain (`libgomp`).
- Installing `llvm-openmp` keeps Clang-compatible OpenMP runtime available for mixed toolchain scenarios.

## How to use the base program on Windows (target workflow)

1. From PowerShell, run bootstrap:
   - `powershell -ExecutionPolicy Bypass -File scripts\setup_windows_msys2_ucrt64.ps1`
2. Open MSYS2 UCRT64 shell.
3. In repository:
   - `source ./scripts/env_windows_msys2_ucrt64.sh` (if generated),
   - `source ./.venv-msys2-ucrt64/bin/activate`,
   - `python -m ffmpeg_builder`.
4. Use the same interactive flow as Linux/macOS.

## Immediate next coding steps after bootstrap

1. Introduce `windows` in `PlatformInfo` + platform selection in `app.py` and `builder.py`.
2. Add `windows` config dataclass section with UCRT64 defaults.
3. Guard Linux/macOS-only component logic with explicit platform checks.
4. Add first Windows profile (minimal component set) to reach green end-to-end build.
