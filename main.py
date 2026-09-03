"""
Niho Ride - Location Service (FastAPI, Google Maps edition)
-------------------------------------------------------------
Fixes applied to the version you had running on Render:

1. `/share-location` and `/shared-location/{id}` were MISSING entirely.
   The Flutter app already calls `/share-location` (the "ሎኬሽን አጋራ" button) —
   without this route, that feature would 404 with FastAPI's bare
   `{"detail":"Not Found"}`, the exact error you saw earlier.
2. `GOOGLE_MAPS_API_KEY` was silently left as a placeholder if unset. Every
   geocode/route call would then fail with Google's "REQUEST_DENIED", which
   your old code mislabeled as "ቦታው አልተገኘም" (place not found) — the real
   problem (missing/invalid key) was hidden. Now it's checked up front and
   reported clearly.
3. Google's various error statuses (ZERO_RESULTS, OVER_QUERY_LIMIT,
   REQUEST_DENIED, INVALID_REQUEST) were all collapsed into one generic
   404. Each now gets its own message so you know which one you're hitting.
4. Network failures (timeouts, DNS errors, Render cold-start hiccups) to the
   Google API weren't caught — they'd bubble up as an unhandled 500. Now
   wrapped and reported as a clear 502.
5. `decode_polyline` had no bounds/format checking — a malformed or
   truncated polyline string would raise an uncaught IndexError (500).
   Now wrapped with a clear error instead.
6. Added a `__main__` block using Render's `$PORT` env var, in case your
   start command isn't already passing `--port $PORT` explicitly.

Run locally:
  pip install -r requirements.txt
  export GOOGLE_MAPS_API_KEY=your_real_key_here
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload

On Render:
  Set GOOGLE_MAPS_API_KEY under Environment → Environment Variables.
  Make sure the Geocoding API AND Directions API are both enabled for that
  key in Google Cloud Console, and billing is enabled on the project —
  Google will return REQUEST_DENIED otherwise, even with a valid-looking key.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Niho Ride Location Service", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔑 Set this as a real Environment Variable on Render — never hardcode a key
# in source control.
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# In-memory store for shared locations. Fine for a single Render instance /
# testing; swap for a real DB (e.g. Supabase) so links survive restarts and
# work if you ever scale to multiple instances.
shared_locations: dict[str, dict] = {}


def _require_api_key() -> None:
    if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY_HERE":
        raise HTTPException(
            status_code=500,
            detail=(
                "GOOGLE_MAPS_API_KEY አልተቀናበረም። Render → Environment ላይ "
                "ትክክለኛ የGoogle Maps API ቁልፍ ያክሉ (Geocoding API እና Directions "
                "API ሁለቱም መንቃት አለባቸው)."
            ),
        )


_GOOGLE_STATUS_MESSAGES = {
    "ZERO_RESULTS": "ቦታው አልተገኘም",
    "OVER_QUERY_LIMIT": "የGoogle Maps ጥያቄ ገደብ ደርሷል፣ ትንሽ ቆይተው ይሞክሩ",
    "REQUEST_DENIED": "የGoogle Maps API ቁልፍ ትክክል አይደለም ወይም Geocoding/Directions API አልነቃም",
    "INVALID_REQUEST": "የተላከው ጥያቄ ትክክል አይደለም",
}


def _google_error_detail(status: str, fallback: str) -> str:
    return _GOOGLE_STATUS_MESSAGES.get(status, fallback)


class GeocodeResult(BaseModel):
    lat: float
    lng: float
    display_name: str


class RoutePoint(BaseModel):
    lat: float
    lng: float


class RouteRequest(BaseModel):
    origin: RoutePoint
    destination: RoutePoint


class RouteResult(BaseModel):
    distance_km: float
    duration_min: float
    polyline: list[list[float]]


class ShareLocationRequest(BaseModel):
    lat: float
    lng: float
    user_name: str | None = None


class ShareLocationResult(BaseModel):
    share_id: str
    share_url: str
    expires_at: str


class SharedLocationResult(BaseModel):
    lat: float
    lng: float
    user_name: str | None
    expires_at: str


@app.get("/")
async def root():
    return {"status": "ok", "service": "niho-ride-location-service"}


@app.get("/geocode", response_model=GeocodeResult)
async def geocode(address: str):
    """የቦታ ስም ተቀብሎ Google Geocoding API በመጠቀም lat/lng ይመልሳል።"""
    _require_api_key()
    formatted_address = f"{address}, Ethiopia"

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": formatted_address, "key": GOOGLE_MAPS_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="የGoogle Maps አገልግሎት ላይ መድረስ አልተቻለም")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="የካርታ አገልግሎት ምላሽ አልሰጠም")

    data = resp.json()
    status = data.get("status")
    if status != "OK" or not data.get("results"):
        raise HTTPException(
            status_code=404,
            detail=_google_error_detail(status, f"'{address}' የሚባል ቦታ አልተገኘም"),
        )

    location = data["results"][0]["geometry"]["location"]
    display_name = data["results"][0]["formatted_address"]

    return GeocodeResult(lat=location["lat"], lng=location["lng"], display_name=display_name)


@app.post("/route", response_model=RouteResult)
async def get_route(req: RouteRequest):
    """Google Directions API በመጠቀም ከመነሻ እስከ መድረሻ ያለውን መንገድ ያሰላል።"""
    _require_api_key()
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{req.origin.lat},{req.origin.lng}",
        "destination": f"{req.destination.lat},{req.destination.lng}",
        "key": GOOGLE_MAPS_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="የGoogle Maps አገልግሎት ላይ መድረስ አልተቻለም")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="የመንገድ አገልግሎት ምላሽ አልሰጠም")

    data = resp.json()
    status = data.get("status")
    if status != "OK" or not data.get("routes"):
        raise HTTPException(
            status_code=400,
            detail=_google_error_detail(status, "ከመነሻ እስከ መድረሻ መንገድ ማግኘት አልተቻለም"),
        )

    route = data["routes"][0]
    leg = route["legs"][0]

    encoded_polyline = route["overview_polyline"]["points"]
    try:
        decoded_points = decode_polyline(encoded_polyline)
    except (IndexError, ValueError):
        raise HTTPException(status_code=502, detail="የመንገድ መስመር መፍታት (decode) አልተቻለም")

    return RouteResult(
        distance_km=round(leg["distance"]["value"] / 1000, 2),
        duration_min=round(leg["duration"]["value"] / 60, 1),
        polyline=decoded_points,
    )


@app.post("/share-location", response_model=ShareLocationResult)
async def share_location(req: ShareLocationRequest):
    """የተጠቃሚን የአሁን መገኛ ቦታ ለ2 ሰዓት ብቻ የሚቆይ ማጋሪያ ሊንክ ይፈጥራል።"""
    share_id = uuid.uuid4().hex[:8]
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)

    shared_locations[share_id] = {
        "lat": req.lat,
        "lng": req.lng,
        "user_name": req.user_name,
        "expires_at": expires_at,
    }

    return ShareLocationResult(
        share_id=share_id,
        share_url=f"https://ride-api-3.onrender.com/track/{share_id}",
        expires_at=expires_at.isoformat(),
    )


@app.get("/shared-location/{share_id}", response_model=SharedLocationResult)
async def get_shared_location(share_id: str):
    """የተጋራ ሊንክ ተከትሎ የቦታውን lat/lng ይመልሳል (ገና ካላበቃ)።"""
    entry = shared_locations.get(share_id)
    if not entry:
        raise HTTPException(status_code=404, detail="ሊንኩ አልተገኘም ወይም ጊዜው አልፎበታል")

    if datetime.now(timezone.utc) > entry["expires_at"]:
        del shared_locations[share_id]
        raise HTTPException(status_code=410, detail="የማጋሪያ ሊንኩ ጊዜው አልፎበታል")

    return SharedLocationResult(
        lat=entry["lat"],
        lng=entry["lng"],
        user_name=entry["user_name"],
        expires_at=entry["expires_at"].isoformat(),
    )


def decode_polyline(polyline_str: str) -> list[list[float]]:
    """Decodes a Google encoded polyline string into [lat, lng] pairs."""
    if not polyline_str:
        raise ValueError("empty polyline")

    index, lat, lng = 0, 0, 0
    coordinates = []
    length = len(polyline_str)

    while index < length:
        result, shift = 0, 0
        while True:
            if index >= length:
                raise IndexError("truncated polyline")
            b = ord(polyline_str[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        result, shift = 0, 0
        while True:
            if index >= length:
                raise IndexError("truncated polyline")
            b = ord(polyline_str[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coordinates.append([lat / 1e5, lng / 1e5])

    return coordinates


if __name__ == "__main__":
    import uvicorn

    # Render injects $PORT — bind to it so the service is reachable even if
    # your start command doesn't pass --port explicitly.
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
