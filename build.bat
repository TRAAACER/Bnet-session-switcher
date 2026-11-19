@echo off
setlocal EnableExtensions DisableDelayedExpansion
title bnet-session-switcher Build Helper

pushd "%~dp0" || (
    echo ERROR: Failed to switch to script directory.
    pause
    exit /b 1
)

set "LOG=%CD%\build.log"
echo Build started %DATE% %TIME% > "%LOG%"

if not exist "modern_gui.py" (
    echo ERROR: modern_gui.py not found. Extract the archive completely before running build.bat.
    echo ERROR: modern_gui.py not found. >> "%LOG%"
    popd
    pause
    exit /b 1
)

if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found.
    echo ERROR: requirements.txt not found. >> "%LOG%"
    popd
    pause
    exit /b 1
)

set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
)

if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    echo ERROR: Python 3.10+ x64 was not found in PATH.
    echo Install Python 3.10+ x64 and enable Add python.exe to PATH.
    echo ERROR: Python not found. >> "%LOG%"
    popd
    pause
    exit /b 1
)

echo [1/6] Verifying Python 3.10+ x64 interpreter...
%PYEXE% -c "import struct, sys; print(sys.version); assert sys.version_info >= (3, 10) and struct.calcsize('P') == 8, '64-bit Python 3.10+ required'" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

if exist ".venv-build\Scripts\python.exe" (
    ".venv-build\Scripts\python.exe" -c "import struct, sys; assert sys.version_info >= (3, 10) and struct.calcsize('P') == 8" >nul 2>nul
    if errorlevel 1 (
        echo [2/6] Recreating incompatible .venv-build environment...
        rmdir /s /q ".venv-build" 2>nul
    )
)

if not exist ".venv-build\Scripts\python.exe" (
    echo [2/6] Creating isolated build environment [.venv-build]...
    %PYEXE% -m venv ".venv-build" >> "%LOG%" 2>&1
    if errorlevel 1 goto fail
) else (
    echo [2/6] Reusing verified build environment [.venv-build]...
)

set "VPY=%CD%\.venv-build\Scripts\python.exe"

echo [3/6] Installing locked build dependencies...
if exist "packaging\requirements-build.lock.txt" (
    "%VPY%" -m pip install -r "packaging\requirements-build.lock.txt" >> "%LOG%" 2>&1
) else (
    "%VPY%" -m pip install -r requirements.txt pyinstaller >> "%LOG%" 2>&1
)
if errorlevel 1 goto fail

echo [4/6] Cleaning temporary build files [preserving dist\data]...
if exist "build" rmdir /s /q "build" 2>nul
del /q "*.spec" 2>nul
if exist "dist\bnet-session-switcher.exe" del /f /q "dist\bnet-session-switcher.exe" 2>nul
if exist "dist\bnet-session-switcher.exe" (
    echo ERROR: dist\bnet-session-switcher.exe is currently running. Please close it first.
    echo ERROR: dist\bnet-session-switcher.exe locked by running process. >> "%LOG%"
    goto fail
)

echo [5/6] Building dist\bnet-session-switcher.exe with PyInstaller...
set "ICON_ARGS="
if exist "icon.ico" (
    set "ICON_ARGS=--icon icon.ico --add-data icon.ico;."
) else if exist "006.ico" (
    set "ICON_ARGS=--icon 006.ico --add-data 006.ico;."
)

"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "bnet-session-switcher" %ICON_ARGS% modern_gui.py >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo [6/6] Staging and auditing clean release bundle...
"%VPY%" "packaging\stage_release.py" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo.
echo Build succeeded!
echo   - Local test EXE : dist\bnet-session-switcher.exe
echo   - Release bundle : release\
popd
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Last 40 lines of build.log:
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path -LiteralPath $env:LOG) { Get-Content -LiteralPath $env:LOG -Tail 40 }"
echo.
popd
pause
exit /b 1
