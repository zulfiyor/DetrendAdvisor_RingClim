#!/usr/bin/env python3
"""
Convert application logo images into icon files used by the build scripts.

For every application name (default: detrend_advisor, ringclim) the newest image
found in ./icons or in the project folder is used, e.g.:
    icons/detrend_advisor.jpg, icons/detrend_advisor_logo.png, ringclim.webp, ...

Outputs (default folder: build/icons):
    <name>.ico   Windows EXE / title bar / taskbar icon (16..256 px)
    <name>.png   256 px runtime icon (all platforms)
    <name>.icns  macOS application bundle icon

A plain white (or other flat) background, as produced by most image generators,
is removed automatically by flood-filling from the corners. Use --keep-background
to disable this.

Requires: Pillow
Usage:
    python make_icons.py
    python make_icons.py --out build/icons --names detrend_advisor ringclim
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

try:
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - build-time dependency
    print("ERROR: Pillow is not installed. Run: pip install pillow", file=sys.stderr)
    sys.exit(2)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".ico", ".icns")
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def find_source_image(root: str, name: str, out_dir: str) -> str | None:
    """Return the newest image whose file name starts with the application name."""
    out_abs = os.path.abspath(out_dir)
    candidates: list[str] = []
    for folder in (os.path.join(root, "icons"), root):
        for path in glob.glob(os.path.join(folder, f"{name}*")):
            if not os.path.isfile(path):
                continue
            if not path.lower().endswith(IMAGE_EXTS):
                continue
            if os.path.abspath(os.path.dirname(path)) == out_abs:
                continue
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def remove_flat_background(img: Image.Image, threshold: int = 40) -> Image.Image:
    """Make a flat background transparent by flood-filling from the four corners."""
    img = img.convert("RGBA")
    w, h = img.size
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    if any(img.getpixel(c)[3] < 250 for c in corners):
        return img
    for corner in corners:
        if img.getpixel(corner)[3] == 0:
            continue
        ImageDraw.floodfill(img, corner, (0, 0, 0, 0), thresh=threshold)
    return img


def square_canvas(img: Image.Image, size: int = 1024, margin: float = 0.04) -> Image.Image:
    """Trim transparent borders, center the logo on a square transparent canvas."""
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    side = max(img.size)
    side = int(round(side * (1.0 + 2.0 * margin)))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas.resize((size, size), Image.LANCZOS)


def build_icons(src: str, name: str, out_dir: str, keep_background: bool) -> None:
    """Write <name>.ico, <name>.png and <name>.icns from one source image."""
    with Image.open(src) as im:
        if getattr(im, "n_frames", 1) > 1 and src.lower().endswith((".ico", ".icns")):
            frames = []
            for i in range(im.n_frames):
                im.seek(i)
                frames.append(im.copy())
            base = max(frames, key=lambda f: f.width * f.height)
        else:
            base = im.copy()
    base = base.convert("RGBA")
    if not keep_background:
        base = remove_flat_background(base)
    master = square_canvas(base, 1024)

    os.makedirs(out_dir, exist_ok=True)
    ico_path = os.path.join(out_dir, f"{name}.ico")
    png_path = os.path.join(out_dir, f"{name}.png")
    icns_path = os.path.join(out_dir, f"{name}.icns")
    master.save(ico_path, format="ICO", sizes=ICO_SIZES)
    master.resize((256, 256), Image.LANCZOS).save(png_path, format="PNG")
    try:
        master.save(icns_path, format="ICNS")
    except Exception as exc:
        print(f"WARNING: could not write {icns_path}: {exc}")
    print(f"{name}: {os.path.basename(src)} -> {ico_path}, {png_path}, {icns_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create .ico/.png/.icns application icons.")
    parser.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--out", default=None, help="Output folder (default: <root>/build/icons)")
    parser.add_argument("--names", nargs="+", default=["detrend_advisor", "ringclim"])
    parser.add_argument("--keep-background", action="store_true", help="Do not remove a flat background")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    out_dir = os.path.abspath(args.out or os.path.join(root, "build", "icons"))
    missing = 0
    for name in args.names:
        src = find_source_image(root, name, out_dir)
        if src is None:
            print(f"NOTE: no image for '{name}' (put e.g. icons/{name}.png) - default icon will be used.")
            missing += 1
            for ext in (".ico", ".png", ".icns"):
                stale = os.path.join(out_dir, f"{name}{ext}")
                if os.path.isfile(stale):
                    os.remove(stale)
            continue
        build_icons(src, name, out_dir, args.keep_background)
    return 0


if __name__ == "__main__":
    sys.exit(main())
