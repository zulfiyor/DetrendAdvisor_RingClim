#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dendro_online_tk.py - Tkinter dialog: search and download ITRDB ring-width
measurements (.rwl) with a location map. Used by Detrend Advisor
("Project > Import from ITRDB (online)...") and runnable on its own:

    python dendro_online_tk.py

The host passes ``on_import(entries)``; each entry is a dict
    {"path": str, "rwl": pandas.DataFrame (index=year, columns=series, mm),
     "meta": dict (site, precision, DOI, citation ...)}
Ring widths are converted to millimetres using each series' end marker
(999 -> 0.01 mm, -9999 -> 0.001 mm); zero widths become NaN, as in
Detrend Advisor's own Tucson reader.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

import dendro_online as DO

try:  # developer signature + hover help (optional)
    import dendro_ui_help as _uihelp
except Exception:
    _uihelp = None
try:  # download registry + location export (optional)
    import dendro_geoexport as GX
except Exception:
    GX = None


def _decorate(win: tk.Misc) -> None:
    if _uihelp is not None:
        try:
            _uihelp.tk_decorate(win)
        except Exception:
            pass

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".dendro_online_tk.json")

_VARIANT_SHORT = {"standard": "std", "residual": "res", "arstan": "ars"}


def _load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_settings(data: Dict[str, Any]) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    except Exception:
        pass


