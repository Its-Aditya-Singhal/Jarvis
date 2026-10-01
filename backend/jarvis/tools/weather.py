"""Weather from Open-Meteo: free, no API key, and only the city name leaves the Mac.

Two requests: the city's coordinates (geocoding), then today's and tomorrow's forecast.
Coordinates are kept per city for the session, so later questions make one request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

log = logging.getLogger(__name__)

HOSTS = ("open-meteo.com",)  # geocoding-api.open-meteo.com and api.open-meteo.com
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes -> spoken words (English, Hindi)
CODES: dict[int, tuple[str, str]] = {
    0: ("clear", "साफ़"), 1: ("mostly clear", "ज़्यादातर साफ़"), 2: ("partly cloudy", "थोड़े बादल"),
    3: ("cloudy", "बादल"), 45: ("foggy", "कोहरा"), 48: ("foggy", "कोहरा"),
    51: ("light drizzle", "हल्की बूँदाबाँदी"), 53: ("drizzle", "बूँदाबाँदी"), 55: ("heavy drizzle", "तेज़ बूँदाबाँदी"),
    56: ("freezing drizzle", "जमने वाली बूँदाबाँदी"), 57: ("freezing drizzle", "जमने वाली बूँदाबाँदी"),
    61: ("light rain", "हल्की बारिश"), 63: ("rain", "बारिश"), 65: ("heavy rain", "तेज़ बारिश"),
    66: ("freezing rain", "जमने वाली बारिश"), 67: ("freezing rain", "जमने वाली बारिश"),
    71: ("light snow", "हल्की बर्फ़"), 73: ("snow", "बर्फ़"), 75: ("heavy snow", "भारी बर्फ़"), 77: ("snow", "बर्फ़"),
    80: ("light showers", "हल्की बौछारें"), 81: ("showers", "बौछारें"), 82: ("heavy showers", "तेज़ बौछारें"),
    85: ("snow showers", "बर्फ़ की बौछारें"), 86: ("snow showers", "बर्फ़ की बौछारें"),
    95: ("thunderstorms", "आँधी-तूफ़ान"), 96: ("thunderstorms with hail", "ओलों के साथ तूफ़ान"),
    99: ("thunderstorms with hail", "ओलों के साथ तूफ़ान"),
}


class WeatherError(RuntimeError):
    pass


@dataclass
class Place:
    name: str
    lat: float
    lon: float


@dataclass
class Forecast:
    place: str
    now_c: float | None
    feels_c: float | None
    now_code: int | None
    day: date
    high_c: float
    low_c: float
    rain_pct: int | None
    day_code: int | None

    def words(self, code: int | None, hi: bool) -> str:
        en, hin = CODES.get(code if code is not None else -1, ("", ""))
        return hin if hi else en


class Weather:
    def __init__(self, transport: httpx.BaseTransport | None = None, timeout_s: float = 8.0):
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=4.0), transport=transport)
        self._places: dict[str, Place] = {}

    def place(self, name: str) -> Place:
        key = " ".join(name.lower().split())
        if key in self._places:
            return self._places[key]
        city = name.split(",")[0].strip()
        try:
            r = self._http.get(GEOCODE, params={"name": city, "count": 5, "language": "en", "format": "json"})
            r.raise_for_status()
            results = r.json().get("results") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError("I can't reach the weather service right now") from exc
        if not results:
            raise WeatherError(f"I don't know a place called {city}")
        hint = name.split(",", 1)[1].strip().lower() if "," in name else ""
        best = next((x for x in results if hint and hint in f"{x.get('country', '')} {x.get('admin1', '')}".lower()),
                    results[0])  # "Paris, Texas" picks Texas; else the biggest match (the API sorts by size)
        p = Place(str(best.get("name") or city), float(best["latitude"]), float(best["longitude"]))
        self._places[key] = p
        return p

    def forecast(self, name: str, tomorrow: bool = False) -> Forecast:
        p = self.place(name)
        try:
            r = self._http.get(FORECAST, params={
                "latitude": p.lat, "longitude": p.lon, "timezone": "auto", "forecast_days": 2,
                "current": "temperature_2m,apparent_temperature,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError("I can't reach the weather service right now") from exc
        cur, daily = data.get("current") or {}, data.get("daily") or {}
        i = 1 if tomorrow else 0
        try:
            day = date.fromisoformat(str(daily["time"][i]))
            high, low = float(daily["temperature_2m_max"][i]), float(daily["temperature_2m_min"][i])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise WeatherError("the weather service sent an answer I couldn't read") from exc

        def at(key: str) -> int | None:
            try:
                v = daily[key][i]
                return None if v is None else int(v)
            except (KeyError, IndexError, TypeError, ValueError):
                return None

        def num(v) -> float | None:
            return float(v) if isinstance(v, (int, float)) else None

        return Forecast(p.name, None if tomorrow else num(cur.get("temperature_2m")),
                        None if tomorrow else num(cur.get("apparent_temperature")),
                        None if tomorrow else (int(cur["weather_code"]) if isinstance(cur.get("weather_code"), (int, float)) else None),
                        day, high, low, at("precipitation_probability_max"), at("weather_code"))


def spoken(f: Forecast, hi: bool, tomorrow: bool = False) -> str:
    """'In Pune it's 27 degrees and partly cloudy, feels like 29. Today: up to 31, down to 22, 40% chance of rain.'"""
    deg = lambda x: f"{round(x)}"
    rain = f.rain_pct
    if hi:
        out_hi = []
        if f.now_c is not None:
            w = f.words(f.now_code, True)
            out_hi.append(f"{f.place} में अभी {deg(f.now_c)} डिग्री है" + (f", {w}" if w else "") + "।")
        day = f"कल {f.place} में" if tomorrow else "आज"
        out_hi.append(f"{day} अधिकतम {deg(f.high_c)} और न्यूनतम {deg(f.low_c)} डिग्री"
                      + (f", बारिश की संभावना {rain}%" if rain else "") + "।")
        return " ".join(out_hi)
    out = []
    if f.now_c is not None:
        now = f"In {f.place} it's {deg(f.now_c)} degrees" + (f" and {f.words(f.now_code, False)}" if f.now_code is not None else "")
        if f.feels_c is not None and abs(f.feels_c - f.now_c) >= 2:
            now += f", feels like {deg(f.feels_c)}"
        out.append(now + ".")
        out.append(f"Today: a high of {deg(f.high_c)} and a low of {deg(f.low_c)}"
                   + (f", {rain}% chance of rain." if rain else "."))
    else:
        what = f.words(f.day_code, False)
        out.append(f"Tomorrow in {f.place}: {what + ', ' if what else ''}a high of {deg(f.high_c)} and a low of {deg(f.low_c)}"
                   + (f", {rain}% chance of rain." if rain else "."))
    return " ".join(out)
