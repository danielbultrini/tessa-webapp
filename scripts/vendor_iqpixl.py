"""Copy ../src/iqpixl into webapp/iqpixl, so the deployed app carries the library.

Vercel builds from this folder and cannot reach ../src at runtime. Run this
before every deploy (and after any change to src/iqpixl):

    python webapp/scripts/vendor_iqpixl.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

WEBAPP = Path(__file__).resolve().parents[1]
SOURCE = WEBAPP.parent / "src" / "iqpixl"
TARGET = WEBAPP / "iqpixl"

if __name__ == "__main__":
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"copied {SOURCE} -> {TARGET}")
