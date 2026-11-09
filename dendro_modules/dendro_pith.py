#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_pith.py - pith (centre) information and radius-to-tree grouping help
for Detrend Advisor. Pure Python (numpy / pandas), no GUI.

Pith table
----------
One row per series (index = series ID) with columns
    status       "present" | "estimated" | "unknown"
    missing_rings  rings between the innermost measured ring and the pith
                   (0 when the pith is present; NaN when unknown)
    offset       dplR pith offset = missing_rings + 1 (NaN when unknown)
    distance_mm  distance from the innermost measured ring to the pith
    method       how the value was obtained
    source       file name, "manual" ...
    reference    reference radius used for the estimate (PyTreeRingRecorder)

Sources that are understood
---------------------------
* PyTreeRingRecorder RWL export sidecars:
    <name>.pith-metadata.csv  (series, status, missing_rings, first_ring_age,
                               distance_to_pith_mm, method, reference_radius ...)
    <name>.pith.csv           (series, pith.offset)   - dplR po format
* .pos files of PyTreeRingRecorder (TRR_PithJSON / TRR_PithStatus) and
  CooRecorder (YearsToPith, DistanceToPith, PithCoordinates).
* Any CSV with a series column and one of: pith.offset / offset / po,
  missing_rings / yearstopith / rings_to_pith, distance_to_pith_mm.

dplR convention: pith.offset = 1 means the first measured ring is the pith
ring; unknown pith is NA and must not silently become 1.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

PITH_COLUMNS = ["status", "missing_rings", "offset", "distance_mm", "method", "source", "reference"]
STATUSES = ("present", "estimated", "unknown")

UNKNOWN_AS_PRESENT = "present"     # treat unknown pith as offset 1 (classic behaviour)
UNKNOWN_EXCLUDE = "exclude"        # leave unknown out of age-based methods (RCS)


# ---------------------------------------------------------------------------
# Pith table basics
# ---------------------------------------------------------------------------

def empty_pith_table(series_ids: Iterable[Any]) -> pd.DataFrame:
    ids = [str(s) for s in series_ids]
    df = pd.DataFrame(index=pd.Index(ids, name="series"))
    df["status"] = "unknown"
    df["missing_rings"] = np.nan
    df["offset"] = np.nan
    df["distance_mm"] = np.nan
    df["method"] = ""
    df["source"] = ""
    df["reference"] = ""
    return df


def normalize_pith_table(table: Optional[pd.DataFrame], series_ids: Iterable[Any]) -> pd.DataFrame:
    """Return a table with exactly the given series (missing ones = unknown)."""
    ids = [str(s) for s in series_ids]
    out = empty_pith_table(ids)
    if table is None or not isinstance(table, pd.DataFrame) or table.empty:
        return out
    t = table.copy()
    t.index = t.index.astype(str)
    for sid in ids:
        if sid in t.index:
            row = t.loc[sid]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            for col in PITH_COLUMNS:
                if col in t.columns:
                    out.at[sid, col] = row[col]
    return _recompute(out)


def _recompute(df: pd.DataFrame) -> pd.DataFrame:
    """Keep status / missing_rings / offset consistent."""
    df = df.copy()
    df["missing_rings"] = pd.to_numeric(df["missing_rings"], errors="coerce")
    df["distance_mm"] = pd.to_numeric(df["distance_mm"], errors="coerce")
    for col in ("method", "source", "reference"):
        df[col] = df[col].fillna("").astype(str)
    status = df["status"].fillna("unknown").astype(str).str.strip().str.lower()
    status = status.where(status.isin(STATUSES), "unknown")
    present = status.eq("present")
    df.loc[present, "missing_rings"] = 0.0
    unknown = status.eq("unknown")
    df.loc[unknown, "missing_rings"] = np.nan
    # estimated without a number is unknown
    bad = status.eq("estimated") & df["missing_rings"].isna()
    status = status.mask(bad, "unknown")
    neg = df["missing_rings"] < 0
    df.loc[neg, "missing_rings"] = np.nan
    status = status.mask(neg, "unknown")
    df["status"] = status
    df["missing_rings"] = df["missing_rings"].round()
    df["offset"] = df["missing_rings"] + 1.0
    return df[PITH_COLUMNS]


