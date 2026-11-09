"""Product description used by app_update*.py (releases, updates, contact, guides)."""
PRODUCT = "DeClim"                              # folder name, archive and manifest prefix (no spaces)
DISPLAY_NAME = "DeClim (Detrend Advisor + RingClim)"
REPOSITORY = "zulfiyor/DeClim"  # GitHub owner/repository with the releases
DEVELOPER = "Dr. Zulfiyor Bakhtiyorov"
CONTACT_EMAIL = "zb308@cam.ac.uk"
# user guides attached to every release (asset file names); also copied to <product folder>/docs
GUIDES = [
    ("User guide (PDF)", "DeClim_User_Guide.pdf"),
    ("User guide (Word)", "DeClim_User_Guide.docx"),
    ("Worked example data (KYRG002)", "DeClim_example_KYRG002.zip"),
]
