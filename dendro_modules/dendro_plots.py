#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_plots.py - shared matplotlib plots for Detrend Advisor and RingClim.

Works with any canvas (Tk, Qt, Agg). All functions draw into axes that the
caller creates, so the apps keep control of layout.

Style rules: one y-scale per panel (no twin axes; a second measure gets its
own panel with a shared x axis), diverging blue - grey - red scale for
correlations, fixed categorical colour order, thin lines, recessive grid,
significance always shown with the critical r for the actual n.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

# ---------------------------------------------------------------------------
# Palette (validated reference palette; light surface)
# ---------------------------------------------------------------------------
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e4e3df"
NEUTRAL = "#f0efec"
NEG = "#2a78d6"     # negative correlation
POS = "#e34948"     # positive correlation
OTHER_LINE = "#b9b8b3"
DEPTH_FILL = "#cde2fb"
DEPTH_LINE = "#3987e5"

CORR_CMAP = LinearSegmentedColormap.from_list(
    "dendro_corr", ["#104281", NEG, "#9ec5f4", NEUTRAL, "#f3a9a8", POS, "#8f1d1c"], N=256
)


def style_axes(ax, grid_axis: str = "both") -> None:
    """Recessive grid and spines, readable ticks."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=TEXT_2, labelsize=8)
    ax.xaxis.label.set_color(TEXT_2)
    ax.yaxis.label.set_color(TEXT_2)
    ax.title.set_color(TEXT)
    if grid_axis:
        ax.grid(True, axis=grid_axis, color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)


def message(ax, text: str) -> None:
    ax.clear()
    ax.text(0.5, 0.5, text, ha="center", va="center", transform=ax.transAxes, color=TEXT_2, fontsize=10,
            wrap=True)
    ax.set_axis_off()


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def r_critical(n: int, alpha: float = 0.05) -> float:
    """Two-sided critical |r| for n paired values (t distribution)."""
    if n is None or n < 4:
        return float("nan")
    try:
        from scipy import stats

        t = stats.t.ppf(1.0 - alpha / 2.0, n - 2)
    except Exception:  # normal approximation
        t = 1.96
    return float(t / math.sqrt(n - 2 + t * t))


def pearson(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float, int]:
    """r, two-sided p, n on the finite overlap."""
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    if n < 4 or np.nanstd(x[m]) == 0 or np.nanstd(y[m]) == 0:
        return float("nan"), float("nan"), n
    r = float(np.corrcoef(x[m], y[m])[0, 1])
    try:
        from scipy import stats

        t = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
        p = float(2 * stats.t.sf(abs(t), n - 2))
    except Exception:
        p = float("nan")
    return r, p, n


def zscore(values: Sequence[float]) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    mu = np.nanmean(v)
    sd = np.nanstd(v)
    return (v - mu) / sd if sd and np.isfinite(sd) else v * np.nan


def running_corr(a: pd.Series, b: pd.Series, window: int = 31, min_periods: Optional[int] = None) -> pd.Series:
    """Centred moving-window Pearson correlation of two year-indexed series."""
    df = pd.concat([a.rename("a"), b.rename("b")], axis=1).sort_index()
    mp = min_periods or max(10, int(window * 0.8))
    return df["a"].rolling(window, center=True, min_periods=mp).corr(df["b"])


def format_p(p: float) -> str:
    if not np.isfinite(p):
        return "p = n/a"
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def corr_bars(ax, labels: Sequence[str], r: Sequence[float], n: Optional[Sequence[int]] = None,
              highlight: Optional[str] = None, horizontal: bool = True, title: str = "",
              alpha: float = 0.05) -> None:
    """Correlation bars coloured by sign; significant bars solid, others light, r_crit lines."""
    ax.clear()
    labels = [str(x) for x in labels]
    rv = np.asarray(r, dtype=float)
    nv = list(n) if n is not None else [None] * len(labels)
    crit = [r_critical(int(k), alpha) if k else float("nan") for k in nv]
    colors = []
    for val, c in zip(rv, crit):
        base = POS if val >= 0 else NEG
        sig = np.isfinite(c) and abs(val) >= c
        colors.append(base if sig else ("#f3a9a8" if val >= 0 else "#9ec5f4"))
    pos = np.arange(len(labels))
    if horizontal:
        ax.barh(pos, rv, color=colors, height=0.72, edgecolor="white", linewidth=0.8)
        ax.set_yticks(pos)
        ax.set_yticklabels(labels, fontsize=8 if len(labels) <= 10 else 6.5)
        ax.axvline(0, color=MUTED, linewidth=0.8)
        ax.set_xlabel("r")
        crit_one = np.nanmedian(crit) if np.any(np.isfinite(crit)) else float("nan")
        if np.isfinite(crit_one):
            for s in (-1, 1):
                ax.axvline(s * crit_one, color=TEXT_2, linewidth=0.8, linestyle="--")
        if highlight is not None and highlight in labels:
            i = labels.index(highlight)
            ax.get_yticklabels()[i].set_fontweight("bold")
            ax.get_yticklabels()[i].set_color(TEXT)
        lim = max(0.1, float(np.nanmax(np.abs(rv))) if np.any(np.isfinite(rv)) else 0.1) * 1.15
        ax.set_xlim(-lim if np.nanmin(rv) < 0 else -lim * 0.15, lim)
        style_axes(ax, grid_axis="x")
    else:
        ax.bar(pos, rv, color=colors, width=0.72, edgecolor="white", linewidth=0.8)
        ax.set_xticks(pos)
        ax.set_xticklabels(labels, fontsize=8, rotation=90)
        ax.axhline(0, color=MUTED, linewidth=0.8)
        ax.set_ylabel("r")
        crit_one = np.nanmedian(crit) if np.any(np.isfinite(crit)) else float("nan")
        if np.isfinite(crit_one):
            for s in (-1, 1):
                ax.axhline(s * crit_one, color=TEXT_2, linewidth=0.8, linestyle="--")
        if highlight is not None and highlight in labels:
            i = labels.index(highlight)
            ax.get_xticklabels()[i].set_fontweight("bold")
            ax.get_xticklabels()[i].set_color(TEXT)
        style_axes(ax, grid_axis="y")
    note = f"dashed lines: significance limit (p<{alpha:g}); pale bars: not significant"
    if np.any(np.isfinite(crit)):
        ax.set_title((title + "\n" if title else "") + note, fontsize=9)
    elif title:
        ax.set_title(title, fontsize=10)


def monthly_bars(ax, month_labels: Sequence[str], r: Sequence[float], n: int, title: str = "",
                 alpha: float = 0.05, divider_after: Optional[int] = None) -> None:
    """Classic dendroclimatic response bars (previous-year months, current-year months)."""
    corr_bars(ax, month_labels, r, n=[n] * len(month_labels), horizontal=False, title=title, alpha=alpha)
    if divider_after is not None and 0 < divider_after < len(month_labels):
        ax.axvline(divider_after - 0.5, color=MUTED, linewidth=1.0)
        ymax = ax.get_ylim()[1]
        ax.text(divider_after / 2 - 0.5, ymax, "previous year", ha="center", va="top", fontsize=7, color=TEXT_2)
        ax.text(divider_after + (len(month_labels) - divider_after) / 2 - 0.5, ymax, "current year",
                ha="center", va="top", fontsize=7, color=TEXT_2)


def corr_matrix(ax, corr: pd.DataFrame, cax=None, annotate: Optional[bool] = None, title: str = "",
                highlight: Optional[str] = None):
    """Correlation matrix with a diverging scale; values written in the cells when it fits."""
    ax.clear()
    data = corr.to_numpy(dtype=float).copy()
    if data.shape[0] == data.shape[1]:
        np.fill_diagonal(data, np.nan)   # r = 1 on the diagonal carries no information
    finite = np.abs(data[np.isfinite(data) & ~np.eye(len(data), dtype=bool)]) if data.size else np.array([])
    vmax = max(0.2, float(np.nanmax(finite))) if finite.size else 1.0
    vmax = min(1.0, math.ceil(vmax * 10) / 10)
    im = ax.imshow(data, cmap=CORR_CMAP, vmin=-vmax, vmax=vmax, aspect="auto", interpolation="nearest")
    labels = [str(c) for c in corr.columns]
    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    fs = 8 if len(labels) <= 12 else (6 if len(labels) <= 25 else 5)
    ax.set_xticklabels(labels, rotation=90, fontsize=fs)
    ax.set_yticklabels(labels, fontsize=fs)
    if annotate is None:
        annotate = len(labels) <= 16
    if annotate:
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                v = data[i, j]
                if not np.isfinite(v) or i == j:
                    continue
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=max(5, fs - 1),
                        color="white" if abs(v) > 0.65 * vmax else TEXT)
    if highlight is not None and highlight in labels:
        k = labels.index(highlight)
        ax.get_xticklabels()[k].set_fontweight("bold")
        ax.get_yticklabels()[k].set_fontweight("bold")
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(colors=TEXT_2, length=0)
    if title:
        ax.set_title(title, fontsize=10, color=TEXT)
    if cax is not None:
        cb = ax.figure.colorbar(im, cax=cax)
    else:
        cb = ax.figure.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
    cb.set_label("r", color=TEXT_2)
    cb.outline.set_visible(False)
    cb.ax.tick_params(labelsize=7, colors=TEXT_2)
    return im


def zscore_overlay(ax, years: Sequence[int], a: Sequence[float], b: Sequence[float],
                   label_a: str, label_b: str, title: str = "", smooth: int = 0) -> Tuple[float, float, int]:
    """Two series as z-scores on one axis (common base, no twin axis). Returns r, p, n."""
    ax.clear()
    yrs = np.asarray(years, dtype=float)
    za, zb = zscore(a), zscore(b)
    r, p, n = pearson(a, b)
    ax.plot(yrs, za, color=SERIES[0], linewidth=1.1, label=label_a)
    ax.plot(yrs, zb, color=SERIES[1], linewidth=1.1, label=label_b, alpha=0.9)
    if smooth and smooth >= 3:
        sa = pd.Series(za, index=yrs).rolling(smooth, center=True, min_periods=max(3, smooth // 2)).mean()
        sb = pd.Series(zb, index=yrs).rolling(smooth, center=True, min_periods=max(3, smooth // 2)).mean()
        ax.plot(sa.index, sa.values, color=SERIES[0], linewidth=2.2, alpha=0.55)
        ax.plot(sb.index, sb.values, color=SERIES[1], linewidth=2.2, alpha=0.55)
    ax.axhline(0, color=MUTED, linewidth=0.7)
    ax.set_ylabel("z-score")
    ax.set_xlabel("Year")
    head = title or "Series (standardised)"
    ax.set_title(f"{head}   r = {r:.2f}, {format_p(p)}, n = {n}" if np.isfinite(r) else head, fontsize=10)
    ax.legend(fontsize=8, frameon=False, loc="upper left", ncol=2)
    style_axes(ax)
    return r, p, n


def running_corr_plot(ax, a: pd.Series, b: pd.Series, window: int = 31, title: str = "") -> None:
    """Moving-window correlation with the significance band for the window length."""
    ax.clear()
    rc = running_corr(a, b, window=window).dropna()
    if rc.empty:
        message(ax, f"Not enough overlap for a {window}-year running correlation")
        return
    crit = r_critical(window)
    ax.axhspan(-crit, crit, color=NEUTRAL, zorder=0)
    ax.plot(rc.index, rc.values, color=TEXT, linewidth=1.3)
    ax.fill_between(rc.index, rc.values, crit, where=rc.values >= crit, color=POS, alpha=0.25, interpolate=True)
    ax.fill_between(rc.index, rc.values, -crit, where=rc.values <= -crit, color=NEG, alpha=0.25, interpolate=True)
    ax.axhline(0, color=MUTED, linewidth=0.7)
    ax.set_ylim(-1, 1)
    ax.set_ylabel("r")
    ax.set_xlabel("Year (window centre)")
    ax.set_title(title or f"{window}-year running correlation (grey band: not significant, p>=0.05)", fontsize=10)
    style_axes(ax)


def scatter_fit(ax, a: Sequence[float], b: Sequence[float], label_a: str, label_b: str, title: str = "") -> None:
    ax.clear()
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 4:
        message(ax, "Not enough data for a scatter plot")
        return
    ax.scatter(x[m], y[m], s=16, color=SERIES[0], alpha=0.7, edgecolors="white", linewidths=0.6)
    k, c = np.polyfit(x[m], y[m], 1)
    xs = np.linspace(np.nanmin(x[m]), np.nanmax(x[m]), 50)
    ax.plot(xs, k * xs + c, color=SERIES[1], linewidth=1.6)
    r, p, n = pearson(x, y)
    ax.set_xlabel(label_a)
    ax.set_ylabel(label_b)
    ax.set_title(title or f"r = {r:.2f}, R² = {r * r:.2f}, {format_p(p)}, n = {n}", fontsize=10)
    style_axes(ax)


def window_heatmap(ax, grid: np.ndarray, lengths: Sequence[int], start_labels: Sequence[str],
                   n: int, best: Optional[Tuple[int, int]] = None, cax=None, year_split: int = 12,
                   title: str = "", marked: Optional[Sequence[Tuple[int, int]]] = None):
    """Correlation of climate windows: rows = window length, columns = first month.

    The colour scale is symmetric around 0 and set by the strongest |r| present
    (not -1..1), so typical climate signals stay readable. Cells with |r| below
    the critical value for n are drawn lighter; the best window gets a star.
    """
    ax.clear()
    g = np.asarray(grid, dtype=float)
    finite = np.abs(g[np.isfinite(g)])
    vmax = max(0.2, float(finite.max())) if finite.size else 1.0
    vmax = min(1.0, math.ceil(vmax * 20) / 20)
    crit = r_critical(n)
    im = ax.imshow(g, cmap=CORR_CMAP, vmin=-vmax, vmax=vmax, aspect="auto", origin="lower",
                   interpolation="nearest")
    if np.isfinite(crit):
        insig = np.where(np.isfinite(g) & (np.abs(g) < crit), 1.0, np.nan)
        ax.imshow(insig, cmap=LinearSegmentedColormap.from_list("veil", ["white", "white"]), alpha=0.55,
                  aspect="auto", origin="lower", interpolation="nearest", vmin=0, vmax=1)
    ax.set_yticks(range(len(lengths)))
    ax.set_yticklabels([str(v) for v in lengths], fontsize=7)
    step = 1 if len(start_labels) <= 14 else 2
    ax.set_xticks(range(0, len(start_labels), step))
    ax.set_xticklabels([start_labels[i] for i in range(0, len(start_labels), step)], rotation=90, fontsize=7)
    if 0 < year_split < len(start_labels):
        ax.axvline(year_split - 0.5, color=TEXT, linewidth=1.0)
    if marked:
        for (i, j) in marked:
            ax.plot(j, i, marker="o", markersize=3, color=TEXT, alpha=0.6, linestyle="none")
    if best is not None:
        ax.plot(best[1], best[0], marker="*", markersize=14, color="#eda100", markeredgecolor=TEXT,
                markeredgewidth=0.8, linestyle="none")
    ax.set_xlabel("First month of the window (p = previous year)")
    ax.set_ylabel("Window length (months)")
    sig_txt = f"; pale = not significant (|r| < {crit:.2f}, n = {n})" if np.isfinite(crit) else ""
    ax.set_title((title or "Correlation of climate windows") + sig_txt, fontsize=9)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(colors=TEXT_2, length=0)
    cb = ax.figure.colorbar(im, cax=cax) if cax is not None else ax.figure.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
    cb.set_label("r", color=TEXT_2)
    cb.outline.set_visible(False)
    cb.ax.tick_params(labelsize=7, colors=TEXT_2)
    return im


def chronology_with_depth(fig, chron: Dict[str, pd.Series], depth: Optional[pd.Series], best: Optional[str] = None,
                          title: str = "", smooth: int = 0, ylabel: str = "Index") -> None:
    """Top: chronologies (best highlighted, others grey). Bottom: sample depth (own panel)."""
    fig.clear()
    gs = fig.add_gridspec(4 if depth is not None else 1, 1, hspace=0.08)
    ax = fig.add_subplot(gs[:3, 0] if depth is not None else gs[0, 0])
    others = [k for k in chron if k != best]
    for k in others:
        s = chron[k].dropna()
        ax.plot(s.index, s.values, color=OTHER_LINE, linewidth=0.7, alpha=0.8)
    if best is not None and best in chron:
        s = chron[best].dropna()
        ax.plot(s.index, s.values, color=SERIES[0], linewidth=1.4, label=best)
        if smooth and smooth >= 3:
            sm = s.rolling(smooth, center=True, min_periods=max(3, smooth // 2)).mean()
            ax.plot(sm.index, sm.values, color=SERIES[1], linewidth=2.0, label=f"{best}, {smooth}-yr mean")
    if others:
        ax.plot([], [], color=OTHER_LINE, linewidth=1.2, label=f"other methods ({len(others)})")
    ax.axhline(1.0, color=MUTED, linewidth=0.7)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10)
    ax.legend(fontsize=8, frameon=False, loc="upper left", ncol=3)
    style_axes(ax)
    if depth is not None:
        ax.tick_params(labelbottom=False)
        ax2 = fig.add_subplot(gs[3, 0], sharex=ax)
        d = depth.dropna()
        ax2.fill_between(d.index, d.values, step="mid", color=DEPTH_FILL)
        ax2.plot(d.index, d.values, drawstyle="steps-mid", color=DEPTH_LINE, linewidth=1.0)
        ax2.set_ylabel("Depth")
        ax2.set_xlabel("Year")
        ax2.set_ylim(bottom=0)
        style_axes(ax2, grid_axis="y")
    else:
        ax.set_xlabel("Year")


def eps_rbar_depth(fig, df: pd.DataFrame, title: str = "", eps_threshold: float = 0.85) -> None:
    """EPS & Rbar (top) and sample depth (bottom) - two panels, shared years."""
    fig.clear()
    gs = fig.add_gridspec(3, 1, hspace=0.08)
    ax = fig.add_subplot(gs[:2, 0])
    ax.plot(df.index, df["eps"], color=SERIES[0], linewidth=1.6, marker="o", markersize=3, label="EPS")
    ax.plot(df.index, df["rbar"], color=SERIES[2], linewidth=1.6, marker="o", markersize=3, label="Rbar")
    ax.axhline(eps_threshold, color=TEXT_2, linestyle="--", linewidth=0.9)
    ax.text(df.index.min(), eps_threshold, f" EPS {eps_threshold:g}", va="bottom", fontsize=7, color=TEXT_2)
    eps = df["eps"].dropna()
    ok = eps[eps >= eps_threshold]
    if not ok.empty:
        y0 = ok.index.min()
        ax.axvline(y0, color=SERIES[0], linewidth=0.8, alpha=0.6)
        ax.annotate(f"EPS ≥ {eps_threshold:g} from {int(y0)}", (y0, eps_threshold), xytext=(6, -16),
                    textcoords="offset points", fontsize=8, color=SERIES[0])
    vals = pd.concat([df["eps"], df["rbar"]]).dropna()
    lo = min(0.0, float(vals.min()) - 0.05) if len(vals) else 0.0
    ax.set_ylim(max(-1.0, lo), 1.03)
    ax.set_ylabel("EPS / Rbar")
    ax.legend(fontsize=8, frameon=False, loc="lower right", ncol=2)
    ax.set_title(title, fontsize=10)
    style_axes(ax)
    ax.tick_params(labelbottom=False)
    ax2 = fig.add_subplot(gs[2, 0], sharex=ax)
    ax2.fill_between(df.index, df["depth"], step="mid", color=DEPTH_FILL)
    ax2.plot(df.index, df["depth"], drawstyle="steps-mid", color=DEPTH_LINE, linewidth=1.0)
    ax2.set_ylabel("Depth")
    ax2.set_xlabel("Year")
    ax2.set_ylim(bottom=0)
    style_axes(ax2, grid_axis="y")
