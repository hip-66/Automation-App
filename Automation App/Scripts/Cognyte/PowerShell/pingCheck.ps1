# ==========================================
# PowerShell IP Ping Scanner (Adaptive Version Engine)
# - Runs non-interactively when launched from PS Automation (reads target
#   IPs/ranges from addresses.txt, written by the app from the "List of IP
#   Addresses" field).
# - Falls back to the original interactive prompts when double-clicked
#   manually (no addresses.txt / no redirected console).
# ==========================================

function Expand-Entry([string]$Entry) {
    $result = @()
    $Entry = $Entry.Trim()
    if ($Entry -match '^(\d+\.\d+\.\d+\.)(\d+)-(\d+)$') {
        $BaseIP = $matches[1]
        $StartIP = [int]$matches[2]
        $EndIP = [int]$matches[3]
        if ($StartIP -le $EndIP) {
            foreach ($Number in $StartIP..$EndIP) { $result += "$BaseIP$Number" }
        } else {
            Write-Host "Error: Start IP must be less than or equal to End IP in '$Entry'." -ForegroundColor Red
        }
    } elseif ($Entry -match '^\d+\.\d+\.\d+\.\d+$') {
        $result += $Entry
    } elseif ($Entry) {
        Write-Host "Invalid format ignored: '$Entry' (expected 192.168.0.120-140 or a plain IP)" -ForegroundColor Red
    }
    return $result
}

$IPList = @()
$AddressesFile = Join-Path $PSScriptRoot "addresses.txt"
$Interactive = -not [Console]::IsInputRedirected

if ((Test-Path $AddressesFile) -and (@(Get-Content $AddressesFile | Where-Object { $_.Trim() })).Count -gt 0) {
    # Non-interactive: driven by the PS Automation app
    Write-Host "Reading target IPs/ranges from addresses.txt ..." -ForegroundColor Cyan
    foreach ($Line in Get-Content $AddressesFile) {
        $IPList += Expand-Entry $Line
    }
}
elseif ($Interactive) {
    # Manual double-click run: original interactive prompt loop
    Clear-Host
    do {
        $Range = Read-Host "Enter IP Range (Example: 192.168.0.120-140)"
        $IPList += Expand-Entry $Range

        Write-Host ""
        Write-Host "1 - Add another IP range"
        Write-Host "2 - Start scan"
        $Option = Read-Host "Select an option"
    } while ($Option -eq "1")
}
else {
    Write-Host "ERROR: No addresses.txt found and no interactive console available." -ForegroundColor Red
    exit 1
}

if ($IPList.Count -eq 0) {
    Write-Host "ERROR: No valid IP addresses to scan." -ForegroundColor Red
    exit 1
}

Write-Host ""
# Detect the major version of PowerShell
$PSMajorVersion = $PSVersionTable.PSVersion.Major
Write-Host "Detected PowerShell Version: $PSMajorVersion" -ForegroundColor Cyan
Write-Host "Starting scan..." -ForegroundColor Yellow
Write-Host ""

$Results = @()

# Initialize .NET object only if running on older PowerShell 5.1
if ($PSMajorVersion -le 5) {
    $PingSender = New-Object System.Net.NetworkInformation.Ping
    $Timeout = 1000
}

foreach ($IP in $IPList) {
    $PingResult = $null

    if ($PSMajorVersion -ge 7) {
        # Execution logic optimized for PowerShell 7+
        $PingResult = Test-Connection -ComputerName $IP -Count 1 -TimeoutMilliSec 1000 -Quiet -ErrorAction SilentlyContinue
    }
    else {
        # Execution logic optimized for Windows PowerShell 5.1
        try {
            $Reply = $PingSender.Send($IP, $Timeout)
            $PingResult = ($Reply.Status -eq "Success")
        }
        catch {
            $PingResult = $false
        }
    }

    if ($PingResult) {
        Write-Host "[SUCCESS] $IP is reachable." -ForegroundColor Green

        $Results += [PSCustomObject]@{
            IPAddress = $IP
            Status    = "Success"
            Reachable = "Yes"
            ScanTime  = Get-Date
        }
    }
    else {
        Write-Host "[FAILED ] $IP is unreachable." -ForegroundColor Red

        $Results += [PSCustomObject]@{
            IPAddress = $IP
            Status    = "Failed"
            Reachable = "No"
            ScanTime  = Get-Date
        }
    }
}

# Generate report file name and export results to CSV
$ReportFile = ".\PingReport_$(Get-Date -Format 'yyyyMMdd_HHmmss').csv"
$Results | Export-Csv -Path $ReportFile -NoTypeInformation -Encoding UTF8

# Calculate summary metrics
$SuccessCount = ($Results | Where-Object { $_.Status -eq "Success" }).Count
$FailedCount = ($Results | Where-Object { $_.Status -eq "Failed" }).Count

# Display final summary on screen
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "Scan Completed"
Write-Host "------------------------------------------"
Write-Host "Total IP Addresses : $($Results.Count)"
Write-Host "Reachable          : $SuccessCount" -ForegroundColor Green
Write-Host "Unreachable        : $FailedCount" -ForegroundColor Red
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Report saved to:"
Write-Host $ReportFile -ForegroundColor Yellow
Write-Host ""

# Only pause for a keypress on a real interactive console - when launched by
# PS Automation, stdin is a closed pipe and ReadKey would throw and mark a
# successful scan as failed.
if ($Interactive) {
    Write-Host "Press any key to exit..." -ForegroundColor White
    $null = [System.Console]::ReadKey($true)
}

# Unreachable servers are just scan results, not a tool failure - the scan
# itself succeeded as long as it ran to completion.
exit 0
