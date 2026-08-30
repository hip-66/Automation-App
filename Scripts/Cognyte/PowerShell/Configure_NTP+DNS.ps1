# ===========================================================================
# Update DNS + NTP on Red Hat server(s) over SSH
# ---------------------------------------------------------------------------
# In PS Automation you supply:
#   * the target IP(s)
#   * the DNS server(s) to add   -> /etc/resolv.conf is rewritten to contain
#                                   EXACTLY these lines - nothing else (no
#                                   NetworkManager header/search-domain line,
#                                   nothing left over from before)
#   * the NTP server(s) to add   -> /etc/chrony.conf is rewritten to contain
#                                   EXACTLY these lines - nothing else (no
#                                   default RHEL comments/directives, no
#                                   leftover 'pool ...' line from before)
# The app writes those to addresses.txt / dns.txt / ntp.txt next to this script.
# For a manual (double-click) run it prompts for them instead.
#
# Everything on the remote host is done with sed. The SSH host key is
# auto-accepted the first time (no fingerprint has to exist beforehand).
# Credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the app's UI)
# wins; otherwise PSAUTO_DEFAULT_SSH_USERNAME/PASSWORD (the app's encrypted
# .env default) is used; a standalone run with neither set prompts instead -
# never hardcoded in this file.
# ===========================================================================

$ErrorActionPreference = 'Continue'

$user     = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_SSH_USERNAME) { $env:PSAUTO_DEFAULT_SSH_USERNAME } else { Read-Host "SSH username" }
$password = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_SSH_PASSWORD) { $env:PSAUTO_DEFAULT_SSH_PASSWORD } else { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "SSH password" -AsSecureString))) }

$ScriptDir      = $PSScriptRoot
$NonInteractive = [Console]::IsInputRedirected

function Read-List($fileName, $promptText) {
    $path = Join-Path $ScriptDir $fileName
    $items = @()
    if (Test-Path $path) {
        $items = Get-Content $path | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    }
    if ((-not $items -or $items.Count -eq 0) -and -not $NonInteractive) {
        $entered = Read-Host $promptText
        if (-not [string]::IsNullOrWhiteSpace($entered)) {
            $items = @($entered)
        }
    }
    return @($items)
}

$Servers = Read-List "addresses.txt" "Enter target Red Hat IP"
$DnsList = Read-List "dns.txt"       "Enter DNS server to add (top of the list)"
$NtpList = Read-List "ntp.txt"       "Enter NTP server to add (top of the list)"

if (-not $Servers -or $Servers.Count -eq 0) {
    Write-Host "ERROR: No target IP provided (addresses.txt is empty)." -ForegroundColor Red
    exit 1
}
if (($DnsList.Count -eq 0) -and ($NtpList.Count -eq 0)) {
    Write-Host "ERROR: Nothing to update - provide at least one DNS or NTP server." -ForegroundColor Red
    exit 1
}

