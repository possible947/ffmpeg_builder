# AGENTS.md

## Package layout (unusual)

- Flat layout: the package IS the repo root. The root `ffmpeg_builder` file is a launcher script, not a package directory — the real package modules live at the root (`__main__.py`, `app.py`, `components.py`, ...), with `ui/` mapped to `ffmpeg_builder.ui` (see `[tool.setuptools.package-dir]` in `pyproject.toml`).
- Use absolute imports (`from ffmpeg_builder.components import ComponentRegistry`). Run `pip install -e .` after environment changes.
- `components.py` `ComponentRegistry` loads the 64-component registry from `components.yaml` at the repo root. Its header says `python _gen_components_yaml.py` regenerates it, but that script does not exist in the repo — edit `components.yaml` directly. Custom build functions live in `builder.py` and are dispatched via the `CUSTOM_BUILDERS` dict in `component_builders.py` (referenced by `custom_build_fn`).

## Commands

- Install: `pip install -e .`
- Env check: `./scripts/check_python_env.sh`
- Run: `python -m ffmpeg_builder` (or `./ffmpeg_builder`, or the `ffmpeg-builder` entry point). It takes **no CLI arguments** — the `--help`/`--workspace`/`--config` options documented in the README do not exist.
- Tests: `pytest tests/` (135 tests, ~4 s; config/state/components/builder-split/downloader surface). Do NOT run bare `pytest` at the root: it also collects `workspace/packages/` (extracted third-party sources, gitignored) and crashes on their test programs. Single test: `pytest tests/test_state.py::test_name`.
- Lint/typecheck: dev tools are pinned in `requirements-dev.txt` (CI installs exactly these; bump a pin only deliberately, then re-run `black .` and `python scripts/check_mypy_baseline.py --update` and commit the results together). `black --check .` must pass. mypy is checked with `python scripts/check_mypy_baseline.py` — a frozen baseline (`mypy_baseline.txt`, 34 errors / 26 unique after normalisation; missing `tqdm`/`yaml` stubs, untyped defs): new errors fail, fixed errors stay green until the baseline is refreshed. Never run mypy on `.` — it crawls `workspace/`.
- CI: `.github/workflows/ci.yml` runs pytest + `black --check .` + the mypy baseline on push to `master` and on PRs (ubuntu, Python 3.12, pinned tools). No pre-commit. A full FFmpeg build is a long, hardware-dependent manual process (~20–60 min, ~10 GB); verify code changes with unit tests.
- On this Windows machine, tests run under the MSYS2 venv: `C:\msys64\usr\bin\bash.exe -lc "cd /e/Projects/ffmpeg_builder && source ./.venv-msys2-ucrt64/bin/activate && python -m pytest tests/ -q"`.

## Conventions

- Keep component behavior declarative in the registry; prefer `platform_overrides` / `configure_args_override` / `skip_condition` / `custom_build_fn` over ad-hoc per-component branching in `builder.py`.
- Preserve `ComponentStatus` values exactly (`pending`, `system`, `downloading`, `configuring`, `building`, `installing`, `completed`, `failed`, `skipped` — `state.py:13`) — state recovery and dashboard rendering depend on them.
- Pass `detail="..."` on every status transition (via `mark_component_status`) so the live dashboard shows the running command/progress; `detail` is transient and not persisted.
- Raise build failures as `BuildError(component, message, log_file)` (`build_types.py`) so the UI error handler can show component + log context.
- Builds are offline-first: archives resolve from `third_party/sources`; network fetching is opt-in via `allow_network_downloads`.
- `windows-msys2-ucrt64` and `linux-wsl2` are distinct backends with separate component-eligibility rules; do not generalize native Linux/Windows assumptions onto them.

## Gotchas

- `third_party/sources` archives are Git LFS-tracked (`.gitattributes`); run `git lfs pull` after clone. Check that an archive is a real tarball, not a 3-line LFS pointer (`version https://git-lfs.github.com/spec/v1`), before debugging extraction failures.
- On component failure, inspect `workspace/logs/<component>_<step>.log` and the resume state in `workspace/build_state.json`.
- Root `build_config.yaml` is gitignored and environment-specific; do not rely on repository-tracked defaults from that file.

### Windows MSYS2 UCRT64: incomplete environment

**Symptom**: a cmake-based component (e.g. `svtav1`) fails immediately with return code `3221225781` (0xC0000135, `STATUS_DLL_NOT_FOUND`) and empty stdout/stderr logs — `cmake` was not found or could not load its DLLs.

**Root cause**: an old setup script ran `pacman -Sy` (DB sync only) before `pacman -S`, which does not guarantee on-disk packages match the synced DB; dependency resolution can silently skip packages after a partial update.

**Fix**: run `setup_windows_msys2_ucrt64.ps1` after every MSYS2 environment rebuild. It runs `pacman -Syu --noconfirm` first, then `pacman -S --needed --noconfirm` for project packages, and verifies all critical tools (`gcc`, `cmake`, `ninja`, `meson`, `nasm`, `python`, `pkg-config`) are present, throwing if any is missing.

**Manual recovery**: in an MSYS2 UCRT64 shell:
```bash
pacman -Syu          # full system upgrade (may require restarting the shell once)
pacman -S --needed mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-ninja \
         mingw-w64-ucrt-x86_64-meson mingw-w64-ucrt-x86_64-nasm
which cmake ninja meson nasm  # verify
```

### Windows MSYS2 UCRT64: sh.exe wrapping rules

Only commands beginning with `./` are wrapped through `sh.exe` in `executor.py:71-84`; `./x.py` scripts are run via `sys.executable` directly (sh.exe cannot use shebangs on Windows). cmake, ninja, make, pkg-config, and other `mingw-w64-ucrt-x86_64-*` tools are native Windows PE binaries and must be called directly via `subprocess.run()` — wrapping them through `sh.exe` causes `cannot execute binary file` (exit 126).

## Docs

- `docs/DeveloperReadme.md` is the authoritative architecture doc (module responsibilities, data flow, extension points); not linked from the root README.
- `docs/CHANGELOG.md` tracks applied remediation for high/medium review findings and is the authoritative status source for fix history.
