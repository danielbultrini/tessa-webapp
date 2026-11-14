"""Import the app's own modules (and its vendored iqpixl) the way Vercel does."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
