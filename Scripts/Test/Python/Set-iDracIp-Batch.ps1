# ===========================================================================
# Batch-assign static iDRAC IPs to brand-new / factory-reset servers
# ---------------------------------------------------------------------------
# Every fresh-out-of-the-box server answers at the SAME factory/staging iDRAC
# address (via a direct USB-NIC connection) with the same baseline
# credentials, until it's given its own static identity. You type how many
# systems you have and the first static octet to start from; the script then
# walks you through them one at a time - connect USB to a server, press a
# key, it assigns the next sequential IP (start, start+1, start+2, ...) and
# confirms the server actually answers at its new address before moving on.
#
# Credentials: PSAUTO_USERNAME/PASSWORD (explicit override) wins; otherwise
# PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted .env default, which is
# also this org's baseline factory credential for new servers); a standalone
# console run with neither set prompts instead - never hardcoded in this file.
#
# Direct/prefix/netmask/gateway can be overridden via PSAUTO_DIRECT_IP /
# PSAUTO_IP_PREFIX / PSAUTO_NETMASK / PSAUTO_GATEWAY without editing this file.
# ===========================================================================

$NonInteractive = [Console]::IsInputRedirected

# This whole script is a physical, human-supervised workflow (plug in a USB
# cable to each new server in turn, press a key) - there is no automated
# equivalent, so unlike the app's other scripts there is nothing sensible to
# fall back to here. Fail fast and clearly instead of crashing later on
# [Console]::ReadKey (which throws if the console is redirected).
if ($NonInteractive) {
    Write-Host "ERROR: This script requires an interactive console (it walks you through each server one at a time). Run it directly from a real PowerShell console, not through an automated/non-interactive launcher." -ForegroundColor Red
    exit 1
}

$direct  = if ($env:PSAUTO_DIRECT_IP) { $env:PSAUTO_DIRECT_IP } else { "169.254.0.3" }
$prefix  = if ($env:PSAUTO_IP_PREFIX) { $env:PSAUTO_IP_PREFIX } else { "192.168.0" }
$netmask = if ($env:PSAUTO_NETMASK)   { $env:PSAUTO_NETMASK }   else { "" }
$gateway = if ($env:PSAUTO_GATEWAY)   { $env:PSAUTO_GATEWAY }   else { "" }

# No $NonInteractive branch needed here (unlike the app's other scripts) -
# the guard above already exited if this run isn't interactive, so the
# Read-Host fallback below is always reachable from a real console.
$user     = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_USERNAME) { $env:PSAUTO_DEFAULT_USERNAME } else { Read-Host "iDRAC username (factory default)" }
$password = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $env:PSAUTO_DEFAULT_PASSWORD } else { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "iDRAC password (factory default)" -AsSecureString))) }

if (-not (Get-Command racadm -ErrorAction SilentlyContinue)) {
    Write-Host "FATAL: racadm was not found in PATH. Install Dell RACADM / OpenManage tools." -ForegroundColor Red
    exit 1
}

$countInput = Read-Host "Number of systems"
$count = 0
if (-not [int]::TryParse($countInput, [ref]$count)) {
    Write-Host "ERROR: '$countInput' is not a number." -ForegroundColor Red
    exit 1
}
if ($count -lt 1) {
    Write-Host "ERROR: number of systems must be at least 1." -ForegroundColor Red
    exit 1
}

$startInput = Read-Host "Starting last octet (e.g. 121)"
$start = 0
if (-not [int]::TryParse($startInput, [ref]$start)) {
    Write-Host "ERROR: '$startInput' is not a number." -ForegroundColor Red
    exit 1
}
if ($start -lt 1 -or $start -gt 254) {
    Write-Host "ERROR: starting octet must be between 1 and 254." -ForegroundColor Red
    exit 1
}
if (($start + $count - 1) -gt 254) {
    Write-Host "ERROR: range $prefix.$start - $prefix.$($start + $count - 1) exceeds .254." -ForegroundColor Red
    exit 1
}

function Invoke-Racadm {
    param([string]$TargetIp, [string[]]$RacadmArgs)
    $out = & racadm -r $TargetIp -u $user -p $password --nocertwarn @RacadmArgs 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    return @{ Ok = ($exitCode -eq 0); Output = $out.Trim() }
}

function Test-NewIpReachable {
    param([string]$NewIp)
    $res = Invoke-Racadm -TargetIp $NewIp -RacadmArgs @("getsysinfo")
    return ($res.Ok -and $res.Output -notmatch "(?i)error|unable to connect")
}

