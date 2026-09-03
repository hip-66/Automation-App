# --- Force Admin Privileges ---
if (!([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$ScriptDir = $PSScriptRoot
$NonInteractive = [Console]::IsInputRedirected

# --- Configuration ---
# iDRAC credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the
# app's UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's
# encrypted .env default) is used; a standalone run with neither set prompts
# instead - never hardcoded in this file.
$User = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_USERNAME) { $env:PSAUTO_DEFAULT_USERNAME } else { Read-Host "iDRAC username" }
$Pass = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $env:PSAUTO_DEFAULT_PASSWORD } else { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "iDRAC password" -AsSecureString))) }

# Built-in default target list - used for a standalone run only when
# addresses.txt (below) isn't present and nothing is entered at the prompt.
$DefaultServers = [ordered]@{
    "FM1"    = "192.168.80.122"
    "FM2"    = "192.168.80.123"
    "PMC1"   = "192.168.80.124"
    "PMC2"   = "192.168.80.125"
    "PMC3"   = "192.168.80.126"
    "SRVMGT" = "192.168.80.127"
    "NGINX"  = "192.168.80.128"
}

# --- Resolve target list -----------------------------------------------------
# PS Automation writes addresses.txt (one iDRAC IP per line) next to this
# script before launching it. When present, it takes over from the built-in
# list above (targets are keyed by their own IP, since addresses.txt carries
# no friendly name).
$AddressesFile = Join-Path $ScriptDir "addresses.txt"
$Servers = [ordered]@{}
if (Test-Path $AddressesFile) {
    $ips = Get-Content $AddressesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    foreach ($ip in $ips) { $Servers[$ip] = $ip }
}
if ($Servers.Count -eq 0 -and -not $NonInteractive) {
    $entered = Read-Host "Enter target iDRAC IP (leave blank to use the built-in default list)"
    if (-not [string]::IsNullOrWhiteSpace($entered)) { $Servers[$entered] = $entered }
}
if ($Servers.Count -eq 0) { $Servers = $DefaultServers }

# Array to store tags for the final clean list
$TagList = @()

Write-Host "`nChecking for racadm.exe..." -ForegroundColor Cyan
if (!(Get-Command racadm.exe -ErrorAction SilentlyContinue)) {
    Write-Host "CRITICAL ERROR: racadm.exe not found in system PATH!" -ForegroundColor Red
    if (-not $NonInteractive) { Read-Host "Press Enter to exit" }
    exit
}

Write-Host "Starting Service Tag collection..." -ForegroundColor Cyan
Write-Host "--------------------------------------------------"

foreach ($Name in $Servers.Keys) {
    $IP = $Servers[$Name]

    # Executing the command
    $Info = & racadm.exe -r $IP -u $User -p $Pass --nocertwarn getsysinfo 2>$null

    if ($Info) {
        # Search for the Service Tag line (flexible for SVC Tag or Service Tag)
        $Line = $Info | Select-String "Tag" | Select-Object -First 1
        if ($Line) {
            $Tag = ($Line.ToString() -split "=")[1].Trim()
            Write-Host "$Name - $Tag" -ForegroundColor Green
            $TagList += $Tag # Add to our summary list
        } else {
            Write-Host "$Name - Error: Tag not found in output" -ForegroundColor Yellow
            $TagList += "N/A ($Name)"
        }
    } else {
        Write-Host "$Name - Error: Connection failed" -ForegroundColor Red
        $TagList += "FAILED ($Name)"
    }
}

# --- Final Clean Summary ---
Write-Host "--------------------------------------------------"
foreach ($Item in $TagList) {
    Write-Host $Item
}
Write-Host "--------------------------------------------------"

if (-not $NonInteractive) { Read-Host "Done. Press Enter to exit" }
