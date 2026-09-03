$InitiatorIP = (Get-NetIPAddress | Where-Object {$_.IPv4Address -like "10.11.11.*"}).IPAddress
$Portals = '10.11.11.3','10.11.11.4','10.11.11.5','10.11.11.6'
$TargetIQN = Read-Host "Enter iqn for Curent rack`n"




$Portals | ForEach-Object {
    New-IscsiTargetPortal -TargetPortalAddress $_ -TargetPortalPortNumber 3260 -InitiatorPortalAddress $InitiatorIP
}

$Portals | ForEach-Object {
    Connect-IscsiTarget `
        -NodeAddress $TargetIQN `
        -TargetPortalAddress $_ `
        -TargetPortalPortNumber 3260 `
        -InitiatorPortalAddress $InitiatorIP `
        -InitiatorInstanceName 'ROOT\ISCSIPRT\0000_0' `
        -IsMultipathEnabled $true `
        -IsPersistent $true
}

Get-IscsiSession |
    Where-Object { $_.TargetNodeAddress -eq $TargetIQN } |
    Select-Object TargetNodeAddress,InitiatorPortalAddress,TargetPortalAddress,SessionIdentifier

Read-Host