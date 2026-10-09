"""Things @ai can do while answering: look up weather and places, and draw pictures.

The AI decides when it needs one of these ("what's the weather in Mandi?"),
asks for it with a "tool call", and we run the matching function below and
hand back the result. All services used here are free and need no API key:

  Open-Meteo forecast   https://open-meteo.com          weather
  Open-Meteo geocoding  https://open-meteo.com          place name -> coordinates
  OpenStreetMap         https://nominatim.org           coordinates -> place name
                        (used when someone shares their location)
"""
import json
import logging
import math

import httpx

from .config import AI_DEFAULT_LATITUDE, AI_DEFAULT_LONGITUDE, AI_DEFAULT_PLACE
from .imagegen import ImageGenError, check_quota, generate_image as make_picture, image_quota_key, made_images

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
SEARCH_URL = "https://nominatim.openstreetmap.org/search"
# OpenStreetMap asks every app to identify itself.
USER_AGENT = "UhlWave-campus-chat/1.0 (student project; https://github.com)"

# What the WMO weather codes mean, in plain words.
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


class ToolError(Exception):
    """A problem worth telling the AI about (place not found, service down...)."""


async def _get_json(url: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


# ---- places ----------------------------------------------------------------------

async def find_place(query: str, count: int = 3) -> list[dict]:
    """Places matching a name, best match first."""
    query = (query or "").strip()
    if not query:
        raise ToolError("No place name given.")
    data = await _get_json(GEOCODE_URL, {"name": query, "count": count, "language": "en", "format": "json"})
    results = data.get("results") or []
    if not results and "," in query:
        # The geocoder wants just a place name: "Kamand, Himachal" -> try "Kamand".
        return await find_place(query.split(",")[0], count)
    if not results:
        raise ToolError(f"Couldn't find a place called {query!r}.")
    return [
        {
            "name": r.get("name"),
            "region": r.get("admin1"),
            "country": r.get("country"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "elevation_m": r.get("elevation"),
            "timezone": r.get("timezone"),
            "population": r.get("population"),
        }
        for r in results
    ]


def _label(place: dict) -> str:
    return ", ".join(p for p in (place.get("name"), place.get("region"), place.get("country")) if p)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line ("as the crow flies") distance on the Earth's surface."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def distance_between(from_place: str, to_place: str) -> dict:
    a, b = (await find_place(from_place, 1))[0], (await find_place(to_place, 1))[0]
    km = distance_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"])
    return {
        "from": _label(a), "to": _label(b),
        "straight_line_km": round(km, 1),
        "note": "Straight-line distance. Roads, especially in the hills, are usually much longer.",
    }


async def reverse_geocode(latitude: float, longitude: float) -> str | None:
    """A readable name for some coordinates, like "Kamand, Mandi, Himachal Pradesh". None if unknown."""
    try:
        data = await _get_json(REVERSE_URL, {"format": "jsonv2", "lat": latitude, "lon": longitude, "zoom": 14})
    except Exception:
        log.warning("Reverse geocoding failed", exc_info=True)
        return None
    address = data.get("address") or {}
    parts = [
        address.get("suburb") or address.get("village") or address.get("town") or address.get("city") or address.get("hamlet"),
        address.get("county") or address.get("state_district"),
        address.get("state"),
    ]
    seen, label = set(), []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            label.append(part)
    return ", ".join(label) or data.get("display_name")


async def search_places(query: str, near: tuple[float, float] | None = None) -> list[dict]:
    """Places and buildings matching a search, for the "pick a place" map. Uses OpenStreetMap."""
    params = {"format": "jsonv2", "q": query, "limit": 8, "addressdetails": 0}
    if near:
        lat, lon = near
        params["viewbox"] = f"{lon - 0.5},{lat + 0.5},{lon + 0.5},{lat - 0.5}"  # prefer nearby results
    data = await _get_json(SEARCH_URL, params)
    out = []
    for r in data if isinstance(data, list) else []:
        try:
            out.append({
                "name": r.get("name") or (r.get("display_name") or "").split(",")[0],
                "address": r.get("display_name"),
                "latitude": float(r["lat"]),
                "longitude": float(r["lon"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---- weather -----------------------------------------------------------------------

async def get_weather(place: str | None = None, latitude: float | None = None,
                      longitude: float | None = None, days: int = 1) -> dict:
    """Current weather plus a short daily forecast."""
    if place:
        found = (await find_place(place, 1))[0]
        latitude, longitude, label = found["latitude"], found["longitude"], _label(found)
    elif latitude is not None and longitude is not None:
        label = f"{latitude:.3f}, {longitude:.3f}"
    else:
        latitude, longitude, label = AI_DEFAULT_LATITUDE, AI_DEFAULT_LONGITUDE, AI_DEFAULT_PLACE
    if not (-90 <= float(latitude) <= 90 and -180 <= float(longitude) <= 180):
        raise ToolError("Those coordinates aren't on Earth.")
    days = max(1, min(int(days or 1), 7))

    data = await _get_json(FORECAST_URL, {
        "latitude": latitude, "longitude": longitude, "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,sunrise,sunset",
    })
    now, daily = data.get("current") or {}, data.get("daily") or {}
    forecast = []
    for i, day in enumerate(daily.get("time") or []):
        forecast.append({
            "date": day,
            "conditions": WEATHER_CODES.get(daily["weather_code"][i], "unknown"),
            "max_c": daily["temperature_2m_max"][i],
            "min_c": daily["temperature_2m_min"][i],
            "chance_of_rain_percent": daily["precipitation_probability_max"][i],
            "sunrise": daily["sunrise"][i][-5:],
            "sunset": daily["sunset"][i][-5:],
        })
    return {
        "place": label,
        "local_time": now.get("time"),
        "timezone": data.get("timezone"),
        "now": {
            "conditions": WEATHER_CODES.get(now.get("weather_code"), "unknown"),
            "temperature_c": now.get("temperature_2m"),
            "feels_like_c": now.get("apparent_temperature"),
            "humidity_percent": now.get("relative_humidity_2m"),
            "wind_kmh": now.get("wind_speed_10m"),
            "precipitation_mm": now.get("precipitation"),
            "daytime": bool(now.get("is_day")),
        },
        "forecast": forecast,
        "source": "Open-Meteo",
    }


# ---- pictures ----------------------------------------------------------------------

async def generate_image(prompt: str) -> dict:
    """Draw a picture. It's posted in the chat after the AI's reply."""
    pending = made_images.get()
    if pending is None:
        raise ToolError("Pictures can only be made inside a chat.")
    if len(pending) >= 2:
        raise ToolError("You've already made two pictures for this message.")
    try:
        check_quota(image_quota_key.get())
        data, content_type = await make_picture(prompt)
    except ImageGenError as error:
        raise ToolError(str(error))
    pending.append({"prompt": prompt, "data": data, "content_type": content_type})
    return {"ok": True, "note": "The picture is ready and will appear in the chat right after your message. Don't describe it in detail or include a link."}


# ---- what the AI is told it can call ---------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Current weather and a daily forecast. Give a place name, or latitude and longitude "
                "(for example from a location someone shared in the chat). With neither, it uses "
                f"the default campus location ({AI_DEFAULT_PLACE})."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "City or town name, e.g. 'Mandi' or 'Delhi'."},
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "days": {"type": "integer", "description": "Days of forecast, 1 to 7. Default 1.", "minimum": 1, "maximum": 7},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_place",
            "description": "Look up a place by name: its region, country, coordinates, elevation, time zone and population.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Place name, e.g. 'Kamand' or 'Manali'."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "distance_between",
            "description": "Straight-line distance in kilometres between two named places.",
            "parameters": {
                "type": "object",
                "properties": {"from_place": {"type": "string"}, "to_place": {"type": "string"}},
                "required": ["from_place", "to_place"],
            },
        },
    },
]

TOOLS.append({
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Draw a picture from a description and post it in the chat. Use it when someone asks you to "
            "draw, make, create, generate or imagine an image, picture, poster, logo, meme or sticker."
        ),
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "A clear, detailed English description of the picture."}},
            "required": ["prompt"],
        },
    },
})

_FUNCTIONS = {
    "get_weather": get_weather, "find_place": find_place, "distance_between": distance_between,
    "generate_image": generate_image,
}


async def run_tool(name: str, arguments: str) -> str:
    """Run a tool the AI asked for. Always returns a JSON string, even on errors,
    so the AI can explain the problem instead of the reply failing."""
    function = _FUNCTIONS.get(name)
    if function is None:
        return json.dumps({"error": f"There's no tool called {name!r}."})
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return json.dumps({"error": "The tool arguments weren't valid JSON."})
    try:
        result = await function(**args)
        return json.dumps(result, ensure_ascii=False)
    except ToolError as error:
        return json.dumps({"error": str(error)})
    except TypeError:
        return json.dumps({"error": f"Wrong arguments for {name}."})
    except Exception:
        log.warning("Tool %s failed", name, exc_info=True)
        return json.dumps({"error": f"The {name} service didn't respond. Try again later."})
