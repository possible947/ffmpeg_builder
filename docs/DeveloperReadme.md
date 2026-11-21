# FFmpeg Builder — Developer Guide

This document describes the internal architecture, module responsibilities, data flow, and extension points of the FFmpeg Builder system.

## Release Status

The current package version is `3.0.0`. The toolchain store replaces the
external `nix shell` environment: compilers are built on the host for the
host, and both FFmpeg versions build end-to-end (Linux, GCC 16.2.1 host: 8.1
with deployed gcc12 12.5.0 and 9.0 with the system GCC — 58/58 components
each, smoke-tested). WSL-Mingw G1/G2 preparation and offline-closure
acceptance are complete; G3 has established and validated the
`linux-cross-mingw` cross-environment and shared build contracts. G3 is PASS;
G4 Waves 0–9 are complete. Wave 1 accepted seven Windows target dependencies;
Wave 2 accepted eleven more through production Autotools and CMake paths with
the mandatory primary profile (`native_build=true`, `openmp=true`), and Wave
3A accepted six LV2 dependencies through Meson. Wave 3B reused target libogg
and accepted libvorbis 1.3.7 and libtheora 1.2.0 through Autotools, validating
their COFF archives, target pkg-config closure, link smokes, and same-identity
reuse. Wave 4A accepted static SVT-AV1 4.2.0 and AOM 3.13.3 using the shared
CMake cross path and host NASM producing COFF; AOM's static metadata records
target pthread and its LLVM-MinGW runtime DLL. The LLVM-MinGW `libomp.dll`
runtime was confirmed on physical Windows. Wave 4B accepted x264, x265 and
libvpx with host NASM, Windows COFF archives, target metadata and PE link
smokes. x265's 12/10/8-bit archives are merged and independently rechecked;
the registered 4.2 archive's embedded version reports `4.0+1-6318f22`.
Wave 4C built zimg 3.0.6 from its checksum-verified offline archive using
host autoreconf and the shared Autotools cross projection; the legacy MSYS
lookup is not reached. Its static COFF archive, C and C++ headers, metadata,
PE link smokes, libc++/libunwind transitive requirements, and same-identity
reuse were accepted. Wave 5A revalidated target zlib and accepted static
OpenSSL 3.6.4 and SRT 1.5.6 in dependency order. Their target metadata,
COFF archives, combined encrypted SRT/OpenSSL/zlib PE link, physical Windows
digest/TLS/SRT smoke and same-identity reuse passed. Wave 5B accepted libzmq
4.3.5 via the shared Autotools cross projection, static COFF and target-only
pkg-config metadata, negative dependency links and a Windows inproc socket
smoke. Its libc++/libunwind/pthread runtime DLLs are classified for G6.
Wave 5C accepted static libjxl with target lcms2 and pinned Brotli/Highway,
and the VapourSynth header interface with external `VSScript.dll`. Both were
revalidated and reused. Wave 6 accepted rav1e 0.8.1 via the project Rust and
LLVM-MinGW target, with staged static C interface, target pkg-config metadata,
physical-Windows smoke and same-identity reuse. Wave 7 accepted the existing
Vulkan target inputs and matching registry, glslang with its pinned SPIR-V
optimizer sources, fast-float and Vulkan-enabled static libplacebo. A Windows
consumer compiled and dispatched a shader and verified pixels; target metadata,
COFF outputs and same-identity reuse passed. Wave 8 reused G2 nv-codec target
headers and accepted CUDA-enabled static libvmaf with host NVCC/bin2c and
LLVM-MinGW target compilation; a Windows consumer ran its CUDA motion, ADM
and VIF extractors, and reuse passed. Wave 9 accepted the G2 OpenCL target
headers and import interface using the physical Windows target's OpenCL GPU;
its Windows kernel execution, component state and reuse passed. `OpenCL.dll`
and the vendor ICD remain external runtime prerequisites. The physical target
has no Intel or AMD GPU, so oneVPL/QSV and AMF were not selected. The final
Windows FFmpeg integration has not started. The
compatibility/portable profile is `native_build=false`,
`openmp=false`; it is for portable releases/diagnostics and cannot substitute
for a failed primary-profile build.
See `CHANGELOG.md` for the release history.

## Architecture Overview

```
                    +-----------------+
                    |   __main__.py   |
                    +--------+--------+
                             |
                    +--------v--------+
                    |     app.py      |  FFmpegBuilderApp
                    |  (orchestrator) |  - owns all managers
                    +--------+--------+
                             |
            +----------------+----------------+
            |                |                |
   +--------v------+ +------v-------+ +------v--------+
   | platform_     | |  config.py   | |   state.py    |
   | detect.py     | | ConfigManager| | StateManager  |
   | SystemInfo    | | BuildConfig  | | BuildState    |
   | PlatformInfo  | | YAML R/W     | | JSON R/W      |
   | ToolInfo      | +--------------+ +---------------+
   +--------+------+
            |
   +--------v------+
   | system_       |
   | report.py     |
   | SystemReport  |
   +--------+------+
            |
   +--------v--------+      +------------------+
   |   components.py |      |    builder.py    |
   | Component       |<---->|  FFmpegBuilder   |
   | ComponentRegistry|     |  - download      |
   | BuildSystem     |      |  - configure     |
   +-----------------+      |  - make          |
                            |  - install       |
                            +---+-------+------+
                                |       |
                    +-----------+   +---v----------+
                    |               | executor.py  |
           +--------v------+       | CommandExec  |
           | downloader.py |       | - subprocess |
           | Downloader    |       | - logging    |
           | AsyncDlMgr    |       | - stdin      |
           | requests+tqdm |       |              |
           +---------------+       +--------------+
```

## Module Reference

### `windows_probe.py`, `build_identity.py`, `target_guard.py` (G1/G2.7)

Windows target probe (WSL interop, overrides), semantic build/runtime identity
and the pre-reuse guard used by `app.prepare_target()` and
`scripts/run_build.py`. G2.7 adds the resolved WSL-Mingw material-input
projection without changing native identity requirements. Details:
`docs/windows_build_strategy.md`, "G1 implementation notes" and §6.

### `target_inputs.py`, `scripts/prepare_target_inputs.py` (G2.6/G2.8/G3)

Resolve and stage the checksum-pinned NVIDIA, Vulkan, and OpenCL target headers
plus the generated Windows Vulkan/OpenCL import interfaces. The disposable
target prefix and manifest are reproduced offline from `third_party/sources`;
their provenance and materialisation ID feed the schema-2 identity. G2.8
verified independent reproducibility and target contamination boundaries.
After the identity guard authorizes the target workspace, G3 materializes or
revalidates the same manifest under that workspace's `prefix/`. It does not
stage the G4 dependency stack or build Windows components.

### `cross_environment.py`, `platforms/linux_cross_mingw.py` (G3)

The resolver selects a distinct Linux/WSL-to-Windows strategy for
`target_backend: linux-cross-mingw`. The strategy consumes the existing
`WslMingwPreparation`; it does not re-probe Windows or rediscover toolchains.
`construct_cross_environment()` creates identity-local target/build/generated/
diagnostic paths, resolves target tools from the verified LLVM-MinGW profile,
keeps host build tools separate, filters target-sensitive inherited state,
generates a target-only pkg-config wrapper plus deterministic CMake/Meson
cross files, and exposes Autotools and Rust projections. The accepted target
prefix contains only G2.6 headers/interfaces at this stage.

`run_cross_environment_acceptance()` executes temporary C/C++/resource and
static-archive probes, target pkg-config isolation, and minimal CMake, Meson,
and Autotools fixtures. The TUI and normal headless build route stop after
these checks; headless `--dry-run` additionally reports G4 Wave-0 component
roles and selection, then stops before any component build. This is not a
Windows FFmpeg build. When
`native_build` is enabled, target C/C++ uses exactly `-march=native
-mtune=native`; LLVM-MinGW interprets `native`, and the project does not
expand or independently validate compiler-selected ISA features. G1 CPU data
remains physical-target identity/runtime evidence, not a compiler feature
whitelist. G3 acceptance proves CUDA PTX/fatbin generation with selected NVCC
and GCC `-ccbin`, host bin2c conversion, LLVM-MinGW compilation of generated C
to COFF, and a PE32+ link. Rust/Cargo staticlib and transitive C++ runtime
links are accepted, along with a host-generator-to-target mixed-role pipeline
and target Vulkan/OpenCL interfaces. CUDA-enabled FFmpeg and libvmaf component
recipes remain fail-closed until their G4 adapters are implemented; G3 stops
before the component graph and does not claim a full Windows FFmpeg build.

### `__main__.py`

Entry point. Instantiates `FFmpegBuilderApp` and executes the application.

### `app.py` — `FFmpegBuilderApp`

Central orchestrator. Responsibilities:

- Initialize all managers (config, state, platform detection, component registry)
- Dynamically synchronize version-specific workspaces (`workspace_81`, `workspace_90`) with configuration
- Run the main event loop: show system report, handle user actions
- Coordinate the build process: iterate components, handle errors, update state
- Manage cleanup

Key methods:

| Method | Description |
|--------|-------------|
| `run()` | Main loop — shows screens, dispatches actions, synchronizes workspace |
| `_sync_workspace(config)` | Synchronize `workspace`, `packages`, and `StateManager` with current FFmpeg version |
| `_run_build(config, resume)` | Build loop with retry/skip/abort error handling and Live dashboard lifecycle |
| `_get_buildable_components(config)` | Resolve the component set shared by build and info screens |
| `_cleanup()` | Remove active versioned workspace, packages, and state file (as well as legacy workspace) |

The build session owns a `BuildDashboard` renderable in `ui/dashboard.py`. `StateManager.status_listener` forwards builder status updates to the dashboard, and `AsyncDownloadManager` uses callback hooks for background download row updates. `Live(..., screen=True)` is active only during the build loop; it is stopped before interactive error prompts and final reports.

Build loop logic:

```
idx = 1
while idx <= len(components):
    try:
        builder.build_component(components[idx - 1])
        mark_completed()
        idx += 1
    except BuildError:
        action = error_handler.handle_error(...)
        if retry:  continue          # same idx
        if skip:   mark_skipped(); idx += 1
        if abort:  break
```

### `config.py` — Configuration Management

**`BuildConfig`** — dataclass holding all build settings:

```python
@dataclass
class BuildConfig:
    ffmpeg_version: str = "8.1"
    gpl_enabled: bool = False
    make_release: bool = False
    native_build: bool = False
    full_static: bool = False
    enable_libvmaf: bool = True
    enable_libvmaf_cuda: bool = True
    cuda_build_mode: str = "auto"
    cuda_compute_capability: Optional[str] = None
    allow_nonfree_cuda: bool = False
    enable_libplacebo_vulkan: bool = False
    disable_lv2: bool = False
    num_jobs: str = "auto"
    async_downloads: bool = True
    download_workers: int = 4
    macos: MacOSConfig          # clang version, openmp
    linux: LinuxConfig          # c_standard, cxx_standard
```

