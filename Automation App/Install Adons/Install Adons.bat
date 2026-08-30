@echo off
title PS Automation - Install Add-ons
setlocal EnableExtensions

echo ============================================================
echo   PS Automation  -  One-time setup for a new machine
echo ============================================================
echo.
echo Run this ONCE on each new computer. It installs every Python
echo library the app and its scripts need, checks the external
echo tools (Chrome / racadm / plink), and prepares the local
echo secrets file so login works. After this, just use the app
echo normally (PS-Automation shortcut).
echo.

REM ---------- 1) Is Python installed? ----------
python --version >nul 2>&1
if errorlevel 1 goto NO_PYTHON
echo [OK] Python detected:
python --version
echo.

REM ---------- 2) Make sure pip is up to date ----------
echo --- Updating pip ...
python -m pip install --upgrade pip
echo.

REM ---------- 3) Install the required Python libraries ----------
echo --- Installing required Python libraries ...
echo     (flask, selenium, python-docx, Pillow, pyvmomi,
echo      python-dotenv, cryptography)
echo.
REM Prefer requirements.txt (single source of truth); fall back to an explicit
REM list if it isn't present next to the app.
if exist "%~dp0..\requirements.txt" (
    python -m pip install -r "%~dp0..\requirements.txt"
) else (
    python -m pip install flask selenium python-docx Pillow pyvmomi python-dotenv cryptography
)
set "PIP_RC=%errorlevel%"
echo.
if not "%PIP_RC%"=="0" goto PIP_FAILED
echo [OK] All Python libraries are installed.

REM Remember that dependencies are satisfied so app startup skips its own
REM re-check and launches faster.
echo ok> "%~dp0..\.deps_ok"
echo.

REM ---------- 4) Check external tools (cannot be installed with pip) ----------
echo ============================================================
echo   External tools check
echo ============================================================

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=1"
if defined CHROME (echo [OK] Google Chrome is installed.) else (echo [!!] Google Chrome NOT found  -  needed for iDRAC/ESXi screenshot reports.  Get it: https://www.google.com/chrome/)

where racadm >nul 2>&1
if errorlevel 1 (echo [!!] racadm NOT found  -  needed for RAID / power-down / set-hostname scripts.  Install Dell iDRAC Tools / OpenManage.) else (echo [OK] racadm is installed.)

where plink >nul 2>&1
if errorlevel 1 (echo [!!] plink NOT found  -  needed for SSH validation / DNS-NTP scripts.  Get PuTTY: https://www.putty.org/) else (echo [OK] plink is installed.)

echo.
echo [i] ChromeDriver is bundled in the app's "Chromedrivers" folder - no install needed.
echo.

REM ---------- 5) Make sure a .env (secrets) file exists so login works ----------
echo ============================================================
echo   Secrets (.env) check
echo ============================================================
if exist "%~dp0..\.env" (
    echo [OK] .env already exists - login and credentials are configured.
) else (
    echo [!!] .env not found - generating one now with the default Admin
    echo      login and default iDRAC/SSH credentials. The password is stored
    echo      only as a one-way hash, never in plain text.
    python "%~dp0..\generate_env.py"
)
echo.

echo ============================================================
echo   DONE. Setup complete on this machine.
echo   You can now run the app (PS-Automation shortcut).
echo   Log in with user: Admin  (use the password you were given).
echo   Items marked [!!] are optional - only the scripts that use
echo   those tools require them.
echo ============================================================
echo.
pause
exit /b 0

:NO_PYTHON
echo [X] Python is NOT installed, or not in PATH.
echo.
echo     Option A: run "python-3.13.14-amd64.exe" in this folder,
echo               and CHECK "Add python.exe to PATH" during setup.
echo     Option B: download Python 3.x from https://www.python.org/downloads/
echo               (also check "Add python.exe to PATH").
echo     Then run this file again.
echo.
pause
exit /b 1

:PIP_FAILED
echo [X] Some libraries failed to install.
echo     - Check your internet connection.
echo     - On a closed/proxy network, install them manually with:
echo       python -m pip install -r "%~dp0..\requirements.txt"
echo.
pause
exit /b 1