Write-Host ""
Write-Host "Direct (factory) IP : $direct"
Write-Host "Target prefix       : $prefix.x"
Write-Host "Netmask             : $(if ($netmask) { $netmask } else { '(unchanged)' })"
Write-Host "Gateway             : $(if ($gateway) { $gateway } else { '(unchanged)' })"
Write-Host "Range               : $prefix.$start - $prefix.$($start + $count - 1)  ($count system(s))"

$results = @()

for ($i = 0; $i -lt $count; $i++) {
    $octet = $start + $i
    $newIp = "$prefix.$octet"

    Write-Host ""
    Write-Host "System $($i + 1) of $count -> $newIp" -ForegroundColor Cyan
    Write-Host "Connect USB to this server, then press any key to apply..."

    while ([Console]::KeyAvailable) { [void][Console]::ReadKey($true) }
    [void][Console]::ReadKey($true)

    $stepsOk = $true

    $r = Invoke-Racadm -TargetIp $direct -RacadmArgs @("set", "iDRAC.IPv4.DHCPEnable", "0")
    Write-Host "  DHCPEnable=0  $(if ($r.Ok) { 'OK' } else { 'FAILED' })  $($r.Output)"
    if (-not $r.Ok) { $stepsOk = $false }

    # Netmask + Gateway BEFORE Address - they don't drop the current (direct)
    # connection, so they're already in place before the address flips. Same
    # order already proven correct in change_ip.py, for the same reason.
    if ($netmask -ne "") {
        $r = Invoke-Racadm -TargetIp $direct -RacadmArgs @("set", "iDRAC.IPv4.Netmask", $netmask)
        Write-Host "  Netmask=$netmask  $(if ($r.Ok) { 'OK' } else { 'FAILED' })  $($r.Output)"
        if (-not $r.Ok) { $stepsOk = $false }
    }
    if ($gateway -ne "") {
        $r = Invoke-Racadm -TargetIp $direct -RacadmArgs @("set", "iDRAC.IPv4.Gateway", $gateway)
        Write-Host "  Gateway=$gateway  $(if ($r.Ok) { 'OK' } else { 'FAILED' })  $($r.Output)"
        if (-not $r.Ok) { $stepsOk = $false }
    }

    # Address LAST - this is what drops the connection to $direct, so its own
    # exit code/output can no longer be trusted once the link flips.
    $r = Invoke-Racadm -TargetIp $direct -RacadmArgs @("set", "iDRAC.IPv4.Address", $newIp)
    Write-Host "  Address=$newIp  $($r.Output)"

    # Real success signal: does the NEW address actually answer? (Not the
    # command's own exit code - see above.) Same settle+poll approach as
    # change_ip.py, which performs this exact operation.
    Write-Host "  Waiting for $newIp to come up..."
    Start-Sleep -Seconds 12
    $deadline = (Get-Date).AddSeconds(90)
    $reached = $false
    while (-not $reached -and (Get-Date) -lt $deadline) {
        if (Test-NewIpReachable -NewIp $newIp) { $reached = $true; break }
        Start-Sleep -Seconds 8
    }

    if ($reached -and $stepsOk) {
        Write-Host "  OK: iDRAC is now reachable at $newIp." -ForegroundColor Green
        $results += [pscustomobject]@{ System = $i + 1; NewIp = $newIp; Status = "OK"; Note = "" }
    } elseif ($reached) {
        Write-Host "  PARTIAL: $newIp answers, but Netmask/Gateway/DHCPEnable had a failed step above - please check it." -ForegroundColor Yellow
        $results += [pscustomobject]@{ System = $i + 1; NewIp = $newIp; Status = "PARTIAL"; Note = "check Netmask/Gateway/DHCPEnable above" }
    } else {
        Write-Host "  FAILED: $newIp did not respond within 90s of the address change." -ForegroundColor Red
        $results += [pscustomobject]@{ System = $i + 1; NewIp = $newIp; Status = "FAILED"; Note = "new IP never answered" }
    }
}

Write-Host ""
Write-Host ("=" * 60)
Write-Host " FINAL SUMMARY"
Write-Host ("=" * 60)
foreach ($r in $results) {
    Write-Host ("  {0,-8} System {1,-4} {2,-16} {3}" -f $r.Status, $r.System, $r.NewIp, $r.Note)
}
$ok = ($results | Where-Object { $_.Status -eq "OK" }).Count
Write-Host ("=" * 60)
Write-Host " Done. OK: $ok, Failed/Partial: $($results.Count - $ok), Total: $($results.Count)"
Write-Host " Range: $prefix.$start - $prefix.$($start + $count - 1)"

if (-not $NonInteractive) {
    Write-Host "`nPress Enter to exit..." -ForegroundColor Yellow
    Read-Host
}
