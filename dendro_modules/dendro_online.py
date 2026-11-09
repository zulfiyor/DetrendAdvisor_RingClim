#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_online.py - online data access for DeClim tools (RingClim, Detrend Advisor).

Pure-Python backend (no Qt / Tk). Can be imported by the GUI apps or used
on its own from the command line.

Features
--------
* CRU TS (Climatic Research Unit, UEA) gridded monthly climate:
    - discovers available releases and data directories on crudata.uea.ac.uk,
    - downloads only the decadal NetCDF files that cover the requested period,
    - extracts one or many targets (point bilinear / nearest cell / bbox mean)
      in a single pass per file, including the CRU 'stn' station count,
    - downloads are temporary by default (deleted right after extraction).
* ITRDB (NOAA NCEI World Data Service for Paleoclimatology):
    - searches tree-ring studies by bounding box, species, period and text,
    - lists available files (raw measurements .rwl, chronologies .crn, ...),
    - downloads selected files.
* Parsers: Tucson chronology (.crn) and Tucson ring width (.rwl) with
  correct precision handling (999 -> 0.01 mm, -9999 -> 0.001 mm).
* Location map: draws only the country that contains the point (plus
  neighbours inside the view), the CRU 0.5 deg grid, the grid cells that are
  used for interpolation / averaging and their weights.

Command line
------------
    python dendro_online.py probe [--with-cru] [--out DIR]
    python dendro_online.py cru-releases
    python dendro_online.py cru-map --lat 39.9 --lon 69.0 [--mode bilinear] --out map.png
    python dendro_online.py cru-extract --var tmp pre --lat 39.9 --lon 69.0 --start 1950 --end 2020 --out DIR
    python dendro_online.py itrdb-search --min-lat 36 --max-lat 42 --min-lon 66 --max-lon 76 [--out sites.csv]
    python dendro_online.py itrdb-download --url URL [--url URL ...] --out DIR
    python dendro_online.py crn-to-csv FILE.crn [--out FILE.csv]
    python dendro_online.py rwl-to-csv FILE.rwl [--out FILE.csv]
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import html
import json
import math
import os
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

__version__ = "1.0.0"

USER_AGENT = f"DeClim-dendro_online/{__version__} (+tree-ring research software)"

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ---------------------------------------------------------------------------
# CRU TS constants
# ---------------------------------------------------------------------------

CRU_HRG_URL = "https://crudata.uea.ac.uk/cru/data/hrg/"

# Fallback when the HTML index cannot be parsed. version -> data directory
KNOWN_CRU_RELEASES: Dict[str, str] = {
    "4.10": "cruts.2604091129.v4.10",
    "4.09": "cruts.2503051245.v4.09",
}

CRU_GRID_RES = 0.5  # degrees; CRU TS 4.x grid cell centres at -89.75 ... 89.75, -179.75 ... 179.75


@dataclass(frozen=True)
class CruVariable:
    code: str
    label: str
    units: str
    ringclim_param: str       # climate type used by RingClim
    monthly_agg: str          # sensible aggregation over months: "mean" or "sum"


CRU_VARIABLES: Dict[str, CruVariable] = {
    "tmp": CruVariable("tmp", "Mean temperature", "degC", "Temperature (mean)", "mean"),
    "tmn": CruVariable("tmn", "Minimum temperature", "degC", "Temperature (min)", "mean"),
    "tmx": CruVariable("tmx", "Maximum temperature", "degC", "Temperature (max)", "mean"),
    "dtr": CruVariable("dtr", "Diurnal temperature range", "degC", "Diurnal temperature range", "mean"),
    "pre": CruVariable("pre", "Precipitation", "mm/month", "Precipitation", "sum"),
    "wet": CruVariable("wet", "Wet day frequency", "days", "Wet days", "sum"),
    "frs": CruVariable("frs", "Frost day frequency", "days", "Frost days", "sum"),
    "vap": CruVariable("vap", "Vapour pressure", "hPa", "Vapour pressure", "mean"),
    "cld": CruVariable("cld", "Cloud cover", "%", "Cloud cover", "mean"),
    "pet": CruVariable("pet", "Potential evapotranspiration", "mm/day", "PET", "mean"),
}

CRU_CITATION = (
    "Harris, I., Osborn, T.J., Jones, P. & Lister, D. (2020) Version 4 of the CRU TS "
    "monthly high-resolution gridded multivariate climate dataset. Scientific Data 7, 109. "
    "https://doi.org/10.1038/s41597-020-0453-3"
)

# ---------------------------------------------------------------------------
# ITRDB constants
# ---------------------------------------------------------------------------

NOAA_SEARCH_URLS = [
    "https://www.ncei.noaa.gov/access/paleo-search/study/search.json",
    "https://www.ncdc.noaa.gov/paleo-search/study/search.json",
]
NOAA_STUDY_PAGE = "https://www.ncei.noaa.gov/access/paleo-search/study/{id}"
NOAA_TREE_RING_TYPE_ID = "18"

ITRDB_CITATION = (
    "International Tree-Ring Data Bank (ITRDB), NOAA NCEI World Data Service for "
    "Paleoclimatology. Cite the original contributors and the study DOI."
)

# ITRDB measurement-parameter suffixes on the file stem (after the site code)
ITRDB_PARAM_SUFFIX = {
    "": "Total ring width",
    "e": "Earlywood width",
    "l": "Latewood width",
    "w": "Total ring width",
    "x": "Maximum density",
    "i": "Minimum density",
}


class OnlineError(RuntimeError):
    """Any problem talking to a remote service or parsing its output."""


class CancelledError(RuntimeError):
    """Raised when the user cancels a long operation."""


ProgressCallback = Callable[[str, int, int, str], None]
"""progress(stage, done, total, message); total may be 0 when unknown."""

CancelCheck = Callable[[], bool]


def _noop_progress(stage: str, done: int, total: int, message: str) -> None:
    pass


def _never_cancel() -> bool:
    return False


def make_cancel_check(event: Optional[threading.Event]) -> CancelCheck:
    if event is None:
        return _never_cancel
    return event.is_set


# ---------------------------------------------------------------------------
# Paths / cache
# ---------------------------------------------------------------------------

def default_cache_dir() -> str:
    """Per-user cache directory for temporary downloads."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "DeClim", "online_cache")
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Caches", "DeClim", "online_cache")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        path = os.path.join(base, "DeClim", "online_cache")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Disk cache of extracted series (shared by all running copies of the programs)
# ---------------------------------------------------------------------------

SERIES_CACHE_DAYS = 180.0


def _series_cache_path(kind: str, key_obj: Any) -> str:
    import hashlib

    raw = json.dumps([kind, key_obj], sort_keys=True, default=str)
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    d = os.path.join(default_cache_dir(), "series_cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{kind}_{h}.pkl")


def series_cache_get(kind: str, key_obj: Any) -> Any:
    """A result stored by series_cache_put (None if missing, too old or unreadable)."""
    import pickle

    p = _series_cache_path(kind, key_obj)
    try:
        if not os.path.exists(p) or (time.time() - os.path.getmtime(p)) > SERIES_CACHE_DAYS * 86400:
            return None
        with open(p, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def series_cache_put(kind: str, key_obj: Any, value: Any) -> None:
    import pickle

    p = _series_cache_path(kind, key_obj)
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            pickle.dump(value, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def targets_key(targets: Sequence[Any]) -> List[Any]:
    return [[t.name, round(float(t.lat), 5), round(float(t.lon), 5), t.mode,
             [round(float(x), 5) for x in t.bbox] if t.bbox else None] for t in targets]


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_work_dir(preferred_parent: Optional[str] = None) -> str:
    """Create a temporary directory with an ASCII-only path.

    netCDF4 / HDF5 on Windows cannot open files whose path contains
    non-ASCII characters, so decompressed NetCDF files go here.
    """
    candidates: List[str] = []
    if preferred_parent:
        candidates.append(preferred_parent)
    candidates.append(tempfile.gettempdir())
    if sys.platform.startswith("win"):
        drive = os.environ.get("SystemDrive", "C:") + os.sep
        candidates.append(os.path.join(drive, "ProgramData", "DeClim_tmp"))
        candidates.append(os.path.join(drive, "DeClim_tmp"))
    for parent in candidates:
        if not parent or not _is_ascii(os.path.abspath(parent)):
            continue
        try:
            os.makedirs(parent, exist_ok=True)
            return tempfile.mkdtemp(prefix="dendro_online_", dir=parent)
        except OSError:
            continue
    return tempfile.mkdtemp(prefix="dendro_online_")


def human_size(n_bytes: float) -> str:
    n = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SSL_CONTEXT: Optional[ssl.SSLContext] = None


def _ssl_context() -> ssl.SSLContext:
    """SSL context.

    Windows / Linux: system certificate store (works behind corporate proxies).
    macOS: certifi bundle when available, because python.org / frozen builds
    do not see the macOS keychain.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        ctx: Optional[ssl.SSLContext] = None
        if sys.platform == "darwin":
            try:
                import certifi  # type: ignore

                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = None
        if ctx is None:
            ctx = ssl.create_default_context()
            # also trust the certifi bundle: some servers (e.g. opendata.dwd.de) send a chain that the
            # Windows store or an un-activated conda Python cannot complete on its own
            try:
                import certifi  # type: ignore

                ctx.load_verify_locations(cafile=certifi.where())
            except Exception:
                pass
        _SSL_CONTEXT = ctx
    return _SSL_CONTEXT


def _open_url(url: str, timeout: float = 60.0, headers: Optional[Dict[str, str]] = None):
    req_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def http_head_size(url: str, timeout: float = 20.0) -> Optional[int]:
    """Content-Length of a remote file (HEAD request); None if unknown."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            n = resp.headers.get("Content-Length")
            return int(n) if n and n.isdigit() else None
    except Exception:
        return None


def http_get_bytes(url: str, timeout: float = 60.0, retries: int = 2) -> bytes:
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            with _open_url(url, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410):
                raise OnlineError(f"HTTP {exc.code} for {url}") from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
        time.sleep(1.5 * (attempt + 1))
    raise OnlineError(f"Cannot reach {url}: {last_exc}")


def http_get_text(url: str, timeout: float = 60.0, retries: int = 2) -> str:
    data = http_get_bytes(url, timeout=timeout, retries=retries)
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def http_get_json(url: str, timeout: float = 90.0, retries: int = 2) -> Any:
    text = http_get_text(url, timeout=timeout, retries=retries)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OnlineError(f"Invalid JSON from {url}: {exc}") from exc


def download_file(
    url: str,
    dest_path: str,
    progress: ProgressCallback = _noop_progress,
    cancel: CancelCheck = _never_cancel,
    expected_size: Optional[int] = None,
    timeout: float = 60.0,
    retries: int = 3,
    stage: str = "download",
) -> str:
    """Stream a file to disk with resume (HTTP Range), progress and cancel.

    The file is written to ``dest_path + '.part'`` and renamed on success.
    An existing complete file (same size as expected, if known) is reused.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
    if os.path.exists(dest_path):
        if expected_size is None or os.path.getsize(dest_path) == expected_size:
            size = os.path.getsize(dest_path)
            progress(stage, size, size, f"Using cached {os.path.basename(dest_path)}")
            return dest_path
        os.remove(dest_path)

    part_path = dest_path + ".part"
    name = os.path.basename(dest_path)
    last_exc: Optional[BaseException] = None

    for attempt in range(retries + 1):
        if cancel():
            raise CancelledError("Download cancelled")
        have = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        headers = {"Range": f"bytes={have}-"} if have > 0 else None
        try:
            with _open_url(url, timeout=timeout, headers=headers) as resp:
                status = getattr(resp, "status", 200)
                if have > 0 and status != 206:
                    have = 0  # server ignored Range -> restart
                length_hdr = resp.headers.get("Content-Length")
                total = (int(length_hdr) + have) if length_hdr and length_hdr.isdigit() else (expected_size or 0)
                mode = "ab" if have > 0 else "wb"
                done = have
                t0 = time.time()
                last_report = 0.0
                with open(part_path, mode) as fh:
                    while True:
                        if cancel():
                            raise CancelledError("Download cancelled")
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_report > 0.25:
                            last_report = now
                            speed = (done - have) / max(now - t0, 1e-6)
                            progress(stage, done, total,
                                     f"{name}: {human_size(done)} / {human_size(total) if total else '?'}"
                                     f"  ({human_size(speed)}/s)")
            if total and os.path.getsize(part_path) < total:
                raise OnlineError(f"Incomplete download of {name}")
            os.replace(part_path, dest_path)
            size = os.path.getsize(dest_path)
            progress(stage, size, size, f"Downloaded {name} ({human_size(size)})")
            return dest_path
        except CancelledError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have > 0:  # range not satisfiable -> restart
                try:
                    os.remove(part_path)
                except OSError:
                    pass
                last_exc = exc
                continue
            if exc.code in (401, 403, 404, 410):
                raise OnlineError(f"HTTP {exc.code} for {url}") from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, OnlineError) as exc:
            last_exc = exc
        progress(stage, 0, 0, f"Retrying {name} ({attempt + 1}/{retries}) after error: {last_exc}")
        time.sleep(2.0 * (attempt + 1))
    raise OnlineError(f"Download failed for {url}: {last_exc}")


def _links_from_html(page: str) -> List[str]:
    return [html.unescape(m) for m in re.findall(r'href\s*=\s*["\']([^"\']+)["\']', page, flags=re.I)]


# ---------------------------------------------------------------------------
# CRU TS catalogue
# ---------------------------------------------------------------------------

@dataclass
class CruRelease:
    version: str                 # e.g. "4.10"
    data_dir: str                # e.g. "cruts.2604091129.v4.10"
    end_year: int                # last year in the release

    @property
    def base_url(self) -> str:
        return f"{CRU_HRG_URL}cru_ts_{self.version}/{self.data_dir}/"

    def variable_url(self, var: str) -> str:
        return f"{self.base_url}{var}/"


