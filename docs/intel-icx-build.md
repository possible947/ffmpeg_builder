# Building FFmpeg with the Intel oneAPI DPC++/C++ Compiler (icx/icpx)

## Objective

Evaluate whether the project's FFmpeg stack (FFmpeg 9.0, GPL profile, host-tuned,
OpenMP-enabled) can be built end-to-end with the Intel oneAPI compiler
(`CC=icx`, `CXX=icpx`, Intel/LLVM OpenMP runtime) instead of GCC, reusing the
existing build system without breaking the established GCC profiles.

Motivation:

- Replace the `libgomp`/winpthreads OpenMP chain with the Intel OpenMP runtime
  (`libiomp5`).
- Keep every dependency of the project's real component set building with the
  same compiler (single OpenMP runtime per binary).
- Stay within the current toolchain abstraction (`materialize` /
  `component_env` / per-component `CC`/`CXX` injection), extending it only
  where a hard compiler assumption blocks a new toolchain.

Scope: Linux native build on this host (Intel Xeon Gold 6248, Fedora).
No CUDA/NVENC on this machine, so the nvcc host-compiler question is out of
scope for this run. Windows (native ICX vs MSYS2-UCRT GCC, cross-build) is
discussed separately below and was not built here.

## Environment

- Intel oneAPI 2026.1.1 (`/opt/intel/oneapi`, `compiler/latest/bin/icx|icpx`).
  oneAPI 2026.x compilers are clang-20 based.
- Host GCC 16 (system compiler; used only as the control baseline).
- NASM/YASM built as project components (external assemblers; the C compiler
  does not assemble).
- oneAPI runtime library directories exported for configure/link checks and
  for running the built binaries (`LD_LIBRARY_PATH`/`LIBRARY_PATH`).

## Method

A throw-away evaluation harness, `scripts/icx_poc.py`, drives the standard
`FFmpegBuilder` with a per-component `toolchain_env`:

- `PATH_PREPEND=<oneapi>/compiler/latest/bin`, `CC=<...>/icx`, `CXX=<...>/icpx`
  for **every** component (so the whole static set links against one OpenMP
  runtime).
- An ICX baseline flag set merged into each component's in-memory
  `platform_overrides["linux"]` (PoC-only, not committed as defaults):
  `-D_GNU_SOURCE -ffp-model=precise -Wno-recommended-option` (C) plus
  `-Wno-register` (C++).
- `CC`/`CXX` mirrored into `component.extra_env` so declarative source
  patches can gate on the active compiler.
- `MAKEFLAGS=LD=<icx>` for libvpx (its configure hardcodes `LD=gcc`).

No changes to the toolchain registry, the persistent store, or the standard
workspaces were required to run the experiment; the lower layer
(`component_env` / `profile_paths` / `get_build_env` / FFmpeg `--cc/--cxx`)
already accepts an arbitrary compiler.

```sh
.venv/bin/python scripts/icx_poc.py --ffmpeg-version 9.0          # full set
.venv/bin/python scripts/icx_poc.py --ffmpeg-version 9.0 --resume  # resume
.venv/bin/python scripts/icx_poc.py --ffmpeg-version 9.0 --components ffmpeg
```

## Result

FFmpeg 9.0, GPL profile, `native_build=true`, `openmp=true`: **58 components
(46 built + 12 system), all green**, release bundle in `workspace_90/release`
(~328 MB):

- `ffmpeg` / `ffprobe` / `ffplay` (~100 MB each, static).
- Bundled Intel runtimes collected automatically by `release_bundle.py`:
  `libiomp5.so` (OpenMP), `libirc.so`, `libsvml.so`, `libintlc.so.5`.
- No `libgomp` anywhere in the workspace artifacts. `libstdc++` stays system.

Verified on the binaries:

- `ffmpeg -version` reports `--cc=.../icx --cxx=.../icpx` with the full
  `--enable-libx264 --enable-libx265 --enable-libvpx --enable-libaom
  --enable-libsvtav1 --enable-librav1e ... --enable-vulkan --enable-libplacebo
  --enable-opencl` set.
