# Windows FFmpeg build strategy: analysis and implementation plan (Variant 1: WSL + mingw-w64)

> Audience: an implementing agent/engineer working on a real system (Windows 11 + WSL2 Ubuntu).
> Status: design/plan document. Nothing in it is implemented yet.
> Provenance: produced from a repository analysis plus a design discussion with the maintainer
> (@possible947). Claims marked **[UNVERIFIED]** come from general knowledge of FFmpeg/mingw/CUDA and
> were NOT exercised in this repository; they must be confirmed by the PoC in section 7.
> Repo conventions (see `AGENTS.md`) apply: declarative registry, `ComponentStatus` values unchanged,
> `detail=` on every status transition, failures as `BuildError`, offline-first sources, `pytest tests/`,
> `black --check .`, `python scripts/check_mypy_baseline.py`, add a `docs/CHANGELOG.md` entry.

---

## 1. Task

Find and implement the best way to produce **Windows** FFmpeg builds from this build system.

Why: the current Windows path (`windows-msys2-ucrt64`, MSYS2 GCC) is considered not viable long term:
the full toolset and the full component set failed to build, and MSYS2 gives no control over compiler and
system-package versions. Using MSYS2 alone is therefore rejected as the primary route.

Hard requirements for the resulting `ffmpeg.exe`:

1. A **complete hwaccel set**: hardware decoders, hardware encoders (NVENC, QSV, AMF), and
   Vulkan, OpenCL and CUDA filters.
2. Support for **neural-network filters in the future** (backend choice still open, see 8).
3. Nvidia and Vulkan support at full scope. These were the original reason Windows builds were developed.
4. Build principle stays **"build for host on host"**: the Windows machine that runs WSL is both build
   host and target. No generic "build for someone else's machine" requirement.

## 2. Variants (in priority order)

| # | Variant | Verdict | Priority |
|---|---------|---------|----------|
| 1 | Build in Linux **WSL** using a **mingw-w64 cross toolchain** (target `x86_64-w64-mingw32`, UCRT) | Feasible, closest to current architecture, low risk. **Implement first.** | 1 |
| 2 | Native Windows build with the **MSVC** compiler. MSYS2 is used only as a Unix shell. | Feasible but a new backend and a reduced component set. Needed if MSVC-ABI, `nvcc` host builds or MSVC-only ML libraries (libtorch) are required. | 2 (separate project, after 1) |
| 3 | Native Windows build with **Intel oneAPI** (`icx`) | Not standalone: Windows `icx` is MSVC-ABI and needs Visual Studio Build Tools + Windows SDK, so it is Variant 2 plus an extra layer. No Linux->Windows ICX cross-compile exists (see `docs/intel-icx-build.md`, "Windows note"). | 3 (only if a measurable speed gain over mingw is shown) |

MSYS2-UCRT64 stays as a **deprecated** backend: do not extend it, do not delete it until Variant 1
reaches component parity.

### Variant summaries

**Variant 1 (WSL + mingw-w64).**
- Strengths: pinned toolchain versions are fully controllable (via a pinned `llvm-mingw` binary or a
  mingw-w64 chain built by the existing toolchain-store `gcc` recipe); all build tools (nasm, cmake,
  meson, ninja, pkg-config, perl, cargo) are native Linux, which removes the sh.exe/path pain of MSYS2;
  fast, reproducible; the built `ffmpeg.exe` can be run directly from WSL through interop.
- Risks: new build/host/target axis; every component must be cross-built; C++ ABI libraries that are
  only shipped as MSVC binaries (libtorch, OpenVINO C++) cannot be linked; CUDA filters need a PTX path
  that does not use Windows `nvcc` (section 4.3).

**Variant 2 (MSVC).**
- Strengths: MSVC ABI (`.lib`/`.pdb`), native debugging, Windows `nvcc` works, libtorch linkable.
- Risks: many autotools components do not build with MSVC (gmp, nettle, gnutls, fdk-aac, opencore,
  xvidcore, lame, libsdl, ...) and need replacements (e.g. schannel instead of gnutls); needs
  `lib.exe`/`link.exe`, hand-made `.pc` files, `c99wrap`; MSYS2 shell quirks remain; OpenMP is `vcomp`
  (OpenMP 2.0) or `clang-cl` + `libomp`. Start with a cut-down set: ffmpeg + x264/x265/dav1d/svtav1/
  opus/vpx/libwebp.

