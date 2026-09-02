"""
Niho Ride - Location Service (FastAPI)
----------------------------------------
Endpoints:
  GET  /geocode?address=...           -> lat/lng for a place name (ቦታ ስም -> መጋጠሚያ)
  POST /route                         -> driving route between origin & destination (ከመነሻ እስከ መድረሻ)
  POST /share-location                -> creates a short-lived shareable link for a location
  GET  /shared-location/{share_id}    -> resolves a shared link back to lat/lng

Run locally:
  pip install -r requirements.txt
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Then in Flutter, point `backendBaseUrl` at this server
(use your machine's LAN IP or a deployed URL, not "localhost", when testing on a phone/emulator).
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Niho Ride Location Service", version="1.0.0")

# Allow the Flutter app (any origin) to call this API.
# Lock this down to your app's actual domain(s) in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for shared locations. Swap for Supabase/Redis/a DB in production
# so links survive a server restart and work across multiple server instances.
shared_locations: dict[str, dict] = {}


# ---------- Models ----------

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
    polyline: list[list[float]]  # list of [lat, lng]


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


# ---------- Routes ----------

@app.get("/")
async def root():
    return {"status": "ok", "service": "niho-ride-location-service"}


@app.get("/geocode", response_model=GeocodeResult)
async def geocode(address: str):
    """
    መነሻ/መድረሻ ቦታ ስም ተቀብሎ ኬክሮስ/ኬንትሮስ (lat/lng) ይመልሳል።
    Uses OpenStreetMap's Nominatim (free, but rate-limited to ~1 req/sec —
    for production traffic, switch to Google Geocoding API or Mapbox and add an API key).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": address,
                "format": "json",
                "limit": 1,
                "countrycodes": "et",  # bias results to Ethiopia
            },
            headers={"User-Agent": "NihoRideApp/1.0 (contact: you@example.com)"},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="የካርታ አገልግሎት ላይ ችግር ተፈጥሯል")

    data = resp.json()
    if not data:
        raise HTTPException(status_code=404, detail=f"'{address}' የሚባል ቦታ አልተገኘም")

    return GeocodeResult(
        lat=float(data[0]["lat"]),
        lng=float(data[0]["lon"]),
        display_name=data[0]["display_name"],
    )


@app.post("/route", response_model=RouteResult)
async def get_route(req: RouteRequest):
    """
    ከመነሻ እስከ መድረሻ ያለውን መንገድ (ርቀት፣ ቆይታ፣ በካርታ ላይ የሚሳል መስመር) ያሰላል።
    Uses the public OSRM demo server — fine for testing, but self-host OSRM
    (or use Google Directions / Mapbox Directions) for production reliability.
    """
    url = (
        "http://router.project-osrm.org/route/v1/driving/"
        f"{req.origin.lng},{req.origin.lat};{req.destination.lng},{req.destination.lat}"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params={"overview": "full", "geometries": "geojson"})

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="የመንገድ አገልግሎት ላይ ችግር ተፈጥሯል")

    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise HTTPException(status_code=400, detail="ከመነሻ እስከ መድረሻ መንገድ ማግኘት አልተቻለም")

    route = data["routes"][0]
    # OSRM returns [lng, lat] pairs — flip to [lat, lng] for the Flutter side.
    polyline = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]

    return RouteResult(
        distance_km=round(route["distance"] / 1000, 2),
        duration_min=round(route["duration"] / 60, 1),
        polyline=polyline,
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
        # Replace with your deployed backend's public URL (or a web page that
        # calls /shared-location/{share_id} and renders a map).
        share_url=f"https://YOUR_DOMAIN.com/track/{share_id}",
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
