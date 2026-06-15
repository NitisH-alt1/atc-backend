"""
ATC Live — FastAPI Backend
==========================
Proxies ADS-B flight data from multiple sources (airplanes.live, adsb.fi, OpenSky)
with authentication, caching, CORS headers, route enrichment and a unified API.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Your HTML frontend then replaces its direct fetch calls with:
    http://localhost:8000/api/flights
"""

import asyncio
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Optional: pyopensky for authenticated OpenSky access ─────────────────────
try:
    from pyopensky.rest import REST as OpenSkyREST
    PYOPENSKY_AVAILABLE = True
except ImportError:
    PYOPENSKY_AVAILABLE = False

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config (override via .env or environment variables)
# ─────────────────────────────────────────────────────────────────────────────
OPENSKY_USERNAME   = os.getenv("OPENSKY_USERNAME", "")
OPENSKY_PASSWORD   = os.getenv("OPENSKY_PASSWORD", "")
AERODATABOX_KEY    = os.getenv("AERODATABOX_KEY", "")
AVIATIONSTACK_KEY  = os.getenv("AVIATIONSTACK_KEY", "")

CACHE_TTL_SECONDS  = int(os.getenv("CACHE_TTL", "8"))    # how often to refresh data
MAX_AIRCRAFT       = int(os.getenv("MAX_AIRCRAFT", "5000"))
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("atc-backend")

# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ─────────────────────────────────────────────────────────────────────────────
_cache: dict = {
    "flights": [],
    "last_fetch": 0.0,
    "source": "none",
    "count": 0,
}
_route_cache: dict = {}   # icao24 → route info
_http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    log.info("HTTP client started")
    yield
    await _http_client.aclose()
    log.info("HTTP client closed")


# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ATC Live Backend",
    description="Unified ADS-B proxy server for the Global ATC Surveillance dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production to your frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation helpers
# ─────────────────────────────────────────────────────────────────────────────
def _norm_adsb_v2(ac: dict) -> Optional[dict]:
    """Convert ADS-B Exchange v2 / airplanes.live / adsb.fi format → unified dict."""
    lon = ac.get("lon") or ac.get("longitude")
    lat = ac.get("lat") or ac.get("latitude")
    if lon is None or lat is None:
        return None

    alt_baro = ac.get("alt_baro")
    alt_geom = ac.get("alt_geom")
    altitude  = ac.get("altitude")
    alt_ft = (
        alt_baro if isinstance(alt_baro, (int, float)) else
        alt_geom if isinstance(alt_geom, (int, float)) else
        altitude if isinstance(altitude, (int, float)) else 0
    )
    if alt_ft < 500:
        return None
    if ac.get("on_ground") in (True, 1, "1"):
        return None

    icao24   = (ac.get("hex") or ac.get("icao24") or "").lower().strip()
    callsign = (ac.get("flight") or ac.get("callsign") or "").strip() or icao24.upper()
    gs       = ac.get("gs")
    heading  = ac.get("track") or ac.get("true_heading") or ac.get("heading") or 0
    squawk   = ac.get("squawk") or "2000"
    ac_type  = (ac.get("t") or ac.get("type") or ac.get("aircraft_type") or "").upper()[:4]

    return {
        "icao24":   icao24,
        "callsign": callsign,
        "lat":      round(float(lat), 5),
        "lon":      round(float(lon), 5),
        "alt_ft":   int(alt_ft),
        "gs_kts":   int(gs) if isinstance(gs, (int, float)) else 450,
        "heading":  round(float(heading), 1),
        "squawk":   squawk,
        "type":     ac_type or "UNKN",
        "on_ground": False,
        "source":   "",   # filled by caller
    }


