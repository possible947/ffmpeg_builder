# Copilot instructions for `ffmpeg_builder`

## Build, test, and lint commands

```bash
# Install the project and the pinned development tools used by CI
python -m pip install -e .
python -m pip install -r requirements-dev.txt

# Optional environment sanity check
./scripts/check_python_env.sh

# Launch the interactive TUI (it takes no CLI arguments)
python -m ffmpeg_builder

# Headless toolchain setup and build
python scripts/prepare_toolchains.py --dry-run
python scripts/prepare_toolchains.py
python scripts/run_build.py --dry-run
python scripts/run_build.py --resume

# Test suite and one test
python -m pytest tests/ -q
python -m pytest tests/test_state.py::test_name -q

# Formatting and the repository's frozen mypy baseline
python -m black --check .
python scripts/check_mypy_baseline.py
```

CI is defined in `.github/workflows/ci.yml`. Use `tests/` as the pytest target:
the build workspaces can contain extracted third-party projects whose tests
must not be collected. Development tool versions are pinned in
`requirements-dev.txt`; use `python scripts/check_mypy_baseline.py` rather
than running mypy over the repository or workspace.

## Package layout

The repository root is mapped to the `ffmpeg_builder` Python package by
`pyproject.toml`; application modules live at the root, with implementation
packages such as `builders/`, `platforms/`, and `ui/`. Use absolute imports
such as `from ffmpeg_builder.components import ComponentRegistry`.

## High-level architecture

- `__main__.py` starts the interactive application. `app.py` coordinates
  configuration, platform detection, component selection, workspace/state
  setup, the build loop, and error handling. `scripts/run_build.py` provides
  the headless build path; both routes share the build and preparation logic.
- `components.py` loads the dependency-ordered component graph and build
  metadata from `components.yaml`. `platform_detect.py` gathers host and
  hardware capabilities, while `platforms/` resolves backend-specific build
  contexts.
- `builder.py` runs each selected component through source acquisition,
  extraction, configure, build, and install. Shared command/status handling
  lives in `build_steps.py` and `executor.py`; `downloader.py` handles local
  archives and optional network downloads. Component-specific recipes are
  registered in `component_builders.py` and implemented under `builders/`.
- `toolchain_registry.py` validates declarative profiles in
  `toolchains.yaml`; `toolchain_store.py` manages installed toolchains and
  `toolchain_builders.py` builds profiles declared as source builds.
  `scripts/prepare_toolchains.py` and the TUI use the same registry/store.
- `state.py` persists component progress. Status listeners feed the live
  dashboard in `ui/dashboard.py`; `BuildError` is handled by the app's
  retry/skip/abort flow.
- FFmpeg versions use separate workspaces (for example `workspace_81` and
  `workspace_90`). `build_identity.py` and `target_guard.py` protect target
  state and artifacts from reuse when build-material inputs or relevant host
  identity differ. Read `docs/DeveloperReadme.md` for module responsibilities
  and `docs/windows_build_strategy.md` for the WSL-Mingw design.

## Key conventions

- Keep component and toolchain choices declarative in `components.yaml` and
  `toolchains.yaml`. Prefer registry metadata and a registered custom builder
  over adding component-specific branches to `builder.py`.
- Preserve the `ComponentStatus` values used by persistence and the dashboard.
  Pass a meaningful `detail` to each `mark_component_status` transition so
  the live dashboard can show the current command or progress.
- Raise component failures as `BuildError` with component and log context;
  use the existing skip/error flow rather than hiding failures.
- Builds are offline-first: source archives come from `third_party/sources`;
  network access is opt-in in configuration. These archives are Git LFS files,
  so verify an archive is not an LFS pointer before investigating extraction.
- The root `build_config.yaml` is environment-specific and gitignored. Do not
  treat a local copy as a tracked default or commit it.
- Keep native Linux/macOS and `linux-cross-mingw` behavior distinct. WSL-Mingw
  is the Windows route; G1–G6 passed for the configured physical target.
  G6 packages the accepted LLVM-MinGW runtime DLLs and leaves Windows, UCRT,
  GPU-driver, Vulkan/OpenCL and optional VapourSynth runtimes external. The
  GPL/nonfree bundle is internal and nonredistributable. Legacy MSYS2 code is not a
  supported backend; do not extend it. For cross builds, keep Linux host tools
  separate from Windows-target inputs and do not let Linux SDK/library paths
  satisfy target dependency lookups.
