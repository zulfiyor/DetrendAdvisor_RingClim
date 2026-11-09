#!/usr/bin/env python3
"""
dendro_field_qt.py - "Spatial correlation" page for RingClim (PySide6).

The page correlates a chronology with a gridded climate field cell by cell (see dendro_field.py),
shows the map with the site marked, the series of any clicked cell, and a summary of the
correlation results of RingClim with ready suggestions for maps.

The page works with a *host* object (RingClim's main window) that provides::

    host.field_chronologies() -> list of dict(name, series, lat, lon, site)
    host.field_climate_info() -> dict(name -> dendro_field.ClimateInfo)
    host.field_results()      -> pandas.DataFrame or None (the last correlation table)
    host.field_set_location(name, lat, lon) -> None   (remember coordinates typed by the user)
"""
from __future__ import annotations

import os
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QTableWidget, QTableWidgetItem,
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QVBoxLayout, QWidget,
)

import matplotlib
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

import dendro_field as F
import dendro_online as DO

try:
    import dendro_sources_qt as SQ
except Exception:  # pragma: no cover
    SQ = None  # type: ignore

HELP_TEXT = """<h3>Spatial correlation</h3>
<p>The chronology is correlated with a climate window (for example previous July-September) in every grid
cell of a region, as in the KNMI Climate Explorer. The map shows r; cells that are not significant are greyed
(or hidden with <i>Only significant cells</i>); the star marks the site. Click a cell to see its series.</p>
<ul>
<li><b>Results summary</b>: what the correlation analysis of RingClim found (strongest chronology, variable types,
recurring months, top pairs). <b>Suggested maps</b> turn the strongest windows into settings: double-click one.</li>
<li><b>Chronology</b>: coordinates come from ITRDB or Detrend Advisor; for other chronologies type them or tick
<i>Pick the site on the map</i> and click the map.</li>
<li><b>Field</b>: CRU TS, GPCC, scPDSI and SPEIbase are global files that are downloaded once; only the region is
kept in the cache. TerraClimate sends only the region (every 6th cell, about 0.25 deg). ERA5 and ERA5-Land need your
own Copernicus key (API keys...). A local NetCDF file with a monthly lat/lon grid can also be used.</li>
<li><b>Season</b>: first and last month of the window; p = previous year. Mean for temperature-like variables,
sum for precipitation-like variables (Auto chooses).</li>
<li><b>Statistics</b>: Pearson or Spearman; optional removal of linear trends or first differences; cells with fewer
years than <i>Min. years</i> are left empty.</li>
</ul>
<p>About 5% of the cells are significant at p &lt; 0.05 by chance alone; neighbouring cells are not independent,
so judge the size and position of the significant area, not single cells.</p>
"""


class _Worker(QObject):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str, int, int, str], None], Callable[[], bool]], Any],
                 cancel: threading.Event) -> None:
        super().__init__()
        self.fn = fn
        self.cancel = cancel

    def run(self) -> None:
        def prog(stage: str, done: int, total: int, msg: str) -> None:
            self.progress.emit(msg or stage)

        try:
            self.done.emit(self.fn(prog, self.cancel.is_set))
        except DO.CancelledError:
            self.failed.emit("Cancelled.")
        except Exception as exc:  # pragma: no cover - shown to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}")


def _notify(parent, text: str, path: str) -> None:
    try:
        import dendro_ui_help as _uh

        _uh.qt_notify_saved(parent, "Saved", text, path)
    except Exception:
        QMessageBox.information(parent, "Saved", f"{text}\n{path}")


def _dspin(lo: float, hi: float, val: float, dec: int = 3, step: float = 0.5) -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setValue(val)
    w.setKeyboardTracking(False)
    return w


