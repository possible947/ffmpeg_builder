# Windows FFmpeg build strategy: analysis and implementation plan (Variant 1: WSL + mingw-w64)

> Audience: an implementing agent/engineer working on a real system (Windows 11 + WSL2 Ubuntu).
> Status: design/plan document. The WSL-to-Windows cross backend and its gates are not implemented;
> the existing native backends and some reusable infrastructure already exist.
> Provenance: produced from a repository analysis plus a design discussion with the maintainer
> (@possible947). **[REQUIREMENT]** identifies a project invariant; **[REPO]** identifies a fact
> checked in the current repository or its bundled sources; **[UNVERIFIED]** identifies a technical
> hypothesis that requires a PoC. Maintainer-reported Linux results are identified separately and do
> not establish cross-build results.
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
   Vulkan, OpenCL and CUDA filters. Registry/configuration policy declares the required build
   capabilities; the absence of a particular GPU must not silently remove them.
2. Support for **neural-network filters in the future** (backend choice still open, see 8).
3. Nvidia and Vulkan support at full scope. These were the original reason Windows builds were developed.
4. **[REQUIREMENT] Build on host for host:** the physical Windows machine running WSL is the actual
   target. WSL is its controlled build environment. Optimize and specialize for this host; producing
   a generic binary for other machines or materially different host configurations is not a goal.
   Performance and efficiency take precedence over cross-host portability.
5. **[REQUIREMENT] Build identity includes relevant host and build state**, not just the FFmpeg
   version or target triple. A material hardware, driver, runtime, component, toolchain or
   configuration change can require a new build and rebuilding the dependency stack. Artifacts and
   completed/resume state from an incompatible identity must never be silently reused.

## 2. Variants (in priority order)

| # | Variant | Verdict | Priority |
|---|---------|---------|----------|
| 1 | Build in Linux **WSL** using a **mingw-w64 cross toolchain** (target `x86_64-w64-mingw32`, UCRT) | Closest to current architecture; **try first**, with early ABI/GPU PoCs. End-to-end feasibility remains unverified. | 1 |
| 2 | Native Windows build with the **MSVC** compiler. MSYS2 is used only as a Unix shell. | A possible new backend with a reduced component set **[UNVERIFIED]**; consider for a required MSVC-only feature only if reasonable interfaces/adapters cannot make Variant 1 work. | 2 (separate project, after 1) |
| 3 | Native Windows build with **Intel oneAPI** (`icx`) | Not standalone: Windows `icx` is MSVC-ABI and needs Visual Studio Build Tools + Windows SDK, so it is Variant 2 plus an extra layer. No Linux->Windows ICX cross-compile exists (see `docs/intel-icx-build.md`, "Windows note"). | 3 (only if a measurable speed gain over mingw is shown) |

MSYS2-UCRT64 stays as a **deprecated** backend: do not extend it, do not delete it until Variant 1
reaches component parity.

### Variant summaries

**Variant 1 (WSL + mingw-w64).**
- Strengths: pinned toolchain versions are fully controllable (via a pinned `llvm-mingw` binary or a
  mingw-w64 chain with the necessary store/recipe extensions); build tools (nasm, cmake, meson, ninja,
  pkg-config, perl, cargo) run natively in Linux, removing MSYS2 sh.exe/path pain. Inputs can be pinned
  for repeatable builds of the recorded host state; the resulting `ffmpeg.exe` runs on that Windows
  host through interop. Host-specific optimization is intentional.
- Risks **[UNVERIFIED]**: the build/host/target axis and each component's cross-build need validation;
  toolchain, C++ and Rust runtime ABIs must agree; MSVC-only C++ APIs may need another interface or
  backend; CUDA filters need a working PTX path that does not use Windows `nvcc` (section 4.3).
  Missing registry entries or build adapters are implementation work, not architectural failures.

**Variant 2 (MSVC) [UNVERIFIED in this repository].**
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

1. Keep **auto-detection in WSL** for build-environment properties (tools, jobs, RAM). Preserve native
   Linux behavior except for any explicitly tested identity-safety change required by the project rule.
2. Hardware capabilities of the **Windows target** must **not** come from the WSL view of the system:
   - WSL hides or changes things: VAAPI/QSV disabled, OpenCL often absent, Vulkan usually only via Mesa
     `dzn`, CUDA libs live in `/usr/lib/wsl/lib`. For a Windows binary these are irrelevant or misleading.
   - Target APIs depend on the appropriate Windows headers, libraries/import libraries and runtime
     drivers/loaders (`nvcuda.dll`, `nvEncodeAPI64.dll`, `vulkan-1.dll`, `OpenCL.dll`, ...).
     Whether each dependency is linked or loaded dynamically must be verified; they are not all
     interchangeable.
3. **[REQUIREMENT] Separate capability policy from specialization:** the declarative registry and
   configuration define which capabilities/components the build provides. The real Windows host
   determines how the current build is specialized and validated: CPU optimization, GPU compute
   capability, compatible SDK/header selection, explicit feature choices and runtime checks.
   Do not silently drop a required component because its GPU is absent; report an incompatible
   selected combination explicitly instead.
