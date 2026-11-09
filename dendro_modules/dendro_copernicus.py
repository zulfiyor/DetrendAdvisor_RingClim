"""
dendro_copernicus.py - Copernicus data stores (CDS and EWDS) for RingClim / DeClim.

Every user works with his or her own free Copernicus account and personal access token
("API key"). Nothing is shared or built in: the token is entered once in the program
("API keys...") and saved only on this computer (see keys_path()).

Data sets
---------
* ERA5 monthly averaged reanalysis (CDS, 0.25 deg, 1940-)        reanalysis-era5-single-levels-monthly-means
* ERA5-Land monthly averaged reanalysis (CDS, 0.1 deg, 1950-)     reanalysis-era5-land-monthly-means
* GloFAS v4 historical river discharge (EWDS, 0.05 deg, 1979-)    cems-glofas-historical

How it works
------------
The data stores take a request, put it in a queue, compute it and give back a file. The
retrieve API (OGC API - Processes) is used directly with HTTPS, so no extra package is needed:

    POST {api}/retrieve/v1/processes/{dataset}/execution   {"inputs": request}   -> jobID
    GET  {api}/retrieve/v1/jobs/{jobID}                                           -> status
    GET  {api}/retrieve/v1/jobs/{jobID}/results                                   -> file link
    DELETE {api}/retrieve/v1/jobs/{jobID}                                         (cancel)

Only a small area around the location(s) is requested. Results are kept in the cache
(the same request is not sent twice).

Before the first download of a data set its licence must be accepted once on the data set
page (tab "Download", at the bottom); the program shows the link when the server asks for it.

Command line (for testing on your computer):
    python dendro_copernicus.py check                      test the saved keys
    python dendro_copernicus.py era5 --lat 40.28 --lon 69.62 --years 2000 2001
    python dendro_copernicus.py glofas --lat 40.28 --lon 69.62 --years 2000 2000
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import os
import shutil
import sys
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import dendro_online as DO

ProgressCallback = Callable[[str, int, int, str], None]
CancelCheck = Callable[[], bool]


def _noop(*_a: Any) -> None:
    return None


def _never() -> bool:
    return False


# ===========================================================================
# Services and keys
# ===========================================================================

@dataclass(frozen=True)
class Service:
    key: str
    label: str
    api: str
    site: str
    register_url: str
    token_url: str


SERVICES: Dict[str, Service] = {
    "cds": Service("cds", "Copernicus Climate Data Store (CDS)", "https://cds.climate.copernicus.eu/api",
                   "https://cds.climate.copernicus.eu",
                   "https://cds.climate.copernicus.eu/",
                   "https://cds.climate.copernicus.eu/profile"),
    "ewds": Service("ewds", "Copernicus Early Warning Data Store (EWDS)", "https://ewds.climate.copernicus.eu/api",
                    "https://ewds.climate.copernicus.eu",
                    "https://ewds.climate.copernicus.eu/",
                    "https://ewds.climate.copernicus.eu/profile"),
}


class KeyMissingError(DO.OnlineError):
    """No personal access token for a service."""


class LicenceError(DO.OnlineError):
    """The data set licence has not been accepted yet."""

    def __init__(self, message: str, url: str) -> None:
        super().__init__(message)
        self.url = url


class AuthError(DO.OnlineError):
    """The token was rejected."""


def config_dir() -> str:
    """Per-user settings folder (not the program folder, so keys never travel with the program)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "DeClim")
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "DeClim")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        path = os.path.join(base, "DeClim")
    os.makedirs(path, exist_ok=True)
    return path


def keys_path() -> str:
    return os.path.join(config_dir(), "api_keys.json")


_KEYS_LOCK = threading.Lock()


