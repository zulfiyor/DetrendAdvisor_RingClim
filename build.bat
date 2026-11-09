@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

REM ============================================================
REM build.bat  (Windows)
REM - Creates/uses local venv: .venv_build
REM - Builds the NEWEST detrend_advisor*.py (detrend_advisor2.py, 3 ...) and ringclim.py
REM - Converts icons\detrend_advisor.* and icons\ringclim.* (png/jpg/webp/bmp/ico)
REM   to .ico and embeds them in the EXE, title bar and taskbar
REM - Builds two onedir app folders with EXEs via PyInstaller
REM - Optionally creates desktop shortcuts with the app icons
REM - Packs both programs into dist\windows-DeClim-<version>-<build>\DeClim and prepares the
REM   GitHub release in release\DeClim-<version>-<build>\ :
REM     DeClim-Windows-x64.zip + DeClim-update-windows-x64.json (for the built-in updater),
REM     the user guides (docs\), SHA256SUMS.txt and RELEASE_NOTES.md (release description)
REM Version: VERSION.txt (e.g. 2.0.1); build number = UTC time. Write the changes in CHANGELOG.md.
REM Usage:
REM   build.bat
REM   build.bat CLEAN        (recreate the build environment .venv_build)
REM   set DECLIM_VERSION=2.0.1 ^& build.bat   (override VERSION.txt)
REM ============================================================

REM ---- Hard isolation from user site-packages ----
set "PYTHONNOUSERSITE=1"
set "PIP_USER=0"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONUTF8=1"
set "ORIG_CONDA_PREFIX=%CONDA_PREFIX%"
REM ---- Avoid DLL / plugin contamination from conda or external Qt installs ----
set "CONDA_PREFIX="
set "CONDA_DEFAULT_ENV="
set "QT_PLUGIN_PATH="
set "QT_QPA_PLATFORM_PLUGIN_PATH="
set "QML2_IMPORT_PATH="
set "PYTHONPATH="
set "PYTHONHOME="
set "PYTHONSTARTUP="
set "PYTHONEXECUTABLE="

REM Always run from this .bat directory
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
pushd "%ROOT%"

REM ---- Product names ----
set "PRODUCT=DeClim"
set "NAME_A=detrend_advisor"
set "NAME_B=ringclim"
set "SCRIPT_B=ringclim.py"

REM ---- Pick the NEWEST detrend_advisor*.py (by modification time) ----
set "SCRIPT_A="
for /f "delims=" %%F in ('dir /b /a-d /o-d "detrend_advisor*.py" 2^>nul') do (
  if not defined SCRIPT_A set "SCRIPT_A=%%F"
)

set "VENV_DIR=.venv_build"
set "VENV_PY=%ROOT%\%VENV_DIR%\Scripts\python.exe"
set "ICON_DIR=%ROOT%\build\icons"
REM Shared modules (dendro_*.py and the country map) are in this folder
set "MOD_DIR=%ROOT%\dendro_modules"

REM ---- Version and build number ----
set "APP_VERSION="
if defined DECLIM_VERSION set "APP_VERSION=%DECLIM_VERSION%"
if not defined APP_VERSION if exist "VERSION.txt" set /p APP_VERSION=<"VERSION.txt"
if not defined APP_VERSION set "APP_VERSION=2.0.0"
for /f "tokens=* delims= " %%V in ("%APP_VERSION%") do set "APP_VERSION=%%V"
set "APP_BUILD=%DECLIM_BUILD%"
if not defined APP_BUILD for /f %%B in ('powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')"') do set "APP_BUILD=%%B"
set "STAMP_DIR=%ROOT%\build\stamp"
set "DIST_PARENT=%ROOT%\dist\windows-%PRODUCT%-%APP_VERSION%-%APP_BUILD%"
set "DIST_DIR=%DIST_PARENT%\%PRODUCT%"
set "RELEASE_DIR=%ROOT%\release\%PRODUCT%-%APP_VERSION%-%APP_BUILD%"
echo Version: %APP_VERSION%  build %APP_BUILD%

REM ---- Folders ----
if not exist "build" mkdir "build"
if not exist "build\logs" mkdir "build\logs"
if not exist "build\specs" mkdir "build\specs"
if not exist "icons" mkdir "icons"

REM ---- Optional CLEAN ----
if /I "%~1"=="CLEAN" (
  echo Cleaning venv and build artifacts...
  if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%" 2>nul
  if exist "build\icons" rmdir /s /q "build\icons" 2>nul
)