@dataclass
class CruFile:
    version: str
    var: str
    start_year: int
    end_year: int
    url: str
    size_bytes: Optional[int] = None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def _version_key(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _guess_end_year(version: str) -> int:
    # CRU TS 4.0x releases: 4.08 -> 2023, 4.09 -> 2024, 4.10 -> 2025
    parts = _version_key(version)
    if len(parts) >= 2 and parts[0] == 4:
        return 2015 + parts[1]
    return time.gmtime().tm_year - 1


def list_cru_versions(timeout: float = 30.0) -> List[str]:
    """Return available CRU TS versions (newest first)."""
    versions: set = set()
    try:
        page = http_get_text(CRU_HRG_URL, timeout=timeout)
        for link in _links_from_html(page):
            m = re.search(r"cru_ts_(\d+\.\d+)", link)
            if m:
                versions.add(m.group(1))
    except OnlineError:
        pass
    versions.update(KNOWN_CRU_RELEASES.keys())
    return sorted(versions, key=_version_key, reverse=True)


def resolve_cru_release(version: str, timeout: float = 30.0) -> CruRelease:
    """Find the main data directory of a CRU TS release."""
    url = f"{CRU_HRG_URL}cru_ts_{version}/"
    data_dir: Optional[str] = None
    try:
        page = http_get_text(url, timeout=timeout)
        found = []
        for link in _links_from_html(page) + re.findall(r"cruts\.\d+\.v\d+\.\d+", page):
            m = re.search(r"(cruts\.\d+\.v\d+\.\d+)", link)
            if m:
                found.append(m.group(1))
        if found:
            data_dir = sorted(set(found))[-1]
    except OnlineError:
        pass
    if data_dir is None:
        data_dir = KNOWN_CRU_RELEASES.get(version)
    if data_dir is None:
        raise OnlineError(f"Cannot find the data directory for CRU TS {version}")
    return CruRelease(version=version, data_dir=data_dir, end_year=_guess_end_year(version))


def _parse_size_kb(text: str) -> Optional[int]:
    try:
        return int(float(text) * 1024)
    except (TypeError, ValueError):
        return None


def list_cru_files(release: CruRelease, var: str, timeout: float = 30.0) -> List[CruFile]:
    """List decadal NetCDF files (*.dat.nc.gz) of one variable.

    Falls back to the standard naming scheme when the listing is not readable.
    """
    var = var.lower()
    url = release.variable_url(var)
    pattern = re.compile(
        r"cru_ts(\d+\.\d+)\.(\d{4})\.(\d{4})\." + re.escape(var) + r"\.dat\.nc\.gz", re.I
    )
    files: Dict[Tuple[int, int], CruFile] = {}
    try:
        page = http_get_text(url, timeout=timeout)
        text_only = re.sub(r"<[^>]+>", " ", page)
        for m in pattern.finditer(page):
            y0, y1 = int(m.group(2)), int(m.group(3))
            fname = m.group(0)
            size = None
            # "Size (kb)" table (crudata style): filename followed by an integer column
            sm = re.search(re.escape(fname) + r"\s+(\d{3,})\s", text_only + " ")
            if sm and "size (kb)" in text_only.lower():
                size = _parse_size_kb(sm.group(1))
            files[(y0, y1)] = CruFile(release.version, var, y0, y1, url + fname, size)
    except OnlineError:
        pass

    if files:
        end_years = [k[1] for k in files]
        release.end_year = max(release.end_year, max(end_years))
    else:
        # Fallback: build names from the standard decadal scheme
        y = 1901
        while y <= release.end_year:
            y_end = min(y + 9, release.end_year)
            fname = f"cru_ts{release.version}.{y}.{y_end}.{var}.dat.nc.gz"
            files[(y, y_end)] = CruFile(release.version, var, y, y_end, url + fname, None)
            y += 10
    return sorted(files.values(), key=lambda f: (f.end_year - f.start_year > 20, f.start_year))


def select_cru_files(files: Sequence[CruFile], start_year: int, end_year: int,
                     prefer_decadal: bool = True) -> List[CruFile]:
    """Pick the smallest set of files that covers [start_year, end_year]."""
    decadal = [f for f in files if f.end_year - f.start_year <= 10]
    full = [f for f in files if f.end_year - f.start_year > 10]
    chosen = [f for f in decadal if f.end_year >= start_year and f.start_year <= end_year]
    covered = set()
    for f in chosen:
        covered.update(range(f.start_year, f.end_year + 1))
    needed = set(range(start_year, end_year + 1))
    max_avail = max((f.end_year for f in files), default=end_year)
    needed = {y for y in needed if y <= max_avail}
    if (not prefer_decadal or not needed.issubset(covered)) and full:
        return [max(full, key=lambda f: f.end_year)]
    return sorted(chosen, key=lambda f: f.start_year)


def estimate_download_size(files: Sequence[CruFile]) -> Tuple[int, bool]:
    """Return (bytes, exact) - exact is False if some sizes are estimated."""
    total = 0
    exact = True
    for f in files:
        if f.size_bytes:
            total += f.size_bytes
        else:
            exact = False
            total += int(36 * 1024 * 1024 * max(1, (f.end_year - f.start_year + 1)) / 10)
    return total, exact


# ---------------------------------------------------------------------------
# Grid geometry (CRU 0.5 deg regular grid) and cell selection
# ---------------------------------------------------------------------------

@dataclass
class GridCell:
    lat: float          # cell centre
    lon: float
    weight: float       # interpolation / averaging weight (normalised over valid cells)
    i_lat: int = -1     # index in the file (filled during extraction)
    i_lon: int = -1
    valid: Optional[bool] = None  # None = unknown (before extraction)


@dataclass
class ExtractionTarget:
    """One thing to extract from the grid."""
    name: str
    lat: float
    lon: float
    mode: str = "bilinear"            # "bilinear" | "nearest" | "bbox_mean"
    bbox: Optional[Tuple[float, float, float, float]] = None  # (min_lat, max_lat, min_lon, max_lon)

    def validate(self) -> None:
        if not (-90.0 <= self.lat <= 90.0):
            raise ValueError(f"Latitude out of range: {self.lat}")
        if not (-180.0 <= self.lon <= 360.0):
            raise ValueError(f"Longitude out of range: {self.lon}")
        if self.mode not in ("bilinear", "nearest", "bbox_mean"):
            raise ValueError(f"Unknown extraction mode: {self.mode}")
        if self.mode == "bbox_mean":
            if not self.bbox:
                raise ValueError("bbox_mean requires a bounding box")
            a, b, c, d = self.bbox
            if a > b or c > d:
                raise ValueError("Bounding box must be (min_lat, max_lat, min_lon, max_lon)")


def _wrap_lon(lon: float) -> float:
    """Wrap longitude to [-180, 180)."""
    x = ((float(lon) + 180.0) % 360.0) - 180.0
    return x


def regular_grid_axes(res: float = CRU_GRID_RES) -> Tuple[np.ndarray, np.ndarray]:
    lats = np.arange(-90.0 + res / 2.0, 90.0, res)
    lons = np.arange(-180.0 + res / 2.0, 180.0, res)
    return lats, lons


def _bilinear_cells(lats: np.ndarray, lons: np.ndarray, lat: float, lon: float) -> List[GridCell]:
    """Up to 4 surrounding cell centres with bilinear weights (axes ascending)."""
    lon = _wrap_lon(lon)
    # latitude bracket
    if lat <= lats[0]:
        i0 = i1 = 0
        ty = 0.0
    elif lat >= lats[-1]:
        i0 = i1 = len(lats) - 1
        ty = 0.0
    else:
        i1 = int(np.searchsorted(lats, lat, side="right"))
        i0 = i1 - 1
        ty = (lat - lats[i0]) / (lats[i1] - lats[i0])
    # longitude bracket (periodic)
    n = len(lons)
    if lon < lons[0] or lon >= lons[-1]:
        j0, j1 = n - 1, 0
        span = (lons[0] + 360.0) - lons[-1]
        dx = lon - lons[-1] if lon >= lons[-1] else lon + 360.0 - lons[-1]
        tx = dx / span
    else:
        j1 = int(np.searchsorted(lons, lon, side="right"))
        j0 = j1 - 1
        tx = (lon - lons[j0]) / (lons[j1] - lons[j0])
    raw = [
        (i0, j0, (1 - ty) * (1 - tx)),
        (i0, j1, (1 - ty) * tx),
        (i1, j0, ty * (1 - tx)),
        (i1, j1, ty * tx),
    ]
    merged: Dict[Tuple[int, int], float] = {}
    for i, j, w in raw:
        merged[(i, j)] = merged.get((i, j), 0.0) + w
    cells = [
        GridCell(float(lats[i]), float(lons[j]), float(w), i, j)
        for (i, j), w in merged.items() if w > 1e-12
    ]
    return cells


def _nearest_cell(lats: np.ndarray, lons: np.ndarray, lat: float, lon: float) -> List[GridCell]:
    lon = _wrap_lon(lon)
    i = int(np.argmin(np.abs(lats - lat)))
    dlon = np.abs(((lons - lon) + 180.0) % 360.0 - 180.0)
    j = int(np.argmin(dlon))
    return [GridCell(float(lats[i]), float(lons[j]), 1.0, i, j)]


def _bbox_cells(lats: np.ndarray, lons: np.ndarray,
                bbox: Tuple[float, float, float, float]) -> List[GridCell]:
    min_lat, max_lat, min_lon, max_lon = bbox
    ii = np.where((lats >= min_lat) & (lats <= max_lat))[0]
    lo0, lo1 = _wrap_lon(min_lon), _wrap_lon(max_lon)
    if max_lon - min_lon >= 360.0:
        jj = np.arange(len(lons))
    elif lo0 <= lo1:
        jj = np.where((lons >= lo0) & (lons <= lo1))[0]
    else:  # crosses the dateline
        jj = np.where((lons >= lo0) | (lons <= lo1))[0]
    cells: List[GridCell] = []
    for i in ii:
        w = math.cos(math.radians(float(lats[i])))
        for j in jj:
            cells.append(GridCell(float(lats[i]), float(lons[j]), w, int(i), int(j)))
    total = sum(c.weight for c in cells)
    if total > 0:
        for c in cells:
            c.weight /= total
    return cells


def cells_for_target(target: ExtractionTarget,
                     lats: Optional[np.ndarray] = None,
                     lons: Optional[np.ndarray] = None) -> List[GridCell]:
    """Grid cells (with weights) that a target will use.

    Without axes the standard CRU 0.5 deg grid is assumed, so this works
    before anything is downloaded (for the location map preview).
    """
    target.validate()
    if lats is None or lons is None:
        lats, lons = regular_grid_axes()
    if target.mode == "nearest":
        return _nearest_cell(lats, lons, target.lat, target.lon)
    if target.mode == "bbox_mean":
        assert target.bbox is not None
        return _bbox_cells(lats, lons, target.bbox)
    return _bilinear_cells(lats, lons, target.lat, target.lon)


# ---------------------------------------------------------------------------
# NetCDF extraction
# ---------------------------------------------------------------------------

def _decompress_gz(src: str, dst: str, cancel: CancelCheck = _never_cancel) -> str:
    with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            if cancel():
                raise CancelledError("Cancelled during decompression")
            chunk = fin.read(1024 * 1024 * 4)
            if not chunk:
                break
            fout.write(chunk)
    return dst


def _nc_time_to_year_month(time_var) -> Tuple[np.ndarray, np.ndarray]:
    from netCDF4 import num2date

    units = getattr(time_var, "units", "days since 1900-1-1")
    calendar = getattr(time_var, "calendar", "standard")
    dates = num2date(time_var[:], units=units, calendar=calendar)
    years = np.array([d.year for d in np.atleast_1d(dates)], dtype=int)
    months = np.array([d.month for d in np.atleast_1d(dates)], dtype=int)
    return years, months


def _find_coord_name(names: Iterable[str], candidates: Sequence[str]) -> Optional[str]:
    lower = {n.lower(): n for n in names}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


@dataclass
class TargetResult:
    """Monthly result for one target and one variable."""
    target: ExtractionTarget
    var: str
    data: pd.DataFrame                    # columns: Year, Month, value, stn (stn may be NaN)
    cells: List[GridCell]
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_wide(self, column: str = "value", month_names: Sequence[str] = MONTH_NAMES) -> pd.DataFrame:
        """Year + 12 month columns (as used by RingClim)."""
        return monthly_long_to_wide(self.data, column=column, month_names=month_names)


def monthly_long_to_wide(df: pd.DataFrame, column: str = "value",
                         month_names: Sequence[str] = MONTH_NAMES) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Year", *month_names])
    piv = df.pivot_table(index="Year", columns="Month", values=column, aggfunc="mean")
    piv = piv.reindex(columns=range(1, 13))
    piv.columns = list(month_names)
    piv = piv.reset_index()
    piv["Year"] = piv["Year"].astype(int)
    return piv


# netCDF4 / HDF5 must not read files from several threads at the same time (parallel downloads are fine)
NETCDF_LOCK = threading.RLock()


def extract_targets_from_netcdf(*args: Any, **kwargs: Any) -> Dict[str, Tuple[pd.DataFrame, List[GridCell]]]:
    """Thread-safe wrapper (see _extract_targets_from_netcdf)."""
    with NETCDF_LOCK:
        return _extract_targets_from_netcdf(*args, **kwargs)


def _extract_targets_from_netcdf(
    nc_path: str,
    var: str,
    targets: Sequence[ExtractionTarget],
    cancel: CancelCheck = _never_cancel,
) -> Dict[str, Tuple[pd.DataFrame, List[GridCell]]]:
    """Extract all targets from one (uncompressed) NetCDF file.

    Returns {target.name: (DataFrame[Year, Month, value, stn], cells)}.
    Bilinear weights are renormalised over valid (non-missing) cells for each
    month, so a coastal / lake point still gets a value from the land cells.
    """
    from netCDF4 import Dataset  # lazy import

    out: Dict[str, Tuple[pd.DataFrame, List[GridCell]]] = {}
    with Dataset(nc_path, "r") as nc:
        names = list(nc.variables.keys())
        lat_name = _find_coord_name(names, ("lat", "latitude", "y"))
        lon_name = _find_coord_name(names, ("lon", "longitude", "x"))
        if lat_name is None or lon_name is None:
            raise OnlineError("Latitude/longitude variables not found in NetCDF")
        var_name = _find_coord_name(names, (var.lower(),))
        if var_name is None:
            # first 3-D variable that is not 'stn'
            for n in names:
                v = nc.variables[n]
                if len(v.dimensions) == 3 and n.lower() != "stn":
                    var_name = n
                    break
        if var_name is None:
            raise OnlineError(f"Variable '{var}' not found in {os.path.basename(nc_path)}")
        stn_name = _find_coord_name(names, ("stn",))

        lats = np.asarray(nc.variables[lat_name][:], dtype=float)
        lons = np.asarray(nc.variables[lon_name][:], dtype=float)
        lat_desc = lats[0] > lats[-1]
        lats_asc = lats[::-1] if lat_desc else lats
        lons_wrapped = np.array([_wrap_lon(x) for x in lons])
        lon_order = np.argsort(lons_wrapped)
        lons_asc = lons_wrapped[lon_order]

        years, months = _nc_time_to_year_month(nc.variables["time"])
        data_var = nc.variables[var_name]
        data_var.set_auto_maskandscale(True)
        stn_var = nc.variables[stn_name] if stn_name else None

        for t in targets:
            if cancel():
                raise CancelledError("Cancelled during extraction")
            cells = cells_for_target(t, lats_asc, lons_asc)
            if not cells:
                raise OnlineError(f"No grid cells for target '{t.name}' (check coordinates / bbox)")
            # map to file indices
            for c in cells:
                c.i_lat = (len(lats) - 1 - c.i_lat) if lat_desc else c.i_lat
                c.i_lon = int(lon_order[c.i_lon])

            n_t = len(years)
            values = np.full((n_t, len(cells)), np.nan)
            stns = np.full((n_t, len(cells)), np.nan)
            for k, c in enumerate(cells):
                col = data_var[:, c.i_lat, c.i_lon]
                values[:, k] = np.ma.filled(np.ma.asarray(col, dtype=float), np.nan)
                if stn_var is not None:
                    scol = stn_var[:, c.i_lat, c.i_lon]
                    stns[:, k] = np.ma.filled(np.ma.asarray(scol, dtype=float), np.nan)
            # implausible fill values
            values[np.abs(values) > 1e20] = np.nan

            w = np.array([c.weight for c in cells], dtype=float)
            valid = np.isfinite(values)
            wmat = np.where(valid, w[None, :], 0.0)
            wsum = wmat.sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                series = np.where(wsum > 0, np.nansum(np.where(valid, values, 0.0) * wmat, axis=1) / wsum, np.nan)
                stn_series = np.where(
                    wsum > 0,
                    np.nansum(np.where(np.isfinite(stns) & valid, stns, 0.0) * wmat, axis=1) / wsum,
                    np.nan,
                ) if stn_var is not None else np.full(n_t, np.nan)
            for k, c in enumerate(cells):
                c.valid = bool(valid[:, k].any())

            df = pd.DataFrame({"Year": years, "Month": months, "value": series, "stn": stn_series})
            out[t.name] = (df, cells)
    return out


# ---------------------------------------------------------------------------
# High-level CRU job
# ---------------------------------------------------------------------------

@dataclass
class CruRequest:
    version: str
    variables: List[str]
    targets: List[ExtractionTarget]
    start_year: int
    end_year: int
    cache_dir: Optional[str] = None
    keep_downloads: bool = False      # False -> each file is deleted right after extraction
    prefer_decadal: bool = True


@dataclass
class CruResult:
    request: CruRequest
    release: CruRelease
    results: Dict[str, Dict[str, TargetResult]]   # results[target_name][var]
    files: List[CruFile]
    log: List[str] = field(default_factory=list)

    def provenance(self, target_name: str, var: str) -> Dict[str, Any]:
        r = self.results[target_name][var]
        return {
            "source": "CRU TS",
            "version": self.release.version,
            "data_dir": self.release.data_dir,
            "variable": var,
            "variable_label": CRU_VARIABLES.get(var, CruVariable(var, var, "", "Other", "mean")).label,
            "units": CRU_VARIABLES.get(var, CruVariable(var, var, "", "Other", "mean")).units,
            "target": dataclasses.asdict(r.target),
            "cells": [dataclasses.asdict(c) for c in r.cells],
            "files": [f.url for f in self.files if f.var == var],
            "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "citation": CRU_CITATION,
        }


def plan_cru_download(request: CruRequest, timeout: float = 30.0) -> Tuple[CruRelease, List[CruFile]]:
    """Resolve release and files without downloading (for size estimate / confirmation)."""
    release = resolve_cru_release(request.version, timeout=timeout)
    all_files: List[CruFile] = []
    for var in request.variables:
        files = list_cru_files(release, var, timeout=timeout)
        all_files.extend(select_cru_files(files, request.start_year, request.end_year, request.prefer_decadal))
    # exact sizes of the files that will really be downloaded
    for f in all_files:
        size = http_head_size(f.url, timeout=timeout)
        if size:
            f.size_bytes = size
    return release, all_files


def run_cru_request(
    request: CruRequest,
    progress: ProgressCallback = _noop_progress,
    cancel: CancelCheck = _never_cancel,
    planned: Optional[Tuple[CruRelease, List[CruFile]]] = None,
) -> CruResult:
    """Cached wrapper: the same request (version, variables, targets, years) is read from the disk cache,
    also by another running copy of the program."""
    key = [request.version, sorted(request.variables), targets_key(request.targets),
           int(request.start_year), int(request.end_year)]
    hit = series_cache_get("cru", key)
    if isinstance(hit, CruResult):
        progress("cache", 1, 1, "CRU TS: values from the cache (no download)")
        hit.log = list(hit.log) + ["values from the disk cache"]
        return hit
    res = _run_cru_request_uncached(request, progress, cancel, planned)
    if all(set(request.variables) <= set(res.results.get(t.name, {}).keys()) for t in request.targets):
        series_cache_put("cru", key, res)
    return res


def _run_cru_request_uncached(
    request: CruRequest,
    progress: ProgressCallback = _noop_progress,
    cancel: CancelCheck = _never_cancel,
    planned: Optional[Tuple[CruRelease, List[CruFile]]] = None,
) -> CruResult:
    """Download (temporarily), extract and assemble CRU TS series.

    Files are processed one by one: download -> decompress to an ASCII temp
    dir -> extract all targets -> delete the decompressed file -> delete the
    download unless ``keep_downloads`` is set. Disk usage therefore stays at
    roughly one decade of one variable.
    """
    for t in request.targets:
        t.validate()
    names = [t.name for t in request.targets]
    if len(set(names)) != len(names):
        raise ValueError("Target names must be unique")
    if request.start_year > request.end_year:
        raise ValueError("Start year must not be after end year")

    progress("plan", 0, 0, f"Resolving CRU TS {request.version} ...")
    release, files = planned if planned is not None else plan_cru_download(request)
    cache_dir = request.cache_dir or default_cache_dir()
    dl_dir = os.path.join(cache_dir, f"cru_ts_{release.version}")
    os.makedirs(dl_dir, exist_ok=True)

    log: List[str] = []
    parts: Dict[str, Dict[str, List[pd.DataFrame]]] = {n: {} for n in names}
    cells_by: Dict[str, Dict[str, List[GridCell]]] = {n: {} for n in names}

    total_files = len(files)
    for idx, f in enumerate(files, start=1):
        if cancel():
            raise CancelledError("Cancelled")
        progress("file", idx - 1, total_files, f"[{idx}/{total_files}] {f.filename}")
        gz_path = os.path.join(dl_dir, f.filename)
        work_dir: Optional[str] = None
        try:
            for attempt in range(2):
                download_file(f.url, gz_path, progress=progress, cancel=cancel,
                              expected_size=None, stage="download")
                work_dir = _ascii_work_dir()
                nc_path = os.path.join(work_dir, f.filename[:-3] if f.filename.endswith(".gz") else f.filename)
                progress("decompress", 0, 0, f"Decompressing {f.filename} ...")
                try:
                    _decompress_gz(gz_path, nc_path, cancel=cancel)
                    break
                except (OSError, EOFError, gzip.BadGzipFile) as exc:
                    log.append(f"Corrupt download {f.filename}: {exc}; re-downloading")
                    shutil.rmtree(work_dir, ignore_errors=True)
                    work_dir = None
                    try:
                        os.remove(gz_path)
                    except OSError:
                        pass
                    if attempt == 1:
                        raise OnlineError(f"Downloaded file is corrupt: {f.filename}") from exc
            progress("extract", 0, 0, f"Extracting {len(request.targets)} target(s) from {f.filename} ...")
            assert work_dir is not None
            extracted = extract_targets_from_netcdf(nc_path, f.var, request.targets, cancel=cancel)
            for name, (df, cells) in extracted.items():
                df = df[(df["Year"] >= request.start_year) & (df["Year"] <= request.end_year)]
                parts[name].setdefault(f.var, []).append(df)
                cells_by[name][f.var] = cells
            log.append(f"OK {f.filename}")
        finally:
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            if not request.keep_downloads and os.path.exists(gz_path):
                try:
                    os.remove(gz_path)
                    log.append(f"Deleted temporary file {f.filename}")
                except OSError as exc:
                    log.append(f"Could not delete {f.filename}: {exc}")
        progress("file", idx, total_files, f"[{idx}/{total_files}] done")

    if not request.keep_downloads:
        try:
            if os.path.isdir(dl_dir) and not os.listdir(dl_dir):
                os.rmdir(dl_dir)
        except OSError:
            pass

    results: Dict[str, Dict[str, TargetResult]] = {}
    for t in request.targets:
        results[t.name] = {}
        for var, dfs in parts[t.name].items():
            df = pd.concat(dfs, ignore_index=True)
            df = df.drop_duplicates(subset=["Year", "Month"], keep="last").sort_values(["Year", "Month"])
            df = df.reset_index(drop=True)
            cells = cells_by[t.name].get(var, [])
            stn_valid = df["stn"].dropna()
            meta = {
                "n_months": int(len(df)),
                "n_missing": int(df["value"].isna().sum()),
                "first_year": int(df["Year"].min()) if len(df) else None,
                "last_year": int(df["Year"].max()) if len(df) else None,
                "mean_stn": float(stn_valid.mean()) if len(stn_valid) else None,
                "share_months_stn0": float((stn_valid <= 0).mean()) if len(stn_valid) else None,
                "invalid_cells": int(sum(1 for c in cells if c.valid is False)),
            }
            results[t.name][var] = TargetResult(t, var, df, cells, meta)
    progress("done", total_files, total_files, "CRU TS extraction finished")
    return CruResult(request, release, results, files, log)


def station_support_by_year(result: TargetResult) -> pd.DataFrame:
    """Annual mean CRU station count around the target (0 = climatology only)."""
    df = result.data.groupby("Year", as_index=False)["stn"].mean()
    return df.rename(columns={"stn": "mean_stn"})


def station_support_warning(result: TargetResult, threshold: float = 1.0) -> Optional[str]:
    """Human-readable warning when the series relies on few/no stations."""
    s = station_support_by_year(result).dropna()
    if s.empty:
        return None
    weak = s[s["mean_stn"] < threshold]
    if weak.empty:
        return None
    yrs = weak["Year"].astype(int).tolist()
    ranges: List[str] = []
    start = prev = yrs[0]
    for y in yrs[1:]:
        if y != prev + 1:
            ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = y
        prev = y
    ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
    return (f"{result.var}: weak station support (mean stn < {threshold:g}) in "
            f"{len(yrs)} year(s): {', '.join(ranges)}. In these years CRU TS relaxes "
            f"towards climatology and correlations may be underestimated.")


# ---------------------------------------------------------------------------
# ITRDB search / download
# ---------------------------------------------------------------------------

@dataclass
class ItrdbFile:
    url: str
    description: str
    kind: str            # "rwl", "rwl-noaa", "crn", "crn-noaa", "stats", "other"
    variant: str = ""    # for crn: "standard" | "residual" | "arstan"
    parameter: str = ""  # e.g. "Total ring width", "Earlywood width", "Maximum density"

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


@dataclass
class ItrdbSite:
    study_id: str
    study_name: str
    site_name: str
    site_code: str
    lat: Optional[float]
    lon: Optional[float]
    elevation_m: Optional[float]
    species: str
    first_year: Optional[int]
    last_year: Optional[int]
    investigators: str
    doi: str
    location: str
    files: List[ItrdbFile] = field(default_factory=list)

    @property
    def study_url(self) -> str:
        return NOAA_STUDY_PAGE.format(id=self.study_id)

    def files_of_kind(self, *kinds: str) -> List[ItrdbFile]:
        return [f for f in self.files if f.kind in kinds]

    def as_row(self) -> Dict[str, Any]:
        return {
            "site_code": self.site_code, "site_name": self.site_name, "species": self.species,
            "lat": self.lat, "lon": self.lon, "elevation_m": self.elevation_m,
            "first_year": self.first_year, "last_year": self.last_year,
            "investigators": self.investigators, "doi": self.doi, "study_id": self.study_id,
            "n_rwl": len(self.files_of_kind("rwl")), "n_crn": len(self.files_of_kind("crn")),
            "location": self.location, "study_url": self.study_url,
        }


def _to_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> Optional[int]:
    v = _to_float(x)
    return int(v) if v is not None else None


def classify_itrdb_file(url: str, description: str = "", site_code: str = "") -> ItrdbFile:
    name = url.rsplit("/", 1)[-1]
    lower = name.lower()
    desc = description or ""
    if "/correlation-stats/" in url.lower():
        return ItrdbFile(url, desc, "stats")
    if lower.endswith("-rwl-noaa.txt") or lower.endswith("-noaa.rwl"):
        kind = "rwl-noaa"
    elif lower.endswith("-crn-noaa.txt") or lower.endswith("-noaa.crn"):
        kind = "crn-noaa"
    elif lower.endswith(".rwl"):
        kind = "rwl"
    elif lower.endswith(".crn"):
        kind = "crn"
    else:
        return ItrdbFile(url, desc, "other")

    stem = re.sub(r"(-rwl-noaa\.txt|-crn-noaa\.txt|-noaa\.rwl|-noaa\.crn|\.rwl|\.crn)$", "", lower)
    code = (site_code or "").lower()
    suffix = stem[len(code):] if code and stem.startswith(code) else re.sub(r"^[a-z]+\d+", "", stem)

    variant = ""
    if kind.startswith("crn"):
        if suffix.endswith("r"):
            variant, suffix = "residual", suffix[:-1]
        elif suffix.endswith("a"):
            variant, suffix = "arstan", suffix[:-1]
        else:
            variant = "standard"
    parameter = ITRDB_PARAM_SUFFIX.get(suffix, f"Parameter '{suffix}'" if suffix else ITRDB_PARAM_SUFFIX[""])
    return ItrdbFile(url, desc, kind, variant, parameter)


def _site_code_from(study: Dict[str, Any], files: List[str]) -> str:
    name = str(study.get("studyName") or "")
    m = re.search(r"ITRDB\s+([A-Za-z]{2,6}\d{2,4})", name)
    if m:
        return m.group(1).lower()
    code = study.get("studyCode")
    if code:
        return str(code).lower()
    for u in files:
        m = re.match(r"([a-z]{2,6}\d{2,4})", u.rsplit("/", 1)[-1].lower())
        if m:
            return m.group(1)
    return ""


def _species_text(paleo: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    sp = paleo.get("species")
    if isinstance(sp, list):
        for s in sp:
            if isinstance(s, dict):
                code = s.get("speciesCode") or ""
                sci = s.get("scientificName") or ""
                common = s.get("commonName")
                if isinstance(common, list):
                    common = ", ".join(str(c) for c in common[:1])
                text = " ".join(x for x in (str(code).upper(), str(sci)) if x).strip()
                if not text and common:
                    text = str(common)
                if text:
                    out.append(text)
            elif s:
                out.append(str(s))
    elif isinstance(sp, str) and sp:
        out.append(sp)
    return out


def parse_noaa_search_json(data: Any) -> List[ItrdbSite]:
    """Convert NOAA paleo-search JSON into ItrdbSite objects."""
    studies = data.get("study", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    sites: List[ItrdbSite] = []
    for st in studies:
        if not isinstance(st, dict):
            continue
        study_id = str(st.get("NOAAStudyId") or st.get("xmlId") or "")
        investigators = st.get("investigators") or ""
        if isinstance(investigators, list):
            investigators = "; ".join(str(x) for x in investigators)
        doi = str(st.get("doi") or "")
        for site in st.get("site") or []:
            if not isinstance(site, dict):
                continue
            geo = site.get("geo") or {}
            coords = (geo.get("geometry") or {}).get("coordinates") or []
            props = geo.get("properties") or {}
            lat = lon = None
            if isinstance(coords, list) and len(coords) >= 2 and not isinstance(coords[0], list):
                # NOAA gives [lat, lon] (not GeoJSON order)
                lat, lon = _to_float(coords[0]), _to_float(coords[1])
            if lat is None:
                n, s = _to_float(props.get("northernmostLatitude")), _to_float(props.get("southernmostLatitude"))
                if n is not None and s is not None:
                    lat = (n + s) / 2.0
            if lon is None:
                e, w = _to_float(props.get("easternmostLongitude")), _to_float(props.get("westernmostLongitude"))
                if e is not None and w is not None:
                    lon = (e + w) / 2.0
            elev = _to_float(props.get("maxElevationMeters")) or _to_float(props.get("minElevationMeters"))

            species: List[str] = []
            urls: List[Tuple[str, str]] = []
            first_years: List[int] = []
            last_years: List[int] = []
            for paleo in site.get("paleoData") or []:
                if not isinstance(paleo, dict):
                    continue
                species.extend(_species_text(paleo))
                fy, ly = _to_int(paleo.get("earliestYear")), _to_int(paleo.get("mostRecentYear"))
                if fy is not None:
                    first_years.append(fy)
                if ly is not None:
                    last_years.append(ly)
                for df in paleo.get("dataFile") or []:
                    if isinstance(df, dict) and df.get("fileUrl"):
                        urls.append((str(df["fileUrl"]), str(df.get("urlDescription") or df.get("linkText") or "")))
            if not first_years:
                v = _to_int(st.get("earliestYearCE"))
                if v is not None:
                    first_years.append(v)
            if not last_years:
                v = _to_int(st.get("mostRecentYearCE"))
                if v is not None:
                    last_years.append(v)
            code = _site_code_from(st, [u for u, _ in urls])
            seen: set = set()
            files: List[ItrdbFile] = []
            for u, d in urls:
                if u in seen:
                    continue
                seen.add(u)
                files.append(classify_itrdb_file(u, d, code))
            sites.append(ItrdbSite(
                study_id=study_id,
                study_name=str(st.get("studyName") or ""),
                site_name=str(site.get("siteName") or ""),
                site_code=code.upper(),
                lat=lat, lon=lon, elevation_m=elev,
                species="; ".join(dict.fromkeys(species)),
                first_year=min(first_years) if first_years else None,
                last_year=max(last_years) if last_years else None,
                investigators=str(investigators),
                doi=doi,
                location=str(site.get("locationName") or ""),
                files=files,
            ))
    return sites


def build_itrdb_query(
    min_lat: Optional[float] = None, max_lat: Optional[float] = None,
    min_lon: Optional[float] = None, max_lon: Optional[float] = None,
    species: Optional[str] = None, earliest_year: Optional[int] = None,
    latest_year: Optional[int] = None, text: Optional[str] = None,
    investigators: Optional[str] = None, location: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, str]:
    q: Dict[str, str] = {"dataPublisher": "NOAA", "dataTypeId": NOAA_TREE_RING_TYPE_ID}
    if min_lat is not None:
        q["minLat"] = f"{min_lat:g}"
    if max_lat is not None:
        q["maxLat"] = f"{max_lat:g}"
    if min_lon is not None:
        q["minLon"] = f"{min_lon:g}"
    if max_lon is not None:
        q["maxLon"] = f"{max_lon:g}"
    if species:
        q["species"] = species.strip().upper()
    if earliest_year is not None or latest_year is not None:
        q["timeFormat"] = "CE"
        q["timeMethod"] = "overAny"
        if earliest_year is not None:
            q["earliestYear"] = str(int(earliest_year))
        if latest_year is not None:
            q["latestYear"] = str(int(latest_year))
    if text:
        q["searchText"] = text.strip()
    if investigators:
        q["investigators"] = investigators.strip()
    if location:
        q["locations"] = location.strip()
    if limit:
        q["limit"] = str(int(limit))
    return q


def _itrdb_fetch(params: Dict[str, str], timeout: float) -> Any:
    """GET the NOAA search with fallback hosts; raises OnlineError."""
    last_exc: Optional[BaseException] = None
    for base in NOAA_SEARCH_URLS:
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            return http_get_json(url, timeout=timeout)
        except OnlineError as exc:
            last_exc = exc
            continue
    raise OnlineError(f"ITRDB search failed: {last_exc}")


def _integer_bounds(params: Dict[str, str]) -> Dict[str, str]:
    """The NOAA API expects whole degrees; widen the box outward (exact filtering is done locally)."""
    out = dict(params)
    for key, fn in (("minLat", math.floor), ("maxLat", math.ceil), ("minLon", math.floor), ("maxLon", math.ceil)):
        if key in out:
            try:
                v = int(fn(float(out[key])))
            except ValueError:
                continue
            lim = 90 if "Lat" in key else 180
            out[key] = str(max(-lim, min(lim, v)))
    return out


def search_itrdb(timeout: float = 90.0, raw_json_path: Optional[str] = None,
                 page_size: int = 1000, max_pages: int = 8, **query: Any) -> List[ItrdbSite]:
    """Search ITRDB tree-ring studies. Keyword args as in build_itrdb_query.

    Coordinates are sent as whole degrees, results are paged, and when the server rejects
    a species or text filter the search is repeated without it and the filter is applied
    locally to the species and names of the sites.
    """
    params = _integer_bounds(build_itrdb_query(**query))
    attempts = [params]
    local_species: List[str] = []
    local_text = ""
    if "species" in params:
        p2 = {k: v for k, v in params.items() if k != "species"}
        attempts.append(p2)
    if "searchText" in params:
        attempts.append({k: v for k, v in attempts[-1].items() if k != "searchText"})
    data_all: List[Any] = []
    last_exc: Optional[BaseException] = None
    used: Optional[Dict[str, str]] = None
    for att in attempts:
        try:
            studies: List[Any] = []
            for page in range(max_pages):
                pp = dict(att)
                pp["limit"] = str(int(page_size))
                if page:
                    pp["skip"] = str(page * int(page_size))
                data = _itrdb_fetch(pp, timeout)
                chunk = data.get("study", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                studies.extend(chunk)
                if len(chunk) < int(page_size):
                    break
            data_all = studies
            used = att
            break
        except OnlineError as exc:
            last_exc = exc
            continue
    if used is None:
        raise OnlineError(f"ITRDB search failed: {last_exc}")
    if "species" in params and "species" not in used:
        local_species = [c for c in re.split(r"[|,;\s]+", params["species"].upper()) if c]
    if "searchText" in params and "searchText" not in used:
        local_text = params["searchText"]
    if raw_json_path:
        with open(raw_json_path, "w", encoding="utf-8") as fh:
            json.dump({"study": data_all}, fh, ensure_ascii=False, indent=1)
    sites = parse_noaa_search_json({"study": data_all})
    # keep only ITRDB-style entries that actually have tree-ring files
    sites = [s for s in sites if s.files_of_kind("rwl", "crn", "rwl-noaa", "crn-noaa")]
    if query.get("min_lat") is not None and query.get("max_lat") is not None:
        sites = [s for s in sites if s.lat is None or query["min_lat"] - 0.01 <= s.lat <= query["max_lat"] + 0.01]
    if query.get("min_lon") is not None and query.get("max_lon") is not None:
        sites = [s for s in sites if s.lon is None or query["min_lon"] - 0.01 <= s.lon <= query["max_lon"] + 0.01]
    if local_species:
        sites = [s for s in sites if any(c in s.species.upper() for c in local_species)]
    if local_text:
        words = [w.lower() for w in re.split(r"\s+(?:AND\s+)?", local_text) if w and w.upper() != "AND"]
        sites = [s for s in sites if all(w in _site_haystack(s) for w in words)]
    # one entry per site and study
    seen: set = set()
    uniq: List[ItrdbSite] = []
    for s in sites:
        key = (s.study_id, s.site_code, s.site_name)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    return uniq


def _site_haystack(s: "ItrdbSite") -> str:
    return " ".join(str(x) for x in (s.site_code, s.site_name, s.study_name, s.species, s.investigators,
                                     s.location)).lower()


# ---------------------------------------------------------------------------
# Free-text ITRDB search ("Juniper Turkey", "Pinus sylvestris Finland", "PCAB Alps")
# ---------------------------------------------------------------------------

GENUS_COMMON_NAMES: Dict[str, str] = {
    "juniper": "Juniperus", "junipers": "Juniperus", "pine": "Pinus", "pines": "Pinus",
    "spruce": "Picea", "spruces": "Picea", "fir": "Abies", "firs": "Abies", "larch": "Larix",
    "larches": "Larix", "tamarack": "Larix", "oak": "Quercus", "oaks": "Quercus", "beech": "Fagus",
    "cedar": "Cedrus", "cedars": "Cedrus", "hemlock": "Tsuga", "douglas-fir": "Pseudotsuga",
    "douglas": "Pseudotsuga", "cypress": "Cupressus", "baldcypress": "Taxodium", "birch": "Betula",
    "poplar": "Populus", "aspen": "Populus", "maple": "Acer", "ash": "Fraxinus", "elm": "Ulmus",
    "chestnut": "Castanea", "teak": "Tectona", "redwood": "Sequoia", "sequoia": "Sequoiadendron",
    "walnut": "Juglans", "willow": "Salix", "yew": "Taxus", "alder": "Alnus", "hornbeam": "Carpinus",
    "thuja": "Thuja", "arborvitae": "Thuja", "bristlecone": "Pinus", "hickory": "Carya",
    "tupelo": "Nyssa", "linden": "Tilia", "lime": "Tilia", "cottonwood": "Populus", "araucaria": "Araucaria",
    "fitzroya": "Fitzroya", "nothofagus": "Nothofagus", "southern-beech": "Nothofagus",
}

COUNTRY_ALIASES: Dict[str, str] = {
    "usa": "United States of America", "us": "United States of America", "u.s.": "United States of America",
    "united states": "United States of America", "america": "United States of America",
    "uk": "United Kingdom", "britain": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "turkiye": "Turkey", "türkiye": "Turkey", "czech republic": "Czechia", "russian federation": "Russia",
    "korea": "South Korea", "persia": "Iran", "macedonia": "North Macedonia", "bosnia": "Bosnia and Herz.",
    "ivory coast": "Côte d'Ivoire", "holland": "Netherlands", "burma": "Myanmar", "kirgizia": "Kyrgyzstan",
    "kyrgyz": "Kyrgyzstan", "tadjikistan": "Tajikistan", "swaziland": "eSwatini",
}

# Regions that are not countries: (min_lat, max_lat, min_lon, max_lon)
REGION_BOXES: Dict[str, Tuple[float, float, float, float]] = {
    "europe": (34.0, 72.0, -25.0, 45.0), "asia": (-11.0, 78.0, 25.0, 180.0), "africa": (-35.0, 38.0, -18.0, 52.0),
    "north america": (7.0, 84.0, -170.0, -50.0), "south america": (-56.0, 13.0, -82.0, -34.0),
    "oceania": (-48.0, 0.0, 110.0, 180.0), "siberia": (45.0, 78.0, 60.0, 180.0),
    "alps": (43.5, 48.5, 5.0, 16.5), "tibet": (27.0, 40.0, 78.0, 104.0), "tibetan plateau": (27.0, 40.0, 73.0, 104.0),
    "himalaya": (26.0, 37.0, 70.0, 98.0), "himalayas": (26.0, 37.0, 70.0, 98.0),
    "tien shan": (39.0, 45.5, 67.0, 96.0), "tianshan": (39.0, 45.5, 67.0, 96.0), "pamir": (36.5, 40.0, 70.0, 76.0),
    "central asia": (35.0, 56.0, 46.0, 88.0), "caucasus": (39.0, 45.0, 37.0, 50.0),
    "carpathians": (44.0, 50.0, 17.0, 27.5), "balkans": (39.0, 47.0, 13.0, 30.0), "pyrenees": (42.0, 43.5, -2.0, 3.5),
    "mediterranean": (30.0, 46.0, -10.0, 37.0), "scandinavia": (54.5, 71.5, 4.0, 32.0),
    "fennoscandia": (54.5, 71.5, 4.0, 42.0), "patagonia": (-56.0, -37.0, -76.0, -62.0),
    "rocky mountains": (31.0, 60.0, -125.0, -104.0), "rockies": (31.0, 60.0, -125.0, -104.0),
    "alaska": (51.0, 72.0, -170.0, -129.0), "yukon": (60.0, 70.0, -141.0, -123.0),
    "california": (32.5, 42.0, -124.5, -114.0), "colorado": (37.0, 41.0, -109.1, -102.0),
    "arizona": (31.3, 37.0, -114.8, -109.0), "new mexico": (31.3, 37.0, -109.1, -103.0),
    "utah": (37.0, 42.0, -114.1, -109.0), "nevada": (35.0, 42.0, -120.0, -114.0),
    "oregon": (42.0, 46.3, -124.6, -116.5), "washington": (45.5, 49.0, -124.8, -116.9),
    "montana": (44.4, 49.0, -116.1, -104.0), "idaho": (42.0, 49.0, -117.3, -111.0),
    "wyoming": (41.0, 45.0, -111.1, -104.0), "texas": (25.8, 36.5, -106.7, -93.5),
    "quebec": (45.0, 62.6, -79.8, -57.1), "ontario": (41.7, 56.9, -95.2, -74.3),
    "british columbia": (48.3, 60.0, -139.1, -114.0), "yakutia": (55.5, 76.8, 105.0, 163.0),
    "mongolian plateau": (41.0, 52.0, 87.0, 120.0), "karakoram": (34.0, 37.5, 74.0, 78.5),
}


@dataclass
class ItrdbTextQuery:
    genera: List[str] = field(default_factory=list)
    species_codes: List[str] = field(default_factory=list)
    countries: List[Dict[str, Any]] = field(default_factory=list)
    regions: List[str] = field(default_factory=list)
    words: List[str] = field(default_factory=list)

    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        boxes = [c["bounds"] for c in self.countries] + [REGION_BOXES[r] for r in self.regions]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes))

    def describe(self) -> str:
        parts = []
        if self.genera:
            parts.append("genus " + ", ".join(self.genera))
        if self.species_codes:
            parts.append("species " + ", ".join(self.species_codes))
        if self.countries:
            parts.append("in " + ", ".join(c["name"] for c in self.countries))
        if self.regions:
            parts.append("region " + ", ".join(r.title() for r in self.regions))
        if self.words:
            parts.append("text '" + " ".join(self.words) + "'")
        return "; ".join(parts) or "all tree-ring sites"


def parse_itrdb_text(text: str, species: str = "") -> ItrdbTextQuery:
    """Split free text into genera, species codes, countries/regions and remaining words."""
    q = ItrdbTextQuery()
    raw = f"{species or ''} {text or ''}".strip()
    if not raw:
        return q
    low = " " + re.sub(r"\s+", " ", raw.lower()) + " "
    # multi-word regions and countries first (longest first)
    try:
        countries = load_countries()
    except Exception:
        countries = []
    names: Dict[str, Dict[str, Any]] = {}
    for c in countries:
        for n in (c.get("name"), c.get("name_long")):
            if n:
                names[n.lower()] = c
    for alias, target in COUNTRY_ALIASES.items():
        hit = next((c for c in countries if c.get("name") == target), None)
        if hit is not None:
            names.setdefault(alias, hit)
    for key in sorted(list(REGION_BOXES) + list(names), key=len, reverse=True):
        pat = " " + key + " "
        if pat in low:
            if key in REGION_BOXES:
                if key not in q.regions:
                    q.regions.append(key)
            else:
                c = names[key]
                if all(c is not x for x in q.countries):
                    q.countries.append(c)
            low = low.replace(pat, " ")
    species_tokens = {t.strip(",;.|").upper() for t in (species or "").split()}
    caps = set(re.findall(r"\b[A-Z]{4}\b", text or ""))
    for tok in low.split():
        t = tok.strip(",;.|")
        if not t:
            continue
        if re.fullmatch(r"[a-z]{4}", t) and (t.upper() in caps or t.upper() in species_tokens):
            if t.upper() not in q.species_codes:
                q.species_codes.append(t.upper())
        elif t in GENUS_COMMON_NAMES:
            g = GENUS_COMMON_NAMES[t]
            if g not in q.genera:
                q.genera.append(g)
        elif t.capitalize() in _KNOWN_GENERA:
            g = t.capitalize()
            if g not in q.genera:
                q.genera.append(g)
        else:
            q.words.append(t)
    return q


_KNOWN_GENERA = set(GENUS_COMMON_NAMES.values()) | {
    "Austrocedrus", "Chamaecyparis", "Calocedrus", "Liriodendron", "Pilgerodendron", "Callitris",
    "Athrotaxis", "Lagarostrobos", "Phyllocladus", "Libocedrus", "Platycladus", "Sabina", "Cedrela",
    "Polylepis", "Prosopis", "Pterocarpus", "Toona", "Fokienia", "Cunninghamia", "Cryptomeria", "Metasequoia"}


def smart_search_itrdb(text: str = "", species: str = "",
                       area: Optional[Tuple[float, float, float, float]] = None,
                       earliest_year: Optional[int] = None, latest_year: Optional[int] = None,
                       timeout: float = 120.0) -> Tuple[List[ItrdbSite], str]:
    """ITRDB search from free text.

    * genus or common tree names ("juniper", "Pinus") filter the species of the sites;
    * country and region names ("Turkey", "Alps", "Tien Shan") set the search area
      (country polygons are used to keep only sites inside the country) and replace ``area``;
    * four-letter codes in capitals ("JUTU", "PCAB") are ITRDB species codes;
    * everything else is sent to the NOAA full-text search (site, investigator, study).
    Without area, country or region the search is world-wide.
    Returns (sites, description of the interpreted query).
    """
    q = parse_itrdb_text(text, species)
    box = q.bbox() or area
    query: Dict[str, Any] = dict(earliest_year=earliest_year, latest_year=latest_year)
    if box is not None:
        query.update(min_lat=box[0], max_lat=box[1], min_lon=box[2], max_lon=box[3])
    if q.species_codes:
        query["species"] = "|".join(q.species_codes)
    server_words = list(q.words)
    if box is None and q.genera and not q.species_codes:
        server_words.append(" OR ".join(q.genera) if len(q.genera) > 1 else q.genera[0])
    if server_words:
        query["text"] = " AND ".join(server_words)
    sites = search_itrdb(timeout=timeout, **query)
    if q.genera:
        gl = [g.lower() for g in q.genera]
        sites = [s for s in sites if any(g in s.species.lower() for g in gl)]
    if q.countries and not q.regions:
        def inside(s: ItrdbSite) -> bool:
            if s.lat is None or s.lon is None:
                return False
            for c in q.countries:
                la0, la1, lo0, lo1 = c["bounds"]
                if la0 - 0.3 <= s.lat <= la1 + 0.3 and lo0 - 0.3 <= s.lon <= lo1 + 0.3:
                    if any(_point_in_polygon(p, _wrap_lon(s.lon), s.lat) for p in c["polygons"]):
                        return True
                    fc = find_country(s.lat, s.lon)
                    if fc is c:
                        return True
            return False
        sites = [s for s in sites if inside(s)]
    desc = q.describe()
    if box is not None and q.bbox() is None:
        desc += "; inside the map area"
    elif box is None:
        desc += "; world-wide"
    return sites, desc


def bbox_around(lat: float, lon: float, radius_km: float) -> Tuple[float, float, float, float]:
    dlat = radius_km / 111.0
    dlon = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-6)
    return (max(-90.0, lat - dlat), min(90.0, lat + dlat), max(-180.0, lon - dlon), min(180.0, lon + dlon))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class ItrdbBatchCriteria:
    """Criteria for picking files from many ITRDB sites at once."""
    kinds: Tuple[str, ...] = ("crn",)                       # "crn" and/or "rwl"
    parameters: Tuple[str, ...] = ("Total ring width",)     # empty = any parameter
    variants: Tuple[str, ...] = ("standard",)               # crn only; empty = any
    min_span_years: int = 0                                 # last_year - first_year + 1
    min_last_year: Optional[int] = None                     # e.g. 1950 for overlap with CRU
    max_first_year: Optional[int] = None                    # e.g. 1800 for long records
    max_distance_km: Optional[float] = None
    center: Optional[Tuple[float, float]] = None
    one_per_site: bool = True                                # one file per kind/parameter/variant per site


def select_itrdb_files(sites: Sequence[ItrdbSite], c: ItrdbBatchCriteria) -> List[Tuple[ItrdbSite, ItrdbFile]]:
    """Files matching the criteria across all given sites (ordered by distance if center is set)."""
    picked: List[Tuple[float, ItrdbSite, ItrdbFile]] = []
    for s in sites:
        if c.min_span_years and s.first_year is not None and s.last_year is not None:
            if s.last_year - s.first_year + 1 < c.min_span_years:
                continue
        if c.min_last_year is not None and (s.last_year is None or s.last_year < c.min_last_year):
            continue
        if c.max_first_year is not None and (s.first_year is None or s.first_year > c.max_first_year):
            continue
        dist = 0.0
        if c.center is not None and s.lat is not None and s.lon is not None:
            dist = haversine_km(c.center[0], c.center[1], s.lat, s.lon)
            if c.max_distance_km is not None and dist > c.max_distance_km:
                continue
        seen_kind: set = set()
        files = sorted(s.files, key=lambda f: (f.parameter != "Total ring width", f.variant != "standard", f.filename))
        for f in files:
            if f.kind not in c.kinds:
                continue
            if c.parameters and f.parameter not in c.parameters:
                continue
            if f.kind == "crn" and c.variants and f.variant not in c.variants:
                continue
            if c.one_per_site:
                key = (f.kind, f.parameter, f.variant)  # one file per kind/parameter/variant per site
                if key in seen_kind:
                    continue
                seen_kind.add(key)
            picked.append((dist, s, f))
    picked.sort(key=lambda t: (t[0], t[1].site_code, t[2].filename))
    return [(s, f) for _, s, f in picked]


def download_itrdb_files(
    files: Sequence[ItrdbFile],
    dest_dir: str,
    progress: ProgressCallback = _noop_progress,
    cancel: CancelCheck = _never_cancel,
    overwrite: bool = False,
) -> List[str]:
    os.makedirs(dest_dir, exist_ok=True)
    paths: List[str] = []
    for k, f in enumerate(files, start=1):
        if cancel():
            raise CancelledError("Cancelled")
        dest = os.path.join(dest_dir, f.filename)
        if overwrite and os.path.exists(dest):
            os.remove(dest)
        progress("file", k - 1, len(files), f"[{k}/{len(files)}] {f.filename}")
        paths.append(download_file(f.url, dest, progress=progress, cancel=cancel, stage="download"))
    progress("done", len(files), len(files), "ITRDB download finished")
    return paths


# ---------------------------------------------------------------------------
# Tucson parsers
# ---------------------------------------------------------------------------

def _read_text_lines(path: str) -> List[str]:
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _try_crn_line(line: str, id_width: int) -> Optional[Tuple[str, int, List[Tuple[Optional[float], Optional[int]]]]]:
    if len(line) < id_width + 4 + 7:
        return None
    sid = line[:id_width].strip()
    ystr = line[id_width:id_width + 4].strip()
    if not re.fullmatch(r"-?\d{1,4}", ystr):
        return None
    full = line[id_width + 4:].rstrip()
    # 10 fields of 7 chars (4 index + 3 sample depth); anything after column 70
    # is a tag such as "STD"/"RES"/"ARS" in some ITRDB files and is ignored.
    body = full[:70]
    tail = full[70:].strip()
    if tail and not re.fullmatch(r"[A-Za-z_\-]{1,8}", tail):
        return None
    if not body.strip():
        return None
    vals: List[Tuple[Optional[float], Optional[int]]] = []
    for pos in range(0, len(body), 7):
        chunk = body[pos:pos + 7]
        if not chunk.strip():
            vals.append((None, None))
            continue
        idx_s, n_s = chunk[:4].strip(), chunk[4:7].strip()
        if not re.fullmatch(r"-?\d+", idx_s or "x"):
            return None
        if n_s and not re.fullmatch(r"-?\d+", n_s):
            return None
        iv = int(idx_s)
        nv = int(n_s) if n_s else None
        vals.append((None if iv == 9990 or iv < 0 else iv / 1000.0, nv))
    return sid, int(ystr), vals


def read_tucson_crn(path: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Read a Tucson-format chronology (.crn).

    Returns (DataFrame[Year, <name>, <name>_n], meta). Index values are
    divided by 1000; 9990 is missing. If the file holds several chronologies
    (different IDs), each gets its own pair of columns.
    """
    lines = [ln.rstrip("\n") for ln in _read_text_lines(path)]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        raise ValueError("Empty chronology file")

    best_width, best_hits = 6, -1
    for width in (6, 8, 7, 5):
        hits = sum(1 for ln in lines if _try_crn_line(ln, width) is not None)
        if hits > best_hits:
            best_width, best_hits = width, hits
    if best_hits <= 0:
        raise ValueError("No Tucson chronology data lines found")

    header: List[str] = []
    chrons: Dict[str, Dict[int, Tuple[Optional[float], Optional[int]]]] = {}
    order: List[str] = []
    for ln in lines:
        parsed = _try_crn_line(ln, best_width)
        if parsed is None:
            if not chrons:
                header.append(ln)
            continue
        sid, decade, vals = parsed
        key = sid or "chron"
        if key not in chrons:
            chrons[key] = {}
            order.append(key)
        # Values sit in decade slots: the line "1626" holds 1620..1629 with the
        # slots before the first year padded by 9990.
        base = decade - (decade % 10)
        for k, (iv, nv) in enumerate(vals):
            y = base + k
            if iv is None and (nv is None or nv == 0):
                continue
            chrons[key][y] = (iv, nv)

    frames = []
    for key in order:
        d = chrons[key]
        if not d:
            continue
        yrs = sorted(d)
        col = key if len(order) > 1 else "Index"
        frames.append(pd.DataFrame({
            "Year": yrs,
            col: [d[y][0] for y in yrs],
            f"{col}_n": [d[y][1] for y in yrs],
        }).set_index("Year"))
    if not frames:
        raise ValueError("Chronology contains no values")
    df = pd.concat(frames, axis=1).sort_index()
    df.index = df.index.astype(int)
    # trim trailing / leading all-missing years
    value_cols = [c for c in df.columns if not str(c).endswith("_n")]
    valid = df[value_cols].notna().any(axis=1)
    if valid.any():
        df = df.loc[valid[valid].index.min(): valid[valid].index.max()]
    df = df.reset_index().rename(columns={"index": "Year"})

    meta: Dict[str, Any] = {"header": header[:3], "ids": order, "id_width": best_width,
                            "source_path": os.path.abspath(path)}
    if header:
        h1 = header[0]
        meta["site_id"] = h1[:best_width].strip()
        rest = h1[9:].rstrip() if len(h1) > 9 else ""
        rest = re.sub(r"\s+(STD|RES|ARS|ARSTAN)\s*$", "", rest)
        rest = re.sub(r"(\s+-)+\s*$", "", rest)
        sp = re.search(r"\b([A-Z]{4})\s*$", rest)
        meta["species_code"] = sp.group(1) if sp else ""
        meta["site_name"] = (rest[:sp.start()] if sp else rest).strip()
    if len(header) > 1:
        meta["header_line2"] = header[1]
    return df, meta


def _rwl_line_fixed(line: str) -> Optional[Tuple[str, int, List[int]]]:
    if len(line) < 13:
        return None
    sid = line[:8].strip()
    ystr = line[8:12].strip()
    if not sid or not re.fullmatch(r"-?\d{1,4}", ystr):
        return None
    vals: List[int] = []
    # 10 values x 6 characters; anything after column 72 (e.g. "GAP" flags in some old
    # ITRDB files) is not data
    body = line[12:72].rstrip()
    for pos in range(0, len(body), 6):
        chunk = body[pos:pos + 6].strip()
        if not chunk:
            continue
        if not re.fullmatch(r"-?\d+", chunk):
            return None
        vals.append(int(chunk))
    if not vals:
        return None
    return sid, int(ystr), vals


def _rwl_line_split(line: str) -> Optional[Tuple[str, int, List[int]]]:
    toks = line.split()
    while len(toks) > 3 and not re.fullmatch(r"-?\d+", toks[-1]):  # trailing flags such as "GAP"
        toks.pop()
    if len(toks) < 3:
        return None
    # ID may contain the year glued at its end when IDs are long
    if re.fullmatch(r"-?\d{1,4}", toks[1]) and all(re.fullmatch(r"-?\d+", t) for t in toks[2:]):
        return toks[0], int(toks[1]), [int(t) for t in toks[2:]]
    return None


def read_tucson_rwl(path: str, zero_as_nan: bool = False) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Read a Tucson ring-width file and return widths in millimetres.

    Precision is detected per series from its end marker:
    999 -> values in 0.01 mm, -9999 -> values in 0.001 mm.
    Zeros (missing rings) are kept as 0.0 unless zero_as_nan is True.
    Returns (DataFrame indexed by year, meta with per-series precision).
    """
    lines = [ln.rstrip("\n") for ln in _read_text_lines(path) if ln.strip()]
    raw: Dict[str, Dict[int, int]] = {}
    precision: Dict[str, float] = {}
    order: List[str] = []
    n_fixed = sum(1 for ln in lines if _rwl_line_fixed(ln) is not None)
    n_split = sum(1 for ln in lines if _rwl_line_split(ln) is not None)
    parser = _rwl_line_fixed if n_fixed >= n_split else _rwl_line_split
    for ln in lines:
        parsed = parser(ln)
        if parsed is None:
            continue
        sid, year, vals = parsed
        if sid not in raw:
            raw[sid] = {}
            order.append(sid)
        for k, v in enumerate(vals):
            if v == 999 or v == -9999:
                precision[sid] = 0.01 if v == 999 else 0.001
                break
            raw[sid][year + k] = v
    if not raw:
        raise ValueError("No Tucson ring-width data lines found")
    years = sorted({y for d in raw.values() for y in d})
    df = pd.DataFrame(index=pd.Index(years, name="year"), columns=order, dtype=float)
    # a series without its own end marker takes the precision used by most series of the file
    found = list(precision.values())
    default_prec = max(set(found), key=found.count) if found else 0.01
    for sid in order:
        factor = precision.get(sid, default_prec)
        s = pd.Series(raw[sid], dtype=float)
        s[s < 0] = np.nan
        s = s * factor
        if zero_as_nan:
            s[s == 0] = np.nan
        df.loc[s.index, sid] = s.values
    df = df.dropna(how="all")
    meta = {
        "precision_mm": {sid: precision.get(sid, default_prec) for sid in order},
        "series_without_end_marker": [sid for sid in order if sid not in precision],
        "source_path": os.path.abspath(path),
    }
    return df, meta


# ---------------------------------------------------------------------------
# ITRDB preview (download to a temporary file, parse, summarise, plot)
# ---------------------------------------------------------------------------

@dataclass
class ItrdbPreview:
    file: ItrdbFile
    site: Optional[ItrdbSite]
    kind: str                     # "rwl" or "crn"
    data: pd.DataFrame            # rwl: index=year, columns=series (mm); crn: Year, Index, Index_n
    meta: Dict[str, Any]
    summary: Dict[str, Any]
    header: List[str]
    size_bytes: int

    def summary_text(self) -> str:
        lines = [f"File: {self.file.filename}  ({human_size(self.size_bytes)})",
                 f"Type: {self.file.kind}  |  {self.file.parameter}"
                 + (f"  |  {self.file.variant}" if self.file.variant else "")]
        if self.site is not None:
            s = self.site
            lines.append(f"Site: {s.site_code} {s.site_name}  |  {s.species}")
            if s.lat is not None:
                lines.append(f"Location: {s.lat:.3f} N, {s.lon:.3f} E"
                             + (f", {s.elevation_m:.0f} m" if s.elevation_m else ""))
            if s.investigators:
                lines.append(f"Investigators: {s.investigators}")
            if s.doi:
                lines.append(f"DOI: {s.doi}")
        lines.append("")
        for k, v in self.summary.items():
            lines.append(f"{k}: {v}")
        if self.header:
            lines.append("")
            lines.append("File header:")
            lines.extend("  " + h.rstrip() for h in self.header)
        return "\n".join(lines)


def _first_lines(path: str, n: int = 3) -> List[str]:
    return [ln for ln in _read_text_lines(path) if ln.strip()][:n]


def preview_itrdb_file(f: ItrdbFile, site: Optional[ItrdbSite] = None, timeout: float = 60.0) -> ItrdbPreview:
    """Download a .rwl / .crn into a temporary file, parse it and summarise it.

    Nothing is written to the user's folders; the temporary file is removed.
    """
    if f.kind not in ("rwl", "crn"):
        raise OnlineError("Preview is available for Tucson .rwl and .crn files only")
    data = http_get_bytes(f.url, timeout=timeout)
    tmp_dir = tempfile.mkdtemp(prefix="dendro_preview_")
    path = os.path.join(tmp_dir, f.filename)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        header = _first_lines(path, 3)
        if f.kind == "rwl":
            df, meta = read_tucson_rwl(path, zero_as_nan=False)
            lengths = df.notna().sum(axis=0)
            depth = df.notna().sum(axis=1)
            zeros = int((df == 0).sum().sum())
            precisions = sorted(set(meta["precision_mm"].values()))
            summary: Dict[str, Any] = {
                "Series": int(df.shape[1]),
                "Period": f"{int(df.index.min())}-{int(df.index.max())} ({len(df)} years)",
                "Series length (min / mean / max)": f"{int(lengths.min())} / {lengths.mean():.0f} / {int(lengths.max())}",
                "Mean ring width (mm)": f"{np.nanmean(df.replace(0, np.nan).values):.3f}",
                "Precision (mm)": ", ".join(f"{p:g}" for p in precisions),
                "Zero rings (missing rings)": zeros,
                "Max sample depth": int(depth.max()),
                "Years with depth >= 5": (f"{int(depth[depth >= 5].index.min())}-{int(depth[depth >= 5].index.max())}"
                                          if (depth >= 5).any() else "none"),
            }
        else:
            df, meta = read_tucson_crn(path)
            vcols = [c for c in df.columns if c != "Year" and not str(c).endswith("_n")]
            main = vcols[0]
            depth = pd.to_numeric(df[f"{main}_n"], errors="coerce")
            v = df[main]
            summary = {
                "Chronologies in file": len(vcols),
                "Period": f"{int(df['Year'].min())}-{int(df['Year'].max())} ({len(df)} years)",
                "Index mean / SD": f"{v.mean():.3f} / {v.std():.3f}",
                "Max sample depth": int(depth.max()) if depth.notna().any() else "n/a",
                "Years with depth >= 5": (f"{int(df.loc[depth >= 5, 'Year'].min())}-{int(df.loc[depth >= 5, 'Year'].max())}"
                                          if (depth >= 5).any() else "none"),
            }
            if len(v.dropna()) > 2:
                summary["Lag-1 autocorrelation"] = f"{v.autocorr(lag=1):.3f}"
        return ItrdbPreview(f, site, f.kind, df, meta, summary, header, len(data))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def draw_itrdb_preview(fig, pv: ItrdbPreview) -> None:
    """Plot a preview: rwl = series spans + mean width/depth; crn = index + sample depth."""
    fig.clear()
    gs = fig.add_gridspec(2, 1, height_ratios=[1.3, 1.0], hspace=0.28)
    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
    title = f"{pv.site.site_code + ' ' if pv.site else ''}{pv.file.filename}"
    if pv.kind == "rwl":
        df = pv.data
        spans = []
        for col in df.columns:
            s = df[col].dropna()
            if len(s):
                spans.append((int(s.index.min()), int(s.index.max()), col))
        spans.sort()
        for k, (a, b, col) in enumerate(spans):
            ax1.hlines(k, a, b, color="#2a6fdb", linewidth=1.6)
        ax1.set_ylabel("Series")
        ax1.set_yticks(range(len(spans)))
        ax1.set_yticklabels([c for _, _, c in spans] if len(spans) <= 40 else [""] * len(spans), fontsize=6)
        ax1.set_title(f"{title}  |  {len(spans)} series", fontsize=9)
        mean_w = df.replace(0, np.nan).mean(axis=1)
        depth = df.notna().sum(axis=1)
        ax2.plot(mean_w.index, mean_w.values, color="#8b4513", linewidth=1.0)
        ax2.set_ylabel("Mean width (mm)", color="#8b4513")
        ax3 = ax2.twinx()
        ax3.fill_between(depth.index, depth.values, step="mid", color="#999999", alpha=0.25)
        ax3.set_ylabel("Sample depth", color="#666666")
    else:
        df = pv.data
        vcols = [c for c in df.columns if c != "Year" and not str(c).endswith("_n")]
        for c in vcols:
            ax1.plot(df["Year"], df[c], linewidth=0.9, label=str(c))
        ax1.axhline(1.0, color="black", linewidth=0.6, alpha=0.6)
        ax1.set_ylabel("Index")
        if len(vcols) > 1:
            ax1.legend(fontsize=7)
        ax1.set_title(f"{title}  |  {pv.file.variant or 'chronology'}", fontsize=9)
        depth = pd.to_numeric(df[f"{vcols[0]}_n"], errors="coerce")
        ax2.fill_between(df["Year"], depth.values, step="mid", color="#2a6fdb", alpha=0.35)
        ax2.axhline(5, color="#c62828", linewidth=0.7, linestyle="--")
        ax2.set_ylabel("Sample depth")
    ax2.set_xlabel("Year")
    for ax in (ax1, ax2):
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.25)
    fig.subplots_adjust(left=0.1, right=0.9, top=0.93, bottom=0.09)


# ---------------------------------------------------------------------------
# Country boundaries and location map
# ---------------------------------------------------------------------------

COUNTRIES_FILENAME = "dendro_online_countries.geojson"
COUNTRIES_FALLBACK_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_50m_admin_0_countries.geojson"
)