**Variant 3 (oneAPI).**
- Adds: Intel OpenMP (`libiomp5md`), experience from the Linux PoC (`scripts/icx_poc.py`,
  `patches/icx_compat.py`, x265 `-DINTEL_CXX=1`, `-ffp-model=precise`).
- Costs: everything from Variant 2, plus oneAPI release compatibility and redistribution of Intel runtime
  DLLs (`libiomp5md`, `libmmd`, `svml`).

## 3. Summary of the discussion and decisions

1. Keep **auto-detection in WSL** for host properties (tools, jobs, RAM). Linux builds are unchanged.
2. Hardware capabilities of the **Windows target** must **not** come from the WSL view of the system:
   - WSL hides or changes things: VAAPI/QSV disabled, OpenCL often absent, Vulkan usually only via Mesa
     `dzn`, CUDA libs live in `/usr/lib/wsl/lib`. For a Windows binary these are irrelevant or misleading.
   - NVENC/NVDEC/cuvid, QSV (`libvpl`), AMF, Vulkan, OpenCL are **loaded at runtime** on Windows
     (`nvcuda.dll`, `nvEncodeAPI64.dll`, `vulkan-1.dll`, `OpenCL.dll`, ...). Building needs only headers
     and import libraries, not the GPU stack.
3. Therefore: **hwaccel component set = defined by pinned SDKs/headers in the registry** (declarative),
   not by what hardware the build host exposes. Hardware probing is used only for recommendations,
   defaults, and validation.
4. Hardware probing for the Windows target is done **in real time from WSL through interop**
   (`powershell.exe -NoProfile`, `nvidia-smi.exe`) - no separate description file. WSL2 always runs on the
   same physical machine, so "build for host on host" is preserved and `-march=native` is valid.
5. CUDA filters: install the **CUDA Toolkit 12.4 (Linux, toolkit only, no driver)** in WSL and use
   `nvcc -ptx` with a Linux host compiler (`-ccbin gcc-12/13`). PTX is OS-independent and JIT-compiled by
   the Windows driver at runtime. The existing `nvcc_ccbin`/registry `cuda:` logic for gcc12/gcc13 is
   expected to be reusable. Fallback: `--enable-cuda-llvm`. **[UNVERIFIED in cross configuration]**
6. CUDA/gcc12/gcc13 version sets and Linux behavior are already verified by the maintainer on Linux.
7. Known remaining structural risk of mingw: **neural-network filters** (libtorch and OpenVINO C++ are
   MSVC-ABI only). C-API backends (TensorFlow C, OpenVINO C API, ONNX Runtime C API) are usable via import
   libraries; libplacebo/Vulkan shader upscaling needs no ML runtime. If libtorch is required, use Variant 2
   for that feature set.
8. Note: the maintainer said CUDA 12.x compile patches for gcc 12/13 exist in the build system. A search of
   `patches/` found no CUDA-specific patch (only `c23_fixes`, `cxx_headers`, `icx_compat`, `darwin_patches`,
   `ffmpeg_patches`); the relevant mechanism found is `PlatformDetector._detect_cuda_nvcc_support` +
   `nvcc_ccbin` + `toolchains.yaml` `cuda:` table. **Action: confirm where the patches live before reusing.**

## 4. Target architecture (Variant 1)

### 4.1 Build / host / target model

- build machine = WSL2 Ubuntu (26.04 LTS base). host = target = Windows 11 on the same physical machine.
- Build-time tools (nasm/yasm, cmake, meson, ninja, pkg-config, perl, cargo, nvcc) are **Linux-native**.
- Libraries and FFmpeg are produced for `x86_64-w64-mingw32` into a **target sysroot/prefix** that is
  separate from native Linux prefixes (never mix `PKG_CONFIG_PATH` of Linux and target prefixes; use
  `PKG_CONFIG_LIBDIR`/`PKG_CONFIG_SYSROOT_DIR`).
