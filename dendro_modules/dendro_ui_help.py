#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_ui_help.py - developer signature and hover help for every window of
Detrend Advisor (Tk), RingClim (Qt) and the online-data dialogs.

* ``install_tk(root)`` - every Tk Toplevel that appears gets the developer
  signature at the bottom and hover tooltips on buttons, check/radio buttons,
  menu buttons and on input fields next to known labels.
* ``install_qt(app)`` - the same for every QDialog of a Qt application;
  ``decorate_qt(widget, footer=False)`` adds tooltips to an existing window.

Existing tooltips are never overwritten. All texts are in English.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

DEVELOPER = "Dr. Zulfiyor Bakhtiyorov"
DEVELOPER_SIGNATURE = f"Developed by {DEVELOPER}"

# ---------------------------------------------------------------------------
# Help texts: button / checkbox / radio text -> explanation
# ---------------------------------------------------------------------------
TOOLTIPS: Dict[str, str] = {
    # common
    "Help": "Open the help for this window (F1 in the main window).",
    "About": "Program version, author and licence information.",
    "Cite": "How to cite this program in a publication.",
    "Close": "Close this window.",
    "Cancel": "Close this window without saving the changes.",
    "Save": "Save the changes and close this window.",
    "OK": "Accept and close this window.",
    "Apply": "Apply the changes.",
    "Update": "Recalculate and redraw this view with the current settings.",
    "Defaults": "Restore the default values in this window.",
    "Browse...": "Choose a file or folder.",
    "Select all": "Select every item in this list.",
    "Select none": "Clear the selection in this list.",
    "Clear": "Clear the current selection / list.",
    "Clear selection": "Clear the current selection.",
    "Copy all": "Copy the whole text to the clipboard.",
    "Find next": "Find the next occurrence of the search text.",
    "Export...": "Choose what to export from this view.",
    "Remove selected": "Remove the selected rows from the list.",
    "Preview...": "Load the selected file into memory and show its plot and summary without saving anything.",
    # Detrend Advisor - main window
    "RUN": "Run the detrending advisor: detrend every dataset with all selected methods, score them and recommend the best one.",
    "Run": "Run the detrending advisor for the current project and settings.",
    "Project": "Project menu: new/open/save project, import data, ITRDB, pith information, climate, export folder.",
    "Add data": "Add ring-width files (Tucson .rwl or CSV) to the current project without replacing the loaded datasets.",
    "Actions": "Export results (RWI, chronologies, reports) or open the dataset in RingClim.",
    "Score settings...": "Set the frequency cut-offs and weights used to score the detrending methods.",
    "Tree groups": "Average radii of the same tree before detrending (tree-level series instead of radius-level).",
    "Tree groups...": "Review how radii are grouped into trees and set the pith (centre) of each series.",
    "...": "Open the tree-group and pith editor for the active dataset.",
    "POWT": "Power transformation (dplR powt) before detrending, to stabilise the variance of ring widths.",
    "Export POWT+NoPOWT": "Export the results both with and without the power transformation.",
    "SSF": "Also run simple signal-free versions of the spline methods.",
    "SF-RCS": "Also run signal-free regional curve standardisation (needs enough series and age spread).",
    "Auto score preset": "Keep score weights and cut-offs tied to the selected goal.",
    "Apply score preset": "Reset the score weights and cut-offs to the preset of the current goal.",
    "Excel only": "Export one Excel workbook plus the Markdown report.",
    "CSV + Excel": "Export a CSV file per method, one Excel workbook and the Markdown report.",
    # Detrend Advisor - tree groups / pith
    "Apply to selected": "Give the selected series the typed tree label and core/radius label.",
    "Reset selected to auto": "Return the selected series to the automatically detected tree/radius labels.",
    "Rebuild all auto": "Detect tree and radius labels again for all series (name rules, then ring-width check).",
    "Rebuild auto from variants": "Rebuild the automatic grouping with the radius suffix variants typed above.",
    "Apply prefixes": "Use these site/locality prefixes when detecting tree numbers.",
    "Apply pith to selected": "Set the pith status and the number of rings to the pith for the selected series.",
    "Import .pos / pith CSV...": "Read pith information from .pos files (PyTreeRingRecorder, CooRecorder) or pith CSV files.",
    "Import from folder...": "Read pith information from every .pos / .pith.csv file in a folder and its subfolders.",
    "Check": "Correlate the radii (year-to-year ring-width signal) to check the tree grouping and suggest a better ID scheme.",
    "Apply suggested scheme": "Group the radii with the ID scheme that the ring-width correlation suggests.",
    # Detrend Advisor - ITRDB window (Tk)
    "Search ITRDB": "Search the International Tree-Ring Data Bank (NOAA) with the area and filters above.",
    "Around point": "Search within a radius (km) around the given latitude / longitude.",
    "Bounding box": "Search inside a latitude / longitude rectangle.",
    "Download selected": "Download the selected ring-width files (and add them to the project if ticked).",
    "Batch download...": "Download files from many sites at once, chosen by criteria (type, parameter, length, years, distance).",
    "Open study page": "Open the NOAA study page of the selected site in the web browser.",
    "Download & add this file": "Download the previewed file and add it to the project.",
    "Download this file": "Download the previewed file to the chosen folder.",
    "Download matching files": "Download every file that matches the criteria above.",
    "Add downloaded .rwl files to the project": "After downloading, load the .rwl files as datasets (ring widths in mm).",
    # RingClim - main window
    "Add / Import File": "Import one data file (CSV, TXT, PRN, XLS, XLSX); GRDC and station tables are detected automatically.",
    "Add multiple files": "Import many files; files with the same layout are imported automatically.",
    "Large gridded data...": "Extract a point or area time series from a NetCDF grid on your disk (e.g. CRU TS).",
    "Online data...": "Download chronologies from ITRDB and climate from CRU TS, with a location map.",
    "Remove Selected": "Remove the selected datasets from the project.",
    "Edit Selected": "Reopen the import settings of the selected dataset (layout, columns, names, climate type).",
    "Save project": "Save all datasets and settings to a project file.",
    "Load project": "Open a project (replace the session or merge into it).",
    "Add project": "Merge another project into the current session.",
    "Run analysis": "Correlate the selected chronologies with all climate windows of the selected climate datasets.",
    "Preview selected dataset": "Show how the selected dataset was imported.",
    "Preview selected daily/hourly → monthly": "Show the monthly table built from the selected daily/hourly series.",
    "Use common period for selected datasets": "Limit the analysis to the years shared by all selected datasets.",
    "Exclude single-month windows": "Leave out windows of one month; only multi-month seasons are tested.",
    "Enable multi-year period analysis": "Also compute correlations in sub-periods (stability of the climate signal over time).",
    "Sliding fixed length": "Sub-periods of fixed length moved by the step (e.g. 1901-1950, 1911-1960 ...).",
    "Anchored": "Sub-periods that all start (or end) at the same year and grow in length.",
    "Exhaustive grid (experimental)": "Every start/end combination allowed by the span and step settings (slow).",
    "Export report": "Save the report table (top rows per pair) to a file.",
    "Ignore missing (use available months)": "Climate windows use the months that are available; missing months are skipped.",
    "Strict: drop windows with any missing value": "A climate window is empty if any of its months is missing.",
    "Fill missing with monthly mean (per station)": "Replace missing months with the long-term mean of that month.",
    "Fill missing with local window mean (±K years, per month)": "Replace a missing month with the mean of the same month in the K years around it.",
    "Linear interpolation (only if gap ≤ N years, per month)": "Interpolate missing months linearly, only for short gaps.",
    "Use only years:": "Restrict the analysis to the given year range.",
    # RingClim - import dialogs
    "Preview / auto-detect": "Read the file again with the current settings and propose a layout.",
    "Auto-detect layout & suggestions": "Detect the table layout, year column and month columns automatically.",
    "Auto-detect and apply column names": "Detect month / year columns and rename them automatically.",
    "Edit / Filter Columns": "Choose which columns to keep and rename them.",
    "File has header row (first row contains column names)": "The first row holds column names, not data.",
    "Treat lines starting with '#' as comments": "Ignore lines that begin with '#'.",
    "Year + 12 month columns (wide)": "One row per year with twelve month columns.",
    "Year + Month + Value (stacked monthly)": "One row per month: year, month and value columns.",
    "Date (YYYY-MM or YYYY-MM-DD) + Value (monthly)": "One row per date with a value column.",
    "Year + multiple series columns (chronologies or climate series)": "One row per year, one column per series.",
    "Import only a single selected series as chronology": "Import just one column of the table as a chronology.",
    "Apply rename to selected": "Rename the selected column.",
    "Apply quick suggestions": "Apply the suggested column names (Year, Jan ... Dec).",
    "Add to project": "Add the extracted series to the project.",
    "Run extraction": "Extract the time series from the gridded file.",
    "Specify bounding box manually": "Average all grid cells inside the box instead of interpolating to the point.",
    "STD only": "Select only standard (STD) chronologies.",
    "RES only": "Select only residual (RES) chronologies.",
    "POWT only": "Select only power-transformed outputs.",
    "Exclude POWT": "Leave out power-transformed outputs.",
    "Chronologies only": "Select chronologies only (no RWI series).",
    "Top 3 recommended (per dataset)": "Select the three best-scored methods of every dataset.",
    # Online data dialog (Qt)
    "Use CRU point": "Copy the latitude / longitude from the CRU TS tab.",
    "Std RW chronology": "Tick the standard total-ring-width chronology of each selected site.",
    "All chronologies": "Tick every chronology (.crn) of the selected sites.",
    "Raw .rwl": "Tick the raw ring-width measurements (.rwl).",
    "None": "Untick all files.",
    "Download checked files": "Download the ticked files; chronologies are imported as datasets if that option is ticked.",
    "Send selected sites to CRU": "Add the selected sites as CRU TS extraction targets.",
    "Import checked chronologies (.crn) as datasets": "After downloading, import ticked .crn chronologies into RingClim.",
    "Check versions online": "Read the list of CRU TS releases from the CRU website.",
    "Box +-0.5 deg around point": "Set the bounding box to ±0.5° around the point.",
    "Add current location": "Add the location above to the list of extraction targets.",
    "Estimate download": "Show how many files and megabytes the download needs.",
    "Download & extract": "Download the CRU TS files (one decade at a time), extract the targets and add climate datasets.",
    "Delete downloaded files after extraction (temporary download)": "Remove every downloaded CRU file as soon as its values have been extracted.",
    "Save map as PNG...": "Save the location map as an image.",
    "Download / import this file": "Download the previewed file (chronologies are imported).",
    "Download & import this chronology": "Download the previewed chronology and import it as a dataset.",
    "Chronologies (.crn) - imported as datasets": "Include chronology files; they are imported into RingClim.",
    "Raw measurements (.rwl) - downloaded only": "Include raw measurement files; they are saved to the folder only.",
    # Location export / cache
    "Export locations...": "Export coordinates and attributes of everything downloaded (ITRDB sites, climate points, "
                           "grid cells, stations) as CSV, GeoJSON, KMZ (Google Earth) or Shapefile.",
    "Export": "Write the selected layers in the selected formats.",
    "All formats": "Tick every export format (CSV, GeoJSON, KMZ, Shapefile).",
    "Remove from list": "Remove the selected items from the download list (the downloaded files are not deleted).",
    "This session only": "Show only what was downloaded since the program was started.",
    "Open folder": "Open the folder with the exported files.",
    "ITRDB sites and files": "Points of the downloaded ITRDB files (site, species, period, parameter, links).",
    "Climate extraction points": "The points / sites for which climate series were extracted.",
    "Grid cells / pixels used": "Polygons of the grid cells or pixels used for interpolation or averaging, with weights.",
    "Area-mean boxes": "Bounding boxes used for area means.",
    "Weather and gauge stations": "Weather stations whose data were downloaded.",
    "CSV tables (ArcGIS XY Table To Point, QGIS, Excel)": "One CSV table per layer with decimal-degree coordinates (WGS84).",
    "GeoJSON (ArcGIS JSON To Features, QGIS)": "One GeoJSON file per layer.",
    "KMZ (Google Earth, opens with a double-click)": "One KMZ file with folders per layer, popups and time spans.",
    "Shapefile (ArcGIS, QGIS)": "One Shapefile (.shp/.shx/.dbf/.prj/.cpg) per layer.",
    # More climate data
    "Start batch download": "Download everything ticked, one source after the other, in the background.",
    "Add to the project and select everything in the Analysis tab": "New datasets are added and selected for the "
                                                                    "analysis, so you only need to press Run analysis.",
    "Also export the locations (CSV, KMZ, GeoJSON)": "Write the points, grid cells and stations of this batch for GIS / "
                                                     "Google Earth into the same folder.",
        "Get data": "Download / read the selected variables for the location and period and add them as datasets.",
    "Find stations": "Search weather stations near the point.",
    "Data page": "Open the web page of the selected data set.",
    "GRDC portal...": "Open the GRDC data portal (river discharge needs a request there; import the files afterwards).",
    "Copy from CRU TS tab": "Use the name and coordinates of the CRU TS tab.",
    "Use all CRU TS targets": "Extract every point in the CRU TS target list in one run.",
    "Redraw map": "Draw the map again around the current location (resets zoom / pan).",
    "World": "Show the whole world to choose a place anywhere (then zoom with the mouse wheel).",
    "Snap box to grid cells": "A box drawn on the map is expanded to whole grid cells of the selected data set.",
    "standard": "Standard chronology version.",
    "residual": "Residual (prewhitened) chronology version.",
    "ARSTAN": "ARSTAN chronology version.",
}

