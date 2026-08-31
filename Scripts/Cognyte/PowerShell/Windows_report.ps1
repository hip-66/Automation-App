Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, int c, int e);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int hh, bool r);
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, int w, int l);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")]  public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")]  public static extern int SendMessage(IntPtr hWnd, uint Msg, int wParam, int lParam);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxLen);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder cls, int maxLen);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    public const uint BM_CLICK  = 0x00F5;
    public const uint LEFTDOWN  = 0x0002;
    public const uint LEFTUP    = 0x0004;
    public const uint RIGHTDOWN = 0x0008;
    public const uint RIGHTUP   = 0x0010;
    public const uint WM_CLOSE  = 0x0010;
}
"@

# ---- Minimize the PowerShell console for the entire run ----
$consoleHwnd = [Win32]::GetConsoleWindow()
[Win32]::ShowWindow($consoleHwnd, 6) | Out-Null   # SW_MINIMIZE

$hostname   = $env:COMPUTERNAME
$scriptDir  = $PSScriptRoot
$OutputDocx = "$scriptDir\${hostname}_Validation.docx"
$screenW    = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width
$screenH    = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height

function Log([string]$msg) {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg"
}

function Log-TopWindows([string]$label) {
    Log "=== OPEN WINDOWS ($label) ==="
    try {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $allW = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                    [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($w in $allW) {
            try {
                $hwnd = [IntPtr]$w.Current.NativeWindowHandle
                if (-not [Win32]::IsWindowVisible($hwnd)) { continue }
                $clsSb = New-Object System.Text.StringBuilder 128
                [Win32]::GetClassName($hwnd, $clsSb, 128) | Out-Null
                $wr = New-Object Win32+RECT
                [Win32]::GetWindowRect($hwnd,[ref]$wr) | Out-Null
                Log "  hwnd=$hwnd cls='$($clsSb)' rect=($($wr.Left),$($wr.Top),$($wr.Right),$($wr.Bottom)) title='$($w.Current.Name)'"
            } catch {}
        }
    } catch { Log "  (enum failed: $_)" }
    Log "=== END OPEN WINDOWS ==="
}

function Log-ChildTree([IntPtr]$parent, [string]$indent, [int]$depth) {
    if ($depth -gt 6) { return }
    $child = [Win32]::FindWindowEx($parent, [IntPtr]::Zero, $null, $null)
    while ($child -ne [IntPtr]::Zero) {
        $tLen = [Win32]::GetWindowTextLength($child)
        $tSb  = New-Object System.Text.StringBuilder([Math]::Max($tLen+2, 64))
        [Win32]::GetWindowText($child, $tSb, $tSb.Capacity) | Out-Null
        $cSb  = New-Object System.Text.StringBuilder 64
        [Win32]::GetClassName($child, $cSb, 64) | Out-Null
        $cR   = New-Object Win32+RECT
        [Win32]::GetWindowRect($child,[ref]$cR) | Out-Null
        $vis  = [Win32]::IsWindowVisible($child)
        Log "${indent}hwnd=0x$($child.ToString('X')) vis=$vis cls='$($cSb)' text='$($tSb)' rect=($($cR.Left),$($cR.Top),$($cR.Right),$($cR.Bottom))"
        Log-ChildTree $child ("  " + $indent) ($depth + 1)
        $child = [Win32]::FindWindowEx($parent, $child, $null, $null)
    }
}

function Find-TopWindow([string]$namePattern) {
    try {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $allW = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                    [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($w in $allW) {
            try {
                if ($w.Current.Name -match $namePattern) {
                    $h = [IntPtr]$w.Current.NativeWindowHandle
                    if ($h -ne [IntPtr]::Zero) { return $h }
                }
            } catch {}
        }
    } catch {}
    return [IntPtr]::Zero
}

function Find-ChildByText([IntPtr]$parent, [string]$pattern) {
    $child = [Win32]::FindWindowEx($parent, [IntPtr]::Zero, $null, $null)
    while ($child -ne [IntPtr]::Zero) {
        $len = [Win32]::GetWindowTextLength($child)
        if ($len -gt 0) {
            $sb = New-Object System.Text.StringBuilder($len + 2)
            [Win32]::GetWindowText($child, $sb, $sb.Capacity) | Out-Null
            if ($sb.ToString() -match $pattern) { return $child }
        }
        $found = Find-ChildByText $child $pattern
        if ($found -ne [IntPtr]::Zero) { return $found }
        $child = [Win32]::FindWindowEx($parent, $child, $null, $null)
    }
    return [IntPtr]::Zero
}

function Click-At([int]$x,[int]$y) {
    Log "LEFT-CLICK at ($x,$y)"
    [Win32]::SetCursorPos($x,$y) | Out-Null; Start-Sleep -Milliseconds 200
    [Win32]::mouse_event([Win32]::LEFTDOWN, 0,0,0,0)
    Start-Sleep -Milliseconds 100
    [Win32]::mouse_event([Win32]::LEFTUP,   0,0,0,0)
    Start-Sleep -Milliseconds 300
}

function RightClick-At([int]$x,[int]$y) {
    Log "RIGHT-CLICK at ($x,$y)"
    [Win32]::SetCursorPos($x,$y) | Out-Null; Start-Sleep -Milliseconds 200
    [Win32]::mouse_event([Win32]::RIGHTDOWN, 0,0,0,0)
    Start-Sleep -Milliseconds 100
    [Win32]::mouse_event([Win32]::RIGHTUP,   0,0,0,0)
    Start-Sleep -Milliseconds 700
}

function Take-Screenshot([string]$path) {
    $bmp = New-Object System.Drawing.Bitmap($screenW,$screenH)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen(0,0,0,0,$bmp.Size); $g.Dispose()
    $bmp.Save($path,[System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    Log "Screenshot saved: $(Split-Path $path -Leaf)"
}

function Valid-Rect($b) {
    return (-not [double]::IsInfinity($b.X) -and -not [double]::IsNaN($b.X) `
        -and $b.Width -gt 0 -and $b.Height -gt 0 `
        -and $b.X -gt -5000 -and $b.Y -gt -5000 `
        -and $b.X -lt 9999  -and $b.Y -lt 9999)
}

function Close-Hwnd([IntPtr]$h) {
    if ($h -ne [IntPtr]::Zero) {
        [Win32]::PostMessage($h,[Win32]::WM_CLOSE,0,0) | Out-Null
        Start-Sleep -Milliseconds 400
    }
}

function Xml-Escape([string]$s) { $s -replace '&','&amp;' -replace '<','&lt;' -replace '>','&gt;' }

# Opens the Status dialog for a named adapter, clicks Details, arranges side-by-side, screenshots
function Capture-AdapterStatusDetails([string]$adapterPattern, [string]$imgPath, [string]$imgTitle) {
    Log "--- Capturing [$adapterPattern] Status + Details ---"

    # Restore ncpa and bring to foreground
    [Win32]::ShowWindow($ncpaHwnd, 9) | Out-Null
    [Win32]::SetForegroundWindow($ncpaHwnd) | Out-Null
    Start-Sleep -Milliseconds 600

    $ncpaElem = [System.Windows.Automation.AutomationElement]::FromHandle($ncpaHwnd)
    $liCond   = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::ListItem)
    $allItems = $ncpaElem.FindAll([System.Windows.Automation.TreeScope]::Descendants, $liCond)

    $adapterItem = $null
    foreach ($it in $allItems) {
        if ($it.Current.Name -like "*$adapterPattern*") { $adapterItem = $it; break }
    }
    if (-not $adapterItem) { Log "WARNING: Adapter '$adapterPattern' not found - skipping"; return }

    $pcx = 0; $pcy = 0
    $b = $adapterItem.Current.BoundingRectangle
    if (Valid-Rect $b) {
        $pcx = [int]($b.X + $b.Width/2); $pcy = [int]($b.Y + $b.Height/2)
        Log "$adapterPattern at ($pcx,$pcy) - selecting..."
        Click-At $pcx $pcy
        Start-Sleep -Milliseconds 600
    }

    $statusOpenClicked = $false

    # Approach A: toolbar "View status of this connection" button
    try {
        $btnCond2 = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)
        $tbBtns = $ncpaElem.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond2)
        foreach ($tb in $tbBtns) {
            if ($tb.Current.Name -match "(?i)status") {
                $br = $tb.Current.BoundingRectangle
                if (Valid-Rect $br) {
                    $tx = [int]($br.X + $br.Width/2); $ty = [int]($br.Y + $br.Height/2)
                    Log "Toolbar 'View status' at ($tx,$ty) - clicking"
                    Click-At $tx $ty
                    $statusOpenClicked = $true; break
                }
            }
        }
    } catch { Log "Toolbar UIA failed: $_" }

    # Approach B: right-click → DOWN DOWN ENTER
    if (-not $statusOpenClicked) {
        Log "Toolbar click failed - right-click + keyboard nav"
        if ($pcx -gt 0) {
            RightClick-At $pcx $pcy
        } else {
            $r = New-Object Win32+RECT; [Win32]::GetWindowRect($ncpaHwnd,[ref]$r)
            $pcx = [int](($r.Left+$r.Right)/2); $pcy = [int](($r.Top+$r.Bottom)/2)
            Click-At $pcx $pcy; Start-Sleep -Milliseconds 400
            RightClick-At $pcx $pcy
        }
        Start-Sleep -Milliseconds 900
        [System.Windows.Forms.SendKeys]::SendWait("{DOWN}"); Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("{DOWN}"); Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    }
    Start-Sleep -Seconds 2

    # Find Status dialog
    $statusHwnd = Find-TopWindow "(?i)^.*(status).*$"
    if ($statusHwnd -ne [IntPtr]::Zero) {
        $clsSb = New-Object System.Text.StringBuilder 64
        [Win32]::GetClassName($statusHwnd, $clsSb, 64) | Out-Null
        if ($clsSb.ToString() -ne "#32770") { $statusHwnd = [IntPtr]::Zero }
    }
    if ($statusHwnd -eq [IntPtr]::Zero) {
        try {
            $rootF = [System.Windows.Automation.AutomationElement]::RootElement
            $allWF = $rootF.FindAll([System.Windows.Automation.TreeScope]::Children,
                         [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($wF in $allWF) {
                try {
                    $hF  = [IntPtr]$wF.Current.NativeWindowHandle
                    $sbF = New-Object System.Text.StringBuilder 64
                    [Win32]::GetClassName($hF, $sbF, 64) | Out-Null
                    if ($sbF.ToString() -eq "#32770" -and $wF.Current.Name -match "(?i)status") {
                        $statusHwnd = $hF
                        Log "Status dialog (fallback): '$($wF.Current.Name)' hwnd=$hF"
                        break
                    }
                } catch {}
            }
        } catch {}
    }
    if ($statusHwnd -eq [IntPtr]::Zero) {
        Log "WARNING: Status dialog not found for $adapterPattern - skipping"
        return
    }
    Log "Status dialog hwnd=$statusHwnd"
    [Win32]::ShowWindow($statusHwnd, 5) | Out-Null
    [Win32]::SetForegroundWindow($statusHwnd) | Out-Null
    Start-Sleep -Milliseconds 600

    # Click Details button
    $detailsClicked = $false
    try {
        $statusElem = [System.Windows.Automation.AutomationElement]::FromHandle($statusHwnd)
        $btnCond    = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)
        $allBtns = $statusElem.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
        foreach ($btn in $allBtns) {
            if ($btn.Current.Name -match "(?i)details") {
                $br = $btn.Current.BoundingRectangle
                if (Valid-Rect $br) {
                    $bx = [int]($br.X + $br.Width/2); $by = [int]($br.Y + $br.Height/2)
                    Log "Clicking Details (UIA) at ($bx,$by)"
                    Click-At $bx $by
                    $detailsClicked = $true; break
                }
            }
        }
    } catch { Log "UIA for Details failed: $_" }

    if (-not $detailsClicked) {
        $btnHwnd = Find-ChildByText $statusHwnd "(?i)details"
        if ($btnHwnd -ne [IntPtr]::Zero) {
            $btnRect = New-Object Win32+RECT
            [Win32]::GetWindowRect($btnHwnd, [ref]$btnRect) | Out-Null
            $bx = [int](($btnRect.Left + $btnRect.Right)/2)
            $by = [int](($btnRect.Top  + $btnRect.Bottom)/2)
            Log "Details button (Win32) at ($bx,$by)"
            Click-At $bx $by
            $detailsClicked = $true
        }
    }
    if (-not $detailsClicked) {
        $r2 = New-Object Win32+RECT; [Win32]::GetWindowRect($statusHwnd,[ref]$r2)
        $dlgW = $r2.Right - $r2.Left; $dlgH = $r2.Bottom - $r2.Top
        $dxC = $r2.Left + [int]($dlgW * 0.18); $dyC = $r2.Top + [int]($dlgH * 0.44)
        Log "Proportional fallback: Details at ($dxC,$dyC)"
        Click-At $dxC $dyC
    }
    Start-Sleep -Seconds 2

    # Find Details dialog
    $detailHwnd = Find-TopWindow "(?i)connection.?details|details.*connection|פרטי חיבור"
    if ($detailHwnd -ne [IntPtr]::Zero) {
        Log "Details dialog hwnd=$detailHwnd"
    } else {
        Log "WARNING: Details dialog not found"
    }

    # Restore ncpa as background, bring both dialogs to foreground
    if ($ncpaHwnd -ne [IntPtr]::Zero) {
        [Win32]::ShowWindow($ncpaHwnd, 3) | Out-Null
        Start-Sleep -Milliseconds 200
    }
    if ($statusHwnd -ne [IntPtr]::Zero) { [Win32]::ShowWindow($statusHwnd, 9) | Out-Null }
    if ($detailHwnd -ne [IntPtr]::Zero) { [Win32]::ShowWindow($detailHwnd, 9) | Out-Null }
    Start-Sleep -Milliseconds 200

    # Measure sizes and place side by side
    $sW = 290; $sH = 350; $dW = 340; $dH = 290
    $srR = New-Object Win32+RECT
    if ($statusHwnd -ne [IntPtr]::Zero) {
        [Win32]::GetWindowRect($statusHwnd, [ref]$srR) | Out-Null
        if (($srR.Right - $srR.Left) -gt 20) { $sW = $srR.Right - $srR.Left; $sH = $srR.Bottom - $srR.Top }
    }
    $drR = New-Object Win32+RECT
    if ($detailHwnd -ne [IntPtr]::Zero) {
        [Win32]::GetWindowRect($detailHwnd, [ref]$drR) | Out-Null
        if (($drR.Right - $drR.Left) -gt 20) { $dW = $drR.Right - $drR.Left; $dH = $drR.Bottom - $drR.Top }
    }
    $lX  = 80
    $tY  = [int](($screenH - [Math]::Max($sH, $dH)) / 2); if ($tY -lt 40) { $tY = 40 }
    if ($statusHwnd -ne [IntPtr]::Zero) {
        [Win32]::MoveWindow($statusHwnd, $lX, $tY, $sW, $sH, $true) | Out-Null
        [Win32]::SetForegroundWindow($statusHwnd) | Out-Null
    }
    if ($detailHwnd -ne [IntPtr]::Zero) {
        [Win32]::MoveWindow($detailHwnd, ($lX + $sW + 20), $tY, $dW, $dH, $true) | Out-Null
        [Win32]::SetForegroundWindow($detailHwnd) | Out-Null
    }
    Start-Sleep -Milliseconds 600

    Take-Screenshot $imgPath
    $images.Add(@{ Path=$imgPath; Title=$imgTitle })
    Log "Screenshot saved: $imgTitle"

    Close-Hwnd $detailHwnd
    Close-Hwnd $statusHwnd
}

$images = [System.Collections.Generic.List[hashtable]]::new()
Log "=== START: Hostname=$hostname ==="

# ============================================================
# SECTION 1: Server Manager -> Local Server
# ============================================================
Log "--- [1] Server Manager - Local Server ---"
$smProc = Start-Process ServerManager -PassThru
Log "Waiting 10s for Server Manager..."
Start-Sleep -Seconds 10

$hwnd = $smProc.MainWindowHandle
[Win32]::ShowWindow($hwnd,3) | Out-Null
[Win32]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 800

$rect = New-Object Win32+RECT
[Win32]::GetWindowRect($hwnd,[ref]$rect)
$cx = $rect.Left + 90
$cy = $rect.Top  + 145
Log "Clicking Local Server at ($cx,$cy)"
Click-At $cx $cy
Start-Sleep -Seconds 3

$img1 = "$env:TEMP\img1_localserver.png"
Take-Screenshot $img1
$images.Add(@{ Path=$img1; Title="1. Server Manager - Local Server" })

$smProc.CloseMainWindow() | Out-Null; Start-Sleep -Seconds 1
if (-not $smProc.HasExited) { $smProc.Kill() }
Log "Server Manager closed"

# ============================================================
# SECTION 2a: Open ncpa.cpl and screenshot all adapters
# ============================================================
Log "--- [2] Network Connections - All Adapters ---"

# Open via CLSID (language-independent)
Start-Process "explorer.exe" -ArgumentList "shell:::{7007ACC7-3202-11D1-AAD2-00805FC1270E}"
Log "Launched Network Connections. Searching for window..."
Start-Sleep -Seconds 2

$ncpaHwnd = [IntPtr]::Zero
$shellCom = New-Object -ComObject Shell.Application

for ($i = 0; $i -lt 25 -and $ncpaHwnd -eq [IntPtr]::Zero; $i++) {
    Start-Sleep -Seconds 1
    try {
        $wins = $shellCom.Windows()
        for ($j = 0; $j -lt $wins.Count; $j++) {
            try {
                $w    = $wins.Item($j)
                $url  = [string]$w.LocationURL
                $name = [string]$w.LocationName
                if ($url -like "*7007ACC7*" -or $url -like "*ncpa*" -or
                    $name -like "*Connection*" -or $name -like "*Network*" -or
                    $name -like "*רשת*"        -or $name -like "*חיבור*") {
                    $ncpaHwnd = [IntPtr]$w.HWND
                    Log "Shell.Application: '$name' (hwnd=$ncpaHwnd)"
                    break
                }
            } catch {}
        }
    } catch {}

    if ($ncpaHwnd -eq [IntPtr]::Zero) {
        foreach ($t in @("Network Connections","Network Connection","חיבורי רשת","Сетевые подключения")) {
            $h = [Win32]::FindWindow($null,$t)
            if ($h -ne [IntPtr]::Zero) { $ncpaHwnd = $h; Log "FindWindow: '$t'"; break }
        }
    }
}

# Fallback: control.exe
if ($ncpaHwnd -eq [IntPtr]::Zero) {
    Log "CLSID failed - trying control.exe ncpa.cpl..."
    Start-Process "control.exe" -ArgumentList "ncpa.cpl"
    for ($i = 0; $i -lt 15 -and $ncpaHwnd -eq [IntPtr]::Zero; $i++) {
        Start-Sleep -Seconds 1
        foreach ($t in @("Network Connections","חיבורי רשת","Network Connection")) {
            $h = [Win32]::FindWindow($null,$t)
            if ($h -ne [IntPtr]::Zero) { $ncpaHwnd = $h; Log "FindWindow: '$t'"; break }
        }
    }
}

if ($ncpaHwnd -eq [IntPtr]::Zero) {
    Log "ERROR: Could not find Network Connections window - skipping section 2"
} else {
    Log "Found Network Connections (hwnd=$ncpaHwnd)"

    # Screenshot 2 — all adapters (maximized)
    [Win32]::ShowWindow($ncpaHwnd,3) | Out-Null
    [Win32]::SetForegroundWindow($ncpaHwnd) | Out-Null
    Start-Sleep -Milliseconds 800

    $img2 = "$env:TEMP\img2_netoverview.png"
    Take-Screenshot $img2
    $images.Add(@{ Path=$img2; Title="2. Network Connections - All Adapters" })

    # ============================================================
    # SECTION 2b: Right-click Production -> Status
    # ============================================================
    # Screenshot 3 + 4: Status + Details for each adapter
    # ============================================================
    Capture-AdapterStatusDetails "Production" "$env:TEMP\img3_prod.png"  "3. Network - Production Status and Details"
    Capture-AdapterStatusDetails "Delivery"   "$env:TEMP\img4_deliv.png" "4. Network - Delivery Status and Details"

    Close-Hwnd $ncpaHwnd
    Log "Network Connections closed"
}

# ============================================================
# SECTION 3: Disk Management
# ============================================================
Log "--- [5] Disk Management ---"
Start-Process "diskmgmt.msc"
Log "Waiting for Disk Management..."
Start-Sleep -Seconds 4

$diskHwnd = [IntPtr]::Zero
for ($i = 0; $i -lt 15 -and $diskHwnd -eq [IntPtr]::Zero; $i++) {
    Start-Sleep -Seconds 1
    $diskHwnd = Find-TopWindow "(?i)disk management|ניהול דיסקים|computer management"
    if ($diskHwnd -eq [IntPtr]::Zero) {
        try {
            $root2 = [System.Windows.Automation.AutomationElement]::RootElement
            $allW2 = $root2.FindAll([System.Windows.Automation.TreeScope]::Children,
                         [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($w2 in $allW2) {
                try {
                    $h2 = [IntPtr]$w2.Current.NativeWindowHandle
                    $cls2 = New-Object System.Text.StringBuilder 64
                    [Win32]::GetClassName($h2, $cls2, 64) | Out-Null
                    if ($cls2.ToString() -eq "MMCMainFrame") {
                        $diskHwnd = $h2
                        Log "MMCMainFrame: '$($w2.Current.Name)' hwnd=$h2"
                        break
                    }
                } catch {}
            }
        } catch {}
    }
}

if ($diskHwnd -eq [IntPtr]::Zero) {
    Log "WARNING: Disk Management window not found - skipping"
} else {
    Log "Disk Management hwnd=$diskHwnd"
    [Win32]::ShowWindow($diskHwnd, 3) | Out-Null
    [Win32]::SetForegroundWindow($diskHwnd) | Out-Null
    Start-Sleep -Milliseconds 1500

    $img5 = "$env:TEMP\img5_diskmgmt.png"
    Take-Screenshot $img5
    $images.Add(@{ Path=$img5; Title="5. Disk Management" })

    Close-Hwnd $diskHwnd
    Log "Disk Management closed"
}

# ============================================================
# SECTION 4: Windows Activation (Settings app)
# ============================================================
Log "--- [6] Windows Activation ---"
Start-Process "ms-settings:activation"
Log "Waiting for Settings app..."
Start-Sleep -Seconds 5

$settingsHwnd = [IntPtr]::Zero
for ($i = 0; $i -lt 15 -and $settingsHwnd -eq [IntPtr]::Zero; $i++) {
    Start-Sleep -Seconds 1
    # Settings app is an ApplicationFrameWindow titled "Settings" (or localized)
    try {
        $root3 = [System.Windows.Automation.AutomationElement]::RootElement
        $allW3 = $root3.FindAll([System.Windows.Automation.TreeScope]::Children,
                     [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($w3 in $allW3) {
            try {
                $h3 = [IntPtr]$w3.Current.NativeWindowHandle
                if ($h3 -eq [IntPtr]::Zero -or -not [Win32]::IsWindowVisible($h3)) { continue }
                $cls3 = New-Object System.Text.StringBuilder 64
                [Win32]::GetClassName($h3, $cls3, 64) | Out-Null
                if ($cls3.ToString() -eq "ApplicationFrameWindow" -and
                    $w3.Current.Name -match "(?i)settings|הגדרות") {
                    $settingsHwnd = $h3
                    Log "Settings window: '$($w3.Current.Name)' hwnd=$h3"
                    break
                }
            } catch {}
        }
    } catch {}
}

if ($settingsHwnd -eq [IntPtr]::Zero) {
    Log "WARNING: Settings window not found - skipping Activation"
} else {
    Log "Settings hwnd=$settingsHwnd - maximizing"
    [Win32]::ShowWindow($settingsHwnd, 3) | Out-Null      # SW_MAXIMIZE
    [Win32]::SetForegroundWindow($settingsHwnd) | Out-Null
    Start-Sleep -Seconds 3   # let the Activation page fully render

    $img6 = "$env:TEMP\img6_activation.png"
    Take-Screenshot $img6
    $images.Add(@{ Path=$img6; Title="6. Windows Activation" })

    Close-Hwnd $settingsHwnd
    Log "Settings closed"
}

# ============================================================
# BUILD DOCX  (with real Word Heading1 style)
# ============================================================
Log "--- Building DOCX ($($images.Count) images) ---"
$tempDir = "$env:TEMP\docx_$(Get-Random)"
New-Item -ItemType Directory -Path "$tempDir\word\media" -Force | Out-Null
New-Item -ItemType Directory -Path "$tempDir\_rels"       -Force | Out-Null
New-Item -ItemType Directory -Path "$tempDir\word\_rels"  -Force | Out-Null

# --- styles.xml: defines Title + Heading1 ---
[System.IO.File]::WriteAllText("$tempDir\word\styles.xml", @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:jc w:val="center"/>
      <w:spacing w:before="400" w:after="200"/>
    </w:pPr>
    <w:rPr>
      <w:b/><w:bCs/>
      <w:color w:val="2E4057"/>
      <w:sz w:val="56"/><w:szCs w:val="56"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="240" w:after="60"/>
      <w:outlineLvl w:val="0"/>
    </w:pPr>
    <w:rPr>
      <w:b/><w:bCs/>
      <w:color w:val="1F3864"/>
      <w:sz w:val="36"/><w:szCs w:val="36"/>
    </w:rPr>
  </w:style>
</w:styles>
'@, [System.Text.Encoding]::UTF8)

$body = [System.Text.StringBuilder]::new()
$rels = [System.Text.StringBuilder]::new()
# styles relationship comes first (rId0), images start at rId1
[void]$rels.AppendLine('  <Relationship Id="rId0" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')

# First line: hostname as Heading1
$hostnameXml = Xml-Escape "Hostname: $hostname"
[void]$body.AppendLine(@"
<w:p>
  <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
  <w:r><w:t xml:space="preserve">$hostnameXml</w:t></w:r>
</w:p>
<w:p><w:pPr><w:spacing w:after="0"/></w:pPr></w:p>
"@)

for ($i = 0; $i -lt $images.Count; $i++) {
    $item    = $images[$i]
    $rId     = "rId$($i+1)"
    $imgFile = "image$($i+1).png"
    Copy-Item $item.Path "$tempDir\word\media\$imgFile" -Force

    [void]$rels.AppendLine("  <Relationship Id=`"$rId`" Type=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image`" Target=`"media/$imgFile`"/>")

    if ($i -gt 0) { [void]$body.AppendLine('<w:p><w:r><w:br w:type="page"/></w:r></w:p>') }

    $titleXml = Xml-Escape $item.Title
    [void]$body.AppendLine(@"
<w:p>
  <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
  <w:r><w:t xml:space="preserve">$titleXml</w:t></w:r>
</w:p>
<w:p>
  <w:r>
    <w:drawing>
      <wp:inline distT="0" distB="0" distL="0" distR="0"
                 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
        <wp:extent cx="5943600" cy="3341025"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:docPr id="$($i+1)" name="$imgFile"/>
        <wp:cNvGraphicFramePr/>
        <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:nvPicPr>
                <pic:cNvPr id="$($i*10)" name="$imgFile"/>
                <pic:cNvPicPr/>
              </pic:nvPicPr>
              <pic:blipFill>
                <a:blip r:embed="$rId" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
                <a:stretch><a:fillRect/></a:stretch>
              </pic:blipFill>
              <pic:spPr>
                <a:xfrm><a:off x="0" y="0"/><a:ext cx="5943600" cy="3341025"/></a:xfrm>
                <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
              </pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
"@)
}

[System.IO.File]::WriteAllText("$tempDir\word\document.xml", @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
$($body.ToString())
  </w:body>
</w:document>
"@, [System.Text.Encoding]::UTF8)

[System.IO.File]::WriteAllText("$tempDir\word\_rels\document.xml.rels", @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
$($rels.ToString())
</Relationships>
"@, [System.Text.Encoding]::UTF8)

[System.IO.File]::WriteAllText("$tempDir\_rels\.rels", @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"@, [System.Text.Encoding]::UTF8)

[System.IO.File]::WriteAllText("$tempDir\[Content_Types].xml", @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml"  ContentType="application/xml"/>
  <Default Extension="png"  ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"   ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"@, [System.Text.Encoding]::UTF8)

$zipPath = $OutputDocx -replace '\.docx$','.zip'
if (Test-Path $OutputDocx) { Remove-Item $OutputDocx -Force }
if (Test-Path $zipPath)    { Remove-Item $zipPath    -Force }

Compress-Archive -Path "$tempDir\*" -DestinationPath $zipPath -Force
Rename-Item -Path $zipPath -NewName (Split-Path $OutputDocx -Leaf) -Force
Remove-Item $tempDir -Recurse -Force

$images | ForEach-Object { if (Test-Path $_.Path) { Remove-Item $_.Path -Force } }

Log "=== DONE. DOCX: $OutputDocx ==="

# Restore and bring forward the PowerShell console
[Win32]::ShowWindow($consoleHwnd, 9) | Out-Null    # SW_RESTORE
[Win32]::SetForegroundWindow($consoleHwnd) | Out-Null

if (-not [Console]::IsInputRedirected) {
    Write-Host "`nPress Enter to exit..." -ForegroundColor Yellow
    Read-Host
}
