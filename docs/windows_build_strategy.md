# Windows FFmpeg cross-build strategy

**Status:** G1 **PASS**; G2.1–G2.8 **PASS**; G3 **PASS**; G4 **PASS**;
G5 **PASS**; G6 **PASS**; G7 CLI **PASS**; final TUI acceptance **PASS** on
the recorded hosts.

For 4.0.0, clean Linux/native FFmpeg 8.1 and 9.0 builds and a clean Windows FFmpeg
9.0 build through the normal TUI are accepted. The Windows build used the
`9.0-windows` variant, resolving internally to FFmpeg 9.0 plus
`linux-cross-mingw`. The normal CLI workflow is also accepted. Clean
orchestration, identity-checked reuse, applicable runtime acceptance,
packaging and relocation passed. Windows FFmpeg 8.1 is not part of the accepted
contract.

The supported Windows route is Windows host → WSL2 build host →
LLVM-MinGW/UCRT → Windows FFmpeg target. G1–G7 records below preserve
stage-specific evidence and historical "not started" results; those statements
describe their respective acceptance points, not current blockers. G5 scope
remains consumer-driven: absent, unsupported and out-of-scope hardware
features are classified and do not by themselves block release. Neural/DNN
and Intel oneAPI/ICX work are deferred, outside 4.0.0.

### G5 — accepted, consumer-oriented hardware validation

The production Windows FFmpeg build has executed CUDA, NVENC, CUVID, CUDA
libvmaf, Vulkan Video, Vulkan/libplacebo and OpenCL tests on the recorded
target. The maintained scope is current and planned cross-platform consumer
pipelines, not every platform-specific FFmpeg API. D3D11VA, D3D12VA and DXVA2
are **OUT_OF_SCOPE** and do not block G5.

On Windows 11 build 26200 with an NVIDIA TITAN V (driver 581.57),
`python scripts/run_build.py --g5` passed H.264 and HEVC NVENC and CUVID
encode/decode, CUDA scaling, CUDA libvmaf, Vulkan scaling and libplacebo,
H.264/HEVC Vulkan Video encode/decode, and OpenCL filtering on the GPU and
Intel OpenCL CPU. The TITAN V explicitly rejected NVIDIA and Vulkan AV1
encode/decode (**UNSUPPORTED**); QSV and AMF GPU hardware was **ABSENT**.
The acceptance is for these recorded operations and device, not all codecs,
profiles or machines. Earlier physical-target evidence is preserved.

The final run is recorded in `g5-runs/4acd9fa7d6934f418e29902372cc5d14.json`
under the identity-specific workspace. Repeating the normal path reused it in
`g5-runs/42b8efcb95f14d909c61853ddf050485.json` after matching the test
contract, build and Windows runtime identities, G4 artifacts and staged DLL
hashes. Both runs rechecked G4 and all 706 previously accepted dependency
files; a fresh physical runtime probe was also rechecked.

Earlier D3D12 H.264 B-frame encoding and cropped HEVC self-decoding failed.
P-only H.264 and 320x256 HEVC roundtrips passed; an isolated coded-dimensions
change passed the cropped HEVC test but was not integrated. These findings
remain informational, not consumer acceptance results.

Neural filtering is planned, not implemented or required by the accepted
production contract. No production neural backend is selected. An ONNX Runtime
investigation was exploratory only; it established neither a dependency nor a
neural acceptance requirement. Unspecified future filter chains are deferred.

An exploratory run of the production binary completed `nlmeans_vulkan` followed
by `libplacebo`, and `nlmeans_opencl` and `boxblur_opencl` on the TITAN V.
This supplemental evidence is not yet incorporated into the repeatable G5
acceptance path. A separate CUDA bilateral-filter run produced frames, but its
device-selection assertion did not pass and it is not accepted as a G5 result.

### Permanent capability and testing policy

FFmpeg capability requirements are consumer-driven: compilation or feature
listing alone does not make an API a project requirement. Hardware codec paths
actually used by consumers are production requirements. Broad cross-platform
CUDA, Vulkan/libplacebo and OpenCL compute/filter coverage is intentionally
required for current and planned consumers; CUDA-enabled libvmaf is an explicit
production requirement for an existing quality-metrics consumer.

Platform-specific APIs are optional unless a consumer explicitly depends on
them. Windows D3D11VA, D3D12VA and DXVA2 therefore do not block by default.
Meaningful runtime work on the selected physical device is required: listing
an encoder, decoder, filter or hwaccel is not acceptance. Reports distinguish
**PASS**, **FAIL**, **ABSENT**, **UNSUPPORTED**, **OUT_OF_SCOPE** and
**DEFERRED**; only a failure of an applicable consumer-required production
capability blocks acceptance. An inability to establish identity or evidence
also prevents claiming a pass; it is not a hardware failure.

Neural support is planned. ONNX, cuDNN, DirectML or another neural runtime
must not become a required FFmpeg dependency before a consumer architecture
explicitly selects it. Its build, runtime, dependencies and acceptance belong to
dedicated feature development, not a retroactive expansion of G5. G5 verifies
correctness and runtime operation, not benchmarking. G6 packaging/release is
separate from hardware capability acceptance and is recorded below.

### G4 final — configured Windows FFmpeg

The production `linux-cross-mingw` route consumes the 45 selected, previously
accepted target dependencies and interfaces, checks their identity and output
hashes, and builds FFmpeg 9.0 with LLVM-MinGW/UCRT, native CPU tuning and
OpenMP. This historical G4 acceptance used accepted component waves; the
normal production route now builds the selected stack automatically.
The engineering `--g4-final` path can consume previously accepted wave state.
The host CUDA 12.4 NVCC/GCC 12.5 pair generates PTX, while the verified host
Vulkan SDK shader compiler generates SPIR-V. The upstream nv-codec pkg-config
template and glslang's pinned SPIR-V headers are provisioned from the same
verified project sources as their accepted interfaces.

Configured libraries, CUDA/NVENC/NVDEC/CUVID, Vulkan/libplacebo, OpenCL and
the CUDA libvmaf filter are present. The physical Windows target has no AMD or
Intel hardware GPU, so AMF and oneVPL/QSV are not selected; Linux VAAPI and
macOS VideoToolbox are not Windows target features. The resulting build is
**nonfree and unredistributable** under FFmpeg's configure policy.

On the physical Windows target both x86-64 PE32+ `ffmpeg.exe` and
`ffprobe.exe` report version 9.0 and start successfully. A generated software
H.264 encode, ffprobe inspection and decode smoke passed. Same-identity
production reuse repeated those checks; all 706 prior manifest-recorded target
files retained their hashes, and the Wave-9 OpenCL acceptance also reused
successfully after the FFmpeg install. The acceptance stages the required
LLVM-MinGW `libc++.dll`, `libunwind.dll`, `libwinpthread-1.dll` and
`libomp.dll`; Windows/UCRT, `OpenCL.dll`, `vulkan-1.dll`, vendor ICDs and
NVIDIA driver libraries are external runtime prerequisites. `VSScript.dll` is
an external prerequisite when VapourSynth is used. G5 hardware acceptance and
G6 release packaging were accepted separately as documented below.

### G6 — accepted Windows runtime bundle

The normal `release_bundle.py` path produced an internal, target-specific
bundle from the accepted G4 FFmpeg 9.0 binaries and G5 runtime metadata.
`release/` contains only `ffmpeg.exe`, `ffprobe.exe` and the four hash-verified
LLVM-MinGW runtime DLLs: `libc++.dll`, `libomp.dll`, `libunwind.dll` and
`libwinpthread-1.dll`. Repeating packaging preserved the bundle payload and
runtime manifest apart from its generation timestamp.

PE imports were checked against G4 acceptance; the approved LLVM-MinGW DLL
closure was recursively inspected and hash-verified. Windows system/UCRT
imports and dynamically loaded external prerequisites are classified, not
copied. Component runtime metadata and accepted G5 features record NVIDIA
driver interfaces (`nvcuda.dll`, `nvEncodeAPI64.dll`, `nvcuvid.dll`),
`OpenCL.dll`, `vulkan-1.dll` and optional `VSScript.dll`. GPU drivers,
Vulkan/OpenCL loaders and vendor ICDs, Windows/UCRT/system files, and
VapourSynth's `VSScript.dll` remain external.

The bundle was copied to an identity-specific `g6-runs/<id>/bundle` path and
launched on the physical Windows target with PATH restricted to that relocated
directory and `%SystemRoot%\\System32`. `ffmpeg.exe` and `ffprobe.exe` started;
software H.264 encode/probe/decode and representative CUDA scaling,
CUDA-enabled libvmaf, Vulkan scaling and OpenCL filtering passed. G6 reused the
accepted G5 device identity and ordinal for the CUDA probe; Vulkan and OpenCL
reported the NVIDIA TITAN V. Full hardware acceptance remains the G5 evidence,
not a G6 goal.

The accepted build is GPL/nonfree and unredistributable. G6 verifies packaging
and runtime closure only; this bundle is internal to the recorded target and
does not establish legal redistributability or clean-machine portability.
Acceptance evidence is in `g6-acceptance.json`; the release `manifest.json`
records bundled hashes, PE imports, system imports and external prerequisites.

### G4 Wave 9 — physical-Windows OpenCL interface

The configured physical target reports an NVIDIA TITAN V OpenCL GPU and an
Intel OpenCL CPU, but no Intel or AMD GPU. Wave 9 therefore accepts the
registered `opencl-headers` target input; `opencl-icd-loader` is native-only
and supplies the pinned `OpenCL.def` used by G2 to generate the import
library, not a Windows loader build. Intel oneVPL/QSV and AMD AMF are not
selected for this target. The wave fails explicitly rather than silently
claiming acceptance for a target with an Intel or AMD hardware GPU.

The normal G2 preparation verifies OpenCL-Headers 2026.05.29, the matching
ICD loader source definition, target headers and LLVM-MinGW `libOpenCL.a`.
Wave 9 records their component state and hashes, validates the Windows COFF
import interface and links a PE consumer. On physical Windows the consumer
compiled and dispatched an OpenCL kernel on TITAN V and checked its results.
`OpenCL.dll` and the vendor ICD/driver are external Windows runtime
prerequisites. Target metadata exposes the import library and runtime
requirement, and cross-target feature selection does not use a WSL OpenCL
runtime as a substitute. Revalidation and reuse passed; 687 files from prior
acceptance manifests retained their hashes. At that acceptance point no FFmpeg
build had been run.

### G4 Wave 8 — CUDA and libvmaf

The bounded `python scripts/run_build.py --g4-wave8` production path reuses
the G2 checksum-verified nv-codec target headers before building registered
libvmaf 3.2.0-8fc71e30. The Windows target build uses LLVM-MinGW, native
CPU tuning, and static pkg-config metadata including libc++ and libunwind.
The selected Linux CUDA 12.4 NVCC and bin2c and project GCC 12.5 host compiler
generate device fatbins and C arrays; LLVM-MinGW compiles those arrays and
the library as Windows COFF. Upstream's architecture set is retained alongside
the selected compute_70 target. The CUDA driver declarations remain separate
from the selected NVENC headers when the latter need an older version.

