#!/usr/bin/env python3
"""
dendro_moving.py - moving (running) correlation windows: temporal stability of the climate signal.

A chronology is correlated with one climate season (for example June-July mean temperature) in a
window of ``w`` years that is moved one year at a time (centred: the value is plotted at the middle
year of the window), as in TreeClim ``dcc(..., moving = TRUE)`` and in Fig. 4 of
Agapova et al. (2026, Agricultural and Forest Meteorology): "Centered 25-year running correlations ...
Dashed and dotted lines represent significant correlations at p < 0.01 and p < 0.05".

Besides the curves, every series gets stability statistics:

* full-period r, mean / min / max / SD of the running r, share of windows with p < 0.05;
* split-period test: r of the first and the second half of the common period (independent samples)
  compared with Fisher's z test - the formal test of a change in the signal;
* the year with the largest shift of the running r (where the change happens) and a short verdict
  (stable / weakening since ~year / strengthening / unstable / sign change).

Running windows overlap, so neighbouring values are not independent; the halves test does not have
this problem and is used for the verdicts. Optionally each window gets a bootstrap 95 % confidence
interval (percentile, resampling the years of the window).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WINDOW_LABELS = ["p" + m for m in MONTHS] + MONTHS  # 0..23: previous Jan .. current Dec

PRECIP_LIKE = ("precip", "pre", "ppt", "rain", "snow", "runoff", "discharge", "wet days", "aet", "pet",
               "evapotrans")


# ---------------------------------------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------------------------------------
def season_label(si: int, ei: int) -> str:
    return WINDOW_LABELS[si] if si == ei else f"{WINDOW_LABELS[si]}-{WINDOW_LABELS[ei]}"


def parse_season(text: str) -> Optional[Tuple[int, int]]:
    """'Jun_Jul', 'Jun-Jul', 'pJul_pSep', 'Aug' -> window indices (0..23)."""
    import re

    parts = [p for p in re.split(r"[_\-– ]+", str(text or "").strip()) if p]
    if not parts or len(parts) > 2:
        return None
    idx = []
    for p in parts:
        if p not in WINDOW_LABELS:
            return None
        idx.append(WINDOW_LABELS.index(p))
    if len(idx) == 1:
        idx.append(idx[0])
    return (idx[0], idx[1]) if idx[1] >= idx[0] else None


def default_agg(param: str) -> str:
    p = str(param or "").lower()
    return "sum" if any(w in p for w in PRECIP_LIKE) else "mean"


def season_series(monthly: pd.DataFrame, si: int, ei: int, agg: str = "mean",
                  min_months_frac: float = 1.0) -> pd.Series:
    """Seasonal value per year from a monthly table (index = year, columns 1..12).

    Months 0..11 of the window are taken from the previous year, 12..23 from the current year.
    A year needs at least ``min_months_frac`` of the months (sum is scaled to the full window).
    """
    m = monthly.copy()
    m.index = pd.to_numeric(m.index, errors="coerce")
    m = m[~m.index.isna()]
    m.index = m.index.astype(int)
    m = m.reindex(columns=list(range(1, 13)))
    years = np.arange(int(m.index.min()) if len(m) else 0, (int(m.index.max()) + 1) if len(m) else 0)
    m = m.reindex(years)
    vals = m.to_numpy(dtype=float)
    prev = np.vstack([np.full((1, 12), np.nan), vals[:-1]]) if len(vals) else vals
    full = np.hstack([prev, vals]) if len(vals) else np.zeros((0, 24))
    block = full[:, si:ei + 1]
    n_m = ei - si + 1
    valid = np.isfinite(block).sum(axis=1)
    import warnings

    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if agg == "sum":
            v = np.nansum(block, axis=1) * (n_m / np.maximum(valid, 1))
        else:
            v = np.nanmean(block, axis=1)
    v[valid < max(1, int(math.ceil(min_months_frac * n_m)))] = np.nan
    return pd.Series(v, index=years).dropna()


def r_critical(n: int, alpha: float = 0.05) -> float:
    """Two-sided critical |r| for n pairs (t distribution with n - 2 df)."""
    if n is None or n < 4:
        return float("nan")
    try:
        from scipy import stats

        t = float(stats.t.ppf(1.0 - alpha / 2.0, n - 2))
    except Exception:  # pragma: no cover
        t = {0.05: 1.96, 0.01: 2.576, 0.001: 3.291}.get(alpha, 1.96)
    return t / math.sqrt(n - 2 + t * t)


def _p_value(r: float, n: int) -> float:
    if not np.isfinite(r) or n < 4:
        return float("nan")
    r = max(min(r, 0.999999), -0.999999)
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    try:
        from scipy import stats

        return float(2.0 * stats.t.sf(abs(t), n - 2))
    except Exception:  # pragma: no cover
        return float(math.erfc(abs(t) / math.sqrt(2.0)))


def _rank(a: np.ndarray) -> np.ndarray:
    return pd.Series(a).rank().to_numpy(dtype=float)


def _corr(x: np.ndarray, y: np.ndarray, method: str) -> float:
    if method == "spearman":
        x, y = _rank(x), _rank(y)
    x = x - x.mean()
    y = y - y.mean()
    d = math.sqrt(float((x * x).sum() * (y * y).sum()))
    return float((x * y).sum() / d) if d > 0 else float("nan")


def _detrend(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    if len(y) < 3:
        return y - np.mean(y)
    k, c = np.polyfit(t, y, 1)
    return y - (k * t + c)


def fisher_z_test(r1: float, n1: int, r2: float, n2: int) -> Tuple[float, float]:
    """Difference of two independent correlations: (z, two-sided p)."""
    if not (np.isfinite(r1) and np.isfinite(r2)) or n1 < 4 or n2 < 4:
        return float("nan"), float("nan")
    z1 = math.atanh(max(min(r1, 0.999999), -0.999999))
    z2 = math.atanh(max(min(r2, 0.999999), -0.999999))
    z = (z1 - z2) / math.sqrt(1.0 / (n1 - 3) + 1.0 / (n2 - 3))
    return z, float(math.erfc(abs(z) / math.sqrt(2.0)))


# ---------------------------------------------------------------------------------------------------------
# moving correlation
# ---------------------------------------------------------------------------------------------------------
@dataclass
class MovingOptions:
    window: int = 25                 # years per window
    step: int = 1                    # years between windows
    position: str = "centre"         # year shown for a window: "centre" | "end" | "start"
    method: str = "pearson"          # "pearson" | "spearman"
    prep: str = "detrend_climate"    # "none" | "detrend_climate" | "detrend_both" | "diff" | "detrend_window"
    min_frac: float = 0.8            # minimum share of years with data in a window
    period: Optional[Tuple[int, int]] = None
    bootstrap: int = 0               # resamples for the 95 % CI (0 = off)
    seed: int = 42


@dataclass
class MovingItem:
    name: str                        # unique name (legend + tables)
    group: str                       # panel
    label: str                       # line label inside the panel (e.g. RES, TRW)
    chron: pd.Series                 # year -> value
    climate: pd.Series               # year -> seasonal climate value
    climate_label: str = ""          # e.g. "CRU tmp Jun-Jul mean"
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class MovingResult:
    item: MovingItem
    table: pd.DataFrame              # year, start, end, r, n, p, lo, hi
    full_r: float
    full_p: float
    full_n: int
    years: Tuple[int, int]
    stats: Dict[str, Any] = field(default_factory=dict)


def _prepare(item: MovingItem, opt: MovingOptions) -> pd.DataFrame:
    d = pd.concat([item.chron.rename("a"), item.climate.rename("b")], axis=1).sort_index()
    d.index = d.index.astype(int)
    if opt.period:
        d = d.loc[(d.index >= opt.period[0]) & (d.index <= opt.period[1])]
    d = d.dropna()
    if d.empty:
        return d
    t = d.index.to_numpy(dtype=float)
    if opt.prep == "diff":
        full = d.reindex(range(int(d.index.min()), int(d.index.max()) + 1))
        d = full.diff().dropna()
    elif opt.prep == "detrend_climate":
        d["b"] = _detrend(d["b"].to_numpy(dtype=float), t)
    elif opt.prep == "detrend_both":
        d["a"] = _detrend(d["a"].to_numpy(dtype=float), t)
        d["b"] = _detrend(d["b"].to_numpy(dtype=float), t)
    return d


def moving_correlation(item: MovingItem, opt: MovingOptions) -> MovingResult:
    d = _prepare(item, opt)
    w = int(opt.window)
    rows: List[Dict[str, Any]] = []
    if d.empty:
        return MovingResult(item, pd.DataFrame(columns=["year", "start", "end", "r", "n", "p", "lo", "hi"]),
                            float("nan"), float("nan"), 0, (0, 0))
    y0, y1 = int(d.index.min()), int(d.index.max())
    a_all, b_all = d["a"].to_numpy(dtype=float), d["b"].to_numpy(dtype=float)
    idx = d.index.to_numpy()
    full_r = _corr(a_all, b_all, opt.method) if len(d) >= 4 else float("nan")
    full_p = _p_value(full_r, len(d))
    rng = np.random.default_rng(opt.seed)
    need = max(5, int(math.ceil(opt.min_frac * w)))
    start = y0
    while start + w - 1 <= y1:
        end = start + w - 1
        m = (idx >= start) & (idx <= end)
        n = int(m.sum())
        if n >= need:
            a, b = a_all[m], b_all[m]
            if opt.prep == "detrend_window":
                tt = idx[m].astype(float)
                a, b = _detrend(a, tt), _detrend(b, tt)
            r = _corr(a, b, opt.method)
            lo = hi = float("nan")
            if opt.bootstrap and opt.bootstrap >= 100 and np.isfinite(r):
                lo, hi = _bootstrap_ci(a, b, opt.method, int(opt.bootstrap), rng)
            pos = {"centre": (start + end) / 2.0, "end": float(end), "start": float(start)}.get(opt.position,
                                                                                               (start + end) / 2.0)
            rows.append({"year": pos, "start": start, "end": end, "r": r, "n": n, "p": _p_value(r, n),
                         "lo": lo, "hi": hi})
        start += max(1, int(opt.step))
    tab = pd.DataFrame(rows, columns=["year", "start", "end", "r", "n", "p", "lo", "hi"])
    res = MovingResult(item, tab, full_r, full_p, len(d), (y0, y1))
    res.stats = stability_statistics(res, opt, d)
    return res


def _bootstrap_ci(a: np.ndarray, b: np.ndarray, method: str, nboot: int, rng) -> Tuple[float, float]:
    n = len(a)
    if method == "spearman":
        a, b = _rank(a), _rank(b)
    ii = rng.integers(0, n, size=(nboot, n))
    x, y = a[ii], b[ii]
    x = x - x.mean(axis=1, keepdims=True)
    y = y - y.mean(axis=1, keepdims=True)
    with np.errstate(all="ignore"):
        r = (x * y).sum(axis=1) / np.sqrt((x * x).sum(axis=1) * (y * y).sum(axis=1))
    r = r[np.isfinite(r)]
    if len(r) < 20:
        return float("nan"), float("nan")
    return float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))


# ---------------------------------------------------------------------------------------------------------
# stability statistics and verdicts
# ---------------------------------------------------------------------------------------------------------
def _change_year(d: pd.DataFrame, method: str, min_seg: int) -> Tuple[Optional[int], float, float]:
    """First year of the period after which the correlation differs most from the one before it.

    Every split year with at least ``min_seg`` years on both sides is tried; the split with the largest
    Fisher z statistic wins. Returns (year, r before, r after) - the same idea as a break-point search,
    done on the yearly data (not on the smoothed running r).
    """
    idx = d.index.to_numpy()
    a, b = d["a"].to_numpy(float), d["b"].to_numpy(float)
    if len(idx) < 2 * min_seg:
        return None, float("nan"), float("nan")
    best = (0.0, None, float("nan"), float("nan"))
    for k in range(min_seg, len(idx) - min_seg + 1):
        r1, r2 = _corr(a[:k], b[:k], method), _corr(a[k:], b[k:], method)
        z, _p = fisher_z_test(r1, k, r2, len(idx) - k)
        if np.isfinite(z) and abs(z) > abs(best[0]):
            best = (z, int(idx[k]), r1, r2)
    return best[1], best[2], best[3]


def stability_statistics(res: MovingResult, opt: MovingOptions, d: pd.DataFrame) -> Dict[str, Any]:
    tab = res.table
    out: Dict[str, Any] = {"full_r": res.full_r, "full_p": res.full_p, "full_n": res.full_n,
                           "period": f"{res.years[0]}-{res.years[1]}", "windows": int(len(tab))}
    if tab.empty:
        out["verdict"] = f"too short for {opt.window}-year windows"
        return out
    r = tab["r"].to_numpy(dtype=float)
    p = tab["p"].to_numpy(dtype=float)
    sign = 1.0 if (np.isfinite(res.full_r) and res.full_r >= 0) or not np.isfinite(res.full_r) else -1.0
    sig = (p < 0.05)
    sig_same = sig & (np.sign(r) == sign)
    sig_opp = sig & (np.sign(r) == -sign)
    out.update({
        "mean_r": float(np.nanmean(r)), "sd_r": float(np.nanstd(r)),
        "min_r": float(np.nanmin(r)), "min_year": float(tab["year"].iloc[int(np.nanargmin(r))]),
        "max_r": float(np.nanmax(r)), "max_year": float(tab["year"].iloc[int(np.nanargmax(r))]),
        "pct_sig05": float(100.0 * sig_same.mean()),
        "pct_sig01": float(100.0 * ((p < 0.01) & (np.sign(r) == sign)).mean()),
        "first_r": float(r[0]), "last_r": float(r[-1]),
    })
    # trend of the running r (descriptive only: windows overlap)
    yrs = tab["year"].to_numpy(dtype=float)
    ok = np.isfinite(r)
    out["trend_per_decade"] = float(np.polyfit(yrs[ok], r[ok], 1)[0] * 10.0) if ok.sum() >= 3 else float("nan")
    # split-period test (independent halves)
    idx = d.index.to_numpy()
    half = idx[len(idx) // 2] if len(idx) else 0
    m1, m2 = idx < half, idx >= half
    r1 = _corr(d["a"].to_numpy(float)[m1], d["b"].to_numpy(float)[m1], opt.method) if m1.sum() >= 5 else float("nan")
    r2 = _corr(d["a"].to_numpy(float)[m2], d["b"].to_numpy(float)[m2], opt.method) if m2.sum() >= 5 else float("nan")
    z, pz = fisher_z_test(r1, int(m1.sum()), r2, int(m2.sum()))
    out.update({"half1": f"{int(idx[m1].min())}-{int(idx[m1].max())}" if m1.any() else "",
                "r_half1": r1, "half2": f"{int(idx[m2].min())}-{int(idx[m2].max())}" if m2.any() else "",
                "r_half2": r2, "z_halves": z, "p_halves": pz})
    cy, rb, ra = _change_year(d, opt.method, max(10, int(opt.window) // 2 + 3))
    out["change_year"] = cy
    out["r_before_change"], out["r_after_change"] = rb, ra
    out["verdict"] = _verdict(out, sign, bool(sig_opp.any()), opt)
    return out


def _fmt_year(y: Optional[float]) -> str:
    if y is None or not np.isfinite(y):
        return "?"
    return str(int(round(y)))


def _verdict(s: Dict[str, Any], sign: float, opposite: bool, opt: MovingOptions) -> str:
    pct = s.get("pct_sig05", 0.0)
    pz = s.get("p_halves", float("nan"))
    r1, r2 = s.get("r_half1", float("nan")), s.get("r_half2", float("nan"))
    cy = s.get("change_year")
    when = "" if cy is None else (f" (change from about {_fmt_year(cy)}: {s.get('r_before_change', float('nan')):+.2f}"
                                  f" before, {s.get('r_after_change', float('nan')):+.2f} after)")
    if opposite and pct > 0:
        return "unstable: significant correlations of both signs" + when
    if np.isfinite(pz) and pz < 0.05:
        if abs(r2) < abs(r1):
            return f"weakening: {r1:+.2f} -> {r2:+.2f} (halves differ, p = {pz:.3f})" + when
        return f"strengthening: {r1:+.2f} -> {r2:+.2f} (halves differ, p = {pz:.3f})" + when
    if pct >= 90:
        return f"stable: significant in {pct:.0f}% of the windows"
    if pct >= 60:
        return f"mostly stable: significant in {pct:.0f}% of the windows" + (
            f"; weakest around {_fmt_year(s.get('min_year'))}" if sign > 0 else
            f"; weakest around {_fmt_year(s.get('max_year'))}")
    if pct > 0:
        return f"unstable / weak: significant in only {pct:.0f}% of the windows" + when
    return "no significant window"


def statistics_table(results: Sequence[MovingResult]) -> pd.DataFrame:
    rows = []
    for res in results:
        s = res.stats
        rows.append({
            "Series": res.item.name, "Panel": res.item.group, "Line": res.item.label,
            "Climate": res.item.climate_label, "Period": s.get("period", ""), "N": s.get("full_n", 0),
            "r (full)": s.get("full_r"), "p (full)": s.get("full_p"), "Windows": s.get("windows", 0),
            "Mean r": s.get("mean_r"), "SD r": s.get("sd_r"), "Min r": s.get("min_r"),
            "Min at": s.get("min_year"), "Max r": s.get("max_r"), "Max at": s.get("max_year"),
            "% windows p<0.05": s.get("pct_sig05"), "% windows p<0.01": s.get("pct_sig01"),
            "Trend of r per decade": s.get("trend_per_decade"),
            "First half": s.get("half1"), "r first half": s.get("r_half1"),
            "Second half": s.get("half2"), "r second half": s.get("r_half2"),
            "p (halves differ)": s.get("p_halves"), "Change from": s.get("change_year"),
            "r before change": s.get("r_before_change"), "r after change": s.get("r_after_change"),
            "Verdict": s.get("verdict", ""),
        })
    return pd.DataFrame(rows)


def long_table(results: Sequence[MovingResult], opt: MovingOptions) -> pd.DataFrame:
    parts = []
    c05, c01 = r_critical(opt.window, 0.05), r_critical(opt.window, 0.01)
    for res in results:
        t = res.table.copy()
        t.insert(0, "Climate", res.item.climate_label)
        t.insert(0, "Line", res.item.label)
        t.insert(0, "Panel", res.item.group)
        t.insert(0, "Series", res.item.name)
        t["r_crit_0.05"] = c05
        t["r_crit_0.01"] = c01
        parts.append(t)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def summary_text(results: Sequence[MovingResult], opt: MovingOptions) -> str:
    if not results:
        return "No result."
    c05, c01 = r_critical(opt.window, 0.05), r_critical(opt.window, 0.01)
    pos = {"centre": "centred", "end": "end-year", "start": "start-year"}.get(opt.position, "")
    prep = {"none": "no detrending", "detrend_climate": "climate linearly detrended",
            "detrend_both": "both series linearly detrended", "diff": "first differences",
            "detrend_window": "linear trends removed inside every window"}.get(opt.prep, opt.prep)
    lines = [f"{pos.capitalize()} {opt.window}-year running {opt.method.capitalize()} correlations, step "
             f"{opt.step} year(s), {prep}. For n = {opt.window}: |r| >= {c05:.2f} is significant at p < 0.05, "
             f"|r| >= {c01:.2f} at p < 0.01 (two-sided)."]
    ok = [r for r in results if r.stats.get("windows", 0) > 0]
    if ok:
        best = max(ok, key=lambda r: (r.stats.get("pct_sig05", 0), abs(r.stats.get("mean_r", 0))))
        lines.append(f"Most stable signal: {best.item.name} with {best.item.climate_label} "
                     f"(significant in {best.stats['pct_sig05']:.0f}% of the windows, mean r = "
                     f"{best.stats['mean_r']:+.2f}).")
        weak = [r for r in ok if str(r.stats.get("verdict", "")).startswith("weakening")]
        if weak:
            lines.append("Weakening (first vs second half differ): " + "; ".join(
                f"{r.item.name} {r.stats['r_half1']:+.2f} -> {r.stats['r_half2']:+.2f}"
                + (f" (from about {_fmt_year(r.stats.get('change_year'))})" if r.stats.get("change_year") else "")
                for r in weak) + ".")
        strong = [r for r in ok if str(r.stats.get("verdict", "")).startswith("strengthening")]
        if strong:
            lines.append("Strengthening: " + "; ".join(
                f"{r.item.name} {r.stats['r_half1']:+.2f} -> {r.stats['r_half2']:+.2f}" for r in strong) + ".")
        unstable = [r for r in ok if str(r.stats.get("verdict", "")).startswith(("unstable", "no significant"))]
        if unstable:
            lines.append("Unstable or weak: " + ", ".join(r.item.name for r in unstable) + ".")
        # within each panel: which line keeps the highest r
        groups: Dict[str, List[MovingResult]] = {}
        for r in ok:
            groups.setdefault(r.item.group, []).append(r)
        for g, rs in groups.items():
            if len(rs) > 1:
                top = max(rs, key=lambda r: abs(r.stats.get("mean_r", 0)))
                lines.append(f"{g}: {top.item.label} has the highest mean |r| ({abs(top.stats['mean_r']):.2f}).")
    lines.append("Note: running windows overlap, so neighbouring values are not independent; the change is "
                 "judged with the two independent halves of the period (Fisher's z test).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------
# plotting
# ---------------------------------------------------------------------------------------------------------
LINE_COLORS = ["#eb6834", "#1baf7a", "#2a78d6", "#6250d6", "#eda100", "#e87ba4", "#008300", "#e34948",
               "#52514e", "#00a3c4"]


def _short_label(text: str, n: int = 44) -> str:
    """Shorter legend text: drop repeated words such as 'Chronology', keep both ends."""
    t = str(text).replace(" | Chronology ", " | ").replace("Chronology ", "")
    t = t.replace("__", "_").strip(" |")
    return t if len(t) <= n else t[: n // 2 - 2] + "..." + t[-(n // 2 - 1):]


def draw_moving(fig, results: Sequence[MovingResult], opt: MovingOptions, ncols: int = 0,
                show_ci: bool = True, title: str = "", sharey: bool = True,
                ylim: Optional[Tuple[float, float]] = None) -> Dict[Any, List[Tuple[Any, MovingResult]]]:
    """Small multiples (one panel per group); returns {axes: [(line, result)]} for hover."""
    fig.clear()
    groups: Dict[str, List[MovingResult]] = {}
    for r in results:
        groups.setdefault(r.item.group, []).append(r)
    if not groups:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "Nothing to show", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return {}
    labels = []
    for r in results:
        if r.item.label not in labels:
            labels.append(r.item.label)
    color = {lab: LINE_COLORS[i % len(LINE_COLORS)] for i, lab in enumerate(labels)}
    n = len(groups)
    nc = ncols or (1 if n == 1 else 2 if n <= 4 else 3)
    nr = int(math.ceil(n / nc))
    axes = fig.subplots(nr, nc, sharex=True, sharey=sharey, squeeze=False)
    c05, c01 = r_critical(opt.window, 0.05), r_critical(opt.window, 0.01)
    allr = np.concatenate([r.table["r"].to_numpy(dtype=float) for r in results if not r.table.empty] or
                          [np.array([0.0])])
    lo = float(np.nanmin(allr)) if np.isfinite(allr).any() else -0.5
    hi = float(np.nanmax(allr)) if np.isfinite(allr).any() else 0.8
    lo, hi = min(lo, -c01, 0.0) - 0.08, max(hi, c01) + 0.08
    hover: Dict[Any, List[Tuple[Any, MovingResult]]] = {}
    for k, (g, rs) in enumerate(groups.items()):
        ax = axes[k // nc][k % nc]
        for lvl, ls in ((c01, (0, (5, 3))), (c05, (0, (1, 2)))):
            ax.axhline(lvl, color="#52514e", linewidth=0.8, linestyle=ls, zorder=1)
            ax.axhline(-lvl, color="#52514e", linewidth=0.8, linestyle=ls, zorder=1)
        ax.axhline(0, color="#0b0b0b", linewidth=0.8, zorder=1)
        hover[ax] = []
        for r in rs:
            t = r.table
            if t.empty:
                continue
            col = color[r.item.label]
            if show_ci and t["lo"].notna().any():
                ax.fill_between(t["year"], t["lo"], t["hi"], color=col, alpha=0.10, linewidth=0, zorder=2)
            (ln,) = ax.plot(t["year"], t["r"], color=col, linewidth=1.8, label=r.item.label, zorder=3)
            hover[ax].append((ln, r))
        gt = "\n".join(x if len(x) <= 62 else x[:28] + "..." + x[-31:] for x in g.split("  |  "))
        ax.set_title(gt, fontsize=8, fontweight="bold", loc="left", pad=3)
        ax.set_ylim(*(ylim or (lo, hi)))
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=8)
        from matplotlib.ticker import MaxNLocator

        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(True, axis="x", color="#e4e3df", linewidth=0.6)
    for k in range(n, nr * nc):
        axes[k // nc][k % nc].set_axis_off()
    pos = {"centre": "window centre", "end": "window end", "start": "window start"}.get(opt.position, "")
    import matplotlib.lines as mlines

    handles = [mlines.Line2D([0], [0], color=color[lab], lw=2) for lab in labels]
    handles += [mlines.Line2D([0], [0], color="#52514e", lw=0.8, linestyle=(0, (5, 3))),
                mlines.Line2D([0], [0], color="#52514e", lw=0.8, linestyle=(0, (1, 2)))]
    if len(labels) <= 1:  # one line per panel: the panel titles name it
        handles, labels = handles[len(labels):], []
    leg_labels = [_short_label(x) for x in labels] + ["p < 0.01", "p < 0.05"]
    # legend columns from the width of the figure and the longest label; rows get their own space
    fw, fh = fig.get_size_inches()
    longest = max(len(x) for x in leg_labels)
    per_col_in = 0.45 + 0.062 * longest
    ncol = max(1, min(len(leg_labels), int((fw - 0.4) // per_col_in)))
    nrows = int(math.ceil(len(leg_labels) / ncol))
    row_h = 0.21 / max(fh, 1.0)
    leg_h = nrows * row_h + 0.02
    fig.legend(handles, leg_labels, loc="lower center", bbox_to_anchor=(0.5, 0.005), ncol=ncol, frameon=False,
               fontsize=8, handlelength=2.2, columnspacing=1.2)
    xlab_y = leg_h + 0.012
    fig.supxlabel(f"Year ({pos})", fontsize=9, y=xlab_y, va="bottom")
    fig.supylabel("Correlation coefficient", fontsize=9)
    head = title or (f"Centred {opt.window}-year running correlations" if opt.position == "centre"
                     else f"{opt.window}-year running correlations")
    fig.suptitle(head, fontsize=10)
    try:
        fig.tight_layout(rect=(0.02, xlab_y + 0.035, 1, 0.96), h_pad=1.2, w_pad=1.0)
    except Exception:
        pass
    return hover


# ---------------------------------------------------------------------------------------------------------
# screening: every season x every climate data set, ranked by stability (independent of other results)
# ---------------------------------------------------------------------------------------------------------
def all_windows(max_len: int = 12) -> List[Tuple[int, int]]:
    """Every window of 1..max_len consecutive months between previous January and current December."""
    out = []
    for length in range(1, int(max_len) + 1):
        for si in range(0, 24 - length + 1):
            out.append((si, si + length - 1))
    return out


def _season_matrix(monthly: pd.DataFrame, years: np.ndarray, windows: List[Tuple[int, int]]) -> np.ndarray:
    """(years x windows) mean of every window; NaN when a month of the window is missing."""
    m = monthly.copy()
    m.index = pd.to_numeric(m.index, errors="coerce")
    m = m[~m.index.isna()]
    m.index = m.index.astype(int)
    m = m.groupby(level=0).mean().reindex(columns=list(range(1, 13)))
    cur = m.reindex(years).to_numpy(dtype=float)
    prev = m.reindex(years - 1).to_numpy(dtype=float)
    x = np.hstack([prev, cur])                       # (Y, 24)
    ok = np.isfinite(x)
    cs = np.concatenate([np.zeros((len(years), 1)), np.cumsum(np.where(ok, x, 0.0), axis=1)], axis=1)
    cn = np.concatenate([np.zeros((len(years), 1)), np.cumsum(ok, axis=1)], axis=1)
    si = np.array([w[0] for w in windows])
    ei = np.array([w[1] for w in windows]) + 1
    tot = cs[:, ei] - cs[:, si]
    cnt = cn[:, ei] - cn[:, si]
    lens = (ei - si)[None, :]
    with np.errstate(all="ignore"):
        out = tot / lens
    out[cnt < lens] = np.nan
    return out


def _detrend_matrix(a: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Remove the linear trend of every column (NaN-aware)."""
    out = np.array(a, dtype=float, copy=True)
    if out.ndim == 1:
        out = out[:, None]
    ok = np.isfinite(out)
    n = ok.sum(axis=0)
    tt = np.where(ok, t[:, None], 0.0)
    yy = np.where(ok, out, 0.0)
    with np.errstate(all="ignore"):
        tm = tt.sum(axis=0) / n
        ym = yy.sum(axis=0) / n
        cov = (np.where(ok, (t[:, None] - tm) * (out - ym), 0.0)).sum(axis=0)
        var = (np.where(ok, (t[:, None] - tm) ** 2, 0.0)).sum(axis=0)
        b = cov / var
    res = out - (ym + b * (t[:, None] - tm))
    res[~ok] = np.nan
    return res if a.ndim > 1 else res[:, 0]


