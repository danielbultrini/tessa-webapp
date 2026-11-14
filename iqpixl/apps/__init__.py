"""Example applications built on the interwoven encoder.

* :mod:`~iqpixl.apps.midi` -- two-pass MIDI codec (pitch pass + duration pass).
* :mod:`~iqpixl.apps.audio` -- wavetable synthesiser fed a quantum-reconstructed table.
* :mod:`~iqpixl.apps.standard_qpixl` -- serial-QPIXL baseline for comparison.

The submodules are imported lazily so that ``import iqpixl.apps`` does not require the
optional ``mido``/``scipy``/``pillow`` dependencies.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from . import audio, midi, standard_qpixl

__all__ = ["audio", "midi", "standard_qpixl"]


def __getattr__(name: str):
    if name in __all__:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
