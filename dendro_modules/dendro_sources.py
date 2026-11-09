#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_sources.py - keyless climate / hydrology data sources for DeClim tools.

Pure Python backend (no GUI). Works together with dendro_online.py (HTTP helpers,
NetCDF extraction, grid cells, maps).

Sources (no registration, no API key):
    CHELSA monthly / daily  (V2.1, ~1 km, Cloud-Optimized GeoTIFF; only the needed
                             tiles are read with HTTP range requests)
    TerraClimate            (monthly, ~4 km, THREDDS NetCDF Subset Service)
    GPCC                    (monthly precipitation since 1891, DWD open data)
    CRU scPDSI              (self-calibrating PDSI on the CRU TS grid)
    SPEIbase                (CSIC, SPEI 1-48 months; files are discovered online)
    SPEI / SPI computed     (from TerraClimate or CHELSA P and PET, any scale)
    CHIRPS v3               (daily / monthly precipitation, 0.05 deg)
    Berkeley Earth          (country / region monthly temperature)
    GHCN-Daily              (NOAA weather stations near the site)
    Open-Meteo ERA5         (daily ERA5 / ERA5-Land for a point, free non-commercial)

Command line:
    python dendro_sources.py probe [--out DIR]      # checks every source, writes a report
    python dendro_sources.py list                   # sources and variables
    python dendro_sources.py extract --source chelsa_monthly --var tas pr --lat 40.28 --lon 69.62 \
                                     --start 1980 --end 2000 --out DIR
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures as _cf
import dataclasses
import datetime as _dt
import html
import json
import math
import os
import re
import sqlite3
import statistics
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import dendro_online as DO

try:  # optional, shared with the export module
    import dendro_geoexport as GX
except Exception:  # pragma: no cover
    GX = None

__version__ = "1.0.0"

ProgressCallback = DO.ProgressCallback
CancelCheck = DO.CancelCheck
_noop = DO._noop_progress
_never = DO._never_cancel


# ===========================================================================
# HTTP: range requests (keep-alive with 'requests' when available)
# ===========================================================================

_TLS = threading.local()
_USE_REQUESTS: Optional[bool] = None


def _requests_session():
    global _USE_REQUESTS
    if _USE_REQUESTS is False:
        return None
    try:
        import requests  # type: ignore
    except Exception:
        _USE_REQUESTS = False
        return None
    s = getattr(_TLS, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": DO.USER_AGENT})
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _TLS.session = s
    _USE_REQUESTS = True
    return s


def fetch_range(url: str, start: int, length: int, timeout: float = 60.0, retries: int = 3) -> bytes:
    """Bytes [start, start+length) of a remote file (HTTP Range)."""
    global _USE_REQUESTS
    end = start + length - 1
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            s = _requests_session()
            if s is not None:
                try:
                    r = s.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=timeout, stream=True)
                except Exception as exc:
                    if "SSL" in type(exc).__name__ or "SSL" in str(exc):
                        _USE_REQUESTS = False  # corporate certificates: use the system store via urllib
                        continue
                    raise
                with r:
                    if r.status_code in (429, 500, 502, 503, 504):  # busy / rate limit: wait and retry
                        last = DO.OnlineError(f"HTTP {r.status_code} for {url}")
                        time.sleep(_retry_after(r.headers.get("Retry-After"), attempt))
                        continue
                    if r.status_code >= 400:
                        raise DO.OnlineError(f"HTTP {r.status_code} for {url}")
                    if r.status_code == 206:
                        return r.content
                    data = r.raw.read(start + length)  # server ignored Range
                    return data[start:start + length]
            with DO._open_url(url, timeout=timeout, headers={"Range": f"bytes={start}-{end}"}) as resp:
                status = getattr(resp, "status", 200)
                if status == 206:
                    return resp.read()
                data = resp.read(start + length)
                return data[start:start + length]
        except DO.OnlineError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410, 416):
                raise DO.OnlineError(f"HTTP {exc.code} for {url}") from exc
            last = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(_retry_after(exc.headers.get("Retry-After") if exc.headers else None, attempt))
                continue
        except Exception as exc:
            last = exc
        time.sleep(0.8 * (attempt + 1))
    raise DO.OnlineError(f"Cannot read {url}: {last}")


def _retry_after(header: Optional[str], attempt: int) -> float:
    """Seconds to wait after HTTP 429 / 5xx (the server's Retry-After, else 3, 9, 27 s)."""
    try:
        if header:
            return min(120.0, max(1.0, float(header)))
    except ValueError:
        pass
    return float(3 ** (attempt + 1))


def http_text(url: str, timeout: float = 60.0) -> str:
    return DO.http_get_text(url, timeout=timeout, retries=2)


def http_exists(url: str, timeout: float = 20.0) -> bool:
    return DO.http_head_size(url, timeout=timeout) is not None or _range_ok(url)


def _range_ok(url: str) -> bool:
    try:
        return len(fetch_range(url, 0, 16, timeout=20.0, retries=0)) > 0
    except Exception:
        return False


def list_http_dir(url: str, timeout: float = 45.0, retries: int = 3) -> List[str]:
    """Absolute links of an Apache / nginx style directory index."""
    page = ""
    for attempt in range(retries + 1):
        try:
            page = http_text(url, timeout=timeout)
            break
        except DO.OnlineError as exc:
            msg = str(exc)
            if attempt >= retries or not any(f" {c}" in msg for c in ("429", "500", "502", "503", "504", "timed out",
                                                                       "Timeout", "reset", "Cannot reach")):
                raise
            time.sleep(_retry_after(None, attempt))
    out: List[str] = []
    for href in DO._links_from_html(page):
        if href.startswith(("?", "#", "mailto:", "javascript:")) or href in ("../", "/"):
            continue
        absu = urllib.parse.urljoin(url, href)
        if absu.startswith(url) and absu != url:
            out.append(absu)
    return sorted(set(out))


def s3_list(endpoint: str, bucket: str, prefix: str, delimiter: Optional[str] = "/",
            timeout: float = 60.0, max_pages: int = 200) -> Tuple[List[Tuple[str, int]], List[str]]:
    """ListObjectsV2 of a public S3 / Swift bucket: ([(key, size)], [common prefixes])."""
    keys: List[Tuple[str, int]] = []
    prefixes: List[str] = []
    token: Optional[str] = None
    for _ in range(max_pages):
        q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            q["delimiter"] = delimiter
        if token:
            q["continuation-token"] = token
        url = f"{endpoint.rstrip('/')}/{bucket}?{urllib.parse.urlencode(q)}"
        xml = http_text(url, timeout=timeout)
        for m in re.finditer(r"<Contents>(.*?)</Contents>", xml, flags=re.S):
            body = m.group(1)
            k = re.search(r"<Key>(.*?)</Key>", body, flags=re.S)
            s = re.search(r"<Size>(\d+)</Size>", body)
            if k:
                keys.append((html.unescape(k.group(1)), int(s.group(1)) if s else -1))
        prefixes += [html.unescape(p) for p in re.findall(r"<CommonPrefixes>\s*<Prefix>(.*?)</Prefix>", xml, flags=re.S)]
        truncated = re.search(r"<IsTruncated>\s*true\s*</IsTruncated>", xml, flags=re.I)
        nt = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", xml, flags=re.S)
        if not truncated or not nt:
            break
        token = html.unescape(nt.group(1))
    return keys, prefixes


# ===========================================================================
# Remote (Cloud-Optimized) GeoTIFF reader
# ===========================================================================

_TIFF_TYPES = {1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1), 7: ("B", 1),
               8: ("h", 2), 9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8), 13: ("I", 4),
               16: ("Q", 8), 17: ("q", 8), 18: ("Q", 8)}


def lzw_decode(data: bytes) -> bytes:
    """TIFF LZW (MSB first, early change)."""
    try:
        import imagecodecs  # type: ignore

        return imagecodecs.lzw_decode(data)
    except Exception:
        pass
    out = bytearray()
    table: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    nbits = 9
    bitpos = 0
    total_bits = len(data) * 8
    padded = bytes(data) + b"\x00\x00\x00"
    prev: Optional[bytes] = None
    while bitpos + nbits <= total_bits:
        byte = bitpos >> 3
        chunk = (padded[byte] << 16) | (padded[byte + 1] << 8) | padded[byte + 2]
        code = (chunk >> (24 - (bitpos & 7) - nbits)) & ((1 << nbits) - 1)
        bitpos += nbits
        if code == 256:
            del table[258:]
            nbits = 9
            prev = None
            continue
        if code == 257:
            break
        if prev is None:
            entry = table[code]
            out += entry
            prev = entry
            continue
        if code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        n = len(table)
        if n >= 511 and nbits == 9:
            nbits = 10
        elif n >= 1023 and nbits == 10:
            nbits = 11
        elif n >= 2047 and nbits == 11:
            nbits = 12
    return bytes(out)