# Patterns for texts that contain numbers or names
TOOLTIP_PATTERNS: List[Tuple[str, str]] = [
    (r"^All sites in the results", "Choose files from every site found by the search."),
    (r"^Selected sites", "Choose files only from the sites selected in the table."),
    (r"^(tmp|tmn|tmx|dtr|pre|wet|frs|vap|cld|pet) - ", "Download and extract this CRU TS variable."),
    (r"^Total ring width$|^Earlywood width$|^Latewood width$|^Maximum density$|^Minimum density$|^Parameter",
     "Include files with this measured parameter."),
]

# Field labels -> explanation (also shown on the input field next to the label)
LABEL_TOOLTIPS: Dict[str, str] = {
    "Dataset:": "Dataset shown in the result tabs.",
    "Goal:": "What the chronology is for; it sets how the detrending methods are scored.",
    "Type:": "Kind of measurement (e.g. TRW = total ring width).",
    "R workers:": "Number of parallel R processes for the dplR backend.",
    "Min count:": "Minimum number of series (or trees) per year; earlier years are not detrended.",
    "Count by:": "Count series or trees for the minimum sample depth.",
    "Mode:": "What to compare: one method across datasets, or all methods.",
    "Method:": "Detrending method shown in this view ('best' = recommended method).",
    "Min overlap (years):": "Minimum number of common years needed to compute a correlation.",
    "Window:": "Moving window length in years for EPS / Rbar.",
    "View:": "What to show in this plot.",
    "Chronology:": "Standard (STD) or residual (RES) chronology.",
    "Running window (years):": "Window length of the running correlation.",
    "Running window:": "Window length of the running correlation.",
    "Plot:": "Plot type for the selected result row.",
    "Status:": "Pith status: present (pith in the sample), estimated (rings to pith known) or unknown.",
    "Rings to pith:": "Rings missing between the innermost measured ring and the pith (0 = pith present).",
    "Unknown pith in RCS:": "How series without pith information are handled in RCS.",
    "Tree label:": "Tree identifier given to the selected series.",
    "Core/radius:": "Radius / core identifier given to the selected series.",
    "User prefixes:": "Site or locality prefixes that are not part of the tree number.",
    "Pattern groups:": "ID patterns: A = letters, B = tree number, C = radius (e.g. AAAABBC).",
    "Backend:": "dplR (R) for results identical to dplR, or the internal Python implementation.",
    "Find:": "Text to search for.",
    "Save files to:": "Folder for the downloaded files.",
    "Species:": "Four-letter ITRDB species code, e.g. JUTU, PCSH, PISY.",
    "Text:": "Free text: site name, investigator ...",
    "Years from:": "Data must cover years from ... to ... (empty = any).",
    "Correlation method:": "Pearson (linear) or Spearman (rank) correlation.",
    "Aggregation:": "How months are combined in a climate window: sum (e.g. precipitation) or mean (e.g. temperature).",
    "Min window length (months):": "Shortest climate window tested.",
    "Max window length (months):": "Longest climate window tested.",
    "Show only top N correlations:": "Number of strongest correlations kept in the result table (0 = all).",
    "Show:": "Main correlations or sub-period correlations.",
    "Export:": "Export the results, climate windows, merged data or the plot.",
    "CRU TS version:": "CRU TS release to download.",
    "Variables:": "CRU TS variables to extract.",
    "Name:": "Name of the extraction target (used in the dataset name).",
    "Lat (N+):": "Latitude in decimal degrees, north positive.",
    "Lon (E+):": "Longitude in decimal degrees, east positive.",
    "Lat:": "Latitude in decimal degrees, north positive.",
    "Lon:": "Longitude in decimal degrees, east positive.",
    "Radius:": "Search radius around the point.",
    "Years:": "First and last year to extract.",
    "Download folder:": "Folder for (temporary) CRU downloads.",
    "Minimum sample depth:": "Years with fewer series are removed when a chronology is imported.",
    "Minimum record length:": "Sites with shorter records are skipped.",
    "Last year at least:": "Records must reach at least this year.",
    "First year at most:": "Records must start no later than this year.",
    "Max distance from point:": "Only sites within this distance.",
    "Chronology version:": "Which chronology versions to download.",
    "Parameter:": "Measured parameter (ring width, earlywood, latewood, density ...).",
    "Climate parameter type:": "Kind of climate variable; it sets whether months are summed or averaged.",
    "Climate variable type (for climate datasets):": "Kind of climate variable; it sets whether months are summed or averaged.",
    "Start month:": "First month of the season.",
    "End month:": "Last month of the season.",
    "Target lat:": "Latitude of the point to extract.",
    "Target lon:": "Longitude of the point to extract.",
    "Source frequency:": "Time step of the input data.",
    "Target frequency:": "Time step of the output series.",
    "Resolution:": "Analysis resolution: monthly windows or daily windows.",
    "Correlation target:": "Correlate chronologies with climate, or chronologies with each other.",
    "to:": "End of the range.",
    "to": "End of the range.",
    "Lat from:": "Southern edge of the search box (decimal degrees).",
    "Lon from:": "Western edge of the search box (decimal degrees, east positive).",
    "Min lat:": "Southern edge of the box (decimal degrees).",
    "Max lat:": "Northern edge of the box (decimal degrees).",
    "Min lon:": "Western edge of the box (decimal degrees, east positive).",
    "Max lon:": "Eastern edge of the box (decimal degrees, east positive).",
    "Box lat:": "South and north edges of the averaging box (decimal degrees).",
    "Box lon:": "West and east edges of the averaging box (decimal degrees).",
    "File:": "Input file.",
    "K:": "Number of years on each side used for the local mean.",
    "N:": "Longest gap (years) that may be interpolated.",
    "New name:": "New name for the selected column.",
    "Selected column:": "Column to rename.",
    "Loaded datasets:": "Datasets in the project; select the ones to analyse.",
    "Minimal data points inside period:": "A sub-period needs at least this many years with data.",
    "Minimal period span (years):": "Shortest sub-period length.",
    "Step between periods (years):": "How far each next sub-period is moved.",
    "Year start / Year end:": "Overall range of years used for the sub-periods.",
    "Daily/hourly → monthly:": "How daily or hourly values are combined into monthly values.",
    "Report:": "Number of rows per chronology-climate pair in the report.",
    "Report: top K rows per pair (0 = default 10):": "Number of rows per chronology-climate pair in the report.",
    "Season filter (only windows fully inside season):": "Only climate windows that lie fully inside this season are tested.",
    "Search mode:": "Search around a point or inside a latitude / longitude box.",
    "Radius (km):": "Search radius around the point.",
    "Radius km:": "Search radius around the point (km).",
    "Source:": "Data source (no registration or API key needed).",
    "GPCC grid:": "Grid spacing of the GPCC product.",
    "ERA5 model:": "ERA5 (0.25 deg, from 1940) or ERA5-Land (0.1 deg, from 1950).",
    "P and PET from:": "Data set that provides monthly precipitation and potential evapotranspiration for SPEI / SPI.",
    "Region / country:": "Berkeley Earth region name; empty = country of the point.",
    "Import daily data as:": "Daily series (for daily analysis) and / or monthly tables.",
    "Min years:": "Minimum number of years with data inside the period.",
    "Preset:": "Tick a typical set of sources and variables (single ticks can be changed afterwards).",
    "Search radius:": "ITRDB sites and weather stations are searched within this distance of each point.",
    "Stations per point:": "Number of nearest weather stations used for each point.",
    "Parallel sources:": "How many servers are used at the same time (1 = one source after the other).",
    "Folder:": "Folder for the exported files.",
    "File name:": "Base name of the exported files.",
    "Variable:": "Climate variable.",
}


