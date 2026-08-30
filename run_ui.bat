@echo off
:: Set active code page to UTF-8 to prevent Hebrew output issues
chcp 65001 > nul

title PS Automation - Local Backend Server
echo Starting PS Automation UI...

:: Fast path: once the required libraries have been verified once, a marker
:: file (.deps_ok) lets every later launch skip the check - which otherwise
:: spawns a whole extra Python interpreter and imports cryptography (~1s) on
:: every startup, for nothing.
if exist "%~dp0.deps_ok" goto start_server

:: Check if Flask + the security libraries (python-dotenv, cryptography -
:: used for the login gate and encrypted credentials) are installed
python -c "import flask, dotenv, cryptography" >nul 2>&1
if %errorlevel% equ 0 goto mark_ok

echo Some required libraries are missing. Installing them...
python -m pip install flask python-dotenv cryptography
if %errorlevel% equ 0 goto mark_ok

echo [ERROR] Library installation failed. Please verify internet connection,
echo         or run "Install Adons\Install Adons.bat" manually.
:: timeout instead of pause: still readable if you ran this window directly,
:: but won't hang forever if launched hidden via "Start PS Automation.vbs"
timeout /t 15 >nul 2>&1
exit /b 1

:mark_ok
:: Remember that dependencies are satisfied so the next launch is instant.
echo ok> "%~dp0.deps_ok"

:start_server
echo Launching server.py...
python server.py
:: No trailing "pause" here on purpose: the server shuts itself down when the
:: browser window is closed (see the auto-close feature), and this window is
:: usually hidden (launched via "Start PS Automation.vbs") - a pause would
:: leave it sitting invisibly forever instead of actually closing.
exit /b %errorlevel%
