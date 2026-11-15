"use strict";

// An image too big for the chip runs in parts: bands of opaque pixels, in reading
// order, that each fit. The backend cuts the bands (tessa/split.py). Here the page
// counts them for its warning, and joins the simulator's bands into one result.
// IBM hardware runs every band in one job, and the backend joins those itself,
// in the same way (tessa.split.join).
//
// A band's result covers only the rows the band touches. Its `split` says where:
// { band, of, top, height }, with `height` the whole image's height.

const Split = (() => {
  // Most opaque pixels one band can hold: tessa.split, slot count inverted.
  function pixelsPerBand(capacity, valuesPerSlot, valuesPerPixel) {
    return Math.floor((capacity * valuesPerSlot) / valuesPerPixel);
  }

  function count(pixels, perBand) {
    return Math.max(1, Math.ceil(pixels / perBand));
  }

  function toBase64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }

  function fromBase64(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  // Mean of each number over the bands, weighted by each band's pixel count
  // (every metric is a mean or a fraction over pixels). A palette size is the largest.
  function weighted(dicts, weights) {
    const total = weights.reduce((s, w) => s + w, 0);
    const out = {};
    for (const key of Object.keys(dicts[0])) {
      const values = dicts.map((d) => d[key]);
      if (!values.every((v) => typeof v === "number")) out[key] = values[0];
      else if (key === "palette_size") out[key] = Math.max(...values);
      else out[key] = values.reduce((s, v, j) => s + v * weights[j], 0) / total;
    }
    return out;
  }

  // The bands' results -> one result of the whole image, as tessa.split.join.
  function join(results) {
    if (results.length === 1) return results[0];
    const r0 = results[0];
    const width = r0.width;
    const height = r0.split.height;
    const n = width * height;
    const layers = r0.pixel_group.length / (r0.width * r0.height);
    // For each band: the whole image's index of each of its own pixels.
    const owned = results.map((r) => {
      const at = [];
      const size = r.width * r.height;
      for (let i = 0; i < size; i++) if (r.pixel_group[i] >= 0) at.push([i, r.split.top * width + i]);
      return at;
    });
    const pixelBand = new Array(n).fill(-1);
    owned.forEach((at, j) => at.forEach(([, w]) => (pixelBand[w] = j)));

    const perPixel = (key) => {
      const out = new Array(layers * n).fill(-1);
      results.forEach((r, j) => {
        const size = r.width * r.height;
        for (let c = 0; c < layers; c++) for (const [i, w] of owned[j]) out[c * n + w] = r[key][c * size + i];
      });
      return out;
    };
    const image = (pick) => {
      const out = new Uint8Array(4 * n);
      results.forEach((r, j) => {
        const crop = fromBase64(pick(r));
        for (const [i, w] of owned[j]) out.set(crop.subarray(4 * i, 4 * i + 4), 4 * w);
      });
      return toBase64(out);
    };
    const images = (get) =>
      get(r0).frames
        ? { frames: get(r0).frames.map((_, k) => image((r) => get(r).frames[k])) }
        : { image: image((r) => get(r).image) };

    const bands = results.length;
    const weights = results.map((r) => r.n_pixels);
    const methods = results.map((r) => r.drr.method);
    const scores = weighted(results.map((r) => r.drr.scores), weights);
    // Each band picks its own best recovery. If they differ, the joined image
    // mixes methods, and "per part" scores that mix.
    const method = methods.every((m) => m === methods[0]) ? methods[0] : "per part";
    if (method === "per part") scores[method] = weighted(results.map((r) => ({ s: r.drr.scores[r.drr.method] })), weights).s;
    const { split, ...rest } = r0;
    return {
      ...rest,
      ...images((r) => r),
      height,
      n_pixels: weights.reduce((s, w) => s + w, 0),
      slots_used: results.reduce((s, r) => s + r.slots_used, 0),
      bands,
      pixel_band: pixelBand,
      pixel_group: perPixel("pixel_group"),
      pixel_address: perPixel("pixel_address"),
      pixel_part: perPixel("pixel_part"),
      metrics: weighted(results.map((r) => r.metrics), weights),
      notes: [
        `The image was too big for the chip, so it ran in ${bands} parts, one band of pixels each, ` +
          `and the results were joined. Every part used the same qubits.`,
      ],
      run: { ...r0.run, bands },
      drr: {
        method,
        methods,
        metrics: weighted(results.map((r) => r.drr.metrics), weights),
        scores,
        ...images((r) => r.drr),
      },
    };
  }

  return { pixelsPerBand, count, join };
})();

if (typeof module !== "undefined") module.exports = Split; // for tests under Node
