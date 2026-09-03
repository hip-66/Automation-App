<#
.SYNOPSIS
    NovaHUB Combined iDRAC Report via PowerShell
.DESCRIPTION
    A faithful PowerShell translation of ATP_iDrac_Report.py.
    Uses ChromeDriver (WebDriver protocol) for browser automation,
    System.Drawing for desktop screenshots, and Word COM Object
    to create the report document.
    The CMD/PowerShell window is opened on the RIGHT side of the screen
    and the browser occupies 65% on the LEFT side — exactly as the Python original.
    Word document is ALWAYS created, even if the script is interrupted mid-run.
.NOTES
    - MUST HAVE: chromedriver.exe in the same folder.
    - MUST HAVE: Microsoft Word installed (required for the COM object).
    - MUST HAVE: Google Chrome installed.
#>

# ======================================================================
# CONFIGURATION
# ======================================================================
$ErrorActionPreference = "Continue"   # Don't stop on errors — handle them manually
$WorkingDir = $PSScriptRoot
if (-not $WorkingDir) { $WorkingDir = (Get-Location).Path }

# Enable TLS 1.2 and SSL bypass for PowerShell web calls to HTTPS iDRAC endpoints
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11 -bor [Net.SecurityProtocolType]::Tls
[Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}

# ======================================================================
# USER INPUTS: Collected once at the start
# ======================================================================
Write-Host ("=" * 50) -ForegroundColor Cyan
Write-Host " Nova-HUB Unified iDRAC ATP Report Generator " -ForegroundColor Cyan
Write-Host ("=" * 50) -ForegroundColor Cyan
$PO_INPUT = if ($env:PSAUTO_PO_NUMBER) { $env:PSAUTO_PO_NUMBER.Trim() } else { (Read-Host "Please enter PO").Trim() }
$SO_INPUT = if ($env:PSAUTO_SO_NUMBER) { $env:PSAUTO_SO_NUMBER.Trim() } else { (Read-Host "Please enter SO").Trim() }
$SN_INPUT = if ($env:PSAUTO_SN_NUMBER) { $env:PSAUTO_SN_NUMBER.Trim() } else { (Read-Host "Please enter SN").Trim() }
Write-Host ("=" * 50) -ForegroundColor Cyan

# ======================================================================
# GLOBAL CONFIGURATION & SERVER DEFINITIONS
# ======================================================================
$USERNAME = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_USERNAME) { $env:PSAUTO_DEFAULT_USERNAME } else { "root" }
$PASSWORD = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $env:PSAUTO_DEFAULT_PASSWORD } else { "admin1234" }
$TEMP_IMG_DIR = Join-Path $WorkingDir "temp_screenshots"

if (-not (Test-Path $TEMP_IMG_DIR)) {
    New-Item -ItemType Directory -Path $TEMP_IMG_DIR | Out-Null
}

# Targeted Servers — Grouped by Generation
$SERVERS_10 = [ordered]@{
    "FM1"  = "192.168.80.122"
    "FM2"  = "192.168.80.123"
    "K8S1" = "192.168.80.124"
    "K8S2" = "192.168.80.125"
    "K8S3" = "192.168.80.126"
}

$SERVERS_9 = [ordered]@{
    "SRVMGT" = "192.168.80.127"
    "NGINX"  = "192.168.80.128"
}