- Encoder matrix all `rc=0`: libx264, libx264rgb, libx265, libvpx, libvpx-vp9,
  libaom-av1, libsvtav1, librav1e, libxvid, libtheora, mpeg4, aac, libmp3lame,
  libopus, libfdk_aac. (`libvmaf` returns 8 — it is a filter, not an encoder.)
- SVT-AV1 self-reports `Intel(R) oneAPI DPC++/C++ Compiler 2026.1.1`.
- Vulkan filters and `libplacebo` present; smoke transcodes pass.
- Regression clean: `pytest tests/` 335 passed, `black --check .` clean,
  mypy baseline check OK.

## Problems found and fixes

### 1. x265: unknown `IntelLLVM` compiler ID (`builders/codecs/x265.py`)

CMake reports icx/icpx as `IntelLLVM`. x265 only branches on
GNU/Clang/Intel-classic, so it took neither path and never compiled the
SSE3/SSSE3/SSE4.1 intrinsic objects (`vec/dct-sse3.cc` etc.), ending in
`undefined reference to setupIntrinsicDCT_*` at the 8-bit link.

Fix: pass `-DINTEL_CXX=1` when `CC`/`CXX` is icx/icpx. That selects x265's
GCC-like path, whose `-msse3/-mssse3/-msse4.1` flags icx accepts. GCC builds
are unaffected (flag added conditionally).

### 2. libvpx: `LD=gcc` hardcoded by configure (harness `MAKEFLAGS`)

libvpx's configure writes `LD=gcc` into the generated `*-x86_64-linux-gcc.mk`,
ignoring the `LD` environment. icx-compiled objects linked by gcc lose icx's
runtime symbols (`_intel_fast_memcpy`, `__svml_*`).

Fix (PoC harness): `MAKEFLAGS=LD=<icx>` for libvpx — a make command-line
variable beats the makefile assignment.

### 3. Strict `-std=c11/c++17` vs POSIX prototypes (harness baseline flags)

Unlike GCC, clang-based icx treats an implicit function declaration as a hard
error. The project's strict `-std=c11` hides POSIX/GNU prototypes
(`clock_gettime` in libvpx, `strtok_r` in x265's CMake detection — the latter
additionally compiled a conflicting static fallback), and `-std=c++17`
rejects `register` (x265 `md5.cpp`).

Fix (PoC harness baseline): `-D_GNU_SOURCE` (C/C++) and `-Wno-register`
(C++) for every component.

### 4. ICX default fast FP model vs `FENV_ACCESS` (harness baseline flag)

ICX defaults to a fast floating-point model and rejects
`#pragma STDC FENV_ACCESS` (soxr), besides changing FP semantics vs GCC.

Fix (PoC harness baseline): `-ffp-model=precise` restores GCC-like strict FP.

### 5. `-fopenmp` driver warning under `-Werror` (harness baseline flag)

icx emits `[-Wrecommended-option] use of '-qopenmp' recommended over
'-fopenmp'`, which `-Werror` builds (libzmq) turn into a failure.

Fix (PoC harness baseline): `-Wno-recommended-option`.

### 6. libplacebo glslang lookup misses `lib64` (`builders/graphics/placebo.py`)

With `prefer_static=true`, meson locates `libSPIRV.a` via the compiler's
`--print-search-dirs`. GCC's search honors `LIBRARY_PATH`; clang/icx's does
not, and glslang installs to `lib64` (GNUInstallDirs on Fedora/RHEL) while
libplacebo only searches `<sdk>/lib`.

Fix: extend the existing glslang `dirs:` patch to also search
`<sdk>/lib64` when the pattern is present (tolerant to libplacebo revisions).
Also made the original glslang-lookup patch idempotent.

### 7. FFmpeg ignored per-component CFLAGS (`builders/ffmpeg/ffmpeg.py`)

`build_ffmpeg()` built `--extra-cflags` from the global `builder.cflags`,
so the ICX baseline (notably `-ffp-model=precise`) never reached FFmpeg's own
translation units — FFmpeg compiled under ICX's default fast FP model.

