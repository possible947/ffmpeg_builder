# Windows FFmpeg cross-build strategy

**Status:** implementation plan for G1-G7. G0/G0A feasibility is complete for
the scoped host and early-PoC feature set. This consolidation does not start G1
or any later implementation gate. The production `linux-cross-mingw` backend
and full component stack are not implemented.

This plan distinguishes **verified results**, **project requirements**,
**repository facts**, **unverified items**, and **implementation work**.
Verification is scoped to the exact host/configuration recorded below; it is
not a portability claim. Status labels are `[VERIFIED <gate>]`,
`[REQUIREMENT]`, `[REPO]`, `[UNVERIFIED]`, and `[IMPLEMENTATION]`.

## 1. Decision register

| Decision | Status and evidence | Implementation consequence | Caveat |
|---|---|---|---|
| Variant 1, WSL Linux to Windows with LLVM-MinGW/UCRT, is selected for implementation. | [VERIFIED G0-G0A.5] Each early-PoC gate completed on the host below; FFmpeg 9.0 integrated the required early NVIDIA/CUDA/Vulkan/OpenCL paths. | Implement `linux-cross-mingw`; do not rerun feasibility per component. | Full dependency stack, hardware matrix, and package are not complete. |
| The physical Windows machine running WSL is the target; WSL is the controlled build environment. | [REQUIREMENT] Maintainer decision; [VERIFIED G0] Windows interop executed the PoCs. | Probe Windows target state separately from Linux build-host tools. Optimize for this host, not arbitrary Windows machines. | A material host change can require rebuild and new acceptance. |
| LLVM-MinGW UCRT is the initial cross toolchain. | [VERIFIED G0-G0A.5] LLVM-MinGW 20261104, Clang/LLD 23.1.5, Windows PE execution and actual UCRT loading. | Pin and validate toolchain identity, target triple, and runtime. | Does not establish compatibility with every library or toolchain revision. |
| C++ and Rust communicate through a C ABI. | [VERIFIED G0A.1] The mixed C/C++/Rust tests passed in both linking directions and in one executable. | Use fixed-layout C interfaces and caller-owned buffers; contain exceptions and panics. | No arbitrary C++/Rust ABI or cross-boundary unwinding is supported. |
| Rust uses `x86_64-pc-windows-gnullvm` with the matching target standard library. | [VERIFIED G0A.1] Rust 1.99.0 target-std was installed to an isolated PoC sysroot and linked with LLVM-MinGW. | Provision and version the target std alongside its compiler. | PoC provisioning is not the production store implementation. |
| CPU specialization comes from effective Windows-host capability. | [VERIFIED G0A.2/G0A.5] Windows CPUID/OS-state data and compiler resolution were compared; specialized executables ran on this host. | Derive or validate target flags from fresh Windows capabilities and selected compiler. | Do not hard-code the observed CPU flag string or assume a model name determines effective ISA. |
| CUDA uses Linux NVCC to emit PTX for the Windows NVIDIA driver. | [VERIFIED G0A.3/G0A.5] CUDA 12.4.99 generated compute_70 PTX; Windows driver JIT/kernel and FFmpeg CUDA-filter execution passed. | Select NVCC, host compiler (`-ccbin`), and target architecture explicitly. | Tested on TITAN V (CC 7.0), one driver/toolkit combination. |
| CUDA host compiler selection is explicit. | [VERIFIED G0A.3/G0A.5] Project/store GCC 12.5.0 was passed as `-ccbin`; ambient GCC 15 was not used. | Version, validate, configure, and include effective `-ccbin` in identity. | Do not switch system GCC or use global alternatives. |
| CUDA and Vulkan SDKs are prepared WSL prerequisites under `/opt`. | [MAINTAINER DECISION; current paths/version checked] CUDA `/opt/cuda-12.4`; Vulkan root `/opt/vulkan-sdk/1.4.363.1`, effective Linux root `/opt/vulkan-sdk/1.4.363.1/x86_64`. | Resolve configured paths, validate versions/tools, and fail clearly if invalid. | These are not target-prefix contents or automatically project-managed components. |
| The Windows Vulkan runtime uses the system loader and vendor ICD. | [VERIFIED G0A.4/G0A.5] Windows `vulkan-1.dll` discovered the NVIDIA ICD/device and ran compute and FFmpeg filter work. | Do not build or bundle a Vulkan loader by default; isolate target discovery. | Future dependencies may require an import interface. |
| The Windows OpenCL runtime uses the system dispatcher and vendor platforms. | [VERIFIED G0A.4/G0A.5] Windows `OpenCL.dll` exposed two platforms; the NVIDIA TITAN V program and FFmpeg filter dispatched successfully. | Do not treat the dispatcher as an implementation or bundle a separate ICD loader by default. | Probe/select a platform and device explicitly; platform inventory is host-specific. |
| Static C++ dependencies must carry their runtime requirements through metadata. | [VERIFIED G0A.5 Phase B] Static libplacebo objects needed libc++/libunwind flags at FFmpeg's final C-link invocation; the PoC repaired isolated pkg-config metadata. | Production dependency metadata/link composition must express those requirements transitively. | Do not copy the PoC metadata edit as a production recipe. |
| Target-scoped discovery is mandatory. | [REQUIREMENT; verified in PoC isolation] Target pkg-config/CMake/Meson lookup must not consume Linux SDK/library paths. | Build isolated cross files and environment; keep host tools explicit. | PoC paths and temporary metadata are not production inputs. |
| Build identity protects artifacts and resume state. | [REQUIREMENT; repository behavior is insufficient today] `build_id` is a UUID, not a host/input comparison; see §6. | G1 must compare identity before resume or artifact reuse. | Decide representation during G1; this plan does not prescribe a hash format. |
| Capability policy is separate from present hardware. | [REQUIREMENT] Config/registry define required build capabilities; host inventory specializes and validates. | Missing host hardware cannot silently remove required build features. | Runtime acceptance is only required for host-present/supported hardware. |
| PoC outputs are evidence only. | [REQUIREMENT] G0/G0A/E0 outputs are outside production workspace/store. | Production tests must reproduce behavior independently. | Never silently import PoC binaries, `.pc` files, import libraries, or DLL copies. |

