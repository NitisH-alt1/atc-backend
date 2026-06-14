# ATC Live — FastAPI Backend

A lightweight Python backend that proxies real-time ADS-B flight data from
multiple sources, adds caching, authentication support, and a clean unified
API for your Global ATC Surveillance dashboard.

---

## Architecture

```
  Browser (atc_live.html)
        │
        │  GET /api/flights  (every 8 s)
        ▼
  FastAPI Backend  :8000
        │
        ├──► airplanes.live    (primary — free, global ADS-B+MLAT)
        ├──► adsb.fi            (fallback — free community feed)
        ├──► OpenSky (auth)     (fallback — 5 s resolution, needs account)
        └──► OpenSky (anon)     (last resort — 10 s resolution, no key)
```

Benefits over the current direct-from-browser approach:
- **No CORS issues** — your backend handles cross-origin requests
- **Authenticated OpenSky** — 5 s resolution vs 10 s, higher rate limits
- **Server-side caching** — all browser tabs share one upstream request
- **Route enrichment** — `/api/route/:callsign` via pyopensky (free)
- **Emergency monitor** — `/api/emergencies` endpoint
- **Bounding box filter** — fetch only aircraft in a geographic region

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

> To skip pyopensky (if you don't want OpenSky auth), remove that line from
> `requirements.txt` first.

### 2. Configure credentials (optional)

```bash
cp .env.example .env
# Edit .env and add your OpenSky username/password
```

Free OpenSky account: https://opensky-network.org/  
(Doubles your data resolution from 10 s → 5 s)

### 3. Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000/docs to see the Swagger UI.

### 4. Connect your frontend

Open `atc_live.html` in a text editor.

Find the closing `</body>` tag and paste in the contents of `frontend_patch.js`
inside a `<script>` tag, like this:

```html
  <!-- ... existing scripts ... -->

  <script>
    /* paste entire contents of frontend_patch.js here */
  </script>
</body>
```

That's it — the patch overrides the fetch functions to use your backend
instead of direct external calls. If the backend is unreachable it automatically
falls back to the original direct-fetch behaviour.

---

## API Reference

| Endpoint | Description |
|---|---|
| `GET /api/flights` | All live aircraft (normalised) |
| `GET /api/flights?lat_min=&lat_max=&lon_min=&lon_max=` | Bounding box filter |
| `GET /api/flights?min_alt=35000` | Only aircraft above FL350 |
| `GET /api/flights?squawk=7700` | Aircraft squawking 7700 |
| `GET /api/flights/{icao24}` | Single aircraft by hex |
| `GET /api/route/{callsign}` | Route info via pyopensky |
| `GET /api/emergencies` | Aircraft squawking 7700/7600/7500 |
| `GET /api/status` | Health + cache info |
| `GET /docs` | Swagger UI |

### Example response — `/api/flights`

```json
{
  "source": "airplanes.live",
  "count": 4821,
  "total": 4821,
  "timestamp": 1718273405,
  "aircraft": [
    {
      "icao24":   "a835af",
      "callsign": "UAL123",
      "lat":      40.63,
      "lon":      -73.78,
      "alt_ft":   38000,
      "gs_kts":   472,
      "heading":  87.0,
      "squawk":   "2056",
      "type":     "B738",
      "on_ground": false,
      "source":   "airplanes.live"
    }
    // ...
  ]
}
```

---

## Historical Data (Replay Mode)

pyopensky also gives access to the OpenSky Trino database for historical
ADS-B data. To query it (requires OpenSky account):

```python
from pyopensky.trino import Trino

trino = Trino()

# All flights over India on a specific day
df = trino.history(
    "2024-06-01", "2024-06-01 01:00",
    bounds=(60, 5, 90, 35)   # lon_min, lat_min, lon_max, lat_max
)
print(df.head())
```

You could expose this as a `/api/replay?date=2024-06-01&region=india` endpoint
to add a time-travel slider to your dashboard.

---

## Production Deployment

For public hosting, change in `main.py`:

```python
allow_origins=["https://yourdomain.com"]   # instead of "*"
```

Run with Gunicorn + multiple workers:

```bash
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

Or use the included `Dockerfile` pattern:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## File Structure

```
atc_backend/
├── main.py             ← FastAPI application (this is the server)
├── frontend_patch.js   ← Drop-in JS to connect your HTML to the backend
├── requirements.txt    ← Python dependencies
├── .env.example        ← Credential template (copy to .env)
└── README.md           ← This file
```
