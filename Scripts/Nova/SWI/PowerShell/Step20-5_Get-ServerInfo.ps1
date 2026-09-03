$bios = Get-CimInstance Win32_BIOS
$os = Get-CimInstance Win32_OperatingSystem
$comp = Get-CimInstance Win32_ComputerSystem
$reg = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"

# בניית גרסה בפורמט שאתה רוצה
$osVersionFormatted = "$($os.Caption) - Version $($reg.DisplayVersion) (OS Build $($os.BuildNumber).$($reg.UBR))"

[PSCustomObject]@{
    Model            = $comp.Model
    Node             = $env:COMPUTERNAME
    "Service Tag"    = $bios.SerialNumber
    "OS Version"     = $osVersionFormatted
    "Windows License"= $reg.ProductId
    "Product ID"     = $reg.ProductId
    "Product Key"    = (Get-CimInstance SoftwareLicensingService).OA3xOriginalProductKey
}