Fix: use the component-scoped `env.get('CFLAGS', builder.cflags)`, which
includes platform overrides. Neutral for GCC (same value when no override).

### 8. x264 stack-alignment contract broken by ICX (`patches/icx_compat.py`)

Root cause chain:

- x264's `configure` does not know `icx` (only `icc` is special-cased), so it
  treats it as GCC and probes `-mpreferred-stack-boundary=6`, declaring
  `STACK_ALIGNMENT=64` to the NASM assembly.
- icx *accepts* the flag (the probe passes) but *ignores* it semantically
  (warning `#10430: Unsupported command line options`), so the generated code
  does not keep the 64-byte alignment the AVX/AVX-512 assembly requires.
- Result: deterministic segfault in libx264 asm paths (default/medium/fast
  presets; `no-asm=1`/ultrafast unaffected). Other asm-heavy components
  (dav1d, libvpx, aom, x265, native h264/hevc) were unaffected — the defect is
  specific to the x264 configure/compiler contract, not to SIMD code in
  general.

Fix, following the upstream x264 guidance documented in `docs/x264 Stack.md`
(Henrik Gramner): drop the forced alignment **on both sides consistently**
(CFLAGS probe block and the resulting `STACK_ALIGNMENT` define). The patch
replaces the `boundary=6` probe block with a fallback to default ABI
alignment (16); x264 then realigns at library/thread entrypoints as upstream
documents. Gated on `CC=icx/icpx` via `component.extra_env`, so GCC builds
keep `STACK_ALIGNMENT=64`.

Intermediate attempts that failed: replacing the flag with
`-mstack-alignment=64` (icx accepts it, but the crash persisted — the codegen
contract still did not hold), and a first patch revision whose idempotence
guard matched unrelated `stack_alignment=16` lines (the patch silently never
applied) plus a revision that broke the `if/elif/fi` structure.

Verification: `STACK_ALIGNMENT=16`, no forced flags in `config.mak`;
libx264/libx264rgb encode cleanly on default/medium/fast presets.

### 9. Release relocatability: Intel runtimes resolved via `LD_LIBRARY_PATH`
(`release_bundle.py`, **resolved**)

