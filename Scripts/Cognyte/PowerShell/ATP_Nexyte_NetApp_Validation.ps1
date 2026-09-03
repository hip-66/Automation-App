# ==============================================================================
# NetApp ONTAP Auto-Report Generator (Sequential Execution & Manual File Build)
# Output file: Downloads\ATP_NETAPP.txt
# ==============================================================================
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Clear-Host
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "      NetApp ONTAP Auto-Report Generator          " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

try {
    # 1. Target IP: the app writes addresses.txt next to this script when
    #    launched through its wizard (the "ips" field) - read that if present,
    #    otherwise fall back to the original interactive prompt for a
    #    standalone/manual run.
    $AddressesFile = Join-Path $PSScriptRoot "addresses.txt"
    if (Test-Path $AddressesFile) {
        $NetAppIP = (Get-Content $AddressesFile | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1).Trim()
    } else {
        $DefaultIP = "192.168.100.208"
        $InputIP   = Read-Host -Prompt "Enter NetApp Cluster IP / Hostname [Default: $DefaultIP]"
        $NetAppIP  = if ([string]::IsNullOrWhiteSpace($InputIP)) { $DefaultIP } else { $InputIP.Trim() }
    }

    # 2. Username/Password: PSAUTO_USERNAME/PSAUTO_PASSWORD are set by the app
    #    (its Username/Password form fields) - fall back to the original
    #    interactive prompts (with the same defaults) for a standalone run.
    $AdminUser = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } else { "admin" }

    if ($env:PSAUTO_PASSWORD) {
        $AdminPass = $env:PSAUTO_PASSWORD
    } else {
        $DefaultPass = "Rel7.xPass!"
        $InputPass   = Read-Host -Prompt "Enter Password [Default: $DefaultPass]"
        $AdminPass   = if ([string]::IsNullOrWhiteSpace($InputPass)) { $DefaultPass } else { $InputPass }
    }

    # 3. Setup Paths: PSAUTO_RUN_OUTPUT_DIR (set by the app to this run's own
    #    dated Outputs folder) takes priority so the report lands where the
    #    app's UI expects it; a standalone run keeps writing to Downloads as
    #    before, since there's no "Outputs" concept outside the app.
    $DownloadsPath = if ($env:PSAUTO_RUN_OUTPUT_DIR) { $env:PSAUTO_RUN_OUTPUT_DIR } else { [System.IO.Path]::Combine($env:USERPROFILE, "Downloads") }
    if (-not (Test-Path $DownloadsPath)) { New-Item -ItemType Directory -Path $DownloadsPath -Force | Out-Null }
    $OutputFile    = Join-Path -Path $DownloadsPath -ChildPath "ATP_NETAPP.txt"
    $PlinkPath     = Join-Path -Path $env:TEMP -ChildPath "plink.exe"

    if (-not (Test-Path $PlinkPath)) {
        Write-Host "Downloading Plink helper utility..." -ForegroundColor Gray
        $PlinkUrl = "https://the.earth.li/~sgtatham/putty/latest/w64/plink.exe"
        Invoke-WebRequest -Uri $PlinkUrl -OutFile $PlinkPath -UseBasicParsing
    }

    # 4. Corrected Command List (100% Valid Syntax)
    $Commands = @(
        "system node run -node * -command sysconfig -a",
        "system node show -fields serialnumber",
        "df -aggregate * -h",
        "vol show",
        "aggr show",
        "df -volume * -h",
        "vol show -fields Vserver,volume,aggregate,state,type,size,available,used,snapshot-policy,fractional-reserve",
        "system node run -node * -command sysconfig -r",
        "lun show",
        "qtree show",
        "igroup show",
        "iscsi show",
        "vserver fcp show",
        "snapmirror show",
        "License show",
        "cifs share show",
        "cifs domain name-mapping-search show",
        "dns show",
        "net int show",
        "net port show",
        "ifgrp show",
        "vserver export-policy show",
        "vserver show",
        "storage failover show",
        "failover-groups show",
        "broadcast-domain show",
        "lun mapping show",
        "fcp adapter show",
        "snapshot policy show",
        "nfs show",
        "route show",
        "vserver object-store-server bucket show -vserver NEXYTE-S3-SVM -bucket nexyte-*",
        "object-store-server user show -vserver NEXYTE-S3-SVM -user nexyte-*"
    )

    # 5. Initialize In-Memory Log Buffer
    $FileContentBuffer = [System.Collections.Generic.List[string]]::new()
    $FileContentBuffer.Add("==================================================")
    $FileContentBuffer.Add("         NETAPP ONTAP AUTO-GENERATED REPORT       ")
    $FileContentBuffer.Add("         Generated on: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    $FileContentBuffer.Add("==================================================`n")

    Write-Host "`nExecuting ONTAP commands sequentially..." -ForegroundColor Yellow
    Write-Host "Collecting full output buffers... Please wait.`n" -ForegroundColor Gray

    $SuccessCount = 0
    $FailedCount  = 0

    # 6. Execute Each Command Separately to Avoid Output Cutoff
    foreach ($Cmd in $Commands) {
        Write-Host "Running: $Cmd ... " -NoNewline -ForegroundColor Gray
        
        # Append Command Headers to Output File
        $FileContentBuffer.Add("==================================================")
        $FileContentBuffer.Add("COMMAND: $Cmd")
        $FileContentBuffer.Add("==================================================")

        # Run Plink command independently and capture entire buffer
        $SingleOutput = & $PlinkPath -batch -ssh -pw "$AdminPass" "$AdminUser@$NetAppIP" "$Cmd" 2>&1

        if ($LastExitCode -eq 0 -or $SingleOutput) {
            Write-Host "[ SUCCESS ]" -ForegroundColor Green
            $FileContentBuffer.AddRange([string[]]$SingleOutput)
            $FileContentBuffer.Add("`n")
            $SuccessCount++
        } else {
            Write-Host "[ FAILED ]" -ForegroundColor Red
            $FileContentBuffer.Add("ERROR: Failed to retrieve data for command.")
            $FileContentBuffer.Add("`n")
            $FailedCount++
        }
    }

    # 7. Manually Write Complete Text Buffer to Output File
    if (-not (Test-Path $DownloadsPath)) {
        New-Item -ItemType Directory -Path $DownloadsPath -Force | Out-Null
    }

    [System.IO.File]::WriteAllLines($OutputFile, $FileContentBuffer, [System.Text.Encoding]::UTF8)

    # 8. Final Report Status
    Write-Host "`n=================== EXECUTION SUMMARY ===================" -ForegroundColor Cyan
    $SummaryColor = if ($FailedCount -eq 0) { "Green" } else { "Yellow" }
    Write-Host "Summary: $SuccessCount Commands Succeeded | $FailedCount Failed" -ForegroundColor $SummaryColor

    if (Test-Path $OutputFile) {
        $FileObj = Get-Item $OutputFile
        $SizeKB  = [math]::Round($FileObj.Length / 1KB, 2)
        Write-Host "`nReport file successfully generated and saved!" -ForegroundColor Cyan
        Write-Host "File Location: $($FileObj.FullName)" -ForegroundColor White
        Write-Host "File Size    : $SizeKB KB" -ForegroundColor White
    }

}
catch {
    Write-Host "`nFatal Error: $_" -ForegroundColor Red
}
finally {
    Write-Host ""
    Write-Host "--------------------------------------------------" -ForegroundColor DarkGray
    Read-Host -Prompt "Press Enter to exit"
}