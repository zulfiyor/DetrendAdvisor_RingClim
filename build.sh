#!/usr/bin/env bash
# ============================================================
# build.sh  (macOS)
# - Creates/uses local venv: .venv_build_mac
# - Builds the NEWEST detrend_advisor*.py (detrend_advisor2.py, 3 ...) and ringclim.py
# - Converts icons/detrend_advisor.* and icons/ringclim.* (png/jpg/webp/bmp/ico)
#   to .icns and embeds them in the .app bundles (Dock, Finder, Launchpad, window)
# - Builds dist/macos-DeClim-<version>-<build>/DeClim/*.app via PyInstaller
# - Packs both apps into one portable folder and prepares the GitHub release
#   release in release/DeClim-<version>-<build>/ :
#     DeClim-macOS-<arch>.zip + DeClim-update-macos-<arch>.json (built-in updater),
#     the user guides (docs/), SHA256SUMS.txt and RELEASE_NOTES.md (release description)
# Version: VERSION.txt (e.g. 2.0.1); build number = UTC time. Write the changes in CHANGELOG.md.
# Every macOS distribution build uses Developer ID signing, Apple notarization,
# stapling, and Gatekeeper verification. Keychain defaults are checked before
# building; DECLIM_SIGN_IDENTITY / DECLIM_NOTARY_PROFILE may override them.
# Usage:
#   chmod +x build.sh
#   ./build.sh
#   ./build.sh CLEAN
#   DECLIM_VERSION=2.0.1 ./build.sh
# Build on each Mac type you want to support (Intel -> macos-x64, Apple Silicon -> macos-arm64).
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

export PYTHONNOUSERSITE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONUTF8=1
unset PYTHONPATH PYTHONHOME QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH 2>/dev/null || true

PRODUCT="DeClim"
NAME_A="detrend_advisor"
NAME_B="ringclim"
SCRIPT_B="ringclim.py"
VENV_DIR=".venv_build_mac"
VENV_PY="$ROOT/$VENV_DIR/bin/python"
# Shared modules (dendro_*.py and the country map) are in this folder
MOD_DIR="$ROOT/dendro_modules"

fail() {
  echo
  echo "=== Build FAILED ==="
  [ -n "${1:-}" ] && echo "$1"
  exit 1
}

show_tail() {
  [ -f "$1" ] && tail -n "${2:-200}" "$1"
}

# ---- Pick the NEWEST detrend_advisor*.py (by modification time) ----
SCRIPT_A="$(ls -t detrend_advisor*.py 2>/dev/null | head -n 1 || true)"

# ---- Version and build number ----
APP_VERSION="${DECLIM_VERSION:-}"
[ -n "$APP_VERSION" ] || APP_VERSION="$(head -n 1 VERSION.txt 2>/dev/null | tr -d '[:space:]' || true)"
[ -n "$APP_VERSION" ] || APP_VERSION="2.0.0"
APP_BUILD="${DECLIM_BUILD:-$(date -u +%Y%m%d%H%M%S)}"
case "$(uname -m)" in arm64) ARCH="arm64";; *) ARCH="x64";; esac

