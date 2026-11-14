"use strict";

// ---------------------------------------------------------------------------
// What each representation and basis means
// ---------------------------------------------------------------------------
const BASES = {
  gray: ["z", "x", "zx", "zxy"],
  palette: ["z", "x", "zx", "zxy", "sphere1", "sphere2"],
  color: ["z", "x", "zx", "zxy"],
  huevalue: ["zx", "zxy"],
  transition: ["zx"],
};
const BASIS_LABEL = {
  z: "Z",
  x: "X",
  zx: "Z + X",
  zxy: "Z + X + Y",
  sphere1: "Constellation (Z + X + Y)",
  sphere2: "Constellation, two per slot (Z + X + Y)",
};
const BASIS_INFO = {
  gray: {
    z: "Each pixel's brightness is the amplitude (an RY rotation), read in Z. One pixel per slot.",
    x: "Each pixel's brightness is the phase (an RZ rotation on the equator), read in X. One pixel per slot.",
    zx: "Two pixels per slot: one in the amplitude (read in Z), one in the phase on a half turn (read in X). Twice the pixels.",
    zxy: "Two pixels per slot, as Z + X, but the phase uses the full turn and a third (Y) readout. Finer steps for the phase pixels, not more pixels.",
  },
  huevalue: {
    zx: "Value (brightness) in the amplitude, hue in the phase. The whole colour wheel is squeezed onto half a turn of phase. Saturation is kept from the input.",
    zxy: "Value in the amplitude, hue on the full turn of phase, with a third (Y) readout. Finer hue. Saturation is kept from the input.",
  },
  transition: {
    zx: "Image A in the amplitude, image B in the phase. The slider picks a readout axis between Z (0°, image A) and X (90°, image B).",
  },
};
BASIS_INFO.palette = Object.fromEntries(
  Object.entries(BASIS_INFO.gray).map(([k, v]) => [
    k,
    v.replace("brightness", "palette index").replace(/^Two pixels/, "Two pixels (palette indices)") +
      " The palette is derived from the image colours, sorted dark to light.",
  ])
);
BASIS_INFO.palette.zx =
  "Two pixels (palette indices) per slot: one in the amplitude (read in Z), one in the phase  (read in X). " +
  "The decoder snaps the amplitude to its colour first, then reads the phase with that colour's angle. " +
  "The palette is derived from the image colours, sorted dark to light.";
BASIS_INFO.palette.sphere1 =
  "One pixel per slot. Each colour is one point, and the points are spread evenly over the whole sphere of qubit states. " +
  "The decoder reads Z, X and Y and picks the nearest point. The points sit further apart than levels on one axis, " +
  "so fewer shots give exact colours. At most 4096 colours.";
BASIS_INFO.palette.sphere2 =
  "Two pixels per slot. Each pair of colours is one point: k colours make k x k points, spread evenly over the whole sphere. " +
  "The decoder reads Z, X and Y and picks the nearest point. For many colours this needs far fewer shots than Z + X. " +
  "At most 64 colours.";
BASIS_INFO.color = {
  z: "Each channel of each pixel is an amplitude (an RY rotation), read in Z. Three slots per pixel.",
  x: "Each channel of each pixel is a phase (an RZ rotation on the equator), read in X. Three slots per pixel.",
  zx: "Two channel values per slot: one in the amplitude (read in Z), one in the phase on a half turn (read in X). Three slots for every two pixels.",
  zxy: "Two channel values per slot, as Z + X, but the phase uses the full turn and a third (Y) readout. Finer steps for the phase values.",
};
const COLOR_INFO = " The three channels are encoded as three stacked images, so the mapping lays out each channel like a picture.";
const MAPPING_INFO = {
  lines: "Row by row: each data qubit holds a strip of pixels.",
  squares: "Square blocks, such as 4 x 4 for a data qubit with 16 slots. The biggest data qubits are filled first, so each one gets a whole block.",
  rectangles: "Flat blocks, such as 2 x 8 for a data qubit with 16 slots.",
  patches: "Puzzle pieces, grown at random. Change the seed for new pieces.",
  random: "A random shuffle: neighbouring pixels land on unrelated qubits. Change the seed for a new shuffle.",
};
const PART = [
  "amplitude (theta, read in Z)",
  "phase (phi, read in X)",
  "amplitude and phase",
];
// A constellation stores the slot's pixels together, as one point on the sphere.
const SPHERE_PART = {
  sphere1: ["point on the sphere (read in Z, X and Y)"],
  sphere2: ["first place of the slot's point (read in Z, X and Y)", "second place of the slot's point (read in Z, X and Y)"],
};

const $ = (id) => document.getElementById(id);
const state = {
  devices: null,
  images: [null, null],
  result: null,
  resultDevice: null,
  request: null,
  jobs: null, // { requests, owner, tickets, results }: one entry per job
  credentials: null,
  timer: null,
  polling: false,
  hoverGroup: -1,
  frame: 0,
  layer: 0,
  paletteOrder: null, // "#rrggbb" per colour, stored from the |0> end to the |1> end
};

const device = () => document.querySelector("input[name=device]:checked").value;
const target = () => document.querySelector("input[name=target]:checked").value;
const representation = () => $("representation").value;
const basis = () => $("basis").value;
const valuesPerPixel = () => (representation() === "color" ? 3 : 1);
const valuesPerSlot = () =>
  ["gray", "palette", "color"].includes(representation()) && ["zx", "zxy", "sphere2"].includes(basis()) ? 2 : 1;

