#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_geoexport.py - download registry and location export (GIS / Google Earth).

Every ITRDB file, climate extraction (point, grid cells, area) and weather
station that the online tools download is recorded in a small registry. The
registry is kept between program runs (in the session cache folder) until the
user decides, when the program closes, not to keep the cache.

Exports (one call, any combination):
    * CSV       - one table per layer (WGS84 decimal degrees), for ArcGIS
                  "XY Table To Point", QGIS "Delimited text", Excel.
    * GeoJSON   - one file per layer (ArcGIS "JSON To Features", QGIS).
    * KMZ       - one file for Google Earth with folders per layer, popups
                  with all attributes and time spans (time slider).
    * Shapefile - one .shp/.shx/.dbf/.prj/.cpg set per layer (pure Python).

Layers:
    itrdb        ITRDB sites / files (points)
    climate      climate extraction targets: the point the series belongs to
    grid_cell    grid cells (or pixels) used for interpolation / averaging (polygons)
    area         bounding boxes used for area means (polygons)
    station      weather / gauge stations (points)

No GUI code here; the Qt / Tk dialogs call these functions.
"""

from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import uuid
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "LAYERS", "FORMATS", "Registry", "registry", "session_cache_dir", "export_records",
    "clear_session_cache", "session_cache_has_content",
]

LAYERS: Dict[str, str] = {
    "itrdb": "ITRDB sites and files",
    "climate": "Climate extraction points",
    "grid_cell": "Grid cells / pixels used",
    "area": "Area-mean boxes",
    "station": "Weather and gauge stations",
}
FORMATS: Dict[str, str] = {
    "csv": "CSV tables (ArcGIS XY Table To Point, QGIS, Excel)",
    "geojson": "GeoJSON (ArcGIS JSON To Features, QGIS)",
    "kmz": "KMZ (Google Earth, opens with a double-click)",
    "shp": "Shapefile (ArcGIS, QGIS)",
}
_POINT_LAYERS = ("itrdb", "climate", "station")
_BOX_LAYERS = ("grid_cell", "area")


# ---------------------------------------------------------------------------
# Cache folder
# ---------------------------------------------------------------------------

def _base_cache_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "DeClim", "online_cache")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", "DeClim", "online_cache")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "DeClim", "online_cache")


def session_cache_dir() -> str:
    """Folder for the registry and re-usable cached data (station lists, extracted values).

    Downloaded data files saved by the user are never stored here.
    """
    path = os.path.join(_base_cache_dir(), "session")
    os.makedirs(path, exist_ok=True)
    return path


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def session_cache_has_content() -> bool:
    path = os.path.join(_base_cache_dir(), "session")
    if not os.path.isdir(path):
        return False
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def session_cache_summary() -> str:
    path = os.path.join(_base_cache_dir(), "session")
    n_rec = len(registry.records())
    size = _dir_size(path) if os.path.isdir(path) else 0
    mb = size / (1024.0 * 1024.0)
    return f"{n_rec} downloaded item(s) in the list, {mb:.1f} MB of cached data"


def clear_session_cache() -> None:
    """Delete the registry and cached data (never the user's downloaded files)."""
    path = os.path.join(_base_cache_dir(), "session")
    registry.clear(save=False)
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _clean(v: Any) -> Any:
    """JSON / DBF friendly scalar."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, str)):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return None
    if f.is_integer() and abs(f) < 1e15 and not isinstance(v, float):
        return int(f)
    return round(f, 6)


class Registry:
    """Thread-safe list of downloaded items, saved as JSON in the session cache."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: List[Dict[str, Any]] = []
        self._loaded = False
        self.session_id = uuid.uuid4().hex[:12]
        self._listeners: List[Any] = []

    # -- persistence
    @property
    def path(self) -> str:
        return os.path.join(session_cache_dir(), "download_registry.json")

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            recs = data.get("records", []) if isinstance(data, dict) else []
            self._records = [r for r in recs if isinstance(r, dict) and r.get("layer") in LAYERS]
        except (OSError, ValueError):
            self._records = []

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"format": "declim_download_registry", "version": 1,
                               "records": self._records}, fh, indent=1, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:
                pass

    def clear(self, save: bool = True) -> None:
        with self._lock:
            self._records = []
            self._loaded = True
            if save:
                self.save()
        self._notify()

    def add_listener(self, fn) -> None:
        """fn() is called (from any thread) after the registry changed."""
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    # -- queries
    def records(self, layers: Optional[Iterable[str]] = None, this_session_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            self._load()
            out = list(self._records)
        if layers is not None:
            ls = set(layers)
            out = [r for r in out if r.get("layer") in ls]
        if this_session_only:
            out = [r for r in out if r.get("session") == self.session_id]
        return out

    def count(self, this_session_only: bool = False) -> int:
        return len(self.records(this_session_only=this_session_only))

    def groups(self, this_session_only: bool = False) -> List[Tuple[str, str, int]]:
        """[(group_id, label, n_records)] - one group per download / extraction."""
        seen: Dict[str, List[Dict[str, Any]]] = {}
        for r in self.records(this_session_only=this_session_only):
            seen.setdefault(r.get("group", r.get("uid", "")), []).append(r)
        out = []
        for gid, recs in seen.items():
            head = next((r for r in recs if r.get("layer") in _POINT_LAYERS), recs[0])
            label = head.get("group_label") or head.get("props", {}).get("name", gid)
            out.append((gid, f"{head.get('time', '')[:16].replace('T', ' ')}  {label}", len(recs)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    # -- adding
    def _add(self, recs: List[Dict[str, Any]], key: Optional[str] = None) -> None:
        if not recs:
            return
        with self._lock:
            self._load()
            if key:
                self._records = [r for r in self._records if r.get("key") != key]
                for r in recs:
                    r["key"] = key
            self._records.extend(recs)
            self.save()
        self._notify()

    @staticmethod
    def _rec(layer: str, geom: Dict[str, Any], props: Dict[str, Any], group: str, group_label: str,
             session: str) -> Dict[str, Any]:
        return {
            "uid": uuid.uuid4().hex[:16], "layer": layer, "geom": geom,
            "props": {k: _clean(v) for k, v in props.items()},
            "group": group, "group_label": group_label, "time": _now(), "session": session,
        }

    def add_itrdb_file(self, site: Any, f: Any, local_path: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        """ITRDB file downloaded (site / file objects from dendro_online)."""
        lat, lon = getattr(site, "lat", None), getattr(site, "lon", None)
        if lat is None or lon is None:
            return
        props = {
            "name": getattr(site, "site_code", "") or getattr(site, "site_name", ""),
            "site_code": getattr(site, "site_code", ""),
            "site_name": getattr(site, "site_name", ""),
            "file": getattr(f, "filename", ""),
            "file_type": getattr(f, "kind", ""),
            "parameter": getattr(f, "parameter", ""),
            "chron_type": getattr(f, "variant", ""),
            "species": getattr(site, "species", ""),
            "lat": lat, "lon": lon,
            "elev_m": getattr(site, "elevation_m", None),
            "first_year": getattr(site, "first_year", None),
            "last_year": getattr(site, "last_year", None),
            "investigators": getattr(site, "investigators", ""),
            "location": getattr(site, "location", ""),
            "doi": getattr(site, "doi", ""),
            "study_url": getattr(site, "study_url", ""),
            "file_url": getattr(f, "url", ""),
            "local_path": local_path,
            "source": "ITRDB (NOAA NCEI)",
        }
        if extra:
            props.update(extra)
        gid = "itrdb:" + str(getattr(site, "study_id", props["site_code"]))
        rec = self._rec("itrdb", {"type": "point", "lat": float(lat), "lon": float(lon)}, props,
                        gid, f"ITRDB {props['site_code']} {props['site_name']}".strip(), self.session_id)
        self._add([rec], key="itrdb:" + props["file_url"])

    def add_climate_extraction(
        self,
        source: str,
        variables: Sequence[str],
        target_name: str,
        lat: float,
        lon: float,
        method: str,
        first_year: Optional[int],
        last_year: Optional[int],
        cells: Sequence[Any] = (),
        cell_size: Optional[Tuple[float, float]] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        version: str = "",
        resolution: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """A gridded climate extraction: the main point, the cells used and the area box.

        ``cells`` are objects / dicts with lat, lon, weight, valid (optional),
        ``cell_size`` = (dlat, dlon) in degrees (cell polygons are drawn when known).
        """
        group = f"climate:{source}:{target_name}:{lat:.4f}:{lon:.4f}:{method}:{uuid.uuid4().hex[:6]}"
        label = f"{source} {', '.join(variables)} - {target_name}"
        base = {
            "name": target_name, "source": source, "version": version,
            "variables": ", ".join(variables), "method": method,
            "first_year": first_year, "last_year": last_year, "resolution": resolution,
            "lat": lat, "lon": lon,
        }
        if extra:
            base.update(extra)
        recs = [self._rec("climate", {"type": "point", "lat": float(lat), "lon": float(lon)},
                          dict(base, n_cells=len(cells)), group, label, self.session_id)]
        for k, c in enumerate(cells, start=1):
            get = (lambda key, c=c: c.get(key) if isinstance(c, dict) else getattr(c, key, None))
            clat, clon = get("lat"), get("lon")
            if clat is None or clon is None:
                continue
            props = {
                "name": f"{target_name} cell {k}", "target": target_name, "source": source,
                "variables": ", ".join(variables), "cell_lat": clat, "cell_lon": clon,
                "weight": get("weight"), "has_data": get("valid"),
                "dist_km": _haversine_km(lat, lon, float(clat), float(clon)),
                "stations": get("stn"),
            }
            if cell_size:
                dlat, dlon = cell_size
                geom = {"type": "box", "s": clat - dlat / 2, "n": clat + dlat / 2,
                        "w": clon - dlon / 2, "e": clon + dlon / 2}
            else:
                geom = {"type": "point", "lat": float(clat), "lon": float(clon)}
            recs.append(self._rec("grid_cell", geom, props, group, label, self.session_id))
        if bbox:
            s, n, w, e = bbox
            recs.append(self._rec("area", {"type": "box", "s": s, "n": n, "w": w, "e": e},
                                  dict(base, name=f"{target_name} area"), group, label, self.session_id))
        key = f"climate:{source}:{','.join(sorted(variables))}:{target_name}:{lat:.4f}:{lon:.4f}:{method}:{first_year}:{last_year}"
        self._add(recs, key=key)

    def add_station(self, source: str, station_id: str, name: str, lat: float, lon: float,
                    elevation_m: Optional[float] = None, variables: Sequence[str] = (),
                    first_year: Optional[int] = None, last_year: Optional[int] = None,
                    local_path: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        props = {"name": name or station_id, "station_id": station_id, "source": source,
                 "variables": ", ".join(variables), "lat": lat, "lon": lon, "elev_m": elevation_m,
                 "first_year": first_year, "last_year": last_year, "local_path": local_path}
        if extra:
            props.update(extra)
        rec = self._rec("station", {"type": "point", "lat": float(lat), "lon": float(lon)}, props,
                        f"station:{source}:{station_id}", f"{source} {station_id} {name}", self.session_id)
        self._add([rec], key=f"station:{source}:{station_id}:{','.join(sorted(variables))}")

    def remove_groups(self, group_ids: Iterable[str]) -> None:
        gs = set(group_ids)
        with self._lock:
            self._load()
            self._records = [r for r in self._records if r.get("group") not in gs]
            self.save()
        self._notify()


registry = Registry()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(min(1.0, math.sqrt(a))), 3)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _safe_base(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._")
    return s or "locations"


def _columns(recs: Sequence[Dict[str, Any]]) -> List[str]:
    cols: List[str] = []
    for r in recs:
        for k in r.get("props", {}):
            if k not in cols:
                cols.append(k)
    return cols


def _box_ring(g: Dict[str, Any]) -> List[Tuple[float, float]]:
    s, n, w, e = g["s"], g["n"], g["w"], g["e"]
    return [(w, s), (w, n), (e, n), (e, s), (w, s)]  # clockwise (shapefile outer ring)


def _layer_rows(layer: str, recs: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[List[Any]]]:
    cols = _columns(recs)
    geo_cols = ["lat", "lon"] if layer in _POINT_LAYERS else ["center_lat", "center_lon", "south", "north", "west", "east"]
    cols = [c for c in cols if c not in geo_cols]
    header = (["name"] if "name" in cols else []) + geo_cols + [c for c in cols if c != "name"]
    rows = []
    for r in recs:
        g, p = r["geom"], r["props"]
        if g["type"] == "point":
            geo = [g["lat"], g["lon"]] if layer in _POINT_LAYERS else [g["lat"], g["lon"], None, None, None, None]
        else:
            geo = [(g["s"] + g["n"]) / 2, (g["w"] + g["e"]) / 2, g["s"], g["n"], g["w"], g["e"]]
            if layer in _POINT_LAYERS:
                geo = geo[:2]
        row = ([p.get("name", "")] if "name" in cols else []) + [round(float(x), 6) if x is not None else None for x in geo]
        row += [p.get(c) for c in cols if c != "name"]
        rows.append(row)
    return header, rows


def _write_csv(path: str, header: List[str], rows: List[List[Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])


def _geojson_feature(r: Dict[str, Any]) -> Dict[str, Any]:
    g = r["geom"]
    if g["type"] == "point":
        geom = {"type": "Point", "coordinates": [round(g["lon"], 6), round(g["lat"], 6)]}
    else:
        ring = [[round(x, 6), round(y, 6)] for x, y in reversed(_box_ring(g))]  # counter-clockwise (RFC 7946)
        geom = {"type": "Polygon", "coordinates": [ring]}
    return {"type": "Feature", "geometry": geom, "properties": dict(r["props"], layer=r["layer"])}


def _write_geojson(path: str, recs: Sequence[Dict[str, Any]]) -> None:
    fc = {"type": "FeatureCollection",
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "features": [_geojson_feature(r) for r in recs]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, indent=1)


# -- KML / KMZ ---------------------------------------------------------------

_KML_STYLES = {
    "itrdb": ("http://maps.google.com/mapfiles/kml/shapes/parks.png", "ff2e8b57", 1.1),
    "climate": ("http://maps.google.com/mapfiles/kml/shapes/star.png", "ff0000ff", 1.2),
    "station": ("http://maps.google.com/mapfiles/kml/shapes/triangle.png", "ffff7f00", 1.0),
}


def _kml_desc(props: Dict[str, Any]) -> str:
    rows = []
    for k, v in props.items():
        if v is None or v == "":
            continue
        sv = html.escape(str(v))
        if isinstance(v, str) and v.startswith("http"):
            sv = f'<a href="{sv}">{sv}</a>'
        rows.append(f"<tr><td><b>{html.escape(str(k))}</b></td><td>{sv}</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def _kml_timespan(props: Dict[str, Any]) -> str:
    a, b = props.get("first_year"), props.get("last_year")
    try:
        a = int(a) if a is not None else None
        b = int(b) if b is not None else None
    except (TypeError, ValueError):
        return ""
    if a is None and b is None:
        return ""
    parts = ["<TimeSpan>"]
    if a is not None:
        parts.append(f"<begin>{a:04d}</begin>")
    if b is not None:
        parts.append(f"<end>{b:04d}</end>")
    parts.append("</TimeSpan>")
    return "".join(parts)


def build_kml(recs: Sequence[Dict[str, Any]], title: str = "DeClim downloaded data") -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2">', "<Document>",
           f"<name>{html.escape(title)}</name>",
           "<description>Exported by DeClim online tools (RingClim / Detrend Advisor). "
           "Coordinates WGS84.</description>"]
    for layer, (icon, color, scale) in _KML_STYLES.items():
        out.append(f'<Style id="st_{layer}"><IconStyle><color>{color}</color><scale>{scale}</scale>'
                   f"<Icon><href>{icon}</href></Icon></IconStyle><LabelStyle><scale>0.8</scale></LabelStyle></Style>")
    out.append('<Style id="st_grid_cell"><LineStyle><color>ff0080ff</color><width>1.5</width></LineStyle>'
               "<PolyStyle><color>4d0080ff</color></PolyStyle></Style>")
    out.append('<Style id="st_area"><LineStyle><color>ffff00ff</color><width>2.5</width></LineStyle>'
               "<PolyStyle><fill>0</fill></PolyStyle></Style>")
    for layer, label in LAYERS.items():
        lrecs = [r for r in recs if r["layer"] == layer]
        if not lrecs:
            continue
        out.append(f"<Folder><name>{html.escape(label)} ({len(lrecs)})</name>")
        for r in lrecs:
            p, g = r["props"], r["geom"]
            name = html.escape(str(p.get("name", "")))
            out.append(f"<Placemark><name>{name}</name><styleUrl>#st_{layer}</styleUrl>")
            out.append(f"<description><![CDATA[{_kml_desc(p)}]]></description>")
            out.append(_kml_timespan(p))
            if g["type"] == "point":
                out.append(f"<Point><coordinates>{g['lon']:.6f},{g['lat']:.6f},0</coordinates></Point>")
            else:
                ring = " ".join(f"{x:.6f},{y:.6f},0" for x, y in reversed(_box_ring(g)))
                out.append("<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>"
                           f"<coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon>")
            out.append("</Placemark>")
        out.append("</Folder>")
    out.append("</Document></kml>")
    return "\n".join(x for x in out if x)


def _write_kmz(path: str, recs: Sequence[Dict[str, Any]], title: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", build_kml(recs, title).encode("utf-8"))


# -- Shapefile ----------------------------------------------------------------

_WGS84_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
              'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def _dbf_fields(header: List[str], rows: List[List[Any]]) -> List[Tuple[str, str, int, int]]:
    """[(name<=10 chars, type C/N, length, decimals)]"""
    used: set = set()
    fields = []
    for j, col in enumerate(header):
        base = re.sub(r"[^A-Za-z0-9_]", "_", col)[:10] or f"F{j}"
        name, k = base, 1
        while name.upper() in used:
            suffix = str(k)
            name = base[: 10 - len(suffix)] + suffix
            k += 1
        used.add(name.upper())
        vals = [r[j] for r in rows if r[j] is not None and r[j] != ""]
        numeric = bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
        if numeric:
            is_int = all(float(v).is_integer() for v in vals)
            fields.append((name, "N", 18, 0 if is_int else 6))
        else:
            n = max([len(str(v).encode("utf-8")) for v in vals] + [1])
            fields.append((name, "C", min(254, max(1, n)), 0))
    return fields


def _write_dbf(path: str, header: List[str], rows: List[List[Any]]) -> None:
    fields = _dbf_fields(header, rows)
    rec_len = 1 + sum(f[2] for f in fields)
    hdr_len = 32 + 32 * len(fields) + 1
    today = _dt.date.today()
    with open(path, "wb") as fh:
        fh.write(struct.pack("<BBBBIHH20x", 0x03, today.year - 1900, today.month, today.day,
                             len(rows), hdr_len, rec_len))
        for name, typ, length, dec in fields:
            fh.write(struct.pack("<11sc4xBB14x", name.encode("ascii"), typ.encode("ascii"), length, dec))
        fh.write(b"\r")
        for row in rows:
            fh.write(b" ")
            for (name, typ, length, dec), v in zip(fields, row):
                if typ == "N":
                    if v is None or v == "":
                        s = b" " * length
                    else:
                        s = (f"{float(v):.{dec}f}" if dec else str(int(round(float(v))))).encode("ascii")
                        s = s[:length].rjust(length)
                else:
                    b = ("" if v is None else str(v)).encode("utf-8")[:length]
                    while True:  # do not cut a UTF-8 sequence in half
                        try:
                            b.decode("utf-8")
                            break
                        except UnicodeDecodeError:
                            b = b[:-1]
                    s = b.ljust(length)
                fh.write(s)
        fh.write(b"\x1a")


def _write_shp(base: str, recs: Sequence[Dict[str, Any]], header: List[str], rows: List[List[Any]],
               polygons: bool) -> List[str]:
    shp_type = 5 if polygons else 1
    contents: List[bytes] = []
    xs: List[float] = []
    ys: List[float] = []
    for r in recs:
        g = r["geom"]
        if polygons:
            ring = _box_ring(g) if g["type"] == "box" else [(g["lon"], g["lat"])] * 5
            px = [p[0] for p in ring]
            py = [p[1] for p in ring]
            body = struct.pack("<i4dii", 5, min(px), min(py), max(px), max(py), 1, len(ring))
            body += struct.pack("<i", 0)
            body += b"".join(struct.pack("<2d", x, y) for x, y in ring)
            xs += px
            ys += py
        else:
            body = struct.pack("<i2d", 1, g["lon"], g["lat"])
            xs.append(g["lon"])
            ys.append(g["lat"])
        contents.append(body)
    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)

    def file_header(length_words: int) -> bytes:
        return (struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, length_words)
                + struct.pack("<ii", 1000, shp_type) + struct.pack("<4d", *bbox) + struct.pack("<4d", 0, 0, 0, 0))

    shp_len = 50 + sum(4 + len(c) // 2 for c in contents)
    shx_len = 50 + 4 * len(contents)
    with open(base + ".shp", "wb") as shp, open(base + ".shx", "wb") as shx:
        shp.write(file_header(shp_len))
        shx.write(file_header(shx_len))
        offset = 50
        for k, c in enumerate(contents, start=1):
            shp.write(struct.pack(">ii", k, len(c) // 2))
            shp.write(c)
            shx.write(struct.pack(">ii", offset, len(c) // 2))
            offset += 4 + len(c) // 2
    _write_dbf(base + ".dbf", header, rows)
    with open(base + ".prj", "w", encoding="ascii") as fh:
        fh.write(_WGS84_PRJ)
    with open(base + ".cpg", "w", encoding="ascii") as fh:
        fh.write("UTF-8")
    return [base + ext for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")]


def export_records(records: Sequence[Dict[str, Any]], out_dir: str, base_name: str,
                   formats: Iterable[str], title: str = "DeClim downloaded data") -> List[str]:
    """Write the selected formats; returns the list of written files."""
    fmts = [f for f in formats if f in FORMATS]
    if not fmts:
        raise ValueError("No export format selected.")
    if not records:
        raise ValueError("Nothing to export.")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, _safe_base(base_name))
    written: List[str] = []
    by_layer: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        by_layer.setdefault(r["layer"], []).append(r)
    for layer, recs in by_layer.items():
        header, rows = _layer_rows(layer, recs)
        lbase = f"{base}_{layer}"
        if "csv" in fmts:
            _write_csv(lbase + ".csv", header, rows)
            written.append(lbase + ".csv")
        if "geojson" in fmts:
            _write_geojson(lbase + ".geojson", recs)
            written.append(lbase + ".geojson")
        if "shp" in fmts:
            written += _write_shp(lbase, recs, header, rows, polygons=layer in _BOX_LAYERS)
    if "kmz" in fmts:
        _write_kmz(base + ".kmz", records, title)
        written.append(base + ".kmz")
    return written