## 2. Scope, host, and verified outcome

### 2.1 Tested configuration

The results below apply to this one configuration, not to other Windows PCs:

- Physical target: Windows 11 Pro for Workstations 25H2, build 10.0.26200.9711.
- WSL build host: Ubuntu 26.04.1 LTS, WSL 3.0.3.0, WSL2 kernel 6.18.54.1-1.
- CPU: Intel Core i9-7980XE. Windows reported AVX2 and AVX-512 feature/state
  support; G0A.2 separately compared actual Windows effective features with
  LLVM-MinGW compiler target resolution.
- GPU: NVIDIA TITAN V, compute capability 7.0, NVIDIA Windows driver 581.57.
- LLVM-MinGW: 20261104 UCRT archive, SHA-256
  `919380403de87dca3c581c2053230cd298af5e891286628ba0613b7fed370926`;
  Clang/Clang++ and LLD 23.1.5, target `x86_64-w64-windows-gnu`.
- C++ runtime: LLVM libc++ and libunwind. The test processes loaded the
  selected DLLs from their isolated PoC directories. Windows loaded
  `C:\WINDOWS\System32\ucrtbase.dll`.
- Rust: 1.99.0 with `x86_64-pc-windows-gnullvm` target std provisioned into an
  isolated PoC sysroot.
- FFmpeg: source 9.0, archive SHA-256
  `d97647ace36a307f17ba2bca052d68937487bed8682e1eb9b6737076a9c442b7`.
- CUDA: Linux NVCC 12.4.99 and project/store GCC 12.5.0 as `-ccbin`;
  compute_70 PTX. No Linux NVIDIA driver was installed or used.
- Vulkan/OpenCL: tested through the installed Windows runtime and NVIDIA
  driver; separate WSL SDK provisioning is specified in §4.

The CPU, GPU, OS build, loader versions, and exact tool versions above are
observations, not defaults for other hosts. See §5 for what belongs in build
identity versus diagnostics.

### 2.2 Gate results

| Gate | Result actually established | Evidence |
|---|---|---|
| G0 | LLVM-MinGW built Windows PE C and C++ programs, OpenMP program, and all ran through Windows interop. The processes loaded UCRT; libc++/libunwind and libomp DLL paths were observed. OpenMP reported four workers. | `files/g0/G0-report.md`, final G0 addendum and logs. |
| G0A.1 | C-compatible boundary passed with C, LLVM-MinGW C++ static library, and Rust static library. Both mixed link directions and combined program ran on Windows. C++ exceptions and Rust panic were caught inside their respective exported functions and returned as status; no unwind crossed the C ABI. | `files/g0a1/G0A1-report.md`, `files/g0a1/ABI.md`, logs. |
| G0A.2 | On this host/toolchain, `-march=native` resolution and tested explicit specialization resolved to usable host features; Windows CPUID and OS-enabled vector state were inspected. Generic/model-only and explicit/native cases were compared. | `files/g0a2/logs/compiler-native-resolution.txt`, `explicit-host-resolution.txt`, `windows-cpuid-runtime.txt`, build/disassembly logs. |
| G0A.3 | Linux NVCC generated compute_70 PTX with selected GCC 12.5 `-ccbin`; a Windows PE harness loaded the Windows CUDA Driver API, JIT-compiled PTX, dispatched a kernel, validated output, and exited successfully on TITAN V. A separate Windows codec probe exercised NVIDIA runtime APIs. | `files/g0a3/logs/nvcc-ptx-build-no-gnu-source.txt`, `cuda-driver-harness-runtime.txt`, `nvidia-codec-probe-runtime.txt`, PTX hash. The companion `nvcc-ptx-build.txt` captures the declaration-conflict failure without `-U_GNU_SOURCE`. |
| G0A.4 | Windows PE Vulkan and OpenCL programs ran compute work on the NVIDIA TITAN V through installed Windows loaders/dispatcher and vendor driver. Shader compile/SPIR-V validation and Windows runtime discovery were recorded. | `files/g0a4/logs/vulkan-runtime.txt`, `opencl-runtime.txt`, imports and Windows inventory. |
| G0A.5 core | Actual FFmpeg 9.0 Windows binaries configured and built with NVENC, NVDEC/CUVID, CUDA NVCC filters, Vulkan filters, and OpenCL filters. On the physical host, NVENC encoded, CUVID/NVDEC decoded, and CUDA, Vulkan, and OpenCL filters ran. `ffmpeg.exe`/`ffprobe.exe` executed via interop. Inspected final executables are PE32+ and their imports contain Windows DLLs (including UCRT API sets and `OpenCL.dll`), with no Linux `.so` import. | `files/g0a5/logs/configured-feature-summary.txt`, `final-host-feature-lists.txt`, `nvenc-encode-final.log`, `nvdec-decode-nv12.log`, `cuda-filter-yuv420p.log`, `vulkan-filter.log`, `opencl-filter.log`; final PoC binaries inspected read-only. |
| G0A.5 Phase B | A static libplacebo/glslang FFmpeg 9.0 variant was linked and run; `libplacebo` initialized the TITAN V Vulkan device and processed frames. Its static C++ runtime link requirements were identified. | `files/g0a5/logs/libplacebo-phase-b-build-retry.log`, `libplacebo-filter-vulkan.log`, host-run-libplacebo artifacts and configure/build logs. |

