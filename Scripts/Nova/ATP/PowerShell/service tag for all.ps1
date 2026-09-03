# --- Force Admin Privileges ---
if (!([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# --- Configuration ---
$User = "root"
$Pass = "admin1234"

$Servers = [ordered]@{
    "FM1"    = "192.168.80.122"
    "FM2"    = "192.168.80.123"
    "PMC1"   = "192.168.80.124"
    "PMC2"   = "192.168.80.125"
    "PMC3"   = "192.168.80.126"
    "SRVMGT" = "192.168.80.127"
    "NGINX"  = "192.168.80.128"
}

# Array to store tags for the final clean list
$TagList = @()

Write-Host "`nChecking for racadm.exe..." -ForegroundColor Cyan
if (!(Get-Command racadm.exe -ErrorAction SilentlyContinue)) {
    Write-Host "CRITICAL ERROR: racadm.exe not found in system PATH!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
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

Read-Host "Done. Press Enter to exit"