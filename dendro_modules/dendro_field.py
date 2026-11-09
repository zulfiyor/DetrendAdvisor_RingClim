#!/usr/bin/env python3
"""
dendro_field.py - spatial (field) correlation of tree-ring chronologies with gridded climate.

Like "correlate with a field" in the KNMI Climate Explorer: a chronology is correlated, grid cell by
grid cell, with a seasonal climate window (e.g. previous July-September mean temperature) of a gridded
data set over a region. The result is a map of r with its significance and the site marked on it.

Fields
------
* CRU TS 4 (0.5 deg)              - decadal global files, downloaded temporarily, only the region is kept
* GPCC Full Data (0.25-1 deg)      - global NetCDF files, as above
* CRU scPDSI, SPEIbase (0.5 deg)   - one global file each, as above
* TerraClimate (~4 km)             - NetCDF subset of the region from THREDDS, read every n-th cell
* ERA5 / ERA5-Land monthly (CDS)   - with the user's own Copernicus key, only the region is requested
* Local NetCDF file                - any regular lat/lon grid with a monthly time axis

The region cube of every request is cached (``<cache>/fields/*.npz``), so trying other seasons,
periods or chronologies does not download anything again.

Results summary
---------------
``summarize_results`` reads a RingClim results table (Chronology, Climate, Window, Agg, R, p, N) and
tells which chronologies, variables and seasons give the strongest and most consistent signals, and
``suggest_fields`` turns the best of them into ready field-correlation settings.

Command line test::

    python dendro_field.py --source cru --var tmp --bbox 30 50 60 90 --years 1951 1995 --chron chron.csv \
        --window pJul pSep --agg mean --site 40.17 72.58 --png map.png
"""
from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import dendro_online as DO

try:
    import dendro_sources as S
except Exception:  # pragma: no cover - optional
    S = None  # type: ignore

try:
    import dendro_copernicus as DC
except Exception:  # pragma: no cover - optional
    DC = None  # type: ignore

ProgressCallback = Callable[[str, int, int, str], None]
CancelCheck = Callable[[], bool]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WINDOW_LABELS = ["p" + m for m in MONTHS] + MONTHS  # index 0..23: previous Jan .. current Dec


def _noop(*_a: Any) -> None:
    return None


def _never() -> bool:
    return False


# ===========================================================================
# Catalogue
# ===========================================================================

@dataclass(frozen=True)
class FieldVar:
    code: str
    label: str
    agg: str            # "mean" | "sum" over the months of a window
    param: str          # RingClim climate type (Temperature (mean), Precipitation, PET, ...)
    units: str = ""


@dataclass(frozen=True)
class FieldSource:
    key: str
    label: str
    res: float
    variables: Tuple[FieldVar, ...]
    citation: str
    needs_key: str = ""     # "" | "cds"
    first_year: int = 1901
    note: str = ""

    def var(self, code: str) -> FieldVar:
        for v in self.variables:
            if v.code == code:
                return v
        raise KeyError(f"{self.key}: unknown variable {code}")


def _cru_vars() -> Tuple[FieldVar, ...]:
    out = []
    for code, v in DO.CRU_VARIABLES.items():
        out.append(FieldVar(code, v.label, "sum" if code in ("pre", "wet", "frs") else "mean",
                            v.ringclim_param, v.units))
    return tuple(out)


def _src_vars(key: str, codes: Optional[Sequence[str]] = None) -> Tuple[FieldVar, ...]:
    if S is None or key not in S.SOURCES:
        return tuple()
    out = []
    for v in S.SOURCES[key].variables:
        if codes is None or v.code in codes:
            out.append(FieldVar(v.code, v.label, v.agg, v.ringclim_param, v.units))
    return tuple(out)


def _cds_vars(source: str) -> Tuple[FieldVar, ...]:
    if S is None or source not in S.SOURCES:
        return tuple()
    return tuple(FieldVar(v.code, v.label, v.agg, v.ringclim_param, v.units) for v in S.SOURCES[source].variables
                 if v.code not in ("dis24",))


def field_sources() -> Dict[str, FieldSource]:
    out: Dict[str, FieldSource] = {}
    out["cru"] = FieldSource("cru", "CRU TS 4 (0.5 deg)", 0.5, _cru_vars(), DO.CRU_CITATION, first_year=1901)
    if S is not None:
        tc = S.SOURCES.get("terraclimate")
        if tc is not None:
            out["terraclimate"] = FieldSource("terraclimate", "TerraClimate (~4 km, read at 0.25 deg)", 0.25,
                                              _src_vars("terraclimate"), tc.citation, first_year=1958,
                                              note="every 6th cell is read (0.25 deg) to keep the download small")
        g = S.SOURCES.get("gpcc")
        if g is not None:
            out["gpcc"] = FieldSource("gpcc", "GPCC Full Data (0.5 deg)", 0.5, _src_vars("gpcc"), g.citation,
                                      first_year=1891)
        sc = S.SOURCES.get("scpdsi")
        if sc is not None:
            out["scpdsi"] = FieldSource("scpdsi", "CRU scPDSI (0.5 deg)", 0.5, _src_vars("scpdsi"), sc.citation)
        sp = S.SOURCES.get("speibase")
        if sp is not None:
            out["speibase"] = FieldSource("speibase", "SPEIbase (0.5 deg)", 0.5, _src_vars("speibase"), sp.citation)
        for key, label, res, fy in (("era5_cds_monthly", "ERA5 monthly (0.25 deg, your Copernicus key)", 0.25, 1940),
                                    ("era5land_cds_monthly", "ERA5-Land monthly (0.1 deg, your Copernicus key)",
                                     0.1, 1950)):
            info = S.SOURCES.get(key)
            if info is not None and DC is not None:
                out[key] = FieldSource(key, label, res, _cds_vars(key), info.citation, needs_key="cds",
                                       first_year=fy)
    out["netcdf"] = FieldSource("netcdf", "Local NetCDF file (any regular lat/lon grid)", 0.0,
                                (FieldVar("auto", "first 3-D variable (or the name you type)", "mean", "Other"),),
                                "Cite the data set of the file.")
    return out


# ===========================================================================
# Field container and cache
# ===========================================================================

@dataclass
class Field:
    source: str
    var: str
    label: str
    units: str
    lats: np.ndarray          # ascending
    lons: np.ndarray          # ascending, -180..180
    years: np.ndarray         # per time step
    months: np.ndarray
    data: np.ndarray          # (time, lat, lon) float32
    citation: str = ""
    info: Dict[str, Any] = field(default_factory=dict)

    @property
    def res(self) -> float:
        if len(self.lats) > 1:
            return float(abs(np.median(np.diff(self.lats))))
        return 0.5

    def bbox(self) -> Tuple[float, float, float, float]:
        h = self.res / 2.0
        return (float(self.lats.min() - h), float(self.lats.max() + h),
                float(self.lons.min() - h), float(self.lons.max() + h))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp.npz"
        np.savez_compressed(tmp, lats=self.lats, lons=self.lons, years=self.years, months=self.months,
                            data=self.data.astype(np.float32),
                            meta=json.dumps({"source": self.source, "var": self.var, "label": self.label,
                                             "units": self.units, "citation": self.citation, "info": self.info},
                                            default=str))
        os.replace(tmp, path)

    @staticmethod
    def load(path: str) -> "Field":
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            return Field(meta["source"], meta["var"], meta["label"], meta.get("units", ""),
                         z["lats"].astype(float), z["lons"].astype(float), z["years"].astype(int),
                         z["months"].astype(int), z["data"].astype(np.float32), meta.get("citation", ""),
                         meta.get("info", {}))


def fields_cache_dir(base: Optional[str] = None) -> str:
    d = os.path.join(base or DO.default_cache_dir(), "fields")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(source: str, var: str, bbox: Sequence[float], y0: int, y1: int, extra: str = "",
                base: Optional[str] = None) -> str:
    key = json.dumps([source, var, [round(float(x), 3) for x in bbox], int(y0), int(y1), extra])
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", f"{source}_{var}")
    return os.path.join(fields_cache_dir(base), f"{safe}_{int(y0)}_{int(y1)}_{h}.npz")


def _covering_cache(source: str, var: str, bbox: Sequence[float], y0: int, y1: int, extra: str = "",
                    base: Optional[str] = None) -> Optional[str]:
    """A cached cube of the same field that covers the requested area and years."""
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", f"{source}_{var}")
    d = fields_cache_dir(base)
    for name in os.listdir(d):
        if not (name.startswith(safe + "_") and name.endswith(".npz")):
            continue
        p = os.path.join(d, name)
        try:
            with np.load(p, allow_pickle=False) as z:
                meta = json.loads(str(z["meta"]))
                if meta.get("info", {}).get("extra", "") != extra:
                    continue
                la, lo, yy = z["lats"], z["lons"], z["years"]
                res = float(abs(np.median(np.diff(la)))) if len(la) > 1 else 0.5
                h = res / 2 + 1e-6
                if (la.min() - h <= bbox[0] and la.max() + h >= bbox[1] and lo.min() - h <= bbox[2]
                        and lo.max() + h >= bbox[3] and yy.min() <= y0 and yy.max() >= y1):
                    return p
        except Exception:
            continue
    return None


