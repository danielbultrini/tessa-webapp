"""The encode / decode protocol: values in, circuit out, values back.

::

    circuit, enc = encode(graph, values)          # classical -> quantum
    ...                                           # your own gates here
    values_out   = decode(circuit, enc, sampler)  # quantum -> classical

:func:`encode` returns a bare, measurement-free circuit plus an
:class:`Encoding` -- the auxiliary data (which graph, which mode, what the
values were scaled by) that :func:`decode` needs to read it back. Nothing is
executed until :func:`decode`, and anything may happen to the circuit in
between.

:func:`decode` appends the measurement settings the mode calls for, runs them on
a **sampler object** (see :mod:`iqpixl.backends`) and inverts the encoding.
``per_group=True`` (default) cuts the circuit into one sub-circuit per data
qubit and simulates those; ``per_group=False`` submits it whole, which is how
hardware runs it. The cells commute, so the two agree up to shot noise and
crosstalk. Each cell also feels its *halo* (see :func:`cell_halo`): the noise of
the gates neighbouring cells apply to the shared address qubits. By default that
noise is traced onto the address qubits, so the halo qubits themselves are never
simulated (see :func:`decode`).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit.circuit.library import UnitaryGate

from .backends.base import counts_of, device_of
from .core.decode import decode_cells
from .core.encode import build_interwoven_circuit
from .core.lattice import DataGroup
from .core.mode import DEFAULT_CLAMP, get_mode
from .noise import halo_label

__all__ = [
    "DEFAULT_CLAMP",
    "Encoding",
    "cell_circuit",
    "cell_halo",
    "decode",
    "decode_from_counts",
    "encode",
    "transpile_to_backend",
]


@dataclass(frozen=True)
class Encoding:
    """What :func:`decode` needs to invert an :func:`encode` -- the auxiliary data."""

    graph: object
    mode: str
    #: One clamp per field: how far ``theta`` and ``phi`` stay off the ends of
    #: their ranges. See :mod:`iqpixl.core.mode`.
    clamps: tuple[float, ...]
    #: ``(lo, hi)`` per field: what ``[0, 1]`` on the circuit means in real units.
    ranges: tuple[tuple[float, float], ...]
    #: Shape of the input field(s), and how many of the capacity slots they filled.
    shape: tuple[int, ...]
    n: int

    @property
    def n_fields(self) -> int:
        return get_mode(self.mode).n_fields

    @property
    def data_qubits(self) -> list[int]:
        return [g.data_qubit for g in self.graph.groups]


def _normalise(values, given_range) -> tuple[np.ndarray, tuple[float, float]]:
    flat = np.asarray(values, dtype=float).ravel()
    lo, hi = given_range if given_range is not None else (flat.min(), flat.max())
    lo, hi = float(lo), float(hi)
    if hi <= lo:
        hi = lo + 1.0
    return np.clip((flat - lo) / (hi - lo), 0.0, 1.0), (lo, hi)


def encode(
    graph,
    values,
    values_b=None,
    *,
    mode: str | None = None,
    clamp: float | None = None,
    clamp_b: float | None = None,
    value_range=None,
    value_range_b=None,
    compression: float = 0.0,
    strategy: str = "parallel",
) -> tuple[QuantumCircuit, Encoding]:
    """Encode one or two fields as a circuit plus the :class:`Encoding` to read it.

    Parameters
    ----------
    graph:
        An :class:`~iqpixl.InterwovenGraph` (or :class:`~iqpixl.Lattice`) whose
        qubit indices are the target device's own -- see
        :func:`~iqpixl.graph_from_backend`.
    values, values_b:
        Arrays of any shape with at most ``graph.capacity`` entries; they are
        flattened and zero-padded to capacity. A second field selects a pair
        mode, storing it in each cell's phase at no extra two-qubit gate cost.
    mode:
        ``"single"``, ``"pair"`` or ``"pair_full"`` (see
        :mod:`iqpixl.core.mode`); inferred from whether ``values_b`` is given,
        so it is only worth passing to ask for ``"pair_full"``.
    clamp, clamp_b:
        Per-field clamps, holding ``theta`` and ``phi`` off the ends of their
        angle ranges. ``clamp`` defaults to ``0`` for one field and
        :data:`DEFAULT_CLAMP` for two; ``clamp_b`` defaults to ``0`` on the
        half-circle and to ``clamp`` on the full one. See
        :mod:`iqpixl.core.mode`.
    value_range, value_range_b:
        ``(lo, hi)`` mapped onto the full angle range. Defaults to each field's
        own min/max, so a field round-trips in its original units; pass a fixed
        range when several encodings must share a scale.
    compression, strategy:
        See :func:`~iqpixl.core.encode.build_interwoven_circuit`.
    """
    if mode is None:
        mode = "single" if values_b is None else "pair"
    mode = get_mode(mode)
    clamps = mode.resolve_clamps(clamp, clamp_b)
    if (values_b is None) == mode.is_pair:
        raise ValueError(f"mode {mode.name!r} needs exactly {mode.n_fields} field(s)")

    shape = np.asarray(values).shape
    fields = [(values, value_range)]
    if mode.is_pair:
        if np.asarray(values_b).shape != shape:
            raise ValueError("the two fields must have the same shape")
        fields.append((values_b, value_range_b))

    payload = np.zeros((graph.capacity, len(fields)))
    ranges = []
    for j, (field, rng) in enumerate(fields):
        flat01, rng = _normalise(field, rng)
        if flat01.size > graph.capacity:
            raise ValueError(f"{flat01.size} values exceed capacity {graph.capacity}")
        payload[: flat01.size, j] = flat01
        ranges.append(rng)

    circuit = build_interwoven_circuit(
        graph,
        payload if mode.is_pair else payload[:, 0],
        mode=mode,
        clamps=clamps,
        compression=compression,
        strategy=strategy,
    )
    n = int(np.prod(shape))
    return circuit, Encoding(graph, mode.name, clamps, tuple(ranges), shape, n)


# ----------------------------------------------------------------------
# Cutting the circuit into independent cells
# ----------------------------------------------------------------------
def cell_halo(graph, group: DataGroup, shape: str = "diamond") -> tuple[int, ...]:
    """The neighbouring data qubits a cell is simulated with.

    ``shape="cross"`` keeps the data qubits that share exactly one
    address qubit with ``group``. On a square lattice that is the data qubit
    straight across each address, which gives a 9-qubit cross::

            D
            A
        D A D A D
            A
            D

    ``shape="diamond"`` (default) keeps every data qubit that shares an address qubit with
    ``group``. On a square lattice that adds the four diagonal data qubits,
    which share two address qubits, for 13 qubits::

            D
          D A D
        D A D A D
          D A D
            D

    On a heavy-hex graph no two data qubits share two addresses, so the two
    shapes are the same.
    """
    if shape not in ("cross", "diamond"):
        raise ValueError(f"unknown halo shape {shape!r}; use 'cross' or 'diamond'")
    own = set(group.address_qubits)
    halo: list[int] = []
    for a in group.address_qubits:
        for other in graph.groups:
            d = other.data_qubit
            if d == group.data_qubit or d in halo or a not in other.address_qubits:
                continue
            if shape == "diamond" or len(own.intersection(other.address_qubits)) == 1:
                halo.append(d)
    return tuple(halo)


def cell_circuit(
    circuit: QuantumCircuit, group: DataGroup, halo: tuple[int, ...] = ()
) -> QuantumCircuit:
    """The part of ``circuit`` acting on one data qubit and its address qubits.

    Every gate whose qubits all lie inside the cell is kept, in order, remapped
    onto a compact register with the data qubit at index 0, its address qubits
    next and the ``halo`` data qubits (see :func:`cell_halo`) last. Gates
    reaching outside are dropped -- they belong to neighbouring cells, which
    touch the shared address qubits only as controls. A halo keeps the
    neighbours' gates on the shared address qubits, so their noise reaches the
    cell, and drops only the neighbours' gates on address qubits further out.
    """
    qubits = [group.data_qubit, *group.address_qubits, *halo]
    position = {q: i for i, q in enumerate(qubits)}
    cell = QuantumCircuit(len(qubits))
    for instruction in circuit.data:
        idx = [circuit.find_bit(q).index for q in instruction.qubits]
        if all(q in position for q in idx):
            cell.append(instruction.operation, [position[q] for q in idx])
    return cell


def _cell_group(group: DataGroup) -> DataGroup:
    """``group`` re-indexed onto the compact register of :func:`cell_circuit`."""
    return DataGroup(
        site=group.site,
        data_qubit=0,
        address_qubits=tuple(range(1, group.degree + 1)),
        address_sites=group.address_sites,
    )


def _measured(circuit: QuantumCircuit, data_qubits, basis: str) -> QuantumCircuit:
    """``circuit`` with a basis change on the data qubits, then ``measure_all``.

    Address qubits are never rotated: they carry no payload, and turning them
    would break the per-cell independence.
    """
    out = circuit.copy()
    if basis == "x":
        out.h(data_qubits)
    elif basis == "y":
        out.sdg(data_qubits)
        out.h(data_qubits)
    out.measure_all()
    return out


def _trace_halo(circuit: QuantumCircuit, halo) -> QuantumCircuit:
    """``circuit`` with its ``halo`` qubits traced out into noise on the addresses.

    A halo data qubit meets the cell only through ``cz`` gates on shared address
    qubits. Those gates are diagonal on the address, and an address qubit gets no
    other gate after its first ``H``, so its populations -- all that is measured
    -- never depend on the halo qubit. Only the noise of those gates reaches the
    cell. Each such ``cz`` therefore becomes an identity on the address that
    carries that noise (labelled :func:`~iqpixl.noise.halo_label`), and every
    other operation on a halo qubit is dropped.
    """
    halo = set(halo)
    out = circuit.copy_empty_like()
    for instruction in circuit.data:
        idx = [circuit.find_bit(q).index for q in instruction.qubits]
        touched = [q for q in idx if q in halo]
        if not touched:
            out.append(instruction.operation, instruction.qubits, instruction.clbits)
        elif instruction.operation.name == "cz" and len(touched) == 1:
            (address,) = (q for q in idx if q not in halo)
            out.append(UnitaryGate(np.eye(2), label=halo_label(touched[0])), [address])
    return out


def _check_traceable(sampler) -> None:
    """Fail unless ``sampler``'s noise model can stand in for a traced halo."""
    if not hasattr(sampler, "noise_model"):
        raise ValueError(
            "per_group=True with halo='traced' simulates each cell, so it needs a "
            "simulator sampler from iqpixl.backends (aer_sampler, noisy_sampler, "
            "calibrated_sampler, fake_sampler); on hardware, run the whole circuit "
            "with per_group=False"
        )
    nm = sampler.noise_model
    prefix = halo_label(0).removesuffix("0")
    if nm is not None and not any(
        name.startswith(prefix) for name in nm.noise_instructions
    ):
        raise ValueError(
            "halo='traced' needs the noise model to carry halo_label errors; "
            "build it with build_average_noise_model or build_local_noise_model"
        )