def _norm(text: str) -> str:
    t = str(text or "").replace("&&", "\x00")
    t = re.sub(r"&(?=\w)", "", t).replace("\x00", "&")  # remove Qt mnemonics only
    return re.sub(r"\s+", " ", t).strip()


def help_for(text: str) -> str:
    t = _norm(text)
    if not t:
        return ""
    if t in TOOLTIPS:
        return TOOLTIPS[t]
    for pat, tip in TOOLTIP_PATTERNS:
        if re.search(pat, t):
            return tip
    return ""


def help_for_label(text: str) -> str:
    return LABEL_TOOLTIPS.get(_norm(text), "")


# ---------------------------------------------------------------------------
# Tk
# ---------------------------------------------------------------------------

class TkTooltip:
    """Hover tooltip for Tk/ttk widgets (one per widget)."""

    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        try:
            self._job = self.widget.after(450, self._show)
        except Exception:
            pass

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self) -> None:
        import tkinter as tk

        self._job = None
        if self.tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw._dendro_no_decorate = True
            try:
                tw.wm_attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(tw, text=self.text, justify="left", background="#fffdd0", relief="solid", borderwidth=1,
                     padx=6, pady=3, wraplength=340).pack()
            tw.wm_geometry(f"+{x}+{y}")
            self.tip = tw
        except Exception:
            self.tip = None

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def tk_set_tooltip(widget, text: str, overwrite: bool = False) -> None:
    """Attach (or update) a tooltip; existing tooltips are kept unless overwrite=True."""
    if not text:
        return
    if getattr(widget, "_hover_tooltip_text", None) and not overwrite:
        return  # the host application already gave this widget a tooltip
    existing = getattr(widget, "_dendro_tip", None)
    if existing is not None:
        if overwrite:
            existing.text = text
        return
    try:
        widget._dendro_tip = TkTooltip(widget, text)
    except Exception:
        pass


