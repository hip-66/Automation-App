# Requires Administrator privileges

$ScriptDir = $PSScriptRoot
$addressesPath = Join-Path $ScriptDir "addresses.txt"

$adapters = @(
    "ConnectX Family mlx5Gen Virtual Function",
    "ConnectX  Family mlx5Gen Virtual Function #2"
)

if (Test-Path $addressesPath) {
    $fileAdapters = Get-Content $addressesPath | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
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