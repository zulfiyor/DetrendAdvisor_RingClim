import numpy as np
"""
Ring-Climate Analyzer (PySide6)

Updated import dialog and dataset import logic to better handle:
- files without headers (assign names col_1..col_n and allow renaming)
- files with metadata lines at top (skip rows) and files with comment lines starting with '#'
- files with extra leading columns (station id) and wide (Year + 12 months) or two-column chronology formats
- automatic suggestions (auto-detect) for common layouts and prefill rename suggestions
- ability to edit dataset columns after import (Edit Selected)

How to use:
- Add / Import File -> in import dialog:
  - Set "Skip top rows" if the file contains metadata above the table.
  - Check "Treat lines starting with '#' as comments" to automatically ignore comment lines.
  - Toggle "File has header row" depending on whether the file contains column names.
  - Use "Edit / Filter Columns" to keep/rename columns.
  - Use "Auto-detect layout" to let the dialog propose best import settings for typical formats
    (Year+value, Year+12 months with leading station id, stacked monthly, etc.).
- After import you can Edit Selected dataset to rename/drop columns and update dataset metadata.

Notes:
- This file keeps earlier features (Top-N filtering, exclude single-month windows, plotting, exporting).
- For large customizations further changes may be needed, but this update should cover the examples you provided.
"""
import sys
import os

# Shared modules (dendro_*.py) live in the "dendro_modules" folder next to this script.
# In the built program (PyInstaller) they are bundled, so the folder is not needed there.
_DENDRO_MODULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dendro_modules")
if os.path.isdir(_DENDRO_MODULES_DIR) and _DENDRO_MODULES_DIR not in sys.path:
    sys.path.insert(0, _DENDRO_MODULES_DIR)
import json
import math
import re
from typing import List, Dict, Optional, Tuple, Any
import numpy as np
import pandas as pd
import io
import gzip
import shutil
import subprocess
import tempfile
import traceback

# optional scipy for p-values
try:
    from scipy import stats
except Exception:
    stats = None
    import warnings
    warnings.warn("scipy not available: p-values will be NaN. Install scipy for p-values.")

# optional xarray for large gridded NetCDF (CRU TS .dat.nc etc.)
try:
    import xarray as xr
except Exception:
    xr = None

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QScrollArea,
    QWidget,
    QFileDialog,
    QMessageBox,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTabWidget,
    QGroupBox,
    QComboBox,
    QSpinBox,
    QCheckBox,
    QTableView,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QTextEdit,
    QRadioButton,
    QButtonGroup,
    QLineEdit,
    QGridLayout,
    QFormLayout,
    QProgressBar,
    QPlainTextEdit,
    QDoubleSpinBox,
    QSizePolicy,
    QStatusBar,
    QTextBrowser,
    QMenu,
    QLayout,
)
from PySide6.QtCore import (
    Qt,
    QByteArray,
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Signal,
    QThread,
    QTimer,
    QEvent,
    QPoint,
    QRect,
    QSize,
)
from PySide6.QtGui import QFont
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtGui import QGuiApplication, QFontMetrics
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QProgressDialog
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
from typing import Optional, Dict, Any, List, Tuple

# Month names used throughout (English short names)
MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# Shared correlation plots / style: dendro_plots.py (optional)
try:
    import dendro_plots as _dplots
except Exception:
    _dplots = None

PLOT_MODES = [
    "Overview",
    "Monthly correlations",
    "Window heatmap",
    "Top windows",
    "Time series",
    "Running correlation",
    "Scatter",
]

# Optional online data access (ITRDB + CRU TS): dendro_online.py / dendro_online_qt.py
try:
    import dendro_online_qt as _online_qt
    _ONLINE_IMPORT_ERROR = ""
except Exception as _online_exc:  # keep RingClim usable without the module
    _online_qt = None
    _ONLINE_IMPORT_ERROR = f"{type(_online_exc).__name__}: {_online_exc}"
# Spatial (field) correlation page: dendro_field.py / dendro_field_qt.py (optional)
try:
    import dendro_field as _field
    import dendro_field_qt as _field_qt
    _FIELD_IMPORT_ERROR = ""
except Exception as _field_exc:
    _field = None
    _field_qt = None
    _FIELD_IMPORT_ERROR = f"{type(_field_exc).__name__}: {_field_exc}"
# Moving correlation page: dendro_moving.py / dendro_moving_qt.py (optional)
try:
    import dendro_moving_qt as _moving_qt
    _MOVING_IMPORT_ERROR = ""
except Exception as _moving_exc:  # pragma: no cover
    _moving_qt = None
    _MOVING_IMPORT_ERROR = f"{type(_moving_exc).__name__}: {_moving_exc}"
# Developer signature + hover help on every dialog: dendro_ui_help.py (optional)
try:
    import dendro_ui_help as _uihelp
except Exception:
    _uihelp = None
# Releases, automatic updates, guides and contact: app_update*.py (optional)
try:
    import app_update as _appupd
    import app_update_qt as _appupd_qt
except Exception:  # pragma: no cover
    _appupd = None
    _appupd_qt = None
class PrimaryButton(QPushButton):
    """Green bold button whose size hint uses the bold font of the style sheet (no clipped text)."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setProperty("primary", True)
        self.setMinimumHeight(34)

    def _text_width(self) -> int:
        f = QFont(self.font())
        f.setBold(True)
        return QFontMetrics(f).horizontalAdvance(self.text()) + 2 * 18 + 16

    def sizeHint(self):  # noqa: N802
        sh = super().sizeHint()
        sh.setWidth(max(sh.width(), self._text_width()))
        return sh

    def minimumSizeHint(self):  # noqa: N802
        sh = super().minimumSizeHint()
        sh.setWidth(max(sh.width(), self._text_width()))
        return sh


class FlowLayout(QLayout):
    """Layout that places widgets in rows and wraps them when the window is narrow
    (buttons are never cut on small screens or with large Windows scaling)."""

    def __init__(self, parent=None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 (Qt API)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = eff.x(), eff.y(), 0
        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisible() and not test_only:
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y = y + line_h + self._spacing
                next_x = x + hint.width() + self._spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class FlowBar(QWidget):
    """Widget holding a FlowLayout; its height follows the number of wrapped rows (no overlapping)."""

    def __init__(self, flow: "FlowLayout", parent=None) -> None:
        super().__init__(parent)
        self._flow = flow
        self.setLayout(flow)
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        sp.setVerticalPolicy(QSizePolicy.Fixed)
        self.setSizePolicy(sp)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._flow.heightForWidth(width)

    def sizeHint(self):  # noqa: N802
        w = self.width() if self.width() > 50 else 1200
        return QSize(w, self._flow.heightForWidth(w))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        h = self._flow.heightForWidth(event.size().width())
        if h != self.minimumHeight():
            self.setMinimumHeight(h)
            self.setMaximumHeight(h)


ROMAN_MONTH_NAMES = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]


def _normalize_month_header_token(token: Any) -> Optional[str]:
    """Map common month header variants to canonical English short names."""
    if token is None:
        return None

    s = str(token).strip()
    if not s:
        return None

    sl = s.lower()

    # English short names
    for idx, name in enumerate(MONTH_NAMES):
        if sl == name.lower():
            return MONTH_NAMES[idx]

    # Russian short names
    ru_months = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    for idx, name in enumerate(ru_months):
        if sl == name:
            return MONTH_NAMES[idx]

    # Roman numerals I..XII
    su = s.upper()
    for idx, name in enumerate(ROMAN_MONTH_NAMES):
        if su == name:
            return MONTH_NAMES[idx]

    # Legacy Y1..Y12 month headers, but not Year/Years
    if su.startswith("Y") and su not in ("YEAR", "YEARS"):
        num_part = su[1:]
        if num_part.isdigit():
            month_idx = int(num_part)
            if 1 <= month_idx <= 12:
                return MONTH_NAMES[month_idx - 1]

    return None


def _looks_like_year_column_name(name: Any) -> bool:
    """Return True if the column name looks like a year field."""
    if name is None:
        return False
    s = str(name).strip().lower()
    return s in {
        "year", "years", "год", "годы", "yr", "yrs",
        "year_1", "col_1",
    }


def _detect_station_metadata_columns(columns: List[Any]) -> Dict[str, Optional[str]]:
    """Detect common station metadata columns by header name."""
    alias_map = {
        "country": {
            "country", "country_id", "countrycode", "cntr", "nation",
        },
        "code_wmo": {
            "code_wmo", "wmo", "wmo_code", "station_id", "stationid",
            "station_code", "code",
        },
        "name": {
            "name", "station", "station_name", "stationname",
        },
    }

    out: Dict[str, Optional[str]] = {
        "country": None,
        "code_wmo": None,
        "name": None,
    }
    for col in columns:
        s = str(col).strip().lower()
        for key, aliases in alias_map.items():
            if out[key] is None and s in aliases:
                out[key] = str(col)
    return out


def _detect_monthly_wide_columns(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Detect a wide monthly table with one year column and 12 month columns."""
    if df is None or df.empty:
        return None

    cols = list(df.columns.astype(str))
    if not cols:
        return None

    def _series_looks_like_years(series: pd.Series) -> bool:
        try:
            nums = pd.to_numeric(series.dropna(), errors="coerce")
            nums = nums[nums.notna()]
            if nums.empty:
                return False
            mn = float(nums.min())
            mx = float(nums.max())
            return (mn >= 1800 and mx <= 2100) or (mn >= 100 and mx <= 3000 and len(nums) >= 10)
        except Exception:
            return False

    year_col: Optional[str] = None
    for col in cols:
        if _looks_like_year_column_name(col):
            year_col = col
            break

    if year_col is None:
        for col in cols:
            try:
                if _series_looks_like_years(df[col]):
                    year_col = col
                    break
            except Exception:
                continue

    month_hits: List[Tuple[int, str]] = []
    for col in cols:
        canonical = _normalize_month_header_token(col)
        if canonical is None:
            continue
        month_hits.append((MONTH_NAMES.index(canonical), col))

    month_map: Dict[int, str] = {}
    for month_idx, col_name in month_hits:
        if month_idx not in month_map:
            month_map[month_idx] = col_name

    if year_col is None or len(month_map) < 12:
        return None

    month_cols = [month_map[i] for i in range(12) if i in month_map]
    if len(month_cols) != 12:
        return None

    metadata_cols = [c for c in cols if c not in ([year_col] + month_cols)]
    return {
        "year_col": year_col,
        "month_cols": month_cols,
        "metadata_cols": metadata_cols,
        "station_meta": _detect_station_metadata_columns(metadata_cols),
    }

# Daily labels used in daily-resolution analysis.
# Leap-day is included as Feb29 so the full daily table has 366 positions.
DAILY_LABELS_366 = (
    [f"Jan{d:02d}" for d in range(1, 32)]
    + [f"Feb{d:02d}" for d in range(1, 30)]
    + [f"Mar{d:02d}" for d in range(1, 32)]
    + [f"Apr{d:02d}" for d in range(1, 31)]
    + [f"May{d:02d}" for d in range(1, 32)]
    + [f"Jun{d:02d}" for d in range(1, 31)]
    + [f"Jul{d:02d}" for d in range(1, 32)]
    + [f"Aug{d:02d}" for d in range(1, 32)]
    + [f"Sep{d:02d}" for d in range(1, 31)]
    + [f"Oct{d:02d}" for d in range(1, 32)]
    + [f"Nov{d:02d}" for d in range(1, 31)]
    + [f"Dec{d:02d}" for d in range(1, 32)]
)
DAILY_LABEL_TO_INDEX = {lab: idx for idx, lab in enumerate(DAILY_LABELS_366)}


def _is_grdc_mean_monthly_discharge_file(path: str) -> bool:
    """
    Quick heuristic to detect a GRDC Station Data File containing Mean Monthly Discharge.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = "".join([f.readline() for _ in range(200)])
        h = head.lower()
        if "grdc station data file" not in h and "grdc" not in h:
            return False
        return ("mean monthly discharge" in h) or ("data set content" in h and "monthly" in h and "discharge" in h)
    except Exception:
        return False


def _ringclim_import_debug_log_path() -> str:
    """Return the debug-log path used for frozen import diagnostics."""
    return os.path.join(tempfile.gettempdir(), "ringclim_import_debug.log")


def _append_import_debug_log_hint(message: str) -> str:
    """Append the debug-log path to an auto-import failure message."""
    base = str(message or "").rstrip()
    log_path = _ringclim_import_debug_log_path()
    if not base:
        base = "Automatic import failed for an unknown reason."
    return (
        f"{base}\n\n"
        f"Debug log path:\n{log_path}\n\n"
        "If you are using the frozen EXE build, open this log after the failed import "
        "to see the exact parser/import error."
    )


def _parse_grdc_mean_monthly_discharge_to_wide(path: str) -> tuple[pd.DataFrame, dict]:
    """
    Parse GRDC Mean Monthly Discharge station file into wide format: Year + 12 months.

    Rules:
    - Data section starts after a line "DATA" and ends before a line starting with "END DATA".
    - Table delimiter is ';'.
    - Date column is usually "YYYY-MM-DD" (monthly series often uses day=01).
    - Prefer "Original" values unless they are missing (-999.000 or NaN); then use "Calculated".
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    meta: dict = {"source": "GRDC", "source_path": path}
    for ln in lines[:200]:
        if "Data Set Content" in ln:
            meta["data_set_content"] = ln.strip()
        if "Station" in ln and "Number" in ln:
            meta["station_line"] = ln.strip()
    # Find DATA section markers. Some GRDC exports use '# DATA' / '# END DATA'.
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.upper() == "DATA":
            start = i + 1
            break
        if s.startswith("#") and s.lstrip("#").strip().upper() == "DATA":
            start = i + 1
            break

    # Fallback: treat the first non-comment semicolon header as the start.
    if start is None:
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if ";" in s:
                start = i
                break
    if start is None:
        raise ValueError("GRDC parser: DATA section not found.")

    end = None
    for i in range(start, len(lines)):
        s = lines[i].strip()
        if s.upper().startswith("END DATA"):
            end = i
            break
        if s.startswith("#") and s.lstrip("#").strip().upper().startswith("END DATA"):
            end = i
            break
    if end is None:
        end = len(lines)


    table_lines = [
        ln for ln in lines[start:end]
        if ln.strip() and (not ln.lstrip().startswith("#"))
    ]
    if not table_lines:
        raise ValueError("GRDC parser: no data lines found in DATA section.")

    df_raw = pd.read_csv(io.StringIO("\n".join(table_lines)), sep=";", engine="python")
    df_raw.columns = [str(c).strip() for c in df_raw.columns]
    df_raw = _drop_empty_columns(df_raw)

    if df_raw is None or df_raw.empty:
        raise ValueError("GRDC parser: data section is empty.")

    # Identify main columns
    date_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if "yyyy" in cu or cu == "date" or "yyyy-mm-dd" in cu:
            date_col = c
            break
    if date_col is None:
        date_col = df_raw.columns[0]

    orig_col = None
    calc_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu.startswith("original"):
            orig_col = c
        if cu.startswith("calculated"):
            calc_col = c

    def _num(col: str) -> pd.Series:
        if col is None or col not in df_raw.columns:
            return pd.Series([np.nan] * len(df_raw))
        return pd.to_numeric(df_raw[col], errors="coerce")

    s_orig = _num(orig_col)
    s_calc = _num(calc_col)

    # Prefer "Original" when present, otherwise fallback to "Calculated".
    # Missing is typically encoded as -999 (sometimes other <= -900 sentinels).
    missing_orig = s_orig.isna() | (s_orig <= -900.0)
    missing_calc = s_calc.isna() | (np.isclose(s_calc, -999.0) | (s_calc <= -998.999))

    value = s_orig.copy()
    use_calc = missing_orig & ~missing_calc
    value[use_calc] = s_calc[use_calc]

    # Normalize missing sentinel values to NaN.
    value = value.where(~(value <= -900.0), np.nan)

    # Normalize monthly date formats.
    # GRDC monthly files may use YYYY-MM-DD with DD=00 (or DD=01), or sometimes YYYY-MM.
    date_s = df_raw[date_col].astype(str).str.strip()
    date_s = date_s.str.replace(r"^(\d{4}-\d{2})$", r"\1-01", regex=True)
    date_s = date_s.str.replace(r"^(\d{4}-\d{2})-00(\b)", r"\1-01\2", regex=True)

    dt = pd.to_datetime(date_s, errors="coerce")

    tmp = pd.DataFrame({"Year": dt.dt.year, "Month": dt.dt.month, "Value": value})
    tmp = tmp.dropna(subset=["Year", "Month"])
    tmp["Year"] = tmp["Year"].astype(int)
    tmp["Month"] = tmp["Month"].astype(int)

    # If duplicates exist, average within month
    tmp = tmp.groupby(["Year", "Month"], as_index=False)["Value"].mean()

    wide = tmp.pivot(index="Year", columns="Month", values="Value").reindex(columns=list(range(1, 13)))
    wide.columns = MONTH_NAMES
    wide = wide.reset_index().sort_values("Year").reset_index(drop=True)
    return wide, meta


def try_import_grdc_monthly(app: Any, path: str) -> bool:
    """
    Auto-import GRDC mean monthly discharge file into the app (best-effort).
    Returns True only if import succeeded.
    """
    setattr(app, "_last_grdc_monthly_error", "")

    if not _is_grdc_mean_monthly_discharge_file(path):
        return False

    try:
        wide, meta = _parse_grdc_mean_monthly_discharge_to_wide(path)
    except Exception as exc:
        setattr(
            app,
            "_last_grdc_monthly_error",
            "GRDC monthly file was detected, but parsing failed.\n\n"
            f"{exc}\n\n"
            f"Traceback:\n{traceback.format_exc()}",
        )
        return False
    if wide is None or wide.empty:
        setattr(
            app,
            "_last_grdc_monthly_error",
            "GRDC monthly file was detected, but the parsed table is empty.",
        )
        return False

    base = os.path.splitext(os.path.basename(path))[0]
    ds_name = base

    # Prefer using the app's native import pipeline if available.
    mappings = {
        "layout": "wide",
        "year_col": "Year",
        "month_cols": MONTH_NAMES,
        "dataset_type": "Climate",
        "climate_param": "Runoff",
    }

    if hasattr(app, "_create_datasets_from_mapping"):
        try:
            app._create_datasets_from_mapping(wide, ds_name, path, mappings)
        except Exception as exc_map:
            # Fallback: create a DataSet object directly (avoid dict entries in app.datasets).
            try:
                ds = DataSet(name=ds_name, path=path, df=wide, kind="climate")
                ds.year_col = "Year"
                ds.month_columns = MONTH_NAMES[:]
                ds.climate_param = "Runoff"
                setattr(ds, "meta", meta)

                if hasattr(app, "datasets") and isinstance(getattr(app, "datasets"), list):
                    app.datasets.append(ds)

                if hasattr(app, "list_datasets"):
                    try:
                        item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
                        item.setData(Qt.UserRole, ds)
                        app.list_datasets.addItem(item)
                    except Exception:
                        pass
            except Exception as exc_fallback:
                setattr(app, "_last_import_df", wide)
                setattr(app, "_last_import_meta", meta)
                setattr(
                    app,
                    "_last_grdc_monthly_error",
                    "GRDC monthly file was parsed successfully, but importing it into the application failed.\n\n"
                    f"Mapping error:\n{exc_map}\n\n"
                    f"Fallback error:\n{exc_fallback}\n\n"
                    f"Traceback:\n{traceback.format_exc()}",
                )
                return False
    else:
        # Minimal fallback without mapping pipeline.
        try:
            ds = DataSet(name=ds_name, path=path, df=wide, kind="climate")
            ds.year_col = "Year"
            ds.month_columns = MONTH_NAMES[:]
            ds.climate_param = "Runoff"
            setattr(ds, "meta", meta)

            if hasattr(app, "datasets") and isinstance(getattr(app, "datasets"), list):
                app.datasets.append(ds)

            if hasattr(app, "list_datasets"):
                try:
                    item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
                    item.setData(Qt.UserRole, ds)
                    app.list_datasets.addItem(item)
                except Exception:
                    pass
        except Exception as exc:
            setattr(app, "_last_import_df", wide)
            setattr(app, "_last_import_meta", meta)
            setattr(
                app,
                "_last_grdc_monthly_error",
                "GRDC monthly file was parsed successfully, but fallback dataset creation failed.\n\n"
                f"{exc}\n\n"
                f"Traceback:\n{traceback.format_exc()}",
            )
            return False
            setattr(
                app,
                "_last_grdc_monthly_error",
                "GRDC monthly file was parsed successfully, but fallback dataset creation failed.\n\n"
                f"{exc}\n\n"
                f"Traceback:\n{traceback.format_exc()}",
            )
            return False


    # Best-effort UI refresh (do not fail import if UI hooks are missing).
    for fn in ("update_data_view", "refresh_dataset_tree", "update_dataset_tree", "refresh_views"):
        if hasattr(app, fn):
            try:
                getattr(app, fn)()
            except Exception:
                pass

    setattr(app, "_last_grdc_monthly_error", "")
    setattr(app, "_last_grdc_monthly_error", "")
    return True



def _grdc_declares_zero_data_lines(path: str) -> bool:
    """Return True if the GRDC header explicitly declares 0 data lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = "".join([f.readline() for _ in range(400)])
        h = head.lower()
        if "grdc" not in h:
            return False
        m = re.search(r"data\s+lines\s*:\s*(\d+)", head, flags=re.IGNORECASE)
        if not m:
            return False
        return int(m.group(1)) == 0
    except Exception:
        return False


def _is_grdc_discharge_timeseries_file(path: str) -> bool:
    """Detect GRDC daily/hourly discharge station files (non-monthly)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = "".join([f.readline() for _ in range(250)])
        h = head.lower()
        if "grdc station data file" not in h and "grdc" not in h:
            return False
        if "mean monthly discharge" in h:
            return False
        if "mean daily discharge" in h or "mean hourly discharge" in h:
            return True
        if "data set content" in h and "discharge" in h:
            # Accept daily/hourly mentions, reject monthly.
            return ("daily" in h or "hourly" in h) and ("monthly" not in h)
        return False
    except Exception:
        return False


def _parse_grdc_discharge_timeseries(path: str) -> tuple[pd.DataFrame, dict]:
    """
    Parse GRDC daily/hourly discharge into a two-column table: DateTime + Value.

    Notes:
    - No aggregation is performed; original resolution is preserved.
    - Prefer the 'Value' column when present. Otherwise prefer 'Original' unless missing,
      then fall back to 'Calculated'. Missing sentinel is typically -999.000.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    meta: dict = {"source": "GRDC", "source_path": path}
    for ln in lines[:250]:
        if "Data Set Content" in ln:
            meta["data_set_content"] = ln.strip()

    # Locate DATA section.
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.upper() == "DATA":
            start = i + 1
            break
        if s.startswith("#") and s.lstrip("#").strip().upper() == "DATA":
            start = i + 1
            break
    if start is None:
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if ";" in s:
                start = i
                break
    if start is None:
        raise ValueError("GRDC parser: DATA section not found.")

    end = None
    for i in range(start, len(lines)):
        s = lines[i].strip()
        if s.upper().startswith("END DATA"):
            end = i
            break
        if s.startswith("#") and s.lstrip("#").strip().upper().startswith("END DATA"):
            end = i
            break
    if end is None:
        end = len(lines)

    table_lines = [ln for ln in lines[start:end] if ln.strip() and (not ln.lstrip().startswith("#"))]
    if not table_lines:
        raise ValueError("GRDC parser: no data lines found in DATA section.")

    df_raw = pd.read_csv(io.StringIO("\n".join(table_lines)), sep=";", engine="python")
    df_raw.columns = [str(c).strip() for c in df_raw.columns]
    df_raw = _drop_empty_columns(df_raw)
    if df_raw is None or df_raw.empty:
        raise ValueError("GRDC parser: data section is empty.")

    # Identify date/time columns.
    date_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu == "date" or "yyyy-mm-dd" in cu or "yyyy" in cu:
            date_col = c
            break
    if date_col is None:
        date_col = df_raw.columns[0]

    time_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu in ("hh:mm", "time") or "hh:mm" in cu:
            time_col = c
            break

    value_col = None
    orig_col = None
    calc_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu.startswith("value"):
            value_col = c
        elif cu.startswith("original"):
            orig_col = c
        elif cu.startswith("calculated"):
            calc_col = c

    def _num(col: Optional[str]) -> pd.Series:
        if not col or col not in df_raw.columns:
            return pd.Series([np.nan] * len(df_raw))
        return pd.to_numeric(df_raw[col], errors="coerce")

    if value_col and value_col in df_raw.columns:
        s_val = _num(value_col)
    else:
        s_orig = _num(orig_col)
        s_calc = _num(calc_col)
        missing_orig = s_orig.isna() | (s_orig <= -900.0)
        missing_calc = s_calc.isna() | (s_calc <= -900.0)
        s_val = s_orig.copy()
        use_calc = missing_orig & ~missing_calc
        s_val[use_calc] = s_calc[use_calc]

    s_val = s_val.where(~(s_val <= -900.0), np.nan)

    date_s = df_raw[date_col].astype(str).str.strip()
    # If only YYYY-MM is present, normalize it for parsing.
    date_s = date_s.str.replace(r"^(\d{4}-\d{2})$", r"\1-01", regex=True)
    date_s = date_s.str.replace(r"^(\d{4}-\d{2})-00(\b)", r"\1-01\2", regex=True)

    if time_col and time_col in df_raw.columns:
        time_s = df_raw[time_col].astype(str).str.strip().replace({"--:--": "00:00"})
        dt = pd.to_datetime(date_s + " " + time_s, errors="coerce")
        uniq_times = pd.Series(time_s).dropna().astype(str).str.strip()
        uniq_times = [t for t in uniq_times.unique().tolist() if t]
        meta["frequency"] = "hourly" if len(set(uniq_times)) > 1 else "daily"
    else:
        dt = pd.to_datetime(date_s, errors="coerce")
        meta["frequency"] = "daily"

    out = pd.DataFrame({"DateTime": dt, "Value": s_val})
    out = out[out["DateTime"].notna()].copy()
    out = out.sort_values("DateTime").reset_index(drop=True)
    return out, meta


def try_import_grdc_discharge_timeseries(app: Any, path: str) -> bool:
    """Auto-import GRDC daily/hourly discharge file (best-effort)."""
    setattr(app, "_last_grdc_timeseries_error", "")

    if not _is_grdc_discharge_timeseries_file(path):
        return False
    if _grdc_declares_zero_data_lines(path):
        setattr(
            app,
            "_last_grdc_timeseries_error",
            "GRDC discharge file was detected, but the header declares 0 data lines.",
        )
        return False

    try:
        df_ts, meta = _parse_grdc_discharge_timeseries(path)
    except Exception as exc:
        setattr(
            app,
            "_last_grdc_timeseries_error",
            "GRDC discharge time-series file was detected, but parsing failed.\n\n"
            f"{exc}\n\n"
            f"Traceback:\n{traceback.format_exc()}",
        )
        return False
    if df_ts is None or df_ts.empty:
        setattr(
            app,
            "_last_grdc_timeseries_error",
            "GRDC discharge time-series file was detected, but the parsed table is empty.",
        )
        return False

    base = os.path.splitext(os.path.basename(path))[0]
    ds_name = base

    mappings = {
        "layout": "datetime_series",
        "datetime_col": "DateTime",
        "value_col": "Value",
        "dataset_type": "TimeSeries",
        "source_freq": meta.get("frequency", "daily"),
        "climate_param": "Runoff",
    }

    if hasattr(app, "_create_datasets_from_mapping"):
        try:
            app._create_datasets_from_mapping(df_ts, ds_name, path, mappings)
        except Exception as exc_map:
            # Fallback: create a DataSet object directly.
            try:
                ds = DataSet(name=ds_name, path=path, df=df_ts, kind="timeseries")
                ds.year_col = "DateTime"
                ds.value_columns = ["Value"]
                ds.month_columns = []
                ds.climate_param = "Runoff"
                setattr(ds, "source_freq", meta.get("frequency", "daily"))
                setattr(ds, "meta", meta)

                if hasattr(app, "datasets") and isinstance(getattr(app, "datasets"), list):
                    app.datasets.append(ds)

                if hasattr(app, "list_datasets"):
                    try:
                        freq = getattr(ds, "source_freq", "daily")
                        item = QListWidgetItem(f"{ds.name} (timeseries: {freq})")
                        item.setData(Qt.UserRole, ds)
                        app.list_datasets.addItem(item)
                    except Exception:
                        pass
            except Exception as exc_fallback:
                setattr(
                    app,
                    "_last_grdc_timeseries_error",
                    "GRDC discharge time-series file was parsed successfully, but importing it into the application failed.\n\n"
                    f"Mapping error:\n{exc_map}\n\n"
                    f"Fallback error:\n{exc_fallback}\n\n"
                    f"Traceback:\n{traceback.format_exc()}",
                )
                return False
    else:
        try:
            ds = DataSet(name=ds_name, path=path, df=df_ts, kind="timeseries")
            ds.year_col = "DateTime"
            ds.value_columns = ["Value"]
            ds.month_columns = []
            ds.climate_param = "Runoff"
            setattr(ds, "source_freq", meta.get("frequency", "daily"))
            setattr(ds, "meta", meta)
            if hasattr(app, "datasets") and isinstance(getattr(app, "datasets"), list):
                app.datasets.append(ds)
            if hasattr(app, "list_datasets"):
                try:
                    freq = getattr(ds, "source_freq", "daily")
                    item = QListWidgetItem(f"{ds.name} (timeseries: {freq})")
                    item.setData(Qt.UserRole, ds)
                    app.list_datasets.addItem(item)
                except Exception:
                    pass
        except Exception as exc:
            setattr(
                app,
                "_last_grdc_timeseries_error",
                "GRDC discharge time-series file was parsed successfully, but fallback dataset creation failed.\n\n"
                f"{exc}\n\n"
                f"Traceback:\n{traceback.format_exc()}",
            )
            return False

    for fn in ("update_data_view", "refresh_dataset_tree", "update_dataset_tree", "refresh_views"):
        if hasattr(app, fn):
            try:
                getattr(app, fn)()
            except Exception:
                pass

    setattr(app, "_last_grdc_timeseries_error", "")
    return True

 
def _ringclim_import_debug_log(message: str) -> None:
    """Write import diagnostics to a persistent temp log for frozen builds."""
    try:
        log_path = os.path.join(tempfile.gettempdir(), "ringclim_import_debug.log")
        with open(log_path, "a", encoding="utf-8") as f:
            ts = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {message.rstrip()}\n")
    except Exception:
        pass


def _is_grdc_discharge_timeseries_file(path: str) -> bool:
    """Detect GRDC daily/hourly discharge station files (non-monthly)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = "".join([f.readline() for _ in range(250)])
        h = head.lower()
        if "grdc station data file" not in h and "grdc" not in h:
            return False
        if "mean monthly discharge" in h:
            return False
        if "mean daily discharge" in h or "mean hourly discharge" in h:
            return True
        if "data set content" in h and "discharge" in h:
            return ("daily" in h or "hourly" in h) and ("monthly" not in h)
        return False
    except Exception as exc:
        _ringclim_import_debug_log(
            "GRDC detect error for '{}': {}\n{}".format(
                path, exc, traceback.format_exc()
            )
        )
        return False


def _parse_grdc_discharge_timeseries(path: str) -> tuple[pd.DataFrame, dict]:
    """
    Parse GRDC daily/hourly discharge into a two-column table: DateTime + Value.

    Notes:
    - No aggregation is performed; original resolution is preserved.
    - Prefer the 'Value' column when present. Otherwise prefer 'Original' unless missing,
      then fall back to 'Calculated'. Missing sentinel is typically -999.000.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    meta: dict = {"source": "GRDC", "source_path": path}
    for ln in lines[:250]:
        if "Data Set Content" in ln:
            meta["data_set_content"] = ln.strip()

    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.upper() == "DATA":
            start = i + 1
            break
        if s.startswith("#") and s.lstrip("#").strip().upper() == "DATA":
            start = i + 1
            break
    if start is None:
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if ";" in s:
                start = i
                break
    if start is None:
        raise ValueError("GRDC parser: DATA section not found.")

    end = None
    for i in range(start, len(lines)):
        s = lines[i].strip()
        if s.upper().startswith("END DATA"):
            end = i
            break
        if s.startswith("#") and s.lstrip("#").strip().upper().startswith("END DATA"):
            end = i
            break
    if end is None:
        end = len(lines)

    table_lines = [ln for ln in lines[start:end] if ln.strip() and (not ln.lstrip().startswith("#"))]
    if not table_lines:
        raise ValueError("GRDC parser: no data lines found in DATA section.")

    df_raw = pd.read_csv(io.StringIO("\n".join(table_lines)), sep=";", engine="python")
    df_raw.columns = [str(c).strip() for c in df_raw.columns]
    df_raw = _drop_empty_columns(df_raw)
    if df_raw is None or df_raw.empty:
        raise ValueError("GRDC parser: data section is empty.")

    date_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu == "date" or "yyyy-mm-dd" in cu or "yyyy" in cu:
            date_col = c
            break
    if date_col is None:
        date_col = df_raw.columns[0]

    time_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu in ("hh:mm", "time") or "hh:mm" in cu:
            time_col = c
            break

    value_col = None
    orig_col = None
    calc_col = None
    for c in df_raw.columns:
        cu = str(c).strip().lower()
        if cu.startswith("value"):
            value_col = c
        elif cu.startswith("original"):
            orig_col = c
        elif cu.startswith("calculated"):
            calc_col = c

    def _num(col: Optional[str]) -> pd.Series:
        if not col or col not in df_raw.columns:
            return pd.Series([np.nan] * len(df_raw))
        return pd.to_numeric(df_raw[col], errors="coerce")

    if value_col and value_col in df_raw.columns:
        s_val = _num(value_col)
    else:
        s_orig = _num(orig_col)
        s_calc = _num(calc_col)
        missing_orig = s_orig.isna() | (s_orig <= -900.0)
        missing_calc = s_calc.isna() | (s_calc <= -900.0)
        s_val = s_orig.copy()
        use_calc = missing_orig & ~missing_calc
        s_val[use_calc] = s_calc[use_calc]

    s_val = s_val.where(~(s_val <= -900.0), np.nan)

    date_s = df_raw[date_col].astype(str).str.strip()
    date_s = date_s.str.replace(r"^(\d{4}-\d{2})$", r"\1-01", regex=True)
    date_s = date_s.str.replace(r"^(\d{4}-\d{2})-00(\b)", r"\1-01\2", regex=True)

    if time_col and time_col in df_raw.columns:
        time_s = df_raw[time_col].astype(str).str.strip().replace({"--:--": "00:00"})
        dt = pd.to_datetime(date_s + " " + time_s, errors="coerce")
        uniq_times = pd.Series(time_s).dropna().astype(str).str.strip()
        uniq_times = [t for t in uniq_times.unique().tolist() if t]
        meta["frequency"] = "hourly" if len(set(uniq_times)) > 1 else "daily"
    else:
        dt = pd.to_datetime(date_s, errors="coerce")
        meta["frequency"] = "daily"

    out = pd.DataFrame({"DateTime": dt, "Value": s_val})
    out = out[out["DateTime"].notna()].copy()
    out = out.sort_values("DateTime").reset_index(drop=True)
    return out, meta


def try_import_grdc_discharge_timeseries(app: Any, path: str) -> bool:
    """Auto-import GRDC daily/hourly discharge file with persistent diagnostics."""
    setattr(app, "_last_grdc_timeseries_error", "")

    detected = _is_grdc_discharge_timeseries_file(path)
    _ringclim_import_debug_log(f"GRDC timeseries detect: path='{path}' detected={detected}")
    if not detected:
        return False

    if _grdc_declares_zero_data_lines(path):
        msg = "GRDC discharge file was detected, but the header declares 0 data lines."
        setattr(app, "_last_grdc_timeseries_error", msg)
        _ringclim_import_debug_log(msg)
        return False

    try:
        df_ts, meta = _parse_grdc_discharge_timeseries(path)
    except Exception as exc:
        msg = (
            "GRDC discharge time-series file was detected, but parsing failed.\n\n"
            f"{exc}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )
        setattr(app, "_last_grdc_timeseries_error", msg)
        _ringclim_import_debug_log(msg)
        return False

    if df_ts is None or df_ts.empty:
        msg = "GRDC discharge time-series file was detected, but the parsed table is empty."
        setattr(app, "_last_grdc_timeseries_error", msg)
        _ringclim_import_debug_log(msg)
        return False

    base = os.path.splitext(os.path.basename(path))[0]
    ds_name = base

    try:
        ds = DataSet(name=ds_name, path=path, df=df_ts, kind="timeseries")
        ds.year_col = "DateTime"
        ds.value_columns = ["Value"]
        ds.month_columns = []
        ds.climate_param = "Runoff"
        setattr(ds, "source_freq", meta.get("frequency", "daily"))
        setattr(ds, "meta", meta)

        if hasattr(app, "datasets") and isinstance(getattr(app, "datasets"), list):
            app.datasets.append(ds)
        else:
            raise RuntimeError("Application datasets container is not a list.")

        if hasattr(app, "list_datasets"):
            try:
                freq = getattr(ds, "source_freq", "daily")
                item = QListWidgetItem(f"{ds.name} (timeseries: {freq})")
                item.setData(Qt.UserRole, ds)
                app.list_datasets.addItem(item)
            except Exception:
                pass

    except Exception as exc:
        msg = (
            "GRDC discharge time-series file was parsed successfully, but dataset creation failed.\n\n"
            f"{exc}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )
        setattr(app, "_last_grdc_timeseries_error", msg)
        _ringclim_import_debug_log(msg)
        return False

    for fn in (
        "refresh_analysis_lists",
        "update_data_view",
        "refresh_dataset_tree",
        "update_dataset_tree",
        "refresh_views",
    ):
        if hasattr(app, fn):
            try:
                getattr(app, fn)()
            except Exception:
                pass

    setattr(app, "_last_grdc_timeseries_error", "")
    _ringclim_import_debug_log(
        "GRDC timeseries import OK: path='{}' rows={} freq={}".format(
            path, len(df_ts), meta.get("frequency", "daily")
        )
    )
    return True


# Default aggregation by climate parameter
CLIMATE_PARAM_DEFAULT_AGG = {
    # Temperatures
    "Temperature": "mean",          # for legacy projects
    "Temperature (mean)": "mean",
    "Temperature (min)": "mean",
    "Temperature (max)": "mean",
    # Precipitation and others
    "Precipitation": "sum",
    "Runoff": "sum",
    "River discharge": "mean",
    "Soil moisture": "mean",
    "Snow": "sum",
    "PDSI": "mean",
    "Relative humidity": "mean",
    "Sea-level pressure": "mean",
    "Wind speed": "mean",
    "Radiation": "sum",
    # CRU TS variables
    "Diurnal temperature range": "mean",
    "Vapour pressure": "mean",
    "Cloud cover": "mean",
    "PET": "mean",
    "Wet days": "sum",
    "Frost days": "sum",
    # Drought indices / water balance (online sources)
    "SPEI": "mean",
    "SPI": "mean",
    "Climatic water deficit": "sum",
    "Actual evapotranspiration": "sum",
    "Vapour pressure deficit": "mean",
    "Climate moisture index": "sum",
    "Other": "sum",
}

def get_default_agg_for_param(param: str) -> str:
    """Return default aggregation for a given climate variable type."""
    return CLIMATE_PARAM_DEFAULT_AGG.get(param, "sum")


def _resolve_monthly_agg_for_timeseries_dataset(ds: Any, agg_choice: str) -> str:
    """Resolve how a daily/hourly time series should be aggregated to monthly values."""
    choice = str(agg_choice or "").strip()
    if choice in ("sum", "mean"):
        return choice
    return get_default_agg_for_param(getattr(ds, "climate_param", "Other"))


def timeseries_to_monthly_climate_dataset(ds: Any, agg_choice: str = "Auto (by variable)"):
    """
    Convert a daily/hourly dataset (kind="timeseries") to a Year + 12 months climate dataset.

    This keeps the current monthly-correlation engine unchanged while allowing
    daily/hourly inputs to be used from the Analysis tab.
    """
    if ds is None or getattr(ds, "kind", None) != "timeseries":
        raise ValueError("Selected dataset is not a daily/hourly time series.")

    df_src = getattr(ds, "df", None)
    if df_src is None or df_src.empty:
        raise ValueError("Time-series dataset is empty.")

    dt_col = getattr(ds, "year_col", "DateTime") or "DateTime"
    if dt_col not in df_src.columns:
        raise ValueError(f"Datetime column '{dt_col}' was not found.")

    value_cols = [c for c in getattr(ds, "value_columns", []) if c in df_src.columns]
    if value_cols:
        value_col = value_cols[0]
    else:
        candidates = [c for c in df_src.columns if c != dt_col]
        if not candidates:
            raise ValueError("No numeric value column was found in the time-series dataset.")
        value_col = candidates[0]

    work = df_src[[dt_col, value_col]].copy()
    work["__dt__"] = pd.to_datetime(work[dt_col], errors="coerce")
    work["__value__"] = pd.to_numeric(work[value_col], errors="coerce")
    work = work[work["__dt__"].notna()].copy()
    if work.empty:
        raise ValueError("No valid timestamps were found in the selected dataset.")

    work["Year"] = work["__dt__"].dt.year.astype(int)
    work["Month"] = work["__dt__"].dt.month.astype(int)

    monthly_agg = _resolve_monthly_agg_for_timeseries_dataset(ds, agg_choice)
    if monthly_agg == "sum":
        monthly_df = (
            work.groupby(["Year", "Month"], as_index=False)["__value__"]
            .agg(lambda s: s.sum(min_count=1))
            .rename(columns={"__value__": "Value"})
        )
    else:
        monthly_df = (
            work.groupby(["Year", "Month"], as_index=False)["__value__"]
            .mean()
            .rename(columns={"__value__": "Value"})
        )

    if monthly_df.empty:
        raise ValueError("Monthly aggregation produced an empty table.")

    pivot = monthly_df.pivot(index="Year", columns="Month", values="Value")
    pivot = pivot.reindex(columns=list(range(1, 13)))
    pivot.columns = MONTH_NAMES
    pivot = pivot.reset_index().sort_values("Year")

    ds_out = DataSet(name=ds.name, path=ds.path, df=pivot, kind="climate")
    ds_out.year_col = "Year"
    ds_out.month_columns = MONTH_NAMES[:]
    ds_out.climate_param = getattr(ds, "climate_param", "Other")
    setattr(ds_out, "source_freq", getattr(ds, "source_freq", ""))
    setattr(ds_out, "derived_from_kind", "timeseries")
    setattr(ds_out, "monthly_agg", monthly_agg)
    setattr(ds_out, "meta", getattr(ds, "meta", {}))
    return ds_out


def timeseries_to_daily_wide_dataframe(
    ds: Any,
    base_agg_choice: str = "Auto (by variable)",
) -> pd.DataFrame:
    """
    Convert a daily/hourly dataset (kind="timeseries") to a Year + daily calendar table.

    Hourly data are aggregated to daily values first.
    Daily data are kept at daily resolution.
    """
    if ds is None or getattr(ds, "kind", None) != "timeseries":
        raise ValueError("Selected dataset is not a daily/hourly time series.")

    df_src = getattr(ds, "df", None)
    if df_src is None or df_src.empty:
        raise ValueError("Time-series dataset is empty.")

    dt_col = getattr(ds, "year_col", "DateTime") or "DateTime"
    if dt_col not in df_src.columns:
        raise ValueError(f"Datetime column '{dt_col}' was not found.")

    value_cols = [c for c in getattr(ds, "value_columns", []) if c in df_src.columns]
    if value_cols:
        value_col = value_cols[0]
    else:
        candidates = [c for c in df_src.columns if c != dt_col]
        if not candidates:
            raise ValueError("No numeric value column was found in the time-series dataset.")
        value_col = candidates[0]

    work = df_src[[dt_col, value_col]].copy()
    work["__dt__"] = pd.to_datetime(work[dt_col], errors="coerce")
    work["__value__"] = pd.to_numeric(work[value_col], errors="coerce")
    work = work[work["__dt__"].notna()].copy()
    if work.empty:
        raise ValueError("No valid timestamps were found in the selected dataset.")

    work["Year"] = work["__dt__"].dt.year.astype(int)
    work["DayOfYear"] = work["__dt__"].dt.dayofyear.astype(int)

    base_agg = _resolve_monthly_agg_for_timeseries_dataset(ds, base_agg_choice)
    if base_agg == "sum":
        daily_df = (
            work.groupby(["Year", "DayOfYear"], as_index=False)["__value__"]
            .agg(lambda s: s.sum(min_count=1))
            .rename(columns={"__value__": "Value"})
        )
    else:
        daily_df = (
            work.groupby(["Year", "DayOfYear"], as_index=False)["__value__"]
            .mean()
            .rename(columns={"__value__": "Value"})
        )

    if daily_df.empty:
        raise ValueError("Daily aggregation produced an empty table.")

    pivot = daily_df.pivot(index="Year", columns="DayOfYear", values="Value")
    pivot = pivot.reindex(columns=list(range(1, 367)))
    pivot.columns = DAILY_LABELS_366[:]
    pivot = pivot.reset_index().sort_values("Year")
    return pivot



def _window_label_to_idx(label: str) -> Optional[int]:
    """
    Convert a window label (e.g. 'pJan', 'Jan', 'pFeb_Mar', 'L1', 'Y3')
    to a numeric index 0..23, where 0..11 correspond to previous-year
    months (pJan..pDec) and 12..23 to current-year months (Jan..Dec).
    Returns None if the label cannot be mapped.
    """
    s = str(label)
    if not s:
        return None
    # pJan..pDec -> 0..11
    if s.startswith("p") and len(s) > 1:
        m = s[1:4].capitalize()
        if m in MONTH_NAMES:
            return MONTH_NAMES.index(m)
    # Jan..Dec -> 12..23
    m3 = s[:3].capitalize()
    if m3 in MONTH_NAMES:
        return 12 + MONTH_NAMES.index(m3)
    # Legacy notations L1..L12, Y1..Y12
    if s.startswith("L"):
        try:
            return int(s[1:]) - 1
        except Exception:
            return None
    if s.startswith("Y"):
        try:
            return 12 + (int(s[1:]) - 1)
        except Exception:
            return None

    # Daily labels Jan01..Dec31 (+ Feb29)
    if s in DAILY_LABEL_TO_INDEX:
        return 1000 + DAILY_LABEL_TO_INDEX[s]

    # Backward compatibility for older daily labels D001..D366
    su = s.upper()
    if su.startswith("D") and su[1:].isdigit():
        try:
            d = int(su[1:])
            if 1 <= d <= 366:
                return 1000 + d - 1
        except Exception:
            return None
    return None

def _compute_window_sort_key(label: Any) -> Tuple[int, int]:
    """
    Compute a numeric sort key (start_index, length) for a climate window label.

    This is used when exporting correlations so that windows go in the
    same chronological month order as in the heatmap (pJan..pDec, Jan..Dec).
    """
    # push missing/unknown labels to the end
    if not isinstance(label, str) or not label:
        return (999, 0)

    text = label.strip()
    # take only the first token before whitespace
    text = text.split()[0]
    parts = text.split("_")
    start_token = parts[0]
    end_token = parts[-1]

    def _map_token(token: str) -> Optional[int]:
        token = token.strip()
        if not token:
            return None
        # Delegate mapping to the shared helper so all exports
        # use the same pJan..pDec, Jan..Dec ordering.
        return _window_label_to_idx(token)

    start_idx = _map_token(start_token)
    if start_idx is None:
        return (999, 0)

    if len(parts) == 1:
        length = 1
    else:
        end_idx = _map_token(end_token)
        if end_idx is None:
            length = 1
        else:
            if end_idx < start_idx:
                end_idx += 24
            length = end_idx - start_idx + 1

    return (start_idx, length)


def _sort_window_columns_for_export(
    df: pd.DataFrame,
    year_col: str,
) -> pd.DataFrame:
    """
    Return a copy of df with window columns ordered by month and
    window length. The year column (year_col) is kept first.
    """
    if year_col not in df.columns:
        return df
    other_cols = [c for c in df.columns if c != year_col]

    def col_key(col: str) -> Tuple[int, int, str]:
        base = str(col)
        if base.endswith("_sum"):
            base = base[:-4]
        elif base.endswith("_mean"):
            base = base[:-5]
        parts = base.split("_")
        if len(parts) == 1:
            start_label = parts[0]
            end_label = parts[0]
        else:
            start_label, end_label = parts[0], parts[-1]
        si = _window_label_to_idx(start_label)
        ei = _window_label_to_idx(end_label)
        if si is None or ei is None:
            return (999, 999, base)
        start_idx = min(si, ei)
        length = max(si, ei) - start_idx + 1
        return (start_idx, length, base)

    sorted_cols = sorted(other_cols, key=col_key)
    return df[[year_col] + sorted_cols]

CLIMATE_PARAM_ALIAS_RULES: List[Tuple[str, List[str]]] = [
    (
        "Temperature (min)",
        [
            "tasmin", "tmin", "tmn", "tnn", "tn", "mintemp", "min temp", "temperature 2 m min",
            "minimum temperature", "air temperature minimum", "daily minimum temperature",
        ],
    ),
    (
        "Temperature (max)",
        [
            "tasmax", "tmax", "tmx", "txx", "tx", "maxtemp", "max temp", "temperature 2 m max",
            "maximum temperature", "air temperature maximum", "daily maximum temperature",
        ],
    ),
    (
        "Temperature (mean)",
        [
            "tas", "tmp", "temp", "temperature", "air temperature", "mean temperature",
            "average temperature", "avg temperature", "tmean", "tavg", "t2m", "2m temperature",
        ],
    ),
    (
        "Precipitation",
        [
            "precipitation", "precip", "pre", "prcp", "prc", "tp", "rain", "rainfall",
            "rr", "pcp", "ppt", "pr", "precipitation sum", "total precipitation",
        ],
    ),
    (
        "Runoff",
        [
            "runoff", "discharge", "disch", "streamflow", "riverflow", "river flow", "flow",
            "grdc", "qmonth", "q_month", "qmon", "qday", "q_day", "qdaily", "q", "ro", "dis 24",
            "qhour", "q_hour", "mean daily discharge", "mean monthly discharge",
            "mean hourly discharge",
        ],
    ),
    (
        "Soil moisture",
        [
            "soil moisture", "soilmoisture", "soil moist", "soilliq", "soil liquid", "soil",
            "soil water", "soilwater", "root zone soil moisture", "rootzone soil moisture",
            "swvl", "mrso",
        ],
    ),
    (
        "Snow",
        [
            "snow", "snowfall", "snow depth", "snow water equivalent", "swe",
            "snw", "snod", "snd",
        ],
    ),
    (
        "PDSI",
        [
            "pdsi", "scpdsi", "palmer drought severity index",
        ],
    ),
    (
        "Relative humidity",
        [
            "relative humidity", "rel humidity", "rel. humidity", "humidity",
            "relhum", "hurs", "rhs",
        ],
    ),
    (
        "Sea-level pressure",
        [
            "sea-level pressure", "sea level pressure", "slp", "mslp", "prmsl",
            "mean sea level pressure",
        ],
    ),
    (
        "Wind speed",
        [
            "wind speed", "windspeed", "wind", "wspd", "ws", "sfcwind",
        ],
    ),
    (
        "Radiation",
        [
            "radiation", "solar radiation", "shortwave", "longwave", "net radiation",
            "surface radiation", "srad", "rsds", "rsus", "rlds", "rlus", "ssrd",
        ],
    ),
    # SPI before SPEI: files of the SPEI source are named "SPEI_spi3_..."
    ("SPI", ["spi", "standardised precipitation index", "standardized precipitation index"]),
    ("SPEI", ["spei", "standardised precipitation evapotranspiration index",
              "standardized precipitation evapotranspiration index"]),
    ("Climate moisture index", ["cmi", "climate moisture index"]),
    ("Climatic water deficit", ["def", "cwd", "climatic water deficit", "water deficit"]),
    ("Vapour pressure deficit", ["vpd", "vapour pressure deficit", "vapor pressure deficit"]),
    ("Vapour pressure", ["vap", "vapour pressure", "vapor pressure"]),
    ("Actual evapotranspiration", ["aet", "actual evapotranspiration", "total evaporation"]),
    ("PET", ["pet", "pev", "et 0", "eto", "potential evapotranspiration", "reference evapotranspiration",
             "evapotranspiration", "potential evaporation"]),
    ("Wet days", ["wet", "wet days", "wet day frequency"]),
    ("Frost days", ["frs", "frost days", "frost day frequency"]),
    ("Cloud cover", ["cld", "cloud cover", "cloudiness"]),
    ("Diurnal temperature range", ["dtr", "diurnal temperature range"]),
]


def batch_metadata_for_file(file_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Metadata of a file written by 'Download everything for the area' (datasets.json / summary.csv).

    Looks in the file's folder and its parent; returns the manifest entry (name, kind, climate_param,
    value_column, source_freq, monthly_agg, meta) or None.
    """
    if not file_path:
        return None
    base = os.path.basename(file_path)
    stem = os.path.splitext(base)[0]
    folder = os.path.dirname(os.path.abspath(file_path))
    for d in (folder, os.path.dirname(folder)):
        man = os.path.join(d, "datasets.json")
        if os.path.isfile(man):
            try:
                with open(man, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for e in data.get("datasets", []) if isinstance(data, dict) else data:
                    if isinstance(e, dict) and os.path.basename(str(e.get("file", ""))) == base:
                        return e
            except Exception:
                pass
        summ = os.path.join(d, "summary.csv")
        if os.path.isfile(summ):
            try:
                sm = pd.read_csv(summ, encoding="utf-8-sig")
                for _i, r in sm.iterrows():
                    nm = str(r.get("dataset", ""))
                    safe = (re.sub(r"[^A-Za-z0-9._-]+", "_", nm).strip("_") or "dataset")[:80]
                    if safe == stem or nm == stem:
                        ct = r.get("climate_type")
                        out = {"name": nm, "kind": str(r.get("kind") or "climate")}
                        if isinstance(ct, str) and ct:
                            out["climate_param"] = ct
                        meta = {k: r.get(k) for k in ("source", "variable", "units", "lat", "lon", "citation")
                                if not pd.isna(r.get(k, float("nan")))}
                        out["meta"] = meta
                        return out
            except Exception:
                pass
    return None


def _normalize_climate_guess_text(*parts: Any) -> str:
    """Normalize text for climate-parameter guessing from names and headers."""
    raw = " ".join(str(p) for p in parts if p)
    txt = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    txt = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", txt)
    txt = re.sub(r"([A-Za-z])(\d)", r"\1 \2", txt)
    txt = re.sub(r"(\d)([A-Za-z])", r"\1 \2", txt)
    txt = txt.lower()
    txt = txt.replace("\\", " ").replace("/", " ")
    txt = re.sub(r"[_\-\.\(\)\[\],;:]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return f" {txt} "


def _climate_guess_stem_from_path(file_path: Optional[str]) -> str:
    """Return file name stem for climate guessing, removing everything after the first dot."""
    if not file_path:
        return ""
    stem = os.path.basename(file_path)
    # remove real extensions only (".csv", ".txt.gz"); keep dots inside names such as "CRU4.10_pre_40.28N"
    for _ in range(3):
        root, ext = os.path.splitext(stem)
        if ext and re.fullmatch(r"\.[A-Za-z][A-Za-z0-9]{0,4}", ext) and not re.search(r"[_\-]", ext):
            stem = root
        else:
            break
    return stem.strip()


def _guess_climate_param_from_text(*parts: Any) -> Optional[str]:
    """Guess climate parameter type from arbitrary text using alias rules."""
    text = _normalize_climate_guess_text(*parts)
    if not text.strip():
        return None

    def _has_alias(alias: str) -> bool:
        alias_norm = _normalize_climate_guess_text(alias).strip()
        if not alias_norm:
            return False
        # Match only whole normalized tokens/phrases, never arbitrary substrings.
        return f" {alias_norm} " in text

    # Hard rule for temperature:
    # - explicit min -> Temperature (min)
    # - explicit max -> Temperature (max)
    # - otherwise temperature can only become mean, never min/max
    for alias in dict(CLIMATE_PARAM_ALIAS_RULES).get("Temperature (min)", []):
        if _has_alias(alias):
            return "Temperature (min)"

    for alias in dict(CLIMATE_PARAM_ALIAS_RULES).get("Temperature (max)", []):
        if _has_alias(alias):
            return "Temperature (max)"

    # Non-temperature parameters
    for param_name, aliases in CLIMATE_PARAM_ALIAS_RULES:
        if param_name in ("Temperature (min)", "Temperature (max)", "Temperature (mean)"):
            continue
        for alias in aliases:
            if _has_alias(alias):
                return param_name

    # Generic temperature only if temperature is mentioned explicitly
    for alias in dict(CLIMATE_PARAM_ALIAS_RULES).get("Temperature (mean)", []):
        if _has_alias(alias):
            return "Temperature (mean)"

    return None


def guess_climate_param_from_metadata(file_path: Optional[str]) -> Optional[str]:
    """Try to guess climate parameter type from comment/header lines at top of a text file."""
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        lines: List[str] = []
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(120):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                lines.append(stripped)
                if not stripped.startswith("#") and len(lines) > 20:
                    break
        stem = _climate_guess_stem_from_path(file_path)
        return _guess_climate_param_from_text(" ".join(lines), stem)
    except Exception:
        return None


def guess_climate_param_from_name(path: str, var_name: str) -> Optional[str]:
    """Guess climate parameter type only from file stem and variable name."""
    stem = _climate_guess_stem_from_path(path)
    return _guess_climate_param_from_text(stem, var_name)

def guess_time_frequency(index: pd.DatetimeIndex) -> str:
    """Guess time frequency from a DatetimeIndex (rough heuristic)."""
    if not isinstance(index, pd.DatetimeIndex) or len(index) < 2:
        return "monthly"

    # use first non-zero delta
    deltas = (index[1:] - index[:-1]).to_series().dt.total_seconds()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return "monthly"

    delta_sec = float(deltas.iloc[0])
    day_sec = 24 * 3600

    # very rough thresholds
    if delta_sec <= 2 * 3600:
        return "hourly"
    if delta_sec <= 1.5 * day_sec:
        return "daily"
    # around month
    if 20 * day_sec <= delta_sec <= 40 * day_sec:
        return "monthly"
    # fallback
    return "daily"

def _to_numeric_1d(values: Any) -> np.ndarray:
    """Convert input to a 1-D float numpy array safely.

    This wrapper prevents pandas.to_numeric from raising
    'arg must be a list, tuple, 1-d array, or Series' when
    it accidentally receives a 2-D object (e.g. DataFrame).
    """
    arr = np.asarray(values)
    if arr.ndim > 1:
        arr = arr.ravel()
    return pd.to_numeric(arr, errors="coerce").astype(float)


def _guess_separator(file_path: str) -> str:
    """Guess a reasonable separator for a text file.

    If the sample contains ',' or ';' we treat it as CSV.
    Otherwise we fall back to a whitespace regex so that
    multiple spaces are treated as a single separator.
    """
    sample_lines: list[str] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                sample_lines.append(line)
                if len(sample_lines) >= 10:
                    break
    except OSError:
        # Fallback: default to whitespace
        return r"\s+"

    sample = "\n".join(sample_lines)
    if "," in sample:
        return ","
    if ";" in sample:
        return ";"
    if "\t" in sample:
        return r"\t+"
    return r"\s+"

def _read_csv_robust(
    path: str,
    sep: str,
    header: int | None,
    skiprows: int,
    comment: Optional[str],
) -> pd.DataFrame:
    """Read CSV with best-effort settings (tolerant to bad encoding/lines)."""
    # First try: latin1 (never fails on bytes)
    try:
        return pd.read_csv(
            path,
            sep=sep,
            header=header,
            skiprows=skiprows,
            comment=comment,
            engine="python",
            encoding="latin1",
            on_bad_lines="skip",
        )
    except TypeError:
        # older pandas without on_bad_lines
        return pd.read_csv(
            path,
            sep=sep,
            header=header,
            skiprows=skiprows,
            comment=comment,
            engine="python",
            encoding="latin1",
        )


_LEGACY_XLS_CACHE: Dict[Tuple[str, int, int], str] = {}


def _convert_legacy_xls_with_excel(path: str, out_path: str) -> str:
    """Convert legacy .xls to .xlsx using installed Microsoft Excel via PowerShell COM."""
    if os.name != "nt":
        raise RuntimeError("Excel COM conversion is available only on Windows.")

    in_ps = os.path.abspath(path).replace("'", "''")
    out_ps = os.path.abspath(out_path).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
$excel = $null
$workbook = $null
try {{
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $workbook = $excel.Workbooks.Open('{in_ps}')
    $workbook.SaveAs('{out_ps}', 51)
    $workbook.Close($false)
    $excel.Quit()
}}
finally {{
    if ($workbook -ne $null) {{
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($workbook)
    }}
    if ($excel -ne $null) {{
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
    }}
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}}
"""
    last_error: Optional[Exception] = None
    for exe_name in ("powershell", "pwsh"):
        exe_path = shutil.which(exe_name)
        if not exe_path:
            continue
        try:
            proc = subprocess.run(
                [exe_path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0 and os.path.isfile(out_path):
                return out_path
            stderr = (proc.stderr or proc.stdout or "").strip()
            last_error = RuntimeError(stderr or f"{exe_name} returned exit code {proc.returncode}.")
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("PowerShell was not found.")


def _convert_legacy_xls_with_libreoffice(path: str, out_dir: str) -> str:
    """Convert legacy .xls to .xlsx using LibreOffice in headless mode."""
    source_path = os.path.abspath(path)
    expected_out = os.path.join(
        out_dir,
        os.path.splitext(os.path.basename(source_path))[0] + ".xlsx",
    )
    last_error: Optional[Exception] = None
    for exe_name in ("soffice", "libreoffice"):
        exe_path = shutil.which(exe_name)
        if not exe_path:
            continue
        try:
            proc = subprocess.run(
                [exe_path, "--headless", "--convert-to", "xlsx", "--outdir", out_dir, source_path],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0 and os.path.isfile(expected_out):
                return expected_out
            stderr = (proc.stderr or proc.stdout or "").strip()
            last_error = RuntimeError(stderr or f"{exe_name} returned exit code {proc.returncode}.")
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("LibreOffice was not found.")


def _resolve_excel_source(path: str) -> Tuple[str, Optional[str]]:
    """Return a readable Excel path and pandas engine, converting .xls when possible."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return path, "openpyxl"
    if ext != ".xls":
        return path, None

    try:
        import xlrd  # noqa: F401
        return path, "xlrd"
    except Exception as xlrd_exc:
        abs_path = os.path.abspath(path)
        try:
            stat = os.stat(abs_path)
            cache_key = (abs_path, int(stat.st_mtime), int(stat.st_size))
        except Exception:
            cache_key = (abs_path, 0, 0)

        cached_path = _LEGACY_XLS_CACHE.get(cache_key)
        if cached_path and os.path.isfile(cached_path):
            return cached_path, "openpyxl"

        cache_root = os.path.join(tempfile.gettempdir(), "ringclim_xls_cache")
        os.makedirs(cache_root, exist_ok=True)

        safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(abs_path))[0]).strip("._")
        if not safe_base:
            safe_base = "workbook"
        out_name = f"{safe_base}_{abs(hash(cache_key))}.xlsx"
        out_path = os.path.join(cache_root, out_name)

        conversion_errors: List[str] = []

        if not os.path.isfile(out_path):
            try:
                _convert_legacy_xls_with_excel(abs_path, out_path)
            except Exception as exc:
                conversion_errors.append(f"Excel COM conversion failed: {exc}")

            if not os.path.isfile(out_path):
                try:
                    converted = _convert_legacy_xls_with_libreoffice(abs_path, cache_root)
                    if converted != out_path and os.path.isfile(converted):
                        try:
                            shutil.copy2(converted, out_path)
                        except Exception:
                            out_path = converted
                except Exception as exc:
                    conversion_errors.append(f"LibreOffice conversion failed: {exc}")

        if os.path.isfile(out_path):
            _LEGACY_XLS_CACHE[cache_key] = out_path
            return out_path, "openpyxl"

        details = "\n".join(conversion_errors).strip()
        if details:
            details = f"\n\nTried automatic conversion methods:\n{details}"
        raise ImportError(
            "Legacy Excel file (*.xls) could not be opened directly because 'xlrd' is not available, "
            "and automatic conversion to .xlsx failed."
            f"{details}\n\n"
            "Install xlrd >= 2.0.1, or open the file in Excel/LibreOffice and save it as .xlsx."
        ) from xlrd_exc


def _read_excel_robust(
    path: str,
    sheet_name: Any = 0,
    header: Optional[int] = 0,
    skiprows: int = 0,
):
    """Read Excel with an explicit engine so pandas does not guess incorrectly."""
    read_path, engine = _resolve_excel_source(path)
    kwargs: Dict[str, Any] = {
        "sheet_name": sheet_name,
        "header": header,
        "skiprows": skiprows,
    }
    if engine:
        kwargs["engine"] = engine
    return pd.read_excel(read_path, **kwargs)


def _get_excel_sheet_names(path: str) -> List[str]:
    """Return sheet names for an Excel workbook using the correct engine."""
    read_path, engine = _resolve_excel_source(path)
    if engine:
        with pd.ExcelFile(read_path, engine=engine) as xls:
            return list(xls.sheet_names)
    with pd.ExcelFile(read_path) as xls:
        return list(xls.sheet_names)

def _normalize_path_for_windows(path: str) -> str:
    """
    Normalize path for Windows:
    - make it absolute
    - add \\?\\ prefix for long paths
    On non-Windows platforms returns path unchanged.
    """
    if os.name != "nt":
        return path

    path = os.path.abspath(path)

    # Already normalized
    if path.startswith("\\\\?\\"):
        return path

    # UNC network path: \\server\share\...
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]

    # Regular local path: C:\...
    return "\\\\?\\" + path


def _drop_empty_columns(df: "pd.DataFrame") -> "pd.DataFrame":
    """Drop columns that are completely empty (NaN or blank strings)."""
    if df.empty:
        return df

    cols_to_keep: list[str] = []
    for col in df.columns:
        s = df[col]
        # All NaN?
        if s.isna().all():
            continue
        # For object columns, check for only blank strings and NaN
        if s.dtype == object:
            if s.map(
                lambda v: (
                    v is None
                    or (isinstance(v, str) and not v.strip())
                    or (not isinstance(v, str) and pd.isna(v))
                )
            ).all():
                continue
        cols_to_keep.append(col)

    if not cols_to_keep:
        return df.iloc[:, 0:0]

    return df[cols_to_keep]

def extract_timeseries_from_gridded_file(
    path: str,
    target_lat: float,
    target_lon: float,
    bbox: tuple[float, float, float, float],
    source_freq: str,
    target_freq: str,
    agg: str,
    use_bbox_mean: bool = False,
) -> tuple[pd.Series, dict]:
    """
    Extract a single time series from a gridded NetCDF file.

    If use_bbox_mean is False (default) - bilinear interpolation to the target
    point is used inside the bounding box.
    If use_bbox_mean is True - the mean over all grid cells inside the bounding
    box is computed.
    """
    orig_path = path

    # Heuristic: if filename does NOT contain ".nc", treat it as ASCII CRU/Tyndall grid
    base_name = os.path.basename(path).lower()
    if ".nc" not in base_name:
        raise NotImplementedError(
            "ASCII .dat.gz CRU/Tyndall grids are not supported by this function yet.\n"
            "Please use a NetCDF variant (*.dat.nc or *.nc)."
        )

    tmp_dir: Optional[str] = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="dendroclim_cru_")

        def _ascii_safe_name(fname: str) -> str:
            base = os.path.basename(fname)
            safe = "".join(
                ch if (ch.isalnum() or ch in "._-") else "_"
                for ch in base
            )
            return safe or "grid.nc"

        norm_path = _normalize_path_for_windows(path)

        if norm_path.lower().endswith(".gz"):
            inner_name = os.path.basename(norm_path)[:-3]
            safe_name = _ascii_safe_name(inner_name)
            open_path = os.path.join(tmp_dir, safe_name)
            with gzip.open(norm_path, "rb") as src, open(open_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            safe_name = _ascii_safe_name(norm_path)
            open_path = os.path.join(tmp_dir, safe_name)
            shutil.copy2(norm_path, open_path)

        open_path = _normalize_path_for_windows(open_path)

        ds = xr.open_dataset(open_path)

        # --- Detect coordinate names ---
        lat_name: Optional[str] = None
        lon_name: Optional[str] = None
        for cand in ("lat", "latitude", "y", "LAT", "Latitude"):
            if cand in ds.coords:
                lat_name = cand
                break
        for cand in ("lon", "longitude", "x", "LON", "Longitude"):
            if cand in ds.coords:
                lon_name = cand
                break
        if lat_name is None or lon_name is None:
            ds.close()
            raise ValueError(
                "Could not find latitude/longitude coordinates in the NetCDF file. "
                "Expected coords named like 'lat'/'lon' or 'latitude'/'longitude'."
            )

        # --- Detect main data variable (time, lat, lon) ---
        var_name: Optional[str] = None
        for vname, da in ds.data_vars.items():
            dims = set(da.dims)
            if {"time", lat_name, lon_name}.issubset(dims):
                var_name = vname
                break
        if var_name is None:
            ds.close()
            raise ValueError(
                "Could not find a data variable with dimensions (time, lat, lon)."
            )

        # --- Latitude / longitude handling and bounding box ---
        min_lat, max_lat, min_lon, max_lon = bbox

        lons = ds[lon_name]
        lon_values = lons.values
        lon_is_0_360 = float(np.nanmin(lon_values)) >= 0.0 and float(
            np.nanmax(lon_values)
        ) > 180.0

        def _to_0_360(lon: float) -> float:
            if lon < 0.0:
                return lon + 360.0
            return lon

        if lon_is_0_360 and (min_lon < 0.0 or max_lon < 0.0 or target_lon < 0.0):
            min_lon_eff = _to_0_360(min_lon)
            max_lon_eff = _to_0_360(max_lon)
            target_lon_eff = _to_0_360(target_lon)
        else:
            min_lon_eff = min_lon
            max_lon_eff = max_lon
            target_lon_eff = target_lon

        lat_min_eff = min(min_lat, max_lat)
        lat_max_eff = max(min_lat, max_lat)

        ds_sub = ds.sel(
            {
                lat_name: slice(lat_min_eff, lat_max_eff),
                lon_name: slice(min_lon_eff, max_lon_eff),
            }
        )

        # If bbox selection is empty, fall back to full domain
        if ds_sub[lat_name].size == 0 or ds_sub[lon_name].size == 0:
            ds_sub = ds

        da_sub = ds_sub[var_name]

        # Number of cells in the (effective) bbox
        n_lat = int(ds_sub[lat_name].size)
        n_lon = int(ds_sub[lon_name].size)
        n_cells_bbox = n_lat * n_lon

        if n_cells_bbox == 0:
            ds.close()
            raise ValueError("No grid cells found inside the bounding box.")

        # --- Either mean over bbox or interpolation to a point ---
        if use_bbox_mean:
            # area-weighted mean over all grid cells in bbox
            if lat_name not in da_sub.dims or lon_name not in da_sub.dims:
                ds.close()
                raise ValueError(
                    "Data variable does not have expected latitude/longitude dimensions."
                )
            weights = np.cos(np.deg2rad(ds_sub[lat_name]))
            da_point = da_sub.weighted(weights).mean(dim=(lat_name, lon_name))
        else:
            # bilinear interpolation at the single point
            da_point = da_sub.interp(
                {lat_name: target_lat, lon_name: target_lon_eff},
                method="linear",
            )
            da_point = da_point.squeeze(drop=True)

        if "time" not in da_point.dims and "time" not in da_point.coords:
            ds.close()
            raise ValueError("Interpolated data has no 'time' dimension.")

        ts = da_point.to_series()
        ts = ts.sort_index()
        ts.name = var_name

        if not isinstance(ts.index, pd.DatetimeIndex):
            time_coord = ds_sub["time"]
            try:
                idx = pd.to_datetime(time_coord.values)
                ts.index = idx
            except Exception:
                ds.close()
                raise ValueError(
                    "Failed to interpret 'time' coordinate as dates. "
                    "Check NetCDF time units/encoding."
                )

        # --- Temporal aggregation / resampling ---
        source_freq = (source_freq or "").lower()
        target_freq = (target_freq or "").lower()
        agg = (agg or "mean").lower()

        if source_freq != target_freq:
            if source_freq == "hourly" and target_freq == "daily":
                rule = "D"
            elif source_freq == "hourly" and target_freq == "monthly":
                rule = "M"
            elif source_freq == "daily" and target_freq == "monthly":
                rule = "M"
            elif source_freq == "monthly" and target_freq == "daily":
                ds.close()
                raise ValueError(
                    "Upsampling from monthly to daily is not implemented. "
                    "Use target frequency 'monthly' for CRU TS monthly data."
                )
            else:
                rule = {"daily": "D", "monthly": "M"}.get(target_freq, None)
                if rule is None:
                    ds.close()
                    raise ValueError(
                        f"Unsupported frequency conversion: {source_freq} -> {target_freq}"
                    )

            if agg == "sum":
                ts = ts.resample(rule).sum()
            else:
                ts = ts.resample(rule).mean()

        # --- Metadata, including interpolation details ---
        if use_bbox_mean:
            interp_method = "bbox_mean"
            n_cells_used = n_cells_bbox
        else:
            interp_method = "bilinear_point"
            n_cells_used = min(4, n_cells_bbox)

        meta: Dict[str, Any] = {
            "var_name": var_name,
            "lat_target": float(target_lat),
            "lon_target": float(target_lon),
            "lat_coord_name": lat_name,
            "lon_coord_name": lon_name,
            "time_start": ts.index.min().isoformat() if len(ts) > 0 else None,
            "time_end": ts.index.max().isoformat() if len(ts) > 0 else None,
            "source_freq": source_freq,
            "target_freq": target_freq,
            "agg": agg,
            "bbox": (float(min_lat), float(max_lat), float(min_lon), float(max_lon)),
            "n_cells_bbox": int(n_cells_bbox),
            "interp_method": interp_method,
            "n_cells_used": int(n_cells_used),
            "use_bbox_mean": bool(use_bbox_mean),
            # keep original human-readable path (with spaces, non-ASCII)
            "source_path": orig_path,
        }

        ds.close()
        return ts, meta

    finally:
        if tmp_dir is not None:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                # Not critical; ignore cleanup errors.
                pass


class LargeGridWorker(QObject):
    """
    Worker object to process a large gridded file in a separate thread.

    It calls extract_timeseries_from_gridded_file and converts the result
    to a Year + 12 months wide table that fits the rest of the app.
    """

    finished = Signal(pd.DataFrame, dict)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        path: str,
        target_lat: float,
        target_lon: float,
        bbox: tuple[float, float, float, float],
        source_freq: str,
        target_freq: str,
        agg: str,
        climate_param: str,
        year_start: Optional[int] = None,
        year_end: Optional[int] = None,
        use_bbox_mean: bool = False,
    ) -> None:
        super().__init__()
        self.path = path
        self.target_lat = target_lat
        self.target_lon = target_lon
        self.bbox = bbox
        self.source_freq = source_freq
        self.target_freq = target_freq
        self.agg = agg
        self.climate_param = climate_param
        self.year_start = year_start
        self.year_end = year_end
        self.use_bbox_mean = use_bbox_mean

    def run(self) -> None:
        """Main worker entry point, executed inside QThread."""
        try:
            self.progress.emit("Starting extraction...")
            series, meta = extract_timeseries_from_gridded_file(
                path=self.path,
                target_lat=self.target_lat,
                target_lon=self.target_lon,
                bbox=self.bbox,
                source_freq=self.source_freq,
                target_freq=self.target_freq,
                agg=self.agg,
                use_bbox_mean=self.use_bbox_mean,
            )
            self.progress.emit("Extraction finished, building Year × Month table...")

            if series.empty:
                raise ValueError("Extracted time series is empty.")

            if isinstance(series.index, pd.DatetimeIndex):
                if self.year_start is not None:
                    series = series[series.index.year >= self.year_start]
                if self.year_end is not None:
                    series = series[series.index.year <= self.year_end]

            if series.empty:
                raise ValueError("Extracted time series is empty after year filter.")

            if not isinstance(series.index, pd.DatetimeIndex):
                raise ValueError("Time series index must be a DatetimeIndex.")

            df = series.to_frame(name="Value").copy()
            df["Year"] = df.index.year.astype(int)
            df["Month"] = df.index.month.astype(int)

            pivot = df.pivot_table(
                index="Year",
                columns="Month",
                values="Value",
                aggfunc="mean",
            )

            pivot = pivot.reindex(columns=list(range(1, 13)))
            pivot.columns = MONTH_NAMES
            pivot = pivot.reset_index()
            pivot = pivot.sort_values("Year")

            meta_out = dict(meta) if meta is not None else {}
            meta_out.setdefault("source_path", self.path)
            meta_out.setdefault("climate_param", self.climate_param)
            if isinstance(series.index, pd.DatetimeIndex):
                meta_out["year_start_used"] = int(series.index.year.min())
                meta_out["year_end_used"] = int(series.index.year.max())

            self.progress.emit(
                f"Built pivot table with {len(pivot)} years of data."
            )
            self.finished.emit(pivot, meta_out)
        except NotImplementedError as exc:
            self.failed.emit(
                f"Large-grid extraction is not implemented yet:\n{exc}"
            )
        except Exception as exc:
            self.failed.emit(f"Error while processing large grid file:\n{exc}")


class LargeGridPreviewWorker(QObject):
    """
    Lightweight worker to run preview extraction for large gridded files
    in a background thread so that the GUI does not freeze.
    """

    finished = Signal(pd.Series, dict)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        path: str,
        target_lat: float,
        target_lon: float,
        bbox: tuple[float, float, float, float],
        source_freq: str,
        target_freq: str,
        agg: str,
        use_bbox_mean: bool = False,
    ) -> None:
        super().__init__()
        self.path = path
        self.target_lat = target_lat
        self.target_lon = target_lon
        self.bbox = bbox
        self.source_freq = source_freq
        self.target_freq = target_freq
        self.agg = agg
        self.use_bbox_mean = use_bbox_mean

    def run(self) -> None:
        try:
            self.progress.emit("Starting preview extraction...")
            series, meta = extract_timeseries_from_gridded_file(
                path=self.path,
                target_lat=self.target_lat,
                target_lon=self.target_lon,
                bbox=self.bbox,
                source_freq=self.source_freq,
                target_freq=self.target_freq,
                agg=self.agg,
                use_bbox_mean=self.use_bbox_mean,
            )
            self.finished.emit(series, meta)
        except NotImplementedError as exc:
            self.failed.emit(
                f"Large-grid preview is not implemented yet:\n{exc}"
            )
        except Exception as exc:
            self.failed.emit(f"Error while previewing large grid file:\n{exc}")

# ---------------------------
# Pandas -> QTableView model
# ---------------------------
class PandasModel(QAbstractTableModel):
    def __init__(self, df: pd.DataFrame, parent=None):
        super().__init__(parent)
        self._df = df.fillna("")

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._df.columns)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            value = self._df.iloc[index.row(), index.column()]
            if pd.isna(value):
                return ""
            if isinstance(value, float):
                return f"{value:.6g}"
            return str(value)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return str(self._df.columns[section])
        else:
            return str(self._df.index[section])


# ---------------------------
# Matplotlib canvas
# ---------------------------
# ---------------------------
# Modern look, clipboard support and help dialogs
# ---------------------------
RINGCLIM_STYLESHEET = """
QMainWindow, QDialog { background: #f3f5f9; }
QWidget { color: #1f2937; }
QLabel { background: transparent; }
QPushButton {
    background: #ffffff;
    border: 1px solid #c9d2e0;
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 18px;
}
QPushButton:hover { background: #e8f0fe; border-color: #2563eb; }
QPushButton:pressed { background: #dbe6fb; border-color: #1d4ed8; }
QPushButton:focus { border-color: #2563eb; }
QPushButton:disabled { color: #9ca3af; background: #eef1f5; border-color: #dde3ec; }
QPushButton[primary="true"] {
    background: #16a34a;
    color: #ffffff;
    border: 1px solid #15803d;
    font-weight: 700;
    padding: 6px 18px;
}
QPushButton[primary="true"]:hover { background: #15803d; }
QPushButton[primary="true"]:pressed { background: #166534; }
QPushButton[primary="true"]:disabled { background: #a7d8b5; border-color: #a7d8b5; }
QLineEdit {
    background: #ffffff;
    border: 1px solid #c9d2e0;
    border-radius: 5px;
    padding: 3px 6px;
    min-height: 18px;
}
QLineEdit:hover, QLineEdit:focus { border-color: #2563eb; }
QLineEdit:disabled { background: #eef1f5; color: #9ca3af; }
QComboBox, QSpinBox, QDoubleSpinBox { min-height: 22px; }
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #c9d2e0;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QGroupBox {
    background: #fafbfd;
    border: 1px solid #d5dce8;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #1e40af;
}
QTabWidget::pane {
    border: 1px solid #d5dce8;
    border-radius: 6px;
    top: -1px;
    background: #ffffff;
}
QTabBar::tab {
    background: #e5e9f0;
    color: #4b5563;
    padding: 7px 18px;
    border: 1px solid #d5dce8;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #ffffff; color: #1d4ed8; }
QTabBar::tab:hover:!selected { background: #eef2f8; color: #1f2937; }
QListWidget, QTreeWidget, QTableView, QTextEdit, QPlainTextEdit, QTextBrowser {
    background: #ffffff;
    border: 1px solid #d5dce8;
    border-radius: 6px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QTableView { gridline-color: #e5e9f0; alternate-background-color: #f8fafc; }
QHeaderView::section {
    background: #eef2f7;
    color: #1f2937;
    border: none;
    border-right: 1px solid #d5dce8;
    border-bottom: 1px solid #d5dce8;
    padding: 4px 6px;
}
QStatusBar { background: #e9edf4; border-top: 1px solid #d5dce8; }
QProgressBar {
    background: #ffffff;
    border: 1px solid #c9d2e0;
    border-radius: 5px;
    text-align: center;
}
QProgressBar::chunk { background: #2563eb; border-radius: 4px; }
QMenu { background: #ffffff; border: 1px solid #c9d2e0; padding: 4px; }
QMenu::item { padding: 5px 24px 5px 18px; border-radius: 4px; }
QMenu::item:selected { background: #2563eb; color: #ffffff; }
QMenu::item:disabled { color: #9ca3af; }
QToolTip {
    background: #1f2937;
    color: #ffffff;
    border: none;
    padding: 5px 7px;
}
QScrollArea { border: none; background: transparent; }
"""




def _app_icon_search_dirs() -> List[str]:
    """Return folders where the application icon files may be located."""
    dirs: List[str] = []
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


def _find_app_icon_files(app_name: str) -> Tuple[Optional[str], Optional[str]]:
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


def _apply_app_icon(app: QApplication) -> None:
    """Use the application icon for all windows, the taskbar and dialogs."""
    ico_path, png_path = _find_app_icon_files("ringclim")
    icon_path = (ico_path if os.name == "nt" else None) or png_path or ico_path
    if not icon_path:
        return
    try:
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(icon_path))
    except Exception:
        pass


def _apply_modern_style(app: QApplication) -> None:
    """Apply the Fusion style, a light palette and the application stylesheet."""
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    try:
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor("#f3f5f9"))
        pal.setColor(QPalette.WindowText, QColor("#1f2937"))
        pal.setColor(QPalette.Base, QColor("#ffffff"))
        pal.setColor(QPalette.AlternateBase, QColor("#f8fafc"))
        pal.setColor(QPalette.Text, QColor("#1f2937"))
        pal.setColor(QPalette.Button, QColor("#ffffff"))
        pal.setColor(QPalette.ButtonText, QColor("#1f2937"))
        pal.setColor(QPalette.Highlight, QColor("#2563eb"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ToolTipBase, QColor("#1f2937"))
        pal.setColor(QPalette.ToolTipText, QColor("#ffffff"))
        pal.setColor(QPalette.Link, QColor("#2563eb"))
        app.setPalette(pal)
    except Exception:
        pass
    try:
        app.setStyleSheet(RINGCLIM_STYLESHEET)
    except Exception:
        pass
    try:
        clipboard_filter = _ItemViewClipboardFilter(app)
        app.installEventFilter(clipboard_filter)
        app._ringclim_clipboard_filter = clipboard_filter
    except Exception:
        pass


def _copy_item_view_selection(
    view: QAbstractItemView,
    with_headers: bool = False,
    whole_view: bool = False,
) -> None:
    """Copy selected cells (or the whole view) of a table/list/tree as tab-separated text."""
    model = view.model()
    if model is None:
        return
    if whole_view:
        indexes = [
            model.index(r, c)
            for r in range(model.rowCount())
            for c in range(model.columnCount())
        ]
    else:
        sel = view.selectionModel()
        indexes = list(sel.selectedIndexes()) if sel is not None else []
        if not indexes:
            cur = view.currentIndex()
            indexes = [cur] if cur.isValid() else []
    if not indexes:
        return

    def _cell_text(idx: QModelIndex) -> str:
        value = idx.data(Qt.DisplayRole)
        return "" if value is None else str(value)

    lines: List[str] = []
    if isinstance(view, QTableView):
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes})
        lookup = {(i.row(), i.column()): i for i in indexes}
        if with_headers:
            lines.append(
                "\t".join(
                    str(model.headerData(c, Qt.Horizontal, Qt.DisplayRole) or "")
                    for c in cols
                )
            )
        for r in rows:
            lines.append(
                "\t".join(
                    _cell_text(lookup[(r, c)]) if (r, c) in lookup else ""
                    for c in cols
                )
            )
    else:
        indexes = sorted(indexes, key=lambda i: (view.visualRect(i).top(), i.column()))
        lines = [_cell_text(i) for i in indexes]
    QGuiApplication.clipboard().setText("\n".join(lines))


class _ItemViewClipboardFilter(QObject):
    """Application-wide Ctrl+C / Ctrl+A / context menu support for lists, tables and trees."""

    @staticmethod
    def _is_copy_key(event) -> bool:
        if event.matches(QKeySequence.Copy):
            return True
        ctrl = bool(event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))
        if not ctrl:
            return False
        if event.key() == Qt.Key_C:
            return True
        try:
            return sys.platform.startswith("win") and int(event.nativeVirtualKey()) == 0x43
        except Exception:
            return False

    @staticmethod
    def _is_select_all_key(event) -> bool:
        if event.matches(QKeySequence.SelectAll):
            return True
        ctrl = bool(event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))
        if not ctrl:
            return False
        try:
            return sys.platform.startswith("win") and int(event.nativeVirtualKey()) == 0x41
        except Exception:
            return False

    def eventFilter(self, obj, event):
        try:
            etype = event.type()
        except Exception:
            return False
        if etype == QEvent.KeyPress and isinstance(obj, QAbstractItemView):
            if self._is_copy_key(event):
                _copy_item_view_selection(
                    obj,
                    with_headers=bool(event.modifiers() & Qt.ShiftModifier),
                )
                return True
            if self._is_select_all_key(event):
                obj.selectAll()
                return True
            return False
        if etype == QEvent.ContextMenu:
            view = obj.parent() if isinstance(obj, QWidget) else None
            if isinstance(view, QAbstractItemView) and obj is view.viewport():
                # Tables with a custom context menu own this event (for example,
                # the download table's retry/pause menu). Let Qt emit their
                # customContextMenuRequested signal instead of showing Copy.
                if (view.contextMenuPolicy() == Qt.CustomContextMenu
                        or obj.contextMenuPolicy() == Qt.CustomContextMenu):
                    return False
                menu = QMenu(view)
                act_copy = menu.addAction("Copy")
                act_copy.setShortcut(QKeySequence.Copy)
                act_copy_hdr = None
                if isinstance(view, QTableView):
                    act_copy_hdr = menu.addAction("Copy with headers")
                    act_copy_hdr.setShortcut(QKeySequence("Ctrl+Shift+C"))
                act_copy_all = menu.addAction("Copy whole table" if isinstance(view, QTableView) else "Copy all")
                menu.addSeparator()
                act_select_all = menu.addAction("Select all")
                act_select_all.setShortcut(QKeySequence.SelectAll)
                chosen = menu.exec(event.globalPos())
                if chosen is act_copy:
                    _copy_item_view_selection(view)
                elif act_copy_hdr is not None and chosen is act_copy_hdr:
                    _copy_item_view_selection(view, with_headers=True)
                elif chosen is act_copy_all:
                    _copy_item_view_selection(view, with_headers=True, whole_view=True)
                elif chosen is act_select_all:
                    view.selectAll()
                return True
        return False


HELP_CSS = """
<style>
body { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; color: #1f2937; }
h1 { color: #1d4ed8; font-size: 20pt; margin-bottom: 2px; }
h2 { color: #1e3a8a; font-size: 14pt; margin-top: 16px; margin-bottom: 4px; }
h3 { color: #374151; font-size: 11pt; margin-top: 10px; margin-bottom: 2px; }
p { margin-top: 3px; margin-bottom: 3px; }
li { margin-bottom: 2px; }
code { background-color: #eef1f6; color: #0f172a; font-family: Consolas, 'Courier New', monospace; }
.kbd { background-color: #eef1f6; font-family: Consolas, 'Courier New', monospace; font-weight: 600; }
.note { background-color: #eef4ff; }
table.grid { border-collapse: collapse; }
table.grid td, table.grid th { border: 1px solid #d5dce8; padding: 3px 8px; }
table.grid th { background-color: #eef2f7; }
a { color: #2563eb; text-decoration: none; }
</style>
"""


def _show_html_help_dialog(
    parent: Optional[QWidget],
    title: str,
    body_html: str,
    width: int = 900,
    height: int = 680,
    anchor: str = "",
) -> None:
    """Show a modern, searchable, copyable HTML help dialog."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(width, height)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(10, 10, 10, 10)
    lay.setSpacing(8)

    browser = QTextBrowser()
    browser.setOpenExternalLinks(False)
    browser.setOpenLinks(True)
    browser.setHtml(HELP_CSS + body_html)
    lay.addWidget(browser, 1)

    row = QHBoxLayout()
    row.addWidget(QLabel("Find:"))
    edit_find = QLineEdit()
    edit_find.setPlaceholderText("Type a word and press Enter")
    edit_find.setClearButtonEnabled(True)
    row.addWidget(edit_find, 1)
    btn_find = QPushButton("Find next")
    btn_copy = QPushButton("Copy all")
    btn_close = QPushButton("Close")
    row.addWidget(btn_find)
    row.addSpacing(12)
    row.addWidget(btn_copy)
    row.addWidget(btn_close)
    lay.addLayout(row)

    def _find_next() -> None:
        needle = edit_find.text().strip()
        if not needle:
            return
        if not browser.find(needle):
            cursor = browser.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            browser.setTextCursor(cursor)
            browser.find(needle)

    edit_find.returnPressed.connect(_find_next)
    btn_find.clicked.connect(_find_next)
    btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(browser.toPlainText()))
    btn_close.clicked.connect(dlg.accept)
    btn_find.setToolTip("Jump to the next match of the search text.")
    btn_copy.setToolTip("Copy the whole help text to the clipboard.")
    if anchor:
        QTimer.singleShot(0, lambda: browser.scrollToAnchor(anchor))
    dlg.exec()


RINGCLIM_HELP_HTML = """
<h1>Ring-Climate Analyzer (RingClim) &ndash; Help</h1>
<p>Correlates tree-ring chronologies with monthly or daily climate data (or with other chronologies),
shows ranked results with plots and exports tables, windows, merged data and reports.</p>
<p class="note">&nbsp;Tip: use the <b>Find</b> box below. Every table, list and text field supports
<span class="kbd">Ctrl+C</span>, <span class="kbd">Ctrl+A</span> and a right-click menu.
<span class="kbd">F1</span> opens this help.&nbsp;</p>
<p><b>Contents:</b> <a href="#quick">Quick start</a> &middot; <a href="#data">Data tab</a> &middot;
<a href="#import">Import dialog</a> &middot; <a href="#grid">Large gridded data</a> &middot;
<a href="#prefill">Import from Detrend Advisor</a> &middot; <a href="#analysis">Analysis tab</a> &middot;
<a href="#results">Results tab</a> &middot; <a href="#report">Report tab</a> &middot;
<a href="#windows">Window names</a> &middot; <a href="#keys">Keyboard and mouse</a> &middot;
<a href="#status">Status bar and settings</a></p>

<a name="quick"></a><h2>1. Quick start</h2>
<ol>
<li><b>Data</b> tab: import chronologies and climate files (<i>Add / Import File</i> or <i>Add multiple files</i>).</li>
<li><b>Analysis</b> tab: select chronologies (A) and climate datasets (right list), set years and windows.</li>
<li>Press <b>Run analysis</b>. Results open automatically in the <b>Results</b> tab.</li>
<li>Click a result row to see its plots; use <b>Export</b> in Results or <b>Export report</b> in Report.</li>
</ol>

<a name="data"></a><h2>2. Data tab</h2>
<table class="grid">
<tr><th>Button</th><th>What it does</th></tr>
<tr><td>Add / Import File</td><td>Import one file (CSV, TXT, PRN, XLS, XLSX). Excel files with several sheets let you choose sheets.
Special formats are detected automatically (GRDC monthly and daily/hourly discharge, combined station tables).</td></tr>
<tr><td>Add multiple files</td><td>Batch import. After the first file you can let the program reuse the detected layout for identical files;
files that cannot be recognised confidently can be reviewed manually or skipped. A summary lists manual, automatic,
empty and skipped files.</td></tr>
<tr><td>Large gridded data...</td><td>Extract a point or area time series from a NetCDF grid (see section 4).</td></tr>
<tr><td>Remove Selected</td><td>Remove the selected datasets from the project (multi-selection allowed).</td></tr>
<tr><td>Edit Selected</td><td>Reopen the import dialog for the selected dataset to change layout, columns, names or climate type.</td></tr>
<tr><td>Save project</td><td>Save all datasets and their metadata to a JSON project file.</td></tr>
<tr><td>Load project</td><td>Open a project. If datasets are already loaded you choose <i>Open new (replace)</i> or <i>Merge into current</i>.</td></tr>
<tr><td>Add project</td><td>Merge another project into the current session (duplicate names get a number).</td></tr>
</table>
<ul>
<li><b>Loaded datasets</b> list: shows name, type and period. <b>Double-click</b> an entry to preview its table
(daily/hourly series are shown as monthly or daily wide tables depending on the Resolution in the Analysis tab).</li>
<li>Dataset kinds: <i>chronology</i> (Year + series), <i>climate</i> (Year + 12 months), <i>timeseries</i> (DateTime + value, daily/hourly).</li>
</ul>

<a name="import"></a><h2>3. Import dialog (File Import &ndash; Column mapping)</h2>
<p>The current file is shown at the top; the left table previews the first 100 rows with the current settings.
After opening, the layout is detected automatically and you are asked whether a manual review is needed.</p>
<ul>
<li><b>Data layout</b>: <i>Year + 12 month columns</i> (wide), <i>Year + Month + Value</i> (stacked),
<i>Year + multiple series columns</i> (chronologies), <i>Date + Value (monthly)</i>.</li>
<li><b>Climate variable type</b>: temperature (mean/min/max), precipitation, runoff, soil moisture, snow, PDSI,
humidity, pressure, wind, radiation, other. Guessed from file name, headers and comments; defines the default
aggregation (sum for precipitation/runoff/snow/radiation, mean for the others).</li>
<li><b>Select Year column</b>, <b>12 month columns</b> (wide), <b>Month + Value</b> (stacked), <b>Date + Value</b> (date-based).</li>
<li><b>Multiple series</b>: select value columns, or tick <i>Import only a single selected series</i>.</li>
<li><b>Decimal separator</b>: Auto, Dot or Comma (for values like 1,23).</li>
<li><b>File has header row</b>, <b>Skip top rows</b> (metadata), <b>Treat lines starting with '#' as comments</b>.</li>
<li><b>Edit / Filter Columns</b>: keep/drop columns (Select all, Clear selection), rename a column
(Apply rename to selected) and <i>Apply quick suggestions</i> (Year, Chronology, Jan..Dec).</li>
<li><b>Auto-detect layout &amp; suggestions</b>: prefill layout and columns for common formats.</li>
<li><b>Auto-detect and apply column names</b>: detect and rename in one step.</li>
<li>Month headers accepted: Jan..Dec, Russian short names, Roman numerals I..XII, Y1..Y12, 1..12.
Values such as -999 are treated as missing.</li>
</ul>

<a name="grid"></a><h2>4. Large gridded data (NetCDF)</h2>
<ul>
<li><b>File / Browse...</b>: *.nc, *.nc.gz, *.dat.nc (e.g. CRU TS). Requires <code>xarray</code> and <code>netCDF4</code>.</li>
<li><b>Target lat / lon</b>: point of interest. Without a manual box the value is bilinearly interpolated from the
nearest cells (automatic box of &plusmn;10&deg;).</li>
<li><b>Specify bounding box manually</b>: Min/Max lat/lon; the result is the latitude-weighted mean of all cells in the box.</li>
<li><b>Source / Target frequency</b> and <b>Aggregation</b> (mean or sum) for resampling (hourly/daily to monthly).</li>
<li><b>Climate parameter type</b> and <b>Use only years</b> (limit the period; <i>Available period</i> is shown after preview).</li>
<li><b>Preview / auto-detect</b>: read metadata, detect frequency, variable type and period, show the first rows.</li>
<li><b>Run extraction</b> (runs in the background), <b>Export...</b> (Excel with a <i>meta</i> sheet or CSV + meta.txt),
<b>Add to project</b>, <b>Close</b>. The log shows interpolation method and number of cells used.</li>
</ul>

<a name="prefill"></a><h2>5. Import from Detrend Advisor</h2>
<p>When RingClim is opened from Detrend Advisor (<code>--prefill manifest.json</code>) you can <i>Include all</i> datasets or
<i>Select...</i> them. Selection buttons: <b>Select all</b>, <b>Chronologies only</b>, <b>STD only</b>, <b>RES only</b>,
<b>POWT only</b>, <b>Exclude POWT</b>, <b>Top 3 recommended (per dataset)</b>, <b>Select none</b>.
Items are grouped by dataset and method.</p>

<a name="analysis"></a><h2>6. Analysis tab</h2>
<h3>Top row</h3>
<ul>
<li><b>Correlation target</b>: <i>Chronology &times; Climate</i> or <i>Chronology &times; Chronology</i> (full-period series correlation).</li>
<li><b>Resolution</b>: <i>Monthly</i> (monthly windows, previous and current year) or <i>Daily</i>
(same-year rolling day windows from daily/hourly datasets).</li>
<li><b>Select all</b> selects the right-hand list; <b>Clear selection</b> clears both lists.</li>
<li><b>Minimum years per correlation (n)</b>: windows with fewer common years are skipped (default 15).</li>
<li><b>Preview selected dataset</b>: preview exactly one selected dataset.</li>
<li><b>Run analysis</b>: compute all correlations.</li>
</ul>
<h3>Data selection</h3>
<ul>
<li><b>Chronologies (A)</b> and <b>Climate / daily-hourly datasets</b> (or <b>Chronologies (B)</b>). Double-click to preview.</li>
<li><b>Daily/hourly &rarr; monthly</b> (or <b>Hourly &rarr; daily base agg</b> in Daily mode): Auto, sum or mean;
<b>Preview selected ...</b> shows the table that will actually be used.</li>
<li>The label below lists the period of each selected dataset and, if enabled, the common period.</li>
</ul>
<h3>Analysis parameters</h3>
<ul>
<li><b>Year start / Year end</b>; <b>Use common period for selected datasets</b> sets the overlapping years
(if there is no overlap you may keep all datasets or exclude the conflicting ones).</li>
<li><b>Min / Max window length</b> (months, 1&ndash;24; days in Daily mode).</li>
<li><b>Aggregation</b>: <i>Auto (by variable)</i>, <i>sum</i>, <i>mean</i>, <i>sum/2</i> (default aggregation &times; 0.5), <i>both</i>.</li>
<li><b>Show only top N correlations</b> + mode <i>Overall</i> or <i>Per pair</i> (0 = show all).</li>
<li><b>Report: top K rows per pair</b> (0 = 10).</li>
<li><b>Exclude single-month windows</b>.</li>
<li><b>Multi-year period analysis</b>: <i>Minimal period span</i>, <i>Step</i>, <i>Minimal data points</i> and mode:
<i>Anchored</i> (trim start or end by the step), <i>Sliding fixed length</i>, <i>Exhaustive grid</i> (slow, max 5000 intervals).</li>
<li><b>Correlation method</b>: Pearson or Spearman (p-values need <code>scipy</code>; significance at p &lt; 0.05).</li>
<li><b>Missing values (climate)</b>: <i>Ignore</i> (use available months), <i>Strict</i> (window with a gap = missing),
<i>Fill with monthly mean</i>, <i>Local window mean (&plusmn;K years)</i>, <i>Linear interpolation (gap &le; N years)</i>.</li>
<li><b>Season filter</b>: All months, Apr&ndash;Sep, Oct&ndash;Mar or Custom (start/end month); only windows fully inside are kept.</li>
</ul>

<a name="results"></a><h2>7. Results tab</h2>
<ul>
<li><b>Show</b>: <i>Main correlations</i> or <i>Period correlations</i>.</li>
<li>Columns: Chronology, Chron_Column, Climate, (Target_Column), Window, Agg, R, p, N, Significant, Annot
(periods add PeriodStart, PeriodEnd, PeriodMode).</li>
<li>Click a row: upper plot = strongest windows of this pair (red = significant, blue = not significant);
lower plot = heatmap of R by window start and length, or a series overlay for chronology &times; chronology.</li>
<li><b>Export</b>: <i>Export results</i> (Excel with sheets All / Top filtered / Periods, or CSV files),
<i>Export climate windows...</i> (one or all climate datasets), <i>Export merged data for selected result</i>
(chronology + windows), <i>Save plot PNG</i> (300 dpi).</li>
</ul>

<a name="report"></a><h2>8. Report tab</h2>
<ul>
<li>Top K correlations for every chronology &times; target pair. <b>Export report</b> saves it as Excel or CSV.</li>
</ul>

<a name="spatial"></a><h2>8a. Spatial correlation</h2>
<p><b>Spatial correlation...</b> (Data or Results tab) opens a page that correlates a chronology with a climate window
in every grid cell of a region (CRU TS, GPCC, TerraClimate, scPDSI, SPEIbase, ERA5/ERA5-Land with your key, or a local
NetCDF file), like the KNMI Climate Explorer. Its <i>Results summary</i> systematises the last correlation table
(strongest chronology, variable types, recurring months, top pairs) and <i>Suggested maps</i> turns the strongest
windows into ready settings: double-click a suggestion to draw the map with the site marked. Click a cell to compare
its series with the chronology. Chronologies without coordinates: type them or pick the site on the map.</p>
<p>Online data and Spatial correlation open as tabs of this window; <b>Detach to window</b> moves a page to its own
window (e.g. to a second monitor), closing that window puts it back as a tab.</p>
<p><b>Screen all windows for this region</b> correlates the chronology with every window of months (1-12 or 1-24
months) of every ticked variable (e.g. CRU TS tmp, tmn, tmx, pre, pet, vap) and ranks them by the significant area
(same sign as at the site) that is connected with the site - a coherent regional signal, as recommended for the KNMI
Climate Explorer. Significance can account for serial correlation (effective n). <i>Shuffles</i> repeat the whole
screening with shuffled chronology years: <i>p (shuffled)</i> below 0.05 means such an area is unlikely by chance.
Double-click a row to draw its map.</p>
<a name="moving"></a><h2>8b. Moving correlation (temporal stability)</h2>
<p><b>Moving correlation...</b> (Data or Results tab) correlates the ticked chronologies with one climate season in a
window of <i>w</i> years (default 25) moved one year at a time; each r is drawn at the centre year of its window, with
dashed (p &lt; 0.01) and dotted (p &lt; 0.05) significance lines, one panel per data set or per column (e.g.
TRW/EWBI/LWBI/DBI or STD/RES). <i>Suggest from results</i> picks the strongest season of the analysis. The table
<i>Stability statistics</i> gives the share of significant windows, min/max, the r of both halves of the period with
Fisher's z test, the year from which the correlation changes most and a verdict (stable / weakening / strengthening /
unstable). Optional bootstrap 95% bands; the linear trend of the climate is removed by default.</p>
<p><b>Screen all seasons x all climate data</b> (Moving correlation page) computes the running correlations of every
ticked chronology with every monthly climate dataset and every window of months (1-12 or 1-24) and lists the most
stable signals per chronology: share of running windows with p &lt; 0.05 (same sign), mean, min and max r, the r of
both halves and a verdict. <i>Shuffles</i> repeat the whole screening with shuffled chronology years; <i>p
(shuffled)</i> below 0.05 means the stability is unlikely to be chance among so many combinations. Double-click a row
or use <i>Plot the top rows</i>. The screenings are saved with the project.</p>
<a name="windows"></a><h2>9. Window names</h2>
<ul>
<li><code>pJan..pDec</code> = months of the previous year, <code>Jan..Dec</code> = current year.</li>
<li><code>pMay_Aug</code> = May of previous year to August of current year; suffix <code>_sum</code>/<code>_mean</code> in exports.</li>
<li>Daily mode: <code>Jan01..Dec31</code> (+ <code>Feb29</code>), e.g. <code>May01_May14</code>.</li>
</ul>

<a name="keys"></a><h2>10. Keyboard and mouse</h2>
<table class="grid">
<tr><th>Keys</th><th>Action</th></tr>
<tr><td><span class="kbd">Ctrl+C</span></td><td>Copy selected cells/rows (tab-separated, paste into Excel)</td></tr>
<tr><td><span class="kbd">Ctrl+Shift+C</span></td><td>Copy selected table cells with column headers</td></tr>
<tr><td><span class="kbd">Ctrl+A</span></td><td>Select all</td></tr>
<tr><td>Right click</td><td>Copy, Copy with headers, Copy whole table, Select all</td></tr>
<tr><td><span class="kbd">F1</span></td><td>Open this help</td></tr>
</table>

<a name="status"></a><h2>11. Status bar and settings</h2>
<ul>
<li><b>UI scale</b>: enlarge or shrink the interface. <b>Cite</b>, <b>Help</b> (F1), <b>About</b>.</li>
<li>Settings (window size, folders, analysis options, UI scale) are saved in <code>~/.ringclim_settings.json</code>.</li>
<li>Import problems of the EXE build are written to <code>ringclim_import_debug.log</code> in the temp folder.</li>
</ul>
"""


IMPORT_HELP_HTML = """
<h1>Import help</h1>
<p>This dialog defines how a file becomes a chronology, a monthly climate dataset or a time series.</p>
<h2>Recommended steps</h2>
<ol>
<li>Check the preview. If it is wrong, first adjust <b>File has header row</b>, <b>Skip top rows</b> and
<b>Treat lines starting with '#' as comments</b>.</li>
<li>Press <b>Auto-detect layout &amp; suggestions</b> (or <b>Auto-detect and apply column names</b>).</li>
<li>Check the <b>Data layout</b> and the selected columns; fix them manually if needed.</li>
<li>For climate data choose the <b>Climate variable type</b>; it defines the default aggregation.</li>
<li>Press <b>OK</b>. If the mapping is invalid the dialog reopens for the same file.</li>
</ol>
<h2>Controls</h2>
<ul>
<li><b>Year + 12 month columns (wide)</b>: select the Year column and exactly 12 month columns in order.</li>
<li><b>Year + Month + Value (stacked)</b>: Month may be 1..12, Jan..Dec or Russian short names.</li>
<li><b>Year + multiple series columns</b>: select value columns (Ctrl+click) or import a single series.</li>
<li><b>Date + Value (monthly)</b>: YYYY-MM or YYYY-MM-DD; values are averaged per month.</li>
<li><b>Decimal separator</b>: Auto, Dot (.) or Comma (,).</li>
<li><b>Edit / Filter Columns</b>: keep/drop and rename columns; <i>Apply quick suggestions</i> proposes Year/Jan..Dec names.</li>
</ul>
<h2>Automatically recognised formats</h2>
<ul>
<li><b>Chronology</b>: Year + one or more series.</li>
<li><b>Monthly climate</b>: Year + 12 months, also with station metadata columns before Year.</li>
<li><b>Combined station table</b> (Country / Code_WMO / Name / year / jan..dec): one dataset per station,
named <code>Name (Code_Country)</code>.</li>
<li><b>GRDC</b> mean monthly discharge and daily/hourly discharge; empty GRDC files are skipped.</li>
<li>Missing value codes such as -999 are treated as missing.</li>
</ul>
<h2>Batch import</h2>
<ul>
<li>If you accept the detected settings without review, identical files are imported automatically.</li>
<li>Files that do not match confidently can be reviewed manually or skipped.</li>
</ul>
"""


class MplCanvas(FigureCanvas):
    def __init__(self, parent: Optional[QWidget] = None):
        fig = Figure(figsize=(6, 4))
        self.fig = fig
        self.figure = fig
        self.ax = fig.add_subplot(111)
        super().__init__(fig)
        self.setParent(parent)


# ---------------------------
# DataSet container
# ---------------------------
class DataSet:
    def __init__(self, name: str, path: str, df: pd.DataFrame, kind: str):
        """
        kind: "chronology" or "climate"
        """
        self.name = name
        self.path = path
        self.df = df.copy()
        self.kind = kind
        self.year_col = "Year"
        self.value_columns: List[str] = []
        self.month_columns: List[str] = []
        self.climate_param: str = "Other"  # NEW: climate variable type
        self._init_metadata()

    def _init_metadata(self):
        cols = list(self.df.columns)
        # guess year column
        for cand in cols:
            if _looks_like_year_column_name(cand):
                self.year_col = cand
                break
        if self.kind == "chronology":
            self.value_columns = [c for c in cols if c != self.year_col]
        elif self.kind == "climate":
            month_candidates: list[tuple[int, str]] = []

            for c in cols:
                if not c:
                    continue
                canonical = _normalize_month_header_token(c)
                if canonical is not None:
                    month_candidates.append((MONTH_NAMES.index(canonical), c))

            if len(month_candidates) >= 12:
                # Keep one column per month in calendar order.
                month_candidates.sort(key=lambda x: x[0])
                seen_months: set[int] = set()
                ordered_cols: list[str] = []
                for month_idx, col_name in month_candidates:
                    if month_idx in seen_months:
                        continue
                    seen_months.add(month_idx)
                    ordered_cols.append(col_name)
                self.month_columns = ordered_cols[:12]
            else:
                # fallback: any 12 non-year
                self.month_columns = [c for c in cols if c != self.year_col][:12]

# ---------------------------
# Climate missing-value handling
# ---------------------------
def _replace_sentinel_missing(
    df: pd.DataFrame,
    cols: List[str],
    sentinels: Tuple[float, ...] = (-999.0, -999, -999.000),
) -> pd.DataFrame:
    """Replace common sentinel missing values with NaN for the given columns."""
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
            out[c] = out[c].replace(list(sentinels), np.nan)
    return out


def _drop_all_nan_years(df: pd.DataFrame, month_cols: List[str]) -> pd.DataFrame:
    """Drop rows (years) where all 12 month values are NaN."""
    if not month_cols:
        return df
    out = df.copy()
    keep_mask = ~out[month_cols].isna().all(axis=1)
    return out.loc[keep_mask].reset_index(drop=True)


def _fill_monthly_means(df: pd.DataFrame, month_cols: List[str]) -> pd.DataFrame:
    """Fill NaNs in each monthly column with that column's mean."""
    out = df.copy()
    for c in month_cols:
        if c in out.columns:
            mean_val = pd.to_numeric(out[c], errors="coerce").mean(skipna=True)
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(mean_val)
    return out


def _fill_monthly_local_window_mean(
    df: pd.DataFrame,
    year_col: str,
    month_cols: List[str],
    k_years: int,
) -> pd.DataFrame:
    """Fill NaNs using a local mean around each missing year (per month column).

    For each missing value, the algorithm searches backward and forward in time to collect up to
    k_years valid values on each side (skipping missing values). A value is filled only if at least
    k_years valid values are found in total. No aggregation across different months is performed.
    """
    out = df.copy()
    if year_col in out.columns:
        out = out.sort_values(by=year_col).reset_index(drop=True)
    k = int(k_years) if k_years is not None else 0
    if k <= 0:
        return out

    n = len(out)
    for c in month_cols:
        if c not in out.columns:
            continue

        base = pd.to_numeric(out[c], errors="coerce").to_numpy()
        filled = base.copy()

        # Use only original (base) values as candidates to avoid cascading fills.
        for i in range(n):
            if not np.isnan(base[i]):
                continue

            before: List[float] = []
            j = i - 1
            while j >= 0 and len(before) < k:
                if not np.isnan(base[j]):
                    before.append(float(base[j]))
                j -= 1

            after: List[float] = []
            j = i + 1
            while j < n and len(after) < k:
                if not np.isnan(base[j]):
                    after.append(float(base[j]))
                j += 1

            candidates = before + after
            if len(candidates) >= k:
                filled[i] = float(np.mean(candidates))

        out[c] = filled

    return out


def _interpolate_monthly_limited(
    df: pd.DataFrame,
    year_col: str,
    month_cols: List[str],
    max_gap_years: int,
) -> pd.DataFrame:
    """Fill NaNs by linear interpolation across years (per month), limited by max_gap_years.

    Only interior gaps (surrounded by valid values) are filled. Leading/trailing NaNs remain NaN.
    The limit applies to the number of consecutive missing points in the series.
    """
    out = df.copy()
    if year_col in out.columns:
        out = out.sort_values(by=year_col).reset_index(drop=True)

    max_gap = int(max_gap_years) if max_gap_years is not None else 0
    if max_gap <= 0 or year_col not in out.columns:
        return out

    years = pd.to_numeric(out[year_col], errors="coerce").to_numpy()
    for c in month_cols:
        if c not in out.columns:
            continue
        vals = pd.to_numeric(out[c], errors="coerce").to_numpy()
        s = pd.Series(vals, index=years)
        s2 = s.interpolate(method="index", limit=max_gap, limit_area="inside")
        out[c] = s2.to_numpy()

    return out


def _prepare_monthly_climate_df_for_analysis(
    df: pd.DataFrame,
    year_col: str,
    month_cols: List[str],
    missing_policy: str,
    local_window_k: int = 5,
    interpolate_max_gap: int = 3,
) -> pd.DataFrame:
    """Return a processed copy of a monthly climate table ready for window generation."""
    out = df.copy()
    if year_col in out.columns:
        out[year_col] = pd.to_numeric(out[year_col], errors="coerce")
        out = out.dropna(subset=[year_col])
        out[year_col] = out[year_col].astype(int)

    out = _replace_sentinel_missing(out, month_cols)
    out = _drop_all_nan_years(out, month_cols)

    # Policies that actively fill missing values before aggregation.
    if missing_policy == "fill_monthly_mean":
        out = _fill_monthly_means(out, month_cols)
    elif missing_policy == "fill_local_window_mean":
        out = _fill_monthly_local_window_mean(out, year_col, month_cols, local_window_k)
    elif missing_policy == "interpolate_limited":
        out = _interpolate_monthly_limited(out, year_col, month_cols, interpolate_max_gap)

    return out

# ---------------------------
# Generate climate windows
# ---------------------------
def generate_climate_windows(
    climate_df: pd.DataFrame,
    year_col: str,
    month_cols_y: List[str],
    min_len: int = 1,
    max_len: int = 24,
    aggs: List[str] = ["sum"],
    missing_policy: str = "ignore",
) -> pd.DataFrame:
    df = climate_df.copy()
    df = df.sort_values(by=year_col).reset_index(drop=True)
    years = df[year_col].values
    month_cols_y = list(month_cols_y)
    if len(month_cols_y) != 12:
        raise ValueError("Expected 12 monthly columns for climate data.")
    # Use pJan..pDec for previous year and Jan..Dec for current year
    labels_prev = [f"p{m}" for m in MONTH_NAMES]  # pJan..pDec
    labels_curr = MONTH_NAMES[:]                  # Jan..Dec
    all_labels = labels_prev + labels_curr
    window_defs: Dict[Tuple[int, int], str] = {}
    for win_len in range(min_len, max_len + 1):
        for start in range(0, 24 - win_len + 1):
            end = start + win_len - 1
            start_label = all_labels[start]
            end_label = all_labels[end]
            # If single-month window, use single label (avoid Jan_Jan or pMay_pMay)
            if start == end:
                win_name = start_label
            else:
                win_name = f"{start_label}_{end_label}"
            window_defs[(start, end)] = win_name
    result_rows = []
    result_years = []
    for i in range(1, len(df)):
        year_t = years[i]
        prev_row = df.iloc[i - 1]
        curr_row = df.iloc[i]
        prev_vals = _to_numeric_1d(prev_row[month_cols_y])
        curr_vals = _to_numeric_1d(curr_row[month_cols_y])
        full_24 = np.concatenate([prev_vals, curr_vals])
        windows_values: Dict[str, Any] = {}
        for (start, end), name in window_defs.items():
            segment = full_24[start:end + 1]

            # Missing value policy:
            # - ignore: use NaN-aware numpy aggregations (current behavior)
            # - strict: require complete windows; if any value is missing, the window becomes NaN
            if missing_policy == "strict" and np.isnan(segment).any():
                if "sum" in aggs:
                    windows_values[f"{name}_sum"] = np.nan
                if "mean" in aggs:
                    windows_values[f"{name}_mean"] = np.nan
                continue
            if "sum" in aggs:
                windows_values[f"{name}_sum"] = float(np.nansum(segment))
            if "mean" in aggs:
                windows_values[f"{name}_mean"] = float(np.nanmean(segment))
        result_rows.append(windows_values)
        result_years.append(year_t)
    windows_df = pd.DataFrame(result_rows)
    windows_df.insert(0, year_col, result_years)
    return windows_df


# ---------------------------
# Compute correlations
# ---------------------------
def _fast_r_p(x, y, method: str = "pearson") -> Tuple[float, float]:
    """Pearson / Spearman r with the two-sided t-test p (same values as scipy.stats, ~50x faster)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan"), float("nan")
    if method == "spearman":
        x = pd.Series(x).rank().to_numpy()
        y = pd.Series(y).rank().to_numpy()
    xm = x - x.mean()
    ym = y - y.mean()
    den = np.sqrt(np.dot(xm, xm) * np.dot(ym, ym))
    if not den > 0:
        return float("nan"), float("nan")
    r = float(np.dot(xm, ym) / den)
    r = max(-1.0, min(1.0, r))
    if abs(r) >= 1.0:
        return r, 0.0
    df = n - 2
    t = r * np.sqrt(df / ((1.0 - r) * (1.0 + r)))
    try:
        from scipy.special import stdtr

        p = float(2.0 * stdtr(df, -abs(t)))
    except Exception:  # pragma: no cover
        p = float(math.erfc(abs(t) / math.sqrt(2.0)))
    return r, p


def compute_correlations(
    chron_sets: List[DataSet],
    climate_sets: List[DataSet],
    year_start: Optional[int],
    year_end: Optional[int],
    min_len: int,
    max_len: int,
    corr_method: str = "pearson",
    aggs: List[str] = ("sum",),
    season_months: Optional[Tuple[int, int]] = None,
    alpha: float = 0.05,
    exclude_single: bool = False,
    period_enable: bool = False,
    min_period_span: int = 50,
    step_years: int = 10,
    min_period_points: int = 30,
    period_mode: str = "anchored",
    period_rows_out: Optional[List[Dict[str, Any]]] = None,
    missing_policy: str = "ignore",
    local_window_k: int = 5,
    interpolate_max_gap: int = 3,
    agg_resolver=None,
    min_n: int = 3,
) -> pd.DataFrame:
    """
    Main correlation routine.

    If period_enable is True and period_rows_out is provided, for each
    Chronology x Climate x Window additionally computes correlations on
    multi-year periods according to the selected mode (anchored, sliding,
    or exhaustive grid).
    """
    rows: List[Dict[str, Any]] = []


    def window_in_season(si: int, ei: int, season: Tuple[int, int]) -> bool:
        s_month, e_month = season
        for idx in range(si, ei + 1):
            month = (idx % 12) + 1
            if s_month <= e_month:
                if not (s_month <= month <= e_month):
                    return False
            else:
                # season crossing year boundary (e.g., Oct–Mar)
                if not (month >= s_month or month <= e_month):
                    return False
        return True

    def compute_r_p(x_arr: np.ndarray, y_arr: np.ndarray) -> Tuple[float, float]:
        """Return (r, p) for given vectors according to corr_method."""
        if len(x_arr) < 3:
            return np.nan, np.nan
        r_val: float = np.nan
        p_val: float = np.nan
        if corr_method == "pearson":
            if stats is not None:
                try:
                    r_val, p_val = _fast_r_p(x_arr, y_arr, "pearson")
                except Exception:
                    r_val = float(np.corrcoef(x_arr, y_arr)[0, 1])
                    p_val = np.nan
            else:
                r_val = float(np.corrcoef(x_arr, y_arr)[0, 1])
        elif corr_method == "spearman":
            if stats is not None:
                try:
                    r_val, p_val = _fast_r_p(x_arr, y_arr, "spearman")
                except Exception:
                    r_val = np.nan
                    p_val = np.nan
        return float(r_val), float(p_val)

    max_grid_intervals = 5000

    def add_period_result(
        base_meta: Dict[str, Any],
        years_valid: np.ndarray,
        x_valid: np.ndarray,
        y_valid: np.ndarray,
        start_year: int,
        end_year: int,
        mode_label: str,
    ) -> None:
        if period_rows_out is None:
            return
        p_mask = (years_valid >= start_year) & (years_valid <= end_year)
        n_period = int(p_mask.sum())
        if n_period < min_period_points:
            return
        x_p = x_valid[p_mask]
        y_p = y_valid[p_mask]
        if len(x_p) < 3:
            return
        r_p, p_p = compute_r_p(x_p, y_p)
        if np.isnan(r_p):
            return
        signif_p = bool(not np.isnan(p_p) and p_p < alpha)
        prow = dict(base_meta)
        prow.update(
            {
                "PeriodStart": int(start_year),
                "PeriodEnd": int(end_year),
                "R": float(r_p),
                "p": float(p_p),
                "N": n_period,
                "Significant": signif_p,
                "PeriodMode": mode_label,
            }
        )
        period_rows_out.append(prow)

    for cset in chron_sets:
        for col in cset.value_columns:
            if cset.year_col not in cset.df.columns or col not in cset.df.columns:
                continue
            chrono = cset.df[[cset.year_col, col]].copy()
            chrono = chrono.dropna()
            chrono[cset.year_col] = pd.to_numeric(chrono[cset.year_col], errors="coerce")
            chrono = chrono.dropna(subset=[cset.year_col])
            chrono[cset.year_col] = chrono[cset.year_col].astype(int)

            for qset in climate_sets:
                month_cols = [mc for mc in qset.month_columns if mc in qset.df.columns]
                if qset.year_col not in qset.df.columns or len(month_cols) != 12:
                    continue

                aggs_for_climate = list(aggs)
                scale_factor = 1.0
                if agg_resolver is not None:
                    try:
                        aggs_for_climate, scale_factor = agg_resolver(cset, col, qset)
                    except Exception:
                        aggs_for_climate = list(aggs)
                        scale_factor = 1.0
                qdf_proc = _prepare_monthly_climate_df_for_analysis(
                    qset.df,
                    year_col=qset.year_col,
                    month_cols=month_cols,
                    missing_policy=missing_policy,
                    local_window_k=local_window_k,
                    interpolate_max_gap=interpolate_max_gap,
                )

                windows_df = generate_climate_windows(
                    qdf_proc,
                    year_col=qset.year_col,
                    month_cols_y=month_cols,
                    min_len=min_len,
                    max_len=max_len,
                    aggs=list(aggs_for_climate),
                    missing_policy=missing_policy,
                )
                if scale_factor != 1.0:
                    num_cols = [c for c in windows_df.columns if c != qset.year_col]
                    try:
                        windows_df[num_cols] = windows_df[num_cols] * scale_factor
                    except Exception:
                        for c in num_cols:
                            try:
                                windows_df[c] = pd.to_numeric(windows_df[c], errors="coerce") * scale_factor
                            except Exception:
                                pass
                df_merged = chrono.merge(
                    windows_df,
                    left_on=cset.year_col,
                    right_on=qset.year_col,
                    how="inner",
                    suffixes=("", "_clim"),
                )
                if year_start is not None:
                    df_merged = df_merged[df_merged[cset.year_col] >= year_start]
                if year_end is not None:
                    df_merged = df_merged[df_merged[cset.year_col] <= year_end]
                if df_merged.empty:
                    continue

                window_cols = [
                    c
                    for c in df_merged.columns
                    if ("_" in c and (c.startswith("L") or c.startswith("Y") or c.startswith("p") or c[:3] in [m[:3] for m in MONTH_NAMES]))
                ]

                for wcol in window_cols:
                    agg: Optional[str] = None
                    if wcol.endswith("_sum"):
                        agg = "sum"
                        wbase = wcol[:-4]
                    elif wcol.endswith("_mean"):
                        agg = "mean"
                        wbase = wcol[:-5]
                    else:
                        wbase = wcol

                    try:
                        # for multi-month names the base has an underscore; for single-month names wbase may be like 'Jan' or 'pMay'
                        parts = wbase.split("_")
                        if len(parts) == 1:
                            start_label = parts[0]
                            end_label = parts[0]
                        else:
                            start_label, end_label = parts[0], parts[-1]
                    except Exception:
                        continue

                    def label_to_idx(lbl: str) -> Optional[int]:
                        s = str(lbl)
                        # pJan..pDec -> 0..11
                        if s.startswith("p") and len(s) > 1:
                            m = s[1:4].capitalize()
                            if m in MONTH_NAMES:
                                return MONTH_NAMES.index(m)
                        # Jan..Dec -> 12..23
                        m3 = s[:3].capitalize()
                        if m3 in MONTH_NAMES:
                            return 12 + MONTH_NAMES.index(m3)
                        # Backward compatibility: L1..L12, Y1..Y12
                        if s.startswith("L"):
                            try:
                                return int(s[1:]) - 1
                            except Exception:
                                return None
                        if s.startswith("Y"):
                            try:
                                return 12 + (int(s[1:]) - 1)
                            except Exception:
                                return None
                        # Fallback: if label equals one of month_columns (old style)
                        if s in qset.month_columns:
                            idx = qset.month_columns.index(s)
                            return 12 + idx
                        return None

                    si = label_to_idx(start_label)
                    ei = label_to_idx(end_label)
                    if si is None or ei is None:
                        continue

                    if exclude_single and (ei - si + 1) == 1:
                        continue

                    if season_months is not None:
                        if not window_in_season(si, ei, season_months):
                            continue

                    x = pd.to_numeric(df_merged[col], errors="coerce").astype(float)
                    y = pd.to_numeric(df_merged[wcol], errors="coerce").astype(float)
                    mask = ~np.isnan(x) & ~np.isnan(y)
                    if not mask.any():
                        continue

                    x_valid = x[mask].to_numpy()
                    y_valid = y[mask].to_numpy()
                    n = int(mask.sum())
                    if n < max(3, int(min_n)):
                        continue

                    r, p = compute_r_p(x_valid, y_valid)
                    if np.isnan(r):
                        continue
                    signif = bool(not np.isnan(p) and p < alpha)

                    base_meta = {
                        "Chronology": cset.name,
                        "Chron_Column": col,
                        "Climate": qset.name,
                        "Window": wbase,
                        "Agg": agg or "",
                    }

                    row = dict(base_meta)
                    row.update({"R": float(r), "p": float(p), "N": n, "Significant": signif})
                    rows.append(row)

                    # --- optional multi-year periods ---
                    if period_enable and period_rows_out is not None:
                        years_all = pd.to_numeric(
                            df_merged[cset.year_col], errors="coerce"
                        ).astype(float)
                        years_valid = years_all[mask].to_numpy()
                        if len(years_valid) < min_period_points:
                            continue
                        y_min = int(np.nanmin(years_valid))
                        y_max = int(np.nanmax(years_valid))
                        span_total = y_max - y_min + 1
                        if span_total < min_period_span:
                            continue

                        step_val = max(1, int(step_years))
                        min_span_val = max(2, int(min_period_span))
                        visited: set[tuple[int, int]] = set()

                        if period_mode.lower() == "anchored":
                            # Right-anchored series: varying start, fixed end
                            start = y_min + step_val
                            while (y_max - start + 1) >= min_span_val:
                                if (start, y_max) not in visited:
                                    add_period_result(base_meta, years_valid, x_valid, y_valid, start, y_max, "anchored")
                                    visited.add((start, y_max))
                                start += step_val
                            # Left-anchored series: varying end, fixed start
                            end = y_max - step_val
                            while (end - y_min + 1) >= min_span_val:
                                if (y_min, end) not in visited:
                                    add_period_result(base_meta, years_valid, x_valid, y_valid, y_min, end, "anchored")
                                    visited.add((y_min, end))
                                end -= step_val

                        elif period_mode.lower() == "sliding":
                            L = min_span_val
                            if span_total >= L:
                                start_year = y_min
                                while start_year <= y_max - L + 1:
                                    end_year = start_year + L - 1
                                    add_period_result(base_meta, years_valid, x_valid, y_valid, start_year, end_year, "sliding")
                                    start_year += step_val

                        elif period_mode.lower() == "grid":
                            intervals_checked = 0
                            for start_year in range(y_min, y_max + 1, step_val):
                                for end_year in range(start_year + min_span_val - 1, y_max + 1, step_val):
                                    add_period_result(base_meta, years_valid, x_valid, y_valid, start_year, end_year, "grid")
                                    intervals_checked += 1
                                    if intervals_checked >= max_grid_intervals:
                                        print(
                                            f"Grid period scan for {cset.name}×{qset.name} reached {max_grid_intervals} intervals; stopping further intervals.",
                                            flush=True,
                                        )
                                        break
                                if intervals_checked >= max_grid_intervals:
                                    break
                    # --- end multi-year periods ---

    df_res = pd.DataFrame(rows)
    if not df_res.empty:
        df_res["absR"] = df_res["R"].abs()
        df_res = df_res.sort_values(by="absR", ascending=False).drop(columns=["absR"])
        df_res["Annot"] = df_res.apply(lambda r: f"{r['R']:.3f}" + (" *" if r["Significant"] else ""), axis=1)

    return df_res


def compute_daily_timeseries_correlations(
    chron_sets: List[DataSet],
    timeseries_sets: List[DataSet],
    year_start: Optional[int],
    year_end: Optional[int],
    min_len: int,
    max_len: int,
    corr_method: str = "pearson",
    aggs: List[str] = ("sum",),
    alpha: float = 0.05,
    exclude_single: bool = False,
    base_agg_choice: str = "Auto (by variable)",
    agg_resolver=None,
    min_n: int = 3,
    ) -> pd.DataFrame:
    """
    Compute chronology correlations against same-year daily climate windows.

    Window labels are calendar-aware: Jan01..Dec31 for single-day windows and
    Jan01_Jan07 for multi-day windows.
    """
    rows: List[Dict[str, Any]] = []

    def compute_r_p(x_arr: np.ndarray, y_arr: np.ndarray) -> Tuple[float, float]:
        if len(x_arr) < 3:
            return np.nan, np.nan
        r_val: float = np.nan
        p_val: float = np.nan
        if corr_method == "pearson":
            if stats is not None:
                try:
                    r_val, p_val = _fast_r_p(x_arr, y_arr, "pearson")
                except Exception:
                    r_val = float(np.corrcoef(x_arr, y_arr)[0, 1])
                    p_val = np.nan
            else:
                r_val = float(np.corrcoef(x_arr, y_arr)[0, 1])
        elif corr_method == "spearman":
            if stats is not None:
                try:
                    r_val, p_val = _fast_r_p(x_arr, y_arr, "spearman")
                except Exception:
                    r_val = np.nan
                    p_val = np.nan
        return float(r_val), float(p_val)

    for cset in chron_sets:
        for col in cset.value_columns:
            if cset.year_col not in cset.df.columns or col not in cset.df.columns:
                continue

            chrono = cset.df[[cset.year_col, col]].copy()
            chrono = chrono.dropna()
            chrono[cset.year_col] = pd.to_numeric(chrono[cset.year_col], errors="coerce")
            chrono = chrono.dropna(subset=[cset.year_col])
            chrono[cset.year_col] = chrono[cset.year_col].astype(int)

            for qset in timeseries_sets:
                daily_wide = timeseries_to_daily_wide_dataframe(
                    qset,
                    base_agg_choice=base_agg_choice,
                )
                if "Year" not in daily_wide.columns:
                    continue

                aggs_for_climate = list(aggs)
                scale_factor = 1.0
                if agg_resolver is not None:
                    try:
                        aggs_for_climate, scale_factor = agg_resolver(cset, col, qset)
                    except Exception:
                        aggs_for_climate = list(aggs)
                        scale_factor = 1.0

                day_cols = [c for c in daily_wide.columns if c != "Year"]
                if not day_cols:
                    continue

                day_values = daily_wide[day_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
                years_vec = pd.to_numeric(daily_wide["Year"], errors="coerce").to_numpy()

                window_rows: List[Dict[str, Any]] = []
                window_years: List[int] = []
                for i in range(len(daily_wide)):
                    y = years_vec[i]
                    if not np.isfinite(y):
                        continue
                    vals = day_values[i]
                    row_out: Dict[str, Any] = {}
                    for win_len in range(int(min_len), int(max_len) + 1):
                        if exclude_single and win_len == 1:
                            continue
                        for start in range(0, 366 - win_len + 1):
                            end = start + win_len - 1
                            label_start = day_cols[start]
                            label_end = day_cols[end]
                            win_name = label_start if start == end else f"{label_start}_{label_end}"
                            seg = vals[start:end + 1]
                            if "sum" in aggs_for_climate:
                                row_out[f"{win_name}_sum"] = float(np.nansum(seg))
                            if "mean" in aggs_for_climate:
                                row_out[f"{win_name}_mean"] = float(np.nanmean(seg))
                    window_rows.append(row_out)
                    window_years.append(int(y))

                if not window_rows:
                    continue

                windows_df = pd.DataFrame(window_rows)
                windows_df.insert(0, "Year", window_years)

                if scale_factor != 1.0:
                    num_cols = [c for c in windows_df.columns if c != "Year"]
                    try:
                        windows_df[num_cols] = windows_df[num_cols] * scale_factor
                    except Exception:
                        for c in num_cols:
                            try:
                                windows_df[c] = pd.to_numeric(windows_df[c], errors="coerce") * scale_factor
                            except Exception:
                                pass

                df_merged = chrono.merge(
                    windows_df,
                    left_on=cset.year_col,
                    right_on="Year",
                    how="inner",
                    suffixes=("", "_clim"),
                )
                if year_start is not None:
                    df_merged = df_merged[df_merged[cset.year_col] >= year_start]
                if year_end is not None:
                    df_merged = df_merged[df_merged[cset.year_col] <= year_end]
                if df_merged.empty:
                    continue

                window_cols = []
                for c in df_merged.columns:
                    if c in ("Year", cset.year_col, col):
                        continue
                    if not (c.endswith("_sum") or c.endswith("_mean")):
                        continue
                    if c.endswith("_sum"):
                        base = c[:-4]
                    else:
                        base = c[:-5]
                    first_token = base.split("_")[0]
                    if _window_label_to_idx(first_token) is not None:
                        window_cols.append(c)
                for wcol in window_cols:
                    if wcol.endswith("_sum"):
                        agg = "sum"
                        wbase = wcol[:-4]
                    elif wcol.endswith("_mean"):
                        agg = "mean"
                        wbase = wcol[:-5]
                    else:
                        agg = ""
                        wbase = wcol

                    x = pd.to_numeric(df_merged[col], errors="coerce").astype(float)
                    y = pd.to_numeric(df_merged[wcol], errors="coerce").astype(float)
                    mask = ~np.isnan(x) & ~np.isnan(y)
                    if not mask.any():
                        continue

                    x_valid = x[mask].to_numpy()
                    y_valid = y[mask].to_numpy()
                    n = int(mask.sum())
                    if n < max(3, int(min_n)):
                        continue

                    r, p = compute_r_p(x_valid, y_valid)
                    if np.isnan(r):
                        continue
                    signif = bool(not np.isnan(p) and p < alpha)

                    rows.append(
                        {
                            "Chronology": cset.name,
                            "Chron_Column": col,
                            "Climate": qset.name,
                            "Window": wbase,
                            "Agg": agg,
                            "R": float(r),
                            "p": float(p),
                            "N": n,
                            "Significant": signif,
                        }
                    )

    df_res = pd.DataFrame(rows)
    if not df_res.empty:
        df_res["absR"] = df_res["R"].abs()
        df_res = df_res.sort_values(by="absR", ascending=False).drop(columns=["absR"])
        df_res["Annot"] = df_res.apply(
            lambda r: f"{r['R']:.3f}" + (" *" if r["Significant"] else ""),
            axis=1,
        )
    return df_res




def compute_chronology_correlations(
    chron_sets_a: List[DataSet],
    chron_sets_b: List[DataSet],
    year_start: Optional[int],
    year_end: Optional[int],
    corr_method: str = "pearson",
    alpha: float = 0.05,
    period_enable: bool = False,
    min_period_span: int = 50,
    step_years: int = 10,
    min_period_points: int = 30,
    period_mode: str = "anchored",
    period_rows_out: Optional[List[Dict[str, Any]]] = None,
    min_n: int = 3,
) -> pd.DataFrame:
    """
    Compute correlations between chronology series (chronology × chronology).

    Output schema is aligned with the main results table:
        - Chronology, Chron_Column: left series
        - Climate: right series dataset name (used as "target" for compatibility)
        - Target_Column: right series column name
        - Window: always "FULL" (no climate windows)
        - Agg: always "series"
    If period_enable is True and period_rows_out is provided, additional rows are appended to
    period_rows_out with PeriodStart/PeriodEnd and PeriodMode.
    """
    rows: List[Dict[str, Any]] = []

    def compute_r_p(x_arr: np.ndarray, y_arr: np.ndarray) -> Tuple[float, float]:
        """Return (r, p) for given vectors according to corr_method."""
        if len(x_arr) < 3:
            return np.nan, np.nan
        r_val: float = np.nan
        p_val: float = np.nan
        if corr_method == "pearson":
            if stats is not None:
                try:
                    r_val, p_val = _fast_r_p(x_arr, y_arr, "pearson")
                except Exception:
                    r_val = float(np.corrcoef(x_arr, y_arr)[0, 1])
                    p_val = np.nan
            else:
                r_val = float(np.corrcoef(x_arr, y_arr)[0, 1])
        elif corr_method == "spearman":
            if stats is not None:
                try:
                    r_val, p_val = _fast_r_p(x_arr, y_arr, "spearman")
                except Exception:
                    r_val = pd.Series(x_arr).corr(pd.Series(y_arr), method="spearman")
                    p_val = np.nan
            else:
                r_val = pd.Series(x_arr).corr(pd.Series(y_arr), method="spearman")
        else:
            r_val = pd.Series(x_arr).corr(pd.Series(y_arr), method=corr_method)
            p_val = np.nan
        return float(r_val), float(p_val)

    def add_period_result(
        base_meta: Dict[str, Any],
        years_valid: np.ndarray,
        x_valid: np.ndarray,
        y_valid: np.ndarray,
        start_year: int,
        end_year: int,
        mode_label: str,
    ) -> None:
        if period_rows_out is None:
            return
        p_mask = (years_valid >= start_year) & (years_valid <= end_year)
        n_period = int(p_mask.sum())
        if n_period < min_period_points:
            return
        x_p = x_valid[p_mask]
        y_p = y_valid[p_mask]
        if len(x_p) < 3:
            return
        r_p, p_p = compute_r_p(x_p, y_p)
        if np.isnan(r_p):
            return
        signif_p = bool(not np.isnan(p_p) and p_p < alpha)
        prow = dict(base_meta)
        prow.update(
            {
                "PeriodStart": int(start_year),
                "PeriodEnd": int(end_year),
                "R": float(r_p),
                "p": float(p_p),
                "N": n_period,
                "Significant": signif_p,
                "PeriodMode": mode_label,
            }
        )
        period_rows_out.append(prow)

    for a in chron_sets_a:
        # Ensure value_columns are aligned with the actual dataframe columns.
        if not getattr(a, "value_columns", None):
            a.value_columns = [c for c in a.df.columns if c != a.year_col]
        cols_a = [c for c in a.value_columns if c in a.df.columns and c != a.year_col]

        for col_a in cols_a:
            try:
                df_a = a.df[[a.year_col, col_a]].copy()
            except KeyError:
                continue
            df_a[a.year_col] = pd.to_numeric(df_a[a.year_col], errors="coerce")
            df_a[col_a] = pd.to_numeric(df_a[col_a], errors="coerce")
            df_a = df_a.dropna(subset=[a.year_col])

            for b in chron_sets_b:
                if b.year_col not in b.df.columns:
                    continue

                # Ensure value_columns are aligned with the actual dataframe columns.
                if not getattr(b, "value_columns", None):
                    b.value_columns = [c for c in b.df.columns if c != b.year_col]
                cols_b = [c for c in b.value_columns if c in b.df.columns and c != b.year_col]

                for col_b in cols_b:
                    for col_b in [col_b]:  # (a second loop over all columns duplicated every row)
                        if a is b and col_a == col_b:
                            continue

                        if col_b not in b.df.columns:
                            continue

                        try:
                            df_b = b.df[[b.year_col, col_b]].copy()
                        except KeyError:
                            continue
                        df_b[b.year_col] = pd.to_numeric(df_b[b.year_col], errors="coerce")
                        df_b[col_b] = pd.to_numeric(df_b[col_b], errors="coerce")
                        df_b = df_b.dropna(subset=[b.year_col])

                        merged = df_a.merge(
                            df_b,
                            left_on=a.year_col,
                            right_on=b.year_col,
                            how="inner",
                            suffixes=("_a", "_b"),
                        )
                        if merged.empty:
                            continue

                        merged["Year"] = pd.to_numeric(merged[a.year_col], errors="coerce")
                        merged = merged.dropna(subset=["Year"])
                        merged["Year"] = merged["Year"].astype(int)

                        if year_start is not None:
                            merged = merged[merged["Year"] >= int(year_start)]
                        if year_end is not None:
                            merged = merged[merged["Year"] <= int(year_end)]
                        if merged.empty:
                            continue

                        # If both datasets share the same value-column name, pandas.merge() applies suffixes
                        # and the unsuffixed column is not available (e.g. "Chronology" -> "Chronology_a"/"Chronology_b").
                        x_col = col_a
                        y_col = col_b
                        if col_a == col_b:
                            x_col = f"{col_a}_a"
                            y_col = f"{col_b}_b"
                        if x_col not in merged.columns or y_col not in merged.columns:
                            # Fallback for unexpected column renames.
                            if f"{col_a}_a" in merged.columns:
                                x_col = f"{col_a}_a"
                            if f"{col_b}_b" in merged.columns:
                                y_col = f"{col_b}_b"
                        if x_col not in merged.columns or y_col not in merged.columns:
                            continue

                        x = _to_numeric_1d(merged[x_col])
                        y = _to_numeric_1d(merged[y_col])
                        mask = ~np.isnan(x) & ~np.isnan(y)
                        n = int(mask.sum())
                        if n < max(3, int(min_n)):
                            continue

                        years_valid = merged["Year"].to_numpy(dtype=float)[mask]
                        x_valid = x[mask]
                        y_valid = y[mask]

                        r, p = compute_r_p(x_valid, y_valid)
                        if np.isnan(r):
                            continue
                        signif = bool(not np.isnan(p) and p < alpha)

                        y_min = int(np.nanmin(years_valid))
                        y_max = int(np.nanmax(years_valid))

                        row = {
                            "Chronology": a.name,
                            "Chron_Column": col_a,
                            "Climate": b.name,
                            "Target_Column": col_b,
                            "Window": "FULL",
                            "Agg": "series",
                            "OverlapStart": y_min,
                            "OverlapEnd": y_max,
                            "R": float(r),
                            "p": float(p),
                            "N": n,
                            "Significant": signif,
                        }
                        rows.append(row)

                        if period_enable and period_rows_out is not None:
                            span_total = y_max - y_min + 1
                            if span_total < min_period_span:
                                continue
                            step_val = max(1, int(step_years))
                            min_span_val = max(2, int(min_period_span))
                            visited: set[tuple[int, int]] = set()

                            base_meta = {
                                "Chronology": a.name,
                                "Chron_Column": col_a,
                                "Climate": b.name,
                                "Target_Column": col_b,
                                "Window": "FULL",
                                "Agg": "series",
                                "OverlapStart": y_min,
                                "OverlapEnd": y_max,
                            }

                            if period_mode.lower() == "anchored":
                                start = y_min + step_val
                                while (y_max - start + 1) >= min_span_val:
                                    if (start, y_max) not in visited:
                                        add_period_result(base_meta, years_valid, x_valid, y_valid, start, y_max, "anchored")
                                        visited.add((start, y_max))
                                    start += step_val

                                end = y_max - step_val
                                while (end - y_min + 1) >= min_span_val:
                                    if (y_min, end) not in visited:
                                        add_period_result(base_meta, years_valid, x_valid, y_valid, y_min, end, "anchored")
                                        visited.add((y_min, end))
                                    end -= step_val

                            elif period_mode.lower() == "sliding":
                                L = min_span_val
                                if span_total >= L:
                                    start_year = y_min
                                    while start_year <= y_max - L + 1:
                                        end_year = start_year + L - 1
                                        add_period_result(base_meta, years_valid, x_valid, y_valid, start_year, end_year, "sliding")
                                        start_year += step_val

                            elif period_mode.lower() == "grid":
                                max_grid_intervals = 20000
                                intervals_checked = 0
                                for start_year in range(y_min, y_max + 1, step_val):
                                    for end_year in range(start_year + min_span_val - 1, y_max + 1, step_val):
                                        add_period_result(base_meta, years_valid, x_valid, y_valid, start_year, end_year, "grid")
                                        intervals_checked += 1
                                        if intervals_checked >= max_grid_intervals:
                                            print(
                                                f"Grid period scan for {a.name}×{b.name} reached {max_grid_intervals} intervals; stopping further intervals.",
                                                flush=True,
                                            )
                                            break
                                    if intervals_checked >= max_grid_intervals:
                                        break

    df_res = pd.DataFrame(rows)
    if not df_res.empty:
        df_res["absR"] = df_res["R"].abs()
        df_res = df_res.sort_values(by="absR", ascending=False).drop(columns=["absR"])
        df_res["Annot"] = df_res.apply(
            lambda r: f"{r['R']:.3f}" + (" *" if r["Significant"] else ""), axis=1
        )
    return df_res


# ---------------------------
# Parallel correlation screening (one task per chronology x dataset pair)
# ---------------------------
class _AggResolver:
    """Picklable version of the aggregation choice (used in worker processes)."""

    def __init__(self, choice: str) -> None:
        self.choice = choice

    def __call__(self, cset, col_name, clim_ds):
        choice = self.choice
        if choice == "both":
            return ["sum", "mean"], 1.0
        if choice == "Auto (by variable)":
            return [get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))], 1.0
        if choice in ("sum", "mean"):
            return [choice], 1.0
        if choice == "sum/2":
            return [get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))], 0.5
        return [choice], 1.0


def _corr_pair_task(kind: str, cset, rset, kwargs: Dict[str, Any]):
    """Worker: correlations of one chronology data set with one climate / target data set."""
    periods: List[Dict[str, Any]] = []
    if kind == "monthly":
        df = compute_correlations(chron_sets=[cset], climate_sets=[rset], period_rows_out=periods, **kwargs)
    elif kind == "daily":
        df = compute_daily_timeseries_correlations(chron_sets=[cset], timeseries_sets=[rset], **kwargs)
    else:
        df = compute_chronology_correlations(chron_sets_a=[cset], chron_sets_b=[rset], period_rows_out=periods,
                                             **kwargs)
    return df, periods


def system_resources() -> Dict[str, float]:
    """Logical CPUs and available / total memory (MB) of this computer."""
    cpu = os.cpu_count() or 1
    avail = total = 0.0
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        avail, total = vm.available / 2 ** 20, vm.total / 2 ** 20
    except Exception:
        try:
            if sys.platform == "win32":
                import ctypes

                class _MS(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

                ms = _MS()
                ms.dwLength = ctypes.sizeof(_MS)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
                avail, total = ms.ullAvailPhys / 2 ** 20, ms.ullTotalPhys / 2 ** 20
            elif sys.platform == "darwin":
                import subprocess as _sp

                total = int(_sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2 ** 20
                vm = _sp.run(["vm_stat"], capture_output=True, text=True).stdout
                page = 4096
                m = re.search(r"page size of (\d+)", vm)
                if m:
                    page = int(m.group(1))
                free = sum(int(x) for x in re.findall(r"Pages (?:free|inactive|speculative|purgeable):\s+(\d+)", vm))
                avail = free * page / 2 ** 20
            else:
                pages = os.sysconf("SC_AVPHYS_PAGES")
                size = os.sysconf("SC_PAGE_SIZE")
                avail = pages * size / 2 ** 20
                total = os.sysconf("SC_PHYS_PAGES") * size / 2 ** 20
        except Exception:
            pass
    return {"cpu": float(cpu), "mem_avail_mb": float(avail), "mem_total_mb": float(total)}


def safe_worker_count(n_tasks: int, requested: int = 0, per_worker_mb: float = 450.0,
                      data_mb: float = 0.0) -> int:
    """How many worker processes can run at the same time without slowing the computer down.

    One logical CPU is left for the window and the system; every worker needs about ``per_worker_mb``
    (Python + numpy/pandas/scipy) plus its copy of the data; 1.5 GB of memory stays free.
    """
    res = system_resources()
    cpu = int(res["cpu"])
    by_cpu = max(1, cpu - 1)
    if res["mem_avail_mb"] > 0:
        by_mem = int((res["mem_avail_mb"] - 1536.0) // (per_worker_mb + data_mb))
    else:
        by_mem = 4
    limit = max(1, min(by_cpu, by_mem, 61))  # 61 = Windows limit of ProcessPoolExecutor
    if requested and requested > 0:
        limit = max(1, min(int(requested), by_cpu + 1, 61))
    return max(1, min(limit, int(n_tasks)))


class AnalysisControl:
    """Progress and cancel state shared between the GUI and the analysis."""

    def __init__(self) -> None:
        import threading as _th

        self.cancel = _th.Event()
        self.done = 0
        self.total = 0
        self.workers = 1
        self.note = ""


def run_correlation_tasks(kind: str, chron_sets: List["DataSet"], right_sets: List["DataSet"],
                          kwargs: Dict[str, Any], workers: int = 0,
                          ctl: Optional[AnalysisControl] = None, pairs: Optional[List[Tuple[Any, Any]]] = None,
                          per_pair_out: Optional[List[Any]] = None) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """All chronology x data-set pairs (or the given ``pairs``); in parallel worker processes when it pays off.

    ``per_pair_out`` receives (chronology, dataset, df, periods) of every pair (for re-use later).
    """
    ctl = ctl or AnalysisControl()
    tasks = list(pairs) if pairs is not None else [(c, r) for c in chron_sets for r in right_sets]
    if not tasks:
        return pd.DataFrame(), []
    ctl.total = len(tasks)
    results: Dict[int, Tuple[pd.DataFrame, List[Dict[str, Any]]]] = {}
    try:
        data_mb = max(float(r.df.memory_usage(deep=True).sum()) for _c, r in tasks) / 2 ** 20 * 3
    except Exception:
        data_mb = 0.0
    n_proc = safe_worker_count(len(tasks), workers, data_mb=data_mb)
    use_pool = n_proc > 1 and len(tasks) >= 3
    ctl.workers = n_proc if use_pool else 1

    def sequential(indices) -> None:
        for k in indices:
            if ctl.cancel.is_set():
                raise _AnalysisCancelled()
            c, r = tasks[k]
            results[k] = _corr_pair_task(kind, c, r, kwargs)
            ctl.done = len(results)

    if use_pool:
        import concurrent.futures as cf
        import multiprocessing as mp

        try:
            ctx = mp.get_context("spawn")
            ex = cf.ProcessPoolExecutor(max_workers=n_proc, mp_context=ctx)
        except Exception as exc:  # pragma: no cover
            ctl.note = f"parallel start failed ({exc}); one process"
            ctl.workers = 1
            sequential(range(len(tasks)))
            ex = None
        if ex is not None:
            futs = {}
            try:
                for k, (c, r) in enumerate(tasks):
                    futs[ex.submit(_corr_pair_task, kind, c, r, kwargs)] = k
                pending = set(futs)
                while pending:
                    if ctl.cancel.is_set():
                        raise _AnalysisCancelled()
                    done, pending = cf.wait(pending, timeout=0.25, return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        results[futs[f]] = f.result()
                    ctl.done = len(results)
            except _AnalysisCancelled:
                _kill_pool(ex)
                raise
            except Exception as exc:
                # e.g. a worker could not start (antivirus, frozen app without freeze_support)
                _kill_pool(ex)
                ctl.note = f"parallel run failed ({type(exc).__name__}: {exc}); finished in one process"
                ctl.workers = 1
                sequential([k for k in range(len(tasks)) if k not in results])
            else:
                ex.shutdown(wait=True)
    else:
        sequential(range(len(tasks)))
    if per_pair_out is not None:
        for k in sorted(results):
            per_pair_out.append((tasks[k][0], tasks[k][1], results[k][0], results[k][1]))
    frames = [results[k][0] for k in sorted(results) if results[k][0] is not None and not results[k][0].empty]
    periods: List[Dict[str, Any]] = []
    for k in sorted(results):
        periods.extend(results[k][1])
    if not frames:
        return pd.DataFrame(), periods
    df = pd.concat(frames, ignore_index=True)
    if "R" in df.columns:
        order = df["R"].abs().sort_values(ascending=False, kind="mergesort").index
        df = df.loc[order].reset_index(drop=True)
    return df, periods


def dataset_fingerprint(ds) -> str:
    """Changes when the data, the columns or the climate type of a dataset change."""
    import hashlib

    h = hashlib.sha1()
    h.update(str(getattr(ds, "name", "")).encode())
    for attr in ("kind", "climate_param", "year_col", "value_columns", "month_columns", "monthly_agg", "source_freq"):
        h.update(repr(getattr(ds, attr, None)).encode())
    try:
        h.update(pd.util.hash_pandas_object(ds.df, index=False).values.tobytes())
        h.update(repr(list(ds.df.columns)).encode())
    except Exception:
        h.update(repr(ds.df.shape).encode())
    return h.hexdigest()


def settings_fingerprint(kind: str, kwargs: Dict[str, Any]) -> str:
    items = []
    for k in sorted(kwargs):
        v = kwargs[k]
        if isinstance(v, _AggResolver):
            v = ("agg", v.choice)
        items.append((k, repr(v)))
    return repr((kind, items))


def merge_pair_results(parts: List[Tuple[pd.DataFrame, List[Dict[str, Any]]]]) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    frames = [df for df, _p in parts if df is not None and not df.empty]
    periods: List[Dict[str, Any]] = []
    for _df, p in parts:
        periods.extend(p or [])
    if not frames:
        return pd.DataFrame(), periods
    df = pd.concat(frames, ignore_index=True)
    if "R" in df.columns:
        order = df["R"].abs().sort_values(ascending=False, kind="mergesort").index
        df = df.loc[order].reset_index(drop=True)
    return df, periods


class _AnalysisCancelled(Exception):
    pass


def _kill_pool(ex) -> None:
    try:
        ex.shutdown(wait=False, cancel_futures=True)
    except TypeError:  # Python < 3.9
        ex.shutdown(wait=False)
    for p in list(getattr(ex, "_processes", {}) .values() if getattr(ex, "_processes", None) else []):
        try:
            p.terminate()
        except Exception:
            pass


# ---------------------------
# Column Edit Dialog (filter + rename)
# ---------------------------
class ColumnEditDialog(QDialog):
    """
    Dialog to select which columns to keep and to rename them before import.
    Returns a dict: { 'keep': [col1, col2, ...], 'rename': {old: new, ...} }
    Supports prefill suggestions via optional param.
    """
    def __init__(self, columns: List[str], prefill_rename: Optional[Dict[str, str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit / Filter Columns")
        self.resize(600, 420)
        self.columns = list(columns)
        self.rename_map: Dict[str, str] = {c: c for c in self.columns}
        if prefill_rename:
            # only apply keys present
            for k, v in prefill_rename.items():
                if k in self.rename_map and v:
                    self.rename_map[k] = v
        self.keep_set = set(self.columns)

        layout = QVBoxLayout()
        instr = QLabel("Select columns to keep (checked) and rename selected column using the field below.")
        layout.addWidget(instr)

        self.list_widget = QListWidget()
        for c in self.columns:
            item = QListWidgetItem(c)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            item.setCheckState(Qt.Checked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)

        # Buttons to quickly select or clear all columns
        btn_sel_layout = QHBoxLayout()
        btn_select_all = QPushButton("Select all")
        btn_clear_all = QPushButton("Clear selection")
        btn_select_all.clicked.connect(self.select_all_columns)
        btn_clear_all.clicked.connect(self.clear_all_columns)
        btn_sel_layout.addWidget(btn_select_all)
        btn_sel_layout.addWidget(btn_clear_all)
        layout.addLayout(btn_sel_layout)

        grid = QGridLayout()
        grid.addWidget(QLabel("Selected column:"), 0, 0)
        self.lbl_selected = QLabel("")
        grid.addWidget(self.lbl_selected, 0, 1)

        grid.addWidget(QLabel("New name:"), 1, 0)
        self.edit_newname = QLineEdit()
        grid.addWidget(self.edit_newname, 1, 1)

        btn_apply_name = QPushButton("Apply rename to selected")
        btn_apply_name.clicked.connect(self.apply_rename)
        grid.addWidget(btn_apply_name, 2, 0, 1, 2)

        # quick suggestions area: try to suggest Year / Chronology / Jan..Dec if columns match
        self.btn_suggest = QPushButton("Apply quick suggestions")
        self.btn_suggest.clicked.connect(self.apply_quick_suggestions)
        grid.addWidget(self.btn_suggest, 3, 0, 1, 2)  # Fixed: added self.

        layout.addLayout(grid)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self.list_widget.currentItemChanged.connect(self.on_selection_changed)
        self.setLayout(layout)

    def select_all_columns(self):
        """Mark all columns as kept (checked)."""
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.Checked)

    def clear_all_columns(self):
        """Uncheck all columns (none kept)."""
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.Unchecked)

    def on_selection_changed(self, current: QListWidgetItem, previous: QListWidgetItem):
        if current is None:
            self.lbl_selected.setText("")
            self.edit_newname.setText("")
            return
        old = current.text()
        self.lbl_selected.setText(old)
        self.edit_newname.setText(self.rename_map.get(old, old))

    def apply_rename(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        old = item.text()
        new = self.edit_newname.text().strip()
        if new == "":
            QMessageBox.warning(self, "Empty name", "New name cannot be empty.")
            return
        kept_old_names = [self.list_widget.item(i).text() for i in range(self.list_widget.count()) if self.list_widget.item(i).checkState() == Qt.Checked]
        future_names = [self.rename_map.get(n, n) for n in kept_old_names if n != old] + [new]
        if len(set(future_names)) != len(future_names):
            QMessageBox.warning(self, "Duplicate name", "This new name would duplicate another column name. Choose a different name.")
            return
        self.rename_map[old] = new
        QMessageBox.information(self, "Rename applied", f"Column '{old}' will be renamed to '{new}'.")

    def apply_quick_suggestions(self):
        cols = self.columns
        suggestions = {}
        # common heuristics
        # 2-column: Year + Value
        if len(cols) == 2:
            suggestions = {cols[0]: "Year", cols[1]: "Chronology"}
        # 13+ columns: Year + 12 months, or StationID + Year + 12 months
        elif len(cols) >= 13:
            suggestions = {}
            year_idx = None
            for i, c in enumerate(cols):
                if _looks_like_year_column_name(c):
                    year_idx = i
                    break

            # First, try explicit month headers such as I..XII, Jan..Dec, Янв..Дек, Y1..Y12.
            month_hits: list[tuple[int, str]] = []
            for c in cols:
                canonical = _normalize_month_header_token(c)
                if canonical is not None:
                    month_hits.append((MONTH_NAMES.index(canonical), c))

            unique_month_indices = {idx for idx, _ in month_hits}
            if year_idx is not None and len(unique_month_indices) >= 12:
                suggestions[cols[year_idx]] = "Year"
                month_hits.sort(key=lambda x: x[0])
                seen_months: set[int] = set()
                for month_idx, col_name in month_hits:
                    if month_idx in seen_months:
                        continue
                    seen_months.add(month_idx)
                    suggestions[col_name] = MONTH_NAMES[month_idx]
            elif all(c.startswith("col_") for c in cols):
                # Headerless table: first column is Year, then 12 months.
                suggestions[cols[0]] = "Year"
                start_idx = 1
                for i in range(12):
                    idx = start_idx + i
                    if idx < len(cols):
                        suggestions[cols[idx]] = MONTH_NAMES[i]
            elif year_idx is not None and (year_idx + 12) < len(cols):
                # Table with metadata columns before Year and 12 month columns after it.
                suggestions[cols[year_idx]] = "Year"
                start_idx = year_idx + 1
                for i in range(12):
                    idx = start_idx + i
                    if idx < len(cols):
                        suggestions[cols[idx]] = MONTH_NAMES[i]
            else:
                # Backward-compatible fallback.
                suggestions[cols[1]] = "Year"
                start_idx = 2
                for i in range(12):
                    idx = start_idx + i
                    if idx < len(cols):
                        suggestions[cols[idx]] = MONTH_NAMES[i]
        # generic: if columns named col_1.., suggest Year for first numeric-looking
        else:
            for i, c in enumerate(cols):
                if i == 0:
                    suggestions[c] = "Year"
                elif i <= 12:
                    suggestions[c] = MONTH_NAMES[(i-1) % 12]
                else:
                    suggestions[c] = c
        # apply suggestions (only for keys that exist)
        for k, v in suggestions.items():
            if k in self.rename_map and v:
                self.rename_map[k] = v
        QMessageBox.information(self, "Suggestions applied", "Quick suggestions were applied to rename map. Adjust names if needed.")

    def get_config(self) -> Dict[str, Any]:
        keep = [self.list_widget.item(i).text() for i in range(self.list_widget.count()) if self.list_widget.item(i).checkState() == Qt.Checked]
        rename = {old: self.rename_map.get(old, old) for old in keep}
        return {"keep": keep, "rename": rename}


# ---------------------------
# File import dialog (with Edit Columns, Skip rows, comments, Auto-detect)
# ---------------------------
class FileImportDialog(QDialog):
    """
    Dialog to preview a dataframe and allow mapping columns.
    Accepts either an already-read DataFrame (df) OR a file_path (with optional sheet_name).
    New controls:
      - Skip top rows (skiprows)
      - Treat lines starting with '#' as comments
      - Auto-detect layout (quick suggestion)
    Returns mappings including columns_config, has_header, skiprows, comment_char
    """
    def __init__(
        self,
        df: Optional[pd.DataFrame] = None,
        file_path: Optional[str] = None,
        sheet_name: Optional[str] = None,
        parent=None,
        batch_ctx: Optional[dict] = None,
    ):
        super().__init__(parent)
        self.resize(1050, 620)
        self.setMinimumSize(700, 420)

        self._orig_df = df.copy() if df is not None else None
        self._file_path = file_path
        self._sheet_name = sheet_name
        self._batch_ctx = batch_ctx
        self.columns_config: Optional[Dict[str, Any]] = None
        # Flag indicating whether the last auto-detect run found a clear layout
        self._auto_detect_ok: bool = False
        self._has_header = True  # default
        self._skiprows = 0
        self._comment = "#"  # default comment char; user can disable
        self._treat_comment = True
        self._auto_prompt_done: bool = False
        self._last_preview_error: str = ""

        # Try to load preview with sensible defaults (header if present)
        self._preview_df = None
        self._load_initial_preview()
        self._update_dialog_title()

        # Build UI inside a scroll area so the dialog can be reduced safely.
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(6)

        self.lbl_current_source = QLabel()
        self.lbl_current_source.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_current_source.setToolTip(self._build_dialog_source_text())
        root_layout.addWidget(self.lbl_current_source)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root_layout.addWidget(self._scroll_area)

        content = QWidget()
        layout = QVBoxLayout(content)
        htop = QHBoxLayout()

        # Left: preview
        self.preview_table = QTableView()
        self.preview_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_table.setModel(PandasModel(self._preview_df.head(100)))
        htop.addWidget(self.preview_table, 3)

        # Right: controls
        ctrl = QWidget()
        ctrl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        ctrl_layout = QVBoxLayout()

        ctrl_layout.addWidget(QLabel("Data layout (choose how months/values are arranged):"))
        self.radio_group = QButtonGroup(self)
        self.radio_wide = QRadioButton("Year + 12 month columns (wide)")
        self.radio_stacked = QRadioButton("Year + Month + Value (stacked monthly)")
        self.radio_multi = QRadioButton("Year + multiple series columns (chronologies or climate series)")
        self.radio_date_monthly = QRadioButton("Date (YYYY-MM or YYYY-MM-DD) + Value (monthly)")
        self.radio_date_monthly.setToolTip(
            "Use this for files with a single date column and one monthly value column. "
            "The importer will extract Year and Month from the date and build 12 month columns."
        )
        self.radio_wide.setChecked(True)
        self.radio_group.addButton(self.radio_wide)
        self.radio_group.addButton(self.radio_stacked)
        self.radio_group.addButton(self.radio_multi)
        self.radio_group.addButton(self.radio_date_monthly)
        ctrl_layout.addWidget(self.radio_wide)
        ctrl_layout.addWidget(self.radio_stacked)
        ctrl_layout.addWidget(self.radio_multi)
        ctrl_layout.addWidget(self.radio_date_monthly)

        # Climate variable type (used only for climate datasets)
        ctrl_layout.addWidget(QLabel("Climate variable type (for climate datasets):"))
        self.combo_climate_param = QComboBox()
        self.combo_climate_param.addItems(
            [
                "Temperature (mean)",
                "Temperature (min)",
                "Temperature (max)",
                "Temperature",          # legacy projects
                "Precipitation",
                "Runoff",
                "Soil moisture",
                "Snow",
                "PDSI",
                "Relative humidity",
                "Sea-level pressure",
                "Wind speed",
                "Radiation",
                "Diurnal temperature range",
                "Vapour pressure",
                "Cloud cover",
                "PET",
                "Wet days",
                "Frost days",
                "SPEI",
                "SPI",
                "Climatic water deficit",
                "Actual evapotranspiration",
                "Vapour pressure deficit",
                "Climate moisture index",
                "Other",
            ]
        )

        # Try to pre-select parameter: batch manifest, metadata comments, then file name.
        _bm = batch_metadata_for_file(self._file_path)
        guess = (_bm or {}).get("climate_param") or None
        if not guess:
            guess = guess_climate_param_from_metadata(self._file_path)
        if not guess:
            guess = guess_climate_param_from_name(self._file_path or "", "")
        if guess:
            idx = self.combo_climate_param.findText(guess)
            if idx >= 0:
                self.combo_climate_param.setCurrentIndex(idx)

        ctrl_layout.addWidget(self.combo_climate_param)

        # Year selection
        ctrl_layout.addWidget(QLabel("Select Year column:"))
        self.combo_year = QComboBox()
        self.combo_year.addItems(list(self._preview_df.columns.astype(str)))
        self.combo_year.setToolTip("Choose the column that contains year values (e.g., 1980...)")
        ctrl_layout.addWidget(self.combo_year)

        # wide months selection
        ctrl_layout.addWidget(QLabel("If wide: select 12 month columns (in order):"))
        self.list_months = QListWidget()
        self.list_months.addItems(list(self._preview_df.columns.astype(str)))
        self.list_months.setSelectionMode(QAbstractItemView.MultiSelection)
        ctrl_layout.addWidget(self.list_months, 1)

        # stacked selection
        ctrl_layout.addWidget(QLabel("If stacked: select Month column and Value column:"))
        self.combo_month_col = QComboBox()
        self.combo_month_col.addItems(list(self._preview_df.columns.astype(str)))
        self.combo_value_col = QComboBox()
        self.combo_value_col.addItems(list(self._preview_df.columns.astype(str)))
        ctrl_layout.addWidget(self.combo_month_col)
        ctrl_layout.addWidget(self.combo_value_col)

        # date-based monthly selection
        ctrl_layout.addWidget(QLabel("If date-based monthly: select Date and Value columns:"))
        h_date = QHBoxLayout()
        self.combo_date_col = QComboBox()
        self.combo_date_col.addItems(list(self._preview_df.columns.astype(str)))
        self.combo_value_date = QComboBox()
        self.combo_value_date.addItems(list(self._preview_df.columns.astype(str)))
        h_date.addWidget(QLabel("Date column:"))
        h_date.addWidget(self.combo_date_col)
        h_date.addWidget(QLabel("Value column:"))
        h_date.addWidget(self.combo_value_date)
        ctrl_layout.addLayout(h_date)

        # multi selection
        ctrl_layout.addWidget(QLabel("If multiple series: select value columns (Ctrl+click to multi-select):"))
        self.list_values_multi = QListWidget()
        self.list_values_multi.addItems(list(self._preview_df.columns.astype(str)))
        self.list_values_multi.setSelectionMode(QAbstractItemView.MultiSelection)
        ctrl_layout.addWidget(self.list_values_multi, 1)

        # single series option
        sing = QHBoxLayout()
        self.chk_use_single = QCheckBox("Import only a single selected series as chronology")
        sing.addWidget(self.chk_use_single)
        self.combo_single_value = QComboBox()
        self.combo_single_value.addItems(list(self._preview_df.columns.astype(str)))
        sing.addWidget(self.combo_single_value)
        ctrl_layout.addLayout(sing)

        # decimal separator
        ctrl_layout.addWidget(QLabel("Decimal separator handling:"))
        self.combo_dec = QComboBox()
        self.combo_dec.addItems(["Auto", "Dot (.)", "Comma (,)"])
        self.combo_dec.setToolTip(
            "If numeric values use a comma as decimal separator (e.g., 1,23), choose 'Comma (,)' "
            "so numbers are parsed correctly."
        )
        ctrl_layout.addWidget(self.combo_dec)

        # header + skiprows + comments
        form = QFormLayout()
        self.chk_has_header = QCheckBox("File has header row (first row contains column names)")
        self.chk_has_header.setChecked(self._has_header)
        self.chk_has_header.toggled.connect(self.on_has_header_toggled)
        form.addRow(self.chk_has_header)

        self.spin_skiprows = QSpinBox()
        self.spin_skiprows.setRange(0, 2000)
        self.spin_skiprows.setValue(self._skiprows)
        self.spin_skiprows.setToolTip("Number of top rows to skip before the table (metadata).")
        self.spin_skiprows.valueChanged.connect(self.on_skiprows_changed)
        form.addRow("Skip top rows:", self.spin_skiprows)

        self.chk_comment = QCheckBox("Treat lines starting with '#' as comments")
        self.chk_comment.setChecked(self._treat_comment)
        self.chk_comment.toggled.connect(self.on_comment_toggled)
        form.addRow(self.chk_comment)

        ctrl_layout.addLayout(form)

        # edit columns
        btn_edit_cols = QPushButton("Edit / Filter Columns")
        btn_edit_cols.clicked.connect(self.open_edit_columns)
        ctrl_layout.addWidget(btn_edit_cols)

        # Auto-detect button
        # Auto-detect button
        btn_auto = QPushButton("Auto-detect layout & suggestions")
        btn_auto.clicked.connect(self.auto_detect_layout)
        ctrl_layout.addWidget(btn_auto)

        # Auto-detect and immediately apply rename suggestions
        btn_auto_rename = QPushButton("Auto-detect and apply column names")
        btn_auto_rename.setToolTip(
            "Run auto-detect and, if successful, apply quick rename suggestions "
            "to columns without opening the Edit/Filter dialog."
        )
        btn_auto_rename.clicked.connect(self.auto_detect_and_rename)
        ctrl_layout.addWidget(btn_auto_rename)

        help_btn = QPushButton("Help")
        help_btn.clicked.connect(self.show_help)
        ctrl_layout.addWidget(help_btn)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        ctrl_layout.addWidget(btns)

        ctrl.setLayout(ctrl_layout)
        htop.addWidget(ctrl, 2)

        layout.addLayout(htop)

        tips = QTextEdit()
        tips.setReadOnly(True)
        tips.setHtml(
            "<b>Quick tips:</b><br>"
            "- If your file has no header, uncheck 'File has header row' and use Edit/Filter Columns to set column names.<br>"
            "- Use Skip top rows to remove metadata blocks above the table.<br>"
            "- If file contains '#' comments (like many climate outputs), check the comment box to ignore them.<br>"
            "- Auto-detect tries to propose Year/Month column mapping and rename suggestions."
        )
        layout.addWidget(tips)
        self._scroll_area.setWidget(content)
        self._update_dialog_source_label()
        QTimer.singleShot(0, self._prompt_after_auto_detect)

    def _build_dialog_source_text(self) -> str:
        """Return full source text for the current previewed file/sheet."""
        if self._file_path:
            full_path = os.path.abspath(self._file_path)
            if self._sheet_name:
                return f"{full_path}  |  sheet: {self._sheet_name}"
            return full_path
        if self._sheet_name:
            return f"<in-memory dataframe>  |  sheet: {self._sheet_name}"
        return "<in-memory dataframe>"

    def _update_dialog_title(self) -> None:
        """Show current file name in the dialog title."""
        base_title = "File Import - Column mapping"
        if self._file_path:
            file_name = os.path.basename(self._file_path)
            if self._sheet_name:
                self.setWindowTitle(f"{base_title} - {file_name} [{self._sheet_name}]")
            else:
                self.setWindowTitle(f"{base_title} - {file_name}")
        else:
            self.setWindowTitle(base_title)

    def _update_dialog_source_label(self) -> None:
        """Show full path with middle elision so the file name always stays visible."""
        if not hasattr(self, "lbl_current_source"):
            return
        full_text = self._build_dialog_source_text()
        self.lbl_current_source.setToolTip(full_text)

        fm = QFontMetrics(self.lbl_current_source.font())
        avail = max(180, self.lbl_current_source.width() - 8)
        elided = fm.elidedText(full_text, Qt.TextElideMode.ElideMiddle, avail)
        self.lbl_current_source.setText(f"Current file: {elided}")

    def _build_auto_detect_signature(self) -> Tuple[Any, ...]:
        """Build a compact signature of the currently auto-detected file layout."""
        cols = list(self._preview_df.columns.astype(str)) if self._preview_df is not None else []
        selected_months = [it.text() for it in self.list_months.selectedItems()]
        if self.radio_date_monthly.isChecked():
            layout = "date_monthly"
        elif self.radio_stacked.isChecked():
            layout = "stacked"
        elif self.radio_multi.isChecked():
            layout = "multi"
        else:
            layout = "wide"
        return (
            layout,
            bool(self._has_header),
            int(len(cols)),
            tuple(cols[:12]),
            self.combo_year.currentText(),
            self.combo_month_col.currentText(),
            self.combo_value_col.currentText(),
            self.combo_date_col.currentText(),
            self.combo_value_date.currentText(),
            tuple(selected_months[:12]),
        )

    def resizeEvent(self, event) -> None:
        """Keep the current-file label elided correctly when the dialog is resized."""
        super().resizeEvent(event)
        self._update_dialog_source_label()

    def _prompt_after_auto_detect(self) -> None:
        """Run auto-detect once after the dialog appears and ask whether manual review is needed."""
        if self._auto_prompt_done:
            return
        self._auto_prompt_done = True
        if self._preview_df is None or self._preview_df.empty:
            return

        self.auto_detect_layout(show_message=False)
        cached_pref = None
        cached_sig = None
        if isinstance(self._batch_ctx, dict):
            cached_pref = self._batch_ctx.get("auto_detect_review_pref", None)
            cached_sig = self._batch_ctx.get("auto_detect_signature", None)

        current_sig = self._build_auto_detect_signature() if getattr(self, "_auto_detect_ok", False) else None

        if cached_pref is False:
            if current_sig is not None and cached_sig is not None and current_sig == cached_sig:
                self.accept()
                return

            if current_sig is None:
                res = QMessageBox.question(
                    self,
                    "Auto-detect",
                    "This file does not match the previously auto-accepted layout confidently.\n\n"
                    "The program could not identify it reliably.\n\n"
                    "Do you want to review it manually?\n\n"
                    "Yes = open manual import for this file\n"
                    "No = skip this file",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if res == QMessageBox.StandardButton.No:
                    if isinstance(self._batch_ctx, dict) and self._file_path:
                        self._batch_ctx.setdefault("skipped_unrecognized", [])
                        self._batch_ctx["skipped_unrecognized"].append(self._file_path)
                    self.reject()
                elif isinstance(self._batch_ctx, dict):
                    self._batch_ctx["auto_detect_review_pref"] = True
                    self._batch_ctx["reuse_mapping_by_ext"] = False
                    self._batch_ctx["auto_detect_signature"] = None
                    self._batch_ctx["mapping_cache_by_ext"] = {}
                return

            res = QMessageBox.question(
                self,
                "Auto-detect",
                "This file does not look identical to the previously auto-accepted files.\n\n"
                "Do you want to review it manually?\n\n"
                    "Yes = keep this dialog open for manual review\n"
                    "No = continue with the detected settings",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if isinstance(self._batch_ctx, dict):
                if res == QMessageBox.StandardButton.No:
                    self._batch_ctx["auto_detect_review_pref"] = False
                    self._batch_ctx["auto_detect_signature"] = current_sig
                    self._batch_ctx["reuse_mapping_by_ext"] = True
                else:
                    self._batch_ctx["auto_detect_review_pref"] = True
                    self._batch_ctx["reuse_mapping_by_ext"] = False
                    self._batch_ctx["auto_detect_signature"] = None
                    self._batch_ctx["mapping_cache_by_ext"] = {}
            if res == QMessageBox.StandardButton.No:
                self.accept()
            return

        if not getattr(self, "_auto_detect_ok", False):
            return

        res = QMessageBox.question(
            self,
            "Auto-detect",
            "A likely file layout was detected and controls were prefilled.\n\n"
            "Do you want to review the import manually?\n\n"
            "Yes = keep this dialog open for manual review\n"
            "No = continue with the detected settings",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if isinstance(self._batch_ctx, dict):
            if res == QMessageBox.StandardButton.No:
                self._batch_ctx["auto_detect_review_pref"] = False
                self._batch_ctx["auto_detect_signature"] = current_sig
                self._batch_ctx["reuse_mapping_by_ext"] = True
            else:
                self._batch_ctx["auto_detect_review_pref"] = True
                self._batch_ctx["reuse_mapping_by_ext"] = False
                self._batch_ctx["auto_detect_signature"] = None
                self._batch_ctx["mapping_cache_by_ext"] = {}
        if res == QMessageBox.StandardButton.No:
            self.accept()

    def _rebuild_column_widgets_from_preview(self) -> None:
        """Refresh combo boxes and month list based on current preview dataframe."""
        df = getattr(self, "_preview_df", None)
        if df is None:
            cols: List[str] = []
        else:
            cols = list(df.columns.astype(str))

        # Avoid signal storms while repopulating widgets
        self.combo_year.blockSignals(True)
        self.combo_single_value.blockSignals(True)
        self.list_months.blockSignals(True)

        # Year combo
        self.combo_year.clear()
        self.combo_year.addItem("")  # optional empty
        for c in cols:
            self.combo_year.addItem(c)

        # Single-value combo (for 2-column series)
        self.combo_single_value.clear()
        self.combo_single_value.addItem("")
        for c in cols:
            self.combo_single_value.addItem(c)

        # Month list (we just show all columns; user selects which are months)
        self.list_months.clear()
        for c in cols:
            item = QListWidgetItem(c)
            self.list_months.addItem(item)

        self.combo_year.blockSignals(False)
        self.combo_single_value.blockSignals(False)
        self.list_months.blockSignals(False)


    # -----------------------
    # Loading & UI helpers
    # -----------------------
    def _load_initial_preview(self):
        """Read a small preview and try to guess if the file has a header.

        For text files we auto-detect: if the first data row is fully numeric,
        we assume there is no header and re-read with header=None so that all
        rows become data. We also drop completely empty columns that may appear
        because of irregular spacing.
        """
        self._last_preview_error = ""

        def _row_is_all_numeric(row: pd.Series) -> bool:
            """Return True if all non-empty cells in the row look numeric."""
            has_nonempty = False
            for v in row:
                if pd.isna(v):
                    continue
                s = str(v).strip()
                if not s:
                    continue
                has_nonempty = True
                s = s.replace(",", ".")
                try:
                    float(s)
                except Exception:
                    return False
            return has_nonempty

        try:
            if self._file_path is not None:
                # Special case: runoff.prn is a fixed-width file with missing months.
                # Use read_fwf so that empty month slots become NaN columns instead of disappearing.
                if self._file_path.lower().endswith(".prn"):
                    df0 = pd.read_fwf(
                        self._file_path,
                        header=None,
                    )
                    df0 = _drop_empty_columns(df0)
                    df0.columns = [f"col_{i+1}" for i in range(len(df0.columns))]
                    self._preview_df = df0
                    self._has_header = False
                    return

                # Excel: use an explicit engine so pandas does not guess incorrectly
                if self._file_path.lower().endswith((".xls", ".xlsx")):

                    df0 = _read_excel_robust(
                        self._file_path,
                        sheet_name=self._sheet_name,
                        header=0,
                    )
                    df0 = _drop_empty_columns(df0)
                    self._preview_df = df0
                    self._has_header = True
                else:
                    # Text files: detect separator and read with header=0 first
                    sep = _guess_separator(self._file_path)
                    df0 = _read_csv_robust(
                        path=self._file_path,
                        sep=sep,
                        header=0,
                        skiprows=0,
                        comment="#",
                    )
                    # Drop obviously empty columns caused by extra spaces
                    df0 = _drop_empty_columns(df0)

                    # Check if the first DATA row is fully numeric
                    if not df0.empty and _row_is_all_numeric(df0.iloc[0]):
                        # Re-read without header: every row is data
                        df0 = _read_csv_robust(
                            path=self._file_path,
                            sep=sep,
                            header=None,
                            skiprows=0,
                            comment="#",
                        )
                        df0 = _drop_empty_columns(df0)
                        df0.columns = [f"col_{i+1}" for i in range(len(df0.columns))]
                        self._has_header = False
                    else:
                        # First row is not purely numeric -> keep it as header
                        self._has_header = True

                    self._preview_df = df0

            elif self._orig_df is not None:
                df0 = self._orig_df.copy()
                df0 = _drop_empty_columns(df0)
                self._preview_df = df0
                self._has_header = True
            else:
                self._preview_df = pd.DataFrame()
        except Exception as exc_first:
            # Fallback: try reading without header
            try:
                if self._file_path is not None:
                    if self._file_path.lower().endswith((".xls", ".xlsx")):
                        df0 = _read_excel_robust(
                            self._file_path,
                            sheet_name=self._sheet_name,
                            header=None,
                        )
                    else:
                        sep = _guess_separator(self._file_path)
                        df0 = pd.read_csv(
                            self._file_path,
                            sep=sep,
                            header=None,
                            comment="#",
                            engine="python",
                            encoding="latin1",
                        )
                    df0 = _drop_empty_columns(df0)
                    df0.columns = [f"col_{i+1}" for i in range(len(df0.columns))]
                    self._preview_df = df0
                    self._has_header = False
                else:
                    self._preview_df = (
                        self._orig_df.copy()
                        if self._orig_df is not None
                        else pd.DataFrame()
                    )
            except Exception as exc_second:
                self._last_preview_error = (
                    "Initial preview read failed.\n\n"
                    f"First error:\n{exc_first}\n\n"
                    f"Fallback error:\n{exc_second}\n\n"
                    f"Traceback:\n{traceback.format_exc()}"
                )
                self._preview_df = pd.DataFrame()
                if self._file_path:
                    QMessageBox.critical(
                        self,
                        "Preview read error",
                        f"Failed to read preview for:\n{self._file_path}\n\n{self._last_preview_error}",
                    )

    def on_skiprows_changed(self, v: int):
        self._skiprows = int(v)
        # re-read preview
        self._refresh_preview()

    def on_comment_toggled(self, checked: bool):
        self._treat_comment = bool(checked)
        self._refresh_preview()

    def on_has_header_toggled(self, checked: bool):
        self._has_header = bool(checked)
        self._refresh_preview()

    def _refresh_preview(self):
        # re-read the file with current skiprows/comment/header settings
        self._last_preview_error = ""
        try:
            if self._file_path is None and self._orig_df is not None:
                # transform original df accordingly
                df = self._orig_df.copy()
                # if user turned off header, move header row into first data row
                if not self._has_header:
                    header = list(df.columns.astype(str))
                    df.columns = [f"col_{i+1}" for i in range(len(header))]
                    header_series = pd.Series(header, index=df.columns)
                    df = pd.concat([pd.DataFrame([header_series]), df], ignore_index=True)
                self._preview_df = df
            else:
                if self._file_path.lower().endswith((".xls", ".xlsx")):
                    header_arg = 0 if self._has_header else None
                    df_new = _read_excel_robust(
                        self._file_path,
                        sheet_name=self._sheet_name,
                        header=header_arg,
                        skiprows=self._skiprows,
                    )
                    df_new = _drop_empty_columns(df_new)
                else:
                    comment = "#" if self._treat_comment else None
                    # Special case: runoff.prn – fixed-width with possible leading empty months.
                    if self._file_path.lower().endswith(".prn"):
                        df_new = pd.read_fwf(
                            self._file_path,
                            header=None,
                            skiprows=self._skiprows,
                        )
                        df_new = _drop_empty_columns(df_new)
                        df_new.columns = [
                            f"col_{i+1}" for i in range(len(df_new.columns))
                        ]
                        self._has_header = False
                    else:
                        header_arg = 0 if self._has_header else None
                        sep = _guess_separator(self._file_path)
                        try:
                            df_new = pd.read_csv(
                                self._file_path,
                                sep=sep,
                                engine="python",
                                header=header_arg,
                                skiprows=self._skiprows,
                                comment=comment,
                            )
                        except Exception:
                            df_new = pd.read_csv(
                                self._file_path,
                                sep=sep,
                                header=header_arg,
                                skiprows=self._skiprows,
                                comment=comment,
                            )
                        if header_arg is None:
                            new_names = [
                                f"col_{i+1}" for i in range(len(df_new.columns))
                            ]
                            df_new.columns = new_names
                        df_new = _drop_empty_columns(df_new)
                self._preview_df = df_new

        except Exception as exc:
            self._last_preview_error = (
                f"Preview refresh failed for:\n{self._file_path or '<in-memory dataframe>'}\n\n"
                f"{exc}\n\n"
                f"Traceback:\n{traceback.format_exc()}"
            )
            QMessageBox.critical(
                self,
                "Preview refresh error",
                self._last_preview_error,
            )
            return

        # update preview table and controls
        self.preview_table.setModel(PandasModel(self._preview_df.head(100)))
        cols = list(self._preview_df.columns.astype(str))
        for cb in (
            self.combo_year,
            self.combo_month_col,
            self.combo_value_col,
            self.combo_single_value,
            self.combo_date_col,
            self.combo_value_date,
        ):
            cb.clear()
            cb.addItems(cols)
        self.list_months.clear()
        self.list_months.addItems(cols)
        self.list_values_multi.clear()
        self.list_values_multi.addItems(cols)

    # -----------------------
    # Column editing & suggestions
    # -----------------------
    def open_edit_columns(self):
        cols = list(self._preview_df.columns.astype(str))
        # Attempt to create prefill suggestions before opening the dialog.
        prefill: Dict[str, str] = {}
        if len(cols) == 2:
            prefill = {cols[0]: "Year", cols[1]: "Chronology"}
        else:
            detected = _detect_monthly_wide_columns(self._preview_df)
            if detected is not None:
                prefill[str(detected["year_col"])] = "Year"
                for src, dst in zip(detected["month_cols"], MONTH_NAMES):
                    prefill[str(src)] = dst
            elif len(cols) >= 13:
                # Fallback: assume station metadata first, then Year, then 12 months.
                months_short = MONTH_NAMES[:]
                if len(cols) > 1:
                    prefill[cols[1]] = "Year"
                for i in range(12):
                    idx = 2 + i
                    if idx < len(cols):
                        prefill[cols[idx]] = months_short[i]

        dlg = ColumnEditDialog(cols, prefill_rename=prefill, parent=self)

        if dlg.exec() != QDialog.Accepted:
            return
        cfg = dlg.get_config()

        df_preview = self._preview_df.copy()  # <--- добавляем эту строку

        keep = cfg.get("keep", list(df_preview.columns))
        if keep:
            df_preview = df_preview[keep].copy()
        rename = cfg.get("rename", {})
        if rename:
            df_preview = df_preview.rename(columns=rename)
        # Update stored preview so auto-detect uses the new columns as well
        self._preview_df = df_preview
        self.preview_table.setModel(PandasModel(df_preview.head(100)))
        # update comboboxes with new names
        cols2 = list(df_preview.columns.astype(str))
        for cb in (
            self.combo_year,
            self.combo_month_col,
            self.combo_value_col,
            self.combo_single_value,
            self.combo_date_col,
            self.combo_value_date,
        ):
            cb.clear()
            cb.addItems(cols2)
        self.list_months.clear(); self.list_months.addItems(cols2)
        self.list_values_multi.clear(); self.list_values_multi.addItems(cols2)

    def auto_detect_layout(self, show_message: bool = True):
        """
        Try to detect common layouts and prefill dialog controls:
        - 2 columns, first looks like year -> multi + single series
        - >=13 columns with first Year + 12 months -> wide (Year in first column)
        - >=13 columns with second Year -> wide with station id (Year in second column)
        - stacked detection if there are 3 columns with month names etc.
        """
        show_message = bool(show_message) and not bool(
            getattr(self, "_suppress_auto_detect_messages", False)
        )
        # reset flag; will be set True only if a specific pattern is detected
        self._auto_detect_ok = False

        df = self._preview_df.copy()
        cols = list(df.columns.astype(str))
        # quick guard
        if df.empty:
            if show_message:
                QMessageBox.information(self, "Auto-detect", "Preview is empty - cannot auto-detect.")
            return

        # helper to check if a column looks like years
        def looks_like_years(series: pd.Series) -> bool:
            try:
                nums = pd.to_numeric(series.dropna(), errors='coerce')
                if nums.empty:
                    return False
                mn = nums.min(); mx = nums.max()
                return (mn >= 1800 and mx <= 2100) or (mn >= 100 and mx <= 3000 and len(nums)>=10)
            except Exception:
                return False

        def looks_like_month_headers(names: list[str]) -> bool:
            """
            Return True if the provided column names resemble 12 month columns.
            Accepts common cases: Jan..Dec, Russian short names, Roman numerals,
            and numeric 1..12 / 01..12.
            """
            if len(names) != 12:
                return False
            hits: set[int] = set()
            for n in names:
                canonical = _normalize_month_header_token(n)
                if canonical is not None:
                    hits.add(MONTH_NAMES.index(canonical))
                    continue
                s = str(n).strip()
                if s.isdigit():
                    try:
                        v = int(s)
                    except Exception:
                        v = -1
                    if 1 <= v <= 12:
                        hits.add(v - 1)
            return len(hits) == 12

        # 0) Explicit wide monthly table with year column not necessarily first
        explicit_wide = _detect_monthly_wide_columns(df)
        if explicit_wide is not None:
            self.radio_wide.setChecked(True)
            self.combo_year.setCurrentText(str(explicit_wide["year_col"]))
            selected_months = {str(c) for c in explicit_wide["month_cols"]}
            for i in range(self.list_months.count()):
                item = self.list_months.item(i)
                item.setSelected(item.text() in selected_months)
            if show_message:
                station_meta = explicit_wide.get("station_meta", {}) or {}
                if station_meta.get("name") or station_meta.get("code_wmo"):
                    QMessageBox.information(
                        self,
                        "Auto-detect",
                        "Detected combined station monthly layout with station metadata, "
                        "a Year column, and 12 month columns. Controls were prefilled.",
                    )
                else:
                    QMessageBox.information(
                        self,
                        "Auto-detect",
                        "Detected wide Year + 12 month columns layout. Controls were prefilled.",
                    )
            self._auto_detect_ok = True
            return

        def looks_like_numeric_month_block(frame: pd.DataFrame, names: list[str]) -> bool:
            """
            Return True if the provided 12 columns look like monthly numeric data,
            even when the headers are generic (for example col_1..col_12).
            """
            if len(names) != 12:
                return False
            good = 0
            min_non_na = max(3, int(len(frame) * 0.4))
            for n in names:
                if n not in frame.columns:
                    return False
                s = pd.to_numeric(frame[n], errors="coerce")
                if int(s.notna().sum()) >= min_non_na:
                    good += 1
            return good >= 10

        # 1) Two-column chronology: Year + value
        if len(cols) == 2:
            # detect year-like first column
            if looks_like_years(df[cols[0]]):
                self.radio_multi.setChecked(True)
                self.chk_use_single.setChecked(True)
                self.combo_single_value.setCurrentText(cols[1])
                self.combo_year.setCurrentText(cols[0])
                if show_message:
                    QMessageBox.information(
                        self,
                        "Auto-detect",
                        "Detected Year + single chronology. Prefilled controls and rename suggestions."
                    )
                self._auto_detect_ok = True  # auto-detect succeeded
                return

        # 2) Year in first column + 12 month columns immediately after
        if len(cols) >= 13 and looks_like_years(df[cols[0]]) and (
            looks_like_month_headers(cols[1:1+12]) or
            looks_like_numeric_month_block(df, cols[1:1+12])
        ):
            # assume wide layout Year + 12 months
            self.radio_wide.setChecked(True)
            self.combo_year.setCurrentText(cols[0])
            month_candidates = cols[1:1+12]
            for i in range(self.list_months.count()):
                item = self.list_months.item(i)
                item.setSelected(item.text() in month_candidates)
            if show_message:
                QMessageBox.information(
                    self,
                    "Auto-detect",
                    "Detected Year + 12 months layout (Year in first column). Prefilled controls.",
                )
            self._auto_detect_ok = True
            return

        # 3) wide with station id + year + 12 months (year in second column)
        if len(cols) >= 13:
            # check if second column is year-like
            if looks_like_years(df[cols[1]]) and (
                looks_like_month_headers(cols[2:2+12]) or
                looks_like_numeric_month_block(df, cols[2:2+12])
            ):
                # assume first column might be station id; set layout wide
                self.radio_wide.setChecked(True)
                self.combo_year.setCurrentText(cols[1])
                # set 12 months as cols[2:14] if present
                month_candidates = cols[2:2+12]
                for i in range(self.list_months.count()):
                    item = self.list_months.item(i)
                    if item.text() in month_candidates:
                        item.setSelected(True)
                    else:
                        item.setSelected(False)
                if show_message:
                    QMessageBox.information(self, "Auto-detect", "Detected StationID + Year + 12 months layout. Prefilled Year and months.")
                self._auto_detect_ok = True
                return

        # 3b) Metadata columns + Year + 12 month columns immediately after Year (Year can be any position)
        if len(cols) >= 13:
            year_idx = None
            for i, c in enumerate(cols):
                if str(c).strip().lower() == "year":
                    year_idx = i
                    break
            if year_idx is not None and (year_idx + 12) < len(cols):
                month_candidates = cols[year_idx + 1 : year_idx + 13]
                if looks_like_years(df[cols[year_idx]]) and (
                    looks_like_month_headers(month_candidates) or
                    looks_like_numeric_month_block(df, month_candidates)
                ):
                    self.radio_wide.setChecked(True)
                    self.combo_year.setCurrentText(cols[year_idx])
                    for i in range(self.list_months.count()):
                        item = self.list_months.item(i)
                        item.setSelected(item.text() in month_candidates)
                    if show_message:
                        QMessageBox.information(
                            self,
                            "Auto-detect",
                            "Detected metadata + Year + 12 months layout (months immediately after Year). Prefilled controls.",
                        )
                    self._auto_detect_ok = True
                    return

        # 3c) Headerless wide layout: generic columns, first column is Year,
        # and the next 12 columns are mostly numeric monthly values.
        if len(cols) >= 13 and looks_like_years(df[cols[0]]):
            month_candidates = cols[1:13]
            if looks_like_numeric_month_block(df, month_candidates):
                self.radio_wide.setChecked(True)
                self.combo_year.setCurrentText(cols[0])
                for i in range(self.list_months.count()):
                    item = self.list_months.item(i)
                    item.setSelected(item.text() in month_candidates)
                if show_message:
                    QMessageBox.information(
                        self,
                        "Auto-detect",
                        "Detected headerless Year + 12 numeric monthly columns. Prefilled controls.",
                    )
                self._auto_detect_ok = True
                return

        # 3d) Headerless station layout: generic first column, second column is Year,
        # and the next 12 columns are mostly numeric monthly values.
        if len(cols) >= 14 and looks_like_years(df[cols[1]]):
            month_candidates = cols[2:14]
            if looks_like_numeric_month_block(df, month_candidates):
                self.radio_wide.setChecked(True)
                self.combo_year.setCurrentText(cols[1])
                for i in range(self.list_months.count()):
                    item = self.list_months.item(i)
                    item.setSelected(item.text() in month_candidates)
                if show_message:
                    QMessageBox.information(
                        self,
                        "Auto-detect",
                        "Detected headerless station + Year + 12 numeric monthly columns. Prefilled controls.",
                    )
                self._auto_detect_ok = True
                return

        # stacked detection (3 columns year, month, value)
        if len(cols) >= 3:
            # try to see if first column looks like years, second small set of repeating month-like values
            if looks_like_years(df[cols[0]]):
                # check second column unique values count small <= 12 -> likely month column
                uniq = df[cols[1]].dropna().unique()
                if len(uniq) <= 12:
                    self.radio_stacked.setChecked(True)
                    self.combo_year.setCurrentText(cols[0])
                    self.combo_month_col.setCurrentText(cols[1])
                    self.combo_value_col.setCurrentText(cols[2])
                    if show_message:
                        QMessageBox.information(self, "Auto-detect", "Detected stacked Year/Month/Value layout. Prefilled controls.")
                    self._auto_detect_ok = True
                    return

        # 4) Date + single value (monthly) detection
        date_col_candidate = None
        value_col_candidate = None

        for c in cols:
            # Do not treat pure Year columns as dates (pandas would parse "2001" as 2001-01-01).
            try:
                if looks_like_years(df[c]):
                    continue
            except Exception:
                pass
            s = df[c].dropna()
            if s.empty:
                continue
            # Use string representation but guard against pandas internal errors
            try:
                parsed = pd.to_datetime(
                    s.astype(str),
                    errors="coerce",
                )
            except (TypeError, ValueError):
                # Some exotic dtypes (e.g. dict-like values) can cause
                # "cannot assemble with duplicate keys" inside pandas.
                # Treat such columns as non-date for auto-detection.
                continue

            ok_ratio = parsed.notna().mean()
            if ok_ratio > 0.8:
                date_col_candidate = c
                break

        if date_col_candidate is not None:
            for c in cols:
                if c == date_col_candidate:
                    continue
                s = pd.to_numeric(df[c], errors="coerce")
                # At least 12 numeric values -> likely a monthly time series
                if s.notna().sum() >= 12:
                    value_col_candidate = c
                    break

        if date_col_candidate is not None and value_col_candidate is not None:
            self.radio_date_monthly.setChecked(True)
            self.combo_date_col.setCurrentText(date_col_candidate)
            self.combo_value_date.setCurrentText(value_col_candidate)
            if show_message:
                QMessageBox.information(
                    self,
                    "Auto-detect",
                    "Detected date-based monthly layout. Using '{}' as Date and '{}' as Value.".format(
                        date_col_candidate, value_col_candidate
                    ),
                )
            self._auto_detect_ok = True
            return
        if show_message:
            QMessageBox.information(self, "Auto-detect", "No clear auto-detection result; please use Edit/Filter Columns to adjust manually.")

    def auto_detect_and_rename(self) -> None:
        """
        One-click helper for typical files.

        1) Run auto_detect_layout() to detect Year / Month / Value columns.
        2) Build a rename map based on the detected layout (Year + 12 months
           or simple 2-column chronology). If that is not enough, fall back
           to ColumnEditDialog quick suggestions.
        3) Apply renames to the preview dataframe and refresh widgets.
        4) Run auto_detect_layout() again on the renamed columns so that
           Year and month controls automatically point to the new names.

        If anything fails, the dialog state is restored to the previous
        working preview and stays open.
        """
        if self._preview_df is None or self._preview_df.empty:
            QMessageBox.information(
                self,
                "Auto-rename",
                "Preview is empty - nothing to rename.",
            )
            return

        # Backup last good state so we can roll back on error
        try:
            self._last_preview_df = self._preview_df.copy()
        except Exception:
            self._last_preview_df = self._preview_df
        self._last_columns_config = getattr(self, "columns_config", None)

        try:
            # 1) Detect layout: fills Year/month selections etc.
            self._suppress_auto_detect_messages = True
            try:
                self.auto_detect_layout(show_message=False)
            finally:
                self._suppress_auto_detect_messages = False
            if not getattr(self, "_auto_detect_ok", False):
                # Layout was not clearly detected - nothing further to do.
                QMessageBox.information(
                    self,
                    "Auto-rename",
                    "Auto-detect could not find a clear layout.\n"
                    "Please adjust layout or columns manually.",
                )
                return

            df_preview = self._preview_df.copy()
            cols = list(df_preview.columns.astype(str))

            rename: Dict[str, str] = {}
            keep: List[str] = cols

            # ---- Case A: simple 2-column chronology (Year + value) ----
            if self.radio_multi.isChecked() and len(cols) == 2:
                year_col = self.combo_year.currentText() or cols[0]
                value_col = self.combo_single_value.currentText() or cols[1]
                keep = [year_col, value_col]
                rename = {
                    year_col: "Year",
                    value_col: "Chronology",
                }

            # ---- Case B: wide layout Year + 12 months ----
            elif self.radio_wide.isChecked():
                year_col = self.combo_year.currentText()
                month_items = [item.text() for item in self.list_months.selectedItems()]
                keep = []

                keep = cols[:]
                if year_col:
                    rename[year_col] = "Year"

                if len(month_items) == 12:
                    month_labels = [
                        "Jan",
                        "Feb",
                        "Mar",
                        "Apr",
                        "May",
                        "Jun",
                        "Jul",
                        "Aug",
                        "Sep",
                        "Oct",
                        "Nov",
                        "Dec",
                    ]
                    for col_name, month_name in zip(month_items, month_labels):
                        rename[col_name] = month_name
                else:
                    # If months are not clearly selected, fall back to generic suggestions
                    rename = {}

            # ---- Fallback: use the same heuristics as ColumnEditDialog ----
            if not rename:
                dlg = ColumnEditDialog(cols, parent=self)
                dlg.apply_quick_suggestions()
                cfg = dlg.get_config()
                keep = cfg.get("keep", cols)
                rename = cfg.get("rename", {})

            cfg = {
                "keep": keep,
                "rename": rename,
            }
            # Remember config so it is used during the actual import
            self.columns_config = cfg

            # 3) Apply config to preview dataframe
            if keep:
                df_preview = df_preview[keep].copy()
            if rename:
                df_preview = df_preview.rename(columns=rename)

            # Store updated preview and refresh widgets
            self._preview_df = df_preview
            self.preview_table.setModel(PandasModel(df_preview.head(100)))
            self._rebuild_column_widgets_from_preview()

            # 4) Run auto-detect again on the renamed columns so controls
            #    point directly to 'Year', 'Jan'...'Dec', etc.
            self._suppress_auto_detect_messages = True
            try:
                self.auto_detect_layout(show_message=False)
            finally:
                self._suppress_auto_detect_messages = False

            QMessageBox.information(
                self,
                "Auto-rename",
                "Layout was detected, column names were applied, and controls were refreshed.\n"
                "You do not need to run the auto-detect button again manually.",
            )

        except Exception as exc:
            # Roll back to last good state, keep dialog open
            import traceback

            traceback.print_exc()
            if getattr(self, "_last_preview_df", None) is not None:
                self._preview_df = self._last_preview_df
            self.columns_config = getattr(self, "_last_columns_config", None)

            if self._preview_df is not None:
                self.preview_table.setModel(PandasModel(self._preview_df.head(100)))
                self._rebuild_column_widgets_from_preview()

            QMessageBox.critical(
                self,
                "Auto-rename error",
                "An unexpected error occurred while auto-detecting "
                "and renaming columns:\n"
                f"{exc}\n\n"
                "The import dialog has been restored to the last working state.",
            )

    def show_help(self):
        _show_html_help_dialog(self, "Help - Import", IMPORT_HELP_HTML, width=760, height=600)

    def show_help_about(self) -> None:
        """Compatibility wrapper for older Help/About actions."""
        self.show_help()

    def show_about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("About")
        dlg.resize(520, 260)
        lay = QVBoxLayout()
        text = QTextEdit()
        text.setReadOnly(True)
        text.setHtml(
            "<h2>RingClim Import Help</h2>"
            "<p>This dialog is used to map input columns before creating chronology, monthly climate, or time-series datasets.</p>"
            "<h3>Preview and source</h3>"
            "<ul>"
            "<li>The current file path is shown at the top of the dialog.</li>"
            "<li>The left preview shows the first rows after the current header/skip/comment settings are applied.</li>"
            "<li>If the preview is wrong, first adjust header and skipped rows before setting the column mapping.</li>"
            "</ul>"
            "<h3>Input cleanup</h3>"
            "<ul>"
            "<li><b>File has header row</b>: use when the first data line contains column names.</li>"
            "<li><b>Skip top rows</b>: remove metadata lines before the table.</li>"
            "<li><b>Treat lines starting with '#'</b>: ignore comment/header lines in text files.</li>"
            "<li><b>Edit / Filter Columns</b>: keep only needed columns and rename them before import.</li>"
            "<li><b>Auto-detect layout</b> tries to identify common structures and prefill the controls.</li>"
            "</ul>"
            "<h3>Layouts</h3>"
            "<ul>"
            "<li><b>Year + 12 month columns</b>: monthly wide climate tables.</li>"
            "<li><b>Year + Month + Value</b>: stacked monthly tables that will be pivoted to Jan..Dec.</li>"
            "<li><b>Date + Value (monthly)</b>: one date column and one value column for monthly data.</li>"
            "<li><b>Year + multiple series columns</b>: chronology or multiseries import.</li>"
            "</ul>"
            "<h3>Climate parameter type</h3>"
            "<ul>"
            "<li>The dialog tries to guess the variable type from metadata comments, file names, and known aliases.</li>"
            "<li>You can still override it manually for precipitation, runoff, soil moisture, snow, humidity, radiation, and temperature variants.</li>"
            "</ul>"
            "<h3>Batch import behavior</h3>"
            "<ul>"
            "<li>When importing many files, you can approve manual review file-by-file or auto-accept repeated identical layouts.</li>"
            "<li>If a later file does not match the previously accepted layout confidently, the dialog can ask again or skip that file.</li>"
            "</ul>"
        )
        lay.addWidget(text)
        ok = QPushButton("Close")
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok)
        dlg.setLayout(lay)
        dlg.exec()

    def get_mappings(self) -> Optional[Dict[str, Any]]:
        """Collect user choices from the import dialog.

        Returns:
            A mapping describing layout, column selections, header/skiprows,
            climate parameter, etc., or None if dialog was cancelled.
        """
        if self.exec() != QDialog.Accepted:
            return None

        if self._preview_df is None or self._preview_df.empty:
            msg = self._last_preview_error or "Preview is empty. The file could not be parsed."
            QMessageBox.critical(self, "Import error", msg)
            return None

        mappings: Dict[str, Any] = {}

        # ---- Layout ----
        if self.radio_wide.isChecked():
            layout = "wide"
        elif self.radio_stacked.isChecked():
            layout = "stacked"
        elif hasattr(self, "radio_date_monthly") and self.radio_date_monthly.isChecked():
            layout = "date_monthly"
        else:
            layout = "multi"

        mappings["layout"] = layout

        # ---- Common settings ----
        mappings["year_col"] = self.combo_year.currentText()
        mappings["dec_sep"] = self.combo_dec.currentText()
        mappings["columns_config"] = self.columns_config  # may be None
        mappings["has_header"] = bool(self.chk_has_header.isChecked())
        mappings["skiprows"] = int(self.spin_skiprows.value())
        mappings["comment"] = "#" if self.chk_comment.isChecked() else None
        # climate variable type (used later for Auto aggregation)
        if hasattr(self, "combo_climate_param"):
            mappings["climate_param"] = self.combo_climate_param.currentText()
        else:
            mappings["climate_param"] = "Other"

        # ---- Layout-specific settings ----
        if layout == "wide":
            # Year + 12 months
            mappings["month_cols"] = [item.text() for item in self.list_months.selectedItems()]
        elif layout == "stacked":
            # Year + Month + Value
            mappings["month_col"] = self.combo_month_col.currentText()
            mappings["value_col"] = self.combo_value_col.currentText()
        elif layout == "date_monthly":
            # Date (YYYY-MM or YYYY-MM-DD) + Value
            # NOTE: we put value into "value_col" so that _create_datasets_from_mapping
            # can use mappings.get("value_col") without changes.
            mappings["date_col"] = self.combo_date_col.currentText()
            mappings["value_col"] = self.combo_value_date.currentText()
        else:
            # multi-series: Year + multiple value columns
            mappings["value_cols"] = [item.text() for item in self.list_values_multi.selectedItems()]
            mappings["use_single"] = self.chk_use_single.isChecked()
            mappings["single_col"] = self.combo_single_value.currentText()

        return mappings

class LargeGridImportDialog(QDialog):
    """
    Dialog to import a large gridded climate file (e.g., CRU .dat.gz),
    interpolate it to a single point, optionally aggregate, preview and
    export before adding to the project.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import large gridded climate data")
        self.resize(700, 500)

        # Background thread/worker for full extraction
        self._thread: Optional[QThread] = None
        self._worker: Optional[LargeGridWorker] = None

        # Background thread/worker for preview (lightweight)
        self._preview_thread: Optional[QThread] = None
        self._preview_worker: Optional["LargeGridPreviewWorker"] = None

        self.result_df: Optional[pd.DataFrame] = None
        self.result_meta: dict = {}

        self._build_ui()

    # ---- UI ----
    def _build_ui(self) -> None:
        layout = QVBoxLayout()

        # --- File selection ---
        file_row = QHBoxLayout()
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("Path to NetCDF gridded file (*.dat.nc, *.dat.nc.gz, *.nc, *.nc.gz)")
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self.on_browse)
        file_row.addWidget(QLabel("File:"))
        file_row.addWidget(self.edit_path, 1)
        file_row.addWidget(btn_browse)
        layout.addLayout(file_row)

        # --- Target point and bounding box ---
        coord_group = QGroupBox("Target point and bounding box")
        coord_layout = QGridLayout()

        self.spin_lat = QDoubleSpinBox()
        self.spin_lat.setRange(-90.0, 90.0)
        self.spin_lat.setDecimals(3)
        self.spin_lat.setValue(0.0)

        self.spin_lon = QDoubleSpinBox()
        self.spin_lon.setRange(-180.0, 180.0)
        self.spin_lon.setDecimals(3)
        self.spin_lon.setValue(0.0)

        self.spin_min_lat = QDoubleSpinBox()
        self.spin_min_lat.setRange(-90.0, 90.0)
        self.spin_min_lat.setDecimals(3)
        self.spin_min_lat.setValue(-90.0)

        self.spin_max_lat = QDoubleSpinBox()
        self.spin_max_lat.setRange(-90.0, 90.0)
        self.spin_max_lat.setDecimals(3)
        self.spin_max_lat.setValue(90.0)

        self.spin_min_lon = QDoubleSpinBox()
        self.spin_min_lon.setRange(-180.0, 180.0)
        self.spin_min_lon.setDecimals(3)
        self.spin_min_lon.setValue(-180.0)

        self.spin_max_lon = QDoubleSpinBox()
        self.spin_max_lon.setRange(-180.0, 180.0)
        self.spin_max_lon.setDecimals(3)
        self.spin_max_lon.setValue(180.0)

        coord_layout.addWidget(QLabel("Target lat:"), 0, 0)
        coord_layout.addWidget(self.spin_lat, 0, 1)
        coord_layout.addWidget(QLabel("Target lon:"), 0, 2)
        coord_layout.addWidget(self.spin_lon, 0, 3)

        coord_layout.addWidget(QLabel("Min lat:"), 1, 0)
        coord_layout.addWidget(self.spin_min_lat, 1, 1)
        coord_layout.addWidget(QLabel("Max lat:"), 1, 2)
        coord_layout.addWidget(self.spin_max_lat, 1, 3)

        coord_layout.addWidget(QLabel("Min lon:"), 2, 0)
        coord_layout.addWidget(self.spin_min_lon, 2, 1)
        coord_layout.addWidget(QLabel("Max lon:"), 2, 2)
        coord_layout.addWidget(self.spin_max_lon, 2, 3)

        # чекбокс: использовать свою область или авто (±10° вокруг точки)
        self.chk_use_bbox = QCheckBox("Specify bounding box manually")
        self.chk_use_bbox.setChecked(False)

        self.chk_use_bbox.setToolTip(
            "If unchecked, the program interpolates the gridded field to the given "
            "latitude/longitude using bilinear interpolation from up to 4 nearest grid cells "
            "(auto bounding box about ±10° around the point).\n\n"
            "If checked, the program computes a latitude-weighted mean over all grid cells "
            "inside the specified bounding box."
        )
        

        def _toggle_bbox_enabled(checked: bool) -> None:
            for w in (
                self.spin_min_lat,
                self.spin_max_lat,
                self.spin_min_lon,
                self.spin_max_lon,
            ):
                w.setEnabled(checked)

        self.chk_use_bbox.toggled.connect(_toggle_bbox_enabled)
        # по умолчанию ручные границы отключены
        _toggle_bbox_enabled(False)

        coord_layout.addWidget(self.chk_use_bbox, 3, 0, 1, 4)

        coord_group.setLayout(coord_layout)
        layout.addWidget(coord_group)

        # --- Frequency and aggregation ---
        freq_group = QGroupBox("Frequency and aggregation")
        freq_layout = QHBoxLayout()

        self.combo_source_freq = QComboBox()
        self.combo_source_freq.addItems(["daily", "monthly", "hourly"])
        self.combo_source_freq.setCurrentText("monthly")

        self.combo_target_freq = QComboBox()
        self.combo_target_freq.addItems(["daily", "monthly"])
        self.combo_target_freq.setCurrentText("monthly")

        self.combo_agg = QComboBox()
        self.combo_agg.addItems(["mean", "sum"])
        self.combo_agg.setCurrentText("mean")

        freq_layout.addWidget(QLabel("Source frequency:"))
        freq_layout.addWidget(self.combo_source_freq)
        freq_layout.addWidget(QLabel("Target frequency:"))
        freq_layout.addWidget(self.combo_target_freq)
        freq_layout.addWidget(QLabel("Aggregation:"))
        freq_layout.addWidget(self.combo_agg)

        freq_group.setLayout(freq_layout)
        layout.addWidget(freq_group)

        # --- Climate parameter type (for analysis defaults) ---
        param_row = QHBoxLayout()
        self.combo_climate_param = QComboBox()
        self.combo_climate_param.addItems(list(CLIMATE_PARAM_DEFAULT_AGG.keys()))
        self.combo_climate_param.setCurrentText("Soil moisture")
        param_row.addWidget(QLabel("Climate parameter type:"))
        param_row.addWidget(self.combo_climate_param)
        layout.addLayout(param_row)
        # --- Available / selected period ---
        period_row = QHBoxLayout()
        self.label_period_info = QLabel("Available period: unknown")

        self.chk_limit_years = QCheckBox("Use only years:")
        self.spin_year_start = QSpinBox()
        self.spin_year_start.setRange(1800, 3000)
        self.spin_year_start.setValue(1901)

        self.spin_year_end = QSpinBox()
        self.spin_year_end.setRange(1800, 3000)
        self.spin_year_end.setValue(2020)

        # по умолчанию ограничение выключено
        self.spin_year_start.setEnabled(False)
        self.spin_year_end.setEnabled(False)

        def _toggle_year_limit(checked: bool) -> None:
            self.spin_year_start.setEnabled(checked)
            self.spin_year_end.setEnabled(checked)

        self.chk_limit_years.toggled.connect(_toggle_year_limit)

        period_row.addWidget(self.label_period_info)
        period_row.addSpacing(20)
        period_row.addWidget(self.chk_limit_years)
        period_row.addWidget(self.spin_year_start)
        period_row.addWidget(QLabel("to"))
        period_row.addWidget(self.spin_year_end)
        period_row.addStretch(1)

        layout.addLayout(period_row)

        # --- Progress and log ---
        self.progress = QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

        # --- Buttons: preview / run / export / add / close ---
        btn_row = QHBoxLayout()

        # Preview button: quick check and auto-detect settings
        self.btn_preview = QPushButton("Preview / auto-detect")
        self.btn_preview.setToolTip(
            "Open the file, inspect metadata and a short time-series preview, "
            "and auto-detect source frequency and climate parameter."
        )
        self.btn_preview.clicked.connect(self.on_preview)

        self.btn_run = PrimaryButton("Run extraction")
        self.btn_run.clicked.connect(self.on_run)

        self.btn_export = QPushButton("Export...")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.on_export)

        self.btn_add = QPushButton("Add to project")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self.on_add)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)

        btn_row.addWidget(self.btn_preview)
        btn_row.addWidget(self.btn_run)
        btn_row.addWidget(self.btn_export)
        btn_row.addWidget(self.btn_add)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)


        self.setLayout(layout)

        # обновляем авто-bbox, когда меняются координаты точки
        try:
            self.spin_lat.valueChanged.connect(lambda _v: self._update_auto_bbox())
            self.spin_lon.valueChanged.connect(lambda _v: self._update_auto_bbox())
            # один раз сразу проставим авто-границы
            self._update_auto_bbox()
        except Exception:
            pass

    def _update_auto_bbox(self) -> None:
        """
        Обновляет спинбоксы min/max lat/lon по схеме ±10°
        вокруг целевой точки, если ручной bbox не включён.
        """
        if getattr(self, "chk_use_bbox", None) is None:
            return
        if self.chk_use_bbox.isChecked():
            # если пользователь сам задаёт границы – не трогаем
            return

        lat = float(self.spin_lat.value())
        lon = float(self.spin_lon.value())
        d = 10.0

        min_lat = max(-90.0, lat - d)
        max_lat = min(90.0, lat + d)
        min_lon = lon - d
        max_lon = lon + d

        # ограничим по -180..180
        if min_lon < -180.0:
            min_lon = -180.0
        if max_lon > 180.0:
            max_lon = 180.0

        # аккуратно обновляем без лишних сигналов
        for spin, val in (
            (self.spin_min_lat, min_lat),
            (self.spin_max_lat, max_lat),
            (self.spin_min_lon, min_lon),
            (self.spin_max_lon, max_lon),
        ):
            spin.blockSignals(True)
            spin.setValue(val)
            spin.blockSignals(False)

    def _get_effective_bbox(self) -> tuple[float, float, float, float]:
        """
        Возвращает реальный bbox, который нужно передать в интерполяцию:
        либо ручной (если чекбокс включён), либо авто ±10° вокруг точки.
        """
        if self.chk_use_bbox.isChecked():
            return (
                float(self.spin_min_lat.value()),
                float(self.spin_max_lat.value()),
                float(self.spin_min_lon.value()),
                float(self.spin_max_lon.value()),
            )
        # авто режим
        self._update_auto_bbox()
        return (
            float(self.spin_min_lat.value()),
            float(self.spin_max_lat.value()),
            float(self.spin_min_lon.value()),
            float(self.spin_max_lon.value()),
        )

    # ---- Slots ----
    def on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select gridded climate file",
            "",
            "CRU NetCDF gridded files (*.dat.nc *.dat.nc.gz *.nc *.nc.gz);;All files (*)",
        )
        if path:
            self.edit_path.setText(path)

    def on_preview(self) -> None:
        """
        Quick preview of the gridded file at the selected point.

        - Uses extract_timeseries_from_gridded_file with no resampling
          (source_freq == target_freq),
        - Shows basic metadata and first rows of the extracted series,
        - Auto-detects source frequency and climate parameter and updates
          the corresponding combo boxes,
        - Detects available period (years) and pre-fills year controls.
        """
        path = self.edit_path.text().strip()
        if not path:
            QMessageBox.warning(self, "File", "Please select a file first.")
            return

        bbox = self._get_effective_bbox()
        use_bbox_mean = self.chk_use_bbox.isChecked()

        try:
            # Для предпросмотра не меняем частоту: берём как есть (monthly)
            ts, meta = extract_timeseries_from_gridded_file(
                path=path,
                target_lat=float(self.spin_lat.value()),
                target_lon=float(self.spin_lon.value()),
                bbox=bbox,
                source_freq="monthly",
                target_freq="monthly",
                agg="mean",
                use_bbox_mean=use_bbox_mean,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Preview error",
                f"Could not read file for preview:\n{exc}",
            )
            self._append_log(f"Preview error: {exc}")
            return

        if ts is None or ts.empty:
            QMessageBox.information(
                self,
                "Preview",
                "Time series extracted for preview is empty.",
            )
            return

        # --- частота ---
        if isinstance(ts.index, pd.DatetimeIndex):
            freq_guess = guess_time_frequency(ts.index)
            idx = self.combo_source_freq.findText(freq_guess)
            if idx >= 0:
                self.combo_source_freq.setCurrentIndex(idx)
        else:
            freq_guess = "unknown"

        # --- тип параметра ---
        var_name = meta.get("var_name", "")
        param_guess = guess_climate_param_from_name(path, var_name)
        if param_guess:
            idx = self.combo_climate_param.findText(param_guess)
            if idx >= 0:
                self.combo_climate_param.setCurrentIndex(idx)

        # --- период (годы) ---
        if isinstance(ts.index, pd.DatetimeIndex):
            min_year = int(ts.index.year.min())
            max_year = int(ts.index.year.max())
            self.label_period_info.setText(
                f"Available period: {min_year}–{max_year}"
            )

            # настроим диапазон и значения спинбоксов
            self.spin_year_start.blockSignals(True)
            self.spin_year_end.blockSignals(True)
            self.spin_year_start.setRange(min_year, max_year)
            self.spin_year_end.setRange(min_year, max_year)
            self.spin_year_start.setValue(min_year)
            self.spin_year_end.setValue(max_year)
            self.spin_year_start.blockSignals(False)
            self.spin_year_end.blockSignals(False)
        else:
            self.label_period_info.setText("Available period: unknown")

        # --- вывод в лог ---
        self.log.clear()
        self._append_log(f"File: {meta.get('source_path', path)}")
        self._append_log(f"Variable: {var_name}")
        self._append_log(
            f"Time range: {meta.get('time_start')} .. {meta.get('time_end')}"
        )
        self._append_log(f"Detected source frequency: {freq_guess}")
        self._append_log(
            f"Detected period: {self.label_period_info.text()}"
        )

        # interpolation / spatial info
        interp_method = meta.get("interp_method", "unknown")
        n_cells_used = meta.get("n_cells_used", "?")
        n_cells_bbox = meta.get("n_cells_bbox", "?")
        use_bbox_mean = meta.get("use_bbox_mean", False)
        bbox_meta = meta.get("bbox")
        if isinstance(bbox_meta, (tuple, list)) and len(bbox_meta) == 4:
            bbox_str = (
                f"lat {bbox_meta[0]}..{bbox_meta[1]}, "
                f"lon {bbox_meta[2]}..{bbox_meta[3]}"
            )
        else:
            bbox_str = "unknown"

        self._append_log(
            "Interpolation: {method} (used cells: {used}, "
            "cells in bbox: {bbox_cells}, bbox mean: {bbox_mean})".format(
                method=interp_method,
                used=n_cells_used,
                bbox_cells=n_cells_bbox,
                bbox_mean=use_bbox_mean,
            )
        )
        self._append_log(f"Bounding box: {bbox_str}")

        self._append_log("")
        self._append_log("Preview of extracted series (first 10 rows):")
        self._append_log(ts.head(10).to_string())



    def _start_thread(self, worker: LargeGridWorker) -> None:
        self._thread = QThread(self)
        self._worker = worker
        worker.moveToThread(self._thread)

        self._thread.started.connect(worker.run)
        worker.finished.connect(self._on_worker_finished)
        worker.failed.connect(self._on_worker_failed)
        worker.progress.connect(self._append_log)

        worker.finished.connect(self._thread.quit)
        worker.failed.connect(self._thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _start_preview_thread(self, worker: "LargeGridPreviewWorker") -> None:
        """Start background thread for preview worker."""
        self._preview_thread = QThread(self)
        self._preview_worker = worker
        worker.moveToThread(self._preview_thread)

        self._preview_thread.started.connect(worker.run)
        worker.finished.connect(self._on_preview_finished)
        worker.failed.connect(self._on_preview_failed)
        worker.progress.connect(self._append_log)

        worker.finished.connect(self._preview_thread.quit)
        worker.failed.connect(self._preview_thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._preview_thread.finished.connect(self._on_preview_thread_finished)

        self._preview_thread.start()

    def _on_preview_finished(self, ts: pd.Series, meta: dict) -> None:
        """Handle successful preview completion."""
        # restore progress bar
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.btn_preview.setEnabled(True)
        self.btn_run.setEnabled(True)

        if ts is None or ts.empty:
            QMessageBox.information(
                self,
                "Preview",
                "Time series extracted for preview is empty.",
            )
            return

        # Auto-detect time frequency from the series index
        if isinstance(ts.index, pd.DatetimeIndex):
            freq_guess = guess_time_frequency(ts.index)
        else:
            freq_guess = "monthly"

        idx = self.combo_source_freq.findText(freq_guess)
        if idx >= 0:
            self.combo_source_freq.setCurrentIndex(idx)

        # Auto-detect climate parameter from file/variable name
        path = meta.get("source_path", self.edit_path.text().strip())
        var_name = meta.get("var_name", "")
        param_guess = guess_climate_param_from_name(path, var_name)
        if param_guess:
            idx = self.combo_climate_param.findText(param_guess)
            if idx >= 0:
                self.combo_climate_param.setCurrentIndex(idx)

        # Log basic info + first rows
        self.log.clear()
        self._append_log(f"File: {path}")
        self._append_log(f"Variable: {var_name}")
        self._append_log(
            f"Time range: {meta.get('time_start')} .. {meta.get('time_end')}"
        )
        self._append_log(f"Detected source frequency: {freq_guess}")
        self._append_log("")
        self._append_log("Preview of extracted series (first 10 rows):")
        self._append_log(ts.head(10).to_string())

    def _on_preview_failed(self, msg: str) -> None:
        """Handle errors during preview."""
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.btn_preview.setEnabled(True)
        self.btn_run.setEnabled(True)
        QMessageBox.critical(self, "Preview error", msg)
        self._append_log(msg)

    def _on_preview_thread_finished(self) -> None:
        """Cleanup after preview thread exits."""
        self._preview_thread = None
        self._preview_worker = None


    def on_run(self) -> None:
        if self._thread is not None:
            QMessageBox.information(
                self,
                "Busy",
                "Extraction is already running.",
            )
            return
        if self._preview_thread is not None:
            QMessageBox.information(
                self,
                "Busy",
                "Preview is running. Please wait until it finishes.",
            )
            return

        path = self.edit_path.text().strip()
        if not path:
            QMessageBox.warning(self, "File", "Please select a file first.")
            return

        bbox = self._get_effective_bbox()
        use_bbox_mean = self.chk_use_bbox.isChecked()

        year_start = None
        year_end = None
        if self.chk_limit_years.isChecked():
            year_start = int(self.spin_year_start.value())
            year_end = int(self.spin_year_end.value())

        worker = LargeGridWorker(
            path=path,
            target_lat=float(self.spin_lat.value()),
            target_lon=float(self.spin_lon.value()),
            bbox=bbox,
            source_freq=self.combo_source_freq.currentText(),
            target_freq=self.combo_target_freq.currentText(),
            agg=self.combo_agg.currentText(),
            climate_param=self.combo_climate_param.currentText(),
            year_start=year_start,
            year_end=year_end,
            use_bbox_mean=use_bbox_mean,
        )

        self.result_df = None
        self.result_meta = {}
        self.btn_run.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_add.setEnabled(False)
        self.progress.setRange(0, 0)  # busy indicator
        self.log.clear()
        self._append_log("Starting background extraction...")
        self._start_thread(worker)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _on_worker_finished(self, df: pd.DataFrame, meta: dict) -> None:
        self.result_df = df
        self.result_meta = meta
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.btn_export.setEnabled(True)
        self.btn_add.setEnabled(True)
        self._append_log(f"Finished. Rows: {len(df)}.")

        # log interpolation metadata if available
        interp_method = meta.get("interp_method")
        if interp_method:
            n_cells_used = meta.get("n_cells_used", "?")
            n_cells_bbox = meta.get("n_cells_bbox", "?")
            use_bbox_mean = meta.get("use_bbox_mean", False)
            self._append_log(
                "Interpolation method: {method} (used cells: {used}, "
                "cells in bbox: {bbox_cells}, bbox mean: {bbox_mean})".format(
                    method=interp_method,
                    used=n_cells_used,
                    bbox_cells=n_cells_bbox,
                    bbox_mean=use_bbox_mean,
                )
            )

    def _on_worker_failed(self, msg: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.btn_run.setEnabled(True)
        QMessageBox.critical(self, "Error", msg)
        self._append_log(msg)

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.btn_run.setEnabled(True)

    def on_export(self) -> None:
        if self.result_df is None or self.result_df.empty:
            QMessageBox.information(self, "Export", "No data to export.")
            return

        path, selected = QFileDialog.getSaveFileName(
            self,
            "Save extracted data",
            "extracted_climate.xlsx",
            "Excel files (*.xlsx);;CSV files (*.csv)",
        )
        if not path:
            return

        is_excel = path.lower().endswith(".xlsx") or "Excel" in selected
        meta = getattr(self, "result_meta", {}) or {}

        try:
            meta_info_path: Optional[str] = None

            if is_excel:
                if not path.lower().endswith(".xlsx"):
                    path = path + ".xlsx"
                # write data + metadata into separate sheets
                with pd.ExcelWriter(path) as writer:
                    self.result_df.to_excel(writer, sheet_name="data", index=False)
                    if meta:
                        meta_df = pd.DataFrame(
                            {
                                "key": list(meta.keys()),
                                "value": [str(v) for v in meta.values()],
                            }
                        )
                        meta_df.to_excel(writer, sheet_name="meta", index=False)
            else:
                if not path.lower().endswith(".csv"):
                    path = path + ".csv"
                self.result_df.to_csv(path, index=False)

                # for CSV export metadata to a separate text file
                if meta:
                    meta_path = os.path.splitext(path)[0] + "_meta.txt"
                    with open(meta_path, "w", encoding="utf-8") as fh:
                        for k, v in meta.items():
                            fh.write(f"{k}: {v}\n")
                    meta_info_path = meta_path

            msg_lines = [f"Data exported to:\n{path}"]
            if meta:
                if is_excel:
                    msg_lines.append("Metadata written to sheet 'meta'.")
                elif meta_info_path is not None:
                    msg_lines.append(f"Metadata exported to:\n{meta_info_path}")

            QMessageBox.information(
                self,
                "Export",
                "\n".join(msg_lines),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Export error",
                f"Failed to export data:\n{exc}",
            )

    def on_add(self) -> None:
        """
        Accept dialog so that caller can retrieve result_df and result_meta.
        """
        if self.result_df is None or self.result_df.empty:
            QMessageBox.warning(
                self,
                "Add to project",
                "No extracted data to add.",
            )
            return
        self.accept()

    def get_result(self) -> tuple[Optional[pd.DataFrame], dict]:
        """
        Execute dialog and return (df, meta) if user confirmed adding.
        """
        res = self.exec()
        if res != QDialog.Accepted or self.result_df is None:
            return None, {}
        return self.result_df, self.result_meta

# ---------------------------

 
# ---------------------------
# Prefill selection dialog (Detrend Advisor -> Dendro-Climate Analyzer)
# ---------------------------
class PrefillSelectionDialog(QDialog):
    def __init__(self, entries: List[Dict[str, Any]], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Import from Detrend Advisor")
        self._entries = entries
        self._selected_entries: List[Dict[str, Any]] = []

        layout = QVBoxLayout(self)

        info = QLabel(
            "Select detrending outputs to import.\n\n"
            "Chronologies are 2-column datasets (Year + value).\n"
            "Individual datasets are multi-column tables (Year + series columns)."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        layout.addWidget(self.tree)

        self.root_chron = QTreeWidgetItem(["Chronologies (2-column)"])
        self.root_ind = QTreeWidgetItem(["Individual series (multi-column)"])
        self.tree.addTopLevelItem(self.root_chron)
        self.tree.addTopLevelItem(self.root_ind)
        self.root_chron.setExpanded(True)
        self.root_ind.setExpanded(True)

        for e in self._entries:
            cat = str(e.get("category", "chronology")).lower()
            root = self.root_chron if cat == "chronology" else self.root_ind

            dataset_name = str(e.get("dataset_name") or e.get("dataset_key") or "Ungrouped").strip() or "Ungrouped"
            group_name = str(e.get("group") or "Other").strip() or "Other"
            method_name = str(e.get("method") or "").strip()
            variant_name = str(e.get("variant") or "").strip()

            # Create stable grouping nodes (not checkable): Dataset -> Method -> Items.
            if not hasattr(self, "_group_nodes"):
                self._group_nodes = {"chronology": {}, "individual": {}}

            root_key = "chronology" if cat == "chronology" else "individual"
            ds_nodes = self._group_nodes[root_key]

            ds_state = ds_nodes.get(dataset_name)
            if ds_state is None:
                ds_item = QTreeWidgetItem([dataset_name])
                ds_item.setExpanded(True)
                ds_state = {"item": ds_item, "methods": {}}
                ds_nodes[dataset_name] = ds_state
                root.addChild(ds_item)
            else:
                ds_item = ds_state["item"]

            method_label = method_name or group_name or "Other"
            method_nodes = ds_state["methods"]
            grp_item = method_nodes.get(method_label)
            if grp_item is None:
                grp_item = QTreeWidgetItem([method_label])
                grp_item.setExpanded(True)
                method_nodes[method_label] = grp_item
                if str(method_label).strip().upper() == "POWT":
                    ds_item.insertChild(0, grp_item)
                else:
                    ds_item.addChild(grp_item)

            if variant_name.upper() == "STD":
                label = "Chronology STD"
            elif variant_name.upper() == "RES":
                label = "Chronology RES"
            elif variant_name.upper() == "RWI":
                label = "RWI (all series)"
            elif variant_name.upper() == "POWT":
                label = "POWT"
            else:
                label = str(e.get("name", os.path.basename(str(e.get("path", "")))))
            item = QTreeWidgetItem([label])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked)
            item.setData(0, Qt.UserRole, e)
            grp_item.addChild(item)

        self.tree.expandAll()

        btn_row = QHBoxLayout()
        layout.addLayout(btn_row)

        btn_all = QPushButton("Select all")
        btn_all.clicked.connect(self._select_all)
        btn_row.addWidget(btn_all)

        btn_chron_only = QPushButton("Chronologies only")
        btn_chron_only.clicked.connect(self._chronologies_only)
        btn_row.addWidget(btn_chron_only)
        btn_std_only = QPushButton("STD only")
        btn_res_only = QPushButton("RES only")
        btn_std_only.clicked.connect(self._std_only)
        btn_res_only.clicked.connect(self._res_only)
        btn_row.addWidget(btn_std_only)
        btn_row.addWidget(btn_res_only)

        btn_powt_only = QPushButton("POWT only")
        btn_powt_only.clicked.connect(self._powt_only)
        btn_row.addWidget(btn_powt_only)

        btn_exclude_powt = QPushButton("Exclude POWT")
        btn_exclude_powt.clicked.connect(self._exclude_powt)
        btn_row.addWidget(btn_exclude_powt)

        btn_top3 = QPushButton("Top 3 recommended (per dataset)")
        btn_top3.clicked.connect(self._top3_per_group)
        btn_row.addWidget(btn_top3)


        btn_none = QPushButton("Select none")
        btn_none.clicked.connect(self._select_none)
        btn_row.addWidget(btn_none)

        btn_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _iter_leaf_items(self):
        """Yield only checkable leaf items, regardless of grouping depth."""

        def walk(node: QTreeWidgetItem):
            for i in range(node.childCount()):
                child = node.child(i)
                if child.childCount() == 0:
                    yield child
                else:
                    yield from walk(child)

        yield from walk(self.root_chron)
        yield from walk(self.root_ind)

    def _select_all(self):
        for it in self._iter_leaf_items():
            it.setCheckState(0, Qt.Checked)

    def _select_none(self):
        for it in self._iter_leaf_items():
            it.setCheckState(0, Qt.Unchecked)

    def _chronologies_only(self):
        for it in self._iter_leaf_items():
            e = it.data(0, Qt.UserRole) or {}
            cat = str(e.get("category", "chronology")).lower()
            it.setCheckState(0, Qt.Checked if cat == "chronology" else Qt.Unchecked)

    def _is_powt_entry(self, e: Dict[str, Any], it: QTreeWidgetItem) -> bool:
        """Return True when the entry belongs to the POWT export group."""
        group_name = str(e.get("group", "") or "").strip().upper()
        method_name = str(e.get("method", "") or "").strip().lower()
        variant_name = str(e.get("variant", "") or "").strip().upper()
        label = str(e.get("name", "") or it.text(0) or "").strip().upper()
        return (
            group_name == "POWT"
            or method_name == "powt"
            or variant_name == "POWT"
            or " POWT " in f" {label} "
        )

    def _powt_only(self):
        """Select only POWT items."""
        for it in self._iter_leaf_items():
            e = it.data(0, Qt.UserRole) or {}
            it.setCheckState(0, Qt.Checked if self._is_powt_entry(e, it) else Qt.Unchecked)

    def _exclude_powt(self):
        """Uncheck POWT items and keep all other current selections unchanged."""
        for it in self._iter_leaf_items():
            e = it.data(0, Qt.UserRole) or {}
            if self._is_powt_entry(e, it):
                it.setCheckState(0, Qt.Unchecked)


    def _detect_chron_variant(self, e: Dict[str, Any], it: QTreeWidgetItem) -> str:
        """Detect chronology flavor (STD/RES) robustly.

        This avoids false positives when dataset names contain 'RES' (e.g. 'FOREST').
        """
        # Prefer explicit metadata when provided by the prefill manifest.
        v = str(e.get("variant", "") or "").strip().upper()
        if v in ("STD", "RES"):
            return v
        label = str(e.get("name", "") or it.text(0) or "").strip()
        up = label.upper()

        # Prefer explicit suffix from the prefill naming scheme.
        if "CHRONOLOGY STD" in up:
            return "STD"
        if "CHRONOLOGY RES" in up:
            return "RES"

        # Fallback to file name markers from the generated CSV paths.
        p = str(e.get("path", "") or "")
        base = os.path.basename(p).lower()
        if "chron_std" in base:
            return "STD"
        if "chron_res" in base:
            return "RES"

        # Last-resort: last token.
        parts = up.split()
        if parts and parts[-1] in ("STD", "RES"):
            return parts[-1]
        return ""

    def _std_only(self):
        """Select only STD chronologies (does not change non-chronology items)."""
        for it in self._iter_leaf_items():
            e = it.data(0, Qt.UserRole) or {}
            cat = str(e.get("category", "chronology")).lower()
            if cat != "chronology":
                continue
            variant = self._detect_chron_variant(e, it)
            it.setCheckState(0, Qt.Checked if variant == "STD" else Qt.Unchecked)

    def _res_only(self):
        """Select only RES chronologies (does not change non-chronology items)."""
        for it in self._iter_leaf_items():
            e = it.data(0, Qt.UserRole) or {}
            cat = str(e.get("category", "chronology")).lower()
            if cat != "chronology":
                continue
            variant = self._detect_chron_variant(e, it)
            it.setCheckState(0, Qt.Checked if variant == "RES" else Qt.Unchecked)

    def _top3_per_group(self):
        """Select the top 3 recommended detrending methods per dataset (name group).

        The selection is applied to chronology leaf items only.
        - If nothing is currently checked, all chronology leaf items are considered candidates.
        - If some items are already checked, this narrows the selection within that checked subset (sequential UX).
        - Methods are ranked by: recommended first, then score (descending), then rank_in_group (ascending).
        - If multiple variants exist for the same method, the currently-eligible variant is preferred
          (STD is the default when both STD and RES are eligible).
        """
        # Collect non-alias chronology leaf items.
        chron_leaf: list[tuple[QTreeWidgetItem, dict]] = []
        for it in self._iter_leaf_items():
            e = it.data(0, Qt.UserRole) or {}
            if e.get("category") != "chronology":
                continue
            if bool(e.get("is_alias", False)):
                continue
            chron_leaf.append((it, e))

        if not chron_leaf:
            return

        # Candidates: checked items if any, otherwise all chronology leaves.
        candidates = [(it, e) for (it, e) in chron_leaf if it.checkState(0) == Qt.Checked]
        if not candidates:
            candidates = chron_leaf

        def _dataset_key_from_entry(it: QTreeWidgetItem, e: dict) -> str:
            ds = str(e.get("dataset_key", "") or "").strip()
            if ds:
                return ds
            # Try to derive from "name" convention: "<dataset> | <method> | ..."
            name = str(e.get("dataset") or e.get("dataset_name") or e.get("name") or it.text(0) or "").strip()
            if "|" in name:
                ds = name.split("|", 1)[0].strip()
                if ds:
                    return ds
            # Fallback: walk parents to find the dataset node directly under Chronologies root.
            p = it.parent()
            while p is not None and p.parent() is not None:
                if p.parent() == self.root_chron:
                    return str(p.text(0) or "").strip()
                p = p.parent()
            return ""

        def _method_key_from_entry(it: QTreeWidgetItem, e: dict) -> str:
            mk = str(e.get("method_key", "") or e.get("method", "") or "").strip()
            if mk:
                return mk
            name = str(e.get("name") or it.text(0) or "").strip()
            if "|" in name:
                parts = [p.strip() for p in name.split("|")]
                if len(parts) >= 2 and parts[1]:
                    return parts[1]
            return name

        # Aggregate method stats and keep per-variant items for each method inside each dataset.
        method_stats: dict[str, dict[str, dict[str, float | int | bool | None]]] = {}
        items_by_ds_method: dict[tuple[str, str], dict[str, list[QTreeWidgetItem]]] = {}

        for it, e in candidates:
            ds_key = _dataset_key_from_entry(it, e)
            method_key = _method_key_from_entry(it, e)

            variant = self._detect_chron_variant(e, it)
            items_by_ds_method.setdefault((ds_key, method_key), {}).setdefault(variant, []).append(it)

            rec = method_stats.setdefault(ds_key, {}).setdefault(
                method_key,
                {"recommended": False, "score": float("-inf"), "rank": None},
            )

            rec["recommended"] = bool(rec["recommended"]) or bool(e.get("recommended", False))

            score_raw = e.get("score", None)
            try:
                score_val = float(score_raw) if score_raw is not None else float("-inf")
            except Exception:
                score_val = float("-inf")
            if score_val > float(rec["score"]):
                rec["score"] = score_val

            rank_raw = e.get("rank_in_group", None)
            try:
                rank_val = int(rank_raw) if rank_raw is not None else None
            except Exception:
                rank_val = None
            if rank_val is not None and rank_val > 0:
                if rec["rank"] is None or rank_val < int(rec["rank"]):
                    rec["rank"] = rank_val

        # Choose top 3 methods per dataset.
        top_methods_by_ds: dict[str, set[str]] = {}
        for ds_key, mstats in method_stats.items():
            def _sort_key(item: tuple[str, dict[str, float | int | bool | None]]):
                m, rec = item
                recommended = 0 if bool(rec.get("recommended", False)) else 1
                score = float(rec.get("score", float("-inf")))
                rank = rec.get("rank", None)
                rank_sort = int(rank) if rank is not None else 10**9
                return (recommended, -score, rank_sort, m)

            ordered = [m for (m, _rec) in sorted(mstats.items(), key=_sort_key)]
            top_methods_by_ds[ds_key] = set(ordered[:3])

        # Build the final keep set: exactly one item per selected method, with a stable variant choice.
        keep: set[QTreeWidgetItem] = set()
        for (ds_key, method_key), by_variant in items_by_ds_method.items():
            if method_key not in top_methods_by_ds.get(ds_key, set()):
                continue

            variants_present = set(by_variant.keys())
            if variants_present == {"RES"}:
                pref = "RES"
            elif variants_present == {"STD"}:
                pref = "STD"
            else:
                pref = "STD"

            chosen: QTreeWidgetItem | None = None
            if pref in by_variant and by_variant[pref]:
                chosen = by_variant[pref][0]
            else:
                # Fallback: pick any available variant deterministically.
                for v in ("STD", "RES", "RWI", ""):
                    if v in by_variant and by_variant[v]:
                        chosen = by_variant[v][0]
                        break
                if chosen is None:
                    # Last resort: pick the first available.
                    for _v, items in by_variant.items():
                        if items:
                            chosen = items[0]
                            break

            if chosen is not None:
                keep.add(chosen)

        # Apply selection to candidate items only.
        for it, _e in candidates:
            it.setCheckState(0, Qt.Checked if it in keep else Qt.Unchecked)


    def selected_entries(self) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        for it in self._iter_leaf_items():
            if it.checkState(0) == Qt.Checked:
                e = it.data(0, Qt.UserRole)
                if isinstance(e, dict):
                    selected.append(e)
        return selected


# Main application window
# ---------------------------
class MainWindow(QMainWindow):
    def __init__(self, prefill_manifest: Optional[str] = None):
        print("MainWindow __init__ started")
        super().__init__()
        # UI scaling: remember the base application font size (in points)
        base_ps = QApplication.font().pointSizeF()
        self._base_font_point_size = float(base_ps) if base_ps and base_ps > 0 else 10.0
        self._ui_scale = 1.0
        self.setWindowTitle("Ring-Climate Analyzer")
        self._prefill_manifest = prefill_manifest
        self.datasets: List[DataSet] = []
        self.result_df: Optional[pd.DataFrame] = None
        self.filtered_result_df: Optional[pd.DataFrame] = None
        self.report_df: Optional[pd.DataFrame] = None
        self.period_result_df: Optional[pd.DataFrame] = None  # <--- NEW
        self.settings_path = os.path.join(os.path.expanduser("~"), ".ringclim_settings.json")
        self.current_project_path: Optional[str] = None
        self._base_app_font = QApplication.font()
        self.ui_scale: float = 1.0
        # UI/paths persistence
        self.last_dir_open: str = os.path.expanduser("~")
        self.last_dir_project: str = os.path.expanduser("~")
        self.last_dir_export: str = os.path.expanduser("~")
        # Online data dialog (ITRDB / CRU TS)
        self.online_settings: Dict[str, Any] = {}
        self._online_dialog = None
        # allow small laptops
        self.setMinimumSize(720, 480)
        self._init_ui()
        self.load_settings()
        self._help_shortcut = QShortcut(QKeySequence("F1"), self)
        self._help_shortcut.setContext(Qt.ApplicationShortcut)
        self._help_shortcut.activated.connect(self.show_help)

        if self._prefill_manifest:
            QTimer.singleShot(
                0,
                lambda p=self._prefill_manifest: self._handle_prefill_manifest(p),
            )
        # session recovery: autosave every 2 minutes (and after analyses / data changes)
        self._results_unsaved = False
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setInterval(120000)
        self._recovery_timer.timeout.connect(lambda: self._autosave_session(now=True))
        self._recovery_timer.start()
        QTimer.singleShot(1500, self._check_recovery)

    def _request_user_attention(self) -> None:
        """Bring the main window to the front and request taskbar attention."""
        try:
            self.showNormal()
        except Exception:
            pass
        try:
            self.raise_()
        except Exception:
            pass
        try:
            self.activateWindow()
        except Exception:
            pass
        try:
            QApplication.alert(self, 5000)
        except Exception:
            pass


    def _handle_prefill_manifest(self, manifest_path: str) -> None:
        """Optionally import datasets passed from Detrend Advisor."""
        if not manifest_path:
            return
        if not os.path.isfile(manifest_path):
            QMessageBox.warning(
                self,
                "Prefill import",
                f"Prefill manifest not found:\n{manifest_path}",
            )
            return

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Prefill import",
                f"Failed to read prefill manifest:\n{exc}",
            )
            return

        entries = manifest.get("entries", [])
        if not isinstance(entries, list) or not entries:
            QMessageBox.information(
                self,
                "Prefill import",
                "Prefill manifest does not contain any datasets.",
            )
            return

        mb = QMessageBox(self)
        mb.setWindowTitle("Import from Detrend Advisor")
        mb.setIcon(QMessageBox.Question)
        mb.setText(
            "Detrend Advisor prepared datasets for import.\n\n"
            "Do you want to include all datasets, or select specific ones?"
        )
        btn_all = mb.addButton("Include all", QMessageBox.AcceptRole)
        btn_select = mb.addButton("Select...", QMessageBox.ActionRole)
        btn_cancel = mb.addButton(QMessageBox.Cancel)
        mb.setWindowModality(Qt.ApplicationModal)
        mb.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._request_user_attention()
        mb.show()
        try:
            mb.raise_()
            mb.activateWindow()
            QApplication.alert(mb, 5000)
        except Exception:
            pass
        mb.exec()

        clicked = mb.clickedButton()
        if clicked == btn_cancel:
            return

        if clicked == btn_all:
            selected = entries
        else:
            dlg = PrefillSelectionDialog(entries, parent=self)
            dlg.setWindowModality(Qt.ApplicationModal)
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self._request_user_attention()
            dlg.show()
            try:
                dlg.raise_()
                dlg.activateWindow()
                QApplication.alert(dlg, 5000)
            except Exception:
                pass
            if dlg.exec() != QDialog.Accepted:
                return
            selected = dlg.selected_entries()

        if not selected:
            QMessageBox.information(self, "Prefill import", "No datasets selected.")
            return

        self._import_prefill_entries(selected)

    def _import_prefill_entries(self, entries: List[Dict[str, Any]]) -> None:
        """Import prefilled datasets from CSV paths and add them to the project."""
        errors: List[str] = []
        added = 0

        for e in entries:
            try:
                path = str(e.get("path", "")).strip()
                if not path or not os.path.isfile(path):
                    raise FileNotFoundError(path or "(missing path)")

                kind = str(e.get("kind", "chronology")).strip().lower() or "chronology"
                name = str(e.get("name", os.path.basename(path))).strip() or os.path.basename(path)

                df = pd.read_csv(path)
                ds = DataSet(name=name, path=path, df=df, kind=kind)
                loc = e.get("location") or {}
                if loc.get("lat") is not None and loc.get("lon") is not None:
                    setattr(ds, "meta", {"source": "Detrend Advisor", "lat": float(loc["lat"]),
                                         "lon": float(loc["lon"]), "site_code": loc.get("site_code", ""),
                                         "site_name": loc.get("site_name", ""), "species": loc.get("species", ""),
                                         "elevation_m": loc.get("elevation_m")})

                # Ensure Year is preferred when present.
                if "Year" in df.columns:
                    ds.year_col = "Year"
                if ds.kind == "chronology":
                    ds.value_columns = [c for c in ds.df.columns if c != ds.year_col]

                self.datasets.append(ds)

                if ds.kind == "climate":
                    item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
                else:
                    item = QListWidgetItem(f"{ds.name} (chronology)")
                item.setData(Qt.UserRole, ds)
                self.list_datasets.addItem(item)
                added += 1
            except Exception as exc:
                label = str(e.get("name", e.get("path", "(unknown)")))
                errors.append(f"{label}: {exc}")
        # show the new chronologies in the Analysis lists (they were only in the Data tab before)
        try:
            self.refresh_analysis_lists()
            self._refresh_loaded_dataset_list_labels()
        except Exception:
            pass
        if added:
            self.statusBar().showMessage(f"Detrend Advisor: {added} dataset(s) added", 8000)
        if errors:
            QMessageBox.warning(self, "Prefill import",
                                f"{len(errors)} dataset(s) could not be imported:\n\n" + "\n".join(errors[:15]))

    def _get_dataset_year_range_safe(self, ds: "DataSet") -> tuple[int, int] | None:
        """
        Return (min_year, max_year) for a dataset using its configured year column.
        Works for numeric years or datetime-like years.
        """
        try:
            if ds is None or ds.df is None:
                return None
            if not hasattr(ds, "year_col") or ds.year_col not in ds.df.columns:
                return None
            s = ds.df[ds.year_col]

            # 1) Prefer numeric years when they look like real calendar years.
            #    This prevents the common pitfall where pd.to_datetime(int_years)
            #    interprets them as nanoseconds since epoch -> all become year 1970.
            years_num = pd.to_numeric(s, errors="coerce").dropna()
            if not years_num.empty:
                # Keep plausible calendar years only.
                y = years_num.astype(float)
                in_range = y.between(800, 3000)
                # If most values are plausible years, use numeric parsing.
                if in_range.any() and float(in_range.mean()) >= 0.80:
                    y2 = y[in_range].astype(int)
                    if not y2.empty:
                        return (int(y2.min()), int(y2.max()))

            # 2) Fall back to datetime parsing (for true date columns).
            dt = pd.to_datetime(s, errors="coerce")
            if dt.notna().any():
                years_dt = dt.dt.year.dropna()
                if not years_dt.empty:
                    # Detect mis-parsed numeric years (classic: everything becomes 1970).
                    if int(years_dt.min()) == 1970 and int(years_dt.max()) == 1970:
                        # If original values can be numeric years, prefer them.
                        years_num2 = pd.to_numeric(s, errors="coerce").dropna()
                        if not years_num2.empty:
                            y3 = years_num2.astype(float)
                            in_range2 = y3.between(800, 3000)
                            if in_range2.any():
                                y4 = y3[in_range2].astype(int)
                                if not y4.empty:
                                    return (int(y4.min()), int(y4.max()))
                    return (int(years_dt.min()), int(years_dt.max()))

            # 3) Last resort: numeric years even if not confidently in-range.
            if not years_num.empty:
                return (int(years_num.min()), int(years_num.max()))

            return None
        except Exception:
            return None

    def _format_dataset_label(self, ds: "DataSet", include_kind: bool = False) -> str:
        """
        Build list label like: 'StationName (1901–2020)' or with kind:
        'StationName (climate, 1901–2020)'.
        """
        base = getattr(ds, "name", "Dataset")
        rng = self._get_dataset_year_range_safe(ds)
        kind = getattr(ds, "kind", "")
        if rng is None:
            if include_kind and kind:
                return f"{base} ({kind})"
            return base
        y0, y1 = rng
        if include_kind and kind:
            return f"{base} ({kind}, {y0}–{y1})"
        return f"{base} ({y0}–{y1})"

    def _refresh_loaded_dataset_list_labels(self) -> None:
        """
        Keep Data tab 'Loaded datasets' labels in sync with actual data.
        This fixes cases where datasets are updated after import (e.g. auto-detect),
        but list text still shows an old/placeholder year range.
        """
        try:
            n = self.list_datasets.count()
        except Exception:
            return
        for i in range(n):
            try:
                item = self.list_datasets.item(i)
                ds = item.data(Qt.UserRole)
            except Exception:
                continue
            if ds is None:
                continue
            try:
                if getattr(ds, "kind", "") == "climate":
                    item.setText(self._format_dataset_label(ds, include_kind=True))
                elif getattr(ds, "kind", "") == "timeseries":
                    freq = getattr(ds, "source_freq", "")
                    label = f"{ds.name} (timeseries: {freq})" if freq else f"{ds.name} (timeseries)"
                    item.setText(label)
                else:
                    item.setText(self._format_dataset_label(ds, include_kind=False))
            except Exception:
                pass

    def _open_dataset_preview_from_item(self, item: "QListWidgetItem") -> None:
        """Open a simple, scrollable preview of the dataset (Year + months/values)."""
        try:
            ds = item.data(Qt.UserRole)
        except Exception:
            ds = None
        if ds is None:
            return
        self._show_dataset_preview_dialog(ds)

    def _show_dataset_preview_dialog(self, ds: "DataSet") -> None:
        """Show a read-only table preview of the dataset contents."""
        try:
            dlg = QDialog(self)
            dlg.setWindowTitle(f"Data preview: {getattr(ds, 'name', 'Dataset')}")
            dlg.resize(900, 600)
            lay = QVBoxLayout()
            info = QLabel()
            rng = self._get_dataset_year_range_safe(ds)
            if rng is None:
                info.setText(f"Dataset: {getattr(ds, 'name', 'Dataset')}")
            else:
                info.setText(
                    f"Dataset: {getattr(ds, 'name', 'Dataset')}    Period: {rng[0]}–{rng[1]}"
                )
            lay.addWidget(info)

            preview_df = self._build_preview_dataframe_for_dataset(ds)

            view = QTableView()
            view.setSortingEnabled(True)
            try:
                model = PandasModel(preview_df.copy())
                view.setModel(model)
            except Exception:
                model = PandasModel(preview_df)
                view.setModel(model)
            lay.addWidget(view)

            btn = QPushButton("Close")
            btn.clicked.connect(dlg.accept)
            lay.addWidget(btn)
            dlg.setLayout(lay)
            dlg.exec()
        except Exception:
            QMessageBox.warning(self, "Preview failed", "Cannot preview this dataset.")

    def _build_preview_dataframe_for_dataset(self, ds: "DataSet") -> pd.DataFrame:
        """
        Build the preview table shown from double-click and generic preview actions.

        Rules:
        - chronology/climate datasets: show stored dataframe with Year + ordered month columns first;
        - timeseries datasets:
            * in Daily resolution mode -> show Year + Jan01..Dec31 daily wide table;
            * otherwise -> show transformed monthly Year + Jan..Dec table.
        """
        if ds is None:
            return pd.DataFrame()

        if getattr(ds, "kind", None) == "timeseries":
            agg_choice = (
                self.combo_timeseries_monthly_agg.currentText()
                if hasattr(self, "combo_timeseries_monthly_agg")
                else "Auto (by variable)"
            )
            resolution = (
                self._get_analysis_resolution_mode()
                if hasattr(self, "_get_analysis_resolution_mode")
                else "monthly"
            )
            if resolution == "daily":
                return timeseries_to_daily_wide_dataframe(ds, agg_choice).head(60).copy()
            ds_monthly = timeseries_to_monthly_climate_dataset(ds, agg_choice)
            return ds_monthly.df.head(60).copy()

        df = getattr(ds, "df", None)
        if df is None or not isinstance(df, pd.DataFrame):
            return pd.DataFrame()

        preview_df = df.copy()
        year_col = getattr(ds, "year_col", None)
        month_cols = getattr(ds, "month_columns", []) or []
        ordered_cols: list[str] = []
        if year_col and year_col in preview_df.columns:
            ordered_cols.append(year_col)
        for c in month_cols:
            if c in preview_df.columns and c not in ordered_cols:
                ordered_cols.append(c)
        for c in preview_df.columns:
            if c not in ordered_cols:
                ordered_cols.append(c)
        return preview_df[ordered_cols].head(60)

    def _apply_default_window_geometry(self) -> None:
        """
        Fit initial window size to the current screen (laptops included),
        without preventing the user from resizing later.
        """
        try:
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is None:
                return
            geo = screen.availableGeometry()
            w = min(1200, int(geo.width() * 0.95))
            h = min(800, int(geo.height() * 0.90))
            w = max(w, self.minimumWidth())
            h = max(h, self.minimumHeight())
            self.resize(w, h)
            # center
            self.move(
                geo.x() + max(0, (geo.width() - w) // 2),
                geo.y() + max(0, (geo.height() - h) // 2),
            )
        except Exception:
            # never crash on geometry issues
            pass

    def _init_ui(self):
        self.tabs = QTabWidget()
        # Keep the tab bar fixed. Inner views (lists/tables) scroll by themselves.
        self.scroll_area = None
        self.setCentralWidget(self.tabs)
        self.tab_data = QWidget()
        self.tab_analysis = QWidget()
        self.tab_results = QWidget()
        self.tab_report = QWidget()
        self.tabs.addTab(self.tab_data, "Data")
        self.tabs.addTab(self.tab_analysis, "Analysis")
        self.tabs.addTab(self.tab_results, "Results")
        self.tabs.addTab(self.tab_report, "Report")
        self._init_tab_data()
        self._init_tab_analysis()
        self._init_tab_results()
        self._init_tab_report()
        self._init_statusbar()

    def _ask_open_manual_import_after_auto_fail(
        self,
        title: str,
        detail: str,
        batch_ctx: Optional[dict] = None,
    ) -> bool:
        """Ask whether manual import should be opened after an automatic path failed.

        In batch import, a single "No" suppresses the same follow-up question for
        all remaining files in the current batch.
        """
        if isinstance(batch_ctx, dict):
            cached_pref = batch_ctx.get("auto_fail_open_manual_pref", None)
            if cached_pref is False:
                return False

        res = QMessageBox.question(
            self,
            title,
            f"{detail}\n\nDo you want to open the manual import dialog instead?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        want_manual = (res == QMessageBox.StandardButton.Yes)
        if isinstance(batch_ctx, dict) and not want_manual:
            batch_ctx["auto_fail_open_manual_pref"] = False
        return want_manual

    def _apply_interface_font(self, font: QFont) -> None:
        """Apply font only to the main interface widgets, not to status/help/about dialogs."""
        targets = [
            getattr(self, "scroll_area", None),
            getattr(self, "tabs", None),
            getattr(self, "tab_data", None),
            getattr(self, "tab_analysis", None),
            getattr(self, "tab_results", None),
            getattr(self, "tab_report", None),
        ]
        seen: set[int] = set()
        for target in targets:
            if target is None:
                continue
            tid = id(target)
            if tid in seen:
                continue
            seen.add(tid)
            try:
                target.setFont(font)
            except Exception:
                pass
            try:
                for child in target.findChildren(QWidget):
                    child.setFont(font)
            except Exception:
                pass

    def _init_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        self._base_status_font = QFont(sb.font())
        self._base_interface_font = QFont(self.tabs.font())

        self.lbl_signature = QLabel("Developed by   Dr. Zulfiyor Bakhtiyorov")
        self.lbl_signature.setStyleSheet("font-weight: 700;")
        self.lbl_signature.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sb.addWidget(self.lbl_signature)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sb.addPermanentWidget(spacer, 1)

        self.status_tools = QWidget()
        tools_row = QHBoxLayout(self.status_tools)
        tools_row.setContentsMargins(6, 0, 6, 0)
        tools_row.setSpacing(6)

        self.lbl_ui_scale = QLabel("UI scale")
        self.lbl_ui_scale.setStyleSheet("font-weight: 600;")
        tools_row.addWidget(self.lbl_ui_scale)

        self.spin_ui_scale = QDoubleSpinBox()
        self.spin_ui_scale.setRange(0.60, 1.50)
        self.spin_ui_scale.setSingleStep(0.05)
        self.spin_ui_scale.setDecimals(2)
        self.spin_ui_scale.setValue(float(self.ui_scale))
        self.spin_ui_scale.setFixedWidth(92)
        self.spin_ui_scale.setToolTip("Scale the user interface (saved in settings).")
        self.spin_ui_scale.valueChanged.connect(self._apply_ui_scale)
        tools_row.addWidget(self.spin_ui_scale)

        if _uihelp is not None and hasattr(_uihelp, "qt_saved_link_label"):
            self.lbl_open_export = _uihelp.qt_saved_link_label(self)
            self.lbl_open_export.setToolTip("Open the folder of the last exported file")
            tools_row.addWidget(self.lbl_open_export)

        self.btn_updates = QPushButton("Updates")
        self.btn_updates.setToolTip("Check for updates, download the latest version and the user guides, "
                                    "contact the developer")
        tools_row.addWidget(self.btn_updates)
        self.updater = None
        if _appupd_qt is not None:
            try:
                self.updater = _appupd_qt.QtUpdater(self, button=self.btn_updates)
            except Exception as exc:  # pragma: no cover
                print("Updater not available:", exc)
                self.btn_updates.setEnabled(False)
        else:
            self.btn_updates.setEnabled(False)

        self.btn_cite = QPushButton("Cite")
        self.btn_cite.setToolTip("Open citation dialog")
        self.btn_cite.clicked.connect(self.show_cite)
        tools_row.addWidget(self.btn_cite)

        self.btn_help = QPushButton("Help")
        self.btn_help.setToolTip("Open Help dialog (hotkey: F1)")
        self.btn_help.clicked.connect(self.show_help)
        tools_row.addWidget(self.btn_help)

        self.btn_about = QPushButton("About")
        self.btn_about.setToolTip("Open About dialog")
        self.btn_about.clicked.connect(self.show_about)
        tools_row.addWidget(self.btn_about)

        sb.addPermanentWidget(self.status_tools)

    def _apply_ui_scale(self, value: float) -> None:
        try:
            scale = float(value)
        except Exception:
            return

        self.ui_scale = max(0.60, min(1.50, scale))
        base = getattr(self, "_base_app_font", QApplication.font())
        f = base
        try:
            # QFont is implicitly shared; make an explicit copy via constructor
            from PySide6.QtGui import QFont
            f = QFont(base)
        except Exception:
            pass

        ps = float(getattr(f, "pointSizeF", lambda: 0.0)())
        if ps <= 0:
            ps = float(getattr(f, "pointSize", lambda: 10)())
        f.setPointSizeF(max(1.0, ps * self.ui_scale))
        self._apply_interface_font(f)

        try:
            base_status = QFont(self._base_status_font)
        except Exception:
            base_status = QFont(QApplication.font())
        for w in (
            getattr(self, "lbl_signature", None),
            getattr(self, "lbl_ui_scale", None),
            getattr(self, "spin_ui_scale", None),
            getattr(self, "btn_cite", None),
            getattr(self, "btn_help", None),
            getattr(self, "btn_about", None),
        ):
            if w is not None:
                try:
                    w.setFont(base_status)
                except Exception:
                    pass

        # Persist immediately (best effort)
        try:
            self.save_settings()
        except Exception:
            pass

    def show_help(self, anchor: str = "") -> None:
        """Open the full, searchable Help dialog (F1)."""
        _show_html_help_dialog(
            self,
            "Help - Ring-Climate Analyzer",
            RINGCLIM_HELP_HTML,
            anchor=str(anchor or ""),
        )

    def show_help_about(self) -> None:
        """Compatibility wrapper for older Help/About actions."""
        self.show_help()

    def show_about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("About")
        dlg.resize(520, 260)
        lay = QVBoxLayout()
        text = QTextEdit()
        text.setReadOnly(True)
        text.setHtml(
            "<h2>Ring-Climate Analyzer</h2>"
            + (f"<p><b>Version:</b> {_appupd.version_text()} - part of {_appupd.display_name()}</p>"
               if _appupd is not None else "")
            + "<p><b>Author:</b> Dr. Zulfiyor Bakhtiyorov</p>"
            "<p><b>Institution:</b> University of Cambridge, Xinjiang Institute of Ecology and Geography CAS, "
            "Khujand Science Center NAST</p>"
            "<p><b>Year:</b> 2026</p>"
            "<p><b>License:</b> MIT License</p>"
        )
        lay.addWidget(text)
        ok = QPushButton("Close")
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok)
        dlg.setLayout(lay)
        dlg.exec()

    def show_cite(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Cite")
        dlg.resize(520, 220)
        lay = QVBoxLayout()
        text = QTextEdit()
        text.setReadOnly(True)
        text.setHtml(
            "<h2>Cite</h2>"
            "<p>Citation text will be added later after the program paper is published.</p>"
        )
        lay.addWidget(text)
        ok = QPushButton("Close")
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok)
        dlg.setLayout(lay)
        dlg.exec()

    def _init_status_bar(self) -> None:
        """
        Create a bottom status bar with a left signature and a right Help/About link.
        """
        sb = QStatusBar(self)
        self.setStatusBar(sb)

        self._status_left = QLabel("Developed by   Dr. Zulfiyor Bakhtiyorov")
        sb.addWidget(self._status_left)

        self._status_right = QLabel('<a href="about">Help/About</a>')
        self._status_right.setTextFormat(Qt.RichText)
        self._status_right.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self._status_right.setOpenExternalLinks(False)
        self._status_right.linkActivated.connect(self.show_about_dialog)
        sb.addPermanentWidget(self._status_right)

    def show_about_dialog(self) -> None:
        """Compatibility wrapper: open the full Help dialog."""
        self.show_help()

    def apply_ui_scale(self, scale: float) -> None:
        """
        Apply a UI scale factor by scaling the global application font size.
        """
        try:
            s = float(scale)
        except Exception:
            return
        s = max(0.5, min(2.0, s))
        self._ui_scale = s
        f = QApplication.font()
        f.setPointSizeF(self._base_font_point_size * s)
        QApplication.setFont(f)
        self.update()

    def _init_tab_data(self):
        layout = QVBoxLayout()
        btn_layout = FlowLayout(spacing=6)  # wraps on narrow screens instead of cutting the buttons
        btn_add = QPushButton("Add / Import File")
        btn_add_multi = QPushButton("Add multiple files")
        btn_large = QPushButton("Large gridded data...")
        btn_online = QPushButton("Online data...")
        btn_online.setToolTip("Download chronologies from ITRDB and climate data (CRU TS, CHELSA, TerraClimate, GPCC, "
                              "SPEI, CHIRPS, GHCN stations, ERA5 ...) with a location map; export locations to GIS")
        btn_online.clicked.connect(self.open_online_data_dialog)
        btn_remove = QPushButton("Remove Selected")
        btn_edit = QPushButton("Edit Selected")
        btn_edit.setToolTip("Reopen the import dialog for the selected dataset (layout, columns, names, climate type)")
        btn_edit.clicked.connect(self.edit_selected_dataset)
        btn_save_project = QPushButton("Save project")
        btn_load_project = QPushButton("Load project")
        btn_add_project = QPushButton("Add project")
        btn_add.setToolTip("Import one file (CSV, TXT, PRN, XLS, XLSX); GRDC and station tables are detected automatically")
        btn_add_multi.setToolTip("Import many files; identical layouts can be imported automatically")
        btn_large.setToolTip("Extract a point or area time series from a NetCDF grid (CRU TS etc.)")
        btn_remove.setToolTip("Remove the selected datasets from the project")
        btn_save_project.setToolTip("Save all datasets to a JSON project file")
        btn_load_project.setToolTip("Open a project (replace the session or merge into it)")
        btn_add_project.setToolTip("Merge another project into the current session")
        btn_save_project.clicked.connect(self.save_project_as)
        btn_load_project.clicked.connect(self.load_project)
        btn_add_project.clicked.connect(self.add_project)
        btn_add.clicked.connect(self.import_any_file)
        btn_add_multi.clicked.connect(self.import_multiple_files)
        btn_large.clicked.connect(self.import_large_gridded_data)
        btn_remove.clicked.connect(self.remove_selected_dataset)
        btn_layout.addWidget(btn_add)
        btn_layout.addWidget(btn_add_multi)
        btn_layout.addWidget(btn_large)
        btn_layout.addWidget(btn_online)
        btn_batch_folder = QPushButton("Import batch folder...")
        btn_batch_folder.setToolTip("Add all datasets of a folder written by 'Download everything for the area' "
                                    "(e.g. by another RingClim window), with their metadata")
        btn_batch_folder.clicked.connect(self.import_batch_folder)
        btn_layout.addWidget(btn_batch_folder)
        btn_redetect = QPushButton("Detect climate types")
        btn_redetect.setToolTip("Set the climate variable type of every climate dataset again from the batch "
                                "manifest (datasets.json / summary.csv), the file header and the file name")
        btn_redetect.clicked.connect(self.redetect_climate_types)
        btn_layout.addWidget(btn_redetect)
        btn_spatial = QPushButton("Spatial correlation...")
        btn_spatial.setToolTip("Correlation maps of a chronology with gridded climate (like the KNMI Climate Explorer), "
                               "with a summary of the correlation results and suggested maps")
        btn_spatial.clicked.connect(self.open_field_panel)
        btn_layout.addWidget(btn_spatial)
        btn_moving = QPushButton("Moving correlation...")
        btn_moving.setToolTip("Running (moving-window) correlations: is the climate signal stable over time? "
                              "With stability statistics and a test of change")
        btn_moving.clicked.connect(self.open_moving_panel)
        btn_layout.addWidget(btn_moving)
        btn_layout.addWidget(btn_edit)
        btn_layout.addWidget(btn_remove)
        btn_remove.setText("Remove selected (Del)")
        btn_layout.addWidget(btn_save_project)
        btn_layout.addWidget(btn_load_project)
        btn_layout.addWidget(btn_add_project)
        layout.addWidget(FlowBar(btn_layout))

        self.list_datasets = QListWidget()
        self.list_datasets.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_datasets.setToolTip("Double-click a dataset to preview it. Ctrl+C copies the selected names. "
                                      "Delete removes the selected datasets.")
        _del = QShortcut(QKeySequence(Qt.Key_Delete), self.list_datasets)
        _del.setContext(Qt.WidgetWithChildrenShortcut)
        _del.activated.connect(self._remove_by_key)
        _bs = QShortcut(QKeySequence(Qt.Key_Backspace), self.list_datasets)  # Mac keyboards
        _bs.setContext(Qt.WidgetWithChildrenShortcut)
        _bs.activated.connect(self._remove_by_key)
        layout.addWidget(QLabel("Loaded datasets:"))
        layout.addWidget(self.list_datasets)
        # Double-click any dataset to preview how it was imported.
        self.list_datasets.itemDoubleClicked.connect(self._open_dataset_preview_from_item)
        self.tab_data.setLayout(layout)

    def show_general_help(self):
        self.show_help_about()

    def _build_default_mappings_for_dataset(self, ds: "DataSet") -> Dict[str, Any]:
        """Build default import-dialog mappings from one existing dataset."""
        mappings: Dict[str, Any] = {}
        if ds is None:
            return mappings

        mappings["climate_param"] = getattr(ds, "climate_param", "Other")

        if getattr(ds, "kind", "") == "climate":
            mappings["layout"] = "wide"
            mappings["year_col"] = getattr(ds, "year_col", "Year")
            mappings["month_cols"] = list(getattr(ds, "month_columns", []) or [])
        elif getattr(ds, "kind", "") == "timeseries":
            mappings["layout"] = "datetime_series"
            mappings["datetime_col"] = getattr(ds, "year_col", "DateTime")
            value_cols = list(getattr(ds, "value_columns", []) or [])
            mappings["value_col"] = value_cols[0] if value_cols else "Value"
            mappings["source_freq"] = getattr(ds, "source_freq", "daily")
        else:
            mappings["layout"] = "multi"
            mappings["year_col"] = getattr(ds, "year_col", "Year")
            value_cols = list(getattr(ds, "value_columns", []) or [])
            mappings["value_cols"] = value_cols
            if len(value_cols) == 1:
                mappings["use_single"] = True
                mappings["single_col"] = value_cols[0]
        return mappings

    def _replace_dataset_from_mapping(
        self,
        row: int,
        old_ds: "DataSet",
        mappings: Dict[str, Any],
    ) -> bool:
        """Replace one dataset using the normal import mapping pipeline."""
        prev_ds_count = len(self.datasets)
        prev_item_count = self.list_datasets.count()

        created_ok = self._create_datasets_from_mapping(
            old_ds.df.copy(),
            old_ds.name,
            old_ds.path,
            mappings,
        )
        if not created_ok:
            return False

        if len(self.datasets) <= prev_ds_count or self.list_datasets.count() <= prev_item_count:
            QMessageBox.warning(
                self,
                "Edit dataset",
                "Dataset replacement did not produce a new imported dataset.",
            )
            return False

        new_ds = self.datasets.pop()
        new_item = self.list_datasets.takeItem(self.list_datasets.count() - 1)

        try:
            old_item = self.list_datasets.takeItem(row)
            del old_item
        except Exception:
            pass

        self.datasets = [d for d in self.datasets if d is not old_ds]
        row = max(0, min(int(row), len(self.datasets)))
        self.datasets.insert(row, new_ds)

        if new_item is None:
            new_item = QListWidgetItem(self._format_dataset_label(new_ds, include_kind=True))
            new_item.setData(Qt.UserRole, new_ds)

        self.list_datasets.insertItem(row, new_item)
        self.list_datasets.setCurrentRow(row)
        try:
            new_item.setSelected(True)
        except Exception:
            pass

        try:
            self.update_data_view()
        except Exception:
            pass
        try:
            self.refresh_analysis_lists()
        except Exception:
            pass
        return True

    def edit_selected_dataset(self):
        row = self.list_datasets.currentRow()
        if row < 0:
            QMessageBox.information(self, "No selection", "Select a dataset in the list to edit its columns.")
            return
        item = self.list_datasets.item(row)
        ds: DataSet = item.data(Qt.UserRole)
        if ds is None:
            QMessageBox.warning(self, "No dataset", "Cannot retrieve the selected dataset.")
            return
        defaults = self._build_default_mappings_for_dataset(ds)

        while True:
            dlg = FileImportDialog(
                df=ds.df.copy(),
                file_path=ds.path,
                sheet_name=None,
                parent=self,
                batch_ctx=None,
            )
            dlg._auto_prompt_done = True

            year_col = defaults.get("year_col", "")
            if year_col:
                try:
                    dlg.combo_year.setCurrentText(year_col)
                except Exception:
                    pass

            if defaults.get("layout") == "wide":
                dlg.radio_wide.setChecked(True)
                month_cols = list(defaults.get("month_cols", []) or [])
                for i in range(dlg.list_months.count()):
                    item_month = dlg.list_months.item(i)
                    item_month.setSelected(item_month.text() in month_cols)
            elif defaults.get("layout") == "datetime_series":
                dlg.radio_date_monthly.setChecked(True)
                try:
                    dlg.combo_date_col.setCurrentText(defaults.get("datetime_col", "DateTime"))
                    dlg.combo_value_date.setCurrentText(defaults.get("value_col", "Value"))
                except Exception:
                    pass
            else:
                dlg.radio_multi.setChecked(True)
                value_cols = list(defaults.get("value_cols", []) or [])
                for i in range(dlg.list_values_multi.count()):
                    item_val = dlg.list_values_multi.item(i)
                    item_val.setSelected(item_val.text() in value_cols)
                if defaults.get("use_single") and defaults.get("single_col"):
                    try:
                        dlg.chk_use_single.setChecked(True)
                        dlg.combo_single_value.setCurrentText(defaults.get("single_col"))
                    except Exception:
                        pass

            try:
                dlg.combo_climate_param.setCurrentText(defaults.get("climate_param", "Other"))
            except Exception:
                pass

            mappings = dlg.get_mappings()
            if mappings is None:
                return

            if getattr(ds, "kind", "") == "timeseries":
                mappings["layout"] = "datetime_series"
                mappings["datetime_col"] = mappings.get("date_col") or defaults.get("datetime_col", "DateTime")
                mappings["value_col"] = mappings.get("value_col") or defaults.get("value_col", "Value")
                mappings["source_freq"] = defaults.get("source_freq", getattr(ds, "source_freq", "daily"))
                mappings["climate_param"] = mappings.get("climate_param", getattr(ds, "climate_param", "Other"))

            if self._replace_dataset_from_mapping(row, ds, mappings):
                return

    def import_large_gridded_data(self) -> None:
        """
        Open dialog for importing a large gridded climate file (e.g., CRU .dat.gz),
        run extraction in background, and add the resulting Year+months table
        as a climate dataset.
        """
        dlg = LargeGridImportDialog(self)
        df, meta = dlg.get_result()
        if df is None or df.empty:
            return

        name_base = os.path.basename(meta.get("source_path", "large_grid"))
        name = meta.get("name", name_base)

        ds = DataSet(name=name, path=meta.get("source_path", ""), df=df, kind="climate")
        ds.year_col = "Year"
        ds.month_columns = MONTH_NAMES
        ds.climate_param = meta.get("climate_param", "Other")

        self.datasets.append(ds)
        item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
        item.setData(Qt.UserRole, ds)
        self.list_datasets.addItem(item)
        self.refresh_analysis_lists()

        QMessageBox.information(
            self,
            "Large data import",
            f"Dataset '{ds.name}' added to the project.",
        )

    # ---- Online data (ITRDB / CRU TS) ----
    def open_online_data_dialog(self) -> None:
        """Open (or raise) the non-modal online data dialog."""
        if _online_qt is None:
            QMessageBox.warning(
                self,
                "Online data",
                "Online data module is not available.\n\n"
                "Put dendro_online.py, dendro_online_qt.py, dendro_sources.py, dendro_sources_qt.py, dendro_copernicus.py, "
                "dendro_geoexport.py and dendro_online_countries.geojson into the dendro_modules folder next to ringclim.py.\n\n"
                + _ONLINE_IMPORT_ERROR,
            )
            return
        if self._online_dialog is not None:
            dock = getattr(self, "_online_dock", None)
            if dock is not None:
                dock.show()
            else:
                self._online_dialog.show()
                self._online_dialog.raise_()
                self._online_dialog.activateWindow()
            return
        dlg = _online_qt.OnlineDataDialog(self, settings=self.online_settings, month_names=MONTH_NAMES)
        dlg.datasets_ready.connect(self._add_online_datasets)
        dlg.settings_changed.connect(self._on_online_settings_changed)
        dlg.finished.connect(self._on_online_dialog_closed)
        self._online_dialog = dlg
        self._push_chronology_locations()
        self._online_dock = None
        if _uihelp is not None and hasattr(_uihelp, "QtTabDock") and hasattr(dlg, "set_dock"):
            # default: a tab of the main window; "Detach to window" makes it a separate window
            self._online_dock = _uihelp.QtTabDock(self.tabs, dlg, "Online data")
            dlg.set_dock(self._online_dock)
            self._online_dock.attach()
        else:
            dlg.show()

    # -----------------------
    # Session recovery (autosave after analyses and data changes; offered again after a crash)
    # -----------------------
    def _recovery_dir(self) -> str:
        d = os.path.join(os.path.expanduser("~"), ".ringclim_recovery")
        os.makedirs(d, exist_ok=True)
        return d

    def _recovery_path(self) -> str:
        return os.path.join(self._recovery_dir(), f"session-{os.getpid()}.pkl")

    @staticmethod
    def _dataset_record(ds: "DataSet") -> Dict[str, Any]:
        return {"name": ds.name, "path": getattr(ds, "path", ""), "kind": ds.kind,
                "climate_param": getattr(ds, "climate_param", "Other"), "year_col": ds.year_col,
                "value_columns": list(ds.value_columns or []), "month_columns": list(ds.month_columns or []),
                "source_freq": getattr(ds, "source_freq", ""), "meta": getattr(ds, "meta", None),
                "monthly_agg": getattr(ds, "monthly_agg", None),
                "derived_from_kind": getattr(ds, "derived_from_kind", None),
                "columns_config": getattr(ds, "columns_config", None), "df": ds.df}

    def _dataset_from_record(self, d: Dict[str, Any]) -> "DataSet":
        ds = DataSet(name=d.get("name", "dataset"), path=d.get("path", ""), df=d["df"], kind=d.get("kind", "chronology"))
        ds.year_col = d.get("year_col", ds.year_col)
        ds.value_columns = d.get("value_columns", ds.value_columns)
        ds.month_columns = d.get("month_columns", ds.month_columns)
        ds.climate_param = d.get("climate_param", "Other")
        for attr in ("source_freq", "meta", "monthly_agg", "derived_from_kind", "columns_config"):
            if d.get(attr) is not None:
                setattr(ds, attr, d.get(attr))
        return ds

    def _schedule_autosave(self) -> None:
        t = getattr(self, "_autosave_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(5000)
            t.timeout.connect(lambda: self._autosave_session(now=True))
            self._autosave_timer = t
        t.start()

    def _autosave_session(self, now: bool = False) -> None:
        """Write the datasets and results to the recovery file (in the background)."""
        if not now:
            self._schedule_autosave()
            return
        if getattr(self, "_restoring", False):
            return
        has_res = self.result_df is not None and not self.result_df.empty
        if not self.datasets and not has_res:
            return
        import pickle
        import threading
        import time as _time

        payload = {
            "version": 1, "time": _time.time(), "pid": os.getpid(),
            "project_path": getattr(self, "current_project_path", None),
            "datasets": [self._dataset_record(ds) for ds in self.datasets],
            "result_df": self.result_df, "period_result_df": self.period_result_df,
            "results_unsaved": bool(getattr(self, "_results_unsaved", False)),
            "pair_cache": dict(getattr(self, "_pair_cache", None) or {}),
            "moving_screen_df": getattr(self, "moving_screen_df", None),
            "field_screen_df": getattr(self, "field_screen_df", None),
        }
        path = self._recovery_path()

        def write() -> None:
            try:
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    pickle.dump(payload, fh, protocol=4)
                os.replace(tmp, path)
            except Exception as exc:  # pragma: no cover
                print("Session autosave failed:", exc)

        threading.Thread(target=write, daemon=True).start()

    def _discard_recovery(self) -> None:
        try:
            os.remove(self._recovery_path())
        except OSError:
            pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0 or pid == os.getpid():
            return pid == os.getpid()
        if sys.platform == "win32":
            import ctypes

            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _check_recovery(self) -> None:
        """Offer sessions of RingClim windows that ended without closing properly."""
        import pickle
        import time as _time

        try:
            files = [os.path.join(self._recovery_dir(), f) for f in os.listdir(self._recovery_dir())
                     if f.startswith("session-") and f.endswith(".pkl")]
        except OSError:
            return
        for path in sorted(files, key=os.path.getmtime, reverse=True):
            try:
                pid = int(os.path.basename(path)[8:-4])
            except ValueError:
                continue
            if self._pid_alive(pid):
                continue  # another RingClim window is still open
            try:
                with open(path, "rb") as fh:
                    data = pickle.load(fh)
            except Exception:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            n_ds = len(data.get("datasets") or [])
            res = data.get("result_df")
            n_res = 0 if res is None else len(res)
            when = _time.strftime("%d.%m.%Y %H:%M", _time.localtime(data.get("time", os.path.getmtime(path))))
            box = QMessageBox(self)
            box.setWindowTitle("Restore session")
            box.setIcon(QMessageBox.Question)
            box.setText(f"RingClim was closed unexpectedly on {when}.\n\nThe last session can be restored: "
                        f"{n_ds} dataset(s)" + (f" and {n_res:,} correlation result rows" if n_res else "") + ".")
            box.setInformativeText("Restore it now?")
            b_restore = box.addButton("Restore", QMessageBox.AcceptRole)
            b_discard = box.addButton("Discard", QMessageBox.DestructiveRole)
            box.addButton("Later", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == b_restore:
                self._restore_session(data)
                try:
                    os.remove(path)
                except OSError:
                    pass
                self._autosave_session(now=True)
                return
            if box.clickedButton() == b_discard:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return

    def _restore_session(self, data: Dict[str, Any]) -> None:
        self._restoring = True
        try:
            names = {ds.name for ds in self.datasets}
            for d in data.get("datasets") or []:
                if d.get("name") in names:
                    continue
                ds = self._dataset_from_record(d)
                self.datasets.append(ds)
                try:
                    label = self._format_dataset_label(ds, include_kind=True)
                except Exception:
                    label = f"{ds.name} ({ds.kind})"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, ds)
                self.list_datasets.addItem(item)
            if data.get("project_path") and not getattr(self, "current_project_path", None):
                self.current_project_path = data.get("project_path")
            self.refresh_analysis_lists()
            res = data.get("result_df")
            if res is not None and not res.empty:
                self._set_results(res, data.get("period_result_df"))
                self._results_unsaved = bool(data.get("results_unsaved", True))
            if isinstance(data.get("pair_cache"), dict):
                self._pair_cache = dict(data["pair_cache"])
            for attr in ("moving_screen_df", "field_screen_df"):
                if isinstance(data.get(attr), pd.DataFrame):
                    setattr(self, attr, data[attr])
                self.tabs.setCurrentWidget(self.tab_results)
        finally:
            self._restoring = False
        self.statusBar().showMessage("Session restored.", 15000)

    def _set_results(self, df: pd.DataFrame, periods: Optional[pd.DataFrame] = None) -> None:
        """Show a results table (restored session or project) like after an analysis."""
        self.result_df = df
        self.period_result_df = periods if periods is not None else pd.DataFrame()
        self._refresh_result_type_list()
        self.filtered_result_df = self._filter_results(self.result_df)
        self.filtered_period_df = self._filter_results(self.period_result_df)
        try:
            self._create_report_from_results()
        except Exception as exc:
            print("Report could not be rebuilt:", exc)
        self.update_results_table()
        for attr in ("_field_panel", "_moving_panel"):
            fp = getattr(self, attr, None)
            if fp is not None:
                try:
                    fp.refresh_from_host()
                except Exception:
                    pass

    def notify_saved(self, title: str, text: str, path: str) -> None:
        """Message with a clickable location; the 'Open export folder' link at the bottom keeps it."""
        try:  # results exported from the Results / Report tab count as saved
            if self.tabs.currentWidget() in (getattr(self, "tab_results", None), getattr(self, "tab_report", None)):
                self._results_unsaved = False
        except Exception:
            pass
        if _uihelp is not None and hasattr(_uihelp, "qt_notify_saved"):
            _uihelp.qt_notify_saved(self, title, text, path)
        else:
            QMessageBox.information(self, title, f"{text}\n\n{path}")
        try:
            self.statusBar().showMessage(f"{text} {path}", 15000)
        except Exception:
            pass

    def import_batch_folder(self, folder: str = "") -> None:
        """Add every dataset of a batch folder (datasets.json, or summary.csv + data/*.csv)."""
        if not folder:
            start = os.path.join(os.path.expanduser("~"), "DeClim_batch")
            folder = QFileDialog.getExistingDirectory(self, "Batch folder (batch_...)",
                                                      start if os.path.isdir(start) else os.path.expanduser("~"))
        if not folder:
            return
        import re as _re

        def safe(name: str) -> str:
            return (_re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "dataset")[:80]

        entries: List[Dict[str, Any]] = []
        problems: List[str] = []
        man = os.path.join(folder, "datasets.json")
        items: List[Dict[str, Any]] = []
        if os.path.exists(man):
            try:
                with open(man, "r", encoding="utf-8") as fh:
                    items = list(json.load(fh).get("datasets", []))
            except Exception as exc:
                problems.append(f"datasets.json: {exc}")
        if not items:
            summ = os.path.join(folder, "summary.csv")
            if not os.path.exists(summ):
                QMessageBox.warning(self, "Import batch folder",
                                    "No datasets.json or summary.csv in this folder. Choose a folder batch_... "
                                    "written by 'Download everything for the area'.")
                return
            sm = pd.read_csv(summ, encoding="utf-8-sig")
            data_dir = os.path.join(folder, "data")
            files = {os.path.splitext(f)[0]: f for f in os.listdir(data_dir)} if os.path.isdir(data_dir) else {}
            for _i, r in sm.iterrows():
                name = str(r.get("dataset", ""))
                f = files.get(safe(name)) or next((v for k, v in files.items() if k.startswith(safe(name)[:40])), None)
                if f is None:
                    problems.append(f"{name}: file not found")
                    continue
                meta = {k: (None if pd.isna(v) else v) for k, v in r.items()
                        if k in ("source", "variable", "units", "lat", "lon", "method", "citation", "warning")}
                items.append({"name": name, "kind": str(r.get("kind", "climate")),
                              "climate_param": str(r.get("climate_type", "Other") or "Other"),
                              "file": "data/" + f, "meta": meta})
        for it in items:
            try:
                path = os.path.join(folder, str(it.get("file", "")).replace("/", os.sep))
                df = pd.read_csv(path)
                kind = str(it.get("kind", "climate"))
                if kind == "timeseries" and "DateTime" in df.columns:
                    df["DateTime"] = pd.to_datetime(df["DateTime"], errors="coerce")
                meta = dict(it.get("meta") or {})
                meta["select_for_analysis"] = True
                meta.setdefault("imported_from", folder)
                e = {"kind": kind, "name": it.get("name") or os.path.basename(path), "path": path, "df": df,
                     "climate_param": it.get("climate_param", "Other"), "meta": meta}
                for k in ("value_column", "source_freq", "monthly_agg"):
                    if it.get(k):
                        e[k] = it[k]
                entries.append(e)
            except Exception as exc:
                problems.append(f"{it.get('name')}: {exc}")
        have = {ds.name for ds in self.datasets}
        dup = [e for e in entries if e["name"] in have]
        if dup and QMessageBox.question(
                self, "Import batch folder",
                f"{len(dup)} of {len(entries)} dataset(s) are already loaded (same name). Skip them?") == QMessageBox.Yes:
            entries = [e for e in entries if e["name"] not in have]
        if entries:
            self._add_online_datasets(entries)
        msg = f"{len(entries)} dataset(s) added from\n{folder}"
        if problems:
            msg += "\n\nProblems:\n" + "\n".join(problems[:15])
        QMessageBox.information(self, "Import batch folder", msg)

    def redetect_climate_types(self) -> None:
        """Re-detect climate_param of all climate datasets (manifest, header, file name)."""
        changes = []
        for ds in self.datasets:
            if getattr(ds, "kind", "") not in ("climate", "timeseries"):
                continue
            path = getattr(ds, "path", "") or ""
            bm = batch_metadata_for_file(path) if path else None
            guess = ((bm or {}).get("climate_param") or (guess_climate_param_from_metadata(path) if path else None)
                     or guess_climate_param_from_name(path or ds.name, "")
                     or guess_climate_param_from_name(ds.name + ".csv", ""))
            if guess and guess != getattr(ds, "climate_param", "Other"):
                changes.append((ds, getattr(ds, "climate_param", "Other"), guess))
        if not changes:
            QMessageBox.information(self, "Detect climate types", "All climate types already match.")
            return
        text = "\n".join(f"{ds.name}: {old} -> {new}" for ds, old, new in changes[:40])
        if len(changes) > 40:
            text += f"\n... and {len(changes) - 40} more"
        if QMessageBox.question(self, "Detect climate types",
                                f"{len(changes)} dataset(s) get another type:\n\n{text}\n\nApply?") != QMessageBox.Yes:
            return
        for ds, _old, new in changes:
            ds.climate_param = new
        for i in range(self.list_datasets.count()):
            it = self.list_datasets.item(i)
            ds = it.data(Qt.UserRole)
            if any(ds is c[0] for c in changes):
                try:
                    it.setText(self._format_dataset_label(ds, include_kind=True))
                except Exception:
                    pass
        self.refresh_analysis_lists()
        self.statusBar().showMessage(f"Climate type changed for {len(changes)} dataset(s).", 10000)

    # ---- Spatial correlation page ----
    def open_field_panel(self) -> None:
        """Open (or show) the spatial correlation page; by default it is a tab of the main window."""
        if _field_qt is None:
            QMessageBox.warning(self, "Spatial correlation",
                                "The spatial correlation module is not available.\n\nPut dendro_field.py and "
                                "dendro_field_qt.py into the dendro_modules folder.\n\n" + _FIELD_IMPORT_ERROR)
            return
        panel = getattr(self, "_field_panel", None)
        if panel is not None:
            panel.refresh_from_host()
            dock = getattr(self, "_field_dock", None)
            if dock is not None:
                dock.show()
            else:
                panel.show()
                panel.raise_()
            return
        panel = _field_qt.FieldCorrelationPanel(self)
        self._field_panel = panel
        self._field_dock = None

        def _closed() -> None:
            self._field_dock = None
            p = getattr(self, "_field_panel", None)
            self._field_panel = None
            if p is not None:
                p.deleteLater()

        if _uihelp is not None and hasattr(_uihelp, "QtTabDock"):
            self._field_dock = _uihelp.QtTabDock(self.tabs, panel, "Spatial correlation", on_closed=_closed)
            panel.set_dock(self._field_dock)
            self._field_dock.attach()
        else:
            panel.setParent(self, Qt.Window)
            panel.resize(1300, 850)
            panel.show()

    # ---- Moving correlation page ----
    def open_moving_panel(self) -> None:
        """Open (or show) the moving correlation page; by default it is a tab of the main window."""
        if _moving_qt is None:
            QMessageBox.warning(self, "Moving correlation",
                                "The moving correlation module is not available.\n\nPut dendro_moving.py and "
                                "dendro_moving_qt.py into the dendro_modules folder.\n\n" + _MOVING_IMPORT_ERROR)
            return
        panel = getattr(self, "_moving_panel", None)
        if panel is not None:
            panel.refresh_from_host()
            dock = getattr(self, "_moving_dock", None)
            if dock is not None:
                dock.show()
            else:
                panel.show()
                panel.raise_()
            return
        panel = _moving_qt.MovingCorrelationPanel(self)
        self._moving_panel = panel
        self._moving_dock = None

        def _closed() -> None:
            self._moving_dock = None
            p = getattr(self, "_moving_panel", None)
            self._moving_panel = None
            if p is not None:
                p.deleteLater()

        if _uihelp is not None and hasattr(_uihelp, "QtTabDock"):
            self._moving_dock = _uihelp.QtTabDock(self.tabs, panel, "Moving correlation", on_closed=_closed)
            panel.set_dock(self._moving_dock)
            self._moving_dock.attach()
        else:
            panel.setParent(self, Qt.Window)
            panel.resize(1300, 850)
            panel.show()

    def moving_chronologies(self) -> List[dict]:
        out = []
        for c in self.field_chronologies():
            ds = c["dataset"]
            name = c["name"]
            col = name[len(ds.name) + 2:-1] if name.startswith(ds.name + " [") else (
                (ds.value_columns or [""])[0] if len(ds.value_columns or []) == 1 else "")
            out.append({"name": name, "dataset": ds.name, "column": col, "series": c["series"],
                        "lat": c.get("lat"), "lon": c.get("lon")})
        return out

    def moving_climates(self) -> dict:
        """name -> dict(monthly=DataFrame year x 1..12 | None, annual=Series | None, param=str)."""
        out = {}
        for ds in self.datasets:
            if getattr(ds, "kind", "") != "climate":
                continue
            try:
                df = ds.df
                ycol = ds.year_col if ds.year_col in df.columns else df.columns[0]
                years = pd.to_numeric(df[ycol], errors="coerce")
                mcols = [c for c in (ds.month_columns or []) if c in df.columns]
                if len(mcols) != 12:
                    continue
                tab = _replace_sentinel_missing(df, mcols)[mcols].apply(pd.to_numeric, errors="coerce")
                tab.index = years.values
                tab = tab[~pd.isna(tab.index)]
                tab.index = tab.index.astype(int)
                tab = tab.groupby(level=0).mean()
                tab.columns = list(range(1, 13))
                out[ds.name] = {"monthly": tab, "annual": None,
                                "param": getattr(ds, "climate_param", "Other") or "Other"}
            except Exception:
                continue
        return out

    def _chronology_series(self, ds: "DataSet") -> List[tuple]:
        """[(label, pd.Series year->value)] of a chronology dataset (one per value column)."""
        out = []
        try:
            df = ds.df
            ycol = ds.year_col if ds.year_col in df.columns else df.columns[0]
            years = pd.to_numeric(df[ycol], errors="coerce")
            cols = [c for c in (ds.value_columns or []) if c in df.columns and c != ycol]
            if len(cols) > 8:   # RWI tables with many series: not a chronology
                return []
            for c in cols:
                s = pd.Series(pd.to_numeric(df[c], errors="coerce").values, index=years.values).dropna()
                s = s[~s.index.isna()]
                s.index = s.index.astype(int)
                if len(s) >= 10:
                    out.append((ds.name if len(cols) == 1 else f"{ds.name} [{c}]", s))
        except Exception:
            pass
        return out

    def field_chronologies(self) -> List[dict]:
        out = []
        for ds in self.datasets:
            if getattr(ds, "kind", "") != "chronology":
                continue
            meta = getattr(ds, "meta", None) or {}
            for label, s in self._chronology_series(ds):
                out.append({"name": label, "series": s, "lat": meta.get("lat"), "lon": meta.get("lon"),
                            "site": meta.get("site_code") or "", "dataset": ds})
        return out

    def field_climate_info(self) -> dict:
        out = {}
        if _field is None:
            return out
        for ds in self.datasets:
            if getattr(ds, "kind", "") not in ("climate", "timeseries"):
                continue
            meta = getattr(ds, "meta", None) or {}
            rng = self._get_dataset_year_range_safe(ds) if hasattr(self, "_get_dataset_year_range_safe") else None
            out[ds.name] = _field.ClimateInfo(ds.name, getattr(ds, "climate_param", "Other") or "Other",
                                              str(meta.get("source", "") or meta.get("dataset", "")),
                                              str(meta.get("variable", "") or ""),
                                              rng[0] if rng else None, rng[1] if rng else None)
        return out

    def field_period(self):
        """Year range of the Analysis tab (used for map and moving-correlation suggestions)."""
        try:
            return int(self.spin_year_start.value()), int(self.spin_year_end.value())
        except Exception:
            return None

    def field_results(self):
        return getattr(self, "result_df", None)

    def field_set_location(self, name: str, lat: float, lon: float) -> None:
        for ds in self.datasets:
            if getattr(ds, "kind", "") != "chronology":
                continue
            if ds.name == name or name.startswith(ds.name + " ["):
                meta = dict(getattr(ds, "meta", None) or {})
                meta["lat"], meta["lon"] = float(lat), float(lon)
                meta.setdefault("source", "typed by the user")
                setattr(ds, "meta", meta)
        self._push_chronology_locations()

    def chronology_locations(self) -> List[tuple]:
        """(name, lat, lon) of every chronology with coordinates in its metadata (one per location)."""
        out: List[tuple] = []
        seen = set()
        for ds in self.datasets:
            if getattr(ds, "kind", "") != "chronology":
                continue
            meta = getattr(ds, "meta", None) or {}
            lat, lon = meta.get("lat"), meta.get("lon")
            if lat is None or lon is None:
                continue
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            key = (round(lat, 4), round(lon, 4))
            if key in seen:
                continue
            seen.add(key)
            name = str(meta.get("site_code") or "").strip() or str(ds.name).split("|")[0].strip()
            out.append((name, lat, lon))
        return out

    def _push_chronology_locations(self) -> None:
        dlg = getattr(self, "_online_dialog", None)
        if dlg is not None and hasattr(dlg, "set_chronology_locations"):
            try:
                dlg.set_chronology_locations(self.chronology_locations())
            except Exception:
                pass
        for attr in ("_field_panel", "_moving_panel"):
            fc = getattr(self, attr, None)
            if fc is not None and hasattr(fc, "refresh_from_host"):
                try:
                    fc.refresh_from_host()
                except Exception:
                    pass

    def _on_online_settings_changed(self, settings: dict) -> None:
        self.online_settings = dict(settings or {})

    def _on_online_dialog_closed(self, *args) -> None:
        dlg = self._online_dialog
        if dlg is not None:
            try:
                self.online_settings = dlg.get_settings()
            except Exception:
                pass
            dlg.deleteLater()
        self._online_dialog = None
        self._online_dock = None

    def _add_online_datasets(self, entries: list) -> None:
        """Receive datasets from the online dialog and add them to the project."""
        added = 0
        for e in entries or []:
            try:
                df = e["df"].copy()
                kind = e.get("kind", "climate")
                ds = DataSet(name=e["name"], path=e.get("path", ""), df=df, kind=kind)
                ds.year_col = "Year"
                if kind == "climate":
                    ds.month_columns = list(MONTH_NAMES)
                    ds.climate_param = e.get("climate_param", "Other")
                elif kind == "timeseries":  # daily series (CHELSA daily, CHIRPS, GHCN-Daily, ERA5 ...)
                    ds.year_col = "DateTime"
                    ds.value_columns = ["Value"]
                    ds.month_columns = []
                    ds.climate_param = e.get("climate_param", "Other")
                    setattr(ds, "source_freq", e.get("source_freq", "daily"))
                else:
                    ds.value_columns = [e.get("value_column") or [c for c in df.columns if c != "Year"][0]]
                if e.get("monthly_agg"):
                    setattr(ds, "monthly_agg", e.get("monthly_agg"))
                setattr(ds, "meta", e.get("meta"))
                self.datasets.append(ds)
                if kind == "timeseries":
                    label = f"{ds.name} (timeseries: {getattr(ds, 'source_freq', 'daily')})"
                else:
                    label = self._format_dataset_label(ds, include_kind=(kind == "climate"))
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, ds)
                item.setToolTip(str((e.get("meta") or {}).get("warning", "")) or e.get("path", ""))
                self.list_datasets.addItem(item)
                added += 1
            except Exception as exc:
                QMessageBox.warning(self, "Online data", f"Could not add {e.get('name', 'dataset')}:\n{exc}")
        if added:
            self.refresh_analysis_lists()
            self._refresh_loaded_dataset_list_labels()
            self.statusBar().showMessage(f"Online data: {added} dataset(s) added", 8000)
            # Batch download ("Download everything for the area"): select all new datasets for the analysis
            batch = [e for e in entries or [] if (e.get("meta") or {}).get("select_for_analysis")]
            if batch:
                self._select_new_datasets_for_analysis(len(batch))

    def _select_new_datasets_for_analysis(self, n_new: int) -> None:
        """Select the last n_new datasets in the Analysis lists and show the Analysis tab."""
        new = set(id(ds) for ds in self.datasets[-n_new:])
        try:
            for lst in (self.list_chron_for_analysis, self.list_climate_for_analysis):
                lst.clearSelection()
                for i in range(lst.count()):
                    it = lst.item(i)
                    ds = it.data(Qt.UserRole)
                    if ds is not None and id(ds) in new:
                        it.setSelected(True)
            # keep the user's chronologies selected when the batch brought none
            if not self.list_chron_for_analysis.selectedItems():
                for i in range(self.list_chron_for_analysis.count()):
                    self.list_chron_for_analysis.item(i).setSelected(True)
            self.tabs.setCurrentWidget(self.tab_analysis)
            self.statusBar().showMessage(
                f"Batch: {n_new} dataset(s) added and selected - press 'Run analysis'", 12000)
        except Exception:
            pass

    # ---- Analysis tab ----
    def _init_tab_analysis(self):
        layout = QVBoxLayout()

        target_h = FlowLayout(spacing=8)  # wraps on narrow screens
        target_h.addWidget(QLabel("Correlation target:"))
        self.combo_analysis_target = QComboBox()
        self.combo_analysis_target.addItems(["Chronology × Climate", "Chronology × Chronology"])
        self.combo_analysis_target.setToolTip(
            "Choose whether to correlate chronologies with climate datasets, or to correlate chronologies with other chronologies."
        )
        self.combo_analysis_target.currentIndexChanged.connect(self._on_analysis_target_changed)
        target_h.addWidget(self.combo_analysis_target)
        target_h.addWidget(QLabel("Resolution:"))
        self.combo_analysis_resolution = QComboBox()
        self.combo_analysis_resolution.addItems(["Monthly", "Daily"])
        self.combo_analysis_resolution.setToolTip(
            "Monthly = use monthly climate windows. Daily = use direct daily windows from daily/hourly time-series datasets."
        )
        self.combo_analysis_resolution.currentIndexChanged.connect(self._on_analysis_resolution_changed)
        target_h.addWidget(self.combo_analysis_resolution)

        self.btn_select_all_targets = QPushButton("Select all")
        self.btn_select_all_targets.setToolTip("Select all items in the climate/target list.")
        self.btn_select_all_targets.clicked.connect(
            lambda: self.list_climate_for_analysis.selectAll()
        )
        target_h.addWidget(self.btn_select_all_targets)

        self.btn_clear_targets = QPushButton("Clear selection")
        self.btn_clear_targets.setToolTip("Clear the selection in both lists (chronologies and climate/targets).")
        self.btn_clear_targets.clicked.connect(self._clear_analysis_selection)
        target_h.addWidget(self.btn_clear_targets)

        self.btn_preview_selected = QPushButton("Preview selected dataset")
        self.btn_preview_selected.setToolTip("Show a preview table for the currently selected dataset (first rows).")
        self.btn_preview_selected.clicked.connect(self.preview_selected_dataset)
        target_h.addWidget(self.btn_preview_selected)

        self.btn_run_analysis_top = PrimaryButton("Run analysis")
        self.btn_run_analysis_top.setMinimumHeight(34)
        _run_font_top = QFont(self.btn_run_analysis_top.font())
        _run_font_top.setBold(True)
        self.btn_run_analysis_top.setFont(_run_font_top)
        self.btn_run_analysis_top.setProperty("primary", True)
        self.btn_run_analysis_top.setToolTip("Compute all correlations for the selected datasets and settings.")
        self.btn_run_analysis_top.clicked.connect(self.run_analysis)
        target_h.addWidget(self.btn_run_analysis_top)

        layout.addWidget(FlowBar(target_h))

        # ---- data lists (large, resizable) ----
        group_sets = QGroupBox("Select data for analysis")
        g_layout = QHBoxLayout()
        g_layout.setContentsMargins(6, 6, 6, 6)
        g_layout.setSpacing(10)
        self.list_chron_for_analysis = QListWidget()
        self.list_chron_for_analysis.setSelectionMode(QAbstractItemView.MultiSelection)
        self.list_climate_for_analysis = QListWidget()
        self.list_climate_for_analysis.setSelectionMode(QAbstractItemView.MultiSelection)
        for lw in (self.list_chron_for_analysis, self.list_climate_for_analysis):
            lw.setMinimumHeight(140)
            lw.setAlternatingRowColors(True)
            lw.setUniformItemSizes(True)
        g_layout.addWidget(self._analysis_list_panel(QLabel("Chronologies (A)"), self.list_chron_for_analysis), 1)
        self._label_target_list = QLabel("Climate datasets")
        self._wrap_target_list = self._analysis_list_panel(self._label_target_list, self.list_climate_for_analysis)
        g_layout.addWidget(self._wrap_target_list, 1)
        group_sets.setLayout(g_layout)

        # ---- compact parameter area ----
        params_host = QWidget()
        params_lay = QVBoxLayout(params_host)
        params_lay.setContentsMargins(0, 0, 0, 0)
        params_lay.setSpacing(4)

        ts_h = QHBoxLayout()
        self.lbl_timeseries_agg = QLabel("Daily/hourly → monthly:")
        ts_h.addWidget(self.lbl_timeseries_agg)
        self.combo_timeseries_monthly_agg = QComboBox()
        self.combo_timeseries_monthly_agg.addItems(["Auto (by variable)", "sum", "mean"])
        self.combo_timeseries_monthly_agg.setToolTip(
            "Base aggregation for time-series climate data. In Monthly mode it controls daily/hourly → monthly aggregation. In Daily mode it controls hourly → daily aggregation."
        )
        ts_h.addWidget(self.combo_timeseries_monthly_agg)
        self.btn_preview_timeseries_monthly = QPushButton("Preview selected daily/hourly → monthly")
        self.btn_preview_timeseries_monthly.setToolTip(
            "Preview the time-series table that will be used in the current resolution mode."
        )
        self.btn_preview_timeseries_monthly.clicked.connect(self.preview_selected_timeseries_monthly)
        ts_h.addWidget(self.btn_preview_timeseries_monthly)
        ts_h.addSpacing(12)
        self.lbl_selected_periods = QLabel("")
        self.lbl_selected_periods.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_selected_periods.setWordWrap(True)
        self.lbl_selected_periods.setStyleSheet("color: #52514e;")
        ts_h.addWidget(self.lbl_selected_periods, 1)
        params_lay.addLayout(ts_h)

        def _spin(lo: int, hi: int, val: int, tip: str, width: int = 80) -> QSpinBox:
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(val)
            sp.setToolTip(tip)
            sp.setMaximumWidth(width)
            return sp

        def _pair(*widgets) -> QWidget:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            for x in widgets:
                h.addWidget(QLabel(x) if isinstance(x, str) else x)
            h.addStretch(1)
            return w

        group_params = QGroupBox("Analysis parameters")
        group_params.setStyleSheet(
            "QGroupBox { font-weight: 700; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )
        p_layout = QHBoxLayout()
        p_layout.setContentsMargins(8, 6, 8, 6)
        p_layout.setSpacing(16)

        # column 1: what is correlated
        self.spin_year_start = _spin(-9999, 9999, 1900, "Start year for the analysis. Only overlapping data inside the selected year range will be used.")
        self.spin_year_end = _spin(-9999, 9999, 2020, "End year for the analysis. Only overlapping data inside the selected year range will be used.")
        self.chk_common_period = QCheckBox("common period")
        self.chk_common_period.setToolTip(
            "When checked, year range will be set to the overlapping years "
            "across all selected chronology and climate datasets."
        )
        self.spin_min_len = _spin(1, 24, 1, "Shortest climate window length in months.", 60)
        self.spin_max_len = _spin(1, 24, 24, "Longest climate window length in months.", 60)
        self.combo_corr = QComboBox()
        self.combo_corr.addItems(["pearson", "spearman"])
        self.combo_corr.setToolTip("Correlation method used to compare chronology values with climate windows.")
        self.combo_agg = QComboBox()
        self.combo_agg.addItems(["Auto (by variable)", "sum", "mean", "sum/2", "both"])
        self.combo_agg.setCurrentText("Auto (by variable)")
        self.combo_agg.setToolTip(
            "Choose how to aggregate climate windows.\n"
            "Auto (by variable): uses climate variable type to pick sum or mean.\n"
            "sum: sums months in each window.\n"
            "mean: averages months in each window.\n"
            "sum/2: sum then divide by 2 for all windows.\n"
            "both: compute both sum and mean."
        )
        self.spin_min_n = _spin(3, 500, 15,
                                "Correlations computed from fewer common years than this are skipped. Short station records "
                                "otherwise produce very high but meaningless correlations (e.g. r = 0.99 with n = 3).", 70)
        self.chk_exclude_single = QCheckBox("Exclude single-month windows")
        self.chk_exclude_single.setChecked(False)
        self.chk_exclude_single.setToolTip("Skip one-month windows such as pJan or Jan and keep only multi-month windows.")
        self.combo_season = QComboBox()
        self.combo_season.addItems(["All months", "Apr-Sep", "Oct-Mar", "Custom"])
        self.combo_season.setCurrentText("All months")
        self.combo_season.setToolTip("Limit analysis to windows fully contained inside the selected season.")
        self.spin_season_start = _spin(1, 12, 1, "Custom season start month.", 55)
        self.spin_season_end = _spin(1, 12, 12, "Custom season end month.", 55)

        for cb in (self.combo_agg, self.combo_corr):
            cb.setMaximumWidth(240)
        col1 = QFormLayout()
        col1.setLabelAlignment(Qt.AlignRight)
        col1.setVerticalSpacing(4)
        col1.addRow("Years:", _pair(self.spin_year_start, "–", self.spin_year_end, self.chk_common_period))
        col1.addRow("Window length:", _pair(self.spin_min_len, "–", self.spin_max_len, "months"))
        col1.addRow("Aggregation:", self.combo_agg)
        col1.addRow("Correlation:", self.combo_corr)
        col1.addRow("Min. years (n):", _pair(self.spin_min_n, self.chk_exclude_single))
        col1.addRow("Season filter:", _pair(self.combo_season, self.spin_season_start, "–", self.spin_season_end))
        p_layout.addLayout(col1, 3)

        # column 2: result size and missing values
        self.spin_top_n = _spin(0, 100000, 0, "Show only the strongest N correlations. Use 0 to keep all rows.")
        self.combo_top_mode = QComboBox()
        self.combo_top_mode.addItems(["Overall (top N)", "Per pair (top N per pair)"])
        self.combo_top_mode.setCurrentText("Per pair (top N per pair)")
        self.combo_top_mode.setToolTip(
            "Choose whether top-N filtering is applied globally or separately for each chronology-climate pair."
        )
        self.spin_report_k = _spin(0, 1000, 10,
                                   "How many strongest rows per pair are included in the compact report. Use 0 for the default value 10.")
        self.radio_missing_ignore = QRadioButton("Ignore (use available months)")
        self.radio_missing_ignore.setChecked(True)
        self.radio_missing_strict = QRadioButton("Strict: drop windows with a gap")
        self.radio_missing_strict.setToolTip("Drop a climate window for that year if any month inside the window is missing.")
        self.radio_missing_fill_mean = QRadioButton("Fill with monthly mean")
        self.radio_missing_fill_mean.setToolTip("Replace missing values by the long-term mean of the same calendar month.")
        self.radio_missing_local = QRadioButton("Local mean ±K years")
        self.radio_missing_local.setToolTip("Replace missing values using nearby years of the same month. Controlled by K.")
        self.radio_missing_interp_limited = QRadioButton("Interpolate gaps ≤ N years")
        self.radio_missing_interp_limited.setToolTip(
            "Linearly interpolate interior gaps for the same month when the gap length does not exceed N."
        )
        self.spin_missing_local_k = _spin(1, 100, 5,
                                          "K controls the local window fill: the algorithm collects up to K valid values before and K after a missing year (per month). "
                                          "Missing values are skipped; the search continues outward. A value is filled only if at least K valid values are found in total.", 55)
        self.spin_missing_interp_gap = _spin(1, 100, 3,
                                             "N controls linear interpolation: only interior gaps with at most N consecutive missing points are filled (per month). "
                                             "Leading/trailing missing values remain missing.", 55)
        self.radio_missing_ignore.setToolTip(
            "How missing values in monthly climate data are handled.\n"
            "Sentinels like -999 are treated as missing.\n"
            "- Ignore: window sums/means use available months (NaN-aware).\n"
            "- Strict: any missing month makes the window NaN for that year.\n"
            "- Fill mean: replace NaNs with the long-term mean for each month.\n"
            "- Local mean: replace NaNs with the mean of nearby years for the same month (uses K).\n"
            "- Linear: fill interior gaps by linear interpolation if the gap length is ≤ N."
        )
        self._missing_group = QButtonGroup(self)
        for rb in (self.radio_missing_ignore, self.radio_missing_strict, self.radio_missing_fill_mean,
                   self.radio_missing_local, self.radio_missing_interp_limited):
            self._missing_group.addButton(rb)
        missing_box = QWidget()
        mg = QGridLayout(missing_box)
        mg.setContentsMargins(0, 0, 0, 0)
        mg.setHorizontalSpacing(8)
        mg.setVerticalSpacing(2)
        mg.addWidget(self.radio_missing_ignore, 0, 0)
        mg.addWidget(self.radio_missing_strict, 0, 1)
        mg.addWidget(self.radio_missing_fill_mean, 1, 0)
        mg.addWidget(_pair(self.radio_missing_local, self.spin_missing_local_k), 1, 1)
        mg.addWidget(_pair(self.radio_missing_interp_limited, self.spin_missing_interp_gap), 2, 0, 1, 2)

        def _update_missing_controls():
            self.spin_missing_local_k.setEnabled(self.radio_missing_local.isChecked())
            self.spin_missing_interp_gap.setEnabled(self.radio_missing_interp_limited.isChecked())

        for rb in (self.radio_missing_ignore, self.radio_missing_strict, self.radio_missing_fill_mean,
                   self.radio_missing_local, self.radio_missing_interp_limited):
            rb.toggled.connect(_update_missing_controls)
        _update_missing_controls()

        col2 = QFormLayout()
        col2.setLabelAlignment(Qt.AlignRight)
        col2.setVerticalSpacing(4)
        col2.addRow("Show top N:", _pair(self.spin_top_n, self.combo_top_mode))
        col2.addRow("Report: top K / pair:", self.spin_report_k)
        col2.addRow("Missing climate:", missing_box)
        _res = system_resources()
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(0, max(1, int(_res["cpu"])))
        self.spin_workers.setValue(0)
        self.spin_workers.setSpecialValueText(f"Auto (up to {safe_worker_count(10 ** 6)})")
        self.spin_workers.setMaximumWidth(150)
        self.spin_workers.setToolTip(
            "Parallel processes for the analysis. Auto uses the computer's logical CPUs minus one, limited by the free "
            f"memory (this computer: {int(_res['cpu'])} logical CPUs, "
            f"{_res['mem_avail_mb'] / 1024:.1f} of {_res['mem_total_mb'] / 1024:.1f} GB free). "
            "Each chronology x dataset pair is one task; small analyses run in one process."
        )
        self.chk_reuse_pairs = QCheckBox("Re-use unchanged pairs")
        self.chk_reuse_pairs.setChecked(True)
        self.chk_reuse_pairs.setToolTip(
            "Pairs (chronology x dataset) already computed in this session with the same settings and unchanged data "
            "are not computed again: only the new datasets are screened, the table shows all selected pairs "
            "without duplicates. Untick to compute everything again.")
        col2.addRow("CPU processes:", _pair(self.spin_workers, self.chk_reuse_pairs))
        p_layout.addLayout(col2, 3)

        # column 3: multi-year periods
        period_group = QGroupBox("Multi-year periods")
        period_group.setStyleSheet("QGroupBox { font-weight: 400; }")
        pf = QFormLayout()
        pf.setLabelAlignment(Qt.AlignRight)
        pf.setVerticalSpacing(3)
        self.chk_enable_periods = QCheckBox("Enable")
        self.chk_enable_periods.setChecked(False)
        self.chk_enable_periods.setToolTip(
            "When enabled, correlations are also computed over multiple sub-periods using the settings below."
        )
        self.spin_period_min_span = _spin(2, 500, 50,
                                          "Minimal period span (years). In Anchored/Grid modes this is the minimum allowed length; "
                                          "in Sliding it is also the fixed window length.", 70)
        self.spin_period_step = _spin(1, 100, 5,
                                      "Step (years) for shifting period boundaries: Anchored trims edges by this step; "
                                      "Sliding moves fixed-length windows by this step; Grid uses it as the start/end grid step.", 70)
        self.spin_min_period_points = _spin(5, 5000, 10,
                                            "Minimal number of non-missing observations required inside the period to compute correlation.", 70)
        self.radio_period_anchored = QRadioButton("Anchored")
        self.radio_period_sliding = QRadioButton("Sliding")
        self.radio_period_grid = QRadioButton("Grid")
        self.radio_period_anchored.setChecked(True)
        self.radio_period_anchored.setToolTip(
            "Anchored mode: trim from the earliest/latest side by the chosen step until the minimal span is reached "
            "(e.g., 1910–Ymax, 1920–Ymax, and Ymin–1990, Ymin–1980, ...)."
        )
        self.radio_period_sliding.setToolTip(
            "Sliding mode: use a fixed length equal to Minimal period span and slide the window by the step."
        )
        self.radio_period_grid.setToolTip(
            "Exhaustive mode (experimental): start/end grid with the given step; only intervals of at least the minimal span are kept "
            "(may be slow)."
        )
        self.period_mode_group = QButtonGroup(self)
        for rb in (self.radio_period_anchored, self.radio_period_sliding, self.radio_period_grid):
            self.period_mode_group.addButton(rb)
        pf.addRow(self.chk_enable_periods)
        pf.addRow("Min. span (years):", self.spin_period_min_span)
        pf.addRow("Step (years):", self.spin_period_step)
        pf.addRow("Min. points:", self.spin_min_period_points)
        pf.addRow("Mode:", _pair(self.radio_period_anchored, self.radio_period_sliding, self.radio_period_grid))
        period_group.setLayout(pf)
        p_layout.addWidget(period_group, 2)

        group_params.setLayout(p_layout)
        params_lay.addWidget(group_params)
        params_lay.addStretch(1)

        params_scroll = QScrollArea()
        params_scroll.setWidgetResizable(True)
        params_scroll.setFrameShape(QScrollArea.NoFrame)
        params_scroll.setWidget(params_host)

        self.analysis_splitter = QSplitter(Qt.Vertical)
        self.analysis_splitter.setChildrenCollapsible(False)
        self.analysis_splitter.addWidget(group_sets)
        self.analysis_splitter.addWidget(params_scroll)
        self.analysis_splitter.setStretchFactor(0, 3)
        self.analysis_splitter.setStretchFactor(1, 2)
        self.analysis_splitter.setSizes([600, 260])
        layout.addWidget(self.analysis_splitter, 1)
        # Connect selection / checkbox changes to common-period handler
        self.list_chron_for_analysis.itemSelectionChanged.connect(
            self._update_year_range_from_selection
        )
        self.list_climate_for_analysis.itemSelectionChanged.connect(
            self._update_year_range_from_selection
        )
        # Double-click any dataset in Analysis lists to preview it (no extra preview panel).
        self.list_chron_for_analysis.itemDoubleClicked.connect(self._open_dataset_preview_from_item)
        self.list_climate_for_analysis.itemDoubleClicked.connect(self._open_dataset_preview_from_item)
        self.chk_common_period.toggled.connect(
            self._on_common_period_toggled
        )

        self.tab_analysis.setLayout(layout)

    def show_analysis_help(self):
        """Open the Help dialog at the Analysis section."""
        self.show_help(anchor="analysis")

    def _on_common_period_toggled(self, checked: bool) -> None:
        """
        Handle toggling of common-period checkbox.

        When enabled:
            - adjust year range to the overlapping period across selected datasets.
        When disabled:
            - reset year range to the global full period across all datasets.
        """
        if checked:
            # Turned ON: compute common period from the current selection
            self._update_year_range_from_selection()
            return

        # Turned OFF: restore full global period (as in the default behaviour)
        global_min = getattr(self, "_global_min_year", None)
        global_max = getattr(self, "_global_max_year", None)
        if global_min is None or global_max is None:
            return

        self.spin_year_start.blockSignals(True)
        self.spin_year_end.blockSignals(True)
        self.spin_year_start.setValue(global_min)
        self.spin_year_end.setValue(global_max)
        self.spin_year_start.blockSignals(False)
        self.spin_year_end.blockSignals(False)


    def _update_year_range_from_selection(self) -> None:
        """
        If 'Use common period for selected datasets' is enabled, adjust
        year start/end spin boxes to the overlapping (common) period across
        all selected chronology and climate datasets.
        """
        if not hasattr(self, "chk_common_period") or not self.chk_common_period.isChecked():
            return

        selected_datasets: list[DataSet] = []
        for lst in (self.list_chron_for_analysis, self.list_climate_for_analysis):
            for item in lst.selectedItems():
                ds = item.data(Qt.UserRole)
                if isinstance(ds, DataSet) and ds not in selected_datasets:
                    selected_datasets.append(ds)

        if not selected_datasets:
            return

        min_years: list[int] = []
        max_years: list[int] = []
        ds_year_ranges: list[tuple[DataSet, int, int]] = []
        for ds in selected_datasets:
            try:
                if ds.year_col not in ds.df.columns:
                    continue
                years = pd.to_numeric(ds.df[ds.year_col], errors="coerce")
                years = years.dropna()
                if years.empty:
                    continue
                y_min = int(years.min())
                y_max = int(years.max())
                min_years.append(y_min)
                max_years.append(y_max)
                ds_year_ranges.append((ds, y_min, y_max))
            except Exception:
                continue

        if not min_years or not max_years:
            return

        common_start = max(min_years)
        common_end = min(max_years)
        if common_start > common_end:
            # Ask once: keep all (disable common period) or exclude datasets to make overlap possible.
            mb = QMessageBox(self)
            mb.setWindowTitle("Common period")
            mb.setIcon(QMessageBox.Question)
            mb.setText(
                "Selected datasets have no overlapping years.\n\n"
                "Choose what to do:"
            )
            btn_keep = mb.addButton("Keep all (use own period)", QMessageBox.AcceptRole)
            btn_exclude = mb.addButton("Exclude conflicting datasets", QMessageBox.DestructiveRole)
            btn_cancel = mb.addButton(QMessageBox.Cancel)
            mb.exec()

            clicked = mb.clickedButton()
            if clicked == btn_cancel:
                return

            if clicked == btn_keep:
                # Disable common period and restore global range.
                try:
                    self.chk_common_period.blockSignals(True)
                    self.chk_common_period.setChecked(False)
                    self.chk_common_period.blockSignals(False)
                except Exception:
                    pass
                self._on_common_period_toggled(False)
                return

            # Exclude datasets until an overlap exists (best-effort heuristic).
            remaining = list(ds_year_ranges)
            removed: list[DataSet] = []
            # Prevent repeated UI updates/messages while we modify selections.
            removed_names: list[str] = []
            blocked_lists: list[QListWidget] = []

            def _overlap(ranges: list[tuple[DataSet, int, int]]) -> tuple[int, int]:
                if not ranges:
                    return (0, -1)
                a = max(r[1] for r in ranges)
                b = min(r[2] for r in ranges)
                return (a, b)

            while len(remaining) >= 2:
                a, b = _overlap(remaining)
                if a <= b:
                    break

                # Candidates are those on the limiting boundaries.
                cand_idx = [
                    i
                    for i, (_, y0, y1) in enumerate(remaining)
                    if y0 == a or y1 == b
                ]
                if not cand_idx:
                    cand_idx = list(range(len(remaining)))

                best_i = cand_idx[0]
                best_score = None
                for i in cand_idx:
                    tmp = [r for j, r in enumerate(remaining) if j != i]
                    na, nb = _overlap(tmp)
                    score = nb - na
                    span = remaining[i][2] - remaining[i][1]
                    key = (score, -span)
                    if best_score is None or key > best_score:
                        best_score = key
                        best_i = i

                removed.append(remaining[best_i][0])
                remaining.pop(best_i)

            a2, b2 = _overlap(remaining)
            if len(remaining) < 2 or a2 > b2:
                QMessageBox.warning(
                    self,
                    "Common period",
                    "Could not find an overlapping period after excluding datasets.",
                )
                try:
                    self.chk_common_period.blockSignals(True)
                    self.chk_common_period.setChecked(False)
                    self.chk_common_period.blockSignals(False)
                except Exception:
                    pass
                self._on_common_period_toggled(False)
                return

            # Deselect removed datasets in both lists.
            removed_set = set(removed)
            try:
                removed_names = [getattr(ds, "name", "Unnamed") for ds in removed]
            except Exception:
                removed_names = []

            # Block signals to avoid selection-changed handlers firing repeatedly (and spamming dialogs).
            for lst in (self.list_chron_for_analysis, self.list_climate_for_analysis):
                try:
                    lst.blockSignals(True)
                    blocked_lists.append(lst)
                except Exception:
                    pass

            try:
                for lst in (self.list_chron_for_analysis, self.list_climate_for_analysis):
                    try:
                        for i in range(lst.count()):
                            it = lst.item(i)
                            ds = it.data(Qt.UserRole)
                            if isinstance(ds, DataSet) and ds in removed_set:
                                it.setSelected(False)
                    except Exception:
                        pass
            finally:
                for lst in blocked_lists:
                    try:
                        lst.blockSignals(False)
                    except Exception:
                        pass

            # Update year range using the remaining selection.
            self._update_year_range_from_selection()
            if removed_names:
                QMessageBox.information(
                    self,
                    "Common period",
                    "Excluded datasets:\n- " + "\n- ".join(removed_names),
                )
            return

        self.spin_year_start.blockSignals(True)
        self.spin_year_end.blockSignals(True)
        self.spin_year_start.setValue(common_start)
        self.spin_year_end.setValue(common_end)
        self.spin_year_start.blockSignals(False)
        self.spin_year_end.blockSignals(False)

    def _get_dataset_year_range(self, ds: Any) -> Optional[tuple[int, int]]:
        """Return (min_year, max_year) for a dataset or None if not available."""
        try:
            df = getattr(ds, "df", None)
            year_col = getattr(ds, "year_col", None)
            if df is None or year_col is None or year_col not in df.columns:
                return None

            s = df[year_col]
            # Support both numeric year columns and datetime columns
            if pd.api.types.is_datetime64_any_dtype(s):
                years = pd.to_datetime(s, errors="coerce").dt.year
            else:
                years = pd.to_numeric(s, errors="coerce")

            years = years.dropna()
            if years.empty:
                return None
            y0 = int(years.min())
            y1 = int(years.max())
            return (y0, y1)
        except Exception:
            return None

    def _get_selected_datasets_for_analysis(self) -> list[DataSet]:
        """Collect selected datasets from both analysis lists (chron + target list)."""
        selected: list[DataSet] = []
        for lst in (self.list_chron_for_analysis, self.list_climate_for_analysis):
            for item in lst.selectedItems():
                ds = item.data(Qt.UserRole)
                if isinstance(ds, DataSet):
                    selected.append(ds)
        return selected

    def _update_selected_periods_label(self) -> None:
        """Update the label that shows year coverage for all currently selected datasets."""
        if not hasattr(self, "lbl_selected_periods"):
            return

        selected = self._get_selected_datasets_for_analysis()
        if not selected:
            self.lbl_selected_periods.setText("")
            return

        lines_out: list[str] = []
        min_years: list[int] = []
        max_years: list[int] = []
        for ds in selected:
            rng = self._get_dataset_year_range(ds)
            if rng is None:
                lines_out.append(f"{ds.name}: (no year info)")
                continue
            y0, y1 = rng
            min_years.append(y0)
            max_years.append(y1)
            lines_out.append(f"{ds.name}: {y0}–{y1}")

        # If common period is enabled, show the current overlap at the top
        if hasattr(self, "chk_common_period") and self.chk_common_period.isChecked() and min_years and max_years:
            a = max(min_years)
            b = min(max_years)
            if a <= b:
                lines_out.insert(0, f"Common period: {a}–{b}")

        self.lbl_selected_periods.setText("\n".join(lines_out))

    def preview_selected_dataset(self) -> None:
        """Open a dialog showing a quick preview of the selected dataset (Year + months)."""
        selected_items = []
        for lst in (self.list_chron_for_analysis, self.list_climate_for_analysis):
            selected_items.extend(lst.selectedItems())

        if len(selected_items) != 1:
            QMessageBox.information(
                self,
                "Preview",
                "Select exactly one dataset (in the analysis lists) to preview.",
            )
            return

        ds = selected_items[0].data(Qt.UserRole)
        if not isinstance(ds, DataSet):
            QMessageBox.warning(self, "Preview", "Selected item is not a dataset.")
            return

        df = getattr(ds, "df", None)
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            QMessageBox.information(self, "Preview", "Dataset has no data to preview.")
            return

        preview_df = self._build_preview_dataframe_for_dataset(ds)

        rng = self._get_dataset_year_range(ds)
        rng_txt = ""
        if rng is not None:
            rng_txt = f" ({rng[0]}–{rng[1]})"

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Preview: {ds.name}{rng_txt}")
        dlg.resize(900, 520)
        lay = QVBoxLayout()
        lay.addWidget(QLabel(f"Dataset: {ds.name}{rng_txt}"))
        tbl = QTableView()
        tbl.setModel(PandasModel(preview_df))
        tbl.resizeColumnsToContents()
        lay.addWidget(tbl)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.setLayout(lay)
        dlg.exec()

    # ---- Results tab ----
    def _init_tab_results(self):
        layout = QVBoxLayout()
        view_layout = QHBoxLayout()
        view_layout.addWidget(QLabel("Show:"))
        self.combo_results_view = QComboBox()
        self.combo_results_view.addItems(["Main correlations", "Period correlations"])
        self.combo_results_view.currentIndexChanged.connect(lambda *_a: self.apply_result_filters())
        view_layout.addWidget(self.combo_results_view)
        view_layout.addSpacing(12)
        view_layout.addWidget(QLabel("Export:"))
        self.combo_results_export = QComboBox()
        self.combo_results_export.addItems(
            [
                "Export...",
                "Export results",
                "Export climate windows...",
                "Export merged data for selected result",
                "Save plot PNG",
            ]
        )
        self.combo_results_export.setCurrentIndex(0)
        self.combo_results_export.currentIndexChanged.connect(self._on_results_export_changed)
        view_layout.addWidget(self.combo_results_export)
        view_layout.addSpacing(12)
        btn_spatial_r = QPushButton("Spatial correlation...")
        btn_spatial_r.setToolTip("Summary of these results, suggested correlation maps and the map page")
        btn_spatial_r.clicked.connect(self.open_field_panel)
        view_layout.addWidget(btn_spatial_r)
        btn_moving_r = QPushButton("Moving correlation...")
        btn_moving_r.setToolTip("Temporal stability of the strongest climate signals (running correlations)")
        btn_moving_r.clicked.connect(self.open_moving_panel)
        view_layout.addWidget(btn_moving_r)
        view_layout.addStretch(1)
        layout.addLayout(view_layout)
        layout.addWidget(self._build_results_filter_bar())
        self.table_results = QTableView()
        self.table_results.setAlternatingRowColors(True)
        self.table_results.setToolTip(
            "Click a row (or move with the arrow keys) to plot it. Ctrl+C copies cells, "
            "Ctrl+Shift+C copies with headers, right-click for more."
        )
        graph_group = QGroupBox("Plots for selected row / pair")
        g_layout = QVBoxLayout()
        plot_ctl = QHBoxLayout()
        plot_ctl.addWidget(QLabel("Plot:"))
        self.combo_plot_mode = QComboBox()
        self.combo_plot_mode.addItems(PLOT_MODES)
        self.combo_plot_mode.setToolTip(
            "Overview: monthly correlations, window heatmap, standardised series and running correlation"
        )
        plot_ctl.addWidget(self.combo_plot_mode)
        plot_ctl.addSpacing(12)
        plot_ctl.addWidget(QLabel("Running window:"))
        self.spin_running_window = QSpinBox()
        self.spin_running_window.setRange(11, 101)
        self.spin_running_window.setSingleStep(2)
        self.spin_running_window.setValue(31)
        self.spin_running_window.setSuffix(" yr")
        plot_ctl.addWidget(self.spin_running_window)
        plot_ctl.addStretch(1)
        g_layout.addLayout(plot_ctl)
        self.canvas = MplCanvas()
        self.plot_toolbar = NavigationToolbar2QT(self.canvas, graph_group)
        if _uihelp is not None and hasattr(_uihelp, "mpl_wheel_zoom"):
            _uihelp.mpl_wheel_zoom(self.canvas)  # mouse wheel zooms around the cursor
        g_layout.addWidget(self.plot_toolbar)
        g_layout.addWidget(self.canvas, 1)
        graph_group.setLayout(g_layout)
        results_split = QSplitter(Qt.Vertical)
        results_split.addWidget(self.table_results)
        results_split.addWidget(graph_group)
        results_split.setStretchFactor(0, 2)
        results_split.setStretchFactor(1, 3)
        results_split.setChildrenCollapsible(False)
        layout.addWidget(results_split, 1)
        self.tab_results.setLayout(layout)
        self.combo_plot_mode.currentIndexChanged.connect(lambda _i: self._replot_current_result())
        self.spin_running_window.valueChanged.connect(lambda _v: self._replot_current_result())
        self.table_results.clicked.connect(self.update_plot_from_selection)

    # ---- Report tab ----
    def _init_tab_report(self):
        layout = QVBoxLayout()
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Report:"))
        btn_export = QPushButton("Export report")
        btn_export.clicked.connect(self.export_report_csv)
        top_row.addWidget(btn_export)
        top_row.addStretch(1)
        layout.addLayout(top_row)
        self.table_report = QTableView()
        self.table_report.setAlternatingRowColors(True)
        layout.addWidget(self.table_report)
        self.tab_report.setLayout(layout)

    def preview_selected_timeseries_monthly(self) -> None:
        """Preview the transformed table built from the selected daily/hourly dataset."""
        item = self.list_climate_for_analysis.currentItem()
        if item is None:
            selected = self.list_climate_for_analysis.selectedItems()
            item = selected[0] if selected else None
        if item is None:
            QMessageBox.information(
                self,
                "Preview",
                "Select one dataset on the right side first.",
            )
            return

        ds = item.data(Qt.UserRole)
        if not isinstance(ds, DataSet) or ds.kind != "timeseries":
            QMessageBox.information(
                self,
                "Preview",
                "The selected dataset is not a daily/hourly time-series dataset.",
            )
            return

        agg_choice = (
            self.combo_timeseries_monthly_agg.currentText()
            if hasattr(self, "combo_timeseries_monthly_agg")
            else "Auto (by variable)"
        )
        resolution = self._get_analysis_resolution_mode() if hasattr(self, "_get_analysis_resolution_mode") else "monthly"
        try:
            if resolution == "daily":
                preview_df = timeseries_to_daily_wide_dataframe(ds, agg_choice).head(20).copy()
                preview_title = "Preview daily table"
                preview_note = (
                    f"Dataset: {ds.name}\n"
                    f"Source frequency: {getattr(ds, 'source_freq', '') or (getattr(ds, 'meta', {}) or {}).get('frequency', '')}\n"
                    f"Base aggregation: {agg_choice}"
                )
            else:
                ds_monthly = timeseries_to_monthly_climate_dataset(ds, agg_choice)
                preview_df = ds_monthly.df.head(30).copy()
                preview_title = "Preview monthly table"
                preview_note = (
                    f"Dataset: {ds.name}\n"
                    f"Source frequency: {getattr(ds, 'source_freq', '') or (getattr(ds, 'meta', {}) or {}).get('frequency', '')}\n"
                    f"Monthly aggregation: {getattr(ds_monthly, 'monthly_agg', agg_choice)}"
                )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Preview error",
                f"Could not build a preview table from the selected dataset:\n{exc}",
            )
            return

        rng = self._get_dataset_year_range_safe(ds)
        rng_txt = f" ({rng[0]}–{rng[1]})" if rng is not None else ""

        dlg = QDialog(self)
        dlg.setWindowTitle(f"{preview_title}: {ds.name}{rng_txt}")
        dlg.resize(900, 520)
        lay = QVBoxLayout()
        lay.addWidget(QLabel(preview_note))
        tbl = QTableView()
        tbl.setModel(PandasModel(preview_df))
        tbl.resizeColumnsToContents()
        lay.addWidget(tbl)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.setLayout(lay)
        dlg.exec()

    def _on_results_export_changed(self, index: int) -> None:
        """Execute the selected Results export action immediately."""
        if index <= 0 or not hasattr(self, "combo_results_export"):
            return
        action = self.combo_results_export.currentText()
        try:
            if action == "Export results":
                self.export_results_csv()
            elif action == "Export climate windows...":
                self.export_windows_csv()
            elif action == "Export merged data for selected result":
                self.export_merged_csv()
            elif action == "Save plot PNG":
                self.export_plot_png()
        finally:
            self.combo_results_export.blockSignals(True)
            self.combo_results_export.setCurrentIndex(0)
            self.combo_results_export.blockSignals(False)

    # -----------------------
    # helper
    # -----------------------
    def _analysis_list_panel(self, title: QLabel, lw: QListWidget) -> QWidget:
        """List with a title, live counter, filter box and All / None buttons."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        head = QHBoxLayout()
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        head.addWidget(title)
        count = QLabel("")
        count.setStyleSheet("color: #52514e;")
        head.addWidget(count)
        head.addStretch(1)
        flt = QLineEdit()
        flt.setPlaceholderText("Filter...")
        flt.setClearButtonEnabled(True)
        flt.setMaximumWidth(220)
        head.addWidget(flt)
        b_all = QPushButton("All")
        b_none = QPushButton("None")
        for b in (b_all, b_none):
            b.setMaximumWidth(60)
            head.addWidget(b)
        v.addLayout(head)
        v.addWidget(lw, 1)

        def update_count() -> None:
            try:
                lw.count()
            except RuntimeError:  # window closed
                return
            n = lw.count()
            sel = len(lw.selectedItems())
            shown = sum(1 for i in range(n) if not lw.item(i).isHidden())
            count.setText(f"{sel} of {n} selected" + (f", {shown} shown" if shown != n else ""))

        def apply_filter(text: str) -> None:
            words = [t for t in text.lower().split() if t]
            for i in range(lw.count()):
                it = lw.item(i)
                it.setHidden(bool(words) and not all(t in it.text().lower() for t in words))
            update_count()

        def select_visible(on: bool) -> None:
            lw.blockSignals(True)
            for i in range(lw.count()):
                it = lw.item(i)
                if not it.isHidden():
                    it.setSelected(on)
            lw.blockSignals(False)
            lw.itemSelectionChanged.emit()

        flt.textChanged.connect(apply_filter)
        b_all.clicked.connect(lambda: select_visible(True))
        b_none.clicked.connect(lambda: select_visible(False))
        lw.itemSelectionChanged.connect(update_count)
        lw.model().rowsInserted.connect(lambda *_a: (apply_filter(flt.text())))
        lw.model().rowsRemoved.connect(lambda *_a: update_count())
        lw.model().modelReset.connect(lambda *_a: update_count())
        lw.setToolTip("Click to select or deselect; double-click to preview. Words in the filter must all match.")
        return w

    def _wrap_with_label(self, text: str, widget: QWidget) -> QWidget:
        w = QWidget()
        l = QVBoxLayout()
        l.addWidget(QLabel(text))
        l.addWidget(widget)
        w.setLayout(l)
        return w

    def _try_import_grdc_monthly(self, path: str) -> bool:
        """
        Try to auto-import a GRDC-like monthly discharge file (Q_Month.txt).

        Logic is maximally tolerant:
        - ignore all lines starting with '#' (after lstrip);
        - expect text file with ';' separator;
        - find one column that parses well as date;
        - find one numeric column with >= 12 non-zero values;
        - build Year x Month = 12 months and create climate DataSet.

        Returns:
            True  - if file recognized and dataset created;
            False - if structure not suitable (then caller goes to normal dialog).
        """
        try:
            name = os.path.basename(path)

            # --- 1. Read all lines and manually remove comments/empties ---
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    raw_lines = f.readlines()
            except Exception:
                return False

            data_lines: list[str] = []
            for line in raw_lines:
                if not line.strip():
                    continue
                # ignore lines where first non-whitespace char is '#'
                if line.lstrip().startswith("#"):
                    continue
                data_lines.append(line)

            if not data_lines:
                return False

            # quick test: is there ';' as separator?
            if not any(";" in ln for ln in data_lines[:20]):
                return False

            # --- 2. Assemble temporary buffer and read without comment='#' ---
            buffer = io.StringIO("".join(data_lines))
            df = pd.read_csv(
                buffer,
                sep=";",
                header=0,
                engine="python",
            )
            df = _drop_empty_columns(df)

            if df is None or df.empty or df.shape[1] < 2:
                return False

            # --- 3. Find date column ---
            date_col: Optional[str] = None
            cols = list(df.columns.astype(str))

            for col in cols:
                s = df[col].dropna().astype(str)
                if s.empty:
                    continue
                parsed = pd.to_datetime(s, errors="coerce", format=None)
                ok_ratio = parsed.notna().mean()
                # at least 80% values parse successfully as dates
                if ok_ratio >= 0.8:
                    date_col = col
                    break

            if date_col is None:
                return False

            # --- 4. Find numeric column (discharge) ---
            # Prefer the column with the highest number of valid values after removing
            # typical GRDC missing codes (e.g. -999). If both 'Original' and 'Calculated'
            # exist, build a combined series that uses Calculated when available and
            # falls back to Original otherwise.
            numeric_candidates: list[str] = []
            for col in cols:
                if col == date_col:
                    continue
                s_num = pd.to_numeric(df[col], errors="coerce")
                if s_num.notna().sum() >= 12:
                    numeric_candidates.append(col)

            if not numeric_candidates:
                return False


            def _good_count(colname: str) -> int:
                s = pd.to_numeric(df[colname], errors="coerce")
                s = s.mask(s <= -900)
                return int(s.notna().sum())

            orig_col = next(
                (c for c in numeric_candidates if "ORIGINAL" in str(c).upper()), None
            )
            calc_col = next(
                (c for c in numeric_candidates if "CALCULATED" in str(c).upper()), None
            )

            value_col: str
            if orig_col is not None and calc_col is not None:
                value_col = "__combined__"
            elif calc_col is not None:
                value_col = str(calc_col)
            else:
                value_col = str(max(numeric_candidates, key=_good_count))

            # --- 5. Clean and build Year × Month ---
            df = df.copy()
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce", format=None)
            df = df[df[date_col].notna()].copy()
            if df.empty:
                return False

            if value_col == "__combined__":
                s_calc = pd.to_numeric(df[calc_col], errors="coerce")  # type: ignore[index]
                s_orig = pd.to_numeric(df[orig_col], errors="coerce")  # type: ignore[index]
                s_calc = s_calc.mask(s_calc <= -900)
                s_orig = s_orig.mask(s_orig <= -900)
                df["_Value"] = s_calc.where(s_calc.notna(), s_orig)
                value_col_used = "_Value"
            else:
                df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
                # typical GRDC missing codes: strongly negative
                df.loc[df[value_col] <= -900, value_col] = np.nan
                value_col_used = value_col


            df["Year"] = df[date_col].dt.year.astype(int)
            df["Month"] = df[date_col].dt.month.astype(int)

            pivot = df.pivot_table(
                index="Year",
                columns="Month",
                values=value_col_used,
                aggfunc="mean",
            )
            if pivot is None or pivot.empty:
                return False

            # ensure 12 months columns 1..12
            pivot = pivot.reindex(columns=list(range(1, 13)))
            month_cols: list[str] = []
            for m in range(1, 13):
                src = m
                if src not in pivot.columns:
                    pivot[src] = np.nan
                month_cols.append(f"Y{m}")
            pivot = pivot.reset_index()
            for m in range(1, 13):
                if m in pivot.columns:
                    pivot.rename(columns={m: f"Y{m}"}, inplace=True)

            # now we have Year + Y1..Y12
            cols_final = ["Year"] + [f"Y{i}" for i in range(1, 13) if f"Y{i}" in pivot.columns]
            pivot = pivot[cols_final]

            # --- 6. Create climate DataSet ---
            ds = DataSet(name=name, path=path, df=pivot, kind="climate")
            ds.year_col = "Year"
            ds.month_columns = month_cols
            ds.climate_param = "Runoff"  # reasonable default for Q_Month

            self.datasets.append(ds)
            item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
            item.setData(Qt.UserRole, ds)
            self.list_datasets.addItem(item)
    
            # NEW: select the newly added dataset and switch to Data tab
            self.list_datasets.setCurrentItem(item)
            # if you have a method to update preview on selection signal, it will trigger automatically
    
            # switch user to Data tab so they see immediately what was added
            try:
                self.tabs.setCurrentWidget(self.tab_data)
            except Exception:
                # if tabs are not created yet — just ignore
                pass
    
            self.refresh_analysis_lists()
            return True
    
        except Exception as e:
            print("GRDC monthly auto-import failed:", e)
            return False

    def _try_import_combined_station_monthly_csv(self, path: str) -> bool:
        """Import a combined station-by-year monthly table (many stations in one CSV).

        Expected columns (case-insensitive):
        Country, Code_WMO, Name, ... , year, jan..dec

        Creates one climate dataset per station with name:
        Name (Code_WMO_Country)
        """
        try:
            sep = _guess_separator(path)
            df = _read_csv_robust(
                path,
                sep=sep,
                header=0,
                skiprows=0,
                comment=None,
            )
        except Exception:
            return False

        if df is None or df.empty:
            return False

        detected = _detect_monthly_wide_columns(df)
        if detected is None:
            return False

        station_meta = detected.get("station_meta", {}) or {}
        country_col = station_meta.get("country")
        code_col = station_meta.get("code_wmo")
        name_col = station_meta.get("name")
        year_col = detected["year_col"]
        month_src = detected["month_cols"]

        if not name_col and not code_col:
            return False

        meta_cols = [c for c in [country_col, code_col, name_col] if c]
        df_sub = df[meta_cols + [year_col] + month_src].copy()

        # Treat -999 style sentinels as missing values.
        df_sub = _replace_sentinel_missing(df_sub, month_src)

        imported = 0
        excluded: List[str] = []

        group_cols = [c for c in [code_col, name_col, country_col] if c]
        for key_vals, g in df_sub.groupby(group_cols, dropna=False):
            if not isinstance(key_vals, tuple):
                key_vals = (key_vals,)
            key_map = {col: val for col, val in zip(group_cols, key_vals)}
            country = key_map.get(country_col, "")
            code = key_map.get(code_col, "")
            st_name = key_map.get(name_col, "")

            g2 = g[[year_col] + month_src].copy()
            g2 = g2.rename(columns={year_col: "Year", **{src: dst for src, dst in zip(month_src, MONTH_NAMES)}})

            # Drop years with no data (all 12 months missing).
            g2 = _prepare_monthly_climate_df_for_analysis(
                g2,
                year_col="Year",
                month_cols=MONTH_NAMES,
                missing_policy="ignore",
            )

            if g2.empty:
                ds_label = f"{st_name} ({code}_{country})"
                excluded.append(ds_label)
                continue

            ds_name = f"{st_name} ({code}_{country})"
            ds = DataSet(name=str(ds_name), path=path, df=g2, kind="climate")
            ds.year_col = "Year"
            ds.month_columns = MONTH_NAMES
            ds.climate_param = "Precipitation"

            self.datasets.append(ds)
            item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
            item.setData(Qt.UserRole, ds)
            self.list_datasets.addItem(item)
            imported += 1

        if imported <= 0:
            return False

        self.refresh_analysis_lists()

        msg = f"Imported {imported} station dataset(s) from {os.path.basename(path)}."
        if excluded:
            msg += "\n\nExcluded (no data after removing missing years):\n" + "\n".join(excluded)
        QMessageBox.information(self, "Import finished", msg)
        return True

    # -----------------------
    # Import logic (uses FileImportDialog)
    # -----------------------
    def import_any_file(self):
        """UI entry: ask for a single file and import it."""
        start_dir = self.last_dir_open or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open file",
            start_dir,
            "All files (*.*)",
        )
        if not path:
            return
        try:
            self.last_dir_open = os.path.dirname(path) or self.last_dir_open
            self.save_settings()
        except Exception:
            pass
        self._import_single_path(path)

    def import_multiple_files(self):
        """UI entry: select and import multiple files one by one."""
        start_dir = self.last_dir_open or os.path.expanduser("~")
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open multiple files",
            start_dir,
            "All files (*.*)",
        )
        if not paths:
            return
        try:
            self.last_dir_open = os.path.dirname(paths[0]) or self.last_dir_open
            self.save_settings()
        except Exception:
            pass
        imported = 0
        batch_ctx = {
            "is_batch": True,
            "grdc_open_dialog": None,
            # Cache first manual import mapping and reuse it for the rest (per extension).
            # This avoids opening the mapping dialog for every file in multi-import.
            "mapping_cache_by_ext": {},
            "reuse_mapping_by_ext": False,
            "excluded": [],
            "manual_imported": 0,
            "auto_imported": 0,
            "skipped_unrecognized": [],
            "auto_detect_signature": None,
            "auto_detect_review_pref": None,
        }
        for path in paths:
            try:
                ok = self._import_single_path(path, batch_ctx=batch_ctx)
                if ok:
                    imported += 1
            except Exception as e:
                # Do not crash on one bad file, just report to console.
                print(f"Error importing {path}: {e}", flush=True)

        if imported <= 0:
            msg = "No files were imported."
        else:
            msg = f"Imported {imported} file(s)."

        manual_imported = int(batch_ctx.get("manual_imported", 0) or 0)
        auto_imported = int(batch_ctx.get("auto_imported", 0) or 0)
        summary_parts = []
        if manual_imported > 0:
            summary_parts.append(f"Manual: {manual_imported}")
        if auto_imported > 0:
            summary_parts.append(f"Automatic: {auto_imported}")
        if summary_parts:
            msg += "\n\n" + "\n".join(summary_parts)

        excluded = batch_ctx.get("excluded", []) if isinstance(batch_ctx, dict) else []
        if excluded:
            excluded_names = [os.path.basename(p) for p in excluded]
            msg += "\n\nExcluded (empty data):\n" + "\n".join(excluded_names)

        skipped_unrecognized = batch_ctx.get("skipped_unrecognized", []) if isinstance(batch_ctx, dict) else []
        if skipped_unrecognized:
            skipped_names = [os.path.basename(p) for p in skipped_unrecognized]
            msg += "\n\nNot imported (not confidently recognized):\n" + "\n".join(skipped_names)

        QMessageBox.information(self, "Import finished", msg)

    def _import_single_path(self, path: str, batch_ctx: Optional[dict] = None) -> bool:
        """Import a single file given its path (used by single and multiple import). Returns True if something was imported."""
    
        if not path:
            return False

        name = os.path.basename(path)
        did_import = False
        is_batch = bool(batch_ctx.get("is_batch")) if isinstance(batch_ctx, dict) else False

        def _mark_batch_import(mode: str) -> None:
            """Record one imported file in batch summary counters."""
            if not isinstance(batch_ctx, dict):
                return
            key = "manual_imported" if mode == "manual" else "auto_imported"
            batch_ctx[key] = int(batch_ctx.get(key, 0) or 0) + 1

        try:
            # ==== Excel files ====
            if path.lower().endswith((".xls", ".xlsx")):
                # Read only sheet names first
                sheet_names = _get_excel_sheet_names(path)
                if len(sheet_names) > 1:
                    sheets_list = list(sheet_names)
                    sel, ok = self.choose_from_list_dialog(
                        "Select sheets to import",
                        "Choose sheets (Ctrl+click multi):",
                        sheets_list,
                    )
                    if not ok or not sel:
                        return False
                    chosen = sel
                else:
                    chosen = list(sheet_names)

                for sh in chosen:
                    while True:
                        dlg = FileImportDialog(
                            df=None,
                            file_path=path,
                            sheet_name=sh,
                            parent=self,
                            batch_ctx=batch_ctx,
                        )
                        mappings = dlg.get_mappings()
                        if mappings is None:
                            break

                        has_header = mappings.get("has_header", True)
                        skiprows = mappings.get("skiprows", 0)
                        header_arg = 0 if has_header else None
                        comment = mappings.get("comment", None)  # kept for parity

                        # Read sheet once with proper header setting
                        df_sheet = _read_excel_robust(
                            path,
                            sheet_name=sh,
                            header=header_arg,
                            skiprows=skiprows,
                        )
                        df_sheet = _drop_empty_columns(df_sheet)

                        # If there is no header, assign generic column names
                        if not has_header:
                            df_sheet.columns = [
                                f"col_{i+1}" for i in range(len(df_sheet.columns))
                            ]

                        # apply comment filtering if user selected it (for Excel usually not needed)
                        if comment is not None:
                            # Already not really used for Excel, but kept for API consistency
                            pass

                        created_ok = self._create_datasets_from_mapping(
                            df_sheet,
                            name + f"::{sh}",
                            path,
                            mappings,
                        )
                        if created_ok:
                            did_import = True
                            break

                if did_import and is_batch:
                    _mark_batch_import("manual")
                elif not is_batch:
                    QMessageBox.information(
                        self,
                        "Import finished",
                        "Excel import completed.",
                    )

            else:
                grdc_monthly_detected = _is_grdc_mean_monthly_discharge_file(path)
                grdc_ts_detected = (not grdc_monthly_detected) and _is_grdc_discharge_timeseries_file(path)
                if grdc_monthly_detected and try_import_grdc_monthly(self, path):
                    # Ask once per multi-import and reuse answer for the rest.
                    open_dialog = None
                    if isinstance(batch_ctx, dict):
                        open_dialog = batch_ctx.get("grdc_open_dialog", None)
                    if open_dialog is None:
                        res = QMessageBox.question(
                            self,
                            "GRDC detected",
                            "File detected as GRDC mean monthly discharge and imported automatically.\n"
                            "Do you also want to open the import dialog for manual control?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No,
                        )
                        open_dialog = (res == QMessageBox.StandardButton.Yes)
                        if isinstance(batch_ctx, dict):
                            batch_ctx["grdc_open_dialog"] = open_dialog
                    if not open_dialog:
                        # Already imported automatically; do not open the manual import dialog.
                        if is_batch:
                            _mark_batch_import("auto")
                        return True
                    did_import = True
                elif grdc_monthly_detected:
                    if not self._ask_open_manual_import_after_auto_fail(
                        "GRDC monthly auto-import failed",
                        _append_import_debug_log_hint(
                            getattr(
                                self,
                                "_last_grdc_monthly_error",
                                "The file was detected as GRDC mean monthly discharge, "
                                "but automatic import failed for an unknown reason.",
                            )
                        ),
                        batch_ctx=batch_ctx,
                    ):
                        return False
                elif grdc_ts_detected and try_import_grdc_discharge_timeseries(self, path):
                    # Ask once per multi-import and reuse answer for the rest.
                    open_dialog = None
                    if isinstance(batch_ctx, dict):
                        open_dialog = batch_ctx.get("grdc_open_dialog", None)
                    if open_dialog is None:
                        res = QMessageBox.question(
                            self,
                            "GRDC detected",
                            "File detected as GRDC discharge time series (daily/hourly) and imported automatically.\n"
                            "Do you also want to open the import dialog for manual control?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No,
                        )
                        open_dialog = (res == QMessageBox.StandardButton.Yes)
                        if isinstance(batch_ctx, dict):
                            batch_ctx["grdc_open_dialog"] = open_dialog
                    if not open_dialog:
                        # Already imported automatically; do not open the manual import dialog.
                        if is_batch:
                            _mark_batch_import("auto")
                        return True
                    did_import = True
                elif grdc_ts_detected:
                    if not self._ask_open_manual_import_after_auto_fail(
                        "GRDC time-series auto-import failed",
                        _append_import_debug_log_hint(
                            getattr(
                                self,
                                "_last_grdc_timeseries_error",
                                "The file was detected as GRDC discharge time series, "
                                "but automatic import failed for an unknown reason.",
                            )
                        ),
                        batch_ctx=batch_ctx,
                    ):
                        return False


                # ---- 2) Fallback: normal generic import dialog ----
                # Fast-path: combined station-by-year monthly CSV (one file -> many datasets)
                try:
                    combined_station_done = self._try_import_combined_station_monthly_csv(path)
                except Exception as exc_auto:
                    combined_station_done = False
                    if not self._ask_open_manual_import_after_auto_fail(
                        "Automatic import failed",
                        _append_import_debug_log_hint(
                            "A specialized automatic import path detected the file, but it could not be completed.\n\n"
                            f"{exc_auto}\n\n"
                            f"Traceback:\n{traceback.format_exc()}"
                        ),
                        batch_ctx=batch_ctx,
                    ):
                        return False
                if combined_station_done:
                    if is_batch:
                        _mark_batch_import("auto")
                    return True

                # Reuse the first mapping settings only when the user chose automatic continuation.
                ext = os.path.splitext(path)[1].lower()
                while True:
                    reuse_mapping = bool(batch_ctx.get("reuse_mapping_by_ext")) if isinstance(batch_ctx, dict) else False
                    cache = batch_ctx.get("mapping_cache_by_ext", {}) if (isinstance(batch_ctx, dict) and reuse_mapping) else {}
                    mappings = cache.get(ext) if isinstance(cache, dict) else None
                    if mappings is not None:
                        # same layout, but every file has its own variable: detect it again for this file
                        mappings = dict(mappings)
                        bm = batch_metadata_for_file(path)
                        guess = ((bm or {}).get("climate_param") or guess_climate_param_from_metadata(path)
                                 or guess_climate_param_from_name(path, ""))
                        if guess:
                            mappings["climate_param"] = guess
                    if mappings is None:
                        dlg = FileImportDialog(
                            df=None,
                            file_path=path,
                            sheet_name=None,
                            parent=self,
                            batch_ctx=batch_ctx,
                        )
                        mappings = dlg.get_mappings()
                        if mappings is None:
                            return False
                        if isinstance(batch_ctx, dict) and reuse_mapping:
                            batch_ctx.setdefault("mapping_cache_by_ext", {})
                            if isinstance(batch_ctx.get("mapping_cache_by_ext"), dict):
                                batch_ctx["mapping_cache_by_ext"][ext] = mappings

                    has_header = mappings.get("has_header", True)
                    skiprows = mappings.get("skiprows", 0)
                    comment = mappings.get("comment", None)

                    header_arg = 0 if has_header else None

                    # ==== SPECIAL CASE: runoff.prn (fixed-width) ====
                    if path.lower().endswith(".prn"):
                        # Fixed-width table: year + up to 12 monthly values.
                        # Missing months remain as NaN columns instead of disappearing.
                        df = pd.read_fwf(
                            path,
                            header=None,
                            skiprows=skiprows,
                        )
                        df = _drop_empty_columns(df)
                        df.columns = [
                            f"col_{i+1}" for i in range(len(df.columns))
                        ]
                        self._has_header = False
                    else:
                        # Regular text files
                        sep = _guess_separator(path)
                        df = _read_csv_robust(
                            path,
                            sep=sep,
                            header=header_arg,
                            skiprows=skiprows,
                            comment=comment,
                        )
                        df = _drop_empty_columns(df)

                        # If user indicated no header,
                        # assign column names.
                        if not has_header:
                            df.columns = [
                                f"col_{i+1}" for i in range(len(df.columns))
                            ]

                    _n_before = len(self.datasets)
                    created_ok = self._create_datasets_from_mapping(df, name, path, mappings)
                    if created_ok:
                        bm = batch_metadata_for_file(path)
                        if bm and bm.get("meta"):
                            for _ds in self.datasets[_n_before:]:
                                m = dict(getattr(_ds, "meta", None) or {})
                                for k, v in bm["meta"].items():
                                    m.setdefault(k, v)
                                setattr(_ds, "meta", m)
                        if is_batch:
                            _mark_batch_import("manual")
                        else:
                            QMessageBox.information(
                                self,
                                "Import finished",
                                f"File {name} imported.",
                            )
                        return True

                    if isinstance(batch_ctx, dict):
                        batch_ctx.setdefault("mapping_cache_by_ext", {})
                        if isinstance(batch_ctx.get("mapping_cache_by_ext"), dict):
                            batch_ctx["mapping_cache_by_ext"].pop(ext, None)
                    # Reopen the same preview for the same file and let the user fix the settings.
                    continue

        except Exception as e:
            QMessageBox.critical(self, "File import error", str(e))
            return False

    def choose_from_list_dialog(self, title: str, label: str, items: List[str]) -> Tuple[List[str], bool]:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(400, 300)
        layout = QVBoxLayout()
        layout.addWidget(QLabel(label))
        lst = QListWidget()
        lst.addItems(items)
        lst.setSelectionMode(QAbstractItemView.MultiSelection)
        layout.addWidget(lst)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        dlg.setLayout(layout)
        res = dlg.exec()
        if res != QDialog.Accepted:
            return [], False
        sel = [it.text() for it in lst.selectedItems()]
        return sel, True

    def _create_datasets_from_mapping(self, df: pd.DataFrame, name: str, path: str, mappings: Dict[str, Any]) -> bool:
        """
        Apply columns_config (filter/rename) if set, then create DataSet(s) according to mapping.
        Ensures renames are applied to df_proc so DataSet.df contains new names.
        """
        layout = mappings.get("layout", "wide")
        dec = mappings.get("dec_sep", "Auto")
        df_proc = df.copy()

        # decimal separator fix
        if dec == "Comma (,)":
            for col in df_proc.columns:
                if df_proc[col].dtype == object:
                    try:
                        df_proc[col] = df_proc[col].astype(str).str.replace(",", ".")
                    except Exception:
                        pass

        # apply columns_config (keep + rename)
        columns_cfg = mappings.get("columns_config", None)
        if columns_cfg:
            keep = columns_cfg.get("keep", [])
            rename = columns_cfg.get("rename", {})
            keep_valid = [c for c in keep if c in df_proc.columns]
            if not keep_valid:
                QMessageBox.warning(self, "Columns", "After filtering no valid columns remain. Import cancelled.")
                return False
            df_proc = df_proc[keep_valid].copy()
            if rename:
                rename_valid = {k: v for k, v in rename.items() if k in df_proc.columns and v}
                if rename_valid:
                    df_proc = df_proc.rename(columns=rename_valid)

        # Now create DataSet according to layout
        if layout == "wide":
            months = mappings.get("month_cols", [])
            yr = mappings.get("year_col")
            if yr not in df_proc.columns:
                QMessageBox.warning(self, "Year column", f"Year column '{yr}' not found after column filtering. Import skipped.")
                return False
            if len(months) != 12:
                QMessageBox.warning(self, "Month columns", f"Expected 12 month columns for wide layout. Got {len(months)}. Import skipped.")
                return False
            month_cols = []
            for m in months:
                if m in df_proc.columns:
                    month_cols.append(m)
                else:
                    QMessageBox.warning(self, "Month columns", f"Month column '{m}' not found after filtering/rename. Import skipped.")
                    return False

            meta_candidates = [c for c in df_proc.columns if c not in ([yr] + month_cols)]
            station_meta = _detect_station_metadata_columns(meta_candidates)
            group_cols = [c for c in [station_meta.get("code_wmo"), station_meta.get("name"), station_meta.get("country")] if c]

            if group_cols:
                imported = 0
                for key_vals, g in df_proc.groupby(group_cols, dropna=False):
                    if not isinstance(key_vals, tuple):
                        key_vals = (key_vals,)
                    key_map = {col: val for col, val in zip(group_cols, key_vals)}
                    station_name = key_map.get(station_meta.get("name"), "")
                    station_code = key_map.get(station_meta.get("code_wmo"), "")
                    station_country = key_map.get(station_meta.get("country"), "")

                    g2 = g[[yr] + month_cols].copy()
                    g2 = g2.rename(
                        columns={yr: "Year", **{src: dst for src, dst in zip(month_cols, MONTH_NAMES)}}
                    )
                    g2 = _prepare_monthly_climate_df_for_analysis(
                        g2,
                        year_col="Year",
                        month_cols=MONTH_NAMES,
                        missing_policy="ignore",
                    )
                    if g2.empty:
                        continue

                    name_base = str(station_name).strip() if str(station_name).strip() else str(name)
                    suffix_parts = [str(v).strip() for v in (station_code, station_country) if str(v).strip()]
                    ds_name = name_base if not suffix_parts else f"{name_base} ({'_'.join(suffix_parts)})"

                    ds = DataSet(name=ds_name, path=path, df=g2, kind="climate")
                    ds.year_col = "Year"
                    ds.month_columns = MONTH_NAMES[:]
                    ds.climate_param = mappings.get("climate_param", "Other")
                    self.datasets.append(ds)
                    item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
                    item.setData(Qt.UserRole, ds)
                    self.list_datasets.addItem(item)
                    imported += 1

                if imported > 0:
                    return True

            ds = DataSet(name=name, path=path, df=df_proc[[yr] + month_cols], kind="climate")
            ds.year_col = yr
            ds.month_columns = month_cols
            ds.climate_param = mappings.get("climate_param", "Other")
            self.datasets.append(ds)
            item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
            item.setData(Qt.UserRole, ds)
            self.list_datasets.addItem(item)
        elif layout == "stacked":
            yr = mappings.get("year_col")
            month_col = mappings.get("month_col")
            value_col = mappings.get("value_col")
            if yr not in df_proc.columns or month_col not in df_proc.columns or value_col not in df_proc.columns:
                QMessageBox.warning(self, "Stacked import", "Required columns for stacked layout not found after filtering/rename.")
                return False
            try:
                df_temp = df_proc[[yr, month_col, value_col]].copy()
                df_temp[yr] = pd.to_numeric(df_temp[yr], errors="coerce")
                df_temp[value_col] = pd.to_numeric(df_temp[value_col], errors="coerce")
                df_temp = df_temp.dropna(subset=[yr, value_col]).copy()
                df_temp[yr] = df_temp[yr].astype(int)
                df_temp[month_col] = df_temp[month_col].astype(str)
                month_map = {
                    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
                    'янв':1,'фев':2,'мар':3,'апр':4,'май':5,'июн':6,'июл':7,'авг':8,'сен':9,'окт':10,'ноя':11,'дек':12
                }
                def month_to_idx(s):
                    s2 = str(s).strip().lower()[:3]
                    if s2.isdigit():
                        try:
                            return int(s2)
                        except Exception:
                            return None
                    return month_map.get(s2, None)
                df_temp["__midx__"] = df_temp[month_col].apply(month_to_idx)
                if df_temp["__midx__"].isnull().any():
                    bad_vals = sorted(
                        {
                            str(v)
                            for v in df_temp.loc[df_temp["__midx__"].isnull(), month_col].dropna().tolist()
                        }
                    )
                    QMessageBox.warning(
                        self,
                        "Stacked import",
                        "Some month labels could not be recognized in stacked layout:\n"
                        + ", ".join(bad_vals[:20])
                        + ("\n..." if len(bad_vals) > 20 else "")
                    )
                    return False
                df_temp = (
                    df_temp.groupby([yr, "__midx__"], as_index=False)[value_col]
                    .mean()
                    .copy()
                )
                df_temp["__mname__"] = df_temp["__midx__"].apply(lambda x: f"Y{int(x)}")
                pivot = df_temp.pivot_table(
                    index=yr,
                    columns="__mname__",
                    values=value_col,
                    aggfunc="mean",
                )
                pivot = pivot.reindex(columns=[f"Y{i}" for i in range(1, 13)])
                pivot = pivot.reset_index().rename_axis(columns=None)
                month_cols = [f"Y{i}" for i in range(1,13)]
                pivot = pivot[[yr] + month_cols]
                ds = DataSet(name=name, path=path, df=pivot, kind="climate")
                ds.year_col = yr
                ds.month_columns = month_cols
                ds.climate_param = mappings.get("climate_param", "Other")
                self.datasets.append(ds)
                item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
                item.setData(Qt.UserRole, ds)
                self.list_datasets.addItem(item)
            except Exception as e:
                QMessageBox.critical(self, "Stacked import error", str(e))
                return False
        elif layout == "date_monthly":
            # New: Date (YYYY-MM or YYYY-MM-DD) + Value (monthly)
            date_col = mappings.get("date_col")
            value_col = mappings.get("value_col")

            if not date_col or not value_col:
                QMessageBox.warning(
                    self,
                    "Import error",
                    "Date or value column is not specified. Please select both in the import dialog.",
                )
                return False

            if date_col not in df_proc.columns or value_col not in df_proc.columns:
                QMessageBox.warning(
                    self,
                    "Import error",
                    "Selected Date column '{}' or Value column '{}' not found after filtering/rename.\n"
                    "Available columns: {}".format(
                        date_col,
                        value_col,
                        list(df_proc.columns.astype(str)),
                    ),
                )
                return False

            df_proc = df_proc.copy()

            # 1) Parse dates
            df_proc[date_col] = pd.to_datetime(
                df_proc[date_col], errors="coerce"
            )
            n_dates_ok = df_proc[date_col].notna().sum()
            if n_dates_ok == 0:
                QMessageBox.warning(
                    self,
                    "Import error",
                    "Failed to parse any valid dates in the selected Date column.\n"
                    "Check that the correct column is selected and header/comment options match the file.",
                )
                return False

            # keep only rows with a valid date
            df_proc = df_proc[df_proc[date_col].notna()].copy()

            # 2) Parse numeric values (do NOT drop NaN rows – pivot handles them)
            df_proc[value_col] = pd.to_numeric(df_proc[value_col], errors="coerce")
            n_vals_ok = df_proc[value_col].notna().sum()
            if n_vals_ok == 0:
                QMessageBox.warning(
                    self,
                    "Import warning",
                    "No numeric values could be parsed in the selected Value column.\n"
                    "If values use a comma as decimal separator (e.g., 1,23), "
                    "choose 'Comma (,)' in Decimal separator.",
                )
                # continue, but values will be NaN

            # 3) Extract Year / Month
            df_proc["Year"] = df_proc[date_col].dt.year.astype(int)
            df_proc["Month"] = df_proc[date_col].dt.month.astype(int)

            # 4) Pivot to Year × Month (1..12)
            pivot = df_proc.pivot_table(
                index="Year",
                columns="Month",
                values=value_col,
                aggfunc="mean",
            )

            # Ensure all 12 months present as columns
            pivot = pivot.reindex(columns=list(range(1, 13)))
            pivot.columns = MONTH_NAMES

            pivot = pivot.reset_index()

            # 5) Create climate DataSet
            ds = DataSet(name=name, path=path, df=pivot, kind="climate")
            ds.year_col = "Year"
            ds.month_columns = MONTH_NAMES.copy()
            ds.value_columns = []
            ds.climate_param = mappings.get("climate_param", "Runoff")

            self.datasets.append(ds)
            item = QListWidgetItem(f"{ds.name} (climate: {ds.climate_param})")
            item.setData(Qt.UserRole, ds)
            self.list_datasets.addItem(item)

        elif layout == "datetime_series":
            # New: DateTime + Value (no aggregation, keep original resolution)
            dt_col = mappings.get("datetime_col")
            value_col = mappings.get("value_col")

            if not dt_col or not value_col:
                QMessageBox.warning(
                    self,
                    "Import error",
                    "DateTime or value column is not specified. Please select both in the import dialog.",
                )
                return False

            if dt_col not in df_proc.columns or value_col not in df_proc.columns:
                QMessageBox.warning(
                    self,
                    "Import error",
                    "Selected DateTime column '{}' or Value column '{}' not found after filtering/rename.\n"
                    "Available columns: {}".format(dt_col, value_col, list(df_proc.columns.astype(str))),
                )
                return False

            df_ts = df_proc[[dt_col, value_col]].copy()
            df_ts[dt_col] = pd.to_datetime(df_ts[dt_col], errors="coerce")
            df_ts[value_col] = pd.to_numeric(df_ts[value_col], errors="coerce")
            df_ts = df_ts[df_ts[dt_col].notna()].copy()
            if df_ts.empty:
                QMessageBox.warning(self, "Import error", "Time series is empty after DateTime parsing. Import skipped.")
                return False

            ds = DataSet(name=name, path=path, df=df_ts, kind="timeseries")
            ds.year_col = dt_col
            ds.value_columns = [value_col]
            ds.month_columns = []
            ds.climate_param = mappings.get("climate_param", "Other")
            setattr(ds, "source_freq", mappings.get("source_freq", "daily"))
            self.datasets.append(ds)

            try:
                freq = getattr(ds, "source_freq", "daily")
                item = QListWidgetItem(f"{ds.name} (timeseries: {freq})")
            except Exception:
                item = QListWidgetItem(ds.name)
            item.setData(Qt.UserRole, ds)
            self.list_datasets.addItem(item)

        else:  # multi / chronology
            yr = mappings.get("year_col")
            vals = mappings.get("value_cols", [])
            use_single = mappings.get("use_single", False)
            single_col = mappings.get("single_col", None)
            if yr not in df_proc.columns:
                QMessageBox.warning(self, "Year column", f"Year column '{yr}' not found after filtering. Import skipped.")
                return False
            if use_single:
                if not single_col or single_col not in df_proc.columns:
                    QMessageBox.warning(self, "Columns", "No valid single column selected for single-series import.")
                    return False
                try:
                    cols = [yr, single_col]
                    df_sub = df_proc[cols].copy()
                    ds = DataSet(name=name, path=path, df=df_sub, kind="chronology")
                    ds.year_col = yr
                    ds.value_columns = [single_col]
                    self.datasets.append(ds)
                    item = QListWidgetItem(f"{ds.name} (chronology)")
                    item.setData(Qt.UserRole, ds)
                    self.list_datasets.addItem(item)
                except Exception as e:
                    QMessageBox.critical(self, "Single-series import error", str(e))
                    return False
            else:
                vals_valid = [v for v in vals if v in df_proc.columns]
                if not vals_valid:
                    QMessageBox.warning(self, "Columns", "No valid value columns selected for multi-series import after filtering.")
                    return False
                try:
                    cols = [yr] + vals_valid
                    df_sub = df_proc[cols].copy()
                    ds = DataSet(name=name, path=path, df=df_sub, kind="chronology")
                    ds.year_col = yr
                    ds.value_columns = vals_valid
                    self.datasets.append(ds)
                    item = QListWidgetItem(f"{ds.name} (chronology)")
                    item.setData(Qt.UserRole, ds)
                    self.list_datasets.addItem(item)
                except Exception as e:
                    QMessageBox.critical(self, "Multi import error", str(e))
                    return False

        return True

        self.refresh_analysis_lists()

    # -----------------------
    # Remove / refresh
    # -----------------------
    def _remove_by_key(self) -> None:
        n = len(self.list_datasets.selectedItems())
        if n == 0:
            return
        if QMessageBox.question(self, "Remove datasets", f"Remove {n} selected dataset(s) from the session?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
            self.remove_selected_dataset()

    def remove_selected_dataset(self):
        items = self.list_datasets.selectedItems()
        if not items:
            row = self.list_datasets.currentRow()
            if row < 0:
                return
            item = self.list_datasets.item(row)
            items = [item] if item is not None else []
        if not items:
            return

        rows = sorted(
            {self.list_datasets.row(item) for item in items if item is not None},
            reverse=True,
        )
        remove_ds = []
        for item in items:
            if item is None:
                continue
            ds = item.data(Qt.UserRole)
            if ds is not None:
                remove_ds.append(ds)

        for row in rows:
            if row >= 0:
                self.list_datasets.takeItem(row)

        if remove_ds:
            self.datasets = [d for d in self.datasets if d not in remove_ds]
        self.refresh_analysis_lists()

    def update_data_view(self):
        """Backward-compatible alias to refresh dataset-driven UI widgets."""
        try:
            if hasattr(self, "refresh_analysis_lists"):
                # Refresh analysis lists
                self.refresh_analysis_lists()

            # Also refresh the main Data tab list labels (show periods in parentheses).
            try:
                current_ds = None
                cur_item = self.list_datasets.currentItem()
                if cur_item is not None:
                    current_ds = cur_item.data(Qt.UserRole)
            except Exception:
                current_ds = None

            try:
                self.list_datasets.blockSignals(True)
                self.list_datasets.clear()
                for ds in self.datasets:
                    item = QListWidgetItem(self._format_dataset_label(ds, include_kind=True))
                    item.setData(Qt.UserRole, ds)
                    self.list_datasets.addItem(item)
                    if current_ds is not None and ds is current_ds:
                        item.setSelected(True)
                        self.list_datasets.setCurrentItem(item)
            finally:
                try:
                    self.list_datasets.blockSignals(False)
                except Exception:
                    pass
        except Exception:
            pass


    def _get_analysis_target_mode(self) -> str:
        """
        Return analysis target mode.
        - "climate": chronology × climate (default)
        - "chronology": chronology × chronology
        """
        txt = (
            self.combo_analysis_target.currentText()
            if hasattr(self, "combo_analysis_target")
            else "Chronology × Climate"
        )
        return "chronology" if "Chronology × Chronology" in txt else "climate"

    def _get_analysis_resolution_mode(self) -> str:
        """
        Return resolution mode for chronology × climate analysis.
        - "monthly": monthly climate windows
        - "daily": daily climate windows from time-series datasets
        """
        txt = (
            self.combo_analysis_resolution.currentText()
            if hasattr(self, "combo_analysis_resolution")
            else "Monthly"
        )
        return "daily" if "Daily" in txt else "monthly"

    def _on_analysis_resolution_changed(self) -> None:
        """Update Analysis tab controls when monthly/daily mode changes."""
        try:
            self._on_analysis_target_changed()
        except Exception:
            try:
                self.refresh_analysis_lists()
            except Exception:
                pass

    def _on_analysis_target_changed(self) -> None:
        """Update the Analysis tab UI to match the selected correlation target."""
        mode = self._get_analysis_target_mode()
        resolution = self._get_analysis_resolution_mode() if mode == "climate" else "monthly"
        if hasattr(self, "_label_target_list"):
            if mode != "climate":
                self._label_target_list.setText("Chronologies (B)")
            elif resolution == "daily":
                self._label_target_list.setText("Daily/hourly datasets")
            else:
                self._label_target_list.setText("Climate / daily-hourly datasets")

        enable_climate = (mode == "climate")
        enable_monthly_only = enable_climate and (resolution == "monthly")
        for w in (
            getattr(self, "spin_min_len", None),
            getattr(self, "spin_max_len", None),
            getattr(self, "combo_agg", None),
            getattr(self, "combo_analysis_resolution", None),
            getattr(self, "chk_exclude_single", None),
            getattr(self, "combo_timeseries_monthly_agg", None),
            getattr(self, "btn_preview_timeseries_monthly", None),
        ):
            try:
                if w is not None:
                    w.setEnabled(enable_climate)
            except Exception:
                pass

        for w in (
            getattr(self, "combo_season", None),
            getattr(self, "spin_season_start", None),
            getattr(self, "spin_season_end", None),
            getattr(self, "chk_enable_periods", None),
            getattr(self, "spin_period_min_span", None),
            getattr(self, "spin_period_step", None),
            getattr(self, "spin_min_period_points", None),
            getattr(self, "radio_period_anchored", None),
            getattr(self, "radio_period_sliding", None),
            getattr(self, "radio_period_grid", None),
        ):
            try:
                if w is not None:
                    w.setEnabled(enable_monthly_only)
            except Exception:
                pass

        if hasattr(self, "lbl_timeseries_agg"):
            if resolution == "daily":
                self.lbl_timeseries_agg.setText("Hourly → daily base agg:")
            else:
                self.lbl_timeseries_agg.setText("Daily/hourly → monthly:")

        if hasattr(self, "btn_preview_timeseries_monthly"):
            if resolution == "daily":
                self.btn_preview_timeseries_monthly.setText("Preview selected daily table")
            else:
                self.btn_preview_timeseries_monthly.setText("Preview selected daily/hourly → monthly")

        try:
            if hasattr(self, "combo_top_mode") and self.combo_top_mode.count() >= 2:
                self.combo_top_mode.setItemText(1, "Per pair (top N per pair)")
        except Exception:
            pass

        try:
            self.refresh_analysis_lists()
        except Exception:
            pass

    def refresh_analysis_lists(self):
        try:
            self._push_chronology_locations()
        except Exception:
            pass
        try:
            self._autosave_session()
        except Exception:
            pass
        self.list_chron_for_analysis.clear()
        self.list_climate_for_analysis.clear()
        mode = self._get_analysis_target_mode() if hasattr(self, "_get_analysis_target_mode") else "climate"
        resolution = self._get_analysis_resolution_mode() if (mode == "climate" and hasattr(self, "_get_analysis_resolution_mode")) else "monthly"
        years_all = []
        for idx, ds in enumerate(self.datasets):
            # Coerce legacy dict entries to DataSet to avoid crashes in UI actions.
            if isinstance(ds, dict):
                try:
                    df = ds.get("df")
                    if df is None and "data" in ds:
                        d = ds.get("data")
                        if isinstance(d, dict) and "columns" in d and "data" in d:
                            df = pd.DataFrame(data=d.get("data", []), columns=d.get("columns", []))
                    if df is None:
                        continue

                    kind = ds.get("kind")
                    if not kind:
                        t = str(ds.get("type", "")).lower()
                        kind = "climate" if "climate" in t else "chronology"
                    name = ds.get("name", "dataset")
                    path = ds.get("path", "")
                    ds_obj = DataSet(name=name, path=path, df=df, kind=kind)
                    ds_obj.year_col = ds.get("year_col", ds_obj.year_col)
                    ds_obj.value_columns = ds.get("value_columns", getattr(ds_obj, "value_columns", []))
                    ds_obj.month_columns = ds.get("month_columns", getattr(ds_obj, "month_columns", MONTH_NAMES))
                    ds_obj.climate_param = ds.get("climate_param", getattr(ds_obj, "climate_param", "Other"))
                    self.datasets[idx] = ds_obj
                    ds = ds_obj
                except Exception:
                    continue

            if isinstance(ds, dict):
                # Best-effort coercion for legacy dict entries.
                try:
                    d = ds
                    df = d.get("df")
                    if df is None:
                        continue
                    kind = d.get("kind")
                    if not kind:
                        t = str(d.get("type", "")).lower()
                        kind = "climate" if "climate" in t else "chronology"
                    ds_obj = DataSet(name=d.get("name", "dataset"), path=d.get("path", ""), df=df, kind=kind)
                    ds_obj.year_col = d.get("year_col", ds_obj.year_col)
                    ds_obj.value_columns = d.get("value_columns", getattr(ds_obj, "value_columns", []))
                    ds_obj.month_columns = d.get("month_columns", getattr(ds_obj, "month_columns", MONTH_NAMES))
                    ds_obj.climate_param = d.get("climate_param", getattr(ds_obj, "climate_param", "Other"))
                    ds = ds_obj
                except Exception:
                    continue

            if not isinstance(ds, DataSet):
                continue


            # Allow daily/hourly climate time series in the right analysis list.
            if ds.kind not in ("chronology", "climate", "timeseries"):
                continue

            rng = self._get_dataset_year_range_safe(ds) if hasattr(self, "_get_dataset_year_range_safe") else None
            if rng is not None:
                years_all.append(rng)
            if ds.kind == "chronology":
                item = QListWidgetItem(ds.name)
                item.setData(Qt.UserRole, ds)
                self.list_chron_for_analysis.addItem(item)

                if mode == "chronology":
                    item2 = QListWidgetItem(ds.name)
                    item2.setData(Qt.UserRole, ds)
                    self.list_climate_for_analysis.addItem(item2)

            elif ds.kind == "climate" and mode == "climate" and resolution == "monthly":
                item = QListWidgetItem(ds.name)
                item.setData(Qt.UserRole, ds)
                # show climate type in list
                rng = self._get_dataset_year_range_safe(ds)
                if rng is None:
                    item.setText(f"{ds.name} (climate: {ds.climate_param})")
                else:
                    y0, y1 = rng
                    item.setText(f"{ds.name} (climate: {ds.climate_param}, {y0}–{y1})")
                self.list_climate_for_analysis.addItem(item)
            elif ds.kind == "timeseries" and mode == "climate":
                item = QListWidgetItem(ds.name)
                item.setData(Qt.UserRole, ds)
                meta = getattr(ds, "meta", {})
                freq = str(
                    getattr(ds, "source_freq", "")
                    or (meta.get("frequency", "") if isinstance(meta, dict) else "")
                ).strip()
                if resolution == "daily":
                    parts = [f"daily mode: {freq}" if freq else "daily mode"]
                else:
                    parts = [f"timeseries: {freq}" if freq else "timeseries"]
                clim = str(getattr(ds, "climate_param", "") or "").strip()
                if clim:
                    parts.append(clim)
                rng = self._get_dataset_year_range_safe(ds)
                if rng is not None:
                    y0, y1 = rng
                    parts.append(f"{y0}–{y1}")
                item.setText(f"{ds.name} ({', '.join(parts)})")
                self.list_climate_for_analysis.addItem(item)
        if years_all:
            global_min = min(y[0] for y in years_all)
            global_max = max(y[1] for y in years_all)
            # Remember global full-period range for all datasets
            self._global_min_year = global_min
            self._global_max_year = global_max

            # If common-period mode is OFF, keep full range on the controls.
            # If it is ON, refine years according to current selection.
            if not hasattr(self, "chk_common_period") or not self.chk_common_period.isChecked():
                self.spin_year_start.setValue(global_min)
                self.spin_year_end.setValue(global_max)
            else:
                self._update_year_range_from_selection()

        # If common-period mode is active, refine year range to selected datasets.
        if hasattr(self, "chk_common_period") and self.chk_common_period.isChecked():
            self._update_year_range_from_selection()

        # Ensure Data tab list labels reflect real dataset year ranges
        try:
            self._refresh_loaded_dataset_list_labels()
        except Exception:
            pass

    # -----------------------
    # (rest of the code: run_analysis, plotting, exports, settings)
    # For brevity those methods are unchanged from previous version and kept below,
    # except where they were updated to honor the new aggregation choices and defaults.
    # -----------------------------------------------------------------
    def _run_in_background(self, fn, label: str, ctl=None):
        """Run fn() in a worker thread while the window stays responsive; None if cancelled.

        With ``ctl`` (AnalysisControl) the dialog shows done/total and Cancel stops the worker processes.
        """
        import threading
        import time as _time

        box: Dict[str, Any] = {}

        def work():
            try:
                box["result"] = fn()
            except BaseException as exc:  # reported in the GUI thread
                box["error"] = exc

        th = threading.Thread(target=work, daemon=True)
        dlg = QProgressDialog(label, "Cancel", 0, 0, self)
        dlg.setWindowTitle("Analysis")
        # not modal: the other tabs and windows stay usable while the analysis runs
        dlg.setWindowModality(Qt.NonModal)
        dlg.setMinimumDuration(300)
        self._analysis_running = True
        self._analysis_ctl = ctl
        btn = getattr(self, "btn_run_analysis_top", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("Analysis running...")
        t0 = _time.time()
        th.start()
        while th.is_alive():
            QApplication.processEvents()
            if dlg.wasCanceled() or getattr(self, "_closing", False):
                if ctl is not None:
                    ctl.cancel.set()
                dlg.close()
                self._analysis_done_ui()
                return None
            el = int(_time.time() - t0)
            if ctl is not None and ctl.total:
                if dlg.maximum() != ctl.total:
                    dlg.setRange(0, ctl.total)
                dlg.setValue(min(ctl.done, ctl.total))
                eta = ""
                if ctl.done:
                    rest = el / ctl.done * (ctl.total - ctl.done)
                    eta = f", about {int(rest // 60)} min {int(rest % 60)} s left"
                dlg.setLabelText(f"{label}\n{ctl.done} of {ctl.total} pair(s) done with {ctl.workers} parallel "
                                 f"process(es), {el} s{eta}")
            else:
                dlg.setLabelText(f"{label}\n{el} s")
            _time.sleep(0.05)
        dlg.close()
        self._analysis_done_ui()
        self._last_run_seconds = _time.time() - t0
        if "error" in box:
            if type(box["error"]).__name__ == "_AnalysisCancelled":
                return None
            raise box["error"]
        return box.get("result")

    def _analysis_done_ui(self) -> None:
        self._analysis_running = False
        self._analysis_ctl = None
        btn = getattr(self, "btn_run_analysis_top", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText("Run analysis")

    def _clear_analysis_selection(self) -> None:
        """Clear the selection in the chronology list and in the climate/target list."""
        for lst in (getattr(self, "list_chron_for_analysis", None), getattr(self, "list_climate_for_analysis", None)):
            if lst is not None:
                lst.clearSelection()

    def run_analysis(self):
        if getattr(self, "_analysis_running", False):
            QMessageBox.information(self, "Analysis", "An analysis is already running - wait until it has finished "
                                    "or cancel it in its progress window.")
            return
        self.filtered_result_df = None
        self.period_result_df = None
        self.filtered_period_df = None

        mode = self._get_analysis_target_mode() if hasattr(self, "_get_analysis_target_mode") else "climate"
        resolution = (
            self._get_analysis_resolution_mode()
            if mode == "climate" and hasattr(self, "_get_analysis_resolution_mode")
            else "monthly"
        )

        chron_sets = []
        for item in self.list_chron_for_analysis.selectedItems():
            ds = item.data(Qt.UserRole)
            chron_sets.append(ds)
        right_sets = []
        for item in self.list_climate_for_analysis.selectedItems():
            ds = item.data(Qt.UserRole)
            right_sets.append(ds)

        if mode == "climate":
            climate_sets = []
            timeseries_sets = []
            ts_monthly_agg = (
                self.combo_timeseries_monthly_agg.currentText()
                if hasattr(self, "combo_timeseries_monthly_agg")
                else "Auto (by variable)"
            )
            for ds in right_sets:
                if not isinstance(ds, DataSet):
                    continue
                if resolution == "daily":
                    if ds.kind == "timeseries":
                        timeseries_sets.append(ds)
                elif ds.kind == "climate":
                    climate_sets.append(ds)
                elif ds.kind == "timeseries":
                    try:
                        climate_sets.append(
                            timeseries_to_monthly_climate_dataset(ds, ts_monthly_agg)
                        )
                    except Exception as exc:
                        QMessageBox.warning(
                            self,
                            "Daily/hourly dataset",
                            f"Dataset '{ds.name}' cannot be converted to monthly climate data:\n{exc}",
                        )
                        return
            if resolution == "daily":
                if not chron_sets or not timeseries_sets:
                    QMessageBox.warning(
                        self,
                        "No data",
                        "Select at least one chronology and one daily/hourly dataset.",
                    )
                    return
            elif not chron_sets or not climate_sets:
                QMessageBox.warning(
                    self,
                    "No data",
                    "Select at least one chronology and one climate dataset.",
                )
                return
        else:
            chron_targets = right_sets
            if not chron_sets or not chron_targets:
                QMessageBox.warning(
                    self,
                    "No data",
                    "Select at least one chronology (A) and one chronology (B).",
                )
                return

        year_start = self.spin_year_start.value()
        year_end = self.spin_year_end.value()
        if year_start > year_end:
            QMessageBox.warning(self, "Year range error", "Start year is greater than end year.")
            return

        if mode == "climate":
            min_len = self.spin_min_len.value()
            max_len = self.spin_max_len.value()
            if min_len > max_len:
                QMessageBox.warning(self, "Window error", "Min window length is greater than max.")
                return
        else:
            min_len = 1
            max_len = 1

        corr_method = self.combo_corr.currentText()
        # Missing values in climate datasets (monthly tables only)
        missing_policy = "ignore"
        missing_local_k = 5
        missing_interp_gap = 3

        if getattr(self, "spin_missing_local_k", None) is not None:
            try:
                missing_local_k = int(self.spin_missing_local_k.value())
            except Exception:
                missing_local_k = 5
        if getattr(self, "spin_missing_interp_gap", None) is not None:
            try:
                missing_interp_gap = int(self.spin_missing_interp_gap.value())
            except Exception:
                missing_interp_gap = 3

        if getattr(self, "radio_missing_strict", None) is not None and self.radio_missing_strict.isChecked():
            missing_policy = "strict"
        elif getattr(self, "radio_missing_fill_mean", None) is not None and self.radio_missing_fill_mean.isChecked():
            missing_policy = "fill_monthly_mean"
        elif getattr(self, "radio_missing_local", None) is not None and self.radio_missing_local.isChecked():
            missing_policy = "fill_local_window_mean"
        elif getattr(self, "radio_missing_interp_limited", None) is not None and self.radio_missing_interp_limited.isChecked():
            missing_policy = "interpolate_limited"

        agg_text = self.combo_agg.currentText()

        def resolve_aggs(cset: DataSet, col_name: str, clim_ds: DataSet):
            override = None  # placeholder for future per-pair overrides
            scale = 1.0
            choice = override or agg_text
            if choice == "both":
                return ["sum", "mean"], 1.0
            if choice == "Auto (by variable)":
                base = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
                return [base], 1.0
            if choice == "sum":
                return ["sum"], 1.0
            if choice == "mean":
                return ["mean"], 1.0
            if choice == "sum/2":
                base = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
                return [base], 0.5
            return [choice], scale

        if mode == "climate":
            if resolution == "daily":
                season_months = None
                exclude_single = self.chk_exclude_single.isChecked()
            else:
                season_choice = self.combo_season.currentText()
                if season_choice == "Apr-Sep":
                    season_months = (4, 9)
                elif season_choice == "Oct-Mar":
                    season_months = (10, 3)
                elif season_choice == "Custom":
                    season_months = (self.spin_season_start.value(), self.spin_season_end.value())
                else:
                    season_months = None

                exclude_single = self.chk_exclude_single.isChecked()
        else:
            season_months = None
            exclude_single = False
        # --- NEW: period options ---
        period_rows: List[Dict[str, Any]] = []
        period_enable = self.chk_enable_periods.isChecked()
        if mode == "climate" and resolution == "daily":
            period_enable = False
        min_span = int(self.spin_period_min_span.value())
        step_years = int(self.spin_period_step.value())
        min_period_points = int(self.spin_min_period_points.value())
        min_n = int(self.spin_min_n.value()) if hasattr(self, "spin_min_n") else 3
        if self.radio_period_anchored.isChecked():
            period_mode = "anchored"
        elif self.radio_period_sliding.isChecked():
            period_mode = "sliding"
        else:
            period_mode = "grid"
        # --- END NEW ---

        common = dict(year_start=year_start, year_end=year_end, corr_method=corr_method, alpha=0.05, min_n=min_n)
        if mode == "climate" and resolution == "daily":
            kind, right_list = "daily", timeseries_sets
            kw = dict(common, min_len=min_len, max_len=max_len, aggs=["sum"], exclude_single=exclude_single,
                      base_agg_choice=ts_monthly_agg, agg_resolver=_AggResolver(agg_text))
        elif mode == "climate":
            kind, right_list = "monthly", climate_sets
            kw = dict(common, min_len=min_len, max_len=max_len, missing_policy=missing_policy,
                      local_window_k=missing_local_k, interpolate_max_gap=missing_interp_gap, aggs=["sum"],
                      season_months=season_months, exclude_single=exclude_single, period_enable=period_enable,
                      min_period_span=min_span, step_years=step_years, min_period_points=min_period_points,
                      period_mode=period_mode, agg_resolver=_AggResolver(agg_text))
        else:
            kind, right_list = "chronology", chron_targets
            kw = dict(common, period_enable=period_enable, min_period_span=min_span, step_years=step_years,
                      min_period_points=min_period_points, period_mode=period_mode)
        ctl = AnalysisControl()
        n_workers = int(self.spin_workers.value()) if hasattr(self, "spin_workers") else 0

        # pairs computed before with the same settings and unchanged data are taken from the cache
        reuse = not hasattr(self, "chk_reuse_pairs") or self.chk_reuse_pairs.isChecked()
        cache = getattr(self, "_pair_cache", None)
        if cache is None:
            cache = self._pair_cache = {}
        sig = settings_fingerprint(kind, kw)
        fps = {id(d): dataset_fingerprint(d) for d in list(chron_sets) + list(right_list)}
        all_pairs = [(c, r) for c in chron_sets for r in right_list]
        cached_parts, todo = [], []
        for c, r in all_pairs:
            key = (sig, fps[id(c)], fps[id(r)])
            if reuse and key in cache:
                cached_parts.append(cache[key])
            else:
                todo.append((c, r))
        reused = len(cached_parts)

        def _compute(ctl=ctl):
            per_pair: List[Any] = []
            run_correlation_tasks(kind, chron_sets, right_list, kw, n_workers, ctl, pairs=todo, per_pair_out=per_pair)
            new_parts = []
            for c, r, d, p in per_pair:
                cache[(sig, fps[id(c)], fps[id(r)])] = (d, p)
                new_parts.append((d, p))
            df_res, periods = merge_pair_results(cached_parts + new_parts)
            period_rows.extend(periods)
            return df_res

        if mode == "climate":
            n_right = len(timeseries_sets) if resolution == "daily" else len(climate_sets)
        else:
            n_right = len(chron_targets)
        n_pairs = len(todo)
        if n_pairs == 0 and reused:
            self.statusBar().showMessage(f"All {reused} pair(s) were computed before with the same settings - "
                                         "results taken from the previous analysis.", 15000)
        if n_pairs > 300 and QMessageBox.question(
            self,
            "Large analysis",
            f"{len(chron_sets)} chronologies x {n_right} datasets = {n_pairs} pairs will be screened. "
            "This can take a long time. Continue?\n\n(Tip: select only the chronologies you need, "
            "e.g. with 'Top 3 recommended' or 'STD only'.)",
        ) != QMessageBox.Yes:
            return
        try:
            label = f"Screening {n_pairs} new pair(s)" + (f" ({reused} unchanged pair(s) re-used)" if reused else "") + " ..."
            df_res = self._run_in_background(_compute, label, ctl=ctl)
        except Exception as e:
            QMessageBox.critical(self, "Analysis error", str(e))
            return
        if df_res is None:  # cancelled
            return

        self.result_df = df_res
        self._results_unsaved = not df_res.empty
        if ctl.note:
            self.statusBar().showMessage("Analysis: " + ctl.note, 20000)
        for attr in ("_field_panel", "_moving_panel"):
            fp = getattr(self, attr, None)
            if fp is not None:
                try:
                    fp.refresh_from_host()
                except Exception:
                    pass
        # build period dataframe
        if period_rows:
            self.period_result_df = pd.DataFrame(period_rows)
        else:
            self.period_result_df = pd.DataFrame()
        self._sync_result_filters_from_analysis()
        self.filtered_result_df = self._filter_results(self.result_df)
        self.filtered_period_df = self._filter_results(self.period_result_df)

        self._create_report_from_results()
        self.apply_result_filters()
        self.tabs.setCurrentWidget(self.tab_results)
        self._autosave_session(now=True)
        n_periods = 0 if self.period_result_df is None else len(self.period_result_df)
        if df_res.empty:
            QMessageBox.information(
                self,
                "Done",
                f"No correlations found (check years, formats).\nMulti-year periods: {n_periods} rows.",
            )
        else:
            QMessageBox.information(
                self,
                "Done",
                f"Obtained {len(df_res)} correlation rows.\nMulti-year periods: {n_periods} rows.\n\n"
                f"Computed {n_pairs} new pair(s) in {getattr(self, '_last_run_seconds', 0):.0f} s with {ctl.workers} "
                f"parallel process(es)" + (f"; {reused} pair(s) re-used from the previous analysis "
                                          "(same settings, unchanged data)." if reused else ".")
                + "\nThe results are kept for recovery until you close the program; save the project to keep them.",
            )

    def _get_results_view_df(self) -> Optional[pd.DataFrame]:
        view_text = (
            self.combo_results_view.currentText()
            if hasattr(self, "combo_results_view")
            else "Main correlations"
        )
        if view_text == "Period correlations":
            df_base = self.period_result_df
            df_filtered = getattr(self, "filtered_period_df", None)
        else:
            df_base = self.result_df
            df_filtered = getattr(self, "filtered_result_df", None)
        if df_filtered is not None and df_base is not None and not df_base.empty:
            return df_filtered
        return df_base

    # ---- result filters (applied to the finished results, no new analysis) ----
    def _build_results_filter_bar(self) -> QWidget:
        flow = FlowLayout(spacing=6)

        def lab(t: str) -> QLabel:
            return QLabel(t)

        self.rf_top = QSpinBox()
        self.rf_top.setRange(0, 1000000)
        self.rf_top.setSpecialValueText("all")
        self.rf_top.setToolTip("Keep only the N strongest rows (|r|); 'all' = no limit")
        self.rf_top_mode = QComboBox()
        self.rf_top_mode.addItems(["overall", "per pair", "per chronology", "per climate dataset",
                                   "per climate type"])
        self.rf_top_mode.setMaximumWidth(170)
        self.rf_top_mode.setToolTip("Top N of the whole table, or of every chronology x dataset pair, chronology, "
                                    "climate dataset or climate type")
        self.rf_absr = QDoubleSpinBox()
        self.rf_absr.setRange(0.0, 1.0)
        self.rf_absr.setSingleStep(0.05)
        self.rf_absr.setDecimals(2)
        self.rf_absr.setToolTip("Minimum |r|")
        self.rf_sign = QComboBox()
        self.rf_sign.addItems(["r +/-", "r > 0", "r < 0"])
        self.rf_sig = QComboBox()
        self.rf_sig.addItems(["any p", "p < 0.05", "p < 0.01", "p < 0.001"])
        self.rf_minn = QSpinBox()
        self.rf_minn.setRange(0, 1000)
        self.rf_minn.setSpecialValueText("any")
        self.rf_minn.setToolTip("Minimum number of years (n)")
        self.rf_len0 = QSpinBox()
        self.rf_len1 = QSpinBox()
        for sp, v in ((self.rf_len0, 1), (self.rf_len1, 24)):
            sp.setRange(1, 24)
            sp.setValue(v)
            sp.setToolTip("Window length in months (daily windows are not filtered)")
        self.rf_type = QComboBox()
        self.rf_type.addItem("all climate types")
        self.rf_type.setToolTip("Only one climate variable type")
        self.rf_text = QLineEdit()
        self.rf_text.setPlaceholderText("Search (all words): e.g. CRU pJul RES")
        self.rf_text.setClearButtonEnabled(True)
        self.rf_text.setMinimumWidth(220)
        self.rf_best = QCheckBox("best window per pair")
        self.rf_best.setToolTip("Only the strongest window of every chronology x dataset pair")
        b_reset = QPushButton("Reset")
        b_reset.setToolTip("Show all rows")
        b_reset.clicked.connect(self._reset_result_filters)
        self.rf_info = QLabel("")
        self.rf_info.setStyleSheet("color: #52514e;")
        for w in (lab("Filter:"), lab("top"), self.rf_top, self.rf_top_mode, lab("|r| >="), self.rf_absr,
                  self.rf_sign, self.rf_sig, lab("n >="), self.rf_minn, lab("window"), self.rf_len0, lab("-"),
                  self.rf_len1, lab("months"), self.rf_type, self.rf_best, self.rf_text, b_reset, self.rf_info):
            flow.addWidget(w)
        self._rf_timer = QTimer(self)
        self._rf_timer.setSingleShot(True)
        self._rf_timer.setInterval(250)
        self._rf_timer.timeout.connect(self.apply_result_filters)
        for w in (self.rf_top, self.rf_absr, self.rf_minn, self.rf_len0, self.rf_len1):
            w.valueChanged.connect(lambda *_a: self._rf_timer.start())
        for w in (self.rf_top_mode, self.rf_sign, self.rf_sig, self.rf_type):
            w.currentIndexChanged.connect(lambda *_a: self._rf_timer.start())
        self.rf_text.textChanged.connect(lambda *_a: self._rf_timer.start())
        self.rf_best.toggled.connect(lambda *_a: self._rf_timer.start())
        bar = FlowBar(flow)
        bar.setToolTip("Filters work on the finished results immediately - the analysis is not run again.")
        return bar

    def _reset_result_filters(self) -> None:
        widgets = (self.rf_top, self.rf_absr, self.rf_minn, self.rf_len0, self.rf_len1, self.rf_top_mode,
                   self.rf_sign, self.rf_sig, self.rf_type, self.rf_text, self.rf_best)
        for w in widgets:
            w.blockSignals(True)
        self.rf_top.setValue(0)
        self.rf_absr.setValue(0.0)
        self.rf_minn.setValue(0)
        self.rf_len0.setValue(1)
        self.rf_len1.setValue(24)
        self.rf_top_mode.setCurrentIndex(0)
        self.rf_sign.setCurrentIndex(0)
        self.rf_sig.setCurrentIndex(0)
        self.rf_type.setCurrentIndex(0)
        self.rf_text.clear()
        self.rf_best.setChecked(False)
        for w in widgets:
            w.blockSignals(False)
        self.apply_result_filters()

    def _sync_result_filters_from_analysis(self) -> None:
        """After an analysis: top N from the Analysis tab, the climate types of the results."""
        if not hasattr(self, "rf_top"):
            return
        self.rf_top.blockSignals(True)
        self.rf_top_mode.blockSignals(True)
        self.rf_top.setValue(int(self.spin_top_n.value()) if hasattr(self, "spin_top_n") else 0)
        if hasattr(self, "combo_top_mode"):
            self.rf_top_mode.setCurrentIndex(0 if self.combo_top_mode.currentText().startswith("Overall") else 1)
        self.rf_top.blockSignals(False)
        self.rf_top_mode.blockSignals(False)
        self._refresh_result_type_list()

    def _climate_type_of(self) -> Dict[str, str]:
        return {d.name: (getattr(d, "climate_param", "") or "Other") for d in self.datasets}

    def _refresh_result_type_list(self) -> None:
        if not hasattr(self, "rf_type"):
            return
        df = self.result_df
        types = []
        if df is not None and not df.empty and "Climate" in df.columns:
            m = self._climate_type_of()
            types = sorted({m.get(c, "Other") for c in df["Climate"].unique()})
        cur = self.rf_type.currentText()
        self.rf_type.blockSignals(True)
        self.rf_type.clear()
        self.rf_type.addItem("all climate types")
        self.rf_type.addItems(types)
        i = self.rf_type.findText(cur)
        self.rf_type.setCurrentIndex(i if i >= 0 else 0)
        self.rf_type.blockSignals(False)

    def _filter_results(self, df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if df is None or df.empty or not hasattr(self, "rf_top") or "R" not in df.columns:
            return df
        d = df
        r = pd.to_numeric(d["R"], errors="coerce")
        keep = r.notna()
        if self.rf_absr.value() > 0:
            keep &= r.abs() >= self.rf_absr.value()
        if self.rf_sign.currentIndex() == 1:
            keep &= r > 0
        elif self.rf_sign.currentIndex() == 2:
            keep &= r < 0
        a = {1: 0.05, 2: 0.01, 3: 0.001}.get(self.rf_sig.currentIndex())
        if a and "p" in d.columns:
            keep &= pd.to_numeric(d["p"], errors="coerce") < a
        if self.rf_minn.value() > 0 and "N" in d.columns:
            keep &= pd.to_numeric(d["N"], errors="coerce") >= self.rf_minn.value()
        if "Window" in d.columns and (self.rf_len0.value() > 1 or self.rf_len1.value() < 24):
            def wlen(w) -> float:
                parts = str(w).split("_")
                si = _window_label_to_idx(parts[0])
                ei = _window_label_to_idx(parts[-1])
                return float(ei - si + 1) if si is not None and ei is not None else float("nan")

            ln = d["Window"].map(wlen)
            keep &= ln.isna() | ((ln >= self.rf_len0.value()) & (ln <= self.rf_len1.value()))
        if self.rf_type.currentIndex() > 0 and "Climate" in d.columns:
            m = self._climate_type_of()
            keep &= d["Climate"].map(lambda c: m.get(c, "Other")) == self.rf_type.currentText()
        words = [w for w in self.rf_text.text().lower().split() if w]
        if words:
            cols = [c for c in ("Chronology", "Chron_Column", "Climate", "Target_Column", "Window", "Agg")
                    if c in d.columns]
            hay = d[cols].astype(str).agg(" ".join, axis=1).str.lower()
            for w in words:
                keep &= hay.str.contains(w, regex=False)
        d = d[keep].copy()
        d["_absr"] = pd.to_numeric(d["R"], errors="coerce").abs()
        d = d.sort_values("_absr", ascending=False, kind="mergesort")
        pair_cols = [c for c in ("Chronology", "Chron_Column", "Climate", "Target_Column") if c in d.columns]
        if self.rf_best.isChecked() and pair_cols:
            d = d.groupby(pair_cols, sort=False, dropna=False).head(1)
        n = self.rf_top.value()
        if n > 0:
            mode = self.rf_top_mode.currentIndex()
            if mode == 0:
                d = d.head(n)
            else:
                if mode == 1:
                    gcols = pair_cols
                elif mode == 2:
                    gcols = [c for c in ("Chronology", "Chron_Column") if c in d.columns]
                elif mode == 3:
                    gcols = [c for c in ("Climate", "Target_Column") if c in d.columns]
                else:
                    m = self._climate_type_of()
                    d["_type"] = d["Climate"].map(lambda c: m.get(c, "Other")) if "Climate" in d.columns else ""
                    gcols = ["_type"]
                if gcols:
                    d = d.groupby(gcols, sort=False, dropna=False).head(n)
        return d.drop(columns=[c for c in ("_absr", "_type") if c in d.columns]).reset_index(drop=True)

    def apply_result_filters(self) -> None:
        """Filter the finished results (main and period tables) and show them."""
        self.filtered_result_df = self._filter_results(self.result_df)
        self.filtered_period_df = self._filter_results(self.period_result_df)
        self.update_results_table()
        df = self._get_results_view_df()
        view_text = self.combo_results_view.currentText() if hasattr(self, "combo_results_view") else ""
        base = self.period_result_df if view_text == "Period correlations" else self.result_df
        if hasattr(self, "rf_info"):
            nb = 0 if base is None else len(base)
            nd = 0 if df is None else len(df)
            self.rf_info.setText(f"showing {nd:,} of {nb:,} rows" if nb else "")

    def _apply_top_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        top_n = int(self.spin_top_n.value()) if hasattr(self, "spin_top_n") else 0
        # if top-per-pair checkbox enabled, use spin_top_per_pair and per-pair mode
        if hasattr(self, "chk_top_per_pair") and self.chk_top_per_pair.isChecked():
            # apply per-pair top-K
            k = int(self.spin_top_per_pair.value())
            groups = []
            df_copy = df.copy()
            df_copy["absR"] = df_copy["R"].abs()
            group_cols = ["Chronology", "Chron_Column", "Climate"]
            if "Target_Column" in df_copy.columns:
                group_cols.append("Target_Column")
            for _, grp in df_copy.groupby(group_cols):
                g = grp.sort_values(by="absR", ascending=False).head(k)
                groups.append(g)
            if groups:
                res = pd.concat(groups, ignore_index=True).drop(columns=["absR"])
                res["absR2"] = res["R"].abs()
                res = res.sort_values(by="absR2", ascending=False).drop(columns=["absR2"])
                return res
            return pd.DataFrame(columns=df.columns)
        if top_n <= 0:
            return df.copy()
        mode = self.combo_top_mode.currentText() if hasattr(self, "combo_top_mode") else "Overall (top N)"
        df_copy = df.copy()
        df_copy["absR"] = df_copy["R"].abs()
        if mode.startswith("Overall"):
            df_top = df_copy.sort_values(by="absR", ascending=False).head(top_n).drop(columns=["absR"])
            return df_top
        else:
            groups = []
            group_cols = ["Chronology", "Chron_Column", "Climate"]
            if "Target_Column" in df_copy.columns:
                group_cols.append("Target_Column")
            for _, grp in df_copy.groupby(group_cols):
                g = grp.sort_values(by="absR", ascending=False).head(top_n)
                groups.append(g)
            if groups:
                res = pd.concat(groups, ignore_index=True).drop(columns=["absR"])
                res["absR2"] = res["R"].abs()
                res = res.sort_values(by="absR2", ascending=False).drop(columns=["absR2"])
                return res
            return pd.DataFrame(columns=df.columns)

    def update_results_table(self):
        df_to_show = self._get_results_view_df()
        if df_to_show is None or df_to_show.empty:
            self.table_results.setModel(None)
            return
        model = PandasModel(df_to_show)
        self.table_results.setModel(model)
        self.table_results.resizeColumnsToContents()
        try:
            self.table_results.selectionModel().currentRowChanged.connect(
                lambda cur, _prev: self.update_plot_from_selection(cur) if cur.isValid() else None
            )
        except Exception:
            pass

    def update_plot_from_selection(self, index: QModelIndex):
        df_visible = self._get_results_view_df()
        if df_visible is None or df_visible.empty:
            return
        row_idx = index.row()
        if row_idx < 0 or row_idx >= len(df_visible):
            return
        rec = df_visible.iloc[row_idx]
        if df_visible is None:
            return
        # Choose full dataset matching current view for plotting context
        view_text = (
            self.combo_results_view.currentText()
            if hasattr(self, "combo_results_view")
            else "Main correlations"
        )
        df_source_full = self.period_result_df if view_text == "Period correlations" else self.result_df
        if df_source_full is None or df_source_full.empty:
            return
        # Guard against result tables that do not follow the climate-correlation schema.
        required_cols = {"Chronology", "Climate", "Chron_Column"}
        if not required_cols.issubset(set(df_source_full.columns)):
            return
        if not required_cols.issubset(set(df_visible.columns)):
            return
        mask_pair = (
            (df_source_full["Chronology"] == rec["Chronology"]) &
            (df_source_full["Climate"] == rec["Climate"]) &
            (df_source_full["Chron_Column"] == rec["Chron_Column"])
        )
        if "Target_Column" in df_source_full.columns and "Target_Column" in rec.index:
            mask_pair = mask_pair & (df_source_full["Target_Column"] == rec["Target_Column"])

        df_pair_all = df_source_full[mask_pair].copy()
        if df_pair_all.empty:
            return
        mask_vis = (
            (df_visible["Chronology"] == rec["Chronology"]) &
            (df_visible["Climate"] == rec["Climate"]) &
            (df_visible["Chron_Column"] == rec["Chron_Column"])
        )
        if "Target_Column" in df_visible.columns and "Target_Column" in rec.index:
            mask_vis = mask_vis & (df_visible["Target_Column"] == rec["Target_Column"])
        df_pair_visible = df_visible[mask_vis].copy()
        if _dplots is not None:
            chron_ds_v2 = next((d for d in self.datasets if d.name == rec["Chronology"]), None)
            clim_ds_v2 = next((d for d in self.datasets if d.name == rec["Climate"]), None)
            if chron_ds_v2 is not None and clim_ds_v2 is not None:
                try:
                    self._plot_selected_result_v2(rec, view_text, df_pair_all, df_pair_visible, chron_ds_v2, clim_ds_v2)
                    return
                except Exception as exc:
                    print("Plot error (falling back to the classic plot):", exc)
        df_for_bars = df_pair_visible if (df_pair_visible is not None and not df_pair_visible.empty) else df_pair_all
        top_n_display = int(self.spin_top_n.value()) if hasattr(self, "spin_top_n") else 0
        if top_n_display > 0:
            df_for_bars = df_for_bars.sort_values(by="R", key=lambda s: s.abs(), ascending=False).head(top_n_display)
        else:
            df_for_bars = df_for_bars.sort_values(by="R", key=lambda s: s.abs(), ascending=False).head(50)
        try:
            self.canvas.fig.clf()
        except Exception:
            pass
        ax1 = self.canvas.fig.add_subplot(211)
        df_for_bars = df_for_bars.sort_values(by="R", key=lambda s: s.abs(), ascending=True)
        y_pos = np.arange(len(df_for_bars))
        colors = ["red" if s else "blue" for s in df_for_bars["Significant"]]
        ax1.barh(y_pos, df_for_bars["R"], color=colors)
        ax1.set_yticks(y_pos)
        labels = (df_for_bars["Window"] + "_" + df_for_bars["Agg"].astype(str)).tolist()
        ax1.set_yticklabels(labels, fontsize=8)
        ax1.set_xlabel("R")
        ax1.set_title(f"{rec['Chronology']} ({rec['Chron_Column']}) vs {rec['Climate']}")
        try:
            chron_ds = next((d for d in self.datasets if d.name == rec["Chronology"]), None)
            clim_ds = next((d for d in self.datasets if d.name == rec["Climate"]), None)
            if chron_ds is None or clim_ds is None:
                self.canvas.draw()
                return

            if getattr(clim_ds, "kind", "") != "climate":
                ax2 = self.canvas.fig.add_subplot(212)
                col_a = rec["Chron_Column"]
                col_b = rec["Target_Column"] if ("Target_Column" in rec.index and pd.notna(rec["Target_Column"])) else None
                if col_b is None:
                    try:
                        col_b = clim_ds.value_columns[0]
                    except Exception:
                        col_b = None
                if col_b is None:
                    self.canvas.draw()
                    return

                df_a = chron_ds.df[[chron_ds.year_col, col_a]].copy()
                df_b = clim_ds.df[[clim_ds.year_col, col_b]].copy()
                df_a[chron_ds.year_col] = pd.to_numeric(df_a[chron_ds.year_col], errors="coerce")
                df_b[clim_ds.year_col] = pd.to_numeric(df_b[clim_ds.year_col], errors="coerce")
                df_a[col_a] = pd.to_numeric(df_a[col_a], errors="coerce")
                df_b[col_b] = pd.to_numeric(df_b[col_b], errors="coerce")

                merged_ts = df_a.merge(df_b, left_on=chron_ds.year_col, right_on=clim_ds.year_col, how="inner")
                merged_ts = merged_ts.dropna(subset=[chron_ds.year_col])
                merged_ts["Year"] = pd.to_numeric(merged_ts[chron_ds.year_col], errors="coerce")
                merged_ts = merged_ts.dropna(subset=["Year"])
                merged_ts["Year"] = merged_ts["Year"].astype(int)

                if view_text == "Period correlations" and "PeriodStart" in rec.index and "PeriodEnd" in rec.index:
                    try:
                        ps = int(rec["PeriodStart"])
                        pe = int(rec["PeriodEnd"])
                        merged_ts = merged_ts[(merged_ts["Year"] >= ps) & (merged_ts["Year"] <= pe)]
                    except Exception:
                        pass

                merged_ts = merged_ts.dropna(subset=[col_a, col_b])
                if merged_ts.empty:
                    self.canvas.draw()
                    return

                years = merged_ts["Year"].to_numpy(dtype=float)
                ax2.plot(years, merged_ts[col_a].to_numpy(dtype=float), label=f"{rec['Chronology']}::{col_a}")
                ax2.plot(years, merged_ts[col_b].to_numpy(dtype=float), label=f"{rec['Climate']}::{col_b}")
                ax2.set_xlabel("Year")
                ax2.set_ylabel("Value")
                ax2.set_title("Series overlay (overlap period)")
                try:
                    ax2.legend(fontsize=8)
                except Exception:
                    pass

                self.canvas.fig.tight_layout()
                self.canvas.draw()
                return
            min_len = self.spin_min_len.value()
            max_len = self.spin_max_len.value()
            agg_text = rec["Agg"] if ("Agg" in rec and pd.notna(rec["Agg"]) and rec["Agg"] != "") else self.combo_agg.currentText()
            if agg_text == "both":
                aggs = ["sum", "mean"]
            elif agg_text in ("sum", "mean"):
                aggs = [agg_text]
            else:
                # If agg_text is Auto (by variable) or others, resolve per climate dataset:
                if agg_text == "Auto (by variable)":
                    base_agg = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
                    aggs = [base_agg]
                elif agg_text == "sum/2":
                    base_agg = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
                    aggs = [base_agg]
                else:
                    aggs = ["sum"]

            windows_df = generate_climate_windows(
                clim_ds.df,
                year_col=clim_ds.year_col,
                month_cols_y=clim_ds.month_columns,
                min_len=min_len,
                max_len=max_len,
                aggs=aggs
            )

            # if the UI requested sum/2 and it resolved to base agg, scale here for plotting
            if agg_text == "sum/2":
                num_cols = [c for c in windows_df.columns if c != clim_ds.year_col]
                try:
                    windows_df[num_cols] = windows_df[num_cols] * 0.5
                except Exception:
                    for c in num_cols:
                        try:
                            windows_df[c] = pd.to_numeric(windows_df[c], errors="coerce") * 0.5
                        except Exception:
                            pass

            chrono_df = chron_ds.df[[chron_ds.year_col, rec["Chron_Column"]]].copy()
            chrono_df[chron_ds.year_col] = pd.to_numeric(chrono_df[chron_ds.year_col], errors="coerce")
            windows_df[clim_ds.year_col] = pd.to_numeric(windows_df[clim_ds.year_col], errors="coerce")
            merged = chrono_df.merge(windows_df, left_on=chron_ds.year_col, right_on=clim_ds.year_col, how="inner")
            lengths = list(range(min_len, max_len + 1))
            starts = list(range(0, 24))
            grid = np.full((len(lengths), len(starts)), np.nan)
            selected_full_set = set()
            if df_pair_visible is not None and not df_pair_visible.empty:
                for _, r in df_pair_visible.iterrows():
                    full = f"{r['Window']}_{r['Agg']}"
                    selected_full_set.add(full)
            for i_len, L in enumerate(lengths):
                for j_start, S in enumerate(starts):
                    E = S + L - 1
                    if E >= 24:
                        continue
                    # compute start_label and end_label in new naming (pJan..Jan..Dec)
                    all_labels = [f"p{m}" for m in MONTH_NAMES] + MONTH_NAMES
                    start_label = all_labels[S]
                    end_label = all_labels[E]
                    for agg in aggs:
                        # build wname considering single-month naming
                        if start_label == end_label:
                            wname_base = start_label
                        else:
                            wname_base = f"{start_label}_{end_label}"
                        wname = f"{wname_base}_{agg}"
                        if wname not in merged.columns:
                            continue
                        if selected_full_set and f"{wname_base}_{agg}" not in selected_full_set and f"{wname_base}_{agg}" not in selected_full_set:
                            # If selection filtering applied and this window isn't selected, skip plotting it
                            grid[i_len, j_start] = np.nan
                            continue
                        x = _to_numeric_1d(merged[rec["Chron_Column"]])
                        y = _to_numeric_1d(merged[wname])
                        mask_xy = ~np.isnan(x) & ~np.isnan(y)
                        if mask_xy.sum() < 3:
                            r = np.nan
                        else:
                            try:
                                if self.combo_corr.currentText() == "spearman" and stats is not None:
                                    r, p = stats.spearmanr(x[mask_xy], y[mask_xy])
                                elif self.combo_corr.currentText() == "pearson" and stats is not None:
                                    r, p = stats.pearsonr(x[mask_xy], y[mask_xy])
                                else:
                                    r = pd.Series(x[mask_xy]).corr(pd.Series(y[mask_xy]), method=self.combo_corr.currentText())
                            except Exception:
                                r = pd.Series(x[mask_xy]).corr(pd.Series(y[mask_xy]), method=self.combo_corr.currentText())
                        grid[i_len, j_start] = r
            ax2 = self.canvas.fig.add_subplot(212)
            im = ax2.imshow(grid, aspect='auto', cmap='coolwarm', vmin=-1, vmax=1, origin='lower')
            ax2.set_yticks(range(len(lengths)))
            ax2.set_yticklabels([str(l) for l in lengths])
            xticks = list(range(0, 24, 2))
            ax2.set_xticks(xticks)
            xtick_labels = []
            for s in xticks:
                if s < 12:
                    xtick_labels.append(f"p{MONTH_NAMES[s]}")     # pJan..pDec
                else:
                    xtick_labels.append(MONTH_NAMES[s - 12])      # Jan..Dec
            ax2.set_xticklabels(xtick_labels, rotation=90)
            ax2.set_xlabel("Window start (p=previous year, current year without prefix)")
            ax2.set_ylabel("Window length (months)")
            ax2.set_title("Correlation heatmap (R)")
            self.canvas.fig.colorbar(im, ax=ax2, orientation='vertical', fraction=0.05)
            self.canvas.fig.tight_layout()
        except Exception as e:
            print("Heatmap error:", e)
        self.canvas.draw()

    def _replot_current_result(self) -> None:
        try:
            idx = self.table_results.currentIndex()
        except Exception:
            return
        if idx is not None and idx.isValid():
            self.update_plot_from_selection(idx)

    @staticmethod
    def _dataset_series(ds: Any, column: str) -> pd.Series:
        """Year-indexed numeric series of one dataset column."""
        df = ds.df[[ds.year_col, column]].copy()
        years = pd.to_numeric(df[ds.year_col], errors="coerce")
        vals = pd.to_numeric(df[column], errors="coerce")
        s = pd.Series(vals.to_numpy(dtype=float), index=years)
        s = s[~pd.isna(s.index)]
        s.index = s.index.astype(int)
        return s[~s.index.duplicated(keep="first")].sort_index()

    def _plot_selected_result_v2(
        self,
        rec: pd.Series,
        view_text: str,
        df_pair_all: pd.DataFrame,
        df_pair_visible: Optional[pd.DataFrame],
        chron_ds: "DataSet",
        clim_ds: "DataSet",
    ) -> None:
        """Result plots: monthly response, window heatmap, standardised series, running r, scatter."""
        DP = _dplots
        fig = self.canvas.fig
        fig.clf()
        mode = self.combo_plot_mode.currentText() if hasattr(self, "combo_plot_mode") else "Overview"
        window = int(self.spin_running_window.value()) if hasattr(self, "spin_running_window") else 31
        corr_method = self.combo_corr.currentText() if hasattr(self, "combo_corr") else "pearson"
        chron_col = str(rec["Chron_Column"])
        pair_title = f"{rec['Chronology']} ({chron_col}) vs {rec['Climate']}"
        period = None
        if view_text == "Period correlations" and "PeriodStart" in rec.index and "PeriodEnd" in rec.index:
            try:
                period = (int(rec["PeriodStart"]), int(rec["PeriodEnd"]))
            except Exception:
                period = None
        if period:
            pair_title += f"  [{period[0]}-{period[1]}]"

        a_full = self._dataset_series(chron_ds, chron_col)

        top_src = df_pair_visible if (df_pair_visible is not None and not df_pair_visible.empty) else df_pair_all
        n_top = int(self.spin_top_n.value()) if hasattr(self, "spin_top_n") else 0
        n_top = n_top if n_top > 0 else 25
        top = top_src.sort_values(by="R", key=lambda s: s.abs(), ascending=False).head(min(n_top, 40))
        top = top.iloc[::-1]
        has_window = "Window" in top.columns and "Agg" in top.columns
        top_labels = (
            (top["Window"].astype(str) + "_" + top["Agg"].astype(str)).tolist()
            if has_window
            else [str(i) for i in range(len(top))]
        )
        sel_label = f"{rec.get('Window', '')}_{rec.get('Agg', '')}" if has_window else None
        top_n_vals = top["N"].tolist() if "N" in top.columns else None

        def draw_top(ax) -> None:
            DP.corr_bars(ax, top_labels, top["R"].tolist(), n=top_n_vals, highlight=sel_label,
                         title=f"Strongest windows ({corr_method})")

        def finish(single_title: str = "") -> None:
            fig.suptitle(pair_title, fontsize=10, y=0.995)
            self.canvas.draw()

        # ---------- chronology vs non-monthly target (series / time series) ----------
        if getattr(clim_ds, "kind", "") != "climate":
            col_b = rec["Target_Column"] if ("Target_Column" in rec.index and pd.notna(rec["Target_Column"])) else None
            if col_b is None:
                col_b = clim_ds.value_columns[0] if getattr(clim_ds, "value_columns", None) else None
            if col_b is None:
                DP.message(fig.add_subplot(111), "No target column to plot")
                return finish()
            b_full = self._dataset_series(clim_ds, str(col_b))
            both = pd.concat([a_full.rename("a"), b_full.rename("b")], axis=1, join="inner").dropna()
            if period:
                both = both.loc[(both.index >= period[0]) & (both.index <= period[1])]
            if len(both) < 4:
                DP.message(fig.add_subplot(111), "The two series do not overlap")
                return finish()
            la, lb = f"{rec['Chronology']}::{chron_col}", f"{rec['Climate']}::{col_b}"
            if mode in ("Overview", "Monthly correlations", "Window heatmap"):
                gs = fig.add_gridspec(2, 2, hspace=0.6, wspace=0.3, left=0.1, right=0.97, top=0.9, bottom=0.08)
                if has_window:
                    draw_top(fig.add_subplot(gs[0, 0]))
                else:
                    DP.scatter_fit(fig.add_subplot(gs[0, 0]), both["a"].values, both["b"].values, la, lb)
                DP.scatter_fit(fig.add_subplot(gs[0, 1]), both["a"].values, both["b"].values, la, lb)
                DP.zscore_overlay(fig.add_subplot(gs[1, 0]), both.index.values, both["a"].values, both["b"].values,
                                  la, lb, smooth=11)
                DP.running_corr_plot(fig.add_subplot(gs[1, 1]), both["a"], both["b"], window=window)
            elif mode == "Top windows" and has_window:
                draw_top(fig.add_subplot(111))
            elif mode == "Running correlation":
                DP.running_corr_plot(fig.add_subplot(111), both["a"], both["b"], window=window)
            elif mode == "Scatter":
                DP.scatter_fit(fig.add_subplot(111), both["a"].values, both["b"].values, la, lb)
            else:
                DP.zscore_overlay(fig.add_subplot(111), both.index.values, both["a"].values, both["b"].values,
                                  la, lb, smooth=11)
            return finish()

        # ---------- chronology vs monthly climate ----------
        min_len = int(self.spin_min_len.value())
        max_len = int(self.spin_max_len.value())
        agg_text = rec["Agg"] if ("Agg" in rec and pd.notna(rec["Agg"]) and rec["Agg"] != "") else self.combo_agg.currentText()
        if agg_text in ("sum", "mean"):
            agg_sel = agg_text
        else:
            agg_sel = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
        windows_df = generate_climate_windows(
            clim_ds.df,
            year_col=clim_ds.year_col,
            month_cols_y=clim_ds.month_columns,
            min_len=1,
            max_len=max(1, max_len),
            aggs=[agg_sel],
        )
        windows_df[clim_ds.year_col] = pd.to_numeric(windows_df[clim_ds.year_col], errors="coerce")
        windows_df = windows_df.dropna(subset=[clim_ds.year_col])
        windows_df.index = windows_df[clim_ds.year_col].astype(int)
        merged = pd.concat([a_full.rename("__chron__"), windows_df.drop(columns=[clim_ds.year_col])], axis=1, join="inner")
        if period:
            merged = merged.loc[(merged.index >= period[0]) & (merged.index <= period[1])]
        x_all = merged["__chron__"]

        def r_of(col: str) -> tuple:
            if col not in merged.columns:
                return float("nan"), 0
            pair = pd.concat([x_all, merged[col]], axis=1).dropna()
            if len(pair) < 4:
                return float("nan"), len(pair)
            try:
                return float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method=corr_method)), len(pair)
            except Exception:
                return float(pair.iloc[:, 0].corr(pair.iloc[:, 1])), len(pair)

        all_labels = [f"p{m}" for m in MONTH_NAMES] + MONTH_NAMES

        def wname(start: int, end: int) -> str:
            return all_labels[start] if start == end else f"{all_labels[start]}_{all_labels[end]}"

        lengths = list(range(max(1, min_len), max(1, max_len) + 1))
        grid = np.full((len(lengths), 24), np.nan)
        n_cells = []
        for i_len, L in enumerate(lengths):
            for S in range(0, 24 - L + 1):
                rr, nn = r_of(f"{wname(S, S + L - 1)}_{agg_sel}")
                grid[i_len, S] = rr
                if nn:
                    n_cells.append(nn)
        monthly = [r_of(f"{all_labels[k]}_{agg_sel}") for k in range(24)]
        month_r = [m[0] for m in monthly]
        month_n = int(np.median([m[1] for m in monthly if m[1]])) if any(m[1] for m in monthly) else 0
        n_grid = int(np.median(n_cells)) if n_cells else 0

        def cell_of(window_name: str):
            parts = str(window_name).split("_")
            try:
                s0 = all_labels.index(parts[0])
                e0 = all_labels.index(parts[-1]) if len(parts) > 1 else s0
            except ValueError:
                return None
            L0 = e0 - s0 + 1
            return (lengths.index(L0), s0) if L0 in lengths else None

        best_cell = cell_of(rec["Window"]) if "Window" in rec.index else None
        marked = []
        if has_window:
            for _, row in top.iterrows():
                if str(row.get("Agg", "")) == agg_sel:
                    c = cell_of(row["Window"])
                    if c is not None and c != best_cell:
                        marked.append(c)

        sel_col = f"{rec['Window']}_{agg_sel}" if "Window" in rec.index else None
        b_sel = merged[sel_col] if (sel_col and sel_col in merged.columns) else None
        sel_name = f"{rec['Climate']} {rec.get('Window', '')} ({agg_sel})"
        both = pd.concat([x_all.rename("a"), b_sel.rename("b")], axis=1).dropna() if b_sel is not None else None

        def draw_monthly(ax) -> None:
            DP.monthly_bars(ax, all_labels, month_r, n=month_n, divider_after=12,
                            title=f"Single-month correlations ({agg_sel}, {corr_method})")

        def draw_heat(ax, cax=None) -> None:
            DP.window_heatmap(ax, grid, lengths, all_labels, n=n_grid, best=best_cell, marked=marked, cax=cax,
                              title=f"Climate windows ({agg_sel}); star = selected, dots = top windows")

        def need_both(ax) -> bool:
            if both is None or len(both) < 4:
                DP.message(ax, "Selected window has no overlapping years")
                return False
            return True

        if mode == "Overview":
            gs = fig.add_gridspec(2, 2, hspace=0.62, wspace=0.28, left=0.07, right=0.95, top=0.9, bottom=0.12)
            draw_monthly(fig.add_subplot(gs[0, 0]))
            draw_heat(fig.add_subplot(gs[0, 1]))
            ax3 = fig.add_subplot(gs[1, 0])
            if need_both(ax3):
                DP.zscore_overlay(ax3, both.index.values, both["a"].values, both["b"].values,
                                  chron_col, sel_name, smooth=11)
            ax4 = fig.add_subplot(gs[1, 1])
            if need_both(ax4):
                DP.running_corr_plot(ax4, both["a"], both["b"], window=window)
        elif mode == "Monthly correlations":
            draw_monthly(fig.add_subplot(111))
        elif mode == "Window heatmap":
            draw_heat(fig.add_subplot(111))
        elif mode == "Top windows":
            draw_top(fig.add_subplot(111))
        elif mode == "Running correlation":
            ax = fig.add_subplot(111)
            if need_both(ax):
                DP.running_corr_plot(ax, both["a"], both["b"], window=window)
        elif mode == "Scatter":
            ax = fig.add_subplot(111)
            if need_both(ax):
                DP.scatter_fit(ax, both["a"].values, both["b"].values, chron_col, sel_name)
        else:
            ax = fig.add_subplot(111)
            if need_both(ax):
                DP.zscore_overlay(ax, both.index.values, both["a"].values, both["b"].values, chron_col, sel_name,
                                  smooth=11)
        finish()

    # -----------------------
    # Export computations & report
    # -----------------------
    def export_windows_csv(self):
        clim_datasets = [d for d in self.datasets if d.kind == "climate"]
        if not clim_datasets:
            QMessageBox.information(
                self,
                "No climate data",
                "No climate datasets available to export windows.",
            )
            return

        options = ["All"] + [d.name for d in clim_datasets]

        dlg = QDialog(self)
        dlg.setWindowTitle("Choose climate dataset to export windows")
        dlg_layout = QVBoxLayout()

        label = QLabel("Choose which climate dataset to export windows for:")
        dlg_layout.addWidget(label)

        combo = QComboBox()
        combo.addItems(options)
        dlg_layout.addWidget(combo)

        agg_label = QLabel("Aggregation:")
        agg_combo = QComboBox()
        agg_combo.addItems(["Auto (by variable)", "sum", "mean", "sum/2"])
        dlg_layout.addWidget(agg_label)
        dlg_layout.addWidget(agg_combo)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        dlg_layout.addWidget(btn_box)
        dlg.setLayout(dlg_layout)

        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)

        if dlg.exec() != QDialog.Accepted:
            return

        choice = combo.currentText()
        agg_choice = agg_combo.currentText()
        min_len = self.spin_min_len.value()
        max_len = self.spin_max_len.value()

        if choice == "All":
            folder = QFileDialog.getExistingDirectory(
                self,
                "Choose folder to save windows for all climate datasets",
            )
            if not folder:
                return

            # Choose export format (Excel vs CSV), default is Excel
            fmt_box = QMessageBox(self)
            fmt_box.setWindowTitle("Export format")
            fmt_box.setText("Choose export format for climate windows.")
            btn_excel = fmt_box.addButton("Excel (.xlsx)", QMessageBox.AcceptRole)
            btn_csv = fmt_box.addButton("CSV (.csv)", QMessageBox.AcceptRole)
            fmt_box.setDefaultButton(btn_excel)
            fmt_box.exec()
            clicked = fmt_box.clickedButton()
            is_excel = clicked is btn_excel

            try:
                for d in clim_datasets:
                    scale_factor = 1.0
                    if agg_choice == "Auto (by variable)":
                        base_agg = get_default_agg_for_param(
                            getattr(d, "climate_param", "Other")
                        )
                        aggs = [base_agg]
                    elif agg_choice == "sum":
                        aggs = ["sum"]
                    elif agg_choice == "mean":
                        aggs = ["mean"]
                    elif agg_choice == "sum/2":
                        base_agg = get_default_agg_for_param(
                            getattr(d, "climate_param", "Other")
                        )
                        aggs = [base_agg]
                        scale_factor = 0.5
                    else:
                        aggs = ["sum"]

                    windows_df = generate_climate_windows(
                        d.df,
                        year_col=d.year_col,
                        month_cols_y=d.month_columns,
                        min_len=min_len,
                        max_len=max_len,
                        aggs=aggs,
                    )
                    if scale_factor != 1.0:
                        num_cols = [c for c in windows_df.columns if c != d.year_col]
                        windows_df[num_cols] = windows_df[num_cols] * scale_factor

                    # Sort window columns by months for export
                    windows_df = _sort_window_columns_for_export(
                        windows_df,
                        d.year_col,
                    )

                    ext = ".xlsx" if is_excel else ".csv"
                    outp = os.path.join(folder, f"{d.name}_windows{ext}")
                    if is_excel:
                        windows_df.to_excel(outp, index=False)
                    else:
                        windows_df.to_csv(outp, index=False)
                QMessageBox.information(
                    self,
                    "Export complete",
                    f"Windows exported to folder {folder}",
                )
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "Export windows",
                    f"Failed to export windows: {e}",
                )
        else:
            d = next((x for x in clim_datasets if x.name == choice), None)
            if d is None:
                QMessageBox.warning(
                    self,
                    "Select dataset",
                    "Selected dataset not found.",
                )
                return

            path, selected_filter = QFileDialog.getSaveFileName(
                self,
                f"Save windows for {d.name}",
                f"{d.name}_windows.xlsx",
                "Excel files (*.xlsx);;CSV files (*.csv)",
            )
            if not path:
                return

            is_excel = path.lower().endswith(".xlsx") or "Excel" in selected_filter

            try:
                scale_factor = 1.0
                if agg_choice == "Auto (by variable)":
                    base_agg = get_default_agg_for_param(
                        getattr(d, "climate_param", "Other")
                    )
                    aggs = [base_agg]
                elif agg_choice == "sum":
                    aggs = ["sum"]
                elif agg_choice == "mean":
                    aggs = ["mean"]
                elif agg_choice == "sum/2":
                    base_agg = get_default_agg_for_param(
                        getattr(d, "climate_param", "Other")
                    )
                    aggs = [base_agg]
                    scale_factor = 0.5
                else:
                    aggs = ["sum"]

                windows_df = generate_climate_windows(
                    d.df,
                    year_col=d.year_col,
                    month_cols_y=d.month_columns,
                    min_len=min_len,
                    max_len=max_len,
                    aggs=aggs,
                )
                if scale_factor != 1.0:
                    num_cols = [c for c in windows_df.columns if c != d.year_col]
                    windows_df[num_cols] = windows_df[num_cols] * scale_factor

                # Sort window columns by months for export
                windows_df = _sort_window_columns_for_export(
                    windows_df,
                    d.year_col,
                )

                if is_excel:
                    if not path.lower().endswith(".xlsx"):
                        path = path + ".xlsx"
                    windows_df.to_excel(path, index=False)
                else:
                    if not path.lower().endswith(".csv"):
                        path = path + ".csv"
                    windows_df.to_csv(path, index=False)

                self.notify_saved("Saved", "Windows saved.", path)
            except Exception as e:
                QMessageBox.critical(self, "Export error", str(e))

    def export_merged_csv(self):
        sel = self.table_results.currentIndex()
        if not sel.isValid():
            QMessageBox.information(self, "No selection", "Select a result row first.")
            return
        df_visible = self.filtered_result_df if (self.filtered_result_df is not None and not self.filtered_result_df.empty) else self.result_df
        rec = df_visible.iloc[sel.row()]
        chron_ds = next((d for d in self.datasets if d.name == rec["Chronology"]), None)
        clim_ds = next((d for d in self.datasets if d.name == rec["Climate"]), None)
        if chron_ds is None or clim_ds is None:
            QMessageBox.warning(self, "Datasets missing", "Chronology or Climate dataset not found.")
            return
        agg = rec["Agg"] if pd.notna(rec["Agg"]) and rec["Agg"] else self.combo_agg.currentText()
        aggs = [agg] if agg != "both" else (["sum", "mean"])
        try:
            # if agg is Auto resolve by climate param
            if agg == "Auto (by variable)":
                base_agg = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
                aggs = [base_agg]
            if agg == "sum/2":
                base_agg = get_default_agg_for_param(getattr(clim_ds, "climate_param", "Other"))
                aggs = [base_agg]
                scale = 0.5
            else:
                scale = 1.0
            windows_df = generate_climate_windows(clim_ds.df, year_col=clim_ds.year_col, month_cols_y=clim_ds.month_columns, min_len=self.spin_min_len.value(), max_len=self.spin_max_len.value(), aggs=aggs)
            if scale != 1.0:
                num_cols = [c for c in windows_df.columns if c != clim_ds.year_col]
                windows_df[num_cols] = windows_df[num_cols] * scale
            chrono_df = chron_ds.df[[chron_ds.year_col, rec["Chron_Column"]]].copy()
            chrono_df[chron_ds.year_col] = pd.to_numeric(chrono_df[chron_ds.year_col], errors="coerce")
            windows_df[clim_ds.year_col] = pd.to_numeric(windows_df[clim_ds.year_col], errors="coerce")
            merged = chrono_df.merge(windows_df, left_on=chron_ds.year_col, right_on=clim_ds.year_col, how="inner")
            ys = self.spin_year_start.value()
            ye = self.spin_year_end.value()
            merged = merged[(merged[chron_ds.year_col] >= ys) & (merged[chron_ds.year_col] <= ye)]
            path, selected_filter = QFileDialog.getSaveFileName(
                self,
                f"Save merged data for {rec['Chronology']}×{rec['Climate']}",
                f"{rec['Chronology']}_{rec['Climate']}_merged.xlsx",
                "Excel files (*.xlsx);;CSV files (*.csv)",
            )
            if not path:
                return
            is_excel = path.lower().endswith(".xlsx") or "Excel" in selected_filter
            if is_excel:
                if not path.lower().endswith(".xlsx"):
                    path = path + ".xlsx"
                merged.to_excel(path, index=False)
            else:
                if not path.lower().endswith(".csv"):
                    path = path + ".csv"
                merged.to_csv(path, index=False)
            self.notify_saved("Saved", "Merged data saved.", path)
        except Exception as e:
            QMessageBox.critical(
                self,
                "Export error",
                f"Failed to export merged data:\n{e}",
            )

    def _create_report_from_results(self):
        if self.result_df is None or self.result_df.empty:
            self.report_df = pd.DataFrame()
            self.table_report.setModel(PandasModel(self.report_df))
            return
        report_k = int(self.spin_report_k.value()) if hasattr(self, "spin_report_k") else 10
        if report_k <= 0:
            report_k = 10
        groups = []
        group_cols = ["Chronology", "Chron_Column", "Climate"]
        if "Target_Column" in self.result_df.columns:
            group_cols.append("Target_Column")
        for keys, grp in self.result_df.groupby(group_cols):
            chron = keys[0]
            col = keys[1]
            clim = keys[2]
            tcol = keys[3] if len(keys) > 3 else None
            top = grp.sort_values(by="R", key=lambda s: s.abs(), ascending=False).head(report_k).copy()
            if tcol is not None:
                top["Pair"] = f"{chron}::{col} × {clim}::{tcol}"
            else:
                top["Pair"] = f"{chron}::{col} × {clim}"
            groups.append(top)
        if groups:
            report = pd.concat(groups, ignore_index=True)
        else:
            report = pd.DataFrame()
        self.report_df = report
        self.table_report.setModel(PandasModel(report))
        self.table_report.resizeColumnsToContents()

    def export_results_csv(self):
        """
        Export correlations and (optionally) period correlations.

        Behaviour:
        - If path ends with .xlsx -> one Excel file with multiple sheets:
            * "All correlations"
            * "Top filtered" (if top filter is active)
            * "Periods (all)" (if period analysis was enabled)
            * "Periods (top)" (top-N for periods)
        - Otherwise -> several CSV files with suffixes:
            *_all.csv, *_top.csv, *_periods_all.csv, *_periods_top.csv
        """
        if self.result_df is None or self.result_df.empty:
            QMessageBox.information(self, "No data", "No results to export.")
            return

        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save results",
            "correlations.xlsx",
            "Excel files (*.xlsx);;CSV files (*.csv)",
        )
        if not path:
            return

        is_excel = path.lower().endswith(".xlsx") or "Excel" in selected_filter

        def _prepare_export_df(
            df: Optional[pd.DataFrame],
        ) -> Optional[pd.DataFrame]:
            if df is None:
                return None
            if df.empty:
                return df.copy()

            df_out = df.copy()

            # Significant -> "True"/"False" (English strings for export)
            if "Significant" in df_out.columns:
                df_out["Significant"] = df_out["Significant"].map(
                    lambda v: "True"
                    if (not pd.isna(v) and bool(v))
                    else "False"
                )

            return df_out

        def _sort_results_by_window(
            df: Optional[pd.DataFrame],
        ) -> Optional[pd.DataFrame]:
            """
            Sort result rows by climate window using _compute_window_sort_key.

            Logic:
            - First sort by fixed set of metadata columns:
              * Chronology, Chron_Column, Climate, Agg
              * plus any period-related columns for period tables.
            - Inside each metadata group, sort windows so that:
              * all single-month windows (length = 1) go first:
                pJan..pDec, then Jan..Dec;
              * then multi-month windows, ordered by the same start index and
                by window length.
            """
            if df is None or df.empty or "Window" not in df.columns:
                return df

            df_sorted = df.copy()

            # Convert each window label to:
            #   _WinGroup: 0 = single month, 1 = multi-month, 2 = unknown
            #   _WinStart: month index from _window_label_to_idx (0..23)
            #   _WinLen:   window length in months
            def _split_window(label: Any) -> tuple[int, int, int]:
                start_idx, length = _compute_window_sort_key(label)
                if start_idx >= 999:
                    # unknown / unparsable label -> push to the end
                    return 2, 999, 999
                group = 0 if length == 1 else 1
                return group, start_idx, length

            groups_starts_lens = df_sorted["Window"].map(_split_window)
            df_sorted["_WinGroup"] = groups_starts_lens.map(lambda t: t[0])
            df_sorted["_WinStart"] = groups_starts_lens.map(lambda t: t[1])
            df_sorted["_WinLen"] = groups_starts_lens.map(lambda t: t[2])

            # Detect whether this is a "periods" table (has any Period* columns)
            is_period_table = any(
                "period" in col.lower()
                for col in df_sorted.columns
                if col not in ("Window", "_WinGroup", "_WinStart", "_WinLen")
            )

            # Base metadata columns only, to avoid extra clutter.
            base_cols: list[str] = []
            for col in ("Chronology", "Chron_Column", "Climate", "Agg"):
                if col in df_sorted.columns:
                    base_cols.append(col)

            # For period tables, add all Period* columns to grouping
            if is_period_table:
                for col in df_sorted.columns:
                    if (
                        "period" in col.lower()
                        and col not in base_cols
                        and col not in ("Window", "_WinGroup", "_WinStart", "_WinLen")
                    ):
                        base_cols.append(col)

            # Remove duplicates while keeping order
            seen: set[str] = set()
            meta_cols: list[str] = []
            for col in base_cols:
                if col not in seen:
                    meta_cols.append(col)
                    seen.add(col)

            # Final sort order: metadata, then group/single vs multi, then window start/length
            sort_cols = meta_cols + ["_WinGroup", "_WinStart", "_WinLen"]

            df_sorted = df_sorted.sort_values(by=sort_cols, kind="mergesort")

            return df_sorted.drop(columns=["_WinGroup", "_WinStart", "_WinLen"])

        try:
            # All correlations: prepare and sort by windows
            res_all = _sort_results_by_window(
                _prepare_export_df(self.result_df)
            )

            res_top = (
                _prepare_export_df(self.filtered_result_df)
                if self.filtered_result_df is not None
                and not self.filtered_result_df.empty
                else None
            )

            # Periods (all): same window ordering, but with period columns
            res_period_all = (
                _sort_results_by_window(
                    _prepare_export_df(self.period_result_df)
                )
                if self.period_result_df is not None
                and not self.period_result_df.empty
                else None
            )

            if is_excel:
                if not path.lower().endswith(".xlsx"):
                    path = path + ".xlsx"
                with pd.ExcelWriter(path) as writer:
                    if res_all is not None:
                        res_all.to_excel(
                            writer,
                            sheet_name="All correlations",
                            index=False,
                        )

                    if (
                        res_top is not None
                        and not res_top.empty
                        and res_all is not None
                        and len(res_top) != len(res_all)
                    ):
                        res_top.to_excel(
                            writer,
                            sheet_name="Top filtered",
                            index=False,
                        )

                    if res_period_all is not None and not res_period_all.empty:
                        res_period_all.to_excel(
                            writer,
                            sheet_name="Periods (all)",
                            index=False,
                        )

                        period_top = (
                            self.filtered_period_df
                            if hasattr(self, "filtered_period_df")
                            and self.filtered_period_df is not None
                            else self._apply_top_filter(self.period_result_df)
                        )
                        period_top = _prepare_export_df(period_top)
                        if period_top is not None and not period_top.empty:
                            period_top.to_excel(
                                writer,
                                sheet_name="Periods (top)",
                                index=False,
                            )
            else:
                base, ext = os.path.splitext(path)
                if not ext:
                    base = path
                else:
                    base = path[: -len(ext)]

                if res_all is not None and not res_all.empty:
                    full_path = base + "_all.csv"
                    res_all.to_csv(full_path, index=False)

                if res_top is not None and not res_top.empty:
                    top_path = base + "_top.csv"
                    res_top.to_csv(top_path, index=False)

                if res_period_all is not None and not res_period_all.empty:
                    periods_all = base + "_periods_all.csv"
                    res_period_all.to_csv(periods_all, index=False)

                    period_top = (
                        self.filtered_period_df
                        if hasattr(self, "filtered_period_df")
                        and self.filtered_period_df is not None
                        else self._apply_top_filter(self.period_result_df)
                    )
                    period_top = _prepare_export_df(period_top)
                    if period_top is not None and not period_top.empty:
                        periods_top = base + "_periods_top.csv"
                        period_top.to_csv(periods_top, index=False)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Export error",
                f"Failed to export results:\n{exc}",
            )
            return
        self.notify_saved("Export", "The results were exported.", path)

    
    def _compute_window_sort_key(label: Any) -> Tuple[int, int]:
        """
        Compute numeric sort key (start_index, length) for a climate window label.
    
        Uses _window_label_to_idx so that windows are ordered consistently as:
        pJan..pDec, then Jan..Dec, then longer multi-month windows.
        """
        s = str(label)
        if not s:
            return (999, 999)
    
        # ignore extra tokens like " (something)" if they ever appear
        base = s.split()[0]
        parts = base.split("_")
        if len(parts) == 1:
            start_label = parts[0]
            end_label = parts[0]
        else:
            start_label, end_label = parts[0], parts[-1]
    
        si = _window_label_to_idx(start_label)
        ei = _window_label_to_idx(end_label)
        if si is None or ei is None:
            # unknown labels go to the end
            return (999, 999)
    
        start_idx = min(si, ei)
        length = max(si, ei) - start_idx + 1
        return (start_idx, length)
        
        start_idx = min(si, ei)
        length = max(si, ei) - start_idx + 1
        return (start_idx, length)
    

    def export_plot_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save plot PNG", "plot.png", "PNG files (*.png)")
        if not path:
            return
        try:
            self.canvas.fig.savefig(path, dpi=300)
            self.notify_saved("Saved", "Plot saved.", path)
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))

    def export_report_csv(self):
        if self.report_df is None or self.report_df.empty:
            QMessageBox.information(self, "No report", "No report to export.")
            return

        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save report",
            "report.xlsx",
            "Excel files (*.xlsx);;CSV files (*.csv)",
        )
        if not path:
            return

        is_excel = path.lower().endswith(".xlsx") or "Excel" in selected_filter

        df_export = self.report_df.copy()
        if "Significant" in df_export.columns:
            df_export["Significant"] = df_export["Significant"].map(
                lambda v: "True"
                if (not pd.isna(v) and bool(v))
                else "False"
            )

        try:
            if is_excel:
                if not path.lower().endswith(".xlsx"):
                    path = path + ".xlsx"
                df_export.to_excel(path, index=False)
            else:
                if not path.lower().endswith(".csv"):
                    path = path + ".csv"
                df_export.to_csv(path, index=False)
            self.notify_saved("Saved", "Report saved.", path)
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))

    # -----------------------
    # Project save/load
    # -----------------------
    def save_project_as(self):
        if not self.datasets:
            QMessageBox.information(self, "No data", "No datasets to save.")
            return
        base_dir = self.last_dir_project or os.path.expanduser("~")
        default_path = os.path.join(base_dir, "project.json")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project",
            default_path,
            "JSON files (*.json)",
        )
        if not path:
            return
        proj = {
            "datasets": [],
            "analysis_settings_path": self.settings_path,
        }
        for key, attr in (("moving_screening", "moving_screen_df"), ("field_screening", "field_screen_df")):
            sdf = getattr(self, attr, None)
            if isinstance(sdf, pd.DataFrame) and not sdf.empty:
                try:
                    proj[key] = json.loads(sdf.to_json(orient="split"))
                except Exception as exc:
                    print(f"{key} not stored in the project:", exc)
        if self.result_df is not None and not self.result_df.empty:
            try:
                proj["results"] = json.loads(self.result_df.to_json(orient="split", date_format="iso"))
                if self.period_result_df is not None and not self.period_result_df.empty:
                    proj["period_results"] = json.loads(self.period_result_df.to_json(orient="split"))
            except Exception as exc:
                print("Results not stored in the project:", exc)
        for ds in self.datasets:
            proj["datasets"].append(
                {
                    "name": ds.name,
                    "kind": ds.kind,
                    "climate_param": getattr(ds, "climate_param", "Other"),
                    "year_col": ds.year_col,
                    "value_columns": ds.value_columns,
                    "month_columns": ds.month_columns,
                    "source_freq": getattr(ds, "source_freq", ""),
                    "meta": getattr(ds, "meta", None),
                    "monthly_agg": getattr(ds, "monthly_agg", None),
                    "derived_from_kind": getattr(ds, "derived_from_kind", None),
                    "data": ds.df.to_dict(orient="split"),
                }
            )
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(proj, f, indent=2)
            self.current_project_path = path
            self._results_unsaved = False
            try:
                self.last_dir_project = os.path.dirname(path) or self.last_dir_project
                self.save_settings()
            except Exception:
                pass
            self.notify_saved("Saved", "Project saved" + (" with the analysis results." if "results" in proj
                                                          else "."), path)
        except Exception as e:
            QMessageBox.critical(self, "Save project error", str(e))

 
    def add_project(self):
        """Merge a saved project into the current session without clearing current datasets."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Add project file",
            self.last_dir_open,
            "Project JSON (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                proj = json.load(f)

            self.last_dir_open = os.path.dirname(path) or self.last_dir_open
            # "Add project" always merges into the current session; keep current_project_path unless empty.
            if not getattr(self, "current_project_path", None):
                self.current_project_path = path

            # Ensure dataset names are unique when merging.
            existing_names = set()
            try:
                existing_names = {getattr(ds, "name", "") for ds in self.datasets if getattr(ds, "name", "")}
            except Exception:
                existing_names = set()

            def _make_unique_name(base: str) -> str:
                base = str(base) if base is not None else "dataset"
                if base not in existing_names:
                    existing_names.add(base)
                    return base
                i = 2
                while True:
                    candidate = f"{base} ({i})"
                    if candidate not in existing_names:
                        existing_names.add(candidate)
                        return candidate
                    i += 1

            datasets_ser = proj.get("datasets", [])
            added = 0
            for d in datasets_ser:
                if not isinstance(d, dict):
                    continue
                # Robust loader: supports both legacy {"df": ...} and current {"data": orient="split"} formats.

                try:
                    if (
                        "data" in d
                        and isinstance(d["data"], dict)
                        and {"index", "columns", "data"}.issubset(set(d["data"].keys()))
                    ):
                        df = pd.DataFrame(**d["data"])
                    elif "data" in d and isinstance(d["data"], dict):
                        df = pd.DataFrame(d["data"])
                    else:
                        df = pd.DataFrame(d.get("df", {}))
                except Exception:
                    df = pd.DataFrame(d.get("df", d.get("data", {})))


                ds_name = _make_unique_name(d.get("name", "dataset"))

                kind_raw = str(d.get("kind", "chronology")).lower()
                if kind_raw in ("chron", "chrono", "chronology"):
                    kind = "chronology"
                elif kind_raw in ("clim", "climate"):
                    kind = "climate"
                else:
                    kind = kind_raw

                ds = DataSet(
                    name=ds_name,
                    path=d.get("path", ""),
                    df=df,
                    kind=kind,
                )
                ds.year_col = d.get("year_col", "Year")
                ds.value_columns = d.get("value_columns", getattr(ds, "value_columns", []))
                ds.month_columns = d.get("month_columns", getattr(ds, "month_columns", MONTH_NAMES))
                ds.climate_param = d.get("climate_param", "Other")
                ds.columns_config = d.get("columns_config", None)
                if "source_freq" in d:
                    setattr(ds, "source_freq", d.get("source_freq", ""))
                if "meta" in d:
                    setattr(ds, "meta", d.get("meta", None))
                if "monthly_agg" in d:
                    setattr(ds, "monthly_agg", d.get("monthly_agg", None))
                if "derived_from_kind" in d:
                    setattr(ds, "derived_from_kind", d.get("derived_from_kind", None))
                self.datasets.append(ds)
                if ds.kind == "climate":
                    item = QListWidgetItem(self._format_dataset_label(ds, include_kind=True))
                elif ds.kind == "timeseries":
                    freq = getattr(ds, "source_freq", "")
                    label = f"{ds.name} (timeseries: {freq})" if freq else f"{ds.name} (timeseries)"
                    item = QListWidgetItem(label)
                else:
                    item = QListWidgetItem(self._format_dataset_label(ds, include_kind=False))
                item.setData(Qt.UserRole, ds)
                self.list_datasets.addItem(item)
                added += 1

            self.refresh_analysis_lists()
            self.load_settings()

            QMessageBox.information(
                self,
                "Project merged",
                f"Merged {added} dataset(s) from project.\nCurrent session now has {len(self.datasets)} dataset(s).",
            )
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))

    def load_project(self):
        """Load a saved project from JSON (replace current session or merge into it)."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open project file",
            self.last_dir_open,
            "Project JSON (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                proj = json.load(f)

            # Ask whether to replace the current session or merge into it (keep current datasets).
            merge_mode = False
            if getattr(self, "datasets", None) and len(self.datasets) > 0:
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Question)
                msg.setWindowTitle("Load project")
                msg.setText("How do you want to load this project?")
                msg.setInformativeText(
                    "Open new (replace): clears current datasets and loads the project.\n"
                    "Merge into current: keeps current datasets and adds datasets from the project."
                )
                btn_replace = msg.addButton("Open new (replace)", QMessageBox.AcceptRole)
                btn_merge = msg.addButton("Merge into current", QMessageBox.ActionRole)
                btn_cancel = msg.addButton(QMessageBox.Cancel)
                msg.setDefaultButton(btn_merge)
                msg.exec()
                if msg.clickedButton() == btn_cancel:
                    return
                merge_mode = msg.clickedButton() == btn_merge

            # Replace mode: clear current datasets first.
            if not merge_mode:
                self.datasets.clear()
                self.list_datasets.clear()

            if not getattr(self, "current_project_path", None):
                self.current_project_path = path

            # Ensure dataset names are unique when merging.
            existing_names = set()
            try:
                existing_names = {getattr(ds, "name", "") for ds in self.datasets if getattr(ds, "name", "")}
            except Exception:
                existing_names = set()

            def _make_unique_name(base: str) -> str:
                base = str(base) if base is not None else "dataset"
                if base not in existing_names:
                    existing_names.add(base)
                    return base
                i = 2
                while True:
                    candidate = f"{base} ({i})"
                    if candidate not in existing_names:
                        existing_names.add(candidate)
                        return candidate
                    i += 1

            datasets_ser = proj.get("datasets", [])
            for d in datasets_ser:
                # Robust loader: supports both legacy {"df": ...} and current {"data": orient="split"} formats.
                try:
                    if (
                        "data" in d
                        and isinstance(d["data"], dict)
                        and {"index", "columns", "data"}.issubset(set(d["data"].keys()))
                    ):
                        df = pd.DataFrame(**d["data"])
                    elif "data" in d and isinstance(d["data"], dict):
                        df = pd.DataFrame(d["data"])
                    else:
                        df = pd.DataFrame(d.get("df", {}))
                except Exception:
                    df = pd.DataFrame(d.get("df", d.get("data", {})))
                ds_name = _make_unique_name(d.get("name", "dataset"))
                # Fix: 'kind' must come from the serialized dataset record (fallback to 'chronology').
                kind = d.get("kind", "chronology")
                ds = DataSet(
                    name=ds_name,
                    path=d.get("path", ""),
                    df=df,
                    kind=kind,
                )
                ds.year_col = d.get("year_col", "Year")
                ds.value_columns = d.get("value_columns", getattr(ds, "value_columns", []))
                ds.month_columns = d.get("month_columns", getattr(ds, "month_columns", MONTH_NAMES))

                ds.climate_param = d.get("climate_param", "Other")
                ds.columns_config = d.get("columns_config", None)
                for _attr in ("source_freq", "meta", "monthly_agg", "derived_from_kind"):
                    if _attr in d:
                        setattr(ds, _attr, d.get(_attr))
                self.datasets.append(ds)
                # Add to the main list on the Data tab (analysis lists are rebuilt by refresh_analysis_lists()).
                try:
                    label = self._format_dataset_label(ds, include_kind=True)
                except Exception:
                    label = f"{ds.name} ({ds.kind})"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, ds)
                self.list_datasets.addItem(item)

            self.refresh_analysis_lists()
            self.load_settings()
            for key, attr in (("moving_screening", "moving_screen_df"), ("field_screening", "field_screen_df")):
                if not merge_mode and isinstance(proj.get(key), dict):
                    try:
                        setattr(self, attr, pd.DataFrame(**proj[key]))
                    except Exception:
                        pass
            if not merge_mode and isinstance(proj.get("results"), dict):
                try:
                    res = pd.DataFrame(**proj["results"])
                    per = pd.DataFrame(**proj["period_results"]) if isinstance(proj.get("period_results"), dict) else None
                    self._set_results(res, per)
                    self._results_unsaved = False
                except Exception as exc:
                    print("Saved results could not be restored:", exc)

            QMessageBox.information(
                self,
                "Project loaded" if not merge_mode else "Project merged",
                f"{'Loaded' if not merge_mode else 'Merged'} {len(datasets_ser)} dataset(s) from project.\n"
                f"Current session now has {len(self.datasets)} dataset(s).",
            )
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))

    # -----------------------
    # Settings persistence
    # -----------------------
    def load_settings(self):
        try:
            if os.path.exists(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                # window geometry + last directories
                self.last_dir_open = str(s.get("last_dir_open", self.last_dir_open))
                self.last_dir_project = str(s.get("last_dir_project", self.last_dir_project))
                self.last_dir_export = str(s.get("last_dir_export", self.last_dir_export))
                if isinstance(s.get("online_data"), dict):
                    self.online_settings = dict(s.get("online_data"))

                geo_b64 = s.get("window_geometry_b64")
                if isinstance(geo_b64, str) and geo_b64.strip():
                    try:
                        self.restoreGeometry(QByteArray.fromBase64(geo_b64.encode("ascii")))
                    except Exception:
                        self._apply_default_window_geometry()
                else:
                    self._apply_default_window_geometry()

                if "ui_scale" in s:
                    try:
                        val = float(s.get("ui_scale", 1.0))
                        self.ui_scale = val
                        if hasattr(self, "spin_ui_scale"):
                            self.spin_ui_scale.blockSignals(True)
                            self.spin_ui_scale.setValue(val)
                            self.spin_ui_scale.blockSignals(False)
                        self._apply_ui_scale(val)
                    except Exception:
                        pass
                if "spin_min_len" in s:
                    self.spin_min_len.setValue(int(s.get("spin_min_len", self.spin_min_len.value())))
                if "spin_max_len" in s:
                    self.spin_max_len.setValue(int(s.get("spin_max_len", self.spin_max_len.value())))
                if "combo_agg" in s:
                    val = s.get("combo_agg")
                    if val in [self.combo_agg.itemText(i) for i in range(self.combo_agg.count())]:
                        self.combo_agg.setCurrentText(val)
                if "combo_corr" in s:
                    val = s.get("combo_corr")
                    if val in [self.combo_corr.itemText(i) for i in range(self.combo_corr.count())]:
                        self.combo_corr.setCurrentText(val)
                if "spin_top_n" in s:
                    self.spin_top_n.setValue(int(s.get("spin_top_n", 0)))
                if "combo_top_mode" in s:
                    val = s.get("combo_top_mode")
                    if val in [self.combo_top_mode.itemText(i) for i in range(self.combo_top_mode.count())]:
                        self.combo_top_mode.setCurrentText(val)
                if "spin_report_k" in s:
                    self.spin_report_k.setValue(int(s.get("spin_report_k", 10)))
                if "chk_exclude_single" in s:
                    self.chk_exclude_single.setChecked(bool(s.get("chk_exclude_single", False)))
                if "spin_min_n" in s and hasattr(self, "spin_min_n"):
                    self.spin_min_n.setValue(int(s.get("spin_min_n", 15)))
                if "spin_year_start" in s and "spin_year_end" in s:
                    try:
                        self.spin_year_start.setValue(int(s.get("spin_year_start", self.spin_year_start.value())))
                        self.spin_year_end.setValue(int(s.get("spin_year_end", self.spin_year_end.value())))
                    except Exception:
                        pass
                # NEW: season + periods
                if "combo_season" in s:
                    val = s.get("combo_season")
                    if val in [self.combo_season.itemText(i) for i in range(self.combo_season.count())]:
                        self.combo_season.setCurrentText(val)
                if "spin_season_start" in s:
                    self.spin_season_start.setValue(int(s.get("spin_season_start", self.spin_season_start.value())))
                if "spin_season_end" in s:
                    self.spin_season_end.setValue(int(s.get("spin_season_end", self.spin_season_end.value())))
                if "chk_enable_periods" in s:
                    self.chk_enable_periods.setChecked(bool(s.get("chk_enable_periods", False)))
                if "spin_period_min_span" in s:
                    self.spin_period_min_span.setValue(int(s.get("spin_period_min_span", self.spin_period_min_span.value())))
                if "spin_period_step" in s:
                    self.spin_period_step.setValue(int(s.get("spin_period_step", self.spin_period_step.value())))
                if "spin_min_period_points" in s:
                    self.spin_min_period_points.setValue(int(s.get("spin_min_period_points", self.spin_min_period_points.value())))
                if "period_mode" in s:
                    mode_val = s.get("period_mode")
                    if mode_val == "sliding":
                        self.radio_period_sliding.setChecked(True)
                    elif mode_val == "grid":
                        self.radio_period_grid.setChecked(True)
                    else:
                        self.radio_period_anchored.setChecked(True)
                if "ui_scale" in s:
                    try:
                        val = float(s.get("ui_scale", 1.0))
                        if hasattr(self, "spin_ui_scale"):
                            self.spin_ui_scale.setValue(val)
                        self.apply_ui_scale(val)
                    except Exception:
                        pass
        except Exception as e:
            print("Failed to load settings:", e)

            # still make sure window is usable on small screens
            self._apply_default_window_geometry()

    def save_settings(self):
        s = {
            "window_geometry_b64": bytes(self.saveGeometry().toBase64()).decode("ascii"),
            "last_dir_open": self.last_dir_open,
            "last_dir_project": self.last_dir_project,
            "last_dir_export": self.last_dir_export,
            "spin_min_len": int(self.spin_min_len.value()),
            "spin_max_len": int(self.spin_max_len.value()),
            "combo_agg": self.combo_agg.currentText(),
            "combo_corr": self.combo_corr.currentText(),
            "spin_top_n": int(self.spin_top_n.value()),
            "combo_top_mode": self.combo_top_mode.currentText(),
            "spin_report_k": int(self.spin_report_k.value()),
            "chk_exclude_single": bool(self.chk_exclude_single.isChecked()),
            "spin_min_n": int(self.spin_min_n.value()),
            "spin_year_start": int(self.spin_year_start.value()),
            "spin_year_end": int(self.spin_year_end.value()),
            "combo_season": self.combo_season.currentText(),
            "spin_season_start": int(self.spin_season_start.value()),
            "spin_season_end": int(self.spin_season_end.value()),
            "chk_enable_periods": bool(self.chk_enable_periods.isChecked()),
            "spin_period_min_span": int(self.spin_period_min_span.value()),
            "spin_period_step": int(self.spin_period_step.value()),
            "spin_min_period_points": int(self.spin_min_period_points.value()),
            "period_mode": "grid"
            if self.radio_period_grid.isChecked()
            else ("sliding" if self.radio_period_sliding.isChecked() else "anchored"),
            "ui_scale": float(self.spin_ui_scale.value()) if hasattr(self, "spin_ui_scale") else 1.0,
            "online_data": (
                self._online_dialog.get_settings()
                if getattr(self, "_online_dialog", None) is not None
                else getattr(self, "online_settings", {})
            ),
        }
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print("Failed to save settings:", e)

    def closeEvent(self, event):
        if getattr(self, "_analysis_running", False):
            if QMessageBox.question(self, "Analysis running", "An analysis is still running. Stop it and close?") \
                    != QMessageBox.Yes:
                event.ignore()
                return
            self._closing = True
            ctl = getattr(self, "_analysis_ctl", None)
            if ctl is not None:
                ctl.cancel.set()
        if getattr(self, "_results_unsaved", False) and self.result_df is not None and not self.result_df.empty:
            ans = QMessageBox.question(
                self, "Unsaved results",
                f"The analysis results ({len(self.result_df):,} rows) have not been saved or exported.\n\n"
                "Save the project with the results before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
            if ans == QMessageBox.Cancel:
                event.ignore()
                return
            if ans == QMessageBox.Save:
                self.save_project_as()
                if getattr(self, "_results_unsaved", False):  # save dialog cancelled
                    event.ignore()
                    return
        self._discard_recovery()
        try:
            self.save_settings()
        except Exception as e:
            print("Error saving settings:", e)
        # Online data: keep the download list / cached values for the next start?
        if _online_qt is not None and getattr(_online_qt, "SQ", None) is not None:
            try:
                _online_qt.SQ.ask_keep_cache_qt(self)
            except Exception:
                pass
        super().closeEvent(event)


# ---------------------------


# Application entry point
# ---------------------------
def main():
    app = QApplication(sys.argv)
    window = MainWindow()
def main():
    argv = list(sys.argv)
    prefill_manifest: Optional[str] = None
    if "--prefill" in argv:
        idx = argv.index("--prefill")
        if idx + 1 < len(argv):
            prefill_manifest = argv[idx + 1]
        # Remove custom args so Qt does not see them.
        for j in sorted([idx, idx + 1 if idx + 1 < len(argv) else idx], reverse=True):
            try:
                argv.pop(j)
            except Exception:
                pass

    _set_windows_app_user_model_id("Bakhtiyorov.RingClim")
    app = QApplication(argv)
    _apply_modern_style(app)
    _apply_app_icon(app)
    if _uihelp is not None:
        _uihelp.install_qt(app)
    window = MainWindow(prefill_manifest=prefill_manifest)

    window.show()
    if _uihelp is not None:
        # main window already shows the signature in the status bar: tooltips only
        QTimer.singleShot(300, lambda: _uihelp.decorate_qt(window, footer=False))
    QTimer.singleShot(0, window._request_user_attention)
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing as _mp

    _mp.freeze_support()  # parallel analysis workers in the packaged program
    main()

# Developed by Dr. Zulfiyor Bakhtiyorov
