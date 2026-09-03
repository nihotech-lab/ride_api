import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Niho Ride Location Service", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔑 የጉግል ኤፒአይ ቁልፍህን እዚህ አስገባ ወይም በ Environment Variable አድርገው
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "YOUR_GOOGLE_MAPS_API_KEY_HERE")

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

@app.get("/")
async def root():
    return {"status": "ok", "service": "niho-ride-location-service"}

# 1. ጂኦኮዲንግ - በGoogle Geocoding API የተተካ
@app.get("/geocode", response_model=GeocodeResult)
async def geocode(address: str):
    """
    የቦታ ስም ተቀብሎ የGoogle Geocoding API በመጠቀም lat/lng ይመልሳል።
    """
    # የተሳሳቱ ቃላትንና አጻጻፎችን ጎግል እንዲረደው አገርና ከተማ እንጨምርበታለን
    formatted_address = f"{address}, Ethiopia"
    
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": formatted_address,
        "key": GOOGLE_MAPS_API_KEY,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="የካርታ አገልግሎት ምላሽ አልሰጠም")

    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        raise HTTPException(status_code=404, detail=f"'{address}' የሚባል ቦታ አልተገኘም")

    location = data["results"][0]["geometry"]["location"]
    display_name = data["results"][0]["formatted_address"]

    return GeocodeResult(
        lat=location["lat"],
        lng=location["lng"],
        display_name=display_name,
    )

# 2. የመንገድ መስመር - በGoogle Directions API የተተካ
@app.post("/route", response_model=RouteResult)
async def get_route(req: RouteRequest):
    """
    Google Directions API በመጠቀም ከመነሻ እስከ መድረሻ ያለውን መንገድ ያሰላል።
    """
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{req.origin.lat},{req.origin.lng}",
        "destination": f"{req.destination.lat},{req.destination.lng}",
        "key": GOOGLE_MAPS_API_KEY,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="የመንገድ አገልግሎት ምላሽ አልሰጠም")

    data = resp.json()
    if data.get("status") != "OK" or not data.get("routes"):
        raise HTTPException(status_code=400, detail="ከመነሻ እስከ መድረሻ መንገድ ማግኘት አልተቻለም")

    route = data["routes"][0]
    leg = route["legs"][0]

    # Google የሚያስመልሰውን Encoded Polyline ወደ LatLng Coordinates መቀየር
    encoded_polyline = route["overview_polyline"]["points"]
    decoded_points = decode_polyline(encoded_polyline)

    distance_km = round(leg["distance"]["value"] / 1000, 2)
    duration_min = round(leg["duration"]["value"] / 60, 1)

    return RouteResult(
        distance_km=distance_km,
        duration_min=duration_min,
        polyline=decoded_points,
    )

# 3. Encoded Polyline Decode ማድረጊያ Helper Function
def decode_polyline(polyline_str: str) -> list[list[float]]:
    index, lat, lng = 0, 0, 0
    coordinates = []

    while index < len(polyline_str):
        result, shift = 0, 0
        while True:
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
