Param(
    [string]$Msys2Root = "C:\msys64",
    [string]$ProjectRoot = "",
    [string]$VenvName = ".venv-msys2-ucrt64",
    [switch]$SkipPackageInstall,
    [switch]$SkipPythonDeps,
    [switch]$SkipSdkChecks,
    [switch]$LaunchBuilder
)

$ErrorActionPreference = "Stop"

function Write-Step {
    Param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function To-MsysPath {
    Param([string]$WindowsPath)
    if ([string]::IsNullOrWhiteSpace($WindowsPath)) {
        return ""
    }
    $normalized = $WindowsPath -replace "\\", "/"
    if ($normalized -match "^([A-Za-z]):/(.*)$") {
        return "/$($matches[1].ToLower())/$($matches[2])"
    }
    return $normalized
}

function Invoke-MsysBash {
    Param([string]$Command)
    $bash = Join-Path $Msys2Root "usr\bin\bash.exe"
    if (-not (Test-Path $bash)) {
        throw "MSYS2 bash not found: $bash"
    }

    $wrapped = "source /etc/profile; $Command"
    $previousMsystem = $env:MSYSTEM
    $previousChere = $env:CHERE_INVOKING
    $env:MSYSTEM = "UCRT64"
    $env:CHERE_INVOKING = "1"

    & $bash -lc $wrapped

    if ($null -eq $previousMsystem) {
        Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue
    } else {
        $env:MSYSTEM = $previousMsystem
    }

    if ($null -eq $previousChere) {
        Remove-Item Env:CHERE_INVOKING -ErrorAction SilentlyContinue
    } else {
        $env:CHERE_INVOKING = $previousChere
    }

    if ($LASTEXITCODE -ne 0) {
        throw "MSYS2 command failed with exit code $LASTEXITCODE"
    }
}

function Invoke-MsysBashWithRetry {
    Param(
        [string]$Command,
        [int]$Attempts = 3,
        [int]$DelaySeconds = 5
    )

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Invoke-MsysBash $Command
            return
        } catch {
            if ($i -ge $Attempts) {
                throw
            }
            Write-Host "Attempt $i/$Attempts failed, retrying in $DelaySeconds seconds..." -ForegroundColor Yellow
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not (Test-Path $ProjectRoot)) {
    throw "Project root not found: $ProjectRoot"
}

if ($env:OS -ne "Windows_NT") {
    throw "This script is intended for Windows hosts."
}

Write-Step "Checking MSYS2 installation"
$bashExe = Join-Path $Msys2Root "usr\bin\bash.exe"
if (-not (Test-Path $bashExe)) {
    throw "MSYS2 is not installed at $Msys2Root. Install MSYS2 first: https://www.msys2.org/"
}

Write-Step "Validating active MSYS2 toolchain"
Invoke-MsysBash "which gcc; gcc -dumpmachine; which python; python -V"

if (-not $SkipPackageInstall) {
    Write-Step "Updating MSYS2 system and installing required UCRT64 packages"

    # Step 1: Full system upgrade first.  MSYS2 documentation requires running
    # pacman -Syu (not just -Sy) before installing packages so that dependency
    # resolution uses the latest package metadata and the runtime libs are
    # consistent.  The upgrade may exit early asking for a shell restart; the
    # retry loop in Invoke-MsysBashWithRetry handles transient failures.
    Write-Host "  Step 1/2: pacman -Syu (full system upgrade)..." -ForegroundColor Gray
    Invoke-MsysBashWithRetry "pacman -Syu --noconfirm" -Attempts 2 -DelaySeconds 3

    $packages = @(
        "base-devel"
        "git"
        # In current MSYS2 repos Git LFS is provided as a UCRT64 mingw package.
        "mingw-w64-ucrt-x86_64-git-lfs"
        "curl"
        "tar"
        "unzip"
        "patch"
        "diffutils"
        "autoconf"
        "automake"
        "libtool"
        "perl"
        "make"
        "mingw-w64-ucrt-x86_64-toolchain"
        "mingw-w64-ucrt-x86_64-python"
        "mingw-w64-ucrt-x86_64-python-pip"
        "mingw-w64-ucrt-x86_64-python-setuptools"
        "mingw-w64-ucrt-x86_64-python-wheel"
        "mingw-w64-ucrt-x86_64-python-rich"
        "mingw-w64-ucrt-x86_64-python-tqdm"
        "mingw-w64-ucrt-x86_64-python-yaml"
        "mingw-w64-ucrt-x86_64-python-requests"
        "mingw-w64-ucrt-x86_64-cmake"
        "mingw-w64-ucrt-x86_64-meson"
        "mingw-w64-ucrt-x86_64-ninja"
        "mingw-w64-ucrt-x86_64-nasm"
        "mingw-w64-ucrt-x86_64-yasm"
        "mingw-w64-ucrt-x86_64-pkgconf"
        "mingw-w64-ucrt-x86_64-rust"
        "mingw-w64-ucrt-x86_64-ffnvcodec-headers"
        "mingw-w64-ucrt-x86_64-libvpl"
        "mingw-w64-ucrt-x86_64-vulkan-headers"
        "mingw-w64-ucrt-x86_64-vulkan-loader"
        "mingw-w64-ucrt-x86_64-vulkan-validation-layers"
        "mingw-w64-ucrt-x86_64-python-jinja"
        "mingw-w64-ucrt-x86_64-shaderc"
        "mingw-w64-ucrt-x86_64-glslang"
        "mingw-w64-ucrt-x86_64-opencl-headers"
        "mingw-w64-ucrt-x86_64-opencl-icd"
        "mingw-w64-ucrt-x86_64-giflib"
        "mingw-w64-ucrt-x86_64-llvm-openmp"
    )

    $pkgLine = $packages -join " "
    Write-Host "  Step 2/2: installing required packages..." -ForegroundColor Gray
    Invoke-MsysBashWithRetry "pacman -S --needed --noconfirm $pkgLine"

    # Verify that the critical build tools are actually present after install.
    Write-Step "Verifying required build tools"
    $requiredTools = @("gcc", "cmake", "ninja", "meson", "nasm", "python", "pkg-config")
    $missing = @()
    foreach ($tool in $requiredTools) {
        $result = & $bashExe -lc "source /etc/profile >/dev/null 2>&1; which $tool >/dev/null 2>&1 && echo ok || echo missing" 2>&1
        if ($result -match "missing") {
            $missing += $tool
            Write-Host "  MISSING: $tool" -ForegroundColor Red
        } else {
            $ver = & $bashExe -lc "source /etc/profile >/dev/null 2>&1; $tool --version 2>&1 | head -1" 2>&1
            Write-Host "  OK: $tool  ($($ver -replace '\r?\n.*',''))" -ForegroundColor Green
        }
    }
    if ($missing.Count -gt 0) {
        throw "Required tools not found after package install: $($missing -join ', '). Run 'pacman -Syu' in MSYS2 UCRT64 and re-run this script."
    }
}

$projectMsys = To-MsysPath $ProjectRoot
$venvMsys = "$projectMsys/$VenvName"

if (Test-Path (Join-Path $ProjectRoot ".git")) {
    Write-Step "Syncing Git LFS source archives"
    Invoke-MsysBash "cd '$projectMsys'; git lfs install --local; git lfs pull"
}

Write-Step "Creating Python virtual environment in MSYS2 UCRT64 if missing"
Invoke-MsysBash "cd '$projectMsys'; if [ ! -d '$VenvMsys' ]; then /ucrt64/bin/python -m venv --system-site-packages '$VenvMsys'; fi"

$venvCfgWin = Join-Path (Join-Path $ProjectRoot $VenvName) "pyvenv.cfg"
if (Test-Path $venvCfgWin) {
    $venvCfg = Get-Content -Path $venvCfgWin -Raw
    if ($venvCfg -match "(?m)^include-system-site-packages\s*=") {
        $venvCfg = [regex]::Replace(
            $venvCfg,
            "(?m)^include-system-site-packages\s*=.*$",
            "include-system-site-packages = true"
        )
    } else {
        $venvCfg = $venvCfg.TrimEnd() + "`r`ninclude-system-site-packages = true`r`n"
    }
    Set-Content -Path $venvCfgWin -Value $venvCfg -Encoding utf8
}

if (-not $SkipPythonDeps) {
    Write-Step "Installing Python dependencies in MSYS2 UCRT64 venv"
    Invoke-MsysBash "cd '$projectMsys'; source '$venvMsys/bin/activate'; python -m pip install --upgrade pip setuptools wheel; python -m pip install -e . --no-deps"

    Write-Step "Running Python environment check"
    Invoke-MsysBash "cd '$projectMsys'; source '$venvMsys/bin/activate'; ./scripts/check_python_env.sh"
}

if (-not $SkipSdkChecks) {
    Write-Step "Detecting CUDA / Vulkan / OpenCL / QSV availability"

    $cudaPath = $env:CUDA_PATH
    if (-not $cudaPath) {
        $cudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
        if (Test-Path $cudaRoot) {
            $latestCuda = Get-ChildItem $cudaRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
            if ($latestCuda) {
                $cudaPath = $latestCuda.FullName
            }
        }
    }

    $vulkanSdk = $env:VULKAN_SDK
    if (-not $vulkanSdk) {
        $vkRoot = "C:\VulkanSDK"
        if (Test-Path $vkRoot) {
            $latestVk = Get-ChildItem $vkRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
            if ($latestVk) {
                $vulkanSdk = $latestVk.FullName
            }
        }
    }

    $openclDll = "C:\Windows\System32\OpenCL.dll"
    $hasOpenClRuntime = Test-Path $openclDll

    $sdkEnvSh = Join-Path $ProjectRoot "scripts\env_windows_msys2_ucrt64.sh"
    $lines = @(
        "#!/usr/bin/env bash"
        "# Generated by setup_windows_msys2_ucrt64.ps1"
        "# Source this file before running ffmpeg_builder in MSYS2 UCRT64."
        ""
    )

    if ($cudaPath) {
        $cudaMsys = To-MsysPath $cudaPath
        $lines += "export CUDA_PATH='$cudaMsys'"
        $lines += "export PATH=`"$cudaMsys/bin:`$PATH`""
        Write-Host "CUDA detected: $cudaPath" -ForegroundColor Green
    } else {
        Write-Host "CUDA toolkit not detected (optional)." -ForegroundColor Yellow
    }

    if ($vulkanSdk) {
        $vkMsys = To-MsysPath $vulkanSdk
        $lines += "export VULKAN_SDK='$vkMsys'"
        $lines += "export PATH=`"$vkMsys/Bin:`$PATH`""
        $lines += "export VK_LAYER_PATH='$vkMsys/Bin'"
        Write-Host "Vulkan SDK detected: $vulkanSdk" -ForegroundColor Green
    } else {
        Write-Host "Vulkan SDK not detected (optional)." -ForegroundColor Yellow
    }

    if ($hasOpenClRuntime) {
        Write-Host "OpenCL runtime detected: $openclDll" -ForegroundColor Green
    } else {
        Write-Host "OpenCL runtime not detected in System32 (optional)." -ForegroundColor Yellow
    }

    $intelGpuDetected = $false
    try {
        $intelGpuDetected = @(
            Get-CimInstance Win32_VideoController |
            Where-Object { $_.Name -match "Intel" }
        ).Count -gt 0
    } catch {
        $intelGpuDetected = $false
    }

    $previousMsystem = $env:MSYSTEM
    $previousChere = $env:CHERE_INVOKING
    $env:MSYSTEM = "UCRT64"
    $env:CHERE_INVOKING = "1"
    $vplProbe = & $bashExe -lc "source /etc/profile >/dev/null 2>&1; if pkg-config --exists vpl || pkg-config --exists libvpl; then echo yes; else echo no; fi"
    $hasVplPkgConfig = ($LASTEXITCODE -eq 0) -and ($vplProbe -match "yes")
    if ($null -eq $previousMsystem) {
        Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue
    } else {
        $env:MSYSTEM = $previousMsystem
    }
    if ($null -eq $previousChere) {
        Remove-Item Env:CHERE_INVOKING -ErrorAction SilentlyContinue
    } else {
        $env:CHERE_INVOKING = $previousChere
    }

    if ($intelGpuDetected -and $hasVplPkgConfig) {
        Write-Host "QSV prerequisites detected: Intel GPU + oneVPL pkg-config module." -ForegroundColor Green
    } else {
        if (-not $intelGpuDetected) {
            Write-Host "QSV prerequisite missing: Intel GPU not detected by Win32_VideoController." -ForegroundColor Yellow
        }
        if (-not $hasVplPkgConfig) {
            Write-Host "QSV prerequisite missing: oneVPL pkg-config module (vpl/libvpl) not found in UCRT64." -ForegroundColor Yellow
        }
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($sdkEnvSh, ($lines -join "`n"), $utf8NoBom)
    Write-Host "SDK environment file generated: $sdkEnvSh" -ForegroundColor Green
}

Write-Step "Bootstrap finished"
Write-Host "Use MSYS2 UCRT64 shell and run:" -ForegroundColor Gray
Write-Host "  cd $(To-MsysPath $ProjectRoot)" -ForegroundColor Gray
Write-Host "  source ./scripts/env_windows_msys2_ucrt64.sh  # optional if file exists" -ForegroundColor Gray
Write-Host "  source ./$VenvName/bin/activate" -ForegroundColor Gray
Write-Host "  python -m ffmpeg_builder" -ForegroundColor Gray

if ($LaunchBuilder) {
    Write-Step "Launching ffmpeg_builder in MSYS2 UCRT64"
    Invoke-MsysBash "cd '$projectMsys'; if [ -f ./scripts/env_windows_msys2_ucrt64.sh ]; then source ./scripts/env_windows_msys2_ucrt64.sh; fi; source '$venvMsys/bin/activate'; python -m ffmpeg_builder"
}
