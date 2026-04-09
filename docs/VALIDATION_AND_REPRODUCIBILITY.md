# Validation and Reproducibility

## Validation-oriented modes
`detrend_advisor.py` includes CLI paths for:
- batch/headless export workflows,
- validation against dplR-oriented computation context,
- EPS/Rbar validation outputs.

## Reproducibility context
- dplR-oriented workflows rely on external R/Rscript + dplR versions.
- Reproducibility statements should include environment details (Python version, dependency versions, R version, dplR version, OS).
- Avoid overclaiming deterministic equivalence across all platforms/configurations.

## Suggested environment capture
For reports/publications, document:
- software suite version/tag,
- Python version and installed requirements snapshot,
- R/Rscript version,
- dplR version,
- input data provenance and preprocessing steps,
- key run options (goal, mode, method subset, validation options).

## Batch and export workflows
Where possible, keep exported outputs, runtime logs, and settings metadata together to support auditability and reruns.
