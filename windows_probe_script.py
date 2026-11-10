"""PowerShell probe script executed on the physical Windows host via WSL interop.

The script is Windows-side only: it reads Windows CIM/registry state and calls
the Windows Vulkan/OpenCL loaders directly. It never inspects the WSL Linux
view. It prints a single compact JSON document (see ``windows_probe``).
"""

PROBE_SCRIPT_VERSION = 1

PROBE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'

function Get-FileVer([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        return (Get-Item -LiteralPath $Path).VersionInfo.FileVersion
    }
    return $null
}

$sys32 = Join-Path $env:SystemRoot 'System32'
$result = [ordered]@{}

# --- Windows OS -----------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$archMap = @{ 9 = 'x86_64'; 12 = 'arm64'; 0 = 'x86'; 5 = 'arm' }
$cpuObj = @(Get-CimInstance Win32_Processor)[0]
$result.os = [ordered]@{
    platform       = 'windows'
    architecture   = $archMap[[int]$cpuObj.Architecture]
    product_name   = [string]$os.Caption
    edition        = [string]$cv.EditionID
    version        = [string]$os.Version
    build_number   = [int]$os.BuildNumber
    ubr            = if ($null -ne $cv.UBR) { [int]$cv.UBR } else { $null }
    display_version = if ($cv.DisplayVersion) { [string]$cv.DisplayVersion } else { $null }
}

# --- CPU ------------------------------------------------------------------
$family = $null; $model = $null; $stepping = $null
if ($cpuObj.Description -match 'Family\s+(\d+)\s+Model\s+(\d+)\s+Stepping\s+(\d+)') {
    $family = [int]$Matches[1]; $model = [int]$Matches[2]; $stepping = [int]$Matches[3]
}
Add-Type -Namespace Probe -Name Kernel32 -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern bool IsProcessorFeaturePresent(uint feature);
'@
$pf = [ordered]@{
    sse3 = 13; ssse3 = 36; sse4_1 = 37; sse4_2 = 38; xsave = 17
    avx = 39; avx2 = 40; avx512f = 41
}
$isa = [ordered]@{}
foreach ($name in $pf.Keys) {
    $isa[$name] = [bool][Probe.Kernel32]::IsProcessorFeaturePresent([uint32]$pf[$name])
}
$result.cpu = [ordered]@{
    vendor             = [string]$cpuObj.Manufacturer
    name               = ([string]$cpuObj.Name).Trim()
    family             = $family
    model              = $model
    stepping           = $stepping
    architecture       = $archMap[[int]$cpuObj.Architecture]
    cores              = [int]$cpuObj.NumberOfCores
    logical_processors = [int]$cpuObj.NumberOfLogicalProcessors
    isa_features       = $isa
}

# --- GPU inventory --------------------------------------------------------
$gpus = @()
foreach ($vc in @(Get-CimInstance Win32_VideoController)) {
    $pnp = [string]$vc.PNPDeviceID
    $ven = $null; $dev = $null; $sub = $null; $rev = $null
    if ($pnp -match 'VEN_([0-9A-Fa-f]{4})') { $ven = $Matches[1] }
    if ($pnp -match 'DEV_([0-9A-Fa-f]{4})') { $dev = $Matches[1] }
    if ($pnp -match 'SUBSYS_([0-9A-Fa-f]{8})') { $sub = $Matches[1] }
    if ($pnp -match 'REV_([0-9A-Fa-f]{2})') { $rev = $Matches[1] }
    $loc = $null
    try {
        $loc = [string](Get-PnpDeviceProperty -InstanceId $pnp -KeyName 'DEVPKEY_Device_LocationInfo').Data
    } catch { $loc = $null }
    $isSoftware = ($ven -eq '1414') -or ($pnp -notmatch '^PCI\\')
    $gpus += [ordered]@{
        name           = [string]$vc.Name
        vendor_id      = $ven
        device_id      = $dev
        subsystem_id   = $sub
        revision       = $rev
        pnp_device_id  = $pnp
        location       = $loc
        driver_version = [string]$vc.DriverVersion
        adapter_type   = if ($isSoftware) { 'software' } else { 'hardware' }
    }
}
$result.gpus = $gpus

