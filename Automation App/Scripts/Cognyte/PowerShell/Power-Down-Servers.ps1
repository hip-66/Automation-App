<#
.SYNOPSIS
  Power down one or more Dell servers by iDRAC IP, using
  "racadm serveraction powerdown".

.DESCRIPTION
  Targets come from addresses.txt, which PS Automation writes from the IP list
  OR the range (start + count) you choose in the form - either way this script
  just reads one IP per line. For manual double-click use it falls back to
  prompting for a single IP.

  iDRAC credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the
  app's UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's
  encrypted .env default) is used; a standalone run with neither set prompts
  instead - never hardcoded in this file.
#>

$ErrorActionPreference = 'Continue'

$IdracUser = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_USERNAME) { $env:PSAUTO_DEFAULT_USERNAME } else { Read-Host "iDRAC username" }
$IdracPass = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $env:PSAUTO_DEFAULT_PASSWORD } else { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "iDRAC password" -AsSecureString))) }

Write-Host "=== Power Down Servers (racadm serveraction powerdown) ===" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AddressesFile = Join-Path $ScriptDir "addresses.txt"
$NonInteractive = [Console]::IsInputRedirected

# --- Resolve target IP(s) ---------------------------------------------------
$Servers = @()
if (Test-Path $AddressesFile) {
    $Servers = Get-Content $AddressesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
}
if (-not $Servers -or $Servers.Count -eq 0) {
    if ($NonInteractive) {
        Write-Host "ERROR: No target IP provided (addresses.txt is empty)." -ForegroundColor Red
        exit 1
    }
    $ip = Read-Host "Enter iDRAC IP to power down"
    if ([string]::IsNullOrWhiteSpace($ip)) {
        Write-Host "ERROR: No IP entered." -ForegroundColor Red
        exit 1
    }
    $Servers = @($ip)
}

# --- Resolve racadm.exe -----------------------------------------------------
$Racadm = "racadm"
if (-not (Get-Command $Racadm -ErrorAction SilentlyContinue)) {
    $common = @(
        "C:\Program Files\Dell\SysMgt\rac5\racadm.exe",
        "C:\Program Files\Dell\SysMgt\iDRAC\racadm.exe",
        "C:\Program Files (x86)\Dell\SysMgt\rac5\racadm.exe"
    )
    $found = $common | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) {
        $Racadm = $found
    }
    else {
        Write-Host "ERROR: racadm.exe not found in PATH or standard Dell install locations." -ForegroundColor Red
        Write-Host "Install Dell OpenManage / racadm, or add racadm.exe to PATH." -ForegroundColor Yellow
        if (-not $NonInteractive) { Read-Host "Press ENTER to exit" }
        exit 1
    }
}

Write-Host "racadm       : $Racadm"
Write-Host "iDRAC user   : $IdracUser"
Write-Host "Target count : $($Servers.Count)"
Write-Host "Targets      : $($Servers -join ', ')"
Write-Host ""

$ok = 0
$fail = 0
foreach ($ip in $Servers) {
    Write-Host "--------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Powering down $ip ..." -ForegroundColor Cyan
    try {
        & $Racadm -r $ip -u $IdracUser -p $IdracPass --nocertwarn serveraction powerdown
        $code = $LASTEXITCODE
        if ($code -eq 0) {
            Write-Host "OK: power down command sent to $ip." -ForegroundColor Green
            $ok++
        }
        else {
            Write-Host "ERROR: racadm exited with code $code for $ip." -ForegroundColor Red
            $fail++
        }
    }
    catch {
        Write-Host "ERROR on $ip : $($_.Exception.Message)" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Done. Success: $ok, Failed: $fail, Total: $($Servers.Count)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

if (-not $NonInteractive) { Read-Host "Press ENTER to exit" }

# Non-zero exit only if EVERY target failed (partial success is still success
# for the run as a whole).
if ($fail -gt 0 -and $ok -eq 0) { exit 1 }
exit 0