# addresses.txt (written by PS Automation next to this script) overrides the
# hardcoded IPs above BY POSITION - line 1 -> FM1, line 2 -> FM2, ... line 7 ->
# NGINX - leaving names/order untouched. A standalone run with no
# addresses.txt present is completely unaffected.
$AddressesFile = Join-Path $WorkingDir "addresses.txt"
if (Test-Path $AddressesFile) {
    $overrideIps = @(Get-Content $AddressesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    if ($overrideIps.Count -gt 0) {
        $orderedKeys = @($SERVERS_10.Keys) + @($SERVERS_9.Keys)
        for ($i = 0; $i -lt [Math]::Min($overrideIps.Count, $orderedKeys.Count); $i++) {
            $key = $orderedKeys[$i]
            if ($SERVERS_10.Contains($key)) { $SERVERS_10[$key] = $overrideIps[$i] }
            else { $SERVERS_9[$key] = $overrideIps[$i] }
        }
        Write-Host "Applied $($overrideIps.Count) target IP override(s) from addresses.txt" -ForegroundColor Gray
    }
}

# Navigation Paths — Preserved exactly from the Python original
$NAV_PAGES_10 = @(
    @{ name="iDRAC view entire server's health status"; click_path=@("Dashboard");                                                          needs_scroll=$false },
    @{ name="Server Names";                             click_path=@("iDRAC Settings","Connectivity","Network","Common Settings");           needs_scroll=$false },
    @{ name="IPv4 Settings";                            click_path=@("iDRAC Settings","Connectivity","Network Interface Settings","IPv4");   needs_scroll=$false },
    @{ name="Virtual Disks";                            click_path=@("Storage","Overview","Virtual Disks");                                  needs_scroll=$false },
    @{ name="Firmware Inventory";                       click_path=@("System","Inventory","Firmware Inventory");                             needs_scroll=$false },
    @{ name="Memory setting";                           click_path=@("Configuration","BIOS Settings","Memory Settings");                     needs_scroll=$false },
    @{ name="Processor setting";                        click_path=@("Configuration","BIOS Settings","Processor Settings");                  needs_scroll=$true },
    @{ name="Boot setting";                             click_path=@("Configuration","BIOS Settings","Boot Settings");                       needs_scroll=$false },
    @{ name="FM Integrated Devices";                    click_path=@("Configuration","BIOS Settings","More","Integrated Devices");           needs_scroll=$false },
    @{ name="System profiles settings";                 click_path=@("Configuration","BIOS Settings","More","System Profile Settings");      needs_scroll=$false }
)

$NAV_PAGES_9 = @(
    @{ name="view entire server's health status"; click_path=@("Dashboard");                                                            needs_double_pic=$false },
    @{ name="Common Settings";                    click_path=@("iDRAC Settings","Connectivity","Network","Common Settings");             needs_double_pic=$false },
    @{ name="IPv4 Settings";                      click_path=@("iDRAC Settings","Connectivity","Network","IPv4 Settings");               needs_double_pic=$false },
    @{ name="Virtual Disks";                      click_path=@("Storage","Overview","Virtual Disks");                                    needs_double_pic=$false },
    @{ name="Firmware Inventory";                  click_path=@("System","Inventory","Firmware Inventory");                               needs_double_pic=$false },
    @{ name="Memory Settings";                    click_path=@("Configuration","BIOS Settings","Memory Settings");                       needs_double_pic=$false },
    @{ name="Processor Settings";                 click_path=@("Configuration","BIOS Settings","Processor Settings");                    needs_double_pic=$false },
    @{ name="Boot Settings";                      click_path=@("Configuration","BIOS Settings","Boot Settings");                         needs_double_pic=$false },
    @{ name="Integrated Devices";                 click_path=@("Configuration","BIOS Settings","Integrated Devices");                    needs_double_pic=$true },
    @{ name="System Profile Settings";            click_path=@("Configuration","BIOS Settings","System Profile Settings");               needs_double_pic=$false }
)

# ======================================================================
# C# Assemblies for Screen Capture & Window Manipulation
# ======================================================================
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# P/Invoke signatures for Windows API
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;

public class WinAPI {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, int dwExtraInfo);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, int dwExtraInfo);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool SystemParametersInfo(uint uAction, uint uParam, ref RECT pvParam, uint fWinIni);

    public static IntPtr FindWindowBySubstring(string substring) {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            if (!IsWindowVisible(hWnd)) return true;
            int len = GetWindowTextLength(hWnd);
            if (len > 0) {
                StringBuilder sb = new StringBuilder(len + 1);
                GetWindowText(hWnd, sb, sb.Capacity);
                if (sb.ToString().Contains(substring)) { found = hWnd; return false; }
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public static void GetWorkArea(out int w, out int h) {
        RECT r = new RECT();
        SystemParametersInfo(48, 0, ref r, 0);   // SPI_GETWORKAREA
        w = r.Right - r.Left;
        h = r.Bottom - r.Top;
    }
}
"@

# ======================================================================
# SCREEN CAPTURE  (WebDriver screenshot — works in RDP / headless / service)
# ======================================================================
function Take-ScreenGrab {
    param([string]$FilePath, [string]$SessionId = $null)

    # Primary: GDI+ desktop capture
    try {
        $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $bitmap.Save($FilePath, [System.Drawing.Imaging.ImageFormat]::Png)
        $graphics.Dispose()
        $bitmap.Dispose()
        return
    } catch {
        Write-Host "[WARN] Desktop screenshot failed: $($_.Exception.Message) - trying WebDriver..." -ForegroundColor Yellow
    }

    # Fallback: WebDriver screenshot API
    if ($SessionId) {
        try {
            $res = Invoke-RestMethod -Uri "$global:CD_URL/$SessionId/screenshot" -Method GET -ContentType "application/json"
            $b64 = $res.value
            if ($b64) {
                [IO.File]::WriteAllBytes($FilePath, [Convert]::FromBase64String($b64))
                return
            }
        } catch {}
    }

    Write-Host "[WARN] All screenshot methods failed for: $FilePath" -ForegroundColor Yellow
}

# ======================================================================
# OS-LEVEL WINDOW MANIPULATION
# ======================================================================
function Get-ScreenDimensions {
    $w = 0; $h = 0
    [WinAPI]::GetWorkArea([ref]$w, [ref]$h)
    return @{ Width = $w; Height = $h }
}

function Setup-CmdWindow {
    param([int]$TargetX, [int]$TargetY, [int]$TargetW, [int]$TargetH)

    $uniqueTitle = "iDRAC_CMD_$([int](Get-Date -UFormat %s))"

    # Start actual CMD window with ipconfig /all
    Start-Process cmd.exe -ArgumentList "/K", "title $uniqueTitle & ipconfig /all"
    Start-Sleep -Seconds 3

    $hwnd = [WinAPI]::FindWindowBySubstring($uniqueTitle)
    if ($hwnd -ne [IntPtr]::Zero) {
        [WinAPI]::ShowWindow($hwnd, 9) | Out-Null          # Restore window
        [WinAPI]::SetForegroundWindow($hwnd) | Out-Null     # Bring to front
        Start-Sleep -Seconds 1

        # Apply Ctrl+Minus zoom out via keyboard events
        $VK_CONTROL = 0x11
        $VK_OEM_MINUS = 0xBD
        for ($i = 0; $i -lt 2; $i++) {
            [WinAPI]::keybd_event($VK_CONTROL, 0, 0, 0)
            [WinAPI]::keybd_event($VK_OEM_MINUS, 0, 0, 0)
            Start-Sleep -Milliseconds 50
            [WinAPI]::keybd_event($VK_OEM_MINUS, 0, 2, 0)   # KEYEVENTF_KEYUP
            [WinAPI]::keybd_event($VK_CONTROL, 0, 2, 0)
            Start-Sleep -Milliseconds 300
        }

        # Position window on the right side of the screen
        [WinAPI]::SetWindowPos($hwnd, [IntPtr]::Zero, $TargetX, $TargetY, $TargetW, $TargetH, 0x0040) | Out-Null
        Start-Sleep -Seconds 1

        # Move mouse and click to lock focus on CMD
        $clickX = [int]($TargetX + ($TargetW / 2))
        $clickY = [int]($TargetY + ($TargetH / 2))
        [WinAPI]::SetCursorPos($clickX, $clickY) | Out-Null
        [WinAPI]::mouse_event(0x0002, 0, 0, 0, 0)   # MOUSEEVENTF_LEFTDOWN
        [WinAPI]::mouse_event(0x0004, 0, 0, 0, 0)   # MOUSEEVENTF_LEFTUP
        Start-Sleep -Milliseconds 500

        # Physical scroll to top via mouse wheel events
        for ($i = 0; $i -lt 15; $i++) {
            [WinAPI]::mouse_event(0x0800, 0, 0, (120 * 5), 0)   # MOUSEEVENTF_WHEEL
            Start-Sleep -Milliseconds 50
        }
    }
    return $uniqueTitle
}

# ======================================================================
# CHROMEDRIVER WEBDRIVER PROTOCOL FUNCTIONS
# ======================================================================
$global:CD_PORT = 9515
$global:CD_URL  = "http://localhost:$global:CD_PORT/session"
$ChromeDriverPath = if ($env:CHROMEDRIVER_PATH) { $env:CHROMEDRIVER_PATH } else { Join-Path $WorkingDir "chromedriver.exe" }

if (-not (Test-Path $ChromeDriverPath)) {
    Write-Host "[WARNING] chromedriver.exe not found! Browser automation will not work." -ForegroundColor Yellow
}

$Script:DriverProcess = $null

function Start-ChromeDriver {
    Write-Host "Starting ChromeDriver..."
    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
    $pinfo.FileName = $ChromeDriverPath
    $pinfo.Arguments = "--port=$global:CD_PORT"
    $pinfo.WindowStyle = 'Hidden'
    $pinfo.CreateNoWindow = $true
    $Script:DriverProcess = [System.Diagnostics.Process]::Start($pinfo)
    Start-Sleep -Seconds 2
}

function Stop-ChromeDriver {
    if ($Script:DriverProcess -and -not $Script:DriverProcess.HasExited) {
        try { $Script:DriverProcess.Kill() } catch {}
    }
    Get-Process -Name "chromedriver" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

function Invoke-WD {
    param([string]$Uri, [string]$Method = "GET", $Body)
    $splat = @{ Uri = $Uri; Method = $Method; ContentType = "application/json" }
    if ($Body) { $splat.Body = ($Body | ConvertTo-Json -Depth 10 -Compress) }
    $res = Invoke-RestMethod @splat
    return $res.value
}

# --- Session management (Fixed automation banner & SSL 500 errors) ---
function New-WebSession {
    param([int]$BrowserW, [int]$BrowserH)
    $body = @{
        capabilities = @{
            alwaysMatch = @{
                browserName = "chrome"
                acceptInsecureCerts = $true
                "goog:chromeOptions" = @{
                    excludeSwitches = @("enable-automation")
                    useAutomationExtension = $false
                    args = @(
                        "--ignore-certificate-errors",
                        "--ignore-ssl-errors",
                        "--allow-insecure-localhost",
                        "--disable-infobars",
                        "--disable-blink-features=AutomationControlled",
                        "--window-position=0,0",
                        "--window-size=$BrowserW,$BrowserH"
                    )
                }
            }
        }
    }
    $res = Invoke-WD -Uri $global:CD_URL -Method "POST" -Body $body
    return $res.sessionId
}

function Close-WebSession {
    param($SessionId)
    if ($SessionId) {
        try { Invoke-WD -Uri "$global:CD_URL/$SessionId" -Method "DELETE" | Out-Null } catch {}
    }
}

function Set-Url {
    param($SessionId, [string]$Url)
    Invoke-WD -Uri "$global:CD_URL/$SessionId/url" -Method "POST" -Body @{ url = $Url } | Out-Null
}

# --- Element helpers ---
function Get-ElementId {
    param($el)
    $id = $el."ELEMENT"
    if (-not $id) { $id = $el."element-6066-11e4-a52e-4f735466cecf" }
    return $id
}

function Find-Element {
    param($SessionId, [string]$Using, [string]$Value, [int]$TimeoutMs = 15000)
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    $body = @{ using = $Using; value = $Value }
    while ((Get-Date) -lt $deadline) {
        try {
            $el = Invoke-WD -Uri "$global:CD_URL/$SessionId/element" -Method "POST" -Body $body
            $id = Get-ElementId $el
            if ($id) { return $id }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Find-Elements {
    param($SessionId, [string]$Using, [string]$Value)
    try {
        $body = @{ using = $Using; value = $Value }
        $els = Invoke-WD -Uri "$global:CD_URL/$SessionId/elements" -Method "POST" -Body $body
        return $els
    } catch {
        return @()
    }
}

function Is-ElementVisible {
    param($SessionId, [string]$ElementId)
    try {
        $displayed = Invoke-WD -Uri "$global:CD_URL/$SessionId/element/$ElementId/displayed" -Method "GET"
        return $displayed
    } catch {
        return $false
    }
}

function Click-Element {
    param($SessionId, [string]$ElementId)
    try {
        Invoke-RestMethod -Uri "$global:CD_URL/$SessionId/element/$ElementId/click" `
            -Method "POST" -Body "{}" -ContentType "application/json" | Out-Null
        return $true
    } catch {}
    try {
        $jsBody = @{
            script = "arguments[0].click();"
            args   = @( @{ "ELEMENT" = $ElementId; "element-6066-11e4-a52e-4f735466cecf" = $ElementId } )
        }
        Invoke-WD -Uri "$global:CD_URL/$SessionId/execute/sync" -Method "POST" -Body $jsBody | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Send-ElementKeys {
    param($SessionId, [string]$ElementId, [string]$Text)
    try {
        Invoke-RestMethod -Uri "$global:CD_URL/$SessionId/element/$ElementId/clear" `
            -Method "POST" -Body "{}" -ContentType "application/json" | Out-Null
    } catch {}
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/element/$ElementId/value" -Method "POST" -Body @{ text = $Text } | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Invoke-JS {
    param($SessionId, [string]$Script, $Arguments = @())
    try {
        $body = @{ script = $Script; args = $Arguments }
        return Invoke-WD -Uri "$global:CD_URL/$SessionId/execute/sync" -Method "POST" -Body $body
    } catch {
        return $null
    }
}

function Scroll-IntoView {
    param($SessionId, [string]$ElementId)
    $ref = @{ "ELEMENT" = $ElementId; "element-6066-11e4-a52e-4f735466cecf" = $ElementId }
    Invoke-JS $SessionId "arguments[0].scrollIntoView({block:'center'});" @($ref) | Out-Null
    Start-Sleep -Milliseconds 300
}

function Press-Key {
    param($SessionId, [string]$Key)
    $body = @{
        actions = @(
            @{
                type = "key"
                id   = "keyboard"
                actions = @(
                    @{ type = "keyDown"; value = $Key },
                    @{ type = "keyUp";   value = $Key }
                )
            }
        )
    }
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "POST" -Body $body | Out-Null
    } catch {}
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "DELETE" | Out-Null
    } catch {}
}

function Press-CtrlMinus {
    param($SessionId)
    $body = @{
        actions = @(
            @{
                type = "key"
                id   = "keyboard"
                actions = @(
                    @{ type = "keyDown"; value = [char]0xE009 },   # Control
                    @{ type = "keyDown"; value = "-" },
                    @{ type = "keyUp";   value = "-" },
                    @{ type = "keyUp";   value = [char]0xE009 }
                )
            }
        )
    }
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "POST" -Body $body | Out-Null
    } catch {}
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "DELETE" | Out-Null
    } catch {}
}

function Press-CtrlZero {
    param($SessionId)
    $body = @{
        actions = @(
            @{
                type = "key"
                id   = "keyboard"
                actions = @(
                    @{ type = "keyDown"; value = [char]0xE009 },   # Control
                    @{ type = "keyDown"; value = "0" },
                    @{ type = "keyUp";   value = "0" },
                    @{ type = "keyUp";   value = [char]0xE009 }
                )
            }
        )
    }
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "POST" -Body $body | Out-Null
    } catch {}
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "DELETE" | Out-Null
    } catch {}
}

