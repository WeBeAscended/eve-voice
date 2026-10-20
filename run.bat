@echo off
rem Opens the EVE Voice app (window, no console) via launcher.py, which applies a
rem signed update first if one is available. Set up first with setup.bat.
rem Command-line version: run_cli.bat
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" goto no_setup
if not exist "venv\Scripts\pythonw.exe" goto console
start "" "venv\Scripts\pythonw.exe" launcher.py %*
exit /b 0

:console
"venv\Scripts\python.exe" launcher.py %*
if errorlevel 1 pause
exit /b 0

:no_setup
echo EVE Voice isn't set up yet. Double-click setup.bat first.
echo.
pause
exit /b 1