# ----------------------------------------------------------------------
# Running and decoding
# ----------------------------------------------------------------------
def _run(sampler, jobs, shots, optimization_level) -> list[dict]:
    """Transpile every ``(circuit, layout, traced)`` and run them in one submission.

    ``traced`` lists the qubits :func:`_trace_halo` removes after transpiling.
    One call rather than one per circuit: a single job on hardware, and on a
    simulator the noise model is set up once instead of once per cell.
    """
    device = device_of(sampler)
    transpiled = []
    for circuit, layout, traced in jobs:
        tqc = transpile(
            circuit,
            device,
            initial_layout=layout,
            optimization_level=optimization_level,
            seed_transpiler=0,
        )
        transpiled.append(_trace_halo(tqc, traced) if traced else tqc)
    result = sampler.run(transpiled, shots=shots).result()
    return [counts_of(result, i) for i in range(len(transpiled))]


def decode(
    circuit: QuantumCircuit,
    encoding: Encoding,
    sampler,
    *,
    per_group: bool = True,
    halo: str | None = "traced",
    shots: int = 4096,
    optimization_level: int = 1,
    return_counts: bool = False,
):
    """Measure ``circuit`` on ``sampler`` and recover the encoded field(s).

    ``circuit`` must be measurement-free; the settings the mode needs are
    appended here. Returns the reconstruction in the input's own shape and units
    -- a single array, or one per field for a pair mode. With ``return_counts``
    the raw counts come along: ``{basis: counts}`` for a joint run,
    ``{site: {basis: counts}}`` per group.

    ``per_group=True`` is for simulation; on hardware, pass ``per_group=False``.
    ``halo`` (per group only) sets how each cell feels its neighbours.
    ``"traced"`` (default) gives the ``"diamond"`` result at the bare cell's
    cost: the halo qubits are traced out, and only the noise their gates leave
    on the shared address qubits is kept (see :func:`_trace_halo`). It needs a
    simulator sampler from :mod:`iqpixl.backends`, and it is exact for the
    circuit :func:`encode` builds and those samplers' noise models. A gate you
    add to an address qubit that is not diagonal makes it approximate.
    ``"diamond"`` and ``"cross"`` simulate the halo qubits in full (see
    :func:`cell_halo`), and ``None`` runs the bare cell.
    """
    if circuit.num_clbits:
        raise ValueError(
            "decode measures the circuit itself; pass the unmeasured circuit"
        )
    graph = encoding.graph
    mode = get_mode(encoding.mode)
    device = device_of(sampler)
    n_device = getattr(device, "num_qubits", None)
    if n_device is not None and graph.n_qubits > n_device:
        raise ValueError(
            f"graph uses {graph.n_qubits} qubits but the device has {n_device}; "
            "build the graph from this device with graph_from_backend"
        )

    if per_group:
        if halo == "traced":
            _check_traceable(sampler)
        keys, jobs = [], []
        n_cell = {}
        for g in graph.groups:
            shape = "diamond" if halo == "traced" else halo
            extra = cell_halo(graph, g, shape) if shape else ()
            traced = extra if halo == "traced" else ()
            cell = cell_circuit(circuit, g, extra)
            n_cell[g.site] = cell.num_qubits
            layout = [g.data_qubit, *g.address_qubits, *extra]
            for basis in mode.bases:
                keys.append((g.site, basis))
                jobs.append((_measured(cell, [0], basis), layout, traced))
        every = _run(sampler, jobs, shots, optimization_level)

        counts = {g.site: {} for g in graph.groups}
        for (site, basis), c in zip(keys, every, strict=True):
            counts[site][basis] = c
        packed = {}
        for g in graph.groups:
            packed.update(
                decode_cells(
                    counts[g.site],
                    [_cell_group(g)],
                    n_cell[g.site],
                    mode,
                    encoding.clamps,
                )
            )
        values = _restore(graph.unpack(packed), encoding)
    else:
        layout = list(range(graph.n_qubits))
        jobs = [
            (_measured(circuit, encoding.data_qubits, basis), layout, ())
            for basis in mode.bases
        ]
        counts = dict(
            zip(mode.bases, _run(sampler, jobs, shots, optimization_level), strict=True)
        )
        values = decode_from_counts(counts, encoding)
    return (values, counts) if return_counts else values