def packbits_decode(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        h = data[i]
        i += 1
        if h < 128:
            out += data[i:i + h + 1]
            i += h + 1
        elif h > 128:
            out += bytes([data[i]]) * (257 - h)
            i += 1
    return bytes(out)


def _decompress(compression: int, raw: bytes) -> bytes:
    if compression == 1:
        return raw
    if compression in (8, 32946):
        return zlib.decompress(raw)
    if compression == 5:
        return lzw_decode(raw)
    if compression == 32773:
        return packbits_decode(raw)
    if compression == 50000:
        try:
            import zstandard  # type: ignore

            return zstandard.ZstdDecompressor().decompress(raw, max_output_size=256 * 1024 * 1024)
        except ImportError:
            try:
                import imagecodecs  # type: ignore

                return imagecodecs.zstd_decode(raw)
            except Exception as exc:
                raise DO.OnlineError("This GeoTIFF uses ZSTD compression: install 'zstandard' "
                                     "(pip install zstandard).") from exc
    if compression == 34887:
        raise DO.OnlineError("This GeoTIFF uses LERC compression, which is not supported.")
    raise DO.OnlineError(f"Unsupported TIFF compression {compression}")


class RemoteGeoTiff:
    """Minimal GeoTIFF reader that fetches only the bytes it needs.

    Supports classic and BigTIFF, tiles and strips, no / LZW / Deflate / PackBits
    / (ZSTD) compression, horizontal (2) and floating-point (3) predictors,
    GDAL scale / offset and nodata. Only the first image (full resolution) and
    the first band are read.
    """

    HEAD = 32768

    def __init__(self, url: str, head: Optional[bytes] = None) -> None:
        self.url = url
        self._buf = head if head is not None else fetch_range(url, 0, self.HEAD)
        self._buf_start = 0
        b = self._buf
        if b[:2] == b"II":
            self.e = "<"
        elif b[:2] == b"MM":
            self.e = ">"
        else:
            raise DO.OnlineError(f"Not a TIFF file: {url}")
        magic = struct.unpack(self.e + "H", b[2:4])[0]
        if magic == 42:
            self.big = False
            ifd = struct.unpack(self.e + "I", b[4:8])[0]
        elif magic == 43:
            self.big = True
            ifd = struct.unpack(self.e + "Q", b[8:16])[0]
        else:
            raise DO.OnlineError(f"Unknown TIFF magic {magic}: {url}")
        self.tags: Dict[int, Tuple[int, int, int, bytes]] = {}
        self._parse_ifd(ifd)
        self._setup()
        self._tile_cache: Dict[int, np.ndarray] = {}

    # -- byte access
    def _bytes(self, offset: int, length: int) -> bytes:
        s = offset - self._buf_start
        if 0 <= s and s + length <= len(self._buf):
            return self._buf[s:s + length]
        return fetch_range(self.url, offset, length)

    def _parse_ifd(self, offset: int) -> None:
        if self.big:
            n = struct.unpack(self.e + "Q", self._bytes(offset, 8))[0]
            esize, start = 20, offset + 8
        else:
            n = struct.unpack(self.e + "H", self._bytes(offset, 2))[0]
            esize, start = 12, offset + 2
        raw = self._bytes(start, n * esize)
        for k in range(n):
            ent = raw[k * esize:(k + 1) * esize]
            if self.big:
                tag, typ, count = struct.unpack(self.e + "HHQ", ent[:12])
                val = ent[12:20]
            else:
                tag, typ, count = struct.unpack(self.e + "HHI", ent[:8])
                val = ent[8:12]
            self.tags[tag] = (typ, count, 0, val)

    def _tag_info(self, tag: int) -> Tuple[int, int, bool, int, bytes]:
        typ, count, _, val = self.tags[tag]
        size = _TIFF_TYPES.get(typ, ("B", 1))[1] * count
        inline = size <= len(val)
        off = 0 if inline else struct.unpack(self.e + ("Q" if self.big else "I"), val)[0]
        return typ, count, inline, off, val

    def tag_values(self, tag: int) -> Optional[List[Any]]:
        if tag not in self.tags:
            return None
        typ, count, inline, off, val = self._tag_info(tag)
        fmt, size = _TIFF_TYPES.get(typ, ("B", 1))
        data = val[:size * count] if inline else self._bytes(off, size * count)
        if typ == 2:
            return [data.split(b"\x00")[0].decode("latin-1", errors="replace")]
        if typ in (5, 10):
            vals = struct.unpack(self.e + (fmt[0] * 2 * count), data)
            return [vals[i] / vals[i + 1] if vals[i + 1] else 0.0 for i in range(0, len(vals), 2)]
        return list(struct.unpack(self.e + fmt * count, data))

    def tag_element(self, tag: int, index: int) -> int:
        typ, count, inline, off, val = self._tag_info(tag)
        fmt, size = _TIFF_TYPES[typ]
        if inline:
            return struct.unpack(self.e + fmt * count, val[:size * count])[index]
        return struct.unpack(self.e + fmt, self._bytes(off + index * size, size))[0]

    def tag_elements_pair(self, tag_a: int, tag_b: int, index: int) -> Tuple[int, int]:
        """Offset and byte count of one tile/strip, fetched in one request when possible."""
        ta, ca, ia, oa, va = self._tag_info(tag_a)
        tb, cb, ib, ob, vb = self._tag_info(tag_b)
        sa, sb = _TIFF_TYPES[ta][1], _TIFF_TYPES[tb][1]
        if not ia and not ib:
            pa, pb = oa + index * sa, ob + index * sb
            lo, hi = min(pa, pb), max(pa + sa, pb + sb)
            outside = not (0 <= pa - self._buf_start and pb + sb - self._buf_start <= len(self._buf))
            if outside and hi - lo <= 2 * 1024 * 1024:
                chunk = fetch_range(self.url, lo, hi - lo)
                a = struct.unpack(self.e + _TIFF_TYPES[ta][0], chunk[pa - lo:pa - lo + sa])[0]
                b = struct.unpack(self.e + _TIFF_TYPES[tb][0], chunk[pb - lo:pb - lo + sb])[0]
                return int(a), int(b)
        return int(self.tag_element(tag_a, index)), int(self.tag_element(tag_b, index))

    # -- image description
    def _setup(self) -> None:
        tv = self.tag_values
        self.width = int(tv(256)[0])
        self.height = int(tv(257)[0])
        bps = tv(258) or [8]
        self.bits = int(bps[0])
        self.compression = int((tv(259) or [1])[0])
        self.spp = int((tv(277) or [1])[0])
        self.planar = int((tv(284) or [1])[0])
        self.predictor = int((tv(317) or [1])[0])
        sf = int((tv(339) or [1])[0])
        kind = {1: "u", 2: "i", 3: "f"}.get(sf, "u")
        self.dtype = np.dtype(f"{self.e}{kind}{self.bits // 8}")
        self.tiled = 322 in self.tags
        if self.tiled:
            self.tw = int(tv(322)[0])
            self.th = int(tv(323)[0])
            self.tiles_across = (self.width + self.tw - 1) // self.tw
            self.tiles_down = (self.height + self.th - 1) // self.th
            self._off_tag, self._cnt_tag = 324, 325
        else:
            self.tw = self.width
            self.th = int((tv(278) or [self.height])[0])
            self.th = min(self.th, self.height) if self.th > 0 else self.height
            self.tiles_across = 1
            self.tiles_down = (self.height + self.th - 1) // self.th
            self._off_tag, self._cnt_tag = 273, 279
        # georeferencing
        scale = tv(33550)
        tie = tv(33922)
        mt = tv(34264)
        if scale and tie:
            sx, sy = float(scale[0]), float(scale[1])
            i0, j0, x, y = float(tie[0]), float(tie[1]), float(tie[3]), float(tie[4])
            self.x0 = x - i0 * sx
            self.y0 = y + j0 * sy
            self.sx, self.sy = sx, sy
        elif mt:
            self.sx, self.sy = float(mt[0]), -float(mt[5])
            self.x0, self.y0 = float(mt[3]), float(mt[7])
        else:
            raise DO.OnlineError("GeoTIFF has no georeferencing")
        geokeys = tv(34735) or []
        raster_type = 1
        for k in range(4, len(geokeys) - 3, 4):
            if geokeys[k] == 1025:
                raster_type = int(geokeys[k + 3])
        if raster_type == 2:  # PixelIsPoint: tie point is a pixel centre
            self.x0 -= self.sx / 2.0
            self.y0 += self.sy / 2.0
        nod = tv(42113)
        self.nodata: Optional[float] = None
        if nod:
            try:
                self.nodata = float(str(nod[0]).strip())
            except ValueError:
                self.nodata = None
        self.scale, self.offset = 1.0, 0.0
        md = tv(42112)
        if md:
            xml = str(md[0])
            m = re.search(r'<Item[^>]*name="SCALE"[^>]*>\s*([-+0-9.eE]+)\s*<', xml)
            if m:
                self.scale = float(m.group(1))
            m = re.search(r'<Item[^>]*name="OFFSET"[^>]*>\s*([-+0-9.eE]+)\s*<', xml)
            if m:
                self.offset = float(m.group(1))
        self.metadata_xml = str(md[0]) if md else ""

    def layout_key(self) -> Tuple[Any, ...]:
        return (self.width, self.height, round(self.x0, 9), round(self.y0, 9), round(self.sx, 12), round(self.sy, 12))

    # -- geometry
    def pixel_center(self, row: int, col: int) -> Tuple[float, float]:
        return self.y0 - (row + 0.5) * self.sy, self.x0 + (col + 0.5) * self.sx

    def _lon_in_raster(self, lon: float) -> float:
        lon = float(lon)
        x1 = self.x0 + self.width * self.sx
        while lon < self.x0:
            lon += 360.0
        while lon >= x1 and lon - 360.0 >= self.x0:
            lon -= 360.0
        return lon

    def pixels_for_target(self, target: "DO.ExtractionTarget", max_pixels: int = 20000) -> List["DO.GridCell"]:
        """Pixels (as GridCell with row/col in i_lat/i_lon) and weights for a target."""
        t = target
        cells: List[DO.GridCell] = []
        if t.mode == "bbox_mean" and t.bbox:
            s, n, w, e = t.bbox
            wl, el = self._lon_in_raster(w), self._lon_in_raster(e)
            r_lo = max(0, int(math.floor((self.y0 - n) / self.sy)) - 1)
            r_hi = min(self.height - 1, int(math.ceil((self.y0 - s) / self.sy)) + 1)
            c_lo = max(0, int(math.floor((wl - self.x0) / self.sx)) - 1)
            c_hi = min(self.width - 1, int(math.ceil((el - self.x0) / self.sx)) + 1)
            rows = [r for r in range(r_lo, r_hi + 1) if s <= self.pixel_center(r, 0)[0] <= n]
            cols = [c for c in range(c_lo, c_hi + 1) if wl <= self.pixel_center(0, c)[1] <= el]
            if len(rows) * len(cols) > max_pixels:
                side_km = int(math.sqrt(max_pixels) * self.sy * 111)
                raise DO.OnlineError(f"skipped: this ~1 km data set would need {len(rows) * len(cols):,} pixels for the "
                                     f"box (maximum {max_pixels:,}, about {side_km} x {side_km} km). Use a point or a "
                                     f"smaller box; for large areas use CRU TS or TerraClimate.")
            for r in rows:
                lat_c = self.pixel_center(r, 0)[0]
                wgt = math.cos(math.radians(lat_c))
                for c in cols:
                    la, lo = self.pixel_center(r, c)
                    cells.append(DO.GridCell(la, lo, wgt, r, c))
            if not cells:  # box smaller than a pixel -> nearest
                return self.pixels_for_target(DO.ExtractionTarget(t.name, t.lat, t.lon, "nearest"))
        elif t.mode == "nearest":
            lon = self._lon_in_raster(t.lon)
            c = int(math.floor((lon - self.x0) / self.sx))
            r = int(math.floor((self.y0 - t.lat) / self.sy))
            r, c = min(max(r, 0), self.height - 1), min(max(c, 0), self.width - 1)
            la, lo = self.pixel_center(r, c)
            cells.append(DO.GridCell(la, lo, 1.0, r, c))
        else:  # bilinear on pixel centres
            lon = self._lon_in_raster(t.lon)
            fc = (lon - self.x0) / self.sx - 0.5
            fr = (self.y0 - t.lat) / self.sy - 0.5
            c0, r0 = int(math.floor(fc)), int(math.floor(fr))
            tx, ty = fc - c0, fr - r0
            raw = [(r0, c0, (1 - ty) * (1 - tx)), (r0, c0 + 1, (1 - ty) * tx),
                   (r0 + 1, c0, ty * (1 - tx)), (r0 + 1, c0 + 1, ty * tx)]
            merged: Dict[Tuple[int, int], float] = {}
            for r, c, wgt in raw:
                r = min(max(r, 0), self.height - 1)
                c = c % self.width if self.sx * self.width >= 359.9 else min(max(c, 0), self.width - 1)
                merged[(r, c)] = merged.get((r, c), 0.0) + wgt
            for (r, c), wgt in merged.items():
                if wgt > 1e-12:
                    la, lo = self.pixel_center(r, c)
                    cells.append(DO.GridCell(la, lo, wgt, r, c))
        tot = sum(x.weight for x in cells)
        if tot > 0:
            for x in cells:
                x.weight /= tot
        return cells

    # -- pixel data
    def _read_block(self, index: int) -> np.ndarray:
        if index in self._tile_cache:
            return self._tile_cache[index]
        off, cnt = self.tag_elements_pair(self._off_tag, self._cnt_tag, index)
        if cnt == 0 or off == 0:
            arr = np.full((self.th, self.tw), np.nan)
            self._tile_cache[index] = arr
            return arr
        raw = _decompress(self.compression, fetch_range(self.url, off, cnt))
        rows = self.th
        if not self.tiled:
            rows = min(self.th, self.height - index * self.th)
        nbytes = self.dtype.itemsize
        spp = self.spp if self.planar == 1 else 1
        need = rows * self.tw * spp * nbytes
        if len(raw) < need:
            raw = raw + b"\x00" * (need - len(raw))
        raw = raw[:need]
        if self.predictor == 3:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(rows, self.tw * spp * nbytes)
            b = np.cumsum(b, axis=1, dtype=np.uint8)
            b = b.reshape(rows, nbytes, self.tw * spp).transpose(0, 2, 1).copy()
            arr = b.view(np.dtype(">" + self.dtype.kind + str(nbytes))).reshape(rows, self.tw, spp)
        else:
            arr = np.frombuffer(raw, dtype=self.dtype).reshape(rows, self.tw, spp)
            if self.predictor == 2:
                arr = np.cumsum(arr, axis=1, dtype=arr.dtype)
        out = arr[:, :, 0].astype(float)
        self._tile_cache[index] = out
        return out

    def read_pixels(self, pixels: Sequence[Tuple[int, int]]) -> np.ndarray:
        """Values (scale/offset applied, nodata -> NaN) of (row, col) pixels."""
        vals = np.full(len(pixels), np.nan)
        for k, (r, c) in enumerate(pixels):
            if not (0 <= r < self.height and 0 <= c < self.width):
                continue
            if self.tiled:
                idx = (r // self.th) * self.tiles_across + (c // self.tw)
                block = self._read_block(idx)
                v = block[r % self.th, c % self.tw]
            else:
                block = self._read_block(r // self.th)
                v = block[r % self.th, c]
            if self.nodata is not None and (v == self.nodata or (np.isnan(self.nodata) and np.isnan(v))):
                continue
            vals[k] = v * self.scale + self.offset
        vals[np.abs(vals) > 1e20] = np.nan
        return vals


# ===========================================================================
# Value cache (SQLite in the session cache)
# ===========================================================================

class ValueCache:
    """Pixel values already read from remote files: (url, row, col) -> value."""

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            base = GX.session_cache_dir() if GX is not None else DO.default_cache_dir()
            path = os.path.join(base, "pixel_values.sqlite")
        self.path = path
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self._db.execute("CREATE TABLE IF NOT EXISTS v (url TEXT, r INTEGER, c INTEGER, val REAL, "
                             "PRIMARY KEY (url, r, c))")
        return self._db

    def get(self, url: str, pixels: Sequence[Tuple[int, int]]) -> Optional[np.ndarray]:
        try:
            with self._lock:
                cur = self._conn().execute("SELECT r, c, val FROM v WHERE url = ?", (url,))
                got = {(r, c): (np.nan if val is None else val) for r, c, val in cur.fetchall()}
        except sqlite3.Error:
            return None
        if not got or any(p not in got for p in pixels):
            return None
        return np.array([got[p] for p in pixels], dtype=float)

    def put(self, url: str, pixels: Sequence[Tuple[int, int]], values: Sequence[float]) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.executemany("INSERT OR REPLACE INTO v VALUES (?, ?, ?, ?)",
                               [(url, int(r), int(c), None if not np.isfinite(v) else float(v))
                                for (r, c), v in zip(pixels, values)])
                db.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None


# ===========================================================================
# Source catalogue
# ===========================================================================

@dataclass(frozen=True)
class SourceVar:
    code: str
    label: str
    units: str
    ringclim_param: str
    agg: str  # "mean" | "sum" over months / days


@dataclass(frozen=True)
class SourceInfo:
    key: str
    label: str
    freq: str                       # "monthly" | "daily"
    period: Tuple[int, int]         # typical coverage (first, last year)
    resolution: str
    cell_deg: float                 # grid spacing (0 = stations / regions)
    variables: Tuple[SourceVar, ...]
    citation: str
    url: str
    kind: str = "grid"              # "grid" | "station" | "region" | "computed"
    modes: Tuple[str, ...] = ("nearest", "bilinear", "bbox_mean")
    note: str = ""
    key_service: str = ""           # "" = no key needed; "cds" / "ewds" = personal Copernicus token


_T = "Temperature (mean)"
SOURCES: Dict[str, SourceInfo] = {}


def _src(info: SourceInfo) -> None:
    SOURCES[info.key] = info


_src(SourceInfo(
    "chelsa_monthly", "CHELSA V2.1 monthly (~1 km)", "monthly", (1979, 2021), "30 arc-seconds (~1 km)", 1.0 / 120.0,
    (SourceVar("tas", "Mean temperature", "degC", _T, "mean"),
     SourceVar("tasmax", "Maximum temperature", "degC", "Temperature (max)", "mean"),
     SourceVar("tasmin", "Minimum temperature", "degC", "Temperature (min)", "mean"),
     SourceVar("pr", "Precipitation", "mm/month", "Precipitation", "sum"),
     SourceVar("pet", "Potential evapotranspiration", "mm/month", "PET", "sum"),
     SourceVar("cmi", "Climate moisture index", "mm/month", "Climate moisture index", "sum"),
     SourceVar("hurs", "Relative humidity", "%", "Relative humidity", "mean"),
     SourceVar("vpd", "Vapour pressure deficit", "Pa", "Vapour pressure deficit", "mean"),
     SourceVar("rsds", "Shortwave radiation", "MJ m-2 d-1", "Radiation", "mean"),
     SourceVar("sfcWind", "Wind speed", "m/s", "Wind speed", "mean"),
     SourceVar("clt", "Cloud cover", "%", "Cloud cover", "mean"),
     SourceVar("spei12", "SPEI 12 months", "-", "SPEI", "mean"),
     SourceVar("spi12", "SPI 12 months", "-", "SPI", "mean")),
    "Karger, D.N. et al. (2017) Climatologies at high resolution for the earth's land surface areas. "
    "Scientific Data 4, 170122. Data: Karger et al. (2021) EnviDat, doi:10.16904/envidat.228.v2.1",
    "https://www.chelsa-climate.org/datasets/chelsa_monthly",
    note="Values are read directly from the Cloud-Optimized GeoTIFF files (only the tiles around the point)."))
_src(SourceInfo(
    "chelsa_daily", "CHELSA V2.1 daily (~1 km)", "daily", (1941, 2025), "~1 km", 1.0 / 120.0,
    (SourceVar("tas", "Mean temperature", "degC", _T, "mean"),
     SourceVar("tasmax", "Maximum temperature", "degC", "Temperature (max)", "mean"),
     SourceVar("tasmin", "Minimum temperature", "degC", "Temperature (min)", "mean"),
     SourceVar("pr", "Precipitation (GPCC bias-corrected)", "mm/day", "Precipitation", "sum"),
     SourceVar("hurs", "Relative humidity", "%", "Relative humidity", "mean"),
     SourceVar("rsds", "Shortwave radiation", "W m-2", "Radiation", "mean"),
     SourceVar("sfcWind", "Wind speed", "m/s", "Wind speed", "mean"),
     SourceVar("clt", "Cloud cover", "%", "Cloud cover", "mean"),
     SourceVar("ps", "Surface pressure", "hPa", "Sea-level pressure", "mean")),
    "Karger, D.N. et al. (2017) Scientific Data 4, 170122. Data: Karger, D.N. (2025) CHELSA-daily "
    "climate data at high resolution. EnviDat, doi:10.16904/envidat.687",
    "https://www.chelsa-climate.org/datasets/chelsa_daily",
    note="One file per day: long periods need many requests (values are cached)."))
_src(SourceInfo(
    "terraclimate", "TerraClimate monthly (~4 km)", "monthly", (1958, 2024), "1/24 deg (~4 km)", 1.0 / 24.0,
    (SourceVar("tmax", "Maximum temperature", "degC", "Temperature (max)", "mean"),
     SourceVar("tmin", "Minimum temperature", "degC", "Temperature (min)", "mean"),
     SourceVar("ppt", "Precipitation", "mm/month", "Precipitation", "sum"),
     SourceVar("pet", "Reference evapotranspiration", "mm/month", "PET", "sum"),
     SourceVar("aet", "Actual evapotranspiration", "mm/month", "Actual evapotranspiration", "sum"),
     SourceVar("def", "Climatic water deficit", "mm/month", "Climatic water deficit", "sum"),
     SourceVar("PDSI", "Palmer Drought Severity Index", "-", "PDSI", "mean"),
     SourceVar("soil", "Soil moisture", "mm", "Soil moisture", "mean"),
     SourceVar("q", "Runoff", "mm/month", "Runoff", "sum"),
     SourceVar("swe", "Snow water equivalent", "mm", "Snow", "mean"),
     SourceVar("vap", "Vapour pressure", "kPa", "Vapour pressure", "mean"),
     SourceVar("vpd", "Vapour pressure deficit", "kPa", "Vapour pressure deficit", "mean"),
     SourceVar("srad", "Shortwave radiation", "W m-2", "Radiation", "mean"),
     SourceVar("ws", "Wind speed", "m/s", "Wind speed", "mean")),
    "Abatzoglou, J.T., Dobrowski, S.Z., Parks, S.A. & Hegewisch, K.C. (2018) TerraClimate, a "
    "high-resolution global dataset of monthly climate and climatic water balance from 1958-2015. "
    "Scientific Data 5, 170191.",
    "https://www.climatologylab.org/terraclimate.html",
    note="Point values come from the THREDDS NetCDF Subset Service (no file download)."))
_src(SourceInfo(
    "gpcc", "GPCC Full Data monthly precipitation", "monthly", (1891, 2024), "0.25 / 0.5 / 1.0 / 2.5 deg", 0.5,
    (SourceVar("precip", "Precipitation", "mm/month", "Precipitation", "sum"),),
    "Schneider, U. et al. GPCC Full Data Monthly Product, Global Precipitation Climatology Centre, "
    "Deutscher Wetterdienst. doi:10.5676/DWD_GPCC",
    "https://opendata.dwd.de/climate_environment/GPCC/",
    note="Files are downloaded temporarily (5-year packages) and deleted after extraction; "
         "the number of gauges per cell is checked like the CRU station count."))
_src(SourceInfo(
    "scpdsi", "CRU scPDSI (self-calibrating PDSI)", "monthly", (1901, 2024), "0.5 deg", 0.5,
    (SourceVar("scpdsi", "Self-calibrating PDSI", "-", "PDSI", "mean"),),
    "van der Schrier, G., Barichivich, J., Briffa, K.R. & Jones, P.D. (2013) A scPDSI-based global data "
    "set of dry and wet spells for 1901-2009. J. Geophys. Res. Atmos. 118, 4025-4048. "
    "(CRU / Barichivich et al. annual updates)",
    "https://crudata.uea.ac.uk/cru/data/drought/",
    note="One global file is downloaded temporarily."))
_src(SourceInfo(
    "speibase", "SPEIbase (CSIC) 1-48 months", "monthly", (1901, 2024), "0.5 deg", 0.5,
    tuple(SourceVar(f"spei{n:02d}", f"SPEI {n} month(s)", "-", "SPEI", "mean")
          for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 18, 24, 36, 48)),
    "Begueria, S., Vicente-Serrano, S.M., Reig, F. & Latorre, B. (2014) Standardized precipitation "
    "evapotranspiration index (SPEI) revisited. Int. J. Climatol. 34, 3001-3023. SPEIbase, CSIC.",
    "https://spei.csic.es/database.html",
    note="One global file per time scale; large download. 'SPEI / SPI computed' is much faster."))
_src(SourceInfo(
    "spei_calc", "SPEI / SPI computed (TerraClimate or CHELSA P and PET)", "monthly", (1958, 2024),
    "as the base dataset", 1.0 / 24.0,
    tuple([SourceVar(f"spei{n}", f"SPEI {n} month(s)", "-", "SPEI", "mean") for n in (1, 3, 6, 9, 12, 24)]
          + [SourceVar(f"spi{n}", f"SPI {n} month(s)", "-", "SPI", "mean") for n in (1, 3, 6, 9, 12, 24)]),
    "Vicente-Serrano, S.M., Begueria, S. & Lopez-Moreno, J.I. (2010) A multiscalar drought index "
    "sensitive to global warming: the SPEI. J. Climate 23, 1696-1718. McKee, T.B. et al. (1993) SPI.",
    "https://spei.csic.es/", kind="computed",
    note="SPEI: log-logistic (generalized logistic) distribution fitted with unbiased probability-weighted "
         "moments, as in the R package SPEI; SPI: gamma distribution (maximum likelihood, Thom), "
         "with the probability of zero precipitation; parameters per calendar month over the whole period."))
_src(SourceInfo(
    "chirps_daily", "CHIRPS v3 daily precipitation (0.05 deg)", "daily", (1981, 2025), "0.05 deg", 0.05,
    (SourceVar("precip", "Precipitation", "mm/day", "Precipitation", "sum"),),
    "Funk, C. et al. (2015) The climate hazards infrared precipitation with stations - a new environmental "
    "record for monitoring extremes. Scientific Data 2, 150066. CHIRPS v3, Climate Hazards Center, UCSB.",
    "https://www.chc.ucsb.edu/data/chirps3",
    note="Land only, 60 S - 60 N. One file per day (values are cached)."))
_src(SourceInfo(
    "chirps_monthly", "CHIRPS v3 monthly precipitation (0.05 deg)", "monthly", (1981, 2025), "0.05 deg", 0.05,
    (SourceVar("precip", "Precipitation", "mm/month", "Precipitation", "sum"),),
    "Funk, C. et al. (2015) Scientific Data 2, 150066. CHIRPS v3, Climate Hazards Center, UCSB.",
    "https://www.chc.ucsb.edu/data/chirps3"))
_src(SourceInfo(
    "berkeley", "Berkeley Earth country / region temperature", "monthly", (1750, 2024), "country mean", 0.0,
    (SourceVar("TAVG", "Mean temperature", "degC", _T, "mean"),
     SourceVar("TMAX", "Maximum temperature", "degC", "Temperature (max)", "mean"),
     SourceVar("TMIN", "Minimum temperature", "degC", "Temperature (min)", "mean")),
    "Rohde, R.A. & Hausfather, Z. (2020) The Berkeley Earth Land/Ocean Temperature Record. "
    "Earth Syst. Sci. Data 12, 3469-3479.",
    "https://berkeleyearth.org/data/", kind="region", modes=("region",),
    note="Absolute temperature = monthly anomaly + 1951-1980 monthly climatology of the region."))
_src(SourceInfo(
    "ghcnd", "GHCN-Daily weather stations (NOAA)", "daily", (1763, 2025), "stations", 0.0,
    (SourceVar("TMAX", "Maximum temperature", "degC", "Temperature (max)", "mean"),
     SourceVar("TMIN", "Minimum temperature", "degC", "Temperature (min)", "mean"),
     SourceVar("TAVG", "Mean temperature", "degC", _T, "mean"),
     SourceVar("PRCP", "Precipitation", "mm/day", "Precipitation", "sum"),
     SourceVar("SNWD", "Snow depth", "mm", "Snow", "mean"),
     SourceVar("SNOW", "Snowfall", "mm/day", "Snow", "sum")),
    "Menne, M.J. et al. (2012) An overview of the Global Historical Climatology Network-Daily database. "
    "J. Atmos. Oceanic Technol. 29, 897-910. doi:10.7289/V5D21VHZ",
    "https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily",
    kind="station", modes=("station",),
    note="Values flagged by NOAA quality control are removed. Monthly values need >= 90% (precipitation) "
         "or >= 20 days (temperature) of valid data."))
_src(SourceInfo(
    "openmeteo", "ERA5 / ERA5-Land via Open-Meteo (daily, point)", "daily", (1940, 2025), "0.25 / 0.1 deg", 0.25,
    (SourceVar("temperature_2m_mean", "Mean temperature", "degC", _T, "mean"),
     SourceVar("temperature_2m_max", "Maximum temperature", "degC", "Temperature (max)", "mean"),
     SourceVar("temperature_2m_min", "Minimum temperature", "degC", "Temperature (min)", "mean"),
     SourceVar("precipitation_sum", "Precipitation", "mm/day", "Precipitation", "sum"),
     SourceVar("snowfall_sum", "Snowfall", "cm/day", "Snow", "sum"),
     SourceVar("et0_fao_evapotranspiration", "Reference evapotranspiration (FAO)", "mm/day", "PET", "sum"),
     SourceVar("relative_humidity_2m_mean", "Relative humidity", "%", "Relative humidity", "mean"),
     SourceVar("vapour_pressure_deficit_max", "Max vapour pressure deficit", "kPa", "Vapour pressure deficit", "mean"),
     SourceVar("shortwave_radiation_sum", "Shortwave radiation", "MJ m-2", "Radiation", "sum"),
     SourceVar("wind_speed_10m_mean", "Wind speed", "km/h", "Wind speed", "mean"),
     SourceVar("soil_moisture_0_to_100cm_mean", "Soil moisture 0-100 cm", "m3/m3", "Soil moisture", "mean")),
    "Hersbach, H. et al. (2020) The ERA5 global reanalysis. Q. J. R. Meteorol. Soc. 146, 1999-2049. "
    "Data served by Open-Meteo.com (CC BY 4.0), Zippenfenig, P. (2023) Open-Meteo.com Weather API. "
    "doi:10.5281/zenodo.7970649",
    "https://open-meteo.com/en/docs/historical-weather-api", modes=("nearest",),
    note="Free for non-commercial use (10 000 requests per day). Attribution required (CC BY 4.0)."))

_ERA5_CIT = ("Hersbach, H. et al. (2020) The ERA5 global reanalysis. Q. J. R. Meteorol. Soc. 146, 1999-2049. "
             "Hersbach, H. et al. (2023) ERA5 monthly averaged data on single levels from 1940 to present. "
             "Copernicus Climate Change Service (C3S) Climate Data Store (CDS), doi:10.24381/cds.f17050d7")
_ERA5L_CIT = ("Munoz-Sabater, J. et al. (2021) ERA5-Land: a state-of-the-art global reanalysis dataset for land "
              "applications. Earth Syst. Sci. Data 13, 4349-4383. Munoz Sabater, J. (2019) ERA5-Land monthly "
              "averaged data from 1950 to present. C3S Climate Data Store (CDS), doi:10.24381/cds.68d2bb30")


def _cds_vars(land: bool) -> Tuple[SourceVar, ...]:
    out = [SourceVar("t2m", "Mean temperature (2 m)", "degC", _T, "mean"),
           SourceVar("tp", "Precipitation", "mm/month", "Precipitation", "sum"),
           SourceVar("pev", "Potential evaporation", "mm/month", "PET", "sum"),
           SourceVar("e", "Evaporation (actual)", "mm/month", "Actual evapotranspiration", "sum"),
           SourceVar("vpd", "Vapour pressure deficit (from monthly T and dew point)", "kPa",
                     "Vapour pressure deficit", "mean"),
           SourceVar("rh", "Relative humidity (from monthly T and dew point)", "%", "Relative humidity", "mean"),
           SourceVar("swvl1", "Soil water 0-7 cm", "m3/m3", "Soil moisture", "mean"),
           SourceVar("swvl2", "Soil water 7-28 cm", "m3/m3", "Soil moisture", "mean"),
           SourceVar("swvl3", "Soil water 28-100 cm", "m3/m3", "Soil moisture", "mean"),
           SourceVar("sd", "Snow water equivalent", "mm", "Snow", "mean"),
           SourceVar("ro", "Runoff", "mm/month", "Runoff", "sum"),
           SourceVar("ssrd", "Solar radiation (downward)", "MJ m-2 d-1", "Radiation", "mean")]
    if not land:
        out.append(SourceVar("si10", "Wind speed (10 m)", "m/s", "Wind speed", "mean"))
    return tuple(out)


_src(SourceInfo(
    "era5_cds_monthly", "ERA5 monthly (Copernicus CDS, 0.25 deg) - needs key", "monthly", (1940, 2025), "0.25 deg",
    0.25, _cds_vars(False), _ERA5_CIT,
    "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels-monthly-means",
    note="Official monthly means from the Copernicus Climate Data Store with your own free key. Requests wait in "
         "the CDS queue (usually a few minutes); results are cached.", key_service="cds"))
_src(SourceInfo(
    "era5land_cds_monthly", "ERA5-Land monthly (Copernicus CDS, 0.1 deg) - needs key", "monthly", (1950, 2025),
    "0.1 deg (land)", 0.1, _cds_vars(True), _ERA5L_CIT,
    "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-monthly-means",
    note="Land only. Official monthly means from the Copernicus Climate Data Store with your own free key; "
         "requests wait in the CDS queue; results are cached.", key_service="cds"))
_src(SourceInfo(
    "glofas", "GloFAS v4 river discharge (Copernicus EWDS, 0.05 deg, daily) - needs key", "daily", (1979, 2025),
    "0.05 deg (~5 km)", 0.05,
    (SourceVar("dis24", "River discharge", "m3/s", "River discharge", "mean"),),
    "Harrigan, S. et al. (2020) GloFAS-ERA5 operational global river discharge reanalysis 1979-present. "
    "Earth Syst. Sci. Data 12, 2043-2060. CEMS (2019) River discharge and related historical data from the Global "
    "Flood Awareness System. Copernicus Emergency Management Service, Early Warning Data Store (EWDS), "
    "doi:10.24381/cds.a4fdd6b9",
    "https://ewds.climate.copernicus.eu/datasets/cems-glofas-historical",
    modes=("nearest", "bbox_mean"),
    note="Modelled daily river discharge. The river cell is the cell with the largest mean discharge near the point "
         "(or inside the box = outlet). One request per year (EWDS queue); results are cached.",
    key_service="ewds"))

GRDC_PORTAL_URL = "https://portal.grdc.bafg.de/"


def source_var(source: str, code: str) -> SourceVar:
    for v in SOURCES[source].variables:
        if v.code == code:
            return v
    return SourceVar(code, code, "", "Other", "mean")


# ===========================================================================
# Date parsing of file names
# ===========================================================================

def _strip_version(name: str) -> str:
    name = re.sub(r"[Vv]\.?\d{1,2}(?:\.\d{1,2})+(?!\d)", "", name)
    name = re.sub(r"(?<![A-Za-z])[Vv]\d{1,2}(?:\.\d{1,2})*(?!\d)", "", name)
    return name


def parse_file_dates(names: Sequence[str], freq: str, ignore: Sequence[str] = ()) -> List[Optional[Tuple[int, int, int]]]:
    """(year, month, day) for each file name; day = 1 for monthly files.

    Handles YYYYMMDD, YYYYMM, YYYY_MM_DD, YYYY.MM.DD, MM_YYYY, DD_MM_YYYY ...
    Numbers that do not change between files (e.g. '12' in 'spei12') are
    ignored; the day is the varying number that goes above 12.
    """
    toks: List[Tuple[Optional[int], List[int], Optional[Tuple[int, int, int]]]] = []
    for nm in names:
        base = _strip_version(os.path.basename(nm))
        base = re.sub(r"\.(tif|tiff|nc|gz|zip)$", "", base, flags=re.I)
        for word in ignore:
            if word:
                base = base.replace(word, "_")
        nums = re.findall(r"\d+", base)
        full = None
        year = None
        small: List[int] = []
        for t in nums:
            if len(t) == 8 and 1700 <= int(t[:4]) <= 2200 and 1 <= int(t[4:6]) <= 12:
                full = (int(t[:4]), int(t[4:6]), int(t[6:8]))
            elif len(t) == 6 and 1700 <= int(t[:4]) <= 2200 and 1 <= int(t[4:]) <= 12 and year is None:
                year = int(t[:4])
                small.append(int(t[4:]))
            elif len(t) == 4 and 1700 <= int(t) <= 2200 and year is None:
                year = int(t)
            elif 1 <= len(t) <= 2:
                small.append(int(t))
        toks.append((year, small, full))
    npos = max((len(s) for _, s, _ in toks), default=0)
    distinct: List[set] = [set() for _ in range(npos)]
    for _, s, _ in toks:
        for i, v in enumerate(s):
            distinct[i].add(v)
    varying = [i for i in range(npos) if len(distinct[i]) > 1]
    if not varying:
        varying = list(range(npos))[-2:] if freq == "daily" else list(range(npos))[-1:]
    day_pos = next((i for i in varying if max(distinct[i]) > 12), None) if freq == "daily" else None
    month_pos = next((i for i in varying if i != day_pos and max(distinct[i]) <= 12), None)
    if freq == "daily" and day_pos is None and len(varying) >= 2:
        month_pos, day_pos = varying[0], varying[1]
    out: List[Optional[Tuple[int, int, int]]] = []
    for year, small, full in toks:
        if full:
            out.append(full)
            continue
        if year is None or month_pos is None or month_pos >= len(small):
            out.append(None)
            continue
        m = small[month_pos]
        d = 1
        if freq == "daily":
            if day_pos is None or day_pos >= len(small):
                out.append(None)
                continue
            d = small[day_pos]
        out.append((year, m, d) if 1 <= m <= 12 and 1 <= d <= 31 else None)
    return out


# ===========================================================================
# CHELSA (S3 bucket, COG)
# ===========================================================================

CHELSA_ENDPOINT = "https://os.unil.cloud.switch.ch"
CHELSA_BUCKET = "chelsa02"
CHELSA_PREFIX = {"monthly": "chelsa/global/monthly/", "daily": "chelsa/global/daily/"}


def _year_of_prefix(p: str) -> Optional[int]:
    last = p.rstrip("/").rsplit("/", 1)[-1]
    return int(last) if re.fullmatch(r"(1[7-9]|2[0-2])\d\d", last) else None


def chelsa_list_files(var: str, freq: str, y0: int, y1: int,
                      progress: ProgressCallback = _noop, cancel: CancelCheck = _never
                      ) -> List[Tuple[_dt.date, str]]:
    """[(date, url)] of CHELSA COG files for one variable in [y0, y1]."""
    root = CHELSA_PREFIX[freq] + var + "/"
    found: List[str] = []
    stack = [(root, 0)]
    while stack:
        if cancel():
            raise DO.CancelledError("Cancelled")
        prefix, depth = stack.pop()
        progress("list", 0, 0, f"Listing CHELSA {prefix} ...")
        keys, prefs = s3_list(CHELSA_ENDPOINT, CHELSA_BUCKET, prefix)
        found += [k for k, _ in keys if k.lower().endswith((".tif", ".tiff"))]
        if depth >= 3:
            continue
        for p in prefs:
            yr = _year_of_prefix(p)
            if yr is not None and not (y0 <= yr <= y1):
                continue
            stack.append((p, depth + 1))
    if not found:
        raise DO.OnlineError(f"No CHELSA {freq} files found for '{var}' under {root}")
    dates = parse_file_dates(found, freq, ignore=(var,))
    out: List[Tuple[_dt.date, str]] = []
    for key, d in zip(found, dates):
        if d is None:
            continue
        try:
            dd = _dt.date(d[0], d[1], d[2])
        except ValueError:
            continue
        if y0 <= dd.year <= y1:
            out.append((dd, f"{CHELSA_ENDPOINT}/{CHELSA_BUCKET}/{urllib.parse.quote(key)}"))
    out.sort()
    # one file per date (prefer the shortest name: no '_extra' variants)
    uniq: Dict[_dt.date, str] = {}
    for d, u in out:
        if d not in uniq or len(u) < len(uniq[d]):
            uniq[d] = u
    return sorted(uniq.items())


# ===========================================================================
# CHIRPS v3 (HTTP directory, GeoTIFF)
# ===========================================================================

CHIRPS_BASE = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/"


_CRAWL_ERRORS: List[str] = []
CHC_BLOCKED_HINT = ("The CHIRPS server (Climate Hazards Center, UCSB) refused access (HTTP 403). It temporarily "
                    "blocks computers that send many requests in a short time; try again in a few hours. "
                    "Months already read are kept in the cache and are not requested again.")


def _crawl_tifs(url: str, depth: int, year_range: Tuple[int, int], cancel: CancelCheck,
                progress: ProgressCallback) -> List[str]:
    if cancel():
        raise DO.CancelledError("Cancelled")
    progress("list", 0, 0, f"Listing {url} ...")
    try:
        links = list_http_dir(url)
    except DO.OnlineError as exc:
        _CRAWL_ERRORS.append(str(exc))
        return []
    tifs = [u for u in links if u.lower().endswith((".tif", ".tiff"))]
    if depth <= 0:
        return tifs
    for u in links:
        if not u.endswith("/"):
            continue
        last = u.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"\d{4}", last) and not (year_range[0] <= int(last) <= year_range[1]):
            continue
        if last.lower() in ("prelim", "sat", "africa", "camer-carib", "whem", "p25", "netcdf", "cogs_legacy",
                            "bils", "pngs", "jpgs"):
            continue
        tifs += _crawl_tifs(u, depth - 1, year_range, cancel, progress)
    return tifs


def chirps_list_files(freq: str, y0: int, y1: int, progress: ProgressCallback = _noop,
                      cancel: CancelCheck = _never) -> List[Tuple[_dt.date, str]]:
    if freq == "daily":
        roots = [CHIRPS_BASE + "daily/final/rnl/", CHIRPS_BASE + "daily/final/sat/"]
    else:
        roots = [CHIRPS_BASE + "monthly/global/tifs/", CHIRPS_BASE + "monthly/global/", CHIRPS_BASE + "monthly/"]
    del _CRAWL_ERRORS[:]
    for root in roots:
        tifs = _crawl_tifs(root, 2, (y0, y1), cancel, progress)
        if tifs:
            break
    else:
        first = _CRAWL_ERRORS[0] if _CRAWL_ERRORS else ""
        if "HTTP 403" in first:
            raise DO.OnlineError(CHC_BLOCKED_HINT + f" ({first})")
        why = f" Server answer: {first}" if first else ""  # the main folder's answer
        raise DO.OnlineError("No CHIRPS v3 GeoTIFF files found (server busy or the folder layout changed)." + why)
    dates = parse_file_dates(tifs, freq)
    uniq: Dict[_dt.date, str] = {}
    for u, d in zip(tifs, dates):
        if d is None:
            continue
        try:
            dd = _dt.date(d[0], d[1], d[2] if freq == "daily" else 1)
        except ValueError:
            continue
        if y0 <= dd.year <= y1 and (dd not in uniq or len(u) < len(uniq[dd])):
            uniq[dd] = u
    return sorted(uniq.items())


# ===========================================================================
# Series extraction from many remote GeoTIFF files
# ===========================================================================

def _convert_units(source: str, var: str, values: np.ndarray) -> np.ndarray:
    v = values.astype(float)
    if source.startswith("chelsa") and var in ("tas", "tasmax", "tasmin"):
        fin = v[np.isfinite(v)]
        if fin.size and np.nanmedian(fin) > 150.0:  # Kelvin
            v = v - 273.15
    return v


def extract_geotiff_series(
    source: str,
    var: str,
    files: Sequence[Tuple[_dt.date, str]],
    targets: Sequence["DO.ExtractionTarget"],
    progress: ProgressCallback = _noop,
    cancel: CancelCheck = _never,
    workers: int = 12,
    cache: Optional[ValueCache] = None,
) -> Dict[str, Tuple[pd.DataFrame, List["DO.GridCell"]]]:
    """{target: (DataFrame[Date, value], cells)} from one GeoTIFF per time step."""
    if not files:
        raise DO.OnlineError("No files for the requested period.")
    progress("open", 0, 0, f"Opening {files[0][1].rsplit('/', 1)[-1]} ...")
    first = RemoteGeoTiff(files[0][1])
    layout = first.layout_key()
    cells_by: Dict[str, List[DO.GridCell]] = {t.name: first.pixels_for_target(t) for t in targets}
    pixels = sorted({(c.i_lat, c.i_lon) for cells in cells_by.values() for c in cells})
    pix_index = {p: k for k, p in enumerate(pixels)}
    head_size = RemoteGeoTiff.HEAD
    values = np.full((len(files), len(pixels)), np.nan)
    errors: List[str] = []
    lock = threading.Lock()
    done = [0]

    def work(k: int) -> None:
        if cancel():
            return
        d, url = files[k]
        got = cache.get(url, pixels) if cache is not None else None
        if got is None:
            last_exc: Optional[Exception] = None
            for attempt in range(3):  # transient server errors, a header longer than the first read
                if cancel():
                    return
                try:
                    if k == 0 and attempt == 0:
                        tif = first
                    elif attempt == 0:
                        tif = RemoteGeoTiff(url, head=fetch_range(url, 0, head_size))
                    else:
                        tif = RemoteGeoTiff(url)
                    if tif.layout_key() == layout:
                        got = tif.read_pixels(pixels)
                    else:  # other extent / tiling (e.g. a newer file version): same places, new rows/cols
                        got = tif.read_pixels([_same_place(first, tif, r, c) for r, c in pixels])
                    if cache is not None:
                        cache.put(url, pixels, got)
                    last_exc = None
                    break
                except DO.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    time.sleep(1.0 + 2.0 * attempt)
            if last_exc is not None:
                with lock:
                    errors.append(f"{url.rsplit('/', 1)[-1]}: {last_exc}")
                got = np.full(len(pixels), np.nan)
        values[k, :] = got
        with lock:
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(files):
                progress("read", done[0], len(files), f"{SOURCES[source].label} {var}: {done[0]}/{len(files)} files")

    with _cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, k) for k in range(len(files))]
        for f in futures:
            while True:
                try:
                    f.result(timeout=0.5)
                    break
                except _cf.TimeoutError:
                    if cancel():
                        for g in futures:
                            g.cancel()
                        raise DO.CancelledError("Cancelled")
    if cancel():
        raise DO.CancelledError("Cancelled")
    if errors and len(errors) == len(files):
        raise DO.OnlineError("All files failed. First error: " + errors[0])
    values = _convert_units(source, var, values)
    dates = pd.to_datetime([d for d, _ in files])
    out: Dict[str, Tuple[pd.DataFrame, List[DO.GridCell]]] = {}
    for t in targets:
        cells = cells_by[t.name]
        cols = [pix_index[(c.i_lat, c.i_lon)] for c in cells]
        sub = values[:, cols]
        w = np.array([c.weight for c in cells])
        valid = np.isfinite(sub)
        wm = np.where(valid, w[None, :], 0.0)
        ws = wm.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            series = np.where(ws > 0, np.nansum(np.where(valid, sub, 0.0) * wm, axis=1) / ws, np.nan)
        for j, c in enumerate(cells):
            c.valid = bool(valid[:, j].any())
        out[t.name] = (pd.DataFrame({"Date": dates, "value": series}), cells)
    out["__cell_size__"] = (abs(first.sy), abs(first.sx))  # type: ignore[assignment]
    if errors:
        msg = f"{len(errors)} of {len(files)} file(s) could not be read, e.g. {errors[0]}"
        if source.startswith("chirps") and any("HTTP 403" in e for e in errors):
            msg += ". " + CHC_BLOCKED_HINT
        progress("warn", 0, 0, msg)
        out["__errors__"] = msg  # type: ignore[assignment]
    return out


