@echo off
REM Opens the ESXi Host Configuration window with SAMPLE data (no server / no
REM connection needed) so you can preview the layout and tabs. Double-click me.
set PSAUTO_ESXI_SELFTEST=1
python "%~dp0Scripts\Cognyte\Python\esxi_host_config.py"
