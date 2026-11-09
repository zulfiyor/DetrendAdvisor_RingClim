@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
REM ============================================================
REM publish_release.bat  - GitHub repository and release for DeClim (uses GitHub CLI "gh")
REM   publish_release.bat prepare   repository (created if missing) + source code + DRAFT release
REM                                 v<VERSION.txt> with notes, user guides and example data
REM   publish_release.bat upload    after build.bat: uploads the newest release\DeClim-*\ files
REM                                 (ZIP + update JSON + checksums) to the draft release
REM   publish_release.bat publish   makes the release public and "Latest" (updaters see it now)
REM One-time setup: winget install --id GitHub.cli   then   gh auth login
REM ============================================================
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
pushd "%ROOT%"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=prepare"

where gh >nul 2>nul
if errorlevel 1 (
  echo GitHub CLI "gh" is not installed. Install it once with:
  echo    winget install --id GitHub.cli
  echo then open a NEW terminal and run:  gh auth login
  goto :fail
)
gh auth status >nul 2>nul
if errorlevel 1 (
  echo You are not logged in to GitHub. Starting "gh auth login" ...
  gh auth login
  if errorlevel 1 goto :fail
)

set "PY="
if exist "%ROOT%\.venv_build\Scripts\python.exe" set "PY=%ROOT%\.venv_build\Scripts\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY set "PY=python"

for /f "usebackq delims=" %%R in (`%PY% -c "import sys; sys.path.insert(0, r'%ROOT%\dendro_modules'); import app_product as p; print(p.REPOSITORY)"`) do set "REPO=%%R"
if not defined REPO (echo ERROR: cannot read REPOSITORY from dendro_modules\app_product.py & goto :fail)
set "VER="
if exist "VERSION.txt" set /p VER=<"VERSION.txt"
for /f "tokens=* delims= " %%V in ("%VER%") do set "VER=%%V"
if not defined VER (echo ERROR: VERSION.txt is empty & goto :fail)
set "TAG=v%VER%"
echo Repository: %REPO%   release: %TAG%   mode: %MODE%
if not exist "build" mkdir "build"

if /I "%MODE%"=="prepare" goto :prepare
if /I "%MODE%"=="upload" goto :upload
if /I "%MODE%"=="publish" goto :publish
echo Unknown mode "%MODE%". Use: prepare ^| upload ^| publish
goto :fail

