import os
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from agent_framework import tool
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from tavily import TavilyClient

from classes.secret_manager_services import SecretManager

# Same Key Vault mechanism as in agent_execution.py
key_vault_url = os.getenv("KEY_VAULT_URL")
credential = DefaultAzureCredential()
secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
secret_manager = SecretManager(secret_client)


# Booking.com renders prices in whatever currency/language is selected via
# its query params, so the search query, the Tavily country filter and the
# returned links all need to agree on the same target currency for a given
# chat language to keep hotel offers consistent.
LOCALE_BY_CURRENCY = {
    "PLN": {"country": "poland", "booking_lang": "pl"},
    "USD": {"country": "united states", "booking_lang": "en-us"},
}

# Booking sites whose pages contain name, price, rating and reviews
HOTEL_DOMAINS = ["booking.com", "hotels.com", "tripadvisor.com"]
MAX_RESULTS = 8


def target_currency(language: str) -> str:
    return "PLN" if (language or "").strip().lower().startswith("pl") else "USD"


def _prepare_booking_url(url: str, currency: str, check_in: str | None, check_out: str | None) -> str:
    """Force booking.com links to the target currency/locale and to the travel dates,
    so the page opens with the offers for the requested stay."""
    if "booking.com" not in url:
        return url

    locale = LOCALE_BY_CURRENCY[currency]
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["selected_currency"] = currency
    query["lang"] = locale["booking_lang"]
    if check_in:
        query["checkin"] = check_in
    if check_out:
        query["checkout"] = check_out
    return urlunsplit(parts._replace(query=urlencode(query)))


def _is_direct_hotel_url(url: str) -> bool:
    """Direct single-property pages (e.g. booking.com/hotel/pl/xyz.html) are
    more useful to the user than city/category overview pages. Excludes
    /reviews/.../hotel/... pages, which also match "/hotel/" but link to a
    review listing rather than the bookable property page."""
    path = urlsplit(url).path
    return "/hotel/" in path and "/reviews/" not in path


class HotelService:
    def __init__(self, api_key: str):
        self.client = TavilyClient(api_key=api_key)

    def search_hotels(
        self, city: str, check_in: str | None = None, check_out: str | None = None, language: str = "en"
    ) -> Dict[str, Any]:
        """
        Search for hotels in a given city using Tavily web search.
        Returns raw search results from booking sites so the agent can extract
        structured hotel data (name, price, rating, reviews, highlights).

        Args:
            city: The city name to search hotels in.
            check_in: Check-in date in YYYY-MM-DD format (optional). The dates are
                not part of the search text; they are added to the booking.com links.
            check_out: Check-out date in YYYY-MM-DD format (optional).
            language: ISO 639-1 language code of the chat, used to pick the
                target currency: "pl" -> PLN, anything else -> USD.

        Returns:
            Dict with "target_currency" (PLN or USD, the ONLY currency the agent
            should report) and "results" list (each entry has url, title,
            content, score, is_direct), or {"error": "..."} on failure.
        """
        if not city:
            return {"error": "No city provided."}

        currency = target_currency(language)
        try:
            response = self.client.search(
                query=self._build_query(city, currency),
                search_depth="advanced",
                max_results=MAX_RESULTS,
                include_domains=HOTEL_DOMAINS,
                country=LOCALE_BY_CURRENCY[currency]["country"],
            )
        except Exception as exc:
            return {"error": f"Hotel search failed: {exc}"}

        results = response.get("results", [])
        if not results:
            return {"error": f"No hotel results found for '{city}'."}

        simplified = [
            {
                "url": _prepare_booking_url(r.get("url", ""), currency, check_in, check_out),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0.0),
                "is_direct": _is_direct_hotel_url(r.get("url", "")),
            }
            for r in results
        ]
        # Direct single-property pages first — they're more useful to the
        # user than generic city/category overview pages.
        simplified.sort(key=lambda r: not r["is_direct"])

        return {
            "city": city,
            "check_in": check_in,
            "check_out": check_out,
            "target_currency": currency,
            "results": simplified,
        }

    @staticmethod
    def _build_query(city: str, currency: str) -> str:
        """Build a short search query in the language matching the target currency.

        Tavily searches indexed web pages, not live availability. Dates and words such as
        "price per night" or "booking" in the query do not filter hotel offers; they pull in
        cruises, flights and forum topics, so the query only names what is searched.
        """
        if currency == "PLN":
            return f"hotele {city}"
        return f"hotels in {city}"


@tool
def search_for_hotels(
    city: str, check_in: str | None = None, check_out: str | None = None, language: str = "en"
) -> Dict[str, Any]:
    """
    Tool function to search for hotels in a given city on booking sites.
    Returns a dictionary with hotel search results, or {"error": "message"} on failure.

    Args:
        city: The city in which to search for hotels.
        check_in: Check-in date in YYYY-MM-DD format (optional).
        check_out: Check-out date in YYYY-MM-DD format (optional).
        language: ISO 639-1 language code of the chat ("pl" -> prices in PLN, otherwise USD).

    Returns:
        Dict with "target_currency" and "results" (url, title, content, score, is_direct),
        or {"error": "..."} if the call failed.
    """
    try:
        api_key = secret_manager.get_secret("TAVILY-API-KEY")
    except Exception:
        return {"error": "Hotel search API key (TAVILY-API-KEY) is not configured."}

    hotel_service = HotelService(api_key)
    return hotel_service.search_hotels(city, check_in, check_out, language)
