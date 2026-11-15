"""The web app's pipeline: every mode round trips, sprites keep their shape, and
the hardware path decodes whole-circuit counts (against a fake IBM service)."""

from __future__ import annotations

import base64
import filecmp
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
from tessa import devices, mapping, pipeline, runners, split

WEBAPP = Path(__file__).resolve().parents[1]


def sprite(w=7, h=6, seed=0, colours=None):
    """A random RGBA sprite whose four corners are transparent."""
    rng = np.random.default_rng(seed)
    if colours is None:
        rgb = rng.integers(0, 256, size=(h, w, 3))
    else:
        rgb = np.asarray(colours)[rng.integers(0, len(colours), size=(h, w))]
    alpha = np.full((h, w, 1), 255)
    for y, x in [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]:
        alpha[y, x] = 0
    rgba = np.concatenate([rgb, alpha], axis=2).astype(np.uint8)
    return {"width": w, "height": h, "rgba": base64.b64encode(rgba.tobytes()).decode()}


def rgba_of(b64, w, h):
    return np.frombuffer(base64.b64decode(b64), np.uint8).reshape(h, w, 4)


def test_the_vendored_library_matches_src():
    """Run scripts/vendor_iqpixl.py after changing src/iqpixl."""
    src = WEBAPP.parent / "src" / "iqpixl"
    for path in src.rglob("*.py"):
        copy = WEBAPP / "iqpixl" / path.relative_to(src)
        assert copy.is_file() and filecmp.cmp(path, copy, shallow=False), copy


@pytest.mark.parametrize(("device", "capacity"), [("fez", 448), ("phoenix", 792)])
def test_the_chips_load_offline(device, capacity):
    info = devices.info(device)
    assert info["capacity"] == capacity
    assert len(info["coords"]) == info["n_qubits"]


CASES = [
    ("gray", "z", 4),
    ("gray", "x", 4),
    ("gray", "zx", 6),
    ("gray", "zxy", 4),
    ("huevalue", "zx", None),
    ("huevalue", "zxy", None),
]


@pytest.mark.parametrize(("rep", "basis", "tolerance"), CASES)
def test_every_mode_round_trips_without_noise(rep, basis, tolerance):
    image = sprite()
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": rep,
            "basis": basis,
            "images": [image],
            "shots": 100_000,
            "noise": "none",
        }
    )
    m = out["metrics"]
    if rep == "gray":
        assert m["mean_abs_error_255"] < tolerance
    else:
        assert m["mean_abs_error_value_255"] < 4 and m["mean_abs_error_hue_degrees"] < 4
    got = rgba_of(out["image"], 7, 6)
    want = rgba_of(image["rgba"], 7, 6)
    assert (got[..., 3] == want[..., 3]).all()  # the sprite keeps its shape
    groups = np.array(out["pixel_group"]).reshape(6, 7)
    assert (groups[want[..., 3] == 0] == -1).all()
    assert (groups[want[..., 3] == 255] >= 0).all()


@pytest.mark.parametrize("basis", ["z", "x", "zx", "zxy", "sphere1", "sphere2"])
def test_a_small_palette_comes_back_exactly(basis):
    colours = [(10, 20, 30), (200, 40, 40), (40, 200, 40), (250, 250, 250)]
    out = runners.simulate(
        {
            "device": "fez",
            "representation": "palette",
            "basis": basis,
            "images": [sprite(colours=colours)],
            "shots": 20_000,
            "noise": "none",
        }
    )
    assert out["metrics"]["palette_size"] == 4
    assert out["metrics"]["pixels_exact"] == 1.0


def test_the_transition_ends_on_each_image():
    a, b = sprite(seed=1), sprite(seed=2)
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "transition",
            "images": [a, b],
            "alphas": 3,
            "shots": 100_000,
            "noise": "none",
        }
    )
    assert out["alphas_degrees"] == [0.0, 45.0, 90.0]
    assert out["metrics"]["mean_abs_error_A_at_0_255"] < 6
    assert out["metrics"]["mean_abs_error_B_at_90_255"] < 8