REM Replace only this build's versioned output; keep earlier releases and other platforms.
if exist "%DIST_PARENT%" rmdir /s /q "%DIST_PARENT%" 2>nul
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"
if exist "build\work_%NAME_A%" rmdir /s /q "build\work_%NAME_A%" 2>nul
if exist "build\work_%NAME_B%" rmdir /s /q "build\work_%NAME_B%" 2>nul
del /q "*.spec" 2>nul
del /q "build\specs\*.spec" 2>nul

echo === Build started ===
echo Working dir: "%CD%"

REM ---- Validate scripts ----
if not exist "%MOD_DIR%\dendro_online.py" (
  echo ERROR: folder "%MOD_DIR%" with the dendro_*.py modules not found.
  goto :fail
)
if not defined SCRIPT_A (
  echo ERROR: no detrend_advisor*.py found in "%CD%".
  goto :fail
)
echo Detrend Advisor source (newest): %SCRIPT_A%
set "HAS_B=0"
if exist "%SCRIPT_B%" set "HAS_B=1"
if "%HAS_B%"=="1" (echo RingClim source: %SCRIPT_B%) else (echo RingClim source: %SCRIPT_B% not found - will be skipped)

REM ---- Auto-create requirements if missing ----
if not exist "requirements.txt" (
  echo Creating requirements.txt ...
  > requirements.txt echo numpy^>=1.23
  >> requirements.txt echo pandas^>=1.5
  >> requirements.txt echo scipy^>=1.9
  >> requirements.txt echo matplotlib^>=3.6
  >> requirements.txt echo statsmodels^>=0.13
  >> requirements.txt echo XlsxWriter^>=3.0
  >> requirements.txt echo openpyxl^>=3.1
  >> requirements.txt echo xlrd^>=2.0.1
  >> requirements.txt echo PySide6==6.8.3
  >> requirements.txt echo shiboken6==6.8.3
  >> requirements.txt echo xarray^>=2023.1
  >> requirements.txt echo netCDF4^>=1.6
)

if not exist "requirements-build.txt" (
  echo Creating requirements-build.txt ...
  > requirements-build.txt echo pyinstaller^>=6.0
  >> requirements-build.txt echo pyinstaller-hooks-contrib
  >> requirements-build.txt echo pillow^>=10.0
)

REM ---- Choose Python: prefer py 3.11, then py 3, then conda python, then python ----
set "SYS_PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.11 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "SYS_PY=py -3.11"
  if not defined SYS_PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "SYS_PY=py -3"
  )
)
if not defined SYS_PY (
  if defined ORIG_CONDA_PREFIX (
    if exist "%ORIG_CONDA_PREFIX%\python.exe" set "SYS_PY="%ORIG_CONDA_PREFIX%\python.exe""
  )
)
if not defined SYS_PY (
  where python >nul 2>nul
  if errorlevel 1 (
    echo ERROR: python not found on PATH and py launcher not available.
    echo Fix: install Python 3.11 from python.org or disable the Microsoft Store python alias.
    goto :fail
  )
  set "SYS_PY=python"
)

echo Python launcher: %SYS_PY%
%SYS_PY% --version

REM ---- Create venv if missing ----
if not exist "%VENV_PY%" (
  echo Creating venv "%VENV_DIR%" ...
  %SYS_PY% -m venv "%VENV_DIR%" > "build\logs\venv_create.log" 2>&1
  if errorlevel 1 (
    echo ERROR: Failed to create venv.
    call :tail "build\logs\venv_create.log" 200
    goto :fail
  )
)

REM ---- Force include-system-site-packages = false ----
if exist "%VENV_DIR%\pyvenv.cfg" powershell -NoProfile -Command "$p='%ROOT%\%VENV_DIR%\pyvenv.cfg'; (Get-Content $p) -replace 'include-system-site-packages = true','include-system-site-packages = false' | Set-Content $p" >nul 2>&1

echo Using venv Python: "%VENV_PY%"

REM ---- Upgrade pip ----
echo Upgrading pip/setuptools/wheel in venv...
"%VENV_PY%" -m ensurepip --upgrade > "build\logs\pip_ensure.log" 2>&1
"%VENV_PY%" -m pip install --no-user -U pip setuptools wheel > "build\logs\pip_upgrade.log" 2>&1
if errorlevel 1 (
  echo ERROR: Failed to upgrade pip in venv.
  call :tail "build\logs\pip_upgrade.log" 220
  goto :fail
)

REM ---- Install requirements ----
echo Installing runtime requirements...
"%VENV_PY%" -m pip install --no-user -r requirements.txt > "build\logs\pip_requirements.log" 2>&1
if errorlevel 1 (
  echo ERROR: Failed to install requirements.txt
  call :tail "build\logs\pip_requirements.log" 260
  goto :fail
)

