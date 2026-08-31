"""Tests for state serialization, StateManager, and ComponentStatus."""

import json
from pathlib import Path

import pytest

from ffmpeg_builder import state as state_module
from ffmpeg_builder.state import (
    IN_PROGRESS_STATUSES,
    TERMINAL_STATUSES,
    BuildState,
    ComponentState,
    ComponentStatus,
    StateManager,
)


class TestComponentStatus:
    """Test status enum values and sets."""

    def test_terminal_statuses(self):
        assert ComponentStatus.COMPLETED in TERMINAL_STATUSES
        assert ComponentStatus.SYSTEM in TERMINAL_STATUSES
        assert ComponentStatus.SKIPPED in TERMINAL_STATUSES
        assert ComponentStatus.BUILDING not in TERMINAL_STATUSES
        assert ComponentStatus.FAILED not in TERMINAL_STATUSES

    def test_in_progress_statuses(self):
        assert ComponentStatus.DOWNLOADING in IN_PROGRESS_STATUSES
        assert ComponentStatus.CONFIGURING in IN_PROGRESS_STATUSES
        assert ComponentStatus.BUILDING in IN_PROGRESS_STATUSES
        assert ComponentStatus.INSTALLING in IN_PROGRESS_STATUSES
        assert ComponentStatus.PENDING not in IN_PROGRESS_STATUSES
        assert ComponentStatus.COMPLETED not in IN_PROGRESS_STATUSES


class TestComponentState:
    """Test ComponentState dataclass."""

    def test_default_state(self):
        s = ComponentState()
        assert s.status == ComponentStatus.PENDING
        assert s.version is None
        assert s.built_at is None
        assert s.error_message is None
        assert s.log_file is None


class TestBuildStateRoundTrip:
    """Test to_dict / from_dict serialization round-trip."""

    def test_empty_roundtrip(self):
        state = BuildState()
        data = state.to_dict()
        restored = BuildState.from_dict(data)
        assert len(restored.components) == 0
        assert restored.current_step == 0
        assert restored.total_steps == 0

    def test_full_roundtrip(self, sample_build_state_dict):
        state = BuildState.from_dict(sample_build_state_dict)
        data = state.to_dict()

        # Build ID preserved
        assert data["build_id"] == "test-build-001"

        # Components present
        assert "pkg-config" in data["components"]
        assert "x264" in data["components"]
        assert "x265" in data["components"]

        # pkg-config completed with version
        pc = data["components"]["pkg-config"]
        assert pc["status"] == "completed"
        assert pc["version"] == "0.29.2"

        # x264 failed with error message
        x264 = data["components"]["x264"]
        assert x264["status"] == "failed"
        assert x264["error_message"] == "configure failed"

    def test_in_progress_statuses_reset_to_pending(self, sample_build_state_dict):
        """IN_PROGRESS statuses should be reset to PENDING on load."""
        state = BuildState.from_dict(sample_build_state_dict)
        # x265 was "building" — should become PENDING
        assert state.components["x265"].status == ComponentStatus.PENDING