The installed target library and public headers passed a physical-Windows
consumer that loaded the NVIDIA driver and ran the motion, ADM and VIF CUDA
extractors; same-identity revalidation and reuse passed, with 674 prior
manifest-recorded files unchanged. The external Windows `nvcuda.dll` and
compatible driver/GPU, plus LLVM-MinGW's libc++, libunwind and winpthread DLLs,
remain runtime requirements. At that acceptance point Wave 9 and the full
Windows FFmpeg build had not started.

### G4 Wave 7 — Vulkan, glslang and libplacebo

The bounded `python scripts/run_build.py --g4-wave7` production path reuses
the G2 Vulkan-Headers 1.4.357.0 target inputs and import library, installs the
matching registry and target `vulkan.pc`, then builds glslang 16.5.0 and
fast-float 8.2.10 before libplacebo 7.360.1. glslang's upstream
`known_good.json` pins SPIRV-Tools
`b707790a898e44038547df54580022fc1cf89c3d` and SPIRV-Headers
`29981f65241605e08b0ede4cfeb999fe3b723c6a`. Their checksum-pinned archives
use the normal project downloader and must be present in the local source cache
for an offline build. The optimizer remains enabled; host Python and
LLVM-MinGW's host-executable dlltool are used for generation, while target
libraries use the generated LLVM-MinGW toolchain and native tuning.

The target prefix contains Vulkan headers and loader import library, static
glslang and SPIRV-Tools COFF archives, fast-float headers, and static
libplacebo with Vulkan and glslang enabled. Static pkg-config closure includes
Vulkan, glslang, SPIRV-Tools, libc++ and libunwind. The external Windows
`vulkan-1.dll` and ICD remain runtime prerequisites; the consumer also imports
LLVM-MinGW `libc++.dll` and `libunwind.dll`. A physical-Windows target consumer
created a Vulkan device, compiled and dispatched a shader and verified its
pixels. All four components passed same-identity revalidation and reuse; 574
prior manifest-recorded files retained their hashes. At that point Wave 8 had
not started.

### G4 Wave 6 — rav1e

The registered rav1e 0.8.1 source was built offline with the project Rust
1.99.0 `x86_64-pc-windows-gnullvm` target, LLVM-MinGW and pinned host
`cargo-c` 0.10.25. Its locked Cargo dependencies were provisioned from the
local Cargo source cache, copied into the identity-local
`generated/cargo-home/registry/{cache,index}` before the offline run; the
pinned `cargo-cinstall` was supplied on the host PATH (superseded after G7 by
the store-managed cargo-c profile). Offline builds require
those locked crates to be provisioned locally. Host build-script pkg-config
discovery is separated from the target wrapper; target-native Rust tuning,
NASM assembly and runtime dispatch remain enabled. Staged installation prevents
partial target outputs.
The installed `include/rav1e/rav1e.h`, `lib/librav1e.a` and
`lib/pkgconfig/rav1e.pc` passed acceptance. The archive has 614 x86-64 COFF
members including Windows import objects. Static metadata carries Windows
system libraries and `-lunwind`; the PE consumer imports LLVM-MinGW
`libunwind.dll` and Windows system/API-set DLLs. A physical-Windows C API
lifecycle smoke passed. Same-identity revalidation and reuse passed, and 571
previously accepted files retained their hashes. At that point Wave 7 had not started.

### G4 Wave 5C — libjxl and VapourSynth

The registered libjxl 0.12.0 archive contains empty Brotli and Highway
submodule directories. Its `deps.sh` pins Brotli
`028fb5a23661f123017c060daa546b55cf4bde29` and Highway
`457c891775a7397bdb0376bb1031e6e027af1c48`. A bounded investigation fetched
only those upstream revisions and configured and built libjxl in an isolated
directory using the existing LLVM-MinGW CMake toolchain, native tuning, target
lcms2, static output and upstream Highway dispatch. The production cross
builder now provisions those checksum-verified sources through the project
downloader, uses the generated LLVM-MinGW toolchain, and reuses accepted target
lcms2. The accepted static closure contains libjxl, libjxl_cms,
libjxl_threads, Highway and Brotli libraries. Their 180 archive members are
Windows x86-64 COFF. Target pkg-config metadata supplies lcms2, Brotli, Highway,
libc++ and libunwind; no archive references OpenMP or pthread symbols.
The decoder/encoder/thread-runner PE consumer and its physical-Windows
lifecycle smoke passed. libc++.dll and libunwind.dll are G6 runtime candidates.

The isolated install exposed an upstream Brotli install rule with absolute
manpage destinations: overriding CMake's install prefix alone did not isolate
those files. The four written manpages were removed; none was recorded in
prior acceptance manifests, but their previous unmanifested state was not
captured. All 487 files recorded in prior component acceptance manifests
retained their hashes. The production install uses `DESTDIR` and promotes only
the selected target headers, static libraries and metadata, so Brotli's
absolute manpage destinations remain in staging.

FFmpeg 9.0's VapourSynth configure check preprocesses `VSScript4.h` and
`VapourSynth4.h`, and its Windows demuxer dynamically loads `VSScript.dll` and
resolves `getVSScriptAPI`. An isolated target preprocessing probe succeeded.
The DLL is an external VapourSynth runtime prerequisite, not a target import
library produced by this project. The installed R78 interface and the pinned
FFmpeg preprocessing contract were accepted without a link library. Both
components have acceptance manifests and passed same-identity reuse; no FFmpeg
build was performed.

### G4 Wave 5B — libzmq

The registered zeromq 4.3.5 archive is local and matches SHA-256
`6653ef5910f17954861fe72332e68b03ca6e4d9c7160eb3a8de5a5a913bfab43`.
Its generated configure script uses the shared Autotools target projection;
no bootstrap is needed. The cross-only adapter adds the pinned source's missing
`<new>` include and selects static output, with docs, perf, keygen, drafts,
WebSocket, libbsd, optional libunwind, libsodium, GSSAPI, PGM, NORM, VMCI and
TLS disabled. The target uses Windows select/Winsock and Windows threads, with
pthread references for thread naming. Native tuning and the primary OpenMP
profile remain enabled; the archive has no OpenMP references.

The installed public headers, static `libzmq.a`, pkg-config and libtool metadata
passed target and contamination validation. All 111 archive members are x86-64
COFF. Target metadata replaces upstream `-lstdc++` and adds `ZMQ_STATIC`, libc++,
libunwind, pthread, IP Helper and Winsock; the legacy MSYS repair is not used.
Negative links established libc++, pthread, IP Helper and Winsock requirements.
The physical Windows inproc PAIR message smoke and same-identity revalidation
and reuse passed. `libc++.dll`, `libunwind.dll` and `libwinpthread-1.dll` are
classified for G6; Windows system DLLs remain system-owned. At that acceptance
point Wave 5C and Windows FFmpeg had not been built.

### G4 Wave 5A — zlib, OpenSSL and SRT

The target dependency chain is `zlib → openssl → srt`. Wave 1 zlib was
revalidated and reused. The registered offline OpenSSL 3.6.4 and SRT 1.5.6
archives matched their SHA-256 checksums. OpenSSL uses host Perl and its
`mingw64` Configure target with LLVM-MinGW tools, static zlib, no shared
libraries, tests, applications or loadable modules. SRT uses the generated
CMake target toolchain, static libraries, target OpenSSL pkg-config discovery
and enabled `openssl-evp` encryption; its unconditional upstream ffplay script
install is excluded. Both retain native tuning and the primary OpenMP profile.

The installed target OpenSSL headers, `libcrypto.a`, `libssl.a`, their three
pkg-config files, SRT headers, `libsrt.a` and two pkg-config files passed
target-only metadata, archive and link validation. The archives contain 1057,
94 and 41 x86-64 COFF members respectively. Static metadata records target
zlib, Windows system libraries, and SRT's libc++/libunwind and pthread
requirements. The SRT adapter removes an unused inferred `-lomp` only after
checking archive symbols. Windows target link smokes include negative
dependency links and a combined encrypted SRT/OpenSSL/zlib consumer. A physical
Windows smoke verified SHA-256, TLS context creation and SRT socket lifecycle
without external network traffic; the static default provider worked without
a provider module or configuration file. `libc++.dll`, `libunwind.dll` and
`libwinpthread-1.dll` are classified for G6, not bundled.

Same-identity reruns revalidate and reuse all three components, and missing or
changed accepted outputs fail safely. At that acceptance point Wave 5B and
Windows FFmpeg were not built.

### G4 Wave 4C — zimg

The local zimg 3.0.6 archive has SHA-256
`be89390f13a5c9b2388ce0f44a5e89364a20c1c57ce46d382b1fcc3967057577`.
It lacks a generated configure script, so the cross-only recipe bootstraps
with host autoreconf, projects the shared Autotools cross environment, and
selects static output without examples, tests or the test application. The
recipe adds the missing `<exception>` include required by LLVM libc++ and
isolates the old MSYS libtool lookup in the unchanged legacy/native route.
Upstream x86 SIMD and dispatch remain enabled alongside the configured native
target flags.

The target prefix contains `zimg.h`, `zimg++.hpp`, `libzimg.a`, `libzimg.la`
and `zimg.pc`. All 49 archive objects are x86-64 COFF; C and C++ public-API
consumers link as Windows PE, including a negative C link without C++ runtime
metadata. pkg-config, libtool and registry metadata publish `-lc++ -lunwind`;
`libc++.dll` and `libunwind.dll` are classified for later packaging. The link
smokes did not require target pthread or OpenMP; unused inherited libtool
flags are removed only after checking the archive's undefined symbols. The accumulated prefix scan
and same-identity revalidation/reuse passed. No Windows FFmpeg or Wave 5A
component was built.

### G4 Wave 4B — bespoke software codecs

The verified offline archives for `x264` revision
`b35605ace3ddf7c1a5d67a2eb553f034aef41d55`, `x265` registered as 4.2,
and `libvpx` 1.17.0 were built with LLVM-MinGW. x264 and libvpx use their
configure/make paths; x265 uses separate 12-, 10-, and 8-bit CMake builds and
an LLVM archive merge that verifies every input member and preserves duplicate
member names. The installed combined archive is independently reconstructed
and compared on acceptance. NASM is a host executable emitting win64 COFF;
upstream runtime CPU dispatch remains enabled alongside the project's target
native-tuning flags.

Individual and combined pkg-config-based Windows target links, all archive
members and the accumulated prefix scan passed. x265's static-consumer
metadata publishes libc++/libunwind, with `libc++.dll` and its transitive
`libunwind.dll` recorded for packaging. libvpx publishes target pthread and
`libwinpthread-1.dll`. Same-identity reruns revalidate and reuse all three.
The pinned x265 archive's own `x265Version.txt` reports `4.0+1-6318f22`, and
its generated pkg-config version is `4.0`, despite the registry's 4.2 label;
the archive checksum matches the registry. This provenance discrepancy is not
silently rewritten. (Resolved after G7: the archive is upstream tag 4.2, whose
shipped `x265Version.txt` is stale; see "Post-G7 stabilisation".) At that acceptance point Wave 4C and FFmpeg had not been built.

### G4 Wave 4A — CMake AV1 codecs