def set_pith(table: pd.DataFrame, series_ids: Iterable[str], status: str,
             missing_rings: Optional[float] = None, source: str = "manual",
             method: str = "manual", distance_mm: Optional[float] = None) -> pd.DataFrame:
    """Manual edit of one or more rows."""
    t = table.copy()
    status = str(status).lower()
    if status not in STATUSES:
        raise ValueError(f"Unknown pith status: {status}")
    for sid in series_ids:
        sid = str(sid)
        if sid not in t.index:
            continue
        t.at[sid, "status"] = status
        if status == "present":
            t.at[sid, "missing_rings"] = 0.0
        elif status == "estimated":
            if missing_rings is None or not np.isfinite(float(missing_rings)) or float(missing_rings) < 0:
                raise ValueError("Enter the number of rings to the pith (0 or more).")
            t.at[sid, "missing_rings"] = float(missing_rings)
            if float(missing_rings) == 0:
                t.at[sid, "status"] = "present"
        else:
            t.at[sid, "missing_rings"] = np.nan
        if distance_mm is not None:
            t.at[sid, "distance_mm"] = float(distance_mm)
        t.at[sid, "method"] = method
        t.at[sid, "source"] = source
    return _recompute(t)


def merge_pith(base: pd.DataFrame, new_rows: Dict[str, Dict[str, Any]], *,
               overwrite_known: bool = True, keep_manual: bool = True) -> Tuple[pd.DataFrame, int]:
    """Merge parsed info {series_id: info} into the table. Returns (table, n_updated)."""
    t = base.copy()
    n = 0
    for sid, info in new_rows.items():
        if sid not in t.index:
            continue
        cur_status = str(t.at[sid, "status"])
        if keep_manual and str(t.at[sid, "source"]) == "manual":
            continue
        if not overwrite_known and cur_status != "unknown":
            continue
        st = str(info.get("status", "unknown")).lower()
        if st == "unknown" and cur_status != "unknown":
            continue  # never downgrade known information with "unknown"
        t.at[sid, "status"] = st
        t.at[sid, "missing_rings"] = info.get("missing_rings", np.nan)
        t.at[sid, "distance_mm"] = info.get("distance_mm", np.nan)
        t.at[sid, "method"] = str(info.get("method", "") or "")
        t.at[sid, "source"] = str(info.get("source", "") or "")
        t.at[sid, "reference"] = str(info.get("reference", "") or "")
        n += 1
    return _recompute(t), n


def pith_summary(table: Optional[pd.DataFrame]) -> Dict[str, int]:
    if table is None or table.empty:
        return {"present": 0, "estimated": 0, "unknown": 0}
    vc = table["status"].value_counts()
    return {k: int(vc.get(k, 0)) for k in STATUSES}


def pith_summary_text(table: Optional[pd.DataFrame]) -> str:
    s = pith_summary(table)
    return f"Pith: {s['present']} present, {s['estimated']} estimated, {s['unknown']} unknown"


def pith_to_po(table: Optional[pd.DataFrame], series_ids: Iterable[Any],
               unknown_policy: str = UNKNOWN_AS_PRESENT) -> Tuple[pd.DataFrame, List[str]]:
    """dplR po data frame (series, pith.offset) for the given series.

    unknown_policy:
      "present" - unknown pith gets offset 1 (as if the pith were measured);
      "exclude" - unknown series are returned in the second value so the caller
                  can leave them out of age-based methods.
    """
    ids = [str(s) for s in series_ids]
    t = normalize_pith_table(table, ids)
    excluded: List[str] = []
    rows = []
    for sid in ids:
        off = t.at[sid, "offset"]
        if not np.isfinite(off):
            if unknown_policy == UNKNOWN_EXCLUDE:
                excluded.append(sid)
            off = 1.0
        rows.append({"series": sid, "pith.offset": int(max(1, round(float(off))))})
    return pd.DataFrame(rows, columns=["series", "pith.offset"]), excluded


# ---------------------------------------------------------------------------
# Matching names
# ---------------------------------------------------------------------------

def norm_id(text: Any) -> str:
    return re.sub(r"[\s\-_./\\]+", "", str(text or "")).upper()


def match_series_id(name: str, series_ids: Sequence[str]) -> Optional[str]:
    """Find the series in the dataset that a file / table name refers to.

    Tucson IDs are at most 8 characters, so long names are also compared by
    their first 8 characters (that is how PyTreeRingRecorder and CDendro
    shorten them).
    """
    if not name:
        return None
    ids = [str(s) for s in series_ids]
    if name in ids:
        return name
    n = norm_id(name)
    by_norm = {norm_id(s): s for s in ids}
    if n in by_norm:
        return by_norm[n]
    if n[:8] in by_norm:
        return by_norm[n[:8]]
    stripped = {norm_id(s).rstrip("0") or norm_id(s): s for s in ids}
    if n.rstrip("0") in stripped and n.rstrip("0"):
        return stripped[n.rstrip("0")]
    return None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _f(value: Any) -> float:
    try:
        if value is None:
            return np.nan
        text = str(value).strip()
        if text == "" or text.upper() in ("NA", "NAN", "NONE", "NULL", "-"):
            return np.nan
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return np.nan


