"""Tests for UI screen input validation (ConfigScreen numeric prompts)."""

import io

from rich.console import Console

from ffmpeg_builder.config import BuildConfig
from ffmpeg_builder.toolchain_registry import (
    ComponentToolchainPlan,
    ToolchainAssignment,
    ToolchainRegistry,
)
from ffmpeg_builder.toolchain_store import ToolchainStore
from ffmpeg_builder.ui import screens
from ffmpeg_builder.ui.screens import (
    ConfigScreen,
    InfoScreen,
    ToolchainPlanScreen,
    ToolchainStoreScreen,
    _parse_positive_int,
)


class TestParsePositiveInt:
    """_parse_positive_int(): only positive integers are accepted."""

    def test_valid(self):
        assert _parse_positive_int("4") == 4

    def test_surrounding_whitespace(self):
        assert _parse_positive_int(" 7 ") == 7

    def test_zero_rejected(self):
        assert _parse_positive_int("0") is None

    def test_negative_rejected(self):
        assert _parse_positive_int("-2") is None

    def test_non_numeric_rejected(self):
        assert _parse_positive_int("abc") is None

    def test_float_rejected(self):
        assert _parse_positive_int("4.5") is None


def _patch_prompts(monkeypatch, jobs_answers, workers_answers, version_answers=None):
    """Patch Confirm.ask / Prompt.ask to feed scripted answers.

    All boolean questions answer False; version, jobs, and workers prompts
    consume from their provided iterators. A missing version iterator accepts
    the prompt default. Exhaustion raises StopIteration, which fails the test
    loudly if the prompt order changes.
    """

    def fake_confirm_ask(prompt, **kwargs):
        return False

    def fake_prompt_ask(prompt, **kwargs):
        text = str(prompt)
        if "FFmpeg version" in text:
            if version_answers is None:
                return kwargs["default"]
            return next(version_answers)
        if "parallel jobs" in text:
            return next(jobs_answers)
        if "download workers" in text:
            return next(workers_answers)
        return ""  # "Press Enter to continue"

    monkeypatch.setattr(screens.Confirm, "ask", staticmethod(fake_confirm_ask))
    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(fake_prompt_ask))


def test_config_screen_reprompts_until_jobs_valid(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ConfigScreen(console)
    config = BuildConfig()

    _patch_prompts(monkeypatch, iter(["abc", "0", "8"]), iter(["3"]))

    result = screen.show(config)

    assert result.num_jobs == "8"
    assert result.download_workers == 3


def test_config_screen_accepts_auto_jobs(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ConfigScreen(console)
    config = BuildConfig(num_jobs="8")

    _patch_prompts(monkeypatch, iter(["auto"]), iter(["xyz", "2"]))

    result = screen.show(config)

    assert result.num_jobs == "auto"
    assert result.download_workers == 2


def test_config_screen_updates_ffmpeg_version(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ConfigScreen(console)
    config = BuildConfig()

    _patch_prompts(monkeypatch, iter(["auto"]), iter(["2"]), iter(["9.0"]))

    result = screen.show(config)

    assert result.ffmpeg_version == "9.0"


def test_config_screen_hides_blocked_ffmpeg_versions(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ConfigScreen(console)
    config = BuildConfig()

    captured = {}

    def fake_confirm_ask(prompt, **kwargs):
        return False

    def fake_prompt_ask(prompt, **kwargs):
        text = str(prompt)
        if "FFmpeg version" in text:
            captured["choices"] = kwargs.get("choices")
            return "9.0"
        if "parallel jobs" in text:
            return "auto"
        if "download workers" in text:
            return "2"
        return ""

    monkeypatch.setattr(screens.Confirm, "ask", staticmethod(fake_confirm_ask))
    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(fake_prompt_ask))

    result = screen.show(config, available_ffmpeg_versions=["9.0"])

    assert captured["choices"] == ["9.0"]
    assert result.ffmpeg_version == "9.0"


def test_config_screen_rejects_empty_policy_version_list(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ConfigScreen(console)

    _patch_prompts(monkeypatch, iter(["auto"]), iter(["2"]))

    try:
        screen.show(BuildConfig(), available_ffmpeg_versions=[])
    except ValueError as exc:
        assert str(exc) == "No FFmpeg versions available for the current policy."
    else:
        raise AssertionError("Expected ValueError for empty FFmpeg version list")


def test_toolchain_plan_screen_renders(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ToolchainPlanScreen(console)
    plan = [
        ComponentToolchainPlan(component="x264", toolchain="n/a", assignment=None),
        ComponentToolchainPlan(
            component="nv-codec",
            toolchain="gcc12",
            assignment=ToolchainAssignment(mode="deploy", profile="gcc12"),
        ),
        ComponentToolchainPlan(
            component="ffmpeg",
            toolchain="n/a",
            assignment=ToolchainAssignment(mode="system"),
        ),
    ]

    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(lambda *args, **kwargs: ""))

    screen.show(plan)

    output = console.file.getvalue()
    assert "Toolchain Plan" in output
    assert "deploy: gcc12" in output
    assert "unresolved (would skip)" in output


def test_toolchain_plan_screen_renders_cuda_host(monkeypatch):
    console = Console(file=io.StringIO())
    screen = ToolchainPlanScreen(console)
    plan = [
        ComponentToolchainPlan(
            component="nv-codec",
            toolchain="n/a",
            assignment=ToolchainAssignment(mode="system"),
            cuda_toolchain="gcc12",
        ),
    ]

    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(lambda *args, **kwargs: ""))

    screen.show(plan)

    assert "cuda: gcc12" in console.file.getvalue()


def test_toolchain_store_screen_quits(monkeypatch, tmp_path):
    import yaml

    registry_path = tmp_path / "toolchains.yaml"
    data = {"toolchains": {"gcc12": {"kind": "compiler", "platforms": ["linux"]}}}
    registry_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    registry = ToolchainRegistry(registry_path)
    store = ToolchainStore(
        tmp_path / "store",
        workspace=tmp_path / "workspace",
        source_archives_dir=tmp_path / "sources",
    )
    console = Console(file=io.StringIO())
    screen = ToolchainStoreScreen(console)

    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(lambda *args, **kwargs: "q"))
    screen.show(store, registry)

    assert "Toolchain Store" in console.file.getvalue()


def test_info_screen_shows_toolchain_skip_reason(monkeypatch):
    from ffmpeg_builder.components import BuildSystem, Component, ComponentCategory

    console = Console(file=io.StringIO(), width=120)
    screen = InfoScreen(console)
    gated = Component(
        name="needs-gcc12",
        version="1.0",
        url="https://example.org/needs.tar.gz",
        category=ComponentCategory.OTHER_LIB,
        build_system=BuildSystem.AUTOTOOLS,
        toolchain="gcc12",
    )
    plain = Component(
        name="plain",
        version="1.0",
        url="https://example.org/plain.tar.gz",
        category=ComponentCategory.OTHER_LIB,
        build_system=BuildSystem.AUTOTOOLS,
    )
    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(lambda *args, **kwargs: "q"))
    screen.show(
        [plain],
        [plain, gated],
        toolchain_reasons={"needs-gcc12": "toolchain 'gcc12' not installed"},
    )
    output = console.file.getvalue()
    assert "toolchain: toolchain 'gcc12' not installed" in output