- New backend name: **`linux-cross-mingw`** (keep `linux-wsl2` and `windows-msys2-ucrt64` distinct; do not
  generalize their rules onto it). Suggested config: `target: windows`, `windows.backend: wsl-mingw`.

### 4.2 Code touch points (existing, verified by reading)

| Area | File / symbol | Change |
|------|---------------|--------|
| Backend id | `platform_detect.py` (backend name logic near `_check_wsl2`, lines ~272, ~660-671) | Add target selection; keep host detection untouched |
| Strategy | `platforms/windows_ucrt64.py`, `platforms/resolver.py` | Add `platforms/linux_cross_mingw.py`; resolver picks it for the new backend |
| Windows gating | `builder.py` `_is_windows_ucrt64_backend()` (many uses), `builders/base.py` (~189, ~245), `builders/ffmpeg/ffmpeg.py` (~55, ~114, ~125, ~175-188) | Replace with capability predicates (`targets_windows()`, `uses_msys_paths()`, `uses_posix_toolchain()`) so UCRT64-only behavior (MSYS path munging, `;` pkg-config, `sh.exe`) is not applied to cross builds |
| HW component set | `components.py` `WINDOWS_UCRT64_HW_ACCEL_COMPONENTS` (line ~16, used ~535) | Reuse/extend for the new backend (nv-codec, opencl-headers, opencl-icd-loader, onevpl; add amf, vulkan loader) |
| Toolchains | `toolchains.yaml` (`toolchains:`, `platform_profiles:`, `cuda:` at ~180), `toolchain_store.py`, `toolchain_builders.py` | Add mingw-w64 / llvm-mingw profile(s); keep `gcc12`/`gcc13` for `nvcc -ccbin` |
| FFmpeg configure | `builders/ffmpeg/ffmpeg.py` | Cross flags + hwaccel flags (4.4); enable nvcc block for the new backend |
| CUDA detect | `platform_detect.py` `_detect_cuda_nvcc_support` (~838), `nvcc_ccbin` | Detect Linux `nvcc`, target-driven `cuda_available` |
| Packaging | `release_bundle.py` | Windows bundle (static preferred; collect required mingw runtime DLLs) |
| Config | `config.py` (`windows:` section) | `target`, `windows.backend`, optional manual overrides (compute caps, march) |
| New | `windows_probe.py` (name suggested) | Interop probe, section 4.5 |

### 4.3 CUDA filters (nvcc path)

- Install toolkit only (WSL-Ubuntu variant; `cuda-toolkit-12-4` or the runfile with
  `--silent --toolkit --override`, never `cuda-drivers`).
- CUDA 12.4 supports host GCC <= 13; Ubuntu 26.04 defaults to a newer GCC. Use `-ccbin` with the
  store-provided `gcc13` (or `gcc12`). Possible glibc/`math.h` incompatibilities between CUDA 12.4 and the
  newer glibc **[UNVERIFIED]**; if they appear, try `-allow-unsupported-compiler`, an Ubuntu 22.04/24.04
  WSL distro used only as the "CUDA host" for PTX, or `--enable-cuda-llvm`.
- Existing logic to reuse: `--enable-cuda-nvcc`, `--nvccflags="-ccbin=... -gencode arch=compute_X,code=sm_X -O2"`,
  fallback to `--enable-cuda-llvm` (`builders/ffmpeg/ffmpeg.py` ~125-160).
- Compute capability must come from the **target GPU(s)** (probe, or `CUDA_COMPUTE_CAPABILITY` override), not
  from the WSL view. Verify `nvccflags` architectures are supported by CUDA 12.4 (e.g. TITAN V = sm_70).
- Driver compatibility: the Windows driver must support the PTX ISA generated by CUDA 12.4 (driver 550+
  expected). `--enable-cuda-nvcc` implies `--enable-nonfree`: the resulting binary is not redistributable.

### 4.4 FFmpeg configure (target)

Expected flags **[verify each in PoC]**:

