## Summary
- Describe what changed and why.

## Architecture checklist
- [ ] I preserved the two-application model:
  - `detrend_advisor.py` is the primary upstream detrending/advisory app.
  - `ringclim.py` is the downstream companion climate-analysis app.
- [ ] I did not describe this project as a single embedded launcher window.
- [ ] If relevant, I documented Detrend Advisor → RingClim prefill-manifest handoff behavior.

## Validation
- [ ] I ran syntax/tests locally (or documented environment limitations).

## Documentation
- [ ] I updated docs/README as needed.
- [ ] I did not invent publication metadata (DOI/journal/etc.).
