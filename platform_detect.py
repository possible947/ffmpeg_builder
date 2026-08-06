"""Platform detection and system information gathering."""

import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class SystemInfo:
    """System information."""

    os_name: str = ""
    os_version: str = ""
    architecture: str = ""
    cpu_model: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    gpu_info: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            "os_name": self.os_name,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "cpu_model": self.cpu_model,
            "cpu_cores": self.cpu_cores,
            "ram_gb": self.ram_gb,
            "gpu_info": self.gpu_info,
        }


@dataclass
class ToolInfo:
    """Information about a tool."""

    name: str
    path: Optional[str] = None
    version: Optional[str] = None
    available: bool = False

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "available": self.available,
        }


@dataclass
class PlatformInfo:
    """Platform-specific information."""

    platform: str = "linux"
    build_backend: str = "linux-native"
    is_macos: bool = False
    is_linux: bool = False
    is_windows: bool = False
    is_arm64: bool = False
    is_wsl2: bool = False
    is_msys2: bool = False
    is_ucrt64: bool = False
    msystem: Optional[str] = None
    macports_clang: Optional[ToolInfo] = None
    cuda_available: bool = False
    cuda_path: Optional[str] = None
    cuda_compute_capability: Optional[str] = None
    libvmaf_cuda_supported: bool = False
    libvmaf_cuda_reason: str = ""
    vaapi_available: bool = False
    qsv_available: bool = False
    amf_available: bool = False
    vulkan_available: bool = False
    opencl_available: bool = False

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            "platform": self.platform,
            "build_backend": self.build_backend,
            "is_macos": self.is_macos,
            "is_linux": self.is_linux,
            "is_windows": self.is_windows,
            "is_arm64": self.is_arm64,
            "is_wsl2": self.is_wsl2,
            "is_msys2": self.is_msys2,
            "is_ucrt64": self.is_ucrt64,
            "msystem": self.msystem,
            "macports_clang": self.macports_clang.to_dict() if self.macports_clang else None,
            "cuda_available": self.cuda_available,
            "cuda_path": self.cuda_path,
            "cuda_compute_capability": self.cuda_compute_capability,
            "libvmaf_cuda_supported": self.libvmaf_cuda_supported,
            "libvmaf_cuda_reason": self.libvmaf_cuda_reason,
            "vaapi_available": self.vaapi_available,
            "qsv_available": self.qsv_available,
            "amf_available": self.amf_available,
            "vulkan_available": self.vulkan_available,
            "opencl_available": self.opencl_available,
        }


