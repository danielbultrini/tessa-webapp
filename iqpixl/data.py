"""Locate the bundled example inputs (``qcat.png``, ``telemann_small.mid``, ...).

``data_path("qcat.png")`` works from a source checkout, an editable install and a
built wheel.  Resolution order:

1. ``$IQPIXL_DATA_DIR`` if set,
2. the repository's top-level ``data/`` directory (source checkout / ``pip -e .``),
3. the ``iqpixl/data`` directory packaged inside the wheel.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["data_dir", "data_path", "outputs_dir"]

_PACKAGE_ROOT = Path(__file__).resolve().parent


def _candidate_dirs() -> list[Path]:
    cands: list[Path] = []
    env = os.environ.get("IQPIXL_DATA_DIR")
    if env:
        cands.append(Path(env).expanduser())
    # Walk up from src/iqpixl/ looking for a repo root that holds data/.
    for parent in _PACKAGE_ROOT.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "data").is_dir():
            cands.append(parent / "data")
            break
    cands.append(_PACKAGE_ROOT / "data")  # packaged copy
    return cands


def data_dir() -> Path:
    """Directory holding the bundled example inputs."""
    for cand in _candidate_dirs():
        if cand.is_dir():
            return cand
    raise FileNotFoundError(
        "could not locate the iqpixl data directory; set $IQPIXL_DATA_DIR"
    )


def data_path(name: str) -> Path:
    """Absolute path to a bundled example input, e.g. ``data_path("qcat.png")``."""
    for cand in _candidate_dirs():
        p = cand / name
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"{name!r} not found in any of: "
        + ", ".join(str(c) for c in _candidate_dirs())
        + " (set $IQPIXL_DATA_DIR to point at the data directory)"
    )


def outputs_dir(create: bool = True) -> Path:
    """Directory for generated artifacts (``$IQPIXL_OUTPUT_DIR`` or ``<repo>/outputs``).

    Everything here is git-ignored and safe to delete.
    """
    env = os.environ.get("IQPIXL_OUTPUT_DIR")
    if env:
        out = Path(env).expanduser()
    else:
        out = Path.cwd() / "outputs"
        for parent in _PACKAGE_ROOT.parents:
            if (parent / "pyproject.toml").is_file() and (parent / "data").is_dir():
                out = parent / "outputs"
                break
    if create:
        out.mkdir(parents=True, exist_ok=True)
    return out
