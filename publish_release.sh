#!/usr/bin/env bash
# publish_release.sh - GitHub release for DeClim from the Mac (uses GitHub CLI "gh")
#   ./publish_release.sh upload    after ./build.sh: uploads the newest release/DeClim-*/ files
#                                  (macOS ZIP + update JSON + checksums) to release v<VERSION.txt>
#   ./publish_release.sh publish   makes the release public and "Latest"
# The repository and the draft release are created on Windows with publish_release.bat prepare
# (or here: ./publish_release.sh prepare - creates only the draft release with notes and guides).
# One-time setup: brew install gh   then   gh auth login
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
MODE="${1:-upload}"
command -v gh >/dev/null || { echo 'Install GitHub CLI: brew install gh   then: gh auth login'; exit 1; }
gh auth status >/dev/null 2>&1 || gh auth login
PY="$ROOT/.venv_build_mac/bin/python"; [ -x "$PY" ] || PY="$ROOT/.venv_build/Scripts/python.exe"; [ -x "$PY" ] || PY="python3"
REPO="$("$PY" -c "import sys; sys.path.insert(0, 'dendro_modules'); import app_product as p; print(p.REPOSITORY)")"
VER="$(head -n 1 VERSION.txt | tr -d '[:space:]')"
TAG="v$VER"
echo "Repository: $REPO   release: $TAG   mode: $MODE"
case "$MODE" in
  prepare)
    mkdir -p build
    "$PY" release_kit/make_release.py notes --version "$VER" --out build/RELEASE_NOTES.md --changelog CHANGELOG.md \
      --product-module dendro_modules/app_product.py
    if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
      gh release create "$TAG" --repo "$REPO" --draft --target main --title "DeClim $VER" --notes-file build/RELEASE_NOTES.md
    fi
    for f in docs/*.pdf docs/*.docx docs/*.zip; do [ -f "$f" ] && gh release upload "$TAG" "$f" --repo "$REPO" --clobber; done
    ;;
  upload)
    REL="$(ls -td release/DeClim-*/ 2>/dev/null | head -n 1)"
    [ -n "$REL" ] || { echo "No release/DeClim-* folder - run ./build.sh first"; exit 1; }
    REL_VERSION="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("version", ""))' "$REL/release.json" 2>/dev/null || true)"
    [ "$REL_VERSION" = "$VER" ] || { echo "ERROR: newest package is version ${REL_VERSION:-unknown}, but VERSION.txt is $VER. Build a successful $VER release before uploading."; exit 1; }
    gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1 || { echo "Release $TAG not found - run prepare first"; exit 1; }
    shopt -s nullglob
    archives=("$REL"DeClim-Windows-*.zip "$REL"DeClim-macOS-*.zip)
    manifests=("$REL"DeClim-update-*.json)
    [ ${#archives[@]} -gt 0 ] || { echo "ERROR: no application ZIP in $REL"; exit 1; }
    [ ${#manifests[@]} -gt 0 ] || { echo "ERROR: no update manifest in $REL"; exit 1; }
    for manifest in "${manifests[@]}"; do
      "$PY" release_kit/make_release.py verify --zip "$(dirname "$manifest")/$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["archive"])' "$manifest")" --manifest "$manifest"
    done
    for f in "$REL"*.zip "$REL"*-update-*.json "$REL"SHA256SUMS-*.txt; do
      [ -f "$f" ] && { echo " - $(basename "$f")"; gh release upload "$TAG" "$f" --repo "$REPO" --clobber; }
    done
    ;;
  publish)
    gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[].name'
    read -r -p "Publish $TAG now (public, Latest)? [y/N] " a
    [ "$a" = "y" ] || [ "$a" = "Y" ] || exit 1
    gh release edit "$TAG" --repo "$REPO" --draft=false --latest
    ;;
  *) echo "Use: prepare | upload | publish"; exit 1 ;;
esac
echo "Done: https://github.com/$REPO/releases"