function Click-AtPosition {
    param($SessionId, [int]$X, [int]$Y)
    $body = @{
        actions = @(
            @{
                type       = "pointer"
                id         = "mouse1"
                parameters = @{ pointerType = "mouse" }
                actions    = @(
                    @{ type = "pointerMove"; duration = 0; x = $X; y = $Y },
                    @{ type = "pointerDown"; button = 0 },
                    @{ type = "pointerUp";   button = 0 }
                )
            }
        )
    }
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "POST" -Body $body | Out-Null
    } catch {}
    try {
        Invoke-WD -Uri "$global:CD_URL/$SessionId/actions" -Method "DELETE" | Out-Null
    } catch {}
}

# ======================================================================
# SMART CLICK ENGINES
# ======================================================================
function Smart-Click-10 {
    param($SessionId, [string]$StepText, [bool]$IsParent = $false, [string]$Child = $null)

    if ($IsParent -and $Child) {
        $childEls = Find-Elements $SessionId "xpath" "//*[text()='$Child']"
        foreach ($ce in $childEls) {
            $ceId = Get-ElementId $ce
            if ($ceId -and (Is-ElementVisible $SessionId $ceId)) {
                return $true
            }
        }
    }

    $els = Find-Elements $SessionId "xpath" "//*[text()='$StepText']"
    foreach ($e in $els) {
        $eId = Get-ElementId $e
        if ($eId -and (Is-ElementVisible $SessionId $eId)) {
            Scroll-IntoView $SessionId $eId
            if (Click-Element $SessionId $eId) {
                Start-Sleep -Milliseconds 2500
                return $true
            }
        }
    }

    $els2 = Find-Elements $SessionId "xpath" "//*[contains(text(), '$StepText')]"
    foreach ($e in $els2) {
        $eId = Get-ElementId $e
        if ($eId -and (Is-ElementVisible $SessionId $eId)) {
            Scroll-IntoView $SessionId $eId
            if (Click-Element $SessionId $eId) {
                Start-Sleep -Milliseconds 2000
                return $true
            }
        }
    }

    return $false
}