4. Hardware probing for the Windows target is done **in real time from WSL through interop**
   (`powershell.exe -NoProfile`, `nvidia-smi.exe`) at build/resume time. The same physical host is
   intentional: preserve host-specific optimization. **[UNVERIFIED]** Check that the selected compiler's
   native flags accurately reflect instructions usable by Windows; derive equivalent host-specific
   flags if needed, rather than weakening optimization for portability.
5. CUDA filters: a Linux toolkit-only CUDA installation and `nvcc -ptx` with a compatible Linux host
   compiler are the initial candidate; CUDA 12.4 with gcc12/gcc13 is one candidate combination, not
   an architectural requirement. The Windows driver executes the generated PTX. Existing
   `nvcc_ccbin`/registry logic can be extended; `--enable-cuda-llvm` is a possible fallback.
   **[UNVERIFIED in the WSL-to-Windows configuration]**
6. CUDA/gcc12/gcc13 combinations and Linux behavior were reported as verified by the maintainer on
   Linux. That does not verify the Windows cross-build or a different selected version combination.
7. Neural-network filters remain a structural risk where only an MSVC C++ ABI is available.
   C-API backends may be viable through appropriate import libraries; evaluate the chosen backend,
   FFmpeg version and available interface before escalating. **[REPO]** Bundled FFmpeg 8.1 supports
   OpenVINO, TensorFlow and Torch DNN backends; 9.0 also has ONNX Runtime. The actual Windows
   cross-link and execution remain **[UNVERIFIED]**. libplacebo/Vulkan shader upscaling needs no ML
   runtime.
8. Note: the maintainer said CUDA 12.x compile patches for gcc 12/13 exist in the build system. A search of
   `patches/` found no CUDA-specific patch (only `c23_fixes`, `cxx_headers`, `icx_compat`, `darwin_patches`,
   `ffmpeg_patches`); the relevant mechanism found is `PlatformDetector._detect_cuda_nvcc_support` +
   `nvcc_ccbin` + `toolchains.yaml` `cuda:` table. **Action: confirm where the patches live before reusing.**
9. **[REQUIREMENT]** Tool/component registries and the store are extensible infrastructure that
   declaratively provide the inputs needed by a supported build. Add a required tool, version,
   mapping, SDK, component or adapter when absent. Only a limitation that remains after reasonable
   support is added, such as an unavoidable ABI/platform incompatibility, is an architectural
   feasibility blocker.

## 4. Target architecture (Variant 1)

### 4.1 Build / host / target model

- The physical build host and actual target are the same Windows 11 machine; WSL2 Ubuntu (26.04 LTS
  base) is its controlled build environment. In cross-compilation terminology, the tools execute on
  WSL (`build`) and the produced programs execute on Windows (`host`/target).
- Build-time tools (nasm/yasm, cmake, meson, ninja, pkg-config, perl, cargo, nvcc) are **Linux-native**.
- Libraries and FFmpeg are produced for `x86_64-w64-mingw32` into a **target sysroot/prefix** that is
  separate from native Linux prefixes and from incompatible build identities. Never mix Linux and
  target `PKG_CONFIG_PATH`; use a target-only `PKG_CONFIG_LIBDIR` and a sysroot setting where
  appropriate, checking that staged absolute paths are not prefixed twice.
- New backend name: **`linux-cross-mingw`** (keep `linux-wsl2` and `windows-msys2-ucrt64` distinct; do not
  generalize their rules onto it). Suggested config: `target: windows`, `windows.backend: wsl-mingw`.
- The host's relevant hardware, Windows/driver/runtime state and all effective selected build inputs
  form the build identity. A materially different identity gets isolated state and artifacts and may
  require a full dependency rebuild (section 4.7).

### 4.2 Code touch points (existing, verified by reading)

| Area | File / symbol | Change |
|------|---------------|--------|
| Backend id | `platform_detect.py` (backend name logic near `_check_wsl2`, lines ~272, ~660-671) | Add target selection; keep host detection untouched |
| Strategy | `platforms/windows_ucrt64.py`, `platforms/resolver.py` | Add `platforms/linux_cross_mingw.py`; resolver picks it for the new backend |
| Windows gating | `builder.py` `_is_windows_ucrt64_backend()` (many uses), `builders/base.py` (~189, ~245), `builders/ffmpeg/ffmpeg.py` (~55, ~114, ~125, ~175-188) | Replace with capability predicates (`targets_windows()`, `uses_msys_paths()`, `uses_posix_toolchain()`) so UCRT64-only behavior (MSYS path munging, `;` pkg-config, `sh.exe`) is not applied to cross builds |
| HW component set | `components.py` `WINDOWS_UCRT64_HW_ACCEL_COMPONENTS`, `_is_eligible()`; `app.py` toolchain gate | Extend declarative target policy; do not silently omit required components for missing host GPUs or unprovisioned tools. Enable the existing AMF component for the target and add any actually required loader/import-library support. |
| Toolchains | `toolchains.yaml`, `toolchain_store.py`, `toolchain_builders.py`, `toolchain_registry.py` | Add the selected cross profile(s), host `nvcc` compiler mappings, SDKs and Rust target support; missing entries are implementation work. |
| FFmpeg configure | `builders/ffmpeg/ffmpeg.py` | Cross and version-specific hwaccel flags (4.4); select C++ runtime by toolchain; make NVCC/nonfree policy explicit. |
| CUDA detect | `platform_detect.py` `_detect_cuda_nvcc_support`, `nvcc_ccbin` | Separate Linux NVCC usability from Windows GPU/driver capabilities and requested build features. |
| Workspace/state | `config.py`, `app.py`, `state.py`, `builder.py` | Compare recorded/current build identity and isolate state, installed artifacts and mutable build trees before any reuse (4.7). |
| Packaging | `release_bundle.py` | Windows bundle for this host; collect required toolchain runtime DLLs and record external Windows/GPU runtime dependencies. |
| Config | `config.py` (`windows:` section) | `target`, `windows.backend`, feature choices and explicit overrides for host-specific parameters when needed. |
| New | `windows_probe.py` (name suggested) | Interop probe, section 4.5 |