def test_two_pixels_per_slot_doubles_what_fits():
    big = sprite(w=30, h=30)  # 896 opaque pixels: more than 792 slots
    request = {"device": "phoenix", "representation": "gray", "images": [big]}
    with pytest.raises(ValueError, match="792"):
        pipeline.build_plan({**request, "basis": "z"}, devices.graph("phoenix"))
    plan = pipeline.build_plan({**request, "basis": "zx"}, devices.graph("phoenix"))
    assert plan.slot.max() + 1 == 448


def test_hsv_round_trips():
    rgb = np.random.default_rng(3).random((500, 3))
    assert np.allclose(pipeline.hsv_to_rgb(pipeline.rgb_to_hsv(rgb)), rgb)


def test_the_palette_is_sorted_dark_to_light():
    palette, index = pipeline.palette_of(
        np.array([[255, 255, 255], [0, 0, 0], [255, 0, 0]])
    )
    assert palette.tolist() == [[0, 0, 0], [255, 0, 0], [255, 255, 255]]
    assert index.tolist() == [2, 0, 1]


# ----------------------------------------------------------------------
# The hardware path, against a fake service that runs the jobs on Aer
# ----------------------------------------------------------------------
class FakeService:
    """Stands in for QiskitRuntimeService: a 3x4 grid chip, jobs run on Aer."""

    jobs: ClassVar[dict] = {}

    def __init__(self):
        from iqpixl import Lattice
        from iqpixl.backends.aer import topology_device

        self._backend = topology_device(Lattice(3, 4))

    def backend(self, name):
        return self._backend

    def job(self, job_id):
        return self.jobs[job_id]


class FakeJob:
    def __init__(self, result):
        self._result = result

    def job_id(self):
        return "fake-job"

    def status(self):
        return "DONE"

    def result(self):
        return self._result


def test_the_hardware_path_decodes_whole_circuit_counts(monkeypatch):
    import qiskit_ibm_runtime
    from qiskit_aer.primitives import SamplerV2 as AerSampler

    class FakeSampler:
        def __init__(self, mode):
            pass

        def run(self, pubs, shots):
            job = FakeJob(AerSampler(seed=0).run(pubs, shots=shots).result())
            FakeService.jobs["fake-job"] = job
            return job

    monkeypatch.setattr(runners, "_service", lambda credentials: FakeService())
    monkeypatch.setattr(qiskit_ibm_runtime, "SamplerV2", FakeSampler)

    image = sprite(w=6, h=5)  # 26 opaque pixels; the 3x4 grid holds 48 slots in Z + X
    request = {
        "device": "fez",
        "representation": "gray",
        "basis": "zx",
        "images": [image],
        "shots": 50_000,
    }
    ticket = runners.ibm_submit(request, {})
    assert ticket["n_circuits"] == 2  # Z and X, whole circuit each
    out = runners.ibm_status(request, {}, ticket)
    assert out["status"] == "DONE"
    assert out["metrics"]["mean_abs_error_255"] < 6


def test_the_hardware_path_decodes_a_palette_constellation(monkeypatch):
    import qiskit_ibm_runtime
    from qiskit_aer.primitives import SamplerV2 as AerSampler

    class FakeSampler:
        def __init__(self, mode):
            pass

        def run(self, pubs, shots):
            job = FakeJob(AerSampler(seed=0).run(pubs, shots=shots).result())
            FakeService.jobs["fake-job"] = job
            return job

    monkeypatch.setattr(runners, "_service", lambda credentials: FakeService())
    monkeypatch.setattr(qiskit_ibm_runtime, "SamplerV2", FakeSampler)

    request = {
        "device": "fez",
        "representation": "palette",
        "basis": "sphere2",
        "images": [sprite(w=6, h=5, colours=PALETTE)],
        "shots": 20_000,
    }
    ticket = runners.ibm_submit(request, {})
    assert ticket["n_circuits"] == 3  # Z, X and Y, whole circuit each
    out = runners.ibm_status(request, {}, ticket)
    assert out["symbol_scheme"] == "constellation"
    assert out["metrics"]["pixels_exact"] == 1.0
    assert out["drr"]["scores"]["none"] == 0.0  # a constellation scores wrong pixels