Accepted `svtav1` 4.2.0 and `av1` (AOM) 3.13.3 from verified offline sources
using the shared CMake cross toolchain and host NASM producing `win64` COFF.
AOM's generated tables use host Perl and runtime CPU dispatch remains enabled.
Both target archives, headers, pkg-config/CMake metadata and Windows PE link
smokes passed. The AOM static link requires target `-lm` and `-lpthread`, and
imports the LLVM-MinGW `libwinpthread-1.dll`; this is recorded as transitive
target metadata for later packaging. SVT's C consumer required no extra C++
or OpenMP link flags. The repeated same-identity run revalidated and reused
both codecs. At that acceptance point Wave 4B and FFmpeg had not been built.

### G4 Wave 3B — Ogg, Vorbis, and Theora

The Wave-1 `libogg` 1.3.6 target outputs were revalidated and reused for
`libvorbis` 1.3.7 and `libtheora` 1.2.0. Vorbis uses the shared Autotools
cross projection; both recipes preflight target-only pkg-config dependencies.
The cross-only library recipes exclude upstream documentation, test, and
training subdirectories that are not reliably excluded by configure options.
The installed Vorbis, Vorbis encoder and Vorbis file archives, and the Theora,
Theora encoder and Theora decoder archives were verified as x86-64 COFF.
Their target pkg-config static closures and individual and combined Windows
target link smokes passed, as did the full target-prefix contamination scan.
The same-identity rerun revalidated and reused both components. Sources were
local and checksum-verified; no FFmpeg or Wave-4 component was built.

### G4 Wave 3A — LV2 dependencies

Accepted `lv2` 1.18.10, `serd` 0.32.10, `zix` 0.8.0, `sord` 0.16.22,
`sratom` 0.6.22, and `lilv` 0.28.0 through the production Meson path. The
declared dependency order is `lv2`, `serd`, `zix`, then `sord` (using `zix`
and `serd`), `sratom` (using `serd`, `sord`, and `lv2`), and `lilv` (using
all five preceding components). Acceptance verified local source checksums,
target headers and LV2 data, static COFF archives, target pkg-config closure,
component link smokes, and the full prefix contamination scan. The repeat
same-identity invocation reused all six accepted outputs. LV2's unconditional
validator utility is omitted from the target install.

At that acceptance point Wave 4B and FFmpeg integration had not started.

### G4 Wave 2 — static media and utility dependencies

Accepted `gettext` 0.26, `pcre` 8.45, `xvidcore` 1.3.7, `opencore` 0.1.6,
`lame` 3.100, `fdk_aac` 2.0.3, `soxr` 0.1.3, `libtiff` 4.7.2, `lcms2`
2.19.1, `libwebp` 1.6.0, and `libsdl` 2.30.12 for the identity-specific
Windows target prefix. Acceptance verified their expected headers, static
archives, target pkg-config metadata where provided, COFF archive members,
component link smokes, and the complete target-prefix contamination scan.
Wave 1 remained reusable; the same-identity Wave-2 rerun reused all eleven
components without rebuilding.

The primary host-optimized Windows profile requires both
`native_build=true` and `openmp=true`; failure with either enabled is a build
failure, not a compatibility fallback. The supported compatibility/portable
profile uses `native_build=false` and `openmp=false` for portable release or
diagnostic builds, and its success cannot substitute for a primary-profile
failure. Wave 2 passed with the primary profile. C and C++ OpenMP compile/link
probes and a physical-Windows runtime smoke confirmed LLVM-MinGW's
`libomp.dll`. soxr's effective CMake option was `WITH_OPENMP=ON`; its static
consumer metadata requires `-fopenmp` and records `libomp.dll` as a
toolchain-runtime DLL and G6 packaging input.

All eleven registered source archives were already present in the
project-managed offline mirror and passed their declared SHA-256 checks; no
source or validation-tool downloads were needed. Wave 3A is now accepted as
described above; at that acceptance point Wave 4B and FFmpeg integration had
not started and G4 remained in progress.

### G4 Wave 1 — first production target dependencies

Accepted `zlib` 1.3.2, `libogg` 1.3.6, `opus` 1.6.1, `libpng` 1.6.58,
`FreeType2` 2.14.3, `dav1d` 1.5.4, and `vid_stab` 1.1.1 using the production
Autotools, Meson, and CMake cross-build paths. Their identity-local target
archives, headers, and pkg-config metadata passed Windows COFF validation,
target link smokes, and the full target-prefix contamination scan. A repeated
same-identity run validated and reused all seven outputs.

The Wave-1 dependency graph declares `zlib -> libpng` and
`zlib, libpng -> FreeType2`; FreeType configuration requires both target
dependencies, and its pkg-config metadata publishes them. libpng is built
without tools, tests, shell `*-config` helpers, or unversioned header/library
links. Its versioned static library and headers retain the upstream `libpng.pc`
pkg-config alias required by FreeType's configure check. Sources were
verified against local registered checksums; no project-source download was
needed. At Wave-1 acceptance, Wave 2 and FFmpeg had not started and G4
remained in progress.

### G4 Wave 0 — role and ownership boundary

The existing `BuildRole` remains the single role model: `host` means a
`HOST_TOOL`, `target` means a `TARGET_COMPONENT`, and `mixed` requires
separate host and target phases. The ten registered host tools are
`pkg-config`, `yasm`, `nasm`, `m4`, `autoconf`, `automake`, `libtool`,
`cmake`, `meson`, and `ninja`. Under `linux-cross-mingw`, they resolve as
read-only WSL host prerequisites; their availability is not persisted as a
Windows-target component status, and they are never placed in the target
prefix. NASM/Yasm remain host executables; a target consumer must request its
Windows object format explicitly.

`system_component` remains valid for native builds. On the cross backend it
may satisfy a `HOST_TOOL`, but never a `TARGET_COMPONENT` or the target-output
phase of a `MIXED` component. Thus host zlib or oneVPL cannot suppress their
Windows-target builds. Same-identity target artifacts remain reusable through
the identity guard and component state; host-system detection is not target
artifact reuse.

`giflib` remains registered and native-only: the FFmpeg 9.0 registry has no
configured target consumer or FFmpeg flag for it, while the production
builder treats it as an always-system prerequisite. It is not selected as a
WSL-Mingw target output. `opencl-icd-loader` is native-only for component
builds and is retained as G2 `OpenCL.def` provenance for the generated
`libOpenCL.a` interface; Windows runtime policy remains system
`OpenCL.dll` plus the vendor ICD. `cargo-c` 0.10.25 is a ToolchainStore
binary profile required by the WSL-Mingw platform profiles (post-G7
stabilisation; it was previously a prepared PATH prerequisite). The cross
Cargo path invokes only the verified store `cargo-cinstall`, checks its
version, and fails closed if it is missing or mismatched, without consulting
PATH or invoking `cargo install`.

The cross dry-run reports G4 Wave-0 roles, host-tool satisfaction,
conditional components, target inputs, and target-build selection after G1/G2
authorization and G3 acceptance. It stops before any component build; the
normal production route builds the selected stack and continues to final G4
integration without requiring historical wave state.

**Naming.** The user-facing Windows build route is **WSL-Mingw**: Windows host
→ WSL2 Ubuntu build environment → LLVM-MinGW/UCRT cross toolchain → Windows
FFmpeg target. The internal backend identifier is **`linux-cross-mingw`**.
These are two names for one route, not two routes.

**Superseded route.** The previous MSYS2-based Windows build path
(`windows-msys2-ucrt64`) was replaced by WSL-Mingw. It is not a supported
target backend in the final architecture, and this plan imposes no
requirement to preserve it. Legacy MSYS2 code may remain temporarily in the
repository during implementation (see §3.3) but must not be presented as
supported.

This plan distinguishes **verified results**, **project requirements**,
**repository facts**, **unverified items**, and **implementation work**.
Verification is scoped to the exact host/configuration recorded below; it is
not a portability claim. Status labels are `[VERIFIED <gate>]`,
`[REQUIREMENT]`, `[REPO]`, `[UNVERIFIED]`, and `[IMPLEMENTATION]`.

## 1. Decision register

| Decision | Status and evidence | Implementation consequence | Caveat |
|---|---|---|---|
| Variant 1, WSL Linux to Windows with LLVM-MinGW/UCRT, is selected. | [VERIFIED G0-G0A.5, G4-G7 CLI] Early PoCs and production FFmpeg 9.0 integration passed on the recorded host. | Use `linux-cross-mingw`; do not rerun feasibility per component. | G5 hardware and G6 package acceptance are scoped to the recorded target. |
| The physical Windows machine running WSL is the target; WSL is the controlled build environment. | [REQUIREMENT] Maintainer decision; [VERIFIED G0] Windows interop executed the PoCs. | Probe Windows target state separately from Linux build-host tools. Optimize for this host, not arbitrary Windows machines. | A material host change can require rebuild and new acceptance. |
| LLVM-MinGW UCRT is the initial cross toolchain. | [VERIFIED G0-G0A.5] LLVM-MinGW 20261104, Clang/LLD 23.1.5, Windows PE execution and actual UCRT loading. | Pin and validate toolchain identity, target triple, and runtime. | Does not establish compatibility with every library or toolchain revision. |
| C++ and Rust communicate through a C ABI. | [VERIFIED G0A.1] The mixed C/C++/Rust tests passed in both linking directions and in one executable. | Use fixed-layout C interfaces and caller-owned buffers; contain exceptions and panics. | No arbitrary C++/Rust ABI or cross-boundary unwinding is supported. |
| Rust uses `x86_64-pc-windows-gnullvm` with the matching target standard library. | [VERIFIED G0A.1] Rust 1.99.0 target-std was installed to an isolated PoC sysroot and linked with LLVM-MinGW. | Provision and version the target std alongside its compiler. | PoC provisioning is not the production store implementation. |
| WSL-Mingw target C/C++ tuning is compiler-native and host-specific. | [REQUIREMENT] Use `-march=native -mtune=native`; the selected LLVM-MinGW compiler interprets `native`. | Pass these flags in order when `native_build` is enabled; accept the policy when the selected compiler accepts them. | No portability claim is made for unrelated Windows CPUs. G1 CPU data remains target identity/runtime evidence, not a feature whitelist. |
| CUDA uses Linux NVCC to emit PTX for the Windows NVIDIA driver. | [VERIFIED G0A.3/G0A.5] CUDA 12.4.99 generated compute_70 PTX; Windows driver JIT/kernel and FFmpeg CUDA-filter execution passed. | Select NVCC, host compiler (`-ccbin`), and target architecture explicitly. | Tested on TITAN V (CC 7.0), one driver/toolkit combination. |
| CUDA host compiler selection is explicit. | [VERIFIED G0A.3/G0A.5] Project/store GCC 12.5.0 was passed as `-ccbin`; ambient GCC 15 was not used. | Version, validate, configure, and include effective `-ccbin` in identity. | Do not switch system GCC or use global alternatives. |
| CUDA and Vulkan SDKs are external prerequisites of the prepared WSL environment. | [MAINTAINER DECISION; current paths checked] Example layout: CUDA `/opt/cuda-12.4`; Vulkan root `/opt/vulkan-sdk/1.4.363.1`, effective Linux root `/opt/vulkan-sdk/1.4.363.1/x86_64`. | Locate via documented/configured path or environment; check presence and that the tools/interfaces required by the selected configuration are usable; fail clearly if not. | The build system does not own, archive, provision, hash or version-manage these SDKs; ordinary SDK tool-version changes need no registry edits. They are not target-prefix contents. |
| The Windows Vulkan runtime uses the system loader and vendor ICD. | [VERIFIED G0A.4/G0A.5] Windows `vulkan-1.dll` discovered the NVIDIA ICD/device and ran compute and FFmpeg filter work. | Do not build or bundle a Vulkan loader by default; isolate target discovery. | Future dependencies may require an import interface. |
| The Windows OpenCL runtime uses the system dispatcher and vendor platforms. | [VERIFIED G0A.4/G0A.5] Windows `OpenCL.dll` exposed two platforms; the NVIDIA TITAN V program and FFmpeg filter dispatched successfully. | Do not treat the dispatcher as an implementation or bundle a separate ICD loader by default. | Probe/select a platform and device explicitly; platform inventory is host-specific. |
| Static C++ dependencies must carry their runtime requirements through metadata. | [VERIFIED G0A.5 Phase B] Static libplacebo objects needed libc++/libunwind flags at FFmpeg's final C-link invocation; the PoC repaired isolated pkg-config metadata. | Production dependency metadata/link composition must express those requirements transitively. | Do not copy the PoC metadata edit as a production recipe. |
| Target-scoped discovery is mandatory. | [REQUIREMENT; verified in PoC isolation] Target pkg-config/CMake/Meson lookup must not consume Linux SDK/library paths. | Build isolated cross files and environment; keep host tools explicit. | PoC paths and temporary metadata are not production inputs. |
| Build identity protects artifacts and resume state. | [IMPLEMENTED G1/G2.7] The full Windows target and resolved project/effective material-input identity is compared before reuse. | Keep every new material input in the projection before it can affect artifacts. | Identity prevents unsafe reuse; it does not make the project own external SDK lifecycle. |
| MSYS2-UCRT64 is not a supported backend. | [MAINTAINER DECISION] The old MSYS2 approach motivated this design; it is replaced, not preserved. | No new feature work targets it; legacy branches may be removed/migrated when a gate reaches them. | Legacy code may remain temporarily; it carries no support or parity obligation. |
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
  driver; the Linux Vulkan SDK used for build/shader tools is an external WSL
  prerequisite (§4).