// Colour results carry one qubit map per channel, channel after channel.
const layerCount = (r) => (r.channels ? r.channels.length : 1);
function layerOf(r, key, layer = state.layer) {
  const n = r.width * r.height;
  return r[key].slice(layer * n, (layer + 1) * n);
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
function square(n) {
  const s = Math.floor(Math.sqrt(n));
  return `${s}x${s}`;
}

function showDevice() {
  const d = state.devices && state.devices[device()];
  if (!d) return;
  const date = d.calibrated_at.slice(0, 10);
  $("device-info").textContent =
    `${d.name}: ${d.n_qubits} qubits, ${d.groups.length} data qubits holding ${d.capacity} slots ` +
    `(calibration of ${date}). One pixel per slot fits ${d.capacity} pixels, for example ${square(d.capacity)}. ` +
    `Grayscale or palette in Z + X or Z + X + Y, or a palette as a constellation with two per slot, ` +
    `puts two pixels in each slot: ${2 * d.capacity} pixels, ` +
    `for example ${square(2 * d.capacity)}. Full colour needs three slots per pixel: ` +
    `${Math.floor(d.capacity / 3)} pixels (${square(d.capacity / 3)}) in Z or X, ` +
    `${Math.floor((2 * d.capacity) / 3)} (${square((2 * d.capacity) / 3)}) in Z + X or Z + X + Y. ` +
    `Transparent pixels cost nothing.`;
}

function showBasis() {
  const rep = representation();
  const keep = basis();
  $("basis").innerHTML = BASES[rep]
    .map((b) => `<option value="${b}">${BASIS_LABEL[b]}</option>`)
    .join("");
  if (BASES[rep].includes(keep)) $("basis").value = keep;
  $("basis-info").textContent = BASIS_INFO[rep][basis()] + (rep === "color" ? COLOR_INFO : "");
  $("colorspace-row").hidden = rep !== "color";
  showPalette();
  const two = rep === "transition";
  $("file-b-row").hidden = !two;
  $("alphas-row").hidden = !two;
  $("tvalues-row").hidden = !two;
  $("file-a-label").textContent = two ? "Image A" : "Image";
  checkFit();
}

// Mirrors tessa.pipeline.grayscale_needed: full colour just short of room stores
// the last few pixels as grayscale, at most 1% of them (at least 2).
function grayscaleNeeded(n, room) {
  const k = Math.ceil((3 * n - room) / 2);
  return k > 0 && k <= Math.max(2, Math.floor(n / 100)) ? k : 0;
}

function opaqueMask() {
  const [a, b] = state.images;
  const imgs = representation() === "transition" ? [a, b] : [a];
  if (imgs.some((im) => !im)) return null;
  if (imgs.length === 2 && (a.width !== b.width || a.height !== b.height)) return "size";
  const mask = new Uint8Array(a.width * a.height);
  for (const im of imgs) for (let i = 0; i < mask.length; i++) if (im.data[4 * i + 3] >= 128) mask[i] = 1;
  return mask;
}

// How the image is split into jobs: one job if it fits (as is, or with a few
// grayscale pixels), more if it does not. Null until there is something to run.
function jobPlan() {
  const d = state.devices && state.devices[device()];
  const mask = opaqueMask();
  if (!d || mask === null || mask === "size") return null;
  const n = mask.reduce((s, v) => s + v, 0);
  const room = d.capacity * valuesPerSlot();
  const gray = valuesPerPixel() === 3 ? grayscaleNeeded(n, room) : 0;
  const slots = Math.ceil((n * valuesPerPixel() - 2 * gray) / valuesPerSlot());
  if (n === 0 || slots <= d.capacity) return { jobs: 1, owner: null, n, gray, slots };
  const perJob = Split.pixelsPerJob(d.capacity, valuesPerSlot(), valuesPerPixel());
  return { ...Split.plan(mask, perJob), n, gray: 0, slots };
}

function checkFit() {
  const fit = $("fit");
  const d = state.devices && state.devices[device()];
  const mask = opaqueMask();
  let ok = false;
  if (!d || mask === null) {
    fit.textContent = representation() === "transition" ? "Choose two images of the same size." : "Choose an image.";
    fit.className = "note";
  } else if (mask === "size") {
    fit.textContent = "The two images must have the same size.";
    fit.className = "note bad";
  } else {
    const { n, gray, slots, jobs } = jobPlan();
    const room = d.capacity * valuesPerSlot();
    ok = n > 0;
    const what = valuesPerPixel() === 3 ? `${n} opaque pixels x 3 channels` : `${n} opaque pixels`;
    if (jobs > 1) {
      const each = Math.ceil(n / jobs);
      fit.textContent =
        `${what} need ${slots} slots, more than the ${d.capacity} on ${d.name}. ` +
        `The image will be split into ${jobs} jobs of about ${each} pixels each. ` +
        `The jobs run one after the other${target() === "ibm" ? " as separate IBM jobs" : ""}, ` +
        `and the results are joined into one image. ` +
        `Each job takes as long as a full run, and a large number of jobs can time out or crash the page.`;
      fit.className = "note warn";
    } else if (gray) {
      fit.textContent =
        `${what} need ${3 * n} numbers, ${3 * n - room} more than the ${room} that ${d.name} holds. ` +
        `It fits if the last ${gray} pixels (bottom right) are stored in grayscale, and they will be.`;
      fit.className = "note warn";
    } else {
      fit.textContent =
        `${what} need ${slots} of the ${d.capacity} slots on ${d.name}. ` +
        (ok ? "It fits." : "There is nothing to encode.");
      fit.className = "note " + (ok ? "good" : "bad");
    }
  }
  $("ibm-fields").hidden = target() !== "ibm";
  $("run").disabled = !ok || state.timer !== null;
  return ok;
}

// ---------------------------------------------------------------------------
// Palette order: which colour sits where on the qubit's axis
// ---------------------------------------------------------------------------
const hex = (r, g, b) => "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
const rgbOf = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const CLEAR = "transparent"; // a pixel transparent in only one transition image
const lum = (h) => {
  if (h === CLEAR) return -1;
  const [r, g, b] = rgbOf(h);
  return 0.299 * r + 0.587 * g + 0.114 * b;
};
function hueKey(h) {
  if (h === CLEAR) return [-1, 0, 0];
  const [r, g, b] = rgbOf(h).map((v) => v / 255);
  const max = Math.max(r, g, b);
  const c = max - Math.min(r, g, b);
  if (c / (max || 1) < 0.1) return [1, 0, lum(h)]; // greys go last, dark to light
  const hue = max === r ? ((g - b) / c + 6) % 6 : max === g ? (b - r) / c + 2 : (r - g) / c + 4;
  return [0, hue, lum(h)];
}

const paletteTransition = () => representation() === "transition" && $("transition-values").value === "palette";
const usesPalette = () => representation() === "palette" || paletteTransition();

// The colours to order: the image's own, or for a transition both images' together,
// with "transparent" for a pixel that is opaque in only one of them.
function imageColours() {
  const imgs = paletteTransition() ? state.images : [state.images[0]];
  if (imgs.some((im) => !im)) return [];
  if (imgs.length === 2 && imgs[0].data.length !== imgs[1].data.length) return [];
  const seen = new Set();
  for (let i = 0; i < imgs[0].data.length; i += 4) {
    const opaque = imgs.map((im) => im.data[i + 3] >= 128);
    if (!opaque.some(Boolean)) continue;
    imgs.forEach((im, k) => seen.add(opaque[k] ? hex(im.data[i], im.data[i + 1], im.data[i + 2]) : CLEAR));
  }
  return [...seen];
}

function sortPalette(how) {
  const order = state.paletteOrder.slice();
  if (how === "dark") order.sort((a, b) => lum(a) - lum(b));
  else if (how === "light") order.sort((a, b) => lum(b) - lum(a));
  else if (how === "hue") {
    order.sort((a, b) => {
      const ka = hueKey(a);
      const kb = hueKey(b);
      return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2];
    });
  } else {
    for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
  }
  state.paletteOrder = order;
  showPalette();
}

