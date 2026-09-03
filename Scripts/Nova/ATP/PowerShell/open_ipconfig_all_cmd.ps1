<#
.SYNOPSIS
    OPEN_CONFIGALL

.DESCRIPTION
    Opens CMD on right side of screen,
    runs ipconfig /all,
    temporarily reduces font size,
    scrolls to top,
    then restores original console settings.
#>

$ErrorActionPreference = "Continue"

Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class WinAPI {

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(
        IntPtr hWnd,
        IntPtr hWndInsertAfter,
        int X,
        int Y,
        int cx,
        int cy,
        uint uFlags
    );

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(
        uint dwFlags,
        int dx,
        int dy,
        int dwData,
        int dwExtraInfo
    );

    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(
        IntPtr hWnd,
        StringBuilder lpString,
        int nMaxCount
    );

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(
        EnumWindowsProc lpEnumFunc,
        IntPtr lParam
    );

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll")]
    public static extern bool SystemParametersInfo(
        uint uAction,
        uint uParam,
        ref RECT pvParam,
        uint fWinIni
    );

    public static IntPtr FindWindowBySubstring(string substring) {
        IntPtr found = IntPtr.Zero;

        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            if (!IsWindowVisible(hWnd)) return true;

            int len = GetWindowTextLength(hWnd);

            if (len > 0) {
                StringBuilder sb = new StringBuilder(len + 1);
                GetWindowText(hWnd, sb, sb.Capacity);

                if (sb.ToString().Contains(substring)) {
                    found = hWnd;
                    return false;
                }
            }

            return true;
        }, IntPtr.Zero);

        return found;
    }

    public static void GetWorkArea(out int w, out int h) {
        RECT r = new RECT();
        SystemParametersInfo(48, 0, ref r, 0);

        w = r.Right - r.Left;
        h = r.Bottom - r.Top;
    }
}
"@

# ============================
# Save original font
# ============================

$ConsoleReg = "HKCU:\Console"

$OriginalFontSize = $null

try {
    $OriginalFontSize = (Get-ItemProperty -Path $ConsoleReg -Name FontSize -ErrorAction SilentlyContinue).FontSize
} catch {}

# ============================
# Temporary font
# ============================

function Set-TempFont {
    if (-not (Test-Path $ConsoleReg)) {
        New-Item -Path $ConsoleReg -Force | Out-Null
    }

    # Slightly bigger than 0x00090000
    Set-ItemProperty -Path $ConsoleReg -Name FontSize -Value 0x00090000
}

# ============================
# Restore font
# ============================

function Restore-Font {
    if ($null -ne $OriginalFontSize) {
        Set-ItemProperty -Path $ConsoleReg -Name FontSize -Value $OriginalFontSize
    }
    else {
        Remove-ItemProperty -Path $ConsoleReg -Name FontSize -ErrorAction SilentlyContinue
    }
}

# ============================
# Main
# ============================

function Open-ConfigAll {

    try {

        Write-Host "Starting OPEN_CONFIGALL..." -ForegroundColor Cyan

        Set-TempFont

        $screenW = 0
        $screenH = 0

        [WinAPI]::GetWorkArea([ref]$screenW, [ref]$screenH)

        $cmdW = [int]($screenW / 3)
        $cmdX = $screenW - $cmdW

        $uniqueTitle = "OPEN_CONFIGALL_$([int](Get-Date -UFormat %s))"

        Start-Process cmd.exe -ArgumentList "/K", "title $uniqueTitle & ipconfig /all"

        Start-Sleep -Seconds 3

        $hwnd = [WinAPI]::FindWindowBySubstring($uniqueTitle)

        if ($hwnd -eq [IntPtr]::Zero) {
            Write-Host "CMD window not found." -ForegroundColor Red
            return
        }

        [WinAPI]::ShowWindow($hwnd, 9) | Out-Null
        Start-Sleep -Milliseconds 500

        [WinAPI]::SetWindowPos(
            $hwnd,
            [IntPtr]::Zero,
            $cmdX,
            0,
            $cmdW,
            $screenH,
            0x0040
        ) | Out-Null

        Start-Sleep -Milliseconds 500

        [WinAPI]::SetForegroundWindow($hwnd) | Out-Null
        Start-Sleep -Milliseconds 500

        $clickX = [int]($cmdX + ($cmdW / 2))
        $clickY = [int]($screenH / 2)

        [WinAPI]::SetCursorPos($clickX, $clickY)

        [WinAPI]::mouse_event(0x0002,0,0,0,0)
        [WinAPI]::mouse_event(0x0004,0,0,0,0)

        Start-Sleep -Milliseconds 500

        # Scroll to top
        for ($i = 0; $i -lt 20; $i++) {
            [WinAPI]::mouse_event(0x0800,0,0,600,0)
            Start-Sleep -Milliseconds 50
        }

        Write-Host "OPEN_CONFIGALL completed." -ForegroundColor Green
    }
    finally {
        Restore-Font
    }
}

Open-ConfigAll