import os
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from agent_framework import tool
from tavily import TavilyClient


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


def _force_currency(url: str, currency: str) -> str:
    """Force booking.com links to the target currency/locale."""
    if "booking.com" not in url:
        return url

    locale = LOCALE_BY_CURRENCY[currency]
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["selected_currency"] = currency
    query["lang"] = locale["booking_lang"]
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
            check_in: Check-in date in YYYY-MM-DD format (optional).
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
                query=self._build_query(city, check_in, check_out, currency),
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
                "url": _force_currency(r.get("url", ""), currency),
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
    def _build_query(city: str, check_in: str | None, check_out: str | None, currency: str) -> str:
        """Build a search query in the language matching the target currency."""
        date_hint = ""
        if check_in:
            date_hint += f" check-in {check_in}"
        if check_out:
            date_hint += f" check-out {check_out}"

        if currency == "PLN":
            return f"hotele {city}{date_hint} cena za noc w złotówkach opinie rezerwacja"
        return f"hotels in {city}{date_hint} price per night USD rating reviews booking"


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
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"error": "Hotel search API key (TAVILY_API_KEY) is not configured."}

    hotel_service = HotelService(api_key)
    return hotel_service.search_hotels(city, check_in, check_out, language)