// Where index i of k lands, as text, for the chosen basis.
function paletteAngle(i, k) {
  const v = k > 1 ? i / (k - 1) : 0;
  const deg = (x) => `${Math.round(x)}°`;
  if (representation() === "transition") {
    return `θ = ${deg(36 + 108 * v)} in image A's amplitude, φ = ${deg(180 * v)} in image B's phase`;
  }
  switch (basis()) {
    case "z":
      return `θ = ${deg(180 * v)}`;
    case "x":
      return `φ = ${deg(180 * v)}`;
    case "zx":
      return `θ = ${deg(36 + 108 * v)} in the amplitude, φ = ${deg(36 + 108 * v)} in the phase`;
    case "sphere1":
      return `point ${i + 1} of ${k} on the sphere`;
    case "sphere2":
      return `with the slot's other pixel, one of ${k * k} points on the sphere`;
    default:
      return `θ = ${deg(36 + 108 * v)} in the amplitude, φ = ${deg(36 + 288 * v)} in the phase`;
  }
}

function showPalette() {
  const panel = $("palette-panel");
  const colours = imageColours();
  panel.hidden = !usesPalette() || colours.length === 0;
  if (panel.hidden) return;
  // Keep the user's order while the colours stay the same; start dark to light otherwise.
  const same =
    state.paletteOrder &&
    state.paletteOrder.length === colours.length &&
    colours.every((c) => state.paletteOrder.includes(c));
  if (!same) state.paletteOrder = colours.slice().sort((a, b) => lum(a) - lum(b));

  const k = state.paletteOrder.length;
  const strip = $("palette-strip");
  strip.innerHTML = "";
  // One line along the axis while indexes can stay 5 px or wider; wrap past that.
  const fit = Math.floor(((strip.clientWidth || 800) - 2 * (k - 1)) / k);
  const oneLine = fit >= 5;
  const size = oneLine ? Math.min(32, fit) : 8;
  strip.style.flexWrap = oneLine ? "nowrap" : "wrap";
  strip.style.justifyContent = oneLine && k > 1 ? "space-between" : "flex-start";
  state.paletteOrder.forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "index";
    el.style.cssText = `width:${size}px;height:${size}px;background:${c === CLEAR ? "none" : c}`;
    if (c === CLEAR) el.classList.add("clear");
    el.textContent = size >= 24 ? i : "";
    el.title = `Index ${i}: ${c}, ${paletteAngle(i, k)}. Drag to move.`;
    el.draggable = true;
    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", String(i));
      el.classList.add("dragging");
    });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
    el.addEventListener("dragover", (e) => {
      e.preventDefault();
      el.classList.add("target");
    });
    el.addEventListener("dragleave", () => el.classList.remove("target"));
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      const from = Number(e.dataTransfer.getData("text/plain"));
      const order = state.paletteOrder;
      order.splice(i, 0, order.splice(from, 1)[0]);
      showPalette();
    });
    strip.appendChild(el);
  });
  const ends = {
    z: ["|0⟩", "|1⟩"],
    x: ["|+⟩", "|−⟩"],
    zx: ["", ""],
    zxy: ["", ""],
    sphere1: ["", ""],
    sphere2: ["", ""],
  }[basis()];
  $("axis-start").textContent = `Index 0: ${paletteAngle(0, k)} ${ends[0]}`.trim();
  $("axis-end").textContent = `Index ${k - 1}: ${paletteAngle(k - 1, k)} ${ends[1]}`.trim();
  const shared = paletteTransition()
    ? "Both images share this palette; a checkered index is a pixel that is transparent in only one of them. " +
      "Between 0° and 90° a pixel moves through the colours placed between its colour in A and its colour in B. "
    : "";
  const sphere = ["sphere1", "sphere2"].includes(basis()) && !paletteTransition();
  $("palette-info").textContent = sphere
    ? `${k} colours. Each colour (or pair of colours, two per slot) is a point on the sphere of qubit states, ` +
      `in this order. Noise moves a pixel to a nearby point, which is not always a nearby index. ` +
      `Drag a index to move it.`
    : `${k} colours. ${shared}Each pixel stores its colour's index, from the first index at one end ` +
      `of the qubit's axis to the last at the other. Noise moves a pixel to a nearby index, ` +
      `so colours placed next to each other are the ones most often mistaken for each other. ` +
      `Drag a index to move it.`;
}

