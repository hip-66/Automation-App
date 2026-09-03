# ===============================================
# PowerShell Script: run_all.ps1
# Purpose: Disable UAC, Firewall, Defender, RDP NLA, IE ESC, set power plan, etc.
# Run as Administrator
# ===============================================

function Execute-Step {
    param (
        [string]$Description,
        [scriptblock]$Command
    )

    Write-Host "Running: $Description" -ForegroundColor Cyan
    try {
        & $Command
        Write-Host "SUCCESS: $Description`n" -ForegroundColor Green
    }
    catch {
        Write-Host "FAILED: $Description`n" -ForegroundColor Red
    }
}

# --------------------------
# UAC Settings
# --------------------------
Execute-Step "Disable UAC Consent Prompt for Admins" {
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
    -Name "ConsentPromptBehaviorAdmin" -Value 0
}

Execute-Step "Disable Secure Desktop Prompt" {
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
    -Name "PromptOnSecureDesktop" -Value 0
}

# --------------------------
# Firewall Settings
# --------------------------
Execute-Step "Disable Windows Firewall (All Profiles)" {
    Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False
}

Execute-Step "Verify Firewall Status" {
    Get-NetFirewallProfile | Select Name, Enabled
}

Execute-Step "Disable Firewall and Allow All Traffic" {
   Set-NetFirewallProfile -All -Enabled False -PolicyStore $env:COMPUTERNAME -DefaultInboundAction Allow -DefaultOutboundAction Allow
}

Execute-Step "Verify Firewall Policy Settings" {
    Get-NetFirewallProfile -All -PolicyStore $env:COMPUTERNAME | Select Enabled, DefaultInboundAction, DefaultOutboundAction
}

# --------------------------
# Microsoft Defender
# --------------------------
Execute-Step "Disable Defender Real-Time Protection (Temporary)" {
    Set-MpPreference -DisableRealtimeMonitoring $true
}

Execute-Step "Verify Defender Real-Time Protection" {
    Get-MpPreference | Select DisableRealtimeMonitoring
}

Execute-Step "Disable Defender Cloud Protection" {
    Set-MpPreference -MAPSReporting 0
}

Execute-Step "Disable Automatic Sample Submission" {
    Set-MpPreference -SubmitSamplesConsent 2
}

# Disable Real-Time Protection via Policy (Permanent)
Execute-Step "Disable Defender Real-Time Protection via Policy" {
    $DefenderPolicy = "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection"
    if (-not (Test-Path $DefenderPolicy)) {
        New-Item -Path $DefenderPolicy -Force | Out-Null
    }
    Set-ItemProperty -Path $DefenderPolicy -Name "DisableRealtimeMonitoring" -Value 1
    # Restart Defender service to apply
    Restart-Service WinDefend -Force
}

# --------------------------
# Remote Desktop Settings
# --------------------------
Execute-Step "Enable Remote Desktop" {
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server" `
    -Name "fDenyTSConnections" -Value 0
}

Execute-Step "Disable RDP Network Level Authentication" {
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" `
    -Name "UserAuthentication" -Value 0
}

# --------------------------
# IE Enhanced Security Configuration
# --------------------------
Execute-Step "Disable IE ESC for Administrators" {
    $adminPath = "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{AEB6717E-7E19-11d0-97EE-00C04FD91972}"
    if (Test-Path $adminPath) {
        Set-ItemProperty -Path $adminPath -Name "IsInstalled" -Value 0
    } else {
        Write-Host "Registry key for IE ESC Administrators not found. Skipping..." -ForegroundColor Yellow
    }
}

Execute-Step "Disable IE ESC for Users" {
    $userPath = "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{AEB6717E-7E19-11d0-97EE-00C04FD91973}"
    if (Test-Path $userPath) {
        Set-ItemProperty -Path $userPath -Name "IsInstalled" -Value 0
    } else {
        Write-Host "Registry key for IE ESC Users not found. Skipping..." -ForegroundColor Yellow
    }
}

Execute-Step "Restart Explorer Process" {
    Stop-Process -Name explorer -Force
}

# --------------------------
# Power Settings
# --------------------------
Execute-Step "Set Power Plan to High Performance" {
    powercfg -setactive SCHEME_MIN
}

Execute-Step "Disable Monitor Timeout (AC)" {
    powercfg -change -monitor-timeout-ac 0
}

Execute-Step "Disable Sleep Timeout (AC)" {
    powercfg -change -standby-timeout-ac 0
}

# --------------------------
# Completion & Restart Prompt
# --------------------------
Write-Host "`nAll steps completed." -ForegroundColor Yellow
$restart = Read-Host "Do you want to restart the computer now? (Y/N)"

if ($restart -eq "Y" -or $restart -eq "y") {
    Write-Host "Restarting..." -ForegroundColor Cyan
    Restart-Computer
} else {
    Write-Host "Restart skipped." -ForegroundColor Yellow
}