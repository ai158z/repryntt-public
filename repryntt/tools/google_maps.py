"""
google_maps.py — Google Maps API tools extracted from BrainSystem monolith.

Standalone functions — no BrainSystem dependency.
All require GOOGLE_MAPS_API_KEY environment variable.
"""

import os
import math
import logging
import requests

logger = logging.getLogger("repryntt.tools.google_maps")


def _get_api_key():
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        return None, {
            "success": False,
            "error": "Google Maps API key not configured. Set GOOGLE_MAPS_API_KEY environment variable.",
        }
    return key, None


def _is_coordinates(location: str) -> bool:
    if not location or "," not in location:
        return False
    parts = [p.strip() for p in location.split(",")]
    if len(parts) != 2:
        return False
    try:
        float(parts[0])
        float(parts[1])
        return True
    except ValueError:
        return False


def _haversine_meters(lat1, lng1, lat2, lng2):
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ─── geocode_address ──────────────────────────────────────────────

def geocode_address(address: str = "", **kw) -> dict:
    """Convert an address to coordinates using Google Maps Geocoding API.

    Parameters:
        address: Address to geocode (e.g. '1600 Amphitheatre Parkway, Mountain View, CA')
    """
    api_key, err = _get_api_key()
    if err:
        return err
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"key": api_key, "address": address},
            timeout=30,
        )
        data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            return {"success": False, "error": f"Geocoding failed: {data.get('status', 'No results')}",
                    "address": address}

        result = data["results"][0]
        loc = result["geometry"]["location"]
        return {
            "success": True,
            "address": address,
            "formatted_address": result.get("formatted_address", address),
            "latitude": loc["lat"],
            "longitude": loc["lng"],
            "location_type": result["geometry"].get("location_type", "Unknown"),
            "place_id": result.get("place_id", ""),
            "types": result.get("types", []),
            "coordinates": f"{loc['lat']},{loc['lng']}",
        }
    except Exception as e:
        logger.error(f"Error geocoding address: {e}")
        return {"success": False, "error": f"Geocoding failed: {e}", "address": address}


def _resolve_location(location: str) -> tuple:
    """Geocode a location string if it isn't already lat,lng. Returns (resolved, error_dict|None)."""
    if not location:
        return location, None
    if _is_coordinates(location):
        return location, None
    result = geocode_address(location)
    if result.get("success"):
        return f"{result['latitude']},{result['longitude']}", None
    return None, {"success": False, "error": f"Could not geocode '{location}': {result.get('error', 'Unknown')}"}


# ─── google_maps_search ──────────────────────────────────────────

def google_maps_search(query: str = "", location: str = "", radius: int = 5000, **kw) -> dict:
    """Search for places using Google Maps Places API.

    Parameters:
        query: What to search for (e.g. 'solar panel installers', 'community centers')
        location: Location to search around (e.g. 'Orlando, FL' or '28.5383,-81.3792')
        radius: Search radius in meters (default 5000m = 5km)
    """
    api_key, err = _get_api_key()
    if err:
        err["query"] = query
        return err
    try:
        resolved, loc_err = _resolve_location(location)
        if loc_err:
            loc_err["query"] = query
            return loc_err

        params = {"key": api_key, "keyword": query, "radius": int(radius), "type": "establishment"}
        if resolved:
            params["location"] = resolved

        data = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params=params, timeout=30,
        ).json()

        if data.get("status") != "OK":
            return {"success": False, "error": f"Google Maps API error: {data.get('status')}",
                    "query": query, "location": resolved, "radius": radius}

        places = []
        for p in data.get("results", []):
            places.append({
                "name": p.get("name", ""),
                "address": p.get("vicinity", ""),
                "rating": p.get("rating", 0),
                "user_ratings_total": p.get("user_ratings_total", 0),
                "price_level": p.get("price_level", 0),
                "types": p.get("types", []),
                "place_id": p.get("place_id", ""),
                "location": {
                    "lat": p.get("geometry", {}).get("location", {}).get("lat", 0),
                    "lng": p.get("geometry", {}).get("location", {}).get("lng", 0),
                },
                "business_status": p.get("business_status", "Unknown"),
            })

        return {
            "success": True, "query": query, "location": resolved,
            "radius": radius, "total_results": len(places),
            "places": places[:10],
            "insights": f"Found {len(places)} places matching '{query}' within {radius}m radius",
        }
    except Exception as e:
        logger.error(f"Error searching Google Maps: {e}")
        return {"success": False, "error": f"Search failed: {e}", "query": query, "location": location}


# ─── get_directions ───────────────────────────────────────────────

