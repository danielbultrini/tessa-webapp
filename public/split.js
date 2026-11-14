"use strict";

// An image too big for the chip, split into jobs that each fit, and the jobs'
// results put back together into one result the page can draw.
//
// The opaque pixels are cut, in reading order, into bands of nearly equal size.
// Job j gets the whole image with every pixel outside band j made transparent,
// so every job's result has the same width and height, and each pixel of the
// joined result comes from the one job that encoded it. Every job keeps the
// whole image's palette order (the backend allows that for a request with a
// `split` field), so a colour sits at the same place on the qubit in every job.

const Split = (() => {
  // Most opaque pixels one job can hold: the backend's slot count, inverted.
  function pixelsPerJob(capacity, valuesPerSlot, valuesPerPixel) {
    return Math.floor((capacity * valuesPerSlot) / valuesPerPixel);
  }

  // mask: 0/1 per pixel (opaque in any input image). Returns the number of jobs
  // and the job of every pixel (-1 for a transparent pixel).
  function plan(mask, perJob) {
    let n = 0;
    for (let i = 0; i < mask.length; i++) n += mask[i] ? 1 : 0;
    const jobs = Math.max(1, Math.ceil(n / perJob));
    const owner = new Int32Array(mask.length).fill(-1);
    let k = 0;
    for (let i = 0; i < mask.length; i++) {
      if (!mask[i]) continue;
      owner[i] = Math.floor((k * jobs) / n); // band sizes differ by at most one
      k++;
    }
    return { jobs, owner, pixels: n };
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

  // The request of job j: `base` without its images, and each image (raw RGBA
  // bytes) with the pixels of other jobs made transparent.
  function request(base, images, owner, j, jobs) {
    const imgs = images.map((im) => {
      const data = new Uint8Array(im.data);
      for (let i = 0; i < owner.length; i++) if (owner[i] !== j) data[4 * i + 3] = 0;
      return { width: im.width, height: im.height, rgba: toBase64(data) };
    });
    return { ...base, images: imgs, split: { job: j, of: jobs } };
  }

  // Each pixel's RGBA from the job that owns it.
  function joinImage(b64s, owner) {
    const parts = b64s.map(fromBase64);
    const out = new Uint8Array(4 * owner.length);
    for (let i = 0; i < owner.length; i++) {
      const j = owner[i];
      if (j < 0) continue;
      out.set(parts[j].subarray(4 * i, 4 * i + 4), 4 * i);
    }
    return toBase64(out);
  }

  // Per-pixel arrays (one block of owner.length per layer) from the owning job.
  function joinPerPixel(arrays, owner) {
    const n = owner.length;
    const out = new Array(arrays[0].length).fill(-1);
    for (let at = 0; at < out.length; at++) {
      const j = owner[at % n];
      if (j >= 0) out[at] = arrays[j][at];
    }
    return out;
  }

  // Mean of each number over the jobs, weighted by each job's pixel count (every
  // metric is a mean or a fraction over pixels). A palette size is the largest.
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

  function joinImages(results, owner, pick) {
    const first = pick(results[0]);
    if (first.frames) {
      return { frames: first.frames.map((_, k) => joinImage(results.map((r) => pick(r).frames[k]), owner)) };
    }
    return { image: joinImage(results.map((r) => pick(r).image), owner) };
  }

  // The jobs' results -> one result, shaped as the backend's own.
  function join(results, owner) {
    if (results.length === 1) return results[0];
    const r0 = results[0];
    const weights = results.map((r) => r.n_pixels);
    const methods = results.map((r) => r.drr.method);
    const same = methods.every((m) => m === methods[0]);
    const scores = weighted(results.map((r) => r.drr.scores), weights);
    // Each job picks its own best dynamic range recovery. If they differ, the
    // joined image mixes methods, and "per job" scores that mix.
    const method = same ? methods[0] : "per job";
    if (!same) scores[method] = weighted(results.map((r) => ({ s: r.drr.scores[r.drr.method] })), weights).s;
    const jobs = results.length;
    const run = { ...r0.run, jobs };
    if (r0.run && r0.run.job_id) run.job_id = results.map((r) => r.run.job_id).join(", ");
    const pixelJob = Array.from(owner);
    return {
      ...r0,
      ...joinImages(results, owner, (r) => r),
      n_pixels: weights.reduce((s, w) => s + w, 0),
      slots_used: results.reduce((s, r) => s + r.slots_used, 0),
      jobs,
      pixel_job: pixelJob,
      pixel_group: joinPerPixel(results.map((r) => r.pixel_group), owner),
      pixel_address: joinPerPixel(results.map((r) => r.pixel_address), owner),
      pixel_part: joinPerPixel(results.map((r) => r.pixel_part), owner),
      metrics: weighted(results.map((r) => r.metrics), weights),
      notes: [
        `The image was too big for the chip, so it ran as ${jobs} jobs, one band of pixels each, ` +
          `and the results were joined. Each job reused the same qubits.`,
      ],
      run,
      drr: {
        method,
        methods,
        metrics: weighted(results.map((r) => r.drr.metrics), weights),
        scores,
        ...joinImages(results, owner, (r) => r.drr),
      },
    };
  }

  return { pixelsPerJob, plan, request, join };
})();

if (typeof module !== "undefined") module.exports = Split; // for tests under Node
