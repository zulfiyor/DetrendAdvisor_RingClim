#!/usr/bin/env python3
"""
make_release.py - build-time helper for portable releases with automatic updates (see app_update.py).

Commands (run by build.bat / build.sh, usable by hand):

  stamp    --version 2.0.1 --build 20261028153000 --out dendro_modules/app_version.py
  package  --product-dir dist/<platform-version-build>/DeClim --platform windows-x64 --out release/DeClim-2.0.1-...
           [--guides docs] [--changelog CHANGELOG.md]
           writes <PRODUCT>-INSTALLED.json into the product folder, the portable ZIP, its update manifest
           <PRODUCT>-update-<platform>.json, SHA256SUMS.txt, RELEASE_NOTES.md and copies the guides.
  notes    --version 2.0.1 --out RELEASE_NOTES.md [--changelog CHANGELOG.md]
  verify   --zip release/.../DeClim-Windows-x64.zip       (checks the zip against its manifest)

The product (name, repository, guides) is read from app_product.py (--product-module, default:
the first app_product.py found in the project).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1


def load_product(path: str = ""):
    cand = Path(path) if path else next((p for p in ROOT.rglob("app_product.py")
                                         if ".venv" not in str(p) and "build" not in p.parts), None)
    if cand is None or not cand.is_file():
        sys.exit("app_product.py not found")
    spec = importlib.util.spec_from_file_location("app_product", cand)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_name(product: str, key: str) -> str:
    system, arch = key.split("-", 1)
    return f"{product}-{ {'windows': 'Windows', 'macos': 'macOS'}.get(system, system.capitalize()) }-{arch}.zip"


def detect_platform() -> str:
    import platform
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    return f"{system}-" + {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(m, m)


# ------------------------------------------------------------------------------------------------- stamp
def cmd_stamp(a) -> None:
    v = a.version.strip().lstrip("vV")
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", v) or not re.fullmatch(r"\d+", a.build):
        sys.exit("version must be like 2.0.1 and build a number")
    Path(a.out).write_text('"""Version of this build - written by release_kit/make_release.py stamp '
                           '(do not edit by hand)."""\n'
                           f'VERSION = "{v}"\nBUILD = "{a.build}"\n', encoding="utf-8")
    print(f"Version stamped: {v} build {a.build} -> {a.out}")


# ------------------------------------------------------------------------------------------------- notes
def changelog_section(path: Path, version: str) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^##\s*\[?v?{re.escape(version)}\]?.*?$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def release_notes(prod, version: str, build: str, changelog: Path) -> str:
    repo = prod.REPOSITORY
    dl = f"https://github.com/{repo}/releases/latest/download"
    name = prod.PRODUCT
    news = changelog_section(changelog, version) or "- See CHANGELOG.md."
    guides = "\n".join(f"- [{label}]({dl}/{asset})" for label, asset in getattr(prod, "GUIDES", []))
    build_line = f"Build {build} · " if str(build).strip() not in ("", "0") else ""
    return f"""# {getattr(prod, 'DISPLAY_NAME', name)} {version}

{build_line}{_dt.date.today().isoformat()}

## What's new
{news}

## Download (ready to use, nothing to install)

| System | Package |
|---|---|
| Windows 10/11 (64-bit) | [{archive_name(name, 'windows-x64')}]({dl}/{archive_name(name, 'windows-x64')}) |
| macOS Intel | [{archive_name(name, 'macos-x64')}]({dl}/{archive_name(name, 'macos-x64')}) |

1. Download the ZIP for your system and unpack it into a folder where you can write (Documents, Desktop or your user folder - not *Program Files*).
2. Windows: open `{name}\\detrend_advisor\\detrend_advisor.exe` or `{name}\\ringclim\\ringclim.exe`.
   macOS: open `{name}/detrend_advisor.app` or `{name}/ringclim.app`.
3. Detrend Advisor computes with **R + dplR** (free): the program finds R or helps to install it on first use.

The Intel package can also run on Apple Silicon through Rosetta 2 until a native Apple silicon package is available.

## Updates
The programs check this page once a day (*Updates* button → *Check for updates…*). A new version is downloaded only after you agree, verified (SHA-256) and installed when the program windows are closed; the previous version is kept for *Restore previous version*. Your settings, projects and own files are kept.

## User guides
{guides or '- see the assets below'}

## Contact
Suggestions, comments and problem reports: *Updates* → *Contact the developer…*, e-mail **{getattr(prod, 'CONTACT_EMAIL', '')}**, or [GitHub issues](https://github.com/{repo}/issues).

<sub>Each ZIP has a matching `{name}-update-<platform>.json` used by the built-in updater - please keep both. Checksums: `SHA256SUMS-<platform>.txt`.</sub>
"""


