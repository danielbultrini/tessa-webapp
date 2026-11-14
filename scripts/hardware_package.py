"""Send a package of TESSA images to IBM hardware as one job, and wait for the result.

A package file (TOML) lists the images and the settings, the same ones the page
offers. Every image's circuits go into one IBM job. Everything the run needs is
saved in an output folder as it happens, so a lost connection or a restart costs
nothing: run the same command again and it picks up where it stopped.

From ``webapp/``::

    uv run --no-project --python 3.12 --with-requirements requirements.txt \\
        --with pillow python scripts/hardware_package.py check package.toml
    uv run ... python scripts/hardware_package.py run package.toml
    uv run ... python scripts/hardware_package.py run package.toml      # again, to resume
    uv run ... python scripts/hardware_package.py decode ../data/hardware/<name>

``check`` builds every plan on the chip's calibration snapshot and contacts
nobody. ``run`` submits (once) and waits. ``decode`` decodes the saved raw result
again, offline. The API key and the instance CRN come from the package's
``[ibm]`` table (``token``, ``instance``). Without it, the key comes from
``QISKIT_IBM_TOKEN`` (or ``IQPIXL_IBM_TOKEN``), and the CRN from
``IQPIXL_IBM_INSTANCE`` (or the paper's default instance). The script never
writes the key to the output folder: its copy of the package has the key removed.

The output folder holds::

    manifest.json          state, job id, job tag, the chip graph, which circuits are whose
    package.toml           the package file as submitted; inputs/ holds its images
    requests/<label>.json  each image's full request, as the page would send it
    circuits.qpy           the transpiled circuits, in job order
    calibration_submit.json  the chip's calibration when the job was submitted
    log.txt                every step and every status change, with times
    job/result.json        the raw job result (qiskit_ibm_runtime.RuntimeDecoder reads it)
    job/metrics.json       IBM's timestamps and usage; job/calibration_run.json, the
                           calibration at execution time; job/error.json if it failed
    results/<label>/       result.json (what the page shows), raw and DRR PNGs
    summary.csv, summary.json  one row per image: settings and metrics, raw and DRR
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import tomllib
import uuid
from pathlib import Path

WEBAPP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEBAPP))  # the vendored iqpixl copy and tessa

import numpy as np  # noqa: E402
from tessa import devices  # noqa: E402
from tessa.pipeline import build_plan, shots_of  # noqa: E402
from tessa.runners import decode_hardware, hardware_circuits  # noqa: E402

from iqpixl import graph_from_coupling  # noqa: E402
from iqpixl.backends.base import counts_of  # noqa: E402
from iqpixl.backends.ibm import ibm_instance, make_service  # noqa: E402

DEFAULT_OUT = WEBAPP.parent / "data" / "hardware"
#: Settings a run may give; any of them may also sit at the top of the package.
RUN_KEYS = {
    "representation",
    "basis",
    "shots",
    "alphas",
    "colorspace",
    "transition_values",
    "mapping",
    "seed",
}
PACKAGE_KEYS = RUN_KEYS | {"name", "device", "runs", "ibm"}
IBM_KEYS = {"token", "instance"}
FINAL = ("DONE", "ERROR", "CANCELLED")


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def write_json(path: Path, data) -> None:
    """Write through a temporary file, so a power cut never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


class Log:
    def __init__(self, path: Path):
        self.path = path

    def __call__(self, message: str) -> None:
        line = f"{now()}  {message}"
        print(line, flush=True)
        with self.path.open("a") as f:
            f.write(line + "\n")