echo Installing build requirements...
"%VENV_PY%" -m pip install --no-user -r requirements-build.txt > "build\logs\pip_requirements_build.log" 2>&1
if errorlevel 1 (
  echo ERROR: Failed to install requirements-build.txt
  call :tail "build\logs\pip_requirements_build.log" 260
  goto :fail
)
REM Old requirements-build.txt files may not list these
"%VENV_PY%" -m pip install --no-user -U pyinstaller pyinstaller-hooks-contrib pillow > "build\logs\pip_build_tools.log" 2>&1
if errorlevel 1 (
  echo ERROR: Failed to install PyInstaller / Pillow.
  call :tail "build\logs\pip_build_tools.log" 220
  goto :fail
)

REM ---- Guard: pathlib backport ----
"%VENV_PY%" -c "import pathlib; print(pathlib.__file__)" > "build\logs\pathlib_where.log" 2>&1
findstr /I "site-packages" "build\logs\pathlib_where.log" >nul 2>&1
if not errorlevel 1 (
  echo ERROR: Detected pathlib from site-packages. It breaks PyInstaller.
  echo Fix: pip uninstall pathlib
  goto :fail
)

REM ---- Preflight tkinter ----
"%VENV_PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
  echo ERROR: tkinter missing. Reinstall Python from python.org with "tcl/tk and IDLE" enabled.
  goto :fail
)

REM ---- Preflight PySide6 only if script B exists ----
if "%HAS_B%"=="1" (
  "%VENV_PY%" -c "import PySide6, shiboken6; from PySide6.QtWidgets import QApplication" > "build\logs\pyside6_preflight.log" 2>&1
  if errorlevel 1 (
    echo ERROR: PySide6 / shiboken6 / QtWidgets import failed. Check build\logs\pyside6_preflight.log
    goto :fail
  )
)

REM ---- Version stamp (build\stamp\app_version.py is found before the source copy) ----
if not exist "%STAMP_DIR%" mkdir "%STAMP_DIR%"
"%VENV_PY%" "%ROOT%\release_kit\make_release.py" stamp --version "%APP_VERSION%" --build "%APP_BUILD%" --out "%STAMP_DIR%\app_version.py"
if errorlevel 1 (
  echo ERROR: version stamp failed - check VERSION.txt ^(e.g. 2.0.1^)
  goto :fail
)

REM ---- Icons: icons\<name>.png/jpg/jpeg/webp/bmp/ico  ->  build\icons\<name>.ico/.png/.icns ----
echo.
echo Preparing application icons...
if exist "make_icons.py" (
  "%VENV_PY%" "make_icons.py" --root "%ROOT%" --out "%ICON_DIR%" > "build\logs\icons.log" 2>&1
  type "build\logs\icons.log"
) else (
  echo WARNING: make_icons.py not found - default icons will be used.
)

set "ICON_ARGS_A="
if exist "%ICON_DIR%\%NAME_A%.ico" set ICON_ARGS_A=--icon "%ICON_DIR%\%NAME_A%.ico" --add-data "%ICON_DIR%\%NAME_A%.ico;." --add-data "%ICON_DIR%\%NAME_A%.png;."
set "ICON_ARGS_B="
if exist "%ICON_DIR%\%NAME_B%.ico" set ICON_ARGS_B=--icon "%ICON_DIR%\%NAME_B%.ico" --add-data "%ICON_DIR%\%NAME_B%.ico;." --add-data "%ICON_DIR%\%NAME_B%.png;."

REM ---- PyInstaller flags ----
set "COMMON=--onedir --noconsole --noconfirm --clean --specpath build\specs"
set "UPDATER_A=--hidden-import app_update --hidden-import app_update_tk --hidden-import app_product --hidden-import app_version --collect-all certifi"
set "UPDATER_B=--hidden-import app_update --hidden-import app_update_qt --hidden-import app_product --hidden-import app_version --collect-all certifi"
set "COLLECT_MPL=--collect-all matplotlib"
set "COLLECT_NUMPY=--collect-all numpy"
set "COLLECT_PANDAS=--collect-all pandas"
set "COLLECT_SCIPY=--collect-all scipy"
set "COLLECT_STATSM=--collect-all statsmodels"
set "COLLECT_PYSIDE=--collect-all PySide6"
set "COLLECT_OPENPYXL=--collect-all openpyxl"
set "COLLECT_XLSXWRITER=--collect-all xlsxwriter"
set "COLLECT_XARRAY=--collect-all xarray"
set "COLLECT_NETCDF4=--collect-all netCDF4"
set "COLLECT_CFTIME=--collect-all cftime"
set "COLLECT_XLRD=--collect-all xlrd"
set "PYTHONPATH=%STAMP_DIR%;%MOD_DIR%;%ROOT%"

