# ===========================================================================
# MDE / ATP Validation 2.0
# ---------------------------------------------------------------------------
# Just enter the target IP(s) in PS Automation and click Run - everything else
# is automatic:
#   * connects over SSH using the configured credentials (no prompts, no
#     profile choice) - never hardcoded here (see the credentials note below)
#   * pre-verifies the host key via ssh-keyscan so no interactive trust prompt
#     is ever needed (plink's own trust prompt reads its answer straight from
#     the Windows console, which doesn't exist under this app's launch mode -
#     piping "y" to it does not work, so this is the only reliable fix)
#   * runs the fixed validation command set (embedded below, so it never
#     depends on a shared commands.txt that other scripts might overwrite)
#   * saves a SEPARATE output file PER SERVER, named after that server's
#     hostname (falls back to the IP when the hostname can't be read), under
#     .\Validations\ - the app then auto-moves them into the run's own
#     Outputs\<script>_<date>_<time>\validation\ folder, so every server's
#     output sits as its own file inside that single run folder
#
# Credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the app's UI)
# wins; otherwise PSAUTO_DEFAULT_SSH_USERNAME/PASSWORD (the app's encrypted
# .env default) is used; a standalone run with neither set prompts instead -
# never hardcoded in this file.
# ===========================================================================

$ErrorActionPreference = 'Continue'
$NonInteractive = [Console]::IsInputRedirected

$user     = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_SSH_USERNAME) { $env:PSAUTO_DEFAULT_SSH_USERNAME } elseif ($NonInteractive) { Write-Host "ERROR: No SSH username available (PSAUTO_USERNAME / PSAUTO_DEFAULT_SSH_USERNAME are both unset) and there is no console to prompt on." -ForegroundColor Red; exit 1 } else { Read-Host "SSH username" }
$password = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_SSH_PASSWORD) { $env:PSAUTO_DEFAULT_SSH_PASSWORD } elseif ($NonInteractive) { Write-Host "ERROR: No SSH password available (PSAUTO_PASSWORD / PSAUTO_DEFAULT_SSH_PASSWORD are both unset) and there is no console to prompt on." -ForegroundColor Red; exit 1 } else { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "SSH password" -AsSecureString))) }

# --- The fixed validation commands (single-quoted here-string: everything is
#     literal, so $i / $(...) are sent verbatim to the remote bash) -----------
$commands = @'
echo hostname
hostname

echo ----------------------------------------------------

echo ifconfig -a
ifconfig -a

echo ----------------------------------------------------

echo ip a
ip a

echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-eno8303
cat /etc/sysconfig/network-scripts/ifcfg-eno8303
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-eno8403
cat /etc/sysconfig/network-scripts/ifcfg-eno8403
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-eno12399
cat /etc/sysconfig/network-scripts/ifcfg-eno12399
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-eno12409
cat /etc/sysconfig/network-scripts/ifcfg-eno12409
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-eno12419
cat /etc/sysconfig/network-scripts/ifcfg-eno12419
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-eno12429
cat /etc/sysconfig/network-scripts/ifcfg-eno12429
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-team0
cat /etc/sysconfig/network-scripts/ifcfg-team0
echo ----------------------------------------------------
echo cat /etc/sysconfig/network-scripts/ifcfg-team1
cat /etc/sysconfig/network-scripts/ifcfg-team1
echo ----------------------------------------------------

echo yum repolist
yum repolist

echo ----------------------------------------------------

echo lsblk --list
lsblk --list

echo ----------------------------------------------------

echo cat /etc/resolv.conf
cat /etc/resolv.conf

echo ----------------------------------------------------

echo cat /etc/chrony.conf
cat /etc/chrony.conf

echo ----------------------------------------------------