### 4.3 CUDA/NVIDIA coherent configuration

- Select and record a compatible combination of FFmpeg, nv-codec headers, Windows driver/NVENC API,
  GPU compute capabilities, Linux CUDA toolkit and its host compiler, and the MinGW toolchain. These
  inputs participate in build identity; a relevant change may require a new probe/build/acceptance
  cycle. Do not select versions to keep an old binary working after an arbitrary host change.
- **[REPO]** `toolchains.yaml` currently maps CUDA 12.2/12.3 to gcc12 and 13.1 to gcc13; lookup is
  exact and there is no 12.4 mapping. Add the selected toolkit/compiler mapping and provisioning
  support when needed. Its absence is not evidence against Variant 1.
- **[REPO]** The bundled nv-codec 13.0.19.1 headers specify Windows driver 570+; the bundled
  12.2.72.0 headers specify Windows driver 551.76+. These are requirements of those specific
  header releases, not a universal driver threshold. The registry has an NVENC API-to-header map,
  but its current fallback uses the default when detection/mapping is missing. Probe the actual
  Windows NVENC API or use a verified explicit selection; extend the declared versions/mappings
  as needed and report incompatible combinations rather than silently assuming compatibility.
- **[UNVERIFIED]** CUDA 12.4 with a store-provided gcc12/gcc13 and toolkit-only installation is an
  initial PTX candidate; Ubuntu 26.04 host headers/glibc and the selected toolkit/compiler may be
  incompatible. Use a supported coherent host environment or test `--enable-cuda-llvm` if needed.
  `-allow-unsupported-compiler` only bypasses a compiler check; it is not a fix for incompatible
  headers or proof of correctness. Install no Linux GPU driver into WSL.
- Extend the existing `--enable-cuda-nvcc`/`--nvccflags` and `nvcc_ccbin` logic. Derive architecture
  flags from the actual selected Windows GPU(s), or an explicit configuration override; verify
  toolkit/PTX/driver compatibility and multi-GPU behavior. If no NVIDIA GPU is present but CUDA
  filters remain required, use an explicitly configured architecture/profile or report the missing
  build input; do not silently omit the component or claim runtime validation.
- **[REPO]** FFmpeg's `--enable-cuda-nvcc` requires an explicit `--enable-nonfree`; it does not turn
  that option on automatically. Make the choice explicit and document that FFmpeg labels such a
  build unredistributable. Evaluate other selected libraries separately; an LLVM fallback is not a
  blanket license conclusion.

### 4.4 FFmpeg configure (target)

Expected flags **[verify each in PoC]**:

```
--enable-cross-compile --target-os=mingw32 --arch=x86_64 --cross-prefix=<triplet>-
--pkg-config=<wrapper using PKG_CONFIG_LIBDIR of the target prefix> --enable-w32threads
--enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuvid
--enable-cuda-nvcc --enable-nonfree --nvccflags=...  (NVCC route)
--enable-libvpl --enable-amf
--enable-vulkan --enable-libplacebo
--enable-opencl
--enable-d3d11va --enable-dxva2 --enable-d3d12va   (check availability in pinned FFmpeg + mingw-w64 headers)
```

**[REPO]** The bundled FFmpeg 8.1 accepts `--enable-libglslang` or `--enable-libshaderc`; the bundled
9.0 configure script does not. Both have a `--glslc` path; select and validate the version-specific
shader compiler and libplacebo dependencies instead of applying 8.1 flags to 9.0.
`--enable-cuda-llvm` is the alternative CUDA-filter route; evaluate nonfree requirements for the
complete selected dependency set separately.
`d3d11va`/`dxva2`/`d3d12va` are not set anywhere in `builders/ffmpeg/ffmpeg.py` today; they are important
native-Windows decoders and need mingw-w64 headers new enough (D3D12 video).

### 4.5 Windows hardware probe (real time, via interop)

- A module (e.g. `windows_probe.py`) obtains fresh Windows host state at each build or resume. It must
  not use a previous state file as its current probe; the fresh relevant values are compared with the
  saved build identity and persisted with the build (4.7).
- Resolve explicit interop executable paths, e.g.
  `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command ...` and the
  actual `nvidia-smi.exe` path; do not rely on `appendWindowsPath`. Emit/parse structured JSON for
  PowerShell and parse the documented NVIDIA query format.
