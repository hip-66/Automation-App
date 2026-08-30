# =====================================================================
# Configure Raid1 - iDRAC9 fleet automation
# Per server:
# 1. Select physical disks by Slot 0 + Slot 1
# 2. If disks are Non-RAID, run ConvertToRAID
# 3. Commit storage job + PowerCycle
# 4. WAIT until disks are really RAID-capable / Ready
# 5. Only then create RAID1 vDisk1
# 6. Commit RAID job + PowerCycle
# 7. Set BIOS BootMode to UEFI
#
# PARALLEL: every target server runs in its OWN separate session (a child
# PowerShell process), all at once - so 10 servers finish in the time of one,
# not one-after-another. Each child's output is tagged with its IP so the app
# can show a per-server log, and everything is also merged into the single
# console/log. When launched with -SingleIp, this script runs in CHILD mode
# and processes exactly that one server (this is how the parent fans out).
# =====================================================================
param([string]$SingleIp)

Clear-Host

$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# ---------------- USER SETTINGS ----------------
# Never hardcoded: PSAUTO_USERNAME/PASSWORD (explicit override from the app's
# UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted
# .env default) is used; a standalone run with neither set prompts instead.
#
# Fail-fast when NON-INTERACTIVE (launched by PS Automation - stdin is a closed
# pipe): if no credentials are available we print a clear error and exit,
# instead of calling Read-Host with no console attached, which would hang the
# whole run forever with zero output. (Same hardening pattern as Non_Raid.ps1.)
$NonInteractive = [Console]::IsInputRedirected

if ($env:PSAUTO_USERNAME) { $User = $env:PSAUTO_USERNAME }
elseif ($env:PSAUTO_DEFAULT_USERNAME) { $User = $env:PSAUTO_DEFAULT_USERNAME }
elseif ($NonInteractive) {
    Write-Host "ERROR: No iDRAC username available (PSAUTO_USERNAME / PSAUTO_DEFAULT_USERNAME are both unset) and there is no console to prompt on." -ForegroundColor Red
    exit 1
}
else { $User = Read-Host "iDRAC username" }

if ($env:PSAUTO_PASSWORD) { $Password = $env:PSAUTO_PASSWORD }
elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $Password = $env:PSAUTO_DEFAULT_PASSWORD }
elseif ($NonInteractive) {
    Write-Host "ERROR: No iDRAC password available (PSAUTO_PASSWORD / PSAUTO_DEFAULT_PASSWORD are both unset) and there is no console to prompt on." -ForegroundColor Red
    exit 1
}
else { $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "iDRAC password" -AsSecureString))) }

# RAID (virtual disk) name. Default is "vDisk1", but the app can override it
# per-run via the PSAUTO_RAID_NAME env var (the "RAID name" form field). A RAID
# MUST have a name, so an explicitly-empty override is rejected (fail fast)
# rather than silently falling back - matching the app's own required-field
# rule. When the var isn't set at all (a plain standalone run), we default.
if ($null -ne $env:PSAUTO_RAID_NAME) {
    $VdName = $env:PSAUTO_RAID_NAME.Trim()
    if ([string]::IsNullOrWhiteSpace($VdName)) {
        Write-Host "ERROR: RAID name (PSAUTO_RAID_NAME) is empty. A RAID must have a name." -ForegroundColor Red
        exit 1
    }
}
else { $VdName = "vDisk1" }
$RaidLevel = "r1"
$PreferredSlots = @(0, 1)

$WaitAfterReboot = $true
$RebootTimeoutMinutes = 25
$StorageApplyTimeoutMinutes = 45
$JobTimeoutMinutes = 45
$MaxStorageReboots = 3
# ------------------------------------------------

if (-not $SingleIp) {
    Write-Host "=== Configure Raid1 (RAID1 + UEFI) - iDRAC9 ===" -ForegroundColor Cyan
    Write-Host "PowerShell: $($PSVersionTable.PSVersion)" -ForegroundColor Cyan
    Write-Host "Flow (per server): Convert Non-RAID -> Reboot/Apply -> Verify Ready -> Create RAID1 -> Reboot/Apply -> UEFI" -ForegroundColor Cyan
}

if (-not (Get-Command racadm -ErrorAction SilentlyContinue)) {
    Write-Host "[FATAL] racadm was not found in PATH." -ForegroundColor Red
    Write-Host "Install Dell RACADM / OpenManage tools or add racadm.exe folder to PATH." -ForegroundColor Yellow
    Exit 1
}

$IpList = New-Object System.Collections.Generic.List[string]
$ReportList = New-Object System.Collections.Generic.List[object]