### 2.3 What these results do not prove

- They do not prove arbitrary C++/Rust ABI compatibility, cross-boundary
  exception/panic unwinding, all compiler/runtime combinations, or portable
  CPU targeting.
- They do not prove the full configured software component stack, every
  hardware backend, clean-machine package behavior, or offline provisioning
  closure.
- They do not establish QSV, AMF, D3D11VA, DXVA2, D3D12VA, neural-network
  backends, or AV1 hardware decode on this host. For these, record build
  presence, runtime acceptance, and unavailable/unsupported hardware
  separately; do not infer success from the early PoCs.
- G0A.5 is an early feature-slice build, not a production or full-feature
  FFmpeg distribution. Some tests used deliberately small generated media.
- The final PoC PE import inspection did not show Linux `.so` dependencies;
  this is not a full-prefix scan or proof that dynamically loaded runtime
  modules are discoverable from the import table.
- A loader/runtime route proven for these FFmpeg dependencies does not prove
  that every future dependency has the same import or dynamic-loading needs.
- No result implies that success on this host transfers to a different
  Windows host or a materially changed driver/runtime.

## 3. Variants and project requirements

### 3.1 Variant decision

**Variant 1 (selected):** WSL Linux build tools + LLVM-MinGW cross toolchain,
Windows UCRT target, physical Windows host as runtime target. Its early
feasibility is established for §2.1. The remaining work is production
implementation, full-stack integration, hardware-matrix validation, identity
safety, and packaging—not a repeat of the early ABI/GPU feasibility exercise.

**Variant 2 (retained):** native Windows/MSVC. It is not selected. Revisit only
if a real required dependency exposes an unavoidable MSVC-only ABI/platform
surface that cannot be handled through a supported C ABI, import interface, or
reasonable adapter. Do not switch because a registry entry, toolchain recipe,
or component adapter has not yet been implemented.

**Variant 3 (retained):** native Windows oneAPI/ICX. It adds the MSVC ABI and
Visual Studio/Windows SDK dependency. Revisit only for a measured requirement,
not as a presumed way to complete Variant 1.

### 3.2 Requirements

- Registry/configuration declares the required feature set. Missing hardware
  must not silently remove configured capabilities.
- Probe actual Windows state for target specialization and runtime acceptance;
  do not infer it from WSL's Linux view.
- A changed material host or effective build input must not reuse incompatible
  completion state, mutable build trees, target prefixes, or installed
  artifacts.
- Keep project-managed source/toolchain provisioning offline-first. SDK
  prerequisites owned by the WSL environment are validated, not implicitly
  installed or modified.
- Keep `windows-msys2-ucrt64` and `linux-wsl2` distinct from the new backend.
  Preserve current backend behavior except for intentional, tested identity
  safety changes.
- Keep MSYS2-UCRT64 deprecated: do not extend it or remove it before Variant 1
  reaches component parity; keep its documentation status consistent.
- Preserve `ComponentStatus` values, pass `detail=` for transitions, and
  report failures using repository `BuildError` conventions.

### 3.3 Repository facts to account for [REPO]

The repository currently has native Linux/WSL and MSYS2-UCRT64 strategies, not
`linux-cross-mingw`. `builder.py` has UCRT64-specific branches; do not reuse
MSYS path rewriting in the Linux cross backend. `state.py` persists a UUID
`build_id`, configuration, and component statuses; it does not compare a fresh
Windows/build identity before reuse. Workspaces are version-oriented, and a
state reset alone does not isolate an existing prefix.

The existing CUDA detector/store contains `nvcc_ccbin` and version mappings,
but the selected CUDA 12.4 / GCC 12.5 combination and Windows target behavior
need explicit backend support. Existing FFmpeg configuration is not the
production cross-build implementation. Extend declarative component and
toolchain metadata rather than scattering component-specific policy.

## 4. Build-host and target ownership

### Layer A — prepared WSL build-host environment

Linux-native tools and prerequisites: Ubuntu 26.04.1 WSL; CMake, Meson, Ninja,
pkg-config, NASM/Yasm, Perl/Python and other explicitly selected host tools;
CUDA SDK `/opt/cuda-12.4`; Vulkan SDK root
`/opt/vulkan-sdk/1.4.363.1`, effective Linux SDK root
`/opt/vulkan-sdk/1.4.363.1/x86_64`. The selected environment currently reports
NVCC 12.4.99, GCC 12.5.0 from the project store, glslang 16.6.0 and
SPIRV-Tools 2026.4. The ambient Ubuntu compiler is GCC 15; it is not the
CUDA 12.4 host compiler.