# When launched from exFAT, make a clean internal APFS staging copy of the
# source tree, build there, and export only the release deliverables. Keeping
# the build source and every .app bundle on APFS avoids exFAT metadata issues.
SOURCE_DEVICE="$(df -P "$ROOT" | awk 'END {print $1}')"
SOURCE_FS="$(diskutil info -plist "$SOURCE_DEVICE" 2>/dev/null | plutil -extract FilesystemType raw - 2>/dev/null || true)"
if { [ "$SOURCE_FS" = "exfat" ] || [ "$SOURCE_FS" = "msdos" ]; } && [ "${DECLIM_BUILD_STAGED:-0}" != "1" ]; then
  STAGE_KEY="$(printf '%s' "$ROOT-$APP_VERSION-$APP_BUILD-$ARCH" | shasum -a 256 | cut -c1-16)"
  STAGE_DIR="$HOME/Library/Application Support/DeClim/staging/$STAGE_KEY"
  [ ! -e "$STAGE_DIR" ] || fail "ERROR: internal staging path already exists: $STAGE_DIR"
  mkdir -p "$STAGE_DIR"
  echo "[INFO] Copying build sources from exFAT to internal APFS: $STAGE_DIR"
  rsync -a \
    --exclude '/.git/' --exclude '/.venv*/' --exclude '/.venv_build/' --exclude '/venv/' \
    --exclude '/dist/' --exclude '/release/' --exclude '/build/' \
    --exclude '/MacOS_DeClim/' --exclude '/Windows_DeClim/' --exclude '/MacOS_DeClim.zip' \
    --exclude '/probe/' --exclude '/sources_probe/' --exclude '/_backup_before_batch/' \
    --exclude '/__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' --exclude '._*' \
    --exclude '/run_*.log' --exclude '/kyrg002_correlations.xlsx' \
    --exclude '/node_modules/' \
    "$ROOT/" "$STAGE_DIR/"
  echo "[INFO] Building and signing from the internal APFS copy."
  if env DECLIM_BUILD_STAGED=1 DECLIM_EXPORT_ROOT="$ROOT" DECLIM_VERSION="$APP_VERSION" DECLIM_BUILD="$APP_BUILD" \
    "$STAGE_DIR/build.sh" "$@"; then
    echo "[INFO] Release export completed; removing temporary internal build copy."
    /usr/bin/python3 -c 'import os, shutil, sys; target=os.path.realpath(sys.argv[1]); base=os.path.realpath(os.path.expanduser("~/Library/Application Support/DeClim/staging")); assert os.path.commonpath([target, base]) == base and target != base; shutil.rmtree(target)' "$STAGE_DIR"
    exit 0
  else
    status=$?
    echo "[ERROR] Build failed; internal staging copy and logs are kept for diagnosis: $STAGE_DIR"
    exit "$status"
  fi
fi

export DECLIM_SIGN_IDENTITY="${DECLIM_SIGN_IDENTITY:-Developer ID Application: Zulfiyor Bakhtiyorov (D8HVUVP954)}"
export DECLIM_NOTARY_PROFILE="${DECLIM_NOTARY_PROFILE:-NTFSManager-Notary}"
BUILD_ROOT="$ROOT"
EXPORT_ROOT="${DECLIM_EXPORT_ROOT:-$ROOT}"
BUILD_DIR="$BUILD_ROOT/build"
ICON_DIR="$BUILD_DIR/icons"
LOG_DIR="$BUILD_DIR/logs"
SPEC_DIR="$BUILD_DIR/specs"
STAMP_DIR="$BUILD_DIR/stamp"
DIST_PARENT="$BUILD_ROOT/dist/macos-$PRODUCT-$APP_VERSION-$APP_BUILD"
DIST_DIR="$DIST_PARENT/$PRODUCT"
RELEASE_DIR="$BUILD_ROOT/release/$PRODUCT-$APP_VERSION-$APP_BUILD"
EXPORT_RELEASE_DIR="$EXPORT_ROOT/release/$PRODUCT-$APP_VERSION-$APP_BUILD"
EXPORT_DIST_PARENT="$EXPORT_ROOT/dist/macos-$PRODUCT-$APP_VERSION-$APP_BUILD"
echo "Version: $APP_VERSION  build $APP_BUILD  (macos-$ARCH)"

mkdir -p "$LOG_DIR" "$SPEC_DIR" "$ROOT/icons"

if [ "${1:-}" = "CLEAN" ]; then
  echo "Cleaning venv and build artifacts..."
  rm -rf "$VENV_DIR" "$ICON_DIR"