REM ---- Build A: Tkinter ----
echo.
echo [1/2] Building %NAME_A%.exe from %SCRIPT_A% ...
"%VENV_PY%" -m PyInstaller ^
  %COMMON% --distpath "%DIST_DIR%" ^
  --paths "%STAMP_DIR%" --paths "%MOD_DIR%" --paths "%ROOT%" ^
  --name "%NAME_A%" ^
  %UPDATER_A% ^
  --workpath "build\work_%NAME_A%" ^
  %ICON_ARGS_A% ^
  %COLLECT_MPL% ^
  --hidden-import matplotlib.backends.backend_tkagg ^
  --hidden-import statsmodels.nonparametric.smoothers_lowess ^
  --exclude-module PySide6 ^
  --exclude-module shiboken6 ^
  %COLLECT_NUMPY% ^
  %COLLECT_PANDAS% ^
  %COLLECT_SCIPY% ^
  %COLLECT_STATSM% ^
  %COLLECT_OPENPYXL% ^
  %COLLECT_XLSXWRITER% ^
  --hidden-import dendro_online ^
  --hidden-import dendro_online_tk ^
  --hidden-import dendro_pith ^
  --hidden-import dendro_plots ^
  --hidden-import dendro_ui_help ^
  --hidden-import dendro_geoexport ^
  --add-data "%MOD_DIR%\dendro_online_countries.geojson;." ^
  "%ROOT%\%SCRIPT_A%" > "build\logs\%NAME_A%.log" 2>&1

if errorlevel 1 (
  echo ERROR: %NAME_A% build failed.
  call :tail "build\logs\%NAME_A%.log" 260
  goto :fail
)

REM ---- Build B: PySide6 (optional) ----
if "%HAS_B%"=="1" (
  echo.
  echo [2/2] Building %NAME_B%.exe from %SCRIPT_B% ...
  "%VENV_PY%" -m PyInstaller ^
    %COMMON% --distpath "%DIST_DIR%" ^
    --paths "%STAMP_DIR%" --paths "%MOD_DIR%" --paths "%ROOT%" ^
    --name "%NAME_B%" ^
    %UPDATER_B% ^
    --workpath "build\work_%NAME_B%" ^
    %ICON_ARGS_B% ^
    %COLLECT_MPL% ^
    %COLLECT_PYSIDE% ^
    --collect-all shiboken6 ^
    --hidden-import PySide6.QtCore ^
    --hidden-import PySide6.QtGui ^
    --hidden-import PySide6.QtWidgets ^
    --hidden-import shiboken6 ^
    --hidden-import matplotlib.backends.backend_qtagg ^
    %COLLECT_NUMPY% ^
    %COLLECT_PANDAS% ^
    %COLLECT_SCIPY% ^
    %COLLECT_OPENPYXL% ^
    %COLLECT_XLSXWRITER% ^
    %COLLECT_XARRAY% ^
    %COLLECT_NETCDF4% ^
    %COLLECT_CFTIME% ^
    %COLLECT_XLRD% ^
    --hidden-import dendro_online ^
    --hidden-import dendro_online_qt ^
    --hidden-import dendro_plots ^
    --hidden-import dendro_ui_help ^
    --hidden-import dendro_geoexport ^
    --hidden-import dendro_sources ^
    --hidden-import dendro_sources_qt ^
    --hidden-import dendro_copernicus ^
    --hidden-import dendro_field ^
    --hidden-import dendro_field_qt ^
    --hidden-import dendro_moving ^
    --hidden-import dendro_moving_qt ^
    --add-data "%MOD_DIR%\dendro_online_countries.geojson;." ^
    "%ROOT%\%SCRIPT_B%" > "build\logs\%NAME_B%.log" 2>&1

  if errorlevel 1 (
    echo ERROR: %NAME_B% build failed.
    call :tail "build\logs\%NAME_B%.log" 320
    goto :fail
  )
) else (
  echo.
  echo [2/2] %SCRIPT_B% not found - skipping.
)