_COUNTRIES: Optional[List[Dict[str, Any]]] = None


def _module_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return getattr(sys, "_MEIPASS")
    return os.path.dirname(os.path.abspath(__file__))


def _countries_paths() -> List[str]:
    paths = [
        os.path.join(_module_dir(), COUNTRIES_FILENAME),
        os.path.join(_module_dir(), "data", COUNTRIES_FILENAME),
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0] or ".")), COUNTRIES_FILENAME),
    ]
    try:
        paths.append(os.path.join(default_cache_dir(), COUNTRIES_FILENAME))
    except OSError:
        pass
    return paths


def _polygons_of(geometry: Dict[str, Any]) -> List[List[np.ndarray]]:
    """Return list of polygons; each polygon = [outer_ring, hole1, ...] as Nx2 (lon, lat)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    polys: List[List[np.ndarray]] = []
    if gtype == "Polygon":
        polys.append([np.asarray(r, dtype=float) for r in coords])
    elif gtype == "MultiPolygon":
        for p in coords:
            polys.append([np.asarray(r, dtype=float) for r in p])
    return polys


def load_countries(download_if_missing: bool = True) -> List[Dict[str, Any]]:
    """Load country polygons (Natural Earth 1:50m, public domain)."""
    global _COUNTRIES
    if _COUNTRIES is not None:
        return _COUNTRIES
    path = next((p for p in _countries_paths() if os.path.exists(p)), None)
    if path is None and download_if_missing:
        dest = os.path.join(default_cache_dir(), COUNTRIES_FILENAME)
        try:
            download_file(COUNTRIES_FALLBACK_URL, dest)
            path = dest
        except Exception:
            path = None
    if path is None:
        _COUNTRIES = []
        return _COUNTRIES
    with open(path, "r", encoding="utf-8") as fh:
        gj = json.load(fh)
    out: List[Dict[str, Any]] = []
    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        polys = _polygons_of(feat.get("geometry") or {})
        if not polys:
            continue
        allpts = np.vstack([p[0] for p in polys])
        out.append({
            "name": props.get("name") or props.get("NAME") or props.get("ADMIN") or "",
            "name_long": props.get("name_long") or props.get("NAME_LONG") or "",
            "iso_a3": props.get("iso_a3") or props.get("ISO_A3") or props.get("ADM0_A3") or "",
            "polygons": polys,
            "bounds": (float(allpts[:, 1].min()), float(allpts[:, 1].max()),
                       float(allpts[:, 0].min()), float(allpts[:, 0].max())),  # lat0, lat1, lon0, lon1
        })
    _COUNTRIES = out
    return out


def _point_in_polygon(poly: List[np.ndarray], lon: float, lat: float) -> bool:
    from matplotlib.path import Path

    if not Path(poly[0]).contains_point((lon, lat)):
        return False
    for hole in poly[1:]:
        if Path(hole).contains_point((lon, lat)):
            return False
    return True


def find_country(lat: float, lon: float, max_snap_deg: float = 0.3) -> Optional[Dict[str, Any]]:
    """Country containing the point; snaps to the nearest one within max_snap_deg (coast/border)."""
    lon = _wrap_lon(lon)
    countries = load_countries()
    for c in countries:
        la0, la1, lo0, lo1 = c["bounds"]
        if not (la0 <= lat <= la1 and lo0 <= lon <= lo1):
            continue
        if any(_point_in_polygon(p, lon, lat) for p in c["polygons"]):
            return c
    best, best_d = None, max_snap_deg
    for c in countries:
        la0, la1, lo0, lo1 = c["bounds"]
        if not (la0 - max_snap_deg <= lat <= la1 + max_snap_deg and lo0 - max_snap_deg <= lon <= lo1 + max_snap_deg):
            continue
        for p in c["polygons"]:
            ring = p[0]
            d = float(np.min(np.hypot(ring[:, 0] - lon, ring[:, 1] - lat)))
            if d < best_d:
                best, best_d = c, d
    return best


def _map_extent(lat, lon, country, cells, bbox, sites, grid_res, max_span_deg):
    xs: List[float] = []
    ys: List[float] = []
    if country is not None:
        la0, la1, lo0, lo1 = country["bounds"]
        ys += [la0, la1]
        xs += [lo0, lo1]
    if lat is not None and lon is not None:
        ys.append(lat)
        xs.append(_wrap_lon(lon))
    for c in cells or []:
        ys += [c.lat - grid_res / 2, c.lat + grid_res / 2]
        xs += [c.lon - grid_res / 2, c.lon + grid_res / 2]
    if bbox:
        ys += [bbox[0], bbox[1]]
        xs += [_wrap_lon(bbox[2]), _wrap_lon(bbox[3])]
    for s in sites or []:
        ys.append(s[0])
        xs.append(_wrap_lon(s[1]))
    if not xs:
        return -180.0, 180.0, -60.0, 80.0
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    ref_lat = lat if lat is not None else (y0 + y1) / 2.0
    coslat = max(math.cos(math.radians(ref_lat)), 0.2)
    if lat is not None and lon is not None and (y1 - y0 > max_span_deg or (x1 - x0) * coslat > max_span_deg):
        cx, cy = _wrap_lon(lon), lat
        half_y = max_span_deg / 2.0
        half_x = half_y / coslat
        x0, x1 = max(x0, cx - half_x), min(x1, cx + half_x)
        y0, y1 = max(y0, cy - half_y), min(y1, cy + half_y)
        if bbox:
            x0, x1 = min(x0, bbox[2]), max(x1, bbox[3])
            y0, y1 = min(y0, bbox[0]), max(y1, bbox[1])
    pad = max(0.4, 0.06 * max(x1 - x0, y1 - y0))
    x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    if x1 - x0 < 3.0:
        cx = (x0 + x1) / 2
        x0, x1 = cx - 1.5, cx + 1.5
    if y1 - y0 < 2.0:
        cy = (y0 + y1) / 2
        y0, y1 = cy - 1.0, cy + 1.0
    return x0, x1, max(y0, -90.0), min(y1, 90.0)


_ALL_RINGS: Optional[List[np.ndarray]] = None


def _all_country_rings() -> List[np.ndarray]:
    """Outer rings of all countries (drawn once as one collection, so the map can be panned anywhere)."""
    global _ALL_RINGS
    if _ALL_RINGS is None:
        _ALL_RINGS = [p[0] for c in load_countries() for p in c["polygons"]]
    return _ALL_RINGS


def _update_grid_lines(ax) -> None:
    """Grid lines for the visible part of the map (redrawn after zoom / pan)."""
    from matplotlib.collections import LineCollection

    old = getattr(ax, "_dendro_grid", None)
    if old is not None:
        try:
            old.remove()
        except Exception:
            pass
        ax._dendro_grid = None
    res = float(getattr(ax, "_dendro_grid_res", 0.0) or 0.0)
    if res <= 0:
        return
    x0, x1 = sorted(ax.get_xlim())
    y0, y1 = sorted(ax.get_ylim())
    if max(x1 - x0, y1 - y0) / res > getattr(ax, "_dendro_grid_max", 48):
        return
    xs = np.arange(math.floor((x0 + 180.0) / res) * res - 180.0, x1 + res, res)
    ys = np.arange(math.floor((y0 + 90.0) / res) * res - 90.0, y1 + res, res)
    segs = [[(x, y0), (x, y1)] for x in xs] + [[(x0, y), (x1, y)] for y in ys]
    lc = LineCollection(segs, colors="#5b7da8", alpha=0.22, linewidths=0.5, zorder=3)
    ax.add_collection(lc, autolim=False)
    ax._dendro_grid = lc


def _draw_map_panel(ax, extent, lat, lon, country, cells, bbox, sites, grid_res,
                    show_weights, label_neighbours, max_grid_lines=48):
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Polygon as MplPolygon, Rectangle

    x0, x1, y0, y1 = extent
    ax.set_facecolor("#dfeaf3")
    # all countries (light), so that zooming out / panning shows the whole world
    ax.add_collection(PolyCollection(_all_country_rings(), facecolors="#ebebeb", edgecolors="#9a9a9a",
                                     linewidths=0.6, zorder=1), autolim=False)
    for c in load_countries():
        la0, la1, lo0, lo1 = c["bounds"]
        is_main = country is not None and c is country
        if is_main:
            for p in c["polygons"]:
                ax.add_patch(MplPolygon(p[0], closed=True, facecolor="#f4efe1", edgecolor="#333333",
                                        linewidth=1.3, zorder=2))
            continue
        if la1 < y0 or la0 > y1 or lo1 < x0 or lo0 > x1:
            continue
        cx, cy = (lo0 + lo1) / 2, (la0 + la1) / 2
        if label_neighbours and x0 < cx < x1 and y0 < cy < y1:
            ax.text(cx, cy, c["name"], fontsize=7, color="#7a7a7a", ha="center", va="center", zorder=3,
                    clip_on=True)

    ax._dendro_map = True
    ax._dendro_grid_res = grid_res
    ax._dendro_grid_max = max_grid_lines
    ax._dendro_grid = None

    cells = list(cells or [])
    if cells:
        wmax = max(c.weight for c in cells) or 1.0
        label = show_weights and len(cells) <= 16
        for c in cells:
            alpha = 0.25 + 0.5 * (c.weight / wmax)
            color = "#d9534f" if c.valid is False else "#2a6fdb"
            ax.add_patch(Rectangle((c.lon - grid_res / 2, c.lat - grid_res / 2), grid_res, grid_res,
                                   facecolor=color, alpha=alpha, edgecolor=color, linewidth=0.8, zorder=4))
            if label:
                txt = f"w={c.weight:.2f}" if c.valid is not False else "no data"
                ax.text(c.lon, c.lat - grid_res * 0.28, txt, fontsize=7, ha="center", va="center",
                        color="black", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.15", facecolor="white", alpha=0.75, linewidth=0))
                ax.plot(c.lon, c.lat, marker="o", color="#0d2d6b", markersize=2.5, zorder=5)

    if bbox:
        ax.add_patch(Rectangle((bbox[2], bbox[0]), bbox[3] - bbox[2], bbox[1] - bbox[0],
                               fill=False, edgecolor="#e07b00", linewidth=1.6, linestyle="--", zorder=6))

    # group sites sharing (almost) the same coordinates: one marker, one label "CODE +n"
    tol = 0.03 * max(x1 - x0, y1 - y0)
    groups: Dict[Tuple[float, float], List[str]] = {}
    for s in sites or []:
        sx, sy = _wrap_lon(s[1]), s[0]
        if not (x0 <= sx <= x1 and y0 <= sy <= y1):
            continue
        ax.plot(sx, sy, marker="^", color="#1b7f3b", markersize=6,
                markeredgecolor="white", markeredgewidth=0.6, zorder=7)
        key = next((k for k in groups if abs(k[0] - sy) <= tol and abs(k[1] - sx) <= tol), (sy, sx))
        groups.setdefault(key, []).append(str(s[2] or ""))
    for (sy, sx), labels in groups.items():
        names = [x for x in labels if x]
        if names and len(groups) <= 60:
            text = names[0] + (f" +{len(labels) - 1}" if len(labels) > 1 else "")
            ax.annotate(text, (sx, sy), xytext=(4, 3), textcoords="offset points",
                        fontsize=7, color="#1b5e2e", zorder=8)

    if lat is not None and lon is not None:
        ax.plot(_wrap_lon(lon), lat, marker="*", color="#c62828", markersize=14,
                markeredgecolor="white", markeredgewidth=0.8, zorder=9)

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect(1.0 / max(math.cos(math.radians((y0 + y1) / 2.0)), 0.2))
    ax.tick_params(labelsize=7)
    _update_grid_lines(ax)
    ax.callbacks.connect("xlim_changed", _update_grid_lines)
    ax.callbacks.connect("ylim_changed", _update_grid_lines)


def draw_location_map(
    fig,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    cells: Optional[Sequence[GridCell]] = None,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    sites: Optional[Sequence[Tuple[float, float, str]]] = None,
    grid_res: float = CRU_GRID_RES,
    title: Optional[str] = None,
    max_span_deg: float = 24.0,
    show_weights: bool = True,
    zoom: Optional[bool] = None,
    view: Optional[Tuple[float, float, float, float]] = None,
):
    """Draw a country-level location map on a matplotlib Figure.

    ``view`` = (lon_min, lon_max, lat_min, lat_max) keeps a view chosen by the
    user (zoom / pan) instead of the automatic country extent.

    Left panel: the country that contains (lat, lon) with neighbours inside
    the view, the target point, bbox and extra points (e.g. ITRDB sites).
    Right panel (zoom): the 0.5 deg grid around the cells actually used, with
    their weights; red cells have no data (sea / lake). The zoom panel is
    added automatically when the cells would be too small in the overview.
    Returns the list of Axes. Works with any canvas (Qt, Tk, Agg).
    """
    fig.clear()
    country = find_country(lat, lon) if (lat is not None and lon is not None) else None
    cells = list(cells or [])
    extent = _map_extent(lat, lon, country, cells, bbox, sites, grid_res, max_span_deg)
    if view is not None:
        extent = (float(view[0]), float(view[1]), max(float(view[2]), -90.0), min(float(view[3]), 90.0))
    span = max(extent[1] - extent[0], extent[3] - extent[2])

    if zoom is None:
        fine = bool(grid_res) and span / grid_res > 60
        zoom = bool(cells) and len(cells) <= 400 and (span > 7.0 or fine)
    if zoom:
        gs = fig.add_gridspec(1, 2, width_ratios=[1.35, 1.0])
        ax_main = fig.add_subplot(gs[0, 0])
        ax_zoom = fig.add_subplot(gs[0, 1])
    else:
        ax_main = fig.add_subplot(111)
        ax_zoom = None

    _draw_map_panel(ax_main, extent, lat, lon, country, cells, bbox, sites, grid_res,
                    show_weights=not zoom, label_neighbours=True)
    fig._dendro_main_ax = ax_main
    ax_main.set_xlabel("Longitude (deg E)", fontsize=8)
    ax_main.set_ylabel("Latitude (deg N)", fontsize=8)
    axes = [ax_main]

    if ax_zoom is not None:
        zx = [c.lon for c in cells] + ([_wrap_lon(lon)] if lon is not None else [])
        zy = [c.lat for c in cells] + ([lat] if lat is not None else [])
        if bbox:
            zx += [bbox[2], bbox[3]]
            zy += [bbox[0], bbox[1]]
        m = grid_res * 1.5
        z_extent = (min(zx) - m, max(zx) + m, min(zy) - m, max(zy) + m)
        zw, zh = z_extent[1] - z_extent[0], z_extent[3] - z_extent[2]
        min_w = 2.5 if grid_res >= 0.25 else max(10 * grid_res, 0.05)
        min_h = 2.0 if grid_res >= 0.25 else max(8 * grid_res, 0.04)
        if zw < min_w:
            c0 = (z_extent[0] + z_extent[1]) / 2
            z_extent = (c0 - min_w / 2, c0 + min_w / 2, z_extent[2], z_extent[3])
        if zh < min_h:
            c0 = (z_extent[2] + z_extent[3]) / 2
            z_extent = (z_extent[0], z_extent[1], c0 - min_h / 2, c0 + min_h / 2)
        from matplotlib.patches import Rectangle
        ax_main.add_patch(Rectangle((z_extent[0], z_extent[2]), z_extent[1] - z_extent[0],
                                    z_extent[3] - z_extent[2], fill=False, edgecolor="#6a1b9a",
                                    linewidth=1.0, zorder=10))
        _draw_map_panel(ax_zoom, z_extent, lat, lon, country, cells, bbox, sites, grid_res,
                        show_weights=show_weights, label_neighbours=False, max_grid_lines=200)
        ax_zoom.set_title("Grid cells used (zoom)" if grid_res >= 0.02 else
                          f"Pixels used (zoom, {grid_res * 111.0:.1f} km)", fontsize=8, color="#6a1b9a")
        ax_zoom.set_xlabel("Longitude (deg E)", fontsize=8)
        axes.append(ax_zoom)

    if title is None:
        parts = []
        if country is not None:
            parts.append(country["name"])
        elif lat is not None:
            parts.append("Point is outside all country polygons (sea / large lake?)")
        if lat is not None and lon is not None:
            parts.append(f"{lat:.3f} N, {_wrap_lon(lon):.3f} E")
        if cells:
            n_bad = sum(1 for c in cells if c.valid is False)
            parts.append(f"{len(cells)} grid cell(s)" + (f", {n_bad} without data" if n_bad else ""))
        title = " | ".join(parts)
    fig.suptitle(title, fontsize=9)
    try:
        import warnings as _w

        with _w.catch_warnings():
            _w.simplefilter("ignore")
            fig.tight_layout()
    except Exception:
        pass
    ax_main._dendro_auto_extent = (*ax_main.get_xlim(), *ax_main.get_ylim())
    return axes


def user_map_view(fig, lat: Optional[float], lon: Optional[float]) -> Optional[Tuple[float, float, float, float]]:
    """The view of the last drawn map if the user zoomed / panned it and the point is still inside."""
    ax = getattr(fig, "_dendro_main_ax", None)
    if ax is None or ax.figure is not fig:
        return None
    cur = (*ax.get_xlim(), *ax.get_ylim())
    auto = getattr(ax, "_dendro_auto_extent", None)
    if auto is not None and all(abs(a - b) < 1e-9 for a, b in zip(cur, auto)):
        return None
    if lat is None or lon is None:
        return cur
    if cur[0] <= _wrap_lon(lon) <= cur[1] and cur[2] <= lat <= cur[3]:
        return cur
    return None


WORLD_VIEW = (-180.0, 180.0, -60.0, 84.0)


def snap_box_to_grid(bbox: Tuple[float, float, float, float], res: float) -> Tuple[float, float, float, float]:
    """(S, N, W, E) expanded to whole grid cells of size ``res`` (grid edges at -180 / -90 + k * res)."""
    s, n, w, e = bbox
    if not res or res <= 0:
        return bbox
    s2 = math.floor((s + 90.0) / res + 1e-9) * res - 90.0
    n2 = math.ceil((n + 90.0) / res - 1e-9) * res - 90.0
    w2 = math.floor((w + 180.0) / res + 1e-9) * res - 180.0
    e2 = math.ceil((e + 180.0) / res - 1e-9) * res - 180.0
    if n2 <= s2:
        n2 = s2 + res
    if e2 <= w2:
        e2 = w2 + res
    r = lambda v: round(v, 6)  # noqa: E731
    return r(max(s2, -90.0)), r(min(n2, 90.0)), r(w2), r(e2)


class MapInteractor:
    """Mouse picking on a location map (any matplotlib canvas: Qt or Tk).

    * click            -> on_point(lat, lon, add=False)
    * Ctrl + click     -> on_point(lat, lon, add=True)
    * drag (left)      -> on_box(S, N, W, E)  (snapped to the grid when snap() > 0)
    * mouse wheel      -> zoom in / out around the cursor
    * right / middle drag -> pan
    The toolbar zoom / pan modes keep working (picking is paused while they are on).
    """

    def __init__(self, canvas, on_point: Optional[Callable[..., None]] = None,
                 on_box: Optional[Callable[..., None]] = None,
                 snap: Optional[Callable[[], float]] = None) -> None:
        self.canvas = canvas
        self.on_point = on_point
        self.on_box = on_box
        self.snap = snap or (lambda: 0.0)
        self.enabled = True
        self._press: Optional[Tuple[Any, ...]] = None
        self._rect = None
        self._pan: Optional[Tuple[Any, ...]] = None
        self._cids = [
            canvas.mpl_connect("button_press_event", self._on_press),
            canvas.mpl_connect("motion_notify_event", self._on_motion),
            canvas.mpl_connect("button_release_event", self._on_release),
            canvas.mpl_connect("scroll_event", self._on_scroll),
        ]

    def disconnect(self) -> None:
        for cid in self._cids:
            try:
                self.canvas.mpl_disconnect(cid)
            except Exception:
                pass
        self._cids = []

    @staticmethod
    def _is_map(ax) -> bool:
        return ax is not None and bool(getattr(ax, "_dendro_map", False))

    def _locked(self) -> bool:
        try:
            return bool(self.canvas.widgetlock.locked())
        except Exception:
            return False

    @staticmethod
    def _data(ax, event) -> Tuple[float, float]:
        x, y = ax.transData.inverted().transform((event.x, event.y))
        return float(x), float(y)

    def _on_press(self, event) -> None:
        if not self.enabled or self._locked() or not self._is_map(event.inaxes):
            return
        ax = event.inaxes
        if event.button == 1:
            key = str(event.key or "").lower()
            self._press = (ax, event.x, event.y, *self._data(ax, event), "control" in key or "ctrl" in key)
        elif event.button in (2, 3):
            self._pan = (ax, event.x, event.y, ax.get_xlim(), ax.get_ylim())

    def _on_motion(self, event) -> None:
        if self._pan is not None and event.x is not None:
            ax, px, py, xl, yl = self._pan
            inv = ax.transData.inverted()
            (ax0, ay0), (ax1, ay1) = inv.transform([(px, py), (event.x, event.y)])
            dx, dy = ax0 - ax1, ay0 - ay1
            ax.set_xlim(xl[0] + dx, xl[1] + dx)
            ax.set_ylim(yl[0] + dy, yl[1] + dy)
            self.canvas.draw_idle()
            return
        if self._press is None or event.x is None:
            return
        ax, px, py, x0, y0, _add = self._press
        if abs(event.x - px) < 5 and abs(event.y - py) < 5 and self._rect is None:
            return
        x1, y1 = self._data(ax, event)
        from matplotlib.patches import Rectangle

        if self._rect is None:
            self._rect = Rectangle((x0, y0), 0, 0, facecolor="#e07b00", alpha=0.18, edgecolor="#e07b00",
                                   linewidth=1.5, linestyle="--", zorder=30)
            ax.add_patch(self._rect)
        self._rect.set_bounds(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        self.canvas.draw_idle()

    def _on_release(self, event) -> None:
        if self._pan is not None:
            self._pan = None
            return
        if self._press is None:
            return
        ax, px, py, x0, y0, add = self._press
        self._press = None
        if self._rect is not None:
            try:
                self._rect.remove()
            except Exception:
                pass
            self._rect = None
            if event.x is None:
                return
            x1, y1 = self._data(ax, event)
            box = (max(min(y0, y1), -90.0), min(max(y0, y1), 90.0), min(x0, x1), max(x0, x1))
            res = float(self.snap() or 0.0)
            if res > 0:
                box = snap_box_to_grid(box, res)
            if self.on_box is not None:
                self.on_box(*box)
            return
        lat, lon = max(min(y0, 90.0), -90.0), _wrap_lon(x0)
        if self.on_point is not None:
            self.on_point(round(lat, 5), round(lon, 5), add)

    def _on_scroll(self, event) -> None:
        ax = event.inaxes
        if not self.enabled or not self._is_map(ax) or event.xdata is None:
            return
        f = 1 / 1.3 if event.button == "up" else 1.3
        x, y = event.xdata, event.ydata
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        ax.set_xlim(x - (x - x0) * f, x + (x1 - x) * f)
        ax.set_ylim(y - (y - y0) * f, y + (y1 - y) * f)
        self.canvas.draw_idle()


MAP_HINT = ("Map: click = set point, drag = draw box, Ctrl+click = add target, mouse wheel = zoom, "
            "right-drag = pan")


def save_location_map_png(path: str, dpi: int = 150, figsize: Tuple[float, float] = (11.0, 6.0), **kwargs) -> str:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=figsize)
    FigureCanvasAgg(fig)
    draw_location_map(fig, **kwargs)
    fig.savefig(path, dpi=dpi)
    return path


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------

def _cli_progress(stage: str, done: int, total: int, message: str) -> None:
    pct = f"{100.0 * done / total:5.1f}% " if total else ""
    sys.stdout.write(f"\r[{stage:>10}] {pct}{message[:100]:<100}")
    sys.stdout.flush()
    if stage in ("done",) or (stage == "file" and total and done == total):
        sys.stdout.write("\n")


def _cmd_probe(args: argparse.Namespace) -> int:
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    report: List[str] = []

    def rep(msg: str) -> None:
        print(msg)
        report.append(msg)

    rep(f"dendro_online {__version__} | Python {sys.version.split()[0]} | {sys.platform}")
    ok_all = True

    # 1. CRU
    try:
        versions = list_cru_versions()
        rep(f"[OK] CRU versions: {', '.join(versions[:6])}")
        rel = resolve_cru_release(versions[0])
        rep(f"[OK] CRU {rel.version} data dir: {rel.data_dir}")
        files = list_cru_files(rel, "tmp")
        sizes = [f for f in files if f.size_bytes]
        rep(f"[OK] CRU tmp files: {len(files)} (sizes known for {len(sizes)}); "
            f"first={files[0].filename}, last={files[-1].filename}")
    except Exception as exc:
        ok_all = False
        rep(f"[FAIL] CRU catalogue: {exc}")
        rel, files = None, []

    # 2. ITRDB search around Sughd / Zeravshan
    sites: List[ItrdbSite] = []
    try:
        raw_path = os.path.join(out, "itrdb_search_raw.json")
        sites = search_itrdb(min_lat=36.5, max_lat=42.0, min_lon=66.0, max_lon=76.0, raw_json_path=raw_path)
        rep(f"[OK] ITRDB search (Tajikistan region): {len(sites)} site(s); raw JSON: {raw_path}")
        for s in sites[:8]:
            rep(f"     {s.site_code:<8} {s.site_name[:30]:<30} {s.species[:22]:<22} "
                f"{s.lat} {s.lon}  {s.first_year}-{s.last_year}  rwl={len(s.files_of_kind('rwl'))} "
                f"crn={len(s.files_of_kind('crn'))}")
        pd.DataFrame([s.as_row() for s in sites]).to_csv(os.path.join(out, "itrdb_sites.csv"), index=False)
    except Exception as exc:
        ok_all = False
        rep(f"[FAIL] ITRDB search: {exc}")

    # 3. ITRDB download + parse
    try:
        site = next((s for s in sites if s.files_of_kind("crn") and s.files_of_kind("rwl")
                     and any(f.parameter == ITRDB_PARAM_SUFFIX[""] for f in s.files_of_kind("crn"))), None)
        if site is None:
            site = next((s for s in sites if s.files_of_kind("crn") and s.files_of_kind("rwl")), None)
        if site is None:
            rep("[SKIP] No ITRDB site with both .crn and .rwl in the probe region")
        else:
            crns = sorted(site.files_of_kind("crn"), key=lambda f: (f.parameter != ITRDB_PARAM_SUFFIX[""],
                                                                    f.variant != "standard"))
            rwls = sorted(site.files_of_kind("rwl"), key=lambda f: f.parameter != ITRDB_PARAM_SUFFIX[""])
            crn, rwl = crns[0], rwls[0]
            paths = download_itrdb_files([crn, rwl], os.path.join(out, "itrdb"))
            cdf, cmeta = read_tucson_crn(paths[0])
            rep(f"[OK] CRN {crn.filename}: {len(cdf)} years {int(cdf['Year'].min())}-{int(cdf['Year'].max())}, "
                f"columns={list(cdf.columns)}")
            rdf, rmeta = read_tucson_rwl(paths[1])
            precs = sorted(set(rmeta["precision_mm"].values()))
            rep(f"[OK] RWL {rwl.filename}: {rdf.shape[1]} series, {rdf.index.min()}-{rdf.index.max()}, "
                f"precision(mm)={precs}, mean width={np.nanmean(rdf.values):.3f} mm")
    except Exception as exc:
        ok_all = False
        rep(f"[FAIL] ITRDB download/parse: {exc}")

    # 4. Map
    try:
        lat, lon = 40.28, 69.62  # Khujand
        t = ExtractionTarget("probe", lat, lon, "bilinear")
        cells = cells_for_target(t)
        mp = save_location_map_png(os.path.join(out, "probe_map.png"), lat=lat, lon=lon, cells=cells,
                                   sites=[(s.lat, s.lon, s.site_code) for s in sites if s.lat is not None])
        country = find_country(lat, lon)
        rep(f"[OK] Map: {mp} (country: {country['name'] if country else 'none'})")
    except Exception as exc:
        ok_all = False
        rep(f"[FAIL] Map: {exc}")

    # 5. Optional CRU download test
    if args.with_cru:
        try:
            req = CruRequest(version=args.cru_version or (rel.version if rel else "4.10"), variables=["tmp"],
                             targets=[ExtractionTarget("probe", 40.28, 69.62, "bilinear")],
                             start_year=2011, end_year=2020, keep_downloads=False)
            res = run_cru_request(req, progress=_cli_progress)
            r = res.results["probe"]["tmp"]
            wide = r.to_wide()
            wide.to_csv(os.path.join(out, "probe_cru_tmp.csv"), index=False)
            rep(f"\n[OK] CRU tmp 2011-2020 at 40.28N 69.62E: {len(wide)} years, "
                f"Jul mean={wide['Jul'].mean():.2f} degC, mean stn={r.meta.get('mean_stn')}")
            save_location_map_png(os.path.join(out, "probe_map_cru.png"), lat=40.28, lon=69.62, cells=r.cells)
            for line in res.log:
                rep("     " + line)
        except Exception as exc:
            ok_all = False
            rep(f"\n[FAIL] CRU download/extract: {exc}")

    with open(os.path.join(out, "probe_report.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"\nReport: {os.path.join(out, 'probe_report.txt')}")
    return 0 if ok_all else 1


def _cmd_cru_releases(args: argparse.Namespace) -> int:
    for v in list_cru_versions():
        try:
            rel = resolve_cru_release(v)
            print(f"{v:<6} {rel.data_dir:<28} ~1901-{rel.end_year}")
        except OnlineError as exc:
            print(f"{v:<6} (unresolved: {exc})")
    return 0


def _parse_bbox(values: Optional[Sequence[float]]) -> Optional[Tuple[float, float, float, float]]:
    if not values:
        return None
    a, b, c, d = values
    return (float(a), float(b), float(c), float(d))


def _cmd_cru_map(args: argparse.Namespace) -> int:
    t = ExtractionTarget("map", args.lat, args.lon, args.mode, _parse_bbox(args.bbox))
    cells = cells_for_target(t)
    save_location_map_png(args.out, lat=args.lat, lon=args.lon, cells=cells, bbox=t.bbox)
    for c in cells[:20]:
        print(f"cell {c.lat:8.2f} {c.lon:8.2f}  w={c.weight:.4f}")
    if len(cells) > 20:
        print(f"... {len(cells)} cells in total")
    print(f"Map saved: {args.out}")
    return 0


def _cmd_cru_extract(args: argparse.Namespace) -> int:
    version = args.version or list_cru_versions()[0]
    t = ExtractionTarget(args.name, args.lat, args.lon, args.mode, _parse_bbox(args.bbox))
    req = CruRequest(version=version, variables=[v.lower() for v in args.var], targets=[t],
                     start_year=args.start, end_year=args.end, cache_dir=args.cache_dir,
                     keep_downloads=args.keep)
    release, files = plan_cru_download(req)
    size, exact = estimate_download_size(files)
    print(f"CRU TS {release.version}: {len(files)} file(s), {'' if exact else '~'}{human_size(size)} to download")
    res = run_cru_request(req, progress=_cli_progress, planned=(release, files))
    os.makedirs(args.out, exist_ok=True)
    for var, r in res.results[t.name].items():
        p = os.path.join(args.out, f"cru_ts{release.version}_{var}_{t.name}.csv")
        r.to_wide().to_csv(p, index=False)
        ps = os.path.join(args.out, f"cru_ts{release.version}_{var}_{t.name}_stn.csv")
        r.to_wide("stn").to_csv(ps, index=False)
        print(f"\n{var}: {p}")
        w = station_support_warning(r)
        if w:
            print("WARNING: " + w)
        with open(p[:-4] + "_provenance.json", "w", encoding="utf-8") as fh:
            json.dump(res.provenance(t.name, var), fh, indent=1)
    first = next(iter(res.results[t.name].values()))
    save_location_map_png(os.path.join(args.out, f"map_{t.name}.png"), lat=t.lat, lon=t.lon,
                          cells=first.cells, bbox=t.bbox)
    return 0


def _cmd_itrdb_search(args: argparse.Namespace) -> int:
    kw: Dict[str, Any] = dict(species=args.species, earliest_year=args.start, latest_year=args.end, text=args.text)
    if args.lat is not None and args.lon is not None:
        a, b, c, d = bbox_around(args.lat, args.lon, args.radius_km)
        kw.update(min_lat=a, max_lat=b, min_lon=c, max_lon=d)
    else:
        kw.update(min_lat=args.min_lat, max_lat=args.max_lat, min_lon=args.min_lon, max_lon=args.max_lon)
    sites = search_itrdb(**kw)
    df = pd.DataFrame([s.as_row() for s in sites])
    if args.lat is not None and args.lon is not None and not df.empty:
        df["distance_km"] = [haversine_km(args.lat, args.lon, s.lat, s.lon) if s.lat is not None else np.nan
                             for s in sites]
        df = df.sort_values("distance_km")
    with pd.option_context("display.max_rows", 200, "display.width", 200):
        print(df.drop(columns=["study_url", "location"], errors="ignore").to_string(index=False) if not df.empty
              else "No sites found")
    if args.out:
        df.to_csv(args.out, index=False)
        print(f"Saved: {args.out}")
    for s in sites if args.files else []:
        print(f"\n{s.site_code} {s.site_name}")
        for f in s.files:
            print(f"   {f.kind:<9} {f.variant:<9} {f.parameter:<18} {f.url}")
    return 0


def _cmd_itrdb_download(args: argparse.Namespace) -> int:
    files = [classify_itrdb_file(u) for u in args.url]
    paths = download_itrdb_files(files, args.out, progress=_cli_progress)
    for p in paths:
        print(p)
    return 0


def _cmd_crn_to_csv(args: argparse.Namespace) -> int:
    df, meta = read_tucson_crn(args.file)
    out = args.out or os.path.splitext(args.file)[0] + "_crn.csv"
    df.to_csv(out, index=False)
    print(f"{len(df)} years, columns {list(df.columns)} -> {out}")
    return 0


def _cmd_rwl_to_csv(args: argparse.Namespace) -> int:
    df, meta = read_tucson_rwl(args.file, zero_as_nan=args.zero_as_nan)
    out = args.out or os.path.splitext(args.file)[0] + "_rwl_mm.csv"
    df.to_csv(out)
    print(f"{df.shape[1]} series, {df.index.min()}-{df.index.max()}, "
          f"precision {sorted(set(meta['precision_mm'].values()))} mm -> {out}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dendro_online", description="CRU TS and ITRDB access for DeClim tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("probe", help="Check network access and parsers; writes a report")
    s.add_argument("--out", default="dendro_online_probe")
    s.add_argument("--with-cru", action="store_true", help="Also download one CRU decade (~35 MB) and extract a point")
    s.add_argument("--cru-version", default=None)
    s.set_defaults(func=_cmd_probe)

    s = sub.add_parser("cru-releases", help="List CRU TS releases")
    s.set_defaults(func=_cmd_cru_releases)

    s = sub.add_parser("cru-map", help="Save a location map with the grid cells that would be used")
    s.add_argument("--lat", type=float, required=True)
    s.add_argument("--lon", type=float, required=True)
    s.add_argument("--mode", default="bilinear", choices=["bilinear", "nearest", "bbox_mean"])
    s.add_argument("--bbox", type=float, nargs=4, metavar=("MIN_LAT", "MAX_LAT", "MIN_LON", "MAX_LON"))
    s.add_argument("--out", default="location_map.png")
    s.set_defaults(func=_cmd_cru_map)

    s = sub.add_parser("cru-extract", help="Download CRU TS (temporarily) and extract a point / area")
    s.add_argument("--var", nargs="+", default=["tmp", "pre"], choices=sorted(CRU_VARIABLES))
    s.add_argument("--lat", type=float, required=True)
    s.add_argument("--lon", type=float, required=True)
    s.add_argument("--mode", default="bilinear", choices=["bilinear", "nearest", "bbox_mean"])
    s.add_argument("--bbox", type=float, nargs=4, metavar=("MIN_LAT", "MAX_LAT", "MIN_LON", "MAX_LON"))
    s.add_argument("--start", type=int, default=1901)
    s.add_argument("--end", type=int, default=2100)
    s.add_argument("--version", default=None)
    s.add_argument("--name", default="site")
    s.add_argument("--cache-dir", default=None)
    s.add_argument("--keep", action="store_true", help="Keep downloaded .gz files in the cache")
    s.add_argument("--out", default="cru_output")
    s.set_defaults(func=_cmd_cru_extract)

    s = sub.add_parser("itrdb-search", help="Search ITRDB sites")
    s.add_argument("--min-lat", type=float)
    s.add_argument("--max-lat", type=float)
    s.add_argument("--min-lon", type=float)
    s.add_argument("--max-lon", type=float)
    s.add_argument("--lat", type=float, help="Centre latitude (with --lon and --radius-km)")
    s.add_argument("--lon", type=float)
    s.add_argument("--radius-km", type=float, default=200.0)
    s.add_argument("--species", default=None, help="4-letter ITRDB species code, e.g. JUSE")
    s.add_argument("--start", type=int, default=None)
    s.add_argument("--end", type=int, default=None)
    s.add_argument("--text", default=None)
    s.add_argument("--files", action="store_true", help="List file URLs")
    s.add_argument("--out", default=None, help="CSV with the site table")
    s.set_defaults(func=_cmd_itrdb_search)

    s = sub.add_parser("itrdb-download", help="Download ITRDB files by URL")
    s.add_argument("--url", nargs="+", required=True)
    s.add_argument("--out", default="itrdb_files")
    s.set_defaults(func=_cmd_itrdb_download)

    s = sub.add_parser("crn-to-csv", help="Convert Tucson .crn to CSV")
    s.add_argument("file")
    s.add_argument("--out", default=None)
    s.set_defaults(func=_cmd_crn_to_csv)

    s = sub.add_parser("rwl-to-csv", help="Convert Tucson .rwl to CSV (mm)")
    s.add_argument("file")
    s.add_argument("--out", default=None)
    s.add_argument("--zero-as-nan", action="store_true")
    s.set_defaults(func=_cmd_rwl_to_csv)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CancelledError:
        print("\nCancelled.")
        return 130
    except (OnlineError, ValueError) as exc:
        print(f"\nERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