class TestStateManager:
    """Test StateManager persistence and thread safety."""

    def test_load_existing_state(self, state_file):
        mgr = StateManager(state_file)
        st = mgr.load()
        assert st is not None
        assert st.build_id == "test-build-001"
        assert len(st.components) == 3

    def test_save_new_state(self, tmp_path):
        sp = tmp_path / "state.json"
        mgr = StateManager(sp)
        state = mgr.get()
        mgr.mark_component_status("pkg-config", ComponentStatus.COMPLETED, version="0.29.2")
        mgr.save()

        # Reload and check
        mgr2 = StateManager(sp)
        loaded = mgr2.load()
        assert loaded is not None
        assert "pkg-config" in loaded.components
        assert loaded.components["pkg-config"].status == ComponentStatus.COMPLETED
        assert loaded.components["pkg-config"].version == "0.29.2"

    def test_reset_clears_state(self, state_file):
        mgr = StateManager(state_file)
        mgr.load()
        mgr.reset()
        assert mgr.state is None
        assert not state_file.exists()

    def test_mark_failed_sets_error_message(self, empty_state_file):
        mgr = StateManager(empty_state_file)
        mgr.mark_component_status(
            "x264", ComponentStatus.FAILED, version="0480cb05", error_message="make: *** error"
        )
        state = mgr.get()
        comp = state.components["x264"]
        assert comp.status == ComponentStatus.FAILED
        assert comp.error_message == "make: *** error"

    def test_mark_completed_clears_error(self, empty_state_file):
        mgr = StateManager(empty_state_file)
        mgr.mark_component_status("comp", ComponentStatus.FAILED, error_message="oops")
        mgr.mark_component_status("comp", ComponentStatus.COMPLETED, version="1.0")
        comp = mgr.get().components["comp"]
        assert comp.status == ComponentStatus.COMPLETED
        assert comp.error_message is None

    def test_get_resume_point(self, empty_state_file):
        mgr = StateManager(empty_state_file)
        mgr.mark_component_status("a", ComponentStatus.COMPLETED, version="1.0")
        mgr.mark_component_status("b", ComponentStatus.FAILED, error_message="fail")
        mgr.mark_component_status("c", ComponentStatus.PENDING)
        point = mgr.get_resume_point()
        # First non-terminal is "b" (FAILED)
        assert point == "b"

    def test_is_component_completed(self, empty_state_file):
        mgr = StateManager(empty_state_file)
        mgr.mark_component_status("pkg-config", ComponentStatus.COMPLETED, version="0.29.2")
        assert mgr.is_component_completed("pkg-config", "0.29.2") is True
        assert mgr.is_component_completed("pkg-config", "1.0.0") is False  # version mismatch
        assert mgr.is_component_completed("unknown", "1.0") is False

    def test_status_listener_called(self, empty_state_file):
        mgr = StateManager(empty_state_file)
        events: list = []
        mgr.status_listener = lambda name, status, ver, err, detail: events.append(
            (name, status.value)
        )
        mgr.mark_component_status("comp", ComponentStatus.COMPLETED, version="1.0")
        assert len(events) == 1
        assert events[0] == ("comp", "completed")

    def test_save_creates_parent_dir(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "state.json"
        mgr = StateManager(deep)
        mgr.mark_component_status("x", ComponentStatus.COMPLETED)
        # File should now exist with parent dirs created
        assert deep.exists()

    def test_save_no_state_raises(self, tmp_path):
        sp = tmp_path / "state.json"
        mgr = StateManager(sp)
        with pytest.raises(ValueError, match="No state to save"):
            mgr.save()

    def test_default_path_anchored_to_project_root(self, monkeypatch, tmp_path):
        """M5: the default state path must not depend on the CWD."""
        monkeypatch.chdir(tmp_path)
        mgr = StateManager()
        assert mgr.state_path == state_module.PROJECT_ROOT / "workspace" / "build_state.json"


class TestStateFileRobustness:
    """Corrupt/forward-incompatible state files must not crash the app."""

    def test_from_dict_ignores_unknown_top_level_keys(self, sample_build_state_dict):
        data = dict(sample_build_state_dict)
        data["future_field_added_by_newer_version"] = {"x": 1}
        state = BuildState.from_dict(data)
        assert state.build_id == "test-build-001"
        assert len(state.components) == 3

    def test_from_dict_unknown_component_status_becomes_pending(self):
        data = {
            "build_id": "b",
            "started_at": "",
            "config": {},
            "components": {"x264": {"status": "some_future_status", "version": "1.0"}},
            "current_step": 1,
            "total_steps": 1,
        }
        state = BuildState.from_dict(data)
        assert state.components["x264"].status == ComponentStatus.PENDING

    def test_from_dict_missing_status_becomes_pending(self):
        data = {
            "build_id": "b",
            "started_at": "",
            "config": {},
            "components": {"x264": {"version": "1.0"}},
            "current_step": 1,
            "total_steps": 1,
        }
        state = BuildState.from_dict(data)
        assert state.components["x264"].status == ComponentStatus.PENDING

    def test_from_dict_non_dict_component_treated_as_pending(self):
        data = {
            "build_id": "b",
            "started_at": "",
            "config": {},
            "components": {"x264": "corrupted-entry"},
            "current_step": 1,
            "total_steps": 1,
        }
        state = BuildState.from_dict(data)
        assert state.components["x264"].status == ComponentStatus.PENDING

    def test_load_corrupt_json_returns_none(self, tmp_path, caplog):
        sp = tmp_path / "build_state.json"
        sp.write_text("{not valid json", encoding="utf-8")
        mgr = StateManager(sp)
        with caplog.at_level("WARNING"):
            assert mgr.load() is None
        assert "Ignoring unreadable build state file" in caplog.text

    def test_load_valid_state_still_works(self, state_file):
        mgr = StateManager(state_file)
        st = mgr.load()
        assert st is not None
        assert st.build_id == "test-build-001"

    def test_save_leaves_no_tmp_file(self, tmp_path):
        sp = tmp_path / "state.json"
        mgr = StateManager(sp)
        mgr.mark_component_status("x", ComponentStatus.COMPLETED)
        mgr.save()
        leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == []
        # File must be valid JSON after the atomic replace.
        json.loads(sp.read_text(encoding="utf-8"))