CUDA/Vulkan SDKs are versioned **WSL prerequisites**, not Windows target-prefix
contents, and not automatically provisioned project-store components. The
backend must resolve an explicit configured path (with documented overrides),
validate the selected version and required tools/capabilities, record effective
path/version when material, and fail with an actionable diagnostic if missing
or incompatible. Do not install system SDKs, mutate `/opt`, install a Linux
NVIDIA driver, or select the first matching `nvcc`/Vulkan tool on ambient PATH.

It is acceptable for the WSL environment to define `CUDA_HOME`, `CUDA_PATH`,
`VULKAN_SDK`, and PATH additions to explicitly selected SDK binaries. LunarG
`setup-env.sh` changes `LD_LIBRARY_PATH`, `VK_ADD_LAYER_PATH`,
`PKG_CONFIG_PATH`, and `CMAKE_PREFIX_PATH`; activate those full changes only
for intentional native WSL Vulkan work. They must not implicitly enter
Windows-target dependency discovery. The installed Vulkan SDK supplies the
shader/SPIR-V tools used in the tested environment; do not require duplicate
Ubuntu shader packages for that same SDK. Revalidate this for future SDK
versions.

CUDA 12.4's reconstructed runfile payload was reported as an environment
provisioning artifact, not a target component. The retained evidence has the
original NVIDIA runfile hash and G0A.3 component/PTX logs, but does **not**
contain the E0.1 reconstructed archive's manifest and archive hash. Therefore
the reconstruction hash, full deterministic-rebuild comparison, and
manifest/file-hash/symlink/pkg-config checks are not independently verified
from the retained files in this consolidation. Recover and retain the E0.1
manifest before claiming those properties. The maintainer's reported cause
was the legacy installer's unavailable legacy libxml2 ABI on Ubuntu 26.04;
this is not evidence that CUDA 12.4 is vendor-supported on Ubuntu 26.04.

The tested CUDA 12.4.99 PTX compile failed on a glibc declaration conflict
without a workaround and passed with
`-Xcompiler=-U_GNU_SOURCE`. Production must reproduce/classify the condition
and apply a narrowly scoped validated workaround only to an affected
configuration, or select another coherent supported environment/toolchain.
Keep an effective workaround in build identity. Preserve original diagnostics.
`--allow-unsupported-compiler` is not a compatibility test or a substitute.
The exact E0.1 reconstruction artifact is `[UNVERIFIED]` from retained
evidence: do not claim its archive hash, deterministic reconstruction,
manifest/file-hash validation, symlink validation, or relocatable pkg-config
validation until the E0.1 manifest/report is recovered. The original NVIDIA
runfile SHA-256 `e6a842f4eca9490575cdb68b6b1bb78d47b95a897de48dee292c431892e57d17`
and Vulkan SDK tarball SHA-256
`b7867d8ca658563813af666f34b93db87f1b1c7d9d69d97e924c210c7ba33eb8` in the
G0A.5 inventory are source-archive hashes, not the reconstructed CUDA archive
hash.

### Layer B — project-managed/versioned tools and inputs

Project store/registries own the items the project selects and provisions:
LLVM-MinGW; explicit GCC 12.5 `-ccbin` where project-managed; Rust compiler,
matching target std and required Cargo/cargo-c inputs; component sources and
patches; target headers; nv-codec headers; project-pinned OpenCL/Vulkan target
headers/interfaces where needed; import-library generation recipes and
outputs; shader/component sources; and target dependency metadata. Keep
network access opt-in for these artifacts and give missing-source failures
different diagnostics from missing prepared WSL SDK prerequisites.

### Layer C — Windows target prefix and build graph

Contains only Windows-target headers, objects, libraries, metadata, generated
import libraries and artifacts for one build identity. Validate PE/COFF
objects and archives. Target pkg-config/CMake/Meson lookup must be isolated
from Linux SDK/library paths; use a target-only `PKG_CONFIG_LIBDIR`/wrapper and
explicit cross files/toolchain file. Never let `/usr/lib`, WSL CUDA libraries,
or Vulkan SDK native libraries satisfy a Windows target link.

### Layer D — physical Windows runtime

Examples: system UCRT and Windows DLLs; required packaged LLVM-MinGW runtime
DLLs (including libc++/libunwind as required); NVIDIA Windows driver,
`nvcuda.dll`, `nvEncodeAPI64.dll`, `nvcuvid.dll`; system `vulkan-1.dll` plus
vendor ICD; system `OpenCL.dll` plus vendor ICD/platform. These are runtime
dependencies, not WSL target libraries. Do not blindly copy system/driver
graphics DLLs into a package.

## 5. Runtime and build-input findings

### 5.1 ABI and runtime contract

Expose C-compatible fixed-width values and caller-owned buffers across
language boundaries. C++ exceptions and Rust panics must be caught inside
their language-specific implementation and returned as an error status; neither
may unwind across the C ABI. The G0A.1 test also showed Rust static panic
support and LLVM-MinGW shared libunwind coexisting for this contained-error
contract.