async function loadFile(file, slot) {
  const bitmap = await createImageBitmap(file);
  if (bitmap.width > 256 || bitmap.height > 256) {
    alert("Use pixel art of at most 256 x 256 pixels.");
    return;
  }
  const c = document.createElement("canvas");
  c.width = bitmap.width;
  c.height = bitmap.height;
  const ctx = c.getContext("2d");
  ctx.drawImage(bitmap, 0, 0);
  const data = ctx.getImageData(0, 0, c.width, c.height).data;
  state.images[slot] = { width: c.width, height: c.height, data };
  checkFit();
  showPalette();
}

function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function buildRequest() {
  const imgs = representation() === "transition" ? state.images : [state.images[0]];
  return {
    device: device(),
    representation: representation(),
    basis: basis(),
    shots: Number($("shots").value),
    alphas: Number($("alphas").value),
    colorspace: $("colorspace").value,
    palette_order: usesPalette() ? state.paletteOrder : undefined,
    transition_values: $("transition-values").value,
    noise: target() === "sim-ideal" ? "none" : "calibrated",
    mapping: $("mapping").value,
    seed: Number($("seed").value),
    images: imgs.map((im) => ({ width: im.width, height: im.height, rgba: toBase64(new Uint8Array(im.data.buffer)) })),
  };
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => ({ detail: res.statusText }));
  if (!res.ok) throw new Error(json.detail || res.statusText);
  return json;
}

function setStatus(text, cls = "") {
  $("status").textContent = text;
  $("status").className = "note " + cls;
}

// One request per job. A single job sends the request unchanged.
function jobRequests(request, split) {
  if (split.jobs === 1) return [request];
  const { images, ...base } = request;
  const imgs = representation() === "transition" ? state.images : [state.images[0]];
  return Array.from({ length: split.jobs }, (_, j) => Split.request(base, imgs, split.owner, j, split.jobs));
}

async function run() {
  if (!checkFit()) return;
  const request = buildRequest();
  const split = jobPlan();
  const requests = jobRequests(request, split);
  const jobs = requests.length;
  const label = (j) => (jobs > 1 ? ` ${j + 1} of ${jobs}` : "");
  state.request = jobs > 1 ? { ...request, jobs } : request;
  state.jobs = { requests, owner: split.owner, tickets: [], results: [] };
  $("run").disabled = true;
  try {
    if (target() === "ibm") {
      state.credentials = { token: $("token").value, instance: $("instance").value };
      for (let j = 0; j < jobs; j++) {
        setStatus(`Encoding and submitting job${label(j)} to IBM...`);
        try {
          state.jobs.tickets.push(await post("/api/ibm/submit", { request: requests[j], credentials: state.credentials }));
        } catch (err) {
          const sent = state.jobs.tickets.map((t) => t.job_id);
          throw new Error(
            `Job${label(j)} was not submitted: ${err.message}` +
              (sent.length ? ` These jobs were already submitted: ${sent.join(", ")}.` : "")
          );
        }
      }
      const ids = state.jobs.tickets.map((t) => t.job_id).join(", ");
      setStatus(`Submitted to ${state.jobs.tickets[0].backend}: ${ids}. Checking every 30 s...`);
      state.timer = setInterval(poll, 30000);
      poll();
      return;
    }
    const t0 = performance.now();
    for (let j = 0; j < jobs; j++) {
      setStatus(`Simulating${jobs > 1 ? ` job${label(j)}` : ""}...`);
      try {
        state.jobs.results.push(await post("/api/simulate", requests[j]));
      } catch (err) {
        throw new Error(`Job${label(j)} failed: ${err.message}`);
      }
    }
    const jobsText = jobs > 1 ? ` (${jobs} jobs)` : "";
    setStatus(`Done in ${((performance.now() - t0) / 1000).toFixed(1)} s${jobsText}.`, "good");
    show(Split.join(state.jobs.results, state.jobs.owner), request.device);
  } catch (err) {
    setStatus(err.message, "bad");
  } finally {
    checkFit();
  }
}