def subset_field(f: Field, bbox: Sequence[float], y0: Optional[int] = None, y1: Optional[int] = None) -> Field:
    h = f.res / 2.0
    li = np.where((f.lats >= bbox[0] - h) & (f.lats <= bbox[1] + h))[0]
    lj = np.where((f.lons >= bbox[2] - h) & (f.lons <= bbox[3] + h))[0]
    ti = np.arange(len(f.years))
    if y0 is not None:
        ti = ti[f.years[ti] >= y0]
    if y1 is not None:
        ti = ti[f.years[ti] <= y1]
    return Field(f.source, f.var, f.label, f.units, f.lats[li], f.lons[lj], f.years[ti], f.months[ti],
                 f.data[np.ix_(ti, li, lj)], f.citation, dict(f.info))


# ===========================================================================
# NetCDF region reader
# ===========================================================================

def _time_year_month(nc, names: Sequence[str]) -> Tuple[np.ndarray, np.ndarray]:
    tname = DO._find_coord_name(names, ("time", "valid_time", "t"))
    if tname is None:
        raise DO.OnlineError("No time axis in the NetCDF file")
    tv = nc.variables[tname]
    if S is not None and hasattr(S, "_nc_time_year_month"):
        return S._nc_time_year_month(tv)
    return DO._nc_time_to_year_month(tv)


def _index_runs(idx: np.ndarray) -> List[Tuple[int, int]]:
    """Contiguous runs [(start, stop_exclusive)] of a sorted integer index array."""
    runs: List[Tuple[int, int]] = []
    if len(idx) == 0:
        return runs
    start = prev = int(idx[0])
    for k in idx[1:]:
        k = int(k)
        if k != prev + 1:
            runs.append((start, prev + 1))
            start = k
        prev = k
    runs.append((start, prev + 1))
    return runs


def read_netcdf_region(path: str, var_hint: str, bbox: Sequence[float], y0: int, y1: int,
                       stride: int = 1, skip_names: Sequence[str] = ("stn", "numgauge", "time_bnds")) -> Field:
    """Read only the region (min_lat, max_lat, min_lon, max_lon) and years y0..y1 of a gridded file."""
    from netCDF4 import Dataset

    with DO.NETCDF_LOCK, Dataset(path, "r") as nc:
        names = list(nc.variables.keys())
        lat_n = DO._find_coord_name(names, ("lat", "latitude", "y"))
        lon_n = DO._find_coord_name(names, ("lon", "longitude", "x"))
        if lat_n is None or lon_n is None:
            raise DO.OnlineError("Latitude/longitude variables not found")
        var_n = DO._find_coord_name(names, (var_hint.lower(),)) if var_hint and var_hint != "auto" else None
        if var_n is None:
            for nme in names:
                v = nc.variables[nme]
                if len(v.dimensions) >= 3 and nme.lower() not in skip_names:
                    var_n = nme
                    break
        if var_n is None:
            raise DO.OnlineError(f"No gridded variable in {os.path.basename(path)}")
        lats = np.asarray(nc.variables[lat_n][:], float)
        lons = np.asarray(nc.variables[lon_n][:], float)
        years, months = _time_year_month(nc, names)
        tsel = np.where((years >= y0) & (years <= y1))[0]
        if len(tsel) == 0:
            raise DO.OnlineError(f"{os.path.basename(path)}: no time steps in {y0}-{y1}")
        lw = np.array([DO._wrap_lon(x) for x in lons])
        isel = np.where((lats >= bbox[0]) & (lats <= bbox[1]))[0][::max(1, int(stride))]
        jsel_all = np.where((lw >= bbox[2]) & (lw <= bbox[3]))[0]
        jsel = jsel_all[np.argsort(lw[jsel_all])][::max(1, int(stride))]
        if len(isel) == 0 or len(jsel) == 0:
            raise DO.OnlineError("The region contains no grid cells of this data set")
        v = nc.variables[var_n]
        v.set_auto_maskandscale(True)
        dims = [d.lower() for d in v.dimensions]
        t_ax = next((i for i, d in enumerate(dims) if "time" in d or d == "t"), 0)
        la_ax = next((i for i, d in enumerate(dims) if d == lat_n.lower() or "lat" in d or d == "y"), 1)
        lo_ax = next((i for i, d in enumerate(dims) if d == lon_n.lower() or "lon" in d or d == "x"), 2)
        t0, t1 = int(tsel.min()), int(tsel.max()) + 1
        i0, i1 = int(isel.min()), int(isel.max()) + 1
        blocks = []
        jorder: List[int] = []
        for a, b in _index_runs(np.sort(jsel)):
            idx: List[Any] = [slice(None)] * v.ndim
            idx[t_ax], idx[la_ax], idx[lo_ax] = slice(t0, t1), slice(i0, i1), slice(a, b)
            arr = np.ma.filled(np.ma.asarray(v[tuple(idx)], dtype=np.float32), np.nan)
            other = [k for k in range(arr.ndim) if k not in (t_ax, la_ax, lo_ax)]
            arr = np.transpose(arr, [t_ax, la_ax, lo_ax] + other)
            if other:
                with np.errstate(all="ignore"):
                    arr = np.nanmean(arr.reshape(arr.shape[:3] + (-1,)), axis=3)
            blocks.append(arr)
            jorder.extend(range(a, b))
        data = np.concatenate(blocks, axis=2)
        # keep only the selected (strided) columns / rows / time steps, sorted by longitude
        jpos = {j: k for k, j in enumerate(jorder)}
        data = data[:, :, [jpos[j] for j in jsel]]
        data = data[:, [int(i) - i0 for i in isel], :]
        data = data[[int(t) - t0 for t in tsel], :, :]
        data[np.abs(data) > 1e20] = np.nan
        units = str(getattr(v, "units", ""))
        la = lats[isel]
        lo = lw[jsel]
        if len(la) > 1 and la[0] > la[-1]:
            la = la[::-1]
            data = data[:, ::-1, :]
        return Field("", var_n, var_n, units, la.astype(float), lo.astype(float), years[tsel].astype(int),
                     months[tsel].astype(int), data.astype(np.float32))


def _concat_fields(parts: Sequence[Field]) -> Field:
    parts = [p for p in parts if p is not None and len(p.years)]
    if not parts:
        raise DO.OnlineError("No data read")
    f0 = parts[0]
    years = np.concatenate([p.years for p in parts])
    months = np.concatenate([p.months for p in parts])
    data = np.concatenate([p.data for p in parts], axis=0)
    key = years * 12 + months
    _u, first = np.unique(key[::-1], return_index=True)   # keep the last occurrence
    keep = np.sort(len(key) - 1 - first)
    order = keep[np.argsort(key[keep])]
    return Field(f0.source, f0.var, f0.label, f0.units, f0.lats, f0.lons, years[order], months[order],
                 data[order], f0.citation, dict(f0.info))


# ===========================================================================
# Downloads per source
# ===========================================================================

def _download_and_read(url: str, filename: str, var_hint: str, bbox, y0: int, y1: int,
                       progress: ProgressCallback, cancel: CancelCheck, keep: bool = False,
                       stride: int = 1) -> Field:
    dl_dir = os.path.join(DO.default_cache_dir(), "grid_downloads")
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, filename)
    work = DO._ascii_work_dir()
    try:
        DO.download_file(url, path, progress=progress, cancel=cancel, stage="download")
        if filename.lower().endswith(".gz"):
            nc_path = os.path.join(work, filename[:-3])
            progress("decompress", 0, 0, f"Decompressing {filename} ...")
            DO._decompress_gz(path, nc_path, cancel=cancel)
        elif DO._is_ascii(os.path.abspath(path)):
            nc_path = path
        else:
            nc_path = os.path.join(work, filename)
            shutil.copyfile(path, nc_path)
        progress("extract", 0, 0, f"Reading the region from {filename} ...")
        return read_netcdf_region(nc_path, var_hint, bbox, y0, y1, stride=stride)  # NETCDF_LOCK inside
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if not keep and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _parallel_files(files, var: str, bbox, y0: int, y1: int, progress, cancel, workers: int = 4,
                    stride: int = 1):
    """Download and read several global files at the same time (decades of CRU TS, GPCC periods)."""
    import concurrent.futures as cf
    import threading

    if len(files) <= 1:
        return [_download_and_read(u, n, var, bbox, y0, y1, progress, cancel, stride=stride) for u, n in files]
    lock = threading.Lock()
    done = [0]
    progress("file", 0, len(files), f"Downloading {len(files)} file(s), {min(workers, len(files))} at a time ...")

    def one(item):
        url, name = item
        if cancel():
            raise DO.CancelledError("Cancelled")
        fld = _download_and_read(url, name, var, bbox, y0, y1, progress, cancel, stride=stride)
        with lock:
            done[0] += 1
            progress("file", done[0], len(files), f"[{done[0]}/{len(files)}] {name} ready")
        return fld

    with cf.ThreadPoolExecutor(max_workers=min(workers, len(files))) as ex:
        return list(ex.map(one, files))


