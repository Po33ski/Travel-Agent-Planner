"""Shared utilities: the dated task message, the console login of the chats and helpers for the weather tools."""

import re
from datetime import date

# First words of the date line; the system prompts describe the line
TODAYS_DATE_PREFIX = "Today's date:"

# Demo check only: something@domain.tld
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def add_todays_date(message: str) -> str:
    """Put today's date in front of the user's message.

    The agents do not know the current date. They need it to turn "next week" into dates
    and to tell whether a trip is within the 15 days that the weather forecast covers.
    The system prompts of the manager, the single agent, the concierge and its subagents
    describe this first line.
    """
    today = date.today()
    return f"{TODAYS_DATE_PREFIX} {today.isoformat()} ({today:%A})\n\n{message}"


def ask_user_name() -> str | None:
    """Ask for the user name until a non-empty one is given; None when the user quits."""
    while True:
        try:
            user_name = input("User name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if user_name:
            return user_name
        print("The user name is required.")


def ask_user_email(stored_email: str | None) -> str | None:
    """Ask for the e-mail address until a valid one is given; Enter keeps the stored one. None when the user quits."""
    prompt = f"E-mail [{stored_email}]: " if stored_email else "E-mail: "
    while True:
        try:
            email = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not email and stored_email:
            return stored_email
        if EMAIL_PATTERN.match(email):
            return email
        print("A valid e-mail address is required.")


def _time_to_hhmm(value: str | None) -> str | None:
    """Truncate HH:MM:SS to HH:MM."""
    if not value or not isinstance(value, str):
        return value
    parts = value.strip().split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return value


def normalize_sunrise_sunset(data: dict) -> dict:
    """Convert sunrise/sunset from HH:MM:SS to HH:MM in API response."""
    result = dict(data)
    if "currentConditions" in result and isinstance(result["currentConditions"], dict):
        current = dict(result["currentConditions"])
        current["sunrise"] = _time_to_hhmm(current.get("sunrise"))
        current["sunset"] = _time_to_hhmm(current.get("sunset"))
        result["currentConditions"] = current
    if "days" in result:
        result["days"] = [
            {
                **day,
                "sunrise": _time_to_hhmm(day.get("sunrise")),
                "sunset": _time_to_hhmm(day.get("sunset")),
            }
            for day in result["days"]
        ]
    return result