// Ask about every job that is not done yet; join the results once all are done.
async function poll() {
  if (state.polling) return; // the last check of many jobs is still running
  state.polling = true;
  try {
    await pollOnce();
  } finally {
    state.polling = false;
  }
}

async function pollOnce() {
  const { requests, tickets, results } = state.jobs;
  const jobs = tickets.length;
  const name = (j) => `Job ${tickets[j].job_id}${jobs > 1 ? ` (${j + 1} of ${jobs})` : ""}`;
  let last = "";
  try {
    for (let j = 0; j < jobs; j++) {
      if (results[j]) continue;
      const out = await post("/api/ibm/status", { request: requests[j], credentials: state.credentials, ticket: tickets[j] });
      if (out.status === "DONE") results[j] = out;
      else if (out.status === "ERROR" || out.status === "CANCELLED") {
        stopPolling();
        setStatus(`${name(j)}: ${out.status}. ${out.error || ""}`, "bad");
        return;
      } else last = `${name(j)} on ${tickets[j].backend}: ${out.status}.`;
    }
    const done = results.filter(Boolean).length;
    if (done === jobs) {
      stopPolling();
      setStatus(jobs > 1 ? `All ${jobs} jobs are done.` : `Job ${tickets[0].job_id} is done.`, "good");
      show(Split.join(results, state.jobs.owner), state.request.device);
    } else {
      setStatus(`${jobs > 1 ? `${done} of ${jobs} jobs done. ` : ""}${last} Checking again in 30 s.`);
    }
  } catch (err) {
    stopPolling();
    setStatus(err.message, "bad");
  }
}

