
import os as _os

# Admission holds (hyperloom/admission.py) stay in memory during tests: never in a real configuration directory.
_os.environ.setdefault("HYPERLOOM_ADMISSION_DB", ":memory:")
