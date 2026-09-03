# Requires Administrator privileges

# Target adapter name(s): PS Automation can supply these via addresses.txt
# (one adapter name per line) written into this script's folder before it is
# launched. A standalone (double-click) run with no addresses.txt present
# falls back to the original hardcoded Mellanox ConnectX-6 Lx Adapter pair
# below, unchanged.
$ScriptDir = $PSScriptRoot
$addressesFile = Join-Path $ScriptDir "addresses.txt"

$adapters = @(
    "Mellanox ConnectX-6 Lx Adapter",
    "Mellanox ConnectX-6 Lx Adapter #2"
)

if (Test-Path $addressesFile) {
    $fileAdapters = Get-Content $addressesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    if ($fileAdapters -and $fileAdapters.Count -gt 0) {
        $adapters = @($fileAdapters)
    }
}

foreach ($adapter in $adapters) {
    Write-Host "Configuring $adapter..." -ForegroundColor Cyan

    # Jumbo Packet = 9014
    Set-NetAdapterAdvancedProperty -Name $adapter `
        -DisplayName "Jumbo Packet" `
        -DisplayValue "9014 Bytes" -ErrorAction SilentlyContinue

    # Disable LSO IPv4
    Set-NetAdapterAdvancedProperty -Name $adapter `
        -DisplayName "Large Send Offload V2 (IPv4)" `
        -DisplayValue "Disabled" -ErrorAction SilentlyContinue

    # Disable LSO IPv6
    Set-NetAdapterAdvancedProperty -Name $adapter `
        -DisplayName "Large Send Offload V2 (IPv6)" `
        -DisplayValue "Disabled" -ErrorAction SilentlyContinue

    Write-Host "$adapter configured successfully." -ForegroundColor Green
}

Write-Host "All done." -ForegroundColor Yellow