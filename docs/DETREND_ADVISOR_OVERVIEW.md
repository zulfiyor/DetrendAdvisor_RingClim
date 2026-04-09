# Detrend Advisor Overview (`detrend_advisor.py`)

## Role
Primary dendrochronology-focused detrending and advisory desktop app in this suite.

## Inputs (high-level)
- Ring-width-like data (for example TRW / BI) from CSV, Tucson-style text, generic text, JSON, and Excel.
- Optional climate/target CSV input.
- Optional pith-offset input for RCS/validation contexts.

## Goals
Supported goal keys include:
- `highfreq`
- `mixed`
- `lowfreq`
- `temp_recon`
- `runoff_recon`
- `drought_recon`
- `variance_check`

## Detrending/advisory workflow
- Runs diagnostics and candidate-method comparison.
- Includes workflows around exponential, spline, RCS, and signal-free RCS contexts.
- Produces recommendation-oriented outputs with chronology and RWI context.

## Exports and validation
- Exports chronology outputs and reports.
- Supports batch/headless and validation-oriented CLI modes.
- Includes EPS/Rbar-oriented diagnostics and method-comparison context.
- Runtime logs and report outputs support auditability.

## R / dplR context
- User-facing workflow emphasizes dplR-oriented execution through external R/Rscript runtime.
- dplR availability is feature-relevant for key detrending/validation workflows.

## Suite integration
- Can launch `ringclim.py` and pass prepared export outputs via prefill manifest for downstream climate analysis.
