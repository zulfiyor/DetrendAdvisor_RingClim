# User Guide

## Quick start: Detrend Advisor
1. Run `python detrend_advisor.py`.
2. Import ring-width-like input (CSV/Tucson text/TXT/JSON/Excel).
3. (Optional) import climate/target CSV.
4. Select goal (`highfreq`, `mixed`, `lowfreq`, `temp_recon`, `runoff_recon`, `drought_recon`, `variance_check`).
5. Run diagnostics/advisory workflow.
6. Review recommended method and outputs (chronologies/RWI, EPS/Rbar context, diagnostics, logs).
7. Export chronology/report outputs.

## Quick start: RingClim
1. Run `python ringclim.py`.
2. Import chronology/climate/time-series datasets.
3. Review auto-detected mappings/import settings where offered.
4. Configure analysis window settings.
5. Run correlation/window workflows.
6. Review plots/tables and export results/reports.

## Connected workflow: Detrend Advisor -> RingClim
1. Prepare and export outputs in Detrend Advisor.
2. Use Detrend Advisor launch/handoff path to open RingClim with prefill manifest.
3. Continue downstream climate-correlation and climate-window analysis.
4. Export final analysis/report artifacts from RingClim.

## Decision guide
- Use **`detrend_advisor.py`** for detrending-method recommendation and chronology/RWI generation.
- Use **`ringclim.py`** for climate correlation analysis and reporting.
- Use **both together** for end-to-end detrending-to-climate workflow.
