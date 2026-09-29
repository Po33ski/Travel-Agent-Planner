import json
import os
from typing import Any, Dict
from urllib.parse import quote
from agent_framework import tool
import requests

from utils.utils import normalize_sunrise_sunset

API_HTTP = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/"

# Daily fields the agent needs; hourly data is skipped to keep tool output small
FORECAST_ELEMENTS = (
    "datetime,tempmax,tempmin,feelslike,precip,precipprob,preciptype,"
    "windspeed,conditions,description,sunrise,sunset"
)

class WeatherService:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_forecast(self, city: str, start_date: str | None = None, end_date: str | None = None) -> Dict[str, Any]:
        """
        Fetch daily weather forecast data for a given city using the Visual Crossing API.
        Returns a dictionary with weather data, or {"error": "message"} on failure.

        Args:
            city: The city name
            start_date: First day in YYYY-MM-DD format. Omit for the next 15 days.
            end_date: Last day in YYYY-MM-DD format. Used only with start_date.

        Returns:
            Dict containing weather data from API, or {"error": "..."} if the call failed.
        """
        path = quote(city or "", safe="")
        if start_date:
            path += f"/{start_date}" + (f"/{end_date}" if end_date else "")
        return self._fetch(city, path, include="days", elements=FORECAST_ELEMENTS)

    def get_current_weather(self, city: str) -> Dict[str, Any]:
        """
        Fetch current weather data for a given city using the Visual Crossing API.
        Returns a dictionary with weather data, or {"error": "message"} on failure.

        Args:
            city: The city name

        Returns:
            Dict containing weather data from API, or {"error": "..."} if the call failed.
        """
        return self._fetch(city, quote(city or "", safe=""), include="current")

    def _fetch(self, city: str, path: str, **params: str) -> Dict[str, Any]:
        """Call the timeline API for `path` (city[/start[/end]]) and normalise the response."""
        if not city:
            return {"error": "No city provided."}

        if not self.api_key:
            return {"error": "Weather service API key is not configured."}

        try:
            response = requests.get(
                f"{API_HTTP}{path}",
                params={"unitGroup": "metric", "key": self.api_key, "contentType": "json", **params},
                timeout=10,
            )
            response.raise_for_status()
            weather_data = response.json()
            return normalize_sunrise_sunset(weather_data)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                return {"error": f"City '{city}' not found or invalid date (use YYYY-MM-DD)."}
            return {"error": f"Weather service error ({e.response.status_code})."}
        except requests.exceptions.Timeout:
            return {"error": "Weather service request timed out."}
        except requests.exceptions.RequestException:
            return {"error": "Weather service is temporarily unavailable."}
        except json.JSONDecodeError:
            return {"error": "Weather service returned invalid data."}

@tool
def get_forecast_weather(location: str, start_date: str | None = None, end_date: str | None = None) -> Dict[str, Any]:
    """
    Tool function to get the daily weather forecast for a given location and optional date range.
    Returns a dictionary with weather data, or {"error": "message"} on failure.

    Args:
        location: The location for which to get weather information.
        start_date: First day in YYYY-MM-DD format. Omit both dates for the next 15 days.
        end_date: Last day in YYYY-MM-DD format. Use only with start_date.

    Returns:
        Dict containing weather data from API, or {"error": "..."} if the call failed.
    """
    api_key = os.getenv("VISUAL_CROSSING_API_KEY")
    if not api_key:
        return {"error": "Weather service API key is not configured."}

    weather_service = WeatherService(api_key)
    return weather_service.get_forecast(location, start_date, end_date)

@tool
def get_current_weather(location: str) -> Dict[str, Any]:
    """
    Tool function to get current weather for a given location.
    Returns a dictionary with weather data, or {"error": "message"} on failure.

    Args:
        location: The location for which to get current weather information.

    Returns:
        Dict containing weather data from API, or {"error": "..."} if the call failed.
    """
    api_key = os.getenv("VISUAL_CROSSING_API_KEY")
    if not api_key:
        return {"error": "Weather service API key is not configured."}

    weather_service = WeatherService(api_key)
    return weather_service.get_current_weather(location)