ELF audit of `workspace_90` (all binaries, shared libs, plugins; read-only):
no artifact in the graph carries an absolute `RPATH`/`RUNPATH`, and none embeds
a `/opt/intel/oneapi` string except informational ones (ffmpeg's
`configuration:` banner, openssl's `OPENSSLDIR/compiler:` info). The only
dependency on the oneAPI path was *implicit*: the Intel runtimes
(`libiomp5`, `libsvml`, `libirc`, `libintlc.so.5`; the PoC's shared
`libxvidcore`/`libSPIRV-Tools-shared`/`ossl-modules/legacy.so` additionally
need `libimf`/`libirng`) were found only through the oneAPI
`LD_LIBRARY_PATH` exported in the build env. The release copies of the four
libraries were never consulted. Intermediate install artifacts do not leak
RPATHs downstream (static graph; no RPATH set anywhere).

The earlier per-binary `$ORIGIN` attempt through FFmpeg's configure
(`--extra-ldexeflags` token + `config.mak` rewrite; every layer mangles `$`)
was abandoned and removed from `builders/ffmpeg/ffmpeg.py`. It also could not
solve the transitive case (`libsvml.so -> libintlc.so.5`).

Systemic fix, in the shared release step (`release_bundle.py`, Linux only,
runs when anything was bundled):

- `patchelf --set-rpath '$ORIGIN'` on **every** bundled ELF (executables and
  collected libraries; RUNPATH is not inherited by a dependency's own NEEDED
  lookups). `patchelf` is a Linux dependency in `pyproject.toml`.
- Verification with a scrubbed loader env (`LD_LIBRARY_PATH=""`): fails the
  release if any file has an absolute RPATH/RUNPATH entry, a `not found`
  dependency, or a bundled library resolving outside the release directory.
- `x265` and `SvtAv1EncApp` (from `workspace/bin`) are copied into the
  release too (`OPTIONAL_TOOLS`) and go through the same collection and
  `$ORIGIN` patching; downstream packaging can take them from `release/`.
- Applies to all toolchains/components uniformly; no per-component code.

Verified on the existing `workspace_90` binaries (no rebuild): release copied
to another directory, `env -i ldd ffmpeg` resolves all four Intel libs from
that directory, ffmpeg/ffprobe/ffplay run with an empty environment
(libx264 encode + ffprobe OK), and `strace` shows no file access under
`/opt/intel/oneapi`. The manifest records `rpath_rewrites`.

Remaining observation (not Intel-related): `ffmpeg` links the **system**
`libxvidcore.so.4` (`/lib64`) instead of the workspace static `libxvidcore.a`
(xvidcore installs a `.so` despite `--disable-shared`), so the release needs
that distro package at runtime.

## OpenMP runtime (Linux)

- icx links the Intel OpenMP RTL `libiomp5.so` (dynamic). Static linking of
  the OpenMP runtime is explicitly discouraged by Intel (risk of two runtimes
  in one process, and `-static-intel` does not cover it).
- Because **all** components build with icx, the release contains exactly one
  OpenMP runtime; `libgomp` is absent. Intel `libirc`/`libsvml`/`libintlc`
  are likewise dynamic and bundled by `release_bundle.py`.
- Post-link checks to keep: `ldd` shows only `libiomp5` (+ `irc`/`svml`/
  `intlc`), all resolvable from the release dir; unresolved `GOMP_*` absent.

## Windows note

Not built in this work. For the record, from the earlier analysis phase:

- There is no ICX Linux→Windows cross-compilation (oneAPI "cross-architecture"
  means CPU/GPU targets within one OS, not cross-OS). Scenario "WSL host →
  Windows target via ICX" does not exist.
- Native Windows ICX is MSVC-ABI (Visual Studio + Windows SDK + Ninja), not
  MinGW; MinGW-built static libraries cannot be mixed in safely. Moving the
  Windows backend off MSYS2-UCRT64 GCC to native ICX therefore means moving
  the whole stack to MSVC-ABI (`msvc` toolchain mode in FFmpeg configure,
  `lib.exe`, MSVC flags) — a separate project from the Linux work above.

## Verdict

**GO for Linux native/CPU builds.** The project's real dependency set builds
with icx/icpx; the fixes are small, conditional, and GCC-neutral. Remaining
work before treating ICX as a supported profile:

1. ~~`$ORIGIN` RPATH fix~~ — done centrally in `release_bundle.py` (section 9).
2. Promote the PoC harness baseline (`_GNU_SOURCE`, `-ffp-model=precise`,
   `-Wno-register`, `-Wno-recommended-option`, libvpx `MAKEFLAGS=LD`) into a
   proper toolchain profile instead of in-memory overrides.
3. Add post-link runtime checks (single OpenMP runtime; Intel `.so` closure
   resolvable) to the release step.
4. Run x264 `checkasm` and the 10-bit x264 path as extra validation.

## Files

Committed-track changes from this work (uncommitted at time of writing):

- `builders/codecs/x265.py` — `-DINTEL_CXX=1` for icx/icpx.
- `builders/graphics/placebo.py` — glslang `lib64` search + idempotence.
- `builders/ffmpeg/ffmpeg.py` — component-scoped `--extra-cflags`.
- `release_bundle.py`, `pyproject.toml` — `$ORIGIN` RUNPATH on all bundled ELF
  files + self-containment verification (patchelf).
- `patches/icx_compat.py` (new) + `patches/registry.py` — x264
  stack-alignment fallback for ICX, condition-gated.
- `scripts/icx_poc.py` (new) — PoC driver (oneAPI resolve, `toolchain_env`
  injection, ICX baseline flags, `--dry-run/--resume/--components`).
- `docs/x264 Stack.md` (user-provided analysis, Russian) — upstream-guidance
  background for the x264 fix.