def _norm_opensky_state(state: list) -> Optional[dict]:
    """Convert OpenSky raw state vector array → unified dict."""
    # columns: 0=icao24 1=callsign 2=origin 3=time_pos 4=last_contact
    # 5=lon 6=lat 7=baro_alt_m 8=on_ground 9=velocity_ms 10=true_track
    # 11=vert_rate 12=sensors 13=geo_alt_m 14=squawk 15=spi 16=pos_src
    try:
        icao24   = state[0]
        callsign = (state[1] or "").strip() or icao24.upper()
        lon      = state[5]
        lat      = state[6]
        alt_m    = state[7] or state[13] or 0
        on_ground= state[8]
        vel_ms   = state[9] or 0
        heading  = state[10] or 0
        squawk   = state[14] or "2000"
    except (IndexError, TypeError):
        return None

    if on_ground or lon is None or lat is None:
        return None

    alt_ft = int(alt_m * 3.28084)
    if alt_ft < 500:
        return None

    gs_kts = int(vel_ms * 1.94384)

    return {
        "icao24":   icao24.lower(),
        "callsign": callsign,
        "lat":      round(float(lat), 5),
        "lon":      round(float(lon), 5),
        "alt_ft":   alt_ft,
        "gs_kts":   gs_kts,
        "heading":  round(float(heading), 1),
        "squawk":   squawk,
        "type":     "UNKN",
        "on_ground": False,
        "source":   "opensky",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Data source fetchers
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_airplanes_live() -> list[dict]:
    """airplanes.live — free, no key. Queries multiple busy regions and merges results."""
    points = [
        (51, 10, 250),    # Western Europe
        (45, 15, 250),    # Central/Southern Europe
        (40, -74, 250),   # US Northeast
        (34, -118, 250),  # US West Coast
        (41, -87, 250),   # US Midwest
        (35, 139, 250),   # Japan
        (31, 121, 250),   # China East Coast
        (1, 104, 250),    # Southeast Asia
        (25, 55, 250),    # Middle East
        (-34, 151, 250),  # Australia East Coast
    ]

    async def fetch_point(lat, lon, radius):
        url = f"https://api.airplanes.live/v2/point/{lat}/{lon}/{radius}"
        try:
            r = await _http_client.get(url)
            r.raise_for_status()
            data = r.json()
            return data.get("ac") or data.get("aircraft") or []
        except Exception as e:
            log.debug("airplanes.live point %s,%s failed: %s", lat, lon, e)
            return []

    raw_lists = await asyncio.gather(*(fetch_point(*p) for p in points))

    seen = set()
    results = []
    for raw in raw_lists:
        for ac in raw:
            p = _norm_adsb_v2(ac)
            if p:
                icao = p.get("icao24")
                if icao in seen:
                    continue
                seen.add(icao)
                p["source"] = "airplanes.live"
                results.append(p)
                if len(results) >= MAX_AIRCRAFT:
                    break

    log.info("airplanes.live → %d aircraft (merged from %d regions)", len(results), len(points))
    return results


async def fetch_adsb_fi() -> list[dict]:
    """adsb.fi — free community feed, global."""
    url = "https://opendata.adsb.fi/api/v2/aircraft"
    r = await _http_client.get(url)
    r.raise_for_status()
    data = r.json()
    raw = data.get("aircraft") or data.get("ac") or []
    results = []
    for ac in raw:
        p = _norm_adsb_v2(ac)
        if p:
            p["source"] = "adsb.fi"
            results.append(p)
        if len(results) >= MAX_AIRCRAFT:
            break
    log.info("adsb.fi → %d aircraft", len(results))
    return results


async def fetch_opensky_anonymous() -> list[dict]:
    """OpenSky anonymous REST — 10s resolution, no auth needed."""
    url = "https://opensky-network.org/api/states/all"
    r = await _http_client.get(url)
    r.raise_for_status()
    data = r.json()
    states = data.get("states") or []
    results = []
    for state in states:
        p = _norm_opensky_state(state)
        if p:
            p["source"] = "opensky-anon"
            results.append(p)
        if len(results) >= MAX_AIRCRAFT:
            break
    log.info("opensky-anon → %d aircraft", len(results))
    return results


async def fetch_opensky_authenticated() -> list[dict]:
    """
    OpenSky with OAuth2 client-credentials auth — direct REST call, 5s resolution.
    Requires OPENSKY_USERNAME (client_id) + OPENSKY_PASSWORD (client_secret) in env.
    """
    if not OPENSKY_USERNAME or not OPENSKY_PASSWORD:
        raise RuntimeError("No OpenSky OAuth2 credentials configured")

    token_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": OPENSKY_USERNAME,
                "client_secret": OPENSKY_PASSWORD,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise RuntimeError("OpenSky token response missing access_token")

        states_resp = await client.get(
            "https://opensky-network.org/api/states/all",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        states_resp.raise_for_status()
        data = states_resp.json()

    states = data.get("states") or []
    results = []
    for state in states:
        p = _norm_opensky_state(state)
        if p:
            p["source"] = "opensky-auth"
            results.append(p)
            if len(results) >= MAX_AIRCRAFT:
                break

    log.info("opensky-auth → %d aircraft", len(results))
    return results
async def fetch_opensky_route(callsign: str) -> Optional[dict]:
    """Fetch route info for a callsign via pyopensky REST."""
    if not PYOPENSKY_AVAILABLE:
        return None
    cached = _route_cache.get(callsign)
    if cached and time.time() - cached["_ts"] < 3600:
        return cached

    loop = asyncio.get_event_loop()

    def _fetch():
        try:
            rest = OpenSkyREST()
            result = rest.routes(callsign=callsign)
            if result is None:
                return None
            # result may be a dict or DataFrame row
            if hasattr(result, "to_dict"):
                result = result.iloc[0].to_dict() if len(result) else None
            return result
        except Exception as e:
            log.debug("route fetch failed for %s: %s", callsign, e)
            return None

    data = await loop.run_in_executor(None, _fetch)
    if data:
        data["_ts"] = time.time()
        _route_cache[callsign] = data
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Master fetcher — tries sources in priority order
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_PRIORITY = [
    ("opensky-anon",      fetch_opensky_anonymous),
    ("airplanes.live",    fetch_airplanes_live),
    ("adsb.fi",           fetch_adsb_fi),
    ("opensky-auth",      fetch_opensky_authenticated),
]


async def refresh_cache() -> None:
    now = time.time()
    if now - _cache["last_fetch"] < CACHE_TTL_SECONDS:
        return  # still fresh

    for name, fetcher in SOURCE_PRIORITY:
        try:
            flights = await fetcher()
            if flights:
                _cache["flights"]    = flights
                _cache["last_fetch"] = time.time()
                _cache["source"]     = name
                _cache["count"]      = len(flights)
                log.info("Cache refreshed from %s: %d aircraft", name, len(flights))
                return
        except Exception as e:
           log.warning("Source %s failed: %s: %r", name, type(e).__name__, e)

    log.error("All sources failed — cache not updated")


# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "ATC Live Backend",
        "version": "1.0.0",
        "endpoints": {
            "/api/flights":          "All live aircraft (normalised)",
            "/api/flights/{icao24}": "Single aircraft by ICAO24 hex",
            "/api/route/{callsign}": "Route info for a callsign (pyopensky)",
            "/api/status":           "Server + cache health",
            "/docs":                 "Swagger UI",
        }
    }


@app.get("/api/flights")
async def get_flights(
    lat_min:  Optional[float] = Query(None, description="Bounding box min latitude"),
    lat_max:  Optional[float] = Query(None, description="Bounding box max latitude"),
    lon_min:  Optional[float] = Query(None, description="Bounding box min longitude"),
    lon_max:  Optional[float] = Query(None, description="Bounding box max longitude"),
    min_alt:  Optional[int]   = Query(None, description="Minimum altitude in feet"),
    squawk:   Optional[str]   = Query(None, description="Filter by squawk code e.g. 7700"),
    limit:    int             = Query(5000,  description="Max aircraft to return"),
):
    """
    Return live aircraft positions.

    Optional bounding box: lat_min, lat_max, lon_min, lon_max
    Optional filters:      min_alt (feet), squawk (e.g. 7700), limit
    """
    await refresh_cache()

    flights = _cache["flights"]

    # Bounding box filter
    if all(v is not None for v in (lat_min, lat_max, lon_min, lon_max)):
        flights = [
            f for f in flights
            if lat_min <= f["lat"] <= lat_max and lon_min <= f["lon"] <= lon_max
        ]

    # Altitude filter
    if min_alt is not None:
        flights = [f for f in flights if f["alt_ft"] >= min_alt]

    # Squawk filter
    if squawk:
        flights = [f for f in flights if f["squawk"] == squawk]

    return {
        "source":    _cache["source"],
        "count":     len(flights[:limit]),
        "total":     len(flights),
        "timestamp": int(_cache["last_fetch"]),
        "aircraft":  flights[:limit],
    }


@app.get("/api/flights/{icao24}")
async def get_flight(icao24: str):
    """Return data for a single aircraft by its ICAO24 hex code."""
    await refresh_cache()
    icao24 = icao24.lower().strip()
    match = next((f for f in _cache["flights"] if f["icao24"] == icao24), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Aircraft {icao24} not found in current data")
    return match


@app.get("/api/route/{callsign}")
async def get_route(callsign: str):
    """
    Return route (origin → destination) for a callsign via pyopensky.
    Cached for 1 hour.  Returns 503 if pyopensky is not available.
    """
    if not PYOPENSKY_AVAILABLE:
        raise HTTPException(status_code=503, detail="pyopensky not installed on this server")

    data = await fetch_opensky_route(callsign.upper().strip())
    if not data:
        raise HTTPException(status_code=404, detail=f"No route data found for {callsign}")
    return data


@app.get("/api/emergencies")
async def get_emergencies():
    """Convenience endpoint — returns only aircraft squawking 7700/7600/7500."""
    await refresh_cache()
    emerg_codes = {"7700", "7600", "7500"}
    aircraft = [f for f in _cache["flights"] if f["squawk"] in emerg_codes]
    return {
        "source":    _cache["source"],
        "count":     len(aircraft),
        "timestamp": int(_cache["last_fetch"]),
        "aircraft":  aircraft,
    }


@app.get("/api/status")
async def get_status():
    """Health check + cache info."""
    age = time.time() - _cache["last_fetch"]
    return {
        "ok":               age < 60,
        "source":           _cache["source"],
        "aircraft_cached":  _cache["count"],
        "cache_age_secs":   round(age, 1),
        "cache_ttl_secs":   CACHE_TTL_SECONDS,
        "pyopensky":        PYOPENSKY_AVAILABLE,
        "opensky_auth":     bool(OPENSKY_USERNAME),
        "aerodatabox":      bool(AERODATABOX_KEY),
    }
