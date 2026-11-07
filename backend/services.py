"""Small service functions for real geocoding, routes, and trip estimates."""
import math
import re
import requests

HEADERS = {"User-Agent": "SmartRouteAI-CollegeProject/1.0 (route planning demo)"}
FUEL_DEFAULTS = {
    "car": {"fuel_type": "petrol", "efficiency": 15, "energy_price": 104, "emission_factor": 2.31},
    "bike": {"fuel_type": "petrol", "efficiency": 45, "energy_price": 104, "emission_factor": 2.31},
    "walking": {"fuel_type": "none", "efficiency": 1, "energy_price": 0, "emission_factor": 0},
    "bicycle": {"fuel_type": "none", "efficiency": 1, "energy_price": 0, "emission_factor": 0},
    "bus": {"fuel_type": "diesel", "efficiency": 5, "energy_price": 92, "emission_factor": 2.68},
}

def parse_trip(text):
    """Extract a simple from/to trip and optional vehicle without guessing places."""
    cleaned = re.sub(r"[?.!,]+$", "", text.strip())
    patterns = [r"\bfrom\s+(.+?)\s+to\s+(.+)", r"\bbetween\s+(.+?)\s+and\s+(.+)"]
    match = next((m for p in patterns if (m := re.search(p, cleaned, re.I))), None)
    if not match:
        raise ValueError("Please include both places, for example: from MVP Colony to RK Beach.")
    source, destination = match.groups()
    destination = re.split(r"\s+(?:by|using|with)\s+(?:my\s+)?(?:car|bike|bicycle|bus|walking|foot)\b", destination, flags=re.I)[0]
    source = re.sub(r"^(?:i want to go|i want to travel|i need to travel|take me|navigate|show route|route|travel|go)\s+", "", source, flags=re.I).strip()
    destination = destination.strip()
    vehicle = "car"
    vmatch = re.search(r"\b(car|bike|bicycle|bus|walking|walk|foot)\b", text, re.I)
    if vmatch:
        vehicle = {"walk":"walking", "foot":"walking"}.get(vmatch.group(1).lower(), vmatch.group(1).lower())
    if len(source) < 2 or len(destination) < 2:
        raise ValueError("I found an incomplete trip. Please provide a source and destination.")
    return {"source": source, "destination": destination, "vehicle": vehicle, "confidence": 0.9}

def geocode(location):
    try:
        response = requests.get("https://nominatim.openstreetmap.org/search", params={"q": location, "format": "jsonv2", "limit": 1}, headers=HEADERS, timeout=12)
        if response.status_code == 429:
            raise ValueError("The map search is busy. Please wait a moment and try again.")
        response.raise_for_status()
        found = response.json()
    except requests.RequestException as exc:
        raise ValueError("Map search is temporarily unavailable. Please try again shortly.") from exc
    if not found:
        raise ValueError(f"No map result found for ‘{location}’. Try adding the city or region.")
    item = found[0]
    return {"latitude": float(item["lat"]), "longitude": float(item["lon"]), "display_name": item.get("display_name", location)}

def reverse_geocode(latitude, longitude):
    """Turn a browser-provided GPS point into a readable place label."""
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": latitude, "lon": longitude, "format": "jsonv2"},
            headers=HEADERS,
            timeout=12,
        )
        if response.status_code == 429:
            raise ValueError("Map search is busy. Your GPS location can still be used; try again shortly for its address.")
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise ValueError("Could not get an address for your GPS location.") from exc
    return {"latitude": float(latitude), "longitude": float(longitude),
            "display_name": result.get("display_name") or "My current location"}

def search_places(query):
    """Search OpenStreetMap place names and POIs without biasing results to one city."""
    try:
        response = requests.get("https://photon.komoot.io/api/", params={"q": query, "limit": 8, "lang": "en"}, headers=HEADERS, timeout=10)
        response.raise_for_status()
        features = response.json().get("features", [])
    except requests.RequestException as exc:
        raise ValueError("Place suggestions are temporarily unavailable. You can still enter a full place name.") from exc
    results = []
    for item in features:
        props = item.get("properties", {})
        coords = item.get("geometry", {}).get("coordinates", [])
        if len(coords) != 2 or not props.get("name"):
            continue
        local = [props.get(k) for k in ("street", "district", "city", "state", "country") if props.get(k) and props.get(k) != props.get("name")]
        results.append({"name": props["name"], "display_name": ", ".join([props["name"], *local]), "latitude": coords[1], "longitude": coords[0], "category": props.get("osm_value", props.get("type", "place"))})
    normalized_query = query.strip().casefold()
    results.sort(key=lambda place: (
        0 if place["name"].casefold() == normalized_query else
        1 if place["name"].casefold().startswith(normalized_query) else 2
    ))
    return results