```
--enable-cross-compile --target-os=mingw32 --arch=x86_64 --cross-prefix=<triplet>-
--pkg-config=<wrapper using PKG_CONFIG_LIBDIR of the target prefix> --enable-w32threads
--enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuvid
--enable-cuda-nvcc --nvccflags=... (fallback --enable-cuda-llvm)
--enable-libvpl --enable-amf
--enable-vulkan --enable-libglslang (or --enable-libshaderc) --enable-libplacebo
--enable-opencl
--enable-d3d11va --enable-dxva2 --enable-d3d12va   (check availability in pinned FFmpeg + mingw-w64 headers)
```

`d3d11va`/`dxva2`/`d3d12va` are not set anywhere in `builders/ffmpeg/ffmpeg.py` today; they are important
native-Windows decoders and need mingw-w64 headers new enough (D3D12 video).

### 4.5 Windows hardware probe (real time, via interop)

- A module (e.g. `windows_probe.py`) invoked once per build; do not read a state file.
- Use an explicit path: `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command ...`
  and `nvidia-smi.exe` (do not rely on `appendWindowsPath`). Emit JSON via `ConvertTo-Json`; parse JSON,
  not text.
- Collect: Windows version/build; CPU model + ISA; GPUs (vendor, model, driver version) via
  `Get-CimInstance Win32_VideoController`; NVIDIA `compute_cap`, driver version and max supported CUDA via
  `nvidia-smi.exe --query-gpu=name,compute_cap,driver_version --format=csv`; Vulkan ICD registry keys and
  API version; OpenCL ICD registry keys; Intel `libvpl`/runtime presence; `amfrt64.dll`; D3D12 feature level.
  Much of this already exists in `platform_detect.py` (`_detect_gpu_info_windows`, OpenCL/Vulkan paths,
  CUDA detection) and should be reused/ported rather than rewritten.
- Role: recommendations and validation only (gencode list, "driver too old for this PTX" warning,
  runtime acceptance tests). It must **not** decide which hwaccel components are built; that is the
  registry's job. Otherwise a build on a GPU-less box silently produces a reduced FFmpeg.
- Failure handling: if interop is disabled (`/etc/wsl.conf` `[interop] enabled=false`) or the probe fails,
  fail with a clear message or accept manual config overrides. Never silently fall back to WSL/Linux
  detection for target capabilities. Log the probe result to `workspace/logs/` (diagnostic only).
- Tests: mock `subprocess` with recorded output, like the existing WMIC/CIM tests.

### 4.6 Per-component cross-build notes **[UNVERIFIED, expected approach]**

