# DetrendAdvisor + RingClim Suite

Integrated desktop suite for dendrochronological detrending advice, chronology export, and tree-ring climate correlation analysis.

## Overview
DetrendAdvisor_RingClim is a connected research software suite for dendrochronology and climate analysis. The repository contains an upstream detrending/advisory desktop application (`detrend_advisor.py`) and a downstream companion climate-analysis desktop application (`ringclim.py`). The two tools can be used together in one workflow, and each can also be run independently as a standalone application.

## What this repository is for
This repository supports practical, reproducible workflows for:
- dendrochronological detrending advice and method comparison,
- chronology and RWI-oriented exports,
- tree-ring to climate correlation analysis,
- climate-window screening and reporting,
- validation-oriented workflows, and
- handoff from Detrend Advisor to RingClim via a prefill manifest.

## Software model (important)
This repository contains **two connected but independently runnable desktop applications**:

1. **`detrend_advisor.py`** (primary/upstream): GUI detrending advisor for ring-width-like data (for example TRW / BI), with diagnostics, method comparisons, recommendations, exports, and validation-oriented modes.
2. **`ringclim.py`** (companion/downstream): GUI tree-ring/climate analysis app for importing chronology/climate/time-series data, running correlation and climate-window analysis, visualizing results, and exporting outputs.

`detrend_advisor.py` can launch RingClim and pass prepared outputs via a **prefill manifest** workflow. However, this repository is **not** a single embedded launcher window; both apps remain standalone runnable desktop programs.

## Applications at a glance

### `detrend_advisor.py`
- Tkinter desktop app with Matplotlib/Tk backend.
- Imports ring-width-like data from CSV, Tucson-style text, generic text, JSON, and Excel.
- Optional climate/target CSV input.
- Supports goals such as `highfreq`, `mixed`, `lowfreq`, `temp_recon`, `runoff_recon`, `drought_recon`, and `variance_check`.
- Runs detrending advisor workflows (including exponential, spline, RCS, and signal-free RCS contexts), diagnostics, and recommendation logic.
- Includes GUI usage plus validation/batch-oriented CLI modes.
- Exports chronology/report outputs and can hand off prepared files to RingClim.

### `ringclim.py`
- PySide6 desktop app for chronology/climate correlation workflows.
- Imports chronology, monthly climate, and time-series style inputs from text/CSV/Excel and related supported formats.
- Includes import helpers and auto-detection for common layouts and some specialized data formats.
- Supports climate-window analysis (including daily/hourly and monthly contexts), visualization, export, and report generation.
- Can run independently, or as downstream continuation after Detrend Advisor export.

## Connected workflow (Detrend Advisor → RingClim)
Typical end-to-end use:
1. Import dendro data in Detrend Advisor.
2. Choose analysis goal and run detrending/advisory workflows.
3. Review recommendations, chronologies/RWI outputs, diagnostics, EPS/Rbar context, compare outputs, and runtime logs.
4. Export chronology/RWI outputs.
5. Launch RingClim from Detrend Advisor using the prefill-manifest handoff.
6. Continue with climate-correlation analysis, climate-window scans, plots, and report/export steps in RingClim.

## Standalone use cases
- **Use only `detrend_advisor.py`** when your main objective is detrending-method evaluation, chronology/RWI preparation, diagnostics, or validation-oriented outputs.
- **Use only `ringclim.py`** when your main objective is climate-correlation and window-based analysis from already prepared chronology/climate datasets.
- **Use both together** when you want an integrated detrending-to-climate workflow from raw dendro-style input to climate interpretation.

## Installation and run from source

### Recommended Python version
- Recommended: **Python 3.11+** (conservative recommendation for current dependency set and desktop workflows).

### 1) Create environment and install runtime dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2) Optional build dependencies
```bash
pip install -r requirements-build.txt
```

### 3) Run Detrend Advisor
```bash
python detrend_advisor.py
```

### 4) Run RingClim
```bash
python ringclim.py
```

### 5) Connected handoff usage
Run Detrend Advisor first, perform detrending/export, then use its RingClim launch/handoff path to open `ringclim.py` with prefilled data manifest context.

## Runtime dependencies and external tools
- **Tkinter**: runtime/platform dependency for `detrend_advisor.py` (typically provided by Python distribution; not a PyPI package in this repo).
- **PySide6 + shiboken6**: Python runtime dependencies for `ringclim.py`.
- **R / Rscript + dplR**: external runtime dependency for dplR-oriented workflows in `detrend_advisor.py`.
- Some dependencies are feature-specific; see [docs/RUNTIME_DEPENDENCIES.md](docs/RUNTIME_DEPENDENCIES.md).

## Packaged builds (end users)
If you use packaged desktop builds instead of source execution, follow release notes for that artifact.

- Release/download location: **TODO: add official release location**
- Packaged builds may include a subset/scope of workflows per platform and release.
- External runtime dependencies (for example R/dplR) must be documented per release if required.

## Citation
This repository is intended to be cited as a connected software suite covering both Detrend Advisor and RingClim.

- See [CITATION.cff](CITATION.cff) for machine-readable metadata.
- See [CITATION_POLICY.md](CITATION_POLICY.md) for scholarly citation guidance.
- In-app citation dialogs currently indicate that finalized article text may be added after program paper publication.

If full publication metadata is not yet available, TODO placeholders are used intentionally instead of guessed values.

## License
This project is licensed under the MIT License. See [LICENSE](LICENSE).

Note: MIT license terms do not themselves mandate academic citation, but scholarly citation is strongly encouraged for research reuse (see citation policy).

## Limitations
- Desktop-GUI-first design.
- Mixed GUI stacks across the suite (Tkinter in Detrend Advisor, PySide6 in RingClim).
- Some workflows are feature-specific and depend on optional/external runtimes.
- dplR-oriented detrending/validation workflows depend on external R/Rscript/dplR availability.
- Format-specific edge cases may require manual import/cleaning.
- Packaging/runtime behavior can differ by operating system and release artifact.

## Additional documentation
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- [docs/DETREND_ADVISOR_OVERVIEW.md](docs/DETREND_ADVISOR_OVERVIEW.md)
- [docs/RINGCLIM_OVERVIEW.md](docs/RINGCLIM_OVERVIEW.md)
- [docs/RUNTIME_DEPENDENCIES.md](docs/RUNTIME_DEPENDENCIES.md)
- [docs/VALIDATION_AND_REPRODUCIBILITY.md](docs/VALIDATION_AND_REPRODUCIBILITY.md)
- [docs/BUILD_AND_RELEASE_NOTES.md](docs/BUILD_AND_RELEASE_NOTES.md)
