# DeClim — Detrend Advisor + RingClim

**DeClim** is a pair of linked desktop programs for dendroclimatology, from raw ring widths to the climate response:

- **Detrend Advisor** standardises tree-ring measurements (RWL): it computes many detrending methods at once with R/dplR, scores them against the goal of the study and exports the chronologies with a report. ITRDB chronologies can be searched and imported directly.
- **RingClim** (Ring-Climate Analyzer) correlates chronologies with climate: it imports almost any table layout, downloads climate, drought, reanalysis and station data for your sites, screens all monthly or daily windows of the previous and current year and presents the results as tables, heatmaps, spatial correlation maps, moving correlations and reports.

One command in Detrend Advisor opens RingClim with all exported chronologies already loaded.

## Download

Ready-to-use packages — nothing to install — are on the **[Releases page](https://github.com/zulfiyor/DeClim/releases/latest)**:

| System | Package |
|---|---|
| Windows 10/11 (64-bit) | `DeClim-Windows-x64.zip` |
| macOS Intel | `DeClim-macOS-x64.zip` |

1. Unpack the ZIP into a folder where you can write (Documents, Desktop, your user folder — not *Program Files*).
2. Start `DeClim/detrend_advisor/detrend_advisor.exe` or `DeClim/ringclim/ringclim.exe` (macOS: `DeClim/detrend_advisor.app`, `DeClim/ringclim.app`).
3. Detrend Advisor computes with **R** and the R package **dplR** (both free). The program finds R or helps to install it on first use.

### Updates
Both programs check this page once a day (button **Updates** → *Check for updates…*). A new version is downloaded only after you agree, verified with SHA-256 and installed when the program windows are closed; the previous version is kept for *Restore previous version*. Settings, projects and your own files are kept.
Each published release must include the platform ZIP and its matching `DeClim-update-<platform>.json` file. Keep their generated filenames unchanged.

## User guide
- [DeClim User Guide (PDF)](https://github.com/zulfiyor/DeClim/releases/latest/download/DeClim_User_Guide.pdf) · [Word](https://github.com/zulfiyor/DeClim/releases/latest/download/DeClim_User_Guide.docx)
- [Worked example data (KYRG002)](https://github.com/zulfiyor/DeClim/releases/latest/download/DeClim_example_KYRG002.zip)

The current macOS package is built for Intel. Apple Silicon Macs can run it with Rosetta 2.
The guides are included in every release and in the `docs` folder of the package.

## Main features
- Many detrending methods side by side (negative exponential, splines, age-dependent spline, RCS, Friedman, Hugershoff, AR models …) with automatic scoring and peak diagnostics; STD, RES and ARS chronologies.
- Custom detrending methods without code: splines, signal-free splines and running means of any period, ranked together with the presets.
- Direct ITRDB search by map, area or free text (e.g. "Juniper Turkey").
- Online climate data for chronology locations: CRU TS, TerraClimate, CHELSA, CHIRPS, GPCC, SPEI/SPI, scPDSI, Berkeley Earth, GHCN-Daily stations, ERA5 / ERA5-Land and GloFAS (with your own free Copernicus key) and more; batch download for the whole area.
- Correlations with all monthly or daily windows of the previous and current year, multi-year periods, ranked tables, heatmaps and reports; parallel and incremental analysis, filters of the results without re-running, automatic session recovery.
- Spatial (field) correlation maps like the KNMI Climate Explorer, with the site marked, and screening of all variables and windows for the region (effective sample size, shuffle test).
- Moving (running) correlations with stability statistics and a test of change, and screening of all seasons for the most stable signals.

## Contact and feedback
Suggestions, comments and problem reports: **Updates → Contact the developer…** in the programs, e-mail **zb308@cam.ac.uk**, or [GitHub issues](https://github.com/zulfiyor/DeClim/issues).

## Citation
Bakhtiyorov, Z. DeClim: Detrend Advisor and RingClim for dendroclimatology (manuscript under review). Please cite this repository until the paper is published.

## License
MIT License — see [LICENSE](LICENSE). Developed by Dr. Zulfiyor Bakhtiyorov (University of Cambridge; Xinjiang Institute of Ecology and Geography, CAS; Khujand Scientific Centre, NAS Tajikistan).
