import React, { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

const DEFAULT_CENTER = { lat: 39.8283, lng: -98.5795 };
const MODES = ["driving", "walking", "bicycling", "transit"];
const MODE_ICONS = { driving: "🚗", walking: "🚶", bicycling: "🚲", transit: "🚌" };
const ROUTE_COLOR = "#8b5cf6";

let googleMapsLoader = null;

function loadGoogleMaps(key) {
  if (window.google?.maps) {
    return Promise.resolve(window.google.maps);
  }
  if (googleMapsLoader) {
    return googleMapsLoader;
  }
  googleMapsLoader = new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-nova-google-maps="1"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(window.google.maps), { once: true });
      existing.addEventListener("error", () => reject(new Error("Failed to load Google Maps.")), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&libraries=places`;
    script.async = true;
    script.defer = true;
    script.dataset.novaGoogleMaps = "1";
    script.onload = () => resolve(window.google.maps);
    script.onerror = () => reject(new Error("Failed to load Google Maps."));
    document.head.appendChild(script);
  });
  return googleMapsLoader;
}

function coordsText(location) {
  if (!location) return "";
  return `${location.lat},${location.lng}`;
}

function formatMiles(value) {
  const meters = Number(value);
  if (!Number.isFinite(meters)) return "";
  const miles = meters / 1609.344;
  if (miles >= 10) return `${miles.toFixed(1)} mi`;
  return `${miles.toFixed(2)} mi`;
}

export default function MapsSheet({
  routePreload = null,
  currentLocation = null,
  locationStatus = "idle",
  locationNote = "",
  onRequestCurrentLocation,
  onManualLocationSet,
}) {
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [nearbyQuery, setNearbyQuery] = useState("");
  const [manualLocation, setManualLocation] = useState("");
  const [mode, setMode] = useState("driving");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [mapError, setMapError] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [mapReady, setMapReady] = useState(false);
  const [route, setRoute] = useState(null);
  const [placesResult, setPlacesResult] = useState(null);
  const [activeRouteRequest, setActiveRouteRequest] = useState(null);
  const [selectedPlaceId, setSelectedPlaceId] = useState(null);
  const [routeQr, setRouteQr] = useState("");

  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const directionsRendererRef = useRef(null);
  const markersRef = useRef([]);
  const infoWindowRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    async function fetchKey() {
      try {
        const res = await fetch(apiUrl("/api/maps/key"));
        const data = await res.json();
        if (!res.ok) throw new Error(data?.detail || JSON.stringify(data));
        if (!cancelled) setApiKey(String(data.key || ""));
      } catch (err) {
        if (!cancelled) setMapError(String(err?.message || err));
      }
    }
    fetchKey();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!apiKey || !mapContainerRef.current) return () => {
      cancelled = true;
    };

    loadGoogleMaps(apiKey)
      .then(() => {
        if (cancelled || mapRef.current) {
          if (!cancelled) setMapReady(true);
          return;
        }
        mapRef.current = new window.google.maps.Map(mapContainerRef.current, {
          center: currentLocation || DEFAULT_CENTER,
          zoom: currentLocation ? 13 : 4,
          mapTypeControl: true,
          streetViewControl: false,
          fullscreenControl: true,
          zoomControl: true,
          gestureHandling: "greedy",
          styles: [
            { elementType: "geometry", stylers: [{ color: "#111827" }] },
            { elementType: "labels.text.fill", stylers: [{ color: "#f5d589" }] },
            { elementType: "labels.text.stroke", stylers: [{ color: "#1f2937" }] },
          ],
        });
        infoWindowRef.current = new window.google.maps.InfoWindow();
        setMapReady(true);
      })
      .catch((err) => {
        if (!cancelled) setMapError(String(err?.message || err));
      });

    return () => {
      cancelled = true;
    };
  }, [apiKey, currentLocation]);

  useEffect(() => {
    if (!routePreload) return;
    setError("");
    if (routePreload.kind === "places") {
      setPlacesResult(routePreload);
      setRoute(null);
      setActiveRouteRequest(null);
      setSelectedPlaceId(null);
      setNearbyQuery(routePreload.query || "");
      return;
    }
    setRoute(routePreload);
    setPlacesResult(null);
    setActiveRouteRequest({
      origin: routePreload.origin_query || routePreload.origin || "",
      destination: routePreload.destination_query || routePreload.destination || "",
      mode: routePreload.mode || "driving",
    });
    setOrigin(routePreload.origin_query || routePreload.origin || "");
    setDestination(routePreload.destination_query || routePreload.destination || "");
    setMode(routePreload.mode || "driving");
  }, [routePreload]);

  // Phone hand-off: turn the route's Google Maps URL into a QR code Marcus can
  // scan to get real live turn-by-turn on his phone. Generated fully client-side
  // (offline) — no external request.
  useEffect(() => {
    let cancelled = false;
    const url = route?.maps_url;
    if (!url) {
      setRouteQr("");
      return undefined;
    }
    QRCode.toDataURL(url, { margin: 1, width: 176 })
      .then((dataUrl) => {
        if (!cancelled) setRouteQr(dataUrl);
      })
      .catch(() => {
        if (!cancelled) setRouteQr("");
      });
    return () => {
      cancelled = true;
    };
  }, [route?.maps_url]);

  function clearMarkers() {
    for (const marker of markersRef.current) {
      marker.setMap(null);
    }
    markersRef.current = [];
  }

  function clearDirections() {
    if (directionsRendererRef.current) {
      directionsRendererRef.current.setMap(null);
      directionsRendererRef.current = null;
    }
  }

  function ensureDirectionsRenderer() {
    if (!mapRef.current || !window.google?.maps) return null;
    if (!directionsRendererRef.current) {
      directionsRendererRef.current = new window.google.maps.DirectionsRenderer({
        map: mapRef.current,
        suppressMarkers: false,
        polylineOptions: {
          strokeColor: ROUTE_COLOR,
          strokeOpacity: 0.92,
          strokeWeight: 6,
        },
      });
    }
    return directionsRendererRef.current;
  }

  function showPlaceInfo(marker, place) {
    if (!infoWindowRef.current) return;
    const parts = [
      `<div style="color:#111827;font-weight:600;">${place.name || "Place"}</div>`,
      place.address ? `<div style="color:#374151;">${place.address}</div>` : "",
      place.distance_meters ? `<div style="color:#6b7280;">${formatMiles(place.distance_meters)} away</div>` : "",
    ].filter(Boolean);
    infoWindowRef.current.setContent(parts.join(""));
    infoWindowRef.current.open({ anchor: marker, map: mapRef.current });
  }

  function renderIdleMap() {
    if (!mapRef.current || !window.google?.maps) return;
    clearDirections();
    clearMarkers();
    if (currentLocation) {
      mapRef.current.setCenter(currentLocation);
      mapRef.current.setZoom(13);
      const marker = new window.google.maps.Marker({
        map: mapRef.current,
        position: currentLocation,
        title: "Your current location",
      });
      markersRef.current = [marker];
    } else {
      mapRef.current.setCenter(DEFAULT_CENTER);
      mapRef.current.setZoom(4);
    }
  }

  function renderPlacesOnMap(result) {
    if (!mapRef.current || !window.google?.maps) return;
    clearDirections();
    clearMarkers();

    const bounds = new window.google.maps.LatLngBounds();
    const newMarkers = [];

    if (currentLocation) {
      const currentMarker = new window.google.maps.Marker({
        map: mapRef.current,
        position: currentLocation,
        title: "Your current location",
        label: "Y",
      });
      newMarkers.push(currentMarker);
      bounds.extend(currentLocation);
    }

    for (const place of result.places || []) {
      const position = place.location;
      if (!position?.lat || !position?.lng) continue;
      const marker = new window.google.maps.Marker({
        map: mapRef.current,
        position,
        title: place.name || result.query || "Place",
      });
      marker.addListener("click", () => showPlaceInfo(marker, place));
      newMarkers.push(marker);
      bounds.extend(position);
    }

    markersRef.current = newMarkers;
    if (!bounds.isEmpty()) {
      mapRef.current.fitBounds(bounds, 56);
    }
  }

  function renderRouteOnMap(result) {
    if (!mapRef.current || !window.google?.maps) return;
    clearMarkers();
    const renderer = ensureDirectionsRenderer();
    if (!renderer) return;

    const originRequest = result.origin_coords || result.origin_query || result.origin;
    const destinationRequest = result.destination_query || result.destination;
    const travelMode = window.google.maps.TravelMode[String(result.mode || "driving").toUpperCase()] || window.google.maps.TravelMode.DRIVING;

    new window.google.maps.DirectionsService().route(
      {
        origin: originRequest,
        destination: destinationRequest,
        travelMode,
      },
      (response, status) => {
        if (status === "OK") {
          renderer.setDirections(response);
        }
      }
    );
  }

  useEffect(() => {
    if (!mapReady) return;
    if (route) {
      renderRouteOnMap(route);
      return;
    }
    if (placesResult?.places?.length) {
      renderPlacesOnMap(placesResult);
      return;
    }
    renderIdleMap();
  }, [mapReady, route, placesResult, currentLocation]);

  function handleReset() {
    setOrigin("");
    setDestination("");
    setNearbyQuery("");
    setManualLocation("");
    setMode("driving");
    setRoute(null);
    setPlacesResult(null);
    setActiveRouteRequest(null);
    setSelectedPlaceId(null);
    setError("");
    renderIdleMap();
  }

  async function requestDirections({ origin: nextOrigin, destination: nextDestination, nextMode = mode }) {
    const originQuery = (nextOrigin || "").trim() || coordsText(currentLocation);
    const destinationQuery = (nextDestination || "").trim();
    if (!originQuery || !destinationQuery) {
      setError("Enter a destination. Leave origin blank to use your current location.");
      return null;
    }

    setLoading(true);
    setError("");
    try {
      const url = apiUrl(
        `/api/maps/directions?origin=${encodeURIComponent(originQuery)}&destination=${encodeURIComponent(destinationQuery)}&mode=${nextMode}`
      );
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || JSON.stringify(data));
      if (data.status !== "OK") throw new Error(`Maps API: ${data.status} ${data.error || ""}`);
      setRoute(data);
      setPlacesResult((prev) => prev);
      setActiveRouteRequest({ origin: originQuery, destination: destinationQuery, mode: nextMode });
      return data;
    } catch (err) {
      setError(String(err?.message || err));
      return null;
    } finally {
      setLoading(false);
    }
  }

  async function handleUseMyLocation() {
    setError("");
    try {
      await onRequestCurrentLocation?.({ silent: false });
    } catch (err) {
      setError(String(err?.message || err));
    }
  }

  async function handleManualLocation(e) {
    e?.preventDefault();
    const query = manualLocation.trim();
    if (!query) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/maps/geocode?address=${encodeURIComponent(query)}`));
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || JSON.stringify(data));
      if (data.status !== "OK" || !data.location) {
        throw new Error(`Maps API: ${data.status || "UNKNOWN_ERROR"}`);
      }
      onManualLocationSet?.(
        {
          lat: Number(data.location.lat),
          lng: Number(data.location.lng),
          accuracy_m: null,
        },
        data.formatted_address || query
      );
      setOrigin("");
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
  }

  async function handleDirections(e) {
    e?.preventDefault();
    setPlacesResult(null);
    setSelectedPlaceId(null);
    await requestDirections({ origin, destination, nextMode: mode });
  }

  async function handleNearby(e) {
    e?.preventDefault();
    if (!nearbyQuery.trim()) return;
    if (!currentLocation) {
      setError("Location access is required to find places near you.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const url = apiUrl(
        `/api/maps/nearby?query=${encodeURIComponent(nearbyQuery)}&lat=${encodeURIComponent(currentLocation.lat)}&lng=${encodeURIComponent(currentLocation.lng)}&limit=6`
      );
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || JSON.stringify(data));
      if (data.status !== "OK" && data.status !== "ZERO_RESULTS") {
        throw new Error(`Maps API: ${data.status} ${data.error || ""}`);
      }
      setPlacesResult(data);
      setRoute(null);
      setActiveRouteRequest(null);
      setSelectedPlaceId(null);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
  }

  async function focusPlace(place) {
    if (!mapRef.current || !place?.location) return;
    mapRef.current.setCenter(place.location);
    mapRef.current.setZoom(15);
    setSelectedPlaceId(place.place_id || place.name || null);
    setDestination(place.address || place.name || "");
    await requestDirections({
      origin,
      destination: place.address || place.name || "",
      nextMode: mode,
    });
  }

  useEffect(() => {
    if (!activeRouteRequest || !route) return;
    if ((route.mode || "") === mode) return;
    requestDirections({
      origin: activeRouteRequest.origin || origin,
      destination: activeRouteRequest.destination || destination,
      nextMode: mode,
    });
  }, [mode]);

  return (
    <div className="space-y-4 text-nova-gold h-full flex flex-col">
      <div className="rounded-2xl border border-nova-gold/15 bg-black/15 p-3 space-y-3">
        <form onSubmit={handleDirections} className="space-y-2">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <button
              type="button"
              onClick={handleUseMyLocation}
              className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm"
            >
              {locationStatus === "locating" ? "Locating..." : "Use My Location"}
            </button>
            <div className="text-[11px] text-nova-gold/55 flex-1">
              {locationNote || "Allow location access or type your location below."}
            </div>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row">
            <input
              value={manualLocation}
              onChange={(e) => setManualLocation(e.target.value)}
              className="flex-1 rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/15 text-nova-gold outline-none placeholder:text-nova-gold/45 text-sm"
              placeholder="Type your current location or address..."
            />
            <button
              type="button"
              onClick={handleManualLocation}
              disabled={loading || !manualLocation.trim()}
              className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50"
            >
              Set Location
            </button>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row">
            <input
              value={origin}
              onChange={(e) => setOrigin(e.target.value)}
              className="flex-1 rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/15 text-nova-gold outline-none placeholder:text-nova-gold/45 text-sm"
              placeholder={currentLocation ? "From… (leave blank to use your location)" : "From…"}
            />
            <input
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
              className="flex-1 rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/15 text-nova-gold outline-none placeholder:text-nova-gold/45 text-sm"
              placeholder="To…"
            />
          </div>
          <div className="flex flex-wrap gap-2 items-center">
            <div className="flex gap-1 flex-1 flex-wrap">
              {MODES.map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  className={[
                    "rounded-lg px-3 py-1 text-xs border capitalize flex items-center gap-1",
                    mode === m
                      ? "border-nova-gold/60 bg-nova-gold/10 text-nova-gold"
                      : "border-nova-gold/15 bg-black/20 text-nova-gold/55 hover:text-nova-gold",
                  ].join(" ")}
                >
                  <span>{MODE_ICONS[m]}</span>
                  <span>{m}</span>
                </button>
              ))}
            </div>
            <button
              type="submit"
              disabled={loading || !destination.trim()}
              className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50"
            >
              {loading ? "…" : "Directions"}
            </button>
          </div>
        </form>

        <form onSubmit={handleNearby} className="flex flex-col gap-2 lg:flex-row">
          <input
            value={nearbyQuery}
            onChange={(e) => setNearbyQuery(e.target.value)}
            className="flex-1 rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/15 text-nova-gold outline-none placeholder:text-nova-gold/45 text-sm"
            placeholder="Find nearby places… (e.g. tire shop, McDonald's, gas station)"
          />
          <button
            type="submit"
            disabled={loading || !nearbyQuery.trim()}
            className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50"
          >
            {loading ? "…" : "Find Nearby"}
          </button>
          <button
            type="button"
            onClick={handleReset}
            className="rounded-xl px-3 py-2 bg-black/20 border border-nova-gold/15 text-nova-gold/60 hover:text-nova-gold text-sm"
          >
            Reset
          </button>
        </form>

        <div className="text-[11px] text-nova-gold/55">
          {currentLocation ? "Location ready. Nova can route from here and search nearby places." : "No location set yet. Click “Use My Location” or type your address above."}
        </div>
      </div>

      {(error || mapError) && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">
          {error || mapError}
        </div>
      )}

      <div className="rounded-2xl overflow-hidden border border-nova-gold/15 bg-black/10 shadow-lg flex-shrink-0" style={{ height: 380 }}>
        <div ref={mapContainerRef} className="w-full h-full" />
      </div>

      {route && (
        <div className="flex flex-col gap-3 min-h-0 overflow-y-auto">
          <div className="rounded-xl border border-nova-gold/15 bg-black/20 px-3 py-2 flex flex-wrap gap-3 text-xs text-nova-gold/85 items-center">
            <span className="truncate max-w-[38%]">📍 {route.origin}</span>
            <span className="text-nova-gold/35">→</span>
            <span className="truncate max-w-[38%]">📍 {route.destination}</span>
          </div>
          <div className="flex gap-4 text-xs text-nova-gold/70 px-1">
            <span>🕐 {route.duration}</span>
            <span>📏 {formatMiles(route.distance_meters) || route.distance}</span>
            {route.distance ? <span className="text-nova-gold/45">({route.distance})</span> : null}
            <span className="capitalize">{MODE_ICONS[route.mode] || "🚗"} {route.mode}</span>
          </div>
          {route.steps && route.steps.length > 0 && (
            <div className="space-y-1">
              <div className="text-[10px] uppercase tracking-widest text-nova-gold/50 px-1">
                Turn-by-turn ({route.steps.length} steps)
              </div>
              {route.steps.map((step, index) => (
                <div key={index} className="flex gap-2 text-xs text-nova-gold/80 px-1 py-0.5">
                  <span className="text-nova-gold/35 shrink-0 w-5 text-right">{index + 1}.</span>
                  <span className="flex-1">{step.instruction}</span>
                  <span className="text-nova-gold/40 shrink-0 text-right">{step.distance}</span>
                </div>
              ))}
            </div>
          )}
          {routeQr && (
            <div className="flex items-center gap-3 rounded-xl border border-nova-gold/15 bg-black/20 px-3 py-2">
              <img src={routeQr} alt="Route QR code" className="w-24 h-24 rounded bg-white p-1 shrink-0" />
              <div className="text-xs text-nova-gold/70">
                <div className="text-nova-gold font-medium mb-1">📱 Send to your phone</div>
                Scan with your phone camera to open this route in Google Maps for live turn-by-turn navigation.
              </div>
            </div>
          )}
          {route.maps_url && (
            <a
              href={route.maps_url}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-nova-gold/55 hover:text-nova-gold underline"
            >
              Open in Google Maps ↗
            </a>
          )}
        </div>
      )}

      {placesResult && (
        <div className="space-y-2 min-h-0 overflow-y-auto">
          <div className="text-[10px] uppercase tracking-widest text-nova-gold/50 px-1">
            Nearby {placesResult.query || "places"}
          </div>
          {(placesResult.places || []).length === 0 && (
            <div className="text-xs text-nova-gold/60 px-1">No nearby places matched that search.</div>
          )}
          {(placesResult.places || []).map((place, index) => (
            <button
              key={`${place.place_id || place.name || "place"}-${index}`}
              type="button"
              onClick={() => focusPlace(place)}
              className={[
                "w-full text-left rounded-xl border px-3 py-2 hover:bg-black/30",
                selectedPlaceId && selectedPlaceId === (place.place_id || place.name || null)
                  ? "border-nova-gold/50 bg-nova-gold/10"
                  : "border-nova-gold/15 bg-black/20",
              ].join(" ")}
            >
              <div className="text-sm text-nova-gold">{place.name}</div>
              <div className="text-xs text-nova-gold/65">{place.address}</div>
              <div className="mt-1 flex gap-3 text-[11px] text-nova-gold/50">
                {place.distance_meters ? <span>{formatMiles(place.distance_meters)} away</span> : null}
                {place.rating ? <span>⭐ {place.rating}</span> : null}
                {typeof place.open_now === "boolean" ? <span>{place.open_now ? "Open now" : "Closed now"}</span> : null}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

