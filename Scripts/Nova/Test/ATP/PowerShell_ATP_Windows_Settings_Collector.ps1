<#
.SYNOPSIS
    NovaHUB Windows Settings Collector
.DESCRIPTION
    1. Opens CMD on the right (35%), scrolls up.
    2. Opens Windows settings one-by-one on the left (65%).
    3. Takes screenshot, closes the window, and moves to next.
    4. Saves all into a partial DOCX for later merging.
#>

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

# ======================================================================
# 1. CONFIGURATION & INPUTS
# ======================================================================
$ServerName = (Read-Host "Enter Server Name (e.g. FM1, APP1)").ToUpper().Trim()
$WorkingDir = $PSScriptRoot
$TempImgDir = Join-Path $WorkingDir "temp_caps_$ServerName"
$ReportFile = Join-Path $WorkingDir "Partial_$($ServerName).docx"

if (Test-Path $TempImgDir) { Remove-Item $TempImgDir -Recurse -Force }
New-Item -ItemType Directory -Path $TempImgDir | Out-Null

# ======================================================================
# 2. WIN API FOR WINDOW CONTROL
# ======================================================================
$WinAPI = Add-Type -MemberDefinition @"
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, int dwExtraInfo);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);
    
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    public static IntPtr FindWindowBySubstring(string sub) {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            System.Text.StringBuilder sb = new System.Text.StringBuilder(256);
            GetWindowText(hWnd, sb, 256);
            if (sb.ToString().Contains(sub)) { found = hWnd; return false; }
            return true;
        }, IntPtr.Zero);
        return found;
    }
"@ -Name "WinAPI" -PassThru

# ======================================================================
# 3. HELPER FUNCTIONS
# ======================================================================

function Setup-SideCMD {
    $title = "ATP_MONITOR_$ServerName"
    Start-Process cmd.exe -ArgumentList "/K", "title $title & ipconfig /all"
    Start-Sleep -Seconds 2
    $hwnd = [WinAPI]::FindWindowBySubstring($title)
    if ($hwnd -ne [IntPtr]::Zero) {
        $sw = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Width
        $sh = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Height
        $cmdW = [int]($sw * 0.35)
        $cmdX = $sw - $cmdW
        
        [WinAPI]::ShowWindow($hwnd, 9)
        [WinAPI]::SetWindowPos($hwnd, [IntPtr]::Zero, $cmdX, 0, $cmdW, $sh, 0x0040)
        [WinAPI]::SetForegroundWindow($hwnd) | Out-Null
        
        # Scroll to top
        Start-Sleep -Milliseconds 500
        for($i=0; $i -lt 15; $i++) {
            [WinAPI]::mouse_event(0x0800, 0, 0, (120*5), 0)
            Start-Sleep -Milliseconds 50
        }
    }
    return $title
}

function Align-And-Capture {
    param($ProcessName, $FileName)
    Start-Sleep -Seconds 3
    $proc = Get-Process $ProcessName -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
    if ($proc) {
        $hwnd = $proc.MainWindowHandle
        $sw = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Width
        $sh = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Height
        $targetW = [int]($sw * 0.65)
        
        [WinAPI]::ShowWindow($hwnd, 9)
        [WinAPI]::SetWindowPos($hwnd, [IntPtr]::Zero, 0, 0, $targetW, $sh, 0x0040)
        [WinAPI]::SetForegroundWindow($hwnd) | Out-Null
        Start-Sleep -Seconds 1
    }
    
    # Capture Screenshot
    $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
    $path = Join-Path $TempImgDir "$FileName.png"
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    return $path
}

# ======================================================================
# 4. MAIN WORKFLOW
# ======================================================================

$CmdTitle = Setup-SideCMD
$CapturedItems = [System.Collections.Generic.List[PSObject]]::new()

$TaskFlow = @(
    @{ Name = "Windows_Version"; Cmd = "winver"; Proc = "winver" },
    @{ Name = "Power_Settings";  Cmd = "control.exe powercfg.cpl"; Proc = "control" },
    @{ Name = "UAC_Settings";    Cmd = "UserAccountControlSettings.exe"; Proc = "UserAccountControlSettings" },
    @{ Name = "Device_Manager";  Cmd = "devmgmt.msc"; Proc = "mmc" },
    @{ Name = "Disk_Management"; Cmd = "diskmgmt.msc"; Proc = "mmc" }
)

foreach ($task in $TaskFlow) {
    Write-Host "Capturing: $($task.Name)" -ForegroundColor Yellow
    $p = Start-Process $task.Cmd -PassThru
    $img = Align-And-Capture -ProcessName $task.Proc -FileName $task.Name
    $CapturedItems.Add(@{ Title = $task.Name; Path = $img })
    
    # Close the window before next task
    Stop-Process -Name $task.Proc -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

# Cleanup CMD
& cmd.exe /c "taskkill /F /FI `"WINDOWTITLE eq $CmdTitle*`" >nul 2>&1"

# ======================================================================
# 5. PACKAGING INTO DOCX (XML-ZIP METHOD)
# ======================================================================
Write-Host "Building Partial DOCX..." -ForegroundColor Green

$zipStream = [System.IO.File]::Create($ReportFile)
$zip = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)

# Minimalistic word/document.xml with images placeholders
$docXml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
$docXml += "<w:p><w:r><w:t>ATP REPORT - SERVER $ServerName</w:t></w:r></w:p>"

foreach ($item in $CapturedItems) {
    $docXml += "<w:p><w:r><w:t>$($item.Title)</w:t></w:r></w:p>"
    # In the final aggregator we will insert the actual <w:drawing> tags. 
    # For now, we store filenames in a way the aggregator can find.
}
$docXml += '</w:body></w:document>'

# Add required files
function Add-TextEntry($z, $name, $content) {
    $e = $z.CreateEntry($name)
    $sw = New-Object System.IO.StreamWriter($e.Open())
    $sw.Write($content)
    $sw.Dispose()
}

Add-TextEntry $zip "word/document.xml" $docXml
Add-TextEntry $zip "[Content_Types].xml" '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'

# Add Media folder
foreach ($item in $CapturedItems) {
    $imgEntry = $zip.CreateEntry("word/media/$($item.Title).png")
    $imgStream = $imgEntry.Open()
    $bytes = [System.IO.File]::ReadAllBytes($item.Path)
    $imgStream.Write($bytes, 0, $bytes.Length)
    $imgStream.Dispose()
}

$zip.Dispose(); $zipStream.Dispose()
Remove-Item $TempImgDir -Recurse -Force
Write-Host "Success! Partial report for $ServerName saved to $ReportFile" -ForegroundColor Cyan