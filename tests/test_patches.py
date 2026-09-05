from pathlib import Path

import pytest

from build_types import BuildError
from patches.base import SourcePatch, assert_patch_absent, assert_patch_present
from patches.registry import PatchRegistry


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
