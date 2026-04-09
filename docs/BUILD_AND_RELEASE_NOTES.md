# Build and Release Notes

## Source release guidance
- Keep release notes explicit that this repository is a connected two-application suite.
- Document standalone run instructions for both apps.
- Document connected Detrend Advisor -> RingClim prefill-manifest workflow.

## Packaged desktop build guidance
- Packaging may require separate handling for two GUI stacks:
  - Tkinter-based Detrend Advisor
  - PySide6-based RingClim
- Verify packaged behavior of each app independently.
- Validate downstream handoff behavior when both apps are included.

## Runtime dependency communication
- Document R/Rscript/dplR requirements accurately for dplR-oriented Detrend Advisor workflows.
- Do not claim external runtimes are bundled unless they are actually packaged and verified.
- Describe package scope per release artifact (platform, included components, known limitations).

## Practical checklist before release
- Confirm README and docs match actual architecture.
- Confirm citation files are present and non-fabricated.
- Confirm license and NOTICE consistency.
- Run CI and local smoke checks.
- Tag release notes with clear scope of packaged/source deliverables.
