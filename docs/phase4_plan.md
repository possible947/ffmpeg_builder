# Phase 4 Implementation Plan: Domain-Specific Component Builders Modularization

## 1. Overview & Objective

Phase 4 extracts the 15 custom component builders and standard build runners out of [builder.py](builder.py) into organized domain modules under `builders/`.

---

## 2. Detailed Task Breakdown

### Task 4.1: Base Build Systems (`builders/base.py`)
- **Action**:
  - Implement `ComponentBuildContext` linking `FFmpegBuilder`, `BasePlatformStrategy`, `CommandExecutor`, and `StateManager`.
  - Extract and modularize standard build runners from [builder.py](builder.py):
    - `build_autotools(context, component, source_dir)`
    - `build_cmake(context, component, source_dir)`
    - `build_meson(context, component, source_dir)`
    - `build_make_only(context, component, source_dir)`
    - `build_cargo(context, component, source_dir)`
    - `install_headers_only(context, component, source_dir)`

### Task 4.2: Codec Builders (`builders/codecs/`)
- **Action**:
  - `builders/codecs/x264.py`: Static PIC configuration and `install-lib-static` ([builder.py:1548-1606](builder.py#L1548-L1606)).
  - `builders/codecs/x265.py`: Multi-bitdepth build (8-bit, 10-bit, 12-bit) with static archive merging ([builder.py:1608-1800](builder.py#L1608-L1800)).
  - `builders/codecs/vpx.py`: Libvpx custom configure and make ([builder.py:1802-1866](builder.py#L1802-L1866)).
  - `builders/codecs/zimg.py`: Libtoolize/glibtoolize discovery and autogen build ([builder.py:1868-1975](builder.py#L1868-L1975)).
  - `builders/codecs/vorbis.py`: Libvorbis autogen & OGG path wiring ([builder.py:1977-2045](builder.py#L1977-L2045)).
  - `builders/codecs/jxl.py`: Libjxl CMake build with `deps.sh` execution ([builder.py:2047-2148](builder.py#L2047-L2148)).

### Task 4.3: Crypto Builders (`builders/crypto/`)
- **Action**:
  - `builders/crypto/openssl.py`: OpenSSL `./Configure` with `zlib`, `no-shared`, and prefix setup ([builder.py:1479-1546](builder.py#L1479-L1546)).

### Task 4.4: Graphics & Filtering Builders (`builders/graphics/`)
- **Action**:
  - `builders/graphics/glslang.py`: Glslang source update and CMake build ([builder.py:2610-2668](builder.py#L2610-L2668)).
  - `builders/graphics/placebo.py`: Libplacebo Meson setup with Vulkan discovery, `fast_float` injection, glslang search fix, and `.pc` normalization ([builder.py:2372-2608](builder.py#L2372-L2608)).
  - `builders/graphics/vmaf.py`: Libvmaf Meson build with CUDA nvcc `-Xcompiler` flags ([builder.py:2150-2232](builder.py#L2150-L2232)).

### Task 4.5: Network Builders (`builders/network/`)
- **Action**:
  - `builders/network/srt.py`: SRT CMake build and `.pc` `-lgcc_s` replacement ([builder.py:2234-2295](builder.py#L2234-L2295)).
  - `builders/network/zmq.py`: Libzmq autotools build, `proxy.cpp` struct initializer fix, and Windows `-DZMQ_STATIC` `.pc` patch ([builder.py:2297-2370](builder.py#L2297-L2370)).

### Task 4.6: Tool Builders (`builders/tools/`)
- **Action**:
  - `builders/tools/ninja.py`: Ninja bootstrap build ([builder.py:2670-2693](builder.py#L2670-L2693)).
  - `builders/tools/meson.py`: Meson bootstrap install from source ([builder.py:2695-2745](builder.py#L2695-L2745)).

### Task 4.7: FFmpeg Final Builder (`builders/ffmpeg/`)
- **Action**:
  - `builders/ffmpeg/ffmpeg.py`: FFmpeg configure flag composition from `ComponentRegistry`, Darwin/Linux C++ runtime injection (`-lc++` / `-lstdc++`), Vulkan runtime flags, w32threads/pthreads selection, and compilation ([builder.py:2747-2938](builder.py#L2747-L2938)).

### Task 4.8: Connect Custom Builder Dispatch (`component_builders.py`)
- **Action**:
  - Update `CUSTOM_BUILDERS` in [component_builders.py](component_builders.py) to dispatch to the modular functions under `builders/`.
  - Maintain signature compatibility: `CustomBuilder = Callable[["FFmpegBuilder", "Component", Path], None]`.

---

## 3. Acceptance Criteria

1. Every custom build routine is isolated in its respective domain module.
2. `CUSTOM_BUILDERS` in [component_builders.py](component_builders.py) successfully dispatches to modular builders.
3. [tests/test_builder_split.py](tests/test_builder_split.py) verifies that all registered builders exist and can be looked up.
4. All unit tests pass across all platforms.