# ----------------------------------------------------------------------
# Palettes as symbols
# ----------------------------------------------------------------------
def test_palette_bases_pick_their_symbol_scheme():
    graph = devices.graph("fez")
    image = sprite(colours=PALETTE)
    for basis, scheme, per_slot in [
        ("zx", "levels", 2),
        ("sphere1", "constellation", 1),
        ("sphere2", "constellation", 2),
    ]:
        plan = pipeline.build_plan(
            {"representation": "palette", "basis": basis, "images": [image]}, graph
        )
        enc = plan.extra["symbols"]
        assert (enc.scheme, enc.per_slot) == (scheme, per_slot)
    plan = pipeline.build_plan(
        {"representation": "palette", "basis": "z", "images": [image]}, graph
    )
    assert "symbols" not in plan.extra  # one level per slot: the plain value decode


def test_a_constellation_needs_few_shots_for_many_colours():
    """20 colours, two per slot, 8192 shots: a constellation gets about 97% of the
    pixels right, Z + X levels about 70%."""
    rng = np.random.default_rng(4)
    colours = [tuple(int(x) for x in rng.integers(0, 256, 3)) for _ in range(20)]
    request = {
        "device": "phoenix",
        "representation": "palette",
        "images": [sprite(w=30, h=30, seed=5, colours=colours)],
        "shots": 8192,
        "noise": "none",
    }
    sphere = runners.simulate({**request, "basis": "sphere2"})
    levels = runners.simulate({**request, "basis": "zx"})
    assert sphere["metrics"]["palette_size"] == 20
    assert levels["metrics"]["pixels_exact"] < 0.9 < sphere["metrics"]["pixels_exact"]


def test_too_many_colours_for_a_constellation_are_refused():
    rng = np.random.default_rng(6)
    colours = [tuple(int(x) for x in rng.integers(0, 256, 3)) for _ in range(70)]
    request = {
        "representation": "palette",
        "basis": "sphere2",
        "images": [sprite(w=16, h=16, seed=7, colours=colours)],
    }
    with pytest.raises(ValueError, match="at most 64 colours"):
        pipeline.build_plan(request, devices.graph("phoenix"))


# ----------------------------------------------------------------------
# Images too big for the chip
# ----------------------------------------------------------------------
def test_a_big_image_splits_into_bands_that_each_fit():
    """Bands of nearly equal size in reading order, each cropped to its rows, and
    one band (the request itself) when the image fits."""
    graph = devices.graph("fez")  # 448 slots
    image = sprite(w=40, h=30, seed=1)  # 1196 opaque pixels
    request = {"representation": "gray", "basis": "z", "images": [image]}
    bands = split.band_requests(request, graph.capacity)
    assert len(bands) == split.band_count(request, graph.capacity) == 3
    sizes = []
    for j, band in enumerate(bands):
        top = band["split"]["top"]
        assert band["split"] == {"band": j, "of": 3, "top": top, "height": 30}
        plan = pipeline.build_plan(band, graph)  # fits, or this raises
        sizes.append(int(plan.mask.sum()))
    assert sum(sizes) == 1196 and max(sizes) - min(sizes) <= 1
    assert split.band_requests({**request, "basis": "zx"}, graph.capacity)[0]["split"]
    small = {**request, "images": [sprite(w=10, h=10)]}
    assert split.band_requests(small, graph.capacity) == [small]
    with pytest.raises(ValueError, match="band 3 does not exist"):
        split.band_request(request, graph.capacity, 3)


@pytest.mark.parametrize(
    ("rep", "basis", "extra"),
    [
        ("gray", "zx", {}),
        ("palette", "sphere2", {}),
        ("color", "zx", {"colorspace": "hsv"}),
        ("transition", "zx", {"transition_values": "palette", "alphas": 3}),
    ],
)
def test_the_joined_bands_are_the_whole_image(rep, basis, extra):
    colours = PALETTE if rep in ("palette", "transition") else None
    images = [sprite(w=36, h=30, seed=s, colours=colours) for s in (2, 3)]
    request = {
        "device": "fez",
        "representation": rep,
        "basis": basis,
        "images": images[: 2 if rep == "transition" else 1],
        "shots": 20_000,
        "noise": "none",
        **extra,
    }
    count = split.band_count(request, devices.graph("fez").capacity)
    assert count > 1
    out = split.join([runners.simulate({**request, "band": j}) for j in range(count)])
    assert (out["width"], out["height"], out["bands"]) == (36, 30, count)
    assert "split" not in out
    want = rgba_of(images[0]["rgba"], 36, 30)
    got = rgba_of(out["image"] if "image" in out else out["frames"][0], 36, 30)
    opaque = want[..., 3] >= 128
    assert (got[..., 3] == 255).tolist() == opaque.tolist()
    band = np.array(out["pixel_band"]).reshape(30, 36)
    assert ((band >= 0) == opaque).all()
    assert set(band[opaque].tolist()) == set(range(count))
    if rep == "palette":  # every band kept the whole palette: exact colours
        assert (got[opaque, :3] == want[opaque, :3]).all()
        assert out["metrics"]["pixels_exact"] == 1.0
    elif rep == "gray":
        assert out["metrics"]["mean_abs_error_255"] < 6


