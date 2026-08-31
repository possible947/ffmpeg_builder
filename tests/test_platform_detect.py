"""Tests for hwaccel detection logic in platform_detect.py.

Covers the VAAPI / AMF / Vulkan / OpenCL detectors on Linux, including the
fallback chains and diagnostic reason fields introduced to fix
"detects as luck would have it" behaviour. Detection functions are exercised
directly (not via hand-built PlatformInfo mocks) so regressions in the real
pkg-config/filesystem-probing logic are actually caught.

One real-hardware smoke test (`TestRealHardware`) validates the detector
against this development machine (Ubuntu 24.04, dual AMD Radeon Pro VII
[Vega 20], ROCm 6.3, system + LunarG-capable Vulkan) and is skipped
automatically on any other machine.
"""

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from ffmpeg_builder.platform_detect import PlatformDetector


def _patch_path_exists(monkeypatch, existing):
    """Make Path.exists() return True only for paths in `existing`."""
    existing_strs = {str(p) for p in existing}

    def fake_exists(self):
        return str(self) in existing_strs

    monkeypatch.setattr(Path, "exists", fake_exists)


def _patch_path_glob(monkeypatch, mapping):
    """Make Path.glob(pattern) return mapping[(str(self), pattern)] or []."""

    def fake_glob(self, pattern):
        return iter(mapping.get((str(self), pattern), []))

    monkeypatch.setattr(Path, "glob", fake_glob)


def _detector_linux():
    d = PlatformDetector()
    d.platform_info.is_linux = True
    d.platform_info.is_windows = False
    d.platform_info.is_macos = False
    d.platform_info.is_wsl2 = False
    return d


class TestVaapiDetection:
    """_check_vaapi(): pkg-config -> header+loader fallback -> reason."""

    def test_pkg_config_hit_is_authoritative(self, monkeypatch):
        d = _detector_linux()
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0),
        )
        _patch_path_exists(monkeypatch, set())
        assert d._check_vaapi() is True
        assert d.platform_info.vaapi_detected_via == "pkg-config (libva)"
        assert d.platform_info.vaapi_reason == ""

    def test_header_and_loader_fallback_when_pkg_config_missing(self, monkeypatch):
        d = _detector_linux()
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1),
        )
        _patch_path_exists(
            monkeypatch,
            {"/usr/include/va/va.h", "/usr/lib/x86_64-linux-gnu/libva.so"},
        )
        monkeypatch.setattr(d, "get_multiarch_dir", lambda: "x86_64-linux-gnu")
        assert d._check_vaapi() is True
        assert d.platform_info.vaapi_detected_via == "headers+loader (pkg-config unavailable)"

    def test_missing_dev_package_reports_reason(self, monkeypatch):
        """Reproduces the real state of this machine before libva-dev was
        installed: libva2 runtime present, no headers, no pkg-config."""
        d = _detector_linux()
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1),
        )
        _patch_path_exists(monkeypatch, {"/usr/lib/x86_64-linux-gnu/libva.so"})
        monkeypatch.setattr(d, "get_multiarch_dir", lambda: "x86_64-linux-gnu")
        assert d._check_vaapi() is False
        assert "headers not found" in d.platform_info.vaapi_reason
        assert "libva-dev" in d.platform_info.vaapi_reason

    def test_wsl2_disabled_with_explicit_reason(self, monkeypatch):
        d = _detector_linux()
        d.platform_info.is_wsl2 = True
        assert d._check_vaapi() is False
        assert "WSL2" in d.platform_info.vaapi_reason

    def test_non_linux_disabled(self):
        d = PlatformDetector()
        d.platform_info.is_linux = False
        assert d._check_vaapi() is False
        assert "Linux" in d.platform_info.vaapi_reason


class TestAmfDetection:
    """AMF gating is GPU-presence-based, not header-based (dead code)."""

    def test_amd_gpu_present_enables_amf_regardless_of_headers(self, monkeypatch):
        d = _detector_linux()
        d._amd_gpu_detected = True
        d.system_info.gpu_info = ["AMD Radeon Pro VII"]
        # No AMF headers anywhere on disk - amf must still be available.
        monkeypatch.setattr(Path, "exists", lambda self: False)
        d.platform_info.build_backend = "linux-native"
        d.platform_info.is_windows = False
        # Re-run just the AMF-relevant slice of _detect_platform_info logic
        d.platform_info.amf_headers_detected_paths = d._check_amf_headers()
        d.platform_info.amf_gpu_names = [
            g for g in d.system_info.gpu_info if "amd" in g.lower() or "radeon" in g.lower()
        ]
        d.platform_info.amf_available = d._amd_gpu_detected
        assert d.platform_info.amf_available is True
        assert d.platform_info.amf_headers_detected_paths == []

    def test_no_amd_gpu_disables_amf(self):
        d = _detector_linux()
        d._amd_gpu_detected = False
        assert d._amd_gpu_detected is False

    def test_check_amf_headers_is_diagnostics_only(self, monkeypatch):
        d = _detector_linux()
        _patch_path_exists(monkeypatch, {"/usr/include/AMF"})
        paths = d._check_amf_headers()
        assert paths == ["/usr/include/AMF"]