The actual UCRT path was observed despite Rust target/link diagnostics
containing `-lmsvcrt`: tested PE imports used UCRT API sets and Windows loaded
`ucrtbase.dll`. Do not generalize from the target spec or a link flag; inspect
the actual effective runtime in production acceptance.

### 5.2 CPU specialization

G0A.2 used Windows CPU/OS state and compared compiler-native resolution,
explicit flags, generic target and a model-only case. On this exact host and
toolchain, compiler-native selection was validated by execution; a bare CPU
model did not fully describe effective features. Windows-host capability is
authoritative for target specialization. Cross-host portability is not the
objective. The exact observed `skylake-avx512`/feature-flag string is evidence
for that PoC only, not a backend policy. Production must validate every
effective ISA feature against Windows-reported CPU and OS state.

### 5.3 CUDA/NVIDIA

G0A.3 proved Linux NVCC PTX generation for TITAN V / compute capability 7.0,
Windows Driver API PTX JIT, and kernel output validation. G0A.5 then proved
integrated FFmpeg 9.0 NVENC encode, NVDEC/CUVID H.264 decode, and CUDA filter
execution on that same Windows host. NVIDIA API probing and the Windows driver
are target-runtime concerns; Linux WSL CUDA libraries are not target inputs.

The observed host/device/API support does not establish AV1 decode or any
other untested codec on this GPU. Record each codec/profile capability
independently. The driver/header combination tested is not a universal minimum.
Keep FFmpeg's explicit `--enable-nonfree` decision visible when selecting
NVCC; assess redistribution of the complete configured build separately.

### 5.4 Vulkan

Verified Windows runtime path:

`Windows executable -> installed Windows vulkan-1.dll -> installed NVIDIA ICD -> selected physical TITAN V -> compute dispatch`

The standalone G0A.4 PE dynamically resolved the system loader, and G0A.5's
FFmpeg Vulkan filter ran on the selected TITAN V. A separately cross-built
Vulkan Loader is not a default runtime prerequisite for this tested path.
Still provide whatever target import interface a particular dependency
actually requires; do not turn this result into a claim about all future
dependencies.

### 5.5 OpenCL

Verified Windows runtime path:

`Windows executable -> installed Windows OpenCL.dll -> vendor ICD/platform -> selected NVIDIA TITAN V -> program build and kernel dispatch`

G0A.4 enumerated two platforms on this host (NVIDIA CUDA and Intel OpenCL);
G0A.5 explicitly ran the FFmpeg OpenCL filter. Its target link used an
OpenCL import interface for the system dispatcher; that is not a separately
distributed ICD Loader or vendor implementation. A second ICD Loader is not
a default runtime prerequisite for the tested path. Production probing must
preserve platform/device identity and explicit selection.

### 5.6 FFmpeg 9.0 and libplacebo

G0A.5 used an actual Windows-target FFmpeg 9.0 configure/build, not standalone
API probes alone. The core feature-slice binary reported `cuda`, `opencl`,
`vulkan`, `h264_nvenc`, `h264_cuvid`, `scale_cuda`, `scale_vulkan`, and
`unsharp_opencl`; Windows interop ran encode, decode, and filters. The
configuration was intentionally minimal and is not the full project feature
set.

Phase B built and ran a separate FFmpeg 9.0 variant with static libplacebo
and glslang; `libplacebo` initialized the Vulkan device and processed frames.
Static libplacebo C++ objects required libc++/libunwind link flags to reach
the final FFmpeg C linker invocation. The PoC solved this through isolated
pkg-config metadata. Production must make target dependency metadata/link
composition transitively express static C++ runtime requirements; do not
prescribe or reuse that temporary `.pc` edit. The runtime test used copied
`libc++.dll` and `libunwind.dll`, so package/runtime policy must include those
DLLs when the production dependency graph remains dynamically dependent on
them.

## 6. Build identity and reuse policy

Do not choose a final serialization or hash format here. G1 must define
material inputs and comparison semantics.

| Category | Examples | Why |
|---|---|---|
| Raw diagnostics | Full PowerShell/CIM dumps, all enumerated devices, compiler logs, unused tool versions, probe timestamps. | Useful for audit/troubleshooting; do not invalidate artifacts merely because incidental formatting/order changed. |
| Effective build inputs | Target triple/ABI; LLVM-MinGW identity/hash; C/C++ runtime; Rust compiler and target std; FFmpeg/component source/version/hash and patches; configure features/options; CPU feature policy and effective flags; CUDA SDK/NVCC, explicit `-ccbin`, architecture and validated workaround state; nv-codec headers/API selection; Vulkan SDK/shader compiler/target environment; OpenCL/Vulkan target interfaces; generated import libraries and metadata; static C++ runtime requirements. | Material changes can change PE/COFF output or link behavior; do not reuse target artifacts across them. |
| Runtime acceptance inputs | Windows build/UCRT where material; CPU/OS-enabled ISA; selected GPU and capabilities; NVIDIA driver/API; selected Vulkan ICD/device; OpenCL dispatcher/platform/device/driver. | A change may invalidate previous runtime acceptance or require a compatible rebuild even when source objects remain unchanged. |