def _tk_iter(root):
    yield root
    try:
        children = root.winfo_children()
    except Exception:
        children = []
    for ch in children:
        yield from _tk_iter(ch)


def tk_apply_tooltips(root, fallback=None) -> None:
    """Tooltips for buttons / check / radio / menu buttons and for fields next to known labels."""
    import tkinter as tk
    from tkinter import ttk

    btn_types = (tk.Button, ttk.Button, tk.Checkbutton, ttk.Checkbutton, tk.Radiobutton, ttk.Radiobutton,
                 ttk.Menubutton, tk.Menubutton)
    field_types = (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox, ttk.Spinbox, tk.Text, tk.Listbox)
    for w in _tk_iter(root):
        try:
            text = str(w.cget("text")) if isinstance(w, btn_types + (tk.Label, ttk.Label)) else ""
        except Exception:
            text = ""
        if isinstance(w, btn_types):
            tip = help_for(text) or (fallback(w) if fallback else "")
            tk_set_tooltip(w, tip)
        elif isinstance(w, (tk.Label, ttk.Label)):
            tip = help_for_label(text)
            if not tip:
                continue
            tk_set_tooltip(w, tip)
            try:
                sibs = w.master.winfo_children()
                i = sibs.index(w)
                for nxt in sibs[i + 1:i + 3]:
                    if isinstance(nxt, field_types):
                        tk_set_tooltip(nxt, tip)
                        break
            except Exception:
                pass


