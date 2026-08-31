"""Tests for UI screen input validation (ConfigScreen numeric prompts)."""

import io

from rich.console import Console

from ffmpeg_builder.config import BuildConfig
from ffmpeg_builder.ui import screens
from ffmpeg_builder.ui.screens import ConfigScreen, _parse_positive_int


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


def _patch_prompts(monkeypatch, jobs_answers, workers_answers):
    """Patch Confirm.ask / Prompt.ask to feed scripted answers.

    All boolean questions answer False; the jobs/workers prompts consume
    from the provided iterators (exhaustion raises StopIteration, which
    fails the test loudly if the prompt order changes).
    """

    def fake_confirm_ask(prompt, **kwargs):
        return False

    def fake_prompt_ask(prompt, **kwargs):
        text = str(prompt)
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