def _same_place(a: "RemoteGeoTiff", b: "RemoteGeoTiff", row: int, col: int) -> Tuple[int, int]:
    """(row, col) in raster b of the centre of pixel (row, col) of raster a (-1 if outside b)."""
    lat, lon = a.pixel_center(row, col)
    r = int(math.floor((b.y0 - lat) / b.sy))
    c = int(math.floor((b._lon_in_raster(lon) - b.x0) / b.sx))
    if not (0 <= r < b.height and 0 <= c < b.width):
        return -1, -1
    return r, c


# ===========================================================================
# TerraClimate (THREDDS NCSS)
# ===========================================================================

TERRACLIMATE_SERVERS = [
    "https://tds-proxy.nkn.uidaho.edu/thredds",
    "http://thredds.northwestknowledge.net:8080/thredds",
    "https://www.reacchpna.org/thredds",
]
_TC_RESOLVED: Dict[str, str] = {}


def _terraclimate_names(var: str) -> List[str]:
    return [f"agg_terraclimate_{var}_1950_CurrentYear_GLOBE.nc", f"agg_terraclimate_{var}_1950-CurrentYear_GLOBE.nc",
            f"agg_terraclimate_{var}_1958_CurrentYear_GLOBE.nc", f"agg_terraclimate_{var}_1958-CurrentYear_GLOBE.nc"]


