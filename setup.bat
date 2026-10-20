@echo off
rem ==========================================================================
rem  EVE Voice setup - installs everything needed, then you just run run.bat
rem  1) find (or install) Python 3.9+   2) make venv   3) install libraries
rem  4) download the offline speech model
rem  Written with "goto" instead of ( ) blocks on purpose: folder names like
rem  "eve-voice (1)" contain brackets that break cmd.exe if-blocks.
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title EVE Voice setup

set "PYVER_CHECK=import sys; assert sys.hexversion >= 0x03090000"
set "PY_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PY_INSTALLER=%TEMP%\eve-voice-python-3.12.10-amd64.exe"
set "MODEL=vosk-model-small-en-us-0.15"
set "MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"

echo.
echo  ===============================
echo   EVE Voice setup
echo  ===============================
echo  First-time setup needs an internet connection
echo  (Python ~25 MB if you don't have it, speech model ~40 MB).
echo.

rem ---------------------------------------------------------------- 1. Python
echo [1/4] Looking for Python 3.9 or newer...
call :find_python
if defined PY goto have_python

echo       Python was not found. Installing Python 3.12 for your user account
echo       (no admin needed). This can take a minute or two...
call :install_python
call :add_user_python_to_path
call :find_python
if defined PY goto have_python
goto fail_python

:have_python
echo       Found Python: %PY%

rem ---------------------------------------------------------------- 2. venv
echo.
echo [2/4] Creating the private Python environment (venv)...
if exist "venv\Scripts\python.exe" goto venv_ok
if exist "venv" rmdir /s /q "venv"
%PY% -m venv venv
if errorlevel 1 goto fail_venv
if not exist "venv\Scripts\python.exe" goto fail_venv
echo       Created venv.
goto venv_done
:venv_ok
echo       venv already exists - reusing it.
:venv_done
set "VPY=venv\Scripts\python.exe"

rem ---------------------------------------------------------------- 3. libraries
echo.
echo [3/4] Installing libraries (vosk, sounddevice, pydirectinput, ...)...
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto fail_pip
"%VPY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 echo       Note: tkinter is missing, so the EVE Voice window can't open - use run_cli.bat, or reinstall Python with "tcl/tk" ticked.
echo       Libraries installed.

rem ---------------------------------------------------------------- 4. model
echo.
echo [4/4] Speech model...
if exist "model\am" goto model_ok
if exist "model" rmdir /s /q "model"
echo       Downloading %MODEL% (~40 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%MODEL_URL%' -OutFile '%MODEL%.zip'"
if errorlevel 1 goto fail_model
if not exist "%MODEL%.zip" goto fail_model
echo       Unzipping...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force -LiteralPath '%MODEL%.zip' -DestinationPath '.'"
if errorlevel 1 goto fail_model
if not exist "%MODEL%\am" goto fail_model
ren "%MODEL%" model
if errorlevel 1 goto fail_model
del /q "%MODEL%.zip" >nul 2>&1
echo       Model ready.
goto model_done
:model_ok
echo       Model already downloaded.
:model_done

rem ---------------------------------------------------------------- your settings
if not exist "commands.json" copy /y "commands.default.json" "commands.json" >nul

rem ---------------------------------------------------------------- desktop shortcut
rem (re)created every time so an old shortcut to eve_voice_gui.py now goes through launcher.py
rem Paths go in via environment variables so brackets/spaces in folder names are safe.
set "EV_DIR=%CD%"
set "EV_LNK=EVE Voice.lnk"
if not exist "venv\Scripts\pythonw.exe" goto shortcut_done
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $l=Join-Path $d $env:EV_LNK; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); $s.TargetPath=(Join-Path $env:EV_DIR 'venv\Scripts\pythonw.exe'); $q=[string][char]34; $s.Arguments=$q + (Join-Path $env:EV_DIR 'launcher.py') + $q; $s.WorkingDirectory=$env:EV_DIR; $s.Description='EVE Voice'; $s.Save()" >nul 2>&1
if errorlevel 1 echo       Couldn't make a desktop shortcut - just use run.bat instead.
if not errorlevel 1 echo       Desktop shortcut "EVE Voice" is ready.
:shortcut_done

echo.
echo  ===============================
echo   Setup finished!
echo  ===============================
echo  Next: double-click run.bat (or the "EVE Voice" desktop shortcut).
echo.
pause
exit /b 0


rem ====================================================================== helpers
:find_python
rem Sets PY to a command that runs a REAL Python 3.9+. We actually run it, so the
rem Microsoft Store "python" stub (which only prints a message / opens the Store)
rem fails the check and is ignored.
set "PY="
py -3 -c "%PYVER_CHECK%" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :eof
python -c "%PYVER_CHECK%" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :eof
call :try_exe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :eof
call :try_exe "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto :eof
call :try_exe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto :eof
call :try_exe "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined PY goto :eof
call :try_exe "%ProgramFiles%\Python312\python.exe"
if defined PY goto :eof
call :try_exe "%ProgramFiles%\Python313\python.exe"
goto :eof

:try_exe
if not exist "%~1" goto :eof
"%~1" -c "%PYVER_CHECK%" >nul 2>&1
if errorlevel 1 goto :eof
set PY="%~1"
goto :eof

:install_python
where winget >nul 2>&1
if errorlevel 1 goto install_direct
echo       Using winget (Windows Package Manager)...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :add_user_python_to_path
call :find_python
if defined PY goto :eof
echo       winget didn't work - trying the direct download from python.org instead...
:install_direct
echo       Downloading the official Python 3.12 installer from python.org...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'"
if errorlevel 1 goto :eof
if not exist "%PY_INSTALLER%" goto :eof
echo       Running the installer silently (just for you, adds Python to PATH, includes tkinter)...
start "" /wait "%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_pip=1
del /q "%PY_INSTALLER%" >nul 2>&1
goto :eof

:add_user_python_to_path
rem A new install only updates PATH for NEW windows. Add the usual per-user
rem install folders to THIS window so setup can carry on without reopening.
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Launcher;%PATH%"
goto :eof


rem ====================================================================== errors
:fail_python
echo.
echo  !! Couldn't find or install Python automatically.
echo     Please install it by hand:
echo       1. Go to https://www.python.org/downloads/
echo       2. Download Python 3.12 and run the installer
echo       3. On the first screen TICK "Add python.exe to PATH", then click Install Now
echo       4. Close this window and double-click setup.bat again
goto fail_end

:fail_venv
echo.
echo  !! Couldn't create the venv folder. Make sure this folder isn't read-only
echo     (e.g. don't run it from inside the zip - unzip it first), then try again.
goto fail_end

:fail_pip
echo.
echo  !! Installing the libraries failed. Check your internet connection and run
echo     setup.bat again. If it keeps failing, copy the red error text above.
goto fail_end

:fail_model
echo.
echo  !! Downloading or unzipping the speech model failed. Check your internet
echo     connection and run setup.bat again. Or download it yourself:
echo       %MODEL_URL%
echo     unzip it here and rename the folder to "model".
goto fail_end

:fail_end
echo.
pause
exit /b 1
