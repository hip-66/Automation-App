<#
.SYNOPSIS
  Rename one or more Dell iDRACs by setting iDRAC.NIC.DNSRacName from a parallel
  list of IPs and hostnames.

.DESCRIPTION
  Reads addresses.txt (IPs) and hostnames.txt (names), both written by PS
  Automation from the two lists you type in the form. Line N in hostnames.txt
  is applied to line N in addresses.txt (first name -> first IP, and so on), so
  you can rename many servers in one run.

  This sets the iDRAC DNS name (iDRAC.NIC.DNSRacName) - the name that shows up
  as the iDRAC hostname (e.g. in the iDRAC report). iDRAC credentials use the
  app's configured default ($IdracUser / $IdracPass are resolved below - never
  hardcoded in this file).
#>

$ErrorActionPreference = 'Continue'

# Never hardcoded: PSAUTO_USERNAME/PASSWORD (explicit override from the app's
# UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted
# .env default) is used; a standalone run with neither set prompts instead.
$IdracUser = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_USERNAME) { $env:PSAUTO_DEFAULT_USERNAME } else { Read-Host "iDRAC username" }
$IdracPass = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $env:PSAUTO_DEFAULT_PASSWORD } else { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "iDRAC password" -AsSecureString))) }

Write-Host "=== Set iDRAC Hostname (iDRAC.NIC.DNSRacName) ===" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AddressesFile = Join-Path $ScriptDir "addresses.txt"
$HostnamesFile = Join-Path $ScriptDir "hostnames.txt"
$NonInteractive = [Console]::IsInputRedirected

# --- Read the two parallel lists -------------------------------------------
$Servers = @()
if (Test-Path $AddressesFile) {
    $Servers = Get-Content $AddressesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
}
$Names = @()
if (Test-Path $HostnamesFile) {
    $Names = Get-Content $HostnamesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
}

if (-not $Servers -or $Servers.Count -eq 0) {
    Write-Host "ERROR: No IPs provided (addresses.txt is empty)." -ForegroundColor Red
    if (-not $NonInteractive) { Read-Host "Press ENTER to exit" }
    exit 1
}
if (-not $Names -or $Names.Count -eq 0) {
    Write-Host "ERROR: No hostnames provided (hostnames.txt is empty)." -ForegroundColor Red
    if (-not $NonInteractive) { Read-Host "Press ENTER to exit" }
    exit 1
}
if ($Servers.Count -ne $Names.Count) {
    Write-Host "ERROR: Count mismatch - $($Servers.Count) IP(s) but $($Names.Count) hostname(s)." -ForegroundColor Red
    Write-Host "The IP list and the hostname list must have the same number of lines." -ForegroundColor Yellow
    if (-not $NonInteractive) { Read-Host "Press ENTER to exit" }
    exit 1
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
Write-Host "Pairs        : $($Servers.Count)"
Write-Host ""

$ok = 0
$fail = 0
for ($i = 0; $i -lt $Servers.Count; $i++) {
    $ip = $Servers[$i]
    $name = $Names[$i]
    Write-Host "--------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Setting $ip  ->  hostname '$name' ..." -ForegroundColor Cyan
    try {
        & $Racadm -r $ip -u $IdracUser -p $IdracPass --nocertwarn set iDRAC.NIC.DNSRacName $name
        $code = $LASTEXITCODE
        if ($code -eq 0) {
            Write-Host "OK: $ip hostname set to '$name'." -ForegroundColor Green
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

if ($fail -gt 0 -and $ok -eq 0) { exit 1 }
exit 0