def cmd_notes(a) -> None:
    prod = load_product(a.product_module)
    Path(a.out).write_text(release_notes(prod, a.version, a.build or "0", Path(a.changelog)), encoding="utf-8")
    print("Release notes:", a.out)


# ------------------------------------------------------------------------------------------------- package
def make_index(prod, folder: Path, key: str, version: str, build: str) -> None:
    idx_name = f"{prod.PRODUCT}-INSTALLED.json"
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                   if (p.is_file() or p.is_symlink()) and p.name != idx_name)
    write_json(folder / idx_name, {"schema": SCHEMA, "product": prod.PRODUCT, "platform": key, "version": version,
                                   "build": build, "built": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "files": files})
    print(f"Index: {len(files)} files, {key} {version}/{build}")


def make_zip(folder: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(folder), str(target)],
                       check=True)
    else:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
            for item in sorted(folder.rglob("*")):
                if item.is_file():
                    z.write(item, item.relative_to(folder.parent).as_posix())
    with zipfile.ZipFile(target) as z:
        bad = z.testzip()
        if bad:
            sys.exit("Damaged archive member: " + bad)
    print(f"Archive: {target} ({target.stat().st_size / 1024 ** 2:.1f} MB)")


def cmd_package(a) -> None:
    prod = load_product(a.product_module)
    folder = Path(a.product_dir).resolve()
    if folder.name != prod.PRODUCT:
        sys.exit(f"The product folder must be named {prod.PRODUCT}: {folder}")
    if not folder.is_dir():
        sys.exit(f"Product folder does not exist: {folder}. Build the programs first and check --product-dir.")
    vmod = {}
    exec(Path(a.version_file).read_text(encoding="utf-8"), vmod)
    version, build = vmod["VERSION"], vmod["BUILD"]
    key = a.platform or detect_platform()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # guides inside the product (docs/) and next to the release
    gdir = Path(a.guides) if a.guides else None
    if gdir and gdir.is_dir():
        docs = folder / "docs"
        docs.mkdir(exist_ok=True)
        for _label, asset in getattr(prod, "GUIDES", []):
            src = gdir / asset
            if src.is_file():
                shutil.copy2(src, docs / asset)
                shutil.copy2(src, out / asset)
                print("Guide:", asset)
            else:
                print("WARNING: guide not found:", src)
    make_index(prod, folder, key, version, build)
    zpath = out / archive_name(prod.PRODUCT, key)
    make_zip(folder, zpath)
    man = {"schema": SCHEMA, "product": prod.PRODUCT, "platform": key, "version": version, "build": build,
           "root": prod.PRODUCT, "archive": zpath.name, "size": zpath.stat().st_size, "sha256": sha256(zpath)}
    mpath = out / f"{prod.PRODUCT}-update-{key}.json"
    write_json(mpath, man)
    sums = out / f"SHA256SUMS-{key}.txt"
    lines = [ln for ln in (sums.read_text(encoding="utf-8").splitlines() if sums.exists() else [])
             if not ln.endswith(zpath.name)]
    lines.append(f"{man['sha256']}  {zpath.name}")
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "RELEASE_NOTES.md").write_text(release_notes(prod, version, build, Path(a.changelog)), encoding="utf-8")
    (out / "release.json").write_text(json.dumps({"tag": f"v{version}", "title": f"{prod.PRODUCT} {version}",
                                                  "version": version, "build": build}, indent=2), encoding="utf-8")
    print()
    print("Release folder:", out)
    print(f"Upload to ONE GitHub release (tag v{version}): {zpath.name} + {mpath.name}"
          + " + the guides; paste RELEASE_NOTES.md as the description; mark it 'Latest'.")


def cmd_verify(a) -> None:
    z = Path(a.zip)
    man = json.loads(next(z.parent.glob("*-update-*.json")).read_text(encoding="utf-8")) if not a.manifest else \
        json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    ok = z.stat().st_size == man["size"] and sha256(z) == man["sha256"]
    print("OK" if ok else "MISMATCH", z.name, man["version"], man["build"])
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stamp")
    s.add_argument("--version", required=True)
    s.add_argument("--build", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_stamp)
    n = sub.add_parser("notes")
    n.add_argument("--version", required=True)
    n.add_argument("--build", default="0")
    n.add_argument("--out", default="RELEASE_NOTES.md")
    n.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    n.add_argument("--product-module", default="")
    n.set_defaults(fn=cmd_notes)
    p = sub.add_parser("package")
    p.add_argument("--product-dir", required=True)
    p.add_argument("--platform", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--version-file", required=True)
    p.add_argument("--guides", default="")
    p.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    p.add_argument("--product-module", default="")
    p.set_defaults(fn=cmd_package)
    v = sub.add_parser("verify")
    v.add_argument("--zip", required=True)
    v.add_argument("--manifest", default="")
    v.set_defaults(fn=cmd_verify)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
