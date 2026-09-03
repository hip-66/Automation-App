#Requires -RunAsAdministrator
<#
.SYNOPSIS
    ATP Windows Settings Screenshot Automation - Chapter 2
    Run as Administrator on each server.

.DESCRIPTION
    Captures all 15 required Windows settings screenshots, each showing the
    settings window on the LEFT half and ipconfig /all on the RIGHT half,
    exactly matching the ATP documentation template.

.PARAMETER OutputFolder
    Where to save screenshots. Defaults to C:\ATP_Screenshots\<ServerName>
    Can be a network share: \\server\share\ATP_Screenshots

.PARAMETER SkipSections
    Comma-separated section numbers to skip (e.g. "4,5")

.EXAMPLE
    .\Capture-WindowsSettings.ps1
    .\Capture-WindowsSettings.ps1 -OutputFolder "\\fileserver\share\ATP"
    .\Capture-WindowsSettings.ps1 -SkipSections "4,15"
#>
param(
    [string]$OutputFolder = "C:\ATP_Screenshots\$env:COMPUTERNAME",
    [string]$SkipSections = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ─── Assemblies ───────────────────────────────────────────────────────────────
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int ht, bool r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    // SetWindowPos — used to pin windows as always-on-top (HWND_TOPMOST = -1)
    [DllImport("user32.dll")] public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
}
"@

# HWND_TOPMOST = -1, SWP_NOMOVE|SWP_NOSIZE = 0x0003
$HWND_TOPMOST    = [IntPtr](-1)
$SWP_NOMOVE_SIZE = [uint32]0x0003

# ─── Layout constants ─────────────────────────────────────────────────────────
$screen  = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$SW      = $screen.Width
$SH      = $screen.Height
$leftW   = [int]($SW * 0.60)
$rightW  = $SW - $leftW
$winH    = $SH - 50   # leave room for taskbar

$skip = $SkipSections -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }

# ─── Helpers ──────────────────────────────────────────────────────────────────
function Set-WinPos([IntPtr]$hwnd, [string]$side) {
    if ($hwnd -eq [IntPtr]::Zero) { return }
    [Win32]::ShowWindow($hwnd, 9) | Out-Null   # SW_RESTORE
    Start-Sleep -Milliseconds 150
    if ($side -eq "left") {
        [Win32]::MoveWindow($hwnd, 0, 0, $leftW, $winH, $true) | Out-Null
    } else {
        [Win32]::MoveWindow($hwnd, $leftW, 0, $rightW, $winH, $true) | Out-Null
    }
    # Pin as always-on-top so nothing can cover it during the screenshot
    [Win32]::SetWindowPos($hwnd, $HWND_TOPMOST, 0, 0, 0, 0, $SWP_NOMOVE_SIZE) | Out-Null
    [Win32]::SetForegroundWindow($hwnd) | Out-Null
    Start-Sleep -Milliseconds 200
}

function Save-Screen([string]$path) {
    # Script window stays minimized throughout — never appears in any screenshot
    if ($script:myHwnd -ne [IntPtr]::Zero) {
        [Win32]::ShowWindow($script:myHwnd, 6) | Out-Null   # SW_MINIMIZE
    }
    # Re-position ipconfig window to the right and set topmost
    if ($script:ipcfgHwnd -ne [IntPtr]::Zero) {
        Set-WinPos $script:ipcfgHwnd "right"
        # Focus it and send Ctrl+Home to scroll buffer to the very top
        [Win32]::SetForegroundWindow($script:ipcfgHwnd) | Out-Null
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("^{HOME}")
        Start-Sleep -Milliseconds 350
    }
    Start-Sleep -Milliseconds 500
    $bmp = New-Object System.Drawing.Bitmap($SW, $SH)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
    $g.Dispose()
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    # Log to file (window stays minimized so Write-Host goes to buffer)
    Write-Host "    -> $([IO.Path]::GetFileName($path))" -ForegroundColor Green
}

function Find-Win([string]$pat, [int]$sec = 15) {
    $dl = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $dl) {
        $p = Get-Process -EA SilentlyContinue |
             Where-Object { $_.MainWindowTitle -match $pat -and $_.MainWindowHandle -ne [IntPtr]::Zero } |
             Select-Object -First 1
        if ($p) { return $p }
        Start-Sleep -Milliseconds 400
    }
    return $null
}