def test_a_big_image_is_one_ibm_job(monkeypatch):
    import qiskit_ibm_runtime
    from qiskit_aer.primitives import SamplerV2 as AerSampler

    runs = []

    class FakeSampler:
        def __init__(self, mode):
            pass

        def run(self, pubs, shots):
            runs.append(len(pubs))
            job = FakeJob(AerSampler(seed=0).run(pubs, shots=shots).result())
            FakeService.jobs["fake-job"] = job
            return job

    monkeypatch.setattr(runners, "_service", lambda credentials: FakeService())
    monkeypatch.setattr(qiskit_ibm_runtime, "SamplerV2", FakeSampler)

    image = sprite(w=12, h=12, seed=4)  # 140 opaque pixels; the 3x4 grid holds 48 slots
    request = {"representation": "gray", "basis": "z", "images": [image]}
    request["shots"] = 50_000
    ticket = runners.ibm_submit(request, {})
    assert runs == [3] and ticket["bands"] == 3 and ticket["n_circuits"] == 3
    out = runners.ibm_status(request, {}, ticket)
    assert out["bands"] == 3 and out["run"]["bands"] == 3
    assert out["n_pixels"] == 140 and (out["width"], out["height"]) == (12, 12)
    assert out["metrics"]["mean_abs_error_255"] < 6


# ----------------------------------------------------------------------
# Mappings
# ----------------------------------------------------------------------
def _pieces(mask, graph, how, per_slot=1, seed=0):
    """``{data-qubit index: list of (y, x)}`` for an assignment."""
    slot, _ = mapping.assign(how, mask, graph, per_slot, seed)
    bounds = np.cumsum([g.n_values for g in graph.groups])
    group = np.searchsorted(bounds, slot, side="right")
    ys, xs = np.nonzero(mask)
    pieces: dict = {}
    for g, y, x in zip(group, ys, xs, strict=True):
        pieces.setdefault(int(g), []).append((int(y), int(x)))
    return slot, pieces


def _is_rectangle(cells):
    ys, xs = zip(*cells, strict=True)
    return (max(ys) - min(ys) + 1) * (max(xs) - min(xs) + 1) == len(cells)


def test_squares_give_every_data_qubit_a_whole_block():
    graph = devices.graph("phoenix")  # 40 x 16 + 18 x 8 = 784 = 28 x 28
    _, pieces = _pieces(np.ones((28, 28), bool), graph, "squares")
    assert all(_is_rectangle(c) for c in pieces.values())
    shapes = {(len({y for y, _ in c}), len({x for _, x in c})) for c in pieces.values()}
    assert shapes == {(4, 4), (2, 4)}


def test_rectangles_are_flat_blocks():
    graph = devices.graph("phoenix")
    _, pieces = _pieces(np.ones((16, 32), bool), graph, "rectangles")
    assert all(_is_rectangle(c) for c in pieces.values())
    assert {
        (len({y for y, _ in c}), len({x for _, x in c})) for c in pieces.values()
    } == {(2, 8)}


def test_two_pixels_per_slot_doubles_the_tiles():
    graph = devices.graph("phoenix")
    _, pieces = _pieces(np.ones((32, 32), bool), graph, "squares", per_slot=2)
    assert all(_is_rectangle(c) and len(c) == 32 for c in pieces.values())


def test_patches_are_mostly_connected_pieces_of_the_right_size():
    graph = devices.graph("phoenix")
    slot, pieces = _pieces(np.ones((28, 28), bool), graph, "patches", seed=4)
    assert np.unique(slot).size == slot.size  # one pixel per slot
    sizes = {g: n.n_values for g, n in enumerate(graph.groups)}
    assert all(len(c) == sizes[g] for g, c in pieces.items())

    def connected(cells):
        cells = set(cells)
        todo, seen = [next(iter(cells))], set()
        while todo:
            y, x = todo.pop()
            if (y, x) in seen:
                continue
            seen.add((y, x))
            todo += [
                p for p in [(y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)] if p in cells
            ]
        return seen == cells

    share = np.mean([connected(c) for c in pieces.values()])
    assert share > 0.85, share