def tk_add_footer(win, text: Optional[str] = None) -> None:
    """Developer signature at the bottom of a Tk window (works with pack and grid layouts)."""
    import tkinter as tk

    text = text or DEVELOPER_SIGNATURE
    if getattr(win, "_dendro_footer", False):
        return
    win._dendro_footer = True
    try:
        from tkinter import ttk

        lbl = ttk.Label(win, text=text, font=("Arial", 8), foreground="#52514e", anchor="w")
        lbl._dendro_footer_label = True
        grid_slaves = win.grid_slaves()
        pack_slaves = win.pack_slaves()
        if grid_slaves:
            rows = [int(s.grid_info().get("row", 0)) + int(s.grid_info().get("rowspan", 1)) for s in grid_slaves]
            cols = [int(s.grid_info().get("column", 0)) + int(s.grid_info().get("columnspan", 1)) for s in grid_slaves]
            lbl.grid(row=max(rows), column=0, columnspan=max(cols), sticky="w", padx=8, pady=(0, 3))
        elif pack_slaves:
            lbl.pack(side=tk.BOTTOM, anchor="w", padx=8, pady=(0, 3), before=pack_slaves[0])
        else:
            lbl.place(relx=0.0, rely=1.0, anchor="sw", x=6, y=-2)
            return
        # Windows with a fixed size: grow so that the signature does not hide other widgets
        win.update_idletasks()
        h, req = win.winfo_height(), win.winfo_reqheight()
        if h > 1 and req > h and win.wm_state() == "normal":
            new_h = min(req, win.winfo_screenheight() - 80)
            win.geometry(f"{win.winfo_width()}x{new_h}")
    except Exception:
        pass


def tk_decorate(win, fallback=None, footer: bool = True) -> None:
    if getattr(win, "_dendro_no_decorate", False):
        return
    try:
        if win.wm_overrideredirect():
            return
    except Exception:
        pass
    if footer:
        tk_add_footer(win)
    tk_apply_tooltips(win, fallback=fallback)



# ---------------------------------------------------------------------------
# F12 = save a screenshot of the current window (for documentation / reports)
# ---------------------------------------------------------------------------

def screenshot_dir() -> str:
    """Folder for window screenshots (environment variable DECLIM_SCREENSHOT_DIR or ~/DeClim_screenshots)."""
    import os

    d = os.environ.get("DECLIM_SCREENSHOT_DIR") or os.path.join(os.path.expanduser("~"), "DeClim_screenshots")
    os.makedirs(d, exist_ok=True)
    return d


def _screenshot_path(title: str) -> str:
    import datetime as _dt
    import os
    import re as _re

    slug = _re.sub(r"[^A-Za-z0-9]+", "_", title or "window").strip("_")[:60] or "window"
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return os.path.join(screenshot_dir(), f"{stamp}_{slug}.png")


def _win_capture_hwnd(hwnd: int):
    """PIL image of a Windows window (works when other windows cover it)."""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("empty window")
    hdc_win = user32.GetWindowDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, hdc, 2):  # PW_RENDERFULLCONTENT
            user32.PrintWindow(hwnd, hdc, 0)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight, bih.biPlanes, bih.biBitCount = w, -h, 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(hwnd, hdc_win)


def tk_save_screenshot(win) -> str:
    """Save the Tk toplevel that contains 'win' as PNG; returns the file path."""
    import sys

    top = win.winfo_toplevel()
    top.update_idletasks()
    path = _screenshot_path(top.title())
    img = None
    if sys.platform.startswith("win"):
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
            img = _win_capture_hwnd(hwnd)
        except Exception:
            img = None
    if img is None:
        from PIL import ImageGrab

        x, y = top.winfo_rootx(), top.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + top.winfo_width(), y + top.winfo_height()))
    img.save(path)
    return path