class PlatformDetector:
    """Detects platform and system information."""

    def __init__(self):
        """Initialize platform detector."""
        self.system_info = SystemInfo()
        self.platform_info = PlatformInfo()
        self.tools: Dict[str, ToolInfo] = {}
        self._amd_gpu_detected = False

    def detect_all(self) -> Tuple[SystemInfo, PlatformInfo, Dict[str, ToolInfo]]:
        """Detect all system information.

        Returns:
            Tuple of (SystemInfo, PlatformInfo, tools dict).
        """
        self._detect_system_info()
        self._detect_platform_info()
        self._detect_tools()

        return self.system_info, self.platform_info, self.tools

    def get_platform_name(self) -> str:
        """Get normalized platform name used by component filtering/build logic."""
        return self.platform_info.platform

    def get_build_backend_name(self) -> str:
        """Get normalized backend name (e.g. linux-wsl2, windows-msys2-ucrt64)."""
        return self.platform_info.build_backend

    def get_multiarch_dir(self) -> str:
        """Get Linux multiarch directory suffix based on architecture.

        Returns:
            Multiarch directory string (e.g., "x86_64-linux-gnu") or empty string.
        """
        if not self.platform_info.is_linux:
            return ""

        arch = self.system_info.architecture
        arch_map = {
            "x86_64": "x86_64-linux-gnu",
            "aarch64": "aarch64-linux-gnu",
            "arm64": "aarch64-linux-gnu",
            "armv7l": "arm-linux-gnueabihf",
            "i386": "i386-linux-gnu",
            "i686": "i386-linux-gnu",
        }
        return arch_map.get(arch, "")

    def _detect_system_info(self) -> None:
        """Detect system information."""
        self.system_info.os_name = platform.system()
        self.system_info.architecture = platform.machine()

        # Get proper OS name and version
        if platform.system() == "Linux":
            try:
                with open("/etc/os-release", "r") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            pretty_name = line.split("=", 1)[1].strip().strip('"')
                            self.system_info.os_name = pretty_name
                            self.system_info.os_version = ""
                            break
            except Exception:
                self.system_info.os_version = platform.version()
        elif platform.system() == "Darwin":
            self.system_info.os_version = platform.mac_ver()[0]
        else:
            self.system_info.os_version = platform.version()

        # CPU info
        try:
            if platform.system() == "Darwin":
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.system_info.cpu_model = result.stdout.strip()

                result = subprocess.run(
                    ["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True, timeout=5
                )
                self.system_info.cpu_cores = int(result.stdout.strip())

            elif platform.system() == "Linux":
                with open("/proc/cpuinfo", "r") as f:
                    content = f.read()
                    for line in content.split("\n"):
                        if "model name" in line:
                            self.system_info.cpu_model = line.split(":")[1].strip()
                            break

                    self.system_info.cpu_cores = content.count("processor")

                # RAM info
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if "MemTotal" in line:
                            ram_kb = int(line.split()[1])
                            self.system_info.ram_gb = ram_kb / (1024 * 1024)
                            break

                # GPU info
                self._detect_gpu_info()
            elif (
                platform.system() == "Windows"
                or platform.system().startswith("MSYS")
                or platform.system().startswith("MINGW")
                or platform.system().startswith("CYGWIN")
            ):
                self._detect_gpu_info_windows()

        except Exception:
            pass

        # macOS RAM
        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
                )
                ram_bytes = int(result.stdout.strip())
                self.system_info.ram_gb = ram_bytes / (1024**3)
            except Exception:
                pass

        if self.system_info.cpu_cores <= 0:
            self.system_info.cpu_cores = os.cpu_count() or 1

        if self.system_info.ram_gb <= 0 and (
            platform.system() == "Windows"
            or platform.system().startswith("MSYS")
            or platform.system().startswith("MINGW")
            or platform.system().startswith("CYGWIN")
        ):
            self.system_info.ram_gb = self._get_windows_ram_gb()

    def _detect_gpu_info_windows(self) -> None:
        """Detect GPU information on Windows/MSYS2."""
        self.system_info.gpu_info = []
        self._amd_gpu_detected = False

        def _add_gpu(gpu_name: str) -> None:
            gpu = gpu_name.strip()
            if not gpu or gpu.lower() == "name":
                return
            if gpu not in self.system_info.gpu_info:
                self.system_info.gpu_info.append(gpu)
            if "amd" in gpu.lower() or "radeon" in gpu.lower():
                self._amd_gpu_detected = True

        try:
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    _add_gpu(line)
        except Exception:
            pass

        if self.system_info.gpu_info:
            return

        # WMIC is deprecated and can be missing on recent Windows builds.
        # Fall back to PowerShell CIM query when available.
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    _add_gpu(line)
        except Exception:
            pass

    def _get_windows_ram_gb(self) -> float:
        """Read total RAM on Windows using GlobalMemoryStatusEx."""
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / (1024**3)
        except Exception:
            return 0.0

    def _detect_gpu_info(self) -> None:
        """Detect GPU information on Linux.

        Populates system_info.gpu_info with human readable GPU model names and
        stores whether an AMD GPU is present for AMF enablement.
        """
        self.system_info.gpu_info = []
        self._amd_gpu_detected = False

        lspci = shutil.which("lspci")
        if lspci:
            try:
                result = subprocess.run([lspci, "-nn"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if (
                            "VGA compatible controller" in line
                            or "3D controller" in line
                            or "Display controller" in line
                        ):
                            # Match vendor bracket and device model.
                            # Example: ... [AMD/ATI] Vega 20 [Radeon Pro VII/...] [1002:66a1] (rev 06)
                            match = re.search(
                                r"\[([A-Za-z/]+)\]\s+(.*?)\s+\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]",
                                line,
                            )
                            if match:
                                vendor_label = match.group(1)
                                model = match.group(2).split("[")[0].strip()
                                # Skip the ASPEED BMC graphics adapter
                                if "ASPEED" in model or "BMC" in model:
                                    continue
                                self.system_info.gpu_info.append(model)
                                if vendor_label in ("AMD/ATI", "AMD", "ATI"):
                                    self._amd_gpu_detected = True
            except Exception:
                pass

        # Fallback: enumerate DRM devices via sysfs if lspci parsing failed
        if not self.system_info.gpu_info:
            try:
                drm_path = Path("/sys/class/drm")
                if drm_path.exists():
                    seen = set()
                    for device in drm_path.iterdir():
                        if not device.name.startswith("card"):
                            continue
                        device_dir = device / "device"
                        vendor_file = device_dir / "vendor"
                        if vendor_file.exists():
                            vendor_id = vendor_file.read_text().strip()
                            if vendor_id in seen:
                                continue
                            seen.add(vendor_id)
                            if vendor_id == "0x1002":
                                self.system_info.gpu_info.append("AMD GPU")
                                self._amd_gpu_detected = True
                            elif vendor_id == "0x10de":
                                self.system_info.gpu_info.append("NVIDIA GPU")
                            elif vendor_id == "0x8086":
                                self.system_info.gpu_info.append("Intel GPU")
            except Exception:
                pass

    def _detect_platform_info(self) -> None:
        """Detect platform-specific information."""
        sys_name = platform.system()
        self.platform_info.is_macos = sys_name == "Darwin"
        self.platform_info.is_linux = sys_name == "Linux"
        self.platform_info.is_windows = (
            sys_name == "Windows"
            or sys_name.startswith("MSYS")
            or sys_name.startswith("MINGW")
            or sys_name.startswith("CYGWIN")
        )
        if self.platform_info.is_macos:
            self.platform_info.platform = "darwin"
        elif self.platform_info.is_windows:
            self.platform_info.platform = "windows"
        else:
            self.platform_info.platform = "linux"
        self.platform_info.is_arm64 = platform.machine() in ("arm64", "aarch64")
        self._detect_msys2_mode()

        # Detect WSL2
        if self.platform_info.is_linux:
            self.platform_info.is_wsl2 = self._check_wsl2()
        self.platform_info.build_backend = self._resolve_build_backend()

        # Detect AMF on Linux: enable when an AMD GPU is present, since the
        # AMF headers component downloads the required headers from GPUOpen.
        if self.platform_info.is_linux:
            self.platform_info.amf_available = getattr(self, "_amd_gpu_detected", False)
        elif self.platform_info.is_windows:
            self.platform_info.amf_available = any(
                "amd" in gpu.lower() or "radeon" in gpu.lower() for gpu in self.system_info.gpu_info
            )

        # Detect macports clang on macOS
        if self.platform_info.is_macos:
            self.platform_info.macports_clang = self._find_macports_clang()

        # Detect CUDA on Linux
        if self.platform_info.is_linux or self.platform_info.is_windows:
            self._detect_cuda()
            self._detect_libvmaf_cuda_support()
            self.platform_info.qsv_available = self._check_qsv()
            self.platform_info.vulkan_available = self._check_vulkan()
            self.platform_info.opencl_available = self._check_opencl()
        self.platform_info.vaapi_available = self._check_vaapi()

    def _detect_msys2_mode(self) -> None:
        """Detect MSYS2 runtime details when running on Windows."""
        msystem = os.environ.get("MSYSTEM")
        if not msystem:
            return
        self.platform_info.is_msys2 = True
        self.platform_info.msystem = msystem
        self.platform_info.is_ucrt64 = msystem.upper() == "UCRT64"

    def _resolve_build_backend(self) -> str:
        """Resolve build backend identifier for policy routing."""
        if self.platform_info.is_macos:
            return "darwin-native"

        if self.platform_info.is_linux:
            if self.platform_info.is_wsl2:
                return "linux-wsl2"
            return "linux-native"

        if self.platform_info.is_windows:
            if self.platform_info.is_msys2:
                if self.platform_info.is_ucrt64:
                    return "windows-msys2-ucrt64"
                msystem = (self.platform_info.msystem or "unknown").lower()
                return f"windows-msys2-{msystem}"
            return "windows-native"

        return "unknown"

    def _detect_cuda(self) -> None:
        """Detect CUDA installation."""
        # Try PATH first
        nvcc_path = shutil.which("nvcc")
        if nvcc_path:
            self.platform_info.cuda_available = True
            self.platform_info.cuda_path = nvcc_path
            self._detect_cuda_compute_capability()
            return

        # Try common Linux CUDA installation paths
        cuda_paths = [
            Path("/usr/local/cuda/bin/nvcc"),
            Path("/usr/local/cuda-12/bin/nvcc"),
            Path("/usr/local/cuda-11/bin/nvcc"),
        ]

        # Also check versioned paths like /usr/local/cuda-12.*
        for cuda_dir in Path("/usr/local").glob("cuda-*/bin/nvcc"):
            cuda_paths.append(cuda_dir)

        if self.platform_info.is_windows:
            windows_cuda_root = Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA")
            if windows_cuda_root.exists():
                for nvcc in windows_cuda_root.glob("v*/bin/nvcc.exe"):
                    cuda_paths.append(nvcc)

        for nvcc in cuda_paths:
            if nvcc.exists():
                self.platform_info.cuda_available = True
                self.platform_info.cuda_path = str(nvcc)
                self._detect_cuda_compute_capability()
                return

    def _detect_cuda_compute_capability(self) -> None:
        """Detect CUDA compute capability using nvidia-smi.

        Queries all GPUs and returns the minimum compute capability
        to ensure compatibility with all installed GPUs.
        """
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                capabilities = []
                for line in result.stdout.strip().split("\n"):
                    cap = line.strip()
                    if cap and "." in cap:
                        capabilities.append(cap.replace(".", ""))

                if capabilities:
                    self.platform_info.cuda_compute_capability = min(capabilities)
        except Exception:
            pass

    def _detect_libvmaf_cuda_support(self) -> None:
        """Detect whether libvmaf can be built with CUDA in this backend."""
        self.platform_info.libvmaf_cuda_supported = False
        self.platform_info.libvmaf_cuda_reason = "CUDA toolkit is not detected"

        if not self.platform_info.cuda_available:
            return

        backend = self.platform_info.build_backend
        if backend not in ("linux-native", "linux-wsl2"):
            self.platform_info.libvmaf_cuda_reason = (
                f"Backend '{backend}' is not supported for libvmaf CUDA in current policy"
            )
            return

        nvcc_path = self.platform_info.cuda_path or shutil.which("nvcc")
        if not nvcc_path:
            self.platform_info.libvmaf_cuda_reason = "nvcc is not available in PATH"
            return

        if not self._probe_nvcc_compile(nvcc_path):
            self.platform_info.libvmaf_cuda_reason = (
                "nvcc compile sanity-check failed (host compiler/toolchain mismatch)"
            )
            return

        self.platform_info.libvmaf_cuda_supported = True
        self.platform_info.libvmaf_cuda_reason = "Supported"

    def _probe_nvcc_compile(self, nvcc_path: str) -> bool:
        """Check whether nvcc can compile a minimal CUDA source file."""
        try:
            with tempfile.TemporaryDirectory(prefix="nvcc-probe-") as temp_dir:
                temp_path = Path(temp_dir)
                source_file = temp_path / "probe.cu"
                output_file = temp_path / "probe.o"
                source_file.write_text(
                    "__global__ void kernel() {}\nint main() { kernel<<<1,1>>>(); return 0; }\n",
                    encoding="utf-8",
                )

                result = subprocess.run(
                    [nvcc_path, "-c", str(source_file), "-o", str(output_file)],
                    capture_output=True,
                    timeout=20,
                )
                return result.returncode == 0
        except Exception:
            return False

    def _check_wsl2(self) -> bool:
        """Check if running in WSL2 environment.

        Returns:
            True if running in WSL2.
        """
        try:
            with open("/proc/version", "r") as f:
                version_info = f.read().lower()
                return "microsoft" in version_info or "wsl" in version_info
        except Exception:
            return False

    def _check_qsv(self) -> bool:
        """Check if Intel QSV is available.

        Returns:
            True if Intel QSV is available.
        """
        if self.platform_info.is_windows:
            has_intel_gpu = any("intel" in gpu.lower() for gpu in self.system_info.gpu_info)
            if not has_intel_gpu:
                return False

            if self.platform_info.is_msys2 and self.platform_info.is_ucrt64:
                for pkg_name in ("vpl", "libvpl"):
                    try:
                        result = subprocess.run(
                            ["pkg-config", "--exists", pkg_name], capture_output=True, timeout=5
                        )
                        if result.returncode == 0:
                            return True
                    except Exception:
                        pass
                return False

            try:
                result = subprocess.run(
                    ["pkg-config", "--exists", "libvpl"], capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass

            return has_intel_gpu

        # QSV is not supported in WSL2
        if self.platform_info.is_wsl2:
            return False

        # QSV requires VAAPI
        if not self.platform_info.vaapi_available:
            return False

        # Check for Intel GPU via vainfo
        if shutil.which("vainfo"):
            try:
                result = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=5)
                # Check for Intel driver (iHD or i965)
                if "Intel" in result.stdout or "iHD" in result.stdout or "i965" in result.stdout:
                    return True
            except Exception:
                pass

        # Check for Intel GPU via PCI IDs (only display/3D class devices)
        display_class_prefixes = ("03", "0300", "0301", "0302", "0320", "0380")
        try:
            pci_devices = Path("/sys/bus/pci/devices")
            if pci_devices.exists():
                for device in pci_devices.iterdir():
                    vendor_file = device / "vendor"
                    class_file = device / "class"
                    if vendor_file.exists() and class_file.exists():
                        vendor_id = vendor_file.read_text().strip()
                        class_code = class_file.read_text().strip()
                        # Intel vendor ID is 0x8086, class must be display/3D
                        if vendor_id == "0x8086" and class_code.startswith(display_class_prefixes):
                            return True
        except Exception:
            pass

        return False

    def _find_macports_clang(self) -> Optional[ToolInfo]:
        """Find macports clang.

        Returns:
            ToolInfo for macports clang or None.
        """
        macports_bin = Path("/opt/local/bin")

        # Look for clang-mp-*
        for clang_path in macports_bin.glob("clang-mp-*"):
            version = clang_path.name.replace("clang-mp-", "")
            return ToolInfo(
                name=f"macports-clang-{version}",
                path=str(clang_path),
                version=version,
                available=True,
            )

        return None

    def _check_vaapi(self) -> bool:
        """Check if VAAPI is available.

        Returns:
            True if VAAPI is available.
        """
        if not self.platform_info.is_linux:
            return False

        # VAAPI is not supported in WSL2
        if self.platform_info.is_wsl2:
            return False

        try:
            result = subprocess.run(
                ["pkg-config", "--exists", "libva"], capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def _check_amf(self) -> bool:
        """Check if AMF headers are available.

        Returns:
            True if AMF is available.
        """
        # Check common locations for AMF headers
        amf_paths = [
            Path("/usr/include/AMF"),
            Path("/usr/local/include/AMF"),
            Path("/opt/AMF/amf/public/include"),
        ]

        return any(path.exists() for path in amf_paths)

    def _check_vulkan(self) -> bool:
        """Check if Vulkan SDK/headers are available.

        Returns:
            True if Vulkan is available.
        """
        # Check pkg-config
        try:
            result = subprocess.run(
                ["pkg-config", "--exists", "vulkan"], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

        # Check header files
        vulkan_header_paths = [
            Path("/usr/include/vulkan/vulkan.h"),
            Path("/usr/local/include/vulkan/vulkan.h"),
        ]
        if self.platform_info.is_windows:
            vulkan_header_paths.extend(
                [
                    Path("/ucrt64/include/vulkan/vulkan.h"),
                    Path("C:/msys64/ucrt64/include/vulkan/vulkan.h"),
                    Path("C:/VulkanSDK/Include/vulkan/vulkan.h"),
                ]
            )
            vulkan_sdk = os.environ.get("VULKAN_SDK")
            if vulkan_sdk:
                vulkan_header_paths.append(Path(vulkan_sdk) / "Include" / "vulkan" / "vulkan.h")

        if any(path.exists() for path in vulkan_header_paths):
            return True

        # Check vulkaninfo command
        if shutil.which("vulkaninfo") is not None:
            return True

        return False

    def _check_opencl(self) -> bool:
        """Check if OpenCL development files and runtime are available.

        Returns:
            True if OpenCL headers, ICD loader, and at least one vendor ICD are available.
        """
        # Check pkg-config first
        for pkg_name in ("OpenCL", "opencl"):
            try:
                result = subprocess.run(
                    ["pkg-config", "--exists", pkg_name], capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass

        # Check for OpenCL headers
        opencl_header_paths = [
            Path("/usr/include/CL/cl.h"),
            Path("/usr/local/include/CL/cl.h"),
        ]
        if self.platform_info.is_windows:
            opencl_header_paths.extend(
                [
                    Path("/ucrt64/include/CL/cl.h"),
                    Path("C:/msys64/ucrt64/include/CL/cl.h"),
                ]
            )

        has_headers = any(path.exists() for path in opencl_header_paths)
        if not has_headers:
            return False

        # Check for ICD loader library
        icd_loader_paths = [
            Path("/usr/lib/libOpenCL.so"),
            Path("/usr/lib64/libOpenCL.so"),
        ]
        if self.platform_info.is_windows:
            icd_loader_paths.extend(
                [
                    Path("/c/Windows/System32/OpenCL.dll"),
                    Path("C:/Windows/System32/OpenCL.dll"),
                    Path("/ucrt64/bin/libOpenCL.dll.a"),
                ]
            )

        # Add architecture-specific paths
        multiarch = self.get_multiarch_dir()
        if multiarch:
            icd_loader_paths.extend(
                [
                    Path(f"/usr/lib/{multiarch}/libOpenCL.so"),
                    Path(f"/usr/lib/{multiarch}/libOpenCL.so.1"),
                ]
            )

        has_loader = any(path.exists() for path in icd_loader_paths)
        if not has_loader:
            return False

        # Check for at least one vendor ICD file
        icd_vendors_dir = Path("/etc/OpenCL/vendors")
        if icd_vendors_dir.exists() and any(icd_vendors_dir.glob("*.icd")):
            return True

        # Check WSL NVIDIA OpenCL (not available in WSL)
        wsl_lib = Path("/usr/lib/wsl/lib")
        if wsl_lib.exists():
            if any(wsl_lib.glob("libnvidia-opencl*")):
                return True
            # WSL without OpenCL implementation
            return False

        return False

    def _detect_tools(self) -> None:
        """Detect available tools."""
        tool_names = [
            "make",
            "g++",
            "clang++",
            "gcc",
            "clang",
            "pkg-config",
            "nasm",
            "yasm",
            "cmake",
            "python3",
            "meson",
            "ninja",
            "cargo",
            "rustc",
            "curl",
            "git",
        ]

        for tool_name in tool_names:
            self.tools[tool_name] = self._detect_tool(tool_name)

    def _detect_tool(self, tool_name: str) -> ToolInfo:
        """Detect a single tool.

        Args:
            tool_name: Name of the tool.

        Returns:
            ToolInfo instance.
        """
        tool_path = shutil.which(tool_name)

        if tool_path is None:
            return ToolInfo(name=tool_name, available=False)

        # Try to get version
        version = self._get_tool_version(tool_name, tool_path)

        return ToolInfo(name=tool_name, path=tool_path, version=version, available=True)

    def _get_tool_version(self, tool_name: str, tool_path: str) -> Optional[str]:
        """Get tool version.

        Args:
            tool_name: Name of the tool.
            tool_path: Path to the tool.

        Returns:
            Version string or None.
        """
        try:
            if tool_name in ("g++", "gcc", "clang++", "clang"):
                result = subprocess.run(
                    [tool_path, "--version"], capture_output=True, text=True, timeout=5
                )
                output = result.stdout.split("\n")[0]
                # Extract version number
                for part in output.split():
                    if part[0].isdigit():
                        return part.rstrip(")")

            elif tool_name in ("cmake", "nasm", "yasm", "python3", "meson", "ninja"):
                result = subprocess.run(
                    [tool_path, "--version"], capture_output=True, text=True, timeout=5
                )
                output = result.stdout.strip()
                # First line often contains version
                for part in output.split():
                    if part[0].isdigit():
                        return part

            elif tool_name in ("cargo", "rustc"):
                result = subprocess.run(
                    [tool_path, "--version"], capture_output=True, text=True, timeout=5
                )
                output = result.stdout.strip()
                # Format: "cargo 1.70.0" or "rustc 1.70.0"
                parts = output.split()
                if len(parts) >= 2:
                    return parts[1]

            elif tool_name == "pkg-config":
                result = subprocess.run(
                    [tool_path, "--version"], capture_output=True, text=True, timeout=5
                )
                return result.stdout.strip()

        except Exception:
            pass

        return None

    def get_num_jobs(self, config_num_jobs: str = "auto") -> int:
        """Get number of parallel jobs.

        Args:
            config_num_jobs: Configuration value ("auto" or number).

        Returns:
            Number of jobs.
        """
        if config_num_jobs != "auto":
            return int(config_num_jobs)

        return self.system_info.cpu_cores or 4