@pytest.mark.parametrize("how", ["patches", "random"])
def test_seeded_mappings_repeat(how):
    graph = devices.graph("fez")
    mask = np.ones((20, 20), bool)
    first, _ = mapping.assign(how, mask, graph, 1, seed=1)
    again, _ = mapping.assign(how, mask, graph, 1, seed=1)
    other, _ = mapping.assign(how, mask, graph, 1, seed=2)
    assert (first == again).all() and (first != other).any()


@pytest.mark.parametrize("how", mapping.MAPPINGS)
@pytest.mark.parametrize("basis", ["z", "zx"])
def test_every_mapping_round_trips(how, basis):
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "gray",
            "basis": basis,
            "mapping": how,
            "seed": 3,
            "images": [sprite(w=12, h=10)],
            "shots": 100_000,
            "noise": "none",
        }
    )
    assert out["metrics"]["mean_abs_error_255"] < 6
    assert out["mapping"] == how


# ----------------------------------------------------------------------
# Full colour
# ----------------------------------------------------------------------
@pytest.mark.parametrize("space", sorted(pipeline.COLOR_SPACES))
def test_colour_spaces_round_trip(space):
    rgb = np.random.default_rng(5).random((300, 3))
    _, to_space, to_rgb, _ = pipeline.COLOR_SPACES[space]
    channels = to_space(rgb)
    assert channels.min() >= 0 and channels.max() <= 1
    assert np.allclose(to_rgb(channels), rgb)


@pytest.mark.parametrize("space", sorted(pipeline.COLOR_SPACES))
@pytest.mark.parametrize("basis", ["z", "zx"])
def test_full_colour_round_trips_without_noise(space, basis):
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "color",
            "colorspace": space,
            "basis": basis,
            "mapping": "squares",
            "images": [sprite(w=9, h=8)],
            "shots": 100_000,
            "noise": "none",
        }
    )
    assert out["channels"] == list(pipeline.COLOR_SPACES[space][0])
    assert out["metrics"]["mean_abs_error_rgb_255"] < 6
    groups = np.array(out["pixel_group"]).reshape(3, 8, 9)  # one map per channel
    want = rgba_of(sprite(w=9, h=8)["rgba"], 9, 8)[..., 3] == 255
    assert all(((g >= 0) == want).all() for g in groups)
    assert len({int(x) for x in groups[0][want]} & {int(x) for x in groups[2][want]}) == 0


def _opaque(w, h, seed=0):
    rgba = np.random.default_rng(seed).integers(0, 256, size=(h, w, 4)).astype(np.uint8)
    rgba[..., 3] = 255
    return {"width": w, "height": h, "rgba": base64.b64encode(rgba.tobytes()).decode()}


def test_full_colour_fits_22_by_22_on_phoenix_in_two_bases():
    graph = devices.graph("phoenix")  # 792 slots x 2 values = 1584 = 528 pixels x 3
    request = {"representation": "color", "basis": "zx"}
    plan = pipeline.build_plan({**request, "images": [_opaque(22, 22)]}, graph)
    assert plan.extra["n_gray"] == 0
    plan = pipeline.build_plan({**request, "images": [sprite(w=23, h=23)]}, graph)
    assert plan.extra["n_gray"] == 0  # 525 pixels: the transparent corners make room


def test_a_colour_image_just_short_of_room_goes_grayscale_bottom_right():
    graph = devices.graph("phoenix")
    request = {"representation": "color", "basis": "zx"}
    plan = pipeline.build_plan({**request, "images": [_opaque(23, 23)]}, graph)
    assert plan.extra["n_gray"] == 2  # 1587 numbers for 1584 places: 2 pixels save 4
    assert plan.slot.size == 3 * 529 - 2 * 2
    with pytest.raises(ValueError, match="slots"):  # far over: no quiet downgrade
        pipeline.build_plan({**request, "images": [_opaque(26, 26)]}, graph)


