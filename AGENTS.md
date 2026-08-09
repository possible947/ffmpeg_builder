# AGENTS.md

## Repo history: past merge (2026-08-09)

- `master` previously diverged from `origin/master`; the merge was resolved keeping the YAML-driven `ComponentRegistry` (from `components.yaml`) and the `enable_libplacebo_vulkan` config key, while taking origin/master's macOS/libplacebo fixes (glslang meson patch, `-Dvulkan`/`-Dglslang` meson args, `_find_macports_clang` highest-version resolution, `clang-mp-N` fallback, `SystemReport.configured_clang`, giflib pkg-config aliases). The `fast-float` v6.1.6 component (origin-only) was added to `components.yaml`; its archive was re-fetched because origin had committed an LFS pointer. The merged history was then rewritten with `git lfs migrate` so all `third_party/sources/` archives are Git LFS objects (the raw 170MB AMF blob exceeds GitHub's 100MB file limit) and force-pushed.

## Package layout (unusual)

- Flat layout: the package IS the repo root. The root `ffmpeg_builder` file is a launcher script, not a package directory — the real package modules live at the root (`__main__.py`, `app.py`, `components.py`, ...), with `ui/` mapped to `ffmpeg_builder.ui` (see `[tool.setuptools.package-dir]` in `pyproject.toml`).
- Use absolute imports (`from ffmpeg_builder.components import ComponentRegistry`). A `.venv` exists; run `pip install -e .` after environment changes.
- `components.py` `ComponentRegistry` loads the 64-component registry from `components.yaml` at the repo root. Its header says `python _gen_components_yaml.py` regenerates it, but that script does not exist in the repo — edit `components.yaml` directly. Custom build functions live in `builder.py` and are referenced by `custom_build_fn`.

## Commands

- Install: `pip install -e .`
- Env check: `./scripts/check_python_env.sh`
- Run: `python -m ffmpeg_builder` (or `./ffmpeg_builder`)
- Tests (pure unit tests for config/state/components — no integration tests): `pytest tests/`. Do NOT run bare `pytest` at the root: it also collects `workspace/packages/` (extracted third-party sources, gitignored) and crashes on their test programs.
- Lint/typecheck: `black .`, `mypy <files>`. mypy enforces `disallow_untyped_defs = true` (annotate new code) and currently has a pre-existing baseline of errors (missing `tqdm`/`yaml` stubs, untyped test functions); it also requires `python_version >= 3.10` in `pyproject.toml` (mypy 2.x rejects 3.8). Run mypy on specific files, not `.`, to avoid crawling `workspace/`.

## Conventions (verified in code, preserved from .github/copilot-instructions.md)

- Keep component behavior declarative in the registry; prefer `platform_overrides` / `configure_args_override` / `skip_condition` / `custom_build_fn` over ad-hoc per-component branching in `builder.py`.
- Preserve `ComponentStatus` values exactly (`pending`, `system`, `downloading`, `configuring`, `building`, `installing`, `completed`, `failed`, `skipped`) — state recovery and dashboard rendering depend on them.
- Pass `detail="..."` on every status transition (via `mark_component_status`) so the live dashboard shows the running command/progress; `detail` is transient and not persisted.
- Raise build failures as `BuildError(component, message, log_file)` so the UI error handler can show component + log context.
- Builds are offline-first: archives resolve from `third_party/sources`; network fetching is opt-in via `allow_network_downloads`.
- `windows-msys2-ucrt64` and `linux-wsl2` are distinct backends with separate component-eligibility rules; do not generalize native Linux/Windows assumptions onto them.

## Gotchas

- `third_party/sources` archives are Git LFS-tracked (`.gitattributes`); run `git lfs pull` after clone. A 2026-08-09 migration converted the committed archives to LFS objects because plain blobs over GitHub's 100MB file limit (e.g. AMF-1.5.0.tar.gz, 170MB) are rejected. Check that an archive is a real tarball, not a 3-line LFS pointer (`version https://git-lfs.github.com/spec/v1`), before debugging extraction failures.
- A full build is a long, hardware-dependent manual process (~20-60 min, ~10 GB, full toolchain). Verify code changes with unit tests; do not expect a CI build.
- On component failure, inspect `workspace/logs/<component>_<step>.log` and the resume state in `workspace/build_state.json`.

## Docs

- `docs/DeveloperReadme.md` is the authoritative architecture doc (module responsibilities, data flow, extension points) and is not linked from the root README. `docs/Fix-Plan.md` tracks the code-review refactor status.
