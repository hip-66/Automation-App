<#
Convert-ReadyDisks-To-NonRaid_v3.ps1
Converts only Physical Disks with State = Ready to Non-RAID using Dell RACADM.
Does NOT touch Online disks.
#>

# Credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the app's UI)
# wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted .env
# default) is used; a standalone run with neither set prompts instead - never
# hardcoded in this file.
param(
    [string]$IdracIPs,
    [string]$Username,
    [string]$Password,
    [switch]$DryRun,
    [string]$RacadmPath = "racadm.exe"
)

$ErrorActionPreference = "Stop"
$NonInteractive = [Console]::IsInputRedirected

# Credentials are resolved here as TOP-LEVEL statements (NOT as param()
# default-value expressions, which is how this used to be written) so that,
# if the app's default-credential injection ever fails for any reason, this
# FAILS FAST with a clear message instead of calling Read-Host with no
# console attached and no way to ever answer it - previously that combination
# hung forever with ZERO output, since parameter binding (where Read-Host used
# to live) happens before the script body can print anything at all.
if ([string]::IsNullOrWhiteSpace($Username)) {
    if ($env:PSAUTO_USERNAME) { $Username = $env:PSAUTO_USERNAME }
    elseif ($env:PSAUTO_DEFAULT_USERNAME) { $Username = $env:PSAUTO_DEFAULT_USERNAME }
    elseif ($NonInteractive) {
        Write-Host "ERROR: No iDRAC username available (PSAUTO_USERNAME / PSAUTO_DEFAULT_USERNAME are both unset) and there is no console to prompt on." -ForegroundColor Red
        exit 1
    }
    else { $Username = Read-Host "iDRAC username" }
}
if ([string]::IsNullOrWhiteSpace($Password)) {
    if ($env:PSAUTO_PASSWORD) { $Password = $env:PSAUTO_PASSWORD }
    elseif ($env:PSAUTO_DEFAULT_PASSWORD) { $Password = $env:PSAUTO_DEFAULT_PASSWORD }
    elseif ($NonInteractive) {
        Write-Host "ERROR: No iDRAC password available (PSAUTO_PASSWORD / PSAUTO_DEFAULT_PASSWORD are both unset) and there is no console to prompt on." -ForegroundColor Red
        exit 1
    }
    else { $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "iDRAC password" -AsSecureString))) }
}

function Pause-End {
    # Only pause when a real console is attached. Launched by PS Automation,
    # stdin is a closed pipe, so skip the prompt (avoids hanging/erroring).
    if (-not [Console]::IsInputRedirected) {
        Write-Host ""
        Read-Host "Press ENTER to close"
    }
}

function Expand-IPList {
    param([string]$InputText)

    $result = New-Object System.Collections.Generic.List[string]
    $parts = $InputText.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }

    foreach ($part in $parts) {
        if ($part -match '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.)(\d{1,3})-(\d{1,3})$') {
            $prefix = $matches[1]
            $start = [int]$matches[2]
            $end = [int]$matches[3]
            if ($start -gt $end) { throw "Invalid IP range: $part" }
            for ($i = $start; $i -le $end; $i++) { $result.Add("$prefix$i") }
        }
        elseif ($part -match '^(\d{1,3}\.){3}\d{1,3}$') {
            $result.Add($part)
        }
        else {
            throw "Invalid IP format: $part"
        }
    }
    return $result
}

function Invoke-Racadm {
    param(
        [string]$Ip,
        [string[]]$SubCommand,
        [switch]$ShowCommand
    )

    # --nocertwarn goes BEFORE -r. ($racArgs, not $args - $args is a PowerShell
    # automatic variable and must not be reused.)
    $racArgs = @("--nocertwarn", "-r", $Ip, "-u", $Username, "-p", $Password) + $SubCommand

    if ($ShowCommand) {
        $safeArgs = @("--nocertwarn", "-r", $Ip, "-u", $Username, "-p", "********") + $SubCommand
        Write-Host ("racadm.exe " + ($safeArgs -join " ")) -ForegroundColor DarkGray
    }

    $output = & $RacadmPath @racArgs 2>&1
    $exitCode = $LASTEXITCODE

    return [PSCustomObject]@{
        ExitCode = $exitCode
        Output   = ($output | Out-String)
    }
}

