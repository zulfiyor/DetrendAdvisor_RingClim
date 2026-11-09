# Changelog - DeClim (Detrend Advisor + RingClim)

The version is set in `VERSION.txt`; the build number is added by the build scripts.
Write the changes of the next version under its own heading before building a release.

## 2.1.1
- Detrend Advisor: switching the displayed results between POWT ON and POWT OFF now refreshes every tab
  (data structure, POWT, EPS / Rbar / Depth, comparison); the EPS / Rbar plot no longer showed stale values.
- Detrend Advisor: plot titles, exported file names and the comparison tab follow the results that are shown
  (POWT ON or OFF) instead of the run setting, so exports are no longer mislabelled.
- Updated user guide and example data (KYRG002) made with version 2.1.

## 2.1.0
- Detrend Advisor: *Custom methods...* adds parameterized detrending methods without code - splines and
  signal-free splines of any period (3-10000 years, f = 0.5) and running means of any length; they are
  computed next to the standard presets, ranked by the same goal score, explained in the method guide and
  saved in the preferences and in projects.
- RingClim, moving correlation: *Screen all seasons x all climate data* shortens the running window
  automatically when the common period of a chronology and a climate data set is short (at least 7 years);
  the new column *Window years* shows the window used, and adjusted or skipped pairs are reported.
- RingClim: the right-click menu of the batch download table (retry, pause, fixes) works again.
- macOS: *Open RingClim* starts the RingClim app bundle and reports a failed start.
- Updated user guide (custom methods, moving and spatial correlation with screening, result filters,
  parallel and incremental analysis, session recovery, batch pause/resume and fixes, updates).

## 2.0.1
- Open RingClim from Actions brings the import prompt forward on macOS; dialogs stay attached to Detrend Advisor.
- Hovering over large chronology plots no longer scans every value on every mouse movement.
- The Export mode dialog is centered, reliably focused and keeps all three choices visible in packaged builds.
- Configure automatic updates to use the new public GitHub repository, zulfiyor/DeClim.

## 2.0.0
- Detrend Advisor: direct ITRDB search and import (free text such as "Juniper Turkey"), docked windows,
  click/hover on series and chronologies with explanations of peaks, corrected POWT and residual chronologies.
- RingClim: twelve online climate sources with batch download for the area (finished sources are usable at
  once, pause/resume, right-click fixes), spatial (field) correlation maps like the KNMI Climate Explorer,
  moving (running) correlations with stability statistics, disk cache shared between program windows.
- Built-in updates, user guides and contact with the developer (*Updates* button).
