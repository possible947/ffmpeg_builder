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
mypy .

# Tests
pytest
pytest path/to/test_file.py::test_name
```

Windows 11 + MSYS2 UCRT64 bootstrap (from PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows_msys2_ucrt64.ps1
```

## High-level architecture

Primary flow:

`python -m ffmpeg_builder` → `__main__.py` → `FFmpegBuilderApp` (`app.py`) → orchestration across config/state/platform/report/UI → `FFmpegBuilder` (`builder.py`) executes each component.

Key boundaries:

- **Build graph + component metadata (`components.py`)**: defines component list, build order, dependencies, platform/gating metadata, and FFmpeg flags.
- **Detection layer (`platform_detect.py`)**: computes backend-aware capabilities (Linux/macOS/Windows MSYS2 UCRT64, CUDA/Vulkan/OpenCL/QSV/tool availability) used by component filtering and flag decisions.
- **Execution layer (`builder.py`, `executor.py`, `downloader.py`)**: per-component lifecycle is download/extract/configure/build/install; command output is logged to `workspace/logs/<component>_<step>.log`.
- **State/resume layer (`state.py`)**: persists progress in `workspace/build_state.json`; in-progress states are normalized to `pending` on reload for safe resume after interruption.
- **Interactive UI layer (`ui/`)**: screens + live dashboard + interactive error handler (`retry` / `skip` / `abort`) are driven by status callbacks from the state/builder flow.

## Key conventions

- Keep component behavior declarative in `ComponentRegistry`; avoid scattering component metadata/rules into unrelated builder code.
- Prefer metadata-driven customization (`platform_overrides`, `configure_args_override`, `skip_condition`, `custom_build_fn`) over ad-hoc per-component branching in generic build paths.
- Preserve `ComponentStatus` values exactly (`pending`, `system`, `downloading`, `configuring`, `building`, `installing`, `completed`, `failed`, `skipped`) because state recovery and dashboard rendering depend on them.
- Raise build failures as `BuildError(component, message, log_file)` so UI recovery can show actionable component + log context.
- Maintain offline-first source behavior: archives resolve from `third_party/sources`; network fetching is opt-in via `allow_network_downloads`.
- Treat `windows-msys2-ucrt64` as a distinct backend with explicit path normalization/system-package rules; do not generalize native Windows assumptions onto the UCRT64 flow.