def _parse_ncss_csv(text: str, var: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or "," not in lines[0]:
        raise DO.OnlineError("Unexpected NCSS answer: " + text[:200])
    import csv as _csv

    rows = list(_csv.reader(lines))
    header = [h.strip().strip('"') for h in rows[0]]
    vcol = next((i for i, h in enumerate(header) if h.split("[")[0].strip().lower() == var.lower()), len(header) - 1)
    tcol = next((i for i, h in enumerate(header) if h.lower().startswith(("time", "date"))), 0)
    dates, vals = [], []
    for r in rows[1:]:
        if len(r) <= max(vcol, tcol):
            continue
        try:
            dates.append(pd.Timestamp(r[tcol].strip().replace("Z", "")))
            v = float(r[vcol])
            vals.append(v if math.isfinite(v) and abs(v) < 1e20 else np.nan)
        except (ValueError, TypeError):
            continue
    return pd.DataFrame({"Date": dates, "value": vals})


_TC_MEMO: Dict[Tuple[Any, ...], Any] = {}
_TC_MEMO_LOCK = threading.Lock()


def terraclimate_point(var: str, lat: float, lon: float, y0: int, y1: int, timeout: float = 120.0) -> pd.DataFrame:
    """Monthly TerraClimate series of the nearest cell (memoised: SPEI re-uses the P / PET already read)."""
    key = ("pt", var, round(lat, 5), round(lon, 5), y0, y1)
    with _TC_MEMO_LOCK:
        if key in _TC_MEMO:
            return _TC_MEMO[key].copy()
    df = _terraclimate_point(var, lat, lon, y0, y1, timeout)
    with _TC_MEMO_LOCK:
        _TC_MEMO[key] = df.copy()
    return df


def _terraclimate_point(var: str, lat: float, lon: float, y0: int, y1: int, timeout: float = 120.0) -> pd.DataFrame:
    q = urllib.parse.urlencode({
        "var": var, "latitude": f"{lat:.5f}", "longitude": f"{DO._wrap_lon(lon):.5f}",
        "time_start": f"{y0}-01-01T00:00:00Z", "time_end": f"{y1}-12-31T23:59:59Z", "accept": "csv",
    })
    tried: List[str] = []
    cand: List[str] = []
    if var in _TC_RESOLVED:
        cand.append(_TC_RESOLVED[var])
    for srv in TERRACLIMATE_SERVERS:
        for name in _terraclimate_names(var):
            for path in ("/ncss/grid/", "/ncss/"):
                cand.append(srv + path + name)
    last: Optional[BaseException] = None
    for base in cand:
        url = f"{base}?{q}"
        tried.append(base)
        try:
            text = http_text(url, timeout=timeout)
            df = _parse_ncss_csv(text, var)
            if not df.empty:
                _TC_RESOLVED[var] = base
                return df
        except Exception as exc:
            last = exc
            continue
    raise DO.OnlineError(f"TerraClimate '{var}' is not reachable on any known THREDDS server ({last}).")


def terraclimate_box(var: str, bbox: Tuple[float, float, float, float], y0: int, y1: int,
                     timeout: float = 300.0) -> Tuple[pd.DataFrame, List["DO.GridCell"]]:
    """Area mean (cos-lat weights) from an NCSS NetCDF subset (memoised)."""
    key = ("box", var, tuple(round(x, 5) for x in bbox), y0, y1)
    with _TC_MEMO_LOCK:
        if key in _TC_MEMO:
            df, cells = _TC_MEMO[key]
            return df.copy(), list(cells)
    df, cells = _terraclimate_box(var, bbox, y0, y1, timeout)
    with _TC_MEMO_LOCK:
        _TC_MEMO[key] = (df.copy(), list(cells))
    return df, cells


def _terraclimate_box(var: str, bbox: Tuple[float, float, float, float], y0: int, y1: int,
                      timeout: float = 300.0) -> Tuple[pd.DataFrame, List["DO.GridCell"]]:
    s, n, w, e = bbox
    q = urllib.parse.urlencode({
        "var": var, "north": n, "south": s, "west": w, "east": e, "horizStride": 1,
        "time_start": f"{y0}-01-01T00:00:00Z", "time_end": f"{y1}-12-31T23:59:59Z", "accept": "netcdf",
    })
    last: Optional[BaseException] = None
    cand = ([_TC_RESOLVED[var]] if var in _TC_RESOLVED else []) + [
        srv + path + name for srv in TERRACLIMATE_SERVERS for name in _terraclimate_names(var)
        for path in ("/ncss/grid/", "/ncss/")]
    for base in cand:
        try:
            data = DO.http_get_bytes(f"{base}?{q}", timeout=timeout, retries=1)
            if not data[:4] in (b"CDF\x01", b"CDF\x02", b"\x89HDF"):
                raise DO.OnlineError("not a NetCDF answer")
            _TC_RESOLVED[var] = base
            return _box_mean_from_nc_bytes(data, var, bbox)
        except Exception as exc:
            last = exc
    raise DO.OnlineError(f"TerraClimate area subset failed ({last}).")


def _box_mean_from_nc_bytes(data: bytes, var: str, bbox) -> Tuple[pd.DataFrame, List["DO.GridCell"]]:
    from netCDF4 import Dataset

    with DO.NETCDF_LOCK, Dataset("inmemory.nc", memory=data) as nc:
        names = list(nc.variables.keys())
        lat_n = DO._find_coord_name(names, ("lat", "latitude"))
        lon_n = DO._find_coord_name(names, ("lon", "longitude"))
        v_n = DO._find_coord_name(names, (var.lower(),)) or [n for n in names if len(nc.variables[n].dimensions) == 3][0]
        lats = np.asarray(nc.variables[lat_n][:], float)
        lons = np.asarray(nc.variables[lon_n][:], float)
        tv = nc.variables["time"]
        years, months = _nc_time_year_month(tv)
        arr = np.ma.filled(np.ma.asarray(nc.variables[v_n][:], dtype=float), np.nan)
    arr[np.abs(arr) > 1e20] = np.nan
    w = np.cos(np.radians(lats))[None, :, None] * np.ones((1, 1, len(lons)))
    valid = np.isfinite(arr)
    num = np.nansum(np.where(valid, arr, 0) * w, axis=(1, 2))
    den = np.sum(np.where(valid, w, 0), axis=(1, 2))
    with np.errstate(invalid="ignore", divide="ignore"):
        series = np.where(den > 0, num / den, np.nan)
    cells: List[DO.GridCell] = []
    tot = float(np.cos(np.radians(lats)).sum() * len(lons)) or 1.0
    anyvalid = valid.any(axis=0)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            cells.append(DO.GridCell(float(la), float(lo), math.cos(math.radians(la)) / tot, i, j,
                                     bool(anyvalid[i, j])))
    dates = pd.to_datetime([f"{y:04d}-{m:02d}-01" for y, m in zip(years, months)])
    return pd.DataFrame({"Date": dates, "value": series}), cells


def _nc_time_year_month(time_var) -> Tuple[np.ndarray, np.ndarray]:
    units = str(getattr(time_var, "units", ""))
    m = re.match(r"\s*months since\s+(\d{1,4})-(\d{1,2})", units)
    if m:
        y0, m0 = int(m.group(1)), int(m.group(2))
        k = np.floor(np.asarray(time_var[:], float) + 1e-6).astype(int)
        tot = (y0 * 12 + m0 - 1) + k
        return tot // 12, tot % 12 + 1
    return DO._nc_time_to_year_month(time_var)


def terraclimate_cell(lat: float, lon: float) -> "DO.GridCell":
    res = 1.0 / 24.0
    i = math.floor((90.0 - lat) / res)
    j = math.floor((DO._wrap_lon(lon) + 180.0) / res)
    return DO.GridCell(90.0 - (i + 0.5) * res, -180.0 + (j + 0.5) * res, 1.0, i, j)


# ===========================================================================
# Remote NetCDF files (GPCC, scPDSI, SPEIbase): download -> extract -> delete
# ===========================================================================

@dataclass
class RemoteFile:
    url: str
    filename: str
    first_year: int
    last_year: int
    size_bytes: Optional[int] = None


def _years_in_name(name: str) -> List[int]:
    base = re.sub(r"[Vv]\d{4}", "", name)
    out = []
    for t in re.findall(r"(?<!\d)(\d{4})(\d{2})?(?!\d)", base):
        y = int(t[0])
        if 1850 <= y <= 2200:
            out.append(y)
    return out


GPCC_ROOT = "https://opendata.dwd.de/climate_environment/GPCC/GPCC_Precipitation_Analysis_Monthly/"
GPCC_RES = {"0.25": "025_degree", "0.5": "050_degree", "1.0": "100_degree", "2.5": "250_degree"}


def gpcc_list_files(resolution: str, y0: int, y1: int) -> Tuple[str, List[RemoteFile]]:
    versions = [u for u in list_http_dir(GPCC_ROOT) if re.search(r"Version_(\d{4})/?$", u)]
    if not versions:
        raise DO.OnlineError("GPCC: no Version_* folder found at " + GPCC_ROOT)
    versions.sort(key=lambda u: int(re.search(r"Version_(\d{4})", u).group(1)))
    vurl = versions[-1]
    version = re.search(r"Version_(\d{4})", vurl).group(1)
    sub = GPCC_RES.get(resolution, "050_degree")
    rurl = vurl.rstrip("/") + "/" + sub + "/"
    files: List[RemoteFile] = []
    for u in list_http_dir(rurl):
        name = u.rsplit("/", 1)[-1]
        if not re.search(r"\.nc(\.gz)?$", name, flags=re.I):
            continue
        ys = _years_in_name(name)
        if not ys:
            continue
        a, b = min(ys), max(ys)
        if b < y0 or a > y1:
            continue
        files.append(RemoteFile(u, name, a, b))
    if not files:
        raise DO.OnlineError(f"GPCC {version}: no files for {y0}-{y1} at {rurl}")
    files.sort(key=lambda f: (f.first_year, f.last_year))
    # drop single-month files when a package covers the same year
    packaged = {y for f in files if f.last_year > f.first_year for y in range(f.first_year, f.last_year + 1)}
    files = [f for f in files if f.last_year > f.first_year or f.first_year not in packaged]
    return f"GPCC {version} ({resolution} deg)", files


SCPDSI_URL = "https://crudata.uea.ac.uk/cru/data/drought/"


def scpdsi_find_file() -> Tuple[str, RemoteFile]:
    links = list_http_dir(SCPDSI_URL)
    nc = [u for u in links if re.search(r"scpdsi.*\.nc(\.gz)?$", u, flags=re.I)]
    for u in [x for x in links if x.endswith("/")][:20]:
        if not nc and re.search(r"scpdsi|pdsi", u, flags=re.I):
            try:
                nc += [x for x in list_http_dir(u) if re.search(r"scpdsi.*\.nc(\.gz)?$", x, flags=re.I)]
            except DO.OnlineError:
                pass
    if not nc:
        raise DO.OnlineError("CRU scPDSI: no NetCDF file found at " + SCPDSI_URL)

    def key(u: str) -> Tuple[Any, ...]:
        v = re.search(r"cru_ts(\d+)\.(\d+)", u)
        ys = _years_in_name(u.rsplit("/", 1)[-1])
        return (int(v.group(1)) if v else 0, int(v.group(2)) if v else 0, max(ys) if ys else 0,
                0 if u.endswith(".gz") else 1)

    nc.sort(key=key)
    best = nc[-1]
    name = best.rsplit("/", 1)[-1]
    ys = _years_in_name(name)
    v = re.search(r"cru_ts(\d+\.\d+)", name)
    label = f"CRU scPDSI (CRU TS {v.group(1)})" if v else "CRU scPDSI"
    return label, RemoteFile(best, name, min(ys) if ys else 1901, max(ys) if ys else 2100)


SPEIBASE_PAGES = ["https://spei.csic.es/spei_database_2_11", "https://spei.csic.es/spei_database",
                  "https://spei.csic.es/spei_database_2_10", "https://spei.csic.es/database.html"]


def speibase_find_file(code: str) -> Tuple[str, RemoteFile]:
    n = int(re.sub(r"\D", "", code) or 12)
    patterns = [rf"spei0?{n}\.nc(\.gz)?$", rf"spei_?0?{n}\.nc(\.gz)?$"]
    links: List[str] = []
    for page in SPEIBASE_PAGES:
        try:
            text = http_text(page, timeout=40)
        except Exception:
            continue
        for href in DO._links_from_html(text) + re.findall(r'["\']([^"\']+\.nc(?:\.gz)?)["\']', text):
            links.append(urllib.parse.urljoin(page.rstrip("/") + "/", href))
    for u in links:
        name = u.rsplit("/", 1)[-1].lower()
        if any(re.search(p, name) for p in patterns):
            return "SPEIbase", RemoteFile(u, u.rsplit("/", 1)[-1], 1901, 2100)
    for base in ("https://spei.csic.es/spei_database_2_11/nc/", "https://spei.csic.es/spei_database/nc/",
                 "https://spei.csic.es/spei_database_2_10/nc/"):
        for name in (f"spei{n:02d}.nc", f"spei{n}.nc"):
            if DO.http_head_size(base + name) is not None:
                return "SPEIbase", RemoteFile(base + name, name, 1901, 2100)
    raise DO.OnlineError(f"SPEIbase file for SPEI-{n} was not found online. "
                         "Use 'SPEI / SPI computed' instead, or download the file from spei.csic.es "
                         "and import it as a local NetCDF file (Large gridded data...).")


def run_netcdf_files(
    files: Sequence[RemoteFile],
    var_hint: str,
    targets: Sequence["DO.ExtractionTarget"],
    y0: int,
    y1: int,
    cache_dir: Optional[str],
    keep: bool,
    progress: ProgressCallback,
    cancel: CancelCheck,
    stn_names: Sequence[str] = ("stn", "numgauge", "num_gauge", "gauges"),
    log: Optional[List[str]] = None,
) -> Dict[str, Tuple[pd.DataFrame, List["DO.GridCell"]]]:
    log = log if log is not None else []
    dl_dir = os.path.join(cache_dir or DO.default_cache_dir(), "grid_downloads")
    os.makedirs(dl_dir, exist_ok=True)
    parts: Dict[str, List[pd.DataFrame]] = {t.name: [] for t in targets}
    cells_by: Dict[str, List[DO.GridCell]] = {}
    for idx, f in enumerate(files, start=1):
        if cancel():
            raise DO.CancelledError("Cancelled")
        progress("file", idx - 1, len(files), f"[{idx}/{len(files)}] {f.filename}")
        path = os.path.join(dl_dir, f.filename)
        work_dir: Optional[str] = None
        try:
            DO.download_file(f.url, path, progress=progress, cancel=cancel, stage="download")
            work_dir = DO._ascii_work_dir()
            if f.filename.lower().endswith(".gz"):
                nc_path = os.path.join(work_dir, f.filename[:-3])
                progress("decompress", 0, 0, f"Decompressing {f.filename} ...")
                DO._decompress_gz(path, nc_path, cancel=cancel)
            elif DO._is_ascii(os.path.abspath(path)):
                nc_path = path
            else:  # netCDF4 on Windows cannot open non-ASCII paths
                import shutil as _sh

                nc_path = os.path.join(work_dir, f.filename)
                _sh.copyfile(path, nc_path)
            progress("extract", 0, 0, f"Extracting {len(targets)} target(s) from {f.filename} ...")
            ex = extract_netcdf_targets(nc_path, var_hint, targets, cancel=cancel, stn_names=stn_names)
            for name, (df, cells) in ex.items():
                df = df[(df["Year"] >= y0) & (df["Year"] <= y1)]
                parts[name].append(df)
                cells_by[name] = cells
            log.append(f"OK {f.filename}")
        finally:
            if work_dir:
                import shutil as _sh

                _sh.rmtree(work_dir, ignore_errors=True)
            if not keep and os.path.exists(path):
                try:
                    os.remove(path)
                    log.append(f"Deleted temporary file {f.filename}")
                except OSError as exc:
                    log.append(f"Could not delete {f.filename}: {exc}")
        progress("file", idx, len(files), f"[{idx}/{len(files)}] done")
    out = {}
    for t in targets:
        if not parts[t.name]:
            continue
        df = pd.concat(parts[t.name], ignore_index=True)
        df = df.drop_duplicates(subset=["Year", "Month"], keep="last").sort_values(["Year", "Month"])
        out[t.name] = (df.reset_index(drop=True), cells_by.get(t.name, []))
    return out


def extract_netcdf_targets(*args: Any, **kwargs: Any) -> Dict[str, Tuple[pd.DataFrame, List["DO.GridCell"]]]:
    """Thread-safe wrapper (netCDF4 / HDF5 reads one file at a time)."""
    with DO.NETCDF_LOCK:
        return _extract_netcdf_targets(*args, **kwargs)


def _extract_netcdf_targets(nc_path: str, var_hint: str, targets: Sequence["DO.ExtractionTarget"],
                           cancel: CancelCheck = _never,
                           stn_names: Sequence[str] = ("stn",)) -> Dict[str, Tuple[pd.DataFrame, List["DO.GridCell"]]]:
    """Like dendro_online.extract_targets_from_netcdf, with any station-count variable
    name and 'months since' time units."""
    from netCDF4 import Dataset

    out: Dict[str, Tuple[pd.DataFrame, List[DO.GridCell]]] = {}
    with Dataset(nc_path, "r") as nc:
        names = list(nc.variables.keys())
        lat_n = DO._find_coord_name(names, ("lat", "latitude", "y"))
        lon_n = DO._find_coord_name(names, ("lon", "longitude", "x"))
        if lat_n is None or lon_n is None:
            raise DO.OnlineError("Latitude/longitude variables not found in NetCDF")
        stn_n = DO._find_coord_name(names, tuple(s.lower() for s in stn_names))
        var_n = DO._find_coord_name(names, (var_hint.lower(),))
        if var_n is None:
            for nme in names:
                if len(nc.variables[nme].dimensions) == 3 and nme != stn_n:
                    var_n = nme
                    break
        if var_n is None:
            raise DO.OnlineError(f"No data variable in {os.path.basename(nc_path)}")
        lats = np.asarray(nc.variables[lat_n][:], float)
        lons = np.asarray(nc.variables[lon_n][:], float)
        lat_desc = lats[0] > lats[-1]
        lats_asc = lats[::-1] if lat_desc else lats
        lw = np.array([DO._wrap_lon(x) for x in lons])
        order = np.argsort(lw)
        lons_asc = lw[order]
        tname = DO._find_coord_name(names, ("time",)) or "time"
        years, months = _nc_time_year_month(nc.variables[tname])
        dv = nc.variables[var_n]
        dv.set_auto_maskandscale(True)
        dims = [d.lower() for d in dv.dimensions]
        # (time, lat, lon) or (lat, lon, time) or (time, lon, lat)
        la_ax = next((i for i, d in enumerate(dims) if "lat" in d or d == "y"), 1)
        lo_ax = next((i for i, d in enumerate(dims) if "lon" in d or d == "x"), 2)
        sv = nc.variables[stn_n] if stn_n else None

        def column(var, i, j):
            idx: List[Any] = [slice(None)] * 3
            idx[la_ax], idx[lo_ax] = i, j
            col = var[tuple(idx)]
            return np.ma.filled(np.ma.asarray(col, dtype=float), np.nan)

        for t in targets:
            if cancel():
                raise DO.CancelledError("Cancelled")
            cells = DO.cells_for_target(t, lats_asc, lons_asc)
            if not cells:
                raise DO.OnlineError(f"No grid cells for target '{t.name}'")
            for c in cells:
                c.i_lat = (len(lats) - 1 - c.i_lat) if lat_desc else c.i_lat
                c.i_lon = int(order[c.i_lon])
            n_t = len(years)
            vals = np.full((n_t, len(cells)), np.nan)
            stns = np.full((n_t, len(cells)), np.nan)
            for k, c in enumerate(cells):
                vals[:, k] = column(dv, c.i_lat, c.i_lon)
                if sv is not None:
                    stns[:, k] = column(sv, c.i_lat, c.i_lon)
            vals[np.abs(vals) > 1e20] = np.nan
            w = np.array([c.weight for c in cells])
            valid = np.isfinite(vals)
            wm = np.where(valid, w[None, :], 0.0)
            ws = wm.sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                series = np.where(ws > 0, np.nansum(np.where(valid, vals, 0) * wm, axis=1) / ws, np.nan)
                stn_series = (np.where(ws > 0, np.nansum(np.where(np.isfinite(stns) & valid, stns, 0) * wm, axis=1) / ws,
                                       np.nan) if sv is not None else np.full(n_t, np.nan))
            for k, c in enumerate(cells):
                c.valid = bool(valid[:, k].any())
            out[t.name] = (pd.DataFrame({"Year": years, "Month": months, "value": series, "stn": stn_series}), cells)
    return out


# ===========================================================================
# Berkeley Earth regional series
# ===========================================================================

BERKELEY_URLS = [
    "https://berkeley-earth-temperature.s3.us-west-1.amazonaws.com/Regional/{v}/{name}-{v}-Trend.txt",
    "https://berkeleyearth.lbl.gov/auto/Regional/{v}/Text/{name}-{v}-Trend.txt",
    "http://berkeleyearth.lbl.gov/auto/Regional/{v}/Text/{name}-{v}-Trend.txt",
]
_BERKELEY_NAME_FIX = {"united states of america": "united-states", "russian federation": "russia",
                      "dem. rep. congo": "congo-(democratic-republic-of-the)", "czechia": "czech-republic",
                      "united kingdom": "united-kingdom", "bosnia and herz.": "bosnia-and-herzegovina",
                      "kyrgyz republic": "kyrgyzstan", "eswatini": "swaziland", "macedonia": "macedonia",
                      "north macedonia": "macedonia", "iran (islamic republic of)": "iran"}


def berkeley_region_name(country_name: str) -> str:
    n = country_name.strip().lower()
    n = _BERKELEY_NAME_FIX.get(n, n)
    return re.sub(r"\s+", "-", n)


def parse_berkeley_trend(text: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    clim: Optional[List[float]] = None
    lines = text.splitlines()
    for k, ln in enumerate(lines):
        if ln.startswith("%") and re.search(r"\bJan\b.*\bFeb\b.*\bDec\b", ln):
            for nxt in lines[k + 1:k + 4]:
                nums = re.findall(r"[-+]?\d+\.\d+|NaN", nxt)
                if len(nums) >= 12 and not nxt.lstrip("% ").startswith("+/-"):
                    clim = [float(x) for x in nums[:12]]
                    break
            if clim:
                break
    rows = []
    for ln in lines:
        if ln.startswith("%") or not ln.strip():
            continue
        parts = ln.split()
        if len(parts) < 3:
            continue
        try:
            y, m = int(parts[0]), int(parts[1])
            a = float(parts[2])
        except ValueError:
            continue
        rows.append((y, m, a))
    if not rows:
        raise DO.OnlineError("No data rows in Berkeley Earth file")
    df = pd.DataFrame(rows, columns=["Year", "Month", "anomaly"])
    if clim is not None:
        df["value"] = df["anomaly"] + df["Month"].map(lambda m: clim[m - 1])
    else:
        df["value"] = df["anomaly"]
    meta = {"climatology_1951_1980": clim, "values": "absolute" if clim else "anomaly"}
    return df[["Year", "Month", "value"]], meta


def berkeley_series(region: str, var: str) -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    name = berkeley_region_name(region)
    last: Optional[BaseException] = None
    for tmpl in BERKELEY_URLS:
        url = tmpl.format(v=var, name=name)
        try:
            text = http_text(url, timeout=60)
            df, meta = parse_berkeley_trend(text)
            return df, meta, url
        except Exception as exc:
            last = exc
    raise DO.OnlineError(f"Berkeley Earth: region '{name}' / {var} not found ({last}).")


# ===========================================================================
# GHCN-Daily stations
# ===========================================================================

GHCND_BASE = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/"


@dataclass
class Station:
    station_id: str
    name: str
    lat: float
    lon: float
    elevation_m: Optional[float]
    country: str = ""
    elements: Dict[str, Tuple[int, int]] = field(default_factory=dict)  # element -> (first, last year)
    dist_km: Optional[float] = None


def _ghcnd_cached(name: str, progress: ProgressCallback, cancel: CancelCheck, max_age_days: float = 30.0) -> str:
    base = GX.session_cache_dir() if GX is not None else DO.default_cache_dir()
    path = os.path.join(base, "ghcnd", name)
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age_days * 86400:
        return path
    if os.path.exists(path):
        os.remove(path)
    return DO.download_file(GHCND_BASE + name, path, progress=progress, cancel=cancel, stage="download")


def ghcnd_station_list(progress: ProgressCallback = _noop, cancel: CancelCheck = _never) -> Dict[str, Station]:
    sp = _ghcnd_cached("ghcnd-stations.txt", progress, cancel)
    ip = _ghcnd_cached("ghcnd-inventory.txt", progress, cancel)
    st: Dict[str, Station] = {}
    with open(sp, "r", encoding="latin-1") as fh:
        for ln in fh:
            if len(ln) < 40:
                continue
            sid = ln[0:11].strip()
            try:
                lat, lon = float(ln[12:20]), float(ln[21:30])
                elev = float(ln[31:37])
            except ValueError:
                continue
            st[sid] = Station(sid, ln[41:71].strip(), lat, lon, None if elev <= -999 else elev, sid[:2])
    with open(ip, "r", encoding="latin-1") as fh:
        for ln in fh:
            sid = ln[0:11].strip()
            s = st.get(sid)
            if s is None:
                continue
            try:
                s.elements[ln[31:35].strip()] = (int(ln[36:40]), int(ln[41:45]))
            except ValueError:
                pass
    return st


def ghcnd_search(lat: float, lon: float, radius_km: float, elements: Sequence[str], y0: int, y1: int,
                 min_years: int = 10, progress: ProgressCallback = _noop, cancel: CancelCheck = _never,
                 limit: int = 200) -> List[Station]:
    stations = ghcnd_station_list(progress, cancel)
    a = DO.bbox_around(lat, lon, radius_km)
    out: List[Station] = []
    for s in stations.values():
        if not (a[0] <= s.lat <= a[1]) or not (a[2] <= DO._wrap_lon(s.lon) <= a[3] or a[2] > a[3]):
            continue
        d = DO.haversine_km(lat, lon, s.lat, s.lon)
        if d > radius_km:
            continue
        ok = False
        for el in elements:
            if el in s.elements:
                f, l = s.elements[el]
                if min(l, y1) - max(f, y0) + 1 >= min_years:
                    ok = True
        if ok:
            s.dist_km = round(d, 1)
            out.append(s)
    out.sort(key=lambda s: s.dist_km or 0)
    return out[:limit]


def ghcnd_station_data(station_id: str, elements: Sequence[str], y0: int, y1: int,
                       progress: ProgressCallback = _noop, cancel: CancelCheck = _never,
                       keep_dir: Optional[str] = None) -> Tuple[Dict[str, pd.DataFrame], str]:
    url = f"{GHCND_BASE}by_station/{station_id}.csv.gz"
    base = keep_dir or os.path.join(GX.session_cache_dir() if GX is not None else DO.default_cache_dir(), "ghcnd")
    path = os.path.join(base, f"{station_id}.csv.gz")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) > 7 * 86400:
        os.remove(path)
    DO.download_file(url, path, progress=progress, cancel=cancel, stage="download")
    df = pd.read_csv(path, header=None, compression="gzip", dtype={0: str, 1: str, 2: str, 3: float, 4: str, 5: str},
                     names=["ID", "DATE", "ELEMENT", "VALUE", "MFLAG", "QFLAG", "SFLAG", "OBSTIME"],
                     usecols=[0, 1, 2, 3, 4, 5], low_memory=False)
    df = df[df["ELEMENT"].isin(list(elements))]
    df = df[df["QFLAG"].isna() | (df["QFLAG"].astype(str).str.strip() == "")]
    df["Date"] = pd.to_datetime(df["DATE"], format="%Y%m%d", errors="coerce")
    df = df[df["Date"].notna() & (df["Date"].dt.year >= y0) & (df["Date"].dt.year <= y1)]
    out: Dict[str, pd.DataFrame] = {}
    for el in elements:
        sub = df[df["ELEMENT"] == el]
        if sub.empty:
            continue
        v = sub["VALUE"].astype(float)
        if el in ("TMAX", "TMIN", "TAVG", "PRCP"):
            v = v / 10.0
        out[el] = pd.DataFrame({"Date": sub["Date"].values, "value": v.values}).sort_values("Date").reset_index(drop=True)
    return out, path


def daily_to_monthly(df: pd.DataFrame, agg: str, min_days: int = 20, min_frac_sum: float = 0.9) -> pd.DataFrame:
    """Daily (Date, value) -> monthly long (Year, Month, value) with completeness rules."""
    if df.empty:
        return pd.DataFrame(columns=["Year", "Month", "value"])
    d = df.dropna(subset=["value"]).copy()
    d["Year"] = d["Date"].dt.year
    d["Month"] = d["Date"].dt.month
    g = d.groupby(["Year", "Month"])["value"]
    res = g.agg(["count", "sum", "mean"]).reset_index()
    res["ndays"] = [calendar.monthrange(int(y), int(m))[1] for y, m in zip(res["Year"], res["Month"])]
    if agg == "sum":
        ok = res["count"] >= np.ceil(min_frac_sum * res["ndays"])
        res["value"] = np.where(ok, res["sum"] * res["ndays"] / res["count"].clip(lower=1), np.nan)
    else:
        ok = res["count"] >= np.minimum(min_days, res["ndays"])
        res["value"] = np.where(ok, res["mean"], np.nan)
    return res[["Year", "Month", "value"]]


# ===========================================================================
# Open-Meteo (ERA5 / ERA5-Land)
# ===========================================================================

OPENMETEO_URL = "https://archive-api.open-meteo.com/v1/archive"
OPENMETEO_MODELS = {"era5": ("ERA5 (0.25 deg, 1940-)", 0.25), "era5_land": ("ERA5-Land (0.1 deg, 1950-)", 0.1),
                    "era5_seamless": ("ERA5 seamless (best of ERA5 / ERA5-Land)", 0.1)}


def openmeteo_daily(lat: float, lon: float, y0: int, y1: int, variables: Sequence[str], model: str = "era5",
                    progress: ProgressCallback = _noop, cancel: CancelCheck = _never) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
    today = _dt.date.today()
    end_all = min(_dt.date(y1, 12, 31), today - _dt.timedelta(days=6))
    chunks: List[Tuple[_dt.date, _dt.date]] = []
    a = _dt.date(y0, 1, 1)
    while a <= end_all:
        b = min(_dt.date(a.year + 9, 12, 31), end_all)
        chunks.append((a, b))
        a = _dt.date(b.year + 1, 1, 1)
    frames: Dict[str, List[pd.DataFrame]] = {v: [] for v in variables}
    meta: Dict[str, Any] = {}
    for k, (a, b) in enumerate(chunks, start=1):
        if cancel():
            raise DO.CancelledError("Cancelled")
        progress("read", k - 1, len(chunks), f"Open-Meteo {model}: {a.year}-{b.year}")
        q = urllib.parse.urlencode({
            "latitude": f"{lat:.4f}", "longitude": f"{DO._wrap_lon(lon):.4f}",
            "start_date": a.isoformat(), "end_date": b.isoformat(),
            "daily": ",".join(variables), "models": model, "timezone": "GMT",
        })
        for attempt in range(4):
            try:
                data = DO.http_get_json(f"{OPENMETEO_URL}?{q}", timeout=180, retries=1)
                break
            except DO.OnlineError as exc:
                if "429" in str(exc) and attempt < 3:
                    progress("wait", 0, 0, "Open-Meteo rate limit: waiting 60 s ...")
                    for _ in range(60):
                        if cancel():
                            raise DO.CancelledError("Cancelled")
                        time.sleep(1)
                    continue
                raise
        if isinstance(data, dict) and data.get("error"):
            raise DO.OnlineError(f"Open-Meteo: {data.get('reason', data)}")
        daily = data.get("daily", {})
        dates = pd.to_datetime(daily.get("time", []))
        for v in variables:
            vals = daily.get(v)
            if vals is None:
                continue
            frames[v].append(pd.DataFrame({"Date": dates, "value": pd.to_numeric(pd.Series(vals), errors="coerce")}))
        meta = {"grid_lat": data.get("latitude"), "grid_lon": data.get("longitude"),
                "elevation_m": data.get("elevation"), "units": data.get("daily_units", {})}
    progress("read", len(chunks), len(chunks), "Open-Meteo done")
    out = {v: pd.concat(fr, ignore_index=True) for v, fr in frames.items() if fr}
    return out, meta


# ===========================================================================
# SPEI / SPI
# ===========================================================================

def _gammaln(x: float) -> float:
    return math.lgamma(x)


def _gammainc_lower_reg(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) (Numerical Recipes)."""
    if x <= 0:
        return 0.0
    if x < a + 1.0:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-12:
                break
        return s * math.exp(-x + a * math.log(x) - _gammaln(a))
    b = x + 1.0 - a
    c = 1.0 / 1e-300
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-12:
            break
    return 1.0 - math.exp(-x + a * math.log(x) - _gammaln(a)) * h


_ND = statistics.NormalDist()


def _norm_ppf(p: float) -> float:
    p = min(max(p, 1e-10), 1 - 1e-10)
    return _ND.inv_cdf(p)


def _ub_pwm(x: np.ndarray) -> Tuple[float, float, float]:
    """Unbiased probability-weighted moments b0, b1, b2 (x ascending)."""
    n = len(x)
    j = np.arange(1, n + 1, dtype=float)
    b0 = x.mean()
    b1 = np.sum((j - 1) / (n - 1) * x) / n
    b2 = np.sum((j - 1) * (j - 2) / ((n - 1) * (n - 2)) * x) / n
    return b0, b1, b2


def _glo_fit(x: np.ndarray) -> Optional[Tuple[float, float, float]]:
    """Generalized logistic (xi, alpha, k) from unbiased PWM / L-moments (Hosking 1986).

    This is what the R package SPEI uses for its 'log-Logistic' distribution
    (lmomco::pwm.ub -> pwm2lmom -> parglo); unlike the 3-parameter log-logistic
    formulas it also works for negatively skewed samples.
    """
    x = np.sort(x[np.isfinite(x)])
    if len(x) < 10:
        return None
    b0, b1, b2 = _ub_pwm(x)
    l1, l2, l3 = b0, 2 * b1 - b0, 6 * b2 - 6 * b1 + b0
    if l2 <= 0:
        return None
    k = -l3 / l2
    if abs(k) >= 1.0:
        return None
    if abs(k) < 1e-6:
        return l1, l2, 0.0
    kp = k * math.pi
    alpha = l2 * math.sin(kp) / kp
    xi = l1 - alpha * (1.0 / k - math.pi / math.sin(kp))
    return xi, alpha, k


def _glo_cdf(x: float, xi: float, alpha: float, k: float) -> float:
    if k == 0.0:
        y = (x - xi) / alpha
    else:
        arg = 1.0 - k * (x - xi) / alpha
        if arg <= 0:
            return 1.0 if k > 0 else 0.0
        y = -math.log(arg) / k
    return 1.0 / (1.0 + math.exp(-y)) if y > -700 else 0.0


def _gamma_fit(x: np.ndarray) -> Optional[Tuple[float, float, float]]:
    """(shape, scale, probability of zero) by Thom's maximum-likelihood approximation."""
    x = x[np.isfinite(x)]
    if len(x) < 10:
        return None
    q = float(np.mean(x <= 0))
    pos = x[x > 0]
    if len(pos) < 5:
        return None
    mean = pos.mean()
    A = math.log(mean) - np.mean(np.log(pos))
    if A <= 0:
        return None
    shape = (1 + math.sqrt(1 + 4 * A / 3)) / (4 * A)
    return shape, mean / shape, q


def _rolling_sum(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if k <= 1:
        return x.astype(float)
    for i in range(k - 1, len(x)):
        w = x[i - k + 1:i + 1]
        if np.all(np.isfinite(w)):
            out[i] = w.sum()
    return out


def standardized_index(monthly: pd.DataFrame, scale: int, kind: str = "spei",
                       ref: Optional[Tuple[int, int]] = None) -> pd.DataFrame:
    """SPEI (column 'water_balance' = P - PET) or SPI (column 'value' = P) for one scale.

    Distribution parameters are fitted per calendar month over the reference
    period (default: whole series), as in the R package SPEI.
    """
    df = monthly.sort_values(["Year", "Month"]).reset_index(drop=True)
    col = "water_balance" if kind == "spei" else "value"
    acc = _rolling_sum(df[col].to_numpy(float), scale)
    res = np.full(len(df), np.nan)
    months = df["Month"].to_numpy()
    years = df["Year"].to_numpy()
    for m in range(1, 13):
        idx = np.where(months == m)[0]
        fit_idx = idx if ref is None else idx[(years[idx] >= ref[0]) & (years[idx] <= ref[1])]
        sample = acc[fit_idx]
        sample = sample[np.isfinite(sample)]
        if kind == "spei":
            par = _glo_fit(sample)
            if par is None:
                continue
            for i in idx:
                if np.isfinite(acc[i]):
                    res[i] = _norm_ppf(_glo_cdf(acc[i], *par))
        else:
            par = _gamma_fit(sample)
            if par is None:
                continue
            shape, sc, q = par
            for i in idx:
                if np.isfinite(acc[i]):
                    p = q + (1 - q) * (_gammainc_lower_reg(shape, acc[i] / sc) if acc[i] > 0 else 0.0)
                    res[i] = _norm_ppf(p)
    return pd.DataFrame({"Year": years, "Month": months, "value": res})


# ===========================================================================
# High-level request
# ===========================================================================

@dataclass
class SourceRequest:
    source: str
    variables: List[str]
    targets: List["DO.ExtractionTarget"]
    start_year: int
    end_year: int
    options: Dict[str, Any] = field(default_factory=dict)
    cache_dir: Optional[str] = None
    keep_downloads: bool = False


@dataclass
class VarResult:
    data: pd.DataFrame        # monthly: Year, Month, value[, stn]; daily: Date, value
    freq: str
    cells: List["DO.GridCell"]
    cell_size: Optional[Tuple[float, float]]
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceResult:
    request: SourceRequest
    label: str
    results: Dict[str, Dict[str, VarResult]]     # results[target][var]
    stations: List[Station] = field(default_factory=list)
    log: List[str] = field(default_factory=list)

    def provenance(self, target: str, var: str) -> Dict[str, Any]:
        info = SOURCES[self.request.source]
        r = self.results[target][var]
        v = source_var(self.request.source, var)
        t = next((x for x in self.request.targets if x.name == target), None)
        return {
            "source": info.label, "dataset": self.label, "variable": var, "variable_label": v.label,
            "units": v.units, "frequency": r.freq, "resolution": info.resolution,
            "target": dataclasses.asdict(t) if t is not None else {"name": target},
            "cells": [dataclasses.asdict(c) for c in r.cells[:500]],
            "n_cells": len(r.cells),
            "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "citation": info.citation, "url": info.url, "note": info.note, **r.meta,
        }


def _monthly_from_daily_dates(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["Year"] = d["Date"].dt.year
    d["Month"] = d["Date"].dt.month
    return d[["Year", "Month", "value"]]


def run_source_request(req: SourceRequest, progress: ProgressCallback = _noop,
                       cancel: CancelCheck = _never) -> SourceResult:
    """Cached wrapper: an identical request is read from the disk cache (also by another running copy)."""
    key = [req.source, sorted(req.variables), DO.targets_key(req.targets), int(req.start_year), int(req.end_year),
           {k: v for k, v in sorted((req.options or {}).items()) if isinstance(v, (str, int, float, bool, type(None)))}]
    if hasattr(DO, "series_cache_get"):
        hit = DO.series_cache_get("src", key)
        if isinstance(hit, SourceResult):
            progress("cache", 1, 1, f"{SOURCES[req.source].label if req.source in SOURCES else req.source}: "
                                    "values from the cache (no download)")
            hit.log = list(hit.log) + ["values from the disk cache"]
            return hit
    res = _run_source_request_uncached(req, progress, cancel)
    complete = all(set(req.variables) <= set(res.results.get(t.name, {}).keys()) for t in req.targets) \
        if req.source not in ("ghcnd", "berkeley") else any(res.results.get(t.name) for t in req.targets)
    if hasattr(DO, "series_cache_put") and complete and "__errors__" not in str(res.log):
        DO.series_cache_put("src", key, res)
    return res


def _run_source_request_uncached(req: SourceRequest, progress: ProgressCallback = _noop,
                                 cancel: CancelCheck = _never) -> SourceResult:
    src = req.source
    if src not in SOURCES:
        raise ValueError(f"Unknown source {src}")
    info = SOURCES[src]
    if req.start_year > req.end_year:
        raise ValueError("Start year must not be after end year")
    if not req.variables:
        raise ValueError("Select at least one variable")
    for t in req.targets:
        t.validate()
    y0, y1 = req.start_year, req.end_year
    results: Dict[str, Dict[str, VarResult]] = {t.name: {} for t in req.targets}
    log: List[str] = []
    label = info.label
    cell = (info.cell_deg, info.cell_deg) if info.cell_deg else None

    if info.key_service:  # Copernicus CDS / EWDS with the user's own key
        import dendro_copernicus as DC

        results, cell, label, extra_log = DC.run_request(req, progress, cancel)
        log += extra_log

    elif src in ("chelsa_monthly", "chelsa_daily", "chirps_daily", "chirps_monthly"):
        cache = ValueCache()
        try:
            for var in req.variables:
                if src.startswith("chelsa"):
                    files = chelsa_list_files(var, info.freq, y0, y1, progress, cancel)
                else:
                    files = chirps_list_files(info.freq, y0, y1, progress, cancel)
                if not files:
                    raise DO.OnlineError(f"{info.label}: no files for {var} in {y0}-{y1}")
                log.append(f"{info.label} {var}: {len(files)} file(s) {files[0][0]} .. {files[-1][0]}")
                workers = int(req.options.get("workers", 12))
                if src.startswith("chirps"):  # the CHC server refuses many parallel connections
                    workers = min(workers, int(req.options.get("chirps_workers", 2)))
                ex = extract_geotiff_series(src, var, files, req.targets, progress, cancel,
                                            workers=workers, cache=cache)
                cell = ex.pop("__cell_size__", cell)  # type: ignore[arg-type]
                read_errors = ex.pop("__errors__", None)
                if read_errors:
                    log.append(f"{info.label} {var}: {read_errors}")
                for tname, (df, cells) in ex.items():
                    if info.freq == "monthly":
                        data = _monthly_from_daily_dates(df)
                    else:
                        data = df
                    vmeta = {"n_files": len(files), "first_file": files[0][1]}
                    if read_errors:
                        vmeta["warning"] = f"{var}: {read_errors}"
                    results[tname][var] = VarResult(data, info.freq, cells, cell, vmeta)
        finally:
            cache.close()

    elif src == "terraclimate":
        # one THREDDS request per variable and point; several run at the same time (the server answers slowly)
        jobs = [(var, t) for var in req.variables for t in req.targets]
        done_n = [0]
        lock = threading.Lock()

        def tc_job(var: str, t: "DO.ExtractionTarget"):
            if cancel():
                raise DO.CancelledError("Cancelled")
            if t.mode == "bbox_mean" and t.bbox:
                df, cells = terraclimate_box(var, t.bbox, y0, y1)
            else:
                df = terraclimate_point(var, t.lat, t.lon, y0, y1)
                cells = [terraclimate_cell(t.lat, t.lon)]
                cells[0].valid = bool(df["value"].notna().any())
            with lock:
                done_n[0] += 1
                progress("read", done_n[0], len(jobs), f"TerraClimate: {done_n[0]}/{len(jobs)} ({var} {t.name})")
            return var, t, df, cells

        progress("read", 0, len(jobs), f"TerraClimate: {len(jobs)} request(s) ...")
        n_par = max(1, min(int(req.options.get("tc_parallel", 4)), len(jobs)))
        errors_tc: List[str] = []
        with _cf.ThreadPoolExecutor(max_workers=n_par) as ex:
            futs = [ex.submit(tc_job, var, t) for var, t in jobs]
            for f in futs:
                try:
                    var, t, df, cells = f.result()
                except DO.CancelledError:
                    for g in futs:
                        g.cancel()
                    raise
                except Exception as exc:
                    errors_tc.append(str(exc))
                    continue
                results[t.name][var] = VarResult(_monthly_from_daily_dates(df), "monthly", cells, cell,
                                                 {"server": _TC_RESOLVED.get(var, "")})
        if errors_tc:
            if not any(results[t.name] for t in req.targets):
                raise DO.OnlineError(errors_tc[0])
            log.append(f"TerraClimate: {len(errors_tc)} request(s) failed, e.g. {errors_tc[0]}")
        if "nearest" not in [t.mode for t in req.targets] and any(t.mode == "bilinear" for t in req.targets):
            log.append("TerraClimate point values are taken from the nearest 1/24 deg cell.")

    elif src in ("gpcc", "scpdsi", "speibase"):
        for var in req.variables:
            if src == "gpcc":
                res = str(req.options.get("gpcc_resolution", "0.5"))
                label, files = gpcc_list_files(res, y0, y1)
                cell = (float(res), float(res))
                hint = "precip"
            elif src == "scpdsi":
                label, f = scpdsi_find_file()
                files = [f]
                hint = "scpdsi"
            else:
                label, f = speibase_find_file(var)
                files = [f]
                hint = "spei"
            ex = run_netcdf_files(files, hint, req.targets, y0, y1, req.cache_dir, req.keep_downloads,
                                  progress, cancel, log=log)
            for tname, (df, cells) in ex.items():
                meta: Dict[str, Any] = {"files": [x.url for x in files]}
                if src == "gpcc" and df["stn"].notna().any():
                    meta["mean_gauges"] = float(df["stn"].mean())
                    weak = df.groupby("Year")["stn"].mean()
                    weak = weak[weak < 1.0]
                    if len(weak):
                        meta["warning"] = (f"{var}: fewer than 1 gauge per cell in {len(weak)} year(s) "
                                           f"({int(weak.index.min())}-{int(weak.index.max())}).")
                results[tname][var] = VarResult(df, "monthly", cells, cell, meta)

    elif src == "spei_calc":
        base = str(req.options.get("base", "terraclimate"))
        if base == "chelsa_monthly":
            base_req = SourceRequest("chelsa_monthly", ["pr", "pet"], req.targets, y0, y1, dict(req.options),
                                     req.cache_dir)
        else:
            base_req = SourceRequest("terraclimate", ["ppt", "pet"], req.targets, y0, y1, dict(req.options),
                                     req.cache_dir)
        progress("plan", 0, 0, f"Reading precipitation and PET from {SOURCES[base_req.source].label} ...")
        base_res = run_source_request(base_req, progress, cancel)
        label = f"SPEI/SPI from {SOURCES[base_req.source].label}"
        pv, ev = base_req.variables
        for t in req.targets:
            r_p = base_res.results[t.name].get(pv)
            r_e = base_res.results[t.name].get(ev)
            if r_p is None:
                continue
            m = r_p.data.rename(columns={"value": "P"}).merge(
                r_e.data.rename(columns={"value": "PET"}) if r_e is not None else
                pd.DataFrame(columns=["Year", "Month", "PET"]), on=["Year", "Month"], how="left")
            m = m.groupby(["Year", "Month"], as_index=False).mean(numeric_only=True)
            m["water_balance"] = m["P"] - m["PET"]
            m["value"] = m["P"]
            for var in req.variables:
                kind = "spei" if var.startswith("spei") else "spi"
                scale = int(re.sub(r"\D", "", var) or 1)
                idx = standardized_index(m, scale, kind)
                results[t.name][var] = VarResult(idx, "monthly", r_p.cells, r_p.cell_size,
                                                 {"base_dataset": SOURCES[base_req.source].label, "scale_months": scale,
                                                  "method": SOURCES["spei_calc"].note})
        cell = None

    elif src == "berkeley":
        region = str(req.options.get("region", "")).strip()
        for t in req.targets:
            reg = region
            if not reg:
                c = DO.find_country(t.lat, t.lon)
                reg = c["name"] if c else ""
            if not reg:
                raise DO.OnlineError("Berkeley Earth: no country found for the point; type a region name.")
            for var in req.variables:
                if cancel():
                    raise DO.CancelledError("Cancelled")
                progress("read", 0, 0, f"Berkeley Earth {var}: {reg}")
                df, meta, url = berkeley_series(reg, var)
                df = df[(df["Year"] >= y0) & (df["Year"] <= y1)]
                meta.update({"region": reg, "file_url": url})
                results[t.name][var] = VarResult(df.reset_index(drop=True), "monthly", [], None, meta)
            log.append(f"Berkeley Earth region: {reg}")

    elif src == "ghcnd":
        stations: List[Station] = list(req.options.get("stations") or [])
        if not stations:
            raise DO.OnlineError("Select one or more stations first (Find stations).")
        results = {}
        new_targets: List[DO.ExtractionTarget] = []
        for k, s in enumerate(stations, start=1):
            if cancel():
                raise DO.CancelledError("Cancelled")
            progress("file", k - 1, len(stations), f"GHCN-Daily {s.station_id} {s.name}")
            data, path = ghcnd_station_data(s.station_id, req.variables, y0, y1, progress, cancel,
                                            keep_dir=req.options.get("station_dir"))
            name = f"{s.station_id} {s.name}".strip()
            new_targets.append(DO.ExtractionTarget(name, s.lat, s.lon, "nearest"))
            results[name] = {}
            for el, df in data.items():
                results[name][el] = VarResult(df, "daily", [], None,
                                              {"station_id": s.station_id, "station_name": s.name,
                                               "elevation_m": s.elevation_m, "local_path": path,
                                               "dist_km": s.dist_km})
            if GX is not None:
                yrs = [df["Date"].dt.year for df in data.values() if len(df)]
                GX.registry.add_station("GHCN-Daily", s.station_id, s.name, s.lat, s.lon, s.elevation_m,
                                        list(data.keys()),
                                        int(min(y.min() for y in yrs)) if yrs else None,
                                        int(max(y.max() for y in yrs)) if yrs else None, path,
                                        {"dist_km": s.dist_km})
        req.targets = new_targets
        return SourceResult(req, info.label, results, stations, log)

    elif src == "openmeteo":
        model = str(req.options.get("model", "era5"))
        label = f"Open-Meteo {OPENMETEO_MODELS.get(model, (model,))[0]}"
        res_deg = OPENMETEO_MODELS.get(model, ("", 0.25))[1]
        for t in req.targets:
            data, meta = openmeteo_daily(t.lat, t.lon, y0, y1, req.variables, model, progress, cancel)
            # ERA5-Land does not provide every variable (e.g. precipitation sum, ET0): take them from ERA5
            empty = [v for v in req.variables if v not in data or data[v]["value"].notna().sum() == 0]
            if empty and model != "era5":
                progress("read", 0, 0, f"Open-Meteo: {', '.join(empty)} not in {model}, using ERA5 ...")
                extra, _meta2 = openmeteo_daily(t.lat, t.lon, y0, y1, empty, "era5", progress, cancel)
                for v, df in extra.items():
                    if df["value"].notna().any():
                        data[v] = df
                        meta.setdefault("model_per_variable", {})[v] = "era5"
                log.append(f"Open-Meteo: {', '.join(empty)} taken from ERA5 (not available in {model}).")
            glat, glon = meta.get("grid_lat"), meta.get("grid_lon")
            cells = [DO.GridCell(float(glat), float(glon), 1.0, -1, -1, True)] if glat is not None else []
            for var, df in data.items():
                results[t.name][var] = VarResult(df, "daily", cells, (res_deg, res_deg),
                                                 {"model": model, **meta})
        cell = (res_deg, res_deg)

    else:
        raise ValueError(f"Source {src} is not implemented")

    # registry (location export)
    if GX is not None and src != "ghcnd":
        for t in req.targets:
            rs = results.get(t.name, {})
            if not rs:
                continue
            any_r = next(iter(rs.values()))
            years = []
            for r in rs.values():
                if r.freq == "daily" and len(r.data):
                    years += [int(r.data["Date"].dt.year.min()), int(r.data["Date"].dt.year.max())]
                elif len(r.data):
                    years += [int(r.data["Year"].min()), int(r.data["Year"].max())]
            GX.registry.add_climate_extraction(
                label, list(rs.keys()), t.name, t.lat, t.lon, t.mode,
                min(years) if years else y0, max(years) if years else y1,
                any_r.cells[:2000], any_r.cell_size, t.bbox, version=label,
                resolution=info.resolution,
                extra={"region": any_r.meta.get("region")} if src == "berkeley" else None)
    return SourceResult(req, label, results, [], log)


# ===========================================================================
# Probe (connectivity / format check, run on the user's computer)
# ===========================================================================

def probe(out_dir: Optional[str] = None, lat: float = 40.28, lon: float = 69.62,
          progress: ProgressCallback = DO._cli_progress) -> str:
    """Try every source with a tiny request; write a JSON + text report."""
    out_dir = out_dir or os.path.join(os.getcwd(), "sources_probe")
    os.makedirs(out_dir, exist_ok=True)
    report: List[Dict[str, Any]] = []
    t_pt = DO.ExtractionTarget("probe", lat, lon, "nearest")

    def step(name: str, fn: Callable[[], Any]) -> None:
        t0 = time.time()
        try:
            info = fn()
            report.append({"step": name, "ok": True, "seconds": round(time.time() - t0, 1), "info": info})
            print(f"[OK]   {name}: {str(info)[:300]}")
        except Exception as exc:
            report.append({"step": name, "ok": False, "seconds": round(time.time() - t0, 1),
                           "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]})
            print(f"[FAIL] {name}: {exc}")

    def chelsa_listing(freq: str, var: str, y: int):
        keys, prefs = s3_list(CHELSA_ENDPOINT, CHELSA_BUCKET, CHELSA_PREFIX[freq] + var + "/")
        files = chelsa_list_files(var, freq, y, y)
        return {"top_prefixes": prefs[:5], "top_keys": [k for k, _ in keys[:3]], "n_files_year": len(files),
                "first": [str(d) + " " + u for d, u in files[:2]]}

    def chelsa_read(freq: str, var: str, y: int):
        files = chelsa_list_files(var, freq, y, y)[:3]
        tif = RemoteGeoTiff(files[0][1])
        ex = extract_geotiff_series("chelsa_" + freq, var, files, [t_pt], workers=3)
        df, cells = ex["probe"]
        cell_size = ex["__cell_size__"]
        return {"size": [tif.width, tif.height], "tiled": tif.tiled, "block": [tif.tw, tif.th],
                "compression": tif.compression, "predictor": tif.predictor, "dtype": str(tif.dtype),
                "scale": tif.scale, "offset": tif.offset, "nodata": tif.nodata,
                "origin": [tif.x0, tif.y0], "pixel": [tif.sx, tif.sy], "values": df["value"].round(3).tolist(),
                "cell": [cells[0].lat, cells[0].lon], "cell_size": cell_size}

    step("CHELSA monthly listing", lambda: chelsa_listing("monthly", "tas", 2000))
    step("CHELSA monthly read", lambda: chelsa_read("monthly", "tas", 2000))
    step("CHELSA daily listing", lambda: chelsa_listing("daily", "pr", 2000))
    step("CHELSA daily read", lambda: chelsa_read("daily", "pr", 2000))
    step("TerraClimate point", lambda: terraclimate_point("ppt", lat, lon, 2000, 2000).round(2).to_dict("list"))
    step("GPCC listing", lambda: (lambda r: {"label": r[0], "files": [f.filename for f in r[1][:4]]})(
        gpcc_list_files("0.5", 1990, 2000)))
    step("CRU scPDSI file", lambda: (lambda r: {"label": r[0], "url": r[1].url, "size": DO.http_head_size(r[1].url)})(
        scpdsi_find_file()))
    step("SPEIbase file", lambda: (lambda r: {"url": r[1].url, "size": DO.http_head_size(r[1].url)})(
        speibase_find_file("spei12")))
    step("CHIRPS monthly listing", lambda: (lambda f: {"n": len(f), "first": [str(d) + " " + u for d, u in f[:2]]})(
        chirps_list_files("monthly", 2000, 2000)))
    step("CHIRPS daily listing", lambda: (lambda f: {"n": len(f), "first": [str(d) + " " + u for d, u in f[:2]]})(
        chirps_list_files("daily", 2000, 2000)))

    def chirps_read():
        files = chirps_list_files("monthly", 2000, 2000)[:2]
        tif = RemoteGeoTiff(files[0][1])
        ex = extract_geotiff_series("chirps_monthly", "precip", files, [t_pt], workers=2)
        return {"tiled": tif.tiled, "block": [tif.tw, tif.th], "compression": tif.compression,
                "values": ex["probe"][0]["value"].round(2).tolist()}

    step("CHIRPS monthly read", chirps_read)
    step("Berkeley Earth", lambda: (lambda r: {"url": r[2], "rows": len(r[0]), "last": r[0].tail(2).to_dict("list")})(
        berkeley_series("Tajikistan", "TAVG")))
    step("GHCN-Daily stations", lambda: [(s.station_id, s.name, s.dist_km) for s in
                                         ghcnd_search(lat, lon, 150, ["TMAX", "PRCP"], 1950, 2020, 10)[:5]])
    step("Open-Meteo ERA5", lambda: (lambda r: {"vars": list(r[0].keys()), "meta": r[1],
                                                "n": len(next(iter(r[0].values())))})(
        openmeteo_daily(lat, lon, 2000, 2000, ["temperature_2m_mean", "precipitation_sum"], "era5")))

    path_json = os.path.join(out_dir, "sources_probe_report.json")
    with open(path_json, "w", encoding="utf-8") as fh:
        json.dump({"version": __version__, "python": sys.version, "time": _dt.datetime.now().isoformat(),
                   "steps": report}, fh, indent=1, default=str)
    print(f"\nReport written to {path_json}")
    return path_json


def _cmd_list(args: argparse.Namespace) -> int:
    for k, s in SOURCES.items():
        print(f"{k:16s} {s.label} [{s.freq}, {s.period[0]}-{s.period[1]}, {s.resolution}]")
        for v in s.variables:
            print(f"    {v.code:30s} {v.label} ({v.units})")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    t = DO.ExtractionTarget(args.name, args.lat, args.lon, args.mode,
                            tuple(args.bbox) if args.bbox else None)
    req = SourceRequest(args.source, args.var, [t], args.start, args.end,
                        {"model": args.model, "gpcc_resolution": args.gpcc_res, "region": args.region or ""},
                        keep_downloads=False)
    res = run_source_request(req, DO._cli_progress)
    os.makedirs(args.out, exist_ok=True)
    for tname, rs in res.results.items():
        for var, r in rs.items():
            p = os.path.join(args.out, f"{args.source}_{var}_{re.sub(r'[^A-Za-z0-9]+', '_', tname)}.csv")
            r.data.to_csv(p, index=False)
            print("wrote", p)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Keyless climate data sources (DeClim)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="check all sources and write a report")
    p.add_argument("--out", default=None)
    p.add_argument("--lat", type=float, default=40.28)
    p.add_argument("--lon", type=float, default=69.62)
    sub.add_parser("list", help="list sources and variables")
    e = sub.add_parser("extract", help="extract a series to CSV")
    e.add_argument("--source", required=True, choices=list(SOURCES))
    e.add_argument("--var", nargs="+", required=True)
    e.add_argument("--lat", type=float, required=True)
    e.add_argument("--lon", type=float, required=True)
    e.add_argument("--mode", default="nearest", choices=["nearest", "bilinear", "bbox_mean"])
    e.add_argument("--bbox", type=float, nargs=4, metavar=("S", "N", "W", "E"))
    e.add_argument("--start", type=int, required=True)
    e.add_argument("--end", type=int, required=True)
    e.add_argument("--name", default="site")
    e.add_argument("--model", default="era5")
    e.add_argument("--gpcc-res", default="0.5")
    e.add_argument("--region", default=None)
    e.add_argument("--out", default=".")
    args = ap.parse_args(argv)
    if args.cmd == "probe":
        probe(args.out, args.lat, args.lon)
        return 0
    if args.cmd == "list":
        return _cmd_list(args)
    return _cmd_extract(args)


if __name__ == "__main__":
    sys.exit(main())
