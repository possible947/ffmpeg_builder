# Phase 3 Implementation Plan: Declarative & Versioned Source Patch Engine

## 1. Overview & Objective

Phase 3 extracts hardcoded source-patching logic from [builder.py](builder.py) into a dedicated, declarative patch engine under `patches/`.

Every patch is registered with:
- Target component name and component version constraints.
- Platform/compiler conditions (e.g., only GCC 15/16 or only Darwin).
- Precise search anchors, replacements, and verification assertions (`assert_patch_present`, `assert_patch_absent`).

---

## 2. Detailed Task Breakdown

### Task 3.1: Create Patch Framework Core (`patches/base.py` & `patches/registry.py`)
- **Action**:
  - Implement `SourcePatch` dataclass in `patches/base.py` with:
    - `name: str`
    - `component_name: str`
    - `target_rel_path: str`
    - `apply_fn: Callable[[Path, Component, BasePlatformStrategy], None]`
    - `condition: Optional[Callable[[Component, BasePlatformStrategy], bool]]`
  - Implement assertion helpers:
    - `assert_patch_present(component_name: str, path: Path, marker: str, context_desc: str)`
    - `assert_patch_absent(component_name: str, path: Path, marker: str, context_desc: str)`
  - Implement `PatchRegistry` in `patches/registry.py` to collect and apply all matching patches for a component before building.

### Task 3.2: Implement C23 & Compiler Patches (`patches/c23_fixes.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - `xvidcore` bool typedef patch ([builder.py:1128-1155](builder.py#L1128-L1155)):
    - Target: `src/encoder.h`
    - Guard `typedef int bool;` for C23 (`__STDC_VERSION__ < 202311L`).
  - `openssl` C11 inline assembly patch ([builder.py:1508-1528](builder.py#L1508-L1528)):
    - Target: `configdata.pm`
    - Replace `-std=c11` with `-std=gnu11` and re-run `perl configdata.pm`.

### Task 3.3: Implement C++ Standard Library Header Patches (`patches/cxx_headers.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - `x265` `json11.cpp` `<cstdint>` inclusion patch ([builder.py:1620-1642](builder.py#L1620-L1642)):
    - Target: `source/dynamicHDR10/json11/json11.cpp`
    - Insert `#include <cstdint>` after `#include <limits>`.

### Task 3.4: Implement macOS / Darwin Patches (`patches/darwin_patches.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - `libjxl` `deps.sh` `realpath` guard ([builder.py:2053-2081](builder.py#L2053-L2081)):
    - Target: `deps.sh`
    - Insert fallback for systems where `realpath` is missing.
  - `libvorbis` CPU subtype patch ([builder.py:1984-1998](builder.py#L1984-L1998)):
    - Target: `configure.ac`
    - Remove `-force_cpusubtype_ALL`.

### Task 3.5: Implement FFmpeg Versioned Patches (`patches/ffmpeg_patches.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - `ffmpeg` 9.0 Vulkan renderer context include patch ([builder.py:298-317](builder.py#L298-L317)):
    - Target: `fftools/ffplay_renderer.c`
    - Insert `#include "libavutil/hwcontext_vulkan.h"`.

### Task 3.6: Integrate Patch Engine into Component Lifecycle
- **Action**:
  - Call `PatchRegistry.apply_patches(component, source_dir, strategy)` immediately after downloading/extracting and before executing configure/build steps.

---

## 3. Acceptance Criteria

1. Patch execution is fully automated during component build lifecycle.
2. If an upstream component updates and removes a patch anchor, `assert_patch_present` / `assert_patch_absent` fails fast with a clear error message.
3. Unit tests in [tests/test_patches.py](tests/test_patches.py) verify every patch on simulated file fixtures.
4. Clean removal of ~180 lines of inline patching from [builder.py](builder.py).
