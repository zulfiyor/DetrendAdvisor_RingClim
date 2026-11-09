#!/usr/bin/env python3
"""
GUI-based detrending advisor for dendrochronological data (TRW / BI).
Features:
- Load ring-width-like data via GUI from:
    * CSV (years x series),
    * Tucson decadal format (.rwl, .tuc, .dec, .txt, etc.),
    * generic TXT (tries Tucson first then CSV-like),
    * JSON (long or wide),
    * Excel (.xls, .xlsx).
- Optionally load climate/target CSV.
- Work with multi-dataset projects: create, open, save, save as, merge, replace, and append datasets.
- Choose goal: highfreq / mixed / lowfreq / temp_recon / runoff_recon / drought_recon / variance_check.
- Run diagnostics and detrending advisor with dplR-backed methods, including exp, linear, mean,
  running mean, Hugershoff, LOWESS, spline, age-dependent spline, AR, RCS, signal-free RCS,
  and signal-free spline/age-dependent spline variants.
- Support optional POWT preprocessing and side-by-side POWT / no-POWT result handling.
- Group cores into trees automatically or manually with editable Series ID parsing rules.
- Show recommendation, runtime log, chronologies, RWI, EPS/Rbar diagnostics, signal diagnostics,
  POWT preview/results, compare views, and guide text in the GUI.
- Export chronologies, RWI matrices, reports, and Excel / CSV outputs from the GUI.
- Launch RingClim and prefill it with detrended outputs from the current project.
- Detect R / Rscript / dplR automatically, allow manual Rscript selection, and support optional
  online installation attempts on supported systems.
Inside code:
- All comments and docstrings are in English only.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys

# Shared modules (dendro_*.py) live in the "dendro_modules" folder next to this script.
# In the built program (PyInstaller) they are bundled, so the folder is not needed there.
_DENDRO_MODULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dendro_modules")
if os.path.isdir(_DENDRO_MODULES_DIR) and _DENDRO_MODULES_DIR not in sys.path:
    sys.path.insert(0, _DENDRO_MODULES_DIR)
import tempfile
import textwrap
import gzip
import pickle
import traceback
import warnings
import time
import threading
import queue
from pathlib import Path
# Force Tk backend for Matplotlib (avoids Qt hooks in PyInstaller)
os.environ.setdefault("MPLBACKEND", "TkAgg")
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Literal
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from scipy.interpolate import UnivariateSpline
from scipy.optimize import curve_fit, OptimizeWarning
from scipy.linalg import solveh_banded, toeplitz
from scipy.stats import rankdata, kendalltau
try:
    # Optional: used for LOWESS detrending
    from statsmodels.nonparametric.smoothers_lowess import lowess as sm_lowess
except Exception:  # pragma: no cover - optional dependency
    sm_lowess = None
from scipy.signal import butter, filtfilt
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
# Footer label text; can be overridden via environment variable
CREATED_BY_LABEL = os.getenv("CREATED_BY_LABEL", "Developed by")
matplotlib.use("TkAgg")
# Shared plot styles / correlation plots: dendro_plots.py (optional)
try:
    import dendro_plots as _dplots
except Exception:
    _dplots = None


def open_in_file_manager(path: str) -> None:
    """Open a folder (or show a file selected in its folder) in Explorer / Finder / the file manager."""
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
    except Exception as exc:
        messagebox.showerror("Open folder", f"Could not open:\n{path}\n\n{exc}")


def _add_plot_toolbar(canvas, master) -> None:
    """Zoom / pan / save toolbar under a Tk matplotlib canvas (packed before the canvas)."""
    try:
        tb = NavigationToolbar2Tk(canvas, master, pack_toolbar=False)
        tb.update()
        tb.pack(side=tk.BOTTOM, fill=tk.X)
    except Exception:
        pass
    if _uihelp is not None and hasattr(_uihelp, "mpl_wheel_zoom"):
        try:
            _uihelp.mpl_wheel_zoom(canvas)  # mouse wheel zooms around the cursor
        except Exception:
            pass


# Developer signature and hover help on every window: dendro_ui_help.py (optional)
try:
    import dendro_ui_help as _uihelp
except Exception:
    _uihelp = None

# Releases, automatic updates, guides and contact: app_update*.py (optional)
try:
    import app_update as _appupd
    import app_update_tk as _appupd_tk
except Exception:  # pragma: no cover
    _appupd = None
    _appupd_tk = None


# Pith (centre) information: dendro_pith.py
try:
    import dendro_pith as _pith
    _PITH_IMPORT_ERROR = ""
except Exception as _pith_exc:
    _pith = None
    _PITH_IMPORT_ERROR = f"{type(_pith_exc).__name__}: {_pith_exc}"
# Optional online import from ITRDB: dendro_online.py / dendro_online_tk.py
try:
    import dendro_online_tk as _online_tk
    _ONLINE_IMPORT_ERROR = ""
except Exception as _online_exc:  # keep Detrend Advisor usable without the module
    _online_tk = None
    _ONLINE_IMPORT_ERROR = f"{type(_online_exc).__name__}: {_online_exc}"
GoalType = Literal[
    "highfreq",
    "mixed",
    "lowfreq",
    "temp_recon",
    "runoff_recon",
    "drought_recon",
    "variance_check",
]

class _HoverToolTip:
    """Small hover tooltip helper for Tk/ttk widgets."""

    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = str(text or "")
        self.tipwindow: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        self.widget.bind("<Enter>", self._schedule, add="+")
        self.widget.bind("<Leave>", self._hide, add="+")
        self.widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(450, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._cancel()
        if self.tipwindow is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except Exception:
            pass
        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#fffdd0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
            wraplength=320,
        )
        label.pack()
        tw.wm_geometry(f"+{x}+{y}")
        self.tipwindow = tw

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self.tipwindow is not None:
            try:
                self.tipwindow.destroy()
            except Exception:
                pass
            self.tipwindow = None


def _attach_hover_tooltip(widget: tk.Widget, text: str) -> None:
    """Attach a hover tooltip to a Tk/ttk widget once."""
    tip_text = str(text or "").strip()
    if not tip_text:
        return
    if _uihelp is not None and getattr(widget, "_dendro_tip", None) is not None:
        widget._dendro_tip.text = tip_text  # already has a shared tooltip: update its text
        setattr(widget, "_hover_tooltip_text", tip_text)
        return
    if getattr(widget, "_hover_tooltip_text", None) == tip_text:
        return
    setattr(widget, "_hover_tooltip_text", tip_text)
    setattr(widget, "_hover_tooltip", _HoverToolTip(widget, tip_text))
 

# ---------------------------------------------------------------------------
# Compatibility options (dplR vs legacy Python)
# ---------------------------------------------------------------------------
CompatMode = Literal["dplR", "legacy"]
ConstrainNLS = Literal["never", "when.fail", "always"]
PowtMethod = Literal["universal", "cook"]

@dataclass
class DplrCompatConfig:
    """Compatibility / reproducibility options.

    Notes:
        - compat="dplR" will try to use Rscript + dplR as the computation backend
          for all methods. For methods that are not implemented by dplR directly,
          the script uses base R implementations inside the same R harness.
          If R is unavailable, the script falls back to legacy Python behavior
          unless strict_dplr=True.
        - recode_zeros emulates the dplR convention of replacing zeros by 0.001
          prior to detrending (useful for BI / density-like series and some TRW files).
        - difference emulates detrend.series(..., difference=TRUE): indices are
          computed as y - fitted instead of y / fitted.
        - pos_slope and constrain_nls follow dplR argument names/meaning.
        - auto_install_r_deps optionally installs dplR from CRAN inside the R harness.
          This requires an internet connection and write access to the R library path.

    """
    compat: CompatMode = "dplR"
    recode_zeros: bool = True
    difference: bool = False
    pos_slope: bool = False
    constrain_nls: ConstrainNLS = "never"
    rscript: str = "Rscript"
    strict_dplr: bool = False
    auto_install_r: bool = False
    auto_install_r_deps: bool = False
    cran_repo: str = "https://cloud.r-project.org"
    powt_enabled: bool = True
    powt_method: PowtMethod = "universal"
    powt_rescale: bool = True
    # Set for runs on already power-transformed data: indices are computed as differences
    # (Cook & Peters 1997) and expressed as 1 + difference / series mean (dimensionless, mean 1).
    scale_difference: bool = False

def _cfg_for_powt_transformed(cfg: "DplrCompatConfig") -> "DplrCompatConfig":
    """Configuration for detrending data that were already power-transformed by dplR::powt.

    The data must not be transformed a second time, and power-transformed series (which
    may contain values <= 0 after rescaling) are detrended by differences instead of ratios.
    """
    out = DplrCompatConfig(**vars(cfg))
    out.powt_enabled = False
    out.difference = True
    out.scale_difference = True
    return out


def _default_compat_config() -> DplrCompatConfig:
    env = (os.getenv("DETREND_COMPAT", "dplR") or "dplR").strip().lower()
    compat: CompatMode = "legacy" if env in ("legacy", "python") else "dplR"
    auto_install_r = os.getenv("DETREND_AUTO_INSTALL_R", "0").strip() in ("1", "true", "yes", "y")
    rscript = (os.getenv("RSCRIPT", "Rscript") or "Rscript").strip()
    strict_env = (os.getenv("DETREND_STRICT_DPLR", "0") or "0").strip().lower()
    auto_env = (os.getenv("DETREND_AUTO_INSTALL_DPLR", "0") or "0").strip().lower()
    strict_dplr = strict_env in ("1", "true", "yes", "y")
    auto_install_r_deps = auto_env in ("1", "true", "yes", "y")
    cran_repo = (os.getenv("DETREND_CRAN_REPO", "https://cloud.r-project.org") or "https://cloud.r-project.org").strip()
    return DplrCompatConfig(
        compat=compat,
        rscript=rscript,
        strict_dplr=strict_dplr,
        auto_install_r=auto_install_r,
        auto_install_r_deps=auto_install_r_deps,
        cran_repo=cran_repo,
    )

DEFAULT_COMPAT_CONFIG = _default_compat_config()

_MANUAL_RSCRIPT_PATH_CACHE: str = ""


def _app_icon_search_dirs() -> list[str]:
    """Return folders where the application icon files may be located."""
    dirs: list[str] = []
    for base in (
        getattr(sys, "_MEIPASS", None),
        os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
        os.path.dirname(os.path.abspath(__file__)),
    ):
        if not base:
            continue
        for sub in ("", "icons", os.path.join("build", "icons")):
            cand = os.path.join(base, sub) if sub else base
            if cand not in dirs:
                dirs.append(cand)
    return dirs


def _find_app_icon_files(app_name: str) -> tuple[Optional[str], Optional[str]]:
    """Return (ico_path, png_path) for the application icon, if present."""
    ico_path: Optional[str] = None
    png_path: Optional[str] = None
    for folder in _app_icon_search_dirs():
        if ico_path is None and os.path.isfile(os.path.join(folder, f"{app_name}.ico")):
            ico_path = os.path.join(folder, f"{app_name}.ico")
        if png_path is None and os.path.isfile(os.path.join(folder, f"{app_name}.png")):
            png_path = os.path.join(folder, f"{app_name}.png")
    return ico_path, png_path


def _set_windows_app_user_model_id(app_id: str) -> None:
    """Give the process its own taskbar identity so Windows shows the app icon."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
    except Exception:
        pass


def _hidden_subprocess_kwargs(gui: bool = False) -> Dict[str, Any]:
    """Return subprocess keyword arguments that keep console windows hidden.

    Notes:
        - On Windows, console tools (Rscript, where, winget, choco, pip) are started
          with CREATE_NO_WINDOW and a hidden STARTUPINFO, so no black console window
          appears on top of other windows. Child processes (e.g. R parallel workers)
          inherit the windowless console instead of opening new visible consoles.
        - For GUI launches (gui=True) only the console window is suppressed; the
          application's own windows stay visible.
        - stdin is always redirected to DEVNULL so windowed/frozen builds never pass
          an invalid stdin handle to child processes.
    """
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if os.name != "nt":
        return kwargs
    kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    if not gui:
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001))
            si.wShowWindow = 0
            kwargs["startupinfo"] = si
        except Exception:
            pass
    return kwargs


_CTRL_SHORTCUT_LATIN = {
    "c": "copy",
    "insert": "copy",
    "v": "paste",
    "x": "cut",
    "a": "select_all",
}
_CTRL_SHORTCUT_NON_LATIN = {
    "cyrillic_es": "copy",
    "\u0441": "copy",
    "cyrillic_em": "paste",
    "\u043c": "paste",
    "cyrillic_che": "cut",
    "\u0447": "cut",
    "cyrillic_ef": "select_all",
    "\u0444": "select_all",
}
_CTRL_SHORTCUT_WIN_KEYCODES = {67: "copy", 86: "paste", 88: "cut", 65: "select_all"}
_CTRL_SHORTCUT_X11_KEYCODES = {54: "copy", 55: "paste", 53: "cut", 38: "select_all"}
_READONLY_NAV_KEYSYMS = {
    "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Meta_L", "Meta_R", "Super_L", "Super_R", "Win_L", "Win_R",
    "Caps_Lock", "Num_Lock", "Escape", "App", "Menu",
}


def _classify_ctrl_shortcut(event) -> str:
    """Map a Ctrl/Command key event to copy/paste/cut/select_all independent of layout."""
    keysym = str(getattr(event, "keysym", "") or "").lower()
    if keysym in _CTRL_SHORTCUT_LATIN:
        return _CTRL_SHORTCUT_LATIN[keysym]
    if keysym in _CTRL_SHORTCUT_NON_LATIN:
        return _CTRL_SHORTCUT_NON_LATIN[keysym]
    try:
        keycode = int(getattr(event, "keycode", 0) or 0)
    except Exception:
        keycode = 0
    if os.name == "nt":
        return _CTRL_SHORTCUT_WIN_KEYCODES.get(keycode, "")
    if sys.platform.startswith("linux"):
        return _CTRL_SHORTCUT_X11_KEYCODES.get(keycode, "")
    return ""


def _rscript_available(rscript: str) -> bool:
    try:
        return shutil.which(rscript) is not None
    except Exception:
        return False


def _candidate_rscript_paths(rscript_name: str) -> list[str]:
    """Return common fallback locations for Rscript on macOS, Linux, and conda."""
    try:
        home = os.path.expanduser("~")
    except Exception:
        home = ""

    base_name = os.path.basename(str(rscript_name or "Rscript")) or "Rscript"
    cands: list[str] = []

    def _add(path: Optional[str]) -> None:
        if not path:
            return
        norm = os.path.abspath(path)
        if norm not in cands:
            cands.append(norm)

    r_home = os.getenv("R_HOME", "").strip()
    if r_home:
        _add(os.path.join(r_home, "bin", base_name))
        _add(os.path.join(r_home, "Resources", "bin", base_name))

    conda_prefix = os.getenv("CONDA_PREFIX", "").strip()
    if conda_prefix:
        _add(os.path.join(conda_prefix, "bin", base_name))

    common_prefixes = [
        os.path.join(home, "anaconda3"),
        os.path.join(home, "miniconda3"),
        os.path.join(home, "mambaforge"),
        os.path.join(home, "miniforge3"),
        "/opt/anaconda3",
        "/opt/miniconda3",
        "/opt/mambaforge",
        "/opt/miniforge3",
        "/opt/homebrew",
        "/usr/local",
        "/Library/Frameworks/R.framework/Resources",
        "/Library/Frameworks/R.framework/Versions/Current/Resources",
    ]

    for prefix in common_prefixes:
        _add(os.path.join(prefix, "bin", base_name))

    return cands



def _resolve_rscript_path(rscript: str = "Rscript") -> Optional[str]:
    """Resolve an Rscript executable path.

    Notes:
      - If `rscript` is an absolute/relative file path, it must exist.
      - If not found on PATH, Windows fallback probes common R install locations.
    """
    try:
        import shutil
        from pathlib import Path as _Path

        # Direct path
        if os.path.sep in rscript or (os.path.altsep and os.path.altsep in rscript):
            if os.path.isfile(rscript):
                return rscript
            return None

        # PATH
        found = shutil.which(rscript)
        if found:
            return found

        # macOS / Linux / conda fallback locations
        for cand in _candidate_rscript_paths(rscript):
            if os.path.isfile(cand):
                return cand

        # Windows common install locations
        if os.name == "nt":
            cands: list[str] = []

            r_home = os.getenv("R_HOME", "").strip()
            if r_home:
                p1 = os.path.join(r_home, "bin", "Rscript.exe")
                p2 = os.path.join(r_home, "bin", "x64", "Rscript.exe")
                if os.path.isfile(p1):
                    cands.append(p1)
                if os.path.isfile(p2):
                    cands.append(p2)

            for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
                base = os.getenv(env_key, "").strip()
                if not base:
                    continue
                r_root = _Path(base) / "R"
                if not r_root.exists():
                    continue
                cands.extend([str(p) for p in r_root.glob("R-*/bin/Rscript.exe")])
                cands.extend([str(p) for p in r_root.glob("R-*/bin/x64/Rscript.exe")])

            if cands:
                # Best-effort: pick the last in sorted order (usually highest version folder).
                return sorted(set(cands))[-1]

        return None
    except Exception:
        return None


def _validate_rscript_file(path: str) -> Optional[str]:
    """Validate one manually selected Rscript path."""
    try:
        cand = os.path.abspath(str(path or "").strip())
    except Exception:
        return None
    if not cand:
        return None
    if not os.path.isfile(cand):
        return None
    name = os.path.basename(cand).lower()
    if name not in ("rscript", "rscript.exe"):
        return None
    return cand


def _ask_user_for_rscript_path(parent: Optional[tk.Misc] = None) -> Optional[str]:
    """Ask the user to select the Rscript executable manually."""
    global _MANUAL_RSCRIPT_PATH_CACHE

    initial_path = _validate_rscript_file(_MANUAL_RSCRIPT_PATH_CACHE)
    initial_dir = ""
    if initial_path:
        initial_dir = os.path.dirname(initial_path)
    else:
        resolved = _resolve_rscript_path("Rscript")
        if resolved:
            initial_dir = os.path.dirname(str(resolved))

    filetypes = [
        ("Rscript executable", "Rscript.exe" if os.name == "nt" else "Rscript"),
        ("All files", "*.*" if os.name == "nt" else "*"),
    ]
    selected = filedialog.askopenfilename(
        parent=parent,
        title="Select Rscript executable",
        initialdir=initial_dir or None,
        filetypes=filetypes,
    )
    valid = _validate_rscript_file(selected)
    if valid:
        _MANUAL_RSCRIPT_PATH_CACHE = str(valid)
        return str(valid)
    return None


def _iter_known_rscript_candidates(rscript: str = "Rscript") -> list[str]:
    """Return unique existing Rscript candidates in priority order."""
    cands: list[str] = []

    def _add(path: Optional[str]) -> None:
        if not path:
            return
        try:
            norm = os.path.abspath(str(path))
        except Exception:
            return
        if not os.path.isfile(norm):
            return
        if norm not in cands:
            cands.append(norm)

    _add(_validate_rscript_file(_MANUAL_RSCRIPT_PATH_CACHE))
    _add(_resolve_rscript_path(rscript))

    try:
        probe = subprocess.run(
            ["where" if os.name == "nt" else "which", "Rscript"],
            capture_output=True,
            text=True,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        for line in str(probe.stdout or "").splitlines():
            _add(line.strip())
    except Exception:
        pass

    for cand in _candidate_rscript_paths(rscript):
        _add(cand)

    if os.name == "nt":
        for env_key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            base = os.getenv(env_key, "").strip()
            if not base:
                continue
            for sub in (
                os.path.join("Programs", "R", "R-*", "bin", "Rscript.exe"),
                os.path.join("Programs", "R", "R-*", "bin", "x64", "Rscript.exe"),
                os.path.join("R", "R-*", "bin", "Rscript.exe"),
                os.path.join("R", "R-*", "bin", "x64", "Rscript.exe"),
            ):
                try:
                    from pathlib import Path as _Path
                    for p in _Path(base).glob(sub):
                        _add(str(p))
                except Exception:
                    pass

    return cands


def _r_package_available_with_rscript(rscript_cmd: str, package_name: str) -> bool:
    """Return True when a specific Rscript can load one package."""
    try:
        cmd = [
            str(rscript_cmd),
            "-e",
            (
                "quit(save='no', status="
                f"if (requireNamespace('{str(package_name)}', quietly=TRUE)) 0 else 11)"
            ),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        return int(proc.returncode) == 0
    except Exception:
        return False


def _find_rscript_with_package(config: DplrCompatConfig, package_name: str) -> Optional[str]:
    """Find an installed Rscript that can load the requested R package."""
    for rscript_cmd in _iter_known_rscript_candidates(config.rscript):
        if _r_package_available_with_rscript(rscript_cmd, package_name):
            return str(rscript_cmd)
    return None

def _try_install_r_windows_online() -> tuple[bool, str]:
    """Attempt to install R on Windows using online package managers.

    Notes:
        - This function is online-only.
        - It first tries winget, then falls back to chocolatey.
        - It returns both success state and captured console output.
    """
    try:
        import shutil

        logs: list[str] = []

        if shutil.which("winget"):
            cmd = [
                "winget", "install",
                "--id", "RProject.R",
                "--source", "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, **_hidden_subprocess_kwargs())
            out = "\n".join(
                part for part in (
                    "Command: " + " ".join(cmd),
                    (proc.stdout or "").strip(),
                    (proc.stderr or "").strip(),
                ) if part
            )
            logs.append(out)
            if proc.returncode == 0:
                return True, "\n\n".join(logs)
            out_lc = out.lower()
            if (
                "found an existing package already installed" in out_lc
                or "no available upgrade found" in out_lc
            ):
                return True, "\n\n".join(logs)

        if shutil.which("choco"):
            cmd = ["choco", "install", "r.project", "-y"]
            proc = subprocess.run(cmd, capture_output=True, text=True, **_hidden_subprocess_kwargs())
            out = "\n".join(
                part for part in (
                    "Command: " + " ".join(cmd),
                    (proc.stdout or "").strip(),
                    (proc.stderr or "").strip(),
                ) if part
            )
            logs.append(out)
            if proc.returncode == 0:
                return True, "\n\n".join(logs)

        if not logs:
            logs.append(
                "No supported online installer was found. Neither 'winget' nor 'choco' is available."
            )
        return False, "\n\n".join(logs)
    except Exception as exc:
        return False, str(exc)
 

def _ensure_rscript(config: DplrCompatConfig) -> str:
    """Ensure Rscript is available; optionally install R online on Windows."""
    resolved = _resolve_rscript_path(config.rscript)
    if resolved:
        config.rscript = resolved
        return resolved

    manual_cached = _validate_rscript_file(_MANUAL_RSCRIPT_PATH_CACHE)
    if manual_cached:
        config.rscript = str(manual_cached)
        return str(manual_cached)

    if config.auto_install_r and os.name == "nt":
        ok, _install_log = _try_install_r_windows_online()
        if ok:
            resolved2 = _resolve_rscript_path(config.rscript)
            if resolved2:
                config.rscript = resolved2
                return resolved2

    raise RuntimeError(
        "Rscript not found. Install R (R runtime) so 'Rscript' is available, "
        "or pass an explicit --rscript path. In this build, automatic installation "
        "uses online package managers on Windows (winget/choco)."

    )


def _dplr_package_available(config: DplrCompatConfig) -> bool:
    """Return True if the installed R runtime can load dplR."""
    try:
        rscript_cmd = _find_rscript_with_package(config, "dplR")
        if rscript_cmd:
            config.rscript = str(rscript_cmd)
            global _MANUAL_RSCRIPT_PATH_CACHE
            _MANUAL_RSCRIPT_PATH_CACHE = str(rscript_cmd)
            return True
        return False
    except Exception:
        return False


def _try_install_dplr_package(config: DplrCompatConfig) -> tuple[bool, str]:
    """Attempt to install dplR into the active R library path."""
    try:
        rscript_cmd = _ensure_rscript(config)
    except Exception as exc:
        return False, str(exc)
    install_expr = (
        f"install.packages('dplR', repos={json.dumps(str(config.cran_repo))}); "
        "quit(save='no', status=if (requireNamespace('dplR', quietly=TRUE)) 0 else 12)"
    )
    cmd = [rscript_cmd, "-e", install_expr]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        output = "\n".join(
            part for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if part
        )
        return int(proc.returncode) == 0, output
    except Exception as exc:
        return False, str(exc)


def _compat_signature(config: DplrCompatConfig) -> str:
    """Build a stable signature for backend-sensitive caches/results."""
    return "|".join(
        [
            str(config.compat),
            f"recode_zeros={int(bool(config.recode_zeros))}",
            f"difference={int(bool(config.difference))}",
            f"pos_slope={int(bool(config.pos_slope))}",
            f"constrain_nls={config.constrain_nls}",
            f"strict_dplr={int(bool(config.strict_dplr))}",
            f"auto_install_r={int(bool(config.auto_install_r))}",
            f"auto_install_r_deps={int(bool(config.auto_install_r_deps))}",
            f"cran_repo={config.cran_repo}",
            f"powt_enabled={int(bool(config.powt_enabled))}",
            f"powt_method={config.powt_method}",
            f"powt_rescale={int(bool(config.powt_rescale))}",
        ]
    )


@dataclass
class PowtPreviewResult:
    """Cached POWT preview produced by dplR."""
    transformed: pd.DataFrame
    power: pd.Series
    raw_skew: pd.Series
    powt_skew: pd.Series
    method: str
    rescale: bool


def _series_skewness(df: pd.DataFrame) -> pd.Series:
    """Return per-series skewness with a stable numeric fallback."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    out: Dict[str, float] = {}
    for col in df.columns:
        vals = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
        vals = vals[np.isfinite(vals)]
        if vals.size < 3:
            out[str(col)] = np.nan
            continue
        try:
            out[str(col)] = float(pd.Series(vals).skew(skipna=True))
        except Exception:
            out[str(col)] = np.nan
    return pd.Series(out, dtype=float)


def _print_progress(message: str) -> None:
    """Print a progress message immediately."""
    try:
        print(str(message), file=sys.stderr, flush=True)
    except Exception:
        pass


_RUNTIME_PROGRESS_CALLBACK = None

def _set_runtime_progress_callback(callback) -> None:
    """Register a temporary callback for live runtime progress lines."""
    global _RUNTIME_PROGRESS_CALLBACK
    _RUNTIME_PROGRESS_CALLBACK = callback


def _emit_runtime_progress(message: str) -> None:
    """Emit one runtime progress line to the registered callback, if any."""
    cb = _RUNTIME_PROGRESS_CALLBACK
    if cb is None:
        return
    try:
        cb(str(message))
    except Exception:
        pass


def _app_log_dir() -> str:
    """Return a writable directory for runtime and crash logs."""
    try:
        if sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Logs")
        elif os.name == "nt":
            base = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        else:
            base = os.path.join(os.path.expanduser("~"), ".cache")
    except Exception:
        base = os.getcwd()
    log_dir = os.path.join(base, "detrend_advisor")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _new_runtime_log_path(prefix: str) -> str:
    """Create a timestamped log file path."""
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(_app_log_dir(), f"{prefix}_{stamp}.log")


def _write_exception_log(
    context: str,
    exc_info: Optional[tuple[type[BaseException], BaseException, Any]] = None,
) -> str:
    """Write a startup/runtime exception log and return its path."""
    path = _new_runtime_log_path(context)
    if exc_info is None:
        exc_text = traceback.format_exc()
    else:
        exc_text = "".join(traceback.format_exception(*exc_info))
    lines = [
        f"context: {context}",
        f"timestamp: {_dt.datetime.now().isoformat()}",
        f"python: {sys.executable}",
        f"argv: {sys.argv!r}",
        f"cwd: {os.getcwd()}",
        f"frozen: {bool(getattr(sys, 'frozen', False))}",
        "",
        exc_text.rstrip(),
        "",
    ]
    try:
        Path(path).write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        fallback = os.path.join(os.getcwd(), os.path.basename(path))
        Path(fallback).write_text("\n".join(lines), encoding="utf-8")
        path = fallback
    return path


def _run_subprocess_capture(
    args: list[str],
    verbose: bool = False,
    prefix: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with optional live progress streaming."""
    live_stream = bool(verbose) or (_RUNTIME_PROGRESS_CALLBACK is not None)
    if not live_stream:
        return subprocess.run(args, capture_output=True, text=True, **_hidden_subprocess_kwargs())

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_hidden_subprocess_kwargs(),
    )
    lines: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            lines.append(line)
            msg = line.rstrip()
            if msg:
                full_msg = f"{prefix}{msg}"
                if verbose:
                    _print_progress(full_msg)
                _emit_runtime_progress(full_msg)
    rc = proc.wait()
    return subprocess.CompletedProcess(args=args, returncode=rc, stdout="".join(lines), stderr="")


def _build_clean_qt_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build a sanitized environment for launching PySide6 / Qt subprocesses."""
    env = dict(base_env or os.environ)

    # Remove only Qt/plugin overrides that commonly break runtime resolution.
    # Keep the active conda environment intact so installed packages remain visible.
    for key in (
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
    ):
        env.pop(key, None)

    raw_path = str(env.get("PATH", "") or "")
    path_entries = [p for p in raw_path.split(os.pathsep) if p]
    clean_entries: list[str] = []
    for entry in path_entries:
        if entry not in clean_entries:
            clean_entries.append(entry)

    if os.name == "nt":
        system_entries = [
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
            os.environ.get("SystemRoot", r"C:\Windows"),
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Wbem"),
        ]
        for entry in reversed(system_entries):
            if entry and entry not in clean_entries:
                clean_entries.insert(0, entry)

    env["PATH"] = os.pathsep.join(clean_entries)
    return env


def _probe_ringclim_qt_runtime(python_exe: str) -> tuple[bool, str]:
    """Probe PySide6 / shiboken6 / QtWidgets import in a clean subprocess."""
    probe_code = (
        "import PySide6, shiboken6\n"
        "from PySide6.QtWidgets import QApplication\n"
        "print('OK')\n"
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", probe_code],
            capture_output=True,
            text=True,
            env=_build_clean_qt_env(),
            **_hidden_subprocess_kwargs(),
        )
    except Exception as exc:
        return False, str(exc)

    if int(proc.returncode) == 0:
        return True, ""

    detail = "\n".join(
        part for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if part
    ).strip()
    return False, detail or f"Probe exited with code {proc.returncode}"

def _recode_zeros_df(df: pd. DataFrame, enabled: bool) -> pd. DataFrame:
    if not enabled:
        return df
    out = df.copy().astype(float)
    out[out == 0.0] = 0.001
    return out

_VALIDATE_DPLR_R = textwrap.dedent(r"""

    args <- commandArgs(trailingOnly=TRUE)
    if (length(args) < 3) {
    stop("Usage: Rscript validate_dplr.R <input> <po.csv|NA> <out_dir> [method_keys_csv] [key=value ...]")
    }
    infile <- args[[1]]
    po_file <- args[[2]]
    out_dir <- args[[3]]

    method_keys_csv <- ""
    if (length(args) >= 4) {
      method_keys_csv <- args[[4]]
    }

    # Parse optional key=value args
    opts <- list()
    if (length(args) >= 5) {
      for (i in 5:length(args)) {
        kv <- strsplit(args[[i]], "=", fixed=TRUE)[[1]]
        if (length(kv) == 2) {
          opts[[kv[[1]]]] <- kv[[2]]
        }
      }
    }

    get_opt <- function(name, default) {
      if (!is.null(opts[[name]]) && nzchar(opts[[name]])) return(opts[[name]])
      return(default)
    }

    as_bool <- function(x) {
      x <- tolower(trimws(as.character(x)))
      if (x %in% c("true","t","1","yes","y")) return(TRUE)
      if (x %in% c("false","f","0","no","n")) return(FALSE)
      return(FALSE)
    }

    pos_slope <- as_bool(get_opt("pos_slope", "FALSE"))
    difference <- as_bool(get_opt("difference", "FALSE"))
    recode_zeros <- as_bool(get_opt("recode_zeros", "TRUE"))
    constrain_nls <- tolower(trimws(get_opt("constrain_nls", "never")))
    if (!(constrain_nls %in% c("never","when.fail","always"))) {
      stop("constrain_nls must be one of: never, when.fail, always")
    }

    auto_install <- as_bool(get_opt("auto_install", "FALSE"))
    cran_repo <- get_opt("cran_repo", "https://cloud.r-project.org")
    window_length_opt <- suppressWarnings(as.integer(get_opt("window_length", "")))
    window_overlap_opt <- suppressWarnings(as.integer(get_opt("window_overlap", "")))
    min_corr_overlap_opt <- suppressWarnings(as.integer(get_opt("min_corr_overlap", "")))
    parallel_workers_opt <- suppressWarnings(as.integer(get_opt("parallel_workers", "")))
    powt_enabled <- as_bool(get_opt("powt_enabled", "FALSE"))
    powt_method <- tolower(trimws(get_opt("powt_method", "universal")))
    powt_rescale <- as_bool(get_opt("powt_rescale", "FALSE"))
    if (!(powt_method %in% c("universal", "cook"))) {
      stop("powt_method must be one of: universal, cook")
    }
    if (!requireNamespace("dplR", quietly=TRUE)) {
      if (auto_install) {
        install.packages("dplR", repos=cran_repo)
      } else {
        stop("Package 'dplR' is not installed. Install it in R, or run with auto_install=TRUE.")
      }
    }
    suppressPackageStartupMessages(library(dplR))

    dir.create(out_dir, showWarnings=FALSE, recursive=TRUE)
    writeLines(as.character(packageVersion("dplR")), file.path(out_dir, "dplr_version.txt"))

    # Read input
    ext <- tolower(tools::file_ext(infile))
    rwl <- NULL
    if (ext %in% c("rwl","tuc","dec","txt")) {
      rwl <- read.rwl(infile)
    } else if (ext %in% c("csv")) {
      df <- read.csv(infile, check.names=FALSE, stringsAsFactors=FALSE)
      if ("Year" %in% names(df)) {
        rn <- df$Year
        df$Year <- NULL
        rownames(df) <- rn
      }
      # numeric coercion
      for (j in 1:ncol(df)) {
        df[[j]] <- as.numeric(df[[j]])
      }
      rwl <- as.data.frame(df)
    } else {
      stop(paste0("Unsupported input extension: ", ext))
    }

    # Read pith-offset table (optional)
    po <- NULL
    if (!is.null(po_file) && nzchar(po_file) && po_file != "NA" && file.exists(po_file)) {
      po <- read.csv(po_file, check.names=FALSE, stringsAsFactors=FALSE)
    }

# dplR will error if there are NA values between the first and last non-NA
# observation within a series. In dendro workflows, internal gaps are typically
# encoded as 0 ("missing ring") rather than NA; we convert internal NA -> 0 so
# that recode_zeros can map it to 0.001 (dplR behavior).
fill_internal_na_as_zero <- function(rwl_df) {
  for (j in seq_len(ncol(rwl_df))) {
    x <- rwl_df[[j]]
    idx <- which(!is.na(x))
    if (length(idx) == 0) next
    lo <- min(idx); hi <- max(idx)
    if (hi > lo) {
      span <- x[lo:hi]
      span[is.na(span)] <- 0
      x[lo:hi] <- span
      rwl_df[[j]] <- x
    }
  }
  rwl_df
}
rwl <- fill_internal_na_as_zero(rwl)

    # Apply dplR-style zero recoding consistently for all methods when enabled.
    if (recode_zeros) {
      rwl[rwl == 0] <- 0.001
    }

    # Optional dplR power transformation before detrending.
    if (powt_enabled) {
      message(
        paste0(
          "dplR powt preprocessing: method=",
          powt_method,
          ", rescale=",
          ifelse(powt_rescale, "TRUE", "FALSE")
        )
      )
      rwl <- as.data.frame(
        powt(
          rwl,
          method=powt_method,
          rescale=powt_rescale,
          return.power=FALSE
        )
      )
    }

    # Default method set if none provided
    default_keys <- c(
      "exp","linear","runmean_32","mean","hugershoff","lowess",
      "spline_30","spline_60","spline_120",
      "age_dep_spline","ar","rcs","rcs_signal_free",
      "ssf_spline_30","ssf_spline_60","ssf_spline_120","ssf_age_dep_spline"
    )
    keys <- default_keys
    if (!is.null(method_keys_csv) && nzchar(method_keys_csv)) {
      keys <- strsplit(method_keys_csv, ",", fixed=TRUE)[[1]]
      keys <- trimws(keys)
      keys <- keys[nzchar(keys)]
    }

    # Method specs
    specs <- list(
      exp = list(kind="detrend", method="ModNegExp"),
      linear = list(kind="custom_linear"),
      runmean_32 = list(kind="custom_runmean", window=32, min_periods=3),
      mean = list(kind="detrend", method="Mean"),
      hugershoff = list(kind="detrend", method="ModHugershoff"),
      lowess = list(kind="detrend", method="Friedman"),
      spline_30 = list(kind="detrend", method="Spline", nyrs=30, f=0.5),
      spline_60 = list(kind="detrend", method="Spline", nyrs=60, f=0.5),
      spline_120 = list(kind="detrend", method="Spline", nyrs=120, f=0.5),
      age_dep_spline = list(kind="detrend", method="AgeDepSpline"),
      ar = list(kind="detrend", method="Ar"),
      rcs = list(kind="rcs"),
      rcs_signal_free = list(kind="custom_rcs_signal_free", max_iter=6),
      ssf_spline_30 = list(kind="ssf", method="Spline", nyrs=30, f=0.5, max_iterations=60, mad_threshold=1e-3),
      ssf_spline_60 = list(kind="ssf", method="Spline", nyrs=60, f=0.5, max_iterations=60, mad_threshold=1e-3),
      ssf_spline_120 = list(kind="ssf", method="Spline", nyrs=120, f=0.5, max_iterations=60, mad_threshold=1e-3),
      ssf_age_dep_spline = list(kind="ssf", method="AgeDepSpline", max_iterations=60, mad_threshold=1e-3)
    )

    # Parameterized variants use the same algorithms as the built-in presets.
    for (key in keys) {
      if (!is.null(specs[[key]])) next
      if (grepl("^(spline|ssf_spline|runmean)_[0-9]+$", key)) {
        period <- as.numeric(sub("^.*_", "", key))
        if (!is.finite(period) || period < 3 || period > 10000) {
          stop(paste("Invalid method period:", key))
        }
        if (startsWith(key, "ssf_spline_")) {
          specs[[key]] <- list(kind="ssf", method="Spline", nyrs=period, f=0.5,
                              max_iterations=60, mad_threshold=1e-3)
        } else if (startsWith(key, "spline_")) {
          specs[[key]] <- list(kind="detrend", method="Spline", nyrs=period, f=0.5)
        } else {
          specs[[key]] <- list(kind="custom_runmean", window=period, min_periods=3)
        }
      }
    }

    # Helper: ensure a po table exists for rcs() if user didn't provide one.
    ensure_po <- function(rwl, po) {
      if (!is.null(po) && nrow(po) > 0) return(po)
      # Create a minimal po with offset=1 for all series.
      po2 <- data.frame(series=colnames(rwl), offset=rep(1, ncol(rwl)), stringsAsFactors=FALSE)
      return(po2)
    }

    subset_po_table <- function(po_use, series_names) {
      if (is.null(po_use) || nrow(po_use) == 0) return(NULL)
      series_col <- if ("series" %in% names(po_use)) "series" else names(po_use)[1]
      keep <- as.character(po_use[[series_col]]) %in% as.character(series_names)
      po_sub <- po_use[keep, , drop=FALSE]
      if (nrow(po_sub) == 0) return(NULL)
      return(po_sub)
    }

    empty_exclusion_log <- function() {
      data.frame(series=character(0), reason=character(0), stringsAsFactors=FALSE)
    }

    add_exclusion_entry <- function(log_df, series_name, reason_text) {
      rbind(
        log_df,
        data.frame(
          series=as.character(series_name),
          reason=as.character(reason_text),
          stringsAsFactors=FALSE
        )
      )
    }

    write_exclusion_log <- function(log_df, path) {
      if (is.null(log_df) || nrow(log_df) == 0) return(invisible(NULL))
      write.table(
        log_df,
        file=path,
        sep="\t",
        row.names=FALSE,
        col.names=TRUE,
        quote=TRUE
      )
      invisible(NULL)
    }

    resolve_parallel_workers <- function(n_tasks) {
      n_tasks <- as.integer(max(1L, n_tasks))
      if (!is.na(parallel_workers_opt) && parallel_workers_opt > 0L) {
        return(as.integer(max(1L, min(n_tasks, parallel_workers_opt))))
      }
      n_detect <- suppressWarnings(parallel::detectCores(logical=TRUE))
      if (!is.finite(n_detect) || is.na(n_detect)) n_detect <- 1L
      reserve <- if (n_detect >= 6L) 2L else 1L
      by_headroom <- max(1L, as.integer(n_detect) - reserve)
      by_fraction <- max(1L, floor(as.integer(n_detect) / 2L))
      default_cap <- 4L
      return(as.integer(max(1L, min(n_tasks, by_headroom, by_fraction, default_cap))))
    }

    finite_count_series <- function(x) {
      sum(is.finite(as.numeric(x)))
    }

    positive_count_series <- function(x) {
      sum(is.finite(as.numeric(x)) & as.numeric(x) > 0)
    }

    sd_series <- function(x) {
      vals <- as.numeric(x)
      vals <- vals[is.finite(vals)]
      if (length(vals) < 2) return(NA_real_)
      stats::sd(vals)
    }

    prefilter_heavy_method_series <- function(full_rwl, min_obs=3L, min_positive=2L) {
      keep <- rep(TRUE, ncol(full_rwl))
      excluded <- empty_exclusion_log()
      if (ncol(full_rwl) == 0) {
        return(list(rwl=full_rwl, excluded=excluded))
      }
      for (j in seq_len(ncol(full_rwl))) {
        s <- full_rwl[[j]]
        n_finite <- finite_count_series(s)
        n_pos <- positive_count_series(s)
        s_sd <- sd_series(s)
        if (n_finite < as.integer(min_obs)) {
          keep[j] <- FALSE
          excluded <- add_exclusion_entry(
            excluded,
            colnames(full_rwl)[j],
            paste0(
              "Excluded before heavy dplR retry because the series has fewer than ",
              as.integer(min_obs),
              " finite values (",
              n_finite,
              ")."
            )
          )
        } else if (n_pos < as.integer(min_positive)) {
          keep[j] <- FALSE
          excluded <- add_exclusion_entry(
            excluded,
            colnames(full_rwl)[j],
            paste0(
              "Excluded before heavy dplR retry because the series has fewer than ",
              as.integer(min_positive),
              " positive values (",
              n_pos,
              ")."
            )
          )
        } else if (is.finite(s_sd) && s_sd <= 0) {
          keep[j] <- FALSE
          excluded <- add_exclusion_entry(
            excluded,
            colnames(full_rwl)[j],
            "Excluded before heavy dplR retry because the series variance is zero."
          )
        }
      }
      return(list(rwl=full_rwl[, keep, drop=FALSE], excluded=excluded))
    }

    sanitize_signal_free_common <- function(x, floor_frac=0.01) {
      vals <- as.numeric(x)
      good <- is.finite(vals)
      if (!any(good)) return(vals)
      pos <- vals[good & vals > 0]
      if (length(pos) == 0) {
        vals[good] <- 1
        return(vals)
      }
      floor_val <- max(min(pos, na.rm=TRUE) * as.numeric(floor_frac), 1e-06)
      vals[good & vals <= floor_val] <- floor_val
      return(vals)
    }

    keep_signal_free_ready_series <- function(rwl_df, min_obs=4L, min_positive=3L) {
      if (is.null(rwl_df) || ncol(rwl_df) == 0) return(rwl_df)
      keep <- rep(TRUE, ncol(rwl_df))
      for (j in seq_len(ncol(rwl_df))) {
        vals <- as.numeric(rwl_df[[j]])
        n_finite <- sum(is.finite(vals))
        n_pos <- sum(is.finite(vals) & vals > 0)
        if (n_finite < as.integer(min_obs) || n_pos < as.integer(min_positive)) {
          keep[j] <- FALSE
        }
      }
      rwl_df[, keep, drop=FALSE]
    }

    expand_rwi_to_full <- function(rwi_sub, full_rwl) {
      out <- as.data.frame(matrix(NA_real_, nrow=nrow(full_rwl), ncol=ncol(full_rwl)))
      rownames(out) <- rownames(full_rwl)
      colnames(out) <- colnames(full_rwl)
      if (is.null(rwi_sub) || ncol(rwi_sub) == 0) return(out)
      common <- intersect(colnames(rwi_sub), colnames(out))
      if (length(common) > 0) {
        out[, common] <- as.data.frame(rwi_sub[, common, drop=FALSE])
      }
      return(out)
    }

    method_runs_successfully <- function(full_rwl, keep_cols, method_fun, min_series=2L) {
      keep_cols <- as.character(keep_cols)
      if (length(keep_cols) < as.integer(min_series)) return(FALSE)
      trial_rwl <- full_rwl[, keep_cols, drop=FALSE]
      trial_res <- tryCatch(method_fun(trial_rwl), error=function(e) e)
      return(!inherits(trial_res, "error"))
    }

    find_culprit_by_group_elimination <- function(full_rwl, remaining_cols, method_fun, min_series=2L) {
      candidates <- as.character(remaining_cols)
      if (length(candidates) <= 1) return(NA_character_)
      stable_remaining <- as.character(remaining_cols)

      repeat {
        n_cand <- length(candidates)
        if (n_cand <= 1) break

        split_at <- floor(n_cand / 2)
        left <- candidates[seq_len(split_at)]
        right <- candidates[(split_at + 1):n_cand]
        found_half <- FALSE

        if (length(left) > 0) {
          keep_left_removed <- setdiff(stable_remaining, left)
          if (length(keep_left_removed) >= as.integer(min_series)) {
            if (method_runs_successfully(full_rwl, keep_left_removed, method_fun, min_series=min_series)) {
              candidates <- left
              stable_remaining <- keep_left_removed
              found_half <- TRUE
            }
          }
        }

        if (!found_half && length(right) > 0) {
          keep_right_removed <- setdiff(stable_remaining, right)
          if (length(keep_right_removed) >= as.integer(min_series)) {
            if (method_runs_successfully(full_rwl, keep_right_removed, method_fun, min_series=min_series)) {
              candidates <- right
              stable_remaining <- keep_right_removed
              found_half <- TRUE
            }
          }
        }

        if (!found_half) break
      }

      if (length(candidates) == 1) {
        return(as.character(candidates[[1]]))
      }
      return(NA_character_)
    }

    run_method_with_series_retry <- function(full_rwl, method_fun, min_series=2L) {
      pref <- prefilter_heavy_method_series(
        full_rwl,
        min_obs=max(3L, as.integer(min_series)),
        min_positive=2L
      )
      full_rwl2 <- pref$rwl
      excluded <- pref$excluded
      remaining_cols <- colnames(full_rwl2)
      if (length(remaining_cols) < as.integer(min_series)) {
        stop("Too few series are available for this method.")
      }
      repeat {
        subset_rwl <- full_rwl2[, remaining_cols, drop=FALSE]
        res <- tryCatch(method_fun(subset_rwl), error=function(e) e)
        if (!inherits(res, "error")) {
          res$rwi <- expand_rwi_to_full(as.data.frame(res$rwi), full_rwl)
          res$excluded <- excluded
          return(res)
        }
        err_msg <- conditionMessage(res)
        culprit <- NA_character_
        if (length(remaining_cols) <= as.integer(min_series)) {
          stop(
            paste0(
              "Too few series remained after exclusions. Last observed error: ",
              err_msg
            )
          )
        }
        culprit <- find_culprit_by_group_elimination(
          full_rwl2,
          remaining_cols,
          method_fun,
          min_series=min_series
        )
        if (is.na(culprit)) {
          ranked_cols <- remaining_cols
          if (length(ranked_cols) > 1) {
            finite_counts <- vapply(
              ranked_cols,
              function(nm) finite_count_series(full_rwl2[[nm]]),
              numeric(1)
            )
            pos_counts <- vapply(
              ranked_cols,
              function(nm) positive_count_series(full_rwl2[[nm]]),
              numeric(1)
            )
            sds <- vapply(
              ranked_cols,
              function(nm) {
                val <- sd_series(full_rwl2[[nm]])
                if (is.finite(val)) val else Inf
              },
              numeric(1)
            )
            ord <- order(finite_counts, pos_counts, sds, ranked_cols, decreasing=FALSE, na.last=TRUE)
            ranked_cols <- ranked_cols[ord]
          }
          for (series_name in ranked_cols) {
            trial_cols <- setdiff(remaining_cols, series_name)
            if (length(trial_cols) < as.integer(min_series)) next
            if (method_runs_successfully(full_rwl2, trial_cols, method_fun, min_series=min_series)) {
              culprit <- as.character(series_name)
              break
            }
          }
        }
        if (is.na(culprit)) {
          stop(
            paste0(
              "Method failed even after single-series exclusion attempts. ",
              "Last observed error: ",
              err_msg
            )
          )
        }
        excluded <- add_exclusion_entry(
          excluded,
          culprit,
          paste0(
            "Excluded after retry because the method failed on the current subset. ",
            "Last observed error: ",
            err_msg
          )
        )
        remaining_cols <- setdiff(remaining_cols, culprit)
      }
    }


    # Helper: map po table to a named offset vector (defaults to 1).
    get_po_vec <- function(rwl, po) {
      po_use <- ensure_po(rwl, po)
      series_col <- if ("series" %in% names(po_use)) "series" else names(po_use)[1]
      offset_col <- if ("offset" %in% names(po_use)) "offset" else if ("pith.offset" %in% names(po_use)) "pith.offset" else names(po_use)[2]
      v <- rep(1, ncol(rwl))
      names(v) <- colnames(rwl)
      for (i in 1:nrow(po_use)) {
        s <- as.character(po_use[[series_col]][i])
        if (s %in% names(v)) {
          v[[s]] <- as.numeric(po_use[[offset_col]][i])
        }
      }
      v[is.na(v) | v < 1] <- 1
      return(v)
    }

    # Helper: apply an RC curve (by cambial age) to build fitted values aligned to the rwl matrix.
    apply_rc_curve <- function(rwl, po_vec, rc) {
      out <- matrix(NA_real_, nrow=nrow(rwl), ncol=ncol(rwl))
      rownames(out) <- rownames(rwl)
      colnames(out) <- colnames(rwl)
      rc <- as.numeric(rc)
      for (j in 1:ncol(rwl)) {
        y <- rwl[, j]
        idx <- which(!is.na(y))
        if (length(idx) == 0) next
        rings <- seq_len(length(idx))
        age <- rings + as.numeric(po_vec[[colnames(rwl)[j]]]) - 1
        fit <- rep(NA_real_, length(y))
        ok <- age >= 1 & age <= length(rc)
        if (any(ok)) {
          fit[idx[ok]] <- rc[age[ok]]
        }
        out[, j] <- fit
      }
      return(as.data.frame(out))
    }

    detrend_linear <- function(rwl, difference, pos_slope) {
      out <- as.data.frame(rwl)
      rownames(out) <- rownames(rwl)
      colnames(out) <- colnames(rwl)
      x <- seq_len(nrow(rwl))
      for (j in 1:ncol(rwl)) {
        y <- rwl[, j]
        mask <- !is.na(y)
        out[, j] <- NA_real_
        if (sum(mask) < 2) next
        fit <- lm(y[mask] ~ x[mask])
        slope <- as.numeric(coef(fit)[2])
        fitted <- rep(NA_real_, length(y))
        fitted[mask] <- as.numeric(predict(fit))
        if (!pos_slope && is.finite(slope) && slope > 0) {
          mu <- mean(y[mask], na.rm=TRUE)
          fitted[mask] <- mu
        }
        if (any(fitted[mask] <= 0, na.rm=TRUE)) {
          mu <- mean(y[mask], na.rm=TRUE)
          fitted[mask] <- mu
        }
        if (difference) {
          out[mask, j] <- y[mask] - fitted[mask]
        } else {
          out[mask, j] <- y[mask] / fitted[mask]
        }
      }
      return(out)
    }

    detrend_dplr_blockwise <- function(rwl, method, nyrs=NULL, f=NULL,
                                       pos_slope, constrain_nls, difference) {
      # dplR::detrend() can fail when a series contains internal NA gaps.
      # To keep computation inside dplR, run dplR::detrend.series() on each
      #
      # NOTE: dplR function signatures have changed across versions.
      # We only pass arguments that exist in the installed dplR to avoid
      # "unused arguments" errors.
      out <- as.data.frame(matrix(NA_real_, nrow=nrow(rwl), ncol=ncol(rwl)))
      rownames(out) <- rownames(rwl)
      colnames(out) <- colnames(rwl)
      excluded <- empty_exclusion_log()
      fm <- names(formals(detrend.series))
      for (j in seq_len(ncol(rwl))) {
        y_full <- as.numeric(rwl[, j])
        ok <- which(!is.na(y_full))
        if (length(ok) < 2) next
        blocks <- split(ok, cumsum(c(1, diff(ok) != 1)))
        series_failed <- FALSE
        for (b in blocks) {
          y <- y_full[b]
          if (length(y) < 2) next
          args <- list(
            y,
            y.name="",
            make.plot=FALSE,
            method=method
          )
          if (!is.null(nyrs) && ("nyrs" %in% fm)) args$nyrs <- nyrs
          if (!is.null(f) && ("f" %in% fm)) args$f <- f
          if ("pos.slope" %in% fm) args$pos.slope <- pos_slope
          if ("constrain.nls" %in% fm) args$constrain.nls <- constrain_nls
          if ("difference" %in% fm) args$difference <- difference
          rwi_b <- tryCatch(do.call(detrend.series, args), error=function(e) e)
          if (inherits(rwi_b, "error")) {
            excluded <- add_exclusion_entry(
              excluded,
              colnames(rwl)[j],
              conditionMessage(rwi_b)
            )
            series_failed <- TRUE
            break
          }
          out[b, j] <- as.numeric(rwi_b)
        }
        if (isTRUE(series_failed)) {
          out[, j] <- NA_real_
        }
      }
      return(list(rwi=out, excluded=excluded))
    }

    detrend_runmean <- function(rwl, window, min_periods, difference) {
      out <- as.data.frame(rwl)
      rownames(out) <- rownames(rwl)
      colnames(out) <- colnames(rwl)
      half <- floor(window / 2)
      for (j in 1:ncol(rwl)) {
        y <- as.numeric(rwl[, j])
        out[, j] <- NA_real_
        if (all(is.na(y))) next
        trend <- rep(NA_real_, length(y))
        for (i in 1:length(y)) {
          left <- i - half
          right <- left + window - 1
          if (left < 1) {
            left <- 1
            right <- min(length(y), left + window - 1)
          }
          if (right > length(y)) {
            right <- length(y)
            left <- max(1, right - window + 1)
          }
          w <- y[left:right]
          n_ok <- sum(!is.na(w))
          if (n_ok >= min_periods) {
            trend[i] <- mean(w, na.rm=TRUE)
          }
        }
        mask <- !is.na(y) & !is.na(trend)
        if (any(mask) && any(trend[mask] <= 0, na.rm=TRUE)) {
          mu <- mean(y[!is.na(y)], na.rm=TRUE)
          trend[mask] <- mu
        }
        if (difference) {
          out[mask, j] <- y[mask] - trend[mask]
        } else {
          out[mask, j] <- y[mask] / trend[mask]
        }
      }
      return(out)
    }

    detrend_rcs_signal_free <- function(rwl, po, difference, max_iter) {
      po_use <- ensure_po(rwl, po)
      po_vec <- get_po_vec(rwl, po_use)
      raw <- as.data.frame(rwl)
      rownames(raw) <- rownames(rwl)
      colnames(raw) <- colnames(rwl)
      raw <- keep_signal_free_ready_series(raw, min_obs=4L, min_positive=3L)
      if (ncol(raw) < 2) {
        stop("Too few usable series remained for signal-free RCS.")
      }

      indices_from_rc <- function(rc_curve) {
        fit <- apply_rc_curve(raw, po_vec, rc_curve)
        out <- raw
        rownames(out) <- rownames(raw)
        colnames(out) <- colnames(raw)
        for (j in 1:ncol(raw)) {
          y <- as.numeric(raw[, j])
          t <- as.numeric(fit[, j])
          mask <- !is.na(y) & !is.na(t) & t > 0
          out[, j] <- NA_real_
          if (any(mask)) {
            if (difference) out[mask, j] <- y[mask] - t[mask] else out[mask, j] <- y[mask] / t[mask]
          }
        }
        return(as.data.frame(out))
      }

      res0 <- rcs(raw, po=po_use, ratios=!difference, rc.out=TRUE, make.plot=FALSE)
      rc_curve <- res0$rc
      rwi <- indices_from_rc(rc_curve)
      rwi <- keep_signal_free_ready_series(rwi, min_obs=3L, min_positive=0L)
      if (ncol(rwi) < 2) {
        stop("Too few usable series remained after the initial signal-free RCS step.")
      }
      crn <- chron(rwi, biweight=TRUE, prewhiten=FALSE)
      prev_std <- sanitize_signal_free_common(crn$std)
      if (sum(is.finite(prev_std)) < 3) {
        stop("Signal-free RCS chronology has fewer than 3 usable data points.")
      }

      for (it in 1:max_iter) {
        common <- prev_std
        common2 <- rep(NA_real_, nrow(raw))
        names(common2) <- rownames(raw)
        common2[names(common)] <- sanitize_signal_free_common(common)

        sf <- raw
        for (j in 1:ncol(raw)) {
          y <- as.numeric(raw[, j])
          mask <- !is.na(y) & !is.na(common2)
          sf[, j] <- NA_real_
          if (any(mask)) {
            if (difference) {
              sf[mask, j] <- y[mask] - common2[mask]
            } else {
              denom <- pmax(as.numeric(common2[mask]), 1e-06)
              sf[mask, j] <- y[mask] / denom
            }
          }
          ym <- mean(y[!is.na(y)], na.rm=TRUE)
          sfm <- mean(sf[mask, j], na.rm=TRUE)
          if (is.finite(ym) && is.finite(sfm)) {
            if (difference) {
              sf[mask, j] <- sf[mask, j] + (ym - sfm)
            } else {
              if (sfm != 0) sf[mask, j] <- sf[mask, j] * (ym / sfm)
            }
          }
        }
        sf <- keep_signal_free_ready_series(sf, min_obs=4L, min_positive=(if (difference) 0L else 3L))
        if (ncol(sf) < 2) {
          stop("Too few usable series remained during signal-free RCS iterations.")
        }

        res1 <- rcs(sf, po=po_use, ratios=!difference, rc.out=TRUE, make.plot=FALSE)
        rc_curve <- res1$rc
        rwi <- indices_from_rc(rc_curve)
        rwi <- keep_signal_free_ready_series(rwi, min_obs=3L, min_positive=0L)
        if (ncol(rwi) < 2) {
          stop("Too few usable series remained after a signal-free RCS iteration.")
        }
        crn <- chron(rwi, biweight=TRUE, prewhiten=FALSE)
        std <- sanitize_signal_free_common(crn$std)
        if (sum(is.finite(std)) < 3) {
          stop("Signal-free RCS chronology has fewer than 3 usable data points.")
        }

        common_years <- intersect(names(prev_std), names(std))
        if (length(common_years) > 0) {
          delta <- as.numeric(std[common_years]) - as.numeric(prev_std[common_years])
          madv <- median(abs(delta), na.rm=TRUE)
          if (is.finite(madv) && madv < 1e-6) {
            prev_std <- std
            break
          }
        }
        prev_std <- std
      }

      return(list(rwi=rwi, crn=crn, rc=rc_curve))
    }

    run_one_method <- function(k) {
      if (is.null(specs[[k]])) {
        return(invisible(NULL))
      }
      spec <- specs[[k]]
      kind <- spec$kind
      t0 <- proc.time()[["elapsed"]]
      message(paste0("dplR detrending START: ", k))
      tryCatch({
      if (kind == "detrend") {
        if (!is.null(spec$nyrs)) {
          out_det <- detrend_dplr_blockwise(
            rwl, method=spec$method,
            nyrs=spec$nyrs, f=spec$f,
            pos_slope=pos_slope, constrain_nls=constrain_nls,
            difference=difference
          )
        } else {
          out_det <- detrend_dplr_blockwise(
            rwl, method=spec$method,
            pos_slope=pos_slope, constrain_nls=constrain_nls,
            difference=difference
          )
        }
        rwi <- out_det$rwi
        crn <- chron(rwi, biweight=TRUE, prewhiten=FALSE)
        write.csv(rwi, file.path(out_dir, paste0("dplr_rwi_", k, ".csv")), row.names=TRUE)
        write.csv(crn, file.path(out_dir, paste0("dplr_chron_", k, ".csv")), row.names=TRUE)
        write_exclusion_log(
          out_det$excluded,
          file.path(out_dir, paste0("dplr_series_exclusions_", k, ".tsv"))
        )
      } else if (kind == "custom_linear") {
        rwi <- as.data.frame(matrix(NA_real_, nrow=nrow(rwl), ncol=ncol(rwl)))
        rownames(rwi) <- rownames(rwl)
        colnames(rwi) <- colnames(rwl)
        excluded <- empty_exclusion_log()
        for (j in seq_len(ncol(rwl))) {
          one <- rwl[, j, drop=FALSE]
          out_one <- tryCatch(
            detrend_linear(one, difference=difference, pos_slope=pos_slope),
            error=function(e) e
          )
          if (inherits(out_one, "error")) {
            excluded <- add_exclusion_entry(
              excluded,
              colnames(rwl)[j],
              conditionMessage(out_one)
            )
          } else {
            rwi[, j] <- as.numeric(out_one[, 1])
          }
        }
        crn <- chron(rwi, biweight=TRUE, prewhiten=FALSE)
        write.csv(rwi, file.path(out_dir, paste0("dplr_rwi_", k, ".csv")), row.names=TRUE)
        write.csv(crn, file.path(out_dir, paste0("dplr_chron_", k, ".csv")), row.names=TRUE)
        write_exclusion_log(
          excluded,
          file.path(out_dir, paste0("dplr_series_exclusions_", k, ".tsv"))
        )
      } else if (kind == "custom_runmean") {
        rwi <- as.data.frame(matrix(NA_real_, nrow=nrow(rwl), ncol=ncol(rwl)))
        rownames(rwi) <- rownames(rwl)
        colnames(rwi) <- colnames(rwl)
        excluded <- empty_exclusion_log()
        for (j in seq_len(ncol(rwl))) {
          one <- rwl[, j, drop=FALSE]
          out_one <- tryCatch(
            detrend_runmean(
              one,
              window=spec$window,
              min_periods=spec$min_periods,
              difference=difference
            ),
            error=function(e) e
          )
          if (inherits(out_one, "error")) {
            excluded <- add_exclusion_entry(
              excluded,
              colnames(rwl)[j],
              conditionMessage(out_one)
            )
          } else {
            rwi[, j] <- as.numeric(out_one[, 1])
          }
        }
        crn <- chron(rwi, biweight=TRUE, prewhiten=FALSE)
        write.csv(rwi, file.path(out_dir, paste0("dplr_rwi_", k, ".csv")), row.names=TRUE)
        write.csv(crn, file.path(out_dir, paste0("dplr_chron_", k, ".csv")), row.names=TRUE)
        write_exclusion_log(
          excluded,
          file.path(out_dir, paste0("dplr_series_exclusions_", k, ".tsv"))
        )

      } else if (kind == "rcs") {
        po_use <- ensure_po(rwl, po)
        out_rcs <- run_method_with_series_retry(
          rwl,
          function(rwl_sub) {
            po_sub <- ensure_po(rwl_sub, subset_po_table(po_use, colnames(rwl_sub)))
            res <- rcs(rwl_sub, po=po_sub, ratios=!difference, rc.out=TRUE, make.plot=FALSE)
            crn <- chron(res$rwi, biweight=TRUE, prewhiten=FALSE)
            list(rwi=res$rwi, crn=crn, rc=res$rc)
          }
        )
        rwi <- out_rcs$rwi
        crn <- out_rcs$crn
        write.csv(rwi, file.path(out_dir, paste0("dplr_rwi_", k, ".csv")), row.names=TRUE)
        write.csv(crn, file.path(out_dir, paste0("dplr_chron_", k, ".csv")), row.names=TRUE)
        write.csv(data.frame(age=seq_along(out_rcs$rc), rc=out_rcs$rc),
                  file.path(out_dir, paste0("dplr_rc_curve_", k, ".csv")),
                  row.names=FALSE)
        write_exclusion_log(
          out_rcs$excluded,
          file.path(out_dir, paste0("dplr_series_exclusions_", k, ".tsv"))
        )
      } else if (kind == "custom_rcs_signal_free") {
        po_use <- ensure_po(rwl, po)
        out2 <- run_method_with_series_retry(
          rwl,
          function(rwl_sub) {
            po_sub <- ensure_po(rwl_sub, subset_po_table(po_use, colnames(rwl_sub)))
            detrend_rcs_signal_free(
              rwl_sub,
              po_sub,
              difference=difference,
              max_iter=spec$max_iter
            )
          },
          min_series=3L
        )
        rwi <- out2$rwi
        crn <- out2$crn
        write.csv(rwi, file.path(out_dir, paste0("dplr_rwi_", k, ".csv")), row.names=TRUE)
        write.csv(crn, file.path(out_dir, paste0("dplr_chron_", k, ".csv")), row.names=TRUE)
        write.csv(data.frame(age=seq_along(out2$rc), rc=out2$rc),
                  file.path(out_dir, paste0("dplr_rc_curve_", k, ".csv")),
                  row.names=FALSE)
        write_exclusion_log(
          out2$excluded,
          file.path(out_dir, paste0("dplr_series_exclusions_", k, ".tsv"))
        )

      } else if (kind == "ssf") {
        # Signal-free standardization from dplR (returns chronology + RWI array in return.info)
        #
        # NOTE: dplR function signatures have changed across versions.
        # We only pass arguments that exist in the installed dplR to avoid
        # "unused arguments" errors.
        out_ssf <- run_method_with_series_retry(
          rwl,
          function(rwl_sub) {
            rwl_sub <- keep_signal_free_ready_series(
              rwl_sub,
              min_obs=4L,
              min_positive=(if (difference) 0L else 3L)
            )
            if (ncol(rwl_sub) < 2) {
              stop("Too few usable series remained for signal-free standardization.")
            }
            fm_ssf <- names(formals(ssf))
            if (!("return.info" %in% fm_ssf)) {
              stop("ssf() in this dplR version does not support return.info=TRUE, which is required by this script.")
            }
            args <- list(
              rwl_sub,
              method=spec$method,
              return.info=TRUE
            )
            if (!is.null(spec$nyrs) && ("nyrs" %in% fm_ssf)) args$nyrs <- spec$nyrs
            if (!is.null(spec$f) && ("f" %in% fm_ssf)) args$f <- spec$f
            if (!is.null(spec$max_iterations) && ("max.iterations" %in% fm_ssf)) args$max.iterations <- spec$max_iterations
            if (!is.null(spec$mad_threshold) && ("mad.threshold" %in% fm_ssf)) args$mad.threshold <- spec$mad_threshold
            if ("pos.slope" %in% fm_ssf) args$pos.slope <- pos_slope
            if ("constrain.nls" %in% fm_ssf) args$constrain.nls <- constrain_nls
            if ("difference" %in% fm_ssf) args$difference <- difference
            if ("recode.zeros" %in% fm_ssf) args$recode.zeros <- recode_zeros
            out <- do.call(ssf, args)
            it <- out$k + 1
            arr <- out$sfRWI_Array[,,it]
            rwi <- as.data.frame(arr)
            rownames(rwi) <- rownames(rwl_sub)
            colnames(rwi) <- colnames(rwl_sub)
            list(rwi=rwi, crn=out$ssfCrn)
          },
          min_series=3L
        )
        rwi <- out_ssf$rwi
        crn <- out_ssf$crn
        write.csv(rwi, file.path(out_dir, paste0("dplr_rwi_", k, ".csv")), row.names=TRUE)
        write.csv(crn, file.path(out_dir, paste0("dplr_chron_", k, ".csv")), row.names=TRUE)
        write_exclusion_log(
          out_ssf$excluded,
          file.path(out_dir, paste0("dplr_series_exclusions_", k, ".tsv"))
        )
      }

      dt <- proc.time()[["elapsed"]] - t0
      message(sprintf("dplR detrending DONE: %s | %.3fs", k, dt))

      }, error=function(e) {
        err_path <- file.path(out_dir, paste0("dplr_error_", k, ".txt"))
        msg <- paste0("Error in method '", k, "': ", conditionMessage(e))
        writeLines(msg, err_path)
        dt <- proc.time()[["elapsed"]] - t0
        message(sprintf("dplR detrending ERROR: %s | %.3fs | %s", k, dt, conditionMessage(e)))
      })
      invisible(NULL)
    }

    run_all_methods <- function(keys_to_run) {
      keys_to_run <- as.character(keys_to_run)
      keys_to_run <- keys_to_run[nzchar(keys_to_run)]
      if (length(keys_to_run) <= 1L) {
        invisible(lapply(keys_to_run, run_one_method))
        return(invisible(NULL))
      }
      n_workers <- resolve_parallel_workers(length(keys_to_run))
      if (n_workers <= 1L) {
        invisible(lapply(keys_to_run, run_one_method))
        return(invisible(NULL))
      }
      message(
        paste0(
          "dplR detrending parallel workers: ",
          n_workers,
          " (queued: ",
          max(0L, length(keys_to_run) - n_workers),
          ")"
        )
      )
      cl <- tryCatch(parallel::makeCluster(n_workers), error=function(e) e)
      if (inherits(cl, "error")) {
        invisible(lapply(keys_to_run, run_one_method))
        return(invisible(NULL))
      }
      on.exit(try(parallel::stopCluster(cl), silent=TRUE), add=TRUE)
      export_names <- c(
        "keys", "specs", "rwl", "po", "out_dir",
        "pos_slope", "constrain_nls", "difference", "recode_zeros",
        "ensure_po", "subset_po_table",
        "empty_exclusion_log", "add_exclusion_entry", "write_exclusion_log",
        "resolve_parallel_workers",
        "finite_count_series", "positive_count_series", "sd_series",
        "prefilter_heavy_method_series", "keep_signal_free_ready_series", "sanitize_signal_free_common",
        "expand_rwi_to_full",
        "method_runs_successfully", "find_culprit_by_group_elimination",
        "run_method_with_series_retry",
        "get_po_vec", "apply_rc_curve",
        "detrend_linear", "detrend_dplr_blockwise", "detrend_runmean",
        "detrend_rcs_signal_free",
        "run_one_method"
      )
      parallel::clusterExport(cl, export_names, envir=environment())
      parallel::clusterEvalQ(
        cl,
        {
          suppressPackageStartupMessages(library(dplR))
          NULL
        }
      )
      invisible(parallel::parLapply(cl, keys_to_run, run_one_method))
      invisible(NULL)
    }

    run_all_methods(keys)

""").lstrip()

_VALIDATE_EPS_RBAR_DPLR_R = textwrap.dedent(r"""

    args <- commandArgs(trailingOnly=TRUE)
    if (length(args) < 2) {
      stop("Usage: Rscript validate_eps_rbar_dplr.R <rwi_dir> <out_dir> [method_keys_csv] [key=value ...]")
    }
    rwi_dir <- args[[1]]
    out_dir <- args[[2]]

    method_keys_csv <- ""
    if (length(args) >= 3) {
      method_keys_csv <- args[[3]]
    }

    opts <- list()
    if (length(args) >= 4) {
      for (i in 4:length(args)) {
        kv <- strsplit(args[[i]], "=", fixed=TRUE)[[1]]
        if (length(kv) == 2) {
          opts[[kv[[1]]]] <- kv[[2]]
        }
      }
    }

    get_opt <- function(name, default) {
      if (!is.null(opts[[name]]) && nzchar(opts[[name]])) return(opts[[name]])
      return(default)
    }

    as_bool <- function(x) {
      x <- tolower(trimws(as.character(x)))
      if (x %in% c("true","t","1","yes","y")) return(TRUE)
      if (x %in% c("false","f","0","no","n")) return(FALSE)
      return(FALSE)
    }

    auto_install <- as_bool(get_opt("auto_install", "FALSE"))
    cran_repo <- get_opt("cran_repo", "https://cloud.r-project.org")
    window_length_opt <- suppressWarnings(as.integer(get_opt("window_length", "")))
    window_overlap_opt <- suppressWarnings(as.integer(get_opt("window_overlap", "")))
    min_corr_overlap_opt <- suppressWarnings(as.integer(get_opt("min_corr_overlap", "")))
    parallel_workers_opt <- suppressWarnings(as.integer(get_opt("parallel_workers", "")))
    if (!requireNamespace("dplR", quietly=TRUE)) {
      if (auto_install) {
        install.packages("dplR", repos=cran_repo)
      } else {
        stop("Package 'dplR' is not installed. Install it in R, or run with auto_install=TRUE.")
      }
    }
    suppressPackageStartupMessages(library(dplR))

    dir.create(out_dir, showWarnings=FALSE, recursive=TRUE)
    writeLines(as.character(packageVersion("dplR")), file.path(out_dir, "dplr_version.txt"))

    full_cols <- c(
      "rbar.tot","rbar.wt","rbar.bt","c.eff","rbar.eff",
      "eps","snr","n.cores","n.trees","n"
    )
    run_cols <- c(
      "start.year","mid.year","end.year",
      "rbar.tot","rbar.wt","rbar.bt","c.eff","rbar.eff",
      "eps","snr","n.cores","n.trees","n",
      "rbar","depth","n_series_win","n_years_win"
    )

    empty_full <- function() {
      out <- as.data.frame(matrix(numeric(0), nrow=0, ncol=length(full_cols)))
      names(out) <- full_cols
      out
    }

    empty_run <- function() {
      out <- as.data.frame(matrix(numeric(0), nrow=0, ncol=length(run_cols)))
      names(out) <- run_cols
      out
    }

    empty_exclusion_log <- function() {
      data.frame(series=character(0), reason=character(0), stringsAsFactors=FALSE)
    }

    add_exclusion_entry <- function(log_df, series_name, reason_text) {
      rbind(
        log_df,
        data.frame(
          series=as.character(series_name),
          reason=as.character(reason_text),
          stringsAsFactors=FALSE
        )
      )
    }

    write_exclusion_log <- function(log_df, path) {
      if (is.null(log_df) || nrow(log_df) == 0) return(invisible(NULL))
      write.table(
        log_df,
        file=path,
        sep="\t",
        row.names=FALSE,
        col.names=TRUE,
        quote=TRUE
      )
      invisible(NULL)
    }

    resolve_parallel_workers <- function(n_tasks) {
      n_tasks <- as.integer(max(1L, n_tasks))
      if (!is.na(parallel_workers_opt) && parallel_workers_opt > 0L) {
        return(as.integer(max(1L, min(n_tasks, parallel_workers_opt))))
      }
      n_detect <- suppressWarnings(parallel::detectCores(logical=TRUE))
      if (!is.finite(n_detect) || is.na(n_detect)) n_detect <- 1L
      reserve <- if (n_detect >= 6L) 2L else 1L
      by_headroom <- max(1L, as.integer(n_detect) - reserve)
      by_fraction <- max(1L, floor(as.integer(n_detect) / 2L))
      default_cap <- 4L
      return(as.integer(max(1L, min(n_tasks, by_headroom, by_fraction, default_cap))))
    }

    read_rwi_csv <- function(path) {
      df <- read.csv(path, row.names=1, check.names=FALSE, stringsAsFactors=FALSE)
      for (j in seq_len(ncol(df))) {
        df[[j]] <- as.numeric(df[[j]])
      }
      as.data.frame(df)
    }

    normalize_stats_df <- function(x) {
      if (is.null(x)) {
        return(empty_full())
      }
      if (is.data.frame(x)) {
        out <- x
      } else {
        out <- as.data.frame(as.list(x), check.names=FALSE, stringsAsFactors=FALSE)
      }
      for (nm in full_cols) {
        if (!(nm %in% names(out))) out[[nm]] <- NA_real_
      }
      out <- out[, full_cols, drop=FALSE]
      for (j in seq_len(ncol(out))) {
        out[[j]] <- as.numeric(out[[j]])
      }
      out
    }

    normalize_running_df <- function(x) {
      if (is.null(x)) {
        return(empty_run())
      }
      out <- as.data.frame(x, check.names=FALSE, stringsAsFactors=FALSE)
      if ("rbar.tot" %in% names(out) && !("rbar" %in% names(out))) out$rbar <- out[["rbar.tot"]]
      if ("n.trees" %in% names(out) && !("depth" %in% names(out))) out$depth <- out[["n.trees"]]
      if ("n.cores" %in% names(out) && !("n_series_win" %in% names(out))) out$n_series_win <- out[["n.cores"]]
      if (all(c("start.year","end.year") %in% names(out)) && !("n_years_win" %in% names(out))) {
        out$n_years_win <- as.numeric(out[["end.year"]]) - as.numeric(out[["start.year"]]) + 1
      }
      for (nm in run_cols) {
        if (!(nm %in% names(out))) out[[nm]] <- NA_real_
      }
      out <- out[, run_cols, drop=FALSE]
      for (j in seq_len(ncol(out))) {
        out[[j]] <- as.numeric(out[[j]])
      }
      out
    }

    compute_eps_rbar <- function(rwi) {
      if (is.null(rwi) || ncol(rwi) < 2 || nrow(rwi) < 1) {
        return(list(stats=empty_full(), running=empty_run()))
      }

      if (is.na(window_length_opt) || window_length_opt <= 0L) {
        wl <- min(50L, nrow(rwi))
      } else {
        wl <- min(as.integer(window_length_opt), nrow(rwi))
      }
      if (is.na(window_overlap_opt) || window_overlap_opt < 0L) {
        wo <- floor(wl / 2)
      } else {
        wo <- min(as.integer(window_overlap_opt), max(0L, wl - 1L))
      }
      if (is.na(min_corr_overlap_opt) || min_corr_overlap_opt <= 0L) {
        mo <- min(30L, wl)
      } else {
        mo <- min(as.integer(min_corr_overlap_opt), wl)
      }

      fm_stats <- names(formals(rwi.stats))
      args_stats <- list(rwi)
      if ("method" %in% fm_stats) args_stats$method <- "spearman"
      if ("zero.is.missing" %in% fm_stats) args_stats$zero.is.missing <- TRUE
      if ("min.corr.overlap" %in% fm_stats) args_stats$min.corr.overlap <- mo
      stats_df <- normalize_stats_df(do.call(rwi.stats, args_stats))

      if (wl < 3L || wo >= wl || mo > wl) {
        running_df <- empty_run()
      } else {
        fm_run <- names(formals(rwi.stats.running))
        args_run <- list(rwi)
        if ("method" %in% fm_run) args_run$method <- "spearman"
        if ("zero.is.missing" %in% fm_run) args_run$zero.is.missing <- TRUE
        if ("window.length" %in% fm_run) args_run$window.length <- wl
        if ("window.overlap" %in% fm_run) args_run$window.overlap <- wo
        if ("min.corr.overlap" %in% fm_run) args_run$min.corr.overlap <- mo
        running_df <- normalize_running_df(do.call(rwi.stats.running, args_run))
      }

      return(list(stats=stats_df, running=running_df))
    }

    prepare_rwi_for_eps <- function(rwi) {
      excluded <- empty_exclusion_log()
      if (is.null(rwi) || ncol(rwi) == 0) {
        return(list(rwi=rwi, excluded=excluded))
      }
      keep <- rep(TRUE, ncol(rwi))
      for (j in seq_len(ncol(rwi))) {
        vals <- as.numeric(rwi[[j]])
        n_finite <- sum(is.finite(vals))
        if (n_finite < 2) {
          keep[j] <- FALSE
          excluded <- add_exclusion_entry(
            excluded,
            colnames(rwi)[j],
            paste0(
              "Excluded from EPS/RBAR because fewer than 2 finite detrended values remained (",
              n_finite,
              " finite value(s))."
            )
          )
        }
      }
      return(list(rwi=rwi[, keep, drop=FALSE], excluded=excluded))
    }

    eps_runs_successfully <- function(rwi) {
      out <- tryCatch(compute_eps_rbar(rwi), error=function(e) e)
      !inherits(out, "error")
    }

    find_eps_culprit_by_group_elimination <- function(current, min_series=2L) {
      candidates <- colnames(current)
      if (length(candidates) <= 1) return(NA_character_)
      stable_remaining <- candidates
      repeat {
        n_cand <- length(candidates)
        if (n_cand <= 1) break
        split_at <- floor(n_cand / 2)
        left <- candidates[seq_len(split_at)]
        right <- candidates[(split_at + 1):n_cand]
        found_half <- FALSE

        if (length(left) > 0) {
          keep_left_removed <- setdiff(stable_remaining, left)
          if (length(keep_left_removed) >= as.integer(min_series)) {
            if (eps_runs_successfully(current[, keep_left_removed, drop=FALSE])) {
              candidates <- left
              stable_remaining <- keep_left_removed
              found_half <- TRUE
            }
          }
        }

        if (!found_half && length(right) > 0) {
          keep_right_removed <- setdiff(stable_remaining, right)
          if (length(keep_right_removed) >= as.integer(min_series)) {
            if (eps_runs_successfully(current[, keep_right_removed, drop=FALSE])) {
              candidates <- right
              stable_remaining <- keep_right_removed
              found_half <- TRUE
            }
          }
        }

        if (!found_half) break
      }

      if (length(candidates) == 1) {
        return(as.character(candidates[[1]]))
      }
      return(NA_character_)
    }

    compute_eps_rbar_with_retry <- function(rwi) {
      prep <- prepare_rwi_for_eps(rwi)
      current <- prep$rwi
      excluded <- prep$excluded
      if (is.null(current) || ncol(current) < 2 || nrow(current) < 1) {
        return(list(stats=empty_full(), running=empty_run(), excluded=excluded))
      }
      repeat {
        out <- tryCatch(compute_eps_rbar(current), error=function(e) e)
        if (!inherits(out, "error")) {
          out$excluded <- excluded
          return(out)
        }
        err_msg <- conditionMessage(out)
        if (ncol(current) <= 2) {
          stop(
            paste0(
              "EPS/RBAR failed and too few series remained after exclusions. ",
              "Last observed error: ",
              err_msg
            )
          )
        }
        culprit <- find_eps_culprit_by_group_elimination(current, min_series=2L)
        if (is.na(culprit)) {
          ranked_cols <- colnames(current)
          if (length(ranked_cols) > 1) {
            finite_counts <- vapply(
              ranked_cols,
              function(nm) sum(is.finite(as.numeric(current[[nm]]))),
              numeric(1)
            )
            ord <- order(finite_counts, ranked_cols, decreasing=FALSE, na.last=TRUE)
            ranked_cols <- ranked_cols[ord]
          }
          for (series_name in ranked_cols) {
            trial_cols <- setdiff(colnames(current), series_name)
            if (length(trial_cols) < 2) next
            if (eps_runs_successfully(current[, trial_cols, drop=FALSE])) {
              culprit <- as.character(series_name)
              break
            }
          }
        }
        if (is.na(culprit)) {
          stop(
            paste0(
              "EPS/RBAR failed even after single-series exclusion attempts. ",
              "Last observed error: ",
              err_msg
            )
          )
        }
        excluded <- add_exclusion_entry(
          excluded,
          culprit,
          paste0(
            "Excluded after retry because EPS/RBAR failed on the current subset. ",
            "Last observed error: ",
            err_msg
          )
        )
        current <- current[, setdiff(colnames(current), culprit), drop=FALSE]
      }
    }

    keys <- character(0)
    if (!is.null(method_keys_csv) && nzchar(method_keys_csv)) {
      keys <- strsplit(method_keys_csv, ",", fixed=TRUE)[[1]]
      keys <- trimws(keys)
      keys <- keys[nzchar(keys)]
    } else {
      files <- list.files(rwi_dir, pattern="^rwi_.*\\.csv$", full.names=FALSE)
      keys <- sub("^rwi_(.*)\\.csv$", "\\1", files)
    }

    run_one_eps_method <- function(k) {
      in_path <- file.path(rwi_dir, paste0("rwi_", k, ".csv"))
      if (!file.exists(in_path)) return(invisible(NULL))
      t0 <- proc.time()[["elapsed"]]
      message(paste0("dplR EPS/RBAR START: ", k))
      tryCatch({
        rwi <- read_rwi_csv(in_path)
        out <- compute_eps_rbar_with_retry(rwi)
        write.csv(out$stats, file.path(out_dir, paste0("dplr_eps_rbar_stats_", k, ".csv")), row.names=FALSE)
        write.csv(out$running, file.path(out_dir, paste0("dplr_eps_rbar_running_", k, ".csv")), row.names=FALSE)
        write_exclusion_log(
          out$excluded,
          file.path(out_dir, paste0("dplr_eps_rbar_series_exclusions_", k, ".tsv"))
        )
        dt <- proc.time()[["elapsed"]] - t0
        message(sprintf("dplR EPS/RBAR DONE: %s | %.3fs", k, dt))
      }, error=function(e) {
        err_path <- file.path(out_dir, paste0("dplr_eps_rbar_error_", k, ".txt"))
        msg <- paste0("Error in EPS/RBAR method '", k, "': ", conditionMessage(e))
        writeLines(msg, err_path)
        dt <- proc.time()[["elapsed"]] - t0
        message(sprintf("dplR EPS/RBAR ERROR: %s | %.3fs | %s", k, dt, conditionMessage(e)))
      })
      invisible(NULL)
    }

    run_all_eps_methods <- function(keys_to_run) {
      keys_to_run <- as.character(keys_to_run)
      keys_to_run <- keys_to_run[nzchar(keys_to_run)]
      if (length(keys_to_run) <= 1L) {
        invisible(lapply(keys_to_run, run_one_eps_method))
        return(invisible(NULL))
      }
      n_workers <- resolve_parallel_workers(length(keys_to_run))
      if (n_workers <= 1L) {
        invisible(lapply(keys_to_run, run_one_eps_method))
        return(invisible(NULL))
      }
      message(
        paste0(
          "dplR EPS/RBAR parallel workers: ",
          n_workers,
          " (queued: ",
          max(0L, length(keys_to_run) - n_workers),
          ")"
        )
      )
      cl <- tryCatch(parallel::makeCluster(n_workers), error=function(e) e)
      if (inherits(cl, "error")) {
        invisible(lapply(keys_to_run, run_one_eps_method))
        return(invisible(NULL))
      }
      on.exit(try(parallel::stopCluster(cl), silent=TRUE), add=TRUE)
      export_names <- c(
        "rwi_dir", "out_dir",
        "window_length_opt", "window_overlap_opt", "min_corr_overlap_opt",
        "full_cols", "run_cols",
        "empty_full", "empty_run",
        "empty_exclusion_log", "add_exclusion_entry", "write_exclusion_log",
        "resolve_parallel_workers",
        "read_rwi_csv", "normalize_stats_df", "normalize_running_df",
        "compute_eps_rbar", "prepare_rwi_for_eps", "eps_runs_successfully",
        "find_eps_culprit_by_group_elimination", "compute_eps_rbar_with_retry",
        "run_one_eps_method"
      )
      parallel::clusterExport(cl, export_names, envir=environment())
      parallel::clusterEvalQ(
        cl,
        {
          suppressPackageStartupMessages(library(dplR))
          NULL
        }
      )
      invisible(parallel::parLapply(cl, keys_to_run, run_one_eps_method))
      invisible(NULL)
    }

    run_all_eps_methods(keys)

""").lstrip()

_POWT_DPLR_R = textwrap.dedent(r"""

    args <- commandArgs(trailingOnly=TRUE)
    if (length(args) < 3) {
      stop("Usage: Rscript powt_dplr.R <input_csv> <out_dir> <method> [key=value ...]")
    }
    infile <- args[[1]]
    out_dir <- args[[2]]
    powt_method <- tolower(trimws(args[[3]]))

    opts <- list()
    if (length(args) >= 4) {
      for (i in 4:length(args)) {
        kv <- strsplit(args[[i]], "=", fixed=TRUE)[[1]]
        if (length(kv) == 2) {
          opts[[kv[[1]]]] <- kv[[2]]
        }
      }
    }

    get_opt <- function(name, default) {
      if (!is.null(opts[[name]]) && nzchar(opts[[name]])) return(opts[[name]])
      return(default)
    }

    as_bool <- function(x) {
      x <- tolower(trimws(as.character(x)))
      if (x %in% c("true","t","1","yes","y")) return(TRUE)
      if (x %in% c("false","f","0","no","n")) return(FALSE)
      return(FALSE)
    }

    rescale <- as_bool(get_opt("rescale", "FALSE"))
    auto_install <- as_bool(get_opt("auto_install", "FALSE"))
    cran_repo <- get_opt("cran_repo", "https://cloud.r-project.org")

    if (!(powt_method %in% c("universal", "cook"))) {
      stop("POWT method must be one of: universal, cook")
    }

    if (!requireNamespace("dplR", quietly=TRUE)) {
      if (auto_install) {
        install.packages("dplR", repos=cran_repo)
      } else {
        stop("Package 'dplR' is not installed. Install it in R, or run with auto_install=TRUE.")
      }
    }
    suppressPackageStartupMessages(library(dplR))

    dir.create(out_dir, showWarnings=FALSE, recursive=TRUE)
    writeLines(as.character(packageVersion("dplR")), file.path(out_dir, "dplr_version.txt"))

    df <- read.csv(infile, check.names=FALSE, stringsAsFactors=FALSE)
    if ("Year" %in% names(df)) {
      rn <- df$Year
      df$Year <- NULL
      rownames(df) <- rn
    }
    for (j in seq_len(ncol(df))) {
      df[[j]] <- as.numeric(df[[j]])
    }
    rwl <- as.data.frame(df)

    res <- powt(
      rwl,
      method=powt_method,
      rescale=rescale,
      return.power=TRUE
    )

    transformed <- NULL
    power_vals <- NULL
    if (is.list(res)) {
      if ("rwl" %in% names(res)) transformed <- res$rwl
      if (is.null(transformed) && "data" %in% names(res)) transformed <- res$data
      if (is.null(transformed) && "x" %in% names(res)) transformed <- res$x
      if (is.null(transformed)) transformed <- res[[1]]

      if ("power" %in% names(res)) power_vals <- res$power
      if (is.null(power_vals) && "pow" %in% names(res)) power_vals <- res$pow
      if (is.null(power_vals) && length(res) >= 2) power_vals <- res[[2]]
    } else {
      transformed <- res
      power_vals <- rep(NA_real_, ncol(rwl))
      names(power_vals) <- colnames(rwl)
    }

    transformed <- as.data.frame(transformed)
    write.csv(transformed, file.path(out_dir, "powt_transformed.csv"), row.names=TRUE)

    power_df <- data.frame(
      series=colnames(transformed),
      power=as.numeric(power_vals),
      stringsAsFactors=FALSE
    )
    write.csv(power_df, file.path(out_dir, "powt_power.csv"), row.names=FALSE)

    get_skew_vec <- function(x) {
      sx <- summary(x)
      if (is.matrix(sx) && ("skew" %in% rownames(sx))) {
        out <- as.numeric(sx["skew", ])
        names(out) <- colnames(x)
        return(out)
      }
      out <- rep(NA_real_, ncol(x))
      names(out) <- colnames(x)
      return(out)
    }

    raw_skew <- get_skew_vec(rwl)
    powt_skew <- get_skew_vec(transformed)
    write.csv(
      data.frame(series=names(raw_skew), skew=as.numeric(raw_skew), stringsAsFactors=FALSE),
      file.path(out_dir, "powt_raw_skew.csv"),
      row.names=FALSE
    )
    write.csv(
      data.frame(series=names(powt_skew), skew=as.numeric(powt_skew), stringsAsFactors=FALSE),
      file.path(out_dir, "powt_transformed_skew.csv"),
      row.names=FALSE
    )

    message(
      paste0(
        "dplR POWT preview DONE: method=",
        powt_method,
        ", rescale=",
        ifelse(rescale, "TRUE", "FALSE")
      )
    )

""").lstrip()

def _write_validate_dplr_r(path: str) -> None:
    Path(path).write_text(_VALIDATE_DPLR_R, encoding="utf-8")


def _write_validate_eps_rbar_dplr_r(path: str) -> None:
    Path(path).write_text(_VALIDATE_EPS_RBAR_DPLR_R, encoding="utf-8")


def _write_powt_dplr_r(path: str) -> None:
    Path(path).write_text(_POWT_DPLR_R, encoding="utf-8")


def _read_series_exclusion_tsv(path: str) -> Dict[str, str]:
    """Read a per-series exclusion log written by the R harness."""
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, sep="\t", dtype=str)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    series_col = "series" if "series" in df.columns else df.columns[0]
    reason_col = "reason" if "reason" in df.columns else (df.columns[1] if len(df.columns) > 1 else series_col)
    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        series_name = str(row.get(series_col, "") or "").strip()
        if not series_name:
            continue
        reason_text = str(row.get(reason_col, "") or "").strip()
        out[series_name] = reason_text or "Unknown exclusion reason."
    return out

def _write_rwl_csv_for_r(rwl: pd. DataFrame, path: str) -> None:
    df = rwl.copy()
    df.insert(0, "Year", df.index.astype(int))
    df.to_csv(path, index=False)


def _dplr_powt_preview(
    rwl: pd.DataFrame,
    config: DplrCompatConfig,
    verbose: bool = False,
    progress_label: str = "dplR POWT",
) -> PowtPreviewResult:
    """Run dplR::powt and return transformed data plus preview diagnostics."""
    rscript_cmd = _ensure_rscript(config)
    rwl2 = _fill_internal_nas_as_zero_df(rwl)
    rwl2 = _recode_zeros_df(rwl2, True)
    with tempfile.TemporaryDirectory(prefix="detrend_powt_dplr_") as td:
        td_path = str(td)
        script_path = os.path.join(td_path, "powt_dplr.R")
        _write_powt_dplr_r(script_path)
        in_path = os.path.join(td_path, "input_rwl.csv")
        _write_rwl_csv_for_r(rwl2, in_path)
        out_dir = os.path.join(td_path, "out")
        os.makedirs(out_dir, exist_ok=True)

        args = [
            rscript_cmd,
            script_path,
            in_path,
            out_dir,
            str(config.powt_method),
            f"rescale={'TRUE' if config.powt_rescale else 'FALSE'}",
            f"auto_install={'TRUE' if config.auto_install_r_deps else 'FALSE'}",
            f"cran_repo={config.cran_repo}",
        ]
        proc = _run_subprocess_capture(
            args,
            verbose=verbose,
            prefix=f"{progress_label}: ",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "dplR POWT execution failed.\n"
                f"Command: {' '.join(args)}\n"
                f"STDERR:\n{proc.stderr}\n"
                f"STDOUT:\n{proc.stdout}\n"
            )

        transformed_path = os.path.join(out_dir, "powt_transformed.csv")
        power_path = os.path.join(out_dir, "powt_power.csv")
        raw_skew_path = os.path.join(out_dir, "powt_raw_skew.csv")
        powt_skew_path = os.path.join(out_dir, "powt_transformed_skew.csv")

        if not os.path.exists(transformed_path):
            raise RuntimeError("dplR POWT did not produce transformed output.")

        transformed = pd.read_csv(transformed_path, index_col=0)
        transformed.index = transformed.index.astype(int)

        if os.path.exists(power_path):
            power_df = pd.read_csv(power_path)
            if "series" in power_df.columns and "power" in power_df.columns:
                power = pd.Series(
                    pd.to_numeric(power_df["power"], errors="coerce").to_numpy(dtype=float),
                    index=power_df["series"].astype(str).tolist(),
                    dtype=float,
                    name="power",
                )
            else:
                power = pd.Series(dtype=float, name="power")
        else:
            power = pd.Series(dtype=float, name="power")

        def _read_skew_series(path: str) -> pd.Series:
            if not os.path.exists(path):
                return pd.Series(dtype=float)
            df = pd.read_csv(path)
            if "series" not in df.columns or "skew" not in df.columns:
                return pd.Series(dtype=float)
            return pd.Series(
                pd.to_numeric(df["skew"], errors="coerce").to_numpy(dtype=float),
                index=df["series"].astype(str).tolist(),
                dtype=float,
            )

        raw_skew = _read_skew_series(raw_skew_path)
        powt_skew = _read_skew_series(powt_skew_path)
        if raw_skew.empty or not bool(np.isfinite(pd.to_numeric(raw_skew, errors="coerce").to_numpy(dtype=float)).any()):
            raw_skew = _series_skewness(rwl2)
        if powt_skew.empty or not bool(np.isfinite(pd.to_numeric(powt_skew, errors="coerce").to_numpy(dtype=float)).any()):
            powt_skew = _series_skewness(transformed)

        return PowtPreviewResult(
            transformed=transformed,
            power=power,
            raw_skew=raw_skew,
            powt_skew=powt_skew,
            method=str(config.powt_method),
            rescale=bool(config.powt_rescale),
        )

def _fill_internal_nas_as_zero_df(df: pd. DataFrame) -> pd. DataFrame:
    """Replace NA values inside the active span of each series with 0.

    dplR errors when NA values occur between the first and last non-NA value of a
    series ("NA's are not allowed in the middle of the series"). In typical ring-width
    workflows, internal gaps are encoded as 0 (missing ring) rather than NA, and dplR
    then recodes 0 -> 0.001.

    This function leaves leading/trailing NA untouched, but converts internal NA to 0.
    """
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if s.isna().all():
            continue
        first = s.first_valid_index()
        last = s.last_valid_index()
        if first is None or last is None:
            continue
        span = s.loc[first:last]
        if span.isna().any():
            out.loc[first:last, col] = span.fillna(0.0)
    return out


def _dplr_compute_methods(
    rwl: pd. DataFrame,
    method_keys: list[str],
    config: DplrCompatConfig,
    po: Optional[pd. DataFrame] = None,
    verbose: bool = False,
    progress_label: str = "dplR detrending",
) -> tuple[Dict[str, pd. DataFrame], Dict[str, pd. Series], Dict[str, Any]]:
    """Compute dplR-backed outputs via Rscript + dplR.

    Returns:
        (rwi_by_method, chron_std_by_method, extras)
    """
    rscript_cmd = _ensure_rscript(config)
    rwl2 = _fill_internal_nas_as_zero_df(rwl)
    rwl2 = _recode_zeros_df(rwl2, config.recode_zeros)
    with tempfile.TemporaryDirectory(prefix="detrend_dplr_") as td:
        td_path = str(td)
        script_path = os.path.join(td_path, "validate_dplr.R")
        _write_validate_dplr_r(script_path)
        in_path = os.path.join(td_path, "input_rwl.csv")
        _write_rwl_csv_for_r(rwl2, in_path)
        out_dir = os.path.join(td_path, "out")
        os.makedirs(out_dir, exist_ok=True)

        po_arg = "NA"
        if po is not None:
            po_path = os.path.join(td_path, "po.csv")
            po.to_csv(po_path, index=False)
            po_arg = po_path

        n_workers = _choose_r_parallel_workers(len(method_keys))

        args = [
            rscript_cmd,
            script_path,
            in_path,
            po_arg,
            out_dir,
            ",".join(method_keys),
            f"pos_slope={'TRUE' if config.pos_slope else 'FALSE'}",
            f"constrain_nls={config.constrain_nls}",
            f"difference={'TRUE' if config.difference else 'FALSE'}",
            f"recode_zeros={'TRUE' if config.recode_zeros else 'FALSE'}",
            f"auto_install={'TRUE' if config.auto_install_r_deps else 'FALSE'}",
            f"cran_repo={config.cran_repo}",
            f"parallel_workers={n_workers}",
            f"powt_enabled={'TRUE' if config.powt_enabled else 'FALSE'}",
            f"powt_method={config.powt_method}",
            f"powt_rescale={'TRUE' if config.powt_rescale else 'FALSE'}",
        ]
        method_label = ", ".join(str(k) for k in list(method_keys))
        _emit_runtime_progress(
            f"{progress_label}: requested methods: {method_label}"
        )
        if verbose:
            _print_progress(
                f"{progress_label}: launching R for {len(method_keys)} method(s), "
                f"workers={n_workers}, queued={max(0, len(method_keys) - n_workers)}"
            )
        proc = _run_subprocess_capture(
            args,
            verbose=verbose,
            prefix=f"{progress_label}: ",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "dplR execution failed.\n"
                f"Command: {' '.join(args)}\n"
                f"STDERR:\n{proc.stderr}\n"
                f"STDOUT:\n{proc.stdout}\n"
            )

        rwi_by: Dict[str, pd. DataFrame] = {}
        chron_std_by: Dict[str, pd. Series] = {}
        extras: Dict[str, Any] = {}
        ver_path = os.path.join(out_dir, "dplr_version.txt")
        if os.path.exists(ver_path):
            extras["dplr_version"] = Path(ver_path).read_text(encoding="utf-8").strip()

 
        errors: Dict[str, str] = {}
        series_exclusions: Dict[str, Dict[str, str]] = {}

        for k in method_keys:
            err_path = os.path.join(out_dir, f"dplr_error_{k}.txt")
            if os.path.exists(err_path):
                try:
                    errors[k] = Path(err_path).read_text(encoding="utf-8").strip()
                except Exception:
                    errors[k] = "Error text could not be read."

            rwi_path = os.path.join(out_dir, f"dplr_rwi_{k}.csv")
            chron_path = os.path.join(out_dir, f"dplr_chron_{k}.csv")
            if os.path.exists(rwi_path):
                df = pd.read_csv(rwi_path, index_col=0)
                df.index = df.index.astype(int)
                rwi_by[k] = df
            if os.path.exists(chron_path):
                crn = pd.read_csv(chron_path, index_col=0)
                crn.index = crn.index.astype(int)
                if "std" in crn.columns:
                    chron_std_by[k] = crn["std"].astype(float)
                else:
                    # Fall back to the first numeric column if "std" missing.
                    chron_std_by[k] = crn.iloc[:, 0].astype(float)

            rc_path = os.path.join(out_dir, f"dplr_rc_curve_{k}.csv")
            if os.path.exists(rc_path):
                extras[f"rc_curve_{k}"] = pd.read_csv(rc_path)

            excl_path = os.path.join(out_dir, f"dplr_series_exclusions_{k}.tsv")
            excl_map = _read_series_exclusion_tsv(excl_path)
            if excl_map:
                series_exclusions[k] = excl_map


        if errors:
            extras["dplr_errors"] = errors
        if series_exclusions:
            extras["dplr_series_exclusions"] = series_exclusions

        return rwi_by, chron_std_by, extras

def _choose_excel_engine() -> Optional[str]:
    """Return an available Excel writer engine or None if unavailable."""
    if importlib.util.find_spec("xlsxwriter") is not None:
        return "xlsxwriter"
    if importlib.util.find_spec("openpyxl") is not None:
        return "openpyxl"
    return None



def _pip_install(packages: list[str]) -> tuple[bool, str]:
    """Install packages via pip for the running Python interpreter.

    Returns:
        (ok, combined_output)
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        *packages,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        return proc.returncode == 0, (proc.stdout or "")
    except Exception as exc:
        return False, str(exc)


def _ensure_excel_engine() -> Optional[str]:
    """Ensure an Excel writer engine is available, attempting pip install if needed."""
    engine = _choose_excel_engine()
    if engine is not None:
        return engine
    for pkg in ("xlsxwriter", "openpyxl"):
        ok, _out = _pip_install([pkg])
        if ok:
            engine = _choose_excel_engine()
            if engine is not None:
                return engine
    return None


def _module_available(module_name: str) -> bool:
    """Return True if a Python module can be imported in the current interpreter."""
    try:
        return importlib.util.find_spec(str(module_name)) is not None
    except Exception:
        return False


def _ensure_python_package(package_name: str, import_name: Optional[str] = None) -> tuple[bool, str]:
    """Ensure a Python package is available in the current interpreter."""
    mod_name = str(import_name or package_name)
    if _module_available(mod_name):
        return True, ""
    ok, output = _pip_install([str(package_name)])
    if ok and _module_available(mod_name):
        return True, output
    return False, output


def _iter_ringclim_launch_candidates() -> list[tuple[str, str]]:
    """Yield possible RingClim launch targets near the current app/script."""
    dirs: list[str] = []

    def _add_dir(path: Optional[str]) -> None:
        if not path:
            return
        try:
            norm = os.path.abspath(str(path))
        except Exception:
            return
        if norm not in dirs:
            dirs.append(norm)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    _add_dir(script_dir)

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    _add_dir(exe_dir)

    if bool(getattr(sys, "frozen", False)):
        contents_dir = os.path.dirname(exe_dir)
        app_dir = os.path.dirname(contents_dir)
        dist_dir = os.path.dirname(app_dir)
        _add_dir(contents_dir)
        _add_dir(app_dir)
        _add_dir(dist_dir)
        _add_dir(os.path.join(contents_dir, "Resources"))

    candidates: list[tuple[str, str]] = []
    for base in dirs:
        candidates.extend(
            [
                # Launch the bundle through Launch Services on macOS. Starting
                # Contents/MacOS/ringclim directly can leave Qt modal dialogs
                # behind the calling Tk application.
                (os.path.join(base, "ringclim.app"), "app"),
                (os.path.join(base, "ringclim.py"), "script"),
                (os.path.join(base, "ringclim"), "executable"),
                (os.path.join(base, "ringclim.exe"), "executable"),
                (os.path.join(base, "ringclim", "ringclim.py"), "script"),
                (os.path.join(base, "ringclim", "ringclim.exe"), "executable"),
                (os.path.join(base, "dist", "ringclim", "ringclim.py"), "script"),
                (os.path.join(base, "dist", "ringclim", "ringclim.exe"), "executable"),
        
                # onedir sibling folder
                (os.path.join(base, "ringclim", "ringclim.py"), "script"),
                (os.path.join(base, "ringclim", "ringclim.exe"), "executable"),
        
                # if launched from project root
                (os.path.join(base, "dist", "ringclim", "ringclim.py"), "script"),
                (os.path.join(base, "dist", "ringclim", "ringclim.exe"), "executable"),
        
                (
                    os.path.join(base, "ringclim.app", "Contents", "MacOS", "ringclim"),
                    "executable",
                ),
            ]
        )
    return candidates


def _find_ringclim_launch_target() -> tuple[Optional[str], str]:
    """Return the best matching RingClim target and its launch mode."""
    for candidate_path, launch_mode in _iter_ringclim_launch_candidates():
        if os.path.isfile(candidate_path) or (
            launch_mode == "app" and os.path.isdir(candidate_path)
        ):
            return os.path.abspath(candidate_path), launch_mode
    return None, ""


def _ensure_ringclim_runtime(parent: Optional[tk.Misc] = None) -> bool:
    """Ensure RingClim runtime dependencies are available before launch."""
    python_exe = sys.executable
    ok_probe, probe_detail = _probe_ringclim_qt_runtime(python_exe)
    if ok_probe:
        return True

    install_now = messagebox.askyesno(
        "PySide6 is required",
        "RingClim requires working PySide6 / shiboken6 Qt bindings, but they are not "
        "available in the current Python environment.\n\n"
        "Install PySide6 and shiboken6 now?",
        parent=parent,
    )
    if not install_now:
        return False
    ok1, output1 = _ensure_python_package("PySide6", import_name="PySide6")
    ok2, output2 = _ensure_python_package("shiboken6", import_name="shiboken6")
    ok = bool(ok1 and ok2)
    output = "\n".join(part for part in ((output1 or "").strip(), (output2 or "").strip()) if part)
    if not ok:
        detail = (output or "").strip()
        if detail:
            detail = "\n\n" + detail
        messagebox.showerror(
            "PySide6 installation failed",
            "Failed to install the Python packages 'PySide6' and 'shiboken6' required by RingClim." + detail,
            parent=parent,
        )
        return False

    ok_probe, probe_detail = _probe_ringclim_qt_runtime(python_exe)
    if not ok_probe:
        messagebox.showerror(
            "PySide6 runtime error",
            "PySide6 was installed, but the QtWidgets runtime still cannot be imported.\n\n"
            f"{probe_detail}",
            parent=parent,
        )
        return False
    return True


def _choose_r_parallel_workers(n_tasks: int) -> int:
    """Choose a conservative cross-platform worker count for R parallel execution.

    Notes:
        - Extra methods are automatically queued by parLapply when workers < tasks.
        - The default is intentionally conservative for laptops.
        - DETREND_R_PARALLEL_WORKERS forces an exact worker count.
        - DETREND_R_PARALLEL_MAX applies an upper cap without forcing the value.
    """
    n_tasks2 = int(max(1, n_tasks))

    env_raw = str(os.getenv("DETREND_R_PARALLEL_WORKERS", "") or "").strip()
    if env_raw:
        try:
            env_val = int(env_raw)
        except Exception:
            env_val = 0
        if env_val > 0:
            return int(max(1, min(n_tasks2, env_val)))

    env_cap_raw = str(os.getenv("DETREND_R_PARALLEL_MAX", "") or "").strip()
    try:
        env_cap = int(env_cap_raw) if env_cap_raw else 0
    except Exception:
        env_cap = 0

    cpu = int(os.cpu_count() or 1)
    if cpu <= 1 or n_tasks2 <= 1:
        return 1

    reserve = 2 if cpu >= 6 else 1
    by_headroom = max(1, cpu - reserve)
    by_fraction = max(1, cpu // 2)
    default_cap = 4
    workers = min(n_tasks2, by_headroom, by_fraction, default_cap)

    if env_cap > 0:
        workers = min(workers, env_cap)

    return int(max(1, workers))


def _apply_arstan_missing_codes(df: pd. DataFrame) -> pd. DataFrame:
    """Apply ARSTAN-style missing/sentinel codes to a numeric DataFrame.
    Notes:
        - Tucson formats often encode missing values as 999, 9990, 9999 (and negatives).
        - Treat these as NaN across ALL loaders to avoid artificial spikes during analysis.
        - Non-positive values are also treated as missing, consistent with typical TRW handling.
    """
    missing_abs = np.array([999.0, 9990.0, 9999.0], dtype=float)
    out = df.copy()
    for col in out.columns:
        v = pd.to_numeric(out[col], errors="coerce").to_numpy(dtype=float, copy=True)
        is_missing = np.isin(np.abs(v), missing_abs) | (v == -9999.0) | (v == -999.0) | (v == -9990.0)
        v[is_missing] = np.nan
        v[v <= 0] = np.nan
        out[col] = v
    return out
def _ensure_unique_sorted_year_index(df: pd. DataFrame) -> pd. DataFrame:
    """Ensure year index is integer, unique, and sorted (CSV files may contain duplicates)."""
    df = df.copy()
    df.index = pd.to_numeric(df.index, errors="coerce")
    df = df.loc[~df.index.isna()]
    df.index = df.index.astype(int)
    if not df.index.is_unique:
        # Aggregate duplicate years using mean across duplicates.
        df = df.groupby(df.index).mean(numeric_only=True)
    return df.sort_index()
def _ask_export_mode(parent: tk. Misc) -> str | None:
    """Ask the user which export mode to use.
    Returns:
        - "excel_only": write only one Excel file (plus Markdown report).
        - "csv_excel": write CSV files + Excel file (plus Markdown report).
        - None: user cancelled.
    """
    dialog = tk. Toplevel(parent.winfo_toplevel())
    dialog.title("Export mode")
    dialog.resizable(False, False)
    dialog.transient(parent.winfo_toplevel())
    choice: dict[str, str | None] = {"value": None}
    frm = ttk. Frame(dialog, padding=12)
    frm.pack(fill="both", expand=True)
    ttk. Label(frm, text="Choose export format:", font=("Arial", 11, "bold")).pack(
        anchor="w", pady=(0, 8)
    )
    desc = (
        "Excel only: one .xlsx file (chronologies + RWI for all methods)\n"
        "CSV + Excel: per-method CSV files plus one .xlsx file"
    )
    ttk. Label(frm, text=desc, justify="left", wraplength=540).pack(
        anchor="w", fill="x", pady=(0, 14)
    )
    btns = ttk. Frame(frm)
    btns.pack(fill="x", expand=True)
    for column in range(3):
        btns.columnconfigure(column, weight=1, uniform="export-mode")
    def set_choice(val: str | None) -> None:
        choice["value"] = val
        dialog.destroy()
    btn_excel_only = ttk. Button(btns, text="Excel only", command=lambda: set_choice("excel_only"))
    btn_excel_only.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=3)
    btn_csv_excel = ttk. Button(btns, text="CSV + Excel", command=lambda: set_choice("csv_excel"))
    btn_csv_excel.grid(row=0, column=1, sticky="ew", padx=3, ipady=3)
    btn_cancel = ttk. Button(btns, text="Cancel", command=lambda: set_choice(None))
    btn_cancel.grid(row=0, column=2, sticky="ew", padx=(6, 0), ipady=3)
    _attach_hover_tooltip(
        btn_excel_only,
        "Export one Excel workbook plus the Markdown report without writing per-method CSV files."
    )
    _attach_hover_tooltip(
        btn_csv_excel,
        "Export per-method CSV files, one Excel workbook, and the Markdown report."
    )
    _attach_hover_tooltip(
        btn_cancel,
        "Close this dialog without starting export."
    )
    dialog.protocol("WM_DELETE_WINDOW", lambda: set_choice(None))
    # On macOS a transient Toplevel with a grab set before it is mapped can stay
    # behind a bundled Tk app. Size and center it before requesting focus.
    dialog.update_idletasks()
    width = max(580, dialog.winfo_reqwidth() + 24)
    height = max(190, dialog.winfo_reqheight() + 20)
    try:
        parent.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = max(parent.winfo_width(), 1), max(parent.winfo_height(), 1)
        x, y = px + (pw - width) // 2, py + (ph - height) // 2
        x = max(0, min(x, dialog.winfo_screenwidth() - width))
        y = max(0, min(y, dialog.winfo_screenheight() - height))
    except Exception:
        x = y = 0
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    dialog.deiconify()
    dialog.lift(parent.winfo_toplevel())
    try:
        dialog.attributes("-topmost", True)
        def clear_temporary_topmost() -> None:
            try:
                if dialog.winfo_exists():
                    dialog.attributes("-topmost", False)
            except tk.TclError:
                pass
        dialog.after(700, clear_temporary_topmost)
    except tk.TclError:
        pass
    try:
        dialog.wait_visibility()
    except tk.TclError:
        pass
    try:
        dialog.grab_set()
        dialog.focus_force()
        btn_excel_only.focus_set()
    except tk.TclError:
        pass
    dialog.wait_window()
    return choice["value"]
# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_rwl_csv(path: str) -> pd. DataFrame:
    """Load ring-width-like data from CSV.
    Assumes:
        - one column with years (named 'year' or the first column),
        - all other columns are series.
    Args:
        path: CSV path.
    Returns:
        DataFrame indexed by year (int), columns are series.
    """
    df = pd.read_csv(path)
    year_col = None
    for col in df.columns:
        if col.lower() == "year":
            year_col = col
            break
    if year_col is None:
        year_col = df.columns[0]
    df = df.set_index(year_col)
    df = _ensure_unique_sorted_year_index(df)
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = _apply_arstan_missing_codes(df)
    return _ensure_unique_sorted_year_index(df)
# Smart reading helper (handles a few encodings and normalizes newlines).
def smart_read_lines(path: str) -> list[str]:
    """Read text file using several encodings and normalize newlines."""
    encodings = ("utf-8", "utf-8-sig", "cp1251", "latin-1")
    text = ""
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
                break
        except Exception:
            continue
    else:
        # Fallback: ignore errors
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n")
def load_rwl_tucson(path: str) -> pd. DataFrame:
    """Load ring widths from a classic fixed-width Tucson RWL file, in millimetres.

    Layout: cols 1-8 series ID, cols 9-12 year of the first value on the line,
    then 6-character integer fields (10 per decade line).

    Units (as in dplR read.tucson): the end-of-series marker gives the precision
    of each series separately - 999 -> 0.01 mm, -9999 -> 0.001 mm. Series without
    a marker are read as 0.01 mm. End markers are not data; zero, negative and
    (for 0.01 mm series) 9990/9999 values are treated as missing.
    """
    lines = [ln for ln in smart_read_lines(path) if ln.strip()]
    raw: dict[str, dict[int, int]] = {}
    precision: dict[str, float] = {}
    rows: list[tuple[str, int, list[Optional[int]]]] = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if len(line) < 12:
            continue
        series_id = line[0:8].strip()
        year_str = line[8:12].strip()
        if not series_id or not year_str or not year_str.lstrip("-").isdigit():
            continue  # header, comment or non-data line
        try:
            year = int(year_str)
        except ValueError:
            continue
        values: list[Optional[int]] = []
        meas_part = line[12:]
        for i in range(0, len(meas_part), 6):
            chunk = meas_part[i : i + 6].strip()
            if not chunk:
                values.append(None)
                continue
            try:
                values.append(int(chunk))
            except ValueError:
                try:
                    values.append(int(float(chunk.replace(",", "."))))
                except ValueError:
                    values.append(None)
        # Drop empty trailing fields
        while values and values[-1] is None:
            values.pop()
        rows.append((series_id, year, values))
        if -9999 in values:
            precision[series_id] = 0.001
    if not rows:
        raise ValueError("No Tucson-format data lines detected in file.")
    # Second pass: drop the end markers (precision is known for the whole series now)
    for series_id, year, values in rows:
        if precision.get(series_id) == 0.001:
            if -9999 in values:
                values = values[: values.index(-9999)]
        elif values and values[-1] == 999:
            values = values[:-1]
        dest = raw.setdefault(series_id, {})
        for k, v in enumerate(values):
            if v is not None:
                dest[year + k] = v

    series_data: dict[str, dict[int, float]] = {}
    for sid, yvals in raw.items():
        unit = precision.get(sid, 0.01)
        out: dict[int, float] = {}
        for y, v in yvals.items():
            if v <= 0 or (unit == 0.01 and v in (999, 9990, 9999)):
                out[y] = np.nan
            else:
                out[y] = round(float(v) * unit, 4)
        series_data[sid] = out

    all_years = sorted({y for d in series_data.values() for y in d.keys()})
    all_series = sorted(series_data.keys())
    data = np.full((len(all_years), len(all_series)), np.nan, dtype=float)
    year_index = {y: i for i, y in enumerate(all_years)}
    series_index = {s: j for j, s in enumerate(all_series)}
    for sid, yvals in series_data.items():
        j = series_index[sid]
        for y, val in yvals.items():
            data[year_index[y], j] = val
    df = pd. DataFrame(data, index=all_years, columns=all_series)
    df.index.name = "year"
    df.attrs["units"] = "mm"
    return df


def rwl_raw_units_to_mm(df: pd. DataFrame, path: Optional[str] = None) -> pd. DataFrame:
    """Convert a ring-width table still in Tucson integer units to mm (old projects).

    Tables whose median width is below 20 are taken as mm already and returned
    unchanged. Otherwise the per-series precision is read from the RWL end
    markers when the file exists, or guessed from the size of the values.
    """
    if df is None or df.empty:
        return df
    vals = pd.to_numeric(pd.Series(df.to_numpy(dtype=float).ravel()), errors="coerce").dropna()
    vals = vals[vals > 0]
    if vals.empty or float(vals.median()) < 20.0:
        return df
    prec: dict[str, float] = {}
    if path and os.path.isfile(path) and _pith is not None:
        try:
            prec = _pith.tucson_precision_mm(path)
        except Exception:
            prec = {}
    out = df.copy()
    for col in out.columns:
        ser = pd.to_numeric(out[col], errors="coerce")
        unit = prec.get(str(col)[:8].strip())
        if unit is None:
            med = float(ser[ser > 0].median()) if (ser > 0).any() else 0.0
            unit = 0.001 if med >= 400 else 0.01
        out[col] = (ser * unit).round(4)
    out.attrs["units"] = "mm"
    return out


def load_rwl_any(path: str, fmt_hint: str = "auto") -> pd. DataFrame:
    """Load ring-width data from either CSV or Tucson (.rwl / text) format.
    Logic:
        - If extension is .csv (case-insensitive) -> CSV loader.
        - Otherwise -> try Tucson loader first; if it fails, fall back to CSV.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return load_rwl_csv(path)
    # Try Tucson first for non-CSV files
    try:
        return load_rwl_tucson(path)
    except Exception:
        # Fallback: attempt CSV
        return load_rwl_csv(path)
def load_climate_csv(path: str, column: Optional[str] = None) -> pd. Series:
    """Load climate/target series from CSV.
    Args:
        path: CSV path.
        column: name of climate column; if None, first non-year column is used.
    Returns:
        Series indexed by year (int).
    """
    df = pd.read_csv(path)
    year_col = None
    for col in df.columns:
        if col.lower() == "year":
            year_col = col
            break
    if year_col is None:
        year_col = df.columns[0]
    df = df.set_index(year_col)
    df.index = df.index.astype(int)
    if column is None:
        candidates = [c for c in df.columns if c != year_col]
        if not candidates:
            raise ValueError("No climate column found in CSV.")
        column = candidates[0]
    ser = pd.to_numeric(df[column], errors="coerce")
    return ser.sort_index()
# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def ensure_dir(path: str) -> None:
    """Ensure directory for a file path exists."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
# ---------------------------------------------------------------------------
# Basic diagnostics: ages, depth, EPS, etc.
# ---------------------------------------------------------------------------
def compute_series_age(series: pd. Series) -> int:
    """Return series age as number of non-NaN rings."""
    return int(series.dropna().shape[0])
def compute_ages(rwl: pd. DataFrame, po: Optional[pd.DataFrame] = None) -> pd. Series:
    """Return ages for each series (columns).

    Without ``po`` this is the number of measured rings. With a pith-offset
    table it is the cambial age of the outermost ring
    (measured rings + pith.offset - 1).
    """
    ages = rwl.apply(compute_series_age, axis=0)
    if po is None or getattr(po, "empty", True):
        return ages
    po_vec = _get_po_vector(rwl, po=po)
    return pd.Series(
        {col: int(ages[col]) + int(po_vec.get(str(col), 1)) - 1 for col in ages.index},
        dtype=int,
    )
def compute_sample_depth(rwl: pd. DataFrame) -> pd. Series:
    """Return sample depth (number of non-NaN series) per year."""
    return rwl.notna().sum(axis=1)


def compute_tree_depth(
    rwl: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
) -> pd.Series:
    """Return per-year tree depth when tree/core ids are available.

    Notes:
        - If ids is missing or invalid, this falls back to series depth.
        - Each tree is counted once per year if any of its cores has data.
    """
    if ids is None or not isinstance(ids, pd.DataFrame) or "tree" not in ids.columns:
        return compute_sample_depth(rwl)
    if rwl is None or rwl.empty:
        return pd.Series(dtype=int)

    try:
        ids2 = ids.copy()
        rownames_ids = ids2.index.astype(str)
        colnames_rwl = pd.Index(rwl.columns).astype(str)

        if len(rownames_ids) > 0 and all(col in set(rownames_ids) for col in colnames_rwl):
            ids2 = ids2.loc[colnames_rwl, ["tree"]].copy()
        elif int(ids2.shape[0]) == int(rwl.shape[1]):
            ids2 = ids2.iloc[: int(rwl.shape[1])][["tree"]].copy()
            ids2.index = rwl.columns
        else:
            return compute_sample_depth(rwl)

        ids2["tree"] = pd.to_numeric(ids2["tree"], errors="coerce")
        if ids2["tree"].isna().all():
            return compute_sample_depth(rwl)

        depth = pd.Series(0, index=rwl.index, dtype=int)
        for _, col_labels in ids2.groupby("tree").groups.items():
            block = rwl.loc[:, list(col_labels)]
            depth = depth.add(block.notna().any(axis=1).astype(int), fill_value=0)
        return pd.to_numeric(depth, errors="coerce").fillna(0).astype(int)
    except Exception:
        return compute_sample_depth(rwl)


def compute_effective_depth(
    rwl: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
    count_mode: str = "series",
) -> pd.Series:
    """Return the depth series used for the detrending start threshold."""
    mode = str(count_mode or "series").strip().lower()
    if mode == "trees":
        return compute_tree_depth(rwl, ids=ids)
    return compute_sample_depth(rwl)


def find_minimum_depth_start_year(
    rwl: pd.DataFrame,
    min_series: int = 1,
    ids: Optional[pd.DataFrame] = None,
    count_mode: str = "series",
) -> Optional[int]:
    """Return the first year where at least min_series series/trees are present."""
    if rwl is None or rwl.empty:
        return None
    depth = compute_effective_depth(rwl, ids=ids, count_mode=count_mode)
    if depth.empty:
        return None
    threshold = int(max(1, min_series))
    mask = pd.to_numeric(depth, errors="coerce").fillna(0).astype(float) >= float(threshold)
    if not bool(mask.any()):
        return None
    years = depth.index[mask.to_numpy(dtype=bool)]
    if len(years) == 0:
        return None
    return int(years[0])


def trim_rwl_to_minimum_depth_start(
    rwl: pd.DataFrame,
    min_series: int = 1,
    ids: Optional[pd.DataFrame] = None,
    count_mode: str = "series",
) -> tuple[pd.DataFrame, pd.Series, Optional[int]]:
    """Trim the input so detrending starts at the first year meeting the depth threshold."""
    depth = compute_effective_depth(rwl, ids=ids, count_mode=count_mode)
    start_year = find_minimum_depth_start_year(
        rwl,
        min_series=min_series,
        ids=ids,
        count_mode=count_mode,
    )
    if start_year is None:
        return rwl.iloc[0:0].copy(), depth, None
    out = rwl.loc[rwl.index >= int(start_year)].copy()
    return out, depth, int(start_year)


DEFAULT_RADIUS_SUFFIX_VARIANT_LINES = [
    "A/B",
    "R1/R2",
    "N/S/E/W",
    "a/b",
    "01/02",
]


def _default_radius_suffix_variants_text() -> str:
    """Return default radius/core suffix variants as editable text."""
    return "\n".join(DEFAULT_RADIUS_SUFFIX_VARIANT_LINES)


DEFAULT_SERIES_GROUP_PATTERN_LINES = [
    "AAAABBC",
    "BBC",
]


def _default_series_group_patterns_text() -> str:
    """Return default editable Series ID grouping patterns."""
    return "\n".join(DEFAULT_SERIES_GROUP_PATTERN_LINES)


def _parse_manual_tree_prefixes(text: str) -> list[str]:
    """Parse user-defined tree/site prefixes from a comma- or line-separated text block."""
    out: list[str] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        for token in re.split(r"[,;]+", str(raw_line or "")):
            key = _normalize_series_id_token(token)
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _parse_radius_suffix_variants(text: str) -> list[str]:
    """Parse editable radius/core suffix variants from a multi-line text block."""
    out: list[str] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        key = line.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _parse_series_group_patterns(text: str) -> list[str]:
    """Parse editable Series ID grouping patterns.

    Notes:
        - Only A/B/C markers are kept.
        - A = alphabetic prefix part.
        - B = numeric tree part.
        - C = trailing core/radius/series suffix part.
        - Commas, semicolons, spaces, and other separators are ignored.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        for token in re.split(r"[,;]+", str(raw_line or "")):
            key = re.sub(r"[^ABC]", "", str(token or "").upper())
            if not key:
                continue
            if "C" not in key:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _split_series_id_by_group_pattern(
    series_id: str,
    pattern: str,
) -> Optional[tuple[str, str]]:
    """Split one Series ID into tree/core parts using an A/B/C pattern.

    Notes:
        - The normalized Series ID is used, so spaces and separators are ignored.
        - The pattern is matched against the rightmost part of the Series ID.
        - Any unmatched leading prefix is treated as a site/locality prefix and
          is ignored for tree grouping.
        - A/B accept both conventions:
          * A = letters, B = digits
          * A = digits, B = letters
        - C positions must be trailing alphanumeric suffix characters.
        - All A/B positions are merged into the tree label.
        - All trailing C positions are merged into the core/radius label.
    """
    sid = _normalize_series_id_token(series_id)
    pat = re.sub(r"[^ABC]", "", str(pattern or "").upper())
    if not sid or not pat or len(sid) < len(pat):
        return None
    if "C" not in pat:
        return None

    first_c = pat.find("C")
    if first_c < 0:
        return None
    if any(ch == "A" for ch in pat[first_c:]) or any(ch == "B" for ch in pat[first_c:]):
        return None

    sid_tail = sid[-len(pat):]
    sid_lead = sid[: len(sid) - len(pat)]

    def _try_match(a_is_alpha: bool) -> Optional[tuple[str, str]]:
        tree_chars: list[str] = []
        core_chars: list[str] = []
        for ch, token in zip(sid_tail, pat):
            if token == "A":
                if a_is_alpha:
                    if not ch.isalpha():
                        return None
                else:
                    if not ch.isdigit():
                        return None
                tree_chars.append(ch)
            elif token == "B":
                if a_is_alpha:
                    if not ch.isdigit():
                        return None
                else:
                    if not ch.isalpha():
                        return None
                tree_chars.append(ch)
            elif token == "C":
                if not ch.isalnum():
                    return None
                core_chars.append(ch)
            else:
                return None
        tree_label = "".join(tree_chars).strip()
        core_label = "".join(core_chars).strip()
        if not tree_label or not core_label:
            return None
        # Never split one number: "K101" is tree 101, not tree 10 + radius 1.
        # Allowed only when the whole ID has exactly the pattern's shape (e.g. "031").
        if sid_lead[-1:].isdigit() or (tree_label[-1:].isdigit() and core_label[:1].isdigit()):
            if sid_lead or (tree_label[-1:].isdigit() and core_label[:1].isdigit() and len(sid) != len(pat)):
                return None
        # A numeric radius needs a tree number: "KC4" is tree KC4, not tree "KC" + radius 4.
        if core_label.isdigit() and not any(ch.isdigit() for ch in tree_label + sid_lead):
            return None
        # Keep the leading part of the ID so that K10x and AM10x never merge.
        return (sid_lead + tree_label, core_label)

    res = _try_match(a_is_alpha=True)
    if res is not None:
        return res
    return _try_match(a_is_alpha=False)


def _build_radius_suffix_guess_patterns(
    suffix_variant_lines: Optional[list[str]] = None,
) -> list[tuple[str, float]]:
    """Build ordered regex patterns for radius/core suffix detection."""
    lines = list(suffix_variant_lines or [])
    patterns: list[tuple[str, float]] = []
    seen_regex: set[str] = set()

    def _add(expr: str, confidence: float) -> None:
        expr2 = str(expr or "").strip()
        if not expr2 or expr2 in seen_regex:
            return
        seen_regex.add(expr2)
        patterns.append((expr2, float(confidence)))

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.lower().startswith("regex:"):
            expr = str(line.split(":", 1)[1] or "").strip()
            if expr:
                _add(expr, 0.80)
            continue

        parts = [
            _normalize_series_id_token(tok)
            for tok in re.split(r"[,\s/|;]+", line)
            if str(tok or "").strip()
        ]
        tokens = [tok for tok in parts if tok]
        if len(tokens) < 2:
            continue
        uniq = list(dict.fromkeys(tokens))

        if all(re.fullmatch(r"[A-Z]", tok) for tok in uniq):
            chars = "".join(uniq)
            _add(rf"^(.*?)([{re.escape(chars)}])$", 0.84)
            continue

        if all(re.fullmatch(r"[NSEW]{1,2}", tok) for tok in uniq):
            alt = "|".join(re.escape(tok) for tok in sorted(uniq, key=len, reverse=True))
            _add(rf"^(.*?)({alt})$", 0.95)
            continue

        if all(re.fullmatch(r"[A-Z]+\d{1,2}", tok) for tok in uniq):
            prefixes = {
                re.match(r"([A-Z]+)\d{1,2}", tok).group(1)
                for tok in uniq
                if re.match(r"([A-Z]+)\d{1,2}", tok)
            }
            if len(prefixes) == 1:
                prefix = next(iter(prefixes))
                max_digits = max(
                    len(re.sub(r"^[A-Z]+", "", tok))
                    for tok in uniq
                )
                _add(rf"^(.*?)(?:({re.escape(prefix)}\d{{1,{int(max(1, max_digits))}}}))$", 0.98)
                continue

        if all(re.fullmatch(r"\d+", tok) for tok in uniq):
            alt = "|".join(re.escape(tok) for tok in sorted(uniq, key=len, reverse=True))
            _add(rf"^(.*?)({alt})$", 0.72)
            continue

        alt = "|".join(re.escape(tok) for tok in sorted(uniq, key=len, reverse=True))
        _add(rf"^(.*?)({alt})$", 0.78)

    _add(r"^(.*?)(R\d{1,2})$", 0.98)
    _add(r"^(.*?)([NSEW]{1,2})$", 0.95)
    _add(r"^(.*?)([A-Z]{1,2}\d{1,2})$", 0.90)
    _add(r"^(.*?)([A-Z])$", 0.82)
    return patterns


def _normalize_series_id_token(text: str) -> str:
    """Normalize one series identifier token for grouping heuristics."""
    raw = str(text or "").strip().upper()
    raw = re.sub(r"[\s\-_./\\,:;|+(){}\[\]<>]+", "", raw)
    return raw


def _common_series_site_prefix(series_names: list[str]) -> str:
    """Return a shared site/locality prefix that should be ignored for tree grouping."""
    vals = [
        _normalize_series_id_token(x)
        for x in [str(v) for v in list(series_names)]
        if str(x).strip()
    ]
    if len(vals) < 2:
        return ""
    prefix = os.path.commonprefix(vals)
    if not prefix:
        return ""
    prefix = re.sub(r"\d+$", "", str(prefix))
    if not prefix:
        return ""
    if len(prefix) >= min(len(v) for v in vals):
        return ""
    return str(prefix)


def _choose_common_site_prefix(
    series_names: list[str],
    manual_prefixes: Optional[list[str]] = None,
) -> str:
    """Choose a site/locality prefix, preferring user-defined prefixes when possible."""
    vals = [
        _normalize_series_id_token(x)
        for x in [str(v) for v in list(series_names)]
        if str(x).strip()
    ]
    if vals and manual_prefixes:
        manual_norm = [
            _normalize_series_id_token(x)
            for x in list(manual_prefixes)
            if _normalize_series_id_token(x)
        ]
        manual_norm.sort(key=len, reverse=True)
        for pref in manual_norm:
            if len(pref) >= min(len(v) for v in vals):
                continue
            if any(v.startswith(pref) for v in vals):
                return str(pref)
    return _common_series_site_prefix(series_names)


def _choose_series_site_prefix(
    series_id: str,
    all_series_names: list[str],
    manual_prefixes: Optional[list[str]] = None,
) -> str:
    """Choose the effective site/locality prefix for one series only.

    Notes:
        - Manual prefixes must never be applied globally to unrelated series.
        - This keeps prefix-free numeric series stable when the dataset also
          contains a few coded series with a site prefix.
        - A manual prefix is used only when the current normalized series id
          actually starts with that prefix.
    """
    sid = _normalize_series_id_token(series_id)
    manual_norm = [
        _normalize_series_id_token(x)
        for x in list(manual_prefixes or [])
        if _normalize_series_id_token(x)
    ]
    manual_norm.sort(key=len, reverse=True)
    for pref in manual_norm:
        if sid.startswith(pref):
            return str(pref)

    common_prefix = _common_series_site_prefix(all_series_names)
    if common_prefix and sid.startswith(common_prefix):
        return str(common_prefix)
    return ""


def _merge_series_prefix_parts(common_site_prefix: str, tree_prefix: str) -> str:
    """Merge a shared site prefix with a parsed tree prefix without duplicating overlap."""
    left = _normalize_series_id_token(common_site_prefix)
    right = _normalize_series_id_token(tree_prefix)
    if not left:
        return str(right)
    if not right:
        return str(left)
    if right.startswith(left):
        return str(right)
    if left.endswith(right):
        return str(left)
    max_overlap = min(len(left), len(right))
    for size in range(max_overlap, 0, -1):
        if left[-size:] == right[:size]:
            return str(left + right[size:])
    return str(left + right)


def _split_series_id_prefix_number_suffix(series_id: str) -> tuple[str, str, str]:
    """Split a normalized Series ID into tree prefix, tree number, and trailing core suffix.

    Notes:
        - Matching is intentionally right-to-left.
        - Generic automatic parsing must not treat plain trailing digits as a core/radius suffix,
          because identifiers such as KC0221 and KC0224 are usually different tree numbers.
        - Numeric-only suffixes should be handled by explicit suffix rules such as 1/2 or 01/02.
        - Separators are ignored earlier by _normalize_series_id_token(), including spaces,
          hyphens, underscores, dots, slashes, backslashes, commas, semicolons, colons, pipes,
          plus signs, parentheses, brackets, braces, and angle brackets.
    """
    sid = _normalize_series_id_token(series_id)
    if not sid:
        return ("", "", "")

    m = re.match(r"^(.*?)(\d+)((?:S\d{1,2})?R\d{1,2}|S\d{1,2}|AVG|MEAN)$", sid)
    if m is not None:
        return (
            str(m.group(1) or ""),
            str(m.group(2) or ""),
            str(m.group(3) or ""),
        )

    m = re.match(r"^(.*?)(\d+)([NSEW]{1,2})$", sid)
    if m is not None:
        return (
            str(m.group(1) or ""),
            str(m.group(2) or ""),
            str(m.group(3) or ""),
        )

    m = re.match(r"^(.*?)(\d+)([A-Z])$", sid)
    if m is not None:
        return (
            str(m.group(1) or ""),
            str(m.group(2) or ""),
            str(m.group(3) or ""),
        )

    m = re.match(r"^(.*?)(\d+)$", sid)
    if m is not None:
        return (
            str(m.group(1) or ""),
            str(m.group(2) or ""),
            "",
        )

    prefix_match = re.match(r"^[A-Z0-9]+", sid)
    tree_prefix = prefix_match.group(0) if prefix_match else sid
    return (str(tree_prefix), "", "")


def _series_active_year_span(series: pd.Series) -> tuple[Optional[int], Optional[int]]:
    """Return the active non-missing year span for one series."""
    try:
        s = pd.to_numeric(series, errors="coerce")
    except Exception:
        return (None, None)
    s = s.dropna()
    if s.empty:
        return (None, None)
    try:
        years = pd.Index(s.index).astype(int)
    except Exception:
        years = pd.to_numeric(pd.Index(s.index), errors="coerce")
        years = years[~pd.isna(years)].astype(int)
    if len(years) == 0:
        return (None, None)
    return (int(np.min(years)), int(np.max(years)))


def _year_spans_overlap(
    span_a: tuple[Optional[int], Optional[int]],
    span_b: tuple[Optional[int], Optional[int]],
) -> bool:
    """Return True when two year spans overlap by at least one year."""
    a0, a1 = span_a
    b0, b1 = span_b
    if a0 is None or a1 is None or b0 is None or b1 is None:
        return True
    return max(int(a0), int(b0)) <= min(int(a1), int(b1))


def _apply_nonoverlap_split_guard(
    ids_df: pd.DataFrame,
    rwl: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Prevent merging same-labeled series when their active periods do not overlap.

    Notes:
        - Some datasets intentionally split one logical code into separate temporal segments.
        - When two rows have the same auto tree label but no year overlap, they must stay separate.
        - This guard rewrites tree labels only for conflicting non-overlapping subsets.
    """
    if ids_df is None or ids_df.empty:
        return ids_df
    if rwl is None or not isinstance(rwl, pd.DataFrame) or rwl.empty:
        return ids_df
    if "series_id" not in ids_df.columns or "tree_label" not in ids_df.columns:
        return ids_df

    out = ids_df.copy()
    if out.index.name != "series_id" and "series_id" in out.columns:
        out = out.set_index("series_id", drop=False)

    rwl2 = rwl.copy()
    rwl2.columns = [str(x) for x in list(rwl2.columns)]

    for tree_label, idx_labels in out.groupby("tree_label", sort=False).groups.items():
        members = [str(x) for x in list(idx_labels)]
        if len(members) <= 1:
            continue

        spans: dict[str, tuple[Optional[int], Optional[int]]] = {}
        for series_id in members:
            if series_id in rwl2.columns:
                spans[series_id] = _series_active_year_span(rwl2[series_id])
            else:
                spans[series_id] = (None, None)

        clusters: list[list[str]] = []
        for series_id in members:
            placed = False
            for cluster in clusters:
                if all(_year_spans_overlap(spans[series_id], spans[other]) for other in cluster):
                    cluster.append(series_id)
                    placed = True
                    break
            if not placed:
                clusters.append([series_id])

        if len(clusters) <= 1:
            continue

        for cluster_idx, cluster in enumerate(clusters, start=1):
            if cluster_idx == 1:
                new_tree_label = str(tree_label)
            else:
                new_tree_label = f"{tree_label}__SPLIT{cluster_idx}"
            for series_id in cluster:
                out.loc[series_id, "tree_label"] = str(new_tree_label)

    return out


def _guess_tree_core_labels_from_series_id(
    series_id: str,
    suffix_variant_lines: Optional[list[str]] = None,
    group_pattern_lines: Optional[list[str]] = None,
    common_site_prefix: str = "",
) -> tuple[str, str, float]:
    """Guess tree/core labels from one series identifier.

    Notes:
        - Generic parsing is intentionally conservative for numeric endings.
        - Examples such as KC02 24 and KC0221 are normalized first and then parsed from the end,
          so the numeric ending is treated as part of the tree number unless an explicit suffix
          rule or grouping pattern says otherwise.
    """
    sid_full = _normalize_series_id_token(series_id)
    if not sid_full:
        return ("SERIES", "1", 0.0)

    # Separator rule: "AM80_1", "DKC115_2", "K114-1", "T12.B" -> radius after the separator.
    # One character (or R1/S2 style) after the separator is a radius; two or more
    # digits ("KC02_21") are a tree number and stay in the tree label.
    raw = str(series_id or "").strip()
    m_sep = re.match(r"^(.*?[A-Za-z0-9])[\s_\-.]+([A-Za-z0-9]|[RrSs]\d{1,2})$", raw)
    if m_sep is not None:
        tree_raw = _normalize_series_id_token(m_sep.group(1))
        core_raw = _normalize_series_id_token(m_sep.group(2))
        if tree_raw and core_raw:
            return (tree_raw, core_raw, 0.995)

    sid = sid_full
    site_prefix = _normalize_series_id_token(common_site_prefix)
    if site_prefix and sid.startswith(site_prefix) and len(sid) > len(site_prefix):
        sid = sid[len(site_prefix):]

    for sid_candidate in (sid, sid_full):
        tree_prefix, tree_number_text, trailing_suffix = _split_series_id_prefix_number_suffix(sid_candidate)
        if tree_prefix and tree_number_text and trailing_suffix:
            tree_label = f"{tree_prefix}{tree_number_text}".strip()
            core_label = trailing_suffix.strip()
            if tree_label and core_label:
                return (tree_label, core_label, 0.995)

    for sid_candidate in (sid, sid_full):
        for pattern in list(group_pattern_lines or []):
            split_res = _split_series_id_by_group_pattern(sid_candidate, pattern)
            if split_res is None:
                continue
            tree_label, core_label = split_res
            if len(tree_label) >= 1 and core_label:
                return (tree_label, core_label, 0.99)

    tree_prefix, tree_number_text, trailing_suffix = _split_series_id_prefix_number_suffix(sid)
    if tree_number_text:
        tree_label = f"{tree_prefix}{tree_number_text}".strip()
        core_label = trailing_suffix or "1"
        if tree_label:
            return (tree_label, core_label, 0.99)

    patterns = _build_radius_suffix_guess_patterns(suffix_variant_lines=suffix_variant_lines)
    for pattern, confidence in patterns:
        m = re.match(pattern, sid)
        if m is None:
            continue
        tree_label = str(m.group(1) or "").strip()
        core_label = str(m.group(2) or "").strip()
        if len(tree_label) >= 2 and core_label:
            return (tree_label, core_label, float(confidence))

    return (sid, "1", 0.25)


def finalize_tree_ids_mapping(
    ids: Optional[pd.DataFrame],
    series_names: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Finalize a tree/core mapping table and ensure numeric ids are present."""
    names = [str(x) for x in (series_names or [])]
    if ids is None or not isinstance(ids, pd.DataFrame) or ids.empty:
        ids2 = pd.DataFrame({"series_id": names}, dtype=str)
    else:
        ids2 = ids.copy()
        if "series_id" not in ids2.columns:
            ids2["series_id"] = ids2.index.astype(str)
        ids2["series_id"] = ids2["series_id"].astype(str)

    if names:
        ids2 = ids2.set_index("series_id", drop=False)
        for name in names:
            if name not in ids2.index:
                ids2.loc[name, "series_id"] = name
        ids2 = ids2.loc[names].copy()
    else:
        ids2 = ids2.set_index("series_id", drop=False)

    if "tree_label" not in ids2.columns:
        ids2["tree_label"] = ids2["series_id"].astype(str)
    if "core_label" not in ids2.columns:
        ids2["core_label"] = "1"
    if "tree_prefix" not in ids2.columns:
        ids2["tree_prefix"] = ""
    if "tree_number_text" not in ids2.columns:
        ids2["tree_number_text"] = ""
    if "confidence" not in ids2.columns:
        ids2["confidence"] = np.nan
    if "source" not in ids2.columns:
        ids2["source"] = "manual"

    derived_tree_parts = ids2["tree_label"].astype(str).map(_split_series_id_prefix_number_suffix)
    derived_prefix = derived_tree_parts.map(lambda x: str(x[0]))
    derived_number = derived_tree_parts.map(lambda x: str(x[1]))
    derived_series_parts = ids2["series_id"].astype(str).map(_split_series_id_prefix_number_suffix)
    derived_suffix = derived_series_parts.map(lambda x: str(x[2]))

    ids2["tree_label"] = ids2["tree_label"].astype(str).map(_normalize_series_id_token)
    ids2["core_label"] = ids2["core_label"].astype(str).map(_normalize_series_id_token)
    ids2["tree_prefix"] = (
        ids2["tree_prefix"]
        .astype(str)
        .str.upper()
        .str.replace(r"[^A-Z0-9]+", "", regex=True)
    )
    ids2["tree_number_text"] = (
        ids2["tree_number_text"]
        .astype(str)
        .str.replace(r"[^0-9]+", "", regex=True)
    )
    ids2["tree_label"] = ids2["tree_label"].mask(ids2["tree_label"].eq(""), np.nan)
    ids2["core_label"] = ids2["core_label"].mask(ids2["core_label"].eq(""), np.nan)
    ids2["tree_prefix"] = ids2["tree_prefix"].mask(ids2["tree_prefix"].eq(""), np.nan)
    ids2["tree_number_text"] = ids2["tree_number_text"].mask(ids2["tree_number_text"].eq(""), np.nan)

    ids2["tree_prefix"] = ids2["tree_prefix"].where(~ids2["tree_prefix"].isna(), derived_prefix)
    ids2["tree_number_text"] = ids2["tree_number_text"].where(~ids2["tree_number_text"].isna(), derived_number)

    missing_tree = ids2["tree_label"].isna()
    if bool(missing_tree.any()):
        ids2.loc[missing_tree, "tree_label"] = (
            ids2.loc[missing_tree, "tree_prefix"].fillna("").astype(str)
            + ids2.loc[missing_tree, "tree_number_text"].fillna("").astype(str)
        ).replace("", np.nan)
        still_missing_tree = ids2["tree_label"].isna()
        if bool(still_missing_tree.any()):
            ids2.loc[still_missing_tree, "tree_label"] = (
                ids2.loc[still_missing_tree, "series_id"].astype(str).map(_normalize_series_id_token)
            )

    missing_core = ids2["core_label"].isna()
    if bool(missing_core.any()):
        ids2.loc[missing_core, "core_label"] = derived_suffix.loc[missing_core].replace("", np.nan)
        still_missing_core = ids2["core_label"].isna()
        if bool(still_missing_core.any()):
            ids2.loc[still_missing_core, "core_label"] = "1"

    tree_labels = pd.Index(pd.unique(ids2["tree_label"].astype(str)))
    tree_map = {str(lbl): int(i + 1) for i, lbl in enumerate(tree_labels)}
    ids2["tree"] = ids2["tree_label"].astype(str).map(tree_map).astype(int)

    core_num = np.zeros(ids2.shape[0], dtype=int)
    for tree_label, block_index in ids2.groupby("tree_label", sort=False).groups.items():
        core_labels = pd.Index(pd.unique(ids2.loc[list(block_index), "core_label"].astype(str)))
        core_map = {str(lbl): int(i + 1) for i, lbl in enumerate(core_labels)}
        ids2.loc[list(block_index), "core"] = ids2.loc[list(block_index), "core_label"].astype(str).map(core_map).astype(int)

    ids2["tree"] = pd.to_numeric(ids2["tree"], errors="coerce").fillna(0).astype(int)
    ids2["core"] = pd.to_numeric(ids2["core"], errors="coerce").fillna(0).astype(int)
    ids2.index = ids2["series_id"].astype(str)
    return ids2


def build_identity_tree_ids(series_names: list[str]) -> pd.DataFrame:
    """Build a one-series-per-tree mapping without any automatic grouping."""
    rows: list[dict[str, Any]] = []
    for series_id in [str(x) for x in list(series_names)]:
        sid = _normalize_series_id_token(series_id)
        rows.append(
            {
                "series_id": str(series_id),
                "tree_prefix": "",
                "tree_number_text": "",
                "tree_label": str(sid or series_id),
                "core_label": "1",
                "confidence": 0.0,
                "source": "identity",
            }
        )
    return finalize_tree_ids_mapping(pd.DataFrame(rows), series_names=[str(x) for x in list(series_names)])


def build_auto_tree_ids(
    series_names: list[str],
    suffix_variant_lines: Optional[list[str]] = None,
    group_pattern_lines: Optional[list[str]] = None,
    manual_prefix_lines: Optional[list[str]] = None,
    rwl: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Build an automatic tree/core mapping table from series identifiers."""
    rows: list[dict[str, Any]] = []
    for series_id in [str(x) for x in list(series_names)]:
        common_site_prefix = _choose_series_site_prefix(
            series_id,
            [str(x) for x in list(series_names)],
            manual_prefixes=manual_prefix_lines,
        )
        tree_label, core_label, confidence = _guess_tree_core_labels_from_series_id(
            series_id,
            suffix_variant_lines=suffix_variant_lines,
            group_pattern_lines=group_pattern_lines,
            common_site_prefix=common_site_prefix,
        )
        _tree_prefix_from_label, tree_number_text, _suffix = _split_series_id_prefix_number_suffix(tree_label)
        tree_prefix = _merge_series_prefix_parts(common_site_prefix, _tree_prefix_from_label)
        rows.append(
            {
                "series_id": str(series_id),
                "tree_prefix": str(tree_prefix),
                "tree_number_text": str(tree_number_text),
                "tree_label": str(tree_label),
                "core_label": str(core_label),
                "confidence": float(confidence),
                "source": "auto",
            }
        )
    rows_df = pd.DataFrame(rows)
    rows_df = _apply_nonoverlap_split_guard(rows_df, rwl=rwl)
    return finalize_tree_ids_mapping(rows_df, series_names=[str(x) for x in list(series_names)])


def _refine_auto_ids_with_ring_widths(ids_df: pd.DataFrame, rwl: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Use ring widths when the series names did not reveal any multi-radius tree.

    Radii of one tree share their year-to-year signal. If the name rules put
    every series in its own tree, test simple ID schemes (trailing letters,
    R1/R2, last 1-3 characters = radius) and apply one only when the radii it
    groups correlate clearly better than different trees do.
    """
    if _pith is None or rwl is None or ids_df is None or ids_df.empty:
        return ids_df
    try:
        names = [str(x) for x in ids_df["series_id"].tolist()]
        if len(names) < 4 or (ids_df.groupby("tree_label").size() > 1).any():
            return ids_df
        rwl2 = rwl.copy()
        rwl2.columns = [str(c) for c in rwl2.columns]
        if not set(names).issubset(set(rwl2.columns)):
            return ids_df
        r = _pith.pairwise_r(rwl2[names])
        cands = _pith.infer_grouping_schemes(names, rwl2, current=None, r=r)
        best = cands[0] if cands else None
        if (
            best is None
            or not np.isfinite(best["within_r"])
            or best["within_r"] < 0.5
            or best["score"] < 0.15
            or best["multi_trees"] < 2
        ):
            return ids_df
        cores = best.get("cores") or {}
        rows = []
        for sid in names:
            tree_label = str(best["groups"].get(sid, sid))
            tp, tn, _sf = _split_series_id_prefix_number_suffix(tree_label)
            rows.append(
                {
                    "series_id": sid,
                    "tree_prefix": tp,
                    "tree_number_text": tn,
                    "tree_label": tree_label,
                    "core_label": str(cores.get(sid, "1")),
                    "confidence": float(best["within_r"]),
                    "source": "ring-width",
                }
            )
        out = _apply_nonoverlap_split_guard(pd.DataFrame(rows), rwl=rwl2)
        return finalize_tree_ids_mapping(out, series_names=names)
    except Exception:
        return ids_df


def build_age_curve(rwl: pd. DataFrame, po: Optional[pd.DataFrame] = None) -> pd. Series:
    """Build mean growth curve by cambial age (pith offset aware when ``po`` is given)."""
    age_values: Dict[int, list[float]] = {}
    po_vec = _get_po_vector(rwl, po=po) if po is not None else {}
    for col in rwl.columns:
        ser = rwl[col].dropna()
        if ser.empty:
            continue
        ages = np.arange(1, len(ser) + 1, dtype=int) + int(po_vec.get(str(col), 1)) - 1
        for age, val in zip(ages, ser.values):
            if np.isnan(val):
                continue
            age_values.setdefault(int(age), []).append(float(val))
    means = {
        age: float(np.nanmean(vals))
        for age, vals in sorted(age_values.items())
        if len(vals) > 0
    }
    return pd. Series(means, name="age_curve")

def _prepare_ids_dplr_like(
    rwi: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prepare dplR-like tree/core ids for correlation statistics.

    Notes:
        - If ids is None, assume one core per tree.
        - If ids contains duplicate tree/core pairs, average those columns first.
        - If row names are available in ids and match all rwi columns, use them for reordering.
    """
    x = rwi.copy().astype(float)
    n_cores = int(x.shape[1])
    if n_cores == 0:
        return x, pd.DataFrame(columns=["tree", "core"])

    if ids is None:
        ids2 = pd.DataFrame(
            {
                "tree": np.arange(1, n_cores + 1, dtype=int),
                "core": np.ones(n_cores, dtype=int),
            },
            index=x.columns,
        )
        return x, ids2

    if (not isinstance(ids, pd.DataFrame)) or (not {"tree", "core"}.issubset(ids.columns)):
        raise ValueError("'ids' must be a DataFrame with columns 'tree' and 'core'.")

    ids2 = ids.copy()
    rownames_ids = ids2.index.astype(str)
    colnames_rwi = pd.Index(x.columns).astype(str)

    if len(rownames_ids) > 0 and all(col in set(rownames_ids) for col in colnames_rwi):
        ids2 = ids2.loc[colnames_rwi, ["tree", "core"]].copy()
    else:
        if int(ids2.shape[0]) != n_cores:
            raise ValueError("dimension problem: 'ncol(rwi)' != 'nrow(ids)'")
        ids2 = ids2.iloc[:n_cores][["tree", "core"]].copy()
        ids2.index = x.columns

    ids2["tree"] = pd.to_numeric(ids2["tree"], errors="raise").astype(int)
    ids2["core"] = pd.to_numeric(ids2["core"], errors="raise").astype(int)

    dup_mask = ids2.duplicated(subset=["tree", "core"], keep=False)
    if not bool(dup_mask.any()):
        return x, ids2

    grouped_cols: list[pd.Series] = []
    grouped_ids: list[tuple[int, int]] = []
    unique_pairs = ids2.loc[:, ["tree", "core"]].drop_duplicates().reset_index(drop=True)
    for _, row in unique_pairs.iterrows():
        tree_id = int(row["tree"])
        core_id = int(row["core"])
        these_cols = ids2.index[(ids2["tree"] == tree_id) & (ids2["core"] == core_id)]
        block = x.loc[:, these_cols]
        avg = block.mean(axis=1, skipna=True)
        avg[block.notna().sum(axis=1) == 0] = np.nan
        grouped_cols.append(avg.rename(f"{tree_id}_{core_id}"))
        grouped_ids.append((tree_id, core_id))

    x2 = pd.concat(grouped_cols, axis=1)
    ids3 = pd.DataFrame(grouped_ids, columns=["tree", "core"], index=x2.columns)
    return x2, ids3

def _cor_with_limit_dplr_like(
    limit: int,
    x: np.ndarray,
    y: np.ndarray,
    method: str,
) -> np.ndarray:
    """Compute dplR-like correlations between columns of x and y."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n_x = int(x.shape[1])
    n_y = int(y.shape[1])
    out = np.full((n_x, n_y), np.nan, dtype=float)
    for i in range(n_x):
        xi = x[:, i]
        good_x = np.isfinite(xi)
        for j in range(n_y):
            yj = y[:, j]
            good_both = np.flatnonzero(good_x & np.isfinite(yj))
            if int(good_both.size) >= int(limit):
                out[i, j] = _corr_1d_dplr_like(
                    xi[good_both],
                    yj[good_both],
                    method=method,
                    min_overlap=int(limit),
                )
    return out

def _cor_with_limit_upper_dplr_like(
    limit: int,
    x: np.ndarray,
    method: str,
) -> np.ndarray:
    """Compute dplR-like upper-triangle correlations between columns of x."""
    x = np.asarray(x, dtype=float)
    n_x = int(x.shape[1])
    vals: list[float] = []
    for i in range(n_x - 1):
        xi = x[:, i]
        good_i = np.isfinite(xi)
        for j in range(i + 1, n_x):
            xj = x[:, j]
            good_both = np.flatnonzero(good_i & np.isfinite(xj))
            if int(good_both.size) >= int(limit):
                r = _corr_1d_dplr_like(
                    xi[good_both],
                    xj[good_both],
                    method=method,
                    min_overlap=int(limit),
                )
                vals.append(r)
            else:
                vals.append(np.nan)
    return np.asarray(vals, dtype=float)

def interseries_corr_matrix(
    rwi: pd.DataFrame,
    min_overlap: int = 2,
    ids: Optional[pd.DataFrame] = None,
) -> np.ndarray:
    """Compute a dplR-like pairwise correlation matrix between series.

    Notes:
        - This matches the default dplR logic more closely than pandas.DataFrame.corr:
          method defaults to Spearman in dplR::rwi.stats.running.
        - Zeros are treated as missing values by default in dplR statistics.
        - Correlations with overlap below the minimum are returned as NaN.
    """
    df = rwi.copy().astype(float)
    df = df.replace(0.0, np.nan)
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    df, _ids = _prepare_ids_dplr_like(df, ids=ids)
    n = int(df.shape[1])
    if n < 2:
        return np.array([[1.0]], dtype=float)
    arr = df.to_numpy(dtype=float)
    out = np.full((n, n), np.nan, dtype=float)
    np.fill_diagonal(out, 1.0)
    for i in range(n - 1):
        xi = arr[:, i]
        for j in range(i + 1, n):
            xj = arr[:, j]
            r = _corr_1d_dplr_like(xi, xj, method="spearman", min_overlap=int(max(2, min_overlap)))
            out[i, j] = r
            out[j, i] = r
    return out

def mean_rbar(
    rwi: pd.DataFrame,
    min_overlap: int = 2,
    ids: Optional[pd.DataFrame] = None,
) -> float:
    """Compute dplR-compatible Rbar.

    Returns:
        rbar.tot from a dplR-like rwi.stats calculation with ids=NULL.
    """
    stats_df = _rwi_stats_running_dplr_like(
        rwi,
        ids=ids,
        running_window=False,
        method="spearman",
        zero_is_missing=True,
        min_corr_overlap=int(max(2, min_overlap)),
    )
    if stats_df.empty or "rbar.tot" not in stats_df.columns:
        return float("nan")
    val = float(stats_df.iloc[0]["rbar.tot"])
    return val if np.isfinite(val) else float("nan")


EPS_RBAR_FULL_COLUMNS = [
    "rbar.tot",
    "rbar.wt",
    "rbar.bt",
    "c.eff",
    "rbar.eff",
    "eps",
    "snr",
    "n.cores",
    "n.trees",
    "n",
]

EPS_RBAR_RUNNING_KEY_COLUMNS = ["start.year", "mid.year", "end.year"]
EPS_RBAR_RUNNING_COLUMNS = [
    *EPS_RBAR_FULL_COLUMNS,
    "rbar",
    "depth",
    "n_series_win",
    "n_years_win",
]


def compute_eps_rbar_stats_full_period(
    rwi: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
    min_overlap: Optional[int] = None,
) -> pd.DataFrame:
    """Compute full-period dplR-like EPS/Rbar statistics."""
    if rwi is None or rwi.empty:
        return pd.DataFrame(columns=EPS_RBAR_FULL_COLUMNS)
    n_years = int(rwi.shape[0])
    mo = int(min(30, n_years) if min_overlap is None else min_overlap)
    mo = int(max(2, min(mo, n_years)))
    out = _rwi_stats_running_dplr_like(
        rwi,
        ids=ids,
        running_window=False,
        window_length=n_years,
        window_overlap=0,
        min_corr_overlap=mo,
        method="spearman",
        zero_is_missing=True,
    )
    if out is None or out.empty:
        return pd.DataFrame(columns=EPS_RBAR_FULL_COLUMNS)
    out2 = out.copy()
    for col in EPS_RBAR_FULL_COLUMNS:
        if col not in out2.columns:
            out2[col] = np.nan
    out2 = out2.loc[:, EPS_RBAR_FULL_COLUMNS].reset_index(drop=True)
    return out2


def _normalize_eps_compare_df(
    df: Optional[pd.DataFrame],
    key_cols: list[str],
    value_cols: list[str],
) -> pd.DataFrame:
    """Normalize a stats table before alignment and diffing."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(columns=[*key_cols, *value_cols])
    out = df.copy()
    for col in key_cols:
        if col not in out.columns:
            out[col] = np.nan
        try:
            out[col] = pd.to_numeric(out[col], errors="raise")
        except Exception:
            out[col] = out[col].astype(str)
    for col in value_cols:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.loc[:, [*key_cols, *value_cols]].copy()


def _prepare_running_eps_rbar_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Normalize and index a running EPS/Rbar table for plotting/caching."""
    out = _normalize_eps_compare_df(
        df,
        key_cols=EPS_RBAR_RUNNING_KEY_COLUMNS,
        value_cols=EPS_RBAR_RUNNING_COLUMNS,
    )
    out = sanitize_eps_rbar_depth_df(out)
    if out.empty:
        return out
    if "mid.year" in out.columns:
        mid = pd.to_numeric(out["mid.year"], errors="coerce")
        keep = np.isfinite(mid.to_numpy(dtype=float))
        out = out.loc[keep].copy()
        out.index = mid.loc[keep].astype(int)
        out.index.name = "year"
    return out


def _compute_running_eps_rbar_backend(
    rwi: pd.DataFrame,
    window: int,
    compat_mode: CompatMode = "legacy",
    compat_config: Optional[DplrCompatConfig] = None,
    method_key: str = "series",
) -> pd.DataFrame:
    """Compute running EPS/Rbar using either the Python or dplR backend."""
    win = int(max(3, window))
    if compat_mode == "dplR":
        cfg = compat_config or DEFAULT_COMPAT_CONFIG
        try:
            _stats_by, running_by, _extras = _dplr_compute_eps_rbar_from_rwi(
                {str(method_key): rwi},
                cfg,
                window=win,
                verbose=False,
            )
            return _prepare_running_eps_rbar_df(running_by.get(str(method_key)))
        except Exception:
            if bool(getattr(cfg, "strict_dplr", False)):
                raise
    try:
        df = compute_eps_rbar_depth_timeseries(rwi, window=win)
    except Exception:
        df = pd.DataFrame()
    return sanitize_eps_rbar_depth_df(df)


def _compare_eps_tables(
    py_df: Optional[pd.DataFrame],
    ref_df: Optional[pd.DataFrame],
    key_cols: list[str],
    value_cols: list[str],
) -> pd.DataFrame:
    """Align Python and dplR stats tables and compute numeric diffs."""
    py2 = _normalize_eps_compare_df(py_df, key_cols=key_cols, value_cols=value_cols)
    ref2 = _normalize_eps_compare_df(ref_df, key_cols=key_cols, value_cols=value_cols)

    if key_cols:
        merged = py2.merge(ref2, on=key_cols, how="outer", suffixes=("_py", "_dplr"), sort=True)
        if not merged.empty:
            merged = merged.sort_values(key_cols).reset_index(drop=True)
    else:
        py3 = py2.copy()
        ref3 = ref2.copy()
        py3["_rowid"] = np.arange(py3.shape[0], dtype=int)
        ref3["_rowid"] = np.arange(ref3.shape[0], dtype=int)
        merged = py3.merge(ref3, on="_rowid", how="outer", suffixes=("_py", "_dplr"), sort=True)
        merged = merged.drop(columns=["_rowid"])

    for col in value_cols:
        py_col = pd.to_numeric(merged.get(f"{col}_py"), errors="coerce")
        ref_col = pd.to_numeric(merged.get(f"{col}_dplr"), errors="coerce")
        merged[f"diff_{col}"] = py_col - ref_col
    return merged


def _max_abs_from_diff_table(diff_df: Optional[pd.DataFrame]) -> float:
    """Return the maximum absolute numeric difference across diff_* columns."""
    if diff_df is None or not isinstance(diff_df, pd.DataFrame) or diff_df.empty:
        return float("nan")
    diff_cols = [c for c in diff_df.columns if str(c).startswith("diff_")]
    if not diff_cols:
        return float("nan")
    vals: list[float] = []
    for col in diff_cols:
        arr = pd.to_numeric(diff_df[col], errors="coerce").to_numpy(dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size > 0:
            vals.append(float(np.nanmax(np.abs(arr))))
    if not vals:
        return float("nan")
    return float(np.nanmax(np.asarray(vals, dtype=float)))


def _count_compared_cells_from_diff_table(diff_df: Optional[pd.DataFrame]) -> int:
    """Return the number of finite compared diff cells."""
    if diff_df is None or not isinstance(diff_df, pd.DataFrame) or diff_df.empty:
        return 0
    total = 0
    for col in diff_df.columns:
        if str(col).startswith("diff_"):
            arr = pd.to_numeric(diff_df[col], errors="coerce").to_numpy(dtype=float)
            total += int(np.isfinite(arr).sum())
    return int(total)


def _dplr_compute_eps_rbar_from_rwi(
    rwi_by_method: Dict[str, pd.DataFrame],
    config: DplrCompatConfig,
    window: int = 50,
    verbose: bool = False,
    progress_label: str = "dplR EPS/RBAR",
) -> tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], Dict[str, Any]]:
    """Compute dplR EPS/Rbar stats from precomputed RWI matrices via Rscript."""
    if not rwi_by_method:
        return {}, {}, {}
    rscript_cmd = _ensure_rscript(config)
    with tempfile.TemporaryDirectory(prefix="detrend_eps_rbar_dplr_") as td:
        td_path = str(td)
        script_path = os.path.join(td_path, "validate_eps_rbar_dplr.R")
        _write_validate_eps_rbar_dplr_r(script_path)
        in_dir = os.path.join(td_path, "rwi_in")
        out_dir = os.path.join(td_path, "out")
        os.makedirs(in_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        method_keys = sorted(rwi_by_method.keys())
        for key in method_keys:
            rwi_path = os.path.join(in_dir, f"rwi_{key}.csv")
            _write_rwl_csv_for_r(rwi_by_method[key], rwi_path)

        n_workers = _choose_r_parallel_workers(len(method_keys))

        args = [
            rscript_cmd,
            script_path,
            in_dir,
            out_dir,
            ",".join(method_keys),
            f"auto_install={'TRUE' if config.auto_install_r_deps else 'FALSE'}",
            f"cran_repo={config.cran_repo}",
            f"window_length={int(max(3, window))}",
            f"window_overlap={int(math.floor(max(3, window) / 2))}",
            f"min_corr_overlap={int(max(3, min(30, max(3, window))))}",
            f"parallel_workers={n_workers}",
        ]
        if verbose:
            _print_progress(
                f"{progress_label}: launching R for {len(method_keys)} method(s), "
                f"window={int(max(3, window))}, "
                f"workers={n_workers}, queued={max(0, len(method_keys) - n_workers)}"
            )
        proc = _run_subprocess_capture(
            args,
            verbose=verbose,
            prefix=f"{progress_label}: ",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "dplR EPS/RBAR execution failed.\n"
                f"Command: {' '.join(args)}\n"
                f"STDERR:\n{proc.stderr}\n"
                f"STDOUT:\n{proc.stdout}\n"
            )

        stats_by: Dict[str, pd.DataFrame] = {}
        running_by: Dict[str, pd.DataFrame] = {}
        extras: Dict[str, Any] = {}
        ver_path = os.path.join(out_dir, "dplr_version.txt")
        if os.path.exists(ver_path):
            extras["dplr_version"] = Path(ver_path).read_text(encoding="utf-8").strip()

        errors: Dict[str, str] = {}
        series_exclusions: Dict[str, Dict[str, str]] = {}
        for key in method_keys:
            err_path = os.path.join(out_dir, f"dplr_eps_rbar_error_{key}.txt")
            if os.path.exists(err_path):
                try:
                    errors[key] = Path(err_path).read_text(encoding="utf-8").strip()
                except Exception:
                    errors[key] = "Error text could not be read."

            stats_path = os.path.join(out_dir, f"dplr_eps_rbar_stats_{key}.csv")
            running_path = os.path.join(out_dir, f"dplr_eps_rbar_running_{key}.csv")
            if os.path.exists(stats_path):
                stats_by[key] = pd.read_csv(stats_path)
            if os.path.exists(running_path):
                running_by[key] = pd.read_csv(running_path)

            excl_path = os.path.join(out_dir, f"dplr_eps_rbar_series_exclusions_{key}.tsv")
            excl_map = _read_series_exclusion_tsv(excl_path)
            if excl_map:
                series_exclusions[key] = excl_map

        if errors:
            extras["dplr_eps_rbar_errors"] = errors
        if series_exclusions:
            extras["dplr_eps_rbar_series_exclusions"] = series_exclusions
        return stats_by, running_by, extras

def eps_like(
    rwi: pd.DataFrame,
    min_overlap: int = 2,
    ids: Optional[pd.DataFrame] = None,
) -> float:
    """Compute dplR-compatible EPS.

    Returns:
        eps from a dplR-like rwi.stats calculation with ids=NULL.
    """
    stats_df = _rwi_stats_running_dplr_like(
        rwi,
        ids=ids,
        running_window=False,
        method="spearman",
        zero_is_missing=True,
        min_corr_overlap=int(max(2, min_overlap)),
    )
    if stats_df.empty or "eps" not in stats_df.columns:
        return float("nan")
    val = float(stats_df.iloc[0]["eps"])
    return val if np.isfinite(val) else float("nan")

def _corr_1d_dplr_like(
    x: np.ndarray,
    y: np.ndarray,
    method: str = "spearman",
    min_overlap: int = 2,
) -> float:
    """Compute one pairwise correlation using dplR-like defaults."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < int(max(2, min_overlap)):
        return float("nan")
    xx = x[mask]
    yy = y[mask]
    if method == "spearman":
        xx = rankdata(xx, method="average")
        yy = rankdata(yy, method="average")
        if float(np.nanstd(xx)) == 0.0 or float(np.nanstd(yy)) == 0.0:
            return float("nan")
        return float(np.corrcoef(xx, yy)[0, 1])
    if method == "kendall":
        r, _ = kendalltau(xx, yy)
        return float(r) if np.isfinite(r) else float("nan")
    if float(np.nanstd(xx)) == 0.0 or float(np.nanstd(yy)) == 0.0:
        return float("nan")
    return float(np.corrcoef(xx, yy)[0, 1])

def _pairwise_corrs_upper_dplr_like(
    arr: np.ndarray,
    min_corr_overlap: int,
    method: str = "spearman",
) -> tuple[np.ndarray, np.ndarray]:
    """Return upper-triangle pairwise correlations and pair counts per series."""
    n = int(arr.shape[1])
    vals: list[float] = []
    good_flag = np.zeros(n, dtype=bool)
    for i in range(n - 1):
        xi = arr[:, i]
        for j in range(i + 1, n):
            xj = arr[:, j]
            r = _corr_1d_dplr_like(xi, xj, method=method, min_overlap=min_corr_overlap)
            if np.isfinite(r):
                vals.append(float(r))
                good_flag[i] = True
                good_flag[j] = True
    return np.asarray(vals, dtype=float), good_flag

def _choose_first_window_start_dplr_like(
    valid_mask: np.ndarray,
    window_length: int,
    window_overlap: int,
    min_corr_overlap: int,
) -> int:
    """Match dplR heuristic for the first running-window position."""
    n_years = int(valid_mask.shape[0])
    window_advance = int(window_length - window_overlap)
    n_trees_by_year = np.sum(valid_mask, axis=1)
    good_rows_flag = n_trees_by_year > 1
    good_rows = np.flatnonzero(good_rows_flag)
    if good_rows.size == 0:
        return 0
    min_offset = max(0, int(good_rows.min()) - (int(window_length) - int(min_corr_overlap)))
    max_offset = min(min_offset + window_advance - 1, n_years - window_length)
    offsets = list(range(min_offset, max_offset + 1))
    if not offsets:
        return 0
    n_data: list[float] = []
    for offset in offsets:
        n_windows_minus_one = (n_years - offset - window_length) // window_advance
        max_idx = offset + window_length + n_windows_minus_one * window_advance
        row_idx = np.arange(offset, max_idx, dtype=int)
        good_idx = row_idx[good_rows_flag[row_idx]]
        if good_idx.size == 0:
            n_data.append(float("-inf"))
        else:
            n_data.append(float(np.sum(valid_mask[good_idx, :])))
    best_rev = int(np.argmax(np.asarray(n_data[::-1], dtype=float)))
    best_idx = len(offsets) - 1 - best_rev
    return int(offsets[best_idx])

def _window_rwi_stats_dplr_like(
    win: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
    method: str = "spearman",
    min_corr_overlap: int = 30,
    zero_is_missing: bool = True,
) -> Dict[str, float]:
    """Compute dplR-like rwi.stats values for one window."""
    x = win.copy().astype(float)
    if zero_is_missing:
        x = x.mask(x == 0.0)
    x = x.dropna(axis=1, how="all")
    n_cores_total = int(x.shape[1])
    if n_cores_total < 2:
        return {
            "n.cores": float(n_cores_total),
            "n.trees": float(n_cores_total),
            "n": float("nan"),
            "n.tot": 0.0,
            "n.wt": 0.0,
            "n.bt": 0.0,
            "rbar.tot": float("nan"),
            "rbar.wt": float("nan"),
            "rbar.bt": float("nan"),
            "c.eff": 0.0,
            "rbar.eff": float("nan"),
            "eps": float("nan"),
            "snr": float("nan"),
        }
    x2, ids3 = _prepare_ids_dplr_like(x, ids=ids)
    arr = x2.to_numpy(dtype=float)
    rwi_not_na = np.isfinite(arr)
    tree_ids = ids3["tree"].to_numpy(dtype=int)
    unique_trees = pd.unique(tree_ids)
    n_trees = int(len(unique_trees))
    if n_trees < 2:
        return {
            "n.cores": float(int(x2.shape[1])),
            "n.trees": float(n_trees),
            "n": float("nan"),
            "n.tot": 0.0,
            "n.wt": 0.0,
            "n.bt": 0.0,
            "rbar.tot": float("nan"),
            "rbar.wt": float("nan"),
            "rbar.bt": float("nan"),
            "c.eff": 0.0,
            "rbar.eff": float("nan"),
            "eps": float("nan"),
            "snr": float("nan"),
        }

    cores_of_tree = [np.flatnonzero(tree_ids == tree_id) for tree_id in unique_trees]
    rsum_bt = 0.0
    n_bt = 0
    good_flag = np.zeros(n_trees, dtype=bool)
    for i in range(n_trees - 1):
        i_data = arr[:, cores_of_tree[i]]
        for j in range(i + 1, n_trees):
            j_data = arr[:, cores_of_tree[j]]
            bt_r_mat = _cor_with_limit_dplr_like(
                int(min_corr_overlap),
                i_data,
                j_data,
                method=method,
            )
            bt_r_vals = bt_r_mat[np.isfinite(bt_r_mat)]
            n_bt_temp = int(bt_r_vals.size)
            if n_bt_temp > 0:
                rsum_bt += float(np.sum(bt_r_vals))
                n_bt += n_bt_temp
                good_flag[[i, j]] = True

    good_trees = np.flatnonzero(good_flag)
    rsum_wt = 0.0
    n_wt = 0
    n_cores_tree = np.full(n_trees, np.nan, dtype=float)
    for i in good_trees:
        these_cores = cores_of_tree[i]
        if int(len(these_cores)) == 1:
            n_cores_tree[i] = 1.0
        else:
            these_data = arr[:, these_cores]
            wt_r_vec = _cor_with_limit_upper_dplr_like(
                int(min_corr_overlap),
                these_data,
                method=method,
            )
            wt_r_vals = wt_r_vec[np.isfinite(wt_r_vec)]
            n_wt_temp = int(wt_r_vals.size)
            if n_wt_temp > 0:
                rsum_wt += float(np.sum(wt_r_vals))
                n_wt += n_wt_temp
                n_cores_tree[i] = 0.5 + math.sqrt(0.25 + 2.0 * n_wt_temp)
            else:
                n_cores_tree[i] = 1.0

    n_tot = int(n_wt + n_bt)
    rbar_tot = float((rsum_wt + rsum_bt) / n_tot) if n_tot > 0 else float("nan")
    rbar_wt = float(rsum_wt / n_wt) if n_wt > 0 else float("nan")
    rbar_bt = float(rsum_bt / n_bt) if n_bt > 0 else float("nan")

    cores_present = np.flatnonzero(np.any(rwi_not_na, axis=0))
    trees_present = pd.unique(tree_ids[cores_present]) if int(cores_present.size) > 0 else np.array([], dtype=int)
    n_cores_present = int(len(cores_present))
    n_trees_present = int(len(trees_present))
    n_good_trees = int(len(good_trees))

    if n_wt == 0:
        c_eff = 1.0 if n_bt > 0 else 0.0
        rbar_eff = rbar_bt
    else:
        n_cores_tree_non_na = n_cores_tree[np.isfinite(n_cores_tree)]
        unique_nc = np.unique(n_cores_tree_non_na)
        if int(len(unique_nc)) == 1:
            c_eff = float(unique_nc[0])
            rbar_eff = float(rbar_bt / (rbar_wt + (1.0 - rbar_wt) / c_eff))
        else:
            c_eff_rproc = float(np.mean(1.0 / n_cores_tree_non_na))
            c_eff = float(1.0 / c_eff_rproc)
            rbar_eff = float(rbar_bt / (rbar_wt + (1.0 - rbar_wt) * c_eff_rproc))

    if np.isfinite(rbar_tot) and abs(float(rbar_tot)) > 1.0:
        rbar_tot = float("nan")
    if np.isfinite(rbar_wt) and abs(float(rbar_wt)) > 1.0:
        rbar_wt = float("nan")
    if np.isfinite(rbar_bt) and abs(float(rbar_bt)) > 1.0:
        rbar_bt = float("nan")

    if (not np.isfinite(rbar_eff)) or float(rbar_eff) <= 0.0 or float(rbar_eff) >= 1.0:
        eps = float("nan")
        snr = float("nan")
    else:
        eps = float(n_good_trees * rbar_eff / (((n_good_trees - 1.0) * rbar_eff) + 1.0))
        snr = float(n_good_trees * rbar_eff / (1.0 - rbar_eff)) if rbar_eff != 1.0 else float("nan")

    return {
        "n.cores": float(n_cores_present),
        "n.trees": float(n_trees_present),
        "n": float(n_good_trees),
        "n.tot": float(n_tot),
        "n.wt": float(n_wt),
        "n.bt": float(n_bt),
        "rbar.tot": float(rbar_tot) if np.isfinite(rbar_tot) else float("nan"),
        "rbar.wt": float(rbar_wt) if np.isfinite(rbar_wt) else float("nan"),
        "rbar.bt": float(rbar_bt) if np.isfinite(rbar_bt) else float("nan"),
        "c.eff": float(c_eff),
        "rbar.eff": float(rbar_eff) if np.isfinite(rbar_eff) else float("nan"),
        "eps": float(eps) if np.isfinite(eps) else float("nan"),
        "snr": float(snr) if np.isfinite(snr) else float("nan"),
    }

def _rwi_stats_running_dplr_like(
    rwi: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
    running_window: bool = True,
    window_length: Optional[int] = None,
    window_overlap: Optional[int] = None,
    min_corr_overlap: Optional[int] = None,
    method: str = "spearman",
    zero_is_missing: bool = True,
) -> pd.DataFrame:
    """Approximate dplR::rwi.stats.running with ids support and default settings."""
    df = _ensure_unique_sorted_year_index(rwi.copy().astype(float))
    if zero_is_missing:
        df = df.mask(df == 0.0)
    df = df.dropna(axis=1, how="all")
    if df.empty or df.shape[1] < 2:
        return pd.DataFrame()
    df, ids2 = _prepare_ids_dplr_like(df, ids=ids)

    n_years = int(df.shape[0])
    wl = int(min(50, n_years) if window_length is None else window_length)
    wo = int(math.floor(wl / 2) if window_overlap is None else window_overlap)
    mo = int(min(30, wl) if min_corr_overlap is None else min_corr_overlap)
    if wl < 3 or wl > n_years or wo >= wl or mo > wl:
        return pd.DataFrame()

    years = df.index.to_numpy(dtype=int)
    valid_mask = np.isfinite(df.to_numpy(dtype=float))
    n_trees_by_year = np.sum(valid_mask, axis=1)
    good_rows_flag = n_trees_by_year > 1
    if int(np.sum(good_rows_flag)) < mo:
        return pd.DataFrame()

    rows: list[Dict[str, float]] = []
    if running_window:
        first_start = _choose_first_window_start_dplr_like(
            valid_mask=valid_mask,
            window_length=wl,
            window_overlap=wo,
            min_corr_overlap=mo,
        )
        step = int(wl - wo)
        starts = list(range(int(first_start), n_years - wl + 1, step))
    else:
        starts = [0]
        wl = n_years

    for s in starts:
        e = int(s + wl - 1)
        win = df.iloc[s : e + 1, :]
        stats_row = _window_rwi_stats_dplr_like(
            win,
            ids=ids2,
            method=method,
            min_corr_overlap=mo,
            zero_is_missing=zero_is_missing,
        )
        if running_window:
            stats_row = {
                "start.year": float(years[s]),
                "mid.year": float(math.floor((int(years[s]) + int(years[e])) / 2)),
                "end.year": float(years[e]),
                **stats_row,
            }
        rows.append(stats_row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    if running_window:
        out["rbar"] = out["rbar.tot"]
        out["depth"] = out["n.trees"]
        out["n_series_win"] = out["n.cores"]
        out["n_years_win"] = out["end.year"] - out["start.year"] + 1.0
        out.index = out["mid.year"].astype(int)
        out.index.name = "year"
    return out
def compute_eps_rbar_depth_timeseries(
    rwi: pd. DataFrame,
    window: int = 50,
    ids: Optional[pd.DataFrame] = None,
    min_overlap: Optional[int] = None,
) -> pd.DataFrame:
    """Compute dplR-like running EPS/Rbar statistics.

    Notes:
        - This now follows the rwi.stats.running defaults much more closely:
          Spearman correlations, zeros treated as missing, window overlap of half
          the window length, and the dplR EPS formula based on rbar.eff.
        - The returned table also keeps convenience aliases used elsewhere in
          this script: rbar, depth, n_series_win, and n_years_win.
    """
    if rwi is None or rwi.empty:
        return pd.DataFrame(
            columns=[
                "start.year",
                "mid.year",
                "end.year",
                "n.cores",
                "n.trees",
                "n",
                "n.tot",
                "n.wt",
                "n.bt",
                "rbar.tot",
                "rbar.wt",
                "rbar.bt",
                "c.eff",
                "rbar.eff",
                "eps",
                "snr",
                "rbar",
                "depth",
                "n_series_win",
                "n_years_win",
            ]
        )
    return _rwi_stats_running_dplr_like(
        rwi,
        ids=ids,
        running_window=True,
        window_length=int(window),
        window_overlap=int(math.floor(window / 2)),
        min_corr_overlap=int(
            max(
                3,
                min(
                    int(window),
                    int(min(30, window) if min_overlap is None else min_overlap),
                ),
            )
        ),
        method="spearman",
        zero_is_missing=True,
    )

def sanitize_eps_rbar_depth_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Sanitize EPS/Rbar diagnostic output for plotting and scoring."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "eps" in out.columns:
        eps_vals = pd.to_numeric(out["eps"], errors="coerce")
        eps_bad = (~np.isfinite(eps_vals)) | (eps_vals < 0.0) | (eps_vals > 1.0)
        out.loc[eps_bad, "eps"] = np.nan
    for col in ("rbar", "rbar.tot", "rbar.bt", "rbar.wt"):
        if col in out.columns:
            r_vals = pd.to_numeric(out[col], errors="coerce")
            r_bad = (~np.isfinite(r_vals)) | (np.abs(r_vals) > 1.0)
            out.loc[r_bad, col] = np.nan
    if "rbar.eff" in out.columns:
        re_vals = pd.to_numeric(out["rbar.eff"], errors="coerce")
        re_bad = (~np.isfinite(re_vals)) | (re_vals <= 0.0) | (re_vals >= 1.0)
        out.loc[re_bad, "rbar.eff"] = np.nan
    if "depth" in out.columns:
        d_vals = pd.to_numeric(out["depth"], errors="coerce")
        d_bad = (~np.isfinite(d_vals)) | (d_vals < 0.0)
        out.loc[d_bad, "depth"] = np.nan
    return out
# ---------------------------------------------------------------------------
# Detrending methods
# ---------------------------------------------------------------------------
def _safe_ratio(y: np.ndarray, trend: np.ndarray, min_frac: float = 1e-3) -> np.ndarray:
    """Compute y/trend while avoiding non-positive or near-zero trend values."""
    y = np.asarray(y, dtype=float)
    t = np.asarray(trend, dtype=float).copy()
    finite = np.isfinite(t) & (t > 0)
    if not np.any(finite):
        return np.full_like(y, np.nan, dtype=float)
    y_finite = np.isfinite(y) & (y > 0)
    t_scale = float(np.nanmedian(t[finite]))
    y_scale = float(np.nanmedian(y[y_finite])) if np.any(y_finite) else t_scale
    scale = float(max(t_scale, y_scale))
    floor = max(scale * float(min_frac), float(np.finfo(float).tiny))
    t[~finite] = np.nan
    t[t < floor] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        out = y / t
    return out

def _recode_zeros_series(series: pd.Series, enabled: bool) -> pd.Series:
    """Apply dplR-style 0 -> 0.001 recoding on a single series."""
    if not enabled:
        return series.astype(float).copy()
    out = series.astype(float).copy()
    vals = out.to_numpy(dtype=float)
    zero_mask = np.isfinite(vals) & (vals == 0.0)
    if np.any(zero_mask):
        vals[zero_mask] = 0.001
        out = pd.Series(vals, index=series.index)
    return out

def _apply_blockwise_detrend(
    series: pd.Series,
    detrend_func,
    recode_zeros: bool,
) -> pd.Series:
    """Apply a detrending function to each contiguous non-NA block separately."""
    src = series.astype(float)
    out = pd.Series(np.full(src.shape[0], np.nan, dtype=float), index=series.index)
    vals = src.to_numpy(dtype=float)
    valid = np.flatnonzero(np.isfinite(vals))
    if valid.size == 0:
        return out
    split_at = np.where(np.diff(valid) != 1)[0] + 1
    for block in np.split(valid, split_at):
        if block.size == 0:
            continue
        block_series = src.iloc[block].copy()
        block_series = _recode_zeros_series(block_series, recode_zeros)
        out.iloc[block] = detrend_func(block_series).to_numpy(dtype=float)
    return out

def _safe_difference(y: np.ndarray, trend: np.ndarray) -> np.ndarray:
    """Compute y - trend while preserving NaN positions."""
    y = np.asarray(y, dtype=float)
    t = np.asarray(trend, dtype=float)
    out = np.full_like(y, np.nan, dtype=float)
    mask = np.isfinite(y) & np.isfinite(t)
    out[mask] = y[mask] - t[mask]
    return out

def _finalize_detrended_values(
    y: np.ndarray,
    trend: np.ndarray,
    difference: bool,
) -> np.ndarray:
    """Return either ratio or difference detrended values."""
    if difference:
        return _safe_difference(y, trend)
    return _safe_ratio(y, trend)


def _safe_difference(y: np.ndarray, trend: np.ndarray) -> np.ndarray:
    """Compute y - trend while keeping invalid locations as NaN."""
    y = np.asarray(y, dtype=float)
    t = np.asarray(trend, dtype=float)
    out = np.full_like(y, np.nan, dtype=float)
    mask = np.isfinite(y) & np.isfinite(t)
    out[mask] = y[mask] - t[mask]
    return out


def _index_from_trend(
    y: np.ndarray,
    trend: np.ndarray,
    difference: bool = False,
) -> np.ndarray:
    """Build either ratio or difference indices from a fitted trend."""
    if difference:
        return _safe_difference(y, trend)
    return _safe_ratio(y, trend)


def _series_segments(mask: np.ndarray) -> list[np.ndarray]:
    """Return contiguous valid segments from a boolean mask."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    split_at = np.where(np.diff(idx) != 1)[0] + 1
    return [seg.astype(int) for seg in np.split(idx, split_at) if seg.size > 0]


def _get_po_vector(
    rwl: pd.DataFrame,
    po: Optional[pd.DataFrame] = None,
) -> Dict[str, int]:
    """Build a pith-offset lookup with dplR-like defaults."""
    out = {str(col): 1 for col in rwl.columns}
    if po is None or po.empty:
        return out
    po_use = po.copy()
    series_col = "series" if "series" in po_use.columns else po_use.columns[0]
    if "offset" in po_use.columns:
        offset_col = "offset"
    elif "pith.offset" in po_use.columns:
        offset_col = "pith.offset"
    else:
        offset_col = po_use.columns[1]
    for _, row in po_use.iterrows():
        sid = str(row[series_col])
        if sid not in out:
            continue
        try:
            val = int(float(row[offset_col]))
        except Exception:
            val = 1
        out[sid] = max(val, 1)
    return out

def _po_lookup_raw(po: Optional[pd.DataFrame]) -> Dict[str, float]:
    """Pith offsets by series; NaN means unknown pith (not silently 1)."""
    out: Dict[str, float] = {}
    if po is None or getattr(po, "empty", True):
        return out
    series_col = "series" if "series" in po.columns else po.columns[0]
    if "pith.offset" in po.columns:
        offset_col = "pith.offset"
    elif "offset" in po.columns:
        offset_col = "offset"
    else:
        offset_col = po.columns[1]
    for _, row in po.iterrows():
        try:
            val = float(row[offset_col])
        except Exception:
            val = float("nan")
        out[str(row[series_col])] = val if (np.isfinite(val) and val >= 1) else float("nan")
    return out


def _prepare_po_for_run(
    rwl: pd.DataFrame,
    po: Optional[pd.DataFrame],
    unknown_pith_policy: str = "present",
) -> tuple[Optional[pd.DataFrame], list[str]]:
    """Return (po without NaN, series to exclude from age-based methods).

    Unknown pith (NaN) becomes offset 1 for the computation. With the policy
    "exclude" those series are also returned so RCS can leave them out.
    """
    if po is None or getattr(po, "empty", True):
        return None, []
    raw = _po_lookup_raw(po)
    rows = []
    excluded: list[str] = []
    for col in [str(c) for c in rwl.columns]:
        val = raw.get(col, float("nan"))
        if not np.isfinite(val):
            if str(unknown_pith_policy) == "exclude":
                excluded.append(col)
            val = 1.0
        rows.append({"series": col, "pith.offset": int(round(val))})
    return pd.DataFrame(rows, columns=["series", "pith.offset"]), excluded


def _align_ids_to_series_names(
    ids: Optional[pd.DataFrame],
    series_names: list[str],
) -> Optional[pd.DataFrame]:
    """Align an ids table to one explicit ordered list of series names."""
    if ids is None or (not isinstance(ids, pd.DataFrame)) or ids.empty:
        return None
    ids2 = ids.copy()
    target = pd.Index([str(x) for x in series_names], dtype="object")
    rownames_ids = ids2.index.astype(str)
    if len(rownames_ids) > 0 and all(name in set(rownames_ids) for name in target):
        ids2 = ids2.loc[target, :].copy()
        ids2.index = target
        return ids2
    if int(ids2.shape[0]) != int(len(target)):
        return None
    ids2 = ids2.iloc[: len(target), :].copy()
    ids2.index = target
    return ids2

def _collapse_rwl_to_tree_means(
    rwl: pd.DataFrame,
    ids: Optional[pd.DataFrame] = None,
    po: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    """Collapse a series matrix to one detrending series per tree."""
    if rwl is None or rwl.empty:
        return rwl.copy(), ids, po

    series_names = [str(x) for x in list(rwl.columns)]
    ids2 = _align_ids_to_series_names(ids, series_names)
    if ids2 is None or ids2.empty or "tree" not in ids2.columns:
        return rwl.copy(), ids2, po

    ids2 = ids2.copy()
    if "core" not in ids2.columns:
        ids2["core"] = 1
    ids2["tree"] = pd.to_numeric(ids2["tree"], errors="coerce")
    ids2["core"] = pd.to_numeric(ids2["core"], errors="coerce").fillna(1).astype(int)
    if not np.any(np.isfinite(ids2["tree"].to_numpy(dtype=float))):
        return rwl.copy(), ids2.loc[:, ["tree", "core"]].copy(), po

    ids2 = ids2.loc[np.isfinite(ids2["tree"].to_numpy(dtype=float)), :].copy()
    ids2["tree"] = ids2["tree"].astype(int)

    tree_sizes = ids2.groupby("tree").size()
    if tree_sizes.empty or int(tree_sizes.max()) <= 1:
        return rwl.copy(), ids2.loc[:, ["tree", "core"]].copy(), po

    po_raw = _po_lookup_raw(po)
    tree_cols: Dict[str, pd.Series] = {}
    tree_id_rows: list[Dict[str, Any]] = []
    tree_po_rows: list[Dict[str, Any]] = []

    for tree_id, grp in ids2.groupby("tree", sort=True):
        member_names = set(pd.Index(grp.index).astype(str))
        member_cols = [col for col in list(rwl.columns) if str(col) in member_names]
        if not member_cols:
            continue

        block = rwl.loc[:, member_cols].astype(float)
        tree_mean = block.mean(axis=1, skipna=True)
        tree_mean[block.notna().sum(axis=1) == 0] = np.nan

        tree_name = f"TREE_{int(tree_id)}"
        tree_cols[tree_name] = tree_mean.astype(float)
        tree_id_rows.append(
            {
                "series": tree_name,
                "tree": int(tree_id),
                "core": 1,
            }
        )

        # Each radius with known pith gives a pith year (first year - offset + 1);
        # the tree offset is counted from the first year of the tree mean.
        valid_tree = tree_mean.dropna()
        member_first: list[int] = []
        member_off: list[float] = []
        for col in member_cols:
            ser_m = block[col].dropna()
            if ser_m.empty:
                continue
            member_first.append(int(ser_m.index.min()))
            member_off.append(float(po_raw.get(str(col), float("nan"))))
        tree_off: float = float("nan")
        if po is not None and not valid_tree.empty:
            pith_years = [fy - int(round(off)) + 1 for fy, off in zip(member_first, member_off) if np.isfinite(off)]
            if pith_years:
                pith_year = int(round(float(np.median(pith_years))))
                tree_off = float(max(1, int(valid_tree.index.min()) - pith_year + 1))
        elif po is None:
            tree_off = 1.0
        tree_po_rows.append(
            {
                "series": tree_name,
                "pith.offset": tree_off,
            }
        )

    if not tree_cols:
        return rwl.copy(), ids2.loc[:, ["tree", "core"]].copy(), po

    rwl_tree = pd.DataFrame(tree_cols, index=rwl.index, dtype=float)
    ids_tree = pd.DataFrame(tree_id_rows).set_index("series")[["tree", "core"]]
    po_tree = pd.DataFrame(tree_po_rows, columns=["series", "pith.offset"]) if tree_po_rows else None
    return rwl_tree, ids_tree, po_tree


def _series_cambial_ages(
    series: pd.Series,
    offset: int = 1,
) -> pd.Series:
    """Map each observed year to cambial age using a pith offset."""
    ser = series.dropna()
    if ser.empty:
        return pd.Series(dtype=float)
    ages = np.arange(1, len(ser) + 1, dtype=int) + int(offset) - 1
    return pd.Series(ages, index=ser.index, dtype=int)


def _compute_cambial_age_frame(
    rwl: pd.DataFrame,
    po: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Create a cambial-age aligned matrix like dplR::rcs."""
    po_vec = _get_po_vector(rwl, po=po)
    n_series = int(rwl.shape[1])
    if n_series == 0:
        return pd.DataFrame()
    max_len = int(rwl.notna().sum(axis=0).max()) if int(rwl.shape[0]) > 0 else 0
    max_po = max(po_vec.values()) if po_vec else 1
    n_rows = int(max_len + max_po)
    out = pd.DataFrame(
        np.full((n_rows, n_series), np.nan, dtype=float),
        index=np.arange(1, n_rows + 1, dtype=int),
        columns=rwl.columns,
    )
    for col in rwl.columns:
        ser = rwl[col].dropna()
        if ser.empty:
            continue
        start_age = int(max(po_vec.get(str(col), 1), 1))
        end_age = start_age + int(len(ser)) - 1
        out.loc[start_age:end_age, col] = ser.to_numpy(dtype=float)
    out.index.name = "age"
    return out


def _build_rcs_curve(
    rwl: pd.DataFrame,
    po: Optional[pd.DataFrame] = None,
    nyrs: Optional[int] = None,
    f: float = 0.5,
    biweight: bool = True,
) -> pd.Series:
    """Approximate dplR::rcs regional curve in pure Python."""
    rwca = _compute_cambial_age_frame(rwl, po=po)
    if rwca.empty:
        return pd.Series(dtype=float, name="rcs_curve")

    if biweight:
        ca_mean = rwca.apply(lambda row: build_chronology(pd.DataFrame([row])).iloc[0], axis=1)
    else:
        ca_mean = rwca.mean(axis=1, skipna=True)
    ca_mean = ca_mean.astype(float)
    valid = ca_mean.dropna()
    if valid.empty:
        return pd.Series(dtype=float, name="rcs_curve")

    if nyrs is None:
        nyrs2 = int(max(3, math.floor(float(valid.shape[0]) * 0.1)))
    else:
        nyrs2 = int(max(3, nyrs))

    trend_vals = _cook_peters_spline_trend(
        valid.to_numpy(dtype=float),
        target_years=nyrs2,
        frequency_cutoff=float(f),
        rigidity_scale=1.0,
    )
    if trend_vals is None:
        rc = valid.copy()
    else:
        rc = pd.Series(trend_vals, index=valid.index, dtype=float)
    rc = rc.reindex(ca_mean.index).astype(float)
    rc.name = "rcs_curve"
    return rc


def _apply_rcs_curve_to_frame(
    rwl: pd.DataFrame,
    rc_curve: pd.Series,
    po: Optional[pd.DataFrame] = None,
    difference: bool = False,
) -> pd.DataFrame:
    """Apply a regional curve to each series using cambial age alignment."""
    po_vec = _get_po_vector(rwl, po=po)
    out = pd.DataFrame(np.nan, index=rwl.index, columns=rwl.columns, dtype=float)
    rc = rc_curve.astype(float)
    for col in rwl.columns:
        ser = rwl[col].astype(float)
        valid = ser.dropna()
        if valid.empty:
            continue
        ages = np.arange(1, len(valid) + 1, dtype=int) + int(max(po_vec.get(str(col), 1), 1)) - 1
        trend = pd.Series(np.nan, index=ser.index, dtype=float)
        for year, age in zip(valid.index, ages):
            if age in rc.index:
                trend.loc[year] = float(rc.loc[age])
        out[col] = _index_from_trend(
            ser.to_numpy(dtype=float),
            trend.to_numpy(dtype=float),
            difference=difference,
        )
    return out


def _high_freq_residual_chronology(
    chron: pd.Series,
    target_years: int,
) -> np.ndarray:
    """Return high-frequency chronology residuals for SSF convergence."""
    vals = chron.to_numpy(dtype=float)
    out = np.full_like(vals, np.nan, dtype=float)
    good = np.isfinite(vals)
    if int(good.sum()) == 0:
        return out
    hp = _cook_peters_spline_trend(
        vals[good],
        target_years=max(int(target_years), 3),
        frequency_cutoff=0.5,
        rigidity_scale=1.0,
    )
    if hp is None:
        out[good] = vals[good]
    else:
        out[good] = vals[good] - hp
    return out

def _cook_peters_lambda(
    target_years: int,
    frequency_cutoff: float = 0.5,
) -> float:
    """Convert a target wavelength to a cubic-smoother penalty.

    Notes:
        This uses a discrete smoothing-spline frequency-response approximation
        so that the low-pass gain is close to `frequency_cutoff` at the target
        wavelength. It is intended to behave closer to dplR/ARSTAN-style spline
        detrending than the generic UnivariateSpline smoothing factor.
    """
    period = max(float(target_years), 3.0)
    keep = min(max(float(frequency_cutoff), 1e-6), 1.0 - 1e-6)
    omega = 2.0 * math.pi / period
    penalty = (2.0 - 2.0 * math.cos(omega)) ** 2
    if (not np.isfinite(penalty)) or penalty <= 0.0:
        return 0.0
    return float((1.0 / keep - 1.0) / penalty)

def _second_diff_penalty_banded(n: int) -> np.ndarray:
    """Return the upper-banded form of D' D for second differences."""
    ab = np.zeros((3, n), dtype=float)
    if n <= 0:
        return ab
    if n == 1:
        ab[2, 0] = 0.0
        return ab
    if n == 2:
        ab[2, :] = 0.0
        return ab

    main = np.zeros(n, dtype=float)
    off1 = np.zeros(max(n - 1, 0), dtype=float)
    off2 = np.zeros(max(n - 2, 0), dtype=float)

    if n == 3:
        main[:] = np.array([1.0, 4.0, 1.0], dtype=float)
        off1[:] = np.array([-2.0, -2.0], dtype=float)
        off2[:] = np.array([1.0], dtype=float)
    else:
        main[0] = 1.0
        main[-1] = 1.0
        main[1] = 5.0
        main[-2] = 5.0
        if n > 4:
            main[2:-2] = 6.0

        off1[:] = -4.0
        off1[0] = -2.0
        off1[-1] = -2.0
        off2[:] = 1.0

    ab[2, :] = main
    if n >= 2:
        ab[1, 1:] = off1
    if n >= 3:
        ab[0, 2:] = off2
    return ab

def _cook_peters_spline_trend(
    y: np.ndarray,
    target_years: int,
    frequency_cutoff: float = 0.5,
    rigidity_scale: float = 1.0,
) -> Optional[np.ndarray]:
    """Fit a Cook-Peters-like penalized cubic smoother to one contiguous block."""
    y = np.asarray(y, dtype=float)
    if y.ndim != 1 or y.size < 4 or np.any(~np.isfinite(y)):
        return None

    lam = _cook_peters_lambda(target_years, frequency_cutoff=frequency_cutoff)
    lam *= max(float(rigidity_scale), 1e-6)
    if (not np.isfinite(lam)) or lam < 0.0:
        return None

    n = int(y.size)
    penalty_ab = _second_diff_penalty_banded(n)
    system_ab = penalty_ab.copy()
    system_ab *= lam
    system_ab[2, :] += 1.0

    try:
        trend = solveh_banded(system_ab, y, lower=False, check_finite=False)
    except Exception:
        return None

    if trend is None or np.any(~np.isfinite(trend)):
        return None
    return np.asarray(trend, dtype=float)

def _fit_spline_trend_block(
    series: pd.Series,
    target_years: int,
    smoothing_strength: float = 1.0,
) -> pd.Series:
    """Fit only the spline trend for one contiguous block."""
    y = series.to_numpy(dtype=float)
    mask = np.isfinite(y)
    if int(mask.sum()) < 4:
        return pd.Series(np.full_like(y, np.nan), index=series.index)
    y_fit = y[mask]
    trend = np.full_like(y, np.nan, dtype=float)
    cp_trend = _cook_peters_spline_trend(
        y_fit,
        target_years=target_years,
        frequency_cutoff=0.5,
        rigidity_scale=smoothing_strength,
    )
    if cp_trend is not None:
        trend[mask] = cp_trend
    else:
        x_fit = np.arange(1, len(y_fit) + 1, dtype=float)
        n = int(mask.sum())
        rigidity = max(float(target_years), 1.0) / max(float(n), 1.0)
        base_s = float(np.nanvar(y_fit)) * max(float(n), 1.0)
        s = smoothing_strength * base_s * max(rigidity, 1e-6)
        if (not np.isfinite(s)) or s <= 0:
            s = None
        try:
            k = int(min(3, max(1, len(x_fit) - 1)))
            spl = UnivariateSpline(x_fit, y_fit, s=s, k=k)
            trend[mask] = spl(x_fit)
        except Exception:
            mu = float(np.nanmean(y_fit))
            trend[mask] = mu
    if np.any(~np.isfinite(trend[mask])) or np.any(trend[mask] <= 0.0):
        mu = float(np.nanmean(y_fit))
        trend[mask] = mu
    return pd.Series(trend, index=series.index)

def _apply_blockwise_trend(
    series: pd.Series,
    trend_func,
    recode_zeros: bool,
) -> pd.Series:
    """Apply a trend-fitting function to each contiguous non-NA block separately."""
    src = series.astype(float)
    out = pd.Series(np.full(src.shape[0], np.nan, dtype=float), index=series.index)
    vals = src.to_numpy(dtype=float)
    valid = np.flatnonzero(np.isfinite(vals))
    if valid.size == 0:
        return out
    split_at = np.where(np.diff(valid) != 1)[0] + 1
    for block in np.split(valid, split_at):
        if block.size == 0:
            continue
        block_series = src.iloc[block].copy()
        block_series = _recode_zeros_series(block_series, recode_zeros)
        out.iloc[block] = trend_func(block_series).to_numpy(dtype=float)
    return out

def _build_mean_chronology(rwi: pd.DataFrame) -> pd.Series:
    """Build a simple arithmetic-mean chronology."""
    return rwi.mean(axis=1, skipna=True)

def _build_biweight_or_mean_chronology(rwi: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Build chronology and sample depth with dplR-like zero fallback."""
    chron = build_chronology(rwi)
    samp_depth = rwi.notna().sum(axis=1).astype(float)
    if np.any(np.nan_to_num(chron.to_numpy(dtype=float), nan=0.0) == 0.0):
        chron = _build_mean_chronology(rwi)
    return chron.astype(float), samp_depth

def _restore_signal_free_mean(
    sf_values: np.ndarray,
    raw_values: np.ndarray,
    difference: bool,
) -> np.ndarray:
    """Restore the per-series mean after common-signal removal using dplR-like logic."""
    out = np.asarray(sf_values, dtype=float).copy()
    raw = np.asarray(raw_values, dtype=float)
    raw_mask = np.isfinite(raw)
    sf_mask = np.isfinite(out)
    if (not np.any(raw_mask)) or (not np.any(sf_mask)):
        return out
    raw_mean = float(np.nanmean(raw[raw_mask]))
    sf_mean = float(np.nanmean(out[sf_mask]))
    if (not np.isfinite(raw_mean)) or (not np.isfinite(sf_mean)):
        return out
    if difference:
        out[sf_mask] = out[sf_mask] + (raw_mean - sf_mean)
    else:
        if sf_mean != 0.0:
            out[sf_mask] = out[sf_mask] * (raw_mean / sf_mean)
    return out

def neg_exp_func(x: np.ndarray, a: float, b: float, k: float) -> np.ndarray:
    """Negative exponential function."""
    return a * np.exp(b * x) + k
def detrend_negexp(
    series: pd.Series,
    difference: bool = False,
    pos_slope: bool = False,
    constrain_nls: ConstrainNLS = "never",
) -> pd.Series:
    """Detrend a series by dplR-like ModNegExp with fallback logic."""
    y = series.to_numpy(dtype=float)
    x = np.arange(1, len(y) + 1, dtype=float)
    mask = ~np.isnan(y)
    if mask.sum() < 3:
        return pd. Series(np.full_like(y, np.nan), index=series.index)
    x_fit, y_fit = x[mask], y[mask]
    k0 = float(max(np.nanmin(y_fit), 0.0))
    a0 = float(max(np.nanmax(y_fit) - k0, np.nanmax(y_fit) * 0.1, 1e-6))
    b0 = -0.01
    p0 = (a0, b0, k0)

    def _valid_trend(trend: Optional[np.ndarray]) -> bool:
        if trend is None:
            return False
        if np.any(~np.isfinite(trend[mask])):
            return False
        if np.any(trend[mask] <= 0.0):
            return False
        if (not pos_slope) and float(trend[mask][-1]) > float(trend[mask][0]):
            return False
        return True

    def _fit_negexp(use_bounds: bool) -> Optional[np.ndarray]:
        try:
            kwargs: Dict[str, Any] = {"p0": p0, "maxfev": 50000}
            if use_bounds:
                kwargs["bounds"] = ([0.0, -1.0, 0.0], [np.inf, 0.0, np.inf])
            with warnings.catch_warnings():
                warnings.simplefilter("error", OptimizeWarning)
                params, _ = curve_fit(neg_exp_func, x_fit, y_fit, **kwargs)
            return neg_exp_func(x, *params)
        except Exception:
            return None

    trend: Optional[np.ndarray] = None
    if constrain_nls == "always":
        trend = _fit_negexp(use_bounds=True)
    elif constrain_nls == "when.fail":
        trend = _fit_negexp(use_bounds=False)
        if not _valid_trend(trend):
            trend = _fit_negexp(use_bounds=True)
    else:
        trend = _fit_negexp(use_bounds=False)

    if not _valid_trend(trend):
        coeffs = np.polyfit(x_fit, y_fit, 1)
        slope = float(coeffs[0])
        trend = np.full_like(y, np.nan, dtype=float)
        trend[mask] = np.polyval(coeffs, x_fit)
        if ((not pos_slope) and slope > 0.0) or np.any(~np.isfinite(trend[mask])) or np.any(trend[mask] <= 0.0):
            mu = float(np.nanmean(y_fit))
            trend[mask] = mu

    idx = _index_from_trend(y, trend, difference=difference)
    return pd.Series(idx, index=series.index)
def detrend_linear(
    series: pd.Series,
    pos_slope: bool = False,
    difference: bool = False,
) -> pd.Series:
    """Detrend a series by dplR-like linear regression in time."""
    y = series.to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    mask = ~np.isnan(y)
    if mask.sum() < 2:
        return pd.Series(np.full_like(y, np.nan), index=series.index)
    x_fit, y_fit = x[mask], y[mask]
    coeffs = np.polyfit(x_fit, y_fit, 1)
    slope = float(coeffs[0])
    trend = np.full_like(y, np.nan, dtype=float)
    trend[mask] = np.polyval(coeffs, x_fit)
    if (not pos_slope) and np.isfinite(slope) and slope > 0:
        mu = float(np.nanmean(y_fit))
        trend[mask] = mu
    if np.any(trend[mask] <= 0):
        mu = float(np.nanmean(y_fit))
        trend[mask] = mu
    idx = _index_from_trend(y, trend, difference=difference)
    return pd.Series(idx, index=series.index)
def detrend_mean(series: pd. Series, difference: bool = False) -> pd. Series:
    """Detrend by dividing by the mean value (horizontal line)."""
    y = series.to_numpy(dtype=float)
    mask = ~np.isnan(y)
    if mask.sum() == 0:
        return pd. Series(np.full_like(y, np.nan), index=series.index)
    m = float(np.nanmean(y[mask]))
    if m == 0 or np.isnan(m):
        return pd. Series(np.full_like(y, np.nan), index=series.index)
    trend = np.full_like(y, m)
    idx = _finalize_detrended_values(y, trend, difference=difference)
    return pd. Series(idx, index=series.index)
def detrend_running_mean(
    series: pd.Series,
    window: int = 32,
    min_periods: int = 3,
    difference: bool = False,
) -> pd.Series:
    """Detrend using a dplR-harness-like running mean."""
    if window < 3:
        window = 3
    y = series.to_numpy(dtype=float)
    trend = np.full_like(y, np.nan, dtype=float)
    half = int(window // 2)
    for i in range(len(y)):
        left = i - half
        right = left + window - 1
        if left < 0:
            left = 0
            right = min(len(y) - 1, left + window - 1)
        if right >= len(y):
            right = len(y) - 1
            left = max(0, right - window + 1)
        w = y[left : right + 1]
        n_ok = int(np.isfinite(w).sum())
        if n_ok >= int(min_periods):
            trend[i] = float(np.nanmean(w))
    mask = np.isfinite(y) & np.isfinite(trend)
    if np.any(mask) and np.any(trend[mask] <= 0):
        mu = float(np.nanmean(y[np.isfinite(y)]))
        trend[mask] = mu
    idx = _index_from_trend(y, trend, difference=difference)
    return pd.Series(idx, index=series.index)

def _interp_trend_on_x(
    x_fit: np.ndarray,
    y_fit: np.ndarray,
    x_new: np.ndarray,
) -> np.ndarray:
    """Interpolate a fitted trend back to requested x positions."""
    x_fit = np.asarray(x_fit, dtype=float)
    y_fit = np.asarray(y_fit, dtype=float)
    x_new = np.asarray(x_new, dtype=float)
    if x_fit.size == 0 or y_fit.size == 0:
        return np.full_like(x_new, np.nan, dtype=float)
    order = np.argsort(x_fit)
    xx = x_fit[order]
    yy = y_fit[order]
    if xx.size == 1:
        return np.full_like(x_new, yy[0], dtype=float)
    keep = np.concatenate(([True], np.diff(xx) > 0))
    xx = xx[keep]
    yy = yy[keep]
    if xx.size == 1:
        return np.full_like(x_new, yy[0], dtype=float)
    return np.interp(x_new, xx, yy, left=yy[0], right=yy[-1])

def _fit_lowess_sorted_reflected(
    x: np.ndarray,
    y: np.ndarray,
    frac: float,
    it: int = 2,
) -> np.ndarray:
    """Fit LOWESS on reflected edge-padded data and return sorted fitted points."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = int(len(y))
    if n == 0:
        return np.empty((0, 2), dtype=float)
    if n == 1:
        return np.column_stack([x, y]).astype(float)

    frac_use = float(min(max(float(frac), 4.0 / max(float(n), 1.0)), 0.95))
    span = int(max(2, math.ceil(frac_use * n)))
    pad = int(min(max(2, span), max(1, n - 1)))

    if pad <= 1:
        fit_sorted = sm_lowess(
            y,
            x,
            frac=frac_use,
            it=int(max(it, 0)),
            return_sorted=True,
        )
        return np.asarray(fit_sorted, dtype=float)

    left_x = 2.0 * x[0] - x[1 : pad + 1][::-1]
    right_x = 2.0 * x[-1] - x[-pad - 1 : -1][::-1]
    left_y = y[1 : pad + 1][::-1]
    right_y = y[-pad - 1 : -1][::-1]

    x_aug = np.concatenate([left_x, x, right_x]).astype(float)
    y_aug = np.concatenate([left_y, y, right_y]).astype(float)

    fit_sorted = sm_lowess(
        y_aug,
        x_aug,
        frac=frac_use,
        it=int(max(it, 0)),
        return_sorted=True,
    )
    fit_sorted = np.asarray(fit_sorted, dtype=float)
    order = np.argsort(fit_sorted[:, 0])
    return fit_sorted[order]

def _moving_nanmean_1d(values: np.ndarray, window: int) -> np.ndarray:
    """Compute a simple centered moving mean ignoring NaN values."""
    x = np.asarray(values, dtype=float)
    n = int(x.size)
    if n == 0:
        return np.asarray([], dtype=float)
    w = int(max(1, window))
    half = int(w // 2)
    out = np.full(n, np.nan, dtype=float)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        seg = x[lo:hi]
        if np.any(np.isfinite(seg)):
            out[i] = float(np.nanmean(seg))
    return out

def _lowess_supsmu_like_trend(
    x_fit: np.ndarray,
    y_fit: np.ndarray,
) -> Optional[np.ndarray]:
    """Approximate Friedman/supsmu using a multiscale LOWESS blend."""
    if sm_lowess is None:
        return None
    x_fit = np.asarray(x_fit, dtype=float)
    y_fit = np.asarray(y_fit, dtype=float)
    n = int(y_fit.size)
    if n < 4:
        return None

    min_frac = max(4.0 / max(float(n), 1.0), 0.05)
    frac_candidates = sorted(
        {
            round(min(max(v, min_frac), 0.95), 6)
            for v in (
                min_frac,
                0.08,
                0.12,
                0.18,
                0.28,
                0.40,
            )
        }
    )
    if len(frac_candidates) == 1:
        frac_candidates = [frac_candidates[0], min(max(frac_candidates[0] * 1.5, min_frac), 0.95)]

    trend_list: list[np.ndarray] = []
    score_list: list[np.ndarray] = []
    score_window = int(max(3, round(n * 0.08)))
    for frac in frac_candidates:
        fit_sorted = _fit_lowess_sorted_reflected(
            x_fit,
            y_fit,
            frac=float(frac),
            it=2,
        )
        trend = _interp_trend_on_x(fit_sorted[:, 0], fit_sorted[:, 1], x_fit)
        if np.any(~np.isfinite(trend)):
            continue
        resid = np.abs(y_fit - trend)
        local_score = _moving_nanmean_1d(resid, window=score_window)
        trend_list.append(trend.astype(float))
        score_list.append(local_score.astype(float))

    if not trend_list:
        return None
    if len(trend_list) == 1:
        return trend_list[0]

    trend_mat = np.vstack(trend_list)
    score_mat = np.vstack(score_list)
    score_scale = float(np.nanmedian(score_mat[np.isfinite(score_mat)])) if np.any(np.isfinite(score_mat)) else 1.0
    if (not np.isfinite(score_scale)) or score_scale <= 0.0:
        score_scale = 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = 1.0 / np.square(score_mat + 0.05 * score_scale)
    weights[~np.isfinite(weights)] = 0.0
    wsum = np.sum(weights, axis=0)
    out = np.full(n, np.nan, dtype=float)
    ok = wsum > 0.0
    if np.any(ok):
        out[ok] = np.sum(weights[:, ok] * trend_mat[:, ok], axis=0) / wsum[ok]
    return out

def _select_lowess_frac_cv(
    x: np.ndarray,
    y: np.ndarray,
) -> float:
    """Pick a LOWESS fraction using simple deterministic cross-validation."""
    n = int(len(y))
    if n < 6:
        return 0.3
    min_frac = max(4.0 / max(float(n), 1.0), 0.05)

    # For long series, exhaustive CV is a major bottleneck in batch detrending.
    # Use a deterministic heuristic that stays within the admissible LOWESS span.
    if n >= 240:
        return float(min(max(0.10, min_frac), 0.95))
    if n >= 160:
        return float(min(max(0.12, min_frac), 0.95))
    if n >= 100:
        return float(min(max(0.15, min_frac), 0.95))
    if n >= 60:
        return float(min(max(0.20, min_frac), 0.95))

    candidates = sorted(
        {
            round(min(max(v, min_frac), 0.95), 6)
            for v in (0.10, 0.125, 0.15, 0.20, 0.25, 0.30, 0.40)
        }
    )
    folds = int(min(3, max(2, n // 12)))
    idx = np.arange(n, dtype=int)
    best_frac = 0.3
    best_score = float("inf")
    for frac in candidates:
        total_sse = 0.0
        total_n = 0
        for fold in range(folds):
            test_mask = (idx % folds) == fold
            train_mask = ~test_mask
            if int(train_mask.sum()) < 4 or int(test_mask.sum()) < 1:
                continue
            fit_sorted = _fit_lowess_sorted_reflected(
                x[train_mask],
                y[train_mask],
                frac=float(frac),
                it=2,
            )
            pred = _interp_trend_on_x(
                fit_sorted[:, 0],
                fit_sorted[:, 1],
                x[test_mask],
            )
            good = np.isfinite(pred) & np.isfinite(y[test_mask])
            if not np.any(good):
                continue
            diff = y[test_mask][good] - pred[good]
            total_sse += float(np.sum(diff * diff))
            total_n += int(good.sum())
        if total_n > 0:
            score = total_sse / float(total_n)
            if score < best_score:
                best_score = score
                best_frac = float(frac)
    return float(best_frac)

def detrend_lowess(
    series: pd. Series,
    frac: Optional[float] = None,
    difference: bool = False,
) -> pd. Series:
    """Detrend using a LOWESS approximation that is closer to Friedman/supsmu behavior."""
    y = series.to_numpy(dtype=float)
    x = np.arange(1, len(y) + 1, dtype=float)
    mask = np.isfinite(y)
    if mask.sum() < 4 or sm_lowess is None:
        return pd. Series(np.full_like(y, np.nan), index=series.index)
    x_fit = x[mask]
    y_fit = y[mask]
    fit_sorted = _fit_lowess_sorted_reflected(
        x_fit,
        y_fit,
        frac=float(min(max(float(frac), 4.0 / max(float(len(y_fit)), 1.0)), 0.95)) if (frac is not None and np.isfinite(frac) and float(frac) > 0.0) else 0.2,
        it=2,
    )
    trend = np.full_like(y, np.nan)
    if frac is None or (not np.isfinite(frac)) or float(frac) <= 0.0:
        supsmu_like = _lowess_supsmu_like_trend(x_fit, y_fit)
        if supsmu_like is not None:
            trend[mask] = supsmu_like
        else:
            frac_use = _select_lowess_frac_cv(x_fit, y_fit)
            fit_sorted = _fit_lowess_sorted_reflected(
                x_fit,
                y_fit,
                frac=frac_use,
                it=2,
            )
            trend[mask] = _interp_trend_on_x(fit_sorted[:, 0], fit_sorted[:, 1], x_fit)
    else:
        frac_use = float(min(max(float(frac), 4.0 / max(float(len(y_fit)), 1.0)), 0.95))
        fit_sorted = _fit_lowess_sorted_reflected(
            x_fit,
            y_fit,
            frac=frac_use,
            it=2,
        )
        trend[mask] = _interp_trend_on_x(fit_sorted[:, 0], fit_sorted[:, 1], x_fit)
    if np.any(~np.isfinite(trend[mask])):
        frac_fallback = float(min(max(0.25, 4.0 / max(float(len(y_fit)), 1.0)), 0.95))
        fit_sorted = _fit_lowess_sorted_reflected(
            x_fit,
            y_fit,
            frac=frac_fallback,
            it=1,
        )
        trend[mask] = _interp_trend_on_x(fit_sorted[:, 0], fit_sorted[:, 1], x_fit)
    if np.any(~np.isfinite(trend[mask])) or np.any(trend[mask] <= 0.0):
        return detrend_mean(series, difference=difference)
    idx = _index_from_trend(y, trend, difference=difference)
    return pd. Series(idx, index=series.index)

def _hugershoff_initial_guess(
    x_fit: np.ndarray,
    y_fit: np.ndarray,
) -> tuple[float, float, float, float]:
    """Build one stable deterministic starting point for Hugershoff fitting."""
    a0 = float(max(np.nanmax(y_fit) - np.nanmin(y_fit), np.nanmax(y_fit) * 0.1, 1e-6))
    peak_x = float(x_fit[int(np.nanargmax(y_fit))]) if y_fit.size > 0 else 1.0
    b0 = 1.0
    g0 = float(max(b0 / max(peak_x, 1.0), 1e-3))
    d0 = float(max(np.nanmin(y_fit), 0.0))
    return (a0, b0, g0, d0)

def hugershoff_func(
    x: np.ndarray, a: float, b: float, g: float, d: float
) -> np.ndarray:
    """Hugershoff function a * x^b * exp(-g * x) + d."""
    with np.errstate(over="ignore", invalid="ignore"):
        return a * (x**b) * np.exp(-g * x) + d
def detrend_hugershoff(
    series: pd.Series,
    difference: bool = False,
    pos_slope: bool = False,
    constrain_nls: ConstrainNLS = "never",
) -> pd.Series:
    """Detrend using a dplR-like Hugershoff curve with constrained fallback logic."""
    y = series.to_numpy(dtype=float)
    x = np.arange(1, len(y) + 1, dtype=float)
    mask = np.isfinite(y)
    if mask.sum() < 4:
        return pd. Series(np.full_like(y, np.nan), index=series.index)
    x_fit, y_fit = x[mask], y[mask]
    p0 = _hugershoff_initial_guess(x_fit, y_fit)

    def _valid_trend(trend: Optional[np.ndarray]) -> bool:
        if trend is None:
            return False
        if np.any(~np.isfinite(trend[mask])):
            return False
        if np.any(trend[mask] <= 0.0):
            return False
        if (not pos_slope) and float(trend[mask][-1]) > float(trend[mask][0]):
            return False
        return True

    def _fit_hugershoff(use_bounds: bool) -> Optional[np.ndarray]:
        try:
            kwargs: Dict[str, Any] = {"p0": p0, "maxfev": 50000}
            if use_bounds:
                kwargs["bounds"] = ([0.0, 0.0, 0.0, 0.0], [np.inf, np.inf, np.inf, np.inf])
            with warnings.catch_warnings():
                warnings.simplefilter("error", OptimizeWarning)
                params, _ = curve_fit(hugershoff_func, x_fit, y_fit, **kwargs)
            return hugershoff_func(x, *params)
        except Exception:
            return None

    trend: Optional[np.ndarray] = None
    if constrain_nls == "always":
        trend = _fit_hugershoff(use_bounds=True)
    elif constrain_nls == "when.fail":
        trend = _fit_hugershoff(use_bounds=False)
        if not _valid_trend(trend):
            trend = _fit_hugershoff(use_bounds=True)
    else:
        trend = _fit_hugershoff(use_bounds=False)

    if not _valid_trend(trend):
        return detrend_negexp(
            series,
            difference=difference,
            pos_slope=pos_slope,
            constrain_nls=constrain_nls,
        )

    idx = _index_from_trend(y, trend, difference=difference)
    return pd.Series(idx, index=series.index)
def detrend_spline(
    series: pd. Series,
    target_years: int,
    difference: bool = False,
    smoothing_strength: float = 1.0,
) -> pd. Series:
    """Detrend a contiguous block with a Cook-Peters-like spline fallback."""
    y = series.to_numpy(dtype=float)
    x = np.arange(1, len(y) + 1, dtype=float)
    mask = np.isfinite(y)
    if mask.sum() < 4:
        return pd. Series(np.full_like(y, np.nan), index=series.index)
    x_fit, y_fit = x[mask], y[mask]
    trend = np.full_like(y, np.nan, dtype=float)

    cp_trend = _cook_peters_spline_trend(
        y_fit,
        target_years=target_years,
        frequency_cutoff=0.5,
        rigidity_scale=smoothing_strength,
    )
    if cp_trend is not None:
        trend[mask] = cp_trend
    else:
        n = int(mask.sum())
        rigidity = max(float(target_years), 1.0) / max(float(n), 1.0)
        base_s = float(np.nanvar(y_fit)) * max(float(n), 1.0)
        s = smoothing_strength * base_s * max(rigidity, 1e-6)
        if (not np.isfinite(s)) or s <= 0:
            s = None
        try:
            k = int(min(3, max(1, len(x_fit) - 1)))
            spl = UnivariateSpline(x_fit, y_fit, s=s, k=k)
            trend[mask] = spl(x_fit)
        except Exception:
            return detrend_mean(series, difference=difference)

    if np.any(~np.isfinite(trend[mask])) or np.any(trend[mask] <= 0.0):
        return detrend_mean(series, difference=difference)
    idx = _finalize_detrended_values(y, trend, difference=difference)
    return pd. Series(idx, index=series.index)

def _age_dependent_penalty_matrix(
    n: int,
    base_years: int = 50,
    frequency_cutoff: float = 0.5,
) -> Optional[np.ndarray]:
    """Build a dense age-dependent second-difference penalty matrix."""
    if n < 4:
        return None
    dmat = np.zeros((n - 2, n), dtype=float)
    for i in range(n - 2):
        dmat[i, i] = 1.0
        dmat[i, i + 1] = -2.0
        dmat[i, i + 2] = 1.0
    row_lam = np.array(
        [
            _cook_peters_lambda(
                int(max(base_years + i, 3)),
                frequency_cutoff=frequency_cutoff,
            )
            for i in range(n - 2)
        ],
        dtype=float,
    )
    if np.any(~np.isfinite(row_lam)):
        return None
    w = np.diag(row_lam)
    return dmat.T @ w @ dmat

def _age_dependent_spline_trend(
    y: np.ndarray,
    base_years: int = 50,
    frequency_cutoff: float = 0.5,
    pos_slope: bool = False,
) -> Optional[np.ndarray]:
    """Fit an age-dependent spline trend using increasing smoothness with age."""
    y = np.asarray(y, dtype=float)
    if y.ndim != 1 or y.size < 4 or np.any(~np.isfinite(y)):
        return None
    penalty = _age_dependent_penalty_matrix(
        int(y.size),
        base_years=base_years,
        frequency_cutoff=frequency_cutoff,
    )
    if penalty is None:
        return None
    system = np.eye(int(y.size), dtype=float) + penalty
    try:
        trend = np.linalg.solve(system, y)
    except Exception:
        return None
    if trend is None or np.any(~np.isfinite(trend)):
        return None
    if not pos_slope:
        diffs = np.diff(trend)
        pos_tail = np.where(diffs > 0.0)[0]
        if pos_tail.size > 0:
            cut = int(pos_tail[0] + 1)
            trend[cut:] = trend[cut]
    return np.asarray(trend, dtype=float)

def detrend_age_dep_spline(
    series: pd.Series,
    difference: bool = False,
    pos_slope: bool = False,
    base_years: int = 50,
) -> pd.Series:
    """Detrend a contiguous block with an age-dependent spline."""
    y = series.to_numpy(dtype=float)
    mask = np.isfinite(y)
    if int(mask.sum()) < 4:
        return pd.Series(np.full_like(y, np.nan), index=series.index)
    y_fit = y[mask]
    trend = np.full_like(y, np.nan, dtype=float)
    ads_trend = _age_dependent_spline_trend(
        y_fit,
        base_years=base_years,
        frequency_cutoff=0.5,
        pos_slope=pos_slope,
    )
    if ads_trend is None:
        return detrend_mean(series, difference=difference)
    trend[mask] = ads_trend
    if np.any(~np.isfinite(trend[mask])) or np.any(trend[mask] <= 0.0):
        return detrend_mean(series, difference=difference)
    idx = _index_from_trend(y, trend, difference=difference)
    return pd.Series(idx, index=series.index)

def _fit_ar_residuals_aic(
    y: np.ndarray,
    max_order: int = 10,
) -> tuple[int, np.ndarray]:
    """Fit an AR model with Yule-Walker equations and AIC-like order selection."""
    y = np.asarray(y, dtype=float)
    n = int(y.size)
    mu = float(np.nanmean(y))
    yc = y - mu
    best_order = 0
    best_aic = float("inf")
    best_resid = yc.copy()
    max_p = int(min(max_order, max(0, n - 2)))
    if n < 2:
        return 0, yc.copy()

    gamma = np.array(
        [float(np.dot(yc[: n - k], yc[k:]) / max(n, 1)) for k in range(max_p + 1)],
        dtype=float,
    )

    for p in range(max_p + 1):
        if p == 0:
            resid = yc.copy()
            sigma2 = float(gamma[0])
            n_eff = n
        else:
            if (not np.isfinite(gamma[: p + 1]).all()) or float(gamma[0]) <= 0.0:
                continue
            try:
                rmat = toeplitz(gamma[:p])
                rvec = gamma[1 : p + 1]
                coef = np.linalg.solve(rmat, rvec)
            except Exception:
                continue
            sigma2 = float(gamma[0] - np.dot(coef, gamma[1 : p + 1]))
            if (not np.isfinite(sigma2)) or sigma2 <= 0.0:
                continue
            resid = np.empty(n, dtype=float)
            resid[:p] = yc[:p]
            for t in range(p, n):
                resid[t] = yc[t] - float(np.dot(coef, yc[t - p : t][::-1]))
            n_eff = max(n - p, 1)
        if (not np.isfinite(sigma2)) or sigma2 <= 0.0 or n_eff <= 0:
            continue
        aic = float(n_eff * math.log(sigma2) + 2.0 * p)
        if aic < best_aic:
            best_aic = aic
            best_order = p
            best_resid = resid

    return best_order, np.asarray(best_resid, dtype=float)

def detrend_ar(
    series: pd.Series,
    difference: bool = False,
    max_order: int = 10,
) -> pd.Series:
    """Approximate dplR Ar detrending using ar.func + detrend.series logic."""
    y = series.to_numpy(dtype=float)
    mask = np.isfinite(y)
    if int(mask.sum()) < 4:
        return pd.Series(np.full_like(y, np.nan), index=series.index)
    y_fit = y[mask]
    mean_y = float(np.nanmean(y_fit))
    if difference:
        base = y_fit - mean_y
    else:
        if (not np.isfinite(mean_y)) or mean_y <= 0.0:
            return pd.Series(np.full_like(y, np.nan), index=series.index)
        with np.errstate(divide="ignore", invalid="ignore"):
            base = y_fit / mean_y
    if np.any(~np.isfinite(base)):
        return pd.Series(np.full_like(y, np.nan), index=series.index)
    _order, resid = _fit_ar_residuals_aic(base, max_order=max_order)
    if difference:
        out_fit = resid
    else:
        out_fit = resid + 1.0
    out = np.full_like(y, np.nan, dtype=float)
    out[mask] = out_fit
    return pd.Series(out, index=series.index)

def signal_free_detrend(
    rwl: pd.DataFrame,
    detrend_block_func,
    recode_zeros: bool,
    difference: bool = False,
    iterations: int = 3,
    mad_threshold: float = 5e-4,
) -> pd.DataFrame:
    """Perform a dplR-like generic signal-free iterative detrending."""
    raw = _recode_zeros_df(rwl.copy().astype(float), recode_zeros)
    if raw.empty:
        return raw

    ages = compute_ages(raw)
    median_seg_length = int(max(3, math.floor(float(np.nanmedian(ages.to_numpy(dtype=float))))))
    prev_hf: Optional[np.ndarray] = None
    max_iter = max(int(iterations), 1)

    initial_rwi = raw.apply(
        lambda s: _apply_blockwise_detrend(
            s,
            detrend_block_func,
            recode_zeros=False,
        ),
        axis=0,
    )
    initial_chron, samp_depth = _build_biweight_or_mean_chronology(initial_rwi)
    depth_vals = samp_depth.to_numpy(dtype=float)
    depth_base = np.clip(depth_vals - 1.0, 0.0, None)
    if np.nanmax(depth_base) > 0.0:
        normalized_sample_depth = np.sqrt(depth_base) / math.sqrt(float(np.nanmax(depth_base)))
    else:
        normalized_sample_depth = np.zeros_like(depth_base, dtype=float)

    final_rwi = initial_rwi.copy().astype(float)
    iter_chron = initial_chron.copy().astype(float)
    for _ in range(max_iter):
        common = iter_chron.reindex(raw.index).to_numpy(dtype=float)
        sf_meas = raw.copy().astype(float)
        for col in sf_meas.columns:
            ser = raw[col].to_numpy(dtype=float)
            tmp = np.full_like(ser, np.nan, dtype=float)
            mask = np.isfinite(ser) & np.isfinite(common)
            if np.any(mask):
                if difference:
                    tmp[mask] = ser[mask] - common[mask]
                else:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        tmp[mask] = ser[mask] / common[mask]
            sf_meas[col] = tmp

        sf_rescaled = sf_meas.copy().astype(float)
        for col in sf_rescaled.columns:
            sf_col = sf_meas[col].to_numpy(dtype=float)
            raw_col = raw[col].to_numpy(dtype=float)
            sf_rescaled[col] = _restore_signal_free_mean(sf_col, raw_col, difference=difference)

        depth_one_mask = samp_depth.reindex(raw.index).to_numpy(dtype=float) == 1.0
        if np.any(depth_one_mask):
            sf_rescaled.loc[depth_one_mask, :] = raw.loc[depth_one_mask, :]

        rwi = sf_rescaled.apply(
            lambda s: _apply_blockwise_detrend(
                s,
                detrend_block_func,
                recode_zeros=False,
            ),
            axis=0,
        )
        iter_chron, _ = _build_biweight_or_mean_chronology(rwi)
        hf = _high_freq_residual_chronology(iter_chron, target_years=median_seg_length)
        final_rwi = rwi.copy().astype(float)
        if prev_hf is not None:
            mad_vec = np.abs(hf * normalized_sample_depth - prev_hf * normalized_sample_depth)
            median_abs_diff = float(np.nanmedian(mad_vec))
            if np.isfinite(median_abs_diff) and median_abs_diff <= float(mad_threshold):
                break
        prev_hf = hf
    return final_rwi

def signal_free_spline_dplr_like(
    rwl: pd.DataFrame,
    target_years: int,
    recode_zeros: bool,
    difference: bool = False,
    max_iterations: int = 25,
    mad_threshold: float = 5e-4,
) -> pd.DataFrame:
    """Approximate dplR ssf for spline methods."""
    dat = rwl.copy().astype(float)
    dat = _recode_zeros_df(dat, recode_zeros)
    if dat.empty:
        return dat

    dat_curves = dat.apply(
        lambda s: _apply_blockwise_trend(
            s,
            lambda b: _fit_spline_trend_block(
                b,
                target_years=target_years,
                smoothing_strength=1.0,
            ),
            recode_zeros=False,
        ),
        axis=0,
    )
    if np.any((dat_curves.to_numpy(dtype=float) <= 0.0) & np.isfinite(dat_curves.to_numpy(dtype=float))):
        return signal_free_detrend(
            rwl,
            detrend_block_func=lambda b: detrend_spline(
                b,
                target_years=target_years,
                difference=difference,
                smoothing_strength=1.0,
            ),
            recode_zeros=recode_zeros,
            difference=difference,
            iterations=max_iterations,
            mad_threshold=mad_threshold,
        )

    if difference:
        dat_rwi = dat - dat_curves
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            dat_rwi = dat / dat_curves

    iter_crn, dat_samp_depth = _build_biweight_or_mean_chronology(dat_rwi)
    depth_vals = dat_samp_depth.to_numpy(dtype=float)
    depth_base = np.clip(depth_vals - 1.0, 0.0, None)
    if np.nanmax(depth_base) > 0.0:
        normalized_sample_depth = np.sqrt(depth_base) / math.sqrt(float(np.nanmax(depth_base)))
    else:
        normalized_sample_depth = np.zeros_like(depth_base, dtype=float)

    ages = compute_ages(dat)
    median_seg_length = int(max(3, math.floor(float(np.nanmedian(ages.to_numpy(dtype=float))))))
    prev_hf: Optional[np.ndarray] = None
    final_rwi = dat_rwi.copy().astype(float)

    for iteration_number in range(1, max(int(max_iterations), 1) + 1):
        common = iter_crn.reindex(dat.index).to_numpy(dtype=float)
        sf_rw = dat.copy().astype(float)
        for col in sf_rw.columns:
            y = dat[col].to_numpy(dtype=float)
            tmp = np.full_like(y, np.nan, dtype=float)
            mask = np.isfinite(y) & np.isfinite(common)
            if np.any(mask):
                if difference:
                    tmp[mask] = y[mask] - common[mask]
                else:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        tmp[mask] = y[mask] / common[mask]
            sf_rw[col] = tmp

        sf_rescaled = sf_rw.copy().astype(float)
        for col in sf_rescaled.columns:
            sf_col = sf_rw[col].to_numpy(dtype=float)
            dat_col = dat[col].to_numpy(dtype=float)
            sf_mean = float(np.nanmean(sf_col))
            dat_mean = float(np.nanmean(dat_col))
            out_col = sf_col.copy()
            if np.isfinite(sf_mean) and np.isfinite(dat_mean):
                out_col = (sf_col - sf_mean) + dat_mean
            out_col[np.isnan(out_col)] = 0.0
            sf_rescaled[col] = out_col

        depth_one_mask = dat_samp_depth.reindex(dat.index).to_numpy(dtype=float) == 1.0
        if np.any(depth_one_mask):
            sf_rescaled.loc[depth_one_mask, :] = dat.loc[depth_one_mask, :]

        sf_curves = sf_rescaled.apply(
            lambda s: _apply_blockwise_trend(
                s,
                lambda b: _fit_spline_trend_block(
                    b,
                    target_years=target_years,
                    smoothing_strength=1.0,
                ),
                recode_zeros=False,
            ),
            axis=0,
        )
        if np.any((sf_curves.to_numpy(dtype=float) <= 0.0) & np.isfinite(sf_curves.to_numpy(dtype=float))):
            break

        if difference:
            sf_rwi = dat - sf_curves
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                sf_rwi = dat / sf_curves

        sf_crn, _sf_depth = _build_biweight_or_mean_chronology(sf_rwi)
        sf_crn_vals = sf_crn.to_numpy(dtype=float)
        hf_curve = _cook_peters_spline_trend(
            sf_crn_vals[np.isfinite(sf_crn_vals)],
            target_years=max(median_seg_length, 3),
            frequency_cutoff=0.5,
            rigidity_scale=1.0,
        )
        hf_crn = np.full_like(sf_crn_vals, np.nan, dtype=float)
        good = np.isfinite(sf_crn_vals)
        if hf_curve is None:
            hf_crn[good] = sf_crn_vals[good]
        else:
            hf_crn[good] = sf_crn_vals[good] - hf_curve

        final_rwi = sf_rwi.copy().astype(float)
        if prev_hf is not None:
            mad_vec = np.abs(hf_crn * normalized_sample_depth - prev_hf * normalized_sample_depth)
            median_abs_diff = float(np.nanmedian(mad_vec))
            if np.isfinite(median_abs_diff) and median_abs_diff <= float(mad_threshold):
                break
        prev_hf = hf_crn
        iter_crn = sf_crn

    return final_rwi

def build_chronology(rwi: pd. DataFrame) -> pd. Series:
    """Build standard chronology as a biweight robust mean by year (ARSTAN default)."""
    def _biweight_mean_1d(x: np.ndarray, c: float = 9.0) -> float:
        x = np.asarray(x, dtype=float)
        x = x[np.isfinite(x)]
        if x.size == 0:
            return float("nan")
        if x.size == 1:
            return float(x[0])
        m = float(np.median(x))
        mad = float(np.median(np.abs(x - m)))
        if mad <= 0 or (not np.isfinite(mad)):
            return float(np.mean(x))
        u = (x - m) / (c * mad)
        mask = np.abs(u) < 1.0
        if not np.any(mask):
            return float(np.mean(x))
        u = u[mask]
        xw = x[mask]
        w = (1.0 - u * u) ** 2
        denom = float(np.sum(w))
        if denom <= 0 or (not np.isfinite(denom)):
            return float(np.mean(xw))
        return float(m + np.sum((xw - m) * w) / denom)
    return rwi.apply(lambda row: _biweight_mean_1d(row.to_numpy(dtype=float)), axis=1)
def _yule_walker_aic(x: np.ndarray, max_order: int) -> np.ndarray:
    """AR coefficients by Yule-Walker (Levinson-Durbin) with the order chosen by AIC, as R ar()."""
    n = len(x)
    max_order = int(max(0, min(max_order, n - 2)))
    if max_order == 0:
        return np.zeros(0)
    acov = np.array([np.dot(x[: n - k], x[k:]) / n for k in range(max_order + 1)])
    if acov[0] <= 0:
        return np.zeros(0)
    best_aic = n * np.log(acov[0])
    best = np.zeros(0)
    phi = np.zeros(0)
    var = acov[0]
    for m in range(1, max_order + 1):
        k = (acov[m] - np.dot(phi, acov[m - 1:0:-1])) / var if m > 1 else acov[1] / var
        phi = np.concatenate([phi - k * phi[::-1], [k]]) if m > 1 else np.array([k])
        var = var * (1.0 - k * k)
        if var <= 0:
            break
        aic = n * np.log(var) + 2 * m
        if aic < best_aic:
            best_aic, best = aic, phi.copy()
    return best


def prewhiten_ar1(series: pd. Series) -> pd. Series:
    """Prewhiten one series as dplR does: AR model (order by AIC) on the centred series,
    residuals plus the series mean (so the residual indices keep a mean of about 1)."""
    y = series.to_numpy(dtype=float)
    mask = ~np.isnan(y)
    out = np.full_like(y, np.nan)
    if mask.sum() < 5:
        return pd. Series(out, index=series.index)
    idx = np.flatnonzero(mask)
    yy = y[idx[0]: idx[-1] + 1]
    if np.isnan(yy).any():  # interior gaps: fill linearly for the fit only
        s = pd.Series(yy).interpolate(limit_direction="both").to_numpy()
    else:
        s = yy
    mu = float(np.mean(s))
    xc = s - mu
    order_max = int(min(10, max(1, np.floor(10 * np.log10(len(xc))))))
    phi = _yule_walker_aic(xc, order_max)
    p = len(phi)
    res = np.full(len(xc), np.nan)
    for t in range(p, len(xc)):
        res[t] = xc[t] - (np.dot(phi, xc[t - p:t][::-1]) if p else 0.0)
    res = res + mu
    res[np.isnan(yy)] = np.nan
    out[idx[0]: idx[-1] + 1] = res
    return pd. Series(out, index=series.index)
def build_residual_chronology(rwi: pd. DataFrame) -> pd. Series:
    """Residual chronology (RES): prewhitened series combined with the same robust mean as STD."""
    res_df = rwi.apply(prewhiten_ar1, axis=0)
    try:
        return build_chronology(res_df)
    except Exception:
        return res_df.median(axis=1, skipna=True)
def compute_residual_matrix(rwi: pd. DataFrame) -> pd. DataFrame:
    """Apply AR(1) prewhitening to each series and return the matrix of residuals."""
    res = rwi.copy().astype(float)
    for col in rwi.columns:
        ser = rwi[col]
        res[col] = prewhiten_ar1(ser)
    return res
# ---------------------------------------------------------------------------
# RCS and signal-free RCS
# ---------------------------------------------------------------------------
@dataclass
class RCSResult:
    rwi: pd. DataFrame
    age_curve: pd. Series
def rcs_detrend(
    rwl: pd.DataFrame,
    po: Optional[pd.DataFrame] = None,
    difference: bool = False,
    biweight: bool = True,
    nyrs: Optional[int] = None,
    f: float = 0.5,
    recode_zeros: bool = False,
) -> RCSResult:
    """Perform a dplR-like RCS detrending in pure Python."""
    raw = _recode_zeros_df(rwl.copy().astype(float), recode_zeros)
    age_curve = _build_rcs_curve(
        raw,
        po=po,
        nyrs=nyrs,
        f=f,
        biweight=biweight,
    )
    rwi = _apply_rcs_curve_to_frame(
        raw,
        rc_curve=age_curve,
        po=po,
        difference=difference,
    )
    return RCSResult(rwi=rwi, age_curve=age_curve)


def signal_free_rcs(
    rwl: pd.DataFrame,
    iterations: int = 3,
    po: Optional[pd.DataFrame] = None,
    difference: bool = False,
    biweight: bool = True,
    nyrs: Optional[int] = None,
    f: float = 0.5,
    mad_threshold: float = 5e-4,
    recode_zeros: bool = False,
) -> RCSResult:
    """Perform a dplR-like signal-free RCS in pure Python."""
    raw = _recode_zeros_df(rwl.copy().astype(float), recode_zeros)
    if raw.empty:
        return RCSResult(rwi=raw, age_curve=pd.Series(dtype=float, name="rcs_curve"))

    initial = rcs_detrend(
        raw,
        po=po,
        difference=difference,
        biweight=biweight,
        nyrs=nyrs,
        f=f,
        recode_zeros=False,
    )
    iter_chron, samp_depth = _build_biweight_or_mean_chronology(initial.rwi)
    depth_vals = samp_depth.to_numpy(dtype=float)
    depth_base = np.clip(depth_vals - 1.0, 0.0, None)
    if np.nanmax(depth_base) > 0.0:
        normalized_sample_depth = np.sqrt(depth_base) / math.sqrt(float(np.nanmax(depth_base)))
    else:
        normalized_sample_depth = np.zeros_like(depth_base, dtype=float)

    ages = compute_ages(raw)
    median_seg_length = int(max(3, math.floor(float(np.nanmedian(ages.to_numpy(dtype=float))))))
    prev_hf = _high_freq_residual_chronology(iter_chron, target_years=median_seg_length)
    final_age_curve = initial.age_curve.copy()
    final_rwi = initial.rwi.copy()

    for _ in range(2, max(int(iterations), 1) + 1):
        common = iter_chron.reindex(raw.index).to_numpy(dtype=float)
        sf_rw = raw.copy().astype(float)
        for col in sf_rw.columns:
            y = raw[col].to_numpy(dtype=float)
            tmp = np.full_like(y, np.nan, dtype=float)
            mask = np.isfinite(y) & np.isfinite(common)
            if np.any(mask):
                if difference:
                    tmp[mask] = y[mask] - common[mask]
                else:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        tmp[mask] = y[mask] / common[mask]
            sf_rw[col] = tmp

        sf_rescaled = sf_rw.copy().astype(float)
        for col in sf_rescaled.columns:
            sf_col = sf_rw[col].to_numpy(dtype=float)
            raw_col = raw[col].to_numpy(dtype=float)
            sf_rescaled[col] = _restore_signal_free_mean(sf_col, raw_col, difference=difference)

        depth_one_mask = samp_depth.reindex(raw.index).to_numpy(dtype=float) == 1.0
        if np.any(depth_one_mask):
            sf_rescaled.loc[depth_one_mask, :] = raw.loc[depth_one_mask, :]

        rc_curve = _build_rcs_curve(
            sf_rescaled,
            po=po,
            nyrs=nyrs,
            f=f,
            biweight=biweight,
        )
        sf_rwi = _apply_rcs_curve_to_frame(
            raw,
            rc_curve=rc_curve,
            po=po,
            difference=difference,
        )
        sf_chron, _ = _build_biweight_or_mean_chronology(sf_rwi)
        hf = _high_freq_residual_chronology(sf_chron, target_years=median_seg_length)

        final_age_curve = rc_curve.copy()
        final_rwi = sf_rwi.copy()
        mad_vec = np.abs(hf * normalized_sample_depth - prev_hf * normalized_sample_depth)
        median_abs_diff = float(np.nanmedian(mad_vec))
        iter_chron = sf_chron
        prev_hf = hf
        if np.isfinite(median_abs_diff) and median_abs_diff <= float(mad_threshold):
            break

    return RCSResult(rwi=final_rwi, age_curve=final_age_curve)
# ---------------------------------------------------------------------------
# Disturbance detection
# ---------------------------------------------------------------------------
@dataclass
class DisturbanceStats:
    n_events: int
    fraction_series_with_events: float
def detect_releases_suppressions(
    rwl: pd. DataFrame,
    window: int = 10,
    threshold: float = 0.5,
) -> DisturbanceStats:
    """Detect strong releases/suppressions per series."""
    total_events = 0
    series_with_events = 0
    for col in rwl.columns:
        ser = rwl[col].dropna()
        if ser.empty:
            continue
        smooth = ser.rolling(window=window, center=True, min_periods=3).mean()
        vals = smooth.to_numpy(dtype=float)
        mask = ~np.isnan(vals)
        vals = vals[mask]
        if len(vals) < 3:
            continue
        prev = vals[:-1]
        nxt = vals[1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            change = (nxt - prev) / np.abs(prev)
        events_idx = np.where(np.abs(change) > threshold)[0]
        n_ev = int(events_idx.shape[0])
        total_events += n_ev
        if n_ev > 0:
            series_with_events += 1
    n_series = rwl.shape[1]
    frac = series_with_events / n_series if n_series > 0 else float("nan")
    return DisturbanceStats(n_events=total_events, fraction_series_with_events=frac)
# ---------------------------------------------------------------------------
# Candidate metrics and scoring
# ---------------------------------------------------------------------------
@dataclass
class CandidateMetrics:
    eps: float
    rbar: float
    var_highfreq: float
    var_lowfreq: float
    climate_corr: Optional[float]
def band_variance(
    chron: pd. Series,
    cutoff_years: float,
    highfreq: bool,
    dt: float = 1.0,
) -> float:
    """Variance of high- or low-frequency band of chronology.

    This is used only for scoring; it must be robust to NaNs and short series.
    """
    y = chron.to_numpy(dtype=float)
    finite = np.isfinite(y)
    if int(finite.sum()) < 3:
        return float("nan")
    y = y - float(np.nanmean(y))
    if float(np.nanvar(y)) == 0.0:
        return 0.0

    y_fill = y
    if not np.all(np.isfinite(y_fill)):
        y_fill = (
            pd.Series(y_fill)
            .interpolate(limit_direction="both")
            .bfill()
            .ffill()
            .to_numpy(dtype=float)
        )

    fs = 1.0 / dt
    nyq = 0.5 * fs
    freq_cut = 1.0 / max(cutoff_years, 1.0)
    wn = freq_cut / nyq
    wn = min(max(wn, 1e-4), 0.99)
    btype = "high" if highfreq else "low"
    b, a = butter(2, wn, btype=btype)

    padlen = 3 * (max(len(a), len(b)) - 1)
    if int(y_fill.size) <= padlen:
        return float(np.nanvar(y_fill))

    y_filt = filtfilt(b, a, y_fill)
    if int(np.isfinite(y_filt).sum()) < 1:
        return float("nan")
    return float(np.nanvar(y_filt))


def _coerce_metric_scalar(value: Any) -> float:
    """Convert a metric-like value to one finite scalar float or NaN."""
    try:
        arr = np.asarray(value, dtype=float)
    except Exception:
        return float("nan")
    if arr.size == 0:
        return float("nan")
    try:
        val = float(arr.reshape(-1)[0])
    except Exception:
        return float("nan")
    return val if np.isfinite(val) else float("nan")


def _compress_variance_metric(value: float) -> float:
    """Compress an unbounded non-negative variance metric to the [0, 1) range."""
    if not np.isfinite(value) or value <= 0.0:
        return 0.0
    return float(value / (1.0 + value))


def compute_candidate_metrics(
    rwi: pd.DataFrame,
    climate: Optional[pd.Series],
    ids: Optional[pd.DataFrame] = None,
    high_cutoff_years: float = 10.0,
    low_cutoff_years: float = 20.0,
) -> CandidateMetrics:
    """Compute metrics for one detrending candidate."""
    chron = build_chronology(rwi)
    eps_val = _coerce_metric_scalar(eps_like(rwi, min_overlap=30, ids=ids))
    rbar_val = _coerce_metric_scalar(mean_rbar(rwi, min_overlap=30, ids=ids))
    if (not np.isfinite(eps_val)) or eps_val < 0.0 or eps_val > 1.0:
        eps_val = float("nan")
    if (not np.isfinite(rbar_val)) or abs(rbar_val) > 1.0:
        rbar_val = float("nan")
    var_high = band_variance(
        chron,
        cutoff_years=float(max(3.0, high_cutoff_years)),
        highfreq=True,
    )
    var_low = band_variance(
        chron,
        cutoff_years=float(max(3.0, low_cutoff_years)),
        highfreq=False,
    )
    climate_corr: Optional[float] = None
    if climate is not None:
        aligned = pd.concat([chron, climate], axis=1, join="inner").dropna()
        if aligned.shape[0] > 5:
            climate_corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
    return CandidateMetrics(
        eps=float(eps_val),
        rbar=float(rbar_val),
        var_highfreq=float(var_high),
        var_lowfreq=float(var_low),
        climate_corr=climate_corr,
    )
def score_candidate(
    metrics: CandidateMetrics,
    goal: GoalType,
    score_profile: Optional[Dict[str, Any]] = None,
) -> float:
    """Score candidate based on goal."""
    profile = _normalize_score_profile(score_profile, goal=goal)
    eps = 0.0 if math.isnan(metrics.eps) else float(min(max(metrics.eps, 0.0), 1.0))
    rbar = 0.0 if math.isnan(metrics.rbar) else float(min(max(metrics.rbar, 0.0), 1.0))
    var_hi = _compress_variance_metric(metrics.var_highfreq)
    var_lo = _compress_variance_metric(metrics.var_lowfreq)
    corr_abs = 0.0
    if metrics.climate_corr is not None and not math.isnan(metrics.climate_corr):
        corr_abs = float(min(max(abs(metrics.climate_corr), 0.0), 1.0))
    return float(
        profile["w_var_high"] * var_hi
        + profile["w_var_low"] * var_lo
        + profile["w_eps"] * eps
        + profile["w_rbar"] * rbar
        + profile["w_climate"] * corr_abs
    )
# ---------------------------------------------------------------------------
# RCS feasibility and advisor logic
# ---------------------------------------------------------------------------
@dataclass
class RCSFeasibility:
    possible: bool
    reason: str
@dataclass
class CandidateResult:
    name: str
    description: str
    rwi: pd. DataFrame
    chronology_std: pd. Series
    chronology_res: pd. Series
    metrics: CandidateMetrics
    score: float
@dataclass
class AdvisorResult:
    goal: GoalType
    rcs_feasibility: RCSFeasibility
    disturbance_stats: DisturbanceStats
    ages: pd. Series
    age_curve: pd. Series
    sample_depth: pd. Series
    candidates: Dict[str, CandidateResult]
    best_key: str
    rcs_std: Optional[pd. Series]
    rcs_sf_std: Optional[pd. Series]
    rcs_sf_age_curve: Optional[pd. Series]
    compat_mode: CompatMode = "legacy"
    compat_config: Optional[DplrCompatConfig] = None
    eps_rbar_best: Optional[pd. DataFrame] = None
    eps_rbar_cache: Dict[tuple[str, int], pd.DataFrame] = field(default_factory=dict)
    dplr_method_errors: Dict[str, str] = field(default_factory=dict)
    dplr_eps_rbar_errors: Dict[str, str] = field(default_factory=dict)
    pith_note: str = ""
    rcs_excluded_series: list = field(default_factory=list)


def _get_cached_signal_diagnostics_for_result(
    advisor_result: Optional[AdvisorResult],
    method_key: str,
    window: int,
) -> pd.DataFrame:
    """Get or compute cached EPS/Rbar/depth diagnostics for one advisor result."""
    if advisor_result is None:
        return pd.DataFrame()
    resolved_method = str(method_key).strip() or "best"
    if resolved_method == "best":
        resolved_method = advisor_result.best_key
    cand = advisor_result.candidates.get(resolved_method)
    if cand is None:
        return pd.DataFrame()
    win = int(max(3, window))
    cache_key = (resolved_method, win)
    cached = advisor_result.eps_rbar_cache.get(cache_key)
    if cached is not None:
        return cached.copy()

    if resolved_method == advisor_result.best_key and win == 50 and advisor_result.eps_rbar_best is not None:
        df = advisor_result.eps_rbar_best.copy()
    elif advisor_result.compat_mode == "dplR":
        cfg = advisor_result.compat_config or DEFAULT_COMPAT_CONFIG
        try:
            _stats_by, running_by, extras = _dplr_compute_eps_rbar_from_rwi(
                {resolved_method: cand.rwi},
                cfg,
                window=win,
                verbose=False,
            )
            err_map = dict(extras.get("dplr_eps_rbar_errors", {}) or {})
            if resolved_method in err_map:
                advisor_result.dplr_eps_rbar_errors[resolved_method] = str(err_map.get(resolved_method, "")).strip()
            excl_map = dict(extras.get("dplr_eps_rbar_series_exclusions", {}) or {}).get(resolved_method, {}) or {}
            if excl_map:
                eps_logs = dict(getattr(advisor_result, "eps_series_exclusion_logs", {}) or {})
                merged = dict(eps_logs.get(resolved_method, {}) or {})
                merged.update({str(k): str(v) for k, v in dict(excl_map).items()})
                eps_logs[resolved_method] = merged
                setattr(advisor_result, "eps_series_exclusion_logs", eps_logs)
            df = _prepare_running_eps_rbar_df(running_by.get(resolved_method))
            if df.empty and resolved_method not in err_map:
                advisor_result.dplr_eps_rbar_errors.setdefault(
                    resolved_method,
                    "No dplR EPS/RBAR output was produced for this method.",
                )
        except Exception as exc:
            advisor_result.dplr_eps_rbar_errors[resolved_method] = str(exc)
            if bool(getattr(cfg, "strict_dplr", False)):
                raise
            df = pd.DataFrame()
    else:
        try:
            df = _compute_running_eps_rbar_backend(
                cand.rwi,
                window=win,
                compat_mode=advisor_result.compat_mode,
                compat_config=advisor_result.compat_config,
                method_key=resolved_method,
            )
        except Exception:
            df = pd.DataFrame()
    df = _prepare_running_eps_rbar_df(df) if advisor_result.compat_mode == "dplR" else sanitize_eps_rbar_depth_df(df)
    advisor_result.eps_rbar_cache[cache_key] = df.copy()
    return df.copy()


def _precompute_signal_diagnostics_for_result(
    advisor_result: Optional[AdvisorResult],
    window: int = 50,
    verbose: bool = False,
) -> None:
    """Precompute and cache EPS/Rbar/depth diagnostics for all successful methods."""
    if advisor_result is None:
        return
    if not isinstance(advisor_result.candidates, dict) or not advisor_result.candidates:
        advisor_result.eps_rbar_best = None
        return

    win = int(max(3, window))
    cache = advisor_result.eps_rbar_cache

    if advisor_result.compat_mode == "dplR":
        cfg = advisor_result.compat_config or DEFAULT_COMPAT_CONFIG
        try:
            rwi_by_method = {
                str(key): cand.rwi
                for key, cand in advisor_result.candidates.items()
                if getattr(cand, "rwi", None) is not None
            }
            _stats_by, running_by, _extras = _dplr_compute_eps_rbar_from_rwi(
                rwi_by_method,
                cfg,
                window=win,
                verbose=verbose,
                progress_label="dplR EPS/RBAR precompute",
            )
            batch_excl = dict(_extras.get("dplr_eps_rbar_series_exclusions", {}) or {})
            if batch_excl:
                eps_logs = dict(getattr(advisor_result, "eps_series_exclusion_logs", {}) or {})
                for method_name, excl_map in batch_excl.items():
                    merged = dict(eps_logs.get(str(method_name), {}) or {})
                    merged.update({str(k): str(v) for k, v in dict(excl_map).items()})
                    eps_logs[str(method_name)] = merged
                setattr(advisor_result, "eps_series_exclusion_logs", eps_logs)
            for key in advisor_result.candidates.keys():
                df = _prepare_running_eps_rbar_df(running_by.get(str(key)))
                if df is not None and not df.empty:
                    cache[(str(key), win)] = df.copy()
        except Exception:
            if bool(getattr(cfg, "strict_dplr", False)):
                raise

    for key, cand in advisor_result.candidates.items():
        cache_key = (str(key), win)
        if cache_key in cache:
            continue
        try:
            if advisor_result.compat_mode == "dplR":
                df = _prepare_running_eps_rbar_df(
                    compute_eps_rbar_depth_timeseries(cand.rwi, window=win)
                )
            else:
                df = sanitize_eps_rbar_depth_df(
                    compute_eps_rbar_depth_timeseries(cand.rwi, window=win)
                )
            cache[cache_key] = df.copy()
        except Exception:
            cache[cache_key] = pd.DataFrame()

    best_key = str(advisor_result.best_key)
    advisor_result.eps_rbar_best = cache.get((best_key, win), pd.DataFrame()).copy()


def _format_backend_method_errors(advisor_result: Optional[AdvisorResult]) -> list[str]:
    """Build human-readable backend error lines for the final summary."""
    if advisor_result is None:
        return []
    lines: list[str] = []
    method_errors = dict(getattr(advisor_result, "dplr_method_errors", {}) or {})
    eps_errors = dict(getattr(advisor_result, "dplr_eps_rbar_errors", {}) or {})
    series_logs = dict(getattr(advisor_result, "series_exclusion_logs", {}) or {})
    eps_series_logs = dict(getattr(advisor_result, "eps_series_exclusion_logs", {}) or {})
    matrix_kind = str(getattr(advisor_result, "analysis_matrix_kind", "series") or "series")
    unit_label = "trees" if matrix_kind == "trees" else "series"
    unit_label_singular = "Tree" if matrix_kind == "trees" else "Series"

    for method_key in sorted(method_errors.keys()):
        detail = str(method_errors.get(method_key, "") or "").strip()
        if not detail:
            detail = "Unknown dplR detrending error."
        lines.append(f"- Detrending [{method_key}]: {detail}")

    for method_key in sorted(eps_errors.keys()):
        detail = str(eps_errors.get(method_key, "") or "").strip()
        if not detail:
            detail = "Unknown dplR EPS/RBAR error."
        lines.append(f"- EPS/RBAR [{method_key}]: {detail}")

    for method_key in sorted(series_logs.keys()):
        if str(method_key) in method_errors:
            continue
        if str(method_key) in method_errors:
            continue
        summary = series_logs.get(method_key, {}) or {}
        n_total = int(summary.get("n_total", 0) or 0)
        n_used = int(summary.get("n_used", 0) or 0)
        n_excluded = int(summary.get("n_excluded", 0) or 0)
        if n_excluded <= 0 and not (summary.get("excluded_series") or {}):
            continue  # nothing was excluded: not a problem, no warning
        lines.append(
            f"- Method [{method_key}]: used {n_used}/{n_total} {unit_label}; excluded {n_excluded}."
        )
        excluded = dict(summary.get("excluded_series", {}) or {})
        for series_name in sorted(excluded.keys()):
            detail = str(excluded.get(series_name, "") or "").strip()
            if not detail:
                detail = "Unknown exclusion reason."
            lines.append(f"  - {unit_label_singular} [{series_name}]: {detail}")

    for method_key in sorted(eps_series_logs.keys()):
        excluded = dict(eps_series_logs.get(method_key, {}) or {})
        if not excluded:
            continue
        lines.append(
            f"- EPS/RBAR exclusions [{method_key}]: {len(excluded)} series excluded."
        )
        for series_name in sorted(excluded.keys()):
            detail = str(excluded.get(series_name, "") or "").strip()
            if not detail:
                detail = "Unknown EPS/RBAR exclusion reason."
            lines.append(f"  - Series [{series_name}]: {detail}")
    return lines

GOAL_LABELS = {
    "highfreq": "Interannual (high-frequency) signal",
    "mixed": "Mixed (high + low frequency)",
    "lowfreq": "Multi-decadal / centennial trends",
    "temp_recon": "Temperature reconstruction",
    "runoff_recon": "Runoff / discharge reconstruction",
    "drought_recon": "Drought reconstruction (PDSI, SPEI)",
    "variance_check": "Variance / quality check",
}
 
GOAL_EXPLANATIONS = {
    "highfreq": (
        "Use when the main target is year-to-year variability (e.g., drought years, extreme events). "
        "The advisor prioritizes methods that retain more high-frequency variance while keeping a stable signal."
    ),
    "mixed": (
        "Use when you need a balanced chronology that keeps both interannual variability and multi-decadal structure. "
        "The advisor balances high- and low-frequency variance and signal strength."
    ),
    "lowfreq": (
        "Use when multi-decadal to centennial variability is the priority (e.g., long-term climate variability). "
        "The advisor prioritizes methods that preserve low-frequency variance and, if feasible, RCS approaches."
    ),
    "temp_recon": (
        "Use for temperature reconstruction tasks. Temperature often carries more low-frequency structure, "
        "but some sites may also include interannual information. The advisor balances both bands and favors "
        "methods with stronger climate coherence if a climate series is provided."
    ),
    "runoff_recon": (
        "Use for runoff/discharge reconstruction tasks. Runoff signals can be both interannual and decadal. "
        "The advisor balances high- and low-frequency variance and favors coherence with the climate/target series."
    ),
    "drought_recon": (
        "Use for drought indices (e.g., PDSI/SPEI). Drought often has strong interannual variability and some "
        "multi-year persistence. The advisor emphasizes both frequency bands and climate coherence."
    ),
    "variance_check": (
        "Use for quality control / exploratory variance comparison. The advisor aims for a stable balance of "
        "variance in both bands together with EPS support."
    ),
}
DEFAULT_SCORE_PROFILES: Dict[str, Dict[str, float]] = {
    "highfreq": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 20.0,
        "w_var_high": 0.50,
        "w_var_low": 0.00,
        "w_eps": 0.15,
        "w_rbar": 0.15,
        "w_climate": 0.20,
    },
    "mixed": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 20.0,
        "w_var_high": 0.25,
        "w_var_low": 0.25,
        "w_eps": 0.15,
        "w_rbar": 0.15,
        "w_climate": 0.20,
    },
    "lowfreq": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 30.0,
        "w_var_high": 0.00,
        "w_var_low": 0.50,
        "w_eps": 0.15,
        "w_rbar": 0.15,
        "w_climate": 0.20,
    },
    "temp_recon": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 30.0,
        "w_var_high": 0.15,
        "w_var_low": 0.35,
        "w_eps": 0.15,
        "w_rbar": 0.15,
        "w_climate": 0.20,
    },
    "runoff_recon": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 20.0,
        "w_var_high": 0.25,
        "w_var_low": 0.25,
        "w_eps": 0.15,
        "w_rbar": 0.15,
        "w_climate": 0.20,
    },
    "drought_recon": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 20.0,
        "w_var_high": 0.30,
        "w_var_low": 0.20,
        "w_eps": 0.15,
        "w_rbar": 0.15,
        "w_climate": 0.20,
    },
    "variance_check": {
        "high_cutoff_years": 10.0,
        "low_cutoff_years": 20.0,
        "w_var_high": 0.25,
        "w_var_low": 0.25,
        "w_eps": 0.20,
        "w_rbar": 0.20,
        "w_climate": 0.10,
    },
}


def _normalize_score_profile(profile: Optional[Dict[str, Any]], goal: GoalType = "mixed") -> Dict[str, float]:
    """Normalize score-profile values and force weights to sum to 1."""
    base = dict(DEFAULT_SCORE_PROFILES.get(goal, DEFAULT_SCORE_PROFILES["mixed"]))
    raw = dict(profile or {})

    def _get_num(name: str, fallback: float) -> float:
        try:
            val = float(raw.get(name, fallback))
        except Exception:
            val = float(fallback)
        if not np.isfinite(val):
            val = float(fallback)
        return float(val)

    out = {
        "high_cutoff_years": max(3.0, _get_num("high_cutoff_years", base["high_cutoff_years"])),
        "low_cutoff_years": max(3.0, _get_num("low_cutoff_years", base["low_cutoff_years"])),
        "w_var_high": max(0.0, _get_num("w_var_high", base["w_var_high"])),
        "w_var_low": max(0.0, _get_num("w_var_low", base["w_var_low"])),
        "w_eps": max(0.0, _get_num("w_eps", base["w_eps"])),
        "w_rbar": max(0.0, _get_num("w_rbar", base["w_rbar"])),
        "w_climate": max(0.0, _get_num("w_climate", base["w_climate"])),
    }
    wsum = (
        out["w_var_high"]
        + out["w_var_low"]
        + out["w_eps"]
        + out["w_rbar"]
        + out["w_climate"]
    )
    if wsum <= 0.0 or (not np.isfinite(wsum)):
        out.update(
            {
                "w_var_high": base["w_var_high"],
                "w_var_low": base["w_var_low"],
                "w_eps": base["w_eps"],
                "w_rbar": base["w_rbar"],
                "w_climate": base["w_climate"],
            }
        )
        wsum = (
            out["w_var_high"]
            + out["w_var_low"]
            + out["w_eps"]
            + out["w_rbar"]
            + out["w_climate"]
        )
    for k in ("w_var_high", "w_var_low", "w_eps", "w_rbar", "w_climate"):
        out[k] = float(out[k] / wsum)
    return out


def _format_score_rule_text(goal: GoalType, profile: Optional[Dict[str, Any]] = None) -> str:
    """Return a human-readable score formula for the current goal/profile."""
    p = _normalize_score_profile(profile, goal=goal)
    return (
        "score = "
        f"{p['w_var_high']:.2f} * C(Var(high)) + "
        f"{p['w_var_low']:.2f} * C(Var(low)) + "
        f"{p['w_eps']:.2f} * EPS + "
        f"{p['w_rbar']:.2f} * Rbar + "
        f"{p['w_climate']:.2f} * |climate corr|"
    )


SCORE_RULES = {
    g: _format_score_rule_text(g, DEFAULT_SCORE_PROFILES[g])
    for g in DEFAULT_SCORE_PROFILES.keys()
}
METHOD_EXPLANATIONS = {
    "exp": (
        "Negative exponential or linear detrending.\n"
        "How it is computed: fit a declining negative-exponential curve a*exp(b*x)+k with safeguards; "
        "if the fit fails, fall back to a linear trend or (if slope is positive) to mean detrending.\n"
        "Good when series show classic biological decline with age.\n"
        "Keeps a lot of low-frequency variability but can be sensitive "
        "to disturbances and non-classical growth patterns."
    ),
    "linear": (
        "Simple linear detrending.\n"
        "How it is computed: fit a straight line with least squares and divide observed values by the fitted trend.\n"
        "Captures monotonic trends without curvature; useful when exponential "
        "form is unnecessary or unstable."
    ),
    "mean": (
        "Mean-value detrending (divide by series mean).\n"
        "How it is computed: divide each series by its mean (a horizontal trend).\n"
        "Keeps all temporal structure and only normalizes the mean.\n"
        "Useful as a baseline when the biological trend is weak or when you want "
        "to maximize variance at all frequencies."
    ),
    "runmean_32": (
        "Running-mean detrending (~32-year window).\n"
        "How it is computed: compute a centered rolling mean trend and divide by it.\n"
        "Smooths multi-decadal variability while keeping interannual signal."
    ),
    "lowess": (
        "LOWESS / Friedman (dplR) detrending.\n"
        "How it is computed:\n"
        "- In dplR compatibility mode: uses dplR's 'Friedman' (supsmu) smoother via R.\n"
        "- In legacy mode: uses statsmodels LOWESS if available.\n"
        "Use when: you want flexible smoothing without assuming a functional form, typically for high-frequency emphasis."
    ),
    "age_dep_spline": (
        "Age-dependent spline (dplR).\n"
        "How it is computed: dplR 'AgeDepSpline' via R (age-dependent smoothing).\n"
        "Use when: age-related smoothing is preferred over a fixed spline wavelength."
    ),
    "ar": (
        "AR detrending (dplR).\n"
        "How it is computed: dplR 'Ar' method via R (autoregressive modeling).\n"
        "Use when: you want an AR-based approach comparable to dplR/ARSTAN-style options."
    ),
    "ssf_spline_30": (
        "Signal-free standardization (dplR) with spline (~30-year response).\n"
        "How it is computed: dplR 'ssf' via R; final indices are taken from the last iteration.\n"
        "Use when: you want signal-free detrending to reduce trend bias while preserving low-frequency variability."
    ),
    "ssf_spline_60": (
        "Signal-free standardization (dplR) with spline (~60-year response).\n"
        "How it is computed: dplR 'ssf' via R; final indices are taken from the last iteration."
    ),
    "ssf_spline_120": (
        "Signal-free standardization (dplR) with spline (~120-year response).\n"
        "How it is computed: dplR 'ssf' via R; final indices are taken from the last iteration."
    ),
    "ssf_age_dep_spline": (
        "Signal-free standardization (dplR) with age-dependent spline.\n"
        "How it is computed: dplR 'ssf' with method='AgeDepSpline' via R."
    ),
    "hugershoff": (
        "Hugershoff growth curve.\n"
        "How it is computed: fit a * x^b * c^x + d, then divide by the fitted curve; "
        "falls back to the exponential method if fitting fails.\n"
        "Can model early acceleration and later decline; falls back to negative "
        "exponential if the fit fails."
    ),
    "spline_30": (
        "Spline ~30-year response.\n"
        "How it is computed: fit a smoothing spline with a heuristic smoothness target (~30 years) and divide by it.\n"
        "Optimized for interannual to decadal variability; removes most "
        "age-related and multi-decadal trends."
    ),
    "spline_60": (
        "Spline ~60-year response.\n"
        "How it is computed: same as spline_30 but with a longer target (~60 years).\n"
        "Balance between interannual signal and multi-decadal trends."
    ),
    "spline_120": (
        "Spline ~120-year response.\n"
        "How it is computed: same as spline_30 but with a longer target (~120 years).\n"
        "Better preserves long-term variability, more aggressive at removing "
        "high-frequency noise."
    ),
    "rcs": (
        "Classic RCS detrending.\n"
        "How it is computed: estimate a common age-related growth curve across all series "
        "(here: median by cambial age) and divide each ring width by the curve value at its age.\n"
        "Assumes trees share a common age-related growth curve.\n"
        "Best when age structure is representative and disturbances are limited."
    ),
    "rcs_signal_free": (
        "Signal-free RCS.\n"
        "How it is computed: iterate RCS while removing the common chronology signal before re-estimating the age curve.\n"
        "Iteratively removes common signal before estimating the age curve.\n"
        "Helps reduce bias in the RCS curve and preserve low frequency."
    ),
}
def check_rcs_feasibility(
    rwl: pd. DataFrame,
    ages: pd. Series,
    disturbances: DisturbanceStats,
    min_max_age: int = 150,
    min_series_per_age: int = 10,
    min_fraction_ages_ok: float = 0.5,
    max_disturbance_fraction: float = 0.4,
    po: Optional[pd.DataFrame] = None,
) -> RCSFeasibility:
    """Check if RCS is feasible for the dataset.
    Criteria (defaults):
      - at least `min_max_age` years maximum series age,
      - at least `min_series_per_age` series for a given cambial age across >= `min_fraction_ages_ok` fraction of ages,
      - fraction of series with strong disturbance events must be <= `max_disturbance_fraction`.
    Returns:
      RCSFeasibility(possible: bool, reason: str)
    """
    # max age present among series
    po_vec = _get_po_vector(rwl, po=po) if po is not None else {}
    max_age = int(ages.max()) if not ages.empty else 0
    if max_age < min_max_age:
        return RCSFeasibility(
            possible=False,
            reason=f"Maximum age {max_age} < {min_max_age} years.",
        )
    # build counts by cambial age (how many series contribute at each age)
    age_counts: Dict[int, int] = {}
    for col in rwl.columns:
        ser = rwl[col].dropna()
        if ser.empty:
            continue
        local_ages = np.arange(1, len(ser) + 1, dtype=int) + int(po_vec.get(str(col), 1)) - 1
        for age, val in zip(local_ages, ser.values):
            if np.isnan(val):
                continue
            age_counts[int(age)] = age_counts.get(int(age), 0) + 1
    if not age_counts:
        return RCSFeasibility(False, "No usable age distribution for RCS.")
    counts_arr = np.array(list(age_counts.values()), dtype=float)
    n_ok = (counts_arr >= min_series_per_age).sum()
    frac_ok = n_ok / counts_arr.size if counts_arr.size > 0 else 0.0
    if frac_ok < min_fraction_ages_ok:
        return RCSFeasibility(
            possible=False,
            reason=(
                f"Only {frac_ok:.2f} of ages have >= {min_series_per_age} series; "
                f"required {min_fraction_ages_ok:.2f}."
            ),
        )
    # check disturbance fraction
    if (
        not math.isnan(disturbances.fraction_series_with_events)
        and disturbances.fraction_series_with_events > max_disturbance_fraction
    ):
        return RCSFeasibility(
            possible=False,
            reason=(
                "Too many series with strong releases/suppressions "
                f"({disturbances.fraction_series_with_events:.2f})."
            ),
        )
    return RCSFeasibility(True, "RCS feasible: age spread and disturbances acceptable.")
def diagnose_series_peaks(
    name: str,
    rwi: pd. Series,
    raw: Optional[pd. Series],
    depth: Optional[pd. Series] = None,
    difference_mode: bool = False,
    max_peaks: int = 6,
    method: str = "",
) -> Dict[str, Any]:
    """Find unusual index values of one series and explain where they come from.

    The fitted trend is reconstructed from the input and the index (ratio: fit = raw / index;
    difference indices scaled by the series mean: fit = raw - (index - 1) * mean).
    Reasons: division by a very small fitted value (edge of the curve or near-zero widths),
    an exceptionally wide/narrow ring in the measurements, the first/last years of the series
    (end effects of the curve) and low sample depth of the chronology in that year.
    """
    idx = pd.to_numeric(rwi, errors="coerce")
    valid = idx.dropna()
    out: Dict[str, Any] = {"peaks": [], "fit": None}
    if valid.empty:
        out["text"] = f"Selected: {name} - no finite index values."
        return out
    first, last = int(valid.index.min()), int(valid.index.max())
    fit = None
    rawv = None
    if raw is not None:
        rawv = pd.to_numeric(raw, errors="coerce").reindex(idx.index)
        with np.errstate(divide="ignore", invalid="ignore"):
            if difference_mode:
                mu = float(np.nanmean(rawv.values)) if rawv.notna().any() else 1.0
                fit = rawv - (idx - 1.0) * mu
            else:
                fit = rawv / idx.where(idx.abs() > 1e-9)
        out["fit"] = fit
    q1, q3 = float(valid.quantile(0.25)), float(valid.quantile(0.75))
    iqr = max(q3 - q1, 1e-9)
    hi_thr = min(max(q3 + 3.0 * iqr, float(valid.median()) + 1.0), 3.0)
    lo_thr = q1 - 3.0 * iqr
    cand = valid[(valid > hi_thr) | (valid < lo_thr)].sort_values(key=lambda s: -(s - valid.median()).abs())
    mean_raw = float(np.nanmean(rawv.values)) if rawv is not None and rawv.notna().any() else float("nan")
    sd_raw = float(np.nanstd(rawv.values)) if rawv is not None and rawv.notna().any() else float("nan")
    for yr, val in cand.head(max_peaks).items():
        y = int(yr)
        reasons: List[str] = []
        f = float(fit.get(y)) if fit is not None and pd.notna(fit.get(y)) else float("nan")
        rv = float(rawv.get(y)) if rawv is not None and pd.notna(rawv.get(y)) else float("nan")
        if np.isfinite(f) and np.isfinite(mean_raw) and not difference_mode and abs(f) < 0.3 * abs(mean_raw):
            reasons.append(f"the fitted trend is very small here ({f:.3f} vs series mean {mean_raw:.3f}), "
                           "so the ratio width/trend is inflated")
        if np.isfinite(rv) and np.isfinite(mean_raw) and np.isfinite(sd_raw) and sd_raw > 0:
            z = (rv - mean_raw) / sd_raw
            if z > 3:
                reasons.append(f"the measured ring is exceptionally wide ({rv:.3f}, {z:.1f} SD above the mean): "
                               "release, false/merged ring or a measurement error - check the sample")
            elif z < -2.5:
                reasons.append(f"the measured ring is exceptionally narrow ({rv:.3f})")
        if y - first < 10:
            reasons.append("first years of the series: the curve is poorly constrained at the end "
                           "(juvenile growth / end effect)")
        elif last - y < 10:
            reasons.append("last years of the series: end effect of the fitted curve")
        if depth is not None:
            try:
                d = float(depth.get(y))
                if np.isfinite(d) and d < 5:
                    reasons.append(f"only {int(d)} series in this year, so the peak also dominates the chronology")
            except Exception:
                pass
        if not reasons:
            reasons.append("the ring is wide/narrow relative to the local trend (real growth anomaly or "
                           "a dating problem in this series)")
        out["peaks"].append({"year": y, "index": float(val), "reasons": reasons})
    head = (f"Selected: {name}  |  {first}-{last} ({len(valid)} values)  |  index mean {float(valid.mean()):.3f}, "
            f"max {float(valid.max()):.3f} in {int(valid.idxmax())}, min {float(valid.min()):.3f} in {int(valid.idxmin())}")
    notes: List[str] = []
    m_idx = float(valid.mean())
    if not difference_mode and (m_idx > 1.5 or m_idx < 0.67):
        notes.append(f"the mean index of this series is {m_idx:.2f} instead of about 1: the fitted curve does not "
                     "follow the growth of this sample")
        if "rcs" in str(method).lower():
            notes.append("RCS divides every series by ONE common age curve: trees that grow faster or slower than "
                         "the average, or series whose distance to the pith is unknown (they are treated as "
                         "starting at the pith), get indices far from 1. Import pith offsets (Project > Import pith "
                         "info) or use Tree groups; check whether this tree belongs to the same population")
    if rawv is not None and fit is not None:
        rv = rawv.reindex(valid.index)
        fv = fit.reindex(valid.index)
        n3 = max(5, len(valid) // 3)
        r_early, r_late = float(np.nanmean(rv.values[:n3])), float(np.nanmean(rv.values[-n3:]))
        f_early, f_late = float(np.nanmean(fv.values[:n3])), float(np.nanmean(fv.values[-n3:]))
        if np.isfinite(r_early) and np.isfinite(f_early) and r_early > 0 and f_early > 0:
            if r_late / r_early > 1.6 and f_late / f_early < 1.15:
                notes.append(f"growth increases late in the series (mean width {r_early:.2f} -> {r_late:.2f}, e.g. a "
                             "release after canopy opening) while the curve of this method can only stay level or "
                             "decline (negative exponential, age-dependent spline, RCS age curve), so the late indices "
                             "become large. A flexible spline (spline_30/60) follows such changes; also check the sample")
            elif r_late / r_early < 0.5 and f_late / f_early > 0.8:
                notes.append("growth drops strongly late in the series (suppression or injury) and the curve does not "
                             "follow it, so the late indices are small")
        roll = rv.rolling(15, min_periods=8).mean()
        if roll.notna().sum() > 30:
            ratio = (roll / roll.shift(15)).replace([np.inf, -np.inf], np.nan).dropna()
            if not ratio.empty and float(ratio.max()) > 2.5:
                ystep = int(ratio.idxmax())
                notes.append(f"abrupt growth increase around {ystep - 15}-{ystep} (15-year mean more than 2.5 times "
                             "higher): possible release or a change of the measured radius")
    if not out["peaks"] and not notes:
        out["text"] = head + "  |  no unusual peaks."
        return out
    lines = [head]
    if notes:
        lines.append("Series: " + "; ".join(notes) + ".")
    if out["peaks"]:
        lines.append("Peaks:")
    for pk in out["peaks"][:4]:
        lines.append(f"  {pk['year']}: index {pk['index']:.2f} - " + "; ".join(pk["reasons"]))
    if any("very small" in " ".join(pk["reasons"]) for pk in out["peaks"]):
        lines.append("Tip: compare with a stiffer curve (spline_120, exp, mean) or POWT (difference indices), "
                     "which avoid division by values close to zero.")
    out["text"] = "\n".join(lines)
    return out


def diagnose_chronology_peaks(
    method: str,
    chron: pd. Series,
    rwi: Optional[pd. DataFrame],
    depth: Optional[pd. Series] = None,
    other_chronologies: Optional[Dict[str, pd. Series]] = None,
    max_peaks: int = 4,
) -> str:
    """Explain the largest deviations of a chronology: which series make them, replication,
    agreement with the other detrending methods, end effects and long trend distortions."""
    c = pd.to_numeric(pd.Series(chron), errors="coerce").dropna()
    if c.empty:
        return f"{method}: no values."
    c.index = pd.Index(c.index).astype(int)
    med = float(c.median())
    q1, q3 = float(c.quantile(0.25)), float(c.quantile(0.75))
    iqr = max(q3 - q1, 1e-9)
    dev = (c - med).abs()
    thr = max(3.0 * iqr, 0.25)
    peaks = dev[dev > thr].sort_values(ascending=False).head(max_peaks)
    first, last = int(c.index.min()), int(c.index.max())
    lines = [f"{method}: {first}-{last}, median {med:.2f}, max {float(c.max()):.2f} in {int(c.idxmax())}, "
             f"min {float(c.min()):.2f} in {int(c.idxmin())}"]
    roll = c.rolling(31, center=True, min_periods=15).mean()
    if roll.notna().any():
        hi, lo = float(roll.max()), float(roll.min())
        if hi > med + 0.2 or lo < med - 0.2:
            yh, yl = int(roll.idxmax()), int(roll.idxmin())
            near_end = any(min(abs(y - first), abs(y - last)) < 40 for y in (yh, yl))
            lines.append(f"Long-term: the 31-year mean ranges from {lo:.2f} ({yl}) to {hi:.2f} ({yh})"
                         + (" - mostly at the ends of the chronology, where the curves of single series are least "
                            "constrained (end effect / trend distortion)." if near_end else
                            " - low-frequency variability kept by this method."))
    if peaks.empty:
        lines.append("No single years stand out.")
        return "\n".join(lines)
    lines.append("Largest single-year deviations:")
    for y, _d in peaks.items():
        y = int(y)
        v = float(c.loc[y])
        why: List[str] = []
        if rwi is not None and y in rwi.index:
            row = pd.to_numeric(rwi.loc[y], errors="coerce").dropna()
            n = len(row)
            if n:
                up = v > med
                extreme = row[row > med + thr] if up else row[row < med - thr]
                share = len(extreme) / n
                top = (row.sort_values(ascending=not up)).head(3)
                tops = ", ".join(f"{k} {float(x):.2f}" for k, x in top.items())
                if share >= 0.5:
                    why.append(f"{len(extreme)} of {n} series are {'high' if up else 'low'} - a common signal "
                               f"(pointer year, likely climatic)")
                elif len(extreme) <= max(2, int(0.15 * n)):
                    why.append(f"made by few series ({len(extreme)} of {n}): {tops} - check these samples "
                               f"(dating, wide/false ring, fit of their curve)")
                else:
                    why.append(f"{len(extreme)} of {n} series contribute (strongest: {tops})")
        if depth is not None:
            try:
                d = float(depth.get(y))
                if np.isfinite(d) and d < 10:
                    why.append(f"low sample depth ({int(d)} series)")
            except Exception:
                pass
        if other_chronologies:
            vals = []
            for _k, oc in other_chronologies.items():
                try:
                    ov = float(pd.Series(oc).get(y))
                    if np.isfinite(ov):
                        vals.append(ov)
                except Exception:
                    pass
            if vals:
                om = float(np.median(vals))
                if abs(v - om) > 0.5 * abs(v - med) and abs(v - om) > 0.15:
                    why.append(f"the other methods give {om:.2f} here - the peak comes from the curve of this "
                               f"method, not from the measurements")
                else:
                    why.append(f"the other methods agree ({om:.2f})")
        if y - first < 20 or last - y < 20:
            why.append("near the end of the chronology")
        lines.append(f"  {y}: {v:.2f} - " + "; ".join(why))
    return "\n".join(lines)


def _parameterized_method(key: str) -> Optional[tuple[str, int]]:
    """Decode a validated parameterized method identifier."""
    match = re.fullmatch(r"(spline|ssf_spline|runmean)_([0-9]+)", str(key))
    if match is None:
        return None
    period = int(match.group(2))
    if not 3 <= period <= 10000:
        raise ValueError("Method period must be a whole number from 3 to 10000 years.")
    if str(period) != match.group(2):
        raise ValueError("Method period must not contain leading zeros.")
    return match.group(1), period


def _normalize_custom_methods(value: Any) -> list[str]:
    """Validate saved variants and exclude duplicate built-in presets."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("Custom methods must be a list.")
    result = []
    for key in value:
        key = str(key)
        if _parameterized_method(key) is None:
            raise ValueError(f"Unsupported custom method: {key}")
        if key not in VALIDATION_METHOD_KEYS and key not in result:
            result.append(key)
    return result


def _method_explanation(key: str, default: str = "") -> str:
    if key in METHOD_EXPLANATIONS:
        return METHOD_EXPLANATIONS[key]
    variant = _parameterized_method(key)
    if variant is None:
        return default
    kind, period = variant
    if kind == "runmean":
        return f"Running mean detrending: {period}-year window, minimum 3 observations."
    prefix = "Signal-free spline" if kind == "ssf_spline" else "Spline"
    return (f"{prefix} detrending: {period}-year wavelength at 50% frequency response "
            "(f=0.5), using the same algorithm as the standard spline presets.")


def run_advisor_core(
    rwl: pd.DataFrame,
    goal: GoalType,
    climate: Optional[pd.Series] = None,
    compat_config: Optional[DplrCompatConfig] = None,
    po: Optional[pd.DataFrame] = None,
    ids: Optional[pd.DataFrame] = None,
    method_keys: Optional[list[str]] = None,
    score_profile: Optional[Dict[str, Any]] = None,
    min_series_for_detrending: int = 1,
    min_series_count_mode: str = "series",
    unknown_pith_policy: str = "present",
) -> AdvisorResult:
    """Core advisor: diagnostics + candidate evaluation.

    ``po`` (series, pith.offset) may contain NaN for unknown pith; see
    ``unknown_pith_policy`` ("present" = treat as offset 1, "exclude" = leave
    those series out of RCS / signal-free RCS).
    """
    rwl = _ensure_unique_sorted_year_index(_apply_arstan_missing_codes(rwl))
    min_series_for_detrending = int(max(1, min_series_for_detrending))
    min_series_count_mode = str(min_series_count_mode or "series").strip().lower()
    if min_series_count_mode not in ("series", "trees"):
        min_series_count_mode = "series"
    rwl_original = rwl.copy()
    rwl, original_depth, analysis_start_year = trim_rwl_to_minimum_depth_start(
        rwl,
        min_series=min_series_for_detrending,
        ids=ids,
        count_mode=min_series_count_mode,
    )
    if analysis_start_year is None or rwl.empty:
        raise RuntimeError(
            "No year satisfies the minimum detrending threshold of "
            f"{min_series_for_detrending} {min_series_count_mode}."
        )
    ids = _align_ids_to_series_names(ids, [str(x) for x in list(rwl.columns)])
    analysis_input_series_count = int(rwl.shape[1])
    analysis_input_tree_count = int(analysis_input_series_count)
    if ids is not None and not ids.empty and "tree" in ids.columns:
        try:
            tree_vals = pd.to_numeric(ids["tree"], errors="coerce")
            analysis_input_tree_count = int(pd.Index(tree_vals.dropna().astype(int)).nunique())
        except Exception:
            analysis_input_tree_count = int(analysis_input_series_count)
    rwl, ids, po = _collapse_rwl_to_tree_means(
        rwl,
        ids=ids,
        po=po,
    )
    analysis_output_count = int(rwl.shape[1])
    analysis_matrix_kind = "trees" if analysis_output_count < analysis_input_series_count else "series"
    po_raw_input = po
    po, rcs_excluded = _prepare_po_for_run(rwl, po_raw_input, unknown_pith_policy)
    rcs_cols = [c for c in rwl.columns if str(c) not in set(rcs_excluded)]
    rwl_rcs = rwl.loc[:, rcs_cols]
    pith_note = ""
    if po is not None:
        n_unknown = sum(1 for v in _po_lookup_raw(po_raw_input).values() if not np.isfinite(v))
        n_unknown += sum(1 for c in rwl.columns if str(c) not in _po_lookup_raw(po_raw_input))
        n_off = int((po["pith.offset"] > 1).sum())
        pith_note = (
            f"Pith offsets used: {n_off} of {int(rwl.shape[1])} "
            f"{'trees' if analysis_matrix_kind == 'trees' else 'series'} start away from the pith"
            + (f"; {n_unknown} with unknown pith" if n_unknown else "")
            + (f" (excluded from RCS: {len(rcs_excluded)})" if rcs_excluded else
               (" (treated as starting at the pith)" if n_unknown else ""))
        )
    ages = compute_ages(rwl, po=po)
    age_curve = build_age_curve(rwl, po=po)
    depth = compute_effective_depth(rwl, ids=ids, count_mode=min_series_count_mode)
    disturbances = detect_releases_suppressions(rwl)
    if rcs_cols:
        rcs_feas = check_rcs_feasibility(rwl_rcs, ages.loc[rcs_cols], disturbances, po=po)
    else:
        rcs_feas = RCSFeasibility(False, "All series have unknown pith and are excluded from RCS.")
    candidates: Dict[str, CandidateResult] = {}

    cfg = compat_config or DEFAULT_COMPAT_CONFIG
    score_profile_local = _normalize_score_profile(score_profile, goal=goal)
    use_dplr = (cfg.compat == "dplR")
    selected_method_set: Optional[set[str]] = None
    if method_keys:
        selected_method_set = {str(k).strip() for k in method_keys if str(k).strip()}

    custom_keys = [
        key for key in sorted(selected_method_set or [])
        if key not in VALIDATION_METHOD_KEYS and _parameterized_method(key) is not None
    ]

    def _want_method(key: str) -> bool:
        return selected_method_set is None or key in selected_method_set

    method_series_logs: Dict[str, Dict[str, Any]] = {}
    eps_series_logs: Dict[str, Dict[str, str]] = {}

    def _collect_series_exclusion_summary(
        rwi: pd.DataFrame,
        explicit_errors: Optional[Dict[str, str]] = None,
        min_finite_values: int = 2,
    ) -> Dict[str, Any]:
        """Summarize which series remained usable for a method and which were excluded."""
        explicit = dict(explicit_errors or {})
        used_series: list[str] = []
        excluded_series: Dict[str, str] = {}
        for col in list(rwi.columns):
            name = str(col)
            try:
                vals = pd.to_numeric(rwi[col], errors="coerce").to_numpy(dtype=float)
                n_finite = int(np.isfinite(vals).sum())
            except Exception as exc:
                n_finite = 0
                explicit.setdefault(name, f"{type(exc).__name__}: {exc}")
            if name in explicit:
                excluded_series[name] = str(explicit.get(name, "")).strip() or "Unknown series error."
            elif n_finite < int(max(1, min_finite_values)):
                excluded_series[name] = (
                    f"No usable detrended output was produced for this series "
                    f"({n_finite} finite value(s))."
                )
            else:
                used_series.append(name)
        return {
            "n_total": int(len(rwi.columns)),
            "n_used": int(len(used_series)),
            "n_excluded": int(len(excluded_series)),
            "used_series": used_series,
            "excluded_series": excluded_series,
        }

    def _compute_eps_series_exclusions(
        rwi: pd.DataFrame,
        min_finite_values: int = 2,
    ) -> Dict[str, str]:
        """Return per-series reasons for exclusion from EPS/Rbar calculations."""
        out: Dict[str, str] = {}
        for col in list(rwi.columns):
            name = str(col)
            try:
                vals = pd.to_numeric(rwi[col], errors="coerce").to_numpy(dtype=float)
                n_finite = int(np.isfinite(vals).sum())
            except Exception as exc:
                out[name] = f"{type(exc).__name__}: {exc}"
                continue
            if n_finite < int(max(1, min_finite_values)):
                out[name] = (
                    "Excluded from EPS/RBAR because fewer than "
                    f"{int(max(1, min_finite_values))} finite detrended values remained "
                    f"({n_finite} finite value(s))."
                )
        return out

    def _add_candidate(
        key: str,
        description: str,
        rwi: pd. DataFrame,
        chron_std_override: Optional[pd. Series] = None,
        series_errors: Optional[Dict[str, str]] = None,
    ) -> None:
        if not _want_method(key):
            return
        if bool(getattr(cfg, "scale_difference", False)) and rwi is not None and not rwi.empty:
            # difference indices -> 1 + d / mean of the (transformed) input series
            scaled = rwi.astype(float).copy()
            for col in scaled.columns:
                base = rwl[col] if col in rwl.columns else None
                mu = float(np.nanmean(pd.to_numeric(base, errors="coerce"))) if base is not None else float("nan")
                if not np.isfinite(mu) or abs(mu) < 1e-9:
                    mu = float(np.nanmean(np.abs(pd.to_numeric(base, errors="coerce")))) if base is not None else 1.0
                    if not np.isfinite(mu) or mu < 1e-9:
                        mu = 1.0
                scaled[col] = 1.0 + scaled[col] / mu
            rwi = scaled
            chron_std_override = None
        if series_errors is None:
            series_errors = dict(
                ((dplr_extras.get("dplr_series_exclusions", {}) or {}).get(key, {}) or {})
            )
        method_series_logs[key] = _collect_series_exclusion_summary(
            rwi,
            explicit_errors=series_errors,
            min_finite_values=2,
        )
        eps_series_logs[key] = _compute_eps_series_exclusions(
            rwi,
            min_finite_values=2,
        )
        m = compute_candidate_metrics(
            rwi,
            climate,
            ids=ids,
            high_cutoff_years=float(score_profile_local["high_cutoff_years"]),
            low_cutoff_years=float(score_profile_local["low_cutoff_years"]),
        )
        c_std = chron_std_override if chron_std_override is not None else build_chronology(rwi)
        c_res = build_residual_chronology(rwi)
        candidates[key] = CandidateResult(
            name=key,
            description=description,
            rwi=rwi,
            chronology_std=c_std,
            chronology_res=c_res,
            metrics=m,
            score=score_candidate(m, goal, score_profile=score_profile_local),
        )

    def _apply_simple_method_logged(
        method_key: str,
        description: str,
        detrend_block_func,
    ) -> None:
        """Apply a simple per-series detrending method with series-level error logging."""
        cols_out: Dict[str, pd. Series] = {}
        series_errors: Dict[str, str] = {}
        for col in list(rwl.columns):
            src = rwl[col]
            try:
                out = _apply_blockwise_detrend(
                    src,
                    detrend_block_func,
                    recode_zeros=cfg.recode_zeros,
                )
            except Exception as exc:
                out = pd.Series(
                    np.full(src.shape[0], np.nan, dtype=float),
                    index=src.index,
                    dtype=float,
                )
                series_errors[str(col)] = f"{type(exc).__name__}: {exc}"
            try:
                vals = pd.to_numeric(out, errors="coerce").to_numpy(dtype=float)
                n_finite = int(np.isfinite(vals).sum())
            except Exception as exc:
                n_finite = 0
                series_errors.setdefault(str(col), f"{type(exc).__name__}: {exc}")
            if n_finite < 2 and str(col) not in series_errors:
                series_errors[str(col)] = (
                    "No usable detrended output was produced for this series."
                )
            cols_out[str(col)] = out.astype(float)
        rwi_method = pd.DataFrame(cols_out, index=rwl.index)
        _add_candidate(
            method_key,
            description,
            rwi_method,
            series_errors=series_errors,
        )

    dplr_rwi_by: Dict[str, pd. DataFrame] = {}
    dplr_chron_std_by: Dict[str, pd. Series] = {}
    dplr_extras: Dict[str, Any] = {}
    dplr_keys: list[str] = []
    if use_dplr:
        # Compute all dplR-backed candidates in one R invocation for consistency.
        dplr_keys = [
            "exp",
            "linear",
            "runmean_32",
            "mean",
            "hugershoff",
            "lowess",
            "spline_30",
            "spline_60",
            "spline_120",
            "age_dep_spline",
            "ar",
            "ssf_spline_30",
            "ssf_spline_60",
            "ssf_spline_120",
            "ssf_age_dep_spline",
        ]
        dplr_keys.extend(custom_keys)
        if rcs_feas.possible:
            dplr_keys.extend(["rcs", "rcs_signal_free"])
        if selected_method_set is not None:
            dplr_keys = [k for k in dplr_keys if k in selected_method_set]
        rcs_keys_sep = [k for k in dplr_keys if k in ("rcs", "rcs_signal_free")] if rcs_excluded else []
        try:
            dplr_rwi_by, dplr_chron_std_by, dplr_extras = _dplr_compute_methods(
                rwl=rwl,
                method_keys=[k for k in dplr_keys if k not in rcs_keys_sep],
                config=cfg,
                po=po,
            )
            if rcs_keys_sep:
                # RCS only for series with known pith
                po_rcs = po.loc[po["series"].astype(str).isin([str(c) for c in rcs_cols])] if po is not None else None
                r_rwi, r_std, r_extra = _dplr_compute_methods(
                    rwl=rwl_rcs,
                    method_keys=rcs_keys_sep,
                    config=cfg,
                    po=po_rcs,
                )
                for k_r, v_r in r_rwi.items():
                    dplr_rwi_by[k_r] = v_r.reindex(columns=rwl.columns)
                dplr_chron_std_by.update(r_std)
                for k_e, v_e in (r_extra or {}).items():
                    if isinstance(v_e, dict) and isinstance(dplr_extras.get(k_e), dict):
                        dplr_extras[k_e].update(v_e)
                    else:
                        dplr_extras[k_e] = v_e
        except Exception:
            if cfg.strict_dplr:
                raise
            use_dplr = False
    dplr_method_errors: Dict[str, str] = dict(dplr_extras.get("dplr_errors", {}) or {})
    if use_dplr:
        for method_key in dplr_keys:
            if (
                method_key not in dplr_rwi_by
                and method_key not in dplr_chron_std_by
                and method_key not in dplr_method_errors
            ):
                dplr_method_errors[method_key] = (
                    "No dplR detrending output was produced for this method."
                )

    # 1) Negative exponential
    if _want_method("exp"):
        if use_dplr and "exp" in dplr_rwi_by:
            _add_candidate(
                "exp",
                "Negative exponential / linear detrending (dplR ModNegExp via R).",
                dplr_rwi_by["exp"],
                chron_std_override=dplr_chron_std_by.get("exp"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "exp",
                "Negative exponential / linear detrending.",
                lambda b: detrend_negexp(
                    b,
                    difference=cfg.difference,
                    pos_slope=cfg.pos_slope,
                    constrain_nls=cfg.constrain_nls,
                ),
            )


    # 1b) Pure linear trend
    if _want_method("linear"):
        if use_dplr and "linear" in dplr_rwi_by:
            _add_candidate(
                "linear",
                "Linear detrending (dplR lm / custom in R).",
                dplr_rwi_by["linear"],
                chron_std_override=dplr_chron_std_by.get("linear"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "linear",
                "Linear detrending (straight line).",
                lambda b: detrend_linear(
                    b,
                    pos_slope=cfg.pos_slope,
                    difference=cfg.difference,
                ),
            )

    # 1c) Mean-value (horizontal) detrending
    if _want_method("mean"):
        if use_dplr and "mean" in dplr_rwi_by:
            _add_candidate(
                "mean",
                "Mean-value detrending (dplR Mean via R).",
                dplr_rwi_by["mean"],
                chron_std_override=dplr_chron_std_by.get("mean"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "mean",
                "Mean-value detrending (divide by series mean).",
                lambda b: detrend_mean(
                    b,
                    difference=cfg.difference,
                ),
            )

    # 1d) 32-year running mean
    if _want_method("runmean_32"):
        if use_dplr and "runmean_32" in dplr_rwi_by:
            _add_candidate(
                "runmean_32",
                "32-year running-mean detrending (custom in R).",
                dplr_rwi_by["runmean_32"],
                chron_std_override=dplr_chron_std_by.get("runmean_32"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "runmean_32",
                "32-year running-mean detrending.",
                lambda b: detrend_running_mean(
                    b,
                    window=32,
                    difference=cfg.difference,
                ),
            )

    # 2) LOWESS (if available)
    if _want_method("lowess") and ((use_dplr and "lowess" in dplr_rwi_by) or (sm_lowess is not None)):
        if use_dplr and "lowess" in dplr_rwi_by:
            _add_candidate(
                "lowess",
                "Friedman/supsmu detrending (dplR Friedman via R).",
                dplr_rwi_by["lowess"],
                chron_std_override=dplr_chron_std_by.get("lowess"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "lowess",
                "LOWESS detrending (local regression).",
                lambda b: detrend_lowess(
                    b,
                    difference=cfg.difference,
                ),
            )
    # 3) Hugershoff
    if _want_method("hugershoff"):
        if use_dplr and "hugershoff" in dplr_rwi_by:
            _add_candidate(
                "hugershoff",
                "Hugershoff detrending (dplR ModHugershoff via R).",
                dplr_rwi_by["hugershoff"],
                chron_std_override=dplr_chron_std_by.get("hugershoff"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "hugershoff",
                "Hugershoff detrending.",
                lambda b: detrend_hugershoff(
                    b,
                    difference=cfg.difference,
                    pos_slope=cfg.pos_slope,
                    constrain_nls=cfg.constrain_nls,
                ),
            )

    # Custom variants are calculated exclusively by R/dplR.
    for key in custom_keys:
        if key in dplr_rwi_by:
            _add_candidate(key, _method_explanation(key), dplr_rwi_by[key],
                           chron_std_override=dplr_chron_std_by.get(key))
        else:
            dplr_method_errors.setdefault(key, "No R/dplR output was produced for this custom method.")

    # 4) Spline 30
    if _want_method("spline_30"):
        if use_dplr and "spline_30" in dplr_rwi_by:
            _add_candidate(
                "spline_30",
                "Spline detrending (~30-year response; dplR Spline via R).",
                dplr_rwi_by["spline_30"],
                chron_std_override=dplr_chron_std_by.get("spline_30"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "spline_30",
                "Spline detrending (~30-year response).",
                lambda b: detrend_spline(
                    b,
                    target_years=30,
                    difference=cfg.difference,
                    smoothing_strength=1.0,
                ),
            )

    # 5) Spline 60
    if _want_method("spline_60"):
        if use_dplr and "spline_60" in dplr_rwi_by:
            _add_candidate(
                "spline_60",
                "Spline detrending (~60-year response; dplR Spline via R).",
                dplr_rwi_by["spline_60"],
                chron_std_override=dplr_chron_std_by.get("spline_60"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "spline_60",
                "Spline detrending (~60-year response).",
                lambda b: detrend_spline(
                    b,
                    target_years=60,
                    difference=cfg.difference,
                    smoothing_strength=1.0,
                ),
            )

    # 6) Spline 120
    if _want_method("spline_120"):
        if use_dplr and "spline_120" in dplr_rwi_by:
            _add_candidate(
                "spline_120",
                "Spline detrending (~120-year response; dplR Spline via R).",
                dplr_rwi_by["spline_120"],
                chron_std_override=dplr_chron_std_by.get("spline_120"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "spline_120",
                "Spline detrending (~120-year response).",
                lambda b: detrend_spline(
                    b,
                    target_years=120,
                    difference=cfg.difference,
                    smoothing_strength=1.0,
                ),
            )

    # Additional dplR-only candidates (if available)
    if _want_method("age_dep_spline"):
        if use_dplr and "age_dep_spline" in dplr_rwi_by:
            _add_candidate(
                "age_dep_spline",
                "Age-dependent spline (dplR AgeDepSpline via R).",
                dplr_rwi_by["age_dep_spline"],
                chron_std_override=dplr_chron_std_by.get("age_dep_spline"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            _apply_simple_method_logged(
                "age_dep_spline",
                "Age-dependent spline detrending.",
                lambda b: detrend_age_dep_spline(
                    b,
                    difference=cfg.difference,
                    pos_slope=cfg.pos_slope,
                    base_years=50,
                ),
            )

    if _want_method("ar"):
        if use_dplr and "ar" in dplr_rwi_by:
            _add_candidate(
                "ar",
                "AR detrending (dplR Ar via R).",
                dplr_rwi_by["ar"],
                chron_std_override=dplr_chron_std_by.get("ar"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            rwi_ar = rwl.apply(
                lambda s: _apply_blockwise_detrend(
                    s,
                    lambda b: detrend_ar(
                        b,
                        difference=cfg.difference,
                        max_order=10,
                    ),
                    recode_zeros=cfg.recode_zeros,
                ),
                axis=0,
            )
            _add_candidate("ar", "AR detrending.", rwi_ar)

    if _want_method("ssf_spline_30"):
        if use_dplr and "ssf_spline_30" in dplr_rwi_by:
            _add_candidate(
                "ssf_spline_30",
                "Signal-free spline (~30-year; dplR ssf via R).",
                dplr_rwi_by["ssf_spline_30"],
                chron_std_override=dplr_chron_std_by.get("ssf_spline_30"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            rwi_ssf30 = signal_free_spline_dplr_like(
                rwl,
                target_years=30,
                recode_zeros=cfg.recode_zeros,
                difference=cfg.difference,
                max_iterations=60,
                mad_threshold=1e-3,
            )
            _add_candidate("ssf_spline_30", "Signal-free spline (~30-year).", rwi_ssf30)
            dplr_method_errors.pop("ssf_spline_30", None)

    if _want_method("ssf_spline_60"):
        if use_dplr and "ssf_spline_60" in dplr_rwi_by:
            _add_candidate(
                "ssf_spline_60",
                "Signal-free spline (~60-year; dplR ssf via R).",
                dplr_rwi_by["ssf_spline_60"],
                chron_std_override=dplr_chron_std_by.get("ssf_spline_60"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            rwi_ssf60 = signal_free_spline_dplr_like(
                rwl,
                target_years=60,
                recode_zeros=cfg.recode_zeros,
                difference=cfg.difference,
                max_iterations=60,
                mad_threshold=1e-3,
            )
            _add_candidate("ssf_spline_60", "Signal-free spline (~60-year).", rwi_ssf60)
            dplr_method_errors.pop("ssf_spline_60", None)

    if _want_method("ssf_spline_120"):
        if use_dplr and "ssf_spline_120" in dplr_rwi_by:
            _add_candidate(
                "ssf_spline_120",
                "Signal-free spline (~120-year; dplR ssf via R).",
                dplr_rwi_by["ssf_spline_120"],
                chron_std_override=dplr_chron_std_by.get("ssf_spline_120"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            rwi_ssf120 = signal_free_spline_dplr_like(
                rwl,
                target_years=120,
                recode_zeros=cfg.recode_zeros,
                difference=cfg.difference,
                max_iterations=60,
                mad_threshold=1e-3,
            )
            _add_candidate("ssf_spline_120", "Signal-free spline (~120-year).", rwi_ssf120)
            dplr_method_errors.pop("ssf_spline_120", None)

    if _want_method("ssf_age_dep_spline"):
        if use_dplr and "ssf_age_dep_spline" in dplr_rwi_by:
            _add_candidate(
                "ssf_age_dep_spline",
                "Signal-free age-dependent spline (dplR ssf via R).",
                dplr_rwi_by["ssf_age_dep_spline"],
                chron_std_override=dplr_chron_std_by.get("ssf_age_dep_spline"),
            )
        elif cfg.strict_dplr and cfg.compat == "dplR":
            # Skip: strict dplR mode forbids non-dplR fallbacks.
            pass
        else:
            rwi_ssf_ads = signal_free_detrend(
                rwl,
                detrend_block_func=lambda b: detrend_age_dep_spline(
                    b,
                    difference=cfg.difference,
                    pos_slope=cfg.pos_slope,
                    base_years=50,
                ),
                recode_zeros=cfg.recode_zeros,
                difference=cfg.difference,
                iterations=6,
            )
            _add_candidate(
                "ssf_age_dep_spline",
                "Signal-free age-dependent spline.",
                rwi_ssf_ads,
            )
            dplr_method_errors.pop("ssf_age_dep_spline", None)

    rcs_std: Optional[pd. Series] = None
    rcs_sf_std: Optional[pd. Series] = None
    rcs_sf_curve: Optional[pd. Series] = None
    if rcs_feas.possible and (_want_method("rcs") or _want_method("rcs_signal_free")):
        try:
            if _want_method("rcs"):
                if use_dplr and "rcs" in dplr_rwi_by:
                    _add_candidate(
                        "rcs",
                        "Classic RCS detrending (dplR rcs via R).",
                        dplr_rwi_by["rcs"],
                        chron_std_override=dplr_chron_std_by.get("rcs"),
                    )
                    rcs_std = dplr_chron_std_by.get("rcs")
                elif cfg.strict_dplr and cfg.compat == "dplR":
                    # Skip: strict dplR mode forbids non-dplR fallbacks.
                    pass
                else:
                    rcs_res = rcs_detrend(
                        rwl_rcs,
                        po=po,
                        difference=cfg.difference,
                        recode_zeros=cfg.recode_zeros,
                    )
                    _add_candidate(
                        "rcs",
                        "Classic RCS detrending.",
                        rcs_res.rwi.reindex(columns=rwl.columns),
                        series_errors={c: "unknown pith (excluded from RCS)" for c in rcs_excluded} or None,
                    )
                    rcs_std = candidates["rcs"].chronology_std

            if _want_method("rcs_signal_free"):
                if use_dplr and "rcs_signal_free" in dplr_rwi_by:
                    _add_candidate(
                        "rcs_signal_free",
                        "Signal-free RCS (dplR-compatible; computed in R).",
                        dplr_rwi_by["rcs_signal_free"],
                        chron_std_override=dplr_chron_std_by.get("rcs_signal_free"),
                    )
                    rcs_sf_std = dplr_chron_std_by.get("rcs_signal_free")
                    rc_df = dplr_extras.get("rc_curve_rcs_signal_free")
                    if isinstance(rc_df, pd.DataFrame) and ("rc" in rc_df.columns):
                        ages_idx = (
                            rc_df["age"].to_numpy(dtype=int)
                            if "age" in rc_df.columns
                            else np.arange(1, len(rc_df) + 1, dtype=int)
                        )
                        rcs_sf_curve = pd.Series(rc_df["rc"].to_numpy(dtype=float), index=ages_idx)
                    else:
                        rcs_sf_curve = None
                elif cfg.strict_dplr and cfg.compat == "dplR":
                    # Skip: strict dplR mode forbids non-dplR fallbacks.
                    pass
                else:
                    rcs_sf_res = signal_free_rcs(
                        rwl_rcs,
                        iterations=6,
                        po=po,
                        difference=cfg.difference,
                        recode_zeros=cfg.recode_zeros,
                    )
                    _add_candidate(
                        "rcs_signal_free",
                        "Signal-free RCS (iterative).",
                        rcs_sf_res.rwi.reindex(columns=rwl.columns),
                        series_errors={c: "unknown pith (excluded from RCS)" for c in rcs_excluded} or None,
                    )
                    rcs_sf_std = candidates["rcs_signal_free"].chronology_std
                    rcs_sf_curve = rcs_sf_res.age_curve
                    dplr_method_errors.pop("rcs_signal_free", None)
        except Exception:
            rcs_std = None
            rcs_sf_std = None
            rcs_sf_curve = None

    if not candidates:
        raise RuntimeError(
            "No candidates could be produced. In strict dplR mode this usually means all dplR methods failed."
        )

    best_key = max(candidates.keys(), key=lambda k: candidates[k].score)
    advisor_result = AdvisorResult(
        goal=goal,
        rcs_feasibility=rcs_feas,
        disturbance_stats=disturbances,
        ages=ages,
        age_curve=age_curve,
        sample_depth=depth,
        candidates=candidates,
        best_key=best_key,
        rcs_std=rcs_std,
        rcs_sf_std=rcs_sf_std,
        rcs_sf_age_curve=rcs_sf_curve,
        pith_note=pith_note,
        rcs_excluded_series=list(rcs_excluded),
        compat_mode=cfg.compat,
        compat_config=cfg,
        eps_rbar_best=None,
        eps_rbar_cache={},
        dplr_method_errors=dplr_method_errors,
        dplr_eps_rbar_errors={},
    )
    setattr(advisor_result, "series_exclusion_logs", method_series_logs)
    setattr(advisor_result, "eps_series_exclusion_logs", eps_series_logs)
    valid_candidate_keys = set(str(k) for k in dict(candidates or {}).keys())
    setattr(
        advisor_result,
        "series_exclusion_logs",
        {
            str(k): v
            for k, v in dict(getattr(advisor_result, "series_exclusion_logs", {}) or {}).items()
            if str(k) in valid_candidate_keys
        },
    )
    setattr(
        advisor_result,
        "eps_series_exclusion_logs",
        {
            str(k): v
            for k, v in dict(getattr(advisor_result, "eps_series_exclusion_logs", {}) or {}).items()
            if str(k) in valid_candidate_keys
        },
    )
    setattr(advisor_result, "score_profile", dict(score_profile_local))
    setattr(advisor_result, "min_series_for_detrending", int(min_series_for_detrending))
    setattr(advisor_result, "min_series_count_mode", str(min_series_count_mode))
    setattr(advisor_result, "analysis_start_year", int(analysis_start_year))
    setattr(advisor_result, "analysis_matrix_kind", str(analysis_matrix_kind))
    setattr(advisor_result, "analysis_ids", ids.copy() if isinstance(ids, pd.DataFrame) else None)
    setattr(advisor_result, "analysis_input_series_count", int(analysis_input_series_count))
    setattr(advisor_result, "analysis_input_tree_count", int(analysis_input_tree_count))
    setattr(advisor_result, "analysis_output_count", int(analysis_output_count))
    setattr(advisor_result, "analysis_depth_original", original_depth.copy())
    setattr(advisor_result, "analysis_depth_filtered", depth.copy())
    setattr(advisor_result, "analysis_input_years_original", int(rwl_original.shape[0]))
    setattr(advisor_result, "analysis_input_years_filtered", int(rwl.shape[0]))
    if not rwl_original.empty:
        setattr(advisor_result, "full_period_start_year", int(rwl_original.index.min()))
        setattr(advisor_result, "full_period_end_year", int(rwl_original.index.max()))
    if not rwl.empty:
        setattr(advisor_result, "analysis_period_start_year", int(rwl.index.min()))
        setattr(advisor_result, "analysis_period_end_year", int(rwl.index.max()))
    _precompute_signal_diagnostics_for_result(
        advisor_result,
        window=50,
        verbose=False,
    )
    return advisor_result
    for method_key in list(candidates.keys()):
        try:
            _get_cached_signal_diagnostics_for_result(advisor_result, method_key, 50)
        except Exception as exc:
            advisor_result.dplr_eps_rbar_errors[method_key] = str(exc)
            if bool(getattr(cfg, "strict_dplr", False)):
                raise
    best_cached = advisor_result.eps_rbar_cache.get((best_key, 50))
    if best_cached is not None and not best_cached.empty:
        advisor_result.eps_rbar_best = best_cached.copy()
    return advisor_result


FAST_VALIDATION_METHOD_KEYS = [
    "exp",
    "linear",
    "runmean_32",
    "mean",
    "hugershoff",
    "lowess",
    "spline_30",
    "spline_60",
    "spline_120",
    "age_dep_spline",
    "ar",
    "rcs",
]

FAST_GUI_DPLR_METHOD_KEYS = [
    "exp",
    "linear",
    "runmean_32",
    "mean",
    "hugershoff",
    "lowess",
    "spline_30",
    "spline_60",
    "spline_120",
    "age_dep_spline",
    "ar",
    "rcs",
]

SSF_GUI_DPLR_METHOD_KEYS = [
    "ssf_spline_30",
    "ssf_spline_60",
    "ssf_spline_120",
    "ssf_age_dep_spline",
]


def _advisor_result_method_keys(advisor_result: Optional[AdvisorResult]) -> set[str]:
    """Return the method keys already present in one advisor result."""
    if advisor_result is None:
        return set()
    try:
        return {str(k) for k in dict(getattr(advisor_result, "candidates", {}) or {}).keys()}
    except Exception:
        return set()


def _missing_method_keys_for_result(
    advisor_result: Optional[AdvisorResult],
    requested_keys: Optional[set[str]],
) -> Optional[set[str]]:
    """Return the subset of requested methods that is still missing."""
    if requested_keys is None:
        return None
    have = _advisor_result_method_keys(advisor_result)
    return {str(k) for k in requested_keys if str(k) not in have}


def _merge_advisor_results(
    base_result: Optional[AdvisorResult],
    extra_result: Optional[AdvisorResult],
) -> Optional[AdvisorResult]:
    """Merge newly computed methods into an existing advisor result."""
    if base_result is None:
        return extra_result
    if extra_result is None:
        return base_result


def _is_no_candidates_runtime_error(exc: BaseException) -> bool:
    """Return True when a partial advisor run produced no valid new candidates."""
    msg = str(exc or "").strip().lower()
    return "no candidates could be produced" in msg


def _sorted_method_key_list(method_keys: Optional[set[str] | list[str] | tuple[str, ...]]) -> Optional[list[str]]:
    """Return a stable sorted method-key list or None."""
    if method_keys is None:
        return None
    out = sorted({str(k).strip() for k in method_keys if str(k).strip()})
    return out

    merged_candidates = dict(getattr(base_result, "candidates", {}) or {})
    merged_candidates.update(dict(getattr(extra_result, "candidates", {}) or {}))
    base_result.candidates = merged_candidates

    merged_eps_cache = dict(getattr(base_result, "eps_rbar_cache", {}) or {})
    merged_eps_cache.update(dict(getattr(extra_result, "eps_rbar_cache", {}) or {}))
    base_result.eps_rbar_cache = merged_eps_cache

    merged_method_errors = dict(getattr(base_result, "dplr_method_errors", {}) or {})
    merged_method_errors.update(dict(getattr(extra_result, "dplr_method_errors", {}) or {}))
    base_result.dplr_method_errors = merged_method_errors

    merged_eps_errors = dict(getattr(base_result, "dplr_eps_rbar_errors", {}) or {})
    merged_eps_errors.update(dict(getattr(extra_result, "dplr_eps_rbar_errors", {}) or {}))
    base_result.dplr_eps_rbar_errors = merged_eps_errors

    merged_series_logs = dict(getattr(base_result, "series_exclusion_logs", {}) or {})
    merged_series_logs.update(dict(getattr(extra_result, "series_exclusion_logs", {}) or {}))
    merged_eps_series_logs = dict(getattr(base_result, "eps_series_exclusion_logs", {}) or {})
    merged_eps_series_logs.update(dict(getattr(extra_result, "eps_series_exclusion_logs", {}) or {}))

    failed_now = set(str(k) for k in dict(getattr(extra_result, "dplr_method_errors", {}) or {}).keys())
    for method_key in list(failed_now):
        merged_series_logs.pop(method_key, None)
        merged_eps_series_logs.pop(method_key, None)

    valid_keys = set(str(k) for k in dict(getattr(base_result, "candidates", {}) or {}).keys())
    setattr(
        base_result,
        "series_exclusion_logs",
        {str(k): v for k, v in merged_series_logs.items() if str(k) in valid_keys},
    )
    setattr(
        base_result,
        "eps_series_exclusion_logs",
        {str(k): v for k, v in merged_eps_series_logs.items() if str(k) in valid_keys},
    )

    if merged_candidates:
        best_key = max(
            merged_candidates.keys(),
            key=lambda k: float(getattr(merged_candidates[k], "score", float("-inf"))),
        )
        base_result.best_key = str(best_key)
        best_cached = base_result.eps_rbar_cache.get((str(best_key), 50))
        if best_cached is not None and not best_cached.empty:
            base_result.eps_rbar_best = best_cached.copy()

    for attr_name in (
        "sample_depth",
        "rcs_std",
        "rcs_sf_std",
        "rcs_sf_age_curve",
        "compat_mode",
        "compat_config",
        "analysis_start_year",
        "analysis_matrix_kind",
        "analysis_ids",
        "analysis_input_series_count",
        "analysis_input_tree_count",
        "analysis_output_count",
        "analysis_depth_original",
        "analysis_depth_filtered",
        "analysis_input_years_original",
        "analysis_input_years_filtered",
        "full_period_start_year",
        "full_period_end_year",
        "analysis_period_start_year",
        "analysis_period_end_year",
        "score_profile",
        "min_series_for_detrending",
        "min_series_count_mode",
    ):
        if hasattr(extra_result, attr_name):
            try:
                setattr(base_result, attr_name, getattr(extra_result, attr_name))
            except Exception:
                pass

    return base_result


def _powt_preview_cache_key_for_dataset(
    dataset_key: str,
    method: str,
    rescale: bool,
) -> tuple[str, str, bool]:
    """Build a stable cache key for one dataset POWT preview."""
    return (str(dataset_key), str(method).strip().lower(), bool(rescale))


def _advisor_result_method_keys(advisor_result: Optional[AdvisorResult]) -> set[str]:
    """Return the set of method keys already present in one advisor result."""
    if advisor_result is None:
        return set()
    try:
        return {str(k) for k in dict(getattr(advisor_result, "candidates", {}) or {}).keys()}
    except Exception:
        return set()


def _missing_method_keys_for_result(
    advisor_result: Optional[AdvisorResult],
    requested_keys: Optional[set[str]],
) -> Optional[set[str]]:
    """Return the subset of requested methods that is still missing in one result."""
    if requested_keys is None:
        return None
    have = _advisor_result_method_keys(advisor_result)
    return {str(k) for k in requested_keys if str(k) not in have}


def _merge_advisor_results(
    base_result: Optional[AdvisorResult],
    extra_result: Optional[AdvisorResult],
) -> Optional[AdvisorResult]:
    """Merge newly computed methods into an existing advisor result."""
    if base_result is None:
        return extra_result
    if extra_result is None:
        return base_result

    merged_candidates = dict(getattr(base_result, "candidates", {}) or {})
    merged_candidates.update(dict(getattr(extra_result, "candidates", {}) or {}))
    base_result.candidates = merged_candidates

    merged_eps_cache = dict(getattr(base_result, "eps_rbar_cache", {}) or {})
    merged_eps_cache.update(dict(getattr(extra_result, "eps_rbar_cache", {}) or {}))
    base_result.eps_rbar_cache = merged_eps_cache

    merged_method_errors = dict(getattr(base_result, "dplr_method_errors", {}) or {})
    merged_method_errors.update(dict(getattr(extra_result, "dplr_method_errors", {}) or {}))
    base_result.dplr_method_errors = merged_method_errors

    merged_eps_errors = dict(getattr(base_result, "dplr_eps_rbar_errors", {}) or {})
    merged_eps_errors.update(dict(getattr(extra_result, "dplr_eps_rbar_errors", {}) or {}))
    base_result.dplr_eps_rbar_errors = merged_eps_errors

    base_series_logs = dict(getattr(base_result, "series_exclusion_logs", {}) or {})
    base_series_logs.update(dict(getattr(extra_result, "series_exclusion_logs", {}) or {}))

    base_eps_series_logs = dict(getattr(base_result, "eps_series_exclusion_logs", {}) or {})
    base_eps_series_logs.update(dict(getattr(extra_result, "eps_series_exclusion_logs", {}) or {}))

    failed_now = set(str(k) for k in dict(getattr(extra_result, "dplr_method_errors", {}) or {}).keys())
    for method_key in list(failed_now):
        base_series_logs.pop(method_key, None)
        base_eps_series_logs.pop(method_key, None)

    valid_keys = set(str(k) for k in dict(merged_candidates or {}).keys())
    setattr(
        base_result,
        "series_exclusion_logs",
        {str(k): v for k, v in base_series_logs.items() if str(k) in valid_keys},
    )
    setattr(
        base_result,
        "eps_series_exclusion_logs",
        {str(k): v for k, v in base_eps_series_logs.items() if str(k) in valid_keys},
    )

    if merged_candidates:
        best_key = max(
            merged_candidates.keys(),
            key=lambda k: float(getattr(merged_candidates[k], "score", float("-inf"))),
        )
        base_result.best_key = str(best_key)
        best_cached = base_result.eps_rbar_cache.get((str(best_key), 50))
        if best_cached is not None and not best_cached.empty:
            base_result.eps_rbar_best = best_cached.copy()

    for attr_name in (
        "sample_depth",
        "rcs_std",
        "rcs_sf_std",
        "rcs_sf_age_curve",
        "compat_mode",
        "compat_config",
        "analysis_start_year",
        "analysis_matrix_kind",
        "analysis_ids",
        "analysis_input_series_count",
        "analysis_input_tree_count",
        "analysis_output_count",
        "analysis_depth_original",
        "analysis_depth_filtered",
        "analysis_input_years_original",
        "analysis_input_years_filtered",
        "full_period_start_year",
        "full_period_end_year",
        "analysis_period_start_year",
        "analysis_period_end_year",
        "score_profile",
        "min_series_for_detrending",
        "min_series_count_mode",
    ):
        if hasattr(extra_result, attr_name):
            try:
                setattr(base_result, attr_name, getattr(extra_result, attr_name))
            except Exception:
                pass

    return base_result


def _resolve_validation_method_keys(
    methods_arg: Optional[str],
    fast: bool = False,
) -> list[str]:
    """Resolve validation method keys from CLI options."""
    if methods_arg is not None and str(methods_arg).strip():
        raw = [part.strip() for part in str(methods_arg).split(",")]
        keys = [part for part in raw if part]
        if len(keys) == 1 and keys[0].lower() == "all":
            return list(VALIDATION_METHOD_KEYS)
    elif fast:
        keys = list(FAST_VALIDATION_METHOD_KEYS)
    else:
        return list(VALIDATION_METHOD_KEYS)

    unknown = [k for k in keys if k not in VALIDATION_METHOD_KEYS and _parameterized_method(k) is None]
    if unknown:
        raise ValueError(
            "Unknown validation method(s): "
            + ", ".join(sorted(unknown))
            + ". Allowed keys: "
            + ", ".join(VALIDATION_METHOD_KEYS)
        )
    return keys
def cli_run_and_export_best(
    input_path: str,
    goal: str,
    output_path: str,
    climate_path: str | None = None,
    export_all_methods: bool = False,
    compat_config: Optional[DplrCompatConfig] = None,
    po_path: str | None = None,
) -> None:
    """Batch-mode: run advisor and export best chronology for bridge."""
    rwl = load_rwl_any(input_path)
    climate = load_climate_csv(climate_path) if climate_path else None
    po_df: Optional[pd. DataFrame] = None
    if po_path:
        try:
            po_df = pd.read_csv(po_path)
        except Exception:
            po_df = None
    if verbose:
        _print_progress("validate: computing Python-side outputs")
    py_result = run_advisor_core(
        rwl,
        goal="mixed",
        climate=None,
        compat_config=py_cfg,
        po=po_df,
        method_keys=method_keys2,
    )

    best = result.candidates[result.best_key]
    df = pd. DataFrame({"Year": best.chronology_std.index.astype(int)})
    if export_all_methods:
        for key, cand in result.candidates.items():
            df[f"{key}_STD"] = cand.chronology_std.values
            df[f"{key}_RES"] = cand.chronology_res.values
    else:
        df["best_STD"] = best.chronology_std.values
        df["best_RES"] = best.chronology_res.values
    ensure_dir(output_path)
    df.to_csv(output_path, index=False)


# ---------------------------------------------------------------------------
# dplR validation harness (unified-diff friendly outputs)
# ---------------------------------------------------------------------------
VALIDATION_METHOD_KEYS = [
    "exp",
    "linear",
    "runmean_32",
    "mean",
    "hugershoff",
    "lowess",
    "spline_30",
    "spline_60",
    "spline_120",
    "age_dep_spline",
    "ar",
    "rcs",
    "rcs_signal_free",
    "ssf_spline_30",
    "ssf_spline_60",
    "ssf_spline_120",
    "ssf_age_dep_spline",
]


def _corr_and_maxabs(a: np. ndarray, b: np. ndarray) -> tuple[float, float]:
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 3:
        return float("nan"), float("nan")
    aa = a[mask]
    bb = b[mask]
    if float(np.nanstd(aa)) == 0.0 or float(np.nanstd(bb)) == 0.0:
        # Correlation undefined when one side is constant; fall back to maxabs only.
        return float("nan"), float(np.max(np.abs(aa - bb)))
    corr = float(np.corrcoef(aa, bb)[0, 1])
    maxabs = float(np.max(np.abs(aa - bb)))
    return corr, maxabs

def _compare_rwi_matrices(py: pd. DataFrame, ref: pd. DataFrame) -> pd. DataFrame:
    cols = sorted(set(py.columns).intersection(set(ref.columns)))
    rows: list[dict[str, Any]] = []
    for c in cols:
        a = py[c].reindex(ref.index).to_numpy(dtype=float)
        b = ref[c].to_numpy(dtype=float)
        corr, maxabs = _corr_and_maxabs(a, b)
        rows.append({"series": c, "corr": corr, "max_abs": maxabs})
    return pd. DataFrame(rows)

def _compare_chron(py: pd. Series, ref: pd. Series) -> tuple[float, float, pd. DataFrame]:
    aligned = pd.concat([py.rename("py"), ref.rename("dplR")], axis=1, join="inner").dropna()
    a = aligned["py"].to_numpy(dtype=float)
    b = aligned["dplR"].to_numpy(dtype=float)
    corr, maxabs = _corr_and_maxabs(a, b)
    diff = aligned.copy()
    diff["diff"] = diff["py"] - diff["dplR"]
    return corr, maxabs, diff

def cli_validate_against_dplr(
    input_path: str,
    out_dir: str,
    po_path: str | None = None,
    python_compat: CompatMode = "dplR",
    difference: bool = False,
    pos_slope: bool = False,
    constrain_nls: ConstrainNLS = "never",
    recode_zeros: bool = True,
    rscript: str = "Rscript",
    auto_install_r: bool = False,
    auto_install_r_deps: bool = False,
    cran_repo: str = "https://cloud.r-project.org",
    method_keys: Optional[list[str]] = None,
    verbose: bool = False,
) -> None:
    """Validate Python outputs against dplR reference for dplR-equivalent methods.

    This function writes:
      - validate_dplr.R (re-usable R harness),
      - dplR outputs (CSV),
      - Python outputs (CSV),
      - per-method diffs + a Markdown summary report.
    method_keys2 = list(method_keys) if method_keys else list(VALIDATION_METHOD_KEYS)
    if verbose:
        _print_progress(f"validate: input={input_path}")
        _print_progress(f"validate: methods={','.join(method_keys2)}")
    if verbose:
        _print_progress("validate: loading input")
    """
    os.makedirs(out_dir, exist_ok=True)
    method_keys2 = list(method_keys) if method_keys else list(VALIDATION_METHOD_KEYS)
    if verbose:
        _print_progress(f"validate: input={input_path}")
        _print_progress(f"validate: methods={','.join(method_keys2)}")

    # Save the R harness alongside outputs so it can be called as requested:
    #   Rscript validate_dplr.R input.rwl po.csv out_dir
    harness_path = os.path.join(out_dir, "validate_dplr.R")
    _write_validate_dplr_r(harness_path)

    if verbose:
        _print_progress("validate: loading input")
    rwl = load_rwl_any(input_path)
    rwl = _ensure_unique_sorted_year_index(_apply_arstan_missing_codes(rwl))

    po_df: Optional[pd. DataFrame] = None
    if po_path:
        try:
            po_df = pd.read_csv(po_path)
        except Exception:
            po_df = None

    # Reference (always dplR)
    ref_cfg = DplrCompatConfig(
        compat="dplR",
        recode_zeros=recode_zeros,
        difference=difference,
        pos_slope=pos_slope,
        constrain_nls=constrain_nls,
        rscript=rscript,
        auto_install_r=auto_install_r,
        auto_install_r_deps=auto_install_r_deps,
        cran_repo=cran_repo,
    )
    method_keys = list(VALIDATION_METHOD_KEYS)
    ref_rwi_by, ref_chron_by, ref_extras = _dplr_compute_methods(rwl, method_keys, ref_cfg, po=po_df)

    # Python under test (legacy or dplR)
    py_cfg = DplrCompatConfig(
        compat=python_compat,
        recode_zeros=recode_zeros,
        difference=difference,
        pos_slope=pos_slope,
        constrain_nls=constrain_nls,
        rscript=rscript,
        auto_install_r=auto_install_r,
        auto_install_r_deps=auto_install_r_deps,
        cran_repo=cran_repo,
    )
    if verbose:
        _print_progress("validate: computing Python-side outputs")
    py_result = run_advisor_core(
        rwl,
        goal="mixed",
        climate=None,
        compat_config=py_cfg,
        po=po_df,
        method_keys=method_keys2,
    )

    # Write outputs + report
    report_lines: list[str] = []
    report_lines.append("# dplR validation report")
    report_lines.append("")
    report_lines.append(f"- Generated on: {_dt.datetime.now().isoformat(timespec='seconds')}")
    report_lines.append(f"- Input: `{os.path.basename(input_path)}`")
    report_lines.append(f"- Python compat: `{python_compat}`")
    report_lines.append(f"- dplR version: `{ref_extras.get('dplr_version', 'unknown')}`")
    report_lines.append(f"- difference: `{difference}`; pos_slope: `{pos_slope}`; constrain_nls: `{constrain_nls}`; recode_zeros: `{recode_zeros}`")
    report_lines.append("")

    summary_rows: list[dict[str, Any]] = []
    all_rwi_stats: list[pd. DataFrame] = []
    for k in method_keys:
        if k not in ref_rwi_by or k not in ref_chron_by:
            continue
        if k not in py_result.candidates:
            # Not produced in legacy mode (or failed) – skip but record.
            summary_rows.append({"method": k, "status": "missing_in_python", "chron_corr": float("nan"), "chron_max_abs": float("nan")})
            continue

        py_cand = py_result.candidates[k]
        ref_rwi = ref_rwi_by[k]
        ref_std = ref_chron_by[k]

        # Save raw outputs
        py_rwi_path = os.path.join(out_dir, f"py_rwi_{k}.csv")
        py_std_path = os.path.join(out_dir, f"py_chron_std_{k}.csv")
        ref_rwi_path = os.path.join(out_dir, f"dplr_rwi_{k}.csv")
        ref_std_path = os.path.join(out_dir, f"dplr_chron_std_{k}.csv")
        py_cand.rwi.to_csv(py_rwi_path)
        py_cand.chronology_std.rename("std").to_csv(py_std_path, header=True)
        ref_rwi.to_csv(ref_rwi_path)
        ref_std.rename("std").to_csv(ref_std_path, header=True)

        # Compare chronology
        chron_corr, chron_maxabs, chron_diff = _compare_chron(py_cand.chronology_std, ref_std)
        chron_diff_path = os.path.join(out_dir, f"diff_chron_std_{k}.csv")
        chron_diff.to_csv(chron_diff_path)

        # Compare RWI per-series
        rwi_stats = _compare_rwi_matrices(py_cand.rwi, ref_rwi)
        rwi_stats_path = os.path.join(out_dir, f"diff_rwi_stats_{k}.csv")
        rwi_stats.to_csv(rwi_stats_path, index=False)


        # Also accumulate per-series correlation/diff stats into one combined file.
        if rwi_stats is not None and not rwi_stats.empty:
            rwi_stats_all = rwi_stats.copy()
            rwi_stats_all.insert(0, "method", k)
            all_rwi_stats.append(rwi_stats_all)

        # Summaries
        min_corr = float(np.nanmin(rwi_stats["corr"].to_numpy(dtype=float))) if not rwi_stats.empty else float("nan")
        max_abs = float(np.nanmax(rwi_stats["max_abs"].to_numpy(dtype=float))) if not rwi_stats.empty else float("nan")
        summary_rows.append({
            "method": k,
            "status": "ok",
            "chron_corr": chron_corr,
            "chron_max_abs": chron_maxabs,
            "min_series_corr": min_corr,
            "max_series_abs": max_abs,
        })
    if all_rwi_stats:
        pd. concat(all_rwi_stats, ignore_index=True).to_csv(
            os.path.join(out_dir, "validation_correlations.csv"), index=False
        )

    summary_df = pd. DataFrame(summary_rows)
    summary_df.to_csv(os.path.join(out_dir, "validation_summary.csv"), index=False)

    report_lines.append("## Summary")
    report_lines.append("")
    if not summary_df.empty:
        for _, row in summary_df.iterrows():
            report_lines.append(
                f"- `{row['method']}`: {row['status']}; "
                f"chron corr={row.get('chron_corr', float('nan')):.6g}, "
                f"chron max_abs={row.get('chron_max_abs', float('nan')):.6g}, "
                f"min series corr={row.get('min_series_corr', float('nan')):.6g}, "
                f"max series abs={row.get('max_series_abs', float('nan')):.6g}"
            )
    else:
        report_lines.append("- No comparable methods were produced.")
    report_lines.append("")
    Path(os.path.join(out_dir, "validation_report.md")).write_text("\n".join(report_lines), encoding="utf-8")
    if verbose:
        _print_progress(f"validate: done, outputs in {out_dir}")
    if verbose:
        _print_progress(f"validate: done, outputs in {out_dir}")


def cli_validate_eps_rbar_against_dplr(
    input_path: str,
    out_dir: str,
    po_path: str | None = None,
    python_compat: CompatMode = "dplR",
    difference: bool = False,
    pos_slope: bool = False,
    constrain_nls: ConstrainNLS = "never",
    recode_zeros: bool = True,
    rscript: str = "Rscript",
    auto_install_r: bool = False,
    auto_install_r_deps: bool = False,
    cran_repo: str = "https://cloud.r-project.org",
    method_keys: Optional[list[str]] = None,
    verbose: bool = False,
) -> None:
    """Validate Python EPS/Rbar outputs against dplR reference statistics."""
    os.makedirs(out_dir, exist_ok=True)
    method_keys2 = list(method_keys) if method_keys else list(VALIDATION_METHOD_KEYS)
    if verbose:
        _print_progress(f"validate-eps-rbar: input={input_path}")
        _print_progress(f"validate-eps-rbar: methods={','.join(method_keys2)}")

    harness_path = os.path.join(out_dir, "validate_eps_rbar_dplr.R")
    _write_validate_eps_rbar_dplr_r(harness_path)

    if verbose:
        _print_progress("validate-eps-rbar: loading input")
    rwl = load_rwl_any(input_path)
    rwl = _ensure_unique_sorted_year_index(_apply_arstan_missing_codes(rwl))

    po_df: Optional[pd.DataFrame] = None
    if po_path:
        try:
            po_df = pd.read_csv(po_path)
        except Exception:
            po_df = None

    ref_cfg = DplrCompatConfig(
        compat="dplR",
        recode_zeros=recode_zeros,
        difference=difference,
        pos_slope=pos_slope,
        constrain_nls=constrain_nls,
        rscript=rscript,
        auto_install_r=auto_install_r,
        auto_install_r_deps=auto_install_r_deps,
        cran_repo=cran_repo,
    )
    if verbose:
        _print_progress("validate-eps-rbar: computing dplR detrending reference")
    ref_rwi_by, _ref_chron_by, ref_extras = _dplr_compute_methods(
        rwl,
        method_keys2,
        ref_cfg,
        po=po_df,
        verbose=verbose,
        progress_label="validate-eps-rbar/detrending",
    )

    py_cfg = DplrCompatConfig(
        compat=python_compat,
        recode_zeros=recode_zeros,
        difference=difference,
        pos_slope=pos_slope,
        constrain_nls=constrain_nls,
        rscript=rscript,
        auto_install_r=auto_install_r,
        auto_install_r_deps=auto_install_r_deps,
        cran_repo=cran_repo,
    )
    if verbose:
        _print_progress("validate-eps-rbar: computing Python-side detrending outputs")
    py_result = run_advisor_core(
        rwl,
        goal="mixed",
        climate=None,
        compat_config=py_cfg,
        po=po_df,
        method_keys=method_keys2,
    )
    if isinstance(py_result, AdvisorResult):
        py_candidates = dict(py_result.candidates)
    elif hasattr(py_result, "candidates") and isinstance(getattr(py_result, "candidates"), dict):
        py_candidates = dict(getattr(py_result, "candidates"))
    else:
        py_candidates = {}
        if verbose:
            _print_progress(
                "validate-eps-rbar: Python-side detrending returned no AdvisorResult; "
                "continuing with missing_in_python statuses"
            )

    if verbose:
        _print_progress("validate-eps-rbar: computing dplR EPS/RBAR reference")
    ref_stats_by, ref_running_by, eps_extras = _dplr_compute_eps_rbar_from_rwi(
        ref_rwi_by,
        ref_cfg,
        window=50,
        verbose=verbose,
        progress_label="validate-eps-rbar/eps-rbar",
    )
    dplr_eps_errors = dict(eps_extras.get("dplr_eps_rbar_errors", {}) or {})

    report_lines: list[str] = []
    report_lines.append("# dplR EPS/RBAR validation report")
    report_lines.append("")
    report_lines.append(f"- Generated on: {_dt.datetime.now().isoformat(timespec='seconds')}")
    report_lines.append(f"- Input: `{os.path.basename(input_path)}`")
    report_lines.append(f"- Python compat: `{python_compat}`")
    report_lines.append(
        f"- dplR version: `{eps_extras.get('dplr_version', ref_extras.get('dplr_version', 'unknown'))}`"
    )
    report_lines.append(
        f"- difference: `{difference}`; pos_slope: `{pos_slope}`; "
        f"constrain_nls: `{constrain_nls}`; recode_zeros: `{recode_zeros}`"
    )
    report_lines.append("")

    summary_rows: list[dict[str, Any]] = []
    for k in method_keys2:
        if verbose:
            _print_progress(f"validate-eps-rbar: comparing method {k}")
        if k not in ref_rwi_by:
            summary_rows.append(
                {
                    "method": k,
                    "status": "missing_in_dplr_rwi",
                    "full_max_abs": float("nan"),
                    "running_max_abs": float("nan"),
                    "full_compared_cells": 0,
                    "running_compared_cells": 0,
                    "running_rows_py": 0,
                    "running_rows_dplr": 0,
                    "running_rows_aligned": 0,
                }
            )
            continue
        if k not in py_candidates:
            summary_rows.append(
                {
                    "method": k,
                    "status": "missing_in_python",
                    "full_max_abs": float("nan"),
                    "running_max_abs": float("nan"),
                    "full_compared_cells": 0,
                    "running_compared_cells": 0,
                    "running_rows_py": 0,
                    "running_rows_dplr": 0,
                    "running_rows_aligned": 0,
                }
            )
            continue
        if k in dplr_eps_errors:
            summary_rows.append(
                {
                    "method": k,
                    "status": "dplr_eps_rbar_error",
                    "detail": str(dplr_eps_errors.get(k, "")),
                    "full_max_abs": float("nan"),
                    "running_max_abs": float("nan"),
                    "full_compared_cells": 0,
                    "running_compared_cells": 0,
                    "running_rows_py": 0,
                    "running_rows_dplr": 0,
                    "running_rows_aligned": 0,
                }
            )
            continue

        py_cand = py_candidates[k]
        ref_rwi = ref_rwi_by[k]

        py_rwi_path = os.path.join(out_dir, f"py_rwi_{k}.csv")
        ref_rwi_path = os.path.join(out_dir, f"dplr_rwi_{k}.csv")
        py_cand.rwi.to_csv(py_rwi_path)
        ref_rwi.to_csv(ref_rwi_path)

        py_stats = compute_eps_rbar_stats_full_period(py_cand.rwi)
        py_running = sanitize_eps_rbar_depth_df(compute_eps_rbar_depth_timeseries(py_cand.rwi, window=50))
        ref_stats = _normalize_eps_compare_df(ref_stats_by.get(k), key_cols=[], value_cols=EPS_RBAR_FULL_COLUMNS)
        ref_running = _prepare_running_eps_rbar_df(ref_running_by.get(k))

        diff_stats = _compare_eps_tables(py_stats, ref_stats, key_cols=[], value_cols=EPS_RBAR_FULL_COLUMNS)
        diff_running = _compare_eps_tables(
            py_running,
            ref_running,
            key_cols=EPS_RBAR_RUNNING_KEY_COLUMNS,
            value_cols=EPS_RBAR_RUNNING_COLUMNS,
        )

        py_stats.to_csv(os.path.join(out_dir, f"py_eps_rbar_stats_{k}.csv"), index=False)
        ref_stats.to_csv(os.path.join(out_dir, f"dplr_eps_rbar_stats_{k}.csv"), index=False)
        diff_stats.to_csv(os.path.join(out_dir, f"diff_eps_rbar_stats_{k}.csv"), index=False)
        py_running.to_csv(os.path.join(out_dir, f"py_eps_rbar_running_{k}.csv"), index=False)
        ref_running.to_csv(os.path.join(out_dir, f"dplr_eps_rbar_running_{k}.csv"), index=False)
        diff_running.to_csv(os.path.join(out_dir, f"diff_eps_rbar_running_{k}.csv"), index=False)

        running_rows_aligned = 0
        if not diff_running.empty and all(col in diff_running.columns for col in EPS_RBAR_RUNNING_KEY_COLUMNS):
            aligned_mask = (
                diff_running["start.year"].notna()
                & diff_running["mid.year"].notna()
                & diff_running["end.year"].notna()
            )
            running_rows_aligned = int(aligned_mask.sum())

        summary_rows.append(
            {
                "method": k,
                "status": "ok",
                "detail": "",
                "full_max_abs": _max_abs_from_diff_table(diff_stats),
                "running_max_abs": _max_abs_from_diff_table(diff_running),
                "full_compared_cells": _count_compared_cells_from_diff_table(diff_stats),
                "running_compared_cells": _count_compared_cells_from_diff_table(diff_running),
                "running_rows_py": int(py_running.shape[0]),
                "running_rows_dplr": int(ref_running.shape[0]),
                "running_rows_aligned": int(running_rows_aligned),
            }
        )

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(os.path.join(out_dir, "validation_eps_rbar_summary.csv"), index=False)

    report_lines.append("## Summary")
    report_lines.append("")
    if not summary_df.empty:
        for _, row in summary_df.iterrows():
            detail = str(row.get("detail", "") or "").strip()
            report_lines.append(
                f"- `{row['method']}`: {row['status']}; "
                f"full max_abs={row.get('full_max_abs', float('nan')):.6g}, "
                f"running max_abs={row.get('running_max_abs', float('nan')):.6g}, "
                f"full compared cells={int(row.get('full_compared_cells', 0))}, "
                f"running compared cells={int(row.get('running_compared_cells', 0))}, "
                f"running rows py/dplR/aligned="
                f"{int(row.get('running_rows_py', 0))}/"
                f"{int(row.get('running_rows_dplr', 0))}/"
                f"{int(row.get('running_rows_aligned', 0))}"
            )
            if detail:
                report_lines.append(f"  - detail: {detail}")
    else:
        report_lines.append("- No comparable methods were produced.")
    report_lines.append("")
    Path(os.path.join(out_dir, "validation_eps_rbar_report.md")).write_text(
        "\n".join(report_lines),
        encoding="utf-8",
    )
    if verbose:
        _print_progress(f"validate-eps-rbar: done, outputs in {out_dir}")

# ---------------------------------------------------------------------------
# Report (for export)
# ---------------------------------------------------------------------------
def generate_markdown_report(
    advisor: AdvisorResult,
    dataset_name: str,
    climate_present: bool,
    input_filename: str,
) -> str:
    """Generate markdown report text from advisor result.""" 
    best = advisor.candidates[advisor.best_key]
    goal = advisor.goal
    climate_info = "provided" if climate_present else "not provided"
    lines: list[str] = []
    lines.append("# Detrending advisor report")
    lines.append("")
    lines.append(f"- Generated on: {_dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Dataset: `{dataset_name}` (file: `{input_filename}`)")
    lines.append(f"- Goal: `{goal}`")
    lines.append(f"- Climate series: {climate_info}")
    lines.append("")
    lines.append("## 1. Age and sample structure")
    lines.append("")
    lines.append(f"- Number of series: **{advisor.ages.shape[0]}**")
    lines.append(f"- Min age: **{int(advisor.ages.min())}** years")
    lines.append(f"- Max age: **{int(advisor.ages.max())}** years")
    lines.append(f"- Median age: **{float(advisor.ages.median()):.1f}** years")
    lines.append("")
    lines.append("## 2. Disturbances")
    lines.append("")
    lines.append(
        f"- Strong release/suppression events (total): "
        f"**{advisor.disturbance_stats.n_events}**"
    )
    if not math.isnan(advisor.disturbance_stats.fraction_series_with_events):
        lines.append(
            f"- Fraction of series with events: "
            f"**{advisor.disturbance_stats.fraction_series_with_events:.2f}**"
        )
    lines.append("")
    lines.append("## 3. RCS feasibility")
    lines.append("")
    lines.append(f"- RCS feasible: **{advisor.rcs_feasibility.possible}**")
    lines.append(f"- Reason: {advisor.rcs_feasibility.reason}")
    lines.append("")
    lines.append("## 4. Candidate methods")
    lines.append("")
    lines.append("| Key | Description | Score | EPS | Rbar | Var(high) | Var(low) | Climate |")
    lines.append("|-----|-------------|-------|-----|------|-----------|----------|---------|")
    for key, cand in advisor.candidates.items():
        m = cand.metrics
        corr_str = (
            f"{m.climate_corr:.2f}" if m.climate_corr is not None else "NA"
        )
        lines.append(
            f"| `{key}` | {cand.description} | {cand.score:.3f} | "
            f"{m.eps:.3f} | {m.rbar:.3f} | "
            f"{m.var_highfreq:.3f} | {m.var_lowfreq:.3f} | {corr_str} |"
        )
    lines.append("")
    lines.append("## 5. Recommended method")
    lines.append("")
    lines.append(f"- Key: `{advisor.best_key}`")
    lines.append(f"- Description: {best.description}")
    lines.append(f"- Score: **{best.score:.3f}**")
    lines.append(f"- EPS: **{best.metrics.eps:.3f}**")
    lines.append(f"- Rbar: **{best.metrics.rbar:.3f}**")
    if best.metrics.climate_corr is not None:
        lines.append(
            f"- Climate correlation: **{best.metrics.climate_corr:.3f}**"
        )
    lines.append("")
    lines.append("## 6. Chronologies (exported separately)")
    lines.append("")
    lines.append("- STD / RES for recommended method.")
    if advisor.rcs_std is not None:
        lines.append("- RCS STD chronology.")
    if advisor.rcs_sf_std is not None:
        lines.append("- Signal-free RCS STD chronology.")
    lines.append("")
    lines.append("## 7. Interpretation notes")
    lines.append("")
    if goal == "highfreq":
        lines.append(
            "- Focus on inter-annual variability: methods preserving "
            "high-frequency variance and good EPS were prioritized."
        )
    elif goal == "lowfreq":
        lines.append(
            "- Focus on multi-decadal/centennial trends: methods preserving "
            "low-frequency variance and, where feasible, RCS/signal-free RCS "
            "were prioritized."
        )
    elif goal == "temp_recon":
        lines.append(
            "- Temperature reconstruction goal: prioritize multi-decadal "
            "structure and strong climate correlation while keeping interannual "
            "signal."
        )
    elif goal == "runoff_recon":
        lines.append(
            "- Runoff reconstruction goal: emphasize interannual and decadal "
            "variance with a strong climate relationship."
        )
    elif goal == "drought_recon":
        lines.append(
            "- Drought reconstruction goal: balance high- and low-frequency "
            "variance with emphasis on climate coherence."
        )
    elif goal == "variance_check":
        lines.append(
            "- Variance / QC goal: prioritize balanced high- and low-frequency "
            "variance together with stable EPS."
        )
    else:
        lines.append(
            "- Mixed goal: balance between high- and low-frequency variance "
            "and overall signal (EPS, climate correlation) was considered."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Report generated by a Python detrending advisor GUI.")
    lines.append("")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Multi-dataset state
# ---------------------------------------------------------------------------
@dataclass
class DatasetState:
    """Container for one imported dataset and its analysis results."""
    key: str
    path: str
    rwl: pd.DataFrame
    ids: Optional[pd.DataFrame] = None
    ids_reviewed: bool = False
    ids_source: str = "auto"
    pith: Optional[pd.DataFrame] = None   # per-series pith info (dendro_pith table)
    advisor_result: Optional[AdvisorResult] = None
    compat_signature: Optional[str] = None
    advisor_result_no_powt: Optional[AdvisorResult] = None
    advisor_result_with_powt: Optional[AdvisorResult] = None
    advisor_result_with_powt_universal: Optional[AdvisorResult] = None
    advisor_result_with_powt_cook: Optional[AdvisorResult] = None
    powt_preview_for_run: Optional[PowtPreviewResult] = None
    powt_preview_for_run_universal: Optional[PowtPreviewResult] = None
    powt_preview_for_run_cook: Optional[PowtPreviewResult] = None
    powt_best_method: str = ""
    powt_recommendation_text: str = ""
    powt_recommendation_flag: str = "unknown"
    displayed_result_mode: str = "recommended"

# ---------------------------------------------------------------------------
# GUI application
# ---------------------------------------------------------------------------
class DetrendAdvisorApp(tk. Tk):
    """Main GUI application for detrending advisor."""
    def __init__(self) -> None:
        _set_windows_app_user_model_id("Bakhtiyorov.DetrendAdvisor")
        super().__init__()
        self._fix_windows_dpi()  # sharp, readable fonts on high-DPI Windows screens
        self.title("Detrending advisor (TRW / BI)")
        self._apply_app_icon()
        self.geometry("1200x800")
        self.datasets: Dict[str, DatasetState] = {}
        self.active_dataset_key: Optional[str] = None

        self.rwl: Optional[pd. DataFrame] = None
        self.rwl_path: Optional[str] = None
        self.ids: Optional[pd.DataFrame] = None
        self.climate: Optional[pd. Series] = None
        self.climate_path: Optional[str] = None
        self.climate_column: Optional[str] = None
        self.advisor_result: Optional[AdvisorResult] = None
        self.output_dir: Optional[str] = None
        self.goal_var = tk. StringVar(value="highfreq")
        self.var_type_var = tk. StringVar(value="TRW")
        self.compat_mode_var = tk.StringVar(value="dplR")
        self.enable_dplr_ssf_var = tk.BooleanVar(value=False)
        self.enable_dplr_rcs_sf_var = tk.BooleanVar(value=False)
        self.enable_tree_grouping_var = tk.BooleanVar(value=False)
        # Unknown pith: "present" = treat as starting at the pith (offset 1),
        # "exclude" = leave such series out of RCS / signal-free RCS.
        self.unknown_pith_policy_var = tk.StringVar(value="present")
        self._run_unknown_pith_policy = "present"
        self.radius_suffix_variants_var = tk.StringVar(value=_default_radius_suffix_variants_text())
        self.series_group_patterns_var = tk.StringVar(value=_default_series_group_patterns_text())
        self.dplr_parallel_workers_var = tk.StringVar(value="6")
        self.min_series_for_detrending_var = tk.StringVar(value="5")
        self.min_series_count_mode_var = tk.StringVar(value="series")
        self.powt_method_var = tk.StringVar(value=str(DEFAULT_COMPAT_CONFIG.powt_method))
        self.powt_rescale_var = tk.BooleanVar(value=bool(DEFAULT_COMPAT_CONFIG.powt_rescale))
        self.powt_use_for_run_var = tk.BooleanVar(value=bool(DEFAULT_COMPAT_CONFIG.powt_enabled))
        self.enable_dplr_powt_var = tk.BooleanVar(value=bool(DEFAULT_COMPAT_CONFIG.powt_enabled))
        self.dplr_powt_method_var = tk.StringVar(value=str(DEFAULT_COMPAT_CONFIG.powt_method))
        self.dplr_powt_rescale_var = tk.BooleanVar(value=bool(DEFAULT_COMPAT_CONFIG.powt_rescale))
        self.show_powt_results_var = tk.BooleanVar(value=True)
        self.export_both_powt_modes_var = tk.BooleanVar(value=True)
        self.score_auto_preset_var = tk.BooleanVar(value=True)
        _score0 = _normalize_score_profile(DEFAULT_SCORE_PROFILES["highfreq"], goal="highfreq")
        self.score_high_cutoff_var = tk.DoubleVar(value=float(_score0["high_cutoff_years"]))
        self.score_low_cutoff_var = tk.DoubleVar(value=float(_score0["low_cutoff_years"]))
        self.score_w_hi_var = tk.DoubleVar(value=float(_score0["w_var_high"]))
        self.score_w_lo_var = tk.DoubleVar(value=float(_score0["w_var_low"]))
        self.score_w_eps_var = tk.DoubleVar(value=float(_score0["w_eps"]))
        self.score_w_rbar_var = tk.DoubleVar(value=float(_score0["w_rbar"]))
        self.score_w_clim_var = tk.DoubleVar(value=float(_score0["w_climate"]))
        self.status_var = tk.StringVar(value="Load data to begin.")
        self._powt_sync_in_progress = False
        self._powt_display_sync_in_progress = False
        self._powt_last_user_run_signature = (
            bool(self.powt_use_for_run_var.get()),
            str(self.powt_method_var.get()).strip(),
            bool(self.powt_rescale_var.get()),
        )
        self.powt_use_for_run_var.trace_add("write", lambda *_a: self._on_powt_run_controls_changed(source="main"))
        self.enable_dplr_powt_var.trace_add("write", lambda *_a: self._on_powt_run_controls_changed(source="tab"))
        self.powt_method_var.trace_add("write", lambda *_a: self._on_powt_run_controls_changed(source="main"))
        self.dplr_powt_method_var.trace_add("write", lambda *_a: self._on_powt_run_controls_changed(source="tab"))
        self.powt_rescale_var.trace_add("write", lambda *_a: self._on_powt_run_controls_changed(source="main"))
        self.dplr_powt_rescale_var.trace_add("write", lambda *_a: self._on_powt_run_controls_changed(source="tab"))
        self.show_powt_results_var.trace_add("write", self._on_powt_display_changed)
        self.min_series_info_var = tk.StringVar(value="Detrending start year: n/a")
        self.manual_tree_prefixes_var = tk.StringVar(value="")
        self.dataset_var = tk.StringVar(value="")
        self.compare_mode_var = tk. StringVar(value="selected_method")
        self.compare_method_var = tk. StringVar(value="best")
        self.compare_min_overlap_var = tk. IntVar(value=30)
 
        self.signal_method_var = tk. StringVar(value="best")
        self.signal_window_var = tk.IntVar(value=50)
        self.guide_method_var = tk. StringVar(value=sorted(METHOD_EXPLANATIONS.keys())[0])
        self.guide_goal_var = tk. StringVar(value=self.goal_var.get())
        self.current_project_path: Optional[str] = None
        self._last_dialog_dir: str = os.getcwd()
        self._project_dirty: bool = False
        self._project_version: int = 0
        self._compare_cache: Dict[tuple, Any] = {}
        self._pairwise_corr_cache: Dict[tuple, tuple[float, int]] = {}
        self._signal_diag_cache: Dict[tuple, pd.DataFrame] = {}
        self._topbar_items: list[tuple[tk.Widget, int, int, int]] = []
        self._topbar_reflow_job: Optional[str] = None
        self._runtime_log_lines: list[str] = []
        self._runtime_log_session_counter: int = 0
        self._runtime_log_active_session: int = 0
        self._analysis_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._analysis_thread: Optional[threading.Thread] = None
        self._analysis_running: bool = False
        self._analysis_poll_job: Optional[str] = None
        self._powt_preview_cache: Dict[tuple[str, str, bool], PowtPreviewResult] = {}
        self._tree_grouping_working_df: Optional[pd.DataFrame] = None
        self._radius_variants_working_text: str = ""

        self._ui_scale: float = 1.0
        self._base_font_sizes: dict[str, int] = {}
        self._base_widget_fonts: list = []  # [(widget, base_size, family, weight, slant, underline, overstrike)]
        self._base_ttk_font_size: int = 0
        self._base_ttk_font_family: str = ""
        self._settings_path: str = os.path.join(
            os.path.expanduser("~"), ".detrend_advisor_settings.json"
        )
        self.custom_method_keys: list[str] = []
        self._build_ui()
        self._capture_base_font_sizes()
        self._load_prefs()
        self._refresh_custom_method_guide()
        self.after(0, self._apply_button_tooltips)
        # Every dialog / Toplevel: developer signature at the bottom + hover help
        if _uihelp is not None:
            _uihelp.DEVELOPER_SIGNATURE = f"{CREATED_BY_LABEL} {_uihelp.DEVELOPER}"
            _uihelp.install_tk(self, fallback=self._button_tooltip_text)
        self._install_global_text_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.minsize(820, 560)
    # --------------------------- UI layout ---------------------------------
    def on_close(self) -> None:
        """Ensure the Tk process terminates cleanly."""
        try:
            self._save_prefs()
        except Exception:
            pass
        # Online data: keep the download list / cached values for the next start?
        if _online_tk is not None and hasattr(_online_tk, "ask_keep_cache_tk"):
            try:
                _online_tk.ask_keep_cache_tk(self)
            except Exception:
                pass
        try:
            plt.close("all")
        except Exception:
            pass
        try:
            self.quit()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _iter_descendant_widgets(self, root: tk.Widget):
        """Yield all descendant widgets recursively."""
        yield root
        for child in root.winfo_children():
            yield from self._iter_descendant_widgets(child)

    # ----------------------- UI scale helpers --------------------------------

    def _fix_windows_dpi(self) -> None:
        """On high-DPI Windows screens Tk may keep 96 dpi, so all text looks tiny: use the real DPI."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes

            dpi = 0
            try:
                dpi = int(ctypes.windll.user32.GetDpiForWindow(self.winfo_id()))
            except Exception:
                dpi = 0
            if not dpi:
                hdc = ctypes.windll.user32.GetDC(0)
                dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
                ctypes.windll.user32.ReleaseDC(0, hdc)
            current = float(self.tk.call("tk", "scaling"))
            if dpi > 96:
                wanted = dpi / 72.0
                if wanted > current * 1.15:
                    self.tk.call("tk", "scaling", wanted)
        except Exception:
            pass

    def _on_ui_scale_entry(self, _event=None) -> None:
        """Apply the value typed in the UI-scale box (ignores text that is not a number)."""
        try:
            raw = str(self._spin_ui_scale.get()).strip().replace(",", ".")
            self._apply_ui_scale(float(raw))
        except (ValueError, tk.TclError):
            try:
                self._spin_ui_scale_var.set(round(self._ui_scale, 2))
            except Exception:
                pass

    def _capture_base_font_sizes(self) -> None:
        """Record baseline fonts for all three font categories after _build_ui()."""
        named_font_names = set(tkfont.names(root=self))

        # 1. Named fonts (TkDefaultFont, TkTextFont, etc.)
        for name in named_font_names:
            try:
                f = tkfont.nametofont(name)
                self._base_font_sizes[name] = int(f.cget("size"))
            except Exception:
                pass

        # 2. Per-widget hardcoded font tuples / strings that are NOT named fonts.
        #    ttk widgets raise TclError on cget("font") — skip them silently.
        self._base_widget_fonts = []
        for w in self._iter_descendant_widgets(self):
            try:
                spec = w.cget("font")
            except (tk.TclError, AttributeError):
                continue
            if not spec:
                continue
            # Skip if it's just a reference to a named font (handled above)
            if isinstance(spec, str) and spec in named_font_names:
                continue
            try:
                f = tkfont.Font(font=spec)
                self._base_widget_fonts.append((
                    w,
                    int(f.cget("size")),
                    f.cget("family"),
                    f.cget("weight"),
                    f.cget("slant"),
                    int(f.cget("underline")),
                    int(f.cget("overstrike")),
                ))
            except Exception:
                pass

        # 3. TTK theme default font (covers all ttk.Label, ttk.Checkbutton, etc.)
        try:
            style = ttk.Style(self)
            spec = style.lookup(".", "font")
            if spec:
                f = tkfont.Font(font=spec)
                self._base_ttk_font_size = int(f.cget("size"))
                self._base_ttk_font_family = f.cget("family")
        except Exception:
            pass
        # Fallback: use TkDefaultFont as the ttk base
        if not self._base_ttk_font_size:
            self._base_ttk_font_size = self._base_font_sizes.get("TkDefaultFont", 10)
        if not self._base_ttk_font_family:
            try:
                self._base_ttk_font_family = tkfont.nametofont("TkDefaultFont").cget("family")
            except Exception:
                self._base_ttk_font_family = "Arial"

    def _apply_ui_scale(self, value: float) -> None:
        """Rescale all widget fonts (named, per-widget, and ttk styles) proportionally."""
        try:
            scale = float(value)
        except (TypeError, ValueError):
            return
        self._ui_scale = max(0.60, min(2.00, scale))

        # 1. Named fonts
        for name, base_size in self._base_font_sizes.items():
            try:
                f = tkfont.nametofont(name)
                new_size = max(1, round(abs(base_size) * self._ui_scale))
                f.configure(size=new_size if base_size >= 0 else -new_size)
            except Exception:
                pass

        # 2. Per-widget hardcoded fonts
        for item in self._base_widget_fonts:
            w, base_size, family, weight, slant, underline, overstrike = item
            try:
                new_size = max(1, round(abs(base_size) * self._ui_scale))
                w.configure(font=(
                    family,
                    new_size if base_size >= 0 else -new_size,
                    weight,
                    slant,
                ))
            except Exception:
                pass

        # 3. TTK styles — default theme font + all custom styles used in this app
        try:
            style = ttk.Style(self)
            new_ttk_size = max(1, round(self._base_ttk_font_size * self._ui_scale))
            normal_font = (self._base_ttk_font_family, new_ttk_size)
            bold_font = (self._base_ttk_font_family, new_ttk_size, "bold")
            # Update default ttk theme font (covers ttk.Label, ttk.Checkbutton, etc.)
            style.configure(".", font=normal_font)
            # Update custom styles
            style.configure("TopbarBold.TButton", font=bold_font)
            style.configure("TopbarBold.TMenubutton", font=bold_font)
            style.configure("RunAccent.TButton", font=bold_font)
        except Exception:
            pass

        # Sync the spinbox without triggering another callback
        if hasattr(self, "_spin_ui_scale"):
            try:
                self._spin_ui_scale_var.set(round(self._ui_scale, 2))
            except Exception:
                pass

    def _save_prefs(self) -> None:
        """Persist UI preferences (e.g. scale) to a JSON file in the home dir."""
        try:
            prefs = {"ui_scale": float(self._ui_scale), "custom_methods": list(self.custom_method_keys)}
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f, indent=2)
        except Exception as e:
            print("Failed to save preferences:", e)

    def _load_prefs(self) -> None:
        """Restore UI preferences saved by _save_prefs."""
        try:
            if os.path.exists(self._settings_path):
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                self.custom_method_keys = _normalize_custom_methods(prefs.get("custom_methods", []))
                if "ui_scale" in prefs:
                    val = float(prefs["ui_scale"])
                    self._apply_ui_scale(val)
        except Exception as e:
            print("Failed to load preferences:", e)

    def _button_tooltip_text(self, widget: tk.Widget) -> str:
        """Return a short tooltip for button-like widgets."""
        try:
            text = str(widget.cget("text") or "").strip()
        except Exception:
            text = ""
        if not text:
            return ""

        tooltip_map = {
            "Run": "Run the detrending advisor for the current project and selected options.",
            "RUN": "Run the detrending advisor for the current project and selected options.",
            "Project": "Open the project menu for project, data, climate, export-folder, and exit actions.",
            "Add data": "Add one or more data files to the current project without replacing the existing datasets.",
            "Actions": "Open export and RingClim actions.",
            "Help": "Open the help dialog (F1).",
            "Cite": "Open the citation dialog.",
            "About": "Open the About dialog.",
            "Close": "Close this dialog.",
            "Cancel": "Close this dialog without saving changes.",
            "Save": "Save the current changes and close this dialog.",
            "Defaults": "Restore the default example values in this dialog.",
            "Update": "Apply the current controls and refresh the current view.",
            "Tree groups...": "Open the tree-group review dialog for the active dataset.",
            "Score settings...": "Open score settings for frequency cutoffs and score weights.",
            "POWT": "Enable or disable the full POWT workflow. When enabled, the app computes POWT previews and POWT-based advisor results automatically. When disabled, POWT is not analyzed.",
            "Export POWT+NoPOWT": "When enabled, export both no-POWT and POWT result sets.",
            "Auto score preset": "Keep score weights and cutoffs tied automatically to the selected goal.",
            "Apply score preset": "Reset score weights and cutoffs to the default preset for the current goal.",
            "SSF": "Enable signal-free standardization where supported.",
            "SF-RCS": "Enable signal-free RCS workflow.",
            "Apply to selected": "Apply the typed tree label and core/radius label to all selected rows.",
            "Reset selected to auto": "Restore the selected rows to automatically detected tree/core labels.",
            "Rebuild all auto": "Rebuild automatic tree/core labels for the whole active dataset.",
            "Select all": "Select all datasets or methods in this list.",
            "Clear": "Clear the current selection in this list.",
            "Excel only": "Export one Excel workbook plus the Markdown report without per-method CSV files.",
            "CSV + Excel": "Export per-method CSV files, one Excel workbook, and the Markdown report.",
        }
        if text in tooltip_map:
            return tooltip_map[text]
        if _uihelp is not None:
            shared = _uihelp.help_for(text)
            if shared:
                return shared

        if isinstance(widget, (ttk.Menubutton, tk.Menubutton)):
            return f"Open the '{text}' menu."
        if isinstance(widget, (ttk.Checkbutton, tk.Checkbutton)):
            return f"Toggle option: {text}."
        if isinstance(widget, (ttk.Radiobutton, tk.Radiobutton)):
            return f"Choose: {text}."
        return f"Run action: {text}."

    def _apply_button_tooltips_for(self, root: tk.Widget) -> None:
        """Attach hover tooltips to buttons, check/radio buttons and labelled fields."""
        if _uihelp is not None:
            _uihelp.tk_apply_tooltips(root, fallback=self._button_tooltip_text)
            return
        for widget in self._iter_descendant_widgets(root):
            if isinstance(widget, (tk.Button, ttk.Button, ttk.Checkbutton, ttk.Menubutton)):
                tip = self._button_tooltip_text(widget)
                if tip:
                    _attach_hover_tooltip(widget, tip)

    def _apply_button_tooltips(self) -> None:
        """Attach hover tooltips to all button-like widgets in the main window."""
        self._apply_button_tooltips_for(self)

    def _show_scrollable_text_dialog(
        self,
        title: str,
        text: str,
        width: int = 760,
        height: int = 520,
        min_width: int = 420,
        min_height: int = 260,
    ) -> None:
        """Show a resizable dialog with scrollable read-only text."""
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.geometry(f"{int(width)}x{int(height)}")
        dlg.minsize(int(min_width), int(min_height))
        dlg.transient(self)
        try:
            dlg.grab_set()
        except Exception:
            pass

        dlg.rowconfigure(0, weight=1)
        dlg.columnconfigure(0, weight=1)

        body = ttk.Frame(dlg, padding=8)
        body.grid(row=0, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        txt = tk.Text(body, wrap="word")
        txt.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        txt.configure(yscrollcommand=yscroll.set)
        self._insert_formatted_help_text(txt, str(text))
        self._make_text_readonly(txt)

        btn_row = ttk.Frame(body)
        btn_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        btn_row.columnconfigure(1, weight=1)
        ttk.Label(btn_row, text="Find:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        find_var = tk.StringVar(value="")
        find_entry = ttk.Entry(btn_row, textvariable=find_var)
        find_entry.grid(row=0, column=1, sticky="ew")
        find_state = {"pos": "1.0"}

        def _find_next(_event=None):
            needle = str(find_var.get() or "").strip()
            txt.tag_remove("find_hit", "1.0", tk.END)
            if not needle:
                return "break"
            start = txt.search(needle, find_state["pos"], stopindex=tk.END, nocase=True)
            if not start:
                start = txt.search(needle, "1.0", stopindex=tk.END, nocase=True)
            if not start:
                return "break"
            end = f"{start}+{len(needle)}c"
            txt.tag_add("find_hit", start, end)
            txt.tag_remove("sel", "1.0", tk.END)
            txt.tag_add("sel", start, end)
            txt.see(start)
            find_state["pos"] = end
            return "break"

        find_entry.bind("<Return>", _find_next)
        btn_find = ttk.Button(btn_row, text="Find next", command=_find_next)
        btn_find.grid(row=0, column=2, sticky="w", padx=(6, 12))
        btn_copy_all = ttk.Button(
            btn_row,
            text="Copy all",
            command=lambda: self._copy_all_from_widget(txt),
        )
        btn_copy_all.grid(row=0, column=3, sticky="e", padx=(0, 6))
        btn_close = ttk.Button(btn_row, text="Close", command=dlg.destroy)
        btn_close.grid(row=0, column=4, sticky="e")
        _attach_hover_tooltip(find_entry, "Type a word and press Enter to jump to the next match.")
        _attach_hover_tooltip(btn_find, "Jump to the next match of the search text.")
        _attach_hover_tooltip(btn_copy_all, "Copy the whole text of this dialog to the clipboard.")
        _attach_hover_tooltip(btn_close, "Close this dialog.")

        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        try:
            dlg.focus_force()
        except Exception:
            pass
        self._apply_button_tooltips_for(dlg)

    def _apply_app_icon(self) -> None:
        """Use the application icon for the title bar, taskbar and all dialogs."""
        ico_path, png_path = _find_app_icon_files("detrend_advisor")
        if ico_path and os.name == "nt":
            try:
                self.iconbitmap(default=ico_path)
            except Exception:
                pass
        if png_path:
            try:
                self._app_icon_image = tk.PhotoImage(file=png_path)
                self.iconphoto(True, self._app_icon_image)
            except Exception:
                pass

    def _insert_formatted_help_text(self, txt: tk.Text, text: str) -> None:
        """Insert lightweight-markup text with headings, bullets and key hints.

        Markup (line based):
            '# '  -> title, '## ' -> section, '### ' -> subsection,
            '- '  -> bullet, '  - ' -> sub-bullet, '> ' -> note box.
            Text between backticks is shown as a key/code chip.
        """
        try:
            base = tkfont.nametofont("TkDefaultFont")
            family = str(base.cget("family"))
            size = int(abs(int(base.cget("size"))) or 10)
        except Exception:
            family, size = "Arial", 10
        txt.configure(wrap="word", padx=14, pady=10, spacing1=1, spacing3=1)
        txt.tag_configure("body", font=(family, size))
        txt.tag_configure("h1", font=(family, size + 7, "bold"), foreground="#1d4ed8", spacing1=4, spacing3=8)
        txt.tag_configure("h2", font=(family, size + 3, "bold"), foreground="#1e3a8a", spacing1=12, spacing3=4)
        txt.tag_configure("h3", font=(family, size + 1, "bold"), foreground="#374151", spacing1=8, spacing3=2)
        txt.tag_configure("bullet", font=(family, size), lmargin1=14, lmargin2=30)
        txt.tag_configure("subbullet", font=(family, size), lmargin1=34, lmargin2=50)
        txt.tag_configure(
            "note",
            font=(family, size),
            background="#eef4ff",
            lmargin1=10,
            lmargin2=10,
            rmargin=10,
            spacing1=4,
            spacing3=4,
        )
        txt.tag_configure("code", font=("Consolas", size), background="#eef1f6", foreground="#0f172a")
        txt.tag_configure("find_hit", background="#fde68a")

        def _insert_inline(line_text: str, tag: str) -> None:
            parts = line_text.split("`")
            for idx, part in enumerate(parts):
                if not part:
                    continue
                txt.insert(tk.END, part, (tag, "code") if idx % 2 == 1 else (tag,))

        txt.tag_configure("link", font=(family, size), foreground="#2563eb", underline=True, lmargin1=14, lmargin2=30)
        txt.tag_bind("link", "<Enter>", lambda _e: txt.configure(cursor="hand2"))
        txt.tag_bind("link", "<Leave>", lambda _e: txt.configure(cursor=""))
        lines = str(text).split("\n")
        sections = [ln.rstrip()[3:] for ln in lines if ln.startswith("## ")]

        def _jump(mark_name: str) -> str:
            txt.yview(mark_name)
            txt.tag_remove("find_hit", "1.0", tk.END)
            txt.tag_add("find_hit", mark_name, f"{mark_name} lineend")
            return "break"

        section_no = 0
        for raw_line in lines:
            line = raw_line.rstrip()
            if line == "[[toc]]":
                for idx, title in enumerate(sections):
                    link_tag = f"link_{idx}"
                    txt.insert(tk.END, title, ("link", link_tag))
                    txt.insert(tk.END, "\n")
                    txt.tag_bind(link_tag, "<Button-1>", lambda _e, m=f"help_sec_{idx}": _jump(m))
                continue
            if line.startswith("### "):
                _insert_inline(line[4:], "h3")
            elif line.startswith("## "):
                txt.mark_set(f"help_sec_{section_no}", "end-1c")
                txt.mark_gravity(f"help_sec_{section_no}", "left")
                section_no += 1
                _insert_inline(line[3:], "h2")
            elif line.startswith("# "):
                _insert_inline(line[2:], "h1")
            elif line.startswith("  - "):
                _insert_inline("\u25e6 " + line[4:], "subbullet")
            elif line.startswith("- "):
                _insert_inline("\u2022 " + line[2:], "bullet")
            elif line.startswith("> "):
                _insert_inline(line[2:], "note")
            else:
                _insert_inline(line, "body")
            txt.insert(tk.END, "\n")

    def _show_about(self) -> None:
        """Show About dialog."""
        about_text = (
            "Detrend Advisor"
            + (f" {_appupd.version_text()} - part of {_appupd.display_name()}" if _appupd is not None else "")
            + "\n"
            "Author: Dr. Zulfiyor Bakhtiyorov\n"
            "Institution: University of Cambridge, Xinjiang Institute of Ecology and Geography CAS, "
            "Khujand Science Center NAST\n"
            "Year: 2026\n"
            "License: MIT License\n"
        )
        self._show_scrollable_text_dialog(
            "About",
            about_text,
            width=520,
            height=320,
            min_width=360,
            min_height=220,
        )

    def _show_help(self) -> None:
        """Show the full, formatted Help dialog (also opened with F1)."""
        help_lines = [
            "# Detrend Advisor - Help",
            "Detrending advisor for tree-ring width (TRW) and blue intensity (BI) data. "
            "The program detrends every dataset of a project with many methods, scores them for the "
            "selected research goal and recommends the best method.",
            "> Tip: type a word in the Find box below and press Enter. Every text window supports "
            "`Ctrl+C`, `Ctrl+A` and a right-click menu (Copy / Select all / Copy all).",
            "",
            "### Contents (click a line to jump to the section)",
            "[[toc]]",
            "",
            "## 1. Quick start",
            "- Project > Import data (replace project) or the `Add data` button: load one or more files.",
            "- Optional: Project > Import climate (CSV) when the goal uses climate correlation.",
            "- Choose the Dataset, the Goal and (optionally) Score settings, SSF, SF-RCS, POWT.",
            "- Press `RUN`. R/dplR works in the background; the window stays usable and no console window opens.",
            "- Review the tabs, then Actions > Export project outputs or Actions > Open RingClim.",
            "",
            "## 2. Supported input",
            "- Tucson decadal format: .rwl, .tuc, .dec, .txt (fixed width, 999/9990/9999 = missing, -9999 = end).",
            "- CSV: first column (or column named Year) = year, other columns = series.",
            "- Non-positive values and ARSTAN sentinel codes are treated as missing.",
            "- Climate CSV: Year column + one climate column (the first non-year column is used).",
            "",
            "## 3. Project menu",
            "- New project: clear all datasets, results and climate data.",
            "- Open project... / Save project / Save project as...: .daproj files with data, ids, cached results and POWT results.",
            "- Merge project into current...: append datasets and results of another project (names are made unique).",
            "- Import data (replace project)...: replace all datasets with the selected files.",
            "- Add data to project...: append files to the current project (same as the `Add data` button).",
            "- Import climate (CSV)...: load one climate/target series used for the |climate corr| score term.",
            "- Choose export folder...: default folder for all exports (default = folder of the active dataset).",
            "- Exit: save preferences and close the program.",
            "",
            "## 4. Top toolbar",
            "### Row 1",
            "- Project: opens the Project menu (section 3).",
            "- Add data: append one or more files to the project.",
            "- RUN: analyse every dataset that has no up-to-date result for the current settings. "
            "Already computed methods are reused from cache, only missing ones are computed.",
            "- Dataset: the active dataset shown in all tabs.",
            "- Goal: highfreq, mixed, lowfreq, temp_recon, runoff_recon, drought_recon, variance_check (section 6).",
            "- Score settings...: frequency cutoffs and score weights (section 6).",
            "- SSF: add dplR signal-free standardization methods (ssf_spline_30/60/120, ssf_age_dep_spline). Slower.",
            "- SF-RCS: add signal-free RCS (rcs_signal_free). Slower.",
            "### Row 2",
            "- Type: free label of the data type (TRW, BI, ...).",
            "- R workers: number of parallel R processes (Auto = conservative choice for laptops).",
            "- Min count: minimum number of series or trees that must exist in a year before detrending starts.",
            "- Count by: count the minimum by `series` or by `trees`; the label below shows the resulting start year.",
            "- Tree groups (checkbox): group several cores/radii of one tree into one tree mean before detrending.",
            "- ... (button next to Tree groups): open the tree-group editor (section 5).",
            "### Row 3",
            "- POWT: run dplR power transformation (universal and cook) in addition to the normal run and compare them.",
            "- POWT method: which POWT variant is displayed (universal or cook).",
            "- Export POWT+NoPOWT: export both result sets (with and without POWT).",
            "- Actions > Export RWI (all series): save the detrended matrix of the method chosen in the Chronologies tab.",
            "- Actions > Export project outputs: full export for all analysed datasets (section 9).",
            "- Actions > Open RingClim: start RingClim and pre-fill it with chronologies (STD/RES), RWI and POWT tables.",
            "",
            "## 5. Tree groups editor",
            "- Radius/core suffix variants: one set per line (A/B, R1/R2, N/S/E/W, 01/02) or `regex:<pattern>`.",
            "- Defaults: restore built-in suffix sets. Rebuild auto from variants: regroup with the edited sets.",
            "- Table columns: #, Series ID, Prefix, Tree number, Tree label, Core/radius, Confidence, Source.",
            "- Tree label / Core/radius + Apply to selected: assign labels manually to the selected rows.",
            "- Reset selected to auto: restore automatic labels. Rebuild all auto: regroup the whole dataset.",
            "- User prefixes + Apply prefixes: site prefixes to ignore when detecting tree numbers.",
            "- Pattern groups + Update: A/B/C patterns (A = letters, B = digits, C = core suffix), e.g. `AAAABBC, BBC`.",
            "- Series with the same label but without overlapping years are kept as separate trees.",
            "- Save: store the grouping in the dataset. Cancel: discard changes.",
            "",
            "## 6. Goals and scoring",
            "- score = w1*C(Var high) + w2*C(Var low) + w3*EPS + w4*Rbar + w5*|climate corr|, with C(x) = x/(1+x).",
            "- Var high / Var low: variance of the STD chronology after a Butterworth high-/low-pass filter.",
            "- EPS and Rbar: dplR rwi.stats style (Spearman, zeros = missing, tree/core aware).",
            "- Score settings dialog: Auto score preset (weights follow the goal), Apply score preset, "
            "High/Low-frequency cutoff (years), five weights. Weights are normalised to sum 1.",
            "- Goal presets: highfreq favours interannual variance, lowfreq and temp_recon favour low-frequency variance, "
            "mixed/runoff/drought balance both, variance_check stresses EPS/Rbar.",
            "",
            "## 7. Detrending methods",
            "- exp (ModNegExp), linear, mean, runmean_32, hugershoff (ModHugershoff), lowess (Friedman/supsmu).",
            "- Custom methods...: add spline, signal-free spline and running mean periods (for example 10, 15 years). Saved with preferences and projects.",
            "- spline_30 / spline_60 / spline_120 (Cook-Peters spline, 50% cutoff), age_dep_spline, ar.",
            "- rcs (only when RCS is feasible: age spread and few disturbances), rcs_signal_free (SF-RCS).",
            "- ssf_* signal-free standardization variants (SSF).",
            "- Backend: dplR through Rscript. Series that make a method fail are excluded automatically "
            "and listed in the Recommendation tab and in the statistics export.",
            "- Details of each method: Guide tab > Detrending methods.",
            "",
            "## 8. Result tabs",
            "- Recommendation: goal, best method, POWT comparison, metrics, top 3 methods, ages, RCS feasibility, "
            "disturbances, backend warnings. Text is selectable and copyable.",
            "- Runtime log: live R/dplR progress of every run (each run is numbered).",
            "- POWT: raw vs universal vs cook mean series, skewness histograms and summaries.",
            "- Data structure: age histogram, mean growth curve by cambial age, sample depth.",
            "- Chronologies: View = best_vs_all, single_method, rcs_vs_rcs_sf, all_series; Method = method to show.",
            "- all_series: click a series (or choose it in 'Series') to see its measured values, the fitted trend and an "
            "explanation of its peaks (small trend values, release, end effects, low replication); hover shows year and value; "
            "zoom with the toolbar.",
            "- Project > Import from ITRDB (online) opens as a tab; 'Detach to window' / 'Attach as tab' moves it.",
            "- Compare / Correlation: Mode (selected_method / all_methods), Method, Min overlap (years), Update, "
            "dataset list with Select all / Clear; overlay plot and Pearson correlation matrix.",
            "- EPS / Rbar / Depth: Method, Window (years), Update; running EPS, Rbar and sample depth. EPS = 0.85 line.",
            "- Guide: explanations of methods, goals and of the selection logic.",
            "",
            "## 9. Export",
            "- Export mode dialog: `Excel only` (one workbook per dataset + report) or `CSV + Excel` (also per-method CSV). Cancel aborts.",
            "- Per dataset: <name>_all_chronologies.xlsx, <name>_report.md; with CSV mode also "
            "_chron_*_std/res.csv, _rwi_<method>.csv, _eps_rbar_<method>.csv and POWT tables.",
            "- With Export POWT+NoPOWT the names get __powt_off / __powt_on.",
            "- project_statistics_all_datasets.xlsx: one file with ALL statistics of ALL analysed datasets:",
            "  - readme, datasets_summary (periods, ages, depth, disturbances, RCS, best method, POWT decision, weights),",
            "  - methods (rank, score, EPS, Rbar, variances, climate corr, chronology stats, how the method is computed),",
            "  - series_rwi and raw_series (per-series statistics, AR1, correlation with chronology, tree/core labels),",
            "  - eps_rbar_full, eps_rbar_running, exclusions, backend_errors, powt, settings, methods_guide.",
            "- project_statistics_csv/: the same tables as CSV (CSV mode or when Excel is unavailable).",
            "- compare_correlations.csv: pairwise correlations of the Compare tab.",
            "",
            "## 10. R / dplR",
            "- Rscript and the dplR package are detected automatically (PATH, R_HOME, Program Files, conda, Homebrew).",
            "- If not found you can select Rscript manually, install R online (Windows: winget/choco) or install dplR from CRAN.",
            "- All R processes run hidden in the background; progress appears in the Runtime log.",
            "",
            "## 11. Keyboard and mouse",
            "- `Ctrl+C` copy, `Ctrl+A` select all, `Ctrl+V` / `Ctrl+X` in editable fields; works with any keyboard layout.",
            "- Right-click in any text field, list or table: Copy, Cut, Paste, Select all, Copy all / Copy rows.",
            "- In tables (e.g. Tree groups) `Ctrl+C` copies the selected rows with headers as tab-separated text (paste into Excel).",
            "- `F1` opens this Help. Hovering over a control shows a tooltip.",
            "",
            "## 12. Status bar",
            "- UI scale: enlarge or shrink all fonts (saved in ~/.detrend_advisor_settings.json).",
            "- About, Cite, Help: program information, citation, this help.",
            "",
            "## 13. Command line",
            "- `--validate --input FILE [--out-dir DIR]`: compare detrending with dplR.",
            "- `--validate-eps-rbar --input FILE`: compare EPS/Rbar with dplR.",
            "- `--batch --input FILE --output OUT.csv [--goal G --climate C.csv --export-all-methods]`: headless run.",
            "- Options: --compat, --difference, --pos-slope, --constrain-nls, --no-recode-zeros, --rscript, "
            "--install-r, --install-r-deps, --strict-dplr, --validation-methods, --validation-fast, --verbose-progress.",
            "",
            "## 14. Logs and files",
            "- Crash and RingClim launch logs: %LOCALAPPDATA%\\detrend_advisor (Windows), ~/Library/Logs/detrend_advisor (macOS), ~/.cache/detrend_advisor (Linux).",
        ]
        self._show_scrollable_text_dialog(
            "Help",
            "\n".join(help_lines),
            width=900,
            height=680,
            min_width=480,
            min_height=320,
        )

    def _show_cite(self) -> None:
        """Show Cite dialog."""
        cite_text = (
            "Citation text will be added later after the program paper is published."
        )
        self._show_scrollable_text_dialog(
            "Cite",
            cite_text,
            width=520,
            height=240,
            min_width=360,
            min_height=180,
        )

    def _show_cite(self) -> None:
        """Show Cite dialog."""
        cite_text = (
            "Citation text will be added later after the program paper is published."
        )
        messagebox.showinfo("Cite", cite_text)
    def _set_text_readonly(self, widget: tk. Text, text: str) -> None:
        """Set text in a Tk Text widget and keep it read-only but selectable."""
        widget.config(state="normal")
        widget.delete("1.0", tk. END)
        widget.insert(tk. END, text)
        widget.edit_modified(False)
        self._make_text_readonly(widget)

    def _make_text_readonly(self, widget: tk.Text) -> None:
        """Keep a Text widget selectable and copyable while blocking user edits."""
        try:
            widget.config(state="normal", insertwidth=0)
        except Exception:
            pass
        setattr(widget, "_readonly_text_widget", True)
        if bool(getattr(widget, "_readonly_guard_installed", False)):
            return
        widget.bind("<Key>", self._readonly_text_keyguard)
        for seq in ("<<Paste>>", "<<Cut>>", "<<Clear>>", "<<PasteSelection>>"):
            widget.bind(seq, lambda _e: "break")
        setattr(widget, "_readonly_guard_installed", True)

    def _readonly_text_keyguard(self, event):
        """Allow navigation, selection and copy keys in read-only text; block editing."""
        keysym = str(getattr(event, "keysym", "") or "")
        try:
            state = int(getattr(event, "state", 0) or 0)
        except Exception:
            state = 0
        ctrl = bool(state & 0x0004) or (sys.platform == "darwin" and bool(state & 0x0008))
        if keysym in _READONLY_NAV_KEYSYMS:
            return None
        if keysym.startswith("F") and keysym[1:].isdigit():
            return None
        if ctrl:
            if _classify_ctrl_shortcut(event) in ("copy", "select_all"):
                return None
            if keysym.lower() in ("slash", "backslash"):
                return None
        return "break"

    def _clear_runtime_log(self) -> None:
        """Clear the runtime log tab."""
        self._runtime_log_lines = []
        self._runtime_log_session_counter = 0
        self._runtime_log_active_session = 0
        if hasattr(self, "text_runtime_log"):
            try:
                self.text_runtime_log.delete("1.0", tk.END)
            except Exception:
                pass

    def _start_runtime_log_session(self, title: str = "Analysis run") -> int:
        """Start one new runtime-log session without removing previous runs."""
        self._runtime_log_session_counter = int(getattr(self, "_runtime_log_session_counter", 0) or 0) + 1
        self._runtime_log_active_session = int(self._runtime_log_session_counter)
        session_no = int(self._runtime_log_active_session)
        self._append_runtime_log(f"{'=' * 16} {str(title).strip()} #{session_no} {'=' * 16}")
        return session_no

    def _append_runtime_log(self, message: str) -> None:
        """Append one timestamped line to the runtime log tab."""
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {str(message).rstrip()}"
        self._runtime_log_lines.append(line)
        if hasattr(self, "text_runtime_log"):
            try:
                self.text_runtime_log.insert(tk.END, line + "\n")
                self.text_runtime_log.see(tk.END)
                self.update_idletasks()
            except Exception:
                pass



    def _set_run_button_enabled(self, enabled: bool) -> None:
        """Enable or disable the main Run button."""
        if hasattr(self, "btn_run"):
            try:
                self.btn_run.config(state=("normal" if enabled else "disabled"))
            except Exception:
                pass

    def _enqueue_analysis_event(self, kind: str, payload: Any = None) -> None:
        """Send one worker event back to the Tk thread."""
        try:
            self._analysis_queue.put((str(kind), payload))
        except Exception:
            pass

    def _pump_analysis_queue(self) -> None:
        """Process queued worker messages without blocking the UI."""
        self._analysis_poll_job = None
        handled_any = False
        while True:
            try:
                kind, payload = self._analysis_queue.get_nowait()
            except queue.Empty:
                break
            handled_any = True
            if kind == "log":
                self._append_runtime_log(str(payload))
            elif kind == "status":
                try:
                    self.status_var.set(str(payload))
                except Exception:
                    pass
            elif kind == "done":
                self._finish_analysis_from_worker(dict(payload or {}))
            elif kind == "error":
                self._analysis_running = False
                self._set_run_button_enabled(True)
                msg = str(payload or "Unknown analysis worker error.")
                self._append_runtime_log(f"Worker ERROR: {msg}")
                self.status_var.set("Analysis failed before completion.")
                messagebox.showerror("Analysis error", msg)
        if self._analysis_running or handled_any:
            self._analysis_poll_job = self.after(80, self._pump_analysis_queue)

    def _finish_analysis_from_worker(self, payload: Dict[str, Any]) -> None:
        """Finalize GUI state after the background worker finishes."""
        self._analysis_running = False
        self._set_run_button_enabled(True)

        ok = int(payload.get("ok", 0) or 0)
        total = int(payload.get("total", 0) or 0)
        skipped = int(payload.get("skipped", 0) or 0)
        backend = str(payload.get("backend", "") or "")
        workers = int(payload.get("workers", 1) or 1)
        active_key_from_payload = str(payload.get("active_key", "") or "")
        errors = list(payload.get("errors", []) or [])

        if self.active_dataset_key is None and self.datasets:
            self.active_dataset_key = next(iter(self.datasets.keys()))
        if active_key_from_payload and active_key_from_payload in self.datasets:
            self.active_dataset_key = active_key_from_payload
        if self.active_dataset_key is not None:
            self.set_active_dataset(self.active_dataset_key)

        dt_total = float(payload.get("dt_total", 0.0) or 0.0)
        self._append_runtime_log(
            f"Analysis finished | ok={ok}/{total} | total={dt_total:.2f}s"
        )
        session_no = int(getattr(self, "_runtime_log_active_session", 0) or 0)
        if session_no > 0:
            self._append_runtime_log(f"{'=' * 16} Analysis run #{session_no} end {'=' * 16}")

        if ok == 0:
            self.status_var.set("Analysis failed for all selected datasets.")
        else:
            active_key = self.active_dataset_key or ""
            active_best = ""
            if self.advisor_result is not None:
                active_best = str(self.advisor_result.best_key)
            self.status_var.set(
                f"Analysis complete: {ok}/{total} dataset(s) analyzed "
                f"(backend={backend}, workers={workers}, skipped {skipped}). "
                f"Active: {active_key} (best={active_best})"
            )
            self._apply_active_dataset_powt_display_choice()
            self._refresh_dataset_ui_state()
            self._mark_project_dirty()
            self._bump_project_version()

        if errors:
            messagebox.showwarning(
                "Analysis warnings",
                "Some datasets or methods had issues during analysis:\n\n" + "\n".join(errors),
            )

    def _analysis_worker(
        self,
        to_run: list[tuple[str, Any]],
        goal: str,
        cfg: DplrCompatConfig,
        compat_sig: str,
        selected_method_keys: Optional[set[str]],
        n_workers: int,
        score_profile: Dict[str, float],
        min_series_for_detrending: int,
        min_series_count_mode: str,
    ) -> None:
        """Run heavy analysis off the Tk main thread."""
        ok = 0
        errors: list[str] = []
        skipped = int(len(self.datasets) - len(to_run))
        t_run0 = time.perf_counter()
        callback = lambda msg: self._enqueue_analysis_event("log", msg)
        _set_runtime_progress_callback(callback)
        try:
            for key, ds in to_run:
                ds_name = os.path.basename(ds.path) if ds.path else key
                t_ds0 = time.perf_counter()
                self._enqueue_analysis_event("log", f"Dataset START: {key} ({ds_name})")
                self._enqueue_analysis_event(
                    "status",
                    f"Running analysis: {key} ({ok + 1}/{len(to_run)})...",
                )
                try:
                    source_rwl = ds.rwl
                    powt_res: Optional[PowtPreviewResult] = None
                    if cfg.compat == "dplR" and bool(cfg.powt_enabled):
                        self._enqueue_analysis_event(
                            "log",
                            f"POWT START: {key} | method={cfg.powt_method} | rescale={cfg.powt_rescale}",
                        )
                        powt_res = _dplr_powt_preview(
                            ds.rwl,
                            cfg,
                            verbose=False,
                            progress_label="dplR POWT",
                        )
                        source_rwl = powt_res.transformed
                        self._enqueue_analysis_event(
                            "log",
                            f"POWT DONE: {key} | transformed series={int(source_rwl.shape[1])}",
                        )
                        active_key = str(self.active_dataset_key or "")
                        if active_key and active_key == str(key):
                            self._powt_preview_cache[(active_key, str(cfg.powt_method), bool(cfg.powt_rescale))] = powt_res
                    setattr(ds, "powt_preview_result", powt_res)
                    ds.advisor_result = run_advisor_core(
                        source_rwl,
                        goal=goal,
                        climate=self.climate,
                        compat_config=(_cfg_for_powt_transformed(cfg) if powt_res is not None else cfg),
                        po=self._po_for_dataset(ds),
                        method_keys=selected_method_keys,
                        unknown_pith_policy=self._run_unknown_pith_policy,
                    )
                    ds.compat_signature = compat_sig
                    self._invalidate_pairwise_compare_cache_for_dataset_key(key)
                    backend_errors = _format_backend_method_errors(ds.advisor_result)
                    if backend_errors:
                        errors.append(
                            f"{key} ({os.path.basename(ds.path)}):\n" + "\n".join(backend_errors)
                        )
                    dt_ds = time.perf_counter() - t_ds0
                    self._enqueue_analysis_event("log", f"Dataset DONE: {key} | {dt_ds:.2f}s")
                    ok += 1
                except Exception as exc:
                    ds.advisor_result = None
                    ds.compat_signature = None
                    setattr(ds, "powt_preview_result", None)
                    tb = traceback.format_exc()
                    dt_ds = time.perf_counter() - t_ds0
                    self._enqueue_analysis_event(
                        "log",
                        f"Dataset ERROR: {key} | {dt_ds:.2f}s | {exc}",
                    )
                    _print_progress(
                        f"Run analysis failed for dataset '{key}' "
                        f"({os.path.basename(ds.path)}):\n{tb}"
                    )
                    errors.append(
                        f"{key} ({os.path.basename(ds.path)}): {exc}\n\n{tb}"
                    )
        except Exception:
            self._enqueue_analysis_event("error", traceback.format_exc())
            return
        finally:
            _set_runtime_progress_callback(None)

        self._enqueue_analysis_event(
            "done",
            {
                "ok": ok,
                "total": len(to_run),
                "skipped": skipped,
                "backend": cfg.compat,
                "workers": n_workers,
                "errors": errors,
                "active_key": self.active_dataset_key or (next(iter(self.datasets.keys())) if self.datasets else ""),
                "dt_total": time.perf_counter() - t_run0,
            },
        )

    def _clear_figure_legends(self, fig) -> None:
        """Remove figure-level legends before drawing a fresh layout."""
        try:
            for leg in list(getattr(fig, "legends", [])):
                try:
                    leg.remove()
                except Exception:
                    pass
            fig.legends.clear()
        except Exception:
            pass

    def _add_compact_figure_legend(
        self,
        fig,
        ax,
        *,
        ncol: int = 4,
        anchor_y: float = 0.995,
    ) -> None:
        """Place a compact figure legend at the top to preserve plot width."""
        try:
            handles, labels = ax.get_legend_handles_labels()
            clean = [(h, l) for h, l in zip(handles, labels) if str(l).strip()]
            if not clean:
                return
            handles2, labels2 = zip(*clean)
            fig.legend(
                handles2,
                labels2,
                loc="upper center",
                bbox_to_anchor=(0.5, anchor_y),
                ncol=max(1, min(int(ncol), len(labels2))),
                frameon=False,
                fontsize="small",
                borderaxespad=0.0,
            )
        except Exception:
            pass

    def _set_run_button_enabled(self, enabled: bool) -> None:
        """Enable or disable the main Run button."""
        if hasattr(self, "btn_run"):
            try:
                self.btn_run.config(state=("normal" if enabled else "disabled"))
            except Exception:
                pass

    def _enqueue_analysis_event(self, kind: str, payload: Any = None) -> None:
        """Send one worker event back to the Tk thread."""
        try:
            self._analysis_queue.put((str(kind), payload))
        except Exception:
            pass

    def _pump_analysis_queue(self) -> None:
        """Process queued worker messages without blocking the UI."""
        self._analysis_poll_job = None
        handled_any = False
        while True:
            try:
                kind, payload = self._analysis_queue.get_nowait()
            except queue.Empty:
                break
            handled_any = True
            if kind == "log":
                self._append_runtime_log(str(payload))
            elif kind == "status":
                try:
                    self.status_var.set(str(payload))
                except Exception:
                    pass
            elif kind == "done":
                self._finish_analysis_from_worker(dict(payload or {}))
            elif kind == "error":
                self._analysis_running = False
                self._set_run_button_enabled(True)
                msg = str(payload or "Unknown analysis worker error.")
                self._append_runtime_log(f"Worker ERROR: {msg}")
                self.status_var.set("Analysis failed before completion.")
                messagebox.showerror("Analysis error", msg)
        if self._analysis_running or handled_any:
            self._analysis_poll_job = self.after(80, self._pump_analysis_queue)

    def _finish_analysis_from_worker(self, payload: Dict[str, Any]) -> None:
        """Finalize GUI state after the background worker finishes."""
        self._analysis_running = False
        self._set_run_button_enabled(True)

        ok = int(payload.get("ok", 0) or 0)
        total = int(payload.get("total", 0) or 0)
        skipped = int(payload.get("skipped", 0) or 0)
        backend = str(payload.get("backend", "") or "")
        workers = int(payload.get("workers", 1) or 1)
        active_key_from_payload = str(payload.get("active_key", "") or "")
        errors = list(payload.get("errors", []) or [])

        if self.active_dataset_key is None and self.datasets:
            self.active_dataset_key = next(iter(self.datasets.keys()))
        if active_key_from_payload and active_key_from_payload in self.datasets:
            self.active_dataset_key = active_key_from_payload
        if self.active_dataset_key is not None:
            self.set_active_dataset(self.active_dataset_key)

        dt_total = float(payload.get("dt_total", 0.0) or 0.0)
        self._append_runtime_log(
            f"Analysis finished | ok={ok}/{total} | total={dt_total:.2f}s"
        )

        if ok == 0:
            self.status_var.set("Analysis failed for all selected datasets.")
        else:
            active_key = self.active_dataset_key or ""
            active_best = ""
            if self.advisor_result is not None:
                active_best = str(self.advisor_result.best_key)
            self.status_var.set(
                f"Analysis complete: {ok}/{total} dataset(s) analyzed "
                f"(backend={backend}, workers={workers}, skipped {skipped}). "
                f"Active: {active_key} (best={active_best})"
            )
            self._refresh_dataset_ui_state()
            self._mark_project_dirty()
            self._bump_project_version()

        if errors:
            messagebox.showwarning(
                "Analysis warnings",
                "Some datasets or methods had issues during analysis:\n\n" + "\n".join(errors),
            )

    def _analysis_worker(
        self,
        to_run: list[tuple[str, Any]],
        goal: str,
        cfg: DplrCompatConfig,
        compat_sig: str,
        selected_method_keys: Optional[set[str]],
        n_workers: int,
        score_profile: Dict[str, float],
        min_series_for_detrending: int,
        min_series_count_mode: str,
    ) -> None:
        """Run heavy analysis off the Tk main thread."""
        ok = 0
        errors: list[str] = []
        skipped = int(len(self.datasets) - len(to_run))
        t_run0 = time.perf_counter()
        callback = lambda msg: self._enqueue_analysis_event("log", msg)
        _set_runtime_progress_callback(callback)
        try:
            for key, ds in to_run:
                self._ensure_dataset_ids_initialized(ds)
                ds_name = os.path.basename(ds.path) if ds.path else key
                t_ds0 = time.perf_counter()
                had_cached_before = bool(
                    getattr(ds, "compat_signature", None) == compat_sig
                    and (
                        getattr(ds, "advisor_result_no_powt", None) is not None
                        or getattr(ds, "advisor_result_with_powt_universal", None) is not None
                        or getattr(ds, "advisor_result_with_powt_cook", None) is not None
                        or getattr(ds, "advisor_result_with_powt", None) is not None
                    )
                )
                ds_ids = getattr(ds, "ids", None)
                ds_start_year = find_minimum_depth_start_year(
                    ds.rwl,
                    min_series=int(min_series_for_detrending),
                    ids=(ds_ids if str(min_series_count_mode) == "trees" else None),
                    count_mode=str(min_series_count_mode),
                )
                self._enqueue_analysis_event("log", f"Dataset START: {key} ({ds_name})")
                self._enqueue_analysis_event(
                    "log",
                    "Dataset threshold | "
                    f"key={key} | min_count={int(min_series_for_detrending)} | "
                    f"mode={str(min_series_count_mode)} | "
                    f"start_year={('n/a' if ds_start_year is None else int(ds_start_year))}",
                )
                self._enqueue_analysis_event(
                    "status",
                    f"Running analysis: {key} ({ok + 1}/{len(to_run)}) | "
                    f"min {str(min_series_count_mode)}={int(min_series_for_detrending)} | "
                    f"start year={('n/a' if ds_start_year is None else int(ds_start_year))}",
                )
                try:
                    cfg_no_powt = DplrCompatConfig(**vars(cfg))
                    cfg_no_powt.powt_enabled = False

                    cfg_powt_universal = DplrCompatConfig(**vars(cfg))
                    cfg_powt_universal.powt_enabled = bool(cfg.powt_enabled) and bool(cfg.powt_rescale)
                    cfg_powt_universal.powt_method = "universal"

                    cfg_powt_cook = DplrCompatConfig(**vars(cfg))
                    cfg_powt_cook.powt_enabled = bool(cfg.powt_enabled) and bool(cfg.powt_rescale)
                    cfg_powt_cook.powt_method = "cook"

                    cached_no_powt = (
                        getattr(ds, "advisor_result_no_powt", None)
                        if getattr(ds, "compat_signature", None) == compat_sig
                        else None
                    )
                    missing_no_powt = _missing_method_keys_for_result(
                        cached_no_powt,
                        selected_method_keys,
                    )
                    if selected_method_keys is not None and not missing_no_powt:
                        ar_no_powt = cached_no_powt
                        self._enqueue_analysis_event(
                            "log",
                            f"Dataset NO-POWT SKIP: {key} | all requested methods already cached",
                        )
                    else:
                        run_keys_no_powt = (
                            sorted(missing_no_powt)
                            if selected_method_keys is not None
                            else None
                        )
                        if run_keys_no_powt:
                            self._enqueue_analysis_event(
                                "log",
                                f"Dataset NO-POWT START: {key} | methods={','.join(run_keys_no_powt)}",
                            )
                        else:
                            self._enqueue_analysis_event("log", f"Dataset NO-POWT START: {key}")
                        try:
                            ar_no_powt_new = run_advisor_core(
                                ds.rwl,
                                goal=goal,
                                climate=self.climate,
                                compat_config=cfg_no_powt,
                                ids=ds_ids,
                                po=self._po_for_dataset(ds),
                                method_keys=run_keys_no_powt,
                                score_profile=score_profile,
                                min_series_for_detrending=int(min_series_for_detrending),
                                min_series_count_mode=str(min_series_count_mode),
                                unknown_pith_policy=self._run_unknown_pith_policy,
                            )
                        except Exception as exc:
                            if cached_no_powt is not None and _is_no_candidates_runtime_error(exc):
                                ar_no_powt = cached_no_powt
                                self._enqueue_analysis_event(
                                    "log",
                                    f"Dataset NO-POWT KEEP-CACHE: {key} | no new valid candidates for methods={','.join(run_keys_no_powt or [])}",
                                )
                            else:
                                raise
                        else:
                            ar_no_powt = _merge_advisor_results(cached_no_powt, ar_no_powt_new)
                            self._enqueue_analysis_event("log", f"Dataset NO-POWT DONE: {key}")
 
                    powt_res_universal: Optional[PowtPreviewResult] = None
                    ar_powt_universal: Optional[AdvisorResult] = None
                    if cfg_powt_universal.compat == "dplR" and bool(cfg_powt_universal.powt_enabled):
                        cached_powt_universal = (
                            getattr(ds, "advisor_result_with_powt_universal", None)
                            if getattr(ds, "compat_signature", None) == compat_sig
                            else None
                        )
                        missing_powt_universal = _missing_method_keys_for_result(
                            cached_powt_universal,
                            selected_method_keys,
                        )
                        powt_cache_key_u = (str(key), "universal", True)
                        powt_res_universal = getattr(ds, "powt_preview_for_run_universal", None)
                        if powt_res_universal is None:
                            powt_res_universal = self._powt_preview_cache.get(powt_cache_key_u)
                        if powt_res_universal is None:
                            self._enqueue_analysis_event(
                                "log",
                                f"POWT START: {key} | method={cfg_powt_universal.powt_method} | rescale={cfg_powt_universal.powt_rescale}",
                            )
                            powt_res_universal = _dplr_powt_preview(
                                ds.rwl,
                                cfg_powt_universal,
                                verbose=False,
                                progress_label="dplR POWT universal",
                            )
                            self._powt_preview_cache[powt_cache_key_u] = powt_res_universal
                            self._enqueue_analysis_event("log", f"POWT DONE: {key} | method=universal")
                        else:
                            self._enqueue_analysis_event("log", f"POWT SKIP: {key} | method=universal | cached preview reused")
                        if selected_method_keys is not None and not missing_powt_universal:
                            ar_powt_universal = cached_powt_universal
                            self._enqueue_analysis_event("log", f"Dataset POWT SKIP: {key} | method=universal | all requested methods already cached")
                        else:
                            run_keys_powt_u = (
                                sorted(missing_powt_universal)
                                if selected_method_keys is not None
                                else None
                            )
                            if run_keys_powt_u:
                                self._enqueue_analysis_event(
                                    "log",
                                    f"Dataset POWT START: {key} | method=universal | methods={','.join(run_keys_powt_u)}",
                                )
                            else:
                                self._enqueue_analysis_event("log", f"Dataset POWT START: {key} | method=universal")
                            try:
                                ar_powt_universal_new = run_advisor_core(
                                    powt_res_universal.transformed,
                                    goal=goal,
                                    climate=self.climate,
                                    compat_config=_cfg_for_powt_transformed(cfg_powt_universal),
                                    ids=ds_ids,
                                    po=self._po_for_dataset(ds),
                                    method_keys=run_keys_powt_u,
                                    score_profile=score_profile,
                                    min_series_for_detrending=int(min_series_for_detrending),
                                    min_series_count_mode=str(min_series_count_mode),
                                    unknown_pith_policy=self._run_unknown_pith_policy,
                                )
                            except Exception as exc:
                                if cached_powt_universal is not None and _is_no_candidates_runtime_error(exc):
                                    ar_powt_universal = cached_powt_universal
                                    self._enqueue_analysis_event(
                                        "log",
                                        f"Dataset POWT KEEP-CACHE: {key} | method=universal | no new valid candidates for methods={','.join(run_keys_powt_u or [])}",
                                    )
                                else:
                                    raise
                            else:
                                ar_powt_universal = _merge_advisor_results(cached_powt_universal, ar_powt_universal_new)
                                self._enqueue_analysis_event("log", f"Dataset POWT DONE: {key} | method=universal")

                    powt_res_cook: Optional[PowtPreviewResult] = None
                    ar_powt_cook: Optional[AdvisorResult] = None
                    if cfg_powt_cook.compat == "dplR" and bool(cfg_powt_cook.powt_enabled):
                        cached_powt_cook = (
                            getattr(ds, "advisor_result_with_powt_cook", None)
                            if getattr(ds, "compat_signature", None) == compat_sig
                            else None
                        )
                        missing_powt_cook = _missing_method_keys_for_result(
                            cached_powt_cook,
                            selected_method_keys,
                        )
                        powt_cache_key_c = (str(key), "cook", True)
                        powt_res_cook = getattr(ds, "powt_preview_for_run_cook", None)
                        if powt_res_cook is None:
                            powt_res_cook = self._powt_preview_cache.get(powt_cache_key_c)
                        if powt_res_cook is None:
                            self._enqueue_analysis_event(
                                "log",
                                f"POWT START: {key} | method={cfg_powt_cook.powt_method} | rescale={cfg_powt_cook.powt_rescale}",
                            )
                            powt_res_cook = _dplr_powt_preview(
                                ds.rwl,
                                cfg_powt_cook,
                                verbose=False,
                                progress_label="dplR POWT cook",
                            )
                            self._powt_preview_cache[powt_cache_key_c] = powt_res_cook
                            self._enqueue_analysis_event("log", f"POWT DONE: {key} | method=cook")
                        else:
                            self._enqueue_analysis_event("log", f"POWT SKIP: {key} | method=cook | cached preview reused")
                        if selected_method_keys is not None and not missing_powt_cook:
                            ar_powt_cook = cached_powt_cook
                            self._enqueue_analysis_event("log", f"Dataset POWT SKIP: {key} | method=cook | all requested methods already cached")
                        else:
                            run_keys_powt_c = (
                                sorted(missing_powt_cook)
                                if selected_method_keys is not None
                                else None
                            )
                            if run_keys_powt_c:
                                self._enqueue_analysis_event(
                                    "log",
                                    f"Dataset POWT START: {key} | method=cook | methods={','.join(run_keys_powt_c)}",
                                )
                            else:
                                self._enqueue_analysis_event("log", f"Dataset POWT START: {key} | method=cook")
                            try:
                                ar_powt_cook_new = run_advisor_core(
                                    powt_res_cook.transformed,
                                    goal=goal,
                                    climate=self.climate,
                                    compat_config=_cfg_for_powt_transformed(cfg_powt_cook),
                                    ids=ds_ids,
                                    po=self._po_for_dataset(ds),
                                    method_keys=run_keys_powt_c,
                                    score_profile=score_profile,
                                    min_series_for_detrending=int(min_series_for_detrending),
                                    min_series_count_mode=str(min_series_count_mode),
                                    unknown_pith_policy=self._run_unknown_pith_policy,
                                )
                            except Exception as exc:
                                if cached_powt_cook is not None and _is_no_candidates_runtime_error(exc):
                                    ar_powt_cook = cached_powt_cook
                                    self._enqueue_analysis_event(
                                        "log",
                                        f"Dataset POWT KEEP-CACHE: {key} | method=cook | no new valid candidates for methods={','.join(run_keys_powt_c or [])}",
                                    )
                                else:
                                    raise
                            else:
                                ar_powt_cook = _merge_advisor_results(cached_powt_cook, ar_powt_cook_new)
                                self._enqueue_analysis_event("log", f"Dataset POWT DONE: {key} | method=cook")

                    score_u = self._best_advisor_score(ar_powt_universal)
                    score_c = self._best_advisor_score(ar_powt_cook)
                    if np.isfinite(score_u) and (not np.isfinite(score_c) or score_u >= score_c):
                        ar_with_powt = ar_powt_universal
                        powt_res = powt_res_universal
                        powt_best_method = "universal"
                    elif np.isfinite(score_c):
                        ar_with_powt = ar_powt_cook
                        powt_res = powt_res_cook
                        powt_best_method = "cook"
                    else:
                        ar_with_powt = ar_powt_universal or ar_powt_cook
                        powt_res = powt_res_universal or powt_res_cook
                        powt_best_method = "universal" if ar_powt_universal is not None else ("cook" if ar_powt_cook is not None else "")

                    if not bool(cfg.powt_rescale):
                        flag = "not_required"
                        note = "POWT was skipped because POWT rescale is off. Recommended state: POWT OFF."
                        chosen = ar_no_powt
                    else:
                        flag, note, chosen = self._compare_powt_runs(
                            ar_no_powt,
                            ar_with_powt,
                            powt_res,
                        )

                    ds.advisor_result_no_powt = ar_no_powt
                    ds.advisor_result_with_powt_universal = ar_powt_universal
                    ds.advisor_result_with_powt_cook = ar_powt_cook
                    ds.advisor_result_with_powt = ar_with_powt
                    ds.powt_preview_for_run_universal = powt_res_universal
                    ds.powt_preview_for_run_cook = powt_res_cook
                    ds.powt_preview_for_run = powt_res
                    ds.powt_best_method = powt_best_method
                    ds.powt_recommendation_flag = flag
                    ds.powt_recommendation_text = note
                    ds.advisor_result = chosen or ar_no_powt or ar_with_powt
                    if bool(self.show_powt_results_var.get()) and ar_with_powt is not None:
                        ds.displayed_result_mode = "with_powt"
                        ds.advisor_result = ar_with_powt
                        ds.powt_preview_result = powt_res
                    elif (not bool(self.show_powt_results_var.get())) and ar_no_powt is not None:
                        ds.displayed_result_mode = "no_powt"
                        ds.advisor_result = ar_no_powt
                        ds.powt_preview_result = None
                    else:
                        ds.displayed_result_mode = "recommended"
                        ds.powt_preview_result = powt_res if bool(self.show_powt_results_var.get()) else None
                    ds.compat_signature = compat_sig
                    self._invalidate_pairwise_compare_cache_for_dataset_key(key)

                    backend_errors = _format_backend_method_errors(ds.advisor_result)
                    if backend_errors:
                        errors.append(
                            f"{key} ({os.path.basename(ds.path)}):\n" + "\n".join(backend_errors)
                        )
                    if note:
                        self._enqueue_analysis_event("log", f"POWT recommendation [{key}]: {note}")
                    dt_ds = time.perf_counter() - t_ds0
                    self._enqueue_analysis_event("log", f"Dataset DONE: {key} | {dt_ds:.2f}s")
                    ok += 1
                except Exception as exc:
                    if not had_cached_before:
                        ds.advisor_result = None
                        ds.advisor_result_no_powt = None
                        ds.advisor_result_with_powt_universal = None
                        ds.advisor_result_with_powt_cook = None
                        ds.advisor_result_with_powt = None
                        ds.powt_preview_for_run_universal = None
                        ds.powt_preview_for_run_cook = None
                        ds.powt_preview_for_run = None
                        ds.powt_best_method = ""
                        ds.powt_recommendation_text = ""
                        ds.powt_recommendation_flag = "unknown"
                        ds.compat_signature = None
                    else:
                        self._enqueue_analysis_event(
                            "log",
                            f"Dataset CACHE PRESERVED: {key} | previous successful results kept after partial-run error",
                        )
                    tb = traceback.format_exc()
                    dt_ds = time.perf_counter() - t_ds0
                    self._enqueue_analysis_event(
                        "log",
                        f"Dataset ERROR: {key} | {dt_ds:.2f}s | {exc}",
                    )
                    _print_progress(
                        f"Run analysis failed for dataset '{key}' "
                        f"({os.path.basename(ds.path)}):\n{tb}"
                    )
                    errors.append(
                        f"{key} ({os.path.basename(ds.path)}): {exc}\n\n{tb}"
                    )
        except Exception:
            self._enqueue_analysis_event("error", traceback.format_exc())
            return
        finally:
            _set_runtime_progress_callback(None)

        self._enqueue_analysis_event(
            "done",
            {
                "ok": ok,
                "total": len(to_run),
                "skipped": skipped,
                "backend": cfg.compat,
                "workers": n_workers,
                "errors": errors,
                "active_key": self.active_dataset_key or (next(iter(self.datasets.keys())) if self.datasets else ""),
                "dt_total": time.perf_counter() - t_run0,
            },
        )

    def _clear_figure_legends(self, fig) -> None:
        """Remove figure-level legends before drawing a fresh layout."""
        try:
            for leg in list(getattr(fig, "legends", [])):
                try:
                    leg.remove()
                except Exception:
                    pass
            fig.legends.clear()
        except Exception:
            pass

    def _add_compact_figure_legend(
        self,
        fig,
        ax,
        *,
        ncol: int = 4,
        anchor_y: float = 0.995,
    ) -> None:
        """Place a compact figure legend at the top to preserve plot width."""
        try:
            handles, labels = ax.get_legend_handles_labels()
            clean = [(h, l) for h, l in zip(handles, labels) if str(l).strip()]
            if not clean:
                return
            handles2, labels2 = zip(*clean)
            fig.legend(
                handles2,
                labels2,
                loc="upper center",
                bbox_to_anchor=(0.5, anchor_y),
                ncol=max(1, min(int(ncol), len(labels2))),
                frameon=False,
                fontsize="small",
                borderaxespad=0.0,
            )
        except Exception:
            pass



    def _install_global_text_shortcuts(self) -> None:
        """Install global clipboard and select-all shortcuts for app text widgets."""
        self.bind_all("<Control-KeyPress>", self._handle_global_ctrl_keypress, add="+")
        self.bind_all("<F1>", lambda _e: (self._show_help(), "break")[1], add="+")
        if sys.platform == "darwin":
            self.bind_all("<Command-KeyPress>", self._handle_global_ctrl_keypress, add="+")
            self.bind_all("<Button-2>", self._show_text_context_menu, add="+")
            self.bind_all("<Control-Button-1>", self._show_text_context_menu, add="+")
        else:
            self.bind_all("<Button-3>", self._show_text_context_menu, add="+")

    def _copy_to_clipboard(self, text: str) -> None:
        """Put plain text on the system clipboard."""
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(str(text))
        except Exception:
            pass

    def _copy_treeview_selection(self, widget: tk.Widget) -> str:
        """Copy selected Treeview rows (or all rows) as tab-separated text with headers."""
        try:
            items = list(widget.selection()) or list(widget.get_children(""))
            cols = list(widget["columns"])
            lines = ["\t".join(str(widget.heading(c, "text") or c) for c in cols)]
            for item in items:
                vals = widget.item(item, "values")
                lines.append("\t".join(str(v) for v in vals))
            self._copy_to_clipboard("\n".join(lines))
        except Exception:
            pass
        return "break"

    def _copy_listbox_selection(self, widget: tk.Widget) -> str:
        """Copy selected Listbox rows (or all rows) as plain text lines."""
        try:
            idx = list(widget.curselection()) or list(range(int(widget.size())))
            self._copy_to_clipboard("\n".join(str(widget.get(i)) for i in idx))
        except Exception:
            pass
        return "break"

    def _copy_all_from_widget(self, widget: tk.Widget) -> None:
        """Copy the whole content of a text-like widget."""
        try:
            if str(widget.winfo_class()) == "Text":
                self._copy_to_clipboard(str(widget.get("1.0", "end-1c")))
            else:
                self._copy_to_clipboard(str(widget.get()))
        except Exception:
            pass

    def _show_text_context_menu(self, event):
        """Show a Copy / Cut / Paste / Select all context menu for text and table widgets."""
        widget = getattr(event, "widget", None)
        if widget is None or isinstance(widget, str):
            return None
        try:
            klass = str(widget.winfo_class())
        except Exception:
            return None
        text_classes = ("Text", "Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox")
        if klass not in text_classes + ("Treeview", "Listbox"):
            return None
        try:
            widget.focus_set()
        except Exception:
            pass
        menu = tk.Menu(self, tearoff=False)
        if klass == "Treeview":
            menu.add_command(
                label="Copy rows",
                accelerator="Ctrl+C",
                command=lambda w=widget: self._copy_treeview_selection(w),
            )
            menu.add_command(
                label="Select all rows",
                accelerator="Ctrl+A",
                command=lambda w=widget: w.selection_set(w.get_children("")),
            )
        elif klass == "Listbox":
            menu.add_command(
                label="Copy",
                accelerator="Ctrl+C",
                command=lambda w=widget: self._copy_listbox_selection(w),
            )
            menu.add_command(
                label="Select all",
                accelerator="Ctrl+A",
                command=lambda w=widget: w.selection_set(0, tk.END),
            )
        else:
            readonly = self._is_widget_readonly(widget)
            menu.add_command(
                label="Cut",
                accelerator="Ctrl+X",
                state=("disabled" if readonly else "normal"),
                command=lambda w=widget: w.event_generate("<<Cut>>"),
            )
            menu.add_command(
                label="Copy",
                accelerator="Ctrl+C",
                command=lambda w=widget: self._copy_widget_selection(w),
            )
            menu.add_command(
                label="Paste",
                accelerator="Ctrl+V",
                state=("disabled" if readonly else "normal"),
                command=lambda w=widget: w.event_generate("<<Paste>>"),
            )
            menu.add_separator()
            menu.add_command(
                label="Select all",
                accelerator="Ctrl+A",
                command=lambda w=widget: self._select_all_in_widget(w),
            )
            menu.add_command(
                label="Copy all",
                command=lambda w=widget: self._copy_all_from_widget(w),
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"

    def _get_focused_text_widget(self) -> Optional[tk.Widget]:
        """Return the currently focused text-like widget, if any."""
        widget = self.focus_get()
        if widget is None:
            return None
        try:
            klass = str(widget.winfo_class())
        except Exception:
            return None
        if klass in ("Text", "Entry", "TEntry", "Spinbox", "TCombobox"):
            return widget
        return None

    def _is_widget_readonly(self, widget: tk.Widget) -> bool:
        """Return True when the widget should not be edited."""
        if bool(getattr(widget, "_readonly_text_widget", False)):
            return True
        try:
            state = str(widget.cget("state")).strip().lower()
        except Exception:
            state = ""
        return state in ("readonly", "disabled")

    def _copy_widget_selection(self, widget: tk.Widget) -> str:
        """Copy the current selection from a text-like widget."""
        text = ""
        try:
            if str(widget.winfo_class()) == "Text":
                text = str(widget.get("sel.first", "sel.last"))
            elif widget.selection_present():
                full = str(widget.get())
                text = full[int(widget.index("sel.first")):int(widget.index("sel.last"))]
        except Exception:
            try:
                text = str(widget.selection_get())
            except Exception:
                text = ""
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
        return "break"

    def _select_all_in_widget(self, widget: tk.Widget) -> str:
        """Select all text in a text-like widget."""
        try:
            if str(widget.winfo_class()) == "Text":
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "1.0")
                widget.see("insert")
            else:
                widget.selection_range(0, tk.END)
                widget.icursor(tk.END)
        except Exception:
            pass
        return "break"

    def _handle_global_copy(self, event=None):
        """Handle global copy for app text widgets."""
        widget = self._get_focused_text_widget()
        if widget is None:
            return None
        return self._copy_widget_selection(widget)

    def _handle_global_cut(self, event=None):
        """Handle global cut for app text widgets."""
        widget = self._get_focused_text_widget()
        if widget is None:
            return None
        if self._is_widget_readonly(widget):
            return "break"
        try:
            widget.event_generate("<<Cut>>")
        except Exception:
            pass
        return "break"

    def _handle_global_paste(self, event=None):
        """Handle global paste for app text widgets."""
        widget = self._get_focused_text_widget()
        if widget is None:
            return None
        if self._is_widget_readonly(widget):
            return "break"
        try:
            widget.event_generate("<<Paste>>")
        except Exception:
            pass
        return "break"

    def _handle_global_ctrl_keypress(self, event):
        """Handle Ctrl shortcuts with any keyboard layout (English, Russian, etc.)."""
        action = _classify_ctrl_shortcut(event)
        if not action:
            return None
        focused = self.focus_get()
        if focused is None:
            return None
        try:
            klass = str(focused.winfo_class())
        except Exception:
            return None
        if klass == "Treeview":
            if action == "copy":
                return self._copy_treeview_selection(focused)
            if action == "select_all":
                try:
                    focused.selection_set(focused.get_children(""))
                except Exception:
                    pass
                return "break"
            return None
        if klass == "Listbox":
            if action == "copy":
                return self._copy_listbox_selection(focused)
            if action == "select_all":
                try:
                    focused.selection_set(0, tk.END)
                except Exception:
                    pass
                return "break"
            return None
        widget = self._get_focused_text_widget()
        if widget is None:
            return None
        if action == "select_all":
            return self._select_all_in_widget(widget)
        # Latin Ctrl+C/V/X (and Command on macOS) are already handled by Tk class
        # bindings; handling them again here would paste or cut twice.
        keysym = str(getattr(event, "keysym", "") or "").lower()
        try:
            state = int(getattr(event, "state", 0) or 0)
        except Exception:
            state = 0
        if sys.platform == "darwin":
            native = keysym in _CTRL_SHORTCUT_LATIN and bool(state & 0x0008)
        else:
            native = keysym in _CTRL_SHORTCUT_LATIN
        if native:
            return None
        if action == "copy":
            return self._copy_widget_selection(widget)
        if action == "paste":
            return self._handle_global_paste(event)
        if action == "cut":
            return self._handle_global_cut(event)
        return None
    def _update_method_guide_text(self) -> None:
        """Update method guide text based on the current selection."""
        if not hasattr(self, "text_method_guide"):
            return
        key = self.guide_method_var.get().strip()
        title = f"{key}\n\n"
        body = _method_explanation(key, "No description available for this method.")
        self._set_text_readonly(self.text_method_guide, title + body)
    def _update_goal_guide_text(self) -> None:
        """Update goal guide text based on the current selection."""
        if not hasattr(self, "text_goal_guide"):
            return
        goal = self.guide_goal_var.get().strip()
        label = GOAL_LABELS.get(goal, goal)
        expl = GOAL_EXPLANATIONS.get(goal, "No description available for this goal.")
        profile = self._get_current_score_profile(goal_override=goal)
        score = _format_score_rule_text(goal, profile)
        txt = (
            f"{goal} ({label})\n\n"
            f"{expl}\n\n"
            "Scoring rule used by the advisor:\n"
            f"{score}\n\n"
            "Where:\n"
            "- C(x) = x / (1 + x)\n"
            f"- Var(high): variance of the STD chronology after a Butterworth high-pass filter "
            f"with cutoff {profile['high_cutoff_years']:.1f} years\n"
            f"- Var(low): variance of the STD chronology after a Butterworth low-pass filter "
            f"with cutoff {profile['low_cutoff_years']:.1f} years\n"
            "- EPS: dplR-like expressed population signal from the detrended RWI matrix\n"
            "- Rbar: dplR-like interseries correlation (rbar.tot)\n"
            "- climate corr: absolute Pearson correlation between the STD chronology and the loaded climate series\n"
            "- All score weights are normalized to sum to 1.0 before scoring"
        )
        self._set_text_readonly(self.text_goal_guide, txt)
    def _update_selection_guide_text(self) -> None:
        """Update explanation of how the best method is selected."""
        if not hasattr(self, "text_select_guide"):
            return
        goal = self.goal_var.get().strip()
        profile = self._get_current_score_profile(goal_override=goal)
        score = _format_score_rule_text(goal, profile)
        txt = (
            "1) The program builds multiple candidate chronologies using different detrending methods.\n"
            "2) For each candidate it computes diagnostics:\n"
            "   - EPS and Rbar (signal strength / coherence)\n"
            "   - Variance in a high-frequency band and a low-frequency band from the STD chronology\n"
            "   - Optional climate/target correlation if a climate series is loaded\n"
            "3) A goal-specific score is calculated and the method with the highest score is recommended.\n\n"
            f"Current goal: {goal} ({GOAL_LABELS.get(goal, goal)})\n"
            f"Score formula:\n{score}\n\n"
            "Current score settings:\n"
            f"- high-frequency cutoff = {profile['high_cutoff_years']:.1f} years\n"
            f"- low-frequency cutoff = {profile['low_cutoff_years']:.1f} years\n"
            f"- weights: Var(high)={profile['w_var_high']:.2f}, Var(low)={profile['w_var_low']:.2f}, "
            f"EPS={profile['w_eps']:.2f}, Rbar={profile['w_rbar']:.2f}, |climate corr|={profile['w_climate']:.2f}\n\n"
            "Implementation notes:\n"
            "- Chronology = robust STD chronology built from detrended RWI values\n"
            "- Variance metrics are filtered-chronology variances compressed with C(x)=x/(1+x)\n"
            "- EPS and Rbar are calculated from dplR-like rwi.stats logic on the detrended RWI matrix\n"
            "- If climate is not loaded, the climate-correlation term becomes 0"
        )
        self._set_text_readonly(self.text_select_guide, txt)
    def _update_guide_texts(self) -> None:
        """Refresh all guide texts."""
        self._update_method_guide_text()
        self._update_goal_guide_text()
        self._update_selection_guide_text()
    def _on_goal_changed(self) -> None:
        """Sync goal-related guide widgets when the main goal combobox changes."""
        if bool(self.score_auto_preset_var.get()):
            self._apply_score_profile_preset(self.goal_var.get())
        self.guide_goal_var.set(self.goal_var.get())
        self._update_goal_guide_text()
        self._update_selection_guide_text()

    def _apply_score_profile_preset(self, goal: Optional[str] = None) -> None:
        """Apply default score settings for the given goal."""
        g = str(goal or self.goal_var.get() or "mixed").strip()
        p = _normalize_score_profile(DEFAULT_SCORE_PROFILES.get(g, DEFAULT_SCORE_PROFILES["mixed"]), goal=g)
        self.score_high_cutoff_var.set(float(p["high_cutoff_years"]))
        self.score_low_cutoff_var.set(float(p["low_cutoff_years"]))
        self.score_w_hi_var.set(float(p["w_var_high"]))
        self.score_w_lo_var.set(float(p["w_var_low"]))
        self.score_w_eps_var.set(float(p["w_eps"]))
        self.score_w_rbar_var.set(float(p["w_rbar"]))
        self.score_w_clim_var.set(float(p["w_climate"]))

    def _on_score_setting_changed(self, _event=None) -> None:
        """Refresh guides and status after score settings are edited."""
        self._update_guide_texts()
        try:
            self.status_var.set(
                "Score settings updated. Run analysis again to apply new weights/cutoffs."
            )
        except Exception:
            pass

    def _get_current_score_profile(self, goal_override: Optional[str] = None) -> Dict[str, float]:
        """Return the current score profile from UI controls."""
        g = str(goal_override or self.goal_var.get() or "mixed").strip()
        profile = {
            "high_cutoff_years": self.score_high_cutoff_var.get(),
            "low_cutoff_years": self.score_low_cutoff_var.get(),
            "w_var_high": self.score_w_hi_var.get(),
            "w_var_low": self.score_w_lo_var.get(),
            "w_eps": self.score_w_eps_var.get(),
            "w_rbar": self.score_w_rbar_var.get(),
            "w_climate": self.score_w_clim_var.get(),
        }
        return _normalize_score_profile(profile, goal=g)

    def _score_profile_signature(self) -> str:
        """Build a stable signature for score settings."""
        p = self._get_current_score_profile()
        return "|".join(
            [
                f"hf={p['high_cutoff_years']:.6f}",
                f"lf={p['low_cutoff_years']:.6f}",
                f"wh={p['w_var_high']:.6f}",
                f"wl={p['w_var_low']:.6f}",
                f"we={p['w_eps']:.6f}",
                f"wr={p['w_rbar']:.6f}",
                f"wc={p['w_climate']:.6f}",
            ]
        )

    def _show_score_settings_dialog(self) -> None:
        """Open a compact dialog for score weights and frequency cutoffs."""
        dlg = tk.Toplevel(self)
        dlg.title("Score settings")
        dlg.geometry("520x340")
        dlg.minsize(420, 280)
        dlg.transient(self)
        try:
            dlg.grab_set()
        except Exception:
            pass
    
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="both", expand=True)
    
        row = 0
    
        ttk.Checkbutton(
            frm,
            text="Auto score preset",
            variable=self.score_auto_preset_var,
            command=self._on_score_setting_changed,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        _attach_hover_tooltip(
            frm.grid_slaves(row=row, column=0)[0],
            "Keep score cutoffs and weights automatically tied to the selected goal."
        )
        row += 1
    
        btn_preset = ttk.Button(
            frm,
            text="Apply score preset",
            command=lambda: (self._apply_score_profile_preset(self.goal_var.get()), self._on_score_setting_changed()),
        )
        btn_preset.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
        _attach_hover_tooltip(
            btn_preset,
            "Reset cutoffs and weights to the default preset for the current goal."
        )
        row += 1
    
        controls = [
            ("High-frequency cutoff (years)", self.score_high_cutoff_var, "Years for the high-pass filter used to compute Var(high)."),
            ("Low-frequency cutoff (years)", self.score_low_cutoff_var, "Years for the low-pass filter used to compute Var(low)."),
            ("Weight: Var(high)", self.score_w_hi_var, "Relative score weight for the high-frequency variance term."),
            ("Weight: Var(low)", self.score_w_lo_var, "Relative score weight for the low-frequency variance term."),
            ("Weight: EPS", self.score_w_eps_var, "Relative score weight for EPS."),
            ("Weight: Rbar", self.score_w_rbar_var, "Relative score weight for Rbar."),
            ("Weight: |climate corr|", self.score_w_clim_var, "Relative score weight for absolute climate correlation."),
        ]
    
        for label_text, var_obj, tip in controls:
            lbl = ttk.Label(frm, text=label_text)
            lbl.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            ent = ttk.Entry(frm, textvariable=var_obj, width=10)
            ent.grid(row=row, column=1, sticky="w", pady=4)
            ent.bind("<Return>", self._on_score_setting_changed)
            ent.bind("<FocusOut>", self._on_score_setting_changed)
            _attach_hover_tooltip(lbl, tip)
            _attach_hover_tooltip(ent, tip)
            row += 1
    
        btn_row = ttk.Frame(frm)
        btn_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
    
        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side="right")
        self._apply_button_tooltips_for(dlg)
    

    def _on_compat_mode_changed(self) -> None:
        """Update backend-dependent controls and status text."""
        self.compat_mode_var.set("dplR")
        use_dplr = True
        state = "normal"
        if hasattr(self, "chk_dplr_ssf"):
            self.chk_dplr_ssf.config(state=state)
        if hasattr(self, "chk_dplr_rcs_sf"):
            self.chk_dplr_rcs_sf.config(state=state)
        if hasattr(self, "combo_dplr_workers"):
            self.combo_dplr_workers.config(state="readonly")
        if hasattr(self, "chk_dplr_powt"):
            self.chk_dplr_powt.config(state=state)
        if hasattr(self, "combo_dplr_powt_method"):
            self.combo_dplr_powt_method.config(state="readonly")

        workers_label = str(self.dplr_parallel_workers_var.get()).strip() or "Auto"
        powt_label = ""
        if bool(self.enable_dplr_powt_var.get()):
            powt_label = f", POWT={str(self.dplr_powt_method_var.get()).strip() or 'universal'}"
        self.status_var.set(
            f"Backend selected: dplR only. Click 'Run analysis' to apply it "
            f"(R workers={workers_label}{powt_label})."
        )
        self._update_min_series_info_for_active_dataset()

    def _get_min_series_for_detrending(self) -> int:
        """Return the current minimum depth threshold for starting detrending."""
        raw = str(self.min_series_for_detrending_var.get()).strip()
        try:
            return int(max(1, int(raw)))
        except Exception:
            self.min_series_for_detrending_var.set("5")
            return 5

    def _update_min_series_info_for_active_dataset(self) -> None:
        """Refresh the main-window label that shows the detrending start year."""
        threshold = self._get_min_series_for_detrending()
        count_mode = self._get_min_series_count_mode()
        unit_label = "trees" if count_mode == "trees" else "series"
        if self.advisor_result is not None:
            result_mode = str(getattr(self.advisor_result, "min_series_count_mode", count_mode) or count_mode)
            result_unit_label = "trees" if result_mode == "trees" else "series"
            result_start_year = getattr(self.advisor_result, "analysis_start_year", None)
            if result_start_year is None:
                self.min_series_info_var.set(
                    f"Detrending start year (>= {threshold} {result_unit_label}): not available"
                )
            else:
                self.min_series_info_var.set(
                    f"Detrending start year (>= {threshold} {result_unit_label}): {int(result_start_year)}"
                )
            return
        if self.rwl is None or self.rwl.empty:
            self.min_series_info_var.set(
                f"Detrending start year (>= {threshold} {unit_label}): n/a"
            )
            return
        ids = self._get_active_dataset_ids() if count_mode == "trees" else None
        start_year = find_minimum_depth_start_year(
            self.rwl,
            min_series=threshold,
            ids=ids,
            count_mode=count_mode,
        )
        if start_year is None:
            self.min_series_info_var.set(
                f"Detrending start year (>= {threshold} {unit_label}): not available"
            )
        else:
            self.min_series_info_var.set(
                f"Detrending start year (>= {threshold} {unit_label}): {int(start_year)}"
            )

    def _on_min_series_threshold_changed(self) -> None:
        """Apply a new minimum depth threshold and refresh dataset-dependent text."""
        self._update_min_series_info_for_active_dataset()
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            try:
                self.set_active_dataset(self.active_dataset_key)
            except Exception:
                pass

    def _apply_dplr_parallel_workers_env(self) -> None:
        """Apply the GUI-selected dplR worker override to the process environment."""
        raw = str(self.dplr_parallel_workers_var.get()).strip()
        if raw.lower() in ("", "auto", "default"):
            os.environ.pop("DETREND_R_PARALLEL_WORKERS", None)
            return
        try:
            n_workers = max(1, int(raw))
        except Exception:
            self.dplr_parallel_workers_var.set("Auto")
            os.environ.pop("DETREND_R_PARALLEL_WORKERS", None)
            return
        os.environ["DETREND_R_PARALLEL_WORKERS"] = str(n_workers)

    def _current_powt_cache_key(self) -> Optional[tuple[str, str, bool]]:
        """Return a cache key for the current POWT preview state."""
        if not self.active_dataset_key:
            return None
        method = str(self.powt_method_var.get()).strip().lower() or "universal"
        rescale = bool(self.enable_dplr_powt_var.get())
        return (str(self.active_dataset_key), method, rescale)

    def _build_powt_summary_text(self, result: Optional[PowtPreviewResult]) -> str:
        """Build a human-readable POWT summary."""
        if result is None:
            return (
                "No POWT preview available.\n\n"
                "Enable POWT in the main toolbar to compute POWT preview and POWT-based advisor results automatically."
            )
        p = pd.to_numeric(result.power, errors="coerce")
        p = p[np.isfinite(p.to_numpy(dtype=float))]
        raw_sk = pd.to_numeric(result.raw_skew, errors="coerce")
        raw_sk = raw_sk[np.isfinite(raw_sk.to_numpy(dtype=float))]
        tr_sk = pd.to_numeric(result.powt_skew, errors="coerce")
        tr_sk = tr_sk[np.isfinite(tr_sk.to_numpy(dtype=float))]
        lines = [
            f"POWT method: {result.method}",
            f"Rescale: {'Yes' if result.rescale else 'No'}",
            f"Series: {int(result.transformed.shape[1])}",
            f"Years: {int(result.transformed.shape[0])}",
            f"Use for normal Run: {'Yes' if bool(self.enable_dplr_powt_var.get()) else 'No'}",
            "",
        ]
        if p.size > 0:
            lines.extend(
                [
                    f"Power min: {float(np.nanmin(p)):.4f}",
                    f"Power median: {float(np.nanmedian(p)):.4f}",
                    f"Power max: {float(np.nanmax(p)):.4f}",
                    "",
                ]
            )
        if raw_sk.size > 0:
            lines.append(f"Raw skew median: {float(np.nanmedian(raw_sk)):.4f}")
        if tr_sk.size > 0:
            lines.append(f"POWT skew median: {float(np.nanmedian(tr_sk)):.4f}")
        return "\n".join(lines).strip()

    def _render_powt_preview(
        self,
        result: Optional[PowtPreviewResult],
        result_universal: Optional[PowtPreviewResult] = None,
        result_cook: Optional[PowtPreviewResult] = None,
    ) -> None:
        """Render POWT preview figure and summary text."""
        if not hasattr(self, "fig_powt"):
            return
        self.fig_powt.clear()
        ax_top = self.fig_powt.add_subplot(2, 1, 1)
        ax_bottom = self.fig_powt.add_subplot(2, 1, 2)
        if result is None and result_universal is None and result_cook is None:
            ax_top.text(
                0.5,
                0.5,
                "No POWT preview available",
                ha="center",
                va="center",
                transform=ax_top.transAxes,
            )
            ax_bottom.axis("off")
            self.fig_powt.tight_layout()
            self.canvas_powt.draw_idle()
            if hasattr(self, "text_powt_summary_universal"):
                self._set_text_readonly(
                    self.text_powt_summary_universal,
                    self._build_powt_summary_text(None),
                )
            if hasattr(self, "text_powt_summary_cook"):
                self._set_text_readonly(
                    self.text_powt_summary_cook,
                    self._build_powt_summary_text(None),
                )
            return

        raw_df = self.rwl.copy().astype(float) if self.rwl is not None else pd.DataFrame()
        raw_mean = raw_df.mean(axis=1, skipna=True) if not raw_df.empty else pd.Series(dtype=float)
        if not raw_mean.empty:
            ax_top.plot(raw_mean.index, raw_mean.values, label="Raw mean", color="tab:gray", linewidth=1.2)

        if result_universal is not None:
            powt_mean_u = result_universal.transformed.mean(axis=1, skipna=True)
            if not powt_mean_u.empty:
                ax_top.plot(
                    powt_mean_u.index,
                    powt_mean_u.values,
                    label="Universal mean",
                    color="tab:blue",
                    linewidth=1.2,
                )
        if result_cook is not None:
            powt_mean_c = result_cook.transformed.mean(axis=1, skipna=True)
            if not powt_mean_c.empty:
                ax_top.plot(
                    powt_mean_c.index,
                    powt_mean_c.values,
                    label="Cook mean",
                    color="tab:orange",
                    linewidth=1.2,
                )
        if result_universal is None and result_cook is None and result is not None:
            powt_mean = result.transformed.mean(axis=1, skipna=True)
            if not powt_mean.empty:
                ax_top.plot(
                    powt_mean.index,
                    powt_mean.values,
                    label=f"{str(result.method).capitalize()} mean",
                    color="tab:blue",
                    linewidth=1.2,
                )
        ax_top.set_title("POWT preview (Raw + Universal + Cook)")
        ax_top.set_xlabel("Year")
        ax_top.set_ylabel("Mean value")
        ax_top.grid(True)
        ax_top.legend(loc="upper right", frameon=False, fontsize="small")

        base_result = result_universal or result_cook or result
        raw_sk = pd.to_numeric(base_result.raw_skew, errors="coerce").to_numpy(dtype=float)
        raw_sk = raw_sk[np.isfinite(raw_sk)]
        bins = 16
        if raw_sk.size > 0:
            ax_bottom.hist(raw_sk, bins=bins, alpha=0.45, color="tab:gray", label="Raw skew")
        if result_universal is not None:
            tr_sk_u = pd.to_numeric(result_universal.powt_skew, errors="coerce").to_numpy(dtype=float)
            tr_sk_u = tr_sk_u[np.isfinite(tr_sk_u)]
            if tr_sk_u.size > 0:
                ax_bottom.hist(
                    tr_sk_u,
                    bins=bins,
                    alpha=0.40,
                    color="tab:blue",
                    label="Universal skew",
                )
        if result_cook is not None:
            tr_sk_c = pd.to_numeric(result_cook.powt_skew, errors="coerce").to_numpy(dtype=float)
            tr_sk_c = tr_sk_c[np.isfinite(tr_sk_c)]
            if tr_sk_c.size > 0:
                ax_bottom.hist(
                    tr_sk_c,
                    bins=bins,
                    alpha=0.40,
                    color="tab:orange",
                    label="Cook skew",
                )
        if result_universal is None and result_cook is None and result is not None:
            tr_sk = pd.to_numeric(result.powt_skew, errors="coerce").to_numpy(dtype=float)
            tr_sk = tr_sk[np.isfinite(tr_sk)]
            if tr_sk.size > 0:
                ax_bottom.hist(
                    tr_sk,
                    bins=bins,
                    alpha=0.45,
                    color="tab:blue",
                    label=f"{str(result.method).capitalize()} skew",
                )
        ax_bottom.set_title("Skew distribution across series")
        ax_bottom.set_xlabel("Skew")
        ax_bottom.set_ylabel("Frequency")
        ax_bottom.grid(True)
        has_any_powt_skew = False
        try:
            has_any_powt_skew = (
                (result_universal is not None and np.isfinite(pd.to_numeric(result_universal.powt_skew, errors="coerce").to_numpy(dtype=float)).any())
                or (result_cook is not None and np.isfinite(pd.to_numeric(result_cook.powt_skew, errors="coerce").to_numpy(dtype=float)).any())
                or (result is not None and np.isfinite(pd.to_numeric(result.powt_skew, errors="coerce").to_numpy(dtype=float)).any())
            )
        except Exception:
            has_any_powt_skew = False
        if raw_sk.size == 0 and not has_any_powt_skew:
            ax_bottom.text(
                0.5,
                0.5,
                "No finite skew values were available for plotting.",
                ha="center",
                va="center",
                transform=ax_bottom.transAxes,
            )
        if raw_sk.size > 0 or has_any_powt_skew:
            ax_bottom.legend(loc="upper right", frameon=False, fontsize="small")

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="The figure layout has changed to tight",
                category=UserWarning,
            )
            self.fig_powt.tight_layout(rect=[0.02, 0.02, 0.98, 0.98])
        self.canvas_powt.draw_idle()
        if hasattr(self, "text_powt_summary_universal"):
            self._set_text_readonly(
                self.text_powt_summary_universal,
                self._build_powt_summary_text(result_universal),
            )
        if hasattr(self, "text_powt_summary_cook"):
            self._set_text_readonly(
                self.text_powt_summary_cook,
                self._build_powt_summary_text(result_cook),
            )

    def _update_powt_tab(self) -> None:
        """Refresh POWT tab for the current dataset and cached settings."""
        if not bool(self.enable_dplr_powt_var.get()):
            self._render_powt_preview(None, result_universal=None, result_cook=None)
            return
        result = None
        result_universal = None
        result_cook = None
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            ds = self.datasets.get(self.active_dataset_key)
            if ds is not None:
                result = getattr(ds, "powt_preview_for_run", None)
                result_universal = getattr(ds, "powt_preview_for_run_universal", None)
                result_cook = getattr(ds, "powt_preview_for_run_cook", None)
        if result is None:
            key = self._current_powt_cache_key()
            result = self._powt_preview_cache.get(key) if key is not None else None
            method_now = self._selected_powt_display_method() or "universal"
            if method_now == "universal":
                result_universal = result
            elif method_now == "cook":
                result_cook = result
        if (
            result is None
            and result_universal is None
            and result_cook is None
            and self.rwl is not None
            and not self.rwl.empty
            and str(self.compat_mode_var.get()).strip() == "dplR"
        ):
            try:
                cfg = self._resolve_run_compat_config()
            except Exception:
                cfg = None
            if cfg is not None and cfg.compat == "dplR":
                try:
                    cfg_universal = DplrCompatConfig(**vars(cfg))
                    cfg_universal.powt_enabled = True
                    cfg_universal.powt_method = "universal"
                    cfg_universal.powt_rescale = True
                    result_universal = _dplr_powt_preview(
                        self.rwl,
                        cfg_universal,
                        verbose=False,
                        progress_label="dplR POWT universal",
                    )
                except Exception:
                    result_universal = None
                try:
                    cfg_cook = DplrCompatConfig(**vars(cfg))
                    cfg_cook.powt_enabled = True
                    cfg_cook.powt_method = "cook"
                    cfg_cook.powt_rescale = True
                    result_cook = _dplr_powt_preview(
                        self.rwl,
                        cfg_cook,
                        verbose=False,
                        progress_label="dplR POWT cook",
                    )
                except Exception:
                    result_cook = None
                method_now = self._selected_powt_display_method() or "universal"
                if method_now == "cook":
                    result = result_cook or result_universal
                else:
                    result = result_universal or result_cook
                if self.active_dataset_key and self.active_dataset_key in self.datasets:
                    ds = self.datasets.get(self.active_dataset_key)
                    if ds is not None:
                        ds.powt_preview_for_run_universal = result_universal
                        ds.powt_preview_for_run_cook = result_cook
                        ds.powt_preview_for_run = result
                        if result_universal is not None and result_cook is not None:
                            ds.powt_best_method = method_now
                        elif result_universal is not None:
                            ds.powt_best_method = "universal"
                        elif result_cook is not None:
                            ds.powt_best_method = "cook"
                if result_universal is not None:
                    self._powt_preview_cache[(str(self.active_dataset_key), "universal", True)] = result_universal
                if result_cook is not None:
                    self._powt_preview_cache[(str(self.active_dataset_key), "cook", True)] = result_cook
        self._render_powt_preview(
            result,
            result_universal=result_universal,
            result_cook=result_cook,
        )

    def on_preview_powt(self) -> None:
        """Compute a POWT preview for the active dataset using dplR."""
        if self.rwl is None or self.rwl.empty:
            messagebox.showwarning("No data", "Load a dataset first.")
            return
        if str(self.compat_mode_var.get()).strip() != "dplR":
            messagebox.showwarning("dplR required", "POWT preview is available only through the dplR backend.")
            return
        cfg = self._resolve_run_compat_config()
        if cfg is None or cfg.compat != "dplR":
            return
        if not bool(self.enable_dplr_powt_var.get()):
            self._render_powt_preview(None, result_universal=None, result_cook=None)
            self.status_var.set("POWT preview skipped because POWT is off.")
            return
        cfg.powt_enabled = True
        cfg.powt_method = self._selected_powt_display_method() or "universal"
        cfg.powt_rescale = True
        self.status_var.set("Computing dplR POWT preview...")
        self.update_idletasks()
        try:
            res = _dplr_powt_preview(
                self.rwl,
                cfg,
                verbose=False,
                progress_label="dplR POWT preview",
            )
        except Exception as exc:
            messagebox.showerror("POWT preview failed", str(exc))
            self.status_var.set("POWT preview failed.")
            return
        cache_key = self._current_powt_cache_key()
        if cache_key is not None:
            self._powt_preview_cache[cache_key] = res
        method_now = self._selected_powt_display_method() or "universal"
        self._render_powt_preview(
            res,
            result_universal=(res if method_now == "universal" else None),
            result_cook=(res if method_now == "cook" else None),
        )
        self.status_var.set("POWT preview updated.")

    def _sync_powt_tab_to_run_controls(self, force_enable: bool = True) -> None:
        """Copy POWT-tab settings into the main run controls.

        When force_enable is True, the normal Run button always includes POWT
        and uses the current POWT-tab method/rescale settings.
        """
        method_now = self._selected_powt_display_method() or "universal"
        rescale_now = bool(self.dplr_powt_rescale_var.get())
        run_enabled_now = bool(self.enable_dplr_powt_var.get())
        self.powt_method_var.set(method_now)
        self.powt_rescale_var.set(rescale_now)
        self.powt_use_for_run_var.set(True if force_enable else run_enabled_now)
        if force_enable:
            self.enable_dplr_powt_var.set(True)
        else:
            self.enable_dplr_powt_var.set(run_enabled_now)
        self.dplr_powt_method_var.set(method_now)
        self.dplr_powt_rescale_var.set(rescale_now)
        self._on_compat_mode_changed()

    def on_run_powt_analysis(self) -> None:
        """Run analysis using the current POWT-tab settings."""
        self._sync_powt_tab_to_run_controls(force_enable=True)
        self.on_run()

    def _register_topbar_widget(
        self,
        widget: tk.Widget,
        *,
        pad_x: int = 0,
        pad_y: int = 0,
        row_hint: int = 0,
    ) -> None:
        """Register one top-bar widget for the fixed three-row layout."""
        self._topbar_items.append((widget, int(pad_x), int(pad_y), int(row_hint)))

    def _schedule_topbar_reflow(self, event=None) -> None:
        """Schedule a deferred reflow of the responsive top bar."""
        if self._topbar_reflow_job is not None:
            try:
                self.after_cancel(self._topbar_reflow_job)
            except Exception:
                pass
        self._topbar_reflow_job = self.after(40, self._reflow_topbar)

    def _reflow_topbar(self) -> None:
        """Place top-bar controls in a fixed three-row layout."""
        self._topbar_reflow_job = None
        if not hasattr(self, "topbar"):
            return
        try:
            self.topbar.update_idletasks()
        except Exception:
            return

        for widget, _, _, _ in self._topbar_items:
            try:
                widget.grid_forget()
            except Exception:
                pass

        row_cols: Dict[int, int] = {}
        for widget, pad_x, pad_y, row_hint in self._topbar_items:
            row = int(max(0, row_hint))
            col = int(row_cols.get(row, 0))
            widget.grid(row=row, column=col, sticky="w", padx=pad_x, pady=pad_y)
            row_cols[row] = col + 1

    def _current_compat_config(self) -> DplrCompatConfig:
        """Build the current runtime compatibility config from GUI state."""
        base = DEFAULT_COMPAT_CONFIG
        powt_method = str(self.dplr_powt_method_var.get()).strip().lower() or "universal"
        if powt_method not in ("universal", "cook"):
            powt_method = "universal"
        return DplrCompatConfig(
            compat="dplR",
            recode_zeros=bool(base.recode_zeros),
            difference=bool(base.difference),
            pos_slope=bool(base.pos_slope),
            constrain_nls=base.constrain_nls,
            rscript=str(base.rscript),
            strict_dplr=True,
            auto_install_r=bool(base.auto_install_r),
            auto_install_r_deps=bool(base.auto_install_r_deps),
            cran_repo=str(base.cran_repo),
            powt_enabled=bool(self.enable_dplr_powt_var.get()),
            powt_method=powt_method,
            powt_rescale=bool(self.dplr_powt_rescale_var.get()),
        )

    def _refresh_custom_method_guide(self) -> None:
        keys = sorted(set(METHOD_EXPLANATIONS) | set(self.custom_method_keys))
        self.guide_method_combo.configure(values=keys)
        if self.guide_method_var.get() not in keys:
            self.guide_method_var.set(keys[0])
        self._update_method_guide_text()

    def on_custom_methods(self) -> None:
        """Edit additional parameterized detrending methods without writing code."""
        if self._analysis_running:
            messagebox.showinfo("Custom methods", "Wait for the current analysis to finish.", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("Custom detrending methods")
        win.transient(self)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Add methods alongside the standard presets.\n"
                  "Spline period = wavelength in years at 50% frequency response (f=0.5).\n"
                  "Custom signal-free splines run even when the standard SSF option is off.",
                  wraplength=560).pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        kind = tk.StringVar(value="Spline")
        ttk.Combobox(row, textvariable=kind, values=["Spline", "Signal-free spline", "Running mean"],
                     state="readonly", width=20).pack(side=tk.LEFT)
        periods = tk.StringVar(value="10, 15")
        ttk.Entry(row, textvariable=periods, width=24).pack(side=tk.LEFT, padx=6)
        ttk.Label(frame, text="Whole years: 3–10000. Add several periods separated by commas.").pack(anchor="w")
        listing = tk.Listbox(frame, selectmode=tk.EXTENDED, height=9, exportselection=False)
        listing.pack(fill=tk.BOTH, expand=True, pady=8)
        pending = list(self.custom_method_keys)

        def refresh():
            listing.delete(0, tk.END)
            for key in pending:
                listing.insert(tk.END, key)

        def add():
            try:
                values = re.split(r"[,;\s]+", periods.get().strip())
                if not values or any(not re.fullmatch(r"[0-9]+", v) for v in values):
                    raise ValueError("Enter whole year periods, for example: 10, 15.")
                prefix = {"Spline": "spline", "Signal-free spline": "ssf_spline", "Running mean": "runmean"}[kind.get()]
                keys = [f"{prefix}_{int(v)}" for v in values]
                for key in keys:
                    _parameterized_method(key)
                duplicates = [k for k in keys if k in VALIDATION_METHOD_KEYS or k in pending]
                pending[:] = _normalize_custom_methods(pending + keys)
                refresh()
                if duplicates:
                    messagebox.showinfo("Custom methods", "Already available: " + ", ".join(dict.fromkeys(duplicates)), parent=win)
            except ValueError as exc:
                messagebox.showerror("Custom methods", str(exc), parent=win)

        def remove():
            for index in reversed(listing.curselection()):
                pending.pop(index)
            refresh()

        def save():
            self.custom_method_keys = list(pending)
            self._refresh_custom_method_guide()
            self._save_prefs()
            self._mark_project_dirty()
            self._bump_project_version()
            self.status_var.set("Custom methods saved. Press RUN to calculate updated results.")
            win.destroy()

        ttk.Button(row, text="Add", command=add).pack(side=tk.LEFT)
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Remove selected", command=remove).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT, padx=6)
        refresh()
        win.grab_set()

    def _selected_method_keys_for_run(self, cfg: DplrCompatConfig) -> Optional[set[str]]:
        """Return the method subset requested by the current GUI state."""
        if cfg.compat != "dplR":
            return None
        keys = set(FAST_GUI_DPLR_METHOD_KEYS)
        if bool(self.enable_dplr_ssf_var.get()):
            keys.update(SSF_GUI_DPLR_METHOD_KEYS)
        if bool(self.enable_dplr_rcs_sf_var.get()):
            keys.add("rcs_signal_free")
        keys.update(self.custom_method_keys)
        return keys

    def _fallback_to_legacy_backend(self, reason: str) -> Optional[DplrCompatConfig]:
        """Disable Python fallback and keep the GUI locked to dplR only."""
        messagebox.showwarning(
            "dplR backend unavailable",
            reason + "\n\nPython backend is temporarily disabled in this build."
        )
        self.compat_mode_var.set("dplR")
        self.status_var.set("Analysis cancelled: dplR backend is unavailable.")
        return None

    def _resolve_run_compat_config(self) -> Optional[DplrCompatConfig]:
        """Resolve the backend for the next run.

        Behavior:
            - Show a warning first when R/Rscript or dplR is missing.
            - Let the user switch to the internal Python backend immediately.
            - If the user does not switch to Python, offer online installation.
        """
        cfg = self._current_compat_config()
        if cfg.compat != "dplR":
            return cfg

        resolved_rscript = _resolve_rscript_path(cfg.rscript)
        if not resolved_rscript:

            manual_now = messagebox.askyesno(
                "R runtime not found",
                "dplR backend requires R and Rscript, but Rscript was not found.\n\n"
                "Yes = select the Rscript executable manually.\n"
                "No = continue with other options.",
            )
            if manual_now:
                manual_path = _ask_user_for_rscript_path(parent=self)
                if manual_path:
                    cfg.rscript = str(manual_path)
                    resolved_rscript = str(manual_path)
                else:
                    messagebox.showwarning(
                        "Rscript not selected",
                        "No valid Rscript executable was selected.",
                        parent=self,
                    )

        if not resolved_rscript:
            use_python = messagebox.askyesno(
                "R runtime not found",
                "dplR backend requires R and Rscript, but Rscript was not found.\n\n"
                "Yes = switch to the internal Python backend for this run.\n"
                "No = try to locate or install R.",
            )
            if use_python:
                cfg.compat = "legacy"
                self.status_var.set("Using Python backend for this run.")
                return cfg

            if os.name == "nt":
                install_now = messagebox.askyesno(
                    "Install R runtime",
                    "Rscript was not found.\n\n"
                    "Yes = try online installation of R now.\n"
                    "No = cancel this run.",
                )
                if install_now:
                    cfg.auto_install_r = True
                    self.status_var.set("Trying to install R runtime online...")
                    self.update_idletasks()
                    ok_r, install_log = _try_install_r_windows_online()
                    resolved_after = _resolve_rscript_path(cfg.rscript)
                    if not resolved_after:
                        for cand in _iter_known_rscript_candidates(cfg.rscript):
                            resolved_after = str(cand)
                            break
                    if not resolved_after:
                        manual_after_install = messagebox.askyesno(
                            "Rscript still not found",
                            "Automatic detection still did not find Rscript.\n\n"
                            "Yes = select the Rscript executable manually.\n"
                            "No = continue with fallback handling.",
                            parent=self,
                        )
                        if manual_after_install:
                            manual_path = _ask_user_for_rscript_path(parent=self)
                            if manual_path:
                                resolved_after = str(manual_path)
                    if not resolved_after:
                        detail = install_log.strip()
                        if detail:
                            detail = "\n\n" + detail
                        return self._fallback_to_legacy_backend(
                            "Online installation of R failed." + detail
                        )
                    cfg.rscript = str(resolved_after)
                    self.status_var.set("R runtime found after installation.")
                else:
                    self.status_var.set("Analysis cancelled: R runtime is required for dplR backend.")
                    return None
            else:
                manual_on_non_windows = messagebox.askyesno(
                    "R runtime not found",
                    "Automatic detection did not find Rscript.\n\n"
                    "Yes = select the Rscript executable manually.\n"
                    "No = cancel this run.",
                    parent=self,
                )
                if manual_on_non_windows:
                    manual_path = _ask_user_for_rscript_path(parent=self)
                    if manual_path:
                        cfg.rscript = str(manual_path)
                        resolved_rscript = str(manual_path)
                    else:
                        self.status_var.set("Analysis cancelled: R runtime is required for dplR backend.")
                        return None
                else:
                    self.status_var.set("Analysis cancelled: R runtime is required for dplR backend.")
                    return None

        else:
            cfg.rscript = str(resolved_rscript)

        rscript_with_dplr = _find_rscript_with_package(cfg, "dplR")
        if rscript_with_dplr:
            cfg.rscript = str(rscript_with_dplr)
            self.status_var.set("Found R runtime and R package 'dplR'.")
            return cfg

        if not _dplr_package_available(cfg):
            try_other_rscript = messagebox.askyesno(
                "dplR package not found",
                "R was found, but the selected R installation cannot load the package 'dplR'.\n\n"
                "Yes = select another Rscript executable manually.\n"
                "No = continue with package installation or fallback.",
                parent=self,
            )
            if try_other_rscript:
                manual_path = _ask_user_for_rscript_path(parent=self)
                if manual_path:
                    cfg.rscript = str(manual_path)
                    if _dplr_package_available(cfg):
                        self.status_var.set("Found R runtime and R package 'dplR'.")
                        return cfg

            use_python = messagebox.askyesno(
                "dplR package not found",
                "R was found, but the R package 'dplR' is not installed.\n\n"
                "Yes = switch to the internal Python backend for this run.\n"
                "No = continue and attempt installation from CRAN.",
            )
            if use_python:
                return self._fallback_to_legacy_backend(
                    "The R package 'dplR' is not installed."
                )
            install_pkg = messagebox.askyesno(
                "dplR package not found",
                "Attempt online installation of 'dplR' from CRAN now?",
            )
            if install_pkg:
                cfg.auto_install_r_deps = True
                self.status_var.set("Trying to install R package 'dplR' from CRAN...")
                self.update_idletasks()
                ok, output = _try_install_dplr_package(cfg)
                rscript_with_dplr = _find_rscript_with_package(cfg, "dplR")
                if rscript_with_dplr:
                    cfg.rscript = str(rscript_with_dplr)
                if not ok or not _dplr_package_available(cfg):
                    detail = output.strip()
                    if detail:
                        detail = "\n\n" + detail
                    return self._fallback_to_legacy_backend(
                        "Online installation of the R package 'dplR' failed." + detail
                    )
                self.status_var.set("R package 'dplR' installed successfully.")
            else:
                self.status_var.set("Analysis cancelled: dplR package is required for dplR backend.")
                return None

        return cfg

    def _apply_modern_theme(self, style: ttk.Style) -> None:
        """Apply a flat, modern ttk theme with accent colors and hover states."""
        bg = "#f3f5f9"
        surface = "#ffffff"
        border = "#c9d2e0"
        text = "#1f2937"
        muted = "#6b7280"
        accent = "#2563eb"
        accent_soft = "#e8f0fe"
        accent_pressed = "#dbe6fb"
        success = "#16a34a"
        success_active = "#15803d"
        self._ui_theme_colors = {"bg": bg, "surface": surface, "border": border, "text": text, "accent": accent}
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        try:
            self.configure(background=bg)
            self.option_add("*Toplevel.background", bg)
            self.option_add("*Text.background", surface)
            self.option_add("*Text.foreground", text)
            self.option_add("*Text.relief", "flat")
            self.option_add("*Text.borderWidth", 0)
            self.option_add("*Text.highlightThickness", 1)
            self.option_add("*Text.highlightBackground", border)
            self.option_add("*Text.highlightColor", accent)
            self.option_add("*Text.selectBackground", accent)
            self.option_add("*Text.selectForeground", "#ffffff")
            self.option_add("*Text.inactiveSelectBackground", "#bfd3fb")
            self.option_add("*Text.padX", 6)
            self.option_add("*Text.padY", 4)
            self.option_add("*Listbox.background", surface)
            self.option_add("*Listbox.relief", "flat")
            self.option_add("*Listbox.highlightThickness", 1)
            self.option_add("*Listbox.highlightBackground", border)
            self.option_add("*Listbox.highlightColor", accent)
            self.option_add("*Listbox.selectBackground", accent)
            self.option_add("*Listbox.selectForeground", "#ffffff")
            self.option_add("*Menu.background", surface)
            self.option_add("*Menu.activeBackground", accent)
            self.option_add("*Menu.activeForeground", "#ffffff")
            self.option_add("*Menu.relief", "flat")
        except Exception:
            pass
        try:
            style.configure(
                ".",
                background=bg,
                foreground=text,
                bordercolor=border,
                focuscolor=accent,
                troughcolor="#e5e9f0",
            )
            style.configure("TFrame", background=bg)
            style.configure("TLabel", background=bg, foreground=text)
            style.configure("TCheckbutton", background=bg, foreground=text)
            style.map("TCheckbutton", background=[("active", bg)], indicatorcolor=[("selected", accent)])
            style.configure("TRadiobutton", background=bg, foreground=text)
            style.map("TRadiobutton", background=[("active", bg)], indicatorcolor=[("selected", accent)])
            style.configure("TLabelframe", background=bg, bordercolor=border)
            style.configure("TLabelframe.Label", background=bg, foreground=text)
            for btn_style in ("TButton", "TMenubutton"):
                style.configure(
                    btn_style,
                    background=surface,
                    foreground=text,
                    bordercolor=border,
                    lightcolor=surface,
                    darkcolor=surface,
                    borderwidth=1,
                    padding=(10, 3),
                    focusthickness=1,
                    focuscolor=accent,
                    arrowcolor=text,
                )
                style.map(
                    btn_style,
                    background=[("disabled", "#eef1f5"), ("pressed", accent_pressed), ("active", accent_soft)],
                    foreground=[("disabled", "#9ca3af")],
                    bordercolor=[("pressed", accent), ("active", accent), ("focus", accent)],
                    lightcolor=[("pressed", accent_pressed), ("active", accent_soft)],
                    darkcolor=[("pressed", accent_pressed), ("active", accent_soft)],
                )
            style.configure(
                "RunAccent.TButton",
                background=success,
                foreground="#ffffff",
                bordercolor=success,
                lightcolor=success,
                darkcolor=success,
                focuscolor="#ffffff",
            )
            style.map(
                "RunAccent.TButton",
                background=[("disabled", "#a7d8b5"), ("pressed", success_active), ("active", success_active)],
                foreground=[("disabled", "#f0fdf4"), ("!disabled", "#ffffff")],
                bordercolor=[("pressed", success_active), ("active", success_active)],
                lightcolor=[("pressed", success_active), ("active", success_active)],
                darkcolor=[("pressed", success_active), ("active", success_active)],
            )
            for field_style in ("TCombobox", "TEntry", "TSpinbox"):
                style.configure(
                    field_style,
                    fieldbackground=surface,
                    background=surface,
                    foreground=text,
                    bordercolor=border,
                    lightcolor=surface,
                    darkcolor=surface,
                    arrowcolor=text,
                    padding=2,
                )
                style.map(
                    field_style,
                    fieldbackground=[("disabled", "#eef1f5"), ("readonly", surface)],
                    foreground=[("disabled", "#9ca3af")],
                    bordercolor=[("focus", accent), ("active", accent)],
                    selectbackground=[("readonly", surface)],
                    selectforeground=[("readonly", text)],
                )
            style.configure("TNotebook", background=bg, borderwidth=0, tabmargins=(2, 4, 2, 0))
            style.configure(
                "TNotebook.Tab",
                background="#e5e9f0",
                foreground=muted,
                bordercolor=border,
                lightcolor="#e5e9f0",
                padding=(14, 5),
            )
            style.map(
                "TNotebook.Tab",
                background=[("selected", surface), ("active", "#eef2f8")],
                foreground=[("selected", accent), ("active", text)],
                lightcolor=[("selected", surface)],
                expand=[("selected", (1, 1, 1, 0))],
            )
            style.configure(
                "Treeview",
                background=surface,
                fieldbackground=surface,
                foreground=text,
                bordercolor=border,
            )
            style.map(
                "Treeview",
                background=[("selected", accent)],
                foreground=[("selected", "#ffffff")],
            )
            style.configure(
                "Treeview.Heading",
                background="#e5e9f0",
                foreground=text,
                bordercolor=border,
                relief="flat",
            )
            style.map("Treeview.Heading", background=[("active", accent_soft)])
            style.configure(
                "TScrollbar",
                background="#d4dbe6",
                troughcolor=bg,
                bordercolor=bg,
                arrowcolor=muted,
                lightcolor="#d4dbe6",
                darkcolor="#d4dbe6",
            )
            style.map("TScrollbar", background=[("active", "#b8c3d4")])
        except Exception:
            pass

    def _build_ui(self) -> None:
        """Build top-level UI layout."""
        style = ttk.Style(self)
        self._apply_modern_theme(style)
        try:
            bold_font = tkfont.nametofont("TkDefaultFont").copy()
            bold_font.configure(weight="bold")
            style.configure("TopbarBold.TButton", font=bold_font)
            style.configure("TopbarBold.TMenubutton", font=bold_font)
            style.configure("RunAccent.TButton", font=bold_font, padding=(16, 3))
        except Exception:
            pass
        # Use a root-level grid to keep the bottom status bar at a fixed height.
        # The notebook row is the only row that expands/shrinks with the window.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(3, weight=0, minsize=32)
        top = ttk. Frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        top.grid_columnconfigure(0, weight=1)
        self.topbar = ttk. Frame(top)
        self.topbar.grid(row=0, column=0, sticky="ew")
        self._topbar_items = []

        # Project menu
        project_btn = ttk. Menubutton(self.topbar, text="Project", style="TopbarBold.TMenubutton")
        project_menu = tk.Menu(project_btn, tearoff=False)
        project_btn["menu"] = project_menu
        project_menu.add_command(label="New project", command=self.on_new_project)
        project_menu.add_separator()
        project_menu.add_command(label="Open project...", command=self.on_open_project)
        project_menu.add_command(label="Save project", command=self.on_save_project)
        project_menu.add_command(label="Save project as...", command=self.on_save_project_as)
        project_menu.add_command(label="Merge project into current...", command=self.on_merge_project)
        project_menu.add_separator()
        project_menu.add_command(label="Import data (replace project)...", command=self.on_import_data_replace)
        project_menu.add_command(label="Add data to project...", command=self.on_add_data_to_project)
        project_menu.add_command(label="Import from ITRDB (online)...", command=self.on_import_from_itrdb)
        project_menu.add_command(label="Import pith info (.pos / .pith.csv)...", command=self.on_import_pith_files)
        project_menu.add_command(label="Import pith info from folder...", command=self.on_import_pith_folder)
        project_menu.add_command(label="Import climate (CSV)...", command=self.on_load_climate)
        project_menu.add_separator()
        project_menu.add_command(label="Choose export folder...", command=self.on_choose_output_dir)
        project_menu.add_separator()
        project_menu.add_command(label="Exit", command=self.on_close)
        self._register_topbar_widget(project_btn, pad_x=0, pad_y=0, row_hint=0)

        btn_add_to_project = ttk.Button(
            self.topbar,
            text="Add data",
            command=self.on_add_data_to_project,
            style="TopbarBold.TButton",
        )
        self._register_topbar_widget(btn_add_to_project, pad_x=0, pad_y=0, row_hint=0)

        lbl_dataset = ttk.Label(self.topbar, text="Dataset:")
        self._register_topbar_widget(lbl_dataset, pad_x=1, pad_y=0, row_hint=0)
        self.dataset_combo = ttk.Combobox(
            self.topbar,
            textvariable=self.dataset_var,
            values=[],
            width=18,
            state="readonly",
        )
        self.dataset_combo.bind("<<ComboboxSelected>>", lambda _: self.on_dataset_selected())
        self._register_topbar_widget(self.dataset_combo, pad_x=0, pad_y=0, row_hint=0)
        _attach_hover_tooltip(lbl_dataset, "Select the active dataset in the current project.")
        _attach_hover_tooltip(self.dataset_combo, "Select the active dataset in the current project.")

        lbl_goal = ttk.Label(self.topbar, text="Goal:")
        self._register_topbar_widget(lbl_goal, pad_x=1, pad_y=0, row_hint=0)
        goal_combo = ttk.Combobox(
            self.topbar,
            textvariable=self.goal_var,
            values=[
                "highfreq",
                "mixed",
                "lowfreq",
                "temp_recon",
                "runoff_recon",
                "drought_recon",
                "variance_check",
            ],
            width=12,
            state="readonly",
        )
        goal_combo.bind("<<ComboboxSelected>>", lambda _: self._on_goal_changed())
        self._register_topbar_widget(goal_combo, pad_x=0, pad_y=0, row_hint=0)
        _attach_hover_tooltip(
            lbl_goal,
            "Choose the analysis goal used to score and recommend detrending methods."
        )
        _attach_hover_tooltip(
            goal_combo,
            "Choose the analysis goal used to score and recommend detrending methods."
        )

        btn_score_settings = ttk.Button(
            self.topbar,
            text="Score settings...",
            command=self._show_score_settings_dialog,
        )
        self._register_topbar_widget(btn_score_settings, pad_x=1, pad_y=0, row_hint=0)


        lbl_var = ttk.Label(self.topbar, text="Type:")
        self._register_topbar_widget(lbl_var, pad_x=1, pad_y=0, row_hint=1)
        var_entry = ttk.Entry(self.topbar, textvariable=self.var_type_var, width=6)
        self._register_topbar_widget(var_entry, pad_x=0, pad_y=0, row_hint=1)
        _attach_hover_tooltip(lbl_var, "Edit the dataset type label, for example TRW or BI.")
        _attach_hover_tooltip(var_entry, "Edit the dataset type label, for example TRW or BI.")

        # Backend controls are intentionally hidden in the top bar for now.
        # The current build stays on dplR only.
        # lbl_backend = ttk.Label(self.topbar, text="Backend:")
        # self._register_topbar_widget(lbl_backend, pad_x=8, pad_y=2, row_hint=1)
        # lbl_backend_value = ttk.Label(self.topbar, text="dplR only")
        # self._register_topbar_widget(lbl_backend_value, pad_x=2, pad_y=2, row_hint=1)

        lbl_workers = ttk.Label(self.topbar, text="R workers:")
        self._register_topbar_widget(lbl_workers, pad_x=1, pad_y=0, row_hint=1)
        self.combo_dplr_workers = ttk.Combobox(
            self.topbar,
            textvariable=self.dplr_parallel_workers_var,
            values=["Auto", "1", "2", "3", "4", "6", "8", "10", "12", "16"],
            width=5,
            state="readonly",
        )
        self.combo_dplr_workers.bind("<<ComboboxSelected>>", lambda _: self._on_compat_mode_changed())
        self._register_topbar_widget(self.combo_dplr_workers, pad_x=0, pad_y=0, row_hint=1)
        _attach_hover_tooltip(lbl_workers, "Choose how many R workers dplR may use during analysis.")
        _attach_hover_tooltip(self.combo_dplr_workers, "Choose how many R workers dplR may use during analysis.")

        lbl_min_series = ttk.Label(self.topbar, text="Min count:")
        self._register_topbar_widget(lbl_min_series, pad_x=1, pad_y=0, row_hint=1)
        self.combo_min_series_for_detrending = ttk.Combobox(
            self.topbar,
            textvariable=self.min_series_for_detrending_var,
            values=["1", "2", "3", "4", "5", "6", "8", "10", "12", "15", "20"],
            width=5,
            state="readonly",
        )
        self.combo_min_series_for_detrending.bind(
            "<<ComboboxSelected>>",
            lambda _: self._on_min_series_threshold_changed(),
        )
        self._register_topbar_widget(self.combo_min_series_for_detrending, pad_x=0, pad_y=0, row_hint=1)
        _attach_hover_tooltip(
            lbl_min_series,
            "Set the minimum number of series or trees required before detrending begins."
        )
        _attach_hover_tooltip(
            self.combo_min_series_for_detrending,
            "Set the minimum number of series or trees required before detrending begins."
        )

        self.count_by_group = ttk.Frame(self.topbar)
        self._register_topbar_widget(self.count_by_group, pad_x=1, pad_y=0, row_hint=1)
        lbl_count_by = ttk.Label(self.count_by_group, text="Count by:")
        lbl_count_by.pack(side=tk.TOP, anchor="w")
        count_by_row = ttk.Frame(self.count_by_group)
        count_by_row.pack(side=tk.TOP, anchor="w")
        self.combo_min_series_count_mode = ttk.Combobox(
            count_by_row,
            textvariable=self.min_series_count_mode_var,
            values=["series", "trees"],
            width=7,
            state="readonly",
        )
        self.combo_min_series_count_mode.bind(
            "<<ComboboxSelected>>",
            lambda _: self._on_min_series_threshold_changed(),
        )
        self.combo_min_series_count_mode.pack(side=tk.LEFT, anchor="w")

        self.lbl_min_series_info = ttk.Label(
            self.count_by_group,
            textvariable=self.min_series_info_var,
            anchor="w",
        )
        self.lbl_min_series_info.pack(side=tk.TOP, anchor="w", pady=(0, 0))
        _attach_hover_tooltip(
            lbl_count_by,
            "Choose whether the minimum detrending threshold is counted by series or by trees."
        )
        _attach_hover_tooltip(
            self.combo_min_series_count_mode,
            "Choose whether the minimum detrending threshold is counted by series or by trees."
        )
        _attach_hover_tooltip(
            self.lbl_min_series_info,
            "Shows the first year that meets the current minimum count threshold."
        )

        self.tree_groups_group = ttk.Frame(self.topbar)
        self._register_topbar_widget(self.tree_groups_group, pad_x=0, pad_y=0, row_hint=1)
        self.chk_enable_tree_grouping = ttk.Checkbutton(
            self.tree_groups_group,
            text="Tree groups",
            variable=self.enable_tree_grouping_var,
            command=self._on_tree_grouping_toggle_changed,
        )
        self.chk_enable_tree_grouping.pack(side=tk.LEFT, anchor="w")
        self.btn_tree_groups = ttk.Button(
            self.tree_groups_group,
            text="...",
            width=3,
            command=self.on_edit_tree_groups,
        )
        self.btn_tree_groups.pack(side=tk.LEFT, padx=(4, 0))
        _attach_hover_tooltip(
            self.chk_enable_tree_grouping,
            "Enable automatic grouping of multiple series into trees. When off, each series is treated as its own tree."
        )
        _attach_hover_tooltip(
            self.btn_tree_groups,
            "Edit automatic tree-grouping rules and review grouped series."
        )

        self.chk_dplr_powt = ttk.Checkbutton(
            self.topbar,
            text="POWT",
            variable=self.enable_dplr_powt_var,
            command=self._on_compat_mode_changed,
        )
        self._register_topbar_widget(self.chk_dplr_powt, pad_x=1, pad_y=0, row_hint=2)

        self.combo_dplr_powt_method = ttk.Combobox(
            self.topbar,
            textvariable=self.dplr_powt_method_var,
            values=["universal", "cook"],
            width=10,
            state="readonly",
        )
        self.combo_dplr_powt_method.bind("<<ComboboxSelected>>", self._on_powt_main_controls_changed)
        self._register_topbar_widget(self.combo_dplr_powt_method, pad_x=0, pad_y=0, row_hint=2)
        _attach_hover_tooltip(
            self.combo_dplr_powt_method,
            "Choose the POWT method used when POWT is enabled."
        )

        self.chk_export_both_powt = ttk.Checkbutton(
            self.topbar,
            text="Export POWT+NoPOWT",
            variable=self.export_both_powt_modes_var,
        )
        self._register_topbar_widget(self.chk_export_both_powt, pad_x=0, pad_y=0, row_hint=2)

        self.chk_dplr_ssf = ttk.Checkbutton(
            self.topbar,
            text="SSF",
            variable=self.enable_dplr_ssf_var,
            command=self._on_compat_mode_changed,
        )
        self._register_topbar_widget(self.chk_dplr_ssf, pad_x=1, pad_y=0, row_hint=0)
        self.chk_dplr_rcs_sf = ttk.Checkbutton(
            self.topbar,
            text="SF-RCS",
            variable=self.enable_dplr_rcs_sf_var,
            command=self._on_compat_mode_changed,
        )
        self._register_topbar_widget(self.chk_dplr_rcs_sf, pad_x=0, pad_y=0, row_hint=0)

        custom_btn = ttk.Button(self.topbar, text="Custom methods...", command=self.on_custom_methods)
        self._register_topbar_widget(custom_btn, pad_x=1, pad_y=0, row_hint=2)
        _attach_hover_tooltip(custom_btn, "Add spline, signal-free spline or running mean periods alongside standard methods.")

        self.btn_run = ttk.Button(
            self.topbar,
            text="RUN",
            command=self.on_run,
            style="RunAccent.TButton",
        )
        self._register_topbar_widget(self.btn_run, pad_x=1, pad_y=0, row_hint=0)

        actions_btn = ttk.Menubutton(self.topbar, text="Actions")
        actions_menu = tk.Menu(actions_btn, tearoff=False)
        actions_btn["menu"] = actions_menu
        actions_menu.add_command(label="Export RWI (all series)", command=self.on_export_current_rwi_matrix)
        actions_menu.add_command(label="Export project outputs", command=self.on_export)
        actions_menu.add_command(label="Open last export folder", command=self.open_last_export_folder)
        actions_menu.add_separator()
        actions_menu.add_command(label="Open RingClim", command=self.on_open_ringclim)
        self._register_topbar_widget(actions_btn, pad_x=0, pad_y=0, row_hint=2)

        self.topbar.bind("<Configure>", self._schedule_topbar_reflow, add="+")
        self.bind("<Configure>", self._schedule_topbar_reflow, add="+")
        self._on_compat_mode_changed()
        self._schedule_topbar_reflow()

        self._on_compat_mode_changed()

        status_row = ttk. Frame(self)
        status_row.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        status = ttk. Label(status_row, textvariable=self.status_var, anchor="w")
        status.pack(side=tk. LEFT, fill=tk. X, expand=True)
        self._last_export_path: Optional[str] = None
        self.lbl_open_export = tk. Label(status_row, text="", fg="#1a56db", cursor="hand2",
                                         font=("Segoe UI", 9, "underline"))
        self.lbl_open_export.pack(side=tk. RIGHT)
        self.lbl_open_export.bind("<Button-1>", lambda _e: self.open_last_export_folder())
        self.notebook = ttk. Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=5, pady=5)
        # Bottom status bar (fixed height): signature + Help/About
        status_bg = "#e9edf4"
        status_bar = tk. Frame(self, bd=0, relief="flat", background=status_bg)
        status_bar.grid(row=3, column=0, sticky="ew")
        status_bar.grid_propagate(False)
        lbl_created = tk. Label(
            status_bar,
            text=f"{CREATED_BY_LABEL} Dr. Zulfiyor Bakhtiyorov.",
            font=("Arial", 10, "bold"),
            anchor="w",
            background=status_bg,
            foreground="#1f2937",
        )
        lbl_created.pack(side="left", padx=8, pady=2, fill="x", expand=True)
        btn_help = ttk.Button(status_bar, text="Help", command=self._show_help)
        btn_help.pack(side="right", padx=4, pady=2)
        btn_cite = ttk.Button(status_bar, text="Cite", command=self._show_cite)
        btn_cite.pack(side="right", padx=4, pady=2)
        btn_about = ttk.Button(status_bar, text="About", command=self._show_about)
        btn_about.pack(side="right", padx=(4, 8), pady=2)
        btn_updates = ttk.Button(status_bar, text="Updates")
        btn_updates.pack(side="right", padx=4, pady=2)
        self.updater = None
        if _appupd_tk is not None:
            try:
                self.updater = _appupd_tk.TkUpdater(self, button=btn_updates, close=self.on_close)
            except Exception as exc:  # pragma: no cover
                print("Updater not available:", exc)
                btn_updates.state(["disabled"])
        else:
            btn_updates.state(["disabled"])
        # UI scale spinbox
        tk.Label(
            status_bar,
            text="UI scale:",
            font=("Arial", 9),
            background=status_bg,
            foreground="#1f2937",
        ).pack(
            side="right", padx=(12, 2), pady=4
        )
        self._spin_ui_scale_var = tk.DoubleVar(value=1.0)
        self._spin_ui_scale = tk.Spinbox(
            status_bar,
            textvariable=self._spin_ui_scale_var,
            from_=0.60,
            to=2.00,
            increment=0.05,
            format="%.2f",
            width=5,
            command=self._on_ui_scale_entry,
        )
        self._spin_ui_scale.pack(side="right", padx=(0, 4), pady=4)
        self._spin_ui_scale.bind("<Return>", self._on_ui_scale_entry)
        self._spin_ui_scale.bind("<FocusOut>", self._on_ui_scale_entry)
        # Tab 1: recommendation text
        self.tab_recommend = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_recommend, text="Recommendation")
        self.text_recommend = tk. Text(self.tab_recommend, wrap="word")
        self.text_recommend.pack(side=tk. LEFT, fill=tk. BOTH, expand=True)
        scroll_rec = ttk. Scrollbar(
            self.tab_recommend, command=self.text_recommend.yview
        )
        scroll_rec.pack(side=tk. RIGHT, fill=tk. Y)
        self.text_recommend.config(yscrollcommand=scroll_rec.set)
        self._make_text_readonly(self.text_recommend)
        # Tab 1b: runtime log
        self.tab_runtime = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_runtime, text="Runtime log")
        self.text_runtime_log = tk.Text(self.tab_runtime, wrap="word")
        self.text_runtime_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_runtime = ttk.Scrollbar(self.tab_runtime, command=self.text_runtime_log.yview)
        scroll_runtime.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_runtime_log.config(yscrollcommand=scroll_runtime.set)
        self._make_text_readonly(self.text_runtime_log)
        # Tab 1c: POWT (dplR power transformation workspace)
        self.tab_powt = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_powt, text="POWT")
        powt_top = ttk.Frame(self.tab_powt)
        powt_top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 2))
        ttk.Label(
            powt_top,
            text="POWT preview and POWT-based advisor results are driven automatically by the main POWT toggle.",
        ).pack(side=tk.LEFT, padx=(0, 8))

        powt_body = ttk.Panedwindow(self.tab_powt, orient=tk.VERTICAL)
        powt_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(4, 6))

        powt_plot_frame = ttk.Frame(powt_body)
        self.fig_powt = plt.Figure(figsize=(6, 6))
        self.canvas_powt = FigureCanvasTkAgg(self.fig_powt, master=powt_plot_frame)
        _add_plot_toolbar(self.canvas_powt, powt_plot_frame)
        self.canvas_powt.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        powt_body.add(powt_plot_frame, weight=4)

        powt_text_frame = ttk.Frame(powt_body)
        powt_text_frame.columnconfigure(0, weight=1)
        powt_text_frame.columnconfigure(1, weight=1)
        powt_text_frame.rowconfigure(1, weight=1)

        ttk.Label(powt_text_frame, text="Universal", font=("Arial", 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 4)
        )
        ttk.Label(powt_text_frame, text="Cook", font=("Arial", 10, "bold")).grid(
            row=0, column=1, sticky="w", padx=(6, 0), pady=(0, 4)
        )

        left_frame = ttk.Frame(powt_text_frame)
        left_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.text_powt_summary_universal = tk.Text(left_frame, wrap="word", height=10)
        self.text_powt_summary_universal.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        powt_scroll_left = ttk.Scrollbar(
            left_frame, command=self.text_powt_summary_universal.yview
        )
        powt_scroll_left.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_powt_summary_universal.config(yscrollcommand=powt_scroll_left.set)

        right_frame = ttk.Frame(powt_text_frame)
        right_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        self.text_powt_summary_cook = tk.Text(right_frame, wrap="word", height=10)
        self.text_powt_summary_cook.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        powt_scroll_right = ttk.Scrollbar(
            right_frame, command=self.text_powt_summary_cook.yview
        )
        powt_scroll_right.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_powt_summary_cook.config(yscrollcommand=powt_scroll_right.set)

        powt_body.add(powt_text_frame, weight=2)
        # Tab 2: age & depth plots
        self.tab_structure = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_structure, text="Data structure")
        self.fig_structure, (self.ax_age_hist, self.ax_age_curve, self.ax_depth) = plt.subplots(
            3, 1, figsize=(6, 8)
        )
        self.fig_structure.tight_layout()
        self.canvas_structure = FigureCanvasTkAgg(
            self.fig_structure, master=self.tab_structure
        )
        _add_plot_toolbar(self.canvas_structure, self.tab_structure)
        self.canvas_structure.get_tk_widget().pack(
            side=tk. TOP, fill=tk. BOTH, expand=True
        )
        # Tab 3: chronologies
        self.tab_chron = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_chron, text="Chronologies")
        self.fig_chron, self.ax_chron = plt.subplots(figsize=(6, 4))
        self.canvas_chron = FigureCanvasTkAgg(
            self.fig_chron, master=self.tab_chron
        )
        _add_plot_toolbar(self.canvas_chron, self.tab_chron)
        self.canvas_chron.get_tk_widget().pack(
            side=tk. TOP, fill=tk. BOTH, expand=True
        )
        bottom = ttk. Frame(self.tab_chron)
        bottom.pack(side=tk. TOP, fill=tk. X)
        ttk. Label(bottom, text="View:").pack(side=tk. LEFT, padx=2)
        self.chron_view_mode = tk. StringVar(value="best_vs_all")
        self.chron_method_var = tk. StringVar(value="best")
        view_combo = ttk. Combobox(
            bottom,
            textvariable=self.chron_view_mode,
            values=["best_vs_all", "single_method", "rcs_vs_rcs_sf", "all_series"],
            state="readonly",
            width=16,
        )
        view_combo.pack(side=tk. LEFT, padx=2)
        view_combo.bind("<<ComboboxSelected>>", lambda _: self.update_chron_plots())
        ttk. Label(bottom, text="Method:").pack(side=tk. LEFT, padx=(10, 2))
        self.chron_method_combo = ttk. Combobox(
            bottom,
            textvariable=self.chron_method_var,
            values=["best"],
            state="readonly",
            width=16,
        )
        self.chron_method_combo.pack(side=tk. LEFT, padx=2)
        self.chron_method_combo.bind(
            "<<ComboboxSelected>>", lambda _: self.update_chron_plots()
        )
        ttk. Label(bottom, text="Series:").pack(side=tk. LEFT, padx=(10, 2))
        self.chron_series_var = tk. StringVar(value="")
        self.chron_series_combo = ttk. Combobox(
            bottom, textvariable=self.chron_series_var, values=[], state="readonly", width=16
        )
        self.chron_series_combo.pack(side=tk. LEFT, padx=2)
        self.chron_series_combo.bind("<<ComboboxSelected>>",
                                     lambda _: self._select_chron_series(self.chron_series_var.get()))
        ttk. Button(bottom, text="Clear", width=6,
                    command=lambda: self._select_chron_series(None)).pack(side=tk. LEFT, padx=2)
        self.chron_info_var = tk. StringVar(
            value="View all_series: click a series (or choose it above) to inspect it; hover shows year and value."
        )
        info = ttk. Label(self.tab_chron, textvariable=self.chron_info_var, justify="left",
                          anchor="w", wraplength=1400, foreground="#1F3864")
        info.pack(side=tk. TOP, fill=tk. X, padx=6, pady=(2, 4))
        self._chron_selected: Optional[str] = None
        self._chron_series_lines: Dict[str, Any] = {}
        self._chron_hover_annot = None
        self._chron_hover_key = None
        try:
            self.canvas_chron.mpl_connect("pick_event", self._on_chron_pick)
            self.canvas_chron.mpl_connect("motion_notify_event", self._on_chron_hover)
        except Exception:
            pass
        # Tab 4: Compare / Correlation
        self.tab_compare = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_compare, text="Compare / Correlation")

        comp_top = ttk. Frame(self.tab_compare)
        comp_top.pack(side=tk. TOP, fill=tk. X, padx=6, pady=(6, 2))

        ttk. Label(comp_top, text="Mode:").pack(side=tk. LEFT, padx=(0, 6))
        self.compare_mode_combo = ttk. Combobox(
            comp_top,
            textvariable=self.compare_mode_var,
            values=["selected_method", "all_methods"],
            state="readonly",
            width=16,
        )
        self.compare_mode_combo.pack(side=tk. LEFT)
        self.compare_mode_combo.bind("<<ComboboxSelected>>", lambda _: self._on_compare_mode_changed())

        ttk. Label(comp_top, text="Method:").pack(side=tk. LEFT, padx=(10, 6))
        self.compare_method_combo = ttk. Combobox(
            comp_top,
            textvariable=self.compare_method_var,
            values=["best"],
            state="readonly",
            width=16,
        )
        self.compare_method_combo.pack(side=tk. LEFT)
        self.compare_method_combo.bind("<<ComboboxSelected>>", lambda _: self.update_compare_plots())

        ttk. Label(comp_top, text="Min overlap (years):").pack(side=tk. LEFT, padx=(10, 6))
        self.compare_overlap_spin = ttk. Spinbox(
            comp_top,
            from_=2,
            to=500,
            textvariable=self.compare_min_overlap_var,
            width=6,
        )
        self.compare_overlap_spin.pack(side=tk. LEFT)

        ttk. Button(comp_top, text="Update", command=self.update_compare_plots).pack(
            side=tk. LEFT, padx=(10, 0)
        )

        comp_body = ttk. Frame(self.tab_compare)
        comp_body.pack(side=tk. TOP, fill=tk. BOTH, expand=True)

        comp_left = ttk. Frame(comp_body)
        comp_left.pack(side=tk. LEFT, fill=tk. Y, padx=(6, 6), pady=(0, 6))
        ttk. Label(comp_left, text="Datasets:").pack(anchor="w")

        lb_frame = ttk. Frame(comp_left)
        lb_frame.pack(fill=tk. BOTH, expand=True)

        self.compare_listbox = tk. Listbox(
            lb_frame,
            selectmode="extended",
            height=12,
            exportselection=False,
        )
        self.compare_listbox.pack(side=tk. LEFT, fill=tk. BOTH, expand=True)
        scr_cmp = ttk. Scrollbar(lb_frame, command=self.compare_listbox.yview)
        scr_cmp.pack(side=tk. RIGHT, fill=tk. Y)
        self.compare_listbox.config(yscrollcommand=scr_cmp.set)
        self.compare_listbox.bind("<<ListboxSelect>>", lambda _: self.update_compare_plots())

        cmp_btns = ttk. Frame(comp_left)
        cmp_btns.pack(fill=tk. X, pady=(6, 0))
        ttk. Button(cmp_btns, text="Select all", command=self._compare_select_all).pack(
            side=tk. LEFT, padx=(0, 6)
        )
        ttk. Button(cmp_btns, text="Clear", command=self._compare_clear_selection).pack(
            side=tk. LEFT
        )

        comp_right = ttk. Frame(comp_body)
        comp_right.pack(side=tk. LEFT, fill=tk. BOTH, expand=True, padx=(0, 6), pady=(0, 6))

        self.fig_compare, (self.ax_compare_overlay, self.ax_compare_corr) = plt.subplots(
            2, 1, figsize=(7, 6)
        )
        # Reserve space on the right for an external legend and a fixed colorbar axis.
        self.fig_compare.subplots_adjust(left=0.08, right=0.76, top=0.95, bottom=0.08, hspace=0.45)
        self.ax_compare_cbar = self.fig_compare.add_axes([0.80, 0.11, 0.03, 0.33])
        # Cache the original axes geometry so repeated updates never shrink the plots.
        self._compare_overlay_pos = self.ax_compare_overlay.get_position().bounds
        self._compare_corr_pos = self.ax_compare_corr.get_position().bounds
        self._compare_cbar_pos = self.ax_compare_cbar.get_position().bounds
        self.canvas_compare = FigureCanvasTkAgg(self.fig_compare, master=comp_right)
        _add_plot_toolbar(self.canvas_compare, comp_right)
        self.canvas_compare.get_tk_widget().pack(side=tk. TOP, fill=tk. BOTH, expand=True)

        self._on_compare_mode_changed()

        # Tab: Climate correlation of detrended chronologies
        self.tab_climate = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_climate, text="Climate")
        clim_top = ttk. Frame(self.tab_climate)
        clim_top.pack(side=tk. TOP, fill=tk. X, padx=6, pady=(6, 2))
        self.climate_method_var = tk.StringVar(value="best")
        self.climate_chron_type_var = tk.StringVar(value="STD")
        self.climate_window_var = tk.StringVar(value="31")
        ttk. Label(clim_top, text="Method:").pack(side=tk. LEFT, padx=(0, 6))
        self.climate_method_combo = ttk. Combobox(
            clim_top, textvariable=self.climate_method_var, values=["best"], state="readonly", width=18
        )
        self.climate_method_combo.pack(side=tk. LEFT)
        self.climate_method_combo.bind("<<ComboboxSelected>>", lambda _: self.update_climate_plots())
        ttk. Label(clim_top, text="Chronology:").pack(side=tk. LEFT, padx=(12, 6))
        clim_type = ttk. Combobox(
            clim_top, textvariable=self.climate_chron_type_var, values=["STD", "RES"], state="readonly", width=6
        )
        clim_type.pack(side=tk. LEFT)
        clim_type.bind("<<ComboboxSelected>>", lambda _: self.update_climate_plots())
        ttk. Label(clim_top, text="Running window (years):").pack(side=tk. LEFT, padx=(12, 6))
        ttk. Spinbox(clim_top, from_=11, to=101, increment=2, textvariable=self.climate_window_var, width=5).pack(
            side=tk. LEFT
        )
        ttk. Button(clim_top, text="Update", command=self.update_climate_plots).pack(side=tk. LEFT, padx=(10, 0))
        self.fig_climate = plt.Figure(figsize=(7, 6))
        self.canvas_climate = FigureCanvasTkAgg(self.fig_climate, master=self.tab_climate)
        _add_plot_toolbar(self.canvas_climate, self.tab_climate)
        self.canvas_climate.get_tk_widget().pack(side=tk. TOP, fill=tk. BOTH, expand=True)
        self.notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _e: self.update_climate_plots() if self.notebook.select() == str(self.tab_climate) else None,
            add="+",
        )

        # Tab 5: EPS / Rbar / Depth
        self.tab_signal = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_signal, text="EPS / Rbar / Depth")
        sig_top = ttk. Frame(self.tab_signal)
        sig_top.pack(side=tk. TOP, fill=tk. X, padx=6, pady=(6, 2))
        ttk. Label(sig_top, text="Method:").pack(side=tk. LEFT, padx=(0, 6))
        self.signal_method_combo = ttk. Combobox(
            sig_top,
            textvariable=self.signal_method_var,
            values=["best"],
            state="readonly",
            width=16,
        )
        self.signal_method_combo.pack(side=tk. LEFT)
        self.signal_method_combo.bind("<<ComboboxSelected>>", lambda _: self._update_signal_plots())
        ttk. Label(sig_top, text="Window:").pack(side=tk. LEFT, padx=(12, 6))
        self.signal_window_spin = ttk. Spinbox(
            sig_top,
            from_=3,
            to=500,
            textvariable=self.signal_window_var,
            width=6,
        )
        self.signal_window_spin.pack(side=tk. LEFT)
        ttk. Button(sig_top, text="Update", command=self._update_signal_plots).pack(
            side=tk. LEFT, padx=(10, 0)
        )
        self.fig_signal, self.ax_signal = plt.subplots(figsize=(6, 4))
        self.canvas_signal = FigureCanvasTkAgg(self.fig_signal, master=self.tab_signal)
        _add_plot_toolbar(self.canvas_signal, self.tab_signal)
        self.canvas_signal.get_tk_widget().pack(side=tk. TOP, fill=tk. BOTH, expand=True, padx=0, pady=(2, 0))
        # Tab 5: Guide (methods, goals, and selection logic)
        self.tab_guide = ttk. Frame(self.notebook)
        self.notebook.add(self.tab_guide, text="Guide")
        guide_root = ttk. Frame(self.tab_guide, padding=6)
        guide_root.pack(side=tk. TOP, fill=tk. BOTH, expand=True)
        # Method guide
        lf_method = ttk. LabelFrame(guide_root, text="Detrending methods")
        lf_method.pack(side=tk. TOP, fill=tk. BOTH, expand=True, pady=(0, 8))
        top_m = ttk. Frame(lf_method)
        top_m.pack(side=tk. TOP, fill=tk. X, padx=6, pady=(6, 2))
        ttk. Label(top_m, text="Method:").pack(side=tk. LEFT)
        self.guide_method_combo = ttk. Combobox(
            top_m,
            textvariable=self.guide_method_var,
            values=sorted(METHOD_EXPLANATIONS.keys()),
            state="readonly",
            width=18,
        )
        self.guide_method_combo.pack(side=tk. LEFT, padx=(6, 0))
        self.guide_method_combo.bind("<<ComboboxSelected>>", lambda _: self._update_method_guide_text())
        self.text_method_guide = tk. Text(lf_method, wrap="word", height=10)
        self.text_method_guide.pack(side=tk. LEFT, fill=tk. BOTH, expand=True, padx=(6, 0), pady=(0, 6))
        scr_m = ttk. Scrollbar(lf_method, command=self.text_method_guide.yview)
        scr_m.pack(side=tk. RIGHT, fill=tk. Y, padx=(0, 6), pady=(0, 6))
        self.text_method_guide.config(yscrollcommand=scr_m.set)
        # Goal guide
        lf_goal = ttk. LabelFrame(guide_root, text="Goals")
        lf_goal.pack(side=tk. TOP, fill=tk. BOTH, expand=True, pady=(0, 8))
        top_g = ttk. Frame(lf_goal)
        top_g.pack(side=tk. TOP, fill=tk. X, padx=6, pady=(6, 2))
        ttk. Label(top_g, text="Goal:").pack(side=tk. LEFT)
        self.guide_goal_combo = ttk. Combobox(
            top_g,
            textvariable=self.guide_goal_var,
            values=list(GOAL_LABELS.keys()),
            state="readonly",
            width=18,
        )
        self.guide_goal_combo.pack(side=tk. LEFT, padx=(6, 0))
        self.guide_goal_combo.bind("<<ComboboxSelected>>", lambda _: self._update_goal_guide_text())
        self.text_goal_guide = tk. Text(lf_goal, wrap="word", height=8)
        self.text_goal_guide.pack(side=tk. LEFT, fill=tk. BOTH, expand=True, padx=(6, 0), pady=(0, 6))
        scr_g = ttk. Scrollbar(lf_goal, command=self.text_goal_guide.yview)
        scr_g.pack(side=tk. RIGHT, fill=tk. Y, padx=(0, 6), pady=(0, 6))
        self.text_goal_guide.config(yscrollcommand=scr_g.set)
        # Selection logic guide
        lf_sel = ttk. LabelFrame(guide_root, text="How the advisor selects the best method")
        lf_sel.pack(side=tk. TOP, fill=tk. BOTH, expand=True)
        self.text_select_guide = tk. Text(lf_sel, wrap="word", height=8)
        self.text_select_guide.pack(side=tk. LEFT, fill=tk. BOTH, expand=True, padx=(6, 0), pady=(6, 6))
        scr_s = ttk. Scrollbar(lf_sel, command=self.text_select_guide.yview)
        scr_s.pack(side=tk. RIGHT, fill=tk. Y, padx=(0, 6), pady=(6, 6))
        self.text_select_guide.config(yscrollcommand=scr_s.set)
        self._update_guide_texts()

    def _make_dataset_key(self, path: str) -> str:
        """Return a unique dataset key for the given path."""
        base = os.path.splitext(os.path.basename(path))[0].strip() or "dataset"
        key = base
        i = 2
        while key in self.datasets:
            key = f"{base}_{i}"
            i += 1
        return key

    def _build_ids_for_series_names(
        self,
        series_names: list[str],
        rwl: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Build ids using either identity mapping or automatic tree grouping.

        ``rwl`` is the ring-width table of the series being grouped (needed for
        the year-overlap guard and the ring-width refinement); without it the
        active dataset is used, as before.
        """
        names = [str(x) for x in list(series_names)]
        if bool(self.enable_tree_grouping_var.get()):
            if rwl is None:
                rwl = (
                    getattr(self.datasets.get(self.active_dataset_key), "rwl", None)
                    if self.active_dataset_key and self.active_dataset_key in self.datasets
                    else None
                )
            ids_auto = build_auto_tree_ids(
                names,
                suffix_variant_lines=self._get_radius_suffix_variant_lines(),
                group_pattern_lines=self._get_series_group_pattern_lines(),
                manual_prefix_lines=self._get_manual_tree_prefix_lines(),
                rwl=rwl,
            )
            return _refine_auto_ids_with_ring_widths(ids_auto, rwl)
        return build_identity_tree_ids(names)

    def _apply_tree_grouping_mode_to_dataset(self, ds: Any) -> None:
        """Rebuild one dataset ids table according to the current tree-grouping mode."""
        if ds is None or getattr(ds, "rwl", None) is None:
            return
        if bool(self.enable_tree_grouping_var.get()):
            ds.ids = _refine_auto_ids_with_ring_widths(
                build_auto_tree_ids(
                    [str(x) for x in list(ds.rwl.columns)],
                    suffix_variant_lines=self._get_radius_suffix_variant_lines(),
                    group_pattern_lines=self._get_series_group_pattern_lines(),
                    manual_prefix_lines=self._get_manual_tree_prefix_lines(),
                    rwl=ds.rwl,
                ),
                ds.rwl,
            )
        else:
            ds.ids = build_identity_tree_ids([str(x) for x in list(ds.rwl.columns)])
        ds.ids_source = "auto" if bool(self.enable_tree_grouping_var.get()) else "identity"

    def _on_tree_grouping_toggle_changed(self) -> None:
        """Apply the current tree-grouping mode to datasets that are still auto-managed."""
        for ds in self.datasets.values():
            if ds is None or getattr(ds, "rwl", None) is None:
                continue
            if bool(getattr(ds, "ids_reviewed", False)):
                continue
            self._apply_tree_grouping_mode_to_dataset(ds)
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            ds_active = self.datasets.get(self.active_dataset_key)
            if ds_active is not None:
                self.ids = getattr(ds_active, "ids", None)
        self._update_min_series_info_for_active_dataset()
        self._mark_project_dirty()
        self._bump_project_version()

    def _ensure_dataset_ids_initialized(self, ds: Any) -> None:
        """Ensure one dataset has a usable ids mapping table."""
        if ds is None or getattr(ds, "rwl", None) is None:
            return
        series_names = [str(x) for x in list(ds.rwl.columns)]
        ids_obj = getattr(ds, "ids", None)
        if ids_obj is None or not isinstance(ids_obj, pd.DataFrame) or ids_obj.empty:
            ds.ids = build_identity_tree_ids(series_names)
            ds.ids_source = "identity"
            if not hasattr(ds, "ids_reviewed"):
                ds.ids_reviewed = False
            return
        ds.ids = finalize_tree_ids_mapping(ids_obj, series_names=series_names)
        if not hasattr(ds, "ids_source"):
            ds.ids_source = "manual"
        if not hasattr(ds, "ids_reviewed"):
            ds.ids_reviewed = False

    def _get_min_series_count_mode(self) -> str:
        """Return whether the threshold counts series or trees."""
        raw = str(self.min_series_count_mode_var.get()).strip().lower()
        if raw not in ("series", "trees"):
            raw = "series"
            self.min_series_count_mode_var.set(raw)
        return raw

    def _get_active_dataset_ids(self) -> Optional[pd.DataFrame]:
        """Return ids mapping for the active dataset, if available."""
        if not self.active_dataset_key or self.active_dataset_key not in self.datasets:
            return None
        ds = self.datasets.get(self.active_dataset_key)
        self._ensure_dataset_ids_initialized(ds)
        return getattr(ds, "ids", None)

    def _get_radius_suffix_variant_lines(self) -> list[str]:
        """Return the current editable list of radius/core suffix variants."""
        return _parse_radius_suffix_variants(self.radius_suffix_variants_var.get())

    def _get_series_group_pattern_lines(self) -> list[str]:
        """Return the current editable list of Series ID grouping patterns."""
        return _parse_series_group_patterns(self.series_group_patterns_var.get())

    def _get_manual_tree_prefix_lines(self) -> list[str]:
        """Return user-defined prefixes that should influence automatic tree grouping."""
        return _parse_manual_tree_prefixes(self.manual_tree_prefixes_var.get())

    def _get_tree_grouping_dialog_radius_lines(self) -> list[str]:
        """Return radius/core suffix variants from the open tree-groups dialog when available."""
        if hasattr(self, "tree_grouping_radius_variants_text"):
            text = self.tree_grouping_radius_variants_text.get("1.0", tk.END).strip()
            return _parse_radius_suffix_variants(text)
        return self._get_radius_suffix_variant_lines()

    def _get_tree_grouping_dialog_pattern_lines(self) -> list[str]:
        """Return Series ID grouping patterns from the open tree-groups dialog when available."""
        text = ""
        if hasattr(self, "tree_group_pattern_entry"):
            text = str(self.tree_group_pattern_entry.get() or "").strip()
        if not text and hasattr(self, "tree_group_pattern_var"):
            text = str(self.tree_group_pattern_var.get() or "").strip()
        if text:
            return _parse_series_group_patterns(text)
        return self._get_series_group_pattern_lines()

    def _rebuild_auto_ids_for_unreviewed_datasets(self) -> None:
        """Rebuild automatic ids for datasets that have not been reviewed manually."""
        for ds in self.datasets.values():
            if ds is None or getattr(ds, "rwl", None) is None:
                continue
            if bool(getattr(ds, "ids_reviewed", False)):
                continue
            self._apply_tree_grouping_mode_to_dataset(ds)
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            ds_active = self.datasets.get(self.active_dataset_key)
            if ds_active is not None:
                self.ids = getattr(ds_active, "ids", None)
        self._update_min_series_info_for_active_dataset()

    def _reset_radius_variants_dialog_defaults(self) -> None:
        """Reset the radius/core suffix dialog text to defaults."""
        if not hasattr(self, "radius_variants_text"):
            if not hasattr(self, "tree_grouping_radius_variants_text"):
                return
        if hasattr(self, "radius_variants_text"):
            self.radius_variants_text.delete("1.0", tk.END)
            self.radius_variants_text.insert("1.0", _default_radius_suffix_variants_text())
        if hasattr(self, "tree_grouping_radius_variants_text"):
            self.tree_grouping_radius_variants_text.delete("1.0", tk.END)
            self.tree_grouping_radius_variants_text.insert("1.0", _default_radius_suffix_variants_text())

    def _save_radius_variants_dialog(self) -> None:
        """Save radius/core suffix variants from the dialog."""
        if not hasattr(self, "radius_variants_text"):
            return
        text = self.radius_variants_text.get("1.0", tk.END).strip()
        lines = _parse_radius_suffix_variants(text)
        if not lines:
            messagebox.showwarning(
                "Radius variants",
                "Add at least one suffix variant or regex rule.",
                parent=self.radius_variants_dialog,
            )
            return
        self.radius_suffix_variants_var.set("\n".join(lines))
        self._rebuild_auto_ids_for_unreviewed_datasets()
        self._mark_project_dirty()
        self._bump_project_version()
        try:
            self.radius_variants_dialog.destroy()
        except Exception:
            pass

    def on_edit_radius_variants(self) -> None:
        """Open the dialog for editing auto-detected radius/core suffix variants."""
        dlg = tk.Toplevel(self)
        dlg.title("Radius/core suffix variants")
        dlg.geometry("760x520")
        dlg.minsize(520, 360)
        dlg.transient(self)
        try:
            dlg.grab_set()
        except Exception:
            pass
        self.radius_variants_dialog = dlg

        root = ttk.Frame(dlg, padding=8)
        root.pack(fill=tk.BOTH, expand=True)
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        ttk.Label(
            root,
            text=(
                "Enter one suffix variant set per line.\n"
                "Examples: A/B, R1/R2, N/S/E/W, a/b, 01/02\n"
                "You can also use regex:<pattern> for a custom regex."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        body = ttk.Frame(root)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.radius_variants_text = tk.Text(body, wrap="none")
        self.radius_variants_text.grid(row=0, column=0, sticky="nsew")
        scr_y = ttk.Scrollbar(body, orient="vertical", command=self.radius_variants_text.yview)
        scr_y.grid(row=0, column=1, sticky="ns")
        scr_x = ttk.Scrollbar(body, orient="horizontal", command=self.radius_variants_text.xview)
        scr_x.grid(row=1, column=0, sticky="ew")
        self.radius_variants_text.configure(
            yscrollcommand=scr_y.set,
            xscrollcommand=scr_x.set,
        )
        self.radius_variants_text.insert("1.0", str(self.radius_suffix_variants_var.get() or ""))

        btns = ttk.Frame(root)
        btns.grid(row=2, column=0, sticky="e", pady=(8, 0))
        ttk.Button(
            btns,
            text="Defaults",
            command=self._reset_radius_variants_dialog_defaults,
        ).pack(side="left")
        ttk.Button(
            btns,
            text="Save",
            command=self._save_radius_variants_dialog,
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            btns,
            text="Cancel",
            command=dlg.destroy,
        ).pack(side="right")
        self._apply_button_tooltips_for(dlg)

    def _build_auto_ids_for_selected_rows(
        self,
        work_df: pd.DataFrame,
        selected_series_ids: list[str],
        suffix_variant_lines: Optional[list[str]] = None,
        group_pattern_lines: Optional[list[str]] = None,
        common_site_prefix_override: str = "",
        manual_prefix_lines: Optional[list[str]] = None,
    ) -> pd.DataFrame:

        """Rebuild auto-detected labels for selected series rows."""
        out = work_df.copy()
        suffix_lines_use = (
            list(suffix_variant_lines)
            if suffix_variant_lines is not None
            else self._get_tree_grouping_dialog_radius_lines()
        )
        group_patterns_use = (
            list(group_pattern_lines)
            if group_pattern_lines is not None
            else self._get_tree_grouping_dialog_pattern_lines()
        )
        manual_prefix_lines_use = (
            list(manual_prefix_lines)
            if manual_prefix_lines is not None
            else self._get_tree_grouping_dialog_manual_prefix_lines()
        )
        for series_id in [str(x) for x in list(selected_series_ids)]:
            common_site_prefix_use = (
                _normalize_series_id_token(common_site_prefix_override)
                or _choose_series_site_prefix(
                    series_id,
                    [str(x) for x in out["series_id"].tolist()],
                    manual_prefixes=manual_prefix_lines_use,
                )
            )
            tree_label, core_label, confidence = _guess_tree_core_labels_from_series_id(
                series_id,
                suffix_variant_lines=suffix_lines_use,
                group_pattern_lines=group_patterns_use,
                common_site_prefix=common_site_prefix_use,
            )
            _tree_prefix_from_label, tree_number_text, _suffix = _split_series_id_prefix_number_suffix(tree_label)
            tree_prefix = _merge_series_prefix_parts(common_site_prefix_use, _tree_prefix_from_label)
            if series_id not in out.index:
                out.loc[series_id, "series_id"] = series_id
            out.loc[series_id, "tree_prefix"] = str(tree_prefix)
            out.loc[series_id, "tree_number_text"] = str(tree_number_text)
            out.loc[series_id, "tree_label"] = str(tree_label)
            out.loc[series_id, "core_label"] = str(core_label)
            out.loc[series_id, "confidence"] = float(confidence)
            out.loc[series_id, "source"] = "auto"
        ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
        out = _apply_nonoverlap_split_guard(
            out,
            rwl=getattr(ds, "rwl", None) if ds is not None else None,
        )
        return finalize_tree_ids_mapping(out, series_names=[str(x) for x in out["series_id"].tolist()])

    def _populate_tree_grouping_treeview(self) -> None:
        """Populate the tree-grouping Treeview from the working table."""
        if not hasattr(self, "tree_grouping_tree"):
            return
        tv = self.tree_grouping_tree
        for item in tv.get_children():
            tv.delete(item)
        work_df = self._tree_grouping_working_df
        if work_df is None or work_df.empty:
            return
        for _, row in work_df.iterrows():
            tree_num = pd.to_numeric(pd.Series([row.get("tree")]), errors="coerce").iloc[0]
            tree_text = "" if not np.isfinite(tree_num) else str(int(tree_num))
            confidence = pd.to_numeric(pd.Series([row.get("confidence")]), errors="coerce").iloc[0]
            conf_text = "" if not np.isfinite(confidence) else f"{float(confidence):.2f}"
            sid = str(row["series_id"])
            pith_status, rings_text, age_text, pith_src = "", "", "", ""
            pith_df = getattr(self, "_pith_working_df", None)
            if pith_df is not None and sid in pith_df.index:
                prow = pith_df.loc[sid]
                pith_status = str(prow["status"])
                mr = prow["missing_rings"]
                rings_text = "" if not np.isfinite(mr) else str(int(mr))
                age_text = "" if not np.isfinite(mr) else str(int(mr) + 1)
                pith_src = str(prow["source"] or "")
            tree_r_text, check_text = "", ""
            chk = getattr(self, "_tree_check_df", None)
            if chk is not None and sid in chk.index:
                tr = chk.at[sid, "tree_r"]
                tree_r_text = "" if not np.isfinite(tr) else f"{float(tr):.2f}"
                check_text = str(chk.at[sid, "flag"] or "")
            tags = []
            if pith_status:
                tags.append(f"pith_{pith_status}")
            if check_text:
                tags.append("flagged")
            tv.insert(
                "",
                "end",
                iid=sid,
                values=(
                    tree_text,
                    sid,
                    str(row.get("tree_prefix", "")),
                    str(row.get("tree_number_text", "")),
                    str(row.get("tree_label", "")),
                    str(row.get("core_label", "")),
                    conf_text,
                    str(row.get("source", "")),
                    pith_status,
                    rings_text,
                    age_text,
                    pith_src,
                    tree_r_text,
                    check_text,
                ),
                tags=tuple(tags),
            )
        if hasattr(self, "tree_pith_summary_var") and _pith is not None:
            self.tree_pith_summary_var.set(_pith.pith_summary_text(getattr(self, "_pith_working_df", None)))

    def _sync_tree_grouping_form_from_selection(self, _event=None) -> None:
        """Sync dialog edit fields from the current Treeview selection."""
        if not hasattr(self, "tree_grouping_tree"):
            return
        sel = list(self.tree_grouping_tree.selection())
        if not sel:
            self.tree_group_tree_var.set("")
            self.tree_group_core_var.set("")
            return
        work_df = self._tree_grouping_working_df
        if work_df is None or work_df.empty:
            return
        first = str(sel[0])
        if first not in work_df.index:
            return
        self.tree_group_tree_var.set(str(work_df.loc[first, "tree_label"]))
        if len(sel) == 1:
            self.tree_group_core_var.set(str(work_df.loc[first, "core_label"]))
        else:
            self.tree_group_core_var.set("")
        pith_df = getattr(self, "_pith_working_df", None)
        if pith_df is not None and first in pith_df.index and hasattr(self, "tree_pith_status_var"):
            self.tree_pith_status_var.set(str(pith_df.at[first, "status"]))
            mr = pith_df.at[first, "missing_rings"]
            self.tree_pith_rings_var.set("" if not np.isfinite(mr) else str(int(mr)))

    def _apply_tree_grouping_selection(self) -> None:
        """Apply manual tree/core labels to the selected rows."""
        if not hasattr(self, "tree_grouping_tree"):
            return
        sel = [str(x) for x in list(self.tree_grouping_tree.selection())]
        if not sel:
            return
        work_df = self._tree_grouping_working_df
        if work_df is None or work_df.empty:
            return
        tree_label = _normalize_series_id_token(self.tree_group_tree_var.get())
        core_label_raw = _normalize_series_id_token(self.tree_group_core_var.get())
        if not tree_label:
            messagebox.showwarning("Tree groups", "Tree label cannot be empty.", parent=self.tree_grouping_dialog)
            return
        manual_prefix, manual_number_text, _manual_suffix = _split_series_id_prefix_number_suffix(tree_label)
        for i, series_id in enumerate(sel, start=1):
            if series_id not in work_df.index:
                continue
            work_df.loc[series_id, "tree_prefix"] = manual_prefix
            work_df.loc[series_id, "tree_number_text"] = manual_number_text
            work_df.loc[series_id, "tree_label"] = tree_label
            if len(sel) == 1 and core_label_raw:
                core_label_use = core_label_raw
            elif core_label_raw and len(sel) > 1:
                core_label_use = f"{core_label_raw}{i}"
            else:
                core_label_use = str(i)
            work_df.loc[series_id, "core_label"] = core_label_use
            work_df.loc[series_id, "source"] = "manual"
            work_df.loc[series_id, "confidence"] = 1.0
        self._tree_grouping_working_df = finalize_tree_ids_mapping(
            work_df,
            series_names=[str(x) for x in work_df["series_id"].tolist()],
        )
        self._update_tree_grouping_summary_label()
        self._populate_tree_grouping_treeview()
        for series_id in sel:
            if self.tree_grouping_tree.exists(series_id):
                self.tree_grouping_tree.selection_add(series_id)
        self._sync_tree_grouping_form_from_selection()

    def _reset_tree_grouping_selection_to_auto(self) -> None:
        """Reset selected rows in the grouping dialog to automatic guesses."""
        if not hasattr(self, "tree_grouping_tree"):
            return
        sel = [str(x) for x in list(self.tree_grouping_tree.selection())]
        if not sel:
            return
        work_df = self._tree_grouping_working_df
        if work_df is None or work_df.empty:
            return
        self._tree_grouping_working_df = self._build_auto_ids_for_selected_rows(work_df, sel)
        self._update_tree_grouping_summary_label()
        self._populate_tree_grouping_treeview()
        for series_id in sel:
            if self.tree_grouping_tree.exists(series_id):
                self.tree_grouping_tree.selection_add(series_id)
        self._sync_tree_grouping_form_from_selection()

    def _rebuild_tree_grouping_all_auto(
        self,
        suffix_variant_lines: Optional[list[str]] = None,
        group_pattern_lines: Optional[list[str]] = None,
        manual_prefix_lines: Optional[list[str]] = None,
    ) -> None:
        """Rebuild the whole mapping table from automatic guesses."""
        if self._tree_grouping_working_df is None or self._tree_grouping_working_df.empty:
            return
        series_names = [str(x) for x in self._tree_grouping_working_df["series_id"].tolist()]
        suffix_lines_use = (
            list(suffix_variant_lines)
            if suffix_variant_lines is not None
            else self._get_tree_grouping_dialog_radius_lines()
        )
        group_patterns_use = (
            list(group_pattern_lines)
            if group_pattern_lines is not None
            else self._get_tree_grouping_dialog_pattern_lines()
        )
        manual_prefix_lines_use = (
            list(manual_prefix_lines)
            if manual_prefix_lines is not None
            else self._get_tree_grouping_dialog_manual_prefix_lines()
        )
        if bool(self.enable_tree_grouping_var.get()):
            ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
            self._tree_grouping_working_df = build_auto_tree_ids(
                series_names,
                suffix_variant_lines=suffix_lines_use,
                group_pattern_lines=group_patterns_use,
                manual_prefix_lines=manual_prefix_lines_use,
                rwl=getattr(ds, "rwl", None) if ds is not None else None,
            )
        else:
            self._tree_grouping_working_df = build_identity_tree_ids(series_names)
        self._update_tree_grouping_summary_label()
        self._populate_tree_grouping_treeview()
        self._sync_tree_grouping_form_from_selection()

    def _get_tree_grouping_dialog_manual_prefix_lines(self) -> list[str]:
        """Return user-defined prefixes from the tree-grouping dialog."""
        text = ""
        if hasattr(self, "tree_group_prefix_entry"):
            text = str(self.tree_group_prefix_entry.get() or "").strip()
        if not text and hasattr(self, "tree_group_prefix_var"):
            text = str(self.tree_group_prefix_var.get() or "").strip()
        return _parse_manual_tree_prefixes(text)

    def _apply_tree_grouping_prefixes_and_rebuild(self) -> None:
        """Save user-defined prefixes from the dialog and rebuild auto grouping."""
        if not hasattr(self, "tree_group_prefix_var"):
            return
        try:
            self.tree_grouping_dialog.update_idletasks()
        except Exception:
            pass
        text = ""
        if hasattr(self, "tree_group_prefix_entry"):
            text = str(self.tree_group_prefix_entry.get() or "").strip()
        if not text:
            text = str(self.tree_group_prefix_var.get() or "").strip()
        lines = _parse_manual_tree_prefixes(text)
        self.manual_tree_prefixes_var.set(", ".join(lines))
        self.tree_group_prefix_var.set(", ".join(lines))
        if hasattr(self, "tree_group_prefix_entry"):
            self.tree_group_prefix_entry.delete(0, tk.END)
            self.tree_group_prefix_entry.insert(0, ", ".join(lines))
        self._rebuild_tree_grouping_all_auto(manual_prefix_lines=lines)

    def _apply_tree_grouping_patterns_and_rebuild(self) -> None:
        """Save Series ID grouping patterns from the dialog and rebuild auto grouping."""
        if not hasattr(self, "tree_group_pattern_var"):
            return
        try:
            self.tree_grouping_dialog.update_idletasks()
        except Exception:
            pass
        text = ""
        if hasattr(self, "tree_group_pattern_entry"):
            text = str(self.tree_group_pattern_entry.get() or "").strip()
        if not text:
            text = str(self.tree_group_pattern_var.get() or "").strip()
        lines = _parse_series_group_patterns(text)
        if not lines:
            messagebox.showwarning(
                "Tree groups",
                "Add at least one valid Series ID grouping pattern, for example: AAAABBC, BBC",
                parent=self.tree_grouping_dialog,
            )
            return
        self.series_group_patterns_var.set("\n".join(lines))
        self.tree_group_pattern_var.set(", ".join(lines))
        if hasattr(self, "tree_group_pattern_entry"):
            self.tree_group_pattern_entry.delete(0, tk.END)
            self.tree_group_pattern_entry.insert(0, ", ".join(lines))
        self._rebuild_tree_grouping_all_auto(group_pattern_lines=lines)

    def _get_tree_grouping_counts(
        self,
        work_df: Optional[pd.DataFrame],
    ) -> tuple[int, int, int, int]:
        """Return series/tree counts for the tree-grouping dialog."""
        if work_df is None or work_df.empty:
            return (0, 0, 0, 0)
        series_names = [str(x) for x in work_df["series_id"].tolist()] if "series_id" in work_df.columns else [str(x) for x in work_df.index.astype(str)]
        ids2 = finalize_tree_ids_mapping(work_df, series_names=series_names)
        n_series = int(ids2.shape[0])
        if ids2.empty or "tree" not in ids2.columns:
            return (n_series, n_series, n_series, 0)
        tree_sizes = ids2.groupby("tree").size()
        n_trees = int(tree_sizes.shape[0])
        n_single = int((tree_sizes == 1).sum())
        n_multi = int((tree_sizes > 1).sum())
        return (n_series, n_trees, n_single, n_multi)

    def _build_tree_grouping_summary_text(self, work_df: Optional[pd.DataFrame]) -> str:
        """Build a short summary for the tree-grouping dialog."""
        n_series, n_trees, n_single, n_multi = self._get_tree_grouping_counts(work_df)
        return (
            f"Series: {n_series} | Trees after grouping: {n_trees} | "
            f"Standalone series: {n_single} | Multi-series trees: {n_multi}"
        )

    def _update_tree_grouping_summary_label(self) -> None:
        """Refresh the summary label inside the tree-grouping dialog."""
        if hasattr(self, "tree_grouping_summary_var"):
            self.tree_grouping_summary_var.set(
                self._build_tree_grouping_summary_text(self._tree_grouping_working_df)
            )

    def _save_tree_grouping_dialog(self) -> None:
        """Save the working grouping table back to the dataset."""
        if not self.active_dataset_key or self.active_dataset_key not in self.datasets:
            return
        ds = self.datasets.get(self.active_dataset_key)
        if ds is None:
            return
        if hasattr(self, "tree_grouping_radius_variants_text"):
            text = self.tree_grouping_radius_variants_text.get("1.0", tk.END).strip()
            lines = _parse_radius_suffix_variants(text)
            if lines:
                self.radius_suffix_variants_var.set("\n".join(lines))
        if hasattr(self, "tree_group_prefix_var"):
            prefix_text = ""
            if hasattr(self, "tree_group_prefix_entry"):
                prefix_text = str(self.tree_group_prefix_entry.get() or "").strip()
            if not prefix_text:
                prefix_text = str(self.tree_group_prefix_var.get() or "").strip()
            prefix_lines = _parse_manual_tree_prefixes(prefix_text)
            self.manual_tree_prefixes_var.set(", ".join(prefix_lines))
            self.tree_group_prefix_var.set(", ".join(prefix_lines))
        if hasattr(self, "tree_group_pattern_var"):
            pattern_text = ""
            if hasattr(self, "tree_group_pattern_entry"):
                pattern_text = str(self.tree_group_pattern_entry.get() or "").strip()
            if not pattern_text:
                pattern_text = str(self.tree_group_pattern_var.get() or "").strip()
            pattern_lines = _parse_series_group_patterns(pattern_text)
            if pattern_lines:
                self.series_group_patterns_var.set("\n".join(pattern_lines))
                self.tree_group_pattern_var.set(", ".join(pattern_lines))
        work_df = self._tree_grouping_working_df
        if work_df is None or work_df.empty:
            return
        ds.ids = finalize_tree_ids_mapping(
            work_df,
            series_names=[str(x) for x in list(ds.rwl.columns)],
        )
        ds.ids_reviewed = True
        ds.ids_source = "manual"
        self.ids = ds.ids.copy()
        if hasattr(self, "tree_pith_policy_label_var"):
            new_policy = "exclude" if "exclude" in str(self.tree_pith_policy_label_var.get()) else "present"
            if new_policy != self.unknown_pith_policy_var.get():
                self.unknown_pith_policy_var.set(new_policy)
        pith_new = getattr(self, "_pith_working_df", None)
        if _pith is not None and pith_new is not None:
            old = getattr(ds, "pith", None)
            old_n = _pith.normalize_pith_table(old, [str(x) for x in list(ds.rwl.columns)])
            if not old_n[["status", "missing_rings"]].fillna(-1).equals(pith_new[["status", "missing_rings"]].fillna(-1)):
                self._set_dataset_pith(ds, pith_new.copy())
        self._update_min_series_info_for_active_dataset()
        self._mark_project_dirty()
        self._bump_project_version()
        try:
            self.tree_grouping_dialog.destroy()
        except Exception:
            pass

    def _apply_tree_pith_selection(self) -> None:
        """Set pith status / rings to pith for the selected rows (working copy)."""
        if _pith is None or getattr(self, "_pith_working_df", None) is None:
            return
        sel = [str(x) for x in list(self.tree_grouping_tree.selection())]
        if not sel:
            messagebox.showinfo("Pith", "Select one or more series first.", parent=self.tree_grouping_dialog)
            return
        status = str(self.tree_pith_status_var.get() or "unknown")
        rings_txt = str(self.tree_pith_rings_var.get() or "").strip()
        rings: Optional[float] = None
        if status == "estimated":
            try:
                rings = float(rings_txt.replace(",", "."))
            except ValueError:
                messagebox.showwarning(
                    "Pith", "Enter the number of rings to the pith (0 or more).", parent=self.tree_grouping_dialog
                )
                return
        try:
            self._pith_working_df = _pith.set_pith(self._pith_working_df, sel, status, rings)
        except ValueError as exc:
            messagebox.showwarning("Pith", str(exc), parent=self.tree_grouping_dialog)
            return
        self._populate_tree_grouping_treeview()
        for sid in sel:
            if self.tree_grouping_tree.exists(sid):
                self.tree_grouping_tree.selection_add(sid)

    def _dialog_import_pith(self, files: list[str]) -> None:
        if _pith is None or getattr(self, "_pith_working_df", None) is None:
            return
        ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
        if ds is None:
            return
        series = [str(c) for c in ds.rwl.columns]
        found, report = _pith.collect_pith_info(series, files, rwl=ds.rwl, rwl_path=str(ds.path or ""))
        self._pith_working_df, n = _pith.merge_pith(self._pith_working_df, found, overwrite_known=True)
        self._populate_tree_grouping_treeview()
        messagebox.showinfo(
            "Pith info",
            f"Updated {n} series (manual edits kept).\n{_pith.pith_summary_text(self._pith_working_df)}\n\n"
            + "\n".join(report)
            + "\n\nPress Save to keep the changes.",
            parent=self.tree_grouping_dialog,
        )

    def _dialog_import_pith_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.tree_grouping_dialog,
            title="Select .pos or pith CSV files",
            filetypes=[("Pith information", "*.pos *.csv"), ("All files", "*.*")],
            initialdir=self._get_dialog_initial_dir(),
        )
        if paths:
            self._remember_dialog_location(paths[0])
            self._dialog_import_pith(list(paths))

    def _dialog_import_pith_folder(self) -> None:
        if _pith is None:
            return
        folder = filedialog.askdirectory(
            parent=self.tree_grouping_dialog, title="Folder with .pos / pith files",
            initialdir=self._get_dialog_initial_dir(),
        )
        if not folder:
            return
        self._remember_dialog_location(folder, is_dir=True)
        files = _pith.files_in_folder(folder, recursive=True)
        if not files:
            messagebox.showinfo("Pith info", "No .pos or .pith.csv files found.", parent=self.tree_grouping_dialog)
            return
        self._dialog_import_pith(files)

    def _run_tree_grouping_check(self) -> None:
        """Correlate radii (high-pass ring widths) to check and suggest tree grouping."""
        if _pith is None:
            return
        ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
        work_df = self._tree_grouping_working_df
        if ds is None or work_df is None or work_df.empty:
            return
        series = [str(x) for x in work_df["series_id"].tolist()]
        rwl = ds.rwl.copy()
        rwl.columns = [str(c) for c in rwl.columns]
        r = _pith.pairwise_r(rwl[series])
        ids_now = work_df.set_index("series_id", drop=False)
        self._tree_check_df = _pith.within_tree_stats(ids_now, r)
        current = {sid: str(ids_now.at[sid, "tree_label"]) for sid in series}
        cands = _pith.infer_grouping_schemes(series, rwl, current=current, r=r)
        cur = next((c for c in cands if c["scheme"] == "current"), None)
        best = cands[0] if cands else None
        lines = []
        if cur is not None:
            w = cur["within_r"]
            lines.append(
                "Current grouping: "
                + (f"mean r between radii of one tree = {w:.2f}" if np.isfinite(w) else "no multi-radius trees")
                + f", typical r between trees = {cur['between_r']:.2f}; {cur['multi_trees']} multi-radius tree(s)."
            )
        n_flag = int((self._tree_check_df["flag"] != "").sum())
        if n_flag:
            lines.append(f"{n_flag} series flagged (see the Check column).")
        self._tree_scheme_suggestion = None
        if best is not None and best["scheme"] != "current" and np.isfinite(best["within_r"]) \
                and best["within_r"] >= 0.4 and (cur is None or best["score"] > cur["score"] + 0.05):
            self._tree_scheme_suggestion = best
            lines.append(
                f"Suggested: {best['label']} -> {best['multi_trees']} multi-radius trees, "
                f"r within trees {best['within_r']:.2f} vs between trees {best['between_r']:.2f}."
            )
        elif best is not None:
            lines.append("The current grouping is the best of the tested ID schemes.")
        self.tree_check_text_var.set("  ".join(lines))
        self.btn_tree_apply_scheme.configure(state=tk.NORMAL if self._tree_scheme_suggestion else tk.DISABLED)
        self._populate_tree_grouping_treeview()

    def _apply_tree_scheme_suggestion(self) -> None:
        sug = getattr(self, "_tree_scheme_suggestion", None)
        work_df = self._tree_grouping_working_df
        if not sug or work_df is None:
            return
        rows = []
        cores = sug.get("cores") or {}
        for sid in [str(x) for x in work_df["series_id"].tolist()]:
            tree_label = str(sug["groups"].get(sid, sid))
            tp, tn, _sf = _split_series_id_prefix_number_suffix(tree_label)
            rows.append(
                {
                    "series_id": sid,
                    "tree_prefix": tp,
                    "tree_number_text": tn,
                    "tree_label": tree_label,
                    "core_label": str(cores.get(sid, "1")),
                    "confidence": float(sug["within_r"]) if np.isfinite(sug["within_r"]) else np.nan,
                    "source": "ring-width",
                }
            )
        self._tree_grouping_working_df = finalize_tree_ids_mapping(
            pd.DataFrame(rows), series_names=[str(x) for x in work_df["series_id"].tolist()]
        )
        self._update_tree_grouping_summary_label()
        self._run_tree_grouping_check()

    def on_edit_tree_groups(self) -> None:
        """Open the tree-grouping review dialog for the active dataset."""
        if not self.active_dataset_key or self.active_dataset_key not in self.datasets:
            messagebox.showwarning("Tree groups", "Load and select a dataset first.")
            return
        ds = self.datasets.get(self.active_dataset_key)
        if ds is None:
            return
        self._ensure_dataset_ids_initialized(ds)
        self._tree_grouping_working_df = finalize_tree_ids_mapping(
            getattr(ds, "ids", None),
            series_names=[str(x) for x in list(ds.rwl.columns)],
        )
        self._pith_working_df = (
            _pith.normalize_pith_table(getattr(ds, "pith", None), [str(x) for x in list(ds.rwl.columns)])
            if _pith is not None
            else None
        )
        self._tree_check_df = None
        self._tree_scheme_suggestion = None

        dlg = tk.Toplevel(self)
        dlg.title(f"Tree groups: {self.active_dataset_key}")
        dlg.geometry("1280x820")
        dlg.minsize(760, 420)
        dlg.transient(self)
        try:
            dlg.grab_set()
        except Exception:
            pass
        self.tree_grouping_dialog = dlg

        root = ttk.Frame(dlg, padding=8)
        root.pack(fill=tk.BOTH, expand=True)
        root.rowconfigure(2, weight=1)
        root.columnconfigure(0, weight=1)

        top = ttk.Frame(root)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            top,
            text="Review automatic tree grouping, edit tree/core labels and the pith (centre) of each series.",
        ).pack(side=tk.LEFT)
        self.tree_grouping_summary_var = tk.StringVar(
            value=self._build_tree_grouping_summary_text(self._tree_grouping_working_df)
        )
        ttk.Label(
            top,
            textvariable=self.tree_grouping_summary_var,
        ).pack(side=tk.RIGHT)

        variants_box = ttk.LabelFrame(root, text="Radius/core suffix variants")
        variants_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        variants_box.rowconfigure(1, weight=1)
        variants_box.columnconfigure(0, weight=1)
        ttk.Label(
            variants_box,
            text=(
                "One variant set per line. Examples: A/B, R1/R2, N/S/E/W, a/b, 01/02\n"
                "You can also use regex:<pattern> for a custom regex."
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 4))
        variants_text_frame = ttk.Frame(variants_box)
        variants_text_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=8, pady=(0, 6))
        variants_text_frame.rowconfigure(0, weight=1)
        variants_text_frame.columnconfigure(0, weight=1)
        self.tree_grouping_radius_variants_text = tk.Text(variants_text_frame, height=3, wrap="none")
        self.tree_grouping_radius_variants_text.grid(row=0, column=0, sticky="nsew")
        variants_scroll_y = ttk.Scrollbar(
            variants_text_frame,
            orient="vertical",
            command=self.tree_grouping_radius_variants_text.yview,
        )
        variants_scroll_y.grid(row=0, column=1, sticky="ns")
        variants_scroll_x = ttk.Scrollbar(
            variants_text_frame,
            orient="horizontal",
            command=self.tree_grouping_radius_variants_text.xview,
        )
        variants_scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree_grouping_radius_variants_text.configure(
            yscrollcommand=variants_scroll_y.set,
            xscrollcommand=variants_scroll_x.set,
        )
        self.tree_grouping_radius_variants_text.insert("1.0", str(self.radius_suffix_variants_var.get() or ""))
        ttk.Button(
            variants_box,
            text="Defaults",
            command=self._reset_radius_variants_dialog_defaults,
        ).grid(row=2, column=1, sticky="e", padx=(0, 8), pady=(0, 6))
        ttk.Button(
            variants_box,
            text="Rebuild auto from variants",
            command=self._apply_tree_grouping_radius_variants_and_rebuild,
        ).grid(row=2, column=2, sticky="e", padx=(0, 8), pady=(0, 6))

        table_frame = ttk.Frame(root)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree_grouping_tree = ttk.Treeview(
            table_frame,
            columns=("tree", "series_id", "tree_prefix", "tree_number_text", "tree_label", "core_label", "confidence", "source",
                     "pith", "rings_to_pith", "first_age", "pith_source", "tree_r", "check"),
            show="headings",
            selectmode="extended",
        )
        self.tree_grouping_tree.heading("tree", text="#")
        self.tree_grouping_tree.heading("series_id", text="Series ID")
        self.tree_grouping_tree.heading("tree_prefix", text="Prefix")
        self.tree_grouping_tree.heading("tree_number_text", text="Tree number")
        self.tree_grouping_tree.heading("tree_label", text="Tree label")
        self.tree_grouping_tree.heading("core_label", text="Core/radius")
        self.tree_grouping_tree.heading("confidence", text="Confidence")
        self.tree_grouping_tree.heading("source", text="Source")
        self.tree_grouping_tree.column("tree", width=56, anchor="center")
        self.tree_grouping_tree.column("series_id", width=240, anchor="w")
        self.tree_grouping_tree.column("tree_prefix", width=110, anchor="w")
        self.tree_grouping_tree.column("tree_number_text", width=110, anchor="center")
        self.tree_grouping_tree.column("tree_label", width=180, anchor="w")
        self.tree_grouping_tree.column("core_label", width=120, anchor="w")
        self.tree_grouping_tree.column("confidence", width=90, anchor="center")
        self.tree_grouping_tree.column("source", width=90, anchor="center")
        self.tree_grouping_tree.heading("pith", text="Pith")
        self.tree_grouping_tree.heading("rings_to_pith", text="Rings to pith")
        self.tree_grouping_tree.heading("first_age", text="First ring age")
        self.tree_grouping_tree.heading("pith_source", text="Pith source")
        self.tree_grouping_tree.heading("tree_r", text="Tree r")
        self.tree_grouping_tree.heading("check", text="Check")
        self.tree_grouping_tree.column("pith", width=80, anchor="center")
        self.tree_grouping_tree.column("rings_to_pith", width=90, anchor="center")
        self.tree_grouping_tree.column("first_age", width=90, anchor="center")
        self.tree_grouping_tree.column("pith_source", width=150, anchor="w")
        self.tree_grouping_tree.column("tree_r", width=60, anchor="center")
        self.tree_grouping_tree.column("check", width=200, anchor="w")
        self.tree_grouping_tree.column("series_id", width=150, anchor="w")
        self.tree_grouping_tree.column("tree_prefix", width=80, anchor="w")
        self.tree_grouping_tree.column("tree_number_text", width=90, anchor="center")
        self.tree_grouping_tree.column("tree_label", width=120, anchor="w")
        self.tree_grouping_tree.column("core_label", width=90, anchor="w")
        self.tree_grouping_tree.tag_configure("pith_present", background="#e8f5e9")
        self.tree_grouping_tree.tag_configure("pith_estimated", background="#e3f2fd")
        self.tree_grouping_tree.tag_configure("pith_unknown", background="#f5f5f5")
        self.tree_grouping_tree.tag_configure("flagged", foreground="#b71c1c")
        self.tree_grouping_tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree_grouping_tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree_grouping_tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree_grouping_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree_grouping_tree.bind("<<TreeviewSelect>>", self._sync_tree_grouping_form_from_selection)

        form = ttk.LabelFrame(root, text="Edit selection")
        form.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.tree_group_tree_var = tk.StringVar(value="")
        self.tree_group_core_var = tk.StringVar(value="")
        self.tree_group_prefix_var = tk.StringVar(
            value=", ".join(self._get_manual_tree_prefix_lines())
        )
        self.tree_group_pattern_var = tk.StringVar(
            value=", ".join(self._get_series_group_pattern_lines())
        )
        ttk.Label(form, text="Tree label:").grid(row=0, column=0, sticky="w", padx=(8, 6), pady=8)
        ttk.Entry(form, textvariable=self.tree_group_tree_var, width=22).grid(row=0, column=1, sticky="w", pady=8)
        ttk.Label(form, text="Core/radius:").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=8)
        ttk.Entry(form, textvariable=self.tree_group_core_var, width=18).grid(row=0, column=3, sticky="w", pady=8)
        ttk.Button(form, text="Apply to selected", command=self._apply_tree_grouping_selection).grid(
            row=0, column=4, sticky="e", padx=(12, 6), pady=8
        )
        ttk.Button(form, text="Reset selected to auto", command=self._reset_tree_grouping_selection_to_auto).grid(
            row=0, column=5, sticky="e", padx=(6, 6), pady=8
        )
        ttk.Button(form, text="Rebuild all auto", command=self._rebuild_tree_grouping_all_auto).grid(
            row=0, column=6, sticky="e", padx=(6, 8), pady=8
        )
        ttk.Label(form, text="User prefixes:").grid(row=1, column=0, sticky="w", padx=(8, 6), pady=(0, 8))
        self.tree_group_prefix_entry = ttk.Entry(form, textvariable=self.tree_group_prefix_var, width=28)
        self.tree_group_prefix_entry.grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=(0, 8)
        )
        self.tree_group_prefix_entry.bind("<Return>", lambda _event: self._apply_tree_grouping_prefixes_and_rebuild())
        ttk.Button(form, text="Apply prefixes", command=self._apply_tree_grouping_prefixes_and_rebuild).grid(
            row=1, column=4, sticky="e", padx=(12, 6), pady=(0, 8)
        )
        ttk.Label(
            form,
            text="Comma-separated prefixes that should influence auto detection.",
            justify="left",
        ).grid(row=1, column=5, columnspan=2, sticky="w", padx=(6, 8), pady=(0, 8))
        ttk.Label(form, text="Pattern groups:").grid(row=2, column=0, sticky="w", padx=(8, 6), pady=(0, 8))
        self.tree_group_pattern_entry = ttk.Entry(form, textvariable=self.tree_group_pattern_var, width=28)
        self.tree_group_pattern_entry.grid(
            row=2, column=1, columnspan=3, sticky="ew", pady=(0, 8)
        )
        self.tree_group_pattern_entry.bind("<Return>", lambda _event: self._apply_tree_grouping_patterns_and_rebuild())
        ttk.Button(form, text="Update", command=self._apply_tree_grouping_patterns_and_rebuild).grid(
            row=2, column=4, sticky="e", padx=(12, 6), pady=(0, 8)
        )
        ttk.Label(
            form,
            text="Examples: AAAABBC, BBC",
            justify="left",
        ).grid(row=2, column=5, columnspan=2, sticky="w", padx=(6, 8), pady=(0, 8))

        pith_box = ttk.LabelFrame(root, text="Pith (centre) of selected series")
        pith_box.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.tree_pith_status_var = tk.StringVar(value="present")
        self.tree_pith_rings_var = tk.StringVar(value="")
        self.tree_pith_summary_var = tk.StringVar(value="")
        ttk.Label(pith_box, text="Status:").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=6)
        ttk.Combobox(
            pith_box, textvariable=self.tree_pith_status_var, values=("present", "estimated", "unknown"),
            state="readonly", width=11,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(pith_box, text="Rings to pith:").grid(row=0, column=2, sticky="w", padx=(12, 4))
        ttk.Entry(pith_box, textvariable=self.tree_pith_rings_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Button(pith_box, text="Apply pith to selected", command=self._apply_tree_pith_selection).grid(
            row=0, column=4, padx=(12, 4)
        )
        ttk.Button(pith_box, text="Import .pos / pith CSV...", command=self._dialog_import_pith_files).grid(
            row=0, column=5, padx=4
        )
        ttk.Button(pith_box, text="Import from folder...", command=self._dialog_import_pith_folder).grid(
            row=0, column=6, padx=4
        )
        ttk.Label(pith_box, text="Unknown pith in RCS:").grid(row=1, column=0, columnspan=2, sticky="w", padx=(8, 4), pady=(0, 6))
        self.tree_pith_policy_label_var = tk.StringVar(
            value="treat as at the pith (offset 1)" if self.unknown_pith_policy_var.get() != "exclude"
            else "exclude from RCS"
        )
        ttk.Combobox(
            pith_box, textvariable=self.tree_pith_policy_label_var,
            values=("treat as at the pith (offset 1)", "exclude from RCS"), state="readonly", width=30,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(pith_box, textvariable=self.tree_pith_summary_var).grid(
            row=1, column=4, columnspan=3, sticky="w", padx=(12, 8), pady=(0, 6)
        )
        ttk.Label(
            pith_box,
            text=(
                "Rings to pith = rings missing between the innermost measured ring and the pith "
                "(0 = pith present). First ring age = rings to pith + 1 (dplR pith.offset)."
            ),
            foreground="gray",
        ).grid(row=2, column=0, columnspan=7, sticky="w", padx=8, pady=(0, 6))

        check_box = ttk.LabelFrame(root, text="Check grouping with ring widths")
        check_box.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.tree_check_text_var = tk.StringVar(
            value="Compares the year-to-year signal of radii: radii of one tree should correlate strongly."
        )
        ttk.Button(check_box, text="Check", command=self._run_tree_grouping_check).grid(
            row=0, column=0, padx=(8, 4), pady=6
        )
        self.btn_tree_apply_scheme = ttk.Button(
            check_box, text="Apply suggested scheme", command=self._apply_tree_scheme_suggestion, state=tk.DISABLED
        )
        self.btn_tree_apply_scheme.grid(row=0, column=1, padx=4, pady=6)
        ttk.Label(check_box, textvariable=self.tree_check_text_var, wraplength=820, justify="left").grid(
            row=0, column=2, sticky="w", padx=(8, 8)
        )

        btns = ttk.Frame(root)
        btns.grid(row=6, column=0, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Save", command=self._save_tree_grouping_dialog).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")

        self._update_tree_grouping_summary_label()
        self._populate_tree_grouping_treeview()
        self._sync_tree_grouping_form_from_selection()
        self._apply_button_tooltips_for(dlg)

    def _apply_tree_grouping_radius_variants_and_rebuild(self) -> None:
        """Save suffix variants from the tree-groups dialog and rebuild auto grouping."""
        if not hasattr(self, "tree_grouping_radius_variants_text"):
            return
        text = self.tree_grouping_radius_variants_text.get("1.0", tk.END).strip()
        lines = _parse_radius_suffix_variants(text)
        if not lines:
            messagebox.showwarning(
                "Tree groups",
                "Add at least one suffix variant or regex rule.",
                parent=self.tree_grouping_dialog,
            )
            return
        self.tree_grouping_radius_variants_text.delete("1.0", tk.END)
        self.tree_grouping_radius_variants_text.insert("1.0", "\n".join(lines))
        self._rebuild_tree_grouping_all_auto(suffix_variant_lines=lines)


    def _make_unique_key(self, base: str) -> str:
        """Return a unique dataset key based on the given base string."""
        base = (base or "dataset").strip() or "dataset"
        key = base
        i = 2
        while key in self.datasets:
            key = f"{base}_{i}"
            i += 1
        return key

    def _get_dialog_initial_dir(self, fallback: Optional[str] = None) -> str:
        """Return the initial directory for open/save/import/export dialogs."""
        active_dataset_dir: Optional[str] = None
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            try:
                ds_path = getattr(self.datasets[self.active_dataset_key], "path", None)
                if ds_path:
                    active_dataset_dir = os.path.dirname(os.path.abspath(str(ds_path)))
            except Exception:
                active_dataset_dir = None

        candidates = [
            getattr(self, "_last_dialog_dir", None),
            fallback,
            self.output_dir,
            os.path.dirname(os.path.abspath(self.current_project_path)) if self.current_project_path else None,
            os.path.dirname(os.path.abspath(self.climate_path)) if self.climate_path else None,
            active_dataset_dir,
            os.path.dirname(os.path.abspath(self.rwl_path)) if self.rwl_path else None,
            os.getcwd(),
        ]
        for cand in candidates:
            if not cand:
                continue
            try:
                d = os.path.abspath(str(cand))
            except Exception:
                continue
            if os.path.isdir(d):
                return d
        return os.getcwd()

    def _remember_dialog_location(self, path: Optional[str], *, is_dir: bool = False) -> None:
        """Remember the last directory actually used by a dialog."""
        if not path:
            return
        try:
            raw = os.path.abspath(str(path))
        except Exception:
            return
        directory = raw if is_dir else os.path.dirname(raw)
        if not directory:
            return
        if os.path.isdir(directory):
            self._last_dialog_dir = directory

    def _mark_project_dirty(self) -> None:
        """Mark the current project as modified."""
        self._project_dirty = True

    def _bump_project_version(self) -> None:
        """Invalidate derived caches when underlying data/results change."""
        self._project_version += 1
        self._compare_cache.clear()
        self._signal_diag_cache.clear()


    def _invalidate_pairwise_compare_cache_for_dataset_key(self, dataset_key: str) -> None:
        """Remove cached pairwise correlations that involve a given dataset key."""
        try:
            ds_key = str(dataset_key)
        except Exception:
            return
        if not ds_key:
            return
        cache = getattr(self, "_pairwise_corr_cache", None)
        if not isinstance(cache, dict) or not cache:
            return
        # Cache key format: (goal, label_a, sig_a, label_b, sig_b). Labels may include "<dataset>|<method>".
        keys_to_delete: list[tuple] = []
        for k in list(cache.keys()):
            try:
                label_a = str(k[1])
                label_b = str(k[3])
            except Exception:
                continue
            ds_a = label_a.split("|", 1)[0]
            ds_b = label_b.split("|", 1)[0]
            if ds_a == ds_key or ds_b == ds_key:
                keys_to_delete.append(k)
        for k in keys_to_delete:
            try:
                del cache[k]
            except Exception:
                pass
    def _reset_project_state(self) -> None:
        """Clear all loaded data and analysis results (start a new project)."""
        self.datasets = {}
        self.active_dataset_key = None
        self.rwl = None
        self.rwl_path = None
        self.advisor_result = None
        self.climate = None
        self.climate_path = None
        self.climate_column = None
        self.output_dir = None
        self.current_project_path = None
        self._project_dirty = False
        self._pairwise_corr_cache.clear()
        self._bump_project_version()
        # Refresh UI safely (some widgets may not exist during early startup).
        try:
            self.dataset_var.set("")
        except Exception:
            pass
        try:
            self._refresh_dataset_ui_state()
        except Exception:
            pass
        try:
            self._sync_method_widgets_for_active_dataset()
        except Exception:
            pass
        try:
            self._update_recommendation_text()
        except Exception:
            pass
        try:
            self._update_structure_plots()
            self.update_chron_plots()
            self._update_signal_plots()
            self.update_compare_plots()
        except Exception:
            pass
        self.status_var.set("New project. Load data to begin.")

    def _resolve_dataset_display_result(self, ds: DatasetState) -> Optional[AdvisorResult]:
        """Return the advisor result that should currently be displayed for one dataset."""
        if ds is None:
            return None
        if bool(self.enable_dplr_powt_var.get()) and bool(self.show_powt_results_var.get()):
            method = self._selected_powt_display_method()
            if method == "universal":
                return (
                    ds.advisor_result_with_powt_universal
                    or ds.advisor_result_with_powt
                    or ds.advisor_result
                    or ds.advisor_result_no_powt
                )
            if method == "cook":
                return (
                    ds.advisor_result_with_powt_cook
                    or ds.advisor_result_with_powt
                    or ds.advisor_result
                    or ds.advisor_result_no_powt
                )
            return ds.advisor_result_with_powt or ds.advisor_result or ds.advisor_result_no_powt
        return ds.advisor_result_no_powt or ds.advisor_result or ds.advisor_result_with_powt

    def _selected_powt_display_method(self) -> str:
        """Return which POWT method should currently be shown in plots/tables."""
        method = ""
        try:
            method = str(self.dplr_powt_method_var.get()).strip().lower()
        except Exception:
            method = ""
        if method not in ("universal", "cook"):
            try:
                method = str(self.powt_method_var.get()).strip().lower()
            except Exception:
                method = ""
        return method if method in ("universal", "cook") else ""

    def _best_advisor_score(self, ar: Optional[AdvisorResult]) -> float:
        """Return best candidate score for one advisor result."""
        if ar is None:
            return float("nan")
        try:
            cand = ar.candidates.get(ar.best_key)
            if cand is None:
                return float("nan")
            return float(cand.score)
        except Exception:
            return float("nan")

    def _current_powt_display_label(self) -> str:
        """Return a human-readable label for the currently displayed POWT state."""
        if not self.active_dataset_key or self.active_dataset_key not in self.datasets:
            return "POWT OFF"
        if not self._displayed_results_use_powt():
            return "POWT OFF"
        method = self._selected_powt_display_method()
        if method not in ("universal", "cook"):
            ds = self.datasets[self.active_dataset_key]
            method = str(getattr(ds, "powt_best_method", "") or "").strip().lower()
        if method in ("universal", "cook"):
            return f"POWT ON ({method})"
        return "POWT ON"

    def _on_powt_main_controls_changed(self, _event=None) -> None:
        """Sync the single POWT toggle across legacy controls and refresh views."""
        enabled = bool(self.enable_dplr_powt_var.get())
        try:
            self.powt_use_for_run_var.set(enabled)
        except Exception:
            pass
        try:
            self.powt_rescale_var.set(enabled)
        except Exception:
            pass
        try:
            self.dplr_powt_rescale_var.set(enabled)
        except Exception:
            pass
        try:
            self.show_powt_results_var.set(enabled)
        except Exception:
            pass
        try:
            self._on_compat_mode_changed()
        except Exception:
            pass
        try:
            self._signal_diag_cache.clear()
        except Exception:
            pass
        try:
            self._compare_cache.clear()
        except Exception:
            pass
        try:
            self._update_powt_tab()
        except Exception:
            pass
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            try:
                self.set_active_dataset(self.active_dataset_key)
            except Exception:
                pass
        if (
            enabled
            and not bool(getattr(self, "_analysis_running", False))
            and self.active_dataset_key
            and self.active_dataset_key in self.datasets
        ):
            ds = self.datasets.get(self.active_dataset_key)
            if ds is not None:
                has_powt_results = (
                    getattr(ds, "powt_preview_for_run_universal", None) is not None
                    and getattr(ds, "advisor_result_with_powt", None) is not None
                )
                if not has_powt_results:
                    try:
                        self.after(10, self.on_run)
                    except Exception:
                        pass

    def _on_result_powt_toggle_changed(self) -> None:
        """Refresh the active dataset after toggling which POWT state should be displayed."""
        try:
            self._signal_diag_cache.clear()
        except Exception:
            pass
        try:
            self._compare_cache.clear()
        except Exception:
            pass
        if self.active_dataset_key and self.active_dataset_key in self.datasets:
            self.set_active_dataset(self.active_dataset_key)

    def _compare_powt_runs(
        self,
        no_powt: Optional[AdvisorResult],
        with_powt: Optional[AdvisorResult],
        powt_preview: Optional[PowtPreviewResult],
    ) -> tuple[str, str, Optional[AdvisorResult]]:
        """Compare no-POWT and POWT runs and return (flag, text, recommended_result)."""
        if no_powt is None and with_powt is None:
            return "unknown", "POWT comparison was not available.", None
        if no_powt is None:
            return "required", "Only the POWT-based analysis completed successfully; POWT is recommended for this dataset.", with_powt
        if with_powt is None:
            return "not_required", "Only the non-POWT analysis completed successfully; POWT is not recommended for this dataset.", no_powt

        best_no = no_powt.candidates.get(no_powt.best_key)
        best_yes = with_powt.candidates.get(with_powt.best_key)
        score_no = float(best_no.score) if best_no is not None else float("nan")
        score_yes = float(best_yes.score) if best_yes is not None else float("nan")
        delta = score_yes - score_no if np.isfinite(score_yes) and np.isfinite(score_no) else float("nan")

        skew_improved = False
        skew_delta = float("nan")
        if powt_preview is not None:
            raw_sk = pd.to_numeric(powt_preview.raw_skew, errors="coerce").to_numpy(dtype=float)
            tr_sk = pd.to_numeric(powt_preview.powt_skew, errors="coerce").to_numpy(dtype=float)
            raw_sk = np.abs(raw_sk[np.isfinite(raw_sk)])
            tr_sk = np.abs(tr_sk[np.isfinite(tr_sk)])
            if raw_sk.size > 0 and tr_sk.size > 0:
                raw_med = float(np.nanmedian(raw_sk))
                tr_med = float(np.nanmedian(tr_sk))
                skew_delta = raw_med - tr_med
                skew_improved = bool(np.isfinite(skew_delta) and skew_delta > 0.02)

        if np.isfinite(delta) and delta >= 0.05:
            return (
                "required",
                f"POWT improved the final recommendation materially (best score {score_no:.3f} -> {score_yes:.3f}). "
                f"Recommended state: POWT ON. Best method without POWT: {no_powt.best_key}. "
                f"Best method with POWT: {with_powt.best_key}.",
                with_powt,
            )
        if np.isfinite(delta) and delta >= 0.015:
            return (
                "helpful",
                f"POWT improved the result slightly (best score {score_no:.3f} -> {score_yes:.3f}). "
                f"Recommended state: POWT ON. Best method without POWT: {no_powt.best_key}. "
                f"Best method with POWT: {with_powt.best_key}.",
                with_powt,
            )
        if np.isfinite(delta) and delta <= -0.015:
            return (
                "not_recommended",
                f"POWT reduced the final score (best score {score_no:.3f} -> {score_yes:.3f}). "
                f"Recommended state: POWT OFF. Best method without POWT: {no_powt.best_key}. "
                f"Best method with POWT: {with_powt.best_key}.",
                no_powt,
            )
        if skew_improved:
            return (
                "neutral",
                f"POWT improved skewness but did not materially improve the final advisor result "
                f"(best score {score_no:.3f} vs {score_yes:.3f}). Recommended state: POWT OFF by default.",
                no_powt,
            )
        return (
            "not_required",
            f"POWT did not materially improve the advisor result "
            f"(best score {score_no:.3f} vs {score_yes:.3f}). Recommended state: POWT OFF.",
            no_powt,
        )

    def _collect_project_state(self) -> dict:
        """Collect a picklable snapshot of the current project."""
        return {
            "format": "detrend_advisor_project",
            "format_version": 2,  # 2: Tucson ring widths stored in mm
            "created_at": _dt.datetime.utcnow().isoformat() + "Z",
            "app_state": {
                "goal": self.goal_var.get(),
                "var_type": self.var_type_var.get(),
                "compat_mode": self.compat_mode_var.get(),
                "output_dir": self.output_dir,
                "unknown_pith_policy": self.unknown_pith_policy_var.get(),
                "active_dataset_key": self.active_dataset_key,
                "custom_methods": list(self.custom_method_keys),
            },
            "datasets": self.datasets,
            "climate": {
                "series": self.climate,
                "path": self.climate_path,
                "column": self.climate_column,
            },
        }

    @staticmethod
    def _migrate_dataset_units(ds: Any) -> None:
        """Projects saved before v2 kept Tucson widths in file units (0.01 / 0.001 mm)."""
        try:
            path = str(getattr(ds, "path", "") or "")
            if path.lower().endswith(".csv"):
                return
            rwl = getattr(ds, "rwl", None)
            if isinstance(rwl, pd.DataFrame):
                ds.rwl = rwl_raw_units_to_mm(rwl, path)
        except Exception:
            pass

    def _apply_project_state(self, state: dict, *, project_path: Optional[str] = None, merge: bool = False) -> None:
        """Apply a saved project state, optionally merging into the current project."""
        if not isinstance(state, dict) or state.get("format") != "detrend_advisor_project":
            raise ValueError("Unsupported project format.")
        datasets = state.get("datasets", {})
        if datasets is None:
            datasets = {}
        if not isinstance(datasets, dict):
            raise ValueError("Project 'datasets' is invalid.")
        app_state = state.get("app_state", {}) or {}
        clim_state = state.get("climate", {}) or {}
        project_methods = _normalize_custom_methods(app_state.get("custom_methods", []))
        if merge:
            project_methods = _normalize_custom_methods(self.custom_method_keys + project_methods)

        if not merge:
            self._reset_project_state()

        self.custom_method_keys = project_methods
        self._refresh_custom_method_guide()

        # Merge datasets with unique keys.
        for key, ds in datasets.items():
            if not isinstance(key, str):
                continue
            if ds is None:
                continue
            base = getattr(ds, "key", None) or key
            if int(state.get("format_version", 1) or 1) < 2:
                self._migrate_dataset_units(ds)
            new_key = self._make_unique_key(str(base))
            try:
                ds.key = new_key
            except Exception:
                pass
            self.datasets[new_key] = ds

        # Climate: only import if the current project does not have a climate series.
        if self.climate is None and isinstance(clim_state, dict):
            ser = clim_state.get("series", None)
            if isinstance(ser, pd.Series):
                self.climate = ser
                self.climate_path = str(clim_state.get("path", "") or "") or None
                self.climate_column = str(clim_state.get("column", "") or "") or None

        # Apply app settings (prefer current ones when merging).
        if not merge:
            goal = str(app_state.get("goal", "") or "").strip() or self.goal_var.get()
            self.goal_var.set(goal)
            self.var_type_var.set(str(app_state.get("var_type", "") or self.var_type_var.get()))
            compat_mode = str(app_state.get("compat_mode", "") or "").strip()
            if compat_mode in ("legacy", "dplR"):
                self.compat_mode_var.set(compat_mode)
            self.output_dir = app_state.get("output_dir", None) or self.output_dir
            policy = str(app_state.get("unknown_pith_policy", "") or "").strip()
            if policy in ("present", "exclude"):
                self.unknown_pith_policy_var.set(policy)

        # Select active dataset.
        active_key = str(app_state.get("active_dataset_key", "") or "").strip()
        if merge:
            # Keep current active dataset when merging.
            active_key = self.active_dataset_key or active_key

        self._refresh_dataset_ui_state()
        if active_key and active_key in self.datasets:
            self.set_active_dataset(active_key)
        elif self.datasets:
            self.set_active_dataset(next(iter(self.datasets.keys())))
        else:
            self.active_dataset_key = None

        self.current_project_path = project_path if project_path else self.current_project_path
        if project_path:
            self._remember_dialog_location(project_path)
        self._project_dirty = False
        self._bump_project_version()
        self.status_var.set("Project loaded." if not merge else "Project merged into current session.")

    def _save_project_to_path(self, path: str) -> None:
        """Save current project to a compressed pickle file."""
        state = self._collect_project_state()
        with gzip.open(path, "wb") as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        self.current_project_path = path
        self._remember_dialog_location(path)
        self._project_dirty = False
        self.status_var.set(f"Project saved: {os.path.basename(path)}")

    def on_save_project(self) -> None:
        """Save the current project (including analysis results)."""
        if self.current_project_path:
            try:
                self._save_project_to_path(self.current_project_path)
            except Exception as exc:
                messagebox.showerror("Save project", f"Failed to save project:\n{exc}")
            return
        self.on_save_project_as()

    def on_save_project_as(self) -> None:
        """Save the current project to a new file."""
        path = filedialog.asksaveasfilename(
            title="Save project",
            defaultextension=".daproj",
            filetypes=[("Detrend Advisor project", "*.daproj"), ("All files", "*.*")],
            initialdir=self._get_dialog_initial_dir(
                os.path.dirname(os.path.abspath(self.current_project_path)) if self.current_project_path else None
            ),
        )
        if not path:
            return
        if not path.lower().endswith(".daproj"):
            path = path + ".daproj"
        try:
            self._save_project_to_path(path)
        except Exception as exc:
            messagebox.showerror("Save project", f"Failed to save project:\n{exc}")

    def on_open_project(self) -> None:
        """Open a saved project (restores imported data and cached analysis results)."""
        path = filedialog.askopenfilename(
            title="Open project",
            filetypes=[("Detrend Advisor project", "*.daproj"), ("All files", "*.*")],
            initialdir=self._get_dialog_initial_dir(
                os.path.dirname(os.path.abspath(self.current_project_path)) if self.current_project_path else None
            ),
        )
        if not path:
            return
        try:
            with gzip.open(path, "rb") as f:
                state = pickle.load(f)
            self._apply_project_state(state, project_path=path, merge=False)
        except Exception as exc:
            messagebox.showerror("Open project", f"Failed to open project:\n{exc}")

    def on_merge_project(self) -> None:
        """Merge another saved project into the current project."""
        path = filedialog.askopenfilename(
            title="Merge project into current",
            filetypes=[("Detrend Advisor project", "*.daproj"), ("All files", "*.*")],
            initialdir=self._get_dialog_initial_dir(
                os.path.dirname(os.path.abspath(self.current_project_path)) if self.current_project_path else None
            ),
        )
        if not path:
            return
        try:
            with gzip.open(path, "rb") as f:
                state = pickle.load(f)
            self._apply_project_state(state, project_path=None, merge=True)
            self._remember_dialog_location(path)
            self._mark_project_dirty()
        except Exception as exc:
            messagebox.showerror("Merge project", f"Failed to merge project:\n{exc}")

    def on_new_project(self) -> None:
        """Start a new project (clears the current workspace)."""
        if self.datasets or self.climate is not None:
            if not messagebox.askyesno("New project", "Clear current project and start a new one?"):
                return
        self._reset_project_state()

    def _maybe_convert_table_units(self, df: pd.DataFrame, path: str) -> pd.DataFrame:
        """Tables that are not Tucson files (CSV ...) keep their units; ask when they do not look like mm.

        Tucson RWL files are always converted to mm by their end markers.
        """
        if getattr(df, "attrs", {}).get("units") == "mm":
            return df
        try:
            vals = pd.Series(df.to_numpy(dtype=float).ravel())
            vals = vals[np.isfinite(vals) & (vals > 0)]
        except Exception:
            return df
        if vals.empty:
            return df
        med = float(vals.median())
        if med < 20.0:
            return df
        factor, unit = (1000.0, "0.001 mm (micrometres)") if med >= 400.0 else (100.0, "0.01 mm")
        ok = messagebox.askyesno(
            "Ring-width units",
            f"{os.path.basename(path)}: the median ring width is {med:.0f}, so the values look like "
            f"{unit}, not millimetres.\n\nConvert to mm (divide by {factor:.0f})?\n\n"
            "Yes = convert (recommended; Tucson .rwl files are always read in mm).\n"
            "No = keep the values as they are.",
            parent=self,
        )
        if not ok:
            return df
        out = df.astype(float) / factor
        out.attrs["units"] = "mm"
        return out

    def _load_data_files(
        self,
        paths: tuple[str, ...],
        *,
        activate_first_if_none: bool = True,
        preloaded: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> None:
        """Load one or more data files and add them as datasets to the current project.

        ``preloaded`` maps a path to an already parsed ring-width table (used by the
        ITRDB import, which reads Tucson files with per-series precision in mm).
        """
        if not paths:
            return
        loaded: list[str] = []
        errors: list[str] = []
        pith_reports: list[str] = []
        for path in paths:
            try:
                if preloaded is not None and path in preloaded:
                    df = _ensure_unique_sorted_year_index(preloaded[path].copy())
                else:
                    df = load_rwl_any(path)
                    df = self._maybe_convert_table_units(df, path)
            except Exception as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
                continue
            key = self._make_dataset_key(path)
            ids_df = self._build_ids_for_series_names([str(x) for x in list(df.columns)], rwl=df)
            pith_df, pith_report = self._autoload_pith(path, df)
            if pith_report:
                pith_reports.append(f"{os.path.basename(path)}: {pith_report}")
            self.datasets[key] = DatasetState(
                key=key,
                path=path,
                rwl=df,
                ids=ids_df,
                ids_reviewed=False,
                ids_source="auto" if bool(self.enable_tree_grouping_var.get()) else "identity",
                advisor_result=None,
                pith=pith_df,
            )
            loaded.append(key)
            self._remember_dialog_location(path)
            if self.output_dir is None and path:
                self.output_dir = os.path.dirname(path)

        if not loaded:
            msg = "Failed to load any selected files."
            if errors:
                msg += "\n\n" + "\n".join(errors)
            messagebox.showerror("Load error", msg)
            return

        self._refresh_dataset_ui_state()
        if loaded:
            if activate_first_if_none and self.active_dataset_key is None:
                self.set_active_dataset(loaded[0])
            else:
                self.set_active_dataset(loaded[-1])

        self._mark_project_dirty()
        self._bump_project_version()
        self.status_var.set(
            f"Loaded {len(loaded)} dataset(s). Total: {len(self.datasets)}"
            + (" | " + "; ".join(pith_reports) if pith_reports else "")
        )
        try:
            self.text_recommend.delete("1.0", tk.END)
            self.text_recommend.insert(tk.END, "Data loaded successfully. Choose goal and click 'Run analysis'.")
        except Exception:
            pass

        if bool(self.enable_tree_grouping_var.get()) and loaded and self.active_dataset_key in self.datasets:
            if messagebox.askyesno(
                "Tree groups",
                "Review automatic tree grouping from Series ID names now?",
            ):
                try:
                    self.on_edit_tree_groups()
                except Exception:
                    pass

        if errors:
            messagebox.showwarning("Load warnings", "Some files could not be loaded:\n\n" + "\n".join(errors))

    def on_import_data_replace(self) -> None:
        """Import data and replace the current project datasets."""
        paths = filedialog.askopenfilenames(
            title="Select one or more series files (CSV or Tucson .rwl)",
            filetypes=[
                ("Tree-ring files", "*.rwl *.tuc *.dec *.txt *.csv"),
                ("All files", "*.*"),
            ],
            initialdir=self._get_dialog_initial_dir(),
        )
        if not paths:
            return
        if self.datasets:
            if not messagebox.askyesno("Import data", "Replace current datasets with the newly imported ones?"):
                return
        self._reset_project_state()
        self._load_data_files(tuple(paths), activate_first_if_none=True)

    def on_add_data_to_project(self) -> None:
        """Import data and add the datasets to the current project."""
        paths = filedialog.askopenfilenames(
            title="Select one or more series files to add (CSV or Tucson .rwl)",
            filetypes=[
                ("Tree-ring files", "*.rwl *.tuc *.dec *.txt *.csv"),
                ("All files", "*.*"),
            ],
            initialdir=self._get_dialog_initial_dir(),
        )
        if not paths:
            return
        self._load_data_files(tuple(paths), activate_first_if_none=True)

    # ------------------------------------------------------------------
    # Pith (centre) information
    # ------------------------------------------------------------------
    def _autoload_pith(self, path: str, rwl: pd.DataFrame) -> tuple[Optional[pd.DataFrame], str]:
        """Read pith info written next to an RWL (PyTreeRingRecorder sidecars, .pos files)."""
        if _pith is None or not path or not os.path.isfile(str(path)):
            return None, ""
        series = [str(c) for c in rwl.columns]
        try:
            files = list(_pith.find_sidecars(str(path)))
            folder = os.path.dirname(os.path.abspath(str(path)))
            files += [f for f in _pith.files_in_folder(folder, recursive=False) if f.lower().endswith(".pos")]
            if not files:
                return None, ""
            found, _report = _pith.collect_pith_info(series, files, rwl=rwl, rwl_path=str(path))
            if not found:
                return None, ""
            table, _n = _pith.merge_pith(_pith.empty_pith_table(series), found)
            return table, _pith.pith_summary_text(table)
        except Exception as exc:
            return None, f"pith info not read ({exc})"

    def _po_for_dataset(self, ds: Any) -> Optional[pd.DataFrame]:
        """dplR po table for a dataset; NaN = unknown pith. None when no pith info exists."""
        table = getattr(ds, "pith", None)
        if _pith is None or table is None or not isinstance(table, pd.DataFrame) or table.empty:
            return None
        series = [str(c) for c in ds.rwl.columns]
        t = _pith.normalize_pith_table(table, series)
        if (t["status"] == "unknown").all():
            return None
        return pd.DataFrame({"series": series, "pith.offset": t["offset"].to_numpy(dtype=float)})

    def _set_dataset_pith(self, ds: Any, table: Optional[pd.DataFrame]) -> None:
        ds.pith = table
        # results depend on pith offsets -> force a new analysis run for this dataset
        ds.compat_signature = None
        self._mark_project_dirty()
        self._bump_project_version()

    def _import_pith_from_files(self, files: list[str]) -> None:
        if _pith is None:
            messagebox.showwarning("Pith info", "dendro_pith.py is not available.\n\n" + _PITH_IMPORT_ERROR)
            return
        if not self.active_dataset_key or self.active_dataset_key not in self.datasets:
            messagebox.showwarning("Pith info", "Load and select a dataset first.")
            return
        ds = self.datasets[self.active_dataset_key]
        series = [str(c) for c in ds.rwl.columns]
        found, report = _pith.collect_pith_info(series, files, rwl=ds.rwl, rwl_path=str(ds.path or ""))
        base = _pith.normalize_pith_table(getattr(ds, "pith", None), series)
        overwrite = True
        if (base["status"] != "unknown").any() and found:
            overwrite = messagebox.askyesno(
                "Pith info",
                "Some series already have pith information.\n\nReplace it with the imported values?\n"
                "(Manual edits are always kept.)",
            )
        table, n = _pith.merge_pith(base, found, overwrite_known=overwrite)
        self._set_dataset_pith(ds, table)
        if hasattr(self, "tree_grouping_tree") and getattr(self, "_pith_working_df", None) is not None:
            try:
                self._pith_working_df = table.copy()
                self._populate_tree_grouping_treeview()
            except Exception:
                pass
        messagebox.showinfo(
            "Pith info",
            f"Updated {n} series.\n{_pith.pith_summary_text(table)}\n\n" + "\n".join(report),
        )

    def on_import_pith_files(self) -> None:
        """Import pith information from .pos / .pith.csv / po CSV files."""
        paths = filedialog.askopenfilenames(
            title="Select .pos or pith CSV files",
            filetypes=[("Pith information", "*.pos *.csv"), ("All files", "*.*")],
            initialdir=self._get_dialog_initial_dir(),
        )
        if paths:
            self._remember_dialog_location(paths[0])
            self._import_pith_from_files(list(paths))

    def on_import_pith_folder(self) -> None:
        """Import pith information from all .pos / pith CSV files in a folder (with subfolders)."""
        if _pith is None:
            messagebox.showwarning("Pith info", "dendro_pith.py is not available.\n\n" + _PITH_IMPORT_ERROR)
            return
        folder = filedialog.askdirectory(title="Folder with .pos / pith files", initialdir=self._get_dialog_initial_dir())
        if not folder:
            return
        self._remember_dialog_location(folder, is_dir=True)
        files = _pith.files_in_folder(folder, recursive=True)
        if not files:
            messagebox.showinfo("Pith info", "No .pos or .pith.csv files found in this folder.")
            return
        self._import_pith_from_files(files)

    def on_import_from_itrdb(self) -> None:
        """Open the ITRDB search / download window (non-modal)."""
        if _online_tk is None:
            messagebox.showwarning(
                "ITRDB",
                "Online import module is not available.\n\n"
                "Put dendro_online.py, dendro_online_tk.py, dendro_geoexport.py and "
                "dendro_online_countries.geojson into the dendro_modules folder next to detrend_advisor.\n\n" + _ONLINE_IMPORT_ERROR,
            )
            return
        dlg = getattr(self, "_itrdb_dialog", None)
        try:
            if dlg is not None and dlg.winfo_exists():
                dock = getattr(self, "_itrdb_dock", None)
                if dock is not None:
                    dock.show()
                else:
                    dlg.deiconify()
                    dlg.lift()
                    dlg.focus_force()
                return
        except Exception:
            pass
        default_dir = os.path.join(self.output_dir, "ITRDB") if self.output_dir else None
        self._itrdb_dialog = _online_tk.ItrdbImportDialog(
            self, on_import=self._on_itrdb_entries, default_dir=default_dir
        )
        self._itrdb_dock = None
        nb = getattr(self, "notebook", None)
        if (_uihelp is not None and hasattr(_uihelp, "TkTabDock") and nb is not None
                and hasattr(self._itrdb_dialog, "set_dock")):
            # default: a tab of the main window; "Detach to window" makes it a separate window
            def _closed() -> None:
                self._itrdb_dock = None
            try:
                dock = _uihelp.TkTabDock(nb, self._itrdb_dialog, "ITRDB (online)", on_closed=_closed)
                self._itrdb_dialog.set_dock(dock)
                dock.attach()
                self._itrdb_dock = dock
            except Exception as exc:
                print(f"[itrdb] docking failed, using a separate window: {exc}")

    def _on_itrdb_entries(self, entries: list) -> None:
        """Add ring-width tables downloaded from ITRDB to the project."""
        if not entries:
            return
        paths = tuple(str(e["path"]) for e in entries)
        preloaded = {str(e["path"]): e["rwl"] for e in entries}
        # remember site coordinates: RingClim uses them for climate downloads and spatial correlation
        locs = getattr(self, "_dataset_locations", None)
        if locs is None:
            locs = self._dataset_locations = {}
        for e in entries:
            meta = e.get("meta") or {}
            if meta.get("lat") is not None and meta.get("lon") is not None:
                info = {"lat": float(meta["lat"]), "lon": float(meta["lon"]),
                        "site_code": meta.get("site_code", ""), "site_name": meta.get("site_name", ""),
                        "species": meta.get("species", ""), "elevation_m": meta.get("elevation_m")}
                locs[os.path.normcase(os.path.abspath(str(e["path"])))] = info
                locs[os.path.splitext(os.path.basename(str(e["path"])))[0].lower()] = info
        self._load_data_files(paths, activate_first_if_none=True, preloaded=preloaded)

    def _refresh_dataset_ui_state(self) -> None:
        """Refresh dataset widgets after adding or removing datasets."""
        keys = list(self.datasets.keys())
        if hasattr(self, "dataset_combo"):
            self.dataset_combo["values"] = keys
            if self.active_dataset_key in keys:
                self.dataset_var.set(self.active_dataset_key)
            elif keys:
                self.dataset_var.set(keys[0])
        if hasattr(self, "compare_listbox"):
            prev: set[str] = set()
            try:
                prev = {self.compare_listbox.get(i) for i in self.compare_listbox.curselection()}
            except Exception:
                prev = set()
            self.compare_listbox.delete(0, tk. END)
            for k in keys:
                self.compare_listbox.insert(tk. END, k)
            restored = 0
            for idx, k in enumerate(keys):
                if k in prev:
                    self.compare_listbox.selection_set(idx)
                    restored += 1
            if restored == 0 and keys:
                # Default to comparing all datasets.
                self.compare_listbox.selection_set(0, tk. END)
        self._update_compare_method_choices()

    def on_dataset_selected(self) -> None:
        """Handle dataset selection from the top combobox."""
        key = self.dataset_var.get().strip()
        if not key:
            return
        self.set_active_dataset(key)

    def set_active_dataset(self, key: str) -> None:
        """Switch the active dataset displayed in the GUI."""
        ds = self.datasets.get(key)
        if ds is None:
            return
        self._ensure_dataset_ids_initialized(ds)
        self.active_dataset_key = key
        self.dataset_var.set(key)
        self.rwl = ds.rwl
        self.rwl_path = ds.path
        self.ids = getattr(ds, "ids", None)
        self.advisor_result = self._resolve_dataset_display_result(ds)
        if self.output_dir is None and ds.path:
            self.output_dir = os.path.dirname(ds.path)
        self._update_min_series_info_for_active_dataset()
        self._sync_method_widgets_for_active_dataset()
        self._update_recommendation_text()
        self._update_powt_tab()
        self._update_structure_plots()
        self.update_chron_plots()
        self._update_signal_plots()
        self.update_compare_plots()
        try:
            if self.notebook.select() == str(self.tab_climate):
                self.update_climate_plots()
        except Exception:
            pass

    def _sync_method_widgets_for_active_dataset(self) -> None:
        """Update method-related widgets based on the active dataset results."""
        active_result = self.advisor_result
        if active_result is None:
            methods = ["best"]
        else:
            methods = ["best"] + sorted(active_result.candidates.keys())
        if hasattr(self, "chron_method_combo"):
            self.chron_method_combo["values"] = methods
            if self.chron_method_var.get() not in methods:
                self.chron_method_var.set("best")
        if hasattr(self, "signal_method_combo"):
            self.signal_method_combo["values"] = methods
            if self.signal_method_var.get() not in methods:
                self.signal_method_var.set("best")
        if hasattr(self, "climate_method_combo"):
            self.climate_method_combo["values"] = methods
            if self.climate_method_var.get() not in methods:
                self.climate_method_var.set("best")
        if hasattr(self, "compare_method_combo"):
            self.compare_method_combo["values"] = methods
            if self.compare_method_var.get() not in methods:
                self.compare_method_var.set("best")
        if hasattr(self, "guide_method_combo") and active_result is not None:
            self.guide_method_combo["values"] = sorted(active_result.candidates.keys())
            if self.guide_method_var.get() not in self.guide_method_combo["values"]:
                vals = list(self.guide_method_combo["values"])
                if vals:
                    self.guide_method_var.set(vals[0])
        self.guide_goal_var.set(self.goal_var.get())
        self._update_guide_texts()

    def _update_compare_method_choices(self) -> None:
        """Update compare method choices based on the active dataset."""
        if not hasattr(self, "compare_method_combo"):
            return
        if self.advisor_result is None:
            self.compare_method_combo["values"] = ["best"]
            self.compare_method_var.set("best")
        else:
            vals = ["best"] + sorted(self.advisor_result.candidates.keys())
            self.compare_method_combo["values"] = vals
            if self.compare_method_var.get() not in vals:
                self.compare_method_var.set("best")

    def _on_compare_mode_changed(self) -> None:
        """Enable or disable compare method control depending on compare mode."""
        if not hasattr(self, "compare_method_combo"):
            return
        mode = self.compare_mode_var.get()
        if mode == "all_methods":
            self.compare_method_combo.config(state="disabled")
        else:
            self.compare_method_combo.config(state="readonly")
        self.update_compare_plots()

    def _compare_select_all(self) -> None:
        """Select all datasets in the compare list."""
        if not hasattr(self, "compare_listbox"):
            return
        self.compare_listbox.selection_set(0, tk. END)
        self.update_compare_plots()

    def _compare_clear_selection(self) -> None:
        """Clear dataset selection in the compare list."""
        if not hasattr(self, "compare_listbox"):
            return
        self.compare_listbox.selection_clear(0, tk. END)
        self.update_compare_plots()

    def _resolve_export_method_key(self) -> Optional[str]:
        """Resolve the currently requested method key for RWI export."""
        if self.advisor_result is None:
            return None
        method_key = "best"
        if hasattr(self, "chron_method_var"):
            method_key = str(self.chron_method_var.get()).strip() or "best"
        if method_key == "best":
            method_key = self.advisor_result.best_key
        if method_key not in self.advisor_result.candidates:
            method_key = self.advisor_result.best_key
        return method_key


    def _displayed_results_use_powt(self) -> bool:
        """True when the results shown for the active dataset are the POWT-transformed ones."""
        ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
        shown = getattr(self, "advisor_result", None)
        if ds is not None and shown is not None:
            if shown is getattr(ds, "advisor_result_no_powt", None):
                return False
            for name in (
                "advisor_result_with_powt",
                "advisor_result_with_powt_universal",
                "advisor_result_with_powt_cook",
            ):
                if shown is getattr(ds, name, None):
                    return True
        if not bool(self.enable_dplr_powt_var.get()):
            return False
        if not bool(self.show_powt_results_var.get()):
            return False
        if ds is not None and str(getattr(ds, "displayed_result_mode", "") or "") == "no_powt":
            return False
        return True

    def _current_powt_display_state_key(self) -> str:
        """Return a stable key for the currently displayed POWT state."""
        return "powt_on" if self._displayed_results_use_powt() else "powt_off"

    def _resolve_export_result_state_suffix(self) -> str:
        """Return a stable suffix for the currently displayed POWT state."""
        return "powt_on" if self._displayed_results_use_powt() else "powt_off"

    def _iter_dataset_result_states(
        self,
        ds: Any,
        include_both: bool = False,
    ) -> list[tuple[str, Optional[AdvisorResult]]]:
        """Yield exportable advisor results for one dataset."""
        if ds is None:
            return []
        if include_both:
            items: list[tuple[str, Optional[AdvisorResult]]] = [
                ("no_powt", getattr(ds, "advisor_result_no_powt", None)),
                ("with_powt", getattr(ds, "advisor_result_with_powt", None)),
            ]
            out: list[tuple[str, Optional[AdvisorResult]]] = []
            seen: set[int] = set()
            for state_key, ar in items:
                if ar is None:
                    continue
                obj_id = id(ar)
                if obj_id in seen:
                    continue
                seen.add(obj_id)
                out.append((state_key, ar))
            if out:
                return out
        return [("displayed", self._resolve_dataset_display_result(ds))]

    def _get_signal_diagnostics_df(self, method_key: str, window: int) -> pd.DataFrame:
        """Get cached EPS/Rbar/depth diagnostics for one method and window."""
        if self.advisor_result is None:
            return pd.DataFrame()
        win = int(max(3, window))
        resolved_method = self.advisor_result.best_key if method_key == "best" else method_key

        cache_key = (
            self._project_version,
            self.active_dataset_key,
            self._current_powt_display_state_key(),
            resolved_method,
            win,
        )

        cached = self._signal_diag_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        df = _get_cached_signal_diagnostics_for_result(self.advisor_result, resolved_method, win)
        self._signal_diag_cache[cache_key] = df.copy()
        return df

    # --------------------------- Actions -----------------------------------
    def on_load_data(self) -> None:

        """Legacy handler for data import.

        This method is kept for backward compatibility with older UI bindings.
        """
        self.on_import_data_replace()

    def on_load_climate(self) -> None:
        """Handle climate import."""
        path = filedialog.askopenfilename(
            title="Select climate CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=self._get_dialog_initial_dir(
                os.path.dirname(os.path.abspath(self.climate_path)) if self.climate_path else None
            ),
        )
        if not path:
            return
        try:
            ser = load_climate_csv(path)
        except Exception as exc:
            messagebox.showerror(
                "Climate load error", f"Failed to load CSV:\n{exc}"
            )
            return
        self.climate = ser
        self.climate_path = path
        self._remember_dialog_location(path)
        self._mark_project_dirty()
        self._bump_project_version()
        self.status_var.set(
            f"Climate loaded: {os.path.basename(path)} "
            f"({ser.index.min()}–{ser.index.max()})"
        )
    def on_choose_output_dir(self) -> None:
        """Choose directory for exports."""
        d = filedialog.askdirectory(
            title="Choose export folder",
            initialdir=self._get_dialog_initial_dir(
                self.output_dir
                or (os.path.dirname(self.rwl_path) if self.rwl_path else None)
            ),
        )
        if not d:
            return
        self.output_dir = d
        self._remember_dialog_location(d, is_dir=True)
        self.status_var.set(f"Export folder: {d}")
        self._mark_project_dirty()

    # ------------------------------------------------------------------ saved files
    def open_last_export_folder(self) -> None:
        p = getattr(self, "_last_export_path", None) or getattr(self, "output_dir", None)
        if not p:
            messagebox.showinfo("Open folder", "Nothing has been exported in this session yet.")
            return
        open_in_file_manager(p)

    def notify_saved(self, title: str, text: str, path: str) -> None:
        """Message with a clickable location; the link also stays in the status line."""
        self._last_export_path = str(path)
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            self.lbl_open_export.configure(text=f"Open export folder: {folder}")
        except Exception:
            pass
        win = tk. Toplevel(self)
        win.title(title)
        win.transient(self)
        win.resizable(False, False)
        frm = ttk. Frame(win, padding=14)
        frm.pack(fill=tk. BOTH, expand=True)
        ttk. Label(frm, text=text, justify="left", wraplength=560).pack(anchor="w")
        ttk. Label(frm, text="Location (click to open):").pack(anchor="w", pady=(10, 0))
        link = tk. Label(frm, text=str(path), fg="#1a56db", cursor="hand2", justify="left", wraplength=560,
                         font=("Segoe UI", 9, "underline"))
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda _e: open_in_file_manager(path))
        btns = ttk. Frame(frm)
        btns.pack(fill=tk. X, pady=(12, 0))
        ttk. Button(btns, text="OK", command=win.destroy).pack(side=tk. RIGHT)
        ttk. Button(btns, text="Open folder", command=lambda: (open_in_file_manager(path), win.destroy())).pack(
            side=tk. RIGHT, padx=6)
        ttk. Label(frm, text="The link stays in the status line and in Actions > Open last export folder.",
                   foreground="#666").pack(anchor="w", pady=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())
        try:
            win.grab_set()
        except Exception:
            pass

    def on_export_current_rwi_matrix(self) -> None:
        """Export the currently selected detrended tree-level RWI table."""
        if self.advisor_result is None:
            messagebox.showwarning("No results", "Run analysis before exporting RWI.")
            return
        if not self.output_dir:
            self.on_choose_output_dir()
            if not self.output_dir:
                return
        method_key = self._resolve_export_method_key()
        if method_key is None:
            messagebox.showwarning("No method", "No detrending method is available.")
            return
        cand = self.advisor_result.candidates.get(method_key)
        if cand is None:
            messagebox.showwarning("No method", f"Method '{method_key}' is not available.")
            return
        dataset_name = self.active_dataset_key or "dataset"
        safe_dataset = self._sanitize_filename(dataset_name)
        safe_method = self._sanitize_filename(method_key)
        state_suffix = self._resolve_export_result_state_suffix()
        out_path = os.path.join(
            self.output_dir,
            f"{safe_dataset}__{safe_method}__{state_suffix}__rwi_tree_level.csv",
        )
        try:
            df_out = cand.rwi.copy()
            df_out.insert(0, "Year", df_out.index.astype(int))
            df_out.to_csv(out_path, index=False)
        except Exception as exc:
            messagebox.showerror("Export error", f"Failed to export RWI table:\n{exc}")
            return
        self.status_var.set(f"Saved tree-level RWI ({state_suffix}): {out_path}")
        self.notify_saved("Export complete", f"Saved detrended tree-level RWI ({state_suffix}).", out_path)

    def on_export_current_rwi_matrix(self) -> None:
        """Export the currently selected detrended RWI table for all series."""
        if self.advisor_result is None:
            messagebox.showwarning("No results", "Run analysis before exporting RWI.")
            return
        if not self.output_dir:
            self.on_choose_output_dir()
            if not self.output_dir:
                return
        method_key = self._resolve_export_method_key()
        if method_key is None:
            messagebox.showwarning("No method", "No detrending method is available.")
            return
        cand = self.advisor_result.candidates.get(method_key)
        if cand is None:
            messagebox.showwarning("No method", f"Method '{method_key}' is not available.")
            return
        dataset_name = self.active_dataset_key or "dataset"
        safe_dataset = self._sanitize_filename(dataset_name)
        safe_method = self._sanitize_filename(method_key)
        out_path = os.path.join(self.output_dir, f"{safe_dataset}__{safe_method}__rwi_all_series.csv")
        try:
            df_out = cand.rwi.copy()
            df_out.insert(0, "Year", df_out.index.astype(int))
            df_out.to_csv(out_path, index=False)
        except Exception as exc:
            messagebox.showerror("Export error", f"Failed to export RWI table:\n{exc}")
            return
        self.status_var.set(f"Saved all-series RWI: {out_path}")
        self.notify_saved("Export complete", "Saved detrended all-series RWI.", out_path)

    def on_run(self) -> None:
        """Run advisor analysis for all imported datasets."""
        if not self.datasets:
            messagebox.showwarning("No data", "Please import data first (CSV or .rwl).")
            return
        if self._analysis_running:
            messagebox.showinfo("Analysis already running", "Please wait for the current run to finish.")
            return
        goal = self.goal_var.get()
        if goal not in (
            "highfreq",
            "mixed",
            "lowfreq",
            "temp_recon",
            "runoff_recon",
            "drought_recon",
            "variance_check",
        ):
            messagebox.showwarning("Invalid goal", "Select an analysis goal.")
            return
        # Keep POWT fully controlled by the single main POWT toggle.
        self._sync_powt_tab_to_run_controls(
            force_enable=bool(self.enable_dplr_powt_var.get())
        )
        self._apply_dplr_parallel_workers_env()
        cfg = self._resolve_run_compat_config()
        if cfg is None:
            return
        min_series_for_detrending = self._get_min_series_for_detrending()
        min_series_count_mode = self._get_min_series_count_mode()
        score_profile = self._get_current_score_profile()
        compat_sig = (
            _compat_signature(cfg)
            + "|score="
            + self._score_profile_signature()
            + f"|min_series={int(min_series_for_detrending)}"
            + f"|unknown_pith={self.unknown_pith_policy_var.get()}"
            + "|custom_methods=" + ",".join(sorted(self.custom_method_keys))
        )
        self._run_unknown_pith_policy = str(self.unknown_pith_policy_var.get() or "present")
        selected_method_keys = self._selected_method_keys_for_run(cfg)

        # Only analyze datasets that do not already have results for the current goal.
        to_run: list[tuple[str, DatasetState]] = []
        for key, ds in self.datasets.items():
            ar = ds.advisor_result
            has_powt_compare = (
                getattr(ds, "advisor_result_no_powt", None) is not None
                and getattr(ds, "advisor_result_with_powt", None) is not None
                and str(getattr(ds, "powt_recommendation_flag", "") or "").strip() not in ("", "unknown")
            )
            if (
                ar is not None
                and getattr(ar, "goal", None) == goal
                and getattr(ds, "compat_signature", None) == compat_sig
                and has_powt_compare
            ):
                missing_no_powt = _missing_method_keys_for_result(
                    getattr(ds, "advisor_result_no_powt", None),
                    selected_method_keys,
                )
                missing_powt_universal = _missing_method_keys_for_result(
                    getattr(ds, "advisor_result_with_powt_universal", None),
                    selected_method_keys,
                )
                missing_powt_cook = _missing_method_keys_for_result(
                    getattr(ds, "advisor_result_with_powt_cook", None),
                    selected_method_keys,
                )
                if (
                    not missing_no_powt
                    and not missing_powt_universal
                    and not missing_powt_cook
                ):
                    continue
            to_run.append((key, ds))

        if not to_run:
            backend_label = "dplR" if cfg.compat == "dplR" else "Python"
            self.status_var.set(f"Analysis is up-to-date for goal: {goal} (backend={backend_label})")
            if self.active_dataset_key is None and self.datasets:
                self.active_dataset_key = next(iter(self.datasets.keys()))
            if self.active_dataset_key is not None:
                self.set_active_dataset(self.active_dataset_key)
            self._refresh_dataset_ui_state()
            return

        self._start_runtime_log_session("Analysis run")
        n_workers = _choose_r_parallel_workers(len(selected_method_keys or [])) if cfg.compat == "dplR" else 1
        methods_label = ", ".join(sorted(selected_method_keys)) if selected_method_keys else "all enabled methods"
        self._append_runtime_log(
            f"Analysis started | backend={cfg.compat} | goal={goal} | "
            f"datasets={len(to_run)}/{len(self.datasets)} | workers={n_workers}"
        )
        self._append_runtime_log(
            "Score profile | "
            f"HF cut={score_profile['high_cutoff_years']:.1f}, "
            f"LF cut={score_profile['low_cutoff_years']:.1f}, "
            f"W_HF={score_profile['w_var_high']:.2f}, "
            f"W_LF={score_profile['w_var_low']:.2f}, "
            f"W_EPS={score_profile['w_eps']:.2f}, "
            f"W_Rbar={score_profile['w_rbar']:.2f}, "
            f"W_Clim={score_profile['w_climate']:.2f}"
        )
        self._append_runtime_log(
            "Minimum detrending threshold | "
            f"count={int(min_series_for_detrending)} | mode={str(min_series_count_mode)}"
        )
        if cfg.compat == "dplR":
            self._append_runtime_log(f"dplR method set: {methods_label}")
            if bool(cfg.powt_enabled):
                self._append_runtime_log(
                    f"dplR POWT enabled for run | method={cfg.powt_method} | rescale={cfg.powt_rescale}"
                )
        self.status_var.set(
            f"Running analysis for {len(to_run)}/{len(self.datasets)} dataset(s) "
            f"(backend={cfg.compat}, workers={n_workers}, skipping {len(self.datasets) - len(to_run)}, "
            f"min {str(min_series_count_mode)}={int(min_series_for_detrending)})..."
        )
        self.update_idletasks()
        self._analysis_running = True
        self._set_run_button_enabled(False)
        self._analysis_thread = threading.Thread(
            target=self._analysis_worker,
            args=(
                to_run,
                goal,
                cfg,
                compat_sig,
                selected_method_keys,
                n_workers,
                score_profile,
                int(min_series_for_detrending),
                str(min_series_count_mode),
            ),
            daemon=True,
        )
        self._analysis_thread.start()
        if self._analysis_poll_job is None:
            self._analysis_poll_job = self.after(80, self._pump_analysis_queue)

    def on_export(self) -> None:
        """Export chronologies, RWI and a markdown report."""
        if not self.datasets:
            messagebox.showwarning("No data", "Import data and run analysis before exporting.")

            return
        # Default export folder: import folder
        if len(self.datasets) == 1:
            ds_list = list(self.datasets.values())
        else:
            ds_list = [ds for ds in self.datasets.values() if ds.advisor_result is not None]
        export_items: list[tuple[Any, str, AdvisorResult]] = []
        include_both_states = bool(self.export_both_powt_modes_var.get())
        for ds in ds_list:
            for state_key, advisor_result in self._iter_dataset_result_states(
                ds,
                include_both=include_both_states,
            ):
                if advisor_result is None:
                    continue
                export_items.append((ds, state_key, advisor_result))
        if not export_items:
            messagebox.showwarning("No results", "Run analysis before exporting.")
            return
        # Default export folder: use the active dataset folder
        if self.output_dir is None:
            active_ds = self.datasets.get(self.active_dataset_key or "")
            if active_ds is not None and active_ds.path:
                self.output_dir = os.path.dirname(active_ds.path)
                self.status_var.set(f"Export folder: {self.output_dir}")
            else:
                messagebox.showwarning(
                    "No export folder",
                    "Choose export folder first (Project -> Choose export folder...).",
                )
                return
        mode = _ask_export_mode(self)
        if mode is None:
            return
        excel_engine = _choose_excel_engine()
        if mode in ("excel_only", "csv_excel") and excel_engine is None:
            install_now = messagebox.askyesno(
                "Excel export engine missing",
                "Excel export requires 'xlsxwriter' (recommended) or 'openpyxl'.\n\n"
                "Install now using pip?",
            )
            if install_now:
                excel_engine = _ensure_excel_engine()

        if mode == "excel_only" and excel_engine is None:
            messagebox.showwarning(
                "Excel export unavailable",
                "Excel export is unavailable (no 'xlsxwriter' / 'openpyxl'). "
                "Falling back to CSV export only.",
            )
            mode = "csv_excel"
        elif mode == "csv_excel" and excel_engine is None:
            messagebox.showwarning(
                "Excel export unavailable",
                "Excel export is unavailable (no 'xlsxwriter' / 'openpyxl'). "
                "Continuing with CSV export only.",
            )

        exported = 0
        errors: list[str] = []
        for ds, state_key, advisor_result in export_items:
            dataset_name = ds.key
            if state_key == "no_powt":
                dataset_name = f"{dataset_name}__powt_off"
            elif state_key == "with_powt":
                dataset_name = f"{dataset_name}__powt_on"
            input_name = os.path.basename(ds.path) if ds.path else dataset_name
            base = os.path.join(self.output_dir, dataset_name)
            powt_result = getattr(ds, "powt_preview_result", None)
            if powt_result is None:
                try:
                    cache_key = (
                        str(dataset_name),
                        str(self.dplr_powt_method_var.get()).strip() or str(DEFAULT_COMPAT_CONFIG.powt_method),
                        bool(self.dplr_powt_rescale_var.get()),
                    )
                    cached_powt = self._powt_preview_cache.get(cache_key)
                    if cached_powt is not None:
                        powt_result = cached_powt
                        setattr(ds, "powt_preview_result", cached_powt)
                except Exception:
                    pass
            best = advisor_result.candidates[advisor_result.best_key]
            # ---------------- CSV exports (optional) ----------------
            if mode == "csv_excel":
                chron_files = {
                    f"{base}_chron_std_best.csv": best.chronology_std,
                    f"{base}_chron_res_best.csv": best.chronology_res,
                }
                if advisor_result.rcs_std is not None:
                    chron_files[f"{base}_chron_rcs_std.csv"] = advisor_result.rcs_std
                if advisor_result.rcs_sf_std is not None:
                    chron_files[f"{base}_chron_rcs_signal_free_std.csv"] = advisor_result.rcs_sf_std
                try:
                    for pth, chron in chron_files.items():
                        chron.to_csv(pth, header=["value"])
                except Exception as exc:
                    errors.append(f"{dataset_name}: failed to save chronologies ({exc})")
                    continue
                # Per-method chronologies
                for key, cand in advisor_result.candidates.items():
                    path_std = f"{base}_chron_{key}_std.csv"
                    path_res = f"{base}_chron_{key}_res.csv"
                    try:
                        cand.chronology_std.to_csv(path_std, header=["value"])
                        cand.chronology_res.to_csv(path_res, header=["value"])
                    except Exception as exc:
                        errors.append(f"{dataset_name}: failed to save chronologies for method {key} ({exc})")
                        continue
                # RWI for all methods
                for key, cand in advisor_result.candidates.items():
                    path_rwi = f"{base}_rwi_{key}.csv"
                    try:
                        cand.rwi.to_csv(path_rwi)
                    except Exception as exc:
                        errors.append(f"{dataset_name}: failed to save RWI for method {key} ({exc})")
                        continue
                # Export EPS/Rbar/depth diagnostics for best method (if available)
                if getattr(advisor_result, "eps_rbar_best", None) is not None:
                    eps_path = f"{base}_eps_rbar_best.csv"
                    try:
                        advisor_result.eps_rbar_best.to_csv(eps_path)
                    except Exception as exc:
                        errors.append(f"{dataset_name}: failed to save EPS/Rbar for best ({exc})")
                # Export EPS/Rbar/depth diagnostics for ALL methods
                for key, cand in advisor_result.candidates.items():
                    eps_path = f"{base}_eps_rbar_{key}.csv"
                    try:
                        eps_df = _get_cached_signal_diagnostics_for_result(advisor_result, key, 50)
                        if eps_df is not None and not eps_df.empty:
                            eps_df.to_csv(eps_path)
                    except Exception as exc:
                        errors.append(f"{dataset_name}: failed to save EPS/Rbar for method {key} ({exc})")
                        continue
                if powt_result is not None:
                    try:
                        powt_result.transformed.to_csv(f"{base}_powt_transformed.csv")
                        pd.DataFrame(
                            {
                                "series": powt_result.power.index.astype(str),
                                "power": pd.to_numeric(powt_result.power, errors="coerce").to_numpy(dtype=float),
                            }
                        ).to_csv(f"{base}_powt_power.csv", index=False)
                        pd.DataFrame(
                            {
                                "series": powt_result.raw_skew.index.astype(str),
                                "skew": pd.to_numeric(powt_result.raw_skew, errors="coerce").to_numpy(dtype=float),
                            }
                        ).to_csv(f"{base}_powt_raw_skew.csv", index=False)
                        pd.DataFrame(
                            {
                                "series": powt_result.powt_skew.index.astype(str),
                                "skew": pd.to_numeric(powt_result.powt_skew, errors="coerce").to_numpy(dtype=float),
                            }
                        ).to_csv(f"{base}_powt_transformed_skew.csv", index=False)
                    except Exception as exc:
                        errors.append(f"{dataset_name}: failed to save POWT export ({exc})")
            # ---------------- Excel export (optional) ----------------
            if excel_engine is not None and mode in ("excel_only", "csv_excel"):
                excel_path = f"{base}_all_chronologies.xlsx"
                try:
                    with pd. ExcelWriter(excel_path, engine=excel_engine) as writer:
                        best.chronology_std.to_frame(name="std").to_excel(writer, sheet_name="best_std")
                        best.chronology_res.to_frame(name="res").to_excel(writer, sheet_name="best_res")
                        best.rwi.to_excel(writer, sheet_name="best_rwi")
                        for key, cand in advisor_result.candidates.items():
                            cand.chronology_std.to_frame(name="std").to_excel(writer, sheet_name=f"{key}_std"[:31])
                            cand.chronology_res.to_frame(name="res").to_excel(writer, sheet_name=f"{key}_res"[:31])
                            cand.rwi.to_excel(writer, sheet_name=f"{key}_rwi"[:31])
                            # Per-method EPS/Rbar/depth diagnostics
                            try:
                                eps_df = _get_cached_signal_diagnostics_for_result(advisor_result, key, 50)
                                if eps_df is not None and not eps_df.empty:
                                    eps_df.to_excel(writer, sheet_name=f"{key}_eps_rbar"[:31])
                            except Exception:
                                # Skip EPS export for this method if computation fails
                                pass
                        if advisor_result.rcs_std is not None:
                            advisor_result.rcs_std.to_frame(name="std").to_excel(writer, sheet_name="rcs_std")
                        if advisor_result.rcs_sf_std is not None:
                            advisor_result.rcs_sf_std.to_frame(name="std").to_excel(writer, sheet_name="rcs_sf_std")
                        if getattr(advisor_result, "eps_rbar_best", None) is not None:
                            advisor_result.eps_rbar_best.to_excel(writer, sheet_name="eps_rbar_best")
                        if powt_result is not None:
                            powt_result.transformed.to_excel(writer, sheet_name="powt_transformed")
                            pd.DataFrame(
                                {
                                    "series": powt_result.power.index.astype(str),
                                    "power": pd.to_numeric(powt_result.power, errors="coerce").to_numpy(dtype=float),
                                }
                            ).to_excel(writer, sheet_name="powt_power", index=False)
                            pd.DataFrame(
                                {
                                    "series": powt_result.raw_skew.index.astype(str),
                                    "skew": pd.to_numeric(powt_result.raw_skew, errors="coerce").to_numpy(dtype=float),
                                }
                            ).to_excel(writer, sheet_name="powt_raw_skew", index=False)
                            pd.DataFrame(
                                {
                                    "series": powt_result.powt_skew.index.astype(str),
                                    "skew": pd.to_numeric(powt_result.powt_skew, errors="coerce").to_numpy(dtype=float),
                                }
                            ).to_excel(writer, sheet_name="powt_transformed_skew", index=False)
                            pd.DataFrame(
                                {
                                    "metric": ["method", "rescale", "series_count", "year_start", "year_end"],
                                    "value": [
                                        str(powt_result.method),
                                        bool(powt_result.rescale),
                                        int(powt_result.transformed.shape[1]),
                                        int(powt_result.transformed.index.min()) if not powt_result.transformed.empty else np.nan,
                                        int(powt_result.transformed.index.max()) if not powt_result.transformed.empty else np.nan,
                                    ],
                                }
                            ).to_excel(writer, sheet_name="powt_summary", index=False)
                except Exception as exc:
                    errors.append(f"{dataset_name}: failed to save Excel file ({exc})")
            # ---------------- Markdown report (always) ----------------
            report_text = generate_markdown_report(
                advisor=advisor_result,
                dataset_name=dataset_name,
                climate_present=self.climate is not None,
                input_filename=input_name,
            )
            report_path = f"{base}_report.md"
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report_text)
            except Exception as exc:
                errors.append(f"{dataset_name}: failed to save report ({exc})")
                continue
            exported += 1
        # Export one project-level statistics file covering every analyzed dataset.
        try:
            stats_errors, stats_paths = self._export_project_statistics(
                export_items,
                excel_engine=excel_engine,
                write_csv=(mode == "csv_excel" or excel_engine is None),
            )
            errors.extend(stats_errors)
            for stats_path in stats_paths:
                self._append_runtime_log(f"Project statistics exported: {stats_path}")
        except Exception as exc:
            errors.append(f"project statistics: {exc}")
        # Export compare/correlation results as a separate file (project-level).
        try:
            compare_df = self._build_compare_correlations_export_df()
            if compare_df is not None and self.output_dir:
                compare_path = os.path.join(self.output_dir, "compare_correlations.csv")
                compare_df.to_csv(compare_path, index=False)
        except Exception as exc:
            errors.append(f"compare_correlations.csv: {exc}")

        if exported == 0:
            messagebox.showerror("Export error", "Export failed for all datasets.")
            return
        self._clear_figure_legends(self.fig_signal)
        if errors:
            messagebox.showwarning("Export warnings", "Some exports had issues:\n\n" + "\n".join(errors))
        self.notify_saved("Export complete", "All outputs of the project were exported.", self.output_dir)
 
    @staticmethod
    def _stats_series_summary(values: Any) -> Dict[str, Any]:
        """Return basic descriptive statistics for one year-indexed series."""
        out: Dict[str, Any] = {
            "n_values": 0,
            "first_year": np.nan,
            "last_year": np.nan,
            "mean": np.nan,
            "std": np.nan,
            "min": np.nan,
            "max": np.nan,
            "ar1": np.nan,
        }
        try:
            s = pd.to_numeric(pd.Series(values), errors="coerce")
            s = s[np.isfinite(s.to_numpy(dtype=float))]
        except Exception:
            return out
        if s.empty:
            return out
        arr = s.to_numpy(dtype=float)
        out["n_values"] = int(arr.size)
        out["mean"] = float(np.mean(arr))
        out["std"] = float(np.std(arr, ddof=1)) if arr.size > 1 else np.nan
        out["min"] = float(np.min(arr))
        out["max"] = float(np.max(arr))
        try:
            years = pd.to_numeric(pd.Index(s.index), errors="coerce")
            years = years[~pd.isna(years)]
            if len(years) > 0:
                y0 = int(np.min(years))
                y1 = int(np.max(years))
                out["first_year"] = y0
                out["last_year"] = y1
                s_int = pd.Series(arr, index=pd.Index(s.index).astype(int))
                s_int = s_int[~s_int.index.duplicated(keep="first")]
                full = s_int.reindex(range(y0, y1 + 1))
                if int(full.notna().sum()) >= 3:
                    ar1 = full.autocorr(lag=1)
                    out["ar1"] = float(ar1) if np.isfinite(ar1) else np.nan
        except Exception:
            pass
        return out

    def _build_project_statistics_tables(
        self,
        export_items: list[tuple[Any, str, AdvisorResult]],
    ) -> Dict[str, pd.DataFrame]:
        """Collect every statistic the advisor computed, for all analyzed datasets."""
        now_text = _dt.datetime.now().isoformat(timespec="seconds")
        summary_rows: list[Dict[str, Any]] = []
        method_rows: list[Dict[str, Any]] = []
        series_rows: list[Dict[str, Any]] = []
        raw_rows: list[Dict[str, Any]] = []
        eps_full_frames: list[pd.DataFrame] = []
        eps_running_frames: list[pd.DataFrame] = []
        exclusion_rows: list[Dict[str, Any]] = []
        error_rows: list[Dict[str, Any]] = []
        powt_rows: list[Dict[str, Any]] = []
        raw_done: set[str] = set()

        for ds, state_key, ar in export_items:
            ds_key = str(getattr(ds, "key", "") or "")
            ds_path = str(getattr(ds, "path", "") or "")
            label = {"dataset": ds_key, "result_state": str(state_key)}
            candidates = dict(getattr(ar, "candidates", {}) or {})
            best_key = str(getattr(ar, "best_key", "") or "")
            profile = dict(getattr(ar, "score_profile", {}) or {})
            if not profile:
                profile = _normalize_score_profile(None, goal=ar.goal)
            ages = getattr(ar, "ages", None)
            ages_ok = isinstance(ages, pd.Series) and not ages.empty
            depth = getattr(ar, "sample_depth", None)
            depth_ok = isinstance(depth, pd.Series) and not depth.empty
            best = candidates.get(best_key)
            rcs_feas = getattr(ar, "rcs_feasibility", None)
            dist = getattr(ar, "disturbance_stats", None)
            cfg = getattr(ar, "compat_config", None)
            ranked = sorted(candidates.keys(), key=lambda k: float(candidates[k].score), reverse=True)
            rank_map = {k: i + 1 for i, k in enumerate(ranked)}

            summary_rows.append(
                {
                    **label,
                    "file": ds_path,
                    "goal": str(ar.goal),
                    "goal_label": GOAL_LABELS.get(str(ar.goal), str(ar.goal)),
                    "backend": str(getattr(ar, "compat_mode", "")),
                    "difference_mode": bool(getattr(cfg, "difference", False)) if cfg is not None else np.nan,
                    "recode_zeros": bool(getattr(cfg, "recode_zeros", False)) if cfg is not None else np.nan,
                    "pos_slope": bool(getattr(cfg, "pos_slope", False)) if cfg is not None else np.nan,
                    "constrain_nls": str(getattr(cfg, "constrain_nls", "")) if cfg is not None else "",
                    "powt_method_used": str(getattr(cfg, "powt_method", "")) if (cfg is not None and bool(getattr(cfg, "powt_enabled", False))) else "",
                    "climate_series": "provided" if self.climate is not None else "not provided",
                    "climate_file": str(self.climate_path or ""),
                    "input_series": int(getattr(ar, "analysis_input_series_count", 0) or 0),
                    "input_trees": int(getattr(ar, "analysis_input_tree_count", 0) or 0),
                    "analysis_matrix": str(getattr(ar, "analysis_matrix_kind", "series") or "series"),
                    "analysis_columns": int(getattr(ar, "analysis_output_count", 0) or 0),
                    "full_period_start": getattr(ar, "full_period_start_year", np.nan),
                    "full_period_end": getattr(ar, "full_period_end_year", np.nan),
                    "analysis_period_start": getattr(ar, "analysis_period_start_year", np.nan),
                    "analysis_period_end": getattr(ar, "analysis_period_end_year", np.nan),
                    "min_count_for_detrending": int(getattr(ar, "min_series_for_detrending", 1) or 1),
                    "min_count_mode": str(getattr(ar, "min_series_count_mode", "series") or "series"),
                    "age_min": float(ages.min()) if ages_ok else np.nan,
                    "age_max": float(ages.max()) if ages_ok else np.nan,
                    "age_median": float(ages.median()) if ages_ok else np.nan,
                    "age_mean": float(ages.mean()) if ages_ok else np.nan,
                    "sample_depth_min": float(depth.min()) if depth_ok else np.nan,
                    "sample_depth_max": float(depth.max()) if depth_ok else np.nan,
                    "disturbance_events": int(getattr(dist, "n_events", 0) or 0) if dist is not None else np.nan,
                    "fraction_series_with_events": float(getattr(dist, "fraction_series_with_events", np.nan)) if dist is not None else np.nan,
                    "rcs_feasible": bool(getattr(rcs_feas, "possible", False)) if rcs_feas is not None else np.nan,
                    "rcs_reason": str(getattr(rcs_feas, "reason", "")) if rcs_feas is not None else "",
                    "n_methods": int(len(candidates)),
                    "best_method": best_key,
                    "best_description": str(getattr(best, "description", "")) if best is not None else "",
                    "best_score": float(best.score) if best is not None else np.nan,
                    "best_eps": float(best.metrics.eps) if best is not None else np.nan,
                    "best_rbar": float(best.metrics.rbar) if best is not None else np.nan,
                    "best_climate_corr": (
                        float(best.metrics.climate_corr)
                        if (best is not None and best.metrics.climate_corr is not None)
                        else np.nan
                    ),
                    "powt_decision_flag": str(getattr(ds, "powt_recommendation_flag", "") or ""),
                    "powt_best_method": str(getattr(ds, "powt_best_method", "") or ""),
                    "powt_recommendation": str(getattr(ds, "powt_recommendation_text", "") or ""),
                    "high_cutoff_years": profile.get("high_cutoff_years", np.nan),
                    "low_cutoff_years": profile.get("low_cutoff_years", np.nan),
                    "w_var_high": profile.get("w_var_high", np.nan),
                    "w_var_low": profile.get("w_var_low", np.nan),
                    "w_eps": profile.get("w_eps", np.nan),
                    "w_rbar": profile.get("w_rbar", np.nan),
                    "w_climate": profile.get("w_climate", np.nan),
                    "score_formula": _format_score_rule_text(ar.goal, profile),
                    "exported_at": now_text,
                }
            )

            series_logs = dict(getattr(ar, "series_exclusion_logs", {}) or {})
            eps_logs = dict(getattr(ar, "eps_series_exclusion_logs", {}) or {})
            analysis_ids = getattr(ar, "analysis_ids", None)

            for method_key in ranked:
                cand = candidates[method_key]
                m = cand.metrics
                std_stats = self._stats_series_summary(cand.chronology_std)
                res_stats = self._stats_series_summary(cand.chronology_res)
                log = dict(series_logs.get(method_key, {}) or {})
                method_rows.append(
                    {
                        **label,
                        "method": method_key,
                        "rank": rank_map.get(method_key, np.nan),
                        "is_best": bool(method_key == best_key),
                        "description": str(cand.description),
                        "how_computed": _method_explanation(method_key),
                        "score": float(cand.score),
                        "eps": float(m.eps),
                        "rbar": float(m.rbar),
                        "var_highfreq": float(m.var_highfreq),
                        "var_lowfreq": float(m.var_lowfreq),
                        "climate_corr": float(m.climate_corr) if m.climate_corr is not None else np.nan,
                        "n_columns_total": int(log.get("n_total", cand.rwi.shape[1]) or 0),
                        "n_columns_used": int(log.get("n_used", cand.rwi.shape[1]) or 0),
                        "n_columns_excluded": int(log.get("n_excluded", 0) or 0),
                        "n_excluded_from_eps_rbar": int(len(dict(eps_logs.get(method_key, {}) or {}))),
                        "std_first_year": std_stats["first_year"],
                        "std_last_year": std_stats["last_year"],
                        "std_n_years": std_stats["n_values"],
                        "std_mean": std_stats["mean"],
                        "std_sd": std_stats["std"],
                        "std_min": std_stats["min"],
                        "std_max": std_stats["max"],
                        "std_ar1": std_stats["ar1"],
                        "res_n_years": res_stats["n_values"],
                        "res_mean": res_stats["mean"],
                        "res_sd": res_stats["std"],
                        "res_ar1": res_stats["ar1"],
                    }
                )

                chron = pd.to_numeric(cand.chronology_std, errors="coerce")
                for col in list(cand.rwi.columns):
                    col_stats = self._stats_series_summary(cand.rwi[col])
                    r_chron = np.nan
                    try:
                        pair = pd.concat(
                            [pd.to_numeric(cand.rwi[col], errors="coerce"), chron],
                            axis=1,
                            join="inner",
                        ).dropna()
                        if pair.shape[0] >= 3:
                            r_chron = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
                    except Exception:
                        r_chron = np.nan
                    series_rows.append(
                        {
                            **label,
                            "method": method_key,
                            "series": str(col),
                            **{f"rwi_{k}": v for k, v in col_stats.items()},
                            "r_with_std_chronology": r_chron,
                        }
                    )

                for series_name, reason in dict(log.get("excluded_series", {}) or {}).items():
                    exclusion_rows.append(
                        {**label, "method": method_key, "stage": "detrending", "series": str(series_name), "reason": str(reason)}
                    )
                for series_name, reason in dict(eps_logs.get(method_key, {}) or {}).items():
                    exclusion_rows.append(
                        {**label, "method": method_key, "stage": "eps_rbar", "series": str(series_name), "reason": str(reason)}
                    )

                try:
                    full_df = compute_eps_rbar_stats_full_period(cand.rwi, ids=analysis_ids)
                except Exception:
                    try:
                        full_df = compute_eps_rbar_stats_full_period(cand.rwi)
                    except Exception:
                        full_df = pd.DataFrame()
                if full_df is not None and not full_df.empty:
                    full_df = full_df.copy()
                    full_df.insert(0, "method", method_key)
                    full_df.insert(0, "result_state", str(state_key))
                    full_df.insert(0, "dataset", ds_key)
                    eps_full_frames.append(full_df)

                try:
                    run_df = _get_cached_signal_diagnostics_for_result(ar, method_key, 50)
                except Exception:
                    run_df = pd.DataFrame()
                if run_df is not None and not run_df.empty:
                    run_df = run_df.reset_index(drop=True).copy()
                    run_df.insert(0, "window", 50)
                    run_df.insert(0, "method", method_key)
                    run_df.insert(0, "result_state", str(state_key))
                    run_df.insert(0, "dataset", ds_key)
                    eps_running_frames.append(run_df)

            for method_key, detail in dict(getattr(ar, "dplr_method_errors", {}) or {}).items():
                error_rows.append({**label, "method": str(method_key), "stage": "detrending", "error": str(detail)})
            for method_key, detail in dict(getattr(ar, "dplr_eps_rbar_errors", {}) or {}).items():
                error_rows.append({**label, "method": str(method_key), "stage": "eps_rbar", "error": str(detail)})

            if ds_key not in raw_done:
                raw_done.add(ds_key)
                rwl_raw = getattr(ds, "rwl", None)
                ids_df = getattr(ds, "ids", None)
                if isinstance(rwl_raw, pd.DataFrame):
                    for col in list(rwl_raw.columns):
                        col_stats = self._stats_series_summary(rwl_raw[col])
                        tree_label = ""
                        core_label = ""
                        try:
                            if isinstance(ids_df, pd.DataFrame) and str(col) in set(ids_df.index.astype(str)):
                                row = ids_df.loc[str(col)]
                                tree_label = str(row.get("tree_label", ""))
                                core_label = str(row.get("core_label", ""))
                        except Exception:
                            pass
                        raw_rows.append(
                            {
                                "dataset": ds_key,
                                "file": ds_path,
                                "series": str(col),
                                "tree_label": tree_label,
                                "core_label": core_label,
                                **{f"raw_{k}": v for k, v in col_stats.items()},
                            }
                        )
                for powt_res in (
                    getattr(ds, "powt_preview_for_run_universal", None),
                    getattr(ds, "powt_preview_for_run_cook", None),
                ):
                    if powt_res is None:
                        continue
                    try:
                        names = sorted(
                            set(powt_res.power.index.astype(str))
                            | set(powt_res.raw_skew.index.astype(str))
                            | set(powt_res.powt_skew.index.astype(str))
                        )
                        for name in names:
                            powt_rows.append(
                                {
                                    "dataset": ds_key,
                                    "powt_method": str(powt_res.method),
                                    "rescale": bool(powt_res.rescale),
                                    "series": name,
                                    "power": float(pd.to_numeric(powt_res.power, errors="coerce").get(name, np.nan)),
                                    "raw_skew": float(pd.to_numeric(powt_res.raw_skew, errors="coerce").get(name, np.nan)),
                                    "powt_skew": float(pd.to_numeric(powt_res.powt_skew, errors="coerce").get(name, np.nan)),
                                }
                            )
                    except Exception:
                        pass

        settings_rows = [
            {"setting": "exported_at", "value": now_text},
            {"setting": "project_file", "value": str(self.current_project_path or "")},
            {"setting": "datasets_in_project", "value": int(len(self.datasets))},
            {"setting": "datasets_exported", "value": int(len({str(getattr(d, "key", "")) for d, _s, _a in export_items}))},
            {"setting": "goal", "value": str(self.goal_var.get())},
            {"setting": "data_type", "value": str(self.var_type_var.get())},
            {"setting": "backend", "value": str(self.compat_mode_var.get())},
            {"setting": "r_workers", "value": str(self.dplr_parallel_workers_var.get())},
            {"setting": "min_count_for_detrending", "value": str(self.min_series_for_detrending_var.get())},
            {"setting": "min_count_mode", "value": str(self.min_series_count_mode_var.get())},
            {"setting": "tree_grouping", "value": bool(self.enable_tree_grouping_var.get())},
            {"setting": "powt_enabled", "value": bool(self.enable_dplr_powt_var.get())},
            {"setting": "powt_method", "value": str(self.dplr_powt_method_var.get())},
            {"setting": "custom_methods", "value": ", ".join(self.custom_method_keys)},
            {"setting": "ssf_methods", "value": bool(self.enable_dplr_ssf_var.get())},
            {"setting": "signal_free_rcs", "value": bool(self.enable_dplr_rcs_sf_var.get())},
            {"setting": "export_powt_and_no_powt", "value": bool(self.export_both_powt_modes_var.get())},
            {"setting": "climate_file", "value": str(self.climate_path or "")},
            {"setting": "eps_rbar_running_window", "value": 50},
            {"setting": "eps_rbar_correlation", "value": "spearman, zeros treated as missing (dplR rwi.stats style)"},
            {"setting": "variance_compression", "value": "C(x) = x / (1 + x)"},
        ]
        guide_rows = [
            {"topic": "method", "key": k, "text": v} for k, v in sorted(METHOD_EXPLANATIONS.items())
        ] + [
            {
                "topic": "goal",
                "key": k,
                "text": f"{GOAL_LABELS.get(k, k)}. {GOAL_EXPLANATIONS.get(k, '')} "
                f"Default rule: {_format_score_rule_text(k, DEFAULT_SCORE_PROFILES.get(k))}",
            }
            for k in GOAL_LABELS.keys()
        ]
        readme_rows = [
            {"sheet": "datasets_summary", "content": "One row per dataset and result state: periods, ages, depth, disturbances, RCS feasibility, best method, POWT decision, score settings."},
            {"sheet": "methods", "content": "One row per dataset/state/method: rank, score, EPS, Rbar, band variances, climate correlation, chronology statistics, used/excluded columns, how the method is computed."},
            {"sheet": "series_rwi", "content": "Per detrended series (or tree) and method: descriptive statistics, AR1 and correlation with the STD chronology."},
            {"sheet": "raw_series", "content": "Per raw input series: period, length, mean, SD, AR1 and tree/core labels."},
            {"sheet": "eps_rbar_full", "content": "Full-period EPS / Rbar statistics per dataset/state/method."},
            {"sheet": "eps_rbar_running", "content": "Running-window (50 years) EPS / Rbar / depth per dataset/state/method."},
            {"sheet": "exclusions", "content": "Series excluded during detrending or EPS/Rbar and the reason."},
            {"sheet": "backend_errors", "content": "Methods that failed in the dplR backend and the error text."},
            {"sheet": "powt", "content": "POWT power and skewness before/after transformation per series."},
            {"sheet": "settings", "content": "Global settings used for this export."},
            {"sheet": "methods_guide", "content": "Explanation of every detrending method and analysis goal."},
        ]
        return {
            "readme": pd.DataFrame(readme_rows),
            "datasets_summary": pd.DataFrame(summary_rows),
            "methods": pd.DataFrame(method_rows),
            "series_rwi": pd.DataFrame(series_rows),
            "raw_series": pd.DataFrame(raw_rows),
            "eps_rbar_full": pd.concat(eps_full_frames, ignore_index=True) if eps_full_frames else pd.DataFrame(),
            "eps_rbar_running": pd.concat(eps_running_frames, ignore_index=True) if eps_running_frames else pd.DataFrame(),
            "exclusions": pd.DataFrame(exclusion_rows),
            "backend_errors": pd.DataFrame(error_rows),
            "powt": pd.DataFrame(powt_rows),
            "settings": pd.DataFrame(settings_rows),
            "methods_guide": pd.DataFrame(guide_rows),
        }

    def _export_project_statistics(
        self,
        export_items: list[tuple[Any, str, AdvisorResult]],
        excel_engine: Optional[str],
        write_csv: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Write the project-level statistics file (Excel and/or CSV folder)."""
        errors: list[str] = []
        paths: list[str] = []
        if not self.output_dir or not export_items:
            return errors, paths
        tables = self._build_project_statistics_tables(export_items)
        if excel_engine is not None:
            xlsx_path = os.path.join(self.output_dir, "project_statistics_all_datasets.xlsx")
            try:
                with pd.ExcelWriter(xlsx_path, engine=excel_engine) as writer:
                    for sheet_name, df in tables.items():
                        out_df = df if (df is not None and not df.empty) else pd.DataFrame({"info": ["No data."]})
                        out_df.to_excel(writer, sheet_name=str(sheet_name)[:31], index=False)
                paths.append(xlsx_path)
            except Exception as exc:
                errors.append(f"project_statistics_all_datasets.xlsx: {exc}")
                write_csv = True
        if write_csv:
            csv_dir = os.path.join(self.output_dir, "project_statistics_csv")
            try:
                os.makedirs(csv_dir, exist_ok=True)
                for sheet_name, df in tables.items():
                    if df is None or df.empty:
                        continue
                    df.to_csv(
                        os.path.join(csv_dir, f"project_statistics_{sheet_name}.csv"),
                        index=False,
                        encoding="utf-8-sig",
                    )
                paths.append(csv_dir)
            except Exception as exc:
                errors.append(f"project_statistics_csv: {exc}")
        return errors, paths

    def _sanitize_filename(self, name: str) -> str:
        """Return a filesystem-safe name segment (ASCII-friendly)."""
        return "".join(ch if (ch.isalnum() or ch in ("-", "_", ".")) else "_" for ch in name)
    def _build_ringclim_prefill_manifest(self) -> str:
        """Write detrended outputs to a temp folder and return manifest path.

        The manifest is a JSON file consumed by ringclim.py via --prefill.
        It lists two categories:
            - "chronology": 2-column chronologies (Year + value)
            - "individual": multi-column RWI tables (Year + series columns)

        This export includes *all* available chronologies for all datasets in the project
        that already have analysis results.
        """
        ds_list = list(self.datasets.values())
        manifest_items: list[tuple[Any, str, AdvisorResult]] = []
        for ds in ds_list:
            for state_key, ar in self._iter_dataset_result_states(
                ds,
                include_both=bool(self.export_both_powt_modes_var.get()),
            ):
                if ar is None:
                    continue
                manifest_items.append((ds, state_key, ar))
        if not manifest_items:
            raise RuntimeError("No analysis results available for any dataset.")
        project_name = "project"
        if self.current_project_path:
            project_name = os.path.splitext(os.path.basename(self.current_project_path))[0] or project_name
        project_safe = self._sanitize_filename(project_name)
        base_tmp = self.output_dir or tempfile.gettempdir()
        session_dir = tempfile.mkdtemp(prefix=f"detrend_to_ringclim_{project_safe}_", dir=base_tmp)
        entries: list[dict] = []

        def _write_chron(series: pd.Series, stem: str) -> str:
            df = pd.DataFrame({"Year": series.index.astype(int), "Chronology": series.values})
            p = os.path.join(session_dir, f"{stem}.csv")
            df.to_csv(p, index=False)
            return p

        def _write_rwi(df_rwi: pd.DataFrame, stem: str) -> str:
            df_out = df_rwi.copy()
            df_out.insert(0, "Year", df_out.index.astype(int))
            p = os.path.join(session_dir, f"{stem}.csv")
            df_out.to_csv(p, index=False)
            return p

        def _method_group(method_key: str) -> str:
            """Assign a stable method group name for UI grouping."""
            mk = (method_key or "").strip().lower()
            if mk in ("exp", "linear", "mean", "hugershoff"):
                return "Curve fits"
            if mk.startswith("spline") or mk in ("runmean_32", "lowess"):
                return "Smooths"
            if mk.startswith("rcs"):
                return "RCS"
            return "Other"

        loc_by_name: Dict[str, Any] = {}
        for ds, state_key, ar in manifest_items:
            dataset_name = ds.key
            if state_key == "no_powt":
                dataset_name = f"{dataset_name}__powt_off"
            elif state_key == "with_powt":
                dataset_name = f"{dataset_name}__powt_on"
            dataset_safe = self._sanitize_filename(dataset_name)
            _locs = getattr(self, "_dataset_locations", {}) or {}
            ds_location = None
            try:
                ds_location = (_locs.get(os.path.normcase(os.path.abspath(str(ds.path))))
                               or _locs.get(os.path.splitext(os.path.basename(str(ds.path)))[0].lower())
                               or _locs.get(str(ds.key).lower()))
            except Exception:
                ds_location = None
            if ds_location:
                loc_by_name[dataset_name] = ds_location
            powt_result = getattr(ds, "powt_preview_result", None)
            if powt_result is None:
                try:
                    cache_key = (
                        str(dataset_name),
                        str(self.dplr_powt_method_var.get()).strip() or str(DEFAULT_COMPAT_CONFIG.powt_method),
                        bool(self.dplr_powt_rescale_var.get()),
                    )
                    cached_powt = self._powt_preview_cache.get(cache_key)
                    if cached_powt is not None:
                        powt_result = cached_powt
                        setattr(ds, "powt_preview_result", cached_powt)
                except Exception:
                    pass
 
            # Compute per-group ranking for "top 3 per group" selection in RingClim.
            group_ranks: Dict[tuple[str, str], Dict[str, int]] = {}
            for gname in {"Curve fits", "Smooths", "RCS", "Other"}:
                methods_in_group = [
                    (mkey, cand.score)
                    for mkey, cand in ar.candidates.items()
                    if _method_group(mkey) == gname
                ]
                methods_in_group.sort(key=lambda t: float(t[1]), reverse=True)
                for rank, (mkey, _score) in enumerate(methods_in_group, start=1):
                    group_ranks.setdefault((dataset_name, gname), {})[mkey] = rank

            # Optional "best" alias (convenience); excluded from top-3 selection by "is_alias".
            best_key = ar.best_key
            best = ar.candidates.get(best_key)
            if best is not None:
                best_group = _method_group(best_key)
                bstd_path = _write_chron(best.chronology_std, f"{dataset_safe}__best__chron_std")
                bres_path = _write_chron(best.chronology_res, f"{dataset_safe}__best__chron_res")
                brwi_path = _write_rwi(best.rwi, f"{dataset_safe}__best__rwi")
                entries.extend(
                    [
                        {
                            "category": "chronology",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": best_group,
                            "method": "best",
                            "alias_of": best_key,
                            "is_alias": True,
                            "variant": "STD",
                            "score": float(best.score),
                            "name": f"{dataset_name} | best ({best_key}) | Chronology STD",
                            "path": bstd_path,
                        },
                        {
                            "category": "chronology",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": best_group,
                            "method": "best",
                            "alias_of": best_key,
                            "is_alias": True,
                            "variant": "RES",
                            "score": float(best.score),
                            "name": f"{dataset_name} | best ({best_key}) | Chronology RES",
                            "path": bres_path,
                        },
                        {
                            "category": "individual",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": best_group,
                            "method": "best",
                            "alias_of": best_key,
                            "is_alias": True,
                            "variant": "RWI",
                            "score": float(best.score),
                            "name": f"{dataset_name} | best ({best_key}) | RWI (all series)",
                            "path": brwi_path,
                        },
                    ]
                )

            if powt_result is not None:
                try:
                    powt_df = getattr(powt_result, "transformed", None)
                except Exception:
                    powt_df = None
                if isinstance(powt_df, pd.DataFrame) and not powt_df.empty:
                    powt_path = _write_rwi(powt_df, f"{dataset_safe}__powt__rwi")
                    entries.append(
                        {
                            "category": "individual",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": "POWT",
                            "method": "powt",
                            "variant": "POWT",
                            "score": 0.0,
                            "rank_in_group": 0,
                            "recommended": False,
                            "goal": ar.goal,
                            "powt_applied": True,
                            "powt_method": str(getattr(powt_result, "method", "") or ""),
                            "powt_rescale": bool(getattr(powt_result, "rescale", False)),
                            "name": f"{dataset_name} | POWT | Transformed RWI (all series)",
                            "path": powt_path,
                        }
                    )

            for key, cand in sorted(ar.candidates.items(), key=lambda kv: kv[0]):
                ksafe = self._sanitize_filename(key)
                gname = _method_group(key)
                rank_in_group = group_ranks.get((dataset_name, gname), {}).get(key, 0)
                recommended = bool(rank_in_group and rank_in_group <= 3)

                std_path = _write_chron(cand.chronology_std, f"{dataset_safe}__{ksafe}__chron_std")
                res_path = _write_chron(cand.chronology_res, f"{dataset_safe}__{ksafe}__chron_res")
                rwi_path = _write_rwi(cand.rwi, f"{dataset_safe}__{ksafe}__rwi")

                entries.extend(
                    [
                        {
                            "category": "chronology",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": gname,
                            "method": key,
                            "variant": "STD",
                            "score": float(cand.score),
                            "rank_in_group": int(rank_in_group),
                            "recommended": recommended,
                            "goal": ar.goal,
                            "name": f"{dataset_name} | {key} | Chronology STD",
                            "path": std_path,
                        },
                        {
                            "category": "chronology",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": gname,
                            "method": key,
                            "variant": "RES",
                            "score": float(cand.score),
                            "rank_in_group": int(rank_in_group),
                            "recommended": recommended,
                            "goal": ar.goal,
                            "name": f"{dataset_name} | {key} | Chronology RES",
                            "path": res_path,
                        },
                        {
                            "category": "individual",
                            "kind": "chronology",
                            "dataset_key": dataset_name,
                            "dataset_name": dataset_name,
                            "group": gname,
                            "method": key,
                            "variant": "RWI",
                            "score": float(cand.score),
                            "rank_in_group": int(rank_in_group),
                            "recommended": recommended,
                            "goal": ar.goal,
                            "name": f"{dataset_name} | {key} | RWI (tree level)",
                            "path": rwi_path,
                        },
                    ]
                )

        for e in entries:
            loc = loc_by_name.get(str(e.get("dataset_name", "")))
            if loc:
                e["location"] = loc
        manifest = {
            "source": "detrend_advisor",
            "project_name": project_name,
            "created_at": _dt.datetime.utcnow().isoformat() + "Z",
            "entries": entries,
        }
        manifest_path = os.path.join(session_dir, "ringclim_prefill.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return manifest_path

    def on_open_ringclim(self) -> None:
        """Launch RingClim and prefill it with detrended outputs."""
        if not any(ds.advisor_result is not None for ds in self.datasets.values()):
            messagebox.showwarning("No results", "Run analysis before opening RingClim.", parent=self)
            return
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ringclim_path, launch_mode = _find_ringclim_launch_target()
        if not ringclim_path:
            ringclim_path = filedialog.askopenfilename(
                title="Locate RingClim",
                filetypes=[
                    ("RingClim launcher", "ringclim ringclim.py ringclim.exe"),
                    ("Python script", "*.py"),
                    ("Executable files", "*.exe"),
                    ("All files", "*.*"),
                ],
                initialdir=self._get_dialog_initial_dir(script_dir),
                parent=self,
            )
            if not ringclim_path and sys.platform == "darwin":
                app_dir = filedialog.askdirectory(
                    title="Locate RingClim.app",
                    initialdir=self._get_dialog_initial_dir(script_dir),
                    parent=self,
                )
                if app_dir:
                    app_exec = os.path.join(app_dir, "Contents", "MacOS", "ringclim")
                    if os.path.isdir(app_dir) and app_dir.lower().endswith(".app") and os.path.isfile(app_exec):
                        ringclim_path = app_dir
                        launch_mode = "app"
                    else:
                        messagebox.showerror(
                            "Launch error",
                            "The selected .app does not contain the RingClim executable.",
                            parent=self,
                        )
                        return
            if not ringclim_path:
                return
            self._remember_dialog_location(ringclim_path)
            if sys.platform == "darwin" and str(ringclim_path).lower().endswith(".app") and os.path.isdir(ringclim_path):
                launch_mode = "app"
            else:
                launch_mode = "script" if str(ringclim_path).lower().endswith(".py") else "executable"
        try:
            manifest_path = self._build_ringclim_prefill_manifest()
        except Exception as exc:
            messagebox.showerror(
                "Prefill error",
                f"Failed to prepare datasets for RingClim:\n{exc}",
                parent=self,
            )
            return
        launch_env = _build_clean_qt_env()
        if launch_mode == "app" and sys.platform == "darwin":
            popen_args = ["/usr/bin/open", "-n", "-a", ringclim_path, "--args", "--prefill", manifest_path]
        elif launch_mode == "script":
            if not _ensure_ringclim_runtime(self):
                self.status_var.set("RingClim launch cancelled: PySide6 is not available.")
                return
            popen_args = [sys.executable, ringclim_path, "--prefill", manifest_path]
        else:
            popen_args = [ringclim_path, "--prefill", manifest_path]
        try:
            self.status_var.set("Launching RingClim...")
            try:
                self.update_idletasks()
            except Exception:
                pass
            log_path = _new_runtime_log_path("ringclim_launch")
            log_fp = open(log_path, "a", encoding="utf-8")
            try:
                ringclim_proc = subprocess.Popen(
                    popen_args,
                    cwd=os.path.dirname(ringclim_path),
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                    env=launch_env,
                    **_hidden_subprocess_kwargs(gui=True),
                )
            finally:
                log_fp.close()
        except Exception as exc:
            messagebox.showerror(
                "Launch error",
                f"Failed to launch RingClim:\n{exc}",
                parent=self,
            )
            return
        if launch_mode == "app" and sys.platform == "darwin":
            self.status_var.set("RingClim opened through macOS. Its import window should now be in front.")
        else:
            self.status_var.set(
                "RingClim launched. The new window should request attention. "
                f"Output and errors are being written to: {log_path}"
            )

        def report_failed_launch() -> None:
            try:
                return_code = ringclim_proc.poll()
            except Exception:
                return
            if return_code is None:
                self.after(1000, report_failed_launch)
            elif return_code != 0:
                message = (f"RingClim stopped with exit code {return_code}.\n\n"
                           f"Its output and error details are in:\n{log_path}")
                self.status_var.set(message.replace("\n", " "))
                messagebox.showerror("RingClim launch error", message, parent=self)

        self.after(1000, report_failed_launch)
        try:
            self.bell()
            self.update_idletasks()
        except Exception:
            pass

    def _powt_run_signature_from_vars(self) -> tuple[bool, str, bool]:
        """Return the user-selected POWT run signature from the top controls."""
        return (
            bool(self.powt_use_for_run_var.get()),
            str(self.powt_method_var.get()).strip() or str(DEFAULT_COMPAT_CONFIG.powt_method),
            bool(self.powt_rescale_var.get()),
        )

    def _invalidate_analysis_cache_for_powt_change(self) -> None:
        """Force a fresh analysis run after any POWT run-control change."""
        for ds in self.datasets.values():
            try:
                ds.compat_signature = None
            except Exception:
                pass

    def _sync_powt_control_vars(self, source: str = "main") -> None:
        """Keep duplicated POWT control variables in sync.

        Notes:
            - source="main" means the top RUN controls are the source of truth.
            - source="tab" means the POWT tab controls are the source of truth.
            - This must stay two-way so the user can disable POWT from either place.
        """
        if self._powt_sync_in_progress:
            return
        self._powt_sync_in_progress = True
        try:
            src = str(source or "main").strip().lower()
            if src == "tab":
                run_enabled = bool(self.enable_dplr_powt_var.get())
                method_text = str(self.dplr_powt_method_var.get()).strip() or str(DEFAULT_COMPAT_CONFIG.powt_method)
                rescale_enabled = bool(self.dplr_powt_rescale_var.get())
            else:
                run_enabled = bool(self.powt_use_for_run_var.get())
                method_text = str(self.powt_method_var.get()).strip() or str(DEFAULT_COMPAT_CONFIG.powt_method)
                rescale_enabled = bool(self.powt_rescale_var.get())

            if bool(self.powt_use_for_run_var.get()) != run_enabled:
                self.powt_use_for_run_var.set(run_enabled)
            if bool(self.enable_dplr_powt_var.get()) != run_enabled:
                self.enable_dplr_powt_var.set(run_enabled)

            if str(self.powt_method_var.get()).strip() != method_text:
                self.powt_method_var.set(method_text)
            if str(self.dplr_powt_method_var.get()).strip() != method_text:
                self.dplr_powt_method_var.set(method_text)

            if bool(self.powt_rescale_var.get()) != rescale_enabled:
                self.powt_rescale_var.set(rescale_enabled)
            if bool(self.dplr_powt_rescale_var.get()) != rescale_enabled:
                self.dplr_powt_rescale_var.set(rescale_enabled)
        finally:
            self._powt_sync_in_progress = False

    def _apply_active_dataset_powt_display_choice(self) -> None:
        """Switch the visible active result immediately when POWT display is toggled."""
        if self._powt_display_sync_in_progress:
            return
        ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
        if ds is None:
            return

        use_powt_display = bool(self.show_powt_results_var.get())
        ar_no_powt = getattr(ds, "advisor_result_no_powt", None)
        ar_with_powt = getattr(ds, "advisor_result_with_powt", None)

        chosen_result = None
        chosen_mode = "recommended"
        chosen_powt_preview = None
        if use_powt_display and ar_with_powt is not None:
            chosen_result = ar_with_powt
            chosen_mode = "with_powt"
            chosen_powt_preview = getattr(ds, "powt_preview_for_run", None)
        elif (not use_powt_display) and ar_no_powt is not None:
            chosen_result = ar_no_powt
            chosen_mode = "no_powt"
            chosen_powt_preview = None
        else:
            chosen_result = getattr(ds, "advisor_result", None)
            chosen_mode = str(getattr(ds, "displayed_result_mode", "recommended") or "recommended")
            chosen_powt_preview = getattr(ds, "powt_preview_for_run", None) if use_powt_display else None

        self._powt_display_sync_in_progress = True
        try:
            ds.displayed_result_mode = str(chosen_mode)
            ds.powt_preview_result = chosen_powt_preview
            self.advisor_result = chosen_result
            try:
                ds.advisor_result = chosen_result
            except Exception:
                pass
        finally:
            self._powt_display_sync_in_progress = False

        try:
            self._update_recommendation_text()
        except Exception:
            pass
        try:
            self.update_chron_plots()
        except Exception:
            pass
        try:
            self._update_structure_plots()
        except Exception:
            pass
        try:
            self._update_powt_tab()
        except Exception:
            pass
        try:
            self._update_signal_plots()
        except Exception:
            pass
        try:
            self.update_compare_plots()
        except Exception:
            pass

    def _on_powt_run_controls_changed(self, *_args, source: str = "main") -> None:
        """Handle any user change in POWT run settings."""
        if self._powt_sync_in_progress:
            return
        old_sig = tuple(self._powt_last_user_run_signature)
        self._sync_powt_control_vars(source=source)
        new_sig = self._powt_run_signature_from_vars()
        self._powt_last_user_run_signature = new_sig
        if new_sig != old_sig:
            self._invalidate_analysis_cache_for_powt_change()
            try:
                self.status_var.set("POWT settings changed. RUN will recompute with the new POWT state.")
            except Exception:
                pass
        self._apply_active_dataset_powt_display_choice()

    def _on_powt_display_changed(self, *_args) -> None:
        """Refresh visible plots and text immediately after POWT display toggling."""
        self._apply_active_dataset_powt_display_choice()

    def _update_recommendation_text(self) -> None:
        """Fill recommendation tab with human-readable summary."""
        self.text_recommend.delete("1.0", tk. END)
        if self.advisor_result is None:
            self.text_recommend.insert(tk. END, "No results.")
            return
        r = self.advisor_result
        best = r.candidates[r.best_key]
        goal_label = GOAL_LABELS.get(r.goal, r.goal)
        self.text_recommend.insert(
            tk. END,
            f"User goal: {r.goal} ({goal_label})\n"
            f"Recommended method: {best.name}\n"
            f"Description: {best.description}\n\n",
        )
        min_series = int(getattr(r, "min_series_for_detrending", 1) or 1)
        min_series_mode = str(getattr(r, "min_series_count_mode", "series") or "series")
        start_year = getattr(r, "analysis_start_year", None)
        start_year_text = "n/a" if start_year is None else str(int(start_year))
        analysis_depth = getattr(r, "analysis_depth_filtered", None)
        original_depth = getattr(r, "analysis_depth_original", None)
        year_period_text = "n/a"
        full_year_period_text = "n/a"
        if isinstance(analysis_depth, pd.Series) and not analysis_depth.empty:
            year_period_text = f"{int(analysis_depth.index.min())}-{int(analysis_depth.index.max())}"
        if isinstance(original_depth, pd.Series) and not original_depth.empty:
            full_year_period_text = f"{int(original_depth.index.min())}-{int(original_depth.index.max())}"
        elif (
            getattr(r, "full_period_start_year", None) is not None
            and getattr(r, "full_period_end_year", None) is not None
        ):
            full_year_period_text = (
                f"{int(getattr(r, 'full_period_start_year'))}-"
                f"{int(getattr(r, 'full_period_end_year'))}"
            )
        self.text_recommend.insert(
            tk. END,
            f"Minimum {min_series_mode} required to start detrending: {min_series}\n"
            f"Detrending start year at this threshold: {start_year_text}\n\n",
        )
        pith_note = str(getattr(r, "pith_note", "") or "").strip()
        if pith_note:
            self.text_recommend.insert(tk. END, pith_note + "\n\n")
        elif _pith is not None:
            self.text_recommend.insert(
                tk. END,
                "Pith: no pith information - all series are treated as starting at the pith "
                "(RCS / age curve). Use Tree groups or Project > Import pith info.\n\n",
            )

        ds = self.datasets.get(self.active_dataset_key) if self.active_dataset_key else None
        if ds is not None:
            ar_no = getattr(ds, "advisor_result_no_powt", None)
            ar_yes = getattr(ds, "advisor_result_with_powt", None)
            ar_yes_universal = getattr(ds, "advisor_result_with_powt_universal", None)
            ar_yes_cook = getattr(ds, "advisor_result_with_powt_cook", None)
            powt_flag = str(getattr(ds, "powt_recommendation_flag", "") or "").strip()
            powt_note = str(getattr(ds, "powt_recommendation_text", "") or "").strip()
            shown_state = self._current_powt_display_label()
            best_powt_method = str(getattr(ds, "powt_best_method", "") or "").strip()

            def _best_summary(ar):
                if ar is None:
                    return ("—", float("nan"), float("nan"), float("nan"))
                cand = ar.candidates.get(ar.best_key)
                if cand is None:
                    return (str(ar.best_key), float("nan"), float("nan"), float("nan"))
                return (
                    str(ar.best_key),
                    float(cand.score),
                    float(cand.metrics.eps),
                    float(cand.metrics.rbar),
                )

            no_key, no_score, no_eps, no_rbar = _best_summary(ar_no)
            yes_key, yes_score, yes_eps, yes_rbar = _best_summary(ar_yes)
            u_key, u_score, u_eps, u_rbar = _best_summary(ar_yes_universal)
            c_key, c_score, c_eps, c_rbar = _best_summary(ar_yes_cook)
            score_delta = (
                yes_score - no_score
                if np.isfinite(yes_score) and np.isfinite(no_score)
                else float("nan")
            )

            self.text_recommend.insert(tk. END, "POWT comparison:\n")
            self.text_recommend.insert(
                tk. END,
                f"- Currently shown in plots/tables: {shown_state}\n"
                f"- POWT decision flag: {powt_flag or 'unknown'}\n"
                f"- Best without POWT: {no_key} | score={no_score:.3f} | EPS={no_eps:.3f} | Rbar={no_rbar:.3f}\n"
                f"- Best with POWT: {yes_key} | score={yes_score:.3f} | EPS={yes_eps:.3f} | Rbar={yes_rbar:.3f}\n",
            )
            if best_powt_method:
                self.text_recommend.insert(
                    tk. END,
                    f"- Best POWT method for ON-state: {best_powt_method}\n",
                )
            if ar_yes_universal is not None or ar_yes_cook is not None:
                self.text_recommend.insert(
                    tk. END,
                    f"- Universal: {u_key} | score={u_score:.3f} | EPS={u_eps:.3f} | Rbar={u_rbar:.3f}\n"
                    f"- Cook: {c_key} | score={c_score:.3f} | EPS={c_eps:.3f} | Rbar={c_rbar:.3f}\n",
                )
            if np.isfinite(score_delta):
                self.text_recommend.insert(
                    tk. END,
                    f"- Score delta (POWT ON - POWT OFF): {score_delta:+.3f}\n",
                )
            if powt_note:
                self.text_recommend.insert(
                    tk. END,
                    f"- Recommendation: {powt_note}\n",
                )
            self.text_recommend.insert(tk. END, "\n")

        self.text_recommend.insert(tk. END, "Reasons (by metrics):\n")
        self.text_recommend.insert(
            tk. END,
            f"- Score: {best.score:.3f}\n"
            f"- EPS: {best.metrics.eps:.3f}\n"
            f"- Rbar: {best.metrics.rbar:.3f}\n"
            f"- Var (high freq): {best.metrics.var_highfreq:.3f}\n"
            f"- Var (low freq): {best.metrics.var_lowfreq:.3f}\n",
        )
        if best.metrics.climate_corr is not None:
            self.text_recommend.insert(
                tk. END,
                f"- Climate correlation: "
                f"{best.metrics.climate_corr:.3f}\n",
            )
        explanation = _method_explanation(best.name)
        if explanation:
            self.text_recommend.insert(tk. END, "\nMethod rationale:\n")
            self.text_recommend.insert(tk. END, explanation + "\n")
        top3 = sorted(r.candidates.values(), key=lambda c: c.score, reverse=True)[:3]
        if top3:
            self.text_recommend.insert(tk. END, "\nTop 3 methods by score:\n")
            for cand in top3:
                self.text_recommend.insert(
                    tk. END,
                    f"- {cand.name}: score={cand.score:.3f}, EPS={cand.metrics.eps:.3f}, "
                    f"Rbar={cand.metrics.rbar:.3f}\n",
                )
        self.text_recommend.insert(tk. END, "\nAge diagnostics:\n")
        analysis_matrix_kind = str(getattr(r, "analysis_matrix_kind", "series") or "series")
        input_series_count = int(getattr(r, "analysis_input_series_count", r.ages.shape[0]) or r.ages.shape[0])
        input_tree_count = int(getattr(r, "analysis_input_tree_count", r.ages.shape[0]) or r.ages.shape[0])
        output_count = int(getattr(r, "analysis_output_count", r.ages.shape[0]) or r.ages.shape[0])
        if analysis_matrix_kind == "trees":
            primary_count_label = "Number of trees"
            primary_count_value = output_count
            secondary_count_line = f"- Number of original series: {input_series_count}\n"
        else:
            primary_count_label = "Number of series"
            primary_count_value = output_count
            secondary_count_line = f"- Number of trees: {input_tree_count}\n"
        self.text_recommend.insert(
            tk. END,
            f"- {primary_count_label}: {primary_count_value}\n"
            f"{secondary_count_line}"
            f"- Full period: {full_year_period_text}\n"
            f"- Analysis years: {year_period_text}\n"
            f"- Min age: {int(r.ages.min())} years\n"
            f"- Max age: {int(r.ages.max())} years\n"
            f"- Median age: {float(r.ages.median()):.1f} years\n",
        )
        self.text_recommend.insert(tk. END, "\nRCS:\n")
        self.text_recommend.insert(
            tk. END,
            f"- RCS feasible: {r.rcs_feasibility.possible}\n"
            f"- Reason: {r.rcs_feasibility.reason}\n",
        )
        self.text_recommend.insert(tk. END, "\nDisturbances:\n")
        self.text_recommend.insert(
            tk. END,
            f"- Strong releases/suppressions (total): "
            f"{r.disturbance_stats.n_events}\n"
        )
        if not math.isnan(r.disturbance_stats.fraction_series_with_events):
            self.text_recommend.insert(
                tk. END,
                f"- Fraction of series with events: "
                f"{r.disturbance_stats.fraction_series_with_events:.2f}\n",
            )
        backend_errors = _format_backend_method_errors(r)
        if backend_errors:
            self.text_recommend.insert(tk. END, "\nBackend warnings:\n")
            for line in backend_errors:
                self.text_recommend.insert(tk. END, line + "\n")
    def _update_structure_plots(self) -> None:
        """Update age histogram, age curve and sample depth plots."""
        if self.advisor_result is None:
            return
        r = self.advisor_result
        self.ax_age_hist.clear()
        self.ax_age_hist.hist(
            r.ages.values, bins=20, color="steelblue", edgecolor="black"
        )
        self.ax_age_hist.set_xlabel("Series age (years)")
        self.ax_age_hist.set_ylabel("Count")
        self.ax_age_hist.set_title("Age distribution")
        self.ax_age_curve.clear()
        self.ax_age_curve.plot(
            r.age_curve.index, r.age_curve.values, "-o", color="darkgreen", markersize=3
        )
        self.ax_age_curve.set_xlabel("Age (years)")
        self.ax_age_curve.set_ylabel("Mean value")
        self.ax_age_curve.set_title("Mean growth curve (age)")
        self.ax_depth.clear()
        self.ax_depth.plot(
            r.sample_depth.index, r.sample_depth.values, color="darkred"
        )
        self.ax_depth.set_xlabel("Year")
        self.ax_depth.set_ylabel("Sample depth")
        self.ax_depth.set_title("Sample depth over time")
        if str(getattr(r, "pith_note", "") or ""):
            self.ax_age_hist.set_xlabel("Cambial age of the outermost ring (years, pith offsets applied)")
            self.ax_age_curve.set_xlabel("Cambial age (years, pith offsets applied)")
        if _dplots is not None:
            for ax_s in (self.ax_age_hist, self.ax_age_curve, self.ax_depth):
                _dplots.style_axes(ax_s, grid_axis="y")
            for patch in self.ax_age_hist.patches:
                patch.set_facecolor(_dplots.SERIES[0])
                patch.set_edgecolor("white")
            for line in self.ax_age_curve.get_lines():
                line.set_color(_dplots.SERIES[2])
                line.set_markersize(2)
            for line in self.ax_depth.get_lines():
                line.set_color(_dplots.DEPTH_LINE)
        self.fig_structure.tight_layout(h_pad=1.6)
        self.canvas_structure.draw_idle()
    def _update_signal_plots(self) -> None:
        """Update EPS/Rbar/depth diagnostics plot."""
        if self.ax_signal not in self.fig_signal.axes:
            self.fig_signal.clear()
            self.ax_signal = self.fig_signal.add_subplot(111)
        if self.advisor_result is None:
            self.ax_signal.clear()
            self.ax_signal.text(
                0.5, 0.5, "No EPS/Rbar/depth data available", ha="center", va="center",
                transform=self.ax_signal.transAxes
            )
            self.canvas_signal.draw_idle()
            return
        r = self.advisor_result
        method_key = self.signal_method_var.get()
        if method_key == "best":
            method_key = r.best_key
        cand = r.candidates.get(method_key)
        if cand is None:
            self.ax_signal.clear()
            self.ax_signal.text(
                0.5, 0.5, f"Method '{method_key}' not available", ha="center", va="center",
                transform=self.ax_signal.transAxes
            )
            self.canvas_signal.draw_idle()
            return
        try:
            window = int(self.signal_window_var.get())
        except Exception:
            window = 50
        window = int(max(3, window))
        df = self._get_signal_diagnostics_df(method_key, window)
        if df is None or df.empty:
            self.ax_signal.clear()
            self.ax_signal.text(
                0.5, 0.5, "No EPS/Rbar/depth data available", ha="center", va="center",
                transform=self.ax_signal.transAxes
            )
            self.canvas_signal.draw_idle()
            return
        if _dplots is not None:
            _dplots.eps_rbar_depth(
                self.fig_signal,
                df,
                title=f"EPS / Rbar / Depth ({method_key}, window={window}, {self._current_powt_display_label()})",
            )
            self.ax_signal = self.fig_signal.axes[0]
            self.canvas_signal.draw_idle()
            return
        self.ax_signal.clear()
        # Remove old twin axes (twinx) to avoid stacking and z-order artifacts.
        for ax in list(self.fig_signal.axes):
            if ax is not self.ax_signal:
                ax.remove()
        ax2 = self.ax_signal.twinx()
        # Place depth behind EPS/Rbar.
        ax2.set_zorder(0)
        self.ax_signal.set_zorder(1)
        self.ax_signal.patch.set_visible(False)
        ax2.fill_between(
            df.index,
            df["depth"],
            color="tab:orange",
            alpha=0.3,
            label="Depth",
            zorder=0,
        )
        # EPS & Rbar
        self.ax_signal.plot(df.index, df["eps"], label="EPS", color="tab:blue", zorder=3)
        self.ax_signal.plot(df.index, df["rbar"], label="Rbar", color="tab:green", zorder=3)
 
        self.ax_signal.axhline(
            0.85,
            color="gray",
            linestyle="--",
            linewidth=1.0,
            label="EPS = 0.85",
        )
        self.ax_signal.set_xlabel("Year")
        self.ax_signal.set_ylabel("EPS / Rbar")
        ax2.set_ylabel("Sample depth")
        finite_eps = df["eps"].to_numpy(dtype=float) if "eps" in df.columns else np.array([], dtype=float)
        finite_rbar = df["rbar"].to_numpy(dtype=float) if "rbar" in df.columns else np.array([], dtype=float)
        finite_all = np.concatenate(
            [
                finite_eps[np.isfinite(finite_eps)],
                finite_rbar[np.isfinite(finite_rbar)],
            ]
        )
        if finite_all.size > 0:
            ymin = min(-0.25, float(np.nanmin(finite_all)) - 0.05)
            ymax = max(1.05, float(np.nanmax(finite_all)) + 0.05)
            self.ax_signal.set_ylim(max(-1.0, ymin), min(1.05, ymax))
        else:
            self.ax_signal.set_ylim(-0.25, 1.05)
        self.ax_signal.set_title(
            f"EPS / Rbar / Depth ({method_key}, window={window}, {self._current_powt_display_label()})"
        )
        self.ax_signal.grid(True)
        lines, labels = self.ax_signal.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        try:
            self.fig_signal.legend(
                list(lines) + list(lines2),
                list(labels) + list(labels2),
                loc="upper center",
                bbox_to_anchor=(0.5, 0.995),
                ncol=4,
                frameon=False,
                fontsize="small",
                borderaxespad=0.0,
            )
        except Exception:
            pass
        self.fig_signal.tight_layout(rect=[0.02, 0.02, 0.98, 0.92])
        self.canvas_signal.draw_idle()
    def update_chron_plots(self) -> None:
        """Update chronologies tab (best vs others OR RCS vs SF)."""
        # Clear the entire figure
        self._chron_hover_key = None
        self.fig_chron.clear()
        self._clear_figure_legends(self.fig_chron)
        if self.advisor_result is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self.fig_chron.tight_layout()
                self.canvas_chron.draw_idle()
                return
        r = self.advisor_result
        mode = self.chron_view_mode.get()
        if mode == "best_vs_all" and _dplots is not None:
            chrons = {k: c.chronology_std for k, c in r.candidates.items()}
            depth = r.sample_depth if isinstance(r.sample_depth, pd.Series) and not r.sample_depth.empty else None
            _dplots.chronology_with_depth(
                self.fig_chron,
                chrons,
                depth,
                best=r.best_key,
                title=f"Best chronology ({r.best_key}) vs other candidates (STD) | {self._current_powt_display_label()}",
                smooth=31,
            )
            self.ax_chron = self.fig_chron.axes[0]
            self._tag_chronology_lines(self.ax_chron, {k: c.chronology_std for k, c in r.candidates.items()})
            self.chron_info_var.set(
                "Hover a line to see method, year and value; click a chronology to explain its peaks "
                "(which series make them, sample depth, agreement with the other methods)."
            )
            self.canvas_chron.draw_idle()
            return
        # New mode: all series (trees) for selected method
        if mode == "all_series":
            # Select method from the second combobox
            method_key = self.chron_method_var.get()
            if method_key == "best":
                method_key = r.best_key
            cand = r.candidates.get(method_key)
            if cand is None:
                ax = self.fig_chron.add_subplot(1, 1, 1)
                ax.text(
                    0.5,
                    0.5,
                    f"No candidate '{method_key}'",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                )
                self.fig_chron.tight_layout()
                self.canvas_chron.draw_idle()
                return
            # Create two axes: upper for STD, lower for RES (or raw + trend of the selected series)
            ax_std = self.fig_chron.add_subplot(2, 1, 1)
            ax_res = self.fig_chron.add_subplot(2, 1, 2, sharex=ax_std)
            # STD: all series (indices) for this method
            rwi = cand.rwi
            self._chron_series_lines = {}
            self._chron_hover_annot = None
            self._chron_cand_key = method_key
            names = [str(c) for c in rwi.columns]
            try:
                self.chron_series_combo["values"] = names
            except Exception:
                pass
            if self._chron_selected not in names:
                self._chron_selected = None
            sel = self._chron_selected
            for col in rwi.columns:
                is_sel = (str(col) == sel)
                (ln,) = ax_std.plot(
                    rwi.index,
                    rwi[col].values,
                    color=("tab:red" if is_sel else "0.5"),
                    alpha=(1.0 if is_sel else 0.3),
                    linewidth=(1.6 if is_sel else 0.6),
                    zorder=(5 if is_sel else 1),
                    picker=True,
                    pickradius=4,
                    label="_" + str(col),
                )
                ln._series_name = str(col)
                self._chron_series_lines[str(col)] = ln
            # Overlay — STD chronology for reference
            ax_std.plot(
                cand.chronology_std.index,
                cand.chronology_std.values,
                color="tab:blue",
                linewidth=1.5,
                label="STD chronology",
            )
            ax_std.set_title(
                f"All series (STD) for method: {method_key} | {self._current_powt_display_label()}"
                + (f" | selected: {sel}" if sel else "")
            )
            ax_std.set_ylabel("Index")
            ax_std.grid(True)
            ax_std.legend(loc="upper right", fontsize="x-small", frameon=False)
            self._chron_ax_std = ax_std
            if sel:
                self._draw_selected_series_panel(ax_res, cand, sel)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self.fig_chron.tight_layout(rect=[0.02, 0.02, 0.98, 0.98])
                self.canvas_chron.draw_idle()
                return
            self.chron_info_var.set(
                "Click a grey series (or choose it in 'Series') to see its raw values, fitted trend and an "
                "explanation of its peaks; hover shows year and value; zoom with the toolbar (magnifier)."
            )
            # RES: compute residual matrix and plot all series
            res_mat = compute_residual_matrix(rwi)
            for col in res_mat.columns:
                ax_res.plot(
                    res_mat.index,
                    res_mat[col].values,
                    color="0.5",
                    alpha=0.3,
                    linewidth=0.6,
                )
            # Overlay — RES chronology (as calculated)
            ax_res.plot(
                cand.chronology_res.index,
                cand.chronology_res.values,
                color="tab:orange",
                linewidth=1.5,
                label="RES chronology",
            )
            ax_res.set_title(
                f"All series (RES) for method: {method_key} | {self._current_powt_display_label()}"
            )
            ax_res.set_xlabel("Year")
            ax_res.set_ylabel("Residual")
            ax_res.grid(True)
            ax_res.legend(loc="upper right", fontsize="x-small", frameon=False)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.fig_chron.tight_layout(rect=[0.02, 0.02, 0.98, 0.98])
            self.canvas_chron.draw_idle()
            return
        # For other modes, recreate the single axis
        self.ax_chron = self.fig_chron.add_subplot(1, 1, 1)
        if mode == "rcs_vs_rcs_sf":
            if r.rcs_std is None or r.rcs_sf_std is None:
                self.ax_chron.text(
                    0.5,
                    0.5,
                    "RCS / signal-free RCS not available",
                    ha="center",
                    va="center",
                    transform=self.ax_chron.transAxes,
                )
            else:
                self.ax_chron.plot(
                    r.rcs_std.index, r.rcs_std.values, label="RCS STD"
                )
                self.ax_chron.plot(
                    r.rcs_sf_std.index,
                    r.rcs_sf_std.values,
                    label="Signal-free RCS STD",
                )
                self.ax_chron.set_title("RCS vs signal-free RCS (STD)")
            self.ax_chron.set_xlabel("Year")
            self.ax_chron.set_ylabel("Index")
            self._add_compact_figure_legend(self.fig_chron, self.ax_chron, ncol=3)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            self.fig_chron.tight_layout(rect=[0.02, 0.02, 0.98, 0.92])
        self.canvas_chron.draw_idle()
        if mode == "best_vs_all":
            best = r.candidates[r.best_key]
            self.ax_chron.plot(
                best.chronology_std.index,
                best.chronology_std.values,
                label=f"best ({r.best_key}) STD",
                linewidth=2,
            )
            for key, cand in r.candidates.items():
                if key == r.best_key:
                    continue
                self.ax_chron.plot(
                    cand.chronology_std.index,
                    cand.chronology_std.values,
                    label=f"{key} STD",
                    alpha=0.7,
                )
            self.ax_chron.set_title(
                f"Best chronology vs other candidates (STD) | {self._current_powt_display_label()}"
            )
        else:
            key = self.chron_method_var.get()
            if key == "best":
                key = r.best_key
            cand = r.candidates.get(key)
            if cand is None:
                self.ax_chron.text(
                    0.5,
                    0.5,
                    f"Method '{key}' not available",
                    ha="center",
                    va="center",
                    transform=self.ax_chron.transAxes,
                )
                self.ax_chron.set_title(
                    f"Chronology not available | {self._current_powt_display_label()}"
                )
            else:
                self.ax_chron.plot(
                    cand.chronology_std.index,
                    cand.chronology_std.values,
                    label=f"{key} STD",
                    color="tab:blue",
                )
                self.ax_chron.plot(
                    cand.chronology_res.index,
                    cand.chronology_res.values,
                    label=f"{key} RES",
                    color="tab:orange",
                    alpha=0.7,
                )
                self.ax_chron.set_title(
                    f"Chronologies for method: {key} | {self._current_powt_display_label()}"
                )
        self.ax_chron.set_xlabel("Year")
        self.ax_chron.set_ylabel("Index")
        self._add_compact_figure_legend(self.fig_chron, self.ax_chron, ncol=4)
        try:
            tag_map = {k: c.chronology_std for k, c in r.candidates.items()}
            tag_map.update({f"{k} RES": c.chronology_res for k, c in r.candidates.items()})
            if getattr(r, "rcs_std", None) is not None:
                tag_map["RCS STD"] = r.rcs_std
            if getattr(r, "rcs_sf_std", None) is not None:
                tag_map["Signal-free RCS STD"] = r.rcs_sf_std
            self._tag_chronology_lines(self.ax_chron, tag_map)
            self.chron_info_var.set("Hover a line to see year and value; click it to explain its peaks.")
        except Exception:
            pass
        self.fig_chron.tight_layout(rect=[0.02, 0.02, 0.98, 0.92])
        self.canvas_chron.draw_idle()


    # ------------------------------------------------------------------ series inspection
    def _select_chron_series(self, name: Optional[str]) -> None:
        self._chron_selected = (str(name) if name else None)
        try:
            self.chron_series_var.set(self._chron_selected or "")
        except Exception:
            pass
        if self._chron_selected and self.chron_view_mode.get() != "all_series":
            self.chron_view_mode.set("all_series")
        self.update_chron_plots()

    def _tag_chronology_lines(self, ax, chrons: Dict[str, Any]) -> None:
        """Give every plotted chronology line its method name (hover) and make it clickable."""
        prepared = []
        for k, sr in chrons.items():
            if sr is None:
                continue
            ss = pd.to_numeric(pd.Series(sr), errors="coerce").dropna()
            if len(ss):
                prepared.append((k, ss.index.to_numpy(dtype=float), ss.to_numpy(dtype=float)))
        for ln in ax.get_lines():
            x = np.asarray(ln.get_xdata(), dtype=float)
            y = np.asarray(ln.get_ydata(), dtype=float)
            if len(x) < 5:
                continue
            for k, px, py in prepared:
                if len(px) == len(x) and np.allclose(px, x) and np.allclose(py, y, equal_nan=True):
                    ln._series_name = str(k)
                    ln._chron_kind = True
                    ln.set_picker(True)
                    ln.set_pickradius(4)
                    break
            else:
                lab = str(ln.get_label())
                if not lab.startswith("_"):
                    ln._series_name = lab
                    ln._chron_kind = True

    def _on_chron_pick(self, event) -> None:
        art = getattr(event, "artist", None)
        name = getattr(art, "_series_name", None)
        if name and getattr(art, "_chron_kind", False):
            self._explain_chronology(name)
            return
        if not name or self.chron_view_mode.get() != "all_series":
            return
        if name == self._chron_selected:
            return
        self._select_chron_series(name)

    def _on_chron_hover(self, event) -> None:
        if event.inaxes is None or event.xdata is None or event.ydata is None:
            annot = self._chron_hover_annot
            if annot is not None and annot.get_visible():
                annot.set_visible(False)
                self._chron_hover_key = None
                self.canvas_chron.draw_idle()
            return
        ax = event.inaxes
        # Line.contains() scans every x/y point in every line on each mouse
        # motion. With many trees this becomes O(lines * years) per event and
        # freezes Tk while the pointer moves. Chronologies are year-sorted, so
        # inspect only the nearest sample in each line (O(lines * log(years))).
        best = None
        pointer = np.asarray([float(event.x), float(event.y)], dtype=float)
        for ln in ax.get_lines():
            if not getattr(ln, "get_visible", lambda: True)():
                continue
            try:
                name = getattr(ln, "_series_name", None)
                if not name:
                    name = str(ln.get_label())
                    if name.startswith("_"):
                        continue
                # Keep the line's native arrays: forcing float here would copy
                # the full year vector on every pointer event.
                xs = np.asarray(ln.get_xdata(orig=False))
                ys = np.asarray(ln.get_ydata(orig=False))
                if xs.size == 0 or xs.size != ys.size:
                    continue
                # These plots use chronological, sorted year indexes.
                idx = int(np.searchsorted(xs, float(event.xdata), side="left"))
                idx = min(max(idx, 0), xs.size - 1)
                if idx > 0 and abs(xs[idx - 1] - event.xdata) <= abs(xs[idx] - event.xdata):
                    idx -= 1
                x, y = float(xs[idx]), float(ys[idx])
                if not np.isfinite(x) or not np.isfinite(y):
                    continue
                display = np.asarray(ax.transData.transform((x, y)), dtype=float)
                distance2 = float(np.sum((display - pointer) ** 2))
                if distance2 <= 100.0 and (best is None or distance2 < best[0]):
                    best = (distance2, x, y, str(name), ln, idx)
            except Exception:
                continue
        annot = self._chron_hover_annot
        if best is None:
            if annot is not None and annot.get_visible():
                annot.set_visible(False)
                self._chron_hover_key = None
                self.canvas_chron.draw_idle()
            return
        _distance2, x, y, name, ln, idx = best
        hover_key = (id(ax), id(ln), int(idx))
        # Avoid re-rendering a potentially large plot for every mouse-motion
        # event that remains over the same series/year sample.
        if hover_key == self._chron_hover_key and annot is not None and annot.get_visible():
            return
        if annot is None or annot.axes is not ax:
            try:
                if annot is not None:
                    annot.remove()
            except Exception:
                pass
            annot = ax.annotate("", xy=(0, 0), xytext=(12, 12), textcoords="offset points",
                                bbox=dict(boxstyle="round", fc="#fffbe6", ec="#999", alpha=0.95),
                                fontsize=8, zorder=20)
            annot.set_visible(False)
            self._chron_hover_annot = annot
        self._chron_hover_key = hover_key
        try:
            yr = int(round(float(x)))
        except Exception:
            yr = x
        annot.xy = (x, y)
        annot.set_text(f"{name}\nyear {yr}: {float(y):.3f}")
        annot.set_visible(True)
        self.canvas_chron.draw_idle()

    def _explain_chronology(self, name: str) -> None:
        r = self.advisor_result
        if r is None:
            return
        key = str(name).replace(" STD", "").strip()
        res_mode = key.endswith(" RES")
        key = key.replace(" RES", "").strip()
        if key.startswith("best (") and key.endswith(")"):
            key = key[6:-1]
        cand = r.candidates.get(key)
        if cand is None:
            self.chron_info_var.set(f"{name}: no details for this line.")
            return
        depth = r.sample_depth if isinstance(r.sample_depth, pd.Series) else None
        others = {k: c.chronology_std for k, c in r.candidates.items() if k != key}
        txt = diagnose_chronology_peaks(key, cand.chronology_res if res_mode else cand.chronology_std,
                                        cand.rwi, depth=depth, other_chronologies=others)
        self.chron_info_var.set(txt)

    def _chron_input_for_display(self) -> tuple[Optional[pd. DataFrame], bool]:
        """Measured (or power-transformed) input of the active dataset and whether indices are differences."""
        ds = self.datasets.get(self.active_dataset_key) if getattr(self, "active_dataset_key", None) else None
        if ds is None:
            return None, False
        pv = getattr(ds, "powt_preview_result", None)
        if pv is not None and getattr(pv, "transformed", None) is not None:
            return pv.transformed, True
        return getattr(ds, "rwl", None), False

    def _draw_selected_series_panel(self, ax, cand, name: str) -> None:
        """Lower panel: raw (input) values, fitted trend and flagged peaks of one series, with reasons."""
        rwi = pd.to_numeric(cand.rwi[name], errors="coerce") if name in cand.rwi.columns else None
        raw_df, diff_mode = self._chron_input_for_display()
        raw = None
        if raw_df is not None and name in raw_df.columns:
            raw = pd.to_numeric(raw_df[name], errors="coerce").reindex(rwi.index) if rwi is not None else None
        if rwi is None or rwi.dropna().empty:
            ax.text(0.5, 0.5, f"No indices for {name}", ha="center", va="center", transform=ax.transAxes)
            self.chron_info_var.set(f"Selected: {name} - no values")
            return
        r = self.advisor_result
        depth = r.sample_depth if (r is not None and isinstance(r.sample_depth, pd.Series)) else None
        diag = diagnose_series_peaks(name, rwi, raw, depth=depth, difference_mode=diff_mode,
                                     method=str(getattr(cand, "name", "") or ""))
        if raw is not None and raw.notna().any():
            ax.plot(raw.index, raw.values, color="0.25", lw=0.9,
                    label=("power-transformed values" if diff_mode else "measured values"))
            fit = diag.get("fit")
            if fit is not None and fit.notna().any():
                ax.plot(fit.index, fit.values, color="tab:green", lw=1.6, label="fitted trend")
            ax.set_ylabel("Transformed width" if diff_mode else "Ring width")
        else:
            ax.plot(rwi.index, rwi.values, color="tab:red", lw=1.0, label="index")
            ax.set_ylabel("Index")
        for pk in diag.get("peaks", [])[:4]:
            yv = raw.get(pk["year"]) if raw is not None else rwi.get(pk["year"])
            if yv is not None and np.isfinite(yv):
                ax.plot([pk["year"]], [yv], "o", color="tab:red", ms=5, zorder=6)
                ax.annotate(str(pk["year"]), (pk["year"], yv), xytext=(3, 6), textcoords="offset points",
                            fontsize=7, color="tab:red")
        ax.set_title(f"{name}: input values and fitted trend (red dots = peaks of the index)")
        ax.set_xlabel("Year")
        ax.grid(True)
        ax.legend(loc="upper right", fontsize="x-small", frameon=False)
        self.chron_info_var.set(diag.get("text", f"Selected: {name}"))

    def update_climate_plots(self) -> None:
        """Climate tab: r of every method with the climate series, plus series / running r / scatter."""
        if not hasattr(self, "fig_climate"):
            return
        fig = self.fig_climate
        fig.clear()
        r = self.advisor_result
        if _dplots is None:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "dendro_plots.py not found", ha="center", va="center", transform=ax.transAxes)
            self.canvas_climate.draw_idle()
            return
        if r is None or not r.candidates:
            _dplots.message(fig.add_subplot(111), "Run the analysis first.")
            self.canvas_climate.draw_idle()
            return
        clim = self.climate
        if clim is None or len(clim) == 0:
            _dplots.message(
                fig.add_subplot(111),
                "No climate series loaded.\nProject > Import climate (CSV): a Year column and one climate column.",
            )
            self.canvas_climate.draw_idle()
            return
        clim = pd.to_numeric(pd.Series(clim), errors="coerce").dropna()
        clim.index = pd.Index(clim.index).astype(int)
        clim_name = str(self.climate_column or os.path.basename(str(self.climate_path or "climate")))
        ctype = str(self.climate_chron_type_var.get() or "STD")
        try:
            window = int(self.climate_window_var.get())
        except Exception:
            window = 31
        window = int(max(11, min(101, window)))

        def chron_of(cand):
            s = cand.chronology_res if ctype == "RES" else cand.chronology_std
            s = pd.to_numeric(pd.Series(s), errors="coerce").dropna()
            s.index = pd.Index(s.index).astype(int)
            return s

        labels, rs, ns = [], [], []
        for key, cand in r.candidates.items():
            s = chron_of(cand)
            both = pd.concat([s, clim], axis=1, join="inner").dropna()
            rr, _p, n = _dplots.pearson(both.iloc[:, 0].values, both.iloc[:, 1].values)
            labels.append(key)
            rs.append(rr)
            ns.append(n)
        order = np.argsort(np.nan_to_num(np.abs(np.asarray(rs, dtype=float)), nan=-1.0))
        labels = [labels[i] for i in order]
        rs = [rs[i] for i in order]
        ns = [ns[i] for i in order]

        sel = self.climate_method_var.get()
        if sel == "best" or sel not in r.candidates:
            sel = r.best_key
        if len(labels) > 12:
            # keep the panel readable: 12 strongest methods, always with the best and the selected one
            keep = set(labels[-12:]) | {sel, r.best_key}
            idx_keep = [i for i, lab in enumerate(labels) if lab in keep]
            labels = [labels[i] for i in idx_keep]
            rs = [rs[i] for i in idx_keep]
            ns = [ns[i] for i in idx_keep]
        s_sel = chron_of(r.candidates[sel])
        both = pd.concat([s_sel.rename("chron"), clim.rename("clim")], axis=1, join="inner").dropna()

        gs = fig.add_gridspec(2, 2, hspace=0.6, wspace=0.3, left=0.13, right=0.97, top=0.86, bottom=0.08)
        _dplots.corr_bars(
            fig.add_subplot(gs[0, 0]), labels, rs, n=ns, highlight=sel,
            title=f"{ctype} chronology vs {clim_name}: r by method (best: {r.best_key}; strongest 12 shown)"
            if len(r.candidates) > 12
            else f"{ctype} chronology vs {clim_name}: r by method (best: {r.best_key})",
        )
        if len(both) >= 4:
            _dplots.zscore_overlay(
                fig.add_subplot(gs[1, 0]), both.index.values, both["chron"].values, both["clim"].values,
                f"{sel} ({ctype})", clim_name, title=f"{sel}", smooth=11,
            )
            _dplots.running_corr_plot(fig.add_subplot(gs[1, 1]), both["chron"], both["clim"], window=window)
            _dplots.scatter_fit(fig.add_subplot(gs[0, 1]), both["chron"].values, both["clim"].values,
                                f"{sel} ({ctype})", clim_name)
        else:
            _dplots.message(fig.add_subplot(gs[1, :]), "Chronology and climate do not overlap.")
        fig.suptitle(f"Climate signal | {self._current_powt_display_label()}", fontsize=10, y=0.985)
        self.canvas_climate.draw_idle()

    def update_compare_plots(self) -> None:
        """Update compare/correlation tab plots."""
        if not hasattr(self, "fig_compare"):
            return
        # Remove any transient axes (e.g., stale colorbars) without shrinking the main subplots.
        keep_axes = {self.ax_compare_overlay, self.ax_compare_corr}
        if hasattr(self, "ax_compare_cbar"):
            keep_axes.add(self.ax_compare_cbar)
        for ax in list(self.fig_compare.axes):
            if ax not in keep_axes:
                ax.remove()
        self.ax_compare_overlay.clear()
        self.ax_compare_corr.clear()
        if hasattr(self, "ax_compare_cbar"):
            self.ax_compare_cbar.cla()

        # Restore axes geometry (prevents incremental shrinking after repeated updates).
        if hasattr(self, "_compare_overlay_pos"):
            self.ax_compare_overlay.set_position(self._compare_overlay_pos)
        if hasattr(self, "_compare_corr_pos"):
            self.ax_compare_corr.set_position(self._compare_corr_pos)
        if hasattr(self, "_compare_cbar_pos") and hasattr(self, "ax_compare_cbar"):
            self.ax_compare_cbar.set_position(self._compare_cbar_pos)

        if not self.datasets:
            self.ax_compare_overlay.text(
                0.5,
                0.5,
                "No datasets loaded",
                ha="center",
                va="center",
                transform=self.ax_compare_overlay.transAxes,
            )
            self.fig_compare.subplots_adjust(left=0.08, right=0.76, top=0.95, bottom=0.08, hspace=0.45)
            self.canvas_compare.draw_idle()
            return
        keys: list[str] = []
        if hasattr(self, "compare_listbox"):
            for i in self.compare_listbox.curselection():
                keys.append(str(self.compare_listbox.get(i)))
        if not keys:
            self.ax_compare_overlay.text(
                0.5,
                0.5,
                "Select one or more datasets to compare",
                ha="center",
                va="center",
                transform=self.ax_compare_overlay.transAxes,
            )
            self.fig_compare.subplots_adjust(left=0.08, right=0.76, top=0.95, bottom=0.08, hspace=0.45)
            self.canvas_compare.draw_idle()
            return
        mode = self.compare_mode_var.get()
        try:
            min_overlap = int(self.compare_min_overlap_var.get())
        except Exception:
            min_overlap = 30
        min_overlap = int(max(2, min_overlap))
        series_map: Dict[str, pd. Series] = {}
        sig_map: dict[str, str] = {}
        skipped: list[str] = []
        if mode == "all_methods":
            for k in keys:
                ds = self.datasets.get(k)
                if ds is None:
                    skipped.append(k)
                    continue
                ar = self._resolve_dataset_display_result(ds)
                if ar is None:
                    skipped.append(k)
                    continue
                # Include a per-dataset 'best' alias plus all candidate methods.
                best_cand = ar.candidates.get(ar.best_key)
                if best_cand is not None:
                    series_map[f"{k}|best"] = best_cand.chronology_std
                    sig_map[f"{k}|best"] = f"{k}|best:{ar.best_key}:STD"
                for mkey, cand in ar.candidates.items():
                    series_map[f"{k}|{mkey}"] = cand.chronology_std
        else:
            method_key = self.compare_method_var.get()
            for k in keys:
                ds = self.datasets.get(k)
                if ds is None or ds.advisor_result is None:
                    skipped.append(k)
                    continue
                ar = ds.advisor_result
                use_key = ar.best_key if method_key == "best" else method_key
                cand = ar.candidates.get(use_key)
                if cand is None:
                    skipped.append(f"{k} (no {use_key})")
                    continue
                series_map[k] = cand.chronology_std
                sig_map[k] = f"{k}|{use_key}|STD"
        if not series_map:
            self.ax_compare_overlay.text(
                0.5,
                0.5,
                "No comparable chronologies available",
                ha="center",
                va="center",
                transform=self.ax_compare_overlay.transAxes,
            )
            self.fig_compare.subplots_adjust(left=0.08, right=0.76, top=0.95, bottom=0.08, hspace=0.45)
            self.canvas_compare.draw_idle()
            return
        df = pd. DataFrame(series_map)
        for col in df.columns:
            self.ax_compare_overlay.plot(df.index, df[col].values, label=str(col))
        title = "Detrended chronologies (STD) comparison"
        if mode == "all_methods":
            title += " (all methods)"
        else:
            title += f" (method={self.compare_method_var.get()})"
        self.ax_compare_overlay.set_title(title)
        self.ax_compare_overlay.set_xlabel("Year")
        self.ax_compare_overlay.set_ylabel("Index")
        self.ax_compare_overlay.grid(True)
        self.ax_compare_overlay.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), borderaxespad=0.0, fontsize="x-small")
        # Compute correlations incrementally: reuse cached pairwise results for unchanged pairs.
        goal = self.goal_var.get()
        cols = list(df.columns)
        n_cols = len(cols)
        corr_arr = np.full((n_cols, n_cols), np.nan, dtype=float)
        np.fill_diagonal(corr_arr, 1.0)
        for i, col_i in enumerate(cols):
            x = df[col_i].to_numpy(dtype=float)
            sig_i = sig_map.get(col_i, col_i)
            for j in range(i + 1, n_cols):
                col_j = cols[j]
                y = df[col_j].to_numpy(dtype=float)
                sig_j = sig_map.get(col_j, col_j)
                # Order labels to keep the cache symmetric.
                if (col_i, sig_i) <= (col_j, sig_j):
                    pair_key = (goal, col_i, sig_i, col_j, sig_j)
                else:
                    pair_key = (goal, col_j, sig_j, col_i, sig_i)
                cached = self._pairwise_corr_cache.get(pair_key)
                if cached is None:
                    mask = (~np.isnan(x)) & (~np.isnan(y))
                    n_overlap = int(mask.sum())
                    if n_overlap >= 3:
                        try:
                            r = float(np.corrcoef(x[mask], y[mask])[0, 1])
                        except Exception:
                            r = float("nan")
                    else:
                        r = float("nan")
                    cached = (r, n_overlap)
                    self._pairwise_corr_cache[pair_key] = cached
                r, n_overlap = cached
                if n_overlap >= min_overlap:
                    corr_arr[i, j] = r
                    corr_arr[j, i] = r
        corr = pd.DataFrame(corr_arr, index=cols, columns=cols)
        corr_title = f"Pearson correlation (min overlap={min_overlap})"
        corr_highlight = None
        if corr.shape[0] < 2 and _dplots is not None and len(keys) == 1:
            # One dataset: compare its detrending methods with each other instead of an empty panel.
            ds1 = self.datasets.get(keys[0])
            ar1 = self._resolve_dataset_display_result(ds1) if ds1 is not None else None
            if ar1 is not None and len(ar1.candidates) >= 2:
                mdf = pd.DataFrame({k: c.chronology_std for k, c in ar1.candidates.items()})
                corr = mdf.corr(min_periods=min_overlap)
                corr_title = f"{keys[0]}: correlation between detrending methods (STD)"
                corr_highlight = ar1.best_key

        if corr.shape[0] < 2:
            self.ax_compare_corr.text(
                0.5,
                0.5,
                "Need at least 2 chronologies for correlation",
                ha="center",
                va="center",
                transform=self.ax_compare_corr.transAxes,
            )
            self.fig_compare.subplots_adjust(left=0.08, right=0.76, top=0.95, bottom=0.08, hspace=0.45)
            self.canvas_compare.draw_idle()
            return
        if _dplots is not None:
            _dplots.corr_matrix(
                self.ax_compare_corr,
                corr,
                cax=self.ax_compare_cbar if hasattr(self, "ax_compare_cbar") else None,
                title=corr_title,
                highlight=corr_highlight,
            )
            _dplots.style_axes(self.ax_compare_overlay)
            if corr.shape[0] > 10:
                self.ax_compare_corr.set_position([0.16, 0.16, 0.6, 0.3])
            if skipped:
                note = "Skipped: " + ", ".join(skipped[:8]) + (" ..." if len(skipped) > 8 else "")
                self.ax_compare_overlay.text(
                    0.01, 0.01, note, transform=self.ax_compare_overlay.transAxes, fontsize="x-small", va="bottom"
                )
            self.canvas_compare.draw_idle()
            return
        im = self.ax_compare_corr.imshow(
            corr.to_numpy(dtype=float),
            vmin=-1.0,
            vmax=1.0,
            aspect="auto",
        )
        self.ax_compare_corr.set_title(corr_title)
        self.ax_compare_corr.set_xticks(list(range(corr.shape[1])))
        self.ax_compare_corr.set_yticks(list(range(corr.shape[0])))
        self.ax_compare_corr.set_xticklabels(
            [str(c) for c in corr.columns.tolist()],
            rotation=90,
            fontsize="x-small",
        )
        self.ax_compare_corr.set_yticklabels(
            [str(i) for i in corr.index.tolist()],
            fontsize="x-small",
        )
        if hasattr(self, "ax_compare_cbar"):
            cbar = self.fig_compare.colorbar(im, cax=self.ax_compare_cbar)
        else:
            cbar = self.fig_compare.colorbar(im, ax=self.ax_compare_corr, fraction=0.046, pad=0.04)
        cbar.set_label("r")
        if skipped:
            note = "Skipped: " + ", ".join(skipped[:8]) + (" ..." if len(skipped) > 8 else "")
            self.ax_compare_overlay.text(
                0.01,
                0.01,
                note,
                transform=self.ax_compare_overlay.transAxes,
                fontsize="x-small",
                va="bottom",
            )
        self.fig_compare.subplots_adjust(left=0.08, right=0.76, top=0.95, bottom=0.08, hspace=0.45)
        self.canvas_compare.draw_idle()

    def _build_compare_correlations_export_df(self):
        """Build a tidy pairwise-correlation table for the Compare tab chronologies.

        The export mirrors the Compare heatmap inputs (STD chronologies), using the current
        compare mode/method and overlap threshold. If nothing is selected in the compare list,
        all datasets currently shown in the list are used. If the list widget is unavailable,
        all loaded datasets are used.
        """
        if not self.datasets:
            return None

        mode = self.compare_mode_var.get() if hasattr(self, "compare_mode_var") else "selected_method"
        method_key = self.compare_method_var.get() if hasattr(self, "compare_method_var") else "best"
        try:
            min_overlap = int(self.compare_min_overlap_var.get()) if hasattr(self, "compare_min_overlap_var") else 30
        except Exception:
            min_overlap = 30
        min_overlap = int(max(2, min_overlap))

        # Determine which dataset keys to include.
        keys = []
        if hasattr(self, "compare_listbox"):
            try:
                sel = list(self.compare_listbox.curselection())
            except Exception:
                sel = []
            if sel:
                for i in sel:
                    try:
                        keys.append(str(self.compare_listbox.get(i)))
                    except Exception:
                        continue
            else:
                try:
                    for i in range(int(self.compare_listbox.size())):
                        keys.append(str(self.compare_listbox.get(i)))
                except Exception:
                    keys = list(self.datasets.keys())
        else:
            keys = list(self.datasets.keys())

        # Build the same series_map as Compare (STD chronologies only).
        series_map = {}
        label_meta = {}
        skipped = []
        if mode == "all_methods":
            for k in keys:
                ds = self.datasets.get(k)
                if ds is None:
                    skipped.append(k)
                    continue
                ar = self._resolve_dataset_display_result(ds)
                if ar is None:
                    skipped.append(k)
                    continue
                best_cand = ar.candidates.get(ar.best_key)
                if best_cand is not None:
                    label = f"{k}|best"
                    series_map[label] = best_cand.chronology_std
                    label_meta[label] = {"dataset": k, "method": "best"}
                for mkey, cand in ar.candidates.items():
                    label = f"{k}|{mkey}"
                    series_map[label] = cand.chronology_std
                    label_meta[label] = {"dataset": k, "method": str(mkey)}
        else:
            for k in keys:
                ds = self.datasets.get(k)
                if ds is None:
                    skipped.append(k)
                    continue
                ar = self._resolve_dataset_display_result(ds)
                if ar is None:
                    skipped.append(k)
                    continue

                use_key = ar.best_key if method_key == "best" else method_key
                cand = ar.candidates.get(use_key)
                if cand is None:
                    skipped.append(f"{k} (no {use_key})")
                    continue
                series_map[k] = cand.chronology_std
                label_meta[k] = {"dataset": k, "method": str(use_key)}

        if not series_map:
            return None

        df = pd.DataFrame(series_map).sort_index()
        cols = list(df.columns)
        rows = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a = cols[i]
                b = cols[j]
                sa = df[a]
                sb = df[b]
                mask = sa.notna() & sb.notna()
                n_overlap = int(mask.sum())

                year_start = None
                year_end = None
                if n_overlap > 0:
                    try:
                        yrs = df.index[mask]
                        year_start = int(min(yrs))
                        year_end = int(max(yrs))
                    except Exception:
                        year_start = None
                        year_end = None

                r = float("nan")
                if n_overlap >= min_overlap:
                    try:
                        xa = sa[mask].to_numpy(dtype=float)
                        xb = sb[mask].to_numpy(dtype=float)
                        r = float(np.corrcoef(xa, xb)[0, 1])
                    except Exception:
                        r = float("nan")

                ma = label_meta.get(a, {"dataset": str(a), "method": ""})
                mb = label_meta.get(b, {"dataset": str(b), "method": ""})
                rows.append(
                    {
                        "mode": str(mode),
                        "selected_method": str(method_key),
                        "chronology": "STD",
                        "dataset_a": str(ma.get("dataset", "")),
                        "method_a": str(ma.get("method", "")),
                        "dataset_b": str(mb.get("dataset", "")),
                        "method_b": str(mb.get("method", "")),
                        "label_a": str(a),
                        "label_b": str(b),
                        "min_overlap": int(min_overlap),
                        "n_overlap": int(n_overlap),
                        "overlap_start": year_start,
                        "overlap_end": year_end,
                        "r": r,
                    }
                )

        columns = [
            "dataset_a",
            "method_a",
            "label_a",
            "dataset_b",
            "method_b",
            "label_b",
            "r",
            "n_overlap",
            "overlap_start",
            "overlap_end",
            "min_overlap",
        ]
        out = pd.DataFrame(rows, columns=columns)
        if skipped:
            out["skipped"] = ", ".join(skipped[:50]) + (" ..." if len(skipped) > 50 else "")
        return out

def main() -> None:
    """Launch GUI application."""
    try:
        app = DetrendAdvisorApp()

        def _tk_report_callback_exception(exc_type, exc_value, exc_tb) -> None:
            log_path = _write_exception_log(
                "tk_callback",
                (exc_type, exc_value, exc_tb),
            )
            try:
                messagebox.showerror(
                    "Application error",
                    f"An internal error occurred.\n\nLog written to:\n{log_path}",
                )
            except Exception:
                pass

        app.report_callback_exception = _tk_report_callback_exception
        app.mainloop()
    except Exception:
        log_path = _write_exception_log("startup")
        try:
            messagebox.showerror(
                "Startup error",
                f"The application failed to start.\n\nLog written to:\n{log_path}",
            )
        except Exception:
            pass
        raise
if __name__ == "__main__":
    # If called without arguments -> GUI. Otherwise, provide CLI utilities.
    parser = argparse.ArgumentParser(prog="detrend_advisor")
    parser.add_argument("--compat", choices=["dplR", "legacy"], default=(os.getenv("DETREND_COMPAT", "dplR") or "dplR"))
    parser.add_argument("--difference", action="store_true", help="Use y - fitted instead of y / fitted (dplR difference=TRUE).")
    parser.add_argument("--pos-slope", action="store_true", help="Allow positive slope in ModNegExp / splines (dplR pos.slope=TRUE).")
    parser.add_argument("--constrain-nls", choices=["never", "when.fail", "always"], default="never")
    parser.add_argument("--no-recode-zeros", action="store_true", help="Disable zero->0.001 recoding (default: enabled).")
    parser.add_argument("--rscript", default=(os.getenv("RSCRIPT", "Rscript") or "Rscript"))
    parser.add_argument("--install-r", action="store_true", help="Attempt to install R runtime on Windows (winget/choco) if Rscript is missing.")
    parser.add_argument("--install-r-deps", action="store_true", help="Attempt to install dplR from CRAN inside the R harness if missing.")
    parser.add_argument("--cran-repo", default=(os.getenv("DETREND_CRAN_REPO", "https://cloud.r-project.org") or "https://cloud.r-project.org"))
    parser.add_argument("--strict-dplr", action="store_true", help="Fail hard if R/dplR backend is unavailable (no legacy fallback).")

    parser.add_argument("--batch", action="store_true", help="Run advisor headlessly and export CSV (no GUI).")
    parser.add_argument("--input", help="Input file (CSV / Tucson .rwl / JSON / Excel supported).")
    parser.add_argument("--goal", default="highfreq", help="Goal key (highfreq/mixed/lowfreq/temp_recon/runoff_recon/drought_recon/variance_check).")
    parser.add_argument("--output", help="Output CSV path (batch mode).")
    parser.add_argument("--climate", default=None, help="Optional climate/target CSV path.")
    parser.add_argument("--export-all-methods", action="store_true", help="Export STD/RES for all candidates (batch mode).")
    parser.add_argument("--po", default=None, help="Optional pith-offset CSV (used for RCS/validation).")

    parser.add_argument("--validate", action="store_true", help="Run validation against dplR and write a report.")
    parser.add_argument("--validate-eps-rbar", action="store_true", help="Run EPS/Rbar validation against dplR and write CSV/Markdown outputs.")
    parser.add_argument("--out-dir", default=None, help="Validation output directory.")
    parser.add_argument("--validation-methods", default=None, help="Comma-separated validation method keys, or 'all'.")
    parser.add_argument("--validation-fast", action="store_true", help="Use a reduced validation method set for EPS/Rbar validation.")
    parser.add_argument("--verbose-progress", action="store_true", help="Print progress messages during validation runs.")

    # No arguments -> GUI (preserves original behavior)
    if len(sys.argv) == 1:
        main()
        raise SystemExit(0)

    args = parser.parse_args()

    cfg = DplrCompatConfig(
        compat="legacy" if str(args.compat).lower() == "legacy" else "dplR",
        recode_zeros=(not bool(args.no_recode_zeros)),
        difference=bool(args.difference),
        pos_slope=bool(args.pos_slope),
        constrain_nls=str(args.constrain_nls),
        rscript=str(args.rscript),
        strict_dplr=bool(args.strict_dplr),
        auto_install_r=bool(args.install_r),
        auto_install_r_deps=bool(args.install_r_deps),
        cran_repo=str(args.cran_repo),
    )
    DEFAULT_COMPAT_CONFIG = cfg  # override process-global default for this run

    if args.validate:
        if not args.input:
            raise SystemExit("ERROR: --validate requires --input")
        out_dir = args.out_dir or os.path.join(os.getcwd(), "dplr_validation_out")
        method_keys = _resolve_validation_method_keys(
            args.validation_methods,
            fast=bool(args.validation_fast),
        )
        cli_validate_against_dplr(
            input_path=args.input,
            out_dir=out_dir,
            po_path=args.po,
            python_compat=cfg.compat,
            difference=cfg.difference,
            pos_slope=cfg.pos_slope,
            constrain_nls=cfg.constrain_nls,
            recode_zeros=cfg.recode_zeros,
            rscript=cfg.rscript,
            auto_install_r=cfg.auto_install_r,
            auto_install_r_deps=cfg.auto_install_r_deps,
            cran_repo=cfg.cran_repo,
            method_keys=method_keys,
            verbose=bool(args.verbose_progress),
        )
        raise SystemExit(0)

    if args.validate_eps_rbar:
        if not args.input:
            raise SystemExit("ERROR: --validate-eps-rbar requires --input")
        out_dir = args.out_dir or os.path.join(os.getcwd(), "dplr_validation_eps_rbar_out")
        method_keys = _resolve_validation_method_keys(
            args.validation_methods,
            fast=bool(args.validation_fast),
        )
        cli_validate_eps_rbar_against_dplr(
            input_path=args.input,
            out_dir=out_dir,
            po_path=args.po,
            python_compat=cfg.compat,
            difference=cfg.difference,
            pos_slope=cfg.pos_slope,
            constrain_nls=cfg.constrain_nls,
            recode_zeros=cfg.recode_zeros,
            rscript=cfg.rscript,
            auto_install_r=cfg.auto_install_r,
            auto_install_r_deps=cfg.auto_install_r_deps,
            cran_repo=cfg.cran_repo,
            method_keys=method_keys,
            verbose=bool(args.verbose_progress),
        )
        raise SystemExit(0)

    if args.batch:
        if not args.input or not args.output:
            raise SystemExit("ERROR: --batch requires --input and --output")
        cli_run_and_export_best(
            input_path=args.input,
            goal=args.goal,
            output_path=args.output,
            climate_path=args.climate,
            export_all_methods=bool(args.export_all_methods),
            compat_config=cfg,
            po_path=args.po,
        )
        raise SystemExit(0)

    # Fallback: if args provided but no mode chosen, run GUI.
    main()
