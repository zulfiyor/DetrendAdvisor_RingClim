#!/usr/bin/env python3
"""
dendro_moving_qt.py - "Moving correlation" page for RingClim (PySide6): temporal stability of the
climate signal with running (moving-window) correlations, see dendro_moving.py.

The page works with a *host* object (RingClim's main window) that provides::

    host.moving_chronologies() -> list of dict(name, dataset, column, series, lat, lon)
    host.moving_climates()     -> dict(name -> dict(monthly=DataFrame(year x 1..12) | None,
                                                   annual=Series | None, param=str))
    host.field_results()       -> pandas.DataFrame or None (the last correlation table)
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

import dendro_moving as M

HELP_TEXT = """<h3>Moving correlation (temporal stability)</h3>
<p>The chronology is correlated with one climate season in a window of <i>w</i> years that moves one year at a
time; each value is drawn at the centre year of its window (as TreeClim <i>dcc(moving = TRUE)</i> and the
"centred 25-year running correlations" of many papers). Dashed lines: p &lt; 0.01, dotted lines: p &lt; 0.05 for
<i>n</i> = <i>w</i> years.</p>
<ul>
<li><b>Chronologies</b>: tick the series. <i>Panels</i>: one panel per data set (lines = STD/RES/ARS or TRW/BI
columns, like the figures of site networks), one per column, or everything in one panel.</li>
<li><b>Climate</b>: data set and season (p = previous year); mean for temperature-like, sum for precipitation-like
variables (Auto). <i>Suggest from results</i> takes the strongest season of the analysis; <i>Best season of each
chronology</i> uses the strongest window of every chronology separately.</li>
<li><b>Preparation</b>: removing the linear trend of the climate (common, e.g. Ols et al. 2023) keeps a common
warming trend from inflating r; differences or detrending inside each window are stricter.</li>
<li><b>Stability</b>: the table gives the share of significant windows, min/max, the r of the two halves of the period
with Fisher's z test (independent samples: the formal test of a change), and the year from which the correlation
changes most. Running windows overlap, so a wavy curve alone is not proof of a change.</li>
<li>Bootstrap: 95% percentile confidence band per window (resampling its years), as in TreeClim.</li>
</ul>
"""


def _notify(parent, text: str, path: str) -> None:
    try:
        import dendro_ui_help as _uh

        _uh.qt_notify_saved(parent, "Saved", text, path)
    except Exception:
        QMessageBox.information(parent, "Saved", f"{text}\n{path}")


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return ""
    if isinstance(v, (float, np.floating)):
        if not np.isfinite(v):
            return ""
        if float(v).is_integer() and abs(v) > 100:
            return str(int(v))
        return f"{v:.{nd}f}"
    return str(v)


class MovingCorrelationPanel(QWidget):
    """Moving correlation page (a tab of RingClim or a separate window)."""

    def __init__(self, host: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Moving correlation")
        self.host = host
        self._dock = None
        self._chrons: List[Dict[str, Any]] = []
        self._climates: Dict[str, Dict[str, Any]] = {}
        self._results: List[M.MovingResult] = []
        self._opt = M.MovingOptions()
        self._hover: Dict[Any, List[Tuple[Any, M.MovingResult]]] = {}
        self._annot = None
        self._build()
        self.refresh_from_host()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        left_inner = QWidget()
        lay = QVBoxLayout(left_inner)
        lay.setContentsMargins(4, 4, 4, 4)

        g_ch = QGroupBox("Chronologies")
        gc = QVBoxLayout(g_ch)
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("Filter (e.g. RES, a site code) ...")
        self.ed_filter.textChanged.connect(self._apply_filter)
        gc.addWidget(self.ed_filter)
        self.lst_chron = QListWidget()
        self.lst_chron.setMinimumHeight(150)
        gc.addWidget(self.lst_chron)
        row = QHBoxLayout()
        for text, fn in (("All", lambda: self._check_all(True)), ("None", lambda: self._check_all(False)),
                         ("Refresh", self.refresh_from_host)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        gc.addLayout(row)
        f = QFormLayout()
        self.cmb_group = QComboBox()
        self.cmb_group.addItem("One panel per data set (lines = columns)", "dataset")
        self.cmb_group.addItem("One panel per column (lines = data sets)", "column")
        self.cmb_group.addItem("All in one panel", "one")
        f.addRow("Panels:", self.cmb_group)
        gc.addLayout(f)
        lay.addWidget(g_ch)

        g_cl = QGroupBox("Climate season")
        fc = QFormLayout(g_cl)
        self.cmb_clim = QComboBox()
        self.cmb_clim.currentIndexChanged.connect(self._on_clim_changed)
        fc.addRow("Climate data:", self.cmb_clim)
        srow = QHBoxLayout()
        self.cmb_s = QComboBox()
        self.cmb_e = QComboBox()
        for c in (self.cmb_s, self.cmb_e):
            c.addItems(M.WINDOW_LABELS)
        self.cmb_s.setCurrentText("Jun")
        self.cmb_e.setCurrentText("Jul")
        srow.addWidget(self.cmb_s)
        srow.addWidget(QLabel("to"))
        srow.addWidget(self.cmb_e)
        fc.addRow("Season:", srow)
        self.cmb_agg = QComboBox()
        self.cmb_agg.addItem("Auto (sum for precipitation-like)", "auto")
        self.cmb_agg.addItem("Mean", "mean")
        self.cmb_agg.addItem("Sum", "sum")
        fc.addRow("Aggregation:", self.cmb_agg)
        self.chk_best_each = QCheckBox("Best season of each chronology (from the results)")
        self.chk_best_each.setToolTip("Every chronology uses its own strongest climate data set and window from the "
                                      "correlation table of the Analysis tab.")
        fc.addRow(self.chk_best_each)
        b_sug = QPushButton("Suggest from results")
        b_sug.setToolTip("Climate data and season of the strongest correlation of the ticked chronologies.")
        b_sug.clicked.connect(self.suggest_from_results)
        fc.addRow(b_sug)
        self.lbl_sug = QLabel("")
        self.lbl_sug.setWordWrap(True)
        self.lbl_sug.setStyleSheet("color: gray;")
        fc.addRow(self.lbl_sug)
        lay.addWidget(g_cl)

        g_o = QGroupBox("Moving window")
        fo = QFormLayout(g_o)
        self.sp_win = QSpinBox()
        self.sp_win.setRange(7, 151)
        self.sp_win.setValue(25)
        self.sp_win.setSuffix(" years")
        fo.addRow("Window:", self.sp_win)
        self.sp_step = QSpinBox()
        self.sp_step.setRange(1, 50)
        self.sp_step.setValue(1)
        self.sp_step.setSuffix(" year(s)")
        fo.addRow("Step:", self.sp_step)
        self.cmb_pos = QComboBox()
        self.cmb_pos.addItem("Centre year of the window", "centre")
        self.cmb_pos.addItem("Last year of the window", "end")
        self.cmb_pos.addItem("First year of the window", "start")
        fo.addRow("Plot at:", self.cmb_pos)
        self.cmb_method = QComboBox()
        self.cmb_method.addItem("Pearson", "pearson")
        self.cmb_method.addItem("Spearman", "spearman")
        fo.addRow("Correlation:", self.cmb_method)
        self.cmb_prep = QComboBox()
        self.cmb_prep.addItem("Remove linear trend of the climate", "detrend_climate")
        self.cmb_prep.addItem("None", "none")
        self.cmb_prep.addItem("Remove linear trends of both series", "detrend_both")
        self.cmb_prep.addItem("First differences", "diff")
        self.cmb_prep.addItem("Remove linear trends inside each window", "detrend_window")
        fo.addRow("Preparation:", self.cmb_prep)
        prow = QHBoxLayout()
        self.sp_y0 = QSpinBox()
        self.sp_y1 = QSpinBox()
        for sp in (self.sp_y0, self.sp_y1):
            sp.setRange(0, 3000)
            sp.setSpecialValueText("auto")
            sp.setValue(0)
        prow.addWidget(self.sp_y0)
        prow.addWidget(QLabel("-"))
        prow.addWidget(self.sp_y1)
        fo.addRow("Period:", prow)
        self.sp_minfrac = QSpinBox()
        self.sp_minfrac.setRange(50, 100)
        self.sp_minfrac.setValue(80)
        self.sp_minfrac.setSuffix(" % of the years")
        fo.addRow("Min. data in a window:", self.sp_minfrac)
        brow = QHBoxLayout()
        self.chk_boot = QCheckBox("Bootstrap 95% band")
        self.sp_boot = QSpinBox()
        self.sp_boot.setRange(100, 10000)
        self.sp_boot.setValue(1000)
        self.sp_boot.setSuffix(" resamples")
        brow.addWidget(self.chk_boot)
        brow.addWidget(self.sp_boot)
        fo.addRow(brow)
        lay.addWidget(g_o)

        g_sc = QGroupBox("Screen all seasons (find the most stable signals)")
        fsc = QFormLayout(g_sc)
        self.cmb_sc_len = QComboBox()
        self.cmb_sc_len.addItem("1-12 months", 12)
        self.cmb_sc_len.addItem("1-24 months", 24)
        fsc.addRow("Window lengths:", self.cmb_sc_len)
        self.sp_sc_perm = QSpinBox()
        self.sp_sc_perm.setRange(0, 2000)
        self.sp_sc_perm.setValue(100)
        self.sp_sc_perm.setToolTip("Chance control: the chronology years are shuffled this many times and the whole "
                                   "screening is repeated; 'p (shuffled)' = share of shuffles that reach the score "
                                   "(0 = off)")
        fsc.addRow("Shuffles (chance control):", self.sp_sc_perm)
        self.sp_sc_top = QSpinBox()
        self.sp_sc_top.setRange(1, 50)
        self.sp_sc_top.setValue(5)
        fsc.addRow("Top per chronology:", self.sp_sc_top)
        self.b_screen = QPushButton("Screen all seasons x all climate data")
        self.b_screen.setToolTip("Every ticked chronology x every monthly climate data set x every window of months, "
                                 "with the moving-window settings above. If a common period is too short, the longest "
                                 "usable window is selected automatically; pairs still too short are reported and skipped. "
                                 "Independent of the correlation analysis.")
        self.b_screen.clicked.connect(self.screen_all)
        fsc.addRow(self.b_screen)
        self.lbl_sc = QLabel("")
        self.lbl_sc.setWordWrap(True)
        self.lbl_sc.setStyleSheet("color: gray;")
        fsc.addRow(self.lbl_sc)
        lay.addWidget(g_sc)

        run = QHBoxLayout()
        self.b_run = QPushButton("Compute moving correlations")
        f_b = self.b_run.font()
        f_b.setBold(True)
        self.b_run.setFont(f_b)
        self.b_run.clicked.connect(self.compute)
        run.addWidget(self.b_run)
        lay.addLayout(run)
        sv = QHBoxLayout()
        b_png = QPushButton("Save figure...")
        b_png.clicked.connect(self.save_figure)
        b_csv = QPushButton("Save tables (CSV)...")
        b_csv.clicked.connect(self.save_tables)
        sv.addWidget(b_png)
        sv.addWidget(b_csv)
        lay.addLayout(sv)
        b_help = QPushButton("Help")
        b_help.clicked.connect(lambda: QMessageBox.information(self, "Moving correlation", HELP_TEXT))
        lay.addWidget(b_help)
        self._dock_row = QHBoxLayout()
        self._dock_row.addStretch(1)
        lay.addLayout(self._dock_row)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_inner)
        scroll.setMinimumWidth(400)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for cmb in left_inner.findChildren(QComboBox):
            cmb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cmb.setMinimumContentsLength(8)
        split.addWidget(scroll)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)
        self.fig = Figure(figsize=(10, 6))
        self.canvas = FigureCanvas(self.fig)
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)
        rl.addWidget(NavigationToolbar(self.canvas, self))
        self._canvas_scroll = QScrollArea()
        self._canvas_scroll.setWidgetResizable(True)
        self._canvas_scroll.setWidget(self.canvas)
        rl.addWidget(self._canvas_scroll, 3)
        try:
            import dendro_ui_help as _uh

            _uh.mpl_wheel_zoom(self.canvas)
        except Exception:
            pass
        tabs = QTabWidget()
        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        tabs.addTab(self.txt, "Summary")
        self.tbl = QTableWidget(0, 0)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSortingEnabled(True)
        tabs.addTab(self.tbl, "Stability statistics")
        scw = QWidget()
        scl = QVBoxLayout(scw)
        scl.setContentsMargins(0, 0, 0, 0)
        scrow = QHBoxLayout()
        self.b_plot_top = QPushButton("Plot the top rows of each chronology")
        self.b_plot_top.clicked.connect(self._plot_top)
        scrow.addWidget(self.b_plot_top)
        b_sc_csv = QPushButton("Save screening (CSV)...")
        b_sc_csv.clicked.connect(self._save_screen)
        scrow.addWidget(b_sc_csv)
        scrow.addWidget(QLabel("Double-click a row to plot it."))
        scrow.addStretch(1)
        scl.addLayout(scrow)
        self.tbl_sc = QTableWidget(0, 0)
        self.tbl_sc.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_sc.setSortingEnabled(True)
        self.tbl_sc.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sc.itemDoubleClicked.connect(lambda it: self._plot_screen_rows([it.row()]))
        scl.addWidget(self.tbl_sc, 1)
        self.tabs_out = tabs
        tabs.addTab(scw, "Screening (top seasons)")
        rl.addWidget(tabs, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 1000])

    def set_dock(self, dock) -> None:
        self._dock = dock
        self._dock_row.addWidget(dock.make_button(self))
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close_requested_from_tab)
        self._dock_row.addWidget(b_close)

    def close_requested_from_tab(self) -> None:
        dock = self._dock
        self._dock = None
        if dock is not None:
            dock.close()
        self.hide()

    # ------------------------------------------------------------------ host data
    def refresh_from_host(self) -> None:
        checked = {self.lst_chron.item(i).data(Qt.UserRole) for i in range(self.lst_chron.count())
                   if self.lst_chron.item(i).checkState() == Qt.Checked}
        first = self.lst_chron.count() == 0
        try:
            self._chrons = list(self.host.moving_chronologies() or [])
        except Exception:
            self._chrons = []
        try:
            self._climates = dict(self.host.moving_climates() or {})
        except Exception:
            self._climates = {}
        self.lst_chron.clear()
        for k, c in enumerate(self._chrons):
            it = QListWidgetItem(c["name"])
            it.setData(Qt.UserRole, c["name"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = (c["name"] in checked) if not first else (k < 12)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            s = c["series"]
            it.setToolTip(f"{c['name']}: {int(s.index.min())}-{int(s.index.max())}, {len(s)} years")
            self.lst_chron.addItem(it)
        self._apply_filter()
        cur = self.cmb_clim.currentText()
        self.cmb_clim.blockSignals(True)
        self.cmb_clim.clear()
        for name, c in self._climates.items():
            kind = "monthly" if c.get("monthly") is not None else "annual"
            self.cmb_clim.addItem(name, name)
            self.cmb_clim.setItemData(self.cmb_clim.count() - 1, f"{kind}, {c.get('param', '')}", Qt.ToolTipRole)
        i = self.cmb_clim.findText(cur)
        self.cmb_clim.setCurrentIndex(i if i >= 0 else 0)
        self.cmb_clim.blockSignals(False)
        self._on_clim_changed()
        try:
            rid = id(self.host.field_results())
        except Exception:
            rid = None
        if (first or rid != getattr(self, "_results_id", None)) and self._results_frame() is not None:
            self.suggest_from_results(quiet=True)
        self._results_id = rid
        if first:
            self.restore_screen()

    def _apply_filter(self) -> None:
        t = self.ed_filter.text().strip().lower()
        for i in range(self.lst_chron.count()):
            it = self.lst_chron.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _check_all(self, on: bool) -> None:
        for i in range(self.lst_chron.count()):
            it = self.lst_chron.item(i)
            if not it.isHidden():
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _on_clim_changed(self) -> None:
        c = self._climates.get(self.cmb_clim.currentText(), {})
        monthly = c.get("monthly") is not None
        self.cmb_s.setEnabled(monthly)
        self.cmb_e.setEnabled(monthly)

    def _checked(self) -> List[Dict[str, Any]]:
        names = {self.lst_chron.item(i).data(Qt.UserRole) for i in range(self.lst_chron.count())
                 if self.lst_chron.item(i).checkState() == Qt.Checked}
        return [c for c in self._chrons if c["name"] in names]

    # ------------------------------------------------------------------ suggestions from the results
    def _results_frame(self) -> Optional[pd.DataFrame]:
        try:
            df = self.host.field_results()
        except Exception:
            df = None
        if df is None or getattr(df, "empty", True) or not {"Chronology", "Climate", "Window", "R"}.issubset(df.columns):
            return None
        d = df.copy()
        d["R"] = pd.to_numeric(d["R"], errors="coerce")
        d["N"] = pd.to_numeric(d["N"], errors="coerce") if "N" in d.columns else np.nan
        d["win"] = d["Window"].map(M.parse_season)
        d = d.dropna(subset=["R"])
        d = d[d["win"].notna() & d["Climate"].isin([k for k, v in self._climates.items()
                                                     if v.get("monthly") is not None])]
        if d.empty:
            return None
        d["absR"] = d["R"].abs()
        nn = d["N"].fillna(30).clip(lower=4)
        d["_z"] = np.arctanh(d["absR"].clip(upper=0.999)) * np.sqrt(nn - 3)
        return d

    def _rows_for(self, d: pd.DataFrame, c: Dict[str, Any]) -> pd.DataFrame:
        m = d["Chronology"] == c["dataset"]
        if "Chron_Column" in d.columns and c.get("column"):
            m2 = m & (d["Chron_Column"].astype(str) == str(c["column"]))
            if m2.any():
                m = m2
        return d[m]

    def suggest_from_results(self, quiet: bool = False) -> None:
        d = self._results_frame()
        if d is None:
            if not quiet:
                QMessageBox.information(self, "Moving correlation", "No monthly correlation results yet: run the "
                                        "analysis in the Analysis tab first (or choose the season by hand).")
            return
        sel = self._checked() or self._chrons
        parts = [self._rows_for(d, c) for c in sel]
        parts = [p for p in parts if not p.empty]
        sub = pd.concat(parts) if parts else d
        # strongest evidence; prefer the season that is strongest for most chronologies
        best_each = sub.sort_values("_z", ascending=False).groupby(["Chronology"] + (
            ["Chron_Column"] if "Chron_Column" in sub.columns else []), as_index=False).head(1)
        key = best_each.groupby(["Climate", "Window"]).agg(n=("R", "size"), z=("_z", "sum")).sort_values(
            ["n", "z"], ascending=False)
        clim, win = key.index[0]
        row = sub[(sub["Climate"] == clim) & (sub["Window"] == win)].sort_values("_z", ascending=False).iloc[0]
        si, ei = M.parse_season(win)
        self.cmb_clim.setCurrentText(str(clim))
        self.cmb_s.setCurrentIndex(si)
        self.cmb_e.setCurrentIndex(ei)
        agg = str(row.get("Agg") or "").lower()
        i = self.cmb_agg.findData(agg if agg in ("mean", "sum") else "auto")
        self.cmb_agg.setCurrentIndex(max(0, i))
        try:
            per = self.host.field_period()
        except Exception:
            per = None
        if per:
            self.sp_y0.setValue(int(per[0]))
            self.sp_y1.setValue(int(per[1]))
        if not quiet or not getattr(self, "_best_each_set", False):
            self.chk_best_each.setChecked(True)
            self._best_each_set = True
        self.lbl_sug.setText(f"Suggested: {clim}, {M.season_label(si, ei)} - strongest season for "
                             f"{int(key.iloc[0]['n'])} of {len(best_each)} chronolog(ies); best r = {row['R']:+.2f}"
                             + ("" if pd.isna(row["N"]) else f" (n = {int(row['N'])})") + ".")

    # ------------------------------------------------------------------ compute
    def _options(self) -> M.MovingOptions:
        y0, y1 = self.sp_y0.value(), self.sp_y1.value()
        return M.MovingOptions(window=self.sp_win.value(), step=self.sp_step.value(),
                               position=self.cmb_pos.currentData(), method=self.cmb_method.currentData(),
                               prep=self.cmb_prep.currentData(), min_frac=self.sp_minfrac.value() / 100.0,
                               period=(y0 or -10 ** 6, y1 or 10 ** 6) if (y0 or y1) else None,
                               bootstrap=self.sp_boot.value() if self.chk_boot.isChecked() else 0)

    def _climate_series(self, name: str, si: int, ei: int, agg: str) -> Tuple[Optional[pd.Series], str]:
        c = self._climates.get(name)
        if c is None:
            return None, ""
        if agg == "auto":
            agg = M.default_agg(c.get("param", ""))
        if c.get("monthly") is not None:
            return M.season_series(c["monthly"], si, ei, agg), f"{name} {M.season_label(si, ei)} {agg}"
        s = c.get("annual")
        return (s if s is not None else None), name

    def _items(self) -> List[M.MovingItem]:
        group_by = self.cmb_group.currentData()
        chrons = self._checked()
        if not chrons:
            raise ValueError("Tick at least one chronology.")
        d = self._results_frame() if self.chk_best_each.isChecked() else None
        if self.chk_best_each.isChecked() and d is None:
            raise ValueError("No monthly correlation results for 'Best season of each chronology'.")
        items: List[M.MovingItem] = []
        cache: Dict[Tuple[str, int, int, str], Tuple[Optional[pd.Series], str]] = {}
        for c in chrons:
            if d is not None:
                rows = self._rows_for(d, c)
                if rows.empty:
                    continue
                row = rows.sort_values("_z", ascending=False).iloc[0]
                name, (si, ei) = str(row["Climate"]), row["win"]
                agg = str(row.get("Agg") or "auto").lower()
                agg = agg if agg in ("mean", "sum") else "auto"
            else:
                name, si, ei = self.cmb_clim.currentText(), self.cmb_s.currentIndex(), self.cmb_e.currentIndex()
                agg = self.cmb_agg.currentData()
            if ei < si:
                raise ValueError("The season must not end before it starts.")
            key = (name, si, ei, agg)
            if key not in cache:
                cache[key] = self._climate_series(name, si, ei, agg)
            clim, clabel = cache[key]
            if clim is None or clim.empty:
                continue
            ds, col = c["dataset"], str(c.get("column") or "")
            if group_by == "dataset":
                group, label = ds, (col or ds)
            elif group_by == "column":
                group, label = (col or "value"), ds
            else:
                group, label = "All", c["name"]
            if d is not None:  # the season of this chronology goes into the panel title
                short = name if len(name) <= 26 else name[:24] + ".."
                group = (f"{M.season_label(si, ei)} {short}" if group == "All"
                         else group + f"  |  {M.season_label(si, ei)} {short}")
            items.append(M.MovingItem(c["name"], group, label,
                                      c["series"], clim, clabel, c.get("lat"), c.get("lon")))
        if not items:
            raise ValueError("No chronology overlaps the climate data.")
        return items

    def compute(self) -> None:
        try:
            items = self._items()
            opt = self._options()
        except ValueError as exc:
            QMessageBox.information(self, "Moving correlation", str(exc))
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            self._results = [M.moving_correlation(it, opt) for it in items]
            self._opt = opt
            clims = sorted({r.item.climate_label for r in self._results})
            title = (("Centred " if opt.position == "centre" else "") + f"{opt.window}-year running correlations"
                     + (f" with {clims[0]}" if len(clims) == 1 else " (best season of each chronology)"))
            self._annot = None
            n_groups = len({r.item.group for r in self._results})
            ncols = 1 if n_groups == 1 else 2 if n_groups <= 4 else 3
            rows = -(-n_groups // ncols)
            # every row of panels keeps a readable height; the plot area scrolls when needed
            self.canvas.setMinimumHeight(max(320, 90 + 190 * rows))
            self._hover = M.draw_moving(self.fig, self._results, opt, show_ci=True, title=title, ncols=ncols)
            self.canvas.draw_idle()
            self.txt.setPlainText(M.summary_text(self._results, opt) + "\n\n" + "\n".join(
                f"{r.item.name}: {r.stats.get('verdict', '')}" for r in self._results))
            self._fill_table(M.statistics_table(self._results))
        except Exception as exc:
            QMessageBox.warning(self, "Moving correlation", f"{type(exc).__name__}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def _fill_table(self, df: pd.DataFrame, tbl=None) -> None:
        tbl = tbl or self.tbl
        self._fill_any(tbl, df)

    def _fill_any(self, tbl, df: pd.DataFrame) -> None:
        self.tbl, _keep = tbl, self.tbl
        try:
            self._fill_table_impl(df)
        finally:
            self.tbl = _keep

    def _fill_table_impl(self, df: pd.DataFrame) -> None:
        self.tbl.setSortingEnabled(False)
        self.tbl.clear()
        self.tbl.setRowCount(len(df))
        self.tbl.setColumnCount(len(df.columns))
        self.tbl.setHorizontalHeaderLabels([str(c) for c in df.columns])
        for i, (_k, row) in enumerate(df.iterrows()):
            for j, col in enumerate(df.columns):
                v = row[col]
                it = QTableWidgetItem(_fmt(v, 3 if str(col).startswith("p") else 2))
                if isinstance(v, (float, int, np.floating)) and np.isfinite(v):
                    it.setData(Qt.UserRole, float(v))
                self.tbl.setItem(i, j, it)
        self.tbl.resizeColumnsToContents()
        if "Verdict" in df.columns:
            self.tbl.setColumnWidth(list(df.columns).index("Verdict"), 520)
        self.tbl.setSortingEnabled(True)

    # ------------------------------------------------------------------ screening
    SCREEN_COLS = ["Chronology", "Climate", "Type", "Window", "Window years", "Period", "% windows p<0.05", "Mean r", "Min r",
                   "Max r", "r (full)", "N", "Windows", "p (shuffled)", "Chance best score (95%)", "Score",
                   "r 1st half", "r 2nd half", "p (halves differ)", "Verdict"]

    def screen_all(self) -> None:
        import threading
        import time as _time

        if getattr(self, "_screen_thread", None) is not None:
            self._screen_cancel.set()
            self.b_screen.setText("Cancelling ...")
            return
        chrons = {c["name"]: c["series"] for c in self._checked()}
        if not chrons:
            QMessageBox.information(self, "Screening", "Tick at least one chronology.")
            return
        clims = {k: {"monthly": v["monthly"], "param": v.get("param", "")}
                 for k, v in self._climates.items() if v.get("monthly") is not None}
        if not clims:
            QMessageBox.information(self, "Screening", "No monthly climate data set is loaded.")
            return
        opt = self._options()
        max_len = int(self.cmb_sc_len.currentData())
        n_perm = int(self.sp_sc_perm.value())
        keep = max(25, int(self.sp_sc_top.value()))
        n_win = len(M.all_windows(max_len))
        self._screen_cancel = threading.Event()
        box: Dict[str, Any] = {"done": 0, "total": len(chrons)}

        def prog(d, t):
            box["done"], box["total"] = d, t

        def work():
            try:
                box["df"] = M.screen_all(chrons, clims, opt, max_len=max_len, n_perm=n_perm, keep=keep,
                                         progress=prog, cancel=self._screen_cancel.is_set)
            except Exception as exc:  # shown in the GUI
                box["error"] = exc

        th = threading.Thread(target=work, daemon=True)
        self._screen_thread = th
        self.b_screen.setText("Cancel screening")
        t0 = _time.time()
        self.lbl_sc.setText(f"Screening {len(chrons)} chronolog(ies) x {len(clims)} data set(s) x {n_win} windows"
                            + (f", {n_perm} shuffles" if n_perm else "") + " ...")
        th.start()
        from PySide6.QtCore import QTimer

        timer = QTimer(self)
        timer.setInterval(300)

        def poll():
            if th.is_alive():
                self.lbl_sc.setText(f"Screening: {box['done']} of {box['total']} chronolog(ies) done, "
                                    f"{int(_time.time() - t0)} s ...")
                return
            timer.stop()
            self._screen_thread = None
            self.b_screen.setText("Screen all seasons x all climate data")
            if "error" in box:
                QMessageBox.warning(self, "Screening", f"{type(box['error']).__name__}: {box['error']}")
                return
            df = box.get("df")
            if self._screen_cancel.is_set() or df is None:
                self.lbl_sc.setText("Screening cancelled.")
                return
            adjustments = list(df.attrs.get("screening_adjustments", []))
            skipped = list(df.attrs.get("screening_skipped", []))
            self._set_screen(df)
            summary = f"Done in {int(_time.time() - t0)} s: {len(df)} row(s) kept."
            if adjustments:
                summary += f" Adjusted window lengths for {len(adjustments)} chronology/data pair(s)."
            if skipped:
                summary += f" Skipped {len(skipped)} pair(s) with insufficient data."
            if not df.empty:
                summary += " See the tab 'Screening (top seasons)'."
            self.lbl_sc.setText(summary)
            if adjustments or skipped:
                details = [summary]
                if adjustments:
                    details.append("\nWindow lengths adjusted:\n" + "\n".join(adjustments[:12]))
                    if len(adjustments) > 12:
                        details.append(f"…and {len(adjustments) - 12} more adjustment(s).")
                if skipped:
                    details.append("\nSkipped pairs:\n" + "\n".join(skipped[:12]))
                    if len(skipped) > 12:
                        details.append(f"…and {len(skipped) - 12} more skipped pair(s).")
                QMessageBox.information(self, "Screening period", "\n".join(details))

        timer.timeout.connect(poll)
        timer.start()

    def _set_screen(self, df: pd.DataFrame) -> None:
        self._screen_df = df
        try:
            self.host.moving_screen_df = df  # saved with the project / session
        except Exception:
            pass
        if df.empty or "Chronology" not in df.columns:
            self._screen_view = pd.DataFrame()
            self._fill_any(self.tbl_sc, pd.DataFrame())
            self.tabs_out.setCurrentIndex(2)
            self.txt.setPlainText("No screening results. Check the period and loaded monthly climate data; "
                                  "chronology/data pairs without enough years are listed in the screening message.")
            return
        top = int(self.sp_sc_top.value())
        view = df.groupby("Chronology", sort=False).head(top) if not df.empty else df
        cols = [c for c in self.SCREEN_COLS if c in view.columns]
        self._screen_view = view.reset_index(drop=True)
        self._fill_any(self.tbl_sc, self._screen_view[cols])
        self.tbl_sc.setSortingEnabled(False)  # row numbers must match the screening table
        self.tabs_out.setCurrentIndex(2)
        lines = []
        for ch, g in view.groupby("Chronology", sort=False):
            r = g.iloc[0]
            pp = r.get("p (shuffled)", float("nan"))
            lines.append(f"{ch}: best {r['Window']} {r['Climate']} - significant in {r['% windows p<0.05']:.0f}% of "
                         f"the running windows, mean r {r['Mean r']:+.2f}"
                         + ("" if not np.isfinite(pp) else f", p (shuffled) = {pp:.3f}") + f"; {r['Verdict']}")
        if lines:
            self.txt.setPlainText("Screening of all seasons (top of each chronology):\n" + "\n".join(lines) +
                                  "\n\n'p (shuffled)' compares the score with the best score of the same screening "
                                  "on shuffled chronologies: below 0.05 the stability is unlikely to be chance.")

    def restore_screen(self) -> None:
        df = getattr(self.host, "moving_screen_df", None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            self._set_screen(df)

    def _plot_screen_rows(self, rows: List[int]) -> None:
        view = getattr(self, "_screen_view", None)
        if view is None or view.empty:
            return
        chrons = {c["name"]: c for c in self._chrons}
        opt = self._options()
        items = []
        for k in rows:
            if not (0 <= k < len(view)):
                continue
            r = view.iloc[k]
            c = chrons.get(r["Chronology"])
            cl = self._climates.get(r["Climate"])
            if c is None or cl is None or cl.get("monthly") is None:
                continue
            agg = M.default_agg(cl.get("param", ""))
            clim = M.season_series(cl["monthly"], int(r["si"]), int(r["ei"]), agg)
            label = f"{r['Window']} {r['Climate']}"
            items.append(M.MovingItem(f"{r['Chronology']} | {label}", r["Chronology"], label, c["series"], clim,
                                      f"{r['Climate']} {r['Window']} {agg}", c.get("lat"), c.get("lon")))
        if not items:
            return
        self._results = [M.moving_correlation(it, opt) for it in items]
        self._opt = opt
        n_groups = len({it.group for it in items})
        ncols = 1 if n_groups == 1 else 2 if n_groups <= 4 else 3
        self.canvas.setMinimumHeight(max(320, 90 + 190 * (-(-n_groups // ncols)) + 18 * len(items)))
        self._annot = None
        self._hover = M.draw_moving(self.fig, self._results, opt, show_ci=True, ncols=ncols,
                                    title=f"Centred {opt.window}-year running correlations - screening top seasons")
        self.canvas.draw_idle()
        self._fill_table(M.statistics_table(self._results))

    def _plot_top(self) -> None:
        view = getattr(self, "_screen_view", None)
        if view is None or view.empty:
            QMessageBox.information(self, "Screening", "Run the screening first.")
            return
        self._plot_screen_rows(list(range(len(view))))

    def _save_screen(self) -> None:
        df = getattr(self, "_screen_df", None)
        if df is None or df.empty:
            QMessageBox.information(self, "Screening", "Run the screening first.")
            return
        path, _f = QFileDialog.getSaveFileName(self, "Save screening", "moving_screening.csv", "CSV (*.csv)")
        if path:
            df.to_csv(path, index=False, encoding="utf-8-sig")
            _notify(self, "Screening saved.", path)

    # ------------------------------------------------------------------ hover
    def _on_hover(self, ev) -> None:
        ax = ev.inaxes
        if ax is None or ax not in self._hover or ev.xdata is None:
            if self._annot is not None and self._annot.get_visible():
                self._annot.set_visible(False)
                self.canvas.draw_idle()
            return
        best = None
        for ln, res in self._hover[ax]:
            t = res.table
            if t.empty:
                continue
            k = int(np.argmin(np.abs(t["year"].to_numpy(dtype=float) - ev.xdata)))
            row = t.iloc[k]
            dy = abs(float(row["r"]) - float(ev.ydata)) if np.isfinite(row["r"]) else 9.0
            if best is None or dy < best[0]:
                best = (dy, res, row)
        if best is None:
            return
        _dy, res, row = best
        text = (f"{res.item.name}\n{int(row['start'])}-{int(row['end'])} (centre {row['year']:.1f})\n"
                f"r = {row['r']:+.2f}, n = {int(row['n'])}, p = {row['p']:.3f}")
        if np.isfinite(row.get("lo", np.nan)):
            text += f"\n95% CI {row['lo']:+.2f} .. {row['hi']:+.2f}"
        if self._annot is None or self._annot.axes is not ax:
            if self._annot is not None:
                try:
                    self._annot.remove()
                except Exception:
                    pass
            self._annot = ax.annotate("", xy=(0, 0), xytext=(12, 12), textcoords="offset points", fontsize=8,
                                      bbox=dict(boxstyle="round", fc="white", ec="#8a8985", alpha=0.95), zorder=10)
        self._annot.xy = (row["year"], row["r"])
        self._annot.set_text(text)
        self._annot.set_visible(True)
        self.canvas.draw_idle()

    # ------------------------------------------------------------------ save
    def save_figure(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Save", "Compute the moving correlations first.")
            return
        path, _f = QFileDialog.getSaveFileName(self, "Save figure", "moving_correlation.png",
                                               "PNG (*.png);;PDF (*.pdf);;SVG (*.svg);;JPEG (*.jpg)")
        if not path:
            return
        kw: Dict[str, Any] = {"dpi": 300, "bbox_inches": "tight"}
        if path.lower().endswith((".jpg", ".jpeg")):
            kw["pil_kwargs"] = {"quality": 100}
        if self._annot is not None:
            self._annot.set_visible(False)
        self.fig.savefig(path, **kw)
        _notify(self, "Figure saved.", path)

    def save_tables(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Save", "Compute the moving correlations first.")
            return
        path, _f = QFileDialog.getSaveFileName(self, "Save running correlations", "moving_correlation.csv",
                                               "CSV (*.csv)")
        if not path:
            return
        base = os.path.splitext(path)[0]
        M.long_table(self._results, self._opt).to_csv(base + ".csv", index=False, encoding="utf-8-sig")
        M.statistics_table(self._results).to_csv(base + "_stability.csv", index=False, encoding="utf-8-sig")
        with open(base + "_summary.txt", "w", encoding="utf-8") as fh:
            fh.write(self.txt.toPlainText() + "\n")
        _notify(self, "Running correlations, stability statistics and summary saved.", base + ".csv")
