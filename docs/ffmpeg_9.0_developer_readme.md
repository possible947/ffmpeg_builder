# FFmpeg 9.0 Integration — Developer Readme

## 1) Goal

Add **FFmpeg 9.0** build support to `ffmpeg_builder` while preserving FFmpeg 8.1 as the default configuration and refreshing component versions to current stable releases where practical.

---

## 2) Current repository baseline (what exists today)

The current project is centered on FFmpeg 8.1:

- Default config version: `8.1`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/config.py`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/build_config.yaml`
- Target component entry:
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/components.yaml` (`name: ffmpeg`, `version: '8.1'`)
- Tests hard-code 8.1 assumptions:
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/tests/test_components.py`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/tests/test_config.py`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/tests/conftest.py`
- Build orchestration for FFmpeg configure/make/install:
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/builder.py` (`build_ffmpeg`)
- FFmpeg flags are assembled from component metadata (`ffmpeg_configure_flag`) + platform logic:
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/components.py` (`get_ffmpeg_configure_flags`)

This means FFmpeg version support is currently **single-version-oriented** and tightly coupled to component metadata.

---

## 3) Upstream FFmpeg 9.0 research summary

> Note: direct access to `ffmpeg.org` was blocked in this environment. Upstream data was collected from the official GitHub mirror and release artifacts.

### Verified upstream points

1. **FFmpeg 9.0 tag exists**: `n9.0` (repo: `FFmpeg/FFmpeg`).
2. **RELEASE_NOTES** identifies release `9.0 "Lei"` and states it follows 8.1 by ~4 months.
3. **Changelog highlights relevant to builder compatibility**:
   - Added ONNX Runtime DNN backend support.
   - Removed CELT decoding support.
   - Removed deprecated NVENC options and support for pre-11.1 SDK versions.
4. **Configure interface delta vs 8.1** (important for this repository):
   - Added option: `--enable-libonnxruntime`
   - Removed options: `--enable-libglslang`, `--enable-libshaderc`, `--enable-libcelt`
   - `libnpp` support path is effectively removed/deprecated (configure warns that enabling it does nothing).
5. **External libraries doc (`doc/general_contents.texi`)**:
   - New external section for **ONNX Runtime** in 9.0.

---

## 4) Impact analysis for `ffmpeg_builder`

## Critical compatibility risk

In current `components.yaml`, the `glslang` component contributes:

- `ffmpeg_configure_flag: --enable-libglslang`

Because FFmpeg 9.0 no longer accepts this option, current flag assembly would cause FFmpeg configure failure if switched to 9.0 without adjustments.

## Architecture-level implications

1. **Component metadata remains the right control plane** (no need to abandon declarative model).
2. We need **version-aware or profile-aware FFmpeg flag handling** so flags valid in 8.1 but removed in 9.0 are not passed.
3. Vulkan/libplacebo workflow should remain, but **glslang becomes an internal dependency for libplacebo**, not an FFmpeg configure flag source.
4. Tests and docs assume a single default version and must be updated in sync.

---

## 5) Component/version strategy for FFmpeg 9.0

The requested direction is: keep a **similar component set** to 8.1, but use more current component versions.

### 5.1 Must-do version/flag changes

1. Switch FFmpeg component from `8.1` to `9.0`.
2. Remove/guard obsolete FFmpeg flags for 9.0:
   - `--enable-libglslang`
   - `--enable-libshaderc` (if ever introduced by metadata)
   - `--enable-libcelt` (not currently used, but should remain absent)
3. Keep `nv-codec-headers` at a version compatible with FFmpeg 9.0 NVENC expectations (>= 11.1; current project version already satisfies this, but can be refreshed).

### 5.2 Candidate component refreshes (internet-verified examples)