# --- NVIDIA ---------------------------------------------------------------
$nvidia = [ordered]@{ state = 'absent'; devices = @(); api = [ordered]@{} }
$hasNvidia = @($gpus | Where-Object { $_.vendor_id -eq '10DE' -and $_.adapter_type -eq 'hardware' }).Count -gt 0
if ($hasNvidia) {
    $nvidia.state = 'unavailable'
    try {
        $smi = Join-Path $sys32 'nvidia-smi.exe'
        if (-not (Test-Path -LiteralPath $smi)) { $smi = 'nvidia-smi.exe' }
        $lines = & $smi '--query-gpu=name,uuid,pci.bus_id,pci.device_id,pci.sub_device_id,driver_version,compute_cap' '--format=csv,noheader,nounits' 2>&1
        if ($LASTEXITCODE -ne 0) { throw ($lines -join ' ') }
        $devs = @()
        foreach ($line in @($lines)) {
            $f = ([string]$line).Split(',') | ForEach-Object { $_.Trim() }
            if ($f.Count -lt 7) { throw "unexpected nvidia-smi output: $line" }
            $devs += [ordered]@{
                name               = $f[0]
                uuid               = $f[1]
                pci_bus_id         = $f[2]
                pci_device_id      = $f[3]
                pci_sub_device_id  = $f[4]
                driver_version     = $f[5]
                compute_capability = $f[6]
            }
        }
        $nvidia.devices = $devs
        $nvidia.state = 'present'
    } catch {
        $nvidia.error = [string]$_.Exception.Message
    }
    foreach ($dll in 'nvcuda.dll', 'nvEncodeAPI64.dll', 'nvcuvid.dll') {
        $nvidia.api[$dll] = Get-FileVer (Join-Path $sys32 $dll)
    }
}
$result.nvidia = $nvidia

