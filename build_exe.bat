@echo off
setlocal
title Build USB Eraser EXE

echo ============================================================
echo   Building USB-Eraser.exe  (single file, no install needed)
echo ============================================================
echo.

rem --- find a Python launcher -------------------------------------------
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY=python"
    ) else (
        echo Python was not found.
        echo Install it from https://www.python.org/downloads/windows/
        echo and tick "Add python.exe to PATH" during setup.
        pause
        exit /b 1
    )
)

echo Using: %PY%
echo.

echo [1/4] Updating pip ...
%PY% -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo.
echo [2/4] Installing PyInstaller ...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

echo.
echo [3/4] Generating floppy.ico ...
%PY% make_icon.py
if errorlevel 1 goto :fail

echo.
echo [4/4] Building the executable ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "USB-Eraser" ^
    --uac-admin ^
    --icon "floppy.ico" ^
    usb_eraser.py
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   Done!
echo   Your tool:  dist\USB-Eraser.exe
echo   Double-click it - Windows will ask for admin rights.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   Build failed. Read the messages above.
echo ============================================================
pause
exit /b 1