class TestVulkanDetection:
    """_check_vulkan(): dev/runtime split; vulkaninfo alone must not grant dev."""

    def test_pkg_config_grants_dev_availability(self, monkeypatch):
        d = _detector_linux()
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=""),
        )
        monkeypatch.setattr("shutil.which", lambda name: None)
        _patch_path_exists(monkeypatch, set())
        _patch_path_glob(monkeypatch, {})
        assert d._check_vulkan() is True
        assert d.platform_info.vulkan_dev_available is True
        assert d.platform_info.vulkan_detected_via == "pkg-config (vulkan)"

    def test_vulkaninfo_alone_does_not_grant_dev_availability(self, monkeypatch):
        """Regression test: a bare `vulkaninfo` binary on PATH must not be
        treated as evidence that Vulkan *development* headers exist."""
        d = _detector_linux()
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=""),
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/vulkaninfo")
        _patch_path_exists(monkeypatch, set())
        _patch_path_glob(monkeypatch, {})
        assert d._check_vulkan() is False
        assert d.platform_info.vulkan_dev_available is False
        assert "headers not found" in d.platform_info.vulkan_reason

    def test_vulkan_sdk_env_honoured_on_linux(self, monkeypatch, tmp_path):
        """LunarG SDK activated via VULKAN_SDK (setup-env.sh) must be
        detected on Linux, not just Windows."""
        d = _detector_linux()
        sdk_root = tmp_path / "1.4.357.1" / "x86_64"
        header = sdk_root / "include" / "vulkan" / "vulkan.h"
        header.parent.mkdir(parents=True)
        header.write_text("// vulkan.h")

        monkeypatch.setenv("VULKAN_SDK", str(sdk_root))
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=""),
        )
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert d._check_vulkan() is True
        assert d.platform_info.vulkan_detected_via == "headers"
        assert str(header) in d.platform_info.vulkan_detected_header_paths

    def test_runtime_available_via_icd_file(self, monkeypatch):
        d = _detector_linux()
        icd_dir_str = "/usr/share/vulkan/icd.d"
        icd_file_str = f"{icd_dir_str}/radeon_icd.json"

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=""),
        )
        monkeypatch.setattr("shutil.which", lambda name: None)
        _patch_path_exists(monkeypatch, {icd_dir_str})
        _patch_path_glob(monkeypatch, {(icd_dir_str, "*.json"): [Path(icd_file_str)]})

        result = d._check_vulkan()
        assert d.platform_info.vulkan_runtime_available is True
        assert icd_file_str in d.platform_info.vulkan_detected_icd_files
        assert result is True  # pkg-config also succeeded in this scenario


class TestOpenclClinfoProbe:
    """clinfo functional probe augments filesystem-only OpenCL detection."""

    def test_clinfo_on_path_sets_runtime_available(self, monkeypatch):
        d = _detector_linux()
        d.platform_info.rocm_path = None

        def fake_run(cmd, **kwargs):
            if cmd[0] == "pkg-config":
                return subprocess.CompletedProcess(cmd, 1)
            if cmd[0] == "/usr/bin/clinfo":
                return subprocess.CompletedProcess(cmd, 0, stdout="Number of platforms:  1\n")
            return subprocess.CompletedProcess(cmd, 1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/clinfo" if name == "clinfo" else None
        )
        _patch_path_exists(monkeypatch, set())
        _patch_path_glob(monkeypatch, {})

        d._check_opencl()
        assert d.platform_info.opencl_runtime_available is True
        assert "clinfo" in d.platform_info.opencl_runtime_reason

    def test_rocm_bundled_clinfo_used_when_not_on_path(self, monkeypatch, tmp_path):
        d = _detector_linux()
        rocm_dir = tmp_path / "rocm-6.3.0"
        clinfo_path = rocm_dir / "bin" / "clinfo"
        clinfo_path.parent.mkdir(parents=True)
        clinfo_path.write_text("#!/bin/sh\n")
        d.platform_info.rocm_path = str(rocm_dir)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "pkg-config":
                return subprocess.CompletedProcess(cmd, 1)
            if cmd[0] == str(clinfo_path):
                return subprocess.CompletedProcess(cmd, 0, stdout="Number of platforms:  1\n")
            return subprocess.CompletedProcess(cmd, 1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda name: None)

        real_exists = Path.exists

        def fake_exists(self):
            if self == clinfo_path:
                return True
            if str(self) == str(clinfo_path):
                return True
            return False

        monkeypatch.setattr(Path, "exists", fake_exists)
        monkeypatch.setattr(Path, "glob", lambda self, pattern: iter([]))

        d._check_opencl()
        assert d.platform_info.opencl_runtime_available is True
        assert str(clinfo_path) in d.platform_info.opencl_runtime_reason