def install_tk(root, fallback=None) -> None:
    """Decorate every Tk Toplevel when it is first shown (and once more after it is filled)."""

    def on_map(event) -> None:
        w = event.widget
        try:
            if w.winfo_toplevel() is not w or w is root:
                return
        except Exception:
            return
        if getattr(w, "_dendro_decorated", False):
            tk_apply_tooltips(w, fallback=fallback)   # widgets added later
            return
        w._dendro_decorated = True
        tk_decorate(w, fallback=fallback)
        try:  # once more for widgets created right after the window was shown
            w.after(400, lambda: tk_apply_tooltips(w, fallback=fallback) if w.winfo_exists() else None)
        except Exception:
            pass

    try:
        root.bind_class("Toplevel", "<Map>", on_map, add="+")
    except Exception:
        pass

    def on_f12(event) -> str:
        try:
            path = tk_save_screenshot(event.widget)
            print(f"Screenshot saved: {path}", flush=True)
            try:
                event.widget.winfo_toplevel().bell()
            except Exception:
                pass
        except Exception as exc:
            print(f"Screenshot failed: {exc}", flush=True)
        return "break"

    try:
        root.bind_all("<F12>", on_f12, add="+")
    except Exception:
        pass
    tk_apply_tooltips(root, fallback=fallback)


# ---------------------------------------------------------------------------
# Qt (PySide6)
# ---------------------------------------------------------------------------

def qt_apply_tooltips(win) -> None:
    from PySide6.QtWidgets import QAbstractButton, QLabel, QWidget

    for b in win.findChildren(QAbstractButton):
        if not b.toolTip():
            tip = help_for(b.text())
            if tip:
                b.setToolTip(tip)
        if b.toolTip() and b.text().strip().endswith(":"):   # e.g. "Use only years:" [spin] to [spin]
            nxt = _qt_next_field(b)
            if nxt is not None and not nxt.toolTip():
                nxt.setToolTip(b.toolTip())
    for lbl in win.findChildren(QLabel):
        tip = help_for_label(lbl.text())
        if not tip:
            continue
        if not lbl.toolTip():
            lbl.setToolTip(tip)
        buddy = lbl.buddy() or _qt_next_field(lbl)
        if isinstance(buddy, QWidget) and not buddy.toolTip():
            buddy.setToolTip(tip)
        second = _qt_next_field(buddy) if isinstance(buddy, QWidget) else None
        if second is not None and not second.toolTip():  # pairs like "Box lat: [min] [max]"
            second.setToolTip(tip)


def _qt_next_field(widget):
    """Input field placed right after widget in its layout (or None)."""
    from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QLineEdit

    parent = widget.parentWidget()
    lay = parent.layout() if parent is not None else None
    found = _qt_find_layout_with(lay, widget) if lay is not None else None
    if found is None:
        return None
    layout, idx = found
    from PySide6.QtWidgets import QGridLayout

    if isinstance(layout, QGridLayout):  # the next column in the same row
        r, c, _rs, _cs = layout.getItemPosition(idx)
        for dc in (1, 2):
            item = layout.itemAtPosition(r, c + dc)
            w = item.widget() if item is not None else None
            if isinstance(w, (QComboBox, QAbstractSpinBox, QLineEdit)):
                return w
            if w is not None and not isinstance(w, (QComboBox, QAbstractSpinBox, QLineEdit)):
                return None
        return None
    for k in (idx + 1, idx + 2):
        item = layout.itemAt(k)
        w = item.widget() if item is not None else None
        if isinstance(w, (QComboBox, QAbstractSpinBox, QLineEdit)):
            return w
    return None


def _qt_find_layout_with(layout, widget):
    """(layout, index) of the layout that directly contains widget."""
    if layout is None:
        return None
    idx = layout.indexOf(widget)
    if idx >= 0:
        return layout, idx
    for i in range(layout.count()):
        sub = layout.itemAt(i).layout() if layout.itemAt(i) is not None else None
        if sub is not None:
            res = _qt_find_layout_with(sub, widget)
            if res is not None:
                return res
    return None


def qt_add_footer(win, text: Optional[str] = None) -> None:
    from PySide6.QtWidgets import QBoxLayout, QGridLayout, QLabel

    text = text or DEVELOPER_SIGNATURE
    if win.property("dendro_footer"):
        return
    win.setProperty("dendro_footer", True)
    lay = win.layout()
    lbl = QLabel(text)
    lbl.setObjectName("dendroDeveloperFooter")
    lbl.setStyleSheet("color: #52514e; font-size: 8pt;")
    if isinstance(lay, QBoxLayout):
        lay.addWidget(lbl)
    elif isinstance(lay, QGridLayout):
        lay.addWidget(lbl, lay.rowCount(), 0, 1, max(1, lay.columnCount()))
    else:
        lbl.deleteLater()
        title = win.windowTitle()
        if DEVELOPER not in title:
            win.setWindowTitle(f"{title} - {DEVELOPER_SIGNATURE}" if title else DEVELOPER_SIGNATURE)


def decorate_qt(win, footer: bool = True) -> None:
    try:
        if footer:
            qt_add_footer(win)
        qt_apply_tooltips(win)
    except Exception:
        pass


def install_qt(app) -> None:
    """Decorate every QDialog when it is shown (system dialogs and message boxes are skipped)."""
    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtWidgets import (QColorDialog, QDialog, QFileDialog, QFontDialog, QInputDialog, QMessageBox,
                                   QProgressDialog)

    skip = (QMessageBox, QFileDialog, QProgressDialog, QInputDialog, QColorDialog, QFontDialog)

    class _Decorator(QObject):
        def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
            try:
                if event.type() == QEvent.Show and isinstance(obj, QDialog) and not isinstance(obj, skip):
                    decorate_qt(obj, footer=not bool(obj.property("dendro_footer")))
                elif event.type() == QEvent.KeyPress and event.key() == Qt.Key_F12:
                    win = app.activeWindow() or (obj.window() if hasattr(obj, "window") else None)
                    if win is not None:
                        path = _screenshot_path(win.windowTitle())
                        win.grab().save(path, "PNG")
                        print(f"Screenshot saved: {path}", flush=True)
                        app.beep()
                    return True
            except Exception:
                pass
            return False

    dec = _Decorator(app)
    app.installEventFilter(dec)
    app._dendro_decorator = dec   # keep a reference


