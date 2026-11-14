"""The two chips the app offers, rebuilt offline from their calibration snapshots.

``devices/<name>.json`` is written by ``scripts/snapshot_devices.py``. From it we
build a minimal ``BackendV2`` whose ``Target`` carries the measured gate errors,
durations, readout errors and ``T1``/``T2`` -- everything the iqpixl noise models
read -- so the simulators never contact IBM.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import numpy as np
from qiskit.circuit import Measure, Parameter
from qiskit.circuit.library import CZGate, IGate, RZGate, SXGate, XGate
from qiskit.providers import BackendV2, Options
from qiskit.transpiler import InstructionProperties, QubitProperties, Target

from iqpixl import calibrated_sampler, graph_from_backend

HERE = Path(__file__).resolve().parent

#: Short name in the API -> IBM backend name.
DEVICES = {"fez": "ibm_fez", "phoenix": "ibm_phoenix"}


class SnapshotBackend(BackendV2):
    """A device that exists only as a calibration snapshot: transpile, never run."""

    def __init__(self, snap: dict) -> None:
        super().__init__(name=snap["name"])
        self.snapshot = snap
        self._target = _target(snap)

    @property
    def target(self) -> Target:
        return self._target

    @property
    def max_circuits(self):
        return None

    @classmethod
    def _default_options(cls) -> Options:
        return Options()

    def run(self, run_input, **options):
        raise NotImplementedError("a snapshot backend only transpiles; run on Aer")


def _target(snap: dict) -> Target:
    def num(x, default):
        return default if x is None else float(x)

    qubits = snap["qubits"]
    target = Target(
        num_qubits=snap["num_qubits"],
        qubit_properties=[
            QubitProperties(t1=num(q["t1"], 1e-4), t2=num(q["t2"], 1e-4)) for q in qubits
        ],
    )
    one = {
        (i,): InstructionProperties(
            error=num(q["sx_error"], 3e-4), duration=num(q["sx_duration"], 3.2e-8)
        )
        for i, q in enumerate(qubits)
    }
    target.add_instruction(SXGate(), one)
    target.add_instruction(XGate(), dict(one))
    target.add_instruction(
        RZGate(Parameter("theta")),
        {
            (i,): InstructionProperties(error=0.0, duration=0.0)
            for i in range(len(qubits))
        },
    )
    target.add_instruction(
        IGate(), {(i,): InstructionProperties(error=0.0) for i in range(len(qubits))}
    )
    target.add_instruction(
        Measure(),
        {
            (i,): InstructionProperties(error=num(q["readout_error"], 1e-2))
            for i, q in enumerate(qubits)
        },
    )
    cz = {}
    for (a, b), (err, dur) in zip(snap["edges"], snap["cz"], strict=True):
        props = {"error": num(err, 8e-3), "duration": num(dur, 7e-8)}
        cz[(a, b)] = InstructionProperties(**props)
        cz[(b, a)] = InstructionProperties(**props)
    target.add_instruction(CZGate(), cz)
    return target


def _check(device: str) -> str:
    if device not in DEVICES:
        raise ValueError(f"unknown device {device!r}; choose from {sorted(DEVICES)}")
    return DEVICES[device]


@cache
def snapshot_backend(device: str) -> SnapshotBackend:
    name = _check(device)
    return SnapshotBackend(json.loads((HERE / "devices" / f"{name}.json").read_text()))


@cache
def graph(device: str):
    return graph_from_backend(snapshot_backend(device))


@cache
def noisy_sampler(device: str):
    """``device``'s own per-qubit calibration, run on Aer. Built once per instance."""
    return calibrated_sampler(snapshot_backend(device), seed=None)


def info(device: str) -> dict:
    """What the page needs to draw the chip and say what fits."""
    backend = snapshot_backend(device)
    snap = backend.snapshot
    g = graph(device)
    return {
        "device": device,
        "name": snap["name"],
        "calibrated_at": snap["calibrated_at"],
        "n_qubits": snap["num_qubits"],
        "coords": snap["coords"],
        "edges": snap["edges"],
        "capacity": g.capacity,
        "groups": [
            {
                "data": g_.data_qubit,
                "addresses": list(g_.address_qubits),
                "n_values": g_.n_values,
            }
            for g_ in g.groups
        ],
        "mean_cz_error": float(np.nanmean([e for e, _ in snap["cz"] if e is not None])),
        "mean_readout_error": float(
            np.nanmean([q["readout_error"] for q in snap["qubits"] if q["readout_error"]])
        ),
    }