def _load_cru(var: str, bbox, y0: int, y1: int, progress, cancel, options) -> Field:
    version = str(options.get("cru_version") or "")
    # newest release first (list_cru_versions is sorted newest -> oldest); only 4.x has the
    # monthly decadal files used here
    cands = [version] if version else [v for v in DO.list_cru_versions() if v.startswith("4.")] or ["4.09"]
    release, last = None, None
    for v in cands[:4]:
        progress("plan", 0, 0, f"Resolving CRU TS {v} ...")
        try:
            release = DO.resolve_cru_release(v)
            break
        except DO.OnlineError as exc:
            last = exc
    if release is None:
        raise last or DO.OnlineError("No CRU TS release found")
    files = DO.select_cru_files(DO.list_cru_files(release, var), y0, y1, True)
    parts = _parallel_files([(f.url, f.filename) for f in files], var, bbox, y0, y1, progress, cancel)
    fld = _concat_fields(parts)
    fld.info["version"] = release.version
    return fld


def _load_gpcc(var: str, bbox, y0: int, y1: int, progress, cancel, options) -> Field:
    label, files = S.gpcc_list_files(str(options.get("gpcc_res", "0.5")), y0, y1)
    parts = _parallel_files([(f.url, f.filename) for f in files], "precip", bbox, y0, y1, progress, cancel)
    fld = _concat_fields(parts)
    fld.info["dataset"] = label
    return fld


def _load_single_file(kind: str, var: str, bbox, y0: int, y1: int, progress, cancel, options) -> Field:
    if kind == "scpdsi":
        label, f = S.scpdsi_find_file()
        hint = "scpdsi"
    else:
        label, f = S.speibase_find_file(var)
        hint = "spei"
    fld = _download_and_read(f.url, f.filename, hint, bbox, y0, y1, progress, cancel,
                             keep=bool(options.get("keep_large_files", False)))
    fld.info["dataset"] = label
    return fld


def _load_terraclimate(var: str, bbox, y0: int, y1: int, progress, cancel, options) -> Field:
    stride = int(options.get("terraclimate_stride", 6))
    s_, n_, w_, e_ = bbox
    q = urllib.parse.urlencode({
        "var": var, "north": n_, "south": s_, "west": w_, "east": e_, "horizStride": stride,
        "time_start": f"{y0}-01-01T00:00:00Z", "time_end": f"{y1}-12-31T23:59:59Z", "accept": "netcdf",
    })
    cand = ([S._TC_RESOLVED[var]] if var in getattr(S, "_TC_RESOLVED", {}) else []) + [
        srv + path + name for srv in S.TERRACLIMATE_SERVERS for name in S._terraclimate_names(var)
        for path in ("/ncss/grid/", "/ncss/")]
    last: Optional[BaseException] = None
    progress("download", 0, 0, f"TerraClimate {var}: requesting the region (every {stride}th cell) ...")
    for base in cand:
        if cancel():
            raise DO.CancelledError("Cancelled")
        try:
            data = DO.http_get_bytes(f"{base}?{q}", timeout=float(options.get("timeout", 600)), retries=1)
            if data[:4] not in (b"CDF\x01", b"CDF\x02", b"\x89HDF"):
                raise DO.OnlineError("not a NetCDF answer")
            work = DO._ascii_work_dir()
            try:
                p = os.path.join(work, f"tc_{var}.nc")
                with open(p, "wb") as fh:
                    fh.write(data)
                return read_netcdf_region(p, var, (-90, 90, -180, 180), y0, y1)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        except DO.CancelledError:
            raise
        except Exception as exc:
            last = exc
    raise DO.OnlineError(f"TerraClimate region request failed ({last}).")


def _load_cds(source: str, var: str, bbox, y0: int, y1: int, progress, cancel, options) -> Field:
    if DC is None:
        raise DO.OnlineError("dendro_copernicus.py is missing")
    ds = DC.DATASETS[source]
    DC.require_key(ds.service)
    cv = ds.variables[var]
    area = [round(min(90.0, bbox[1]), 3), round(max(-180.0, bbox[2]), 3),
            round(max(-90.0, bbox[0]), 3), round(min(180.0, bbox[3]), 3)]
    grids = {}
    jobs = []
    for nm in cv.request_names:
        req = {
            "product_type": ["monthly_averaged_reanalysis"], "variable": [nm],
            "year": [str(y) for y in range(y0, y1 + 1)], "month": [f"{m:02d}" for m in range(1, 13)],
            "time": ["00:00"], "area": area, "data_format": "netcdf", "download_format": "unarchived",
        }
        h = DC.request_hash(ds.dataset, req)
        job = DC.Job(ds.service, ds.dataset, req, os.path.join(DC.cache_dir(), f"field_{source}_{nm}_{h}.nc"),
                     label=f"{nm} field {y0}-{y1}")
        jobs.append((nm, job))
    DC.run_jobs([j for _n, j in jobs], progress, cancel, max_active=2)
    for nm, job in jobs:
        if job.status == "failed":
            raise DO.OnlineError(job.message or f"{nm}: request failed")
        names = cv.nc_names if len(cv.request_names) == 1 else ({"2m_temperature": ("t2m",),
                                                                  "2m_dewpoint_temperature": ("d2m",)}[nm])
        grids[nm] = DC.read_grid(job.dest, names)
    if cv.convert == "derived":
        gt, gd = grids["2m_temperature"], grids["2m_dewpoint_temperature"]
        es, ea = DC._es_kpa(gt.values - 273.15), DC._es_kpa(gd.values - 273.15)
        vals = (es - ea) if var == "vpd" else np.clip(100.0 * ea / es, 0, 100)
        g = gt
    else:
        g = grids[cv.request_names[0]]
        vals = np.empty_like(g.values)
        days = DC._days(g.times)[:, None, None] if cv.convert in ("m_month", "evap_month") else None
        if days is not None:
            sign = -1.0 if cv.convert == "evap_month" else 1.0
            vals = sign * g.values * 1000.0 * days
        else:
            vals = DC.convert(cv, g.values.reshape(len(g.times), -1), g.times).reshape(g.values.shape)
    lats, lons = np.asarray(g.lats, float), np.array([DO._wrap_lon(x) for x in g.lons])
    order = np.argsort(lons)
    vals = vals[:, :, order]
    lons = lons[order]
    if lats[0] > lats[-1]:
        lats = lats[::-1]
        vals = vals[:, ::-1, :]
    t = pd.DatetimeIndex(g.times)
    df = pd.DataFrame({"y": t.year, "m": t.month})
    key = df.drop_duplicates().index.values   # monthly means: one value per month
    return Field(source, var, var, "", lats, lons, t.year.values[key].astype(int), t.month.values[key].astype(int),
                 vals[key].astype(np.float32))


def load_field(source: str, var: str, bbox: Sequence[float], y0: int, y1: int,
               progress: ProgressCallback = _noop, cancel: CancelCheck = _never,
               options: Optional[Dict[str, Any]] = None) -> Field:
    """Region cube of one variable (cached). bbox = (min_lat, max_lat, min_lon, max_lon)."""
    options = dict(options or {})
    srcs = field_sources()
    if source not in srcs:
        raise ValueError(f"Unknown field source {source}")
    info = srcs[source]
    bbox = (max(-90.0, float(bbox[0])), min(90.0, float(bbox[1])),
            max(-180.0, float(bbox[2])), min(180.0, float(bbox[3])))
    if bbox[0] >= bbox[1] or bbox[2] >= bbox[3]:
        raise ValueError("Empty region")
    y0 = max(int(y0), info.first_year)
    extra = ""
    if source == "terraclimate":
        extra = f"stride{int(options.get('terraclimate_stride', 6))}"
    elif source == "gpcc":
        extra = f"res{options.get('gpcc_res', '0.5')}"
    elif source == "netcdf":
        p = str(options.get("path") or "")
        extra = f"{os.path.abspath(p)}|{os.path.getmtime(p) if os.path.exists(p) else 0}"
    cached = _covering_cache(source, var, bbox, y0, y1, extra)
    if cached:
        progress("cache", 1, 1, f"{info.label} {var}: from the cache")
        return subset_field(Field.load(cached), bbox, y0, y1)
    t0 = time.time()
    if source == "cru":
        fld = _load_cru(var, bbox, y0, y1, progress, cancel, options)
    elif source == "gpcc":
        fld = _load_gpcc(var, bbox, y0, y1, progress, cancel, options)
    elif source in ("scpdsi", "speibase"):
        fld = _load_single_file(source, var, bbox, y0, y1, progress, cancel, options)
    elif source == "terraclimate":
        fld = _load_terraclimate(var, bbox, y0, y1, progress, cancel, options)
    elif source in ("era5_cds_monthly", "era5land_cds_monthly"):
        fld = _load_cds(source, var, bbox, y0, y1, progress, cancel, options)
    elif source == "netcdf":
        path = str(options.get("path") or "")
        if not path or not os.path.exists(path):
            raise DO.OnlineError("Choose a NetCDF file")
        fld = read_netcdf_region(path, var, bbox, y0, y1)
    else:
        raise ValueError(source)
    try:
        fv = info.var(var)
        fld.label = fv.label
        fld.units = fld.units or fv.units
    except KeyError:
        pass
    fld.source = source
    fld.citation = info.citation
    fld.info.update({"extra": extra, "source_label": info.label, "downloaded_s": round(time.time() - t0, 1)})
    try:
        fld.save(_cache_path(source, var, bbox, y0, y1, extra))
    except Exception:
        pass
    return fld