function Get-ProcWin([System.Diagnostics.Process]$proc, [int]$sec = 15) {
    $dl = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $dl) {
        $p = Get-Process -Id $proc.Id -EA SilentlyContinue
        if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) { return $p }
        Start-Sleep -Milliseconds 400
    }
    return $null
}

function Close-Win([System.Diagnostics.Process]$proc) {
    if (-not $proc -or $proc.HasExited) { return }
    try { $proc.CloseMainWindow() | Out-Null } catch {}
    Start-Sleep -Milliseconds 500
    if (-not $proc.HasExited) { try { $proc.Kill() } catch {} }
}

function Invoke-UILink([int]$procId, [string]$linkName) {
    try {
        $root   = [System.Windows.Automation.AutomationElement]::RootElement
        $cond   = New-Object System.Windows.Automation.PropertyCondition(
                      [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
        $window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
        if (-not $window) { return $false }
        $linkCond = New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::NameProperty, $linkName)
        $elem = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $linkCond)
        if (-not $elem) { return $false }
        # Try InvokePattern first (hyperlinks, buttons)
        try {
            $inv = $elem.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $inv.Invoke(); return $true
        } catch {}
        # Try SelectionItemPattern (tree/list items like "Local Server")
        try {
            $sel = $elem.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            $sel.Select(); return $true
        } catch {}
        # Fallback: just give focus
        try { $elem.SetFocus(); return $true } catch {}
        return $false
    } catch { return $false }
}

function Skip-Section([int]$n) { return $skip -contains "$n" }

# ─── Init ─────────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $OutputFolder | Out-Null
$SRV = $env:COMPUTERNAME
Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  ATP Windows Settings Capture  |  Server: $SRV" -ForegroundColor Cyan
Write-Host "  Output: $OutputFolder" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# ─── Persistent ipconfig /all window on the RIGHT (small font, wide buffer) ───
Write-Host "Opening ipconfig /all window..." -ForegroundColor Yellow

# PowerShell script that shrinks its own font via SetCurrentConsoleFontEx,
# widens the buffer, then runs ipconfig /all — so the full output is visible.
$ipcfgScript = @'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class ConsoleFont {
    [StructLayout(LayoutKind.Sequential)]
    public struct COORD { public short X; public short Y; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct CONSOLE_FONT_INFOEX {
        public uint   cbSize;
        public uint   nFont;
        public COORD  dwFontSize;
        public int    FontFamily;
        public int    FontWeight;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)]
        public string FaceName;
    }
    [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
    [DllImport("kernel32.dll")] public static extern bool SetCurrentConsoleFontEx(
        IntPtr h, bool max, ref CONSOLE_FONT_INFOEX f);
    public static void SetFont(short height) {
        var h = GetStdHandle(-11);
        var f = new CONSOLE_FONT_INFOEX();
        f.cbSize    = (uint)System.Runtime.InteropServices.Marshal.SizeOf(f);
        f.FaceName  = "Consolas";
        f.dwFontSize.Y = height;
        f.FontFamily   = 54;
        f.FontWeight   = 400;
        SetCurrentConsoleFontEx(h, false, ref f);
    }
}
"@
[ConsoleFont]::SetFont(11)
$b = $Host.UI.RawUI.BufferSize; $b.Width = 220; $Host.UI.RawUI.BufferSize = $b
Clear-Host
ipconfig /all
# Scroll back to the very top so Host Name is the first visible line
$pos = $Host.UI.RawUI.WindowPosition; $pos.Y = 0; $Host.UI.RawUI.WindowPosition = $pos
Read-Host "`nPress Enter to close"
'@

$ipcfgTmp  = "$env:TEMP\atp_ipconfig.ps1"
$ipcfgScript | Out-File $ipcfgTmp -Encoding UTF8
$ipcfgProc = Start-Process powershell.exe `
    -ArgumentList "-NoProfile -NoExit -File `"$ipcfgTmp`"" -PassThru

