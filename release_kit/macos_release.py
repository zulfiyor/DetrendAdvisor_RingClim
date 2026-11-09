#!/usr/bin/env python3
"""Sign, notarize, staple, and verify a DeClim macOS app for distribution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil


DEFAULT_IDENTITY = "Developer ID Application: Zulfiyor Bakhtiyorov (D8HVUVP954)"
DEFAULT_PROFILE = "NTFSManager-Notary"
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
BUNDLE_SUFFIXES = {".app", ".framework", ".xpc", ".appex", ".plugin", ".bundle"}


def config() -> tuple[str, str]:
    return (os.environ.get("DECLIM_SIGN_IDENTITY", DEFAULT_IDENTITY),
            os.environ.get("DECLIM_NOTARY_PROFILE", DEFAULT_PROFILE))


def run(*args: object, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(arg) for arg in args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def preflight() -> None:
    identity, profile = config()
    identities = run("security", "find-identity", "-v", "-p", "codesigning", capture=True).stdout
    if identity not in identities:
        raise RuntimeError(f"Developer ID signing identity is unavailable: {identity}")
    run("xcrun", "notarytool", "history", "--keychain-profile", profile,
        "--output-format", "json", capture=True)
    print(f"Release signing identity available: {identity}")
    print(f"Notary Keychain profile available: {profile}")


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) in MACHO_MAGICS
    except OSError:
        return False


def sign_code(app: Path) -> None:
    identity, _ = config()
    run("xattr", "-cr", app, check=False)

    # PyInstaller duplicates these Qt developer tools across Resources and
    # Frameworks. Their nested Contents/MacOS entries point at generated
    # Frameworks aliases; removing only the Frameworks copy leaves dangling
    # symlinks in Resources, which makes codesign --verify fail with ENOENT.
    # These are Qt development utilities and are not used by DeClim at runtime.
    qt_alias_roots = (
        app / "Contents" / "Resources" / "PySide6",
        app / "Contents" / "Frameworks" / "PySide6",
    )
    for name in ("Assistant", "Designer", "Linguist"):
        for root in qt_alias_roots:
            for suffix in (".app", "__dot__app"):
                alias = root / f"{name}{suffix}"
                if alias.is_symlink() or alias.is_file():
                    alias.unlink()
                elif alias.is_dir():
                    shutil.rmtree(alias)

    broken_links = [path for path in app.rglob("*") if path.is_symlink() and not path.exists()]
    if broken_links:
        examples = ", ".join(str(path.relative_to(app)) for path in broken_links[:5])
        raise RuntimeError(f"App bundle contains dangling symlinks after PySide6 cleanup: {examples}")

    paths = list(app.rglob("*"))
    # Let the final app-bundle signing operation create the main executable's
    # signature. Signing it first as a standalone Mach-O and then signing the
    # containing app produced a signature Apple Notary rejected for PyInstaller
    # launchers, even though local codesign verification passed.
    app_main_executables = app / "Contents" / "MacOS"
    binaries = [path for path in paths if not path.is_symlink() and path.is_file()
                and path.parent != app_main_executables
                # PyInstaller encodes nested bundle aliases as paths ending
                # in ``__dot__app``; these contain symlinked Info.plist files
                # and are not independently signable bundles. The real .app
                # lives under Resources and is signed by the bundle pass.
                and not any(part.endswith("__dot__app") for part in path.parts)
                and is_macho(path)]
    for number, binary in enumerate(binaries, 1):
        run("codesign", "--force", "--options", "runtime", "--timestamp",
            "--sign", identity, binary)
        if number % 100 == 0:
            print(f"Signed {number} Mach-O files…", flush=True)

    bundles = [path for path in paths if not path.is_symlink() and path.is_dir()
               and path.suffix.lower() in BUNDLE_SUFFIXES]
    # Sign nested bundles from the inside out, then sign the containing app last.
    for bundle in sorted(bundles, key=lambda path: len(path.parts), reverse=True):
        run("codesign", "--force", "--options", "runtime", "--timestamp",
            "--sign", identity, bundle)
    run("codesign", "--force", "--options", "runtime", "--timestamp",
        "--sign", identity, app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    details = run("codesign", "-dv", "--verbose=4", app, capture=True).stderr
    if identity not in details or "TeamIdentifier=D8HVUVP954" not in details or "runtime" not in details:
        raise RuntimeError(f"Unexpected signature on {app}: expected Developer ID, Team ID, and hardened runtime")
    print(f"Developer ID signature verified: {app} ({len(binaries)} Mach-O files)", flush=True)


def notarize(app: Path, output: Path) -> None:
    _, profile = config()
    output.mkdir(parents=True, exist_ok=True)
    submission = output / (app.stem + "-notarization-submission.zip")
    result_path = output / (app.stem + "-notarization.json")
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, submission)
    print(f"Submitting {app.name} to Apple notarization…", flush=True)
    result = subprocess.run(
        ["xcrun", "notarytool", "submit", str(submission), "--keychain-profile", profile,
         "--wait", "--output-format", "json"], text=True, capture_output=True)
    result_path.write_text(result.stdout, encoding="utf-8")
    if result.stderr:
        result_path.with_suffix(".stderr.txt").write_text(result.stderr, encoding="utf-8")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Could not read Apple notarization result; see {result_path}") from error
    if result.returncode or report.get("status") != "Accepted":
        if report.get("id"):
            log = run("xcrun", "notarytool", "log", report["id"], "--keychain-profile", profile,
                      capture=True, check=False)
            result_path.with_name(app.stem + "-notarization-log.json").write_text(log.stdout, encoding="utf-8")
        raise RuntimeError(f"Apple notarization status for {app.name}: {report.get('status')}; see {result_path}")

    run("xcrun", "stapler", "staple", app)
    run("xcrun", "stapler", "validate", app)
    run("spctl", "--assess", "--type", "execute", "--verbose=2", app)
    submission.unlink(missing_ok=True)
    print(f"Notarized, stapled, and Gatekeeper-assessed: {app}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("preflight")
    final = sub.add_parser("finalize")
    final.add_argument("--app", required=True)
    final.add_argument("--out", required=True)
    args = parser.parse_args()

    preflight()
    if args.operation == "finalize":
        app = Path(args.app).resolve(strict=True)
        if app.suffix != ".app" or not app.is_dir():
            raise RuntimeError(f"Expected a macOS .app bundle: {app}")
        sign_code(app)
        notarize(app, Path(args.out).resolve())


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"MACOS RELEASE FAILED: {error}", file=sys.stderr)
        sys.exit(1)