fi
rm -rf "$DIST_DIR" "$BUILD_DIR/work_$NAME_A" "$BUILD_DIR/work_$NAME_B" "$SPEC_DIR"/*.spec ./*.spec 2>/dev/null || true
mkdir -p "$SPEC_DIR"

echo "=== Build started ==="
echo "Working dir: $ROOT"
[ -n "$SCRIPT_A" ] || fail "ERROR: no detrend_advisor*.py found in $ROOT"
[ -f "$MOD_DIR/dendro_online.py" ] || fail "ERROR: folder $MOD_DIR with the dendro_*.py modules not found"
echo "Detrend Advisor source (newest): $SCRIPT_A"
HAS_B=0
if [ -f "$SCRIPT_B" ]; then HAS_B=1; echo "RingClim source: $SCRIPT_B"; else echo "RingClim source: $SCRIPT_B not found - will be skipped"; fi

# ---- Auto-create requirements if missing ----
if [ ! -f requirements.txt ]; then
  echo "Creating requirements.txt ..."
  cat > requirements.txt <<'EOF'
numpy>=1.23
pandas>=1.5
scipy>=1.9
matplotlib>=3.6
statsmodels>=0.13
XlsxWriter>=3.0
openpyxl>=3.1
xlrd>=2.0.1
PySide6==6.8.3
shiboken6==6.8.3
xarray>=2023.1
netCDF4>=1.6
EOF
fi
if [ ! -f requirements-build.txt ]; then
  echo "Creating requirements-build.txt ..."
  cat > requirements-build.txt <<'EOF'
pyinstaller>=6.0
pyinstaller-hooks-contrib
pillow>=10.0
EOF
fi

# ---- Choose Python with working tkinter (python.org builds include Tk) ----
SYS_PY=""
for cand in python3.11 python3.12 python3.13 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import tkinter" >/dev/null 2>&1; then
    SYS_PY="$(command -v "$cand")"
    break
  fi
done
[ -n "$SYS_PY" ] || fail "ERROR: no Python 3.10+ with tkinter found. Install Python 3.11 from python.org (Homebrew: brew install python-tk@3.11)."
echo "System Python: $SYS_PY ($("$SYS_PY" --version 2>&1))"

# Fail before dependency installation or compilation if distribution credentials
# are unavailable. Never silently produce an ad-hoc archive for users.
"$SYS_PY" "$ROOT/release_kit/macos_release.py" preflight \
  || fail "ERROR: Developer ID signing / Apple notarization preflight failed."

# ---- Create venv if missing ----
if [ ! -x "$VENV_PY" ]; then
  echo "Creating venv $VENV_DIR ..."
  "$SYS_PY" -m venv "$VENV_DIR" > "$LOG_DIR/venv_create.log" 2>&1 || { show_tail "$LOG_DIR/venv_create.log"; fail "ERROR: Failed to create venv."; }
fi
echo "Using venv Python: $VENV_PY"

echo "Upgrading pip/setuptools/wheel in venv..."
"$VENV_PY" -m pip install -U pip setuptools wheel > "$LOG_DIR/pip_upgrade.log" 2>&1 || { show_tail "$LOG_DIR/pip_upgrade.log"; fail "ERROR: Failed to upgrade pip."; }

echo "Installing runtime requirements..."
"$VENV_PY" -m pip install -r requirements.txt > "$LOG_DIR/pip_requirements.log" 2>&1 || { show_tail "$LOG_DIR/pip_requirements.log" 260; fail "ERROR: Failed to install requirements.txt"; }

echo "Installing build requirements..."
"$VENV_PY" -m pip install -r requirements-build.txt > "$LOG_DIR/pip_requirements_build.log" 2>&1 || { show_tail "$LOG_DIR/pip_requirements_build.log" 260; fail "ERROR: Failed to install requirements-build.txt"; }
"$VENV_PY" -m pip install -U pyinstaller pyinstaller-hooks-contrib pillow > "$LOG_DIR/pip_build_tools.log" 2>&1 || { show_tail "$LOG_DIR/pip_build_tools.log"; fail "ERROR: Failed to install PyInstaller / Pillow."; }

# ---- Preflight ----
"$VENV_PY" -c "import tkinter" >/dev/null 2>&1 || fail "ERROR: tkinter missing in venv Python."
if [ "$HAS_B" = "1" ]; then
  "$VENV_PY" -c "import PySide6, shiboken6; from PySide6.QtWidgets import QApplication" > "$LOG_DIR/pyside6_preflight.log" 2>&1 \
    || { show_tail "$LOG_DIR/pyside6_preflight.log"; fail "ERROR: PySide6 / shiboken6 import failed."; }
fi

# ---- Version stamp (build/stamp/app_version.py is found before the source copy) ----
mkdir -p "$STAMP_DIR"
"$VENV_PY" "$ROOT/release_kit/make_release.py" stamp --version "$APP_VERSION" --build "$APP_BUILD" \
  --out "$STAMP_DIR/app_version.py" || fail "ERROR: version stamp failed - check VERSION.txt (e.g. 2.0.1)"

# ---- Icons: icons/<name>.png/jpg/jpeg/webp/bmp/ico -> build/icons/<name>.icns/.png/.ico ----
echo
echo "Preparing application icons..."
if [ -f make_icons.py ]; then
  "$VENV_PY" make_icons.py --root "$ROOT" --out "$ICON_DIR" 2>&1 | tee "$LOG_DIR/icons.log"
else
  echo "WARNING: make_icons.py not found - default icons will be used."
fi

ICON_ARGS_A=()
if [ -f "$ICON_DIR/$NAME_A.icns" ]; then
  ICON_ARGS_A=(--icon "$ICON_DIR/$NAME_A.icns" --add-data "$ICON_DIR/$NAME_A.png:." --add-data "$ICON_DIR/$NAME_A.ico:.")
fi
ICON_ARGS_B=()
if [ -f "$ICON_DIR/$NAME_B.icns" ]; then
  ICON_ARGS_B=(--icon "$ICON_DIR/$NAME_B.icns" --add-data "$ICON_DIR/$NAME_B.png:." --add-data "$ICON_DIR/$NAME_B.ico:.")
fi

COMMON=(--onedir --windowed --noconfirm --clean --distpath "$DIST_DIR" --specpath "$SPEC_DIR" --paths "$STAMP_DIR" --paths "$MOD_DIR" --paths "$ROOT" --codesign-identity "$DECLIM_SIGN_IDENTITY")
UPDATER=(--hidden-import app_update --hidden-import app_product --hidden-import app_version)

# ---- Build A: Tkinter ----
echo
echo "[1/2] Building $NAME_A.app from $SCRIPT_A ..."
"$VENV_PY" -m PyInstaller "${COMMON[@]}" \
  --name "$NAME_A" \
  --workpath "$BUILD_DIR/work_$NAME_A" \
  "${UPDATER[@]}" --hidden-import app_update_tk \
  --osx-bundle-identifier "com.bakhtiyorov.detrendadvisor" \
  ${ICON_ARGS_A[@]+"${ICON_ARGS_A[@]}"} \
  --collect-all matplotlib \
  --hidden-import matplotlib.backends.backend_tkagg \
  --hidden-import statsmodels.nonparametric.smoothers_lowess \
  --exclude-module PySide6 \
  --exclude-module shiboken6 \
  --collect-all numpy \
  --collect-all pandas \
  --collect-all scipy \
  --collect-all statsmodels \
  --collect-all openpyxl \
  --collect-all xlsxwriter \
  --collect-all certifi \
  --hidden-import dendro_online \
  --hidden-import dendro_online_tk \
  --hidden-import dendro_pith \
  --hidden-import dendro_plots \
  --hidden-import dendro_ui_help \
  --hidden-import dendro_geoexport \
  --add-data "$MOD_DIR/dendro_online_countries.geojson:." \
  "$ROOT/$SCRIPT_A" > "$LOG_DIR/$NAME_A.log" 2>&1 \
  || { show_tail "$LOG_DIR/$NAME_A.log" 260; fail "ERROR: $NAME_A build failed."; }

# ---- Build B: PySide6 ----
if [ "$HAS_B" = "1" ]; then
  echo
  echo "[2/2] Building $NAME_B.app from $SCRIPT_B ..."
  "$VENV_PY" -m PyInstaller "${COMMON[@]}" \
    --name "$NAME_B" \
    --workpath "$BUILD_DIR/work_$NAME_B" \
    "${UPDATER[@]}" --hidden-import app_update_qt \
    --osx-bundle-identifier "com.bakhtiyorov.ringclim" \
    ${ICON_ARGS_B[@]+"${ICON_ARGS_B[@]}"} \
    --collect-all matplotlib \
    --collect-all PySide6 \
    --collect-all shiboken6 \
    --hidden-import PySide6.QtCore \
    --hidden-import PySide6.QtGui \
    --hidden-import PySide6.QtWidgets \
    --hidden-import matplotlib.backends.backend_qtagg \
    --collect-all numpy \
    --collect-all pandas \
    --collect-all scipy \
    --collect-all openpyxl \
    --collect-all xlsxwriter \
    --collect-all xarray \
    --collect-all netCDF4 \
    --collect-all cftime \
    --collect-all xlrd \
    --collect-all certifi \
    --hidden-import dendro_online \
    --hidden-import dendro_online_qt \
    --hidden-import dendro_plots \
    --hidden-import dendro_ui_help \
    --hidden-import dendro_geoexport \
    --hidden-import dendro_sources \
    --hidden-import dendro_sources_qt \
    --hidden-import dendro_copernicus \
    --hidden-import dendro_field \
    --hidden-import dendro_field_qt \
    --hidden-import dendro_moving \
    --hidden-import dendro_moving_qt \
    --add-data "$MOD_DIR/dendro_online_countries.geojson:." \
    "$ROOT/$SCRIPT_B" > "$LOG_DIR/$NAME_B.log" 2>&1 \
    || { show_tail "$LOG_DIR/$NAME_B.log" 320; fail "ERROR: $NAME_B build failed."; }
else
  echo
  echo "[2/2] $SCRIPT_B not found - skipping."
fi

# ---- Version in Info.plist ----
for app in "$DIST_DIR"/*.app; do
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APP_VERSION" "$app/Contents/Info.plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $APP_BUILD" "$app/Contents/Info.plist" 2>/dev/null || true
done
# PyInstaller onedir also leaves the plain folders next to the .app bundles: not needed in the package
for n in "$NAME_A" "$NAME_B"; do [ -d "$DIST_DIR/$n.app" ] && rm -rf "$DIST_DIR/$n"; done

# ---- Sign, notarize, staple, and verify every distributed app ----
mkdir -p "$RELEASE_DIR"
for app in "$DIST_DIR"/*.app; do
  [ -d "$app" ] || continue
  "$SYS_PY" "$ROOT/release_kit/macos_release.py" finalize --app "$app" --out "$RELEASE_DIR" \
    > "$LOG_DIR/release_$(basename "$app" .app).log" 2>&1 \
    || { show_tail "$LOG_DIR/release_$(basename "$app" .app).log" 320; fail "ERROR: signing/notarization failed for $app"; }
  cat "$LOG_DIR/release_$(basename "$app" .app).log"
done

cat > "$DIST_DIR/README_FIRST_RUN.txt" <<EOF
Detrend Advisor / RingClim - first run notes (macOS)

1. The .app bundles contain all Python libraries. Python does not need to be installed.
2. Detrend Advisor computes with R and the R package dplR. They are NOT bundled.
   Install R from https://cran.r-project.org and then run in R:  install.packages("dplR")
3. On first RUN the program finds Rscript automatically (/Library/Frameworks/R.framework,
   Homebrew, conda) or lets you select it manually.
4. R runs hidden in the background; progress is shown in the Runtime log tab.
5. Keep $NAME_A.app and $NAME_B.app together in the $PRODUCT folder so that
   "Actions > Open RingClim" finds RingClim and the built-in updater works.
6. Put the $PRODUCT folder where you can write (Documents, Desktop, your user folder).
7. This release is signed with Developer ID and notarized by Apple; keep the .app bundles unchanged after unpacking.
8. Updates: button "Updates" > "Check for updates...". User guides: folder docs and "Updates" > "User guides".

Version $APP_VERSION build $APP_BUILD (macos-$ARCH)
EOF
[ -f LICENSE ] && cp LICENSE "$DIST_DIR/LICENSE.txt"
[ -f CHANGELOG.md ] && cp CHANGELOG.md "$DIST_DIR/CHANGELOG.md"

# ---- Release package: portable ZIP + update manifest + guides + notes ----
echo
echo "Preparing the release in $RELEASE_DIR ..."
"$VENV_PY" "$ROOT/release_kit/make_release.py" package --product-dir "$DIST_DIR" --platform "macos-$ARCH" \
  --out "$RELEASE_DIR" --version-file "$STAMP_DIR/app_version.py" --guides "$ROOT/docs" \
  --changelog "$ROOT/CHANGELOG.md" --product-module "$MOD_DIR/app_product.py" 2>&1 | tee "$LOG_DIR/release.log"
[ "${PIPESTATUS[0]}" = "0" ] || fail "ERROR: release packaging failed - see $LOG_DIR/release.log"

# exFAT cannot preserve signed .app bundle semantics. Keep the apps and release
# build on APFS, then export only the already notarized ZIP and release assets.
if [ "$EXPORT_ROOT" != "$ROOT" ]; then
  mkdir -p "$EXPORT_RELEASE_DIR" "$EXPORT_DIST_PARENT"
  for asset in "$RELEASE_DIR"/*; do
    [ -f "$asset" ] && cp -p "$asset" "$EXPORT_RELEASE_DIR/"
  done
  for asset in "$RELEASE_DIR"/*.zip "$RELEASE_DIR"/*-update-macos-*.json "$RELEASE_DIR"/SHA256SUMS-macos-*.txt; do
    [ -f "$asset" ] && cp -p "$asset" "$EXPORT_DIST_PARENT/"
  done
  RELEASE_DIR="$EXPORT_RELEASE_DIR"
  DIST_PARENT="$EXPORT_DIST_PARENT"
fi

# ---- Optional: copy apps to /Applications ----
echo
ANSWER="n"
read -r -t 20 -p "Copy the apps to /Applications? [y/N] (auto-No in 20 s): " ANSWER || true
if [ "${ANSWER:-n}" = "y" ] || [ "${ANSWER:-n}" = "Y" ]; then
  for app in "$DIST_DIR/$NAME_A.app" "$DIST_DIR/$NAME_B.app"; do
    [ -d "$app" ] || continue
    rm -rf "/Applications/$(basename "$app")"
    cp -R "$app" /Applications/ && echo "Copied $(basename "$app") to /Applications"
  done
  touch /Applications/*.app 2>/dev/null || true
fi

echo
echo "=== Build OK ==="
echo "Built from: $SCRIPT_A and $SCRIPT_B"
echo "Version $APP_VERSION build $APP_BUILD (macos-$ARCH)"
echo "Outputs:"
echo "- $DIST_DIR/$NAME_A.app"
[ "$HAS_B" = "1" ] && echo "- $DIST_DIR/$NAME_B.app"
echo "- $DIST_DIR/README_FIRST_RUN.txt"
echo "Release for GitHub: $RELEASE_DIR"
echo "  upload DeClim-macOS-$ARCH.zip + DeClim-update-macos-$ARCH.json (+ guides if not yet there),"
echo "  to the same release as the Windows files (tag v$APP_VERSION)."
if [ -f "$ICON_DIR/$NAME_A.icns" ]; then echo "Icon A: build/icons/$NAME_A.icns"; else echo "Icon A: default - put icons/$NAME_A.png to use your logo"; fi
if [ -f "$ICON_DIR/$NAME_B.icns" ]; then echo "Icon B: build/icons/$NAME_B.icns"; else echo "Icon B: default - put icons/$NAME_B.png to use your logo"; fi
echo "Logs: $LOG_DIR"
