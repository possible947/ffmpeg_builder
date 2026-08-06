# FFmpeg Builder — Developer Guide

This document describes the internal architecture, module responsibilities, data flow, and extension points of the FFmpeg Builder system.

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

### `__main__.py`

Entry point. Creates the workspace directory and instantiates `FFmpegBuilderApp`.

### `app.py` — `FFmpegBuilderApp`

Central orchestrator. Responsibilities:

- Initialize all managers (config, state, platform detection, component registry)
- Run the main event loop: show system report, handle user actions
- Coordinate the build process: iterate components, handle errors, update state
- Manage cleanup

Key methods:

| Method | Description |
|--------|-------------|
| `run()` | Main loop — shows screens, dispatches actions |
| `_run_build(config, resume)` | Build loop with retry/skip/abort error handling and Live dashboard lifecycle |
| `_get_buildable_components(config)` | Resolve the component set shared by build and info screens |
| `_cleanup()` | Remove workspace, packages, and state file |

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
    disable_lv2: bool = False
    num_jobs: str = "auto"
    async_downloads: bool = True
    download_workers: int = 4
    macos: MacOSConfig          # clang version, openmp
    linux: LinuxConfig          # c_standard, cxx_standard
```

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
| `PlatformInfo` | platform, build_backend, is_macos, is_linux, is_windows, is_arm64, is_wsl2, is_msys2, is_ucrt64, msystem, macports_clang, cuda_available, cuda_path, vaapi_available, qsv_available, amf_available, vulkan_available, opencl_available |
| `ToolInfo` | name, path, version, available |

**Detection methods:**

| Method | What it detects |
|--------|-----------------|
| `_detect_system_info()` | OS (reads `/etc/os-release` on Linux), CPU (`/proc/cpuinfo` or `sysctl`), RAM |
| `_detect_platform_info()` | Platform flags, HW acceleration |
| `_detect_cuda()` | nvcc in PATH, then `/usr/local/cuda*/bin/nvcc` |
| `_check_wsl2()` | WSL2 environment via `/proc/version` (Microsoft/WSL) |
| `_check_qsv()` | Linux: QSV via vainfo/PCI Intel GPU with VAAPI. Windows UCRT64: Intel GPU + pkg-config oneVPL (`vpl`/`libvpl`) |
| `_check_vulkan()` | pkg-config, headers, vulkaninfo |
| `_check_opencl()` | Headers + ICD loader + vendor ICD files; WSL-aware |
| `_check_vaapi()` | pkg-config libva |
| `_check_amf()` | Header paths |
| `_detect_tools()` | 16 tools via `shutil.which()` + version extraction |

**WSL awareness:** `_check_wsl2()` detects WSL2 by checking `/proc/version` for "Microsoft" or "WSL". `_check_opencl()` detects WSL by checking `/usr/lib/wsl/lib/` and verifies that `libnvidia-opencl*` exists there. `_check_qsv()` disables QSV in WSL2 environments. WSL2 does not expose OpenCL or QSV through its paravirtualized driver.

### `system_report.py` — Report Generation

**`SystemReport`** — aggregates all detected information:

- `get_hardware_acceleration_status()` — returns dict of HW accel availability
- `get_compiler_info()` — detects GCC/Clang/Macports Clang
- `get_missing_required_tools()` — tools required for build
- `get_optional_tools_status()` — optional tools (nasm, cmake, meson, etc.)

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
    windows_ucrt64_supported: bool
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
| `get_ffmpeg_configure_flags(built, gpl, platform)` | Collect `--enable-*` flags from built components |

**HW acceleration filtering** in `get_buildable()`:

| Component | Condition |
|-----------|-----------|
| `nv-codec` | `platform_info.cuda_available` |
| `vulkan-headers`, `glslang` | `platform_info.vulkan_available` |
| `amf` | `platform_info.amf_available` |
| `opencl-headers`, `opencl-icd-loader` | `platform_info.opencl_available` |
| `onevpl` | `platform_info.qsv_available` |

Windows/UCRT64 policy in current implementation:

- only `nv-codec`, `vulkan-headers`, `glslang`, `opencl-headers`, `opencl-icd-loader` are eligible in `HW_ACCEL`,
- these are enabled only in `MSYSTEM=UCRT64`,
- Linux-only acceleration paths (`VAAPI`, `onevpl`) are excluded on Windows,
- `nv-codec`, `opencl-headers`, `opencl-icd-loader` remain `linux_only` and are allowed on Windows only through explicit `windows_ucrt64_supported` override, keeping WSL2 and MSYS2-UCRT64 paths separated.

### `builder.py` — Build Engine

**`FFmpegBuilder`** — orchestrates the build of each component.

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
3. If HEADERS_ONLY → install headers, return
4. If custom_build_fn → call it, return
5. Dispatch by build_system:
   - AUTOTOOLS → _build_autotools()
   - CMAKE     → _build_cmake()
   - MESON     → _build_meson()
   - MAKE_ONLY → _build_make_only()
   - CARGO     → _build_cargo()
```

