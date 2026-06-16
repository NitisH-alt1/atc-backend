/**
 * ATC Live — Backend Connector Patch
 * ====================================
 * Drop this <script> block into your atc_live.html just before </body>
 * (after the existing scripts) to redirect all live data fetches
 * through your local FastAPI backend instead of hitting external APIs directly.
 *
 * Requirements:
 *   1. Backend running:  uvicorn main:app --host 0.0.0.0 --port 8000
 *   2. Paste the contents of this file inside a <script> tag at the bottom of
 *      your HTML file, just before </body>
 *
 * What this does:
 *   - Overrides fetchAirplanesLive(), fetchAdsbFi(), fetchOpenSkyProxy()
 *     to call /api/flights on your backend instead
 *   - The backend handles source selection, CORS, caching, and normalisation
 *   - Emergency squawk monitoring via /api/emergencies
 *   - Route enrichment via /api/route/:callsign (replaces AeroDataBox for basic data)
 */

(function () {
  // ── Config ──────────────────────────────────────────────────────────────────
  const BACKEND_URL = 'https://atc-backend-4unh.onrender.com';  // change if hosted elsewhere
  const POLL_MS      = 8000;  // how often to poll (ms) — matches backend TTL
  let   _pollTimer   = null;
  let   _connected   = false;

  // ── Normalise backend format → internal plane format ────────────────────────
  // The backend returns unified objects; map them to what your existing
  // openSkyStateToPlane() / adsbV2ToPlane() already produces.
  function backendToPlane(ac) {
    return {
      icao24:   ac.icao24,
      callsign: ac.callsign,
      lat:      ac.lat,
      lon:      ac.lon,
      alt:      ac.alt_ft,        // internal uses alt (feet)
      gs:       ac.gs_kts,
      heading:  ac.heading,
      squawk:   ac.squawk,
      type:     ac.type,
      source:   ac.source,
    };
  }

  // ── Drop-in replacement for fetchLiveFlights() ───────────────────────────────
  async function fetchFromBackend() {
    const url = `${BACKEND_URL}/api/flights`;
    const res  = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(9000) });
    if (!res.ok) throw new Error(`Backend HTTP ${res.status}`);
    const data = await res.json();
    if (!data.aircraft || !data.aircraft.length) throw new Error('Backend: empty response');

    // Map to the format the rest of your code expects
    const live = data.aircraft.map(ac => {
      // Re-use existing adsbV2ToPlane logic by building a compatible object
      const v2compat = {
        hex:       ac.icao24,
        flight:    ac.callsign,
        lat:       ac.lat,
        lon:       ac.lon,
        alt_baro:  ac.alt_ft,
        gs:        ac.gs_kts,
        track:     ac.heading,
        squawk:    ac.squawk,
        t:         ac.type,
        on_ground: false,
      };
      return adsbV2ToPlane(v2compat);  // your existing parser
    }).filter(Boolean);

    return { live, source: data.source, count: data.count };
  }

  // ── Route enrichment via backend ──────────────────────────────────────────────
  // Overrides the existing enrichWithAeroDataBox() if no AeroDataBox key is set
  const _origEnrich = window.enrichWithAeroDataBox;
  window.enrichWithAeroDataBox = function (callsign, cb) {
    const key = window._AERODATABOX_KEY || AERODATABOX_KEY;
    if (key) {
      // If user has pasted an AeroDataBox key, let original function handle it
      return _origEnrich ? _origEnrich(callsign, cb) : cb(null);
    }

    // Otherwise try our backend's /api/route endpoint (uses pyopensky)
    const cs = callsign.trim().replace(/\s/g, '');
    fetch(`${BACKEND_URL}/api/route/${cs}`, { signal: AbortSignal.timeout(5000) })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data) { cb(null); return; }
        // Map pyopensky route response to AeroDataBox-style callback structure
        cb({
          departure: { iataCode: data.origin  || data.estDepartureAirport || '?' },
          arrival:   { iataCode: data.destination || data.estArrivalAirport || '?' },
          airline:   { name: data.callsign || callsign },
        });
      })
      .catch(() => cb(null));
  };

  // ── Override the master fetch function ────────────────────────────────────────
  // fetchLiveFlights() in your HTML tries sources in sequence.
  // We prepend the backend as the first (and usually only) source tried.
  const _origFetchLive = window.fetchLiveFlights;
  window.fetchLiveFlights = async function () {
    setApiStatus('⟳ CONNECTING BACKEND…', false);
    try {
      const { live, source, count } = await fetchFromBackend();
      if (live.length) {
        mergeLivePlanes(live, `BACKEND·${source.toUpperCase()}`);
        _connected = true;
        return;
      }
    } catch (e) {
      console.warn('[backend-patch] Backend fetch failed:', e.message);
      _connected = false;
    }

    // Backend unreachable → fall through to original multi-source logic
    console.info('[backend-patch] Falling back to direct fetch');
    return _origFetchLive ? _origFetchLive() : null;
  };

  // ── Emergency monitor ────────────────────────────────────────────────────────
  // Polls /api/emergencies every 10 s and logs to console (extend as needed)
  async function pollEmergencies() {
    try {
      const res  = await fetch(`${BACKEND_URL}/api/emergencies`, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (data.count > 0) {
        console.warn(`[ATC] ${data.count} EMERGENCY SQUAWK(S):`, data.aircraft.map(a => `${a.callsign} (${a.squawk})`));
      }
    } catch (_) { /* silent */ }
  }
  setInterval(pollEmergencies, 10000);

  // ── Status check on load ─────────────────────────────────────────────────────
  fetch(`${BACKEND_URL}/api/status`, { signal: AbortSignal.timeout(3000) })
    .then(r => r.json())
    .then(s => {
      console.info(
        `[ATC Backend] ✓ connected — source=${s.source} aircraft=${s.aircraft_cached}` +
        ` pyopensky=${s.pyopensky} opensky_auth=${s.opensky_auth}`
      );
    })
    .catch(() => {
      console.warn('[ATC Backend] ✗ not reachable — using direct fetch fallback');
    });

  console.info('[backend-patch] ATC backend connector loaded → ' + BACKEND_URL);
})();