# ===========================================================================
# Seasonal windows and correlation
# ===========================================================================

def parse_window(text: str) -> Optional[Tuple[int, int]]:
    """'pJul_pSep' -> (6, 8); 'Aug' -> (19, 19); 'pDec_Feb' -> (11, 13). Daily windows -> None."""
    t = str(text or "").strip()
    parts = [p for p in re.split(r"[_\-– ]+", t) if p]
    if not parts or len(parts) > 2:
        return None
    idx = []
    for p in parts:
        if p in WINDOW_LABELS:
            idx.append(WINDOW_LABELS.index(p))
        else:
            return None
    if len(idx) == 1:
        idx.append(idx[0])
    if idx[1] < idx[0]:
        return None
    return idx[0], idx[1]


def window_label(si: int, ei: int) -> str:
    return WINDOW_LABELS[si] if si == ei else f"{WINDOW_LABELS[si]}-{WINDOW_LABELS[ei]}"


def seasonal_cube(f: Field, si: int, ei: int, agg: str, y0: int, y1: int,
                  min_months_frac: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """years (y0..y1) and the window aggregate per year and cell: (n_years, n_lat, n_lon)."""
    years = np.arange(int(y0), int(y1) + 1)
    pos = {(int(y), int(m)): k for k, (y, m) in enumerate(zip(f.years, f.months))}
    nlat, nlon = f.data.shape[1], f.data.shape[2]
    out = np.full((len(years), nlat, nlon), np.nan, dtype=np.float32)
    n_m = ei - si + 1
    for k, y in enumerate(years):
        idx = []
        for w in range(si, ei + 1):
            yy = y - 1 if w < 12 else y
            mm = (w % 12) + 1
            t = pos.get((yy, mm))
            if t is None:
                idx = None
                break
            idx.append(t)
        if not idx:
            continue
        block = f.data[idx]
        with np.errstate(all="ignore"):
            valid = np.isfinite(block).sum(axis=0)
            if agg == "sum":
                v = np.nansum(block, axis=0) * (n_m / np.maximum(valid, 1))
            else:
                v = np.nanmean(block, axis=0)
        v[valid < math.ceil(min_months_frac * n_m)] = np.nan
        out[k] = v
    return years, out


def _rank(a: np.ndarray) -> np.ndarray:
    """Ranks along axis 0 with NaN kept (average ranks for ties not needed for continuous data)."""
    out = np.full(a.shape, np.nan, dtype=float)
    flat = a.reshape(a.shape[0], -1)
    of = out.reshape(a.shape[0], -1)
    for j in range(flat.shape[1]):
        col = flat[:, j]
        ok = np.isfinite(col)
        if ok.sum() < 3:
            continue
        r = pd.Series(col[ok]).rank().to_numpy()
        of[ok, j] = r
    return out


def _detrend_cols(y: np.ndarray, x_years: np.ndarray) -> np.ndarray:
    out = np.full(y.shape, np.nan, dtype=float)
    flat = y.reshape(y.shape[0], -1)
    of = out.reshape(y.shape[0], -1)
    for j in range(flat.shape[1]):
        col = flat[:, j]
        ok = np.isfinite(col)
        if ok.sum() < 4:
            continue
        b = np.polyfit(x_years[ok], col[ok], 1)
        of[ok, j] = col[ok] - np.polyval(b, x_years[ok])
    return out


def _t_sf_two_sided(t: np.ndarray, df: np.ndarray) -> np.ndarray:
    try:
        from scipy import stats

        return 2.0 * stats.t.sf(np.abs(t), df)
    except Exception:
        # normal approximation with a small-sample correction
        z = np.abs(t) * (1 - 1 / (4 * np.maximum(df, 1)))
        return np.array([math.erfc(v / math.sqrt(2)) if np.isfinite(v) else np.nan for v in z.ravel()]
                        ).reshape(np.shape(z))


@dataclass
class CorrMap:
    lats: np.ndarray
    lons: np.ndarray
    r: np.ndarray            # (lat, lon)
    p: np.ndarray
    n: np.ndarray
    years: np.ndarray
    chron_name: str
    field_label: str
    source: str
    var: str
    window: Tuple[int, int]
    agg: str
    method: str
    prep: str
    site: Optional[Tuple[float, float, str]] = None
    citation: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)

    def title(self) -> str:
        w = window_label(*self.window)
        return (f"{self.chron_name}  vs  {self.field_label} {w} ({self.agg}), "
                f"{int(self.years.min())}-{int(self.years.max())}")

    def to_frame(self) -> pd.DataFrame:
        la, lo = np.meshgrid(self.lats, self.lons, indexing="ij")
        return pd.DataFrame({"lat": la.ravel(), "lon": lo.ravel(), "r": self.r.ravel(), "p": self.p.ravel(),
                             "n": self.n.ravel()})


def correlate_field(chron: pd.Series, f: Field, window: Tuple[int, int], agg: str = "mean",
                    y0: Optional[int] = None, y1: Optional[int] = None, method: str = "pearson",
                    prep: str = "none", min_n: int = 15, alpha: float = 0.05,
                    site: Optional[Tuple[float, float, str]] = None, chron_name: str = "chronology") -> CorrMap:
    """Correlation of a chronology (index = year) with a seasonal window of every grid cell."""
    s = pd.to_numeric(pd.Series(chron), errors="coerce").dropna()
    s.index = pd.Index(s.index).astype(int)
    fy0 = int(f.years.min()) + (1 if window[0] < 12 else 0)
    fy1 = int(f.years.max())
    a = max(int(s.index.min()), fy0, int(y0) if y0 else -10 ** 6)
    b = min(int(s.index.max()), fy1, int(y1) if y1 else 10 ** 6)
    if b - a + 1 < max(3, min_n):
        raise ValueError(f"Too few common years ({max(0, b - a + 1)}) between the chronology and the field")
    years, cube = seasonal_cube(f, window[0], window[1], agg, a, b)
    x = s.reindex(years).to_numpy(dtype=float)
    y = cube.astype(float)
    if prep == "detrend":
        x = _detrend_cols(x[:, None], years.astype(float))[:, 0]
        y = _detrend_cols(y, years.astype(float))
    elif prep == "diff":
        x = np.concatenate([[np.nan], np.diff(x)])
        y = np.concatenate([np.full((1,) + y.shape[1:], np.nan), np.diff(y, axis=0)], axis=0)
    if method == "spearman":
        x = _rank(x[:, None])[:, 0]
        y = _rank(y)
    xs = x[:, None, None]
    valid = np.isfinite(y) & np.isfinite(xs)
    n = valid.sum(axis=0)
    with np.errstate(all="ignore"):
        xm = np.where(valid, xs, 0).sum(axis=0) / n
        ym = np.where(valid, y, 0).sum(axis=0) / n
        dx = np.where(valid, xs - xm, 0)
        dy = np.where(valid, y - ym, 0)
        r = (dx * dy).sum(axis=0) / np.sqrt((dx ** 2).sum(axis=0) * (dy ** 2).sum(axis=0))
        r = np.clip(r, -1, 1)
        t = r * np.sqrt((n - 2) / np.maximum(1e-12, 1 - r ** 2))
    r[n < max(3, min_n)] = np.nan
    p = np.full(r.shape, np.nan)
    ok = np.isfinite(r)
    if ok.any():
        p[ok] = _t_sf_two_sided(t[ok], (n[ok] - 2).astype(float))
    cm = CorrMap(f.lats, f.lons, r, p, n, years, chron_name, f.label, f.source, f.var, window, agg, method, prep,
                 site, f.citation)
    cm.stats = map_statistics(cm, alpha)
    return cm


