@echo off
:: Double-click launcher for the LinkFlex Inventory Automation GUI.
chcp 65001 > nul
title LinkFlex Inventory Automation
cd /d "%~dp0"

python "LinkFlex_Inventory_Automation_GUI.py"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The application closed with an error. See above.
    pause
)
