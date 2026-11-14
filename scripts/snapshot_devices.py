"""Save the calibration of ibm_fez and ibm_phoenix as JSON for the simulators.

Run once, locally, with an IBM account saved (``QiskitRuntimeService.save_account``)
or ``QISKIT_IBM_TOKEN`` set:

    uv run python webapp/scripts/snapshot_devices.py

The deployed app reads only these files, so the simulators never contact IBM.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from qiskit_ibm_runtime import QiskitRuntimeService

OUT = Path(__file__).resolve().parents[1] / "tessa" / "devices"
DEVICES = ("ibm_fez", "ibm_phoenix")


def coordinates(n_qubits: int, edges) -> list[tuple[float, float]]:
    """Chip-drawing ``(x, y)`` per qubit; every coupler has length 1."""
    if n_qubits == 120:  # Nighthawk: 12 rows of 10, row-major
        coords = [(q % 10, q // 10) for q in range(n_qubits)]
    elif n_qubits == 156:  # Heron r2: rows of 16, joined by 4 bridge qubits
        coords = [None] * n_qubits
        for q in range(n_qubits):
            if q % 20 < 16:
                coords[q] = (q % 20, 2 * (q // 20))
        for q in range(n_qubits):
            if coords[q] is None:
                above = min(p for a, b in edges for p in (a, b) if q in (a, b) and p != q)
                coords[q] = (coords[above][0], coords[above][1] + 1)
    else:
        raise ValueError(f"no chip layout for {n_qubits} qubits")
    for a, b in edges:
        (xa, ya), (xb, yb) = coords[a], coords[b]
        assert math.isclose(math.hypot(xa - xb, ya - yb), 1.0), (a, b)
    return coords


def _value(props, attr):
    x = getattr(props, attr, None) if props is not None else None
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def snapshot(backend) -> dict:
    tgt = backend.target
    edges = sorted({tuple(sorted(e)) for e in tgt["cz"]})
    qubits = []
    for q in range(backend.num_qubits):
        qp = tgt.qubit_properties[q]
        qubits.append(
            {
                "t1": _value(qp, "t1"),
                "t2": _value(qp, "t2"),
                "sx_error": _value(tgt["sx"].get((q,)), "error"),
                "sx_duration": _value(tgt["sx"].get((q,)), "duration"),
                "readout_error": _value(tgt["measure"].get((q,)), "error"),
            }
        )
    cz = []
    for a, b in edges:
        props = tgt["cz"].get((a, b)) or tgt["cz"].get((b, a))
        cz.append([_value(props, "error"), _value(props, "duration")])
    return {
        "name": backend.name,
        "num_qubits": backend.num_qubits,
        "calibrated_at": str(backend.properties().last_update_date),
        "edges": [list(e) for e in edges],
        "coords": [list(c) for c in coordinates(backend.num_qubits, edges)],
        "qubits": qubits,
        "cz": cz,
    }


if __name__ == "__main__":
    service = QiskitRuntimeService()
    OUT.mkdir(parents=True, exist_ok=True)
    for name in DEVICES:
        snap = snapshot(service.backend(name))
        (OUT / f"{name}.json").write_text(json.dumps(snap))
        print(
            f"{name}: {snap['num_qubits']} qubits, {len(snap['edges'])} couplers, "
            f"calibrated {snap['calibrated_at']}"
        )