def map_statistics(cm: CorrMap, alpha: float = 0.05) -> Dict[str, Any]:
    w = np.cos(np.radians(cm.lats))[:, None] * np.ones((1, len(cm.lons)))
    ok = np.isfinite(cm.r)
    out: Dict[str, Any] = {"alpha": alpha, "cells": int(ok.sum())}
    if not ok.any():
        return out
    sig = ok & (cm.p < alpha)
    out["area_significant_pct"] = round(100.0 * float(w[sig].sum()) / float(w[ok].sum()), 1)
    out["area_sig_pos_pct"] = round(100.0 * float(w[sig & (cm.r > 0)].sum()) / float(w[ok].sum()), 1)
    out["area_sig_neg_pct"] = round(100.0 * float(w[sig & (cm.r < 0)].sum()) / float(w[ok].sum()), 1)
    k = np.nanargmax(cm.r)
    i, j = np.unravel_index(k, cm.r.shape)
    out["max_r"] = (round(float(cm.r[i, j]), 3), float(cm.lats[i]), float(cm.lons[j]))
    k = np.nanargmin(cm.r)
    i, j = np.unravel_index(k, cm.r.shape)
    out["min_r"] = (round(float(cm.r[i, j]), 3), float(cm.lats[i]), float(cm.lons[j]))
    out["n_years"] = int(np.nanmax(cm.n)) if np.isfinite(cm.n).any() else 0
    if cm.site is not None:
        i = int(np.argmin(np.abs(cm.lats - cm.site[0])))
        j = int(np.argmin(np.abs(cm.lons - cm.site[1])))
        out["site_cell"] = (float(cm.lats[i]), float(cm.lons[j]))
        out["site_r"] = None if not np.isfinite(cm.r[i, j]) else round(float(cm.r[i, j]), 3)
        out["site_p"] = None if not np.isfinite(cm.p[i, j]) else float(cm.p[i, j])
    # expected share of significant cells by chance is alpha (cells are not independent: field significance
    # needs a Monte-Carlo test, here only a rough hint as in Livezey & Chen 1983)
    out["by_chance_pct"] = round(100.0 * alpha, 1)
    return out


def cell_series(cm: CorrMap, f: Field, lat: float, lon: float) -> Tuple[np.ndarray, np.ndarray, float, float]:
    """Seasonal series of the cell nearest to (lat, lon) for the window of the map."""
    i = int(np.argmin(np.abs(f.lats - lat)))
    j = int(np.argmin(np.abs(f.lons - lon)))
    years, cube = seasonal_cube(f, cm.window[0], cm.window[1], cm.agg, int(cm.years.min()), int(cm.years.max()))
    return years, cube[:, i, j], float(f.lats[i]), float(f.lons[j])


# ===========================================================================
# Map drawing
# ===========================================================================

def _smooth_grid(a: np.ndarray, factor: int) -> np.ndarray:
    """Bilinear up-sampling of a cell grid; NaN cells (sea, no data) stay empty."""
    try:
        from scipy import ndimage
    except Exception:  # pragma: no cover
        return np.kron(a, np.ones((factor, factor)))
    a = np.asarray(a, dtype=float)
    ok = np.isfinite(a)
    if not ok.any():
        return np.kron(a, np.ones((factor, factor)))
    # fill gaps with the nearest valid cell so that the interpolation does not spread NaN
    idx = ndimage.distance_transform_edt(~ok, return_distances=False, return_indices=True)
    filled = a[tuple(idx)]
    out = ndimage.zoom(filled, factor, order=1, mode="nearest", grid_mode=True)
    mask = ndimage.zoom(ok.astype(float), factor, order=0, mode="nearest", grid_mode=True) > 0.5
    out[~mask] = np.nan
    return out


