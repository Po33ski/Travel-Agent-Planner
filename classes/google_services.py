import json
import os
from typing import Any, Dict
from agent_framework import tool
# from azure.identity import DefaultAzureCredential
# from azure.keyvault.secrets import SecretClient
import requests

# from classes.secret_manager_services import SecretManager

# For now the key is read from the environment: launch.json loads it from .env
API_KEY_ENV = "GOOGLE_API_KEY"

# Same Key Vault mechanism as in agent_execution.py. The template already stores the key as GOOGLE-API-KEY:
# to switch, uncomment these lines, the imports above and the Key Vault lines in search_hotels_google_maps.
# key_vault_url = os.getenv("KEY_VAULT_URL")
# credential = DefaultAzureCredential()
# secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
# secret_manager = SecretManager(secret_client)

# Text Search (New) of the Places API. The endpoint is shared by all users; the API key identifies the caller.
API_HTTP = "https://places.googleapis.com/v1/places:searchText"

# Fields returned for each place; the field mask is required. Google bills a request by its most
# expensive field: editorialSummary, reviews and the amenity fields (allowsDogs, goodForChildren,
# parkingOptions) make it a Text Search Enterprise + Atmosphere call.
FIELD_MASK = (
    "places.displayName,places.primaryTypeDisplayName,places.formattedAddress,"
    "places.rating,places.userRatingCount,places.priceLevel,"
    "places.editorialSummary,places.reviews,"
    "places.allowsDogs,places.goodForChildren,places.parkingOptions,"
    "places.websiteUri,places.googleMapsUri"
)
# The API returns at most 20 places per page
MAX_RESULTS = 20
# Reviews are the largest part of the response; a few shortened ones per hotel keep the tool output small
MAX_REVIEWS = 3
MAX_REVIEW_CHARS = 300


class GooglePlacesService:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_hotels(self, city: str, preferences: str | None = None, language: str = "en") -> Dict[str, Any]:
        """
        Search for hotels in a given city using the Google Maps Places API (Text Search).
        Returns structured hotel data, so the agent can pick the hotels that best match
        the user's preferences, or {"error": "message"} on failure.

        The Places API has no room prices and no availability for travel dates;
        `price_level` is only a general price category and is often missing for hotels.

        Args:
            city: The city name to search hotels in.
            preferences: What the user is looking for, as a few keywords (optional). They are added
                to the search text, so Google returns the hotels that match them first.
            language: ISO 639-1 language code of the chat; the results are returned in this language.

        Returns:
            Dict with "city", "preferences" and "hotels" list (each entry has name and, when Google
            knows them, type, address, rating, reviews_count, price_level, summary, reviews,
            allows_dogs, good_for_children, parking, website, google_maps_url),
            or {"error": "..."} on failure.
        """
        if not city:
            return {"error": "No city provided."}

        if not self.api_key:
            return {"error": "Google Maps API key is not configured."}

        language = (language or "en").strip().lower()
        preferences = (preferences or "").strip()

        result = self._fetch(self._build_query(city, language, preferences), language)
        note = None
        if preferences and "error" not in result and not result.get("places"):
            # The preferences were too narrow for the text search: fall back to all hotels in the city
            result = self._fetch(self._build_query(city, language), language)
            note = "No hotels matched the preferences; these are general results for the city."

        if "error" in result:
            return result

        places = result.get("places", [])
        if not places:
            return {"error": f"No hotel results found for '{city}'."}

        search_result: Dict[str, Any] = {"city": city}
        if preferences:
            search_result["preferences"] = preferences
        if note:
            search_result["note"] = note
        search_result["hotels"] = [self._simplify_place(place) for place in places]
        return search_result

    def _fetch(self, query: str, language: str) -> Dict[str, Any]:
        """Run one text search and return the raw API response, or {"error": "..."} on failure."""
        try:
            response = requests.post(
                API_HTTP,
                headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK},
                json={
                    "textQuery": query,
                    "includedType": "lodging",
                    "languageCode": language,
                    "pageSize": MAX_RESULTS,
                },
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            return {"error": f"Google Maps service error ({e.response.status_code}): {self._error_message(e.response)}"}
        except requests.exceptions.Timeout:
            return {"error": "Google Maps service request timed out."}
        except json.JSONDecodeError:
            return {"error": "Google Maps service returned invalid data."}
        except requests.exceptions.RequestException:
            return {"error": "Google Maps service is temporarily unavailable."}

    @staticmethod
    def _simplify_place(place: Dict[str, Any]) -> Dict[str, Any]:
        """Keep the fields the agent needs to compare a hotel with the user's preferences."""
        reviews = [
            {"rating": review.get("rating"), "text": review["text"]["text"][:MAX_REVIEW_CHARS]}
            for review in place.get("reviews", [])
            if review.get("text", {}).get("text")
        ][:MAX_REVIEWS]

        hotel = {
            "name": place.get("displayName", {}).get("text"),
            "type": place.get("primaryTypeDisplayName", {}).get("text"),
            "address": place.get("formattedAddress"),
            "rating": place.get("rating"),
            "reviews_count": place.get("userRatingCount"),
            # PRICE_LEVEL_MODERATE -> moderate
            "price_level": (place.get("priceLevel") or "").removeprefix("PRICE_LEVEL_").lower(),
            "summary": place.get("editorialSummary", {}).get("text"),
            "reviews": reviews,
            "allows_dogs": place.get("allowsDogs"),
            "good_for_children": place.get("goodForChildren"),
            # Only the parking options the hotel has, e.g. ["freeParkingLot", "paidGarageParking"]
            "parking": [option for option, available in place.get("parkingOptions", {}).items() if available],
            "website": place.get("websiteUri"),
            "google_maps_url": place.get("googleMapsUri"),
        }
        # Google leaves out what it does not know; the same fields are left out here
        return {field: value for field, value in hotel.items() if value not in (None, "", [])}

    @staticmethod
    def _build_query(city: str, language: str, preferences: str = "") -> str:
        """Build a short search query in the language of the chat."""
        query = f"hotele {city}" if language.startswith("pl") else f"hotels in {city}"
        return f"{query}, {preferences}" if preferences else query

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        """Google explains a failed call in the response body, e.g. an invalid key or a disabled API."""
        try:
            return response.json().get("error", {}).get("message", "no details")
        except ValueError:
            return "no details"


@tool
def search_hotels_google_maps(city: str, preferences: str | None = None, language: str = "en") -> Dict[str, Any]:
    """
    Tool function to search for hotels in a given city on Google Maps.
    Returns a dictionary with structured hotel data, or {"error": "message"} on failure.

    Args:
        city: The city in which to search for hotels.
        preferences: What the user is looking for, as a few keywords, e.g. "quiet, spa, near the Old Town" (optional).
        language: ISO 639-1 language code of the chat, e.g. "pl" or "en".

    Returns:
        Dict with "city" and "hotels" (name and, when available, type, address, rating, reviews_count,
        price_level, summary, reviews, allows_dogs, good_for_children, parking, website, google_maps_url),
        or {"error": "..."} if the call failed.
    """
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        return {"error": f"Google Maps API key ({API_KEY_ENV}) is not configured."}

    # Key Vault version, replaces the three lines above:
    # try:
    #     api_key = secret_manager.get_secret("GOOGLE-API-KEY")
    # except Exception:
    #     return {"error": "Google Maps API key (GOOGLE-API-KEY) is not configured."}

    google_places_service = GooglePlacesService(api_key)
    return google_places_service.search_hotels(city, preferences, language)