CPU-specialized binaries bind to the effective CPU/OS capability set. CUDA
PTX, driver JIT, NVENC/NVDEC, Vulkan, and OpenCL acceptance bind to selected
device/runtime state. Do not include an incidental value just because a PoC
printed it; document why any field is build-material or acceptance-material.

Before overwriting saved config, selecting resume point, trusting `completed`
or `system`, or reusing an installed prefix/build tree, compare a fresh identity
with the saved one. Unknown, corrupt, legacy, missing-probe, or incompatible
identity must never be treated as a match. Preserve old data; start in an
isolated identity-specific workspace/prefix or stop with an explicit
diagnostic. Recheck runtime identity before acceptance. A state-file reset
alone is insufficient.

## 7. Production gates

Every gate records inputs, configuration, commands, outputs, and pass/fail or
not-validated runtime status. Existing PoC results establish architecture and
acceptance techniques; PoC-built files are not reusable production artifacts.

### G1 — Windows probe and identity isolation

- **Scope / code:** Add a fresh Windows interop probe (module name may be
  `windows_probe.py`), explicit overrides, identity model and guard before
  resume/reuse. Integrate with `platform_detect.py`, `config.py`, `app.py`,
  `state.py`, `builder.py` and workspace selection.
- **Status:** [IMPLEMENTATION] No production probe or identity enforcement is
  implemented by this consolidation.
- **Probe contract:** Windows version/build; CPU model/signature plus effective
  ISA and OS-enabled state; GPU inventory; NVIDIA model, compute capability,
  driver, and NVENC/NVDEC/API information needed for selection/acceptance;
  Vulkan loader, ICD and device; OpenCL dispatcher, platforms and devices;
  later configured D3D/QSV/AMF data. Keep detailed dumps diagnostic-only
  unless a field is material. Support explicit sufficient overrides.
- **Failure behavior:** Interop disabled, failed, malformed, timed out, or
  incomplete must not fall back to WSL target detection or silently reuse
  unknown identity. Fail clearly unless an explicit sufficient override
  satisfies the contract. Separate target capabilities from build-host tools.
- **Tests:** Mock PowerShell/NVIDIA/loader inventories; no-GPU, multi-GPU and
  multiple-OpenCL-platform cases; failures/overrides; changed OS/CPU/GPU/driver
  and relevant runtime identity; same-identity resume; incompatible/legacy
  state, stale files, custom workspace, status `system`, and state drift before
  acceptance.
- **Inputs/artifacts:** Interop enabled on the physical host, fixtures, old
  and new state/prefixes; probe evidence and identity persisted with state.
- **Pass:** fresh target identity is established or explicitly overridden;
  same identity may resume; incompatible/unknown identity cannot reuse state
  or mutable artifacts; diagnostics explain rejection.
- **Non-goals:** No CUDA/Vulkan installation, build backend, full GPU matrix,
  or final hash format.
- **Reuse:** Reproduce Windows CPU, NVIDIA, Vulkan and OpenCL queries from
  G0A.2-G0A.4; do not copy PoC probe output as production state.

### G2 — registries, store, and prerequisite validation

- **Scope / code:** Extend `toolchains.yaml`, `toolchain_registry.py`,
  `toolchain_store.py`, `toolchain_builders.py`, component/source registries
  and configuration for selected LLVM-MinGW, Rust target std, project-managed
  GCC `-ccbin` if applicable, headers/interfaces, import-library recipes,
  shader/component inputs and dependency metadata.
- **Status:** [IMPLEMENTATION] G2 validates WSL SDK prerequisites; it does not
  own or install those SDKs.
- **SDK ownership:** Validate configured installed `/opt/cuda-12.4` and
  `/opt/vulkan-sdk/1.4.363.1/x86_64`; do not install them or imply G2
  provisioned them. Check tools, versions, paths and required capabilities.
  Fail actionably on absent/mismatched SDK. Keep explicit overrides. Distinguish
  a missing environment prerequisite from an absent offline project source.
- **Tests:** Registry parsing, version resolution, checksums, target std
  compatibility, `-ccbin` version check, SDK path/version/tool checks,
  actionable missing-prerequisite/source errors, offline provisioning with
  empty external caches and no unexpected PATH selection.
- **Inputs/artifacts:** Maintainer-selected WSL SDK installations; pinned
  project artifacts and licenses/checksums; project store archives, manifests,
  installed tools and provenance.
- **Pass:** selected project-managed closure provisions offline from staged
  sources; prerequisite SDK checks use only explicit selected paths and fail
  clearly when invalid.
- **Non-goals:** SDK system installation, `/opt` mutation, Linux driver,
  complete component stack.
- **Reuse:** G0's toolchain archive identity; G0A.1's target-std requirement;
  G0A.3/G0A.5's versioned GCC/NVCC pairing; G0A.4's shader tools. Do not
  import isolated PoC archives or metadata as production without registry
  verification. Recover E0.1 reconstruction manifest/hash before asserting
  reproducible archive provenance.

### G3 — `linux-cross-mingw` backend and platform strategy

- **Scope / code:** Add backend selection and platform strategy, e.g.
  `platforms/linux_cross_mingw.py`; resolver/config integration; explicit
  host-native build tools; Windows target compilers; target prefix; target-only
  pkg-config; CMake toolchain and Meson cross files; target runtime settings;
  NVCC and explicit `-ccbin`; SDK host-tool paths; Windows-host-derived CPU
  specialization; import-library/dynamic-loader strategy.