# --- Resolve plink.exe ------------------------------------------------------
$plink = "plink"
if (-not (Get-Command $plink -ErrorAction SilentlyContinue)) {
    $candidates = @(
        (Join-Path $ScriptDir "plink.exe"),
        "C:\Program Files\PuTTY\plink.exe",
        "C:\Program Files (x86)\PuTTY\plink.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { $plink = $found }
    else {
        Write-Host "ERROR: plink.exe not found (PATH / next to script / PuTTY install)." -ForegroundColor Red
        exit 1
    }
}

# --- Build the remote bash script (sed only) --------------------------------
# Inserting with 'sed 1i' pushes each new line to the top, so we iterate the
# lists in REVERSE to preserve the order the user typed (first entered = first).
#
# Idempotent by design: running this script again with the SAME DNS/NTP values
# must not pile up duplicate lines. Before inserting each value, any EXISTING
# line for that exact value is deleted first - so re-running only ever results
# in one line per value (moved back to the top), never a growing duplicate list.
function ConvertTo-SedLiteral {
    # Escapes characters that are special in a sed BRE pattern (with '/' as the
    # delimiter), so a DNS/NTP value matches ONLY itself - never as a partial
    # or regex match (relevant since IPs contain '.', which is a regex wildcard).
    param([string]$Value)
    return ($Value -replace '([\\/.^$*[\]])', '\$1')
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add('set -e')
$lines.Add('TS=$(date +%F_%H%M%S)')
# Undo the immutable lock a PREVIOUS run of this script may have set on
# /etc/resolv.conf (see the end of the DNS section below) - otherwise every
# command that touches the file from here on (backup, touch, the rewrite)
# would fail with "Permission denied" even though we own the lock ourselves.
$lines.Add('chattr -i /etc/resolv.conf 2>/dev/null || true')
$lines.Add('cp -p /etc/chrony.conf /etc/chrony.conf.bak_$TS 2>/dev/null || true')
$lines.Add('cp -p /etc/resolv.conf /etc/resolv.conf.bak_$TS 2>/dev/null || true')
# Create either file if it doesn't exist yet, so a bare-minimum/fresh host
# never fails with "No such file or directory" on the sed commands below.
$lines.Add('touch /etc/chrony.conf /etc/resolv.conf')

$CHRONY_CONF_DIRECTIVES = @("server", "pool", "peer")

function Resolve-NtpLine {
    # Same idea as Resolve-DnsLine: an NTP field entry is written to
    # /etc/chrony.conf EXACTLY as typed if it already IS a full chrony
    # directive (e.g. "server 10.168.90.40 iburst") - nothing is ever added on
    # top of it, so it can never be double-wrapped into "server server ...
    # iburst iburst". Only a bare value (just an IP/hostname, the common case)
    # gets "server " / " iburst" added automatically.
    param([string]$Entry)
    $trimmed = $Entry.Trim()
    foreach ($d in $CHRONY_CONF_DIRECTIVES) {
        if ($trimmed -match "^$d(\s|$)") { return $trimmed }
    }
    return "server $trimmed iburst"
}

if ($NtpList.Count -gt 0) {
    # Full, deterministic rewrite: /etc/chrony.conf ends up containing EXACTLY
    # what was typed in the UI - nothing else (no default RHEL comments,
    # driftfile/keyfile/rtcsync boilerplate, no leftover 'pool ...' line from
    # a previous run). Mirrors the /etc/resolv.conf rewrite below. Since this
    # is a full overwrite every run, it's naturally idempotent - no need to
    # hunt down and delete a previous matching line first.
    $lines.Add("echo '=== Rewriting /etc/chrony.conf (exact content, nothing else) ==='")
    $lines.Add('cat > /etc/chrony.conf << ''PSAUTO_CHRONY_EOF''')
    foreach ($entry in $NtpList) { $lines.Add((Resolve-NtpLine $entry)) }
    $lines.Add('PSAUTO_CHRONY_EOF')
    $lines.Add('systemctl restart chronyd 2>/dev/null || true')
}

$RESOLV_CONF_DIRECTIVES = @("nameserver", "search", "domain", "options", "sortlist")

function Resolve-DnsLine {
    # A DNS field entry is written to /etc/resolv.conf EXACTLY as typed if it
    # already starts with a real resolv.conf directive (e.g. "search
    # quartet.com") - nothing is ever added to it. Only a bare value (just an
    # IP/hostname, the common case, e.g. "10.168.225.1") gets "nameserver "
    # prepended automatically, since that's the only way a bare IP is valid
    # in this file.
    param([string]$Entry)
    $trimmed = $Entry.Trim()
    foreach ($d in $RESOLV_CONF_DIRECTIVES) {
        if ($trimmed -match "^$d(\s|$)") { return $trimmed }
    }
    return "nameserver $trimmed"
}

if ($DnsList.Count -gt 0) {
    # On RHEL8+/Rocky/Alma, NetworkManager very often regenerates
    # /etc/resolv.conf on its own (confirmed via the diagnostics below), which
    # both wipes a plain edit AND stamps its own "# Generated by
    # NetworkManager" header on top. The durable fix is telling NetworkManager
    # itself what DNS to use (via nmcli), so future regenerations keep the
    # right servers; the FINAL rewrite below then guarantees the file's
    # CURRENT content is exactly (and only) what was typed here - no
    # NetworkManager header, no leftovers.
    #
    # Classify each DNS entry into what nmcli can actually represent:
    # "nameserver X" / a bare value -> a DNS server IP; "search X" -> a search
    # domain. Anything else (domain/options/sortlist) has no nmcli equivalent.
    $nmcliDnsServers = New-Object System.Collections.Generic.List[string]
    $nmcliSearchDomains = New-Object System.Collections.Generic.List[string]
    foreach ($entry in $DnsList) {
        $t = $entry.Trim()
        if ($t -match '^nameserver\s+(\S+)') { $nmcliDnsServers.Add($Matches[1]) }
        elseif ($t -match '^search\s+(.+)') { ($Matches[1].Trim() -split '\s+') | ForEach-Object { $nmcliSearchDomains.Add($_) } }
        elseif ($t -notmatch '^(domain|options|sortlist)(\s|$)') { $nmcliDnsServers.Add($t) }
    }

    # DEFINITIVE fix: tell NetworkManager to never manage /etc/resolv.conf at
    # all (dns=none). This is what actually stops the "# Generated by
    # NetworkManager" header + stale search-domain line from reappearing -
    # chattr +i alone isn't reliable (some filesystems don't support the
    # immutable flag, and if resolv.conf is a symlink NetworkManager can still
    # rewrite whatever it points at). Reloading here means it's already in
    # effect before the nmcli connection-up below and the final rewrite.
    $lines.Add("echo '=== Disabling NetworkManager resolv.conf management (dns=none) ==='")
    $lines.Add('mkdir -p /etc/NetworkManager/conf.d 2>/dev/null || true')
    $lines.Add('cat > /etc/NetworkManager/conf.d/90-psauto-no-dns.conf << ''PSAUTO_NM_EOF''')
    $lines.Add('[main]')
    $lines.Add('dns=none')
    $lines.Add('PSAUTO_NM_EOF')
    $lines.Add('(systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager 2>/dev/null || true)')

    if ($nmcliDnsServers.Count -gt 0 -or $nmcliSearchDomains.Count -gt 0) {
        $dnsServersStr = ($nmcliDnsServers -join ' ')
        $searchStr = ($nmcliSearchDomains -join ' ')
        # The whole block is wrapped so that ANY failure inside it (nmcli
        # missing, no active connection, etc.) is caught and logged instead
        # of aborting the rest of the script (this script runs under `set -e`,
        # and chrony/NTP already succeeds independently of this DNS step).
        $lines.Add("echo '=== NetworkManager DNS (nmcli) ==='")
        $lines.Add('(')
        $lines.Add('  if command -v nmcli >/dev/null 2>&1 && systemctl is-active NetworkManager >/dev/null 2>&1; then')
        $lines.Add('    NM_CONN=$(nmcli -t -f NAME connection show --active 2>/dev/null | head -1)')
        $lines.Add('    if [ -n "$NM_CONN" ]; then')
        $lines.Add('      echo "Applying DNS via NetworkManager connection: $NM_CONN"')
        $lines.Add(('      nmcli connection modify "$NM_CONN" ipv4.ignore-auto-dns yes ipv4.dns "' + $dnsServersStr + '" ipv4.dns-search "' + $searchStr + '"'))
        $lines.Add('      nmcli connection up "$NM_CONN"')
        $lines.Add('    else')
        $lines.Add('      echo "WARNING: NetworkManager is active but no active connection profile was found."')
        $lines.Add('    fi')
        $lines.Add('  else')
        $lines.Add('    echo "NetworkManager not active/available."')
        $lines.Add('  fi')
        $lines.Add(') || echo "WARNING: NetworkManager DNS setup (nmcli) failed."')
    }

    # Final, deterministic rewrite: /etc/resolv.conf ends up containing
    # EXACTLY these lines, in this order - nothing added by NetworkManager,
    # nothing left over from a previous run. The delimiter is single-quoted
    # ('PSAUTO_RESOLV_EOF') so bash performs NO substitution inside the
    # here-doc at all - every line is written completely literally.
    $lines.Add("echo '=== Rewriting /etc/resolv.conf (exact content, no NetworkManager header) ==='")
    $lines.Add('cat > /etc/resolv.conf << ''PSAUTO_RESOLV_EOF''')
    foreach ($entry in $DnsList) { $lines.Add((Resolve-DnsLine $entry)) }
    $lines.Add('PSAUTO_RESOLV_EOF')

    # Lock the file immutable (chattr +i) as a SECOND, defense-in-depth layer
    # on top of dns=none above - covers any other process (not just
    # NetworkManager) that might try to rewrite the file. Once locked, even
    # root cannot write to it until a future run of this script unlocks it
    # again (see "chattr -i" at the top).
    $lines.Add('if chattr +i /etc/resolv.conf 2>/dev/null; then echo "Locked /etc/resolv.conf (immutable) as an extra safety layer."; else echo "WARNING: chattr not available on this filesystem - relying on dns=none alone."; fi')
}

$lines.Add("echo '=== HOSTNAME ==='; hostname 2>/dev/null || echo unknown")
$lines.Add("echo '=== /etc/chrony.conf ==='; cat /etc/chrony.conf")
$lines.Add("echo '=== /etc/resolv.conf ==='; cat /etc/resolv.conf")
# Diagnostic only (never blocks/aborts anything, hence the "|| true" guards):
# on RHEL8+/Rocky/Alma, /etc/resolv.conf is very often a symlink MANAGED BY
# NetworkManager, which can silently regenerate/overwrite it (wiping any
# direct edit) whenever the network reconnects or on its own schedule. This
# reveals whether that's happening, instead of just showing an unexplained
# empty file.
$lines.Add("echo '=== resolv.conf diagnostics ==='; (ls -la /etc/resolv.conf 2>&1 || true); (readlink -f /etc/resolv.conf 2>&1 || true); (systemctl is-active NetworkManager 2>/dev/null || echo 'NetworkManager: not found/inactive')")
$lines.Add('echo "=== chronyc sources ==="; chronyc sources -v 2>/dev/null || true')
$lines.Add('echo Done.')

$remoteScript = ($lines -join "`n") + "`n"

$cmdFile = Join-Path $env:TEMP ("dnsntp_" + [System.Guid]::NewGuid().ToString('N') + ".sh")
# ASCII + LF only so the remote bash parses it cleanly.
[System.IO.File]::WriteAllText($cmdFile, ($remoteScript -replace "`r`n", "`n"), [System.Text.Encoding]::ASCII)

Write-Host "DNS to add : $($DnsList -join ', ')"
Write-Host "NTP to add : $($NtpList -join ', ')"
Write-Host "Targets    : $($Servers -join ', ')"
Write-Host ""

# Runs plink with a REAL redirected stdin (a genuine OS pipe, written to
# directly - more reliable than PowerShell's own "|" pipeline for feeding a
# native console app's interactive prompt) AND a hard timeout, so a plink call
# can never hang the whole run forever. Previously "y`n" | & $plink ... had no
# timeout at all: if plink's "Store key in cache?" prompt didn't read that
# piped answer (no console exists under this app's CREATE_NO_WINDOW launch),
# the script froze silently with zero further output - exactly the symptom of
# getting stuck right after "Starting automation script...".
function Format-ProcessArgs {
    # Windows PowerShell's ProcessStartInfo.ArgumentList is unavailable on some
    # .NET Framework builds (returns $null, not an empty collection) - build the
    # classic quoted argument string instead, which every Win32 console app
    # (including plink.exe) parses via the standard CommandLineToArgvW rules.
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
    # Uses Windows' bundled OpenSSH client (ssh-keyscan, default-installed
    # since Windows 10 1809 / all of Windows 11) to fetch the target's real
    # host key(s) BEFORE ever calling plink. ssh-keyscan is a pure "scan and
    # report" tool - it never trusts or refuses anything, so it needs no
    # interactive answer at all. Returns EVERY "<algo> <base64key>" line found
    # (the exact format plink's own -hostkey option accepts) - a server
    # commonly offers 2-3 host key types (RSA/ECDSA/ED25519), and the specific
    # one plink ends up negotiating during the real handshake isn't
    # predictable in advance, so ALL of them must be supplied via -hostkey
    # (which may be repeated) or a real but non-matching key gets rejected
    # with "Host key did not appear in manually configured list". Returns an
    # empty array if ssh-keyscan isn't available or the host didn't respond.
    #
    # WHY THIS IS NEEDED AT ALL: plink's own "Store key in cache?" trust
    # prompt reads its answer directly from the Windows console API, NOT from
    # redirected stdin - piping "y" to it (by any method) does not work when
    # there is no real console attached (this app always launches with no
    # console window), so that prompt can never be satisfied programmatically.
    # Only pre-supplying the real key(s) via -hostkey avoids the prompt ever
    # appearing.
    param([string]$HostIp, [int]$TimeoutSec = 8)
    $keyscanCmd = Get-Command ssh-keyscan -ErrorAction SilentlyContinue
    $keyscanPath = if ($keyscanCmd) { $keyscanCmd.Source } else {
        $fallback = "$env:WINDIR\System32\OpenSSH\ssh-keyscan.exe"
        if (Test-Path $fallback) { $fallback } else { $null }
    }
    if (-not $keyscanPath) { return @() }

    $result = Invoke-PlinkTimeout -PlinkPath $keyscanPath -PlinkArgs @("-T", "$TimeoutSec", "-t", "ed25519,rsa,ecdsa", $HostIp) -TimeoutSec ($TimeoutSec + 5)
    if ($result.TimedOut -or -not $result.Output.Trim()) { return @() }

    # ssh-keyscan prints "<host> <algo> <base64key>" (one line per key type)
    # on success, but an ERROR line (e.g. "(1.2.3.4): Connection refused") on
    # failure - which ALSO happens to split into 3 whitespace-separated
    # "fields", so a naive parse would wrongly return that error text as if it
    # were a real key. Guard against this by requiring the middle field to
    # actually be a recognized SSH key algorithm name.
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

function Get-MarkedSection {
    # Pulls the block of text between "=== $Marker ===" and the NEXT "=== "
    # marker (or end of output) out of a captured plink run's combined
    # stdout/stderr - used to build the final per-server verification report
    # from output that's already been captured, with no extra SSH calls.
    param([string]$Output, [string]$Marker)
    $pattern = "(?ms)^=== $([regex]::Escape($Marker)) ===\r?\n(.*?)(?=^===|\z)"
    $m = [regex]::Match($Output, $pattern)
    if ($m.Success) { return $m.Groups[1].Value.TrimEnd() }
    return ""
}

$ok = 0
$fail = 0
$serverResults = New-Object System.Collections.Generic.List[object]
foreach ($ip in $Servers) {
    Write-Host "--------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Updating $ip ..." -ForegroundColor Cyan

    $hostKeyArgs = @()
    $hostKeys = Get-SshHostKeys -HostIp $ip
    if ($hostKeys.Count -gt 0) {
        foreach ($hk in $hostKeys) { $hostKeyArgs += @("-hostkey", $hk) }
        Write-Host "  $($hostKeys.Count) host key(s) verified via ssh-keyscan - connecting non-interactively (no trust prompt needed)." -ForegroundColor DarkGray
    }
    else {
        Write-Host "  WARNING: could not pre-fetch the host key via ssh-keyscan (needs Windows' OpenSSH Client feature)." -ForegroundColor Yellow
        Write-Host "  Falling back to a best-effort prompt workaround for $ip - may require a second run on a BRAND NEW host." -ForegroundColor Yellow
        # Best-effort only: kept as a fallback, but on this app's launch mode
        # (no console window) plink's trust prompt generally can't be
        # satisfied this way - see the Get-SshHostKey comment above.
        $prime = Invoke-PlinkTimeout -PlinkPath $plink -PlinkArgs @("-ssh", "-pw", $password, "$user@$ip", "exit") -StdinLine "y" -TimeoutSec 25
        if ($prime.TimedOut) {
            Write-Host "  WARNING: Timed out waiting for the SSH host-key prompt on $ip (25s) - continuing anyway." -ForegroundColor Yellow
        } elseif ($prime.Output.Trim()) {
            Write-Host "  $($prime.Output.Trim())" -ForegroundColor DarkGray
        }
    }

    # -batch: no prompts are even possible, so this fails fast and visibly if
    # the key still isn't trusted or the SSH login is wrong, instead of
    # hanging, with its own 60s hard cap.
    $run = Invoke-PlinkTimeout -PlinkPath $plink -PlinkArgs (@("-ssh", "-batch", "-pw", $password) + $hostKeyArgs + @("$user@$ip", "-m", $cmdFile)) -TimeoutSec 60
    if ($run.Output.Trim()) { Write-Host $run.Output.Trim() }

    $thisOk = $false
    if ($run.TimedOut) {
        Write-Host "ERROR: plink timed out (60s) for $ip - check network connectivity to this host." -ForegroundColor Red
        $fail++
    }
    elseif ($run.ExitCode -eq 0) {
        Write-Host "OK: $ip updated." -ForegroundColor Green
        $ok++
        $thisOk = $true
    }
    else {
        Write-Host "ERROR: plink exited with code $($run.ExitCode) for $ip." -ForegroundColor Red
        if ($run.Output -match "(?i)host key is not cached|store key in cache") {
            Write-Host "  HINT: the SSH host key for $ip isn't trusted yet. From a real console, run:" -ForegroundColor Yellow
            Write-Host "        plink -ssh $user@$ip" -ForegroundColor Yellow
            Write-Host "        answer 'y' at the prompt once, then re-run this automation." -ForegroundColor Yellow
        }
        elseif ($run.Output -match "(?i)did not appear in manually configured list") {
            Write-Host "  HINT: ssh-keyscan's key(s) for $ip didn't match what plink saw during the real handshake." -ForegroundColor Yellow
            Write-Host "        This can happen right after the host's SSH key was regenerated/rotated, or if" -ForegroundColor Yellow
            Write-Host "        $ip is behind a load balancer routing to different hosts. Re-running usually" -ForegroundColor Yellow
            Write-Host "        re-fetches the current key and succeeds; if it keeps failing, verify the host's" -ForegroundColor Yellow
            Write-Host "        real key by connecting once manually: plink -ssh $user@$ip" -ForegroundColor Yellow
        }
        $fail++
    }

    # Captured from THIS SAME connection's output - no extra SSH calls needed.
    $serverResults.Add([PSCustomObject]@{
        IP          = $ip
        Hostname    = $(if ($run.Output) { $h = Get-MarkedSection $run.Output "HOSTNAME"; if ($h) { $h } else { "(unknown)" } } else { "(unknown)" })
        Success     = $thisOk
        Resolv      = Get-MarkedSection $run.Output "/etc/resolv.conf"
        Chrony      = Get-MarkedSection $run.Output "/etc/chrony.conf"
        Diagnostics = Get-MarkedSection $run.Output "resolv.conf diagnostics"
    })
}

Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue

# Per-server verification report - one block per target IP (as many blocks as
# servers were provided), showing exactly what's on disk right now: hostname
# and the full, current content of both files, so success can be confirmed
# by eye without opening a separate SSH session per server.
Write-Host ""
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " PER-SERVER VERIFICATION - current file contents on each target" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan
foreach ($r in $serverResults) {
    $statusColor = if ($r.Success) { "Green" } else { "Red" }
    $statusText = if ($r.Success) { "OK" } else { "FAILED - config below may be incomplete/unchanged" }
    Write-Host ""
    Write-Host "[$($r.IP)]  hostname: $($r.Hostname)  -  $statusText" -ForegroundColor $statusColor
    # An empty section means two DIFFERENT things depending on whether the
    # connection itself succeeded - conflating them ("not captured") is
    # misleading when the run actually succeeded but the file is genuinely
    # empty on the server (e.g. NetworkManager wiped /etc/resolv.conf).
    $emptyMsg = if ($r.Success) { "  (file is empty on the server)" } else { "  (not captured - connection failed before this point)" }
    Write-Host "  --- /etc/resolv.conf ---" -ForegroundColor DarkGray
    if ($r.Resolv) { ($r.Resolv -split "`n") | ForEach-Object { Write-Host "  $_" } } else { Write-Host $emptyMsg -ForegroundColor Yellow }
    if (-not $r.Resolv -and $r.Diagnostics) {
        Write-Host "  (diagnostics: is /etc/resolv.conf a symlink managed by NetworkManager?)" -ForegroundColor DarkYellow
        ($r.Diagnostics -split "`n") | ForEach-Object { Write-Host "    $_" }
    }
    Write-Host "  --- /etc/chrony.conf ---" -ForegroundColor DarkGray
    if ($r.Chrony) { ($r.Chrony -split "`n") | ForEach-Object { Write-Host "  $_" } } else { Write-Host $emptyMsg -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Done. Success: $ok, Failed: $fail, Total: $($Servers.Count)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Quick pass/fail summary, deliberately printed LAST (the very bottom of the
# log) so it can be read at a glance without scrolling back through the full
# per-server verification output above - one line per server, in the same
# order they were given.
Write-Host ""
Write-Host "QUICK SUMMARY:" -ForegroundColor Cyan
foreach ($r in $serverResults) {
    if ($r.Success) {
        Write-Host "  V  $($r.IP)  ($($r.Hostname))" -ForegroundColor Green
    } else {
        Write-Host "  X  $($r.IP)  ($($r.Hostname))" -ForegroundColor Red
    }
}

if (-not $NonInteractive) { Read-Host "Press ENTER to exit" }

if ($fail -gt 0 -and $ok -eq 0) { exit 1 }
exit 0
