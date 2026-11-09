#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_sources_qt.py - PySide6 widgets for the keyless climate sources and the
location export (used by dendro_online_qt.OnlineDataDialog / RingClim).

* SourcesTab            - "More climate data" tab: CHELSA, TerraClimate, GPCC,
                          scPDSI, SPEIbase, SPEI/SPI computed, CHIRPS, Berkeley
                          Earth, GHCN-Daily stations, ERA5 via Open-Meteo.
* ExportLocationsDialog - CSV / GeoJSON / KMZ / Shapefile export of everything
                          that was downloaded (ITRDB, CRU, the sources above).
* ask_keep_cache_qt     - question at program exit: keep the download list and
                          cached values for the next start?
"""

from __future__ import annotations

import dataclasses
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
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
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import dendro_geoexport as GX
import dendro_online as DO
import dendro_sources as S

try:  # Copernicus CDS / EWDS (sources that need the user's own key)
    import dendro_copernicus as DC
except Exception:  # pragma: no cover - module missing
    DC = None

MODE_TEXT = {
    "nearest": "Point: nearest grid cell / pixel",
    "bilinear": "Point: bilinear (4 nearest cells)",
    "bbox_mean": "Area mean (bounding box)",
    "region": "Country / region mean",
    "station": "Weather stations near the point",
}
SHORT_NAME = {
    "chelsa_monthly": "CHELSA-m", "chelsa_daily": "CHELSA-d", "terraclimate": "TerraClimate", "gpcc": "GPCC",
    "scpdsi": "scPDSI", "speibase": "SPEIbase", "spei_calc": "SPEI", "chirps_daily": "CHIRPS-d",
    "chirps_monthly": "CHIRPS-m", "berkeley": "BerkeleyEarth", "ghcnd": "GHCN-D", "openmeteo": "ERA5",
}
_MODE_SHORT = {"bilinear": "bil", "nearest": "nn", "bbox_mean": "box", "region": "region", "station": "stn"}


def _dspin(lo: float, hi: float, value: float):
    from PySide6.QtWidgets import QDoubleSpinBox

    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(4)
    w.setSingleStep(0.1)
    w.setValue(value)
    return w


def _target_key(t: DO.ExtractionTarget) -> str:
    return f"{t.lat:.4f}|{t.lon:.4f}|{t.mode}|{t.bbox}"


def source_result_to_datasets(res: S.SourceResult, daily_as: str, month_names: List[str]):
    """RingClim dataset dicts from a SourceResult -> (datasets, warnings, {target key: (cells, size, source)})."""
    src = res.request.source
    info = S.SOURCES[src]
    datasets: List[Dict[str, Any]] = []
    warnings: List[str] = []
    cells_out: Dict[str, Any] = {}
    tmap = {t.name: t for t in res.request.targets}
    for tname, rs in res.results.items():
        t = tmap.get(tname)
        for var, r in rs.items():
            if t is not None and r.cells:
                cells_out[_target_key(t)] = (r.cells, r.cell_size, src)
            v = S.source_var(src, var)
            meta = res.provenance(tname, var)
            if r.meta.get("warning"):
                warnings.append(f"{tname}: {r.meta['warning']}")
            n_bad = sum(1 for c in r.cells if c.valid is False)
            if r.cells and n_bad:
                warnings.append(f"{tname} {var}: {n_bad} of {len(r.cells)} cell(s) without data (sea / lake / "
                                f"outside coverage); weights were renormalised.")
            where = (f"{t.lat:.2f}N {t.lon:.2f}E {_MODE_SHORT.get(t.mode, t.mode)}" if t is not None else "")
            if src == "berkeley":
                where = str(r.meta.get("region", ""))
            elif src == "ghcnd":
                where = f"{r.meta.get('dist_km', '')} km"
            base = f"{SHORT_NAME.get(src, src)} {var} {tname} ({where})".replace(" ()", "")
            if r.freq == "monthly":
                data = r.data[["Year", "Month", "value"]].dropna(subset=["Year", "Month"])
                if data["value"].notna().sum() == 0:
                    warnings.append(f"{tname} {var}: no data in the period.")
                    continue
                wide = DO.monthly_long_to_wide(data, month_names=month_names)
                datasets.append({"kind": "climate", "name": base, "path": info.url, "df": wide,
                                 "climate_param": v.ringclim_param, "monthly_agg": v.agg,
                                 "meta": dict(meta, source_key=src, units=v.units)})
            else:
                d = r.data.dropna(subset=["Date"])
                if d["value"].notna().sum() == 0:
                    warnings.append(f"{tname} {var}: no data in the period.")
                    continue
                if daily_as in ("both", "daily"):
                    ts = pd.DataFrame({"DateTime": pd.to_datetime(d["Date"]), "Value": d["value"].astype(float)})
                    datasets.append({"kind": "timeseries", "name": base + " daily", "path": info.url, "df": ts,
                                     "climate_param": v.ringclim_param, "source_freq": "daily",
                                     "monthly_agg": v.agg,
                                     "meta": dict(meta, frequency="daily", source_key=src, units=v.units)})
                if daily_as in ("both", "monthly"):
                    mon = S.daily_to_monthly(d, v.agg)
                    if mon["value"].notna().any():
                        wide = DO.monthly_long_to_wide(mon, month_names=month_names)
                        datasets.append({"kind": "climate", "name": base, "path": info.url, "df": wide,
                                         "climate_param": v.ringclim_param, "monthly_agg": v.agg,
                                         "meta": dict(meta, frequency="monthly from daily", source_key=src,
                                                      units=v.units,
                                                      monthly_rule=("sum, >= 90% of days" if v.agg == "sum"
                                                                    else "mean, >= 20 days"))})
    return datasets, warnings, cells_out


class SourcesTab(QWidget):
    """Tab for the keyless sources; uses the host dialog for tasks, map, log and output."""

    def __init__(self, host: Any) -> None:
        super().__init__()
        self.host = host
        self._stations: List[S.Station] = []
        self._last_cells: Dict[str, Any] = {}   # target key -> (cells, cell size)
        self._build()
        self._on_source_changed()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        lay = QVBoxLayout(self)

        g = QGroupBox("Dataset")
        gl = QGridLayout(g)
        self.cmb_source = QComboBox()
        for key, info in S.SOURCES.items():
            self.cmb_source.addItem(info.label, key)
        self.cmb_source.setToolTip("Data source. Sources marked 'needs key' use your own free Copernicus key "
                                   "(button 'API keys...'); all others need no registration.")
        self.btn_page = QPushButton("Data page")
        self.btn_page.setToolTip("Open the web page of the selected data set (description, licence, citation).")
        self.btn_page.clicked.connect(self._open_page)
        gl.addWidget(QLabel("Source:"), 0, 0)
        gl.addWidget(self.cmb_source, 0, 1, 1, 3)
        gl.addWidget(self.btn_page, 0, 4)
        self.lbl_info = QLabel("")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet("color: #444;")
        gl.addWidget(self.lbl_info, 1, 0, 1, 5)
        self.lst_vars = QListWidget()
        self.lst_vars.setMaximumHeight(130)
        self.lst_vars.setToolTip("Tick the variables to download.")
        gl.addWidget(QLabel("Variables:"), 2, 0, Qt.AlignTop)
        gl.addWidget(self.lst_vars, 2, 1, 1, 4)

        # source-specific options
        self.opt_row = QWidget()
        ol = QHBoxLayout(self.opt_row)
        ol.setContentsMargins(0, 0, 0, 0)
        self.lbl_gpcc = QLabel("GPCC grid:")
        self.cmb_gpcc = QComboBox()
        for r in ("0.25", "0.5", "1.0", "2.5"):
            self.cmb_gpcc.addItem(f"{r} deg", r)
        self.cmb_gpcc.setCurrentIndex(1)
        self.cmb_gpcc.setToolTip("Grid spacing of the GPCC product (finer = larger download).")
        self.lbl_model = QLabel("ERA5 model:")
        self.cmb_model = QComboBox()
        for k, (label, _res) in S.OPENMETEO_MODELS.items():
            self.cmb_model.addItem(label, k)
        self.cmb_model.setToolTip("ERA5 (0.25 deg, from 1940) or ERA5-Land (0.1 deg, land only, from 1950).")
        self.lbl_base = QLabel("P and PET from:")
        self.cmb_base = QComboBox()
        self.cmb_base.addItem("TerraClimate (1958-, ~4 km)", "terraclimate")
        self.cmb_base.addItem("CHELSA monthly (1979-2021, ~1 km)", "chelsa_monthly")
        self.cmb_base.setToolTip("Data set that provides the monthly precipitation and potential evapotranspiration.")
        self.lbl_region = QLabel("Region / country:")
        self.ed_region = QLineEdit()
        self.ed_region.setPlaceholderText("detected from the point")
        self.ed_region.setToolTip("Berkeley Earth region name, e.g. Tajikistan, Kyrgyzstan, Uzbekistan. "
                                  "Empty = country of the point.")
        self.lbl_daily = QLabel("Import daily data as:")
        self.cmb_daily = QComboBox()
        self.cmb_daily.addItem("Daily series + monthly table", "both")
        self.cmb_daily.addItem("Daily series only", "daily")
        self.cmb_daily.addItem("Monthly table only", "monthly")
        self.cmb_daily.setToolTip("Daily series can be used with Resolution = Daily in the Analysis tab; the monthly "
                                  "table is computed here (sum or mean by variable, incomplete months removed).")
        self.lbl_snap = QLabel("Find the river within:")
        self.sp_snap = QSpinBox()
        self.sp_snap.setRange(0, 20)
        self.sp_snap.setValue(3)
        self.sp_snap.setSuffix(" cells")
        self.sp_snap.setToolTip("GloFAS cells are 0.05 deg (~5 km). The cell with the largest mean discharge within "
                                "this distance of the point is used (the point is rarely exactly on the river). "
                                "0 = the cell of the point.")
        for w in (self.lbl_gpcc, self.cmb_gpcc, self.lbl_model, self.cmb_model, self.lbl_base, self.cmb_base,
                  self.lbl_region, self.ed_region, self.lbl_snap, self.sp_snap, self.lbl_daily, self.cmb_daily):
            ol.addWidget(w)
        ol.addStretch(1)
        gl.addWidget(self.opt_row, 3, 0, 1, 5)
        lay.addWidget(g)

        loc = QGroupBox("Location")
        lg = QGridLayout(loc)
        self.ed_name = QLineEdit("site")
        self.sp_lat = _dspin(-90, 90, 40.28)
        self.sp_lon = _dspin(-180, 180, 69.62)
        self.cmb_mode = QComboBox()
        self.cmb_mode.setToolTip("How the value for the site is taken from the grid.")
        self.sp_s = _dspin(-90, 90, 40.0)
        self.sp_n = _dspin(-90, 90, 40.5)
        self.sp_w = _dspin(-180, 180, 69.4)
        self.sp_e = _dspin(-180, 180, 69.9)
        self.btn_from_cru = QPushButton("Copy from CRU TS tab")
        self.btn_from_cru.setToolTip("Use the name and coordinates of the CRU TS tab.")
        self.btn_from_cru.clicked.connect(self._copy_from_cru)
        self.chk_all_targets = QCheckBox("Use all CRU TS targets")
        self.chk_all_targets.setToolTip("Extract every point in the target list of the CRU TS tab in one run.")
        lg.addWidget(QLabel("Name:"), 0, 0)
        lg.addWidget(self.ed_name, 0, 1)
        lg.addWidget(QLabel("Lat:"), 0, 2)
        lg.addWidget(self.sp_lat, 0, 3)
        lg.addWidget(QLabel("Lon:"), 0, 4)
        lg.addWidget(self.sp_lon, 0, 5)
        lg.addWidget(QLabel("Method:"), 1, 0)
        lg.addWidget(self.cmb_mode, 1, 1, 1, 3)
        lg.addWidget(self.btn_from_cru, 1, 4, 1, 2)
        lg.addWidget(QLabel("Min lat:"), 2, 0)
        lg.addWidget(self.sp_s, 2, 1)
        lg.addWidget(QLabel("Max lat:"), 2, 2)
        lg.addWidget(self.sp_n, 2, 3)
        lg.addWidget(self.chk_all_targets, 2, 4, 1, 2)
        self.btn_chron_targets = QPushButton("Add chronology locations")
        self.btn_chron_targets.setToolTip("Add every chronology with known coordinates to the target list and use "
                                          "all targets: each chronology gets its own climate series.")
        self.btn_chron_targets.setEnabled(False)
        self.btn_chron_targets.clicked.connect(lambda: self.host.add_chronology_targets()
                                               if hasattr(self.host, "add_chronology_targets") else None)
        lg.addWidget(self.btn_chron_targets, 3, 4, 1, 2)
        lg.addWidget(QLabel("Min lon:"), 3, 0)
        lg.addWidget(self.sp_w, 3, 1)
        lg.addWidget(QLabel("Max lon:"), 3, 2)
        lg.addWidget(self.sp_e, 3, 3)
        self._box_widgets = [self.sp_s, self.sp_n, self.sp_w, self.sp_e]
        lay.addWidget(loc)

        self.grp_st = QGroupBox("Weather stations (GHCN-Daily)")
        sl = QGridLayout(self.grp_st)
        self.sp_radius = QSpinBox()
        self.sp_radius.setRange(5, 2000)
        self.sp_radius.setValue(150)
        self.sp_radius.setSuffix(" km")
        self.sp_radius.setToolTip("Search radius around the point.")
        self.sp_minyears = QSpinBox()
        self.sp_minyears.setRange(1, 200)
        self.sp_minyears.setValue(20)
        self.sp_minyears.setToolTip("Stations must have at least this many years of the selected variables inside the period.")
        self.btn_find = QPushButton("Find stations")
        self.btn_find.setToolTip("Search NOAA GHCN-Daily stations near the point (the station list is cached).")
        self.btn_find.clicked.connect(self.find_stations)
        self.tbl_st = QTableWidget(0, 6)
        self.tbl_st.setHorizontalHeaderLabels(["Station", "Name", "Dist km", "Elev m", "Lat / Lon", "Data (years)"])
        self.tbl_st.verticalHeader().setVisible(False)
        self.tbl_st.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_st.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_st.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_st.horizontalHeader().setStretchLastSection(True)
        self.tbl_st.setMinimumHeight(110)
        self.tbl_st.itemChanged.connect(lambda *a: self.host._schedule_map())
        sl.addWidget(QLabel("Radius:"), 0, 0)
        sl.addWidget(self.sp_radius, 0, 1)
        sl.addWidget(QLabel("Min years:"), 0, 2)
        sl.addWidget(self.sp_minyears, 0, 3)
        sl.addWidget(self.btn_find, 0, 4)
        sl.addWidget(self.tbl_st, 1, 0, 1, 6)
        lay.addWidget(self.grp_st, 3)

        per = QGroupBox("Period and storage")
        pl = QGridLayout(per)
        self.sp_start = QSpinBox()
        self.sp_end = QSpinBox()
        for w in (self.sp_start, self.sp_end):
            w.setRange(1700, 2100)
        self.sp_start.setValue(1980)
        self.sp_end.setValue(2020)
        self.chk_keep = QCheckBox("Keep downloaded files (otherwise they are deleted after extraction)")
        self.chk_keep.setToolTip("Only for sources that download whole files (GPCC, scPDSI, SPEIbase).")
        self.lbl_est = QLabel("")
        self.lbl_est.setWordWrap(True)
        self.lbl_est.setStyleSheet("color: #555;")
        pl.addWidget(QLabel("Years:"), 0, 0)
        pl.addWidget(self.sp_start, 0, 1)
        pl.addWidget(QLabel("to"), 0, 2)
        pl.addWidget(self.sp_end, 0, 3)
        pl.addWidget(self.chk_keep, 1, 0, 1, 5)
        pl.addWidget(self.lbl_est, 2, 0, 1, 5)
        lay.addWidget(per)

        run = QHBoxLayout()
        self.btn_run = QPushButton("Get data")
        self.btn_run.setToolTip("Download / read the selected variables for the location and period and add them "
                                "as datasets.")
        self.btn_run.clicked.connect(self.run)
        self.btn_grdc = QPushButton("GRDC portal...")
        self.btn_grdc.setToolTip("River discharge (GRDC) has no public API: request the data on the GRDC portal, "
                                 "then import the downloaded files with 'Add / Import File' (detected automatically).")
        self.btn_grdc.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(S.GRDC_PORTAL_URL)))
        run.addWidget(self.btn_run)
        self.btn_batch = QPushButton("Download everything for the area...")
        self.btn_batch.setToolTip("Many sources and variables at once (plus ITRDB chronologies in the area) for the "
                                  "location and period above; everything is added for analysis and can be saved "
                                  "to a folder.")
        self.btn_batch.clicked.connect(lambda: start_batch(self))
        run.addWidget(self.btn_batch)
        self.btn_resume = QPushButton("Resume paused downloads")
        self.btn_resume.setToolTip("Download the sources that were paused in a batch (files already loaded come "
                                   "from the cache).")
        self.btn_resume.clicked.connect(lambda: resume_paused(self))
        self.btn_resume.setVisible(False)
        run.addWidget(self.btn_resume)
        run.addStretch(1)
        self.btn_keys = QPushButton("API keys...")
        self.btn_keys.setToolTip("Your own free Copernicus key (CDS / EWDS) for ERA5, ERA5-Land and GloFAS. "
                                 "The key is saved only on this computer.")
        self.btn_keys.clicked.connect(lambda: ApiKeysDialog(self).exec())
        self.btn_keys.setVisible(DC is not None)
        run.addWidget(self.btn_keys)
        run.addWidget(self.btn_grdc)
        lay.addLayout(run)
        lay.addStretch(1)

        self.cmb_source.currentIndexChanged.connect(self._on_source_changed)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.cmb_gpcc.currentIndexChanged.connect(lambda *a: self.host._schedule_map())
        self.cmb_model.currentIndexChanged.connect(lambda *a: self.host._schedule_map())
        for w in (self.sp_lat, self.sp_lon, self.sp_s, self.sp_n, self.sp_w, self.sp_e):
            w.valueChanged.connect(lambda *a: self.host._schedule_map())
        for w in (self.sp_start, self.sp_end):
            w.valueChanged.connect(lambda *a: self._update_estimate())
        self.lst_vars.itemChanged.connect(lambda *a: self._update_estimate())
        self.host._busy_widgets += [self.btn_run, self.btn_find, self.btn_batch, self.btn_resume]

    # ------------------------------------------------------------ state
    @property
    def source(self) -> str:
        return str(self.cmb_source.currentData())

    def _open_page(self) -> None:
        QDesktopServices.openUrl(QUrl(S.SOURCES[self.source].url))

    def _on_source_changed(self, *args: Any) -> None:
        info = S.SOURCES[self.source]
        self.lst_vars.blockSignals(True)
        self.lst_vars.clear()
        for k, v in enumerate(info.variables):
            it = QListWidgetItem(f"{v.code} - {v.label} ({v.units})")
            it.setData(Qt.UserRole, v.code)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            default = k == 0 or v.code in ("pr", "ppt", "precip", "PRCP", "precipitation_sum", "tmax", "TMIN", "tp",
                                           "spei3", "spei12")
            it.setCheckState(Qt.Checked if default and k < 4 else Qt.Unchecked)
            self.lst_vars.addItem(it)
        self.lst_vars.blockSignals(False)
        current_mode = self.cmb_mode.currentData()
        self.cmb_mode.blockSignals(True)
        self.cmb_mode.clear()
        for m in info.modes:
            self.cmb_mode.addItem(MODE_TEXT[m], m)
        idx = self.cmb_mode.findData(current_mode)
        self.cmb_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_mode.blockSignals(False)
        self.lbl_info.setText(
            f"<b>{info.label}</b> - {info.freq}, {info.period[0]}-{info.period[1]}, {info.resolution}."
            + (f" {info.note}" if info.note else ""))
        show = {
            (self.lbl_gpcc, self.cmb_gpcc): self.source == "gpcc",
            (self.lbl_model, self.cmb_model): self.source == "openmeteo",
            (self.lbl_base, self.cmb_base): self.source == "spei_calc",
            (self.lbl_region, self.ed_region): self.source == "berkeley",
            (self.lbl_snap, self.sp_snap): self.source == "glofas",
            (self.lbl_daily, self.cmb_daily): info.freq == "daily",
        }
        for ws, vis in show.items():
            for w in ws:
                w.setVisible(vis)
        self.grp_st.setVisible(info.kind == "station")
        self.chk_keep.setVisible(self.source in ("gpcc", "scpdsi", "speibase"))
        lo, hi = info.period
        if self.sp_start.value() < lo:
            self.sp_start.setValue(lo)
        if self.sp_end.value() > hi:
            self.sp_end.setValue(hi)
        self._on_mode_changed()

    def _on_mode_changed(self, *args: Any) -> None:
        is_box = self.cmb_mode.currentData() == "bbox_mean"
        for w in self._box_widgets:
            w.setEnabled(is_box)
        self.chk_all_targets.setEnabled(self.cmb_mode.currentData() in ("nearest", "bilinear", "bbox_mean"))
        self._update_estimate()
        self.host._schedule_map()

    def _copy_from_cru(self) -> None:
        h = self.host
        self.ed_name.setText(h.cru_name.text())
        self.sp_lat.setValue(h.cru_lat.value())
        self.sp_lon.setValue(h.cru_lon.value())
        self.sp_s.setValue(min(h.cru_min_lat.value(), h.cru_max_lat.value()))
        self.sp_n.setValue(max(h.cru_min_lat.value(), h.cru_max_lat.value()))
        self.sp_w.setValue(min(h.cru_min_lon.value(), h.cru_max_lon.value()))
        self.sp_e.setValue(max(h.cru_min_lon.value(), h.cru_max_lon.value()))

    def set_point(self, lat: float, lon: float, add: bool = False) -> None:
        """Point chosen on the map (Ctrl+click also adds it to the CRU TS target list)."""
        self.sp_lat.setValue(lat)
        self.sp_lon.setValue(lon)
        if add:
            h = self.host
            n = h.tbl_targets.rowCount() + 1
            h._add_target(DO.ExtractionTarget(f"pt{n}", lat, lon, "bilinear", None))
            self.chk_all_targets.setChecked(True)
            h._log(f"Map: target pt{n} added (used with 'Use all CRU TS targets')")
        if S.SOURCES[self.source].kind == "station":
            self._stations = []
            self.tbl_st.setRowCount(0)

    def set_box(self, s: float, n: float, w: float, e: float) -> None:
        """Box drawn on the map: area mean when the source allows it, otherwise its centre."""
        self.sp_s.setValue(s)
        self.sp_n.setValue(n)
        self.sp_w.setValue(w)
        self.sp_e.setValue(e)
        self.sp_lat.setValue(round((s + n) / 2, 5))
        self.sp_lon.setValue(round((w + e) / 2, 5))
        idx = self.cmb_mode.findData("bbox_mean")
        if idx >= 0:
            self.cmb_mode.setCurrentIndex(idx)
        if self.chk_all_targets.isChecked():
            self.chk_all_targets.setChecked(False)

    def selected_vars(self) -> List[str]:
        out = []
        for i in range(self.lst_vars.count()):
            it = self.lst_vars.item(i)
            if it.checkState() == Qt.Checked:
                out.append(str(it.data(Qt.UserRole)))
        return out

    def _cell_res(self) -> float:
        if self.source == "gpcc":
            return float(self.cmb_gpcc.currentData())
        if self.source == "openmeteo":
            return S.OPENMETEO_MODELS.get(self.cmb_model.currentData(), ("", 0.25))[1]
        if self.source == "spei_calc":
            return 1.0 / 120.0 if self.cmb_base.currentData() == "chelsa_monthly" else 1.0 / 24.0
        if self.source in ("scpdsi", "speibase"):
            return 0.5
        return S.SOURCES[self.source].cell_deg

    def targets(self) -> List[DO.ExtractionTarget]:
        mode = self.cmb_mode.currentData()
        if mode in ("region", "station"):
            mode_t = "nearest"
        else:
            mode_t = mode
        if self.chk_all_targets.isChecked() and self.chk_all_targets.isEnabled():
            ts = self.host._targets_from_table()
            if ts:
                out = []
                for t in ts:
                    bbox = t.bbox if mode_t == "bbox_mean" else None
                    if mode_t == "bbox_mean" and bbox is None:
                        bbox = (t.lat - 0.25, t.lat + 0.25, t.lon - 0.25, t.lon + 0.25)
                    out.append(DO.ExtractionTarget(t.name, t.lat, t.lon, mode_t, bbox))
                return out
        bbox = None
        if mode_t == "bbox_mean":
            bbox = (min(self.sp_s.value(), self.sp_n.value()), max(self.sp_s.value(), self.sp_n.value()),
                    min(self.sp_w.value(), self.sp_e.value()), max(self.sp_w.value(), self.sp_e.value()))
        return [DO.ExtractionTarget(self.ed_name.text().strip() or "site", self.sp_lat.value(), self.sp_lon.value(),
                                    mode_t, bbox)]

    def _update_estimate(self) -> None:
        n_years = max(0, self.sp_end.value() - self.sp_start.value() + 1)
        nv = max(1, len(self.selected_vars()))
        if self.source in ("chelsa_daily", "chirps_daily"):
            n = n_years * 365 * nv
            self.lbl_est.setText(f"About {n:,} daily files will be read (~3 small requests each, in parallel). "
                                 f"Expect roughly {max(1, n // 2500)}-{max(2, n // 800)} min; values are cached, so a "
                                 f"repeated run is fast.")
        elif self.source in ("chelsa_monthly", "chirps_monthly"):
            self.lbl_est.setText(f"About {n_years * 12 * nv:,} monthly files will be read (only the tiles "
                                 f"around the point).")
        elif self.source == "gpcc":
            self.lbl_est.setText("5-year packages are downloaded one at a time (0.5 deg: ~50 MB each in total "
                                 "per resolution folder) and deleted after extraction.")
        elif self.source in ("scpdsi", "speibase"):
            self.lbl_est.setText("One global NetCDF file is downloaded (hundreds of MB) and deleted after extraction.")
        elif self.source == "openmeteo":
            self.lbl_est.setText(f"{(n_years + 9) // 10} request(s) to Open-Meteo per point (10 years each).")
        elif self.source == "ghcnd":
            self.lbl_est.setText("The station list (~40 MB) is downloaded once and cached; each station file "
                                 "is small.")
        elif self.source == "glofas":
            n_t = len(self.targets())
            self.lbl_est.setText(f"{n_years * n_t} request(s) to the EWDS queue (one per year and point, a few at a "
                                 f"time). Each usually takes 1-5 min in the queue; files are cached. "
                                 + _key_status_text("ewds"))
        elif S.SOURCES[self.source].key_service:
            nreq = len({n for v in self.selected_vars() for n in DC.DATASETS[self.source].variables[v].request_names}) \
                if DC is not None else nv
            self.lbl_est.setText(f"{nreq} request(s) to the CDS queue (one per variable, all years at once, small area "
                                 f"around the point). Usually a few minutes; files are cached. "
                                 + _key_status_text("cds"))
        else:
            self.lbl_est.setText("")

    # ------------------------------------------------------------ map
    def update_map(self) -> None:
        h = self.host
        info = S.SOURCES[self.source]
        ts = self.targets()
        main = ts[0]
        sites = [(t.lat, t.lon, t.name) for t in ts[1:]]
        cells: List[DO.GridCell] = []
        res = self._cell_res()
        if info.kind == "station":
            sites = [(st.lat, st.lon, st.station_id) for st in self._checked_stations() or self._stations]
        elif info.kind == "region":
            res = 0.0
        else:
            key = self._target_key(main)
            if key in self._last_cells and self._last_cells[key][2] == self.source:
                cells, size, _src = self._last_cells[key]
                if size:
                    res = float(size[0])
            elif res > 0:
                try:
                    if main.mode == "bbox_mean" and main.bbox:
                        a = main.bbox
                        n_est = ((a[1] - a[0]) / res + 1) * ((a[3] - a[2]) / res + 1)
                        if n_est <= 3000:
                            lats, lons = DO.regular_grid_axes(res)
                            cells = DO.cells_for_target(main, lats, lons)
                    else:
                        lats, lons = DO.regular_grid_axes(res)
                        cells = DO.cells_for_target(main, lats, lons)
                except ValueError:
                    cells = []
        title = None
        if info.kind == "region":
            c = DO.find_country(main.lat, main.lon)
            region = self.ed_region.text().strip() or (c["name"] if c else "?")
            title = f"Berkeley Earth region: {region} | {main.lat:.3f} N, {main.lon:.3f} E"
        h.map_widget.draw_map(lat=main.lat, lon=main.lon, cells=cells, bbox=main.bbox, sites=sites,
                              grid_res=res, title=title)

    @staticmethod
    def _target_key(t: DO.ExtractionTarget) -> str:
        return f"{t.lat:.4f}|{t.lon:.4f}|{t.mode}|{t.bbox}"

    # ------------------------------------------------------------ stations
    def find_stations(self) -> None:
        els = self.selected_vars() or ["TMAX", "PRCP"]
        lat, lon = self.sp_lat.value(), self.sp_lon.value()
        radius, minyears = self.sp_radius.value(), self.sp_minyears.value()
        y0, y1 = self.sp_start.value(), self.sp_end.value()

        def task(progress, cancel):
            progress("search", 0, 0, "Loading the GHCN-Daily station list ...")
            return S.ghcnd_search(lat, lon, radius, els, y0, y1, minyears, progress, cancel)

        self.host._run_task(task, self._on_stations, "Searching GHCN-Daily stations ...")

    def _on_stations(self, stations: List[S.Station]) -> None:
        self._stations = list(stations)
        self.tbl_st.blockSignals(True)
        self.tbl_st.setRowCount(0)
        for k, st in enumerate(self._stations):
            self.tbl_st.insertRow(k)
            data = ", ".join(f"{e} {a}-{b}" for e, (a, b) in sorted(st.elements.items())
                             if e in ("TMAX", "TMIN", "TAVG", "PRCP", "SNWD", "SNOW"))
            vals = [st.station_id, st.name, st.dist_km, st.elevation_m, f"{st.lat:.3f}, {st.lon:.3f}", data]
            for c, v in enumerate(vals):
                it = QTableWidgetItem("" if v is None else str(v))
                if c == 0:
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(Qt.Checked if k < 3 else Qt.Unchecked)
                    it.setData(Qt.UserRole, st)
                self.tbl_st.setItem(k, c, it)
        self.tbl_st.blockSignals(False)
        self.host._log(f"GHCN-Daily: {len(self._stations)} station(s) found")
        if not self._stations:
            QMessageBox.information(self, "Stations", "No station with enough data was found. Increase the radius "
                                                      "or lower 'Min years'.")
        self.host._schedule_map()

    def _checked_stations(self) -> List[S.Station]:
        out = []
        for r in range(self.tbl_st.rowCount()):
            it = self.tbl_st.item(r, 0)
            if it is not None and it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    # ------------------------------------------------------------ run
    def run(self) -> None:
        src = self.source
        info = S.SOURCES[src]
        variables = self.selected_vars()
        if not variables:
            QMessageBox.information(self, info.label, "Tick at least one variable.")
            return
        y0, y1 = self.sp_start.value(), self.sp_end.value()
        if y0 > y1:
            QMessageBox.warning(self, info.label, "Start year must not be after end year.")
            return
        targets = self.targets()
        try:
            for t in targets:
                t.validate()
        except ValueError as exc:
            QMessageBox.warning(self, info.label, str(exc))
            return
        if info.key_service:
            if DC is None:
                QMessageBox.warning(self, info.label, "dendro_copernicus.py is missing.")
                return
            if not DC.get_key(info.key_service)[0]:
                QMessageBox.information(self, info.label, f"{info.label} needs your own free Copernicus key. "
                                                          "Enter it in the next window.")
                ApiKeysDialog(self).exec()
                if not DC.get_key(info.key_service)[0]:
                    return
        opts: Dict[str, Any] = {
            "gpcc_resolution": self.cmb_gpcc.currentData(), "model": self.cmb_model.currentData(),
            "base": self.cmb_base.currentData(), "region": self.ed_region.text().strip(),
            "glofas_snap_cells": self.sp_snap.value(),
        }
        if info.kind == "station":
            st = self._checked_stations()
            if not st:
                QMessageBox.information(self, info.label, "Find stations and tick one or more of them first.")
                return
            opts["stations"] = st
        if src in ("chelsa_daily", "chirps_daily") and (y1 - y0 + 1) * len(variables) > 30:
            if QMessageBox.question(self, info.label,
                                    f"{(y1 - y0 + 1) * 365 * len(variables):,} daily files will be read. This can take "
                                    "a long time (you can cancel at any moment; values read so far are cached).\n\n"
                                    "Continue?") != QMessageBox.Yes:
                return
        if src in ("scpdsi", "speibase") and QMessageBox.question(
                self, info.label, "A large global NetCDF file (hundreds of MB) will be downloaded"
                + (" and kept." if self.chk_keep.isChecked() else " and deleted after extraction.")
                + "\n\nContinue?") != QMessageBox.Yes:
            return
        req = S.SourceRequest(src, variables, targets, y0, y1, opts,
                              cache_dir=self.host.cru_cache.text().strip() or None,
                              keep_downloads=self.chk_keep.isChecked())
        self.host._run_task(lambda p, c: S.run_source_request(req, p, c), self._on_done,
                            f"{info.label}: reading {', '.join(variables)} ...")

    def _on_done(self, res: S.SourceResult) -> None:
        h = self.host
        src = res.request.source
        info = S.SOURCES[src]
        for line in res.log:
            h._log(line)
        datasets, warnings, cells = source_result_to_datasets(res, self.cmb_daily.currentData() or "both",
                                                              h._month_names)
        for key, val in cells.items():
            self._last_cells[key] = val
        h._emit_datasets(datasets)
        h._update_export_enabled()
        self.host._schedule_map()
        text = f"Added {len(datasets)} dataset(s) from {res.label}."
        if src == "openmeteo":
            text += "\n\nData: ERA5 via Open-Meteo.com (CC BY 4.0) - please cite as shown in the dataset metadata."
        if warnings:
            text += "\n\nWarnings:\n- " + "\n- ".join(warnings[:20])
            for w in warnings:
                h._log("WARNING: " + w)
        QMessageBox.information(self, info.label, text)

    # ------------------------------------------------------------ settings
    def update_resume_button(self) -> None:
        n = sum(len(e.get("jobs", [])) for e in self.host._settings.get("paused_batches", []))
        self.btn_resume.setText(f"Resume paused downloads ({n})")
        self.btn_resume.setVisible(n > 0)

    def apply_settings(self, s: Dict[str, Any]) -> None:
        QTimer.singleShot(0, self.update_resume_button)
        idx = self.cmb_source.findData(s.get("src_source"))
        if idx >= 0:
            self.cmb_source.setCurrentIndex(idx)
        for key, w in (("src_lat", self.sp_lat), ("src_lon", self.sp_lon), ("src_s", self.sp_s), ("src_n", self.sp_n),
                       ("src_w", self.sp_w), ("src_e", self.sp_e)):
            if key in s:
                try:
                    w.setValue(float(s[key]))
                except (TypeError, ValueError):
                    pass
        for key, w in (("src_start", self.sp_start), ("src_end", self.sp_end), ("src_radius", self.sp_radius)):
            if key in s:
                try:
                    w.setValue(int(s[key]))
                except (TypeError, ValueError):
                    pass
        if s.get("src_name"):
            self.ed_name.setText(str(s["src_name"]))

    def get_settings(self) -> Dict[str, Any]:
        return {"src_source": self.source, "src_lat": self.sp_lat.value(), "src_lon": self.sp_lon.value(),
                "src_s": self.sp_s.value(), "src_n": self.sp_n.value(), "src_w": self.sp_w.value(),
                "src_e": self.sp_e.value(), "src_start": self.sp_start.value(), "src_end": self.sp_end.value(),
                "src_radius": self.sp_radius.value(), "src_name": self.ed_name.text().strip()}


# ===========================================================================
# API keys (Copernicus CDS / EWDS) - every user enters his or her own key
# ===========================================================================

class ApiKeysDialog(QDialog):
    """Enter, test and save the user's own Copernicus personal access tokens."""

    DATASET_LINKS = (
        ("cds", "ERA5 monthly", "reanalysis-era5-single-levels-monthly-means"),
        ("cds", "ERA5-Land monthly", "reanalysis-era5-land-monthly-means"),
        ("ewds", "GloFAS historical", "cems-glofas-historical"),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API keys - Copernicus data stores")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        intro = QLabel(
            "ERA5, ERA5-Land and GloFAS come from the Copernicus data stores. They are free, but every user needs "
            "<b>his or her own</b> account and key (personal access token):"
            "<ol><li>Register (free) on the data store web site and log in.</li>"
            "<li>Open your profile page, copy the <i>Personal Access Token</i> and paste it below.</li>"
            "<li>Accept the licence of each data set once (links below, tab 'Download', bottom of the page).</li></ol>"
            "The key is saved only on this computer, in your user settings folder - it is never put into the "
            "program, projects or exported files. CDS and EWDS use the same ECMWF account: if the EWDS field is "
            "empty, the CDS key is tried.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        self._edits: Dict[str, QLineEdit] = {}
        self._status: Dict[str, QLabel] = {}
        for sk, svc in (DC.SERVICES.items() if DC is not None else []):
            g = QGroupBox(svc.label)
            gl = QGridLayout(g)
            key, origin = DC.get_key(sk)
            saved = DC._read_keys_file().get(sk, {}).get("key", "")
            ed = QLineEdit(saved)
            ed.setEchoMode(QLineEdit.Password)
            ed.setPlaceholderText("paste your personal access token here"
                                  if not key else f"using {DC.mask_key(key)} ({origin})")
            ed.setToolTip("Personal access token from your profile page on the data store web site.")
            chk = QCheckBox("Show")
            chk.setToolTip("Show the key in plain text.")
            chk.toggled.connect(lambda on, e=ed: e.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
            b_reg = QPushButton("Register / log in")
            b_reg.setToolTip("Open the data store web site (free registration).")
            b_reg.clicked.connect(lambda *_a, u=svc.register_url: QDesktopServices.openUrl(QUrl(u)))
            b_tok = QPushButton("Get my key")
            b_tok.setToolTip("Open your profile page with the personal access token (log in first).")
            b_tok.clicked.connect(lambda *_a, u=svc.token_url: QDesktopServices.openUrl(QUrl(u)))
            b_test = QPushButton("Test")
            b_test.setToolTip("Check the key with the data store (internet needed).")
            b_test.clicked.connect(lambda *_a, k=sk: self._test(k))
            st = QLabel(f"Current: {DC.mask_key(key)} ({origin})" if key else "No key yet.")
            st.setWordWrap(True)
            gl.addWidget(QLabel("Key:"), 0, 0)
            gl.addWidget(ed, 0, 1)
            gl.addWidget(chk, 0, 2)
            gl.addWidget(b_test, 0, 3)
            gl.addWidget(b_reg, 1, 1, Qt.AlignLeft)
            gl.addWidget(b_tok, 1, 3)
            gl.addWidget(st, 2, 0, 1, 4)
            self._edits[sk] = ed
            self._status[sk] = st
            lay.addWidget(g)
        lic = QGroupBox("Licences (accept once per data set, while logged in)")
        ll = QHBoxLayout(lic)
        for sk, label, ds in self.DATASET_LINKS:
            b = QPushButton(label)
            b.setToolTip(f"Open the '{ds}' page; accept the licence at the bottom of the 'Download' tab.")
            if DC is not None:
                b.clicked.connect(lambda *_a, u=DC.licence_url(sk, ds): QDesktopServices.openUrl(QUrl(u)))
            ll.addWidget(b)
        lay.addWidget(lic)
        path = QLabel(f"Keys file: {DC.keys_path() if DC is not None else '-'}")
        path.setStyleSheet("color: #666;")
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(path)
        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        b_save = QPushButton("Save")
        b_save.setToolTip("Save the keys on this computer (an empty field removes the saved key).")
        b_save.clicked.connect(self._save)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.reject)
        btns.addWidget(b_save)
        btns.addWidget(b_close)
        lay.addLayout(btns)

    def _test(self, sk: str) -> None:
        from PySide6.QtWidgets import QApplication

        key = self._edits[sk].text().strip() or DC.get_key(sk)[0]
        self._status[sk].setText("Testing ...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, msg = DC.check_key(sk, key)
        finally:
            QApplication.restoreOverrideCursor()
        import html as _html
        import re as _re

        text = ("OK: " if ok else "Problem: ") + msg
        urls = _re.findall(r"https?://[^\s)]+", text)
        rich = _html.escape(text)
        for u in urls:
            eu = _html.escape(u)
            rich = rich.replace(eu, f'<a href="{eu}">{eu}</a>')
        if not ok and "polic" in msg.lower():
            rich += ("<br><b>Open the link, log in with the same account (do not register again) and accept the "
                     "terms at the bottom of the page; then press Test again.</b>")
        self._status[sk].setTextFormat(Qt.RichText)
        self._status[sk].setOpenExternalLinks(True)
        self._status[sk].setTextInteractionFlags(Qt.TextBrowserInteraction)
        self._status[sk].setText(rich)
        self._status[sk].setStyleSheet("color: #1a7f37;" if ok else "color: #b00020;")

    def _save(self) -> None:
        try:
            for sk, ed in self._edits.items():
                DC.save_key(sk, ed.text().strip())
        except OSError as exc:
            QMessageBox.warning(self, "API keys", f"Could not save the keys: {exc}")
            return
        self.accept()


# ===========================================================================
# Export of locations
# ===========================================================================

class ExportLocationsDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, default_dir: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Export locations of downloaded data")
        self.resize(820, 600)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Downloads and extractions (tick the ones to export):"))
        top.addStretch(1)
        self.chk_session = QCheckBox("This session only")
        self.chk_session.setToolTip("Show only what was downloaded since the program was started.")
        self.chk_session.toggled.connect(self._fill)
        top.addWidget(self.chk_session)
        lay.addLayout(top)
        self.lst = QListWidget()
        lay.addWidget(self.lst, 1)
        sel = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._check_all(True))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self._check_all(False))
        b_rm = QPushButton("Remove from list")
        b_rm.setToolTip("Remove the ticked items from the download list (the downloaded files are not deleted).")
        b_rm.clicked.connect(self._remove)
        for b in (b_all, b_none, b_rm):
            sel.addWidget(b)
        sel.addStretch(1)
        lay.addLayout(sel)

        g = QGroupBox("Layers")
        gl = QGridLayout(g)
        self.layer_checks: Dict[str, QCheckBox] = {}
        for k, (key, label) in enumerate(GX.LAYERS.items()):
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.layer_checks[key] = cb
            gl.addWidget(cb, k // 3, k % 3)
        lay.addWidget(g)
        f = QGroupBox("Formats")
        fl = QGridLayout(f)
        self.fmt_checks: Dict[str, QCheckBox] = {}
        for k, (key, label) in enumerate(GX.FORMATS.items()):
            cb = QCheckBox(label)
            cb.setChecked(key in ("csv", "kmz"))
            self.fmt_checks[key] = cb
            fl.addWidget(cb, k // 2, k % 2)
        b_allf = QPushButton("All formats")
        b_allf.clicked.connect(lambda: [cb.setChecked(True) for cb in self.fmt_checks.values()])
        fl.addWidget(b_allf, 2, 0)
        lay.addWidget(f)
        o = QGridLayout()
        self.ed_dir = QLineEdit(default_dir or os.path.expanduser("~"))
        b_dir = QPushButton("Browse...")
        b_dir.clicked.connect(self._browse)
        self.ed_name = QLineEdit("downloaded_locations")
        o.addWidget(QLabel("Folder:"), 0, 0)
        o.addWidget(self.ed_dir, 0, 1)
        o.addWidget(b_dir, 0, 2)
        o.addWidget(QLabel("File name:"), 1, 0)
        o.addWidget(self.ed_name, 1, 1)
        lay.addLayout(o)
        btns = QHBoxLayout()
        btns.addStretch(1)
        b_exp = QPushButton("Export")
        b_exp.setToolTip("Write the selected layers in the selected formats.")
        b_exp.clicked.connect(self._export)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close)
        btns.addWidget(b_exp)
        btns.addWidget(b_close)
        lay.addLayout(btns)
        self._fill()

    def _fill(self, *args: Any) -> None:
        self.lst.clear()
        for gid, label, n in GX.registry.groups(this_session_only=self.chk_session.isChecked()):
            it = QListWidgetItem(f"{label}   [{n} feature(s)]")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, gid)
            self.lst.addItem(it)

    def _check_all(self, on: bool) -> None:
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _groups(self) -> List[str]:
        return [self.lst.item(i).data(Qt.UserRole) for i in range(self.lst.count())
                if self.lst.item(i).checkState() == Qt.Checked]

    def _remove(self) -> None:
        g = self._groups()
        if g and QMessageBox.question(self, "Export locations",
                                      f"Remove {len(g)} item(s) from the list?\n(Downloaded files are not deleted.)"
                                      ) == QMessageBox.Yes:
            GX.registry.remove_groups(g)
            self._fill()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for the exported files", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    def _export(self) -> None:
        groups = set(self._groups())
        layers = [k for k, cb in self.layer_checks.items() if cb.isChecked()]
        fmts = [k for k, cb in self.fmt_checks.items() if cb.isChecked()]
        recs = [r for r in GX.registry.records(layers) if r.get("group") in groups]
        if not recs:
            QMessageBox.information(self, "Export locations", "Nothing selected to export.")
            return
        try:
            files = GX.export_records(recs, self.ed_dir.text().strip() or os.path.expanduser("~"),
                                      self.ed_name.text().strip() or "downloaded_locations", fmts)
        except Exception as exc:
            QMessageBox.warning(self, "Export locations", str(exc))
            return
        folder = os.path.dirname(files[0])
        try:
            import dendro_ui_help as _uh

            _uh.qt_notify_saved(self, "Export locations", f"{len(files)} file(s) written.", folder)
            return
        except Exception:
            pass
        box = QMessageBox(self)
        box.setWindowTitle("Export locations")
        box.setText(f"{len(files)} file(s) written to\n{folder}")
        b_open = box.addButton("Open folder", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is b_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def ask_keep_cache_qt(parent: Optional[QWidget] = None) -> None:
    """At program exit: keep the download list and cached values for the next start?"""
    try:
        if not (GX.registry.count() or GX.session_cache_has_content()):
            return
        ans = QMessageBox.question(
            parent, "Online data cache",
            "Keep the cache for the next start?\n\n"
            f"{GX.session_cache_summary()}.\n\n"
            "Yes: the list of downloaded items (for 'Export locations') and cached values are kept.\n"
            "No: they are deleted now.\n\nYour downloaded data files are not affected.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans == QMessageBox.No:
            GX.clear_session_cache()
    except Exception:
        pass


# ===========================================================================
# Batch: everything for one area / period in one run
# ===========================================================================

# variables ticked by the "Recommended" preset ("cru" = CRU TS tab, "itrdb" = chronologies in the area)
RECOMMENDED: Dict[str, List[str]] = {
    "cru": ["tmp", "tmn", "tmx", "pre", "pet", "vap"],
    "terraclimate": [v.code for v in S.SOURCES["terraclimate"].variables],
    "spei_calc": ["spei1", "spei3", "spei6", "spei12", "spi3", "spi12"],
    "chelsa_monthly": ["tas", "tasmax", "tasmin", "pr", "pet", "cmi", "vpd", "hurs"],
    "gpcc": ["precip"],
    "chirps_monthly": ["precip"],
    "berkeley": ["TAVG", "TMAX", "TMIN"],
    "ghcnd": ["TMAX", "TMIN", "PRCP"],
    "openmeteo": ["temperature_2m_mean", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                  "et0_fao_evapotranspiration", "relative_humidity_2m_mean", "soil_moisture_0_to_100cm_mean"],
    "itrdb": ["std"],
}
HEAVY = {"chelsa_daily", "chirps_daily", "scpdsi", "speibase"}
_TEMP_PREC = {"Temperature (mean)", "Temperature (min)", "Temperature (max)", "Precipitation"}
_DROUGHT = {"PDSI", "SPEI", "SPI", "Climatic water deficit", "Actual evapotranspiration", "PET", "Soil moisture",
            "Climate moisture index", "Vapour pressure deficit", "Runoff", "Snow"}


def _batch_catalog() -> List[tuple]:
    """[(job key, label, [(var code, label, ringclim param)], period)] in display order."""
    out = [("itrdb", "ITRDB chronologies in the area (standard total ring width, .crn)",
            [("std", "Standard chronologies", "chronology")], (0, 2100)),
           ("cru", "CRU TS (version of the CRU TS tab)",
            [(c, f"{v.label} ({v.units})", v.ringclim_param) for c, v in DO.CRU_VARIABLES.items()], (1901, 2100))]
    for key, info in S.SOURCES.items():
        out.append((key, info.label, [(v.code, f"{v.label} ({v.units})", v.ringclim_param) for v in info.variables],
                    info.period))
    return out


# Jobs on the same server run one after the other; different servers run in parallel.
SERVER_GROUP = {
    "itrdb": "NOAA NCEI", "ghcnd": "NOAA NCEI", "cru": "CRU (UEA)", "scpdsi": "CRU (UEA)",
    "chelsa_monthly": "CHELSA (WSL)", "chelsa_daily": "CHELSA (WSL)", "terraclimate": "TerraClimate (THREDDS)",
    "gpcc": "GPCC (DWD)", "chirps_monthly": "CHIRPS (UCSB)", "chirps_daily": "CHIRPS (UCSB)",
    "berkeley": "Berkeley Earth", "openmeteo": "Open-Meteo", "speibase": "SPEIbase (CSIC)",
    "era5_cds_monthly": "Copernicus CDS", "era5land_cds_monthly": "Copernicus CDS", "glofas": "Copernicus EWDS",
}
# variables ticked by the "Copernicus" preset
COPERNICUS_PRESET: Dict[str, List[str]] = {
    "era5land_cds_monthly": ["t2m", "tp", "pev", "e", "vpd", "swvl1", "swvl3", "sd"],
    "era5_cds_monthly": ["t2m", "tp"],
    "glofas": ["dis24"],
}


def _key_ok(source_key: str) -> bool:
    info = S.SOURCES.get(source_key)
    if info is None or not info.key_service:
        return True
    return DC is not None and bool(DC.get_key(info.key_service)[0])


def _key_status_text(service: str) -> str:
    if DC is None:
        return ""
    key, origin = DC.get_key(service)
    return f"Key: {DC.mask_key(key)} ({origin})." if key else "No key yet: press 'API keys...'."
# jobs that download large whole files: at most two at the same time (the internet line is the limit)
LARGE_FILE_JOBS = {"cru", "gpcc", "scpdsi", "speibase"}


def _job_groups(jobs: List[Dict[str, Any]], opts: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """Jobs grouped by server, in a safe order inside each group (SPEI after its P / PET source)."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        key = job["key"]
        if key == "spei_calc":  # reads TerraClimate / CHELSA: same group, after them (re-uses their values)
            grp = SERVER_GROUP.get(opts.get("spei_base", "terraclimate"), "spei")
        else:
            grp = SERVER_GROUP.get(key, key)
        groups.setdefault(grp, []).append(job)
    for grp in groups.values():
        grp.sort(key=lambda j: j["key"] == "spei_calc")
    # longest groups first, so that they start immediately
    weight = {"cru": 5, "gpcc": 3, "scpdsi": 4, "speibase": 6, "chelsa_daily": 8, "chirps_daily": 8,
              "chelsa_monthly": 3, "chirps_monthly": 3, "itrdb": 2, "glofas": 8, "era5_cds_monthly": 4,
              "era5land_cds_monthly": 4}
    return sorted(groups.values(), key=lambda g: -sum(weight.get(j["key"], 1) * len(j["vars"]) for j in g))


def _run_one_job(job: Dict[str, Any], targets: List[DO.ExtractionTarget], y0: int, y1: int,
                 opts: Dict[str, Any], prog, cancel) -> Dict[str, Any]:
    import time as _time

    key, variables = job["key"], job["vars"]
    lo, hi = job["period"]
    a, b = max(y0, lo), min(y1, hi)
    out: Dict[str, Any] = {"key": key, "label": job["label"], "vars": variables, "years": (a, b)}
    t0 = _time.time()
    prog("running", 0, 0, "starting ...")
    try:
        if a > b:
            raise DO.OnlineError(f"no overlap with the period ({lo}-{hi})")
        if key == "itrdb":
            out["result"] = _batch_itrdb(targets, a, b, opts, prog, cancel)
        elif key == "cru":
            req = DO.CruRequest(version=opts["cru_version"], variables=variables, targets=list(targets),
                                start_year=a, end_year=b, cache_dir=opts.get("cache_dir"), keep_downloads=False)
            out["result"] = DO.run_cru_request(req, progress=prog, cancel=cancel)
        else:
            info = S.SOURCES[key]
            tg = list(targets)
            if info.kind in ("region", "station") or "bbox_mean" not in info.modes:
                tg = [DO.ExtractionTarget(t.name, t.lat, t.lon, "nearest") for t in targets]
            if info.kind == "region":  # one series per country
                seen, uniq = set(), []
                for t in tg:
                    c = DO.find_country(t.lat, t.lon)
                    name = c["name"] if c else t.name
                    if name not in seen:
                        seen.add(name)
                        uniq.append(t)
                tg = uniq
            o = {"gpcc_resolution": opts.get("gpcc_resolution", "0.5"), "model": opts.get("model", "era5"),
                 "base": opts.get("spei_base", "terraclimate"), "workers": opts.get("inner_workers", 8),
                 "glofas_snap_cells": opts.get("glofas_snap_cells", 3)}
            if info.kind == "station":
                stations: Dict[str, S.Station] = {}
                for t in tg:
                    found = S.ghcnd_search(t.lat, t.lon, opts.get("station_radius", 100), variables, a, b,
                                           opts.get("station_min_years", 20), prog, cancel)
                    for st in found[:opts.get("stations_per_point", 3)]:
                        stations.setdefault(st.station_id, st)
                if not stations:
                    raise DO.OnlineError("no station with enough data near the point(s)")
                o["stations"] = list(stations.values())
            req = S.SourceRequest(key, variables, tg, a, b, o, cache_dir=opts.get("cache_dir"),
                                  keep_downloads=False)
            out["result"] = S.run_source_request(req, prog, cancel)
    except DO.CancelledError:
        raise
    except Exception as exc:
        out["error"] = str(exc).split("\n")[0][:400]
    out["seconds"] = round(_time.time() - t0, 1)
    return out


class BatchPause:
    """Thread-safe 'pause' switches: everything not finished yet, or single sources."""

    def __init__(self) -> None:
        import threading as _th

        self.all = _th.Event()
        self.keys: set = set()

    def pause_all(self) -> None:
        self.all.set()

    def pause_key(self, key: str) -> None:
        self.keys.add(key)

    def is_paused(self, key: str) -> bool:
        return self.all.is_set() or key in self.keys


def run_batch_jobs(jobs: List[Dict[str, Any]], targets: List[DO.ExtractionTarget], y0: int, y1: int,
                   opts: Dict[str, Any], progress, cancel, on_result=None,
                   pause: Optional[BatchPause] = None) -> List[Dict[str, Any]]:
    """Run the jobs (worker thread): different servers in parallel, jobs of one server one after the other.

    Progress messages use the stage "job:<key>" (per source) and "batch" (overall), so the GUI can
    show one line per source. Errors are kept per job; the batch continues. ``on_result(outcome)`` is
    called as soon as one source has finished (so the GUI can use it at once). A job may carry its own
    "targets". Paused sources end with {"paused": True} and can be downloaded later.
    """
    import concurrent.futures as cf
    import threading as _th

    parallel = max(1, int(opts.get("parallel", 6)))
    groups = _job_groups(jobs, opts)
    big = _th.Semaphore(2)
    lock = _th.Lock()
    finished = [0]
    outcomes: Dict[int, Dict[str, Any]] = {}
    order = {id(j): k for k, j in enumerate(jobs)}
    for j in jobs:
        progress(f"job:{j['key']}", 0, 0, "queued")

    def run_group(group: List[Dict[str, Any]]) -> None:
        for job in group:
            if cancel():
                return
            key = job["key"]

            def prog(stage, done, total, msg, key=key):
                progress(f"job:{key}", done, total, str(msg))

            def job_cancel(key=key) -> bool:
                return cancel() or (pause is not None and pause.is_paused(key))

            tg = job.get("targets") or targets
            out: Dict[str, Any]
            try:
                if job_cancel():
                    raise DO.CancelledError("paused")
                if key in LARGE_FILE_JOBS:
                    prog("wait", 0, 0, "waiting for a free download slot ...")
                    with big:
                        if job_cancel():
                            raise DO.CancelledError("paused")
                        out = _run_one_job(job, tg, y0, y1, opts, prog, job_cancel)
                else:
                    out = _run_one_job(job, tg, y0, y1, opts, prog, job_cancel)
                if out.get("error") and pause is not None and pause.is_paused(key) and not cancel():
                    raise DO.CancelledError("paused")
            except DO.CancelledError:
                if cancel():
                    raise
                out = {"key": key, "label": job["label"], "vars": job["vars"], "paused": True, "seconds": 0}
            if out.get("paused"):
                prog("paused", 0, 1, "PAUSED - will be downloaded later (files already loaded stay in the cache)")
            elif out.get("error"):
                prog("error", 1, 1, "ERROR: " + out["error"])
            else:
                prog("done", 1, 1, f"done in {out['seconds']} s")
            if on_result is not None:
                try:
                    on_result(out)
                except Exception:
                    pass
            with lock:
                outcomes[order[id(job)]] = out
                finished[0] += 1
                progress("batch", finished[0], len(jobs), f"Batch: {finished[0]}/{len(jobs)} source(s) finished")

    progress("batch", 0, len(jobs), f"Batch: {len(jobs)} source(s) on {len(groups)} server(s), "
                                    f"{min(parallel, len(groups))} in parallel")
    with cf.ThreadPoolExecutor(max_workers=min(parallel, max(1, len(groups)))) as ex:
        futures = [ex.submit(run_group, g) for g in groups]
        errors: List[BaseException] = []
        for f in futures:
            try:
                f.result()
            except DO.CancelledError:
                pass
            except BaseException as exc:  # unexpected: keep going, report below
                errors.append(exc)
    if cancel():
        raise DO.CancelledError("Cancelled")
    if errors:
        raise errors[0]
    progress("batch", len(jobs), len(jobs), "Batch finished")
    return [outcomes[k] for k in sorted(outcomes)]


def _batch_itrdb(targets, y0, y1, opts, progress, cancel) -> Dict[str, Any]:
    import dendro_online_qt as OQ  # lazy: avoids a circular import

    boxes = []
    for t in targets:
        if t.bbox:
            boxes.append(t.bbox)
        else:
            boxes.append(DO.bbox_around(t.lat, t.lon, float(opts.get("itrdb_radius", 150))))
    area = (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes))
    progress("search", 0, 0, "searching ITRDB ...")
    sites = DO.search_itrdb(min_lat=area[0], max_lat=area[1], min_lon=area[2], max_lon=area[3])
    # strict first; then relax (many ITRDB sites have no first / last year in the metadata, or only
    # residual / ARSTAN / other-parameter chronologies)
    tries = [
        DO.ItrdbBatchCriteria(kinds=("crn",), parameters=("Total ring width",), variants=("standard",),
                              min_last_year=y0 + 10, min_span_years=30),
        DO.ItrdbBatchCriteria(kinds=("crn",), parameters=("Total ring width",), variants=("standard",)),
        DO.ItrdbBatchCriteria(kinds=("crn",), parameters=(), variants=("standard",)),
        DO.ItrdbBatchCriteria(kinds=("crn",), parameters=(), variants=()),
    ]
    files: List[Any] = []
    for crit in tries:
        files = DO.select_itrdb_files(sites, crit)
        if files:
            break
    # sites that still have no .crn: build a standard chronology from their Tucson .rwl
    with_crn = {id(s) for s, _ in files}
    rwl_sites = [s for s in sites if id(s) not in with_crn]
    rwl_files: List[Any] = []
    for crit in (DO.ItrdbBatchCriteria(kinds=("rwl",), parameters=("Total ring width",), variants=()),
                 DO.ItrdbBatchCriteria(kinds=("rwl",), parameters=(), variants=())):
        rwl_files = DO.select_itrdb_files(rwl_sites, crit)
        if rwl_files:
            break
    kinds: Dict[str, int] = {}
    for s in sites:
        for f in s.files:
            kinds[f.kind] = kinds.get(f.kind, 0) + 1
    datasets: List[Dict[str, Any]] = []
    messages: List[str] = [
        f"ITRDB: {len(sites)} site(s) in the area, {len(files)} chronology file(s) selected, "
        f"{len(rwl_files)} ring-width file(s) to build chronologies from "
        f"(files found: {', '.join(f'{k} {n}' for k, n in sorted(kinds.items())) or 'none'})"]
    dest = opts.get("itrdb_dir") or os.path.join(DO.default_cache_dir(), "itrdb")
    for k, (s, f) in enumerate(files, start=1):
        if cancel():
            raise DO.CancelledError("Cancelled")
        progress("file", k - 1, len(files), f"{f.filename}")
        try:
            path = DO.download_file(f.url, os.path.join(dest, (s.site_code or s.study_id or "site").lower(),
                                                        f.filename), progress=progress, cancel=cancel)
            GX.registry.add_itrdb_file(s, f, path)
            df, meta = DO.read_tucson_crn(path)
            datasets.extend(OQ._crn_to_datasets(s, f, df, meta, path, int(opts.get("itrdb_min_depth", 5)), messages))
        except DO.CancelledError:
            raise
        except Exception as exc:
            messages.append(f"FAILED {f.filename}: {exc}")
    for k, (s, f) in enumerate(rwl_files, start=1):
        if cancel():
            raise DO.CancelledError("Cancelled")
        progress("file", k - 1, len(rwl_files), f"{f.filename} (building chronology)")
        try:
            path = DO.download_file(f.url, os.path.join(dest, (s.site_code or s.study_id or "site").lower(),
                                                        f.filename), progress=progress, cancel=cancel)
            GX.registry.add_itrdb_file(s, f, path)
            rwl, _meta = DO.read_tucson_rwl(path, zero_as_nan=False)
            crn = rwl_to_chronology(rwl)
            if crn.empty:
                messages.append(f"{f.filename}: no usable series")
                continue
            col = "std"
            df = pd.DataFrame({"Year": crn.index.astype(int), col: crn["index"].values,
                               f"{col}_n": crn["n"].values})
            fake = dataclasses.replace(f, kind="crn", variant="rwlstd")
            new = OQ._crn_to_datasets(s, fake, df, {"header": []}, path, int(opts.get("itrdb_min_depth", 5)),
                                      messages)
            for d in new:
                d["meta"]["chronology_method"] = RWL_CHRON_METHOD
                d["meta"]["warning"] = ("Built by RingClim from the raw ring widths (no .crn in ITRDB): "
                                        + RWL_CHRON_METHOD)
            datasets.extend(new)
            messages.append(f"{s.site_code}: chronology built from {f.filename} ({rwl.shape[1]} series)")
        except DO.CancelledError:
            raise
        except Exception as exc:
            messages.append(f"FAILED {f.filename}: {exc}")
    return {"datasets": datasets, "messages": messages, "area": area}


RWL_CHRON_METHOD = ("each series divided by a fitted modified negative exponential curve (falls back to a "
                    "linear trend with slope <= 0, then to the series mean), Tukey biweight robust mean of "
                    "the indices per year")


def _detrend_curve(t: "np.ndarray", y: "np.ndarray") -> "np.ndarray":
    """Modified negative exponential a*exp(-b*t)+k (b>0, k>=0) like dplR 'ModNegExp'."""
    import numpy as np

    mean = float(np.mean(y))
    try:
        from scipy.optimize import curve_fit

        def f(tt, a, b, k):
            return a * np.exp(-b * tt) + k

        p0 = (max(float(y[: max(3, len(y) // 10)].mean()) - float(y.min()), 1e-3), 0.05, max(float(y.min()), 1e-3))
        p, _ = curve_fit(f, t, y, p0=p0, bounds=([0.0, 1e-6, 0.0], [np.inf, 1.0, np.inf]), maxfev=5000)
        c = f(t, *p)
        if np.all(np.isfinite(c)) and np.all(c > 0):
            return c
    except Exception:
        pass
    slope, icpt = np.polyfit(t, y, 1)
    c = icpt + slope * t
    if slope <= 0 and np.all(c > 0):
        return c
    return np.full_like(y, mean, dtype=float)


def _biweight_mean(x: "np.ndarray", c: float = 9.0, eps: float = 1e-8) -> float:
    import numpy as np

    x = x[np.isfinite(x)]
    if len(x) == 0:
        return float("nan")
    if len(x) < 3:
        return float(np.mean(x))
    m = float(np.median(x))
    for _ in range(10):
        s = float(np.median(np.abs(x - m)))
        u = (x - m) / (c * s + eps)
        w = np.where(np.abs(u) < 1, (1 - u ** 2) ** 2, 0.0)
        if w.sum() <= 0:
            break
        m_new = float((w * x).sum() / w.sum())
        if abs(m_new - m) < 1e-6:
            m = m_new
            break
        m = m_new
    return m


def rwl_to_chronology(rwl: pd.DataFrame, min_len: int = 10) -> pd.DataFrame:
    """Standard chronology from a ring-width table (index = year, one column per series, mm)."""
    import numpy as np

    idx = {}
    for col in rwl.columns:
        s = pd.to_numeric(rwl[col], errors="coerce").dropna()
        if len(s) < min_len or float(s.mean()) <= 0:
            continue
        y = s.values.astype(float)
        t = np.arange(len(y), dtype=float)
        curve = _detrend_curve(t, y)
        idx[col] = pd.Series(y / curve, index=s.index)
    if not idx:
        return pd.DataFrame(columns=["index", "n"])
    tab = pd.DataFrame(idx).sort_index()
    out = pd.DataFrame({
        "index": [_biweight_mean(r) for r in tab.to_numpy()],
        "n": tab.notna().sum(axis=1).values,
    }, index=tab.index)
    return out


def _safe(name: str, n: int = 80) -> str:
    import re as _re

    return (_re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "dataset")[:n]


def _dataset_summary(d: Dict[str, Any]) -> Dict[str, Any]:
    df = d["df"]
    meta = d.get("meta") or {}
    if d["kind"] == "timeseries":
        years = pd.to_datetime(df["DateTime"]).dt.year
        vals = pd.to_numeric(df["Value"], errors="coerce")
    elif d["kind"] == "climate":
        years = df["Year"]
        vals = df.drop(columns=["Year"]).apply(pd.to_numeric, errors="coerce").to_numpy().ravel()
        vals = pd.Series(vals)
    else:
        years = df["Year"]
        vals = pd.to_numeric(df[d.get("value_column") or df.columns[1]], errors="coerce")
    tgt = meta.get("target") or {}
    return {
        "dataset": d["name"], "kind": d["kind"], "source": meta.get("source", ""),
        "variable": meta.get("variable_label") or meta.get("variable") or meta.get("parameter", ""),
        "units": meta.get("units", ""), "climate_type": d.get("climate_param", ""),
        "lat": tgt.get("lat", meta.get("lat")), "lon": tgt.get("lon", meta.get("lon")),
        "method": tgt.get("mode", ""),
        "first_year": int(years.min()) if len(years) else None, "last_year": int(years.max()) if len(years) else None,
        "n_years": int(pd.Series(years).nunique()), "missing_pct": round(100.0 * float(vals.isna().mean()), 1)
        if len(vals) else None, "warning": meta.get("warning", ""), "citation": meta.get("citation", ""),
    }


def save_batch_folder(folder: str, datasets: List[Dict[str, Any]], errors: List[str],
                      export_locations: bool, batch_time: str) -> List[str]:
    """CSV per dataset + one Excel workbook + long table + summary + citations (+ locations)."""
    os.makedirs(os.path.join(folder, "data"), exist_ok=True)
    written: List[str] = []
    rows = [_dataset_summary(d) for d in datasets]
    summary = pd.DataFrame(rows)
    summary.to_csv(os.path.join(folder, "summary.csv"), index=False, encoding="utf-8-sig")
    written.append(os.path.join(folder, "summary.csv"))
    long_parts = []
    names_used: set = set()
    manifest: List[Dict[str, Any]] = []
    for d in datasets:
        base = _safe(d["name"])
        while base in names_used:
            base += "_"
        names_used.add(base)
        p = os.path.join(folder, "data", base + ".csv")
        d["df"].to_csv(p, index=False)
        written.append(p)
        manifest.append({k: d.get(k) for k in ("name", "kind", "climate_param", "value_column", "source_freq",
                                                "monthly_agg") if d.get(k) is not None}
                        | {"file": "data/" + base + ".csv", "meta": d.get("meta") or {}})
        if d["kind"] == "climate":
            m = d["df"].melt(id_vars=["Year"], var_name="Month", value_name="value")
            m.insert(0, "dataset", d["name"])
            long_parts.append(m)
    try:  # lets RingClim re-import the whole folder with all metadata (Data > Import batch folder...)
        import json as _json

        with open(os.path.join(folder, "datasets.json"), "w", encoding="utf-8") as fh:
            _json.dump({"format": "declim-batch-1", "batch_started": batch_time, "datasets": manifest}, fh,
                       indent=1, default=str)
        written.append(os.path.join(folder, "datasets.json"))
    except Exception as exc:
        errors.append(f"datasets.json not written: {exc}")
    if long_parts:
        lp = pd.concat(long_parts, ignore_index=True)
        p = os.path.join(folder, "all_monthly_long.csv")
        lp.to_csv(p, index=False, encoding="utf-8-sig")
        written.append(p)
    # Excel workbook (monthly tables and chronologies; daily series stay in CSV)
    try:
        xp = os.path.join(folder, "batch_data.xlsx")
        with pd.ExcelWriter(xp) as xw:
            summary.to_excel(xw, sheet_name="summary", index=False)
            used = {"summary"}
            for d in datasets:
                if d["kind"] == "timeseries":
                    continue
                sh = _safe(d["name"], 31)
                k = 1
                while sh.lower() in used:
                    k += 1
                    sh = _safe(d["name"], 28) + f"_{k}"
                used.add(sh.lower())
                d["df"].to_excel(xw, sheet_name=sh, index=False)
        written.append(xp)
    except Exception as exc:  # no Excel engine -> CSV files are enough
        errors.append(f"Excel workbook not written: {exc}")
    cites = sorted({r["citation"] for r in rows if r.get("citation")})
    with open(os.path.join(folder, "README_citations.txt"), "w", encoding="utf-8") as fh:
        fh.write("Data downloaded with DeClim / RingClim online tools\n")
        fh.write(f"Batch started: {batch_time}\n\nPlease cite the data sets:\n\n")
        for c in cites:
            fh.write(f"- {c}\n")
        if errors:
            fh.write("\nNot downloaded / problems:\n")
            for e in errors:
                fh.write(f"- {e}\n")
    written.append(os.path.join(folder, "README_citations.txt"))
    if export_locations:
        recs = [r for r in GX.registry.records() if r.get("session") == GX.registry.session_id
                and str(r.get("time", "")) >= batch_time]
        if recs:
            written += GX.export_records(recs, folder, "locations", ["csv", "kmz", "geojson"],
                                         title="Batch download locations")
    return written


class BatchClimateDialog(QDialog):
    """Pick many sources / variables at once for the current location(s) and period."""

    def __init__(self, tab: "SourcesTab") -> None:
        super().__init__(tab)
        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

        self._QTreeWidgetItem = QTreeWidgetItem
        self.tab = tab
        self.setWindowTitle("Download everything for the area")
        self.resize(900, 760)
        lay = QVBoxLayout(self)
        targets = tab.targets()
        t0 = targets[0]
        where = (f"{len(targets)} point(s) from the CRU TS target list" if len(targets) > 1 else
                 (f"box {t0.bbox[0]:.3f}..{t0.bbox[1]:.3f} N, {t0.bbox[2]:.3f}..{t0.bbox[3]:.3f} E"
                  if t0.bbox else f"point {t0.lat:.4f} N, {t0.lon:.4f} E ({t0.mode})"))
        info = QLabel(f"<b>Location:</b> {where} &nbsp; <b>Period:</b> {tab.sp_start.value()}-{tab.sp_end.value()}"
                      "<br>Each source is clipped to its own period. Different servers are used in parallel; sources "
                      "that fail are reported and skipped, the others continue.")
        info.setWordWrap(True)
        lay.addWidget(info)

        top = QHBoxLayout()
        top.addWidget(QLabel("Preset:"))
        self.cmb_preset = QComboBox()
        for key, label in (("rec", "Recommended (fast, no large downloads)"), ("all", "Everything available (slow)"),
                           ("tp", "Temperature and precipitation"), ("drought", "Drought and water balance"),
                           ("copernicus", "Copernicus ERA5-Land / ERA5 / GloFAS (needs key)"),
                           ("none", "Clear all")):
            self.cmb_preset.addItem(label, key)
        self.cmb_preset.setToolTip("Tick a typical set of sources and variables (you can change single ticks).")
        self.cmb_preset.currentIndexChanged.connect(self._apply_preset)
        top.addWidget(self.cmb_preset, 1)
        lay.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Source / variable", "Period", "Note"])
        self.tree.setColumnWidth(0, 470)
        self.tree.setColumnWidth(1, 90)
        self._items: Dict[str, Any] = {}
        for key, label, variables, period in _batch_catalog():
            note = "large download / many files" if key in HEAVY else ""
            if key == "itrdb":
                note = "searched in the box or within the radius"
            if not _key_ok(key):
                note = "needs your Copernicus key (API keys...)"
            elif key in S.SOURCES and S.SOURCES[key].key_service:
                note = "your Copernicus key; waits in the queue"
            top_it = QTreeWidgetItem([label, "" if key == "itrdb" else f"{period[0]}-{period[1]}", note])
            top_it.setFlags(top_it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            top_it.setData(0, Qt.UserRole, (key, period))
            for code, vlabel, _param in variables:
                ch = QTreeWidgetItem([f"{code} - {vlabel}", "", ""])
                ch.setFlags(ch.flags() | Qt.ItemIsUserCheckable)
                ch.setCheckState(0, Qt.Unchecked)
                ch.setData(0, Qt.UserRole, (code, _param))
                top_it.addChild(ch)
            self.tree.addTopLevelItem(top_it)
            self._items[key] = top_it
        lay.addWidget(self.tree, 1)

        opt = QGroupBox("Options")
        og = QGridLayout(opt)
        self.cmb_gpcc = QComboBox()
        for r in ("0.25", "0.5", "1.0", "2.5"):
            self.cmb_gpcc.addItem(f"{r} deg", r)
        self.cmb_gpcc.setCurrentIndex(1)
        self.cmb_model = QComboBox()
        for k, (label, _r) in S.OPENMETEO_MODELS.items():
            self.cmb_model.addItem(label, k)
        self.cmb_model.setCurrentIndex(1)
        self.cmb_base = QComboBox()
        self.cmb_base.addItem("TerraClimate", "terraclimate")
        self.cmb_base.addItem("CHELSA monthly", "chelsa_monthly")
        self.cmb_daily = QComboBox()
        self.cmb_daily.addItem("Monthly table only", "monthly")
        self.cmb_daily.addItem("Daily series + monthly table", "both")
        self.cmb_daily.addItem("Daily series only", "daily")
        self.sp_radius = QSpinBox()
        self.sp_radius.setRange(5, 1000)
        self.sp_radius.setValue(150)
        self.sp_radius.setSuffix(" km")
        self.sp_radius.setToolTip("ITRDB sites and weather stations are searched within this distance of each point.")
        self.sp_nst = QSpinBox()
        self.sp_nst.setRange(1, 20)
        self.sp_nst.setValue(3)
        self.sp_nst.setToolTip("Number of nearest GHCN-Daily stations per point.")
        og.addWidget(QLabel("GPCC grid:"), 0, 0)
        og.addWidget(self.cmb_gpcc, 0, 1)
        og.addWidget(QLabel("ERA5 model:"), 0, 2)
        og.addWidget(self.cmb_model, 0, 3)
        og.addWidget(QLabel("P and PET from:"), 1, 0)
        og.addWidget(self.cmb_base, 1, 1)
        og.addWidget(QLabel("Import daily data as:"), 1, 2)
        og.addWidget(self.cmb_daily, 1, 3)
        self.sp_parallel = QSpinBox()
        self.sp_parallel.setRange(1, 10)
        self.sp_parallel.setValue(6)
        self.sp_parallel.setToolTip("How many servers are used at the same time (1 = one source after the other). "
                                    "Sources on the same server always run one after the other; at most two large "
                                    "file downloads run at the same time.")
        og.addWidget(QLabel("Parallel sources:"), 3, 0)
        og.addWidget(self.sp_parallel, 3, 1)
        og.addWidget(QLabel("Search radius:"), 2, 0)
        og.addWidget(self.sp_radius, 2, 1)
        og.addWidget(QLabel("Stations per point:"), 2, 2)
        og.addWidget(self.sp_nst, 2, 3)
        lay.addWidget(opt)

        out = QGroupBox("Output")
        ol = QGridLayout(out)
        self.chk_select = QCheckBox("Add to the project and select everything in the Analysis tab")
        self.chk_select.setChecked(True)
        self.chk_save = QCheckBox("Save all data to a folder (CSV per dataset, one Excel workbook, long table, "
                                  "summary, citations)")
        self.chk_save.setChecked(True)
        self.chk_save.setToolTip("Write every dataset as CSV, one Excel workbook with all monthly tables, a long table "
                                 "(dataset, year, month, value), a summary table and the citations.")
        self.chk_loc = QCheckBox("Also export the locations (CSV, KMZ, GeoJSON)")
        self.chk_loc.setChecked(True)
        self.ed_dir = QLineEdit(tab.host._settings.get("batch_dir") or os.path.join(os.path.expanduser("~"),
                                                                                    "DeClim_batch"))
        b_dir = QPushButton("Browse...")
        b_dir.setToolTip("Folder for the batch data (a sub-folder per batch is created).")
        b_dir.clicked.connect(self._browse)
        ol.addWidget(self.chk_select, 0, 0, 1, 3)
        ol.addWidget(self.chk_save, 1, 0, 1, 3)
        ol.addWidget(self.chk_loc, 2, 0, 1, 3)
        ol.addWidget(QLabel("Folder:"), 3, 0)
        ol.addWidget(self.ed_dir, 3, 1)
        ol.addWidget(b_dir, 3, 2)
        lay.addWidget(out)

        self.lbl_est = QLabel("")
        self.lbl_est.setWordWrap(True)
        self.lbl_est.setStyleSheet("color: #555;")
        lay.addWidget(self.lbl_est)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_start = QPushButton("Start batch download")
        self.btn_start.setToolTip("Download everything ticked, one source after the other, in the background.")
        self.btn_start.clicked.connect(self.accept)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_start)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)
        self.tree.itemChanged.connect(lambda *a: self._update_estimate())
        self._apply_preset()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for the batch data", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    def _apply_preset(self, *args: Any) -> None:
        preset = self.cmb_preset.currentData()
        self.tree.blockSignals(True)
        for key, top_it in self._items.items():
            for i in range(top_it.childCount()):
                ch = top_it.child(i)
                code, param = ch.data(0, Qt.UserRole)
                if preset == "rec":
                    on = code in RECOMMENDED.get(key, [])
                elif preset == "copernicus":
                    on = code in COPERNICUS_PRESET.get(key, []) and _key_ok(key)
                elif preset == "all":
                    on = _key_ok(key)
                elif preset == "tp":
                    on = key not in HEAVY and key != "itrdb" and param in _TEMP_PREC and _key_ok(key)
                elif preset == "drought":
                    on = key not in HEAVY and key != "itrdb" and param in _DROUGHT and _key_ok(key)
                else:
                    on = False
                if key == "itrdb" and preset in ("tp", "drought"):
                    on = True
                ch.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        self.tree.blockSignals(False)
        for top_it in self._items.values():  # refresh tristate of the parents
            if top_it.childCount():
                top_it.child(0).setCheckState(0, top_it.child(0).checkState(0))
        self._update_estimate()

    def jobs(self) -> List[Dict[str, Any]]:
        out = []
        for key, top_it in self._items.items():
            codes = [top_it.child(i).data(0, Qt.UserRole)[0] for i in range(top_it.childCount())
                     if top_it.child(i).checkState(0) == Qt.Checked]
            if codes:
                out.append({"key": key, "label": top_it.text(0), "vars": codes,
                            "period": top_it.data(0, Qt.UserRole)[1]})
        return out

    def _update_estimate(self) -> None:
        jobs = self.jobs()
        nvar = sum(len(j["vars"]) for j in jobs)
        heavy = [j["label"] for j in jobs if j["key"] in HEAVY]
        text = f"{len(jobs)} source(s), {nvar} variable(s) ticked."
        if heavy:
            text += " Slow / large: " + "; ".join(heavy) + "."
        self.lbl_est.setText(text)
        self.btn_start.setEnabled(bool(jobs))

    def options(self) -> Dict[str, Any]:
        h = self.tab.host
        return {
            "gpcc_resolution": self.cmb_gpcc.currentData(), "model": self.cmb_model.currentData(),
            "spei_base": self.cmb_base.currentData(), "daily_as": self.cmb_daily.currentData(),
            "station_radius": self.sp_radius.value(), "itrdb_radius": self.sp_radius.value(),
            "stations_per_point": self.sp_nst.value(), "station_min_years": 20,
            "parallel": self.sp_parallel.value(), "inner_workers": 16, "glofas_snap_cells": 3,
            "cru_version": h.cmb_version.currentText(), "cache_dir": h.cru_cache.text().strip() or None,
            "itrdb_dir": h.it_dir.text().strip(), "itrdb_min_depth": h.it_min_depth.value(),
            "select": self.chk_select.isChecked(), "save": self.chk_save.isChecked(),
            "locations": self.chk_loc.isChecked(), "folder": self.ed_dir.text().strip(),
        }


def suggest_fixes(job: Dict[str, Any], error: str, targets: List[DO.ExtractionTarget]) -> List[Dict[str, Any]]:
    """What can be done about a failed source: [{label, targets, opts, needs_key}] (first = recommended)."""
    err = str(error or "").lower()
    fixes: List[Dict[str, Any]] = []
    key = job["key"]
    if "pixels" in err or "smaller box" in err:
        pts = [DO.ExtractionTarget(t.name, t.lat, t.lon, "nearest") for t in targets]
        fixes.append({"label": "Retry for the point (nearest pixel of each location)", "targets": pts})
        cell = S.SOURCES[key].cell_deg if key in S.SOURCES and S.SOURCES[key].cell_deg else 1 / 120
        half = max(cell, (math.sqrt(18000) * cell) / 2.0)
        boxes = []
        for t in targets:
            boxes.append(DO.ExtractionTarget(t.name, t.lat, t.lon, "bbox_mean",
                                             (t.lat - half, t.lat + half, t.lon - half, t.lon + half)))
        km = int(2 * half * 111)
        fixes.append({"label": f"Retry with the largest possible box (about {km} x {km} km around each location)",
                      "targets": boxes})
    elif "licen" in err and "accepted" in err:
        import re as _re

        m = _re.search(r"https://\S+", str(error))
        url = m.group(0).rstrip(".,)") if m else ""
        fixes.append({"label": "Open the licence page, accept it, then download again", "targets": list(targets),
                      "open_url": url})
    elif any(w in err for w in ("key", "licen", "token", "401", "polic")):
        fixes.append({"label": "Enter / check the Copernicus key, then retry", "targets": list(targets),
                      "needs_key": True})
    elif any(w in err for w in ("403", "429", "refused", "timed out", "timeout", "temporar", "connection",
                                "reset", "503", "502", "500")):
        fixes.append({"label": "Retry now (the server refused or did not answer)", "targets": list(targets)})
    elif "no overlap" in err:
        return []
    if not fixes:
        fixes.append({"label": "Retry", "targets": list(targets)})
    elif fixes[-1]["label"] != "Retry":
        fixes.append({"label": "Retry unchanged", "targets": list(targets)})
    return fixes


def _fix_menu(parent: QWidget, job: Dict[str, Any], error: str, targets: List[DO.ExtractionTarget],
              pos, on_choose) -> None:
    from PySide6.QtWidgets import QMenu

    fixes = suggest_fixes(job, error, targets)
    if not fixes:
        return
    menu = QMenu(parent)
    for k, fx in enumerate(fixes):
        act = menu.addAction(("Apply: " if k == 0 else "") + fx["label"])
        act.triggered.connect(lambda _c=False, f=fx: on_choose(job, f))
    menu.exec(pos)


class BatchProgressDialog(QDialog):
    """One line per source with its own status while the batch runs in parallel."""

    def __init__(self, host: Any, jobs: List[Dict[str, Any]], opts: Dict[str, Any]) -> None:
        super().__init__(host)
        self.setWindowTitle("Batch download - progress")
        self.setModal(False)
        self.resize(980, 120 + 30 * len(jobs))
        lay = QVBoxLayout(self)
        self.lbl = QLabel("")
        lay.addWidget(self.lbl)
        self.tbl = QTableWidget(len(jobs), 4)
        self.tbl.setHorizontalHeaderLabels(["Source", "Server", "Progress", "Status"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        from PySide6.QtWidgets import QProgressBar

        self._rows: Dict[str, int] = {}
        self._bars: Dict[str, Any] = {}
        self._jobs = {j["key"]: j for j in jobs}
        self._errors: Dict[str, str] = {}
        self.pending_fixes: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        self.targets: List[DO.ExtractionTarget] = []
        self.pause = BatchPause()
        self._finished_keys: set = set()
        self.tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._context_menu)
        self.tbl.setToolTip("Finished sources go to Data at once - you can work with them or save the project.\n"
                            "Right-click a running source to pause it (download later), or a source with an "
                            "error to apply the suggested fix. A retry is queued and starts after this batch "
                            "finishes; it does not interrupt the current batch.")
        for r, j in enumerate(jobs):
            self._rows[j["key"]] = r
            self.tbl.setItem(r, 0, QTableWidgetItem(j["label"]))
            grp = SERVER_GROUP.get(opts.get("spei_base", "terraclimate") if j["key"] == "spei_calc" else j["key"], "")
            self.tbl.setItem(r, 1, QTableWidgetItem(grp))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            self.tbl.setCellWidget(r, 2, bar)
            self._bars[j["key"]] = bar
            self.tbl.setItem(r, 3, QTableWidgetItem("queued"))
        self.tbl.resizeColumnsToContents()
        self.tbl.setColumnWidth(2, 140)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.tbl, 1)
        btns = QHBoxLayout()
        hint = QLabel("Finished sources are already in Data. Right-click a source: pause it / apply a fix.")
        hint.setStyleSheet("color: gray;")
        btns.addWidget(hint)
        btns.addStretch(1)
        self.b_pause = QPushButton("Pause - download the rest later")
        self.b_pause.setToolTip("Stop the sources that are not finished yet and keep everything that is finished.\n"
                                "The paused sources can be resumed later with 'Resume paused downloads' (also "
                                "after restarting the program); files already loaded are taken from the cache.")
        self.b_pause.clicked.connect(self._pause_all)
        btns.addWidget(self.b_pause)
        b_cancel = QPushButton("Cancel all")
        b_cancel.setToolTip("Stop every running download. Sources that are finished stay in Data; "
                            "the rest is not remembered.")
        b_cancel.clicked.connect(host._cancel_task)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

    def _pause_all(self) -> None:
        self.pause.pause_all()
        self.b_pause.setEnabled(False)
        self.lbl.setText("Pausing: running downloads stop at the next file ...")
        for key, r in self._rows.items():
            if key not in self._finished_keys:
                self.tbl.item(r, 3).setText("pausing ...")

    def on_progress(self, stage: str, done: int, total: int, message: str) -> bool:
        """Progress hook of the host dialog; returns True when the message was handled here."""
        if stage == "batch":
            self.lbl.setText(message)
            return False  # the host updates its own progress bar / log with the overall state
        if not stage.startswith("job:"):
            return False
        key = stage[4:]
        r = self._rows.get(key)
        if r is None:
            return True
        bar = self._bars[key]
        low = message.lower()
        if message.startswith(("ERROR", "PAUSED")) or message.lower().startswith("done in"):
            self._finished_keys.add(key)
        if message.startswith("ERROR"):
            bar.setRange(0, 100)
            bar.setValue(100)
            bar.setFormat("error")
            self._errors[key] = message[6:].strip()
            fx = suggest_fixes(self._jobs.get(key, {"key": key}), self._errors[key], self.targets) \
                if key in self._jobs else []
            if fx:
                message = message + "   (right-click: " + fx[0]["label"].lower() + ")"
        elif message.startswith("PAUSED"):
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("paused")
        elif low.startswith("done in"):
            bar.setRange(0, 100)
            bar.setValue(100)
            message += "  - already in Data"
        elif total > 0:
            bar.setRange(0, 100)
            bar.setValue(int(100.0 * done / total))
        elif low not in ("queued",):
            bar.setRange(0, 0)  # busy indicator
        self.tbl.item(r, 3).setText(message[:240])
        return True

    def _context_menu(self, pos) -> None:
        r = self.tbl.rowAt(pos.y())
        key = next((k for k, row in self._rows.items() if row == r), None)
        if key is None:
            return
        if key not in self._errors:
            if key in self._finished_keys:
                return
            from PySide6.QtWidgets import QMenu

            menu = QMenu(self)
            act = menu.addAction("Pause this source - download it later")

            def do_pause(_c=False, k=key, row=r):
                self.pause.pause_key(k)
                self.tbl.item(row, 3).setText("pausing ...")

            act.triggered.connect(do_pause)
            menu.exec(self.tbl.viewport().mapToGlobal(pos))
            return

        def choose(job, fx):
            self.pending_fixes = [(j, f) for j, f in self.pending_fixes if j["key"] != job["key"]]
            self.pending_fixes.append((job, fx))
            self.tbl.item(r, 3).setText("will be downloaded again after the batch: " + fx["label"])

        _fix_menu(self, self._jobs[key], self._errors[key], self.targets, self.tbl.viewport().mapToGlobal(pos),
                  choose)


class BatchSummaryDialog(QDialog):
    def __init__(self, parent: QWidget, rows: List[Dict[str, Any]], problems: List[str], folder: str,
                 failed: Optional[List[Tuple[Dict[str, Any], str]]] = None,
                 targets: Optional[List[DO.ExtractionTarget]] = None,
                 paused: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(parent)
        self.resume = False
        self.chosen_fixes: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        self._failed = list(failed or [])
        self._targets = list(targets or [])
        self.setWindowTitle("Batch download - summary")
        self.resize(1100, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{len(rows)} dataset(s) added.</b> They are selected in the Analysis tab of RingClim: "
                             "press <i>Run analysis</i> to correlate them with the chronologies."))
        cols = ["dataset", "kind", "first_year", "last_year", "n_years", "missing_pct", "units", "warning"]
        tbl = QTableWidget(len(rows), len(cols))
        tbl.setHorizontalHeaderLabels(["Dataset", "Kind", "First", "Last", "Years", "Missing %", "Units", "Warning"])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for r, row in enumerate(rows):
            for c, k in enumerate(cols):
                v = row.get(k)
                tbl.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
        tbl.resizeColumnsToContents()
        tbl.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(tbl, 1)
        if problems:
            lay.addWidget(QLabel("Not downloaded / problems" + (" (right-click a failed source to apply the "
                                                                "suggested fix and download it again):"
                                                                if self._failed else ":")))
            box = QListWidget()
            box.addItems(problems)
            box.setMaximumHeight(160)
            box.setContextMenuPolicy(Qt.CustomContextMenu)
            box.customContextMenuRequested.connect(lambda p, b=box: self._problem_menu(b, p))
            box.itemDoubleClicked.connect(lambda it, b=box: self._problem_menu(b, b.visualItemRect(it).center()))
            self._problem_box = box
            lay.addWidget(box)
        if paused:
            lp = QLabel("Paused (not downloaded yet): " + ", ".join(j["label"] for j in paused)
                        + ".\nResume them now or later with 'Resume paused downloads' on the Sources tab.")
            lp.setWordWrap(True)
            lay.addWidget(lp)
        btns = QHBoxLayout()
        if paused:
            b_res = QPushButton("Resume paused downloads now")
            b_res.clicked.connect(lambda: (setattr(self, "resume", True), self.accept()))
            btns.addWidget(b_res)
        if self._failed:
            b_fix = QPushButton("Apply suggested fixes to all failed sources")
            b_fix.setToolTip("For every failed source: use the first suggestion (e.g. the point instead of a too large "
                             "box, or simply retry) and download again.")
            b_fix.clicked.connect(self._apply_all)
            btns.addWidget(b_fix)
        btns.addStretch(1)
        if folder:
            b_open = QPushButton("Open folder")
            b_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            btns.addWidget(b_open)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_close)
        lay.addLayout(btns)

    def _failed_for_text(self, text: str) -> Optional[Tuple[Dict[str, Any], str]]:
        for job, err in self._failed:
            if text.startswith(job["label"] + ":"):
                return job, err
        return None

    def _problem_menu(self, box, pos) -> None:
        it = box.itemAt(pos)
        if it is None:
            return
        hit = self._failed_for_text(it.text())
        if hit is None:
            return
        job, err = hit

        def choose(j, fx):
            self.chosen_fixes = [(a, b) for a, b in self.chosen_fixes if a["key"] != j["key"]] + [(j, fx)]
            self.accept()

        _fix_menu(self, job, err, self._targets, box.viewport().mapToGlobal(pos), choose)

    def _apply_all(self) -> None:
        for job, err in self._failed:
            fx = suggest_fixes(job, err, self._targets)
            if fx:
                self.chosen_fixes.append((job, fx[0]))
        self.accept()




def _job_to_json(job: Dict[str, Any]) -> Dict[str, Any]:
    out = {"key": job["key"], "label": job["label"], "vars": list(job["vars"]), "period": list(job["period"])}
    if job.get("targets"):
        out["targets"] = [dataclasses.asdict(t) for t in job["targets"]]
    return out


def _job_from_json(d: Dict[str, Any]) -> Dict[str, Any]:
    job = {"key": d["key"], "label": d["label"], "vars": list(d["vars"]), "period": tuple(d["period"])}
    if d.get("targets"):
        job["targets"] = [_target_from_json(t) for t in d["targets"]]
    return job


def _target_from_json(t: Dict[str, Any]) -> DO.ExtractionTarget:
    bbox = tuple(t["bbox"]) if t.get("bbox") else None
    return DO.ExtractionTarget(t["name"], float(t["lat"]), float(t["lon"]), t.get("mode", "bilinear"), bbox)


def _save_host_settings(h: Any) -> None:
    try:
        h.settings_changed.emit(h.get_settings())
    except Exception:
        pass


class BatchSession:
    """One batch run: finished sources go to Data at once, sources can be paused, failed ones fixed."""

    def __init__(self, tab: "SourcesTab", jobs: List[Dict[str, Any]], targets: List[DO.ExtractionTarget],
                 y0: int, y1: int, opts: Dict[str, Any], batch_id: str, batch_time: str,
                 title: str = "Batch download") -> None:
        import queue as _q

        self.tab, self.h = tab, tab.host
        self.jobs, self.targets, self.y0, self.y1, self.opts = jobs, list(targets), y0, y1, opts
        self.batch_id, self.batch_time, self.title = batch_id, batch_time, title
        self.jobs_by_key = {j["key"]: j for j in jobs}
        self.datasets: List[Dict[str, Any]] = []
        self.problems: List[str] = []
        self.failed: List[Tuple[Dict[str, Any], str]] = []
        self.paused: List[Dict[str, Any]] = []
        self.delivered: set = set()
        self.folder = ""
        self.closed = False
        self.queue: "_q.Queue" = _q.Queue()
        self.timer = QTimer(self.h)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._drain)
        self.save_timer = QTimer(self.h)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(2500)
        self.save_timer.timeout.connect(self._save)
        self.dlg: Optional[BatchProgressDialog] = None

    # ---- running -------------------------------------------------------------------------------------
    def start(self) -> bool:
        h = self.h
        self.dlg = BatchProgressDialog(h, self.jobs, self.opts)
        self.dlg.setWindowTitle(self.title + " - progress")
        self.dlg.targets = list(self.targets)
        pause = self.dlg.pause
        h._progress_hook = self.dlg.on_progress
        h._progress_hook_close = self.dlg.close
        jobs, targets, y0, y1, opts, q = self.jobs, self.targets, self.y0, self.y1, self.opts, self.queue
        ok = h._run_task(lambda p, c: run_batch_jobs(jobs, targets, y0, y1, opts, p, c,
                                                     on_result=q.put, pause=pause),
                         self._finish, f"{self.title}: {len(jobs)} source(s) ...")
        if ok:
            self.dlg.show()
            self.timer.start()
        else:
            h._progress_hook = None
        return ok

    def _drain(self) -> None:
        import queue as _q

        while True:
            try:
                o = self.queue.get_nowait()
            except _q.Empty:
                break
            self._deliver(o)
        if not self.closed and getattr(self.h, "_thread", None) is None:
            # cancelled or failed: keep what is finished
            self.closed = True
            self.timer.stop()
            self._save()
            if self.datasets:
                self.h._log(f"{self.title}: stopped - {len(self.datasets)} finished dataset(s) are kept in Data"
                            + (f" and in {self.folder}" if self.folder else ""))

    def _deliver(self, o: Dict[str, Any]) -> None:
        key = o.get("key")
        if key in self.delivered:
            return
        self.delivered.add(key)
        h, opts = self.h, self.opts
        if o.get("paused"):
            self.paused.append(self.jobs_by_key.get(key, {"key": key, "label": o.get("label", key),
                                                          "vars": o.get("vars", []), "period": (self.y0, self.y1)}))
            h._log(f"{self.title}: {o['label']} paused - download it later")
            return
        if o.get("error"):
            self.problems.append(f"{o['label']}: {o['error']}")
            if key in self.jobs_by_key:
                self.failed.append((self.jobs_by_key[key], str(o["error"])))
            h._log(f"{self.title}: {o['label']} FAILED ({o['error']})")
            return
        res = o["result"]
        try:
            if key == "itrdb":
                ds = res["datasets"]
                warns = [m for m in res["messages"] if m.startswith("FAILED")]
                if not ds:
                    warns.append((res["messages"][0] if res["messages"] else "no chronology found")
                                 + " - no standard ring-width chronology covering the period; "
                                   "increase the search radius or draw a larger box")
                for m in res["messages"]:
                    h._log(m)
            elif key == "cru":
                ds, warns = h.cru_result_to_datasets(res)
            else:
                ds, warns, cells = source_result_to_datasets(res, opts["daily_as"], h._month_names)
                self.tab._last_cells.update(cells)
        except Exception as exc:
            self.problems.append(f"{o['label']}: {exc}")
            return
        for d in ds:
            d.setdefault("meta", {})
            d["meta"]["batch_id"] = self.batch_id
            d["meta"]["select_for_analysis"] = bool(opts["select"])
        self.datasets += ds
        self.problems += [f"{o['label']}: {w}" for w in warns[:10]]
        h._log(f"{self.title}: {o['label']} -> {len(ds)} dataset(s) in {o['seconds']} s (added to Data)")
        if ds:
            h._emit_datasets(ds)  # usable at once, while the other sources are still downloading
            h._update_export_enabled()
            h._schedule_map()
            if opts["save"]:
                self.save_timer.start()

    def _save(self) -> None:
        self.save_timer.stop()
        opts = self.opts
        if not opts["save"]:
            return
        folder = os.path.join(opts["folder"] or os.path.expanduser("~"), self.batch_id)
        if self.datasets:
            try:
                files = save_batch_folder(folder, self.datasets, self.problems, opts["locations"], self.batch_time)
                self.folder = folder
                self.h._log(f"{self.title}: {len(files)} file(s) written to {folder}")
                try:
                    import dendro_ui_help as _uh

                    _uh.remember_saved(folder)
                except Exception:
                    pass
            except Exception as exc:
                self.problems.append(f"Saving the folder failed: {exc}")
        elif self.problems:  # nothing downloaded: keep at least the reasons
            try:
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, "problems.txt"), "w", encoding="utf-8") as fh:
                    fh.write(f"Batch {self.batch_time}: nothing was downloaded.\n\n" + "\n".join(self.problems) + "\n")
                self.folder = folder
            except Exception:
                pass

    def _finish(self, outcomes: List[Dict[str, Any]]) -> None:
        h = self.h
        h._progress_hook = None
        self.closed = True
        self.timer.stop()
        self._drain_queue_only()
        for o in outcomes:
            self._deliver(o)
        pending = list(self.dlg.pending_fixes) if self.dlg is not None else []
        if self.dlg is not None:
            self.dlg.close()
        self._save()
        if self.paused:
            remember_paused(self.tab, self)
        fixes = [(j, f) for j, f in pending if any(j["key"] == fj["key"] for fj, _e in self.failed)]
        if fixes:
            QTimer.singleShot(0, lambda: start_fix_session(self.tab, self, fixes))
            return
        sdlg = BatchSummaryDialog(h, [_dataset_summary(d) for d in self.datasets], self.problems, self.folder,
                                  failed=self.failed, targets=self.targets, paused=self.paused)
        sdlg.exec()
        if sdlg.chosen_fixes:
            QTimer.singleShot(0, lambda f=sdlg.chosen_fixes: start_fix_session(self.tab, self, f))
        elif sdlg.resume:
            QTimer.singleShot(0, lambda: resume_paused(self.tab))

    def _drain_queue_only(self) -> None:
        import queue as _q

        while True:
            try:
                self._deliver(self.queue.get_nowait())
            except _q.Empty:
                break


def start_fix_session(tab: "SourcesTab", prev: BatchSession,
                      fixes: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
    """Download failed sources again, each with the targets of the chosen fix."""
    if any(fx.get("needs_key") for _j, fx in fixes):
        ApiKeysDialog(tab).exec()
    urls = [fx["open_url"] for _j, fx in fixes if fx.get("open_url")]
    if urls:
        for u in dict.fromkeys(urls):
            QDesktopServices.openUrl(QUrl(u))
        if QMessageBox.question(
                tab, "Accept the licence",
                "The licence page is open in your browser:\n- " + "\n- ".join(dict.fromkeys(urls))
                + "\n\nLog in with the same account as your key, scroll to the bottom of the 'Download' tab and "
                  "click 'Accept' under 'Terms of use' (once per data set).\n\nClick Yes when done to download "
                  "again.", QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
    jobs = [dict(j, targets=list(fx["targets"])) for j, fx in fixes]
    tab.host._log("Batch: downloading again - " + "; ".join(f"{j['label']}: {f['label']}" for j, f in fixes))
    BatchSession(tab, jobs, prev.targets, prev.y0, prev.y1, prev.opts, prev.batch_id + "_retry",
                 prev.batch_time, title="Downloading again").start()


def remember_paused(tab: "SourcesTab", sess: BatchSession) -> None:
    """Keep paused sources in the settings, so they can be resumed later (also after a restart)."""
    h = tab.host
    entry = {"batch_id": sess.batch_id, "batch_time": sess.batch_time, "y0": sess.y0, "y1": sess.y1,
             "opts": sess.opts, "targets": [dataclasses.asdict(t) for t in sess.targets],
             "jobs": [_job_to_json(j) for j in sess.paused]}
    try:
        import json as _json

        _json.dumps(entry)
    except Exception:
        return
    lst = [e for e in h._settings.get("paused_batches", []) if e.get("batch_id") != sess.batch_id]
    h._settings["paused_batches"] = lst + [entry]
    _save_host_settings(h)
    tab.update_resume_button()


def resume_paused(tab: "SourcesTab") -> None:
    """Start the downloads that were paused (all paused batches, one after the other)."""
    h = tab.host
    lst = list(h._settings.get("paused_batches", []))
    if not lst:
        QMessageBox.information(tab, "Resume", "There are no paused downloads.")
        return
    if getattr(h, "_thread", None) is not None:
        QMessageBox.information(tab, "Resume", "Another download is still running; resume when it has finished.")
        return
    e = lst[0]
    h._settings["paused_batches"] = lst[1:]
    _save_host_settings(h)
    tab.update_resume_button()
    jobs = [_job_from_json(j) for j in e["jobs"]]
    targets = [_target_from_json(t) for t in e["targets"]]
    sess = BatchSession(tab, jobs, targets, int(e["y0"]), int(e["y1"]), dict(e["opts"]),
                        e["batch_id"] + "_resumed", e["batch_time"], title="Resumed download")
    if not sess.start():
        h._settings["paused_batches"] = lst
        _save_host_settings(h)
        tab.update_resume_button()


def start_batch(tab: "SourcesTab") -> None:
    """Show the batch dialog and run the selected jobs (called from the Sources tab)."""
    import datetime as _dt

    h = tab.host
    dlg = BatchClimateDialog(tab)
    if dlg.exec() != QDialog.Accepted:
        return
    jobs = dlg.jobs()
    opts = dlg.options()
    no_key = [j["label"] for j in jobs if not _key_ok(j["key"])]
    if no_key:
        if QMessageBox.question(tab, "Batch", "These sources need your Copernicus key, which is not set yet:\n- "
                                + "\n- ".join(no_key) + "\n\nEnter the key now?") == QMessageBox.Yes:
            ApiKeysDialog(tab).exec()
        jobs = [j for j in jobs if _key_ok(j["key"])]
        if not jobs:
            return
    h._settings["batch_dir"] = opts["folder"]
    targets = tab.targets()
    y0, y1 = tab.sp_start.value(), tab.sp_end.value()
    if y0 > y1:
        QMessageBox.warning(tab, "Batch", "Start year must not be after end year.")
        return
    batch_time = _dt.datetime.now().replace(microsecond=0).isoformat()
    batch_id = "batch_" + batch_time.replace(":", "").replace("-", "")
    h._log(f"Batch: {len(jobs)} source(s) for {len(targets)} location(s), {y0}-{y1}")
    BatchSession(tab, jobs, targets, y0, y1, opts, batch_id, batch_time).start()
