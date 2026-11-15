# TESSA web app

TESSA encodes pixel art on an IBM chip and reads it back. It runs on Vercel. A
FastAPI backend does the quantum work, and a static page in `public/` is the interface.

## What a user can do

1. Upload pixel art (PNG, at most 256 x 256). Transparent pixels (alpha below 128) are
   not encoded, and the result keeps the shape of the sprite.
2. Choose a chip: `ibm_phoenix` (120 qubits, 792 slots) or `ibm_fez` (156 qubits, 448
   slots). A slot is one address state of one data qubit.
3. Choose what to encode, and in which basis:

   | Encode | Basis | Pixels per slot |
   |---|---|---|
   | Grayscale, or palette (the image's own colours, sorted dark to light) | Z (amplitude), X (phase) | 1 |
   | Grayscale, or palette | Z + X, Z + X + Y (one pixel in the amplitude, one in the phase) | 2 |
   | Palette | Constellation (Z + X + Y): each colour is a point on the sphere of qubit states | 1 |
   | Palette | Constellation, two per slot (Z + X + Y): each pair of colours is a point | 2 |
   | Full colour: RGB, HSV, HSL or YCbCr (three numbers per pixel) | Z, X | 1/3 |
   | Full colour | Z + X, Z + X + Y | 2/3 |
   | Hue + value (saturation kept from the input) | Z + X (hue on half a turn), Z + X + Y (hue on the full turn) | 1 |
   | Transition between two images, in grayscale or with a shared palette | A in the amplitude, B in the phase, read at angles from Z to X | 1 |

   A palette is a set of symbols, so the decoder snaps to the nearest allowed
   state (`iqpixl.symbols`). In Z + X it snaps the amplitude to its colour first,
   then reads the phase with that colour's exact angle. A constellation spreads the
   `k` colours (or `k * k` pairs) evenly over the whole sphere and picks the nearest
   point from the Z, X and Y readouts. The points sit further apart than levels on
   one axis, so fewer shots give exact colours: 20 colours, two per slot, come back
   about 97% exact at 8192 shots without noise, against about 70% in Z + X. A
   constellation takes at most 4096 points: 4096 colours one per slot, 64 two per
   slot.

   Two pixels per slot let a 39 x 39 grayscale image fit on `ibm_phoenix`. Full colour
   in Z + X fits 528 pixels there, for example 22 x 22. If full colour is short of room
   by at most 1% of the pixels (at least 2), the last pixels in reading order (bottom
   right) are stored in grayscale, at one number each, and the page says so. A fully
   opaque 23 x 23 image fits that way, with 2 grayscale pixels. The three channels are encoded as three stacked images, so the mapping lays
   out each channel like a picture, and the page shows one qubit map per channel.

   If an image is too big for the chip, it runs in parts, and the page shows a
   warning with the number of parts. `tessa/split.py` cuts the opaque pixels, in
   reading order, into bands of nearly equal size that each fit on the chip. The
   simulator runs the parts one after the other, one request each, and the page
   joins the results. On IBM hardware, every part goes into one job, so the parts
   wait in the queue once and run under one calibration. The backend then joins the
   results. Every part uses the whole image's palette order, so each colour has the
   same place on the qubit's axis in every part. Each part picks its own best DRR
   method. If the methods differ, the DRR result shows "per part". Many parts take a
   long time, and a large number can time out or crash the page. The upload limit of
   256 x 256 pixels does not change.
   For a palette, a "Palette order" panel shows the image's colours along the qubit's
   axis: the first colour is stored at value 0 (the |0> end), the last at value 1.
   Drag a swatch to move it, or sort dark to light, light to dark, by hue, or at
   random. Noise moves a pixel to a nearby index, so neighbouring colours are the ones
   most often mistaken for each other. The order goes with the request, so a hardware
   job decodes with the same order. A palette transition orders one palette made from
   both images. A pixel that is transparent in only one image is a palette entry of
   its own, so the sprite's shape can change too. Between 0 and 90 degrees, a pixel
   moves through the colours placed between its colour in A and its colour in B.
4. Choose which pixels each data qubit holds:

   | Mapping | Each data qubit holds |
   |---|---|
   | Lines | a strip of pixels, row by row |
   | Squares | a square block, for example 4 x 4 for 16 slots |
   | Rectangles | a flat block, for example 2 x 8 for 16 slots |
   | Patches | a puzzle piece, grown at random from a seed |
   | Random | pixels scattered at random, from a seed |

   Squares, rectangles and patches fill the data qubits with the most slots first, so
   each data qubit gets a whole block. A transparent pixel inside a block moves the
   rest of the block along to the next pixels.
5. Run on the simulator, with the chip's calibrated noise or without noise. The
   simulator decodes one cell at a time with `halo="traced"`.
6. Run on IBM hardware. The user pastes an IBM Cloud API key and an instance CRN. The
   page submits one job that runs the whole circuit, then checks it every 30 seconds.
7. Compare the raw read-back with the best dynamic range recovery (DRR). Noise pulls
   values toward the middle of their range. DRR stretches them back with a few numbers
   from the input: min and max, two percentiles, mean and spread, the absolute maximum,
   or min, mean and max. The app tries every method and shows the one closest to the
   input, next to the raw result. For a palette constellation, "closest" means the
   fewest wrong pixels, and DRR acts on each axis of the Bloch vector. That choice needs the original image. Hue is never
   stretched, because it wraps around.
8. See where each pixel lives. Outlines show the pixels each data qubit holds. Hover
   over a pixel or a data qubit to see the qubit, its address qubits and the slot.
9. Download the raw result or the best DRR at 1x, 4x, 8x or 16x: as a PNG, as a PNG with outlines, and
   as JSON with the settings, the metrics and the pixel-to-qubit map. For a
   transition, you can also download the current angle as a PNG, every angle as a
   ZIP of PNGs, or an animated GIF. The GIF can play back and forth.

## Files

| Path | Contents |
|---|---|
| `app.py` | The FastAPI app. Vercel finds it with no configuration. |
| `public/` | The page: `index.html`, `app.js`, `export.js` (GIF and ZIP writers), `split.js` (joins the parts of a big image), `style.css`. |
| `tessa/pipeline.py` | Pixels to circuits, and decoded counts back to pixels. |
| `tessa/mapping.py` | Which pixels each data qubit holds. |
| `tessa/split.py` | Splits an image too big for the chip into bands, and joins their results. |
| `tessa/runners.py` | The simulator, and IBM job submission and status. |
| `tessa/devices.py` | The two chips, rebuilt from their calibration snapshots. |
| `tessa/devices/*.json` | Calibration snapshots of `ibm_fez` and `ibm_phoenix`. |
| `iqpixl/` | A copy of `../src/iqpixl`, made by `scripts/vendor_iqpixl.py`. |
| `scripts/` | The copy script, the snapshot script, and `hardware_package.py`, which sends a package of images to IBM hardware. |
| `tests/` | Tests of the pipeline and of the hardware path against a fake IBM service. |

## Run it locally

From the repository root, copy the library and start the server:

```bash
python webapp/scripts/vendor_iqpixl.py
cd webapp
uv run --no-project --python 3.12 --with-requirements requirements.txt --with uvicorn uvicorn app:app --reload
```

Then open http://127.0.0.1:8000.

## Deploy to Vercel

1. Run `python webapp/scripts/vendor_iqpixl.py` from the repository root. The app
   cannot read `../src` on Vercel, so it needs its own copy of the library.
2. Commit the copy in `webapp/iqpixl/`, or deploy from the command line.
3. Create a Vercel project with Root Directory set to `webapp`. Vercel detects FastAPI
   from `app.py` and serves `public/` from its CDN.
4. Deploy, for example with `vercel deploy` from inside `webapp/`.

`vercel.json` lets one request run for 300 seconds, the Hobby limit. A simulation
takes a few seconds per image. A transition takes longer, because each readout angle
is one more simulation. The Python bundle is about 200 MB, under the 500 MB limit.

One response from Vercel holds at most 4.5 MB. A part of a split simulation holds
only its own rows, so it stays small. An IBM result holds the whole image. A large
transition with many readout angles can pass 4.5 MB, and then the result does not
arrive. The page warns about this before you run. When you run the app on your own
computer, this limit does not apply. Transpiling 30 full parts for `ibm_phoenix`
takes less than one second, and decoding one part takes at most 0.2 seconds. The
time to download a large result from IBM is not measured.

## Send a package of images to IBM hardware

If one status check fails, the page stops checking the job. If you reload the page,
it loses the job. For a set of hardware runs, use `scripts/hardware_package.py`. This
script reads a package file, sends every image in it as one IBM job, and waits for
the result. It saves each step to disk. If the connection drops, it tries again. If
the computer stops, run the same command again, and the script continues.

1. Write a package file. `scripts/package_example.toml` shows the format. The
   settings are the page's options. The palette order is always dark to light.
2. Put the API key and the instance CRN in the package file, in an `[ibm]` table
   with `token` and `instance`. Do not commit a package file that holds a key. If
   the package has no `[ibm]` table, the script reads `QISKIT_IBM_TOKEN` and
   `IQPIXL_IBM_INSTANCE` from the environment. The copy of the package in the
   output folder has the key removed.
3. From `webapp/`, check the package. This step contacts nobody:

   ```bash
   uv run --no-project --python 3.12 --with-requirements requirements.txt --with pillow \
       python scripts/hardware_package.py check scripts/package_example.toml
   ```

4. Submit it and wait. Use the same command again to resume:

   ```bash
   uv run --no-project --python 3.12 --with-requirements requirements.txt --with pillow \
       python scripts/hardware_package.py run scripts/package_example.toml
   ```

The output goes to `../data/hardware/<name>/`, or to `--out`. The folder holds the
job ID, the transpiled circuits, the raw job result, and a log. It also holds IBM's
timestamps and usage, and the chip calibration at submission and at execution. For
each image, it holds the result JSON and the raw and DRR PNGs. `summary.csv` has one row per image with its settings
and its metrics. An image too big for the chip runs in parts, as on the page. Its
parts go into the same job, and its result is joined into one image. `check` and
`summary.csv` show the number of parts. If the job fails, `job/error.json` has IBM's error message. To
decode the saved result again without IBM, run `hardware_package.py decode` on the
folder.

## Refresh the calibration

The simulators use the snapshots in `tessa/devices/`. To update them, save an IBM
account first (`QiskitRuntimeService.save_account`), then run:

```bash
uv run python webapp/scripts/snapshot_devices.py
```

## Test

```bash
uv run pytest webapp/tests
```

If `webapp/iqpixl/` differs from `src/iqpixl/`, one test fails. Then run
`scripts/vendor_iqpixl.py` again.

## IBM credentials

The page sends the API key and the CRN to the backend with each submit and each status
check, over HTTPS. The backend does not store or log them. The page keeps them in
memory only. If you close the tab, they are gone. The backend never stores job data
either: the page sends the original request with the job ID, and the backend rebuilds
the encoding from it to decode the counts.
