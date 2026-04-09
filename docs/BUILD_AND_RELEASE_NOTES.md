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
- Public packaged-build download location:  
  https://drive.google.com/drive/folders/1fQLibCdZHOb9q1mPqpIHZJV1EWFfFGJ3?usp=drive_link
- Keep Windows and macOS packaged builds in clearly separate folders.
- State clearly in release notes that end users can extract build archives to any convenient location and run from there.

## Runtime dependency communication
- Document R/Rscript/dplR requirements accurately for dplR-oriented Detrend Advisor workflows.
- For packaged-build release notes, very strongly recommend that R and dplR are already installed on end-user computers.
- Do not claim external runtimes are bundled unless they are actually packaged and verified.
- Describe package scope per release artifact (platform, included components, known limitations).

## Practical checklist before release
- Confirm README and docs match actual architecture.
- Confirm citation files are present and non-fabricated.
- Confirm license and NOTICE consistency.
- Run CI and local smoke checks.
- Confirm release notes include the external Google Drive download link and that it mentions separate Windows/macOS folders.
- Confirm release notes explicitly say users can extract packaged builds to any convenient location and run from there.
- Confirm release notes include the very strong recommendation to have R and dplR preinstalled.
- Tag release notes with clear scope of packaged/source deliverables.