def _read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_pith_csv(path: str) -> Dict[str, Dict[str, Any]]:
    """Read a pith CSV (PyTRR metadata / dplR po / generic). Keys = series names from the file."""
    text = _read_text(path)
    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return {}
    header = [h.strip().lower().replace(" ", "_") for h in rows[0]]

    def col(*names: str) -> Optional[int]:
        for nm in names:
            if nm in header:
                return header.index(nm)
        return None

    has_header = any(h in header for h in ("series", "pith.offset", "offset", "status", "missing_rings",
                                           "yearstopith", "series_id", "id"))
    data_rows = rows[1:] if has_header else rows
    i_series = col("series", "series_id", "id", "name") if has_header else 0
    if i_series is None:
        i_series = 0
    i_status = col("status", "pith_status")
    i_missing = col("missing_rings", "yearstopith", "years_to_pith", "rings_to_pith", "missing")
    i_offset = col("pith.offset", "offset", "po", "pith_offset")
    i_first_age = col("first_ring_age")
    i_dist = col("distance_to_pith_mm", "distancetopith", "distance_mm", "distance_to_pith")
    i_method = col("method")
    i_ref = col("reference_radius", "reference")
    if not has_header and len(rows[0]) >= 2:
        i_offset = 1

    out: Dict[str, Dict[str, Any]] = {}
    src = os.path.basename(path)
    for r in data_rows:
        if i_series >= len(r):
            continue
        name = r[i_series].strip()
        if not name:
            continue
        missing = np.nan
        if i_missing is not None and i_missing < len(r):
            missing = _f(r[i_missing])
        if not np.isfinite(missing) and i_offset is not None and i_offset < len(r):
            off = _f(r[i_offset])
            if np.isfinite(off):
                missing = off - 1.0
        if not np.isfinite(missing) and i_first_age is not None and i_first_age < len(r):
            off = _f(r[i_first_age])
            if np.isfinite(off):
                missing = off - 1.0
        status = ""
        if i_status is not None and i_status < len(r):
            status = r[i_status].strip().lower()
        if status not in STATUSES:
            if np.isfinite(missing):
                status = "present" if missing == 0 else "estimated"
            else:
                status = "unknown"
        dist = _f(r[i_dist]) if (i_dist is not None and i_dist < len(r)) else np.nan
        method = r[i_method].strip() if (i_method is not None and i_method < len(r)) else "imported_csv"
        ref = r[i_ref].strip() if (i_ref is not None and i_ref < len(r)) else ""
        out[name] = {"status": status, "missing_rings": missing, "distance_mm": dist,
                     "method": method or "imported_csv", "source": src, "reference": ref}
    return out


def read_pos_pith(path: str) -> Dict[str, Any]:
    """Pith info from one .pos file (PyTreeRingRecorder or CooRecorder)."""
    text = _read_text(path)
    src = os.path.basename(path)
    m = re.search(r"^#C\s*TRR_PithJSON=(.*)$", text, re.M)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            status = str(data.get("status", "unknown")).lower()
            info = {"status": status if status in STATUSES else "unknown",
                    "missing_rings": _f(data.get("missing_rings")),
                    "distance_mm": _f(data.get("distance_mm")),
                    "method": "PyTreeRingRecorder: " + str(data.get("method", "")),
                    "source": src,
                    "reference": str((data.get("reference_info") or {}).get("source", "") or "")}
            return info
        except (ValueError, TypeError):
            pass

    def prop(key: str) -> Optional[str]:
        mm = re.search(r"\b" + key + r"\s*=\s*([^;\r\n]+)", text, re.I)
        return mm.group(1).strip() if mm else None

    st = prop("TRR_PithStatus")
    years = _f(prop("YearsToPith"))
    dist = _f(prop("DistanceToPith"))
    if np.isfinite(years):
        status = "present" if years == 0 and (not np.isfinite(dist) or dist == 0) else "estimated"
        if st and st.lower() in STATUSES and st.lower() != "unknown":
            status = st.lower()
        return {"status": status, "missing_rings": years, "distance_mm": dist,
                "method": "pos: YearsToPith", "source": src, "reference": ""}
    if np.isfinite(dist):
        # distance only: rings must be estimated from inner ring widths later
        return {"status": "unknown", "missing_rings": np.nan, "distance_mm": dist,
                "method": "pos: DistanceToPith only", "source": src, "reference": "", "needs_width": True}
    if st and st.lower() == "present":
        return {"status": "present", "missing_rings": 0.0, "distance_mm": 0.0,
                "method": "pos: TRR_PithStatus", "source": src, "reference": ""}
    return {"status": "unknown", "missing_rings": np.nan, "distance_mm": np.nan,
            "method": "pos: no pith fields", "source": src, "reference": ""}