function Smart-Click-9 {
    param($SessionId, [string]$StepText)

    $els = Find-Elements $SessionId "xpath" "//*[text()='$StepText']"
    foreach ($e in $els) {
        $eId = Get-ElementId $e
        if ($eId -and (Is-ElementVisible $SessionId $eId)) {
            Scroll-IntoView $SessionId $eId
            if (Click-Element $SessionId $eId) {
                Start-Sleep -Milliseconds 2000
                return $true
            }
        }
    }

    $els2 = Find-Elements $SessionId "xpath" "//*[normalize-space(text())='$StepText']"
    foreach ($e in $els2) {
        $eId = Get-ElementId $e
        if ($eId -and (Is-ElementVisible $SessionId $eId)) {
            Scroll-IntoView $SessionId $eId
            if (Click-Element $SessionId $eId) {
                Start-Sleep -Milliseconds 2000
                return $true
            }
        }
    }

    return $false
}

# ======================================================================
# UNIFIED RESULTS DATA STRUCTURE
# ======================================================================
$captured_results = @{}
for ($i = 0; $i -lt 10; $i++) { $captured_results[$i] = @{} }

# ======================================================================
# REPORT TIMESTAMP & FILENAME
# ======================================================================
$timestamp = (Get-Date).ToString("dd-MM-yyyy_HH-mm")
$ReportFilename = Join-Path $WorkingDir "NovaHUB_ATP_Report_$timestamp.docx"