$ipcfgWin  = Get-ProcWin $ipcfgProc 20
$script:ipcfgHwnd = if ($ipcfgWin) { $ipcfgWin.MainWindowHandle } else { [IntPtr]::Zero }
Set-WinPos $script:ipcfgHwnd "right"

# Store this script's own window handle and minimize immediately — stays minimized throughout
$_myProc = Get-Process -Id $PID -EA SilentlyContinue
$script:myHwnd = if ($_myProc) { $_myProc.MainWindowHandle } else { [IntPtr]::Zero }
if ($script:myHwnd -ne [IntPtr]::Zero) {
    [Win32]::ShowWindow($script:myHwnd, 6) | Out-Null   # SW_MINIMIZE — keep minimized always
}

Start-Sleep -Milliseconds 800

# ════════════════════════════════════════════════════════════════════════════════
# 1. WINDOWS VERSION
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 1)) {
    Write-Host "[1/15] Windows Version" -ForegroundColor White
    $p = Start-Process winver -PassThru
    Start-Sleep -Milliseconds 2000
    $w = Find-Win "About Windows" 10
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\01_WindowsVersion_$SRV.png"
    if ($w) { Close-Win $w } else { try { $p.CloseMainWindow() | Out-Null } catch {} }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 2. POWER SETTINGS  (Power Options > Edit Plan Settings)
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 2)) {
    Write-Host "[2/15] Power Settings" -ForegroundColor White
    # Open Power Options
    Start-Process "explorer.exe" "shell:::{025A5937-A6BE-4686-A844-36FE4BEC8B6D}"
    Start-Sleep -Milliseconds 3000
    $w = Find-Win "Power Options" 12
    if ($w) {
        Set-WinPos $w.MainWindowHandle "left"
        Start-Sleep -Milliseconds 800
        # Navigate to "Change plan settings" via UI Automation
        $clicked = Invoke-UILink $w.Id "Change plan settings"
        if ($clicked) {
            Start-Sleep -Milliseconds 2000
            $w2 = Find-Win "Edit Plan Settings" 8
            if ($w2) { Set-WinPos $w2.MainWindowHandle "left" }
        }
    }
    Save-Screen "$OutputFolder\02_PowerSettings_$SRV.png"
    Get-Process -EA SilentlyContinue | Where-Object {
        $_.MainWindowTitle -match "Power Options|Edit Plan Settings"
    } | ForEach-Object { try { $_.CloseMainWindow() | Out-Null } catch {} }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 3. USER ACCOUNT CONTROL
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 3)) {
    Write-Host "[3/15] User Account Control" -ForegroundColor White
    Start-Process "UserAccountControlSettings.exe"
    Start-Sleep -Milliseconds 2500
    $w = Find-Win "User Account Control" 10
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\03_UAC_$SRV.png"
    if ($w) { Close-Win $w }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 4. IE ENHANCED SECURITY  (Server Manager > Local Server > click link)
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 4)) {
    Write-Host "[4/15] IE Enhanced Security" -ForegroundColor White
    $smProc = Get-Process ServerManager -EA SilentlyContinue |
              Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1
    if (-not $smProc) {
        $smProc = Start-Process ServerManager -PassThru
        Start-Sleep -Milliseconds 7000
        $smProc = Find-Win "Server Manager" 20
    }
    if ($smProc) {
        Set-WinPos $smProc.MainWindowHandle "left"
        Start-Sleep -Milliseconds 2000

        # Step 1 — navigate to Local Server in the sidebar
        Invoke-UILink $smProc.Id "Local Server" | Out-Null
        Start-Sleep -Milliseconds 2500

        # Step 2 — click "IE Enhanced Security Configuration" link in the properties panel
        $clicked = Invoke-UILink $smProc.Id "IE Enhanced Security Configuration"
        if ($clicked) {
            Start-Sleep -Milliseconds 2500
            # Position the dialog centered over the left half so OFF/OFF is visible
            $dlg = Find-Win "Internet Explorer Enhanced Security Configuration" 8
            if ($dlg) {
                $dlgW = 460; $dlgH = 290
                $dlgX = [int](($leftW - $dlgW) / 2)
                $dlgY = [int](($winH  - $dlgH) / 2)
                [Win32]::ShowWindow($dlg.MainWindowHandle, 9) | Out-Null
                [Win32]::MoveWindow($dlg.MainWindowHandle, $dlgX, $dlgY, $dlgW, $dlgH, $true) | Out-Null
                [Win32]::SetForegroundWindow($dlg.MainWindowHandle) | Out-Null
            }
        }
    }
    Save-Screen "$OutputFolder\04_IEEnhancedSecurity_$SRV.png"
    # Close the IE ESC dialog
    $dlg2 = Find-Win "Internet Explorer Enhanced Security Configuration" 3
    if ($dlg2) {
        [System.Windows.Forms.SendKeys]::SendWait("{ESCAPE}")
        Start-Sleep -Milliseconds 400
    }
    if ($smProc) { try { $smProc.CloseMainWindow() | Out-Null } catch {} }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 5. TIME AND DATE  (script window minimized so desktop is visible)
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 5)) {
    Write-Host "[5/15] Time and Date" -ForegroundColor White
    Start-Process "timedate.cpl"
    Start-Sleep -Milliseconds 2500
    $w = Find-Win "Date and Time" 10
    if ($w) {
        # Position the dialog in the lower-left so desktop is visible above it
        $dlgW = 380; $dlgH = 420
        [Win32]::ShowWindow($w.MainWindowHandle, 9) | Out-Null
        [Win32]::MoveWindow($w.MainWindowHandle, 30, ($SH - $dlgH - 80), $dlgW, $dlgH, $true) | Out-Null
        [Win32]::SetForegroundWindow($w.MainWindowHandle) | Out-Null
    }
    Save-Screen "$OutputFolder\05_TimeDate_$SRV.png"
    if ($w) { Close-Win $w }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 6. WINDOWS ACTIVATION
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 6)) {
    Write-Host "[6/15] Windows Activation" -ForegroundColor White
    Start-Process "ms-settings:activation"
    Start-Sleep -Milliseconds 4000
    $w = Find-Win "Settings" 12
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\06_Activation_$SRV.png"
    if ($w) { Close-Win $w }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 7. DEVICE MANAGER
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 7)) {
    Write-Host "[7/15] Device Manager" -ForegroundColor White
    $p = Start-Process "devmgmt.msc" -PassThru
    Start-Sleep -Milliseconds 3500
    $w = Find-Win "Device Manager" 12
    if (-not $w) { $w = Get-ProcWin $p 5 }
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\07_DeviceManager_$SRV.png"
    if ($w) { Close-Win $w } else { Close-Win $p }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 8. DISK MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 8)) {
    Write-Host "[8/15] Disk Management" -ForegroundColor White
    $p = Start-Process "diskmgmt.msc" -PassThru
    Start-Sleep -Milliseconds 5000   # slow to load
    $w = Find-Win "Disk Management" 15
    if (-not $w) { $w = Get-ProcWin $p 8 }
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\08_DiskManagement_$SRV.png"
    if ($w) { Close-Win $w } else { Close-Win $p }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 9. INSTALLED KBs  (systeminfo in PowerShell, small font)
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 9)) {
    Write-Host "[9/15] Installed KBs (systeminfo)" -ForegroundColor White
    $kbScript = @'
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class ConsoleFont {
    [StructLayout(LayoutKind.Sequential)] public struct COORD { public short X; public short Y; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct CONSOLE_FONT_INFOEX {
        public uint cbSize; public uint nFont; public COORD dwFontSize;
        public int FontFamily; public int FontWeight;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string FaceName;
    }
    [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
    [DllImport("kernel32.dll")] public static extern bool SetCurrentConsoleFontEx(IntPtr h, bool max, ref CONSOLE_FONT_INFOEX f);
    public static void SetFont(short height) {
        var h = GetStdHandle(-11); var f = new CONSOLE_FONT_INFOEX();
        f.cbSize = (uint)System.Runtime.InteropServices.Marshal.SizeOf(f);
        f.FaceName = "Consolas"; f.dwFontSize.Y = height; f.FontFamily = 54; f.FontWeight = 400;
        SetCurrentConsoleFontEx(h, false, ref f);
    }
}
"@
[ConsoleFont]::SetFont(10)
$b = $Host.UI.RawUI.BufferSize; $b.Width = 220; $Host.UI.RawUI.BufferSize = $b
Clear-Host
systeminfo
Read-Host "`nPress Enter to close"
'@
    $kbTmp = "$env:TEMP\atp_kblist.ps1"
    $kbScript | Out-File $kbTmp -Encoding UTF8
    $p = Start-Process powershell.exe -ArgumentList "-NoProfile -NoExit -File `"$kbTmp`"" -PassThru
    Start-Sleep -Milliseconds 8000   # systeminfo takes time
    $w = Get-ProcWin $p 20
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\09_KBList_$SRV.png"
    Close-Win $p
    Remove-Item $kbTmp -EA SilentlyContinue
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 10. INSTALLED PROGRAMS
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 10)) {
    Write-Host "[10/15] Installed Programs" -ForegroundColor White
    Start-Process "appwiz.cpl"
    Start-Sleep -Milliseconds 3000
    $w = Find-Win "Programs and Features" 12
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\10_InstalledPrograms_$SRV.png"
    if ($w) { Close-Win $w }
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 11. IPCONFIG /ALL  (two CMD windows)
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 11)) {
    Write-Host "[11/15] ipconfig /all" -ForegroundColor White
    $ipcfg11 = @'
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class ConsoleFont {
    [StructLayout(LayoutKind.Sequential)] public struct COORD { public short X; public short Y; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct CONSOLE_FONT_INFOEX {
        public uint cbSize; public uint nFont; public COORD dwFontSize;
        public int FontFamily; public int FontWeight;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string FaceName;
    }
    [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
    [DllImport("kernel32.dll")] public static extern bool SetCurrentConsoleFontEx(IntPtr h, bool max, ref CONSOLE_FONT_INFOEX f);
    public static void SetFont(short height) {
        var h = GetStdHandle(-11); var f = new CONSOLE_FONT_INFOEX();
        f.cbSize = (uint)System.Runtime.InteropServices.Marshal.SizeOf(f);
        f.FaceName = "Consolas"; f.dwFontSize.Y = height; f.FontFamily = 54; f.FontWeight = 400;
        SetCurrentConsoleFontEx(h, false, ref f);
    }
}
"@
[ConsoleFont]::SetFont(11)
$b = $Host.UI.RawUI.BufferSize; $b.Width = 220; $Host.UI.RawUI.BufferSize = $b
Clear-Host
ipconfig /all
$pos = $Host.UI.RawUI.WindowPosition; $pos.Y = 0; $Host.UI.RawUI.WindowPosition = $pos
Read-Host "`nPress Enter to close"
'@
    $tmp11 = "$env:TEMP\atp_ipcfg11.ps1"
    $ipcfg11 | Out-File $tmp11 -Encoding UTF8
    $p = Start-Process powershell.exe -ArgumentList "-NoProfile -NoExit -File `"$tmp11`"" -PassThru
    Start-Sleep -Milliseconds 3000
    $w = Get-ProcWin $p 10
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\11_IpconfigAll_$SRV.png"
    Close-Win $p
    Remove-Item $tmp11 -EA SilentlyContinue
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 12. FIREWALL STATUS
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 12)) {
    Write-Host "[12/15] Firewall" -ForegroundColor White
    $cmd = 'Get-NetFirewallProfile -All -PolicyStore $env:COMPUTERNAME | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction | Format-Table -AutoSize; Write-Host ""; Read-Host "Press Enter to close"'
    $p = Start-Process powershell.exe -ArgumentList "-NoExit -NoProfile -Command `"$cmd`"" -PassThru
    Start-Sleep -Milliseconds 4000
    $w = Get-ProcWin $p 12
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\12_Firewall_$SRV.png"
    Close-Win $p
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 13. WINDOWS DEFENDER
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 13)) {
    Write-Host "[13/15] Windows Defender" -ForegroundColor White
    $cmd = 'Get-MpComputerStatus | Select-Object RealTimeProtectionEnabled, AMServiceEnabled, AntispywareEnabled, AntivirusEnabled | Format-List; Write-Host ""; Read-Host "Press Enter to close"'
    $p = Start-Process powershell.exe -ArgumentList "-NoExit -NoProfile -Command `"$cmd`"" -PassThru
    Start-Sleep -Milliseconds 4000
    $w = Get-ProcWin $p 12
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\13_Defender_$SRV.png"
    Close-Win $p
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 14. JUMBO FRAMES / MTU
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 14)) {
    Write-Host "[14/15] Jumbo Frames / MTU" -ForegroundColor White
    $mtuScript = @'
Write-Host "=== Adapters MTU ===" -ForegroundColor Cyan
Get-NetAdapter | Where-Object {$_.Status -eq "Up"} | Select-Object Name, MtuSize | Format-Table -AutoSize

Write-Host "=== Jumbo Packet Settings ===" -ForegroundColor Cyan
$names = (Get-NetAdapter | Where-Object {$_.Status -eq "Up"}).Name
Get-NetAdapterAdvancedProperty -InterfaceAlias $names -DisplayName "Jumbo Packet" -ErrorAction SilentlyContinue |
    Select-Object Name, DisplayName, DisplayValue | Format-Table -AutoSize

Write-Host "=== NlMtu per Interface ===" -ForegroundColor Cyan
Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceAlias, NlMtu | Format-Table -AutoSize

Read-Host "Press Enter to close"
'@
    $tmpFile = "$env:TEMP\atp_mtu.ps1"
    $mtuScript | Out-File $tmpFile -Encoding UTF8
    $p = Start-Process powershell.exe -ArgumentList "-NoExit -NoProfile -File `"$tmpFile`"" -PassThru
    Start-Sleep -Milliseconds 4000
    $w = Get-ProcWin $p 12
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\14_MTU_$SRV.png"
    Close-Win $p
    Remove-Item $tmpFile -EA SilentlyContinue
    Start-Sleep -Milliseconds 400
}

# ════════════════════════════════════════════════════════════════════════════════
# 15. TCP / DNS PARAMETERS  (relevant for APP servers, runs on all)
# ════════════════════════════════════════════════════════════════════════════════
if (-not (Skip-Section 15)) {
    Write-Host "[15/15] TCP/DNS Parameters" -ForegroundColor White
    $tcpScript = @'
Write-Host "=== TCP/IP Parameters ===" -ForegroundColor Cyan
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
$params  = Get-ItemProperty $regPath
$params | Select-Object MaxUserPort, TcpTimedWaitDelay, SocketPoolSize, TcpNumConnections | Format-List

Write-Host "=== DNS Client Settings ===" -ForegroundColor Cyan
Get-DnsClientServerAddress | Select-Object InterfaceAlias, AddressFamily, ServerAddresses | Format-Table -AutoSize

Read-Host "Press Enter to close"
'@
    $tmpFile2 = "$env:TEMP\atp_tcp.ps1"
    $tcpScript | Out-File $tmpFile2 -Encoding UTF8
    $p = Start-Process powershell.exe -ArgumentList "-NoExit -NoProfile -File `"$tmpFile2`"" -PassThru
    Start-Sleep -Milliseconds 4000
    $w = Get-ProcWin $p 12
    if ($w) { Set-WinPos $w.MainWindowHandle "left" }
    Save-Screen "$OutputFolder\15_TCPParams_$SRV.png"
    Close-Win $p
    Remove-Item $tmpFile2 -EA SilentlyContinue
    Start-Sleep -Milliseconds 400
}

# ─── Cleanup ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Closing ipconfig window..." -ForegroundColor Yellow
Close-Win $ipcfgProc
Remove-Item "$env:TEMP\atp_ipconfig.ps1" -EA SilentlyContinue

# Restore script window so the user can see the final summary
if ($script:myHwnd -ne [IntPtr]::Zero) {
    [Win32]::ShowWindow($script:myHwnd, 9) | Out-Null   # SW_RESTORE
    Start-Sleep -Milliseconds 400
}

$count = (Get-ChildItem "$OutputFolder\*.png" -EA SilentlyContinue).Count
Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Done! $count screenshots saved to:" -ForegroundColor Cyan
Write-Host "  $OutputFolder" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next step: copy this folder to the central machine and run:" -ForegroundColor Yellow
Write-Host "  .\Generate-Report.ps1 -Screenshots <combined-folder>" -ForegroundColor Yellow
Write-Host ""
