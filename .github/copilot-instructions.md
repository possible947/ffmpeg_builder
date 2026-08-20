# Copilot instructions for `ffmpeg_builder`

## Build, test, and lint commands

```bash
# Install project + deps
pip install -e .

# Optional environment sanity check
./scripts/check_python_env.sh

# Run the app
python -m ffmpeg_builder
# or
./ffmpeg_builder

# Lint / type-check
black .
mypy <files>              # Run mypy on specific files, not the root (to avoid crawling workspace/)

# Tests
pytest tests/             # 66 unit tests, ~2s. ALWAYS use tests/ — bare pytest crashes on workspace/packages/
pytest tests/test_state.py::test_name  # Run a single test
```

Windows 11 + MSYS2 UCRT64 bootstrap (from PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows_msys2_ucrt64.ps1
```

Then in MSYS2 UCRT64 shell:

```bash
source ./.venv-msys2-ucrt64/bin/activate
python -m ffmpeg_builder
```

## Package layout (unusual)

The package uses a flat layout: the root IS the package directory. Modules live at the root (`__main__.py`, `app.py`, `components.py`, ...), with `ui/` mapped to `ffmpeg_builder.ui`. Use absolute imports: `from ffmpeg_builder.components import ComponentRegistry`. Always run `pip install -e .` after environment changes.

Components are loaded from `components.yaml` (edit this directly — no regeneration script exists). Custom build functions live in `component_builders.py`, dispatched via the `CUSTOM_BUILDERS` dict in that file.

## High-level architecture

Primary flow:

`python -m ffmpeg_builder` → `__main__.py` → `FFmpegBuilderApp` (`app.py`) → orchestration across config/state/platform/report/UI → `FFmpegBuilder` (`builder.py`) executes each component.

Key boundaries:

- **Build graph + component metadata (`components.py`)**: loads 64-component registry from `components.yaml`; defines build order, dependencies, platform/gating metadata, and FFmpeg flags.
- **Detection layer (`platform_detect.py`)**: computes backend-aware capabilities (Linux/macOS/Windows MSYS2 UCRT64, CUDA/Vulkan/OpenCL/QSV/tool availability) used by component filtering and flag decisions.
- **Execution layer (`builder.py`, `executor.py`, `downloader.py`)**: per-component lifecycle is download/extract/configure/build/install; command output is logged to `workspace/logs/<component>_<step>.log`.
- **State/resume layer (`state.py`)**: persists progress in `workspace/build_state.json`; in-progress states are normalized to `pending` on reload for safe resume after interruption.
- **Interactive UI layer (`ui/`)**: screens + live dashboard + interactive error handler (`retry` / `skip` / `abort`) are driven by status callbacks from the state/builder flow.

See `docs/DeveloperReadme.md` for the authoritative architecture doc (module responsibilities, data flow, extension points).

## Key conventions

- Keep component behavior declarative in the registry; prefer `platform_overrides` / `configure_args_override` / `skip_condition` / `custom_build_fn` over ad-hoc per-component branching in `builder.py`.
- Preserve `ComponentStatus` values exactly (`pending`, `system`, `downloading`, `configuring`, `building`, `installing`, `completed`, `failed`, `skipped`) — state recovery and dashboard rendering depend on them.
- Pass `detail="..."` on every status transition (via `mark_component_status`) so the live dashboard shows the running command/progress; `detail` is transient and not persisted.
- Raise build failures as `BuildError(component, message, log_file)` (`build_types.py`) so the UI error handler can show component + log context.
- Builds are offline-first: archives resolve from `third_party/sources`; network fetching is opt-in via `allow_network_downloads`.
- Treat `windows-msys2-ucrt64` and `linux-wsl2` as distinct backends with separate component-eligibility rules; do not generalize native Linux/Windows assumptions onto them.

## Important gotchas

- **Test runner**: Do NOT run bare `pytest` at the root — it collects `workspace/packages/` (extracted third-party sources, gitignored) and crashes on their test programs. Always use `pytest tests/`.
- **Git LFS**: `third_party/sources` archives are Git LFS-tracked (`.gitattributes`). Run `git lfs pull` after clone. Check that an archive is a real tarball, not a 3-line LFS pointer (`version https://git-lfs.github.com/spec/v1`), before debugging extraction failures.
- **build_config.yaml**: Root `build_config.yaml` is gitignored and environment-specific; do not rely on repository-tracked defaults.
- **mypy baseline**: mypy enforces `disallow_untyped_defs = true` and has ~33 pre-existing errors (missing stubs for `tqdm`/`yaml`, untyped defs). Run mypy on specific files, not the root.
- **black flags**: `black --check` currently flags 3 files (`tests/test_builder_split.py`, `component_builders.py`, `release_bundle.py`).

## Windows MSYS2 UCRT64 specifics

### Incomplete environment error

**Symptom**: cmake-based component (e.g., `svtav1`) fails immediately with return code `3221225781` (0xC0000135, `STATUS_DLL_NOT_FOUND`) and empty stdout/stderr logs.

**Root cause**: MSYS2 environment is missing critical tools; old setup scripts ran `pacman -Sy` (DB sync only) without full upgrade.

**Fix**: run `scripts/setup_windows_msys2_ucrt64.ps1` after every MSYS2 rebuild. It runs `pacman -Syu`, then verifies all critical tools (`gcc`, `cmake`, `ninja`, `meson`, `nasm`, `python`, `pkg-config`) are present.

### sh.exe wrapping rules

Only commands beginning with `./` are wrapped through `sh.exe` in `executor.py:71-84`. cmake, ninja, make, pkg-config, and other `mingw-w64-ucrt-x86_64-*` tools are native Windows PE binaries and must be called directly via `subprocess.run()` — wrapping them through `sh.exe` causes `cannot execute binary file` (exit 126).

## Tools & MCP Servers

**Git MCP** is configured for this repository, enabling standardized repository operations across AI sessions. Use it for:
- Querying commit history and current branch state
- Checking for uncommitted changes before making edits
- Staging and committing code changes with proper authorship

For most operations, `git` CLI commands via bash are equally efficient; MCP provides consistency across different agent contexts.
