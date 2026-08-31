# Credentials are never hardcoded here: PSAUTO_USERNAME/PASSWORD (explicit
# override from the app's UI) wins; otherwise PSAUTO_DEFAULT_SSH_USERNAME/
# PASSWORD (the app's encrypted .env default) is used; a standalone console
# run with neither set prompts instead.
$NonInteractive = [Console]::IsInputRedirected

$user     = if ($env:PSAUTO_USERNAME) { $env:PSAUTO_USERNAME } elseif ($env:PSAUTO_DEFAULT_SSH_USERNAME) { $env:PSAUTO_DEFAULT_SSH_USERNAME } elseif ($NonInteractive) { Write-Host "ERROR: No SSH username available (PSAUTO_USERNAME / PSAUTO_DEFAULT_SSH_USERNAME are both unset) and there is no console to prompt on." -ForegroundColor Red; exit 1 } else { Read-Host "Set MDE username" }
$password = if ($env:PSAUTO_PASSWORD) { $env:PSAUTO_PASSWORD } elseif ($env:PSAUTO_DEFAULT_SSH_PASSWORD) { $env:PSAUTO_DEFAULT_SSH_PASSWORD } elseif ($NonInteractive) { Write-Host "ERROR: No SSH password available (PSAUTO_PASSWORD / PSAUTO_DEFAULT_SSH_PASSWORD are both unset) and there is no console to prompt on." -ForegroundColor Red; exit 1 } else { Read-Host "Set MDE password" }

Write-Host "Starting validation.." -ForegroundColor Green
Write-Host "User: $user"

#Grab IP addresses from the addreses.txt file
foreach($line in Get-Content "$($PSScriptRoot)\addresses.txt") {
    
    plink -v $user@$line -batch -pw $password -m "$($PSScriptRoot)\commands.txt" > "$($PSScriptRoot)\Validations\$($line).log"
    Write-Host "Validation has been finished" -ForegroundColor Green
}