class FieldCorrelationPanel(QWidget):
    """Spatial correlation page (can be a tab of RingClim or a separate window)."""

    def __init__(self, host: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Spatial correlation")
        self.host = host
        self._dock = None
        self._chrons: List[Dict[str, Any]] = []
        self._suggestions: List[F.FieldSuggestion] = []
        self._field: Optional[F.Field] = None
        self._map: Optional[F.CorrMap] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self._cancel = threading.Event()
        self._sources = F.field_sources()
        self._build()
        self.refresh_from_host()
        self.restore_screen()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        left_inner = QWidget()
        lay = QVBoxLayout(left_inner)
        lay.setContentsMargins(4, 4, 4, 4)

        # --- results summary
        g_sum = QGroupBox("Results summary (from the Analysis of RingClim)")
        gs = QVBoxLayout(g_sum)
        self.txt_summary = QPlainTextEdit()
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setMinimumHeight(150)
        gs.addWidget(self.txt_summary)
        gs.addWidget(QLabel("Suggested maps (double-click to use):"))
        self.lst_sugg = QListWidget()
        self.lst_sugg.setMinimumHeight(110)
        self.lst_sugg.itemDoubleClicked.connect(lambda it: self._use_suggestion(it, compute=True))
        gs.addWidget(self.lst_sugg)
        row = QHBoxLayout()
        b_ref = QPushButton("Refresh from results")
        b_ref.setToolTip("Read the chronologies, climate data and the last correlation table of RingClim again.")
        b_ref.clicked.connect(self.refresh_from_host)
        b_use = QPushButton("Use selected suggestion")
        b_use.clicked.connect(lambda: self._use_suggestion(self.lst_sugg.currentItem()))
        row.addWidget(b_ref)
        row.addWidget(b_use)
        gs.addLayout(row)
        lay.addWidget(g_sum)

        # --- chronology
        g_ch = QGroupBox("Chronology and site")
        gc = QGridLayout(g_ch)
        self.cmb_chron = QComboBox()
        self.cmb_chron.currentIndexChanged.connect(self._on_chron_changed)
        self.sp_lat = _dspin(-90, 90, 40.0, 4, 0.1)
        self.sp_lon = _dspin(-180, 180, 70.0, 4, 0.1)
        self.ed_site = QLabel("")
        self.ed_site.setWordWrap(True)
        self.chk_pick = QCheckBox("Pick the site on the map")
        self.chk_pick.setToolTip("Then click the map: the clicked point becomes the site of this chronology.")
        b_save_loc = QPushButton("Remember coordinates")
        b_save_loc.setToolTip("Store these coordinates with the chronology (saved with the project).")
        b_save_loc.clicked.connect(self._remember_location)
        gc.addWidget(QLabel("Chronology:"), 0, 0)
        gc.addWidget(self.cmb_chron, 0, 1, 1, 3)
        gc.addWidget(QLabel("Lat:"), 1, 0)
        gc.addWidget(self.sp_lat, 1, 1)
        gc.addWidget(QLabel("Lon:"), 1, 2)
        gc.addWidget(self.sp_lon, 1, 3)
        gc.addWidget(self.chk_pick, 2, 0, 1, 2)
        gc.addWidget(b_save_loc, 2, 2, 1, 2)
        gc.addWidget(self.ed_site, 3, 0, 1, 4)
        lay.addWidget(g_ch)

        # --- field
        g_f = QGroupBox("Climate field")
        gf = QGridLayout(g_f)
        self.cmb_src = QComboBox()
        for k, s in self._sources.items():
            self.cmb_src.addItem(s.label, k)
        self.cmb_src.currentIndexChanged.connect(self._on_source_changed)
        self.cmb_var = QComboBox()
        self.cmb_var.currentIndexChanged.connect(self._on_var_changed)
        self.ed_file = QLabel("")
        self.btn_file = QPushButton("NetCDF file...")
        self.btn_file.clicked.connect(self._choose_file)
        self.btn_keys = QPushButton("API keys...")
        self.btn_keys.setToolTip("Your own Copernicus key for ERA5 / ERA5-Land.")
        self.btn_keys.clicked.connect(self._api_keys)
        self.lbl_src_note = QLabel("")
        self.lbl_src_note.setWordWrap(True)
        self.lbl_src_note.setStyleSheet("color: #555;")
        gf.addWidget(QLabel("Data set:"), 0, 0)
        gf.addWidget(self.cmb_src, 0, 1, 1, 3)
        gf.addWidget(QLabel("Variable:"), 1, 0)
        gf.addWidget(self.cmb_var, 1, 1, 1, 3)
        gf.addWidget(self.btn_file, 2, 0)
        gf.addWidget(self.ed_file, 2, 1, 1, 2)
        gf.addWidget(self.btn_keys, 2, 3)
        gf.addWidget(self.lbl_src_note, 3, 0, 1, 4)
        lay.addWidget(g_f)

        # --- season
        g_s = QGroupBox("Season (window of months)")
        gss = QGridLayout(g_s)
        self.cmb_m0 = QComboBox()
        self.cmb_m1 = QComboBox()
        for lab in F.WINDOW_LABELS:
            self.cmb_m0.addItem(lab)
            self.cmb_m1.addItem(lab)
        self.cmb_m0.setCurrentIndex(F.WINDOW_LABELS.index("Jun"))
        self.cmb_m1.setCurrentIndex(F.WINDOW_LABELS.index("Aug"))
        self.cmb_agg = QComboBox()
        self.cmb_agg.addItem("Auto (by variable)", "auto")
        self.cmb_agg.addItem("Mean", "mean")
        self.cmb_agg.addItem("Sum", "sum")
        gss.addWidget(QLabel("From:"), 0, 0)
        gss.addWidget(self.cmb_m0, 0, 1)
        gss.addWidget(QLabel("to:"), 0, 2)
        gss.addWidget(self.cmb_m1, 0, 3)
        gss.addWidget(QLabel("Aggregation:"), 1, 0)
        gss.addWidget(self.cmb_agg, 1, 1, 1, 3)
        lay.addWidget(g_s)

        # --- region and period
        g_r = QGroupBox("Region and period")
        gr = QGridLayout(g_r)
        self.sp_s = _dspin(-90, 90, 28.0, 2)
        self.sp_n = _dspin(-90, 90, 52.0, 2)
        self.sp_w = _dspin(-180, 180, 52.0, 2)
        self.sp_e = _dspin(-180, 180, 90.0, 2)
        self.sp_half_lat = _dspin(1, 60, 12.0, 1, 1.0)
        self.sp_half_lon = _dspin(1, 90, 18.0, 1, 1.0)
        b_around = QPushButton("Around the site")
        b_around.setToolTip("Region = site +/- the half-widths.")
        b_around.clicked.connect(self._region_around_site)
        self.sp_y0 = QSpinBox()
        self.sp_y0.setRange(1500, 2100)
        self.sp_y0.setValue(1951)
        self.sp_y1 = QSpinBox()
        self.sp_y1.setRange(1500, 2100)
        self.sp_y1.setValue(2000)
        self.sp_stride = QSpinBox()
        self.sp_stride.setRange(1, 24)
        self.sp_stride.setValue(6)
        self.sp_stride.setToolTip("TerraClimate: read every n-th cell (6 = 0.25 deg). Smaller = more detail, "
                                  "larger download.")
        gr.addWidget(QLabel("Lat:"), 0, 0)
        gr.addWidget(self.sp_s, 0, 1)
        gr.addWidget(QLabel("to"), 0, 2)
        gr.addWidget(self.sp_n, 0, 3)
        gr.addWidget(QLabel("Lon:"), 1, 0)
        gr.addWidget(self.sp_w, 1, 1)
        gr.addWidget(QLabel("to"), 1, 2)
        gr.addWidget(self.sp_e, 1, 3)
        gr.addWidget(QLabel("+/- lat:"), 2, 0)
        gr.addWidget(self.sp_half_lat, 2, 1)
        gr.addWidget(QLabel("+/- lon:"), 2, 2)
        gr.addWidget(self.sp_half_lon, 2, 3)
        gr.addWidget(b_around, 3, 0, 1, 4)
        gr.addWidget(QLabel("Years:"), 4, 0)
        gr.addWidget(self.sp_y0, 4, 1)
        gr.addWidget(QLabel("to"), 4, 2)
        gr.addWidget(self.sp_y1, 4, 3)
        gr.addWidget(QLabel("TerraClimate cell step:"), 5, 0, 1, 2)
        gr.addWidget(self.sp_stride, 5, 2, 1, 2)
        lay.addWidget(g_r)

        # --- statistics
        g_st = QGroupBox("Statistics and display")
        fs = QFormLayout(g_st)
        self.cmb_method = QComboBox()
        self.cmb_method.addItem("Pearson", "pearson")
        self.cmb_method.addItem("Spearman", "spearman")
        self.cmb_prep = QComboBox()
        self.cmb_prep.addItem("None", "none")
        self.cmb_prep.addItem("Remove linear trends", "detrend")
        self.cmb_prep.addItem("First differences", "diff")
        self.cmb_alpha = QComboBox()
        for a in (0.05, 0.1, 0.01):
            self.cmb_alpha.addItem(f"p < {a:g}", a)
        self.sp_min_n = QSpinBox()
        self.sp_min_n.setRange(5, 500)
        self.sp_min_n.setValue(20)
        self.chk_only_sig = QCheckBox("Only significant cells")
        self.chk_only_sig.toggled.connect(self._redraw)
        self.cmb_alpha.currentIndexChanged.connect(self._redraw)
        fs.addRow("Correlation:", self.cmb_method)
        fs.addRow("Before correlating:", self.cmb_prep)
        fs.addRow("Significance:", self.cmb_alpha)
        fs.addRow("Min. years:", self.sp_min_n)
        fs.addRow("", self.chk_only_sig)
        self.cmb_style = QComboBox()
        self.cmb_style.addItem("Smooth (interpolated, outline p < alpha)", "smooth")
        self.cmb_style.addItem("Grid cells", "cells")
        self.cmb_style.setToolTip("Smooth: the cell values are interpolated for display and the significant area is "
                                  "outlined. Grid cells: every cell as it is in the data. Hover and click always use "
                                  "the original cells.")
        self.cmb_style.currentIndexChanged.connect(self._redraw)
        fs.addRow("Map style:", self.cmb_style)
        lay.addWidget(g_st)

        # --- screening of all windows for this region
        g_sc = QGroupBox("Screen all windows for this region")
        gsc = QVBoxLayout(g_sc)
        gsc.addWidget(QLabel("Variables of the data set above (each is downloaded once, then cached):"))
        self.lst_sc_vars = QListWidget()
        self.lst_sc_vars.setMaximumHeight(110)
        gsc.addWidget(self.lst_sc_vars)
        fsc = QFormLayout()
        self.cmb_sc_len = QComboBox()
        self.cmb_sc_len.addItem("1-12 months", 12)
        self.cmb_sc_len.addItem("1-24 months", 24)
        fsc.addRow("Window lengths:", self.cmb_sc_len)
        self.sp_sc_perm = QSpinBox()
        self.sp_sc_perm.setRange(0, 1000)
        self.sp_sc_perm.setValue(50)
        self.sp_sc_perm.setToolTip("Chance control: the chronology years are shuffled and all windows and variables "
                                   "are screened again; 'p (shuffled)' = share of shuffles with an equally large "
                                   "significant area around the site (0 = off)")
        fsc.addRow("Shuffles (chance control):", self.sp_sc_perm)
        self.sp_sc_radius = QSpinBox()
        self.sp_sc_radius.setRange(50, 3000)
        self.sp_sc_radius.setValue(500)
        self.sp_sc_radius.setSuffix(" km")
        fsc.addRow("Mean r within:", self.sp_sc_radius)
        self.chk_sc_eff = QCheckBox("Account for serial correlation (effective n, as KNMI)")
        self.chk_sc_eff.setChecked(True)
        fsc.addRow(self.chk_sc_eff)
        gsc.addLayout(fsc)
        self.btn_screen = QPushButton("Screen windows")
        self.btn_screen.setToolTip("Correlate the chronology with every window of months of every ticked variable in "
                                   "the region and rank them by the significant area connected with the site.")
        self.btn_screen.clicked.connect(self.screen_windows)
        gsc.addWidget(self.btn_screen)
        self.tbl_sc = QTableWidget(0, 0)
        self.tbl_sc.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_sc.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sc.setMinimumHeight(200)
        self.tbl_sc.setToolTip("Double-click a row to draw its map")
        self.tbl_sc.itemDoubleClicked.connect(lambda it: self._use_screen_row(it.row()))
        gsc.addWidget(self.tbl_sc)
        lay.addWidget(g_sc)

        # --- actions
        act = QHBoxLayout()
        self.btn_run = QPushButton("Compute map")
        self.btn_run.setProperty("primary", True)
        self.btn_run.setMinimumHeight(34)
        self.btn_run.clicked.connect(self.compute)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel.set)
        act.addWidget(self.btn_run, 1)
        act.addWidget(self.btn_cancel)
        lay.addLayout(act)
        act2 = QHBoxLayout()
        b_png = QPushButton("Save map...")
        b_png.clicked.connect(self._save_png)
        b_csv = QPushButton("Export grid (CSV)...")
        b_csv.clicked.connect(self._save_csv)
        b_help = QPushButton("Help")
        b_help.clicked.connect(lambda: QMessageBox.information(self, "Spatial correlation", HELP_TEXT))
        act2.addWidget(b_png)
        act2.addWidget(b_csv)
        act2.addWidget(b_help)
        lay.addLayout(act2)
        self._dock_row = act2
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_inner)
        scroll.setMinimumWidth(500)
        split.addWidget(scroll)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        vsplit = QSplitter(Qt.Vertical)
        mapw = QWidget()
        ml = QVBoxLayout(mapw)
        ml.setContentsMargins(0, 0, 0, 0)
        self.fig = Figure(figsize=(8, 6))
        self.canvas = FigureCanvas(self.fig)
        ml.addWidget(NavigationToolbar(self.canvas, mapw))
        ml.addWidget(self.canvas, 1)
        vsplit.addWidget(mapw)
        cellw = QWidget()
        cl = QVBoxLayout(cellw)
        cl.setContentsMargins(0, 0, 0, 0)
        self.fig_cell = Figure(figsize=(8, 2.4))
        self.canvas_cell = FigureCanvas(self.fig_cell)
        cl.addWidget(self.canvas_cell, 1)
        cellw.setMinimumHeight(190)
        vsplit.addWidget(cellw)
        vsplit.setChildrenCollapsible(False)
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([620, 230])
        rl.addWidget(vsplit, 1)
        st = QHBoxLayout()
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setMaximumWidth(220)
        st.addWidget(self.lbl_status, 1)
        st.addWidget(self.progress)
        rl.addLayout(st)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)

        try:
            import dendro_ui_help as _uh

            _uh.mpl_wheel_zoom(self.canvas)
            _uh.mpl_wheel_zoom(self.canvas_cell)
        except Exception:
            pass
        self.canvas.mpl_connect("button_press_event", self._on_map_click)
        self.canvas.mpl_connect("motion_notify_event", self._on_map_hover)
        self._annot = None
        self._on_source_changed()
        self._draw_empty()

    # ------------------------------------------------------------------ docking
    def set_dock(self, dock) -> None:
        self._dock = dock
        self._dock_row.addWidget(dock.make_button(self))
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close_requested_from_tab)
        self._dock_row.addWidget(b_close)

    def close_requested_from_tab(self) -> None:
        if self._thread is not None:
            self._cancel.set()
        dock = self._dock
        self._dock = None
        if dock is not None:
            dock.close()
        self.hide()

    # ------------------------------------------------------------------ host data
    def refresh_from_host(self) -> None:
        try:
            self._chrons = list(self.host.field_chronologies() or [])
        except Exception:
            self._chrons = []
        cur = self.cmb_chron.currentText()
        self.cmb_chron.blockSignals(True)
        self.cmb_chron.clear()
        for c in self._chrons:
            self.cmb_chron.addItem(c["name"])
        idx = self.cmb_chron.findText(cur)
        self.cmb_chron.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_chron.blockSignals(False)
        self._on_chron_changed()
        try:
            df = self.host.field_results()
            clim = self.host.field_climate_info() or {}
        except Exception:
            df, clim = None, {}
        if df is not None and not getattr(df, "empty", True):
            summ = F.summarize_results(df, clim, min_n=int(self.sp_min_n.value()))
            self.txt_summary.setPlainText(summ["text"])
            self._suggestions = F.suggest_fields(df, clim, min_n=int(self.sp_min_n.value()))
        else:
            self.txt_summary.setPlainText(
                "No correlation results yet. Run the analysis in the Analysis tab; the summary and suggested maps "
                "appear here. You can also set a map by hand below.")
            self._suggestions = []
        self.lst_sugg.clear()
        for s in self._suggestions:
            it = QListWidgetItem(s.label())
            it.setToolTip(f"{s.chronology}\n{s.reason}")
            self.lst_sugg.addItem(it)
        # fill the settings from the strongest result, unless the user already set a map by hand
        if self._suggestions and not getattr(self, "_manual_settings", False):
            c = self._current_chron()
            mine = [k for k, sg in enumerate(self._suggestions) if c is not None and sg.chronology == c["name"]]
            k = mine[0] if (mine and cur) else 0
            self.lst_sugg.setCurrentRow(k)
            self._use_suggestion(self.lst_sugg.item(k))

    def _current_chron(self) -> Optional[Dict[str, Any]]:
        i = self.cmb_chron.currentIndex()
        return self._chrons[i] if 0 <= i < len(self._chrons) else None

    def _on_chron_changed(self, *_a: Any) -> None:
        self._chron_changed_basic()
        if getattr(self, "_applying", False):
            return
        c = self._current_chron()
        if c is None:
            return
        # the strongest suggestion of this chronology (season, variable, years)
        for k, sg in enumerate(self._suggestions):
            if sg.chronology == c["name"]:
                self.lst_sugg.setCurrentRow(k)
                self._use_suggestion(self.lst_sugg.item(k))
                break

    def _analysis_period(self):
        try:
            return self.host.field_period()
        except Exception:
            return None

    def _chron_changed_basic(self) -> None:
        c = self._current_chron()
        if c is None:
            self.ed_site.setText("No chronology loaded.")
            return
        s = c["series"].dropna()
        if c.get("lat") is not None and c.get("lon") is not None:
            self.sp_lat.setValue(float(c["lat"]))
            self.sp_lon.setValue(float(c["lon"]))
            self.ed_site.setText(f"{c.get('site') or c['name']}: {s.index.min()}-{s.index.max()}, coordinates from "
                                 f"the data source.")
        else:
            self.ed_site.setText(f"{s.index.min()}-{s.index.max()}. <b>Coordinates unknown</b>: type them or tick "
                                 "'Pick the site on the map' and click the map, then 'Remember coordinates'.")
            self.ed_site.setTextFormat(Qt.RichText)
        self.sp_y0.setValue(max(int(s.index.min()) + 1, 1901))
        self.sp_y1.setValue(int(s.index.max()))

    def _remember_location(self) -> None:
        c = self._current_chron()
        if c is None:
            return
        c["lat"], c["lon"] = float(self.sp_lat.value()), float(self.sp_lon.value())
        try:
            self.host.field_set_location(c["name"], c["lat"], c["lon"])
            self.lbl_status.setText(f"Coordinates stored with {c['name']}.")
        except Exception as exc:
            self.lbl_status.setText(f"Could not store the coordinates: {exc}")

    # ------------------------------------------------------------------ field selection
    def _on_source_changed(self, *_a: Any) -> None:
        key = self.cmb_src.currentData()
        src = self._sources.get(key)
        self.cmb_var.blockSignals(True)
        self.cmb_var.clear()
        if src is not None:
            for v in src.variables:
                self.cmb_var.addItem(f"{v.code} - {v.label}", v.code)
        self.cmb_var.blockSignals(False)
        is_file = key == "netcdf"
        self.btn_file.setVisible(is_file)
        self.ed_file.setVisible(is_file)
        self.btn_keys.setVisible(bool(src and src.needs_key))
        self.sp_stride.setEnabled(key == "terraclimate")
        note = src.note if src else ""
        if src and src.needs_key:
            note = "Needs your own free Copernicus key; requests wait in the Copernicus queue."
        if key in ("cru", "gpcc"):
            note = "Global files are downloaded once per decade (CRU) / period (GPCC); only the region is kept."
        if key in ("scpdsi", "speibase"):
            note = "One large global file is downloaded once; only the region is kept."
        self.lbl_src_note.setText(note)
        if key == "netcdf":
            self.cmb_var.setEditable(True)
        else:
            self.cmb_var.setEditable(False)
        self._on_var_changed()
        if hasattr(self, "lst_sc_vars"):
            self.lst_sc_vars.clear()
            default = {"cru": {"tmp", "tmn", "tmx", "pre", "pet", "vap"}}.get(key)
            if src is not None:
                for k, v in enumerate(src.variables):
                    it = QListWidgetItem(f"{v.code} - {v.label}")
                    it.setData(Qt.UserRole, v.code)
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    on = (v.code in default) if default else k < 4
                    it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                    self.lst_sc_vars.addItem(it)

    def _on_var_changed(self, *_a: Any) -> None:
        pass

    def _choose_file(self) -> None:
        p, _f = QFileDialog.getOpenFileName(self, "NetCDF file", "", "NetCDF (*.nc *.nc4 *.cdf);;All files (*)")
        if p:
            self.ed_file.setText(p)

    def _api_keys(self) -> None:
        if SQ is not None and hasattr(SQ, "ApiKeysDialog"):
            try:
                SQ.ApiKeysDialog(self).exec()
            except Exception as exc:
                QMessageBox.warning(self, "API keys", str(exc))

    def _region_around_site(self) -> None:
        b = F.default_region(self.sp_lat.value(), self.sp_lon.value(), self.sp_half_lat.value(), self.sp_half_lon.value())
        self.sp_s.setValue(b[0])
        self.sp_n.setValue(b[1])
        self.sp_w.setValue(b[2])
        self.sp_e.setValue(b[3])

    def _use_suggestion(self, item: Optional[QListWidgetItem], compute: bool = False) -> None:
        if item is None:
            return
        i = self.lst_sugg.row(item)
        if not (0 <= i < len(self._suggestions)):
            return
        s = self._suggestions[i]
        self._applying = True
        try:
            k = self.cmb_chron.findText(s.chronology)
            if k >= 0 and k != self.cmb_chron.currentIndex():
                self.cmb_chron.setCurrentIndex(k)
        finally:
            self._applying = False
        k = self.cmb_src.findData(s.source)
        if k >= 0:
            self.cmb_src.setCurrentIndex(k)
        k = self.cmb_var.findData(s.var)
        if k >= 0:
            self.cmb_var.setCurrentIndex(k)
        self.cmb_m0.setCurrentIndex(s.window[0])
        self.cmb_m1.setCurrentIndex(s.window[1])
        k = self.cmb_agg.findData(s.agg)
        self.cmb_agg.setCurrentIndex(k if k >= 0 else 0)
        self._region_around_site()
        src = self._sources.get(s.source)
        c = self._current_chron()
        missing = c is not None and (c.get("lat") is None or c.get("lon") is None)
        c0 = c["series"].dropna() if c is not None else None
        # years: chronology overlap with the field, limited by the period of the RingClim analysis
        y0 = int(c0.index.min()) + 1 if c0 is not None and len(c0) else self.sp_y0.value()
        y1 = int(c0.index.max()) if c0 is not None and len(c0) else self.sp_y1.value()
        if src is not None:
            y0 = max(y0, src.first_year + 1)
        per = self._analysis_period()
        if per:
            y0, y1 = max(y0, int(per[0])), min(y1, int(per[1]))
        ci = None
        try:
            ci = (self.host.field_climate_info() or {}).get(s.climate)
        except Exception:
            pass
        if ci is not None and s.same_variable:
            if ci.first_year:
                y0 = max(y0, int(ci.first_year) + 1)
            if ci.last_year:
                y1 = min(y1, int(ci.last_year))
        if y1 > y0:
            self.sp_y0.setValue(y0)
            self.sp_y1.setValue(y1)
        self._manual_settings = False
        self.lbl_status.setText(f"Settings from: {s.reason}" + (
            "  -  enter the coordinates of the chronology first." if missing else "  -  press 'Compute map'."))
        if compute and not missing:
            self.compute()

    # ------------------------------------------------------------------ compute
    def _settings(self) -> Dict[str, Any]:
        c = self._current_chron()
        if c is None:
            raise ValueError("No chronology loaded.")
        src = self.cmb_src.currentData()
        var = self.cmb_var.currentData() if not self.cmb_var.isEditable() else (
            self.cmb_var.currentData() if self.cmb_var.currentText().split(" - ")[0] == "auto" else
            self.cmb_var.currentText().split(" - ")[0].strip())
        si, ei = self.cmb_m0.currentIndex(), self.cmb_m1.currentIndex()
        if ei < si:
            raise ValueError("The last month of the window is before the first one.")
        agg = self.cmb_agg.currentData()
        if agg == "auto":
            try:
                agg = self._sources[src].var(var).agg
            except Exception:
                agg = "mean"
        bbox = (min(self.sp_s.value(), self.sp_n.value()), max(self.sp_s.value(), self.sp_n.value()),
                min(self.sp_w.value(), self.sp_e.value()), max(self.sp_w.value(), self.sp_e.value()))
        y0, y1 = int(self.sp_y0.value()), int(self.sp_y1.value())
        if y1 - y0 + 1 < self.sp_min_n.value():
            raise ValueError("The period is shorter than 'Min. years'.")
        area = (bbox[1] - bbox[0]) * (bbox[3] - bbox[2])
        return {"chron": c, "source": src, "var": var or "auto", "window": (si, ei), "agg": agg, "bbox": bbox,
                "y0": y0, "y1": y1, "method": self.cmb_method.currentData(), "prep": self.cmb_prep.currentData(),
                "min_n": int(self.sp_min_n.value()), "alpha": float(self.cmb_alpha.currentData()),
                "site": (float(self.sp_lat.value()), float(self.sp_lon.value()), str(c.get("site") or c["name"])[:18]),
                "options": {"path": self.ed_file.text().strip(), "terraclimate_stride": int(self.sp_stride.value())},
                "area": area}

    def compute(self) -> None:
        self._manual_settings = True
        if self._thread is not None:
            return
        try:
            st = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Spatial correlation", str(exc))
            return
        c = st["chron"]
        if c.get("lat") is None or c.get("lon") is None:
            r = QMessageBox.question(
                self, "Spatial correlation",
                f"The coordinates of '{c['name']}' are not known.\n\nUse {st['site'][0]:.3f} N, {st['site'][1]:.3f} E "
                "(the values in the Lat/Lon fields) for the site marker?")
            if r != QMessageBox.Yes:
                return
        if st["area"] > 2500 and st["source"] in ("terraclimate", "era5land_cds_monthly"):
            if QMessageBox.question(self, "Spatial correlation",
                                    "The region is large for this high-resolution data set (long download).\n"
                                    "Continue?") != QMessageBox.Yes:
                return
        self._cancel.clear()
        chron = c["series"]

        def task(progress, cancel):
            fld = F.load_field(st["source"], st["var"], st["bbox"], st["y0"] - 1, st["y1"], progress, cancel,
                               st["options"])
            progress("correlate", 0, 0, "Correlating ...")
            cm = F.correlate_field(chron, fld, st["window"], st["agg"], st["y0"], st["y1"], st["method"],
                                   st["prep"], st["min_n"], st["alpha"], site=st["site"], chron_name=c["name"])
            return fld, cm

        self._start(task)

    # ------------------------------------------------------------------ screening
    SCREEN_COLS = ["Field", "Variable", "Window", "Period", "Area around site (km2)", "% same sign",
                   "% of region significant", "r at site", "p at site", "Mean r within", "Max |r|", "Max at", "N",
                   "p (shuffled)", "Chance best area (95%, km2)"]

    def screen_windows(self) -> None:
        if self._thread is not None:
            return
        try:
            st = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Screening", str(exc))
            return
        vars_ = [self.lst_sc_vars.item(i).data(Qt.UserRole) for i in range(self.lst_sc_vars.count())
                 if self.lst_sc_vars.item(i).checkState() == Qt.Checked]
        if not vars_:
            QMessageBox.information(self, "Screening", "Tick at least one variable.")
            return
        c = st["chron"]
        if c.get("lat") is None or c.get("lon") is None:
            if QMessageBox.question(self, "Screening", f"The coordinates of '{c['name']}' are not known. Use "
                                    f"{st['site'][0]:.3f} N, {st['site'][1]:.3f} E?") != QMessageBox.Yes:
                return
        max_len = int(self.cmb_sc_len.currentData())
        n_perm = int(self.sp_sc_perm.value())
        radius = float(self.sp_sc_radius.value())
        eff = self.chk_sc_eff.isChecked()
        self._manual_settings = True
        self._cancel.clear()
        chron = c["series"]
        label = self._sources[st["source"]].label if st["source"] in self._sources else st["source"]

        def task(progress, cancel):
            parts, nulls = [], []
            for k, v in enumerate(vars_, start=1):
                progress("var", k, len(vars_), f"[{k}/{len(vars_)}] {v}: loading the field ...")
                fld = F.load_field(st["source"], v, st["bbox"], st["y0"] - 1, st["y1"], progress, cancel,
                                   st["options"])

                def pg(stage, d, t, msg, v=v, k=k):
                    progress(stage, d, t, f"[{k}/{len(vars_)}] {v}: {msg}")

                df, null = F.screen_field_windows(chron, fld, (st["site"][0], st["site"][1]), st["y0"], st["y1"],
                                                  max_len, st["method"], st["prep"], st["alpha"], st["min_n"],
                                                  radius, n_perm, eff, progress=pg, cancel=cancel)
                parts.append((label, v, df))
                nulls.append(null)
            return ("screen", F.finish_screening(parts, nulls), radius)

        self._start(task)

    def _show_screen(self, df: pd.DataFrame, radius: float) -> None:
        self._screen_df = df
        try:
            self.host.field_screen_df = df  # saved with the project / session
        except Exception:
            pass
        d = df.rename(columns={f"Mean r within {int(radius)} km": "Mean r within"})
        cols = [c for c in self.SCREEN_COLS if c in d.columns]
        self.tbl_sc.clear()
        self.tbl_sc.setRowCount(len(d))
        self.tbl_sc.setColumnCount(len(cols))
        self.tbl_sc.setHorizontalHeaderLabels([("Mean r within %d km" % radius) if c == "Mean r within" else c
                                               for c in cols])
        for i in range(len(d)):
            for j, ccol in enumerate(cols):
                v = d.iloc[i][ccol]
                if isinstance(v, (float, np.floating)):
                    txt = f"{v:,.0f}" if "km2" in ccol else (f"{v:.3f}" if ccol.startswith("p") else f"{v:.2f}")
                else:
                    txt = str(v)
                self.tbl_sc.setItem(i, j, QTableWidgetItem(txt))
        self.tbl_sc.resizeColumnsToContents()
        lines = []
        for i in range(min(5, len(d))):
            r = d.iloc[i]
            pp = r.get("p (shuffled)", float("nan"))
            lines.append(f"{i + 1}. {r['Variable']} {r['Window']} ({r['Period']}): r at site {r['r at site']:+.2f}, "
                         f"{r['Area around site (km2)']:,.0f} km2 significant around the site"
                         + ("" if not np.isfinite(pp) else f", p (shuffled) = {pp:.3f}"))
        self.lbl_status.setText("Strongest regional signals: " + " | ".join(lines[:3]) if lines else
                                "No window with data at the site.")
        if lines:
            self.txt_summary.setPlainText("Screening of all windows for this region (top 5):\n" + "\n".join(lines)
                                          + "\n\nRanked by the significant area (same sign as at the site) that is "
                                            "connected with the site. 'p (shuffled)' below 0.05: such an area is "
                                            "unlikely to appear by chance in this screening.\n\n"
                                          + self.txt_summary.toPlainText())

    def restore_screen(self) -> None:
        df = getattr(self.host, "field_screen_df", None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            rad = 500.0
            for ccol in df.columns:
                if str(ccol).startswith("Mean r within"):
                    try:
                        rad = float(str(ccol).split()[3])
                    except Exception:
                        pass
            self._show_screen(df, rad)

    def _use_screen_row(self, row: int) -> None:
        df = getattr(self, "_screen_df", None)
        if df is None or not (0 <= row < len(df)):
            return
        r = df.iloc[row]
        k = self.cmb_var.findData(r["Variable"])
        if k >= 0:
            self.cmb_var.setCurrentIndex(k)
        self.cmb_m0.setCurrentIndex(int(r["si"]))
        self.cmb_m1.setCurrentIndex(int(r["ei"]))
        try:
            a, b = (int(x) for x in str(r["Period"]).split("-"))
            self.sp_y0.setValue(a)
            self.sp_y1.setValue(b)
        except Exception:
            pass
        self.compute()

    def _start(self, task) -> None:
        self.btn_run.setEnabled(False)
        self.btn_screen.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setRange(0, 0)
        self.lbl_status.setText("Working ...")
        th = QThread(self)
        wk = _Worker(task, self._cancel)
        wk.moveToThread(th)
        th.started.connect(wk.run)
        wk.progress.connect(self.lbl_status.setText)
        wk.done.connect(self._on_done)
        wk.failed.connect(self._on_failed)
        wk.done.connect(th.quit)
        wk.failed.connect(th.quit)
        th.finished.connect(self._on_thread_finished)
        self._thread, self._worker = th, wk
        th.start()

    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = self._worker = None
        self.btn_run.setEnabled(True)
        self.btn_screen.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setRange(0, 1)

    def _on_failed(self, msg: str) -> None:
        self.lbl_status.setText(msg.splitlines()[0] if msg else "Failed.")
        if msg != "Cancelled.":
            QMessageBox.warning(self, "Spatial correlation", msg)

    def _on_done(self, result) -> None:
        if isinstance(result, tuple) and len(result) == 3 and result[0] == "screen":
            self._show_screen(result[1], result[2])
            return
        self._field, self._map = result
        st = self._map.stats
        msg = (f"Done: {st.get('cells', 0)} cells; significant area {st.get('area_significant_pct', 0)}% "
               f"(about {st.get('by_chance_pct', 5)}% expected by chance)")
        if st.get("max_r"):
            msg += f"; max r {st['max_r'][0]:+.2f} at {st['max_r'][1]:.2f} N {st['max_r'][2]:.2f} E"
        if st.get("min_r"):
            msg += f"; min r {st['min_r'][0]:+.2f}"
        self.lbl_status.setText(msg)
        self._redraw()
        if self._map.site is not None:
            self._plot_cell(self._map.site[0], self._map.site[1])

    # ------------------------------------------------------------------ drawing
    def _draw_empty(self) -> None:
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        ax.text(0.5, 0.5, "Choose a chronology, a field and a season, then 'Compute map'.\n"
                          "Suggested maps from the correlation results are listed on the left.",
                ha="center", va="center", transform=ax.transAxes, color="#555")
        ax.set_axis_off()
        self.canvas.draw_idle()
        self.fig_cell.clear()
        self.canvas_cell.draw_idle()

    def _redraw(self, *_a: Any) -> None:
        if self._map is None:
            return
        alpha = float(self.cmb_alpha.currentData())
        self._map.stats = F.map_statistics(self._map, alpha)
        self._ax = F.draw_corr_map(self.fig, self._map, alpha=alpha, only_significant=self.chk_only_sig.isChecked(),
                                   style=self.cmb_style.currentData() or "smooth")
        self._annot = None
        try:
            self.fig.tight_layout()
        except Exception:
            pass
        self.canvas.draw_idle()

    def _on_map_hover(self, event) -> None:
        cm = self._map
        if cm is None or event.inaxes is None or event.inaxes is not getattr(self, "_ax", None):
            return
        if event.xdata is None:
            return
        i = int(np.argmin(np.abs(cm.lats - event.ydata)))
        j = int(np.argmin(np.abs(cm.lons - event.xdata)))
        r, p, n = cm.r[i, j], cm.p[i, j], cm.n[i, j]
        txt = (f"{cm.lats[i]:.2f} N, {cm.lons[j]:.2f} E\nr = {r:+.3f}, p = {p:.3g}, n = {int(n)}"
               if np.isfinite(r) else f"{cm.lats[i]:.2f} N, {cm.lons[j]:.2f} E\nno value")
        if self._annot is None:
            self._annot = event.inaxes.annotate("", xy=(0, 0), xytext=(12, 12), textcoords="offset points",
                                                bbox=dict(boxstyle="round", fc="#fffbe6", ec="#999", alpha=0.95),
                                                fontsize=8, zorder=30)
        self._annot.xy = (event.xdata, event.ydata)
        self._annot.set_text(txt)
        self._annot.set_visible(True)
        self.canvas.draw_idle()

    def _on_map_click(self, event) -> None:
        if event.inaxes is None or event.xdata is None or event.button != 1:
            return
        if self.canvas.toolbar is not None and getattr(self.canvas.toolbar, "mode", ""):
            return  # zoom / pan active
        if self.chk_pick.isChecked():
            self.sp_lat.setValue(float(event.ydata))
            self.sp_lon.setValue(float(event.xdata))
            self.chk_pick.setChecked(False)
            self.lbl_status.setText(f"Site set to {event.ydata:.3f} N, {event.xdata:.3f} E - 'Remember coordinates' "
                                    "stores it with the chronology.")
            if self._map is not None:
                self._map.site = (float(event.ydata), float(event.xdata), self._map.site[2] if self._map.site else "site")
                self._redraw()
            return
        if self._map is not None and event.inaxes is getattr(self, "_ax", None):
            self._plot_cell(float(event.ydata), float(event.xdata))

    def _plot_cell(self, lat: float, lon: float) -> None:
        cm, fld = self._map, self._field
        c = self._current_chron()
        if cm is None or fld is None or c is None:
            return
        years, vals, la, lo = F.cell_series(cm, fld, lat, lon)
        chron = pd.to_numeric(c["series"], errors="coerce").reindex(years)
        self.fig_cell.clear()
        ax = self.fig_cell.add_subplot(111)
        ok = np.isfinite(vals) & np.isfinite(chron.to_numpy())
        if ok.sum() < 3:
            ax.text(0.5, 0.5, "No data in this cell", ha="center", va="center", transform=ax.transAxes)
            self.canvas_cell.draw_idle()
            return

        def z(a):
            a = np.asarray(a, float)
            return (a - np.nanmean(a)) / (np.nanstd(a) or 1.0)

        r = float(np.corrcoef(np.asarray(vals)[ok], chron.to_numpy()[ok])[0, 1])
        ax.plot(years, z(chron.to_numpy()), color="tab:blue", lw=1.3, label="chronology")
        ax.plot(years, z(vals), color="tab:red", lw=1.1, label=f"{cm.field_label} {F.window_label(*cm.window)}")
        ax.axhline(0, color="k", lw=0.5)
        ax.set_title(f"Cell {la:.2f} N, {lo:.2f} E: r = {r:+.2f} (n = {int(ok.sum())}), standardised series",
                     fontsize=9)
        ax.legend(fontsize=7, loc="upper left", frameon=False, ncol=2)
        ax.grid(True, lw=0.3)
        try:
            self.fig_cell.tight_layout()
        except Exception:
            pass
        self.canvas_cell.draw_idle()

    # ------------------------------------------------------------------ export
    def _save_png(self) -> None:
        if self._map is None:
            return
        p, _f = QFileDialog.getSaveFileName(self, "Save map", "field_correlation.png",
                                            "PNG (*.png);;PDF (*.pdf);;SVG (*.svg)")
        if p:
            self.fig.savefig(p, dpi=300, bbox_inches="tight")
            self.lbl_status.setText(f"Saved {p}")
            _notify(self, "Map saved.", p)

    def _save_csv(self) -> None:
        if self._map is None:
            return
        p, _f = QFileDialog.getSaveFileName(self, "Export grid", "field_correlation.csv", "CSV (*.csv)")
        if not p:
            return
        df = self._map.to_frame()
        df.to_csv(p, index=False)
        meta = {"chronology": self._map.chron_name, "field": self._map.field_label, "source": self._map.source,
                "variable": self._map.var, "window": F.window_label(*self._map.window), "agg": self._map.agg,
                "years": [int(self._map.years.min()), int(self._map.years.max())], "method": self._map.method,
                "prep": self._map.prep, "stats": self._map.stats, "citation": self._map.citation}
        import json

        with open(os.path.splitext(p)[0] + "_meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1, default=str)
        self.lbl_status.setText(f"Saved {p}")
        _notify(self, "Correlation grid saved (CSV + _meta.json).", p)
