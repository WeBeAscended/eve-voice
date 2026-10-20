@echo off
rem Command-line EVE Voice (advanced). Uses the venv's own Python (made by setup.bat).
rem Extra options pass straight through, e.g.  run_cli.bat --dry-run --text
rem                                            run_cli.bat --calibrate
rem                                            run_cli.bat --list-windows
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" goto no_setup
"venv\Scripts\python.exe" eve_voice.py %*
echo.
pause
exit /b 0

:no_setup
echo EVE Voice isn't set up yet. Double-click setup.bat first.
echo.
pause
exit /b 1