def transient(exc: BaseException) -> bool:
    """A network drop, a timeout or a server-side hiccup: worth trying again.
    A refused request (bad key, bad circuit) is not."""
    import requests

    seen = exc
    while seen is not None:
        if isinstance(
            seen, requests.exceptions.ConnectionError | requests.exceptions.Timeout
        ):
            return True
        if isinstance(seen, ConnectionError | TimeoutError):
            return True
        code = getattr(seen, "status_code", None)
        if code is not None and (code == -1 or code >= 500 or code in (408, 429)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def patiently(what: str, fn, log: Log, first: float = 30, cap: float = 600):
    """``fn()``, tried again with growing pauses for as long as the failure is
    transient. Any other failure is raised."""
    delay = first
    while True:
        try:
            return fn()
        except Exception as exc:
            if not transient(exc):
                raise
            log(
                f"{what} failed ({type(exc).__name__}: {exc}); trying again in {delay:.0f} s"
            )
            time.sleep(delay)
            delay = min(cap, delay * 2)


# ----------------------------------------------------------------------
# The package file
# ----------------------------------------------------------------------
def image_of(path: Path) -> dict:
    """A PNG as the page sends it: width, height and base64 RGBA bytes."""
    from PIL import Image

    with Image.open(path) as im:
        rgba = np.asarray(im.convert("RGBA"), dtype=np.uint8)
    h, w = rgba.shape[:2]
    return {"width": w, "height": h, "rgba": base64.b64encode(rgba.tobytes()).decode()}


def load_package(path: Path) -> tuple[dict, list[dict]]:
    """The package file -> (its top level, one entry per run with its request)."""
    package = tomllib.loads(path.read_text())
    unknown = set(package) - PACKAGE_KEYS
    if unknown:
        raise ValueError(
            f"unknown package keys {sorted(unknown)}; use {sorted(PACKAGE_KEYS)}"
        )
    if "name" not in package:
        raise ValueError("give the package a name")
    unknown = set(package.get("ibm", {})) - IBM_KEYS
    if unknown:
        raise ValueError(f"unknown [ibm] keys {sorted(unknown)}; use {sorted(IBM_KEYS)}")
    device = package.get("device", "phoenix")
    if device not in devices.DEVICES:
        raise ValueError(
            f"unknown device {device!r}; choose from {sorted(devices.DEVICES)}"
        )
    runs = package.get("runs", [])
    if not runs:
        raise ValueError("the package has no [[runs]]")
    defaults = {k: v for k, v in package.items() if k in RUN_KEYS}

    entries, labels = [], set()
    for i, run in enumerate(runs):
        unknown = set(run) - RUN_KEYS - {"label", "image", "images"}
        if unknown:
            raise ValueError(f"run {i}: unknown keys {sorted(unknown)}")
        label = run.get("label") or f"run{i:02d}"
        if label in labels or not label.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"run {i}: labels must be unique, letters, digits, _ and -")
        labels.add(label)
        files = run.get("images") or ([run["image"]] if "image" in run else [])
        if not files:
            raise ValueError(
                f"run {label!r}: give an image (or images, for a transition)"
            )
        files = [(path.parent / f).resolve() for f in files]
        request = {**defaults, **{k: v for k, v in run.items() if k in RUN_KEYS}}
        request.update(device=device, images=[image_of(f) for f in files])
        entries.append(
            {"label": label, "files": [str(f) for f in files], "request": request}
        )
    return package, entries


def connect(package: dict | None):
    """A function that opens the IBM service with the package's ``[ibm]`` table;
    a key it does not give falls back to the environment."""
    ibm = (package or {}).get("ibm", {})
    return lambda: make_service(token=ibm.get("token"), instance=ibm.get("instance"))


def copy_without_key(source: Path, target: Path, package: dict) -> None:
    """Copy the package file with the API key replaced, so the output folder can be
    shared. If the key still shows anywhere, keep no copy at all."""
    text = re.sub(
        r"^(\s*token\s*=\s*).*$", r'\1"<removed>"', source.read_text(), flags=re.M
    )
    token = package.get("ibm", {}).get("token")
    if token and token in text:
        return
    target.write_text(text)


def plans_on(graph, entries: list[dict]) -> list:
    out = []
    for e in entries:
        try:
            out.append(build_plan(e["request"], graph))
        except ValueError as exc:
            raise ValueError(f"run {e['label']!r}: {exc}") from exc
    return out


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------
def check(args) -> None:
    package, entries = load_package(Path(args.package))
    device = package.get("device", "phoenix")
    graph = devices.graph(device)
    plans = plans_on(graph, entries)
    print(
        f"{package['name']}: {len(entries)} runs on {devices.DEVICES[device]} "
        f"(calibration snapshot, {graph.capacity} slots)"
    )
    print(
        f"{'label':24} {'encode':22} {'pixels':>6} {'slots':>5} {'circuits':>8} {'shots':>6}"
    )
    total = 0
    for e, plan in zip(entries, plans, strict=True):
        r = e["request"]
        what = f"{plan.representation} {plan.basis}"
        if plan.representation == "transition":
            what += f" {r.get('transition_values', 'gray')}"
        n = len(hardware_circuits(plan))
        total += n
        print(
            f"{e['label']:24} {what:22} {int(plan.mask.sum()):>6} "
            f"{np.unique(plan.slot).size:>5} {n:>8} {shots_of(r):>6}"
        )
    print(f"{total} circuits in one job. Nothing was sent.")


# ----------------------------------------------------------------------
# run: prepare, submit, wait, fetch, decode
# ----------------------------------------------------------------------
def fingerprint(entries: list[dict]) -> str:
    requests = [e["request"] for e in entries]
    return hashlib.sha256(json.dumps(requests, sort_keys=True).encode()).hexdigest()


