# ========================================
# STEP 4 - Antivirus Certificate Automation
# ========================================

Write-Host "=== STARTING SCRIPT ===" -ForegroundColor Cyan

# =========================
# Desktop Path
# =========================
$desktop = [Environment]::GetFolderPath("Desktop")
$templatePath = "$desktop\Hub_Anti_Virus_Fill_Word.docx"
$outputPath   = "$desktop\Hub_Anti_Virus_Output.docx"

# =========================
# Check Word template exists
# =========================
if (!(Test-Path $templatePath)) {
    Write-Host "❌ ERROR: Word template not found on Desktop!" -ForegroundColor Red
    exit
}

Write-Host "✅ Word template found"

# =========================
# Servers map
# =========================
$servers = @{
    Device1 = "localhost"
    Device2 = "192.168.80.10"
    Device3 = "192.168.80.20"
    Device4 = "192.168.80.11"
    Device5 = "192.168.80.12"
    Device6 = "192.168.80.13"
    Device7 = "192.168.80.14"
}

# =========================
# Ping test
# =========================
Write-Host "`nChecking connectivity..." -ForegroundColor Yellow

foreach ($device in $servers.Keys) {

    $server = $servers[$device]

    if ($server -eq "localhost") { continue }

    if (Test-Connection -ComputerName $server -Count 1 -Quiet) {
        Write-Host "✅ $device ($server) reachable"
    }
    else {
        Write-Host "❌ ERROR: Cannot reach $device ($server)" -ForegroundColor Red
        exit
    }
}

# =========================
# Credentials
# =========================
$cred = Get-Credential

# =========================
# Collect Data Function
# =========================
$scriptBlock = {

    param($deviceName)

    $hostname = $env:COMPUTERNAME

    # =========================
    # MAC LOGIC
    # =========================

    if ($deviceName -eq "Device2" -or $deviceName -eq "Device3") {

        # 👉 לפי הדרישה שלך - getmac + regex
        $line = getmac /v | findstr /R "^vEthernet.*(Priv"
        $macPrivate = ($line -split '\s+')[2]
    }
    else {
        # שאר השרתים - רגיל
        $line = getmac /v | findstr "Private"
        $macPrivate = ($line -split '\s+')[2]
    }

    # FAB (לכולם)
    $lineFAB = getmac /v | findstr "FAB"
    $macFAB = ($lineFAB -split '\s+')[2]

    # OS
    $os = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    $osVersion = "$($os.ProductName) Version $($os.DisplayVersion) (OS Build $($os.CurrentBuild).$($os.UBR))"

    return @{
        Hostname     = $hostname
        MAC_Internal = $macPrivate
        MAC_External = $macFAB
        OS           = $osVersion
    }
}

# =========================
# Collect data
# =========================
Write-Host "`nCollecting data..." -ForegroundColor Yellow

$results = @{}

foreach ($device in $servers.Keys) {

    $server = $servers[$device]

    try {
        if ($server -eq "localhost") {
            $data = & $scriptBlock $device
        }
        else {
            $data = Invoke-Command -ComputerName $server -Credential $cred -ScriptBlock $scriptBlock -ArgumentList $device
        }

        $results[$device] = $data
        Write-Host "✅ $device collected"
    }
    catch {
        Write-Host "❌ Failed on $device ($server)" -ForegroundColor Red
        exit
    }
}

# =========================
# Open Word
# =========================
Write-Host "`nFilling Word..." -ForegroundColor Yellow

$word = New-Object -ComObject Word.Application
$word.Visible = $false

$doc = $word.Documents.Open($templatePath)

function Replace-Text($find, $replace) {
    $range = $doc.Content
    $range.Find.Execute($find, $false, $false, $false, $false, $false, $true, 1, $false, $replace, 2) | Out-Null
}

foreach ($device in $results.Keys) {

    $data = $results[$device]

    Replace-Text "{{${device}_ComputerName}}" $data.Hostname
    Replace-Text "{{${device}_MAC_Internal}}" $data.MAC_Internal
    Replace-Text "{{${device}_MAC_External}}" $data.MAC_External
    Replace-Text "{{${device}_OS}}" $data.OS
}

# =========================
# Save
# =========================
$doc.SaveAs([ref]$outputPath)
$doc.Close()
$word.Quit()

Write-Host "`n✅ SUCCESS: File created on Desktop!" -ForegroundColor Green