echo "for i in $(ls /etc/*release); do echo ===$i===; cat $i; done"
for i in $(ls /etc/*release); do echo ===$i===; cat $i; done

echo ----------------------------------------------------

date

echo ----------------------------------------------------
echo End Of Validation
'@

# --- Prep folders + a temp command file for plink -m -----------------------
$validationsPath = Join-Path $PSScriptRoot "Validations"
if (!(Test-Path $validationsPath)) {
    New-Item -ItemType Directory -Path $validationsPath | Out-Null
}

$cmdFile = Join-Path $env:TEMP ("mde_cmds_" + [System.Guid]::NewGuid().ToString('N') + ".txt")
# ASCII with LF only, so the remote bash doesn't choke on CRLF line endings.
[System.IO.File]::WriteAllText($cmdFile, ($commands -replace "`r`n", "`n"), [System.Text.Encoding]::ASCII)

$addressesFile = Join-Path $PSScriptRoot "addresses.txt"
if (!(Test-Path $addressesFile)) {
    Write-Host "ERROR: addresses.txt not found next to the script." -ForegroundColor Red
    Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue
    exit 1
}

# --- Resolve plink.exe (PATH, next to the script, or a standard PuTTY dir) --
$plink = "plink"
if (-not (Get-Command $plink -ErrorAction SilentlyContinue)) {
    $candidates = @(
        (Join-Path $PSScriptRoot "plink.exe"),
        "C:\Program Files\PuTTY\plink.exe",
        "C:\Program Files (x86)\PuTTY\plink.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) {
        $plink = $found
    }
    else {
        Write-Host "ERROR: plink.exe not found (PATH / next to script / PuTTY install)." -ForegroundColor Red
        Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue
        exit 1
    }
}

# Runs plink with a REAL redirected stdin (a genuine OS pipe) and a hard
# timeout, so a plink call can never hang the whole run forever - piping "y"
# via PowerShell's own "|" pipeline does not reliably answer plink's
# interactive host-key prompt (it reads that answer straight from the Windows
# console, which doesn't exist under this app's no-window launch mode).
function Format-ProcessArgs {
    param([string[]]$ArgArray)
    ($ArgArray | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join ' '
}

function Invoke-PlinkTimeout {
    param([string]$PlinkPath, [string[]]$PlinkArgs, [string]$StdinLine = $null, [int]$TimeoutSec = 25)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PlinkPath
    $psi.Arguments = Format-ProcessArgs $PlinkArgs
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if ($StdinLine) { $proc.StandardInput.WriteLine($StdinLine) }
    $proc.StandardInput.Close()

    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch {}
        return [PSCustomObject]@{ TimedOut = $true; ExitCode = -1; Output = "" }
    }
    [System.Threading.Tasks.Task]::WaitAll(@($outTask, $errTask), 2000) | Out-Null
    $out = if ($outTask.IsCompleted) { $outTask.Result } else { "" }
    $err = if ($errTask.IsCompleted) { $errTask.Result } else { "" }
    return [PSCustomObject]@{ TimedOut = $false; ExitCode = $proc.ExitCode; Output = ($out + $err) }
}

function Get-SshHostKeys {
    # Fetches the target's real host key(s) via Windows' bundled OpenSSH
    # client (ssh-keyscan) BEFORE ever calling plink, so plink can be told to
    # trust them via -hostkey and never needs to show its trust prompt at
    # all. A server commonly offers 2-3 key types (RSA/ECDSA/ED25519), and
    # ssh-keyscan doesn't know in advance which one plink will end up
    # negotiating, so ALL of them are returned and all get passed to plink.
    param([string]$HostIp, [int]$TimeoutSec = 8)
    $keyscanCmd = Get-Command ssh-keyscan -ErrorAction SilentlyContinue
    $keyscanPath = if ($keyscanCmd) { $keyscanCmd.Source } else {
        $fallback = "$env:WINDIR\System32\OpenSSH\ssh-keyscan.exe"
        if (Test-Path $fallback) { $fallback } else { $null }
    }
    if (-not $keyscanPath) { return @() }

    $result = Invoke-PlinkTimeout -PlinkPath $keyscanPath -PlinkArgs @("-T", "$TimeoutSec", "-t", "ed25519,rsa,ecdsa", $HostIp) -TimeoutSec ($TimeoutSec + 5)
    if ($result.TimedOut -or -not $result.Output.Trim()) { return @() }

    # ssh-keyscan prints "<host> <algo> <base64key>" on success, but an ERROR
    # line (e.g. "(1.2.3.4): Connection refused") on failure - which ALSO
    # splits into 3 whitespace-separated "fields", so require the middle
    # field to actually be a recognized SSH key algorithm name.
    $knownAlgos = @("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")
    $lines = ($result.Output -split "`n") |
        Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } |
        Where-Object { ($_.Trim() -split '\s+', 3)[1] -in $knownAlgos }
    $keys = @()
    foreach ($line in $lines) {
        $parts = $line.Trim() -split '\s+', 3
        $keys += "$($parts[1]) $($parts[2])"
    }
    return $keys
}

Write-Host "Starting MDE/ATP validation (user: $user)..." -ForegroundColor Green

$ok = 0
$fail = 0
$results = New-Object System.Collections.Generic.List[object]

foreach ($line in Get-Content $addressesFile) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $ip = $line.Trim()
    Write-Host ""
    Write-Host "Connecting to $ip..." -ForegroundColor Cyan

    $hostKeyArgs = @()
    $hostKeys = Get-SshHostKeys -HostIp $ip
    if ($hostKeys.Count -gt 0) {
        foreach ($hk in $hostKeys) { $hostKeyArgs += @("-hostkey", $hk) }
        Write-Host "  $($hostKeys.Count) host key(s) verified via ssh-keyscan - connecting non-interactively." -ForegroundColor DarkGray
    }
    else {
        Write-Host "  WARNING: could not pre-fetch the host key via ssh-keyscan for $ip - falling back to a best-effort prompt workaround (may not succeed on a brand-new host)." -ForegroundColor Yellow
        $prime = Invoke-PlinkTimeout -PlinkPath $plink -PlinkArgs @("-ssh", "-pw", $password, "$user@$ip", "exit") -StdinLine "y" -TimeoutSec 25
        if ($prime.TimedOut) { Write-Host "  WARNING: Timed out waiting for the SSH host-key prompt on $ip (25s) - continuing anyway." -ForegroundColor Yellow }
    }

    # Get the hostname (best-effort - falls back to the IP if this fails,
    # since it's only used for the log file NAME, not for pass/fail status).
    $hnResult = Invoke-PlinkTimeout -PlinkPath $plink -PlinkArgs (@("-ssh", "-batch", "-pw", $password) + $hostKeyArgs + @("$user@$ip", "hostname")) -TimeoutSec 30
    $hostname = if (-not $hnResult.TimedOut) { ($hnResult.Output -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1) } else { "" }
    if ([string]::IsNullOrWhiteSpace($hostname)) {
        $hostname = $ip
        Write-Host "Could not retrieve hostname for $ip. Falling back to IP." -ForegroundColor Yellow
    }
    else {
        $hostname = $hostname.Trim()
        Write-Host "Retrieved Hostname: $hostname" -ForegroundColor Cyan
    }

    # Run the validation, with a real hard timeout (120s - this command set
    # is longer than a simple check) so a stuck connection can never hang the
    # whole run.
    $runResult = Invoke-PlinkTimeout -PlinkPath $plink -PlinkArgs (@("-ssh", "-batch", "-pw", $password) + $hostKeyArgs + @("$user@$ip", "-m", $cmdFile)) -TimeoutSec 120

    # Real success/failure detection - a connection/auth failure now shows up
    # clearly instead of silently writing an error into the log and reporting
    # "finished" as if the validation had actually run.
    $thisOk = (-not $runResult.TimedOut) -and ($runResult.ExitCode -eq 0) -and ($runResult.Output -match "End Of Validation")
    $statusText = if ($thisOk) { "OK" } elseif ($runResult.TimedOut) { "FAILED (timed out after 120s)" } else { "FAILED (plink exit code $($runResult.ExitCode))" }
    if ($thisOk) {
        Write-Host "OK: Validation for $hostname ($ip) finished." -ForegroundColor Green
        $ok++
    }
    elseif ($runResult.TimedOut) {
        Write-Host "FAILED: plink timed out (120s) for $hostname ($ip)." -ForegroundColor Red
        $fail++
    }
    else {
        Write-Host "FAILED: plink exited with code $($runResult.ExitCode) for $hostname ($ip) - the log may be incomplete." -ForegroundColor Red
        $fail++
    }

    # Write THIS server's own output file, named after its hostname (falls
    # back to the IP when the hostname couldn't be read). Written the moment
    # the server finishes - a Stop/kill mid-run still leaves the files of
    # every server already done.
    try {
        # File is named by the HOSTNAME ONLY (no date/time - the run's dated
        # Outputs folder already carries the timestamp). Falls back to the IP
        # when the hostname couldn't be read.
        $safeName = ($hostname -replace '[^\w\.\-]', '_')
        $logFile  = Join-Path $validationsPath ("${safeName}.log")
        $header   = "===== $hostname ($ip) - $statusText - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====`r`n"
        [System.IO.File]::WriteAllText($logFile, $header + $runResult.Output, [System.Text.Encoding]::UTF8)
        Write-Host "  Log saved: $logFile" -ForegroundColor DarkGray
    } catch {
        Write-Host "  WARNING: could not write the output file for $hostname ($ip) - $($_.Exception.Message)" -ForegroundColor Yellow
    }

    $results.Add([PSCustomObject]@{ IP = $ip; Hostname = $hostname; Success = $thisOk })
}

Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Done. Success: $ok, Failed: $fail, Total: $($results.Count)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Quick pass/fail summary, deliberately printed LAST so it can be read at a
# glance without scrolling back through the full per-server log above.
Write-Host ""
Write-Host "QUICK SUMMARY:" -ForegroundColor Cyan
foreach ($r in $results) {
    if ($r.Success) { Write-Host "  V  $($r.IP)  ($($r.Hostname))" -ForegroundColor Green }
    else { Write-Host "  X  $($r.IP)  ($($r.Hostname))" -ForegroundColor Red }
}

if ($fail -gt 0 -and $ok -eq 0) { exit 1 }
exit 0