> Superseded by the complete survey in [5.4](#54-complete-component-version-survey-verified-2026-08-31), which covers all 63 components.

Compared with current repository pins, newer tags exist for key hwaccel components:

- `glslang`: current `16.2.0` → latest observed `16.5.0`
- `Vulkan-Headers`: current `1.4.341.0` → newer observed `1.4.357.0`
- `OpenCL-Headers`: current `2025.07.22` → newer observed `2026.05.29`
- `OpenCL-ICD-Loader`: current `2025.07.22` → newer observed `2026.05.29`
- `nv-codec-headers`: current `13.0.19.0` → newer observed `13.1.15.0` — **caution:** `13.1.15.0` restructures `NV_ENC_CLOCK_TIMESTAMP_SET` (splits `countingType` into `countingTypeLSB`/`countingTypeMSB`) and breaks compilation against FFmpeg 8.1's `nvenc.c`, which still targets NVENC SDK 13.0 feature checks (`nvenc.h:104`). Verify FFmpeg 9.0's `nvenc.h` has updated its `NVENCAPI_CHECK_VERSION` gates to 13.1 before bumping this pin (see `docs/CHANGELOG.md`, 2026-09-05 entry).

`libplacebo` (`7.360.1`) and `oneVPL` (`2.17.0`) are already aligned with latest observed releases.

### 5.3 ONNX Runtime decision

FFmpeg 9.0 introduces `--enable-libonnxruntime` as optional. For the first 9.0 integration iteration, it is safer to:

- keep ONNX Runtime **disabled by default**,
- document it as future optional component,
- add it only after cross-platform packaging/build rules are defined.

### 5.4 Complete component version survey (verified 2026-08-31)

All 63 components in `components.yaml` were checked against upstream release pages/tags on **2026-08-31**. Legend:

- **BUMP** — a newer stable release exists; recommended target listed.
- **UP-TO-DATE** — the current pin is the latest stable release.
- **FINAL** — the current pin is the last release of the project (no successor; effectively unmaintained).
- **ROLLING** — no versioned releases; pinned by commit hash.

#### Build tools

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| giflib | 5.2.2 | 6.1.3 (2026-04-12) | BUMP | Major version: SourceForge folder changes `giflib-5.x/` → `giflib-6.x/`, so the `url` in `components.yaml` must be updated |
| pkg-config | 0.29.2 | 0.29.2 | UP-TO-DATE | No new release since 2017 |
| yasm | 1.3.0 | 1.3.0 | FINAL | Project effectively unmaintained |
| nasm | 3.01 | 3.02 | BUMP | |
| zlib | 1.3.2 | 1.3.2 (2026-02-17) | UP-TO-DATE | |
| m4 | 1.4.20 | 1.4.21 (2026-02-06) | BUMP | |
| autoconf | 2.72 | 2.72 | UP-TO-DATE | 2.72.90 is a beta, not a stable release |
| automake | 1.18.1 | 1.18.1 (2025-06-25) | UP-TO-DATE | |
| libtool | 2.5.4 | 2.5.4 | UP-TO-DATE | |
| cmake | 4.2.3 | 4.4.3 | BUMP | |
| meson | 1.8.2 | 1.12.0 (2026-08-10) | BUMP | |
| ninja | 1.12.1 | 1.13.2 | BUMP | |

#### Crypto

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| gettext | 0.22.5 | 0.26 (2025-07-19) | BUMP | |
| openssl | 3.6.1 | 3.6.4 (2026-08-25) | BUMP | Security patch release; stay on the 3.6 series (OpenSSL 4.0 exists but is out of scope for this integration) |
| gmp | 6.3.0 | 6.3.0 | UP-TO-DATE | |
| nettle | 3.10.2 | 3.10.2 (2025-06-26) | UP-TO-DATE | |
| gnutls | 3.8.12 | 3.8.13 (2026-04-29) | BUMP | Security patch release |

#### Video codecs

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| dav1d | 1.5.3 | 1.5.4 (2026-07-14) | BUMP | |
| svtav1 | 4.0.1 | 4.2.0 (2026-07-14) | BUMP | 4.1.0 was 2026-03-23 |
| rav1e | 0.8.1 | 0.8.1 (2025-06-16) | UP-TO-DATE | Weekly pre-releases exist (latest p20250902); 0.8.1 remains the latest stable |
| x264 | 0480cb05 (commit) | rolling git | ROLLING | No versioned releases; latest official prebuilt is r3222 (b35605a, 2025-06-08) — re-pin to a current `stable`-branch commit with a fresh sha256 |
| x265 | 8be7dbf (commit) | 4.2 (2026-04-19) | BUMP | Current canonical versioned release; 4.3 is not published upstream |
| libvpx | 1.16.0 | 1.17.0 (2026-08-07) | BUMP | ABI incompatible with 1.16 |
| xvidcore | 1.3.7 | 1.3.7 | FINAL | |
| vid_stab | 1.1.1 | 1.1.1 | FINAL | |
| av1 (libaom) | 3.12.0 | 3.13.3 (2026-04-02) | BUMP | 3.13.1/3.13.2 were 2025-09-05 / 2026-03-17 |
| zimg | 3.0.6 | 3.0.6 | UP-TO-DATE | |

#### Audio codecs (incl. LV2 stack)

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| lv2 | 1.18.10 | 1.18.10 | UP-TO-DATE | |
| serd | 0.32.8 | 0.32.10 (2026-06-08) | BUMP | |
| pcre | 8.45 | 8.45 | FINAL | Final release of PCRE1 |
| zix | 0.8.0 | 0.8.0 | UP-TO-DATE | 0.8.1 appears in NEWS as `unstable`, not a release tag |
| sord | 0.16.22 | 0.16.22 (2026-02-10) | UP-TO-DATE | 0.16.23 appears in NEWS as `unstable`, not a release tag |
| sratom | 0.6.22 | 0.6.22 (2026-02-10) | UP-TO-DATE | |
| lilv | 0.26.4 | 0.28.0 (2026-06-08) | BUMP | 0.26.4 was 2026-02-10 |
| opencore (opencore-amr) | 0.1.6 | 0.1.6 | FINAL | |
| lame | 3.100 | 3.100 | FINAL | 3.100.1 is "under construction" upstream, no release |
| opus | 1.6.1 | 1.6.1 (2026-01-14) | UP-TO-DATE | 1.6 was 2025-12-15 |
| libogg | 1.3.6 | 1.3.6 | UP-TO-DATE | |
| libvorbis | 1.3.7 | 1.3.7 | UP-TO-DATE | |
| libtheora | 1.2.0 | 1.2.0 | FINAL | |
| fdk_aac | 2.0.3 | 2.0.3 | FINAL | |
| soxr | 0.1.3 | 0.1.3 | FINAL | |

#### Image codecs

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| libtiff | 4.7.1 | 4.7.2 | BUMP | |
| libpng | 1.6.55 | 1.6.58 (2026-04-15) | BUMP | 1.6.56/1.6.57 fixed CVEs in the 1.6.55 era |
| lcms2 | 2.18 | 2.19.1 | BUMP | 2.19 is a featured release (2026-04-24); 2.19.1 is the latest patch |
| libjxl | 0.11.2 | 0.12.0 (2026-07-01) | BUMP | |
| libwebp | 1.6.0 | 1.6.0 | UP-TO-DATE | |

#### Other libraries

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| libsdl (SDL2) | 2.30.12 | 2.30.12 | UP-TO-DATE | SDL2 is in maintenance mode; SDL3 (3.4.x) is a separate project, not a drop-in replacement |
| FreeType2 | 2.14.2 | 2.14.3 (2026-03-22) | BUMP | Security fix release |
| VapourSynth | 73 | R78 (2026-07-24) | BUMP | R74 (2026-04-05) and R75 (2026-04-30) also newer; R78 requires a C++20 compiler; R79 / R80A* are RC/experimental |
| libvmaf | 3.0.0 | 3.2.0 (2026-06-20) | BUMP | 3.2.0 is the first public release of VMAF v1 (3.1.0 was 2026-04-02) |
| srt | 1.5.4 | 1.5.6 (2026-07-20) | BUMP | 1.5.5 was 2026-04-28 |
| libzmq | 4.3.5 | 4.3.5 | UP-TO-DATE | |

#### Hardware acceleration

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| vulkan-headers | 1.4.341.0 | 1.4.357.0 (2026-07-17) | BUMP | Tag `vulkan-sdk-1.4.357.0` |
| glslang | 16.2.0 | 16.5.0 (2026-07-31) | BUMP | |
| fast-float | 6.1.6 | 8.2.10 (2026-06-14) | BUMP | Two major versions ahead; verify libplacebo 7.360.1 compatibility with fast_float 8.x before bumping |
| libplacebo | 7.360.1 | 7.360.1 (2026-03-13) | UP-TO-DATE | |
| nv-codec (nv-codec-headers) | 13.0.19.0 | 13.1.15.0 (2026-07-14) | BUMP | Satisfies FFmpeg 9.0 NVENC requirement (>= 11.1) |
| amf | 1.5.0 | 1.5.2 (2026-05-06) | BUMP | |
| opencl-headers | 2025.07.22 | 2026.05.29 | BUMP | |
| opencl-icd-loader | 2025.07.22 | 2026.05.29 | BUMP | |
| onevpl (libvpl) | 2.17.0 | 2.17.0 (2026-06-25) | UP-TO-DATE | |

#### Target

| Component | Current pin | Latest (release date) | Status | Notes |
|---|---|---|---|---|
| ffmpeg | 8.1 | 9.0 | BUMP | Target of this integration (tag `n9.0`) |

#### Bump summary (33 of 63 components)

- **Build tools (6):** giflib 5.2.2→6.1.3 (URL folder change), nasm 3.01→3.02, m4 1.4.20→1.4.21, cmake 4.2.3→4.4.3, meson 1.8.2→1.12.0, ninja 1.12.1→1.13.2
- **Crypto (3):** gettext 0.22.5→0.26, openssl 3.6.1→3.6.4, gnutls 3.8.12→3.8.13
- **Video codecs (6):** dav1d 1.5.3→1.5.4, svtav1 4.0.1→4.2.0, x264 (re-pin commit), x265 (commit→4.2), libvpx 1.16.0→1.17.0, libaom 3.12.0→3.13.3
- **Audio (2):** serd 0.32.8→0.32.10, lilv 0.26.4→0.28.0
- **Image (4):** libtiff 4.7.1→4.7.2, libpng 1.6.55→1.6.58, lcms2 2.18→2.19.1, libjxl 0.11.2→0.12.0
- **Other (4):** FreeType2 2.14.2→2.14.3, VapourSynth R73→R78, libvmaf 3.0.0→3.2.0, srt 1.5.4→1.5.6
- **HW accel (7):** vulkan-headers 1.4.341.0→1.4.357.0, glslang 16.2.0→16.5.0, fast-float 6.1.6→8.2.10, nv-codec-headers 13.0.19.0→13.1.15.0, amf 1.5.0→1.5.2, opencl-headers 2025.07.22→2026.05.29, opencl-icd-loader 2025.07.22→2026.05.29
- **Target (1):** ffmpeg 8.1→9.0

Already current (30 of 63): pkg-config, yasm (final), zlib, autoconf, automake, libtool, gmp, nettle, rav1e, xvidcore (final), vid_stab (final), zimg, lv2, pcre (final), zix, sord, sratom, opencore-amr (final), lame (final), opus, libogg, libvorbis, libtheora (final), fdk-aac (final), soxr (final), libwebp, SDL2, libzmq, libplacebo, oneVPL.

Notes for the version-refresh batches (Phase B):

1. Every bump requires a fresh `sha256` for the archive (see `require_sha256_for_network` policy) and a check that the `url`/`archive_filename` patterns still match the upstream artifact name.
2. giflib is the only bump that changes the download URL path, not just the version.
3. x264 and x265 move from commit-hash pins to (x265) a versioned release or (x264) a newer verified stable-branch commit; both require new SHA-256 values.
4. fast-float (6.1.6→8.2.10) and VapourSynth (R73→R78, C++20 requirement) are the two bumps with real compatibility risk and should be validated in a dry build before landing.
5. libvpx 1.17.0 is ABI-incompatible with 1.16.0 — fine for a from-source build, but note it for anyone reusing a cached workspace.

---

## 6) Implementation status

## Phase A — Safe FFmpeg 9.0 baseline (implemented)

1. `ffmpeg_version` remains `8.1` by default and supports `8.1` and `9.0` as validated selections.
2. The FFmpeg registry target has declared source/checksum profiles for both versions.
3. `--enable-libglslang` is emitted only for FFmpeg 8.1; glslang remains a libplacebo dependency for both versions.
4. The configuration screen, build component list, state, dashboard, and release manifest use the selected version.

## Phase B — Refresh component versions (implemented)

1. Downloaded the FFmpeg 9.0 archive and 32 verified current component archives into `third_party/sources`; each is Git LFS-tracked and has a matching `components.yaml` SHA-256.
2. Updated the corresponding registry pins and URL templates, including the giflib 6.x and libpng SourceForge paths, the lcms2.19.1 release tag, the x264 stable commit `b35605ace3ddf7c1a5d67a2eb553f034aef41d55`, and the canonical x265 4.2 release.
3. Full cross-platform component builds remain required before claiming runtime compatibility for the fast-float 8.x and VapourSynth R78 updates.

## Phase C — Tests and docs (implemented)

1. Configuration tests derive supported values from `SUPPORTED_FFMPEG_VERSIONS` and verify that each selected version persists through save/load.
2. User-facing docs identify `8.1` as the default and `9.0` as an opt-in selection, including FFmpeg 9.0's removed glslang configure flag.
3. Migration guidance is recorded in the repository changelog: existing configurations retain `8.1`; users opt into 9.0 with `ffmpeg_version: "9.0"`.

## Phase D — Validation (in progress)

1. Unit tests and targeted static checks pass after the FFmpeg 9 compatibility fixes.
2. macOS validation completed on 2026-08-31: the default non-GPL FFmpeg 9.0 build completed all 49 selected components (37 source components and 12 system components) with no failures or skips. The generated `ffmpeg`, `ffprobe`, and `ffplay` binaries report FFmpeg 9.0.
3. Remaining backend validation before stable 2.0:
  - Linux
  - Windows MSYS2 UCRT64

---

## 7) Risk register

1. **Configure failure from removed flags** (highest risk, immediate fix required).
2. **Archive/tag naming mismatches** when bumping versions in YAML.
3. **Platform-specific regressions** from newer Vulkan/OpenCL/glslang combinations.
4. **State/resume mismatch** if component version bumps are made without corresponding state/test adjustments.

---

## 8) Recommended execution order

1. Land FFmpeg 9.0 + flag compatibility fix first.
2. Run tests and basic validation.
3. Land component version refreshes in small batches (hwaccel first).
4. Add optional ONNX Runtime support in a separate follow-up change.

This sequencing minimizes blast radius and makes failures easier to isolate.

---

## 9) Source references used for this analysis

- `FFmpeg/FFmpeg` tag list (`n9.0`, `n8.1`)
- `FFmpeg/FFmpeg` files at `refs/tags/n9.0`:
  - `RELEASE`
  - `RELEASE_NOTES`
  - `Changelog`
  - `configure`
  - `doc/general_contents.texi`
- `FFmpeg/FFmpeg` files at `refs/tags/n8.1` for delta analysis:
  - `configure`
  - `doc/general_contents.texi`
- Component upstream release/tag checks (full survey of all 63 `components.yaml` components, verified 2026-08-31):
  - Build tools: `Kitware/CMake`, `mesonbuild/meson`, `ninja-build/ninja`, `netwide-assembler/nasm` (nasm.us), `madler/zlib`, `yasm/yasm` (yasm.tortall.net), GNU m4/autoconf/automake/libtool/gettext (ftp.gnu.org mirrors), `pkgconfig.freedesktop.org`
  - Crypto: `openssl/openssl` (openssl-library.org), `gmplib.org`, `gnutls/nettle` (lysator.liu.se), `gnutls/gnutls` (gnutls.org)
  - Video codecs: `videolan/dav1d`, `AOMediaCodec/SVT-AV1`, `xiph/rav1e`, `videolan/x264`, `Multicorewareinc/x265` (x265.readthedocs.io), `webmproject/libvpx`, `xvid.com`, `georgmartius/vid.stab`, `aomedia/aom`, `sekrit-twc/zimg`, `sourceforge.net/projects/giflib`
  - Audio: `lv2plug.in`, `drobilla/serd`, `drobilla/zix`, `drobilla/sord`, `lv2/sratom`, `lv2/lilv`, `pcre.org`, `sourceforge.net/projects/opencore-amr`, `sourceforge.net/projects/lame`, `opus-codec.org`, `xiph.org` (ogg/vorbis/theora), `sourceforge.net/projects/soxr`
  - Image: `libtiff/libtiff`, `libpng` (libpng.com / SourceForge), `mm2/Little-CMS`, `libjxl/libjxl`, `webmproject/libwebp`
  - Other: `libsdl-org/SDL`, `freetype.org`, `vapoursynth/vapoursynth`, `Netflix/vmaf`, `Haivision/srt`, `zeromq/libzmq`
  - HW accel: `KhronosGroup/Vulkan-Headers`, `KhronosGroup/glslang`, `fastfloat/fast_float`, `haasn/libplacebo`, `FFmpeg/nv-codec-headers`, `GPUOpen-LibrariesAndSDKs/AMF`, `KhronosGroup/OpenCL-Headers`, `KhronosGroup/OpenCL-ICD-Loader`, `intel/libvpl`
