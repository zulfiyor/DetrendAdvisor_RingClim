#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_online_qt.py - PySide6 dialog for online data (ITRDB + CRU TS).

Used by RingClim ("Online data..." button) and runnable on its own:

    python dendro_online_qt.py

The dialog is non-modal. Every finished import emits ``datasets_ready`` with
a list of plain dicts, so the host application decides how to store them:

    {
        "kind": "chronology" | "climate",
        "name": str,
        "path": str,                 # downloaded file or source URL
        "df": pandas.DataFrame,      # chronology: Year + 1 value column
                                     # climate:    Year + 12 month columns
        "value_column": str,         # chronology only
        "climate_param": str,        # climate only (RingClim climate type)
        "meta": dict,                # provenance, warnings, citation
    }

When run standalone, results are written as CSV (+ JSON metadata) into a
folder chosen by the user.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure

import dendro_online as DO

try:  # keyless climate sources + location export (optional modules)
    import dendro_geoexport as GX
    import dendro_sources_qt as SQ
    _SOURCES_ERROR = ""
except Exception as _src_exc:  # keep ITRDB / CRU usable without them
    GX = None
    SQ = None
    _SOURCES_ERROR = f"{type(_src_exc).__name__}: {_src_exc}"

MODE_LABELS = [
    ("bilinear", "Point: bilinear (4 nearest cells)"),
    ("nearest", "Point: nearest grid cell"),
    ("bbox_mean", "Area mean (bounding box)"),
]

HELP_TEXT = """
<h3>Online data</h3>
<p><b>ITRDB</b>: search the International Tree-Ring Data Bank (NOAA NCEI) around a
point or inside a bounding box. Select a site to see its files. Checked
chronologies (<i>.crn</i>) are downloaded and imported as chronology datasets;
other checked files (raw measurements <i>.rwl</i>, NOAA templates) are only
downloaded to the chosen folder.</p>
<ul>
<li><b>std / res / ars</b>: standard, residual and ARSTAN chronology versions.</li>
<li><b>Minimum sample depth</b>: years with fewer series are removed on import.</li>
<li><b>Preview</b> (or double-click a file): loads the file into memory and shows
the series coverage, mean ring width, sample depth / chronology index and the
file header before anything is saved.</li>
<li><b>Batch download</b>: picks files from all found (or selected) sites by
criteria - file type, parameter, chronology version, record length, last/first
year and distance - and downloads them in one go.</li>
<li><b>Send to CRU</b>: adds the selected sites as CRU extraction targets.</li>
</ul>
<p><b>CRU TS</b>: monthly 0.5&deg; gridded climate (Harris et al. 2020). Only the
decadal files covering the chosen period are downloaded. With
<i>Delete downloaded files after extraction</i> checked, each file is removed
as soon as the values are extracted, so the download is temporary.</p>
<ul>
<li><b>Bilinear</b>: weighted mean of the 4 surrounding cell centres; cells
without data (sea, large lakes) are skipped and the weights renormalised.</li>
<li><b>Nearest cell</b>: value of the single closest cell.</li>
<li><b>Area mean</b>: cos(latitude)-weighted mean of all cells whose centre
lies in the bounding box.</li>
<li>Several targets (e.g. ITRDB sites) are extracted in one pass.</li>
<li>The CRU station count (<i>stn</i>) is checked: years with almost no
stations relax towards climatology, which weakens climate-growth correlations.
A warning is shown and stored with the dataset.</li>
</ul>
<p><b>More climate data</b> (no registration or key): CHELSA monthly and daily
(~1 km), TerraClimate (incl. PDSI, soil moisture, runoff), GPCC precipitation,
CRU scPDSI, SPEIbase, SPEI / SPI computed from P and PET at any scale, CHIRPS v3
(daily / monthly), Berkeley Earth country temperature, GHCN-Daily weather
stations and ERA5 / ERA5-Land via Open-Meteo. Daily data are added as daily
series (Analysis tab, Resolution = Daily) and / or as monthly tables. GRDC river
discharge has no public API: use <i>GRDC portal</i> and import the files.</p>
<p><b>Copernicus sources (your own free key)</b>: ERA5 monthly (0.25&deg;, 1940-),
ERA5-Land monthly (0.1&deg;, 1950-) from the Climate Data Store and GloFAS v4 daily
river discharge (0.05&deg;, 1979-) from the Early Warning Data Store. Register for free,
copy your <i>personal access token</i> from your profile page into <i>API keys...</i>
and accept each data set licence once on its web page. Every user uses his or her
own key; it is saved only on this computer. Requests wait in the Copernicus queue
(usually minutes); results are cached. For GloFAS the cell with the largest mean
discharge near the point is used (the river).</p>
<p><b>Download everything for the area</b> (More climate data tab): one run for
many sources and variables at once - CRU TS, TerraClimate, CHELSA, GPCC, SPEI / SPI,
CHIRPS, Berkeley Earth, nearest GHCN stations, ERA5 and the ITRDB chronologies in the
area - for the location (point, box or all CRU TS targets) and period. Every source
is clipped to its own period; failures are listed and skipped. All datasets are
added and selected in the Analysis tab, and can be saved to a folder (CSV per
dataset, one Excel workbook, a long table, a summary, citations and the locations).</p>
<p><b>Map</b>: shows the country containing the point, the grid and the
cells used with their weights (red = no data). Use it to check that the
coordinates are correct (latitude N positive, longitude E positive).</p>
<p><b>Export locations</b>: every download and extraction is listed; export the
points (ITRDB sites, climate points, stations), the grid cells used for the
interpolation and the area boxes as CSV (ArcGIS <i>XY Table To Point</i>),
GeoJSON, KMZ (Google Earth) or Shapefile, with species, periods, variables,
weights and links. The list is kept between program runs unless you choose
not to keep the cache when the program closes.</p>
"""


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class _TaskWorker(QObject):
    progress = Signal(str, int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[DO.ProgressCallback, DO.CancelCheck], Any],
                 cancel_event: threading.Event) -> None:
        super().__init__()
        self._fn = fn
        self._cancel = cancel_event

    def _emit(self, stage: str, done: int, total: int, message: str) -> None:
        self.progress.emit(str(stage), int(min(done, 2**31 - 1)), int(min(total, 2**31 - 1)), str(message))

    def run(self) -> None:
        try:
            result = self._fn(self._emit, self._cancel.is_set)
        except DO.CancelledError:
            self.failed.emit("Cancelled by user.")
        except Exception as exc:  # report everything to the GUI
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")
        else:
            self.finished.emit(result)


class MapWidget(QWidget):
    """Matplotlib canvas with navigation toolbar for the location map."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.figure = Figure(figsize=(7, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.toolbar)
        lay.addWidget(self.canvas, 1)
        self.interactor = DO.MapInteractor(self.canvas) if hasattr(DO, "MapInteractor") else None
        self._view_mode = "keep"   # "keep" (user zoom / pan) | "auto" (once) | "world" (once)

    def set_handlers(self, on_point, on_box, snap) -> None:
        if self.interactor is not None:
            self.interactor.on_point = on_point
            self.interactor.on_box = on_box
            self.interactor.snap = snap

    def reset_view(self) -> None:
        self._view_mode = "auto"

    def world_view(self) -> None:
        self._view_mode = "world"

    def draw_map(self, **kwargs: Any) -> None:
        view = None
        if self._view_mode == "world":
            view = DO.WORLD_VIEW
        elif self._view_mode == "keep" and hasattr(DO, "user_map_view"):
            view = DO.user_map_view(self.figure, kwargs.get("lat"), kwargs.get("lon"))
        self._view_mode = "keep"
        if view is not None:
            kwargs["view"] = view
            kwargs.setdefault("zoom", False)
        try:
            DO.draw_location_map(self.figure, **kwargs)
        except Exception as exc:
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, f"Map error:\n{exc}", ha="center", va="center", transform=ax.transAxes)
            ax.set_axis_off()
        self.canvas.draw_idle()

    def save_png(self, path: str) -> None:
        self.figure.savefig(path, dpi=150)


class PreviewWidget(QWidget):
    """'Preview' tab: plot + summary of an ITRDB file loaded into memory."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.current: Optional[Any] = None   # (site, file)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.figure = Figure(figsize=(7, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        lay.addWidget(NavigationToolbar2QT(self.canvas, self))
        try:
            import dendro_ui_help as _uh

            _uh.mpl_wheel_zoom(self.canvas)
        except Exception:
            pass
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.canvas)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = self.text.font()
        font.setFamily("Consolas")
        self.text.setFont(font)
        split.addWidget(self.text)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)
        row = QHBoxLayout()
        self.btn_import = QPushButton("Download / import this file")
        self.btn_import.setEnabled(False)
        row.addWidget(self.btn_import)
        row.addStretch(1)
        lay.addLayout(row)
        self.show_message("Select a .rwl or .crn file in the list and press Preview (or double-click it).")

    def show_message(self, text: str) -> None:
        self.current = None
        self.btn_import.setEnabled(False)
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.text(0.5, 0.5, text, ha="center", va="center", wrap=True, transform=ax.transAxes, fontsize=9)
        ax.set_axis_off()
        self.canvas.draw_idle()
        self.text.setPlainText("")

    def show_preview(self, pv: "DO.ItrdbPreview", site: Any, f: Any) -> None:
        self.current = (site, f)
        DO.draw_itrdb_preview(self.figure, pv)
        self.canvas.draw_idle()
        self.text.setPlainText(pv.summary_text())
        self.btn_import.setText("Download && import this chronology" if pv.kind == "crn" else "Download this file")
        self.btn_import.setEnabled(True)