def pos_series_name(path: str) -> str:
    """Series name of a .pos file: an explicit name field if present, else the file stem."""
    text = _read_text(path)[:4000]
    for key in ("SeriesName", "Series", "SeriesID", "Name"):
        m = re.search(r"^#C?\s*" + key + r"\s*[=:]\s*([^;\r\n]+)", text, re.M | re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return os.path.splitext(os.path.basename(path))[0]


def find_sidecars(rwl_path: str) -> List[str]:
    """Pith files written next to an RWL file (best first)."""
    if not rwl_path:
        return []
    base = os.path.splitext(rwl_path)[0]
    folder = os.path.dirname(os.path.abspath(rwl_path))
    cands = [base + ".pith-metadata.csv", base + ".pith.csv", base + "_pith.csv",
             base + ".po.csv", base + "_po.csv", os.path.join(folder, "po.csv")]
    return [p for p in cands if os.path.isfile(p)]


def estimate_missing_from_distance(distance_mm: float, inner_widths_mm: Sequence[float],
                                   n_rings: int = 10) -> Optional[int]:
    """Rings to the pith = distance / mean width of the innermost measured rings."""
    w = [float(x) for x in inner_widths_mm if x is not None and np.isfinite(x) and x > 0][:n_rings]
    if not w or not np.isfinite(distance_mm) or distance_mm < 0:
        return None
    mean = float(np.mean(w))
    if mean <= 0:
        return None
    return int(math.floor(distance_mm / mean + 0.5))


def tucson_precision_mm(path: str) -> Dict[str, float]:
    """Per-series unit of a Tucson file from its end marker (999 -> 0.01, -9999 -> 0.001 mm)."""
    out: Dict[str, float] = {}
    try:
        for line in _read_text(path).splitlines():
            if len(line) < 13:
                continue
            sid = line[:8].strip()
            vals = line[12:].split()
            if "-9999" in vals:
                out[sid] = 0.001
            elif "999" in vals:
                out.setdefault(sid, 0.01)
    except OSError:
        pass
    return out


def collect_pith_info(series_ids: Sequence[str], files: Iterable[str],
                      rwl: Optional[pd.DataFrame] = None,
                      rwl_path: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Parse pith files (.pos / .csv) and match them to the dataset's series.

    Returns ({series_id: info}, report_lines). Distance-only .pos entries are
    converted to ring counts with the mean width of the 10 innermost rings
    (units from the RWL end markers when the RWL path is known).
    """
    ids = [str(s) for s in series_ids]
    found: Dict[str, Dict[str, Any]] = {}
    report: List[str] = []
    unmatched: List[str] = []
    precision = tucson_precision_mm(rwl_path) if rwl_path and rwl_path.lower().endswith((".rwl", ".tuc", ".txt")) else {}
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".pos":
                name = pos_series_name(path)
                info = read_pos_pith(path)
                parsed = {name: info}
                if norm_id(name) != norm_id(os.path.splitext(os.path.basename(path))[0]):
                    parsed[os.path.splitext(os.path.basename(path))[0]] = info
            else:
                parsed = read_pith_csv(path)
        except Exception as exc:  # report and continue
            report.append(f"{os.path.basename(path)}: cannot read ({exc})")
            continue
        for name, info in parsed.items():
            sid = match_series_id(name, ids)
            if sid is None:
                unmatched.append(name)
                continue
            if info.get("needs_width") and rwl is not None and sid in rwl.columns:
                ser = pd.to_numeric(rwl[sid], errors="coerce").dropna()
                med = float(ser.median()) if len(ser) else np.nan
                if med < 20:
                    unit = 1.0  # table already in mm (Detrend Advisor reads Tucson files in mm)
                else:
                    unit = precision.get(sid[:8].strip()) or (0.01 if med < 400 else 0.001)
                n = estimate_missing_from_distance(info["distance_mm"], (ser.values * unit).tolist())
                if n is not None:
                    info = dict(info, status="present" if n == 0 else "estimated", missing_rings=float(n),
                                method="DistanceToPith / mean width of 10 inner rings")
            prev = found.get(sid)
            if prev is None or (prev.get("status") == "unknown" and info.get("status") != "unknown"):
                found[sid] = info
    if found:
        report.append(f"Matched pith information for {len(found)} series.")
    if unmatched:
        uniq = list(dict.fromkeys(unmatched))
        report.append(f"No matching series for {len(uniq)} name(s): " + ", ".join(uniq[:12])
                      + (" ..." if len(uniq) > 12 else ""))
    return found, report


def files_in_folder(folder: str, recursive: bool = True) -> List[str]:
    out: List[str] = []
    for root, _dirs, names in os.walk(folder):
        for nm in names:
            low = nm.lower()
            if low.endswith(".pos") or low.endswith(".pith-metadata.csv") or low.endswith(".pith.csv"):
                out.append(os.path.join(root, nm))
        if not recursive:
            break
    # metadata before plain offsets so the richer file wins
    out.sort(key=lambda p: (not p.lower().endswith(".pith-metadata.csv"), p))
    return out


# ---------------------------------------------------------------------------
# Tree-level offsets
# ---------------------------------------------------------------------------

def tree_offset(member_first_years: Sequence[int], member_offsets: Sequence[float],
                tree_first_year: int) -> Optional[int]:
    """Pith offset of a tree mean series built from several radii.

    Every radius with known pith gives an estimated pith year
    (first_year - offset + 1). The tree's pith year is their median, and the
    tree offset is counted from the first year of the tree mean.
    """
    pith_years = [int(fy) - int(round(off)) + 1 for fy, off in zip(member_first_years, member_offsets)
                  if off is not None and np.isfinite(off)]
    if not pith_years:
        return None
    pith_year = int(round(float(np.median(pith_years))))
    return int(max(1, tree_first_year - pith_year + 1))


# ---------------------------------------------------------------------------
# Radius grouping checks (ring-width cross-correlation)
# ---------------------------------------------------------------------------

def highpass_signal(rwl: pd.DataFrame, window: int = 15) -> pd.DataFrame:
    """log widths minus a centred running mean: year-to-year signal shared by radii of one tree."""
    x = rwl.apply(pd.to_numeric, errors="coerce").astype(float)
    x = x.where(x > 0)
    lx = np.log(x)
    trend = lx.rolling(window, center=True, min_periods=max(3, window // 3)).mean()
    return lx - trend


def pairwise_r(rwl: pd.DataFrame, min_overlap: int = 20) -> pd.DataFrame:
    hp = highpass_signal(rwl)
    return hp.corr(min_periods=min_overlap)


def within_tree_stats(ids: pd.DataFrame, r: pd.DataFrame, flag_below: float = 0.3) -> pd.DataFrame:
    """Per series: mean r with its tree-mates, best other series and a flag."""
    rows = []
    labels = ids["tree_label"].astype(str) if "tree_label" in ids.columns else ids["tree"].astype(str)
    for sid in ids.index.astype(str):
        mates = [m for m in labels.index[labels == labels.get(sid)].astype(str) if m != sid]
        vals = [r.at[sid, m] for m in mates if sid in r.index and m in r.columns and np.isfinite(r.at[sid, m])]
        mean_r = float(np.mean(vals)) if vals else np.nan
        best, best_r = "", np.nan
        if sid in r.index:
            others = r.loc[sid].drop(labels=[sid], errors="ignore").dropna()
            if len(others):
                best = str(others.idxmax())
                best_r = float(others.max())
        flag = ""
        if mates and np.isfinite(mean_r) and mean_r < flag_below:
            flag = "low r with tree-mates"
        elif mates and not vals:
            flag = "no overlap with tree-mates"
        elif not mates and np.isfinite(best_r) and best_r >= 0.6 and best not in mates:
            flag = f"alone; very similar to {best}"
        rows.append({"series": sid, "tree_r": mean_r, "best_match": best, "best_r": best_r, "flag": flag})
    return pd.DataFrame(rows).set_index("series")


def _scheme_groups(series_ids: Sequence[str], scheme: str) -> Optional[Dict[str, Tuple[str, str]]]:
    """Split IDs by a scheme: 'last1', 'last2', 'last3' (characters) or 'letters' (trailing letters)."""
    out: Dict[str, Tuple[str, str]] = {}
    for sid in series_ids:
        n = norm_id(sid)
        if scheme.startswith("last"):
            k = int(scheme[4:])
            if len(n) <= k:
                return None
            out[sid] = (n[:-k], n[-k:])
        elif scheme == "letters":
            m = re.match(r"^(.*?\d)([A-Z]+)$", n)
            out[sid] = (m.group(1), m.group(2)) if m else (n, "1")
        elif scheme == "rnum":
            m = re.match(r"^(.*?)(R\d{1,2})$", n)
            out[sid] = (m.group(1), m.group(2)) if m else (n, "1")
        else:
            return None
    return out


SCHEME_LABELS = {
    "current": "current grouping",
    "letters": "trailing letters = radius (e.g. KC021A / KC021B)",
    "rnum": "R1, R2 ... = radius",
    "last1": "last character = radius",
    "last2": "last 2 characters = radius",
    "last3": "last 3 characters = radius",
}


def score_grouping(groups: Dict[str, str], r: pd.DataFrame) -> Dict[str, float]:
    """Within-tree vs between-tree correlation of a grouping {series: tree}."""
    ids = [s for s in groups if s in r.index]
    within: List[float] = []
    between: List[float] = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            v = r.at[a, b]
            if not np.isfinite(v):
                continue
            (within if groups[a] == groups[b] else between).append(float(v))
    sizes = pd.Series([groups[s] for s in ids]).value_counts()
    multi = int((sizes > 1).sum())
    grouped_series = int(sizes[sizes > 1].sum())
    return {
        "within_r": float(np.mean(within)) if within else np.nan,
        "between_r": float(np.median(between)) if between else np.nan,
        "n_within_pairs": len(within),
        "n_trees": int(len(sizes)),
        "multi_trees": multi,
        "grouped_series": grouped_series,
        "max_size": int(sizes.max()) if len(sizes) else 0,
    }


def infer_grouping_schemes(series_ids: Sequence[str], rwl: pd.DataFrame,
                           current: Optional[Dict[str, str]] = None,
                           r: Optional[pd.DataFrame] = None) -> List[Dict[str, Any]]:
    """Rank ID schemes by how well radii of one tree agree in ring width.

    Score = mean within-tree r - median between-tree r, only for schemes
    that group at least 2 pairs and keep trees small (<= 8 radii).
    """
    ids = [str(s) for s in series_ids]
    if r is None:
        r = pairwise_r(rwl[ids] if set(ids).issubset(set(map(str, rwl.columns))) else rwl)
    cands: List[Dict[str, Any]] = []
    if current:
        st = score_grouping(current, r)
        cands.append(dict(scheme="current", label=SCHEME_LABELS["current"], groups=dict(current), cores=None, **st))
    for scheme in ("letters", "rnum", "last1", "last2", "last3"):
        split = _scheme_groups(ids, scheme)
        if split is None:
            continue
        groups = {s: t for s, (t, _c) in split.items()}
        st = score_grouping(groups, r)
        if st["n_within_pairs"] < 2 or st["max_size"] > 8:
            continue
        cands.append(dict(scheme=scheme, label=SCHEME_LABELS[scheme], groups=groups,
                          cores={s: c for s, (_t, c) in split.items()}, **st))
    for c in cands:
        w, b = c["within_r"], c["between_r"]
        c["score"] = (w - (b if np.isfinite(b) else 0.0)) if np.isfinite(w) else -1.0
    # identical groupings: keep one (e.g. SS0101/SS0102 -> "last2" = 01/02 rather than "last1")
    unique: List[Dict[str, Any]] = []
    for c in cands:
        twin = next((u for u in unique if u["scheme"] != "current" and c["scheme"] != "current"
                     and _same_partition(u["groups"], c["groups"])), None)
        if twin is None:
            unique.append(c)
            continue
        cores = list((c.get("cores") or {}).values())
        if c["scheme"].startswith("last") and twin["scheme"].startswith("last") \
                and int(c["scheme"][4:]) > int(twin["scheme"][4:]) and cores and all(x.startswith("0") for x in cores):
            unique[unique.index(twin)] = c
    unique.sort(key=lambda c: c["score"], reverse=True)
    return unique


def _same_partition(a: Dict[str, str], b: Dict[str, str]) -> bool:
    if set(a) != set(b):
        return False
    pa = {frozenset(k for k in a if a[k] == v) for v in set(a.values())}
    pb = {frozenset(k for k in b if b[k] == v) for v in set(b.values())}
    return pa == pb
