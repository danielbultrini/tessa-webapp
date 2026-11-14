"""Run a :class:`~tessa.pipeline.Plan`: on Aer (traced cells) or on IBM hardware.

The simulators decode cell by cell with ``halo="traced"``, each cell on the
physical qubits it occupies, with that chip's calibrated noise. Hardware runs the
whole circuit, one job per request; the page polls :func:`ibm_status` until it is
done. Nothing is stored server side: the page sends the request again with the
job id, and the plan is rebuilt from it.
"""

from __future__ import annotations

from functools import cache

from qiskit import transpile

from iqpixl import aer_sampler, decode, decode_from_counts, graph_from_coupling
from iqpixl.backends.base import counts_of
from iqpixl.codec import _measured
from iqpixl.core import get_mode

from . import devices
from .pipeline import build_plan, finish, shots_of


@cache
def _noiseless_sampler(device: str):
    return aer_sampler(devices.graph(device))


def simulate(request: dict) -> dict:
    device = request.get("device", "phoenix")
    graph = devices.graph(device)
    plan = build_plan(request, graph)
    noisy = request.get("noise", "calibrated") != "none"
    sampler = devices.noisy_sampler(device) if noisy else _noiseless_sampler(device)
    shots = shots_of(request)
    symbolic = "symbols" in plan.extra  # decoded from counts, see pipeline.finish
    decoded = []
    for circuit, enc in plan.readouts:
        out = decode(
            circuit, enc, sampler, shots=shots, halo="traced", return_counts=symbolic
        )
        decoded.append(out[1] if symbolic else out)
    out = finish(plan, decoded)
    out["run"] = {
        "kind": "simulator",
        "device": devices.DEVICES[device],
        "noise": "calibrated" if noisy else "none",
        "shots": shots,
        "calibrated_at": devices.snapshot_backend(device).snapshot["calibrated_at"],
    }
    return out


# ----------------------------------------------------------------------
# IBM hardware
# ----------------------------------------------------------------------
def _service(credentials: dict):
    from qiskit_ibm_runtime import QiskitRuntimeService

    token = (credentials.get("token") or "").strip()
    instance = (credentials.get("instance") or "").strip()
    if not token or not instance:
        raise ValueError("paste both an IBM Cloud API key and an instance CRN")
    return QiskitRuntimeService(
        channel="ibm_quantum_platform", token=token, instance=instance
    )


def hardware_circuits(plan) -> list:
    """Every readout of ``plan``, one whole-chip circuit per measurement basis."""
    return [
        _measured(circuit, enc.data_qubits, basis)
        for circuit, enc in plan.readouts
        for basis in get_mode(enc.mode).bases
    ]


def decode_hardware(plan, counts: list[dict]) -> dict:
    """Counts of :func:`hardware_circuits`, in the same order -> :func:`finish`."""
    symbolic = "symbols" in plan.extra  # decoded from counts, see pipeline.finish
    decoded, i = [], 0
    for _, enc in plan.readouts:
        by_basis = {}
        for basis in get_mode(enc.mode).bases:
            by_basis[basis] = counts[i]
            i += 1
        decoded.append(by_basis if symbolic else decode_from_counts(by_basis, enc))
    if i != len(counts):
        raise ValueError(f"expected {i} circuits' counts, got {len(counts)}")
    return finish(plan, decoded)


def ibm_submit(request: dict, credentials: dict) -> dict:
    """Encode on the live chip's graph, run every measurement setting in one job."""
    from qiskit_ibm_runtime import SamplerV2

    device = request.get("device", "phoenix")
    backend = _service(credentials).backend(devices.DEVICES[device])
    graph = graph_from_coupling(backend.coupling_map.get_edges(), backend.num_qubits)
    circuits = hardware_circuits(build_plan(request, graph))
    transpiled = transpile(
        circuits,
        backend,
        initial_layout=list(range(graph.n_qubits)),
        optimization_level=1,
        seed_transpiler=0,
        num_processes=1,
    )
    shots = shots_of(request)
    job = SamplerV2(mode=backend).run(transpiled, shots=shots)
    return {
        "job_id": job.job_id(),
        "backend": backend.name,
        "n_circuits": len(transpiled),
        "shots": shots,
        # The graph the job was encoded on, so decoding never depends on the chip
        # still having every coupler when the job finishes.
        "edges": [list(e) for e in sorted(graph.edges)],
        "n_qubits": graph.n_qubits,
    }


def ibm_status(request: dict, credentials: dict, ticket: dict) -> dict:
    """``{"status": ...}`` while the job runs; the decoded images once it is done."""
    job = _service(credentials).job(ticket["job_id"])
    status = str(job.status())
    if status in ("ERROR", "CANCELLED"):
        return {"status": status, "error": job.error_message()}
    if status != "DONE":
        return {"status": status}

    graph = graph_from_coupling(ticket["edges"], ticket["n_qubits"])
    plan = build_plan(request, graph)
    result = job.result()
    out = decode_hardware(plan, [counts_of(result, i) for i in range(len(result))])
    out["status"] = status
    out["run"] = {
        "kind": "hardware",
        "device": ticket["backend"],
        "job_id": ticket["job_id"],
        "shots": ticket["shots"],
    }
    return out
