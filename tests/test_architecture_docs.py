from pathlib import Path


def test_readme_mentions_two_app_model():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "two connected but independently runnable desktop applications" in text
    assert "detrend_advisor.py" in text
    assert "ringclim.py" in text
    assert "prefill manifest" in text