def prepare(
    folder: Path, package_path: Path, package: dict, entries: list[dict], log: Log
):
    """Build and transpile every circuit on the live chip, and save it all."""
    from qiskit import qpy, transpile

    device = package.get("device", "phoenix")
    service = patiently("connecting to IBM", connect(package), log)
    backend = patiently(
        "fetching the backend", lambda: service.backend(devices.DEVICES[device]), log
    )
    graph = graph_from_coupling(backend.coupling_map.get_edges(), backend.num_qubits)
    plans = plans_on(graph, entries)

    circuits, runs = [], []
    for e, plan in zip(entries, plans, strict=True):
        own = hardware_circuits(plan)
        request_file = f"requests/{e['label']}.json"
        write_json(folder / request_file, e["request"])
        runs.append(
            {
                "label": e["label"],
                "images": [Path(f).name for f in e["files"]],
                "request_file": request_file,
                "first": len(circuits),
                "n_circuits": len(own),
                "shots": shots_of(e["request"]),
            }
        )
        circuits += own
    log(f"transpiling {len(circuits)} circuits for {backend.name}")
    transpiled = transpile(
        circuits,
        backend,
        initial_layout=list(range(graph.n_qubits)),
        optimization_level=1,
        seed_transpiler=0,
        num_processes=1,
    )
    with (folder / "circuits.qpy").open("wb") as f:
        qpy.dump(transpiled, f)

    copy_without_key(package_path, folder / "package.toml", package)
    for e in entries:
        for f in e["files"]:
            (folder / "inputs").mkdir(exist_ok=True)
            shutil.copy(f, folder / "inputs" / Path(f).name)
    try:
        write_json(folder / "calibration_submit.json", backend.properties().to_dict())
    except Exception as exc:  # the calibration is a record, never a reason to stop
        log(f"could not save the calibration ({type(exc).__name__}: {exc})")

    manifest = {
        "package": package["name"],
        "fingerprint": fingerprint(entries),
        "tag": f"tessa-{package['name']}-{uuid.uuid4().hex[:8]}",
        "created": now(),
        "state": "prepared",
        "device": device,
        "backend": backend.name,
        "instance": ibm_instance(package.get("ibm", {}).get("instance")),
        "job_id": None,
        "submitted": None,
        "finished": None,
        "status": None,
        "n_circuits": len(transpiled),
        "depths": [c.depth() for c in transpiled],
        "edges": [list(e) for e in sorted(graph.edges)],
        "n_qubits": graph.n_qubits,
        "runs": runs,
        "history": [],
    }
    write_json(folder / "manifest.json", manifest)
    log(f"prepared {len(runs)} runs, {len(transpiled)} circuits, tag {manifest['tag']}")
    return manifest


def submit(folder: Path, manifest: dict, service, log: Log) -> None:
    """Send the saved circuits as one job, tagged so a lost reply can be found."""
    from qiskit import qpy
    from qiskit_ibm_runtime import SamplerV2

    if manifest["state"] == "submitting":
        # The last attempt may have reached IBM before the connection dropped.
        found = patiently(
            "looking for the job by its tag",
            lambda: service.jobs(
                job_tags=[manifest["tag"]], backend_name=manifest["backend"]
            ),
            log,
        )
        if found:
            manifest.update(job_id=found[0].job_id(), state="submitted")
            write_json(folder / "manifest.json", manifest)
            log(f"found job {manifest['job_id']} from the last attempt")
            return

    with (folder / "circuits.qpy").open("rb") as f:
        circuits = qpy.load(f)
    pubs = []
    for run in manifest["runs"]:
        for c in circuits[run["first"] : run["first"] + run["n_circuits"]]:
            pubs.append((c, None, run["shots"]))
    manifest["state"] = "submitting"
    write_json(folder / "manifest.json", manifest)

    backend = patiently(
        "fetching the backend", lambda: service.backend(manifest["backend"]), log
    )
    sampler = SamplerV2(
        mode=backend, options={"environment": {"job_tags": [manifest["tag"]]}}
    )
    job = patiently("submitting", lambda: sampler.run(pubs), log)
    manifest.update(job_id=job.job_id(), submitted=now(), state="submitted")
    write_json(folder / "manifest.json", manifest)
    log(f"submitted job {job.job_id()} to {manifest['backend']}")
    try:
        manifest["usage_estimation"] = job.usage_estimation
        write_json(folder / "manifest.json", manifest)
        log(f"IBM estimates {job.usage_estimation}")
    except Exception:
        pass


