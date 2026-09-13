import webbrowser
from urllib.parse import quote_plus

import requests

_GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT      = 8

# WMO weather interpretation codes, as Open-Meteo returns them.
_WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with light hail",
    99: "thunderstorm with heavy hail",
}


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city     = parameters.get("city")
    when     = parameters.get("time", "today")

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip()

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = f"Sir, I couldn't open the browser for the weather report: {e}"
        _log(msg, player)
        return msg

    msg = f"Showing the weather for {city}, {when}, sir."
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    # The model speaks whatever comes back. A bare "showing the weather" gave
    # it nothing to say, and it filled the gap with a made-up placeholder.
    try:
        return _fetch_weather(city)
    except LookupError:
        return (f"{msg} No place called {city} was found, so there are no "
                "figures to give. Say so; do not invent any.")
    except Exception as e:
        print(f"[Weather] Could not fetch figures: {e}")
        return (f"{msg} The weather figures could not be fetched, so there are "
                "none to give. Say so; do not invent any.")


def _fetch_weather(city: str) -> str:
    """Current conditions and today's forecast for city, from Open-Meteo."""
    name, *regions = [part.strip() for part in city.split(",")]

    geo = requests.get(
        _GEOCODE_URL,
        params={"name": name, "count": 10, "language": "en"},
        timeout=_TIMEOUT,
    )
    geo.raise_for_status()
    places = geo.json().get("results") or []

    # "Greeneville, Tennessee" or "Paris, FR": prefer the place in that region.
    wanted = {r.lower() for r in regions if r}
    if wanted:
        places = [
            p for p in places
            if wanted & {str(p.get(k, "")).lower() for k in ("admin1", "country", "country_code")}
        ] or places
    if not places:
        raise LookupError(city)
    place = places[0]

    fc = requests.get(
        _FORECAST_URL,
        params={
            "latitude":      place["latitude"],
            "longitude":     place["longitude"],
            "timezone":      "auto",
            "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "wind_speed_10m,weather_code",
            "daily":   "weather_code,temperature_2m_max,temperature_2m_min,"
                       "precipitation_probability_max",
        },
        timeout=_TIMEOUT,
    )
    fc.raise_for_status()
    data  = fc.json()
    now   = data["current"]
    today = {key: values[0] for key, values in data["daily"].items()}

    where = ", ".join(
        str(place[k]) for k in ("name", "admin1", "country") if place.get(k)
    )
    wind = now["wind_speed_10m"]
    report = (
        f"Weather for {where}, local time {now['time'][11:16]}: "
        f"{_describe(now['weather_code'])}, {_temp(now['temperature_2m'])}, "
        f"feels like {_temp(now['apparent_temperature'])}, "
        f"humidity {now['relative_humidity_2m']}%, "
        f"wind {wind:.0f} km/h ({wind / 1.609:.0f} mph). "
        f"Today: {_describe(today['weather_code'])}, "
        f"high {_temp(today['temperature_2m_max'])}, "
        f"low {_temp(today['temperature_2m_min'])}"
    )
    rain = today.get("precipitation_probability_max")
    if rain is not None:
        report += f", {rain}% chance of precipitation"
    return report + "."


def _temp(celsius: float) -> str:
    return f"{celsius:.0f} °C ({celsius * 9 / 5 + 32:.0f} °F)"


def _describe(code) -> str:
    return _WMO_CODES.get(code, f"weather code {code}")


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"LUMINA: {message}")
        except Exception:
            pass
