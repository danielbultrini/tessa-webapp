"""Standard (serial) QPIXL -- the baseline the lattice method is compared against.

Standard QPIXL/FRQI encodes an image with a *single* data qubit controlled by one
global ``k``-bit address register (``2**k`` pixels), i.e. one big uniformly controlled
``RY``.  That is exactly a interwoven graph with one data qubit joined to ``k`` address
qubits, so we reuse the existing encoder/decoders (with the same ``[0,1] -> [0,pi]``
angle convention) via :meth:`InterwovenGraph.from_groups`.

Compared with the interwoven lattice encoding, standard QPIXL uses very few qubits
(``k+1``) but a deep multiplexer whose data qubit must reach *every* address qubit --
so on a heavy-hex device the transpiler inserts many SWAPs and the depth explodes.
This module builds the circuit, a device-derived noise model for a small noisy
simulation, and a hardware-submission helper that tracks the qubits used and saves the
Qiskit Runtime result object.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pickle
from collections import deque

import numpy as np

from ..codec import decode_from_counts, encode
from ..graph import InterwovenGraph
from ..noise import NATIVE_BASIS, backend_rates

__all__ = [
    "NATIVE_BASIS",
    "backend_rates",
    "connected_region",
    "decode_image",
    "encode_image",
    "generic_noise_model",
    "load_image",
    "n_address_qubits",
    "rank_qubits",
    "run_and_save",
    "standard_qpixl_graph",
]


# ----------------------------------------------------------------------
# Image + standard-QPIXL graph
# ----------------------------------------------------------------------
def load_image(path) -> np.ndarray:
    """Load a grayscale image as a 2-D float array in ``[0, 1]``."""
    from PIL import Image

    return np.asarray(Image.open(str(path)).convert("L"), dtype=float) / 255.0


def n_address_qubits(n_pixels: int) -> int:
    """Smallest ``k`` with ``2**k >= n_pixels``."""
    return int(np.ceil(np.log2(max(n_pixels, 1))))


def standard_qpixl_graph(n_addr: int) -> InterwovenGraph:
    """Standard QPIXL as a interwoven graph: 1 data qubit + ``n_addr`` address qubits."""
    return InterwovenGraph.from_groups(n_addr + 1, [(0, list(range(1, n_addr + 1)))])


def encode_image(graph: InterwovenGraph, img: np.ndarray, measure: bool = True):
    """Encode ``img`` (values in ``[0, 1]``) as the QPIXL circuit; ``(circuit, encoding)``."""
    circuit, encoding = encode(graph, img, value_range=(0.0, 1.0))
    if measure:
        circuit.measure_all()
    return circuit, encoding


def decode_image(counts, encoding) -> np.ndarray:
    """Decode Z-basis counts back into the image."""
    return decode_from_counts({"z": counts}, encoding)


# ----------------------------------------------------------------------
# Device-derived noise model over an arbitrary coupling
# ----------------------------------------------------------------------
def generic_noise_model(n_qubits: int, edges, rates: dict):
    """Noise model on ``n_qubits`` with ``cz`` errors on every ``edges`` pair.

    Mirrors the lattice noise model but over an arbitrary coupling map, so it also
    covers the CZ gates that routing/SWAPs introduce.
    """
    from qiskit_aer.noise import (
        NoiseModel,
        depolarizing_error,
        thermal_relaxation_error,
    )

    nm = NoiseModel(basis_gates=NATIVE_BASIS)
    t1 = rates["t1"]
    t2 = min(rates["t2"], 2.0 * t1) if t1 else rates["t2"]
    sx_d, cz_d = rates["sx_duration"], rates["cz_duration"]
    ro = float(np.clip(rates["readout_error"], 0.0, 0.5))
    for q in range(n_qubits):
        e1 = depolarizing_error(rates["sx_error"], 1).compose(
            thermal_relaxation_error(t1, t2, sx_d)
        )
        nm.add_quantum_error(e1, ["sx", "x"], [q])
        nm.add_readout_error([[1 - ro, ro], [ro, 1 - ro]], [q])
    relax2 = thermal_relaxation_error(t1, t2, cz_d).tensor(
        thermal_relaxation_error(t1, t2, cz_d)
    )
    for a, b in edges:
        e2 = depolarizing_error(rates["cz_error"], 2).compose(relax2)
        nm.add_quantum_error(e2, "cz", [a, b])
        nm.add_quantum_error(e2, "cz", [b, a])
    return nm


def connected_region(backend, size: int, start: int | None = None):
    """BFS a connected set of ``size`` physical qubits; return ``(edges01, phys)``.

    ``edges01`` are the induced coupling edges relabelled to ``0..size-1``;
    ``phys[i]`` is the original physical index of relabelled qubit ``i``.
    """
    und = set()
    for a, b in backend.coupling_map.get_edges():
        und.add((a, b) if a < b else (b, a))
    adj = {q: set() for q in range(backend.num_qubits)}
    for a, b in und:
        adj[a].add(b)
        adj[b].add(a)
    if start is None:
        start = max(range(backend.num_qubits), key=lambda q: len(adj[q]))
    chosen, seen, queue = [], {start}, deque([start])
    while queue and len(chosen) < size:
        u = queue.popleft()
        chosen.append(u)
        for v in sorted(adj[u]):
            if v not in seen:
                seen.add(v)
                queue.append(v)
    chosen = chosen[:size]
    cset = set(chosen)
    remap = {p: i for i, p in enumerate(chosen)}
    edges01 = [(remap[a], remap[b]) for a, b in und if a in cset and b in cset]
    return edges01, chosen


# ----------------------------------------------------------------------
# Best-qubit ranking (for reporting / transparency)
# ----------------------------------------------------------------------
def _safe(tgt, name, key, attr, default):
    try:
        props = tgt[name].get(key)
        val = getattr(props, attr, None)
        return float(val) if val is not None and not np.isnan(val) else default
    except Exception:
        return default


def rank_qubits(backend, k: int | None = None):
    """Rank physical qubits by (readout + 1q gate) error; return best-``k`` first."""
    tgt = backend.target
    scores = []
    for q in range(backend.num_qubits):
        ro = _safe(tgt, "measure", (q,), "error", 1.0)
        sx = _safe(tgt, "sx", (q,), "error", 1e-3)
        scores.append((q, ro + sx))
    scores.sort(key=lambda t: t[1])
    order = [q for q, _ in scores]
    return order if k is None else order[:k]


def _qubit_prop(tgt, q, attr):
    try:
        v = getattr(tgt.qubit_properties[q], attr, None)
        return float(v) if v is not None else None
    except Exception:
        return None


# ----------------------------------------------------------------------
# Hardware run + full bookkeeping
# ----------------------------------------------------------------------
def run_and_save(
    graph: InterwovenGraph,
    img: np.ndarray,
    sampler,
    shots: int = 8192,
    outdir: str = "qcat_qpixl_run",
    optimization_level: int = 3,
):
    """Transpile standard QPIXL onto the device's best qubits, run, and save.

    ``sampler`` is any sampler object (see :mod:`iqpixl.backends`) -- typically
    ``ibm_sampler(name="ibm_fez")`` for the real baseline run, but a calibrated
    local sampler works identically and writes the same artefacts.

    Saves into ``outdir``: the counts (``counts.json``), the ISA circuit
    (``isa_circuit.qpy``), the raw ``result`` object (``result.pkl``), the decoded
    reconstruction (``reconstruction.npy``), and a JSON ``manifest`` recording the
    device, job id, the exact physical qubits used, per-qubit calibration and gate
    counts.  Returns ``(result, counts, reconstruction, manifest)``.
    """
    from qiskit import qpy
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    from ..backends.base import counts_of, device_of

    backend = device_of(sampler)
    backend_name = getattr(backend, "name", None) or type(backend).__name__
    qc, encoding = encode_image(graph, img, measure=True)

    # Error-aware transpile picks the best qubits automatically at opt level 3.
    pm = generate_preset_pass_manager(
        backend=backend, optimization_level=optimization_level
    )
    isa = pm.run(qc)
    used = list(isa.layout.final_index_layout())  # physical qubits carrying the data

    job = sampler.run([isa], shots=shots)
    result = job.result()
    counts = counts_of(result)

    os.makedirs(outdir, exist_ok=True)
    # Counts + circuit are saved first, as robust dependency-light formats.
    with open(os.path.join(outdir, "counts.json"), "w") as f:
        json.dump(counts, f)
    with open(os.path.join(outdir, "isa_circuit.qpy"), "wb") as f:
        qpy.dump(isa, f)
    # The raw Runtime result object (pickle); guarded so a pickling issue never
    # loses the already-saved counts.
    try:
        with open(os.path.join(outdir, "result.pkl"), "wb") as f:
            pickle.dump(result, f)
    except Exception as exc:  # pragma: no cover
        print(f"warning: could not pickle result object ({exc}); counts.json is saved")

    recon = decode_image(counts, encoding)
    np.save(os.path.join(outdir, "reconstruction.npy"), recon)
    np.save(os.path.join(outdir, "input_image.npy"), np.asarray(img))

    ops = dict(isa.count_ops())
    tgt = backend.target
    job_id_fn = getattr(job, "job_id", None)
    manifest = {
        "backend": backend_name,
        "job_id": job_id_fn() if callable(job_id_fn) else None,
        "timestamp_utc": _dt.datetime.now(_dt.UTC).isoformat(),
        "shots": shots,
        "optimization_level": optimization_level,
        "n_pixels": encoding.n,
        "image_shape": list(encoding.shape),
        "n_program_qubits": graph.n_qubits,
        "physical_qubits_used": [int(q) for q in used],
        "isa_depth": int(isa.depth()),
        "isa_2q_gates": int(ops.get("cz", 0) + ops.get("ecr", 0) + ops.get("cx", 0)),
        "isa_ops": {k: int(v) for k, v in ops.items()},
        "calibration_per_qubit": {
            int(q): {
                "readout_error": _safe(tgt, "measure", (q,), "error", None),
                "sx_error": _safe(tgt, "sx", (q,), "error", None),
                "t1_s": _qubit_prop(tgt, q, "t1"),
                "t2_s": _qubit_prop(tgt, q, "t2"),
            }
            for q in used
        },
    }
    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    return result, counts, recon, manifest
