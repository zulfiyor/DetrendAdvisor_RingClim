# Contributing

Thank you for your interest in improving this repository.

## Repository model
This is one connected software suite with two standalone desktop applications:
- `detrend_advisor.py` (primary upstream detrending/advisory app)
- `ringclim.py` (companion downstream climate-analysis app)

Please keep this architecture explicit in code and documentation updates.

## How to contribute
1. Fork the repository.
2. Create a feature/fix branch from the default branch.
3. Keep changes focused and minimal.
4. Run local checks (syntax and tests) before submitting a PR.
5. Open a pull request using the provided PR template.

## Development setup
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-build.txt
pip install pytest
```

## Coding guidance
- Preserve runtime behavior unless the change explicitly requires modification.
- Do not rename the primary scripts.
- Avoid broad refactors in small PRs.
- Keep comments/docs in English.
- Add/update documentation when behavior or workflow expectations change.

## Citation and authorship
When changing citation metadata/docs, do not invent publication metadata. Use TODO placeholders if canonical publication details are not yet available.