The CPU, GPU, OS build, loader versions, and exact tool versions above are
observations, not defaults for other hosts. See §5 for what belongs in build
identity versus diagnostics.

### 2.2 Gate results

| Gate | Result actually established | Evidence |
|---|---|---|
| G0 | LLVM-MinGW built Windows PE C and C++ programs, OpenMP program, and all ran through Windows interop. The processes loaded UCRT; libc++/libunwind and libomp DLL paths were observed. OpenMP reported four workers. | `files/g0/G0-report.md`, final G0 addendum and logs. |
| G0A.1 | C-compatible boundary passed with C, LLVM-MinGW C++ static library, and Rust static library. Both mixed link directions and combined program ran on Windows. C++ exceptions and Rust panic were caught inside their respective exported functions and returned as status; no unwind crossed the C ABI. | `files/g0a1/G0A1-report.md`, `files/g0a1/ABI.md`, logs. |
| G0A.2 | Historical feasibility testing compared compiler-native and explicit specialization, and inspected Windows CPUID/OS-enabled vector state. This experiment is not production policy; production uses `-march=native -mtune=native` and does not reproduce or validate the compiler's internal feature expansion. | `files/g0a2/logs/compiler-native-resolution.txt`, `explicit-host-resolution.txt`, `windows-cpuid-runtime.txt`, build/disassembly logs. |
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
feasibility is established for §2.1. Production implementation, full-stack
integration, applicable hardware validation, identity safety and packaging
have since passed on the recorded target.

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
- Probe actual Windows state for target identity and runtime acceptance; do
  not infer it from WSL's Linux view. CPU probe fields do not validate or
  reconstruct LLVM-MinGW's interpretation of `native`.
- A changed material host or effective build input must not reuse incompatible
  completion state, mutable build trees, target prefixes, or installed
  artifacts.
- Keep project-managed source/toolchain provisioning offline-first. SDK
  prerequisites owned by the WSL environment are checked for presence and
  usability, never implicitly installed, archived, hashed or modified.
- `windows-msys2-ucrt64` is not a supported target backend in the final
  architecture; there is no requirement to preserve it or keep it deprecated
  until parity. No new feature work targets it. Legacy MSYS2-specific branches
  may be removed or migrated when the relevant production gate reaches them.
  `linux-wsl2` (native WSL/Linux build) remains a distinct backend from
  `linux-cross-mingw`.
- Preserve `ComponentStatus` values, pass `detail=` for transitions, and
  report failures using repository `BuildError` conventions.

### 3.3 Repository facts to account for [REPO]

The repository has native Linux/WSL strategies, a **legacy** MSYS2-UCRT64
strategy, and the production `linux-cross-mingw` component and FFmpeg path
accepted through G4. The legacy code (`platforms/windows_ucrt64.py`,
UCRT64-specific branches in `builder.py`,
`executor.py` sh-wrapping, `scripts/setup_windows_msys2_ucrt64.ps1`, registry
`windows_ucrt64_supported` overrides, and related tests) is migration/removal
work, not a supported backend; do not reuse MSYS path rewriting in the Linux
cross backend. `state.py` persists a UUID
`build_id`, configuration, component statuses, and identity. G1/G2.7 prepare
fresh Windows facts, resolve the selected project and external build inputs,
materialise and verify G2.6 target inputs, then compare the complete semantic
identity before reuse and select an identity-specific workspace. A state reset
alone does not isolate an existing prefix.

The existing CUDA resolver/store has validated the selected CUDA 12.4 / GCC
12.5 PTX combination for the tested Windows target, and native Linux shares
a checked CUDA profile between libvmaf and FFmpeg. The Windows adapters and
integration were subsequently accepted in G4; see §5.7. Extend declarative
component and toolchain metadata rather than scattering component-specific policy.

## 4. Build-host and target ownership

### Layer A — prepared WSL build-host environment

Linux-native tools and prerequisites: Ubuntu WSL2; CMake, Meson, Ninja,
pkg-config, NASM/Yasm, Perl/Python and other explicitly selected host tools;
and the external CUDA and Vulkan SDKs. The currently prepared example
environment has CUDA SDK `/opt/cuda-12.4`, Vulkan SDK root
`/opt/vulkan-sdk/1.4.363.1` and effective Linux SDK root
`/opt/vulkan-sdk/1.4.363.1/x86_64`; the tested configuration used NVCC
12.4.99 with project/store GCC 12.5.0 as `-ccbin`. The ambient Ubuntu compiler
is newer (GCC 15) and is not the CUDA 12.4 host compiler. These are
observations of one environment, not registry requirements.

**External SDK ownership.** The CUDA and Vulkan SDKs are external
prerequisites of the prepared WSL environment. The build system uses them but
does **not** own, archive, provision, hash, install, recreate or version-manage
them as project dependencies, and does not maintain a registry entry merely
because an installed SDK or one of its tools changes version. It may:

- require CUDA/Vulkan when the selected configuration needs them;
- locate the SDK from a documented/configured path or environment variable;
- verify presence and that the tools/interfaces the requested build needs
  (e.g. NVCC, shader compiler, SPIR-V tools) are usable;
- report a clear prerequisite error when unavailable;
- record diagnostic information about the environment actually used.

Do not select the first matching `nvcc`/Vulkan tool on ambient PATH when an
explicit path is configured; do not install system SDKs, mutate `/opt`, or
install a Linux NVIDIA driver. A specific tool version is a requirement only
when a project-controlled component genuinely requires that exact version.
The Vulkan SDK supplies the shader/SPIR-V tools used in the tested
environment; do not require duplicate Ubuntu shader packages for the same SDK.

It is acceptable for the WSL environment to define `CUDA_HOME`, `CUDA_PATH`,
`VULKAN_SDK`, and PATH additions to explicitly selected SDK binaries. LunarG
`setup-env.sh` changes `LD_LIBRARY_PATH`, `VK_ADD_LAYER_PATH`,
`PKG_CONFIG_PATH`, and `CMAKE_PREFIX_PATH`; activate those full changes only
for intentional native WSL Vulkan work. They must not implicitly enter
Windows-target dependency discovery.

The E0.1 experiment showed that a CUDA 12.4 payload could be reconstructed on
Ubuntu 26.04 (the legacy runfile installer needed a libxml2 ABI that Ubuntu
26.04 lacks) and used for G0A.3/G0A.5. It is historical evidence only: the
reconstructed archive is not a project artifact, and no project mechanism
stores, hashes or recreates it. This is not evidence that CUDA 12.4 is
vendor-supported on Ubuntu 26.04.

When WSL-Mingw CUDA is enabled, target preparation compiles a minimal PTX
kernel with resolved NVCC and ToolchainStore `-ccbin`. Architecture comes from
`cuda_compute_capability`, then `CUDA_COMPUTE_CAPABILITY`, or, if neither is
set, the fresh G1 Windows probe. In the last case every reported NVIDIA
capability must be valid and the numeric minimum is the PTX baseline. There is
no separate per-GPU selector: a G1 target override can supply an inventory.
An explicit architecture override is checked by the compiler probe but not
against every Windows device; neither path proves runtime compatibility with
every device.

Live production preparation selected `/opt/cuda-12.4/bin/nvcc` (12.4.99),
project `gcc12` (12.5.0), and the physical Windows TITAN V (7.0), requesting
`compute_70`. The first compile failed on the glibc declaration conflict;
the scoped `-Xcompiler=-U_GNU_SOURCE` retry exited successfully and produced
758 bytes of PTX containing `.version 8.4`, `.target sm_70`, and an entry.
Temporary files were removed. This check did not load or execute the PTX; the
earlier G0A runtime experiments are separate evidence.

The retry is restricted to CUDA 12.4 `bits/mathcalls.h` versus
`crt/math_functions.h` exception-specification conflicts for `cospi`, `sinpi`,
or `rsqrt` and their float variants. Unrelated or mixed errors are not retried;
failed retries retain both diagnostics. Effective flags and workaround state
are recorded for later identity projection. `--allow-unsupported-compiler`
is not a compatibility test or a substitute. CUDA Linux libraries are not
automatically added by this prerequisite flow. G3 pass 1 now removes ambient
target-sensitive SDK/compiler and pkg-config state before target discovery.

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

### 5.2 CPU tuning

The production target C/C++ policy is exactly `-march=native -mtune=native`
when `native_build` is enabled. LLVM-MinGW is the authority that interprets
`native`; the project checks only that the selected compiler accepts the
flags. It does not expand or independently validate compiler-selected ISA
features. G1 CPU data remains part of physical-target identity and runtime
evidence, not a compiler feature whitelist. Binaries are intended for the
physical Windows computer running WSL and are not claimed portable to
unrelated Windows CPUs.

G0A.2's compiler-native/explicit-specialization comparison, disassembly, and
Windows CPU/OS inspection were feasibility experiments only. The exact
observed `skylake-avx512` and feature-flag output is historical evidence, not
production policy; production does not reproduce or validate the compiler's
internal feature expansion.

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

