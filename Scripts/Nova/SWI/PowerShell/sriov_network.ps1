#adds sr-iov nics to VMs on the hopst 
$vm=(Get-VM).Name
$vm | ForEach-Object {
    Add-VMNetworkAdapter -VMName $_ -SwitchName "FAB"
    Add-VMNetworkAdapter -VMName $_ -SwitchName "iSCSI"
    Get-VMNetworkAdapter -VMName $_ | Where-Object {$_.SwitchName -eq "FAB"} | Set-VMNetworkAdapter -IovWeight 100
    Get-VMNetworkAdapter -VMName $_ | Where-Object {$_.SwitchName -eq "iSCSI"} | Set-VMNetworkAdapter -IovWeight 100
 }

# Get all VMs on the host
$VMs = Get-VM

ForEach ($VM in $VMs) {
    Write-Host "---------------------------------------------" -ForegroundColor Green
    Write-Host " PASTE THESE COMMANDS INSIDE: $($VM.Name)" -ForegroundColor Green
    Write-Host "---------------------------------------------"

    # Get adapters for this VM, skipping 'Private'
    $Adapters = Get-VMNetworkAdapter -VMName $VM.Name | Where-Object { $_.SwitchName -ne 'Private' }

    ForEach ($Adapter in $Adapters) {
        # 1. Grab the raw MAC (e.g., 00155DE3132F)
        $RawMac = $Adapter.MacAddress
        
        # 2. Add dashes to match the VM format (e.g., 00-15-5D-E3-13-2F)
        # The regex inserts a '-' after every 2 characters, except at the very end
        $FormattedMac = $RawMac -replace '..(?!$)', '$&-'
        
        # 3. Generate the command using the Formatted MAC
        $Command = "Get-NetAdapter | Where-Object { `$_.MacAddress -eq '$FormattedMac' -and `$_.InterfaceDescription -like 'Microsoft Hyper-V*' } | Rename-NetAdapter -NewName '$($Adapter.SwitchName)'"
        
        Write-Host $Command
    }
    Write-Host "" 
}

Read-Host