class BatchDialog(QDialog):
    """Pick files from many ITRDB sites at once by criteria."""

    def __init__(self, parent: QWidget, all_sites: List["DO.ItrdbSite"], selected: List["DO.ItrdbSite"],
                 center: Optional[tuple]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Batch download from ITRDB")
        self.resize(760, 640)
        self._all = list(all_sites)
        self._selected = list(selected)
        self._center = center
        self.result_files: List[Any] = []
        lay = QVBoxLayout(self)

        scope = QGroupBox("Sites")
        sl = QHBoxLayout(scope)
        self.rb_all = QRadioButton(f"All sites in the results ({len(self._all)})")
        self.rb_sel = QRadioButton(f"Selected sites ({len(self._selected)})")
        (self.rb_sel if len(self._selected) > 1 else self.rb_all).setChecked(True)
        self.rb_sel.setEnabled(bool(self._selected))
        sl.addWidget(self.rb_all)
        sl.addWidget(self.rb_sel)
        lay.addWidget(scope)

        what = QGroupBox("Files")
        wl = QGridLayout(what)
        self.cb_crn = QCheckBox("Chronologies (.crn) - imported as datasets")
        self.cb_rwl = QCheckBox("Raw measurements (.rwl) - downloaded only")
        self.cb_crn.setChecked(True)
        wl.addWidget(self.cb_crn, 0, 0, 1, 3)
        wl.addWidget(self.cb_rwl, 1, 0, 1, 3)
        wl.addWidget(QLabel("Chronology version:"), 2, 0)
        self.cb_std = QCheckBox("standard")
        self.cb_res = QCheckBox("residual")
        self.cb_ars = QCheckBox("ARSTAN")
        self.cb_std.setChecked(True)
        wl.addWidget(self.cb_std, 2, 1)
        wl.addWidget(self.cb_res, 2, 2)
        wl.addWidget(self.cb_ars, 2, 3)
        wl.addWidget(QLabel("Parameter:"), 3, 0)
        params = sorted({f.parameter for s in self._all for f in s.files if f.kind in ("crn", "rwl")},
                        key=lambda p: (p != "Total ring width", p))
        self.param_checks: Dict[str, QCheckBox] = {}
        for k, prm in enumerate(params):
            cb = QCheckBox(prm)
            cb.setChecked(prm == "Total ring width")
            self.param_checks[prm] = cb
            wl.addWidget(cb, 3 + k // 3, 1 + k % 3)
        lay.addWidget(what)

        crit = QGroupBox("Criteria (0 = no limit)")
        cl = QGridLayout(crit)
        self.sp_span = QSpinBox()
        self.sp_span.setRange(0, 5000)
        self.sp_span.setSuffix(" years")
        self.sp_span.setSpecialValueText("any")
        self.sp_last = _year_spin(0, allow_any=True)
        self.sp_first = _year_spin(0, allow_any=True)
        self.sp_dist = _dspin(0, 20000, 0, decimals=0, step=50)
        self.sp_dist.setSuffix(" km")
        self.sp_dist.setEnabled(center is not None)
        cl.addWidget(QLabel("Minimum record length:"), 0, 0)
        cl.addWidget(self.sp_span, 0, 1)
        cl.addWidget(QLabel("Last year at least:"), 0, 2)
        cl.addWidget(self.sp_last, 0, 3)
        cl.addWidget(QLabel("First year at most:"), 1, 0)
        cl.addWidget(self.sp_first, 1, 1)
        cl.addWidget(QLabel("Max distance from point:"), 1, 2)
        cl.addWidget(self.sp_dist, 1, 3)
        hint = QLabel("Tip: 'Last year at least' = 1950 keeps records that overlap CRU TS well.")
        hint.setStyleSheet("color: gray;")
        cl.addWidget(hint, 2, 0, 1, 4)
        lay.addWidget(crit)

        self.lbl_count = QLabel("")
        lay.addWidget(self.lbl_count)
        self.lst = QListWidget()
        lay.addWidget(self.lst, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_ok = QPushButton("Download matching files")
        self.btn_ok.clicked.connect(self._accept)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_ok)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

        for w in [self.rb_all, self.rb_sel, self.cb_crn, self.cb_rwl, self.cb_std, self.cb_res, self.cb_ars,
                  *self.param_checks.values()]:
            w.toggled.connect(self._update)
        for w in (self.sp_span, self.sp_last, self.sp_first):
            w.valueChanged.connect(self._update)
        self.sp_dist.valueChanged.connect(self._update)
        self._update()

    def criteria(self) -> "DO.ItrdbBatchCriteria":
        kinds = tuple(k for k, cb in (("crn", self.cb_crn), ("rwl", self.cb_rwl)) if cb.isChecked())
        variants = tuple(v for v, cb in (("standard", self.cb_std), ("residual", self.cb_res),
                                         ("arstan", self.cb_ars)) if cb.isChecked())
        params = tuple(p for p, cb in self.param_checks.items() if cb.isChecked())
        return DO.ItrdbBatchCriteria(
            kinds=kinds, parameters=params, variants=variants or ("__none__",),
            min_span_years=self.sp_span.value(),
            min_last_year=self.sp_last.value() or None,
            max_first_year=self.sp_first.value() or None,
            max_distance_km=self.sp_dist.value() or None,
            center=self._center,
        )

    def _update(self, *args: Any) -> None:
        sites = self._selected if self.rb_sel.isChecked() else self._all
        files = DO.select_itrdb_files(sites, self.criteria()) if self.criteria().kinds else []
        self.result_files = files
        self.lst.clear()
        for s, f in files:
            v = _VARIANT_SHORT.get(f.variant, f.variant)
            self.lst.addItem(f"{s.site_code:<9} {f.kind:<4} {f.parameter:<18} {v:<4} {f.filename}   "
                             f"{s.first_year}-{s.last_year}  {s.species[:30]}")
        n_sites = len({s.study_id for s, _ in files})
        self.lbl_count.setText(f"{len(files)} file(s) from {n_sites} site(s) match")
        self.btn_ok.setEnabled(bool(files))

    def _accept(self) -> None:
        if len(self.result_files) > 50 and QMessageBox.question(
                self, "Batch download", f"Download {len(self.result_files)} files?") != QMessageBox.Yes:
            return
        self.accept()


def _scrollable(page: QWidget) -> QWidget:
    """Tab page inside a scroll area, so the dialog also fits small / highly scaled screens."""
    from PySide6.QtWidgets import QFrame, QScrollArea

    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setWidget(page)
    return area


def _thread_alive(thread: Any) -> bool:
    try:
        return bool(thread.isRunning())
    except RuntimeError:  # C++ object already deleted
        return False


def _dspin(lo: float, hi: float, value: float, decimals: int = 4, step: float = 0.1) -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setSingleStep(step)
    w.setValue(value)
    w.setKeyboardTracking(False)
    return w


def _year_spin(value: int, allow_any: bool = False) -> QSpinBox:
    w = QSpinBox()
    w.setRange(0 if allow_any else -5000, 2100)
    if allow_any:
        w.setSpecialValueText("any")
    w.setValue(value)
    return w


def _short_param(parameter: str) -> str:
    return {
        "Total ring width": "RW",
        "Earlywood width": "EW",
        "Latewood width": "LW",
        "Maximum density": "MXD",
        "Minimum density": "MND",
    }.get(parameter, parameter.replace("Parameter ", "").replace("'", ""))


_VARIANT_SHORT = {"standard": "std", "residual": "res", "arstan": "ars"}


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------

class OnlineDataDialog(QDialog):
    datasets_ready = Signal(list)
    settings_changed = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None, settings: Optional[Dict[str, Any]] = None,
                 month_names: Sequence[str] = DO.MONTH_NAMES, standalone: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Online data: ITRDB, CRU TS and more climate data")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setModal(False)
        self._settings: Dict[str, Any] = dict(settings or {})
        self._month_names = list(month_names)
        self._standalone = standalone
        self._standalone_dir: Optional[str] = None

        self._thread: Optional[QThread] = None
        self._worker: Optional[_TaskWorker] = None
        self._cancel_event = threading.Event()
        self._busy_widgets: List[QWidget] = []

        self._sites: List[DO.ItrdbSite] = []
        self._cru_last_cells: Dict[str, List[DO.GridCell]] = {}
        self._versions_loaded = False
        self._silent_fail = False
        self._pending_on_done: Optional[Callable[[Any], None]] = None
        self._retired: List[Any] = []
        self._preview_cache: Dict[str, Any] = {}
        self._populating_files = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(lambda: self.preview_selected_file(quiet=True))

        self._map_timer = QTimer(self)
        self._map_timer.setSingleShot(True)
        self._map_timer.setInterval(350)
        self._map_timer.timeout.connect(self.update_map)

        self._build_ui()
        self._apply_settings()
        self.resize(1400, 860)
        QTimer.singleShot(0, self.update_map)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(_scrollable(self._build_itrdb_tab()), "ITRDB (tree-ring data)")
        self.tabs.addTab(_scrollable(self._build_cru_tab()), "CRU TS (gridded climate)")
        self.sources_tab = None
        if SQ is not None:
            self.sources_tab = SQ.SourcesTab(self)
            self.tabs.addTab(_scrollable(self.sources_tab), "More climate data (ERA5, CHELSA, GloFAS ...)")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        left_lay.addWidget(self.tabs, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        self.right_tabs = QTabWidget()
        right_lay.addWidget(self.right_tabs, 1)
        map_page = QWidget()
        map_lay = QVBoxLayout(map_page)
        map_lay.setContentsMargins(0, 0, 0, 0)
        self.map_widget = MapWidget()
        map_lay.addWidget(self.map_widget, 1)
        map_btns = QHBoxLayout()
        self.btn_map_refresh = QPushButton("Redraw map")
        self.btn_map_refresh.setToolTip("Draw the map again around the current location (resets zoom / pan).")
        self.btn_map_refresh.clicked.connect(lambda: (self.map_widget.reset_view(), self.update_map()))
        self.btn_map_save = QPushButton("Save map as PNG...")
        self.btn_map_save.clicked.connect(self._save_map)
        self.btn_map_world = QPushButton("World")
        self.btn_map_world.setToolTip("Show the whole world to choose a place anywhere (then zoom with the mouse wheel).")
        self.btn_map_world.clicked.connect(lambda: (self.map_widget.world_view(), self.update_map()))
        self.chk_snap = QCheckBox("Snap box to grid cells")
        self.chk_snap.setChecked(True)
        self.chk_snap.setToolTip("A box drawn on the map is expanded to whole grid cells of the selected data set.")
        map_btns.addWidget(self.btn_map_refresh)
        map_btns.addWidget(self.btn_map_world)
        map_btns.addWidget(self.chk_snap)
        map_btns.addWidget(self.btn_map_save)
        map_btns.addStretch(1)
        map_lay.addLayout(map_btns)
        hint = QLabel(getattr(DO, "MAP_HINT", ""))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        map_lay.addWidget(hint)
        self.map_widget.set_handlers(self._map_point, self._map_box, self._map_snap)
        self.right_tabs.addTab(map_page, "Map")
        self.preview_widget = PreviewWidget()
        self.preview_widget.btn_import.clicked.connect(self._import_previewed)
        self.right_tabs.addTab(self.preview_widget, "Preview")
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 6)

        # status area
        status = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setMinimumWidth(300)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_task)
        self.btn_export_loc = QPushButton("Export locations...")
        self.btn_export_loc.setToolTip("Export coordinates and attributes of everything downloaded (ITRDB sites, "
                                       "climate points, grid cells, stations) as CSV, GeoJSON, KMZ or Shapefile.")
        self.btn_export_loc.clicked.connect(self._export_locations)
        self.btn_export_loc.setEnabled(GX is not None)
        btn_help = QPushButton("Help")
        btn_help.clicked.connect(self._show_help)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self._close_clicked)
        self._status_layout = status
        self._dock = None
        status.addWidget(self.lbl_status, 2)
        status.addWidget(self.progress, 1)
        status.addWidget(self.btn_cancel)
        status.addWidget(self.btn_export_loc)
        status.addWidget(btn_help)
        status.addWidget(btn_close)
        root.addLayout(status)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(48)
        self.log.setMaximumHeight(110)
        root.addWidget(self.log)

    def _build_itrdb_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        area = QGroupBox("Search area")
        g = QGridLayout(area)
        self.rb_itrdb_point = QRadioButton("Around point")
        self.rb_itrdb_bbox = QRadioButton("Bounding box")
        self.rb_itrdb_world = QRadioButton("Anywhere (use the filters only)")
        self.rb_itrdb_world.setToolTip("World-wide search. A country or region typed in the text filter "
                                       "(e.g. 'Turkey', 'Alps') limits the search to that area.")
        self.rb_itrdb_point.setChecked(True)
        grp = QButtonGroup(w)
        grp.addButton(self.rb_itrdb_point)
        grp.addButton(self.rb_itrdb_bbox)
        grp.addButton(self.rb_itrdb_world)
        self.it_lat = _dspin(-90, 90, 40.28)
        self.it_lon = _dspin(-180, 180, 69.62)
        self.it_radius = _dspin(5, 5000, 300, decimals=0, step=50)
        self.it_radius.setSuffix(" km")
        self.it_min_lat = _dspin(-90, 90, 36.0)
        self.it_max_lat = _dspin(-90, 90, 42.0)
        self.it_min_lon = _dspin(-180, 180, 66.0)
        self.it_max_lon = _dspin(-180, 180, 76.0)
        g.addWidget(self.rb_itrdb_point, 0, 0)
        g.addWidget(QLabel("Lat:"), 0, 1)
        g.addWidget(self.it_lat, 0, 2)
        g.addWidget(QLabel("Lon:"), 0, 3)
        g.addWidget(self.it_lon, 0, 4)
        g.addWidget(QLabel("Radius:"), 0, 5)
        g.addWidget(self.it_radius, 0, 6)
        g.addWidget(self.rb_itrdb_bbox, 1, 0)
        g.addWidget(QLabel("Lat from:"), 1, 1)
        g.addWidget(self.it_min_lat, 1, 2)
        g.addWidget(QLabel("to:"), 1, 3)
        g.addWidget(self.it_max_lat, 1, 4)
        g.addWidget(QLabel("Lon from:"), 2, 1)
        g.addWidget(self.it_min_lon, 2, 2)
        g.addWidget(QLabel("to:"), 2, 3)
        g.addWidget(self.it_max_lon, 2, 4)
        btn_from_cru = QPushButton("Use CRU point")
        btn_from_cru.setToolTip("Copy latitude/longitude from the CRU TS tab")
        btn_from_cru.clicked.connect(self._itrdb_point_from_cru)
        g.addWidget(btn_from_cru, 2, 6)
        g.addWidget(self.rb_itrdb_world, 3, 0, 1, 4)
        lay.addWidget(area)

        filt = QGroupBox("Filters (optional)")
        f = QGridLayout(filt)
        self.it_species = QLineEdit()
        self.it_species.setPlaceholderText("code or name, e.g. JUTU, PCAB, juniper, Pinus")
        self.it_species.setToolTip("4-letter ITRDB species codes (several separated by commas) or a genus / "
                                   "common tree name (juniper, pine, spruce, oak, Larix ...)")
        self.it_year_from = _year_spin(0, allow_any=True)
        self.it_year_to = _year_spin(0, allow_any=True)
        self.it_year_from.setToolTip("Data must overlap this period (0 = any)")
        self.it_text = QLineEdit()
        self.it_text.setPlaceholderText("free text, e.g. 'Juniper Turkey', 'PCAB Alps', 'Esper Kyrgyzstan', site name")
        self.it_text.setToolTip("Tree names (juniper, Pinus), species codes in capitals (JUTU), countries or regions "
                                "(Turkey, Tien Shan, Alps, Siberia) and any other words (site, investigator). "
                                "A country or region replaces the search area; without area the search is world-wide.")
        self.it_text.returnPressed.connect(lambda: self.search_itrdb())
        f.addWidget(QLabel("Species:"), 0, 0)
        f.addWidget(self.it_species, 0, 1)
        f.addWidget(QLabel("Years from:"), 0, 2)
        f.addWidget(self.it_year_from, 0, 3)
        f.addWidget(QLabel("to:"), 0, 4)
        f.addWidget(self.it_year_to, 0, 5)
        f.addWidget(QLabel("Text:"), 1, 0)
        f.addWidget(self.it_text, 1, 1, 1, 5)
        self.lbl_itrdb_query = QLabel("")
        self.lbl_itrdb_query.setStyleSheet("color: #555;")
        f.addWidget(self.lbl_itrdb_query, 2, 1, 1, 5)
        lay.addWidget(filt)

        row = QHBoxLayout()
        self.btn_itrdb_search = QPushButton("Search ITRDB")
        self.btn_itrdb_search.setDefault(True)
        self.btn_itrdb_search.clicked.connect(self.search_itrdb)
        self.lbl_itrdb_count = QLabel("")
        row.addWidget(self.btn_itrdb_search)
        row.addWidget(self.lbl_itrdb_count, 1)
        lay.addLayout(row)

        self.tbl_sites = QTableWidget(0, 10)
        self.tbl_sites.setHorizontalHeaderLabels(
            ["Code", "Site", "Species", "Lat", "Lon", "Elev (m)", "First", "Last", "Dist (km)", "Files"])
        self.tbl_sites.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sites.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl_sites.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_sites.setSortingEnabled(True)
        self.tbl_sites.verticalHeader().setVisible(False)
        hh = self.tbl_sites.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_sites.itemSelectionChanged.connect(self._on_site_selection)
        lay.addWidget(self.tbl_sites, 3)

        files_box = QGroupBox("Files of the selected site")
        fl = QVBoxLayout(files_box)
        quick = QHBoxLayout()
        for label, fn in (("Std RW chronology", self._check_std_rw),
                          ("All chronologies", self._check_all_crn),
                          ("Raw .rwl", self._check_rwl),
                          ("None", self._check_none)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            quick.addWidget(b)
        quick.addStretch(1)
        self.btn_preview = QPushButton("Preview...")
        self.btn_preview.setToolTip("Look at the selected .rwl / .crn before downloading (double-click works too)")
        self.btn_preview.clicked.connect(self.preview_selected_file)
        quick.addWidget(self.btn_preview)
        fl.addLayout(quick)
        self.lst_files = QListWidget()
        self.lst_files.itemDoubleClicked.connect(lambda item: self.preview_selected_file())
        self.lst_files.currentItemChanged.connect(self._on_file_current_changed)
        fl.addWidget(self.lst_files, 1)
        lay.addWidget(files_box, 2)

        dl = QGroupBox("Download / import")
        d = QGridLayout(dl)
        self.it_dir = QLineEdit()
        btn_dir = QPushButton("Browse...")
        btn_dir.clicked.connect(self._choose_itrdb_dir)
        self.chk_import_crn = QCheckBox("Import checked chronologies (.crn) as datasets")
        self.chk_import_crn.setChecked(True)
        self.it_min_depth = QSpinBox()
        self.it_min_depth.setRange(1, 200)
        self.it_min_depth.setValue(1)
        self.it_min_depth.setToolTip("Years with fewer series (sample depth) are removed on import")
        self.btn_itrdb_download = QPushButton("Download checked files")
        self.btn_itrdb_download.clicked.connect(self.download_itrdb)
        self.btn_batch = QPushButton("Batch download...")
        self.btn_batch.setToolTip("Download files from many sites at once by criteria")
        self.btn_batch.clicked.connect(self.batch_download)
        self.btn_send_cru = QPushButton("Send selected sites to CRU")
        self.btn_send_cru.clicked.connect(self._send_sites_to_cru)
        self.btn_study = QPushButton("Open study page")
        self.btn_study.clicked.connect(self._open_study_page)
        d.addWidget(QLabel("Save files to:"), 0, 0)
        d.addWidget(self.it_dir, 0, 1, 1, 3)
        d.addWidget(btn_dir, 0, 4)
        d.addWidget(self.chk_import_crn, 1, 0, 1, 2)
        d.addWidget(QLabel("Minimum sample depth:"), 1, 2)
        d.addWidget(self.it_min_depth, 1, 3)
        d.addWidget(self.btn_itrdb_download, 2, 0)
        d.addWidget(self.btn_batch, 2, 1)
        d.addWidget(self.btn_send_cru, 2, 2, 1, 2)
        d.addWidget(self.btn_study, 2, 4)
        lay.addWidget(dl)

        for wdg in (self.it_lat, self.it_lon, self.it_radius, self.it_min_lat, self.it_max_lat,
                    self.it_min_lon, self.it_max_lon):
            wdg.valueChanged.connect(self._schedule_map)
        self.rb_itrdb_point.toggled.connect(self._update_itrdb_enabled)
        self.rb_itrdb_point.toggled.connect(self._schedule_map)
        self.rb_itrdb_world.toggled.connect(self._update_itrdb_enabled)
        self._update_itrdb_enabled()

        self._busy_widgets += [self.btn_itrdb_search, self.btn_itrdb_download, self.btn_send_cru, self.btn_preview,
                               self.btn_batch]
        return w

    def _build_cru_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        top = QGroupBox("Dataset")
        t = QGridLayout(top)
        self.cmb_version = QComboBox()
        self.cmb_version.addItems(sorted(DO.KNOWN_CRU_RELEASES, key=DO._version_key, reverse=True))
        self.btn_versions = QPushButton("Check versions online")
        self.btn_versions.clicked.connect(self.load_versions)
        t.addWidget(QLabel("CRU TS version:"), 0, 0)
        t.addWidget(self.cmb_version, 0, 1)
        t.addWidget(self.btn_versions, 0, 2)
        var_box = QWidget()
        vg = QGridLayout(var_box)
        vg.setContentsMargins(0, 0, 0, 0)
        self.var_checks: Dict[str, QCheckBox] = {}
        for k, (code, var) in enumerate(DO.CRU_VARIABLES.items()):
            cb = QCheckBox(f"{code} - {var.label} ({var.units})")
            cb.setChecked(code in ("tmp", "pre"))
            self.var_checks[code] = cb
            vg.addWidget(cb, k // 2, k % 2)
        t.addWidget(QLabel("Variables:"), 1, 0, Qt.AlignTop)
        t.addWidget(var_box, 1, 1, 1, 2)
        lay.addWidget(top)

        loc = QGroupBox("Location")
        lg = QGridLayout(loc)
        self.cru_name = QLineEdit("site")
        self.cru_lat = _dspin(-90, 90, 40.28)
        self.cru_lon = _dspin(-180, 180, 69.62)
        self.cmb_mode = QComboBox()
        for key, label in MODE_LABELS:
            self.cmb_mode.addItem(label, key)
        self.cru_min_lat = _dspin(-90, 90, 39.5)
        self.cru_max_lat = _dspin(-90, 90, 41.0)
        self.cru_min_lon = _dspin(-180, 180, 69.0)
        self.cru_max_lon = _dspin(-180, 180, 70.5)
        btn_bbox_auto = QPushButton("Box +-0.5 deg around point")
        btn_bbox_auto.clicked.connect(self._bbox_around_point)
        lg.addWidget(QLabel("Name:"), 0, 0)
        lg.addWidget(self.cru_name, 0, 1)
        lg.addWidget(QLabel("Lat (N+):"), 0, 2)
        lg.addWidget(self.cru_lat, 0, 3)
        lg.addWidget(QLabel("Lon (E+):"), 0, 4)
        lg.addWidget(self.cru_lon, 0, 5)
        lg.addWidget(QLabel("Method:"), 1, 0)
        lg.addWidget(self.cmb_mode, 1, 1, 1, 3)
        lg.addWidget(btn_bbox_auto, 1, 4, 1, 2)
        lg.addWidget(QLabel("Box lat:"), 2, 0)
        lg.addWidget(self.cru_min_lat, 2, 1)
        lg.addWidget(self.cru_max_lat, 2, 2)
        lg.addWidget(QLabel("Box lon:"), 2, 3)
        lg.addWidget(self.cru_min_lon, 2, 4)
        lg.addWidget(self.cru_max_lon, 2, 5)
        self._bbox_widgets = [self.cru_min_lat, self.cru_max_lat, self.cru_min_lon, self.cru_max_lon, btn_bbox_auto]
        lay.addWidget(loc)

        tg = QGroupBox("Targets (several points are extracted in one download)")
        tl = QVBoxLayout(tg)
        self.tbl_targets = QTableWidget(0, 5)
        self.tbl_targets.setHorizontalHeaderLabels(["Name", "Lat", "Lon", "Method", "Box"])
        self.tbl_targets.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_targets.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.tbl_targets.verticalHeader().setVisible(False)
        self.tbl_targets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_targets.horizontalHeader().setStretchLastSection(True)
        self.tbl_targets.itemSelectionChanged.connect(self._on_target_selection)
        tl.addWidget(self.tbl_targets)
        tb = QHBoxLayout()
        b_add = QPushButton("Add current location")
        b_add.clicked.connect(self._add_current_target)
        b_rem = QPushButton("Remove selected")
        b_rem.clicked.connect(self._remove_targets)
        b_clr = QPushButton("Clear")
        b_clr.clicked.connect(lambda: (self.tbl_targets.setRowCount(0), self._schedule_map()))
        self.btn_chron_targets = QPushButton("Add chronology locations")
        self.btn_chron_targets.setToolTip("Add one target for every chronology with known coordinates (ITRDB sites, "
                                          "chronologies from Detrend Advisor). Climate is then downloaded for each "
                                          "chronology location separately.")
        self.btn_chron_targets.clicked.connect(self.add_chronology_targets)
        self.btn_chron_targets.setEnabled(False)
        tb.addWidget(self.btn_chron_targets)
        tb.addWidget(b_add)
        tb.addWidget(b_rem)
        tb.addWidget(b_clr)
        tb.addStretch(1)
        tl.addLayout(tb)
        hint = QLabel("If the list is empty, the location above is used.")
        hint.setStyleSheet("color: gray;")
        tl.addWidget(hint)
        lay.addWidget(tg, 1)

        per = QGroupBox("Period and storage")
        pf = QGridLayout(per)
        self.cru_start = _year_spin(1901)
        self.cru_end = _year_spin(2025)
        self.cru_start.setRange(1901, 2100)
        self.cru_end.setRange(1901, 2100)
        self.cru_cache = QLineEdit()
        btn_cache = QPushButton("Browse...")
        btn_cache.clicked.connect(self._choose_cache_dir)
        self.chk_delete = QCheckBox("Delete downloaded files after extraction (temporary download)")
        self.chk_delete.setChecked(True)
        self.lbl_estimate = QLabel("")
        pf.addWidget(QLabel("Years:"), 0, 0)
        pf.addWidget(self.cru_start, 0, 1)
        pf.addWidget(QLabel("to"), 0, 2)
        pf.addWidget(self.cru_end, 0, 3)
        pf.addWidget(QLabel("Download folder:"), 1, 0)
        pf.addWidget(self.cru_cache, 1, 1, 1, 3)
        pf.addWidget(btn_cache, 1, 4)
        pf.addWidget(self.chk_delete, 2, 0, 1, 5)
        pf.addWidget(self.lbl_estimate, 3, 0, 1, 5)
        lay.addWidget(per)

        run = QHBoxLayout()
        self.btn_cru_estimate = QPushButton("Estimate download")
        self.btn_cru_estimate.clicked.connect(lambda: self.run_cru(estimate_only=True))
        self.btn_cru_run = QPushButton("Download && extract")
        self.btn_cru_run.clicked.connect(lambda: self.run_cru(estimate_only=False))
        run.addWidget(self.btn_cru_estimate)
        run.addWidget(self.btn_cru_run)
        run.addStretch(1)
        lay.addLayout(run)

        for wdg in (self.cru_lat, self.cru_lon, self.cru_min_lat, self.cru_max_lat, self.cru_min_lon, self.cru_max_lon):
            wdg.valueChanged.connect(self._schedule_map)
        self.cmb_mode.currentIndexChanged.connect(self._update_cru_enabled)
        self.cmb_mode.currentIndexChanged.connect(self._schedule_map)
        self._update_cru_enabled()
        self._busy_widgets += [self.btn_versions, self.btn_cru_estimate, self.btn_cru_run]
        return w

    # ------------------------------------------------------------ settings
    def _apply_settings(self) -> None:
        s = self._settings

        def setv(widget, key):
            if key in s:
                try:
                    widget.setValue(type(widget.value())(s[key]))
                except Exception:
                    pass

        setv(self.it_lat, "itrdb_lat")
        setv(self.it_lon, "itrdb_lon")
        setv(self.it_radius, "itrdb_radius")
        setv(self.it_min_lat, "itrdb_min_lat")
        setv(self.it_max_lat, "itrdb_max_lat")
        setv(self.it_min_lon, "itrdb_min_lon")
        setv(self.it_max_lon, "itrdb_max_lon")
        setv(self.it_min_depth, "itrdb_min_depth")
        if s.get("itrdb_use_bbox"):
            self.rb_itrdb_bbox.setChecked(True)
        if s.get("itrdb_world"):
            self.rb_itrdb_world.setChecked(True)
        self.it_text.setText(str(s.get("itrdb_text", "")))
        self.it_species.setText(str(s.get("itrdb_species", "")))
        self.it_dir.setText(str(s.get("itrdb_dir") or os.path.join(DO.default_cache_dir(), "itrdb")))
        setv(self.cru_lat, "cru_lat")
        setv(self.cru_lon, "cru_lon")
        setv(self.cru_min_lat, "cru_min_lat")
        setv(self.cru_max_lat, "cru_max_lat")
        setv(self.cru_min_lon, "cru_min_lon")
        setv(self.cru_max_lon, "cru_max_lon")
        setv(self.cru_start, "cru_start")
        setv(self.cru_end, "cru_end")
        if "cru_name" in s:
            self.cru_name.setText(str(s["cru_name"]))
        mode = s.get("cru_mode")
        for i in range(self.cmb_mode.count()):
            if self.cmb_mode.itemData(i) == mode:
                self.cmb_mode.setCurrentIndex(i)
        if isinstance(s.get("cru_vars"), list):
            for code, cb in self.var_checks.items():
                cb.setChecked(code in s["cru_vars"])
        versions = s.get("cru_versions")
        if isinstance(versions, list) and versions:
            self._set_versions(versions)
        if s.get("cru_version"):
            idx = self.cmb_version.findText(str(s["cru_version"]))
            if idx >= 0:
                self.cmb_version.setCurrentIndex(idx)
        self.cru_cache.setText(str(s.get("cru_cache") or DO.default_cache_dir()))
        self.chk_delete.setChecked(bool(s.get("cru_delete", True)))
        if self.sources_tab is not None:
            self.sources_tab.apply_settings(s)
        tab = s.get("tab")
        if isinstance(tab, int) and 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)

    def get_settings(self) -> Dict[str, Any]:
        s = dict(self._settings)
        s.update({
            "itrdb_lat": self.it_lat.value(), "itrdb_lon": self.it_lon.value(),
            "itrdb_radius": self.it_radius.value(),
            "itrdb_min_lat": self.it_min_lat.value(), "itrdb_max_lat": self.it_max_lat.value(),
            "itrdb_min_lon": self.it_min_lon.value(), "itrdb_max_lon": self.it_max_lon.value(),
            "itrdb_use_bbox": self.rb_itrdb_bbox.isChecked(),
            "itrdb_world": self.rb_itrdb_world.isChecked(),
            "itrdb_text": self.it_text.text().strip(),
            "itrdb_species": self.it_species.text().strip(),
            "itrdb_dir": self.it_dir.text().strip(),
            "itrdb_min_depth": self.it_min_depth.value(),
            "cru_version": self.cmb_version.currentText(),
            "cru_versions": [self.cmb_version.itemText(i) for i in range(self.cmb_version.count())],
            "cru_vars": [c for c, cb in self.var_checks.items() if cb.isChecked()],
            "cru_name": self.cru_name.text().strip(),
            "cru_lat": self.cru_lat.value(), "cru_lon": self.cru_lon.value(),
            "cru_mode": self.cmb_mode.currentData(),
            "cru_min_lat": self.cru_min_lat.value(), "cru_max_lat": self.cru_max_lat.value(),
            "cru_min_lon": self.cru_min_lon.value(), "cru_max_lon": self.cru_max_lon.value(),
            "cru_start": self.cru_start.value(), "cru_end": self.cru_end.value(),
            "cru_cache": self.cru_cache.text().strip(),
            "cru_delete": self.chk_delete.isChecked(),
            "tab": self.tabs.currentIndex(),
        })
        if self.sources_tab is not None:
            s.update(self.sources_tab.get_settings())
        return s

    # ------------------------------------------------------------ helpers
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        for wdg in self._busy_widgets:
            wdg.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        if message:
            self.lbl_status.setText(message)

    def _run_task(self, fn: Callable[[DO.ProgressCallback, DO.CancelCheck], Any],
                  on_done: Callable[[Any], None], start_message: str) -> bool:
        if self._thread is not None:
            QMessageBox.information(self, "Busy", "Another operation is still running.")
            return False
        # forget threads that have already stopped
        self._retired = [(t, w) for (t, w) in self._retired if _thread_alive(t)]
        self._cancel_event = threading.Event()
        thread = QThread(self)
        worker = _TaskWorker(fn, self._cancel_event)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Bound methods of this QObject -> queued connections, executed in the GUI thread.
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._task_finished)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        self._pending_on_done = on_done
        self._set_busy(True, start_message)
        self._log(start_message)
        thread.start()
        return True

    def _detach_thread(self) -> None:
        """Release the 'busy' slot; the QThread finishes and deletes itself."""
        if self._thread is not None:
            self._retired.append((self._thread, self._worker))
        self._thread = None
        self._worker = None

    def _on_progress(self, stage: str, done: int, total: int, message: str) -> None:
        hook = getattr(self, "_progress_hook", None)
        if hook is not None:
            try:
                if hook(stage, done, total, message):
                    return
            except RuntimeError:  # the progress window was closed
                self._progress_hook = None
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(100.0 * done / total))
        else:
            self.progress.setRange(0, 0)
        if message:
            self.lbl_status.setText(message)
            if stage != "download" or message.startswith(("Downloaded", "Using cached", "Retrying")):
                last = self.log.toPlainText().rsplit("\n", 1)[-1]
                if message != last:
                    self._log(message)

    def _task_finished(self, result: Any) -> None:
        on_done = self._pending_on_done
        self._pending_on_done = None
        self._silent_fail = False
        self._detach_thread()
        self._set_busy(False, "Done.")
        self._update_export_enabled()
        if on_done is None:
            return
        try:
            on_done(result)
        except Exception as exc:
            self._log(traceback.format_exc())
            QMessageBox.critical(self, "Error", str(exc))

    def _clear_progress_hook(self) -> None:
        self._progress_hook = None
        close = getattr(self, "_progress_hook_close", None)
        self._progress_hook_close = None
        if close is not None:
            try:
                close()
            except RuntimeError:
                pass

    def _task_failed(self, message: str) -> None:
        self._clear_progress_hook()
        self._pending_on_done = None
        self._detach_thread()
        self._set_busy(False, "Failed." if "Cancelled" not in message else "Cancelled.")
        self._log(message)
        silent, self._silent_fail = self._silent_fail, False
        if "Cancelled" not in message and not silent:
            QMessageBox.warning(self, "Online data", message.split("\n\n")[0])

    def _cancel_task(self) -> None:
        self._cancel_event.set()
        self.lbl_status.setText("Cancelling...")

    def _update_export_enabled(self) -> None:
        self.btn_export_loc.setEnabled(GX is not None and GX.registry.count() > 0)

    def _export_locations(self) -> None:
        if SQ is None:
            QMessageBox.warning(self, "Export locations", "Modules dendro_geoexport.py / dendro_sources_qt.py are "
                                "missing.\n\n" + _SOURCES_ERROR)
            return
        if GX.registry.count() == 0:
            QMessageBox.information(self, "Export locations", "Nothing has been downloaded yet.")
            return
        SQ.ExportLocationsDialog(self, default_dir=self.it_dir.text().strip()).exec()
        self._update_export_enabled()

    def _show_help(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Online data - help")
        box.setTextFormat(Qt.RichText)
        box.setText(HELP_TEXT)
        box.exec()

    def _schedule_map(self, *args: Any) -> None:
        # anything that changes the location (site, coordinates, method) -> show the Map tab
        if hasattr(self, "right_tabs"):
            self.right_tabs.setCurrentIndex(0)
        self._map_timer.start()

    def _on_tab_changed(self, index: int) -> None:
        if index == 1 and not self._versions_loaded and self._thread is None:
            self.load_versions(silent=True)
        self._schedule_map()

    # ------------------------------------------------------------ map picking
    def _map_snap(self) -> float:
        if not self.chk_snap.isChecked():
            return 0.0
        tab = self.tabs.currentIndex()
        if tab == 1:
            return DO.CRU_GRID_RES
        if tab == 2 and self.sources_tab is not None:
            return float(self.sources_tab._cell_res() or 0.0)
        return 0.0

    def _site_near(self, lat: float, lon: float) -> Optional[int]:
        ax = getattr(self.map_widget.figure, "_dendro_main_ax", None)
        if ax is None or not self._sites:
            return None
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        tol = 0.015 * max(abs(x1 - x0), abs(y1 - y0))
        best, best_d = None, tol
        for r in range(self.tbl_sites.rowCount()):
            it = self.tbl_sites.item(r, 0)
            st = it.data(Qt.UserRole) if it is not None else None
            if st is None or st.lat is None or st.lon is None:
                continue
            d = max(abs(st.lat - lat), abs(st.lon - lon))
            if d <= best_d:
                best, best_d = r, d
        return best

    def _map_point(self, lat: float, lon: float, add: bool = False) -> None:
        tab = self.tabs.currentIndex()
        if tab == 0:
            row = self._site_near(lat, lon)
            if row is not None:
                self.tbl_sites.selectRow(row)
                self.tbl_sites.scrollToItem(self.tbl_sites.item(row, 0))
                return
            self.tbl_sites.clearSelection()
            self.rb_itrdb_point.setChecked(True)
            self.it_lat.setValue(lat)
            self.it_lon.setValue(lon)
            self._log(f"Map: search point {lat:.4f} N, {lon:.4f} E")
        elif tab == 1:
            self.cru_lat.setValue(lat)
            self.cru_lon.setValue(lon)
            if add:
                n = self.tbl_targets.rowCount() + 1
                mode = self.cmb_mode.currentData()
                bbox = None
                if mode == "bbox_mean":
                    bbox = (lat - 0.25, lat + 0.25, lon - 0.25, lon + 0.25)
                self._add_target(DO.ExtractionTarget(f"pt{n}", lat, lon, mode, bbox))
                self._log(f"Map: target pt{n} added at {lat:.4f} N, {lon:.4f} E")
        elif self.sources_tab is not None:
            self.sources_tab.set_point(lat, lon, add)
        self._schedule_map()

    def _map_box(self, s: float, n: float, w: float, e: float) -> None:
        tab = self.tabs.currentIndex()
        if tab == 0:
            self.rb_itrdb_bbox.setChecked(True)
            self.it_min_lat.setValue(s)
            self.it_max_lat.setValue(n)
            self.it_min_lon.setValue(w)
            self.it_max_lon.setValue(e)
        elif tab == 1:
            idx = self.cmb_mode.findData("bbox_mean")
            if idx >= 0:
                self.cmb_mode.setCurrentIndex(idx)
            self.cru_min_lat.setValue(s)
            self.cru_max_lat.setValue(n)
            self.cru_min_lon.setValue(w)
            self.cru_max_lon.setValue(e)
            self.cru_lat.setValue(round((s + n) / 2, 5))
            self.cru_lon.setValue(round((w + e) / 2, 5))
        elif self.sources_tab is not None:
            self.sources_tab.set_box(s, n, w, e)
        self._log(f"Map: box {s:.4f}..{n:.4f} N, {w:.4f}..{e:.4f} E")
        self._schedule_map()

    def _save_map(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save map", "location_map.png", "PNG (*.png)")
        if path:
            self.map_widget.save_png(path)
            self._log(f"Map saved: {path}")

    # ------------------------------------------------------------ map
    def update_map(self) -> None:
        if self.tabs.currentIndex() == 0:
            self._update_itrdb_map()
        elif self.tabs.currentIndex() == 1:
            self._update_cru_map()
        elif self.sources_tab is not None:
            self.sources_tab.update_map()

    def _itrdb_area(self):
        if self.rb_itrdb_point.isChecked():
            return DO.bbox_around(self.it_lat.value(), self.it_lon.value(), self.it_radius.value())
        return (min(self.it_min_lat.value(), self.it_max_lat.value()), max(self.it_min_lat.value(), self.it_max_lat.value()),
                min(self.it_min_lon.value(), self.it_max_lon.value()), max(self.it_min_lon.value(), self.it_max_lon.value()))

    def _update_itrdb_map(self) -> None:
        sites = [(s.lat, s.lon, s.site_code) for s in self._sites if s.lat is not None and s.lon is not None]
        sel = self._selected_sites()
        if sel and sel[0].lat is not None:
            lat, lon = sel[0].lat, sel[0].lon
            title = f"{sel[0].site_code} {sel[0].site_name} | {lat:.3f} N, {lon:.3f} E"
        elif self.rb_itrdb_point.isChecked():
            lat, lon = self.it_lat.value(), self.it_lon.value()
            title = None
        else:
            a = self._itrdb_area()
            lat, lon = (a[0] + a[1]) / 2, (a[2] + a[3]) / 2
            title = None
        bbox = self._itrdb_area()
        span = 30.0
        if self.rb_itrdb_world.isChecked():
            if sites:
                la = [x[0] for x in sites]
                lo = [x[1] for x in sites]
                bbox = (max(-89.0, min(la) - 1.0), min(89.0, max(la) + 1.0),
                        max(-180.0, min(lo) - 1.0), min(180.0, max(lo) + 1.0))
            else:
                bbox = (-60.0, 80.0, -180.0, 180.0)
            if not (sel and sel[0].lat is not None):
                lat, lon = (bbox[0] + bbox[1]) / 2, (bbox[2] + bbox[3]) / 2
            span = 360.0
        self.map_widget.draw_map(lat=lat, lon=lon, bbox=bbox, sites=sites, title=title,
                                 max_span_deg=span, zoom=False, grid_res=0.0)

    def _current_target(self, name: Optional[str] = None) -> DO.ExtractionTarget:
        mode = self.cmb_mode.currentData()
        bbox = None
        if mode == "bbox_mean":
            bbox = (min(self.cru_min_lat.value(), self.cru_max_lat.value()),
                    max(self.cru_min_lat.value(), self.cru_max_lat.value()),
                    min(self.cru_min_lon.value(), self.cru_max_lon.value()),
                    max(self.cru_min_lon.value(), self.cru_max_lon.value()))
        return DO.ExtractionTarget(name or (self.cru_name.text().strip() or "site"),
                                   self.cru_lat.value(), self.cru_lon.value(), mode, bbox)

    def _targets_from_table(self) -> List[DO.ExtractionTarget]:
        out: List[DO.ExtractionTarget] = []
        for r in range(self.tbl_targets.rowCount()):
            item0 = self.tbl_targets.item(r, 0)
            t = item0.data(Qt.UserRole) if item0 is not None else None
            if isinstance(t, DO.ExtractionTarget):
                t = DO.ExtractionTarget(item0.text().strip() or t.name, t.lat, t.lon, t.mode, t.bbox)
                out.append(t)
        return out

    def _update_cru_map(self) -> None:
        targets = self._targets_from_table()
        rows = self.tbl_targets.selectionModel().selectedRows() if self.tbl_targets.selectionModel() else []
        main = targets[rows[0].row()] if (targets and rows and rows[0].row() < len(targets)) \
            else self._current_target()
        try:
            cells = self._cru_last_cells.get(self._target_key(main)) or DO.cells_for_target(main)
        except ValueError as exc:
            self.lbl_status.setText(str(exc))
            cells = []
        others = [(t.lat, t.lon, t.name) for t in targets if self._target_key(t) != self._target_key(main)]
        self.map_widget.draw_map(lat=main.lat, lon=main.lon, cells=cells, bbox=main.bbox, sites=others)
        if cells and main.mode != "bbox_mean":
            desc = ", ".join(f"({c.lat:.2f}, {c.lon:.2f}) w={c.weight:.2f}" for c in cells)
            self.lbl_estimate.setText(f"Cells used: {desc}")
        elif cells:
            self.lbl_estimate.setText(f"Cells in box: {len(cells)}")

    @staticmethod
    def _target_key(t: DO.ExtractionTarget) -> str:
        return f"{t.lat:.4f}|{t.lon:.4f}|{t.mode}|{t.bbox}"

    # ------------------------------------------------------------ ITRDB
    def _update_itrdb_enabled(self) -> None:
        point = self.rb_itrdb_point.isChecked()
        box = self.rb_itrdb_bbox.isChecked()
        for wdg in (self.it_lat, self.it_lon, self.it_radius):
            wdg.setEnabled(point)
        for wdg in (self.it_min_lat, self.it_max_lat, self.it_min_lon, self.it_max_lon):
            wdg.setEnabled(box)

    def _itrdb_point_from_cru(self) -> None:
        self.it_lat.setValue(self.cru_lat.value())
        self.it_lon.setValue(self.cru_lon.value())
        self.rb_itrdb_point.setChecked(True)

    def _choose_itrdb_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for ITRDB files", self.it_dir.text())
        if d:
            self.it_dir.setText(d)

    def search_itrdb(self) -> None:
        text = self.it_text.text().strip()
        if text and not self.rb_itrdb_world.isChecked():
            # free text searches the whole world (or the country / region named in the text)
            self.rb_itrdb_world.setChecked(True)
        world = self.rb_itrdb_world.isChecked()
        area = None if world else self._itrdb_area()
        species = self.it_species.text().strip()
        parsed = DO.parse_itrdb_text(text, species)
        area_from_text = parsed.bbox() is not None
        y0 = self.it_year_from.value() or None
        y1 = self.it_year_to.value() or None
        center = (self.it_lat.value(), self.it_lon.value()) if (self.rb_itrdb_point.isChecked()
                                                                and not area_from_text) else None
        radius = self.it_radius.value() if center is not None else None

        def task(progress, cancel):
            progress("search", 0, 0, "Searching ITRDB ...")
            sites, desc = DO.smart_search_itrdb(text=text, species=species, area=area,
                                                earliest_year=y0, latest_year=y1)
            if center is not None:
                sites = [s for s in sites if s.lat is None or
                         DO.haversine_km(center[0], center[1], s.lat, s.lon) <= radius * 1.001]
            self._itrdb_last_query = desc
            return sites, center

        self._run_task(task, self._on_itrdb_results, "Searching ITRDB ...")

    def _on_itrdb_results(self, result: Any) -> None:
        sites, center = result
        self._sites = list(sites)
        self.tbl_sites.setSortingEnabled(False)
        self.tbl_sites.setRowCount(0)
        for s in self._sites:
            r = self.tbl_sites.rowCount()
            self.tbl_sites.insertRow(r)
            dist = (DO.haversine_km(center[0], center[1], s.lat, s.lon)
                    if center is not None and s.lat is not None else None)
            n_crn = len(s.files_of_kind("crn"))
            n_rwl = len(s.files_of_kind("rwl"))
            values: List[Any] = [s.site_code, s.site_name, s.species, s.lat, s.lon, s.elevation_m,
                                 s.first_year, s.last_year, None if dist is None else round(dist, 1),
                                 f"crn {n_crn}, rwl {n_rwl}"]
            for c, v in enumerate(values):
                item = QTableWidgetItem()
                if isinstance(v, (int, float)) and v is not None:
                    item.setData(Qt.DisplayRole, float(v) if isinstance(v, float) else int(v))
                else:
                    item.setText("" if v is None else str(v))
                if c == 0:
                    item.setData(Qt.UserRole, s)
                    item.setToolTip(f"{s.study_name}\n{s.investigators}\nDOI: {s.doi}")
                self.tbl_sites.setItem(r, c, item)
        self.tbl_sites.setSortingEnabled(True)
        if center is not None:
            self.tbl_sites.sortItems(8, Qt.AscendingOrder)
        self.lbl_itrdb_count.setText(f"{len(self._sites)} site(s) with tree-ring files")
        self.lbl_itrdb_query.setText("Searched: " + str(getattr(self, "_itrdb_last_query", "")))
        self._log(f"ITRDB: {len(self._sites)} site(s) found")
        self.lst_files.clear()
        self.update_map()

    def _selected_sites(self) -> List[DO.ItrdbSite]:
        out: List[DO.ItrdbSite] = []
        sm = self.tbl_sites.selectionModel()
        if sm is None:
            return out
        for idx in sm.selectedRows(0):
            item = self.tbl_sites.item(idx.row(), 0)
            s = item.data(Qt.UserRole) if item is not None else None
            if isinstance(s, DO.ItrdbSite):
                out.append(s)
        return out

    def _on_site_selection(self) -> None:
        sel = self._selected_sites()
        self._populating_files = True
        self.lst_files.clear()
        order = {"crn": 0, "rwl": 1, "crn-noaa": 2, "rwl-noaa": 3, "stats": 4, "other": 5}
        for s in sel:
            for f in sorted(s.files, key=lambda f: (order.get(f.kind, 9), f.parameter != "Total ring width",
                                                     f.variant != "standard", f.filename)):
                variant = _VARIANT_SHORT.get(f.variant, f.variant)
                text = f"{s.site_code} | {f.kind:<8} | {f.parameter:<18} | {variant:<3} | {f.filename}"
                item = QListWidgetItem(text)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                item.setData(Qt.UserRole, (s, f))
                if f.description:
                    item.setToolTip(f.description)
                self.lst_files.addItem(item)
        self._check_std_rw()
        self._populating_files = False
        self._schedule_map()

    def _iter_file_items(self):
        for i in range(self.lst_files.count()):
            item = self.lst_files.item(i)
            yield item, item.data(Qt.UserRole)

    def _check_where(self, pred: Callable[[DO.ItrdbFile], bool]) -> None:
        for item, (s, f) in self._iter_file_items():
            item.setCheckState(Qt.Checked if pred(f) else Qt.Unchecked)

    def _check_std_rw(self) -> None:
        # per site: standard total-width chronology if present, else the first standard one
        chosen = set()
        by_site: Dict[str, List[DO.ItrdbFile]] = {}
        for _, (s, f) in self._iter_file_items():
            by_site.setdefault(s.study_id, []).append(f)
        for files in by_site.values():
            crn = [f for f in files if f.kind == "crn"]
            pick = ([f for f in crn if f.variant == "standard" and f.parameter == "Total ring width"]
                    or [f for f in crn if f.variant == "standard"])
            if pick:
                chosen.add(pick[0].url)
        self._check_where(lambda f: f.url in chosen)

    def _check_all_crn(self) -> None:
        self._check_where(lambda f: f.kind == "crn")

    def _check_rwl(self) -> None:
        self._check_where(lambda f: f.kind == "rwl")

    def _check_none(self) -> None:
        self._check_where(lambda f: False)

    def _open_study_page(self) -> None:
        for s in self._selected_sites()[:5]:
            QDesktopServices.openUrl(QUrl(s.study_url))

    def _send_sites_to_cru(self) -> None:
        sel = [s for s in self._selected_sites() if s.lat is not None and s.lon is not None]
        if not sel:
            QMessageBox.information(self, "CRU", "Select one or more sites with coordinates.")
            return
        mode = self.cmb_mode.currentData()
        if mode == "bbox_mean":
            mode = "bilinear"
        for s in sel:
            self._add_target(DO.ExtractionTarget(s.site_code or s.site_name or "site", s.lat, s.lon, mode, None))
        self.cru_lat.setValue(sel[0].lat)
        self.cru_lon.setValue(sel[0].lon)
        self.cru_name.setText(sel[0].site_code or "site")
        self.tabs.setCurrentIndex(1)
        self._log(f"{len(sel)} site(s) added as CRU targets")

    def _on_file_current_changed(self, current: Any = None, *args: Any) -> None:
        # selecting a file -> switch to the Preview tab and load it (debounced)
        if current is not None and not self._populating_files:
            self.right_tabs.setCurrentWidget(self.preview_widget)
            self._preview_timer.start()

    def preview_selected_file(self, quiet: bool = False) -> None:
        item = self.lst_files.currentItem()
        if item is None:
            if not quiet:
                QMessageBox.information(self, "Preview", "Select a file in the list first.")
            return
        site, f = item.data(Qt.UserRole)
        self.right_tabs.setCurrentWidget(self.preview_widget)
        if f.kind not in ("rwl", "crn"):
            self.preview_widget.show_message(f"{f.filename}\n\nPreview is available for .rwl and .crn files.")
            return
        cached = self._preview_cache.get(f.url)
        if cached is not None:
            self.preview_widget.show_preview(cached, site, f)
            return
        if self._thread is not None:
            return  # busy: the user can press Preview again

        def task(progress, cancel):
            progress("preview", 0, 0, f"Loading {f.filename} for preview ...")
            return DO.preview_itrdb_file(f, site)

        def done(pv) -> None:
            self._preview_cache[f.url] = pv
            self.preview_widget.show_preview(pv, site, f)

        self.preview_widget.show_message(f"Loading {f.filename} ...")
        self._run_task(task, done, f"Loading {f.filename} for preview ...")

    def _import_previewed(self) -> None:
        if self.preview_widget.current is not None:
            self._download_files([self.preview_widget.current])

    def batch_download(self) -> None:
        if not self._sites:
            QMessageBox.information(self, "Batch download", "Search ITRDB first.")
            return
        center = (self.it_lat.value(), self.it_lon.value()) if self.rb_itrdb_point.isChecked() else None
        dlg = BatchDialog(self, self._sites, self._selected_sites(), center)
        if dlg.exec() != QDialog.Accepted or not dlg.result_files:
            return
        self._log(f"Batch: {len(dlg.result_files)} file(s) selected by criteria")
        self._download_files(list(dlg.result_files))

    def download_itrdb(self) -> None:
        checked = [(s, f) for item, (s, f) in self._iter_file_items() if item.checkState() == Qt.Checked]
        if not checked:
            QMessageBox.information(self, "ITRDB", "Check at least one file.")
            return
        self._download_files(checked)

    def _download_files(self, checked: List[Any]) -> None:
        dest = self.it_dir.text().strip() or os.path.join(DO.default_cache_dir(), "itrdb")
        do_import = self.chk_import_crn.isChecked()
        min_depth = self.it_min_depth.value()

        def task(progress, cancel):
            results: List[Dict[str, Any]] = []
            messages: List[str] = []
            for k, (s, f) in enumerate(checked, start=1):
                if cancel():
                    raise DO.CancelledError("Cancelled")
                site_dir = os.path.join(dest, (s.site_code or s.study_id or "site").lower())
                progress("file", k - 1, len(checked), f"[{k}/{len(checked)}] {f.filename}")
                try:
                    path = DO.download_file(f.url, os.path.join(site_dir, f.filename),
                                            progress=progress, cancel=cancel)
                    messages.append(f"Saved {path}")
                    if GX is not None:
                        try:
                            GX.registry.add_itrdb_file(s, f, path)
                        except Exception:
                            pass
                    if do_import and f.kind == "crn":
                        df, meta = DO.read_tucson_crn(path)
                        results.extend(_crn_to_datasets(s, f, df, meta, path, min_depth, messages))
                except DO.CancelledError:
                    raise
                except Exception as exc:  # one bad file must not stop a batch
                    messages.append(f"FAILED {f.filename}: {exc}")
            progress("done", len(checked), len(checked), "ITRDB download finished")
            return results, messages

        self._run_task(task, self._on_itrdb_downloaded, f"Downloading {len(checked)} ITRDB file(s) ...")

    def _on_itrdb_downloaded(self, result: Any) -> None:
        datasets, messages = result
        for m in messages:
            self._log(m)
        if datasets:
            self._emit_datasets(datasets)
        failed = [m for m in messages if m.startswith("FAILED")]
        QMessageBox.information(
            self, "ITRDB",
            f"Downloaded {sum(1 for m in messages if m.startswith('Saved'))} file(s)."
            + (f"\nImported {len(datasets)} chronology dataset(s)." if datasets else "")
            + (f"\n\n{len(failed)} file(s) failed:\n" + "\n".join(failed[:10]) if failed else ""))

    # ------------------------------------------------------------ CRU
    def _update_cru_enabled(self) -> None:
        is_box = self.cmb_mode.currentData() == "bbox_mean"
        for wdg in self._bbox_widgets:
            wdg.setEnabled(is_box)

    def _bbox_around_point(self) -> None:
        lat, lon = self.cru_lat.value(), self.cru_lon.value()
        self.cru_min_lat.setValue(lat - 0.5)
        self.cru_max_lat.setValue(lat + 0.5)
        self.cru_min_lon.setValue(lon - 0.5)
        self.cru_max_lon.setValue(lon + 0.5)

    def _choose_cache_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Download folder for CRU files", self.cru_cache.text())
        if d:
            self.cru_cache.setText(d)

    def _add_target(self, t: DO.ExtractionTarget) -> None:
        names = {x.name for x in self._targets_from_table()}
        base, n = t.name, 2
        while t.name in names:
            t = DO.ExtractionTarget(f"{base}_{n}", t.lat, t.lon, t.mode, t.bbox)
            n += 1
        r = self.tbl_targets.rowCount()
        self.tbl_targets.insertRow(r)
        mode_label = dict(MODE_LABELS).get(t.mode, t.mode)
        cells = [t.name, f"{t.lat:.4f}", f"{t.lon:.4f}", mode_label,
                 "" if not t.bbox else "%.2f..%.2f N, %.2f..%.2f E" % t.bbox]
        for c, v in enumerate(cells):
            item = QTableWidgetItem(v)
            if c > 0:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if c == 0:
                item.setData(Qt.UserRole, t)
            self.tbl_targets.setItem(r, c, item)
        self._schedule_map()

    # ------------------------------------------------------------ chronology locations
    def set_chronology_locations(self, locations: Sequence[Tuple[str, float, float]]) -> None:
        """Locations (name, lat, lon) of the chronologies loaded in the host program."""
        self._chron_locations = [(str(n), float(a), float(o)) for n, a, o in (locations or [])]
        n = len(self._chron_locations)
        for b in (getattr(self, "btn_chron_targets", None),
                  getattr(getattr(self, "sources_tab", None), "btn_chron_targets", None)):
            if b is not None:
                b.setEnabled(n > 0)
                b.setText(f"Add chronology locations ({n})" if n else "Add chronology locations")

    def add_chronology_targets(self) -> int:
        """Put every chronology location into the target list; returns the number added."""
        locs = list(getattr(self, "_chron_locations", []) or [])
        if not locs:
            QMessageBox.information(self, "Chronology locations",
                                    "No chronology with coordinates is loaded. ITRDB chronologies and chronologies "
                                    "sent from Detrend Advisor (after an ITRDB import) carry their coordinates.")
            return 0
        mode = self.cmb_mode.currentData() if hasattr(self, "cmb_mode") else "nearest"
        if mode not in ("nearest", "bilinear"):
            mode = "nearest"
        have = {(round(t.lat, 4), round(t.lon, 4)) for t in self._targets_from_table()}
        added = 0
        for name, lat, lon in locs:
            key = (round(lat, 4), round(lon, 4))
            if key in have:
                continue
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "site"
            self._add_target(DO.ExtractionTarget(safe[:24], lat, lon, mode))
            have.add(key)
            added += 1
        st = getattr(self, "sources_tab", None)
        if st is not None and hasattr(st, "chk_all_targets") and self.tbl_targets.rowCount() > 0:
            st.chk_all_targets.setChecked(True)
        self._log(f"{added} chronology location(s) added to the target list; climate is extracted for each of them")
        return added

    def _add_current_target(self) -> None:
        try:
            t = self._current_target()
            t.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Target", str(exc))
            return
        if any(self._target_key(x) == self._target_key(t) for x in self._targets_from_table()):
            QMessageBox.information(self, "Target", "This location is already in the list.")
            return
        self._add_target(t)

    def _remove_targets(self) -> None:
        rows = sorted({i.row() for i in self.tbl_targets.selectionModel().selectedRows()}, reverse=True)
        for r in rows:
            self.tbl_targets.removeRow(r)
        self._schedule_map()

    def _on_target_selection(self) -> None:
        self._schedule_map()

    def _set_versions(self, versions: List[str]) -> None:
        current = self.cmb_version.currentText()
        self.cmb_version.blockSignals(True)
        self.cmb_version.clear()
        self.cmb_version.addItems(versions)
        idx = self.cmb_version.findText(current)
        self.cmb_version.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_version.blockSignals(False)

    def load_versions(self, silent: bool = False) -> None:
        def task(progress, cancel):
            progress("plan", 0, 0, "Checking CRU TS versions ...")
            return DO.list_cru_versions()

        def done(versions: List[str]) -> None:
            self._versions_loaded = True
            self._set_versions(versions)
            self._log(f"CRU TS versions: {', '.join(versions)}")

        if self._thread is not None:
            return
        self._versions_loaded = True  # do not retry automatically on every tab switch
        self._silent_fail = silent
        self._run_task(task, done, "Checking CRU TS versions ...")

    def _build_cru_request(self) -> Optional[DO.CruRequest]:
        variables = [c for c, cb in self.var_checks.items() if cb.isChecked()]
        if not variables:
            QMessageBox.information(self, "CRU TS", "Select at least one variable.")
            return None
        targets = self._targets_from_table() or [self._current_target()]
        try:
            for t in targets:
                t.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "CRU TS", str(exc))
            return None
        names = [t.name for t in targets]
        if len(set(names)) != len(names):
            QMessageBox.warning(self, "CRU TS", "Target names must be unique.")
            return None
        y0, y1 = self.cru_start.value(), self.cru_end.value()
        if y0 > y1:
            QMessageBox.warning(self, "CRU TS", "Start year must not be after end year.")
            return None
        return DO.CruRequest(
            version=self.cmb_version.currentText(), variables=variables, targets=targets,
            start_year=y0, end_year=y1,
            cache_dir=self.cru_cache.text().strip() or None,
            keep_downloads=not self.chk_delete.isChecked(),
        )

    def run_cru(self, estimate_only: bool = False) -> None:
        req = self._build_cru_request()
        if req is None:
            return

        def plan_task(progress, cancel):
            progress("plan", 0, 0, f"Resolving CRU TS {req.version} files ...")
            return DO.plan_cru_download(req)

        def plan_done(planned) -> None:
            release, files = planned
            size, exact = DO.estimate_download_size(files)
            unpacked = 260 * 1024 * 1024 if files else 0
            text = (f"CRU TS {release.version}: {len(files)} file(s), {'' if exact else '~'}"
                    f"{DO.human_size(size)} to download (one file is processed at a time; "
                    f"~{DO.human_size(unpacked)} temporary disk space).")
            self.lbl_estimate.setText(text)
            self._log(text)
            if estimate_only:
                return
            if release.end_year < req.end_year:
                self._log(f"Note: CRU TS {release.version} ends in {release.end_year}.")
            msg = (f"{text}\n\nTargets: {', '.join(t.name for t in req.targets)}\n"
                   f"Variables: {', '.join(req.variables)}\nYears: {req.start_year}-{req.end_year}\n\n"
                   + ("Files will be deleted after extraction." if not req.keep_downloads
                      else f"Files will be kept in {req.cache_dir or DO.default_cache_dir()}.")
                   + "\n\nContinue?")
            if QMessageBox.question(self, "CRU TS download", msg) != QMessageBox.Yes:
                return
            self._run_task(lambda p, c: DO.run_cru_request(req, progress=p, cancel=c, planned=planned),
                           self._on_cru_done, "Downloading and extracting CRU TS ...")

        self._run_task(plan_task, plan_done, f"Resolving CRU TS {req.version} files ...")

    def cru_result_to_datasets(self, res: DO.CruResult):
        """(datasets, warnings) from a CRU TS result; also records the extraction for the location export."""
        datasets: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for t in res.request.targets:
            for var, r in res.results.get(t.name, {}).items():
                self._cru_last_cells[self._target_key(t)] = r.cells
                wide = r.to_wide(month_names=self._month_names)
                v = DO.CRU_VARIABLES.get(var)
                meta = res.provenance(t.name, var)
                stn = DO.station_support_by_year(r)
                meta["station_support"] = [[int(a), None if pd.isna(b) else round(float(b), 2)]
                                           for a, b in zip(stn["Year"], stn["mean_stn"])]
                meta["summary"] = r.meta
                warn = DO.station_support_warning(r)
                if warn:
                    meta["warning"] = warn
                    warnings.append(f"{t.name}: {warn}")
                if r.meta.get("invalid_cells"):
                    w2 = (f"{t.name} {var}: {r.meta['invalid_cells']} of {len(r.cells)} cell(s) have no data "
                          f"(sea / lake); weights were renormalised over the remaining cells.")
                    warnings.append(w2)
                if r.meta.get("n_missing"):
                    warnings.append(f"{t.name} {var}: {r.meta['n_missing']} month(s) without value.")
                mode_short = {"bilinear": "bil", "nearest": "nn", "bbox_mean": "box"}.get(t.mode, t.mode)
                name = f"CRU{res.release.version} {var} {t.name} ({t.lat:.2f}N {t.lon:.2f}E {mode_short})"
                datasets.append({
                    "kind": "climate",
                    "name": name,
                    "path": res.release.variable_url(var),
                    "df": wide,
                    "climate_param": v.ringclim_param if v else "Other",
                    "monthly_agg": v.monthly_agg if v else None,
                    "meta": meta,
                })
        if GX is not None:
            for t in res.request.targets:
                rs = res.results.get(t.name, {})
                if not rs:
                    continue
                first = next(iter(rs.values()))
                try:
                    GX.registry.add_climate_extraction(
                        f"CRU TS {res.release.version}", list(rs.keys()), t.name, t.lat, t.lon, t.mode,
                        first.meta.get("first_year"), first.meta.get("last_year"), first.cells,
                        (DO.CRU_GRID_RES, DO.CRU_GRID_RES), t.bbox, version=res.release.version,
                        resolution="0.5 deg")
                except Exception:
                    pass
        return datasets, warnings

    def _on_cru_done(self, res: DO.CruResult) -> None:
        for line in res.log:
            self._log(line)
        datasets, warnings = self.cru_result_to_datasets(res)
        self._emit_datasets(datasets)
        self._schedule_map()
        text = f"Added {len(datasets)} climate dataset(s)."
        if warnings:
            text += "\n\nWarnings:\n- " + "\n- ".join(warnings)
            for w in warnings:
                self._log("WARNING: " + w)
        QMessageBox.information(self, "CRU TS", text)

    # ------------------------------------------------------------ output
    def _emit_datasets(self, datasets: List[Dict[str, Any]]) -> None:
        if not datasets:
            return
        for d in datasets:
            self._log(f"Dataset ready: {d['name']} ({d['kind']}, {len(d['df'])} years)")
        if self._standalone:
            self._save_standalone(datasets)
        self.datasets_ready.emit(datasets)
        self.settings_changed.emit(self.get_settings())

    def _save_standalone(self, datasets: List[Dict[str, Any]]) -> None:
        if not self._standalone_dir:
            d = QFileDialog.getExistingDirectory(self, "Folder for the imported data (CSV)", os.path.expanduser("~"))
            if not d:
                return
            self._standalone_dir = d
        for ds in datasets:
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in ds["name"]).strip("_")
            base = os.path.join(self._standalone_dir, safe)
            ds["df"].to_csv(base + ".csv", index=False)
            with open(base + "_meta.json", "w", encoding="utf-8") as fh:
                json.dump(ds.get("meta", {}), fh, indent=1, default=str)
            self._log(f"Saved {base}.csv")

    # ------------------------------------------------------------ docking
    def set_dock(self, dock) -> None:
        """Called by the host window: ``dock`` is a dendro_ui_help.QtTabDock."""
        self._dock = dock
        btn = dock.make_button(self)
        self._status_layout.insertWidget(max(0, self._status_layout.count() - 1), btn)

    def _close_clicked(self) -> None:
        if self._dock is not None:
            self.close_requested_from_tab()
        else:
            self.close()

    def _can_close(self) -> bool:
        if self._thread is not None:
            if QMessageBox.question(self, "Online data", "An operation is running. Cancel it and close?") \
                    != QMessageBox.Yes:
                return False
            self._cancel_event.set()
            self._thread.quit()
            self._thread.wait(10000)
        return True

    def close_requested_from_tab(self) -> None:
        if not self._can_close():
            return
        self.settings_changed.emit(self.get_settings())
        if self._standalone and SQ is not None:
            SQ.ask_keep_cache_qt(self)
        dock = self._dock
        self._dock = None
        if dock is not None:
            dock.close()
        self.hide()
        self.finished.emit(0)

    def reject(self) -> None:
        # Esc must not hide the page while it is a tab of the main window
        if self._dock is not None and self._dock.docked:
            return
        if self._dock is not None:
            self._dock.attach()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            if QMessageBox.question(self, "Online data", "An operation is running. Cancel it and close?") \
                    != QMessageBox.Yes:
                event.ignore()
                return
            self._cancel_event.set()
            self._thread.quit()
            self._thread.wait(10000)
        self.settings_changed.emit(self.get_settings())
        if self._standalone and SQ is not None:
            SQ.ask_keep_cache_qt(self)
        super().closeEvent(event)


