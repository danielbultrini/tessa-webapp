"""TESSA web app: the FastAPI backend Vercel serves next to ``public/``.

Run locally from this folder with::

    uv run --no-project --python 3.12 --with-requirements requirements.txt --with uvicorn uvicorn app:app --reload

and open http://127.0.0.1:8000.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # the vendored iqpixl copy and tessa

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from tessa import devices, runners  # noqa: E402

app = FastAPI(title="TESSA")


def _call(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:  # a bad request: say what was wrong
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # IBM errors and the like; never echo the request
        raise HTTPException(
            status_code=502, detail=f"{type(exc).__name__}: {exc}"
        ) from exc


@app.get("/api/devices")
def list_devices() -> dict:
    return {name: devices.info(name) for name in devices.DEVICES}


@app.post("/api/simulate")
def simulate(body: dict) -> dict:
    return _call(runners.simulate, body)


@app.post("/api/ibm/submit")
def ibm_submit(body: dict) -> dict:
    return _call(runners.ibm_submit, body.get("request", {}), body.get("credentials", {}))


@app.post("/api/ibm/status")
def ibm_status(body: dict) -> dict:
    return _call(
        runners.ibm_status,
        body.get("request", {}),
        body.get("credentials", {}),
        body.get("ticket", {}),
    )


# Vercel serves public/ from its CDN; these routes only matter when running locally.
@app.get("/")
def index():
    return FileResponse(HERE / "public" / "index.html")


@app.get("/{name}")
def static_file(name: str):
    path = HERE / "public" / name
    if path.parent != HERE / "public" or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)