function Parse-PDisks {
    param([string]$Text)

    $disks = New-Object System.Collections.Generic.List[object]
    $current = $null

    foreach ($rawLine in ($Text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }

        # Object line usually looks like: Disk.Bay.0:Enclosure.Internal.0-1:RAID.SL.3-1
        if ($line -match '^Disk\.') {
            if ($null -ne $current) { $disks.Add([PSCustomObject]$current) }
            $current = [ordered]@{
                FQDD  = $line
                State = $null
                Name  = $null
                Size  = $null
            }
            continue
        }

        if ($null -ne $current -and $line -match '^([^=]+?)\s*=\s*(.*)$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim()
            switch -Regex ($key) {
                '^State$' { $current.State = $val; break }
                '^Name$'  { $current.Name  = $val; break }
                '^Size$'  { $current.Size  = $val; break }
            }
        }
    }

    if ($null -ne $current) { $disks.Add([PSCustomObject]$current) }
    return $disks
}

function Parse-Controllers {
    param([string]$Text)

    $controllers = New-Object System.Collections.Generic.List[string]
    foreach ($rawLine in ($Text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if ($line -match '^RAID\.') { $controllers.Add($line) }
    }
    return $controllers | Select-Object -Unique
}

try {
    Clear-Host
    Write-Host "=== Convert Ready Physical Disks to Non-RAID - v3 ===" -ForegroundColor Cyan
    Write-Host ""

    # Check racadm exists
    $cmd = Get-Command $RacadmPath -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "racadm.exe was not found. Install Dell iDRAC Tools / RACADM or run this script from the folder that contains racadm.exe."
    }

    # Target IPs: prefer addresses.txt (written by PS Automation from the IP
    # list OR the range you pick in the form - one IP per line, ranges already
    # expanded). Fall back to the -IdracIPs parameter, then an interactive
    # prompt for manual double-click runs.
    $NonInteractive = [Console]::IsInputRedirected
    if ([string]::IsNullOrWhiteSpace($IdracIPs)) {
        $addressesFile = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "addresses.txt"
        if (Test-Path $addressesFile) {
            $fileIps = Get-Content $addressesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
            if ($fileIps.Count -gt 0) { $IdracIPs = ($fileIps -join ",") }
        }
    }
    if ([string]::IsNullOrWhiteSpace($IdracIPs)) {
        if ($NonInteractive) {
            Write-Host "ERROR: No iDRAC IPs provided (addresses.txt is empty)." -ForegroundColor Red
            exit 1
        }
        $IdracIPs = Read-Host "Enter iDRAC IP/range. Example: 192.168.0.120 or 192.168.0.120-140"
    }

    $ips = Expand-IPList -InputText $IdracIPs
    Write-Host ""
    Write-Host "Total iDRAC IPs: $($ips.Count)" -ForegroundColor Green
    if ($DryRun) { Write-Host "Mode: DRY RUN - no changes will be made" -ForegroundColor Yellow } else { Write-Host "Mode: LIVE" -ForegroundColor Yellow }
    Write-Host "IPs: $($ips -join ', ')" -ForegroundColor Cyan

    if (-not $DryRun) {
        Write-Host ""
        Write-Host "This will convert ONLY disks with State=Ready to Non-RAID." -ForegroundColor Yellow
        Write-Host "Online disks will NOT be converted." -ForegroundColor Yellow
        if ($NonInteractive) {
            # Launched by PS Automation - the Run (and its confirmation) already
            # happened in the app UI, and there is no console to type YES into,
            # so proceed automatically instead of self-cancelling.
            Write-Host "Launched by PS Automation (Run already confirmed) - proceeding..." -ForegroundColor Cyan
        }
        else {
            $confirm = Read-Host "Type YES to continue"
            if ($confirm -ne "YES") {
                Write-Host "Cancelled by user." -ForegroundColor Yellow
                exit 0
            }
        }
    }

    $hostOk = 0
    $hostFail = 0
    foreach ($ip in $ips) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor Cyan
        Write-Host ">>> Processing iDRAC $ip" -ForegroundColor Cyan
        Write-Host "============================================================" -ForegroundColor Cyan

        # Get physical disks using exact working syntax format
        $pdResult = Invoke-Racadm -Ip $ip -SubCommand @("storage", "get", "pdisks", "-o", "-p", "State,Name,Size") -ShowCommand

        if ($pdResult.ExitCode -ne 0 -or $pdResult.Output -match 'ERROR|RAC\d+') {
            Write-Host "ERROR: Failed to get physical disks from $ip." -ForegroundColor Red
            Write-Host "---- RACADM OUTPUT START ----" -ForegroundColor DarkYellow
            Write-Host $pdResult.Output
            Write-Host "---- RACADM OUTPUT END ----" -ForegroundColor DarkYellow
            Write-Host "Try this manually:" -ForegroundColor Yellow
            Write-Host "racadm.exe --nocertwarn -r $ip -u $Username -p `"$Password`" storage get pdisks -o -p State,Name,Size" -ForegroundColor Yellow
            $hostFail++
            continue
        }
        $hostOk++

        $disks = Parse-PDisks -Text $pdResult.Output
        if (-not $disks -or $disks.Count -eq 0) {
            Write-Host "No physical disks parsed. Raw output:" -ForegroundColor Yellow
            Write-Host $pdResult.Output
            continue
        }

        Write-Host "Physical disks found:" -ForegroundColor Green
        $disks | Format-Table FQDD, State, Size, Name -AutoSize

        $readyDisks = @($disks | Where-Object { $_.State -eq "Ready" })
        if ($readyDisks.Count -eq 0) {
            Write-Host "No disks in Ready state. Nothing to convert on $ip." -ForegroundColor Yellow
            continue
        }

        Write-Host "Disks that will be converted to Non-RAID:" -ForegroundColor Magenta
        $readyDisks | Format-Table FQDD, State, Size, Name -AutoSize

        foreach ($disk in $readyDisks) {
            $fqdd = $disk.FQDD
            $sub = @("storage", "converttononraid:$fqdd")

            if ($DryRun) {
                Write-Host "DRYRUN: racadm.exe --nocertwarn -r $ip -u $Username -p ******** storage converttononraid:$fqdd" -ForegroundColor Yellow
            }
            else {
                Write-Host "Converting $fqdd to Non-RAID..." -ForegroundColor Cyan
                $conv = Invoke-Racadm -Ip $ip -SubCommand $sub -ShowCommand
                Write-Host $conv.Output
            }
        }

        # Create job on all detected controllers
        $ctrlResult = Invoke-Racadm -Ip $ip -SubCommand @("storage", "get", "controllers", "-o") -ShowCommand
        $controllers = Parse-Controllers -Text $ctrlResult.Output

        if ($controllers.Count -eq 0) {
            Write-Host "Could not detect controller automatically. Raw controller output:" -ForegroundColor Yellow
            Write-Host $ctrlResult.Output
            Write-Host "You may need to create the job manually, for example:" -ForegroundColor Yellow
            Write-Host "racadm.exe --nocertwarn -r $ip -u $Username -p `"$Password`" jobqueue create RAID.SL.3-1 -r pwrcycle" -ForegroundColor Yellow
            continue
        }

        foreach ($ctrl in $controllers) {
            if ($DryRun) {
                Write-Host "DRYRUN: racadm.exe --nocertwarn -r $ip -u $Username -p ******** jobqueue create $ctrl -r pwrcycle" -ForegroundColor Yellow
            }
            else {
                Write-Host "Creating job and power cycle for controller $ctrl ..." -ForegroundColor Cyan
                $job = Invoke-Racadm -Ip $ip -SubCommand @("jobqueue", "create", $ctrl, "-r", "pwrcycle") -ShowCommand
                Write-Host $job.Output
            }
        }
    }

    Write-Host ""
    Write-Host "Done. Reachable hosts: $hostOk, failed: $hostFail (of $($ips.Count))." -ForegroundColor Green
    if ($ips.Count -gt 0 -and $hostOk -eq 0) {
        exit 1   # could not reach/query any host - report a real failure, not success
    }
    exit 0
}
catch {
    Write-Host ""
    Write-Host "FATAL ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    Pause-End
}