- Collect: Windows version/build; CPU model + ISA; GPUs (vendor, model, driver version) via
  `Get-CimInstance Win32_VideoController`; NVIDIA `compute_cap` and driver version via
  `nvidia-smi.exe --query-gpu=name,compute_cap,driver_version --format=csv`; obtain any required
  driver CUDA/PTX and NVENC API support separately; Vulkan ICD registry keys and
  API version; OpenCL ICD registry keys; Intel `libvpl`/runtime presence; `amfrt64.dll`; D3D12 feature level.
  Much of this already exists in `platform_detect.py` (`_detect_gpu_info_windows`, OpenCL/Vulkan paths,
  CUDA detection) and should be reused/ported rather than rewritten. **[REPO]** The existing NVENC
  API probe loads a native DLL in a Windows Python process; Linux Python in WSL cannot directly use
  that Windows DLL, so a Windows interop helper or another verified method is needed.
- Role: host-specific build parameters, compatible selected versions, identity, diagnostics and
  validation. The probe may affect *how* a required capability is built, but must not silently remove
  it from the registry/configuration-defined set when a GPU is absent.
- Failure handling: if interop is disabled (`/etc/wsl.conf` `[interop] enabled=false`) or the probe fails,
  fail with a clear message or accept explicit, sufficient manual overrides. Never silently fall back
  to WSL/Linux detection for target capabilities or treat unknown identity as a match. Log diagnostic
  results to `workspace/logs/`; the relevant identity is also persisted in build state.
- Tests: mock `subprocess` with recorded output, like the existing WMIC/CIM tests.

### 4.6 Per-component cross-build notes **[UNVERIFIED, expected approach]**