# --- Vulkan ---------------------------------------------------------------
$vk = [ordered]@{ state = 'absent'; loader = $null; devices = @(); icd_manifests = @() }
$vkDll = Join-Path $sys32 'vulkan-1.dll'
if (Test-Path -LiteralPath $vkDll) {
    $vk.state = 'unavailable'
    $vk.loader = [ordered]@{ version = Get-FileVer $vkDll }
    $manifests = @()
    foreach ($key in 'HKLM:\SOFTWARE\Khronos\Vulkan\Drivers', 'HKLM:\SOFTWARE\WOW6432Node\Khronos\Vulkan\Drivers') {
        if (Test-Path $key) {
            $manifests += @((Get-Item $key).Property)
        }
    }
    $vk.icd_manifests = @($manifests | Sort-Object -Unique)
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
public static class ProbeVulkan {
    [DllImport("vulkan-1.dll")] static extern int vkCreateInstance(IntPtr info, IntPtr alloc, out IntPtr instance);
    [DllImport("vulkan-1.dll")] static extern void vkDestroyInstance(IntPtr instance, IntPtr alloc);
    [DllImport("vulkan-1.dll")] static extern int vkEnumeratePhysicalDevices(IntPtr instance, ref uint count, IntPtr devices);
    [DllImport("vulkan-1.dll")] static extern void vkGetPhysicalDeviceProperties(IntPtr device, IntPtr props);
    public static object[] Enumerate() {
        IntPtr info = Marshal.AllocHGlobal(64);
        for (int i = 0; i < 64; i++) Marshal.WriteByte(info, i, 0);
        Marshal.WriteInt32(info, 0, 1);
        IntPtr inst;
        int rc = vkCreateInstance(info, IntPtr.Zero, out inst);
        Marshal.FreeHGlobal(info);
        if (rc != 0) throw new Exception("vkCreateInstance failed: " + rc);
        try {
            uint count = 0;
            rc = vkEnumeratePhysicalDevices(inst, ref count, IntPtr.Zero);
            if (rc != 0) throw new Exception("vkEnumeratePhysicalDevices failed: " + rc);
            IntPtr devs = Marshal.AllocHGlobal((int)(count * IntPtr.Size + 8));
            rc = vkEnumeratePhysicalDevices(inst, ref count, devs);
            if (rc != 0 && rc != 5) throw new Exception("vkEnumeratePhysicalDevices failed: " + rc);
            var list = new List<object>();
            IntPtr props = Marshal.AllocHGlobal(2048);
            for (int i = 0; i < count; i++) {
                IntPtr dev = Marshal.ReadIntPtr(devs, i * IntPtr.Size);
                for (int j = 0; j < 2048; j++) Marshal.WriteByte(props, j, 0);
                vkGetPhysicalDeviceProperties(dev, props);
                uint api = (uint)Marshal.ReadInt32(props, 0);
                uint drv = (uint)Marshal.ReadInt32(props, 4);
                uint ven = (uint)Marshal.ReadInt32(props, 8);
                uint did = (uint)Marshal.ReadInt32(props, 12);
                int typ = Marshal.ReadInt32(props, 16);
                byte[] nb = new byte[256];
                Marshal.Copy(IntPtr.Add(props, 20), nb, 0, 256);
                int n = Array.IndexOf(nb, (byte)0); if (n < 0) n = 256;
                string name = Encoding.UTF8.GetString(nb, 0, n);
                list.Add(new object[] { name, ven, did, typ, api, drv });
            }
            Marshal.FreeHGlobal(props);
            Marshal.FreeHGlobal(devs);
            return list.ToArray();
        } finally {
            vkDestroyInstance(inst, IntPtr.Zero);
        }
    }
}
'@
        $typeNames = @{ 0 = 'other'; 1 = 'integrated_gpu'; 2 = 'discrete_gpu'; 3 = 'virtual_gpu'; 4 = 'cpu' }
        $devs = @()
        foreach ($d in [ProbeVulkan]::Enumerate()) {
            $api = [uint32]$d[4]
            $devs += [ordered]@{
                name           = [string]$d[0]
                vendor_id      = ('{0:x4}' -f [uint32]$d[1])
                device_id      = ('{0:x4}' -f [uint32]$d[2])
                device_type    = $typeNames[[int]$d[3]]
                api_version    = ('{0}.{1}.{2}' -f ($api -shr 22), (($api -shr 12) -band 0x3ff), ($api -band 0xfff))
                driver_version = [string][uint32]$d[5]
            }
        }
        $vk.devices = $devs
        $vk.state = 'present'
    } catch {
        $vk.error = [string]$_.Exception.Message
    }
}
$result.vulkan = $vk

