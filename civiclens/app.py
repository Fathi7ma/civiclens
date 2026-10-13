"""FastAPI application.

Endpoints
---------
``GET  /``                  single-page demo UI
``GET  /api/health``        liveness + upstream status
``GET  /api/city``          city + dataset registry
``POST /api/ask``           ask a question (optionally with a map point)
``GET  /api/examples``      seed questions
``POST /api/locate``        geocode an address to a point
``GET  /api/eval/report``   latest saved eval report
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, ask, city_summary, examples
from .config import CITY, confidence_threshold
from .resolve import _geocode_via_address_dataset, _geocode_via_nominatim
from .socrata import CACHE, get_client

logging.basicConfig(
    level=os.getenv("CIVICLENS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("civiclens.app")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(
    title="CivicLens",
    version=__version__,
    description="Civic answers that show their receipts.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    lat: Optional[float] = None
    lon: Optional[float] = None
    street: Optional[str] = None


class LocateRequest(BaseModel):
    address: str = Field(..., min_length=2, max_length=200)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    client = get_client()
    return {
        "status": "ok",
        "version": __version__,
        "city": CITY["name"],
        "datasets": len(city_summary()["datasets"]),
        "confidence_threshold": confidence_threshold(),
        "upstream": {
            "domain": client.domain,
            "api_calls_this_process": client.call_count,
            "cache_entries": len(CACHE),
        },
    }


@app.get("/api/city")
def city() -> Dict[str, Any]:
    return city_summary()


@app.get("/api/examples")
def seed_examples() -> Dict[str, Any]:
    return {"examples": examples()}


@app.post("/api/ask")
def api_ask(req: AskRequest) -> Dict[str, Any]:
    try:
        answer = ask(req.question.strip(), lat=req.lat, lon=req.lon, street=req.street)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("ask failed")
        raise HTTPException(status_code=502, detail="Engine error: %s" % exc)
    payload = answer.to_dict()
    payload["intent_scores"] = getattr(answer, "intent_scores", {})
    return payload


@app.post("/api/locate")
def api_locate(req: LocateRequest) -> Dict[str, Any]:
    place = _geocode_via_address_dataset(req.address) or _geocode_via_nominatim(req.address)
    if place is None:
        raise HTTPException(status_code=404, detail="Could not geocode %r" % req.address)
    return {
        "label": place.label,
        "address": place.address,
        "lat": place.lat,
        "lon": place.lon,
        "evidence": {
            key: {"record_id": rid, "detail": detail, "query_url": url}
            for key, (rid, detail, url) in place.evidence.items()
        },
    }


@app.get("/api/eval/report")
def eval_report() -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(HERE), "eval", "results", "report.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No eval report yet. Run: python -m eval.run_eval")
    import json

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    path = os.path.join(STATIC, "index.html")
    if not os.path.exists(path):  # pragma: no cover
        raise HTTPException(status_code=500, detail="UI assets missing")
    return FileResponse(path)


@app.exception_handler(HTTPException)
def http_error(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)