REM ------------------------------------------------------------------ prepare
:prepare
gh repo view %REPO% >nul 2>nul
if errorlevel 1 (
  echo Repository %REPO% does not exist yet.
  choice /C YN /M "Create it as a PUBLIC repository now"
  if errorlevel 2 goto :fail
  gh repo create %REPO% --public --description "DeClim: Detrend Advisor + RingClim - detrending, chronologies and climate-growth analysis for dendroclimatology" --disable-wiki
  if errorlevel 1 goto :fail
)
set "STAGE=%ROOT%\build\github_repo"
if exist "%STAGE%\.git" (
  git -C "%STAGE%" pull --ff-only
) else (
  if exist "%STAGE%" rmdir /s /q "%STAGE%"
  gh repo clone %REPO% "%STAGE%"
  if errorlevel 1 goto :fail
)
echo Copying the source files ...
set "NEWEST_DA="
for /f "delims=" %%F in ('dir /b /a-d /o-d "detrend_advisor*.py" 2^>nul') do if not defined NEWEST_DA set "NEWEST_DA=%%F"
for %%F in (ringclim.py %NEWEST_DA% make_icons.py build.bat build.sh requirements.txt requirements-build.txt VERSION.txt CHANGELOG.md publish_release.bat publish_release.sh) do (
  if exist "%%F" copy /y "%%F" "%STAGE%\" >nul
)
robocopy "dendro_modules" "%STAGE%\dendro_modules" *.py *.geojson /E /XD __pycache__ /NFL /NDL /NJH /NJS >nul
robocopy "release_kit" "%STAGE%\release_kit" *.py /E /XD __pycache__ /NFL /NDL /NJH /NJS >nul
if exist "icons" robocopy "icons" "%STAGE%\icons" /E /NFL /NDL /NJH /NJS >nul
if exist "docs" robocopy "docs" "%STAGE%\docs" /E /NFL /NDL /NJH /NJS >nul
copy /y "github\README.md" "%STAGE%\README.md" >nul
copy /y "github\LICENSE" "%STAGE%\LICENSE" >nul
copy /y "github\gitignore.txt" "%STAGE%\.gitignore" >nul
pushd "%STAGE%"
git config user.name >nul 2>nul || git config user.name "Zulfiyor Bakhtiyorov"
git config user.email >nul 2>nul || git config user.email "zb308@cam.ac.uk"
git branch -M main >nul 2>nul
git add -A
git commit -m "DeClim %VER%" >nul 2>nul
git push -u origin main
if errorlevel 1 (popd & echo ERROR: git push failed & goto :fail)
popd
echo Writing the release notes ...
%PY% "%ROOT%\release_kit\make_release.py" notes --version %VER% --out "%ROOT%\build\RELEASE_NOTES.md" --changelog "%ROOT%\CHANGELOG.md" --product-module "%ROOT%\dendro_modules\app_product.py"
gh release view %TAG% --repo %REPO% >nul 2>nul
if errorlevel 1 (
  gh release create %TAG% --repo %REPO% --draft --target main --title "DeClim %VER%" --notes-file "%ROOT%\build\RELEASE_NOTES.md"
  if errorlevel 1 goto :fail
) else (
  gh release edit %TAG% --repo %REPO% --title "DeClim %VER%" --notes-file "%ROOT%\build\RELEASE_NOTES.md"
)
for %%F in (docs\*.pdf docs\*.docx docs\*.zip) do gh release upload %TAG% "%%F" --repo %REPO% --clobber
echo.
echo DONE: draft release %TAG% with notes and guides: https://github.com/%REPO%/releases
echo Next: build.bat, then  publish_release.bat upload  (and on the Mac: ./publish_release.sh upload),
echo then  publish_release.bat publish
goto :ok

REM ------------------------------------------------------------------ upload
:upload
set "REL="
for /f "delims=" %%D in ('dir /b /ad /o-d "release\DeClim-%VER%-*" 2^>nul') do if not defined REL set "REL=release\%%D"
if not defined REL (echo ERROR: no successful release folder for version %VER% - run build.bat first & goto :fail)
if not exist "%REL%\DeClim-Windows-x64.zip" (echo ERROR: Windows ZIP is missing from %REL% & goto :fail)
if not exist "%REL%\DeClim-update-windows-x64.json" (echo ERROR: Windows update manifest is missing from %REL% & goto :fail)
echo Uploading from %REL% ...
gh release view %TAG% --repo %REPO% >nul 2>nul
if errorlevel 1 (echo ERROR: release %TAG% not found - run "publish_release.bat prepare" first & goto :fail)
for %%F in ("%REL%\*.zip" "%REL%\*-update-*.json" "%REL%\SHA256SUMS-*.txt") do (
  if exist "%%~F" (
    echo  - %%~nxF
    gh release upload %TAG% "%%~F" --repo %REPO% --clobber
    if errorlevel 1 goto :fail
  )
)
gh release edit %TAG% --repo %REPO% --notes-file "%REL%\RELEASE_NOTES.md"
echo DONE. Check the files: https://github.com/%REPO%/releases
goto :ok

REM ------------------------------------------------------------------ publish
:publish
gh release view %TAG% --repo %REPO% --json assets --jq ".assets[].name"
choice /C YN /M "Publish %TAG% now (public, Latest - all programs will offer this update)"
if errorlevel 2 goto :fail
gh release edit %TAG% --repo %REPO% --draft=false --latest
if errorlevel 1 goto :fail
echo PUBLISHED: https://github.com/%REPO%/releases/tag/%TAG%
goto :ok

:ok
popd
endlocal
pause
exit /b 0
:fail
echo.
echo === stopped ===
popd
endlocal
pause
exit /b 1