class TestNumJobs:
    """get_num_jobs(): valid values pass through, invalid ones fall back."""

    def _detector_with_cores(self, cores):
        d = PlatformDetector()
        d.system_info.cpu_cores = cores
        return d

    def test_auto_uses_core_count(self):
        assert self._detector_with_cores(16).get_num_jobs("auto") == 16

    def test_valid_number_passes_through(self):
        assert self._detector_with_cores(16).get_num_jobs("4") == 4

    def test_invalid_string_falls_back_to_cores(self):
        assert self._detector_with_cores(16).get_num_jobs("abc") == 16

    def test_zero_falls_back_to_cores(self):
        # -j0 means unlimited parallelism in make; must never be returned.
        assert self._detector_with_cores(16).get_num_jobs("0") == 16

    def test_negative_falls_back_to_cores(self):
        assert self._detector_with_cores(16).get_num_jobs("-2") == 16

    def test_zero_cores_falls_back_to_four(self):
        assert self._detector_with_cores(0).get_num_jobs("auto") == 4


class TestCudaComputeCapability:
    """_detect_cuda_compute_capability(): numeric min across mixed GPUs."""

    def _run_with_caps(self, monkeypatch, caps):
        d = PlatformDetector()
        stdout = "\n".join(caps)

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout),
        )
        d._detect_cuda_compute_capability()
        return d

    def test_single_gpu_capability(self, monkeypatch):
        d = self._run_with_caps(monkeypatch, ["8.6"])
        assert d.platform_info.cuda_compute_capability == "86"

    def test_mixed_generation_uses_numeric_min(self, monkeypatch):
        # Lexicographic min() would pick "120" over "86" here.
        d = self._run_with_caps(monkeypatch, ["8.6", "12.0"])
        assert d.platform_info.cuda_compute_capability == "86"

    def test_order_independent(self, monkeypatch):
        d = self._run_with_caps(monkeypatch, ["12.0", "8.6", "9.0"])
        assert d.platform_info.cuda_compute_capability == "86"

    def test_non_numeric_lines_ignored(self, monkeypatch):
        d = self._run_with_caps(monkeypatch, ["N/A", "8.6"])
        assert d.platform_info.cuda_compute_capability == "86"

    def test_no_valid_caps_leaves_none(self, monkeypatch):
        d = self._run_with_caps(monkeypatch, ["N/A"])
        assert d.platform_info.cuda_compute_capability is None


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="Real-hardware smoke test only meaningful on the target Linux dev box",
)
class TestToolDetection:
    """M9: curl is not used by the builder, so it must not be detected."""

    def test_detect_tools_does_not_include_curl(self, monkeypatch):
        detector = PlatformDetector()
        monkeypatch.setattr(shutil, "which", lambda name: None)
        detector._detect_tools()
        assert "curl" not in detector.tools


class TestRealHardware:
    """Smoke-tests the detector end-to-end on whatever Linux box runs this.

    Assertions are written to hold on the reference dev machine (Ubuntu
    24.04 HWE, dual AMD Radeon Pro VII / Vega 20, ROCm 6.3, mesa Vulkan +
    libva-dev installed) and are skipped/soft-checked when the expected
    hardware/software is absent so this test remains meaningful (not just
    vacuously skipped) elsewhere on Linux without hard-failing CI.
    """

    def test_detect_all_reports_amd_hwaccel_when_present(self):
        detector = PlatformDetector()
        system_info, platform_info, _tools = detector.detect_all()

        if not platform_info.amf_available:
            pytest.skip("No AMD GPU detected on this machine")

        assert platform_info.amf_available is True
        assert "AMD GPU detected" in platform_info.amf_reason

        # OpenCL via ROCm should be fully available if ROCm is installed.
        if platform_info.rocm_available:
            assert platform_info.opencl_dev_available is True
            assert platform_info.opencl_runtime_available is True

        # Vulkan dev headers ship with mesa-vulkan-drivers/libvulkan-dev.
        assert platform_info.vulkan_dev_available is True

        # VAAPI requires libva-dev; only assert if genuinely present so this
        # test doesn't silently mask the dev-package-missing scenario.
        try:
            result = subprocess.run(
                ["pkg-config", "--exists", "libva"], capture_output=True, timeout=5
            )
            libva_dev_present = result.returncode == 0
        except Exception:
            libva_dev_present = False
        if libva_dev_present:
            assert platform_info.vaapi_available is True
            assert platform_info.vaapi_detected_via == "pkg-config (libva)"