# ---------------------------------------------------------------------------
# Dockable tabs: a window that can live as a tab of the main window or float
# ---------------------------------------------------------------------------

class QtTabDock:
    """Keep ``widget`` either as a tab of ``tabs`` (docked) or as a separate window.

    * ``attach()`` puts the widget into the tab widget, ``detach()`` makes it a window.
    * Closing the floating window docks it again; ``close()`` removes it completely and
      calls ``on_closed``.
    * ``make_button()`` returns a push button that toggles the state.
    """

    def __init__(self, tabs, widget, title: str, on_closed=None, icon=None) -> None:
        from PySide6.QtCore import QEvent, QObject

        self.tabs = tabs
        self.widget = widget
        self.title = title
        self.on_closed = on_closed
        self.icon = icon
        self.docked = False
        self._buttons: List[Any] = []
        self._closing = False
        self._float_geometry = None
        dock = self

        class _Filter(QObject):
            def eventFilter(self, obj, event):  # noqa: N802
                if obj is dock.widget and event.type() == QEvent.Close and not dock.docked and not dock._closing:
                    event.ignore()
                    dock.attach()
                    return True
                return False

        self._filter = _Filter(widget)
        widget.installEventFilter(self._filter)
        try:
            tabs.tabCloseRequested.connect(self._on_tab_close)
        except Exception:
            pass

    # -------------------------------------------------------------- state
    def _update_buttons(self) -> None:
        for b in list(self._buttons):
            try:
                b.setText("Detach to window" if self.docked else "Attach as tab")
                b.setToolTip("Show this page in its own window (it can be moved to another monitor)."
                             if self.docked else "Put this window back as a tab of the main window.")
            except RuntimeError:
                self._buttons.remove(b)

    def _hide_fixed_close_buttons(self) -> None:
        from PySide6.QtWidgets import QTabBar

        bar = self.tabs.tabBar()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not getattr(w, "_dock_closable", False):
                for side in (QTabBar.RightSide, QTabBar.LeftSide):
                    btn = bar.tabButton(i, side)
                    if btn is not None:
                        btn.resize(0, 0)
                        btn.hide()

    def attach(self, select: bool = True) -> None:
        from PySide6.QtCore import Qt

        w = self.widget
        if not self.docked:
            try:
                self._float_geometry = w.saveGeometry()
            except Exception:
                pass
        w.hide()
        w.setParent(None)
        w.setWindowFlags(Qt.Widget)
        w._dock_closable = True
        self.tabs.setTabsClosable(True)
        idx = self.tabs.indexOf(w)
        if idx < 0:
            idx = self.tabs.addTab(w, self.title) if self.icon is None else self.tabs.addTab(w, self.icon, self.title)
        w.show()
        self.docked = True
        self._hide_fixed_close_buttons()
        if select:
            self.tabs.setCurrentIndex(idx)
        self._update_buttons()

    def detach(self) -> None:
        from PySide6.QtCore import Qt

        w = self.widget
        idx = self.tabs.indexOf(w)
        if idx >= 0:
            self.tabs.removeTab(idx)
        parent = self.tabs.window()
        w.setParent(parent, Qt.Window)
        w.setWindowTitle(self.title)
        self.docked = False
        if self._float_geometry is not None:
            try:
                w.restoreGeometry(self._float_geometry)
            except Exception:
                pass
        else:
            try:
                w.resize(max(900, int(parent.width() * 0.85)), max(600, int(parent.height() * 0.85)))
            except Exception:
                pass
        w.show()
        w.raise_()
        w.activateWindow()
        self._update_buttons()

    def toggle(self) -> None:
        if self.docked:
            self.detach()
        else:
            self.attach()

    def show(self) -> None:
        if self.docked:
            self.tabs.setCurrentWidget(self.widget)
        else:
            self.widget.show()
            self.widget.raise_()
            self.widget.activateWindow()

    def close(self) -> None:
        self._closing = True
        idx = self.tabs.indexOf(self.widget)
        if idx >= 0:
            self.tabs.removeTab(idx)
        try:
            self.widget.removeEventFilter(self._filter)
        except Exception:
            pass
        cb = self.on_closed
        self.on_closed = None
        if cb is not None:
            cb()

    def _on_tab_close(self, index: int) -> None:
        if self.tabs.widget(index) is self.widget:
            try:
                self.widget.close_requested_from_tab()
            except AttributeError:
                self.close()

    def make_button(self, parent=None):
        from PySide6.QtWidgets import QPushButton

        b = QPushButton(parent)
        b.clicked.connect(self.toggle)
        self._buttons.append(b)
        self._update_buttons()
        return b


class TkTabDock:
    """Tk version: a Toplevel that can be docked into a ttk.Notebook (``wm forget``/``wm manage``).

    The toplevel must be a child of a widget that is an ancestor of the notebook (e.g. the root window).
    """

    def __init__(self, notebook, toplevel, title: str, on_closed=None) -> None:
        self.nb = notebook
        self.win = toplevel
        self.title = title
        self.on_closed = on_closed
        self.docked = False
        self._buttons: List[Any] = []
        self._geometry = None

    def _update_buttons(self) -> None:
        for b in list(self._buttons):
            try:
                b.configure(text="Detach to window" if self.docked else "Attach as tab")
            except Exception:
                self._buttons.remove(b)

    def attach(self, select: bool = True) -> None:
        if not self.docked:
            try:
                self._geometry = self.win.winfo_geometry()
            except Exception:
                self._geometry = None
            self.win.tk.call("wm", "forget", self.win._w)
            self.nb.add(self.win, text=self.title)
            self.docked = True
        if select:
            self.nb.select(self.win)
        self._update_buttons()

    def detach(self) -> None:
        if self.docked:
            self.nb.forget(self.win)
            self.win.tk.call("wm", "manage", self.win._w)
            self.win.wm_title(self.title)
            if self._geometry and not self._geometry.startswith("1x1"):
                try:
                    self.win.geometry(self._geometry)
                except Exception:
                    pass
            else:
                self.win.geometry("1350x820")
            self.win.protocol("WM_DELETE_WINDOW", self.attach)
            self.docked = False
            self.win.deiconify()
            self.win.lift()
        self._update_buttons()

    def toggle(self) -> None:
        if self.docked:
            self.detach()
        else:
            self.attach()

    def show(self) -> None:
        if self.docked:
            self.nb.select(self.win)
        else:
            self.win.deiconify()
            self.win.lift()

    def close(self) -> None:
        if self.docked:
            try:
                self.nb.forget(self.win)
            except Exception:
                pass
        cb = self.on_closed
        self.on_closed = None
        if cb is not None:
            cb()

    def make_button(self, parent):
        from tkinter import ttk

        b = ttk.Button(parent, command=self.toggle)
        self._buttons.append(b)
        self._update_buttons()
        return b


