# ============================================================
# Nova Step 8 - Microsoft Definition
# Clean Fixed Version - No Temp Files / No Output Files
#
# Includes:
# 1. UAC changes
# 3. Private/Public Firewall OFF after unlock
# 4. Local GPO - Turn off real-time protection = Enabled
# 5. Remote Desktop enable + NLA disable
# 6. IE ESC disable
# 7. Restart Explorer
# 8. High Performance power plan + disable monitor/sleep timeout
#
# Notes:
# - This script does NOT create gpresult reports.
# - This script does NOT create backup files.
# - This script does not create report/backup/output files.
# - Output is printed only to the PowerShell screen.
# ============================================================

$ErrorActionPreference = "Continue"

# --------------------------
# Global Step Runner
# --------------------------

$script:StepSkipped = $false
$script:StepWarning = $false
$script:SuccessCount = 0
$script:FailCount = 0
$script:SkipCount = 0
$script:WarnCount = 0

function Skip-Step {
    param([string]$Reason)

    $script:StepSkipped = $true
    Write-Host "SKIP: $Reason" -ForegroundColor Yellow
}

function Warn-Step {
    param([string]$Reason)

    $script:StepWarning = $true
    Write-Host "WARN: $Reason" -ForegroundColor DarkYellow
}

function Execute-Step {
    param (
        [string]$StepName,
        [scriptblock]$Action
    )

    $script:StepSkipped = $false
    $script:StepWarning = $false

    Write-Host ""
    Write-Host "===============================" -ForegroundColor Cyan
    Write-Host "STEP: $StepName" -ForegroundColor Yellow
    Write-Host "===============================" -ForegroundColor Cyan

    try {
        & $Action

        if ($script:StepSkipped) {
            $script:SkipCount++
            Write-Host "SKIP: $StepName" -ForegroundColor Yellow
        }
        elseif ($script:StepWarning) {
            $script:WarnCount++
            Write-Host "WARN: $StepName" -ForegroundColor DarkYellow
        }
        else {
            $script:SuccessCount++
            Write-Host "SUCCESS: $StepName" -ForegroundColor Green
        }
    }
    catch {
        $script:FailCount++
        Write-Host "FAIL: $StepName" -ForegroundColor Red
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# --------------------------
# Check Admin Permission
# --------------------------

Execute-Step "Check Admin Permission" {
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        throw "Please run PowerShell as Administrator"
    }

    Write-Host "Running as Administrator"
}

# --------------------------
# Disable UAC Consent Prompt for Admins
# --------------------------

Execute-Step "Disable UAC Consent Prompt for Admins" {
    $path = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"

    Set-ItemProperty `
        -Path $path `
        -Name "ConsentPromptBehaviorAdmin" `
        -Value 0 `
        -ErrorAction Stop

    $value = Get-ItemProperty `
        -Path $path `
        -Name "ConsentPromptBehaviorAdmin" `
        -ErrorAction Stop

    Write-Host "ConsentPromptBehaviorAdmin = $($value.ConsentPromptBehaviorAdmin)"
}

# --------------------------
# Disable Secure Desktop Prompt
# --------------------------

Execute-Step "Disable Secure Desktop Prompt" {
    $path = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"

    Set-ItemProperty `
        -Path $path `
        -Name "PromptOnSecureDesktop" `
        -Value 0 `
        -ErrorAction Stop

    $value = Get-ItemProperty `
        -Path $path `
        -Name "PromptOnSecureDesktop" `
        -ErrorAction Stop

    Write-Host "PromptOnSecureDesktop = $($value.PromptOnSecureDesktop)"
}

# ============================================================
# Firewall GUI Unlock Section - Private/Public Only
#
# Removed from old script:
#
# This section:
# - Removes local machine Registry.pol without backup
# - Removes WindowsFirewall policy keys
# - Removes Windows Security Firewall UI lock values
# - Sets Private/Public Firewall OFF after unlock
# - Does NOT touch Domain Firewall state
# ============================================================

# --------------------------
# Close UI / MMC processes
# --------------------------

Execute-Step "Close Windows Security / gpedit / Control Panel / Server Manager" {
    $processNames = @(
        "SecHealthUI",
        "SystemSettings",
        "control",
        "mmc",
        "ServerManager"
    )

    foreach ($processName in $processNames) {
        $processes = Get-Process -Name $processName -ErrorAction SilentlyContinue

        if ($null -eq $processes) {
            Write-Host "Process not running: $processName"
            continue
        }

        foreach ($process in $processes) {
            try {
                Stop-Process -Id $process.Id -Force -ErrorAction Stop
                Write-Host "Closed process: $processName PID=$($process.Id)"
            }
            catch {
                Warn-Step "Could not close process: $processName PID=$($process.Id)"
            }
        }
    }

    Start-Sleep -Seconds 3
}

# --------------------------
# Reset Local Machine Registry.pol
# --------------------------
# Important:
# This removes the local Computer Administrative Template policy file.
# It does not create backup files.
# --------------------------

Execute-Step "Reset Local Machine Registry.pol without creating backup file" {
    $gpMachinePath = "C:\Windows\System32\GroupPolicy\Machine"
    $regPolPath    = Join-Path $gpMachinePath "Registry.pol"

    if (-not (Test-Path $gpMachinePath)) {
        New-Item -Path $gpMachinePath -ItemType Directory -Force | Out-Null
        Write-Host "Created path: $gpMachinePath"
    }

    if (Test-Path $regPolPath) {
        Remove-Item -Path $regPolPath -Force -ErrorAction Stop
        Write-Host "Removed Local Machine Registry.pol"
    }
    else {
        Skip-Step "Registry.pol not found. Nothing to reset."
    }
}

# --------------------------
# Remove WindowsFirewall policy keys completely
# --------------------------

Execute-Step "Remove WindowsFirewall Policy Keys" {
    $mainFirewallPolicyPath = "HKLM:\SOFTWARE\Policies\Microsoft\WindowsFirewall"

    if (Test-Path $mainFirewallPolicyPath) {
        Remove-Item -Path $mainFirewallPolicyPath -Recurse -Force -ErrorAction Stop
        Write-Host "Removed policy key: $mainFirewallPolicyPath"
    }
    else {
        Skip-Step "WindowsFirewall policy key not found."
    }
}

# --------------------------
# Remove Windows Security Firewall UI policy locks
# --------------------------

Execute-Step "Remove Windows Security Firewall UI Policy Locks" {
    $paths = @(
        "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Firewall and network protection",
        "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender Security Center"
    )

    $values = @(
        "UILockdown",
        "DisableFirewallUI",
        "HideFirewall",
        "DisableNotifications"
    )

    foreach ($path in $paths) {
        if (Test-Path $path) {
            foreach ($value in $values) {
                Remove-ItemProperty -Path $path -Name $value -ErrorAction SilentlyContinue
                Write-Host "Removed if existed: $value from $path"
            }
        }
        else {
            Write-Host "Path not found: $path"
        }
    }
}

# --------------------------
# Clean live firewall values - Private/Public only
# --------------------------

Execute-Step "Clean Live Firewall Profile Values - Private and Public" {
    $liveProfiles = @(
        "HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\StandardProfile",
        "HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\PublicProfile"
    )

    foreach ($path in $liveProfiles) {
        if (-not (Test-Path $path)) {
            Skip-Step "Live profile path not found: $path"
            continue
        }

        Remove-ItemProperty -Path $path -Name "DisableNotifications" -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $path -Name "DoNotAllowExceptions" -ErrorAction SilentlyContinue

        Write-Host "Cleaned live profile values from: $path"
    }
}

# --------------------------
# Refresh Group Policy after Firewall unlock
# --------------------------

Execute-Step "Refresh Group Policy After Firewall Unlock" {
    gpupdate /force
}

# --------------------------
# Firewall - set Private/Public OFF after unlock
# --------------------------

Execute-Step "Firewall - Uncheck Notify checkbox - Private profile" {
    netsh advfirewall set privateprofile settings inboundusernotification disable | Out-Host
}

Execute-Step "Firewall - Uncheck Notify checkbox - Public profile" {
    netsh advfirewall set publicprofile settings inboundusernotification disable | Out-Host
}

Execute-Step "Firewall - Set NotifyOnListen False - Private and Public" {
    Set-NetFirewallProfile `
        -Profile Private,Public `
        -NotifyOnListen False `
        -ErrorAction Stop
}

Execute-Step "Firewall - Set Registry Notify OFF - Private and Public" {
    $paths = @(
        "HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\StandardProfile",
        "HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\PublicProfile"
    )

    foreach ($path in $paths) {
        if (Test-Path $path) {
            New-ItemProperty `
                -Path $path `
                -Name "DisableNotifications" `
                -Value 1 `
                -PropertyType DWord `
                -Force `
                -ErrorAction Stop | Out-Null

            Write-Host "Set DisableNotifications=1 in $path"
        }
        else {
            Skip-Step "Firewall registry path not found: $path"
        }
    }
}

Execute-Step "Firewall - Turn OFF Private profile" {
    netsh advfirewall set privateprofile state off | Out-Host
}

Execute-Step "Firewall - Turn OFF Public profile" {
    netsh advfirewall set publicprofile state off | Out-Host
}

Execute-Step "Firewall - Verify Private/Public Status" {
    Get-NetFirewallProfile -Profile Private,Public |
        Select-Object Name, Enabled, NotifyOnListen |
        Format-Table -AutoSize
}

Execute-Step "Firewall - Commit Firewall Policy Store Values + Verify" {

    Set-NetFirewallProfile `
        -All `
        -Enabled False `
        -PolicyStore $env:COMPUTERNAME `
        -DefaultInboundAction Allow `
        -DefaultOutboundAction Allow `
        -ErrorAction Stop

    Get-NetFirewallProfile `
        -All `
        -PolicyStore $env:COMPUTERNAME |
        Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction |
        Format-Table -AutoSize
}

# ============================================================
# Local Group Policy
# gpedit:
# Administrative Templates >
# Windows Components >
# Microsoft Defender Antivirus >
# Real-time Protection >
# Turn off real-time protection = Enabled
# ============================================================

# --------------------------
# Load Local GPO COM Helper
# --------------------------

Execute-Step "Local GPO - Load COM Helper if needed" {

    $existingType = ([System.Management.Automation.PSTypeName]'NovaLocalGPO.LocalGpoWriter').Type

    if ($null -ne $existingType) {
        Skip-Step "NovaLocalGPO.LocalGpoWriter already loaded in this PowerShell session."
        return
    }

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace NovaLocalGPO
{
    [ComImport, Guid("EA502723-A23D-11d1-A7D3-0000F87571E3"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IGroupPolicyObject
    {
        [PreserveSig] int New(
            [MarshalAs(UnmanagedType.LPWStr)] string pszDomainName,
            [MarshalAs(UnmanagedType.LPWStr)] string pszDisplayName,
            uint dwFlags);

        [PreserveSig] int OpenDSGPO(
            [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
            uint dwFlags);

        [PreserveSig] int OpenLocalMachineGPO(uint dwFlags);

        [PreserveSig] int OpenRemoteMachineGPO(
            [MarshalAs(UnmanagedType.LPWStr)] string pszComputerName,
            uint dwFlags);

        [PreserveSig] int Save(
            [MarshalAs(UnmanagedType.Bool)] bool bMachine,
            [MarshalAs(UnmanagedType.Bool)] bool bAdd,
            ref Guid pGuidExtension,
            ref Guid pGuidSnapin);

        [PreserveSig] int Delete();

        [PreserveSig] int GetName(IntPtr pszName, int cchMaxLength);
        [PreserveSig] int GetDisplayName(IntPtr pszName, int cchMaxLength);

        [PreserveSig] int SetDisplayName(
            [MarshalAs(UnmanagedType.LPWStr)] string pszName);

        [PreserveSig] int GetPath(IntPtr pszPath, int cchMaxPath);
        [PreserveSig] int GetDSPath(uint dwSection, IntPtr pszPath, int cchMaxPath);
        [PreserveSig] int GetFileSysPath(uint dwSection, IntPtr pszPath, int cchMaxPath);

        [PreserveSig] int GetRegistryKey(uint dwSection, out IntPtr hKey);
    }

    [ComImport, Guid("EA502722-A23D-11d1-A7D3-0000F87571E3")]
    class GroupPolicyObject
    {
    }

    public static class LocalGpoWriter
    {
        const uint GPO_OPEN_LOAD_REGISTRY = 1;
        const uint GPO_SECTION_MACHINE = 2;

        public static void SetMachineDwordPolicy(string subKey, string valueName, int value)
        {
            IGroupPolicyObject gpo = (IGroupPolicyObject)new GroupPolicyObject();

            int hr = gpo.OpenLocalMachineGPO(GPO_OPEN_LOAD_REGISTRY);
            if (hr != 0)
            {
                Marshal.ThrowExceptionForHR(hr);
            }

            IntPtr hKey;
            hr = gpo.GetRegistryKey(GPO_SECTION_MACHINE, out hKey);
            if (hr != 0)
            {
                Marshal.ThrowExceptionForHR(hr);
            }

            using (SafeRegistryHandle safeHandle = new SafeRegistryHandle(hKey, true))
            using (RegistryKey root = RegistryKey.FromHandle(safeHandle))
            using (RegistryKey key = root.CreateSubKey(subKey))
            {
                key.SetValue(valueName, value, RegistryValueKind.DWord);
            }

            Guid registryExtensionGuid = new Guid("35378EAC-683F-11D2-A89A-00C04FBBCFA2");
            Guid admSnapinGuid = new Guid("0F6B957D-509E-11D1-A7CC-0000F87571E3");

            hr = gpo.Save(true, true, ref registryExtensionGuid, ref admSnapinGuid);
            if (hr != 0)
            {
                Marshal.ThrowExceptionForHR(hr);
            }
        }
    }
}
"@ -ErrorAction Stop
}

# --------------------------
# Set Defender Local GPO
# --------------------------

Execute-Step "Local GPO - Set Turn off real-time protection = Enabled" {
    [NovaLocalGPO.LocalGpoWriter]::SetMachineDwordPolicy(
        "Software\Policies\Microsoft\Windows Defender\Real-Time Protection",
        "DisableRealtimeMonitoring",
        1
    )
}

Execute-Step "Local GPO - Run gpupdate" {
    gpupdate /force
}

Execute-Step "Local GPO - Verify Registry Value" {
    $path = "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection"

    if (-not (Test-Path $path)) {
        throw "Registry path was not created: $path"
    }

    Get-ItemProperty `
        -Path $path `
        -Name "DisableRealtimeMonitoring" `
        -ErrorAction Stop |
        Select-Object DisableRealtimeMonitoring
}

# --------------------------
# Remote Desktop
# --------------------------

Execute-Step "Enable Remote Desktop" {
    $path = "HKLM:\System\CurrentControlSet\Control\Terminal Server"

    Set-ItemProperty `
        -Path $path `
        -Name "fDenyTSConnections" `
        -Value 0 `
        -ErrorAction Stop

    Enable-NetFirewallRule `
        -DisplayGroup "Remote Desktop" `
        -ErrorAction SilentlyContinue

    $value = Get-ItemProperty `
        -Path $path `
        -Name "fDenyTSConnections" `
        -ErrorAction Stop

    Write-Host "fDenyTSConnections = $($value.fDenyTSConnections)"
}

Execute-Step "Disable RDP Network Level Authentication" {
    $path = "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp"

    Set-ItemProperty `
        -Path $path `
        -Name "UserAuthentication" `
        -Value 0 `
        -ErrorAction Stop

    $value = Get-ItemProperty `
        -Path $path `
        -Name "UserAuthentication" `
        -ErrorAction Stop

    Write-Host "UserAuthentication = $($value.UserAuthentication)"
}

# --------------------------
# IE Enhanced Security Configuration
# Correct GUIDs:
# Admins = {A509B1A7-37EF-4b3f-8CFC-4F3A74704073}
# Users  = {A509B1A8-37EF-4b3f-8CFC-4F3A74704073}
# --------------------------

Execute-Step "Disable IE ESC for Administrators" {
    $adminKeys = @(
        "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}"
    )

    $foundAny = $false

    foreach ($key in $adminKeys) {
        if (Test-Path $key) {
            $foundAny = $true

            Set-ItemProperty `
                -Path $key `
                -Name "IsInstalled" `
                -Value 0 `
                -ErrorAction Stop

            Write-Host "Set IsInstalled=0 in $key"
        }
        else {
            Write-Host "Key not found: $key" -ForegroundColor Yellow
        }
    }

    $zoneMapParent = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings"
    $zoneMapPath   = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap"

    if (-not (Test-Path $zoneMapParent)) {
        throw "Parent registry path not found: $zoneMapParent"
    }

    if (-not (Test-Path $zoneMapPath)) {
        New-Item `
            -Path $zoneMapParent `
            -Name "ZoneMap" `
            -ErrorAction Stop | Out-Null

        Write-Host "Created registry key: $zoneMapPath"
    }
    else {
        Write-Host "Registry key already exists: $zoneMapPath"
    }

    New-ItemProperty `
        -Path $zoneMapPath `
        -Name "IEHarden" `
        -Value 0 `
        -PropertyType DWord `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Host "Set HKLM IEHarden=0"

    if (-not $foundAny) {
        Skip-Step "IE ESC Admin Active Setup keys were not found. HKLM ZoneMap IEHarden was still set to 0."
    }
}

Execute-Step "Disable IE ESC for Users" {
    $userKeys = @(
        "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}"
    )

    $foundAny = $false

    foreach ($key in $userKeys) {
        if (Test-Path $key) {
            $foundAny = $true

            Set-ItemProperty `
                -Path $key `
                -Name "IsInstalled" `
                -Value 0 `
                -ErrorAction Stop

            Write-Host "Set IsInstalled=0 in $key"
        }
        else {
            Write-Host "Key not found: $key" -ForegroundColor Yellow
        }
    }

    $zoneMapParent = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    $zoneMapPath   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap"

    if (-not (Test-Path $zoneMapParent)) {
        New-Item `
            -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion" `
            -Name "Internet Settings" `
            -ErrorAction Stop | Out-Null

        Write-Host "Created registry key: $zoneMapParent"
    }

    if (-not (Test-Path $zoneMapPath)) {
        New-Item `
            -Path $zoneMapParent `
            -Name "ZoneMap" `
            -ErrorAction Stop | Out-Null

        Write-Host "Created registry key: $zoneMapPath"
    }
    else {
        Write-Host "Registry key already exists: $zoneMapPath"
    }

    New-ItemProperty `
        -Path $zoneMapPath `
        -Name "IEHarden" `
        -Value 0 `
        -PropertyType DWord `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Host "Set HKCU IEHarden=0"

    if (-not $foundAny) {
        Skip-Step "IE ESC User Active Setup keys were not found. HKCU ZoneMap IEHarden was still set to 0."
    }
}

Execute-Step "Verify IE ESC Settings" {
    $checkItems = @(
        @{
            Name = "IE ESC Admin 64-bit"
            Path = "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}"
        },
        @{
            Name = "IE ESC User 64-bit"
            Path = "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}"
        },
        @{
            Name = "IE ESC Admin 32-bit"
            Path = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}"
        },
        @{
            Name = "IE ESC User 32-bit"
            Path = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}"
        }
    )

    foreach ($item in $checkItems) {
        if (Test-Path $item.Path) {
            $value = Get-ItemProperty `
                -Path $item.Path `
                -Name "IsInstalled" `
                -ErrorAction SilentlyContinue

            [PSCustomObject]@{
                Setting = $item.Name
                Value   = $value.IsInstalled
                Status  = if ($value.IsInstalled -eq 0) { "Disabled" } else { "Enabled or Unknown" }
            }
        }
        else {
            [PSCustomObject]@{
                Setting = $item.Name
                Value   = "Key Not Found"
                Status  = "Skipped"
            }
        }
    }

    $lmZone = Get-ItemProperty `
        -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap" `
        -Name "IEHarden" `
        -ErrorAction SilentlyContinue

    $cuZone = Get-ItemProperty `
        -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap" `
        -Name "IEHarden" `
        -ErrorAction SilentlyContinue

    [PSCustomObject]@{
        Setting = "HKLM ZoneMap IEHarden"
        Value   = $lmZone.IEHarden
        Status  = if ($lmZone.IEHarden -eq 0) { "Disabled" } else { "Enabled or Unknown" }
    }

    [PSCustomObject]@{
        Setting = "HKCU ZoneMap IEHarden"
        Value   = $cuZone.IEHarden
        Status  = if ($cuZone.IEHarden -eq 0) { "Disabled" } else { "Enabled or Unknown" }
    }
}

# --------------------------
# Restart Explorer
# --------------------------

Execute-Step "Restart Explorer Process" {
    $explorer = Get-Process explorer -ErrorAction SilentlyContinue

    if ($null -eq $explorer) {
        Skip-Step "Explorer process is not running."
        return
    }

    Stop-Process -Name explorer -Force -ErrorAction Stop
    Start-Sleep -Seconds 2
    Start-Process explorer.exe -ErrorAction Stop

    Write-Host "Explorer restarted."
}

# --------------------------
# Power Plan
# --------------------------

Execute-Step "Set Power Plan to High Performance" {
    $highPerformance = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

    powercfg /setactive $highPerformance

    $activePlan = powercfg /getactivescheme
    Write-Host $activePlan
}

Execute-Step "Disable Monitor Timeout - AC" {
    powercfg /change monitor-timeout-ac 0
    Write-Host "Monitor timeout on AC set to Never"
}

Execute-Step "Disable Sleep Timeout - AC" {
    powercfg /change standby-timeout-ac 0
    Write-Host "Sleep timeout on AC set to Never"
}

# --------------------------
# Final Summary
# --------------------------

Write-Host ""
Write-Host "===============================" -ForegroundColor Cyan
Write-Host "FINAL SUMMARY" -ForegroundColor Yellow
Write-Host "===============================" -ForegroundColor Cyan

Write-Host "SUCCESS: $script:SuccessCount" -ForegroundColor Green
Write-Host "WARN:    $script:WarnCount" -ForegroundColor DarkYellow
Write-Host "FAIL:    $script:FailCount" -ForegroundColor Red
Write-Host "SKIP:    $script:SkipCount" -ForegroundColor Yellow

if ($script:FailCount -eq 0) {
    Write-Host "All critical steps completed without FAIL." -ForegroundColor Green
}

if ($script:FailCount -ne 0) {
    Write-Host "Some steps failed. Please review the FAIL sections above." -ForegroundColor Red
}

Write-Host ""
Write-Host "Expected Firewall result after restart:" -ForegroundColor Cyan
Write-Host "Private Firewall = OFF and editable" -ForegroundColor Cyan
Write-Host "Public Firewall  = OFF and editable" -ForegroundColor Cyan
Write-Host "No gpresult/report/backup/output files are created by this script." -ForegroundColor Cyan
Write-Host ""
Write-Host "IMPORTANT: Restart is recommended because Windows Security UI may stay cached until restart." -ForegroundColor Yellow

$restart = Read-Host "Do you want to restart the computer now? (Y/N)"

if ($restart -match "^[Yy]$") {
    Restart-Computer -Force
}

if ($restart -notmatch "^[Yy]$") {
    Write-Host "Restart skipped. Please restart manually before checking Windows Security GUI again." -ForegroundColor Yellow
}