def decode_from_counts(counts_by_basis: dict[str, dict], encoding: Encoding):
    """Invert an encoding from counts you measured yourself.

    ``counts_by_basis`` are the counts of the whole encoded circuit, one entry
    per basis the mode asks for.
    """
    graph = encoding.graph
    packed = decode_cells(
        counts_by_basis,
        graph.groups,
        graph.n_qubits,
        encoding.mode,
        encoding.clamps,
    )
    return _restore(graph.unpack(packed), encoding)


def _restore(payload01: np.ndarray, encoding: Encoding):
    """``[0, 1]`` capacity vector(s) -> the input's shape and units."""
    payload01 = payload01.reshape(len(payload01), -1)  # single mode -> one column
    out = []
    for j, (lo, hi) in enumerate(encoding.ranges):
        field = lo + (hi - lo) * payload01[: encoding.n, j]
        out.append(field.reshape(encoding.shape))
    return out[0] if len(out) == 1 else tuple(out)


def transpile_to_backend(
    circuit: QuantumCircuit, sampler, optimization_level: int = 1
) -> tuple[QuantumCircuit, dict]:
    """Transpile a full-graph circuit onto a sampler's device with the identity layout.

    Returns ``(transpiled, report)``; ``report`` holds the depths, the SWAP and
    two-qubit gate counts and the device size.
    """
    device = device_of(sampler)
    tqc = transpile(
        circuit,
        device,
        initial_layout=list(range(circuit.num_qubits)),
        optimization_level=optimization_level,
        seed_transpiler=0,
    )
    ops = dict(tqc.count_ops())
    return tqc, {
        "logical_depth": circuit.depth(),
        "transpiled_depth": tqc.depth(),
        "swaps": ops.get("swap", 0),
        "two_qubit_gates": sum(ops.get(g, 0) for g in ("cx", "cz", "ecr")),
        "n_physical_qubits": device.num_qubits,
    }
