# Phase 5 Implementation Plan: Orchestrator Refactoring, Integration Testing & Verification

## 1. Overview & Objective

Phase 5 completes the refactoring by turning [builder.py](builder.py) into a clean, concise orchestration facade (~300–400 lines) that delegates platform behavior to `platforms/`, source patching to `patches/`, and build logic to `builders/`.

It also performs comprehensive regression testing, updates the developer documentation, and validates all CI quality gates.

---

## 2. Detailed Task Breakdown

### Task 5.1: Streamline `FFmpegBuilder` in [builder.py](builder.py)
- **Action**:
  - Remove all inline custom build methods (`build_x264`, `build_x265`, `build_openssl`, `build_ffmpeg`, etc.).
  - Remove platform-specific private methods (`_to_msys_path`, `_normalize_windows_path_for_flags`, `_resolve_darwin_openmp_runtime`, `_is_windows_ucrt64_backend`, etc.) in favor of `self.platform_strategy`.
  - Retain core public API methods:
    - `__init__(config, workspace, packages, state_manager, platform_detector, ...)`
    - `get_build_env(component: Optional[Component]) -> Dict[str, str]`
    - `prefetch_downloads(components: List[Component]) -> None`
    - `build_component(component: Component) -> None`
    - `make_release_bundle() -> Path`
  - Ensure `build_component` delegates:
    1. System component check & skip verification.
    2. Download/extract archive via `Downloader`.
    3. Source patch application via `PatchRegistry`.
    4. Build dispatch via `ComponentBuilder` / `CUSTOM_BUILDERS` / `BuildSystem` runners.
    5. Post-install execution.

### Task 5.2: Comprehensive Test Suite Updates
- **Action**:
  - Update [tests/test_builder_split.py](tests/test_builder_split.py) to cover:
    - Modular builder imports and dispatch.
    - Platform strategy resolution and flag formatting.
    - Patch engine application and safety assertion enforcement.
  - Fix any OS-specific test edge cases (e.g., Windows symlink privileges in macOS bundle mock test).

### Task 5.3: Verification of CI Quality Gates
- **Action**:
  - Execute full test suite: `pytest tests/`.
  - Validate mypy baseline check: `python scripts/check_mypy_baseline.py`.
  - Validate black formatting: `black --check .`.

### Task 5.4: Documentation Updates
- **Action**:
  - Update [docs/DeveloperReadme.md](docs/DeveloperReadme.md) to document the new `platforms/`, `patches/`, and `builders/` subpackages.
  - Update [docs/CHANGELOG.md](docs/CHANGELOG.md) recording the architecture modularization.

---

## 3. Acceptance Criteria

1. [builder.py](builder.py) is reduced from ~2,600 lines to under 450 lines.
2. The entire test suite passes (`pytest tests/`).
3. `python scripts/check_mypy_baseline.py` passes without introducing new errors.
4. `black --check .` passes without formatting discrepancies.
5. Zero breaking changes for CLI invocation (`python -m ffmpeg_builder`), configuration files, or build state resumption.