function stopPolling() {
  clearInterval(state.timer);
  state.timer = null;
  checkFit();
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------
function scaleFor(w, h) {
  return Math.max(2, Math.floor(Math.min(320 / w, 320 / h)));
}

function rgbaFromBase64(b64, w, h) {
  const bin = atob(b64);
  const bytes = new Uint8ClampedArray(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new ImageData(bytes, w, h);
}

function blit(ctx, imageData, x0, scale) {
  const tmp = document.createElement("canvas");
  tmp.width = imageData.width;
  tmp.height = imageData.height;
  tmp.getContext("2d").putImageData(imageData, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(tmp, x0, 0, imageData.width * scale, imageData.height * scale);
}

function show(result, dev) {
  state.result = result;
  state.resultDevice = dev;
  state.hoverGroup = -1;
  state.frame = 0;
  state.layer = 0;
  $("results").hidden = false;
  $("layer-row").hidden = !result.channels;
  $("notes").hidden = !result.notes;
  $("notes").textContent = (result.notes || []).join(" ");
  $("layer-choices").innerHTML = (result.channels || [])
    .map((c, k) => `<label><input type="radio" name="layer" value="${k}"${k ? "" : " checked"}> ${c}</label>`)
    .join("");
  document.querySelectorAll("input[name=layer]").forEach((el) =>
    el.addEventListener("change", () => {
      state.layer = Number(el.value);
      state.hoverGroup = -1;
      drawOutput();
      drawChip();
    })
  );
  const two = result.representation === "transition";
  $("alpha-row").hidden = !two;
  $("dl-transition").hidden = !two;
  if (two) {
    $("alpha").max = result.frames.length - 1;
    $("alpha").value = 0;
  }
  drawInput();
  drawOutput();
  drawChip();
  const m = { ...result.metrics, colorspace: result.colorspace, mapping: result.mapping, pixels: result.n_pixels, slots_used: result.slots_used, capacity: result.capacity, ...result.run };
  if (result.drr.methods) m.drr_method_per_job = result.drr.methods.join(", ");
  const fmt = (v) => (typeof v === "number" && !Number.isInteger(v) ? v.toFixed(3) : v);
  const scores = Object.entries(result.drr.scores)
    .map(([k, v]) => `${k} ${v.toFixed(2)}`)
    .join(", ");
  $("metrics").textContent =
    Object.entries(m)
      .map(([k, v]) => `${k}: ${fmt(v)}`)
      .join("\n") +
    `\n\nbest DRR: ${result.drr.method}\n` +
    Object.entries(result.drr.metrics)
      .map(([k, v]) => `  ${k}: ${fmt(v)}`)
      .join("\n") +
    `\nDRR scores, lower is better: ${scores}`;
}

function drawInput() {
  const r = state.result;
  const imgs = r.representation === "transition" ? state.images : [state.images[0]];
  const s = scaleFor(r.width * imgs.length, r.height);
  const c = $("input-canvas");
  c.width = (r.width * imgs.length + (imgs.length - 1)) * s;
  c.height = r.height * s;
  const ctx = c.getContext("2d");
  imgs.forEach((im, k) => blit(ctx, new ImageData(new Uint8ClampedArray(im.data), im.width, im.height), k * (r.width + 1) * s, s));
  $("input-caption").textContent =
    r.representation === "transition" ? "Input: image A (amplitude) and image B (phase)" : "Input";
}

function decodedFrame(r, frame, version = "raw") {
  const src = version === "drr" ? r.drr : r;
  return rgbaFromBase64(r.representation === "transition" ? src.frames[frame] : src.image, r.width, r.height);
}

// Draw the read-back image at `s` screen pixels per image pixel, with the hovered
// data qubit's pixels lit and, if asked, an outline around each data qubit's pixels.
function paintResult(ctx, s, frame, { highlight = -1, outlines = false, version = "raw" } = {}) {
  const r = state.result;
  blit(ctx, decodedFrame(r, frame, version), 0, s);
  const pg = layerOf(r, "pixel_group");
  const w = r.width;
  const h = r.height;
  if (highlight >= 0) {
    ctx.fillStyle = "rgba(255, 196, 0, 0.45)";
    for (let i = 0; i < pg.length; i++) if (pg[i] === highlight) ctx.fillRect((i % w) * s, Math.floor(i / w) * s, s, s);
  }
  if (!outlines) return;
  const at = (x, y) => (x < 0 || y < 0 || x >= w || y >= h ? -1 : pg[y * w + x]);
  ctx.strokeStyle = "rgba(230, 40, 40, 0.6)";
  ctx.lineWidth = Math.max(1, Math.round(s / 8));
  ctx.beginPath();
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const g = at(x, y);
      if (g !== at(x + 1, y) && (g >= 0 || at(x + 1, y) >= 0)) {
        ctx.moveTo((x + 1) * s, y * s);
        ctx.lineTo((x + 1) * s, (y + 1) * s);
      }
      if (g !== at(x, y + 1) && (g >= 0 || at(x, y + 1) >= 0)) {
        ctx.moveTo(x * s, (y + 1) * s);
        ctx.lineTo((x + 1) * s, (y + 1) * s);
      }
      if (x === 0 && g >= 0) {
        ctx.moveTo(0, y * s);
        ctx.lineTo(0, (y + 1) * s);
      }
      if (y === 0 && g >= 0) {
        ctx.moveTo(x * s, 0);
        ctx.lineTo((x + 1) * s, 0);
      }
    }
  }
  ctx.stroke();
}

function drawOutput() {
  const r = state.result;
  const s = scaleFor(r.width, r.height);
  const c = $("output-canvas");
  c.width = r.width * s;
  c.height = r.height * s;
  const look = { highlight: state.hoverGroup, outlines: $("outlines").checked };
  paintResult(c.getContext("2d"), s, state.frame, look);
  const d = $("drr-canvas");
  d.width = c.width;
  d.height = c.height;
  paintResult(d.getContext("2d"), s, state.frame, { ...look, version: "drr" });
  const sc = r.drr.scores;
  // A constellation scores the fraction of wrong pixels; every other mode a mean error.
  const score =
    r.symbol_scheme === "constellation"
      ? (x) => `${(100 * x).toFixed(1)}% of pixels wrong`
      : (x) => `${x.toFixed(1)}${r.representation === "palette" ? " palette steps" : " out of 255"}`;
  $("drr-caption").textContent =
    `Best dynamic range recovery: ${r.drr.method} ` + `(error ${score(sc[r.drr.method])}, raw ${score(sc.none)})`;
  if (r.representation === "transition") {
    const a = r.alphas_degrees[state.frame];
    $("alpha-label").textContent = `${a.toFixed(1)}° from Z toward X`;
    $("output-caption").textContent = `Raw read-back along an axis ${a.toFixed(1)}° from Z (0° is image A, 90° is image B)`;
  } else {
    $("output-caption").textContent = `Raw read-back (${BASIS_LABEL[r.basis]}, ${r.mapping})`;
  }
}

// ---------------------------------------------------------------------------
// Downloads
// ---------------------------------------------------------------------------
function fileStem() {
  const r = state.result;
  const run = r.run || {};
  return ["tessa", r.representation, r.colorspace, r.basis, r.mapping, run.device || state.resultDevice, run.kind === "hardware" ? "hw" : run.noise]
    .concat($("version").value === "drr" ? [`drr-${r.drr.method.replace(" ", "-")}`] : [])
    .filter(Boolean)
    .join("_");
}

function renderCanvas(frame, scale, outlines) {
  const r = state.result;
  const c = document.createElement("canvas");
  c.width = r.width * scale;
  c.height = r.height * scale;
  paintResult(c.getContext("2d"), scale, frame, { outlines, version: $("version").value });
  return c;
}

const pngBlob = (canvas) => new Promise((done) => canvas.toBlob(done, "image/png"));
const downloadScale = () => Number($("scale").value);

async function downloadPng(outlines) {
  // Outlines need room between pixels: never draw them at 1x.
  const scale = outlines ? Math.max(4, downloadScale()) : downloadScale();
  const frame = state.frame;
  const suffix = state.result.representation === "transition" ? `_${angleName(frame)}` : "";
  const r = state.result;
  const outlined = outlines ? `_outlined${r.channels ? "_" + r.channels[state.layer] : ""}` : "";
  Export.save(await pngBlob(renderCanvas(frame, scale, outlines)), `${fileStem()}${suffix}${outlined}.png`);
}

function angleName(frame) {
  return `${state.result.alphas_degrees[frame].toFixed(1).replace(".", "p")}deg`;
}

async function downloadZip() {
  const r = state.result;
  const files = [];
  for (let k = 0; k < r.frames.length; k++) {
    const blob = await pngBlob(renderCanvas(k, downloadScale(), false));
    files.push({ name: `${fileStem()}_${String(k).padStart(2, "0")}_${angleName(k)}.png`, bytes: new Uint8Array(await blob.arrayBuffer()) });
  }
  Export.save(Export.zip(files), `${fileStem()}_frames.zip`);
}

function downloadGif() {
  const r = state.result;
  const scale = downloadScale();
  let order = r.frames.map((_, k) => k);
  if ($("gif-bounce").checked) order = order.concat(order.slice(1, -1).reverse());
  const frames = order.map((k) => renderCanvas(k, scale, false).getContext("2d").getImageData(0, 0, r.width * scale, r.height * scale));
  Export.save(Export.gif(frames, Number($("gif-delay").value)), `${fileStem()}.gif`);
}

function downloadJson() {
  const r = state.result;
  const { images, ...settings } = state.request || {};
  const data = {
    settings,
    run: r.run,
    metrics: r.metrics,
    width: r.width,
    height: r.height,
    n_pixels: r.n_pixels,
    capacity: r.capacity,
    slots_used: r.slots_used,
    mapping: r.mapping,
    colorspace: r.colorspace,
    channels: r.channels,
    groups: r.groups,
    pixel_group: r.pixel_group,
    pixel_address: r.pixel_address,
    pixel_part: r.pixel_part,
    jobs: r.jobs,
    pixel_job: r.pixel_job,
    palette: r.palette,
    drr: { method: r.drr.method, methods: r.drr.methods, metrics: r.drr.metrics, scores: r.drr.scores },
    alphas_degrees: r.alphas_degrees,
  };
  Export.save(new Blob([JSON.stringify(data, null, 1)], { type: "application/json" }), `${fileStem()}.json`);
}

function chipGeometry() {
  const d = state.devices[state.resultDevice];
  const xs = d.coords.map((c) => c[0]);
  const ys = d.coords.map((c) => c[1]);
  const step = Math.floor(Math.min(320 / (Math.max(...xs) + 1), 340 / (Math.max(...ys) + 1)));
  return { d, step, pos: (q) => [(d.coords[q][0] + 0.5) * step, (d.coords[q][1] + 0.5) * step] };
}

function drawChip() {
  const r = state.result;
  const { d, step, pos } = chipGeometry();
  const c = $("chip-canvas");
  c.width = (Math.max(...d.coords.map((p) => p[0])) + 1) * step;
  c.height = (Math.max(...d.coords.map((p) => p[1])) + 1) * step;
  const ctx = c.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const muted = css.getPropertyValue("--muted").trim();
  const accent = css.getPropertyValue("--accent").trim();
  const line = css.getPropertyValue("--line").trim();
  const hot = "rgb(230, 150, 0)";

  ctx.strokeStyle = line;
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (const [a, b] of d.edges) {
    ctx.moveTo(...pos(a));
    ctx.lineTo(...pos(b));
  }
  ctx.stroke();

  const used = new Set(r.pixel_group.filter((g) => g >= 0)); // any channel
  const data = new Set(r.groups.map((g) => g.data));
  const hover = state.hoverGroup >= 0 ? r.groups[state.hoverGroup] : null;
  const hotAddr = new Set(hover ? hover.addresses : []);
  for (let q = 0; q < d.n_qubits; q++) {
    const [x, y] = pos(q);
    if (data.has(q)) continue;
    const half = step * 0.18;
    ctx.fillStyle = hotAddr.has(q) ? hot : muted;
    ctx.fillRect(x - half, y - half, 2 * half, 2 * half);
  }
  r.groups.forEach((g, k) => {
    const [x, y] = pos(g.data);
    ctx.beginPath();
    ctx.arc(x, y, step * 0.3, 0, 2 * Math.PI);
    ctx.fillStyle = k === state.hoverGroup ? hot : used.has(k) ? accent : line;
    ctx.fill();
  });
}

function hoverText(i) {
  const r = state.result;
  if (layerOf(r, "pixel_group", 0)[i] < 0) return "Transparent pixel: not encoded.";
  const where = (layer) => {
    const g = layerOf(r, "pixel_group", layer)[i];
    if (g < 0) return "nowhere: this pixel is stored in grayscale";
    const grp = r.groups[g];
    const a = layerOf(r, "pixel_address", layer)[i];
    const bits = grp.addresses.map((q, j) => `q${q}=${(a >> j) & 1}`).join(", ");
    const p = layerOf(r, "pixel_part", layer)[i];
    const part = r.symbol_scheme === "constellation" ? SPHERE_PART[r.basis][p] : PART[p];
    return `data qubit q${grp.data} (${grp.n_values} slots), in the slot where its address qubits read ${bits}, in the ${part}`;
  };
  const job = r.pixel_job ? `, in job ${r.pixel_job[i] + 1} of ${r.jobs},` : "";
  const at = `Pixel (${i % r.width}, ${Math.floor(i / r.width)})${job}`;
  if (!r.channels) return `${at} lives on ${where(0)}.`;
  if (layerOf(r, "pixel_group", 1)[i] < 0) return `${at} is stored in grayscale: its brightness is on ${where(0)}.`;
  return `${at}: ` + r.channels.map((c, k) => `${c} on ${where(k)}`).join("; ") + ".";
}

function setHover(group, text) {
  if (group !== state.hoverGroup) {
    state.hoverGroup = group;
    drawOutput();
    drawChip();
  }
  $("hover").textContent = text;
}

function onOutputHover(ev) {
  const r = state.result;
  if (!r) return;
  const c = ev.currentTarget;
  const rect = c.getBoundingClientRect();
  const x = Math.floor(((ev.clientX - rect.left) / rect.width) * r.width);
  const y = Math.floor(((ev.clientY - rect.top) / rect.height) * r.height);
  if (x < 0 || y < 0 || x >= r.width || y >= r.height) return;
  const i = y * r.width + x;
  setHover(layerOf(r, "pixel_group")[i], hoverText(i));
}

function onChipHover(ev) {
  const r = state.result;
  if (!r) return;
  const { step, pos } = chipGeometry();
  const c = $("chip-canvas");
  const rect = c.getBoundingClientRect();
  const x = ((ev.clientX - rect.left) / rect.width) * c.width;
  const y = ((ev.clientY - rect.top) / rect.height) * c.height;
  let best = -1;
  r.groups.forEach((g, k) => {
    const [gx, gy] = pos(g.data);
    if (Math.hypot(gx - x, gy - y) < step * 0.4) best = k;
  });
  if (best < 0) return;
  const g = r.groups[best];
  const count = (layer) => layerOf(r, "pixel_group", layer).filter((v) => v === best).length;
  const held = r.channels
    ? r.channels.map((c, k) => `${count(k)} ${c}`).join(", ") + " values"
    : `${count(0)} pixel${count(0) === 1 ? "" : "s"}`;
  const across = r.jobs ? `, across ${r.jobs} jobs` : "";
  setHover(
    best,
    `Data qubit q${g.data} with address qubits ${g.addresses.map((q) => "q" + q).join(", ")}: ` +
      `${g.n_values} slots, holding ${held} of this image${across}.`
  );
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
async function init() {
  document.querySelectorAll("input[name=device]").forEach((el) => el.addEventListener("change", () => (showDevice(), checkFit())));
  document.querySelectorAll("input[name=target]").forEach((el) => el.addEventListener("change", checkFit));
  $("representation").addEventListener("change", showBasis);
  $("basis").addEventListener("change", () => {
    $("basis-info").textContent = BASIS_INFO[representation()][basis()] + (representation() === "color" ? COLOR_INFO : "");
    showPalette();
    checkFit();
  });
  $("file-a").addEventListener("change", (e) => e.target.files[0] && loadFile(e.target.files[0], 0));
  $("file-b").addEventListener("change", (e) => e.target.files[0] && loadFile(e.target.files[0], 1));
  $("run").addEventListener("click", run);
  $("transition-values").addEventListener("change", showPalette);
  document.querySelectorAll("button[data-order]").forEach((el) =>
    el.addEventListener("click", () => sortPalette(el.dataset.order))
  );
  const showMapping = () => {
    const m = $("mapping").value;
    $("mapping-info").textContent = MAPPING_INFO[m];
    $("seed-box").hidden = !["patches", "random"].includes(m);
  };
  $("mapping").addEventListener("change", showMapping);
  showMapping();
  const guard = (fn) => async () => {
    try {
      await fn();
    } catch (err) {
      setStatus(`Download failed: ${err.message}`, "bad");
    }
  };
  $("dl-png").addEventListener("click", guard(() => downloadPng(false)));
  $("dl-outlined").addEventListener("click", guard(() => downloadPng(true)));
  $("dl-json").addEventListener("click", guard(downloadJson));
  $("dl-frame").addEventListener("click", guard(() => downloadPng(false)));
  $("dl-zip").addEventListener("click", guard(downloadZip));
  $("dl-gif").addEventListener("click", guard(downloadGif));
  $("outlines").addEventListener("change", () => state.result && drawOutput());
  $("alpha").addEventListener("input", (e) => {
    state.frame = Number(e.target.value);
    drawOutput();
  });
  $("output-canvas").addEventListener("mousemove", onOutputHover);
  $("drr-canvas").addEventListener("mousemove", onOutputHover);
  $("chip-canvas").addEventListener("mousemove", onChipHover);
  showBasis();
  try {
    const res = await fetch("/api/devices");
    if (!res.ok) throw new Error(res.statusText);
    state.devices = await res.json();
    showDevice();
    checkFit();
  } catch (err) {
    setStatus(`Could not load the chips: ${err.message}`, "bad");
  }
}

init();
