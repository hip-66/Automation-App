# Requires Administrator privileges

Write-Host "Setting power plan to High Performance..."

# Get High Performance GUID (if exists)
$highPerf = powercfg -l | Select-String "High performance"

if ($highPerf) {
    $guid = ($highPerf -split '\s+')[3]
    powercfg -setactive $guid
    Write-Host "High Performance plan activated."
} else {
    Write-Host "High Performance plan not found. Creating it..."
    $guid = powercfg -duplicatescheme SCHEME_MIN
    powercfg -setactive $guid
}

Write-Host "Setting display timeout to NEVER..."

# Set display timeout to 0 (Never) for both AC and DC
powercfg -change -monitor-timeout-ac 0
powercfg -change -monitor-timeout-dc 0

Write-Host "Done!"