# Phase 2 Implementation Plan: Concrete Platform & Toolchain Implementations

## 1. Overview & Objective

Phase 2 implements the four concrete platform strategies specified in the target architecture:
1. `MacOSPlatform` (macOS / Darwin with Apple or MacPorts Clang)
2. `LinuxGcc13Platform` (Ubuntu 24.04.4 LTS with GCC 13, C11/C++17 standards)
3. `LinuxGcc15Platform` (Fedora 44 / Ubuntu 26.04.1 with GCC 15/16, C23 standards)
4. `WindowsUcrt64Platform` (Windows 11 with MSYS2 UCRT64 toolchain)

It also creates `PlatformResolver` in `platforms/resolver.py` to automatically instantiate and bind the correct strategy to `FFmpegBuilder`.

---

## 2. Detailed Task Breakdown

### Task 2.1: Implement macOS Strategy (`platforms/darwin.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - OpenMP resolution logic ([builder.py:369-395](builder.py#L369-L395)).
  - Clang compiler selection and validation ([builder.py:563-596](builder.py#L563-L596)).
  - Dynamic library path handling (`@rpath`, `/opt/local/lib`, `/usr/local/lib`).
  - C++ runtime flag (`-lc++`).
  - Threading model (`--enable-pthreads`).
  - System library locations (`/opt/local`, `/usr/local`).

### Task 2.2: Implement Linux GCC 13 Strategy (`platforms/linux_gcc13.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - Default C/C++ standards (`-std=c11`, `-std=c++17`).
  - Multiarch path resolution (`/usr/lib/<triplet>/pkgconfig`).
  - Linux base extra libraries (`-ldl -lpthread -lm -lz`).
  - Static build flags (`-static -fPIC`, `-lgcc_eh` substitution).
  - Native CPU flags (`-march=native -mtune=native`).
  - OpenMP compiler driver linking (`-fopenmp`).

### Task 2.3: Implement Linux GCC 15 Strategy (`platforms/linux_gcc15.py`)
- **Extend `LinuxGcc13Platform`**:
  - Inherit baseline Linux behavior.
  - C23 and C++23 standard handling (`-std=gnu17` / `-std=gnu11` fallbacks when compiling legacy C components).
  - Stricter inline assembly and warning flag management.

### Task 2.4: Implement Windows UCRT64 Strategy (`platforms/windows_ucrt64.py`)
- **Migrate Logic from [builder.py](builder.py)**:
  - Windows 8.3 short path and space escaping ([builder.py:319-348](builder.py#L319-L348)).
  - MSYS POSIX path conversion `/d/...` ([builder.py:349-360](builder.py#L349-L360)).
  - Dual `PKG_CONFIG_PATH` formatting (colon-separated MSYS paths vs semicolon-separated Win32 paths, [builder.py:407-440](builder.py#L407-L440)).
  - Windows-specific libraries (`-lws2_32 -lrpcrt4`).
  - FFmpeg threading (`--enable-w32threads`).
  - MSYS2 UCRT64 include and lib directory discovery ([builder.py:977-1005](builder.py#L977-L1005)).

### Task 2.5: Implement Strategy Resolver (`platforms/resolver.py`)
- **Action**:
  - Implement `resolve_platform_strategy(context: PlatformContext) -> BasePlatformStrategy`.
  - Dispatch based on `platform_info.platform`, `platform_info.build_backend`, and `platform_info.gcc_major_version`.

### Task 2.6: Integrate Strategy into `FFmpegBuilder`
- **Action**:
  - Update `FFmpegBuilder.__init__` in [builder.py](builder.py) to instantiate the resolved strategy.
  - Delegate `_setup_environment()`, `_ws_str()`, `_to_msys_path()`, `_normalize_windows_path_for_flags()`, and `_normalize_pkg_config_path_*()` to the strategy.

---

## 3. Acceptance Criteria

1. All 4 concrete strategies pass comprehensive unit testing under `tests/test_platform_strategy.py`.
2. Windows UCRT64 path conversion and PKG_CONFIG_PATH formatting behave identically to legacy implementations.
3. macOS OpenMP and Clang selection behave identically to legacy implementations.
4. Linux multiarch and static flag assembly match existing tests in [tests/test_builder_split.py](tests/test_builder_split.py).
5. All unit tests pass with zero regressions.