| Build system | Approach |
|--------------|----------|
| autotools | `--host=x86_64-w64-mingw32`, `CC/CXX/AR/RANLIB/STRIP/WINDRES` from the cross toolchain |
| cmake | toolchain file: `CMAKE_SYSTEM_NAME=Windows`, `CMAKE_C_COMPILER=<triplet>-gcc`, `CMAKE_FIND_ROOT_PATH`, `CMAKE_FIND_ROOT_PATH_MODE_*`; host tools still run natively |
| meson | `--cross-file` (c/cpp/ar/strip/windres/pkg-config, `system=windows`, `cpu_family=x86_64`); keep the existing `-Denable_tests=false`/`-Denable_docs=false` rules (libvmaf, LV2 stack) |
| openssl | target `mingw64`, `--cross-compile-prefix=<triplet>-`; needs perl + nasm on host |
| libvpx | `--target=x86_64-win64-gcc` |
| x264 | `--host=x86_64-w64-mingw32 --cross-prefix=<triplet>-` |
| x265 | cmake toolchain file; keep existing assembler options |
| rav1e / cargo-c | Rust target `x86_64-pc-windows-gnu` (needs the target std installed in the store's Rust) and linker set to the cross gcc |
| glslang, libplacebo, dav1d, svtav1 | cmake/meson cross files |
| Vulkan Loader | **new component** (not in `components.yaml` today): build `vulkan-1` for mingw, may need patches |
| nv-codec, amf, vulkan-headers, opencl-headers, fast-float | headers only; gate via registry for the new backend (amf is Linux-only today) |
| opencl-icd-loader, onevpl (libvpl) | cmake cross builds; verify mingw support |
| gmp/nettle/gnutls, fdk_aac, etc. | autotools cross; expected to work but verify; consider `schannel`/openssl as TLS |

Prefer **static** codec libraries into `ffmpeg.exe` (as the current Windows build does) so the bundle
needs few runtime DLLs. OpenMP: libgomp (from the cross GCC) or `libomp` (llvm-mingw); verify the choice
works with the pinned toolchain.

## 5. Implementation recommendations

1. Work in vertical slices with decision gates (section 7); do not refactor everything first.
2. Make the target explicit in data: registry fields and config, not scattered `if backend == ...`.
   Introduce predicates on the builder/platform strategy and migrate `_is_windows_ucrt64_backend()` users
   incrementally while keeping the UCRT64 tests green.
3. Keep Linux behavior bit-for-bit unchanged (run the full existing test suite on every slice).
4. Toolchain source: start with **pinned `llvm-mingw` (UCRT) as a `binary` source** (fastest path, pinned
   by sha256 in `toolchains.yaml`, offline-first via `third_party/sources` + Git LFS). Add a from-source
   mingw-w64 chain (binutils + mingw-w64 headers/crt + gcc, reusing the `gcc` recipe) only if a GNU
   toolchain is required (e.g. libgomp) or llvm-mingw is insufficient.
5. Keep the probe small, mockable, and separate from build gating.
6. Add/extend unit tests with every slice; run `pytest tests/` (never bare `pytest`), `black --check .`,
   `python scripts/check_mypy_baseline.py`.
7. Update `docs/DeveloperReadme.md`, `README.md` and add a `docs/CHANGELOG.md` entry; mark the
   MSYS2-UCRT64 backend as deprecated in docs.

## 6. Preparation checklist (real system)

1. WSL2 Ubuntu 26.04; `/etc/wsl.conf` with interop enabled; verify `powershell.exe -NoProfile -Command "$PSVersionTable"`
   and `nvidia-smi.exe` run from WSL. Windows NVIDIA driver 550+ (or the version verified for CUDA 12.4 PTX).
2. `git lfs pull`; verify archives under `third_party/sources` are real tarballs (not LFS pointers).
3. Install CUDA Toolkit 12.4 (toolkit only) in WSL (section 4.3). Check:
   `nvcc --version`; `nvcc -ptx -ccbin <gcc13> test.cu -o test.ptx`.
4. Provision `gcc12`/`gcc13` through the toolchain store (already verified on Linux) for `-ccbin`.
5. Obtain and pin the mingw toolchain (llvm-mingw UCRT release, record version + sha256).
6. Host tools: nasm/yasm, cmake, meson, ninja, pkg-config, perl, Rust + target `x86_64-pc-windows-gnu`.
7. Optional: `wine` for runtime checks inside WSL (interop to run `.exe` directly on Windows is preferred).
8. Baseline: `pip install -e .`; `pytest tests/` green before starting.

## 7. Test and implementation plan

Each phase ends with a **gate**. Do not proceed if a gate fails; record the result in the changelog or
report and decide: fix, descope, or escalate to Variant 2.

### Phase 0 - Baseline and toolchain smoke (Gate G0)

- `pytest tests/`, `black --check .`, mypy baseline green.
- Manual smoke with the chosen mingw toolchain: compile a hello-world C and C++ to `.exe`, run it from WSL
  via interop; link a small program using `-fopenmp`; check `file`/`objdump -p` for UCRT imports.
- G0 pass: `.exe` runs on Windows, OpenMP works, toolchain pinned with sha256.

### Phase 1 - Toolchain registry and store (Gate G1)

- Add mingw profile(s) to `toolchains.yaml` (kind compiler, target windows, provides cc/cxx/ar/ranlib/
  windres/strip, `source` with `binary` default), and a `platform_profiles` entry for `(ffmpeg_version, windows-cross)`.
- Extend `toolchain_store.py`/`toolchain_registry.py` for a target triple; unit tests for registry parsing,
  materialization, checksum verification (offline).
- G1 pass: `prepare_toolchains.py` (or equivalent) provisions the toolchain offline from `third_party/sources`; tests added.

### Phase 2 - Backend and platform strategy (Gate G2)

- Add `linux-cross-mingw` backend (detect + config selection), `platforms/linux_cross_mingw.py`
  (env: cross `CC/CXX/AR/...`, target prefix, `PKG_CONFIG_LIBDIR`, flags, no MSYS path munging),
  resolver wiring, cmake toolchain file + meson cross file generators.
- Replace `_is_windows_ucrt64_backend()` uses with capability predicates, keeping UCRT64 behavior intact.
- Tests: resolver selection, env/flag generation, generated cross files, UCRT64 + Linux regressions unchanged.
- G2 pass: dry-run on the new backend produces correct env/configure lines for core components; full unit suite green.

### Phase 3 - Windows probe (Gate G3)

- Implement `windows_probe.py` (4.5), integrate into `PlatformInfo` for the target; config overrides.
- Tests: fixtures of recorded `powershell.exe`/`nvidia-smi.exe` output (NVIDIA, Intel Arc, AMD, no GPU,
  multi-GPU, interop disabled/timeout/invalid JSON).
- G3 pass: on the real machine the probe reports GPUs, `compute_cap`, driver version; failure modes tested.

### Phase 4 - Core stack cross-build PoC (Gate G4)

Build order (stop at first failure and fix before continuing): zlib -> x264 -> x265 -> dav1d -> svtav1 ->
opus/ogg/vorbis/lame -> libvpx -> libwebp/png/tiff -> freetype/zimg/soxr -> openssl (and/or gnutls) ->
rav1e/cargo-c -> libvmaf -> srt/zmq -> `ffmpeg` (software only first).

- Verify each artifact is a Windows PE (`file`), `.pc` files point to the target prefix, no host (Linux)
  libraries leak into the link.
- G4 pass: `ffmpeg.exe` software build runs on Windows: `ffmpeg.exe -version`, `-encoders`, an x264/x265/
  SVT-AV1/dav1d round-trip, `ffprobe.exe`.

### Phase 5 - Hardware acceleration matrix (Gate G5)

Add in this order (each as its own small slice with a test and a PoC run):

1. `nv-codec` headers: `--enable-ffnvcodec/nvenc/nvdec/cuvid`.
2. `opencl-headers` + `opencl-icd-loader`: `--enable-opencl`.
3. `onevpl`/`libvpl`: `--enable-libvpl`; `amf` headers: `--enable-amf` (registry change: amf is Linux-only today).
4. `d3d11va`, `dxva2`, `d3d12va` (check header/FFmpeg support).
5. `vulkan-headers` + new **Vulkan-Loader** component + `glslang`: `--enable-vulkan --enable-libglslang`; then `libplacebo`.
6. CUDA filters: nvcc path per 4.3 (`--enable-cuda-nvcc`), fallback `--enable-cuda-llvm`.

Static acceptance on the built binary (run from WSL via interop):

```
ffmpeg.exe -hide_banner -hwaccels                 # expect cuda, d3d11va, dxva2, (d3d12va), vulkan, opencl, qsv
ffmpeg.exe -hide_banner -encoders | grep -E "nvenc|qsv|amf"
ffmpeg.exe -hide_banner -decoders | grep -E "cuvid|qsv"
ffmpeg.exe -hide_banner -filters  | grep -E "cuda|vulkan|opencl|libplacebo"
```

Runtime acceptance (on the real GPUs; adapt per vendor present):

```
ffmpeg.exe -init_hw_device cuda=cu    -f lavfi -i testsrc2 -frames:v 60 -c:v h264_nvenc  out_nvenc.mp4
ffmpeg.exe -init_hw_device vulkan=vk  -f lavfi -i testsrc2 -frames:v 60 -vf "hwupload,scale_vulkan=w=1280:h=720,hwdownload,format=nv12" -f null -
ffmpeg.exe -init_hw_device opencl=ocl -f lavfi -i testsrc2 -frames:v 60 -vf "hwupload,unsharp_opencl,hwdownload,format=yuv420p" -f null -
ffmpeg.exe -hwaccel cuda -hwaccel_output_format cuda -i sample.mp4 -vf scale_cuda=1280:720 -c:v hevc_nvenc out_cuda.mp4
ffmpeg.exe -init_hw_device qsv=hw -f lavfi -i testsrc2 -frames:v 60 -c:v h264_qsv out_qsv.mp4     # if Intel GPU present
ffmpeg.exe -init_hw_device d3d11va -hwaccel d3d11va -i sample.mp4 -f null -
ffmpeg.exe -init_hw_device vulkan -i sample.mp4 -vf "libplacebo=w=1920:h=1080" -f null -
```

- G5 pass: every item present in the requirement list (decoders, hw encoders, Vulkan/OpenCL/CUDA filters)
  works at runtime on at least one real GPU of the matching vendor. Record gaps explicitly.
- If CUDA nvcc fails in cross config or Vulkan-Loader cannot be built for mingw, record the failure and
  evaluate: workaround, `cuda-llvm`, or escalate that feature to Variant 2.

### Phase 6 - Packaging (Gate G6)

- Windows bundle in `release_bundle.py`: prefer static; collect only required runtime DLLs; verify the
  bundle runs on a clean Windows user profile (no MSYS2/WSL in `PATH`); check DLL imports with
  `objdump -p`/`llvm-objdump`.
- Optional: generate MSVC-compatible import libs (`gendef` + `llvm-dlltool`) for shared builds.
- G6 pass: bundle is relocatable and self-contained on Windows.

### Phase 7 - Full set, regression, docs (Gate G7)

- Full component set cross-build; resume after interruption (`workspace/build_state.json`); log files in
  `workspace/logs/<component>_<step>.log`.
- Regression: Linux native, WSL2 native Linux, macOS paths unchanged; full unit suite, black, mypy baseline.
- Docs: `DeveloperReadme.md`, `README.md`, `CHANGELOG.md`; deprecation note for MSYS2-UCRT64.
- G7 pass: full stack builds from a clean workspace on the real machine; acceptance commands pass.

### Unit test inventory (to add)

- Registry/toolchain: mingw profile parsing, checksum verification, offline provisioning.
- Platform: resolver chooses `linux-cross-mingw`; env, cross-file and toolchain-file generation; no MSYS
  path munging in cross mode; Linux/UCRT64/macOS regressions.
- Components: gating of hw-accel components for the new backend (`nv-codec`, `amf`, `vulkan-loader`,
  `opencl-*`, `onevpl`); `ffmpeg.py` configure flags for cross (static assertions on the arg list).
- Probe: parsing and failure modes (see Phase 3).
- CUDA: nvcc path selected/fallback behavior in cross mode (mock `nvcc`).

## 8. Open questions and risks

| # | Item | Impact | Action |
|---|------|--------|--------|
| 1 | Neural-network filter backend not chosen | If libtorch is required, mingw cannot link MSVC C++ ABI binaries | Decide backend early; prefer C-API backends (TensorFlow C, OpenVINO C API, ONNX Runtime C API) or use Variant 2 for that part |
| 2 | CUDA 12.4 on Ubuntu 26.04 (glibc/GCC mismatch) | Toolkit/headers may fail to compile | Use gcc13 `-ccbin`; fallback: separate older-Ubuntu WSL as PTX host; or `cuda-llvm` |
| 3 | `nvcc` in cross `configure` (host compiler used for checks) | CUDA filters missing | PoC in Phase 5 |
| 4 | Vulkan-Loader for mingw | Vulkan hwaccel/filters/libplacebo | PoC; patches may be needed |
| 5 | D3D12VA needs recent mingw-w64 headers | Missing decoder | Check pinned toolchain version |
| 6 | Where are the "CUDA 12.x gcc 12/13 patches"? | Reuse assumption | Maintainer to point to location |
| 7 | OpenMP runtime choice (libgomp vs libomp) | Performance/packaging | Decide in Phase 0/4 |
| 8 | `--enable-cuda-nvcc` implies nonfree | Redistribution | Document; keep as user opt-in policy |
| 9 | Interop disabled/unavailable | Probe fails | Clear error + manual overrides |

Escalation rule: if G4 or G5 fails for a requirement that cannot be worked around within Variant 1, stop
and open the Variant 2 (MSVC) project for the affected features instead of widening Variant 1 with hacks.