def draw_corr_map(fig, cm: CorrMap, alpha: float = 0.05, only_significant: bool = False,
                  vmax: Optional[float] = None, show_countries: bool = True, style: str = "smooth") -> Any:
    """Draw the correlation map on a matplotlib figure; returns the main axes."""
    fig.clear()
    ax = fig.add_subplot(111)
    r = np.array(cm.r, dtype=float)
    sig = np.isfinite(r) & (cm.p < alpha)
    if vmax is None:
        vmax = float(np.nanmax(np.abs(r))) if np.isfinite(r).any() else 0.5
        vmax = max(0.2, math.ceil(vmax * 10) / 10.0)
    h_lat = (cm.lats[1] - cm.lats[0]) / 2 if len(cm.lats) > 1 else 0.25
    h_lon = (cm.lons[1] - cm.lons[0]) / 2 if len(cm.lons) > 1 else 0.25
    lat_e = np.concatenate([cm.lats - h_lat, [cm.lats[-1] + h_lat]])
    lon_e = np.concatenate([cm.lons - h_lon, [cm.lons[-1] + h_lon]])
    shown = np.where(sig, r, np.nan) if only_significant else r
    if style == "smooth" and min(len(cm.lats), len(cm.lons)) >= 3:
        # interpolated surface (like KNMI / contour maps); the cell values stay unchanged for hover/click
        factor = int(max(2, min(8, 600 // max(len(cm.lats), len(cm.lons)))))
        flip = len(cm.lats) > 1 and cm.lats[0] > cm.lats[-1]
        g = shown[::-1] if flip else shown
        sm = _smooth_grid(g, factor)
        ext = (lon_e[0], lon_e[-1], min(lat_e[0], lat_e[-1]), max(lat_e[0], lat_e[-1]))
        mesh = ax.imshow(np.ma.masked_invalid(sm), extent=ext, origin="lower", cmap="RdBu_r", vmin=-vmax,
                         vmax=vmax, interpolation="bilinear", aspect="auto", zorder=1)
        sg = (sig[::-1] if flip else sig).astype(float)
        sg[~np.isfinite(g if not only_significant else (r[::-1] if flip else r))] = np.nan
        if not only_significant:
            veil = _smooth_grid(np.where(np.isfinite(sg), 1.0 - sg, np.nan), factor)
            rgba = np.zeros(veil.shape + (4,), dtype=float)
            rgba[..., :3] = 0.62  # grey veil over the non-significant area only
            rgba[..., 3] = np.where(np.isfinite(veil), np.clip(veil, 0, 1) * 0.55, 0.0)
            ax.imshow(rgba, extent=ext, origin="lower", interpolation="bilinear", aspect="auto", zorder=2)
        # outline of the significant area
        try:
            sgs = _smooth_grid(sg, factor)
            try:
                from scipy import ndimage

                okm = np.isfinite(sgs)
                sgs = ndimage.gaussian_filter(np.where(okm, sgs, 0.0), sigma=factor * 0.7)
                sgs[~okm] = np.nan
            except Exception:
                pass
            yy = np.linspace(ext[2], ext[3], sgs.shape[0])
            xx = np.linspace(ext[0], ext[1], sgs.shape[1])
            if np.nanmax(sgs) > 0.5 > np.nanmin(sgs):
                ax.contour(xx, yy, np.ma.masked_invalid(sgs), levels=[0.5], colors="#222", linewidths=0.9,
                           zorder=3)
        except Exception:
            pass
    else:
        mesh = ax.pcolormesh(lon_e, lat_e, np.ma.masked_invalid(shown), cmap="RdBu_r", vmin=-vmax, vmax=vmax,
                             shading="flat")
        if not only_significant:
            # grey veil over cells that are not significant
            veil = np.where(np.isfinite(r) & ~sig, 1.0, np.nan)
            ax.pcolormesh(lon_e, lat_e, np.ma.masked_invalid(veil), cmap="Greys", vmin=0, vmax=3, alpha=0.55,
                          shading="flat")
    cb = fig.colorbar(mesh, ax=ax, fraction=0.04, pad=0.02)
    cb.set_label(f"r ({cm.method})")
    if show_countries:
        try:
            for c in DO.load_countries(download_if_missing=False):
                la0, la1, lo0, lo1 = c["bounds"]
                if la1 < lat_e[0] or la0 > lat_e[-1] or lo1 < lon_e[0] or lo0 > lon_e[-1]:
                    continue
                for poly in c["polygons"]:
                    ring = poly[0]
                    ax.plot(ring[:, 0], ring[:, 1], color="#333", lw=0.6)
        except Exception:
            pass
    if cm.site is not None:
        ax.plot(cm.site[1], cm.site[0], marker="*", ms=16, color="gold", mec="k", mew=1.0, zorder=10)
        ax.annotate(cm.site[2], (cm.site[1], cm.site[0]), xytext=(6, 6), textcoords="offset points",
                    fontsize=9, fontweight="bold", zorder=11,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.75))
    ax.set_xlim(lon_e[0], lon_e[-1])
    ax.set_ylim(lat_e[0], lat_e[-1])
    ax.set_ylim(min(lat_e[0], lat_e[-1]), max(lat_e[0], lat_e[-1]))
    ax.set_aspect(1.0 / max(0.2, math.cos(math.radians(float(np.mean(cm.lats))))))
    ax.set_xlabel("Longitude (deg E)")
    ax.set_ylabel("Latitude (deg N)")
    st = cm.stats or {}
    sub = (f"p < {alpha:g}: {st.get('area_significant_pct', 0)}% of the area "
           f"(+{st.get('area_sig_pos_pct', 0)}% / -{st.get('area_sig_neg_pct', 0)}%), "
           f"n = {st.get('n_years', '')} years")
    if st.get("site_r") is not None:
        sub += f"; r at the site = {st['site_r']:.2f}"
    prep = {"detrend": ", linear trends removed", "diff": ", first differences"}.get(cm.prep, "")
    ax.set_title(cm.title() + prep + "\n" + sub, fontsize=9)
    ax.grid(True, lw=0.3, alpha=0.5)
    return ax


# ===========================================================================
# Results summary and suggestions
# ===========================================================================

PARAM_FAMILY = {
    "Temperature (mean)": "temperature", "Temperature (max)": "temperature", "Temperature (min)": "temperature",
    "Precipitation": "precipitation", "PET": "evaporative demand", "Vapour pressure deficit": "evaporative demand",
    "Radiation": "evaporative demand", "Climatic water deficit": "drought", "SPEI": "drought", "SPI": "drought",
    "PDSI": "drought", "Soil moisture": "moisture", "Relative humidity": "moisture", "Vapour pressure": "moisture",
    "Actual evapotranspiration": "moisture", "Runoff": "moisture", "River discharge": "moisture",
    "Snow": "snow", "Climate moisture index": "drought",
}

# climate type -> list of (field source, variable) that represent it on a grid, best first
PARAM_TO_FIELDS: Dict[str, List[Tuple[str, str]]] = {
    "Temperature (mean)": [("cru", "tmp"), ("era5_cds_monthly", "t2m")],
    "Temperature (max)": [("cru", "tmx"), ("terraclimate", "tmax")],
    "Temperature (min)": [("cru", "tmn"), ("terraclimate", "tmin")],
    "Precipitation": [("cru", "pre"), ("gpcc", "precip"), ("terraclimate", "ppt")],
    "PET": [("cru", "pet"), ("terraclimate", "pet")],
    "Vapour pressure deficit": [("terraclimate", "vpd"), ("era5_cds_monthly", "vpd")],
    "Radiation": [("terraclimate", "srad"), ("era5_cds_monthly", "ssrd")],
    "Climatic water deficit": [("terraclimate", "def")],
    "SPEI": [("speibase", "spei03")],
    "SPI": [("speibase", "spei03")],
    "PDSI": [("scpdsi", "scpdsi"), ("terraclimate", "PDSI")],
    "Soil moisture": [("terraclimate", "soil"), ("era5_cds_monthly", "swvl1")],
    "Vapour pressure": [("cru", "vap"), ("terraclimate", "vap")],
    "Actual evapotranspiration": [("terraclimate", "aet")],
    "Runoff": [("terraclimate", "q")],
    "Snow": [("terraclimate", "swe")],
    "Relative humidity": [("era5_cds_monthly", "rh")],
    "Wet days": [("cru", "wet")], "Frost days": [("cru", "frs")], "Cloud cover": [("cru", "cld")],
    "Diurnal temperature range": [("cru", "dtr")],
}

_SOURCE_LABEL_TO_KEY = [
    ("cru ts", "cru"), ("cru4", "cru"), ("terraclimate", "terraclimate"), ("gpcc", "gpcc"),
    ("scpdsi", "scpdsi"), ("speibase", "speibase"), ("era5-land", "era5land_cds_monthly"),
    ("era5_land", "era5land_cds_monthly"), ("era5", "era5_cds_monthly"),
]


@dataclass
class ClimateInfo:
    name: str
    param: str = "Other"
    source: str = ""            # free label (meta["source"])
    variable: str = ""          # variable code in the source
    first_year: Optional[int] = None
    last_year: Optional[int] = None


@dataclass
class FieldSuggestion:
    chronology: str
    climate: str
    source: str
    var: str
    window: Tuple[int, int]
    agg: str
    r: float
    n: int
    param: str
    reason: str
    same_variable: bool = True   # field is the same product and variable as the station/grid series

    def label(self) -> str:
        srcs = field_sources()
        src = srcs[self.source].label if self.source in srcs else self.source
        ch = self.chronology if len(self.chronology) <= 38 else self.chronology[:16] + "..." + self.chronology[-19:]
        return (f"{ch}: {window_label(*self.window)} {self.param} (r = {self.r:+.2f}, n = {self.n}) -> "
                f"{src}: {self.var}")


def field_for_climate(ci: ClimateInfo) -> List[Tuple[str, str, bool]]:
    """Candidate (source, var, same_variable) fields for a climate data set, best first."""
    out: List[Tuple[str, str, bool]] = []
    srcs = field_sources()
    low = f"{ci.source} {ci.name}".lower()
    skey = next((k for pat, k in _SOURCE_LABEL_TO_KEY if pat in low), None)
    code = (ci.variable or "").strip()
    if skey == "era5_cds_monthly" and "open-meteo" in low:
        skey = None        # Open-Meteo is a point service; the CDS field needs the user's key
    if skey in srcs and code:
        try:
            srcs[skey].var(code)
            out.append((skey, code, True))
        except KeyError:
            pass
    if skey == "speibase" or (code.lower().startswith("spei") and "speibase" in srcs):
        m = re.search(r"(\d+)", code)
        if m:
            c = f"spei{int(m.group(1)):02d}"
            if c in [v.code for v in srcs["speibase"].variables] and ("speibase", c, True) not in out:
                out.append(("speibase", c, skey == "speibase"))
    for s_, v_ in PARAM_TO_FIELDS.get(ci.param, []):
        if s_ in srcs and (s_, v_, True) not in out and (s_, v_, False) not in out:
            out.append((s_, v_, False))
    return out


def _results_frame(df: pd.DataFrame) -> pd.DataFrame:
    need = {"Chronology", "Climate", "Window", "R"}
    if df is None or df.empty or not need.issubset(df.columns):
        return pd.DataFrame()
    d = df.copy()
    d["R"] = pd.to_numeric(d["R"], errors="coerce")
    if "p" in d.columns:
        d["p"] = pd.to_numeric(d["p"], errors="coerce")
    else:
        d["p"] = np.nan
    if "N" in d.columns:
        d["N"] = pd.to_numeric(d["N"], errors="coerce")
    else:
        d["N"] = np.nan
    if "Agg" not in d.columns:
        d["Agg"] = "mean"
    d = d.dropna(subset=["R"])
    d["absR"] = d["R"].abs()
    d["win"] = d["Window"].map(parse_window)
    return d


def summarize_results(df: pd.DataFrame, climate: Dict[str, ClimateInfo], min_n: int = 20,
                      top: int = 10) -> Dict[str, Any]:
    """Systematise a RingClim results table: best pairs, best chronology, signal by variable type."""
    d = _results_frame(df)
    if d.empty:
        return {"text": "No correlation results yet. Run the analysis in the Analysis tab first, or set the "
                        "field correlation by hand.", "best": pd.DataFrame(), "by_family": pd.DataFrame()}
    d_ok = d[(d["N"].isna()) | (d["N"] >= min_n)]
    if d_ok.empty:
        d_ok = d
    d_ok = d_ok.copy()
    d_ok["param"] = d_ok["Climate"].map(lambda c: climate.get(c, ClimateInfo(c)).param)
    d_ok["family"] = d_ok["param"].map(lambda p: PARAM_FAMILY.get(p, "other"))
    best = (d_ok.sort_values("absR", ascending=False).groupby(["Chronology", "Climate"], as_index=False).head(1)
            .sort_values("absR", ascending=False))
    lines: List[str] = []
    n_chron = d_ok["Chronology"].nunique()
    n_clim = d_ok["Climate"].nunique()
    lines.append(f"{len(d):,} correlations of {n_chron} chronolog{'y' if n_chron == 1 else 'ies'} with {n_clim} "
                 f"climate data set(s); windows with at least {min_n} years are used below.")
    # strongest chronology
    if n_chron > 1:
        per_chron = best.groupby("Chronology")["absR"].agg(["max", "mean"]).sort_values("max", ascending=False)
        c0 = per_chron.index[0]
        lines.append(f"Strongest chronology: {c0} (max |r| = {per_chron.iloc[0]['max']:.2f}, mean of the best "
                     f"|r| per data set = {per_chron.iloc[0]['mean']:.2f}).")
        if len(per_chron) > 1:
            lines.append("Next: " + "; ".join(f"{k} ({v['max']:.2f})" for k, v in per_chron.iloc[1:4].iterrows()) + ".")
    # by family: sign consistency of the best window per data set
    fam_rows = []
    for fam, g in best.groupby("family"):
        g = g.sort_values("absR", ascending=False)
        pos = int((g["R"] > 0).sum())
        neg = int((g["R"] < 0).sum())
        top_row = g.iloc[0]
        fam_rows.append({"family": fam, "datasets": len(g), "positive": pos, "negative": neg,
                         "best_r": float(top_row["R"]), "best_climate": top_row["Climate"],
                         "best_window": top_row["Window"], "best_n": top_row["N"]})
    by_family = pd.DataFrame(fam_rows).sort_values("best_r", key=lambda s: -s.abs()) if fam_rows else pd.DataFrame()
    if not by_family.empty:
        lines.append("Signal by variable type (best window per data set):")
        for _i, r in by_family.iterrows():
            sign = "positive" if r["best_r"] > 0 else "negative"
            agree = max(r["positive"], r["negative"])
            lines.append(f"  - {r['family']}: strongest {r['best_r']:+.2f} ({r['best_climate']}, {r['best_window']}); "
                         f"{agree} of {r['datasets']} data set(s) {sign}.")
    # season consensus: the most frequent best window among the top pairs
    top_pairs = best.head(max(top, 5))
    wins = [w for w in top_pairs["win"] if w is not None]
    if wins:
        months_hit = np.zeros(24)
        for si, ei in wins:
            months_hit[si:ei + 1] += 1
        core = [WINDOW_LABELS[i] for i in np.where(months_hit >= max(2, 0.5 * months_hit.max()))[0]]
        if core:
            lines.append("Months that recur in the strongest windows: " + ", ".join(core) + ".")
    lines.append("Top pairs:")
    for _i, r in best.head(top).iterrows():
        n_txt = "" if pd.isna(r["N"]) else f", n = {int(r['N'])}"
        p_txt = "" if pd.isna(r["p"]) else f", p = {r['p']:.3g}"
        lines.append(f"  {r['R']:+.2f}  {r['Chronology']}  x  {r['Climate']}  [{r['Window']}, {r['Agg']}{n_txt}{p_txt}]")
    lines.append("Short records (stations, CHELSA from 1979) give larger r by chance; compare products with "
                 "similar n, and check a signal on the map before interpreting it.")
    return {"text": "\n".join(lines), "best": best.drop(columns=["absR"]), "by_family": by_family}


def suggest_fields(df: pd.DataFrame, climate: Dict[str, ClimateInfo], max_suggestions: int = 8,
                   min_n: int = 20) -> List[FieldSuggestion]:
    """Ready field-correlation settings for the strongest monthly windows of the results table."""
    d = _results_frame(df)
    if d.empty:
        return []
    d = d[d["win"].notna()]
    d_ok = d[(d["N"].isna()) | (d["N"] >= min_n)]
    if not d_ok.empty:
        d = d_ok
    # rank by the strength of the evidence (Fisher z * sqrt(n - 3)): long records first
    nn = d["N"].fillna(30).clip(lower=4)
    d = d.assign(_z=np.arctanh(d["absR"].clip(upper=0.999)) * np.sqrt(nn - 3)).sort_values("_z", ascending=False)
    out: List[FieldSuggestion] = []
    seen = set()
    for _i, r in d.iterrows():
        ci = climate.get(r["Climate"], ClimateInfo(str(r["Climate"])))
        cands = field_for_climate(ci)
        if not cands:
            continue
        src, var, same = cands[0]
        key = (r["Chronology"], src, var)
        if key in seen:
            continue
        seen.add(key)
        agg = str(r.get("Agg") or "mean").lower()
        if agg not in ("mean", "sum"):
            try:
                agg = field_sources()[src].var(var).agg
            except Exception:
                agg = "mean"
        reason = (f"{r['Climate']} {r['Window']}: r = {r['R']:+.2f}"
                  + ("" if pd.isna(r["N"]) else f", n = {int(r['N'])}")
                  + ("" if same else f" -> nearest gridded equivalent ({src} {var})"))
        out.append(FieldSuggestion(str(r["Chronology"]), str(r["Climate"]), src, var, tuple(r["win"]), agg,
                                   float(r["R"]), int(r["N"]) if not pd.isna(r["N"]) else 0, ci.param, reason,
                                   same))
        if len(out) >= max_suggestions:
            break
    return out


def default_region(lat: float, lon: float, half_lat: float = 12.0, half_lon: float = 18.0
                   ) -> Tuple[float, float, float, float]:
    return (max(-89.75, lat - half_lat), min(89.75, lat + half_lat),
            max(-179.75, lon - half_lon), min(179.75, lon + half_lon))


# ===========================================================================
# Command line
# ===========================================================================

def _cli_progress(stage: str, done: int, total: int, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Spatial correlation of a chronology with a gridded field")
    ap.add_argument("--source", default="cru")
    ap.add_argument("--var", default="tmp")
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("MINLAT", "MAXLAT", "MINLON", "MAXLON"))
    ap.add_argument("--years", nargs=2, type=int, required=True)
    ap.add_argument("--chron", required=True, help="CSV with Year and one value column")
    ap.add_argument("--window", nargs=2, default=["pJul", "pSep"])
    ap.add_argument("--agg", default="mean")
    ap.add_argument("--method", default="pearson")
    ap.add_argument("--site", nargs=2, type=float)
    ap.add_argument("--file", default="", help="NetCDF file for --source netcdf")
    ap.add_argument("--png", default="field_correlation.png")
    a = ap.parse_args(argv)
    df = pd.read_csv(a.chron)
    ycol = next(c for c in df.columns if str(c).lower() in ("year", "years", "unnamed: 0"))
    vcol = [c for c in df.columns if c != ycol][0]
    chron = pd.Series(df[vcol].values, index=df[ycol].astype(int))
    f = load_field(a.source, a.var, a.bbox, a.years[0] - 1, a.years[1], _cli_progress,
                   options={"path": a.file})
    w = (WINDOW_LABELS.index(a.window[0]), WINDOW_LABELS.index(a.window[1]))
    site = (a.site[0], a.site[1], "site") if a.site else None
    cm = correlate_field(chron, f, w, a.agg, a.years[0], a.years[1], a.method, site=site,
                         chron_name=os.path.basename(a.chron))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(10, 7))
    draw_corr_map(fig, cm)
    fig.savefig(a.png, dpi=150, bbox_inches="tight")
    print(json.dumps(cm.stats, indent=1, default=str))
    print("saved", a.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ===========================================================================
# Screening: every window of months for one field (which season / variable gives the regional signal?)
# ===========================================================================

def all_windows(max_len: int = 12) -> List[Tuple[int, int]]:
    out = []
    for length in range(1, int(max_len) + 1):
        for si in range(0, 24 - length + 1):
            out.append((si, si + length - 1))
    return out


def _lag1(a: np.ndarray) -> np.ndarray:
    """Lag-1 autocorrelation along axis 0 (NaN-aware, columns)."""
    x0, x1 = a[:-1], a[1:]
    ok = np.isfinite(x0) & np.isfinite(x1)
    with np.errstate(all="ignore"):
        m0 = np.where(ok, x0, 0).sum(0) / ok.sum(0)
        m1 = np.where(ok, x1, 0).sum(0) / ok.sum(0)
        c = np.where(ok, (x0 - m0) * (x1 - m1), 0).sum(0)
        v = np.sqrt(np.where(ok, (x0 - m0) ** 2, 0).sum(0) * np.where(ok, (x1 - m1) ** 2, 0).sum(0))
        return c / v


def _cell_area_km2(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    dlat = abs(float(lats[1] - lats[0])) if len(lats) > 1 else 0.5
    dlon = abs(float(lons[1] - lons[0])) if len(lons) > 1 else 0.5
    return (dlat * 111.2) * (dlon * 111.2 * np.cos(np.radians(lats)))[:, None] * np.ones((1, len(lons)))


def _site_component(sig_same: np.ndarray, iy: int, ix: int) -> np.ndarray:
    """Connected (8-neighbour) significant area that contains the site cell or touches it."""
    from scipy import ndimage

    lab, _n = ndimage.label(sig_same, structure=np.ones((3, 3)))
    ids = set(lab[max(0, iy - 1):iy + 2, max(0, ix - 1):ix + 2].ravel()) - {0}
    if not ids:
        return np.zeros_like(sig_same, dtype=bool)
    return np.isin(lab, list(ids))


def screen_field_windows(chron: pd.Series, f: Field, site: Tuple[float, float], y0: Optional[int] = None,
                         y1: Optional[int] = None, max_len: int = 12, method: str = "pearson", prep: str = "none",
                         alpha: float = 0.05, min_n: int = 20, radius_km: float = 500.0, n_perm: int = 50,
                         eff_n: bool = True, seed: int = 11, progress: ProgressCallback = _noop,
                         cancel: CancelCheck = _never) -> Tuple[pd.DataFrame, np.ndarray]:
    """Correlate a chronology with every window of months of one field and rank the windows.

    The main measure is the significant area (same sign as r at the site) that is connected with the site -
    a coherent regional signal around the site is more convincing than single cells or distant patches
    (KNMI Climate Explorer advice). Significance uses a t-test; with ``eff_n`` the number of years is reduced
    for serial correlation like the Climate Explorer: n_eff = n (1 - r1x r1y) / (1 + r1x r1y).

    Returns (rows, null) - ``null`` holds the best connected area (km2) of every shuffle of the chronology
    over all windows (to compare across fields: the larger the better).
    """
    from scipy import ndimage  # noqa: F401  (required)

    s = pd.to_numeric(pd.Series(chron), errors="coerce").dropna()
    s.index = pd.Index(s.index).astype(int)
    a = max(int(s.index.min()), int(f.years.min()) + 1, int(y0) if y0 else -10 ** 6)
    b = min(int(s.index.max()), int(f.years.max()), int(y1) if y1 else 10 ** 6)
    period = f"{a}-{b}"
    if b - a + 1 < max(10, min_n):
        raise ValueError(f"Too few common years ({max(0, b - a + 1)}) between the chronology and the field")
    years = np.arange(a, b + 1)
    nlat, nlon = f.data.shape[1], f.data.shape[2]
    pos = {(int(y), int(m)): k for k, (y, m) in enumerate(zip(f.years, f.months))}
    Y = len(years)
    X24 = np.full((Y, 24, nlat, nlon), np.nan, dtype=np.float32)
    for k, yy in enumerate(years):
        for m in range(24):
            t = pos.get((int(yy) - 1 if m < 12 else int(yy), (m % 12) + 1))
            if t is not None:
                X24[k, m] = f.data[t]
    ok = np.isfinite(X24)
    cs = np.concatenate([np.zeros((Y, 1, nlat, nlon), np.float32), np.cumsum(np.where(ok, X24, 0), axis=1)], 1)
    cn = np.concatenate([np.zeros((Y, 1, nlat, nlon), np.int16), np.cumsum(ok, axis=1, dtype=np.int16)], 1)
    del X24, ok
    x = s.reindex(years).to_numpy(dtype=float)
    t = years.astype(float)
    if prep == "detrend":
        x = _detrend_cols(x[:, None], t)[:, 0]
    elif prep == "diff":
        x = np.concatenate([[np.nan], np.diff(x)])
    if method == "spearman":
        x = pd.Series(x).rank().to_numpy()
    lat_i = int(np.argmin(np.abs(f.lats - site[0])))
    lon_i = int(np.argmin(np.abs(f.lons - site[1])))
    area = _cell_area_km2(f.lats, f.lons)
    la, lo = np.meshgrid(f.lats, f.lons, indexing="ij")
    dist = 6371.0 * np.arccos(np.clip(np.sin(np.radians(site[0])) * np.sin(np.radians(la)) +
                                      np.cos(np.radians(site[0])) * np.cos(np.radians(la)) *
                                      np.cos(np.radians(lo - site[1])), -1, 1))
    near = dist <= radius_km
    windows = all_windows(max_len)
    rng = np.random.default_rng(seed)
    xo = np.isfinite(x)
    perms = []
    for _b in range(int(n_perm)):
        xp = x.copy()
        xp[xo] = rng.permutation(x[xo])
        perms.append(xp)
    perms = np.array(perms) if perms else np.zeros((0, Y))
    null_best = np.zeros(len(perms))
    rows: List[Dict[str, Any]] = []
    rx1 = _lag1(x[:, None])[0] if eff_n else 0.0
    chunk = max(1, int(8.0e6 // max(1, Y * nlat * nlon)))  # keeps the memory of one chunk below ~0.5 GB
    for c0 in range(0, len(windows), chunk):
        if cancel():
            raise DO.CancelledError("Cancelled")
        ws = windows[c0:c0 + chunk]
        progress("screen", c0, len(windows), f"Windows {c0 + 1}-{c0 + len(ws)} of {len(windows)} ...")
        si = np.array([w[0] for w in ws])
        ei = np.array([w[1] for w in ws]) + 1
        L = (ei - si).astype(np.float32)
        S = (cs[:, ei] - cs[:, si]) / L[None, :, None, None]           # (Y, W, lat, lon)
        S[(cn[:, ei] - cn[:, si]) < L[None, :, None, None].astype(np.int16)] = np.nan
        flat = S.reshape(Y, -1).astype(float)
        if prep == "detrend":
            flat = _detrend_cols(flat, t)
        elif prep == "diff":
            flat = np.vstack([np.full((1, flat.shape[1]), np.nan), np.diff(flat, axis=0)])
        if method == "spearman":
            flat = pd.DataFrame(flat).rank().to_numpy()
        v = np.isfinite(flat) & np.isfinite(x)[:, None]
        n = v.sum(0)
        with np.errstate(all="ignore"):
            xm = np.where(v, x[:, None], 0).sum(0) / n
            ym = np.where(v, flat, 0).sum(0) / n
            dx = np.where(v, x[:, None] - xm, 0)
            dy = np.where(v, flat - ym, 0)
            r = (dx * dy).sum(0) / np.sqrt((dx ** 2).sum(0) * (dy ** 2).sum(0))
            r = np.clip(r, -1, 1)
            ne = n.astype(float)
            if eff_n:
                ry1 = _lag1(flat)
                fac = (1 - rx1 * ry1) / (1 + rx1 * ry1)
                ne = np.clip(ne * np.where(np.isfinite(fac), np.clip(fac, 0, 1), 1.0), 3, n)
            tt = r * np.sqrt((ne - 2) / np.maximum(1e-12, 1 - r * r))
        r[n < min_n] = np.nan
        p = np.full(r.shape, np.nan)
        okr = np.isfinite(r)
        if okr.any():
            p[okr] = _t_sf_two_sided(tt[okr], ne[okr] - 2)
        r = r.reshape(len(ws), nlat, nlon)
        p = p.reshape(len(ws), nlat, nlon)
        # permutations: complete cells only, plain n (same rule for the observed control value)
        full = n.reshape(len(ws), nlat, nlon) == np.isfinite(x).sum()
        rc = None
        if len(perms):
            nn = int(np.isfinite(x).sum())
            tcrit = None
            try:
                from scipy import stats as _st

                tcrit = float(_st.t.ppf(1 - alpha / 2, nn - 2))
            except Exception:
                tcrit = 1.96
            rc = tcrit / math.sqrt(nn - 2 + tcrit * tcrit)
            Z = np.where(v, flat, np.nan)
            with np.errstate(all="ignore"):
                Z = (Z - np.nanmean(Z, 0)) / np.nanstd(Z, 0)
            Z = np.where(full.reshape(-1)[None, :] & np.isfinite(Z), Z, 0.0)
            P = perms[:, np.isfinite(x)]
            P = (P - P.mean(1, keepdims=True)) / P.std(1, keepdims=True)
            Zs = Z[np.isfinite(x)]
            R = (P @ Zs) / Zs.shape[0]                                  # (n_perm, W*cells)
            R = R.reshape(len(perms), len(ws), nlat, nlon)
            for bi in range(len(perms)):
                for k in range(len(ws)):
                    rr = R[bi, k]
                    sgn = 1.0 if rr[lat_i, lon_i] >= 0 else -1.0
                    sig_same = full[k] & (np.abs(rr) >= rc) & (np.sign(rr) == sgn)
                    comp = _site_component(sig_same, lat_i, lon_i)
                    null_best[bi] = max(null_best[bi], float(area[comp].sum()))
        for k, (a0, a1) in enumerate(ws):
            rk, pk = r[k], p[k]
            okk = np.isfinite(rk)
            if not okk.any() or not np.isfinite(rk[lat_i, lon_i]):
                continue
            sgn = 1.0 if rk[lat_i, lon_i] >= 0 else -1.0
            sig = okk & (pk < alpha)
            sig_same = sig & (np.sign(rk) == sgn)
            comp = _site_component(sig_same, lat_i, lon_i)
            w_all = area[okk].sum()
            ctrl = 0.0
            if rc is not None:
                sctrl = full[k] & (np.abs(rk) >= rc) & (np.sign(rk) == sgn)
                ctrl = float(area[_site_component(sctrl, lat_i, lon_i)].sum())
            nr = near & okk
            j = int(np.nanargmax(np.where(okk, np.abs(rk), -1)))
            rows.append({
                "Window": window_label(a0, a1), "si": a0, "ei": a1,
                "Area around site (km2)": float(area[comp].sum()),
                "% of region significant": round(100.0 * float(area[sig].sum()) / w_all, 1),
                "% same sign": round(100.0 * float(area[sig_same].sum()) / w_all, 1),
                "r at site": float(rk[lat_i, lon_i]), "p at site": float(pk[lat_i, lon_i]),
                f"Mean r within {int(radius_km)} km": float(np.nanmean(rk[nr])) if nr.any() else float("nan"),
                "Max |r|": float(rk.ravel()[j]), "Max at": f"{la.ravel()[j]:.2f} N {lo.ravel()[j]:.2f} E",
                "_control_km2": ctrl, "N": int(np.nanmax(n)),
            })
    progress("screen", len(windows), len(windows), "Screening finished")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["Period"] = period
        df = df.sort_values(["Area around site (km2)", "r at site"], ascending=[False, False],
                            key=lambda c: c.abs() if c.name == "r at site" else c).reset_index(drop=True)
    return df, null_best


def finish_screening(parts: List[Tuple[str, str, pd.DataFrame]], nulls: List[np.ndarray]) -> pd.DataFrame:
    """Combine the screenings of several variables; p (shuffled) from the best area of every shuffle."""
    frames = []
    for label, var, df in parts:
        if df is None or df.empty:
            continue
        d = df.copy()
        d.insert(0, "Variable", var)
        d.insert(0, "Field", label)
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if nulls and all(len(nl) for nl in nulls):
        null = np.max(np.vstack(nulls), axis=0)  # best over all variables and windows, per shuffle
        out["p (shuffled)"] = [float((np.sum(null >= c) + 1) / (len(null) + 1)) for c in out["_control_km2"]]
        out["Chance best area (95%, km2)"] = float(np.percentile(null, 95))
    out = out.sort_values(["Area around site (km2)", "r at site"], ascending=[False, False],
                          key=lambda c: c.abs() if c.name == "r at site" else c).reset_index(drop=True)
    return out
