# FFmpeg 9.0 Integration — Developer Readme

## 1) Goal

Add **FFmpeg 9.0** build support to `ffmpeg_builder` while preserving the current architecture and behavior used for FFmpeg 8.1, and refreshing component versions to current stable releases where practical.

---

## 2) Current repository baseline (what exists today)

The current project is centered on FFmpeg 8.1:

- Default config version: `8.1`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/config.py`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/build_config.yaml`
  - `/home/runner/work/ffmpeg_builder/ffmpeg_builder/profiles/default.yaml`
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

Compared with current repository pins, newer tags exist for key hwaccel components:

- `glslang`: current `16.2.0` → latest observed `16.5.0`
- `Vulkan-Headers`: current `1.4.341.0` → newer observed `1.4.357.0`
- `OpenCL-Headers`: current `2025.07.22` → newer observed `2026.05.29`
- `OpenCL-ICD-Loader`: current `2025.07.22` → newer observed `2026.05.29`
- `nv-codec-headers`: current `13.0.19.0` → newer observed `13.1.15.0`

`libplacebo` (`7.360.1`) and `oneVPL` (`2.17.0`) are already aligned with latest observed releases.

### 5.3 ONNX Runtime decision

FFmpeg 9.0 introduces `--enable-libonnxruntime` as optional. For the first 9.0 integration iteration, it is safer to:

- keep ONNX Runtime **disabled by default**,
- document it as future optional component,
- add it only after cross-platform packaging/build rules are defined.

---

## 6) Preliminary implementation plan

## Phase A — Safe FFmpeg 9.0 baseline (minimum viable integration)

1. Update FFmpeg target version in `components.yaml` to `9.0`.
2. Update defaults in:
   - `config.py` (`BuildConfig.ffmpeg_version`)
   - `build_config.yaml`
   - `profiles/default.yaml`
3. Remove or version-gate obsolete FFmpeg flags (starting with `--enable-libglslang`).
4. Keep libplacebo/glslang build chain intact for Vulkan, but decouple it from FFmpeg configure flags.

## Phase B — Refresh component versions (requested “current” set)

1. Update selected component pins in `components.yaml` (starting with hwaccel stack listed above).
2. Validate source archive naming and URL patterns still match upstream release artifacts.
3. Ensure no platform-specific breakage from updated versions (Linux/macOS/Windows UCRT64 rules remain intact).

## Phase C — Tests and docs

1. Update tests expecting `8.1` literals.
2. Update user-facing docs (`README.md`, `docs/README.md`, `docs/DeveloperReadme.md`) where version is hard-coded.
3. Add migration note to changelog/release notes in this repository.

## Phase D — Validation

1. Run unit tests (`pytest tests/`).
2. Run targeted static checks for modified Python files.
3. Perform at least one smoke build path per backend policy (or dry-run checkpoints where full build is impractical):
   - Linux
   - macOS (MacPorts + LunarG Vulkan SDK assumptions preserved)
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
- Component upstream release/tag checks:
  - `KhronosGroup/glslang`
  - `KhronosGroup/Vulkan-Headers`
  - `KhronosGroup/OpenCL-Headers`
  - `KhronosGroup/OpenCL-ICD-Loader`
  - `FFmpeg/nv-codec-headers`
  - `intel/libvpl`
  - `haasn/libplacebo`