# ---------------------------------------------------------------------------
# Saved files: open the folder from a message and from a permanent link
# ---------------------------------------------------------------------------

_LAST_SAVED: Dict[str, str] = {}


def open_in_file_manager(path: str) -> bool:
    """Open a folder, or show a file selected in its folder (Explorer / Finder / file manager)."""
    import os
    import subprocess
    import sys

    path = os.path.abspath(str(path))
    try:
        if sys.platform.startswith("win"):
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path if os.path.isdir(path) else os.path.dirname(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
        return True
    except Exception:
        return False


def last_saved_path() -> str:
    return _LAST_SAVED.get("path", "")


def qt_notify_saved(parent, title: str, text: str, path: str) -> None:
    """Information box with a clickable location and an 'Open folder' button.

    The path is remembered (``last_saved_path``) and every QLabel created by
    ``qt_saved_link_label`` shows it as a permanent link.
    """
    import html as _html
    import os

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

    _LAST_SAVED["path"] = str(path)
    for lab in list(_SAVED_LINK_LABELS):
        try:
            _update_link_label(lab)
        except RuntimeError:
            _SAVED_LINK_LABELS.remove(lab)
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    t = QLabel(text)
    t.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(QLabel("Location (click to open):"))
    link = QLabel(f'<a href="open">{_html.escape(str(path))}</a>')
    link.setTextFormat(Qt.RichText)
    link.setTextInteractionFlags(Qt.TextBrowserInteraction)
    link.setWordWrap(True)
    link.linkActivated.connect(lambda _h: open_in_file_manager(path))
    lay.addWidget(link)
    hint = QLabel("The link stays at the bottom of the main window ('Open export folder').")
    hint.setStyleSheet("color: #666;")
    lay.addWidget(hint)
    row = QHBoxLayout()
    row.addStretch(1)
    b_open = QPushButton("Open folder")
    b_open.clicked.connect(lambda: (open_in_file_manager(path), dlg.accept()))
    b_ok = QPushButton("OK")
    b_ok.setDefault(True)
    b_ok.clicked.connect(dlg.accept)
    row.addWidget(b_open)
    row.addWidget(b_ok)
    lay.addLayout(row)
    dlg.resize(560, dlg.sizeHint().height())
    dlg.exec()


_SAVED_LINK_LABELS: List[Any] = []


def _update_link_label(lab) -> None:
    import html as _html
    import os

    p = last_saved_path()
    if not p:
        lab.setText("")
        lab.setToolTip("")
        return
    folder = p if os.path.isdir(p) else os.path.dirname(p)
    lab.setText(f'<a href="open">Open export folder</a>')
    lab.setToolTip(folder)


def qt_saved_link_label(parent=None):
    """A QLabel 'Open export folder' that always points to the last saved file / folder."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    lab = QLabel(parent)
    lab.setTextFormat(Qt.RichText)
    lab.setTextInteractionFlags(Qt.TextBrowserInteraction)
    lab.linkActivated.connect(lambda _h: open_in_file_manager(last_saved_path()) if last_saved_path() else None)
    _SAVED_LINK_LABELS.append(lab)
    _update_link_label(lab)
    return lab


def remember_saved(path: str) -> None:
    """Remember a saved file / folder for the 'Open export folder' links without showing a message."""
    _LAST_SAVED["path"] = str(path)
    for lab in list(_SAVED_LINK_LABELS):
        try:
            _update_link_label(lab)
        except RuntimeError:
            _SAVED_LINK_LABELS.remove(lab)


# ---------------------------------------------------------------------------
# Mouse-wheel zoom for matplotlib canvases (Tk and Qt)
# ---------------------------------------------------------------------------

def mpl_wheel_zoom(canvas, factor: float = 1.25, skip=None) -> None:
    """Wheel up = zoom in, wheel down = zoom out, centred on the mouse; the toolbar Home button resets.

    ``skip(ax)`` may return True for axes that handle the wheel themselves (e.g. interactive maps).
    """
    if getattr(canvas, "_declim_wheel_zoom", False):
        return
    canvas._declim_wheel_zoom = True

    def on_scroll(event) -> None:
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        if skip is not None:
            try:
                if skip(ax):
                    return
            except Exception:
                pass
        try:
            tb = getattr(canvas, "toolbar", None)
            if tb is not None and hasattr(tb, "push_current"):
                if not getattr(tb, "_nav_stack", None) or tb._nav_stack() is None:
                    tb.push_current()
        except Exception:
            pass
        scale = 1.0 / factor if event.button == "up" else factor
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        cx, cy = event.xdata, event.ydata
        ax.set_xlim(cx - (cx - x0) * scale, cx + (x1 - cx) * scale)
        if ax.get_navigate_mode() is None or True:
            ax.set_ylim(cy - (cy - y0) * scale, cy + (y1 - cy) * scale)
        canvas.draw_idle()

    canvas.mpl_connect("scroll_event", on_scroll)