class ItrdbImportDialog(tk.Toplevel):
    """Non-modal Toplevel for ITRDB search / download / import."""

    def __init__(self, master: tk.Misc, on_import: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
                 default_dir: Optional[str] = None) -> None:
        super().__init__(master)
        self.title("Import from ITRDB (online)")
        self.geometry("1350x820")
        self.minsize(900, 600)
        self._on_import = on_import
        self._settings = _load_settings()
        self._sites: List[DO.ItrdbSite] = []
        self._site_by_iid: Dict[str, DO.ItrdbSite] = {}
        self._file_by_iid: Dict[str, Any] = {}
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._map_job: Optional[str] = None
        self._poll_job: Optional[str] = None
        self._center: Optional[tuple] = None

        s = self._settings
        self.var_mode = tk.StringVar(value=s.get("mode", "point"))
        self.var_lat = tk.DoubleVar(value=float(s.get("lat", 40.28)))
        self.var_lon = tk.DoubleVar(value=float(s.get("lon", 69.62)))
        self.var_radius = tk.DoubleVar(value=float(s.get("radius", 300.0)))
        self.var_min_lat = tk.DoubleVar(value=float(s.get("min_lat", 36.0)))
        self.var_max_lat = tk.DoubleVar(value=float(s.get("max_lat", 42.0)))
        self.var_min_lon = tk.DoubleVar(value=float(s.get("min_lon", 66.0)))
        self.var_max_lon = tk.DoubleVar(value=float(s.get("max_lon", 76.0)))
        self.var_species = tk.StringVar(value=s.get("species", ""))
        self.var_year_from = tk.StringVar(value=s.get("year_from", ""))
        self.var_year_to = tk.StringVar(value=s.get("year_to", ""))
        self.var_text = tk.StringVar(value=s.get("text", ""))
        self.var_dir = tk.StringVar(value=s.get("dir") or default_dir or os.path.join(DO.default_cache_dir(), "itrdb"))
        self.var_add = tk.BooleanVar(value=bool(s.get("add_to_project", True)))
        self.var_status = tk.StringVar(value="Ready.")

        self._build_ui()
        for v in (self.var_lat, self.var_lon, self.var_radius, self.var_min_lat, self.var_max_lat,
                  self.var_min_lon, self.var_max_lon, self.var_mode):
            v.trace_add("write", lambda *a: self._schedule_map())
        self._update_mode()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._map_job = self.after(100, self._draw_map)
        self._poll_job = self.after(100, self._poll_queue)
        _decorate(self)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 0))
        ttk.Label(top, text="ITRDB search, preview and import", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Close", command=self.on_close).pack(side=tk.RIGHT, padx=2)
        self._dock_btn_parent = top
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        # left side scrolls when the window (or the tab) is not high enough
        left_outer = ttk.Frame(paned)
        self._left_canvas = tk.Canvas(left_outer, highlightthickness=0, borderwidth=0)
        left_sb = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=self._left_canvas.yview)
        self._left_canvas.configure(yscrollcommand=left_sb.set)
        left_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left = ttk.Frame(self._left_canvas)
        self._left_win = self._left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _sync(_e=None) -> None:
            c = self._left_canvas
            c.configure(scrollregion=c.bbox("all"))
            need = left.winfo_reqheight()
            c.itemconfigure(self._left_win, width=c.winfo_width(),
                            height=max(need, c.winfo_height()))

        left.bind("<Configure>", _sync)
        self._left_canvas.bind("<Configure>", _sync)

        def _wheel(e) -> None:
            try:
                if not self.winfo_exists():
                    return
                w = e.widget
                # keep the wheel for tables / text / the map
                if isinstance(w, (ttk.Treeview, tk.Text, tk.Listbox)):
                    return
                if str(w).startswith(str(left)) or w is self._left_canvas:
                    self._left_canvas.yview_scroll(int(-e.delta / 120) if e.delta else 1, "units")
            except Exception:
                pass

        self.bind_all("<MouseWheel>", _wheel, add="+")
        right = ttk.Frame(paned)
        paned.add(left_outer, weight=5)
        paned.add(right, weight=6)

        area = ttk.LabelFrame(left, text="Search area")
        area.pack(fill=tk.X, pady=(0, 4))
        ttk.Radiobutton(area, text="Around point", value="point", variable=self.var_mode,
                        command=self._update_mode).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._point_widgets = []
        for col, (label, var) in enumerate((("Lat:", self.var_lat), ("Lon:", self.var_lon), ("Radius km:", self.var_radius))):
            ttk.Label(area, text=label).grid(row=0, column=1 + 2 * col, sticky="e", padx=2)
            e = ttk.Entry(area, textvariable=var, width=9)
            e.grid(row=0, column=2 + 2 * col, sticky="w", padx=2)
            self._point_widgets.append(e)
        ttk.Radiobutton(area, text="Bounding box", value="bbox", variable=self.var_mode,
                        command=self._update_mode).grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._bbox_widgets = []
        for k, (label, var) in enumerate((("Lat from:", self.var_min_lat), ("to:", self.var_max_lat),
                                          ("Lon from:", self.var_min_lon), ("to:", self.var_max_lon))):
            r, c = 1 + k // 2, 1 + 2 * (k % 2)
            ttk.Label(area, text=label).grid(row=r, column=c, sticky="e", padx=2)
            e = ttk.Entry(area, textvariable=var, width=9)
            e.grid(row=r, column=c + 1, sticky="w", padx=2)
            self._bbox_widgets.append(e)
        ttk.Radiobutton(area, text="Anywhere (use the filters only)", value="world", variable=self.var_mode,
                        command=self._update_mode).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=2)

        filt = ttk.LabelFrame(left, text="Filters (optional)")
        filt.pack(fill=tk.X, pady=4)
        ttk.Label(filt, text="Species:").grid(row=0, column=0, sticky="e", padx=2, pady=2)
        ttk.Entry(filt, textvariable=self.var_species, width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(filt, text="Years from:").grid(row=0, column=2, sticky="e", padx=2)
        ttk.Entry(filt, textvariable=self.var_year_from, width=7).grid(row=0, column=3, sticky="w")
        ttk.Label(filt, text="to:").grid(row=0, column=4, sticky="e", padx=2)
        ttk.Entry(filt, textvariable=self.var_year_to, width=7).grid(row=0, column=5, sticky="w")
        ttk.Label(filt, text="Text:").grid(row=1, column=0, sticky="e", padx=2, pady=2)
        ent_text = ttk.Entry(filt, textvariable=self.var_text, width=40)
        ent_text.grid(row=1, column=1, columnspan=5, sticky="we")
        ent_text.bind("<Return>", lambda e: self.search())
        ttk.Label(filt, text="Species: code(s) like JUTU, PCAB or a tree name (juniper, Pinus).  Text: e.g. "
                             "'Juniper Turkey', 'PCAB Alps', 'Esper Kyrgyzstan' - a country or region "
                             "replaces the search area; text searches world-wide.", foreground="gray", wraplength=520, justify="left").grid(
            row=2, column=0, columnspan=6, sticky="w", padx=2)
        self.lbl_query = ttk.Label(filt, text="", foreground="#1F3864", wraplength=520, justify="left")
        self.lbl_query.grid(row=3, column=0, columnspan=6, sticky="w", padx=2)

        row = ttk.Frame(left)
        row.pack(fill=tk.X, pady=4)
        self.btn_search = ttk.Button(row, text="Search ITRDB", command=self.search)
        self.btn_search.pack(side=tk.LEFT)
        self.lbl_count = ttk.Label(row, text="")
        self.lbl_count.pack(side=tk.LEFT, padx=8)

        sites_frame = ttk.Frame(left)
        sites_frame.pack(fill=tk.BOTH, expand=True)
        cols = ("code", "site", "species", "lat", "lon", "elev", "first", "last", "dist", "rwl")
        heads = ("Code", "Site", "Species", "Lat", "Lon", "Elev", "First", "Last", "Dist km", "RWL files")
        widths = (75, 170, 150, 60, 60, 50, 50, 50, 60, 70)
        self.tree_sites = ttk.Treeview(sites_frame, columns=cols, show="headings", selectmode="extended", height=10)
        for c, h, w in zip(cols, heads, widths):
            self.tree_sites.heading(c, text=h, command=lambda c=c: self._sort_sites(c))
            self.tree_sites.column(c, width=w, stretch=(c in ("site", "species")), anchor="w")
        ysb = ttk.Scrollbar(sites_frame, orient=tk.VERTICAL, command=self.tree_sites.yview)
        self.tree_sites.configure(yscrollcommand=ysb.set)
        self.tree_sites.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.LEFT, fill=tk.Y)
        self.tree_sites.bind("<<TreeviewSelect>>", lambda e: self._on_site_select())

        files_box = ttk.LabelFrame(left, text="Ring-width files of the selected site(s)  (select = download)")
        files_box.pack(fill=tk.BOTH, expand=True, pady=4)
        self.tree_files = ttk.Treeview(files_box, columns=("site", "parameter", "file"), show="headings",
                                       selectmode="extended", height=6)
        for c, h, w in (("site", "Site", 80), ("parameter", "Parameter", 150), ("file", "File", 260)):
            self.tree_files.heading(c, text=h)
            self.tree_files.column(c, width=w, anchor="w", stretch=(c == "file"))
        self.tree_files.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.tree_files.bind("<Double-1>", lambda e: self.preview())
        # user clicks / arrow keys on a file -> Preview tab (programmatic selection does not trigger this)
        self.tree_files.bind("<ButtonRelease-1>", lambda e: self._schedule_preview())
        self.tree_files.bind("<KeyRelease-Up>", lambda e: self._schedule_preview())
        self.tree_files.bind("<KeyRelease-Down>", lambda e: self._schedule_preview())

        dl = ttk.LabelFrame(left, text="Download / import")
        dl.pack(fill=tk.X, pady=4)
        ttk.Label(dl, text="Save files to:").grid(row=0, column=0, sticky="e", padx=2, pady=2)
        ttk.Entry(dl, textvariable=self.var_dir, width=50).grid(row=0, column=1, sticky="we", padx=2)
        ttk.Button(dl, text="Browse...", command=self._choose_dir).grid(row=0, column=2, padx=2)
        ttk.Checkbutton(dl, text="Add downloaded .rwl files to the project", variable=self.var_add).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=2)
        btns = ttk.Frame(dl)
        btns.grid(row=2, column=0, columnspan=3, sticky="we", pady=2)
        self.btn_preview = ttk.Button(btns, text="Preview...", command=self.preview)
        self.btn_preview.pack(side=tk.LEFT, padx=2)
        self.btn_download = ttk.Button(btns, text="Download selected", command=self.download)
        self.btn_download.pack(side=tk.LEFT, padx=2)
        self.btn_batch = ttk.Button(btns, text="Batch download...", command=self.batch_download)
        self.btn_batch.pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Open study page", command=self._open_study).pack(side=tk.LEFT, padx=2)
        self.btn_export_loc = ttk.Button(btns, text="Export locations...", command=self._export_locations)
        self.btn_export_loc.pack(side=tk.LEFT, padx=2)
        dl.grid_columnconfigure(1, weight=1)

        # right side: Map / Preview tabs
        self.right_nb = ttk.Notebook(right)
        self.right_nb.pack(fill=tk.BOTH, expand=True)
        map_tab = ttk.Frame(self.right_nb)
        prev_tab = ttk.Frame(self.right_nb)
        self.right_nb.add(map_tab, text="Map")
        self.right_nb.add(prev_tab, text="Preview")
        self._prev_tab = prev_tab

        self.figure = Figure(figsize=(7, 5))
        self.canvas = FigureCanvasTkAgg(self.figure, master=map_tab)
        tb = NavigationToolbar2Tk(self.canvas, map_tab, pack_toolbar=False)
        tb.update()
        tb.pack(side=tk.TOP, fill=tk.X)
        map_row = ttk.Frame(map_tab)
        map_row.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(map_row, text="Redraw map", command=lambda: self._redraw_map("auto")).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Button(map_row, text="World", command=lambda: self._redraw_map("world")).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Label(map_row, text="Click: point / site   Drag: box   Wheel: zoom   Right-drag: pan",
                  foreground="#555").pack(side=tk.LEFT, padx=6)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._view_mode = "keep"
        self.interactor = DO.MapInteractor(self.canvas, self._map_point, self._map_box) \
            if hasattr(DO, "MapInteractor") else None

        pv_pan = ttk.PanedWindow(prev_tab, orient=tk.VERTICAL)
        pv_pan.pack(fill=tk.BOTH, expand=True)
        pv_top = ttk.Frame(pv_pan)
        pv_bottom = ttk.Frame(pv_pan)
        pv_pan.add(pv_top, weight=3)
        pv_pan.add(pv_bottom, weight=2)
        self.pv_figure = Figure(figsize=(7, 4.5))
        self.pv_canvas = FigureCanvasTkAgg(self.pv_figure, master=pv_top)
        pv_tb = NavigationToolbar2Tk(self.pv_canvas, pv_top, pack_toolbar=False)
        try:
            import dendro_ui_help as _uh

            _uh.mpl_wheel_zoom(self.pv_canvas)
        except Exception:
            pass
        pv_tb.update()
        pv_tb.pack(side=tk.TOP, fill=tk.X)
        self.pv_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.pv_text = tk.Text(pv_bottom, wrap="none", font=("Consolas", 9), height=10)
        self.pv_text.pack(fill=tk.BOTH, expand=True)
        self.btn_pv_import = ttk.Button(pv_bottom, text="Download & add this file", command=self._import_previewed,
                                        state=tk.DISABLED)
        self.btn_pv_import.pack(side=tk.LEFT, padx=2, pady=3)
        self._pv_current: Optional[Any] = None
        self._preview_cache: Dict[str, Any] = {}
        self._preview_job: Optional[str] = None
        self._pv_message("Click a .rwl file in the list to preview it here.")

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100, length=220)
        self.progress.pack(side=tk.RIGHT, padx=4)
        self.btn_cancel = ttk.Button(bottom, text="Cancel", command=self._cancel_task, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.RIGHT, padx=4)
        ttk.Label(bottom, textvariable=self.var_status).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _update_mode(self) -> None:
        mode = self.var_mode.get()
        for w in self._point_widgets:
            w.configure(state=tk.NORMAL if mode == "point" else tk.DISABLED)
        for w in self._bbox_widgets:
            w.configure(state=tk.NORMAL if mode == "bbox" else tk.DISABLED)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _float(var: tk.Variable, default: float) -> float:
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    @staticmethod
    def _int_or_none(var: tk.StringVar) -> Optional[int]:
        text = var.get().strip()
        try:
            return int(text) if text else None
        except ValueError:
            return None

    def _area(self):
        if self.var_mode.get() == "point":
            return DO.bbox_around(self._float(self.var_lat, 0.0), self._float(self.var_lon, 0.0),
                                  max(1.0, self._float(self.var_radius, 300.0)))
        a, b = self._float(self.var_min_lat, -90), self._float(self.var_max_lat, 90)
        c, d = self._float(self.var_min_lon, -180), self._float(self.var_max_lon, 180)
        return (min(a, b), max(a, b), min(c, d), max(c, d))

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.btn_search.configure(state=state)
        self.btn_download.configure(state=state)
        self.btn_preview.configure(state=state)
        self.btn_batch.configure(state=state)
        self.btn_cancel.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
        if msg:
            self.var_status.set(msg)

    def _start(self, fn: Callable[[Callable, Callable], Any], on_done: Callable[[Any], None], msg: str) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("ITRDB", "Another operation is still running.", parent=self)
            return
        self._cancel = threading.Event()

        def progress(stage: str, done: int, total: int, message: str) -> None:
            self._queue.put(("progress", (done, total, message)))

        def run() -> None:
            try:
                res = fn(progress, self._cancel.is_set)
            except DO.CancelledError:
                self._queue.put(("failed", "Cancelled by user."))
            except Exception as exc:
                self._queue.put(("failed", f"{exc}\n\n{traceback.format_exc()}"))
            else:
                self._queue.put(("done", (on_done, res)))

        self._set_busy(True, msg)
        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    done, total, message = payload
                    if total > 0:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=100.0 * done / total)
                    if message:
                        self.var_status.set(message)
                elif kind == "done":
                    on_done, res = payload
                    self._set_busy(False, "Done.")
                    try:
                        on_done(res)
                    except Exception as exc:
                        messagebox.showerror("ITRDB", str(exc), parent=self)
                elif kind == "failed":
                    self._set_busy(False, "Cancelled." if "Cancelled" in payload else "Failed.")
                    if "Cancelled" not in payload:
                        messagebox.showwarning("ITRDB", payload.split("\n\n")[0], parent=self)
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self._poll_job = self.after(100, self._poll_queue)
        except tk.TclError:
            pass

    def _cancel_task(self) -> None:
        self._cancel.set()
        self.var_status.set("Cancelling...")

    def _choose_dir(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Folder for ITRDB files", initialdir=self.var_dir.get())
        if d:
            self.var_dir.set(d)

    # ------------------------------------------------------------ map
    def _schedule_map(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.right_nb.select(0)  # location changed -> show the map
        except Exception:
            pass
        if self._map_job is not None:
            try:
                self.after_cancel(self._map_job)
            except Exception:
                pass
        self._map_job = self.after(400, self._draw_map)

    def _draw_map(self) -> None:
        self._map_job = None
        sites = [(s.lat, s.lon, s.site_code) for s in self._sites if s.lat is not None and s.lon is not None]
        sel = self._selected_sites()
        title = None
        if sel and sel[0].lat is not None:
            lat, lon = sel[0].lat, sel[0].lon
            title = f"{sel[0].site_code} {sel[0].site_name} | {lat:.3f} N, {lon:.3f} E"
        elif self.var_mode.get() == "point":
            lat, lon = self._float(self.var_lat, 0.0), self._float(self.var_lon, 0.0)
        else:
            a = self._area()
            lat, lon = (a[0] + a[1]) / 2, (a[2] + a[3]) / 2
        view = None
        if self._view_mode == "world":
            view = DO.WORLD_VIEW
        elif self._view_mode == "keep" and hasattr(DO, "user_map_view"):
            view = DO.user_map_view(self.figure, lat, lon)
        self._view_mode = "keep"
        bbox = self._area()
        span = 30.0
        if self.var_mode.get() == "world":
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
        try:
            DO.draw_location_map(self.figure, lat=lat, lon=lon, bbox=bbox, sites=sites,
                                 title=title, max_span_deg=span, zoom=False, grid_res=0.0,
                                 **({"view": view} if view is not None else {}))
        except Exception as exc:
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, f"Map error: {exc}", ha="center", va="center", transform=ax.transAxes)
            ax.set_axis_off()
        self.canvas.draw_idle()

    # ------------------------------------------------------------ search
    def search(self) -> None:
        mode = self.var_mode.get()
        text = self.var_text.get().strip()
        if text and mode != "world":
            # free text searches the whole world (or the country / region named in the text)
            self.var_mode.set("world")
            self._update_mode()
            mode = "world"
        area = None if mode == "world" else self._area()
        species = self.var_species.get().strip()
        y0 = self._int_or_none(self.var_year_from)
        y1 = self._int_or_none(self.var_year_to)
        area_from_text = DO.parse_itrdb_text(text, species).bbox() is not None
        center = None
        radius = None
        if mode == "point" and not area_from_text:
            center = (self._float(self.var_lat, 0.0), self._float(self.var_lon, 0.0))
            radius = self._float(self.var_radius, 300.0)

        def task(progress, cancel):
            progress("search", 0, 0, "Searching ITRDB ...")
            sites, desc = DO.smart_search_itrdb(text=text, species=species, area=area,
                                                earliest_year=y0, latest_year=y1)
            self._last_query = desc
            sites = [s for s in sites if s.files_of_kind("rwl")]
            if center is not None:
                sites = [s for s in sites if s.lat is None
                         or DO.haversine_km(center[0], center[1], s.lat, s.lon) <= radius * 1.001]
            return sites, center

        self._start(task, self._show_sites, "Searching ITRDB ...")

    def _show_sites(self, result) -> None:
        sites, center = result
        self._center = center
        self._sites = list(sites)
        self.tree_sites.delete(*self.tree_sites.get_children())
        self.tree_files.delete(*self.tree_files.get_children())
        self._site_by_iid.clear()
        rows = []
        for s in self._sites:
            dist = (DO.haversine_km(center[0], center[1], s.lat, s.lon)
                    if center is not None and s.lat is not None else None)
            rows.append((dist if dist is not None else 1e9, s, dist))
        rows.sort(key=lambda r: (r[0], r[1].site_code))
        for _, s, dist in rows:
            iid = self.tree_sites.insert("", tk.END, values=(
                s.site_code, s.site_name, s.species,
                "" if s.lat is None else f"{s.lat:.3f}", "" if s.lon is None else f"{s.lon:.3f}",
                "" if s.elevation_m is None else f"{s.elevation_m:.0f}",
                s.first_year if s.first_year is not None else "", s.last_year if s.last_year is not None else "",
                "" if dist is None else f"{dist:.0f}", len(s.files_of_kind("rwl"))))
            self._site_by_iid[iid] = s
        self.lbl_count.configure(text=f"{len(self._sites)} site(s) with ring-width files")
        self.lbl_query.configure(text="Searched: " + str(getattr(self, "_last_query", "")))
        self._draw_map()

    # ------------------------------------------------------------ map picking
    def _redraw_map(self, mode: str) -> None:
        self._view_mode = mode
        self._draw_map()

    def _map_point(self, lat: float, lon: float, add: bool = False) -> None:
        ax = getattr(self.figure, "_dendro_main_ax", None)
        if ax is not None and self._site_by_iid:
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            tol = 0.015 * max(abs(x1 - x0), abs(y1 - y0))
            best, best_d = None, tol
            for iid, st in self._site_by_iid.items():
                if st.lat is None or st.lon is None:
                    continue
                d = max(abs(st.lat - lat), abs(st.lon - lon))
                if d <= best_d:
                    best, best_d = iid, d
            if best is not None:
                self.tree_sites.selection_set(best)
                self.tree_sites.see(best)
                return
        if self.tree_sites.selection():
            self.tree_sites.selection_remove(*self.tree_sites.selection())
        self.var_mode.set("point")
        self.var_lat.set(round(lat, 4))
        self.var_lon.set(round(lon, 4))
        self._update_mode()
        self.var_status.set(f"Search point set on the map: {lat:.4f} N, {lon:.4f} E")

    def _map_box(self, s: float, n: float, w: float, e: float) -> None:
        self.var_mode.set("bbox")
        self.var_min_lat.set(round(s, 4))
        self.var_max_lat.set(round(n, 4))
        self.var_min_lon.set(round(w, 4))
        self.var_max_lon.set(round(e, 4))
        self._update_mode()
        self.var_status.set(f"Search box set on the map: {s:.2f}..{n:.2f} N, {w:.2f}..{e:.2f} E")

    def _sort_sites(self, col: str) -> None:
        items = [(self.tree_sites.set(i, col), i) for i in self.tree_sites.get_children("")]

        def key(v):
            try:
                return (0, float(v[0]))
            except ValueError:
                return (1, str(v[0]))

        reverse = getattr(self, "_sort_reverse", {}).get(col, False)
        items.sort(key=key, reverse=reverse)
        for idx, (_, iid) in enumerate(items):
            self.tree_sites.move(iid, "", idx)
        self._sort_reverse = {col: not reverse}

    def _selected_sites(self) -> List[DO.ItrdbSite]:
        return [self._site_by_iid[i] for i in self.tree_sites.selection() if i in self._site_by_iid]

    def _on_site_select(self) -> None:
        self.tree_files.delete(*self.tree_files.get_children())
        self._file_by_iid.clear()
        default_sel = []
        for s in self._selected_sites():
            files = sorted(s.files_of_kind("rwl"), key=lambda f: (f.parameter != "Total ring width", f.filename))
            for k, f in enumerate(files):
                iid = self.tree_files.insert("", tk.END, values=(s.site_code, f.parameter, f.filename))
                self._file_by_iid[iid] = (s, f)
                if k == 0:
                    default_sel.append(iid)
        if default_sel:
            self.tree_files.selection_set(default_sel)
        self._schedule_map()

    def _open_study(self) -> None:
        import webbrowser

        for s in self._selected_sites()[:5]:
            webbrowser.open(s.study_url)

    # ------------------------------------------------------------ preview
    def preview(self, quiet: bool = False) -> None:
        self._preview_job = None
        sel = [i for i in self.tree_files.selection() if i in self._file_by_iid]
        focus = self.tree_files.focus()
        iid = focus if focus in self._file_by_iid else (sel[0] if sel else None)
        if iid is None:
            if not quiet:
                messagebox.showinfo("Preview", "Select a .rwl file first.", parent=self)
            return
        site, f = self._file_by_iid[iid]
        self.right_nb.select(self._prev_tab)
        if f.url in self._preview_cache:
            self._show_preview(self._preview_cache[f.url], site, f)
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._pv_message(f"Loading {f.filename} ...")

        def task(progress, cancel):
            progress("preview", 0, 0, f"Loading {f.filename} for preview ...")
            return DO.preview_itrdb_file(f, site)

        self._start(task, lambda pv: self._show_preview(pv, site, f), f"Loading {f.filename} for preview ...")

    def _pv_message(self, text: str) -> None:
        self._pv_current = None
        self.btn_pv_import.configure(state=tk.DISABLED)
        self.pv_figure.clear()
        ax = self.pv_figure.add_subplot(111)
        ax.text(0.5, 0.5, text, ha="center", va="center", transform=ax.transAxes, fontsize=9)
        ax.set_axis_off()
        self.pv_canvas.draw_idle()
        self.pv_text.configure(state=tk.NORMAL)
        self.pv_text.delete("1.0", tk.END)
        self.pv_text.configure(state=tk.DISABLED)

    def _show_preview(self, pv: "DO.ItrdbPreview", site: DO.ItrdbSite, f: DO.ItrdbFile) -> None:
        self._preview_cache[f.url] = pv
        self._pv_current = (site, f)
        self.right_nb.select(self._prev_tab)
        DO.draw_itrdb_preview(self.pv_figure, pv)
        self.pv_canvas.draw_idle()
        self.pv_text.configure(state=tk.NORMAL)
        self.pv_text.delete("1.0", tk.END)
        self.pv_text.insert("1.0", pv.summary_text())
        self.pv_text.configure(state=tk.DISABLED)
        self.btn_pv_import.configure(state=tk.NORMAL,
                                     text="Download & add this file" if self.var_add.get() else "Download this file")

    def _schedule_preview(self) -> None:
        if self._preview_job is not None:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.after(250, lambda: self.preview(quiet=True))

    def _import_previewed(self) -> None:
        if self._pv_current is not None:
            self._download_files([self._pv_current])

    # ------------------------------------------------------------ batch
    def batch_download(self) -> None:
        if not self._sites:
            messagebox.showinfo("Batch download", "Search ITRDB first.", parent=self)
            return
        center = None
        if self.var_mode.get() == "point":
            center = (self._float(self.var_lat, 0.0), self._float(self.var_lon, 0.0))
        BatchWindow(self, self._sites, self._selected_sites(), center, self._download_files)

    # ------------------------------------------------------------ download
    def download(self) -> None:
        chosen = [self._file_by_iid[i] for i in self.tree_files.selection() if i in self._file_by_iid]
        if not chosen:
            messagebox.showinfo("ITRDB", "Select a site and at least one .rwl file.", parent=self)
            return
        self._download_files(chosen)

    def _download_files(self, chosen: List[Any]) -> None:
        dest = self.var_dir.get().strip() or os.path.join(DO.default_cache_dir(), "itrdb")

        def task(progress, cancel):
            entries: List[Dict[str, Any]] = []
            notes: List[str] = []
            for k, (s, f) in enumerate(chosen, start=1):
                if cancel():
                    raise DO.CancelledError("Cancelled")
                progress("file", k - 1, len(chosen), f"[{k}/{len(chosen)}] {f.filename}")
                site_dir = os.path.join(dest, (s.site_code or s.study_id or "site").lower())
                try:
                    path = DO.download_file(f.url, os.path.join(site_dir, f.filename), progress=progress, cancel=cancel)
                    if GX is not None:
                        try:
                            GX.registry.add_itrdb_file(s, f, path)
                        except Exception:
                            pass
                    df, meta = DO.read_tucson_rwl(path, zero_as_nan=True)
                    precisions = sorted(set(meta["precision_mm"].values()))
                    if len(precisions) > 1:
                        notes.append(f"{f.filename}: mixed precision {precisions} mm - converted to mm per series.")
                    if meta["series_without_end_marker"]:
                        notes.append(f"{f.filename}: {len(meta['series_without_end_marker'])} series without end "
                                     f"marker (assumed 0.01 mm).")
                    entries.append({
                        "path": path,
                        "rwl": df,
                        "meta": {
                            "source": "ITRDB", "site_code": s.site_code, "site_name": s.site_name,
                            "species": s.species, "lat": s.lat, "lon": s.lon, "elevation_m": s.elevation_m,
                            "parameter": f.parameter, "file_url": f.url, "doi": s.doi,
                            "study_url": s.study_url, "investigators": s.investigators,
                            "precision_mm": meta["precision_mm"], "units": "mm",
                            "citation": DO.ITRDB_CITATION,
                        },
                    })
                except DO.CancelledError:
                    raise
                except Exception as exc:  # one bad file must not stop a batch
                    notes.append(f"FAILED {f.filename}: {exc}")
            progress("done", len(chosen), len(chosen), "Download finished")
            return entries, notes

        self._start(task, self._downloaded, f"Downloading {len(chosen)} file(s) ...")

    def _downloaded(self, result) -> None:
        entries, notes = result
        text = "\n".join(f"{os.path.basename(e['path'])}: {e['rwl'].shape[1]} series, "
                         f"{int(e['rwl'].index.min())}-{int(e['rwl'].index.max())}" for e in entries)
        if notes:
            text += "\n\nNotes:\n" + "\n".join(notes)
        if self.var_add.get() and self._on_import is not None:
            self._on_import(entries)
            text += "\n\nAdded to the project (ring widths in mm)."
        else:
            text += f"\n\nSaved to: {self.var_dir.get()}"
        self.var_status.set(f"Downloaded {len(entries)} file(s).")
        messagebox.showinfo("ITRDB", text, parent=self)

    def _export_locations(self) -> None:
        if GX is None:
            messagebox.showwarning("Export locations", "Module dendro_geoexport.py is missing.", parent=self)
            return
        ExportLocationsWindow(self, default_dir=self.var_dir.get().strip())

    # ------------------------------------------------------------ docking
    def set_dock(self, dock) -> None:
        """``dock`` is a dendro_ui_help.TkTabDock (tab of the main window or separate window)."""
        self._dock = dock
        dock.make_button(self._dock_btn_parent).pack(side=tk.RIGHT, padx=2)

    # ------------------------------------------------------------ close
    def on_close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno("ITRDB", "An operation is running. Cancel it and close?", parent=self):
                return
            self._cancel.set()
        self._settings.update({
            "mode": self.var_mode.get(),
            "lat": self._float(self.var_lat, 40.28), "lon": self._float(self.var_lon, 69.62),
            "radius": self._float(self.var_radius, 300.0),
            "min_lat": self._float(self.var_min_lat, 36.0), "max_lat": self._float(self.var_max_lat, 42.0),
            "min_lon": self._float(self.var_min_lon, 66.0), "max_lon": self._float(self.var_max_lon, 76.0),
            "species": self.var_species.get().strip(), "year_from": self.var_year_from.get().strip(),
            "year_to": self.var_year_to.get().strip(), "text": self.var_text.get().strip(),
            "dir": self.var_dir.get().strip(), "add_to_project": bool(self.var_add.get()),
        })
        _save_settings(self._settings)
        dock = getattr(self, "_dock", None)
        self._dock = None
        if dock is not None:
            dock.close()
        self.destroy()

    def destroy(self) -> None:
        for job in (self._map_job, self._poll_job, getattr(self, "_preview_job", None)):
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._map_job = self._poll_job = None
        super().destroy()


class BatchWindow(tk.Toplevel):
    """Pick .rwl files from many ITRDB sites by criteria."""

    def __init__(self, master: tk.Misc, all_sites: List[DO.ItrdbSite], selected: List[DO.ItrdbSite],
                 center: Optional[tuple], on_download: Callable[[List[Any]], None]) -> None:
        super().__init__(master)
        self.title("Batch download from ITRDB (.rwl)")
        self.geometry("760x620")
        self.transient(master)
        self._all, self._selected, self._center = list(all_sites), list(selected), center
        self._on_download = on_download
        self.files: List[Any] = []
        self.var_scope = tk.StringVar(value="selected" if len(selected) > 1 else "all")
        self.var_span = tk.StringVar(value="")
        self.var_last = tk.StringVar(value="")
        self.var_first = tk.StringVar(value="")
        self.var_dist = tk.StringVar(value="")

        sc = ttk.LabelFrame(self, text="Sites")
        sc.pack(fill=tk.X, padx=6, pady=4)
        ttk.Radiobutton(sc, text=f"All sites in the results ({len(self._all)})", value="all",
                        variable=self.var_scope, command=self._update).pack(side=tk.LEFT, padx=4)
        rb = ttk.Radiobutton(sc, text=f"Selected sites ({len(self._selected)})", value="selected",
                             variable=self.var_scope, command=self._update)
        rb.pack(side=tk.LEFT, padx=12)
        if not self._selected:
            rb.configure(state=tk.DISABLED)

        pf = ttk.LabelFrame(self, text="Parameter")
        pf.pack(fill=tk.X, padx=6, pady=4)
        params = sorted({f.parameter for s in self._all for f in s.files_of_kind("rwl")},
                        key=lambda p: (p != "Total ring width", p))
        self.param_vars: Dict[str, tk.BooleanVar] = {}
        for k, prm in enumerate(params):
            v = tk.BooleanVar(value=(prm == "Total ring width"))
            self.param_vars[prm] = v
            ttk.Checkbutton(pf, text=prm, variable=v, command=self._update).grid(row=k // 3, column=k % 3,
                                                                                sticky="w", padx=4)

        cf = ttk.LabelFrame(self, text="Criteria (empty = no limit)")
        cf.pack(fill=tk.X, padx=6, pady=4)
        for r, (label, var) in enumerate((("Minimum record length (years):", self.var_span),
                                          ("Last year at least:", self.var_last),
                                          ("First year at most:", self.var_first),
                                          ("Max distance from point (km):", self.var_dist))):
            ttk.Label(cf, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=1)
            e = ttk.Entry(cf, textvariable=var, width=10)
            e.grid(row=r, column=1, sticky="w")
            if var is self.var_dist and center is None:
                e.configure(state=tk.DISABLED)
            var.trace_add("write", lambda *a: self._update())
        ttk.Label(cf, text="Tip: 'Last year at least' = 1950 keeps records that overlap CRU TS well.",
                  foreground="gray").grid(row=4, column=0, columnspan=2, sticky="w", padx=4)

        self.lbl = ttk.Label(self, text="")
        self.lbl.pack(anchor="w", padx=8)
        lf = ttk.Frame(self)
        lf.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self.lst = tk.Listbox(lf, font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        row = ttk.Frame(self)
        row.pack(fill=tk.X, padx=6, pady=6)
        self.btn_ok = ttk.Button(row, text="Download matching files", command=self._ok)
        self.btn_ok.pack(side=tk.RIGHT, padx=2)
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=2)
        self._update()
        _decorate(self)

    @staticmethod
    def _num(var: tk.StringVar) -> Optional[float]:
        try:
            t = var.get().strip()
            return float(t) if t else None
        except ValueError:
            return None

    def _update(self) -> None:
        sites = self._selected if self.var_scope.get() == "selected" else self._all
        span, last, first, dist = (self._num(v) for v in (self.var_span, self.var_last, self.var_first, self.var_dist))
        crit = DO.ItrdbBatchCriteria(
            kinds=("rwl",), parameters=tuple(p for p, v in self.param_vars.items() if v.get()) or ("__none__",),
            variants=(), min_span_years=int(span or 0),
            min_last_year=int(last) if last else None, max_first_year=int(first) if first else None,
            max_distance_km=dist if dist else None, center=self._center)
        self.files = DO.select_itrdb_files(sites, crit)
        self.lst.delete(0, tk.END)
        for s, f in self.files:
            self.lst.insert(tk.END, f"{s.site_code:<9} {f.parameter:<18} {f.filename:<16} "
                                    f"{s.first_year}-{s.last_year}  {s.species[:32]}")
        n_sites = len({s.study_id for s, _ in self.files})
        self.lbl.configure(text=f"{len(self.files)} file(s) from {n_sites} site(s) match")
        self.btn_ok.configure(state=tk.NORMAL if self.files else tk.DISABLED)

    def _ok(self) -> None:
        if not self.files:
            return
        if len(self.files) > 50 and not messagebox.askyesno("Batch download",
                                                            f"Download {len(self.files)} files?", parent=self):
            return
        files = list(self.files)
        self.destroy()
        self._on_download(files)


class ExportLocationsWindow(tk.Toplevel):
    """Export the coordinates and attributes of downloaded data (CSV, GeoJSON, KMZ, Shapefile)."""

    def __init__(self, master: tk.Misc, default_dir: str = "") -> None:
        super().__init__(master)
        self.title("Export locations of downloaded data")
        self.transient(master)
        self.geometry("760x560")
        self.var_session = tk.BooleanVar(value=False)
        self.var_dir = tk.StringVar(value=default_dir or os.path.expanduser("~"))
        self.var_name = tk.StringVar(value="downloaded_locations")
        self.fmt_vars = {k: tk.BooleanVar(value=k in ("csv", "kmz")) for k in GX.FORMATS}
        self.layer_vars = {k: tk.BooleanVar(value=True) for k in GX.LAYERS}

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=(8, 2))
        ttk.Label(top, text="Downloads and extractions (select the ones to export):").pack(side=tk.LEFT)
        ttk.Checkbutton(top, text="This session only", variable=self.var_session,
                        command=self._fill).pack(side=tk.RIGHT)
        lf = ttk.Frame(self)
        lf.pack(fill=tk.BOTH, expand=True, padx=8)
        self.tree = ttk.Treeview(lf, columns=("item", "n"), show="headings", selectmode="extended", height=10)
        self.tree.heading("item", text="Item")
        self.tree.heading("n", text="Features")
        self.tree.column("item", width=560, anchor="w")
        self.tree.column("n", width=70, anchor="e")
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        sel = ttk.Frame(self)
        sel.pack(fill=tk.X, padx=8, pady=2)
        ttk.Button(sel, text="Select all", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side=tk.LEFT)
        ttk.Button(sel, text="Remove from list", command=self._remove).pack(side=tk.LEFT, padx=4)

        lay = ttk.LabelFrame(self, text="Layers")
        lay.pack(fill=tk.X, padx=8, pady=4)
        for k, (key, label) in enumerate(GX.LAYERS.items()):
            ttk.Checkbutton(lay, text=label, variable=self.layer_vars[key]).grid(row=k // 3, column=k % 3, sticky="w", padx=4)
        fm = ttk.LabelFrame(self, text="Formats")
        fm.pack(fill=tk.X, padx=8, pady=4)
        for k, (key, label) in enumerate(GX.FORMATS.items()):
            ttk.Checkbutton(fm, text=label, variable=self.fmt_vars[key]).grid(row=k // 2, column=k % 2, sticky="w", padx=4)
        ttk.Button(fm, text="All formats", command=lambda: [v.set(True) for v in self.fmt_vars.values()]).grid(
            row=2, column=0, sticky="w", padx=4, pady=2)
        out = ttk.Frame(self)
        out.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(out, text="Folder:").grid(row=0, column=0, sticky="e")
        ttk.Entry(out, textvariable=self.var_dir, width=60).grid(row=0, column=1, sticky="we", padx=2)
        ttk.Button(out, text="Browse...", command=self._browse).grid(row=0, column=2)
        ttk.Label(out, text="File name:").grid(row=1, column=0, sticky="e")
        ttk.Entry(out, textvariable=self.var_name, width=30).grid(row=1, column=1, sticky="w", padx=2)
        out.grid_columnconfigure(1, weight=1)
        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=8, pady=(4, 8))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side=tk.RIGHT, padx=2)
        ttk.Button(btns, text="Export", command=self._export).pack(side=tk.RIGHT, padx=2)
        self._fill()
        _decorate(self)

    def _fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for gid, label, n in GX.registry.groups(this_session_only=self.var_session.get()):
            self.tree.insert("", tk.END, iid=gid, values=(label, n))
        self.tree.selection_set(self.tree.get_children())

    def _remove(self) -> None:
        sel = list(self.tree.selection())
        if sel and messagebox.askyesno("Export locations", f"Remove {len(sel)} item(s) from the list?\n"
                                       "(Downloaded files are not deleted.)", parent=self):
            GX.registry.remove_groups(sel)
            self._fill()

    def _browse(self) -> None:
        d = filedialog.askdirectory(parent=self, initialdir=self.var_dir.get() or os.path.expanduser("~"))
        if d:
            self.var_dir.set(d)

    def _export(self) -> None:
        groups = set(self.tree.selection())
        layers = {k for k, v in self.layer_vars.items() if v.get()}
        fmts = [k for k, v in self.fmt_vars.items() if v.get()]
        recs = [r for r in GX.registry.records(layers) if r.get("group") in groups]
        if not recs:
            messagebox.showinfo("Export locations", "Nothing selected to export.", parent=self)
            return
        try:
            files = GX.export_records(recs, self.var_dir.get().strip() or os.path.expanduser("~"),
                                      self.var_name.get().strip() or "downloaded_locations", fmts)
        except Exception as exc:
            messagebox.showerror("Export locations", str(exc), parent=self)
            return
        folder = os.path.dirname(files[0])
        if messagebox.askyesno("Export locations", f"{len(files)} file(s) written to\n{folder}\n\nOpen the folder?",
                               parent=self):
            _open_folder(folder)


def _open_folder(folder: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", folder])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass


def ask_keep_cache_tk(parent: Optional[tk.Misc] = None) -> None:
    """At program exit: keep the download list and cached data for the next start?"""
    if GX is None:
        return
    try:
        if not (GX.registry.count() or GX.session_cache_has_content()):
            return
        keep = messagebox.askyesno(
            "Online data cache",
            "Keep the cache for the next start?\n\n"
            f"{GX.session_cache_summary()}.\n\n"
            "Yes: the list of downloaded items (for 'Export locations') and cached values are kept.\n"
            "No: they are deleted now.\n\nYour downloaded data files are not affected.",
            parent=parent)
        if not keep:
            GX.clear_session_cache()
    except Exception:
        pass


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    dlg = ItrdbImportDialog(root, on_import=None)
    dlg.var_add.set(False)

    def close() -> None:
        dlg.on_close()
        ask_keep_cache_tk(root)
        root.destroy()

    dlg.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
