# Requires Administrator privileges

$adapters = @(
    "Mellanox ConnectX-6 Lx Adapter",
    "Mellanox ConnectX-6 Lx Adapter #2"
)

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