REM ---- Write first-run runtime note ----
(
  echo Detrend Advisor / RingClim - first run notes
  echo.
  echo 1. The EXE files contain all Python libraries. Python does not need to be installed.
  echo 2. Detrend Advisor computes with R and the R package dplR. They are NOT bundled.
  echo 3. On first RUN the program finds Rscript automatically or lets you select it manually.
  echo 4. If R is missing, Windows installation via winget or chocolatey can be started from the program.
  echo 5. If dplR is missing, the program can install it from CRAN. Internet access is required.
  echo 6. R runs hidden in the background; progress is shown in the Runtime log tab.
  echo 7. Keep the folders %NAME_A% and %NAME_B% together inside the %PRODUCT% folder so that
  echo    "Actions ^> Open RingClim" finds ringclim.exe and the built-in updater works.
  echo 8. Put the %PRODUCT% folder where you can write ^(Documents, Desktop, user folder^), not in Program Files.
  echo 9. Updates: button "Updates" ^> "Check for updates...". User guides: folder docs and "Updates" ^> "User guides".
  echo.
  echo Version %APP_VERSION% build %APP_BUILD%
) > "%DIST_DIR%\README_FIRST_RUN.txt"
if exist "LICENSE" copy /y "LICENSE" "%DIST_DIR%\LICENSE.txt" >nul
if exist "CHANGELOG.md" copy /y "CHANGELOG.md" "%DIST_DIR%\CHANGELOG.md" >nul

REM ---- Release package: portable ZIP + update manifest + guides + notes ----
echo.
echo Preparing the release in "%RELEASE_DIR%" ...
"%VENV_PY%" "%ROOT%\release_kit\make_release.py" package --product-dir "%DIST_DIR%" --platform windows-x64 --out "%RELEASE_DIR%" --version-file "%STAMP_DIR%\app_version.py" --guides "%ROOT%\docs" --changelog "%ROOT%\CHANGELOG.md" --product-module "%MOD_DIR%\app_product.py" > "build\logs\release.log" 2>&1
set "REL_RC=%errorlevel%"
type "build\logs\release.log"
if not "%REL_RC%"=="0" (
  echo ERROR: release packaging failed - see build\logs\release.log
  goto :fail
)

REM ---- Refresh the Windows icon cache so new icons are shown ----
ie4uinit.exe -show >nul 2>&1

REM ---- Optional desktop shortcuts ----
echo.
choice /C YN /T 20 /D N /M "Create desktop shortcuts (auto-No in 20 s)"
if errorlevel 2 goto :after_shortcuts
call :make_shortcut "Detrend Advisor" "%DIST_DIR%\%NAME_A%\%NAME_A%.exe"
if "%HAS_B%"=="1" call :make_shortcut "RingClim" "%DIST_DIR%\%NAME_B%\%NAME_B%.exe"
:after_shortcuts

echo.
echo === Build OK ===
echo Built from: %SCRIPT_A% and %SCRIPT_B%
echo Version %APP_VERSION% build %APP_BUILD%
echo Outputs:
echo - %DIST_DIR%\%NAME_A%\%NAME_A%.exe
if "%HAS_B%"=="1" echo - %DIST_DIR%\%NAME_B%\%NAME_B%.exe
echo - %DIST_DIR%\README_FIRST_RUN.txt
echo Release for GitHub: %RELEASE_DIR%
echo   upload DeClim-Windows-x64.zip + DeClim-update-windows-x64.json + guides,
echo   paste RELEASE_NOTES.md as the description, tag v%APP_VERSION%, mark as Latest.
if exist "%ICON_DIR%\%NAME_A%.ico" (echo Icon A: build\icons\%NAME_A%.ico) else (echo Icon A: default - put icons\%NAME_A%.png to use your logo)
if exist "%ICON_DIR%\%NAME_B%.ico" (echo Icon B: build\icons\%NAME_B%.ico) else (echo Icon B: default - put icons\%NAME_B%.png to use your logo)
echo Logs: build\logs
pause

popd
endlocal
exit /b 0

:fail
echo.
echo === Build FAILED ===
pause
popd
endlocal
exit /b 1

:make_shortcut
set "SC_NAME=%~1"
set "SC_TARGET=%~2"
if not exist "%SC_TARGET%" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell; $d = [Environment]::GetFolderPath('Desktop'); $l = $w.CreateShortcut((Join-Path $d ($env:SC_NAME + '.lnk'))); $l.TargetPath = $env:SC_TARGET; $l.WorkingDirectory = (Split-Path $env:SC_TARGET); $l.IconLocation = $env:SC_TARGET + ',0'; $l.Save()" >nul 2>&1
if errorlevel 1 (echo WARNING: could not create shortcut "%SC_NAME%") else (echo Shortcut created: %SC_NAME%)
exit /b 0

:tail
set "FILE=%~1"
set "N=%~2"
where powershell >nul 2>nul
if not errorlevel 1 (
  powershell -NoProfile -Command "Get-Content -Path '%FILE%' -Tail %N%"
) else (
  type "%FILE%"
)
exit /b 0