**Custom build functions** for components with non-standard build processes:

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
| CUDA available | `--enable-cuda-nvcc`, `--enable-cuvid`, `--enable-nvenc`, `--cuda-sdk=<path>` |
| Per built component | `--enable-libdav1d`, `--enable-libx264`, etc. |
| macOS | `--enable-videotoolbox`, `--enable-opencl` |

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

## Known Limitations

- **WSL2 OpenCL**: Not available through the paravirtualized NVIDIA driver. The builder correctly detects this and excludes OpenCL components.
- **macOS CI**: No automated testing on macOS; platform-specific code paths (macports clang, glibtool, VideoToolbox) require manual verification.
- **Component versions**: Versions are defined in `components.yaml` (externalized from Python). A future improvement could fetch latest versions from an API or config file.
- **No dependency graph**: Components are built in a fixed order defined in `components.yaml`. There is no automatic topological sort based on `depends_on`.
- **Deferred refactoring — Fix #16: split `builder.py` (~100 KB, ~2950 lines) into smaller modules**. The full 16-item code review completed with 15 of 16 fixes applied. Fix #16 (split into `build_steps.py`, `component_builders.py`, `release_bundle.py`) is deferred because it requires careful manual refactoring: all ~20 custom build methods share state via `self.executor`, `self.state_manager`, `self._ws_str()`, `get_build_env()`, `_execute_post_install()`, and many internal helpers. An automated split risks breaking circular imports and method resolution order. Recommended approach after Fix #6 (already applied): manual extraction with explicit dependency injection, one module at a time. See `docs/Fix-Plan.md` for the complete fix plan and status tracking, and `docs/FFmpeg Builder — Full Code Review Report.txt` for the original review findings.

## Code Review & Refactoring Status (2026-08)

The project underwent a comprehensive 16-item code review in August 2026. Results:

| Metric | Value |
|--------|-------|
| Total items reviewed | 16 |
| Applied fixes | 15 (✅ DONE) |
| Deferred items | 1 (🔜 DEFERRED — Fix #16: split builder.py) |

### What changed

- **Components externalized**: `components.yaml` replaces ~850 lines of hardcoded Python dataclasses with a 774-line, 63-component YAML registry.
- **Build orchestration deduplicated**: `_run_step()`, `_run_make()`, `_run_install()` helpers replaced ~100+ lines of repeated mark/execute/check/raise logic across all custom build functions.
- **Thread safety fixed**: `StateManager` now uses `threading.RLock` on all mutations; `AsyncDownloadManager.get()` uses per-file `threading.Event` to eliminate race between futures check and result retrieval.
- **Security hardened**: Shell injection in `_execute_post_install()` wrapped with `shlex.quote()`; HTTP fallback downloads emit `logging.warning()`.
- **Test coverage added**: 56 unit tests across 3 modules (config round-trip, state serialization/thread-safety, component filtering). All passing.
- **Style normalized**: `black` + `isort` applied to all source files (21 files reformatted).

### Remaining work

**Fix #16 — Split `builder.py`** (deferred):

Current state: single file at ~99 KB, ~2950 lines. Target split:

| Target module | Responsibility |
|---------------|----------------|
| `build_steps.py` | Generic build-step orchestration (`_run_step`, `_run_make`, `_run_install`) |
| `component_builders.py` | Per-component custom build functions (~20 methods: openssl, x264, x265, libvpx, zimg, libvorbis, libjxl, libvmaf, srt, libzmq, libplacebo, glslang, ninja, ffmpeg) |
| `release_bundle.py` | `make_release_bundle()` and runtime-dependency collection logic |

Risk assessment: manual refactoring required. Automated tooling cannot safely handle the shared state dependencies without introducing circular imports. Estimated effort: 4–6 hours. Priority: Low (code works correctly; this is purely organizational).

See also:

- `docs/Fix-Plan.md` — full execution plan with per-fix status
- `docs/FFmpeg Builder — Full Code Review Report.txt` — original review findings
- `changelog.md` (repository root) — post-refactor debugging log