def _rolling_r(x: np.ndarray, y: np.ndarray, w: int, need: int) -> Tuple[np.ndarray, np.ndarray]:
    """Running Pearson r of x (T) with every column of y (T x K) in windows of w years: (P x K) r and n."""
    v = np.isfinite(x)[:, None] & np.isfinite(y)
    xv = np.where(v, x[:, None], 0.0)
    yv = np.where(v, y, 0.0)

    def roll(a):
        c = np.concatenate([np.zeros((1,) + a.shape[1:]), np.cumsum(a, axis=0)], axis=0)
        return c[w:] - c[:-w]

    n = roll(v.astype(float))
    sx, sy = roll(xv), roll(yv)
    sxx, syy, sxy = roll(xv * xv), roll(yv * yv), roll(xv * yv)
    with np.errstate(all="ignore"):
        cov = sxy - sx * sy / n
        vx = sxx - sx * sx / n
        vy = syy - sy * sy / n
        r = cov / np.sqrt(vx * vy)
    r[(n < need) | ~np.isfinite(r)] = np.nan
    return np.clip(r, -1, 1), n


def _p_from_r(r: np.ndarray, n: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        t = r * np.sqrt((n - 2) / np.maximum(1e-12, 1 - r * r))
    try:
        from scipy.special import stdtr

        return 2.0 * stdtr(np.maximum(n - 2, 1), -np.abs(t))
    except Exception:  # pragma: no cover
        return np.vectorize(lambda v: math.erfc(abs(v) / math.sqrt(2)) if np.isfinite(v) else np.nan)(t)


def _screen_scores(x: np.ndarray, S: np.ndarray, years: np.ndarray, opt: "MovingOptions",
                   min_windows: int) -> Dict[str, np.ndarray]:
    """Stability statistics of x against every column of S (vectorized)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return _screen_scores_impl(x, S, years, opt, min_windows)


def _screen_scores_impl(x: np.ndarray, S: np.ndarray, years: np.ndarray, opt: "MovingOptions",
                        min_windows: int) -> Dict[str, np.ndarray]:
    w = int(opt.window)
    need = max(5, int(math.ceil(opt.min_frac * w)))
    r, n = _rolling_r(x, S, w, need)
    p = _p_from_r(r, n)
    ok = np.isfinite(r)
    nwin = ok.sum(axis=0)
    # full-period r (sign of the relationship)
    v = np.isfinite(x)[:, None] & np.isfinite(S)
    xv, sv = np.where(v, x[:, None], np.nan), np.where(v, S, np.nan)
    with np.errstate(all="ignore"):
        xm, sm = np.nanmean(xv, axis=0), np.nanmean(sv, axis=0)
        cov = np.nansum((xv - xm) * (sv - sm), axis=0)
        rf = cov / np.sqrt(np.nansum((xv - xm) ** 2, axis=0) * np.nansum((sv - sm) ** 2, axis=0))
        nf = v.sum(axis=0)
        sign = np.where(rf >= 0, 1.0, -1.0)
        sig_same = ok & (p < 0.05) & (np.sign(r) == sign[None, :])
        pct = 100.0 * sig_same.sum(axis=0) / np.maximum(nwin, 1)
        mean_r = np.nanmean(np.where(ok, r, np.nan), axis=0)
        min_r = np.nanmin(np.where(ok, r, np.nan), axis=0)
        max_r = np.nanmax(np.where(ok, r, np.nan), axis=0)
        # halves: r of the first and second half of the common years
        mid = np.nanmedian(np.where(v, years[:, None], np.nan), axis=0)
        def half(mask):
            vv = v & mask
            a = np.where(vv, x[:, None], np.nan)
            b = np.where(vv, S, np.nan)
            am, bm = np.nanmean(a, axis=0), np.nanmean(b, axis=0)
            c = np.nansum((a - am) * (b - bm), axis=0)
            rr = c / np.sqrt(np.nansum((a - am) ** 2, axis=0) * np.nansum((b - bm) ** 2, axis=0))
            return np.clip(rr, -0.999999, 0.999999), vv.sum(axis=0)
        r1, n1 = half(years[:, None] < mid[None, :])
        r2, n2 = half(years[:, None] >= mid[None, :])
        z = (np.arctanh(r1) - np.arctanh(r2)) / np.sqrt(1.0 / np.maximum(n1 - 3, 1) + 1.0 / np.maximum(n2 - 3, 1))
        try:
            from scipy.special import ndtr

            ph = 2.0 * ndtr(-np.abs(z))
        except Exception:  # pragma: no cover
            ph = np.vectorize(lambda q: math.erfc(abs(q) / math.sqrt(2)) if np.isfinite(q) else np.nan)(z)
    score = pct - np.where(ph < 0.05, 25.0, 0.0) + 10.0 * np.nan_to_num(np.abs(mean_r))
    score[nwin < min_windows] = np.nan
    return {"score": score, "pct": pct, "mean_r": mean_r, "min_r": min_r, "max_r": max_r, "r_full": rf,
            "n_full": nf, "nwin": nwin, "r_half1": r1, "r_half2": r2, "p_halves": ph}


def _prep_pair(x: np.ndarray, S: np.ndarray, years: np.ndarray, prep: str) -> Tuple[np.ndarray, np.ndarray]:
    t = years.astype(float)
    if prep in ("detrend_climate", "detrend_both", "detrend_window"):
        S = _detrend_matrix(S, t)
    if prep in ("detrend_both", "detrend_window"):
        x = _detrend_matrix(x, t)
    if prep == "diff":
        x = np.concatenate([[np.nan], np.diff(x)])
        S = np.vstack([np.full((1, S.shape[1]), np.nan), np.diff(S, axis=0)])
    return x, S


def screen_chronology(name: str, chron: pd.Series, climates: Dict[str, Dict[str, Any]], opt: "MovingOptions",
                      max_len: int = 12, n_perm: int = 100, min_windows: int = 20, keep: int = 25,
                      seed: int = 7, cancel=None) -> pd.DataFrame:
    """All climate data sets x all windows for one chronology, ranked by the stability of the running r.

    ``climates``: name -> dict(monthly=DataFrame year x 1..12, param=str). The permutation control shuffles
    the years of the chronology ``n_perm`` times and repeats the whole screening; ``p_perm`` is the share of
    shuffles whose best score reaches the score of the row (a family-wise control over all windows and data sets).
    """
    windows = all_windows(max_len)
    c = pd.to_numeric(pd.Series(chron), errors="coerce").dropna()
    c.index = pd.Index(c.index).astype(int)
    rows: List[Dict[str, Any]] = []
    adjustments: List[str] = []
    skipped: List[str] = []
    prepared = []
    min_window = 7  # matches the minimum selectable moving-correlation window
    min_windows = max(1, int(min_windows))
    requested_window = max(min_window, int(opt.window))
    for cname, cd in climates.items():
        if cancel is not None and cancel():
            break
        mon = cd.get("monthly")
        if mon is None or len(mon) == 0:
            skipped.append(f"{name} × {cname}: no monthly climate data")
            continue
        climate_years = pd.to_numeric(pd.Index(mon.index), errors="coerce")
        climate_years = climate_years[np.isfinite(climate_years)]
        if c.empty or not len(climate_years):
            skipped.append(f"{name} × {cname}: no valid chronology/climate years")
            continue
        lo = max(int(c.index.min()), int(climate_years.min()) + 1)
        hi = min(int(c.index.max()), int(climate_years.max()))
        if opt.period:
            lo, hi = max(lo, int(opt.period[0])), min(hi, int(opt.period[1]))
        span = hi - lo + 1
        if span <= 0:
            skipped.append(f"{name} × {cname}: no overlapping years in the selected period")
            continue
        largest_allowed = min(requested_window, span - min_windows + 1)
        if largest_allowed < min_window:
            required = min_window + min_windows - 1
            skipped.append(f"{name} × {cname}: common period {lo}–{hi} has {span} years; at least "
                           f"{required} years are required for a {min_window}-year window and "
                           f"{min_windows} valid running windows")
            continue
        years = np.arange(lo, hi + 1)
        S = _season_matrix(mon, years, windows)
        x = c.reindex(years).to_numpy(dtype=float)
        x, S = _prep_pair(x, S, years, opt.prep)
        if opt.method == "spearman":
            x = pd.Series(x).rank().to_numpy()
            S = pd.DataFrame(S).rank().to_numpy()
        chosen_window = None
        chosen_opt = None
        st = None
        for candidate in range(largest_allowed, min_window - 1, -1):
            candidate_opt = replace(opt, window=candidate)
            candidate_stats = _screen_scores(x, S, years, candidate_opt, min_windows)
            if np.isfinite(candidate_stats["score"]).any():
                chosen_window, chosen_opt, st = candidate, candidate_opt, candidate_stats
                break
        if st is None or chosen_window is None or chosen_opt is None:
            skipped.append(f"{name} × {cname}: fewer than {min_windows} valid running windows even with the "
                           f"minimum {min_window}-year window; skipped")
            continue
        if chosen_window < requested_window:
            adjustments.append(f"{name} × {cname}: {requested_window} → {chosen_window} years "
                               f"(common period {lo}–{hi})")
        prepared.append((cname, years, x, S, chosen_opt))
        for k, (si, ei) in enumerate(windows):
            if not np.isfinite(st["score"][k]):
                continue
            rows.append({"Chronology": name, "Climate": cname, "Type": cd.get("param", ""), "Window": season_label(si, ei),
                         "Window years": chosen_window, "si": si, "ei": ei, "Period": f"{lo}-{hi}",
                         "Score": float(st["score"][k]),
                         "% windows p<0.05": float(st["pct"][k]), "Mean r": float(st["mean_r"][k]),
                         "Min r": float(st["min_r"][k]), "Max r": float(st["max_r"][k]),
                         "r (full)": float(st["r_full"][k]), "N": int(st["n_full"][k]), "Windows": int(st["nwin"][k]),
                         "r 1st half": float(st["r_half1"][k]), "r 2nd half": float(st["r_half2"][k]),
                         "p (halves differ)": float(st["p_halves"][k])})
    if not rows:
        empty = pd.DataFrame()
        empty.attrs["screening_adjustments"] = adjustments
        empty.attrs["screening_skipped"] = skipped
        return empty
    df = pd.DataFrame(rows).sort_values(["Score", "Mean r"], ascending=[False, False], key=None)
    df = df.sort_values("Score", ascending=False, kind="mergesort").reset_index(drop=True)
    # permutation control: best score of the whole screening on shuffled chronologies
    if n_perm and n_perm > 0:
        rng = np.random.default_rng(seed)
        best = np.full(int(n_perm), -np.inf)
        for b in range(int(n_perm)):
            if cancel is not None and cancel():
                break
            for cname, years, x, S, pair_opt in prepared:
                okx = np.isfinite(x)
                xs = x.copy()
                xs[okx] = rng.permutation(x[okx])
                st = _screen_scores(xs, S, years, pair_opt, min_windows)
                m = np.nanmax(st["score"]) if np.isfinite(st["score"]).any() else -np.inf
                best[b] = max(best[b], m)
        best = best[np.isfinite(best)]
        if len(best):
            df["p (shuffled)"] = [float((np.sum(best >= s) + 1) / (len(best) + 1)) for s in df["Score"]]
            df["Chance best score (95%)"] = float(np.percentile(best, 95))
    df["Verdict"] = [_screen_verdict(r) for _i, r in df.iterrows()]
    df = df.head(int(keep)).reset_index(drop=True)
    df.attrs["screening_adjustments"] = adjustments
    df.attrs["screening_skipped"] = skipped
    return df


def _screen_verdict(r) -> str:
    p = r.get("p (shuffled)", float("nan"))
    sig = "" if not np.isfinite(p) else (" - beats chance (p = %.3f)" % p if p < 0.05 else " - could be chance (p = %.2f)" % p)
    ph = r.get("p (halves differ)", float("nan"))
    if np.isfinite(ph) and ph < 0.05:
        kind = "changing (%+.2f -> %+.2f)" % (r["r 1st half"], r["r 2nd half"])
    elif r["% windows p<0.05"] >= 90:
        kind = "stable"
    elif r["% windows p<0.05"] >= 60:
        kind = "mostly stable"
    else:
        kind = "weak / unstable"
    return kind + sig


def _screen_worker(args):
    name, chron, climates, opt, max_len, n_perm, min_windows, keep = args
    return screen_chronology(name, chron, climates, opt, max_len, n_perm, min_windows, keep)


def screen_all(chronologies: Dict[str, pd.Series], climates: Dict[str, Dict[str, Any]], opt: "MovingOptions",
               max_len: int = 12, n_perm: int = 100, min_windows: int = 20, keep: int = 25, workers: int = 0,
               progress=None, cancel=None) -> pd.DataFrame:
    """Screen every chronology (one task each, in parallel processes when there are several)."""
    tasks = [(n, s, climates, opt, max_len, n_perm, min_windows, keep) for n, s in chronologies.items()]
    out: List[pd.DataFrame] = []
    cpu = os.cpu_count() or 1
    nw = workers or max(1, min(cpu - 1, len(tasks)))
    if nw > 1 and len(tasks) > 1:
        import concurrent.futures as cf
        import multiprocessing as mp

        try:
            with cf.ProcessPoolExecutor(max_workers=nw, mp_context=mp.get_context("spawn")) as ex:
                futs = [ex.submit(_screen_worker, t) for t in tasks]
                pending = set(futs)
                while pending:
                    if cancel is not None and cancel():
                        for f in pending:
                            f.cancel()
                        ex.shutdown(wait=False, cancel_futures=True)
                        return pd.DataFrame()
                    done, pending = cf.wait(pending, timeout=0.3, return_when=cf.FIRST_COMPLETED)
                    if progress is not None:
                        progress(len(futs) - len(pending), len(futs))
                out = [f.result() for f in futs]
        except Exception:
            out = []
    if not out:
        for k, t in enumerate(tasks):
            if cancel is not None and cancel():
                break
            out.append(_screen_worker(t))
            if progress is not None:
                progress(k + 1, len(tasks))
    out = [d for d in out if d is not None]
    adjustments = [line for d in out for line in d.attrs.get("screening_adjustments", [])]
    skipped = [line for d in out for line in d.attrs.get("screening_skipped", [])]
    nonempty = [d for d in out if not d.empty]
    result = pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame()
    result.attrs["screening_adjustments"] = adjustments
    result.attrs["screening_skipped"] = skipped
    return result
