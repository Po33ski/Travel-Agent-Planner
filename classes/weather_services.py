import json
import os
from typing import Any, Dict
from agent_framework import tool
import requests

from ..utils import normalize_sunrise_sunset

API_HTTP = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/"

class WeatherService:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_forecast(city: str) -> Dict[str, Any]:
        """
        Fetch weather forecast data for a given city using the Visual Crossing API.
        Returns a dictionary with weather data, or {"error": "message"} on failure.

        Args:
            city: The city name

        Returns:
            Dict containing weather data from API, or {"error": "..."} if the call failed.
        """
        if not city:
            return {"error": "No city provided."}

        api_key = self.api_key
        if not api_key:
            return {"error": "Weather service API key is not configured."}

        url = f"{API_HTTP}{city}?unitGroup=metric&key={api_key}&contentType=json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            weather_data = response.json()
            return normalize_sunrise_sunset(weather_data)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                return {"error": f"City '{city}' not found or invalid."}
            return {"error": f"Weather service error ({e.response.status_code})."}
        except requests.exceptions.Timeout:
            return {"error": "Weather service request timed out."}
        except requests.exceptions.RequestException:
            return {"error": "Weather service is temporarily unavailable."}
        except json.JSONDecodeError:
            return {"error": "Weather service returned invalid data."}

    def get_current_weather(city: str) -> Dict[str, Any]:
        """
        Fetch current weather data for a given city using the Visual Crossing API.
        Returns a dictionary with weather data, or {"error": "message"} on failure.

        Args:
            city: The city name

        Returns:
            Dict containing weather data from API, or {"error": "..."} if the call failed.
        """
        if not city:
            return {"error": "No city provided."}

        api_key = self.api_key
        if not api_key:
            return {"error": "Weather service API key is not configured."}

        url = f"{API_HTTP}{city}?unitGroup=metric&key={api_key}&contentType=json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            weather_data = response.json()
            return normalize_sunrise_sunset(weather_data)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                return {"error": f"City '{city}' not found or invalid."}
            return {"error": f"Weather service error ({e.response.status_code})."}
        except requests.exceptions.Timeout:
            return {"error": "Weather service request timed out."}
        except requests.exceptions.RequestException:
            return {"error": "Weather service is temporarily unavailable."}
        except json.JSONDecodeError:
            return {"error": "Weather service returned invalid data."}

@tool
def get_forecast_weather(location: str, date_frame: str) -> Dict[str, Any]:
    """
    Tool function to get weather forecast for a given location and date frame.
    Returns a dictionary with weather data, or {"error": "message"} on failure.

    Args:
        location: The location for which to get weather information.
        date_frame: The date frame for the weather information (e.g., '5/10/2026', '5/12/2026-5/15/2026').

    Returns:
        Dict containing weather data from API, or {"error": "..."} if the call failed.
    """
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        return {"error": "Weather service API key is not configured."}

    weather_service = WeatherService(api_key)
    return weather_service.get_forecast(location)

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
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        return {"error": "Weather service API key is not configured."}

    weather_service = WeatherService(api_key)
    return weather_service.get_current_weather(location)