def vehicle_profile(vehicle, custom=None):
    base = FUEL_DEFAULTS.get(vehicle, FUEL_DEFAULTS["car"]).copy()
    if custom:
        base.update({k: custom[k] for k in ("fuel_type", "efficiency", "energy_price", "emission_factor") if k in custom})
    if float(base["efficiency"]) <= 0 or float(base["energy_price"]) < 0:
        raise ValueError("Vehicle efficiency must be above zero and price cannot be negative.")
    return base

def route_options(source, destination, vehicle, mode="balanced", custom=None, source_coordinates=None, destination_coordinates=None):
    local_markers = ("visakhapatnam", "vizag", "rk beach", "mvp colony", "andhra university", "dwaraka nagar", "cmr central")
    local_context = any(marker in f"{source} {destination}".lower() for marker in local_markers)
    # Add city context only to known ambiguous Vizag shortcuts. Never append it
    # to arbitrary place names (for example, Tuni or another user's town).
    local_shortcuts = ("rk beach", "mvp colony", "cmr central mall", "andhra university", "dwaraka nagar")
    if local_context:
        if not source_coordinates and any(x in source.lower() for x in local_shortcuts) and not re.search(r"visakhapatnam|vizag|india", source, re.I):
            source = f"{source}, Visakhapatnam, India"
        if not destination_coordinates and any(x in destination.lower() for x in local_shortcuts) and not re.search(r"visakhapatnam|vizag|india", destination, re.I):
            destination = f"{destination}, Visakhapatnam, India"
    def resolved_place(name, coordinates):
        if not coordinates:
            return geocode(name)
        try:
            latitude = float(coordinates["latitude"])
            longitude = float(coordinates["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("A selected map result has invalid coordinates. Choose the place again.") from exc
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("A selected map result has invalid coordinates. Choose the place again.")
        return {"latitude": latitude, "longitude": longitude,
                "display_name": str(coordinates.get("display_name") or name)}

    a = resolved_place(source, source_coordinates)
    b = resolved_place(destination, destination_coordinates)
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(b["longitude"] - a["longitude"])
    haversine = 2 * 6371 * math.asin(min(1, math.sqrt(
        math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)))
    local_trip = haversine <= 20
    profile = vehicle_profile(vehicle, custom)
    coords = f"{a['longitude']},{a['latitude']};{b['longitude']},{b['latitude']}"
    try:
        response = requests.get(f"https://router.project-osrm.org/route/v1/driving/{coords}", params={"overview":"full", "geometries":"geojson", "alternatives":"true", "steps":"true"}, headers=HEADERS, timeout=25)
        if response.status_code == 429:
            raise ValueError("The route service is busy. Please try again shortly.")
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise ValueError("The route service is temporarily unavailable. Please try again shortly.") from exc
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise ValueError("No drivable route was found between those places.")
    typical_speeds = {
        "car": (25, 50), "bike": (30, 45), "bus": (18, 30),
        "walking": (5, 5), "bicycle": (15, 20),
    }
    city_speed, open_road_speed = typical_speeds.get(vehicle, typical_speeds["car"])
    assumed_speed = city_speed if local_trip else open_road_speed
    routes = []
    for i, item in enumerate(payload["routes"]):
        km = item["distance"] / 1000
        steps = item.get("legs", [{}])[0].get("steps", [])
        turns = sum(1 for step in steps if step.get("maneuver", {}).get("type") in
                    {"turn", "fork", "roundabout", "merge", "on ramp", "off ramp", "end of road"})
        # OSRM supplies road distance and a car-profile duration. Use a vehicle-aware
        # average speed as a lower-bound estimate, then add a small junction allowance.
        distance_seconds = item["distance"] / (assumed_speed * 1000 / 3600)
        seconds = max(item["duration"], distance_seconds) + min(turns, 8) * 12
        minutes = seconds / 60
        if profile["fuel_type"] == "electric":
            energy = km / float(profile["efficiency"]); fuel = 0; cost = energy * float(profile["energy_price"])
            co2 = energy * float(profile.get("emission_factor", 0) or 0)
        elif profile["fuel_type"] == "none":
            energy = fuel = cost = co2 = 0
        else:
            fuel = km / float(profile["efficiency"]); energy = 0
            cost = fuel * float(profile["energy_price"]); co2 = fuel * float(profile.get("emission_factor", 0) or 0)
        instructions = []
        for step in steps:
            maneuver = step.get("maneuver", {}); kind = maneuver.get("type", "continue"); modifier = maneuver.get("modifier", ""); road = step.get("name")
            if kind == "arrive": text = "You have arrived at your destination."
            elif kind == "depart": text = f"Start toward {road}." if road else "Start your journey."
            elif kind == "roundabout": text = f"At the roundabout, take the {modifier} exit." if modifier else "Enter the roundabout and follow the route."
            elif kind in {"turn", "fork", "merge", "on ramp", "off ramp", "end of road"}: text = f"Turn {modifier} onto {road}." if road else f"Turn {modifier}."
            else: text = f"Continue on {road}." if road else "Continue along the route."
            location = maneuver.get("location")
            instructions.append({"text": text, "distance_m": round(step.get("distance", 0)), "location": location})
        routes.append({"id": i, "distance_km": round(km, 1), "duration_min": round(minutes, 1), "duration_seconds": seconds, "assumed_speed_kmh": assumed_speed, "eta_note": f"Estimated at a typical {vehicle} speed of {assumed_speed} km/h, with a small allowance for {turns} turns. Live traffic is not included.", "fuel_used": round(fuel, 2), "energy_used": round(energy, 2), "fuel_type": profile["fuel_type"], "cost": round(cost, 2), "co2_kg": round(co2, 2), "geometry": item["geometry"], "source": a, "destination": b, "instructions": instructions})
    fields = {"fastest":"duration_seconds", "shortest":"distance_km", "cheapest":"cost", "eco":"co2_kg"}
    primary = fields.get(mode)
    if primary:
        best = min(range(len(routes)), key=lambda i: routes[i][primary])
    else:
        # Balanced scoring normalizes each measure across actual returned alternatives.
        keys = ["duration_seconds", "distance_km", "cost", "co2_kg"]
        mins = {k:min(r[k] for r in routes) for k in keys}; maxs = {k:max(r[k] for r in routes) for k in keys}
        weights = {"duration_seconds":.35,"distance_km":.2,"cost":.25,"co2_kg":.2}
        def score(r):
            return sum(weights[k] * ((r[k]-mins[k])/(maxs[k]-mins[k]) if maxs[k] != mins[k] else 0) for k in keys)
        best = min(range(len(routes)), key=lambda i: score(routes[i]))
    tags = ["Alternative"] * len(routes)
    for key,tag in [("duration_seconds","Fastest"),("distance_km","Shortest"),("cost","Cheapest"),("co2_kg","Eco")]:
        idx = min(range(len(routes)),key=lambda i:routes[i][key])
        if tags[idx] == "Alternative": tags[idx]=tag
    for i,r in enumerate(routes):
        r["tag"] = ("Balanced pick" if i==best and mode=="balanced" else tags[i])
        r["description"] = "Recommended · balanced estimates" if i==best and mode=="balanced" else ("Recommended for this preference" if i==best else "Available road alternative")
        r["explanation"] = explanation(r, routes, best)
    return routes, best

def explanation(route, routes, best):
    if len(routes)==1: return "This is the only route option currently returned by the routing service."
    other = min((r for r in routes if r is not route), key=lambda r:r["cost"])
    bits=[]
    if route["duration_seconds"] < other["duration_seconds"]: bits.append(f"{round((other['duration_seconds']-route['duration_seconds'])/60,1)} min quicker than the cheapest alternative")
    if route["cost"] < other["cost"]: bits.append(f"{round(other['cost']-route['cost'])} lower estimated cost than the next option")
    if route["co2_kg"] < other["co2_kg"] and route["co2_kg"] > 0: bits.append(f"{round((other['co2_kg']-route['co2_kg'])/max(other['co2_kg'],.01)*100)}% lower estimated CO₂")
    return " · ".join(bits[:2]) or "Selected from the real route alternatives returned by OSRM."