- **Status:** [IMPLEMENTATION] The production backend is not implemented.
- **Isolation:** No MSYS path rewriting. Do not reuse `windows-ucrt64` path
  separators, shell wrapping, or package discovery. Keep host and target
  compilers/tools distinct. Prevent Linux SDK/library paths from target
  pkg-config, CMake and Meson.
- **Link requirements:** Model libc++/libunwind and other static C++ runtime
  needs transitively in target metadata/link composition. Handle Vulkan and
  OpenCL target interfaces only where the actual dependency requires them.
- **Tests:** Resolver and config; generated compiler/environment/target
  prefix; pkg-config contamination rejection; generated CMake/Meson files;
  explicit SDK/NVCC/`-ccbin`; native Linux, WSL Linux, and MSYS2 regressions.
- **Inputs/artifacts:** G2-resolved compilers, tool metadata, target headers
  and import interfaces; generated cross files and dry-run commands.
- **Pass:** dry-run core commands use intended host tools and Windows target
  tools; target lookups cannot resolve WSL libraries; unrelated backends
  remain unchanged except tested identity safety.
- **Non-goals:** Full dependency builds, G5 hardware matrix, package.
- **Reuse:** G0A.5 configure/link behavior and libplacebo metadata lesson;
  reimplement independently, with no PoC paths.

### G4 — complete software dependency-stack cross-build

- **Scope / code:** Add target support/adapters in existing component builders
  and declarative registries for the complete configured software stack,
  dependency order, static/shared choices and Rust/C++ runtime consistency.
  Use the G3 architecture instead of treating every component as a new
  feasibility gate.
- **Tests:** Component-level build/configure tests, graph/order, metadata and
  archive/object inspection; target pkg-config correctness; no Linux `.so` or
  ELF leakage; PE/COFF checks; target executable smoke on physical Windows;
  same-identity incremental/resume behavior.
- **Inputs/artifacts:** Complete configured source/patch closure, toolchain,
  host tools, target prefix and isolated mutable build trees.
- **Pass:** Every configured software component builds for Windows, target
  outputs contain no Linux libraries, and software FFmpeg/ffprobe execute on
  Windows with representative software encode/decode tests.
- **Non-goals:** Do not require unavailable hardware; no packaging or claim of
  arbitrary-host support.
- **Reuse:** G0A.1 ABI/runtime and G0A.5 target build techniques as test
  designs, not build artifacts.

### G5 — required hardware-feature matrix

- **Scope / code:** Implement required CUDA/NVIDIA, Vulkan/libplacebo,
  OpenCL, D3D11VA/DXVA2/D3D12VA, QSV/oneVPL, AMF and configured neural
  backend support in component registry/builders and FFmpeg configuration.
  Maintain separate assertions for each encoder, decoder, hwaccel, filter,
  codec/profile and configured capability.
- **Tests:** Build-presence assertions independent of hardware; per-device
  runtime tests only for hardware present and feature-supported; verify
  selected device and actual hardware execution, not software fallback.
  Report `passed`, `failed`, `not runtime-validated (absent)`, and
  `not runtime-validated (unsupported)` distinctly. Do not count unavailable
  hardware as a pass and do not require absent hardware to validate.
- **Inputs/artifacts:** G1 device/runtime probe, G2 headers/SDK prerequisites,
  feature config, explicit expected matrix and suitable test media; acceptance
  report tied to runtime identity.
- **Pass:** All configured build features are independently present; all
  host-applicable runtime tests pass; absent/unsupported hardware remains
  explicitly unvalidated.
- **Non-goals:** Do not silently drop configured capabilities or report an
  unsupported capability as a successful runtime test.
- **Reuse:** G0A.3 CUDA/PTX/JIT, G0A.4 Vulkan/OpenCL loaders, G0A.5 FFmpeg
  NVIDIA/CUDA/Vulkan/OpenCL/libplacebo execution techniques. Rebuild and run
  production acceptance tests; never reuse PoC binaries.

### G6 — Windows packaging and runtime policy

- **Scope / code:** Extend `release_bundle.py` and package metadata for the
  produced Windows binaries. Inspect PE imports and dynamically loaded
  dependencies; classify UCRT/system Windows DLLs, LLVM-MinGW runtime DLLs,
  libc++/libunwind where required, and Windows graphics/GPU driver runtimes.
- **Tests:** Controlled PATH launch independent of MSYS2/WSL; dependency
  inspection; package relocation; start `ffmpeg.exe`/`ffprobe.exe`; applicable
  host hardware smoke. Do not assume dynamically loaded APIs appear in the
  PE import table.
- **Inputs/artifacts:** Identity-specific G4/G5 binaries, dependency graph,
  runtime DLL closure and documented external system/driver prerequisites.
- **Pass:** Bundle starts from intended location on the physical target,
  contains required distributable toolchain DLLs, and does not copy system or
  driver-owned GPU runtimes blindly.
- **Non-goals:** Clean-machine or cross-host portability; repackaging Windows
  system DLLs.
- **Reuse:** G0/G0A observed UCRT, libc++, libunwind, libomp, Windows loader
  and driver paths as dependency classifications only.

### G7 — regression, documentation, and full acceptance