# --- OpenCL ---------------------------------------------------------------
$cl = [ordered]@{ state = 'absent'; dispatcher = $null; platforms = @(); icd_vendors = @() }
$clDll = Join-Path $sys32 'OpenCL.dll'
if (Test-Path -LiteralPath $clDll) {
    $cl.state = 'unavailable'
    $cl.dispatcher = [ordered]@{ version = Get-FileVer $clDll }
    $vendors = @()
    foreach ($key in 'HKLM:\SOFTWARE\Khronos\OpenCL\Vendors', 'HKLM:\SOFTWARE\WOW6432Node\Khronos\OpenCL\Vendors') {
        if (Test-Path $key) {
            $vendors += @((Get-Item $key).Property)
        }
    }
    $cl.icd_vendors = @($vendors | Sort-Object -Unique)
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
public static class ProbeOpenCL {
    [DllImport("OpenCL.dll")] static extern int clGetPlatformIDs(uint n, IntPtr[] ids, out uint count);
    [DllImport("OpenCL.dll")] static extern int clGetPlatformInfo(IntPtr p, uint name, UIntPtr size, byte[] value, out UIntPtr ret);
    [DllImport("OpenCL.dll")] static extern int clGetDeviceIDs(IntPtr p, ulong type, uint n, IntPtr[] ids, out uint count);
    [DllImport("OpenCL.dll")] static extern int clGetDeviceInfo(IntPtr d, uint name, UIntPtr size, byte[] value, out UIntPtr ret);
    static string Str(byte[] b, UIntPtr n) {
        int len = (int)n.ToUInt64(); if (len > 0 && b[len - 1] == 0) len--;
        return Encoding.UTF8.GetString(b, 0, len);
    }
    static string PInfo(IntPtr p, uint name) {
        byte[] b = new byte[1024]; UIntPtr n;
        if (clGetPlatformInfo(p, name, (UIntPtr)1024, b, out n) != 0) return null;
        return Str(b, n);
    }
    static string DInfo(IntPtr d, uint name) {
        byte[] b = new byte[1024]; UIntPtr n;
        if (clGetDeviceInfo(d, name, (UIntPtr)1024, b, out n) != 0) return null;
        return Str(b, n);
    }
    static ulong DNum(IntPtr d, uint name) {
        byte[] b = new byte[8]; UIntPtr n;
        if (clGetDeviceInfo(d, name, (UIntPtr)8, b, out n) != 0) return 0;
        return n.ToUInt64() == 4 ? BitConverter.ToUInt32(b, 0) : BitConverter.ToUInt64(b, 0);
    }
    public static object[] Enumerate() {
        uint pc = 0;
        int rc = clGetPlatformIDs(0, null, out pc);
        if (rc == -1001 || pc == 0) return new object[0];
        if (rc != 0) throw new Exception("clGetPlatformIDs failed: " + rc);
        IntPtr[] ps = new IntPtr[pc];
        rc = clGetPlatformIDs(pc, ps, out pc);
        if (rc != 0) throw new Exception("clGetPlatformIDs failed: " + rc);
        var plats = new List<object>();
        foreach (IntPtr p in ps) {
            var devs = new List<object>();
            uint dc = 0;
            int r2 = clGetDeviceIDs(p, 0xFFFFFFFFUL, 0, null, out dc);
            if (r2 == 0 && dc > 0) {
                IntPtr[] ds = new IntPtr[dc];
                clGetDeviceIDs(p, 0xFFFFFFFFUL, dc, ds, out dc);
                foreach (IntPtr d in ds) {
                    devs.Add(new object[] { DInfo(d, 0x102B), DInfo(d, 0x102C), DNum(d, 0x1000), DNum(d, 0x1001), DInfo(d, 0x102F), DInfo(d, 0x102D) });
                }
            }
            plats.Add(new object[] { PInfo(p, 0x0902), PInfo(p, 0x0903), PInfo(p, 0x0901), devs.ToArray() });
        }
        return plats.ToArray();
    }
}
'@
        $typeMap = @{ 1 = 'default'; 2 = 'cpu'; 4 = 'gpu'; 8 = 'accelerator'; 16 = 'custom' }
        $plats = @()
        foreach ($p in [ProbeOpenCL]::Enumerate()) {
            $pdevs = @()
            foreach ($d in $p[3]) {
                $t = [uint64]$d[2]
                $tn = @($typeMap.Keys | Where-Object { ($t -band $_) -ne 0 } | Sort-Object | ForEach-Object { $typeMap[$_] })
                $pdevs += [ordered]@{
                    name           = [string]$d[0]
                    vendor         = [string]$d[1]
                    device_type    = ($tn -join '+')
                    vendor_id      = ('{0:x4}' -f [uint32]$d[3])
                    version        = [string]$d[4]
                    driver_version = [string]$d[5]
                }
            }
            $plats += [ordered]@{
                name    = [string]$p[0]
                vendor  = [string]$p[1]
                version = [string]$p[2]
                devices = $pdevs
            }
        }
        $cl.platforms = $plats
        $cl.state = if ($plats.Count -gt 0) { 'present' } else { 'absent' }
    } catch {
        $cl.error = [string]$_.Exception.Message
    }
}
$result.opencl = $cl

$result.diagnostics = [ordered]@{
    probe_script_version = 1
}

ConvertTo-Json -InputObject $result -Depth 8 -Compress
"""
