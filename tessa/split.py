"""An image too big for the chip, as bands that each fit, and their results joined.

The opaque pixels are cut, in reading order, into ``count`` bands of nearly equal
size: opaque pixel ``k`` of ``n`` goes to band ``k * count // n``. Each band's
request holds only the rows the band touches, with every pixel of another band
made transparent, so a band's result stays small. ``split`` in the band's request
(and in its result) says where the band sits::

    {"band": j, "of": count, "top": first row, "height": the whole image's height}

Every band keeps the whole image's palette order (see :mod:`tessa.pipeline`).
:func:`join` puts the bands' results back into one result of the whole image,
shaped as :func:`tessa.pipeline.finish` shapes one. ``public/split.js`` joins the
simulator's bands the same way in the page.
"""

from __future__ import annotations

import base64

import numpy as np

from .pipeline import REPRESENTATIONS, parse_image, slot_cost, slots_needed


def _layout(request: dict, capacity: int):
    """``(images, owner, count)`` for a request that must be split, else ``None``.

    ``owner`` is the ``(h, w)`` band of every pixel, ``-1`` where transparent. A
    request that fits, or that :func:`tessa.pipeline.build_plan` would refuse for
    another reason (no image, sizes differ, nothing opaque), is not split.
    """
    rep = request.get("representation", "gray")
    if rep not in REPRESENTATIONS or not request.get("images"):
        return None
    basis = request.get("basis", REPRESENTATIONS[rep][0])
    images = [parse_image(im) for im in request["images"]]
    if any(im.shape != images[0].shape for im in images):
        return None
    mask = np.any([im[..., 3] >= 128 for im in images], axis=0)
    n = int(mask.sum())
    layers, per_slot = slot_cost(rep, basis)
    if n == 0 or slots_needed(n, layers, per_slot, capacity)[1] <= capacity:
        return None
    per_band = capacity * per_slot // layers
    count = -(-n // per_band)
    owner = np.full(mask.size, -1)
    owner[np.flatnonzero(mask.ravel())] = np.arange(n) * count // n
    return images, owner.reshape(mask.shape), count


def band_count(request: dict, capacity: int) -> int:
    """How many bands ``request`` runs as on a chip of ``capacity`` slots."""
    layout = _layout(request, capacity)
    return 1 if layout is None else layout[2]


def band_requests(request: dict, capacity: int) -> list[dict]:
    """One request per band; ``[request]`` itself if it needs no split."""
    layout = _layout(request, capacity)
    if layout is None:
        return [request]
    images, owner, count = layout
    height, width = owner.shape
    settings = {k: v for k, v in request.items() if k not in ("images", "band")}
    out = []
    for j in range(count):
        rows = np.flatnonzero((owner == j).any(axis=1))
        top, bottom = int(rows[0]), int(rows[-1]) + 1
        other = owner[top:bottom] != j
        crops = []
        for im in images:
            crop = im[top:bottom].copy()
            crop[other, 3] = 0
            rgba = base64.b64encode(crop.tobytes()).decode()
            crops.append({"width": width, "height": bottom - top, "rgba": rgba})
        split = {"band": j, "of": count, "top": top, "height": height}
        out.append({**settings, "images": crops, "split": split})
    return out


def band_request(request: dict, capacity: int, band: int) -> dict:
    """Band ``band`` of ``request`` (see :func:`band_requests`)."""
    bands = band_requests(request, capacity)
    if not 0 <= band < len(bands):
        raise ValueError(f"band {band} does not exist: this image has {len(bands)}")
    return bands[band]


# ----------------------------------------------------------------------
# Join
# ----------------------------------------------------------------------
def _weighted(dicts: list[dict], weights: list[int]) -> dict:
    """Each number's mean over the bands, weighted by their pixel counts (every
    metric is a mean or a fraction over pixels). A palette size is the largest."""
    total = sum(weights)
    out = {}
    for key in dicts[0]:
        values = [d[key] for d in dicts]
        if not all(isinstance(v, int | float) for v in values):
            out[key] = values[0]
        elif key == "palette_size":
            out[key] = max(values)
        else:
            out[key] = sum(v * w for v, w in zip(values, weights, strict=True)) / total
    return out


def join(results: list[dict]) -> dict:
    """The bands' results (each with its ``split``) -> one result of the image."""
    if len(results) == 1:
        return results[0]
    first = results[0]
    width, height = first["width"], first["split"]["height"]
    layers = len(first["pixel_group"]) // (first["width"] * first["height"])
    owns = []  # per band: its rows, and which pixels in them are its own
    band = np.full((height, width), -1)
    per_pixel = {
        key: np.full((layers, height, width), -1)
        for key in ("pixel_group", "pixel_address", "pixel_part")
    }
    for j, r in enumerate(results):
        rows = slice(r["split"]["top"], r["split"]["top"] + r["height"])
        shape = (layers, r["height"], width)
        own = np.asarray(r["pixel_group"]).reshape(shape)[0] >= 0
        owns.append((rows, own))
        band[rows][own] = j
        for key, whole in per_pixel.items():
            mine = np.asarray(r[key]).reshape(shape)
            for c in range(layers):
                whole[c, rows][own] = mine[c][own]

    def image(pick) -> str:
        whole = np.zeros((height, width, 4), np.uint8)
        for (rows, own), b64 in zip(owns, map(pick, results), strict=True):
            crop = np.frombuffer(base64.b64decode(b64), np.uint8).reshape(-1, width, 4)
            whole[rows][own] = crop[own]
        return base64.b64encode(whole.tobytes()).decode()

    def images(get) -> dict:
        if "frames" in get(first):
            frames = len(get(first)["frames"])
            return {
                "frames": [
                    image(lambda r, k=k: get(r)["frames"][k]) for k in range(frames)
                ]
            }
        return {"image": image(lambda r: get(r)["image"])}

    count = len(results)
    weights = [r["n_pixels"] for r in results]
    methods = [r["drr"]["method"] for r in results]
    scores = _weighted([r["drr"]["scores"] for r in results], weights)
    # Each band picks its own best recovery. If they differ, the joined image mixes
    # methods, and "per part" scores that mix.
    method = methods[0] if len(set(methods)) == 1 else "per part"
    if method == "per part":
        best = [{"s": r["drr"]["scores"][r["drr"]["method"]]} for r in results]
        scores[method] = _weighted(best, weights)["s"]
    out = {k: v for k, v in first.items() if k != "split"}
    out.update(
        images(lambda r: r),
        height=height,
        n_pixels=sum(weights),
        slots_used=sum(r["slots_used"] for r in results),
        bands=count,
        pixel_band=band.ravel().tolist(),
        **{key: whole.ravel().tolist() for key, whole in per_pixel.items()},
        metrics=_weighted([r["metrics"] for r in results], weights),
        notes=[
            f"The image was too big for the chip, so it ran in {count} parts, one "
            "band of pixels each, and the results were joined. Every part used the "
            "same qubits."
        ],
        drr={
            "method": method,
            "methods": methods,
            "metrics": _weighted([r["drr"]["metrics"] for r in results], weights),
            "scores": scores,
            **images(lambda r: r["drr"]),
        },
    )
    if "run" in first:
        out["run"] = {**first["run"], "bands": count}
    return out