`ffmpeg_version` supports `8.1` (default) and `9.0`. Adding a version requires a
declared FFmpeg source profile with its archive name and verified SHA-256, a matching
configuration validation entry, and version-specific configure-flag tests. Record the
completed profile and validation results under `Unreleased` in `docs/CHANGELOG.md`.
Run tests through the project virtual environment and commit each discrete version
profile step atomically after its focused validation.

Version-specific toolchain ranges and fallbacks are defined in
`toolchains.yaml`:

- On Linux, FFmpeg 8.1 uses system GCC `>=12,<14` or Clang `<=25` when
  compatible, otherwise the `gcc12` store fallback.
- Linux FFmpeg 9.0 requires GCC `>=15`; no fallback is currently defined.
- macOS allows FFmpeg 8.1 only with configured and available `macports-clang-17`.
- CUDA resolves and probes its own host compiler via `nvcc -ccbin`, which may
  differ from the compiler used for FFmpeg itself.

Existing configurations without an `ffmpeg_version` field retain the `8.1` default.
Set `ffmpeg_version: "9.0"` explicitly to select the FFmpeg 9.0 source profile.

### Toolchain store and provisioning

`toolchains.yaml` (repo root) is the declarative source of truth, loaded and
validated by `toolchain_registry.py`:

- `toolchains:` — named profiles (`gmp`, `mpfr`, `mpc`, `gcc12/13/15`, `rust`,
  `cargo-c`) with `kind`, `platforms`, `provides`, `materialize`, an optional
  human-readable `note`, and a `source` block whose variants are `build`,
  `binary`, `download`, or `local` (`source.default` preselects one).
- `platform_profiles:` — keyed by `(ffmpeg_version, platform)`; declares the
  `system_tool_range` that decides whether the host toolchain is used or a
  `fallback_toolchain` is deployed.
- `cuda:` — CUDA Toolkit version → host-compiler profile.

`toolchain_store.py` owns `<root>/{archives,installed,logs,locks,profiles,build}`
and `state.json`. The root must not overlap the build workspace. `acquire()`
dispatches on the source variant; `build` variants are executed by
`toolchain_builders.py` recipes (`gcc`, `autotools`, `rust_standalone`).

Registry arguments support three placeholders, expanded by
`toolchain_builders.resolve_args()` before a recipe runs: `{prefix}` (install
prefix), `{jobs}` (parallel `make` value from the `jobs=` argument or the
detected CPU count), and `{prereq:<name>}` (bootstrap prerequisite prefix;
unresolved names fail fast). `bootstrap.requires` installs prerequisites first,
so a profile's libraries always come from the store rather than the host.

Two portability rules apply to every recipe:

- Bootstrap commands run through `default_runner()`, which uses `clean_env()` to
  drop FFmpeg/vendor build variables (`CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `CPATH`,
  `LIBRARY_PATH`, `CUDA_*`, `VULKAN_SDK`, `RUSTFLAGS`, `AS`, …). A toolchain
  build must never inherit the FFmpeg build's flags.
- Recipes use only paths derived from their arguments. Any host-specific flag
  needed to build pre-C23 sources (e.g. `CFLAGS=-O2 -std=gnu17` for
  `gmp`/`mpfr`/`mpc`, whose configure probes GCC 15+ rejects under its `gnu23`
  default) is declared as registry data, not hardcoded in Python.

After every source build, `acquire()` runs `toolchain_builders.verify_installation()`
and records `InstalledToolchain.verified` in `state.json`. Verification is
capability-based, not file-presence-based: GCC and G++ must match the registry
version and compile and link C and C++ probes, Rust must report `rustc`/`cargo`
versions, and bootstrap libraries must provide headers plus an archive. A failure
raises and the profile is not recorded; `profile_paths()`/`component_env()` return
nothing for an unverified entry, and the app's gate distinguishes "not installed"
from "failed verification".

Portability constraint: an installed GCC bakes its `--prefix` into header and
library search paths, and GCC 12.5.0's configure has no `--enable-relocatable`,
so a store root must stay where it was built unless relocation is implemented
(prefix rewriting/RPATH fixups). `scripts/prepare_toolchains.py --verify-only`
re-runs verification for everything installed by default, or only named entries
with `--profiles`, and is the acceptance tool for relocation tests (move the
store, re-verify). It never installs or rebuilds a toolchain.

`ToolchainRegistry.recommend_profiles()` derives the profiles to deploy from the
current system analysis (fallback for an out-of-range host tool, bootstrap
prerequisites, component-declared toolchains, the CUDA table), and both the TUI
store and `scripts/prepare_toolchains.py` consume it — the TUI's `p` action and
the script's default selection are the same computation.

#### Rust target std as an additive artifact (G2.3)

The project-managed Rust toolchain stays at **1.99.0** (no rustup, no system
Rust). A profile may declare `extras:` — project-managed archives installed into
the same staged prefix. The `rust` profile declares one:

```yaml
extras:
  - kind: rust-std
    target: x86_64-pc-windows-gnullvm
    component: rust-std-x86_64-pc-windows-gnullvm
    release: "1.99.0"
    backends: [linux-cross-mingw]
    archive_filename: rust-std-1.99.0-x86_64-pc-windows-gnullvm.tar.xz
    sha256: dcf636d9…   # channel-rust-1.99.0.toml (xz_hash)
```

- `release` must equal the profile version; kind/target/duplicates/sha256/backends
  are validated at registry load. Only `rust-std` is a supported kind.
- `backends` limits when the extra is required; native stores
  (`ToolchainStore(backend=None)`) never require it and accept composite installs.
  Wire the backend via `ToolchainStore(..., backend=...)` / `--backend`.
- `acquire` fetches and sha256-verifies base and extras first, stages the base,
  installs each extra with its `install.sh --components=<component>` into the same
  staged prefix, verifies the composite, writes provenance, then activates
  atomically. Any failure keeps the previous active install.
- Provenance (`.toolchain-install.json`) has an optional `extras` list (kind,
  target, component, release, archive_filename, sha256), sorted so order is not
  semantic. Schema stays 2; no `extras` key means none. Changing, adding or
  removing an extra makes the install `stale`; a pre-G2.3 Rust install is stale
  for `linux-cross-mingw` and is rebuilt as a complete candidate.
- Verification and the errors `extra_install_failed`, `extra_release_mismatch`,
  `extra_component_missing` are described in the CHANGELOG. `ToolchainStore.describe()`
  exposes the resolved metadata. G3 projects the managed Rust target, linker,
  archiver, and target standard library into Cargo builds. Mixed-role recipes,
  remaining builder integration, and final cross-build acceptance are still
  pending.

#### LLVM-MinGW and the hardened store (G2.2)

`llvm-mingw` (`kind: cross_compiler`, release `20261104`, UCRT,
`x86_64-w64-mingw32`) is a project-managed **binary** toolchain for the WSL-Mingw
backend (`linux-cross-mingw`). Its archive
`llvm-mingw-20261104-ucrt-ubuntu-22.04-x86_64.tar.xz` ships in
`third_party/sources` (Git LFS) and is pinned by sha256 in `toolchains.yaml`. The
backend-bound `wsl-mingw-ffmpeg-*` platform profiles select it only when
`recommend_profiles(..., backend="linux-cross-mingw")` is requested; native
recommendations are unchanged. The G3 build backend that consumes it is not
implemented.

Store behaviour (all profiles, not just LLVM-MinGW):

- **Integrity:** every archive, from the local mirror or a download, is
  sha256-verified (streaming) before extraction. A declared checksum is always
  enforced; a missing one is refused. A mismatch leaves the archive untouched and
  extracts nothing. Test fixtures that declare a placeholder digest skip hashing
  only via a test-only fixture; production code has no such bypass.
- **Staged activation:** install = resolve source → verify archive → extract into
  `installed/.staging-*` → run the declarative verifier on the staged prefix →
  write provenance → activate atomically. The previous install is parked as
  `.backup-*` and restored if activation fails; the active install is never
  removed before the candidate verified. Leftover staging/backup dirs from an
  interrupted run are recovered on the next `acquire`. Source `build` profiles
  (GCC) bake the prefix into the build, so they build in place with backup and
  rollback.
- **Verification (`cross_compiler`):** declared tools exist, version checks match,
  and a compile+link probe produces a PE32+ x86-64 executable importing the
  UCRT (`api-ms-win-crt-*`); the output is never executed.
- **Provenance:** New installations record profile, version, source variant,
  archive filename, declared source sha256 and runtime in
  `installed/<dir>/.toolchain-install.json` and state schema 2.
  `ToolchainStore.installed_status()` returns `absent`,
  `compatible`, `unverified`, `stale` (version/hash/variant/runtime/schema drift)
  or `corrupt` (state lists a missing directory). Compatible installs are reused;
  stale ones are re-acquired. Legacy installs without provenance are `stale`
  until `prepare_toolchains.py --verify-only` re-verifies them and, if the
  recorded version and any existing source metadata agree with the registry,
  backfills state provenance. GCC verification also checks both actual compiler
  versions against the registry. A selected missing or incompatible profile
  fails verification. Legacy backfill does not write a marker into the prefix,
  and the recorded registry source hash is not proof of the historical binary's
  origin.
- **Store root:** `--store` > `$FFMPEG_BUILDER_TOOLCHAINS` > `<repo>/toolchains`
  (`resolve_store_root`, shared by the TUI and the script). `--sources` selects
  the source mirror independently; with it given, no other mirror is consulted.
- **Errors:** `BuildError(category=..., code=...)`. `project_source` covers
  missing offline archive, checksum mismatch and unreadable archives;
  `toolchain_install` covers extraction, layout, verifier and activation
  failures. The underlying exception is chained.
- **External host prerequisites:** `BuildConfig.external_prerequisite_paths`
  holds optional named roots independently of `ToolchainStore`. Use
  `resolve_external_prerequisite()` with a documented environment-variable name
  and an optional technology-specific validator. Resolution is explicit config
  > that environment variable; an invalid configured root fails rather than
  falling back. The result records its canonical root, resolution source,
  host tools, diagnostics and effective values. Failures use the
  `external_prerequisite` `BuildError` category. The resolver does not search
  `PATH`, install or hash SDKs, or modify build/target environment variables.
  SDK-specific root names, tools and validation remain the responsibility of
  later technology-specific resolvers.
- **WSL-Mingw Vulkan SDK inputs:** `resolve_wsl_mingw_vulkan_sdk()` in
  `vulkan_prerequisites.py` resolves `external_prerequisite_paths.vulkan`
  before `VULKAN_SDK`; an invalid configured root is authoritative. It accepts
  either the SDK package root (with an `x86_64/` subtree) or the effective
  architecture root, then verifies the Vulkan header layout marker and resolves
  requested host tools only from that root's `bin/`. Shader compilation uses
  `glslc` or `glslangValidator`, followed by `spirv-val`; each selected tool is
  executable- and version-probed, and the shader/validation capability is
  functionally tested with a temporary compute shader and SPIR-V output.
  `spirv-opt` is selected and exercised only when the caller explicitly
  requires `spirv_optimization`; current consumers do not require optimization.
  The typed `ResolvedVulkanSdk` result carries selected/effective roots,
  configuration/environment provenance, tool paths, validated capabilities,
  and diagnostic tool-version output. Tool versions are diagnostic, not
  ToolchainRegistry inputs. The resolver does not source `setup-env.sh`, search
  `PATH`, touch Vulkan runtime/GPU state, or expose SDK libraries,
  pkg-config directories, or CMake prefixes to target discovery. It does not
  provision, copy, hash, or otherwise own the external SDK. `linux-cross-mingw`
  target preparation invokes this resolver when the configured Vulkan
  capability is enabled; existing native Linux/macOS Vulkan
  detection and build behavior are unchanged.
- **WSL-Mingw CUDA inputs:** `resolve_wsl_mingw_cuda_inputs()` resolves the
  optional `external_prerequisite_paths.cuda` root, then `CUDA_HOME`, then
  `CUDA_PATH`. It accepts NVCC only from `<root>/bin/nvcc`, checks that the
  executable stays under the selected SDK root, and reads its reported toolkit
  version. The registry maps CUDA 12.4 to the existing `gcc12` profile; the
  selected compatible ToolchainStore `CC` is the authoritative `-ccbin`.
  `CUDA_NVCC_CCBIN` is ignored for `linux-cross-mingw`; native Linux can use
  that explicit override or its own verified store/system fallback. When CUDA
  is enabled, target preparation runs `validate_cuda_ptx_compatibility()` with
  that resolved NVCC/`-ccbin`. Architecture is chosen from
  `cuda_compute_capability`, then `CUDA_COMPUTE_CAPABILITY`, or the fresh G1
  Windows state. Without an explicit architecture, all reported NVIDIA
  capabilities must be valid and their numeric minimum is the PTX baseline.
  There is no separate per-GPU selector: a G1 target override can supply an
  inventory. The probe validates PTX directives in an isolated temporary
  directory and never executes PTX. It retries only the CUDA 12.4 conflict
  between glibc `bits/mathcalls.h`
  and CUDA `crt/math_functions.h` for `cospi`, `sinpi`, or `rsqrt` (including
  float variants) with `-Xcompiler=-U_GNU_SOURCE`; unrelated errors are not
  retried and failed retries retain the original diagnostic. Architecture,
  compiler versions, effective flags, workaround state and diagnostics are
  recorded in the resolved inputs. Resolution and environment construction do
  not automatically add CUDA SDK libraries to Windows-target discovery.
  `wsl_mingw_environment.py` provides
  `filter_wsl_mingw_target_environment()`: it returns a child-environment copy,
  drops host-only SDK selectors and Vulkan loader configuration, and retains
  discovery paths only when they are under explicitly supplied Windows-target
  input roots. It reports removed entries with their variable, prerequisite
  classification, and reason; `PATH` remains available for host tools.
  `FFmpegBuilder` and the Linux platform strategy also avoid injecting Linux
  Vulkan SDK headers, libraries, pkg-config paths, or `VULKAN_SDK` into the
  `linux-cross-mingw` environment.
  G3 consumes this filtering primitive and constructs explicit target compiler,
  pkg-config, CMake, Meson, and Autotools inputs. Native Linux/WSL and macOS SDK
  wiring is unchanged.

- **WSL-Mingw preparation result:** TUI and `scripts/run_build.py` call the
  same `FFmpegBuilderApp.prepare_target()` path. It performs the fresh G1
  Windows probe and identity guard before resolving enabled CUDA/Vulkan
  prerequisites. Without an explicit architecture override, CUDA uses all
  reported NVIDIA devices and selects the minimum valid compute capability,
  so enumeration order cannot pick a GPU.
  It resolves the backend profile's required LLVM-MinGW and Rust toolchains
  from the project store, including the Rust target-std extra, and fails on
  missing or incompatible required profiles. `WslMingwPreparation` retains
  the typed Windows target, project-toolchain metadata, validated CUDA inputs,
  and validated Vulkan host tools in separate fields. Its effective
  build-input projection contains the managed toolchain profiles and paths,
  CUDA ccbin/profile, target
  architecture, effective PTX flags/workaround, and validated Vulkan
  capabilities; SDK roots, tool versions, resolution provenance and raw
  diagnostics remain diagnostic-only. `prepare_cross_environment()` consumes
  this result only after the guard selects the workspace and runs G3 pass-1
  validation without adding Linux SDK libraries to Windows-target discovery.
  Configure optional roots as:

  ```yaml
  target_backend: linux-cross-mingw
  external_prerequisite_paths:
    cuda: /path/to/cuda
    vulkan: /path/to/vulkan-sdk
  ```

  When omitted, CUDA uses `CUDA_HOME` then `CUDA_PATH`, and Vulkan uses
  `VULKAN_SDK`; a configured invalid root fails without environment fallback.
  Disabled CUDA/Vulkan capabilities do not resolve or require their SDKs.

  Live acceptance on one host selected `/opt/cuda-12.4/bin/nvcc` (12.4.99),
  project `gcc12` (12.5.0), and Windows TITAN V capability 7.0. The scoped retry
  produced 758 bytes of PTX with `.version 8.4` and `.target sm_70`; temporary
  files were removed. This is not a Windows runtime or GPU portability claim.

  Final G2.4/G2.5 acceptance (issue #16) repeated the full `prepare_target()`
  path in an isolated workspace using a fresh Windows probe: Windows 11 Pro
  for Workstations, Intel i9-7980XE, and NVIDIA TITAN V at compute capability
  7.0. CUDA resolved from `CUDA_HOME` to `/opt/cuda-12.4/bin/nvcc` 12.4.99,
  selected verified ToolchainStore `gcc12` 12.5.0 as explicit `-ccbin`, and
  passed PTX generation for `compute_70` with the scoped
  `-Xcompiler=-U_GNU_SOURCE` workaround. Vulkan resolved from `VULKAN_SDK` to
  `/opt/vulkan-sdk/1.4.363.1/x86_64`; `glslc` compiled the shader and
  `spirv-val` validated the SPIR-V. The shader probe did not access a Vulkan
  GPU or runtime.

  Temporary invalid/missing-root checks rejected absent CUDA/Vulkan SDKs,
  invalid explicit roots despite usable ambient SDK/PATH entries, selected
  CUDA roots without NVCC, selected Vulkan roots without required host tools,
  and invalid Vulkan layouts. A missing project GCC was classified as
  `toolchain_install`; missing Windows compute capability as `target_data`;
  prerequisite and PTX failures retain their separate
  `external_prerequisite` and `cuda_compatibility` categories. With CUDA and
  Vulkan explicitly disabled, preparation succeeded despite invalid SDK
  overrides; enabling either capability made its missing SDK fail preparation.
  The controlled contamination case removed 14 Linux SDK/runtime/discovery
  entries, retained host-tool `PATH`, excluded SDK library and metadata paths,
  and left its parent environment unchanged.

  Both `scripts/run_build.py`'s headless `run_build()` and the TUI
  `_run_build()` path reach the same successful preparation result, continue
  through identity-local G3 environment generation and real configure/link
  acceptance, then stop before components. The cross `--dry-run` additionally
  reports Wave-0 selection. This does not claim that either path can yet build
  a Windows target.

Isolated offline provisioning (does not touch the normal store):

```bash
python scripts/prepare_toolchains.py --backend linux-cross-mingw \
    --store /tmp/clean-store --sources /tmp/mirror      # network disabled by default
python scripts/prepare_toolchains.py --verify-only --store /tmp/clean-store
python scripts/prepare_toolchains.py --verify-only --profiles gcc12 \
    --backend linux-cross-mingw
```

### Build goal: host-tuned FFmpeg, not a portable release

Release portability is explicitly out of scope. The build goal is an FFmpeg
maximally tuned for the host it runs on (`-march=native -mtune=native
-fopenmp`, project default): release binaries may depend on the host's runtime
(`libgomp`, `libgcc_s`, system X11/Vulkan/VA-API libraries), and a fully static
release is not wanted because Vulkan filters do not work in it. The toolchain
store exists to *produce* such a build on hosts whose system compiler is out of
range (e.g. GCC 16-only host building the FFmpeg 8.1 profile that requires
`gcc >=12,<14`), not to be bundled into the release — a successful build pulls
nothing from the store at runtime (verified 2026-10-16: FFmpeg 8.1 built with
deployed gcc12 links workspace libraries statically and resolves
`libgomp`/`libgcc_s` to the host).

The project's converter uses FFmpeg 9.0 and 8.1 side by side (`workspace_90/`
and `workspace_81/`); the toolchain store replaced the earlier `nix shell`
approach, which broke that two-version scheme, with compilers built on the host
for the host.

**`ConfigManager`** — loads/saves YAML configuration:

- `load()` — reads `build_config.yaml`, returns `BuildConfig`
- `save(config)` — writes YAML
- `get()` — returns current config, loading from file if needed

### `state.py` — Build State Management

**`BuildState`** — dataclass representing the full build state:

```python
@dataclass
class BuildState:
    build_id: str               # UUID
    started_at: str             # ISO 8601
    config: dict
    components: dict[str, ComponentState]
    current_step: int
    total_steps: int
```

**`ComponentState`** — per-component state:

```python
@dataclass
class ComponentState:
    status: ComponentStatus     # pending/system/downloading/configuring/building/installing/completed/failed/skipped
    version: str
    built_at: str
    error_message: str
    log_file: str
```

**`StateManager`** — JSON persistence:

| Method | Description |
|--------|-------------|
| `load()` | Load state from `workspace/build_state.json` |
| `save()` | Persist current state to disk |
| `mark_component_status()` | Update component status, auto-save |
| `get_resume_point()` | Find first incomplete component |
| `is_component_completed()` | Check if component is already built (version match) |
| `update_progress()` | Update step counter |

### `platform_detect.py` — Environment Detection

Detects the full system environment in a single `detect_all()` call.

**Data classes:**

| Class | Fields |
|-------|--------|
| `SystemInfo` | os_name, os_version, architecture, cpu_model, cpu_cores, ram_gb |
| `PlatformInfo` | platform, build_backend, is_macos, is_linux, is_windows, is_arm64, is_wsl2, is_msys2, is_ucrt64, msystem, macports_clang, cuda_available, cuda_path, cuda_compute_capability, nvenc_api_version, nvenc_reason, vaapi_available, vaapi_reason, vaapi_detected_via, vaapi_detected_header_paths, vaapi_detected_loader_paths, vaapi_render_nodes, qsv_available, amf_available, amf_reason, amf_gpu_names, amf_headers_detected_paths, vulkan_available, vulkan_dev_available, vulkan_runtime_available, vulkan_reason, vulkan_detected_via, vulkan_detected_header_paths, vulkan_detected_icd_files, opencl_available, opencl_runtime_available, opencl_dev_available, opencl_effective_available, opencl_runtime_reason, opencl_dev_reason, opencl_effective_reason, opencl_detected_header_paths, opencl_detected_loader_paths, opencl_detected_icd_files, opencl_pkg_config_name, rocm_available, rocm_path, vulkan_sdk_available, vulkan_sdk_path |
| `ToolInfo` | name, path, version, available |

`is_msys2`, `is_ucrt64` and `msystem` are legacy fields of the unsupported MSYS2 Windows path; they remain only until migration to `linux-cross-mingw`.
**Detection methods:**

| Method | What it detects |
|--------|-----------------|
| `_detect_system_info()` | OS (reads `/etc/os-release` on Linux), CPU (`/proc/cpuinfo` or `sysctl`), RAM |
| `_detect_platform_info()` | Platform flags, HW acceleration |
| `_detect_cuda()` | nvcc in PATH, then `/usr/local/cuda*/bin/nvcc` |
| `_detect_nvenc_api_version()` | Query the NVIDIA driver independently of NVCC, to select compatible nv-codec headers |
| `_check_wsl2()` | WSL2 environment via `/proc/version` (Microsoft/WSL) |
| `_check_qsv()` | Linux: QSV via vainfo/PCI Intel GPU with VAAPI (legacy code also has a Windows UCRT64 oneVPL branch, slated for removal) |
| `_check_vulkan()` | Dev/runtime split: pkg-config or headers (incl. `VULKAN_SDK` on *every* platform, not just Windows) gate `vulkan_dev_available`/the returned bool; ICD JSON files and a real `vulkaninfo --summary` run are runtime-only diagnostics (`vulkan_runtime_available`) and no longer grant dev-availability by themselves |
| `_check_opencl()` | Runtime+dev readiness model (`opencl_runtime_available` + `opencl_dev_available`), including ROCm/CUDA non-standard include/lib paths, WSL-aware runtime checks, and a functional `clinfo -l` probe (PATH or `<ROCM_PATH>/bin/clinfo`) as the strongest runtime signal |
| `_check_vaapi()` | pkg-config libva, falling back to header (`va/va.h`) + loader (`libva.so`, multiarch-aware) presence when pkg-config/`.pc` indexing is unavailable; always records `vaapi_reason` on failure |
| `_check_amf_headers()` | Diagnostics-only: AMF SDK header paths on disk (does **not** gate `amf_available`) |
| `_detect_tools()` | 16 tools via `shutil.which()` + version extraction |

**WSL awareness:** `_check_wsl2()` detects WSL2 by checking `/proc/version` for "Microsoft" or "WSL". `_check_opencl()` detects WSL by checking `/usr/lib/wsl/lib/` and verifies that `libnvidia-opencl*` exists there. `_check_qsv()` disables QSV in WSL2 environments. `_check_vaapi()` explicitly disables VAAPI in WSL2 and records the reason. WSL2 does not expose OpenCL or QSV through its paravirtualized driver.

**AMF detection (Linux/Windows):** `amf_available` is decided purely by AMD GPU *presence* (`lspci -nn` / `/sys/class/drm` sysfs enumeration on Linux, WMIC/CIM `Win32_VideoController` on Windows) — **not** by AMF SDK header presence. This is intentional: the `amf` component always downloads its own headers from GPUOpen regardless of what may already be installed system-wide. `_check_amf_headers()` still exists and populates `amf_headers_detected_paths` purely for diagnostics/troubleshooting.

### `system_report.py` — Report Generation

**`SystemReport`** — aggregates all detected information:

- `get_hardware_acceleration_status()` — returns dict of HW accel availability
- `get_compiler_info()` — detects GCC/Clang/Macports Clang
- `get_missing_required_tools()` — tools required for build
- `get_optional_tools_status()` — optional tools (nasm, cmake, meson, etc.)
- `get_opencl_diagnostics()` — reports effective/runtime/dev OpenCL state with reasons and discovered paths
- `get_vaapi_diagnostics()` — reports VAAPI availability, detection method, and failure reason
- `get_amf_diagnostics()` — reports AMF availability, detected AMD GPU names, and failure reason
- `get_vulkan_diagnostics()` — reports dev/runtime Vulkan state, detection method, and failure reason
- `get_sdk_status()` — reports detected SDK roots (ROCm/CUDA/Vulkan SDK)

**`SystemReportGenerator`** — creates `SystemReport` from raw detection data, adds build environment variables (PATH, PKG_CONFIG_PATH, CFLAGS, etc.).

### `components.py` — Component Registry

**`Component`** — dataclass defining a single build component:

```python
@dataclass
class Component:
    name: str
    version: str
    url: str
    category: ComponentCategory
    build_system: BuildSystem
    configure_args: list[str]
    depends_on: list[str]
    requires_tools: list[str]
    gpl_only: bool
    non_gpl_only: bool
    linux_only: bool
    macos_only: bool
    windows_ucrt64_supported: bool  # legacy MSYS2 override; to be removed
    system_component: bool
    custom_build_fn: str
    ffmpeg_configure_flag: str
    platform_overrides: dict[str, PlatformOverride]
    # ... and more
```

**`BuildSystem`** enum: `AUTOTOOLS`, `CMAKE`, `MESON`, `CUSTOM`, `HEADERS_ONLY`, `MAKE_ONLY`, `CARGO`

**`ComponentRegistry`** — holds all ~50 components in build order:

| Method | Description |
|--------|-------------|
| `get_all()` | All components in build order |
| `get_buildable(gpl, platform, tools, ...)` | Filtered list based on config and platform |
| `get_ffmpeg_component(version)` | FFmpeg target resolved to its declared source profile |
| `get_nv_codec_component(platform_info=None)` | Resolve `nv-codec` through the declared `driver_nvenc_api_map` for the reported API major; use the registry default if detection is absent or the major is unmapped |
| `get_ffmpeg_configure_flags(built, gpl, platform, platform_info=None, ffmpeg_version="8.1")` | Collect version-compatible `--enable-*` flags from built components and platform readiness |

**HW acceleration filtering** in `get_buildable()`:

| Component | Condition |
|-----------|-----------|
| `nv-codec` | `platform_info.cuda_available` **or** a detected `nvenc_api_version`; the declared major-version map selects the header version |
| `vulkan-headers` | Selected independently of Vulkan runtime availability, subject to general component policy |
| `glslang` | `platform_info.vulkan_available` |
| `amf` | `platform_info.amf_available` |
| `opencl-headers`, `opencl-icd-loader` | `platform_info.opencl_available` **or** `platform_info.opencl_runtime_available` |
| `onevpl` | `platform_info.qsv_available` |

**NVENC driver API version detection** (`PlatformDetector._detect_nvenc_api_version()`): on native Linux and the legacy Windows path, queries `NvEncodeAPIGetMaxSupportedVersion()` from the installed driver's NVENC library independently of NVCC. `components.yaml` declares `13.0.19.1` as the default and maps API major `12` to `12.2.72.0` and `13` to `13.0.19.1`; there is no automatic search for the newest compatible version. An absent or unmapped API major leaves the default unchanged. For WSL-Mingw this native probe is disabled: the Windows interop probe currently records NVIDIA DLL file versions, not the maximum NVENC API, so target-compatible selection needs explicit implementation (see §5.7 of `windows_build_strategy.md`).

Legacy Windows/UCRT64 policy in the current code (to be removed/migrated; not a supported backend, and not to be extended):

- only `nv-codec`, `vulkan-headers`, `glslang`, `opencl-headers`, `opencl-icd-loader` are eligible in `HW_ACCEL`,
- these are enabled only in `MSYSTEM=UCRT64`,
- Linux-only acceleration paths (`VAAPI`, `onevpl`) are excluded on Windows,
- `nv-codec`, `opencl-headers`, `opencl-icd-loader` remain `linux_only` and are allowed on Windows only through the legacy `windows_ucrt64_supported` override.

WSL-Mingw (`linux-cross-mingw`) target inputs are declared separately through
`target_backends`; G2.8 reproduced and verified their staged closure offline.
G3 still needs to connect target eligibility to the production build graph.
Target-input declarations do not change native `get_buildable()` behavior.
See `windows_build_strategy.md` (G2.6/G2.8 and G3–G5).

### `builder.py` — Build Engine

**`FFmpegBuilder`** — orchestrates the build of each component and now delegates split responsibilities to internal helper modules:

- `build_types.py` — shared `BuildError` / `SkipComponent` exceptions
- `build_steps.py` — shared `_run_step` / `_run_make` / `_run_install` execution helpers
- `component_builders.py` — explicit custom-build dispatch registry keyed by `custom_build_fn`
- `builders/` — domain-specific custom-builder entry points and `ComponentBuildContext`
- `platforms/` — `BasePlatformStrategy` / `PlatformContext` and per-OS/toolchain strategy implementations selected by `PlatformStrategyResolver`
- `patches/` — declarative `SourcePatch` registry (applied post-extraction, pre-configure) with fail-fast assertions
- `release_bundle.py` — release directory creation and runtime dependency collection

**Environment setup** (`_setup_environment()`):

- Sets `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `LDEXEFLAGS`
- Configures `PKG_CONFIG_PATH` with workspace and system paths
- Adds CUDA paths (`-I`, `-L`, `PATH`) when CUDA is detected
- Adds Vulkan library linkage when Vulkan is detected
- Sets `CC`/`CXX` to macports clang on macOS

**Build dispatch** (`build_component()`):

```
1. Check if already completed (version match) → skip
2. Download and extract source
3. Apply matching source patches from the `patches/` registry
   (fail-fast BuildError if a patch anchor no longer matches)
4. If HEADERS_ONLY → install headers, return
5. If `custom_build_fn` → resolve through `builders.base.dispatch_component_build()` and call it, return
6. Dispatch by build_system:
  - AUTOTOOLS → `builders.base.build_autotools()`
  - CMAKE     → `builders.base.build_cmake()`
  - MESON     → `builders.base.build_meson()`
  - MAKE_ONLY → `builders.base.build_make_only()`
  - CARGO     → `builders.base.build_cargo()`
```

Note: patches whose target file is generated during configure (e.g. OpenSSL's
`configdata.pm`, created by `./Configure`) cannot run in the pre-configure
registry step and remain inline in the relevant custom builder.

**Custom build functions** are exposed through domain modules under `builders/` and dispatched through an explicit registry. Their compatibility signature remains `(builder, component, source_dir)`, while `builders/base.py` provides `ComponentBuildContext` and standard build-system entry points:

- `build_autotools`, `build_cmake`, `build_meson`, `build_make_only`, and `build_cargo`
- `install_headers_only`

The existing `FFmpegBuilder` methods remain the implementation target during this incremental split, preserving the public API while the domain logic is migrated module by module.

| Function | Component | Notes |
|----------|-----------|-------|
| `build_openssl()` | openssl | Uses `./Configure`, `make install_sw` |
| `build_x264()` | x264 | Adds `-fPIC`, `install-lib-static` |
| `build_x265()` | x265 | Multi-bitdepth (12/10/8), merges static libs with `ar -M` (Linux) or `glibtool` (macOS) |
| `build_libvpx()` | libvpx | Darwin Makefile patching for linker flags |
| `build_zimg()` | zimg | Runs `libtoolize -i`, `autogen.sh` before configure |
| `build_libvorbis()` | libvorbis | Patches `configure.ac`, runs `autogen.sh` |
| `build_libjxl()` | libjxl | Runs `./deps.sh`, cmake with many disabled features |
| `build_libvmaf()` | libvmaf | Meson build in `libvmaf/` subdirectory |
| `build_libzmq()` | libzmq | Patches `proxy.cpp` for C++ compatibility |
| `build_glslang()` | glslang | Runs `update_glslang_sources.py` first |
| `build_ninja()` | ninja | Bootstrap build with `./configure.py --bootstrap` |
| `build_ffmpeg()` | ffmpeg | Collects flags from all built components, adds CUDA/Vulkan flags |

**FFmpeg configure flags** added dynamically:

| Condition | Flags |
|-----------|-------|
| `gpl_enabled` | `--enable-gpl`, `--enable-nonfree` |
| CUDA/NVIDIA | Verified and permitted NVCC profile enables `--enable-cuda-nvcc`; NVENC/NVDEC use the driver and nv-codec headers independently. |
| Per built component | `--enable-libdav1d`, `--enable-libx264`, etc. |
| macOS | `--enable-videotoolbox` |
| OpenCL ready (`opencl_available` or runtime+dev ready, or OpenCL components built) | `--enable-opencl` |
| glslang with FFmpeg 8.1 | `--enable-libglslang` |
| glslang with FFmpeg 9.0 | omitted; the option was removed upstream |

**Release bundles** (`release_bundle.py`):

When `make_release: true`, `make_release_bundle()` writes a release directory at `<workspace>/release` after a successful build. It copies the built `ffmpeg`/`ffprobe`/`ffplay` binaries and recursively collects their **non-system runtime dependencies** (via `ldd` on Linux, `otool -L` on macOS, `objdump -p` on Windows) into the same flat directory, then writes a `manifest.json` recording the platform, build backend, FFmpeg version, copied binaries/dependencies, and any missing binaries or dependencies.

Packaging rule (final):

- A release copies **all non-system runtime dependencies** and is **autonomous within the system on which it was built**. System libraries — including glibc and the dynamic loader — are intentionally **not** copied and must be present on the host; a release is not a "portable to any distribution" bundle. This matches the "build on the host, for the host" goal and keeps releases compatible with AppImage-style packaging (which relies on the host C runtime).
- System paths excluded by `_is_system_runtime_library()`: `/lib`, `/lib64`, `/usr/lib`, `/usr/lib64` (Linux); `WINDIR` (Windows); `/usr/lib`, `/System/Library`, and dyld-shared-cache references (macOS, via `_is_system_dependency_reference()`).
- Non-system dependencies — workspace-built libraries and any libraries resolved from a local toolchain prefix — are copied into the release directory. A local-toolchain runtime (e.g. `libstdc++`/`libgomp`/`libgcc_s` under the toolchain prefix) is therefore bundled, while the host glibc is not.
- macOS releases are additionally made relocatable: bundled dylib install names are rewritten to `@rpath/<name>`, `@loader_path` is added as an rpath, and each modified Mach-O is re-signed ad-hoc (`_make_macos_bundle_relocatable()`). Linux/Windows releases copy dependencies flat next to the binaries.

### `executor.py` — Command Execution

**`CommandExecutor`** — wraps `subprocess.run()` with:

- Environment variable merging
- Working directory control
- Timeout support
- Stdin support (used for `ar -M` in x265 multi-bitdepth merge)
- Log file generation per step

**Key methods:**

| Method | Description |
|--------|-------------|
| `execute()` | Core execution, returns `ExecutionResult` |
| `execute_with_log()` | Execute + write log to `workspace/logs/<component>_<step>.log` |
| `execute_make()` | `make -j<N>` |
| `execute_install()` | `make install` |

**Legacy MSYS2 sh-wrapping (to be removed/migrated, not a supported backend)**: the
current `executor.py` still contains a branch that, on `MSYSTEM=UCRT64`, wraps commands
beginning with `./` through `sh.exe`. It belongs to the legacy MSYS2 Windows path being
replaced by WSL-Mingw (`linux-cross-mingw`), which must not reuse it. Do not extend it.

### `downloader.py` — Download Management

**`Downloader`** — downloads source archives with:

- Progress bar via `tqdm`
- Retry logic (3 attempts, 10s delay)
- Skip if file already exists and non-empty
- Atomic `<archive>.part → <archive>` rename to prevent partial files
- Per-file `threading.Lock` so concurrent callers do not race on the same archive

**`AsyncDownloadManager`** — runs `Downloader.download` in a `ThreadPoolExecutor`:

- `prefetch(components)` — schedules every buildable source archive up-front
- `get(component)` — blocks on the in-flight download for the current component, returns the archive path; other downloads keep running in the background
- `schedule(component)` / `retry(component)` — enqueue or replace a download (used when the user retries a failed component)
- `shutdown(wait)` — stops workers; `cancel_futures` is used on Python 3.9+ for non-blocking shutdown

`FFmpegBuilder` exposes this as `prefetch_downloads()`, `retry_download()`, and `shutdown_downloads()`. The build loop in `app.py` calls `prefetch_downloads()` once before the loop, `retry_download()` on retry, and `shutdown_downloads()` in a `finally` block.

### `ui/screens.py` and `ui/dashboard.py` — TUI Screens

Built with the `rich` library. Static screens use `Table`, `Panel`, and `Prompt`; the build screen uses `Live` with `BuildDashboard`.

| Screen | Class | Purpose |
|--------|-------|---------|
| System Report | `SystemReportScreen` | Hardware/software tables, HW accel status, config, letter-key menu |
| Component Info | `InfoScreen` | Paged buildable/all component list |
| Configuration | `ConfigScreen` | Interactive yes/no prompts for build settings |
| Build Dashboard | `BuildDashboard` | Live component rows, status colors, incremental row reveal, in-progress row pinning, message log |
| Final Report | `FinalReportScreen` | Summary, binaries, install prompt, error details |

`BuildDashboard` receives updates from `StateManager.status_listener`, per-component download progress callbacks (`update_download_progress`), and async download status callbacks. Worker threads only update the dashboard model under a lock; Rich rendering happens from the main Live refresh loop. Rows are revealed (`first_seen`) lazily as the first event for a component arrives — on a fresh build this means rows appear when `prefetch_downloads` queues each archive, on a resume the rows present in `state.components` are revealed eagerly so the prior progress is visible from the first frame. The viewport in `_visible_rows` always includes every in-progress row (download/configure/build/install) and fills the remaining height with rows around the active component, so the user always sees what is currently happening even on a 30-row terminal.

`mark_component_status` accepts a transient `detail` argument (not persisted to `workspace/build_state.json`) that is forwarded to the listener so the dashboard can show the running command (e.g. `make -j40`, `cmake`, `ninja -C build`, `cargo cinstall`) or live download progress (`12.3/45.7 MB (27%)`). When adding a new build helper, always pass `detail="…"` on every transition so the dashboard column stays meaningful.

### `ui/error_handler.py` — Error Handling

**`ErrorHandler`** — on `BuildError`:

1. Shows error panel with component name and message
2. Displays last 20 lines of the log file
3. Presents letter-key menu: Retry (`r`) / Skip (`s`) / Abort (`a`) / Show Full Log (`l`)
4. Returns user choice to the build loop in `app.py` after Live has been stopped

## Platform-Specific Notes

### Windows (WSL-Mingw) — implementation direction

The Windows build route is **WSL-Mingw**: a Windows host runs a WSL2 Ubuntu
build environment, which uses the LLVM-MinGW/UCRT cross toolchain to produce
a Windows FFmpeg target. **WSL-Mingw** is the user-facing name; the backend
identifier is **`linux-cross-mingw`**. G1, G2, and G3 are PASS; G4 Waves 0–4A
are accepted, including eleven Wave-2 target dependencies cross-built and
validated with the required primary OpenMP profile and six Wave-3A LV2
dependencies built through Meson, followed by the Ogg/Vorbis/Theora chain.
Wave 4C, the full component stack, and
FFmpeg integration remain **in progress**. The gates
G1–G7, verified early-PoC results and open issues are in
[`windows_build_strategy.md`](windows_build_strategy.md), which is the
authoritative plan. The previous MSYS2-based Windows build path was replaced
by this route.

Implementer rules:

- **Legacy MSYS2 code is migration/removal work, not a supported backend.**
  `platforms/windows_ucrt64.py`, the `is_msys2`/`is_ucrt64`/`msystem` fields,
  `windows_ucrt64_supported` registry overrides, the `./` sh.exe wrapping in
  `executor.py`, MSYS path rewriting in `builder.py`, and the MSYS2 bootstrap
  scripts still exist temporarily. Do not add features to them and do not
  require their behavior to be preserved; remove or migrate them when the
  relevant production gate reaches them.
- **Host tools and target inputs are separate.** Linux host tools (compilers,
  CMake, Meson, Ninja, pkg-config, NASM, Perl/Python, NVCC, shader tools) are
  distinct from Windows target inputs (headers, import libraries, target
  libraries, target pkg-config metadata). Never reuse MSYS path rewriting or
  `windows-ucrt64` path conventions in the Linux cross backend.
- **External SDKs belong to the prepared WSL environment.** The CUDA SDK
  (example `/opt/cuda-12.4`) and Vulkan SDK (example
  `/opt/vulkan-sdk/1.4.363.1`, Linux root `.../x86_64`) are prerequisites the
  project locates and checks for presence and usability. The project does not
  install, archive, hash or version-manage them, and ordinary SDK tool
  version changes must not require registry edits.
- **Project-managed inputs stay project responsibilities:** LLVM-MinGW, the
  Rust compiler and its Windows target standard library, component sources and
  patches, target headers (e.g. `nv-codec`), import-library recipes.
- **Target discovery must not consume Linux SDK libraries.** Target
  pkg-config, CMake and Meson lookups use target-only paths and explicit cross
  files; `/usr/lib`, WSL CUDA libraries and Vulkan SDK native libraries must
  never satisfy a Windows target link. Do not globally source LunarG
  `setup-env.sh` for cross-builds.
- **G3 pass-1 execution boundary:** application preparation materializes the
  G2.6 prefix, generates the target-only pkg-config wrapper, CMake toolchain,
  Meson cross file and Autotools projection, and runs isolated configure/link
  acceptance. It then stops before component building. Do not claim a complete
  Windows FFmpeg build until G4–G7 acceptance.
- **Windows runtime probing goes through Windows interop** (fresh probe of CPU
  features, GPU, drivers, Vulkan/OpenCL runtimes), never through WSL's Linux
  view. Drivers/runtimes (NVIDIA driver, Windows Vulkan loader/ICD, OpenCL
  dispatcher, UCRT) are owned by Windows and are not copied into WSL.
- **Host-specific rebuild behavior is intentional.** Builds target the
  physical host; state and artifacts must not be reused across materially
  different host/build conditions (see build identity in the strategy).

#### G2.6 — Windows target headers and link interfaces

The component registry's `target_backends` field is separate from host
eligibility. For `linux-cross-mingw`, the offline target-input stager consumes
the existing checksum-pinned `nv-codec`, `vulkan-headers`, and
`opencl-headers` sources; native Linux/WSL and macOS component selection is
unchanged. The OpenCL archive's `CL/` tree is staged into `include/CL`.
External CUDA/Vulkan SDKs remain host-tool prerequisites only; `/usr/include`,
SDK include/library paths, Linux loaders, host pkg-config/CMake metadata,
driver DLLs, and ICD implementations are not target inputs.

The link audit found NVENC/NVDEC and CUDA Driver API use dynamically resolved
runtime APIs, so neither gets an import library. The selected static libplacebo
link path has an undefined `vkGetInstanceProcAddr`, so G2.6 generates a
`vulkan-1.dll` interface for that symbol only, verifying the symbol against
the pinned Vulkan registry. FFmpeg's OpenCL path configures with `-lOpenCL`
and calls OpenCL functions directly; the `OpenCL.dll` interface is generated
from the pinned `opencl-icd-loader` source's Windows `.def` file, without
building or staging its loader. Both import libraries are target-only
x86-64 COFF generated by the installed LLVM-MinGW `dlltool`.

Run `python scripts/prepare_target_inputs.py --target-prefix <fresh-path>
--toolchain-root <installed-llvm-mingw>` to stage the declared inputs offline.
The output contains only target headers, the two proven import libraries, and
`target-inputs.json`; its source and file hashes, backend, and interface recipe
identities feed the G2.7 semantic build identity. G2.6 does
not construct the final cross compiler environment, target pkg-config/CMake/
Meson configuration, full target dependency prefix, or FFmpeg build. The
isolated acceptance compiled representative NVENC/NVDEC/CUDA, Vulkan, and
OpenCL headers with LLVM-MinGW and linked Vulkan/OpenCL smoke programs; the
staged tree passed contamination inspection.

#### G2.7 — semantic material-input identity

The WSL-Mingw `prepare_target()` sequence is: fresh G1 Windows target probe;
resolve required project toolchains and selected external prerequisites;
validate effective CUDA inputs and configured capabilities; materialise and
verify G2.6 target headers/interfaces in a disposable prefix; construct the
complete identity; then compare saved state and select the identity workspace.
TUI and headless builds use this same path. Native backends do not require a
Windows probe, LLVM-MinGW, Rust target std, CUDA policy, or Windows target
inputs.

Identity schema 2 persists deterministic, structured evidence. SHA-256 over
canonical build JSON supplies the full digest and the first 16 hex characters
form the workspace key. The target-input manifest (including its file hashes)
is retained and revalidated against its materialisation ID; generated
interface outputs must agree with manifest hashes. Large file-set differences
are summarized in mismatch output. G1-only/incomplete WSL-Mingw identities are
legacy and cannot authorize reuse; native schema-1 identity remains readable.

Material fields include backend and configured capabilities; LLVM-MinGW and
Rust source/version provenance; the Rust `x86_64-pc-windows-gnullvm` std
identity; selected project GCC `-ccbin` provenance/role; effective CUDA
architecture and compatibility workaround; external NVCC/toolkit release;
pinned NVIDIA/Vulkan/OpenCL headers; generated Vulkan/OpenCL interface
recipes/output hashes; target-input materialisation ID; and the selected
shader compiler version. CUDA/Vulkan SDK roots, the temporary target prefix,
SPIR-V validator version, and detailed probe dumps are diagnostic-only.
External SDK trees and Windows runtime DLLs are never hashed or copied into
project provenance.

Same semantic inputs permit reuse. A material change selects an isolated
workspace and leaves old artifacts untouched; runtime-only G1 drift invalidates
acceptance, and diagnostic-only changes do not prevent reuse. Live acceptance
covered fresh/same preparation, material and diagnostic drift, and rejection
of G1-only saved state on the physical Windows/WSL host. This establishes
identity safety, not a production component cross-build; G2.8 final acceptance
is recorded below. G3 pass 2 acceptance is recorded in the module reference.

#### G2.8 — final offline closure and live prerequisites (PASS)

Production registries resolved the complete project-owned G2 source closure:
LLVM-MinGW; GCC 12.5.0 with GMP, MPFR, and MPC bootstrap sources; Rust 1.99.0
with matching `x86_64-pc-windows-gnullvm` target std; nv-codec,
Vulkan-Headers, OpenCL-Headers, and the pinned OpenCL ICD Loader definition
source. All 11 archives were present in the local mirror and matched their
declared checksums. A fresh, network-disabled ToolchainStore provisioned and
verified the six required profiles; a second run reused them without changing
the GCC build log.

Two independent offline target-input stages had the same materialisation ID
and generated-interface hashes. LLVM-MinGW compiled representative target
headers and linked both Windows import interfaces; format and contamination
checks passed. Missing/corrupt managed sources retain project-source error
classification. Production `prepare_target()` then passed twice with fresh
Windows probes, external CUDA 12.4.99 using the clean-store GCC 12.5.0 ccbin
and `compute_70` PTX, and external Vulkan shader/SPIR-V tools. The schema-2
identity digest/key were stable and the second preparation resumed the same
isolated workspace. External SDK/runtime trees remain outside project-owned
provenance and target inputs. No Windows FFmpeg cross-build was performed.

#### Reusing the native CUDA/Vulkan work for WSL-Mingw

Commit `447b555` provides reusable build-input mechanisms, not a completed
Windows component build. Native FFmpeg 8.1 and 9.0 builds used the shared CUDA
profile and matched Vulkan headers/XML; the separate Windows G0A.5 PoC and
G2.4/G2.5 preparation do not validate Windows libvmaf CUDA or a complete
Windows build/resume workflow. The authoritative gate requirements and acceptance cases are in
[`windows_build_strategy.md`](windows_build_strategy.md), §5.7.

| Area | Existing mechanism | Windows adaptation still required |
|------|--------------------|-----------------------------------|
| CUDA selection | `FFmpegBuilderApp.prepare_target()` uses `resolve_wsl_mingw_cuda_inputs()` and `validate_cuda_ptx_compatibility()`; `ResolvedCudaBuildHostInputs.common_flags()` separates common flags from PTX output options. | Preserve explicit SDK selection and store-managed `-ccbin`; use the config/environment architecture override or fresh Windows target data, not WSL GPU state. Ignore the ambient `CUDA_NVCC_CCBIN` and do not use the native system-GCC fallback. |
| Output modes | Native `validate_cuda_output_modes()` checks Linux host-object and fatbin generation; `builders/graphics/vmaf.py` uses the selected NVCC and `bin2c`. | PTX success alone is not fatbin acceptance. Test Meson's CUDA compiler check and the libvmaf fatbin → host `bin2c` → target C compilation path; never link a Linux object into Windows. Check all upstream and injected `-gencode` flags, not only the selected baseline. |
| Environment | `wsl_mingw_environment.filter_wsl_mingw_target_environment()` filters selected host SDK variables and discovery paths; it is not a complete target environment. | Do not apply native `cuda_profile_environment()` wholesale to target discovery: it reintroduces SDK selectors. The filter does not remove `CUDACXX` or sanitize `PATH`; pass selected executables in a host-tool scope and construct isolated target pkg-config/CMake/Meson inputs. |
| NVIDIA headers | `components.py` maps declared NVENC API majors; `_libvmaf_cuda_headers()` and `_link_nv_codec_headers_into_source()` keep libvmaf's CUDA headers separate from driver-compatible FFmpeg headers. | Obtain or explicitly specify Windows target API compatibility; the G1 probe currently records DLL file versions, not `NvEncodeAPIGetMaxSupportedVersion()`. Keep both archive identities, verify target includes and runtime entry points, and do not silently drop requested NVENC/NVDEC when hardware is absent. |
| Vulkan inputs | `builders/graphics/vulkan_headers.py` verifies project headers and matching registry; `builders/graphics/placebo.py` stages the pair and passes `-Dvulkan-registry`. | Use a verified pair in the isolated Windows target prefix and matching bundled dependency; keep Linux SDK shader tools host-only, target glslang/import interfaces target-only, and the Windows loader/ICD external. |
| Resume | G2.7 schema-2 identity combines fresh G1 target facts with resolved project toolchains, effective CUDA, configured capabilities, shader compiler version, and verified G2.6 manifest evidence before the guard selects a workspace. | Same semantic inputs reuse state; material drift selects an isolated workspace; diagnostic-only drift does not block reuse. Legacy/incomplete WSL-Mingw identity is rejected. External SDK roots and the SDK tree remain diagnostic/unhashed; the reported NVCC release and artifact-producing shader compiler version are external material fields. |

Current cross preparation enables the CUDA prerequisite when
`cuda_build_mode: required`, or in `auto` when libvmaf CUDA is requested; in
either case the selected SDK/PTX prerequisite is required. `disabled` skips
it. Independent NVENC/NVDEC eligibility is a required future policy, not yet
an implemented Windows feature matrix. A passed PTX probe or
`libvmaf_cuda_supported` flag is not proof of a Windows fatbin build. G3–G5
must preserve explicitly required features, validate nonfree consent, and
report build presence separately from on-device runtime acceptance.

Legacy MSYS2 behavior is retained here only so that implementers recognize
the code to be removed. Its former setup, troubleshooting and path-format
documentation has been removed; for the historical record see
`docs/CHANGELOG.md`.

### Linux (native) — verified hwaccel reference build

Confirmed end-to-end (58/58 components, 0 failures) on:

- **Host:** Ubuntu 24.04.4 LTS ("noble"), kernel `7.0.0-29-generic` (24.04 HWE), x86_64.
- **GPUs:** 2× AMD Radeon Pro VII / Radeon Instinct MI50 (Vega 20). ROCm 6.3.0 at
  `/opt/rocm`. Mesa 25.2.8 with system Vulkan-Loader 1.3.275 (Wayland session);
  a LunarG Vulkan SDK 1.4.357.1 was present on disk but not activated
  (`VULKAN_SDK` unset) for this build — see "Dual Vulkan environments" below.
  `libva-dev`/`libva2` 2.20.0 installed for VAAPI.
- **Result:** `--enable-vaapi --enable-amf --enable-vulkan --enable-libglslang
  --enable-libplacebo --enable-opencl`, and correctly `--disable-ffnvcodec` with no
  `--enable-libvpl` (no NVIDIA/Intel GPU present). See `docs/CHANGELOG.md` →
  "Verified — successful end-to-end build (2026-08-20)" for the full component/flag
  breakdown and cross-check against `components.yaml`/`build_config.yaml`.

#### Dual Vulkan environments (system + LunarG SDK)

Linux setups commonly have two independent Vulkan "environments" available side by
side: the distro-packaged loader/drivers (e.g. `mesa-vulkan-drivers` +
`libvulkan-dev`) and a LunarG SDK tarball activated by sourcing its
`setup-env.sh`, which sets `VULKAN_SDK` (and `PATH`/`LD_LIBRARY_PATH`/
`VK_LAYER_PATH`). `_check_vulkan()` in `platform_detect.py` now honours
`VULKAN_SDK` on **every** platform (previously Windows-only — see CHANGELOG), and
`builder.py::_setup_environment()` propagates `$VULKAN_SDK/include`,
`$VULKAN_SDK/lib`, and `$VULKAN_SDK/lib/pkgconfig` into the build environment
when a LunarG SDK is active, in addition to the system Vulkan loader.

Vulkan-Headers is installed from the registry component with CMake, including
`include/vulkan`, `include/vk_video`, and `share/vulkan/registry/vk.xml`.
`builders/graphics/vulkan_headers.py` verifies these files against their
source and records a version, archive checksum, and content digest in
`share/vulkan/headers-manifest.json`. libplacebo stages that verified pair in
its supported bundled Vulkan-Headers dependency and passes the workspace XML
through Meson's `-Dvulkan-registry` option. Thus an active SDK cannot supply a
different XML for those headers. The loader and driver remain external.

On resume, when Vulkan-Headers is selected, a missing or invalid manifest
marks completed Vulkan-Headers, libplacebo, and FFmpeg for rebuilding before
the resume point is chosen.
An existing, conflicting bundled header tree produces a build error rather
than silently mixing versions. This check does not pin the system loader or
guarantee compatibility with every future SDK or driver. The existing
glslang-search adjustment and pkg-config normalization remain separate
builder adaptations; the header/registry fix does not eliminate them.

#### AMD hwaccel detection specifics (VAAPI / AMF / OpenCL via ROCm)

- **VAAPI** requires the `libva-dev`/`libva-devel` *development* package, not just
  the runtime (`libva2`/`libva-drm2`/etc.) — a system with only the runtime
  installed will correctly and explainably report VAAPI as unavailable via
  `platform_info.vaapi_reason` (e.g. "libva development headers not found").
- **AMF** is gated purely on AMD GPU presence (`lspci -nn` / `/sys/class/drm`
  sysfs), not on AMF SDK headers being pre-installed — the `amf` component always
  downloads its own headers from GPUOpen. Multi-GPU AMD systems (e.g. 2× Vega 20)
  report all detected AMD GPU names in `platform_info.amf_gpu_names`.
- **OpenCL via ROCm** is detected through `/opt/rocm/include/CL/cl.h` +
  `/opt/rocm/lib*/libOpenCL.so*` header/loader paths, and additionally verified
  with a live `clinfo -l` functional probe — using `<ROCM_PATH>/bin/clinfo` when
  ROCm ships its own `clinfo` outside `PATH` (the common case), rather than
  relying on filesystem presence alone.

### Fedora 44+ / modern GCC (≥15) + CUDA 12.2 — nvcc host-compiler compatibility

The original failure was observed on Fedora 44. The profile below applies to
native Linux and Linux builds inside WSL2, regardless of the selected
platform strategy; it does not describe the experimental WSL-Mingw backend.

Native build preparation resolves a CUDA Toolkit and host compiler before
selecting a resume point. An explicit `CUDA_NVCC_CCBIN` is authoritative;
otherwise it tries the verified store profile mapped to the CUDA release and
then system GCC for eligible mapping or probe failures. The architecture
comes from `cuda_compute_capability`, `CUDA_COMPUTE_CAPABILITY`, or detected
GPUs, in that order; there is no fixed fallback. PTX, object, and fatbin
probes verify the selected compute architecture. A narrowly
recognized CUDA 12.4/glibc declaration conflict permits one retry with
`-Xcompiler=-U_GNU_SOURCE`; other errors are preserved.

The resulting `ResolvedCudaBuildHostInputs` supplies the exact NVCC path and
common flags to libvmaf's Meson/Ninja targets and FFmpeg's `--nvccflags`.
Output-mode flags such as `--ptx` are not forwarded to fatbin builds. CUDA
probe and build environments discard inherited NVCC prepend/append flags so
they cannot silently change the tested profile; libvmaf's `bin2c` is taken
from the selected Toolkit. CUDA profile changes invalidate completed
libvmaf/FFmpeg statuses; their mutable build trees are reconfigured or
cleaned when the recorded profile differs.
NVENC/NVDEC do not depend on NVCC; unverified LLVM compilation is disabled.
When the driver selects an older nv-codec release, libvmaf reads CUDA driver
headers from the registry's default, SHA-verified nv-codec archive in its
own source tree. This leaves the installed driver-compatible NVENC headers
unchanged. Both versions are included in resume identity.

`cuda_build_mode` is `auto`, `required`, or `disabled`; the latter two
explicitly request or disable CUDA compilation. `cuda_compute_capability`
overrides GPU detection for builds without a GPU. FFmpeg NVCC requires
`allow_nonfree_cuda` or the existing `gpl_enabled` setting, both of which
enable FFmpeg's nonfree mode. Without that permission, `auto` may build
libvmaf with CUDA while leaving FFmpeg NVCC disabled. `auto` reports profile
failures; `required` blocks preparation, including when nonfree permission is
missing. WSL-Mingw cross preparation remains experimental and currently
validates PTX only. The profile still uses the host libc rather than a pinned
sysroot, so it cannot make arbitrary future Toolkit/libc combinations
compatible.

### libvmaf + nv-codec-headers CUDA compatibility

libvmaf's CUDA feature extractors use `ffnvcodec` driver headers, as does
FFmpeg's NVENC/NVDEC support. `components.yaml` declares `nv-codec` before
`libvmaf` because the registry preserves declaration order. The default
`nv-codec` 13.0.19.1 includes CUDA functions needed by libvmaf while retaining
the NVENC 13.0 interface required by FFmpeg 8.1. The registry can select an
older release for an older detected NVENC driver.

That older release can lack CUDA driver functions required by libvmaf.
`_libvmaf_cuda_headers()` therefore uses the installed workspace headers
when the selected release matches the default; otherwise it obtains the
default registry archive with its checksum and extracts the CUDA headers
into libvmaf's private `.ffmpeg-builder-cuda-headers` directory. Offline-first
download policy still applies. FFmpeg keeps the driver-compatible workspace
headers; the two versions and checksums are recorded in CUDA resume identity.

The source archive omits libvmaf's `ffnvcodec` submodule. Its Meson fatbin
custom target hardcodes `-I ../include` and does not inherit the workspace
CFLAGS, so `_link_nv_codec_headers_into_source()` links the chosen headers
at `<libvmaf_dir>/include/ffnvcodec`. For private headers, the builder also
adds their parent to CFLAGS for Meson's header checks. An unrelated existing
directory or link is not overwritten. The builder enables
`-Denable_cuda=true` only when requested and the shared profile passed.

Native FFmpeg 8.1 and 9.0 builds have completed with CUDA-enabled libvmaf;
their binaries list `libvmaf_cuda`. A separate test with forced older NVENC
headers verified that libvmaf can use the default CUDA headers without
replacing those installed for FFmpeg. This did not test a physical older
NVIDIA driver.

### Known issue: libvmaf uncontrolled memory growth on GCC 16 (RESOLVED, 2026-09-11)

**Status**: resolved. Root-caused and fixed by vendoring an upstream patch.

A `libvmaf` 3.2.0 binary built with **GCC 16** (Fedora 44) showed uncontrolled
memory growth at runtime. Cross-toolchain comparison at the time:

| Toolchain | Environment | Reproduces? |
|---|---|---|
| GCC 16 | Fedora 44 | Yes |
| GCC 15 (presumed, same modern-toolchain family; not independently confirmed) | — | Presumed yes |
| GCC 12/13 | Ubuntu 24.04.4 | No |
| Clang 17 | macOS | No |

The GCC-16-only reproduction initially suggested a toolchain-specific
codegen defect. Reproducing the leak in a `libvmaf_debug_build`
(`--buildtype=debug -Doptimization=0`, added to `build_libvmaf()` in
`builders/graphics/vmaf.py` for this investigation) ruled out an
optimization artifact: the leak was identical with `-O3` and `-O0`, pointing
at a genuine logic bug rather than something GCC's optimizer introduced.

**Root cause (found by inspecting the upstream `Netflix/vmaf` git history
directly)**: `libvmaf`'s internal thread pool
(`libvmaf/src/thread_pool.c::vmaf_thread_pool_enqueue()`) had **no queue-depth
cap** in the `v3.2.0` release (and `v3.0.0`, also previously vendored here).
Each queued job (`libvmaf/src/libvmaf.c::threaded_read_pictures_batch()`)
pins full decoded `ref`/`dist`/`prev_ref`/`prev_prev_ref` picture buffers via
`vmaf_picture_ref()` until a worker thread drains it. With no bound on queue
depth, a producer (frame decode/filter pipeline) that outruns the
feature-extractor worker threads can queue unboundedly many frames, each
retaining tens of MB (4K YUV) — this is a genuine, timing-dependent design
bug, not a GCC-only defect. GCC 16 (vs. GCC 12/13/Clang 17) most likely just
produces a decode/filter pipeline fast enough relative to feature-extraction
to expose the unbounded queue growth consistently, while other toolchains'
relative producer/consumer speeds happened to keep the queue shallow enough
not to matter in practice.

**Upstream fix**: Netflix/vmaf commit `8fc71e30` ("libvmaf/thread_pool: fix
thread pool queue depth to restore backpressure", also on branch
`thread_pool_backpressure` as `cff37b57`) adds a `queue.depth` counter and a
`not_full` condition variable, blocking `vmaf_thread_pool_enqueue()` once
`depth >= n_threads`. This commit lands 13 commits after the `v3.2.0` tag;
no tagged release contains it yet.

**Fix applied in this project**: `components.yaml`'s `libvmaf` entry now
vendors `libvmaf` built directly from commit `8fc71e30` instead of the
`v3.2.0`/`v3.0.0` release tarballs — archived as
`third_party/sources/vmaf-3.2.0-8fc71e30.tar.gz` (`version: 3.2.0-8fc71e30`),
generated via `git archive --prefix=vmaf-3.2.0-8fc71e30/ 8fc71e30` from a
clone of the upstream repository, matching the layout the extractor expects
(single top-level directory, stripped by the default
`archive_strip_components: 1`). `libvmaf_debug_build` was reverted to
`false` (normal optimized `--buildtype=release`) once the fix was confirmed
independent of optimization level.

**Verification**: real end-to-end VMAF computation (`ffmpeg`'s `libvmaf`
filter, real 4K source pair, GCC 16, both `n_threads=1` and `n_threads=8`)
now holds flat, bounded RSS (plateaus rather than growing) for the full
run duration, compared to ~1.2 GB/s unbounded growth with the un-patched
`v3.2.0` source under identical conditions. A full run now completes and
reports a VMAF score instead of failing with `Cannot allocate memory`.

No toolchain restriction remains: `libvmaf` is safe to enable
(`enable_libvmaf: true`) on GCC 15/16 hosts as on GCC 12/13 and Clang.

### Host-built compilers replace the isolated Nix environment (3.0)

The `shell.nix` isolated environment (`pkgs.gcc13Stdenv`-based) is removed.
Its role — providing a compatible compiler on hosts whose system GCC is out
of range — is now served by the built-in toolchain store (see "Toolchain store
and provisioning" above): compilers are built on the host for the host from
pinned Git LFS archives, resolved per `(ffmpeg_version, platform)` profile,
and verified after installation. The pre-C23 compatibility fixes previously
carried by the Nix shell notes (e.g. `-std=gnu11` for cargo C dependencies)
now live as registry data (`configure_args`) and automatic platform handling
in `builders/base.py`.

### Intel oneAPI ICX toolchain (experimental, Linux PoC)

Status: **evaluation only**. ICX is not a configurator/toolchain-registry
option; it is driven by the throw-away harness `scripts/icx_poc.py`. FFmpeg 9.0
(GPL, native, OpenMP) builds end-to-end with `icx`/`icpx` (58 components) and
produced results identical to the gcc 8.1 baseline on the HQ_converter
segmentator/QP validation, but it offers no advantage over FFmpeg 8.1 + gcc 12,
so it is not promoted. Full history, all problems and fixes:
[`intel-icx-build.md`](intel-icx-build.md).

**Where the code lives**

| What | Where |
|------|-------|
| Harness (oneAPI resolve, per-component `toolchain_env`, ICX baseline flags) | `scripts/icx_poc.py` |
| x264 stack-alignment fix (gated on `CC=icx/icpx`) | `patches/icx_compat.py`, `patches/registry.py` |
| x265 `IntelLLVM` compiler-ID fix (`-DINTEL_CXX=1`) | `builders/codecs/x265.py` |
| libplacebo glslang `lib64` lookup | `builders/graphics/placebo.py` |
| FFmpeg uses component-scoped `--extra-cflags` | `builders/ffmpeg/ffmpeg.py` |
| Relocatable release (generic, all toolchains) | `release_bundle.py` |

**Harness baseline flags** (in-memory per-component overrides, not committed
as defaults): `-D_GNU_SOURCE -ffp-model=precise -Wno-recommended-option`,
plus `-Wno-register` for C++; `MAKEFLAGS=LD=<icx>` for libvpx.

**Relocatable release.** The release step (Linux) copies `ffmpeg`, `ffprobe`,
`ffplay`, the optional `x265`/`SvtAv1EncApp`, and every non-system shared
library they need (for ICX: `libiomp5.so`, `libirc.so`, `libsvml.so`,
`libintlc.so.5`) into `<workspace>/release/`. It then runs
`patchelf --set-rpath '$ORIGIN'` on **every** bundled ELF (RUNPATH is not
inherited by a dependency's own NEEDED lookups, e.g. `libsvml -> libintlc`) and
verifies with `LD_LIBRARY_PATH` scrubbed that nothing resolves outside the
release or via an absolute RPATH. `patchelf` is a Linux dependency in
`pyproject.toml`. Intel oneAPI is then only a build toolchain; the release
runs without it. The OpenMP runtime is Intel `libiomp5` (no `libgomp`).

**Windows (future).** ICX has no Linux-to-Windows cross-compilation. Native
Windows ICX uses the MSVC ABI (Visual Studio, Windows SDK, `lib.exe`, FFmpeg
`--toolchain=msvc`), not MinGW; MinGW-built static libraries cannot be mixed in.
It is therefore not part of the WSL-Mingw (`linux-cross-mingw`) route; it
would mean moving the whole Windows dependency stack to MSVC-ABI, which is a
separate project (Variant 3 in `windows_build_strategy.md`, not selected).
The release relocation logic above is ELF/Linux only.

## Data Flow

### Build Process


```
app.run()
  |
  +-> detect_environment()
  |     platform_detect.detect_all() -> SystemInfo, PlatformInfo, tools
  |     system_report.generate() -> SystemReport
  |
  +-> load_state()
  |     state_manager.load() -> BuildState (or None)
  |
  +-> show_system_report() -> action
  |
  +-> _run_build(config, resume)
        |
        +-> registry.get_buildable(gpl, platform, tools, ..., platform_info)
        |     -> filtered component list
        |
        +-> FFmpegBuilder(config, workspace, packages, state_mgr, platform_detect)
        |     _setup_environment() -> CFLAGS, LDFLAGS, env
        |
        +-> builder.prefetch_downloads(components)
        |     AsyncDownloadManager queues every buildable source archive
        |
        +-> for each component:
              |
              +-> builder.build_component(component)
              |     |
              |     +-> is_component_completed() -> skip?
              |     +-> async_download_manager.get(component) -> archive
              |     +-> dispatch to build system handler
              |     +-> mark_component_status(COMPLETED)
              |
              +-> on BuildError:
                    error_handler.handle_error() -> retry/skip/abort
                    on retry: builder.retry_download(component)
        finally:
            builder.shutdown_downloads(wait=False)
```

### State Persistence

```
BuildState (in memory)
    |
    +-> state_manager.save() -> workspace/build_state.json
    |
    +-> state_manager.load() <- workspace/build_state.json
```

State is saved after every component status change and progress update.

## Extension Points

### Adding a New Component

1. Add a `Component()` entry in the appropriate `_add_*()` method in `components.py`
2. Set `build_system`, `url`, `version`, `configure_args`
3. If non-standard build, set `custom_build_fn` and implement the method in `builder.py`
4. Set `ffmpeg_configure_flag` if the component adds an `--enable-*` flag to FFmpeg
5. Set `depends_on` for dependency ordering
6. Set `gpl_only`, `linux_only`, etc. for filtering

### Adding a New Build System

1. Add a new value to the `BuildSystem` enum in `components.py`
2. Implement a `_build_<system>()` method in `builder.py`
3. Add dispatch case in `build_component()`

### Adding Platform-Specific Behavior

1. Add detection in `platform_detect.py`
2. Add field to `PlatformInfo` dataclass
3. Use in `builder.py` `_setup_environment()` or custom build functions
4. Add filtering in `components.py` `get_buildable()` if needed

## Testing

For implementation changes, use the project virtual environment; do not run bare
`pytest`, which can collect extracted third-party packages under `workspace/`:

```bash
source .venv/bin/activate
python -m pytest tests/ -q
black --check .
python scripts/check_mypy_baseline.py
```

Run the environment check:

```bash
./scripts/check_python_env.sh
```

Verify detection:

```bash
python3 -c "
from ffmpeg_builder.platform_detect import PlatformDetector
d = PlatformDetector()
sys_info, plat_info, tools = d.detect_all()
print(f'OS: {sys_info.os_name}')
print(f'CUDA: {plat_info.cuda_available}')
print(f'Vulkan: {plat_info.vulkan_available}')
print(f'OpenCL: {plat_info.opencl_available}')
"
```

Verify component filtering:

```bash
python3 -c "
from ffmpeg_builder.platform_detect import PlatformDetector
from ffmpeg_builder.components import ComponentRegistry
d = PlatformDetector()
_, pi, tools = d.detect_all()
r = ComponentRegistry()
components = r.get_buildable(False, 'linux', tools, platform_info=pi)
print(f'Total buildable: {len(components)}')
hw = [c.name for c in components if c.category.value == 'hw_accel']
print(f'HW components: {hw}')
"
```

`tests/test_platform_detect.py` exercises the real `_check_vaapi`/`_check_vulkan`/
`_check_opencl`/AMF-gating functions directly via `monkeypatch` (not hand-built
`PlatformInfo` mocks, which is all `test_components.py`/`test_builder_split.py`
cover), plus a `TestRealHardware` smoke test that runs `detect_all()` against
whatever machine executes the suite and asserts on it when AMD/ROCm hardware is
detected (auto-skips otherwise). Run with `pytest tests/test_platform_detect.py -v`.

## Known Limitations

- **WSL2 OpenCL**: Not available through the paravirtualized NVIDIA driver. The builder correctly detects this and excludes OpenCL components.
- **macOS CI**: No automated testing on macOS; platform-specific code paths (macports clang, glibtool, VideoToolbox) require manual verification.
- **Component versions**: Versions are defined in `components.yaml` (externalized from Python). A future improvement could fetch latest versions from an API or config file.
- **No dependency graph**: Components are built in a fixed order defined in `components.yaml`. There is no automatic topological sort based on `depends_on`.
- **Large builder surface still in one module** — `builder.py` remains the main integration point and still contains many component-specific build methods. Shared plumbing has already been split to `build_steps.py`, `component_builders.py`, and `release_bundle.py`; `platforms/` and `patches/` now carry the strategy and source-patch layers, and `builders/` provides the dispatch boundary. Further decomposition should continue in small, behavior-preserving steps.
- **Hardware validation is not exhaustive** — Native Linux CUDA 12.4 with a TITAN V has been exercised, including CUDA-enabled libvmaf and FFmpeg builds. Other GPU, driver, Toolkit, libc, and cross-build combinations need their own validation; the checked CUDA profile is not a complete sysroot.

## Code Review & Refactoring Status (2026-08)

August 2026 hardening work is now reflected directly in the codebase and changelog:

- high-severity findings (H1-H3) resolved,
- medium-severity findings (M1-M11) resolved,
- docs synchronized with current runtime behavior (interactive CLI-only launch, environment-specific local config, and optional archive checksum verification).

See also:

- `docs/CHANGELOG.md` — detailed chronological change log
- `changelog.md` (repository root) — build-session notes and environment-specific debugging history