# ======================================================================
# GET SCREEN DIMENSIONS & COMPUTE LAYOUT
# ======================================================================
$screen = Get-ScreenDimensions
$screenW = $screen.Width
$screenH = $screen.Height

$global_browser_w = [int]($screenW * 2 / 3)
$global_cmd_w     = $screenW - $global_browser_w

# ======================================================================
# CMD INITIALIZATION
# ======================================================================
$cmd_title = Setup-CmdWindow $global_browser_w 0 $global_cmd_w $screenH

# ======================================================================
# MAIN WORKFLOW EXECUTION
# ======================================================================
try {
    if (-not (Test-Path $ChromeDriverPath)) {
        throw "chromedriver.exe not found at: $ChromeDriverPath"
    }
    Start-ChromeDriver

    # ---------------------------------------------------------------
    # PHASE 1: iDRAC 10 SERVERS
    # ---------------------------------------------------------------
    Write-Host "`n>>> Starting iDRAC 10 servers batch..." -ForegroundColor Green

    foreach ($server in $SERVERS_10.GetEnumerator()) {
        $s_name = $server.Name
        $ip     = $server.Value
        Write-Host "[INFO] Connecting to iDRAC 10: $s_name ($ip)"

        $sid = $null
        try {
            $sid = New-WebSession -BrowserW $global_browser_w -BrowserH $screenH
        } catch {
            Write-Host "[ERROR] Failed to create browser session: $($_.Exception.Message)" -ForegroundColor Red
            continue
        }
        if (-not $sid) { Write-Host "[ERROR] Failed to create browser session." -ForegroundColor Red; continue }

        try {
            Set-Url $sid "https://$ip/"
            Start-Sleep -Seconds 5

            $userEl = Find-Element $sid "css selector" "input[name='username'], input[name='user'], #username, #user, #idrac_user" 20000
            if ($userEl) { Send-ElementKeys $sid $userEl $USERNAME | Out-Null }

            $passEl = Find-Element $sid "css selector" "input[name='password'], #password, #idrac_password" 5000
            if ($passEl) { Send-ElementKeys $sid $passEl $PASSWORD | Out-Null }

            $loginEl = Find-Element $sid "css selector" "button:has-text('Log In'), button:has-text('Login'), #btn-login" 5000
            if (-not $loginEl) {
                $loginEl = Find-Element $sid "xpath" "//button[contains(.,'Log In')] | //button[contains(.,'Login')] | //*[@id='btn-login']" 5000
            }
            if ($loginEl) { Click-Element $sid $loginEl | Out-Null }

            Start-Sleep -Seconds 10

            Click-AtPosition $sid ([int]($global_browser_w / 2)) ([int]($screenH / 2))
            Press-CtrlMinus $sid

            for ($idx = 0; $idx -lt $NAV_PAGES_10.Count; $idx++) {
                $nav = $NAV_PAGES_10[$idx]
                
                if ($s_name -match "K8S" -and $nav.name -eq "FM Integrated Devices") { continue }
                
                Write-Host "  [PAGE] $($nav.name)" -ForegroundColor DarkGray

                try {
                    $dashEl = Find-Element $sid "xpath" "//*[text()='Dashboard']" 2000
                    if ($dashEl) { Click-Element $sid $dashEl | Out-Null }
                } catch {}

                $clickPath = $nav.click_path
                for ($i = 0; $i -lt $clickPath.Count; $i++) {
                    $step = $clickPath[$i]
                    if ($step -eq "Dashboard") { continue }
                    $childHint = if ($clickPath.Count -gt 1 -and $i -eq 0) { $clickPath[1] } else { $null }
                    Smart-Click-10 $sid $step ($i -eq 0) $childHint | Out-Null
                }

                Start-Sleep -Seconds 3

                $didZoom = $false
                if ($nav.name -in @("Firmware Inventory", "FM Integrated Devices", "System profiles settings")) {
                    for ($z = 0; $z -lt 4; $z++) { Press-CtrlMinus $sid; Start-Sleep -Milliseconds 300 }
                    $didZoom = $true
                }

                $imgPath = Join-Path $TEMP_IMG_DIR "10_${s_name}_${idx}.png"
                Take-ScreenGrab $imgPath $sid
                $captured_results[$idx][$s_name] = @( $imgPath )

                if ($nav.needs_scroll) {
                    Press-Key $sid ([string][char]0xE00F)
                    Start-Sleep -Milliseconds 2500
                    $imgSc = Join-Path $TEMP_IMG_DIR "10_${s_name}_${idx}_sc.png"
                    Take-ScreenGrab $imgSc $sid
                    $captured_results[$idx][$s_name] += $imgSc
                }

                if ($didZoom) {
                    Press-CtrlZero $sid
                    Start-Sleep -Milliseconds 500
                }
            }
        } catch {
            Write-Host "[ERROR] $s_name`: $($_.Exception.Message)" -ForegroundColor Red
        } finally {
            Close-WebSession $sid
        }
    }

    # ---------------------------------------------------------------
    # PHASE 2: iDRAC 9 SERVERS
    # ---------------------------------------------------------------
    Write-Host "`n>>> Starting iDRAC 9 servers batch..." -ForegroundColor Green

    foreach ($server in $SERVERS_9.GetEnumerator()) {
        $s_name = $server.Name
        $ip     = $server.Value
        Write-Host "[INFO] Connecting to iDRAC 9: $s_name ($ip)"

        $sid = $null
        try {
            $sid = New-WebSession -BrowserW $global_browser_w -BrowserH $screenH
        } catch {
            Write-Host "[ERROR] Failed to create browser session: $($_.Exception.Message)" -ForegroundColor Red
            continue
        }
        if (-not $sid) { continue }

        try {
            Set-Url $sid "https://$ip/"
            Start-Sleep -Seconds 5

            $userEl9 = Find-Element $sid "css selector" "input[name='username'], input[name='user'], #username, #user" 20000
            if ($userEl9) {
                Click-Element $sid $userEl9 | Out-Null
                Send-ElementKeys $sid $userEl9 $USERNAME | Out-Null
            }

            $passEl9 = Find-Element $sid "css selector" "input[name='password'], #password" 5000
            if ($passEl9) {
                Click-Element $sid $passEl9 | Out-Null
                Send-ElementKeys $sid $passEl9 $PASSWORD | Out-Null
            }

            $loginEl9 = Find-Element $sid "xpath" "//button[contains(.,'Log In')] | //*[@id='btn-login'] | //input[@type='submit']" 5000
            if ($loginEl9) { Click-Element $sid $loginEl9 | Out-Null }

            Start-Sleep -Seconds 10

            Click-AtPosition $sid 10 10
            for ($z = 0; $z -lt 3; $z++) {
                Press-CtrlMinus $sid
                Start-Sleep -Milliseconds 300
            }

            for ($idx = 0; $idx -lt $NAV_PAGES_9.Count; $idx++) {
                $nav = $NAV_PAGES_9[$idx]
                
                if ($s_name -in @("NGINX", "SRVMGT") -and $nav.name -eq "Virtual Disks") { continue }
                
                Write-Host "  [PAGE] $($nav.name)" -ForegroundColor DarkGray

                if ($idx -gt 0) {
                    try {
                        $dashEl = Find-Element $sid "xpath" "//*[text()='Dashboard']" 3000
                        if ($dashEl) { Click-Element $sid $dashEl | Out-Null }
                    } catch {}
                }

                $clickPath = $nav.click_path
                for ($i = 0; $i -lt $clickPath.Count; $i++) {
                    $step = $clickPath[$i]
                    if ($step -eq "Dashboard" -and $idx -eq 0) { continue }
                    Smart-Click-9 $sid $step | Out-Null
                }

                Start-Sleep -Seconds 3

                $didZoom = $false
                if ($nav.name -in @("Firmware Inventory", "Processor Settings", "Boot Settings", "System Profile Settings")) {
                    for ($z = 0; $z -lt 4; $z++) { Press-CtrlMinus $sid; Start-Sleep -Milliseconds 300 }
                    $didZoom = $true
                }

                $imgPath = Join-Path $TEMP_IMG_DIR "9_${s_name}_${idx}.png"
                Take-ScreenGrab $imgPath $sid
                $captured_results[$idx][$s_name] = @( $imgPath )

                if ($nav.needs_double_pic) {
                    Invoke-JS $sid "window.scrollBy(0, 1000);" | Out-Null
                    Start-Sleep -Milliseconds 1500
                    $imgSc = Join-Path $TEMP_IMG_DIR "9_${s_name}_${idx}_sc.png"
                    Take-ScreenGrab $imgSc $sid
                    $captured_results[$idx][$s_name] += $imgSc
                }
                
                if ($didZoom) {
                    Press-CtrlZero $sid
                    Start-Sleep -Milliseconds 500
                }
            }
        } catch {
            Write-Host "[ERROR] $s_name`: $($_.Exception.Message)" -ForegroundColor Red
        } finally {
            Close-WebSession $sid
        }
    }

} finally {
    # ======================================================================
    # ALWAYS RUNS
    # ======================================================================
    Stop-ChromeDriver

    # ==================================================================
    # DOCUMENT COMPILATION — Pure PowerShell DOCX
    # ==================================================================
    Write-Host "`n>>> Building Unified Word Document..." -ForegroundColor Green

    try {
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem

        $allOrderedServers = @($SERVERS_10.Keys) + @($SERVERS_9.Keys)
        $imageEntries = @()
        $imgCounter = 1

        for ($idx = 0; $idx -lt 10; $idx++) {
            foreach ($sName in $allOrderedServers) {
                if ($captured_results[$idx].ContainsKey($sName)) {
                    foreach ($iPath in $captured_results[$idx][$sName]) {
                        if (Test-Path $iPath) {
                            $imageEntries += @{
                                relId    = "rId$($imgCounter + 10)"
                                fileName = "image$imgCounter.png"
                                absPath  = (Resolve-Path $iPath).Path
                                idx      = $idx
                                sName    = $sName
                                iPath    = $iPath
                            }
                            $imgCounter++
                        }
                    }
                }
            }
        }

        $bodyXml = ""

        $bodyXml += '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Nova-HUB iDRAC ATP Report</w:t></w:r></w:p>' + "`n"

        $ePO = [System.Security.SecurityElement]::Escape($PO_INPUT)
        $eSO = [System.Security.SecurityElement]::Escape($SO_INPUT)
        $eSN = [System.Security.SecurityElement]::Escape($SN_INPUT)

        $tblBorders = @'
    <w:tblBorders>
      <w:top w:val="single" w:sz="4" w:space="0" w:color="000000"/>
      <w:left w:val="single" w:sz="4" w:space="0" w:color="000000"/>
      <w:bottom w:val="single" w:sz="4" w:space="0" w:color="000000"/>
      <w:right w:val="single" w:sz="4" w:space="0" w:color="000000"/>
      <w:insideH w:val="single" w:sz="4" w:space="0" w:color="000000"/>
      <w:insideV w:val="single" w:sz="4" w:space="0" w:color="000000"/>
    </w:tblBorders>
'@

        $bodyXml += "<w:tbl>`n  <w:tblPr><w:tblStyle w:val=`"TableGrid`"/><w:tblW w:w=`"0`" w:type=`"auto`"/>`n$tblBorders`n  </w:tblPr>`n"
        $bodyXml += "  <w:tr><w:tc><w:p><w:r><w:t>PO</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>$ePO</w:t></w:r></w:p></w:tc></w:tr>`n"
        $bodyXml += "  <w:tr><w:tc><w:p><w:r><w:t>SO</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>$eSO</w:t></w:r></w:p></w:tc></w:tr>`n"
        $bodyXml += "  <w:tr><w:tc><w:p><w:r><w:t>SN</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>$eSN</w:t></w:r></w:p></w:tc></w:tr>`n"
        $bodyXml += "</w:tbl>`n<w:p/>`n"

        $bodyXml += '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + "`n"

        $imgLookup = @{}
        foreach ($ie in $imageEntries) {
            $imgLookup["$($ie.idx)|$($ie.sName)|$($ie.iPath)"] = $ie.relId
        }

        function Get-ImageXml([string]$rId, [int]$wEmu, [int]$hEmu, [int]$picId) {
            $xml  = "<w:p><w:r>`n"
            $xml += "  <w:drawing>`n"
            $xml += "    <wp:inline distT=`"0`" distB=`"0`" distL=`"0`" distR=`"0`">`n"
            $xml += "      <wp:extent cx=`"$wEmu`" cy=`"$hEmu`"/>`n"
            $xml += "      <wp:docPr id=`"$picId`" name=`"Picture`"/>`n"
            $xml += '      <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">' + "`n"
            $xml += '        <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">' + "`n"
            $xml += '          <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">' + "`n"
            $xml += '            <pic:nvPicPr><pic:cNvPr id="0" name="Picture"/><pic:cNvPicPr/></pic:nvPicPr>' + "`n"
            $xml += "            <pic:blipFill>`n"
            $xml += "              <a:blip r:embed=`"$rId`"/>`n"
            $xml += "              <a:stretch><a:fillRect/></a:stretch>`n"
            $xml += "            </pic:blipFill>`n"
            $xml += "            <pic:spPr>`n"
            $xml += "              <a:xfrm><a:off x=`"0`" y=`"0`"/><a:ext cx=`"$wEmu`" cy=`"$hEmu`"/></a:xfrm>`n"
            $xml += '              <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>' + "`n"
            $xml += "            </pic:spPr>`n"
            $xml += "          </pic:pic>`n"
            $xml += "        </a:graphicData>`n"
            $xml += "      </a:graphic>`n"
            $xml += "    </wp:inline>`n"
            $xml += "  </w:drawing>`n"
            $xml += "</w:r></w:p>`n"
            return $xml
        }

        $allImages = [System.Collections.ArrayList]::new()
        for ($idx = 0; $idx -lt 10; $idx++) {
            foreach ($sName in $allOrderedServers) {
                if ($captured_results[$idx].ContainsKey($sName)) {
                    foreach ($iPath in $captured_results[$idx][$sName]) {
                        if (Test-Path $iPath) {
                            $rId = $imgLookup["$idx|$sName|$iPath"]
                            if ($rId) {
                                $allImages.Add(@{
                                    idx   = $idx
                                    sName = $sName
                                    iPath = $iPath
                                    relId = $rId
                                }) | Out-Null
                            }
                        }
                    }
                }
            }
        }

        $maxW_emu = 6858000
        $maxH_emu = 3657600
        $lastIdx = -1
        $imagesOnPage = 0
        $picIdCounter = 1

        foreach ($imgItem in $allImages) {
            $curIdx  = $imgItem.idx
            $curName = $imgItem.sName
            $curPath = $imgItem.iPath
            $curRId  = $imgItem.relId

            if ($curIdx -ne $lastIdx) {
                if ($lastIdx -ge 0) {
                    $bodyXml += '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + "`n"
                }
                $testTitle = [System.Security.SecurityElement]::Escape($NAV_PAGES_10[$curIdx].name)
                $bodyXml += "<w:p><w:pPr><w:pStyle w:val=`"Heading2`"/></w:pPr><w:r><w:t>$($curIdx + 1). ${testTitle}:</w:t></w:r></w:p>`n"
                $lastIdx = $curIdx
                $imagesOnPage = 0
            }

            if ($imagesOnPage -ge 2) {
                $bodyXml += '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + "`n"
                $imagesOnPage = 0
            }

            $bodyXml += "<w:p><w:pPr><w:spacing w:before=`"60`" w:after=`"40`"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>${curName}:</w:t></w:r></w:p>`n"

            try {
                $img = [System.Drawing.Image]::FromFile((Resolve-Path $curPath).Path)
                $origW = $img.Width
                $origH = $img.Height
                $img.Dispose()
            } catch {
                $origW = 1920
                $origH = 1080
            }

            $scaleW = $maxW_emu / $origW
            $scaleH = $maxH_emu / $origH
            $scale  = [Math]::Min($scaleW, $scaleH)
            $targetW_emu = [int]($origW * $scale)
            $targetH_emu = [int]($origH * $scale)

            $bodyXml += (Get-ImageXml $curRId $targetW_emu $targetH_emu $picIdCounter)
            $picIdCounter++
            $imagesOnPage++
        }

        $docHeader = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
  xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
  xmlns:o="urn:schemas-microsoft-com:office:office"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
  xmlns:v="urn:schemas-microsoft-com:vml"
  xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
  xmlns:w10="urn:schemas-microsoft-com:office:word"
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
  xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
  xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
  xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
  xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
  mc:Ignorable="w14 wp14">
  <w:body>
'@
        $docFooter = @'

    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
'@
        $documentXml = $docHeader + "`n" + $bodyXml + $docFooter

        $relsXml = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
'@
        $relsXml += "`n"
        foreach ($ie in $imageEntries) {
            $relsXml += "  <Relationship Id=`"$($ie.relId)`" Type=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image`" Target=`"media/$($ie.fileName)`"/>`n"
        }
        $relsXml += "</Relationships>"

        $stylesXml = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="36"/><w:color w:val="2E74B5"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:pPr><w:spacing w:before="200" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/><w:color w:val="2E74B5"/></w:rPr>
  </w:style>
  <w:style w:type="table" w:styleId="TableGrid">
    <w:name w:val="Table Grid"/>
    <w:tblPr>
      <w:tblBorders>
        <w:top w:val="single" w:sz="4" w:space="0" w:color="000000"/>
        <w:left w:val="single" w:sz="4" w:space="0" w:color="000000"/>
        <w:bottom w:val="single" w:sz="4" w:space="0" w:color="000000"/>
        <w:right w:val="single" w:sz="4" w:space="0" w:color="000000"/>
        <w:insideH w:val="single" w:sz="4" w:space="0" w:color="000000"/>
        <w:insideV w:val="single" w:sz="4" w:space="0" w:color="000000"/>
      </w:tblBorders>
    </w:tblPr>
  </w:style>
</w:styles>
'@

        $contentTypesXml = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
'@

        $rootRelsXml = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
'@

        if (Test-Path $ReportFilename) { Remove-Item $ReportFilename -Force }

        $zipStream = [System.IO.File]::Create($ReportFilename)
        $zip = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)

        function Add-ZipText([System.IO.Compression.ZipArchive]$z, [string]$entryName, [string]$text) {
            $entry = $z.CreateEntry($entryName)
            $sw = New-Object System.IO.StreamWriter($entry.Open())
            $sw.Write($text)
            $sw.Dispose()
        }

        Add-ZipText $zip "[Content_Types].xml"          $contentTypesXml
        Add-ZipText $zip "_rels/.rels"                   $rootRelsXml
        Add-ZipText $zip "word/document.xml"             $documentXml
        Add-ZipText $zip "word/styles.xml"               $stylesXml
        Add-ZipText $zip "word/_rels/document.xml.rels"  $relsXml

        foreach ($ie in $imageEntries) {
            $imgEntry = $zip.CreateEntry("word/media/$($ie.fileName)")
            $imgStream = $imgEntry.Open()
            $fileBytes = [System.IO.File]::ReadAllBytes($ie.absPath)
            $imgStream.Write($fileBytes, 0, $fileBytes.Length)
            $imgStream.Dispose()
        }

        $zip.Dispose()
        $zipStream.Dispose()

        Write-Host "[SUCCESS] Final Report saved: $ReportFilename" -ForegroundColor Green

    } catch {
        Write-Host "[ERROR] Failed to save Word Document: $($_.Exception.Message)" -ForegroundColor Red
    }

    if ($cmd_title) {
        & cmd.exe /c "taskkill /F /FI `"WINDOWTITLE eq $cmd_title*`" >nul 2>&1"
    }

    if (Test-Path $TEMP_IMG_DIR) {
        Remove-Item -Path $TEMP_IMG_DIR -Recurse -Force -ErrorAction SilentlyContinue
    }
}