- **Scope / code/docs:** Run the complete configured build and acceptance
  flow; native Linux, WSL Linux, macOS and existing MSYS2 regressions as
  applicable; same-identity resume and incompatible-identity rejection;
  verify the complete Windows bundle and host-applicable hardware matrix.
  Update `README.md`, `docs/DeveloperReadme.md`, and
  `docs/CHANGELOG.md`. State MSYS2-UCRT64 deprecation consistently with the
  project decision and actual parity.
- **Tests:** Full `pytest tests/`, `black --check .`,
  `python scripts/check_mypy_baseline.py`; full configured component set;
  resume/interruption; identity drift; package checks; all configured
  capabilities and host-applicable runtime tests.
- **Inputs/artifacts:** Clean identity-specific workspace, offline project
  inputs, prepared validated SDKs, Windows runtime probe and bundle.
  Preserve per-capability acceptance statuses for unavailable hardware.
- **Pass:** Full configured stack builds and runs on the recorded host,
  regressions pass, incompatible state cannot be reused, package/runtime
  checks pass, and docs accurately record unvalidated capabilities.
- **Non-goals:** Claiming arbitrary Windows-host portability or forcing
  acceptance of absent devices.
- **Reuse:** Early evidence is an acceptance-test blueprint only; G7 must
  exercise production-built artifacts and current host identity.

## 8. Open issues, grouped by resolving gate

| Gate | Open issue | Required resolution |
|---|---|---|
| G1 | Exact identity schema, normalization, mismatch UX, and treatment of runtime-only drift. | Choose and test semantic identity and isolation; no hash scheme is preselected. |
| G2 | E0.1 reconstructed CUDA archive hash/manifest is absent from retained evidence; exact determinism and symlink/pkg-config checks cannot be independently confirmed here. | Recover the report/manifest if archive provenance is needed; validate the installed prerequisite independently. |
| G2 | Future CUDA toolkit coexistence and explicit version selection. | Resolve configured SDK path/version; reject ambiguous ambient tools. |
| G2/G3 | Exact generated Vulkan/OpenCL target import-interface strategy where dependencies require one. | Pin or generate appropriate target interfaces; do not build a loader without a demonstrated link need. |
| G3 | Production implementation of CPU flag derivation and compatibility-workaround classification. | Derive from effective Windows capabilities and validate selected compiler; scope `_GNU_SOURCE` workaround narrowly. |
| G4 | Remaining full software component cross-build issues and complete offline provisioning closure. | Resolve builder/registry/provisioning gaps component-by-component with target-isolation tests. |
| G5 | Neural-network backend/API and complete D3D/QSV/AMF feature support. | Select required backend/API and prove target build/runtime; only an unavoidable MSVC-only ABI limitation can reopen Variant 2. |
| G5 | Hardware capabilities absent or unsupported on the tested TITAN V host, including untested codec/profile combinations. | Retain build-presence assertions; record runtime acceptance as not validated where appropriate. |
| G6 | Final runtime DLL closure and redistribution policy for all production dependencies. | Inspect built package; include LLVM-MinGW/C++ runtime DLLs where required and document external Windows/driver prerequisites. |

No separate feasibility risk remains for LLVM-MinGW/UCRT smoke execution,
contained C ABI mixed Rust/C++ use, host-specific CPU specialization on the
tested configuration, CUDA PTX/JIT, tested NVENC/NVDEC, Vulkan/OpenCL runtime,
or the early FFmpeg 9.0 feature slice. These are verified within their stated
scope, not universally.

## 9. Evidence provenance and limits

Retained PoC evidence was found under
`/home/viktor/.copilot/session-state/008992f9-d4d6-4f56-9631-29d787c28745/files/`.
The phase-specific summaries/logs cited in §2 are the sources actually used.
G0A.2-G0A.5 did not have a single retained final report; conclusions above
come from their retained commands, status files, runtime logs, feature lists,
and artifacts. G0A.5's `input-hashes-and-versions.txt` records the source
archives and original CUDA runfile/Vulkan SDK archive hashes, but is not the
missing E0.1 reconstructed CUDA archive manifest.

G0A.1/G0A.5 PoC artifacts and temporary metadata are retained for evidence
only. Production code and tests must independently resolve inputs, build,
link, inspect and execute the relevant behavior. The E0.1 reconstructed CUDA
archive, if recovered, is an environment-provisioning artifact and never a
Windows target component.

## 10. Ordered implementation checklist

1. **G1:** Implement fresh Windows interop probe and identity guard; test fresh,
   changed, failed, overridden, legacy and same-identity cases.
2. **G2:** Register/provision project-managed inputs offline; validate explicit
   installed CUDA/Vulkan SDK prerequisites and selected host compiler.
3. **G3:** Add `linux-cross-mingw`, isolated target environment and cross files;
   verify dry-run commands and host/target isolation.
4. **G4:** Cross-build the full configured software dependency stack; verify
   PE/COFF, no Linux leakage, runtime and resume.
5. **G5:** Build-assert every configured hardware feature and run only
   host-applicable runtime tests, retaining unsupported/absent statuses.
6. **G6:** Build and validate the Windows bundle with explicit DLL and external
   system/driver runtime policy.
7. **G7:** Run full regressions and acceptance; update user/developer docs and
   changelog; finalize MSYS2-UCRT64 deprecation status.
