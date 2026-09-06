# Phase 1 Implementation Plan: Architecture & Platform Strategy Abstraction

## 1. Overview & Objective

Phase 1 establishes the foundational abstractions for decoupling platform- and compiler-specific logic from [builder.py](builder.py). It introduces the `PlatformStrategy` protocol and `PlatformContext` data structure, along with granular compiler detection (distinguishing GCC 13 from GCC 15/16).

---

## 2. Detailed Task Breakdown

### Task 1.1: Create Platform Context & Protocol (`platforms/context.py` & `platforms/base.py`)
- **Action**:
  - Define `PlatformContext` dataclass in `platforms/context.py` holding:
    - `config`: `BuildConfig` instance
    - `workspace`: Absolute `Path` to workspace
    - `packages`: Absolute `Path` to source packages
    - `platform_detector`: `PlatformDetector` instance
    - `platform_info`: `PlatformInfo` instance
    - `num_jobs`: Concurrency count
  - Define abstract base class `BasePlatformStrategy` in `platforms/base.py` declaring the following interface:
    - `setup_environment(context: PlatformContext) -> Dict[str, str]`
    - `normalize_path(path: str | Path, context: PlatformContext) -> str`
    - `get_pkg_config_path(context: PlatformContext, for_posix_shell: bool = False) -> str`
    - `get_cflags(context: PlatformContext) -> str`
    - `get_cxxflags(context: PlatformContext) -> str`
    - `get_ldflags(context: PlatformContext) -> str`
    - `get_ldexeflags(context: PlatformContext) -> str`
    - `get_extralibs(context: PlatformContext) -> str`
    - `get_cpp_runtime_lib() -> str` (`-lc++` on Darwin, `-lstdc++` elsewhere)
    - `get_ffmpeg_threading_flag() -> str` (`--enable-w32threads` on UCRT64, `--enable-pthreads` elsewhere)
    - `get_system_lib_paths(context: PlatformContext) -> List[Path]`

### Task 1.2: Enhance Compiler & Toolchain Probing (`platform_detect.py`)
- **Action**:
  - Extend `PlatformDetector` in [platform_detect.py](platform_detect.py) to accurately identify:
    - GCC major version (e.g., 13 vs 14 vs 15 vs 16).
    - C23 default mode status.
    - Clang version on macOS (Apple Clang vs MacPorts `clang-mp-*`).
  - Add `gcc_major_version: Optional[int]` and `is_c23_default: bool` to `PlatformInfo` dataclass in [platform_detect.py](platform_detect.py#L60).

### Task 1.3: Package Structure & Re-exports
- **Action**:
  - Initialize `platforms/__init__.py` exporting `BasePlatformStrategy`, `PlatformContext`, and strategy resolution types.

### Task 1.4: Unit Tests for Strategy Abstraction
- **Action**:
  - Create [tests/test_platform_strategy.py](tests/test_platform_strategy.py) to verify:
    - Context initialization and validation.
    - Base class abstract contract enforcement.
    - GCC version and C23 detection parsing logic.

---

## 3. Acceptance Criteria

1. `platforms/base.py` and `platforms/context.py` are type-hinted and pass `python scripts/check_mypy_baseline.py`.
2. `PlatformDetector` properly extracts GCC major versions across Linux and Windows MSYS2 UCRT64 environments.
3. All new unit tests pass under `pytest tests/`.
4. Zero breaking changes to existing `FFmpegBuilder` operations.
