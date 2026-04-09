# Architecture

## Repository-level model
This repository is one integrated software suite composed of two connected desktop applications:

1. **`detrend_advisor.py`**
   - Primary upstream dendrochronological detrending/advisory application.
   - Tkinter GUI with analysis/export and validation-oriented modes.

2. **`ringclim.py`**
   - Downstream companion climate-analysis application.
   - PySide6 GUI for chronology/climate/time-series analysis.

## Connectivity
- Detrend Advisor can launch RingClim after export and pass prepared datasets through a prefill manifest workflow.
- RingClim can also be launched independently and used with separately imported datasets.

## Important clarification
The suite is connected but **not** a monolithic single-window GUI. Each application remains independently runnable as a standalone desktop app.