def wait(folder: Path, manifest: dict, service, log: Log, every: float) -> str:
    """Ask for the job's status every ``every`` seconds until it is final.
    A failed check is logged and tried again; it never ends the wait."""
    last = manifest["status"]
    delay = every
    while True:
        try:
            status = str(service.job(manifest["job_id"]).status())
            delay = every
        except Exception as exc:
            if not transient(exc):
                raise
            log(
                f"status check failed ({type(exc).__name__}: {exc}); "
                f"trying again in {delay:.0f} s"
            )
            time.sleep(delay)
            delay = min(600, delay * 2)
            continue
        if status != last:
            manifest["status"] = status
            manifest["history"].append({"time": now(), "status": status})
            write_json(folder / "manifest.json", manifest)
            log(f"job {manifest['job_id']}: {status}")
            last = status
        if status in FINAL:
            return status
        time.sleep(every)


def fetch(folder: Path, manifest: dict, service, log: Log, status: str) -> None:
    """Save the raw result (or the error), IBM's metrics and the run's calibration."""
    from qiskit_ibm_runtime import RuntimeEncoder

    job = patiently("fetching the job", lambda: service.job(manifest["job_id"]), log)
    for name, get in [
        ("metrics", job.metrics),
        ("calibration_run", lambda: job.properties().to_dict()),
    ]:
        try:
            write_json(folder / "job" / f"{name}.json", patiently(name, get, log))
        except Exception as exc:
            log(f"could not save {name} ({type(exc).__name__}: {exc})")
    if status != "DONE":
        error = {"status": status, "error": job.error_message(), "time": now()}
        write_json(folder / "job" / "error.json", error)
        manifest.update(state="failed", finished=now(), error=error["error"])
        write_json(folder / "manifest.json", manifest)
        log(f"job {manifest['job_id']} ended {status}: {error['error']}")
        return
    result = patiently("downloading the result", job.result, log)
    path = folder / "job" / "result.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, cls=RuntimeEncoder))
    os.replace(tmp, path)
    manifest.update(state="finished", finished=now())
    write_json(folder / "manifest.json", manifest)
    log(f"saved the result of job {manifest['job_id']}")


def run(args) -> None:
    package_path = Path(args.package).resolve()
    package, entries = load_package(package_path)
    folder = Path(args.out).resolve() if args.out else DEFAULT_OUT / package["name"]
    folder.mkdir(parents=True, exist_ok=True)
    log = Log(folder / "log.txt")

    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["fingerprint"] != fingerprint(entries):
            raise SystemExit(
                f"{folder} holds a different package (the images or settings changed). "
                "Give a new name, or --out a new folder."
            )
        log(f"resuming: state {manifest['state']}, job {manifest['job_id']}")
    else:
        plans_on(devices.graph(package.get("device", "phoenix")), entries)  # fail early
        manifest = prepare(folder, package_path, package, entries, log)
    follow(folder, manifest, log, args.poll, package)


def resume(args) -> None:
    folder = Path(args.folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text())
    log = Log(folder / "log.txt")
    log(f"resuming: state {manifest['state']}, job {manifest['job_id']}")
    package = tomllib.loads(Path(args.package).read_text()) if args.package else None
    follow(folder, manifest, log, args.poll, package)


def follow(
    folder: Path, manifest: dict, log: Log, every: float, package: dict | None
) -> None:
    """Take a prepared package as far as it goes: submit, wait, fetch, decode.
    ``package`` gives the credentials, if any; the environment gives the rest."""
    if manifest["state"] in ("prepared", "submitting", "submitted"):
        service = patiently("connecting to IBM", connect(package), log)
        if manifest["state"] != "submitted":
            submit(folder, manifest, service, log)
        try:
            status = wait(folder, manifest, service, log, every)
        except KeyboardInterrupt:
            log("stopped waiting; the job keeps running. Run the same command to resume.")
            raise SystemExit(130) from None
        except Exception as exc:  # e.g. an expired key: the job is fine, so is the folder
            log(
                f"stopped waiting ({type(exc).__name__}: {exc}). "
                "Fix the cause and run the same command to resume."
            )
            raise SystemExit(1) from exc
        fetch(folder, manifest, service, log, status)
    if manifest["state"] == "finished":
        decode_folder(folder, log)
    elif manifest["state"] == "failed":
        raise SystemExit(f"job {manifest['job_id']} failed: {manifest.get('error')}")