### 5.7 Reusing the native CUDA/Vulkan fixes in WSL-Mingw

**Status and evidence.** Commit `447b555` implemented a shared native CUDA
profile, separate libvmaf/NVENC header selection, and verified Vulkan
header/registry pairing. Native Linux FFmpeg 8.1 and 9.0 builds completed with
these changes; both binaries list `scale_cuda`, `libvmaf_cuda`, `libplacebo`,
and `h264_nvenc`. An isolated native CUDA build also executed `scale_cuda` and
`libvmaf_cuda` on TITAN V. A forced older NVENC header selection was tested
with the available driver, not a physical older driver. See
`docs/CHANGELOG.md` for the scoped results.

These native results are **not Windows cross-build results**. G2.4/G2.5 validate WSL host
prerequisites and PTX, G2.6 validates isolated target headers and link
interfaces, and G0A.5 is a separate early Windows feature slice. Those earlier
results alone did not establish the production Windows CUDA path. Wave 8
subsequently accepted libvmaf CUDA and its Windows runtime path, and final G4
accepted the configured Windows FFmpeg stack. The current G5 and packaging
status is recorded at the start of this document.
The module-level mapping is in
[DeveloperReadme.md](DeveloperReadme.md#reusing-the-native-cudavulkan-work-for-wsl-mingw).

**CUDA profile and host/target split — G2.7, G3, G4.**

- Reuse `ResolvedCudaBuildHostInputs`, the selected-root NVCC resolver, the
  project-store `-ccbin`, PTX diagnostics, and the narrowly recognized
  CUDA 12.4/glibc retry. Keep architecture selection and overrides as described
  in §4; obtain default target facts from the fresh Windows probe, not WSL GPU
  detection. Preserve the configured required capabilities even when hardware
  is absent; obtain a sufficient explicit target or fail clearly rather than
  silently removing them.
- Feed the same selected NVCC, host compiler, architecture, and verified common
  flags to every requested CUDA consumer. `effective_flags` from the PTX probe
  include PTX-specific options: use `common_flags()` plus the consumer's own
  output-mode options instead of passing `--ptx` to a fatbin build. Do not
  introduce an unverified CUDA LLVM fallback or a blanket
  `--allow-unsupported-compiler`; keep the nonfree decision explicit.
- G2.4's `probe_status == "passed"` establishes PTX generation, not libvmaf
  fatbin support. The native `validate_cuda_output_modes()` also compiles a
  Linux host object; that object is **not** Windows COFF and must never be
  linked into the target. For the actual cross build, verify the required
  device-code modes and selected Toolkit's `bin2c`, then validate the libvmaf
  Meson flow: NVCC fatbin → host `bin2c` → generated C → LLVM-MinGW target
  object. Also validate or adapt libvmaf's `add_languages('cuda')` and Meson
  compiler sanity checks for this host/target split; do not assume Linux NVCC
  can generate Windows host objects. This flow and its Windows runtime
  behavior were unverified at G3; Wave 8 subsequently accepted this flow.
- libvmaf's current upstream Meson file adds its own `-gencode` options in
  addition to the selected architecture. Inspect and validate the **actual**
  effective NVCC commands and all requested architectures against the
  selected Toolkit; prepending one architecture does not remove upstream
  options. Resolve incompatibilities through a maintainable build-system
  adaptation, not a new patch for each host or SDK update.
- `cuda_profile_environment()` is a native build helper that sets
  `CUDA_HOME`, `CUDA_PATH`, `CUDACXX`, and NVCC flags. Do not inject it
  unchanged into Windows target dependency discovery. Construct a scoped
  environment for Linux host tools and a separate, filtered target environment;
  SDK executables are host tools, not Linux target libraries. The current
  filtering primitive does not remove `CUDACXX` or sanitize `PATH`; G3 must
  explicitly control those and the remaining compiler/discovery variables.

**NVENC/NVDEC and libvmaf headers — G3–G5.**

- G2.6 stages the pinned, project-managed nv-codec headers as a Windows
  target input. Production integration must keep NVENC/NVDEC eligibility and
  target runtime validation independent of NVCC kernel compilation.
  Registry/config declares the required feature; missing hardware does not
  silently remove it. Select target headers through an explicit compatibility
  policy and Windows target facts, never from WSL's NVIDIA driver.
- The current Windows probe records NVIDIA DLL file versions, but does **not**
  call `NvEncodeAPIGetMaxSupportedVersion()`. `get_nv_codec_component()` maps
  an exact reported API major and otherwise retains the registry default; that
  default is not proof of compatibility. Extend target probing or provide a
  sufficient explicit target policy/override, and report unknown or unmapped
  compatibility without silently dropping a required capability.
- Preserve the native separation when a driver-compatible NVENC release lacks
  CUDA driver declarations required by libvmaf: FFmpeg uses the selected
  target headers, while libvmaf can use the checksum-verified default
  nv-codec archive in a private include tree. Verify both Meson's target C
  checks and the NVCC fatbin include path. Record both project archive
  identities; the newer CUDA declarations alone do not prove that an older
  Windows driver implements every needed function. Make the target dependency
  order explicit: selected nv-codec headers before libvmaf, and libvmaf before
  FFmpeg.

**Vulkan-Headers and libplacebo — G3, G4.**

- G2.6 stages the pinned project Vulkan-Headers and uses the matching `vk.xml`
  from that source archive to validate generation of only the
  `vkGetInstanceProcAddr` `vulkan-1.dll` import interface required by the
  selected static libplacebo link path. G3/G4 must carry the verified
  header/registry pair into libplacebo and FFmpeg target builds. This verifies
  project-owned inputs, not the external SDK.
- Stage the same verified pair in libplacebo's bundled Vulkan-Headers
  dependency; adapt the existing `-Dvulkan-sdk=<target-prefix>` and
  `-Dvulkan-registry=<target-prefix>/share/vulkan/registry/vk.xml` options for
  the cross build. Reject a conflicting pair instead of mixing it with
  `VULKAN_SDK`. Adapt CMake/Meson cross files and paths rather than importing
  Linux SDK include, library, or pkg-config directories.
- Linux SDK shader/SPIR-V executables remain host tools. Windows-target
  glslang libraries and the proven Vulkan import interface come from target
  inputs; runtime uses the installed Windows loader/ICD. Preserve the static
  C++ runtime linkage requirement from §5.6. The existing native glslang
  search adjustment and pkg-config normalization are separate adaptations and
  must not be copied as an unverified cross-link recipe.

**Identity, resume, and acceptance — G2.7, G5, G7.**

- **G2.7 complete:** `prepare_target()` obtains fresh G1 facts, resolves
  selected project toolchains and external CUDA/Vulkan prerequisites,
  materialises and verifies the G2.6 target-input manifest, projects material
  inputs, then compares identity before selecting mutable state. TUI and
  headless routes share this path.
- Schema 2 extends the G1 identity. Its canonical build projection is
  SHA-256 hashed; the first 16 hex characters key the isolated workspace.
  Structured target, material-input, runtime, and diagnostic evidence is
  persisted in state and `workspace_identity.json`. G1-only/incomplete cross
  identities are rejected; native schema-1 identity remains readable.
- A changed or unknown Windows build identity must select an isolated prefix
  or stop without altering the old one; marking a few native component
  statuses pending is not sufficient. For a compatible identity, validate the
  Vulkan pair and CUDA build-tree markers before reuse, rebuilding affected
  consumers or stopping safely when invalid. Runtime-only drift invalidates
  acceptance separately. Do not hash, provision, or modify the external SDKs.
- G2.7 live acceptance used fresh physical-Windows probing, current
  ToolchainStore profiles, external CUDA/Vulkan tools, and verified G2.6
  materialisation in a disposable workspace. Same-input resume, changed
  material isolation, diagnostic-only drift, and G1-only legacy rejection
  were checked without altering maintainer state. This is preparation/identity
  acceptance, not a Windows cross-build.
- The configured component stack and G4 software runtime acceptance passed.
  G5 capability and G6 package/runtime status is recorded at the start of
  this document. Do not report absent or unsupported hardware as a pass.

## 6. Build identity and reuse policy

Schema 2 extends the accepted G1 model without replacing its separate Windows
target, build-input, runtime-acceptance, and diagnostic sections. The current
material projection comes from production-resolved inputs and is compared
before saved state or mutable artifacts may be trusted.

| Category | Examples | Why |
|---|---|---|
| Physical Windows target | G1 Windows OS/architecture, CPU identity/ISA fields, selected GPU/capabilities and relevant driver/runtime facts. | Preserves G1 workspace isolation, changed-host detection and runtime acceptance. CPU feature fields are not an LLVM-MinGW feature whitelist. |
| Backend and capabilities | `linux-cross-mingw`; configured CUDA mode/features, Vulkan selection, registry-selected OpenCL/NVIDIA codec features. | Feature configuration changes expected build contents. Enabled-but-unavailable prerequisites fail rather than being silently reclassified as disabled. |
| LLVM-MinGW | Profile/version, target triple/UCRT runtime, installed source/archive provenance and schema; no absolute store prefix. | Project compiler/ABI changes can alter PE/COFF output; relocation alone must not drift identity. |
| CPU tuning policy | Existing material configuration field `native_build`: either no tuning or the ordered target C/C++ flags `-march=native`, `-mtune=native`. | Distinguishes generic and native-tuned builds without persisting compiler-internal model/features. The selected LLVM-MinGW compiler is the authority for `native`. |
| Rust and target std | Rust profile/version/source provenance and sorted extras; `x86_64-pc-windows-gnullvm` target-std release/archive/checksum. | Compiler or target-std changes invalidate Rust artifacts; unordered extras do not create false drift. |
| CUDA project compiler | Selected GCC profile/version/source provenance and compiler role path relative to its prefix. | Changing the selected project-managed `-ccbin` invalidates CUDA output; unselected ambient GCC and absolute store paths do not. |
| Effective CUDA and external NVCC | Effective compute architecture, compatibility/workaround flags, and reported external toolkit/NVCC releases. | Architecture, flags, and NVCC release can change generated PTX/artifacts. NVCC/toolkit release is an **external material input**, not project-managed provenance. |
| Vulkan/OpenCL/NVIDIA target inputs | Pinned nv-codec, Vulkan-Headers, OpenCL-Headers, and ICD Loader definition provenance; `target-inputs.json` materialisation ID; generated `libvulkan.a`/`libOpenCL.a` recipe, definition, and output hashes. | Project header/interface changes affect compile/link output. Windows runtime DLLs are external and are not staged or hashed. |
| Vulkan host tools | Selected shader compiler version is material because it generates shader artifacts; `spirv-val` version is diagnostic validation metadata. | A shader compiler version change can change output; SDK paths and trees remain externally owned and unhashed. |
| Diagnostics only | Absolute CUDA/Vulkan SDK paths, `glslc`/`spirv-val` paths, full probe dumps, temporary target prefix, timestamps, logs, detailed device inventory. | Path relocation or incidental probe detail must not invalidate compatible artifacts. |
| Windows runtime acceptance | Windows loader/ICD, OpenCL dispatcher/platform/device, NVIDIA driver, and other accepted G1 runtime facts. | Runtime-only changes invalidate prior acceptance as defined by G1. `vulkan-1.dll`, `OpenCL.dll`, and NVIDIA driver DLLs are not project-owned inputs. |

WSL-Mingw target C/C++ builds use `-march=native -mtune=native` when
`native_build` is enabled. The physical Windows machine running WSL is the
intended target; the resulting binaries are not claimed portable to unrelated
Windows CPUs. LLVM-MinGW interprets `native`; the project neither expands nor
independently validates its selected ISA features. G1 CPU data remains part of
physical-target identity/runtime evidence, not a compiler feature whitelist.
Changing the physical target, selected LLVM-MinGW identity, or requested
policy changes compatibility through the existing identity semantics. CUDA
PTX, driver JIT, NVENC/NVDEC, Vulkan, and OpenCL acceptance bind to selected
device/runtime state.

Canonical sorted-key JSON and SHA-256 provide deterministic evidence and
workspace keys; no timestamp, temporary prefix, or absolute SDK/store path is
semantic content. The full structured projection is persisted in state and
the identity marker, while mismatch formatting summarizes large per-file
manifest changes rather than dumping the manifest. Material changes select an
isolated workspace. This does not make the project responsible for external
SDK lifecycle or every external tool version.

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

#### G1 implementation notes

- **Config:** `target_backend: native | linux-cross-mingw` (default `native`),
  `windows.target_override` (mapping, default none) and
  `windows.probe_timeout_seconds` (default 120).
- **Probe:** a fresh probe per build/resume via WSL interop (`powershell.exe`,
  argv without a shell, gzip+base64 `-EncodedCommand` bootstrap, explicit
  timeout). Missing/failed/timed-out/malformed/incomplete probes raise
  `WindowsProbeError` with an override hint; WSL Linux data is never
  substituted. A technology reported `unavailable` never matches saved state.
- **Overrides:** `mode: full` (default) replaces the probe and must state every
  section (`os`, `cpu`, `gpus`, `nvidia`, `vulkan`, `opencl`; use
  `state: absent` for hardware that is not installed). `mode: supplement`
  probes first; `os`/`cpu` fields merge per field, other sections are replaced
  as a whole. A failed probe is not rescued by a supplement.
- **Identity:** *build identity* (backend, target OS/arch, FFmpeg version,
  material config (including the `native_build` tuning choice), component
  versions/toolchain profiles) decides artifact reuse and names the
  workspace. *Runtime identity* (OS version/build, CPU/ISA, GPUs, NVIDIA,
  Vulkan, OpenCL) decides whether earlier runtime acceptance is valid.
  Edition, UBR, ICD manifests and `probed_at` are diagnostic only. Identities
  are canonical JSON with SHA-256 fingerprints verified on load.
- **Isolation:** WSL-Mingw builds run in
  `<workspace>/targets/linux-cross-mingw-<build_key>/` (prefix, `packages`,
  logs, `build_state.json`, `workspace_identity.json`). A material change
  selects a new directory; old artifacts are preserved until Cleanup.
- **Guard:** runs before reset/resume/config overwrite in the TUI and in
  `scripts/run_build.py` (exit code 2). It refuses, without modifying anything,
  a non-empty directory without a trusted marker, corrupt or legacy
  (identity-less) state, and build-identity mismatch. A corrupt state file is
  never treated as "no state". Legacy base-level state is preserved and
  ignored by this route. `StateManager` also refuses to trust
  `completed`/`system` or a resume point until the guard verified the state.
- **Runtime drift:** runtime-only differences on resume reuse artifacts but set
  `acceptance.status = invalidated` in the state. After a successful build the
  target is re-probed; drift invalidates acceptance and a failed re-probe is
  reported. Native Linux/WSL/macOS flows keep their behaviour: they record a
  minimal identity, adopt legacy state, and only refuse resume on a recorded
  identity mismatch.
- **Not in G1:** toolchain/SDK installation, backend selection (G3), the runtime
  acceptance suite (G5).

- **Scope / code:** Add a fresh Windows interop probe (module name may be
  `windows_probe.py`), explicit overrides, identity model and guard before
  resume/reuse. Integrate with `platform_detect.py`, `config.py`, `app.py`,
  `state.py`, `builder.py` and workspace selection.
- **Status:** [IMPLEMENTED — G1] `windows_probe.py` (+ `windows_probe_script.py`),
  `build_identity.py` and `target_guard.py` implement the probe, overrides,
  semantic identity and reuse guard; see "G1 implementation notes" below. G3
  pass 2 and G4 production integration have since passed. The G5 hardware
  validation matrix remains separate from G4 software runtime acceptance.
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
- **Status:** [PASS — G2] G1 PASS; G2.1 audit complete; G2.2
  complete (LLVM-MinGW managed toolchain + hardened `ToolchainStore`: checksum
  enforcement, staged/atomic install, provenance/stale detection, isolated
  `--store`/`--sources` offline provisioning). G2.3 complete (Rust 1.99.0
  `x86_64-pc-windows-gnullvm` std as a project-managed additive `extras`
  artifact of the same Rust prefix, required only for `linux-cross-mingw`;
  Git LFS mirror, composite provenance, atomic activation, offline acceptance;
  no rustup). G2.4 CUDA resolution (#10), the external-prerequisite foundation
  and CUDA PTX probe (#12) are implemented and accepted for the scoped host.
  G2.5 Vulkan prerequisites and host/target isolation (#14) are implemented and
  accepted for the scoped host. G2.6 target headers/interfaces, G2.7 identity
  integration, and G2.8 final offline closure/live prerequisite acceptance are
  complete; G2 is PASS. G2
  checks external WSL SDK prerequisites; it does not own or install those SDKs.
- **SDK ownership:** The CUDA SDK (example `/opt/cuda-12.4`) and Vulkan SDK
  (example `/opt/vulkan-sdk/1.4.363.1/x86_64`) are external prerequisites. Locate
  them from configured/documented paths or environment, verify presence and that
  the tools/interfaces required by the selected configuration are usable, and
  fail actionably when absent or unusable. Do not install them, store or hash
  them, or add registry entries for their tool versions. Keep explicit
  overrides. Distinguish a missing environment prerequisite from an absent
  offline project source.
- **CUDA host inputs (G2.4):** WSL-Mingw resolves
  `external_prerequisite_paths.cuda` > `CUDA_HOME` > `CUDA_PATH`; it takes NVCC
  only from the selected root. CUDA 12.4 maps to the existing ToolchainStore
  `gcc12` profile, whose verified `CC` is selected as `-ccbin`. The ambient
  `CUDA_NVCC_CCBIN` is ignored on this backend; native Linux permits that
  override and has its own verified store/system fallback policy. The
  resolution result keeps external SDK and project compiler provenance
  separate; the functional PTX probe consumes that result.
  Stale legacy GCC state requires successful scoped `--verify-only` before
  backfill, including matching compiler versions and existing source metadata.
  No Linux CUDA libraries are automatically added to target discovery.
- **Host/target SDK isolation (G2.5):** CUDA NVCC and Vulkan shader/SPIR-V
  executables are host tools, not Windows-target libraries or discovery
  prefixes. `filter_wsl_mingw_target_environment()` filters a copy of the
  parent environment: SDK selectors and Linux Vulkan loader variables are
  omitted, SDK paths are classified in structured diagnostics, and discovery
  paths are retained only under caller-supplied Windows-target roots. Host
  `PATH` remains available for tools. The Linux Vulkan SDK is no longer wired
  into the `linux-cross-mingw` builder or Linux platform-strategy environment;
  native Linux/WSL and macOS behavior is unchanged. This establishes the
  filtering contract; G3 pass 1 now constructs the target pkg-config,
  CMake/Meson, Autotools, and Rust projections and validates the discovery
  boundary without treating preserved host or system paths as target inputs.
- **G2.4/G2.5/G2.7 preparation integration:** `prepare_target()` shares the
  same WSL-Mingw preparation path between the TUI and headless runner. Fresh G1
  target probing, project/external prerequisite resolution, and verified G2.6
  target-input materialisation precede full identity comparison and workspace
  selection. CUDA architecture uses the explicit config/environment override
  when supplied, otherwise the minimum valid Windows-probed capability; it is
  not inferred from WSL GPU state or enumeration order. The typed
  `WslMingwPreparation` keeps Windows target facts, required project-store
  LLVM-MinGW/Rust (including target std), project-managed CUDA ccbin/effective
  compatibility inputs, and external CUDA/Vulkan SDK diagnostics in separate
  domains. Missing or incompatible
  required project toolchains fail preparation. Vulkan host
  shader/SPIR-V validation does not validate or replace Windows Vulkan runtime
  facts. CUDA/Vulkan SDK paths are configurable with
  `external_prerequisite_paths.cuda` and `.vulkan`; otherwise their documented
  environment variables are used. Disabled capabilities skip their SDK
  requirements, while enabled capabilities fail preparation when required
  inputs are missing. G2.7 projects the resolved toolchain, effective CUDA,
  configured capability, shader compiler, and target-input evidence into
  schema-2 identity. G3 pass 1 implemented cross-environment construction and
  acceptance. G3 pass 2 is accepted: role-scoped host/target/MIXED contracts,
  shared Autotools/CMake/Meson/Make/custom/Cargo projections, identity-local
  Cargo target configuration, typed transitive target-link/runtime metadata,
  and Vulkan/OpenCL target-interface projections are validated. The CUDA
  contract selects NVCC, GCC `-ccbin`, architecture and scoped compatibility
  flags; the integrated probe generates PTX and fatbin, converts fatbin with
  selected host `bin2c`, compiles generated C with LLVM-MinGW to COFF, and
  links a PE32+ harness without Linux object/runtime leakage. The selected
  CUDA 12.4.99 supports libvmaf's effective upstream `compute_50`, `75`, `80`,
  and `90` requests; its version branch excludes `compute_100`/`120`. Upstream
  supplies its own fatbin/gencode list and enables Meson's CUDA language with
  NVCC, which may introduce NVCC host-object/compiler checks; the accepted
  production contract therefore uses only the proven fatbin → bin2c → target-C
  split and does not treat NVCC as the Windows host compiler. At G3 acceptance,
  full libvmaf adaptation and CUDA-enabled component builds remained
  fail-closed pending G4; Wave 8 subsequently accepted them.
  Project-managed Rust/Cargo produced a Windows staticlib consumed by an
  LLVM-MinGW C harness; Cargo build scripts remain host-native. Positive and
  negative C++ transitive-link probes confirmed libc++/libunwind requirements.
  G3 acceptance also covers the host-generator/MIXED pipeline, target Vulkan
  and OpenCL imports, target path-contamination checks, and reached legacy
  branch isolation. At G3 acceptance, TUI and headless routes stopped before
  the G4 component graph; they now build the selected stack and continue to
  G4 final integration without requiring historical wave state.
  Live preparation passed with the working store's LLVM-MinGW 20261104,
  Rust 1.99.0 plus Windows target std, and GCC 12.5.0. The fresh Windows probe
  reported TITAN V compute capability 7.0; CUDA 12.4.99 produced validated
  `compute_70` PTX with the scoped `-Xcompiler=-U_GNU_SOURCE` workaround, and
  Vulkan SDK 1.4.363.1 `glslc`/`spirv-val` validated the host shader probe.
  The isolation filter removed 11 simulated CUDA/Vulkan SDK entries while
  preserving the parent environment. The native saved configuration was not
  changed; acceptance used an in-memory WSL-Mingw selection and a temporary
  identity workspace.
- **G2.6 target inputs and link interfaces:** `components.yaml` declares
  `target_backends: [linux-cross-mingw]` independently of host eligibility.
  `nv-codec` 13.0.19.1, Vulkan-Headers 1.4.357.0, and OpenCL-Headers
  2026.05.29 are staged from checksum-pinned project archives; OpenCL-Headers'
  `CL/` source tree is materialised as `include/CL`. No CUDA or Vulkan SDK
  headers/libraries, OpenCL host headers, loaders, or NVIDIA driver DLLs enter
  the target prefix. NVENC/NVDEC and CUDA Driver APIs remain dynamically
  resolved at runtime and need no import library. The selected static
  libplacebo link path needs `vkGetInstanceProcAddr`, so a one-symbol
  `vulkan-1.dll` import library is generated using the pinned Vulkan registry.
  FFmpeg's OpenCL configure/link path uses `-lOpenCL` and calls OpenCL API
  functions; the interface is generated from the pinned OpenCL-ICD-Loader
  `OpenCL.def`, without building or staging that loader. Both outputs are
  generated by installed LLVM-MinGW `dlltool` and validated as x86-64 Windows
  COFF archives.
  `scripts/prepare_target_inputs.py` stages into a fresh caller-selected prefix
  offline and writes `target-inputs.json` with source checksums, target
  backend, interface recipe/input identities, and file hashes consumed by
  G2.7 identity. Isolated acceptance materialised only declared headers,
  `lib/libvulkan.a`, `lib/libOpenCL.a`, and the provenance manifest; focused
  LLVM-MinGW header compiles and Vulkan/OpenCL link probes passed. Inspection
  rejected ELF, host/runtime libraries, metadata, forbidden SDK paths, and
  PoC/session paths. No full target dependency prefix or build environment is
  created at G2.6.
- **Tests:** Registry parsing, version resolution, checksums, target std
  compatibility, `-ccbin` version check, SDK path/tool usability checks,
  actionable missing-prerequisite/source errors, offline provisioning with
  empty external caches and no unexpected PATH selection.
- **Inputs/artifacts:** Prepared external WSL SDK installations; pinned
  project artifacts and licenses/checksums; project store archives, manifests,
  installed tools and provenance.
- **Pass:** selected project-managed closure provisions offline from staged
  sources; prerequisite SDK checks use only explicit selected paths and fail
  clearly when unusable.
- **Non-goals:** SDK system installation, `/opt` mutation, Linux driver,
  SDK archival/hashing/provenance, complete component stack.
- **Reuse:** G0's toolchain archive identity; G0A.1's target-std requirement;
  G0A.3/G0A.5's versioned GCC/NVCC pairing; G0A.4's shader tools. Do not
  import isolated PoC archives or metadata as production without registry
  verification.

### G3 — `linux-cross-mingw` backend and platform strategy

- **Status:** G1 **PASS**; G2 **PASS**; G3 **PASS**; G4 **PASS**; G5 **PASS**.
- **Pass 1 completed:** production strategy selection; typed host/target tool
  projections; identity-workspace `prefix/`, `build/`, `generated/`,
  `packages/`, and `diagnostics/`; safe G2.6 target-input reuse; explicit
  LLVM-MinGW compiler/binutils mapping; host-native tool separation;
  target-only pkg-config wrapper and contamination checks; deterministic CMake
  and Meson cross files; Autotools tuples/environment; and identity-local Rust
  and deferred CUDA host-tool projections. The TUI and normal headless cross
  build now proceed to final G4 acceptance when the component waves are
  accepted; cross `--dry-run` reports G4 Wave-0 selection without building.
- **Real pass-1 acceptance:** C/C++ and resource compile/link, static archive
  link, PE32+ x86-64/COFF/UCRT inspection, pkg-config isolation and malicious
  metadata rejection, minimal CMake configure/build, Meson setup/build, and
  Autotools cross-mode configure all pass with project-managed LLVM-MinGW.
- **Isolation:** No MSYS path rewriting or shell wrapping is used. Linux
  libraries, headers and SDK metadata are not target discovery roots; the
  target compiler/binutils are absolute paths and host executables remain
  separate.
- **CPU tuning policy:** when `native_build` is enabled, target C/C++ uses
  exactly `-march=native -mtune=native` in that order. LLVM-MinGW alone
  interprets `native`; a compiler acceptance probe does not inspect its
  internal feature expansion. G1 CPU data is identity/runtime evidence only.
- **Pass 2 accepted:** role-scoped host/target/MIXED environments and prefixes;
  Cargo host/target separation; transitive target-link/runtime metadata;
  Vulkan/OpenCL target interfaces; shared build-system projections; CUDA
  fatbin/bin2c/generated-C COFF and PE acceptance; Rust staticlib/C harness;
  positive/negative C++ runtime metadata; and a generic mixed-role generator
  pipeline. At G3 acceptance, CUDA-enabled FFmpeg/libvmaf recipes remained
  fail-closed pending their G4 adapters. No full component
  stack or production FFmpeg cross-build was run.
- **Legacy and production boundary:** reached MSYS-specific FFmpeg path
  conversion is restricted to the legacy UCRT64 route; `linux-cross-mingw`
  uses no MSYS shell/path semantics. TUI and normal headless builds run
  identity-scoped G3 acceptance, build the selected stack and continue to G4
  integration; cross `--dry-run` performs no component builds.
- **Final acceptance:** selected CUDA/NVCC/GCC/bin2c, Rust/Cargo, C/C++/COFF
  and PE, Autotools, CMake, Meson, target-only pkg-config, Vulkan/OpenCL
  interfaces, target contamination guards, and native regression suite pass.
  Schema-2 identity remains unchanged; CPU tuning remains exactly
  `-march=native -mtune=native` with compiler acceptance only. At G3
  acceptance G4 had not started; it has since passed.
- **Link requirements:** Model libc++/libunwind and other static C++ runtime
  needs transitively in target metadata/link composition. Handle Vulkan and
  OpenCL target interfaces only where the actual dependency requires them.
- **Tests:** Resolver and config; generated compiler/environment/target
  prefix; pkg-config contamination rejection; generated CMake/Meson files;
  explicit SDK/NVCC/`-ccbin`; native Linux, native WSL/Linux and macOS
  regression protection; migration/removal tests for legacy Windows branches
  touched by this gate (the old MSYS2 tests are not acceptance requirements).
- **Inputs/artifacts:** G2-resolved compilers, tool metadata, target headers
  and import interfaces; generated cross files and dry-run commands.
- **Pass:** dry-run core commands use intended host tools and Windows target
  tools; target lookups cannot resolve WSL libraries; native Linux, native
  WSL/Linux and macOS backends remain unchanged except tested identity safety.
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

### G5 — consumer hardware-feature matrix

- **Scope / code:** Validate production CUDA/NVIDIA, Vulkan/libplacebo and
  supported Vulkan Video and OpenCL capabilities used by consumers.
  QSV/oneVPL and AMF are relevant when their hardware is present.
  Windows-specific D3D11VA, D3D12VA and DXVA2 are out of scope unless
  a consumer requires them. Neural support and unspecified future filter
  chains are deferred. Maintain independent results for relevant features.
- **Tests:** Build-presence assertions independent of hardware; per-device
  runtime tests only for hardware present and feature-supported; verify
  selected device and actual hardware execution, not software fallback.
  Report `PASS`, `FAIL`, `ABSENT`, `UNSUPPORTED`, `OUT_OF_SCOPE` and
  `DEFERRED` distinctly.
  Do not count unavailable hardware as a pass or out-of-scope APIs as failures.
- **Inputs/artifacts:** G1 device/runtime probe, G2 headers/SDK prerequisites,
  feature config, explicit expected matrix and suitable test media; acceptance
  report tied to runtime identity.
- **Pass:** Each applicable consumer-required production capability performs
  meaningful runtime work; absent/unsupported hardware is classified explicitly.
- **Non-goals:** Do not silently drop configured capabilities or report an
  unsupported capability as a successful runtime test.
- **Reuse:** G0A.3 CUDA/PTX/JIT, G0A.4 Vulkan/OpenCL loaders, G0A.5 FFmpeg
  NVIDIA/CUDA/Vulkan/OpenCL/libplacebo execution techniques. Test the accepted
  production binaries, never PoC binaries. Reuse completed G5 evidence only
  after contract, G4 artifacts, runtime identity and staged DLLs match.

### G6 — Windows packaging and runtime policy — PASS

- **Scope / code:** The production `release_bundle.py` path packages the
  accepted FFmpeg binaries and hash-verified LLVM-MinGW runtime closure.
  Runtime metadata joins PE imports with component runtime declarations and
  G5-accepted dynamically loaded NVIDIA, Vulkan and OpenCL interfaces.
- **Tests:** Repeated packaging; PE import/recursive DLL closure comparison;
  relocated bundle launch with a controlled Windows PATH; software operation
  and representative CUDA/libvmaf, Vulkan and OpenCL smoke tests.
- **Inputs/artifacts:** Identity-specific G4/G5 artifacts, release
  `manifest.json` and `g6-acceptance.json`.
- **Pass:** The package runs from the relocated path on the recorded target,
  includes project/toolchain-owned runtime DLLs and classifies external
  Windows/system/driver dependencies without copying them.
- **Non-goals:** Clean-machine or cross-host portability; bundling Windows
  system/UCRT, GPU drivers, Vulkan/OpenCL runtimes or optional `VSScript.dll`;
  legal redistribution of the accepted GPL/nonfree configuration.
- **Reuse:** G4/G5 acceptance artifacts are verified and reused; G5's complete
  hardware matrix is not rerun.

### G7 — regression, documentation, and full acceptance — CLI PASS

The command-line gate below remains an engineering record. Final maintainer
acceptance also exercised the normal interactive TUI: clean Linux/native
FFmpeg 8.1, clean Linux/native FFmpeg 9.0, and clean Windows FFmpeg 9.0 via `9.0-windows` all
passed without observed build or runtime problems. No timings, component
counts, or additional hardware results are inferred from this acceptance.

The normal headless production workflow completed these independent FFmpeg
9.0 acceptance runs in BASE-then-PREFERRED order. Neither clean workspace
contained historical G4 wave acceptance state.

| Profile | Target identity | Clean full build | Identical second invocation |
|---|---|---|---|
| BASE | `eb96209a1a3d4ba5` | 40 built, 0 reused; 1617.08 s | 0 built, 40 reused; 229.51 s |
| PREFERRED | `4a8b548bf74703f7` | 46 built, 0 reused; 1867.13 s | 0 built, 46 reused; 365.08 s |

Both clean records have `clean_full_build: true`. BASE configuration SHA-256
is `bd79ed0dbbd47f490f4c1e2db9c93de2d001e2c6e01b5db5b6200099a595c51b`;
PREFERRED is `26915b360b0e17cf9efd515404cf6b1ce33f9e01bc7fd76e974e2fd77ac6d1be`.
Their identity workspaces are under `workspace_g7_base/targets/` and
`workspace_g7_preferred/targets/`, respectively. Original clean invocation
records remain under `g7-runs/` even after `g7-acceptance.json` records reuse.
Evidence snapshots and exact input/effective YAML profiles are preserved in
the G7 session's `files/` directory
(`~/.copilot/session-state/f2138ce7-a169-47a0-8c1c-a9844d6b160f/files/`):
`G7_{BASE,PREFERRED}_{clean,reuse}_PASS.json`,
`G7_{BASE,PREFERRED}_reuse_verification.json`, and
`G7_{BASE,PREFERRED}_effective.yaml`.

BASE passed software H.264 encode/probe/decode and relocated controlled-PATH
startup without hardware tests. PREFERRED additionally passed representative
CUDA scaling, CUDA libvmaf, Vulkan scaling/libplacebo and OpenCL filtering on
the recorded Windows target. Both bundles contain `ffmpeg.exe`, `ffprobe.exe`,
the four accepted LLVM-MinGW DLLs and the release manifest. Payload hashes
were unchanged on repeated packaging. Reuse left all 127 BASE and 134
PREFERRED build logs/binaries unchanged by both hash and modification time.
The immutable reference and live configuration retain SHA-256
`97d8331cfedbdb0f51623112ac041fd9d0717b67f23b19da9cf3ef99b5a166d1`;
the PREFERRED input changes only the target backend.

Earlier diagnostic attempts do not count as clean acceptance. A first
PREFERRED clean build passed runtime but exposed glslang inventory growth on
reuse. Installing the complete pinned SPIR-V headers inside the target
glslang builder before recording acceptance fixed ownership/invalidation;
PREFERRED was then rebuilt entirely clean and passed reuse with all 83
SPIR-V files already recorded. BASE does not select glslang and was unaffected.
`G7_scope_impact.json` verifies that this isolated cross-glslang change is the
only production-code fingerprint difference from accepted BASE. Independent
review confirmed the affected-profile rebuild boundary.

Final regression checks passed: 917 tests passed, one skipped, Black passed,
the frozen mypy baseline passed with zero errors, and `git diff --check`
passed. The preceding full clean linux-wsl2 build remains preserved,
reference-only at `~/Temp/workspace_90`; no new full macOS build or arbitrary
cross-host portability is claimed. No requested software functionality was
removed; no Windows FFmpeg 8.1 acceptance or TUI integration was performed.

- **CLI production path:** `scripts/run_build.py --config PROFILE.yaml
  --workspace WORKSPACE` builds the effective selected dependency graph in
  dependency order, applies the existing installed-output contracts, then
  builds FFmpeg and performs profile-appropriate runtime/package acceptance.
  Manual `--g4-wave*` invocations are diagnostic tools, not prerequisites.
  Matching invocations validate installed/source hashes before reusing work.
  `--resume` permits diagnostic retries; a final clean full acceptance must
  build every selected component in one fresh invocation.
- **Mandatory profiles:** BASE and PREFERRED both use FFmpeg 9.0,
  `linux-cross-mingw`, GPL/nonfree-capable software and release packaging.
  BASE sets `native_build: false`, `enable_hardware_acceleration: false`,
  `cuda_build_mode: disabled`, `enable_libvmaf_cuda: false` and
  `enable_libplacebo_vulkan: false`; it retains software libvmaf and OpenMP.
  PREFERRED preserves the maintainer's immutable reference configuration,
  changing only the target backend. It retains native tuning, required CUDA,
  NVENC/NVDEC/CUVID, CUDA libvmaf, Vulkan/libplacebo and OpenCL.
- **Clean boundary:** Remove only the profile's explicitly selected build
  workspace, including target prefix, build trees, component state, binaries,
  release output and acceptance files. Preserve the checkout, Python
  environment, toolchain store, sources, ordinary caches, external SDKs,
  Windows runtimes/drivers and the reference configuration. The preserved
  native `~/Temp/workspace_90` is reference-only and never a build input.
- **Normal evidence:** `cross-components-acceptance.json`,
  `g4-final-acceptance.json`, `g7-runtime-acceptance.json`,
  `g7-package-acceptance.json`, release `manifest.json`, and
  `g7-acceptance.json` with immutable invocation records in `g7-runs/`.
  The aggregate records effective configuration/hash, target identity,
  production-code fingerprint, clean provenance, components/features and
  built/reused lists. A reuse invocation is not itself a clean full build;
  its corresponding original full-build record must also be retained.
- **UI boundary at G7 acceptance:** the G7 gate recorded headless CLI
  acceptance only; it did not cover the TUI. Final maintainer TUI acceptance
  is recorded at the start of this document.
- **Scope / code/docs:** Run the complete configured build and acceptance
  flow; native Linux, native WSL/Linux and macOS regression protection as
  applicable; migration/removal tests for any remaining legacy Windows
  branches (no MSYS2 regression preservation); same-identity resume and
  incompatible-identity rejection;
  verify the complete Windows bundle and host-applicable hardware matrix.
  Update `README.md`, `docs/DeveloperReadme.md`, and
  `docs/CHANGELOG.md`; the MSYS2-UCRT64 route is already documented as
  replaced, so confirm no stale MSYS2 Windows instructions remain.
- **Tests:** Full `pytest tests/`, `black --check .`,
  `python scripts/check_mypy_baseline.py`; full configured component set;
  resume/interruption; identity drift; package checks; all configured
  capabilities and host-applicable runtime tests.
- **Inputs/artifacts:** Clean identity-specific workspace, offline project
  inputs, prepared external SDKs, Windows runtime probe and bundle.
  Preserve per-capability acceptance statuses for unavailable hardware.
- **Pass:** Full configured stack builds and runs on the recorded host,
  regressions pass, incompatible state cannot be reused, package/runtime
  checks pass, and docs accurately record unvalidated capabilities.
- **Non-goals:** Claiming arbitrary Windows-host portability or forcing
  acceptance of absent devices.
- **Reuse:** Early evidence is an acceptance-test blueprint only; G7 must
  exercise production-built artifacts and current host identity.

### Post-G7 stabilisation — source, tool and provenance closure

- **libjxl sources:** the nested Brotli and Highway revisions pinned by libjxl
  0.12.0's `deps.sh` are project-managed Git LFS archives in
  `third_party/sources` (`libjxl-brotli-028fb5a2….tar.gz`,
  `libjxl-highway-457c8917….tar.gz`). `jxl_sources.py` resolves them through
  the project downloader: local mirror first, verified network fallback only
  with `allow_network_downloads`. It checks the sha256, the `deps.sh` pins and
  the archive roots, and names an unresolved LFS pointer explicitly.
- **cargo-c:** the `cargo-c` 0.10.25 binary profile is required by the
  WSL-Mingw platform profiles alongside LLVM-MinGW and Rust, acquired offline by
  `scripts/prepare_toolchains.py --backend linux-cross-mingw` and shared by the
  TUI and headless preparation. `RustTargetTools.cargo_cinstall` carries the
  store executable, which must sit inside its verified prefix. The rav1e
  builder version-checks it and invokes it by absolute path. PATH order is not
  a selection mechanism, and missing or mismatched store state fails closed.
- **x265 identity:** registry 4.2 = upstream tag 4.2, commit
  `e444744c03978c1fb4e037168967020cf2648427`, archive sha256 `0a3d41f6…ac8ed33`,
  `X265_BUILD 216`. The tag's own stale `x265Version.txt` makes upstream
  `Version.cmake` (which reads it when a host `git` exists) generate
  `4.0+1-6318f22` and pkg-config `Version: 4.0`. That generated string is
  legitimate upstream output, not evidence of a different source. FFmpeg
  checks only `X265_BUILD`. Both relationships are verified fail-closed on the
  cross path, and the source is unpatched.
- **Post-G7 PREFERRED regression PASS:** a new clean full FFmpeg 9.0
  `linux-cross-mingw` build (target `a457dec65db7f3e9`, offline sources, no
  `~/.cargo/bin` on PATH, stale `/usr/bin` cargo-c 0.10.16 present) built all 46
  components in 1991.85 s. rav1e used the store `cargo-cinstall`, libjxl's
  nested sources came from the local mirror, and x265 produced the recorded
  metadata. Software, CUDA scale, CUDA libvmaf, Vulkan/libplacebo, OpenCL and
  relocated-package acceptance passed. An identical rerun reused all 46
  components in 350 s; only the release manifest's `generated_at` changed. BASE
  was not rerun.

## 8. Open issues, grouped by resolving gate

| Gate | Open issue | Required resolution |
|---|---|---|
| Future feature | Neural processing and unspecified consumer interoperability. | Select requirements through consumer architecture and develop/accept separately; not required by G1–G6. |
| Future coverage | Hardware absent or unsupported on TITAN V and untested codec/profile combinations. | Retain their explicit classifications; validate when relevant hardware and consumer requirements exist. |

No separate feasibility risk remains for LLVM-MinGW/UCRT smoke execution,
contained C ABI mixed Rust/C++ use, native compiler tuning on the tested
configuration, CUDA PTX/JIT, tested NVENC/NVDEC, Vulkan/OpenCL runtime,
or the early FFmpeg 9.0 feature slice. These are verified within their stated
scope, not universally.

## 9. Evidence provenance and limits

Retained PoC evidence was found under
`/home/viktor/.copilot/session-state/008992f9-d4d6-4f56-9631-29d787c28745/files/`.
The phase-specific summaries/logs cited in §2 are the sources actually used.
G0A.2-G0A.5 did not have a single retained final report; conclusions above
come from their retained commands, status files, runtime logs, feature lists,
and artifacts. G0A.5's `input-hashes-and-versions.txt` records the source
archives used in that PoC.

G0A.1/G0A.5 PoC artifacts and temporary metadata are retained for evidence
only. Production code and tests must independently resolve inputs, build,
link, inspect and execute the relevant behavior. The E0.1 CUDA reconstruction
is historical evidence that CUDA 12.4 could be made usable in WSL; it is not a
project-owned artifact and never a Windows target component.

## 10. Ordered implementation checklist

1. **G1 — PASS:** Fresh Windows interop probe and identity guard; tested fresh,
   changed, failed, overridden, legacy and same-identity cases.
2. **G2 — PASS:** Register/provision the complete project-managed input
   closure offline; validate explicit installed CUDA/Vulkan SDK prerequisites
   and selected host compiler; establish reproducible schema-2 identity.
3. **G3 — PASS:** establish the backend strategy, isolated target
   environment/cross files, host/target/MIXED build contracts, reusable CUDA
   fatbin/bin2c/COFF flow, Rust/Cargo target integration, transitive link
   metadata, shared builder projections, and integrated production-path
   acceptance.
4. **G4 — PASS:** Cross-build the full configured software dependency stack; verify
   PE/COFF, no Linux leakage, runtime and resume.
5. **G5 — PASS:** Validate consumer-relevant production hardware features,
   retaining absent, unsupported, out-of-scope and deferred statuses.
6. **G6 — PASS:** Build and validate the Windows bundle with explicit DLL and
   external system/driver runtime policy.
7. **G7 CLI — PASS:** Separate clean full BASE/PREFERRED production builds,
   runtime/package acceptance, identical-run reuse and regression checks
   passed; user/developer docs and changelog updated. This gate did not
   exercise the interactive workflow.
