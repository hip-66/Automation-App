@echo off
:: Desktop-shortcut target: updates (or first-time installs) the local copy
:: of PS Automation and launches it. See bootstrap.ps1 next to this file for
:: what it actually does. Keep both files together outside the AutomationApp
:: folder itself (e.g. directly under C:\Scripts\) - see the comment at the
:: top of bootstrap.ps1 for why.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
if %errorlevel% neq 0 (
    echo.
    echo PS Automation exited with an error - see above.
    pause
)