# ----------------------------------------------------------------------
# decode: saved raw result -> images and metrics
# ----------------------------------------------------------------------
def save_png(path: Path, b64: str, width: int, height: int, scale: int) -> None:
    from PIL import Image

    rgba = np.frombuffer(base64.b64decode(b64), np.uint8).reshape(height, width, 4)
    im = Image.fromarray(rgba, "RGBA")
    im.save(path)
    if scale > 1:
        im.resize((width * scale, height * scale), Image.NEAREST).save(
            path.with_name(f"{path.stem}_x{scale}.png")
        )


def save_images(where: Path, out: dict, scale: int) -> None:
    w, h = out["width"], out["height"]
    for kind, images in [("raw", out), ("drr", out["drr"])]:
        if "image" in images:
            save_png(where / f"{kind}.png", images["image"], w, h, scale)
        for frame, angle in zip(
            images.get("frames", []), out.get("alphas_degrees", []), strict=True
        ):
            save_png(where / f"{kind}_{angle:05.1f}deg.png", frame, w, h, scale)


def decode_folder(folder: Path, log: Log, scale: int = 8) -> None:
    from qiskit_ibm_runtime import RuntimeDecoder

    manifest = json.loads((folder / "manifest.json").read_text())
    result = json.loads((folder / "job" / "result.json").read_text(), cls=RuntimeDecoder)
    graph = graph_from_coupling(manifest["edges"], manifest["n_qubits"])
    rows = []
    for run in manifest["runs"]:
        request = json.loads((folder / run["request_file"]).read_text())
        settings = {k: v for k, v in request.items() if k != "images"}
        row = {
            "label": run["label"],
            "images": run["images"],
            **settings,
            "shots": run["shots"],
            "n_circuits": run["n_circuits"],
        }
        where = folder / "results" / run["label"]
        where.mkdir(parents=True, exist_ok=True)
        try:
            plan = build_plan(request, graph)
            counts = [
                counts_of(result, run["first"] + i) for i in range(run["n_circuits"])
            ]
            out = decode_hardware(plan, counts)
        except Exception as exc:  # one bad run never hides the others
            row["error"] = f"{type(exc).__name__}: {exc}"
            write_json(where / "error.json", row)
            log(f"{run['label']}: could not decode ({row['error']})")
            rows.append(row)
            continue
        out["run"] = {
            "kind": "hardware",
            "device": manifest["backend"],
            "job_id": manifest["job_id"],
            "shots": run["shots"],
            "first_circuit": run["first"],
            "n_circuits": run["n_circuits"],
        }
        out["settings"] = settings
        write_json(where / "result.json", out)
        save_images(where, out, scale)
        row.update(
            n_pixels=out["n_pixels"],
            slots_used=out["slots_used"],
            capacity=out["capacity"],
            drr_method=out["drr"]["method"],
        )
        row.update({f"raw.{k}": v for k, v in out["metrics"].items()})
        row.update({f"drr.{k}": v for k, v in out["drr"]["metrics"].items()})
        rows.append(row)
        log(
            f"{run['label']}: decoded, raw {out['metrics']}, "
            f"best DRR {out['drr']['method']} {out['drr']['metrics']}"
        )

    write_json(
        folder / "summary.json",
        {"job_id": manifest["job_id"], "backend": manifest["backend"], "runs": rows},
    )
    columns = list(dict.fromkeys(k for row in rows for k in row))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, columns)
    writer.writeheader()
    writer.writerows(rows)
    (folder / "summary.csv").write_text(buffer.getvalue())
    log(f"wrote {folder / 'summary.csv'}")


def decode(args) -> None:
    folder = Path(args.folder).resolve()
    decode_folder(folder, Log(folder / "log.txt"), args.scale)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="build every plan offline; send nothing")
    p.add_argument("package")
    p.set_defaults(fn=check)
    p = sub.add_parser("run", help="submit the package once, then wait for it")
    p.add_argument("package")
    p.add_argument("--out", help=f"output folder (default {DEFAULT_OUT}/<name>)")
    p.add_argument("--poll", type=float, default=60, help="seconds between checks")
    p.set_defaults(fn=run)
    p = sub.add_parser("resume", help="keep waiting on a submitted package folder")
    p.add_argument("folder")
    p.add_argument("--package", help="a package file to take the [ibm] credentials from")
    p.add_argument("--poll", type=float, default=60, help="seconds between checks")
    p.set_defaults(fn=resume)
    p = sub.add_parser("decode", help="decode a finished package folder again, offline")
    p.add_argument("folder")
    p.add_argument(
        "--scale", type=int, default=8, help="also save PNGs this many times larger"
    )
    p.set_defaults(fn=decode)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
