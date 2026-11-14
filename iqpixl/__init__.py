"""Interwoven QPIXL -- an encode / decode protocol for real numbers on a QPU.

The circuit is the interchange format::

    from iqpixl import graph_from_backend, encode, decode
    from iqpixl.backends import fake_sampler

    sampler = fake_sampler("fez")                  # offline, FakeFez calibration
    graph   = graph_from_backend(sampler.backend())  # 156 qubits -> capacity 448
    circuit, enc = encode(graph, values)
    ...                                            # the circuit is yours in between
    values_out = decode(circuit, enc, sampler, shots=4096)

Values are written onto qubits laid directly on a device's coupling graph. The
graph is 2-coloured into **data** qubits, which hold the values, and **address**
qubits, which form shared control registers. Each data qubit carries a uniformly
controlled ``RY`` over its neighbouring address qubits and so stores
``2**(#neighbours)`` numbers -- optionally twice that, with a second value in
each cell's phase (``mode="pair"``). One measurement setting reads every cell.

**Why "interwoven".** Each address qubit is shared by several data qubits, so
the blocks overlap instead of tiling -- and the overlap is the point: capacity
``sum 2**degree`` can exceed the qubit count only because neighbouring
multiplexers share their controls. Sharing controls also means the cells
commute, which is what lets :func:`decode` read (or simulate) them one at a
time. The multiplexers are interleaved gate by gate in the schedule, which
collapses the circuit depth.

All this needs is a *bipartite* coupling graph, so it maps SWAP-free onto IBM's
square lattice (``ibm_miami``, capacity 792) and heavy-hex chips (``ibm_fez`` /
``ibm_marrakesh`` / ``ibm_kingston``, capacity 448).

Prior art: this generalises **QCrank** / parallel QPIXL (and compressed FRQI);
the multiplexer compiler is the standard scaled-Walsh-Hadamard + Gray-code
construction.

Sub-modules: :mod:`iqpixl.codec` (the protocol), :mod:`iqpixl.core`
(geometry/modes/circuit/decoder), :mod:`iqpixl.graph`, :mod:`iqpixl.backends`
(samplers), :mod:`iqpixl.noise`, :mod:`iqpixl.transforms` and
:mod:`iqpixl.postprocess` (classical conditioning), :mod:`iqpixl.apps`. Nothing
is provider-specific; IBM lives entirely in :mod:`iqpixl.backends.ibm`.
"""

from __future__ import annotations

from .backends import (
    DeviceSampler,
    aer_sampler,
    calibrated_sampler,
    fake_sampler,
    ibm_sampler,
    noisy_sampler,
)
from .codec import (
    Encoding,
    cell_circuit,
    cell_halo,
    decode,
    decode_from_counts,
    encode,
    transpile_to_backend,
)
from .core import (
    DEFAULT_CLAMP,
    MODES,
    DataGroup,
    Lattice,
    Mode,
    build_interwoven_circuit,
    color_data_groups,
    decode_cells,
    encoding_coupling_map,
    encoding_edges,
    flat_to_image,
    is_data_site,
    lattice_capacity,
    reconstruction_error_image,
    rectangle_shape,
    rectangular_layout,
)
from .data import data_dir, data_path, outputs_dir
from .graph import (
    InterwovenGraph,
    graph_capacity,
    graph_from_backend,
    graph_from_coupling,
    square_lattice_edges,
)
from .noise import (
    NATIVE_BASIS,
    backend_rates,
    build_average_noise_model,
    build_local_noise_model,
)
from .postprocess import (
    abs_max,
    match_mean_std,
    match_min_avg_max,
    match_min_max,
    match_percentile_range,
    min_avg_max,
    rescale_to_abs_max,
)
from .symbols import (
    SYMBOL_SCHEMES,
    SymbolEncoding,
    constellation,
    decode_symbols,
    encode_symbols,
    probe_qubits,
    slot_order,
    symbols_from_counts,
)
from .transforms import (
    DYNAMIC_RANGE_METHODS,
    WARP_NAMES,
    FieldPrep,
    Warp,
    apply_dynamic_range,
    build_codebook,
    get_warp,
    prepare_field,
    quantize,
    restore_field,
)

__version__ = "0.3.0"

__all__ = [
    "DEFAULT_CLAMP",
    "DYNAMIC_RANGE_METHODS",
    "MODES",
    "NATIVE_BASIS",
    "SYMBOL_SCHEMES",
    "WARP_NAMES",
    "DataGroup",
    "DeviceSampler",
    "Encoding",
    "FieldPrep",
    "InterwovenGraph",
    "Lattice",
    "Mode",
    "SymbolEncoding",
    "Warp",
    "__version__",
    "abs_max",
    "aer_sampler",
    "apply_dynamic_range",
    "backend_rates",
    "build_average_noise_model",
    "build_codebook",
    "build_interwoven_circuit",
    "build_local_noise_model",
    "calibrated_sampler",
    "cell_circuit",
    "cell_halo",
    "color_data_groups",
    "constellation",
    "data_dir",
    "data_path",
    "decode",
    "decode_cells",
    "decode_from_counts",
    "decode_symbols",
    "encode",
    "encode_symbols",
    "encoding_coupling_map",
    "encoding_edges",
    "fake_sampler",
    "flat_to_image",
    "get_warp",
    "graph_capacity",
    "graph_from_backend",
    "graph_from_coupling",
    "ibm_sampler",
    "is_data_site",
    "lattice_capacity",
    "match_mean_std",
    "match_min_avg_max",
    "match_min_max",
    "match_percentile_range",
    "min_avg_max",
    "noisy_sampler",
    "outputs_dir",
    "prepare_field",
    "probe_qubits",
    "quantize",
    "reconstruction_error_image",
    "rectangle_shape",
    "rectangular_layout",
    "rescale_to_abs_max",
    "restore_field",
    "slot_order",
    "square_lattice_edges",
    "symbols_from_counts",
    "transpile_to_backend",
]