def test_the_grayscale_pixels_decode_and_are_flagged():
    image = _opaque(23, 23, seed=1)
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "color",
            "colorspace": "hsv",
            "basis": "zx",
            "mapping": "patches",
            "images": [image],
            "shots": 100_000,
            "noise": "none",
        }
    )
    assert out["grayscale_pixels"] == 2
    assert "bottom right" in out["notes"][0]
    got = rgba_of(out["image"], 23, 23)
    corner = got[-1, -2:, :3].astype(int)
    assert (corner[:, 0] == corner[:, 1]).all() and (corner[:, 1] == corner[:, 2]).all()
    groups = np.array(out["pixel_group"]).reshape(3, 23, 23)
    assert (groups[0] >= 0).all()  # channel 0 holds every pixel (gray for the last two)
    assert (groups[1:, -1, -2:] == -1).all() and (groups[1:, :-1] >= 0).all()
    assert out["metrics"]["mean_abs_error_rgb_255"] < 8


# ----------------------------------------------------------------------
# Dynamic range recovery
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rep", "basis", "n_images", "extra"),
    [
        ("gray", "z", 1, {}),
        ("palette", "zx", 1, {}),
        ("palette", "sphere2", 1, {}),
        ("color", "zx", 1, {"colorspace": "ycbcr"}),
        ("huevalue", "zxy", 1, {}),
        ("transition", "zx", 2, {"alphas": 3}),
    ],
)
def test_every_mode_returns_raw_and_the_best_recovery(rep, basis, n_images, extra):
    out = runners.simulate(
        {
            "device": "fez",
            "representation": rep,
            "basis": basis,
            "images": [sprite(seed=k) for k in range(n_images)],
            "shots": 4000,
            **extra,
        }
    )
    drr = out["drr"]
    assert drr["method"] in pipeline.DRR_METHODS
    assert set(drr["scores"]) == {"none", *pipeline.DRR_METHODS}
    assert drr["scores"][drr["method"]] == min(
        drr["scores"][m] for m in pipeline.DRR_METHODS
    )
    key = "frames" if rep == "transition" else "image"
    assert key in out and key in drr  # the raw read-back is always there too


def test_recovery_beats_raw_on_a_noisy_grayscale_image():
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "gray",
            "basis": "z",
            "images": [sprite(w=20, h=20)],
            "shots": 8000,
        }
    )
    assert out["drr"]["scores"][out["drr"]["method"]] < out["drr"]["scores"]["none"]


def test_recovery_never_touches_hue():
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "huevalue",
            "basis": "zxy",
            "images": [sprite()],
            "shots": 4000,
        }
    )
    hue = "mean_abs_error_hue_degrees"
    assert out["drr"]["metrics"][hue] == out["metrics"][hue]


# ----------------------------------------------------------------------
# Palette order
# ----------------------------------------------------------------------
PALETTE = [(10, 20, 30), (200, 40, 40), (40, 200, 40), (250, 250, 250)]


def test_a_chosen_palette_order_sets_where_each_colour_is_stored():
    hexes = ["#{:02x}{:02x}{:02x}".format(*c) for c in PALETTE]
    chosen = [hexes[2], hexes[0], hexes[3], hexes[1]]
    rgb = np.array(PALETTE * 3, dtype=np.uint8)
    palette, index = pipeline.palette_of(rgb, chosen)
    assert ["#{:02x}{:02x}{:02x}".format(*tuple(c)) for c in palette] == chosen
    assert index.tolist()[:4] == [1, 3, 0, 2]
    with pytest.raises(ValueError, match="palette_order"):
        pipeline.palette_of(rgb, chosen[:3])
    with pytest.raises(ValueError, match="palette_order"):
        pipeline.palette_of(rgb, [*chosen[:3], "#123456"])


