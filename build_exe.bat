@echo off
setlocal
cd /d "%~dp0"
title Build Isle Alert

if not exist isle_alert_windows.py goto missing
if not exist isle_core.py goto missing
if not exist Hungry.wav goto missing
if not exist Thirsty.wav goto missing
if not exist Oxygen.wav goto missing

set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto nopython

echo.
echo [1/3] Installing the packages (the first time this takes a minute)...
%PY% -m pip install windows-capture numpy winotify pillow pyinstaller
if errorlevel 1 goto fail

echo.
echo [2/3] Building the app...
%PY% -m PyInstaller --noconfirm --clean --windowed --name IsleAlert --collect-all windows_capture --hidden-import winsound isle_alert_windows.py
if errorlevel 1 goto fail

echo.
echo [3/3] Finishing up...
copy /y Hungry.wav dist\IsleAlert\ >nul
copy /y Thirsty.wav dist\IsleAlert\ >nul
copy /y Oxygen.wav dist\IsleAlert\ >nul
if exist IsleAlert rmdir /s /q IsleAlert
move dist\IsleAlert IsleAlert >nul
rmdir /s /q build dist >nul 2>&1
del IsleAlert.spec >nul 2>&1

echo.
echo ============================================================
echo  Done! Open the IsleAlert folder and double-click
echo  IsleAlert.exe  (keep everything in that folder together).
echo  Tip: right-click IsleAlert.exe - Send to - Desktop (shortcut)
echo ============================================================
explorer IsleAlert
pause
exit /b 0

:missing
echo.
echo build_exe.bat must sit in the same folder as isle_alert_windows.py, isle_core.py,
echo Hungry.wav, Thirsty.wav and Oxygen.wav. If you opened it from inside the zip,
echo right-click the zip - Extract All - first.
pause
exit /b 1

:nopython
echo.
echo Python was not found. Install it from https://www.python.org/downloads/
echo and TICK "Add python.exe to PATH" on the first screen of the installer.
echo Then double-click build_exe.bat again.
pause
exit /b 1

:fail
echo.
echo Something went wrong - read the message above and send it to Claude.
pause
exit /b 1
