# Runtime Dependencies

## Python dependencies (`requirements.txt`)
This repository currently tracks scientific/data/GUI dependencies including:
- numpy
- pandas
- scipy
- matplotlib
- statsmodels
- openpyxl
- XlsxWriter
- PySide6
- shiboken6
- xarray
- netCDF4
- h5netcdf
- cftime
- xlrd
- pyxlsb
- odfpy

These dependencies include core runtime and feature-specific support. Not every dependency is required for every workflow path.

## Build dependencies (`requirements-build.txt`)
- pyinstaller
- pyinstaller-hooks-contrib

These are build/distribution-focused and are not required for every source runtime use case.

## Packaged-build download and use (end users)
Ready-made packaged builds are provided in this external folder:

https://drive.google.com/drive/folders/1fQLibCdZHOb9q1mPqpIHZJV1EWFfFGJ3?usp=drive_link

The external download location includes separate Windows and macOS packaged-build directories.  
After download, users can extract the packaged build archive to any convenient location and run it from there.

## GUI runtime stacks
- `detrend_advisor.py` uses **Tkinter** (platform/runtime dependency, usually provided with Python installation).
- `ringclim.py` uses **PySide6 / shiboken6** (Python package dependencies).

## External runtimes for Detrend Advisor workflows
For dplR-oriented workflows in `detrend_advisor.py`:
- external **R / Rscript** runtime is required,
- external R package **dplR** is required for those workflow paths.

These dependencies are feature-specific and should be documented per workflow/use mode.

## Strong recommendation for packaged-build users
- Even when using packaged builds, it is **very strongly recommended** that R and dplR are already installed on the target computer.
- Do not claim or assume that R/dplR are bundled unless a specific release has verified and documented that they are bundled.
- Avoid broad guarantees that every workflow path will operate identically without R/dplR.

## Conservative compatibility note
This repository documents dependencies as they are visible in source and requirements files. It does not claim universal runtime parity across all operating systems or packaging targets without release-specific verification.
