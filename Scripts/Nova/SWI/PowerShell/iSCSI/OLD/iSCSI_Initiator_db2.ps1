$InitiatorIP = '10.11.11.14'
$Portals = '10.11.11.3','10.11.11.4','10.11.11.5','10.11.11.6'
$TargetIQN = 'iqn.1988-11.com.dell:01.array.bc305b68b91a'

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


Get-IscsiSession | Select-Object TargetNodeAddress,InitiatorPortalAddress,TargetPortalAddress