def _crn_to_datasets(site: DO.ItrdbSite, f: DO.ItrdbFile, df: pd.DataFrame, meta: Dict[str, Any],
                     path: str, min_depth: int, messages: List[str]) -> List[Dict[str, Any]]:
    """Convert a parsed .crn into chronology dataset dicts (one per chronology in the file)."""
    out: List[Dict[str, Any]] = []
    value_cols = [c for c in df.columns if c != "Year" and not str(c).endswith("_n")]
    variant = _VARIANT_SHORT.get(f.variant, f.variant or "std")
    param = _short_param(f.parameter)
    for col in value_cols:
        sub = df[["Year", col, f"{col}_n"]].copy()
        if min_depth > 1:
            depth = pd.to_numeric(sub[f"{col}_n"], errors="coerce")
            before = len(sub)
            sub = sub[depth >= min_depth]
            messages.append(f"{f.filename}: removed {before - len(sub)} year(s) with sample depth < {min_depth}")
        sub = sub.dropna(subset=[col])
        if sub.empty:
            messages.append(f"{f.filename}: no years left after filtering")
            continue
        label = f"{site.site_code}_{param}_{variant}" + ("" if len(value_cols) == 1 else f"_{col}")
        out_df = pd.DataFrame({"Year": sub["Year"].astype(int).values, label: sub[col].astype(float).values})
        out.append({
            "kind": "chronology",
            "name": f"ITRDB {site.site_code} {param} {variant}",
            "path": path,
            "df": out_df,
            "value_column": label,
            "meta": {
                "source": "ITRDB",
                "site_code": site.site_code, "site_name": site.site_name, "species": site.species,
                "lat": site.lat, "lon": site.lon, "elevation_m": site.elevation_m,
                "investigators": site.investigators, "doi": site.doi, "study_url": site.study_url,
                "file_url": f.url, "parameter": f.parameter, "variant": f.variant,
                "min_sample_depth": int(min_depth),
                "sample_depth": [[int(y), None if pd.isna(n) else int(n)]
                                 for y, n in zip(sub["Year"], sub[f"{col}_n"])],
                "crn_header": meta.get("header", []),
                "citation": DO.ITRDB_CITATION,
            },
        })
    return out


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    try:  # developer signature + hover help on every dialog (optional module)
        import dendro_ui_help

        dendro_ui_help.install_qt(app)
    except Exception:
        pass
    settings_path = os.path.join(os.path.expanduser("~"), ".dendro_online_qt.json")
    settings: Dict[str, Any] = {}
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except Exception:
        pass
    dlg = OnlineDataDialog(settings=settings, standalone=True)

    def save_settings(s: Dict[str, Any]) -> None:
        try:
            with open(settings_path, "w", encoding="utf-8") as fh:
                json.dump(s, fh, indent=1)
        except Exception:
            pass

    dlg.settings_changed.connect(save_settings)
    dlg.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