function Test-IPRangeFormat {
    param([string]$InputStr)
    return [bool]($InputStr -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(-\d{1,3})?$')
}

function Parse-IPRange {
    param([string]$InputStr)
    if ($InputStr -like "*-*") {
        $lastDotIndex = $InputStr.LastIndexOf('.')
        $prefix = $InputStr.Substring(0, $lastDotIndex)
        $rangePart = $InputStr.Substring($lastDotIndex + 1)
        $rangeBounds = $rangePart.Split('-')
        $start = [int]$rangeBounds[0]
        $end = [int]$rangeBounds[1]
        if ($end -lt $start) { throw "Invalid range. End value is smaller than start value." }
        for ($i = $start; $i -le $end; $i++) { $script:IpList.Add("$prefix.$i") }
    }
    else { $script:IpList.Add($InputStr) }
}

function Invoke-Racadm {
    param(
        [Parameter(Mandatory=$true)][string]$Ip,
        [Parameter(Mandatory=$true)][string[]]$Arguments
    )
    $racArgs = @("-r", $Ip, "-u", $script:User, "-p", $script:Password, "--nocertwarn") + $Arguments
    Write-Host "     racadm $($racArgs -join ' ')" -ForegroundColor DarkGray
    $raw = & racadm @racArgs 2>&1
    $exitCode = $LASTEXITCODE
    $output = ($raw | Out-String).Trim()
    return [PSCustomObject]@{ ExitCode=$exitCode; Output=$output; Arguments=$racArgs }
}

function Test-RacadmConnection {
    param([string]$Ip)
    $result = Invoke-Racadm -Ip $Ip -Arguments @("getversion")
    if ($result.ExitCode -eq 0 -and $result.Output -notmatch "(?i)ERROR|Login failed|Unable to connect|authentication|timed out|RAC0218|RAC0224") { return $true }
    return $false
}

function Wait-ForRacadmReady {
    param([string]$Ip, [int]$TimeoutMinutes = 25)
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    Write-Host " [$Ip] Waiting for iDRAC/RACADM to become ready..." -ForegroundColor Yellow
    Start-Sleep -Seconds 45
    while ((Get-Date) -lt $deadline) {
        if (Test-RacadmConnection -Ip $Ip) {
            Write-Host " [$Ip] RACADM is ready." -ForegroundColor Green
            return $true
        }
        Start-Sleep -Seconds 30
    }
    return $false
}

function Test-PowerActionAccepted {
    param([string]$Output)
    return [bool]($Output -match "(?i)successful|success|completed|Server power operation successful|Power operation successful")
}

function Get-ServerPowerStatus {
    param([string]$Ip)
    $status = Invoke-Racadm -Ip $Ip -Arguments @("serveraction", "powerstatus")
    return $status
}

function Restart-ServerPowerCycle {
    param([string]$Ip)

    Write-Host " [$Ip] Checking current server power status..." -ForegroundColor Magenta
    $before = Get-ServerPowerStatus -Ip $Ip
    if ($before.Output) { Write-Host " [$Ip] Power status before reboot: $($before.Output)" -ForegroundColor DarkMagenta }

    # IMPORTANT:
    # Dell RACADM valid reboot/power action is 'powercycle'.
    # 'pwrcycle' is not reliable/valid on iDRAC9 remote RACADM, so v5 uses powercycle.
    Write-Host " [$Ip] Sending serveraction powercycle..." -ForegroundColor Magenta
    $powerCycle = Invoke-Racadm -Ip $Ip -Arguments @("serveraction", "powercycle")

    if ($powerCycle.ExitCode -eq 0 -or (Test-PowerActionAccepted -Output $powerCycle.Output)) {
        Write-Host " [$Ip] Powercycle command accepted. Output: $($powerCycle.Output)" -ForegroundColor Green
        Start-Sleep -Seconds 20
        return $powerCycle
    }

    Write-Host " [$Ip] powercycle was not accepted. Output: $($powerCycle.Output)" -ForegroundColor Yellow
    Write-Host " [$Ip] Trying serveraction hardreset as fallback..." -ForegroundColor Yellow
    $hardReset = Invoke-Racadm -Ip $Ip -Arguments @("serveraction", "hardreset")

    if ($hardReset.ExitCode -eq 0 -or (Test-PowerActionAccepted -Output $hardReset.Output)) {
        Write-Host " [$Ip] Hardreset command accepted. Output: $($hardReset.Output)" -ForegroundColor Green
        Start-Sleep -Seconds 20
        return $hardReset
    }

    Write-Host " [$Ip] hardreset was not accepted. Output: $($hardReset.Output)" -ForegroundColor Yellow
    Write-Host " [$Ip] Trying powerdown + powerup as final fallback..." -ForegroundColor Yellow

    $down = Invoke-Racadm -Ip $Ip -Arguments @("serveraction", "powerdown")
    Start-Sleep -Seconds 20
    $up = Invoke-Racadm -Ip $Ip -Arguments @("serveraction", "powerup")

    if (($down.ExitCode -eq 0 -or (Test-PowerActionAccepted -Output $down.Output)) -and ($up.ExitCode -eq 0 -or (Test-PowerActionAccepted -Output $up.Output))) {
        Write-Host " [$Ip] powerdown + powerup accepted." -ForegroundColor Green
        Start-Sleep -Seconds 20
        return $up
    }

    throw "Failed to reboot/powercycle server. powercycle output: $($powerCycle.Output) | hardreset output: $($hardReset.Output) | powerdown output: $($down.Output) | powerup output: $($up.Output)"
}

function Get-PhysicalDiskIds {
    param([string]$Ip)
    $result = Invoke-Racadm -Ip $Ip -Arguments @("storage", "get", "pdisks")
    if ($result.Output -match "(?i)ERROR|No physical disks") { return @() }
    $matches = [regex]::Matches($result.Output, "Disk\.Bay\.[^\s`r`n]+")
    return @($matches | ForEach-Object { $_.Value.Trim() } | Select-Object -Unique)
}

function Get-DiskInfo {
    param([string]$Ip, [string]$DiskId)
    $result = Invoke-Racadm -Ip $Ip -Arguments @("storage", "get", "pdisks:$DiskId")
    $out = $result.Output
    $isNonRaid = [bool]($out -match "(?i)RaidStatus\s*=\s*Non[- ]?RAID|State\s*=\s*Non[- ]?RAID|\bNon[- ]?RAID\b")
    $pendingConvert = [bool]($out -match "(?i)Pending.*Convert\s+to\s+RAID|Convert\s+to\s+RAID")
    $ready = [bool]($out -match "(?i)RaidStatus\s*=\s*Ready|State\s*=\s*Ready|Status\s*=\s*Ok|Status\s*=\s*OK")
    # A member disk that is part of an APPLIED virtual disk reports State=Online
    # (Ready = free/available, Online = already in an array). This is the most
    # reliable "the RAID1 is really built" signal per-disk.
    $isOnline = [bool]($out -match "(?i)RaidStatus\s*=\s*Online|State\s*=\s*Online")
    return [PSCustomObject]@{ DiskId=$DiskId; Output=$out; IsNonRaid=$isNonRaid; PendingConvert=$pendingConvert; Ready=$ready; IsOnline=$isOnline }
}

function Get-ControllerFromDiskId {
    param([string]$DiskId)
    if ($DiskId -notmatch ":") { return $null }
    return $DiskId.Substring($DiskId.LastIndexOf(':') + 1)
}

function Select-ControllerAndTwoDisks {
    param([string[]]$DiskIds)
    $preferred = @()
    foreach ($slot in $script:PreferredSlots) {
        $match = @($DiskIds | Where-Object { $_ -match "Disk\.Bay\.$slot(:|\b)" } | Select-Object -First 1)
        if ($match.Count -gt 0) { $preferred += $match[0] }
    }
    if ($preferred.Count -ge 2) {
        $ctrl1 = Get-ControllerFromDiskId -DiskId $preferred[0]
        $ctrl2 = Get-ControllerFromDiskId -DiskId $preferred[1]
        if ($ctrl1 -and $ctrl1 -eq $ctrl2) {
            return [PSCustomObject]@{ Controller=$ctrl1; Disk1=$preferred[0]; Disk2=$preferred[1]; Source="Preferred slots $($script:PreferredSlots -join ',')" }
        }
    }
    $groups = @{}
    foreach ($disk in $DiskIds) {
        $ctrl = Get-ControllerFromDiskId -DiskId $disk
        if ([string]::IsNullOrWhiteSpace($ctrl)) { continue }
        if (-not $groups.ContainsKey($ctrl)) { $groups[$ctrl] = New-Object System.Collections.Generic.List[string] }
        $groups[$ctrl].Add($disk)
    }
    foreach ($ctrl in $groups.Keys) {
        if ($groups[$ctrl].Count -ge 2) {
            return [PSCustomObject]@{ Controller=$ctrl; Disk1=$groups[$ctrl][0]; Disk2=$groups[$ctrl][1]; Source="Fallback first two disks on controller" }
        }
    }
    return $null
}

function Get-SelectedDisks {
    param([string]$Ip)
    $diskIds = Get-PhysicalDiskIds -Ip $Ip
    if ($diskIds.Count -lt 2) { throw "Less than 2 physical disks found." }
    $selection = Select-ControllerAndTwoDisks -DiskIds $diskIds
    if ($null -eq $selection) { throw "No controller with at least 2 disks was found." }
    Write-Host " [$Ip] Controller: $($selection.Controller)" -ForegroundColor Cyan
    Write-Host " [$Ip] Disk 1: $($selection.Disk1)" -ForegroundColor Cyan
    Write-Host " [$Ip] Disk 2: $($selection.Disk2)" -ForegroundColor Cyan
    Write-Host " [$Ip] Selection: $($selection.Source)" -ForegroundColor Cyan
    return $selection
}

function Test-OutputMeansCommittedOrPending {
    param([string]$Output)
    return [bool]($Output -match "(?i)STOR023|Configuration already committed|already been committed|A configuration has already been committed|pending")
}

function Test-OutputMeansSuccessOrAccepted {
    param([string]$Output)
    return [bool]($Output -match "(?i)success|successful|JID_|created|pending|committed")
}

function New-JobQueueNow {
    param([string]$Ip, [string]$TargetFqdd)
    return Invoke-Racadm -Ip $Ip -Arguments @("jobqueue", "create", $TargetFqdd, "-s", "TIME_NOW")
}

function Commit-StorageAndReboot {
    param([string]$Ip, [string]$ControllerFqdd, [string]$Reason)
    $job = New-JobQueueNow -Ip $Ip -TargetFqdd $ControllerFqdd
    if ($job.ExitCode -eq 0 -or (Test-OutputMeansSuccessOrAccepted -Output $job.Output) -or (Test-OutputMeansCommittedOrPending -Output $job.Output)) {
        Write-Host " [$Ip] Storage configuration for '$Reason' is committed or already pending. Reboot is required to apply it." -ForegroundColor Yellow
        Restart-ServerPowerCycle -Ip $Ip | Out-Null
        if ($script:WaitAfterReboot) {
            if (-not (Wait-ForRacadmReady -Ip $Ip -TimeoutMinutes $script:RebootTimeoutMinutes)) {
                throw "Server/iDRAC did not become ready after reboot for $Reason."
            }
        }
        return $true
    }
    throw "Failed to create storage job for $Reason. Output: $($job.Output)"
}

function Test-VirtualDiskExists {
    # Name is merely PRESENT in the vdisks list. Note: this is TRUE even for a
    # still-pending "Create Virtual Disk" that hasn't been applied yet - use it
    # only to decide "create vs just commit", never as proof of success.
    param([string]$Ip, [string]$Name)
    $result = Invoke-Racadm -Ip $Ip -Arguments @("storage", "get", "vdisks")
    if ($result.Output -match [regex]::Escape($Name)) { return $true }
    return $false
}

function Get-VDiskState {
    # Returns the State string of the named virtual disk, or $null if the vdisk
    # isn't present at all. A pending (not-yet-applied) vdisk reports a State
    # like "Information Not Available"; a real, applied RAID1 reports "Online".
    param([string]$Ip, [string]$Name)
    $result = Invoke-Racadm -Ip $Ip -Arguments @("storage", "get", "vdisks", "-o", "-p", "Name,State,Status,Layout")
    $out = $result.Output
    if ([string]::IsNullOrWhiteSpace($out)) { return $null }
    $blocks = [regex]::Split($out, "(?=Disk\.Virtual\.)")
    foreach ($b in $blocks) {
        if ($b -match [regex]::Escape($Name)) {
            if ($b -match "(?im)^\s*State\s*=\s*(.+)$") { return $matches[1].Trim() }
            return "Unknown"
        }
    }
    return $null
}

function Test-VirtualDiskApplied {
    # TRUE only when the vdisk is REALLY built - not merely staged/pending.
    # This is the correct success signal (Test-VirtualDiskExists is not, because
    # a pending "Create Virtual Disk" carries the same name).
    param([string]$Ip, [string]$Name)
    $state = Get-VDiskState -Ip $Ip -Name $Name
    if ($null -eq $state) { return $false }
    if ($state -match "(?i)Not Available") { return $false }
    return [bool]($state -match "(?i)Online|Optimal|Ready")
}

function Test-OutputMeansAnotherJobExists {
    # A REAL blocker: a previous storage config job is still committed/pending
    # on the controller, so a new pending op can't be staged yet. Deliberately
    # does NOT match the bare word "pending" - createvd's normal SUCCESS output
    # says "pending" (the vdisk is pending until applied), and treating that as
    # "already committed" was the bug that skipped the apply job entirely.
    param([string]$Output)
    return [bool]($Output -match "(?i)STOR023|already been committed|A configuration has already been committed|Configuration already committed|already exists")
}

function Wait-ForJobQueueToFinish {
    param([string]$Ip, [int]$TimeoutMinutes = 45)
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    Write-Host " [$Ip] Waiting for job queue to finish..." -ForegroundColor Yellow
    while ((Get-Date) -lt $deadline) {
        $result = Invoke-Racadm -Ip $Ip -Arguments @("jobqueue", "view")
        $out = $result.Output

        # Parse ONLY the real "Status=" values of each job. Do NOT substring-
        # match the raw text: 'jobqueue view' prints a "Scheduled Start Time"
        # field for EVERY job (including finished ones), so a raw match on
        # "Scheduled" always hit that label and made this loop spin forever
        # even though every job was already Completed. Matching the Status
        # line specifically avoids that.
        $statuses = [regex]::Matches($out, "(?im)^\s*Status\s*=\s*\[?\s*(?<s>[^\]\r\n]+?)\s*\]?\s*$") |
                    ForEach-Object { $_.Groups['s'].Value.Trim() }

        if ($statuses | Where-Object { $_ -match "(?i)Failed" }) {
            Write-Host " [!] One or more jobs failed on $Ip" -ForegroundColor Red
            Write-Host $out -ForegroundColor DarkRed
            return $false
        }

        # A job is DONE only when its Status is Completed/Cancelled (this also
        # covers "Reboot Completed"). Anything else - Running, Scheduled, New,
        # Ready For Execution, Reboot Pending, Downloading... - is still active.
        $active = @($statuses | Where-Object { $_ -notmatch "(?i)Completed|Cancelled" })
        if ($active.Count -eq 0) {
            Write-Host " [$Ip] Job queue has no running/pending jobs (all Completed)." -ForegroundColor Green
            return $true
        }
        Write-Host " [$Ip] Job(s) still active: $($active -join ', ')" -ForegroundColor DarkGray
        Start-Sleep -Seconds 30
    }
    Write-Host " [!] Timeout waiting for job queue on $Ip" -ForegroundColor Red
    return $false
}

function Wait-UntilDisksRaidCapable {
    param([string]$Ip, [string]$ControllerFqdd, [string]$Disk1, [string]$Disk2)

    # Important: iDRAC can become reachable before PERC applied the storage job.
    # Therefore we poll the disk state itself. If still Pending Convert to RAID, we reboot again.
    for ($attempt = 1; $attempt -le $script:MaxStorageReboots; $attempt++) {
        $deadline = (Get-Date).AddMinutes($script:StorageApplyTimeoutMinutes)
        while ((Get-Date) -lt $deadline) {
            Wait-ForJobQueueToFinish -Ip $Ip -TimeoutMinutes 3 | Out-Null
            $d1 = Get-DiskInfo -Ip $Ip -DiskId $Disk1
            $d2 = Get-DiskInfo -Ip $Ip -DiskId $Disk2

            Write-Host " [$Ip] Disk state check: Disk1 NonRAID=$($d1.IsNonRaid), PendingConvert=$($d1.PendingConvert) | Disk2 NonRAID=$($d2.IsNonRaid), PendingConvert=$($d2.PendingConvert)" -ForegroundColor DarkCyan

            if ((-not $d1.IsNonRaid) -and (-not $d2.IsNonRaid) -and (-not $d1.PendingConvert) -and (-not $d2.PendingConvert)) {
                Write-Host " [$Ip] Both disks are now RAID-capable / ready for Virtual Disk creation." -ForegroundColor Green
                return $true
            }

            Start-Sleep -Seconds 45
        }

        if ($attempt -lt $script:MaxStorageReboots) {
            Write-Host " [$Ip] Disks still show Non-RAID/Pending Convert after waiting. Sending another power cycle. Attempt $attempt/$($script:MaxStorageReboots)." -ForegroundColor Yellow
            Restart-ServerPowerCycle -Ip $Ip | Out-Null
            if (-not (Wait-ForRacadmReady -Ip $Ip -TimeoutMinutes $script:RebootTimeoutMinutes)) {
                throw "Server/iDRAC did not become ready after extra storage reboot."
            }
        }
    }
    return $false
}

function Ensure-DisksAreRaidCapable {
    param([string]$Ip, [object]$Selection)

    $controllerFqdd = $Selection.Controller
    $disk1 = $Selection.Disk1
    $disk2 = $Selection.Disk2

    $d1 = Get-DiskInfo -Ip $Ip -DiskId $disk1
    $d2 = Get-DiskInfo -Ip $Ip -DiskId $disk2

    if ((-not $d1.IsNonRaid) -and (-not $d2.IsNonRaid) -and (-not $d1.PendingConvert) -and (-not $d2.PendingConvert)) {
        Write-Host " [$Ip] Disks are already RAID-capable / Ready." -ForegroundColor Green
        return "Already Ready"
    }

    Write-Host " [$Ip] Disks are Non-RAID or have Pending Convert. Converting to RAID first..." -ForegroundColor Yellow

    foreach ($disk in @($disk1, $disk2)) {
        $info = Get-DiskInfo -Ip $Ip -DiskId $disk
        if ($info.IsNonRaid -or $info.PendingConvert) {
            if ($info.PendingConvert) {
                Write-Host " [$Ip] $disk already has Pending Action: Convert to RAID. Will commit/reboot." -ForegroundColor Yellow
            }
            else {
                $conv = Invoke-Racadm -Ip $Ip -Arguments @("storage", "converttoRAID:$disk")
                if ($conv.ExitCode -ne 0 -and -not (Test-OutputMeansSuccessOrAccepted -Output $conv.Output) -and -not (Test-OutputMeansCommittedOrPending -Output $conv.Output)) {
                    throw "converttoRAID failed for $disk. Output: $($conv.Output)"
                }
            }
        }
    }

    Commit-StorageAndReboot -Ip $Ip -ControllerFqdd $controllerFqdd -Reason "Convert physical disks to RAID" | Out-Null

    if (-not (Wait-UntilDisksRaidCapable -Ip $Ip -ControllerFqdd $controllerFqdd -Disk1 $disk1 -Disk2 $disk2)) {
        throw "Disks are still Non-RAID / Pending Convert after reboot attempts. Open iDRAC Lifecycle Log / Job Queue and check why Convert to RAID did not apply."
    }

    return "Success"
}

function Create-Raid1AndApply {
    param([string]$Ip, [object]$Selection)

    # Only skip when the vDisk is REALLY applied - not merely present/pending.
    if (Test-VirtualDiskApplied -Ip $Ip -Name $script:VdName) {
        Write-Host " [$Ip] $($script:VdName) already exists and is applied. Skipping RAID creation." -ForegroundColor Green
        return "Already Exists"
    }

    for ($try = 1; $try -le 3; $try++) {
        # Re-select after every reboot because FQDD/state can refresh.
        $Selection = Get-SelectedDisks -Ip $Ip
        $controllerFqdd = $Selection.Controller
        $disk1 = $Selection.Disk1
        $disk2 = $Selection.Disk2
        $pdKey = "$disk1,$disk2"

        $d1 = Get-DiskInfo -Ip $Ip -DiskId $disk1
        $d2 = Get-DiskInfo -Ip $Ip -DiskId $disk2

        # If the member disks are already Online AND the vDisk verifies as
        # applied, we're done (covers a re-run after the array was built).
        if ($d1.IsOnline -and $d2.IsOnline -and (Test-VirtualDiskApplied -Ip $Ip -Name $script:VdName)) {
            Write-Host " [$Ip] RAID1 $($script:VdName) is already built (member disks Online)." -ForegroundColor Green
            return "Success"
        }

        if ($d1.IsNonRaid -or $d2.IsNonRaid -or $d1.PendingConvert -or $d2.PendingConvert) {
            Write-Host " [$Ip] RAID create is blocked because disks are still Non-RAID/Pending. Re-applying convert flow." -ForegroundColor Yellow
            Ensure-DisksAreRaidCapable -Ip $Ip -Selection $Selection | Out-Null
            continue
        }

        # Stage the vDisk ONLY if it isn't already present. If a pending vDisk
        # with our name already exists (from a previous try/run), re-running
        # createvd would error - so we skip straight to committing it.
        if (-not (Test-VirtualDiskExists -Ip $Ip -Name $script:VdName)) {
            Write-Host " [$Ip] Creating (staging) RAID1 virtual disk named $($script:VdName)..." -ForegroundColor Yellow
            $raid = Invoke-Racadm -Ip $Ip -Arguments @(
                "storage", "createvd:$controllerFqdd",
                "-rl", $script:RaidLevel,
                "-pdkey:$pdKey",
                "-name", $script:VdName
            )

            # Only a genuine "another job already committed on this controller"
            # is a blocker. createvd's normal success output contains the word
            # "pending" (the vDisk is pending until a job applies it) - that is
            # SUCCESS, not a reason to skip the apply job. (This misclassification
            # was the bug that left the vDisk pending forever.)
            if (Test-OutputMeansAnotherJobExists -Output $raid.Output) {
                Write-Host " [$Ip] A previous storage config job is still committed/pending. Rebooting to clear it, then retrying. Try $try/3" -ForegroundColor Yellow
                Restart-ServerPowerCycle -Ip $Ip | Out-Null
                if (-not (Wait-ForRacadmReady -Ip $Ip -TimeoutMinutes $script:RebootTimeoutMinutes)) { throw "Server/iDRAC did not become ready after committed-config reboot." }
                Wait-ForJobQueueToFinish -Ip $Ip -TimeoutMinutes $script:JobTimeoutMinutes | Out-Null
                continue
            }

            if ($raid.ExitCode -ne 0 -and -not (Test-OutputMeansSuccessOrAccepted -Output $raid.Output)) {
                throw "RAID creation failed. Output: $($raid.Output)"
            }
            Write-Host " [$Ip] vDisk staged (pending). Now committing a job + reboot to APPLY it." -ForegroundColor Cyan
        }
        else {
            Write-Host " [$Ip] $($script:VdName) is already staged (pending). Committing a job + reboot to APPLY it." -ForegroundColor Cyan
        }

        # THIS is what actually builds the array: create the storage config job
        # on the controller and reboot so the PERC applies the pending vDisk.
        Commit-StorageAndReboot -Ip $Ip -ControllerFqdd $controllerFqdd -Reason "Create RAID1 virtual disk" | Out-Null
        Wait-ForJobQueueToFinish -Ip $Ip -TimeoutMinutes $script:JobTimeoutMinutes | Out-Null

        # Verify it is REALLY applied (State Online), not still pending. Poll,
        # because the array can take a little while to finish building after
        # the job completes.
        $deadline = (Get-Date).AddMinutes($script:StorageApplyTimeoutMinutes)
        while ((Get-Date) -lt $deadline) {
            if (Test-VirtualDiskApplied -Ip $Ip -Name $script:VdName) {
                Write-Host " [$Ip] RAID1 virtual disk $($script:VdName) is built and applied." -ForegroundColor Green
                return "Success"
            }
            Start-Sleep -Seconds 30
        }
        Write-Host " [$Ip] $($script:VdName) still not applied after commit + wait. Will retry (extra reboot). Try $try/3" -ForegroundColor Yellow
    }

    throw "RAID1 $($script:VdName) was staged but never became applied after commit + reboots. Check iDRAC Job Queue / Lifecycle Log."
}

function Ensure-UefiBootMode {
    param([string]$Ip)
    Write-Host " [$Ip] Checking BIOS BootMode..." -ForegroundColor Yellow
    $boot = Invoke-Racadm -Ip $Ip -Arguments @("get", "BIOS.BiosBootSettings.BootMode")
    if ($boot.Output -match "(?i)BootMode\s*=\s*UEFI") {
        Write-Host " [$Ip] BootMode is already UEFI." -ForegroundColor Green
        return "Already UEFI"
    }

    Write-Host " [$Ip] Setting BootMode to UEFI..." -ForegroundColor Yellow
    $setUefi = Invoke-Racadm -Ip $Ip -Arguments @("set", "BIOS.BiosBootSettings.BootMode", "Uefi")
    if ($setUefi.ExitCode -ne 0 -and $setUefi.Output -match "(?i)ERROR|Invalid") {
        throw "Failed to set UEFI. Output: $($setUefi.Output)"
    }

    $biosJob = New-JobQueueNow -Ip $Ip -TargetFqdd "BIOS.Setup.1-1"
    if ($biosJob.ExitCode -ne 0 -and $biosJob.Output -notmatch "(?i)JID_|success|created|committed") {
        throw "Failed to create BIOS job. Output: $($biosJob.Output)"
    }

    Restart-ServerPowerCycle -Ip $Ip | Out-Null
    if ($script:WaitAfterReboot) {
        Wait-ForRacadmReady -Ip $Ip -TimeoutMinutes $script:RebootTimeoutMinutes | Out-Null
        Wait-ForJobQueueToFinish -Ip $Ip -TimeoutMinutes $script:JobTimeoutMinutes | Out-Null
    }
    return "Queued/Applied"
}

# The full per-server flow, factored into one function so it can run either
# inline (child mode, -SingleIp) or be fanned out in parallel (parent mode).
function Process-OneServer {
    param([string]$targetIp)

    Write-Host "`n================================================" -ForegroundColor Cyan
    Write-Host ">>> Processing $targetIp" -ForegroundColor Yellow
    Write-Host "================================================" -ForegroundColor Cyan

    $statusConvert = "Not Required"
    $statusRaid    = "Not Required"
    $statusUefi    = "Not Checked"
    $statusJobs    = "Not Required"

    try {
        $pingOk = Test-Connection -ComputerName $targetIp -Count 1 -Quiet -ErrorAction SilentlyContinue
        if (-not $pingOk) { Write-Host " [$targetIp] Ping failed, but continuing with RACADM test..." -ForegroundColor Yellow }
        if (-not (Test-RacadmConnection -Ip $targetIp)) { throw "RACADM connection/authentication failed." }

        # Skip ONLY when the vDisk is truly APPLIED. A still-pending vDisk (from
        # a previous partial run) must NOT be treated as done - fall through so
        # the flow commits + reboots to actually apply it.
        if (Test-VirtualDiskApplied -Ip $targetIp -Name $VdName) {
            Write-Host " [$targetIp] $VdName already exists and is applied. Skipping convert/RAID creation." -ForegroundColor Green
            $statusConvert = "Skipped"
            $statusRaid = "Already Exists"
        }
        else {
            $selection = Get-SelectedDisks -Ip $targetIp
            $statusConvert = Ensure-DisksAreRaidCapable -Ip $targetIp -Selection $selection
            $selection = Get-SelectedDisks -Ip $targetIp
            $statusRaid = Create-Raid1AndApply -Ip $targetIp -Selection $selection
            $statusJobs = "Storage jobs applied / checked"
        }

        $statusUefi = Ensure-UefiBootMode -Ip $targetIp

        return [PSCustomObject]@{
            ServerIP=$targetIp; ConvertDisks=$statusConvert; CreateRAID1=$statusRaid; BootMode=$statusUefi; Jobs=$statusJobs; Result="OK"
        }
    }
    catch {
        Write-Host " [ERROR] $targetIp - $($_.Exception.Message)" -ForegroundColor Red
        return [PSCustomObject]@{
            ServerIP=$targetIp; ConvertDisks=$statusConvert; CreateRAID1=$statusRaid; BootMode=$statusUefi; Jobs=$statusJobs; Result="ERROR: $($_.Exception.Message)"
        }
    }
}

# =====================================================================
# CHILD MODE (-SingleIp): process exactly one server, print a machine-readable
# result line the parent parses, and exit. Everything this prints is streamed
# back (tagged with the IP) by the parent.
# =====================================================================
if ($SingleIp) {
    $r = Process-OneServer -targetIp $SingleIp
    Write-Host ("[RAID-RESULT]" + (@($r.ServerIP, $r.ConvertDisks, $r.CreateRAID1, $r.BootMode, $r.Jobs, $r.Result) -join "|"))
    if ($r.Result -eq "OK") { Exit 0 } else { Exit 1 }
}

# =====================================================================
# PARENT MODE: read the target list, then fan every server out to its own
# child session (parallel), stream each child's tagged output live, and merge
# all results into one report + CSV at the end.
# =====================================================================

# ---------------- IP input ----------------
$AddressesFile = Join-Path $PSScriptRoot "addresses.txt"
$Interactive = -not [Console]::IsInputRedirected

if ((Test-Path $AddressesFile) -and (@(Get-Content $AddressesFile | Where-Object { $_.Trim() })).Count -gt 0) {
    Write-Host "Reading target IPs/ranges from addresses.txt ..." -ForegroundColor Cyan
    foreach ($line in Get-Content $AddressesFile) {
        $line = $line.Trim()
        if (-not $line) { continue }
        if (-not (Test-IPRangeFormat -InputStr $line)) {
            Write-Host " [!] Invalid format ignored: '$line' (expected 192.168.0.120 or 192.168.0.120-140)" -ForegroundColor Red
            continue
        }
        try { Parse-IPRange -InputStr $line } catch { Write-Host " [!] $($_.Exception.Message)" -ForegroundColor Red }
    }
}
elseif ($Interactive) {
    while ($true) {
        Write-Host "------------------------------------------------" -ForegroundColor Gray
        $ipInput = Read-Host "Enter IP address or range, for example 192.168.0.120 or 192.168.0.120-140"
        if ([string]::IsNullOrWhiteSpace($ipInput)) { Write-Host " [!] Input cannot be empty." -ForegroundColor Red; continue }
        if (-not (Test-IPRangeFormat -InputStr $ipInput)) { Write-Host " [!] Invalid format. Use example: 192.168.0.120 or 192.168.0.120-140" -ForegroundColor Red; continue }
        try { Parse-IPRange -InputStr $ipInput } catch { Write-Host " [!] $($_.Exception.Message)" -ForegroundColor Red; continue }
        Write-Host "--> Total IPs in queue: $($IpList.Count)" -ForegroundColor Green
        do {
            Write-Host "1. Add another IP range"
            Write-Host "2. Start execution"
            $option = Read-Host "Selection"
        } until ($option -eq "1" -or $option -eq "2")
        if ($option -eq "2") { break }
    }
}
else {
    Write-Host "ERROR: No addresses.txt found and no interactive console available." -ForegroundColor Red
    Exit 1
}

if ($IpList.Count -eq 0) {
    Write-Host "ERROR: No valid IP addresses to process." -ForegroundColor Red
    Exit 1
}

$IpList = @($IpList | Select-Object -Unique)

# The app reads this to size its per-server progress panel / chips.
Write-Host "[TOTAL-SERVERS] $($IpList.Count)"
Write-Host "`nStarting PARALLEL automation on $($IpList.Count) server(s) - each in its OWN session..." -ForegroundColor Cyan

# Cap how many run at once so a very large fleet doesn't overwhelm the machine
# (each server = a child powershell process doing racadm + reboots). Typical
# fleets are well under this; extras queue and start as slots free up.
$MaxParallel = 15
$ScriptPath  = $PSCommandPath
$results     = @{}
$jobs        = @{}
$queue       = New-Object System.Collections.Generic.Queue[string]
foreach ($ip in $IpList) { $queue.Enqueue($ip) }

function Start-ServerJob {
    param([string]$ip)
    Write-Host "[SERVER-START] $ip"
    # Each server runs in its own child PowerShell process (a separate session)
    # by re-invoking THIS script with -SingleIp. Child inherits the PSAUTO_*
    # credential env vars automatically. 2>&1 folds its stderr into the stream.
    $script:jobs[$ip] = Start-Job -ScriptBlock {
        param($sp, $ip2)
        & powershell -NoProfile -ExecutionPolicy Bypass -File $sp -SingleIp $ip2 2>&1 | ForEach-Object { [string]$_ }
    } -ArgumentList $ScriptPath, $ip
}

function Emit-ChildLine {
    param([string]$ip, $line)
    $text = [string]$line
    if ($text -match '^\[RAID-RESULT\](.+)$') {
        $f = $matches[1] -split '\|'
        $script:results[$ip] = [PSCustomObject]@{
            ServerIP=$f[0]; ConvertDisks=$f[1]; CreateRAID1=$f[2]; BootMode=$f[3]; Jobs=$f[4]; Result=$f[5]
        }
        # App per-server status markers (must NOT be IP-prefixed - the app
        # matches them at the start of the line).
        if ($f[5] -eq "OK") { Write-Host "[SERVER-OK] $ip" }
        else { Write-Host "[SERVER-FAIL] $ip|$($f[5])" }
        return
    }
    # Tag every other line with its server IP so the app can show a per-server
    # view. Strip any leading [ip] the child already added, to avoid doubling.
    $clean = $text -replace ("^\s*\[" + [regex]::Escape($ip) + "\]\s*"), ""
    if ([string]::IsNullOrWhiteSpace($clean)) { Write-Host "[$ip]" }
    else { Write-Host "[$ip] $clean" }
}

# Prime up to the cap, then keep the pool full as jobs finish.
while ($jobs.Count -lt $MaxParallel -and $queue.Count -gt 0) { Start-ServerJob -ip $queue.Dequeue() }

while ($jobs.Count -gt 0) {
    foreach ($ip in @($jobs.Keys)) {
        $job = $jobs[$ip]
        Receive-Job -Job $job | ForEach-Object { Emit-ChildLine -ip $ip -line $_ }
        if ($job.State -eq 'Completed' -or $job.State -eq 'Failed' -or $job.State -eq 'Stopped') {
            Receive-Job -Job $job | ForEach-Object { Emit-ChildLine -ip $ip -line $_ }  # drain remaining
            if (-not $script:results.ContainsKey($ip)) {
                # Child died without printing a result line - record a failure.
                $script:results[$ip] = [PSCustomObject]@{
                    ServerIP=$ip; ConvertDisks="Unknown"; CreateRAID1="Unknown"; BootMode="Unknown"; Jobs="Unknown"; Result="ERROR: child session ended unexpectedly"
                }
                Write-Host "[SERVER-FAIL] $ip|child session ended unexpectedly"
            }
            Remove-Job -Job $job -Force
            $jobs.Remove($ip)
            if ($queue.Count -gt 0) { Start-ServerJob -ip $queue.Dequeue() }
        }
    }
    Start-Sleep -Seconds 2
}

# ---------------- Merged report ----------------
$ReportList = New-Object System.Collections.Generic.List[object]
foreach ($ip in $IpList) { if ($results.ContainsKey($ip)) { $ReportList.Add($results[$ip]) } }

Write-Host "`n=================================================================================" -ForegroundColor Cyan
Write-Host "FINAL EXECUTION REPORT (all servers merged)" -ForegroundColor Cyan
Write-Host "=================================================================================" -ForegroundColor Cyan
$ReportList | Format-Table -AutoSize
Write-Host "=================================================================================" -ForegroundColor Cyan

$csvPath = Join-Path $PSScriptRoot ("Configure_Raid1_Report_{0}.csv" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$ReportList | Export-Csv -Path $csvPath -NoTypeInformation -Encoding UTF8
Write-Host "Report saved to: $csvPath" -ForegroundColor Green

if ($Interactive) {
    Read-Host "Press ENTER to exit"
}

$failedCount = @($ReportList | Where-Object { $_.Result -ne "OK" }).Count
if ($failedCount -eq $ReportList.Count -and $ReportList.Count -gt 0) { Exit 1 } else { Exit 0 }