def test_a_split_job_keeps_the_whole_palette_order():
    """One band of a split image may list colours it does not use, so every band
    stores each colour at the same place; a request that is not split may not."""
    hexes = ["#{:02x}{:02x}{:02x}".format(*c) for c in PALETTE]
    rgb = np.array(PALETTE[:2] * 3, dtype=np.uint8)
    palette, index = pipeline.palette_of(rgb, hexes[::-1], extra_ok=True)
    assert len(palette) == 4 and index.tolist()[:2] == [3, 2]
    with pytest.raises(ValueError, match="palette_order"):
        pipeline.palette_of(rgb, hexes[::-1])
    with pytest.raises(ValueError, match="palette_order"):  # a used colour is missing
        pipeline.palette_of(rgb, hexes[1:], extra_ok=True)

    image = sprite(w=6, h=5, seed=3, colours=PALETTE[:2])
    request = {"representation": "palette", "basis": "sphere1", "images": [image]}
    request["palette_order"] = hexes
    graph = devices.graph("fez")
    with pytest.raises(ValueError, match="palette_order"):
        pipeline.build_plan(request, graph)
    plan = pipeline.build_plan({**request, "split": {"job": 0, "of": 2}}, graph)
    assert len(plan.extra["palette"]) == 4
    assert set(plan.extra["index"].tolist()) <= {0, 1}


@pytest.mark.parametrize("basis", ["z", "zx", "sphere2"])
def test_any_palette_order_round_trips(basis):
    hexes = ["#{:02x}{:02x}{:02x}".format(*c) for c in PALETTE]
    out = runners.simulate(
        {
            "device": "fez",
            "representation": "palette",
            "basis": basis,
            "palette_order": [hexes[3], hexes[1], hexes[0], hexes[2]],
            "images": [sprite(colours=PALETTE)],
            "shots": 20_000,
            "noise": "none",
        }
    )
    assert out["palette"] == [list(PALETTE[k]) for k in (3, 1, 0, 2)]
    assert out["metrics"]["pixels_exact"] == 1.0


# ----------------------------------------------------------------------
# Palette transitions
# ----------------------------------------------------------------------
def _palette_sprite(w, h, seed, hole):
    """A 4-colour sprite with a transparent square at ``hole`` (y0, y1, x0, x1)."""
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[..., :3] = np.array(PALETTE, np.uint8)[
        np.random.default_rng(seed).integers(0, 4, (h, w))
    ]
    rgba[..., 3] = 255
    y0, y1, x0, x1 = hole
    rgba[y0:y1, x0:x1, 3] = 0
    return rgba, {
        "width": w,
        "height": h,
        "rgba": base64.b64encode(rgba.tobytes()).decode(),
    }


def test_the_shared_palette_includes_transparency():
    a, _ = _palette_sprite(6, 5, 0, (0, 2, 0, 2))
    b, _ = _palette_sprite(6, 5, 1, (3, 5, 4, 6))
    keys, (ia, ib) = pipeline.shared_palette([a.reshape(-1, 4), b.reshape(-1, 4)])
    assert (ib[b[..., 3].ravel() == 0] == 0).all()
    assert keys[0] == pipeline.TRANSPARENT and len(keys) == 5
    assert (ia[a[..., 3].ravel() == 0] == 0).all()


def test_a_palette_transition_ends_on_each_sprite_with_its_shape():
    a_px, a = _palette_sprite(8, 7, 2, (0, 3, 0, 3))
    b_px, b = _palette_sprite(8, 7, 3, (4, 7, 5, 8))
    hexes = ["#{:02x}{:02x}{:02x}".format(*c) for c in PALETTE]
    order = [hexes[2], pipeline.TRANSPARENT, hexes[0], hexes[3], hexes[1]]
    out = runners.simulate(
        {
            "device": "phoenix",
            "representation": "transition",
            "transition_values": "palette",
            "palette_order": order,
            "images": [a, b],
            "alphas": 3,
            "shots": 100_000,
            "noise": "none",
        }
    )
    assert out["transition_palette"] == order
    assert out["metrics"]["pixels_exact_A_at_0"] == 1.0
    assert out["metrics"]["pixels_exact_B_at_90"] == 1.0
    first = rgba_of(out["frames"][0], 8, 7)
    last = rgba_of(out["frames"][-1], 8, 7)
    for got, want in [(first, a_px), (last, b_px)]:
        assert (got[..., 3] == want[..., 3]).all()  # the sprite's shape
        solid = want[..., 3] == 255
        assert (got[solid] == want[solid]).all()  # and its colours


def test_unknown_transition_values_are_rejected():
    with pytest.raises(ValueError, match="transition_values"):
        pipeline.build_plan(
            {
                "representation": "transition",
                "transition_values": "rgb",
                "images": [sprite(), sprite()],
            },
            devices.graph("fez"),
        )
