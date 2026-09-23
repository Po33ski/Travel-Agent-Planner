"""Shared utilities for weather tools."""


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