| Build system | Approach |
|--------------|----------|
| autotools | `--host=x86_64-w64-mingw32`, `CC/CXX/AR/RANLIB/STRIP/WINDRES` from the cross toolchain |
| cmake | toolchain file: `CMAKE_SYSTEM_NAME=Windows`, the selected triplet-prefixed GCC or Clang compiler, `CMAKE_FIND_ROOT_PATH`, `CMAKE_FIND_ROOT_PATH_MODE_*`; host tools still run natively |
| meson | `--cross-file` (c/cpp/ar/strip/windres/pkg-config, `system=windows`, `cpu_family=x86_64`); keep the existing `-Denable_tests=false`/`-Denable_docs=false` rules (libvmaf, LV2 stack) |
| openssl | target `mingw64`, `--cross-compile-prefix=<triplet>-`; needs perl + nasm on host |
| libvpx | `--target=x86_64-win64-gcc` |
| x264 | `--host=x86_64-w64-mingw32 --cross-prefix=<triplet>-` |
| x265 | cmake toolchain file; keep existing assembler options |
| rav1e / cargo-c | For LLVM-MinGW/UCRT, evaluate `x86_64-pc-windows-gnullvm`, its target std and LLVM linker; `x86_64-pc-windows-gnu` defaults to MSVCRT. Prove the selected Rust/C/C++ runtime and static-library combination in the early PoC. See the Rust [gnullvm](https://doc.rust-lang.org/rustc/platform-support/windows-gnullvm.html) and [gnu](https://doc.rust-lang.org/rustc/platform-support/windows-gnu.html) target documentation. |
| glslang, libplacebo, dav1d, svtav1 | cmake/meson cross files |
| Vulkan Loader | **[REPO]** Bundled FFmpeg 8.1/9.0 dynamically load `vulkan-1.dll` by default. Check libplacebo's target import-library needs separately; add SDK/import-library or loader build support if actually needed, not as a presumed prerequisite. |
| nv-codec, amf, vulkan-headers, opencl-headers, fast-float | headers only; gate via registry for the new backend (amf is Linux-only today) |
| opencl-icd-loader, onevpl (libvpl) | cmake cross builds; verify mingw support |
| gmp/nettle/gnutls, fdk_aac, etc. | autotools cross; expected to work but verify; consider `schannel`/openssl as TLS |

Prefer **static** codec libraries into `ffmpeg.exe` (as the current Windows build does) so the bundle
needs few runtime DLLs. OpenMP: libgomp (from the cross GCC) or `libomp` (llvm-mingw); verify the choice
works with the pinned toolchain and host. **[REPO]** `builders/ffmpeg/ffmpeg.py` currently adds
`-lstdc++` for some non-macOS C++ dependencies. LLVM-MinGW documents its different CRT and C++
standard library in its [known issues](https://github.com/mstorsjo/llvm-mingw#known-issues); select
the runtime by the actual toolchain, not just the operating system. **[REPO]** The current Cargo builder
installs `cargo-c` on demand if absent; stage it, the target Rust std and required crates for an
offline build instead of assuming they are already available.

### 4.7 Build identity, workspace and resume

**Current behavior [REPO]:**

- `config.py` chooses the default workspace by FFmpeg version (`workspace_81`, `workspace_90`); an
  app caller can instead supply a custom workspace. `app.py` puts `packages/` and
  `build_state.json` under that workspace.
- `BuildState` persists a generated `build_id`, start time, the config dictionary, component states
  (status, version, build time, error and log), and progress. In-progress statuses become `pending`
  on load. The UUID is not a comparison of the host or inputs.
- Resume starts at the first nonterminal status; `skipped` is terminal. `builder.py` skips a
  `completed` or `system` component when its name and version match. FFmpeg also derives built
  components from those statuses. The saved config is not compared with the current one before
  `app.py` overwrites it.
- A new build resets the state file but does not thereby isolate/remove the existing installed
  prefix and other workspace artifacts. Neither version-only workspaces nor a state reset prove
  that target artifacts belong to the current build identity.

**Smallest robust design [REQUIREMENT]:**

1. Capture the effective relevant state for the actual host and selected build: hardware/ISA and
   GPU capabilities, Windows/runtime/driver state, FFmpeg and component/SDK/header versions,
   compiler/toolchain and CUDA versions, target/ABI, build configuration, selected features and
   other inputs that affect the artifacts. Include relevant recipes/patches and resolved versions;
   distinguish material inputs from incidental diagnostics. Persist this identity alongside the
   existing state. Decide its representation and comparison details during implementation; no
   fingerprint format or hash algorithm is prescribed here.
2. Before overwriting saved config, selecting a resume point, trusting `completed`/`system` status
   or reusing installed artifacts, compare the freshly determined identity with the saved one.
   Matching identity may resume normally. A material mismatch starts a new identity and an isolated
   workspace/prefix and mutable build trees, or stops with a clear explanation until that is done.
   This also applies when a custom workspace is supplied and to a non-resume build; resetting a
   state file alone is insufficient. Recheck relevant host/runtime state before acceptance; if a
   material change is detected during a build or before validation, report it and do not attribute
   mixed-state artifacts or results to the same identity.
3. Missing, legacy, corrupt or unverifiable identity is not proof of compatibility. Do not silently
   reuse it: preserve existing data, explain the issue, and require a new isolated build or a
   migration that actually establishes compatibility. Apply the shared safety invariant to existing
   backends too; preserve their other behavior and test this intentional resume-safety change.
4. Share only appropriately verified immutable source/toolchain inputs across identities. Do not
   share target prefixes, mutable extracted/build trees, cached component completion or `system`
   decisions across incompatible identities. A materially changed host or dependency may require
   rebuilding the entire stack; this is expected behavior, not a portability defect.
5. Test same-identity resume and interruptions; changed CPU/GPU/driver, component/toolchain version,
   target and feature selection; state drift before acceptance; missing identity/probe data; legacy
   and custom workspaces; and stale installed files after a state reset. Verify incompatible
   artifacts are not reused.

## 5. Implementation recommendations

1. Run the high-risk ABI, NVIDIA and Vulkan PoC early, against one coherent configuration on the
   actual Windows host, before building the full component stack or refactoring all builders.
   Missing repository support can be added or staged for the PoC; it is not an architectural failure.
2. Make the target explicit in data: registry fields and config, not scattered `if backend == ...`.
   Introduce predicates on the builder/platform strategy and migrate `_is_windows_ucrt64_backend()` users
   incrementally while keeping the UCRT64 tests green.
3. Implement build-identity comparison and artifact isolation before enabling reusable cross-build
   state. Preserve native backends except for the explicit, tested resume-safety behavior in 4.7.
4. Start with **pinned `llvm-mingw` (UCRT) as a `binary` source**, provisioned offline-first via
   `third_party/sources` + Git LFS and verified by sha256. Add the necessary registry/store support,
   CUDA host compiler mappings, SDKs and Rust target artifacts. Add a from-source GNU mingw-w64
   chain if required; the current native `gcc` recipe alone is not yet a cross toolchain.
5. Keep the fresh Windows probe mockable. Use it for identity, specialization and validation, not
   silent exclusion of configured required capabilities.
6. Preserve offline-first behavior by staging the actual dependency closure, including host tools,
   target std, `cargo-c` and required crates. Test with networking disabled and empty external caches;
   a missing staged input is implementation/provisioning work.
7. Add/extend unit tests with every implementation slice; run `pytest tests/` (never bare `pytest`),
   `black --check .`, and `python scripts/check_mypy_baseline.py`.
8. Update `docs/DeveloperReadme.md`, `README.md` and add a `docs/CHANGELOG.md` entry; mark the
   MSYS2-UCRT64 backend as deprecated in docs.

## 6. Preparation checklist (real system)

1. WSL2 Ubuntu 26.04; enable interop and verify PowerShell from WSL. Record the actual Windows version,
   CPU/ISA, GPUs and relevant driver/runtime versions. Verify `nvidia-smi.exe` when NVIDIA hardware is
   present; validate the selected driver/header/toolkit combination rather than a universal minimum.
2. `git lfs pull`; verify archives under `third_party/sources` are real tarballs (not LFS pointers).
3. Stage the selected Linux CUDA toolkit-only package and compatible `-ccbin` (12.4 plus gcc13 is
   an initial candidate). Check `nvcc --version` and a small `nvcc -ptx -ccbin <compiler> test.cu`
   compile. Do not install a Linux GPU driver.
4. Obtain and pin the MinGW toolchain (initially LLVM-MinGW UCRT, with version and sha256). Stage
   missing profiles or binaries for the PoC; add registry/store support as implementation work.
5. Stage host tools (nasm/yasm, cmake, meson, ninja, pkg-config, perl), the selected Rust target std,
   `cargo-c` and required crates. Verify the selected C/C++/Rust runtimes and linker together.
6. Use Windows interop for actual-host runtime checks. Wine may assist software smoke tests but does
   not establish Windows GPU acceptance.
7. Baseline: `pip install -e .`; `pytest tests/` green before starting.

## 7. Test and implementation plan

Each phase ends with a **gate** and records the selected host/build identity, inputs, logs and
results. A PoC asks whether WSL + MinGW can produce the required capability for this host and
coherent configuration. A missing registry entry, builder, toolchain, SDK or adapter is a
**blocked implementation/provisioning task**: add reasonable support and retry, not an
architectural failure.

Record build support separately from runtime results. For each capability, runtime status is
`passed`, `failed`, or `not runtime-validated` with a reason (for example no matching GPU or the
present GPU lacks that feature). This is an acceptance report, not a change to `ComponentStatus`.
Absent hardware is not a runtime pass or an architectural failure. If applicable hardware is
present, failure to run an expected selected feature is a failure to investigate. A later material
host change legitimately starts a new probe/build/acceptance cycle.

### Phase 0 - Baseline and toolchain smoke (Gate G0)

- `pytest tests/`, `black --check .`, mypy baseline green.
- Record the current host state and an explicit, coherent initial FFmpeg/driver/SDK/toolchain
  combination. A manual or provisional probe is acceptable here; do not assume the eventual
  registry and backend are already implemented.
- Manual smoke with the chosen mingw toolchain: compile a hello-world C and C++ to `.exe`, run it from WSL
  via interop; link a small program using `-fopenmp`; check `file`/`objdump -p` for UCRT imports.
- G0 pass: the executable runs on the actual Windows host, OpenMP works, and the inputs/toolchain
  and relevant host state are recorded.

### Phase 0A - Early GPU and ABI feasibility (Gate G0A)

- In an isolated PoC prefix, link/run a representative C++ static library and a Rust static library
  with the chosen MinGW/UCRT toolchain and runtime. Exercise the selected OpenMP runtime. Validate
  host-specific CPU flags on the Windows host without weakening them for cross-host portability.
- Cross-build a minimal FFmpeg for the selected version with NVIDIA encode/decode, CUDA filters,
  Vulkan filters and OpenCL enabled as required. Exercise NVCC-generated PTX or the LLVM alternative,
  the target Windows loader/import path, and version-specific shader compilation. Add or stage
  missing prerequisites and adapters as needed; do not build the full codec stack first.
- Run the applicable NVIDIA, CUDA, Vulkan and OpenCL paths on the actual host; record unavailable
  hardware as not runtime-validated. Check version-specific configure differences for both supported
  FFmpeg versions, then extend the actual build matrix in later gates.
- G0A pass: the selected combination produces the required early-PoC build features, the C++/Rust
  ABI smoke tests pass, and all applicable real-host runtime tests pass. Missing hardware is reported
  separately, not counted as a pass. Keep PoC artifacts isolated from later build identities.

### Phase 1 - Windows probe and identity isolation (Gate G1)

- Implement the fresh Windows probe and explicit overrides (4.5); integrate the relevant values
  and resolved build inputs with build identity and workspace/resume isolation (4.7).
- Tests: NVIDIA, Intel, AMD, no GPU and multi-GPU fixtures; interop disabled/timeout/invalid JSON;
  same-identity resume, changed hardware/driver/toolchain/component/configuration, drift before
  acceptance, legacy or unknown identity, stale artifacts, `system` statuses and custom workspaces.
- G1 pass: the actual host is probed or supplied explicit sufficient overrides; compatible resume
  works and an incompatible or unknown identity cannot reuse prior completion state or artifacts.

### Phase 2 - Toolchain registry and store (Gate G2)

- Add the selected MinGW profile(s), target tools, FFmpeg/platform profiles, CUDA host-compiler
  mappings, SDK/header/import-library inputs and Rust target support to the registries/store.
- Test parsing, materialization, checksums and offline provisioning, including `cargo-c`, target std
  and the required crate sources. Extend existing recipes/store behavior where needed.
- G2 pass: the selected coherent toolchain and dependency inputs can be provisioned offline from
  staged sources in a clean environment; missing inputs produce explicit actionable failures.

### Phase 3 - Backend and platform strategy (Gate G3)

- Add `linux-cross-mingw` backend (detect + config selection), `platforms/linux_cross_mingw.py`
  (cross `CC/CXX/AR/...`, identity-specific target prefix, target pkg-config, no MSYS path munging),
  resolver wiring, cmake toolchain file + meson cross file generators.
- Replace `_is_windows_ucrt64_backend()` uses with capability predicates; separate host tools from
  target libraries and avoid silently skipping required components for missing host hardware/tools.
- Tests: resolver selection, env/flag generation, generated cross files, target/host isolation,
  explicit missing-support failures and native-backend regression/identity safety.
- G3 pass: dry-run lines and environments are correct for core components; existing behavior remains
  intact apart from the tested identity-safety change.

### Phase 4 - Core stack cross-build PoC (Gate G4)

Build order (stop at first failure and fix before continuing): zlib -> x264 -> x265 -> dav1d -> svtav1 ->
opus/ogg/vorbis/lame -> libvpx -> libwebp/png/tiff -> freetype/zimg/soxr -> openssl (and/or gnutls) ->
rav1e/cargo-c -> libvmaf -> srt/zmq -> `ffmpeg` (software only first).

- Verify executables/DLLs are Windows PE and static libraries contain Windows COFF objects; `.pc`
  files must point to the target prefix and no host (Linux) libraries may leak into the link.
- G4 pass: `ffmpeg.exe` software build runs on Windows: `ffmpeg.exe -version`, `-encoders`, an x264/x265/
  SVT-AV1/dav1d round-trip, `ffprobe.exe`; outputs remain isolated to the selected identity.

### Phase 5 - Hardware acceleration matrix (Gate G5)

Complete the required build features in small slices, extending the early PoC and provisioning
missing registry/build support as necessary:

1. `nv-codec` headers: `--enable-ffnvcodec/nvenc/nvdec/cuvid`.
2. `opencl-headers` + `opencl-icd-loader`: `--enable-opencl`.
3. `onevpl`/`libvpl`: `--enable-libvpl`; `amf` headers: `--enable-amf` (registry change: amf is Linux-only today).
4. `d3d11va`, `dxva2`, `d3d12va` (check header/FFmpeg support).
5. `vulkan-headers` and the selected version's shader tooling: `--enable-vulkan`; then `libplacebo`.
   Add a target import library or loader component only if the selected combination needs it.
6. CUDA filters: nvcc path per 4.3 (`--enable-cuda-nvcc`), fallback `--enable-cuda-llvm`.

Resolve an explicit expected feature matrix from registry/configuration for the selected FFmpeg
version, including the required encoders, decoders, accelerators, filters and relevant
codecs/profiles. Define it independently of which GPUs are currently present. Assert each required
item's build presence **individually**; an OR-pattern match for one item does not establish the
others. Examples on the built binary (run from WSL via interop):

```
ffmpeg.exe -hide_banner -hwaccels
ffmpeg.exe -hide_banner -encoders | grep -w h264_nvenc
ffmpeg.exe -hide_banner -encoders | grep -w h264_qsv
ffmpeg.exe -hide_banner -encoders | grep -w h264_amf
ffmpeg.exe -hide_banner -decoders | grep -w h264_cuvid
ffmpeg.exe -hide_banner -decoders | grep -w h264_qsv
ffmpeg.exe -hide_banner -filters  | grep -w scale_cuda
ffmpeg.exe -hide_banner -filters  | grep -w scale_vulkan
ffmpeg.exe -hide_banner -filters  | grep -w unsharp_opencl
ffmpeg.exe -hide_banner -filters  | grep -w libplacebo
```

Check each expected `-hwaccels` entry individually as well. Expand the assertions to the complete
registry/configuration-defined set and selected FFmpeg version; these examples alone are not the
full matrix.

Runtime acceptance is **host-specific**: for each actual GPU and feature it supports, select the
device explicitly, use a suitable generated/provided input and verify the resulting output and logs
show hardware execution rather than a software fallback. Illustrative commands, to adapt and verify
on the real host, include:

```
ffmpeg.exe -init_hw_device cuda=cu    -f lavfi -i testsrc2 -frames:v 60 -c:v h264_nvenc  out_nvenc.mp4
ffmpeg.exe -init_hw_device vulkan=vk  -f lavfi -i testsrc2 -frames:v 60 -vf "hwupload,scale_vulkan=w=1280:h=720,hwdownload,format=nv12" -f null -
ffmpeg.exe -init_hw_device opencl=ocl -f lavfi -i testsrc2 -frames:v 60 -vf "hwupload,unsharp_opencl,hwdownload,format=yuv420p" -f null -
ffmpeg.exe -hwaccel cuda -hwaccel_output_format cuda -i sample.mp4 -vf scale_cuda=1280:720 -c:v hevc_nvenc out_cuda.mp4
ffmpeg.exe -init_hw_device qsv=hw -f lavfi -i testsrc2 -frames:v 60 -c:v h264_qsv out_qsv.mp4     # if Intel GPU present
ffmpeg.exe -f lavfi -i testsrc2 -frames:v 60 -c:v h264_amf out_amf.mp4                            # if supported AMD GPU present
ffmpeg.exe -init_hw_device d3d11va -hwaccel d3d11va -i sample.mp4 -f null -
ffmpeg.exe -hwaccel d3d12va -i sample.mp4 -f null -                                               # if host supports D3D12VA
ffmpeg.exe -init_hw_device vulkan -i sample.mp4 -vf "libplacebo=w=1920:h=1080" -f null -
```

- Report build support and runtime status separately for NVENC/NVDEC/CUVID, QSV, AMF, D3D11VA,
  DXVA2, D3D12VA, Vulkan, OpenCL, CUDA and libplacebo as applicable, including selected
  codecs/profiles, GPU and driver. No matching GPU or hardware feature means **not
  runtime-validated**, never passed. Do not require hardware absent from this host merely to pass
  the host-applicable runtime gate.
- G5 pass: every required configured capability is present in the build, and all runtime tests
  applicable to this host pass. The report retains explicit unvalidated entries for absent or
  unsupported hardware; no claim is made about a materially changed host.
- If an applicable feature fails, add reasonable support and test alternatives such as the LLVM CUDA
  path or an appropriate Vulkan import library. Escalate only if an actual architectural limitation
  remains after that work, not merely because a loader/tool/profile is currently missing.

### Phase 6 - Packaging (Gate G6)

- Windows bundle in `release_bundle.py`: prefer static codec libraries where appropriate; collect
  required toolchain runtime DLLs and record the actual host's required Windows/GPU drivers and
  loaders. Verify on this Windows host with a controlled PATH that does not depend on MSYS2/WSL.
  Inspect imports with `objdump -p`/`llvm-objdump` and run the applicable features: libraries loaded
  dynamically may not appear in the import table.
- Optional: generate MSVC-compatible import libs (`gendef` + `llvm-dlltool`) for shared builds.
- G6 pass: the bundle runs from its intended location on the same host with its recorded runtime
  dependencies. No clean-machine or cross-host portability requirement is implied.

### Phase 7 - Full set, regression, docs (Gate G7)

- Full component set cross-build in the identity-specific workspace; resume after interruption
  (`build_state.json`); log files under that workspace's `logs/<component>_<step>.log`.
- Regression: Linux native, WSL2 native Linux, macOS and UCRT64 paths preserve intended behavior;
  test the explicit identity-safety change, full unit suite, black and mypy baseline.
- Docs: `DeveloperReadme.md`, `README.md`, `CHANGELOG.md`; deprecation note for MSYS2-UCRT64.
- G7 pass: full stack builds from an isolated workspace for the recorded real-host identity;
  configured build features and applicable host acceptance pass, with unavailable hardware recorded
  as not runtime-validated. A material state change starts a new build/acceptance cycle.

### Unit test inventory (to add)

- Registry/toolchain: mingw profile parsing, CUDA/version mappings, checksum verification and
  complete offline provisioning including Rust target std, `cargo-c` and crates.
- Platform: resolver chooses `linux-cross-mingw`; env, cross-file and toolchain-file generation; no MSYS
  path munging in cross mode; Linux/UCRT64/macOS regressions.
- Identity/workspace: same identity resumes; changed relevant state, unknown/legacy identity and
  custom workspace cannot reuse stale completion, system decisions, prefixes or build trees.
- Components: required hwaccel components remain selected without their hardware; missing tools
  produce an explicit provisioning failure; version-specific FFmpeg flags and independent
  capability assertions.
- Probe: parsing, fresh state, explicit overrides and failure modes (see Phase 1).
- CUDA: selected coherent header/driver/toolkit/compiler/GPU combinations, NVCC/LLVM path and
  explicit nonfree choice; mock probes without assuming absent mappings are architectural failures.

## 8. Open questions and risks

| # | Item | Impact | Action |
|---|------|--------|--------|
| 1 | Neural-network backend not chosen | An MSVC-only C++ API may have no directly compatible MinGW ABI | Select the actual FFmpeg/backend combination; investigate supported C APIs or reasonable wrappers before considering Variant 2. |
| 2 | LLVM-MinGW/UCRT, C++ and Rust runtime compatibility | Cross-linked static dependencies may fail or use incompatible runtimes | Test `windows-gnullvm`, toolchain-selected C++ runtime, OpenMP and representative libraries in G0A. |
| 3 | CUDA toolkit, host compiler, Windows driver, headers and GPU combination | NVCC/PTX or NVENC may not work for the selected host | Probe/select a coherent combination and test in G0A; add missing mappings/versions and rebuild after relevant changes. |
| 4 | Vulkan loading and shader compilation | FFmpeg/libplacebo may have different link and compiler needs | Test actual host loader, import libraries and version-specific shader paths in G0A; add required support rather than assuming a loader build is mandatory. |
| 5 | D3D12VA headers and runtime | Missing configured decoder or unvalidated host support | Add a suitable SDK/toolchain profile; test build support and runtime only where hardware supports it. |
| 6 | Where are the "CUDA 12.x gcc 12/13 patches"? | Reuse assumption | Maintainer to point to location; missing patches/mappings alone are implementation work. |
| 7 | Host CPU optimization from WSL | Selected compiler's native flags may not reflect Windows-usable ISA | Verify actual-host execution; derive host-specific flags if needed, without optimizing for portability. |
| 8 | Identity and legacy workspaces | Stale state or installed files could cross build identities | Implement the guard/isolation in G1; block unsafe reuse with an explicit explanation. |
| 9 | Offline Rust/tool/SDK inputs | On-demand Cargo downloads or missing target std block offline builds | Stage the selected dependency closure and add store support; verify G2 with networking disabled. |
| 10 | NVCC nonfree policy | NVCC requires explicit nonfree enablement and FFmpeg labels the result unredistributable | Make it a deliberate configuration choice; evaluate the complete selected dependency set. |
| 11 | Interop disabled/unavailable | Target state is unknown | Fail clearly or accept explicit sufficient overrides; do not silently reuse unknown identity. |

Escalation rule: investigate and add reasonable registry/store, toolchain, SDK, builder, probe and
adapter support first. Escalate an affected feature to Variant 2 only when a fundamental ABI/platform
limitation remains for the selected host/configuration. Missing hardware is reported as not
runtime-validated; it is not proof of architectural failure or a requirement to support a future
hardware change without rebuilding.