def _read_keys_file() -> Dict[str, Any]:
    try:
        with open(keys_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_key(service: str, key: str) -> None:
    """Save (or remove, when empty) the token of a service in the per-user keys file."""
    with _KEYS_LOCK:
        data = _read_keys_file()
        key = (key or "").strip()
        if key:
            data[service] = {"key": key}
        else:
            data.pop(service, None)
        path = keys_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def _cdsapirc_key(service: str) -> str:
    """Token from ~/.cdsapirc (or %USERPROFILE%\\.cdsapirc) when it points to this service."""
    path = os.environ.get("CDSAPI_RC") or os.path.join(os.path.expanduser("~"), ".cdsapirc")
    url, key = "", ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "url":
                    url = v
                elif k == "key":
                    key = v
    except OSError:
        return ""
    host = urllib.parse.urlparse(SERVICES[service].api).netloc
    if key and host in url and ":" not in key:  # "uid:key" = old CDS API, no longer valid
        return key
    return ""


def get_key(service: str) -> Tuple[str, str]:
    """(token, where it comes from) for a service; ("", "") when there is none.

    Order: saved in the program, environment variable (CDSAPI_KEY with CDSAPI_URL of the
    service), ~/.cdsapirc. EWDS uses the CDS token when it has none of its own (both data
    stores use the same ECMWF account).
    """
    data = _read_keys_file()
    k = str((data.get(service) or {}).get("key") or "").strip()
    if k:
        return k, "saved in the program"
    env_key = os.environ.get("CDSAPI_KEY", "").strip()
    env_url = os.environ.get("CDSAPI_URL", "")
    if env_key and urllib.parse.urlparse(SERVICES[service].api).netloc in env_url:
        return env_key, "environment variable CDSAPI_KEY"
    k = _cdsapirc_key(service)
    if k:
        return k, ".cdsapirc file"
    if service == "ewds":
        k, origin = get_key("cds")
        if k:
            return k, f"CDS key ({origin})"
    return "", ""


def require_key(service: str) -> str:
    key, _origin = get_key(service)
    if not key:
        s = SERVICES[service]
        raise KeyMissingError(
            f"No personal access token for the {s.label}. Register for free at {s.register_url}, copy the "
            f"token from {s.token_url} and enter it with 'API keys...'.")
    return key


def mask_key(key: str) -> str:
    key = key or ""
    return key[:4] + "..." + key[-4:] if len(key) > 10 else ("*" * len(key))


# ===========================================================================
# HTTP
# ===========================================================================

def _session():
    try:
        import requests  # noqa: WPS433
    except ImportError:
        return None
    s = requests.Session()
    s.headers.update({"User-Agent": DO.USER_AGENT})
    return s


def _call(method: str, url: str, key: str, payload: Optional[Dict[str, Any]] = None,
          timeout: float = 60.0) -> Tuple[int, Any, str]:
    """(status, parsed JSON or None, text)."""
    headers = {"PRIVATE-TOKEN": key, "Accept": "application/json"}
    s = _session()
    if s is not None:
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                r = s.request(method, url, headers=headers, json=payload, timeout=timeout)
            except Exception as exc:  # connection problems: retry
                last = exc
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(5.0 * (attempt + 1))
                continue
            text = r.text or ""
            try:
                body = r.json()
            except ValueError:
                body = None
            return r.status_code, body, text
        raise DO.OnlineError(f"Cannot reach {urllib.parse.urlparse(url).netloc}: {last}")
    # urllib fallback
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, "User-Agent": DO.USER_AGENT,
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=DO._ssl_context()) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        status = exc.code
    except Exception as exc:
        raise DO.OnlineError(f"Cannot reach {urllib.parse.urlparse(url).netloc}: {exc}") from exc
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    return status, body, text


def _error_text(body: Any, text: str) -> str:
    if isinstance(body, dict):
        parts = [str(body.get(k)) for k in ("title", "detail", "message") if body.get(k)]
        if parts:
            return " - ".join(dict.fromkeys(parts))
    return (text or "").strip()[:400]


def licence_url(service: str, dataset: str) -> str:
    return f"{SERVICES[service].site}/datasets/{dataset}?tab=download#manage-licences"


def check_key(service: str, key: Optional[str] = None, timeout: float = 30.0) -> Tuple[bool, str]:
    """Test a token: (ok, message)."""
    key = (key if key is not None else get_key(service)[0]).strip()
    if not key:
        return False, "No key entered."
    s = SERVICES[service]
    try:
        status, body, text = _call("GET", f"{s.api}/retrieve/v1/jobs?limit=1", key, timeout=timeout)
    except DO.OnlineError as exc:
        return False, str(exc)
    if status == 200:
        return True, f"The key works ({s.label})."
    if status in (401, 403):
        return False, f"The key was rejected ({status}): {_error_text(body, text)}"
    return False, f"Unexpected answer {status}: {_error_text(body, text)}"


# ===========================================================================
# Jobs
# ===========================================================================

@dataclass
class Job:
    service: str
    dataset: str
    request: Dict[str, Any]
    dest: str                       # local file (cache)
    label: str = ""
    job_id: str = ""
    status: str = "new"             # new / accepted / running / successful / failed / done / cached
    message: str = ""
    size: int = 0
    # other request forms tried when the server answers "no valid combination of values" (400)
    alternatives: List[Dict[str, Any]] = field(default_factory=list)


