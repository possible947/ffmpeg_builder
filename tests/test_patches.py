from pathlib import Path

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.patches.base import SourcePatch, assert_patch_absent, assert_patch_present
from ffmpeg_builder.patches.registry import PatchRegistry, get_patch_registry


def test_assert_patch_present_and_absent_round_trip(tmp_path: Path):
    target = tmp_path / "sample.txt"
    target.write_text("#include <cstdint>\nclean\n", encoding="utf-8")

    assert_patch_present("demo", target, "#include <cstdint>", "example")
    assert_patch_absent("demo", target, "bad_marker", "example")

    with pytest.raises(BuildError, match="version may have changed"):
        assert_patch_present("demo", target, "missing-marker", "example")


def test_patch_registry_applies_only_matching_component_and_condition(tmp_path: Path):
    registry = PatchRegistry()

    def add_header(path: Path, component, strategy):
        text = path.read_text(encoding="utf-8")
        if "#include <cstdint>" not in text:
            path.write_text(text + "#include <cstdint>\n", encoding="utf-8")

    registry.register(
        SourcePatch(
            name="x265-cstdint",
            component_name="x265",
            target_rel_path="json11.cpp",
            apply_fn=add_header,
            condition=lambda component, strategy: component.version == "1.0",
        )
    )

    target = tmp_path / "json11.cpp"
    target.write_text("#include <limits>\n", encoding="utf-8")

    registry.apply_patches(type("C", (), {"name": "x265", "version": "1.0"})(), tmp_path, None)

    assert "#include <cstdint>" in target.read_text(encoding="utf-8")

    other = type("C", (), {"name": "x265", "version": "2.0"})()
    target.write_text("#include <limits>\n", encoding="utf-8")
    registry.apply_patches(other, tmp_path, None)
    assert target.read_text(encoding="utf-8") == "#include <limits>\n"


def _comp(name: str, version: str = "1.0"):
    return type("C", (), {"name": name, "version": version})()


def test_xvidcore_bool_patch_applies_and_is_idempotent(tmp_path: Path):
    (tmp_path / "src").mkdir()
    enc = tmp_path / "src" / "encoder.h"
    enc.write_text("typedef int bool;\nint x;\n", encoding="utf-8")

    registry = get_patch_registry()
    registry.apply_patches(_comp("xvidcore"), tmp_path, None)
    text = enc.read_text(encoding="utf-8")
    assert "__STDC_VERSION__ < 202311L" in text
    assert text.count("typedef int bool;") == 1

    # Second run must not fail or duplicate the guard.
    registry.apply_patches(_comp("xvidcore"), tmp_path, None)
    assert enc.read_text(encoding="utf-8").count("typedef int bool;") == 1


def test_x265_json11_patch_inserts_cstdint_after_limits(tmp_path: Path):
    target = tmp_path / "source" / "dynamicHDR10" / "json11"
    target.mkdir(parents=True)
    cpp = target / "json11.cpp"
    cpp.write_text("#include <limits>\n", encoding="utf-8")

    get_patch_registry().apply_patches(_comp("x265"), tmp_path, None)
    assert cpp.read_text(encoding="utf-8") == "#include <limits>\n#include <cstdint>\n"


def test_ffmpeg_9_vulkan_patch_only_for_9(tmp_path: Path):
    d = tmp_path / "fftools"
    d.mkdir()
    renderer = d / "ffplay_renderer.c"
    renderer.write_text('#include "libavutil/internal.h"\n', encoding="utf-8")

    registry = get_patch_registry()
    registry.apply_patches(_comp("ffmpeg", "9.0"), tmp_path, None)
    assert "hwcontext_vulkan.h" in renderer.read_text(encoding="utf-8")

    renderer.write_text('#include "libavutil/internal.h"\n', encoding="utf-8")
    registry.apply_patches(_comp("ffmpeg", "8.1"), tmp_path, None)
    assert "hwcontext_vulkan.h" not in renderer.read_text(encoding="utf-8")