def get_directions(origin: str = "", destination: str = "", mode: str = "driving",
                   waypoints: str = "", avoid: str = "", **kw) -> dict:
    """Get directions between two locations using Google Maps Directions API.

    Parameters:
        origin: Starting location (address or 'lat,lng')
        destination: Ending location (address or 'lat,lng')
        mode: Travel mode — 'driving', 'walking', 'bicycling', 'transit'
        waypoints: Optional intermediate stops (comma-separated addresses)
        avoid: Features to avoid — 'tolls', 'highways', 'ferries'
    """
    api_key, err = _get_api_key()
    if err:
        return err
    try:
        params = {"key": api_key, "origin": origin, "destination": destination,
                  "mode": mode, "units": "imperial"}
        if waypoints:
            wp_list = [w.strip() for w in waypoints.split(",")] if isinstance(waypoints, str) else waypoints
            params["waypoints"] = f"optimize:true|{'|'.join(wp_list)}"
        if avoid:
            params["avoid"] = avoid if isinstance(avoid, str) else "|".join(avoid)

        data = requests.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params=params, timeout=30,
        ).json()

        if data.get("status") != "OK":
            return {"success": False,
                    "error": f"Directions API error: {data.get('status')}",
                    "origin": origin, "destination": destination}

        route = (data.get("routes") or [{}])[0]
        leg = (route.get("legs") or [{}])[0]

        steps = []
        for s in leg.get("steps", []):
            instr = (s.get("html_instructions", "")
                     .replace("<b>", "**").replace("</b>", "**")
                     .replace("<div>", "\n").replace("</div>", ""))
            steps.append({
                "distance": s.get("distance", {}).get("text", ""),
                "duration": s.get("duration", {}).get("text", ""),
                "instructions": instr,
                "maneuver": s.get("maneuver", ""),
                "travel_mode": s.get("travel_mode", ""),
            })

        result = {
            "success": True, "origin": origin, "destination": destination,
            "mode": mode,
            "summary": route.get("summary", ""),
            "total_distance": leg.get("distance", {}).get("text", ""),
            "total_duration": leg.get("duration", {}).get("text", ""),
            "steps": steps, "step_count": len(steps),
        }
        return result
    except Exception as e:
        logger.error(f"Error getting directions: {e}")
        return {"success": False, "error": f"Directions failed: {e}",
                "origin": origin, "destination": destination}


# ─── find_nearby_places ──────────────────────────────────────────

def find_nearby_places(location: str = "", place_type: str = "", radius: int = 5000,
                       keyword: str = "", min_price: int = -1, max_price: int = -1, **kw) -> dict:
    """Find places of a specific type near a location.

    Parameters:
        location: Location coordinates ('lat,lng') or address
        place_type: Type of place ('restaurant', 'gas_station', 'hospital', etc.)
        radius: Search radius in meters (default 5000m = 5km)
        keyword: Additional keyword to filter results
        min_price: Minimum price level (0-4, -1 to skip)
        max_price: Maximum price level (0-4, -1 to skip)
    """
    api_key, err = _get_api_key()
    if err:
        err["place_type"] = place_type
        return err
    try:
        resolved, loc_err = _resolve_location(location)
        if loc_err:
            loc_err["place_type"] = place_type
            return loc_err

        params = {"key": api_key, "type": place_type, "radius": int(radius)}
        if resolved:
            params["location"] = resolved
        if keyword:
            params["keyword"] = keyword
        if min_price is not None and int(min_price) >= 0:
            params["minprice"] = int(min_price)
        if max_price is not None and int(max_price) >= 0:
            params["maxprice"] = int(max_price)

        data = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params=params, timeout=30,
        ).json()

        if data.get("status") != "OK":
            return {"success": False,
                    "error": f"Places API error: {data.get('status')}",
                    "location": resolved, "place_type": place_type, "radius": radius}

        places = []
        for p in data.get("results", []):
            info = {
                "name": p.get("name", ""),
                "address": p.get("vicinity", ""),
                "rating": p.get("rating", 0),
                "user_ratings_total": p.get("user_ratings_total", 0),
                "price_level": p.get("price_level", 0),
                "types": p.get("types", []),
                "place_id": p.get("place_id", ""),
                "location": {
                    "lat": p.get("geometry", {}).get("location", {}).get("lat", 0),
                    "lng": p.get("geometry", {}).get("location", {}).get("lng", 0),
                },
                "business_status": p.get("business_status", "Unknown"),
            }
            # Distance from search center
            if resolved and "," in resolved:
                try:
                    clat, clng = [float(x) for x in resolved.split(",")]
                    d = _haversine_meters(clat, clng, info["location"]["lat"], info["location"]["lng"])
                    info["distance_meters"] = int(d)
                    info["distance_text"] = f"{d / 1609.34:.1f} miles" if d > 1609 else f"{d:.0f} feet"
                except Exception:
                    info["distance_meters"] = 0
                    info["distance_text"] = "Unknown"
            places.append(info)

        places.sort(key=lambda x: (-x.get("rating", 0), x.get("distance_meters", 999999)))

        return {
            "success": True, "location": resolved, "place_type": place_type,
            "radius": radius, "keyword": keyword,
            "total_results": len(places), "places": places[:15],
            "insights": f"Found {len(places)} {place_type.replace('_', ' ')} places within {radius}m radius",
        }
    except Exception as e:
        logger.error(f"Error finding nearby places: {e}")
        return {"success": False, "error": f"Places search failed: {e}",
                "location": location, "place_type": place_type}