def request_hash(dataset: str, request: Dict[str, Any]) -> str:
    raw = json.dumps({"dataset": dataset, "request": request}, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cache_dir(base: Optional[str] = None) -> str:
    d = os.path.join(base or DO.default_cache_dir(), "copernicus")
    os.makedirs(d, exist_ok=True)
    return d


def _submit(job: Job, key: str) -> None:
    s = SERVICES[job.service]
    url = f"{s.api}/retrieve/v1/processes/{job.dataset}/execution"
    tried = [job.request] + list(job.alternatives or [])
    for k, req in enumerate(tried):
        status, body, text = _call("POST", url, key, {"inputs": req}, timeout=90.0)
        if status in (200, 201) and isinstance(body, dict) and body.get("jobID"):
            job.job_id = str(body["jobID"])
            job.status = str(body.get("status") or "accepted")
            job.request = req
            return
        msg = _error_text(body, text)
        combo = status == 400 and "combination" in (msg + text).lower()
        if not (combo and k + 1 < len(tried)):
            break
    msg = _error_text(body, text)
    if status == 400 and "combination" in (msg + text).lower():
        msg += (f" [{job.label or job.dataset}; tried {len(tried)} request form(s); last request: "
                + ", ".join(f"{k}={v if not isinstance(v, list) or len(v) < 4 else str(v[:2])[:-1] + ', ...]'}"
                            for k, v in tried[-1].items()) + "]")
    if status == 401:
        raise AuthError(f"{s.label}: the API key was rejected. Check it with 'API keys...'. ({msg})")
    if status == 403 and ("licen" in msg.lower() or "licen" in (text or "").lower()):
        url_l = licence_url(job.service, job.dataset)
        raise LicenceError(f"{s.label}: the licence of '{job.dataset}' has not been accepted yet. Open {url_l}, "
                           f"log in, accept the licence at the bottom of the 'Download' tab and try again.", url_l)
    raise DO.OnlineError(f"{s.label}: request for {job.dataset} refused ({status}): {msg}")


def _poll(job: Job, key: str) -> None:
    s = SERVICES[job.service]
    status, body, text = _call("GET", f"{s.api}/retrieve/v1/jobs/{job.job_id}", key)
    if status == 200 and isinstance(body, dict):
        job.status = str(body.get("status") or job.status)
    elif status == 404:
        job.status = "failed"
        job.message = "the request disappeared from the queue"
    else:
        job.message = _error_text(body, text)


def _result_link(job: Job, key: str) -> Tuple[str, int]:
    s = SERVICES[job.service]
    status, body, text = _call("GET", f"{s.api}/retrieve/v1/jobs/{job.job_id}/results", key)
    if status == 200 and isinstance(body, dict):
        asset = (body.get("asset") or {}).get("value") or {}
        href = asset.get("href")
        if href:
            return urllib.parse.urljoin(s.api + "/", str(href)), int(asset.get("file:size") or 0)
    raise DO.OnlineError(f"{s.label}: {job.label or job.dataset} failed: {_error_text(body, text)}")


def _delete(job: Job, key: str) -> None:
    if not job.job_id:
        return
    try:
        _call("DELETE", f"{SERVICES[job.service].api}/retrieve/v1/jobs/{job.job_id}", key, timeout=20.0)
    except Exception:
        pass


def run_jobs(jobs: Sequence[Job], progress: ProgressCallback = _noop, cancel: CancelCheck = _never,
             max_active: int = 4, poll_seconds: float = 6.0) -> List[Job]:
    """Submit, wait for and download all jobs (a few at the same time). Cached files are re-used.

    Raises the first licence / key error immediately (all other jobs would fail the same way);
    other failures are kept in job.status = "failed" / job.message.
    """
    keys = {j.service: require_key(j.service) for j in jobs}
    todo = []
    for j in jobs:
        if os.path.exists(j.dest) and os.path.getsize(j.dest) > 0:
            j.status = "cached"
        else:
            todo.append(j)
    n_all = len(jobs)

    def n_finished() -> int:
        return sum(1 for j in jobs if j.status in ("cached", "done", "failed"))

    active: List[Job] = []
    waiting = list(todo)
    t_start = time.time()
    try:
        while waiting or active:
            if cancel():
                raise DO.CancelledError("Cancelled")
            while waiting and len(active) < max_active:
                j = waiting.pop(0)
                _submit(j, keys[j.service])
                active.append(j)
            progress("queue", n_finished(), n_all,
                     f"Copernicus: {n_finished()}/{n_all} file(s) ready, {len(active)} request(s) in the queue "
                     f"({', '.join(sorted({j.status for j in active}))}), {int(time.time() - t_start)} s")
            time.sleep(poll_seconds)
            for j in list(active):
                if cancel():
                    raise DO.CancelledError("Cancelled")
                _poll(j, keys[j.service])
                if j.status == "successful":
                    href, size = _result_link(j, keys[j.service])
                    j.size = size
                    progress("download", n_finished(), n_all, f"Downloading {j.label or j.dataset} "
                                                              f"({DO.human_size(size) if size else '?'}) ...")
                    tmp = j.dest + ".dl"
                    DO.download_file(href, tmp, progress=progress, cancel=cancel, stage="download")
                    os.replace(tmp, j.dest)
                    j.status = "done"
                    active.remove(j)
                    _delete(j, keys[j.service])  # free the place in the user's request list
                elif j.status in ("failed", "rejected", "dismissed"):
                    try:
                        _result_link(j, keys[j.service])
                    except DO.OnlineError as exc:
                        j.message = str(exc)
                    j.status = "failed"
                    active.remove(j)
    except BaseException:
        for j in active:  # do not leave requests running for nothing
            _delete(j, keys[j.service])
        raise
    progress("queue", n_all, n_all, "Copernicus: all files ready")
    return list(jobs)


# ===========================================================================
# Data sets
# ===========================================================================

@dataclass(frozen=True)
class CVar:
    code: str                 # code used in RingClim (and as SourceVar code)
    request_names: Tuple[str, ...]   # variable names in the request
    nc_names: Tuple[str, ...]        # possible variable names in the NetCDF file
    convert: str              # "K", "m_month", "evap_month", "m_to_mm", "J_day", "none", "derived"


ERA5_MONTHLY_VARS: Dict[str, CVar] = {
    "t2m": CVar("t2m", ("2m_temperature",), ("t2m",), "K"),
    "tp": CVar("tp", ("total_precipitation",), ("tp",), "m_month"),
    "e": CVar("e", ("evaporation",), ("e",), "evap_month"),
    "pev": CVar("pev", ("potential_evaporation",), ("pev",), "evap_month"),
    "ro": CVar("ro", ("runoff",), ("ro",), "m_month"),
    "swvl1": CVar("swvl1", ("volumetric_soil_water_layer_1",), ("swvl1",), "none"),
    "swvl2": CVar("swvl2", ("volumetric_soil_water_layer_2",), ("swvl2",), "none"),
    "swvl3": CVar("swvl3", ("volumetric_soil_water_layer_3",), ("swvl3",), "none"),
    "sd": CVar("sd", ("snow_depth",), ("sd",), "m_to_mm"),
    "ssrd": CVar("ssrd", ("surface_solar_radiation_downwards",), ("ssrd",), "J_day"),
    "si10": CVar("si10", ("10m_wind_speed",), ("si10",), "none"),
    "vpd": CVar("vpd", ("2m_temperature", "2m_dewpoint_temperature"), ("t2m", "d2m"), "derived"),
    "rh": CVar("rh", ("2m_temperature", "2m_dewpoint_temperature"), ("t2m", "d2m"), "derived"),
}
ERA5LAND_MONTHLY_VARS: Dict[str, CVar] = {
    "t2m": CVar("t2m", ("2m_temperature",), ("t2m",), "K"),
    "tp": CVar("tp", ("total_precipitation",), ("tp",), "m_month"),
    "e": CVar("e", ("total_evaporation",), ("e",), "evap_month"),
    "pev": CVar("pev", ("potential_evaporation",), ("pev",), "evap_month"),
    "ro": CVar("ro", ("runoff",), ("ro",), "m_month"),
    "swvl1": CVar("swvl1", ("volumetric_soil_water_layer_1",), ("swvl1",), "none"),
    "swvl2": CVar("swvl2", ("volumetric_soil_water_layer_2",), ("swvl2",), "none"),
    "swvl3": CVar("swvl3", ("volumetric_soil_water_layer_3",), ("swvl3",), "none"),
    "sd": CVar("sd", ("snow_depth_water_equivalent",), ("sd",), "m_to_mm"),
    "ssrd": CVar("ssrd", ("surface_solar_radiation_downwards",), ("ssrd",), "J_day"),
    "vpd": CVar("vpd", ("2m_temperature", "2m_dewpoint_temperature"), ("t2m", "d2m"), "derived"),
    "rh": CVar("rh", ("2m_temperature", "2m_dewpoint_temperature"), ("t2m", "d2m"), "derived"),
}


@dataclass(frozen=True)
class CDataset:
    source: str               # key in dendro_sources.SOURCES
    service: str
    dataset: str
    res: float                # grid spacing, deg
    freq: str                 # "monthly" | "daily"
    variables: Dict[str, CVar] = field(default_factory=dict)


DATASETS: Dict[str, CDataset] = {
    "era5_cds_monthly": CDataset("era5_cds_monthly", "cds", "reanalysis-era5-single-levels-monthly-means",
                                 0.25, "monthly", ERA5_MONTHLY_VARS),
    "era5land_cds_monthly": CDataset("era5land_cds_monthly", "cds", "reanalysis-era5-land-monthly-means",
                                     0.1, "monthly", ERA5LAND_MONTHLY_VARS),
    "glofas": CDataset("glofas", "ewds", "cems-glofas-historical", 0.05, "daily",
                       {"dis24": CVar("dis24", ("river_discharge_in_the_last_24_hours",), ("dis24", "dis"), "none")}),
}


def dataset_for_source(source: str) -> CDataset:
    return DATASETS[source]


def _area_for_targets(targets: Sequence["DO.ExtractionTarget"], res: float, margin_cells: float) -> List[float]:
    """[N, W, S, E] covering every target plus a margin, snapped outward to the grid spacing."""
    m = margin_cells * res
    s_, n_, w_, e_ = 90.0, -90.0, 180.0, -180.0
    for t in targets:
        if t.mode == "bbox_mean" and t.bbox:
            a, b, c, d = t.bbox
        else:
            a, b, c, d = t.lat, t.lat, t.lon, t.lon
        s_, n_ = min(s_, a - m), max(n_, b + m)
        w_, e_ = min(w_, DO._wrap_lon(c) - m), max(e_, DO._wrap_lon(d) + m)

    def down(x: float) -> float:
        return math.floor(x / res) * res

    def up(x: float) -> float:
        return math.ceil(x / res) * res

    return [round(min(90.0, up(n_)), 4), round(max(-180.0, down(w_)), 4),
            round(max(-90.0, down(s_)), 4), round(min(180.0, up(e_)), 4)]


def _target_groups(targets: Sequence["DO.ExtractionTarget"], res: float, margin: float,
                   max_area_deg2: float) -> List[List["DO.ExtractionTarget"]]:
    """All targets in one area when it is small, otherwise one request area per target."""
    area = _area_for_targets(targets, res, margin)
    size = (area[0] - area[2]) * (area[3] - area[1])
    if len(targets) <= 1 or size <= max_area_deg2:
        return [list(targets)]
    return [[t] for t in targets]


def build_jobs(source: str, variables: Sequence[str], targets: Sequence["DO.ExtractionTarget"], y0: int, y1: int,
               options: Optional[Dict[str, Any]] = None, base_cache: Optional[str] = None) -> List[Tuple[Job, Any]]:
    """[(job, context)] for a request. Context = (request variable name, targets) or (year, target)."""
    ds = DATASETS[source]
    opts = options or {}
    out: List[Tuple[Job, Any]] = []
    cdir = cache_dir(base_cache)
    if ds.freq == "monthly":
        names: List[str] = []
        for v in variables:
            for nm in ds.variables[v].request_names:
                if nm not in names:
                    names.append(nm)
        for grp in _target_groups(targets, ds.res, 2, 400.0):
            area = _area_for_targets(grp, ds.res, 2)
            for nm in names:  # one variable per request: small, simple files
                req = {
                    "product_type": ["monthly_averaged_reanalysis"],
                    "variable": [nm],
                    "year": [str(y) for y in range(y0, y1 + 1)],
                    "month": [f"{m:02d}" for m in range(1, 13)],
                    "time": ["00:00"],
                    "area": area,
                    "data_format": "netcdf",
                    "download_format": "unarchived",
                }
                h = request_hash(ds.dataset, req)
                job = Job(ds.service, ds.dataset, req, os.path.join(cdir, f"{source}_{nm}_{h}.nc"),
                          label=f"{nm} {y0}-{y1}")
                out.append((job, (nm, grp)))
    else:  # GloFAS: one request per target and year (daily fields)
        snap = int(opts.get("glofas_snap_cells", 3))
        for t in targets:
            area = _area_for_targets([t], ds.res, snap + 1)
            for y in range(y0, y1 + 1):
                req = {
                    "system_version": ["version_4_0"],
                    "hydrological_model": ["lisflood"],
                    "product_type": ["consolidated"],
                    "variable": ["river_discharge_in_the_last_24_hours"],
                    "hyear": [str(y)],
                    "hmonth": [f"{m:02d}" for m in range(1, 13)],
                    "hday": [f"{d:02d}" for d in range(1, 32)],
                    "area": area,
                    "data_format": "netcdf",
                    "download_format": "unarchived",
                }
                h = request_hash(ds.dataset, req)
                # GloFAS v5.0 became operational in July 2026; recent days exist only as 'intermediate'
                alts = [dict(req, system_version=["version_5_0"]),
                        dict(req, product_type=["intermediate"]),
                        dict(req, system_version=["version_5_0"], product_type=["intermediate"])]
                job = Job(ds.service, ds.dataset, req, os.path.join(cdir, f"glofas_{y}_{h}.nc"),
                          label=f"GloFAS {t.name} {y}", alternatives=alts)
                out.append((job, (y, t)))
    return out


# ===========================================================================
# NetCDF reading
# ===========================================================================

@dataclass
class Grid:
    times: pd.DatetimeIndex
    lats: np.ndarray          # as in the file
    lons: np.ndarray
    values: np.ndarray        # (time, lat, lon)
    name: str


def _nc_files(path: str, work_dir: str) -> List[str]:
    """The file itself, or the NetCDF files inside it when the server sent a zip."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] != b"PK":
        return [path]
    out = []
    with zipfile.ZipFile(path) as z:
        for nm in z.namelist():
            if nm.lower().endswith((".nc", ".nc4", ".netcdf")):
                dst = os.path.join(work_dir, os.path.basename(nm))
                with z.open(nm) as src, open(dst, "wb") as fout:
                    shutil.copyfileobj(src, fout)
                out.append(dst)
    if not out:
        raise DO.OnlineError(f"No NetCDF file in {os.path.basename(path)}")
    return out


def read_grid(path: str, names: Sequence[str]) -> Grid:
    """First matching data variable of a CDS / EWDS NetCDF file (zip handled)."""
    from netCDF4 import Dataset, num2date

    work = DO._ascii_work_dir()
    try:
        src = path
        if not DO._is_ascii(os.path.abspath(path)):
            src = os.path.join(work, os.path.basename(path).encode("ascii", "replace").decode().replace("?", "_"))
            shutil.copyfile(path, src)
        files = _nc_files(src, work)
        grids = []
        with DO.NETCDF_LOCK:
            for f in files:
                with Dataset(f, "r") as nc:
                    vnames = list(nc.variables.keys())
                    lat_n = DO._find_coord_name(vnames, ("latitude", "lat"))
                    lon_n = DO._find_coord_name(vnames, ("longitude", "lon"))
                    t_n = DO._find_coord_name(vnames, ("valid_time", "time", "forecast_reference_time"))
                    var_n = DO._find_coord_name(vnames, tuple(n.lower() for n in names))
                    if var_n is None:
                        continue
                    if lat_n is None or lon_n is None or t_n is None:
                        raise DO.OnlineError(f"Coordinates not found in {os.path.basename(f)}")
                    tv = nc.variables[t_n]
                    dates = num2date(tv[:], units=tv.units, calendar=getattr(tv, "calendar", "standard"),
                                     only_use_cftime_datetimes=False, only_use_python_datetimes=True)
                    times = pd.to_datetime([str(d) for d in np.atleast_1d(dates)])
                    v = nc.variables[var_n]
                    v.set_auto_maskandscale(True)
                    arr = np.ma.filled(np.ma.asarray(v[:], dtype=float), np.nan)
                    dims = list(v.dimensions)
                    # move to (time, lat, lon); average over any other dimension (expver, number)
                    t_ax = next(i for i, d in enumerate(dims) if d in (t_n, "time", "valid_time"))
                    la_ax = next(i for i, d in enumerate(dims) if d == lat_n or "lat" in d.lower())
                    lo_ax = next(i for i, d in enumerate(dims) if d == lon_n or "lon" in d.lower())
                    other = [i for i in range(arr.ndim) if i not in (t_ax, la_ax, lo_ax)]
                    arr = np.transpose(arr, [t_ax, la_ax, lo_ax] + other)
                    if other:
                        with np.errstate(all="ignore"):
                            arr = np.nanmean(arr.reshape(arr.shape[:3] + (-1,)), axis=3)
                    arr[np.abs(arr) > 1e20] = np.nan
                    grids.append(Grid(times, np.asarray(nc.variables[lat_n][:], float),
                                      np.asarray(nc.variables[lon_n][:], float), arr, var_n))
        if not grids:
            raise DO.OnlineError(f"None of {', '.join(names)} found in {os.path.basename(path)}")
        if len(grids) == 1:
            return grids[0]
        g0 = grids[0]
        return Grid(pd.DatetimeIndex(np.concatenate([g.times.values for g in grids])), g0.lats, g0.lons,
                    np.concatenate([g.values for g in grids], axis=0), g0.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _cells(grid: Grid, t: "DO.ExtractionTarget") -> List["DO.GridCell"]:
    lats, lons = grid.lats, grid.lons
    lat_desc = len(lats) > 1 and lats[0] > lats[-1]
    lats_asc = lats[::-1] if lat_desc else lats
    lw = np.array([DO._wrap_lon(x) for x in lons])
    order = np.argsort(lw)
    lons_asc = lw[order]
    if t.mode == "bilinear" and (len(lats_asc) < 2 or len(lons_asc) < 2):
        t = DO.ExtractionTarget(t.name, t.lat, t.lon, "nearest")
    cells = DO.cells_for_target(t, lats_asc, lons_asc)
    for c in cells:
        c.i_lat = (len(lats) - 1 - c.i_lat) if lat_desc else c.i_lat
        c.i_lon = int(order[c.i_lon])
    return cells


def series_for_target(grid: Grid, t: "DO.ExtractionTarget") -> Tuple[np.ndarray, List["DO.GridCell"]]:
    cells = _cells(grid, t)
    if not cells:
        raise DO.OnlineError(f"No grid cells for target '{t.name}'")
    vals = np.stack([grid.values[:, c.i_lat, c.i_lon] for c in cells], axis=1)
    w = np.array([c.weight for c in cells])
    valid = np.isfinite(vals)
    wm = np.where(valid, w[None, :], 0.0)
    ws = wm.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        series = np.where(ws > 0, np.nansum(np.where(valid, vals, 0.0) * wm, axis=1) / ws, np.nan)
    for k, c in enumerate(cells):
        c.valid = bool(valid[:, k].any())
    return series, cells


def river_cell(grid: Grid, t: "DO.ExtractionTarget", snap_cells: int) -> Tuple[np.ndarray, List["DO.GridCell"]]:
    """GloFAS: the cell with the largest mean discharge near the point (or inside the box) = the river."""
    lats, lons = grid.lats, grid.lons
    lw = np.array([DO._wrap_lon(x) for x in lons])
    if t.mode == "bbox_mean" and t.bbox:
        a, b, c, d = t.bbox
    else:
        r = (snap_cells + 0.5) * float(np.median(np.abs(np.diff(lats)))) if len(lats) > 1 else 0.05
        a, b, c, d = t.lat - r, t.lat + r, DO._wrap_lon(t.lon) - r, DO._wrap_lon(t.lon) + r
    ii = np.where((lats >= a) & (lats <= b))[0]
    jj = np.where((lw >= c) & (lw <= d))[0]
    if len(ii) == 0 or len(jj) == 0:
        return series_for_target(grid, DO.ExtractionTarget(t.name, t.lat, t.lon, "nearest"))
    with np.errstate(all="ignore"):
        mean = np.nanmean(grid.values[:, ii][:, :, jj], axis=0)
    if not np.isfinite(mean).any():
        return series_for_target(grid, DO.ExtractionTarget(t.name, t.lat, t.lon, "nearest"))
    k = int(np.nanargmax(mean))
    i, j = int(ii[k // len(jj)]), int(jj[k % len(jj)])
    cell = DO.GridCell(float(lats[i]), float(lw[j]), 1.0, i, j, True)
    return grid.values[:, i, j].astype(float), [cell]


def _days(times: pd.DatetimeIndex) -> np.ndarray:
    return np.array([calendar.monthrange(d.year, d.month)[1] for d in times], dtype=float)


def _es_kpa(t_c: np.ndarray) -> np.ndarray:
    return 0.6108 * np.exp(17.27 * t_c / (t_c + 237.3))


def convert(var: CVar, values: np.ndarray, times: pd.DatetimeIndex) -> np.ndarray:
    v = np.asarray(values, float)
    if var.convert == "K":
        return v - 273.15
    if var.convert == "m_month":       # monthly means of daily accumulations (m/day) -> mm/month
        return v * 1000.0 * _days(times)
    if var.convert == "evap_month":    # ECMWF sign: evaporation is negative -> positive mm/month
        return -v * 1000.0 * _days(times)
    if var.convert == "m_to_mm":
        return v * 1000.0
    if var.convert == "J_day":         # J m-2 per day -> MJ m-2 d-1
        return v / 1.0e6
    return v


# ===========================================================================
# Request runner (called by dendro_sources.run_source_request)
# ===========================================================================

def run_request(req: Any, progress: ProgressCallback = _noop, cancel: CancelCheck = _never):
    """-> (results[target][var] as dendro_sources.VarResult, cell size, label, log lines)."""
    import dendro_sources as S

    ds = DATASETS[req.source]
    info = S.SOURCES[req.source]
    y0, y1 = req.start_year, req.end_year
    log: List[str] = []
    require_key(ds.service)
    jobs_ctx = build_jobs(req.source, req.variables, req.targets, y0, y1, req.options, req.cache_dir)
    n_cached = sum(1 for j, _c in jobs_ctx if os.path.exists(j.dest))
    log.append(f"{info.label}: {len(jobs_ctx)} request(s), {n_cached} already in the cache")
    run_jobs([j for j, _c in jobs_ctx], progress, cancel, max_active=int(req.options.get("cds_parallel", 4)))
    failed = [j for j, _c in jobs_ctx if j.status == "failed"]
    for j in failed:
        log.append(f"FAILED {j.label}: {j.message}")
    results: Dict[str, Dict[str, Any]] = {t.name: {} for t in req.targets}
    cell = (ds.res, ds.res)

    if ds.freq == "monthly":
        grids: Dict[Tuple[str, int], Grid] = {}   # (request name, id(group)) -> grid
        groups: List[List[Any]] = []
        for j, (nm, grp) in jobs_ctx:
            if j.status == "failed":
                continue
            if grp not in groups:
                groups.append(grp)
            progress("extract", 0, 0, f"Reading {os.path.basename(j.dest)} ...")
            cv = next((c for c in ds.variables.values() if c.request_names == (nm,)), None)
            nc_names = cv.nc_names if cv else ({"2m_dewpoint_temperature": ("d2m",)}.get(nm, (nm,)))
            grids[(nm, groups.index(grp))] = read_grid(j.dest, nc_names)
        for gi, grp in enumerate(groups):
            for t in grp:
                for v in req.variables:
                    cv = ds.variables[v]
                    try:
                        if cv.convert == "derived":
                            gt, gd = grids[("2m_temperature", gi)], grids[("2m_dewpoint_temperature", gi)]
                            st, cells = series_for_target(gt, t)
                            sd, _c2 = series_for_target(gd, t)
                            es, ea = _es_kpa(st - 273.15), _es_kpa(sd - 273.15)
                            values = (es - ea) if v == "vpd" else np.clip(100.0 * ea / es, 0, 100)
                            times = gt.times
                        else:
                            g = grids[(cv.request_names[0], gi)]
                            raw, cells = series_for_target(g, t)
                            values = convert(cv, raw, g.times)
                            times = g.times
                    except KeyError:
                        log.append(f"{t.name} {v}: no file (request failed)")
                        continue
                    df = pd.DataFrame({"Year": times.year, "Month": times.month, "value": values})
                    df = df[(df["Year"] >= y0) & (df["Year"] <= y1)]
                    df = df.groupby(["Year", "Month"], as_index=False)["value"].mean()
                    results[t.name][v] = S.VarResult(df.reset_index(drop=True), "monthly", cells, cell,
                                                     {"dataset": ds.dataset, "service": SERVICES[ds.service].label,
                                                      "area": jobs_ctx[0][0].request.get("area")})
    else:
        snap = int(req.options.get("glofas_snap_cells", 3))
        parts: Dict[str, List[pd.DataFrame]] = {t.name: [] for t in req.targets}
        cells_by: Dict[str, List[Any]] = {}
        for j, (y, t) in jobs_ctx:
            if j.status == "failed":
                continue
            progress("extract", 0, 0, f"Reading {os.path.basename(j.dest)} ...")
            g = read_grid(j.dest, ("dis24", "dis"))
            if t.name not in cells_by:  # choose the river cell once (first year) and keep it
                vals, cells = river_cell(g, t, snap)
                cells_by[t.name] = cells
            c = cells_by[t.name][0]
            vals = g.values[:, c.i_lat, c.i_lon]
            parts[t.name].append(pd.DataFrame({"Date": g.times.normalize(), "value": vals}))
        for t in req.targets:
            if not parts[t.name]:
                continue
            df = pd.concat(parts[t.name], ignore_index=True).drop_duplicates("Date").sort_values("Date")
            df = df[(df["Date"].dt.year >= y0) & (df["Date"].dt.year <= y1)].reset_index(drop=True)
            c = cells_by[t.name][0]
            d_km = DO.haversine_km(t.lat, t.lon, c.lat, c.lon) if hasattr(DO, "haversine_km") else None
            results[t.name]["dis24"] = S.VarResult(
                df, "daily", cells_by[t.name], cell,
                {"dataset": ds.dataset, "service": SERVICES[ds.service].label,
                 "river_cell": [c.lat, c.lon], "river_cell_distance_km": round(d_km, 2) if d_km else None,
                 "warning": (f"river cell at {c.lat:.3f} N, {c.lon:.3f} E"
                             + (f" ({d_km:.1f} km from the point)" if d_km else "")
                             + ": the largest mean discharge near the point")})
    if failed and not any(results[t.name] for t in req.targets):
        raise DO.OnlineError(failed[0].message or "all requests failed")
    return results, cell, info.label, log


# ===========================================================================
# Command line test
# ===========================================================================

def _print_progress(stage: str, done: int, total: int, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Copernicus CDS / EWDS test")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the saved keys")
    for name in ("era5", "era5land", "glofas"):
        p = sub.add_parser(name)
        p.add_argument("--lat", type=float, default=40.28)
        p.add_argument("--lon", type=float, default=69.62)
        p.add_argument("--years", type=int, nargs=2, default=[2000, 2001])
    args = ap.parse_args(argv)
    if args.cmd == "check":
        for s in SERVICES:
            key, origin = get_key(s)
            ok, msg = check_key(s, key) if key else (False, "no key")
            print(f"{s}: {('key ' + mask_key(key) + ' from ' + origin) if key else 'no key'} -> {msg}")
        print(f"Keys file: {keys_path()}")
        return 0
    import dendro_sources as S

    src = {"era5": "era5_cds_monthly", "era5land": "era5land_cds_monthly", "glofas": "glofas"}[args.cmd]
    vars_ = ["dis24"] if src == "glofas" else ["t2m", "tp"]
    req = S.SourceRequest(src, vars_, [DO.ExtractionTarget("test", args.lat, args.lon, "nearest")],
                          args.years[0], args.years[1], {})
    res = S.run_source_request(req, _print_progress)
    for line in res.log:
        print(line)
    for tname, rs in res.results.items():
        for v, r in rs.items():
            print(f"{tname} {v}: {len(r.data)} rows, cells {[(c.lat, c.lon) for c in r.cells]}")
            print(r.data.head(14).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
